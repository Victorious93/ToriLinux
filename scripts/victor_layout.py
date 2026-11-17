#!/usr/bin/env python3
"""Partition planning and GPT/MBR writing for VictorLinux boot media.

Used by scripts/make-usb.sh. Standard library only. Python integers are unbounded, so capacity and
offset arithmetic cannot silently wrap; every value that must fit a 32-bit MBR field or the
device is checked explicitly, and the WHOLE plan is validated before anything is written.

Commands (all print KEY=VALUE lines, never evaluated by a shell):
  victor_layout.py plan  --iso ISO (--device DEV | --disk-bytes N [--logical N --physical N]) [options]
  victor_layout.py apply --iso ISO --device DEV|FILE [options]   # writes ONLY the partition table, after plan validation
  victor_layout.py current --device DEV|FILE                      # read-only: where the existing data partition is
  victor_layout.py update-plan  --iso NEW.iso --device DEV [--keep-start LBA --keep-len LBA --keep-table mbr|gpt]
  victor_layout.py update-apply --iso NEW.iso --device DEV [same]  # table only, data partition kept at the same sectors

Model of the boot image: the hybrid ISO is written raw at LBA 0 (512-byte units). Its own system area
(MBR boot code, GPT if present, El Torito/ESP partitions) is preserved. A persistent data partition is
appended after the image. In MBR mode it takes the free slot 3. In GPT mode the ISO's existing GPT entries
are kept verbatim, the backup GPT is relocated to the real end of the drive, and the data entry is added.
"""
import argparse
import os
import struct
import subprocess
import sys
import uuid
import zlib

MIB = 1 << 20
GIB = 1 << 30
ISO_UNIT = 512                      # LBA unit of the hybrid ISO's own partition table
MBR_MAX_SECTORS = 1 << 32           # 32-bit start/length fields
GPT_ENTRIES = 128
GPT_ENTRY_SIZE = 128
GPT_ARRAY_BYTES = GPT_ENTRIES * GPT_ENTRY_SIZE
ALIGN_BYTES = MIB                   # partition alignment (also a multiple of any common physical sector size)
DEFAULT_RESERVE_MIB = 1024
DEFAULT_MIN_DATA_GIB = 2
GUID_LINUX_DATA = uuid.UUID("0FC63DAF-8483-4772-8E79-3D69D8477DE4")
GUID_ESP = uuid.UUID("C12A7328-F81F-11D2-BA4B-00A0C93EC93B")


class PlanError(Exception):
    """The requested layout cannot be created. Raised BEFORE the target is touched."""


# ------------------------------------------------------------------------------ image inspection
def _crc(b):
    return zlib.crc32(b) & 0xFFFFFFFF


def parse_gpt(sector0_to_n, logical):
    """Parse a GPT from the raw bytes of the first 34+ sectors. Returns dict or None when absent/invalid."""
    if len(sector0_to_n) < 2 * logical:
        return None
    hdr = sector0_to_n[logical:logical + 92]
    if hdr[:8] != b"EFI PART":
        return None
    (_rev, hsize, hcrc, _res, cur, bak, first, last, guid, entries_lba, n, esz, ecrc) = struct.unpack("<IIIIQQQQ16sQIII", hdr[8:92])
    if hsize < 92 or hsize > logical:
        raise PlanError("image GPT header has an invalid size")
    raw = bytearray(sector0_to_n[logical:logical + hsize])
    raw[16:20] = b"\0\0\0\0"
    if _crc(bytes(raw)) != hcrc:
        raise PlanError("image GPT header checksum is invalid")
    if cur != 1:
        raise PlanError("image GPT header is not at LBA 1")
    start = entries_lba * logical
    arr = sector0_to_n[start:start + n * esz]
    if len(arr) != n * esz:
        raise PlanError("image GPT entry array is truncated")
    if _crc(arr) != ecrc:
        raise PlanError("image GPT entry-array checksum is invalid")
    parts = []
    for i in range(n):
        e = arr[i * esz:(i + 1) * esz]
        if e[:16] == bytes(16):
            continue
        ptype, pguid, s, l, attrs = struct.unpack("<16s16sQQQ", e[:56])
        parts.append({"index": i, "type": ptype, "guid": pguid, "start": s, "end": l, "attrs": attrs, "name": e[56:128]})
    return {"disk_guid": guid, "entries_lba": entries_lba, "entries": parts, "n": n, "esz": esz, "first_usable": first, "last_usable": last, "backup_lba": bak}


def inspect_image(iso_path):
    """Read the hybrid image's system area. Returns dict(size, mbr, mbr_entries, gpt, end_s512, boot_code)."""
    size = os.path.getsize(iso_path)
    with open(iso_path, "rb") as f:
        head = f.read(64 * 1024)
    if len(head) < 512 or head[510:512] != b"\x55\xaa":
        raise PlanError("the image is not a hybrid ISO (no MBR boot signature)")
    entries = []
    for i in range(4):
        e = head[446 + 16 * i:446 + 16 * (i + 1)]
        status, ptype = e[0], e[4]
        start, length = struct.unpack("<II", e[8:16])
        entries.append({"slot": i + 1, "status": status, "type": ptype, "start": start, "len": length, "raw": e})
    gpt = parse_gpt(head, ISO_UNIT)
    end = 0
    for e in entries:
        if e["type"] != 0xEE:
            end = max(end, e["start"] + e["len"])
    if gpt:
        for p in gpt["entries"]:
            end = max(end, p["end"] + 1)
    end = max(end, (size + ISO_UNIT - 1) // ISO_UNIT)
    # LBA 1..33 will hold the rebuilt GPT. They may only contain the image's own GPT structures or zeros.
    # Anything else there (a boot loader stage) would be destroyed by a GPT.
    area = bytearray(head[512:34 * 512])
    if gpt:
        area[0:512] = bytes(512)
        lo = gpt["entries_lba"] * 512 - 512
        for i in range(max(lo, 0), min(lo + gpt["n"] * gpt["esz"], len(area))):
            area[i] = 0
    sysarea_free = not any(area)
    return {"size": size, "mbr": head[:512], "mbr_entries": entries, "gpt": gpt, "end_s512": end, "sysarea_free": sysarea_free}


def gpt_geometry(image, logical):
    """(entries_lba, n_entries, entry_size, first_usable, array_sectors). An image's own GPT geometry is preserved
    (xorriso/grub-mkrescue images interleave Apple Partition Map blocks with a GPT whose array sits at LBA 20)."""
    g = image.get("gpt")
    if g:
        n, esz, el, fu = g["n"], g["esz"], g["entries_lba"], g["first_usable"]
    else:
        n, esz, el, fu = GPT_ENTRIES, GPT_ENTRY_SIZE, 2, 2 + GPT_ARRAY_BYTES // logical
    return el, n, esz, fu, -(-(n * esz) // logical)


# ------------------------------------------------------------------------------ planning
def _up(v, a):
    return -(-v // a) * a


def _down(v, a):
    return v // a * a


def plan_layout(disk_bytes, image, logical=512, physical=512, table="auto", data_bytes=None,
                reserve_bytes=DEFAULT_RESERVE_MIB * MIB, min_data_bytes=DEFAULT_MIN_DATA_GIB * GIB,
                allow_mbr_cap=False, image_headroom_bytes=0):
    """Compute the complete plan. Pure function: raises PlanError, never touches a device.
    image_headroom_bytes leaves room between the image and the data partition so a larger future image can be
    written in place (`update`) without moving the data partition."""
    if table not in ("auto", "mbr", "gpt"):
        raise PlanError("table must be auto, mbr or gpt")
    if disk_bytes <= 0:
        raise PlanError("device size is unknown or zero")
    if logical not in (512, 4096):
        raise PlanError(f"unsupported logical sector size {logical}")
    if logical != 512:
        # The hybrid ISO's partition table, El Torito catalogue and ESP are expressed in 512-byte LBAs and
        # 2048-byte ISO blocks. Written raw to a 4Kn device the LBAs are misread by firmware.
        raise PlanError(
            f"this drive uses {logical}-byte logical sectors (4Kn). The VictorLinux hybrid image is laid out in 512-byte "
            "sectors and is not bootable when written raw to a 4Kn device, so boot-media creation is refused. "
            "512e drives (512-byte logical, 4096-byte physical) are supported. A 4Kn drive can still be used as a DATA drive.")
    if physical < logical or physical % logical or ALIGN_BYTES % physical:
        raise PlanError(f"unsupported physical sector size {physical}")
    if disk_bytes % logical:
        raise PlanError("device size is not a multiple of its logical sector size")
    total_lba = disk_bytes // logical
    image_end = image["end_s512"] * ISO_UNIT                       # bytes
    if image_headroom_bytes < 0:
        raise PlanError("image headroom cannot be negative")
    data_start = _up(image_end + image_headroom_bytes, ALIGN_BYTES)

    gpt_capable = True
    why_not = ""
    if image["gpt"] is None:
        # A GPT needs LBAs 0..33 for itself. An MBR-only hybrid image whose first partition starts there cannot be
        # re-described in a GPT without changing how the image boots.
        low = [e for e in image["mbr_entries"] if e["type"] != 0xEE and e["len"] and e["start"] < 2 + GPT_ARRAY_BYTES // ISO_UNIT]
        if low:
            gpt_capable, why_not = False, ("the image has an MBR-only partition table whose partition starts inside the area a GPT needs "
                                           "(LBA < 34); the image would have to be rebuilt with a GPT-hybrid system area")
    if gpt_capable and image["gpt"] is None and not image.get("sysarea_free", True):
        gpt_capable, why_not = False, "sectors 1-33 of the image hold data other than a partition table (a boot loader stage), which a GPT would overwrite"
    free_slot = [e for e in image["mbr_entries"] if e["type"] == 0 and e["len"] == 0 and e["start"] == 0]
    # The proven small-drive layout: the image's MBR has slot 3 free and is not a protective MBR (no 0xEE entry), so the kernel reads
    # the MBR and a slot-3 entry is visible. An image that carries a GPT as well keeps it untouched in this mode.
    mbr_slot_ok = any(e["slot"] == 3 for e in free_slot) and not any(e["type"] == 0xEE for e in image["mbr_entries"])

    _el, _n, _esz, g_first, array_sectors = gpt_geometry(image, logical)
    gpt_overhead_bytes = (1 + array_sectors) * logical             # backup header + backup array at end of disk

    def build(kind):
        tail = reserve_bytes + (gpt_overhead_bytes if kind == "gpt" else 0)
        end = _down(disk_bytes - tail, ALIGN_BYTES)
        avail = end - data_start
        if avail < min_data_bytes:
            return None, avail
        size = avail if data_bytes is None else min(_down(data_bytes, ALIGN_BYTES), avail)
        if data_bytes is not None and data_bytes > avail:
            raise PlanError(f"requested data size {data_bytes} bytes exceeds the {avail} bytes available")
        if size < min_data_bytes:
            raise PlanError(f"data partition of {size} bytes is below the minimum {min_data_bytes} bytes")
        return {"start": data_start, "size": size}, avail

    chosen = table
    if table == "auto":
        # Prefer the proven MBR layout whenever it is fully representable; otherwise GPT.
        probe, _ = build("mbr")
        fits = (probe is not None and mbr_slot_ok and logical == 512
                and (probe["start"] + probe["size"]) // logical <= MBR_MAX_SECTORS - 1 and probe["start"] // logical < MBR_MAX_SECTORS
                and probe["size"] // logical < MBR_MAX_SECTORS)
        chosen = "mbr" if fits or (probe is None and mbr_slot_ok) else "gpt"
    notes = []
    if chosen == "gpt":
        if not gpt_capable:
            if allow_mbr_cap and mbr_slot_ok:
                chosen = "mbr"
                notes.append("GPT unavailable for this image (" + why_not + "); data is capped to what MBR can address.")
            else:
                raise PlanError("GPT is required for this drive but unavailable: " + why_not + ". "
                                "Rebuild the ISO, or pass --allow-mbr-cap to accept a data partition limited to the first 2 TiB "
                                "(the remainder stays unallocated).")
    part, avail = build(chosen)
    if part is None:
        need = data_start + min_data_bytes + reserve_bytes + (gpt_overhead_bytes if chosen == "gpt" else 0)
        raise PlanError(f"drive too small for this layout: need at least {need} bytes ({need / GIB:.2f} GiB) "
                        f"= image {image_end / GIB:.2f} GiB + {min_data_bytes / GIB:.0f} GiB data + {reserve_bytes / MIB:.0f} MiB reserve"
                        f"{' + GPT backup' if chosen == 'gpt' else ''}; this drive has {disk_bytes} bytes ({disk_bytes / GIB:.2f} GiB)")
    start_lba, len_lba = part["start"] // logical, part["size"] // logical
    if chosen == "mbr":
        if not mbr_slot_ok:
            raise PlanError("the image's MBR slot 3 is not free or the MBR is a protective MBR; the MBR layout cannot be used")
        if start_lba >= MBR_MAX_SECTORS or len_lba >= MBR_MAX_SECTORS or start_lba + len_lba > MBR_MAX_SECTORS:
            if not allow_mbr_cap:
                raise PlanError("the data partition does not fit MBR's 32-bit sector fields; use --table gpt")
            cap = MBR_MAX_SECTORS - 1 - start_lba
            if cap <= 0:
                raise PlanError("no MBR-addressable space remains after the image")
            len_lba = _down(cap * logical, ALIGN_BYTES) // logical
            part["size"] = len_lba * logical
            notes.append(f"data partition capped at {part['size']} bytes by the MBR 32-bit limit; the rest of the drive stays unallocated")
        if len_lba < min_data_bytes // logical:
            raise PlanError("capped MBR data partition is below the minimum")
    else:
        first_usable = g_first
        last_usable = total_lba - 2 - array_sectors
        if start_lba < first_usable or start_lba + len_lba - 1 > last_usable:
            raise PlanError("data partition falls outside the GPT usable range")
    end_lba = start_lba + len_lba - 1
    if end_lba >= total_lba:
        raise PlanError("data partition extends beyond the device")
    return {
        "table": chosen, "logical": logical, "physical": physical, "disk_bytes": disk_bytes, "total_lba": total_lba,
        "image_bytes": image["size"], "image_end_bytes": image_end,
        "data_start_lba": start_lba, "data_len_lba": len_lba, "data_end_lba": end_lba,
        "data_bytes": len_lba * logical, "unallocated_bytes": disk_bytes - (end_lba + 1) * logical - (gpt_overhead_bytes if chosen == "gpt" else 0),
        "reserve_bytes": reserve_bytes, "min_data_bytes": min_data_bytes, "notes": notes,
        "gpt_backup_lba": total_lba - 1 if chosen == "gpt" else None,
    }


def minimum_drive_bytes(image, table="mbr", reserve_bytes=DEFAULT_RESERVE_MIB * MIB, min_data_bytes=DEFAULT_MIN_DATA_GIB * GIB):
    data_start = _up(image["end_s512"] * ISO_UNIT, ALIGN_BYTES)
    return data_start + min_data_bytes + reserve_bytes + (GPT_ARRAY_BYTES + 512 if table == "gpt" else 0)


# ------------------------------------------------------------------------------ table construction
def build_gpt(image, plan, disk_guid=None, part_guid=None, name="VICTORDATA"):
    """Return (primary_bytes[LBA0..2+array], backup_bytes[array+header], backup_lba_start). Pure function."""
    logical = plan["logical"]
    ent_lba, n_ent, esz, first_usable, array_sectors = gpt_geometry(image, logical)
    total = plan["total_lba"]
    entries = bytearray(n_ent * esz)
    n = 0
    if image["gpt"]:
        for p in sorted(image["gpt"]["entries"], key=lambda x: x["index"]):
            e = bytearray(esz)
            e[:56] = struct.pack("<16s16sQQQ", p["type"], p["guid"], p["start"], p["end"], p["attrs"])
            e[56:128] = p["name"]
            entries[n * esz:(n + 1) * esz] = e
            n += 1
        disk_guid = disk_guid or image["gpt"]["disk_guid"]
    else:
        for e_ in image["mbr_entries"]:
            if e_["type"] == 0xEE or not e_["len"]:
                continue
            ptype = GUID_ESP if e_["type"] == 0xEF else GUID_LINUX_DATA
            e = bytearray(esz)
            e[:56] = struct.pack("<16s16sQQQ", ptype.bytes_le, uuid.uuid4().bytes_le, e_["start"], e_["start"] + e_["len"] - 1, 0)
            label = "ISO" if e_["type"] != 0xEF else "EFI"
            e[56:128] = label.encode("utf-16-le").ljust(72, b"\0")
            entries[n * esz:(n + 1) * esz] = e
            n += 1
    if n >= n_ent:
        raise PlanError("no free GPT entry")
    e = bytearray(esz)
    e[:56] = struct.pack("<16s16sQQQ", GUID_LINUX_DATA.bytes_le, (part_guid or uuid.uuid4()).bytes_le,
                         plan["data_start_lba"], plan["data_end_lba"], 0)
    e[56:128] = name.encode("utf-16-le").ljust(72, b"\0")
    entries[n * esz:(n + 1) * esz] = e
    entries = bytes(entries)
    dg = disk_guid if isinstance(disk_guid, bytes) else (disk_guid or uuid.uuid4()).bytes_le
    last_usable = total - 2 - array_sectors

    def header(cur, bak, entries_lba):
        h = bytearray(logical)
        struct.pack_into("<8sIIIIQQQQ16sQIII", h, 0, b"EFI PART", 0x00010000, 92, 0, 0, cur, bak, first_usable, last_usable,
                         dg, entries_lba, n_ent, esz, _crc(entries))
        struct.pack_into("<I", h, 16, _crc(bytes(h[:92])))
        return bytes(h)

    primary = header(1, total - 1, ent_lba)
    backup = header(total - 1, 1, total - 1 - array_sectors)
    return primary, entries, backup, ent_lba


def build_mbr(image, plan):
    """MBR sector 0. MBR mode: the image's MBR with slot 3 = data partition. GPT mode: the image's boot code and
    entries are kept, and slot 3 (when free) becomes a 0xEE protective entry covering the rest of the disk."""
    mbr = bytearray(image["mbr"])
    if plan["table"] == "mbr":
        entry = struct.pack("<B3sB3sII", 0, b"\xfe\xff\xff", 0x83, b"\xfe\xff\xff", plan["data_start_lba"], plan["data_len_lba"])
        assert plan["data_start_lba"] < MBR_MAX_SECTORS and plan["data_len_lba"] < MBR_MAX_SECTORS
        mbr[446 + 32:446 + 48] = entry
        return bytes(mbr)
    ee_start = 1
    if any(e["type"] == 0xEE for e in image["mbr_entries"]):
        return bytes(mbr)                       # image already has a protective entry
    # protective entry in the first free slot; sized to the disk, saturated at 0xFFFFFFFF as the UEFI spec requires
    slot = next((e["slot"] for e in image["mbr_entries"] if e["type"] == 0 and e["len"] == 0), None)
    if slot is None:
        raise PlanError("no free MBR slot for the GPT protective entry")
    size = min(plan["total_lba"] - 1, 0xFFFFFFFF)
    mbr[446 + 16 * (slot - 1):446 + 16 * slot] = struct.pack("<B3sB3sII", 0, b"\x00\x02\x00", 0xEE, b"\xff\xff\xff", ee_start, size)
    return bytes(mbr)


def validate_written_gpt(read_at, total_lba, logical, expect_name_part=True):
    """Independent check of a written GPT through read_at(offset, n). Returns the parsed partition list."""
    prim = read_at(logical, logical)
    if prim[:8] != b"EFI PART":
        raise PlanError("primary GPT signature missing")
    f = struct.unpack("<IIIIQQQQ16sQIII", prim[8:92])
    hcrc, bak, first, last, ent_lba, n, esz, ecrc = f[2], f[5], f[6], f[7], f[9], f[10], f[11], f[12]
    h = bytearray(prim[:92]); h[16:20] = b"\0\0\0\0"
    if _crc(bytes(h)) != hcrc:
        raise PlanError("primary GPT header CRC mismatch")
    arr = read_at(ent_lba * logical, n * esz)
    if _crc(arr) != ecrc:
        raise PlanError("primary GPT array CRC mismatch")
    back = read_at(bak * logical, logical)
    if back[:8] != b"EFI PART" or bak != total_lba - 1:
        raise PlanError("backup GPT header missing or not at the last LBA")
    parts = []
    for i in range(n):
        e = arr[i * esz:(i + 1) * esz]
        if e[:16] == bytes(16):
            continue
        s, l = struct.unpack("<QQ", e[32:48])
        if s < first or l > last or l < s:
            raise PlanError(f"GPT entry {i + 1} outside usable range")
        parts.append({"start": s, "end": l, "name": e[56:128].decode("utf-16-le").rstrip("\0")})
    spans = sorted((p["start"], p["end"]) for p in parts)
    for a, b in zip(spans, spans[1:]):
        if b[0] <= a[1]:
            raise PlanError("GPT entries overlap")
    return parts


# ------------------------------------------------------------------------------ device access
def device_geometry(path):
    """(size_bytes, logical, physical). Files report 512/512 so sparse test disks work."""
    if os.path.isfile(path):
        return os.path.getsize(path), 512, 512

    def bd(opt):
        r = subprocess.run(["blockdev", opt, path], capture_output=True, text=True)
        if r.returncode != 0:
            raise PlanError(f"cannot read {opt} of {path}: {r.stderr.strip()}")
        return int(r.stdout.strip())
    return bd("--getsize64"), bd("--getss"), bd("--getpbsz")


def apply_plan(path, image, plan, part_guid=None):
    """Write only the partition-table metadata. The image bytes themselves are never rewritten here."""
    logical = plan["logical"]
    mbr = build_mbr(image, plan)
    with open(path, "r+b") as f:
        if plan["table"] == "gpt":
            primary, entries, backup, ent_lba = build_gpt(image, plan, part_guid=part_guid)
            array_sectors = gpt_geometry(image, logical)[4]
            # backup first: a crash before the primary is rewritten leaves the old (valid) image layout intact
            f.seek((plan["total_lba"] - 1 - array_sectors) * logical); f.write(entries)
            f.seek((plan["total_lba"] - 1) * logical); f.write(backup)
            f.seek(ent_lba * logical); f.write(entries)
            f.seek(logical); f.write(primary)
        f.seek(0); f.write(mbr)
        f.flush(); os.fsync(f.fileno())
        if plan["table"] == "gpt":
            parts = validate_written_gpt(lambda o, n: (f.seek(o), f.read(n))[1], plan["total_lba"], logical)
            if not any(p["start"] == plan["data_start_lba"] and p["end"] == plan["data_end_lba"] for p in parts):
                raise PlanError("data partition not found in the written GPT")
        else:
            f.seek(446 + 32); e = f.read(16)
            st, ln = struct.unpack("<II", e[8:16])
            if (st, ln) != (plan["data_start_lba"], plan["data_len_lba"]):
                raise PlanError("MBR slot 3 read-back mismatch")


# ------------------------------------------------------------------------------ update in place
def current_data_partition(path, logical=512):
    """Read-only. The existing VICTORDATA partition: MBR slot 3 (type 0x83) or a GPT entry named VICTORDATA.
    Returns dict(table, start_lba, len_lba, part_guid) or None."""
    with open(path, "rb") as f:
        head = f.read(64 * 1024)
    if len(head) < 512 or head[510:512] != b"\x55\xaa":
        return None
    g = parse_gpt(head, logical)
    if g:
        names = [p["name"].decode("utf-16-le", "replace").rstrip("\0") if isinstance(p["name"], (bytes, bytearray)) else str(p["name"])
                 for p in g["entries"]]
        ventoy = any(n in ("Ventoy", "VTOYEFI") for n in names)
        for p, name in zip(g["entries"], names):
            if name == "VICTORDATA":
                return {"table": "gpt", "start_lba": p["start"], "len_lba": p["end"] - p["start"] + 1, "part_guid": p["guid"], "ventoy": ventoy}
    e = head[446 + 32:446 + 48]
    st, ln = struct.unpack("<II", e[8:16])
    if e[4] == 0x83 and ln:
        return {"table": "mbr", "start_lba": st, "len_lba": ln, "part_guid": None}
    return None


def plan_update(disk_bytes, image, logical, physical, current):
    """Pure. Plan a new image written over the old one while the data partition keeps its exact sectors.
    Raises PlanError (nothing touched) when the new image would reach into the data partition."""
    if logical != 512:
        raise PlanError(f"this drive uses {logical}-byte logical sectors (4Kn); boot media updates are refused")
    if not current:
        raise PlanError("no VICTORDATA partition found on this drive; use `write` for a fresh drive (it erases the drive)")
    if current.get("ventoy"):
        raise PlanError("this is a Ventoy drive: copy the new ISO onto its 'Ventoy' partition instead (no repartitioning needed)")
    total_lba = disk_bytes // logical
    start, length, table = int(current["start_lba"]), int(current["len_lba"]), current["table"]
    end = start + length - 1
    if start <= 0 or length <= 0 or end >= total_lba:
        raise PlanError("the existing data partition entry is outside the drive")
    image_end = image["end_s512"] * ISO_UNIT
    if _up(image_end, ALIGN_BYTES) > start * logical:
        raise PlanError(f"the new image needs {_up(image_end, ALIGN_BYTES) / GIB:.2f} GiB but only {start * logical / GIB:.2f} GiB lie before "
                        "the data partition. Updating in place would overwrite your data. Back up, then use `write` (erases the drive).")
    entries = image["mbr_entries"]
    if table == "mbr":
        if not (any(e["slot"] == 3 and e["type"] == 0 and e["len"] == 0 for e in entries) and not any(e["type"] == 0xEE for e in entries)):
            raise PlanError("the new image's MBR slot 3 is not free, so the data partition cannot be kept in an MBR layout")
    else:
        if image["gpt"] is None and not image.get("sysarea_free", True):
            raise PlanError("the new image cannot carry a GPT, but this drive's data partition is described by a GPT")
        _el, _n, _esz, first, array_sectors = gpt_geometry(image, logical)
        if start < first or end > total_lba - 2 - array_sectors:
            raise PlanError("the data partition falls outside the new GPT's usable range")
    gpt_over = (1 + gpt_geometry(image, logical)[4]) * logical if table == "gpt" else 0
    return {
        "table": table, "logical": logical, "physical": physical, "disk_bytes": disk_bytes, "total_lba": total_lba,
        "image_bytes": image["size"], "image_end_bytes": image_end, "data_start_lba": start, "data_len_lba": length,
        "data_end_lba": end, "data_bytes": length * logical, "unallocated_bytes": disk_bytes - (end + 1) * logical - gpt_over,
        "reserve_bytes": disk_bytes - (end + 1) * logical, "min_data_bytes": 0, "gpt_backup_lba": total_lba - 1 if table == "gpt" else None,
        "notes": ["data partition kept at the same sectors; only the image area and the partition table are rewritten"],
        "part_guid": current.get("part_guid"),
    }


def apply_update(path, image, plan):
    """Like apply_plan, but the GPT data entry keeps its partition GUID so PARTUUID references stay valid."""
    guid = uuid.UUID(bytes_le=bytes(plan["part_guid"])) if plan["table"] == "gpt" and plan.get("part_guid") else None
    return apply_plan(path, image, plan, part_guid=guid)


# ------------------------------------------------------------------------------ CLI
def _add_common(ap):
    ap.add_argument("--iso", required=True)
    ap.add_argument("--device")
    ap.add_argument("--disk-bytes", type=int)
    ap.add_argument("--logical", type=int)
    ap.add_argument("--physical", type=int)
    ap.add_argument("--table", default="auto", choices=("auto", "mbr", "gpt"))
    ap.add_argument("--data-gib", type=float, help="data partition size in GiB (default: all usable space)")
    ap.add_argument("--reserve-mib", type=int, default=DEFAULT_RESERVE_MIB)
    ap.add_argument("--min-data-gib", type=float, default=DEFAULT_MIN_DATA_GIB)
    ap.add_argument("--allow-mbr-cap", action="store_true")
    ap.add_argument("--image-headroom-mib", type=int, default=0, help="free space after the image for in-place updates")


def _plan_from_args(a):
    image = inspect_image(a.iso)
    if a.device:
        size, lg, ph = device_geometry(a.device)
    elif a.disk_bytes:
        size, lg, ph = a.disk_bytes, a.logical or 512, a.physical or a.logical or 512
    else:
        raise PlanError("give --device or --disk-bytes")
    if a.logical and a.device is None:
        lg = a.logical
    if a.reserve_mib < 1 or a.min_data_gib < 1:
        raise PlanError("reserve must be >= 1 MiB and minimum data >= 1 GiB")
    plan = plan_layout(size, image, lg, ph, a.table, None if a.data_gib is None else int(a.data_gib * GIB),
                       a.reserve_mib * MIB, int(a.min_data_gib * GIB), a.allow_mbr_cap, a.image_headroom_mib * MIB)
    return image, plan


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for n in ("plan", "apply"):
        _add_common(sub.add_parser(n))
    cur = sub.add_parser("current")
    cur.add_argument("--device", required=True)
    for n in ("update-plan", "update-apply"):
        u = sub.add_parser(n)
        u.add_argument("--iso", required=True)
        u.add_argument("--device", required=True)
        u.add_argument("--keep-start", type=int, help="recovery: data partition start LBA printed before an interrupted update")
        u.add_argument("--keep-len", type=int)
        u.add_argument("--keep-table", choices=("mbr", "gpt"))
    a = ap.parse_args(argv)
    try:
        if a.cmd == "current":
            c = current_data_partition(a.device)
            if not c:
                raise PlanError("no VICTORDATA partition found")
            print(f"TABLE={c['table']}\nDATA_START_LBA={c['start_lba']}\nDATA_LEN_LBA={c['len_lba']}")
            return 0
        if a.cmd in ("update-plan", "update-apply"):
            image = inspect_image(a.iso)
            size, lg, ph = device_geometry(a.device)
            if a.keep_start is not None:
                if not (a.keep_len and a.keep_table):
                    raise PlanError("--keep-start needs --keep-len and --keep-table")
                c = {"table": a.keep_table, "start_lba": a.keep_start, "len_lba": a.keep_len, "part_guid": None}
            else:
                c = current_data_partition(a.device, lg)
            plan = plan_update(size, image, lg, ph, c)
            if a.cmd == "update-apply":
                apply_update(a.device, image, plan)
        else:
            image, plan = _plan_from_args(a)
        if a.cmd == "apply":
            apply_plan(a.device, image, plan)
    except PlanError as e:
        print(f"ERROR={e}", file=sys.stderr)
        return 3
    for k in ("table", "logical", "physical", "disk_bytes", "image_bytes", "image_end_bytes", "data_start_lba", "data_len_lba",
              "data_end_lba", "data_bytes", "unallocated_bytes", "reserve_bytes", "gpt_backup_lba"):
        print(f"{k.upper()}={'' if plan[k] is None else plan[k]}")
    for n in plan["notes"]:
        print(f"NOTE={n}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
