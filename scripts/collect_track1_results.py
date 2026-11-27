#!/usr/bin/env python3
"""
Downloads Track 1 GitHub Actions artifacts (`track1-ts-*` or `track1-py-*`) into
bench-harness/results/ts/ or bench-harness/results/py/ and runs analyze_track1.py.
"""
from __future__ import annotations

import json
import os
import re
import shutil
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


def main() -> None:
    if len(sys.argv) < 3:
        print("Usage: collect_track1_results.py <ts|py> <run_id_1> [run_id_2 ...]")
        raise SystemExit(1)

    eco = sys.argv[1]
    token = load_lab_pat()
    env = dict(os.environ)
    env["GH_TOKEN"] = token

    tmp_dl = ROOT / "results" / f"_dl_{eco}"
    dest = ROOT / "results" / eco
    dest.mkdir(parents=True, exist_ok=True)

    for run_id in sys.argv[2:]:
        d = tmp_dl / run_id
        d.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["gh", "run", "download", run_id, "--repo", "greenpatch-test-lab/bench-harness", "--dir", str(d)],
            env=env,
            check=False,
            timeout=180,
        )
        for fp in d.rglob("*.json"):
            shutil.copy2(fp, dest / fp.name)

    count = len(list(dest.glob("*.json")))
    print(f"COLLECTED: eco={eco}, files={count}, dir={dest}")
    subprocess.run([sys.executable, str(ROOT / "scripts" / "analyze_track1.py")], check=True)


if __name__ == "__main__":
    main()
