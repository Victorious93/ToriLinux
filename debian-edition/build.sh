#!/usr/bin/env bash
# VictorLinux (Debian edition) local build entry point.
# Usage: sudo ./build.sh [--profile amd|nvidia] [--workdir DIR] [--out DIR] [--clean]
# Requires root, Debian live-build (>= 20250505) and ~20 GiB free disk, 8 GiB RAM recommended, internet (BUILD time only).
set -Eeuo pipefail
trap 'echo "build.sh: FAILED at line $LINENO (exit $?)" >&2' ERR

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROFILE=amd; WORKDIR=/var/tmp/victorlinux-build; OUT="$HERE/../out/debian"; CLEAN=0
while [ $# -gt 0 ]; do
  case "$1" in
    --profile) PROFILE="$2"; shift 2;;
    --workdir) WORKDIR="$2"; shift 2;;
    --out) OUT="$2"; shift 2;;
    --clean) CLEAN=1; shift;;
    -h|--help) sed -n '2,5p' "${BASH_SOURCE[0]}"; exit 0;;
    *) echo "unknown option: $1" >&2; exit 2;;
  esac
done
case "$PROFILE" in amd|nvidia) ;; *) echo "profile must be amd or nvidia" >&2; exit 2;; esac
[ "$(id -u)" -eq 0 ] || { echo "must run as root (live-build uses chroot/mount)" >&2; exit 1; }

# shellcheck source=versions.env
set -a; . "$HERE/versions.env"; set +a
for t in lb debootstrap xorriso mksquashfs git sha256sum; do
  command -v "$t" >/dev/null || { echo "missing prerequisite: $t (apt install live-build debootstrap xorriso squashfs-tools git)" >&2; exit 1; }
done
have="$(dpkg-query -W -f='${Version}' live-build 2>/dev/null || true)"
echo "live-build $have (pinned reference: $LIVE_BUILD_VERSION)"
avail_kb=$(df -Pk "$(dirname "$WORKDIR")" | awk 'NR==2{print $4}')
[ "$avail_kb" -gt $((18*1024*1024)) ] || { echo "need >= 18 GiB free in $(dirname "$WORKDIR")" >&2; exit 1; }

[ "$CLEAN" -eq 1 ] && rm -rf "$WORKDIR"
mkdir -p "$WORKDIR" "$OUT"; OUT="$(cd "$OUT" && pwd)"
cd "$WORKDIR"
[ -d config ] && lb clean --purge >/dev/null 2>&1 || true
rm -rf config auto

lb config noauto \
  --mode debian --distribution "$DEBIAN_SUITE" --architectures amd64 \
  --archive-areas "main contrib non-free non-free-firmware" \
  --parent-archive-areas "main contrib non-free non-free-firmware" \
  --mirror-bootstrap "$DEBIAN_MIRROR" --mirror-chroot "$DEBIAN_MIRROR" --mirror-binary "$DEBIAN_MIRROR" \
  --mirror-chroot-security "$DEBIAN_SECURITY_MIRROR" --mirror-binary-security "$DEBIAN_SECURITY_MIRROR" \
  --security true --updates true \
  --binary-images iso-hybrid --bootloaders "grub-efi,syslinux" --firmware-chroot false \
  --debian-installer none --iso-volume "VICTORLINUX" --iso-application "VictorLinux" \
  --iso-publisher "Victorious / Victorious Framework" \
  --image-name "$IMAGE_NAME" --apt-indices false --apt-recommends false \
  --win32-loader false --memtest none --checksums none \
  --linux-flavours amd64 --compression xz \
  --bootappend-live "boot=live components noroot username=victor hostname=victorlinux locales=en_US.UTF-8 keyboard-layouts=us timezone=UTC nopersistence quiet splash"

# static config tree
cp -a "$HERE/config/package-lists/." config/package-lists/
cp -a "$HERE/config/hooks/live/." config/hooks/live/
mkdir -p config/includes.chroot_after_packages
cp -a "$HERE/config/includes.chroot_after_packages/." config/includes.chroot_after_packages/
if [ "$PROFILE" = nvidia ]; then
  cp config/package-lists/90-nvidia.list.chroot.nvidia config/package-lists/90-nvidia.list.chroot
fi
rm -f config/package-lists/*.nvidia
# pins visible inside the chroot hooks
install -d config/includes.chroot_after_packages/usr/share/victorlinux
cp "$HERE/versions.env" config/includes.chroot_after_packages/usr/share/victorlinux/versions.env
echo "$PROFILE" > config/includes.chroot_after_packages/usr/share/victorlinux/profile
# generated-file hygiene: executable bits + syntax checks before spending an hour building
"$HERE/scripts/check-sources.sh" config

log="$OUT/build-$(date -u +%Y%m%dT%H%M%SZ).log"
echo "build log: $log"
set +e; lb build 2>&1 | tee "$log"; rc=${PIPESTATUS[0]}; set -e
[ "$rc" -eq 0 ] || { echo "lb build failed (rc=$rc); see $log" >&2; exit "$rc"; }

iso="$(ls -1 "$WORKDIR"/*.iso | head -1)"
[ -f "$iso" ] || { echo "no ISO produced" >&2; exit 1; }
final="$OUT/${IMAGE_NAME}-debian-${DEBIAN_SUITE}-${PROFILE}-amd64-$(date -u +%Y%m%d).iso"
cp -f "$iso" "$final"
( cd "$OUT" && sha256sum "$(basename "$final")" > "$(basename "$final").sha256" )
"$HERE/scripts/gen-manifest.sh" "$WORKDIR" "$final" "$PROFILE" "$log" > "$final.manifest.json"
echo "ISO:      $final"; cat "$final.sha256"; echo "MANIFEST: $final.manifest.json"
