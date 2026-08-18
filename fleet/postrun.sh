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
DATE="${DATE:-$(date +%Y%m%d)}"
log() { echo "[postrun $(date '+%F %T')] $*"; }

STAGING="$APP_DIR/upload_staging/$DATE"
ARCHIVE="$APP_DIR/archive"
# python with the modelscope SDK (for ms_upload). Inline convert/align blocks use plain python3.
PY="${MS_PY:-$APP_DIR/.venv-asr/bin/python}"; [ -x "$PY" ] || PY=python3

# ---------- A. assert the recorder is gone ----------
if pgrep -f "python -u main.py" >/dev/null 2>&1; then
  log "WARN recorder still running — SIGINT"; pkill -INT -f "python -u main.py" || true
  for _ in $(seq 1 30); do pgrep -f "python -u main.py" >/dev/null 2>&1 || break; sleep 2; done
fi
pgrep -f "python -u main.py" >/dev/null 2>&1 && IDLE=false || IDLE=true
log "idle=$IDLE"

# ---------- A1. convert ts->mp4 (parallel) ----------
log "convert ts->mp4 ..."
python3 - "$DATA_DIR" "$DATE" <<'PYEOF' || log "WARN convert had issues (non-fatal)"
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
( cd "$APP_DIR" && python3 - "$DATA_DIR" "$DATE" "$CHECK_JSON" <<'PYEOF'
import sys, glob, os, json
sys.path.insert(0, os.getcwd()); import align
data, date, out = sys.argv[1], sys.argv[2], sys.argv[3]
sessions = sorted(s for s in glob.glob(f"{data}/{date}/*") if os.path.isdir(s))
stats = align.check(sessions) or {}
json.dump(stats, open(out, "w", encoding="utf-8"), ensure_ascii=False)
PYEOF
) || log "WARN align/check had issues (non-fatal)"

# ---------- B. pack tonight's sessions into a plain staging tree ----------
log "pack -> $STAGING ..."
rm -rf "$STAGING"
if ! python3 "$HERE/pack.py" --station "$STATION" --date "$DATE" \
      --data-dir "$DATA_DIR" --out-dir "$STAGING" --shard-gb "$SHARD_GB"; then
  log "pack FAILED — aborting (nothing purged)"; rm -f "$CHECK_JSON"; exit 1
fi

# ---------- B1. fold idle + bandwidth into the staging manifest (uploaded with the data) ----------
MANIFEST="$STAGING/manifest/$DATE/$STATION.json"
python3 - "$MANIFEST" "$IDLE" "$CHECK_JSON" <<'PYEOF' || true
import json, sys, os
p, idle, cj = sys.argv[1], sys.argv[2], sys.argv[3]
m = json.load(open(p, encoding="utf-8")); m["idle"] = (idle == "true")
try:
    if cj and os.path.exists(cj):
        chk = json.load(open(cj, encoding="utf-8"))
        m["bandwidth"] = chk.get("bandwidth")
        m["completeness"] = chk.get("completeness")
except Exception: pass
json.dump(m, open(p, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
PYEOF
rm -f "$CHECK_JSON"

# ---------- B2. archive text bundle + manifest locally (kept forever) ----------
mkdir -p "$ARCHIVE/text/$DATE" "$ARCHIVE/manifest/$DATE"
cp -f "$STAGING/text/$DATE/$STATION.tar.gz" "$ARCHIVE/text/$DATE/" 2>/dev/null && log "archived text bundle" || log "WARN no text bundle to archive"
cp -f "$MANIFEST" "$ARCHIVE/manifest/$DATE/$STATION.json" 2>/dev/null || true

# ---------- C. upload via SDK (retry-429 + verify) ----------
log "upload -> $REPO_ID ..."
if "$PY" "$HERE/ms_upload.py" --repo-id "$REPO_ID" --staging "$STAGING" \
      --station "$STATION" --date "$DATE" ${MS_TOKEN_FROM:+--token-from "$MS_TOKEN_FROM"}; then
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
