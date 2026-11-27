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
VERIFY_MODE="${6:-test}" # test | build | typecheck | ruff | mypy | ty

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

# For TS repos, hydrate pre-fetched --ignore-scripts node_modules into the throwaway copy if present
if [[ "${ECOSYSTEM}" == "ts" && -d "${PKG_CACHE}/preinstalled_node_modules" ]]; then
  cp -a "${PKG_CACHE}/preinstalled_node_modules" "${EPHEMERAL_DIR}/node_modules"
fi

if [[ "${ECOSYSTEM}" == "ts" ]]; then
  if [[ -f "${EPHEMERAL_DIR}/pnpm-lock.yaml" ]]; then
    if [[ "${VERIFY_MODE}" == "build" ]]; then
      RUN_STEP="timeout -k 10s 180s pnpm run build"
    elif [[ "${VERIFY_MODE}" == "typecheck" ]]; then
      RUN_STEP="timeout -k 10s 180s sh -c 'if grep -q \"\\\"typecheck\\\"\" package.json; then pnpm run typecheck; else npx --no-install tsc --noEmit; fi'"
    elif [[ "${VERIFY_MODE}" == "lint" ]]; then
      RUN_STEP="timeout -k 10s 180s pnpm run lint"
    else
      RUN_STEP="timeout -k 10s 180s pnpm test"
    fi
    INNER_CMD="if [ ! -d /workspace/node_modules ]; then pnpm install --offline --ignore-scripts --store-dir /pkg-cache/pnpm-store; fi && ${RUN_STEP}"
  else
    TEST_EXTRA=""
    if grep -qE '"test"\s*:\s*"[^"]*(jest|react-scripts)' "${EPHEMERAL_DIR}/package.json" 2>/dev/null; then
      TEST_EXTRA="-- --watchAll=false"
    fi
    if [[ "${VERIFY_MODE}" == "build" ]]; then
      RUN_STEP="timeout -k 10s 180s npm run build"
    elif [[ "${VERIFY_MODE}" == "typecheck" ]]; then
      RUN_STEP="timeout -k 10s 180s sh -c 'if grep -q \"\\\"typecheck\\\"\" package.json; then npm run typecheck; else npx --no-install tsc --noEmit; fi'"
    elif [[ "${VERIFY_MODE}" == "lint" ]]; then
      RUN_STEP="timeout -k 10s 180s npm run lint"
    else
      RUN_STEP="timeout -k 10s 180s npm test ${TEST_EXTRA}"
    fi
    INNER_CMD="if [ ! -d /workspace/node_modules ]; then npm ci --offline --ignore-scripts --no-audit --cache /pkg-cache/npm-cache; fi && ${RUN_STEP}"
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
      PATH="/workspace/node_modules/.bin:/pkg-cache/bin:/usr/local/bin:/usr/bin:/bin" \
      HOME="/tmp" \
      CI="true" \
      npm_config_cache="/pkg-cache/npm-cache" \
      npm_config_logs_dir="/tmp/npm-logs" \
      npm_config_update_notifier="false" \
      npm_config_offline="true" \
      npm_config_ignore_scripts="true" \
      npm_config_manage_package_manager_versions="false" \
    sh -c "${INNER_CMD}" \
    > "${RAW_LOG}" 2>&1
  EXIT_CODE=$?

elif [[ "${ECOSYSTEM}" == "py" ]]; then
  if [[ "${VERIFY_MODE}" == "ruff" ]]; then
    PY_CHECK="timeout -k 10s 120s ruff check ."
  elif [[ "${VERIFY_MODE}" == "mypy" ]]; then
    PY_CHECK="timeout -k 10s 180s mypy ."
  elif [[ "${VERIFY_MODE}" == "ty" ]]; then
    PY_CHECK="timeout -k 10s 120s ty check"
  elif [[ "${VERIFY_MODE}" == "black" ]]; then
    PY_CHECK="timeout -k 10s 120s black --check ."
  else
    PY_CHECK="timeout -k 10s 180s python3 -m pytest -q --maxfail=10 --ignore=tests/e2e --ignore=test/e2e --ignore=test/functional --ignore=tests/integration"
  fi

  PY_INNER="python3 -c 'import os, re, tomllib
os.makedirs(\"/tmp/py-meta\", exist_ok=True)
if os.path.isfile(\"/workspace/pyproject.toml\"):
    d = tomllib.loads(open(\"/workspace/pyproject.toml\", \"rb\").read().decode(\"utf-8\", \"replace\"))
    p = d.get(\"project\") or (d.get(\"tool\") or {}).get(\"poetry\") or {}
    name = p.get(\"name\")
    ver = p.get(\"version\") or \"0.0.0\"
    if name:
        norm = re.sub(r\"[-_.]+|[^A-Za-z0-9]\", \"_\", name)
        di = f\"/tmp/py-meta/{norm}-{ver}.dist-info\"
        os.makedirs(di, exist_ok=True)
        open(f\"{di}/METADATA\", \"w\").write(f\"Metadata-Version: 2.1\\nName: {name}\\nVersion: {ver}\\n\")
' 2>/dev/null || true
${PY_CHECK}"

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
      PYTHONPATH="/workspace:/workspace/src:/tmp/py-meta:/pkg-cache/venv/lib/python3.12/site-packages" \
      UV_OFFLINE="1" \
    sh -c "${PY_INNER}" \
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

patterns = [
    r"AIza[0-9A-Za-z_\-]{25,}",
    r"AQ\.[0-9A-Za-z_\-]{25,}",
    r"github_pat_[0-9A-Za-z_]{30,}",
    r"gh[pousr]_[0-9A-Za-z]{25,}",
    r"sk-[0-9A-Za-z_\-]{20,}",
    r"(?i)(api[_-]?key|secret|token|password)\s*[:=]\s*['\"]?[A-Za-z0-9_\-\.]{12,}['\"]?",
]
for pat in patterns:
    text = re.sub(pat, "[REDACTED_SECRET]", text)

try:
    r = subprocess.run(
        ["gitleaks", "detect", "--no-git", "--source", raw_path, "--redact", "--exit-code", "99"],
        capture_output=True, text=True, timeout=20,
    )
    if r.returncode == 99:
        text = "[GITLEAKS_REDACTED_LOG_LINE_BLOCKED]\n" + text[-6000:]
except Exception:
    pass

if len(text) > 16000:
    text = text[:4000] + "\n... [LOG TRUNCATED TO <=4,000 TOKENS] ...\n" + text[-11500:]

open(out_path, "w", encoding="utf-8").write(text)
PY

exit "${EXIT_CODE}"
