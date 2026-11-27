#!/usr/bin/env python3
"""
Pre-filtered Batch-2 Harvester targeting offline-testable TS & Python OSS libraries (stars 50..3000).
Pre-checks package.json (`scripts.test` present, no playwright/cypress/testcontainers/docker)
and pyproject.toml (compatible with Python 3.12, no cuda/torch/docker/android) BEFORE forking!
"""
from __future__ import annotations

import concurrent.futures
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

OSI_LICENSES = {
    "MIT", "Apache-2.0", "BSD-2-Clause", "BSD-3-Clause", "ISC",
    "MPL-2.0", "LGPL-2.1", "LGPL-3.0", "GPL-2.0", "GPL-3.0",
    "Unlicense", "0BSD", "CC0-1.0", "BSL-1.0", "EPL-2.0",
}

SKIP_TITLE_RE = re.compile(
    r"(github-actions|actions/|docker|dockerfile|helm|terraform|github/codeql|pre-commit|rust|cargo|go\.mod|maven|gradle|playwright|cypress)",
    re.IGNORECASE,
)

BAD_TS_TEST_RE = re.compile(r"(playwright|cypress|testcontainers|docker|e2e|no test specified|exit 1)", re.IGNORECASE)
BAD_PY_TOML_RE = re.compile(r"(requires-python\s*=\s*['\"]>=\s*3\.1[345]|requires-python\s*=\s*['\"]==\s*3\.1[345]|torch|cuda|tensorflow|testcontainers|playwright)", re.IGNORECASE)

PRINT_LOCK = threading.Lock()


def load_pub_pat() -> str:
    kf = Path(r"e:\market research\keys.txt")
    for line in kf.read_text(encoding="utf-8-sig").splitlines():
        m = re.match(r"^GH_PUBLIC_READ_PAT\s+(\S+)", line.strip())
        if m:
            return m.group(1)
    raise RuntimeError("GH_PUBLIC_READ_PAT not found")


def gh_get(path: str, token: str, params: dict | None = None):
    url = f"https://api.github.com{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "greenpatch-clean-harvester",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    for _ in range(2):
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code in (403, 429):
                time.sleep(5)
                continue
            return None
        except Exception:
            time.sleep(1)
    return None


def raw_get(repo: str, branch: str, filename: str) -> str | None:
    url = f"https://raw.githubusercontent.com/{repo}/{branch}/{filename}"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "gp-harv"}), timeout=10) as r:
            return r.read(120_000).decode("utf-8", errors="replace")
    except Exception:
        return None


def inspect_repo(repo: dict, language: str, token: str) -> dict | None:
    full_name = repo["full_name"]
    lic = (repo.get("license") or {}).get("spdx_id")
    if lic not in OSI_LICENSES:
        return None
    stars = int(repo.get("stargazers_count") or 0)
    if not (50 <= stars <= 3000):
        return None
    if int(repo.get("size") or 0) > 60_000:  # smaller, cleaner libraries
        return None

    branch = repo.get("default_branch", "main")
    prs = gh_get(f"/repos/{full_name}/pulls", token, {"state": "open", "per_page": 20, "sort": "updated", "direction": "desc"})
    if not prs or not isinstance(prs, list):
        return None
    bot_prs = [
        p for p in prs
        if (p.get("user") or {}).get("login") in ("dependabot[bot]", "renovate[bot]")
        and not SKIP_TITLE_RE.search(p.get("title") or "")
    ]
    if not bot_prs:
        return None

    contents = gh_get(f"/repos/{full_name}/contents", token)
    if not contents or not isinstance(contents, list):
        return None
    root_files = {c["name"] for c in contents if isinstance(c, dict) and "name" in c}

    if language == "TypeScript":
        if "package.json" not in root_files:
            return None
        lockfile = "package-lock.json" if "package-lock.json" in root_files else (
            "pnpm-lock.yaml" if "pnpm-lock.yaml" in root_files else None
        )
        if not lockfile:
            return None
        pkg_raw = raw_get(full_name, branch, "package.json")
        if not pkg_raw:
            return None
        try:
            pkg_j = json.loads(pkg_raw)
        except Exception:
            return None
        test_cmd = str((pkg_j.get("scripts") or {}).get("test") or "")
        if not test_cmd or BAD_TS_TEST_RE.search(test_cmd):
            return None
        # Skip monorepo workspaces that require complex multi-package builds before test
        if pkg_j.get("workspaces") or "pnpm-workspace.yaml" in root_files:
            return None
    else:
        if "uv.lock" in root_files:
            lockfile = "uv.lock"
        elif "poetry.lock" in root_files:
            lockfile = "poetry.lock"
        elif "pyproject.toml" in root_files and ("tests" in root_files or "test" in root_files):
            lockfile = "pyproject.toml"
        else:
            return None
        toml_raw = raw_get(full_name, branch, "pyproject.toml")
        if not toml_raw or BAD_PY_TOML_RE.search(toml_raw):
            return None

    for pr in bot_prs[:5]:
        head_sha = (pr.get("head") or {}).get("sha")
        if not head_sha:
            continue
        checks = gh_get(f"/repos/{full_name}/commits/{head_sha}/check-runs", token, {"per_page": 25})
        if not checks or not isinstance(checks, dict):
            continue
        failed = [r["name"] for r in (checks.get("check_runs") or []) if r.get("conclusion") in ("failure", "timed_out")]
        if not failed:
            continue
        files = gh_get(f"/repos/{full_name}/pulls/{pr['number']}/files", token, {"per_page": 15})
        if not files or not isinstance(files, list):
            continue
        changed = [f["filename"] for f in files]
        if language == "TypeScript":
            if not any(f in ("package.json", "package-lock.json", "pnpm-lock.yaml") for f in changed):
                continue
        else:
            if not any(f in ("pyproject.toml", "uv.lock", "poetry.lock", "requirements.txt") for f in changed):
                continue
        return {
            "repo": full_name,
            "language": language,
            "stars": stars,
            "license": lic,
            "default_branch": branch,
            "lockfile": lockfile,
            "pr_number": pr["number"],
            "pr_url": pr["html_url"],
            "pr_title": pr["title"],
            "pr_author": (pr.get("user") or {}).get("login"),
            "pr_head_ref": (pr.get("head") or {}).get("ref"),
            "pr_head_sha": head_sha,
            "pr_base_ref": (pr.get("base") or {}).get("ref"),
            "pr_base_sha": (pr.get("base") or {}).get("sha"),
            "failed_checks": failed[:5],
            "changed_files": changed[:10],
            "updated_at": pr.get("updated_at"),
        }
    return None


def harvest_batch2(token: str, language: str, already_seen: set[str], target: int, out_file: Path) -> list[dict]:
    results: list[dict] = []
    seen = set(already_seen)
    # Search across multiple star bands & pages 3..7 (distinct from Batch 1!)
    queries = [
        f"language:{language} stars:50..120 archived:false fork:false pushed:>2026-01-01",
        f"language:{language} stars:121..250 archived:false fork:false pushed:>2026-01-01",
        f"language:{language} stars:251..500 archived:false fork:false pushed:>2026-01-01",
        f"language:{language} stars:501..1200 archived:false fork:false pushed:>2026-01-01",
        f"language:{language} stars:1201..3000 archived:false fork:false pushed:>2026-01-01",
    ]
    for q in queries:
        if len(results) >= target:
            break
        repos_to_check = []
        for page in range(2, 6):
            data = gh_get("/search/repositories", token, {"q": q, "sort": "stars", "order": "desc", "per_page": 100, "page": page})
            time.sleep(2.1)
            if not data or not data.get("items"):
                break
            for r in data["items"]:
                if r["full_name"] not in seen:
                    seen.add(r["full_name"])
                    repos_to_check.append(r)

        with concurrent.futures.ThreadPoolExecutor(max_workers=14) as pool:
            futs = [pool.submit(inspect_repo, r, language, token) for r in repos_to_check]
            for fut in concurrent.futures.as_completed(futs):
                item = fut.result()
                if item:
                    with PRINT_LOCK:
                        if len(results) < target:
                            results.append(item)
                            out_file.write_text(json.dumps(results, indent=2), encoding="utf-8")
                            print(
                                f"[B2 {language}] #{len(results):02d} {item['repo']} (*{item['stars']}, {item['lockfile']}) "
                                f"PR #{item['pr_number']}: {item['pr_title'][:58]}",
                                flush=True,
                            )
                if len(results) >= target:
                    break
    return results


def main() -> None:
    token = load_pub_pat()
    hdir = Path(r"e:\market research\bench-harness\harvest")
    ts1 = json.loads((hdir / "candidates_ts_filtered.json").read_text(encoding="utf-8"))
    py1 = json.loads((hdir / "candidates_py_filtered.json").read_text(encoding="utf-8"))
    seen_ts = {x["repo"] for x in ts1}
    seen_py = {x["repo"] for x in py1}

    target = int(sys.argv[1]) if len(sys.argv) > 1 else 45
    print(f"=== Harvesting Clean Batch 2 TS (target {target}) ===", flush=True)
    ts2 = harvest_batch2(token, "TypeScript", seen_ts, target, hdir / "candidates_ts_batch2.json")
    print(f"\n=== Harvesting Clean Batch 2 Python (target {target}) ===", flush=True)
    py2 = harvest_batch2(token, "Python", seen_py, target, hdir / "candidates_py_batch2.json")
    print(f"\nBATCH2_HARVEST_DONE: ts2={len(ts2)}, py2={len(py2)}", flush=True)


if __name__ == "__main__":
    main()
