"""One unattended research session: `fr lab session`.

Claude Code run headless on deploy/passes/lab.md, signed in with the token in .env like the trading passes. The run
can read the repo, write idea files, source files and journal/lab/, and run the lab's own commands. Everything else
is refused: no web, no agents, no other shell command, no gateway, no holdout look, and no edits to the engine, the
bar or the ledger's rules (those change only in a session with the owner).

A session is `ok` when the ledger grew or the log was written. A new candidate sends a phone alert (no numbers
about any account are in it).
"""

import json
import os
import subprocess
from datetime import datetime
from pathlib import Path

from . import ROOT
from ..config import CT

MODEL, EFFORT = "claude-fable-5-2", "high"
TIMEOUT = 75 * 60
PROMPT = Path("deploy/passes/lab.md")
LOGS = Path("data/pass-logs")
HERE = Path(__file__).resolve().parent
ALLOW = ["Read", "Glob", "Grep",
         f"Write(/{HERE}/ideas/**)", f"Edit(/{HERE}/ideas/**)", f"Write(/{HERE}/sources/**)", f"Edit(/{HERE}/sources/**)",
         f"Write(/{ROOT.resolve()}/**)", f"Edit(/{ROOT.resolve()}/**)",
         "Bash(uv run fr lab run:*)", "Bash(uv run fr lab board)", "Bash(uv run fr lab ideas)", "Bash(uv run fr lab show:*)",
         "Bash(uv run fr lab fetch:*)"]


def _candidates() -> set:
    from . import registry
    return {(i, v) for (i, v), t in registry.latest().items() if t["verdict"] == "candidate"}


def run() -> dict:
    from .. import passes, watch
    from . import registry

    exe, token = passes.binary(), os.environ.get("FR_CLAUDE_TOKEN")
    if not exe or not token:
        return {"ok": False, "error": "Claude Code not found" if not exe else "FR_CLAUDE_TOKEN is not set in .env"}
    start = datetime.now(CT)
    before, lines = _candidates(), len(registry.all_trials())
    log = ROOT / "log.md"
    stamp = log.stat().st_mtime if log.exists() else None
    cmd = [exe, "-p", "--model", MODEL, "--effort", EFFORT, "--output-format", "json", "--permission-mode", "dontAsk",
           "--allowedTools", *ALLOW]
    env = {**os.environ, "CLAUDE_CODE_OAUTH_TOKEN": token}
    try:
        done = subprocess.run(cmd, input=PROMPT.read_text(), capture_output=True, text=True, timeout=TIMEOUT, env=env)
        reply = json.loads(done.stdout)
    except (OSError, subprocess.TimeoutExpired, ValueError) as e:
        reply = {"is_error": True, "result": repr(e)[:300]}
    new = sorted(_candidates() - before)
    ran = len(registry.all_trials()) - lines
    out = {"started": start.isoformat(timespec="seconds"), "minutes": round((datetime.now(CT) - start).total_seconds() / 60, 1),
           "ok": not reply.get("is_error") and (ran > 0 or (log.exists() and log.stat().st_mtime != stamp)),
           "variants_run": ran, "new_candidates": [f"{i}:{v}" for i, v in new], "turns": reply.get("num_turns"),
           "refused": [f"{d.get('tool_name')}: {str(d.get('tool_input'))[:120]}" for d in reply.get("permission_denials", [])],
           "message": reply.get("result")}
    LOGS.mkdir(parents=True, exist_ok=True)
    (LOGS / f"{start:%Y-%m-%d-%H%M}-lab.json").write_text(json.dumps(out, indent=1) + "\n")
    if new:
        watch.send("Research lab: a candidate", f"{len(new)} idea variant(s) passed discovery and validation: "
                   f"{', '.join(out['new_candidates'][:4])}. Next step is the holdout look. See journal/lab/board.md.")
    return out
