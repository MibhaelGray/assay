"""The judge's own record of a pass, kept in the journal: journal/research/transcripts/<session>-<pass>.jsonl.gz.

Claude Code writes every session to ~/.claude/projects/, outside the repo and with no backup. That file is the only
record of what the judge did between reading the screen and recording its verdicts: what it said, every command,
search and file read, and what came back. This copies the part worth keeping into the journal, where the nightly
job commits it.

The repo is public and the raw file is not fit for it: it carries the owner's email, their memory notes and the
whole system prompt. So the copy is built from a short list of record kinds (the prompt, the judge's text, its tool
calls, their results, and the notices that bring an agent's report back) and everything else is left out; then
home paths, emails, account numbers and the values in .env are replaced.

Gzipped on purpose: a pass searches journal/research with Grep, which skips a compressed file, so an old transcript
can never land in a later judge's context.

No pass reads these. They are for the owner: the frozen inputs for replaying a judge on a past night.
"""

import gzip
import json
import os
import re
import subprocess
from pathlib import Path

PROJECTS = Path.home() / ".claude" / "projects"
DIR = "research/transcripts"   # under the journal
# Settings whose values must never reach the repo, from the environment and the two .env files.
SECRET_KEYS = re.compile(r"TOKEN|ACCOUNT|TOPIC|CHAT|USERID|PASSWORD|SECRET|API_KEY|USER_AGENT")
REMINDER = re.compile(r"<system-reminder>.*?</system-reminder>\s*", re.S)
EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]*\w")
ACCOUNT = re.compile(r"\bD?U\d{6,9}\b")   # IBKR account numbers, paper (DU) and live (U)
TOKEN = re.compile(r"sk-ant-[\w-]{20,}|\b\d{8,10}:[\w-]{30,}")   # Claude tokens, Telegram bot tokens


def secrets() -> list[str]:
    """Every private setting's value, longest first so one value inside another is still replaced whole."""
    found = {v for k, v in os.environ.items() if SECRET_KEYS.search(k) and (k.startswith(("FR_", "SEC_", "TWS_", "CLAUDE_")))}
    for name in (".env", ".env.paper"):
        path = Path(name)
        if path.is_file():
            for line in path.read_text().splitlines():
                key, sep, value = line.strip().partition("=")
                if sep and not key.startswith("#") and SECRET_KEYS.search(key):
                    found.add(value.strip().strip('"'))
    return sorted((v for v in found if len(v) >= 6), key=len, reverse=True)


def scrubber(private: list[str] | None = None, home: Path | None = None, repo: Path | None = None):
    """A function that makes one string safe for the public repo."""
    private = secrets() if private is None else private
    home, repo = home or Path.home(), (repo or Path.cwd()).resolve()
    # The repo's own path first (it sits inside home), then home, then the form Claude Code uses in folder names.
    paths = [(str(repo), "."), (str(home), "~"), ("-" + "-".join(home.parts[1:]) + "-", "-~-")]

    def scrub(text: str) -> str:
        text = REMINDER.sub("", text)
        for value in private:
            text = text.replace(value, "<redacted>")
        for old, new in paths:
            text = text.replace(old, new)
        return ACCOUNT.sub("<account>", TOKEN.sub("<redacted>", EMAIL.sub("<email>", text)))

    return scrub


def _clean(value, scrub):
    if isinstance(value, str):
        return scrub(value)
    if isinstance(value, list):
        return [_clean(v, scrub) for v in value]
    if isinstance(value, dict):
        return {k: _clean(v, scrub) for k, v in value.items()}
    return value


def _text(content) -> str:
    """A tool result's content as the text the judge saw."""
    if isinstance(content, str):
        return content
    parts = []
    for block in content or []:
        if isinstance(block, dict):
            parts.append(block["text"] if block.get("type") == "text" else f"[{block.get('type')}]")
    return "\n".join(parts)


def slim(lines, scrub) -> tuple[dict, list[dict]]:
    """Facts about the run and its records in order: prompt, judge, thinking, call, result, notice."""
    facts: dict = {}
    records: list[dict] = []
    tools: dict[str, str] = {}   # tool call id -> tool name
    models: dict[str, int] = {}
    for line in lines:
        try:
            d = json.loads(line)
        except ValueError:
            continue
        kind, at = d.get("type"), d.get("timestamp")
        if kind in ("user", "assistant"):
            facts.setdefault("started", at)
            facts["ended"] = at or facts.get("ended")
            for key, name in (("version", "claude_code"), ("entrypoint", "entrypoint"), ("effort", "effort")):
                if d.get(key) and name not in facts:
                    facts[name] = d[key]
            if kind == "assistant" and d.get("requestedModel"):   # the judge's model, counted by reply: the app once ran a pass on another
                models[d["requestedModel"]] = models.get(d["requestedModel"], 0) + 1
        if kind == "user" and not d.get("isMeta"):
            content = d["message"].get("content")
            if isinstance(content, str):
                records.append({"at": at, "kind": "prompt", "text": content})
                continue
            for block in content or []:
                if block.get("type") == "text":
                    records.append({"at": at, "kind": "prompt", "text": block["text"]})
                elif block.get("type") == "tool_result":
                    records.append({"at": at, "kind": "result", "id": block.get("tool_use_id"), "tool": tools.get(block.get("tool_use_id")),
                                    **({"error": True} if block.get("is_error") else {}), "text": _text(block.get("content"))})
        elif kind == "assistant":
            for block in d["message"].get("content") or []:
                what = block.get("type")
                if what == "text":
                    records.append({"at": at, "kind": "judge", "text": block["text"]})
                elif what == "thinking":   # Claude Code saves the text only when asked to (passes.SETTINGS); else just how long
                    records.append({"at": at, "kind": "thinking", "ms": d.get("thinkingDurationMs"), "text": block.get("thinking") or ""})
                elif what == "tool_use":
                    tools[block["id"]] = block["name"]
                    records.append({"at": at, "kind": "call", "id": block["id"], "tool": block["name"], "input": block.get("input")})
        elif kind == "attachment" and (d.get("attachment") or {}).get("type") == "queued_command":
            # A background command or a research agent reporting back: this is how an agent's write-up reaches the judge.
            records.append({"at": d["attachment"].get("timestamp") or at, "kind": "notice", "text": d["attachment"].get("prompt") or ""})
        elif kind == "cost-state":
            facts["cost_usd"] = round(d.get("totalCostUSD") or 0, 2)
            facts["usage"] = {model: {k: v for k, v in use.items() if k.endswith("Tokens") or k == "webSearchRequests"}
                              for model, use in (d.get("modelUsage") or {}).items()}
    if models:
        facts["model"] = max(models, key=models.get)
        if len(models) > 1:
            facts["models"] = models
    facts["counts"] = {k: sum(r["kind"] == k for r in records) for k in ("judge", "thinking", "call", "result", "notice")}
    facts["thinking_saved"] = any(r["kind"] == "thinking" and r["text"] for r in records)
    return _clean(facts, scrub), _clean(records, scrub)


def find(session_id: str) -> Path | None:
    """The session's file, whichever project folder Claude Code put it in."""
    return next(iter(PROJECTS.glob(f"*/{session_id}.jsonl")), None) if session_id else None


def code_version() -> str | None:
    """The commit the pass ran on, with a + when the working tree had uncommitted changes to tracked code."""
    try:
        sha = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, timeout=10).stdout.strip()
        dirty = subprocess.run(["git", "status", "--porcelain", "--untracked-files=no", "--", "src", "deploy", ".claude"],
                               capture_output=True, text=True, timeout=10).stdout.strip()
        return (sha + ("+" if dirty else "")) or None
    except (OSError, subprocess.TimeoutExpired):
        return None


def archive(source: Path, dest: Path, about: dict | None = None, scrub=None) -> dict:
    """Write the public copy of one session file to `dest` (a .jsonl.gz): a first line of facts, then the records."""
    scrub = scrub or scrubber()
    with source.open() as fh:
        facts, records = slim(fh, scrub)
    head = {"kind": "about", **(about or {}), **facts}
    body = "".join(json.dumps(r, ensure_ascii=False, separators=(",", ":")) + "\n" for r in [head, *records])
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb", mtime=0, filename="") as out:   # same bytes on a re-run
        out.write(body.encode())
    return head


def read(path: Path) -> list[dict]:
    with gzip.open(path, "rt") as fh:
        return [json.loads(line) for line in fh]
