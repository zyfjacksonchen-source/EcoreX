#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT=""
RUNTIME_DIR=""
RUNTIME_CACHE_DIR=""
PYTHON_ARCH=""
PYTHON_STANDALONE_URL="${PYTHON_STANDALONE_URL:-}"
PYTHON_STANDALONE_RELEASE="${PYTHON_STANDALONE_RELEASE:-20260602}"
PYTHON_MINOR="${PYTHON_MINOR:-3.11}"
PREINSTALL_PACKS=""
SKIP_DEPENDENCY_INSTALL=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo-root)
      REPO_ROOT="$2"
      shift 2
      ;;
    --runtime-dir)
      RUNTIME_DIR="$2"
      shift 2
      ;;
    --runtime-cache-dir)
      RUNTIME_CACHE_DIR="$2"
      shift 2
      ;;
    --python-arch)
      PYTHON_ARCH="$2"
      shift 2
      ;;
    --python-standalone-url)
      PYTHON_STANDALONE_URL="$2"
      shift 2
      ;;
    --python-standalone-release)
      PYTHON_STANDALONE_RELEASE="$2"
      shift 2
      ;;
    --python-minor)
      PYTHON_MINOR="$2"
      shift 2
      ;;
    --preinstall-packs)
      PREINSTALL_PACKS="$2"
      shift 2
      ;;
    --skip-dependency-install)
      SKIP_DEPENDENCY_INSTALL=1
      shift
      ;;
    *)
      echo "Unknown argument: $1" >&2
      exit 2
      ;;
  esac
done

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "stage-runtime-mac.sh must run on macOS." >&2
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DESKTOP_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
if [[ -z "$REPO_ROOT" ]]; then
  REPO_ROOT="$(cd "$DESKTOP_ROOT/.." && pwd)"
else
  REPO_ROOT="$(cd "$REPO_ROOT" && pwd)"
fi
if [[ -z "$RUNTIME_DIR" ]]; then
  RUNTIME_DIR="$DESKTOP_ROOT/runtime/ecorex-runtime"
fi
if [[ -z "$RUNTIME_CACHE_DIR" ]]; then
  RUNTIME_CACHE_DIR="$DESKTOP_ROOT/.runtime-cache"
fi
if [[ -z "$PYTHON_ARCH" ]]; then
  case "$(uname -m)" in
    arm64|aarch64) PYTHON_ARCH="arm64" ;;
    x86_64|amd64) PYTHON_ARCH="x64" ;;
    *) echo "Unsupported host arch: $(uname -m)" >&2; exit 1 ;;
  esac
fi

case "$PYTHON_ARCH" in
  arm64|aarch64)
    PYTHON_TRIPLE="aarch64-apple-darwin"
    BUILDER_ARCH="arm64"
    ;;
  x64|x86_64|amd64)
    PYTHON_TRIPLE="x86_64-apple-darwin"
    BUILDER_ARCH="x64"
    ;;
  *)
    echo "Unsupported python arch: $PYTHON_ARCH" >&2
    exit 1
    ;;
esac

resolve_python_url() {
  if [[ -n "$PYTHON_STANDALONE_URL" ]]; then
    printf '%s\n' "$PYTHON_STANDALONE_URL"
    return
  fi

  local api_url
  if [[ "$PYTHON_STANDALONE_RELEASE" == "latest" ]]; then
    api_url="https://api.github.com/repos/astral-sh/python-build-standalone/releases/latest"
  else
    api_url="https://api.github.com/repos/astral-sh/python-build-standalone/releases/tags/$PYTHON_STANDALONE_RELEASE"
  fi

  python3 - "$api_url" "$PYTHON_MINOR" "$PYTHON_TRIPLE" <<'PY'
import json
import os
import sys
import urllib.request

api_url, minor, triple = sys.argv[1:4]
request = urllib.request.Request(api_url)
token = os.environ.get("PYTHON_STANDALONE_GITHUB_TOKEN") or os.environ.get("GITHUB_TOKEN")
if token:
    request.add_header("Authorization", f"Bearer {token}")
request.add_header("Accept", "application/vnd.github+json")
request.add_header("X-GitHub-Api-Version", "2022-11-28")
with urllib.request.urlopen(request, timeout=60) as response:
    data = json.load(response)

assets = data.get("assets", [])
names = []
for asset in assets:
    name = asset.get("name", "")
    url = asset.get("browser_download_url", "")
    if not url:
        continue
    if not name.startswith(f"cpython-{minor}."):
        continue
    if triple not in name:
        continue
    if not name.endswith(".tar.gz"):
        continue
    if "install_only_stripped" in name:
        names.insert(0, (name, url))
    elif "install_only" in name:
        names.append((name, url))

if not names:
    raise SystemExit(f"No python-build-standalone asset found for {minor} {triple}")

print(names[0][1])
PY
}

copy_runtime_sources() {
  local source_dirs=(
    agent
    bridge
    channel
    cli
    common
    models
    plugins
    skills
    translate
    voice
  )
  local source_files=(
    app.py
    config.py
    config-template.json
    requirements.txt
    requirements-optional.txt
    pyproject.toml
    LICENSE
  )

  for dir in "${source_dirs[@]}"; do
    if [[ -d "$REPO_ROOT/$dir" ]]; then
      cp -R "$REPO_ROOT/$dir" "$RUNTIME_DIR/$dir"
    fi
  done

  for file in "${source_files[@]}"; do
    if [[ -f "$REPO_ROOT/$file" ]]; then
      cp "$REPO_ROOT/$file" "$RUNTIME_DIR/$file"
    fi
  done

  cp "$DESKTOP_ROOT/runtime-packs/core-requirements.txt" "$RUNTIME_DIR/core-requirements.txt"
  cp "$DESKTOP_ROOT/runtime-packs/capabilities.json" "$RUNTIME_DIR/capabilities.json"
  mkdir -p "$RUNTIME_DIR/scripts"
  cp "$SCRIPT_DIR/install-capability.py" "$RUNTIME_DIR/scripts/install-capability.py"

  if [[ "${ECOREX_DISABLE_ENTERPRISE_POLICY:-}" != "1" ]]; then
    local admin_base admin_events_url model_config_url capability_policy_url policy_path
    admin_base="${ECOREX_ADMIN_BASE_URL:-https://www.ecoreai.cn/ecorex-agent}"
    admin_events_url="${ECOREX_ADMIN_EVENTS_URL:-$admin_base/client/events}"
    model_config_url="${ECOREX_MODEL_CONFIG_URL:-$admin_base/client/model-config}"
    capability_policy_url="${ECOREX_CAPABILITY_POLICY_URL:-$admin_base/client/capability-policy}"
    policy_path="$RUNTIME_DIR/enterprise-policy.json"
    python3 - "$policy_path" <<'PY'
import json
import os
import sys

out = sys.argv[1]
policy = {
    "adminEventsUrl": os.environ.get("ECOREX_ADMIN_EVENTS_URL") or os.environ.get("ECOREX_ADMIN_BASE_URL", "https://www.ecoreai.cn/ecorex-agent").rstrip("/") + "/client/events",
    "modelConfigUrl": os.environ.get("ECOREX_MODEL_CONFIG_URL") or os.environ.get("ECOREX_ADMIN_BASE_URL", "https://www.ecoreai.cn/ecorex-agent").rstrip("/") + "/client/model-config",
    "capabilityPolicyUrl": os.environ.get("ECOREX_CAPABILITY_POLICY_URL") or os.environ.get("ECOREX_ADMIN_BASE_URL", "https://www.ecoreai.cn/ecorex-agent").rstrip("/") + "/client/capability-policy",
    "clientEventKey": os.environ.get("ECOREX_CLIENT_EVENT_KEY") or "ecorex-desktop-v0.1.12",
    "userEmail": os.environ.get("ECOREX_USER_EMAIL"),
    "deviceId": os.environ.get("ECOREX_DEVICE_ID"),
    "orgId": os.environ.get("ECOREX_ORG_ID"),
}
policy = {key: value for key, value in policy.items() if value}
with open(out, "w", encoding="utf-8") as fh:
    json.dump(policy, fh, ensure_ascii=False, indent=2)
PY
    echo "Enterprise policy staged for EcoreX desktop."
  fi
}

stage_python() {
  mkdir -p "$RUNTIME_CACHE_DIR"
  local url archive extract_dir found source_root
  url="$(resolve_python_url)"
  archive="$RUNTIME_CACHE_DIR/$(basename "$url")"
  if [[ ! -f "$archive" ]]; then
    echo "Downloading $url"
    curl -L --fail --retry 3 --output "$archive" "$url"
  fi

  extract_dir="$(mktemp -d "$RUNTIME_CACHE_DIR/python-standalone.XXXXXX")"
  tar -xzf "$archive" -C "$extract_dir"

  if [[ -x "$extract_dir/python/install/bin/python3" ]]; then
    source_root="$extract_dir/python/install"
  elif [[ -x "$extract_dir/python/bin/python3" ]]; then
    source_root="$extract_dir/python"
  else
    found="$(find "$extract_dir" -path "*/bin/python3" -type f -perm -111 2>/dev/null | head -n 1 || true)"
    if [[ -z "$found" ]]; then
      echo "Could not locate python3 in $archive" >&2
      exit 1
    fi
    source_root="$(cd "$(dirname "$found")/.." && pwd)"
  fi

  mv "$source_root" "$RUNTIME_DIR/python"
  rm -rf "$extract_dir"
}

rm -rf "$RUNTIME_DIR"
mkdir -p "$RUNTIME_DIR"
copy_runtime_sources
stage_python

RUNTIME_PYTHON="$RUNTIME_DIR/python/bin/python3"
if [[ ! -x "$RUNTIME_PYTHON" ]]; then
  echo "Runtime python not found at $RUNTIME_PYTHON" >&2
  exit 1
fi

export PYTHONNOUSERSITE=1
export PYTHONPATH="$RUNTIME_DIR${PYTHONPATH:+:$PYTHONPATH}"

if [[ "$SKIP_DEPENDENCY_INSTALL" != "1" ]]; then
  if ! "$RUNTIME_PYTHON" -m pip --version >/dev/null 2>&1; then
    "$RUNTIME_PYTHON" -m ensurepip --upgrade
  fi
  "$RUNTIME_PYTHON" -m pip install --upgrade pip --no-cache-dir --no-warn-script-location
  "$RUNTIME_PYTHON" -m pip install --no-cache-dir --no-warn-script-location -r "$RUNTIME_DIR/core-requirements.txt"
fi

if [[ -n "$PREINSTALL_PACKS" ]]; then
  IFS=',' read -r -a packs <<< "$PREINSTALL_PACKS"
  for pack in "${packs[@]}"; do
    pack="$(echo "$pack" | xargs)"
    [[ -z "$pack" ]] && continue
    echo "Preinstalling capability pack $pack"
    "$RUNTIME_PYTHON" "$RUNTIME_DIR/scripts/install-capability.py" \
      --pack-id "$pack" \
      --runtime-dir "$RUNTIME_DIR" \
      --manifest "$RUNTIME_DIR/capabilities.json"
  done
fi

find "$RUNTIME_DIR" -type d -name "__pycache__" -prune -exec rm -rf {} +

"$RUNTIME_PYTHON" - "$RUNTIME_DIR/runtime-manifest.json" "$REPO_ROOT" "$BUILDER_ARCH" "$PYTHON_STANDALONE_RELEASE" <<'PY'
import json
import sys
import time
from pathlib import Path

out, repo_root, arch, release = sys.argv[1:5]
manifest = {
    "product": "EcoreX",
    "runtime": "compatible-agent-runtime",
    "stagedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    "repoRoot": repo_root,
    "pythonDistribution": f"python-build-standalone-{release}",
    "pythonArch": arch,
    "dependencyInstall": True,
}
Path(out).write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
PY

echo "EcoreX macOS runtime staged at $RUNTIME_DIR for $BUILDER_ARCH"
