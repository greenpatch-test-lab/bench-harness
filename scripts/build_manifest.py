#!/usr/bin/env python3
"""
Downloads Day-1 Pre-Flight GitHub Actions artifacts from `greenpatch-test-lab/bench-harness`,
filters strictly to `clean_baseline_eligible == True` (main exits 0 offline, PR fails offline),
and locks N=25 TS + N=25 Python = 50 clean-baseline failing PRs into `bench-harness/manifest.json`.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(r"e:\market research\bench-harness")


def load_lab_pat() -> str:
    kf = Path(r"e:\market research\keys.txt")
    for line in kf.read_text(encoding="utf-8-sig").splitlines():
        m = re.match(r"^GREENPATCH_LAB_PAT\s+(\S+)", line.strip())
        if m:
            return m.group(1)
    raise RuntimeError("GREENPATCH_LAB_PAT not found")


def download_run_artifacts(run_id: str, dest: Path, token: str) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env["GH_TOKEN"] = token
    r = subprocess.run(
        ["gh", "run", "download", run_id, "--repo", "greenpatch-test-lab/bench-harness", "--dir", str(dest)],
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )
    if r.returncode != 0:
        print(f"[download-warn] run {run_id}: {r.stderr.strip()}", file=sys.stderr)


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: build_manifest.py <run_id_1> [run_id_2 ...]")
        raise SystemExit(1)

    token = load_lab_pat()
    pf_dir = ROOT / "harvest" / "preflight_artifacts"
    for run_id in sys.argv[1:]:
        download_run_artifacts(run_id, pf_dir / run_id, token)

    # Load batch metadata keyed by (ecosystem, upstream_repo)
    meta_by_key: dict[tuple[str, str], dict] = {}
    for eco in ("ts", "py"):
        for bf in sorted((ROOT / "harvest").glob(f"preflight_batch_{eco}*.json")):
            for item in json.loads(bf.read_text(encoding="utf-8")):
                meta_by_key[(eco, item["repo"])] = item

    eligible_ts: list[dict] = []
    eligible_py: list[dict] = []
    seen: set[tuple[str, str]] = set()
    all_checked = 0

    for fp in sorted(pf_dir.rglob("*.json")):
        rec = json.loads(fp.read_text(encoding="utf-8"))
        eco = rec.get("ecosystem")
        up = rec.get("upstream_repo")
        key = (eco, up)
        if key in seen:
            continue
        seen.add(key)
        all_checked += 1
        if rec.get("clean_baseline_eligible") is True and key in meta_by_key:
            m = meta_by_key[key]
            entry = {
                "upstream_repo": up,
                "fork_repo": m["fork_repo"],
                "fork_name": m["fork_name"],
                "ecosystem": eco,
                "stars": m["stars"],
                "license": m["license"],
                "lockfile": m["lockfile"],
                "default_branch": m["default_branch"],
                "pr_number": m["pr_number"],
                "pr_url": m["pr_url"],
                "pr_title": m["pr_title"],
                "pr_author": m["pr_author"],
                "fork_pr_branch": m["fork_pr_branch"],
                "upgraded_package": m["upgraded_package"],
                "old_version": m["old_version"],
                "new_version": m["new_version"],
                "preflight_main_exit": rec["main_exit_code"],
                "preflight_pr_exit": rec["pr_exit_code"],
                "verify_mode": rec.get("verify_mode", "test"),
            }
            if eco == "ts":
                eligible_ts.append(entry)
            elif eco == "py":
                eligible_py.append(entry)

    selected_ts = eligible_ts[:25]
    selected_py = eligible_py[:25]
    manifest = {
        "scenario": "A",
        "ultra_tier": "5x",
        "target_n": 50,
        "ts_count": len(selected_ts),
        "py_count": len(selected_py),
        "total_selected": len(selected_ts) + len(selected_py),
        "total_preflight_checked": all_checked,
        "total_eligible_ts": len(eligible_ts),
        "total_eligible_py": len(eligible_py),
        "repos": selected_ts + selected_py,
    }
    out = ROOT / "manifest.json"
    out.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in manifest.items() if k != "repos"}, indent=2))


if __name__ == "__main__":
    main()
