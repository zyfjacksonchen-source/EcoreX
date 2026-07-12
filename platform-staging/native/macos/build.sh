#!/bin/sh
set -eu

if [ "$#" -ne 2 ]; then
  echo 'usage: build.sh <arm64|x64> <output-directory>' >&2
  exit 64
fi
case "$1" in
  arm64) arch=arm64 ;;
  x64) arch=x86_64 ;;
  *) exit 64 ;;
esac
output=$2
root=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
mkdir -p "$output"
export SOURCE_DATE_EPOCH=${SOURCE_DATE_EPOCH:-0}
/usr/bin/clang -arch "$arch" -std=c17 -Os -Wall -Wextra -Werror \
  -fstack-protector-strong -Wl,-dead_strip -Wl,-no_uuid \
  "$root/ecorex_launcher.c" -o "$output/ecorex"
chmod 0755 "$output/ecorex"
