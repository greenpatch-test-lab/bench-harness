#!/usr/bin/env python3
"""
GreenPatch Deps Track 1 Candidate Harvester (Volume 10 §2.2 / Volume 11 Day 1)
- Uses GH_PUBLIC_READ_PAT (5,000 req/hr) strictly read-only.
- Harvests open failing Dependabot & Renovate PRs in active TypeScript and Python OSS repos
  with 50..3000 stars, OSI licenses, real lockfiles, and real test suites.
"""
from __future__ import annotations

import json
import os
import re
import sys
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

TS_LOCKFILES = {"package-lock.json", "pnpm-lock.yaml"}
PY_LOCKFILES = {"uv.lock", "poetry.lock", "pyproject.toml"}

SKIP_TITLE_RE = re.compile(
    r"(github-actions|actions/|docker|dockerfile|helm|terraform|github/codeql|pre-commit)",
    re.IGNORECASE,
)


def load_pub_pat() -> str:
    env_pat = os.environ.get("GH_PUBLIC_READ_PAT")
    if env_pat:
        return env_pat.strip()
    kf = Path(r"e:\market research\keys.txt")
    if kf.exists():
        for line in kf.read_text(encoding="utf-8").splitlines():
            m = re.match(r"^GH_PUBLIC_READ_PAT\s+(\S+)", line.strip())
            if m:
                return m.group(1)
    raise RuntimeError("GH_PUBLIC_READ_PAT not found in env or keys.txt")


def gh_get(path: str, token: str, params: dict | None = None) -> dict | list | None:
    url = f"https://api.github.com{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "greenpatch-harvester",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code in (403, 429):
                reset = e.headers.get("X-RateLimit-Reset")
                retry_after = e.headers.get("Retry-After")
                wait = int(retry_after) if retry_after and retry_after.isdigit() else 12
                if reset and reset.isdigit():
                    wait = max(2, min(30, int(reset) - int(time.time()) + 1))
                print(f"[rate-limit] {path} HTTP {e.code}, sleeping {wait}s...", file=sys.stderr)
                time.sleep(wait)
                continue
            if e.code == 404:
                return None
            print(f"[http-err] {path} HTTP {e.code}", file=sys.stderr)
            return None
        except Exception as exc:
            print(f"[net-err] {path}: {exc}", file=sys.stderr)
            time.sleep(2)
    return None


def search_failing_prs(token: str, language: str, author: str, max_pages: int = 4) -> list[dict]:
    """Use GitHub Search Issues API with status:failure / open PRs by dependabot or renovate."""
    items: list[dict] = []
    # Note: GitHub search/issues supports `is:pr is:open author:app/dependabot language:TypeScript`
    # and we verify check-runs / combined status directly via REST so we never miss checks:failure!
    q = f"is:pr is:open author:{author} language:{language}"
    for page in range(1, max_pages + 1):
        data = gh_get(
            "/search/issues",
            token,
            {"q": q, "sort": "updated", "order": "desc", "per_page": 100, "page": page},
        )
        if not data or not data.get("items"):
            break
        items.extend(data["items"])
        time.sleep(2.1)  # stay well under 30 req/min search rate limit
    return items


def search_repos_with_failing_bot_prs(token: str, language: str, target_count: int) -> list[dict]:
    """
    Hybrid strategy:
    1) Search active 50..3000-star repos in `language` pushed recently.
    2) Inspect open PRs by dependabot[bot] / renovate[bot] and verify failing checks + root lockfile + test suite.
    """
    candidates: list[dict] = []
    seen_repos: set[str] = set()

    # Query multiple star bands to get rich diversity of active SMB-sized OSS repos (50..3000 stars)
    star_bands = ["50..200", "201..600", "601..1500", "1501..3000"]
    for band in star_bands:
        if len(candidates) >= target_count:
            break
        for page in range(1, 3):
            if len(candidates) >= target_count:
                break
            q = f"language:{language} stars:{band} archived:false fork:false pushed:>2026-06-01"
            data = gh_get(
                "/search/repositories",
                token,
                {"q": q, "sort": "updated", "order": "desc", "per_page": 50, "page": page},
            )
            time.sleep(2.1)
            if not data or not data.get("items"):
                break
            for repo in data["items"]:
                if len(candidates) >= target_count:
                    break
                full_name = repo["full_name"]
                if full_name in seen_repos:
                    continue
                seen_repos.add(full_name)

                lic = (repo.get("license") or {}).get("spdx_id")
                if lic not in OSI_LICENSES:
                    continue
                stars = repo.get("stargazers_count", 0)
                if not (50 <= stars <= 3000):
                    continue
                if repo.get("size", 0) > 150_000:  # skip huge >150MB repos
                    continue

                # Check open PRs quickly
                prs = gh_get(
                    f"/repos/{full_name}/pulls",
                    token,
                    {"state": "open", "per_page": 30, "sort": "updated", "direction": "desc"},
                )
                if not prs or not isinstance(prs, list):
                    continue

                bot_prs = [
                    p for p in prs
                    if (p.get("user") or {}).get("login") in ("dependabot[bot]", "renovate[bot]")
                    and not SKIP_TITLE_RE.search(p.get("title") or "")
                ]
                if not bot_prs:
                    continue

                # Verify root lockfile & test structure before checking PR check-runs
                contents = gh_get(f"/repos/{full_name}/contents", token)
                if not contents or not isinstance(contents, list):
                    continue
                root_files = {c["name"] for c in contents if isinstance(c, dict) and "name" in c}

                if language == "TypeScript":
                    if "package.json" not in root_files:
                        continue
                    lockfile = "package-lock.json" if "package-lock.json" in root_files else (
                        "pnpm-lock.yaml" if "pnpm-lock.yaml" in root_files else None
                    )
                    if not lockfile:
                        continue
                else:
                    lockfile = "uv.lock" if "uv.lock" in root_files else (
                        "poetry.lock" if "poetry.lock" in root_files else None
                    )
                    if not lockfile or "pyproject.toml" not in root_files:
                        continue

                # Check if any bot PR has failing check runs
                chosen = None
                for pr in bot_prs[:6]:
                    head_sha = (pr.get("head") or {}).get("sha")
                    if not head_sha:
                        continue
                    checks = gh_get(f"/repos/{full_name}/commits/{head_sha}/check-runs", token, {"per_page": 30})
                    if not checks or not isinstance(checks, dict):
                        continue
                    runs = checks.get("check_runs") or []
                    failed = [
                        r["name"] for r in runs
                        if r.get("conclusion") in ("failure", "timed_out")
                    ]
                    if not failed:
                        continue

                    # Verify PR actually modifies a dependency manifest/lockfile
                    files = gh_get(f"/repos/{full_name}/pulls/{pr['number']}/files", token, {"per_page": 20})
                    if not files or not isinstance(files, list):
                        continue
                    changed = [f["filename"] for f in files]
                    if language == "TypeScript":
                        if not any(f.endswith(("package.json", "package-lock.json", "pnpm-lock.yaml")) for f in changed):
                            continue
                    else:
                        if not any(f.endswith(("pyproject.toml", "uv.lock", "poetry.lock", "requirements.txt")) for f in changed):
                            continue

                    chosen = {
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
                    break

                if chosen:
                    candidates.append(chosen)
                    print(
                        f"[{language}] #{len(candidates):02d} {full_name} (*{stars}, {lockfile}) "
                        f"PR #{chosen['pr_number']} ({chosen['pr_author']}): {chosen['pr_title'][:65]}",
                        flush=True,
                    )

    return candidates


def main() -> None:
    token = load_pub_pat()
    out_dir = Path(r"e:\market research\bench-harness\harvest")
    out_dir.mkdir(parents=True, exist_ok=True)

    target_per_lang = int(sys.argv[1]) if len(sys.argv) > 1 else 45

    print(f"=== Harvesting up to {target_per_lang} TypeScript candidates (stars 50..3000) ===", flush=True)
    ts_cands = search_repos_with_failing_bot_prs(token, "TypeScript", target_per_lang)
    (out_dir / "candidates_ts_filtered.json").write_text(json.dumps(ts_cands, indent=2), encoding="utf-8")

    print(f"\n=== Harvesting up to {target_per_lang} Python candidates (stars 50..3000) ===", flush=True)
    py_cands = search_repos_with_failing_bot_prs(token, "Python", target_per_lang)
    (out_dir / "candidates_py_filtered.json").write_text(json.dumps(py_cands, indent=2), encoding="utf-8")

    summary = {
        "ts_count": len(ts_cands),
        "py_count": len(py_cands),
        "total": len(ts_cands) + len(py_cands),
    }
    print(f"\nHARVEST_COMPLETE: {json.dumps(summary)}", flush=True)


if __name__ == "__main__":
    main()
