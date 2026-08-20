#!/usr/bin/env python3
"""
fleet/pack.py — package ONE recording session into ModelScope upload artifacts.

Per-session, everywhere. A session is data/{session}/ (session = YYYYMMDD_HHMM). It is packed
independently (no cross-session merge), so room_id is the flat tar root. Output is a SHALLOW
"outbox" (uploaded by ms_upload.py, which maps it to the endpoint paths):

    <out>/video/{session}_{station}_shard{NN}.tar   # bin-packed rooms, each shard <= --shard-gb
    <out>/audio.tar                                  # lossless FLAC, {room_id}/{segment}.flac inside
    <out>/text.tar.gz                                # all rooms' csv/meta/db/logs/transcript
    <out>/manifest.json                              # the LEDGER (state + artifacts + per-room brief)

ms_upload.py then publishes these to the endpoint, per session + station:
    video/{session}/{station}/…   audio/{session}/{station}.tar
    text/{session}/{station}.tar.gz   manifest/{session}/{station}.json

Rooms are keyed by numeric room_id (from each anchor dir's meta.json), never the display name.

Usage:
    python fleet/pack.py --station st01 --session 20260727_2000 \
        --data-dir data --out-dir upload_staging/20260727_2000
"""
import argparse, hashlib, json, os, sys, tarfile, time
from pathlib import Path

VIDEO_EXT = {".mp4", ".ts", ".flv"}
AUDIO_EXT = {".flac", ".opus", ".m4a", ".ogg", ".aac"}   # voice audio -> its own upload artifact
SKIP_EXT = {".wav"}                              # transient 16k wav (ASR scratch) — never uploaded


def sha256_file(path, buf=1 << 20):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(buf), b""):
            h.update(chunk)
    return h.hexdigest()


def csv_rows(path):
    """data rows in a csv (excludes header); 0 if absent/empty."""
    if not path.exists():
        return 0
    n = 0
    with open(path, "rb") as f:
        for _ in f:
            n += 1
    return max(0, n - 1)


def discover(data_dir, session):
    """Return room dicts for ONE session: data/{session}/{anchor}/ — the anchor dir IS the room
    (files live directly inside). No cross-session merge, so room_id is the flat tar root; a rare
    duplicate room_id within one session is disambiguated by the anchor dir name."""
    sess = Path(data_dir) / session
    if not sess.is_dir():
        return []
    rooms, seen = [], set()
    for anchor_dir in sorted(p for p in sess.iterdir() if p.is_dir()):
        meta_path = anchor_dir / "meta.json"
        if meta_path.exists():
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            room_id = str(meta.get("room_id") or meta.get("live_id"))
            live_id = str(meta.get("live_id", ""))
            name = meta.get("anchor_name", anchor_dir.name)
        else:  # no meta — last-resort key by the dir name
            room_id, live_id, name = anchor_dir.name, "", anchor_dir.name

        video_files, audio_files, text_files, video_bytes, audio_bytes = [], [], [], 0, 0
        for f in sorted(anchor_dir.rglob("*")):
            if f.is_dir():
                continue
            suf = f.suffix.lower()
            if suf in SKIP_EXT:
                continue
            if suf in VIDEO_EXT:
                video_files.append(f); video_bytes += f.stat().st_size
            elif suf in AUDIO_EXT:
                audio_files.append(f); audio_bytes += f.stat().st_size
            else:  # csv, json, db, wal, shm, logs/* -> text bundle
                text_files.append(f)
        if not (video_files or audio_files or text_files):
            continue
        arc_root = room_id if room_id not in seen else f"{room_id}__{anchor_dir.name}"
        seen.add(room_id)
        chat_rows = csv_rows(anchor_dir / "chat.csv")
        # prefer the model-marked name; fall back to the legacy transcript.csv for old data
        transcript_rows = (csv_rows(anchor_dir / "transcript_sensevoice.csv")
                           or csv_rows(anchor_dir / "transcript.csv"))
        outcome = "recorded" if video_files and chat_rows else \
                  ("error" if not video_files and not chat_rows else "partial")
        rooms.append(dict(room_id=room_id, live_id=live_id, name=name, anchor_dir=anchor_dir,
                          arc_root=arc_root, video_files=video_files, audio_files=audio_files,
                          text_files=text_files, video_bytes=video_bytes, audio_bytes=audio_bytes,
                          chat_rows=chat_rows, transcript_rows=transcript_rows, outcome=outcome))
    return rooms


def ffd_shards(rooms, shard_bytes):
    """First-fit-decreasing bin-packing of whole rooms (by video_bytes) into <=shard_bytes bins.
       Rooms with no video are skipped (text-only rooms carry no shard)."""
    bins = []  # each: {"rooms": [...], "bytes": int}
    for r in sorted((x for x in rooms if x["video_bytes"] > 0),
                    key=lambda x: x["video_bytes"], reverse=True):
        placed = False
        for b in bins:
            if b["bytes"] + r["video_bytes"] <= shard_bytes:
                b["rooms"].append(r); b["bytes"] += r["video_bytes"]; placed = True; break
        if not placed:
            bins.append({"rooms": [r], "bytes": r["video_bytes"]})   # oversize room -> own shard
    return bins


def rel_arc(room, f):
    """archive path inside a tar: {arc_root}/{path-relative-to-anchor-dir}."""
    return f"{room['arc_root']}/{f.relative_to(room['anchor_dir'])}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--station", required=True)
    ap.add_argument("--session", required=True, help="YYYYMMDD_HHMM recording session")
    ap.add_argument("--data-dir", default="data")
    ap.add_argument("--out-dir", required=True, help="shallow outbox dir, e.g. upload_staging/<session>")
    ap.add_argument("--shard-gb", type=float, default=7.0)
    args = ap.parse_args()

    shard_bytes = int(args.shard_gb * (1 << 30))
    st, session = args.station, args.session
    date = session[:8]
    out = Path(args.out_dir)
    vdir = out / "video"; vdir.mkdir(parents=True, exist_ok=True)   # shards here; others are files at out/

    rooms = discover(args.data_dir, session)
    if not rooms:
        print(f"[pack] no rooms in {args.data_dir}/{session}", file=sys.stderr); sys.exit(1)
    print(f"[pack] session {session} station {st}: {len(rooms)} rooms: " +
          ", ".join(f"{r['name']}({r['outcome']},{r['video_bytes']//(1<<20)}MB)" for r in rooms))

    # ---- video shards -> <out>/video/{session}_{station}_shardNN.tar ----
    bins = ffd_shards(rooms, shard_bytes)
    shard_records, room_to_shard = [], {}
    for i, b in enumerate(bins, 1):
        shard_name = f"{session}_{st}_shard{i:02d}.tar"
        shard_path = vdir / shard_name
        with tarfile.open(shard_path, "w") as tar:          # no compression: H.264 already compressed
            for r in b["rooms"]:
                for f in r["video_files"]:
                    tar.add(str(f), arcname=rel_arc(r, f))
                room_to_shard[r["arc_root"]] = shard_name
        sz = shard_path.stat().st_size
        shard_records.append(dict(file=f"video/{session}/{st}/{shard_name}", bytes=sz,
                                  sha256=sha256_file(shard_path), rooms=[r["arc_root"] for r in b["rooms"]]))
        print(f"[pack] {shard_name}: {sz/(1<<30):.2f} GB, {len(b['rooms'])} rooms")

    # ---- text bundle -> <out>/text.tar.gz ----
    text_path = out / "text.tar.gz"
    with tarfile.open(text_path, "w:gz") as tar:
        for r in rooms:
            for f in r["text_files"]:
                tar.add(str(f), arcname=rel_arc(r, f))
    text_rec = dict(file=f"text/{session}/{st}.tar.gz", bytes=text_path.stat().st_size,
                    sha256=sha256_file(text_path))
    print(f"[pack] text bundle: {text_path.stat().st_size/(1<<20):.1f} MB")

    # ---- audio bundle -> <out>/audio.tar  (tar xf -> {room_id}/{segment}.flac) ----
    audio_rec, room_audio = None, {}
    if any(r["audio_files"] for r in rooms):
        audio_path = out / "audio.tar"
        with tarfile.open(audio_path, "w") as tar:          # FLAC already compressed -> plain tar
            for r in rooms:
                members = [rel_arc(r, f) for f in r["audio_files"]]
                for f, arc in zip(r["audio_files"], members):
                    tar.add(str(f), arcname=arc)
                if members:
                    room_audio[r["arc_root"]] = members
        audio_rec = dict(file=f"audio/{session}/{st}.tar", bytes=audio_path.stat().st_size,
                         sha256=sha256_file(audio_path))
        print(f"[pack] audio bundle: {audio_path.stat().st_size/(1<<20):.1f} MB, "
              f"{sum(len(v) for v in room_audio.values())} FLAC segment(s)")

    # ---- manifest / LEDGER -> <out>/manifest.json ----
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    manifest = dict(
        session=session, station=st, date=date,
        generated_at=now,
        idle=None,  # set by postrun after asserting recorder exited
        # pipeline state — postrun/ms_upload fill uploaded/verified/purged into the archived copy
        state=dict(recorded=True, packed=now, uploaded=None, verified=None, purged=None),
        # every uploaded artifact's endpoint path + size + checksum (verify against the dataset)
        artifacts=dict(video=shard_records, audio=audio_rec, text=text_rec,
                       manifest=dict(file=f"manifest/{session}/{st}.json")),
        audio_format="flac/16k/mono",
        rooms=[dict(room_id=r["room_id"], live_id=r["live_id"], name=r["name"], arc_root=r["arc_root"],
                    shard=room_to_shard.get(r["arc_root"]),
                    video_bytes=r["video_bytes"], video_files=len(r["video_files"]),
                    audio=room_audio.get(r["arc_root"], []), audio_bytes=r["audio_bytes"],
                    chat_rows=r["chat_rows"], outcome=r["outcome"],
                    transcribed=r["transcript_rows"] > 0, transcript_sentences=r["transcript_rows"])
               for r in rooms],
        summary=dict(rooms=len(rooms),
                     recorded=sum(r["outcome"] == "recorded" for r in rooms),
                     partial=sum(r["outcome"] == "partial" for r in rooms),
                     error=sum(r["outcome"] == "error" for r in rooms),
                     transcribed=sum(r["transcript_rows"] > 0 for r in rooms),
                     video_gb=round(sum(r["video_bytes"] for r in rooms) / (1 << 30), 2),
                     audio_mb=round(sum(r["audio_bytes"] for r in rooms) / (1 << 20), 1),
                     audio_segments=sum(len(v) for v in room_audio.values()),
                     shards=len(shard_records)),
    )
    (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[pack] manifest: {out/'manifest.json'}")
    print(f"[pack] DONE  {manifest['summary']}")


if __name__ == "__main__":
    main()
