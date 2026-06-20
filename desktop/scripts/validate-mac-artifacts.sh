#!/usr/bin/env bash
set -euo pipefail

RELEASE_DIR="${1:-release}"
STRICT_SIGNING="${ECOREX_MAC_STRICT_SIGNING:-0}"

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
  if codesign -dv --verbose=4 "$app" >/dev/null 2>&1; then
    codesign --verify --deep --strict --verbose=2 "$app"
  elif [[ "$STRICT_SIGNING" == "1" ]]; then
    echo "codesign rejected $app and ECOREX_MAC_STRICT_SIGNING=1." >&2
    exit 2
  else
    echo "codesign rejected $app; continuing because unsigned macOS artifacts are allowed for this release lane." >&2
  fi
  if ! spctl -a -vv --type execute "$app"; then
    if [[ "$STRICT_SIGNING" == "1" ]]; then
      echo "spctl rejected $app." >&2
      exit 2
    fi
    echo "spctl rejected $app; record Gatekeeper instructions and install-smoke evidence for ready-unsigned publication." >&2
  fi
done

for dmg in "${dmgs[@]}"; do
  [[ -e "$dmg" ]] || continue
  echo "Inspecting DMG: $dmg"
  hdiutil verify "$dmg"
  if ! spctl -a -vv -t open --context context:primary-signature "$dmg"; then
    if [[ "$STRICT_SIGNING" == "1" ]]; then
      echo "spctl rejected $dmg." >&2
      exit 2
    fi
    echo "spctl rejected $dmg; continuing as ready-unsigned candidate." >&2
  fi
  if ! xcrun stapler validate "$dmg"; then
    if [[ "$STRICT_SIGNING" == "1" ]]; then
      echo "stapler validation failed for $dmg." >&2
      exit 2
    fi
    echo "stapler validation failed for $dmg; continuing as ready-unsigned candidate." >&2
  fi
done

echo "macOS artifact validation passed for strict=$STRICT_SIGNING."
