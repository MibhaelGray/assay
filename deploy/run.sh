#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

# One job of a kind at a time: a later job waits for the one running. flock on Linux, lockf on macOS. Paper jobs
# use the paper gateway and have their own lock, so a long data job can't make the 3:45 PM exits miss the
# market-on-close cutoff.
case "${1:-}" in keep-awake) lock=/tmp/fr-awake.lock ;; watch) lock=/tmp/fr-watch.lock ;; publish) lock=/tmp/fr-publish.lock ;; paper-*) lock=/tmp/fr-paper.lock ;; pass-*) lock=/tmp/fr-pass.lock ;; side-*) lock=/tmp/fr-side.lock ;; *) lock=/tmp/fr-ib.lock ;; esac
if [ -z "${FR_LOCKED:-}" ]; then
  export FR_LOCKED=1
  if command -v flock >/dev/null; then exec flock "$lock" "$0" "$@"; fi
  exec lockf -k "$lock" "$0" "$@"
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
  bars)         exec uv run fr bars ;;
  paper-close|paper-sync)
    # Nothing to do until the paper account is set up in .env (docs/operations.md).
    [ -n "${FR_PAPER_ACCOUNT:-}" ] || { echo "FR_PAPER_ACCOUNT not set; skipping $1" >&2; exit 0; }
    if [ "$1" = paper-close ]; then
      # Started at 3:45 PM and 12:45 PM ET; only the run in the 30 minutes before today's close goes on, so a
      # half day (1:00 PM ET close) exits at 12:45 PM ET and a full day at 3:45 PM ET.
      uv run fr trading-day --closing || exit 0
    else
      uv run fr trading-day || exit 0
    fi
    exec uv run fr paper "${1#paper-}" ;;
  paper-enter)
    # The three-pass book's order job (paper/entries.py), every minute. Not gated on the trading day: Sunday night
    # trades Monday's night pass. A run with nothing to do prints nothing.
    [ -n "${FR_PAPER_ACCOUNT:-}" ] || exit 0
    exec uv run fr paper enter ;;
  publish)
    # The public site (publish.py): pull today's prices and the paper account, push the page if it changed.
    exec uv run fr publish --pull ;;
  watch)
    # The watchdog (watch.py): phone alerts when a pass, a job or the paper gateway breaks. Every five minutes.
    exec uv run fr watch ;;
  side-facts)
    # Facts recorded beside the night's verdicts, which no research pass reads (paper/side.py). Yahoo only.
    exec uv run fr paper side ;;
  pass-night)
    # Sunday to Thursday evenings, for the next session. launchd starts it Monday to Friday and on Sunday
    # (install.py); Friday's run stops here.
    [ "$(TZ=America/New_York date +%u)" = 5 ] && exit 0
    exec uv run fr pass night ;;
  pass-morning)
    uv run fr trading-day || exit 0
    exec uv run fr pass morning ;;
  pass-midday)
    # Not on a half day: the market closes at 1:00 PM ET, when this pass would start.
    uv run fr trading-day --full || exit 0
    exec uv run fr pass midday ;;
  keep-awake)
    # Hold off idle sleep until 9:40 AM New York time, so the order job and the night pass run all night.
    # A closed lid still sleeps the Mac, and so does a flat battery.
    secs=$(python3 -c 'from datetime import datetime, timedelta; from zoneinfo import ZoneInfo
n = datetime.now(ZoneInfo("America/New_York")); e = n.replace(hour=9, minute=40, second=0, microsecond=0)
e += timedelta(days=1) if e <= n else timedelta(); print(int((e - n).total_seconds()))')
    exec caffeinate -i -t "$secs" ;;
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
