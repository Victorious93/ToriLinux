#!/usr/bin/env bash
# Static verification of a built ISO: checksum, El Torito BIOS+UEFI boot records, squashfs presence, no default credentials.
# Usage: verify-iso.sh ISO   (root needed only for the squashfs credential scan)
set -Eeuo pipefail
iso="$1"; fail=0
note(){ printf '%-4s %s\n' "$1" "$2"; [ "$1" = FAIL ] && fail=1 || true; }
[ -f "$iso.sha256" ] && (cd "$(dirname "$iso")" && sha256sum -c "$(basename "$iso").sha256" >/dev/null) && note PASS "sha256 matches $(basename "$iso").sha256" || note FAIL "sha256 check"
rep="$(xorriso -indev "$iso" -report_el_torito plain 2>&1)"
echo "$rep" | grep -q 'BIOS' && note PASS "El Torito BIOS boot image present" || note FAIL "no BIOS boot image"
echo "$rep" | grep -q 'UEFI' && note PASS "El Torito UEFI boot image present" || note FAIL "no UEFI boot image"
xorriso -indev "$iso" -ls /live 2>&1 | grep -q filesystem.squashfs && note PASS "live/filesystem.squashfs present" || note FAIL "squashfs missing"
# boot-menu branding (isolinux + GRUB): VictorLinux labels present, no generic Debian/live-build text
bt="$(mktemp -d)"
for f in /isolinux/live.cfg /isolinux/menu.cfg /boot/grub/grub.cfg; do xorriso -osirrox on -indev "$iso" -extract "$f" "$bt/$(echo "$f" | tr / _)" >/dev/null 2>&1 || true; done
if [ -s "$bt/_isolinux_live.cfg" ] && [ -s "$bt/_boot_grub_grub.cfg" ] && grep -q 'VictorLinux' "$bt/_isolinux_live.cfg" "$bt/_boot_grub_grub.cfg" "$bt/_isolinux_menu.cfg" \
   && ! grep -qE 'Live system|Debian GNU/Linux|live-build' "$bt/_isolinux_live.cfg" "$bt/_isolinux_menu.cfg" "$bt/_boot_grub_grub.cfg"; then
  note PASS "boot menus (isolinux + GRUB) say VictorLinux, no generic Debian/live-build labels"
else note FAIL "boot-menu branding"; fi
rm -rf "$bt"
if [ "$(id -u)" -eq 0 ]; then
  t="$(mktemp -d)"; trap 'umount "$t/sq" 2>/dev/null; rm -rf "$t"' EXIT
  xorriso -osirrox on -indev "$iso" -extract /live/filesystem.squashfs "$t/fs.sq" >/dev/null 2>&1
  mkdir "$t/sq"; mount -t squashfs -o loop,ro "$t/fs.sq" "$t/sq"
  grep -q '^victor:[!*]' "$t/sq/etc/shadow" && note PASS "owner account is locked in the image (no shared password)" || note FAIL "owner account has a password hash"
  [ ! -e "$t/sq/etc/sudoers.d/live" ] && note PASS "no live NOPASSWD:ALL sudoers" || note FAIL "live sudoers present"
  ! grep -rq 'NOPASSWD: ALL' "$t/sq/etc/sudoers" "$t/sq/etc/sudoers.d" 2>/dev/null && note PASS "no unrestricted passwordless sudo" || note FAIL "NOPASSWD: ALL found"
  ls "$t/sq/etc/ssh/ssh_host_"* >/dev/null 2>&1 && note FAIL "SSH host keys baked in" || note PASS "no baked SSH host keys"
  [ ! -s "$t/sq/etc/machine-id" ] && note PASS "machine-id empty (unique per boot)" || note FAIL "machine-id baked"
  [ -L "$t/sq/etc/systemd/system/multi-user.target.wants/ssh.service" ] && note FAIL "ssh enabled" || note PASS "ssh not enabled at boot"
  ls "$t/sq/etc/systemd/system/"*.wants/victor-llama* >/dev/null 2>&1 && note FAIL "AI service enabled at boot" || note PASS "no AI service enabled at boot"
  test -x "$t/sq/opt/victorlinux/llama.cpp/bin/llama-server" && note PASS "llama-server present" || note FAIL "llama-server missing"
  ! grep -q 'kali' "$t/sq/etc/apt/sources.list" "$t/sq/etc/apt/sources.list.d/"* 2>/dev/null && note PASS "no Kali repositories on host" || note FAIL "kali repo on host"
fi
exit $fail
