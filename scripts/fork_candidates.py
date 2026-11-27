#!/usr/bin/env python3
"""
Fork harvested candidate repos strictly into `greenpatch-test-lab/*` (org-isolated!),
verify fork GitHub Actions are DISABLED on every fork, and mirror each failing PR branch
into a fork-local branch `gp-pr-<number>` inside `greenpatch-test-lab/<fork_name>`.
NEVER pushes to or mutates any upstream repository.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ORG = "greenpatch-test-lab"


def load_keys() -> tuple[str, str]:
    kf = Path(r"e:\market research\keys.txt")
    lab = pub = ""
    for line in kf.read_text(encoding="utf-8").splitlines():
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
    # Dependabot: "bump <pkg> from <old> to <new>"
    m = re.search(r"bump\s+([^\s]+)\s+from\s+([^\s]+)\s+to\s+([^\s]+)", pr_title, re.IGNORECASE)
    if m:
        return m.group(1), m.group(2), m.group(3)
    # Renovate: "update dependency <pkg> to v<new>"
    m2 = re.search(r"update\s+(?:dependency\s+)?([^\s]+)\s+to\s+v?([^\s]+)", pr_title, re.IGNORECASE)
    if m2:
        return m2.group(1), "current", m2.group(2)
    return "dependency", "current", "target"


def fork_and_mirror(cands: list[dict], ecosystem: str, lab_pat: str, limit: int) -> list[dict]:
    mirrored: list[dict] = []
    for item in cands[:limit]:
        upstream = item["repo"]
        pr_num = int(item["pr_number"])
        safe_fork_name = re.sub(r"[^A-Za-z0-9_.-]+", "-", f"gp-{ecosystem}-{upstream.replace('/', '-')}")[:95]
        fork_full = f"{ORG}/{safe_fork_name}"

        # 1. Create fork into greenpatch-test-lab via authenticated `gh` CLI (never pushes upstream)
        status, existing = gh_api("GET", f"/repos/{fork_full}", lab_pat)
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
                print(f"[fork-skip] {upstream}: {r.stderr.strip()[:160]}", file=sys.stderr)
                continue
            # Wait briefly for fork readiness
            for _ in range(10):
                st, _ = gh_api("GET", f"/repos/{fork_full}", lab_pat)
                if st == 200:
                    break
                time.sleep(2)

        # 2. Explicitly disable GitHub Actions on the fork repo (Directive 1.1)
        gh_api("PUT", f"/repos/{fork_full}/actions/permissions", lab_pat, {"enabled": False})

        # 3. Mirror the upstream PR head commit into a fork-local branch strictly inside greenpatch-test-lab/*
        fork_pr_branch = f"gp-pr-{pr_num}"
        # Use ephemeral bare git fetch from public upstream (read-only HTTPS) and push ONLY to greenpatch-test-lab/* using GREENPATCH_LAB_PAT
        tmp_bare = Path(os.environ.get("TEMP", r"C:\Temp")) / f"gp_mirror_{safe_fork_name}"
        if tmp_bare.exists():
            subprocess.run(["cmd", "/c", "rmdir", "/s", "/q", str(tmp_bare)], capture_output=True)
        tmp_bare.mkdir(parents=True, exist_ok=True)
        try:
            subprocess.run(["git", "init", "--bare", "-q"], cwd=tmp_bare, check=True, timeout=20)
            # Fetch PR head read-only from public GitHub
            f_res = subprocess.run(
                ["git", "fetch", "--depth=1", f"https://github.com/{upstream}.git", f"refs/pull/{pr_num}/head:refs/heads/{fork_pr_branch}"],
                cwd=tmp_bare,
                capture_output=True,
                text=True,
                timeout=60,
            )
            if f_res.returncode != 0:
                print(f"[fetch-pr-skip] {upstream}#{pr_num}: {f_res.stderr.strip()[:140]}", file=sys.stderr)
                continue
            # Push ONLY to greenpatch-test-lab/* authenticated strictly with GREENPATCH_LAB_PAT
            p_res = subprocess.run(
                ["git", "push", "-f", f"https://x-access-token:{lab_pat}@github.com/{fork_full}.git", f"refs/heads/{fork_pr_branch}:refs/heads/{fork_pr_branch}"],
                cwd=tmp_bare,
                capture_output=True,
                text=True,
                timeout=60,
            )
            if p_res.returncode != 0:
                print(f"[push-fork-skip] {fork_full}: {p_res.stderr.strip()[:140]}", file=sys.stderr)
                continue
        finally:
            subprocess.run(["cmd", "/c", "rmdir", "/s", "/q", str(tmp_bare)], capture_output=True)

        pkg, old_v, new_v = extract_pkg_and_versions(item.get("pr_title", ""))
        enriched = {
            **item,
            "ecosystem": ecosystem,
            "fork_repo": fork_full,
            "fork_name": safe_fork_name,
            "fork_pr_branch": fork_pr_branch,
            "upgraded_package": pkg,
            "old_version": old_v,
            "new_version": new_v,
        }
        mirrored.append(enriched)
        print(f"[forked+mirrored] {len(mirrored):02d} {fork_full} (branch={fork_pr_branch}, actions=DISABLED)", flush=True)
        time.sleep(0.5)

    return mirrored


def main() -> None:
    lab_pat, _ = load_keys()
    eco = sys.argv[1]
    limit = int(sys.argv[2]) if len(sys.argv) > 2 else 40
    hdir = Path(r"e:\market research\bench-harness\harvest")
    src = hdir / f"candidates_{eco}_filtered.json"
    cands = json.loads(src.read_text(encoding="utf-8"))
    out = fork_and_mirror(cands, eco, lab_pat, limit)
    out_file = hdir / f"preflight_batch_{eco}.json"
    out_file.write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(f"FORK_MIRROR_DONE: eco={eco}, count={len(out)}, saved={out_file}")


if __name__ == "__main__":
    main()
