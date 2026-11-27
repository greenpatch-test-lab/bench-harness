#!/usr/bin/env python3
"""
GreenPatch Strict ZDR Cache-Off Refactor Node (Volume 11)
- Step-scoped GEMINI_API_KEY only.
- 100% Strict ZDR Cache-Off Mode: cached_input = 0, Google Search Grounding OFF.
- Runs detect-secrets redaction pre-flight on the 32k-40k local AST slice before calling gemini-4-neon.
- Staged output clamps: max_output_tokens = 4096 (Loop 1), 8192 (Loops 2-3).
- Hard $0.85/repo cumulative token fuse.
- Outputs unified git diff (/tmp/candidate.patch) + telemetry JSON.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

MODEL = "gemini-4-neon"
PRICE_IN_PER_1M = 3.00
PRICE_OUT_PER_1M = 15.00
REPO_USD_FUSE = 0.85

SECRET_REDACT_PATTERNS = [
    re.compile(r"AIza[0-9A-Za-z_\-]{25,}"),
    re.compile(r"github_pat_[0-9A-Za-z_]{30,}"),
    re.compile(r"gh[pousr]_[0-9A-Za-z]{25,}"),
    re.compile(r"sk-[0-9A-Za-z_\-]{20,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"-----BEGIN [A-Z ]+ PRIVATE KEY-----[\s\S]*?-----END [A-Z ]+ PRIVATE KEY-----"),
]


def redact_secrets(text: str) -> tuple[str, int]:
    hits = 0
    for pat in SECRET_REDACT_PATTERNS:
        text, n = pat.subn("[REDACTED_BY_PREFLIGHT]", text)
        hits += n
    # Run detect-secrets CLI scan if available on host
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as tf:
        tf.write(text)
        tf_path = tf.name
    try:
        r = subprocess.run(["detect-secrets", "scan", tf_path], capture_output=True, text=True, timeout=20)
        if r.returncode == 0 and r.stdout:
            data = json.loads(r.stdout)
            results = (data.get("results") or {}).get(tf_path) or []
            if results:
                lines = text.splitlines()
                for finding in results:
                    ln = int(finding.get("line_number", 0)) - 1
                    if 0 <= ln < len(lines):
                        lines[ln] = "// [REDACTED_BY_DETECT_SECRETS]"
                        hits += 1
                text = "\n".join(lines)
    except Exception:
        pass
    finally:
        try:
            os.unlink(tf_path)
        except OSError:
            pass
    return text, hits


def normalize_hunk_headers(diff_text: str) -> str:
    """Recalculate exact @@ -old_s,old_c +new_s,new_c @@ line counts so git apply never fails on LLM off-by-one arithmetic."""
    if not diff_text.strip():
        return ""
    lines = diff_text.splitlines()
    out: list[str] = []
    i = 0
    hunk_re = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$")
    while i < len(lines):
        m = hunk_re.match(lines[i])
        if not m:
            out.append(lines[i])
            i += 1
            continue
        old_s, new_s, tail = int(m.group(1)), int(m.group(3)), m.group(5)
        body: list[str] = []
        i += 1
        while i < len(lines) and not lines[i].startswith(("diff --git ", "--- ", "+++ ", "@@ ")):
            ln = lines[i]
            if ln.startswith(("+", "-", " ", "\\")):
                body.append(ln)
            elif ln == "":
                body.append(" ")
            else:
                break
            i += 1
        old_c = sum(1 for b in body if b.startswith((" ", "-")))
        new_c = sum(1 for b in body if b.startswith((" ", "+")))
        out.append(f"@@ -{old_s},{old_c} +{new_s},{new_c} @@{tail}")
        out.extend(body)
    return "\n".join(out).rstrip("\n") + "\n"


def extract_unified_diff(raw_text: str) -> str:
    # Prefer fenced ```diff ... ``` block
    m = re.search(r"```(?:diff|patch)?\s*\n(diff --git[\s\S]*?)```", raw_text)
    if m:
        return normalize_hunk_headers(m.group(1).strip())
    m2 = re.search(r"```(?:diff|patch)?\s*\n(--- a/[\s\S]*?)```", raw_text)
    if m2:
        return normalize_hunk_headers(m2.group(1).strip())
    idx = raw_text.find("diff --git ")
    if idx != -1:
        return normalize_hunk_headers(raw_text[idx:].strip())
    idx2 = raw_text.find("--- a/")
    if idx2 != -1:
        return normalize_hunk_headers(raw_text[idx2:].strip())
    return ""


def call_gemini_refactor(
    api_key: str,
    loop_num: int,
    slice_data: dict,
    changelog_json: dict,
    test_log: str,
    pr_title: str,
    prior_rejection: str,
) -> dict:
    max_out = 4096 if loop_num == 1 else 8192
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent"

    files_block = []
    for f in slice_data.get("files", []):
        files_block.append(f"=== FILE: {f['path']} (reason: {f['reason']}) ===\n{f['content']}\n")
    joined_files = "\n".join(files_block)
    redacted_files, secret_redactions = redact_secrets(joined_files)
    redacted_log, log_redactions = redact_secrets(test_log[:14000])

    system_instruction = (
        "You are GreenPatch Deps, a minimal, security-hardened dependency upgrade migration engine.\n"
        "Strict Rules:\n"
        "1. Output ONLY a valid unified `git diff` inside a single ```diff code block.\n"
        "2. Touch <= 10 files. Make the smallest possible API migration fix for the dependency upgrade.\n"
        "3. NEVER touch .github/, .vscode/, Dockerfile, package.json `scripts`, or unrelated auth/middleware.\n"
        "4. NEVER delete, skip, or weaken test assertions (`expect`, `assert`, `it.skip`, `pytest.mark.skip`).\n"
        "5. NEVER add eval(), exec(), child_process, os.system, shell=True, or outbound network calls.\n"
        "6. Use exact file paths relative to repo root with `--- a/<path>` and `+++ b/<path>` and accurate @@ hunk headers.\n"
        "7. CRITICAL: Copy context and removed (`-`) lines VERBATIM from the exact file contents shown below. NEVER invent imports or lines that do not exist in the file!"
    )

    user_prompt = (
        f"PR Title: {pr_title}\n"
        f"Loop: {loop_num}/3\n\n"
        f"Typed Breaking Changes JSON (from public changelog):\n"
        f"{json.dumps(changelog_json, indent=2)}\n\n"
        f"Failing Offline Test Log (scrubbed):\n{redacted_log}\n\n"
    )
    if prior_rejection:
        user_prompt += f"Prior Attempt Rejection / Error (FIX THIS):\n{prior_rejection}\n\n"
    user_prompt += f"Local AST Code Slice:\n{redacted_files}\n\nReturn ONLY the ```diff block."

    body = {
        "systemInstruction": {"parts": [{"text": system_instruction}]},
        "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
        # Strict ZDR Cache-Off: NO cachedContent, NO google_search grounding!
        "generationConfig": {
            "temperature": 0.1,
            "maxOutputTokens": max_out,
            "thinkingConfig": {"thinkingLevel": "LOW" if loop_num == 1 else "MEDIUM"},
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
            with urllib.request.urlopen(req, timeout=180) as resp:
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
    diff_text = extract_unified_diff(raw_text)

    return {
        "diff": diff_text,
        "telemetry": {
            "model": MODEL,
            "loop": loop_num,
            "max_output_tokens": max_out,
            "strict_zdr_cache_off": True,
            "input_tokens": in_tok,
            "cached_input_tokens": 0,
            "output_and_thinking_tokens": out_tok,
            "usd_cost": round(usd, 6),
            "preflight_redactions": secret_redactions + log_redactions,
            "finish_reason": cand.get("finishReason"),
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--loop", type=int, required=True)
    parser.add_argument("--slice", required=True)
    parser.add_argument("--changelog", required=True)
    parser.add_argument("--test-log", required=True)
    parser.add_argument("--pr-title", required=True)
    parser.add_argument("--cumulative-usd", type=float, default=0.0)
    parser.add_argument("--prior-rejection", default="")
    parser.add_argument("--out-patch", required=True)
    parser.add_argument("--out-telemetry", required=True)
    args = parser.parse_args()

    if args.cumulative_usd >= REPO_USD_FUSE:
        telem = {"fuse_tripped": True, "cumulative_usd": args.cumulative_usd, "fuse_limit": REPO_USD_FUSE}
        Path(args.out_telemetry).write_text(json.dumps(telem, indent=2), encoding="utf-8")
        print(json.dumps(telem))
        raise SystemExit(44)

    api_key = os.environ.get("GEMINI_API_KEY", "").strip("\ufeff \t\r\n")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY must be injected strictly at step scope")

    slice_data = json.loads(Path(args.slice).read_text(encoding="utf-8"))
    changelog_data = json.loads(Path(args.changelog).read_text(encoding="utf-8"))
    changelog_json = changelog_data.get("changelog", changelog_data)
    test_log = Path(args.test_log).read_text(encoding="utf-8", errors="replace") if Path(args.test_log).is_file() else ""

    res = call_gemini_refactor(
        api_key,
        args.loop,
        slice_data,
        changelog_json,
        test_log,
        args.pr_title,
        args.prior_rejection,
    )
    Path(args.out_patch).write_text(res["diff"], encoding="utf-8")
    Path(args.out_telemetry).write_text(json.dumps(res["telemetry"], indent=2), encoding="utf-8")
    print(json.dumps(res["telemetry"]))


if __name__ == "__main__":
    main()
