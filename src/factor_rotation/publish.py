"""`fr publish`: put a fresh copy of the dashboard on the public GitHub site.

The owner asked on 2026-11-18 for a public site whose Refresh button shows fresh data. A GitHub site has no server,
so the page can't pull anything: this Mac does the pull instead. The `publish` job runs `fr publish --pull` every
15 minutes, which does what the local Refresh button does (today's prices, the paper account's fills and value) and
pushes the page as index.html to the repo's gh-pages branch, which GitHub Pages serves. The page's Refresh button
then loads the newest copy. A page that hasn't changed isn't pushed. The branch holds only that page; main and the
working tree are not touched. The site is public.
"""

import hashlib
import json
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path

from . import localdash
from .config import CT

BRANCH = "gh-pages"
STATE = Path("data/publish.json")


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def site_url(remote: str) -> str:
    """https://github.com/Owner/repo.git or git@github.com:Owner/repo.git -> https://owner.github.io/repo/"""
    path = remote.removesuffix(".git").replace("git@github.com:", "").replace("https://github.com/", "")
    owner, repo = path.split("/")[-2:]
    return f"https://{owner.lower()}.github.io/{repo}/"


def stamp(html: str, now: datetime) -> str:
    """Mark the page with when it was published; the page shows it beside Refresh."""
    return html.replace("</head>", f'<meta name="fr-published" content="{now:%b %-d, %-I:%M %p} CT">\n</head>', 1)


def _last() -> str:
    try:
        return json.loads(STATE.read_text()).get("hash", "")
    except (OSError, ValueError):
        return ""


def run(root: Path = Path("."), pull: bool = False, force: bool = False) -> dict:
    remote = _git("remote", "get-url", "origin", cwd=root)
    now = datetime.now(CT)
    notes = {}
    if pull:
        # The local Refresh button's pull (serve.refresh) without its background threads: this process ends as
        # soon as the page is pushed, and a thread still writing bars then dies mid-pull.
        from . import bars, serve
        paper = serve._sync_paper()
        notes = {**bars.collect_quotes(serve._symbols()), "paper": paper}
    with tempfile.TemporaryDirectory() as d:
        site = Path(d)
        info = localdash.build(site / "index.html")
        html = (site / "index.html").read_text()
        digest = hashlib.sha256(html.encode()).hexdigest()
        out = {"url": site_url(remote), "market": info["market"], "at": f"{now:%-I:%M %p} CT", "pushed": False,
               "paper": notes.get("paper"), "failed": len(notes.get("failed") or [])}
        if digest == _last() and not force:
            return out
        (site / "index.html").write_text(stamp(html, now))
        (site / ".nojekyll").write_text("")
        _git("init", "-q", "-b", BRANCH, cwd=site)
        _git("config", "user.name", _git("config", "user.name", cwd=root), cwd=site)
        _git("config", "user.email", _git("log", "-1", "--format=%ae", cwd=root), cwd=site)
        _git("add", "-A", cwd=site)
        _git("commit", "-q", "-m", f"Dashboard published {now:%Y-%m-%d %-I:%M %p} CT", cwd=site)
        _git("push", "-q", "-f", remote, BRANCH, cwd=site)
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps({"hash": digest, "at": now.isoformat(timespec="seconds")}))
    return {**out, "pushed": True}
