"""Storage health: SMART verdicts and free-space warnings with concrete fixes. Read-only; never writes to a disk.

`smartctl -j` output is parsed here (it is collected by the privileged helper, because SMART needs root).
Standard library only; shared by both editions.
"""
import json
import os
import re
import shutil
import subprocess

GiB = 1024 ** 3
DEV_RE = re.compile(r"^/dev/(sd[a-z]{1,2}|nvme\d{1,2}n\d{1,2}|vd[a-z]{1,2}|mmcblk\d{1,2})$")
RANK = {"ok": 0, "unknown": 1, "warn": 2, "bad": 3}


def valid_disk(dev):
    return isinstance(dev, str) and bool(DEV_RE.match(dev))


def worst(levels):
    levels = list(levels)
    return max(levels, key=lambda x: RANK.get(x, 1)) if levels else "unknown"


def _attr(j, ident):
    for a in (j.get("ata_smart_attributes") or {}).get("table") or []:
        if a.get("id") == ident:
            raw = a.get("raw") or {}
            try:
                return int(raw.get("value", 0))
            except (TypeError, ValueError):
                return None
    return None


def parse_smart(j):
    """smartctl -j -H -A -i output -> verdict with findings. Unknown data never produces a false 'ok'."""
    dev = (j.get("device") or {}).get("name") or "?"
    out = {"device": dev, "model": j.get("model_name") or j.get("model_family") or "", "serial_known": bool(j.get("serial_number")),
           "status": "unknown", "passed": None, "temp_c": (j.get("temperature") or {}).get("current"),
           "power_on_hours": (j.get("power_on_time") or {}).get("hours"), "findings": []}
    f = out["findings"]

    def add(level, text, fix):
        f.append({"level": level, "text": text, "fix": fix})
    msgs = [m.get("string", "") for m in (j.get("smartctl") or {}).get("messages") or []]
    if "smart_status" not in j:
        add("unknown", "SMART data is not available for this drive" + (f" ({msgs[0]})" if msgs else "") + ".",
            "USB adapters often hide SMART. Connect the drive directly (SATA/NVMe) to check it, and keep a backup.")
        return out
    out["passed"] = bool((j.get("smart_status") or {}).get("passed"))
    if not out["passed"]:
        add("bad", "The drive reports that SMART health has FAILED.", "Copy your data off this drive now and replace it.")
    for ident, name, warn_at, bad_at, fix in (
            (5, "reallocated sectors", 1, 50, "The drive is remapping bad areas. Back up and plan to replace it."),
            (197, "sectors waiting to be remapped", 1, 10, "Back up now; pending sectors can turn into unreadable files."),
            (198, "uncorrectable sectors", 1, 1, "Some data could not be read back. Back up now and replace the drive."),
            (187, "reported uncorrectable errors", 1, 20, "Back up and watch this number; replace the drive if it grows."),
            (199, "cable/interface CRC errors", 1, 100, "Usually a cable or USB adapter problem: reseat or replace the cable.")):
        v = _attr(j, ident)
        if v:
            add("bad" if v >= bad_at else "warn" if v >= warn_at else "ok", f"{v} {name}.", fix)
    nv = j.get("nvme_smart_health_information_log") or {}
    if nv:
        if nv.get("critical_warning"):
            add("bad", f"NVMe critical warning flag {nv['critical_warning']}.", "Back up now and check cooling; replace the drive if it persists.")
        used = nv.get("percentage_used")
        if isinstance(used, int) and used >= 80:
            add("bad" if used >= 100 else "warn", f"{used}% of rated write endurance used.", "Plan a replacement and keep backups current.")
        spare, thr = nv.get("available_spare"), nv.get("available_spare_threshold")
        if isinstance(spare, int) and isinstance(thr, int) and spare <= thr:
            add("bad", f"Spare blocks at {spare}% (threshold {thr}%).", "Replace the drive soon; back up now.")
        if nv.get("media_errors"):
            add("warn", f"{nv['media_errors']} media errors logged.", "Back up and watch this number.")
    t = out["temp_c"]
    if isinstance(t, (int, float)) and t >= 60:
        add("bad" if t >= 70 else "warn", f"Drive temperature {t} °C.", "Improve airflow or give the drive a break; heat shortens drive life.")
    levels = [x["level"] for x in f]
    out["status"] = worst(levels) if levels else "ok"
    if out["status"] == "ok":
        add("ok", "SMART health passed; no warning counters.", "")
    return out


def free_space(paths):
    """paths: [(label, path)]. Warn below 5 % or 5 GiB free; bad below 1 % or 1 GiB."""
    out, seen = [], set()
    for label, p in paths:
        if not p or not os.path.exists(p):
            continue
        try:
            st = os.stat(p)
            u = shutil.disk_usage(p)
        except OSError:
            continue
        if st.st_dev in seen:
            continue
        seen.add(st.st_dev)
        pct = 100.0 * u.free / u.total if u.total else 0.0
        level = "bad" if (u.free < GiB or pct < 1) else "warn" if (u.free < 5 * GiB or pct < 5) else "ok"
        fix = {"bad": "Almost full: saving models, chats or settings can fail. Remove unused models or move them to another drive.",
               "warn": "Getting full: remove unused models or old outputs, or add a models drive in Storage.", "ok": ""}[level]
        out.append({"label": label, "path": p, "total": u.total, "free": u.free, "pct_free": round(pct, 1), "level": level, "fix": fix})
    return out


def report(space, smart):
    levels = [s["level"] for s in space] + [s["status"] for s in smart]
    return {"status": worst(levels) if levels else "unknown", "space": space, "smart": smart}


PART_RE = re.compile(r"^/dev/(sd[a-z]{1,2}\d{1,3}|nvme\d{1,2}n\d{1,2}p\d{1,3}|vd[a-z]{1,2}\d{1,3}|mmcblk\d{1,2}p\d{1,3})$")


def disk_of(dev, run=subprocess.run):
    """A partition or whole-disk path -> its whole-disk path, or None when it is not a plain local disk."""
    if not isinstance(dev, str) or not (DEV_RE.match(dev) or PART_RE.match(dev)):
        return None
    if DEV_RE.match(dev):
        return dev
    r = run(["lsblk", "-ndo", "PKNAME", dev], capture_output=True, text=True, timeout=10)
    parent = "/dev/" + r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else None
    return parent if parent and DEV_RE.match(parent) else None


def collect_smart(dev, run=subprocess.run):
    """Root only (called by victor-priv). Read-only SMART query of one validated whole disk."""
    disk = disk_of(dev, run)
    if not disk:
        raise ValueError("not a local disk path")
    if not shutil.which("smartctl"):
        return {"device": {"name": disk}, "smartctl": {"messages": [{"string": "smartctl (smartmontools) is not installed"}]}}
    r = run(["smartctl", "-j", "-H", "-A", "-i", disk], capture_output=True, text=True, timeout=60)
    try:
        j = json.loads(r.stdout)
    except ValueError:
        j = {"smartctl": {"messages": [{"string": (r.stderr or r.stdout).strip()[:200] or "no SMART output"}]}}
    j.setdefault("device", {})["name"] = disk
    return j
