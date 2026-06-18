EcoreX macOS Agent Install README

Audience: local automation agents such as WorkBuddy, Codex, or other trusted
desktop agents that the user explicitly asks to install EcoreX from a DMG.

Use this file only for EcoreX DMG files downloaded from the official EcoreX
download page or the official GitHub release. Do not use these steps for an
unknown DMG.

Official download page:
https://www.ecoreai.cn/ecorex-agent/

Before installing
-----------------
1. Confirm the DMG filename starts with EcoreX_ and ends with .dmg.
2. Confirm the SHA256 hash matches the published manifest or release note:

   shasum -a 256 "/path/to/EcoreX_0.1.14_arm64.dmg"
   shasum -a 256 "/path/to/EcoreX_0.1.14_x64.dmg"

3. Prefer the correct architecture:
   - Apple Silicon: EcoreX_*_arm64.dmg
   - Intel: EcoreX_*_x64.dmg

Agent install script
--------------------
Run this from Terminal, replacing DMG_PATH and EXPECTED_SHA256 as needed.
EXPECTED_SHA256 is optional, but strongly recommended.

#!/bin/bash
set -euo pipefail

DMG_PATH="${1:?Usage: install-ecorex-macos.sh /path/to/EcoreX.dmg [expected_sha256]}"
EXPECTED_SHA256="${2:-}"
INSTALL_DIR="${ECOREX_INSTALL_DIR:-/Applications}"
APP_NAME="EcoreX.app"
APP_DST="$INSTALL_DIR/$APP_NAME"
MOUNT_PATH=""

if [[ ! -f "$DMG_PATH" ]]; then
  echo "DMG not found: $DMG_PATH" >&2
  exit 1
fi

if [[ -n "$EXPECTED_SHA256" ]]; then
  ACTUAL_SHA256="$(shasum -a 256 "$DMG_PATH" | awk '{print toupper($1)}')"
  EXPECTED_UPPER="$(printf '%s' "$EXPECTED_SHA256" | tr '[:lower:]' '[:upper:]')"
  if [[ "$ACTUAL_SHA256" != "$EXPECTED_UPPER" ]]; then
    echo "SHA256 mismatch." >&2
    echo "Expected: $EXPECTED_UPPER" >&2
    echo "Actual:   $ACTUAL_SHA256" >&2
    exit 1
  fi
fi

cleanup() {
  if [[ -n "$MOUNT_PATH" ]]; then
    hdiutil detach "$MOUNT_PATH" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

MOUNT_PATH="$(hdiutil attach -nobrowse -readonly "$DMG_PATH" | awk '/\/Volumes\// {print substr($0, index($0, "/Volumes/")); exit}')"
APP_SRC="$MOUNT_PATH/$APP_NAME"

if [[ ! -d "$APP_SRC" ]]; then
  echo "EcoreX.app not found in mounted DMG: $MOUNT_PATH" >&2
  exit 1
fi

mkdir -p "$INSTALL_DIR" 2>/dev/null || {
  INSTALL_DIR="$HOME/Applications"
  APP_DST="$INSTALL_DIR/$APP_NAME"
  mkdir -p "$INSTALL_DIR"
}

osascript -e 'quit app "EcoreX"' >/dev/null 2>&1 || true

if [[ -d "$APP_DST" ]]; then
  BACKUP_PATH="$APP_DST.backup.$(date +%Y%m%d%H%M%S)"
  mv "$APP_DST" "$BACKUP_PATH"
  echo "Existing EcoreX.app moved to: $BACKUP_PATH"
fi

ditto "$APP_SRC" "$APP_DST"

# EcoreX v0.1.14 macOS DMGs may be unsigned/not notarized. Do not disable
# Gatekeeper globally. Remove quarantine only from this verified EcoreX.app.
xattr -dr com.apple.quarantine "$APP_DST" 2>/dev/null || true

# Optional local trust label. This may fail without admin policy permission;
# failure is non-fatal because quarantine removal is the primary step.
spctl --add --label "EcoreX Local Trusted" "$APP_DST" 2>/dev/null || true

open "$APP_DST"
echo "EcoreX installed and launched from: $APP_DST"

Important safety rules for agents
---------------------------------
- Do not run: sudo spctl --master-disable
- Do not change global Gatekeeper settings.
- Do not remove quarantine from arbitrary downloads.
- Only remove quarantine from EcoreX.app after verifying the DMG source/hash.
- Preserve user data. Do not delete ~/Library/Application Support/EcoreX or any
  EcoreX workspace/session directory during install or upgrade.

