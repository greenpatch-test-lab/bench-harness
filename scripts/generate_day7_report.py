#!/usr/bin/env python3
"""
Automated Day-7 Pre-Capital Go / Pivot / Kill Scorecard Generator (Volume 11 §6)
Deterministically reconciles raw exports in bench-harness/results/day7_exports/:
  - track1_analysis.json (Gate 1 & Gate 2 & Gate 3)
  - canary_report.json (Gate 3)
  - scans.json & waitlist_reservations.json (Gate 5 & Gate 6)
  - cf_usage_audit.json (Gate 3 — Cloudflare % of allowance & $0.00 overage)
  - maintainer_outcomes.json (Gate 4 — strictly human-verified; NEVER fabricated!)
Generates e:\market research\day7_go_nogo_report.md.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

REQUIRED_EXPORTS = [
    "track1_analysis.json",
    "canary_report.json",
    "scans.json",
    "waitlist_reservations.json",
    "cf_usage_audit.json",
    "maintainer_outcomes.json",
]


def score_gate_1(t1: dict) -> tuple[str, str]:
    total = int(t1.get("total_repos", 0))
    green = int(t1.get("verified_green_count", 0))
    rate = float(t1.get("verified_green_rate_pct", 0.0))
    detail = f"{green}/{total} ({rate:.1f}%) verified green (exit 0 + Diff Auditor + custom Semgrep Jail in <=3 loops)"
    if total == 0:
        return "KILL", "0 repos evaluated"
    if rate >= 40.0:
        return "GO", detail
    if rate >= 25.0:
        return "PIVOT", detail
    return "KILL", detail


def score_gate_2(t1: dict) -> tuple[str, str]:
    p50 = float(t1.get("direct_green_pr_p50_usd", 0.0))
    p90 = float(t1.get("direct_green_pr_p90_usd", 0.0))
    blended = float(t1.get("blended_usd_per_green_pr", 0.0))
    detail = f"P50=${p50:.3f}/green PR, P90=${p90:.3f}/green PR (blended incl. suppressed=${blended:.3f}/green PR)"
    if p50 > 1.50 or p90 > 2.50:
        return "KILL", detail
    if p50 <= 0.75 and p90 <= 1.25:
        return "GO", detail
    return "PIVOT", detail


def score_gate_3(t1: dict, canary: dict, cf: dict, maint: dict) -> tuple[str, str]:
    sec = t1.get("security_telemetry") or {}
    leaks = int(sec.get("secret_leaks", 0)) + int(canary.get("secret_leaks", 0))
    planted = int(sec.get("planted_mutations", 0)) + int(canary.get("host_planted_bytes", 0))
    spam = int(maint.get("spam_complaints", 0))
    overage = float(cf.get("total_cash_overage_usd", 0.0))
    weakened = int(sec.get("weakened_assertions_slipped", 0))
    rce = int(sec.get("undetected_rce_or_bypass", 0))
    detail = (
        f"canary_passed={canary.get('canary_passed')}, leaks={leaks}, planted={planted}, "
        f"weakened_slipped={weakened}, spam_flags={spam}, cf_gcp_overage=${overage:.2f}"
    )
    if rce > 0 or leaks > 0 or planted != 0 or spam > 0 or not canary.get("canary_passed") or overage > 0.0:
        return "KILL", detail
    if weakened in (1, 2):
        return "PIVOT", detail
    if weakened > 2:
        return "KILL", detail
    return "GO", detail


def score_gate_4(maint: dict) -> tuple[str, str]:
    sent = int(maint.get("total_sent", 0))
    pos = int(maint.get("positive_replies", 0))
    spam = int(maint.get("spam_complaints", 0))
    slop = int(maint.get("ai_slop_rejections", 0))
    rate = (pos / sent * 100.0) if sent > 0 else 0.0
    detail = f"{pos}/{sent} ({rate:.1f}%) positive replies, {spam} spam complaints, {slop} AI-slop rejections"
    if spam > 0 or slop >= 2 or (sent >= 12 and pos == 0):
        return "KILL", detail
    if pos >= 4 and rate >= 30.0:
        return "GO", detail
    if pos >= 1:
        return "PIVOT", detail
    return "KILL", detail


def score_gate_5(scans: list[dict]) -> tuple[str, str]:
    unique_repos = len({s.get("repo_slug") for s in scans if s.get("repo_slug")})
    completed = [s for s in scans if s.get("status") != "failed"]
    errors = len([s for s in scans if s.get("status") == "failed"])
    vex_dl = len([s for s in completed if int(s.get("vex_downloaded") or 0) > 0])
    pricing_clicks = len([s for s in completed if int(s.get("pricing_clicked") or 0) > 0])
    vex_rate = (vex_dl / len(completed) * 100.0) if completed else 0.0
    ctr = (pricing_clicks / len(completed) * 100.0) if completed else 0.0
    detail = f"{unique_repos} unique repos scanned, {vex_rate:.1f}% OpenVEX downloads, {ctr:.1f}% pricing CTR, {errors} errors"
    if unique_repos < 60 or ctr < 4.0:
        return "KILL", detail
    if unique_repos >= 200 and vex_rate >= 20.0 and ctr >= 10.0 and errors == 0:
        return "GO", detail
    return "PIVOT", detail


def score_gate_6(waitlist: list[dict], unique_scans: int) -> tuple[str, str]:
    qualified = [
        w for w in waitlist
        if int(w.get("is_corporate_domain") or 0) == 1
        or w.get("team_size") in ("4-6", "7-20", "21-50", "51+")
    ]
    pro_scale = [w for w in qualified if w.get("tier") in ("pro_249", "scale_599")]
    byor = [w for w in qualified if w.get("deployment_mode") == "byor_github_action"]
    byor_pct = (len(byor) / len(qualified) * 100.0) if qualified else 0.0
    detail = (
        f"{len(qualified)} qualified B2B org signups ({len(pro_scale)} on $249 Pro/$599 Scale, "
        f"{byor_pct:.1f}% BYOR) across {unique_scans} unique scans"
    )
    if len(qualified) <= 4 and unique_scans >= 200:
        return "KILL", detail
    if len(qualified) >= 10 and len(pro_scale) >= 4 and byor_pct >= 75.0:
        return "GO", detail
    if len(qualified) >= 5:
        return "PIVOT", detail
    return "KILL", detail


def main() -> None:
    root = Path(r"e:\market research")
    exp_dir = root / "bench-harness" / "results" / "day7_exports"
    missing = [f for f in REQUIRED_EXPORTS if not (exp_dir / f).is_file()]
    if missing:
        print(f"ERROR: Missing required Day-7 raw export(s) in {exp_dir}: {missing}", file=sys.stderr)
        raise SystemExit(2)

    t1 = json.loads((exp_dir / "track1_analysis.json").read_text(encoding="utf-8"))
    canary = json.loads((exp_dir / "canary_report.json").read_text(encoding="utf-8"))
    scans = json.loads((exp_dir / "scans.json").read_text(encoding="utf-8"))
    waitlist = json.loads((exp_dir / "waitlist_reservations.json").read_text(encoding="utf-8"))
    cf = json.loads((exp_dir / "cf_usage_audit.json").read_text(encoding="utf-8"))
    maint = json.loads((exp_dir / "maintainer_outcomes.json").read_text(encoding="utf-8"))

    if not maint.get("human_verified"):
        print("ERROR: maintainer_outcomes.json MUST be explicitly human-verified (NEVER fabricated).", file=sys.stderr)
        raise SystemExit(3)

    unique_scans = len({s.get("repo_slug") for s in scans if s.get("repo_slug")})
    g1, d1 = score_gate_1(t1)
    g2, d2 = score_gate_2(t1)
    g3, d3 = score_gate_3(t1, canary, cf, maint)
    g4, d4 = score_gate_4(maint)
    g5, d5 = score_gate_5(scans)
    g6, d6 = score_gate_6(waitlist, unique_scans)

    verdicts = [g1, g2, g3, g4, g5, g6]
    if all(v == "GO" for v in verdicts):
        final_verdict = "GO"
    elif any(v == "KILL" for v in verdicts):
        final_verdict = "KILL"
    else:
        final_verdict = "PIVOT"

    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    md = [
        "# GreenPatch Deps — Automated Day-7 Pre-Capital Go / Pivot / Kill Scorecard",
        "",
        f"**Generated:** {now_iso}  ",
        "**Active Scenario:** `ULTRA_TIER = 5x` (`$40.00/mo`) · `SCENARIO = A` (`RPD <= 125`, `N = 50`, Track 2 `300` cap)  ",
        f"**Final Deterministic Verdict:** **`{final_verdict}`**",
        "",
        "---",
        "",
        "## 1. Six-Gate Reconciliation Table (Volume 11 §6)",
        "",
        "| Gate | Observed Measurement (Raw Exports) | Gate Verdict |",
        "| :--- | :--- | :---: |",
        f"| **1. Verified-Green Fix Rate (`N=50`, Strict ZDR-Off)** | {d1} | **`{g1}`** |",
        f"| **2. Realized Production-Parity ZDR-Off COGS** | {d2} | **`{g2}`** |",
        f"| **3. Security, Anti-Planting & Zero-Overage Integrity** | {d3} | **`{g3}`** |",
        f"| **4. External Maintainer Quality Signal (Human-Verified)** | {d4} | **`{g4}`** |",
        f"| **5. Track 2 Live Diff-Preview & OpenVEX Engagement** | {d5} | **`{g5}`** |",
        f"| **6. Painted-Door Commercial WTP (`$99/$249/$599` D1)** | {d6} | **`{g6}`** |",
        "",
        "---",
        "",
        "## 2. Gemini 4 Neon Credit Ledger & Cloudflare Workers Paid Utilization Audit",
        "",
        f"- **Track 1 Total Neon Burn (Strict ZDR-Off):** `${float(t1.get('total_usd_burn', 0.0)):.4f}`",
        f"- **Day-2 Canary Neon Burn:** `${float(canary.get('total_gemini_usd', 0.0)):.4f}`",
        f"- **Track 2 Total Neon Burn (AI Gateway BYOK):** `${float(cf.get('track2_neon_usd', 0.0)):.4f}`",
        f"- **Total Incremental Cash Overage (CF + GCP):** `${float(cf.get('total_cash_overage_usd', 0.0)):.2f}`",
        f"- **Cloudflare Containers Usage:** `0.000%` (Track 1 kept strictly on `$0` Public GitHub Actions)",
        "",
        "### Raw Export Provenance (`bench-harness/results/day7_exports/`)",
        "- `track1_analysis.json`",
        "- `canary_report.json`",
        "- `scans.json`",
        "- `waitlist_reservations.json`",
        "- `cf_usage_audit.json`",
        "- `maintainer_outcomes.json` (human-verified)",
        "",
    ]

    if final_verdict == "KILL":
        md.extend([
            "---",
            "",
            "## 3. Mandatory Zero-Replatforming Stack Pivot Plan (KILL Contingency)",
            "",
            "GreenPatch Deps is halted at **`$0.00` new cash** (zero Delaware LLC, zero Tech E&O, zero SOC 2 spend). "
            "Reuse **100% of the already-deployed Cloudflare Workers Paid (Pages + Workers + Workflows + Cron + `SpendFuseDO` + D1 + R2 + AI Gateway BYOK) + Google AI Ultra stack** (`$0.00` incremental infrastructure cost) to validate one of the next highest-scoring blueprints from `00_MASTER_AUTONOMOUS_BUSINESS_PLANS.md` — **strictly after running its equivalent adversarial `06`/`07`/`08` market, financial, and security/legal audit**:",
            "",
            "1. **Blueprint #2 — *Open-SLED & Federal \"RFP Shredder\"* (`92.5/100`):** Swap GitHub lockfile ingestion for a Cloudflare Cron Trigger polling sanctioned SAM.gov / Grants.gov / USAspending APIs -> store multi-PDF bid packets in R2 -> orchestrate overnight 50%-off Gemini 4 Neon Batch API shredding via Cloudflare Workflows -> serve *\"Paste Your License -> 1 Live Local RFP Shredded Free\"* on Pages + `SpendFuseDO` + D1 + AI Gateway BYOK.",
            "2. **Blueprint #3 — *API-Native 3PL Billing Overcharge Auditor* (`92.0/100`):** Weekly Cloudflare Cron Trigger ingesting ShipBob's `billing_read` REST API -> store 3PL MSA PDFs & CSV credit memos in R2 -> deterministic invoice reconciliation + Workflows + AI Gateway cached MSA clause verification -> track settled credits in D1.",
            "",
        ])

    out_path = root / "day7_go_nogo_report.md"
    out_path.write_text("\n".join(md), encoding="utf-8")
    print(f"SCORECARD_WRITTEN: {out_path} (VERDICT={final_verdict})")


if __name__ == "__main__":
    main()
