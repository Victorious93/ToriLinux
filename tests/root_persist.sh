#!/usr/bin/env bash
# Root-only test of tori-priv storage handling on a REAL ext4 filesystem in a loop image
# (labelled TORIDATA). Simulates: first boot, data written, "reboot", missing/read-only storage, temp mode.
# Needs: root, loop devices, user 'tori' (created if absent in a throwaway VM/CI container).
set -euo pipefail
priv="$(cd "$(dirname "$0")/.." && pwd)/airootfs/usr/local/lib/tori/tori-priv"
lib="$(dirname "$priv")"
img=$(mktemp /var/tmp/torips.XXXXXX); truncate -s 300M "$img"; mkfs.ext4 -q -L TORIDATA "$img"
id tori >/dev/null 2>&1 || useradd -m -s /bin/bash tori
fail=0; ok() { echo "PASS $1"; }; bad() { echo "FAIL $1"; fail=1; }
state() { python3 -c "import json;print(json.load(open('/run/tori/persist.json'))['mode'])"; }
cleanup() { umount /mnt/toridata 2>/dev/null || true; losetup -j "$img" -O NAME -n | xargs -r losetup -d; rm -f "$img"; }
trap cleanup EXIT

"$priv" persist-auto >/dev/null; [[ $(state) == missing ]] && ok "no media -> missing (no silent temp mode)" || bad missing
L=$(losetup --find --show "$img")
"$priv" persist-auto >/dev/null; [[ $(state) == persistent ]] && ok "TORIDATA found by label -> persistent" || bad persistent
su tori -s /bin/bash -c "python3 -I -c \"import sys; sys.path.insert(0,'$lib'); import toricommon as t; s=t.load_settings(); s['ctx']=7777; t.save_settings(s)\""
echo doc > /mnt/toridata/tori/documents/n.txt; sync
umount /mnt/toridata; losetup -d "$L"; rm -f /run/tori/persist.json
L=$(losetup --find --show "$img"); "$priv" persist-auto >/dev/null
got=$(su tori -s /bin/bash -c "python3 -I -c \"import sys; sys.path.insert(0,'$lib'); import toricommon as t; print(t.load_settings()['ctx'])\"")
[[ "$got" == 7777 && "$(cat /mnt/toridata/tori/documents/n.txt)" == doc ]] && ok "settings + files survive remount" || bad persist-survive
[[ $(stat -c %a /mnt/toridata/tori/config) == 700 ]] && ok "config dir is 0700" || bad perms
umount /mnt/toridata; losetup -d "$L"; L=$(losetup -r --find --show "$img")
"$priv" persist-auto >/dev/null 2>&1 || true; [[ $(state) == readonly ]] && ok "read-only media reported as readonly" || bad readonly
losetup -d "$L"
"$priv" persist-temp >/dev/null; [[ $(state) == temporary ]] && ok "explicit temporary session" || bad temp
"$priv" persist-mount '../../etc' >/dev/null 2>&1 && bad "bad uuid accepted" || ok "malformed UUID rejected"
exit $fail
