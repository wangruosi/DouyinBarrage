# Douyin Livestream Monitoring — Workstation Operator Protocol (v2)

Audience: a research assistant (RA) running **one standalone workstation** that records ~30
Douyin livestream rooms during a fixed nightly window (e.g. 20:00–22:00) for ~30 days,
transcribes them locally, uploads each night's data to a ModelScope dataset, and clears local disk.

You do **not** need to understand the internals. Follow the steps; watch the morning brief.

---

## 0. What it does (the nightly cycle)

```
20:00  start recording ~30 rooms (danmaku/like/social CSV + SD video, split into 1h segments)
22:00  stop → convert .ts→.mp4 → align chat↔video + coverage check
       → transcribe each room (SenseVoice-Small, CPU) → transcript_sensevoice.csv + lossless FLAC voice audio
       → pack: video shards (≤7GB) / audio tar / text+transcript bundle / manifest
       → [wait UPLOAD_DELAY] → upload to ModelScope via SDK + verify
       → purge local video+audio (only after verify) → keep text+manifest locally
       → print the NIGHTLY SUMMARY (rooms/minutes/gaps/flow) + status line (the "morning brief")
```
**Everything is keyed by session** `<date>_<START_AT>` (e.g. `20260819_2000`): recordings in
`data/<session>/<room>/`, packed to `upload_staging/<session>/`, published to
`<type>/<session>/<station>` on the dataset. One session → one bundle (a re-run the same night is
just another session — they never collide). Each run writes **one log file**
`runs/nightly_<date>.log` (the whole night — `tail -f` it to monitor).
Everything after 20:00 is automatic. Your job is **setup once**, then **check the brief each morning**.

**No git clone anywhere** — upload is a direct SDK push (`fleet/ms_upload.py`), so multiple stations
can upload to the same dataset concurrently without conflicts (each writes only its own `st0N` paths).

---

## 1. Prerequisites

**The workstation must have:**
- **Linux** (Ubuntu 22.04+ or similar). A **GPU is NOT required** — transcription runs on CPU
  (SenseVoice-Small does a 20-min room in ~35 s; a full night fits easily overnight).
- **≥150 GB free disk**. An SD (标清) night is ≈40–45 GB (video + a few GB of FLAC audio); nightly
  upload+purge keeps it flat, but a night whose upload fails is retained — keep real headroom.
- **Download bandwidth — during recording (hard, real-time).** Must sustain the *sum* of all live
  streams at once: SD ≈ 1–1.5 Mbps/room → **~40 Mbps down for 30 rooms**. Live streams cannot be
  caught up — if the pull falls behind, frames are **permanently dropped**. Use a **direct**
  connection (no throttling proxy/VPN); `record.sh` defaults to direct.
- **Upload bandwidth — after the window.** ModelScope throttles ~20 Mbps/connection, so a night
  (~40 GB) can take a few hours — fine as long as the machine stays on. A slow upload only delays.
- **Always-on** — auto sleep/suspend/hibernate/shutdown **disabled**. Sleeping mid-recording or
  mid-upload loses or unverifies that night. Easiest thing to get wrong.
- **SSH access** — to operate and monitor remotely.

**Software** (installed once, §2.1): Python 3, ffmpeg, Node 18+, git.

**Accounts / secrets (get from the PI before starting):**
- A **ModelScope access token** with **write** access to `SISU_DynCogLab/douyin-dataset`.
- A **Douyin cookie** (recommended — needed for multi-room runs; guest mode is throttled).
- The **room list** (`rooms.txt`), if not already provided.

---

## 2. One-time setup

### 2.1 Install system software
```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip ffmpeg git
node --version    # need >= 18 (for Douyin request signing); use nvm/NodeSource if missing
ffmpeg -version   # must exist (needs the flac + libx264 muxers — standard build is fine)
```

### 2.2 Get the project
Clone **only** the `feat/v2` branch, shallow (latest commit, no other branches/history — much smaller).
Use the **HTTPS** URL — the repo is public, so no SSH key/setup is needed:
```bash
cd ~
git clone --branch feat/v2 --single-branch --depth 1 \
  https://github.com/wangruosi/DouyinBarrage.git
cd DouyinBarrage
git branch --show-current            # -> feat/v2  (already on it — no checkout needed)
ls fleet/   # -> nightly.sh postrun.sh pack.py ms_upload.py transcribe.py run.sh station.env PROTOCOL.md
```

### 2.3 Python environment (one venv)
```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt   # recorder + SenseVoice (FunASR) + ModelScope SDK (CPU torch)
# pre-fetch the ASR models NOW (~900 MB), so the first night doesn't stall on the download:
.venv/bin/python -c "from funasr import AutoModel; AutoModel(model='iic/SenseVoiceSmall', vad_model='fsmn-vad', disable_update=True)"
```
That caches SenseVoice-Small + the VAD into `~/.cache/modelscope`; nightly runs reuse them.
(The scripts call `.venv/bin/python` directly — you never need to `activate` it.)

### 2.4 Douyin cookie (recommended)
```bash
cp cookie.example.txt cookie.txt
# In a desktop browser: log in to douyin.com → F12 → Application → Cookies →
# copy the FULL cookie string (must include sessionid + ttwid) into cookie.txt (one line).
# (cookie.txt is gitignored — it never leaves this machine.)
```
Without a logged-in cookie, Douyin throttles guest `ttwid` fetches and most rooms fail — a
cookie is effectively required for a real multi-room night.

### 2.5 Room list
`rooms.txt` — one room per line, `id,name` (`#` disables a line). Confirm your ~30 rooms:
`grep -vc '^#' rooms.txt`. **Finalize BEFORE the window — don't edit while recording runs.**

### 2.6 ModelScope token (the upload credential — NO clone needed)
v2 uploads via the SDK, so there is **no dataset clone**. Export the (single) account token:
```bash
echo 'export MODELSCOPE_API_TOKEN=<YOUR_TOKEN>' >> ~/.bashrc && source ~/.bashrc
```
**Never commit the token or paste it into tracked files.**

### 2.7 Configure this station — `fleet/station.env`
Open **`fleet/station.env`** in an editor and change the values below **in the file** — it's a bash
file the scripts *source*, so you edit the assignments in place (don't run them as commands):
```bash
STATION=st01                    # unique id for THIS workstation (st01, st02, …)
START_AT=20:00                  # window start (local time)
MINUTES=120                     # window length in minutes (120 = 2h)
UPLOAD_DELAY=0                  # seconds to wait before uploading — STAGGER per station so the
                                # fleet doesn't all push at once: st01=0 st02=1800(+30m) st03=3600(+1h) …
```
`REPO_ID` already defaults to the production dataset (leave it). `APP_DIR`/`DATA_DIR`/`ASR_JOBS`/
`SHARD_GB`/`DISK_FLOOR_GB` auto-derive or have good defaults — leave them.

### 2.8 Verify recording config — `config.yaml`
```yaml
record:
  quality: 标清         # SD; keep unless the PI wants 原画 (~5x disk/bandwidth)
  auto_convert: false   # conversion is done by postrun, off the stop path — leave false
  segment_time: 3600    # split video into 1h segments (audio mirrors this per segment)
  live_stop: false      # keep recording across brief drops within the window
```

---

## 3. Acceptance test (do this once, before scheduling)

Prove the whole pipeline end-to-end on a short sample. It uploads a tiny test night to the
dataset under station `st01` — you can delete that `…/st01` path from the dataset afterward (or
use a throwaway `--station acctest` to keep it separate). Run during evening hours when rooms are live:
```bash
cd ~/DouyinBarrage
bash fleet/run.sh --rooms 3 --minutes 2 \
     --stages record,check,transcribe --upload \
     --repo-id SISU_DynCogLab/douyin-dataset --station st01
```
**Expected:** `record → recorded N session(s) → check (FLEET SUMMARY + COMPLETENESS) →
transcribe (… @ ~35x) → pack → upload VERIFIED → OK`. Then confirm on the site:
`https://modelscope.cn/datasets/SISU_DynCogLab/douyin-dataset/files` → you should see
`video/<session>/st01/…`, `audio/<session>/st01.tar`, `text/<session>/st01.tar.gz`,
`manifest/<session>/st01.json` (where `<session>` = `<today>_<HHMM>`, e.g. `20260819_2000`).

If you see `VERIFIED` + those four artifact types on the site → **the station is ready.**
(Use `--purge` to also delete the local test data; default keeps it.)

---

## 4. Run each night (manual)

Launch `nightly.sh` **detached**, any time before `START_AT`. It reads `START_AT`/`MINUTES` from
`station.env`, waits for the window, records, then runs the full pipeline:
```bash
cd ~/DouyinBarrage
nohup fleet/nightly.sh > /dev/null 2>&1 &            # all output goes to the one run log (below)
echo "launched pid $!"
tail -f runs/nightly_$(date +%Y%m%d).log            # watch (Ctrl-C stops watching, NOT the run)
```
Leave the machine **powered on and awake** until the upload finishes. To change the window, edit
`START_AT`/`MINUTES` in `station.env` before launching.

### 4.1 Monitor progress — **one file**

The whole night — preflight → recording → convert/align/transcribe/pack/upload → **NIGHTLY
SUMMARY** — goes into a single file:
```bash
tail -f runs/nightly_$(date +%Y%m%d).log     # tonight  (use the night's date for a past run)
```
That's it — one `tail`, no phases or subdirs to juggle. The **NIGHTLY SUMMARY** (rooms recorded,
minutes each, gaps, flow, upload status) is the **last block** in the file — your whole-run picture.
(`logs/<date>.log` is the recorder's own structured, rotating app log — forensic detail you rarely
need.)

---

## 5. Daily monitoring (your morning routine, ~2 min)

```bash
cd ~/DouyinBarrage
pgrep -f 'python -u main.py' | wc -l          # a) 0 = idle (nothing stuck recording)
Y=$(date -d yesterday +%Y%m%d)
python3 -m json.tool archive/manifest/${Y}_*/st01.json | \    # ${Y}_* = last night's session(s)
  grep -E '"idle"|"upload"|"summary"|"bandwidth"|"completeness"|"transcription"'   # b) the brief
df -h .                                        # c) disk
tail -n 40 runs/nightly_$Y.log                 # d) run log — ENDS with the NIGHTLY SUMMARY
```

**What "good" looks like:**
- `idle: true`, `upload: "verified"`.
- `summary`: most rooms `recorded` + `transcribed` = room count; a few `partial`/`error` is normal.
- `bandwidth`: `bandwidth_limited: false` (recorder kept real time).
- `completeness`: high `rooms_complete`, low `uncovered_s_total` (little missed audience timeline).
- disk free comfortably above `DISK_FLOOR_GB` (40 GB).

**Red flags → act (see §6/§7):**
| Sign | Meaning | Action |
|---|---|---|
| `upload: "failed"` | tonight's upload didn't verify; data retained | re-run postrun (§7.3); check internet/token |
| `idle: false` | a recorder was still running after the window | stop it (§7.1); check the log |
| disk free < 40 GB | uploads may be backing up | fix failed nights; free space (§7.3) |
| **no manifest for last night** | the station didn't run | check machine on/awake; check the log |
| many rooms `error` (0 chat) | cookie expired / device blocked | refresh cookie (§2.4) |
| `bandwidth_limited: true` | ≥2 rooms fell behind real time | too many rooms / weak downlink — tell the PI |

**Report to the PI daily:** date, idle, upload, #recorded, #transcribed, disk free, plus any red flag.

---

## 6. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `node: command not found` / `DEVICE_BLOCKED` | Node missing/too old | install Node ≥18; re-run |
| `ffmpeg: command not found` | ffmpeg not installed | `sudo apt install ffmpeg` |
| upload `failed` / `no token` | token missing/expired/no write access | verify `MODELSCOPE_API_TOKEN`; ask PI |
| transcribe skipped / `No module named funasr` | venv not fully set up | redo §2.3 (`.venv` from requirements.txt) |
| most rooms `error`, 0 chat | cookie expired / guest throttle | refresh cookie (§2.4) |
| `pack … no sessions` | nothing recorded (all offline / window missed) | confirm rooms.txt + that streams were live |
| upload very slow (hours) | ModelScope throttling (normal) | let it finish; not an error |

Full log: `runs/nightly_<date>.log` (the whole night — recording through upload + NIGHTLY SUMMARY).
The recorder's own structured, rotating app log is `logs/<date>.log` (forensic detail).

---

## 7. Manual / emergency procedures

### 7.1 Stop / kill a run

Pick based on whether you want to **keep** tonight's recording (and still upload it) or **throw it away**.

**A) Graceful stop of the recorder — keep & still upload.** The recorder flushes and exits; the
wrapper carries on to convert/align/transcribe/pack/**upload** whatever was recorded so far:
```bash
pkill -INT -f 'python -u main.py'             # graceful: flush + close (a few seconds)
sleep 8; pgrep -f 'python -u main.py' | wc -l # should reach 0
```

**B) Fully abort the whole job — stop recording AND skip postprocessing/upload.** Kill the wrapper
first (so it can't advance to the next stage), then the recorder, then postrun if it already began:
```bash
pkill -f 'fleet/nightly.sh'                    # the wrapper  (for a test run: pkill -f 'fleet/run.sh')
pkill -INT -f 'python -u main.py'              # the recorder (graceful flush)
pkill -f 'fleet/postrun.sh'; pkill -f 'ms_upload.py'   # only if postprocessing/upload already started
sleep 8; pgrep -f 'python -u main.py' | wc -l  # confirm 0
```
Aborting is **safe**: nothing is purged unless an upload already verified, so the recording stays
under `data/<session>/`. Resume later with `fleet/postrun.sh --session <session>` (§7.3).

### 7.2 Run a night manually (if one was missed)
```bash
cd ~/DouyinBarrage && fleet/nightly.sh        # records the configured window now-ish, then postrun
```

### 7.3 Re-try a failed upload / purge
If `upload: failed`, local data is retained. Re-run just the post-processing — **pass the session**
(`ls -d data/*/` shows what's on disk; each is a `<date>_<HHMM>` session):
```bash
cd ~/DouyinBarrage && fleet/postrun.sh --session <YYYYMMDD_HHMM>   # e.g. --session 20260819_2000
```
It re-aligns/transcribes/packs/uploads/verifies and purges **ONLY on VERIFIED**. If it keeps
failing, check internet + token and tell the PI. **Do NOT manually delete `data/`** — that loses
the session. (Text bundle + manifest are always archived under `archive/` regardless.)

**Upload-only retry (skip the re-processing).** If the earlier run already converted/aligned/
transcribed/packed and only the *network push* failed, the packed outbox is still in
`upload_staging/<session>/`. Re-push just that — no need to redo the (slow) align/transcribe/pack:
```bash
cd ~/DouyinBarrage && fleet/postrun.sh --session <YYYYMMDD_HHMM> --upload-only
```
It uploads the existing outbox, verifies, and purges on success. If the outbox is gone (e.g. it
was cleared), it errors out and tells you to re-run **without** `--upload-only` to rebuild it.

### 7.4 Disk getting full
Usually means uploads are failing and data is piling up. Fix the upload (§7.3). Never delete `data/`
by hand unless the PI confirms that night is already safely on ModelScope.

---

## 8. Mid-study maintenance

- **Cookie refresh (~every 2 weeks):** Douyin cookies expire. Rising `error`/0-chat rooms → redo §2.4.
- **Keep the machine awake** + clock correct (NTP on — the window timing depends on it).
- **Weekly:** glance at `df -h .` and that a manifest exists for every night.

---

## 9. Quick reference

```bash
# setup (once)
git clone --branch feat/v2 --single-branch --depth 1 https://github.com/wangruosi/DouyinBarrage.git && cd DouyinBarrage
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
echo 'export MODELSCOPE_API_TOKEN=<token>' >> ~/.bashrc && source ~/.bashrc
# ... cookie.txt, rooms.txt, edit fleet/station.env (STATION, START_AT, MINUTES, UPLOAD_DELAY) ...
bash fleet/run.sh --rooms 3 --minutes 2 --stages record,check,transcribe --upload \
     --repo-id SISU_DynCogLab/douyin-dataset --station st01        # acceptance test

# run a night (manual; launch before START_AT)
nohup fleet/nightly.sh > /dev/null 2>&1 &                       # logs -> runs/nightly_<date>.log

# monitor a live run  (ONE file: recording -> upload -> NIGHTLY SUMMARY)
tail -f runs/nightly_$(date +%Y%m%d).log

# each morning
pgrep -f 'python -u main.py' | wc -l                            # 0 = idle
python3 -m json.tool archive/manifest/$(date -d yesterday +%Y%m%d)_*/st01.json
tail -n 40 runs/nightly_$(date -d yesterday +%Y%m%d).log        # run log

# stop recorder gracefully (keeps & still uploads)   |   fully abort the job
pkill -INT -f 'python -u main.py'                     #   pkill -f 'fleet/nightly.sh'; pkill -INT -f 'python -u main.py'
```

Escalate to the PI on: no manifest for a night, repeated upload failures, disk < 40 GB, a wave of
`error` rooms (cookie), or `bandwidth_limited: true`.
