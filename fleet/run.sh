#!/usr/bin/env bash
# fleet/run.sh — composable pipeline driver.
#
# PHASE 1: TEST SUBSETS ONLY. Runs a short, safe session for operators on a station with no
# Claude — it records briefly, checks real-time keep-up (bandwidth verdict), and optionally
# transcribes. It NEVER uploads to ModelScope and NEVER purges. Reuses scripts/record.sh +
# align.py; the full nightly pipeline stays in nightly.sh / postrun.sh (untouched).
#
# Usage:
#   fleet/run.sh --test                         # record(3m) + check          (= steps 2,3)
#   fleet/run.sh --test --stages 2,3,4          # + transcribe                (= steps 2,3,4)
#   fleet/run.sh --test --stages record,check   # names work too
#   fleet/run.sh --test --minutes 5 --room 56697889278   # single room, 5 min
#   fleet/run.sh --test --keep                  # keep the test recordings (default: delete)
#   fleet/run.sh --test --yes                   # no prompts (run straight through)
#   fleet/run.sh --test --check-only            # pre-flight checks only, do NOT record
#
# stages:  2=record  3=check(real-time keep-up)  4=transcribe   (record is always included)
# Exit:    0 = ok | 1 = problem (preflight fail / bandwidth-limited) | 2 = inconclusive (no rooms live)
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(dirname "$HERE")"
cd "$REPO"

MODE=""; STAGES="record,check"; MINUTES=3; ROOM=""; KEEP=0; YES=0; CHECK_ONLY=0
while [ $# -gt 0 ]; do case "$1" in
  --test)       MODE=test; shift;;
  --stages)     STAGES="$2"; shift 2;;
  --minutes)    MINUTES="$2"; shift 2;;
  --room)       ROOM="$2"; shift 2;;
  --keep)       KEEP=1; shift;;
  --yes)        YES=1; shift;;
  --check-only) CHECK_ONLY=1; shift;;
  -h|--help)    sed -n '2,20p' "$0"; exit 0;;
  *) echo "unknown arg: $1  (see: fleet/run.sh --help)"; exit 2;;
esac; done
[ "$MODE" = test ] || { echo "Phase 1 supports --test only (full pipeline: fleet/nightly.sh)."; exit 2; }

# --- resolve requested stages (numeric aliases -> names; record always on) ---
declare -A WANT=()
_name() { case "$1" in 2) echo record;; 3) echo check;; 4) echo transcribe;;
                       record|check|transcribe) echo "$1";; *) echo "BAD:$1";; esac; }
for s in ${STAGES//,/ }; do
  n="$(_name "$s")"; [ "${n#BAD:}" != "$n" ] && { echo "unknown stage: ${n#BAD:}"; exit 2; }
  WANT[$n]=1
done
WANT[record]=1   # test always records fresh (check/transcribe consume its output)

ask() {  # ask "prompt"  -> 0=yes. Auto-yes with --yes or when stdin is not a TTY (cron/pipe).
  [ "$YES" -eq 1 ] && return 0
  [ -t 0 ] || return 0
  local a; read -r -p "  >> $1 [y/N] " a; [[ "$a" =~ ^[Yy] ]]
}
say() { echo "[run $(date '+%T')] $*"; }

# ---------------- pre-flight (fold in the recording smoke checks) ----------------
pass=0; fail=0
ok()  { echo "  [PASS] $*"; pass=$((pass+1)); }
bad() { echo "  [FAIL] $*"; fail=$((fail+1)); }
echo "================= test run ($(hostname), $(date '+%F %T')) ================="
echo "stages: ${!WANT[*]}   minutes: $MINUTES   room: ${ROOM:-<all>}   upload: NO  purge: NO"
echo "--- pre-flight ---"
PY=""; [ -x .venv/bin/python ] && PY=".venv/bin/python"
[ -z "$PY" ] && command -v python3 >/dev/null && PY="python3"
if [ -z "$PY" ]; then bad "python not found (create .venv; pip install -r requirements.txt)"; else
  MISS="$("$PY" - <<'P'
import importlib.util as u
print(",".join(m for m in ["requests","websocket","yaml","google.protobuf"] if u.find_spec(m) is None))
P
)"; [ -z "$MISS" ] && ok "python + deps ($("$PY" --version 2>&1))" || bad "missing deps: $MISS"
fi
command -v node >/dev/null || for d in "$HOME/.nvm/versions/node"/*/bin "$HOME/.local/bin"; do
  [ -x "$d/node" ] && { export PATH="$d:$PATH"; break; }; done
if command -v node >/dev/null; then
  NV="$(node --version | tr -d v | cut -d. -f1)"
  { [ "${NV:-0}" -ge 16 ] 2>/dev/null && ok "node $(node --version) (signing OK)"; } || bad "node too old ($(node --version)); need >=16"
else bad "node NOT found — request signing fails (DEVICE_BLOCKED). Install Node >=20."; fi
command -v ffmpeg >/dev/null || for d in "$HOME/.local/bin" "$HOME/fsl/bin"; do
  [ -x "$d/ffmpeg" ] && { export PATH="$d:$PATH"; break; }; done
command -v ffmpeg >/dev/null && ok "ffmpeg present" || bad "ffmpeg NOT found"
[ -f config.yaml ] && ok "config.yaml ($(grep -oE '标清|原画|蓝光|超清|高清' config.yaml | head -1) quality)" || bad "config.yaml missing"
if [ -n "$ROOM" ]; then ok "single room: $ROOM"
elif [ -s rooms.txt ]; then ok "rooms.txt: $(grep -vc '^#' rooms.txt) rooms"
else bad "rooms.txt missing/empty"; fi
if [ "$fail" -gt 0 ]; then echo "--- PRE-FLIGHT FAILED ($fail) — fix [FAIL] items ---"; exit 1; fi
echo "  pre-flight OK ($pass checks)"
[ "$CHECK_ONLY" -eq 1 ] && { echo "--- --check-only: done ---"; exit 0; }

# ---------------- stage: record (step 2) ----------------
say "STAGE record — ${MINUTES}m (exercises signing + WebSocket + ffmpeg; NO upload)"
STAMP=$(date +%s)
RARGS=(--minutes "$MINUTES" --log-level INFO); [ -n "$ROOM" ] && RARGS+=(--room "$ROOM")
bash scripts/record.sh "${RARGS[@]}" || true
mapfile -t SESS < <(find data -mindepth 2 -maxdepth 2 -type d -newermt "@$STAMP" -name '2*' 2>/dev/null | sort)
say "recorded ${#SESS[@]} session(s)"
if [ "${#SESS[@]}" -eq 0 ]; then
  echo "INCONCLUSIVE — no rooms recorded (none live now?). Re-run during broadcast hours."; exit 2
fi

RC=0
# ---------------- stage: check (step 3 — real-time keep-up / bandwidth verdict) ----------------
if [ -n "${WANT[check]:-}" ]; then
  say "STAGE check — real-time keep-up / bandwidth verdict"
  "$PY" - "${SESS[@]}" <<'PY' || RC=$?
import sys, align
stats = align.report(sys.argv[1:])            # prints per-room detail + fleet verdict
sys.exit(1 if stats.get("bandwidth_limited") else 0)
PY
  [ "$RC" -ne 0 ] && say "⚠ bandwidth-limited — see flagged rooms above"
fi

# ---------------- stage: transcribe (step 4 — PHASE 2 STUB) ----------------
if [ -n "${WANT[transcribe]:-}" ]; then
  if ask "proceed to transcribe ${#SESS[@]} session(s)?"; then
    say "STAGE transcribe — (Phase 2: not yet implemented)"
    echo "  Will: convert .ts->.mp4 -> ASR per room (.venv-asr) -> map video-time to wall-clock via"
    echo "  the timing sidecar (align.video_to_wall) -> write transcript.csv on the chat timeline."
  else
    say "skipped transcribe"
  fi
fi

# ---------------- cleanup (test recordings are disposable) ----------------
if [ "$KEEP" -eq 0 ] && ask "delete the ${#SESS[@]} test recording(s)?"; then
  for d in "${SESS[@]}"; do rm -rf "$d"; done
  say "removed ${#SESS[@]} test session(s) (use --keep to retain)"
else
  say "kept ${#SESS[@]} session(s) under data/"
fi

echo "================= result ================="
[ "$RC" -eq 0 ] && echo "TEST OK — recording works, real-time keep-up good." || echo "TEST: bandwidth-limited (see check)."
exit "$RC"
