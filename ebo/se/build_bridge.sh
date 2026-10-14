#!/usr/bin/env bash
# Build the EBO SE native TUTK bridge from se/ebo_bridge.c.
#
# The bridge dlopen()s Android (bionic) TUTK libraries, so it is itself an Android binary run via
# the bundled bionic linker (see docs/EBO-SE.md). Build the bitness that matches your TUTK .so files
# and bionic runtime:
#   ARCH=arm64  -> aarch64  (Raspberry Pi 5, 64-bit kernel — the default, preferred)
#   ARCH=arm    -> armv7a   (older 32-bit setups / upstream lilium360 default)
#
# Requires the Android NDK. Set NDK=/path/to/android-ndk (or have $ANDROID_NDK_HOME set).
#   NDK=~/Library/Android/sdk/ndk/25.1.8937393 ./se/build_bridge.sh
#
# Output: se/ebo_bridge  (copy it into your se_dir, e.g. /share/ebo-se/ebo_bridge)
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
: "${NDK:=${ANDROID_NDK_HOME:-}}"
: "${NDK:?Set NDK=/path/to/android-ndk (or ANDROID_NDK_HOME)}"
: "${API:=24}"
: "${ARCH:=arm64}"

case "$ARCH" in
  arm64|aarch64) TARGET="aarch64-linux-android${API}" ;;
  arm|armv7|armv7a) TARGET="armv7a-linux-androideabi${API}" ;;
  *) echo "unknown ARCH=$ARCH (use arm64 or arm)"; exit 1 ;;
esac

HOST="$(uname | tr '[:upper:]' '[:lower:]')-x86_64"
CLANG="$NDK/toolchains/llvm/prebuilt/$HOST/bin/clang"
[ -x "$CLANG" ] || { echo "clang not found at $CLANG — check NDK path / host tag"; exit 1; }

"$CLANG" --target="$TARGET" -O2 "$HERE/ebo_bridge.c" -o "$HERE/ebo_bridge" -ldl
echo "built $HERE/ebo_bridge"
file "$HERE/ebo_bridge" 2>/dev/null || true
echo "Next: copy it into your se_dir (e.g. /share/ebo-se/ebo_bridge) alongside lib/ bionic/ ioctl9930.bin"
