#!/usr/bin/env bash
set -euo pipefail

BASE_URL="${ECOREX_BASE_URL:-https://mvdcm.ecoremedia.net/ecorex-agent}"
REQUESTED_VERSION="${ECOREX_VERSION:-}"
OPEN_BROWSER="${OPEN_BROWSER:-1}"

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "Missing required command: $1" >&2
    exit 1
  }
}

join_url() {
  case "$2" in
    http://*|https://*) printf '%s\n' "$2" ;;
    *) printf '%s/%s\n' "${1%/}" "${2#/}" ;;
  esac
}

manifest_value() {
  /usr/bin/plutil -extract "$1" raw -o - "$MANIFEST_JSON" 2>/dev/null || true
}

sha256_file() {
  shasum -a 256 "$1" | awk '{print toupper($1)}'
}

download_file() {
  local url="$1"
  local destination="$2"
  local expected_sha="$3"
  local partial="$destination.part"
  local attempt
  local status
  local retry_all_errors_flag=""
  if curl --help all 2>/dev/null | grep -q -- '--retry-all-errors'; then
    retry_all_errors_flag="--retry-all-errors"
  fi

  if [[ -f "$destination" ]] && [[ "$(sha256_file "$destination")" == "$expected_sha" ]]; then
    echo "Using cached package: $destination"
    return 0
  fi

  if [[ -f "$destination" ]]; then
    echo "Cached package hash mismatch; downloading again." >&2
    rm -f "$destination"
  fi

  echo "Downloading $url"
  for attempt in 1 2 3 4 5; do
    local curl_args=(-fL --retry 5 --retry-delay 5 --retry-max-time 900 --connect-timeout 45 --speed-time 120 --speed-limit 512 --progress-bar)
    local resume_args=()
    if [[ -n "$retry_all_errors_flag" ]]; then
      curl_args+=("$retry_all_errors_flag")
    fi
    if [[ -s "$partial" ]]; then
      echo "Resuming download from existing partial file, attempt $attempt/5..."
      resume_args=(-C -)
    else
      echo "Starting download, attempt $attempt/5..."
    fi
    if curl "${curl_args[@]}" "${resume_args[@]}" "$url" -o "$partial"; then
      status=0
    else
      status=$?
    fi
    if [[ "$status" == "0" ]]; then
      actual_sha="$(sha256_file "$partial")"
      if [[ "$actual_sha" == "$expected_sha" ]]; then
        mv "$partial" "$destination"
        echo "Download verified: $destination"
        return 0
      fi
      echo "Downloaded package SHA256 mismatch for $destination: $actual_sha" >&2
      rm -f "$partial"
      if [[ "$attempt" == "5" ]]; then
        exit 1
      fi
      sleep $((attempt * 3))
      continue
    fi
    if [[ -s "$partial" ]] && [[ "$(sha256_file "$partial")" == "$expected_sha" ]]; then
      echo "Partial package was already complete."
      mv "$partial" "$destination"
      echo "Download verified: $destination"
      return 0
    fi
    if [[ "$attempt" == "5" ]]; then
      echo "Download failed after $attempt attempts. You can rerun this installer to retry." >&2
      exit "$status"
    fi
    sleep $((attempt * 3))
  done
}

need_cmd curl
need_cmd unzip
need_cmd shasum
need_cmd awk
need_cmd /usr/bin/plutil

MANIFEST_URL="$(join_url "$BASE_URL" "manifest.json")"
TMP_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/ecorex-webui-install.XXXXXX")"
cleanup() {
  rm -rf "$TMP_ROOT"
}
trap cleanup EXIT

MANIFEST_JSON="$TMP_ROOT/manifest.json"
echo "Fetching EcoreX manifest: $MANIFEST_URL"
curl -fsSL --retry 3 --retry-delay 2 --connect-timeout 20 "$MANIFEST_URL" -o "$MANIFEST_JSON"

VERSION="$(manifest_value "version")"
echo "EcoreX WebUI installer script: 0.2.7.1"
echo "EcoreX WebUI manifest version: $VERSION"
if [[ -n "$REQUESTED_VERSION" && "$REQUESTED_VERSION" != "$VERSION" ]]; then
  echo "Manifest version '$VERSION' does not match requested '$REQUESTED_VERSION'." >&2
  exit 1
fi

ARTIFACT_HREF=""
ARTIFACT_NAME=""
ARTIFACT_SHA=""
for index in $(seq 0 40); do
  artifact_id="$(manifest_value "artifacts.$index.id")"
  if [[ "$artifact_id" != "webui-macos-universal" ]]; then
    continue
  fi

  artifact_status="$(manifest_value "artifacts.$index.status")"
  if [[ "$artifact_status" != "ready" ]]; then
    continue
  fi

  ARTIFACT_HREF="$(manifest_value "artifacts.$index.href")"
  ARTIFACT_NAME="$(manifest_value "artifacts.$index.fileName")"
  ARTIFACT_SHA="$(manifest_value "artifacts.$index.sha256" | tr '[:lower:]' '[:upper:]')"
  break
done

if [[ -z "$ARTIFACT_HREF" || -z "$ARTIFACT_NAME" || -z "$ARTIFACT_SHA" ]]; then
  echo "Ready webui-macos-universal artifact was not found in manifest." >&2
  exit 1
fi

ARTIFACT_URL="$(join_url "$BASE_URL" "$ARTIFACT_HREF")"
CACHE_ROOT="${ECOREX_CACHE_ROOT:-$HOME/Library/Caches/EcoreX WebUI/downloads}"
ZIP_PATH="$CACHE_ROOT/$ARTIFACT_NAME"
EXTRACT_ROOT="$TMP_ROOT/extract"
mkdir -p "$CACHE_ROOT" "$EXTRACT_ROOT"

download_file "$ARTIFACT_URL" "$ZIP_PATH" "$ARTIFACT_SHA"

echo "Extracting package..."
unzip -q "$ZIP_PATH" -d "$EXTRACT_ROOT"
echo "Package extracted."

INSTALL_SCRIPT="$(find "$EXTRACT_ROOT" -path "*/scripts/install-ecorex-webui-mac.sh" -type f | head -n 1)"
if [[ -z "$INSTALL_SCRIPT" ]]; then
  echo "macOS WebUI installer was not found in the downloaded package." >&2
  exit 1
fi

echo "Starting EcoreX WebUI local installer..."
OPEN_BROWSER="$OPEN_BROWSER" bash "$INSTALL_SCRIPT"
echo "EcoreX WebUI $VERSION installed."
echo "If the browser did not open, double-click the desktop EcoreX WebUI.webloc shortcut or rerun this command."
