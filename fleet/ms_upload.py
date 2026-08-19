#!/usr/bin/env python3
"""v2 uploader — push a station's packed night to a ModelScope dataset via the HTTP API.

No git clone: pack.py writes a staging tree (video/DATE/STATION, text/DATE/STATION.tar.gz,
manifest/DATE/STATION.json); we upload that whole tree in ONE commit. The server serializes
commits with a lock; on '429 commit lock busy' we retry with backoff. Then verify files landed.

Usage:
  ms_upload.py --repo-id SISU_DynCogLab/douyin --staging <dir> --station st01 --date 20260818 \
               [--token T | --token-from /path/to/any/clone] [--retries 8]
"""
import argparse, os, re, subprocess, sys, time, random

ap = argparse.ArgumentParser()
ap.add_argument("--repo-id", required=True)
ap.add_argument("--staging", required=True, help="local dir whose tree mirrors repo paths")
ap.add_argument("--station", required=True)
ap.add_argument("--date", required=True)
ap.add_argument("--token"); ap.add_argument("--token-from")
ap.add_argument("--retries", type=int, default=8)
a = ap.parse_args()

tok = a.token or os.environ.get("MODELSCOPE_API_TOKEN")
if not tok and a.token_from:
    try:
        url = subprocess.check_output(["git","-C",a.token_from,"remote","get-url","origin"], text=True).strip()
        m = re.search(r'oauth2:([^@]+)@', url); tok = m.group(1) if m else None
    except Exception: pass
if not tok: sys.exit("no token (use --token / MODELSCOPE_API_TOKEN / --token-from)")

from modelscope.hub.api import HubApi
api = HubApi(); api.login(tok)

expect = []
for r, _, fs in os.walk(a.staging):
    for f in fs:
        rel = os.path.relpath(os.path.join(r, f), a.staging)
        if rel.startswith('.') or '/.' in rel:   # skip .ms_upload_cache and other dotfiles
            continue
        expect.append(rel)
expect.sort()
print(f"[upload] {a.station} {a.date}: {len(expect)} files from {a.staging}", flush=True)
t0 = time.time()
for attempt in range(a.retries):
    try:
        api.upload_folder(repo_id=a.repo_id, folder_path=a.staging,  # path_in_repo="" -> repo root
                          repo_type="dataset", commit_message=f"{a.station} {a.date}",
                          disable_tqdm=True)
        break
    except Exception as e:
        msg = str(e); retryable = ('429' in msg) or ('commit lock' in msg) or ('RateLimit' in type(e).__name__)
        if retryable and attempt < a.retries-1:
            w = min(2**attempt, 8) * (0.5 + random.random()); print(f"[upload] 429 commit-lock, retry {attempt+1} in {w:.1f}s", flush=True); time.sleep(w); continue
        print(f"[upload] FAILED: {type(e).__name__}: {msg[:200]}"); sys.exit(1)
print(f"[upload] committed in {time.time()-t0:.0f}s; verifying ...", flush=True)

# List only the {type}/{date} subtrees we just uploaded, PAGINATED. get_dataset_files defaults to
# page_size=100 / page_number=1, so a naive single call silently misses files once the dataset
# grows past 100 -> false "VERIFY FAILED". Scope by root_path (small subtrees) and page to the end.
prefixes = sorted({"/".join(p.split("/")[:2]) for p in expect})   # video/DATE, audio/DATE, text/DATE, manifest/DATE
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
                remote.add(p)
        if len(fs) < 100:
            break
        page += 1
missing = [p for p in expect if p not in remote]
if missing:
    print(f"[upload] VERIFY FAILED — {len(missing)} missing:"); [print("   ", m) for m in missing[:10]]; sys.exit(1)
print(f"[upload] VERIFIED — {len(expect)} files present on {a.repo_id}")
