#!/usr/bin/env bash
# Stage ONE (date, station) of the old-data backfill up to the packed outbox — NO upload.
#
# Consolidates all of that station-night's old session stamps into ONE <date>_1958 session
# (reopen rooms merged), SenseVoice-transcribes every room, packs a local staging outbox, then
# STOPS. It prints the package to inspect and the exact upload command to run yourself once happy.
#
# Usage:   fleet/backfill_stage.sh <YYYYMMDD> <stNN>
#   e.g.   fleet/backfill_stage.sh 20260818 st01
#
# Note: the full video pull is slow on ModelScope (~2 MB/s) — expect a few hours per station-night.
#       Run it in the background if you like:  nohup fleet/backfill_stage.sh 20260818 st01 &> stage_20260818_st01.log &
set -euo pipefail

DATE="${1:?usage: backfill_stage.sh <YYYYMMDD> <stNN>}"
STATION="${2:?usage: backfill_stage.sh <YYYYMMDD> <stNN>}"

HERE="$(cd "$(dirname "$0")/.." && pwd)"                 # repo root (DouyinBarrage-v2)
PY="$HERE/.venv/bin/python"; [ -x "$PY" ] || PY=python3
OUT="$HERE/backfill_out/${DATE}_${STATION}"
DST_REPO="SISU_DynCogLab/douyin-dataset"
TOKEN_FROM="$HERE/../douyin"                             # local clone whose git remote carries the MS token

echo "=== staging backfill (NO upload):  $DATE / $STATION  ->  $OUT ==="
"$PY" "$HERE/fleet/backfill.py" \
  --date "$DATE" --station "$STATION" \
  --consolidate --no-upload \
  --out-dir "$OUT" --token-from "$TOKEN_FROM"

# the consolidated session is the single dir under outbox/
SESSION="$(ls "$OUT/outbox" | head -1)"
OB="$OUT/outbox/$SESSION"

echo
echo "================ PACKAGE TO INSPECT ================"
echo "reorganized data: $OUT/data/$SESSION/     (per-room CSVs, transcript, video, FLAC)"
echo "staging outbox:   $OB"
find "$OB" -maxdepth 2 -type f -printf '  %-46p %10s bytes\n' 2>/dev/null | sort
echo
echo "--- manifest ledger (state must show uploaded/verified = null) ---"
"$PY" - "$OB/manifest.json" <<'PY'
import json, sys
m = json.load(open(sys.argv[1]))
print("  session :", m.get("session"), "| station:", m.get("station"), "| date:", m.get("date"))
print("  state   :", m.get("state"))
print("  summary :", m.get("summary"))
PY
echo
echo "================ NEXT STEP — you upload after checking ================"
echo "  cd $HERE"
echo "  $PY fleet/ms_upload.py --repo-id $DST_REPO \\"
echo "      --staging '$OB' \\"
echo "      --station $STATION --session $SESSION --token-from '$TOKEN_FROM'"
echo "======================================================================"
echo "(ms_upload verifies after pushing; it writes ONLY <type>/$SESSION/$STATION paths.)"
