#!/usr/bin/env bash
# fleet/postrun.sh (v2) — after recording:
#   assert-idle -> convert ts->mp4 -> align -> bandwidth verdict -> pack(staging) -> upload(SDK) -> purge -> status
#
# No git clone. Uploads via fleet/ms_upload.py (upload_folder + retry-429 + verify).
# Purge is GATED on a verified upload; text bundle + manifest are archived locally forever.
# Layout: data/{DATE}/{anchor}/  ->  staging/{video,text,manifest}/{DATE}/{STATION}/...
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/station.env"
# which night to process: --date YYYYMMDD  (for a next-morning retry) > $DATE env > today
DATE_ARG=""
while [ $# -gt 0 ]; do case "$1" in
  --date) DATE_ARG="$2"; shift 2;;
  *) echo "postrun.sh: unknown arg '$1' (use --date YYYYMMDD)" >&2; exit 2;;
esac; done
DATE="${DATE_ARG:-${DATE:-$(date +%Y%m%d)}}"
log() { echo "[postrun $(date '+%F %T')] $*"; }

STAGING="$APP_DIR/upload_staging/$DATE"
ARCHIVE="$APP_DIR/archive"
# the single project venv (recorder + funasr + modelscope); one python for every stage.
PY="$APP_DIR/.venv/bin/python"; [ -x "$PY" ] || PY=python3

# ---------- A. assert the recorder is gone ----------
# Only THIS app's recorder: match main.py processes whose cwd is $APP_DIR, so we never signal
# another checkout / unrelated `python -u main.py` on a shared machine.
recorder_pids() { for p in $(pgrep -f "python -u main.py" 2>/dev/null); do
  [ "$(readlink -f "/proc/$p/cwd" 2>/dev/null)" = "$APP_DIR" ] && printf '%s ' "$p"; done; }
if [ -n "$(recorder_pids)" ]; then
  log "WARN recorder still running (this app) — SIGINT"; kill -INT $(recorder_pids) 2>/dev/null || true
  for _ in $(seq 1 30); do [ -z "$(recorder_pids)" ] && break; sleep 2; done
fi
[ -n "$(recorder_pids)" ] && IDLE=false || IDLE=true
log "idle=$IDLE"

# ---------- A1. convert ts->mp4 (parallel) ----------
log "convert ts->mp4 ..."
"$PY" - "$DATA_DIR" "$DATE" <<'PYEOF' || log "WARN convert had issues (non-fatal)"
import sys, glob, os, subprocess, concurrent.futures as cf
data, date = sys.argv[1], sys.argv[2]
ts = sorted(glob.glob(f"{data}/{date}/*/*.ts"))
def conv(t):
    mp4 = t[:-3] + ".mp4"
    if os.path.exists(mp4) and os.path.getsize(mp4) > 0:
        os.path.exists(t) and os.remove(t); return "skip"
    if not os.path.exists(t) or os.path.getsize(t) == 0: return "empty"
    r = subprocess.run(["ffmpeg","-y","-v","error","-i",t,"-c","copy","-movflags","+faststart","-f","mp4",mp4], capture_output=True, timeout=600)
    if r.returncode != 0 or not os.path.exists(mp4) or os.path.getsize(mp4) == 0:
        if os.path.exists(mp4) and os.path.getsize(mp4) == 0: os.remove(mp4)
        return "fail"
    p = subprocess.run(["ffprobe","-v","error","-show_entries","format=duration","-of","csv=p=0",mp4], capture_output=True, timeout=30)
    if p.returncode != 0 or not p.stdout.strip(): os.remove(mp4); return "badprobe"
    os.remove(t); return "ok"
if not ts: print("[convert] no .ts (already mp4?)"); sys.exit(0)
with cf.ThreadPoolExecutor(max_workers=min(16, os.cpu_count() or 4)) as ex:
    ok = sum(1 for r in ex.map(conv, ts) if r in ("ok","skip"))
print(f"[convert] {ok}/{len(ts)} segments -> mp4")
PYEOF

# ---------- A2. align + thorough check (bandwidth pipe + payload completeness) ----------
# align.check tags every session (chat/like/... -> *_aligned.csv, needed by the text bundle),
# then prints both verdicts and returns {bandwidth, completeness} for the manifest.
log "align + thorough check ..."
CHECK_JSON="$(mktemp)"
( cd "$APP_DIR" && "$PY" - "$DATA_DIR" "$DATE" "$CHECK_JSON" <<'PYEOF'
import sys, glob, os, json
sys.path.insert(0, os.getcwd()); import align
data, date, out = sys.argv[1], sys.argv[2], sys.argv[3]
sessions = sorted(s for s in glob.glob(f"{data}/{date}/*") if os.path.isdir(s))
stats = align.check(sessions) or {}
json.dump(stats, open(out, "w", encoding="utf-8"), ensure_ascii=False)
PYEOF
) || log "WARN align/check had issues (non-fatal)"

# ---------- A3. transcribe (SenseVoice-Small, CPU) -> transcript.csv + <seg>.16k.flac ----------
# Uses $PY (the project .venv with funasr+torch). Non-fatal: a missing ASR env just skips transcripts.
log "transcribe (SenseVoice-Small, CPU, jobs=${ASR_JOBS:-1}) ..."
ASR_JSON="$(mktemp)"
( cd "$APP_DIR" && "$PY" - "$DATA_DIR" "$DATE" "$ASR_JSON" "${ASR_JOBS:-1}" <<'PYEOF'
import sys, os, json
sys.path.insert(0, os.getcwd()); sys.path.insert(0, os.path.join(os.getcwd(), "fleet"))
import align, transcribe
data, date, out, jobs = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4])
sessions = align.discover_sessions(f"{data}/{date}")
stats = transcribe.transcribe_all(sessions, jobs=jobs) if sessions else {}
stats.pop("rooms", None)                      # keep the manifest block compact
json.dump(stats, open(out, "w", encoding="utf-8"), ensure_ascii=False)
print(f"[transcribe] {stats.get('transcribed',0)} rooms, {stats.get('sentences',0)} sentences, "
      f"{stats.get('audio_hours',0)}h @ {stats.get('mean_rtf')}x")
PYEOF
) || log "WARN transcribe had issues (non-fatal)"

# ---------- B. pack tonight's sessions into a plain staging tree ----------
log "pack -> $STAGING ..."
rm -rf "$STAGING"
if ! "$PY" "$HERE/pack.py" --station "$STATION" --date "$DATE" \
      --data-dir "$DATA_DIR" --out-dir "$STAGING" --shard-gb "$SHARD_GB"; then
  log "pack FAILED — aborting (nothing purged)"; rm -f "$CHECK_JSON" "$ASR_JSON"; exit 1
fi

# ---------- B1. fold idle + bandwidth into the staging manifest (uploaded with the data) ----------
MANIFEST="$STAGING/manifest/$DATE/$STATION.json"
"$PY" - "$MANIFEST" "$IDLE" "$CHECK_JSON" "$ASR_JSON" <<'PYEOF' || true
import json, sys, os
p, idle, cj, aj = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
m = json.load(open(p, encoding="utf-8")); m["idle"] = (idle == "true")
try:
    if cj and os.path.exists(cj):
        chk = json.load(open(cj, encoding="utf-8"))
        m["bandwidth"] = chk.get("bandwidth")
        m["completeness"] = chk.get("completeness")
except Exception: pass
try:
    if aj and os.path.exists(aj): m["transcription"] = json.load(open(aj, encoding="utf-8"))
except Exception: pass
json.dump(m, open(p, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
PYEOF
rm -f "$CHECK_JSON" "$ASR_JSON"

# ---------- B2. archive text bundle + manifest locally (kept forever) ----------
mkdir -p "$ARCHIVE/text/$DATE" "$ARCHIVE/manifest/$DATE"
cp -f "$STAGING/text/$DATE/$STATION.tar.gz" "$ARCHIVE/text/$DATE/" 2>/dev/null && log "archived text bundle" || log "WARN no text bundle to archive"
cp -f "$MANIFEST" "$ARCHIVE/manifest/$DATE/$STATION.json" 2>/dev/null || true

# ---------- B3. upload stagger — offset stations so they don't all commit at once ----------
# Only the network push is deferred (pack/align/transcribe/archive already ran; purge stays
# gated on a verified upload). UPLOAD_DELAY seconds is set per station in station.env.
if [ "${UPLOAD_DELAY:-0}" -gt 0 ] 2>/dev/null; then
  log "upload stagger: sleeping ${UPLOAD_DELAY}s before push (UPLOAD_DELAY)"; sleep "$UPLOAD_DELAY"
fi

# ---------- C. upload via SDK (retry-429 + verify) ----------
log "upload -> $REPO_ID ..."
if "$PY" "$HERE/ms_upload.py" --repo-id "$REPO_ID" --staging "$STAGING" \
      --station "$STATION" --date "$DATE"; then    # token from MODELSCOPE_API_TOKEN env
  UPLOAD=verified
else
  UPLOAD=failed
fi
log "upload=$UPLOAD"

# ---------- D. purge — ONLY when verified ----------
if [ "$UPLOAD" = verified ]; then
  before=$(du -sk "$DATA_DIR" 2>/dev/null | awk '{print int($1/1024)}')
  rm -rf "$DATA_DIR/$DATE" "$STAGING"
  after=$(du -sk "$DATA_DIR" 2>/dev/null | awk '{print int($1/1024)}')
  log "verified -> purged data/$DATE + staging (data/ ${before}MB->${after}MB); kept archive/ text+manifest"
else
  log "NOT verified -> retaining ALL local data + staging for retry"
fi

# ---------- E. disk guard + final status line (morning brief) ----------
FREE=$(df -Pk "$DATA_DIR" | awk 'NR==2{print int($4/1024/1024)}')
[ "$FREE" -lt "$DISK_FLOOR_GB" ] && log "WARN disk low: ${FREE}GB < floor ${DISK_FLOOR_GB}GB"
log "DONE station=$STATION date=$DATE idle=$IDLE upload=$UPLOAD disk_free=${FREE}GB"

# non-zero exit if something needs a human (not idle, or upload failed)
[ "$IDLE" = true ] && [ "$UPLOAD" = verified ]
