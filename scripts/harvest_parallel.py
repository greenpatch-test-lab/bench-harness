#!/usr/bin/env python3
"""
High-Throughput Parallel REST Harvester for Track 1 (Volume 10 §2.2 / Volume 11 Day 1)
- Uses GH_PUBLIC_READ_PAT (5,000 req/hr) with 12 worker threads (zero GraphQL 502s!).
- Scans active 50..3000-star OSI-licensed TypeScript & Python repos for open failing
  Dependabot / Renovate dependency-upgrade PRs with real lockfiles/manifests & tests.
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
    r"(github-actions|actions/|docker|dockerfile|helm|terraform|github/codeql|pre-commit|rust|cargo|go\.mod|maven|gradle)",
    re.IGNORECASE,
)

PRINT_LOCK = threading.Lock()


def load_pub_pat() -> str:
    env_pat = os.environ.get("GH_PUBLIC_READ_PAT")
    if env_pat:
        return env_pat.strip()
    kf = Path(r"e:\market research\keys.txt")
    for line in kf.read_text(encoding="utf-8").splitlines():
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
            "User-Agent": "greenpatch-parallel-harvester",
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


def inspect_repo(repo: dict, language: str, token: str) -> dict | None:
    full_name = repo["full_name"]
    lic = (repo.get("license") or {}).get("spdx_id")
    if lic not in OSI_LICENSES:
        return None
    stars = int(repo.get("stargazers_count") or 0)
    if not (50 <= stars <= 3000):
        return None
    if int(repo.get("size") or 0) > 120_000:
        return None

    prs = gh_get(
        f"/repos/{full_name}/pulls",
        token,
        {"state": "open", "per_page": 25, "sort": "updated", "direction": "desc"},
    )
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
    else:
        if "uv.lock" in root_files:
            lockfile = "uv.lock"
        elif "poetry.lock" in root_files:
            lockfile = "poetry.lock"
        elif "pyproject.toml" in root_files and ("tests" in root_files or "test" in root_files):
            lockfile = "pyproject.toml"
        else:
            return None

    for pr in bot_prs[:5]:
        head_sha = (pr.get("head") or {}).get("sha")
        if not head_sha:
            continue
        checks = gh_get(f"/repos/{full_name}/commits/{head_sha}/check-runs", token, {"per_page": 25})
        if not checks or not isinstance(checks, dict):
            continue
        runs = checks.get("check_runs") or []
        failed = [r["name"] for r in runs if r.get("conclusion") in ("failure", "timed_out")]
        if not failed:
            continue

        files = gh_get(f"/repos/{full_name}/pulls/{pr['number']}/files", token, {"per_page": 15})
        if not files or not isinstance(files, list):
            continue
        changed = [f["filename"] for f in files]
        if language == "TypeScript":
            if not any(f.endswith(("package.json", "package-lock.json", "pnpm-lock.yaml")) for f in changed):
                continue
        else:
            if not any(f.endswith(("pyproject.toml", "uv.lock", "poetry.lock", "requirements.txt")) for f in changed):
                continue

        return {
            "repo": full_name,
            "language": language,
            "stars": stars,
            "license": lic,
            "default_branch": repo.get("default_branch", "main"),
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


def harvest_language(token: str, language: str, existing: list[dict], target: int, out_file: Path, pages: range = range(1, 3), pushed_after: str = "2026-06-01") -> list[dict]:
    results = list(existing)
    seen = {x["repo"] for x in results}

    bands = [
        "50..100", "101..180", "181..300", "301..500",
        "501..850", "851..1400", "1401..2100", "2101..3000",
    ]

    for band in bands:
        if len(results) >= target:
            break
        repos_to_check = []
        for page in pages:
            q = f"language:{language} stars:{band} archived:false fork:false pushed:>{pushed_after}"
            data = gh_get(
                "/search/repositories",
                token,
                {"q": q, "sort": "updated", "order": "desc", "per_page": 100, "page": page},
            )
            time.sleep(2.1)
            if not data or not data.get("items"):
                break
            for r in data["items"]:
                if r["full_name"] not in seen:
                    seen.add(r["full_name"])
                    repos_to_check.append(r)

        if not repos_to_check:
            continue

        with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
            futs = [pool.submit(inspect_repo, r, language, token) for r in repos_to_check]
            for fut in concurrent.futures.as_completed(futs):
                item = fut.result()
                if item:
                    with PRINT_LOCK:
                        if len(results) < target:
                            results.append(item)
                            out_file.write_text(json.dumps(results, indent=2), encoding="utf-8")
                            print(
                                f"[{language}] #{len(results):02d} {item['repo']} (*{item['stars']}, {item['lockfile']}) "
                                f"PR #{item['pr_number']} ({item['pr_author']}): {item['pr_title'][:60]}",
                                flush=True,
                            )
                if len(results) >= target:
                    break

    return results


def main() -> None:
    token = load_pub_pat()
    out_dir = Path(r"e:\market research\bench-harness\harvest")
    out_dir.mkdir(parents=True, exist_ok=True)
    target = int(sys.argv[1]) if len(sys.argv) > 1 else 42
    only_py = "--only-py" in sys.argv

    ts_file = out_dir / "candidates_ts_filtered.json"
    py_file = out_dir / "candidates_py_filtered.json"
    existing_ts = json.loads(ts_file.read_text(encoding="utf-8")) if ts_file.is_file() else []
    existing_py = json.loads(py_file.read_text(encoding="utf-8")) if py_file.is_file() else []

    if not only_py:
        print(f"=== Parallel Harvesting TS (start={len(existing_ts)}, target={target}) ===", flush=True)
        ts_final = harvest_language(token, "TypeScript", existing_ts, target, ts_file)
    else:
        ts_final = existing_ts

    print(f"\n=== Parallel Harvesting Python (start={len(existing_py)}, target={target}) ===", flush=True)
    pages = range(3, 6) if only_py else range(1, 3)
    pushed_after = "2026-01-01" if only_py else "2026-06-01"
    py_final = harvest_language(token, "Python", existing_py, target, py_file, pages=pages, pushed_after=pushed_after)

    print(f"\nPARALLEL_HARVEST_DONE: ts={len(ts_final)}, py={len(py_final)}, total={len(ts_final)+len(py_final)}", flush=True)


if __name__ == "__main__":
    main()
