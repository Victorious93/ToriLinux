#!/usr/bin/env bash
# Prepare a USB drive: Ventoy + the built ISO + a persistent ext4 data partition labelled VICTORDATA.
#
#   scripts/make-usb.sh list
#   scripts/make-usb.sh plan  --iso out/X.iso --device /dev/sdX
#   scripts/make-usb.sh write --iso out/X.iso --device /dev/sdX --confirm-serial SERIAL [--layout raw|ventoy] [--ventoy DIR]
#   scripts/make-usb.sh verify --iso out/X.iso --device /dev/sdX                    (read-only check of a written stick)
#   scripts/make-usb.sh update --iso out/NEW.iso --device /dev/sdX --confirm-serial SERIAL
#                       (raw layout: new OS image, VICTORDATA kept in place; refuses if the new image would reach the data)
#
# raw-layout options (see docs/usb.md):
#   --table auto|mbr|gpt   auto (default) keeps the proven MBR layout when it is fully representable, else GPT
#   --data-gib N           data partition size (default: all usable space; the rest stays unallocated)
#   --reserve-mib N        space deliberately left unallocated at the end (default 1024)
#   --min-data-gib N       refuse drives that would leave less data space (default 2)
#   --allow-mbr-cap        only for images without GPT support: accept a data partition limited to MBR's 2 TiB
#   --headroom-mib N       free space kept after the OS image so later, larger images can be written with `update`
#                          without moving the data partition (default 2048; 0 = none)
#
# Layouts:
#   raw     (default) the hybrid ISO is written straight to the drive, then an ext4 partition labelled VICTORDATA
#           is added in the free space (MBR slot 3 up to 2 TiB, GPT beyond that or on request). One OS per stick.
#           The complete plan (checked 64-bit arithmetic, real sector sizes) is validated BEFORE anything is erased.
#   ventoy  Ventoy + ISO + VICTORDATA. Multi-ISO, but in testing Ventoy booted this ISO in legacy BIOS mode only;
#           UEFI mode did NOT boot it in the emulator. Use only if you need Ventoy and have tested your hardware.
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
  if [[ -z "$v" && "$2" == SERIAL && -n "${VICTOR_USB_TEST_LOOP:-}" ]]; then v=LOOPTEST; fi
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
  [[ "$(dev_field "$d" TYPE)" == "disk" || ( -n "${VICTOR_USB_TEST_LOOP:-}" && "$(dev_field "$d" TYPE)" == "loop" ) ]] \
    || die "$d is not a whole disk (give the disk, e.g. /dev/sdb, not a partition)"
  local real; real=$(readlink -f "$d")
  local p
  # the running system, the build workspace, and the medium this live session booted from
  for p in / "$repo" /boot /home /var/tmp /tmp /run/archiso/bootmnt /run/live/medium /run/initramfs/live; do
    while read -r s; do
      [[ -n "$s" && "$(readlink -f "$s")" == "$real" ]] && die "$d backs $p (system or build workspace) - refusing"
    done < <(backing_disks "$p")
  done
  if lsblk -nro MOUNTPOINTS "$d" | grep -q '[^[:space:]]'; then
    die "$d (or a partition) is mounted - unmount it first: $(lsblk -nro NAME,MOUNTPOINTS "$d" | tr '\n' ';')"
  fi
  if [[ -z "${VICTOR_USB_TEST_LOOP:-}" ]]; then
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

# --- raw hybrid layout helpers ---------------------------------------------------------------
layout_py="$repo/scripts/victor_layout.py"
table_opt=auto; data_gib=""; reserve_mib=1024; min_data_gib=2; allow_mbr_cap=0; headroom_mib=2048

layout_args() {
  local a=(--iso "$1" --device "$2" --table "$table_opt" --reserve-mib "$reserve_mib" --min-data-gib "$min_data_gib" --image-headroom-mib "$headroom_mib")
  [[ -n "$data_gib" ]] && a+=(--data-gib "$data_gib")
  (( allow_mbr_cap )) && a+=(--allow-mbr-cap)
  printf '%s\0' "${a[@]}"
}

raw_layout() {   # args: iso device. Sets plan_* variables from victor_layout.py; dies (touching nothing) if the plan is invalid.
  need python3
  local args=() out k v
  while IFS= read -r -d '' x; do args+=("$x"); done < <(layout_args "$1" "$2")
  out=$(python3 "$layout_py" plan "${args[@]}" 2>&1) || die "layout rejected before any write: ${out#ERROR=}"
  plan_note=""
  while IFS='=' read -r k v; do
    case "$k" in
      TABLE) plan_table=$v;; LOGICAL) plan_logical=$v;; PHYSICAL) plan_physical=$v;;
      DATA_BYTES) plan_data_bytes=$v;; DATA_START_LBA) plan_start=$v;; DATA_END_LBA) plan_end=$v;;
      UNALLOCATED_BYTES) plan_unalloc=$v;; IMAGE_END_BYTES) plan_image_end=$v;;
      NOTE) plan_note+="  NOTE: $v"$'\n';;
    esac
  done <<<"$out"
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
  if [[ "$layout_kind" == raw ]]; then
    raw_layout "$iso" "$d"
    cat <<PLAN
Target : $d  model='$(dev_field "$d" MODEL)' serial='$(dev_field "$d" SERIAL)' size=$(dev_bytes "$d") bytes ($(( $(dev_bytes "$d") / 1048576 )) MiB) transport=$(dev_field "$d" TRAN)
Sectors: logical=$plan_logical physical=$plan_physical  (512e is supported; 4Kn drives are refused for boot media)
ISO    : $iso ($(stat -c %s "$iso") bytes)
Layout (raw hybrid ISO, partition table: $plan_table):
  -  ISO9660 hybrid image + its own EFI partition   ($(( plan_image_end / 1048576 )) MiB, partition table of the image kept as is)
  -  ${headroom_mib} MiB free after the image for future in-place updates (\`update\` keeps VICTORDATA where it is)
  +  ext4 'VICTORDATA'  LBA $plan_start..$plan_end  $(python3 -c "print(f'{$plan_data_bytes/2**30:.2f}')") GiB  persistent: models, chats, settings, images, outputs, documents, logs
  -  unallocated $(python3 -c "print(f'{$plan_unalloc/2**30:.2f}')") GiB left free on purpose$( [[ "$plan_table" == gpt ]] && echo ' (plus the backup GPT at the end of the drive)' )
${plan_note}Plan check: all offsets are validated with 64-bit arithmetic against the measured device size before anything is written.
The partition table is intentionally changed, so the drive is NOT byte-identical to the ISO; the ISO payload is verified separately.
Marketed vs real capacity: the plan uses the measured byte size above, not the label on the box.
PLAN
    return
  fi
  layout "$iso" "$d"
  cat <<PLAN
Target : $d  model='$(dev_field "$d" MODEL)' serial='$(dev_field "$d" SERIAL)' size=$(dev_bytes "$d") bytes ($(( $(dev_bytes "$d") / 1048576 )) MiB) transport=$(dev_field "$d" TRAN)
ISO    : $iso ($(stat -c %s "$iso") bytes)
Layout (GPT, Ventoy $VENTOY_VERSION):
  1  exFAT  'Ventoy'   $(( p1_mib / 1024 )) GiB   holds the ISO (and any other ISOs you add)
  2  FAT    'VTOYEFI'  $EFI_MIB MiB     Ventoy boot/EFI
  3  ext4   'VICTORDATA' $(( data_mib / 1024 )) GiB   persistent: models, chats, settings, images, outputs, documents, logs
  -  unallocated  ~$(( HEADROOM_MIB / 1024 )) GiB   left free on purpose (wear levelling / future repartitioning)
Marketed vs real capacity: the plan uses the measured byte size above, not the label on the box.
PLAN
}

cmd_plan() { check_target "$device"; show_plan "$iso" "$device"; }

write_raw() {
  need dd; need python3; need wipefs; need partprobe
  raw_layout "$iso" "$device"
  local sig_before; sig_before="$(dev_field "$device" MODEL)|$(dev_field "$device" SERIAL)|$(dev_bytes "$device")"
  check_target "$device"
  [[ "$sig_before" == "$(dev_field "$device" MODEL)|$(dev_field "$device" SERIAL)|$(dev_bytes "$device")" ]] || die "device identity changed - aborting"
  echo ">>> ERASING $device (serial $(dev_field "$device" SERIAL)) now"
  wipefs -a "$device" >/dev/null
  sgdisk --zap-all "$device" >/dev/null 2>&1 || true
  dd if="$iso" of="$device" bs=4M conv=fsync oflag=direct status=progress
  sync
  # verify the bytes on the device match the ISO (read back, not the page cache)
  local isob want got; isob=$(stat -c %s "$iso"); want=$(awk '{print $1}' "$iso.sha256")
  blockdev --flushbufs "$device"
  got=$(dd if="$device" bs=4M iflag=direct,count_bytes count="$isob" status=none | sha256sum | awk '{print $1}')
  [[ "$got" == "$want" ]] || die "ISO read back from the device does NOT match its checksum"
  echo "ISO read back from $device matches $want"
  # write ONLY the partition table (MBR slot 3, or a GPT that keeps the image's own entries), re-validating the plan first
  local args=() x; while IFS= read -r -d '' x; do args+=("$x"); done < <(layout_args "$iso" "$device")
  python3 "$layout_py" apply "${args[@]}" >/dev/null || die "writing the partition table failed"
  sync; blockdev --flushbufs "$device"
  # the image payload (everything after the 32 KiB system area that holds the partition tables) must be unchanged
  local pay_want pay_got
  pay_want=$(tail -c +32769 "$iso" | sha256sum | awk '{print $1}')
  pay_got=$(dd if="$device" bs=4M iflag=direct,skip_bytes,count_bytes skip=32768 count=$((isob - 32768)) status=none | sha256sum | awk '{print $1}')
  [[ "$pay_want" == "$pay_got" ]] || die "image payload changed while adding the data partition"
  echo "Image payload (after the system area) re-verified; partition table intentionally changed (table: $plan_table)."
  partprobe "$device" 2>/dev/null || true; udevadm settle --timeout=20 2>/dev/null || true; sleep 2
  # find the new partition by its start sector in sysfs (independent of the partition number and of the udev database)
  local p3 dn; dn=$(basename "$(readlink -f "$device")")
  p3=$(for sp in /sys/class/block/"$dn"/"$dn"*/start; do
         [[ -r "$sp" && "$(cat "$sp")" == "$plan_start" ]] && echo "/dev/$(basename "$(dirname "$sp")")"
       done | head -n1)
  [[ -n "$p3" && -b "$p3" ]] || die "data partition did not appear after the table write"
  mkfs.ext4 -q -F -L VICTORDATA -m 0 -E lazy_itable_init=1 "$p3"
  local mnt; mnt=$(mktemp -d); mount "$p3" "$mnt"
  mkdir -p "$mnt/victorlinux"/{models,chats,config,images,outputs,documents,logs,server}
  chown -R 1000:1000 "$mnt/victorlinux"; chmod 700 "$mnt/victorlinux/config"
  sync; umount "$mnt"; rmdir "$mnt"; sync; blockdev --flushbufs "$device"
  echo; echo "=== Result ==="; lsblk -o NAME,SIZE,FSTYPE,LABEL,UUID "$device"
  echo "Done. ISO verified by read-back; drive is unmounted and safe to remove."
}

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

  if [[ "$layout_kind" == raw ]]; then write_raw; return; fi
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
  sgdisk -n "3:0:+${data_mib}M" -t 3:8300 -c 3:VICTORDATA "$device"
  partprobe "$device" 2>/dev/null || true; udevadm settle --timeout=20 2>/dev/null || true; sleep 2
  mkfs.ext4 -q -F -L VICTORDATA -m 0 -E lazy_itable_init=1 "$(p 3)"

  # copy ISO to the Ventoy partition and verify it
  local mnt; mnt=$(mktemp -d)
  mount -t exfat "$(p 1)" "$mnt" 2>/dev/null || mount "$(p 1)" "$mnt" 2>/dev/null \
    || mount.exfat-fuse "$(p 1)" "$mnt" || die "cannot mount the exFAT Ventoy partition (need kernel exfat support or exfat-fuse)"
  cp "$iso" "$mnt/"; cp "$iso.sha256" "$mnt/" ; sync
  ( cd "$mnt" && sha256sum -c "$(basename "$iso").sha256" ) || { umount "$mnt"; die "copied ISO FAILED checksum verification"; }
  umount "$mnt"; rmdir "$mnt"
  # initialise the data tree so first boot finds it ready
  mnt=$(mktemp -d); mount "$(p 3)" "$mnt"
  mkdir -p "$mnt/victorlinux"/{models,chats,config,images,outputs,documents,logs,server}
  chown -R 1000:1000 "$mnt/victorlinux"; chmod 700 "$mnt/victorlinux/config"
  sync; umount "$mnt"; rmdir "$mnt"
  sync; blockdev --flushbufs "$device"
  echo; echo "=== Result ==="; sgdisk -p "$device"; lsblk -o NAME,SIZE,FSTYPE,LABEL,UUID "$device"
  echo "Done. ISO verified on the drive; drive is unmounted and safe to remove."
}

# --- verify / update in place ------------------------------------------------------------------
data_part_dev() {   # device start_lba -> partition node with that start sector (sysfs), or empty
  local dn sp; dn=$(basename "$(readlink -f "$1")")
  for sp in /sys/class/block/"$dn"/"$dn"*/start; do
    [[ -r "$sp" && "$(cat "$sp")" == "$2" ]] && { echo "/dev/$(basename "$(dirname "$sp")")"; return; }
  done
}

payload_ok() {   # iso device -> 0 when the image payload after the 32 KiB system area matches the ISO byte for byte
  local isob want got; isob=$(stat -c %s "$1")
  want=$(tail -c +32769 "$1" | sha256sum | awk '{print $1}')
  got=$(dd if="$2" bs=4M iflag=direct,skip_bytes,count_bytes skip=32768 count=$((isob - 32768)) status=none 2>/dev/null | sha256sum | awk '{print $1}')
  [[ "$want" == "$got" ]]
}

cmd_verify() {   # read-only: never writes, never mounts read-write
  [[ $EUID -eq 0 ]] || die "run as root (reading the raw device)"
  [[ -b "$device" ]] || die "$device is not a block device"
  local fail=0 cur start part
  if payload_ok "$iso" "$device"; then echo "OK    OS image on $device matches $(basename "$iso")"; else echo "FAIL  OS image on $device does NOT match $(basename "$iso")"; fail=1; fi
  if cur=$(python3 "$layout_py" current --device "$device" 2>/dev/null); then
    start=$(sed -n 's/^DATA_START_LBA=//p' <<<"$cur")
    part=$(data_part_dev "$device" "$start")
    echo "OK    data partition entry ($(sed -n 's/^TABLE=//p' <<<"$cur"), starts at sector $start)"
    if [[ -n "$part" ]]; then
      local label; label=$(blkid -o value -s LABEL "$part" 2>/dev/null || true)
      [[ "$label" == VICTORDATA || "$label" == TORIDATA ]] && echo "OK    $part is labelled $label" || { echo "FAIL  $part label is '$label', expected VICTORDATA"; fail=1; }
      if lsblk -nro MOUNTPOINTS "$part" | grep -q '[^[:space:]]'; then echo "SKIP  $part is mounted; filesystem check skipped"
      elif e2fsck -n "$part" >/dev/null 2>&1; then echo "OK    filesystem check (read-only) passed"
      else echo "FAIL  filesystem check (read-only) reported problems: run e2fsck on $part"; fail=1; fi
    else echo "WARN  partition node for sector $start not found (run partprobe)"; fi
  else echo "FAIL  no VICTORDATA partition entry found"; fail=1; fi
  (( fail == 0 )) && echo "Verified: nothing was written." || die "verification failed (nothing was written)"
}

cmd_update() {   # raw layout only: rewrite the OS image area and the table; VICTORDATA keeps its sectors and contents
  [[ $EUID -eq 0 ]] || die "run as root"
  for t in dd blockdev lsblk findmnt sha256sum python3 blkid; do need "$t"; done
  check_target "$device"
  local serial; serial=$(dev_field "$device" SERIAL)
  [[ -n "$confirm_serial" ]] || die "pass --confirm-serial '$serial' to approve updating $device"
  [[ "$confirm_serial" == "$serial" ]] || die "serial mismatch: you approved '$confirm_serial' but $device reports '$serial'. Nothing written."
  [[ -f "$iso.sha256" ]] || die "missing $iso.sha256"
  ( cd "$(dirname "$iso")" && sha256sum -c "$(basename "$iso").sha256" ) >/dev/null || die "ISO checksum does not match its .sha256"
  local keep=() out
  [[ -n "$keep_start" ]] && keep=(--keep-start "$keep_start" --keep-len "$keep_len" --keep-table "$keep_table")
  out=$(python3 "$layout_py" update-plan --iso "$iso" --device "$device" "${keep[@]}" 2>&1) || die "update refused before any write: ${out#ERROR=}"
  local start len table; start=$(sed -n 's/^DATA_START_LBA=//p' <<<"$out"); len=$(sed -n 's/^DATA_LEN_LBA=//p' <<<"$out"); table=$(sed -n 's/^TABLE=//p' <<<"$out")
  local part; part=$(data_part_dev "$device" "$start")
  [[ -n "$part" ]] || die "data partition node not found; nothing written"
  local uuid_before sb_before; uuid_before=$(blkid -o value -s UUID "$part" 2>/dev/null || true)
  sb_before=$(dd if="$part" bs=1M count=1 iflag=direct status=none | sha256sum | awk '{print $1}')
  cat <<PLAN
Update : $device  model='$(dev_field "$device" MODEL)' serial='$serial'
New OS : $iso ($(stat -c %s "$iso") bytes)
Keeps  : VICTORDATA $part  sectors $start..$((start + len - 1))  table=$table  UUID=$uuid_before  (contents untouched)
If this is interrupted, your data is still on the drive. Re-run with:
  --keep-start $start --keep-len $len --keep-table $table
PLAN
  local sig_before; sig_before="$(dev_field "$device" MODEL)|$serial|$(dev_bytes "$device")"
  check_target "$device"
  [[ "$sig_before" == "$(dev_field "$device" MODEL)|$(dev_field "$device" SERIAL)|$(dev_bytes "$device")" ]] || die "device identity changed - aborting"
  echo ">>> Writing the new OS image to $device (data partition is not touched)"
  dd if="$iso" of="$device" bs=4M conv=fsync,notrunc oflag=direct status=progress
  sync; blockdev --flushbufs "$device"
  python3 "$layout_py" update-apply --iso "$iso" --device "$device" --keep-start "$start" --keep-len "$len" --keep-table "$table" >/dev/null \
    || die "writing the partition table failed. Your data is intact; re-run update with --keep-start $start --keep-len $len --keep-table $table"
  sync; blockdev --flushbufs "$device"
  payload_ok "$iso" "$device" || die "new OS image read back does NOT match the ISO"
  partprobe "$device" 2>/dev/null || true; udevadm settle --timeout=20 2>/dev/null || true; sleep 2
  part=$(data_part_dev "$device" "$start")
  [[ -n "$part" ]] || die "data partition did not reappear at sector $start (re-run update with --keep-start $start --keep-len $len --keep-table $table)"
  [[ "$(blkid -o value -s UUID "$part" 2>/dev/null || true)" == "$uuid_before" ]] || die "data partition UUID changed - check the drive before using it"
  [[ "$(dd if="$part" bs=1M count=1 iflag=direct status=none | sha256sum | awk '{print $1}')" == "$sb_before" ]] || die "data partition start changed unexpectedly"
  echo "Done. New OS image verified by read-back; VICTORDATA kept at the same sectors with the same UUID."
}

# --- args ----------------------------------------------------------------------------------
sub=${1:-}; shift || true
iso=""; device=""; confirm_serial=""; ventoy_dir=""; layout_kind="raw"; keep_start=""; keep_len=""; keep_table=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --iso) iso=$2; shift 2;;
    --device) device=$2; shift 2;;
    --confirm-serial) confirm_serial=$2; shift 2;;
    --ventoy) ventoy_dir=$2; shift 2;;
    --table) table_opt=$2; shift 2; [[ "$table_opt" == auto || "$table_opt" == mbr || "$table_opt" == gpt ]] || die "--table must be auto, mbr or gpt";;
    --data-gib) data_gib=$2; shift 2;;
    --reserve-mib) reserve_mib=$2; shift 2;;
    --min-data-gib) min_data_gib=$2; shift 2;;
    --allow-mbr-cap) allow_mbr_cap=1; shift;;
    --headroom-mib) headroom_mib=$2; shift 2; [[ "$headroom_mib" =~ ^[0-9]+$ ]] || die "--headroom-mib must be a number";;
    --keep-start) keep_start=$2; shift 2; [[ "$keep_start" =~ ^[0-9]+$ ]] || die "--keep-start must be a sector number";;
    --keep-len) keep_len=$2; shift 2; [[ "$keep_len" =~ ^[0-9]+$ ]] || die "--keep-len must be a sector count";;
    --keep-table) keep_table=$2; shift 2; [[ "$keep_table" == mbr || "$keep_table" == gpt ]] || die "--keep-table must be mbr or gpt";;
    --layout) layout_kind=$2; shift 2; [[ "$layout_kind" == raw || "$layout_kind" == ventoy ]] || die "--layout must be raw or ventoy";;
    *) die "unknown argument $1";;
  esac
done
case "$sub" in
  list) cmd_list;;
  plan) [[ -f "$iso" && -n "$device" ]] || die "need --iso and --device"; cmd_plan;;
  write) [[ -f "$iso" && -n "$device" ]] || die "need --iso and --device"; cmd_write;;
  verify) [[ -f "$iso" && -n "$device" ]] || die "need --iso and --device"; cmd_verify;;
  update) [[ -f "$iso" && -n "$device" ]] || die "need --iso and --device"
          [[ -z "$keep_start" || ( -n "$keep_len" && -n "$keep_table" ) ]] || die "--keep-start needs --keep-len and --keep-table"
          cmd_update;;
  *) sed -n '2,12p' "$0"; exit 2;;
esac
