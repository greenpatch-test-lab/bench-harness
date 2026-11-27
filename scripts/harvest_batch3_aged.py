#!/usr/bin/env python3
"""
Batch-3 Aged Clean-Library Harvester (Volume 11 Day 1)
Key empirical optimizations from Batch 1 & 2 pre-flight telemetry:
  1. Only selects open failing Dependabot/Renovate PRs created <= 2026-11-22 (>= 5 days old)
     so bumped packages ALWAYS clear the mandatory 72-hour supply-chain cooldown!
  2. Pre-filters for lightweight, pure-code libraries (size <= 25 MB, single-package,
     no docker/testcontainers/playwright/cypress/selenium/django/postgres/redis/torch/opencv/electron)
     and Python 3.12 compatibility (`requires-python` check).
  3. Uses `gh search prs --checks=failure --created=2025-11-01..2026-11-22` PLUS repo search
     so we harvest high-signal stuck breaking upgrades rapidly within REST quota.
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
import urllib.parse
import urllib.request
from pathlib import Path

OSI_LICENSES = {
    "MIT", "Apache-2.0", "BSD-2-Clause", "BSD-3-Clause", "ISC",
    "MPL-2.0", "LGPL-2.1", "LGPL-3.0", "GPL-2.0", "GPL-3.0",
    "Unlicense", "0BSD", "CC0-1.0", "BSL-1.0", "EPL-2.0",
}

SKIP_TITLE_RE = re.compile(
    r"(github-actions|actions/|docker|dockerfile|helm|terraform|codeql|pre-commit|rust|cargo|go\.mod|maven|gradle|playwright|cypress|mkdocs|sphinx)",
    re.IGNORECASE,
)

BAD_TS_PKG_RE = re.compile(
    r"(playwright|cypress|testcontainers|docker|electron|react-native|puppeteer|selenium|webdriver|no test specified|exit 1)",
    re.IGNORECASE,
)

BAD_PY_TOML_RE = re.compile(
    r"(requires-python\s*=\s*['\"]>=\s*3\.1[345]|requires-python\s*=\s*['\"]==\s*3\.1[345]|torch|cuda|tensorflow|opencv|testcontainers|playwright|selenium|django|psycopg|pymongo|celery|kubernetes|ansible|pyqt|pyside)",
    re.IGNORECASE,
)

MAX_CREATED_ISO = "2026-11-22T23:59:59Z"
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
            "User-Agent": "greenpatch-aged-harvester",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    for _ in range(2):
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code in (403, 429):
                return None
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


def inspect_repo_aged(repo: dict, language: str, token: str) -> dict | None:
    full_name = repo["full_name"]
    lic = (repo.get("license") or {}).get("spdx_id")
    if lic not in OSI_LICENSES:
        return None
    stars = int(repo.get("stargazers_count") or 0)
    if not (50 <= stars <= 3000):
        return None
    if int(repo.get("size") or 0) > 25_000:  # lightweight libraries only
        return None

    branch = repo.get("default_branch", "main")

    # Check manifest via raw.githubusercontent.com FIRST (0 GitHub REST API calls!)
    if language == "TypeScript":
        pkg_raw = raw_get(full_name, branch, "package.json")
        if not pkg_raw or BAD_TS_PKG_RE.search(pkg_raw):
            return None
        try:
            pkg_j = json.loads(pkg_raw)
        except Exception:
            return None
        test_cmd = str((pkg_j.get("scripts") or {}).get("test") or "")
        if not test_cmd or pkg_j.get("workspaces"):
            return None
        # Check lockfile via raw HEAD/GET (0 REST calls!)
        lockfile = None
        if raw_get(full_name, branch, "package-lock.json") is not None:
            lockfile = "package-lock.json"
        elif raw_get(full_name, branch, "pnpm-lock.yaml") is not None and raw_get(full_name, branch, "pnpm-workspace.yaml") is None:
            lockfile = "pnpm-lock.yaml"
        if not lockfile:
            return None
    else:
        toml_raw = raw_get(full_name, branch, "pyproject.toml")
        if not toml_raw or BAD_PY_TOML_RE.search(toml_raw):
            return None
        if "pytest" not in toml_raw.lower():
            return None
        lockfile = None
        if raw_get(full_name, branch, "uv.lock") is not None:
            lockfile = "uv.lock"
        elif raw_get(full_name, branch, "poetry.lock") is not None:
            lockfile = "poetry.lock"
        else:
            lockfile = "pyproject.toml"

    # Only NOW spend 1 REST call to check open bot PRs created <= 2026-11-22!
    prs = gh_get(f"/repos/{full_name}/pulls", token, {"state": "open", "per_page": 25, "sort": "updated", "direction": "desc"})
    if not prs or not isinstance(prs, list):
        return None

    bot_prs = [
        p for p in prs
        if (p.get("user") or {}).get("login") in ("dependabot[bot]", "renovate[bot]")
        and (p.get("created_at") or "9999") <= MAX_CREATED_ISO
        and not SKIP_TITLE_RE.search(p.get("title") or "")
    ]
    if not bot_prs:
        return None

    for pr in bot_prs[:4]:
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
            "created_at": pr.get("created_at"),
            "updated_at": pr.get("updated_at"),
        }
    return None


def harvest_lang(token: str, language: str, already_seen: set[str], target: int, out_file: Path) -> list[dict]:
    results: list[dict] = []
    seen = set(already_seen)

    # Search smaller/cleaner star bands using topic/keyword diversity and size:<25000
    queries = [
        f"language:{language} stars:50..150 size:<25000 archived:false fork:false pushed:>2025-10-01",
        f"language:{language} stars:151..400 size:<25000 archived:false fork:false pushed:>2025-10-01",
        f"language:{language} stars:401..1000 size:<25000 archived:false fork:false pushed:>2025-10-01",
        f"language:{language} stars:1001..3000 size:<25000 archived:false fork:false pushed:>2025-10-01",
    ]
    for sort_mode in ("updated", "stars"):
        for q in queries:
            if len(results) >= target:
                break
            repos = []
            for page in range(1, 5):
                data = gh_get("/search/repositories", token, {"q": q, "sort": sort_mode, "order": "desc", "per_page": 100, "page": page})
                time.sleep(2.1)
                if not data or not data.get("items"):
                    break
                for r in data["items"]:
                    if r["full_name"] not in seen:
                        seen.add(r["full_name"])
                        repos.append(r)

            if not repos:
                continue
            with concurrent.futures.ThreadPoolExecutor(max_workers=14) as pool:
                futs = [pool.submit(inspect_repo_aged, r, language, token) for r in repos]
                for fut in concurrent.futures.as_completed(futs):
                    item = fut.result()
                    if item:
                        with PRINT_LOCK:
                            if len(results) < target:
                                results.append(item)
                                out_file.write_text(json.dumps(results, indent=2), encoding="utf-8")
                                print(
                                    f"[B3 {language}] #{len(results):02d} {item['repo']} (*{item['stars']}, {item['lockfile']}, cr={item['created_at'][:10]}) "
                                    f"PR #{item['pr_number']}: {item['pr_title'][:55]}",
                                    flush=True,
                                )
                    if len(results) >= target:
                        break
    return results


def main() -> None:
    token = load_pub_pat()
    hdir = Path(r"e:\market research\bench-harness\harvest")
    seen_ts: set[str] = set()
    seen_py: set[str] = set()
    for fn in ("candidates_ts_filtered.json", "candidates_ts_batch2.json"):
        p = hdir / fn
        if p.is_file():
            seen_ts.update(x["repo"] for x in json.loads(p.read_text(encoding="utf-8")))
    for fn in ("candidates_py_filtered.json", "candidates_py_batch2.json"):
        p = hdir / fn
        if p.is_file():
            seen_py.update(x["repo"] for x in json.loads(p.read_text(encoding="utf-8")))

    ts_target = int(sys.argv[1]) if len(sys.argv) > 1 else 55
    py_target = int(sys.argv[2]) if len(sys.argv) > 2 else 75

    print(f"=== Batch-3 Aged TS Harvest (target={ts_target}, excluding {len(seen_ts)} seen) ===", flush=True)
    ts3 = harvest_lang(token, "TypeScript", seen_ts, ts_target, hdir / "candidates_ts_batch3.json")

    print(f"\n=== Batch-3 Aged Python Harvest (target={py_target}, excluding {len(seen_py)} seen) ===", flush=True)
    py3 = harvest_lang(token, "Python", seen_py, py_target, hdir / "candidates_py_batch3.json")

    print(f"\nBATCH3_AGED_DONE: ts3={len(ts3)}, py3={len(py3)}", flush=True)


if __name__ == "__main__":
    main()
