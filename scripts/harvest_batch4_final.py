#!/usr/bin/env python3
"""
Batch-4 Final Aged Harvester (25 TS + 70 Python, strictly capped < 2,200 REST calls).
Targets lightweight pure-code libraries with aged (created <= 2026-11-22) failing Dependabot/Renovate PRs.
Uses raw.githubusercontent.com pre-filtering (0 REST calls!) before touching GitHub REST.
"""
from __future__ import annotations

import concurrent.futures
import json
import os
import re
import sys
import threading
import time
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
    r"(requires-python\s*=\s*['\"]>=\s*3\.1[345]|requires-python\s*=\s*['\"]==\s*3\.1[345]|requires-python\s*=\s*['\"]==\s*3\.10|torch|cuda|tensorflow|opencv|testcontainers|playwright|selenium|django|psycopg|pymongo|celery|kubernetes|ansible|pyqt|pyside)",
    re.IGNORECASE,
)

MAX_CREATED_ISO = "2026-11-22T23:59:59Z"
LOCK = threading.Lock()
REST_CALLS = 0
MAX_REST_CALLS = 2200


def load_pub_pat() -> str:
    kf = Path(r"e:\market research\keys.txt")
    for line in kf.read_text(encoding="utf-8-sig").splitlines():
        m = re.match(r"^GH_PUBLIC_READ_PAT\s+(\S+)", line.strip())
        if m:
            return m.group(1)
    raise RuntimeError("GH_PUBLIC_READ_PAT not found")


def gh_get(path: str, token: str, params: dict | None = None):
    global REST_CALLS
    with LOCK:
        if REST_CALLS >= MAX_REST_CALLS:
            return None
        REST_CALLS += 1
    url = f"https://api.github.com{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "greenpatch-b4-harvester",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return None


def raw_get(repo: str, branch: str, filename: str) -> str | None:
    try:
        req = urllib.request.Request(
            f"https://raw.githubusercontent.com/{repo}/{branch}/{filename}",
            headers={"User-Agent": "gp-b4"},
        )
        with urllib.request.urlopen(req, timeout=8) as r:
            return r.read(100_000).decode("utf-8", errors="replace")
    except Exception:
        return None


def inspect_repo(repo: dict, language: str, token: str) -> dict | None:
    full = repo["full_name"]
    lic = (repo.get("license") or {}).get("spdx_id")
    if lic not in OSI_LICENSES:
        return None
    stars = int(repo.get("stargazers_count") or 0)
    if not (50 <= stars <= 3000):
        return None
    if int(repo.get("size") or 0) > 25_000:
        return None
    branch = repo.get("default_branch", "main")

    if language == "TypeScript":
        pkg_raw = raw_get(full, branch, "package.json")
        if not pkg_raw or BAD_TS_PKG_RE.search(pkg_raw):
            return None
        try:
            pkg_j = json.loads(pkg_raw)
        except Exception:
            return None
        if not (pkg_j.get("scripts") or {}).get("test") or pkg_j.get("workspaces"):
            return None
        if raw_get(full, branch, "package-lock.json") is not None:
            lockfile = "package-lock.json"
        elif raw_get(full, branch, "pnpm-lock.yaml") is not None and raw_get(full, branch, "pnpm-workspace.yaml") is None:
            lockfile = "pnpm-lock.yaml"
        else:
            return None
    else:
        toml_raw = raw_get(full, branch, "pyproject.toml")
        if not toml_raw or BAD_PY_TOML_RE.search(toml_raw):
            return None
        if "[tool.uv.sources]" in toml_raw and "workspace = true" in toml_raw:
            return None
        if "pytest" not in toml_raw.lower():
            return None
        if raw_get(full, branch, "uv.lock") is not None:
            lockfile = "uv.lock"
        elif raw_get(full, branch, "poetry.lock") is not None:
            lockfile = "poetry.lock"
        else:
            lockfile = "pyproject.toml"

    prs = gh_get(f"/repos/{full}/pulls", token, {"state": "open", "per_page": 25, "sort": "updated", "direction": "desc"})
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

    for pr in bot_prs[:3]:
        head_sha = (pr.get("head") or {}).get("sha")
        if not head_sha:
            continue
        checks = gh_get(f"/repos/{full}/commits/{head_sha}/check-runs", token, {"per_page": 20})
        if not checks or not isinstance(checks, dict):
            continue
        failed = [r["name"] for r in (checks.get("check_runs") or []) if r.get("conclusion") in ("failure", "timed_out")]
        if not failed:
            continue
        files = gh_get(f"/repos/{full}/pulls/{pr['number']}/files", token, {"per_page": 15})
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
            "repo": full,
            "language": language,
            "stars": stars,
            "license": lic,
            "default_branch": branch,
            "lockfile": lockfile,
            "pr_number": pr["number"],
            "pr_url": pr["html_url"],
            "pr_title": pr["title"],
            "pr_author": (pr.get("user") or {}).get("login"),
            "changed_files": changed[:10],
            "created_at": pr.get("created_at"),
            "updated_at": pr.get("updated_at"),
        }
    return None


def harvest(token: str, language: str, queries: list[str], seen: set[str], target: int, out_file: Path) -> list[dict]:
    results: list[dict] = []
    for q in queries:
        if len(results) >= target or REST_CALLS >= MAX_REST_CALLS:
            break
        repos = []
        for page in range(1, 5):
            if REST_CALLS >= MAX_REST_CALLS:
                break
            data = gh_get("/search/repositories", token, {"q": q, "sort": "updated", "order": "desc", "per_page": 100, "page": page})
            time.sleep(2.1)
            if not data or not data.get("items"):
                break
            for r in data["items"]:
                if r["full_name"] not in seen:
                    seen.add(r["full_name"])
                    repos.append(r)

        if not repos:
            continue
        with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
            futs = [pool.submit(inspect_repo, r, language, token) for r in repos]
            for fut in concurrent.futures.as_completed(futs):
                item = fut.result()
                if item:
                    with LOCK:
                        if len(results) < target:
                            results.append(item)
                            out_file.write_text(json.dumps(results, indent=2), encoding="utf-8")
                            print(
                                f"[B4 {language}] #{len(results):02d} {item['repo']} (*{item['stars']}, {item['lockfile']}, cr={str(item['created_at'])[:10]}) "
                                f"PR #{item['pr_number']}: {item['pr_title'][:52]}",
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
    for fp in hdir.glob("candidates_ts*.json"):
        try:
            seen_ts.update(x["repo"] for x in json.loads(fp.read_text(encoding="utf-8")))
        except Exception:
            pass
    for fp in hdir.glob("candidates_py*.json"):
        try:
            seen_py.update(x["repo"] for x in json.loads(fp.read_text(encoding="utf-8")))
        except Exception:
            pass

    ts_q = [
        "language:TypeScript stars:60..200 size:<15000 topic:typescript archived:false fork:false pushed:>2025-06-01",
        "language:TypeScript stars:201..800 size:<15000 topic:typescript archived:false fork:false pushed:>2025-06-01",
        "language:TypeScript stars:50..500 size:<15000 topic:nodejs archived:false fork:false pushed:>2025-06-01",
        "language:TypeScript stars:50..1500 size:<15000 topic:library archived:false fork:false pushed:>2025-06-01",
    ]
    py_q = [
        "language:Python stars:50..200 size:<20000 topic:python archived:false fork:false pushed:>2025-06-01",
        "language:Python stars:201..600 size:<20000 topic:python archived:false fork:false pushed:>2025-06-01",
        "language:Python stars:601..2500 size:<20000 topic:python archived:false fork:false pushed:>2025-06-01",
        "language:Python stars:50..1500 size:<20000 topic:cli archived:false fork:false pushed:>2025-06-01",
        "language:Python stars:50..1500 size:<20000 topic:pypi archived:false fork:false pushed:>2025-06-01",
        "language:Python stars:50..1500 size:<20000 topic:pydantic archived:false fork:false pushed:>2025-06-01",
        "language:Python stars:50..1500 size:<20000 topic:fastapi archived:false fork:false pushed:>2025-06-01",
        "language:Python stars:50..1500 size:<20000 topic:async archived:false fork:false pushed:>2025-06-01",
    ]

    print(f"=== B4 Aged TS (target=25, excluding {len(seen_ts)}) ===", flush=True)
    ts4 = harvest(token, "TypeScript", ts_q, seen_ts, 25, hdir / "candidates_ts_batch4.json")

    print(f"\n=== B4 Aged Python (target=65, excluding {len(seen_py)}) ===", flush=True)
    py4 = harvest(token, "Python", py_q, seen_py, 65, hdir / "candidates_py_batch4.json")

    print(f"\nB4_DONE: ts4={len(ts4)}, py4={len(py4)}, rest_calls_used={REST_CALLS}", flush=True)


if __name__ == "__main__":
    main()
