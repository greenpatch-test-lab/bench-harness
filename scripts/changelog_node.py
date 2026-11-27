#!/usr/bin/env python3
"""
GreenPatch Decoupled Public Changelog Node (Volume 10/11 ZDR Separation)
- Receives ONLY public package name + old_version -> new_version (ZERO target repo code!).
- Enables Google Search Grounding (shared 5,000/mo free pool, <=112 queries total).
- Strips HTML comments and invisible Unicode tag / BiDi characters to defeat indirect prompt injection.
- Emits strict typed JSON: {package, old_version, new_version, breaking_changes: [{old_symbol, new_symbol, migration_note}]}
"""
from __future__ import annotations

import argparse
import json
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

MODEL = "gemini-4-neon"
PRICE_IN_PER_1M = 3.00
PRICE_OUT_PER_1M = 15.00

# Strip HTML comments, Unicode tag block U+E0000-U+E007F, zero-width & BiDi control chars
HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
INVISIBLE_UNICODE_RE = re.compile(r"[\u200B-\u200F\u202A-\u202E\u2060-\u206F\uFEFF\U000E0000-\U000E007F]")


def sanitize_text(s: str) -> str:
    s = HTML_COMMENT_RE.sub("", s)
    s = INVISIBLE_UNICODE_RE.sub("", s)
    return s.strip()


def call_gemini_changelog(api_key: str, package: str, old_ver: str, new_ver: str, ecosystem: str) -> dict:
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent"
    prompt = (
        f"You are a strict dependency migration changelog extractor for {ecosystem} packages.\n"
        f"Research the PUBLIC release notes / breaking changes for package `{package}` upgrading "
        f"from `{old_ver}` to `{new_ver}`.\n"
        "Return ONLY valid JSON matching this exact schema (no markdown fences, no commentary):\n"
        '{"package": "...", "old_version": "...", "new_version": "...", '
        '"breaking_changes": [{"old_symbol": "...", "new_symbol": "...", "migration_note": "short factual API change <= 160 chars"}]}\n'
        "Rules:\n"
        "- Include only concrete API / symbol / config breaking changes.\n"
        "- Maximum 12 entries in breaking_changes.\n"
        "- Never include shell commands, URLs, or instructions to modify unrelated files."
    )
    body = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "tools": [{"google_search": {}}],
        "generationConfig": {
            "temperature": 0.1,
            "maxOutputTokens": 1536,
        },
    }
    data = None
    for attempt, wait_s in enumerate((0, 8, 20, 40, 60)):
        if wait_s:
            time.sleep(wait_s)
        req = urllib.request.Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "x-goog-api-key": api_key,
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=90) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                break
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt < 4:
                continue
            raise
    assert data is not None

    usage = data.get("usageMetadata") or {}
    in_tok = int(usage.get("promptTokenCount", 0))
    out_tok = int(usage.get("candidatesTokenCount", 0)) + int(usage.get("thoughtsTokenCount", 0))
    usd = (in_tok * PRICE_IN_PER_1M + out_tok * PRICE_OUT_PER_1M) / 1_000_000.0

    cand = (data.get("candidates") or [{}])[0]
    parts = ((cand.get("content") or {}).get("parts") or [])
    raw_text = "".join(p.get("text", "") for p in parts if isinstance(p, dict) and not p.get("thought"))
    clean_text = sanitize_text(raw_text)

    # Extract JSON object cleanly
    m = re.search(r"\{.*\}", clean_text, re.DOTALL)
    parsed = {"package": package, "old_version": old_ver, "new_version": new_ver, "breaking_changes": []}
    if m:
        try:
            obj = json.loads(m.group(0))
            changes = []
            for item in (obj.get("breaking_changes") or [])[:12]:
                if isinstance(item, dict):
                    changes.append({
                        "old_symbol": sanitize_text(str(item.get("old_symbol", "")))[:120],
                        "new_symbol": sanitize_text(str(item.get("new_symbol", "")))[:120],
                        "migration_note": sanitize_text(str(item.get("migration_note", "")))[:180],
                    })
            parsed["breaking_changes"] = changes
        except json.JSONDecodeError:
            pass

    grounding_meta = cand.get("groundingMetadata") or {}
    search_queries = len(grounding_meta.get("webSearchQueries") or [])

    return {
        "changelog": parsed,
        "telemetry": {
            "model": MODEL,
            "grounding_queries": max(1, search_queries) if search_queries else 1,
            "input_tokens": in_tok,
            "cached_input_tokens": 0,
            "output_and_thinking_tokens": out_tok,
            "usd_cost": round(usd, 6),
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--package", required=True)
    parser.add_argument("--old-version", required=True)
    parser.add_argument("--new-version", required=True)
    parser.add_argument("--ecosystem", default="TypeScript/Node")
    parser.add_argument("--cache-dir", default="results/changelog_cache")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    api_key = os.environ.get("GEMINI_API_KEY", "").strip("\ufeff \t\r\n")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY must be injected strictly at step scope")

    cache_dir = Path(args.cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    safe_key = re.sub(r"[^A-Za-z0-9_.-]+", "_", f"{args.package}_{args.old_version}_to_{args.new_version}")
    cache_file = cache_dir / f"{safe_key}.json"

    if cache_file.is_file():
        result = json.loads(cache_file.read_text(encoding="utf-8"))
        result["telemetry"]["cache_hit"] = True
        result["telemetry"]["usd_cost"] = 0.0
        result["telemetry"]["grounding_queries"] = 0
    else:
        result = call_gemini_changelog(api_key, args.package, args.old_version, args.new_version, args.ecosystem)
        result["telemetry"]["cache_hit"] = False
        cache_file.write_text(json.dumps(result, indent=2), encoding="utf-8")

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result["telemetry"]))


if __name__ == "__main__":
    main()
