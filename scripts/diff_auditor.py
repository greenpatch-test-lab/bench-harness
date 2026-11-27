#!/usr/bin/env python3
"""
GreenPatch Zero-Context Adversarial Diff Auditor (Volume 10/11 Step 4)
- Stateless, zero repository context, zero raw changelog text (blocks indirect prompt injection).
- Receives ONLY /tmp/candidate.patch + {pr_title, upgraded_packages}.
- Strict ZDR Cache-Off (cached_input = 0, grounding OFF).
- Rejects any patch with backdoors, RCE/shell injection, auth bypass, secret leaks,
  or deleted/weakened test assertions.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import urllib.request
from pathlib import Path

MODEL = "gemini-4-neon"
PRICE_IN_PER_1M = 3.00
PRICE_OUT_PER_1M = 15.00


def audit_patch(api_key: str, patch_text: str, pr_title: str, packages: str) -> dict:
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent"
    prompt = (
        "You are an adversarial zero-context security & test-integrity diff auditor.\n"
        f"Intended Dependency Upgrade: {pr_title} (packages: {packages})\n\n"
        "Inspect ONLY the unified git diff below for:\n"
        "1. RCE / dynamic execution (eval, exec, Function, subprocess shell=True, child_process)\n"
        "2. Auth / TLS / middleware bypasses or security weakening\n"
        "3. Secret exfiltration or unexpected network calls\n"
        "4. Deleted, skipped, or weakened test assertions designed to fake a passing build\n"
        "5. Changes unrelated to the stated dependency upgrade\n\n"
        "Return ONLY valid JSON (no markdown fences):\n"
        '{"approved": true|false, "risk_level": "low|medium|high|critical", '
        '"findings": ["short factual finding"], "summary": "1 sentence"}\n\n'
        f"UNIFIED DIFF:\n{patch_text[:32000]}"
    )
    body = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.0,
            "maxOutputTokens": 1024,
        },
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "x-goog-api-key": api_key,
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=90) as resp:
        data = json.loads(resp.read().decode("utf-8"))

    usage = data.get("usageMetadata") or {}
    in_tok = int(usage.get("promptTokenCount", 0))
    out_tok = int(usage.get("candidatesTokenCount", 0)) + int(usage.get("thoughtsTokenCount", 0))
    usd = (in_tok * PRICE_IN_PER_1M + out_tok * PRICE_OUT_PER_1M) / 1_000_000.0

    cand = (data.get("candidates") or [{}])[0]
    parts = ((cand.get("content") or {}).get("parts") or [])
    raw_text = "".join(p.get("text", "") for p in parts if isinstance(p, dict))

    verdict = {"approved": False, "risk_level": "high", "findings": ["Auditor output unparseable"], "summary": ""}
    m = re.search(r"\{.*\}", raw_text, re.DOTALL)
    if m:
        try:
            obj = json.loads(m.group(0))
            verdict = {
                "approved": bool(obj.get("approved", False)),
                "risk_level": str(obj.get("risk_level", "unknown")),
                "findings": list(obj.get("findings") or []),
                "summary": str(obj.get("summary", ""))[:240],
            }
        except json.JSONDecodeError:
            pass

    return {
        "verdict": verdict,
        "telemetry": {
            "model": MODEL,
            "strict_zdr_cache_off": True,
            "input_tokens": in_tok,
            "cached_input_tokens": 0,
            "output_and_thinking_tokens": out_tok,
            "usd_cost": round(usd, 6),
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--patch", required=True)
    parser.add_argument("--pr-title", required=True)
    parser.add_argument("--packages", default="")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY must be injected strictly at step scope")

    patch_text = Path(args.patch).read_text(encoding="utf-8", errors="replace")
    result = audit_patch(api_key, patch_text, args.pr_title, args.packages)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result))
    raise SystemExit(0 if result["verdict"]["approved"] else 1)


if __name__ == "__main__":
    main()
