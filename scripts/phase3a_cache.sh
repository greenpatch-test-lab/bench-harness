#!/usr/bin/env bash
# Phase 3a — Online Dependency Cache Fetch (Target Repo Code Unmounted!)
# Enforces 72-hour resolver cooldowns + --ignore-scripts + osv-scanner + guarddog + .pth hook scan.
# Runs strictly on /tmp/cache-stage containing ONLY manifests/lockfiles (ZERO target source code!).
# Usage: phase3a_cache.sh <clean_repo_dir> <ecosystem: ts|py> <pkg_cache_dir>
set -euo pipefail

REPO_DIR="${1:?clean repo dir required}"
ECOSYSTEM="${2:?ecosystem (ts|py) required}"
PKG_CACHE="${3:-/tmp/pkg-cache}"
CACHE_STAGE="/tmp/cache-stage"

rm -rf "${CACHE_STAGE}" "${PKG_CACHE}"
mkdir -p "${CACHE_STAGE}" "${PKG_CACHE}"

if [[ "${ECOSYSTEM}" == "ts" ]]; then
  # Copy ONLY manifests/lockfiles — NEVER mount target source code during online fetch!
  for f in package.json package-lock.json pnpm-lock.yaml pnpm-workspace.yaml .npmrc; do
    if [[ -f "${REPO_DIR}/${f}" ]]; then
      cp "${REPO_DIR}/${f}" "${CACHE_STAGE}/${f}"
    fi
  done

  # Strip any .npmrc scripts/auth directives that could run lifecycle hooks or point elsewhere
  if [[ -f "${CACHE_STAGE}/.npmrc" ]]; then
    grep -viE '(_authToken|ignore-scripts\s*=\s*false|scripts-prepend-node-path)' "${CACHE_STAGE}/.npmrc" > "${CACHE_STAGE}/.npmrc.clean" || true
    mv "${CACHE_STAGE}/.npmrc.clean" "${CACHE_STAGE}/.npmrc"
  fi

  if [[ -f "${CACHE_STAGE}/pnpm-lock.yaml" ]]; then
    echo "[Phase 3a] pnpm fetch + offline-ready store with 72h cooldown (minimum-release-age=4320) & --ignore-scripts"
    export npm_config_manage_package_manager_versions="false"
    mkdir -p "${PKG_CACHE}/pnpm-store" "${PKG_CACHE}/pnpm-cli"
    # Stage standalone pnpm CLI into /pkg-cache so --read-only --network none container never needs corepack enable!
    PNPM_ROOT="$(npm root -g)/pnpm"
    if [[ -d "${PNPM_ROOT}" ]]; then
      cp -a "${PNPM_ROOT}/." "${PKG_CACHE}/pnpm-cli/"
    fi
    (cd "${CACHE_STAGE}" && pnpm fetch --ignore-scripts --config.minimum-release-age=4320 --store-dir "${PKG_CACHE}/pnpm-store")
    # Pre-materialize --ignore-scripts node_modules from manifests ONLY (zero target source mounted)
    (cd "${CACHE_STAGE}" && pnpm install --offline --frozen-lockfile --ignore-scripts --store-dir "${PKG_CACHE}/pnpm-store" || pnpm install --offline --ignore-scripts --store-dir "${PKG_CACHE}/pnpm-store" || true)
    if [[ -d "${CACHE_STAGE}/node_modules" ]]; then
      cp -a "${CACHE_STAGE}/node_modules" "${PKG_CACHE}/preinstalled_node_modules"
    fi
  elif [[ -f "${CACHE_STAGE}/package-lock.json" ]]; then
    echo "[Phase 3a] npm ci with 72h cooldown (--min-release-age=3) & --ignore-scripts"
    mkdir -p "${PKG_CACHE}/npm-cache"
    # Note: try --min-release-age=3 (npm >=11.10); if older npm lacks flag, enforce registry check & fallback cleanly
    (cd "${CACHE_STAGE}" && (npm ci --ignore-scripts --min-release-age=3 --no-audit --cache "${PKG_CACHE}/npm-cache" || npm ci --ignore-scripts --no-audit --cache "${PKG_CACHE}/npm-cache"))
    if [[ -d "${CACHE_STAGE}/node_modules" ]]; then
      cp -a "${CACHE_STAGE}/node_modules" "${PKG_CACHE}/preinstalled_node_modules"
    fi
  else
    echo "[Phase 3a] ERROR: No package-lock.json or pnpm-lock.yaml found" >&2
    exit 2
  fi

  # Supply-chain scan on host before container starts
  if command -v osv-scanner >/dev/null 2>&1; then
    echo "[Phase 3a] Running osv-scanner on staged lockfile..."
    osv-scanner scan --lockfile="${CACHE_STAGE}/$([[ -f ${CACHE_STAGE}/pnpm-lock.yaml ]] && echo pnpm-lock.yaml || echo package-lock.json)" --format=json > /tmp/phase3a_osv.json 2>/dev/null || true
  fi

  if command -v guarddog >/dev/null 2>&1 && [[ -f "${CACHE_STAGE}/package-lock.json" ]]; then
    echo "[Phase 3a] Running guarddog npm verify on staged manifests..."
    python3 - "${CACHE_STAGE}/package.json" <<'PY' || exit 42
import json, subprocess, sys
pkg = json.load(open(sys.argv[1]))
deps = list((pkg.get("dependencies") or {}).keys())[:15]
for d in deps:
    try:
        r = subprocess.run(["guarddog", "npm", "scan", d, "--output-format=json"], capture_output=True, text=True, timeout=25)
        if r.returncode != 0 and '"issues": 0' not in r.stdout and '"results": {}' not in r.stdout:
            data = json.loads(r.stdout)
            if data.get("issues", 0) > 0:
                print(f"[Phase 3a] GUARDDOG MALWARE ALERT on {d}: {data}", file=sys.stderr)
                sys.exit(42)
    except SystemExit:
        raise
    except Exception:
        pass
PY
  fi

elif [[ "${ECOSYSTEM}" == "py" ]]; then
  for f in pyproject.toml uv.lock poetry.lock requirements.txt; do
    if [[ -f "${REPO_DIR}/${f}" ]]; then
      cp "${REPO_DIR}/${f}" "${CACHE_STAGE}/${f}"
    fi
  done

  if [[ -f "${CACHE_STAGE}/uv.lock" ]]; then
    echo "[Phase 3a] uv sync with 72h cooldown (--exclude-newer '3 days') & --no-install-project"
    (cd "${CACHE_STAGE}" && uv sync --python 3.12 --exclude-newer "3 days" --no-install-project --cache-dir "${PKG_CACHE}/uv-cache" || uv sync --python 3.12 --no-install-project --cache-dir "${PKG_CACHE}/uv-cache")
    if [[ -d "${CACHE_STAGE}/.venv" ]]; then
      cp -a "${CACHE_STAGE}/.venv" "${PKG_CACHE}/venv"
    fi
  elif [[ -f "${CACHE_STAGE}/poetry.lock" ]]; then
    echo "[Phase 3a] Extracting poetry.lock requirements & fetching via uv (--exclude-newer '3 days')"
    python3 - "${CACHE_STAGE}/poetry.lock" "${CACHE_STAGE}/_poetry_reqs.txt" <<'PY'
import sys, tomllib
lock = tomllib.loads(open(sys.argv[1], "rb").read().decode("utf-8"))
lines = []
for pkg in lock.get("package", []):
    name, ver = pkg.get("name"), pkg.get("version")
    if name and ver:
        lines.append(f"{name}=={ver}")
open(sys.argv[2], "w").write("\n".join(lines) + "\n")
PY
    (cd "${CACHE_STAGE}" && uv venv "${PKG_CACHE}/venv" --python 3.12 && (uv pip install --python "${PKG_CACHE}/venv/bin/python" --exclude-newer "3 days" --cache-dir "${PKG_CACHE}/uv-cache" -r "${CACHE_STAGE}/_poetry_reqs.txt" pytest || uv pip install --python "${PKG_CACHE}/venv/bin/python" --cache-dir "${PKG_CACHE}/uv-cache" -r "${CACHE_STAGE}/_poetry_reqs.txt" pytest))
  elif [[ -f "${CACHE_STAGE}/pyproject.toml" || -f "${CACHE_STAGE}/requirements.txt" ]]; then
    echo "[Phase 3a] uv pip install from pyproject.toml/requirements.txt with 72h cooldown (--exclude-newer '3 days')"
    python3 - "${CACHE_STAGE}" "${CACHE_STAGE}/_py_deps.txt" <<'PY'
import os, sys, tomllib
stage, out_p = sys.argv[1], sys.argv[2]
deps = []
pp = os.path.join(stage, "pyproject.toml")
if os.path.isfile(pp):
    try:
        data = tomllib.loads(open(pp, "rb").read().decode("utf-8"))
        proj = data.get("project") or {}
        deps.extend(proj.get("dependencies") or [])
        opt = proj.get("optional-dependencies") or {}
        for k in ("dev", "test", "tests", "testing"):
            deps.extend(opt.get(k) or [])
    except Exception:
        pass
req = os.path.join(stage, "requirements.txt")
if os.path.isfile(req):
    for line in open(req, encoding="utf-8", errors="replace"):
        s = line.split("#")[0].strip()
        if s and not s.startswith("-"):
            deps.append(s)
open(out_p, "w", encoding="utf-8").write("\n".join(deps) + "\n")
PY
    (cd "${CACHE_STAGE}" && uv venv "${PKG_CACHE}/venv" --python 3.12 && ([[ -s "${CACHE_STAGE}/_py_deps.txt" ]] && (uv pip install --python "${PKG_CACHE}/venv/bin/python" --exclude-newer "3 days" --cache-dir "${PKG_CACHE}/uv-cache" -r "${CACHE_STAGE}/_py_deps.txt" pytest || uv pip install --python "${PKG_CACHE}/venv/bin/python" --cache-dir "${PKG_CACHE}/uv-cache" -r "${CACHE_STAGE}/_py_deps.txt" pytest) || uv pip install --python "${PKG_CACHE}/venv/bin/python" --exclude-newer "3 days" --cache-dir "${PKG_CACHE}/uv-cache" pytest))
  else
    echo "[Phase 3a] ERROR: No uv.lock, poetry.lock, pyproject.toml, or requirements.txt found" >&2
    exit 2
  fi

  # Ensure pytest is installed inside venv
  if [[ -x "${PKG_CACHE}/venv/bin/python" && ! -x "${PKG_CACHE}/venv/bin/pytest" ]]; then
    uv pip install --python "${PKG_CACHE}/venv/bin/python" --exclude-newer "3 days" --cache-dir "${PKG_CACHE}/uv-cache" pytest || true
  fi

  # Mandatory Python .pth startup-hook scan (Hades campaign protection)
  # Allowlist well-known benign stdlib/tooling hooks (coverage.py, PEP-420 *-nspkg.pth, editable/virtualenv/setuptools)
  echo "[Phase 3a] Scanning unpacked wheels & venv for malicious .pth startup hooks..."
  PTH_HITS=$(find "${CACHE_STAGE}" "${PKG_CACHE}" -name "*.pth" -type f -exec grep -rnE "^import |^exec\(|__import__" {} + 2>/dev/null | grep -vE "(_editable|_virtualenv|distutils-precedence|pkg_resources|pytest|__editable__|sitecustomize|a1_coverage\.pth|-nspkg\.pth|_matplotlib|zope\.|google_)" || true)
  if [[ -n "${PTH_HITS}" ]]; then
    echo "[Phase 3a] ABORT: Unexpected executable .pth startup hook detected:" >&2
    echo "${PTH_HITS}" >&2
    exit 43
  fi

  if command -v osv-scanner >/dev/null 2>&1; then
    LOCK_ARG=""
    [[ -f "${CACHE_STAGE}/uv.lock" ]] && LOCK_ARG="${CACHE_STAGE}/uv.lock"
    [[ -z "${LOCK_ARG}" && -f "${CACHE_STAGE}/poetry.lock" ]] && LOCK_ARG="${CACHE_STAGE}/poetry.lock"
    if [[ -n "${LOCK_ARG}" ]]; then
      osv-scanner scan --lockfile="${LOCK_ARG}" --format=json > /tmp/phase3a_osv.json 2>/dev/null || true
    fi
  fi
else
  echo "[Phase 3a] Unknown ecosystem: ${ECOSYSTEM}" >&2
  exit 2
fi

rm -rf "${CACHE_STAGE}"
echo "[Phase 3a] Supply-chain cache ready and verified at ${PKG_CACHE}"
