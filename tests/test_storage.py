"""Storage destinations: identity, duplicates, removal, FAT32 limits, in-place models, CLI/GUI parity, no remote exposure."""
import importlib.machinery
import importlib.util
import json
import os
import types

import pytest

import victorcommon as tc
import victorstorage as vs
from test_common import make_gguf

GiB = 1 << 30


def vol(path="/dev/sdb1", uuid="AAAA-1111", fstype="exfat", mountpoint=None, label="Big", **kw):
    v = {"path": path, "uuid": uuid, "partuuid": kw.pop("partuuid", None), "label": label, "fstype": fstype, "size": 2 * 10**12,
         "kind": "usb-hdd", "removable": True, "transport": "usb", "model": "Passport", "serial": "WX1", "disk": path[:-1],
         "majmin": "8:17", "mountpoints": [mountpoint] if mountpoint else [], "mounted": bool(mountpoint), "mountpoint": mountpoint,
         "mounted_ro": False, "device_ro": False, "free": 10 * GiB, "used": 0, "locked": False, "boot_medium": False,
         "posix": fstype in ("ext4", "btrfs", "xfs"), "supported": fstype in vs.FILESYSTEMS,
         "max_file": vs.FILESYSTEMS.get(fstype, {}).get("max_file"), "note": None, "state_ok": fstype in ("ext4", "btrfs", "xfs")}
    v.update(kw)
    vs.flag_duplicates([v])
    return v


# ---------------------------------------------------------------- paths
@pytest.mark.parametrize("sub,out", [("", ""), ("AI/models", "AI/models"), ("My Docs/Ünïcode ✓", "My Docs/Ünïcode ✓"),
                                      ("a//b/./c", "a/b/c"), ("x\\y", "x/y")])
def test_subpath_accepts_spaces_unicode_and_normalises(sub, out):
    assert vs.validate_subpath(sub) == out


@pytest.mark.parametrize("bad", ["/etc", "../x", "a/../../b", "a\0b", "a\nb", "x" * 300, "a/" + "y" * 256, 5])
def test_subpath_rejects_escapes_and_junk(bad):
    with pytest.raises(vs.StorageError):
        vs.validate_subpath(bad)


def test_symlink_cannot_escape_the_drive(tmp_path):
    root = tmp_path / "drive"; root.mkdir()
    (tmp_path / "secret").mkdir()
    os.symlink(tmp_path / "secret", root / "link")
    with pytest.raises(vs.StorageError, match="outside"):
        vs.contained_path(str(root), "link")
    assert vs.contained_path(str(root), "ok/sub").startswith(str(root.resolve()))


# ---------------------------------------------------------------- identity
def test_identity_survives_device_renaming():
    a, b = vol("/dev/sdb1", "AAAA-1111"), vol("/dev/sdc1", "BBBB-2222", label="SSD")
    ref = vs.identity(a)
    assert "path" not in ref
    # next boot the kernel enumerates them the other way round
    a2, b2 = dict(a, path="/dev/sdc1"), dict(b, path="/dev/sdb1")
    got, st, _ = vs.resolve_volume([b2, a2], ref)
    assert st == "ok" and got["path"] == "/dev/sdc1"


def test_duplicate_uuid_is_ambiguous_not_guessed():
    a, b = vol("/dev/sdb1", "SAME-0001"), vol("/dev/sdc1", "SAME-0001")
    vs.flag_duplicates([a, b])
    assert a["duplicate_uuid"] and b["duplicate_uuid"]
    got, st, msg = vs.resolve_volume([a, b], {"uuid": "SAME-0001"})
    assert got is None and st == "ambiguous" and "/dev/sdb1" in msg
    with pytest.raises(vs.StorageError, match="same UUID"):
        vs.set_destination(vs.empty_config(), "models", a, "m")
    # a stored PARTUUID can still tell clones apart
    a["partuuid"], b["partuuid"] = "p-1", "p-2"
    got, st, _ = vs.resolve_volume([a, b], {"uuid": "SAME-0001", "partuuid": "p-2"})
    assert st == "ok" and got["path"] == "/dev/sdc1"


def test_duplicate_labels_flagged_and_selector_refuses_ambiguity():
    a, b = vol("/dev/sdb1", "U1", label="VICTORDATA"), vol("/dev/sdc1", "U2", label="VICTORDATA")
    vs.flag_duplicates([a, b])
    assert a["duplicate_label"]
    with pytest.raises(vs.StorageError, match="matches 2"):
        vs.select_candidates([a, b], "victordata")
    assert vs.select_candidates([a, b], "U2")["path"] == "/dev/sdc1"
    assert vs.select_candidates([a, b], "/dev/sdb1")["uuid"] == "U1"


def test_missing_drive_resolves_to_nothing():
    got, st, msg = vs.resolve_volume([vol()], {"uuid": "GONE-0000", "label": "Big"})
    assert got is None and st == "missing" and "not connected" in msg


# ---------------------------------------------------------------- selection rules
def test_selection_refuses_locked_unsupported_and_boot_volumes():
    cfg = vs.empty_config()
    with pytest.raises(vs.StorageError, match="locked"):
        vs.set_destination(cfg, "models", vol(locked=True, fstype="crypto_LUKS"), "")
    with pytest.raises(vs.StorageError, match="not supported"):
        vs.set_destination(cfg, "models", vol(fstype="hfsplus", supported=False), "")
    with pytest.raises(vs.StorageError, match="boot medium"):
        vs.set_destination(cfg, "models", vol(boot_medium=True), "")
    with pytest.raises(vs.StorageError, match="Unknown destination"):
        vs.set_destination(cfg, "state", vol(), "")


def test_ordinary_unlabelled_exfat_and_ext4_volumes_are_selectable_without_relabelling():
    cfg = vs.empty_config()
    vs.set_destination(cfg, "models", vol(label=None), "AI")
    vs.set_destination(cfg, "documents", vol("/dev/nvme0n1p3", "SSD-UUID", "ext4", label=""), "Docs é")
    assert cfg["destinations"]["models"][0]["ref"]["uuid"] == "AAAA-1111"
    assert "label" not in cfg["destinations"]["documents"][0]["ref"]
    vs.set_destination(cfg, "models", vol("/dev/sdd1", "CCCC-3333"), "more")
    assert len(cfg["destinations"]["models"]) == 2           # several model libraries at once
    vs.set_destination(cfg, "documents", vol("/dev/sdd1", "CCCC-3333"), "elsewhere")
    assert len(cfg["destinations"]["documents"]) == 1        # single-valued roles are replaced


# ---------------------------------------------------------------- active / missing / read-only
def configured(tmp_path, role="models", sub="AI", **vkw):
    mp = tmp_path / "mnt"; (mp / sub).mkdir(parents=True, exist_ok=True)
    v = vol(mountpoint=str(mp), **vkw)
    cfg = vs.empty_config(); vs.set_destination(cfg, role, v, sub)
    return cfg, v, mp


def test_active_destination_resolves_to_the_real_mount(tmp_path):
    cfg, v, mp = configured(tmp_path)
    rows = vs.destination_status(cfg, [v])["models"]
    assert rows[0]["status"] == "active" and rows[0]["path"] == os.path.realpath(mp / "AI") and rows[0]["persistent"]


def test_removed_drive_gives_no_path_and_no_fallback(tmp_path):
    cfg, v, mp = configured(tmp_path)
    rows = vs.destination_status(cfg, [])["models"]          # drive unplugged; the empty mount directory still exists on disk
    assert rows[0]["status"] == "missing" and rows[0]["path"] is None
    assert vs.usable_path(rows) is None and vs.usable_path(rows, need_write=False) is None
    assert vs.summarize(cfg, [], {"mode": "persistent"})["problems"]


def test_connected_but_unmounted_never_resolves_to_mountpoint_directory(tmp_path):
    cfg, v, mp = configured(tmp_path)
    v.update(mounted=False, mountpoint=None, mountpoints=[])
    rows = vs.destination_status(cfg, [v])["models"]
    assert rows[0]["status"] == "unmounted" and rows[0]["path"] is None


def test_read_only_mount_serves_models_but_not_saves(tmp_path):
    cfg, v, mp = configured(tmp_path)
    v.update(mounted_ro=True)
    rows = vs.destination_status(cfg, [v])["models"]
    assert rows[0]["status"] == "readonly" and not rows[0]["writable"]
    assert vs.usable_path(rows, need_write=True) is None
    assert vs.usable_path(rows, need_write=False) == rows[0]["path"]


def test_locked_volume_reports_locked(tmp_path):
    cfg, v, mp = configured(tmp_path)
    v.update(locked=True)
    assert vs.destination_status(cfg, [v])["models"][0]["status"] == "locked"


# ---------------------------------------------------------------- sizes
def test_fat32_refuses_oversize_before_copy_but_allows_the_maximum():
    with pytest.raises(vs.StorageError, match="FAT32.*4 GiB"):
        vs.check_fits(4 * GiB, "vfat")
    assert vs.check_fits(4 * GiB - 1, "vfat")
    for fs in ("ext4", "exfat", "ntfs", "btrfs", "xfs"):
        assert vs.check_fits(64 * GiB, fs)
    with pytest.raises(vs.StorageError, match="Not enough free space"):
        vs.check_fits(10 * GiB, "ext4", free=GiB)


def test_import_to_fat32_is_refused_before_any_bytes_are_copied(tmp_path, monkeypatch):
    src = tmp_path / "big.gguf"; make_gguf(src, pad=100)
    with open(src, "r+b") as f:
        f.truncate(5 * GiB)                                   # sparse
    dest = tmp_path / "usb"; dest.mkdir()
    monkeypatch.setattr(tc, "fstype_of_path", lambda p: "vfat")
    monkeypatch.setattr(tc.shutil, "disk_usage", lambda p: types.SimpleNamespace(free=10 * GiB))
    with pytest.raises(ValueError, match="FAT32"):
        tc.import_model(str(src), str(dest))
    assert os.listdir(dest) == []                             # nothing, not even a .part file


def test_import_large_file_allowed_on_ext4_like_destinations(tmp_path, monkeypatch):
    src = tmp_path / "m.gguf"; make_gguf(src, pad=100)
    with open(src, "r+b") as f:
        f.truncate(5 * GiB + 123)
    dest = tmp_path / "ssd"; dest.mkdir()
    monkeypatch.setattr(tc, "fstype_of_path", lambda p: "exfat")
    # only the pre-flight (limits) is exercised; the copy itself is covered by the existing import tests
    tc._check_dest_limits(str(dest), [src.stat().st_size])


# ---------------------------------------------------------------- lsblk parsing
def lsblk_fixture():
    return [
        {"name": "nvme0n1", "path": "/dev/nvme0n1", "type": "disk", "rm": False, "rota": False, "hotplug": False, "tran": "nvme",
         "model": "Samsung SSD", "serial": "S1", "ro": False, "maj:min": "259:0", "size": 10**12, "children": [
            {"name": "nvme0n1p2", "path": "/dev/nvme0n1p2", "type": "part", "fstype": "ntfs", "uuid": "NTFS-1", "label": "Windows", "size": 10**11,
             "maj:min": "259:2", "ro": False, "mountpoints": [None]}]},
        {"name": "sdb", "path": "/dev/sdb", "type": "disk", "rm": False, "rota": True, "hotplug": True, "tran": "usb", "model": "Elements",
         "serial": "WD1", "ro": False, "maj:min": "8:16", "size": 2 * 10**12, "children": [
            {"name": "sdb1", "path": "/dev/sdb1", "type": "part", "fstype": "exfat", "uuid": "EXF-1", "label": "Big", "size": 2 * 10**12,
             "maj:min": "8:17", "ro": False}]},
        {"name": "sdc", "path": "/dev/sdc", "type": "disk", "rm": True, "rota": False, "hotplug": True, "tran": "usb", "model": "Cruzer",
         "serial": "SD1", "ro": False, "maj:min": "8:32", "size": 64 * 10**9, "children": [
            {"name": "sdc1", "path": "/dev/sdc1", "type": "part", "fstype": "iso9660", "uuid": "ISO", "size": 2 * 10**9, "maj:min": "8:33"},
            {"name": "sdc3", "path": "/dev/sdc3", "type": "part", "fstype": "ext4", "uuid": "VD-1", "label": "VICTORDATA", "size": 60 * 10**9,
             "maj:min": "8:35", "ro": False}]},
        {"name": "mmcblk0", "path": "/dev/mmcblk0", "type": "disk", "rm": True, "rota": False, "tran": None, "model": "SD", "ro": False,
         "maj:min": "179:0", "size": 128 * 10**9, "children": [
            {"name": "mmcblk0p1", "path": "/dev/mmcblk0p1", "type": "part", "fstype": "vfat", "uuid": "FAT-1", "label": "CARD", "size": 128 * 10**9,
             "maj:min": "179:1", "ro": False}]},
        {"name": "sdd", "path": "/dev/sdd", "type": "disk", "tran": "usb", "rm": True, "rota": True, "model": "Enc", "ro": False,
         "maj:min": "8:48", "size": 10**12, "children": [
            {"name": "sdd1", "path": "/dev/sdd1", "type": "part", "fstype": "crypto_LUKS", "uuid": "LUKS-1", "size": 10**12, "maj:min": "8:49"}]},
    ]


def test_discovery_covers_internal_external_usb_sd_and_labels_states():
    mounts = {"8:35": [{"mountpoint": "/mnt/victordata", "fstype": "ext4", "ro": False, "options": "rw"}]}
    vols = {v["uuid"]: v for v in vs.volumes_from_tree(lsblk_fixture(), mounts, free=lambda p: (5 * GiB, GiB))}
    assert set(vols) == {"NTFS-1", "EXF-1", "VD-1", "FAT-1", "LUKS-1"}            # iso9660 and bare partitioned disks are not volumes
    assert vols["NTFS-1"]["kind"] == "internal-nvme" and vols["EXF-1"]["kind"] == "usb-hdd"
    assert vols["FAT-1"]["kind"] == "sd-card" and vols["FAT-1"]["max_file"] == vs.FAT32_MAX_FILE
    assert vols["LUKS-1"]["locked"] and not vols["LUKS-1"]["state_ok"]
    assert vols["VD-1"]["mounted"] and vols["VD-1"]["free"] == 5 * GiB and vols["VD-1"]["state_ok"]
    assert not vols["EXF-1"]["state_ok"] and vols["EXF-1"]["serial"] == "WD1" and vols["EXF-1"]["model"] == "Elements"


def test_boot_medium_partition_is_flagged():
    vols = vs.volumes_from_tree(lsblk_fixture(), {}, boot_devices={os.path.realpath("/dev/sdc3")})
    assert [v["uuid"] for v in vols if v["boot_medium"]] == ["VD-1"]


# ---------------------------------------------------------------- application state removal
def write_state(tmp_path, monkeypatch, **st):
    p = tmp_path / "persist.json"; p.write_text(json.dumps(st))
    monkeypatch.setattr(tc, "PERSIST_STATE", str(p))
    monkeypatch.delenv("VICTOR_DATA", raising=False)


def test_vanished_state_drive_stops_writes_instead_of_writing_into_ram(tmp_path, monkeypatch):
    mnt = tmp_path / "victordata"; root = mnt / "victorlinux"; root.mkdir(parents=True)
    write_state(tmp_path, monkeypatch, mode="persistent", data_root=str(root), mount=str(mnt))
    # the directory exists but nothing is mounted on it (drive pulled)
    st = tc.persist_state()
    assert st["mode"] == "missing" and tc.data_root() is None
    assert "disconnected" in st["message"]
    with pytest.raises(RuntimeError, match="No storage"):
        tc.save_settings(dict(tc.DEFAULT_SETTINGS))
    assert not (root / "config").exists()


def test_explicit_data_override_bypasses_mount_check(tmp_path, monkeypatch):
    monkeypatch.setenv("VICTOR_DATA", str(tmp_path))
    assert tc.data_root() == str(tmp_path)


# ---------------------------------------------------------------- models used in place
def test_models_on_another_drive_are_listed_in_place_including_split_sets(tmp_path, monkeypatch):
    data = tmp_path / "data"; (data / "models").mkdir(parents=True); (data / "config").mkdir()
    monkeypatch.setenv("VICTOR_DATA", str(data))
    ext = tmp_path / "bigdrive"; (ext / "LLM lib").mkdir(parents=True)
    make_gguf(ext / "LLM lib" / "single é.gguf", pad=10)
    for i in (1, 2):
        make_gguf(ext / "LLM lib" / f"split-0000{i}-of-00002.gguf", pad=10)
    v = vol(mountpoint=str(ext))
    cfg = vs.empty_config(); vs.set_destination(cfg, "models", v, "LLM lib", readonly_ok=True)
    (data / "config" / "storage.json").write_text(json.dumps(cfg))
    monkeypatch.setattr(tc, "volume_snapshot", lambda max_age=2.0: [v])
    names = {m["name"]: m for m in tc.list_models()}
    assert set(names) == {"single é.gguf", "split-00001-of-00002.gguf"}               # shard 2 is not a separate model
    assert names["single é.gguf"]["path"].startswith(os.path.realpath(ext))           # same bytes, not a copy
    assert tc.check_split_complete(names["split-00001-of-00002.gguf"]["path"])[0]
    # drive unplugged: the library silently disappears from the list rather than being served from a stale directory
    monkeypatch.setattr(tc, "volume_snapshot", lambda max_age=2.0: [])
    assert tc.list_models() == []


# ---------------------------------------------------------------- desktop integration
def test_links_and_bookmarks_for_ordinary_programs(tmp_path, monkeypatch):
    data = tmp_path / "data"; (data / "config").mkdir(parents=True)
    monkeypatch.setenv("VICTOR_DATA", str(data))
    drive = tmp_path / "ext drive"; (drive / "Save Here").mkdir(parents=True)
    v = vol(mountpoint=str(drive)); cfg = vs.empty_config(); vs.set_destination(cfg, "documents", v, "Save Here")
    (data / "config" / "storage.json").write_text(json.dumps(cfg))
    monkeypatch.setattr(tc, "volume_snapshot", lambda max_age=2.0: [v])
    home = tmp_path / "home"; home.mkdir()
    (home / ".config" / "gtk-3.0").mkdir(parents=True)
    (home / ".config" / "gtk-3.0" / "bookmarks").write_text("file:///srv/mine Mine\n")
    tc.link_destinations(str(home))
    link = home / "victor-storage" / "documents"
    assert os.path.islink(link) and os.path.realpath(link) == os.path.realpath(drive / "Save Here")
    bm = (home / ".config" / "gtk-3.0" / "bookmarks").read_text()
    assert "file:///srv/mine Mine" in bm and "Save%20Here" in bm and "VictorLinux documents" in bm
    # drive removed: link disappears, so a Save As cannot silently land elsewhere
    monkeypatch.setattr(tc, "volume_snapshot", lambda max_age=2.0: [])
    tc.link_destinations(str(home))
    assert not os.path.lexists(link)
    assert "VictorLinux documents" not in (home / ".config" / "gtk-3.0" / "bookmarks").read_text()


# ---------------------------------------------------------------- privileged helper (pure parts)
@pytest.fixture
def priv(monkeypatch):
    loader = importlib.machinery.SourceFileLoader("victor_priv", os.path.join(os.path.dirname(__file__), "..", "airootfs", "usr", "local", "lib", "victorlinux", "victor-priv"))
    spec = importlib.util.spec_from_loader("victor_priv", loader)
    m = importlib.util.module_from_spec(spec); loader.exec_module(m)
    monkeypatch.setattr(m.pwd, "getpwnam", lambda n: types.SimpleNamespace(pw_uid=1000, pw_gid=1000))
    return m


def test_mount_options_never_force_and_never_allow_exec_or_suid(priv):
    for fs in ("ext4", "exfat", "ntfs", "vfat", "btrfs", "xfs"):
        o = priv.mount_options(fs, False).split(",")
        assert {"nosuid", "nodev", "noexec", "rw"} <= set(o) and "force" not in o and "remount" not in o
    assert "uid=1000" in priv.mount_options("exfat", False) and "windows_names" in priv.mount_options("ntfs", False)
    assert priv.mount_options("ext4", True).endswith(",ro")
    assert "fmask=0133" in priv.mount_options("vfat", False)          # files are never executable


def test_priv_exposes_storage_commands_but_no_format_or_partition_command(priv):
    assert {"list-storage", "volume-mount", "volume-umount", "dest-prepare", "dest-apply"} <= set(priv.COMMANDS)
    assert not [c for c in priv.COMMANDS if any(w in c for w in ("format", "mkfs", "wipe", "partition", "label", "chown", "repair", "fsck"))]
    src = open(priv.__file__).read()
    for forbidden in ("mkfs", "wipefs", "sgdisk", "parted", "fsck", "-o\", \"force", "chown -R", "e2label", "tune2fs"):
        assert forbidden not in src, forbidden


# ---------------------------------------------------------------- Control Center API <-> CLI parity, no remote exposure
import http.client
import control_center as cc
import gateway
from test_control_api import serving, request


@pytest.fixture
def control(tmp_path, monkeypatch):
    monkeypatch.setattr(cc, "TOKEN_FILE", str(tmp_path / "cache" / "cc.token"))
    monkeypatch.setenv("VICTOR_DATA", str(tmp_path / "data"))
    (tmp_path / "data" / "config").mkdir(parents=True)
    monkeypatch.setattr(cc, "APP", cc.App(), raising=False)
    with serving(cc.Handler) as port:
        yield port, {"X-Victor-Token": cc.APP.token}, tmp_path


def test_api_and_cli_share_one_implementation(control, monkeypatch):
    port, h, tmp = control
    drive = tmp / "d"; (drive / "Models").mkdir(parents=True)
    v = vol(mountpoint=str(drive))
    monkeypatch.setattr(tc, "volume_snapshot", lambda max_age=2.0: [v])
    monkeypatch.setattr(tc, "link_destinations", lambda home=None: None)
    calls = []
    monkeypatch.setattr(tc, "priv", lambda a, **k: (calls.append(a), (0, json.dumps({"mountpoint": str(drive), "rw": True})))[1])
    st, body = request(port, "POST", "/api/storage/dest", {"role": "models", "drive": "AAAA-1111", "folder": "Models"}, h)
    assert st == 200, body
    assert calls == [["dest-prepare", "AAAA-1111", "Models"]]
    cli = tc.storage_status()["destinations"]["models"][0]
    assert cli["status"] == "active" and cli["path"] == os.path.realpath(drive / "Models")
    st, body = request(port, "POST", "/api/storage/dest", {"role": "models", "drive": "AAAA-1111", "folder": "../escape"}, h)
    assert st == 400 and b".." in body
    st, body = request(port, "POST", "/api/storage/dest/clear", {"role": "models", "uuid": "AAAA-1111"}, h)
    assert st == 200 and "models" not in tc.storage_status()["destinations"]
    assert (drive / "Models").is_dir()                       # clearing a choice never deletes anything


def test_dest_api_requires_the_control_center_token(control):
    port, h, tmp = control
    st, _ = request(port, "POST", "/api/storage/dest", {"role": "models", "drive": "x"})
    assert st == 401


def test_choice_without_any_state_location_is_refused_with_explanation(tmp_path, monkeypatch):
    monkeypatch.delenv("VICTOR_DATA", raising=False)
    monkeypatch.setattr(tc, "PERSIST_STATE", str(tmp_path / "none.json"))
    monkeypatch.setattr(tc, "volume_snapshot", lambda max_age=2.0: [vol(mountpoint=str(tmp_path))])
    monkeypatch.setattr(tc, "priv", lambda *a, **k: (0, "{}"))
    with pytest.raises(vs.StorageError, match="nowhere to save"):
        tc.storage_use("documents", "AAAA-1111", "")


def test_temporary_session_choice_is_reported_as_not_durable(tmp_path, monkeypatch):
    data = tmp_path / "t"; (data / "config").mkdir(parents=True)
    st = tmp_path / "p.json"; st.write_text(json.dumps({"mode": "temporary", "data_root": str(data)}))
    monkeypatch.delenv("VICTOR_DATA", raising=False); monkeypatch.setattr(tc, "PERSIST_STATE", str(st))
    d = tmp_path / "drv"; (d / "x").mkdir(parents=True)
    monkeypatch.setattr(tc, "volume_snapshot", lambda max_age=2.0: [vol(mountpoint=str(d))])
    monkeypatch.setattr(tc, "link_destinations", lambda home=None: None)
    monkeypatch.setattr(tc, "priv", lambda *a, **k: (0, "{}"))
    r = tc.storage_use("backups", "AAAA-1111", "x")
    assert r["durable"] is False and "lost on reboot" in r["message"]


def test_gateway_never_exposes_local_disks(monkeypatch):
    class Upstream(cc.Handler):
        def do_GET(self):
            self._send(404, "x")
    with serving(Upstream) as up:
        monkeypatch.setattr(gateway.Handler, "cfg", {"require_auth": False, "keys": [], "upstream_port": up, "upstream_key": "k", "max_body": 1024})
        with serving(gateway.Handler) as port:
            for path in ("/api/storage", "/api/storage/dest", "/api/volumes", "/files/etc/passwd", "/victor/storage"):
                status, data = request(port, "GET", path)
                assert status != 200 or b"partitions" not in data, path
                assert b"/dev/" not in data and b"uuid" not in data.lower(), path
    src = open(gateway.__file__).read() + open(os.path.join(os.path.dirname(gateway.__file__), "companion.py")).read()
    assert "victorstorage" not in src and "list-storage" not in src and "volume-mount" not in src
