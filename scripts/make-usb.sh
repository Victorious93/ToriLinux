#!/usr/bin/env bash
# Prepare a USB drive: Ventoy + the built ISO + a persistent ext4 data partition labelled TORIDATA.
#
#   scripts/make-usb.sh list
#   scripts/make-usb.sh plan  --iso out/X.iso --device /dev/sdX
#   scripts/make-usb.sh write --iso out/X.iso --device /dev/sdX --confirm-serial SERIAL [--ventoy DIR]
#
# SAFETY: the target is never chosen by capacity. `write` needs the exact device path AND its serial
# number, re-reads the device identity immediately before writing, refuses the system disk, the
# workspace disk, mounted devices and (by default) non-USB transports. It ERASES the whole device.
set -euo pipefail

VENTOY_VERSION=${VENTOY_VERSION:-1.1.18}
VENTOY_SHA256=${VENTOY_SHA256:-d86ff9de63d94c8b8f1f6b14d23c55af4b88e8207bf2893890892a6a97b1ad54}
EFI_MIB=32          # Ventoy's EFI partition
HEADROOM_MIB=1024   # unallocated space deliberately left at the end of the drive
MIN_DATA_GIB=8
repo=$(cd "$(dirname "$0")/.." && pwd)

die() { echo "ERROR: $*" >&2; exit 1; }
need() { command -v "$1" >/dev/null || die "missing tool: $1"; }

# --- identity & safety ------------------------------------------------------------------
dev_field() {
  local v; v=$(lsblk -dno "$2" "$1" 2>/dev/null | head -n1 | sed 's/^ *//; s/ *$//')
  # test mode only: loop images have no serial, so use a fixed one
  if [[ -z "$v" && "$2" == SERIAL && -n "${TORI_USB_TEST_LOOP:-}" ]]; then v=LOOPTEST; fi
  echo "$v"
}
dev_bytes() { blockdev --getsize64 "$1"; }

backing_disks() {   # whole-disk devices underlying a path (resolves partitions / LVM / dm)
  local src; src=$(findmnt -n -o SOURCE --target "$1" 2>/dev/null || true)
  [[ "$src" == /dev/* ]] || return 0
  lsblk -nsro NAME,TYPE "$src" 2>/dev/null | awk '$2=="disk"||$2=="loop"{print "/dev/"$1}'
}

check_target() {
  local d=$1
  [[ -b "$d" ]] || die "$d is not a block device"
  [[ "$(dev_field "$d" TYPE)" == "disk" || ( -n "${TORI_USB_TEST_LOOP:-}" && "$(dev_field "$d" TYPE)" == "loop" ) ]] \
    || die "$d is not a whole disk (give the disk, e.g. /dev/sdb, not a partition)"
  local real; real=$(readlink -f "$d")
  local p
  for p in / "$repo" /boot /home /var/tmp /tmp; do
    while read -r s; do
      [[ -n "$s" && "$(readlink -f "$s")" == "$real" ]] && die "$d backs $p (system or build workspace) - refusing"
    done < <(backing_disks "$p")
  done
  if lsblk -nro MOUNTPOINTS "$d" | grep -q '[^[:space:]]'; then
    die "$d (or a partition) is mounted - unmount it first: $(lsblk -nro NAME,MOUNTPOINTS "$d" | tr '\n' ';')"
  fi
  if [[ -z "${TORI_USB_TEST_LOOP:-}" ]]; then
    [[ "$(dev_field "$d" TRAN)" == "usb" ]] || die "$d transport is '$(dev_field "$d" TRAN)', not usb (internal disk?) - refusing"
  fi
}

cmd_list() {
  echo "Candidate removable/USB disks (system and workspace disks are excluded):"
  printf '%-12s %-10s %-24s %-20s %-6s %-8s %s\n' DEVICE SIZE MODEL SERIAL TRAN REMOV MOUNTS
  local d
  for d in $(lsblk -dnpo NAME,TYPE | awk '$2=="disk"{print $1}'); do
    if ( check_target "$d" ) >/dev/null 2>&1 || [[ "$(dev_field "$d" TRAN)" == "usb" || "$(dev_field "$d" RM)" == "1" ]]; then
      local why=""
      ( check_target "$d" ) >/dev/null 2>&1 || why="  <-- NOT usable: $( ( check_target "$d" ) 2>&1 | head -n1 | sed 's/ERROR: //')"
      printf '%-12s %-10s %-24s %-20s %-6s %-8s %s%s\n' "$d" "$(dev_field "$d" SIZE)" "$(dev_field "$d" MODEL)" \
        "$(dev_field "$d" SERIAL)" "$(dev_field "$d" TRAN)" "$(dev_field "$d" RM)" \
        "$(lsblk -nro MOUNTPOINTS "$d" | tr '\n' ' ')" "$why"
    fi
  done
}

# --- layout -------------------------------------------------------------------------------
layout() {   # sets p1_mib data_mib reserve_mib; args: iso device
  local iso_b disk_b
  iso_b=$(stat -c %s "$1"); disk_b=$(dev_bytes "$2")
  local iso_mib=$(( (iso_b + 1048575) / 1048576 ))
  # Ventoy data partition (holds ISOs): at least 8 GiB, or ~2x the ISO, whichever is larger. Room for a rebuilt ISO.
  p1_mib=$(( iso_mib * 2 )); (( p1_mib < 8192 )) && p1_mib=8192
  p1_mib=$(( (p1_mib + 1023) / 1024 * 1024 ))
  local disk_mib=$(( disk_b / 1048576 ))
  data_mib=$(( disk_mib - 1 - p1_mib - EFI_MIB - HEADROOM_MIB ))
  data_mib=$(( data_mib / 1024 * 1024 ))
  reserve_mib=$(( data_mib + HEADROOM_MIB ))
  (( data_mib >= MIN_DATA_GIB * 1024 )) || die "drive too small: only $((data_mib/1024)) GiB left for data after ISO partition (need >= $MIN_DATA_GIB)"
}

show_plan() {
  local iso=$1 d=$2
  layout "$iso" "$d"
  cat <<PLAN
Target : $d  model='$(dev_field "$d" MODEL)' serial='$(dev_field "$d" SERIAL)' size=$(dev_bytes "$d") bytes ($(( $(dev_bytes "$d") / 1048576 )) MiB) transport=$(dev_field "$d" TRAN)
ISO    : $iso ($(stat -c %s "$iso") bytes)
Layout (GPT, Ventoy $VENTOY_VERSION):
  1  exFAT  'Ventoy'   $(( p1_mib / 1024 )) GiB   holds the ISO (and any other ISOs you add)
  2  FAT    'VTOYEFI'  $EFI_MIB MiB     Ventoy boot/EFI
  3  ext4   'TORIDATA' $(( data_mib / 1024 )) GiB   persistent: models, chats, settings, images, outputs, documents, logs
  -  unallocated  ~$(( HEADROOM_MIB / 1024 )) GiB   left free on purpose (wear levelling / future repartitioning)
Marketed vs real capacity: the plan uses the measured byte size above, not the label on the box.
PLAN
}

cmd_plan() { check_target "$device"; show_plan "$iso" "$device"; }

cmd_write() {
  [[ $EUID -eq 0 ]] || die "run as root"
  for t in sgdisk mkfs.ext4 blockdev lsblk findmnt sha256sum tar; do need "$t"; done
  check_target "$device"
  local serial; serial=$(dev_field "$device" SERIAL)
  [[ -n "$confirm_serial" ]] || die "pass --confirm-serial '$serial' to approve erasing $device"
  [[ "$confirm_serial" == "$serial" ]] || die "serial mismatch: you approved '$confirm_serial' but $device reports '$serial'. Nothing written."
  [[ -f "$iso.sha256" ]] || die "missing $iso.sha256"
  ( cd "$(dirname "$iso")" && sha256sum -c "$(basename "$iso").sha256" ) || die "ISO checksum does not match its .sha256"
  show_plan "$iso" "$device"
  local sig_before; sig_before="$(dev_field "$device" MODEL)|$serial|$(dev_bytes "$device")"

  # Ventoy tools (checksum-verified)
  local vdir=${ventoy_dir:-}
  if [[ -z "$vdir" ]]; then
    local tmp; tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
    echo "Downloading Ventoy $VENTOY_VERSION ..."
    curl -fsSL -o "$tmp/v.tar.gz" "https://github.com/ventoy/Ventoy/releases/download/v$VENTOY_VERSION/ventoy-$VENTOY_VERSION-linux.tar.gz"
    echo "$VENTOY_SHA256  $tmp/v.tar.gz" | sha256sum -c - || die "Ventoy tarball checksum mismatch"
    tar -xzf "$tmp/v.tar.gz" -C "$tmp"; vdir="$tmp/ventoy-$VENTOY_VERSION"
  fi

  # identity re-check immediately before the destructive step
  check_target "$device"
  [[ "$sig_before" == "$(dev_field "$device" MODEL)|$(dev_field "$device" SERIAL)|$(dev_bytes "$device")" ]] || die "device identity changed - aborting"
  echo ">>> ERASING $device (serial $serial) now"

  ( cd "$vdir" && printf 'y\ny\n' | ./Ventoy2Disk.sh -I -g -r "$reserve_mib" "$device" )
  partprobe "$device" 2>/dev/null || true; udevadm settle --timeout=20 2>/dev/null || true; sleep 2

  # persistent partition in the space Ventoy reserved at the end
  local p; p() { if [[ "$device" == *[0-9] ]]; then echo "${device}p$1"; else echo "${device}$1"; fi; }
  sgdisk -n "3:0:+${data_mib}M" -t 3:8300 -c 3:TORIDATA "$device"
  partprobe "$device" 2>/dev/null || true; udevadm settle --timeout=20 2>/dev/null || true; sleep 2
  mkfs.ext4 -q -F -L TORIDATA -m 0 -E lazy_itable_init=1 "$(p 3)"

  # copy ISO to the Ventoy partition and verify it
  local mnt; mnt=$(mktemp -d)
  mount -t exfat "$(p 1)" "$mnt" 2>/dev/null || mount "$(p 1)" "$mnt" 2>/dev/null \
    || mount.exfat-fuse "$(p 1)" "$mnt" || die "cannot mount the exFAT Ventoy partition (need kernel exfat support or exfat-fuse)"
  cp "$iso" "$mnt/"; cp "$iso.sha256" "$mnt/" ; sync
  ( cd "$mnt" && sha256sum -c "$(basename "$iso").sha256" ) || { umount "$mnt"; die "copied ISO FAILED checksum verification"; }
  umount "$mnt"; rmdir "$mnt"
  # initialise the data tree so first boot finds it ready
  mnt=$(mktemp -d); mount "$(p 3)" "$mnt"
  mkdir -p "$mnt/tori"/{models,chats,config,images,outputs,documents,logs,server}
  chown -R 1000:1000 "$mnt/tori"; chmod 700 "$mnt/tori/config"
  sync; umount "$mnt"; rmdir "$mnt"
  sync; blockdev --flushbufs "$device"
  echo; echo "=== Result ==="; sgdisk -p "$device"; lsblk -o NAME,SIZE,FSTYPE,LABEL,UUID "$device"
  echo "Done. ISO verified on the drive; drive is unmounted and safe to remove."
}

# --- args ----------------------------------------------------------------------------------
sub=${1:-}; shift || true
iso=""; device=""; confirm_serial=""; ventoy_dir=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --iso) iso=$2; shift 2;;
    --device) device=$2; shift 2;;
    --confirm-serial) confirm_serial=$2; shift 2;;
    --ventoy) ventoy_dir=$2; shift 2;;
    *) die "unknown argument $1";;
  esac
done
case "$sub" in
  list) cmd_list;;
  plan) [[ -f "$iso" && -n "$device" ]] || die "need --iso and --device"; cmd_plan;;
  write) [[ -f "$iso" && -n "$device" ]] || die "need --iso and --device"; cmd_write;;
  *) sed -n '2,9p' "$0"; exit 2;;
esac
