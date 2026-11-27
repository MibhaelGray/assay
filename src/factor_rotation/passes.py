"""The swing book's research passes run as background jobs, outside the Claude app: `fr pass night`.

Each pass is Claude Code run headless on the prompt in deploy/passes/<name>.md, signed in with the token in
.env (FR_CLAUDE_TOKEN, from `claude setup-token`). The run can read files, search the web, spawn research
agents and run only the commands listed here; everything else is refused, whatever the Mac's own Claude
settings allow. Drafts go in data/pass-drafts/.

`--shadow` is the trial mode: the pass works on a copy of the journal in data/shadow-journal and records
there with orders off, so a prompt change can be tried without touching the real record.
"""

import json
import logging
import os
import re
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path

from . import journal, storage, transcripts
from .config import NY  # importing config also loads FR_* settings from .env, the token among them

log = logging.getLogger(__name__)

MODEL, EFFORT = "claude-fable-5-2", "high"   # the pass itself; its agents run on Sonnet 5.5 at medium (the skill, "Models")
TIMEOUT = 150 * 60   # a pass researches every candidate: an hour to an hour and a half, sometimes more
AGENT_MODEL = "claude-sonnet-5-5"   # the default for any agent the pass starts without naming a model
# Claude Code gives a headless run 200 web searches, shared by every agent in it: eight a name on 2026-11-16, when
# nearly every agent ran out. This many lets 40 names have 40 or more each (CLAUDE_CODE_MAX_WEB_SEARCHES_PER_SESSION).
WEB_SEARCHES = 2500
PROMPTS = Path("deploy/passes")
DRAFTS = Path("data/pass-drafts")
SHADOW = Path("data/shadow-journal")
LOGS = Path("data/pass-logs")
APP_BUILDS = Path.home() / "Library" / "Application Support" / "Claude" / "claude-code"
READING = ["Read", "Glob", "Grep", "WebSearch", "WebFetch", "Agent", "Skill",
           f"Write({DRAFTS}/**)", f"Edit({DRAFTS}/**)", "Bash(uv run fr trading-day)", "Bash(uv run fr paper desk)",
           "Bash(uv run fr quotes:*)", "Bash(uv run fr edgar read:*)", "Bash(uv run fr paper packet:*)"]
# Programmatic guard enforcing Folder 2 isolation (uninfluenced judge) and secret protection in headless passes.
DISALLOWED = [
    "Read(.env)", "Read(.env.*)", "Grep(.env)", "Grep(.env.*)", "Glob(.env*)",
    "Read(**/lessons.md)", "Grep(**/lessons.md)",
    "Read(**/paper/side/**)", "Grep(**/paper/side/**)", "Glob(**/paper/side/**)",
    "Read(**/paper/postmortems/**)", "Grep(**/paper/postmortems/**)", "Glob(**/paper/postmortems/**)",
    "Read(**/journal/archive/**)", "Grep(**/journal/archive/**)", "Glob(**/journal/archive/**)",
    "Read(**/research/agents/**)", "Grep(**/research/agents/**)", "Glob(**/research/agents/**)",
    "Read(**/research/transcripts/**)", "Grep(**/research/transcripts/**)", "Glob(**/research/transcripts/**)",
    "Bash(uv run fr paper learn*)", "Bash(*fr paper learn*)",
    "Bash(uv run fr paper holds*)", "Bash(*fr paper holds*)",
    "Bash(uv run fr paper side*)", "Bash(*fr paper side*)",
    "Bash(uv run fr paper enter*)", "Bash(*fr paper enter*)",
    "Bash(uv run fr paper close*)", "Bash(*fr paper close*)",
    "Bash(uv run fr paper sync*)", "Bash(*fr paper sync*)",
]
# What each pass may run on top of the reading tools. Each leaves the session's proposals file rewritten
# (paper/daily.py). Headless since 2026-11-16 so the judge is always Fable 5.2 at high effort: a pass scheduled in
# the Claude app runs on whatever model the app is set to. "late" is a second night pass, by hand (`fr pass late`).
PASSES = {slot: {"prompt": f"swing-{'late' if slot.startswith('late') else slot}.md", "allow": [
    f"Bash(uv run fr screen --live {slot})", f"Bash(uv run fr paper propose {DRAFTS}/{slot}.json)",
    f"Bash(uv run fr paper postmortem {DRAFTS}/postmortem.json)", "Bash(uv run fr paper depth)",
    "Bash(uv run fr paper hedges:*)", "Bash(uv run fr paper status)"]}
          for slot in ("night", "late", "late2", "late3", "late4", "morning", "midday")}
HEADLESS = f"""

Running as a background job, not in the Claude app (the owner's setup, 2026-11-16):
- You are the judge, on Fable 5.2 at high effort. Start every research agent with the Agent tool's `model: "sonnet"`
  and `effort: "medium"`, one name to an agent, as the skill says.
- The working directory is already the repository root. Run every `uv run fr ...` command directly without prefixing
  `cd ... &&` (chained `cd` commands are refused by the tool allowlist).
- There is no IBKR connector here. Take market numbers from `uv run fr quotes` (for example
  `uv run fr quotes SPY QQQ IWM ^VIX ^TNX CL=F ES=F NQ=F`), Yahoo prices.
- Write every draft under `{DRAFTS}/`: the pass's draft as `{DRAFTS}/<pass>.json` (night.json, late.json, morning.json
  or midday.json) and post-mortems as `{DRAFTS}/postmortem.json`. Those are the only paths the record commands accept.
- The research note goes in `$FR_JOURNAL_DIR/research/<session>-<pass>.md`; FR_JOURNAL_DIR is set for you.
  Never run `fr paper learn` or read/edit `journal/paper/lessons.md`: scorecards and lessons are owner-only while the repository builds. Commands not listed for this pass are refused.
"""

# Put before the late pass's prompt when the session already has one.
AGAIN = """This session already has a late pass recorded, so this run's pass name is `{name}`. Wherever the text below or
the skill says `late` as a pass name, use `{name}`: `uv run fr screen --live {name}`, `"pass": "{name}"` in the draft,
the draft file `{name}.json`, and the note `<session>-{name}.md`. Everything else said of the late pass holds.

"""


def binary() -> str | None:
    """Claude Code on the PATH, or the newest copy the desktop app keeps."""
    if found := shutil.which("claude"):
        return found
    builds = sorted(APP_BUILDS.glob("*/*/claude.app/Contents/MacOS/claude"),
                    key=lambda p: [int(x) for x in re.findall(r"\d+", p.parts[-6])])
    return str(builds[-1]) if builds else None


def _shadow_journal() -> Path:
    """A fresh copy of the journal for a trial run."""
    if SHADOW.exists():
        shutil.rmtree(SHADOW)
    shutil.copytree(journal.JOURNAL, SHADOW)
    return SHADOW


def _is_recorded(root: Path, session, name: str) -> bool:
    """True when `name` is already recorded in `proposals/<session>.json` under `root`."""
    path = root / "paper" / "proposals" / f"{session}.json"
    if not path.exists():
        return False
    try:
        doc = json.loads(path.read_text())
    except (OSError, ValueError):
        return False
    return any(p.get("pass") == name for p in doc.get("passes", []))


def run(name: str, shadow: bool = False) -> dict:
    """Run a pass, telling the owner's phone when a real one starts and at once if it fails (watch.py, since
    2026-11-17, the owner's request). A shadow run or an already-recorded pass says nothing."""
    from . import watch
    from .config import CT
    from .paper import proposals

    if shadow:
        return _run(name, shadow)
    now = datetime.now(NY)
    # Idempotency guard BEFORE sending a phone alert or spending tokens on a duplicate run.
    if name != "late":
        try:
            sess = proposals.pass_session(name, now)
            if _is_recorded(journal.JOURNAL, sess, name):
                leaves = f"paper/proposals/{sess}.json"
                return {"pass": name, "ok": True, "already_recorded": True, "shadow": False,
                        "started": now.isoformat(timespec="seconds"), "minutes": 0.0,
                        "recorded": str(journal.JOURNAL / leaves), "message": f"The {name} pass for {sess} is already recorded."}
        except ValueError:
            pass
    start = datetime.now(CT)
    label = "late" if name.startswith("late") else name
    watch.send(f"{label.capitalize()} pass started", f"The {label} pass started at {start:%-I:%M %p} CT. It usually takes 15 to 90 minutes.")
    try:
        out = _run(name, shadow)
    except Exception as e:
        out = {"pass": name, "ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}
    took = round((datetime.now(CT) - start).total_seconds() / 60)
    if out.get("ok"):
        if not out.get("already_recorded"):
            watch.send(f"{label.capitalize()} pass recorded", f"The {label} pass recorded after {took} minutes; "
                       "its takes go to the order job now.")
    else:
        why = out.get("error") or "it ended without recording its verdicts"
        watch.send(f"{label.capitalize()} pass FAILED", f"The {label} pass that started at {start:%-I:%M %p} CT failed after {took} minutes: {why[:200]}. Log: data/pass-logs.")
    return out


def _keep(session_id: str | None, root: Path, label: str) -> str | None:
    """Copy the judge's own record of the pass into the journal (transcripts.py), where the nightly job commits it.
    Whatever goes wrong here, the pass itself has already run: say so in the log and carry on."""
    try:
        source = transcripts.find(session_id)
        if not source:
            return None
        dest = root / transcripts.DIR / f"{label}.jsonl.gz"
        session, _, name = label.rpartition("-")
        transcripts.archive(source, dest, {"session": session, "pass": name, "session_id": session_id, "code": transcripts.code_version()})
        return str(dest)
    except Exception as e:   # noqa: BLE001
        log.warning("could not keep the pass transcript: %s: %s", type(e).__name__, e)
        return None


TRANSIENT_RETRY_MAX_MIN = 3.0   # retry once if Claude Code crashes/fails quickly without recording
TRANSIENT_RETRY_WAIT = 30       # seconds before retrying a quick crash


def _run(name: str, shadow: bool = False) -> dict:
    from .paper import proposals

    now = datetime.now(NY)
    LOGS.mkdir(parents=True, exist_ok=True)
    log_path = LOGS / f"{now.date()}-{name}{'-shadow' if shadow else ''}.json"

    def fail(error: str) -> dict:
        out = {"pass": name, "ok": False, "shadow": shadow, "started": now.isoformat(timespec="seconds"),
               "minutes": round((datetime.now(NY) - now).total_seconds() / 60, 1), "error": error}
        if not shadow:
            storage.atomic_write_text(log_path, json.dumps(out, indent=1) + "\n")
        return out

    if name == "late":   # a late pass can run more than once a session: each run takes the next free name
        try:
            name = proposals.next_late(now)
            log_path = LOGS / f"{now.date()}-{name}{'-shadow' if shadow else ''}.json"
        except ValueError as e:
            return fail(str(e))
    spec = PASSES[name]
    exe, token = binary(), os.environ.get("FR_CLAUDE_TOKEN")
    if not exe or not token:
        return fail("Claude Code not found" if not exe else "FR_CLAUDE_TOKEN is not set in .env")
    try:
        session = proposals.pass_session(name, now)   # a pass leaves the proposals file of the session it is for
    except ValueError as e:
        return fail(f"not the time for a {name} pass: {e}")
    leaves = f"paper/proposals/{session}.json"
    root = _shadow_journal() if shadow else journal.JOURNAL
    if not shadow and _is_recorded(root, session, name):
        return {"pass": name, "ok": True, "already_recorded": True, "shadow": shadow,
                "started": now.isoformat(timespec="seconds"), "minutes": 0.0, "recorded": str(root / leaves)}
    DRAFTS.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "CLAUDE_CODE_OAUTH_TOKEN": token, "FR_JOURNAL_DIR": str(root.resolve()),
           "CLAUDE_CODE_SUBAGENT_MODEL": AGENT_MODEL, "CLAUDE_CODE_MAX_WEB_SEARCHES_PER_SESSION": str(WEB_SEARCHES),
           **({"FR_PAPER_SEND": "0"} if shadow else {})}
    writes = [f"Write(/{root.resolve()}/research/**)", f"Edit(/{root.resolve()}/research/**)"]   # "//" marks an absolute path
    cmd = [exe, "-p", "--model", MODEL, "--effort", EFFORT, "--output-format", "json", "--permission-mode", "dontAsk",
           "--allowedTools", *READING, *spec["allow"], *writes,
           "--disallowedTools", *DISALLOWED]
    prompt = (PROMPTS / spec["prompt"]).read_text() + HEADLESS
    if name in proposals.LATE[1:]:
        prompt = AGAIN.format(name=name) + prompt

    # Write a running sentinel immediately so watch.py knows the pass started and tracks its PID.
    storage.atomic_write_text(log_path, json.dumps({
        "pass": name, "session": session.isoformat(), "shadow": shadow, "status": "running",
        "pid": os.getpid(), "started": now.isoformat(timespec="seconds"),
    }, indent=1) + "\n")

    for attempt in (1, 2):
        done = None
        attempt_start = datetime.now(NY)
        try:
            done = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=TIMEOUT, env=env)
            reply = json.loads(done.stdout)
        except (OSError, subprocess.TimeoutExpired, ValueError) as e:
            elapsed = (datetime.now(NY) - attempt_start).total_seconds() / 60
            if attempt == 1 and elapsed < TRANSIENT_RETRY_MAX_MIN and not _is_recorded(root, session, name):
                try:
                    proposals.pass_session(name, datetime.now(NY))
                    log.warning("pass_quick_crash_retry pass=%s attempt=1 error=%r", name, e)
                    time.sleep(TRANSIENT_RETRY_WAIT)
                    continue
                except ValueError:
                    pass
            stderr_tail = (getattr(done, "stderr", "") or getattr(e, "stderr", "") or "")[-400:].strip()
            out = {"pass": name, "session": session.isoformat(), "ok": False, "shadow": shadow,
                   "started": now.isoformat(timespec="seconds"),
                   "minutes": round((datetime.now(NY) - now).total_seconds() / 60, 1),
                   "error": repr(e)[:300], **({"stderr": stderr_tail} if stderr_tail else {})}
            storage.atomic_write_text(log_path, json.dumps(out, indent=1) + "\n")
            return out
        recorded = _is_recorded(root, session, name)
        elapsed = (datetime.now(NY) - attempt_start).total_seconds() / 60
        if attempt == 1 and reply.get("is_error") and not recorded and elapsed < TRANSIENT_RETRY_MAX_MIN:
            try:
                proposals.pass_session(name, datetime.now(NY))
                log.warning("pass_quick_error_retry pass=%s attempt=1 result=%r", name, str(reply.get("result"))[:120])
                time.sleep(TRANSIENT_RETRY_WAIT)
                continue
            except ValueError:
                pass
        out = {"pass": name, "session": session.isoformat(), "shadow": shadow, "started": now.isoformat(timespec="seconds"),
               "minutes": round((datetime.now(NY) - now).total_seconds() / 60, 1),
               "ok": recorded and not reply.get("is_error"), "recorded": str(root / leaves) if recorded else None,
               "turns": reply.get("num_turns"), "agents": (reply.get("subagent_stats") or {}).get("spawned"),
               "refused": [f"{d.get('tool_name')}: {str(d.get('tool_input'))[:120]}" for d in reply.get("permission_denials", [])],
               "message": reply.get("result"), "transcript": _keep(reply.get("session_id"), root, f"{session}-{name}")}
        storage.atomic_write_text(log_path, json.dumps(out, indent=1) + "\n")
        return out
    raise RuntimeError("unreachable")
