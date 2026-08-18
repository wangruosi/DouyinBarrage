#!/usr/bin/env python3
"""
fleet/pack.py — package one station-night into ModelScope upload artifacts.

Produces, under --out-dir (a git working tree), the layout designed for the douyin dataset:

    video/{date}/{station}/{date}_{station}_shard{NN}.tar   # bin-packed rooms, each shard <= --shard-gb
    text/{date}/{station}.tar.gz                            # all rooms' csv/meta/db/logs (small, kept local too)
    manifest/{date}/{station}.json                          # room->shard index + sha256 + brief

Rooms are keyed by numeric room_id (from each anchor dir's meta.json), never the display name.
Video is bin-packed whole-room (never split) into <=7GB shards. Text is one gzip bundle.

Usage:
    python fleet/pack.py --station st01 --date 20260727 --after 1920 \
        --data-dir data --out-dir /path/to/douyin
"""
import argparse, hashlib, io, json, os, shutil, sys, tarfile, time
from pathlib import Path

VIDEO_EXT = {".mp4", ".ts", ".flv"}
AUDIO_EXT = {".flac", ".opus", ".m4a", ".ogg", ".aac"}   # voice audio -> its own upload artifact
SKIP_EXT = {".wav"}                              # transient 16k wav (ASR scratch) — never uploaded
# text/sidecar files that travel in the text bundle (everything that is not bulky video/audio)
TEXT_SUFFIXES = (".csv", ".json")
DB_SUFFIXES = (".db", ".db-wal", ".db-shm")


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


def discover(data_dir, date, after=0):
    """Return room dicts for this date. v2 layout: data/{date}/{anchor}/ — the anchor dir
    IS the session (files live directly inside; no per-session subdir).
    `after` is accepted for compatibility but ignored: date-at-root already scopes to the date."""
    rooms = []
    base = Path(data_dir) / date
    if not base.is_dir():
        return rooms
    for anchor_dir in sorted(p for p in base.iterdir() if p.is_dir()):
        meta_path = anchor_dir / "meta.json"
        if meta_path.exists():
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            room_id = str(meta.get("room_id") or meta.get("live_id"))
            live_id = str(meta.get("live_id", ""))
            name = meta.get("anchor_name", anchor_dir.name)
        else:  # e.g. a re-open dir without meta; key by the dir name
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
        if not video_files and not audio_files and not text_files:
            continue
        chat_rows = csv_rows(anchor_dir / "chat.csv")
        transcript_rows = csv_rows(anchor_dir / "transcript.csv")

        outcome = "recorded" if video_files and chat_rows else \
                  ("error" if not video_files and not chat_rows else "partial")
        rooms.append(dict(room_id=room_id, live_id=live_id, name=name,
                          anchor_dir=anchor_dir, sessions=[anchor_dir],
                          video_files=video_files, audio_files=audio_files, text_files=text_files,
                          video_bytes=video_bytes, audio_bytes=audio_bytes,
                          chat_rows=chat_rows, transcript_rows=transcript_rows,
                          outcome=outcome))
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
            # a single room larger than a shard still gets its own shard (won't happen at SD)
            bins.append({"rooms": [r], "bytes": r["video_bytes"]})
    return bins


def add_to_tar(tar, file_path, arcname):
    tar.add(str(file_path), arcname=arcname)


def rel_arc(room, f):
    """archive path inside a tar: {room_id}/{path-relative-to-anchor-dir}"""
    return f"{room['room_id']}/{f.relative_to(room['anchor_dir'])}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--station", required=True)
    ap.add_argument("--date", required=True, help="YYYYMMDD")
    ap.add_argument("--after", type=int, default=0, help="only sessions with HHMM >= this")
    ap.add_argument("--data-dir", default="data")
    ap.add_argument("--out-dir", required=True, help="git working tree root")
    ap.add_argument("--shard-gb", type=float, default=7.0)
    args = ap.parse_args()

    shard_bytes = int(args.shard_gb * (1 << 30))
    st, date = args.station, args.date
    out = Path(args.out_dir)
    vdir = out / "video" / date / st
    adir = out / "audio" / date / st          # per-room per-segment FLAC lives under here
    tdir = out / "text" / date
    mdir = out / "manifest" / date
    for d in (vdir, adir, tdir, mdir):
        d.mkdir(parents=True, exist_ok=True)

    rooms = discover(args.data_dir, date, args.after)
    if not rooms:
        print(f"[pack] no sessions for date={date} after={args.after} in {args.data_dir}", file=sys.stderr)
        sys.exit(1)
    print(f"[pack] {len(rooms)} rooms: " +
          ", ".join(f"{r['name']}({r['outcome']},{r['video_bytes']//(1<<20)}MB)" for r in rooms))

    # ---- video shards ----
    bins = ffd_shards(rooms, shard_bytes)
    shard_records = []
    room_to_shard = {}
    for i, b in enumerate(bins, 1):
        shard_name = f"{date}_{st}_shard{i:02d}.tar"
        shard_path = vdir / shard_name
        with tarfile.open(shard_path, "w") as tar:  # no compression: H.264 already compressed
            for r in b["rooms"]:
                for f in r["video_files"]:
                    add_to_tar(tar, f, rel_arc(r, f))
                room_to_shard[r["room_id"]] = shard_name
        sz = shard_path.stat().st_size
        shard_records.append(dict(file=shard_name, bytes=sz, sha256=sha256_file(shard_path),
                                  rooms=[r["room_id"] for r in b["rooms"]]))
        print(f"[pack] {shard_name}: {sz/(1<<30):.2f} GB, {len(b['rooms'])} rooms")

    # ---- text bundle ----
    text_name = f"{st}.tar.gz"
    text_path = tdir / text_name
    with tarfile.open(text_path, "w:gz") as tar:
        for r in rooms:
            for f in r["text_files"]:
                add_to_tar(tar, f, rel_arc(r, f))
    text_rec = dict(file=f"{date}/{text_name}", bytes=text_path.stat().st_size,
                    sha256=sha256_file(text_path))
    print(f"[pack] text bundle: {text_path.stat().st_size/(1<<20):.1f} MB")

    # ---- audio: per-room per-segment FLAC files (NOT tarred) ----
    # Layout: audio/{date}/{station}/{room_id}/{segment}.flac — mirrors the recorder's
    # segmentation (one FLAC per video segment) and stays directly pullable later for
    # VibeVoice re-transcription (no whole-station download/extract needed).
    room_audio = {}            # room_id -> [ {file, bytes, sha256}, ... ]
    audio_total = 0
    for r in rooms:
        recs = []
        for f in r["audio_files"]:
            arc = rel_arc(r, f)                    # {room_id}/{segment}.flac
            dst = adir / arc
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, dst)
            sz = dst.stat().st_size; audio_total += sz
            recs.append(dict(file=f"{date}/{st}/{arc}", bytes=sz, sha256=sha256_file(dst)))
        if recs:
            room_audio[r["room_id"]] = recs
    if room_audio:
        print(f"[pack] audio: {sum(len(v) for v in room_audio.values())} FLAC segment(s) "
              f"across {len(room_audio)} rooms, {audio_total/(1<<20):.1f} MB")

    # ---- manifest / brief ----
    manifest = dict(
        station=st, date=date,
        generated_at=time.strftime("%Y-%m-%d %H:%M:%S"),
        idle=None,  # set by postrun after asserting recorder exited
        shards=shard_records,
        text_bundle=text_rec,
        audio_format="flac/16k/mono",
        rooms=[dict(room_id=r["room_id"], live_id=r["live_id"], name=r["name"],
                    shard=room_to_shard.get(r["room_id"]),
                    video_bytes=r["video_bytes"], video_files=len(r["video_files"]),
                    audio=room_audio.get(r["room_id"], []),      # per-segment FLAC files (pullable)
                    audio_bytes=r["audio_bytes"], chat_rows=r["chat_rows"],
                    outcome=r["outcome"],
                    transcribed=r["transcript_rows"] > 0,
                    transcript_sentences=r["transcript_rows"])
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
        upload="pending",
    )
    manifest_path = mdir / f"{st}.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[pack] manifest: {manifest_path}")
    print(f"[pack] DONE  {manifest['summary']}")


if __name__ == "__main__":
    main()
