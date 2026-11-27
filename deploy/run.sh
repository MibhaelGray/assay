#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

# The schedule's clock (deploy/launchd/clock.py), every 30 seconds: it starts each timed job below when its New York
# time comes, whatever time zone the Mac is set to. It has its own flock, and needs no .env and no caffeinate.
if [ "${1:-}" = clock ]; then exec python3 deploy/launchd/clock.py; fi

# Keep-awake needs no lockf or wrapper caffeinate: launchd kickstarts it with -k (replacing any prior instance),
# it records its own PID (preserved across exec) so clock.py can self-heal it after a reboot or crash, and it
# prevents idle sleep across the active trading week (Sun 19:55 ET through Fri 20:00 ET).
# Note: a closed lid without an external display still sleeps the Mac, and so does a flat battery.
if [ "${1:-}" = keep-awake ]; then
  mkdir -p data
  secs=$(python3 -c 'from datetime import datetime, timedelta; from zoneinfo import ZoneInfo
n = datetime.now(ZoneInfo("America/New_York"))
if n.weekday() < 5 and (9, 0) <= (n.hour, n.minute) < (16, 30):
    e = n.replace(hour=16, minute=45, second=0, microsecond=0)
elif n.weekday() == 4 and (n.hour, n.minute) >= (16, 30):
    e = n.replace(hour=20, minute=0, second=0, microsecond=0)
else:
    e = n.replace(hour=9, minute=30, second=0, microsecond=0)
    if e <= n:
        e += timedelta(days=1)
print(max(60, int((e - n).total_seconds())))')
  printf '%s\n' "$$" > data/.keep-awake.pid
  exec caffeinate -i -t "$secs"
fi

# One job of a kind at a time. Paper jobs share /tmp/fr-paper.lock so gateway-preflight can't restart Docker in the
# middle of an order run, and paper-enter uses a non-blocking lock (exiting 0 if busy) so minute ticks never pile up
# behind gateway-preflight and starve paper-close before the MOC cutoff.
case "${1:-}" in watch) lock=/tmp/fr-watch.lock ;; publish) lock=/tmp/fr-publish.lock ;; paper-*|gateway-preflight) lock=/tmp/fr-paper.lock ;; pass-*) lock=/tmp/fr-pass.lock ;; side-*) lock=/tmp/fr-side.lock ;; *) lock=/tmp/fr-ib.lock ;; esac
if [ -z "${FR_LOCKED:-}" ]; then
  export FR_LOCKED=1
  if [ "${1:-}" = paper-enter ]; then
    if command -v flock >/dev/null; then
      rc=0; flock -n -E 75 "$lock" "$0" "$@" || rc=$?
    else
      rc=0; lockf -k -t 0 "$lock" "$0" "$@" || rc=$?
    fi
    [ "$rc" -eq 75 ] && exit 0
    exit "$rc"
  elif [ "${1:-}" = paper-close ]; then
    if command -v flock >/dev/null; then exec flock -w 120 "$lock" "$0" "$@"; fi
    exec lockf -k -t 120 "$lock" "$0" "$@"
  else
    if command -v flock >/dev/null; then exec flock "$lock" "$0" "$@"; fi
    exec lockf -k "$lock" "$0" "$@"
  fi
fi

# Keep the Mac awake until this job ends.
caffeinate -i -w $$ &

# This script is what launchd starts, so a job running here is a scheduled one. An order job run by hand has no
# FR_SCHEDULED and only looks: it sends no orders unless given --execute.
export FR_SCHEDULED=1

# Settings (FR_*) from .env; the gateway login is in .env.paper, for docker compose only.
if [ -f .env ]; then
  while IFS='=' read -r key value; do
    value="${value%\"}"; value="${value#\"}"
    case "$key" in FR_*|SEC_USER_AGENT) export "$key=$value" ;; esac
  done < .env
fi

# Three tries, half a minute apart.
retry() {
  local n
  for n in 1 2 3; do
    "$@" && return 0
    [ "$n" = 3 ] || sleep "${FR_RETRY_WAIT:-30}"
  done
  return 1
}

case "$1" in
  bars)
    uv run fr trading-day || exit 0
    exec uv run fr bars ;;
  paper-close|paper-sync)
    # Nothing to do until the paper account is set up in .env (docs/operations.md).
    [ -n "${FR_PAPER_ACCOUNT:-}" ] || { echo "FR_PAPER_ACCOUNT not set; skipping $1" >&2; exit 0; }
    if [ "$1" = paper-close ]; then
      # Started at 12:41/12:46 ET and 15:41/15:46 ET; only runs in the 30 minutes before today's close, so a
      # half day (1:00 PM ET close) exits at 12:41/12:46 ET and a full day at 15:41/15:46 ET.
      uv run fr trading-day --closing || exit 0
    else
      hm=$((10#$(TZ=America/New_York date +%H%M)))
      if [ "$hm" -lt 1500 ]; then
        uv run fr trading-day --half || exit 0
      else
        uv run fr trading-day || exit 0
      fi
    fi
    exec uv run fr paper "${1#paper-}" ;;
  paper-enter)
    # The three-pass book's order job (paper/entries.py), every minute. Not gated on the trading day: Sunday night
    # trades Monday's night pass. A run with nothing to do prints nothing.
    [ -n "${FR_PAPER_ACCOUNT:-}" ] || exit 0
    exec uv run fr paper enter ;;
  gateway-preflight)
    # Deterministic gateway check 10 minutes before each scheduled pass and 9 minutes before paper-close.
    # Auto-restarts a hung/disconnected gateway silently; pings Telegram only if the phone app (10197) is holding
    # quotes or if a restart couldn't recover it.
    [ -n "${FR_PAPER_ACCOUNT:-}" ] || exit 0
    hm=$((10#$(TZ=America/New_York date +%H%M)))
    if [ "$hm" -ge 900 ] && [ "$hm" -le 1015 ]; then
      uv run fr trading-day || exit 0
      exec uv run fr paper preflight "Morning pass"
    elif [ "$hm" -ge 1105 ] && [ "$hm" -le 1130 ]; then
      uv run fr trading-day || exit 0
      exec uv run fr paper preflight "Mid-morning check"
    elif [ "$hm" -ge 1225 ] && [ "$hm" -le 1245 ]; then
      uv run fr trading-day --closing || exit 0
      exec uv run fr paper preflight --no-quotes "Paper close"
    elif [ "$hm" -ge 1246 ] && [ "$hm" -le 1315 ]; then
      uv run fr trading-day --full || exit 0
      exec uv run fr paper preflight "Midday pass"
    elif [ "$hm" -ge 1525 ] && [ "$hm" -le 1545 ]; then
      uv run fr trading-day --closing || exit 0
      exec uv run fr paper preflight --no-quotes "Paper close"
    elif [ "$hm" -ge 2000 ] && [ "$hm" -le 2045 ]; then
      uv run fr trading-day --tomorrow || exit 0   # no night pass when tomorrow is closed
      exec uv run fr paper preflight "Night pass"
    fi
    exit 0 ;;
  publish)
    # The public site (publish.py): pull today's prices and the paper account, push the page if it changed.
    exec uv run fr publish --pull ;;
  watch)
    # The watchdog (watch.py): phone alerts when a pass, a job, a close or the schedule clock breaks. Every five minutes.
    exec uv run fr watch ;;
  side-facts)
    # Facts recorded beside the night's verdicts, which no research pass reads (paper/side.py). Yahoo only.
    exec uv run fr paper side ;;
  pass-night)
    # The evening before a session only. The clock starts it Monday to Friday and on Sunday (install.py); a run
    # whose tomorrow is closed stops here: Friday's, and the evening before a holiday (the pass for the session
    # after a holiday runs on the holiday's own evening, on the latest prices).
    uv run fr trading-day --tomorrow || exit 0
    exec uv run fr pass night ;;
  pass-morning)
    uv run fr trading-day || exit 0
    exec uv run fr pass morning ;;
  pass-midday)
    # Not on a half day: the market closes at 1:00 PM ET, when this pass would start.
    uv run fr trading-day --full || exit 0
    exec uv run fr pass midday ;;
  nightly)
    uv run fr trading-day || exit 0
    # The repo may be a working checkout with edits in progress: stash them around the pull, and commit only
    # the files this job writes, whatever else is staged.
    # A Mac that has just woken may have no network yet. A failed pull costs at most someone else's newer journal
    # files, and tonight's brief and screen are still needed, so it must not stop the job.
    if ! retry git pull --rebase --autostash --quiet; then
      echo "git pull failed; building tonight's files from this checkout" >&2
      if [ -d "$(git rev-parse --git-path rebase-merge)" ] || [ -d "$(git rev-parse --git-path rebase-apply)" ]; then git rebase --abort; fi
    fi
    # Names outside the standard universe (basket members, the paper book's legs): their daily bars, in case
    # the 4:30 PM pull ran before a proposal that holds them was recorded.
    uv run fr bars --pending || echo "bars for names outside the universe failed; carrying on" >&2
    # 5-minute bars for every verdict still being scored, so its stop is replayed as the order job checks it.
    uv run fr bars --intraday 5 || echo "5-minute bars failed; tomorrow's pull covers today" >&2
    uv run fr earnings || echo "earnings dates failed; keeping the last pull" >&2
    uv run fr brief
    uv run fr gaps || echo "earnings gap table failed; keeping the last one" >&2
    uv run fr scan || echo "stock scan failed; run it by hand tonight" >&2
    uv run fr screen || echo "screen failed; run it by hand tonight" >&2
    written=()
    for d in journal/briefs journal/scans journal/screens journal/paper journal/research journal/archive journal/reference/earnings.json journal/reference/earnings_gaps.json; do [ -e "$d" ] && written+=("$d"); done
    git add -- "${written[@]}"
    git diff --cached --quiet -- "${written[@]}" ||
      git commit --quiet -m "Journal $(TZ=America/New_York date +%F): brief, screen and paper book" -- "${written[@]}"
    retry git push --quiet
    ;;
  *) echo "unknown job $1" >&2; exit 2 ;;
esac
