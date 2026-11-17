#!/usr/bin/env bash
# Assemble a Raspberry Pi SD image from an arm64 rootfs directory WITHOUT loop devices:
#   p1 FAT32 "bootfs"   -> /boot/firmware (firmware, kernels, config.txt, cmdline.txt)
#   p2 ext4  "rootfs"   -> /
#   p3 ext4  VICTORDATA -> VictorLinux data partition (models, settings, owner secrets); grown on first boot
# Usage: mkimage.sh ROOTFS_DIR OUT.img BOOT_MIB DATA_MIB
set -Eeuo pipefail
ROOTFS="${1:?rootfs dir}"; IMG="${2:?output image}"; BOOT_MIB="${3:-512}"; DATA_MIB="${4:-256}"
[ -d "$ROOTFS/boot/firmware" ] || { echo "mkimage: $ROOTFS/boot/firmware missing" >&2; exit 1; }
ls "$ROOTFS"/boot/firmware/kernel*.img >/dev/null 2>&1 || { echo "mkimage: no kernel*.img in boot/firmware (raspi-firmware did not install a kernel)" >&2; exit 1; }
work="$(mktemp -d)"; trap 'rm -rf "$work"' EXIT

# Disk identifier -> PARTUUIDs <id>-01/-02/-03 referenced by cmdline.txt and fstab.
DISKID="$(od -An -N4 -tx4 /dev/urandom | tr -d ' \n')"
[ "${#DISKID}" -eq 8 ] || { echo "mkimage: bad disk id" >&2; exit 1; }
cat > "$ROOTFS/etc/fstab" <<FSTAB
# VictorLinux (Raspberry Pi edition). The VICTORDATA partition is mounted by victor-storage.service, not here.
PARTUUID=${DISKID}-01  /boot/firmware  vfat  defaults,noatime  0  2
PARTUUID=${DISKID}-02  /               ext4  defaults,noatime  0  1
FSTAB
cmd="$ROOTFS/boot/firmware/cmdline.txt"
[ -f "$cmd" ] || { echo "mkimage: cmdline.txt missing from overlay" >&2; exit 1; }
sed -i "s/@ROOT_PARTUUID@/${DISKID}-02/" "$cmd"
grep -q "root=PARTUUID=${DISKID}-02" "$cmd" || { echo "mkimage: cmdline.txt has no root= placeholder" >&2; exit 1; }

# ---- boot partition (FAT32) from boot/firmware, then empty the mountpoint in the rootfs
boot="$work/boot.img"
truncate -s "${BOOT_MIB}M" "$boot"
mkfs.vfat -F 32 -n bootfs "$boot" >/dev/null
( cd "$ROOTFS/boot/firmware" && MTOOLS_SKIP_CHECK=1 mcopy -s -p -m -i "$boot" ./* ::/ )
find "$ROOTFS/boot/firmware" -mindepth 1 -delete

# ---- root partition (ext4), size = content + 30% + 768 MiB headroom (apt, logs, home)
used_mib=$(( $(du -sxm "$ROOTFS" | awk '{print $1}') ))
ROOT_MIB=$(( used_mib + used_mib * 30 / 100 + 768 ))
root="$work/root.img"
truncate -s "${ROOT_MIB}M" "$root"
mke2fs -q -t ext4 -L rootfs -d "$ROOTFS" -E root_owner=0:0 "$root"

# ---- data partition (ext4, label VICTORDATA) with the layout the shared tools expect
data="$work/data.img"
truncate -s "${DATA_MIB}M" "$data"
# empty filesystem owned by the owner (uid 1000); victor-priv storage-auto creates the standard layout on first mount
mke2fs -q -t ext4 -L VICTORDATA -E root_owner=1000:1000 "$data"

# ---- partition table (MBR: every Pi bootloader reads it) and placement, 4 MiB aligned
START=8
total=$(( START + BOOT_MIB + ROOT_MIB + DATA_MIB + 4 ))
rm -f "$IMG"; truncate -s "${total}M" "$IMG"
sfdisk --quiet "$IMG" <<PT
label: dos
label-id: 0x${DISKID}
unit: sectors
start=$(( START * 2048 )), size=$(( BOOT_MIB * 2048 )), type=c, bootable
start=$(( (START + BOOT_MIB) * 2048 )), size=$(( ROOT_MIB * 2048 )), type=83
start=$(( (START + BOOT_MIB + ROOT_MIB) * 2048 )), size=$(( DATA_MIB * 2048 )), type=83
PT
dd if="$boot" of="$IMG" bs=1M seek="$START" conv=notrunc,sparse status=none
dd if="$root" of="$IMG" bs=1M seek="$(( START + BOOT_MIB ))" conv=notrunc,sparse status=none
dd if="$data" of="$IMG" bs=1M seek="$(( START + BOOT_MIB + ROOT_MIB ))" conv=notrunc,sparse status=none
sfdisk -d "$IMG"
echo "mkimage: $IMG (${total} MiB; boot ${BOOT_MIB}, root ${ROOT_MIB}, data ${DATA_MIB}; disk id ${DISKID})"
