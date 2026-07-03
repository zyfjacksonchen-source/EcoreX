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

DOWNLOAD_URLS_TEXT=""

add_download_url() {
  local value="${1:-}"
  value="${value#"${value%%[![:space:]]*}"}"
  value="${value%"${value##*[![:space:]]}"}"
  if [[ -z "$value" ]]; then
    return 0
  fi
  case "$value" in
    http://*|https://*) ;;
    *) return 0 ;;
  esac
  if printf '%s\n' "$DOWNLOAD_URLS_TEXT" | grep -Fx -- "$value" >/dev/null 2>&1; then
    return 0
  fi
  DOWNLOAD_URLS_TEXT="${DOWNLOAD_URLS_TEXT}${value}
"
}

add_download_url_for_base() {
  local base="${1:-}"
  local path_mode="${2:-href}"
  local path="$ARTIFACT_HREF"
  base="${base#"${base%%[![:space:]]*}"}"
  base="${base%"${base##*[![:space:]]}"}"
  base="${base%/}"
  if [[ -z "$base" ]]; then
    return 0
  fi
  case "$base" in
    http://*|https://*) ;;
    *) return 0 ;;
  esac
  if [[ "$path_mode" == "fileName" ]]; then
    path="$ARTIFACT_NAME"
  fi
  if [[ -z "$path" ]]; then
    return 0
  fi
  add_download_url "$(join_url "$base" "$path")"
}

add_download_base_urls_csv() {
  local csv="${1:-}"
  local path_mode="${2:-href}"
  local old_ifs
  local item
  if [[ -z "$csv" ]]; then
    return 0
  fi
  old_ifs="$IFS"
  IFS=","
  set -- $csv
  IFS="$old_ifs"
  for item in "$@"; do
    add_download_url_for_base "$item" "$path_mode"
  done
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
    local should_resume=0
    if [[ -n "$retry_all_errors_flag" ]]; then
      curl_args+=("$retry_all_errors_flag")
    fi
    if [[ -s "$partial" ]]; then
      echo "Resuming download from existing partial file, attempt $attempt/5..."
      should_resume=1
    else
      echo "Starting download, attempt $attempt/5..."
    fi
    if [[ "$should_resume" == "1" ]]; then
      if curl "${curl_args[@]}" -C - "$url" -o "$partial"; then
        status=0
      else
        status=$?
      fi
    elif curl "${curl_args[@]}" "$url" -o "$partial"; then
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
        return 1
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
      return "$status"
    fi
    sleep $((attempt * 3))
  done
}

download_file_from_urls() {
  local destination="$1"
  local expected_sha="$2"
  local url
  local attempted=0
  while IFS= read -r url; do
    if [[ -z "$url" ]]; then
      continue
    fi
    attempted=1
    echo "Trying download source: $url"
    if download_file "$url" "$destination" "$expected_sha"; then
      return 0
    fi
    echo "Download source failed or checksum did not match: $url" >&2
    rm -f "$destination.part"
  done <<EOF
$DOWNLOAD_URLS_TEXT
EOF
  if [[ "$attempted" == "0" ]]; then
    echo "No download source was configured." >&2
  else
    echo "All download sources failed. You can rerun this installer to retry." >&2
  fi
  exit 1
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

add_download_base_urls_csv "${ECOREX_DOWNLOAD_ASSET_BASE_URLS:-}" "fileName"
add_download_base_urls_csv "${ECOREX_DOWNLOAD_BASE_URLS:-}" "href"
for index in $(seq 0 20); do
  add_download_url_for_base "$(manifest_value "download.mirrors.$index.baseUrl")" "$(manifest_value "download.mirrors.$index.pathMode")"
done
for index in $(seq 0 20); do
  add_download_url_for_base "$(manifest_value "download.baseUrls.$index")" "href"
done
add_download_url_for_base "$BASE_URL" "href"

CACHE_ROOT="${ECOREX_CACHE_ROOT:-$HOME/Library/Caches/EcoreX WebUI/downloads}"
ZIP_PATH="$CACHE_ROOT/$ARTIFACT_NAME"
EXTRACT_ROOT="$TMP_ROOT/extract"
mkdir -p "$CACHE_ROOT" "$EXTRACT_ROOT"

download_file_from_urls "$ZIP_PATH" "$ARTIFACT_SHA"

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
