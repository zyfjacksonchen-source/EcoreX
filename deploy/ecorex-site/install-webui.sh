#!/usr/bin/env bash
set -euo pipefail

BASE_URL="${ECOREX_BASE_URL:-https://dl.ecoremedia.net/ecorex-agent}"
REQUESTED_VERSION="${ECOREX_VERSION:-}"
OPEN_BROWSER="${OPEN_BROWSER:-1}"
DOWNLOAD_PARALLEL_PARTS="${ECOREX_DOWNLOAD_PARALLEL_PARTS:-16}"

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

file_size() {
  wc -c < "$1" | tr -d '[:space:]'
}

format_mib() {
  awk -v bytes="${1:-0}" 'BEGIN { printf "%.1f MiB", bytes / 1048576 }'
}

format_eta() {
  local seconds="${1:-0}"
  if ! [[ "$seconds" =~ ^[0-9]+$ ]]; then
    printf '%s\n' "--:--"
    return 0
  fi
  if [[ "$seconds" -ge 3600 ]]; then
    printf '%02d:%02d:%02d\n' $((seconds / 3600)) $(((seconds % 3600) / 60)) $((seconds % 60))
  else
    printf '%02d:%02d\n' $((seconds / 60)) $((seconds % 60))
  fi
}

parallel_downloaded_bytes() {
  local total=0
  local part
  for part in "$@"; do
    if [[ -f "$part" ]]; then
      total=$((total + $(file_size "$part")))
    fi
  done
  printf '%s\n' "$total"
}

parallel_part_count() {
  local count="$DOWNLOAD_PARALLEL_PARTS"
  if ! [[ "$count" =~ ^[0-9]+$ ]] || [[ "$count" -lt 2 ]]; then
    count=8
  elif [[ "$count" -gt 32 ]]; then
    count=32
  fi
  printf '%s\n' "$count"
}

download_file_parallel() {
  local url="$1"
  local partial="$2"
  local expected_size="${3:-0}"
  local part_count
  local chunk
  local part_dir="$partial.parts"
  local retry_all_errors_flag=""
  local pids=()
  local starts=()
  local ends=()
  local paths=()
  local index

  if ! [[ "$expected_size" =~ ^[0-9]+$ ]] || [[ "$expected_size" -lt 67108864 ]]; then
    return 2
  fi
  part_count="$(parallel_part_count)"
  if [[ "$expected_size" -lt $((part_count * 16777216)) ]]; then
    part_count=$(( (expected_size + 16777215) / 16777216 ))
    if [[ "$part_count" -lt 2 ]]; then
      return 2
    fi
  fi
  chunk=$(( (expected_size + part_count - 1) / part_count ))
  rm -rf "$part_dir"
  mkdir -p "$part_dir"
  if curl --help all 2>/dev/null | grep -q -- '--retry-all-errors'; then
    retry_all_errors_flag="--retry-all-errors"
  fi
  echo "Using parallel CDN download: ${part_count} parts, total ${expected_size} bytes."
  for index in $(seq 0 $((part_count - 1))); do
    local start=$((index * chunk))
    local end=$(( ((index + 1) * chunk) - 1 ))
    local part
    if [[ "$start" -ge "$expected_size" ]]; then
      continue
    fi
    if [[ "$end" -ge "$expected_size" ]]; then
      end=$((expected_size - 1))
    fi
    part="$(printf '%s/part-%03d' "$part_dir" "$index")"
    starts+=("$start")
    ends+=("$end")
    paths+=("$part")
    curl -fL --retry 8 --retry-delay 5 --retry-max-time 3600 --connect-timeout 45 ${retry_all_errors_flag:+$retry_all_errors_flag} --silent --show-error --range "${start}-${end}" "$url" -o "$part" &
    pids+=("$!")
  done

  local started_at
  local last_stamp
  local last_bytes=0
  started_at="$(date +%s)"
  last_stamp="$started_at"
  while true; do
    local active=0
    local pid
    for pid in "${pids[@]}"; do
      if kill -0 "$pid" 2>/dev/null; then
        active=1
        break
      fi
    done
    local now
    local downloaded
    local elapsed
    local delta_seconds
    local instant_speed
    local average_speed
    local speed
    local percent
    local eta_seconds
    now="$(date +%s)"
    downloaded="$(parallel_downloaded_bytes "${paths[@]}")"
    elapsed=$((now - started_at))
    if [[ "$elapsed" -lt 1 ]]; then elapsed=1; fi
    delta_seconds=$((now - last_stamp))
    if [[ "$delta_seconds" -lt 1 ]]; then delta_seconds=1; fi
    instant_speed=$(((downloaded - last_bytes) / delta_seconds))
    average_speed=$((downloaded / elapsed))
    speed="$instant_speed"
    if [[ "$speed" -le 0 ]]; then speed="$average_speed"; fi
    percent="$(awk -v done="$downloaded" -v total="$expected_size" 'BEGIN { if (total > 0) printf "%.1f", done * 100 / total; else printf "0.0" }')"
    if [[ "$speed" -gt 0 ]]; then
      eta_seconds=$(((expected_size - downloaded) / speed))
    else
      eta_seconds=0
    fi
    printf '\rCDN download progress: %s%%  %s / %s  %s/s  ETA %s' "$percent" "$(format_mib "$downloaded")" "$(format_mib "$expected_size")" "$(format_mib "$speed")" "$(format_eta "$eta_seconds")"
    last_stamp="$now"
    last_bytes="$downloaded"
    if [[ "$active" == "0" ]]; then
      printf '\n'
      break
    fi
    sleep 1
  done

  local failed=0
  local pid
  for pid in "${pids[@]}"; do
    if ! wait "$pid"; then
      failed=1
    fi
  done
  if [[ "$failed" != "0" ]]; then
    echo "Parallel CDN download did not complete; falling back to single-connection resume." >&2
    return 1
  fi
  for index in "${!paths[@]}"; do
    local expected_part_size=$((ends[index] - starts[index] + 1))
    if [[ ! -f "${paths[index]}" || "$(file_size "${paths[index]}")" -ne "$expected_part_size" ]]; then
      echo "Parallel CDN range was not honored; falling back to single-connection resume." >&2
      return 1
    fi
  done
  rm -f "$partial"
  for part in "${paths[@]}"; do
    cat "$part" >> "$partial"
  done
  return 0
}

download_file() {
  local url="$1"
  local destination="$2"
  local expected_sha="$3"
  local expected_size="${4:-0}"
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
  if download_file_parallel "$url" "$partial" "$expected_size"; then
    actual_sha="$(sha256_file "$partial")"
    if [[ "$actual_sha" == "$expected_sha" ]]; then
      mv "$partial" "$destination"
      echo "Download verified: $destination"
      return 0
    fi
    echo "Parallel CDN package SHA256 mismatch for $destination: $actual_sha" >&2
    rm -f "$partial"
  fi
  local max_attempts=5
  local retry_count=8
  local retry_max_time=3600
  for attempt in $(seq 1 "$max_attempts"); do
    local curl_args=(-fL --retry "$retry_count" --retry-delay 5 --retry-max-time "$retry_max_time" --connect-timeout 45 --progress-bar)
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
      if [[ "$attempt" == "$max_attempts" ]]; then
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
    if [[ "$attempt" == "$max_attempts" ]]; then
      echo "Download failed after $attempt attempts. You can rerun this installer to retry." >&2
      return "$status"
    fi
    sleep $((attempt * 3))
  done
  return 1
}

download_file_from_urls() {
  local destination="$1"
  local expected_sha="$2"
  local expected_size="${3:-0}"
  local url
  local attempted=0
  while IFS= read -r url; do
    if [[ -z "$url" ]]; then
      continue
    fi
    attempted=1
    echo "Using primary CDN download source: $url"
    if download_file "$url" "$destination" "$expected_sha" "$expected_size"; then
      return 0
    fi
    echo "Primary CDN download failed or checksum did not match: $url" >&2
    echo "Partial downloads are kept when possible; rerun this installer to resume." >&2
    exit 1
  done <<EOF
$DOWNLOAD_URLS_TEXT
EOF
  if [[ "$attempted" == "0" ]]; then
    echo "No download source was configured." >&2
  else
    echo "Primary CDN download failed. You can rerun this installer to retry." >&2
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
echo "EcoreX WebUI installer script: 0.3.0"
echo "EcoreX WebUI manifest version: $VERSION"
if [[ -n "$REQUESTED_VERSION" && "$REQUESTED_VERSION" != "$VERSION" ]]; then
  echo "Manifest version '$VERSION' does not match requested '$REQUESTED_VERSION'." >&2
  exit 1
fi

ARTIFACT_HREF=""
ARTIFACT_NAME=""
ARTIFACT_SHA=""
ARTIFACT_SIZE=""
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
  ARTIFACT_SIZE="$(manifest_value "artifacts.$index.size")"
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

download_file_from_urls "$ZIP_PATH" "$ARTIFACT_SHA" "$ARTIFACT_SIZE"

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
echo "If the browser did not open, double-click the desktop EcoreX WebUI shortcut; it will start the local service and reopen the browser."
