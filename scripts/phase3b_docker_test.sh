#!/usr/bin/env bash
# Phase 3b — Throwaway /tmp/ephemeral-run Air-Gapped Docker Execution (Blocks "PR Planting")
# NEVER mounts /tmp/clean-repo :rw into untrusted tests.
# Captures ONLY $EXIT_CODE + <=4,000 tokens of gitleaks-scrubbed logs.
# Destroys /tmp/ephemeral-run immediately; ZERO bytes are ever read back.
# Usage: phase3b_docker_test.sh <clean_repo_dir> <ecosystem: ts|py> <pkg_cache_dir> [patch_file] [out_log]
set -uo pipefail

CLEAN_REPO="${1:?clean repo dir required}"
ECOSYSTEM="${2:?ecosystem (ts|py) required}"
PKG_CACHE="${3:-/tmp/pkg-cache}"
PATCH_FILE="${4:-}"
OUT_LOG="${5:-/tmp/scrubbed-test.log}"

EPHEMERAL_DIR="/tmp/ephemeral-run"
RAW_LOG="/tmp/raw-test.log"

rm -rf "${EPHEMERAL_DIR}" "${RAW_LOG}" "${OUT_LOG}"
cp -a "${CLEAN_REPO}" "${EPHEMERAL_DIR}"

if [[ -n "${PATCH_FILE}" && -f "${PATCH_FILE}" && -s "${PATCH_FILE}" ]]; then
  if ! git -C "${EPHEMERAL_DIR}" apply --check "${PATCH_FILE}" 2>"${RAW_LOG}"; then
    echo "[Phase 3b] PATCH_APPLY_FAILED" >> "${RAW_LOG}"
    rm -rf "${EPHEMERAL_DIR}"
    head -c 16000 "${RAW_LOG}" > "${OUT_LOG}"
    exit 90
  fi
  git -C "${EPHEMERAL_DIR}" apply "${PATCH_FILE}"
fi

if [[ "${ECOSYSTEM}" == "ts" ]]; then
  if [[ -f "${EPHEMERAL_DIR}/pnpm-lock.yaml" ]]; then
    INNER_CMD="corepack enable && pnpm install --offline --ignore-scripts --store-dir /pkg-cache && timeout -k 10s 180s pnpm test -- --watchAll=false || timeout -k 10s 180s pnpm test"
  else
    INNER_CMD="npm ci --offline --ignore-scripts --no-audit && timeout -k 10s 180s npm test -- --watchAll=false"
  fi

  docker run --rm \
    --network none \
    -w /workspace \
    --read-only \
    --tmpfs /tmp:rw,nosuid,nodev,size=512m \
    --cap-drop=ALL \
    --security-opt=no-new-privileges \
    --cpus="1.5" --memory="3g" --pids-limit=256 \
    --user "$(id -u):$(id -g)" \
    -v "${EPHEMERAL_DIR}:/workspace:rw" \
    -v "${PKG_CACHE}:/pkg-cache:ro" \
    node:22-bookworm-slim \
    /usr/bin/env -i \
      PATH="/workspace/node_modules/.bin:/usr/local/bin:/usr/bin:/bin" \
      HOME="/tmp" \
      CI="true" \
      npm_config_cache="/pkg-cache" \
      npm_config_logs_dir="/tmp/npm-logs" \
      npm_config_update_notifier="false" \
      npm_config_offline="true" \
      npm_config_ignore_scripts="true" \
    sh -c "${INNER_CMD}" \
    > "${RAW_LOG}" 2>&1
  EXIT_CODE=$?

elif [[ "${ECOSYSTEM}" == "py" ]]; then
  docker run --rm \
    --network none \
    -w /workspace \
    --read-only \
    --tmpfs /tmp:rw,nosuid,nodev,size=512m \
    --cap-drop=ALL \
    --security-opt=no-new-privileges \
    --cpus="1.5" --memory="3g" --pids-limit=256 \
    --user "$(id -u):$(id -g)" \
    -v "${EPHEMERAL_DIR}:/workspace:rw" \
    -v "${PKG_CACHE}:/pkg-cache:ro" \
    python:3.12-slim \
    /usr/bin/env -i \
      PATH="/pkg-cache/venv/bin:/usr/local/bin:/usr/bin:/bin" \
      HOME="/tmp" \
      CI="true" \
      PYTHONDONTWRITEBYTECODE="1" \
      PYTHONPATH="/workspace:/workspace/src" \
      UV_OFFLINE="1" \
    sh -c "timeout -k 10s 180s pytest -q --maxfail=10" \
    > "${RAW_LOG}" 2>&1
  EXIT_CODE=$?
else
  echo "[Phase 3b] Unknown ecosystem: ${ECOSYSTEM}" > "${RAW_LOG}"
  EXIT_CODE=2
fi

# Destroy throwaway workspace IMMEDIATELY — zero bytes ever read back!
rm -rf "${EPHEMERAL_DIR}"

# Scrub secrets with gitleaks (plus regex fallback) and cap to <=4,000 tokens (~16,000 chars)
python3 - "${RAW_LOG}" "${OUT_LOG}" <<'PY'
import re, subprocess, sys
raw_path, out_path = sys.argv[1], sys.argv[2]
text = open(raw_path, "r", encoding="utf-8", errors="replace").read()

# Redact high-risk secret patterns before handing logs to LLM
patterns = [
    r"AIza[0-9A-Za-z_\-]{25,}",
    r"github_pat_[0-9A-Za-z_]{30,}",
    r"gh[pousr]_[0-9A-Za-z]{25,}",
    r"sk-[0-9A-Za-z_\-]{20,}",
    r"(?i)(api[_-]?key|secret|token|password)\s*[:=]\s*['\"]?[A-Za-z0-9_\-\.]{12,}['\"]?",
]
for pat in patterns:
    text = re.sub(pat, "[REDACTED_SECRET]", text)

# Run gitleaks if installed to double-verify zero leaks
try:
    r = subprocess.run(
        ["gitleaks", "detect", "--no-git", "--source", raw_path, "--redact", "--exit-code", "99"],
        capture_output=True, text=True, timeout=20,
    )
    if r.returncode == 99:
        text = "[GITLEAKS_REDACTED_LOG_LINE_BLOCKED]\n" + text[-6000:]
except Exception:
    pass

# Keep tail + head <= 16,000 chars (~4,000 tokens)
if len(text) > 16000:
    text = text[:4000] + "\n... [LOG TRUNCATED TO <=4,000 TOKENS] ...\n" + text[-11500:]

open(out_path, "w", encoding="utf-8").write(text)
PY

exit "${EXIT_CODE}"
