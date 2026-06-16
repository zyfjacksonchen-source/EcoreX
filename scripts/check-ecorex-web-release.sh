#!/usr/bin/env bash
set -euo pipefail

VERSION="${VERSION:-0.1.12}"
SERVICE_NAME="${SERVICE_NAME:-ecorex-web}"
INSTALL_ROOT="${INSTALL_ROOT:-/opt/ecorex-web}"
WORKSPACE_ROOT="${WORKSPACE_ROOT:-/srv/ecorex-agent-workspace}"
ENV_FILE="${ENV_FILE:-/etc/ecorex-web/ecorex-web.env}"
WEB_PORT="${WEB_PORT:-}"
WEB_PASSWORD="${WEB_PASSWORD:-}"
BASE_URL="${BASE_URL:-}"
HTTP_TIMEOUT="${HTTP_TIMEOUT:-20}"
CHECK_HTTP="${CHECK_HTTP:-1}"
CHECK_SYSTEMD="${CHECK_SYSTEMD:-1}"
CHECK_INSTALLED="${CHECK_INSTALLED:-1}"
PACKAGE_PATH="${1:-${PACKAGE_PATH:-}}"

failures=0

pass() {
  printf 'PASS %s\n' "$*"
}

fail() {
  printf 'FAIL %s\n' "$*"
  failures=$((failures + 1))
}

warn() {
  printf 'WARN %s\n' "$*"
}

need_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    fail "missing command: $1"
    return 1
  fi
}

read_env_key() {
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

if [[ -z "$WEB_PORT" ]]; then
  WEB_PORT="$(read_env_key WEB_PORT 9909)"
fi
if [[ -z "$WEB_PASSWORD" ]]; then
  WEB_PASSWORD="$(read_env_key WEB_PASSWORD "")"
fi
if [[ -z "$BASE_URL" ]]; then
  BASE_URL="http://127.0.0.1:${WEB_PORT}"
fi
BASE_URL="${BASE_URL%/}"

check_file() {
  local path="$1"
  if [[ -f "$path" ]]; then
    pass "file $path"
  else
    fail "missing file $path"
  fi
}

check_dir() {
  local path="$1"
  if [[ -d "$path" ]]; then
    pass "dir $path"
  else
    fail "missing dir $path"
  fi
}

check_package() {
  local package="$1"
  if [[ -z "$package" ]]; then
    return
  fi
  if [[ ! -f "$package" ]]; then
    fail "package not found: $package"
    return
  fi
  need_cmd tar || return

  local listing
  if ! listing="$(tar -tzf "$package")"; then
    fail "package is not a readable tar.gz: $package"
    return
  fi

  local required_suffixes=(
    "/runtime/app.py"
    "/runtime/requirements.txt"
    "/runtime/channel/web/chat.html"
    "/runtime/channel/web/static/app/index.html"
    "/scripts/install-ecorex-web.sh"
    "/scripts/check-ecorex-web-release.sh"
    "/service/caddy/ecorex-agent.routes.caddy"
    "/service/nginx/ecorex-agent.conf.example"
    "/service/systemd/ecorex-web.service.example"
    "/SHA256SUMS.txt"
    "/checksums.json"
    "/release.json"
  )

  for suffix in "${required_suffixes[@]}"; do
    if grep -Fq -- "$suffix" <<<"$listing"; then
      pass "package contains *$suffix"
    else
      fail "package missing *$suffix"
    fi
  done
}

login_cookie_jar=""
login_if_needed() {
  if [[ "$CHECK_HTTP" != "1" ]]; then
    return
  fi
  need_cmd curl || return
  login_cookie_jar="$(mktemp)"
  if [[ -z "$WEB_PASSWORD" ]]; then
    warn "WEB_PASSWORD not available; authenticated endpoint checks may fail if password auth is enabled."
    return
  fi

  local login_json
  login_json="$(WEB_PASSWORD="$WEB_PASSWORD" python3 - <<'PY'
import json
import os
print(json.dumps({"password": os.environ["WEB_PASSWORD"]}))
PY
)"

  local body_file
  body_file="$(mktemp)"
  local status
  status="$(curl -sS --max-time "$HTTP_TIMEOUT" -o "$body_file" -w "%{http_code}" \
    -c "$login_cookie_jar" \
    -H "Content-Type: application/json" \
    --data "$login_json" \
    "$BASE_URL/auth/login" || true)"

  if [[ "$status" == "200" ]] && grep -q '"status"[[:space:]]*:[[:space:]]*"success"' "$body_file"; then
    pass "http login $BASE_URL/auth/login"
  else
    fail "http login $BASE_URL/auth/login -> $status $(tr '\n' ' ' < "$body_file" | head -c 200)"
  fi
  rm -f "$body_file"
}

check_http_path() {
  local name="$1"
  local path="$2"
  local expected_status="$3"
  local expected_text="${4:-}"
  local body_file
  body_file="$(mktemp)"

  local curl_args=(-sS --max-time "$HTTP_TIMEOUT" -o "$body_file" -w "%{http_code}")
  if [[ -n "$login_cookie_jar" && -f "$login_cookie_jar" ]]; then
    curl_args+=(-b "$login_cookie_jar")
  fi

  local status
  status="$(curl "${curl_args[@]}" "$BASE_URL$path" || true)"
  if [[ "$status" != "$expected_status" ]]; then
    fail "http $name $BASE_URL$path -> $status expected $expected_status"
    rm -f "$body_file"
    return
  fi
  if [[ -n "$expected_text" ]] && ! grep -q "$expected_text" "$body_file"; then
    fail "http $name $BASE_URL$path missing expected text: $expected_text"
    rm -f "$body_file"
    return
  fi
  pass "http $name $BASE_URL$path -> $status"
  rm -f "$body_file"
}

check_sse_path() {
  local path="/stream?request_id=__ecorex_healthcheck__"
  local body_file
  body_file="$(mktemp)"

  local curl_args=(-sS -N --max-time 8 -o "$body_file" -w "%{http_code}")
  if [[ -n "$login_cookie_jar" && -f "$login_cookie_jar" ]]; then
    curl_args+=(-b "$login_cookie_jar")
  fi

  local status
  status="$(curl "${curl_args[@]}" "$BASE_URL$path" || true)"
  if [[ "$status" != "200" ]]; then
    fail "sse $BASE_URL$path -> $status expected 200"
    rm -f "$body_file"
    return
  fi
  if grep -q 'invalid request_id' "$body_file" && grep -q '^data:' "$body_file"; then
    pass "sse $BASE_URL$path returns event-stream data"
  else
    fail "sse $BASE_URL$path did not return expected invalid request_id event"
  fi
  rm -f "$body_file"
}

check_installation_manifest() {
  local manifest="$WORKSPACE_ROOT/.ecorex/installations.json"
  if [[ ! -f "$manifest" ]]; then
    fail "missing installation manifest $manifest"
    return
  fi
  if python3 - "$manifest" "$VERSION" "$WEB_PORT" <<'PY'
import json
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
expected_version = sys.argv[2]
expected_port = int(sys.argv[3])
payload = json.loads(path.read_text(encoding="utf-8-sig"))
surface = (payload.get("surfaces") or {}).get("webui-linux-service") or {}
if surface.get("version") != expected_version:
    raise SystemExit(f"version={surface.get('version')} expected={expected_version}")
if int(surface.get("port") or 0) != expected_port:
    raise SystemExit(f"port={surface.get('port')} expected={expected_port}")
print("manifest-ok")
PY
  then
    pass "installation manifest webui-linux-service"
  else
    fail "installation manifest webui-linux-service invalid"
  fi
}

check_package "$PACKAGE_PATH"

if [[ "$CHECK_INSTALLED" == "1" ]]; then
  check_dir "$INSTALL_ROOT"
  check_dir "$INSTALL_ROOT/current"
  check_file "$INSTALL_ROOT/current/runtime/app.py"
  check_file "$INSTALL_ROOT/current/runtime/requirements.txt"
  check_file "$INSTALL_ROOT/current/runtime/channel/web/chat.html"
  check_file "$INSTALL_ROOT/current/runtime/channel/web/static/app/index.html"
  check_file "$INSTALL_ROOT/state/config.json"
  check_file "$ENV_FILE"
  check_dir "$WORKSPACE_ROOT"
  check_installation_manifest

  if [[ "$CHECK_SYSTEMD" == "1" ]] && command -v systemctl >/dev/null 2>&1; then
    if systemctl is-enabled "$SERVICE_NAME.service" >/dev/null 2>&1; then
      pass "systemd enabled $SERVICE_NAME.service"
    else
      fail "systemd not enabled $SERVICE_NAME.service"
    fi
    if systemctl is-active "$SERVICE_NAME.service" >/dev/null 2>&1; then
      pass "systemd active $SERVICE_NAME.service"
    else
      fail "systemd not active $SERVICE_NAME.service"
    fi
  fi
fi

if [[ "$CHECK_HTTP" == "1" ]]; then
  if command -v curl >/dev/null 2>&1 && command -v python3 >/dev/null 2>&1; then
    login_if_needed
    check_http_path "app" "/app/" "200"
    check_http_path "auth-check" "/auth/check" "200" '"status"'
    check_http_path "version" "/api/version" "200" '"version"'
    check_sse_path
  else
    need_cmd curl || true
    need_cmd python3 || true
  fi
fi

if [[ -n "$login_cookie_jar" ]]; then
  rm -f "$login_cookie_jar"
fi

if [[ "$failures" -gt 0 ]]; then
  printf 'EcoreX WebUI release check failed: %s issue(s).\n' "$failures"
  exit 1
fi

printf 'EcoreX WebUI release check passed.\n'
