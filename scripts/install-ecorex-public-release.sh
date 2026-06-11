#!/usr/bin/env bash
set -euo pipefail

VERSION="${VERSION:-0.1.10}"
ZIP_PATH="${1:-${ZIP_PATH:-release-artifacts/EcoreX_${VERSION}-public-release.zip}}"
RELEASE_ROOT="${RELEASE_ROOT:-/srv/ecorex-agent-download}"
ADMIN_ROOT="${ADMIN_ROOT:-/srv/ecorex-agent-admin}"
SERVICE_NAME="${SERVICE_NAME:-ecorex-admin-api}"
EXPECTED_SHA256="${EXPECTED_SHA256:-}"
RESTART_SERVICE="${RESTART_SERVICE:-1}"

need_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Missing required command: $1" >&2
    exit 1
  fi
}

need_cmd unzip
need_cmd sha256sum
need_cmd python3

if [[ ! -f "$ZIP_PATH" ]]; then
  echo "Release zip not found: $ZIP_PATH" >&2
  exit 1
fi

actual_sha="$(sha256sum "$ZIP_PATH" | awk '{print toupper($1)}')"
if [[ -n "$EXPECTED_SHA256" && "$actual_sha" != "${EXPECTED_SHA256^^}" ]]; then
  echo "Release zip SHA256 mismatch: expected ${EXPECTED_SHA256^^}, got $actual_sha" >&2
  exit 1
fi

timestamp="$(date -u +%Y%m%d%H%M%S)"
release_dir="$RELEASE_ROOT/releases/${timestamp}-v${VERSION}"
tmp_dir="$(mktemp -d)"

cleanup() {
  rm -rf "$tmp_dir"
}
trap cleanup EXIT

unzip -q "$ZIP_PATH" -d "$tmp_dir"

for required in "site/index.html" "site/manifest.json" "site/admin/index.html" "admin-api/ecorex_admin_api.py" "checksums.json"; do
  if [[ ! -e "$tmp_dir/$required" ]]; then
    echo "Release zip is missing $required" >&2
    exit 1
  fi
done

python3 - "$tmp_dir" <<'PY'
import json
import pathlib
import sys

root = pathlib.Path(sys.argv[1])
checksums = json.loads((root / "checksums.json").read_text(encoding="utf-8-sig"))
if checksums.get("version") != "0.1.10":
    raise SystemExit(f"Unexpected release version: {checksums.get('version')}")
windows = checksums.get("windows") or {}
installer = root / windows.get("relativePath", "")
if not installer.is_file():
    raise SystemExit(f"Windows installer missing from release zip: {installer}")
if installer.stat().st_size != int(windows.get("size") or 0):
    raise SystemExit("Windows installer size does not match checksums.json")
PY

install -d "$RELEASE_ROOT/releases"
install -d "$ADMIN_ROOT/app"
install -d "$ADMIN_ROOT/data"
install -d "$ADMIN_ROOT/env"

cp -a "$tmp_dir/site" "$release_dir"
cp -a "$tmp_dir/admin-api/." "$ADMIN_ROOT/app/"

env_file="$ADMIN_ROOT/env/ecorex-admin-api.env"
if [[ ! -f "$env_file" ]]; then
  cat > "$env_file" <<'EOF'
ECOREX_ADMIN_DB=/srv/ecorex-agent-admin/data/ecorex-admin.sqlite3
ECOREX_CLIENT_EVENT_KEY=ecorex-desktop-v0.1.10
ECOREX_ALLOWED_ORIGINS=https://www.ecoreai.cn
ECOREX_ADMIN_USERNAME=admin
ECOREX_ADMIN_PASSWORD=change-this-before-starting
# ECOREX_ADMIN_TOKEN=
# ECOREX_ADMIN_API_KEY=
EOF
  chmod 600 "$env_file"
  echo "Created $env_file. Replace ECOREX_ADMIN_PASSWORD before exposing Admin API." >&2
fi

ln -sfn "$release_dir" "$RELEASE_ROOT/current"

if [[ "$RESTART_SERVICE" == "1" ]] && command -v systemctl >/dev/null 2>&1; then
  if systemctl list-unit-files "$SERVICE_NAME.service" >/dev/null 2>&1; then
    systemctl restart "$SERVICE_NAME.service"
  else
    echo "systemd service $SERVICE_NAME.service not found; skipped restart." >&2
  fi
fi

cat <<EOF
EcoreX public release installed.
version: $VERSION
zipSha256: $actual_sha
releaseDir: $release_dir
current: $RELEASE_ROOT/current
adminApp: $ADMIN_ROOT/app
adminEnv: $env_file
EOF
