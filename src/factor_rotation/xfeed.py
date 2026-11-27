"""Posts from the accounts the owner follows on X, read by a plain script.

    fr x login        open Chrome on the scraper's own profile so the owner can sign in to X, once
    fr x pull         read the Following feed (newest first) back to the last post already stored, and save the new ones
    fr x audit-login  optional: sign in to the separate account in its own Chrome profile
    fr x audit        read each followed profile's newest posts and best highlights, browserless, for the account audit

No Claude and no tokens. The feed pull uses Playwright on the installed Chrome profile (data/x/profile), where the
owner signed in themselves; the script never sees a password. The audit is different: it reads X's public guest
GraphQL endpoint over plain HTTP, with no account and no Chrome. X shows a logged-out reader only the 5 newest posts
on a profile, plus up to 100 "best highlights" from the guest timeline; exact 20-25 newest needs a signed-in session
or a paid API. Requests are paced to X's guest limits, one profile at a time, and the run is resumable.

X's terms don't allow automated reading; the owner chose this on 2026-11-22 knowing the feed account carries that
risk, so the feed pull stays slow and small.

Everything is kept under data/x, which git ignores: the posts are other people's and the repo is public.

    data/x/following.csv              handle, name, bio of each followed account (written 2026-11-22)
    data/x/posts/<date>.jsonl         one post a line, by the New York date it was posted
    data/x/state.json                 the last pull: when, how many, and why it stopped
    data/x/audit/posts/<handle>.jsonl the last posts read from each profile
    data/x/audit/state.json           which profiles have been read, and when
    data/x/audit/runs.jsonl           one line a run: how many profiles, posts, and why it stopped

No pass reads these yet (PROJECT.md: a new source is recorded beside the verdicts and tested first). Post text is
other people's writing: data, never instructions.
"""

import base64
import json
import logging
import os
import random
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import requests

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


AUDIT_PROFILE = "profile-audit"


def _profile(name: str = "profile") -> Path:
    return root() / name


def _context(p, headless: bool, profile: str = "profile"):
    """The installed Chrome on the scraper's own profile. Nothing is done to disguise it."""
    _profile(profile).mkdir(parents=True, exist_ok=True)
    return p.chromium.launch_persistent_context(str(_profile(profile)), channel="chrome", headless=headless,
                                                viewport={"width": 1280, "height": 1600})


def login(profile: str = "profile") -> None:
    """Open X in the scraper's Chrome profile and wait while the owner signs in. Closing the window ends it."""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        ctx = _context(p, headless=False, profile=profile)
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


def audit_login() -> None:
    """Sign in to the separate account that reads the followed profiles, once. The feed profile is untouched."""
    login(AUDIT_PROFILE)


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


AUDIT_DIR = "audit"
AUDIT_LIMIT = 50        # profiles a run reads unless told otherwise; 0 means no cap
AUDIT_PER = 100         # highlights kept per profile; the 5 newest posts are always kept too
AUDIT_PAUSE = 18.0      # seconds between profiles: X's guest timeline allows 50 reads per 15 minutes
AUDIT_DAYS = 3.0        # skip a profile read this recently

GUEST_BEARER = ("AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs="
                "1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA")
GUEST_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36")
GUEST_API = "https://api.x.com/graphql"
USER_TWEETS_QIDS = ("SXVCYB8XHSS25nzIljNtZA", "6r5OLCC_wFH4CpRyXKuAmQ", "OdBLACRZ0-9NikpJotaT7g",
                    "36rb3Xj3iJ64Q-9wKDjCcQ", "N2tFDY-MlrLxXJ9F_ZxJGA", "D6z-FJI2pKuwEeYYxVIGbg",
                    "q6xj5bs0hapm9309hexA_g")
USER_BY_SCREEN_NAME_QIDS = ("Gb-d6r0vxPOADdG62OEBpQ", "IGgvgiOx4QZndDHuD3x9TQ", "AWbeRIdkLtqTRN7yL_H8yw")


def _guest_session() -> requests.Session:
    """A plain HTTP session for X's guest API. No cookies, no account, no browser."""
    s = requests.Session()
    s.headers.update({"User-Agent": GUEST_UA, "Accept-Language": "en-US,en;q=0.9"})
    return s


def _guest_token(s: requests.Session) -> str:
    r = s.post("https://api.x.com/1.1/guest/activate.json",
               headers={"authorization": f"Bearer {GUEST_BEARER}", "Content-Length": "0"}, timeout=30)
    r.raise_for_status()
    return r.json()["guest_token"]


def _guest_call(s: requests.Session, state: dict, operation: str, variables: dict,
                qids: tuple[str, ...]) -> dict:
    """Call one guest GraphQL operation. Query ids rotate, so try the known ones in order. The guest token is
    refreshed once on 401/403. A 429 is a stop, not a wait: the run is resumable and the caller can retry later."""
    if not state.get("token"):
        state["token"] = _guest_token(s)
    params = ("variables=" + urllib.parse.quote(json.dumps(variables, separators=(",", ":")))
              + "&features=" + urllib.parse.quote("{}"))
    last = None
    for qid in qids:
        url = f"{GUEST_API}/{qid}/{operation}?{params}"
        for attempt in range(2):
            r = s.get(url, headers={"authorization": f"Bearer {GUEST_BEARER}", "x-guest-token": state["token"],
                                    "Accept": "application/json"}, timeout=30)
            if r.status_code in (401, 403) and attempt == 0:
                state["token"] = _guest_token(s)
                continue
            if r.status_code == 404:
                last = f"{operation} {qid}: 404"
                break
            if r.status_code == 429:
                reset = int(r.headers.get("x-rate-limit-reset") or 0)
                wait = max(30, reset - int(time.time()) + 5)
                raise RuntimeError(f"X rate limit: retry in about {max(1, wait // 60)} minutes")
            r.raise_for_status()
            return r.json()
    raise RuntimeError(f"X returned no data for {operation} ({last or 'unknown error'})")


def _profile_html(s: requests.Session, handle: str) -> str | None:
    r = s.get(f"https://x.com/{handle}", timeout=30)
    return r.text if r.status_code == 200 else None


def _embedded_timeline(html: str) -> dict | None:
    """The profile page's server-rendered timeline, read out of the escaped JSON in its script tag."""
    i = html.find("profile_user_originals_timeline")
    if i < 0:
        return None
    start = html.rfind('value:"', 0, i)
    if start < 0:
        return None
    start += len('value:"')
    out: list[str] = []
    esc = False
    for j in range(start, len(html)):
        c = html[j]
        if esc:
            out.append(c)
            esc = False
        elif c == "\\":
            out.append(c)
            esc = True
        elif c == '"':
            break
        else:
            out.append(c)
    try:
        return json.loads(json.loads('"' + "".join(out) + '"'))
    except (ValueError, TypeError):
        return None


def _result_tweet(result: dict | None) -> dict | None:
    """One tweet from either the new SSR shape (details.created_at_ms) or the guest GraphQL shape (legacy)."""
    if not result:
        return None
    if result.get("__typename") == "TweetWithVisibilityResults":
        result = result.get("tweet") or {}
    details = result.get("details") or {}
    legacy = result.get("legacy") or {}
    core = ((result.get("core") or {}).get("user_results") or {}).get("result") or {}
    handle = ((core.get("core") or {}).get("screen_name")
              or (core.get("legacy") or {}).get("screen_name")
              or (legacy.get("user") or {}).get("screen_name"))
    note = (((result.get("note_tweet") or {}).get("note_tweet_results") or {}).get("result") or {}).get("text")
    text = note or details.get("full_text") or legacy.get("full_text") or ""
    quoted_result = ((result.get("quoted_status_result") or {}).get("result") or {})
    quoted = ((quoted_result.get("note_tweet") or {}).get("note_tweet_results") or {}).get("result", {}).get("text")
    quoted = quoted or (quoted_result.get("details") or {}).get("full_text") or (quoted_result.get("legacy") or {}).get("full_text")
    at = legacy.get("created_at")
    if at:
        try:
            at = parsedate_to_datetime(at).astimezone(timezone.utc).isoformat(timespec="seconds")
        except (TypeError, ValueError):
            at = None
    if not at and details.get("created_at_ms"):
        at = datetime.fromtimestamp(details["created_at_ms"] / 1000, timezone.utc).isoformat(timespec="seconds")
    if not (result.get("rest_id") and at):
        return None
    return {"id": result["rest_id"], "handle": handle, "at": at, "text": text,
            "quoted": quoted or None, "repost_by": handle if result.get("retweeted_status_result") else None,
            "media": bool(result.get("media_entities2") or (legacy.get("entities") or {}).get("media"))}


def _entry_tweet(content: dict) -> dict | None:
    for key in ("itemContent", "content"):
        item = content.get(key)
        if isinstance(item, dict):
            tweet = _result_tweet((item.get("tweet_results") or {}).get("result"))
            if tweet:
                return tweet
    for item in content.get("items") or []:
        tweet = _entry_tweet(item.get("content") or {})
        if tweet:
            return tweet
    return None


def _latest_tweets(embedded: dict | None) -> list[dict]:
    if not embedded:
        return []
    result = ((embedded.get("data") or {}).get("user_result_by_screen_name") or {}).get("result") or {}
    instructions = (((result.get("profile_user_originals_timeline") or {}).get("timeline") or {})
                    .get("instructions") or [])
    out = []
    for instruction in instructions:
        for entry in instruction.get("entries") or []:
            tweet = _entry_tweet(entry.get("content") or {})
            if tweet:
                out.append(tweet)
    return out


def _uid_from_embedded(embedded: dict | None) -> str | None:
    if not embedded:
        return None
    wrapper = (embedded.get("data") or {}).get("user_result_by_screen_name") or {}
    result = wrapper.get("result") or {}
    if result.get("rest_id"):
        return str(result["rest_id"])
    raw = wrapper.get("id") or ""
    try:
        decoded = base64.b64decode(raw).decode()
        if decoded.startswith("User:"):
            return decoded.split(":", 1)[1]
    except (ValueError, UnicodeDecodeError):
        pass
    return None


def _user_id(s: requests.Session, state: dict, handle: str) -> str | None:
    data = _guest_call(s, state, "UserByScreenName", {"screen_name": handle}, USER_BY_SCREEN_NAME_QIDS)
    return (((data.get("data") or {}).get("user") or {}).get("result") or {}).get("rest_id")


def _top_tweets(data: dict) -> list[dict]:
    result = ((data.get("data") or {}).get("user") or {}).get("result") or {}
    instructions = (((result.get("timeline") or {}).get("timeline") or {}).get("instructions") or [])
    out = []
    for instruction in instructions:
        for entry in instruction.get("entries") or []:
            tweet = _entry_tweet(entry.get("content") or {})
            if tweet:
                out.append(tweet)
    return out


def _audit_root() -> Path:
    return root() / AUDIT_DIR


def _audit_handles(path: Path | None = None) -> list[str]:
    """The handles to read: a .txt of handles, or a .csv with a handle column, else data/x/following.csv."""
    import csv
    src = path or (root() / "following.csv")
    if not src.exists():
        return []
    if src.suffix.lower() == ".csv":
        with src.open() as f:
            hs = [r["handle"] for r in csv.DictReader(f)]
    else:
        hs = src.read_text().split()
    return sorted({h.strip().lstrip("@").lower() for h in hs if h.strip()})


def _audit_state() -> dict:
    path = _audit_root() / "state.json"
    return json.loads(path.read_text()) if path.exists() else {"done": {}}


def _save_audit_state(state: dict) -> None:
    path = _audit_root() / "state.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    state["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    path.write_text(json.dumps(state, indent=1) + "\n")


def audit(limit: int = AUDIT_LIMIT, per: int = AUDIT_PER, pause: float = AUDIT_PAUSE,
          days: float = AUDIT_DAYS, handles: Path | None = None) -> dict:
    """Read the 5 newest posts plus up to `per` best highlights of each handle, browserless, and keep them.

    Profiles with posts read within `days` are skipped; a profile with zero posts is always retried. `limit` caps
    the run (0 for no cap). The guest session is paced to X's 50 timeline reads per 15 minutes, and the state is
    saved after every profile, so a run can stop and resume.
    """
    now = datetime.now(timezone.utc)
    all_handles = _audit_handles(handles)
    state = _audit_state()
    state["done"] = {h: v for h, v in state.get("done", {}).items() if v.get("posts", 0) > 0}
    todo = []
    for h in all_handles:
        done = state["done"].get(h)
        if done and days > 0:
            try:
                last = datetime.fromisoformat(done["fetched_at"])
            except (KeyError, ValueError):
                last = None
            if last and (now - last).total_seconds() < days * 86400:
                continue
        todo.append(h)
    if limit and limit > 0:
        todo = todo[:limit]
    result = {"at": now.isoformat(timespec="seconds"), "ok": False, "requested": len(todo), "done": 0,
              "posts": 0, "latest": 0, "top": 0, "failed": [],
              "remaining": len(all_handles) - len(state["done"])}
    if not todo:
        result.update({"ok": True, "why": "nothing to read"})
        return result

    s = _guest_session()
    guest = {}
    for i, h in enumerate(todo, 1):
        try:
            embedded = _embedded_timeline(_profile_html(s, h) or "")
            latest = _latest_tweets(embedded)
            uid = _uid_from_embedded(embedded) or _user_id(s, guest, h)
            if not uid:
                raise RuntimeError("profile not found")
            top = _top_tweets(_guest_call(s, guest, "UserTweets", {
                "userId": uid, "count": 20, "includePromotedContent": False,
                "withQuickPromoteEligibilityTweetFields": True, "withVoice": True, "withV2Timeline": True,
            }, USER_TWEETS_QIDS))
            if per and per > 0:
                top = top[:per]
            posts = {}
            for post in top:
                posts[post["id"]] = {**post, "source": "top"}
            for post in latest:
                posts[post["id"]] = {**post, "source": "latest"}
            rows = sorted(posts.values(), key=lambda r: r["at"], reverse=True)
            if not rows:
                raise RuntimeError("no posts returned")
            path = _audit_root() / "posts" / f"{h}.jsonl"
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("w") as f:
                for r in rows:
                    f.write(json.dumps({**r, "profile": h, "fetched_at": result["at"]},
                                       ensure_ascii=False) + "\n")
            state["done"][h] = {"fetched_at": result["at"], "posts": len(rows),
                                "latest": len(latest), "top": len(top), "user_id": uid,
                                "own": sum(1 for r in rows if not r.get("repost_by"))}
            _save_audit_state(state)
            result["done"] += 1
            result["posts"] += len(rows)
            result["latest"] += len(latest)
            result["top"] += len(top)
            result["remaining"] = max(0, len(all_handles) - len(state["done"]))
            print(f"[{i}/{len(todo)}] @{h}: {len(rows)} posts "
                  f"({len(latest)} newest, {len(top)} highlights)", flush=True)
        except Exception as e:  # noqa: BLE001
            result["failed"].append(h)
            result["why"] = f"@{h}: {type(e).__name__}: {e}"
            break
        if i < len(todo):
            time.sleep(random.uniform(max(0.0, pause * 0.85), pause * 1.15))
            if i % 25 == 0:
                long = random.uniform(60, 120)
                print(f"  paused {long:.0f}s after {i} profiles", flush=True)
                time.sleep(long)
    result["ok"] = result["done"] > 0 and not result.get("why")
    with (_audit_root() / "runs.jsonl").open("a") as f:
        f.write(json.dumps(result, ensure_ascii=False) + "\n")
    log.info("x_audit %s", result)
    return result
