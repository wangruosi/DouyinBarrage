#!/usr/bin/env python
# coding: utf-8
"""Map chat/like/social/stats timestamps <-> recorded video position, using the
wall-clock<->out_time sidecar (timing_*.csv) written by the recorder.

Usage:
  # tag a data CSV with (segment_file, video_pts_s, in_gap):
  python align.py tag  <session_dir> [chat|like|social|stats]

  # look up where a wall-clock moment lands in the video:
  python align.py at   <session_dir> "2026-07-26 18:45:03"

  # extract the video frame for a wall-clock moment (needs ffmpeg):
  python align.py frame <session_dir> "2026-07-26 18:45:03" [out.jpg]

  # per-room health: segments, break durations, in_gap/outside counts
  # (point at one session dir, or a parent like data/ for every room)
  python align.py summary <session_dir | data/>

  # thorough post-run check: bandwidth verdict (pipe) + completeness verdict (payload
  # coverage — uncovered audience-timeline seconds, events with no video, align failures)
  python align.py check <session_dir | data/>
"""
import csv, os, sys, glob, subprocess
from datetime import datetime


def _media_exists(session_dir, stem):
    return any(os.path.exists(os.path.join(session_dir, stem + e)) for e in ('.mp4', '.ts', '.flv'))


def _probe_dur(path):
    """Media duration in seconds via ffprobe (0.0 on failure)."""
    try:
        out = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
                              '-of', 'csv=p=0', path], capture_output=True, text=True, timeout=30).stdout.strip()
        return float(out) if out else 0.0
    except Exception:
        return 0.0


def _seg_spans(session_dir, base_stem):
    """Map a sidecar's base stem to the actual segment file(s) in the CONTINUOUS out_time timeline.

    ffmpeg's `-f segment` writes {base}_000/{base}_001/... with -reset_timestamps (each file is
    zero-based) while -progress out_time keeps counting continuously. So a row's out_time picks
    which file it lands in, and (out_time - file_start) is the zero-based position inside it.
    Boundaries come from the files' own durations (== the segment-list boundaries).
    Returns [(stem, start_out, end_out)] sorted; single-file recording -> [(base_stem, 0, inf)]."""
    if _media_exists(session_dir, base_stem):          # non-segmented: the base IS the file
        return [(base_stem, 0.0, float('inf'))]
    segs = []
    for ext in ('.mp4', '.ts', '.flv'):                # segmented: {base}_NNN.*
        segs = sorted(glob.glob(os.path.join(session_dir, f'{base_stem}_[0-9][0-9][0-9]{ext}')))
        if segs:
            break
    if not segs:
        return [(base_stem, 0.0, float('inf'))]         # nothing on disk yet — leave as-is
    spans, c = [], 0.0
    for p in segs:
        stem = os.path.splitext(os.path.basename(p))[0]
        d = _probe_dur(p)
        spans.append((stem, c, c + d)); c += d
    stem, s, _ = spans[-1]
    spans[-1] = (stem, s, float('inf'))                 # last span open-ended (absorbs rounding)
    return spans


def load_timing(session_dir):
    """Return sorted list of (wall_epoch, segment_stem, seek_s).

    Segment-aware: the sidecar records a CONTINUOUS out_time under the base filename, but with
    `segment_time>0` the media is split into {base}_000/{base}_001/... (each zero-based). We
    re-attribute every row to the actual segment file it falls in and re-base out_time to that
    file's start, so seek_s is the correct 0-based `-ss` into that segment. For a single-file
    recording this reduces to the old behavior (video_pts_s - segment's first pts)."""
    raw = []
    for f in glob.glob(os.path.join(session_dir, 'timing_*.csv')):
        with open(f, encoding='utf-8') as fp:
            for r in csv.DictReader(fp):
                base = os.path.splitext(r['segment_file'])[0]
                raw.append((float(r['wall_epoch']), base, float(r['video_pts_s'])))
    spans = {}                                          # base stem -> [(stem, start, end)]
    recon, t0 = [], {}                                  # reconciled rows + per-actual-stem min pts
    for w, base, ot in raw:
        segs = spans.get(base)
        if segs is None:
            segs = spans[base] = _seg_spans(session_dir, base)
        stem, s = segs[-1][0], segs[-1][1]
        for st, a, b in segs:
            if a <= ot < b:
                stem, s = st, a; break
        local = ot - s
        recon.append((w, stem, local)); t0[stem] = min(t0.get(stem, local), local)
    rows = [(w, s, round(p - t0[s], 3)) for (w, s, p) in recon]
    rows.sort()
    return rows


def resolve_media(session_dir, stem):
    """Prefer the converted .mp4, fall back to .ts."""
    for ext in ('.mp4', '.ts', '.flv'):
        p = os.path.join(session_dir, stem + ext)
        if os.path.exists(p):
            return p
    return os.path.join(session_dir, stem + '.mp4')


def data_to_video(T, timing):
    """wall epoch T -> (segment_stem, video_pts_s, in_gap) or None if outside coverage."""
    for i in range(len(timing) - 1):
        w0, s0, p0 = timing[i]
        w1, s1, p1 = timing[i + 1]
        if s0 == s1 and w0 <= T <= w1:
            frac = (T - w0) / (w1 - w0) if w1 > w0 else 0.0
            in_gap = (p1 - p0) < 0.05          # out_time frozen => this instant was lost
            return s0, round(p0 + frac * (p1 - p0), 3), in_gap
    return None


def wall_index(timing):
    """Build {segment_stem: sorted [(video_pts_s, wall_epoch)]} for video->wall lookups.
    (timing rows from load_timing() are already zero-based per segment.)"""
    idx = {}
    for w, s, p in timing:
        idx.setdefault(s, []).append((p, w))
    for s in idx:
        idx[s].sort()
    return idx


def video_to_wall(idx, stem, pts):
    """Inverse of data_to_video: (segment stem, zero-based video pts_s) -> wall epoch.
    Interpolates within the segment; extrapolates at ~real time past the sampled ends
    (used to place transcript sentences, whose audio comes from that segment)."""
    rows = idx.get(stem)
    if not rows:
        return None
    if pts <= rows[0][0]:
        return rows[0][1] - (rows[0][0] - pts)          # before first sample: 1:1 back-off
    for (p0, w0), (p1, w1) in zip(rows, rows[1:]):
        if p0 <= pts <= p1:
            frac = (pts - p0) / (p1 - p0) if p1 > p0 else 0.0
            return w0 + frac * (w1 - w0)
    pl, wl = rows[-1]
    return wl + (pts - pl)                              # past last sample: 1:1 forward


def parse_time(s):
    s = s.strip()
    for fmt in ('%Y-%m-%d %H:%M:%S.%f', '%Y-%m-%d %H:%M:%S'):
        try:
            return datetime.strptime(s, fmt).timestamp()
        except ValueError:
            pass
    # epoch fallback
    return float(s)


def tag_csv(session_dir, kind, timing):
    """Tag one data CSV. Returns (hit, total) or None if the source is missing."""
    src = os.path.join(session_dir, f'{kind}.csv')
    if not os.path.exists(src):
        return None
    out = os.path.join(session_dir, f'{kind}_aligned.csv')
    n = hit = 0
    with open(src, encoding='utf-8-sig') as fi, open(out, 'w', newline='', encoding='utf-8-sig') as fo:
        rd = csv.DictReader(fi)
        w = csv.writer(fo)
        w.writerow(rd.fieldnames + ['segment_file', 'video_pts_s', 'in_gap'])
        for row in rd:
            n += 1
            m = data_to_video(parse_time(row['time']), timing)
            if m:
                hit += 1
                seg = resolve_media(session_dir, m[0])
                extra = [os.path.basename(seg), m[1], m[2]]
            else:
                extra = ['', '', 'outside']
            w.writerow([row[k] for k in rd.fieldnames] + extra)
    return hit, n


# stats has no per-user 'time' string? it does ('time' col). Tag the standard streams.
_TAGGABLE = ('chat', 'like', 'social', 'stats', 'gift', 'lucky_bag', 'member', 'emoji')


def tag_all(session_dir, log=None):
    """Tag every data CSV present against the timing sidecar. Safe to call at
    session end: returns quietly if there's no timing file (e.g. record disabled).
    Returns dict {kind: (hit, total)}."""
    timing = load_timing(session_dir)
    if not timing:
        return {}
    out = {}
    for kind in _TAGGABLE:
        r = tag_csv(session_dir, kind, timing)
        if r:
            out[kind] = r
            if log:
                log(f"[对齐] {kind}: {r[0]}/{r[1]} 条已映射到视频 → {kind}_aligned.csv")
    return out


def cmd_tag(session_dir, kind):
    timing = load_timing(session_dir)
    if not timing:
        sys.exit(f"no timing_*.csv in {session_dir}")
    r = tag_csv(session_dir, kind, timing)
    if r is None:
        sys.exit(f"no {kind}.csv in {session_dir}")
    print(f"wrote {os.path.join(session_dir, kind+'_aligned.csv')}: "
          f"{r[0]}/{r[1]} rows mapped into video ({r[1]-r[0]} outside coverage)")


def cmd_at(session_dir, when):
    timing = load_timing(session_dir)
    m = data_to_video(parse_time(when), timing)
    if not m:
        sys.exit(f"{when} is outside recorded coverage")
    seg = resolve_media(session_dir, m[0])
    print(f"{when}  ->  {os.path.basename(seg)} @ {m[1]}s" + ("  [IN GAP]" if m[2] else ""))
    print(f"  ffmpeg -ss {m[1]} -i '{seg}' -frames:v 1 -q:v 2 frame.jpg")
    return seg, m[1]


def cmd_frame(session_dir, when, out='frame.jpg'):
    seg, pts = cmd_at(session_dir, when)
    r = subprocess.run(['ffmpeg', '-y', '-v', 'error', '-ss', str(pts),
                        '-i', seg, '-frames:v', '1', '-q:v', '2', out])
    if r.returncode == 0 and os.path.exists(out):
        print(f"saved {out}")
    else:
        sys.exit("frame extraction failed")


def _fmt_dur(s):
    s = int(s)
    return f"{s//60}m{s%60:02d}s"


def summarize_session(session_dir):
    """Return per-session health, or None if no timing sidecar."""
    rows = []
    for f in glob.glob(os.path.join(session_dir, 'timing_*.csv')):
        for r in csv.DictReader(open(f, encoding='utf-8')):
            rows.append((float(r['wall_epoch']), r['wall_iso'],
                         r['segment_file'], float(r['video_pts_s'])))
    if not rows:
        return None
    rows.sort()
    start = rows[0][0]

    # group into segments (by segment_file), keep wall order
    order, byseg = [], {}
    for we, wi, sf, pts in rows:
        if sf not in byseg:
            byseg[sf] = []; order.append(sf)
        byseg[sf].append((we, wi, pts))
    seg = [(sf, byseg[sf][0][0], byseg[sf][0][1], byseg[sf][-1][0],
            byseg[sf][0][2], byseg[sf][-1][2]) for sf in order]
    seg.sort(key=lambda s: s[1])

    breaks = []  # (dur_s, wall_iso, offset_s, kind)
    for a, b in zip(seg, seg[1:]):          # inter-segment breaks
        breaks.append((b[1] - a[3], b[2], a[3] - start, 'segment'))
    for sf in order:                        # intra-file freezes
        rs = byseg[sf]
        for x, y in zip(rs, rs[1:]):
            stall = (y[0] - x[0]) - (y[2] - x[2])
            if stall >= 2.0:
                breaks.append((stall, x[1], x[0] - start, 'freeze'))

    video = sum(s[5] - s[4] for s in seg)   # summed per-segment content duration
    wall = rows[-1][0] - start
    coverage = video / wall if wall > 0 else 1.0   # fraction of wall time that has video
    # flow = real-time keep-up on the longest (main) segment while connected:
    #   ~1.0 = recorder tracked real time; <1.0 = fell behind => bandwidth starvation
    main = max(seg, key=lambda s: s[5] - s[4])
    mw = main[3] - main[1]
    flow = (main[5] - main[4]) / mw if mw > 0 else 1.0

    aligned = {}
    for f in sorted(glob.glob(os.path.join(session_dir, '*_aligned.csv'))):
        kind = os.path.basename(f)[:-len('_aligned.csv')]
        tot = gap = out = 0
        for r in csv.DictReader(open(f, encoding='utf-8-sig')):
            tot += 1
            v = r.get('in_gap', '')
            if v == 'True': gap += 1
            elif v == 'outside': out += 1
        aligned[kind] = (tot, gap, out)
    # actual media file count: segment mode splits one base into _000/_001/... The metrics above
    # are (correctly) computed on the CONTINUOUS out_time timeline (all rows share the base stem);
    # only the reported file count needs the real number.
    n_files = sum(len(_seg_spans(session_dir, os.path.splitext(sf)[0])) for sf in order)
    return {'segs': seg, 'breaks': breaks, 'video': video, 'wall': wall, 'n_files': n_files,
            'flow': flow, 'coverage': coverage, 'main_wall': mw, 'aligned': aligned}


def discover_sessions(path):
    """A single session dir (has timing_*.csv), or every session under a parent tree."""
    if glob.glob(os.path.join(path, 'timing_*.csv')):
        return [path]
    return sorted({os.path.dirname(f)
                   for f in glob.glob(os.path.join(path, '**', 'timing_*.csv'), recursive=True)})


def report(sessions):
    """Print per-session detail + the fleet-wide bandwidth verdict.
    Returns a compact stats dict (for the nightly manifest), or {} if no sidecars."""
    FLOW_MIN = 0.95   # below this = recorder fell behind real time (bandwidth starvation)
    COVER_MIN = 0.97  # below this = missing video (breaks/offline), not necessarily bandwidth
    MIN_DUR = 30      # only trust flow on segments >= this many seconds (short = noisy)
    stats, nodata = [], []
    for sd in sessions:
        label = os.path.join(os.path.basename(os.path.dirname(sd)), os.path.basename(sd))
        s = summarize_session(sd)
        if not s:
            nodata.append(label); continue
        stats.append((label, s))
        print(f"\n{label}")
        print(f"  segments: {s.get('n_files', len(s['segs']))}   video {_fmt_dur(s['video'])} / wall {_fmt_dur(s['wall'])}"
              f"   flow {s['flow']:.2f}  cover {s['coverage']:.2f}")
        if s['breaks']:
            for dur, iso, off, kind in sorted(s['breaks'], key=lambda b: -b[0]):
                print(f"  break: {dur:.1f}s @ {_fmt_dur(off)} in ({iso}, {kind})")
        else:
            print("  break: none")
        if s['aligned']:
            parts = [f"{k} {t}(gap{g},out{o})" for k, (t, g, o) in s['aligned'].items()]
            print("  aligned: " + "  ".join(parts))

    # ── fleet-wide bandwidth verdict ──
    if not stats:
        print("\nno usable sidecars"); return {}
    covers = [s['coverage'] for _, s in stats]
    # flow is only meaningful over a long-enough connected window
    ratable = [(l, s['flow']) for l, s in stats if s['main_wall'] >= MIN_DUR]
    short = len(stats) - len(ratable)
    flows = [f for _, f in ratable]
    bw = sorted([(l, f) for l, f in ratable if f < FLOW_MIN], key=lambda x: x[1])   # bandwidth suspects
    brk = sorted([(l, s['coverage']) for l, s in stats
                  if s['coverage'] < COVER_MIN and s['flow'] >= FLOW_MIN], key=lambda x: x[1])
    mean = lambda xs: sum(xs) / len(xs) if xs else 1.0
    print("\n" + "=" * 64)
    print(f"FLEET SUMMARY — {len(stats)} rooms" + (f" (+{len(nodata)} no-data)" if nodata else ""))
    if flows:
        print(f"  flow  (real-time keep-up, {len(flows)} rooms ≥{MIN_DUR}s): mean {mean(flows):.3f}  min {min(flows):.3f}")
    print(f"  cover (video / wall):      mean {mean(covers):.3f}  min {min(covers):.3f}")
    # bandwidth is a SHARED constraint: real starvation hits several rooms at once.
    if len(bw) >= 2:
        print(f"  ⚠ BANDWIDTH-LIMITED — {len(bw)} rooms below real time (flow<{FLOW_MIN}): "
              + ", ".join(f"{l.split('/')[0]} {f:.2f}" for l, f in bw))
        verdict = "BANDWIDTH is the constraint — multiple rooms fell behind real time."
    elif len(bw) == 1:
        print(f"  1 isolated room below real time: {bw[0][0].split('/')[0]} {bw[0][1]:.2f} "
              f"(room-specific, not shared bandwidth)")
        verdict = "bandwidth OK; one isolated room dip (stream-side, not the shared pipe)."
    else:
        print(f"  ✓ no bandwidth starvation — recorder tracked real time on every rated room (flow≥{FLOW_MIN})")
        verdict = "bandwidth OK; any missing video is reconnects/offline, not bandwidth."
    if brk:
        print("  breaks/offline (low cover, flow ok): "
              + ", ".join(f"{l.split('/')[0]} {c:.2f}" for l, c in brk[:8]))
    if nodata:
        print("  no-data (barely started / offline): " + ", ".join(l.split('/')[0] for l in nodata[:8]))
    if short:
        print(f"  ({short} rooms too short (<{MIN_DUR}s) to rate flow)")
    print("  verdict: " + verdict)
    # per-room flow, descending (short recordings marked — flow is noisy under MIN_DUR)
    print(f"\n  flow per room (desc):")
    for l, s in sorted(stats, key=lambda ls: -ls[1]['flow']):
        tag = "" if s['main_wall'] >= MIN_DUR else "  (short)"
        print(f"    {s['flow']:.3f}  cover {s['coverage']:.2f}  {_fmt_dur(s['main_wall']):>6}  {l.split('/')[0]}{tag}")
    print("=" * 64)

    return {
        'rooms': len(stats), 'no_data': len(nodata), 'short': short,
        'flow_mean': round(mean(flows), 4) if flows else None,
        'flow_min': round(min(flows), 4) if flows else None,
        'cover_mean': round(mean(covers), 4), 'cover_min': round(min(covers), 4),
        'bandwidth_limited': len(bw) >= 2,
        'suspects': [{'room': l.split('/')[0], 'flow': round(f, 3)} for l, f in bw],
        'verdict': verdict,
    }


# ───────────────────────── #2 thorough check: payload completeness ─────────────────────────
# report() above checks the PIPE (did the recorder keep up with real time?). The completeness
# layer below checks the PAYLOAD (does the recorded video actually cover the audience timeline?):
# a room can pass the bandwidth verdict yet still have chat/like/social events during a window
# with no video (mid-session reconnect, offline blip, late start / early cut).

def _csv_times(path):
    """Sorted list of wall epochs from a data csv's 'time' column ([] if absent)."""
    if not os.path.exists(path):
        return []
    ts = []
    with open(path, encoding='utf-8-sig') as f:
        for r in csv.DictReader(f):
            v = r.get('time')
            if not v:
                continue
            try:
                ts.append(parse_time(v))
            except Exception:
                pass
    ts.sort()
    return ts


def _union_len(intervals, lo, hi):
    """Length of union(intervals) clipped to [lo,hi]; plus (first_start, last_end) of the
    clipped cover, or (None,None) if nothing overlaps."""
    clip = sorted((max(a, lo), min(b, hi)) for a, b in intervals if min(b, hi) > max(a, lo))
    if not clip:
        return 0.0, None, None
    total = 0.0
    cur_s, cur_e = clip[0]
    for a, b in clip[1:]:
        if a > cur_e:
            total += cur_e - cur_s; cur_s, cur_e = a, b
        else:
            cur_e = max(cur_e, b)
    total += cur_e - cur_s
    return total, clip[0][0], cur_e


def completeness_session(session_dir, s):
    """Payload-coverage metrics for one session, given its summarize_session() dict `s`.

    Two independent views of 'did we miss anything':
      • event view  — audience rows tagged 'outside' (no covering video) in *_aligned.csv
      • time view   — wall seconds inside the chat span with no video (head/inner/tail gaps)
    Plus an alignment-sanity check: every data csv present must have produced *_aligned.csv.
    """
    intervals = [(seg[1], seg[3]) for seg in s['segs']]   # (wall_start, wall_end) per segment

    # ── event view: roll up discrete audience streams (exclude stats = periodic snapshots) ──
    ev_tot = ev_out = ev_gap = 0
    for kind, (t, g, o) in s['aligned'].items():
        if kind == 'stats':
            continue
        ev_tot += t; ev_gap += g; ev_out += o
    ev_cov = (ev_tot - ev_out) / ev_tot if ev_tot else None

    # ── time view: chat timeline is the audience-activity reference (fall back if no chat) ──
    chat = _csv_times(os.path.join(session_dir, 'chat.csv'))
    if not chat:
        for k in ('like', 'social', 'gift', 'member'):
            chat = _csv_times(os.path.join(session_dir, k + '.csv'))
            if chat:
                break
    cstart = cend = cspan = uncovered = head = tail = inner = time_cov = None
    if chat and intervals:
        cstart, cend = chat[0], chat[-1]
        cspan = cend - cstart
        covered, fs, le = _union_len(intervals, cstart, cend)
        uncovered = max(0.0, cspan - covered)
        head = max(0.0, fs - cstart) if fs is not None else cspan     # chat before any video
        tail = max(0.0, cend - le) if le is not None else 0.0         # chat after last video
        inner = max(0.0, uncovered - head - tail)                     # holes between segments
        time_cov = covered / cspan if cspan > 0 else 1.0

    # ── alignment sanity: data present but not tagged (silent align failure) ──
    missing = [k for k in _TAGGABLE
               if os.path.exists(os.path.join(session_dir, k + '.csv'))
               and not os.path.exists(os.path.join(session_dir, k + '_aligned.csv'))]

    return dict(events=ev_tot, ev_outside=ev_out, ev_in_gap=ev_gap, ev_cov=ev_cov,
                chat_span=cspan, uncovered_s=uncovered, head_gap=head, tail_gap=tail,
                inner_gap=inner, time_cov=time_cov, missing_aligned=missing,
                cstart=cstart, cend=cend,
                vstart=intervals[0][0] if intervals else None,
                vend=max((b for _, b in intervals), default=None))


def completeness(sessions):
    """Print the per-room + fleet completeness verdict; return a compact dict for the manifest."""
    GAP_MIN = 5.0        # uncovered seconds below this = effectively complete (rounding/jitter)
    EVCOV_MIN = 0.99     # >=99% of audience events must map into video to count as 'complete'
    rows = []
    for sd in sessions:
        s = summarize_session(sd)
        if not s:
            continue
        label = os.path.basename(sd)
        rows.append((label, completeness_session(sd, s)))
    if not rows:
        return {}

    def complete(c):
        return ((c['uncovered_s'] is None or c['uncovered_s'] < GAP_MIN)
                and (c['ev_cov'] is None or c['ev_cov'] >= EVCOV_MIN)
                and not c['missing_aligned'])

    ncomplete = sum(complete(c) for _, c in rows)
    gappy = sorted((r for r in rows if not complete(r[1])),
                   key=lambda r: -((r[1]['uncovered_s'] or 0) + (1 - (r[1]['ev_cov'] or 1)) * 1e4))
    unc_tot = sum(c['uncovered_s'] or 0 for _, c in rows)
    ev_tot = sum(c['events'] for _, c in rows)
    ev_out = sum(c['ev_outside'] for _, c in rows)
    align_fail = [l for l, c in rows if c['missing_aligned']]
    opening = sorted(((l, c['head_gap']) for l, c in rows if (c['head_gap'] or 0) >= GAP_MIN),
                     key=lambda x: -x[1])

    print("\n" + "=" * 64)
    print(f"COMPLETENESS (payload coverage) — {len(rows)} rooms")
    print(f"  {ncomplete}/{len(rows)} rooms fully covered "
          f"(uncovered <{int(GAP_MIN)}s & events≥{EVCOV_MIN:.0%})")
    print(f"  uncovered audience-timeline: {_fmt_dur(unc_tot)} total across all rooms")
    if ev_tot:
        print(f"  audience events with no video: {ev_out}/{ev_tot} ({ev_out/ev_tot:.2%})")
    if gappy:
        print("  rooms with gaps (uncovered = head/inner/tail | events out/total):")
        for l, c in gappy[:12]:
            u = _fmt_dur(c['uncovered_s']) if c['uncovered_s'] is not None else "?"
            hit = (f"{_fmt_dur(c['head_gap'])}/{_fmt_dur(c['inner_gap'])}/{_fmt_dur(c['tail_gap'])}"
                   if c['uncovered_s'] is not None else "n/a")
            ev = f"{c['ev_outside']}/{c['events']}" if c['events'] else "no-events"
            mark = "  ⚠ANLGN" if c['missing_aligned'] else ""
            print(f"    {u:>7}  ({hit})  events {ev}  {l}{mark}")
    if opening:
        print("  ⚠ opening missed (chat before video starts): "
              + ", ".join(f"{l} {_fmt_dur(g)}" for l, g in opening[:8]))
    if align_fail:
        print("  ⚠ ALIGNMENT FAILURES (data present, not tagged): " + ", ".join(align_fail[:8]))
    else:
        print("  ✓ alignment: all streams processed (no tagging failures)")
    print("=" * 64)

    return {
        'rooms': len(rows), 'rooms_complete': ncomplete,
        'rooms_with_gaps': len(rows) - ncomplete,
        'uncovered_s_total': round(unc_tot, 1),
        'events_total': ev_tot, 'events_uncovered': ev_out,
        'event_cov': round((ev_tot - ev_out) / ev_tot, 4) if ev_tot else None,
        'align_failures': align_fail,
        'worst': [{'room': l, 'uncovered_s': round(c['uncovered_s'], 1) if c['uncovered_s'] is not None else None,
                   'head_s': round(c['head_gap'], 1) if c['head_gap'] is not None else None,
                   'inner_s': round(c['inner_gap'], 1) if c['inner_gap'] is not None else None,
                   'tail_s': round(c['tail_gap'], 1) if c['tail_gap'] is not None else None,
                   'ev_cov': round(c['ev_cov'], 4) if c['ev_cov'] is not None else None}
                  for l, c in gappy[:12]],
    }


def check(sessions):
    """Full post-run check: bandwidth verdict (pipe) + completeness verdict (payload).
    Tags each session first (idempotent) so completeness never mistakes 'not yet tagged'
    for a real alignment failure. Returns {'bandwidth':..., 'completeness':...} for the manifest."""
    for sd in sessions:
        try:
            tag_all(sd)
        except Exception:
            pass
    return {'bandwidth': report(sessions), 'completeness': completeness(sessions)}


def cmd_summary(path):
    sessions = discover_sessions(path)
    if not sessions:
        sys.exit(f"no timing_*.csv found under {path}")
    report(sessions)


def cmd_check(path):
    sessions = discover_sessions(path)
    if not sessions:
        sys.exit(f"no timing_*.csv found under {path}")
    check(sessions)


if __name__ == '__main__':
    if len(sys.argv) < 3:
        print(__doc__); sys.exit(1)
    action, sess = sys.argv[1], sys.argv[2]
    if action == 'tag':
        cmd_tag(sess, sys.argv[3] if len(sys.argv) > 3 else 'chat')
    elif action == 'at':
        cmd_at(sess, sys.argv[3])
    elif action == 'frame':
        cmd_frame(sess, sys.argv[3], sys.argv[4] if len(sys.argv) > 4 else 'frame.jpg')
    elif action == 'summary':
        cmd_summary(sess)
    elif action == 'check':
        cmd_check(sess)
    else:
        print(__doc__); sys.exit(1)
