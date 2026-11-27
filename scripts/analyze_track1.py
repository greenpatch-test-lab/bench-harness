#!/usr/bin/env python3
"""
GreenPatch Track 1 Statistical Analyzer (Volume 11 Day 4 & Day 7)
Aggregates bench-harness/results/ts/*.json and bench-harness/results/py/*.json to compute:
  - Verified-green fix rate (exit 0 AND Diff Auditor approved AND AST Scope Jail / Semgrep passed in <=3 loops)
  - P50 / P90 Strict ZDR-Off COGS per verified-green PR (incl. suppressed-attempt overhead & per-green direct cost)
  - Auditor / AST Scope Jail / Semgrep catch breakdown
  - Failure taxonomy
  - Top 15 cleanest fork fixes for human opt-in maintainer review
"""
from __future__ import annotations

import json
import math
import statistics
from pathlib import Path


def percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    k = (len(s) - 1) * (p / 100.0)
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return s[int(k)]
    return s[f] + (s[c] - s[f]) * (k - f)


def main() -> None:
    root = Path(__file__).resolve().parent.parent
    res_dir = root / "results"
    records = []
    for sub in ("ts", "py"):
        d = res_dir / sub
        if d.is_dir():
            for fp in sorted(d.glob("*.json")):
                try:
                    records.append(json.loads(fp.read_text(encoding="utf-8")))
                except Exception:
                    pass

    total = len(records)
    green = [r for r in records if r.get("verified_green") is True]
    ts_recs = [r for r in records if r.get("ecosystem") == "ts"]
    py_recs = [r for r in records if r.get("ecosystem") == "py"]
    ts_green = [r for r in ts_recs if r.get("verified_green") is True]
    py_green = [r for r in py_recs if r.get("verified_green") is True]

    total_usd = sum(float(r.get("total_usd", 0.0)) for r in records)
    total_in = sum(int(r.get("total_input_tokens", 0)) for r in records)
    total_out = sum(int(r.get("total_output_tokens", 0)) for r in records)
    total_grounding = sum(int(r.get("grounding_queries", 0)) for r in records)

    green_costs = [float(r.get("total_usd", 0.0)) for r in green]
    all_costs = [float(r.get("total_usd", 0.0)) for r in records]
    blended_per_green = (total_usd / len(green)) if green else 0.0

    # Failure & catch taxonomy
    taxonomy: dict[str, int] = {}
    auditor_rejects = 0
    jail_rejects = 0
    semgrep_catches = 0
    fuse_trips = 0
    secret_leaks = 0
    planted_mutations = 0

    for r in records:
        status = r.get("final_status", "unknown")
        taxonomy[status] = taxonomy.get(status, 0) + 1
        if r.get("auditor_rejected"):
            auditor_rejects += 1
        if r.get("jail_rejected"):
            jail_rejects += 1
        if int(r.get("semgrep_findings_count", 0)) > 0:
            semgrep_catches += 1
        if r.get("fuse_tripped"):
            fuse_trips += 1
        secret_leaks += int(r.get("secret_leaks", 0))
        planted_mutations += int(r.get("planted_mutations", 0))

    # Rank cleanest fixes: fewest loops, fewest files changed, smallest diff lines, lowest cost
    ranked_clean = sorted(
        green,
        key=lambda r: (
            int(r.get("loops_used", 3)),
            int(r.get("changed_files_count", 10)),
            int(r.get("diff_lines", 999)),
            float(r.get("total_usd", 9.99)),
        ),
    )[:15]

    summary = {
        "total_repos": total,
        "ts_repos": len(ts_recs),
        "py_repos": len(py_recs),
        "verified_green_count": len(green),
        "verified_green_rate_pct": round((len(green) / total * 100.0), 2) if total else 0.0,
        "ts_green_count": len(ts_green),
        "py_green_count": len(py_green),
        "strict_zdr_cache_off_verified": all(int(r.get("cached_input_tokens", 0)) == 0 for r in records),
        "total_input_tokens": total_in,
        "total_output_and_thinking_tokens": total_out,
        "total_grounding_queries": total_grounding,
        "total_usd_burn": round(total_usd, 4),
        "blended_usd_per_green_pr": round(blended_per_green, 4),
        "direct_green_pr_p50_usd": round(percentile(green_costs, 50), 4),
        "direct_green_pr_p90_usd": round(percentile(green_costs, 90), 4),
        "all_repo_p50_usd": round(percentile(all_costs, 50), 4),
        "all_repo_p90_usd": round(percentile(all_costs, 90), 4),
        "security_telemetry": {
            "auditor_rejections": auditor_rejects,
            "ast_scope_jail_rejections": jail_rejects,
            "semgrep_catches": semgrep_catches,
            "fuse_trips": fuse_trips,
            "secret_leaks": secret_leaks,
            "planted_mutations": planted_mutations,
            "upstream_prs_opened": 0,
        },
        "failure_taxonomy": taxonomy,
        "top_15_cleanest_fixes": [
            {
                "repo": r.get("upstream_repo"),
                "fork_pr_url": r.get("fork_pr_url"),
                "fork_commit_url": r.get("fork_commit_url"),
                "upstream_pr_url": r.get("upstream_pr_url"),
                "pr_title": r.get("pr_title"),
                "ecosystem": r.get("ecosystem"),
                "loops_used": r.get("loops_used"),
                "changed_files_count": r.get("changed_files_count"),
                "diff_lines": r.get("diff_lines"),
                "total_usd": r.get("total_usd"),
            }
            for r in ranked_clean
        ],
    }

    out_path = res_dir / "track1_analysis.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    (res_dir / "day7_exports").mkdir(parents=True, exist_ok=True)
    (res_dir / "day7_exports" / "track1_analysis.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
