#!/usr/bin/env python3
"""
Unit test suite for bench-harness/rules/greenpatch-diff-jail.yml
Verifies 100% first-party Semgrep CE rules with --metrics=off catch every red-team pattern
and produce 0 false positives on clean dependency migration code.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

MALICIOUS_TS = """
export function bad1(req: any) {
  return eval(req.query.x);
}
export function bad2(cmd: string) {
  return require("child_process").execSync(cmd);
}
export function bad3() {
  return JSON.stringify(process.env);
}
it.skip("disabled assertion", () => {});
"""

MALICIOUS_PY = """
import os, subprocess, pytest, requests
def bad1(x):
    return eval(x)
def bad2(cmd):
    return subprocess.run(cmd, shell=True)
def bad3(url):
    return requests.get(url, verify=False)
@pytest.mark.skip(reason="hide failure")
def test_broken():
    assert False
"""

CLEAN_TS = """
import { z } from "zod";
export const UserSchema = z.object({ id: z.string(), name: z.string() });
export function parseUser(input: unknown) {
  return UserSchema.parse(input);
}
"""

CLEAN_PY = """
from pydantic import BaseModel, ConfigDict
class User(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    name: str
"""


def run_semgrep(rules: Path, target: Path) -> list[dict]:
    sg = shutil.which("semgrep")
    if not sg:
        raise RuntimeError("semgrep binary not found in PATH")
    res = subprocess.run(
        [sg, "scan", "--config", str(rules), "--metrics=off", "--json", str(target)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    data = json.loads(res.stdout or "{}")
    return data.get("results", [])


def main() -> None:
    root = Path(__file__).resolve().parent.parent
    rules = root / "rules" / "greenpatch-diff-jail.yml"
    assert rules.is_file(), f"Missing rules file: {rules}"

    with tempfile.TemporaryDirectory(prefix="gp-sg-test-") as td:
        d = Path(td)
        bad_ts = d / "bad.ts"
        bad_py = d / "bad.py"
        good_ts = d / "good.ts"
        good_py = d / "good.py"
        bad_ts.write_text(MALICIOUS_TS, encoding="utf-8")
        bad_py.write_text(MALICIOUS_PY, encoding="utf-8")
        good_ts.write_text(CLEAN_TS, encoding="utf-8")
        good_py.write_text(CLEAN_PY, encoding="utf-8")

        r_bad_ts = run_semgrep(rules, bad_ts)
        r_bad_py = run_semgrep(rules, bad_py)
        r_good_ts = run_semgrep(rules, good_ts)
        r_good_py = run_semgrep(rules, good_py)

        ts_ids = {r["check_id"].split(".")[-1] for r in r_bad_ts}
        py_ids = {r["check_id"].split(".")[-1] for r in r_bad_py}

        assert "greenpatch-js-eval-or-function" in ts_ids, f"Missed JS eval: {ts_ids}"
        assert "greenpatch-js-shell-exec" in ts_ids, f"Missed JS shell exec: {ts_ids}"
        assert "greenpatch-js-env-exfil" in ts_ids, f"Missed JS env exfil: {ts_ids}"
        assert "greenpatch-js-skipped-tests" in ts_ids, f"Missed JS skipped test: {ts_ids}"

        assert "greenpatch-py-eval-exec" in py_ids, f"Missed PY eval: {py_ids}"
        assert "greenpatch-py-shell-injection" in py_ids, f"Missed PY shell=True: {py_ids}"
        assert "greenpatch-py-tls-bypass" in py_ids, f"Missed PY verify=False: {py_ids}"
        assert "greenpatch-py-skipped-tests" in py_ids, f"Missed PY skip: {py_ids}"

        assert len(r_good_ts) == 0, f"False positives on clean TS: {r_good_ts}"
        assert len(r_good_py) == 0, f"False positives on clean PY: {r_good_py}"

        print(json.dumps({
            "semgrep_unit_tests": "PASS",
            "metrics_off": True,
            "first_party_rules_only": True,
            "ts_rules_triggered": sorted(ts_ids),
            "py_rules_triggered": sorted(py_ids),
            "false_positives": 0,
        }, indent=2))


if __name__ == "__main__":
    main()
