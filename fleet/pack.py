#!/usr/bin/env python3
"""
fleet/pack.py — package one station-night into ModelScope upload artifacts.

Produces, under --out-dir (a plain staging tree, uploaded as-is by ms_upload.py), the
type-separated layout of the douyin dataset:

    video/{date}/{station}/{date}_{station}_shard{NN}.tar   # bin-packed rooms, each shard <= --shard-gb
    audio/{date}/{station}.tar                              # lossless FLAC, {room_id}/{segment}.flac inside
    text/{date}/{station}.tar.gz                            # all rooms' csv/meta/db/logs/transcript (kept local too)
    manifest/{date}/{station}.json                          # room->shard/audio index + sha256 + brief

Rooms are keyed by numeric room_id (from each anchor dir's meta.json), never the display name.
Video is bin-packed whole-room (never split) into <=7GB shards. Audio + text are one bundle each.

Usage:
    python fleet/pack.py --station st01 --date 20260727 \
        --data-dir data --out-dir <staging-dir>
"""
import argparse, hashlib, io, json, os, sys, tarfile, time
from collections import Counter
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


def discover(data_dir, date):
    """Return room dicts for this date. v2 layout: data/{date}/{anchor}/ — the anchor dir
    IS the session (files live directly inside; no per-session subdir)."""
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
                          anchor_dir=anchor_dir, session=anchor_dir.name,
                          video_files=video_files, audio_files=audio_files, text_files=text_files,
                          video_bytes=video_bytes, audio_bytes=audio_bytes,
                          chat_rows=chat_rows, transcript_rows=transcript_rows,
                          outcome=outcome))
    return rooms


def assign_arc_roots(rooms):
    """Give each session a tar-member root. Normally {room_id} (flat, unchanged layout); when a
    room_id appears in >1 session dir (same-day rerun / wait-mode reopen), disambiguate as
    {room_id}/{session} so the sessions never collide/overwrite. `session` (dir name) is unique."""
    counts = Counter(r["room_id"] for r in rooms)
    for r in rooms:
        r["arc_root"] = r["room_id"] if counts[r["room_id"]] == 1 else f"{r['room_id']}/{r['session']}"
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
    """archive path inside a tar: {arc_root}/{path-relative-to-anchor-dir}, where arc_root is
    {room_id} (unique) or {room_id}/{session} for a room with multiple sessions."""
    return f"{room['arc_root']}/{f.relative_to(room['anchor_dir'])}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--station", required=True)
    ap.add_argument("--date", required=True, help="YYYYMMDD")
    ap.add_argument("--data-dir", default="data")
    ap.add_argument("--out-dir", required=True, help="staging tree root")
    ap.add_argument("--shard-gb", type=float, default=7.0)
    args = ap.parse_args()

    shard_bytes = int(args.shard_gb * (1 << 30))
    st, date = args.station, args.date
    out = Path(args.out_dir)
    vdir = out / "video" / date / st
    adir = out / "audio" / date               # one FLAC tar per station lives here
    tdir = out / "text" / date
    mdir = out / "manifest" / date
    for d in (vdir, adir, tdir, mdir):
        d.mkdir(parents=True, exist_ok=True)

    rooms = discover(args.data_dir, date)
    if not rooms:
        print(f"[pack] no sessions for date={date} in {args.data_dir}", file=sys.stderr)
        sys.exit(1)
    assign_arc_roots(rooms)
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
                room_to_shard[r["session"]] = shard_name          # key by unique session, not room_id
        sz = shard_path.stat().st_size
        shard_records.append(dict(file=shard_name, bytes=sz, sha256=sha256_file(shard_path),
                                  rooms=[r["arc_root"] for r in b["rooms"]]))
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

    # ---- audio bundle: one FLAC tar per station ----
    # Straightforward structure: `tar xf {station}.tar` -> {room_id}/{segment}.flac, one FLAC
    # per video segment (mirrors the recorder's split), ready to feed VibeVoice-ASR later.
    # Room-keyed like the video/text tars; the manifest lists each room's members.
    audio_rec = None
    room_audio = {}            # session -> [ "{arc_root}/{segment}.flac", ... ]  (paths inside the tar)
    if any(r["audio_files"] for r in rooms):
        audio_path = adir / f"{st}.tar"
        with tarfile.open(audio_path, "w") as tar:          # FLAC already compressed -> plain tar
            for r in rooms:
                members = [rel_arc(r, f) for f in r["audio_files"]]   # {arc_root}/{segment}.flac
                for f, arc in zip(r["audio_files"], members):
                    add_to_tar(tar, f, arc)
                if members:
                    room_audio[r["session"]] = members
        audio_rec = dict(file=f"{date}/{st}.tar", bytes=audio_path.stat().st_size,
                         sha256=sha256_file(audio_path))
        print(f"[pack] audio bundle: {audio_path.stat().st_size/(1<<20):.1f} MB, "
              f"{sum(len(v) for v in room_audio.values())} FLAC segment(s)")

    # ---- manifest / brief ----
    manifest = dict(
        station=st, date=date,
        generated_at=time.strftime("%Y-%m-%d %H:%M:%S"),
        idle=None,  # set by postrun after asserting recorder exited
        shards=shard_records,
        text_bundle=text_rec,
        audio_bundle=audio_rec,                          # one FLAC tar per station
        audio_format="flac/16k/mono",
        rooms=[dict(room_id=r["room_id"], live_id=r["live_id"], name=r["name"],
                    session=r["session"],                        # dir name; disambiguates reopens
                    shard=room_to_shard.get(r["session"]),
                    video_bytes=r["video_bytes"], video_files=len(r["video_files"]),
                    audio=room_audio.get(r["session"], []),      # FLAC paths inside audio_bundle tar
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
