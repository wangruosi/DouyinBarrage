#!/usr/bin/env bash
# fleet/postrun.sh (v2) — after recording ONE session:
#   assert-idle -> convert ts->mp4 -> align -> bandwidth verdict -> pack(outbox) -> upload(SDK) -> purge -> status
#
# Per-session, everywhere. --session YYYYMMDD_HHMM (nightly passes $DOUYIN_SESSION). No git clone;
# uploads via fleet/ms_upload.py (hardlink -> one commit -> verify). Purge is GATED on a verified
# upload; text bundle + manifest (the ledger) are archived locally forever.
# Layout:  data/{session}/{room}/  ->  upload_staging/{session}/{video/,audio.tar,text.tar.gz,manifest.json}
#          ->  endpoint {video,audio,text,manifest}/{session}/{station}
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/station.env"
# which session to process: --session YYYYMMDD_HHMM  >  $DOUYIN_SESSION env
SESSION_ARG=""; UPLOAD_ONLY=0
while [ $# -gt 0 ]; do case "$1" in
  --session) SESSION_ARG="$2"; shift 2;;
  --upload-only) UPLOAD_ONLY=1; shift;;   # resume a failed upload from existing outbox (no re-pack)
  *) echo "postrun.sh: unknown arg '$1' (use --session YYYYMMDD_HHMM | --upload-only)" >&2; exit 2;;
esac; done
SESSION="${SESSION_ARG:-${DOUYIN_SESSION:-}}"
[ -n "$SESSION" ] || { echo "postrun.sh: no session (pass --session YYYYMMDD_HHMM or export DOUYIN_SESSION)" >&2; exit 2; }
DATE="${SESSION:0:8}"
log() { echo "[postrun $(date '+%F %T')] $*"; }

STAGING="$APP_DIR/upload_staging/$SESSION"          # the shallow outbox for this session
ARCHIVE="$APP_DIR/archive"
MANIFEST="$STAGING/manifest.json"                   # the ledger (also archived)
ARCH_MANIFEST="$ARCHIVE/manifest/$SESSION/$STATION.json"
# the single project venv (recorder + funasr + modelscope); one python for every stage.
PY="$APP_DIR/.venv/bin/python"; [ -x "$PY" ] || PY=python3

# logging: under nightly.sh, $RUN_LOG is set and our stdout is already tee'd to the one run log.
# Run standalone (a manual retry), own a file so the run is still captured (tee -> file + console).
if [ -z "${RUN_LOG:-}" ]; then
  RUN_LOG="$APP_DIR/runs/postrun_$SESSION.log"; mkdir -p "$APP_DIR/runs"
  exec > >(tee -a "$RUN_LOG") 2>&1
fi

# update the archived manifest ledger's state field (uploaded/verified/purged timestamps).
patch_ledger() {  # $1=verified(true|false)  $2=purged(true|false)
  "$PY" - "$ARCH_MANIFEST" "$1" "$2" <<'PYEOF' || true
import json, sys, time
p, ver, pur = sys.argv[1], sys.argv[2], sys.argv[3]
try: m = json.load(open(p, encoding="utf-8"))
except Exception: sys.exit(0)
now = time.strftime("%Y-%m-%d %H:%M:%S"); st = m.setdefault("state", {})
if ver == "true": st["uploaded"] = st["verified"] = now
if pur == "true": st["purged"] = now
json.dump(m, open(p, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
PYEOF
}

# this app's recorder pids (main.py whose cwd is $APP_DIR, so we never touch another checkout's
# recorder on a shared machine). Defined before the branch — both paths below use it.
recorder_pids() { for p in $(pgrep -f "python -u main.py" 2>/dev/null); do
  [ "$(readlink -f "/proc/$p/cwd" 2>/dev/null)" = "$APP_DIR" ] && printf '%s ' "$p"; done; }

if [ "$UPLOAD_ONLY" = 1 ]; then
  # --upload-only: resume a previously-FAILED upload from the outbox the last run left behind.
  # Skip all the (already-done) convert/align/transcribe/pack/archive/stagger work; guard that the
  # packed outbox actually exists, then fall straight through to the upload/purge/status steps.
  [ -f "$MANIFEST" ] || {
    log "ERROR --upload-only: no packed outbox for $SESSION (expected $MANIFEST)."
    log "       Re-run postrun WITHOUT --upload-only to rebuild it from data/$SESSION."
    exit 1; }
  # SAFETY: a verified upload triggers the purge below — never do that while a recording is live.
  [ -n "$(recorder_pids)" ] && {
    log "ERROR --upload-only: recorder still running (this app) — refusing (a verified upload PURGES data/)."
    log "       Stop the recorder first (pkill -INT -f 'python -u main.py'), then retry."
    exit 1; }
  IDLE=true
  log "--upload-only: reusing outbox $STAGING (skipping convert/align/transcribe/pack/archive/stagger)"
else

# ---------- A. assert the recorder is gone ----------
if [ -n "$(recorder_pids)" ]; then
  log "WARN recorder still running (this app) — SIGINT"; kill -INT $(recorder_pids) 2>/dev/null || true
  for _ in $(seq 1 30); do [ -z "$(recorder_pids)" ] && break; sleep 2; done
fi
[ -n "$(recorder_pids)" ] && IDLE=false || IDLE=true
log "session=$SESSION idle=$IDLE"

# ---------- A1. convert ts->mp4 (parallel) ----------
log "convert ts->mp4 ..."
"$PY" - "$DATA_DIR" "$SESSION" <<'PYEOF' || log "WARN convert had issues (non-fatal)"
import sys, glob, os, subprocess, concurrent.futures as cf
data, session = sys.argv[1], sys.argv[2]
ts = sorted(glob.glob(f"{data}/{session}/*/*.ts"))
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
# align.check tags every room (chat/like/... -> *_aligned.csv, needed by the text bundle),
# then prints both verdicts and returns {bandwidth, completeness} for the manifest.
log "align + thorough check ..."
CHECK_JSON="$(mktemp)"
( cd "$APP_DIR" && "$PY" - "$DATA_DIR" "$SESSION" "$CHECK_JSON" <<'PYEOF'
import sys, glob, os, json
sys.path.insert(0, os.getcwd()); import align
data, session, out = sys.argv[1], sys.argv[2], sys.argv[3]
sessions = sorted(s for s in glob.glob(f"{data}/{session}/*") if os.path.isdir(s))   # this session's rooms
stats = align.check(sessions) or {}
json.dump(stats, open(out, "w", encoding="utf-8"), ensure_ascii=False)
PYEOF
) || log "WARN align/check had issues (non-fatal)"

# ---------- A3. transcribe (SenseVoice-Small, CPU) -> transcript_sensevoice.csv + <seg>.16k.flac ----------
# Uses $PY (the project .venv with funasr+torch). Non-fatal: a missing ASR env just skips transcripts.
log "transcribe (SenseVoice-Small, CPU, jobs=${ASR_JOBS:-1}) ..."
ASR_JSON="$(mktemp)"
( cd "$APP_DIR" && "$PY" - "$DATA_DIR" "$SESSION" "$ASR_JSON" "${ASR_JOBS:-1}" <<'PYEOF'
import sys, os, json
sys.path.insert(0, os.getcwd()); sys.path.insert(0, os.path.join(os.getcwd(), "fleet"))
import align, transcribe
data, session, out, jobs = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4])
sessions = align.discover_sessions(f"{data}/{session}")
stats = transcribe.transcribe_all(sessions, jobs=jobs) if sessions else {}
stats.pop("rooms", None)                      # keep the manifest block compact
json.dump(stats, open(out, "w", encoding="utf-8"), ensure_ascii=False)
print(f"[transcribe] {stats.get('transcribed',0)} rooms, {stats.get('sentences',0)} sentences, "
      f"{stats.get('audio_hours',0)}h @ {stats.get('mean_rtf')}x")
PYEOF
) || log "WARN transcribe had issues (non-fatal)"

# ---------- B. pack this session into the shallow outbox ----------
log "pack -> $STAGING ..."
rm -rf "$STAGING"
if ! "$PY" "$HERE/pack.py" --station "$STATION" --session "$SESSION" \
      --data-dir "$DATA_DIR" --out-dir "$STAGING" --shard-gb "$SHARD_GB"; then
  log "pack FAILED — aborting (nothing purged)"; rm -f "$CHECK_JSON" "$ASR_JSON"; exit 1
fi

# ---------- B1. fold idle + bandwidth/completeness/transcription into the outbox manifest ----------
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

# ---------- B2. archive text bundle + manifest (the ledger) locally, per session (kept forever) ----------
mkdir -p "$ARCHIVE/text/$SESSION" "$ARCHIVE/manifest/$SESSION"
cp -f "$STAGING/text.tar.gz" "$ARCHIVE/text/$SESSION/$STATION.tar.gz" 2>/dev/null && log "archived text bundle" || log "WARN no text bundle to archive"
cp -f "$MANIFEST" "$ARCH_MANIFEST" 2>/dev/null || true

# ---------- B3. upload stagger — offset stations so they don't all commit at once ----------
if [ "${UPLOAD_DELAY:-0}" -gt 0 ] 2>/dev/null; then
  log "upload stagger: sleeping ${UPLOAD_DELAY}s before push (UPLOAD_DELAY)"; sleep "$UPLOAD_DELAY"
fi

fi   # end of the full-pipeline block (skipped under --upload-only)

# ---------- C. upload via SDK (hardlink -> one commit -> verify) ----------
log "upload -> $REPO_ID ..."
if "$PY" "$HERE/ms_upload.py" --repo-id "$REPO_ID" --staging "$STAGING" \
      --station "$STATION" --session "$SESSION"; then    # token from MODELSCOPE_API_TOKEN env
  UPLOAD=verified
else
  UPLOAD=failed
fi
log "upload=$UPLOAD"
[ "$UPLOAD" = verified ] && patch_ledger true false

# ---------- D. purge — ONLY when verified ----------
if [ "$UPLOAD" = verified ]; then
  before=$(du -sk "$DATA_DIR" 2>/dev/null | awk '{print int($1/1024)}')
  rm -rf "$DATA_DIR/$SESSION" "$STAGING"
  after=$(du -sk "$DATA_DIR" 2>/dev/null | awk '{print int($1/1024)}')
  patch_ledger true true
  log "verified -> purged data/$SESSION + outbox (data/ ${before}MB->${after}MB); kept archive/ text+manifest"
else
  log "NOT verified -> retaining data/$SESSION + outbox for retry"
fi

# ---------- E. disk guard + final status line (morning brief) ----------
FREE=$(df -Pk "$DATA_DIR" | awk 'NR==2{print int($4/1024/1024)}')
[ "$FREE" -lt "$DISK_FLOOR_GB" ] && log "WARN disk low: ${FREE}GB < floor ${DISK_FLOOR_GB}GB"
log "DONE station=$STATION session=$SESSION idle=$IDLE upload=$UPLOAD disk_free=${FREE}GB"

# ---------- F. NIGHTLY SUMMARY (rooms / minutes / gaps / flow) — the LAST thing in the log ----------
# Rendered from the archived manifest (survives the purge above), so no recompute.
( cd "$APP_DIR" && "$PY" - "$ARCH_MANIFEST" "$STATION" "$SESSION" "$IDLE" "$UPLOAD" "$FREE" "${MINUTES:-0}" <<'PYEOF'
import sys, os, json
sys.path.insert(0, os.getcwd()); import align
p, station, session, idle, upload, free, win = sys.argv[1:8]
try:
    m = json.load(open(p, encoding="utf-8"))
except Exception:
    sys.exit(0)
print(align.render_nightly_summary(m, station, session, idle, upload, free, int(win)))
PYEOF
) || true

# non-zero exit if something needs a human (not idle, or upload failed)
[ "$IDLE" = true ] && [ "$UPLOAD" = verified ]
