#!/usr/bin/env bash
# fleet/nightly.sh — ONE cron entry per station: preflight -> record the window -> postrun.
# record.sh waits until START_AT, records MINUTES, then graceful-stops; postrun packs/uploads/purges.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/station.env"

export DATE="$(date +%Y%m%d)"   # v2: date-at-root scopes tonight's data; no AFTER filter needed

# log to file + console (cron discards console; interactive runs still see it)
mkdir -p "$APP_DIR/logs"
LOG="$APP_DIR/logs/nightly-$DATE.log"
exec > >(tee -a "$LOG") 2>&1
echo "===== nightly $DATE  station=$STATION  window=$START_AT +${MINUTES}m ====="

# preflight: disk floor (refuse to start rather than fill the disk mid-window)
FREE=$(df -Pk "$DATA_DIR" | awk 'NR==2{print int($4/1024/1024)}')
if [ "$FREE" -lt "$DISK_FLOOR_GB" ]; then
  echo "[nightly] ABORT: disk ${FREE}GB < floor ${DISK_FLOOR_GB}GB — skipping tonight" >&2
  exit 1
fi
echo "[nightly] preflight OK: disk_free=${FREE}GB"

# 1) record the window (blocks until graceful stop)
"$APP_DIR/scripts/record.sh" --at "$START_AT" --minutes "$MINUTES" --log-level INFO

# 2) post-run pipeline (pack -> upload+verify -> purge -> status)
"$HERE/postrun.sh"
rc=$?
echo "[nightly] postrun exit=$rc  (0 = idle & verified)"
exit $rc
