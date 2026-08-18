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
       → transcribe each room (SenseVoice-Small, CPU) → transcript.csv + lossless FLAC voice audio
       → pack: video shards (≤7GB) / audio tar / text+transcript bundle / manifest
       → [wait UPLOAD_DELAY] → upload to ModelScope via SDK + verify
       → purge local video+audio (only after verify) → keep text+manifest locally
       → write a status line (the "morning brief")
```
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
- A **ModelScope access token** with **write** access to `SISU_DynCogLab/douyin`.
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
```bash
cd ~
git clone git@github.com:wangruosi/DouyinBarrage.git
cd DouyinBarrage
git checkout feat/v2                 # REQUIRED: the v2 fleet code lives on this branch
git branch --show-current            # -> feat/v2
ls fleet/   # -> nightly.sh postrun.sh pack.py ms_upload.py transcribe.py run.sh station.env PROTOCOL.md
```

### 2.3 Python environments (two venvs)
```bash
# a) recorder venv (websocket/protobuf/ffmpeg glue)
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# b) ASR venv (SenseVoice transcription + ModelScope SDK upload) — CPU only
python3 -m venv .venv-asr
.venv-asr/bin/pip install funasr modelscope
.venv-asr/bin/pip install torch torchaudio --index-url https://download.pytorch.org/whl/cpu
```
The first nightly run downloads the SenseVoice-Small model (~900 MB) + VAD into
`~/.cache/modelscope` once, then reuses it. (Both venvs are auto-detected by the scripts;
you never need to `activate` them manually.)

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
v2 uploads via the SDK, so there is **no dataset clone**. Provide the token one of two ways:
```bash
# recommended: export it (persist in ~/.bashrc)
echo 'export MODELSCOPE_API_TOKEN=<YOUR_TOKEN>' >> ~/.bashrc && source ~/.bashrc
```
(Alternatively set `MS_TOKEN_FROM=/path/to/any/modelscope/clone` in station.env to read a token
from an existing clone's remote URL.) **Never commit the token or paste it into tracked files.**

### 2.7 Configure this station — `fleet/station.env`
```bash
STATION=st01                    # unique id for THIS workstation (st01, st02, …)
REPO_ID=SISU_DynCogLab/douyin   # the dataset (default is correct; leave it)
START_AT=20:00                  # window start (local time)
MINUTES=120                     # window length (2h)
UPLOAD_DELAY=0                  # seconds to wait before uploading — STAGGER per station:
                                #   st01=0  st02=1800(+30m)  st03=3600(+1h)  st04=5400 …
```
`APP_DIR`/`DATA_DIR`/`ASR_JOBS`/`SHARD_GB`/`DISK_FLOOR_GB` auto-derive or have good defaults — leave them.
Staggering `UPLOAD_DELAY` keeps the fleet from all pushing at the same moment.

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

Prove the whole pipeline end-to-end on a short sample, **against the test dataset** so you don't
touch production. Run during evening hours when rooms are live:
```bash
cd ~/DouyinBarrage
bash fleet/run.sh --rooms 3 --minutes 2 \
     --stages record,check,transcribe --upload \
     --repo-id SISU_DynCogLab/douyin-test --station st01
```
**Expected:** `record → recorded N session(s) → check (FLEET SUMMARY + COMPLETENESS) →
transcribe (… @ ~35x) → pack → upload VERIFIED → OK`. Then confirm on the site:
`https://modelscope.cn/datasets/SISU_DynCogLab/douyin-test/files` → you should see
`video/<today>/st01/…`, `audio/<today>/st01.tar`, `text/<today>/st01.tar.gz`,
`manifest/<today>/st01.json`.

If you see `VERIFIED` + those four artifact types on the site → **the station is ready.**
(Use `--purge` to also delete the local test data; default keeps it.)

---

## 4. Run each night (manual)

Launch `nightly.sh` **detached**, any time before `START_AT`. It reads `START_AT`/`MINUTES` from
`station.env`, waits for the window, records, then runs the full pipeline:
```bash
cd ~/DouyinBarrage
nohup fleet/nightly.sh > runs/nightly_$(date +%Y%m%d).out 2>&1 &
echo "launched pid $!"
tail -f logs/nightly-$(date +%Y%m%d).log     # watch (Ctrl-C stops watching, NOT the run)
```
Leave the machine **powered on and awake** until the upload finishes. To change the window, edit
`START_AT`/`MINUTES` in `station.env` before launching.

---

## 5. Daily monitoring (your morning routine, ~2 min)

```bash
cd ~/DouyinBarrage
pgrep -f 'python -u main.py' | wc -l          # a) 0 = idle (nothing stuck recording)
Y=$(date -d yesterday +%Y%m%d)
python3 -m json.tool archive/manifest/$Y/st01.json | \
  grep -E '"idle"|"upload"|"summary"|"bandwidth"|"completeness"|"transcription"'   # b) the brief
df -h .                                        # c) disk
tail -n 30 logs/nightly-$Y.log                 # d) run log
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
| transcribe skipped / `No module named funasr` | ASR venv not set up | redo §2.3 (`.venv-asr` + torch/torchaudio) |
| most rooms `error`, 0 chat | cookie expired / guest throttle | refresh cookie (§2.4) |
| `pack … no sessions` | nothing recorded (all offline / window missed) | confirm rooms.txt + that streams were live |
| upload very slow (hours) | ModelScope throttling (normal) | let it finish; not an error |

Full logs: `logs/nightly-YYYYMMDD.log` and `runs/*.out`.

---

## 7. Manual / emergency procedures

### 7.1 Stop everything now (graceful)
```bash
pkill -INT -f 'python -u main.py'             # graceful: flush + close (a few seconds)
sleep 8; pgrep -f 'python -u main.py' | wc -l # should reach 0
```

### 7.2 Run a night manually (if one was missed)
```bash
cd ~/DouyinBarrage && fleet/nightly.sh        # records the configured window now-ish, then postrun
```

### 7.3 Re-try a failed upload / purge
If `upload: failed`, local data is retained. Re-run just the post-processing:
```bash
cd ~/DouyinBarrage && fleet/postrun.sh        # re-align/transcribe/pack/upload/verify; purges ONLY on VERIFIED
```
If it keeps failing, check internet + token and tell the PI. **Do NOT manually delete `data/`** —
that loses the night. (Text bundle + manifest are always archived under `archive/` regardless.)

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
git clone git@github.com:wangruosi/DouyinBarrage.git && cd DouyinBarrage && git checkout feat/v2
python3 -m venv .venv     && .venv/bin/pip install -r requirements.txt
python3 -m venv .venv-asr && .venv-asr/bin/pip install funasr modelscope && \
  .venv-asr/bin/pip install torch torchaudio --index-url https://download.pytorch.org/whl/cpu
echo 'export MODELSCOPE_API_TOKEN=<token>' >> ~/.bashrc && source ~/.bashrc
# ... cookie.txt, rooms.txt, edit fleet/station.env (STATION, START_AT, MINUTES, UPLOAD_DELAY) ...
bash fleet/run.sh --rooms 3 --minutes 2 --stages record,check,transcribe --upload \
     --repo-id SISU_DynCogLab/douyin-test --station st01        # acceptance test

# run a night (manual; launch before START_AT)
nohup fleet/nightly.sh > runs/nightly_$(date +%Y%m%d).out 2>&1 &

# each morning
pgrep -f 'python -u main.py' | wc -l                            # 0 = idle
python3 -m json.tool archive/manifest/$(date -d yesterday +%Y%m%d)/st01.json

# stop now (graceful)
pkill -INT -f 'python -u main.py'
```

Escalate to the PI on: no manifest for a night, repeated upload failures, disk < 40 GB, a wave of
`error` rooms (cookie), or `bandwidth_limited: true`.
