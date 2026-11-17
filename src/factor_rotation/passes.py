"""The swing book's research passes run as background jobs, outside the Claude app: `fr pass pre-close`.

Each pass is Claude Code run headless on the prompt in deploy/passes/<name>.md, signed in with the token in
.env (FR_CLAUDE_TOKEN, from `claude setup-token`). The run can read files, search the web, spawn research
agents and run only the commands listed here; everything else is refused, whatever the Mac's own Claude
settings allow. Drafts go in data/pass-drafts/.

`--shadow` is the trial mode: the pass works on a copy of the journal in data/shadow-journal and records
there, so the pass scheduled in the Claude app stays the one that counts while the two are compared.
"""

import json
import logging
import os
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

from . import journal
from .config import NY
from .intraday.model import binary

log = logging.getLogger(__name__)

MODEL, EFFORT = "claude-fable-5-2", "high"   # the pass itself; its agents run on Opus 5.5 at medium (the skill, "Models")
TIMEOUT = 40 * 60
SWING_TIMEOUT = 150 * 60   # a swing pass researches every candidate: an hour to an hour and a half, sometimes more
AGENT_MODEL = "claude-opus-5-5"   # the default for any agent the pass starts without naming a model
PROMPTS = Path("deploy/passes")
DRAFTS = Path("data/pass-drafts")
SHADOW = Path("data/shadow-journal")
LOGS = Path("data/pass-logs")
READING = ["Read", "Glob", "Grep", "WebSearch", "WebFetch", "Agent", "Skill",
           f"Write({DRAFTS}/**)", f"Edit({DRAFTS}/**)", "Bash(uv run fr trading-day)", "Bash(uv run fr paper learn)",
           "Bash(uv run fr quotes:*)"]
# What each pass may run on top of the reading tools, and the journal file it has to leave behind.
PASSES = {
    "pre-close": {"allow": [f"Bash(uv run fr paper review {DRAFTS}/review.json)"], "leaves": "paper/reviews/{day}.json"},
}
# The three daily swing passes (paper/daily.py), headless since 2026-11-16 so the judge is always Fable 5.2 at high
# effort: a pass scheduled in the Claude app runs on whatever model the app is set to, and on 2026-11-16 the
# midday pass judged on Opus 5.5 at medium. Each leaves the session's proposals file rewritten.
for _slot in ("night", "morning", "midday"):
    PASSES[_slot] = {"prompt": f"swing-{_slot}.md", "slot": _slot, "timeout": SWING_TIMEOUT, "allow": [
        f"Bash(uv run fr screen --live {_slot})", f"Bash(uv run fr paper propose {DRAFTS}/{_slot}.json)",
        f"Bash(uv run fr paper postmortem {DRAFTS}/postmortem.json)", "Bash(uv run fr paper depth)",
        "Bash(uv run fr paper hedges:*)", "Bash(uv run fr paper status)"]}
HEADLESS = f"""

Running as a background job, not in the Claude app (the owner's setup, 2026-11-16):
- You are the judge, on Fable 5.2 at high effort. Start every research agent with the Agent tool's `model: "opus"`
  and `effort: "medium"`, one name to an agent, as the skill says.
- There is no IBKR connector here. Take market numbers from `uv run fr quotes` (for example
  `uv run fr quotes SPY QQQ IWM ^VIX ^TNX CL=F ES=F NQ=F`), Yahoo prices.
- Write every draft under `{DRAFTS}/`: the pass's draft as `{DRAFTS}/<pass>.json` (night.json, morning.json or
  midday.json) and post-mortems as `{DRAFTS}/postmortem.json`. Those are the only paths the record commands accept.
- The research note goes in `$FR_JOURNAL_DIR/research/<session>-<pass>.md`, and lessons in
  `$FR_JOURNAL_DIR/paper/lessons.md`; FR_JOURNAL_DIR is set for you. Commands not listed for this pass are refused.
"""


def _shadow_journal(leaves: str, keep: bool = False) -> Path:
    """A fresh copy of the journal without today's record of this pass, so the trial run starts where the real
    one did."""
    if SHADOW.exists():
        shutil.rmtree(SHADOW)
    shutil.copytree(journal.JOURNAL, SHADOW)
    if keep is False:
        (SHADOW / leaves).unlink(missing_ok=True)
    return SHADOW


def run(name: str, shadow: bool = False) -> dict:
    spec = PASSES[name]
    now = datetime.now(NY)
    exe, token = binary(), os.environ.get("FR_CLAUDE_TOKEN")
    if not exe or not token:
        return {"pass": name, "ok": False, "error": "Claude Code not found" if not exe else "FR_CLAUDE_TOKEN is not set in .env"}
    if spec.get("slot"):   # a swing pass leaves the proposals file of the session it is for
        from .paper import proposals
        try:
            session = proposals.pass_session(spec["slot"], now)
        except ValueError as e:
            return {"pass": name, "ok": False, "error": f"not the time for a {spec['slot']} pass: {e}"}
        leaves = f"paper/proposals/{session}.json"
    else:
        leaves = spec["leaves"].format(day=now.date())
    root = _shadow_journal(leaves, keep=bool(spec.get("slot"))) if shadow else journal.JOURNAL
    before = (root / leaves).stat().st_mtime if (root / leaves).exists() else None
    DRAFTS.mkdir(parents=True, exist_ok=True)
    LOGS.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "CLAUDE_CODE_OAUTH_TOKEN": token, "FR_JOURNAL_DIR": str(root.resolve()),
           "CLAUDE_CODE_SUBAGENT_MODEL": AGENT_MODEL, **({"FR_PAPER_SEND": "0"} if shadow else {})}
    writes = [f"Write(/{root.resolve()}/research/**)", f"Edit(/{root.resolve()}/research/**)",   # "//" marks an absolute path
              f"Edit(/{root.resolve()}/paper/lessons.md)"] if spec.get("slot") else []
    cmd = [exe, "-p", "--model", MODEL, "--effort", EFFORT, "--output-format", "json", "--permission-mode", "dontAsk",
           "--allowedTools", *READING, *spec["allow"], *writes]
    prompt = (PROMPTS / spec.get("prompt", f"{name}.md")).read_text() + (HEADLESS if spec.get("slot") else "")
    try:
        done = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=spec.get("timeout", TIMEOUT), env=env)
        reply = json.loads(done.stdout)
    except (OSError, subprocess.TimeoutExpired, ValueError) as e:
        return {"pass": name, "ok": False, "shadow": shadow, "error": repr(e)[:300]}
    after = (root / leaves).stat().st_mtime if (root / leaves).exists() else None
    out = {"pass": name, "shadow": shadow, "started": now.isoformat(timespec="seconds"),
           "minutes": round((datetime.now(NY) - now).total_seconds() / 60, 1),
           "ok": not reply.get("is_error") and after is not None and after != before, "recorded": str(root / leaves) if after and after != before else None,
           "turns": reply.get("num_turns"), "agents": (reply.get("subagent_stats") or {}).get("spawned"),
           "refused": [f"{d.get('tool_name')}: {str(d.get('tool_input'))[:120]}" for d in reply.get("permission_denials", [])],
           "message": reply.get("result")}
    (LOGS / f"{now.date()}-{name}{'-shadow' if shadow else ''}.json").write_text(json.dumps(out, indent=1) + "\n")
    return out
