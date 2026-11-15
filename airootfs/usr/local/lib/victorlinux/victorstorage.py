"""Shared storage logic for VictorLinux: volume discovery, stable identity, destinations, size limits.

Used by victor-priv (root), the Control Center and the `victor-storage` CLI so that there is exactly one
implementation. Standard library only. Nothing here formats, partitions, relabels or changes ownership
recursively; the only filesystem change it ever makes is creating the requested folders.

Concepts
  volume       a filesystem on a block device (partition, whole-disk filesystem, LUKS/LVM mapping)
  destination  (role, volume identity, subfolder). Roles say what is written there.
  state        VictorLinux application state (settings, chats, API-key hash, logs). Needs POSIX permissions,
               so it can live only on a POSIX filesystem. Models/documents/etc. can live on exFAT/NTFS/FAT32.
"""
import json
import os
import re
import subprocess

GIB = 1 << 30
FAT32_MAX_FILE = 4 * GIB - 1
VOL_BASE = os.environ.get("VICTOR_VOL_BASE", "/mnt/victor-vol")
ROLES = ("models", "downloads", "documents", "outputs", "backups")       # file destinations; `state` is separate
MULTI_ROLES = ("models",)                                                 # several model libraries may be active at once
UUID_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z\-]{3,63}$")
CONFIG_VERSION = 1

# fstype -> capabilities. `posix` = Unix ownership/permissions/locking (needed for application state).
FILESYSTEMS = {
    "ext4": {"posix": True}, "ext3": {"posix": True}, "ext2": {"posix": True}, "btrfs": {"posix": True},
    "xfs": {"posix": True}, "f2fs": {"posix": True},
    "exfat": {"posix": False, "note": "no Unix permissions or symlinks; fine for models, documents, downloads and backups"},
    "ntfs": {"posix": False, "note": "mounted with the kernel ntfs3 driver; read-only if Windows left it hibernated or unclean"},
    "vfat": {"posix": False, "max_file": FAT32_MAX_FILE,
             "note": "FAT32 cannot hold any single file of 4 GiB or more, however large the drive is"},
}
LOCKED_TYPES = ("crypto_LUKS", "BitLocker")
IGNORED_TYPES = ("swap", "iso9660", "squashfs", "LVM2_member", "linux_raid_member", "zfs_member")
STORAGE_ROLE_NAMES = {"state": "Application state", "models": "Models", "downloads": "Downloads", "documents": "Documents",
                      "outputs": "Outputs", "backups": "Backups"}


class StorageError(Exception):
    """A storage request cannot be honoured; the message is safe to show to the owner."""


# ------------------------------------------------------------------------------------ safe paths
def validate_subpath(sub):
    """Normalise a folder name relative to a volume root. Rejects absolute paths, '..', NUL and control characters.
    Spaces and Unicode are fine; the value is never passed through a shell."""
    if sub is None:
        return ""
    if not isinstance(sub, str):
        raise StorageError("Folder must be text.")
    if "\0" in sub or any(ord(c) < 32 for c in sub):
        raise StorageError("Folder contains control characters.")
    if len(sub.encode("utf-8")) > 1024:
        raise StorageError("Folder path is too long.")
    parts = [p for p in sub.replace("\\", "/").split("/") if p not in ("", ".")]
    if sub.replace("\\", "/").startswith("/"):
        raise StorageError("Folder must be relative to the drive (no leading '/').")
    if any(p == ".." for p in parts):
        raise StorageError("Folder may not contain '..'.")
    if any(len(p.encode("utf-8")) > 255 for p in parts):
        raise StorageError("A folder name is too long.")
    return "/".join(parts)


def contained_path(root, sub):
    """root/sub with symlinks resolved, guaranteed to stay inside root. Raises StorageError otherwise."""
    sub = validate_subpath(sub)
    real_root = os.path.realpath(root)
    p = os.path.realpath(os.path.join(real_root, sub)) if sub else real_root
    if p != real_root and not p.startswith(real_root + os.sep):
        raise StorageError("That folder resolves outside the selected drive (symbolic link).")
    return p


# ------------------------------------------------------------------------------------ discovery
def _run(cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as e:
        return subprocess.CompletedProcess(cmd, 127, "", str(e))


def lsblk_tree():
    cols = "NAME,PATH,TYPE,UUID,PARTUUID,LABEL,FSTYPE,SIZE,MOUNTPOINTS,RM,ROTA,HOTPLUG,TRAN,MODEL,SERIAL,PKNAME,RO,MAJ:MIN"
    r = _run(["lsblk", "-J", "-b", "-o", cols])
    if r.returncode != 0:
        raise StorageError("lsblk failed: " + (r.stderr.strip() or "unavailable"))
    devs = json.loads(r.stdout)["blockdevices"]
    for d in flatten(devs):
        if d["type"] in ("part", "disk", "loop", "crypt", "lvm") and not d.get("fstype"):
            b = _run(["blkid", "-p", "-o", "export", d["path"]])       # -p: probe the device itself, never the stale blkid cache (device names get reused)
            if b.returncode == 0:
                kv = {k: re.sub(r"\\(.)", r"\1", v) for k, v in (l.split("=", 1) for l in b.stdout.splitlines() if "=" in l)}   # export format escapes spaces
                d["fstype"], d["uuid"], d["label"] = kv.get("TYPE"), kv.get("UUID"), kv.get("LABEL")
                d["partuuid"] = d.get("partuuid") or kv.get("PARTUUID")
    return devs


def flatten(devs, parent=None):
    for d in devs:
        d["_parent"] = parent
        yield d
        yield from flatten(d.get("children") or [], d)


def mount_table(path="/proc/self/mountinfo"):
    """{maj:min: [ {mountpoint, fstype, ro, options} ]} from the kernel's own table (no shell, no udev)."""
    out = {}
    try:
        lines = open(path).read().splitlines()
    except OSError:
        return out
    for l in lines:
        f = l.split(" ")
        try:
            sep = f.index("-")
        except ValueError:
            continue
        mp = _unescape(f[4])
        opts = f[5].split(",")
        out.setdefault(f[2], []).append({"mountpoint": mp, "fstype": f[sep + 1], "ro": "ro" in opts, "options": ",".join(opts)})
    return out


def _unescape(s):
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), s)


def volumes_from_tree(devs, mounts, boot_devices=frozenset(), free=None):
    """Convert an lsblk tree into volume records. Pure given its inputs (testable)."""
    free = free or _statvfs
    vols = []
    for d in flatten(devs):
        fs = d.get("fstype")
        if d["type"] not in ("part", "disk", "loop", "crypt", "lvm") or not fs or fs in IGNORED_TYPES:
            continue
        par = d.get("_parent")
        disk = par
        while disk and disk.get("_parent"):
            disk = disk["_parent"]
        disk = disk or d
        if d["type"] == "disk" and d.get("children"):
            continue                                                    # a partitioned disk's own signature is the table
        p = d
        is_boot = False
        while p:
            if os.path.realpath(p["path"]) in boot_devices:
                is_boot = True
            p = p.get("_parent")
        mts = [m for m in (mounts.get(d.get("maj:min") or "") or [])]
        rw_mount = next((m for m in mts if not m["ro"]), None)
        mt = rw_mount or (mts[0] if mts else None)
        locked = fs in LOCKED_TYPES
        cap = FILESYSTEMS.get(fs, {})
        total = int(d.get("size") or 0)
        used_free = free(mt["mountpoint"]) if mt else None
        tran = (disk.get("tran") or "").lower()
        removable = bool(d.get("rm") or disk.get("rm") or disk.get("hotplug"))
        name = disk.get("name") or ""
        if name.startswith("mmcblk"):
            kind = "sd-card"
        elif tran == "usb":
            kind = "usb-" + ("hdd" if str(disk.get("rota")) in ("1", "True", "true") else "storage")
        elif tran == "nvme" or name.startswith("nvme"):
            kind = "internal-nvme"
        elif tran in ("sata", "ata"):
            kind = "internal-hdd" if str(disk.get("rota")) in ("1", "True", "true") else "internal-ssd"
        else:
            kind = tran or d["type"]
        vols.append({
            "path": d["path"], "uuid": d.get("uuid"), "partuuid": d.get("partuuid"), "label": d.get("label"), "fstype": fs,
            "size": total, "kind": kind, "removable": removable, "transport": tran or None,
            "model": (disk.get("model") or "").strip(), "serial": (disk.get("serial") or "").strip() or None,
            "disk": disk.get("path"), "majmin": d.get("maj:min"),
            "mountpoints": [m["mountpoint"] for m in mts], "mounted": bool(mts), "mountpoint": mt["mountpoint"] if mt else None,
            "mounted_ro": bool(mts) and rw_mount is None, "device_ro": bool(d.get("ro")),
            "free": used_free[0] if used_free else None, "used": used_free[1] if used_free else None,
            "locked": locked, "boot_medium": is_boot,
            "posix": bool(cap.get("posix")), "supported": fs in FILESYSTEMS,
            "max_file": cap.get("max_file"), "note": cap.get("note") or ("locked: unlock it first (VictorLinux does not unlock drives itself yet)" if locked else None),
            "state_ok": bool(cap.get("posix")) and not locked,
        })
    flag_duplicates(vols)
    return vols


def _statvfs(path):
    try:
        s = os.statvfs(path)
        return s.f_bavail * s.f_frsize, (s.f_blocks - s.f_bfree) * s.f_frsize
    except OSError:
        return None


def flag_duplicates(vols):
    """Mark volumes whose UUID or label appears more than once, so nothing is ever picked silently."""
    for key in ("uuid", "label"):
        seen = {}
        for v in vols:
            if v.get(key):
                seen.setdefault(str(v[key]).lower(), []).append(v)
        for group in seen.values():
            for v in group:
                v["duplicate_" + key] = len(group) > 1


def identity(v):
    """What is stored to find this volume again. Never the device path (it changes between boots)."""
    return {k: v.get(k) for k in ("uuid", "partuuid", "label", "fstype", "serial", "model") if v.get(k)}


def resolve_volume(volumes, ref):
    """Find the one volume matching a stored identity. Returns (volume|None, status, message).
    status: ok | missing | ambiguous"""
    uid = (ref.get("uuid") or "").lower()
    if not uid:
        return None, "missing", "No filesystem identity stored."
    hits = [v for v in volumes if (v.get("uuid") or "").lower() == uid]
    if len(hits) > 1 and ref.get("partuuid"):
        narrowed = [v for v in hits if (v.get("partuuid") or "").lower() == ref["partuuid"].lower()]
        if narrowed:
            hits = narrowed
    if len(hits) > 1 and ref.get("serial"):
        narrowed = [v for v in hits if v.get("serial") == ref["serial"]]
        if narrowed:
            hits = narrowed
    if not hits:
        return None, "missing", f"The drive with filesystem {ref.get('uuid')} ({ref.get('label') or 'no label'}) is not connected."
    if len(hits) > 1:
        return None, "ambiguous", (f"{len(hits)} drives carry the same filesystem identity {ref.get('uuid')} "
                                   f"({', '.join(h['path'] for h in hits)}). Disconnect one, or give one a new UUID, then retry.")
    return hits[0], "ok", ""


def select_candidates(volumes, selector):
    """Resolve a user-typed reference (UUID, LABEL, or /dev path) to exactly one volume; list ambiguities."""
    s = (selector or "").strip()
    if not s:
        raise StorageError("No drive given.")
    lo = s.lower()
    hits = [v for v in volumes if (v.get("uuid") or "").lower() == lo]
    if not hits:
        hits = [v for v in volumes if v["path"] == s]
    if not hits:
        hits = [v for v in volumes if (v.get("label") or "").lower() == lo]
    if not hits:
        raise StorageError(f"No connected volume matches '{s}'. Run `victor-storage list`.")
    if len(hits) > 1:
        raise StorageError(f"'{s}' matches {len(hits)} volumes ({', '.join(h['path'] for h in hits)}); use the device path.")
    return hits[0]


# ------------------------------------------------------------------------------------ limits
def check_fits(nbytes, fstype=None, free=None):
    """Raise StorageError BEFORE a large copy/download starts if it cannot succeed."""
    if nbytes < 0:
        raise StorageError("Invalid size.")
    cap = FILESYSTEMS.get(fstype or "", {})
    mx = cap.get("max_file")
    if mx is not None and nbytes > mx:
        raise StorageError(
            f"This file is {nbytes / GIB:.2f} GiB, but a FAT32 volume cannot hold any single file larger than 4 GiB minus one byte, "
            "no matter how big the drive is. Choose an ext4, exFAT or NTFS destination, or split the file "
            "(models can be saved as split GGUF sets).")
    if free is not None and nbytes > free:
        raise StorageError(f"Not enough free space: need {nbytes / GIB:.2f} GiB, {free / GIB:.2f} GiB free.")
    return True


# ------------------------------------------------------------------------------------ configuration
def empty_config():
    return {"version": CONFIG_VERSION, "destinations": {}}


def load_config(path):
    try:
        with open(path) as f:
            c = json.load(f)
    except (OSError, ValueError):
        return empty_config()
    if not isinstance(c, dict) or not isinstance(c.get("destinations"), dict):
        return empty_config()
    return c


def set_destination(cfg, role, volume, subpath, readonly_ok=False):
    if role not in ROLES:
        raise StorageError(f"Unknown destination '{role}'. Choose one of: {', '.join(ROLES)}.")
    if volume.get("locked"):
        raise StorageError("That volume is locked (encrypted). Unlock it first.")
    if not volume.get("supported"):
        raise StorageError(f"Filesystem '{volume.get('fstype')}' is not supported for file storage.")
    if volume.get("boot_medium"):
        raise StorageError("That is the live boot medium's own boot partition.")
    if volume.get("duplicate_uuid"):
        raise StorageError("Another connected drive has the same UUID; refusing to choose between them.")
    sub = validate_subpath(subpath)
    entry = {"ref": identity(volume), "subpath": sub, "readonly_ok": bool(readonly_ok)}
    if role in MULTI_ROLES:
        lst = [e for e in cfg["destinations"].get(role, []) if not (e["ref"].get("uuid") == entry["ref"].get("uuid") and e["subpath"] == sub)]
        lst.append(entry)
        cfg["destinations"][role] = lst
    else:
        cfg["destinations"][role] = [entry]
    return entry


def clear_destination(cfg, role, uuid=None, subpath=None):
    if role not in ROLES:
        raise StorageError(f"Unknown destination '{role}'.")
    cur = cfg["destinations"].get(role, [])
    if uuid is None:
        cfg["destinations"].pop(role, None)
    else:
        cfg["destinations"][role] = [e for e in cur if not (e["ref"].get("uuid", "").lower() == uuid.lower() and (subpath is None or e["subpath"] == subpath))]
        if not cfg["destinations"][role]:
            cfg["destinations"].pop(role)


def destination_status(cfg, volumes, state_mode="persistent", writable_needed=True):
    """Per-role resolved state. A destination is only 'active' when its drive is connected, unique, mounted at its real
    mount point and (for writes) read-write. It is NEVER resolved to a bare directory on the RAM disk."""
    out = {}
    for role in ROLES:
        rows = []
        for e in cfg.get("destinations", {}).get(role, []):
            v, st, msg = resolve_volume(volumes, e["ref"])
            row = {"role": role, "subpath": e["subpath"], "ref": e["ref"], "readonly_ok": e.get("readonly_ok", False),
                   "status": st, "message": msg, "path": None, "writable": False, "volume": v and v["path"],
                   "fstype": (v or {}).get("fstype") or e["ref"].get("fstype"), "free": None, "persistent": None}
            if v:
                row["persistent"] = True
                row["max_file"] = v.get("max_file")
                if v["locked"]:
                    row.update(status="locked", message="This drive is encrypted and locked.")
                elif not v["mounted"]:
                    row.update(status="unmounted", message="Connected but not mounted yet. It will be mounted when used or by `victor-storage apply`.")
                else:
                    mp = v["mountpoint"]
                    try:
                        p = contained_path(mp, e["subpath"])
                    except StorageError as ex:
                        row.update(status="error", message=str(ex))
                        p = None
                    if p is not None and os.path.isdir(p):
                        row.update(path=p, writable=not v["mounted_ro"] and not v["device_ro"] and os.access(p, os.W_OK), free=v["free"], status="active")
                        if not row["writable"]:
                            row["status"] = "readonly"
                            row["message"] = ("Mounted read-only: usable for reading (for example models), not for saving."
                                              if v["mounted_ro"] or v["device_ro"] else
                                              "The folder exists but this account may not write to it. Choose a different or new folder name; VictorLinux never changes ownership of existing files.")
                    elif p is not None:
                        row.update(status="missing-folder", message=f"Folder '{e['subpath'] or '/'}' does not exist on the drive.")
            rows.append(row)
        if rows:
            out[role] = rows
    return out


def usable_path(rows, need_write=True):
    """First row that can serve the request, or None. No fallback to any other location."""
    for r in rows or []:
        if r["status"] in ("active",) or (r["status"] == "readonly" and not need_write):
            return r["path"]
    return None


def summarize(cfg, volumes, state):
    """The one dictionary the CLI and GUI both render."""
    dest = destination_status(cfg, volumes)
    mode = (state or {}).get("mode", "unknown")
    return {
        "state": {"mode": mode, "message": (state or {}).get("message"), "data_root": (state or {}).get("data_root"),
                  "durable": mode == "persistent", "config_durable": mode == "persistent"},
        "destinations": dest,
        "problems": [r for rows in dest.values() for r in rows if r["status"] not in ("active",)],
    }
