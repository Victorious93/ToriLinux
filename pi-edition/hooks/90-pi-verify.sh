#!/bin/bash
# Fail the build on anything that would make the image unsafe or unbootable.
set -Eeuo pipefail
. /usr/share/victorlinux/pi-build.env
fail() { echo "pi-verify: FAIL: $*" >&2; exit 1; }
ls /boot/firmware/kernel8.img >/dev/null 2>&1 || fail "no kernel8.img (Pi 4 / Zero 2 W kernel)"
ls /boot/firmware/kernel_2712.img >/dev/null 2>&1 || fail "no kernel_2712.img (Pi 5 kernel)"
ls /boot/firmware/start4.elf /boot/firmware/fixup4.dat >/dev/null 2>&1 || echo "pi-verify: note: no start4.elf (Pi 5 does not need it; Pi 4 does)" >&2
ls /boot/firmware/*.dtb >/dev/null 2>&1 || fail "no device trees in /boot/firmware"
for dtb in bcm2712-rpi-5-b.dtb bcm2711-rpi-4-b.dtb bcm2710-rpi-zero-2-w.dtb; do
  [ -e "/boot/firmware/$dtb" ] || fail "missing $dtb"
done
grep -q '@ROOT_PARTUUID@' /boot/firmware/cmdline.txt || fail "cmdline.txt lost its root placeholder"
ls /lib/firmware/brcm/brcmfmac43455-sdio* >/dev/null 2>&1 || fail "no brcmfmac43455 Wi-Fi firmware (Pi 5/4)"
ls /lib/firmware/brcm/brcmfmac43436* /lib/firmware/brcm/brcmfmac43430* >/dev/null 2>&1 || fail "no Zero 2 W Wi-Fi firmware"
ls /lib/firmware/brcm/BCM4345C0* /lib/firmware/brcm/BCM4345C5* >/dev/null 2>&1 || fail "no Pi 4/5 Bluetooth firmware"
grep -qE '^victor:[!*]' /etc/shadow || fail "owner account is not locked"
[ ! -e /etc/systemd/system/multi-user.target.wants/ssh.service ] || fail "SSH enabled at boot"
ls /etc/systemd/system/*.wants/victor-llama* >/dev/null 2>&1 && fail "AI service enabled at boot"
[ "$(readlink -f /etc/systemd/system/sshswitch.service)" = /dev/null ] || fail "sshswitch not masked"
grep -qx 'AutoEnable=false' /etc/bluetooth/main.conf || fail "Bluetooth would power on automatically"
for f in /usr/local/bin/victor-ctl /usr/local/bin/victor-pi /usr/local/lib/victorlinux/pi/victor-pi-priv; do [ -x "$f" ] || fail "$f missing"; done
if [ "$PI_DESKTOP" = 1 ]; then
  [ -x /usr/local/bin/victor-control ] && [ -x /usr/local/bin/victor-pi-setup ] || fail "desktop tools missing"
  dpkg -s xfce4 python3-tk >/dev/null 2>&1 || fail "desktop packages missing"
else
  ! dpkg -s xfce4 >/dev/null 2>&1 || fail "headless image contains xfce4"
fi
if [ "$PI_LLAMACPP" = 1 ]; then file -L /opt/victorlinux/llama.cpp/bin/llama-server 2>/dev/null | grep -q aarch64 || [ -x /opt/victorlinux/llama.cpp/bin/llama-server ] || fail "llama-server missing"; fi
python3 -c "import sys; sys.path.insert(0,'/usr/local/lib/victorlinux/pi'); import victorpi; print(victorpi.classify('Raspberry Pi Zero 2 W Rev 1.0', 427000)['role'])" | grep -qx node || fail "victorpi self-check"
echo "pi-verify: all checks passed"
