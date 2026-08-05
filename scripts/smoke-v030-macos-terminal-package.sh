#!/bin/sh
set -eu

PACKAGE=${1:?package required}
EXPECTED_ARCH=${2:?architecture required}
RECEIPT=${3:?receipt required}
ACTUAL_ARCH=$(uname -m)
test "$ACTUAL_ARCH" = "$EXPECTED_ARCH"
test -f "$PACKAGE"

ROOT=$(mktemp -d "${RUNNER_TEMP:-/tmp}/emate-macos-user.XXXXXX")
INSTALL_PID=
cleanup() {
  if [ -n "$INSTALL_PID" ] && kill -0 "$INSTALL_PID" 2>/dev/null; then
    kill "$INSTALL_PID" 2>/dev/null || true
    wait "$INSTALL_PID" 2>/dev/null || true
  fi
  rm -rf "$ROOT"
}
trap cleanup EXIT HUP INT TERM

mkdir -p "$HOME/Desktop" "$ROOT/package"
test ! -e "$HOME/Desktop/e-Mate.app"
/usr/bin/ditto -x -k "$PACKAGE" "$ROOT/package"
INSTALLER="$ROOT/package/e-Mate WebUI/Install e-Mate WebUI.command"
test -x "$INSTALLER"

INSTALL_ROOT="$ROOT/install"
"$INSTALLER" --install-root "$INSTALL_ROOT" \
  >"$ROOT/install.stdout" 2>"$ROOT/install.stderr" &
INSTALL_PID=$!

READY=false
for _ in $(seq 1 300); do
  if curl --fail --silent --show-error --max-time 2 \
      http://127.0.0.1:8765/api/version >"$ROOT/version.json"; then
    READY=true
    break
  fi
  kill -0 "$INSTALL_PID" 2>/dev/null || {
    sed -n '1,160p' "$ROOT/install.stderr" >&2
    exit 1
  }
  sleep 1
done
test "$READY" = true
test -x "$HOME/Desktop/e-Mate.app/Contents/MacOS/EcoreX"

VERSION_FILE="$ROOT/version.json" INSTALL_ROOT="$INSTALL_ROOT" python - <<'PY'
import json, os
from pathlib import Path
version = json.loads(Path(os.environ['VERSION_FILE']).read_text())
if version.get('product') != 'e-Mate' or version.get('version') != '0.3.1':
    raise SystemExit('installed_version_invalid')
entry = json.loads((Path.home() / 'Desktop/e-Mate.app/Contents/Resources/ecorex-entry.json').read_text())
if Path(entry.get('install_root', '')).resolve() != Path(os.environ['INSTALL_ROOT']).resolve():
    raise SystemExit('desktop_entry_install_root_invalid')
PY

"$HOME/Desktop/e-Mate.app/Contents/MacOS/EcoreX" \
  >"$ROOT/launcher.stdout" 2>"$ROOT/launcher.stderr" &
LAUNCH_PID=$!
for _ in $(seq 1 60); do
  kill -0 "$LAUNCH_PID" 2>/dev/null || break
  sleep 1
done
if kill -0 "$LAUNCH_PID" 2>/dev/null; then
  kill "$LAUNCH_PID" 2>/dev/null || true
  exit 1
fi
wait "$LAUNCH_PID"
curl --fail --silent --show-error --max-time 5 \
  http://127.0.0.1:8765/api/version >"$ROOT/version-after-launch.json"

PACKAGE="$PACKAGE" RECEIPT="$RECEIPT" EXPECTED_ARCH="$EXPECTED_ARCH" python - <<'PY'
import hashlib, json, os
from datetime import datetime, timezone
from pathlib import Path
package = Path(os.environ['PACKAGE'])
receipt = Path(os.environ['RECEIPT'])
receipt.parent.mkdir(parents=True, exist_ok=True)
receipt.write_text(json.dumps({
    'schema_version': 1,
    'status': 'passed',
    'product': 'e-Mate',
    'version': '0.3.1',
    'architecture': os.environ['EXPECTED_ARCH'],
    'package_sha256': hashlib.sha256(package.read_bytes()).hexdigest(),
    'installed_runtime_api': True,
    'desktop_entry_launch': True,
    'generated_at': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
}, sort_keys=True, separators=(',', ':')) + '\n', encoding='utf-8')
PY
