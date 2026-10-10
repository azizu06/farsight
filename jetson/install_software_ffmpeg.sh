#!/usr/bin/env bash
# Opt-in Ubuntu software FFmpeg for Jetson R39/NVIDIA builds without libx264.
# Extracts packages privately; never dpkg-installs or replaces NVIDIA libraries.
# Usage: bash jetson/install_software_ffmpeg.sh [--dry-run] [--prefix PATH] [--wrapper PATH]
#        [--package-dir PATH] [--ffmpeg-version 7:6.1.1-3ubuntu5] [--install-service-path]
set -euo pipefail

PREFIX="${FARSIGHT_FFMPEG_PREFIX:-$HOME/farsight-ffmpeg}"
WRAPPER="${FARSIGHT_FFMPEG_WRAPPER:-$HOME/.local/bin/ffmpeg}"
VERSION=7:6.1.1-3ubuntu5
PACKAGE_DIR=""
DRY=0 SERVICE_PATH=0
PACKAGES=(ffmpeg libavcodec60 libavdevice60 libavfilter9 libavformat60 libavutil58 libswresample4 libswscale7 libpostproc57 libx264-164)
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY=1 ;;
    --install-service-path) SERVICE_PATH=1 ;;
    --prefix|--wrapper|--package-dir|--ffmpeg-version)
      [ $# -ge 2 ] || { echo "$1 needs a value" >&2; exit 2; }
      case "$1" in
        --prefix) PREFIX="$2" ;;
        --wrapper) WRAPPER="$2" ;;
        --package-dir) PACKAGE_DIR="$2" ;;
        --ffmpeg-version) VERSION="$2" ;;
      esac
      shift ;;
    -h|--help) sed -n '2,5p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done
case "$VERSION" in 7:6.1.*-*) ;; *) echo "Use an Ubuntu FFmpeg 6.1 package version (libavcodec60 ABI)" >&2; exit 2 ;; esac
[ ! -L "$PREFIX" ] || { echo "Refusing a symlink prefix: $PREFIX" >&2; exit 2; }
PREFIX="$(python3 -c 'from pathlib import Path;import sys;print(Path(sys.argv[1]).expanduser().resolve())' "$PREFIX")"
WRAPPER="$(python3 -c 'from pathlib import Path;import sys;print(Path(sys.argv[1]).expanduser().absolute())' "$WRAPPER")"
for path in "$PREFIX" "$WRAPPER"; do
  case "$path" in /|/usr|/usr/*|/lib|/lib/*|/lib64|/lib64/*|/etc|/etc/*|/bin|/bin/*|/sbin|/sbin/*)
    echo "Refusing a system-library/binary path: $path" >&2; exit 2 ;;
  esac
  if printf '%s' "$path" | LC_ALL=C grep -q '[[:cntrl:]]'; then echo "Paths must not contain control characters" >&2; exit 2; fi
done
[ ! -L "$WRAPPER" ] || { echo "Refusing to replace a symlink wrapper: $WRAPPER" >&2; exit 2; }

if [ "$DRY" = 1 ]; then
  echo "[dry-run] target: Linux aarch64/arm64; private prefix: $PREFIX"
  echo "[dry-run] reuse only after libx264 listing and a real 640x480 encode pass"
  if [ -n "$PACKAGE_DIR" ]; then echo "[dry-run] extract the required arm64 .deb packages from $PACKAGE_DIR"
  else echo "[dry-run] apt-get download ffmpeg=$VERSION ${PACKAGES[*]:1}"; fi
  echo "[dry-run] dpkg-deb -x each checked package into a temporary private prefix (no package installation)"
  echo "[dry-run] verify candidate encoder, publish $PREFIX and wrapper $WRAPPER, then verify again"
  if [ "$SERVICE_PATH" = 1 ]; then
    echo "[dry-run] install only /etc/systemd/system/farsight.service.d/software-ffmpeg.conf PATH drop-in"
    echo "[dry-run] sudo systemctl daemon-reload (no automatic restart)"
  fi
  exit 0
fi
[ "$(uname -s)" = Linux ] && [ "$(uname -m)" = aarch64 ] && [ "$(dpkg --print-architecture)" = arm64 ] \
  || { echo "Run this on Linux aarch64/arm64 (or use --dry-run)" >&2; exit 1; }
for tool in dpkg-deb mktemp timeout; do command -v "$tool" >/dev/null || { echo "Required tool missing: $tool" >&2; exit 1; }; done
[ -n "$PACKAGE_DIR" ] || command -v apt-get >/dev/null || { echo "apt-get is required unless --package-dir is provided" >&2; exit 1; }
if [ -e "$PREFIX" ] && [ ! -x "$PREFIX/usr/bin/ffmpeg" ]; then
  echo "Existing prefix is not an extracted FFmpeg tree; refusing to replace it: $PREFIX" >&2; exit 1
fi

mkdir -p "$(dirname "$PREFIX")" "$(dirname "$WRAPPER")"
STAGE="$(mktemp -d "$(dirname "$PREFIX")/.farsight-ffmpeg.XXXXXX")"
BACKUP=""
PUBLISHED=0
COMPLETE=0
wrapper_temporary=""
cleanup() {
  if [ "$COMPLETE" = 0 ] && [ "$PUBLISHED" = 1 ]; then
    mv "$PREFIX" "$STAGE/failed-prefix" 2>/dev/null || true
    [ -z "$BACKUP" ] || mv "$BACKUP" "$PREFIX" 2>/dev/null || true
  fi
  [ -z "$wrapper_temporary" ] || rm -f "$wrapper_temporary"
  rm -rf "$STAGE"
}
trap cleanup EXIT
make_wrapper() {
  local destination="$1" prefix="$2"
  {
    printf '#!/bin/sh\n# Farsight private Ubuntu FFmpeg; NVIDIA system libraries stay unchanged.\n'
    printf 'farsight_ffmpeg_lib=%q\n' "$prefix/usr/lib/aarch64-linux-gnu"
    printf 'export LD_LIBRARY_PATH="$farsight_ffmpeg_lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"\n'
    printf 'exec %q "$@"\n' "$prefix/usr/bin/ffmpeg"
  } > "$destination"
  chmod 755 "$destination"
}
verify_encoder() {
  local binary="$1"
  timeout --kill-after=2s 15s "$binary" -hide_banner -encoders > "$STAGE/encoders.txt" 2> "$STAGE/encoder-error.txt" || return 1
  grep -Eq '[[:space:]]libx264[[:space:]]' "$STAGE/encoders.txt" || return 1
  rm -f "$STAGE/encoder-smoke.mp4"
  timeout --kill-after=2s 20s "$binary" -nostdin -hide_banner -loglevel error -y -f lavfi -i color=c=red:s=640x480:r=10 \
    -frames:v 3 -an -c:v libx264 -preset ultrafast -threads 2 -pix_fmt yuv420p \
    "$STAGE/encoder-smoke.mp4" > "$STAGE/smoke-output.txt" 2> "$STAGE/smoke-error.txt" || return 1
  [ -s "$STAGE/encoder-smoke.mp4" ]
}

make_wrapper "$STAGE/check-existing" "$PREFIX"
if [ -x "$PREFIX/usr/bin/ffmpeg" ] && verify_encoder "$STAGE/check-existing"; then
  echo "Existing private FFmpeg passed libx264 and encode checks; reusing it."
else
  mkdir -p "$STAGE/packages" "$STAGE/payload"
  if [ -n "$PACKAGE_DIR" ]; then
    [ -d "$PACKAGE_DIR" ] || { echo "Package directory does not exist: $PACKAGE_DIR" >&2; exit 1; }
    cp "$PACKAGE_DIR"/*.deb "$STAGE/packages/" || { echo "No .deb packages found in $PACKAGE_DIR" >&2; exit 1; }
  else
    (cd "$STAGE/packages" && apt-get download "ffmpeg=$VERSION" "${PACKAGES[@]:1}") \
      || { echo "Ubuntu FFmpeg download failed. Check apt sources/proxy or provide a complete --package-dir; no system libraries were replaced." >&2; exit 1; }
  fi
  found=" "
  for deb in "$STAGE/packages/"*.deb; do
    name="$(dpkg-deb --field "$deb" Package)"
    architecture="$(dpkg-deb --field "$deb" Architecture)"
    case "$architecture" in arm64|all) ;; *) echo "Wrong package architecture: $deb ($architecture)" >&2; exit 1 ;; esac
    allowed=0
    for package in "${PACKAGES[@]}"; do [ "$name" != "$package" ] || allowed=1; done
    [ "$allowed" = 1 ] || { echo "Unexpected package in offline directory: $name" >&2; exit 1; }
    case "$found" in *" $name "*) echo "Duplicate package: $name" >&2; exit 1 ;; esac
    found="$found$name "
    if [ "$name" = ffmpeg ]; then
      actual_version="$(dpkg-deb --field "$deb" Version)"
      [ "$actual_version" = "$VERSION" ] || { echo "Expected ffmpeg $VERSION; found $actual_version" >&2; exit 1; }
    fi
    dpkg-deb -x "$deb" "$STAGE/payload"
  done
  for package in "${PACKAGES[@]}"; do
    case "$found" in *" $package "*) ;; *) echo "Required extracted package missing: $package" >&2; exit 1 ;; esac
  done
  make_wrapper "$STAGE/check-candidate" "$STAGE/payload"
  if ! verify_encoder "$STAGE/check-candidate"; then
    cat "$STAGE/encoder-error.txt" "$STAGE/smoke-error.txt" 2>/dev/null || true
    echo "Private candidate failed libx264/encode verification; existing prefix and wrapper remain unchanged." >&2
    exit 1
  fi
  if [ -e "$PREFIX" ]; then BACKUP="$PREFIX.backup.$(date +%s).$$"; mv "$PREFIX" "$BACKUP"; fi
  PUBLISHED=1
  mv "$STAGE/payload" "$PREFIX"
fi
make_wrapper "$STAGE/final-wrapper" "$PREFIX"
verify_encoder "$STAGE/final-wrapper" || { echo "Published prefix failed final encode verification; rolling back." >&2; exit 1; }
wrapper_temporary="$(mktemp "$(dirname "$WRAPPER")/.farsight-ffmpeg-wrapper.XXXXXX")"
cp "$STAGE/final-wrapper" "$wrapper_temporary"
chmod 755 "$wrapper_temporary"
mv "$wrapper_temporary" "$WRAPPER"
wrapper_temporary=""
COMPLETE=1

if [ "$SERVICE_PATH" = 1 ]; then
  service_path="$(dirname "$WRAPPER"):/usr/local/cuda/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
  service_path="${service_path//\\/\\\\}"
  service_path="${service_path//\"/\\\"}"
  service_path="${service_path//%/%%}"
  printf '[Service]\nEnvironment="PATH=%s"\n' "$service_path" > "$STAGE/software-ffmpeg.conf"
  sudo mkdir -p /etc/systemd/system/farsight.service.d
  sudo install -m 644 "$STAGE/software-ffmpeg.conf" /etc/systemd/system/farsight.service.d/software-ffmpeg.conf
  sudo systemctl daemon-reload
  echo "Service PATH drop-in installed. Restart farsight explicitly when ready; no service was restarted here."
fi
echo "Software libx264 encoder verified: $WRAPPER"
echo "NVIDIA system packages/libraries were not replaced. Camera/Quest playback and loaded throughput still need their separate checks."
[ -z "$BACKUP" ] || echo "Previous private prefix preserved at $BACKUP"
