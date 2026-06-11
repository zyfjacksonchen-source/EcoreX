#!/usr/bin/env bash
set -euo pipefail

RELEASE_DIR="${1:-release}"

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "validate-mac-artifacts.sh must run on macOS." >&2
  exit 1
fi

if [[ ! -d "$RELEASE_DIR" ]]; then
  echo "Release directory not found: $RELEASE_DIR" >&2
  exit 1
fi

shopt -s nullglob
apps=("$RELEASE_DIR"/mac*/EcoreX.app "$RELEASE_DIR"/EcoreX.app)
dmgs=("$RELEASE_DIR"/EcoreX_*.dmg)

if [[ ${#apps[@]} -eq 0 && ${#dmgs[@]} -eq 0 ]]; then
  echo "No macOS app or DMG artifacts found under $RELEASE_DIR" >&2
  exit 1
fi

for app in "${apps[@]}"; do
  [[ -e "$app" ]] || continue
  echo "Inspecting app bundle: $app"
  test -x "$app/Contents/MacOS/EcoreX"
  test -x "$app/Contents/Resources/ecorex-runtime/python/bin/python3"
  test -f "$app/Contents/Resources/ecorex-runtime/app.py"
  test -f "$app/Contents/Resources/ecorex-runtime/capabilities.json"
  test -f "$app/Contents/Resources/ecorex-runtime/scripts/install-capability.py"
  codesign -dv --verbose=4 "$app" >/dev/null
  codesign --verify --deep --strict --verbose=2 "$app"
  spctl -a -vv --type execute "$app" || {
    echo "spctl rejected $app. This is expected for unsigned local builds, but blocks distribution." >&2
    exit 2
  }
done

for dmg in "${dmgs[@]}"; do
  [[ -e "$dmg" ]] || continue
  echo "Inspecting DMG: $dmg"
  hdiutil verify "$dmg"
  spctl -a -vv -t open --context context:primary-signature "$dmg" || {
    echo "spctl rejected $dmg. Notarization/stapling may be missing." >&2
    exit 2
  }
  xcrun stapler validate "$dmg"
done

echo "macOS artifact validation passed."
