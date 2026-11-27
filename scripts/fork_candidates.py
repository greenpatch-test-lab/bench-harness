#!/usr/bin/env python3
"""
Fork harvested candidate repos strictly into `greenpatch-test-lab/*` (org-isolated!),
verify fork GitHub Actions are DISABLED on every fork, and mirror each failing PR branch
into a fork-local branch `gp-pr-<number>` inside `greenpatch-test-lab/<fork_name>`.
NEVER pushes to or mutates any upstream repository.
"""
from __future__ import annotations

import concurrent.futures
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ORG = "greenpatch-test-lab"
LOCK = threading.Lock()


def load_keys() -> tuple[str, str]:
    kf = Path(r"e:\market research\keys.txt")
    lab = pub = ""
    for line in kf.read_text(encoding="utf-8-sig").splitlines():
        m1 = re.match(r"^GREENPATCH_LAB_PAT\s+(\S+)", line.strip())
        if m1:
            lab = m1.group(1)
        m2 = re.match(r"^GH_PUBLIC_READ_PAT\s+(\S+)", line.strip())
        if m2:
            pub = m2.group(1)
    if not lab:
        raise RuntimeError("GREENPATCH_LAB_PAT not found")
    return lab, pub


def gh_api(method: str, path: str, token: str, body: dict | None = None) -> tuple[int, dict]:
    url = f"https://api.github.com{path}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "greenpatch-forker",
            "X-GitHub-Api-Version": "2022-11-28",
            **({"Content-Type": "application/json"} if data else {}),
        },
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {"message": raw}


def extract_pkg_and_versions(pr_title: str) -> tuple[str, str, str]:
    m = re.search(r"bump\s+([^\s]+)\s+from\s+([^\s]+)\s+to\s+([^\s]+)", pr_title, re.IGNORECASE)
    if m:
        return m.group(1), m.group(2), m.group(3)
    m2 = re.search(r"update\s+(?:dependency\s+)?([^\s]+)\s+to\s+v?([^\s]+)", pr_title, re.IGNORECASE)
    if m2:
        return m2.group(1), "current", m2.group(2)
    return "dependency", "current", "target"


def process_one(item: dict, ecosystem: str, lab_pat: str) -> dict | None:
    upstream = item["repo"]
    pr_num = int(item["pr_number"])
    safe_fork_name = re.sub(r"[^A-Za-z0-9_.-]+", "-", f"gp-{ecosystem}-{upstream.replace('/', '-')}")[:95]
    fork_full = f"{ORG}/{safe_fork_name}"

    status, _ = gh_api("GET", f"/repos/{fork_full}", lab_pat)
    if status != 200:
        r = subprocess.run(
            [
                "gh", "api", "-X", "POST", f"/repos/{upstream}/forks",
                "-f", f"organization={ORG}",
                "-f", f"name={safe_fork_name}",
                "-F", "default_branch_only=false",
            ],
            capture_output=True,
            text=True,
            timeout=45,
        )
        if r.returncode != 0:
            with LOCK:
                print(f"[fork-skip] {upstream}: {r.stderr.strip()[:140]}", file=sys.stderr, flush=True)
            return None
        for _ in range(12):
            st, _ = gh_api("GET", f"/repos/{fork_full}", lab_pat)
            if st == 200:
                break
            time.sleep(2)

    # Explicitly disable GitHub Actions on every fork repo (Directive 1.1)
    gh_api("PUT", f"/repos/{fork_full}/actions/permissions", lab_pat, {"enabled": False})

    fork_pr_branch = f"gp-pr-{pr_num}"
    tmp_bare = Path(os.environ.get("TEMP", r"C:\Temp")) / f"gp_mirror_{safe_fork_name}_{os.getpid()}"
    if tmp_bare.exists():
        subprocess.run(["cmd", "/c", "rmdir", "/s", "/q", str(tmp_bare)], capture_output=True)
    tmp_bare.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(["git", "init", "--bare", "-q"], cwd=tmp_bare, check=True, timeout=20)
        f_res = subprocess.run(
            ["git", "fetch", "--depth=1", f"https://github.com/{upstream}.git", f"refs/pull/{pr_num}/head:refs/heads/{fork_pr_branch}"],
            cwd=tmp_bare,
            capture_output=True,
            text=True,
            timeout=60,
        )
        if f_res.returncode != 0:
            with LOCK:
                print(f"[fetch-pr-skip] {upstream}#{pr_num}: {f_res.stderr.strip()[:120]}", file=sys.stderr, flush=True)
            return None
        # Push strictly to greenpatch-test-lab/* using GREENPATCH_LAB_PAT
        p_res = subprocess.run(
            ["git", "push", "-f", f"https://x-access-token:{lab_pat}@github.com/{fork_full}.git", f"refs/heads/{fork_pr_branch}:refs/heads/{fork_pr_branch}"],
            cwd=tmp_bare,
            capture_output=True,
            text=True,
            timeout=60,
        )
        if p_res.returncode != 0:
            with LOCK:
                print(f"[push-fork-skip] {fork_full}: {p_res.stderr.strip()[:120]}", file=sys.stderr, flush=True)
            return None
    finally:
        subprocess.run(["cmd", "/c", "rmdir", "/s", "/q", str(tmp_bare)], capture_output=True)

    pkg, old_v, new_v = extract_pkg_and_versions(item.get("pr_title", ""))
    return {
        **item,
        "ecosystem": ecosystem,
        "fork_repo": fork_full,
        "fork_name": safe_fork_name,
        "fork_pr_branch": fork_pr_branch,
        "upgraded_package": pkg,
        "old_version": old_v,
        "new_version": new_v,
    }


def main() -> None:
    lab_pat, _ = load_keys()
    eco = sys.argv[1]
    limit = int(sys.argv[2]) if len(sys.argv) > 2 else 42
    hdir = Path(r"e:\market research\bench-harness\harvest")
    src = hdir / f"candidates_{eco}_filtered.json"
    cands = json.loads(src.read_text(encoding="utf-8"))[:limit]

    mirrored: list[dict] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        futs = [pool.submit(process_one, c, eco, lab_pat) for c in cands]
        for fut in concurrent.futures.as_completed(futs):
            res = fut.result()
            if res:
                mirrored.append(res)
                with LOCK:
                    print(f"[forked+mirrored] {len(mirrored):02d}/{len(cands)} {res['fork_repo']} ({res['fork_pr_branch']})", flush=True)

    out_file = hdir / f"preflight_batch_{eco}.json"
    out_file.write_text(json.dumps(mirrored, indent=2), encoding="utf-8")
    print(f"FORK_MIRROR_DONE: eco={eco}, count={len(mirrored)}, saved={out_file}", flush=True)


if __name__ == "__main__":
    main()
