#!/usr/bin/env python3
"""v2 uploader — publish ONE session's shallow outbox to a ModelScope dataset (HTTP API, no clone).

pack.py writes a SHALLOW outbox (upload_staging/<session>/{video/*.tar, audio.tar, text.tar.gz,
manifest.json}). We map it to the endpoint layout — <type>/<session>/<station> — by HARDLINKING
each artifact into an ephemeral tree (no copying the multi-GB video), upload that tree in ONE
commit, verify, then remove the temp. The server serializes commits with a lock; on '429 commit
lock busy' we retry with backoff.

Endpoint published:  video/<session>/<station>/…   audio/<session>/<station>.tar
                     text/<session>/<station>.tar.gz   manifest/<session>/<station>.json

Usage:
  ms_upload.py --repo-id SISU_DynCogLab/douyin-dataset --staging upload_staging/<session> \
               --station st01 --session 20260818_2000 [--token T | --token-from /clone] [--retries 8]
"""
import argparse, os, re, shutil, subprocess, sys, tempfile, time, random

ap = argparse.ArgumentParser()
ap.add_argument("--repo-id", required=True)
ap.add_argument("--staging", required=True, help="the shallow outbox dir (upload_staging/<session>)")
ap.add_argument("--station", required=True)
ap.add_argument("--session", required=True, help="YYYYMMDD_HHMM")
ap.add_argument("--token"); ap.add_argument("--token-from")
ap.add_argument("--retries", type=int, default=8)
a = ap.parse_args()

tok = a.token or os.environ.get("MODELSCOPE_API_TOKEN")
if not tok and a.token_from:
    try:
        url = subprocess.check_output(["git", "-C", a.token_from, "remote", "get-url", "origin"], text=True).strip()
        m = re.search(r'oauth2:([^@]+)@', url); tok = m.group(1) if m else None
    except Exception: pass
if not tok: sys.exit("no token (use --token / MODELSCOPE_API_TOKEN / --token-from)")


def outbox_to_repo(outbox, session, station):
    """(local outbox file, endpoint-relative repo path) for each artifact present in the outbox."""
    pairs = []
    vdir = os.path.join(outbox, "video")
    if os.path.isdir(vdir):
        for f in sorted(os.listdir(vdir)):
            if f.endswith(".tar"):
                pairs.append((os.path.join(vdir, f), f"video/{session}/{station}/{f}"))
    for fname, repo in (("audio.tar", f"audio/{session}/{station}.tar"),
                        ("text.tar.gz", f"text/{session}/{station}.tar.gz"),
                        ("manifest.json", f"manifest/{session}/{station}.json")):
        p = os.path.join(outbox, fname)
        if os.path.exists(p):
            pairs.append((p, repo))
    return pairs


pairs = outbox_to_repo(a.staging, a.session, a.station)
if not pairs:
    sys.exit(f"[upload] nothing to upload in outbox {a.staging}")
expect = sorted(repo for _, repo in pairs)

# build the ephemeral <type>/<session>/<station> tree via hardlinks (same fs as the outbox -> no copy)
push = tempfile.mkdtemp(prefix=".push_", dir=os.path.dirname(os.path.abspath(a.staging)))
try:
    for src, repo in pairs:
        dst = os.path.join(push, repo)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        try:
            os.link(src, dst)                 # hardlink — no data copy
        except OSError:
            shutil.copy2(src, dst)            # cross-filesystem fallback

    from modelscope.hub.api import HubApi
    api = HubApi(); api.login(tok)

    print(f"[upload] {a.station} {a.session}: {len(expect)} files from {a.staging}", flush=True)
    t0 = time.time()
    for attempt in range(a.retries):
        try:
            api.upload_folder(repo_id=a.repo_id, folder_path=push,   # path_in_repo="" -> repo root
                              repo_type="dataset", commit_message=f"{a.station} {a.session}",
                              disable_tqdm=True)
            break
        except Exception as e:
            msg = str(e); retryable = ('429' in msg) or ('commit lock' in msg) or ('RateLimit' in type(e).__name__)
            if retryable and attempt < a.retries - 1:
                w = min(2 ** attempt, 8) * (0.5 + random.random())
                print(f"[upload] 429 commit-lock, retry {attempt+1} in {w:.1f}s", flush=True); time.sleep(w); continue
            print(f"[upload] FAILED: {type(e).__name__}: {msg[:200]}"); sys.exit(1)
    print(f"[upload] committed in {time.time()-t0:.0f}s; verifying ...", flush=True)

    # List only the {type}/{session} subtrees we just uploaded, PAGINATED (get_dataset_files
    # defaults to page_size=100/page_1, so a single call silently misses files once >100 -> false fail).
    prefixes = sorted({"/".join(p.split("/")[:2]) for p in expect})   # video/<session>, audio/<session>, ...
    remote = set()
    for pref in prefixes:
        page = 1
        while True:
            fs = api.get_dataset_files(repo_id=a.repo_id, revision="master", root_path=pref,
                                       recursive=True, page_size=100, page_number=page)
            if not fs:
                break
            for f in fs:
                p = f if isinstance(f, str) else (f.get('Path') or f.get('path'))
                if p:
                    remote.add(p.lstrip('/'))
            if len(fs) < 100:
                break
            page += 1
    missing = [p for p in expect if p not in remote]
    if missing:
        print(f"[upload] VERIFY FAILED — {len(missing)} missing:"); [print("   ", m) for m in missing[:10]]; sys.exit(1)
    print(f"[upload] VERIFIED — {len(expect)} files present on {a.repo_id}")
finally:
    shutil.rmtree(push, ignore_errors=True)
