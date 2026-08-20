#!/usr/bin/env bash
# fleet/nightly.sh — ONE cron entry per station: preflight -> record the window -> postrun.
# record.sh waits until START_AT, records MINUTES, then graceful-stops; postrun packs/uploads/purges.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/station.env"

export DATE="$(date +%Y%m%d)"   # v2: date-at-root scopes tonight's data; no AFTER filter needed
# per-session folder stamp: date + the scheduled window start (START_AT, colons stripped) ->
# data/{DATE}_{HHMM}/{room}/. One value shared by every room (all recorders read $DOUYIN_SESSION).
export DOUYIN_SESSION="${DATE}_$(echo "${START_AT:-$(date +%H:%M)}" | tr -d ':')"

# one log dir per run: runs/{session}/. record.sh writes console.log, postrun.sh writes postrun.log;
# nightly's own preflight lines append to console.log (no wrapper-wide tee, so nothing is doubled).
RUNDIR="$APP_DIR/runs/$DOUYIN_SESSION"; mkdir -p "$RUNDIR"
nlog(){ echo "$@" | tee -a "$RUNDIR/console.log"; }
nlog "===== nightly $DATE  station=$STATION  window=$START_AT +${MINUTES}m  -> runs/$DOUYIN_SESSION/ ====="

# preflight: disk floor (refuse to start rather than fill the disk mid-window)
FREE=$(df -Pk "$DATA_DIR" | awk 'NR==2{print int($4/1024/1024)}')
if [ "$FREE" -lt "$DISK_FLOOR_GB" ]; then
  nlog "[nightly] ABORT: disk ${FREE}GB < floor ${DISK_FLOOR_GB}GB — skipping tonight"
  exit 1
fi
nlog "[nightly] preflight OK: disk_free=${FREE}GB"

# 1) record the window (blocks until graceful stop)
"$APP_DIR/scripts/record.sh" --at "$START_AT" --minutes "$MINUTES" --log-level INFO

# 2) post-run pipeline (pack -> upload+verify -> purge -> status)
"$HERE/postrun.sh"
rc=$?
nlog "[nightly] postrun exit=$rc  (0 = idle & verified)"
exit $rc
