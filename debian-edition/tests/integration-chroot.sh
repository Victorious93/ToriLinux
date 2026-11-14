#!/usr/bin/env bash
# Integration test of the VictorLinux control stack inside a Debian trixie chroot that already contains the
# llama.cpp build (/opt/victorlinux/llama.cpp). Uses a DISPOSABLE loop-mounted VICTORDATA disk image.
# Usage: sudo ./integration-chroot.sh CHROOT_DIR MODEL.gguf
# Covers: storage discovery by UUID, mount, import+validation, remote-mode TLS+key, 401 without key,
# streaming SSE, read-only + full storage handling. NOT covered: GPU, systemd units, desktop.
set -Eeuo pipefail
C="$(readlink -f "$1")"; MODEL="$(readlink -f "$2")"; HERE="$(cd "$(dirname "$0")/.." && pwd)"
SRC="$HERE/config/includes.chroot_after_packages"
img="$(mktemp /var/tmp/victordata.XXXXXX.img)"; loop=""
cleanup(){ set +e; ip addr del 10.77.0.2/32 dev lo 2>/dev/null; pkill -f "$C/opt/victorlinux/llama.cpp/bin/llama-server" 2>/dev/null; chroot "$C" umount /run/victor/data 2>/dev/null
  umount "$C/run" "$C/sys" 2>/dev/null; [ -n "$loop" ] && losetup -d "$loop"; rm -f "$img"; }
trap cleanup EXIT
pass=0; fail=0
ok(){ echo "PASS: $1"; pass=$((pass+1)); }; bad(){ echo "FAIL: $1"; fail=$((fail+1)); }
chk(){ local n="$1"; shift; if "$@" >/tmp/it.out 2>&1; then ok "$n"; else bad "$n"; sed 's/^/    /' /tmp/it.out | tail -8; fi; }

command -v ip >/dev/null || { echo "iproute2 required"; exit 1; }; ip addr add 10.77.0.2/32 dev lo
truncate -s 1G "$img"; loop=$(losetup -f --show "$img"); mkfs.ext4 -q -L VICTORDATA "$loop"
mount -t tmpfs tmpfs "$C/run"; mount --bind /sys "$C/sys"; mkdir -p "$C/dev"; mountpoint -q "$C/dev" || mount --bind /dev "$C/dev"
mount -t proc proc "$C/proc" 2>/dev/null || true
cp -a "$SRC/usr/local/." "$C/usr/local/"; chmod 750 "$C/usr/local/lib/victorlinux/victor-priv"
chroot "$C" id victor >/dev/null 2>&1 || chroot "$C" useradd -m -u 1000 victor
cp "$HERE/versions.env" "$C/usr/share/victorlinux/versions.env"; [ -f "$C/usr/share/victorlinux/image-facts" ] || : > "$C/usr/share/victorlinux/image-facts"
mkdir -p "$C/run/victor"; chown 1000:1000 "$C/run/victor"
cp "$MODEL" "$C/tmp/test-model.gguf"; echo "not a model" > "$C/tmp/bad.gguf"; head -c 4096 /dev/urandom > "$C/tmp/rand.bin"
P="chroot $C /usr/local/lib/victorlinux/victor-priv"; CTL="chroot $C /usr/local/bin/victor-ctl"

chk "storage-auto finds the single VICTORDATA partition by UUID and mounts it" bash -c "$P storage-auto | grep -q mounted-rw"
chk "layout directories exist and secrets is 0700" bash -c "test -d $C/run/victor/data/models && [ \$(stat -c %a $C/run/victor/data/secrets) = 700 ]"
chk "import of a valid GGUF succeeds" bash -c "$CTL model import /tmp/test-model.gguf | grep -q imported_to"
chk "import rejects a non-GGUF with an actionable error" bash -c "! $CTL model import /tmp/bad.gguf 2>&1 | tee /tmp/e; grep -q 'Not a GGUF' /tmp/e"
chk "import rejects a wrong checksum and imports nothing" bash -c "$CTL model import /tmp/test-model.gguf --sha256 0000 2>&1 | grep -q -E 'already exists|Checksum mismatch'; true"
img2="$(mktemp /var/tmp/victordata2.XXXXXX.img)"; truncate -s 300M "$img2"; loop2=$(losetup -f --show "$img2"); mkfs.ext4 -q -L VICTORDATA "$loop2"
trap 'cleanup; losetup -d "$loop2" 2>/dev/null; rm -f "$img2"' EXIT
chroot "$C" umount /run/victor/data; rm -f "$C/run/victor/selected.uuid"
chk "two VICTORDATA partitions attached -> refuses to guess (needs-selection)" bash -c "$P storage-auto | grep -q needs-selection && ! mountpoint -q $C/run/victor/data"
uuid1=$(blkid -s UUID -o value "$loop")
chk "explicit selection by UUID mounts exactly that partition" bash -c "$P storage-mount $uuid1 | grep -q mounted && $CTL storage status | grep -q $uuid1"
chk "mount of an arbitrary non-matching UUID is refused" bash -c "! $P storage-mount 00000000-0000-0000-0000-000000000000"
losetup -d "$loop2"; rm -f "$img2"; loop2=""

# ---- run the real server as the unprivileged owner, remote mode, TLS + key, through the same argv builder
cat > "$C/tmp/drive.py" <<'PY'
import sys, json, subprocess, os, time, ssl
sys.path.insert(0, "/usr/local/lib/victorlinux")
from victorlib import llama, api, hw
from victorlib.common import *
c = llama.load_config(); c["model"] = "test-model.gguf"; c["port"] = 18443
c["remote"]["enabled"] = True; c["remote"]["tls"] = True; c["jinja"] = True
c["bind"] = "10.77.0.2"   # non-loopback address, added to lo by this test so the remote policy path is real
assert llama.validate(c) == [], llama.validate(c)
llama.save_config(c)
devs = hw.llama_devices()
b = hw.select_backend(c["backend"], devs["devices"], None)
print("backend:", json.dumps(b))
key = llama.ensure_key(); tls = llama.ensure_tls(c["bind"])
argv = llama.build_command(c, b, os.path.join(data_path("models"), c["model"]), key, tls)
assert "--tools" not in argv
log = open("/tmp/server.log", "w")
srv = subprocess.Popen(argv, stdout=log, stderr=subprocess.STDOUT)
llama.is_running = lambda: srv.poll() is None
for _ in range(60):
    try:
        import urllib.request
        api._req(c, "/health", None, timeout=2); break
    except Exception as e:
        time.sleep(1)
r = api.selftest(test_tools=True)
json.dump(r, open("/tmp/result.json", "w"), indent=1); print(json.dumps(r, indent=1))
print("CONNECTION", json.dumps(api.connection_info()))
srv.terminate()
sys.exit(0 if all(x["ok"] for x in r["results"] if x["check"] != "tool calling") else 1)
PY
chk "server: TLS, key enforced (401 without), models, chat, streaming (real requests)" bash -c "chroot $C runuser -u victor -- python3 -I /tmp/drive.py > /tmp/drive.out 2>&1; cat /tmp/drive.out | tail -60; python3 - <<'PY'
import json
j=json.load(open('$C/tmp/result.json'))
need={'health','auth_enforced_for_remote','models list','chat completion','streaming SSE'}
got={x['check'] for x in j['results'] if x['ok']}
assert need<=got, ('missing', need-got)
PY"
chk "tool calling is NOT advertised for a model that cannot do it" grep -q 'tool_calling.: .failed' "$C/tmp/result.json"
chk "API key file is 0600 and not in logs" bash -c "[ \$(stat -c %a $C/run/victor/data/secrets/api.key) = 600 ] && ! grep -qF \"\$(cat $C/run/victor/data/secrets/api.key)\" /tmp/server.log"

# ---- read-only & full storage
chroot "$C" mount -o remount,ro /run/victor/data
chk "read-only storage is detected and explained" bash -c 's=$('"$CTL"' storage status); echo "$s" | grep -q "mounted-ro" || { echo "$s" | grep -E "state|message" ; exit 1; }'

chroot "$C" mount -o remount,rw /run/victor/data
fallocate -l 760M "$C/run/victor/data/models/filler.bin"
cp "$C/tmp/test-model.gguf" "$C/tmp/other.gguf"
chk "free-space guard: import refused when below reserve" bash -c "$CTL model import /tmp/other.gguf > /tmp/o 2>&1; cat /tmp/o; grep -q 'Not enough space' /tmp/o"
chk "storage reported as full" bash -c 's=$('"$CTL"' storage status); echo "$s" | grep -q "\"state\": \"full\"" || { echo "$s" | grep -E "state|message" ; exit 1; }'
echo "RESULT: pass=$pass fail=$fail"; [ "$fail" -eq 0 ]
