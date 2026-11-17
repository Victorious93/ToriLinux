"""victor-bt-pair: dry-run command plan and confirm-before-trust. No Bluetooth hardware required."""
import json
import os
import subprocess
import sys
from pathlib import Path

BIN = Path(__file__).resolve().parents[1] / "airootfs/usr/local/bin/victor-bt-pair"


def run(*args, env=None):
    e = os.environ.copy()
    if env:
        e.update(env)
    p = subprocess.run([sys.executable, str(BIN), *args], capture_output=True, text=True, env=e)
    return p.returncode, json.loads(p.stdout) if p.stdout.strip().startswith("{") else p.stdout, p.stderr


def test_dry_run_start_list_unpair_does_not_enable_boot(tmp_path, monkeypatch):
    env = {"VICTOR_BT_DIR": str(tmp_path)}
    rc, j, _ = run("--dry-run", "start", env=env)
    assert rc == 0 and j["dry_run"] is True and j["timeout"] == 120
    cmds = [" ".join(c) for c in j["commands"]]
    assert "systemctl start bluetooth" in cmds
    assert "bluetoothctl power on" in cmds
    assert "bluetoothctl pairable on" in cmds
    assert "bluetoothctl discoverable on" in cmds
    # Must not enable the service on boot.
    assert all("enable" not in c for c in cmds)
    rc, j, _ = run("--dry-run", "list", env=env)
    assert rc == 0 and j["dry_run"] is True
    rc, j, err = run("--dry-run", "unpair", "not-a-mac", env=env)
    assert rc == 1 and "AA:BB" in j["error"]
    rc, j, _ = run("--dry-run", "unpair", "AA:BB:CC:DD:EE:FF", env=env)
    assert rc == 0 and any(c[-1] == "AA:BB:CC:DD:EE:FF" and c[-2] == "remove" for c in j["commands"])
    rc, j, _ = run("--dry-run", "status", env=env)
    assert j["default"] == "off" and j["active"] is False


def test_confirm_refuses_when_no_window(tmp_path):
    env = {"VICTOR_BT_DIR": str(tmp_path)}
    rc, j, _ = run("confirm", env=env)
    assert rc == 1 and "No pairing window" in j["error"]
