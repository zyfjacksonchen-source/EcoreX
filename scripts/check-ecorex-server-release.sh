#!/usr/bin/env bash
set -euo pipefail

VERSION="${VERSION:-0.1.17}"
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
failures = 0
REQUIRED_AUTH_NEGATIVE_STATUSES = {
    "messageNoToken",
    "messageWrongToken",
    "messageQueryTokenRejected",
    "streamNoToken",
    "streamWrongToken",
    "streamQueryTokenRejected",
    "fileStatNoToken",
    "fileStatWrongToken",
    "fileServeNoToken",
    "fileServeWrongToken",
    "openPathNoToken",
    "openPathWrongToken",
}

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

def is_publishable(artifact):
    status = artifact.get("status") or ""
    artifact_id = str(artifact.get("id") or artifact.get("fileName") or "")
    if status == "ready" and artifact_id.startswith("macos-") and artifact.get("signature") == "unsigned":
        fail(f"{artifact_id} unsigned macOS artifact must use status=ready-unsigned with installSmoke evidence")
        return False
    if status == "ready":
        return True
    return (
        status == "ready-unsigned"
        and artifact_id.startswith("macos-")
        and artifact.get("signature") == "unsigned"
    )

def validate_macos_unsigned_install_smoke(artifact):
    smoke = artifact.get("installSmoke") or artifact.get("install_smoke") or {}
    artifact_id = artifact.get("id") or artifact.get("fileName") or "unknown"
    if not isinstance(smoke, dict):
        fail(f"{artifact_id} ready-unsigned requires installSmoke evidence")
        return
    if str(smoke.get("status") or "").lower() != "pass":
        fail(f"{artifact_id} installSmoke.status must be pass")
    if str(smoke.get("version") or "") != str(payload.get("version") or ""):
        fail(f"{artifact_id} installSmoke.version must match manifest")
    if str(smoke.get("sha256") or "").upper() != str(artifact.get("sha256") or "").upper():
        fail(f"{artifact_id} installSmoke.sha256 must match artifact")
    evidence = smoke.get("runId") or smoke.get("run_id") or smoke.get("evidenceUrl") or smoke.get("evidence_url") or smoke.get("evidence")
    if not str(evidence or "").strip():
        fail(f"{artifact_id} installSmoke requires runId or evidenceUrl")
    arch = {"macos-arm64-dmg": "arm64", "macos-x64-dmg": "x64"}.get(str(artifact_id))
    if arch:
        expected_file = f"EcoreX_{payload.get('version')}_{arch}.dmg"
        if str(artifact.get("fileName") or "") != expected_file:
            fail(f"{artifact_id} fileName must be {expected_file}")
        if str(smoke.get("artifact") or "") != expected_file:
            fail(f"{artifact_id} installSmoke.artifact must be {expected_file}")
        if str(smoke.get("arch") or "") != arch:
            fail(f"{artifact_id} installSmoke.arch must be {arch}")
        if int(smoke.get("bytes") or 0) != int(artifact.get("size") or 0):
            fail(f"{artifact_id} installSmoke.bytes must match artifact size")
    missing = [
        key for key in (
            "mounted",
            "appFound",
            "copied",
            "launched",
            "versionOk",
            "sidecarReady",
            "authReady",
            "authRequired",
            "authNegativeReady",
            "gatekeeperInstructionShown",
        )
        if not smoke.get(key)
    ]
    if missing:
        fail(f"{artifact_id} installSmoke missing passed flags: {', '.join(missing)}")
    negative = smoke.get("authNegativeStatuses")
    if not isinstance(negative, dict):
        fail(f"{artifact_id} installSmoke requires authNegativeStatuses")
    else:
        missing_negative = sorted(REQUIRED_AUTH_NEGATIVE_STATUSES - set(negative))
        bad_negative = sorted(
            f"{key}={negative.get(key)}"
            for key in REQUIRED_AUTH_NEGATIVE_STATUSES & set(negative)
            if int(negative.get(key) or 0) != 401
        )
        if missing_negative or bad_negative:
            fail(f"{artifact_id} installSmoke authNegativeStatuses invalid missing={missing_negative} bad={bad_negative}")
    instructions = smoke.get("gatekeeperInstructions") or smoke.get("instructions") or smoke.get("instructionsUrl") or smoke.get("instructions_url")
    if not str(instructions or "").strip():
        fail(f"{artifact_id} installSmoke requires Gatekeeper instructions evidence")

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
    if not is_publishable(artifact):
        print(f"SKIP artifact {artifact_id} status={status}")
        continue
    if artifact_id == "windows-x64" and str(artifact.get("signature") or "").lower() != "valid":
        fail(f"{artifact_id} requires signature=Valid")
    if status == "ready-unsigned":
        validate_macos_unsigned_install_smoke(artifact)
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
  public_manifest_tmp="$(mktemp)"
  if python3 - "$PUBLIC_BASE_URL/manifest.json" "$public_manifest_tmp" <<'PY'
import pathlib
import sys
import urllib.request

url = sys.argv[1]
target = pathlib.Path(sys.argv[2])
with urllib.request.urlopen(url, timeout=20) as response:
    target.write_bytes(response.read())
PY
  then
    echo "PASS downloaded public manifest $PUBLIC_BASE_URL/manifest.json"
  else
    echo "FAIL download public manifest $PUBLIC_BASE_URL/manifest.json"
    failures=$((failures + 1))
  fi
  if ! python3 - "$public_manifest_tmp" "$PUBLIC_BASE_URL" "$VERSION" <<'PY'
import json
import pathlib
import sys
import urllib.error
import urllib.request

manifest = pathlib.Path(sys.argv[1])
base_url = sys.argv[2].rstrip("/")
expected_version = sys.argv[3]
failures = 0
REQUIRED_AUTH_NEGATIVE_STATUSES = {
    "messageNoToken",
    "messageWrongToken",
    "messageQueryTokenRejected",
    "streamNoToken",
    "streamWrongToken",
    "streamQueryTokenRejected",
    "fileStatNoToken",
    "fileStatWrongToken",
    "fileServeNoToken",
    "fileServeWrongToken",
    "openPathNoToken",
    "openPathWrongToken",
}

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

def is_publishable(artifact):
    global failures
    status = artifact.get("status") or ""
    artifact_id = str(artifact.get("id") or artifact.get("fileName") or "")
    if status == "ready" and artifact_id.startswith("macos-") and artifact.get("signature") == "unsigned":
        print(f"FAIL {artifact_id} unsigned macOS artifact must use status=ready-unsigned with installSmoke evidence")
        failures += 1
        return False
    if status == "ready":
        return True
    return (
        status == "ready-unsigned"
        and artifact_id.startswith("macos-")
        and artifact.get("signature") == "unsigned"
    )

def validate_macos_unsigned_install_smoke(artifact):
    smoke = artifact.get("installSmoke") or artifact.get("install_smoke") or {}
    artifact_id = artifact.get("id") or artifact.get("fileName") or "unknown"
    if not isinstance(smoke, dict):
        print(f"FAIL {artifact_id} ready-unsigned requires installSmoke evidence")
        return 1
    failed = 0
    if str(smoke.get("status") or "").lower() != "pass":
        print(f"FAIL {artifact_id} installSmoke.status must be pass")
        failed += 1
    if str(smoke.get("version") or "") != str(payload.get("version") or ""):
        print(f"FAIL {artifact_id} installSmoke.version must match manifest")
        failed += 1
    if str(smoke.get("sha256") or "").upper() != str(artifact.get("sha256") or "").upper():
        print(f"FAIL {artifact_id} installSmoke.sha256 must match artifact")
        failed += 1
    evidence = smoke.get("runId") or smoke.get("run_id") or smoke.get("evidenceUrl") or smoke.get("evidence_url") or smoke.get("evidence")
    if not str(evidence or "").strip():
        print(f"FAIL {artifact_id} installSmoke requires runId or evidenceUrl")
        failed += 1
    arch = {"macos-arm64-dmg": "arm64", "macos-x64-dmg": "x64"}.get(str(artifact_id))
    if arch:
        expected_file = f"EcoreX_{payload.get('version')}_{arch}.dmg"
        if str(artifact.get("fileName") or "") != expected_file:
            print(f"FAIL {artifact_id} fileName must be {expected_file}")
            failed += 1
        if str(smoke.get("artifact") or "") != expected_file:
            print(f"FAIL {artifact_id} installSmoke.artifact must be {expected_file}")
            failed += 1
        if str(smoke.get("arch") or "") != arch:
            print(f"FAIL {artifact_id} installSmoke.arch must be {arch}")
            failed += 1
        if int(smoke.get("bytes") or 0) != int(artifact.get("size") or 0):
            print(f"FAIL {artifact_id} installSmoke.bytes must match artifact size")
            failed += 1
    missing = [
        key for key in (
            "mounted",
            "appFound",
            "copied",
            "launched",
            "versionOk",
            "sidecarReady",
            "authReady",
            "authRequired",
            "authNegativeReady",
            "gatekeeperInstructionShown",
        )
        if not smoke.get(key)
    ]
    if missing:
        print(f"FAIL {artifact_id} installSmoke missing passed flags: {', '.join(missing)}")
        failed += 1
    negative = smoke.get("authNegativeStatuses")
    if not isinstance(negative, dict):
        print(f"FAIL {artifact_id} installSmoke requires authNegativeStatuses")
        failed += 1
    else:
        missing_negative = sorted(REQUIRED_AUTH_NEGATIVE_STATUSES - set(negative))
        bad_negative = sorted(
            f"{key}={negative.get(key)}"
            for key in REQUIRED_AUTH_NEGATIVE_STATUSES & set(negative)
            if int(negative.get(key) or 0) != 401
        )
        if missing_negative or bad_negative:
            print(f"FAIL {artifact_id} installSmoke authNegativeStatuses invalid missing={missing_negative} bad={bad_negative}")
            failed += 1
    instructions = smoke.get("gatekeeperInstructions") or smoke.get("instructions") or smoke.get("instructionsUrl") or smoke.get("instructions_url")
    if not str(instructions or "").strip():
        print(f"FAIL {artifact_id} installSmoke requires Gatekeeper instructions evidence")
        failed += 1
    return failed

payload = json.loads(manifest.read_text(encoding="utf-8-sig"))
if payload.get("product") == "EcoreX" and str(payload.get("version") or "") == expected_version:
    print(f"PASS public manifest EcoreX {expected_version}")
else:
    print(f"FAIL public manifest product={payload.get('product')} version={payload.get('version')} expected={expected_version}")
    failures += 1
for artifact in payload.get("artifacts") or []:
    status = artifact.get("status") or ""
    artifact_id = artifact.get("id") or artifact.get("fileName") or "unknown"
    if not is_publishable(artifact):
        print(f"SKIP public artifact {artifact_id} status={status}")
        continue
    if artifact_id == "windows-x64" and str(artifact.get("signature") or "").lower() != "valid":
        print(f"FAIL {artifact_id} requires signature=Valid")
        failures += 1
    if status == "ready-unsigned":
        failures += validate_macos_unsigned_install_smoke(artifact)
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
  rm -f "$public_manifest_tmp"
fi

if [[ "$failures" -gt 0 ]]; then
  echo "EcoreX server release check failed: $failures issue(s)."
  exit 1
fi

echo "EcoreX server release check passed."
