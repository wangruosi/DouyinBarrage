#!/usr/bin/env python
# coding: utf-8
"""fleet/transcribe.py — SenseVoice-Small ASR for recorded rooms (CPU, on-station).

Per room, for each recorded video segment:
  1. extract 16 kHz mono audio (wav) with ffmpeg
  2. SenseVoice-Small + fsmn-vad  ->  chars + per-char [start,end] ms + lang/emotion/event tags
  3. split into sentences (on 。！？), map each sentence's start onto the WALL-CLOCK timeline
     via align.video_to_wall (so transcript shares the chat/like/social axis)
  4. encode a compact Opus copy of the audio (for the `audio/` upload artifact)
Writes  transcript.csv  (time, video_pts_s, end, segment_file, text, emotion, event, lang)
and      <segment>.16k.opus  next to the video.  Runs after align (needs timing_*.csv).

CLI:
  python fleet/transcribe.py <session_dir | data/DATE>   [--jobs N] [--keep-wav]
"""
import os, sys, csv, glob, re, time, subprocess, argparse
from datetime import datetime
from concurrent.futures import ProcessPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import align

TAG_RE = re.compile(r"<\|([^|]+)\|>")
LANGS  = {"zh", "en", "yue", "ja", "ko", "nospeech"}
EMOS   = {"HAPPY", "SAD", "ANGRY", "NEUTRAL", "FEARFUL", "DISGUSTED", "SURPRISED", "EMO_UNKNOWN"}
EVENTS = {"Speech", "BGM", "Applause", "Laughter", "Cry", "Sneeze", "Breath", "Cough", "Event_UNK"}
SENT_END = "。！？!?"
VIDEO_EXT = (".mp4", ".ts", ".flv")

_MODEL = None


def get_model():
    """Load SenseVoice-Small once per process (workers each load their own)."""
    global _MODEL
    if _MODEL is None:
        from funasr import AutoModel
        _MODEL = AutoModel(
            model="iic/SenseVoiceSmall", vad_model="fsmn-vad",
            vad_kwargs={"max_single_segment_time": 30000},
            device="cpu", disable_update=True,
        )
    return _MODEL


def _run(cmd, timeout=1800):
    return subprocess.run(cmd, check=True, timeout=timeout,
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def extract_wav(video, wav):
    _run(["ffmpeg", "-y", "-v", "error", "-i", video, "-ac", "1", "-ar", "16000", "-f", "wav", wav])


def encode_opus(wav, opus):
    _run(["ffmpeg", "-y", "-v", "error", "-i", wav, "-c:a", "libopus", "-b:a", "16k", opus])


def parse_tagged(text, words, ts):
    """Walk SenseVoice `text`, aligning each real char to (word, [start,end] ms) while
    tracking the current lang/emotion/event from <|..|> tags. Returns list of
    (char, start_ms, end_ms, lang, emotion, event)."""
    out, lang, emo, event, wi, pos = [], "", "", "", 0, 0
    n = min(len(words), len(ts))

    def emit(run):
        nonlocal wi
        for ch in run:
            if wi < n:
                out.append((ch, ts[wi][0], ts[wi][1], lang, emo, event)); wi += 1

    for m in TAG_RE.finditer(text):
        emit(text[pos:m.start()])
        tag = m.group(1)
        if tag in LANGS: lang = tag
        elif tag in EMOS: emo = tag
        elif tag in EVENTS: event = tag
        pos = m.end()
    emit(text[pos:])
    return out


def sentences(chars):
    """Group per-char tuples into sentences at sentence-ending punctuation."""
    groups, cur = [], []
    for c in chars:
        cur.append(c)
        if c[0] in SENT_END:
            groups.append(cur); cur = []
    if cur:
        groups.append(cur)
    out = []
    for g in groups:
        txt = "".join(c[0] for c in g).strip()
        if not txt:
            continue
        out.append(dict(
            start_ms=g[0][1], end_ms=g[-1][2], text=txt,
            lang=next((c[3] for c in g if c[3]), ""),
            emotion=next((c[4] for c in g if c[4]), ""),
            event=next((c[5] for c in g if c[5]), ""),
        ))
    return out


def transcribe_session(session_dir, keep_wav=False):
    """Transcribe every video segment in one room; write transcript.csv + <seg>.16k.opus.
    Returns stats dict, or None if there's no timing sidecar (can't place on the timeline)."""
    import wave
    timing = align.load_timing(session_dir)
    if not timing:
        return None
    idx = align.wall_index(timing)
    model = get_model()

    videos = sorted(f for f in glob.glob(os.path.join(session_dir, "*"))
                    if f.lower().endswith(VIDEO_EXT))
    rows, audio_s, t0 = [], 0.0, time.time()
    for v in videos:
        stem = os.path.splitext(os.path.basename(v))[0]
        wav = os.path.join(session_dir, stem + ".16k.wav")
        try:
            extract_wav(v, wav)
        except Exception:
            continue
        try:
            with wave.open(wav) as w:
                audio_s += w.getnframes() / w.getframerate()
            res = model.generate(input=wav, cache={}, language="auto", use_itn=True,
                                 batch_size_s=300, merge_vad=True, merge_length_s=15,
                                 output_timestamp=True)
            r = res[0]
            chars = parse_tagged(r.get("text", ""), r.get("words", []), r.get("timestamp", []))
            for s in sentences(chars):
                pts = s["start_ms"] / 1000.0
                wall = align.video_to_wall(idx, stem, pts)
                iso = datetime.fromtimestamp(wall).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3] if wall else ""
                rows.append(dict(time=iso, video_pts_s=round(pts, 3),
                                 end=round(s["end_ms"] / 1000.0, 3),
                                 segment_file=os.path.basename(v), text=s["text"],
                                 emotion=s["emotion"], event=s["event"], lang=s["lang"]))
            # compact audio artifact for upload (encode from the wav we already have)
            try:
                encode_opus(wav, os.path.join(session_dir, stem + ".16k.opus"))
            except Exception:
                pass
        finally:
            if not keep_wav and os.path.exists(wav):
                os.remove(wav)

    if rows:
        out = os.path.join(session_dir, "transcript.csv")
        with open(out, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=["time", "video_pts_s", "end", "segment_file",
                                              "text", "emotion", "event", "lang"])
            w.writeheader(); w.writerows(rows)
    dt = time.time() - t0
    return dict(room=os.path.basename(session_dir), sentences=len(rows),
                audio_s=round(audio_s, 1), compute_s=round(dt, 1),
                rtf=round(audio_s / dt, 1) if dt > 0 else None)


def _worker(args):
    sd, keep = args
    try:
        return transcribe_session(sd, keep)
    except Exception as e:
        return dict(room=os.path.basename(sd), error=str(e))


def transcribe_all(sessions, jobs=1, keep_wav=False, log=print):
    """Transcribe many rooms (ProcessPool). Returns {'rooms':[...], summary...}."""
    results = []
    if jobs > 1 and len(sessions) > 1:
        with ProcessPoolExecutor(max_workers=jobs) as ex:
            futs = {ex.submit(_worker, (sd, keep_wav)): sd for sd in sessions}
            for fu in as_completed(futs):
                r = fu.result()
                if r: results.append(r)
                if r: log(f"[asr] {r.get('room')}: "
                          + (f"{r['sentences']} sentences, {r['audio_s']}s @ {r['rtf']}x"
                             if "error" not in r else f"ERROR {r['error']}"))
    else:
        for sd in sessions:
            r = _worker((sd, keep_wav))
            if r:
                results.append(r)
                log(f"[asr] {r.get('room')}: "
                    + (f"{r['sentences']} sentences, {r['audio_s']}s @ {r['rtf']}x"
                       if "error" not in r else f"ERROR {r['error']}"))
    ok = [r for r in results if "error" not in r]
    total_audio = sum(r["audio_s"] for r in ok)
    total_comp = sum(r["compute_s"] for r in ok)
    return dict(rooms=results,
                transcribed=len(ok), failed=len(results) - len(ok),
                sentences=sum(r["sentences"] for r in ok),
                audio_hours=round(total_audio / 3600, 2),
                mean_rtf=round(total_audio / total_comp, 1) if total_comp > 0 else None)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("path", help="a session dir, or a parent like data/20260818")
    ap.add_argument("--jobs", type=int, default=1, help="parallel rooms (each loads its own model)")
    ap.add_argument("--keep-wav", action="store_true", help="keep the 16k wav (debug)")
    a = ap.parse_args()
    sessions = align.discover_sessions(a.path)
    if not sessions:
        sys.exit(f"no timing_*.csv found under {a.path}")
    print(f"[asr] transcribing {len(sessions)} room(s), jobs={a.jobs}")
    s = transcribe_all(sessions, jobs=a.jobs, keep_wav=a.keep_wav)
    print(f"[asr] DONE transcribed={s['transcribed']} failed={s['failed']} "
          f"sentences={s['sentences']} audio_h={s['audio_hours']} mean_rtf={s['mean_rtf']}x")


if __name__ == "__main__":
    main()
