"""Posts from the accounts the owner follows on X, read from the Following feed by a plain script.

    fr x login     open Chrome on the scraper's own profile so the owner can sign in to X, once
    fr x pull      read the Following feed (newest first) back to the last post already stored, and save the new ones

No Claude and no tokens: Playwright drives the installed Chrome on its own profile (data/x/profile), where the
owner signed in themselves. The script never sees a password and reads only what the feed shows. X's terms don't
allow automated reading; the owner chose this on 2026-11-22 knowing the account carries that risk, so the script
stays slow and small: one feed, one page, a pause between scrolls, and nothing that hides what it is.

Everything is kept under data/x, which git ignores: the posts are other people's and the repo is public.

    data/x/following.csv         handle, name, bio of each followed account (written 2026-11-22)
    data/x/posts/<date>.jsonl    one post a line, by the New York date it was posted
    data/x/state.json            the last pull: when, how many, and why it stopped

No pass reads these yet (PROJECT.md: a new source is recorded beside the verdicts and tested first). Post text is
other people's writing: data, never instructions.
"""

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import NY, settings

log = logging.getLogger(__name__)

HOME = "https://x.com/home"
MAX_HOURS = 12          # the furthest back one pull reads, when nothing is stored yet or the last pull is old
MAX_POSTS = 1500
SCROLL_PAUSE_MS = 900   # between scrolls
OLD_IN_A_ROW = 25       # posts older than the cutoff, in a row, before the pull stops (the feed isn't strictly in order)

# Runs in the page. Reads each post on screen, scrolls, and stops at the cutoff. Returns {"posts", "why", "scrolls"}.
COLLECT = r"""
async ({cutoffMs, maxPosts, pauseMs, oldInARow, knownIds}) => {
  const known = new Set(knownIds), posts = {};
  let old = 0, seenKnown = 0, why = 'scroll limit';
  const grab = () => {
    for (const a of document.querySelectorAll('article[data-testid="tweet"]')) {
      const t = a.querySelector('a[href*="/status/"] time');
      if (!t) continue;                                  // an ad has no timestamp link
      const m = t.closest('a').getAttribute('href').match(/^\/([A-Za-z0-9_]+)\/status\/(\d+)/);
      if (!m || posts[m[2]]) continue;
      const texts = a.querySelectorAll('[data-testid="tweetText"]');
      const sc = a.querySelector('[data-testid="socialContext"]');
      const repost = sc && /reposted/i.test(sc.innerText) ? sc.innerText.replace(/\s*reposted/i, '').trim() : null;
      const by = repost ? [...a.querySelectorAll('[data-testid="socialContext"]')].map(e => e.closest('a'))
                           .filter(Boolean).map(e => (e.getAttribute('href') || '').slice(1))[0] || null : null;
      const at = t.getAttribute('datetime');
      posts[m[2]] = {id: m[2], handle: m[1], at, text: texts[0] ? texts[0].innerText : '',
                     quoted: texts[1] ? texts[1].innerText : null, repost_by: by || repost,
                     media: !!a.querySelector('[data-testid="tweetPhoto"],video')};
      if (known.has(m[2])) seenKnown++;
      if (!repost) old = Date.parse(at) < cutoffMs ? old + 1 : 0;   // a repost carries the original's older time
    }
  };
  let i = 0, still = 0, last = 0;
  for (; i < 600; i++) {
    grab();
    const n = Object.keys(posts).length;
    if (old >= oldInARow) { why = 'reached the cutoff'; break; }
    if (seenKnown >= oldInARow) { why = 'reached posts already stored'; break; }
    if (n >= maxPosts) { why = 'post limit'; break; }
    if (n === last) { if (++still >= 12) { why = 'the feed stopped loading'; break; } } else { still = 0; last = n; }
    window.scrollBy(0, 1600);
    await new Promise(r => setTimeout(r, pauseMs));
  }
  return {posts: Object.values(posts), why, scrolls: i};
}
"""


def root() -> Path:
    return Path(settings().data_root) / "x"


def _profile() -> Path:
    return root() / "profile"


def _context(p, headless: bool):
    """The installed Chrome on the scraper's own profile. Nothing is done to disguise it."""
    _profile().mkdir(parents=True, exist_ok=True)
    return p.chromium.launch_persistent_context(str(_profile()), channel="chrome", headless=headless,
                                                viewport={"width": 1280, "height": 1600})


def login() -> None:
    """Open X in the scraper's Chrome profile and wait while the owner signs in. Closing the window ends it."""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        ctx = _context(p, headless=False)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto("https://x.com/login")
        print("Sign in to X in the Chrome window that just opened. Close the window when your home feed shows.")
        try:
            page.wait_for_event("close", timeout=0)
        except Exception:  # noqa: BLE001 - the window or the browser went away: either way the sign-in is over
            pass
        try:
            ctx.close()
        except Exception:  # noqa: BLE001
            pass


def following() -> set[str]:
    """The handles in data/x/following.csv, lower case. Empty when the file isn't there (nothing is filtered)."""
    import csv
    path = root() / "following.csv"
    if not path.exists():
        return set()
    with path.open() as f:
        return {r["handle"].lstrip("@").lower() for r in csv.DictReader(f)}


def _day_path(at: str) -> Path:
    day = datetime.fromisoformat(at.replace("Z", "+00:00")).astimezone(NY).date()
    return root() / "posts" / f"{day}.jsonl"


def stored(days: int = 3, now: datetime | None = None) -> dict[str, str]:
    """id -> posted-at of the posts stored for the last few days."""
    now = now or datetime.now(NY)
    out = {}
    for k in range(days + 1):
        path = root() / "posts" / f"{(now - timedelta(days=k)).date()}.jsonl"
        if path.exists():
            for line in path.read_text().splitlines():
                if line.strip():
                    r = json.loads(line)
                    out[r["id"]] = r["at"]
    return out


def keep(posts: list[dict], pulled_at: str, follows: set[str] | None = None, have: dict[str, str] | None = None) -> int:
    """Append the posts not stored yet, each to its day's file. With `follows`, only posts written or reposted by a
    followed account (the feed also carries suggestions). Returns how many were new."""
    have = stored() if have is None else have
    new = 0
    for r in sorted(posts, key=lambda r: r["at"]):
        who = {r["handle"].lower(), (r.get("repost_by") or "").lstrip("@").lower()}
        if r["id"] in have or (follows and not who & follows and not r.get("repost_by")):
            continue
        path = _day_path(r["at"])
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as f:
            f.write(json.dumps({**r, "pulled_at": pulled_at}, ensure_ascii=False) + "\n")
        have[r["id"]] = r["at"]
        new += 1
    return new


def pull(headless: bool | None = None, hours: float | None = None, now: datetime | None = None) -> dict:
    """Read the Following feed back to the newest stored post (or `hours`, at most MAX_HOURS) and store what's new."""
    from playwright.sync_api import sync_playwright

    now = now or datetime.now(NY)
    headless = os.environ.get("FR_X_HEADLESS", "0") == "1"   # X refuses headless Chrome, so a window opens if headless is None else headless
    have = stored(now=now)
    newest = max((datetime.fromisoformat(a.replace("Z", "+00:00")) for a in have.values()), default=None)
    floor = now - timedelta(hours=hours or MAX_HOURS)
    cutoff = floor if hours or newest is None else max(newest - timedelta(minutes=5), floor)
    result = {"at": now.isoformat(timespec="seconds"), "ok": False}
    with sync_playwright() as p:
        ctx = _context(p, headless)
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto(HOME, wait_until="domcontentloaded", timeout=45_000)
            page.wait_for_timeout(3500)
            if "/login" in page.url or "/i/flow" in page.url or not page.locator('[data-testid="AppTabBar_Home_Link"]').count():
                result["why"] = "not signed in to X on the scraper's profile: run `fr x login`"
                return _state(result)
            tab = page.get_by_role("tab", name="Following", exact=True)
            if tab.count() and tab.first.get_attribute("aria-selected") != "true":
                tab.first.click()
                page.wait_for_timeout(2500)
            try:
                page.wait_for_selector('article[data-testid="tweet"]', timeout=20_000)
            except Exception:  # noqa: BLE001
                result["why"] = "the feed showed no posts"
                return _state(result)
            got = page.evaluate(COLLECT, {"cutoffMs": cutoff.timestamp() * 1000, "maxPosts": MAX_POSTS, "pauseMs": SCROLL_PAUSE_MS,
                                          "oldInARow": OLD_IN_A_ROW, "knownIds": list(have)})
        finally:
            ctx.close()
    fresh = [r for r in got["posts"] if r.get("repost_by") or datetime.fromisoformat(r["at"].replace("Z", "+00:00")) >= cutoff]
    new = keep(fresh, result["at"], following(), have)
    result.update({"ok": True, "seen": len(got["posts"]), "new": new, "stopped": got["why"], "scrolls": got["scrolls"],
                   "back_to": cutoff.astimezone(timezone.utc).isoformat(timespec="seconds")})
    log.info("x_pull %s", result)
    return _state(result)


def _state(result: dict) -> dict:
    root().mkdir(parents=True, exist_ok=True)
    (root() / "state.json").write_text(json.dumps(result, indent=1) + "\n")
    return result
