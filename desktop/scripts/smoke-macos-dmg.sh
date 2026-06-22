#!/usr/bin/env bash
set -euo pipefail

DMG_PATH="${1:?Usage: smoke-macos-dmg.sh /path/to/EcoreX_VERSION_ARCH.dmg /path/to/evidence.json}"
OUTPUT_PATH="${2:?Usage: smoke-macos-dmg.sh /path/to/EcoreX_VERSION_ARCH.dmg /path/to/evidence.json}"
VERSION="${VERSION:-0.1.19}"
ARCH="${MAC_ARCH:-}"
PORT="${ECOREX_WEB_PORT:-19133}"
STRICT_SIGNING="${ECOREX_MAC_STRICT_SIGNING:-0}"
RUN_URL="${GITHUB_SERVER_URL:-https://github.com}/${GITHUB_REPOSITORY:-}/actions/runs/${GITHUB_RUN_ID:-}"

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "smoke-macos-dmg.sh must run on macOS." >&2
  exit 1
fi

if [[ ! -f "$DMG_PATH" ]]; then
  echo "DMG not found: $DMG_PATH" >&2
  exit 1
fi

if [[ -z "$ARCH" ]]; then
  case "$(basename "$DMG_PATH")" in
    *_arm64.dmg) ARCH="arm64" ;;
    *_x64.dmg) ARCH="x64" ;;
    *) echo "Cannot infer MAC_ARCH from DMG name: $DMG_PATH" >&2; exit 1 ;;
  esac
fi

EXPECTED_NAME="EcoreX_${VERSION}_${ARCH}.dmg"
if [[ "$(basename "$DMG_PATH")" != "$EXPECTED_NAME" ]]; then
  echo "Unexpected DMG name: $(basename "$DMG_PATH"), expected $EXPECTED_NAME" >&2
  exit 1
fi

WORK_ROOT="${RUNNER_TEMP:-/tmp}/ecorex-macos-dmg-smoke-${VERSION}-${ARCH}-$$"
MOUNT_DIR="$WORK_ROOT/mount"
INSTALL_DIR="$WORK_ROOT/install"
APP_DST="$INSTALL_DIR/EcoreX.app"
LOG_PATH="$WORK_ROOT/ecorex.log"
APP_PID=""

cleanup() {
  if [[ -n "${APP_PID:-}" ]]; then
    kill "$APP_PID" >/dev/null 2>&1 || true
    wait "$APP_PID" >/dev/null 2>&1 || true
  fi
  hdiutil detach "$MOUNT_DIR" >/dev/null 2>&1 || true
  rm -rf "$WORK_ROOT"
}
trap cleanup EXIT

mkdir -p "$MOUNT_DIR" "$INSTALL_DIR" "$(dirname "$OUTPUT_PATH")"

SHA256="$(shasum -a 256 "$DMG_PATH" | awk '{print toupper($1)}')"
BYTES="$(stat -f '%z' "$DMG_PATH")"

hdiutil attach -nobrowse -readonly -mountpoint "$MOUNT_DIR" "$DMG_PATH" >/dev/null
APP_SRC="$(find "$MOUNT_DIR" -maxdepth 2 -name 'EcoreX.app' -type d | head -n 1)"
if [[ -z "$APP_SRC" ]]; then
  echo "EcoreX.app not found in mounted DMG." >&2
  exit 1
fi

ditto "$APP_SRC" "$APP_DST"
test -x "$APP_DST/Contents/MacOS/EcoreX"
test -f "$APP_DST/Contents/Resources/ecorex-runtime/app.py"
test -f "$APP_DST/Contents/Resources/ecorex-runtime/capabilities.json"

README_PATH="$APP_DST/Contents/Resources/README-macos-agent-install.txt"
GATEKEEPER_INSTRUCTIONS_SHOWN=false
if [[ -f "$README_PATH" ]] && grep -qi 'Gatekeeper' "$README_PATH"; then
  GATEKEEPER_INSTRUCTIONS_SHOWN=true
fi

SIGNATURE="unsigned"
if codesign --verify --deep --strict --verbose=2 "$APP_DST" >/dev/null 2>&1; then
  SIGNATURE="Valid"
elif [[ "$STRICT_SIGNING" == "1" ]]; then
  echo "codesign rejected copied app and strict signing is enabled." >&2
  exit 2
fi

GATEKEEPER="unsigned-user-approved"
if spctl -a -vv --type execute "$APP_DST" >/dev/null 2>&1; then
  GATEKEEPER="accepted"
elif [[ "$STRICT_SIGNING" == "1" ]]; then
  echo "spctl rejected copied app and strict signing is enabled." >&2
  exit 2
fi

ECOREX_WEB_PORT="$PORT" WEB_PORT="$PORT" ECOREX_DESKTOP_SMOKE="1" "$APP_DST/Contents/MacOS/EcoreX" >"$LOG_PATH" 2>&1 &
APP_PID="$!"

wait_json() {
  local url="$1"
  local timeout="${2:-75}"
  local deadline=$((SECONDS + timeout))
  local output=""
  while (( SECONDS < deadline )); do
    if output="$(curl --silent --show-error --max-time 5 "$url" 2>/dev/null)"; then
      if [[ -n "$output" ]]; then
        printf '%s' "$output"
        return 0
      fi
    fi
    sleep 1
  done
  return 1
}

VERSION_JSON="$(wait_json "http://127.0.0.1:${PORT}/api/version" 90)"
AUTH_JSON="$(wait_json "http://127.0.0.1:${PORT}/auth/check" 20)"

http_status() {
  local method="$1"
  local url="$2"
  local body="${3:-}"
  local token="${4:-}"
  local args=(--silent --output /dev/null --write-out '%{http_code}' --max-time 8 --request "$method")
  if [[ -n "$body" ]]; then
    args+=(--header 'Content-Type: application/json' --data "$body")
  fi
  if [[ -n "$token" ]]; then
    args+=(--header "X-EcoreX-Runtime-Token: $token")
  fi
  curl "${args[@]}" "$url" || true
}

NEG_MESSAGE_NO_TOKEN="$(http_status POST "http://127.0.0.1:${PORT}/message" '{"message":"macos negative auth smoke","stream":true}')"
NEG_MESSAGE_WRONG_TOKEN="$(http_status POST "http://127.0.0.1:${PORT}/message" '{"message":"macos negative auth smoke","stream":true}' wrong-token)"
NEG_MESSAGE_QUERY_TOKEN_REJECTED="$(http_status POST "http://127.0.0.1:${PORT}/message?runtime_token=wrong-token" '{"message":"macos negative auth smoke","stream":true}')"
NEG_STREAM_NO_TOKEN="$(http_status GET "http://127.0.0.1:${PORT}/stream?request_id=negative-auth-smoke")"
NEG_STREAM_WRONG_TOKEN="$(http_status GET "http://127.0.0.1:${PORT}/stream?request_id=negative-auth-smoke" "" wrong-token)"
NEG_STREAM_QUERY_TOKEN_REJECTED="$(http_status GET "http://127.0.0.1:${PORT}/stream?request_id=negative-auth-smoke&runtime_token=wrong-token")"
NEG_FILE_STAT_NO_TOKEN="$(http_status POST "http://127.0.0.1:${PORT}/api/file-stat" '{"path":"/etc/hosts"}')"
NEG_FILE_STAT_WRONG_TOKEN="$(http_status POST "http://127.0.0.1:${PORT}/api/file-stat" '{"path":"/etc/hosts"}' wrong-token)"
NEG_FILE_SERVE_NO_TOKEN="$(http_status GET "http://127.0.0.1:${PORT}/api/file?path=%2Fetc%2Fhosts")"
NEG_FILE_SERVE_WRONG_TOKEN="$(http_status GET "http://127.0.0.1:${PORT}/api/file?path=%2Fetc%2Fhosts" "" wrong-token)"
NEG_OPEN_PATH_NO_TOKEN="$(http_status POST "http://127.0.0.1:${PORT}/api/open-path" '{"path":"/etc/hosts","action":"open"}')"
NEG_OPEN_PATH_WRONG_TOKEN="$(http_status POST "http://127.0.0.1:${PORT}/api/open-path" '{"path":"/etc/hosts","action":"open"}' wrong-token)"

export VERSION
export SMOKE_DMG_NAME
SMOKE_DMG_NAME="$(basename "$DMG_PATH")"
export SMOKE_ARCH="$ARCH"
export SMOKE_SHA256="$SHA256"
export SMOKE_BYTES="$BYTES"
export SMOKE_SIGNATURE="$SIGNATURE"
export SMOKE_GATEKEEPER="$GATEKEEPER"
export SMOKE_GATEKEEPER_INSTRUCTIONS_SHOWN="$GATEKEEPER_INSTRUCTIONS_SHOWN"
export SMOKE_EVIDENCE_URL="$RUN_URL"
export NEG_MESSAGE_NO_TOKEN NEG_MESSAGE_WRONG_TOKEN NEG_MESSAGE_QUERY_TOKEN_REJECTED
export NEG_STREAM_NO_TOKEN NEG_STREAM_WRONG_TOKEN NEG_STREAM_QUERY_TOKEN_REJECTED
export NEG_FILE_STAT_NO_TOKEN NEG_FILE_STAT_WRONG_TOKEN NEG_FILE_SERVE_NO_TOKEN NEG_FILE_SERVE_WRONG_TOKEN
export NEG_OPEN_PATH_NO_TOKEN NEG_OPEN_PATH_WRONG_TOKEN

python3 - "$OUTPUT_PATH" "$VERSION_JSON" "$AUTH_JSON" <<'PY'
import json
import os
import pathlib
import sys

output = pathlib.Path(sys.argv[1])
version_payload = json.loads(sys.argv[2])
auth_payload = json.loads(sys.argv[3])
expected = os.environ["VERSION"]
instructions = "README-macos-agent-install.txt"
negative_statuses = {
    "messageNoToken": os.environ.get("NEG_MESSAGE_NO_TOKEN", ""),
    "messageWrongToken": os.environ.get("NEG_MESSAGE_WRONG_TOKEN", ""),
    "messageQueryTokenRejected": os.environ.get("NEG_MESSAGE_QUERY_TOKEN_REJECTED", ""),
    "streamNoToken": os.environ.get("NEG_STREAM_NO_TOKEN", ""),
    "streamWrongToken": os.environ.get("NEG_STREAM_WRONG_TOKEN", ""),
    "streamQueryTokenRejected": os.environ.get("NEG_STREAM_QUERY_TOKEN_REJECTED", ""),
    "fileStatNoToken": os.environ.get("NEG_FILE_STAT_NO_TOKEN", ""),
    "fileStatWrongToken": os.environ.get("NEG_FILE_STAT_WRONG_TOKEN", ""),
    "fileServeNoToken": os.environ.get("NEG_FILE_SERVE_NO_TOKEN", ""),
    "fileServeWrongToken": os.environ.get("NEG_FILE_SERVE_WRONG_TOKEN", ""),
    "openPathNoToken": os.environ.get("NEG_OPEN_PATH_NO_TOKEN", ""),
    "openPathWrongToken": os.environ.get("NEG_OPEN_PATH_WRONG_TOKEN", ""),
}
ok = (
    version_payload.get("version") == expected
    and auth_payload.get("status") == "success"
    and bool(auth_payload.get("auth_required"))
    and not bool(auth_payload.get("authenticated"))
    and all(str(value) == "401" for value in negative_statuses.values())
)
payload = {
    "status": "pass" if ok else "fail",
    "version": expected,
    "artifact": os.environ["SMOKE_DMG_NAME"],
    "arch": os.environ["SMOKE_ARCH"],
    "sha256": os.environ["SMOKE_SHA256"],
    "bytes": int(os.environ["SMOKE_BYTES"]),
    "signature": os.environ["SMOKE_SIGNATURE"],
    "mounted": True,
    "appFound": True,
    "copied": True,
    "launched": True,
    "versionOk": version_payload.get("version") == expected,
    "sidecarReady": version_payload.get("version") == expected,
    "authReady": auth_payload.get("status") == "success",
    "authRequired": bool(auth_payload.get("auth_required")),
    "authNegativeReady": all(str(value) == "401" for value in negative_statuses.values()),
    "authNegativeStatuses": {key: int(value) if str(value).isdigit() else value for key, value in negative_statuses.items()},
    "gatekeeper": os.environ["SMOKE_GATEKEEPER"],
    "gatekeeperInstructionShown": os.environ["SMOKE_GATEKEEPER_INSTRUCTIONS_SHOWN"] == "true",
    "gatekeeperInstructions": instructions,
    "instructions": instructions,
    "runId": os.environ.get("GITHUB_RUN_ID", ""),
    "runAttempt": os.environ.get("GITHUB_RUN_ATTEMPT", ""),
    "commit": os.environ.get("GITHUB_SHA", ""),
    "evidence": os.environ["SMOKE_EVIDENCE_URL"],
    "evidenceUrl": os.environ["SMOKE_EVIDENCE_URL"],
}
output.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
print(json.dumps(payload, indent=2, ensure_ascii=False))
if not ok:
    raise SystemExit(1)
PY
