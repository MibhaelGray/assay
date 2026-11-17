#!/bin/bash
# One time, 2026-11-16: close the positions the first book still held when the paper book was restarted on the
# night of 2026-11-15 (journal/paper/book.json, "legacy"). The first book's own close job does it, pointed at
# the archive, so every exit lands on the trade it belongs to and the archive ends with a closed book.
#   close_first_book.sh close   2:40 PM CT: market-on-close orders for every trade still open
#   close_first_book.sh sync    3:25 PM CT: pull the fills into the archive, then remove both one-time jobs
set -u
cd "$(dirname "$0")/.."
export FR_JOURNAL_DIR="journal/archive/first-book-2026-11-15"
export FR_PAPER_CLIENT_ID=73   # its own gateway connection: the new book's jobs (71) run minutes away
echo "== $(date '+%Y-%m-%d %H:%M:%S %Z') $1"
case "$1" in
  close) uv run fr paper close --flatten --execute ;;
  sync)
    uv run fr paper sync
    uv run fr paper report 2>/dev/null | tail -20
    for job in first-book-close first-book-sync; do
      plist="$HOME/Library/LaunchAgents/com.factor-rotation.$job.plist"
      rm -f "$plist"
    done
    launchctl bootout "gui/$(id -u)/com.factor-rotation.first-book-close" 2>/dev/null
    launchctl bootout "gui/$(id -u)/com.factor-rotation.first-book-sync" 2>/dev/null
    ;;
esac
