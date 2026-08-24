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
import argparse, json, os, re, shutil, subprocess, sys, tarfile, tempfile, time
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


def _noproxy_env():
    """This host's route to ModelScope's OSS origin is ~0.04 MB/s and via the proxy ~1 MB/s, but the
    CDN (cdn-lfs-*.modelscope.cn) direct + parallel gives ~13 MB/s — so strip proxy vars for aria2c."""
    return {k: v for k, v in os.environ.items()
            if k.lower() not in ("http_proxy", "https_proxy", "all_proxy")}


def _cdn_url(api, repo, filepath, revision="master"):
    """Resolve a dataset file's pre-signed CDN URL (the fast path the website uses)."""
    ns, name = repo.split("/")
    url = (f"https://modelscope.cn/api/v1/datasets/{ns}/{name}/repo"
           f"?Revision={revision}&FilePath={filepath}")
    r = api.session.get(url, allow_redirects=False, timeout=60)
    loc = r.headers.get("Location", "")
    if r.status_code in (301, 302, 303, 307, 308) and loc:
        return loc
    raise RuntimeError(f"no CDN redirect for {filepath} (status {r.status_code})")


def cdn_pull(repo, date, station, dest, with_video, token, conns=8):
    """Fast pull: aria2c each file from its CDN URL (direct, -x{conns}) into dest/<repo-path>/… ,
    mirroring snapshot_download's layout so reshape() is unchanged. ~13 MB/s vs ~1 via the SDK."""
    from modelscope.hub.api import HubApi
    api = HubApi(); api.login(token)
    files = [f"text/{date}/{station}.tar.gz", f"manifest/{date}/{station}.json"]
    if with_video:
        allf = api.list_repo_files(repo_id=repo, repo_type="dataset", revision="master")
        files += sorted(p for p in (getattr(x, "path", "") for x in allf)
                        if p.startswith(f"video/{date}/{station}/") and p.endswith(".tar"))
    env = _noproxy_env()
    for fp in files:
        out = os.path.join(dest, fp)
        os.makedirs(os.path.dirname(out), exist_ok=True)
        ctl = out + ".aria2"
        # Robust download over a slow / rate-limited CDN with short-lived URLs:
        #   * NO --lowest-speed-limit — the CDN can crawl at ~50 KiB/s/conn and we must NOT abort
        #     a slow-but-progressing transfer.
        #   * aria2c runs in bounded WINDOWs (timeout); after each window we re-resolve a FRESH URL
        #     (the pre-signed token is short-lived) and resume via --continue. So every window makes
        #     forward progress regardless of token expiry, and a true hang can't last past one window.
        #   * Completion is judged by FILE SIZE, not exit code.
        expected = None
        WINDOW, DEADLINE = 600, 6 * 3600           # 10-min token windows; give up after 6h/file
        start = time.time()
        while True:
            url = _cdn_url(api, repo, fp)
            if expected is None:
                try: expected = int(api.session.head(url, timeout=30).headers.get("Content-Length") or 0)
                except Exception: expected = 0
            cmd = ["aria2c", "--dir", os.path.dirname(out), "--out", os.path.basename(out),
                   "--max-connection-per-server", str(conns), "--split", str(conns),
                   "--min-split-size", "1M", "--continue=true", "--auto-file-renaming=false",
                   "--timeout=60", "--connect-timeout=30", "--max-tries=5", "--retry-wait=5",
                   "--auto-save-interval=15", "--console-log-level=warn", "--summary-interval=10", url]
            rc = None
            try: rc = subprocess.run(cmd, env=env, timeout=WINDOW).returncode
            except subprocess.TimeoutExpired: pass          # window elapsed; re-resolve + resume
            sz = os.path.getsize(out) if os.path.exists(out) else 0
            if (expected and sz >= expected) or (rc == 0 and not os.path.exists(ctl)):
                if os.path.exists(ctl): os.remove(ctl)
                break
            if time.time() - start > DEADLINE:
                raise RuntimeError(f"{fp}: gave up after 6h ({sz}/{expected} bytes)")
            log(f"  {os.path.basename(fp)}: {sz//1_000_000}/{(expected or 0)//1_000_000} MB "
                f"({(time.time()-start)/60:.0f} min) — refreshing URL, resuming")
    return dest


def pull_src(repo, date, station, dest, with_video, token):
    """Download the station-date bundle; return the local snapshot dir. Prefers the fast CDN+aria2c
    path (this host is badly peered to the OSS origin); falls back to the SDK if aria2c is absent."""
    if shutil.which("aria2c"):
        try:
            log("pull via CDN+aria2c (fast path) ...")
            return cdn_pull(repo, date, station, dest, with_video, token)
        except Exception as e:
            log(f"CDN pull failed ({type(e).__name__}: {e}); falling back to SDK snapshot_download")
    from modelscope import snapshot_download
    patterns = [f"text/{date}/{station}.tar.gz", f"manifest/{date}/{station}.json"]
    if with_video:
        patterns.append(f"video/{date}/{station}/*")
    return snapshot_download(repo, repo_type="dataset", cache_dir=dest, allow_patterns=patterns)


def _append_csv(dst_file, src_file):
    """Append src rows to an existing dst CSV, dropping src's header (segments share a schema).
    Rows carry wall-clock `time`, so a reopen room's two segments concatenate chronologically."""
    src_lines = Path(src_file).read_text(encoding="utf-8-sig").splitlines(keepends=True)
    body = src_lines[1:] if src_lines else []
    with open(dst_file, "a", encoding="utf-8-sig") as f:
        if body and not (open(dst_file, encoding="utf-8-sig").read().endswith("\n")):
            f.write("\n")
        f.writelines(body)


def _merge_into(dst, seg_dir, seg_stamp):
    """Move one segment's files into the room dir. CSV collisions APPEND; other collisions
    (e.g. a per-session .db) get suffixed with the segment stamp; the rest just move."""
    dst.mkdir(parents=True, exist_ok=True)
    for f in seg_dir.iterdir():
        target = dst / f.name
        if not target.exists():
            shutil.move(str(f), str(target))
        elif f.suffix.lower() == ".csv":
            _append_csv(target, f); f.unlink()
        else:
            shutil.move(str(f), str(dst / f"{f.stem}__{seg_stamp}{f.suffix}"))


def reshape(snap, date, station, data_root, with_video, consolidate=False, session_override=None):
    """Extract text (+ video) and re-key {room_id}/{session}/... -> data_root/<session>/<room_id>/... ;
    reconstruct meta.json from the old manifest. Returns the sorted list of sessions written.

    consolidate=True collapses every old stamp into ONE session (the earliest stamp, i.e. the
    scheduled ~_1958 start, or `session_override`); a room recorded under several stamps (a break +
    reopen, or a late start) is merged into a single room dir instead of overwriting."""
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

    # room_id -> {stamp: seg_dir}
    rooms = {}
    for room_id in sorted(p.name for p in Path(scratch).iterdir() if p.is_dir()):
        for sess_dir in sorted(p for p in (Path(scratch) / room_id).iterdir() if p.is_dir()):
            rooms.setdefault(room_id, {})[sess_dir.name] = sess_dir

    all_stamps = sorted({s for segs in rooms.values() for s in segs})
    target = session_override or (all_stamps[0] if all_stamps else f"{date}_1958")   # earliest = scheduled start

    written = set()
    for room_id, segs in rooms.items():
        room_sessions = set()
        for stamp, seg_dir in sorted(segs.items()):
            session = target if consolidate else stamp
            room_sessions.add(session); written.add(session)
            _merge_into(Path(data_root) / session / room_id, seg_dir, stamp)
        mr = man_rooms.get(room_id, {})
        meta = json.dumps(dict(room_id=room_id, live_id=mr.get("live_id", ""),
                               anchor_name=mr.get("name", room_id)), ensure_ascii=False)
        for session in room_sessions:
            (Path(data_root) / session / room_id / "meta.json").write_text(meta, encoding="utf-8")
    shutil.rmtree(scratch, ignore_errors=True)
    return sorted(written)


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


def pack_session(py, session, station, data_root, shard_gb):
    """Build the shallow staging outbox for one session. Returns its path, or None on failure."""
    outbox = Path(data_root).parent / "outbox" / session
    shutil.rmtree(outbox, ignore_errors=True)
    r = subprocess.run([py, os.path.join(HERE, "pack.py"), "--station", station, "--session", session,
                        "--data-dir", str(data_root), "--out-dir", str(outbox), "--shard-gb", str(shard_gb)])
    if r.returncode != 0:
        log(f"pack FAILED for {session}"); return None
    return outbox


def upload_outbox(py, outbox, session, station, dst_repo, token):
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
    ap.add_argument("--no-upload", action="store_true", help="pull + reorganize + transcribe only; do NOT upload")
    ap.add_argument("--out-dir", help="persist the reorganized/transcribed data here (default: temp, deleted)")
    ap.add_argument("--consolidate", action="store_true",
                    help="collapse all old stamps into one session (earliest, ~_1958); merge reopen rooms")
    ap.add_argument("--session", help="override the consolidated session stamp (default: earliest found)")
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

    persist = bool(a.out_dir)
    work = os.path.abspath(a.out_dir) if persist else tempfile.mkdtemp(prefix=f"backfill_{a.station}_{a.date}_")
    os.makedirs(work, exist_ok=True)
    data_root = os.path.join(work, "data")
    try:
        log(f"=== {a.station}/{a.date}  (src={a.src_repo}, {'text-only' if a.text_only else 'full'}) ===")
        log("pull ...")
        snap = pull_src(a.src_repo, a.date, a.station, os.path.join(work, "dl"), with_video, tok)
        sessions = reshape(snap, a.date, a.station, data_root, with_video,
                           consolidate=a.consolidate, session_override=a.session)
        how = "consolidated into" if a.consolidate else "re-keyed into"
        log(f"{how} {len(sessions)} session(s): {', '.join(sessions)}")

        ok = 0
        for session in sessions:
            sdir = os.path.join(data_root, session)
            if not a.text_only:
                log(f"transcribe {session} ...")
                stats = transcribe_session_dir(sdir, jobs=a.jobs)
                log(f"  {stats.get('transcribed', 0)} rooms, {stats.get('sentences', 0)} sentences @ {stats.get('mean_rtf')}x")
                if a.skip_video_upload:
                    drop_video(sdir)
            log(f"pack {session} ...")
            outbox = pack_session(py, session, a.station, data_root, a.shard_gb)
            if outbox is None:
                log(f"✗ {session}/{a.station} pack FAILED"); continue
            if a.no_upload:
                log(f"— {session}/{a.station}: packed, NO upload. staging outbox at {outbox}")
                ok += 1
                continue
            log(f"upload {session} -> {a.dst_repo} ...")
            if upload_outbox(py, outbox, session, a.station, a.dst_repo, tok):
                log(f"✓ {session}/{a.station} uploaded")
                ok += 1
            else:
                log(f"✗ {session}/{a.station} upload FAILED")
        if a.no_upload:
            log(f"DONE (no-upload) {ok}/{len(sessions)} session(s) packed -> inspect staging at {os.path.join(work, 'outbox')}")
        else:
            log(f"DONE {ok}/{len(sessions)} session(s) for {a.station}/{a.date}")
    finally:
        if not persist:
            shutil.rmtree(work, ignore_errors=True)   # keep --out-dir for inspection


if __name__ == "__main__":
    main()
