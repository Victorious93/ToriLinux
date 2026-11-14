"""Persistent-data discovery. Identity is ALWAYS the filesystem UUID, never /dev/sdX.

States (storage.json "state"):
  mounted-rw | mounted-ro | full | needs-selection | missing | damaged | session
"""
import json, os, shutil
from .common import *


def parse_lsblk(js):
    """Flatten `lsblk -J -b -O` output into a list of partition dicts."""
    out = []

    def walk(node, parent):
        n = dict(node)
        n["_parent"] = parent
        out.append(n)
        for c in node.get("children", []) or []:
            walk(c, n)

    for d in js.get("blockdevices", []):
        walk(d, None)
    return out


def candidates(devs):
    """Partitions that look like VictorLinux data: ext4 + label VICTORDATA or TORIDATA + UUID."""
    res = []
    for d in devs:
        if d.get("fstype") == "ext4" and d.get("label") in DATA_LABELS and d.get("uuid"):
            disk = d["_parent"] or {}
            res.append({
                "uuid": d["uuid"], "label": d.get("label"), "path": d.get("path") or f"/dev/{d.get('name')}",
                "size": int(d.get("size") or 0),
                "model": (disk.get("model") or "").strip(), "serial": (disk.get("serial") or "").strip(),
                "transport": disk.get("tran") or "", "removable": bool(disk.get("rm")),
                "mountpoints": [m for m in (d.get("mountpoints") or []) if m],
            })
    return res


def decide(cands, selected_uuid=None):
    """Pure policy: returns (action, uuid|None, message)."""
    if selected_uuid:
        for c in cands:
            if c["uuid"] == selected_uuid:
                return "mount", selected_uuid, "selected partition"
        return "missing", None, f"The selected data partition (UUID {selected_uuid}) is not attached."
    if not cands:
        return "missing", None, ("No VICTORDATA/TORIDATA partition found. Plug in the prepared VictorLinux USB, or start a "
                                 "temporary session (nothing is saved).")
    if len(cands) > 1:
        return "needs-selection", None, (f"{len(cands)} VICTORDATA/TORIDATA partitions are attached; choose one explicitly "
                                         "(refusing to guess).")
    return "mount", cands[0]["uuid"], "single VICTORDATA/TORIDATA partition found"


def classify_mounted(mountpoint, opts, free_bytes, writable):
    """Pure policy for the health of a mounted data partition."""
    ro = ("ro" in opts.split(",")) or not writable
    if ro:
        return "mounted-ro", ("The data partition is read-only (damaged filesystem, write-protected media or "
                              "forced ro mount). Existing data can be read; nothing can be saved. Run "
                              "'victor-ctl storage check' then 'victor-ctl storage repair'.")
    if free_bytes < MIN_FREE_BYTES:
        return "full", (f"Only {human(free_bytes)} free on the data partition. Delete models/outputs "
                        "(Control Center > Models) or use a larger drive; imports/logs are blocked.")
    return "mounted-rw", "ok"


def lsblk():
    p = run(["lsblk", "-J", "-b", "-O"], timeout=20)
    if p.returncode != 0:
        raise VictorError(f"lsblk failed: {p.stderr.strip()}")
    return parse_lsblk(json.loads(p.stdout))


def enrich(devs):
    """lsblk gets fstype/label/uuid from the udev database; if that is empty (early boot, chroots) probe the
    block device directly so discovery never depends on udev timing."""
    for d in devs:
        if d.get("fstype") or d.get("children") or d.get("type") not in ("part", "disk", "loop"):
            continue
        path = d.get("path") or f"/dev/{d.get('name')}"
        p = run(["blkid", "-p", "-o", "export", path], timeout=15)
        if p.returncode == 0:
            kv = dict(l.split("=", 1) for l in p.stdout.splitlines() if "=" in l)
            d["fstype"] = kv.get("TYPE"); d["label"] = kv.get("LABEL") ; d["uuid"] = kv.get("UUID")
    return devs


def list_candidates():
    return candidates(enrich(lsblk()))


def read_state():
    return read_json(STATE_FILE, {"state": "missing", "message": "storage not initialised yet"})


def probe_active():
    """Measure the active root without modifying anything persistent beyond a tiny temp file."""
    if not os.path.isdir(ROOT_LINK):
        return None
    root = os.path.realpath(ROOT_LINK)
    st = os.statvfs(root)
    free = st.f_bavail * st.f_frsize
    total = st.f_blocks * st.f_frsize
    opts = ""
    try:
        with open("/proc/self/mountinfo") as f:
            best = ""
            for line in f:
                p = line.split()
                mp = p[4]
                if (root == mp or root.startswith(mp.rstrip("/") + "/")) and len(mp) >= len(best):
                    best, opts = mp, p[5]
    except OSError:
        pass
    writable = True
    try:
        probe = os.path.join(root, f".victor-probe-{os.getpid()}")
        with open(probe, "w") as f:
            f.write("x")
        os.unlink(probe)
    except OSError:
        writable = False
    return {"root": root, "free": free, "total": total, "options": opts, "writable": writable}


def status():
    s = dict(read_state())
    s["candidates"] = []
    try:
        s["candidates"] = list_candidates()
    except Exception as e:  # lsblk missing in unit tests etc.
        s["candidates_error"] = str(e)
    m = probe_active()
    if m:
        s["usage"] = m
        if s.get("state") in ("mounted-rw", "mounted-ro", "full"):
            s["state"], s["message"] = classify_mounted(m["root"], m["options"], m["free"], m["writable"])
    return s


def ensure_layout(root):
    for d in SUBDIRS:
        os.makedirs(os.path.join(root, d), exist_ok=True)
    os.chmod(os.path.join(root, "secrets"), 0o700)
    ident = os.path.join(root, "victor-data.json")
    if not os.path.exists(ident):
        import uuid
        write_json_atomic(ident, {"format": 1, "device_id": str(uuid.uuid4()), "created_by": "VictorLinux"}, 0o644)
