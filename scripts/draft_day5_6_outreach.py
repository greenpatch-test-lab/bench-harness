#!/usr/bin/env python3
"""
Deterministic Day-5 & Day-6 Human-Review Draft Generator (Volume 11 Days 5-6)
Reads REAL numbers from `bench-harness/results/track1_analysis.json` ONLY — NEVER fabricates numbers!
Generates strictly human-review markdown files in `e:\market research\outreach-drafts\`:
  - maintainer_replies_batch1.md (top 8 cleanest fork fixes, flag human <=5/day)
  - maintainer_replies_batch2.md (next 5-7 cleanest fork fixes, total 10-15, <=5/day)
  - benchmark_writeup.md (grounded empirical benchmark post with real fix rate, P50/P90 ZDR-Off COGS, catches)
NEVER auto-posts any external content!
"""
from __future__ import annotations

import json
from pathlib import Path

DISCLAIMER = (
    "Experimental Open-Source Research Preview — Static Call-Path Heuristic & Build Check Only. "
    "Does NOT guarantee vulnerability unexploitability, code safety, or EU CRA compliance."
)


def format_reply(idx: int, fix: dict) -> str:
    return (
        f"## {idx}. `{fix['repo']}` — {fix['pr_title']}\n\n"
        f"- **Upstream PR Thread (Post Comment Manually Only):** {fix['upstream_pr_url']}\n"
        f"- **Fork-Local Verified Commit (`greenpatch-test-lab`):** {fix.get('fork_commit_url') or fix.get('fork_pr_url')}\n"
        f"- **Telemetry:** `{fix['ecosystem'].upper()}` · `{fix['loops_used']}` loop(s) · "
        f"`{fix['changed_files_count']}` file(s) · `{fix['diff_lines']}` diff lines · `${float(fix['total_usd']):.3f}` ZDR-Off COGS\n\n"
        f"### Draft Comment (Human Review & Manual Post — Limit `<= 5/day`)\n\n"
        f"> Hi maintainers — I ran into the failing checks on `{fix['pr_title']}` while benchmarking automated breaking-upgrade migrations in an isolated fork (`greenpatch-test-lab`).\n"
        f">\n"
        f"> Here is a minimal `{fix['changed_files_count']}`-file patch (`{fix['diff_lines']}` diff lines) that passed the offline test suite in our isolated fork: {fix.get('fork_commit_url') or fix.get('fork_pr_url')}\n"
        f">\n"
        f"> Happy to open a clean PR matching your contributing guidelines if useful, or feel free to cherry-pick directly!\n"
        f">\n"
        f"> *{DISCLAIMER}*\n\n"
        "---\n"
    )


def main() -> None:
    root = Path(r"e:\market research")
    analysis_path = root / "bench-harness" / "results" / "track1_analysis.json"
    if not analysis_path.is_file():
        raise RuntimeError(f"Missing real Track 1 analysis: {analysis_path}")

    a = json.loads(analysis_path.read_text(encoding="utf-8"))
    out_dir = root / "outreach-drafts"
    out_dir.mkdir(parents=True, exist_ok=True)

    fixes = a.get("top_15_cleanest_fixes") or []
    batch1 = fixes[:8]
    batch2 = fixes[8:15]

    b1_md = [
        "# Day-5 Opt-In Maintainer Replies — Batch 1 (Human Review & Manual Posting ONLY)",
        "",
        "> **CRITICAL ANTI-SPAM RULE (Directive 1.4):** NEVER auto-post. Manually inspect every linked fork diff first, and post at most **`<= 5` replies per day**. Open an upstream PR ONLY if a maintainer explicitly invites it.",
        "",
        f"*(Generated from real Track 1 run: {a['verified_green_count']}/{a['total_repos']} verified green = {a['verified_green_rate_pct']}%)*",
        "",
    ] + [format_reply(i + 1, f) for i, f in enumerate(batch1)]
    (out_dir / "maintainer_replies_batch1.md").write_text("\n".join(b1_md), encoding="utf-8")

    b2_md = [
        "# Day-6 Opt-In Maintainer Replies — Batch 2 (Human Review & Manual Posting ONLY)",
        "",
        "> **CRITICAL ANTI-SPAM RULE (Directive 1.4):** NEVER auto-post. Keep cumulative replies at `10–15` total and **`<= 5/day`**. Open an upstream PR ONLY if a maintainer explicitly invites it.",
        "",
    ] + [format_reply(i + 9, f) for i, f in enumerate(batch2)]
    (out_dir / "maintainer_replies_batch2.md").write_text("\n".join(b2_md), encoding="utf-8")

    sec = a.get("security_telemetry") or {}
    tax = a.get("failure_taxonomy") or {}
    tax_rows = "\n".join(f"| `{k}` | {v} |" for k, v in sorted(tax.items(), key=lambda kv: -kv[1]))

    bm_md = f"""# Empirical Benchmark Writeup Draft (Day 6 — Human Review & Manual Posting ONLY)

> **100% Grounded Real Telemetry** from `bench-harness/results/track1_analysis.json` (`N = {a['total_repos']}` clean-baseline failing Dependabot/Renovate PRs in Strict ZDR Cache-Off Mode).

---

**Suggested Title:**  
`We ran {a['total_repos']} real failing Dependabot & Renovate PRs through an air-gapped Docker --network none + Strict ZDR Cache-Off refactor loop: exact fix rate, COGS, and Diff-Auditor catches`

## Headline Empirical Results (`N = {a['total_repos']}`, `100%` Strict ZDR Cache-Off `cached_input = 0`)

| Metric | Observed Result |
| :--- | :---: |
| **Total Clean-Baseline Failing PRs Tested** | `{a['total_repos']}` (`{a['ts_repos']}` TS/Node + `{a['py_repos']}` Python) |
| **Verified-Green Fix Rate (`exit 0` + Diff Auditor + Semgrep Jail in `<=3` loops)** | **`{a['verified_green_count']} / {a['total_repos']}` (`{a['verified_green_rate_pct']}%`)** (`{a['ts_green_count']}` TS / `{a['py_green_count']}` Py) |
| **Total `gemini-4-neon` Spend (Strict ZDR Cache-Off)** | **`${float(a['total_usd_burn']):.4f}`** (`{a['total_input_tokens']:,}` in / `{a['total_output_and_thinking_tokens']:,}` out+think / `{a['total_grounding_queries']}` grounding queries) |
| **Direct Green-PR COGS (P50 / P90)** | **`${float(a['direct_green_pr_p50_usd']):.3f}` P50 / `${float(a['direct_green_pr_p90_usd']):.3f}` P90** |
| **Blended COGS per Verified-Green PR (incl. suppressed attempts)** | **`${float(a['blended_usd_per_green_pr']):.3f}`** |
| **Security & Anti-Planting Telemetry** | `{sec.get('secret_leaks', 0)}` secret leaks · `{sec.get('planted_mutations', 0)}` planted bytes · `{sec.get('ast_scope_jail_rejections', 0)}` AST Scope Jail catches · `{sec.get('semgrep_catches', 0)}` custom Semgrep catches · `{sec.get('auditor_rejections', 0)}` zero-context Diff Auditor catches · `0` unsolicited upstream PRs |

## Failure & Outcome Taxonomy

| Outcome Bucket | Count |
| :--- | ---: |
{tax_rows}

## Try It Free on Any Public GitHub Repo (60s)
Paste any public GitHub repository URL at `https://greenpatch-diag.pages.dev` for deterministic 4-tier CVE reachability triage, downloadable EU CRA CycloneDX/OpenVEX JSON, and a live 1–3 file breaking-upgrade diff preview.

*{DISCLAIMER}*
"""
    (out_dir / "benchmark_writeup.md").write_text(bm_md, encoding="utf-8")
    print(f"OUTREACH_DRAFTS_GENERATED: batch1={len(batch1)}, batch2={len(batch2)}, benchmark_writeup.md written")


if __name__ == "__main__":
    main()
