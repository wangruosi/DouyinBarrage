#!/usr/bin/env python
# coding: utf-8
"""fleet/backfill.py — bring OLD (main-branch) recordings into the v2 per-session dataset.

Old data (default src SISU_DynCogLab/douyin) is per-DATE {video,text,manifest}/<date>/<station>,
arc {room_id}/{session}/..., WITH timing + aligned CSVs but NO SenseVoice transcript, NO FLAC
audio, NO per-room meta.json. Per station-date this tool:

  pull   text bundle + manifest (+ video shards unless --text-only) from --src-repo
  reshape  {room_id}/{session}/... -> data-tree <session>/<room_id>/... , and reconstruct
           meta.json (room_id, live_id, anchor_name) from the old manifest
  [full]   SenseVoice-transcribe each room (video + timing present) -> transcript_sensevoice.csv + .16k.flac
  pack     per session (shallow outbox, reuse pack.py) + upload to --dst-repo (reuse ms_upload.py)

Modes:
  --text-only          skip video download + transcription; just re-key the old text bundle and
                       upload it (fast — for validating the per-session layout + the analysis refresh).
  --skip-video-upload  download video (to transcribe) but do NOT re-upload the multi-GB video.
  (default = full: video + transcription + audio + video upload)

Usage:
  fleet/backfill.py --date 20260818 --station st01 --text-only [--token-from ../douyin]
  fleet/backfill.py --date 20260818 --station st01 [--skip-video-upload]
"""
import argparse, json, os, re, shutil, subprocess, sys, tarfile, tempfile
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT); sys.path.insert(0, HERE)


def log(*a):
    print("[backfill]", *a, flush=True)


def resolve_token(a):
    tok = a.token or os.environ.get("MODELSCOPE_API_TOKEN")
    if not tok and a.token_from:
        try:
            url = subprocess.check_output(["git", "-C", a.token_from, "remote", "get-url", "origin"], text=True).strip()
            m = re.search(r"oauth2:([^@]+)@", url); tok = m.group(1) if m else None
        except Exception:
            pass
    return tok


def pull_src(repo, date, station, dest, with_video):
    """Download the station-date bundle from the old dataset; return the local snapshot dir."""
    from modelscope import snapshot_download
    patterns = [f"text/{date}/{station}.tar.gz", f"manifest/{date}/{station}.json"]
    if with_video:
        patterns.append(f"video/{date}/{station}/*")
    return snapshot_download(repo, repo_type="dataset", cache_dir=dest, allow_patterns=patterns)


def reshape(snap, date, station, data_root, with_video):
    """Extract text (+ video) and re-key {room_id}/{session}/... -> data_root/<session>/<room_id>/... ;
    reconstruct meta.json from the old manifest. Returns the set of sessions found."""
    manifest = json.load(open(os.path.join(snap, f"manifest/{date}/{station}.json"), encoding="utf-8"))
    man_rooms = {str(r.get("room_id")): r for r in manifest.get("rooms", [])}

    scratch = tempfile.mkdtemp(prefix="bf_extract_")
    with tarfile.open(os.path.join(snap, f"text/{date}/{station}.tar.gz"), "r:gz") as t:
        t.extractall(scratch)
    if with_video:
        vdir = os.path.join(snap, f"video/{date}/{station}")
        for shard in sorted(Path(vdir).glob("*.tar")):
            with tarfile.open(shard) as t:
                t.extractall(scratch)                       # same {room_id}/{session}/ tree

    sessions = set()
    for room_id in sorted(p.name for p in Path(scratch).iterdir() if p.is_dir()):
        for sess_dir in sorted(p for p in (Path(scratch) / room_id).iterdir() if p.is_dir()):
            session = sess_dir.name                          # e.g. 20260818_1959
            sessions.add(session)
            dst = Path(data_root) / session / room_id
            dst.mkdir(parents=True, exist_ok=True)
            for f in sess_dir.iterdir():
                shutil.move(str(f), str(dst / f.name))
            mr = man_rooms.get(room_id, {})
            (dst / "meta.json").write_text(json.dumps(dict(
                room_id=room_id, live_id=mr.get("live_id", ""), anchor_name=mr.get("name", room_id)),
                ensure_ascii=False), encoding="utf-8")
    shutil.rmtree(scratch, ignore_errors=True)
    return sorted(sessions)


def transcribe_session_dir(session_data_dir, jobs=1):
    """SenseVoice-transcribe every room under data_root/<session>/ (video + timing present)."""
    import align, transcribe
    sessions = align.discover_sessions(session_data_dir)
    if not sessions:
        return {}
    return transcribe.transcribe_all(sessions, jobs=jobs)


def drop_video(session_data_dir):
    for p in Path(session_data_dir).rglob("*"):
        if p.suffix.lower() in (".mp4", ".ts", ".flv"):
            p.unlink()


def pack_and_upload(py, session, station, data_root, dst_repo, token, shard_gb):
    outbox = Path(data_root).parent / "outbox" / session
    shutil.rmtree(outbox, ignore_errors=True)
    r = subprocess.run([py, os.path.join(HERE, "pack.py"), "--station", station, "--session", session,
                        "--data-dir", str(data_root), "--out-dir", str(outbox), "--shard-gb", str(shard_gb)])
    if r.returncode != 0:
        log(f"pack FAILED for {session}"); return False
    env = dict(os.environ, MODELSCOPE_API_TOKEN=token)
    r = subprocess.run([py, os.path.join(HERE, "ms_upload.py"), "--repo-id", dst_repo, "--staging", str(outbox),
                        "--station", station, "--session", session], env=env)
    return r.returncode == 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", required=True, help="YYYYMMDD on the OLD dataset")
    ap.add_argument("--station", required=True, help="st01..st04 (etc.)")
    ap.add_argument("--src-repo", default="SISU_DynCogLab/douyin")
    ap.add_argument("--dst-repo", default="SISU_DynCogLab/douyin-dataset")
    ap.add_argument("--text-only", action="store_true", help="skip video+transcription (fast re-key only)")
    ap.add_argument("--skip-video-upload", action="store_true", help="download video to transcribe but don't re-upload it")
    ap.add_argument("--jobs", type=int, default=1)
    ap.add_argument("--shard-gb", type=float, default=7.0)
    ap.add_argument("--token"); ap.add_argument("--token-from")
    a = ap.parse_args()
    tok = resolve_token(a)
    if not tok:
        sys.exit("no token (MODELSCOPE_API_TOKEN / --token / --token-from)")
    py = os.path.join(ROOT, ".venv", "bin", "python")
    if not os.path.exists(py):
        py = sys.executable
    with_video = not a.text_only

    work = tempfile.mkdtemp(prefix=f"backfill_{a.station}_{a.date}_")
    data_root = os.path.join(work, "data")
    try:
        log(f"=== {a.station}/{a.date}  (src={a.src_repo}, {'text-only' if a.text_only else 'full'}) ===")
        log("pull ...")
        snap = pull_src(a.src_repo, a.date, a.station, os.path.join(work, "dl"), with_video)
        sessions = reshape(snap, a.date, a.station, data_root, with_video)
        log(f"re-keyed into {len(sessions)} session(s): {', '.join(sessions)}")

        ok = 0
        for session in sessions:
            sdir = os.path.join(data_root, session)
            if not a.text_only:
                log(f"transcribe {session} ...")
                stats = transcribe_session_dir(sdir, jobs=a.jobs)
                log(f"  {stats.get('transcribed', 0)} rooms, {stats.get('sentences', 0)} sentences @ {stats.get('mean_rtf')}x")
                if a.skip_video_upload:
                    drop_video(sdir)
            log(f"pack + upload {session} -> {a.dst_repo} ...")
            if pack_and_upload(py, session, a.station, data_root, a.dst_repo, tok, a.shard_gb):
                log(f"✓ {session}/{a.station} uploaded")
                ok += 1
            else:
                log(f"✗ {session}/{a.station} FAILED")
        log(f"DONE {ok}/{len(sessions)} session(s) for {a.station}/{a.date}")
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    main()
