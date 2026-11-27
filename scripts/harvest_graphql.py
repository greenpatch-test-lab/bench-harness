#!/usr/bin/env python3
"""
Ultra-fast GraphQL Harvester for Track 1 (Volume 10 §2.2 / Volume 11 Day 1)
Scans active 50..3000-star OSI-licensed TypeScript & Python repos with inline
open Dependabot/Renovate PRs + failing statusCheckRollup in 1 GraphQL call per 40 repos!
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
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

GQL_QUERY = """
query($q: String!, $after: String) {
  search(query: $q, type: REPOSITORY, first: 15, after: $after) {
    pageInfo { hasNextPage endCursor }
    nodes {
      ... on Repository {
        nameWithOwner
        stargazerCount
        isArchived
        isFork
        diskUsage
        licenseInfo { spdxId }
        defaultBranchRef { name }
        rootTree: object(expression: "HEAD:") {
          ... on Tree { entries { name } }
        }
        pullRequests(states: OPEN, first: 8, orderBy: {field: UPDATED_AT, direction: DESC}) {
          nodes {
            number
            title
            url
            updatedAt
            headRefName
            headRefOid
            baseRefName
            baseRefOid
            author { login }
            files(first: 6) { nodes { path } }
            commits(last: 1) {
              nodes {
                commit {
                  statusCheckRollup { state }
                }
              }
            }
          }
        }
      }
    }
  }
}
"""


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


def gh_graphql(token: str, query: str, variables: dict) -> dict:
    req = urllib.request.Request(
        "https://api.github.com/graphql",
        data=json.dumps({"query": query, "variables": variables}).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "greenpatch-gql-harvester",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def harvest_lang(token: str, language: str, existing: list[dict], target: int) -> list[dict]:
    results = list(existing)
    seen = {x["repo"] for x in results}

    bands = [
        "stars:50..120",
        "stars:121..250",
        "stars:251..500",
        "stars:501..1000",
        "stars:1001..1800",
        "stars:1801..3000",
    ]

    for band in bands:
        if len(results) >= target:
            break
        q = f"language:{language} {band} archived:false fork:false pushed:>2026-06-01 sort:updated-desc"
        cursor = None
        for page in range(1, 5):
            if len(results) >= target:
                break
            try:
                resp = gh_graphql(token, GQL_QUERY, {"q": q, "after": cursor})
            except Exception as exc:
                print(f"[gql-err] {language} {band} p{page}: {exc}", file=sys.stderr)
                time.sleep(3)
                break
            search = (resp.get("data") or {}).get("search") or {}
            nodes = search.get("nodes") or []
            for r in nodes:
                if len(results) >= target:
                    break
                if not r or not r.get("nameWithOwner"):
                    continue
                full = r["nameWithOwner"]
                if full in seen or r.get("isArchived") or r.get("isFork"):
                    continue
                stars = int(r.get("stargazerCount") or 0)
                if not (50 <= stars <= 3000):
                    continue
                if int(r.get("diskUsage") or 0) > 150_000:
                    continue
                lic = (r.get("licenseInfo") or {}).get("spdxId")
                if lic not in OSI_LICENSES:
                    continue

                entries = ((r.get("rootTree") or {}).get("entries") or [])
                root_files = {e["name"] for e in entries if isinstance(e, dict) and "name" in e}

                if language == "TypeScript":
                    if "package.json" not in root_files:
                        continue
                    lockfile = "package-lock.json" if "package-lock.json" in root_files else (
                        "pnpm-lock.yaml" if "pnpm-lock.yaml" in root_files else None
                    )
                    if not lockfile:
                        continue
                else:
                    if "uv.lock" in root_files:
                        lockfile = "uv.lock"
                    elif "poetry.lock" in root_files:
                        lockfile = "poetry.lock"
                    elif "pyproject.toml" in root_files and ("tests" in root_files or "test" in root_files):
                        lockfile = "pyproject.toml"
                    else:
                        continue

                prs = ((r.get("pullRequests") or {}).get("nodes") or [])
                chosen = None
                for pr in prs:
                    if not pr:
                        continue
                    author = (pr.get("author") or {}).get("login", "")
                    if author not in ("dependabot", "dependabot[bot]", "renovate", "renovate[bot]"):
                        continue
                    if SKIP_TITLE_RE.search(pr.get("title") or ""):
                        continue
                    c_nodes = ((pr.get("commits") or {}).get("nodes") or [])
                    rollup = (((c_nodes[0].get("commit") or {}).get("statusCheckRollup") or {}).get("state")) if c_nodes else None
                    if rollup not in ("FAILURE", "ERROR"):
                        continue
                    changed = [n["path"] for n in ((pr.get("files") or {}).get("nodes") or []) if n]
                    if language == "TypeScript":
                        if not any(f.endswith(("package.json", "package-lock.json", "pnpm-lock.yaml")) for f in changed):
                            continue
                    else:
                        if not any(f.endswith(("pyproject.toml", "uv.lock", "poetry.lock", "requirements.txt")) for f in changed):
                            continue
                    chosen = {
                        "repo": full,
                        "language": language,
                        "stars": stars,
                        "license": lic,
                        "default_branch": (r.get("defaultBranchRef") or {}).get("name", "main"),
                        "lockfile": lockfile,
                        "pr_number": pr["number"],
                        "pr_url": pr["url"],
                        "pr_title": pr["title"],
                        "pr_author": author,
                        "pr_head_ref": pr.get("headRefName"),
                        "pr_head_sha": pr.get("headRefOid"),
                        "pr_base_ref": pr.get("baseRefName"),
                        "pr_base_sha": pr.get("baseRefOid"),
                        "changed_files": changed[:10],
                        "updated_at": pr.get("updatedAt"),
                    }
                    break

                if chosen:
                    seen.add(full)
                    results.append(chosen)
                    print(
                        f"[{language}] #{len(results):02d} {full} (*{stars}, {lockfile}) "
                        f"PR #{chosen['pr_number']} ({chosen['pr_author']}): {chosen['pr_title'][:65]}",
                        flush=True,
                    )

            pi = search.get("pageInfo") or {}
            if not pi.get("hasNextPage"):
                break
            cursor = pi.get("endCursor")
            time.sleep(0.6)

    return results


def main() -> None:
    token = load_pub_pat()
    out_dir = Path(r"e:\market research\bench-harness\harvest")
    out_dir.mkdir(parents=True, exist_ok=True)
    target = int(sys.argv[1]) if len(sys.argv) > 1 else 48

    ts_file = out_dir / "candidates_ts_filtered.json"
    py_file = out_dir / "candidates_py_filtered.json"
    existing_ts = json.loads(ts_file.read_text(encoding="utf-8")) if ts_file.is_file() else []
    existing_py = json.loads(py_file.read_text(encoding="utf-8")) if py_file.is_file() else []

    print(f"=== GraphQL Harvesting TS (starting at {len(existing_ts)}, target {target}) ===", flush=True)
    ts_final = harvest_lang(token, "TypeScript", existing_ts, target)
    ts_file.write_text(json.dumps(ts_final, indent=2), encoding="utf-8")

    print(f"\n=== GraphQL Harvesting Python (starting at {len(existing_py)}, target {target}) ===", flush=True)
    py_final = harvest_lang(token, "Python", existing_py, target)
    py_file.write_text(json.dumps(py_final, indent=2), encoding="utf-8")

    print(f"\nGQL_HARVEST_DONE: ts={len(ts_final)}, py={len(py_final)}, total={len(ts_final)+len(py_final)}", flush=True)


if __name__ == "__main__":
    main()
