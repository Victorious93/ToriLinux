#!/usr/bin/env python3
"""VictorLinux USB provisioning (Ventoy + dedicated ext4 VICTORDATA partition).

The layout is COMPUTED from the target's real capacity in bytes and the measured ISO size. There are no
hard-coded 64 GB / 128 GB tables: a marketed "64GB" stick is often ~57-60 GiB and two sticks of the same
marketed size differ, so every number below is derived at run time.

  provision-usb.py list                              # candidate drives (identity, bytes, transport, mounts)
  provision-usb.py plan  --device /dev/sdX --iso F   # real capacity -> layout; rejects layouts that do not fit
  provision-usb.py plan  --size-bytes N --iso F      # same, for a hypothetical/virtual disk
  provision-usb.py write --device /dev/sdX --iso F --confirm 'ERASE <id>' [--ventoy-dir DIR]
  ...  --layout ventoy (default) | raw      # raw: the hybrid ISO is written byte-for-byte + a VICTORDATA partition after it
                                            # (one OS per stick, no Ventoy, UEFI+BIOS via the ISO's own boot records)

`write` never guesses: it requires the exact confirmation string printed by `plan`, which embeds the device's
model/serial/size identity, and re-reads that identity immediately before writing.
"""
import argparse, hashlib, json, os, re, shutil, subprocess, sys, time

MiB = 1024 * 1024
GiB = 1024 * MiB
VENTOY_EFI = 32 * MiB            # Ventoy's VTOYEFI partition (fixed by Ventoy)
PART_TABLE_OVERHEAD = 2 * MiB    # 1 MiB alignment gap at start + safety for backup structures at end
MIN_DATA = 4 * GiB               # below this a data partition is not useful -> reject
ISO_COPIES = 3                   # current + previous (rollback) + incoming update
ISO_HEADROOM = 1 * GiB
MIN_SPARE = 256 * MiB            # left unallocated at the end (flash wear-levelling / future use)
SPARE_FRACTION = 0.01


ALLOW_LOOP = False   # set only by --virtual-test, for disposable loop-device images


class Rejected(Exception):
    pass


def align_up(n, a=MiB):
    return -(-n // a) * a


def align_down(n, a=MiB):
    return n // a * a


def compute_layout(total_bytes, iso_bytes, min_data=MIN_DATA):
    """Pure function. All sizes in bytes, MiB-aligned. Raises Rejected if the layout does not fit.

    Ventoy reserve mechanism: Ventoy2Disk.sh -r <MiB> leaves that space unallocated at the END of the disk;
    we then place the ext4 data partition at the start of that space, leaving `spare` unallocated.
    """
    if total_bytes <= 0 or iso_bytes <= 0:
        raise Rejected("capacity and ISO size must be positive")
    iso_part = align_up(ISO_COPIES * iso_bytes + ISO_HEADROOM)          # Ventoy exFAT partition (ISOs live here)
    spare = align_up(max(MIN_SPARE, int(total_bytes * SPARE_FRACTION)))
    usable = align_down(total_bytes) - PART_TABLE_OVERHEAD
    data = align_down(usable - VENTOY_EFI - iso_part - spare)
    if data < min_data:
        need = PART_TABLE_OVERHEAD + VENTOY_EFI + iso_part + spare + min_data
        raise Rejected(f"Layout does not fit: drive has {total_bytes} B ({total_bytes/GiB:.2f} GiB); ISO partition "
                       f"({iso_part/GiB:.2f} GiB) + Ventoy EFI + spare leave only {max(data,0)/GiB:.2f} GiB for data "
                       f"(minimum {min_data/GiB:.0f} GiB). Needs at least {need/GiB:.2f} GiB.")
    reserve = data + spare                                               # passed to Ventoy as -r (MiB)
    layout = {
        "total_bytes": total_bytes, "iso_bytes": iso_bytes,
        "ventoy_iso_partition_bytes": iso_part, "ventoy_efi_bytes": VENTOY_EFI,
        "data_partition_bytes": data, "spare_unallocated_bytes": spare,
        "ventoy_reserve_mib": reserve // MiB, "alignment_bytes": MiB,
        "iso_slots": ISO_COPIES,
    }
    consumed = PART_TABLE_OVERHEAD + VENTOY_EFI + iso_part + data + spare
    assert consumed <= total_bytes, "internal error: layout exceeds device"
    layout["unaccounted_bytes"] = total_bytes - consumed
    return layout


def fmt_layout(l):
    g = lambda k: f"{l[k]/GiB:8.2f} GiB"
    return "\n".join([
        f"  device capacity (real)    : {l['total_bytes']:>16,d} B  ({l['total_bytes']/GiB:.2f} GiB)",
        f"  ISO (measured)            : {l['iso_bytes']:>16,d} B  ({l['iso_bytes']/GiB:.2f} GiB)",
        f"  Ventoy ISO partition      : {g('ventoy_iso_partition_bytes')}  (holds {l['iso_slots']} ISO generations + 1 GiB)",
        f"  Ventoy EFI partition      : {g('ventoy_efi_bytes')}",
        f"  VICTORDATA (ext4, persist): {g('data_partition_bytes')}",
        f"  unallocated spare         : {g('spare_unallocated_bytes')}",
        f"  Ventoy reserve argument   : -r {l['ventoy_reserve_mib']} (MiB)",
    ])


# ---------------------------------------------------------------- raw hybrid-ISO layout
def inspect_iso_mbr(sector0, iso_bytes):
    """Pure. Parse the ISO's MBR (first 512 bytes) and decide whether a data partition can be appended in an empty
    primary slot. Returns {"free_slot": 0-3, "iso_end_sector": int, "entries": [...]}; raises Rejected otherwise."""
    if len(sector0) < 512 or sector0[510:512] != b"\x55\xaa":
        raise Rejected("ISO has no MBR boot signature: it is not a hybrid image, so it cannot be written raw")
    entries, free, end = [], [], 0
    for i in range(4):
        e = sector0[446 + 16 * i: 446 + 16 * (i + 1)]
        ptype, start, size = e[4], int.from_bytes(e[8:12], "little"), int.from_bytes(e[12:16], "little")
        if ptype == 0xEE:
            raise Rejected("ISO carries a GPT protective partition; a raw write would leave its backup GPT header "
                           "unrelocated. Use --layout ventoy for this image")
        if ptype == 0 and size == 0:
            free.append(i)
        else:
            entries.append({"slot": i, "type": ptype, "start": start, "size": size})
            end = max(end, start + size)
    if not free:
        raise Rejected("the ISO's MBR has no free primary slot for a data partition")
    if end * 512 > iso_bytes + 512 * 2048:
        raise Rejected("ISO partition entries extend past the end of the ISO file; image looks truncated or odd")
    return {"free_slot": free[0], "iso_end_sector": max(end, -(-iso_bytes // 512)), "entries": entries}


def compute_layout_raw(total_bytes, iso_end_sector, min_data=MIN_DATA):
    """Pure. The ISO occupies sectors [0, iso_end_sector); VICTORDATA starts at the next MiB boundary and runs to the end
    of the disk minus a spare. All numbers derive from the real capacity; nothing is looked up by marketed size."""
    if total_bytes <= 0 or iso_end_sector <= 0:
        raise Rejected("capacity and ISO size must be positive")
    start = align_up(iso_end_sector * 512)
    spare = align_up(max(MIN_SPARE, int(total_bytes * SPARE_FRACTION)))
    data = align_down(align_down(total_bytes) - start - spare)
    data = min(data, align_down(2**32 * 512 - start))      # an MBR entry addresses at most 2 TiB (32-bit sector numbers)
    if data < min_data:
        raise Rejected(f"Layout does not fit: drive has {total_bytes} B ({total_bytes/GiB:.2f} GiB); the raw ISO ends at "
                       f"{start/GiB:.2f} GiB and a {spare/GiB:.2f} GiB spare leaves only {max(data,0)/GiB:.2f} GiB for data "
                       f"(minimum {min_data/GiB:.0f} GiB).")
    layout = {"kind": "raw", "total_bytes": total_bytes, "iso_end_sector": iso_end_sector,
              "data_start_bytes": start, "data_start_sector": start // 512, "data_partition_bytes": data,
              "data_sectors": data // 512, "spare_unallocated_bytes": total_bytes - start - data, "alignment_bytes": MiB}
    assert start + data <= total_bytes, "internal error: layout exceeds device"
    return layout


def fmt_layout_raw(l, iso_bytes):
    g = lambda b: f"{b/GiB:8.2f} GiB"
    return "\n".join([
        f"  device capacity (real)    : {l['total_bytes']:>16,d} B  ({l['total_bytes']/GiB:.2f} GiB)",
        f"  ISO written raw (measured): {iso_bytes:>16,d} B  (hybrid: BIOS+UEFI boot records come from the ISO itself)",
        f"  VICTORDATA (ext4, persist): {g(l['data_partition_bytes'])}  starting at sector {l['data_start_sector']}",
        f"  unallocated spare         : {g(l['spare_unallocated_bytes'])}",
        "  NOTE: the ISO's own partitions are read-only media; updating the OS means re-running write (VICTORDATA is lost",
        "        unless you back it up first: raw mode rewrites the whole drive).",
    ])


# ---------------------------------------------------------------- device inspection
def sh(cmd, check=True):
    p = subprocess.run(cmd, capture_output=True, text=True)
    if check and p.returncode:
        raise SystemExit(f"{' '.join(cmd)}: {p.stderr.strip()}")
    return p


def lsblk_disks():
    js = json.loads(sh(["lsblk", "-J", "-b", "-O"]).stdout)
    return js["blockdevices"]


def real_size_bytes(dev):
    """Authoritative capacity: kernel's 512-byte sector count (cross-checked with lsblk)."""
    name = os.path.basename(os.path.realpath(dev))
    with open(f"/sys/class/block/{name}/size") as f:
        sectors = int(f.read())
    return sectors * 512


def identity(disk):
    """Stable identity string used in the confirmation token."""
    model = (disk.get("model") or "unknown-model").strip().replace(" ", "_")
    serial = (disk.get("serial") or "no-serial").strip()
    return f"{model}:{serial}:{int(disk['size'])}"


def mounts_of(disk):
    m = []
    def walk(n):
        m.extend(x for x in (n.get("mountpoints") or []) if x)
        for c in n.get("children") or []:
            walk(c)
    walk(disk)
    return m


SYSTEM_MOUNT_PREFIXES = ("/boot", "/usr", "/var", "/etc", "/opt", "/home", "/srv", "/nix", "/snap", "/root", "/proc", "/sys")
ALLOW_NON_USB = False   # --allow-non-usb: explicit override for internal/virtual disks


def protected_reasons(disk, paths):
    """Why this disk must NOT be a target: system mounts, backs a required path, not a plain disk, not USB/removable."""
    reasons = []
    tran = (disk.get("tran") or "").lower()
    if not ALLOW_NON_USB and not (ALLOW_LOOP and disk.get("type") == "loop") and tran != "usb" and not disk.get("rm"):
        reasons.append(f"not a USB/removable device (transport={tran or 'unknown'}); use --allow-non-usb only if you are sure")
    for mp in mounts_of(disk):
        if mp == "/" or mp == "[SWAP]" or mp.startswith(SYSTEM_MOUNT_PREFIXES):
            reasons.append(f"has system mount {mp}")
    if disk.get("type") != "disk" and not (ALLOW_LOOP and disk.get("type") == "loop"):
        reasons.append("not a whole disk")
    mps = mounts_of(disk)
    if "/" in mps or "/boot" in mps or "/boot/efi" in mps or "[SWAP]" in mps:
        reasons.append("holds the running system")
    for p in paths:
        rp = os.path.realpath(p)
        for mp in mps:
            if mp != "/" and (rp == mp or rp.startswith(mp.rstrip("/") + "/")):
                reasons.append(f"backs required path {p}")
    # a device-mapper/loop/virtio root can hide behind another name: check by major:minor of / too
    try:
        st = os.stat("/")
        root_dev = os.makedev(os.major(st.st_dev), os.minor(st.st_dev))
        def has(n):
            if n.get("maj:min") == f"{os.major(root_dev)}:{os.minor(root_dev)}":
                return True
            return any(has(c) for c in n.get("children") or [])
        if has(disk):
            reasons.append("backs the root filesystem")
    except OSError:
        pass
    return sorted(set(reasons))


def cmd_list(a):
    paths = [os.getcwd(), os.path.dirname(os.path.abspath(a.iso)) if a.iso else "."]
    rows = []
    for d in lsblk_disks():
        if d.get("type") != "disk" or d["name"].startswith(("zram", "loop", "ram")):
            continue
        why = protected_reasons(d, paths)
        rows.append({"path": d["path"], "model": (d.get("model") or "").strip(), "serial": d.get("serial"),
                     "bytes": int(d["size"]), "transport": d.get("tran"), "removable": bool(d.get("rm")),
                     "mountpoints": mounts_of(d), "identity": identity(d),
                     "eligible": not why, "excluded_because": why})
    if a.json:
        print(json.dumps(rows, indent=2))
    else:
        for r in rows:
            flag = "ELIGIBLE " if r["eligible"] else "EXCLUDED "
            print(f"{flag}{r['path']:<12} {r['bytes']/GiB:8.2f} GiB  tran={r['transport']} rm={r['removable']} "
                  f"model={r['model']!r} serial={r['serial']!r} mounts={r['mountpoints']}"
                  + ("" if r["eligible"] else f"  <- {', '.join(r['excluded_because'])}"))
        print("Eligibility is NOT a selection: you must name the exact device and confirm the erase token.")


def find_disk(dev):
    rp = os.path.realpath(dev)
    for d in lsblk_disks():
        if os.path.realpath(d["path"]) == rp:
            return d
    raise SystemExit(f"{dev} is not a whole disk known to lsblk")


def cmd_plan(a):
    iso_bytes = os.path.getsize(a.iso)
    if a.device:
        d = find_disk(a.device)
        total = real_size_bytes(a.device)
        if total != int(d["size"]):
            raise SystemExit(f"capacity mismatch sysfs={total} lsblk={d['size']}; refusing")
        ident = identity(d)
    else:
        total, ident = a.size_bytes, f"virtual:{a.size_bytes}"
    kind = getattr(a, "layout", "ventoy")
    try:
        if kind == "raw":
            with open(a.iso, "rb") as f:
                mbr = inspect_iso_mbr(f.read(512), iso_bytes)
            l = compute_layout_raw(total, mbr["iso_end_sector"], a.min_data_gib * GiB)
            l["mbr_free_slot"] = mbr["free_slot"]
        else:
            l = compute_layout(total, iso_bytes, a.min_data_gib * GiB)
    except Rejected as e:
        print(f"REJECTED: {e}", file=sys.stderr)
        raise SystemExit(3)
    if a.json:
        print(json.dumps({"identity": ident, "layout": l}, indent=2))
    else:
        print(f"Target identity: {ident}\nProposed layout ({kind}):\n{fmt_layout_raw(l, iso_bytes) if kind == 'raw' else fmt_layout(l)}")
        print(f"\nTo write, pass exactly:  --confirm 'ERASE {ident}'")
    return l, ident


# ---------------------------------------------------------------- writing
def part_path(dev, n):
    return f"{dev}p{n}" if re.search(r"\d$", dev) else f"{dev}{n}"


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while c := f.read(8 * MiB):
            h.update(c)
    return h.hexdigest()


REQUIRED_TOOLS = ["lsblk", "sfdisk", "partprobe", "udevadm", "mkfs.ext4", "mount", "umount", "cp", "sync", "bash", "blkid"]


RAW_TOOLS = ["lsblk", "dd", "blockdev", "partprobe", "udevadm", "mkfs.ext4", "sync", "blkid", "wipefs"]


def require_tools(kind="ventoy"):
    os.environ["PATH"] = os.environ.get("PATH", "") + ":/usr/sbin:/sbin:/usr/local/sbin"
    missing = [t for t in (RAW_TOOLS if kind == "raw" else REQUIRED_TOOLS) if not shutil.which(t)]
    if missing:
        raise SystemExit("missing required tools (checked BEFORE any write): " + ", ".join(missing) +
                         "  -> apt install util-linux parted e2fsprogs udev")


def cmd_write(a):
    if os.geteuid() != 0:
        raise SystemExit("write needs root")
    kind = a.layout
    require_tools(kind)
    iso_sha_file = a.iso + ".sha256"
    want = None
    if os.path.exists(iso_sha_file):
        want = open(iso_sha_file).read().split()[0]
        if sha256(a.iso) != want:
            raise SystemExit("ISO does not match its .sha256 file; refusing")
    elif not a.allow_unverified_iso:
        raise SystemExit(f"{iso_sha_file} missing; pass --allow-unverified-iso to override")
    a.json = False
    l, ident = cmd_plan(argparse.Namespace(device=a.device, size_bytes=None, iso=a.iso, json=False,
                                           min_data_gib=MIN_DATA // GiB, layout=kind))
    token = f"ERASE {ident}"
    if a.confirm != token:
        raise SystemExit(f"confirmation mismatch. Required exactly: --confirm '{token}'")
    paths = [os.getcwd(), os.path.dirname(os.path.abspath(a.iso)), a.ventoy_dir]
    d = find_disk(a.device)                                   # re-check identity immediately before writing
    if identity(d) != ident:
        raise SystemExit("device identity changed since planning; aborting (nothing written)")
    why = protected_reasons(d, paths)
    if why:
        raise SystemExit(f"refusing: {', '.join(why)}")
    if kind == "raw":
        return write_raw(a, d, ident, l, want)
    vt = os.path.join(a.ventoy_dir, "Ventoy2Disk.sh")
    if not os.path.exists(vt):
        raise SystemExit(f"{vt} not found (download+verify Ventoy first: scripts/fetch-ventoy.sh)")
    dev = os.path.realpath(a.device)
    # unmount ONLY this disk's mounted filesystems
    for mp in sorted(mounts_of(d), key=len, reverse=True):
        sh(["umount", mp])
    def same_disk():
        n = find_disk(dev)
        if identity(n) != ident:
            raise SystemExit("device disappeared or was replaced mid-operation; stopping")
    same_disk()
    print(">> installing Ventoy (reserve %d MiB)" % l["ventoy_reserve_mib"])
    p = subprocess.run(["bash", vt, "-I", "-r", str(l["ventoy_reserve_mib"]), dev], input="y\ny\n", text=True)
    if p.returncode:
        raise SystemExit("Ventoy2Disk failed; drive state is undefined, re-run after investigating")
    sh(["partprobe", dev], check=False); sh(["udevadm", "settle"], check=False); time.sleep(2)
    same_disk()
    # data partition at start of the reserved area
    pt = json.loads(sh(["sfdisk", "-J", dev]).stdout)["partitiontable"]
    sector = 512
    last = max(p["start"] + p["size"] for p in pt["partitions"])
    start = align_up(last * sector) // sector
    size = l["data_partition_bytes"] // sector
    print(f">> creating ext4 data partition start={start} sectors size={size} sectors")
    typ = "0FC63DAF-8483-4772-8E79-3D69D8477DE4" if pt["label"] == "gpt" else "83"
    p = subprocess.run(["sfdisk", "--append", "--no-reread", dev], input=f"{start},{size},{typ}\n", text=True,
                       capture_output=True)
    if p.returncode:
        raise SystemExit(f"sfdisk failed: {p.stderr}")
    sh(["partprobe", dev], check=False); sh(["udevadm", "settle"], check=False); time.sleep(2)
    same_disk()
    dpart = part_path(dev, len(pt["partitions"]) + 1)
    sh(["mkfs.ext4", "-F", "-L", "VICTORDATA", "-m", "1", "-E", "lazy_itable_init=0", dpart])
    # copy ISO to the Ventoy partition and verify
    vpart = part_path(dev, 1)
    mp = f"/run/victor-provision-{os.getpid()}"
    os.makedirs(mp)
    m = sh(["mount", "-t", "exfat", vpart, mp], check=False)
    if m.returncode:   # kernels without the exfat driver: use the FUSE implementation if installed
        if shutil.which("mount.exfat-fuse"):
            sh(["mount", "-t", "exfat-fuse", vpart, mp])
        else:
            raise SystemExit("cannot mount the exFAT Ventoy partition (no kernel exfat driver; install exfat-fuse). "
                             "Ventoy and the data partition were written; copy the ISO manually and verify its sha256.")
    try:
        dst = os.path.join(mp, os.path.basename(a.iso))
        sh(["cp", a.iso, dst]); sh(["sync"])
        got = sha256(dst)
        ref = want or sha256(a.iso)
        if got != ref:
            raise SystemExit("copied ISO checksum MISMATCH")
        print(">> ISO copied and sha256 verified:", got)
    finally:
        sh(["sync"], check=False); sh(["umount", mp], check=False); os.rmdir(mp)
    print(subprocess.run(["sfdisk", "-l", dev], capture_output=True, text=True).stdout)
    sh(["sync"])
    print("DONE: drive prepared. Safe to remove after the OS reports it unmounted.")


def write_raw(a, d, ident, l, want):
    """Raw hybrid-ISO write. Caller already verified: root, tools, ISO checksum, confirmation token, identity, protections."""
    dev = os.path.realpath(a.device)
    iso_bytes = os.path.getsize(a.iso)
    for mp in sorted(mounts_of(d), key=len, reverse=True):          # unmount ONLY this disk's filesystems
        sh(["umount", mp])

    def same_disk():
        if identity(find_disk(dev)) != ident:
            raise SystemExit("device disappeared or was replaced mid-operation; stopping")
    same_disk()
    with open(a.iso, "rb") as f:
        sector0 = f.read(512)
    mbr = inspect_iso_mbr(sector0, iso_bytes)                         # re-parse: the file could have changed since plan
    print(">> writing hybrid ISO raw to", dev)
    sh(["wipefs", "-a", dev], check=False)
    p = subprocess.run(["dd", f"if={a.iso}", f"of={dev}", "bs=4M", "conv=fsync", "oflag=direct", "status=none"])
    if p.returncode:
        raise SystemExit("dd failed; drive state is undefined")
    sh(["sync"]); sh(["blockdev", "--flushbufs", dev], check=False)
    # read back from the device itself (O_DIRECT: not the page cache) and compare with the ISO's checksum
    h = hashlib.sha256()
    rb = subprocess.Popen(["dd", f"if={dev}", "bs=4M", "iflag=direct,count_bytes", f"count={iso_bytes}", "status=none"],
                          stdout=subprocess.PIPE)
    while c := rb.stdout.read(8 * MiB):
        h.update(c)
    rb.wait()
    ref = want or sha256(a.iso)
    if rb.returncode or h.hexdigest() != ref:
        raise SystemExit("ISO read back from the device does NOT match its checksum; do not use this drive")
    print(">> ISO read back from the device matches", ref)
    same_disk()
    # add the data partition: write ONE 16-byte MBR entry into the free slot, leaving boot code and the ISO's entries intact
    slot, start, size = mbr["free_slot"], l["data_start_sector"], l["data_sectors"]
    entry = bytes([0, 0xFE, 0xFF, 0xFF, 0x83, 0xFE, 0xFF, 0xFF]) + start.to_bytes(4, "little") + size.to_bytes(4, "little")
    if start > 0xFFFFFFFF or size > 0xFFFFFFFF:
        raise SystemExit("drive is larger than an MBR can address (2 TiB); use --layout ventoy (GPT)")
    with open(dev, "r+b") as f:
        cur = f.read(512)
        off = 446 + 16 * slot
        if cur != sector0 or cur[off:off + 16] != bytes(16):
            raise SystemExit("MBR on the device is not what was just written; stopping before partitioning")
        f.seek(off); f.write(entry); f.flush(); os.fsync(f.fileno())
    # ask the kernel itself to re-read the table (libparted's partprobe can fail on the ISO's leftover Apple driver descriptor)
    sh(["blockdev", "--rereadpt", dev], check=False)
    sh(["partprobe", dev], check=False); sh(["udevadm", "settle"], check=False); time.sleep(2)
    same_disk()
    dpart = part_path(dev, slot + 1)
    if not os.path.exists(dpart):
        raise SystemExit(f"data partition node {dpart} did not appear (kernel did not re-read the table)")
    sh(["mkfs.ext4", "-F", "-L", "VICTORDATA", "-m", "1", "-E", "lazy_itable_init=0", dpart])
    # the ISO's boot sector and original partition entries must be untouched by the partition add
    with open(dev, "rb") as f:
        after = f.read(512)
    keep = [i for i in range(512) if not (446 + 16 * slot <= i < 446 + 16 * (slot + 1))]
    if any(after[i] != sector0[i] for i in keep):
        raise SystemExit("MBR changed outside the new entry; do not use this drive")
    sh(["sync"])
    print(subprocess.run(["lsblk", "-o", "NAME,SIZE,FSTYPE,LABEL,UUID", dev], capture_output=True, text=True).stdout)
    print("DONE: hybrid ISO written raw and verified by read-back; VICTORDATA created. Safe to remove after the OS reports it unmounted.")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = ap.add_subparsers(dest="cmd", required=True)
    l = sp.add_parser("list"); l.add_argument("--iso"); l.add_argument("--json", action="store_true")
    l.set_defaults(f=cmd_list)
    p = sp.add_parser("plan"); p.add_argument("--device"); p.add_argument("--size-bytes", type=int)
    p.add_argument("--iso", required=True); p.add_argument("--json", action="store_true")
    p.add_argument("--min-data-gib", type=int, default=MIN_DATA // GiB)
    p.add_argument("--layout", choices=["ventoy", "raw"], default="ventoy")
    p.set_defaults(f=lambda a: cmd_plan(a) and None)
    w = sp.add_parser("write"); w.add_argument("--device", required=True); w.add_argument("--iso", required=True)
    w.add_argument("--confirm", required=True); w.add_argument("--ventoy-dir", default="/opt/ventoy")
    w.add_argument("--allow-unverified-iso", action="store_true")
    w.add_argument("--layout", choices=["ventoy", "raw"], default="ventoy",
                   help="ventoy (default, multi-ISO) or raw (hybrid ISO written byte-for-byte + VICTORDATA)")
    w.add_argument("--virtual-test", action="store_true", help="permit /dev/loop* (disposable test images only)")
    w.add_argument("--allow-non-usb", action="store_true", help="permit a non-USB, non-removable disk (dangerous)")
    w.set_defaults(f=cmd_write)
    a = ap.parse_args()
    global ALLOW_LOOP
    ALLOW_LOOP = bool(getattr(a, "virtual_test", False))
    global ALLOW_NON_USB
    ALLOW_NON_USB = bool(getattr(a, "allow_non_usb", False))
    if a.cmd == "plan" and bool(a.device) == bool(a.size_bytes):
        ap.error("plan needs exactly one of --device / --size-bytes")
    a.f(a)


if __name__ == "__main__":
    main()
