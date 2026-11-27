#!/usr/bin/env python3
"""
GreenPatch Track 1 Local Python AST Slicer (stdlib ast)
Produces full production 32k-40k token local slices (Strict ZDR Cache-Off parity).
Extracts:
  - pyproject.toml / setup.cfg / pytest.ini
  - Files referenced in test failure logs
  - Files importing upgraded package(s) (normalizing '-' <-> '_')
  - Related test files within the 32k-40k token budget
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import re
from pathlib import Path

SKIP_DIRS = {
    ".git", ".github", ".venv", "venv", "__pycache__", ".pytest_cache",
    ".mypy_cache", ".ruff_cache", "node_modules", "dist", "build", ".tox",
}


def estimate_tokens(text: str) -> int:
    return (len(text) + 3) // 4


def normalize_pkg(name: str) -> set[str]:
    base = re.split(r"[<>=!~;\[]", name)[0].strip().lower()
    variants = {base, base.replace("-", "_"), base.replace("_", "-")}
    # Common PyPI distribution -> import module mappings
    aliases = {
        "pyyaml": "yaml",
        "pillow": "PIL",
        "scikit-learn": "sklearn",
        "beautifulsoup4": "bs4",
        "python-dateutil": "dateutil",
        "pydantic-settings": "pydantic_settings",
        "google-genai": "google",
        "pyjwt": "jwt",
    }
    if base in aliases:
        variants.add(aliases[base].lower())
    return {v for v in variants if v}


def extract_imported_roots(source: str) -> set[str]:
    roots: set[str] = set()
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return roots
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(alias.name.split(".")[0].lower())
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module.split(".")[0].lower())
    return roots


def extract_paths_from_log(log_text: str, repo_dir: Path) -> set[str]:
    found: set[str] = set()
    for m in re.finditer(r"([A-Za-z0-9_./-]+\.py)", log_text):
        rel = m.group(1).lstrip("./")
        if rel.startswith("workspace/"):
            rel = rel[len("workspace/"):]
        cand = repo_dir / rel
        if cand.is_file():
            found.add(rel.replace("\\", "/"))
    return found


def collect_py_files(repo_dir: Path) -> list[Path]:
    out: list[Path] = []
    for root, dirs, files in os.walk(repo_dir):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for fn in files:
            if fn.endswith(".py"):
                p = Path(root) / fn
                try:
                    if p.stat().st_size <= 250_000:
                        out.append(p)
                except OSError:
                    pass
    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--packages", default="")
    parser.add_argument("--test-log", default="")
    parser.add_argument("--max-tokens", type=int, default=36000)
    parser.add_argument("--out", default="")
    args = parser.parse_args()

    repo_dir = Path(args.repo).resolve()
    max_tokens = min(40_000, max(8_000, args.max_tokens))
    pkgs = [p.strip() for p in args.packages.split(",") if p.strip()]
    pkg_roots: set[str] = set()
    for p in pkgs:
        pkg_roots.update(normalize_pkg(p))

    log_text = ""
    if args.test_log and Path(args.test_log).is_file():
        log_text = Path(args.test_log).read_text(encoding="utf-8", errors="replace")
    log_refs = extract_paths_from_log(log_text, repo_dir)

    scored: list[tuple[int, str, str, str]] = []

    for cfg in ("pyproject.toml", "setup.cfg", "pytest.ini", "conftest.py"):
        p = repo_dir / cfg
        if p.is_file():
            scored.append((1000, cfg, "manifest", p.read_text(encoding="utf-8", errors="replace")))

    py_files = collect_py_files(repo_dir)
    for p in py_files:
        rel = p.relative_to(repo_dir).as_posix()
        raw = p.read_text(encoding="utf-8", errors="replace")
        score = 0
        reasons: list[str] = []

        if rel in log_refs:
            score += 500
            reasons.append("test_log_ref")

        imports = extract_imported_roots(raw)
        matched = imports & pkg_roots
        if matched:
            score += 300
            reasons.append("imports:" + ",".join(sorted(matched)))

        if ("test_" in p.name or rel.startswith("tests/")) and score > 0:
            score += 100
            reasons.append("test_file")

        if score > 0:
            scored.append((score, rel, ",".join(reasons), raw))

    already_rel = {r for _, r, _, _ in scored}
    if sum(1 for _, _, rsn, _ in scored if rsn != "manifest") < 5:
        for p in py_files[:25]:
            rel = p.relative_to(repo_dir).as_posix()
            if rel in already_rel:
                continue
            raw = p.read_text(encoding="utf-8", errors="replace")
            is_test = "test" in rel.lower()
            scored.append((200 if is_test else 50, rel, "test_fallback" if is_test else "src_fallback", raw))

    scored.sort(key=lambda x: (-x[0], x[1]))

    selected = []
    used_tokens = 0
    for _, rel, reason, content in scored:
        tok = estimate_tokens(content)
        if used_tokens + tok > max_tokens:
            rem = max_tokens - used_tokens
            if rem >= 500:
                trunc = content[: rem * 4] + "\n# [TRUNCATED TO SLICE BUDGET]\n"
                t = estimate_tokens(trunc)
                selected.append({"path": rel, "reason": reason + "+truncated", "estimated_tokens": t, "content": trunc})
                used_tokens += t
            break
        selected.append({"path": rel, "reason": reason, "estimated_tokens": tok, "content": content})
        used_tokens += tok

    payload = {
        "repo_dir": str(repo_dir),
        "packages": pkgs,
        "max_tokens": max_tokens,
        "total_estimated_tokens": used_tokens,
        "file_count": len(selected),
        "files": selected,
    }
    text = json.dumps(payload, indent=2)
    if args.out:
        out_p = Path(args.out)
        out_p.parent.mkdir(parents=True, exist_ok=True)
        out_p.write_text(text, encoding="utf-8")
    else:
        print(text)


if __name__ == "__main__":
    main()
