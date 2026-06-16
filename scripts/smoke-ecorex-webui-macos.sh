#!/usr/bin/env bash
set -euo pipefail

VERSION="${VERSION:-0.1.13}"
ARTIFACT_URL="${ARTIFACT_URL:-https://www.ecoreai.cn/ecorex-agent/downloads/EcoreX_${VERSION}-webui-macos-universal.tar.gz}"
PORT="${ECOREX_WEB_PORT:-19090}"
WORK_ROOT="${RUNNER_TEMP:-/tmp}/ecorex-webui-macos-smoke-${VERSION}-$$"
DOWNLOAD_DIR="$WORK_ROOT/download"
EXTRACT_DIR="$WORK_ROOT/extract"
FAKE_BIN="$WORK_ROOT/bin"
INSTALL_ROOT="$WORK_ROOT/install"
WORKSPACE_ROOT="$WORK_ROOT/workspace"
OPEN_CAPTURE="$WORK_ROOT/open-calls.log"

log() {
  printf '[ecorex-webui-macos-smoke] %s\n' "$*"
}

dump_logs() {
  for file in \
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

mkdir -p "$DOWNLOAD_DIR" "$EXTRACT_DIR" "$FAKE_BIN" "$INSTALL_ROOT" "$WORKSPACE_ROOT"

log "Downloading $ARTIFACT_URL"
curl --fail --location --retry 3 --retry-delay 2 \
  --output "$DOWNLOAD_DIR/webui-macos.tar.gz" \
  "$ARTIFACT_URL"

log "Extracting package"
tar -xzf "$DOWNLOAD_DIR/webui-macos.tar.gz" -C "$EXTRACT_DIR"
PACKAGE_DIR="$(find "$EXTRACT_DIR" -maxdepth 1 -type d -name "ecorex-webui-macos-universal-*" | head -n 1)"
if [[ -z "$PACKAGE_DIR" ]]; then
  log "Package root not found under $EXTRACT_DIR"
  find "$EXTRACT_DIR" -maxdepth 2 -print
  exit 1
fi

APP_DIR="$PACKAGE_DIR/Install EcoreX WebUI.app"
APP_EXEC="$APP_DIR/Contents/MacOS/Install EcoreX WebUI"
INSTALL_SCRIPT="$PACKAGE_DIR/scripts/install-ecorex-webui-mac.sh"

[[ -d "$APP_DIR" ]] || { log "Missing installer app: $APP_DIR"; exit 1; }
[[ -f "$APP_EXEC" ]] || { log "Missing installer executable: $APP_EXEC"; exit 1; }
[[ -f "$INSTALL_SCRIPT" ]] || { log "Missing install script: $INSTALL_SCRIPT"; exit 1; }
chmod +x "$APP_EXEC" "$INSTALL_SCRIPT"

if find "$PACKAGE_DIR" -name "Install EcoreX WebUI.command" -print -quit | grep -q .; then
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
export PATH="$FAKE_BIN:$PATH"

log "Launching installer app executable"
"$APP_EXEC"

URL=""
for _ in $(seq 1 360); do
  if [[ -f "$OPEN_CAPTURE" ]]; then
    URL="$(grep -Eo 'http://127\.0\.0\.1:[0-9]+/app/' "$OPEN_CAPTURE" | tail -n 1 || true)"
    if [[ -n "$URL" ]]; then
      break
    fi
  fi
  if [[ -f "$INSTALL_ROOT/state/install.err.log" ]] && grep -q "EcoreX WebUI did not become ready" "$INSTALL_ROOT/state/install.err.log"; then
    log "Installer reported readiness failure"
    dump_logs
    exit 1
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
