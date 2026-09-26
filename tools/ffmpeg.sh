#!/usr/bin/env bash
# Builds the LGPL FFmpeg that the macOS runtime uses in place of the one inside OpenCV's wheel.
#
#   tools/ffmpeg.sh <out-dir>        (version, URL and sha256 come from runtime.json)
#
# Why this exists: opencv-python-headless's macOS arm64 wheel bundles Homebrew's FFmpeg, which is
# configured with --enable-gpl --enable-version3 (x264, x265, rubberband, ...) and reports itself as
# "GPL version 3 or later". Shipping that in the runtime would put GPL code inside a closed-source
# product. OpenCV loads FFmpeg as ordinary shared libraries, so an LGPL build with the same major
# versions takes their place - the same replacement the LGPL guarantees every user - and the GPL
# libraries it brought along are then unreferenced and left out (tools/build.py does the swap).
#
# The configuration is the default LGPL-2.1-or-later one: no --enable-gpl, --enable-version3 or
# --enable-nonfree, and --disable-autodetect so nothing from the build machine (Homebrew in
# particular) is linked in by accident. Only macOS system libraries and frameworks are used.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$(mkdir -p "$1" && cd "$1" && pwd)"
read -r VERSION URL SHA < <(python3 -c 'import json,sys; f=json.load(open(sys.argv[1]))["ffmpeg"]; print(f["version"], f["url"], f["sha256"])' "$ROOT/runtime.json")
export MACOSX_DEPLOYMENT_TARGET="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["platforms"]["darwin-arm64"]["min_os"])' "$ROOT/runtime.json")"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
echo "FFmpeg $VERSION -> $OUT (macOS $MACOSX_DEPLOYMENT_TARGET+)"
# dav1d (VideoLAN, BSD-2-Clause), the AV1 decoder: FFmpeg's own AV1 decoder only drives hardware decoders, which
# OpenCV does not ask for, so without it every AV1 file fails to decode. Built static from its pinned source with meson,
# and linked into libavcodec, so the runtime gains no library of its own.
read -r D_VERSION D_URL D_SHA MESON NINJA < <(python3 -c 'import json,sys; d=json.load(open(sys.argv[1]))["dav1d"]; print(d["version"], d["url"], d["sha256"], d["build_tools"]["meson"], d["build_tools"]["ninja"])' "$ROOT/runtime.json")
echo "dav1d $D_VERSION (static)"
curl -fsSL --retry 3 -o "$WORK/dav1d.tar.xz" "$D_URL"
echo "$D_SHA  $WORK/dav1d.tar.xz" | shasum -a 256 -c - >/dev/null || { echo "ERROR: dav1d source does not match runtime.json sha256" >&2; exit 1; }
tar -xJf "$WORK/dav1d.tar.xz" -C "$WORK"
python3 -m venv "$WORK/buildtools" && "$WORK/buildtools/bin/pip" install -q "meson==$MESON" "ninja==$NINJA"
( export PATH="$WORK/buildtools/bin:$PATH"
  cd "$WORK/dav1d-$D_VERSION"
  meson setup build --prefix="$WORK/dav1d" --libdir=lib --buildtype=release --default-library=static \
    -Denable_tools=false -Denable_tests=false -Denable_examples=false \
    -Dc_args="-mmacosx-version-min=$MACOSX_DEPLOYMENT_TARGET" -Dc_link_args="-mmacosx-version-min=$MACOSX_DEPLOYMENT_TARGET" > "$WORK/dav1d.log" 2>&1 \
    && ninja -C build install >> "$WORK/dav1d.log" 2>&1 ) || { tail -40 "$WORK/dav1d.log"; exit 1; }
export PKG_CONFIG_LIBDIR="$WORK/dav1d/lib/pkgconfig"   # only ours: never a dav1d the build machine happens to have

curl -fsSL --retry 3 -o "$WORK/src.tar.xz" "$URL"
echo "$SHA  $WORK/src.tar.xz" | shasum -a 256 -c - >/dev/null || { echo "ERROR: FFmpeg source does not match runtime.json sha256" >&2; exit 1; }
tar -xJf "$WORK/src.tar.xz" -C "$WORK"
cd "$WORK/ffmpeg-$VERSION"

FLAGS=(
  --prefix="$OUT"
  --install-name-dir=@loader_path           # the libraries find each other next to themselves
  --enable-shared --disable-static
  --disable-programs --disable-doc --disable-debug
  --disable-autodetect                      # nothing from the build machine unless named below
  --enable-zlib --enable-bzlib             # (no iconv: only subtitle charset conversion uses it)
  --enable-libdav1d --pkg-config-flags=--static   # AV1, from the static dav1d above
  --enable-videotoolbox --enable-audiotoolbox
  --disable-network
  --disable-protocols --enable-protocol=file,pipe
  --disable-indevs --disable-outdevs        # libavdevice is linked by OpenCV but never used to capture
  --disable-avfilter --disable-swresample
  --disable-encoders                        # decoding is the job; a few encoders for cv2.VideoWriter
  --enable-encoder=mpeg4,mjpeg,png,rawvideo,ffv1,h264_videotoolbox,hevc_videotoolbox,prores_videotoolbox
  --extra-cflags="-mmacosx-version-min=$MACOSX_DEPLOYMENT_TARGET"
  --extra-ldflags="-mmacosx-version-min=$MACOSX_DEPLOYMENT_TARGET"
)
./configure "${FLAGS[@]}" > "$WORK/configure.log" 2>&1 || { tail -40 "$WORK/configure.log"; exit 1; }
grep -q "^License: LGPL version 2.1 or later" "$WORK/configure.log" \
  || { echo "ERROR: configure did not produce an LGPL-2.1-or-later build" >&2; grep "^License" "$WORK/configure.log" >&2; exit 1; }
make -j"$(sysctl -n hw.ncpu)" > "$WORK/make.log" 2>&1 || { tail -40 "$WORK/make.log"; exit 1; }
make install > /dev/null

mkdir -p "$OUT/licenses"
cp COPYING.LGPLv2.1 LICENSE.md "$OUT/licenses/"
cp "$WORK/dav1d-$D_VERSION/COPYING" "$OUT/licenses/dav1d-COPYING"
{
  echo "FFmpeg $VERSION, built by keypose-runtime tools/ffmpeg.sh"
  echo "Source:  $URL"
  echo "sha256:  $SHA (signed by the FFmpeg release key $(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["ffmpeg"]["signed_by"])' "$ROOT/runtime.json"))"
  echo "Licence: $(grep '^License:' "$WORK/configure.log" | sed 's/^License: //')"
  echo "macOS:   $MACOSX_DEPLOYMENT_TARGET or later, $(uname -m)"
  echo "AV1:     dav1d $D_VERSION (BSD-2-Clause), $D_URL, sha256 $D_SHA, linked statically"
  echo "Configure flags:"
  printf '  %s\n' "${FLAGS[@]}" | sed "s|$OUT|<prefix>|"
} > "$OUT/BUILD.txt"
cat "$OUT/BUILD.txt"
ls -l "$OUT/lib"/*.dylib
