#!/usr/bin/env bash
set -euo pipefail

VERSION="${VERSION:-0.2.3}"
ZIP_PATH="${1:-${ZIP_PATH:-release-artifacts/EcoreX_${VERSION}-public-release.zip}}"
RELEASE_ROOT="${RELEASE_ROOT:-/srv/ecorex-agent-download}"
ADMIN_ROOT="${ADMIN_ROOT:-/srv/ecorex-agent-admin}"
SERVICE_NAME="${SERVICE_NAME:-ecorex-admin-api}"
COMPOSE_ROOT="${COMPOSE_ROOT:-/opt/xhs-report}"
COMPOSE_SERVICE="${COMPOSE_SERVICE:-ecorex-admin-api}"
COMPOSE_ADMIN_CONTEXT="${COMPOSE_ADMIN_CONTEXT:-$COMPOSE_ROOT/_ecorex_admin_api}"
EXPECTED_SHA256="${EXPECTED_SHA256:-}"
RESTART_SERVICE="${RESTART_SERVICE:-1}"
DOWNLOADS_SOURCE_DIR="${DOWNLOADS_SOURCE_DIR:-}"
PROMOTE_PUBLIC_RELEASE="${PROMOTE_PUBLIC_RELEASE:-0}"

need_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Missing required command: $1" >&2
    exit 1
  fi
}

need_cmd python3

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

if [[ ! -f "$ZIP_PATH" ]]; then
  echo "Release zip not found: $ZIP_PATH" >&2
  exit 1
fi

actual_sha="$(calc_sha256 "$ZIP_PATH")"
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

python3 - "$ZIP_PATH" "$tmp_dir" <<'PY'
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

for required in "site/index.html" "site/manifest.json" "site/admin/index.html" "admin-api/ecorex_admin_api.py" "checksums.json"; do
  if [[ ! -e "$tmp_dir/$required" ]]; then
    echo "Release zip is missing $required" >&2
    exit 1
  fi
done

python3 - "$tmp_dir" "$VERSION" "$DOWNLOADS_SOURCE_DIR" <<'PY'
import hashlib
import json
import pathlib
import shutil
import sys

root = pathlib.Path(sys.argv[1])
expected_version = sys.argv[2]
downloads_source_arg = sys.argv[3] if len(sys.argv) > 3 else ""
downloads_source = pathlib.Path(downloads_source_arg).resolve() if downloads_source_arg else None
checksums = json.loads((root / "checksums.json").read_text(encoding="utf-8-sig"))
if checksums.get("version") != expected_version:
    raise SystemExit(f"Unexpected release version: {checksums.get('version')}; expected {expected_version}")
artifacts = checksums.get("artifacts") or {}
if not artifacts:
    raise SystemExit("checksums.json does not contain any ready artifacts")

def sha256_path(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()

def require_download_source(artifact_id, rel, artifact):
    if downloads_source is None:
        raise SystemExit(f"Artifact {artifact_id} is externalized to {rel}; set DOWNLOADS_SOURCE_DIR")
    file_name = str(artifact.get("deploymentSourceFileName") or artifact.get("fileName") or pathlib.PurePosixPath(rel).name)
    if pathlib.PurePosixPath(file_name).name != file_name or ".." in pathlib.PurePosixPath(file_name).parts:
        raise SystemExit(f"Artifact {artifact_id} has unsafe deployment source file name")
    source = (downloads_source / file_name).resolve()
    try:
        source.relative_to(downloads_source)
    except Exception:
        raise SystemExit(f"Artifact {artifact_id} deployment source escaped DOWNLOADS_SOURCE_DIR")
    if not source.is_file():
        raise SystemExit(f"Externalized artifact {artifact_id} missing from DOWNLOADS_SOURCE_DIR: {file_name}")
    expected_size = int(artifact.get("size") or 0)
    if source.stat().st_size != expected_size:
        raise SystemExit(f"Externalized artifact {artifact_id} size does not match checksums.json")
    if sha256_path(source) != str(artifact.get("sha256") or "").upper():
        raise SystemExit(f"Externalized artifact {artifact_id} SHA256 does not match checksums.json")
    target = (root / rel).resolve()
    try:
        target.relative_to(root)
    except Exception:
        raise SystemExit(f"Artifact {artifact_id} target escaped release root")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)

for artifact_id, artifact in artifacts.items():
    rel = artifact.get("relativePath", "")
    if not rel:
        raise SystemExit(f"Artifact {artifact_id} has no relativePath")
    if artifact.get("external") or rel.lower().startswith(("http://", "https://")):
        if int(artifact.get("size") or 0) <= 0:
            raise SystemExit(f"External artifact {artifact_id} has no positive size")
        if len(str(artifact.get("sha256") or "")) != 64:
            raise SystemExit(f"External artifact {artifact_id} has no SHA256")
        if rel.startswith("site/downloads/"):
            require_download_source(artifact_id, rel, artifact)
        continue
    path = root / rel
    if not path.is_file():
        raise SystemExit(f"Artifact {artifact_id} missing from release zip: {path}")
    if path.stat().st_size != int(artifact.get("size") or 0):
        raise SystemExit(f"Artifact {artifact_id} size does not match checksums.json")
    if sha256_path(path) != str(artifact.get("sha256") or "").upper():
        raise SystemExit(f"Artifact {artifact_id} SHA256 does not match checksums.json")
PY

if [[ -n "$DOWNLOADS_SOURCE_DIR" && -d "$DOWNLOADS_SOURCE_DIR" ]]; then
  mkdir -p "$tmp_dir/site/downloads"
  cp -a "$DOWNLOADS_SOURCE_DIR/." "$tmp_dir/site/downloads/"
fi

install -d "$RELEASE_ROOT/releases"
install -d "$ADMIN_ROOT/app"
install -d "$ADMIN_ROOT/data"
install -d "$ADMIN_ROOT/env"
install -d "$ADMIN_ROOT/server"

cp -a "$tmp_dir/site" "$release_dir"
cp -a "$tmp_dir/admin-api/." "$ADMIN_ROOT/app/"
if [[ -d "$COMPOSE_ADMIN_CONTEXT" ]]; then
  cp -a "$tmp_dir/admin-api/." "$COMPOSE_ADMIN_CONTEXT/"
fi
if [[ -d "$tmp_dir/server" ]]; then
  cp -a "$tmp_dir/server/." "$ADMIN_ROOT/server/"
fi

env_file="$ADMIN_ROOT/env/ecorex-admin-api.env"
active_env_file="$ADMIN_ROOT/ecorex-admin-api.env"
required_client_keys="ecorex-web-v0.2.5-web.1,ecorex-web-v0.2.4-web.1,ecorex-web-v0.2.3-web.1,ecorex-web-v0.2.2-web.1,ecorex-web-v0.2.1-web.1,ecorex-desktop-v0.1.10,ecorex-desktop-v0.1.11,ecorex-desktop-v0.1.12,ecorex-desktop-v0.1.13,ecorex-desktop-v0.1.14,ecorex-desktop-v0.1.15,ecorex-desktop-v0.1.16,ecorex-desktop-v0.1.17,ecorex-desktop-v0.1.18,ecorex-desktop-v0.1.19,ecorex-desktop-v0.2.0,ecorex-web-v0.1.11-web.1,ecorex-web-v0.1.12-web.1,ecorex-web-v0.1.13-web.1,ecorex-web-v0.1.14-web.1,ecorex-web-v0.1.15-web.1,ecorex-web-v0.1.16-web.1,ecorex-web-v0.1.17-web.1,ecorex-web-v0.1.18-web.1,ecorex-web-v0.1.19-web.1,ecorex-web-v0.2.0-web.1"
if [[ ! -f "$env_file" ]]; then
  cat > "$env_file" <<'EOF'
ECOREX_ADMIN_DB=/srv/ecorex-agent-admin/data/ecorex-admin.sqlite3
ECOREX_CLIENT_EVENT_KEYS=ecorex-web-v0.2.5-web.1,ecorex-web-v0.2.4-web.1,ecorex-web-v0.2.3-web.1,ecorex-web-v0.2.2-web.1,ecorex-web-v0.2.1-web.1,ecorex-desktop-v0.1.10,ecorex-desktop-v0.1.11,ecorex-desktop-v0.1.12,ecorex-desktop-v0.1.13,ecorex-desktop-v0.1.14,ecorex-desktop-v0.1.15,ecorex-desktop-v0.1.16,ecorex-desktop-v0.1.17,ecorex-desktop-v0.1.18,ecorex-desktop-v0.1.19,ecorex-desktop-v0.2.0,ecorex-web-v0.1.11-web.1,ecorex-web-v0.1.12-web.1,ecorex-web-v0.1.13-web.1,ecorex-web-v0.1.14-web.1,ecorex-web-v0.1.15-web.1,ecorex-web-v0.1.16-web.1,ecorex-web-v0.1.17-web.1,ecorex-web-v0.1.18-web.1,ecorex-web-v0.1.19-web.1,ecorex-web-v0.2.0-web.1
ECOREX_ALLOWED_ORIGINS=https://www.ecoreai.cn
ECOREX_ADMIN_USERNAME=admin
ECOREX_ADMIN_PASSWORD=change-this-before-starting
ECOREX_TONGXIN_AUTH_UPSTREAM_URL=
ECOREX_TONGXIN_BOOTSTRAP_MANIFEST_URL=
ECOREX_TONGXIN_BOOTSTRAP_URL=
ECOREX_TONGXIN_BOOTSTRAP_SHA256=
ECOREX_TONGXIN_BOOTSTRAP_TOKEN=
# ECOREX_ADMIN_TOKEN=
# ECOREX_ADMIN_API_KEY=
EOF
  chmod 600 "$env_file"
  echo "Created $env_file. Replace ECOREX_ADMIN_PASSWORD before exposing Admin API." >&2
fi

merge_client_keys() {
  local target="$1"
  [[ -f "$target" ]] || return 0
  python3 - "$target" "$required_client_keys" <<'PY'
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
required = [item.strip() for item in sys.argv[2].split(",") if item.strip()]
lines = path.read_text(encoding="utf-8").splitlines()
out = []
seen = False
for line in lines:
    if line.startswith("ECOREX_CLIENT_EVENT_KEYS="):
        current = [item.strip() for item in line.split("=", 1)[1].split(",") if item.strip()]
        merged = []
        for item in current + required:
            if item not in merged:
                merged.append(item)
        out.append("ECOREX_CLIENT_EVENT_KEYS=" + ",".join(merged))
        seen = True
    else:
        out.append(line)
if not seen:
    out.append("ECOREX_CLIENT_EVENT_KEYS=" + ",".join(required))
path.write_text("\n".join(out) + "\n", encoding="utf-8")
PY
}

merge_client_keys "$env_file"
merge_client_keys "$active_env_file"

ensure_tongxin_env_defaults() {
  local target="$1"
  [[ -f "$target" ]] || return 0
  for key in \
    ECOREX_TONGXIN_AUTH_UPSTREAM_URL \
    ECOREX_TONGXIN_BOOTSTRAP_MANIFEST_URL \
    ECOREX_TONGXIN_BOOTSTRAP_URL \
    ECOREX_TONGXIN_BOOTSTRAP_SHA256 \
    ECOREX_TONGXIN_BOOTSTRAP_TOKEN; do
    grep -q "^${key}=" "$target" || printf '%s=\n' "$key" >> "$target"
  done
}

ensure_tongxin_env_defaults "$env_file"
ensure_tongxin_env_defaults "$active_env_file"

ln -sfn "$release_dir" "$RELEASE_ROOT/staged-v${VERSION}"
promotion_status="staged"
if [[ "$PROMOTE_PUBLIC_RELEASE" == "1" ]]; then
  ln -sfn "$release_dir" "$RELEASE_ROOT/current"
  promotion_status="promoted"
elif [[ ! -e "$RELEASE_ROOT/current" ]]; then
  echo "No current public release exists; $release_dir is staged but not promoted. Set PROMOTE_PUBLIC_RELEASE=1 to publish stable." >&2
fi

if [[ "$RESTART_SERVICE" == "1" && "$PROMOTE_PUBLIC_RELEASE" == "1" ]]; then
  compose_cmd=()
  if [[ -f "$COMPOSE_ROOT/docker-compose.yml" || -f "$COMPOSE_ROOT/compose.yml" ]]; then
    if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
      compose_cmd=(docker compose)
    elif command -v docker-compose >/dev/null 2>&1; then
      compose_cmd=(docker-compose)
    fi
  fi
  if [[ ${#compose_cmd[@]} -gt 0 && -d "$COMPOSE_ADMIN_CONTEXT" ]]; then
    (
      cd "$COMPOSE_ROOT"
      "${compose_cmd[@]}" up -d --build --force-recreate "$COMPOSE_SERVICE"
    )
  elif command -v systemctl >/dev/null 2>&1; then
    if systemctl list-unit-files "$SERVICE_NAME.service" | grep -q "^$SERVICE_NAME\\.service"; then
      systemctl restart "$SERVICE_NAME.service"
    else
      echo "systemd service $SERVICE_NAME.service not found and compose service not detected; skipped restart." >&2
    fi
  else
    echo "No supported service manager detected; skipped restart." >&2
  fi
fi

cat <<EOF
EcoreX public release installed.
version: $VERSION
zipSha256: $actual_sha
releaseDir: $release_dir
promotion: $promotion_status
staged: $RELEASE_ROOT/staged-v${VERSION}
current: $RELEASE_ROOT/current
adminApp: $ADMIN_ROOT/app
adminEnv: $env_file
adminComposeContext: $COMPOSE_ADMIN_CONTEXT
serverHelpers: $ADMIN_ROOT/server
EOF
