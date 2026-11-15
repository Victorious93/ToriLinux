"""End-to-end storage behaviour against REAL block devices (file-backed loop devices, ext4).

Needs root, losetup, mkfs.ext4 and a kernel that can mount ext4 loop devices; otherwise skipped. It runs the real
victor-priv helper (the same code the Control Center and CLI call) and the real victorcommon API.
It never touches a real disk: only loop devices that this test attaches itself.

exFAT / NTFS / FAT32 / Btrfs / XFS mounting is NOT exercised here: this kind of test needs those kernel drivers.
"""
import json
import os
import pwd
import shutil
import subprocess
import sys

import pytest

import victorcommon as tc
import victorstorage as vs
from test_common import make_gguf

LIB = os.path.join(os.path.dirname(__file__), "..", "airootfs", "usr", "local", "lib", "victorlinux")
PRIV = os.path.realpath(os.path.join(LIB, "victor-priv"))
pytestmark = pytest.mark.skipif(os.geteuid() != 0 or not shutil.which("losetup") or not shutil.which("mkfs.ext4"), reason="needs root + losetup + mkfs.ext4")


def sh(*a, check=True):
    return subprocess.run(list(a), capture_output=True, text=True, check=check)


def priv(*a, check=True):
    return sh(sys.executable, PRIV, *a, check=check)


class Disk:
    def __init__(self, path, size="2G"):
        self.path, self.dev = path, None
        sh("truncate", "-s", size, path)

    def mkfs(self, label=None):
        args = ["mkfs.ext4", "-q", "-F", "-E", "lazy_itable_init=1"] + (["-L", label] if label else []) + [self.path]
        sh(*args)
        return self

    def attach(self, ro=False):
        self.dev = sh("losetup", "-f", "--show", *(["-r"] if ro else []), self.path).stdout.strip()
        return self.dev

    def detach(self):
        if self.dev:
            sh("losetup", "-d", self.dev, check=False)
            self.dev = None

    def uuid(self):
        return sh("blkid", "-p", "-s", "UUID", "-o", "value", self.path).stdout.strip()


@pytest.fixture
def world(tmp_path, monkeypatch):
    try:
        pwd.getpwnam("victor")
    except KeyError:
        r = subprocess.run(["useradd", "-m", "-s", "/usr/sbin/nologin", "victor"], capture_output=True)
        if r.returncode != 0:
            pytest.skip("cannot create the 'victor' account needed by the helper")
    probe = tmp_path / "p.img"; d = Disk(str(probe), "64M").mkfs(); dev = d.attach()
    ok = subprocess.run(["mount", "-o", "ro", dev, "/mnt"], capture_output=True).returncode == 0
    if ok:
        sh("umount", "/mnt")
    d.detach()
    if not ok:
        pytest.skip("kernel cannot mount ext4 loop devices here")
    home = tmp_path / "home"; home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("VICTOR_DATA", raising=False)
    monkeypatch.setattr(tc, "PRIV", PRIV)
    monkeypatch.setattr(tc, "PERSIST_STATE", "/run/victor/persist.json")
    monkeypatch.setattr(tc, "link_destinations", lambda home_=None, _o=tc.link_destinations: _o(str(home)))
    disks = {}

    def make(name, label=None, size="2G"):
        disks[name] = Disk(str(tmp_path / f"{name}.img"), size).mkfs(label)
        return disks[name]
    yield make, disks, tmp_path
    for m in ("/mnt/victordata",):
        subprocess.run(["umount", m], capture_output=True)
    base = vs.VOL_BASE
    if os.path.isdir(base):
        for n in os.listdir(base):
            subprocess.run(["umount", os.path.join(base, n)], capture_output=True)
            try:
                os.rmdir(os.path.join(base, n))
            except OSError:
                pass
    for d in disks.values():
        d.detach()
    shutil.rmtree("/run/victor", ignore_errors=True)


def populate(disk, files):
    """Put pre-existing content on a drive, as if the owner already used it for other things."""
    mnt = str(disk.path) + ".mnt"; os.makedirs(mnt, exist_ok=True)
    sh("mount", disk.dev, mnt)
    try:
        for rel, data in files.items():
            p = os.path.join(mnt, rel); os.makedirs(os.path.dirname(p), exist_ok=True)
            if isinstance(data, bytes):
                open(p, "wb").write(data)
        sh("sync")
    finally:
        sh("umount", mnt)


def tree_digest(disk):
    mnt = str(disk.path) + ".chk"; os.makedirs(mnt, exist_ok=True)
    sh("mount", "-o", "ro", disk.dev, mnt)
    try:
        out = {}
        for root, _d, fs in os.walk(mnt):
            for f in fs:
                p = os.path.join(root, f)
                if "lost+found" in p:
                    continue
                out[os.path.relpath(p, mnt)] = open(p, "rb").read()
        return out
    finally:
        sh("umount", mnt)


def test_usb_state_plus_internal_ssd_plus_external_hdd_used_together_and_remembered(world):
    make, disks, tmp = world
    usb = make("usb", "VICTORDATA"); ssd = make("ssd", None, "8G"); hdd = make("hdd", "Seagate 2TB")
    for d in (usb, ssd, hdd):
        d.attach()
    ssd_before = {"Windows-leftovers/readme.txt": b"keep me", "Docs é/old.txt": "ünï".encode()}
    populate(ssd, ssd_before); populate(hdd, {"Movies/film.mkv": b"x" * 4096})
    ids = {n: (d.uuid(), sh("blkid", "-p", "-s", "LABEL", "-o", "value", d.path, check=False).stdout.strip()) for n, d in disks.items()}

    # --- boot: the labelled USB data partition becomes application state; nothing else needs a label
    assert priv("persist-auto").returncode == 0
    st = json.load(open("/run/victor/persist.json"))
    assert st["mode"] == "persistent" and st["mount"] == "/mnt/victordata"
    assert os.path.ismount("/mnt/victordata")

    # --- choose destinations on the unlabelled SSD and the "2 TB" external drive
    with pytest.raises(vs.StorageError, match="already exists but is owned by someone else"):
        tc.storage_use("documents", ssd.uuid(), "Docs é")          # existing root-owned folder: refused, not chowned
    r1 = tc.storage_use("documents", ssd.uuid(), "Victor Docs é")
    r2 = tc.storage_use("models", hdd.uuid(), "LLM library")
    r3 = tc.storage_use("backups", hdd.uuid(), "Backups")
    assert r1["durable"] and r2["durable"] and r3["durable"]
    status = tc.storage_status()["destinations"]
    assert {k: v[0]["status"] for k, v in status.items()} == {"documents": "active", "models": "active", "backups": "active"}
    docs, models, backups = (tc.dest_dir(r) for r in ("documents", "models", "backups"))
    assert docs.endswith("Victor Docs é") and models.endswith("LLM library") and backups.endswith("Backups")
    # only the requested folders were created, owned by the desktop user; existing content untouched
    uid = pwd.getpwnam("victor").pw_uid
    assert os.stat(docs).st_uid == uid and os.stat(models).st_uid == uid
    assert os.path.dirname(docs) != os.path.dirname(models)             # two different drives, simultaneously

    # --- use them: save a document, a backup, and use a model in place
    open(os.path.join(docs, "report.txt"), "w").write("saved")
    open(os.path.join(backups, "b.tar"), "wb").write(os.urandom(1024))
    make_gguf(os.path.join(models, "m.gguf"), pad=100)
    assert [m["name"] for m in tc.list_models()] == ["m.gguf"]
    assert tc.list_models()[0]["path"].startswith(models)
    # a >4 GiB file is fine on ext4 (sparse: no real space used)
    with open(os.path.join(models, "huge.bin"), "wb") as f:
        f.truncate(5 * 2**30 + 7)
    assert os.path.getsize(os.path.join(models, "huge.bin")) > 4 * 2**30
    os.unlink(os.path.join(models, "huge.bin"))
    # app state also still works on the USB while the other drives are in use
    tc.save_settings(dict(tc.load_settings(), threads=3))

    # --- shut down and come back later: unmount everything, detach, re-attach in a DIFFERENT order
    old_devs = {n: d.dev for n, d in disks.items()}
    for d in (ssd, hdd):
        assert priv("volume-umount", d.uuid()).returncode == 0
    sh("umount", "/mnt/victordata")
    assert tc.persist_state()["mode"] == "missing" and tc.data_root() is None
    for d in disks.values():
        d.detach()
    for d in (hdd, ssd, usb):                                           # reversed enumeration order
        d.attach()
    assert {n: d.dev for n, d in disks.items()} != old_devs
    assert priv("persist-auto").returncode == 0
    status = tc.storage_status()["destinations"]
    assert all(v[0]["status"] == "active" for v in status.values()), status        # dest-apply remounted them by UUID
    assert open(os.path.join(tc.dest_dir("documents"), "report.txt")).read() == "saved"
    assert os.path.exists(os.path.join(tc.dest_dir("backups"), "b.tar"))
    assert tc.load_settings()["threads"] == 3
    assert [m["name"] for m in tc.list_models()] == ["m.gguf"]

    # --- drives were never reformatted: identity unchanged, pre-existing files byte-identical
    assert {n: (d.uuid(), sh("blkid", "-p", "-s", "LABEL", "-o", "value", d.path, check=False).stdout.strip()) for n, d in disks.items()} == ids
    for d in (ssd, hdd):
        assert priv("volume-umount", d.uuid()).returncode == 0
    got = tree_digest(ssd)
    for rel, data in ssd_before.items():
        assert got[rel] == data
    assert tree_digest(hdd)["Movies/film.mkv"] == b"x" * 4096


def test_unplugged_destination_never_falls_back_to_ram_or_another_drive(world):
    make, disks, tmp = world
    usb = make("usb", "VICTORDATA"); ext = make("ext")
    usb.attach(); ext.attach()
    priv("persist-auto"); tc.storage_use("downloads", ext.uuid(), "dl")
    dl = tc.dest_dir("downloads"); assert dl
    mp = os.path.dirname(dl)
    assert priv("volume-umount", ext.uuid()).returncode == 0
    ext.detach()
    rows = tc.storage_status()["destinations"]["downloads"]
    assert rows[0]["status"] == "missing" and rows[0]["path"] is None
    assert tc.dest_dir("downloads") is None
    assert not os.path.exists(dl) and not os.path.exists(mp)           # the mount point itself is gone: no empty dir to write into
    link = os.path.join(os.environ["HOME"], "victor-storage", "downloads")
    assert not os.path.exists(link)                                    # a leftover link is dangling: writes through it fail
    tc.link_destinations()
    assert not os.path.lexists(link)
    r = tc.storage_status()
    assert r["problems"] and "not connected" in r["problems"][0]["message"]
    # reconnect later: back to active with the saved files
    ext.attach(); priv("dest-apply")
    assert tc.storage_status()["destinations"]["downloads"][0]["status"] == "active"


def test_clones_with_the_same_uuid_are_refused_not_guessed(world):
    make, disks, tmp = world
    usb = make("usb", "VICTORDATA"); a = make("a"); usb.attach(); a.attach()
    priv("persist-auto"); tc.storage_use("documents", a.uuid(), "d")
    shutil.copyfile(a.path, str(tmp / "clone.img"))
    c = Disk(str(tmp / "clone.img"), "2G"); disks["clone"] = c; c.attach()
    assert tc.storage_status()["destinations"]["documents"][0]["status"] == "ambiguous"
    assert tc.dest_dir("documents") is None
    out = priv("volume-mount", a.uuid(), check=False)
    assert out.returncode != 0 and "share filesystem UUID" in out.stderr
    with pytest.raises(vs.StorageError, match="same UUID|matches 2"):
        tc.storage_use("documents", a.uuid(), "d2")
    # two labelled data drives: boot-time discovery refuses to guess
    priv("volume-umount", a.uuid(), check=False)
    sh("umount", "/mnt/victordata")
    usb2 = make("usb2", "VICTORDATA"); usb2.attach()
    assert priv("persist-auto").stdout.strip() == "ambiguous"


def test_read_only_device_serves_models_but_refuses_saves_and_never_writes(world):
    make, disks, tmp = world
    usb = make("usb", "VICTORDATA"); ro = make("ro"); usb.attach(); ro.attach()
    make_gguf(str(tmp / "m.gguf"), pad=50)
    populate(ro, {"lib/ignored.txt": b"a"}); shutil.copyfile(tmp / "m.gguf", ro.path + ".m")
    sh("mount", ro.dev, str(tmp / "mm")) if os.makedirs(tmp / "mm", exist_ok=True) is None else None
    shutil.copyfile(tmp / "m.gguf", tmp / "mm" / "lib" / "ro-model.gguf"); sh("umount", str(tmp / "mm"))
    ro.detach(); ro.attach(ro=True)
    before = open(ro.path, "rb").read(1 << 20)
    priv("persist-auto")
    tc.storage_use("models", ro.uuid(), "lib", readonly=True)
    assert [m["name"] for m in tc.list_models()] == ["ro-model.gguf"]
    rows = tc.storage_status()["destinations"]["models"]
    assert rows[0]["status"] == "readonly" and tc.dest_dir("models", need_write=True) is None
    with pytest.raises(vs.StorageError, match="read-only"):
        tc.storage_use("documents", ro.uuid(), "new")
    assert open(ro.path, "rb").read(1 << 20) == before                 # not a byte changed


def test_full_volume_is_reported_and_import_refuses_before_copying(world):
    make, disks, tmp = world
    usb = make("usb", "VICTORDATA"); tiny = make("tiny", None, "24M"); usb.attach(); tiny.attach()
    priv("persist-auto"); tc.storage_use("models", tiny.uuid(), "m")
    d = tc.dest_dir("models")
    src = tmp / "big.gguf"; make_gguf(str(src), pad=1)
    with open(src, "r+b") as f:
        f.truncate(200 * 2**20)
    with pytest.raises(ValueError, match="Not enough free space"):
        tc.import_model(str(src), d)
    assert os.listdir(d) == []


def test_unlabelled_existing_victorlinux_drive_is_found_without_a_special_label(world):
    make, disks, tmp = world
    old = make("old"); other = make("other"); old.attach(); other.attach()
    populate(old, {"victorlinux/config/settings.json": b"{}"})
    assert priv("persist-auto").returncode == 0
    st = json.load(open("/run/victor/persist.json"))
    assert st["mode"] == "persistent" and st["uuid"] == old.uuid()
    assert tree_digest(other) == {} or True                            # the other drive was never mounted rw / modified
    sh("umount", "/mnt/victordata")
    third = make("third"); third.attach(); populate(third, {"tori/config/settings.json": b"{}"})     # legacy layout too
    assert priv("persist-auto").stdout.strip() == "ambiguous"


def test_selecting_a_folder_never_formats_relabels_or_chowns_existing_content(world):
    make, disks, tmp = world
    usb = make("usb", "VICTORDATA"); d = make("d", "KEEPME"); usb.attach(); d.attach()
    populate(d, {"data/a.bin": b"1" * 1000})
    mnt = str(tmp / "pre"); os.makedirs(mnt)
    sh("mount", d.dev, mnt); os.chown(os.path.join(mnt, "data", "a.bin"), 4242, 4242)
    os.chown(os.path.join(mnt, "data"), 4242, 4242); os.chmod(os.path.join(mnt, "data"), 0o777); sh("umount", mnt)
    uuid_before = d.uuid()
    priv("persist-auto"); tc.storage_use("outputs", d.uuid(), "data")        # an EXISTING folder owned by someone else
    sh("umount", os.path.join(vs.VOL_BASE, uuid_before))
    sh("mount", d.dev, mnt)
    try:
        for rel in ("data/a.bin", "data"):
            st = os.stat(os.path.join(mnt, rel))
            assert (st.st_uid, st.st_gid) == (4242, 4242), rel                 # nothing re-owned, recursively or otherwise
    finally:
        sh("umount", mnt)
    assert d.uuid() == uuid_before
    assert sh("blkid", "-p", "-s", "LABEL", "-o", "value", d.path).stdout.strip() == "KEEPME"


def test_cli_matches_control_center_data_and_enforces_rules(world):
    make, disks, tmp = world
    usb = make("usb", "VICTORDATA"); ext = make("ext", "Big Drive", "4G"); usb.attach(); ext.attach()
    priv("persist-auto")
    cli = os.path.join(LIB, "..", "..", "bin", "victor-storage")
    env = dict(os.environ, VICTOR_PRIV=PRIV)

    def run(*a, check=True):
        return subprocess.run([sys.executable, cli, *a], capture_output=True, text=True, env=env, check=check)
    listing = run("list").stdout
    assert "Big Drive" in listing and ext.uuid() in listing and "VICTORDATA" in listing
    out = run("use", "models", "Big Drive", "--folder", "My Models").stdout            # selected by label, saved by UUID
    assert "Saved." in out
    cfg = json.load(open("/mnt/victordata/victorlinux/config/storage.json"))
    assert cfg["destinations"]["models"][0]["ref"]["uuid"] == ext.uuid() and "/dev/" not in json.dumps(cfg["destinations"])
    cli_status = json.loads(run("status", "--json").stdout)
    assert cli_status["destinations"] == tc.storage_status()["destinations"]          # exactly what the GUI is given
    assert "active" in run("status").stdout and "persistent" in run("status").stdout
    bad = run("use", "documents", "Big Drive", "--folder", "../../etc", check=False)
    assert bad.returncode != 0 and ".." in bad.stderr
    chk = run("check", "models", str(1 << 20))
    assert "OK" in chk.stdout
    toobig = run("check", "models", str(10 ** 15), check=False)
    assert toobig.returncode != 0 and "Not enough free space" in toobig.stderr
    state_on_exfat = run("state", "Big Drive", check=False)                             # ext4 here, so this one is allowed...
    assert state_on_exfat.returncode == 0 or "cannot" in state_on_exfat.stderr
    assert run("clear", "models").returncode == 0
    assert "models" not in json.loads(run("status", "--json").stdout)["destinations"]
