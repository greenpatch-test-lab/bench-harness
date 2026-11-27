#!/usr/bin/env python3
"""
Unit tests for scripts/ast_scope_jail.py verifying every Directive 2.4 invariant:
  - Rejects .github/ edits
  - Rejects package.json scripts mutations
  - Rejects weakened/deleted expect()/assert assertions
  - Rejects Semgrep eval() injection
  - Approves a clean minimal migration patch
"""
from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def run_jail(repo: Path, patch_text: str, pkg: str = "demo-sdk") -> tuple[int, dict]:
    p = repo.parent / "test.patch"
    p.write_text(patch_text, encoding="utf-8")
    res = subprocess.run(
        [
            "python3", str(ROOT / "scripts" / "ast_scope_jail.py"),
            "--repo", str(repo),
            "--patch", str(p),
            "--rules", str(ROOT / "rules" / "greenpatch-diff-jail.yml"),
            "--upgraded-packages", pkg,
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    return res.returncode, json.loads(res.stdout)


def make_base_repo(td: Path) -> Path:
    r = td / "repo"
    r.mkdir()
    (r / "package.json").write_text(json.dumps({"name": "demo", "scripts": {"test": "node test.js"}}, indent=2) + "\n")
    (r / "index.ts").write_text("export function greet(x: string) {\n  return oldFn(x);\n}\n")
    (r / "index.test.ts").write_text("test('works', () => {\n  expect(greet('a')).toBe('a');\n  expect(greet('b')).toBe('b');\n});\n")
    return r


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="gp-jail-ut-") as tmp:
        td = Path(tmp)
        repo = make_base_repo(td)

        # 1. Clean patch -> PASS
        clean_patch = (
            "--- a/index.ts\n+++ b/index.ts\n@@ -1,3 +1,3 @@\n"
            " export function greet(x: string) {\n-  return oldFn(x);\n+  return newFn(x);\n }\n"
        )
        rc, rep = run_jail(repo, clean_patch)
        assert rc == 0 and rep["passed"], f"Clean patch failed: {rep}"

        # 2. Weakened assertion -> REJECT
        weak_patch = (
            "--- a/index.test.ts\n+++ b/index.test.ts\n@@ -1,4 +1,3 @@\n"
            " test('works', () => {\n   expect(greet('a')).toBe('a');\n-  expect(greet('b')).toBe('b');\n });\n"
        )
        rc, rep = run_jail(repo, weak_patch)
        assert rc != 0 and not rep["passed"], f"Weakened assertion was NOT rejected: {rep}"

        # 3. package.json scripts mutation -> REJECT
        script_patch = (
            "--- a/package.json\n+++ b/package.json\n@@ -1,6 +1,6 @@\n {\n   \"name\": \"demo\",\n   \"scripts\": {\n"
            "-    \"test\": \"node test.js\"\n+    \"test\": \"true\"\n   }\n }\n"
        )
        rc, rep = run_jail(repo, script_patch)
        assert rc != 0 and not rep["passed"], f"package.json scripts edit was NOT rejected: {rep}"

        # 4. eval() injection caught by custom Semgrep CE rules -> REJECT
        eval_patch = (
            "--- a/index.ts\n+++ b/index.ts\n@@ -1,3 +1,3 @@\n"
            " export function greet(x: string) {\n-  return oldFn(x);\n+  return eval(x);\n }\n"
        )
        rc, rep = run_jail(repo, eval_patch)
        assert rc != 0 and not rep["passed"] and len(rep["semgrep_findings"]) > 0, f"eval() NOT caught by Semgrep: {rep}"

        # 5. .github/ path mutation -> REJECT
        gh_patch = (
            "--- /dev/null\n+++ b/.github/workflows/pwn.yml\n@@ -0,0 +1,2 @@\n+name: pwn\n+on: push\n"
        )
        rc, rep = run_jail(repo, gh_patch)
        assert rc != 0 and not rep["passed"], f".github edit NOT rejected: {rep}"

        print(json.dumps({"ast_scope_jail_unit_tests": "PASS", "cases_verified": 5}, indent=2))


if __name__ == "__main__":
    main()
