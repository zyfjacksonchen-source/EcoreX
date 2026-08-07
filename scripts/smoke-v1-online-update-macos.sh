#!/bin/sh
set -eu

VERSION=${1:?target version required}
FROM_VERSION=${2:?source version required}
ARCH=${3:?architecture required}
RECEIPT=${4:?receipt required}
test "$(uname -m)" = "$ARCH"

ROOT=$(mktemp -d "${RUNNER_TEMP:-/tmp}/ecorex-online-update.XXXXXX")
OLD_PID=
NEW_PID=
cleanup() {
  for pid in "$NEW_PID" "$OLD_PID"; do
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
      kill "$pid" 2>/dev/null || true
      wait "$pid" 2>/dev/null || true
    fi
  done
  rm -rf "$ROOT"
}
trap cleanup EXIT HUP INT TERM

export HOME="$ROOT/home"
mkdir -p "$HOME/Desktop" "$ROOT/old"
OLD_PACKAGE="$ROOT/old.zip"
NEW_PACKAGE="$ROOT/new.zip"
curl --fail --location --retry 4 --retry-all-errors --connect-timeout 20 \
  --output "$OLD_PACKAGE" \
  "https://github.com/zyfjacksonchen-source/EcoreX-installers/releases/download/v${FROM_VERSION}/EcoreX_${FROM_VERSION}-webui-macos-universal.zip"
curl --fail --location --retry 4 --retry-all-errors --connect-timeout 20 \
  --output "$ROOT/manifest.json" \
  "https://dl.ecoremedia.net/ecorex-agent/manifest.json"
VERSION="$VERSION" MANIFEST="$ROOT/manifest.json" python - <<'PY' >"$ROOT/target.json"
import json, os
from pathlib import Path
value = json.loads(Path(os.environ['MANIFEST']).read_text())
item = next(x for x in value['artifacts'] if x['id'] == 'webui-macos-universal')
if value['version'] != os.environ['VERSION'] or item['status'] != 'ready':
    raise SystemExit('public_manifest_target_invalid')
print(json.dumps({'file': item['fileName'], 'size': item['size'], 'sha256': item['sha256'].lower()}))
PY
TARGET_FILE=$(python -c 'import json,sys;print(json.load(open(sys.argv[1]))["file"])' "$ROOT/target.json")
curl --fail --location --retry 4 --retry-all-errors --connect-timeout 20 \
  --output "$NEW_PACKAGE" \
  "https://dl.ecoremedia.net/ecorex-agent/downloads/${TARGET_FILE}"
NEW_PACKAGE="$NEW_PACKAGE" TARGET="$ROOT/target.json" python - <<'PY'
import hashlib, json, os
from pathlib import Path
target = json.loads(Path(os.environ['TARGET']).read_text())
payload = Path(os.environ['NEW_PACKAGE']).read_bytes()
if len(payload) != target['size'] or hashlib.sha256(payload).hexdigest() != target['sha256']:
    raise SystemExit('public_package_integrity_invalid')
PY

/usr/bin/ditto -x -k "$OLD_PACKAGE" "$ROOT/old"
OLD_INSTALLER="$ROOT/old/e-Mate WebUI/Install e-Mate WebUI.command"
test -x "$OLD_INSTALLER"
INSTALL_ROOT="$ROOT/install"

"$OLD_INSTALLER" --install-root "$INSTALL_ROOT" >"$ROOT/old.stdout" 2>"$ROOT/old.stderr" &
OLD_PID=$!
for _ in $(seq 1 300); do
  if curl --fail --silent --max-time 2 http://127.0.0.1:8765/api/version >"$ROOT/old-version.json"; then break; fi
  kill -0 "$OLD_PID" 2>/dev/null || exit 1
  sleep 1
done
for _ in $(seq 1 300); do
  if curl --fail --silent --max-time 2 'http://127.0.0.1:8765/api/update-check?platform=darwin' >"$ROOT/update-notice.json" && \
    VERSION="$VERSION" FROM_VERSION="$FROM_VERSION" NOTICE="$ROOT/update-notice.json" python - <<'PY'
import json, os
from pathlib import Path
value = json.loads(Path(os.environ['NOTICE']).read_text())
raise SystemExit(0 if value.get('currentVersion') == os.environ['FROM_VERSION'] and value.get('latestVersion') == os.environ['VERSION'] and value.get('hasUpdate') is True and (value.get('artifact') or {}).get('id') == 'webui-macos-universal' else 1)
PY
  then break; fi
  kill -0 "$OLD_PID" 2>/dev/null || exit 1
  sleep 1
done
kill "$OLD_PID" 2>/dev/null || true
wait "$OLD_PID" 2>/dev/null || true
OLD_PID=
for _ in $(seq 1 60); do
  if ! curl --silent --max-time 1 http://127.0.0.1:8765/api/version >/dev/null 2>&1; then break; fi
  sleep 1
done

ONLINE_BOOTSTRAP=$(INSTALL_ROOT="$INSTALL_ROOT" python - <<'PY'
import json, os
from pathlib import Path
root = Path(os.environ['INSTALL_ROOT']).resolve()
value = json.loads((root / 'bootstrap/desktop-entry.json').read_text())
path = Path(value['launcher_path']).resolve()
path.relative_to(root / 'bootstrap/versions')
if path.name != 'ecorex-bootstrap' or not path.is_file():
    raise SystemExit('online_bootstrap_invalid')
print(path)
PY
)
"$ONLINE_BOOTSTRAP" --install-root "$INSTALL_ROOT" >"$ROOT/new.stdout" 2>"$ROOT/new.stderr" &
NEW_PID=$!
for _ in $(seq 1 1200); do
  if curl --fail --silent --max-time 2 http://127.0.0.1:8765/api/version >"$ROOT/new-version.json"; then break; fi
  kill -0 "$NEW_PID" 2>/dev/null || exit 1
  sleep 1
done

VERSION="$VERSION" FROM_VERSION="$FROM_VERSION" ARCH="$ARCH" RECEIPT="$RECEIPT" ROOT="$ROOT" INSTALL_ROOT="$INSTALL_ROOT" python - <<'PY'
import hashlib, json, os
from datetime import datetime, timezone
from pathlib import Path
root = Path(os.environ['ROOT'])
install = Path(os.environ['INSTALL_ROOT'])
source = json.loads((root / 'old-version.json').read_text())
target = json.loads((root / 'new-version.json').read_text())
opened = json.loads((install / 'bootstrap/browser-opened.json').read_text())
slots = [path for path in (install / 'slots').iterdir() if path.is_dir()]
if source.get('version') != os.environ['FROM_VERSION'] or target.get('version') != os.environ['VERSION']:
    raise SystemExit('online_update_version_invalid')
if opened.get('status') != 'opened' or opened.get('version') != os.environ['VERSION'] or opened.get('url') != 'http://127.0.0.1:8765/':
    raise SystemExit('target_browser_open_invalid')
if len(slots) < 2:
    raise SystemExit('source_slot_not_retained')
notice = json.loads((root / 'update-notice.json').read_text())
receipt = Path(os.environ['RECEIPT'])
receipt.parent.mkdir(parents=True, exist_ok=True)
receipt.write_text(json.dumps({
    'schema_version': 1,
    'status': 'passed',
    'target': 'macos-arm64' if os.environ['ARCH'] == 'arm64' else 'macos-x64',
    'from_version': os.environ['FROM_VERSION'],
    'version': os.environ['VERSION'],
    'downloaded_from_public_production': True,
    'notification_observed': notice.get('hasUpdate') is True,
    'online_bootstrap_executed': True,
    'source_slot_retained': True,
    'automatic_browser_open': True,
    'browser_url': opened['url'],
    'browser_receipt_sha256': hashlib.sha256((install / 'bootstrap/browser-opened.json').read_bytes()).hexdigest(),
    'completed_at': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
}, sort_keys=True, separators=(',', ':')) + '\n')
PY
