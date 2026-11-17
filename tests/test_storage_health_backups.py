"""Storage health (SMART + free space) and encrypted backups with a verified, confirmed restore."""
import json
import os
import shutil
import subprocess
import sys
import tarfile
import io
from collections import namedtuple

import pytest

import diskhealth
import vbackup
from test_control_api import control, request  # noqa: F401  (fixture reuse)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEB_LIB = os.path.join(ROOT, "debian-edition/config/includes.chroot_after_packages/usr/local/lib/victorlinux")
DEB_CTL = os.path.join(ROOT, "debian-edition/config/includes.chroot_after_packages/usr/local/bin/victor-ctl")
needs_gpg = pytest.mark.skipif(not shutil.which("gpg"), reason="gpg not installed")
PASS = "blue lantern river 42"


def ata(passed=True, attrs=None, temp=35):
    return {"device": {"name": "/dev/sda"}, "model_name": "Disk", "smart_status": {"passed": passed}, "temperature": {"current": temp},
            "ata_smart_attributes": {"table": [{"id": k, "raw": {"value": v}} for k, v in (attrs or {}).items()]}}


def test_smart_verdicts():
    assert diskhealth.parse_smart(ata())["status"] == "ok"
    assert diskhealth.parse_smart(ata(False))["status"] == "bad"
    assert diskhealth.parse_smart(ata(attrs={5: 3}))["status"] == "warn"
    assert diskhealth.parse_smart(ata(attrs={5: 80}))["status"] == "bad"
    assert diskhealth.parse_smart(ata(attrs={198: 1}))["status"] == "bad"
    r = diskhealth.parse_smart(ata(attrs={199: 4}))
    assert r["status"] == "warn" and "cable" in r["findings"][0]["fix"]
    assert diskhealth.parse_smart(ata(temp=65))["status"] == "warn"
    nv = {"device": {"name": "/dev/nvme0n1"}, "smart_status": {"passed": True},
          "nvme_smart_health_information_log": {"critical_warning": 0, "percentage_used": 85, "available_spare": 100, "available_spare_threshold": 10}}
    assert diskhealth.parse_smart(nv)["status"] == "warn"
    nv["nvme_smart_health_information_log"]["available_spare"] = 5
    assert diskhealth.parse_smart(nv)["status"] == "bad"
    usb = diskhealth.parse_smart({"device": {"name": "/dev/sdb"}, "smartctl": {"messages": [{"string": "Unknown USB bridge"}]}})
    assert usb["status"] == "unknown" and "USB" in usb["findings"][0]["fix"]      # missing data is never reported as healthy


def test_device_validation_and_partition_mapping():
    for good in ("/dev/sda", "/dev/nvme0n1", "/dev/mmcblk0", "/dev/vdb"):
        assert diskhealth.valid_disk(good)
    for bad in ("/dev/sda1", "/dev/../etc/passwd", "sda", "/dev/sda;rm", "/dev/loop0", ""):
        assert not diskhealth.valid_disk(bad)
    R = namedtuple("R", "returncode stdout stderr")
    assert diskhealth.disk_of("/dev/nvme0n1p2", run=lambda *a, **k: R(0, "nvme0n1\n", "")) == "/dev/nvme0n1"
    assert diskhealth.disk_of("/dev/sda1", run=lambda *a, **k: R(0, "loop0\n", "")) is None
    assert diskhealth.disk_of("/dev/mapper/x") is None
    with pytest.raises(ValueError):
        diskhealth.collect_smart("/etc/passwd")


def test_free_space_levels(tmp_path, monkeypatch):
    U = namedtuple("U", "total used free")
    G = diskhealth.GiB
    for free, total, want in ((50 * G, 100 * G, "ok"), (4 * G, 100 * G, "warn"), (int(0.5 * G), 100 * G, "bad"), (30 * G, 1000 * G, "warn"), (8 * G, 1000 * G, "bad")):
        monkeypatch.setattr(diskhealth.shutil, "disk_usage", lambda p, f=free, t=total: U(t, t - f, f))
        assert diskhealth.free_space([("data", str(tmp_path))])[0]["level"] == want
    assert diskhealth.free_space([("gone", str(tmp_path / "missing"))]) == []
    assert diskhealth.report([], [])["status"] == "unknown"


def state(root):
    (root / "config").mkdir(parents=True)
    (root / "chats").mkdir()
    (root / "config" / "settings.json").write_text('{"ctx": 4096}')
    (root / "config" / "secrets.json").write_text('{"upstream_key": "TOPSECRETKEY"}')
    (root / "chats" / "one.json").write_text('{"msg": "hello"}')
    os.symlink("/etc/passwd", root / "chats" / "link.json")


@needs_gpg
def test_backup_roundtrip_secrets_and_restore(tmp_path):
    root = tmp_path / "data"; state(root)
    with pytest.raises(vbackup.BackupError):
        vbackup.create(str(root), str(tmp_path / "b"), "short", ["config"])
    r = vbackup.create(str(root), str(tmp_path / "b"), PASS, ["config", "chats"], ["config/secrets.json"])
    raw = open(r["path"], "rb").read()
    assert oct(os.stat(r["path"]).st_mode & 0o777) == "0o600" and r["files"] == 2 and not r["includes_secrets"]
    assert b"TOPSECRETKEY" not in raw and b"4096" not in raw and b"hello" not in raw     # encrypted, secrets excluded
    info = vbackup.inspect(r["path"], PASS, ["config", "chats"])
    assert {f["path"] for f in info["files"]} == {"config/settings.json", "chats/one.json"}     # symlink skipped
    with pytest.raises(vbackup.BackupError, match="Wrong passphrase"):
        vbackup.inspect(r["path"], "not the passphrase", ["config", "chats"])
    with pytest.raises(vbackup.BackupError, match="confirm"):
        vbackup.restore(r["path"], PASS, str(root), ["config", "chats"], str(tmp_path / "b"))
    (root / "config" / "settings.json").write_text('{"ctx": 1}')
    res = vbackup.restore(r["path"], PASS, str(root), ["config", "chats"], str(tmp_path / "b"), confirm=True)
    assert res["restored"] == 2 and json.loads((root / "config" / "settings.json").read_text())["ctx"] == 4096
    assert json.loads(open(os.path.join(res["previous_dir"], "config/settings.json")).read())["ctx"] == 1
    s2 = vbackup.create(str(root), str(tmp_path / "c"), PASS, ["config", "chats"], ["config/secrets.json"], include_secrets=True)
    assert vbackup.inspect(s2["path"], PASS, ["config", "chats"])["includes_secrets"] is True
    assert vbackup.list_backups(str(tmp_path / "b"))[0]["name"] == os.path.basename(r["path"])


@needs_gpg
def test_restore_refuses_unsafe_or_tampered_archives(tmp_path):
    def forge(entries, manifest_files=None):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as t:
            man = {"format": vbackup.FORMAT, "version": 1, "files": manifest_files if manifest_files is not None else
                   [{"path": n[5:], "size": len(b), "sha256": vbackup._sha(b)} for n, b in entries]}
            for n, b in [("manifest.json", json.dumps(man).encode())] + entries:
                ti = tarfile.TarInfo(n); ti.size = len(b); t.addfile(ti, io.BytesIO(b))
        p = tmp_path / ("x%d.vlbak" % len(os.listdir(tmp_path)))
        p.write_bytes(vbackup.encrypt(buf.getvalue(), PASS))
        return str(p)
    for bad in ("data/../../etc/cron.d/x", "data//etc/passwd", "data/models/evil.gguf", "outside.txt"):
        with pytest.raises(vbackup.BackupError):
            vbackup.inspect(forge([(bad, b"x")]), PASS, ["config", "chats"])
    with pytest.raises(vbackup.BackupError, match="Checksum"):
        vbackup.inspect(forge([("data/config/a.json", b"x")], [{"path": "config/a.json", "size": 1, "sha256": "0" * 64}]), PASS, ["config"])
    p = tmp_path / "plain.vlbak"; p.write_bytes(b"not encrypted")
    with pytest.raises(vbackup.BackupError):
        vbackup.inspect(str(p), PASS, ["config"])


@needs_gpg
def test_passphrase_never_on_command_line(tmp_path, monkeypatch):
    seen = []
    real = subprocess.run

    def spy(cmd, *a, **k):
        seen.append(list(cmd))
        return real(cmd, *a, **k)
    monkeypatch.setattr(vbackup.subprocess, "run", spy)
    root = tmp_path / "d"; state(root)
    vbackup.create(str(root), str(tmp_path / "b"), PASS, ["config"])
    assert seen and not any(PASS in " ".join(c) for c in seen)
    assert PASS not in json.dumps(dict(os.environ))


@needs_gpg
def test_health_and_backup_api(control, tmp_path):  # noqa: F811
    port, headers = control
    os.makedirs(tmp_path / "data", exist_ok=True)
    j = json.loads(request(port, "GET", "/api/health?refresh=1", None, headers)[1])
    assert j["space"][0]["label"] == "VictorLinux data" and j["status"] in ("ok", "warn", "bad", "unknown")
    os.makedirs(tmp_path / "data" / "config", exist_ok=True)
    (tmp_path / "data" / "config" / "settings.json").write_text('{"ctx": 2048}')
    (tmp_path / "data" / "config" / "secrets.json").write_text('{"upstream_key": "TOPSECRETKEY"}')
    st, raw = request(port, "POST", "/api/backups", {"action": "create", "passphrase": PASS, "confirm_passphrase": "different one!!"}, headers)
    assert st == 400 and b"do not match" in raw
    st, raw = request(port, "POST", "/api/backups", {"action": "create", "passphrase": PASS, "confirm_passphrase": PASS}, headers)
    assert st == 200 and PASS.encode() not in raw, raw
    name = json.loads(request(port, "GET", "/api/backups", None, headers)[1])["backups"][0]["name"]
    st, raw = request(port, "POST", "/api/backups", {"action": "inspect", "name": name, "passphrase": PASS}, headers)
    assert st == 200 and b"secrets.json" not in raw
    st, raw = request(port, "POST", "/api/backups", {"action": "inspect", "name": "../../etc/passwd", "passphrase": PASS}, headers)
    assert st == 400
    st, raw = request(port, "POST", "/api/backups", {"action": "inspect", "path": "/etc/passwd", "passphrase": PASS}, headers)
    assert st == 400
    st, raw = request(port, "POST", "/api/backups", {"action": "restore", "name": name, "passphrase": PASS}, headers)
    assert st == 400 and b"confirm" in raw
    st, raw = request(port, "POST", "/api/backups", {"action": "restore", "name": name, "passphrase": PASS, "confirm": True}, headers)
    assert st == 200 and json.loads(raw)["restored"] == 1


def test_debian_uses_the_same_modules():
    for m in ("diskhealth.py", "vbackup.py"):
        a = open(os.path.join(ROOT, "airootfs/usr/local/lib/victorlinux", m), "rb").read()
        assert a == open(os.path.join(DEB_LIB, "victorlib", m), "rb").read(), m


@needs_gpg
def test_debian_ctl_backup_via_stdin(tmp_path):
    data = tmp_path / "data"
    for sub in ("settings", "chat", "secrets"):
        (data / sub).mkdir(parents=True)
    (data / "settings" / "llama.json").write_text('{"ctx": 4096}')
    (data / "secrets" / "api.key").write_text("TOPSECRETKEY")
    os.symlink(data, tmp_path / "root")
    env = dict(os.environ, VICTOR_RUN=str(tmp_path), PYTHONPATH=DEB_LIB, HOME=str(tmp_path))

    def ctl(*args, stdin=PASS + "\n", rc=0):
        r = subprocess.run([sys.executable, DEB_CTL, "--json", *args], env=env, capture_output=True, text=True, timeout=120, input=stdin)
        assert r.returncode == rc, r.stderr
        assert PASS not in r.stdout + r.stderr
        return json.loads(r.stdout) if rc == 0 else r.stderr
    made = ctl("backup", "create")
    assert made["files"] == 1 and b"TOPSECRETKEY" not in open(made["path"], "rb").read()
    name = ctl("backup", "list")[0]["name"]
    assert [f["path"] for f in ctl("backup", "inspect", name)["files"]] == ["settings/llama.json"]
    assert "confirm" in ctl("backup", "restore", name, rc=2)
    assert ctl("backup", "restore", name, "--yes")["restored"] == 1
    assert "Wrong passphrase" in ctl("backup", "inspect", name, stdin="nope nope nope\n", rc=2)
