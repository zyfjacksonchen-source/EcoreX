#!/usr/bin/env bash
set -euo pipefail

VERSION="${VERSION:-0.2.7}"
SERVICE_NAME="${SERVICE_NAME:-ecorex-web}"
SERVICE_USER="${SERVICE_USER:-ecorex}"
SERVICE_GROUP="${SERVICE_GROUP:-$SERVICE_USER}"
INSTALL_ROOT="${INSTALL_ROOT:-/opt/ecorex-web}"
WORKSPACE_ROOT="${WORKSPACE_ROOT:-/srv/ecorex-agent-workspace}"
STATE_DIR="${STATE_DIR:-$INSTALL_ROOT/state}"
VENV_DIR="${VENV_DIR:-$INSTALL_ROOT/venv}"
ENV_DIR="${ENV_DIR:-/etc/ecorex-web}"
ENV_FILE="${ENV_FILE:-$ENV_DIR/ecorex-web.env}"
SERVICE_FILE="${SERVICE_FILE:-/etc/systemd/system/$SERVICE_NAME.service}"
WEB_HOST="${WEB_HOST:-127.0.0.1}"
WEB_PORT="${WEB_PORT:-9909}"
PUBLIC_BASE_URL="${PUBLIC_BASE_URL:-}"
RELEASE_BASE_URL="${RELEASE_BASE_URL:-https://mvdcm.ecoremedia.net/ecorex-agent/downloads}"
RELEASE_URL="${RELEASE_URL:-$RELEASE_BASE_URL/EcoreX_${VERSION}-web-linux-service.tar.gz}"
EXPECTED_SHA256="${EXPECTED_SHA256:-}"
START_SERVICE="${START_SERVICE:-1}"
OPEN_BROWSER="${OPEN_BROWSER:-1}"
INSTALL_PY_DEPS="${INSTALL_PY_DEPS:-1}"
INSTALL_NODE_RUNTIME="${INSTALL_NODE_RUNTIME:-1}"
ECOREX_NODE_VERSION="${ECOREX_NODE_VERSION:-22.22.0}"
NODE_DIST_BASE_URL="${NODE_DIST_BASE_URL:-https://nodejs.org/dist}"
NODE_ARCHIVE_PATH="${NODE_ARCHIVE_PATH:-}"
NODE_ARCHIVE_SHA256="${NODE_ARCHIVE_SHA256:-}"
INSTALL_LOCK_DIR="${INSTALL_LOCK_DIR:-/var/lock/ecorex-web-install.lock}"
INSTALL_LOCK_TIMEOUT_SECONDS="${INSTALL_LOCK_TIMEOUT_SECONDS:-900}"
INSTALL_LOCK_STALE_SECONDS="${INSTALL_LOCK_STALE_SECONDS:-3600}"
TARBALL_PATH="${1:-${TARBALL_PATH:-}}"

tmp_dir=""
INSTALL_LOCK_ACQUIRED=0

log() {
  printf '%s\n' "$*"
}

fail() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || fail "Missing required command: $1"
}

release_install_lock() {
  if [[ "$INSTALL_LOCK_ACQUIRED" == "1" ]]; then
    rm -rf -- "$INSTALL_LOCK_DIR"
    INSTALL_LOCK_ACQUIRED=0
  fi
}

cleanup() {
  if [[ -n "$tmp_dir" ]]; then
    rm -rf "$tmp_dir"
  fi
  release_install_lock
}

acquire_install_lock() {
  local start
  local now
  local age
  local pid_file="$INSTALL_LOCK_DIR/pid"
  local stamp_file="$INSTALL_LOCK_DIR/created_at"

  mkdir -p "$(dirname "$INSTALL_LOCK_DIR")"
  start="$(date +%s)"
  while ! mkdir "$INSTALL_LOCK_DIR" 2>/dev/null; do
    now="$(date +%s)"
    age=0
    if [[ -f "$stamp_file" ]]; then
      age=$((now - $(cat "$stamp_file" 2>/dev/null || echo "$now")))
    fi
    if [[ "$age" -gt "$INSTALL_LOCK_STALE_SECONDS" ]]; then
      log "Removing stale install lock: $INSTALL_LOCK_DIR"
      rm -rf -- "$INSTALL_LOCK_DIR"
      continue
    fi
    if [[ $((now - start)) -gt "$INSTALL_LOCK_TIMEOUT_SECONDS" ]]; then
      fail "Another EcoreX Web install is still running (lock: $INSTALL_LOCK_DIR). Try again later."
    fi
    log "Another EcoreX Web install is running; waiting for lock $INSTALL_LOCK_DIR..."
    sleep 3
  done

  INSTALL_LOCK_ACQUIRED=1
  date +%s > "$stamp_file"
  printf '%s\n' "$$" > "$pid_file"
}

if [[ "$(id -u)" != "0" ]]; then
  fail "Run this installer as root because it writes $INSTALL_ROOT, $WORKSPACE_ROOT, $ENV_FILE, and systemd units."
fi

need_cmd python3
need_cmd tar
trap cleanup EXIT
acquire_install_lock

calc_sha256() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | awk '{print toupper($1)}'
  else
    python3 - "$1" <<'PY'
import hashlib
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
digest = hashlib.sha256()
with path.open("rb") as handle:
    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
        digest.update(chunk)
print(digest.hexdigest().upper())
PY
  fi
}

random_secret() {
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -base64 24 | tr -d '\n'
  else
    python3 - <<'PY'
import secrets
print(secrets.token_urlsafe(32))
PY
  fi
}

read_env_value() {
  local key="$1"
  local fallback="$2"
  if [[ -f "$ENV_FILE" ]]; then
    local line
    line="$(grep -E "^${key}=" "$ENV_FILE" | tail -n 1 || true)"
    if [[ -n "$line" ]]; then
      printf '%s' "${line#*=}"
      return
    fi
  fi
  printf '%s' "$fallback"
}

upsert_env_value() {
  local key="$1"
  local value="$2"
  if [[ -f "$ENV_FILE" ]] && grep -q "^${key}=" "$ENV_FILE"; then
    python3 - "$ENV_FILE" "$key" "$value" <<'PY'
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
key = sys.argv[2]
value = sys.argv[3]
lines = path.read_text(encoding="utf-8").splitlines()
prefix = key + "="
next_lines = [prefix + value if line.startswith(prefix) else line for line in lines]
path.write_text("\n".join(next_lines).rstrip() + "\n", encoding="utf-8")
PY
  else
    printf '%s=%s\n' "$key" "$value" >> "$ENV_FILE"
  fi
}

ensure_user() {
  if ! getent group "$SERVICE_GROUP" >/dev/null 2>&1; then
    groupadd --system "$SERVICE_GROUP"
  fi
  if ! id -u "$SERVICE_USER" >/dev/null 2>&1; then
    useradd --system --gid "$SERVICE_GROUP" --home-dir "$WORKSPACE_ROOT" --shell /usr/sbin/nologin "$SERVICE_USER"
  fi
}

download_release() {
  local target="$1"
  if [[ -n "$TARBALL_PATH" ]]; then
    [[ -f "$TARBALL_PATH" ]] || fail "Release tarball not found: $TARBALL_PATH"
    cp "$TARBALL_PATH" "$target"
    return
  fi

  if [[ -z "$EXPECTED_SHA256" ]]; then
    fail "EXPECTED_SHA256 is required for online Web release installs. Set TARBALL_PATH for a local package or provide the pinned SHA256."
  fi

  if command -v curl >/dev/null 2>&1; then
    curl -fL --retry 3 --connect-timeout 20 -o "$target" "$RELEASE_URL"
  elif command -v wget >/dev/null 2>&1; then
    wget -O "$target" "$RELEASE_URL"
  else
    fail "Missing curl or wget for online install. Set TARBALL_PATH to install from a local tarball."
  fi
}

download_url() {
  local url="$1"
  local target="$2"
  if command -v curl >/dev/null 2>&1; then
    curl -fL --retry 3 --connect-timeout 20 -o "$target" "$url"
  elif command -v wget >/dev/null 2>&1; then
    wget -O "$target" "$url"
  else
    fail "Missing curl or wget for online install. Provide a local archive path instead."
  fi
}

safe_extract_tar() {
  local tarball="$1"
  local target="$2"
  python3 - "$tarball" "$target" <<'PY'
import pathlib
import sys
import tarfile

tarball = pathlib.Path(sys.argv[1]).resolve()
target = pathlib.Path(sys.argv[2]).resolve()
target.mkdir(parents=True, exist_ok=True)

with tarfile.open(tarball, "r:gz") as archive:
    for member in archive.getmembers():
        name = member.name.replace("\\", "/")
        if name.startswith("/") or name.startswith("../") or "/../" in name:
            raise SystemExit(f"Unsafe tar member path: {member.name}")
        destination = (target / name).resolve()
        if target not in (destination, *destination.parents):
            raise SystemExit(f"Unsafe tar member path: {member.name}")
    archive.extractall(target)
PY
}

find_bundle_root() {
  local extracted="$1"
  if [[ -f "$extracted/runtime/app.py" ]]; then
    printf '%s' "$extracted"
    return
  fi

  local candidate
  while IFS= read -r candidate; do
    if [[ -f "$candidate/runtime/app.py" ]]; then
      printf '%s' "$candidate"
      return
    fi
  done < <(find "$extracted" -mindepth 1 -maxdepth 2 -type d | sort)

  fail "Release tarball does not contain runtime/app.py"
}

node_arch() {
  local machine
  machine="$(uname -m)"
  case "$machine" in
    x86_64|amd64) printf 'x64' ;;
    aarch64|arm64) printf 'arm64' ;;
    *) fail "Unsupported Linux architecture for bundled Node runtime: $machine" ;;
  esac
}

node_runtime_ready() {
  local root="$1"
  [[ -x "$root/bin/node" && -x "$root/bin/npm" && -x "$root/bin/npx" ]]
}

extract_node_archive() {
  local archive="$1"
  local target="$2"
  python3 - "$archive" "$target" <<'PY'
import os
import pathlib
import shutil
import sys
import tarfile

archive = pathlib.Path(sys.argv[1]).resolve()
target = pathlib.Path(sys.argv[2]).resolve()
target.mkdir(parents=True, exist_ok=True)


def ensure_within(path: pathlib.Path) -> None:
    if target not in (path, *path.parents):
        raise SystemExit(f"Unsafe Node archive member path: {path}")


def safe_mode(member: tarfile.TarInfo) -> int:
    executable = bool(member.mode & 0o111)
    return 0o755 if executable else 0o644

with tarfile.open(archive, "r:*") as tar:
    members = tar.getmembers()
    if not members:
        raise SystemExit("Node archive is empty")
    for member in members:
        name = member.name.replace("\\", "/")
        if name.startswith("/") or name.startswith("../") or "/../" in name:
            raise SystemExit(f"Unsafe Node archive member path: {member.name}")
        parts = pathlib.PurePosixPath(name).parts
        if len(parts) < 2:
            if member.isdir():
                continue
            raise SystemExit(f"Unsupported Node archive top-level member: {member.name}")
        stripped = pathlib.PurePosixPath(*parts[1:])
        if not stripped.parts:
            continue
        destination = (target / pathlib.Path(*stripped.parts)).resolve()
        ensure_within(destination)
        if member.isdir():
            destination.mkdir(parents=True, exist_ok=True)
            os.chmod(destination, 0o755)
            continue
        if member.isfile():
            destination.parent.mkdir(parents=True, exist_ok=True)
            source = tar.extractfile(member)
            if source is None:
                raise SystemExit(f"Could not read Node archive file: {member.name}")
            with source, destination.open("wb") as handle:
                shutil.copyfileobj(source, handle)
            os.chmod(destination, safe_mode(member))
            continue
        if member.issym():
            link = (member.linkname or "").replace("\\", "/")
            if link.startswith("/"):
                raise SystemExit(f"Unsafe Node archive link target: {member.name}")
            link_destination = (destination.parent / link).resolve()
            ensure_within(link_destination)
            destination.parent.mkdir(parents=True, exist_ok=True)
            try:
                destination.unlink()
            except FileNotFoundError:
                pass
            os.symlink(member.linkname, destination)
            continue
        raise SystemExit(f"Unsupported Node archive member type: {member.name}")
PY
}

verify_node_archive() {
  local archive="$1"
  local archive_name="$2"
  local downloaded="$3"
  local expected="$NODE_ARCHIVE_SHA256"
  if [[ -z "$expected" && "$downloaded" == "1" ]]; then
    local sums="$tmp_dir/SHASUMS256.txt"
    download_url "$NODE_DIST_BASE_URL/v${ECOREX_NODE_VERSION}/SHASUMS256.txt" "$sums"
    expected="$(awk -v name="$archive_name" '$2 == name {print toupper($1)}' "$sums")"
    if [[ -z "$expected" ]]; then
      fail "Node archive $archive_name was not present in SHASUMS256.txt"
    fi
  fi
  if [[ -z "$expected" ]]; then
    log "Node archive SHA256 not provided; relying on release package checksum or TLS for local archive."
    return
  fi
  local actual
  actual="$(calc_sha256 "$archive")"
  if [[ "$actual" != "${expected^^}" ]]; then
    fail "Node archive SHA256 mismatch: expected ${expected^^}, got $actual"
  fi
}

install_node_runtime() {
  local bundle_root="$1"
  if [[ "$INSTALL_NODE_RUNTIME" != "1" ]]; then
    log "Node runtime install disabled. Strict Web core baseline may fail if node/npm/npx are not already owned by EcoreX."
    return
  fi

  local node_root="$INSTALL_ROOT/node"
  if node_runtime_ready "$node_root"; then
    log "EcoreX-owned Node runtime already available at $node_root"
    return
  fi

  local arch
  arch="$(node_arch)"
  local archive_name="node-v${ECOREX_NODE_VERSION}-linux-${arch}.tar.xz"
  local archive_path=""
  local downloaded_archive=0
  if [[ -n "$NODE_ARCHIVE_PATH" ]]; then
    [[ -f "$NODE_ARCHIVE_PATH" ]] || fail "NODE_ARCHIVE_PATH does not exist: $NODE_ARCHIVE_PATH"
    archive_path="$NODE_ARCHIVE_PATH"
  elif [[ -f "$bundle_root/node/$archive_name" ]]; then
    archive_path="$bundle_root/node/$archive_name"
  elif [[ -f "$bundle_root/node/${archive_name%.tar.xz}.tar.gz" ]]; then
    archive_path="$bundle_root/node/${archive_name%.tar.xz}.tar.gz"
  else
    archive_path="$tmp_dir/$archive_name"
    download_url "$NODE_DIST_BASE_URL/v${ECOREX_NODE_VERSION}/$archive_name" "$archive_path"
    downloaded_archive=1
  fi
  verify_node_archive "$archive_path" "$(basename "$archive_path")" "$downloaded_archive"

  local versioned_root="$INSTALL_ROOT/node-v${ECOREX_NODE_VERSION}-linux-${arch}"
  local tmp_node_root="$versioned_root.tmp.$$"
  rm -rf -- "$tmp_node_root"
  install -d -m 0755 -o root -g root "$tmp_node_root"
  extract_node_archive "$archive_path" "$tmp_node_root"
  if ! node_runtime_ready "$tmp_node_root"; then
    rm -rf -- "$tmp_node_root"
    fail "Node archive did not provide bin/node, bin/npm, and bin/npx"
  fi
  rm -rf -- "$versioned_root"
  mv "$tmp_node_root" "$versioned_root"
  ln -sfn "$versioned_root" "$node_root"
  log "Installed EcoreX-owned Node runtime: $node_root"
}

install_playwright_chromium_runtime() {
  local browsers_dir="$STATE_DIR/playwright-browsers"
  install -d -m 0750 -o "$SERVICE_USER" -g "$SERVICE_GROUP" "$browsers_dir"

  playwright_chromium_smoke() {
    PLAYWRIGHT_BROWSERS_PATH="$browsers_dir" "$VENV_DIR/bin/python" - <<'PY'
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
    page = browser.new_page()
    page.goto("data:text/html,<title>EcoreX Browser Smoke</title><h1>ok</h1>", wait_until="domcontentloaded", timeout=15000)
    if page.title() != "EcoreX Browser Smoke":
        raise SystemExit(2)
    browser.close()
PY
  }

  if "$VENV_DIR/bin/python" - "$browsers_dir" <<'PY'
import pathlib
import sys

root = pathlib.Path(sys.argv[1])
patterns = [
    "chromium-*/chrome-linux/chrome",
    "chromium_headless_shell-*/chrome-linux/headless_shell",
]
raise SystemExit(0 if any(any(root.glob(pattern)) for pattern in patterns) else 1)
PY
  then
    if playwright_chromium_smoke; then
      chown -R "$SERVICE_USER:$SERVICE_GROUP" "$browsers_dir"
      log "Playwright Chromium runtime already available at $browsers_dir"
      return
    fi
    log "Playwright Chromium exists but native launch smoke failed; repairing browser dependencies."
  fi
  log "Installing EcoreX-owned Playwright Chromium runtime at $browsers_dir"
  PLAYWRIGHT_BROWSERS_PATH="$browsers_dir" "$VENV_DIR/bin/python" -m playwright install --with-deps chromium
  chown -R "$SERVICE_USER:$SERVICE_GROUP" "$browsers_dir"
  playwright_chromium_smoke
}

run_web_core_baseline_gate() {
  local runtime_dir="$1"
  local checker="$runtime_dir/scripts/check-web-core-runtime-baseline.py"
  if [[ ! -f "$checker" ]]; then
    fail "Web core runtime baseline checker is missing: $checker"
  fi
  ECOREX_INSTALL_ROOT="$INSTALL_ROOT" \
  ECOREX_STATE_DIR="$STATE_DIR" \
  ECOREX_NODE_ROOT="$INSTALL_ROOT/node" \
  ECOREX_PYTHON_PATH="$VENV_DIR/bin/python" \
  "$VENV_DIR/bin/python" "$checker" \
    --runtime-root "$runtime_dir" \
    --state-root "$STATE_DIR" \
    --output "$STATE_DIR/runtime-baseline.json" \
    --strict
}

run_web_release_gate() {
  local runtime_dir="$1"
  local generator="$runtime_dir/scripts/generate-web-runtime-release-gate.py"
  if [[ ! -f "$generator" ]]; then
    fail "Web release gate generator is missing: $generator"
  fi
  ECOREX_INSTALL_ROOT="$INSTALL_ROOT" \
  ECOREX_STATE_DIR="$STATE_DIR" \
  ECOREX_NODE_ROOT="$INSTALL_ROOT/node" \
  ECOREX_PYTHON_PATH="$VENV_DIR/bin/python" \
  "$VENV_DIR/bin/python" "$generator" \
    --runtime-root "$runtime_dir" \
    --state-root "$STATE_DIR" \
    --workspace-root "$WORKSPACE_ROOT" \
    --output-dir "$STATE_DIR" \
    --baseline-input "$STATE_DIR/runtime-baseline.json" \
    --strict
}

write_env_file() {
  install -d -m 0750 -o root -g "$SERVICE_GROUP" "$ENV_DIR"

  local existing_password
  existing_password="$(read_env_value WEB_PASSWORD "")"
  if [[ -z "$existing_password" ]]; then
    existing_password="$(random_secret)"
  fi

  local public_base
  local client_base
  local tongxin_auth_url
  public_base="${PUBLIC_BASE_URL%/}"
  client_base=""
  if [[ -n "$public_base" ]]; then
    client_base="$public_base/client"
  fi
  tongxin_auth_url="${ECOREX_TONGXIN_AUTH_URL:-}"
  if [[ -z "$tongxin_auth_url" && -n "$public_base" ]]; then
    if [[ "$public_base" == */ecorex-agent ]]; then
      tongxin_auth_url="$public_base/client/tongxin/auth"
    else
      tongxin_auth_url="$public_base/ecorex-agent/client/tongxin/auth"
    fi
  fi

  if [[ ! -f "$ENV_FILE" ]]; then
    cat > "$ENV_FILE" <<EOF
CHANNEL_TYPE=web
WEB_HOST=$WEB_HOST
WEB_PORT=$WEB_PORT
WEB_PASSWORD=$existing_password
AGENT_WORKSPACE=$WORKSPACE_ROOT
ECOREX_WEB_PUBLIC_BASE_URL=$PUBLIC_BASE_URL
ECOREX_WEB_CLIENT_BASE=$client_base
ECOREX_TONGXIN_AUTH_URL=$tongxin_auth_url
ECOREX_TONGXIN_DATABASE=
ECOREX_TOOL_EXECUTION_LEASE_SECONDS=900
ECOREX_TOOL_EXECUTION_EXTENSION_SECONDS=900
ECOREX_TOOL_EXECUTION_MAX_SECONDS=5400
ECOREX_BASH_MAX_TIMEOUT_SECONDS=7200
PYTHONUNBUFFERED=1
EOF
    chmod 0640 "$ENV_FILE"
    chown root:"$SERVICE_GROUP" "$ENV_FILE"
  else
    grep -q '^CHANNEL_TYPE=' "$ENV_FILE" || printf '\nCHANNEL_TYPE=web\n' >> "$ENV_FILE"
    grep -q '^WEB_HOST=' "$ENV_FILE" || printf 'WEB_HOST=%s\n' "$WEB_HOST" >> "$ENV_FILE"
    grep -q '^WEB_PORT=' "$ENV_FILE" || printf 'WEB_PORT=%s\n' "$WEB_PORT" >> "$ENV_FILE"
    grep -q '^WEB_PASSWORD=' "$ENV_FILE" || printf 'WEB_PASSWORD=%s\n' "$existing_password" >> "$ENV_FILE"
    grep -q '^AGENT_WORKSPACE=' "$ENV_FILE" || printf 'AGENT_WORKSPACE=%s\n' "$WORKSPACE_ROOT" >> "$ENV_FILE"
    grep -q '^PYTHONUNBUFFERED=' "$ENV_FILE" || printf 'PYTHONUNBUFFERED=1\n' >> "$ENV_FILE"
    grep -q '^ECOREX_TOOL_EXECUTION_LEASE_SECONDS=' "$ENV_FILE" || printf 'ECOREX_TOOL_EXECUTION_LEASE_SECONDS=900\n' >> "$ENV_FILE"
    grep -q '^ECOREX_TOOL_EXECUTION_EXTENSION_SECONDS=' "$ENV_FILE" || printf 'ECOREX_TOOL_EXECUTION_EXTENSION_SECONDS=900\n' >> "$ENV_FILE"
    grep -q '^ECOREX_TOOL_EXECUTION_MAX_SECONDS=' "$ENV_FILE" || printf 'ECOREX_TOOL_EXECUTION_MAX_SECONDS=5400\n' >> "$ENV_FILE"
    grep -q '^ECOREX_BASH_MAX_TIMEOUT_SECONDS=' "$ENV_FILE" || printf 'ECOREX_BASH_MAX_TIMEOUT_SECONDS=7200\n' >> "$ENV_FILE"
    if [[ -n "$PUBLIC_BASE_URL" ]]; then
      upsert_env_value ECOREX_WEB_PUBLIC_BASE_URL "$PUBLIC_BASE_URL"
      upsert_env_value ECOREX_WEB_CLIENT_BASE "$client_base"
      upsert_env_value ECOREX_TONGXIN_AUTH_URL "$tongxin_auth_url"
      grep -q '^ECOREX_TONGXIN_DATABASE=' "$ENV_FILE" || printf 'ECOREX_TONGXIN_DATABASE=\n' >> "$ENV_FILE"
    else
      grep -q '^ECOREX_WEB_PUBLIC_BASE_URL=' "$ENV_FILE" || printf 'ECOREX_WEB_PUBLIC_BASE_URL=\n' >> "$ENV_FILE"
      grep -q '^ECOREX_WEB_CLIENT_BASE=' "$ENV_FILE" || printf 'ECOREX_WEB_CLIENT_BASE=\n' >> "$ENV_FILE"
      grep -q '^ECOREX_TONGXIN_AUTH_URL=' "$ENV_FILE" || printf 'ECOREX_TONGXIN_AUTH_URL=\n' >> "$ENV_FILE"
      grep -q '^ECOREX_TONGXIN_DATABASE=' "$ENV_FILE" || printf 'ECOREX_TONGXIN_DATABASE=\n' >> "$ENV_FILE"
    fi
  fi
}

write_runtime_config() {
  local runtime_dir="$1"
  local password
  local effective_host
  local effective_port
  local tongxin_auth_url
  local tongxin_database

  password="$(read_env_value WEB_PASSWORD "")"
  effective_host="$(read_env_value WEB_HOST "$WEB_HOST")"
  effective_port="$(read_env_value WEB_PORT "$WEB_PORT")"
  tongxin_auth_url="$(read_env_value ECOREX_TONGXIN_AUTH_URL "")"
  tongxin_database="$(read_env_value ECOREX_TONGXIN_DATABASE "")"

  install -d -m 0750 -o "$SERVICE_USER" -g "$SERVICE_GROUP" "$STATE_DIR"
  install -d -m 0750 -o "$SERVICE_USER" -g "$SERVICE_GROUP" \
    "$STATE_DIR/appdata" \
    "$STATE_DIR/capability-state" \
    "$STATE_DIR/capability-packages" \
    "$STATE_DIR/playwright-browsers"

  ECOREX_CONFIG_PATH="$STATE_DIR/config.json" \
  ECOREX_TEMPLATE_PATH="$runtime_dir/config-template.json" \
  ECOREX_WEB_HOST="$effective_host" \
  ECOREX_WEB_PORT="$effective_port" \
  ECOREX_WEB_PASSWORD="$password" \
  ECOREX_WORKSPACE_ROOT="$WORKSPACE_ROOT" \
  ECOREX_STATE_DIR="$STATE_DIR" \
  ECOREX_RELEASE_RUNTIME_DIR="$runtime_dir" \
  ECOREX_TONGXIN_AUTH_URL="$tongxin_auth_url" \
  ECOREX_TONGXIN_DATABASE="$tongxin_database" \
  python3 - <<'PY'
import json
import os
import pathlib

config_path = pathlib.Path(os.environ["ECOREX_CONFIG_PATH"])
template_path = pathlib.Path(os.environ["ECOREX_TEMPLATE_PATH"])

payload = {}
if config_path.is_file():
    payload = json.loads(config_path.read_text(encoding="utf-8-sig"))
elif template_path.is_file():
    payload = json.loads(template_path.read_text(encoding="utf-8-sig"))

payload.update({
    "channel_type": "web",
    "web_console": True,
    "web_host": os.environ["ECOREX_WEB_HOST"],
    "web_port": int(os.environ["ECOREX_WEB_PORT"]),
    "web_password": os.environ["ECOREX_WEB_PASSWORD"],
    "agent": True,
    "agent_workspace": os.environ["ECOREX_WORKSPACE_ROOT"],
    "web_file_serve_root": os.environ["ECOREX_WORKSPACE_ROOT"],
    "appdata_dir": os.path.join(os.environ["ECOREX_STATE_DIR"], "appdata"),
})

tools = payload.setdefault("tools", {})
if not isinstance(tools, dict):
    tools = {}
    payload["tools"] = tools

feishu_cli = tools.setdefault("feishu_cli", {})
if not isinstance(feishu_cli, dict):
    feishu_cli = {}
    tools["feishu_cli"] = feishu_cli
feishu_cli.setdefault("package", "@larksuite/cli@1.0.56")
feishu_cli.setdefault("auto_install", False)
feishu_cli.setdefault("allow_system_node", False)
feishu_cli.setdefault("install_root", os.path.join(os.environ["ECOREX_STATE_DIR"], "tools", "lark-cli"))

tongxin_cli = tools.setdefault("tongxin_cli", {})
if not isinstance(tongxin_cli, dict):
    tongxin_cli = {}
    tools["tongxin_cli"] = tongxin_cli
tongxin_cli.setdefault("script_path", "")
built_in_tongxin = os.path.join(os.environ["ECOREX_RELEASE_RUNTIME_DIR"], "tools", "tongxin", "xin_agent_cli.py")
if not tongxin_cli.get("script_path") and os.path.isfile(built_in_tongxin):
    tongxin_cli["script_path"] = built_in_tongxin
tongxin_cli.setdefault("python_path", "")
tongxin_database = os.environ.get("ECOREX_TONGXIN_DATABASE", "").strip()
if tongxin_database:
    tongxin_cli["database_path"] = tongxin_database
else:
    tongxin_cli.setdefault("database_path", os.path.join(os.environ["ECOREX_STATE_DIR"], "tongxin.sqlite3"))
tongxin_cli["read_only"] = True
tongxin_auth_url = os.environ.get("ECOREX_TONGXIN_AUTH_URL", "").strip()
if tongxin_auth_url:
    tongxin_cli["auth_url"] = tongxin_auth_url
else:
    tongxin_cli.setdefault("auth_url", "")
tongxin_cli.setdefault("bootstrap_manifest_url", "")
tongxin_cli.setdefault("bootstrap_url", "")
tongxin_cli.setdefault("bootstrap_sha256", "")
tongxin_cli.setdefault("bootstrap_dir", os.path.join(os.environ["ECOREX_STATE_DIR"], "tools", "tongxin"))

browser = tools.setdefault("browser", {})
if not isinstance(browser, dict):
    browser = {}
    tools["browser"] = browser
browser.setdefault("cdp_endpoint", "http://127.0.0.1:9222")
browser["cdp_auto_launch"] = os.environ.get("ECOREX_BROWSER_CDP_AUTO_LAUNCH", "").strip().lower() not in {"0", "false", "no", "off"}
browser["cdp_fallback"] = os.environ.get("ECOREX_BROWSER_CDP_FALLBACK", "").strip().lower() not in {"0", "false", "no", "off"}
browser["persistent"] = os.environ.get("ECOREX_BROWSER_PERSISTENT", "").strip().lower() not in {"0", "false", "no", "off"}
browser.setdefault("cdp_user_data_dir", os.path.join(os.environ["ECOREX_STATE_DIR"], "chrome-cdp-profile"))
browser.setdefault("user_data_dir", os.path.join(os.environ["ECOREX_STATE_DIR"], "browser-profile"))

config_path.parent.mkdir(parents=True, exist_ok=True)
tmp_path = config_path.with_suffix(".json.tmp")
tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
tmp_path.replace(config_path)
PY

  chown "$SERVICE_USER:$SERVICE_GROUP" "$STATE_DIR/config.json"
  ln -sfn "$STATE_DIR/config.json" "$runtime_dir/config.json"
}

write_installation_manifest() {
  local release_dir="$1"
  local version="$2"
  local password
  local effective_host
  local effective_port

  password="$(read_env_value WEB_PASSWORD "")"
  effective_host="$(read_env_value WEB_HOST "$WEB_HOST")"
  effective_port="$(read_env_value WEB_PORT "$WEB_PORT")"

  ECOREX_WORKSPACE_ROOT="$WORKSPACE_ROOT" \
  ECOREX_INSTALL_ROOT="$INSTALL_ROOT" \
  ECOREX_RELEASE_DIR="$release_dir" \
  ECOREX_SERVICE_NAME="$SERVICE_NAME" \
  ECOREX_WEB_HOST="$effective_host" \
  ECOREX_WEB_PORT="$effective_port" \
  ECOREX_PUBLIC_BASE_URL="$PUBLIC_BASE_URL" \
  ECOREX_VERSION="$version" \
  python3 - <<'PY'
import json
import os
import pathlib
import socket
import time
import uuid

workspace = pathlib.Path(os.environ["ECOREX_WORKSPACE_ROOT"])
state_dir = workspace / ".ecorex"
path = state_dir / "installations.json"
state_dir.mkdir(parents=True, exist_ok=True)

if path.is_file():
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(payload, dict):
            payload = {}
    except Exception:
        payload = {}
else:
    payload = {}

now = int(time.time())
payload.setdefault("schemaVersion", 1)
payload.setdefault("workspaceId", str(uuid.uuid4()))
payload["workspacePath"] = str(workspace.resolve())
payload.setdefault("createdAt", now)
surfaces = payload.setdefault("surfaces", {})

host = os.environ["ECOREX_WEB_HOST"]
port = int(os.environ["ECOREX_WEB_PORT"])
base_url = os.environ.get("ECOREX_PUBLIC_BASE_URL") or f"http://{host if host != '0.0.0.0' else '127.0.0.1'}:{port}"

surfaces["webui-linux-service"] = {
    "surface": "webui-linux-service",
    "version": os.environ["ECOREX_VERSION"],
    "host": socket.gethostname(),
    "installRoot": os.environ["ECOREX_INSTALL_ROOT"],
    "current": os.path.join(os.environ["ECOREX_INSTALL_ROOT"], "current"),
    "releaseDir": os.environ["ECOREX_RELEASE_DIR"],
    "serviceName": os.environ["ECOREX_SERVICE_NAME"],
    "bindHost": host,
    "port": port,
    "url": base_url.rstrip("/") + "/app/",
    "lastSeenAt": now,
}
payload["updatedAt"] = now

tmp = path.with_suffix(path.suffix + f".{uuid.uuid4().hex}.tmp")
tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
tmp.replace(path)
PY

  chown -R "$SERVICE_USER:$SERVICE_GROUP" "$WORKSPACE_ROOT/.ecorex"
}

write_systemd_service() {
  cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=EcoreX WebUI Runtime
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$SERVICE_USER
Group=$SERVICE_GROUP
WorkingDirectory=$INSTALL_ROOT/current/runtime
EnvironmentFile=$ENV_FILE
Environment=PYTHONPATH=$INSTALL_ROOT/current/runtime
Environment=ECOREX_INSTALL_ROOT=$INSTALL_ROOT
Environment=ECOREX_STATE_DIR=$STATE_DIR
Environment=ECOREX_NODE_ROOT=$INSTALL_ROOT/node
Environment=ECOREX_PYTHON_PATH=$VENV_DIR/bin/python
Environment=ECOREX_CAPABILITY_STATE_DIR=$STATE_DIR/capability-state
Environment=ECOREX_CAPABILITY_TARGET_DIR=$STATE_DIR/capability-packages
Environment=ECOREX_PLAYWRIGHT_BROWSERS_DIR=$STATE_DIR/playwright-browsers
Environment=PLAYWRIGHT_BROWSERS_PATH=$STATE_DIR/playwright-browsers
ExecStart=$VENV_DIR/bin/python $INSTALL_ROOT/current/runtime/app.py
Restart=on-failure
RestartSec=3
TimeoutStopSec=30
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF
}

wait_for_webui() {
  local url="$1"
  if [[ "$START_SERVICE" != "1" ]]; then
    return
  fi

  local attempt
  for attempt in $(seq 1 30); do
    if python3 - "$url" <<'PY'
import sys
import urllib.error
import urllib.request

request = urllib.request.Request(sys.argv[1], method="GET")
try:
    with urllib.request.urlopen(request, timeout=2) as response:
        raise SystemExit(0 if response.status < 500 else 1)
except urllib.error.HTTPError as exc:
    raise SystemExit(0 if exc.code < 500 else 1)
except Exception:
    raise SystemExit(1)
PY
    then
      return
    fi
    sleep 1
  done

  log "WebUI did not respond before browser auto-open; continuing with $url"
}

open_browser() {
  local url="$1"
  if [[ "$OPEN_BROWSER" != "1" ]]; then
    log "Browser auto-open disabled. Open $url"
    return
  fi

  if [[ -z "${DISPLAY:-}${WAYLAND_DISPLAY:-}${BROWSER:-}" && "$(uname -s)" != "Darwin" ]]; then
    log "Browser auto-open skipped because no graphical session was detected. Open $url"
    return
  fi

  local opener=()
  if [[ -n "${BROWSER:-}" ]]; then
    opener=("$BROWSER" "$url")
  elif command -v xdg-open >/dev/null 2>&1; then
    opener=(xdg-open "$url")
  elif command -v gio >/dev/null 2>&1; then
    opener=(gio open "$url")
  elif command -v open >/dev/null 2>&1; then
    opener=(open "$url")
  elif command -v python3 >/dev/null 2>&1; then
    opener=(python3 -m webbrowser "$url")
  else
    log "Browser auto-open skipped because no opener was found. Open $url"
    return
  fi

  if [[ -n "${SUDO_USER:-}" && "$SUDO_USER" != "root" ]] && id "$SUDO_USER" >/dev/null 2>&1 && command -v runuser >/dev/null 2>&1; then
    runuser -u "$SUDO_USER" -- env \
      DISPLAY="${DISPLAY:-}" \
      WAYLAND_DISPLAY="${WAYLAND_DISPLAY:-}" \
      XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-}" \
      XAUTHORITY="${XAUTHORITY:-}" \
      "${opener[@]}" >/dev/null 2>&1 &
  else
    "${opener[@]}" >/dev/null 2>&1 &
  fi
  log "Opening EcoreX WebUI: $url"
}

tmp_dir="$(mktemp -d)"

ensure_user
install -d -m 0755 -o root -g root "$INSTALL_ROOT"
install -d -m 0755 -o root -g root "$INSTALL_ROOT/releases"
install -d -m 0755 -o "$SERVICE_USER" -g "$SERVICE_GROUP" "$WORKSPACE_ROOT"

tarball="$tmp_dir/ecorex-web-release.tar.gz"
download_release "$tarball"

actual_sha="$(calc_sha256 "$tarball")"
if [[ -n "$EXPECTED_SHA256" && "$actual_sha" != "${EXPECTED_SHA256^^}" ]]; then
  fail "Release tarball SHA256 mismatch: expected ${EXPECTED_SHA256^^}, got $actual_sha"
fi

extract_dir="$tmp_dir/extract"
safe_extract_tar "$tarball" "$extract_dir"
bundle_root="$(find_bundle_root "$extract_dir")"

bundle_version="$VERSION"
if [[ -f "$bundle_root/release.json" ]]; then
  bundle_version="$(python3 - "$bundle_root/release.json" "$VERSION" <<'PY'
import json
import pathlib
import sys

try:
    data = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8-sig"))
    print(data.get("version") or sys.argv[2])
except Exception:
    print(sys.argv[2])
PY
)"
fi

release_id="$(date -u +%Y%m%d%H%M%S)-v${bundle_version}"
release_dir="$INSTALL_ROOT/releases/$release_id"
if [[ -e "$release_dir" ]]; then
  fail "Release directory already exists: $release_dir"
fi

install -d -m 0755 -o root -g root "$release_dir"
cp -a "$bundle_root/." "$release_dir/"
runtime_dir="$release_dir/runtime"
[[ -f "$runtime_dir/app.py" ]] || fail "Installed release is missing runtime/app.py"

write_env_file
write_runtime_config "$runtime_dir"

chown -R root:root "$release_dir"
chown -h "$SERVICE_USER:$SERVICE_GROUP" "$runtime_dir/config.json" 2>/dev/null || true

if [[ ! -d "$VENV_DIR" ]]; then
  python3 -m venv "$VENV_DIR"
fi

if [[ "$INSTALL_PY_DEPS" == "1" ]]; then
  "$VENV_DIR/bin/python" -m pip install --upgrade pip setuptools wheel
  declare -a python_requirement_files=()
  if [[ -f "$runtime_dir/requirements.txt" ]]; then
    python_requirement_files+=("$runtime_dir/requirements.txt")
  fi
  if [[ -f "$runtime_dir/core-requirements.txt" ]]; then
    python_requirement_files+=("$runtime_dir/core-requirements.txt")
  fi
  if [[ "${#python_requirement_files[@]}" -eq 0 ]]; then
    fail "Installed release is missing runtime Python requirements"
  fi
  for requirements_file in "${python_requirement_files[@]}"; do
    "$VENV_DIR/bin/python" -m pip install -r "$requirements_file"
  done
fi
install_playwright_chromium_runtime
install_node_runtime "$bundle_root"
run_web_core_baseline_gate "$runtime_dir"
run_web_release_gate "$runtime_dir"
ln -sfn "$release_dir" "$INSTALL_ROOT/current"
chown -R "$SERVICE_USER:$SERVICE_GROUP" "$VENV_DIR" "$STATE_DIR" "$WORKSPACE_ROOT"

write_installation_manifest "$release_dir" "$bundle_version"
write_systemd_service

if command -v systemctl >/dev/null 2>&1; then
  systemctl daemon-reload
  systemctl enable "$SERVICE_NAME.service" >/dev/null
if [[ "$START_SERVICE" == "1" ]]; then
    systemctl restart "$SERVICE_NAME.service"
  fi
else
  log "systemctl not found; service file written to $SERVICE_FILE but not started."
fi

final_port="$(read_env_value WEB_PORT "$WEB_PORT")"
final_password="$(read_env_value WEB_PASSWORD "")"
final_host="$(read_env_value WEB_HOST "$WEB_HOST")"
local_host="$final_host"
if [[ "$local_host" == "0.0.0.0" || "$local_host" == "::" ]]; then
  local_host="127.0.0.1"
fi
local_url="http://${local_host}:${final_port}/app/"
if [[ -n "$PUBLIC_BASE_URL" ]]; then
  final_url="${PUBLIC_BASE_URL%/}/app/"
else
  final_url="$local_url"
fi

wait_for_webui "$local_url"
if [[ -n "$PUBLIC_BASE_URL" ]]; then
  if ! python3 - "$final_url" <<'PY'
import sys
import urllib.error
import urllib.request

try:
    with urllib.request.urlopen(sys.argv[1], timeout=5) as response:
        raise SystemExit(0 if response.status < 500 else 1)
except urllib.error.HTTPError as exc:
    raise SystemExit(0 if exc.code < 500 else 1)
except Exception:
    raise SystemExit(1)
PY
  then
    log "Public proxy smoke did not pass yet: $final_url"
    log "Local WebUI is healthy at: $local_url"
  fi
fi
open_browser "$final_url"

cat <<EOF
EcoreX WebUI service installed.
version: $bundle_version
tarballSha256: $actual_sha
installRoot: $INSTALL_ROOT
current: $INSTALL_ROOT/current
workspace: $WORKSPACE_ROOT
stateConfig: $STATE_DIR/config.json
envFile: $ENV_FILE
service: $SERVICE_NAME.service
localUrl: $local_url
publicUrl: $final_url
webPassword: [redacted; read WEB_PASSWORD from $ENV_FILE if you need the bootstrap credential]
EOF
