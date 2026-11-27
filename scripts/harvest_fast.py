#!/usr/bin/env python3
"""
Fast Track 1 Harvester using `gh search prs --checks=failure` + batched GraphQL repo enrichment.
Collects >= 45 TS and >= 45 Python candidates (50..3000 stars, OSI license, root lockfile,
open failing Dependabot/Renovate PR modifying dependency manifests).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
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


def gh_graphql(token: str, query: str) -> dict:
    req = urllib.request.Request(
        "https://api.github.com/graphql",
        data=json.dumps({"query": query}).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "greenpatch-harvester",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def gh_search_prs_batch(token: str, language: str, authors: list[str]) -> list[dict]:
    env = dict(os.environ)
    env["GH_TOKEN"] = token
    # Sweep across multiple date slices & sort orders to get hundreds of distinct failing PRs quickly
    windows = [
        ("updated", ">2026-11-20"),
        ("updated", "2026-11-01..2026-11-20"),
        ("updated", "2026-09-01..2026-10-31"),
        ("updated", "2026-06-01..2026-08-31"),
        ("comments", ">2026-01-01"),
        ("interactions", ">2026-01-01"),
    ]
    raw_prs: dict[str, dict] = {}
    for author in authors:
        for sort_by, updated_range in windows:
            cmd = [
                "gh", "search", "prs",
                "--state=open",
                f"--author={author}",
                "--checks=failure",
                f"--language={language}",
                f"--updated={updated_range}",
                f"--sort={sort_by}",
                "--limit=100",
                "--json", "url,number,title,repository,updatedAt,author",
            ]
            r = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=45)
            if r.returncode == 0 and r.stdout.strip():
                try:
                    arr = json.loads(r.stdout)
                    for item in arr:
                        repo_name = (item.get("repository") or {}).get("nameWithOwner", "")
                        if not repo_name or SKIP_TITLE_RE.search(item.get("title") or ""):
                            continue
                        if repo_name not in raw_prs:
                            raw_prs[repo_name] = item
                except json.JSONDecodeError:
                    pass
            time.sleep(1.5)
    print(f"[{language}] Collected {len(raw_prs)} unique repos with open failing bot PRs", flush=True)
    return list(raw_prs.values())


def enrich_and_filter(token: str, language: str, pr_items: list[dict], existing: list[dict], target: int) -> list[dict]:
    results = list(existing)
    seen = {x["repo"] for x in results}

    # Filter out already-seen repos
    todo = [p for p in pr_items if (p.get("repository") or {}).get("nameWithOwner") not in seen]

    batch_size = 25
    for start in range(0, len(todo), batch_size):
        if len(results) >= target:
            break
        chunk = todo[start : start + batch_size]
        parts = []
        for idx, p in enumerate(chunk):
            full = p["repository"]["nameWithOwner"]
            owner, name = full.split("/", 1)
            pr_num = int(p["number"])
            parts.append(
                f'r{idx}: repository(owner: {json.dumps(owner)}, name: {json.dumps(name)}) {{ '
                f'nameWithOwner stargazerCount isArchived isFork diskUsage '
                f'licenseInfo {{ spdxId }} '
                f'defaultBranchRef {{ name }} '
                f'rootTree: object(expression: "HEAD:") {{ ... on Tree {{ entries {{ name }} }} }} '
                f'pr: pullRequest(number: {pr_num}) {{ '
                f'  number title url updatedAt headRefName headRefOid baseRefName baseRefOid '
                f'  author {{ login }} '
                f'  files(first: 20) {{ nodes {{ path }} }} '
                f'}} '
                f'}}'
            )
        query = "query { " + "\n".join(parts) + " }"
        try:
            resp = gh_graphql(token, query)
        except Exception as exc:
            print(f"[graphql-err] {exc}", file=sys.stderr)
            time.sleep(3)
            continue

        data = resp.get("data") or {}
        for idx, p in enumerate(chunk):
            if len(results) >= target:
                break
            r = data.get(f"r{idx}")
            if not r:
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

            pr = r.get("pr")
            if not pr:
                continue
            changed = [n["path"] for n in ((pr.get("files") or {}).get("nodes") or []) if n]

            if language == "TypeScript":
                if "package.json" not in root_files:
                    continue
                lockfile = "package-lock.json" if "package-lock.json" in root_files else (
                    "pnpm-lock.yaml" if "pnpm-lock.yaml" in root_files else None
                )
                if not lockfile:
                    continue
                if not any(f.endswith(("package.json", "package-lock.json", "pnpm-lock.yaml")) for f in changed):
                    continue
            else:
                if "pyproject.toml" not in root_files:
                    continue
                lockfile = "uv.lock" if "uv.lock" in root_files else (
                    "poetry.lock" if "poetry.lock" in root_files else None
                )
                if not lockfile:
                    continue
                if not any(f.endswith(("pyproject.toml", "uv.lock", "poetry.lock", "requirements.txt")) for f in changed):
                    continue

            seen.add(full)
            item = {
                "repo": full,
                "language": language,
                "stars": stars,
                "license": lic,
                "default_branch": (r.get("defaultBranchRef") or {}).get("name", "main"),
                "lockfile": lockfile,
                "pr_number": pr["number"],
                "pr_url": pr["url"],
                "pr_title": pr["title"],
                "pr_author": (pr.get("author") or {}).get("login"),
                "pr_head_ref": pr.get("headRefName"),
                "pr_head_sha": pr.get("headRefOid"),
                "pr_base_ref": pr.get("baseRefName"),
                "pr_base_sha": pr.get("baseRefOid"),
                "changed_files": changed[:10],
                "updated_at": pr.get("updatedAt"),
            }
            results.append(item)
            print(
                f"[{language}] #{len(results):02d} {full} (*{stars}, {lockfile}) "
                f"PR #{item['pr_number']} ({item['pr_author']}): {item['pr_title'][:65]}",
                flush=True,
            )
        time.sleep(0.5)

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

    ts_raw = gh_search_prs_batch(token, "TypeScript", ["app/dependabot", "app/renovate"])
    ts_final = enrich_and_filter(token, "TypeScript", ts_raw, existing_ts, target)
    ts_file.write_text(json.dumps(ts_final, indent=2), encoding="utf-8")

    py_raw = gh_search_prs_batch(token, "Python", ["app/dependabot", "app/renovate"])
    py_final = enrich_and_filter(token, "Python", py_raw, existing_py, target)
    py_file.write_text(json.dumps(py_final, indent=2), encoding="utf-8")

    print(f"\nFAST_HARVEST_DONE: ts={len(ts_final)}, py={len(py_final)}, total={len(ts_final)+len(py_final)}", flush=True)


if __name__ == "__main__":
    main()
