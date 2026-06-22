#!/usr/bin/env bash
set -euo pipefail

VERSION="${VERSION:-0.1.19}"
ARTIFACT_URL="${ARTIFACT_URL:-https://www.ecoreai.cn/ecorex-agent/downloads/EcoreX_${VERSION}-webui-macos-universal.zip}"
RELEASE_TAG="${RELEASE_TAG:-v${VERSION}}"
RELEASE_ASSET_NAME="${RELEASE_ASSET_NAME:-}"
PORT="${ECOREX_WEB_PORT:-19090}"
WORK_ROOT="${RUNNER_TEMP:-/tmp}/ecorex-webui-macos-smoke-${VERSION}-$$"
DOWNLOAD_DIR="$WORK_ROOT/download"
EXTRACT_DIR="$WORK_ROOT/extract"
FAKE_BIN="$WORK_ROOT/bin"
INSTALL_ROOT="$WORK_ROOT/install"
WORKSPACE_ROOT="$WORK_ROOT/workspace"
OPEN_CAPTURE="$WORK_ROOT/open-calls.log"
SMOKE_HOME="$WORK_ROOT/home"
WRAPPER_STATE_DIR="$SMOKE_HOME/Library/Application Support/EcoreX WebUI/state"

log() {
  printf '[ecorex-webui-macos-smoke] %s\n' "$*"
}

dump_logs() {
  for file in \
    "$WRAPPER_STATE_DIR/install.log" \
    "$WRAPPER_STATE_DIR/install.err.log" \
    "$INSTALL_ROOT/state/install.log" \
    "$INSTALL_ROOT/state/install.err.log" \
    "$INSTALL_ROOT/state/ecorex-webui.log" \
    "$INSTALL_ROOT/state/ecorex-webui.err.log" \
    "$INSTALL_ROOT/state/lark-cli-install.log"; do
    if [[ -f "$file" ]]; then
      log "---- $file ----"
      tail -n 120 "$file" || true
    fi
  done
  if [[ -f "$OPEN_CAPTURE" ]]; then
    log "---- captured open calls ----"
    cat "$OPEN_CAPTURE" || true
  fi
}

cleanup() {
  set +e
  if [[ -f "$INSTALL_ROOT/state/ecorex-webui.pid" ]]; then
    pid="$(cat "$INSTALL_ROOT/state/ecorex-webui.pid" 2>/dev/null || true)"
    if [[ -n "$pid" ]]; then
      kill "$pid" >/dev/null 2>&1 || true
    fi
  fi
  pkill -f "$INSTALL_ROOT/runtime/app.py" >/dev/null 2>&1 || true
  rm -rf "$WORK_ROOT"
}
trap cleanup EXIT
trap 'dump_logs' ERR

mkdir -p "$DOWNLOAD_DIR" "$EXTRACT_DIR" "$FAKE_BIN" "$INSTALL_ROOT" "$WORKSPACE_ROOT" "$SMOKE_HOME" "$WRAPPER_STATE_DIR"

if [[ -n "$RELEASE_ASSET_NAME" ]]; then
  : "${GITHUB_REPOSITORY:?GITHUB_REPOSITORY is required when RELEASE_ASSET_NAME is set}"
  : "${GITHUB_TOKEN:?GITHUB_TOKEN is required when RELEASE_ASSET_NAME is set}"
  log "Downloading GitHub Release asset ${RELEASE_TAG}/${RELEASE_ASSET_NAME}"
  python3 - "$GITHUB_REPOSITORY" "$RELEASE_TAG" "$RELEASE_ASSET_NAME" "$DOWNLOAD_DIR/webui-macos.zip" <<'PY'
import json
import os
import sys
import urllib.request

repo, tag, asset_name, output = sys.argv[1:5]
token = os.environ["GITHUB_TOKEN"]
api_headers = {
    "Authorization": f"Bearer {token}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}
release_req = urllib.request.Request(
    f"https://api.github.com/repos/{repo}/releases/tags/{tag}",
    headers=api_headers,
)
with urllib.request.urlopen(release_req, timeout=30) as response:
    release = json.loads(response.read().decode("utf-8"))
asset = next((item for item in release.get("assets", []) if item.get("name") == asset_name), None)
if not asset:
    raise SystemExit(f"Release asset not found: {tag}/{asset_name}")
download_headers = dict(api_headers)
download_headers["Accept"] = "application/octet-stream"
asset_req = urllib.request.Request(asset["url"], headers=download_headers)
with urllib.request.urlopen(asset_req, timeout=60) as response, open(output, "wb") as handle:
    while True:
        chunk = response.read(1024 * 1024)
        if not chunk:
            break
        handle.write(chunk)
PY
else
  log "Downloading $ARTIFACT_URL"
  curl --fail --location --retry 3 --retry-delay 2 \
    --output "$DOWNLOAD_DIR/webui-macos.zip" \
    "$ARTIFACT_URL"
fi

log "Extracting package"
unzip -q "$DOWNLOAD_DIR/webui-macos.zip" -d "$EXTRACT_DIR"
APP_DIR="$(find "$EXTRACT_DIR" -maxdepth 3 -type d -name "Install EcoreX WebUI.app" | head -n 1)"
if [[ -z "$APP_DIR" ]]; then
  log "Installer app not found under $EXTRACT_DIR"
  find "$EXTRACT_DIR" -maxdepth 2 -print
  exit 1
fi

APP_EXEC="$APP_DIR/Contents/MacOS/Install EcoreX WebUI"
INSTALL_SCRIPT="$APP_DIR/Contents/Resources/package/scripts/install-ecorex-webui-mac.sh"

[[ -d "$APP_DIR" ]] || { log "Missing installer app: $APP_DIR"; exit 1; }
[[ -f "$APP_EXEC" ]] || { log "Missing installer executable: $APP_EXEC"; exit 1; }
[[ -f "$INSTALL_SCRIPT" ]] || { log "Missing install script: $INSTALL_SCRIPT"; exit 1; }
chmod +x "$APP_EXEC" "$INSTALL_SCRIPT"

if find "$EXTRACT_DIR" -name "Install EcoreX WebUI.command" -print -quit | grep -q .; then
  log "Unexpected Terminal-opening .command launcher found"
  exit 1
fi

if /usr/libexec/PlistBuddy -c "Print :LSUIElement" "$APP_DIR/Contents/Info.plist" | grep -q "true"; then
  log "Installer app is LSUIElement=true"
else
  log "Installer app is missing LSUIElement=true"
  exit 1
fi

cat > "$FAKE_BIN/open" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
: "${ECOREX_WEBUI_OPEN_CAPTURE:?missing ECOREX_WEBUI_OPEN_CAPTURE}"
printf '%s\n' "$*" >> "$ECOREX_WEBUI_OPEN_CAPTURE"
exit 0
SH
chmod +x "$FAKE_BIN/open"

export ECOREX_WEBUI_INSTALL_ROOT="$INSTALL_ROOT"
export ECOREX_WORKSPACE_ROOT="$WORKSPACE_ROOT"
export ECOREX_WEB_PORT="$PORT"
export ECOREX_WEBUI_OPEN_CAPTURE="$OPEN_CAPTURE"
export OPEN_BROWSER=1
export HOME="$SMOKE_HOME"
export PATH="$FAKE_BIN:$PATH"

log "Launching installer app executable"
"$APP_EXEC"

URL=""
for i in $(seq 1 900); do
  if [[ -f "$OPEN_CAPTURE" ]]; then
    URL="$(grep -Eo 'http://127\.0\.0\.1:[0-9]+/app/' "$OPEN_CAPTURE" | tail -n 1 || true)"
    if [[ -n "$URL" ]]; then
      break
    fi
  fi
  if [[ -f "$WRAPPER_STATE_DIR/install.err.log" ]] && grep -Eq "EcoreX WebUI did not become ready|installation failed|Missing bundled Python archive|Unsupported macOS architecture|Could not locate python3" "$WRAPPER_STATE_DIR/install.err.log"; then
    log "Installer reported readiness failure"
    dump_logs
    exit 1
  fi
  if [[ "$((i % 60))" == "0" ]]; then
    log "Still waiting for installer to open the browser URL (${i}s elapsed)"
  fi
  sleep 1
done

if [[ -z "$URL" ]]; then
  log "Installer did not attempt to open the browser URL"
  dump_logs
  exit 1
fi

log "Captured browser open URL: $URL"
"$INSTALL_ROOT/python/bin/python3" - "$URL" <<'PY'
import sys
import time
import urllib.error
import urllib.request

url = sys.argv[1]
for _ in range(30):
    try:
        with urllib.request.urlopen(url, timeout=2) as response:
            if response.status < 500:
                print(f"ready: {url} -> {response.status}")
                raise SystemExit(0)
    except urllib.error.HTTPError as exc:
        if exc.code < 500:
            print(f"ready: {url} -> {exc.code}")
            raise SystemExit(0)
    except Exception:
        time.sleep(1)
raise SystemExit(f"WebUI did not respond at {url}")
PY

log "macOS WebUI install smoke passed"
