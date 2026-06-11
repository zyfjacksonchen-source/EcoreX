#!/usr/bin/env bash
set -euo pipefail

VERSION="${VERSION:-0.1.10}"
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
check_file "$current/downloads/EcoreX_${VERSION}_x64-setup.exe"
check_file "$ADMIN_ROOT/app/ecorex_admin_api.py"
check_file "$ADMIN_ROOT/env/ecorex-admin-api.env"
check_file "$ADMIN_ROOT/server/caddy/Caddyfile.example"

python3 - "$current/manifest.json" "$VERSION" <<'PY'
import json
import pathlib
import sys

manifest = pathlib.Path(sys.argv[1])
expected = sys.argv[2]
if not manifest.is_file():
    raise SystemExit(0)
payload = json.loads(manifest.read_text(encoding="utf-8-sig"))
if payload.get("product") == "EcoreX" and payload.get("version") == expected:
    print(f"PASS manifest EcoreX {expected}")
else:
    print(f"FAIL manifest product={payload.get('product')} version={payload.get('version')} expected={expected}")
    raise SystemExit(1)
PY
if [[ $? -ne 0 ]]; then
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
  check_http "public-admin-auth" "$PUBLIC_BASE_URL/admin/" "401"
  check_http "public-client-gate" "$PUBLIC_BASE_URL/client/model-config" "403"
fi

if [[ "$failures" -gt 0 ]]; then
  echo "EcoreX server release check failed: $failures issue(s)."
  exit 1
fi

echo "EcoreX server release check passed."
