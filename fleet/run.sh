#!/usr/bin/env bash
# fleet/run.sh — composable, flexible test driver (v2).
#
# For operators on a station with no Claude: record a subset briefly, check real-time keep-up,
# optionally transcribe, and optionally upload to a (test) ModelScope dataset. Safe by default:
# it KEEPS recordings (use --purge to delete after a verified upload). The full nightly pipeline
# is fleet/nightly.sh; this driver reuses scripts/record.sh + fleet/pack.py + fleet/ms_upload.py.
#
# Usage:
#   fleet/run.sh --rooms 5                        # record 5 rooms(3m) + check       (no upload)
#   fleet/run.sh --rooms 5 --upload               # + pack & upload to douyin-test
#   fleet/run.sh --room <id> --minutes 5 --upload # single room, 5 min, upload
#   fleet/run.sh --stages 2,3,5 --rooms 3         # explicit stages (5=upload)
#   fleet/run.sh --upload --purge                 # delete local recordings after a verified upload
#   fleet/run.sh --repo-id SISU_DynCogLab/douyin  # upload to a different dataset
#   fleet/run.sh --check-only                     # pre-flight checks only
#
# stages: 2=record  3=check  4=transcribe(stub)  5=upload      (record always included)
# Exit:   0 ok | 1 problem (preflight/bandwidth/upload) | 2 inconclusive (no rooms live)
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(dirname "$HERE")"
cd "$REPO"

STAGES="record,check"; MINUTES=3; ROOM=""; ROOMS_N=""; DO_UPLOAD=0; PURGE=0; YES=0; CHECK_ONLY=0
REPO_ID="${REPO_ID:-SISU_DynCogLab/douyin-test}"; STATION="${STATION:-runtest}"
TOKEN_FROM="${MS_TOKEN_FROM:-}"; [ -z "$TOKEN_FROM" ] && [ -d "$REPO/../douyin/.git" ] && TOKEN_FROM="$REPO/../douyin"
while [ $# -gt 0 ]; do case "$1" in
  --test)       shift;;                 # accepted for back-compat (no longer required)
  --stages)     STAGES="$2"; shift 2;;
  --minutes)    MINUTES="$2"; shift 2;;
  --rooms)      ROOMS_N="$2"; shift 2;;
  --room)       ROOM="$2"; shift 2;;
  --upload)     DO_UPLOAD=1; shift;;
  --repo-id)    REPO_ID="$2"; shift 2;;
  --station)    STATION="$2"; shift 2;;
  --token-from) TOKEN_FROM="$2"; shift 2;;
  --purge)      PURGE=1; shift;;
  --keep)       PURGE=0; shift;;        # keep is the default; accepted for clarity
  --yes)        YES=1; shift;;
  --check-only) CHECK_ONLY=1; shift;;
  -h|--help)    sed -n '2,20p' "$0"; exit 0;;
  *) echo "unknown arg: $1  (see: fleet/run.sh --help)"; exit 2;;
esac; done

declare -A WANT=()
_name() { case "$1" in 2) echo record;; 3) echo check;; 4) echo transcribe;; 5) echo upload;;
                       record|check|transcribe|upload) echo "$1";; *) echo "BAD:$1";; esac; }
for s in ${STAGES//,/ }; do
  n="$(_name "$s")"; [ "${n#BAD:}" != "$n" ] && { echo "unknown stage: ${n#BAD:}"; exit 2; }
  WANT[$n]=1
done
WANT[record]=1
[ "$DO_UPLOAD" -eq 1 ] && WANT[upload]=1

say() { echo "[run $(date '+%T')] $*"; }
find_ms_py() { for p in .venv-asr/bin/python ../DouyinBarrage/.venv-asr/bin/python python3; do
  "$p" -c "import modelscope" >/dev/null 2>&1 && { echo "$p"; return; }; done; }

# ---------------- pre-flight ----------------
pass=0; fail=0
ok()   { echo "  [PASS] $*"; pass=$((pass+1)); }
bad()  { echo "  [FAIL] $*"; fail=$((fail+1)); }
warn() { echo "  [WARN] $*"; }
echo "================= run ($(hostname), $(date '+%F %T')) ================="
echo "stages: ${!WANT[*]}   minutes: $MINUTES   rooms: ${ROOM:-${ROOMS_N:-all}}   upload: $([ -n "${WANT[upload]:-}" ] && echo "$REPO_ID" || echo NO)   purge: $([ "$PURGE" -eq 1 ] && echo YES || echo NO)"
echo "--- pre-flight ---"
PY=""; [ -x .venv/bin/python ] && PY=".venv/bin/python"; [ -z "$PY" ] && command -v python3 >/dev/null && PY="python3"
if [ -z "$PY" ]; then bad "python not found"; else
  MISS="$("$PY" - <<'P'
import importlib
miss=[]
for m in ["requests","websocket","yaml","google.protobuf"]:
    try: importlib.import_module(m)
    except Exception: miss.append(m)
print(",".join(miss))
P
)"; [ -z "$MISS" ] && ok "python + deps ($("$PY" --version 2>&1))" || bad "missing deps: $MISS"
fi
command -v node >/dev/null || for d in "$HOME/.nvm/versions/node"/*/bin "$HOME/.local/bin"; do
  [ -x "$d/node" ] && { export PATH="$d:$PATH"; break; }; done
command -v node >/dev/null && ok "node $(node --version)" || bad "node NOT found (signing → DEVICE_BLOCKED)"
command -v ffmpeg >/dev/null || for d in "$HOME/.local/bin" "$HOME/fsl/bin"; do
  [ -x "$d/ffmpeg" ] && { export PATH="$d:$PATH"; break; }; done
command -v ffmpeg >/dev/null && ok "ffmpeg present" || bad "ffmpeg NOT found"
[ -f config.yaml ] && ok "config.yaml ($(grep -oE '标清|原画|蓝光|超清|高清' config.yaml | head -1))" || bad "config.yaml missing"
if [ -n "$ROOM" ]; then ok "single room: $ROOM"
elif [ -s rooms.txt ]; then ok "rooms.txt: $(grep -vc '^#' rooms.txt) rooms$([ -n "$ROOMS_N" ] && echo " (using first $ROOMS_N)")"
else bad "rooms.txt missing/empty"; fi
NROOMS=$( [ -n "$ROOM" ] && echo 1 || echo "${ROOMS_N:-$(grep -vc '^#' rooms.txt 2>/dev/null || echo 0)}" )
if [ -f cookie.txt ] && grep -q 'sessionid=' cookie.txt; then ok "cookie.txt (sessionid — authenticated)"
elif [ "${NROOMS:-0}" -gt 3 ]; then warn "no logged-in cookie + ${NROOMS} rooms — Douyin throttles guest ttwid; many may fail"
else ok "no logged-in cookie (guest — OK for a small test)"; fi
if [ -n "${WANT[upload]:-}" ]; then
  MS_PY="$(find_ms_py)"; [ -n "$MS_PY" ] && ok "modelscope SDK ($MS_PY)" || bad "no python with modelscope (pip install modelscope)"
  { [ -n "$TOKEN_FROM" ] || [ -n "${MODELSCOPE_API_TOKEN:-}" ]; } && ok "ModelScope token available" || bad "no token (--token-from / MODELSCOPE_API_TOKEN)"
fi
[ "$fail" -gt 0 ] && { echo "--- PRE-FLIGHT FAILED ($fail) ---"; exit 1; }
echo "  pre-flight OK ($pass checks)"
[ "$CHECK_ONLY" -eq 1 ] && { echo "--- --check-only: done ---"; exit 0; }

# ---------------- optional: limit to first N rooms (restore rooms.txt on exit) ----------------
if [ -n "$ROOMS_N" ] && [ -z "$ROOM" ]; then
  cp rooms.txt "/tmp/rooms.bak.$$"
  trap 'mv -f "/tmp/rooms.bak.$$" rooms.txt 2>/dev/null || true' EXIT
  grep -vE '^\s*#|^\s*$' "/tmp/rooms.bak.$$" | head -n "$ROOMS_N" > rooms.txt
  say "limited to first $ROOMS_N room(s)"
fi

# ---------------- stage: record ----------------
DATE=$(date +%Y%m%d)
say "STAGE record — ${MINUTES}m"
STAMP=$(date +%s)
RARGS=(--minutes "$MINUTES" --log-level INFO); [ -n "$ROOM" ] && RARGS+=(--room "$ROOM")
bash scripts/record.sh "${RARGS[@]}" || true
# v2 layout: sessions are data/{DATE}/{anchor} (depth 2), created during this run
mapfile -t SESS < <(find "data/$DATE" -mindepth 1 -maxdepth 1 -type d -newermt "@$STAMP" 2>/dev/null | sort)
say "recorded ${#SESS[@]} session(s)"
[ "${#SESS[@]}" -eq 0 ] && { echo "INCONCLUSIVE — no rooms recorded (none live / all throttled?)."; exit 2; }

RC=0
# ---------------- stage: check ----------------
if [ -n "${WANT[check]:-}" ]; then
  say "STAGE check — bandwidth (pipe) + completeness (payload coverage)"
  "$PY" - "${SESS[@]}" <<'PY' || RC=$?
import sys, align
r = align.check(sys.argv[1:]) or {}          # tags sessions, then both verdicts
sys.exit(1 if (r.get("bandwidth") or {}).get("bandwidth_limited") else 0)
PY
  [ "$RC" -ne 0 ] && say "⚠ bandwidth-limited"
fi

# ---------------- stage: transcribe (SenseVoice-Small, CPU) ----------------
if [ -n "${WANT[transcribe]:-}" ]; then
  say "STAGE transcribe — SenseVoice-Small (CPU) -> transcript.csv + <seg>.16k.flac"
  ASR_PY="$(find_ms_py)"; [ -z "$ASR_PY" ] && ASR_PY="$PY"
  "$ASR_PY" fleet/transcribe.py "data/$DATE" --jobs "${ASR_JOBS:-1}" || say "⚠ transcribe had issues"
fi

# ---------------- stage: upload (pack + SDK upload to a test dataset) ----------------
if [ -n "${WANT[upload]:-}" ]; then
  say "STAGE upload — pack + SDK upload -> $REPO_ID (station=$STATION)"
  MS_PY="${MS_PY:-$(find_ms_py)}"; STAGING="/tmp/run_staging.$$"; rm -rf "$STAGING"
  if python3 fleet/pack.py --station "$STATION" --date "$DATE" --data-dir data --out-dir "$STAGING" --shard-gb 7; then
    if "$MS_PY" fleet/ms_upload.py --repo-id "$REPO_ID" --staging "$STAGING" --station "$STATION" \
         --date "$DATE" ${TOKEN_FROM:+--token-from "$TOKEN_FROM"}; then
      say "✓ upload VERIFIED -> $REPO_ID"
    else say "✗ upload FAILED"; RC=1; PURGE=0; fi
  else say "✗ pack FAILED"; RC=1; PURGE=0; fi
  rm -rf "$STAGING"
fi

# ---------------- retention (KEEP by default; --purge deletes after a verified upload) ----------------
if [ "$PURGE" -eq 1 ]; then
  rm -rf "data/$DATE"; say "purged data/$DATE (--purge)"
else
  say "kept ${#SESS[@]} session(s) under data/$DATE"
fi

echo "================= result ================="
[ "$RC" -eq 0 ] && echo "OK" || echo "PROBLEM (see above)"
exit "$RC"
