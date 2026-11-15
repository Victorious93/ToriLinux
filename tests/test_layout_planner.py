"""Boot-media layout planning: MBR/GPT, capacities from small to 16 TB, 32-bit boundary, sector rejection.

Uses sparse files only; no terabytes are allocated and no real device is touched.
"""
import importlib.util
import os
import struct
import uuid
import zlib

import pytest

spec = importlib.util.spec_from_file_location("victor_layout", os.path.join(os.path.dirname(__file__), "..", "scripts", "victor_layout.py"))
vl = importlib.util.module_from_spec(spec)
spec.loader.exec_module(vl)

GIB, MIB, TIB = 1 << 30, 1 << 20, 1 << 40


def make_iso(path, size=1900 * MIB, kind="mbr"):
    """Synthetic hybrid image. kind: 'mbr' (isohybrid style, slot1 starts at 0) or 'gpt' (system area carries a GPT)."""
    sectors = size // 512
    esp_len = 8192
    with open(path, "wb") as f:
        f.truncate(size)
        mbr = bytearray(512)
        mbr[0:3] = b"\xeb\x3c\x90"
        if kind == "mbr":
            mbr[446:462] = struct.pack("<B3sB3sII", 0x80, b"\0\0\0", 0x00, b"\0\0\0", 0, sectors - esp_len)
        else:
            mbr[446:462] = struct.pack("<B3sB3sII", 0x80, b"\0\0\0", 0x00, b"\0\0\0", 64, sectors - esp_len - 64)
        mbr[462:478] = struct.pack("<B3sB3sII", 0, b"\0\0\0", 0xEF, b"\0\0\0", sectors - esp_len, esp_len)
        mbr[510:512] = b"\x55\xaa"
        f.write(mbr)
        if kind == "gpt":
            entries = bytearray(16384)
            for i, (t, s, l, n) in enumerate([(vl.GUID_LINUX_DATA, 64, sectors - esp_len - 1, "ISO"), (vl.GUID_ESP, sectors - esp_len, sectors - 1, "EFI")]):
                e = bytearray(128)
                e[:56] = struct.pack("<16s16sQQQ", t.bytes_le, uuid.uuid4().bytes_le, s, l, 0)
                e[56:128] = n.encode("utf-16-le").ljust(72, b"\0")
                entries[i * 128:(i + 1) * 128] = e
            h = bytearray(512)
            struct.pack_into("<8sIIIIQQQQ16sQIII", h, 0, b"EFI PART", 0x10000, 92, 0, 0, 1, sectors - 1, 34, sectors - 34,
                             uuid.uuid4().bytes_le, 2, 128, 128, zlib.crc32(bytes(entries)) & 0xFFFFFFFF)
            struct.pack_into("<I", h, 16, zlib.crc32(bytes(h[:92])) & 0xFFFFFFFF)
            f.seek(512); f.write(h); f.write(entries)


@pytest.fixture
def iso_mbr(tmp_path):
    p = tmp_path / "a.iso"; make_iso(str(p)); return vl.inspect_image(str(p))


@pytest.fixture
def iso_gpt(tmp_path):
    p = tmp_path / "g.iso"; make_iso(str(p), kind="gpt"); return vl.inspect_image(str(p))


def plan(image, disk, **kw):
    return vl.plan_layout(disk, image, **kw)


@pytest.mark.parametrize("disk", [8 * GIB, 16 * GIB, 59 * GIB, 119 * GIB, 1 * TIB - 12345 * 512])
def test_small_and_ordinary_media_use_proven_mbr_layout(iso_mbr, disk):
    p = plan(iso_mbr, disk)
    assert p["table"] == "mbr"
    assert p["data_start_lba"] % (MIB // 512) == 0
    assert (p["data_end_lba"] + 1) * 512 <= disk - 1024 * MIB + MIB   # reserve kept (alignment may add < 1 MiB)
    assert p["data_bytes"] >= 2 * GIB


def test_minimum_is_computed_and_enforced(iso_mbr):
    need = vl.minimum_drive_bytes(iso_mbr)
    assert plan(iso_mbr, need)["data_bytes"] >= 2 * GIB
    with pytest.raises(vl.PlanError, match="too small"):
        plan(iso_mbr, need - 2 * MIB)
    # a 4 GB-class stick cannot hold image + data
    with pytest.raises(vl.PlanError, match="at least"):
        plan(iso_mbr, 3_900_000_000 // 512 * 512)


@pytest.mark.parametrize("tb", [1, 2, 4, 8, 16])
def test_terabyte_drives_auto_use_gpt_when_image_supports_it(iso_gpt, tb):
    disk = tb * 10**12                      # marketed (decimal) capacity, non-power-of-two
    p = plan(iso_gpt, disk)
    if disk // 512 + 0 >= vl.MBR_MAX_SECTORS or (p["data_end_lba"] + 1) >= vl.MBR_MAX_SECTORS:
        assert p["table"] == "gpt"
    assert p["data_end_lba"] < p["total_lba"] - 33
    assert p["data_bytes"] > 0.9 * disk - 4 * GIB      # nothing truncated: only image, alignment and reserve are excluded


def test_32bit_boundary_exact(iso_gpt, iso_mbr):
    # MBR can address LBAs < 2**32 only. Drive of exactly 2 TiB + reserve edge cases.
    two_tib = vl.MBR_MAX_SECTORS * 512
    assert plan(iso_mbr, two_tib - 1 * MIB * 0 - 512 * 0 * 1 or two_tib, table="mbr", allow_mbr_cap=True)["table"] == "mbr"
    with pytest.raises(vl.PlanError, match="32-bit"):
        plan(iso_mbr, two_tib + 8 * GIB, table="mbr")
    # auto picks GPT above the limit, MBR-capable image or not, and refuses rather than truncates on an MBR-only image
    assert plan(iso_gpt, two_tib + 8 * GIB)["table"] == "gpt"
    with pytest.raises(vl.PlanError, match="GPT is required"):
        plan(iso_mbr, two_tib + 8 * GIB)
    capped = plan(iso_mbr, 4 * TIB, allow_mbr_cap=True)
    assert capped["table"] == "mbr" and capped["data_end_lba"] < vl.MBR_MAX_SECTORS
    assert any("capped" in n or "unavailable" in n for n in capped["notes"])


def test_hybrid_image_with_gpt_and_free_mbr_slot_keeps_the_proven_mbr_layout_when_it_fits(iso_gpt):
    assert plan(iso_gpt, 64 * GIB)["table"] == "mbr"                    # same as the previous script's behaviour
    assert plan(iso_gpt, 64 * GIB, table="gpt")["table"] == "gpt"       # GPT on request
    assert plan(iso_gpt, 4 * TIB)["table"] == "gpt"                     # and automatically where MBR cannot represent it


def test_protective_mbr_image_can_never_use_mbr_slot(tmp_path):
    p = tmp_path / "p.iso"; make_iso(str(p), kind="gpt")
    with open(p, "r+b") as f:
        f.seek(446 + 16 * 2); f.write(struct.pack("<B3sB3sII", 0, b"\0\0\0", 0xEE, b"\0\0\0", 1, 100))
    img = vl.inspect_image(str(p))
    assert plan(img, 64 * GIB)["table"] == "gpt"
    with pytest.raises(vl.PlanError, match="slot 3"):
        plan(img, 64 * GIB, table="mbr")


def test_just_under_limit_stays_mbr(iso_mbr):
    disk = vl.MBR_MAX_SECTORS * 512 - 2 * GIB
    assert plan(iso_mbr, disk)["table"] == "mbr"


def test_4kn_rejected_before_anything_else(iso_gpt):
    with pytest.raises(vl.PlanError, match="4Kn"):
        plan(iso_gpt, 2 * TIB, logical=4096, physical=4096)


def test_512e_supported_and_aligned(iso_mbr):
    p = plan(iso_mbr, 500 * GIB, logical=512, physical=4096)
    assert (p["data_start_lba"] * 512) % 4096 == 0


def test_explicit_data_size_keeps_remainder_unallocated(iso_mbr):
    p = plan(iso_mbr, 500 * GIB, data_bytes=100 * GIB)
    assert p["data_bytes"] == 100 * GIB and p["unallocated_bytes"] > 390 * GIB
    with pytest.raises(vl.PlanError, match="exceeds"):
        plan(iso_mbr, 100 * GIB, data_bytes=200 * GIB)


def test_non_hybrid_image_rejected(tmp_path):
    p = tmp_path / "x.iso"; p.write_bytes(b"\0" * 4096)
    with pytest.raises(vl.PlanError, match="not a hybrid"):
        vl.inspect_image(str(p))


# ------------------------------------------------------------------ written structures
def _sparse_disk(tmp_path, size):
    d = tmp_path / "disk.img"
    with open(d, "wb") as f:
        try:
            f.truncate(size)
        except OSError:
            pytest.skip("filesystem cannot hold a sparse file this large")
    return str(d)


def _copy_image_head(iso, disk, nbytes=1 << 20):
    with open(iso, "rb") as s, open(disk, "r+b") as d:
        d.write(s.read(nbytes))


def test_gpt_written_to_sparse_4tib_disk_is_valid(tmp_path):
    iso = tmp_path / "g.iso"; make_iso(str(iso), kind="gpt")
    disk = _sparse_disk(tmp_path, 4 * TIB)
    _copy_image_head(iso, disk)
    img = vl.inspect_image(str(iso))
    p = vl.plan_layout(os.path.getsize(disk), img)
    assert p["table"] == "gpt"
    vl.apply_plan(disk, img, p)
    with open(disk, "rb") as f:
        parts = vl.validate_written_gpt(lambda o, n: (f.seek(o), f.read(n))[1], p["total_lba"], 512)
        names = [x["name"] for x in parts]
        assert names == ["ISO", "EFI", "VICTORDATA"]
        assert parts[-1]["start"] == p["data_start_lba"] and parts[-1]["end"] == p["data_end_lba"]
        assert parts[-1]["end"] + 1 > vl.MBR_MAX_SECTORS             # genuinely beyond what MBR can describe
        f.seek(0); mbr = f.read(512)
        assert mbr[510:512] == b"\x55\xaa" and mbr[:3] == b"\xeb\x3c\x90"      # boot code preserved
    # ISO's own partitions are preserved verbatim
    assert parts[0]["start"] == 64


def test_mbr_slot3_written_for_ordinary_disk(tmp_path):
    iso = tmp_path / "a.iso"; make_iso(str(iso))
    disk = _sparse_disk(tmp_path, 64 * GIB)
    _copy_image_head(iso, disk)
    img = vl.inspect_image(str(iso))
    p = vl.plan_layout(os.path.getsize(disk), img)
    vl.apply_plan(disk, img, p)
    with open(disk, "rb") as f:
        f.seek(446 + 32 + 8); st, ln = struct.unpack("<II", f.read(8))
        f.seek(446 + 16 + 4); assert f.read(1) == b"\xef"                 # image's own ESP entry untouched
    assert (st, ln) == (p["data_start_lba"], p["data_len_lba"])


def test_cli_reports_plan_without_writing(tmp_path, capsys):
    iso = tmp_path / "g.iso"; make_iso(str(iso), kind="gpt")
    assert vl.main(["plan", "--iso", str(iso), "--disk-bytes", str(3 * TIB)]) == 0
    out = capsys.readouterr().out
    assert "TABLE=gpt" in out and "DATA_BYTES=" in out
    assert vl.main(["plan", "--iso", str(iso), "--disk-bytes", str(2 * TIB), "--logical", "4096"]) == 3


def test_apply_refuses_before_modifying_when_plan_invalid(tmp_path):
    iso = tmp_path / "a.iso"; make_iso(str(iso))
    disk = tmp_path / "small.img"; disk.write_bytes(b"SENTINEL" * 64)
    # too small: plan fails, file untouched
    rc = vl.main(["apply", "--iso", str(iso), "--device", str(disk)])
    assert rc == 3 and disk.read_bytes() == b"SENTINEL" * 64


# ------------------------------------------------------------------ a genuine xorriso/GRUB hybrid image
import shutil
import subprocess


@pytest.mark.skipif(not shutil.which("grub-mkrescue") or not shutil.which("sgdisk"), reason="grub-mkrescue/sgdisk not installed")
def test_real_grub_hybrid_image_keeps_its_gpt_geometry_and_sgdisk_agrees(tmp_path):
    tree = tmp_path / "iso" / "boot" / "grub"; tree.mkdir(parents=True)
    (tree / "grub.cfg").write_text("echo hi\n")
    iso = tmp_path / "r.iso"
    r = subprocess.run(["grub-mkrescue", "-o", str(iso), str(tmp_path / "iso")], capture_output=True, text=True)
    if r.returncode != 0:
        pytest.skip("grub-mkrescue present but cannot build an image here (missing grub-pc-bin/grub-efi-amd64-bin/mtools?): " + r.stderr[-200:])
    img = vl.inspect_image(str(iso))
    assert img["gpt"] and img["gpt"]["entries_lba"] != 2          # array is NOT at the textbook location (APM blocks interleaved)
    disk = _sparse_disk(tmp_path, 3 * TIB)
    _copy_image_head(iso, disk, os.path.getsize(iso))
    p = vl.plan_layout(os.path.getsize(disk), img)
    assert p["table"] == "gpt" and p["data_end_lba"] > vl.MBR_MAX_SECTORS
    vl.apply_plan(disk, img, p)
    out = subprocess.run(["sgdisk", "-v", disk], capture_output=True, text=True).stdout     # independent implementation
    assert "No problems found" in out
    listing = subprocess.run(["sgdisk", "-p", disk], capture_output=True, text=True).stdout
    assert "VICTORDATA" in listing and "EFI boot partition" in listing
