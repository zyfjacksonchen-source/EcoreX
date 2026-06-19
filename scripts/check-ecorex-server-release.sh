#!/usr/bin/env bash
set -euo pipefail

VERSION="${VERSION:-0.1.15}"
RELEASE_ROOT="${RELEASE_ROOT:-/srv/ecorex-agent-download}"
ADMIN_ROOT="${ADMIN_ROOT:-/srv/ecorex-agent-admin}"
PUBLIC_BASE_URL="${PUBLIC_BASE_URL:-https://www.ecoreai.cn/ecorex-agent}"
CHECK_PUBLIC="${CHECK_PUBLIC:-1}"
CHECK_CADDY="${CHECK_CADDY:-1}"

failures=0

check_file() {
  local path="$1"
  if [[ -f "$path" ]]; then
    echo "PASS file $path"
  else
    echo "FAIL missing file $path"
    failures=$((failures + 1))
  fi
}

check_dir() {
  local path="$1"
  if [[ -d "$path" ]]; then
    echo "PASS dir $path"
  else
    echo "FAIL missing dir $path"
    failures=$((failures + 1))
  fi
}

check_http() {
  local name="$1"
  local url="$2"
  local expected="$3"
  local status
  status="$(python3 - "$url" <<'PY'
import sys
import urllib.error
import urllib.request

url = sys.argv[1]
request = urllib.request.Request(url, method="GET")
try:
    with urllib.request.urlopen(request, timeout=20) as response:
        print(response.status)
except urllib.error.HTTPError as exc:
    print(exc.code)
except Exception as exc:
    print(f"error:{exc}")
PY
)"
  if [[ "$status" == "$expected" ]]; then
    echo "PASS http $name $url -> $status"
  else
    echo "FAIL http $name $url -> $status expected $expected"
    failures=$((failures + 1))
  fi
}

need_python() {
  if ! command -v python3 >/dev/null 2>&1; then
    echo "FAIL missing python3"
    exit 1
  fi
}

need_python

current="$RELEASE_ROOT/current"
check_dir "$RELEASE_ROOT/releases"
check_file "$current/index.html"
check_file "$current/manifest.json"
check_file "$current/admin/index.html"
check_file "$ADMIN_ROOT/app/ecorex_admin_api.py"
check_file "$ADMIN_ROOT/env/ecorex-admin-api.env"
check_file "$ADMIN_ROOT/server/caddy/Caddyfile.example"

if ! python3 - "$current/manifest.json" "$current/downloads" "$VERSION" <<'PY'
import hashlib
import json
import pathlib
import sys

manifest = pathlib.Path(sys.argv[1])
downloads = pathlib.Path(sys.argv[2])
expected = sys.argv[3]
publishable = {"ready", "ready-unsigned"}
failures = 0

def fail(message):
    global failures
    print(f"FAIL {message}")
    failures += 1

def hash_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()

def is_external(artifact):
    href = str(artifact.get("href") or "").lower()
    return bool(artifact.get("external")) or href.startswith(("http://", "https://"))

if not manifest.is_file():
    raise SystemExit(0)
payload = json.loads(manifest.read_text(encoding="utf-8-sig"))
if payload.get("product") == "EcoreX" and payload.get("version") == expected:
    print(f"PASS manifest EcoreX {expected}")
else:
    fail(f"manifest product={payload.get('product')} version={payload.get('version')} expected={expected}")

ready_count = 0
for artifact in payload.get("artifacts") or []:
    artifact_id = artifact.get("id") or artifact.get("fileName") or "unknown"
    status = artifact.get("status") or ""
    file_name = artifact.get("fileName") or ""
    if status not in publishable:
        print(f"SKIP artifact {artifact_id} status={status}")
        continue
    ready_count += 1
    if is_external(artifact):
        href = artifact.get("href") or ""
        expected_size = int(artifact.get("size") or 0)
        expected_sha = str(artifact.get("sha256") or "").upper()
        if not str(href).lower().startswith(("http://", "https://")):
            fail(f"external artifact {artifact_id} href is not HTTP(S)")
        elif expected_size <= 0:
            fail(f"external artifact {artifact_id} has no positive size")
        elif len(expected_sha) != 64:
            fail(f"external artifact {artifact_id} has no SHA256")
        else:
            print(f"PASS external artifact {artifact_id} {href}")
        continue
    path = downloads / file_name
    if not file_name or not path.is_file():
        fail(f"missing artifact {artifact_id} file={file_name}")
        continue
    expected_size = int(artifact.get("size") or 0)
    actual_size = path.stat().st_size
    if actual_size != expected_size:
        fail(f"artifact {artifact_id} size={actual_size} expected={expected_size}")
        continue
    actual_sha = hash_file(path)
    expected_sha = str(artifact.get("sha256") or "").upper()
    if actual_sha != expected_sha:
        fail(f"artifact {artifact_id} sha256={actual_sha} expected={expected_sha}")
        continue
    print(f"PASS artifact {artifact_id} {file_name}")

if ready_count == 0:
    fail("manifest has no ready artifacts")

raise SystemExit(1 if failures else 0)
PY
then
  failures=$((failures + 1))
fi

if [[ "$CHECK_CADDY" == "1" ]] && command -v caddy >/dev/null 2>&1; then
  if caddy validate --config "$ADMIN_ROOT/server/caddy/Caddyfile.example" >/tmp/ecorex-caddy-validate.log 2>&1; then
    echo "PASS caddy template validates"
  else
    echo "WARN caddy template validation failed; inspect /tmp/ecorex-caddy-validate.log"
  fi
fi

if [[ "$CHECK_PUBLIC" == "1" ]]; then
  check_http "public-manifest" "$PUBLIC_BASE_URL/manifest.json" "200"
  check_http "public-root" "$PUBLIC_BASE_URL/" "200"
  for asset in "assets/icon.png" "assets/ecorex-app-preview.png" "assets/ecorex-ecosystem-hub.png"; do
    check_http "public-asset-$asset" "$PUBLIC_BASE_URL/$asset" "200"
  done
  check_http "public-admin-auth" "$PUBLIC_BASE_URL/admin/" "401"
  check_http "public-client-gate" "$PUBLIC_BASE_URL/client/model-config" "403"
  if ! python3 - "$current/manifest.json" "$PUBLIC_BASE_URL" <<'PY'
import json
import pathlib
import sys
import urllib.error
import urllib.request

manifest = pathlib.Path(sys.argv[1])
base_url = sys.argv[2].rstrip("/")
publishable = {"ready", "ready-unsigned"}
failures = 0

def status_for(url):
    request = urllib.request.Request(url, method="HEAD")
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        if exc.code != 405:
            return exc.code
    request = urllib.request.Request(url, method="GET")
    request.add_header("Range", "bytes=0-0")
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except Exception as exc:
        return f"error:{exc}"

payload = json.loads(manifest.read_text(encoding="utf-8-sig"))
for artifact in payload.get("artifacts") or []:
    status = artifact.get("status") or ""
    artifact_id = artifact.get("id") or artifact.get("fileName") or "unknown"
    if status not in publishable:
        print(f"SKIP public artifact {artifact_id} status={status}")
        continue
    href = artifact.get("href") or f"downloads/{artifact.get('fileName', '')}"
    if str(href).lower().startswith(("http://", "https://")):
        url = href
    else:
        url = f"{base_url}/{href.lstrip('/')}"
    status_code = status_for(url)
    if status_code in (200, 206):
        print(f"PASS http artifact {artifact_id} {url} -> {status_code}")
    else:
        print(f"FAIL http artifact {artifact_id} {url} -> {status_code}")
        failures += 1

raise SystemExit(1 if failures else 0)
PY
  then
    failures=$((failures + 1))
  fi
fi

if [[ "$failures" -gt 0 ]]; then
  echo "EcoreX server release check failed: $failures issue(s)."
  exit 1
fi

echo "EcoreX server release check passed."
