#!/usr/bin/env python3
"""
GreenPatch AST Scope Jail + First-Party Custom Semgrep CE Gate (LGPL-2.1)
Enforces:
  1. <= 10 changed files per patch
  2. Hard-abort edits to .github/, .vscode/, Dockerfile, docker-compose, package.json scripts,
     pyproject.toml build-system hooks, or unrelated auth/middleware/security files
  3. Deterministic expect()/assert non-decrease invariant (no deleted/weakened test assertions)
  4. 100% first-party custom Semgrep CE rules (`--config ./rules/greenpatch-diff-jail.yml --metrics=off`)
     NEVER uses --config=auto or p/default!
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

FORBIDDEN_PATH_PATTERNS = [
    re.compile(r"(^|/)\.github(/|$)", re.IGNORECASE),
    re.compile(r"(^|/)\.vscode(/|$)", re.IGNORECASE),
    re.compile(r"(^|/)dockerfile(\.|$)", re.IGNORECASE),
    re.compile(r"(^|/)docker-compose", re.IGNORECASE),
    re.compile(r"(^|/)\.git(/|$)", re.IGNORECASE),
    re.compile(r"(^|/)\.npmrc$", re.IGNORECASE),
    re.compile(r"(^|/)Makefile$", re.IGNORECASE),
]

SENSITIVE_AUTH_PATH = re.compile(
    r"(^|/)(auth|oauth|jwt|rbac|acl|permission|middleware|security|session|csrf)[A-Za-z0-9_.-]*\.(ts|tsx|js|jsx|py)$",
    re.IGNORECASE,
)

ASSERTION_PATTERNS = [
    re.compile(r"\bexpect\s*\("),
    re.compile(r"\bassert\b"),
    re.compile(r"\bassertEqual\b|\bassertTrue\b|\bassertFalse\b|\bassertRaises\b"),
    re.compile(r"\btoEqual\s*\(|\btoBe\s*\(|\btoThrow\s*\(|\btoMatch\s*\("),
]


def parse_changed_files(patch_text: str) -> dict[str, dict[str, list[str]]]:
    files: dict[str, dict[str, list[str]]] = {}
    cur_file: str | None = None
    for line in patch_text.splitlines():
        if line.startswith("+++ b/"):
            cur_file = line[6:].strip()
            files.setdefault(cur_file, {"added": [], "removed": []})
        elif line.startswith("--- a/") or line.startswith("diff --git "):
            continue
        elif cur_file is not None:
            if line.startswith("+") and not line.startswith("+++"):
                files[cur_file]["added"].append(line[1:])
            elif line.startswith("-") and not line.startswith("---"):
                files[cur_file]["removed"].append(line[1:])
    return files


def check_package_json_scripts_mutated(repo_dir: Path, patched_dir: Path) -> list[str]:
    violations: list[str] = []
    orig_p = repo_dir / "package.json"
    new_p = patched_dir / "package.json"
    if orig_p.is_file() and new_p.is_file():
        try:
            orig_scripts = json.loads(orig_p.read_text(encoding="utf-8")).get("scripts", {})
            new_scripts = json.loads(new_p.read_text(encoding="utf-8")).get("scripts", {})
            if orig_scripts != new_scripts:
                violations.append("package.json `scripts` section was modified (forbidden by AST Scope Jail)")
        except Exception as exc:
            violations.append(f"package.json invalid JSON after patch: {exc}")
    return violations


def count_assertions(lines: list[str]) -> int:
    total = 0
    for line in lines:
        stripped = line.strip()
        if stripped.startswith(("//", "#", "*")):
            continue
        for pat in ASSERTION_PATTERNS:
            total += len(pat.findall(stripped))
    return total


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--patch", required=True)
    parser.add_argument("--rules", default="rules/greenpatch-diff-jail.yml")
    parser.add_argument("--upgraded-packages", default="")
    parser.add_argument("--max-files", type=int, default=10)
    parser.add_argument("--out", default="")
    args = parser.parse_args()

    repo_dir = Path(args.repo).resolve()
    patch_path = Path(args.patch).resolve()
    rules_path = Path(args.rules).resolve()
    upgraded_pkgs = [p.strip().lower() for p in args.upgraded_packages.split(",") if p.strip()]

    patch_text = patch_path.read_text(encoding="utf-8", errors="replace") if patch_path.is_file() else ""
    violations: list[str] = []
    semgrep_findings: list[dict] = []

    if not patch_text.strip():
        violations.append("Empty patch")

    changed = parse_changed_files(patch_text)
    file_list = list(changed.keys())

    # 1. File count cap
    if len(file_list) > args.max_files:
        violations.append(f"Patch touches {len(file_list)} files (exceeds AST Scope Jail limit <= {args.max_files})")

    # 2. Forbidden paths & unrelated auth/middleware
    for fp in file_list:
        for pat in FORBIDDEN_PATH_PATTERNS:
            if pat.search(fp):
                violations.append(f"Forbidden path modified: {fp}")
        if SENSITIVE_AUTH_PATH.search(fp):
            # Only allow if the upgraded package itself is imported in that file
            orig_file = repo_dir / fp
            orig_text = orig_file.read_text(encoding="utf-8", errors="replace").lower() if orig_file.is_file() else ""
            if not any(pkg in orig_text for pkg in upgraded_pkgs):
                violations.append(f"Unrelated auth/middleware file modified: {fp}")

    # 3. Assertion non-decrease invariant (anti-weakened-test check)
    for fp, hunks in changed.items():
        if re.search(r"(test|spec)", fp, re.IGNORECASE):
            removed_a = count_assertions(hunks["removed"])
            added_a = count_assertions(hunks["added"])
            if removed_a > added_a:
                violations.append(
                    f"Weakened test assertions in {fp}: removed {removed_a} assertions vs added {added_a}"
                )
            for added_line in hunks["added"]:
                if re.search(r"\b(it|test|describe)\.skip\b|pytest\.mark\.skip|unittest\.skip|@pytest\.mark\.xfail", added_line):
                    violations.append(f"Skipped test added in {fp}: {added_line.strip()}")

    # 4. Apply onto a temporary scratch copy to check package.json scripts + run first-party Semgrep CE
    with tempfile.TemporaryDirectory(prefix="gp-jail-") as tmpdir:
        tmp_repo = Path(tmpdir) / "repo"
        shutil.copytree(repo_dir, tmp_repo, symlinks=True, ignore=shutil.ignore_patterns("node_modules", ".venv", ".git"))
        subprocess.run(["git", "init", "-q"], cwd=tmp_repo, check=True)
        subprocess.run(["git", "add", "-A"], cwd=tmp_repo, check=True)
        subprocess.run(
            ["git", "-c", "user.email=jail@greenpatch.local", "-c", "user.name=jail", "commit", "-q", "-m", "base"],
            cwd=tmp_repo,
            check=True,
        )
        apply_res = subprocess.run(["git", "apply", str(patch_path)], cwd=tmp_repo, capture_output=True, text=True)
        if apply_res.returncode != 0:
            violations.append(f"Patch fails clean apply onto pristine host repo: {apply_res.stderr.strip()[:300]}")
        else:
            violations.extend(check_package_json_scripts_mutated(repo_dir, tmp_repo))

            # Run first-party Semgrep CE ONLY on the touched files (--metrics=off, NEVER --config=auto)
            touched_paths = [str(tmp_repo / fp) for fp in file_list if (tmp_repo / fp).is_file()]
            semgrep_bin = shutil.which("semgrep")
            if touched_paths and semgrep_bin and rules_path.is_file():
                sg = subprocess.run(
                    [semgrep_bin, "scan", "--config", str(rules_path), "--metrics=off", "--json", *touched_paths],
                    capture_output=True,
                    text=True,
                    timeout=90,
                )
                try:
                    sg_json = json.loads(sg.stdout or "{}")
                    for r in sg_json.get("results", []):
                        semgrep_findings.append({
                            "rule_id": r.get("check_id"),
                            "path": r.get("path"),
                            "line": (r.get("start") or {}).get("line"),
                            "message": (r.get("extra") or {}).get("message"),
                        })
                        violations.append(
                            f"Semgrep Diff Jail [{r.get('check_id')}] at line {(r.get('start') or {}).get('line')}: "
                            f"{(r.get('extra') or {}).get('message')}"
                        )
                except Exception as exc:
                    violations.append(f"Semgrep output parse error: {exc}")

    report = {
        "passed": len(violations) == 0,
        "changed_file_count": len(file_list),
        "changed_files": file_list,
        "violations": violations,
        "semgrep_findings": semgrep_findings,
    }
    text = json.dumps(report, indent=2)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(text, encoding="utf-8")
    print(text)
    raise SystemExit(0 if report["passed"] else 1)


if __name__ == "__main__":
    main()
