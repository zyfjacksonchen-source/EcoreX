#!/usr/bin/env bash
set -euo pipefail

VERSION="${VERSION:-0.2.0}"
PUBLIC_BASE_URL="${PUBLIC_BASE_URL:-https://mvdcm.ecoremedia.net/ecorex-agent}"
ZIP_PATH="${ZIP_PATH:-/tmp/ecorex-release/EcoreX_0.2.0-mvdcm-public-release.zip}"
EXPECTED_SHA256="${EXPECTED_SHA256:-6F4A3344D25CCCA3DDAA53ED37B21C830A09F11C3E36CF30A329856723931244}"
RELEASE_ROOT="${RELEASE_ROOT:-/srv/ecorex-agent-download}"
ADMIN_ROOT="${ADMIN_ROOT:-/srv/ecorex-agent-admin}"
MIGRATION_SOURCE_ROOT="${MIGRATION_SOURCE_ROOT:-/srv/ecorex-migration-source}"
SERVICE_NAME="${SERVICE_NAME:-ecorex-admin-api}"
RUN_USER="${RUN_USER:-ecorex}"
RUN_GROUP="${RUN_GROUP:-ecorex}"
ADMIN_HOST="${ADMIN_HOST:-127.0.0.1}"
ADMIN_PORT="${ADMIN_PORT:-18084}"

log() {
  printf '[ecorex-mvdcm] %s\n' "$*"
}

fail() {
  printf '[ecorex-mvdcm] ERROR: %s\n' "$*" >&2
  exit 1
}

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || fail "missing required command: $1"
}

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

require_root() {
  if [ "$(id -u)" != "0" ]; then
    fail "run as root so service/user/directory ownership can be prepared"
  fi
}

ensure_user() {
  if ! getent group "$RUN_GROUP" >/dev/null 2>&1; then
    log "creating group $RUN_GROUP"
    groupadd --system "$RUN_GROUP"
  fi
  if ! id "$RUN_USER" >/dev/null 2>&1; then
    log "creating user $RUN_USER"
    useradd --system --gid "$RUN_GROUP" --home-dir "$ADMIN_ROOT" --shell /usr/sbin/nologin "$RUN_USER"
  fi
}

extract_zip_safely() {
  local zip_path="$1"
  local target="$2"
  python3 - "$zip_path" "$target" <<'PY'
import pathlib
import sys
import zipfile

zip_path = pathlib.Path(sys.argv[1])
target = pathlib.Path(sys.argv[2]).resolve()
with zipfile.ZipFile(zip_path) as archive:
    for member in archive.infolist():
        normalized = member.filename.replace("\\", "/")
        destination = (target / normalized).resolve()
        if not str(destination).startswith(str(target)):
            raise SystemExit(f"Unsafe zip member path: {member.filename}")
        if member.is_dir():
            destination.mkdir(parents=True, exist_ok=True)
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        with archive.open(member) as source, destination.open("wb") as output:
            output.write(source.read())
PY
}

validate_release_tree() {
  local root="$1"
  for required in \
    "site/index.html" \
    "site/manifest.json" \
    "site/admin/index.html" \
    "admin-api/ecorex_admin_api.py" \
    "server/install-ecorex-public-release.sh" \
    "server/check-ecorex-server-release.sh" \
    "server/caddy/ecorex-agent.routes.caddy" \
    "server/nginx/ecorex-agent.conf.example" \
    "server/systemd/ecorex-admin-api.service.example"; do
    [ -e "$root/$required" ] || fail "release zip missing $required"
  done

  if grep -RIl --exclude-dir='downloads' -E 'www\.ecoreai\.cn|CowAgent|~/cow' "$root/site" "$root/server" "$root/admin-api" >/tmp/ecorex-mvdcm-legacy-hits.txt 2>/dev/null; then
    log "legacy text hits:"
    cat /tmp/ecorex-mvdcm-legacy-hits.txt >&2
    fail "release tree still contains legacy public text"
  fi
}

install_release() {
  local extract_root="$1"
  log "installing public release into $RELEASE_ROOT and $ADMIN_ROOT"
  VERSION="$VERSION" \
    RELEASE_ROOT="$RELEASE_ROOT" \
    ADMIN_ROOT="$ADMIN_ROOT" \
    EXPECTED_SHA256="$EXPECTED_SHA256" \
    RESTART_SERVICE=0 \
    bash "$extract_root/server/install-ecorex-public-release.sh" "$ZIP_PATH"
}

normalize_env() {
  local env_file="$ADMIN_ROOT/env/ecorex-admin-api.env"
  [ -f "$env_file" ] || fail "missing env file after install: $env_file"

  python3 - "$env_file" "$ADMIN_ROOT/data/ecorex-admin.sqlite3" "$ADMIN_HOST" "$ADMIN_PORT" <<'PY'
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
db = sys.argv[2]
host = sys.argv[3]
port = sys.argv[4]
updates = {
    "ECOREX_ADMIN_DB": db,
    "ECOREX_ADMIN_HOST": host,
    "ECOREX_ADMIN_PORT": port,
    "ECOREX_ALLOWED_ORIGINS": "https://mvdcm.ecoremedia.net",
}
lines = path.read_text(encoding="utf-8").splitlines()
out = []
seen = set()
for line in lines:
    if not line or line.lstrip().startswith("#") or "=" not in line:
        out.append(line)
        continue
    key = line.split("=", 1)[0]
    if key in updates:
        out.append(f"{key}={updates[key]}")
        seen.add(key)
    else:
        out.append(line)
for key, value in updates.items():
    if key not in seen:
        out.append(f"{key}={value}")
path.write_text("\n".join(out) + "\n", encoding="utf-8")
PY
  chmod 600 "$env_file"
}

install_systemd_unit() {
  if ! command -v systemctl >/dev/null 2>&1; then
    log "systemctl not found; skip systemd unit installation"
    return 0
  fi

  local unit_path="/etc/systemd/system/${SERVICE_NAME}.service"
  if [ -f "$unit_path" ]; then
    log "systemd unit already exists: $unit_path"
  else
    log "creating systemd unit: $unit_path"
    cat > "$unit_path" <<EOF
[Unit]
Description=EcoreX Admin API
After=network.target

[Service]
Type=simple
User=$RUN_USER
Group=$RUN_GROUP
WorkingDirectory=$ADMIN_ROOT/app
EnvironmentFile=$ADMIN_ROOT/env/ecorex-admin-api.env
ExecStart=/usr/bin/python3 $ADMIN_ROOT/app/ecorex_admin_api.py --host $ADMIN_HOST --port $ADMIN_PORT
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
  fi

  systemctl daemon-reload
  systemctl enable "$SERVICE_NAME.service"
  systemctl restart "$SERVICE_NAME.service"
  systemctl --no-pager --full status "$SERVICE_NAME.service" || true
}

write_proxy_snippets() {
  local out_dir="$ADMIN_ROOT/server/mvdcm-proxy-snippets"
  install -d "$out_dir"
  if [ -f "$ADMIN_ROOT/server/caddy/ecorex-agent.routes.caddy" ]; then
    cp -f "$ADMIN_ROOT/server/caddy/ecorex-agent.routes.caddy" "$out_dir/ecorex-agent.routes.caddy"
  fi
  if [ -f "$ADMIN_ROOT/server/nginx/ecorex-agent.conf.example" ]; then
    cp -f "$ADMIN_ROOT/server/nginx/ecorex-agent.conf.example" "$out_dir/ecorex-agent.conf.example"
  fi
  log "proxy snippets prepared under $out_dir"
}

post_install_checks() {
  log "running local service checks"
  [ -f "$RELEASE_ROOT/current/index.html" ] || fail "missing current index"
  [ -f "$RELEASE_ROOT/current/manifest.json" ] || fail "missing current manifest"
  [ -f "$RELEASE_ROOT/current/admin/index.html" ] || fail "missing current admin"
  [ -f "$ADMIN_ROOT/app/ecorex_admin_api.py" ] || fail "missing admin API app"
  [ -f "$ADMIN_ROOT/env/ecorex-admin-api.env" ] || fail "missing admin API env"

  if command -v curl >/dev/null 2>&1; then
    curl -fsS "http://$ADMIN_HOST:$ADMIN_PORT/client/model-config" >/tmp/ecorex-client-model-config.out 2>/tmp/ecorex-client-model-config.err || true
    log "client/model-config unauthenticated local probe completed; review HTTP status through reverse proxy after TLS is fixed"
  fi

  log "release current: $RELEASE_ROOT/current"
  log "admin root: $ADMIN_ROOT"
  log "public base: $PUBLIC_BASE_URL"
}

main() {
  require_root
  need_cmd python3
  [ -f "$ZIP_PATH" ] || fail "release zip not found: $ZIP_PATH"

  local actual_sha
  actual_sha="$(calc_sha256 "$ZIP_PATH")"
  if [ "$actual_sha" != "${EXPECTED_SHA256^^}" ]; then
    fail "SHA256 mismatch for $ZIP_PATH: expected ${EXPECTED_SHA256^^}, got $actual_sha"
  fi
  log "zip SHA256 verified: $actual_sha"

  ensure_user
  install -d /tmp/ecorex-release
  install -d "$RELEASE_ROOT/releases" "$ADMIN_ROOT/app" "$ADMIN_ROOT/data" "$ADMIN_ROOT/env" "$ADMIN_ROOT/server" "$ADMIN_ROOT/backups" "$MIGRATION_SOURCE_ROOT"

  local tmp_dir
  tmp_dir="$(mktemp -d)"
  trap 'if [ -n "${tmp_dir:-}" ]; then rm -rf "$tmp_dir"; fi' EXIT
  extract_zip_safely "$ZIP_PATH" "$tmp_dir"
  validate_release_tree "$tmp_dir"
  install_release "$tmp_dir"
  normalize_env

  chown -R "$RUN_USER:$RUN_GROUP" "$ADMIN_ROOT"
  chown -R "$RUN_USER:$RUN_GROUP" "$RELEASE_ROOT" || true
  write_proxy_snippets
  install_systemd_unit
  post_install_checks

  cat <<EOF

EcoreX mvdcm clone migration base deployment is installed.

Next required server steps:
1. Configure the site certificate/TLS for mvdcm.ecoremedia.net.
2. Add the reverse proxy/static routes from:
   $ADMIN_ROOT/server/mvdcm-proxy-snippets/
3. Import existing /srv/ecorex-* data with a consistency-safe SQLite backup.
4. Run external acceptance:
   curl -k -I https://mvdcm.ecoremedia.net/ecorex-agent/
   curl -k -I https://mvdcm.ecoremedia.net/ecorex-agent/manifest.json
   curl -k -I https://mvdcm.ecoremedia.net/ecorex-agent/admin/
   curl -k -I https://mvdcm.ecoremedia.net/ecorex-agent/client/model-config

EOF
}

main "$@"
