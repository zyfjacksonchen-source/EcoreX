#!/usr/bin/env bash
set -euo pipefail

VERSION="${VERSION:-0.1.15}"
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
RELEASE_BASE_URL="${RELEASE_BASE_URL:-https://www.ecoreai.cn/ecorex-agent/downloads}"
RELEASE_URL="${RELEASE_URL:-$RELEASE_BASE_URL/EcoreX_${VERSION}-web-linux-service.tar.gz}"
EXPECTED_SHA256="${EXPECTED_SHA256:-}"
START_SERVICE="${START_SERVICE:-1}"
OPEN_BROWSER="${OPEN_BROWSER:-1}"
INSTALL_PY_DEPS="${INSTALL_PY_DEPS:-1}"
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

  if command -v curl >/dev/null 2>&1; then
    curl -fL --retry 3 --connect-timeout 20 -o "$target" "$RELEASE_URL"
  elif command -v wget >/dev/null 2>&1; then
    wget -O "$target" "$RELEASE_URL"
  else
    fail "Missing curl or wget for online install. Set TARBALL_PATH to install from a local tarball."
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

write_env_file() {
  install -d -m 0750 -o root -g "$SERVICE_GROUP" "$ENV_DIR"

  local existing_password
  existing_password="$(read_env_value WEB_PASSWORD "")"
  if [[ -z "$existing_password" ]]; then
    existing_password="$(random_secret)"
  fi

  if [[ ! -f "$ENV_FILE" ]]; then
    cat > "$ENV_FILE" <<EOF
CHANNEL_TYPE=web
WEB_HOST=$WEB_HOST
WEB_PORT=$WEB_PORT
WEB_PASSWORD=$existing_password
AGENT_WORKSPACE=$WORKSPACE_ROOT
ECOREX_WEB_PUBLIC_BASE_URL=$PUBLIC_BASE_URL
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
  fi
}

write_runtime_config() {
  local runtime_dir="$1"
  local password
  local effective_host
  local effective_port

  password="$(read_env_value WEB_PASSWORD "")"
  effective_host="$(read_env_value WEB_HOST "$WEB_HOST")"
  effective_port="$(read_env_value WEB_PORT "$WEB_PORT")"

  install -d -m 0750 -o "$SERVICE_USER" -g "$SERVICE_GROUP" "$STATE_DIR"

  ECOREX_CONFIG_PATH="$STATE_DIR/config.json" \
  ECOREX_TEMPLATE_PATH="$runtime_dir/config-template.json" \
  ECOREX_WEB_HOST="$effective_host" \
  ECOREX_WEB_PORT="$effective_port" \
  ECOREX_WEB_PASSWORD="$password" \
  ECOREX_WORKSPACE_ROOT="$WORKSPACE_ROOT" \
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
    "appdata_dir": os.path.join(os.environ["ECOREX_WORKSPACE_ROOT"], "appdata"),
})

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

ln -sfn "$release_dir" "$INSTALL_ROOT/current"

if [[ ! -d "$VENV_DIR" ]]; then
  python3 -m venv "$VENV_DIR"
fi

if [[ "$INSTALL_PY_DEPS" == "1" ]]; then
  "$VENV_DIR/bin/python" -m pip install --upgrade pip setuptools wheel
  "$VENV_DIR/bin/python" -m pip install -r "$runtime_dir/requirements.txt"
fi
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
if [[ -n "$PUBLIC_BASE_URL" ]]; then
  final_url="${PUBLIC_BASE_URL%/}/app/"
else
  final_host="$(read_env_value WEB_HOST "$WEB_HOST")"
  if [[ "$final_host" == "0.0.0.0" || "$final_host" == "::" ]]; then
    final_host="127.0.0.1"
  fi
  final_url="http://${final_host}:${final_port}/app/"
fi

wait_for_webui "$final_url"
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
localUrl: $final_url
webPassword: $final_password
EOF
