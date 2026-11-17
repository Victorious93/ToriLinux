"""USB tool upgrades without root: in-place update planning/writing on sparse files, refusals, script safety gates."""
import os
import struct
import subprocess

import pytest

import test_layout_planner as tlp

vl = tlp.vl
GIB, MIB, TIB = tlp.GIB, tlp.MIB, tlp.TIB
REPO = os.path.realpath(os.path.join(os.path.dirname(__file__), ".."))
SCRIPT = os.path.join(REPO, "scripts", "make-usb.sh")


def fresh(tmp_path, size, kind="mbr", iso_size=1900 * MIB):
    iso = tmp_path / f"old-{kind}.iso"; tlp.make_iso(str(iso), size=iso_size, kind=kind)
    disk = tlp._sparse_disk(tmp_path, size)
    tlp._copy_image_head(iso, disk)
    img = vl.inspect_image(str(iso))
    plan = vl.plan_layout(os.path.getsize(disk), img, image_headroom_bytes=512 * MIB)
    vl.apply_plan(disk, img, plan)
    with open(disk, "r+b") as f:                                     # pretend there is a filesystem with data
        f.seek(plan["data_start_lba"] * 512); f.write(b"VICTORDATA-CONTENT" * 100)
    return disk, plan


def data_bytes(disk, plan):
    with open(disk, "rb") as f:
        f.seek(plan["data_start_lba"] * 512); return f.read(1800)


@pytest.mark.parametrize("kind,size", [("mbr", 64 * GIB), ("gpt", 4 * TIB)])
def test_update_keeps_data_partition_sectors_and_contents(tmp_path, kind, size):
    disk, plan = fresh(tmp_path, size, kind)
    cur = vl.current_data_partition(disk)
    assert cur["table"] == plan["table"] and cur["start_lba"] == plan["data_start_lba"] and cur["len_lba"] == plan["data_len_lba"]
    new = tmp_path / "new.iso"; tlp.make_iso(str(new), size=1950 * MIB, kind=kind)
    tlp._copy_image_head(new, disk)                                  # what `dd` of the new image does to the system area
    img = vl.inspect_image(str(new))
    up = vl.plan_update(os.path.getsize(disk), img, 512, 512, cur)
    vl.apply_update(disk, img, up)
    after = vl.current_data_partition(disk)
    assert (after["start_lba"], after["len_lba"], after["table"]) == (cur["start_lba"], cur["len_lba"], cur["table"])
    if kind == "gpt":
        assert after["part_guid"] == cur["part_guid"]               # PARTUUID preserved
    assert data_bytes(disk, plan) == b"VICTORDATA-CONTENT" * 100


def test_headroom_moves_only_the_data_start(tmp_path):
    iso = tmp_path / "h.iso"; tlp.make_iso(str(iso))
    img = vl.inspect_image(str(iso))
    a, b = vl.plan_layout(64 * GIB, img), vl.plan_layout(64 * GIB, img, image_headroom_bytes=2048 * MIB)
    assert b["data_start_lba"] - a["data_start_lba"] == 2048 * MIB // 512 and b["data_end_lba"] == a["data_end_lba"]
    with pytest.raises(vl.PlanError):
        vl.plan_layout(64 * GIB, img, image_headroom_bytes=-1)


def test_update_refuses_image_that_would_reach_the_data(tmp_path):
    disk, plan = fresh(tmp_path, 64 * GIB)
    cur = vl.current_data_partition(disk)
    big = tmp_path / "big.iso"; tlp.make_iso(str(big), size=plan["data_start_lba"] * 512 + 4 * MIB)
    with pytest.raises(vl.PlanError, match="overwrite your data"):
        vl.plan_update(os.path.getsize(disk), vl.inspect_image(str(big)), 512, 512, cur)
    with pytest.raises(vl.PlanError, match="no VICTORDATA"):
        vl.plan_update(os.path.getsize(disk), vl.inspect_image(str(big)), 512, 512, None)
    with pytest.raises(vl.PlanError, match="Ventoy"):
        vl.plan_update(os.path.getsize(disk), vl.inspect_image(str(big)), 512, 512, dict(cur, ventoy=True))
    with pytest.raises(vl.PlanError, match="4Kn"):
        vl.plan_update(os.path.getsize(disk), vl.inspect_image(str(big)), 4096, 4096, cur)


def test_cli_update_refusal_leaves_disk_untouched_and_recovery_flags_work(tmp_path, capsys):
    disk, plan = fresh(tmp_path, 64 * GIB)
    big = tmp_path / "big.iso"; tlp.make_iso(str(big), size=plan["data_start_lba"] * 512 + 4 * MIB)
    with open(disk, "rb") as f:
        before = f.read(1 << 20)
    assert vl.main(["update-apply", "--iso", str(big), "--device", disk]) == 3
    with open(disk, "rb") as f:
        assert f.read(1 << 20) == before
    # interrupted update: the new image's system area already overwrote the table; recover with the printed values
    new = tmp_path / "new.iso"; tlp.make_iso(str(new), size=1950 * MIB)
    tlp._copy_image_head(new, disk)
    assert vl.main(["current", "--device", disk]) == 3                      # entry lost, data still there
    assert vl.main(["update-apply", "--iso", str(new), "--device", disk, "--keep-start", str(plan["data_start_lba"]),
                    "--keep-len", str(plan["data_len_lba"]), "--keep-table", "mbr"]) == 0
    with open(disk, "rb") as f:
        f.seek(446 + 32 + 8); assert struct.unpack("<II", f.read(8)) == (plan["data_start_lba"], plan["data_len_lba"])
    assert data_bytes(disk, plan) == b"VICTORDATA-CONTENT" * 100


def test_script_gates():
    src = open(SCRIPT).read()
    for p in ("/run/archiso/bootmnt", "/run/live/medium", "/run/initramfs/live"):
        assert p in src                                                     # the medium this session booted from is refused
    assert "--confirm-serial" in src and "conv=fsync,notrunc" in src
    env = dict(os.environ, PATH=os.environ["PATH"])
    if os.geteuid() != 0:
        for sub in ("update", "verify"):
            r = subprocess.run(["bash", SCRIPT, sub, "--iso", SCRIPT, "--device", "/dev/null"], capture_output=True, text=True, env=env)
            assert r.returncode != 0 and "root" in r.stderr
    r = subprocess.run(["bash", SCRIPT, "update", "--iso", SCRIPT, "--device", "/dev/null", "--keep-start", "abc"], capture_output=True, text=True)
    assert r.returncode != 0 and "sector number" in r.stderr
    r = subprocess.run(["bash", SCRIPT, "update", "--iso", SCRIPT, "--device", "/dev/null", "--keep-start", "2048"], capture_output=True, text=True)
    assert r.returncode != 0 and "--keep-len" in r.stderr
