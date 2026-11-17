"""Tests for VictorLinux Phone (Tk GUI) and victor-phone-headless."""
from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

import victorphone as vp

ROOT = Path(__file__).resolve().parents[1]
ARCH_LIB = ROOT / "airootfs/usr/local/lib/victorlinux/victorphone.py"
DEB_LIB = ROOT / "debian-edition/config/includes.chroot_after_packages/usr/local/lib/victorlinux/victorphone.py"
ARCH_GUI = ROOT / "airootfs/usr/local/bin/victor-phone-gui"
DEB_GUI = ROOT / "debian-edition/config/includes.chroot_after_packages/usr/local/bin/victor-phone-gui"
ARCH_HEAD = ROOT / "airootfs/usr/local/bin/victor-phone-headless"
DEB_HEAD = ROOT / "debian-edition/config/includes.chroot_after_packages/usr/local/bin/victor-phone-headless"
ARCH_PACKAGES = ROOT / "packages.x86_64.jinja2"
HOOK = ROOT / "debian-edition/config/hooks/live/0250-optional-scrcpy.hook.chroot"


def test_arch_and_debian_phone_app_files_are_identical():
    pairs = ((ARCH_LIB, DEB_LIB), (ARCH_GUI, DEB_GUI), (ARCH_HEAD, DEB_HEAD))
    for left, right in pairs:
        assert left.is_file(), left
        assert right.is_file(), right
        assert left.read_bytes() == right.read_bytes(), f"{left} != {right}"
    for path in (ARCH_LIB, DEB_LIB, ARCH_GUI, DEB_GUI, ARCH_HEAD, DEB_HEAD):
        assert path.stat().st_mode & stat.S_IXUSR, f"{path} must be executable"


def test_palette_is_brand_dark_neon():
    assert vp.PALETTE["bg"].upper() == "#0B0F1A"
    assert vp.PALETTE["accent"].upper() == "#00E5FF"
    assert vp.PALETTE["accent2"].upper() == "#FF2BD6"
    whites = {"#ffffff", "#fff", "white", "#ffffff"}
    for key, value in vp.PALETTE.items():
        assert value.lower() not in whites, f"{key} is white: {value}"
        assert value.startswith("#"), f"{key} should be a hex color"


def test_scrcpy_argv_defaults_match_victor_phone():
    assert vp.scrcpy_argv() == ["scrcpy", "--window-title=VictorLinux Phone"]


def test_scrcpy_argv_flags():
    assert vp.scrcpy_argv(
        no_audio=True,
        record_file="/tmp/phone-session.mp4",
        serial="ABC123",
        stay_awake=True,
        turn_screen_off=True,
        no_window=True,
    ) == [
        "scrcpy",
        "--window-title=VictorLinux Phone",
        "--no-audio",
        "--record=/tmp/phone-session.mp4",
        "--serial=ABC123",
        "--stay-awake",
        "--turn-screen-off",
        "--no-window",
    ]


def test_scrcpy_argv_audio_source_and_extra():
    assert vp.scrcpy_argv(
        audio_source="mic",
        window_title="Test Phone",
        extra=["--turn-screen-off", "-b", "8M"],
    ) == [
        "scrcpy",
        "--window-title=Test Phone",
        "--audio-source=mic",
        "--turn-screen-off",
        "-b",
        "8M",
    ]


def test_scrcpy_argv_rejects_conflicts_and_bad_source():
    with pytest.raises(ValueError, match="either --no-audio or --audio-source"):
        vp.scrcpy_argv(no_audio=True, audio_source="mic")
    with pytest.raises(ValueError, match="audio source"):
        vp.scrcpy_argv(audio_source="hdmi")


def test_build_gui_argv_default_source_omits_flag():
    assert "--audio-source" not in " ".join(vp.build_gui_argv(audio=True, audio_source="default"))
    assert "--no-audio" in vp.build_gui_argv(audio=False)
    assert "--audio-source=output" in vp.build_gui_argv(audio=True, audio_source="output")


def test_parse_adb_devices():
    text = (
        "List of devices attached\n"
        "ABC123\tdevice product:panther model:Pixel_7 device:panther\n"
        "DEF456\tunauthorized\n"
        "* daemon started successfully\n"
        "GHI789\tno permissions\n"
    )
    devices = vp.parse_adb_devices(text)
    assert [d.serial for d in devices] == ["ABC123", "DEF456", "GHI789"]
    assert devices[0].ready and devices[0].model == "Pixel 7"
    assert devices[0].label() == "Pixel 7 (ABC123)  ready"
    assert not devices[1].ready
    assert "unauthorized" in devices[1].label()
    assert vp.device_hint([devices[0]]) is None
    assert "not authorized" in (vp.device_hint([devices[1]]) or "")
    assert "no permissions" in (vp.device_hint([devices[2]]) or "")
    assert "No authorized phone" in (vp.device_hint([]) or "")


def test_parse_adb_devices_space_separated():
    devices = vp.parse_adb_devices("XYZ device\n")
    assert devices == [vp.Device(serial="XYZ", state="device", extras="")]


def test_missing_help_mentions_optional_and_no_download():
    text = vp.missing_help("scrcpy")
    assert "scrcpy is not installed" in text
    assert "optional" in text.lower()
    assert "Nothing is downloaded automatically" in text
    assert "trixie-backports" in text
    assert "udev" in text.lower()


def _bin_env(bin_dir: Path, extra: dict[str, str] | None = None):
    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}:/usr/bin:/bin"
    env.pop("DISPLAY", None)
    env.pop("WAYLAND_DISPLAY", None)
    if extra:
        env.update(extra)
    return env


def _write_exec(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(0o755)


def run_headless(*args, env=None):
    return subprocess.run(
        [str(ARCH_HEAD), *args],
        capture_output=True,
        text=True,
        env=env,
    )


def test_headless_help():
    result = run_headless("--help")
    assert result.returncode == 0
    text = result.stdout
    assert "udev" in text.lower()
    assert "USB debugging" in text
    assert "Nothing is downloaded" in text
    assert "screencap" in text
    assert "record" in text


def test_headless_unknown_command_and_args():
    bad = run_headless("explode")
    assert bad.returncode == 2
    assert "unknown command" in bad.stderr
    unexpected = run_headless("status", "--nope")
    assert unexpected.returncode == 2
    assert "unexpected argument" in unexpected.stderr


def test_headless_dry_run_record_and_mirror():
    rec = run_headless("record", "/tmp/out.mp4", "--dry-run", "--no-audio", "--serial", "ABC")
    assert rec.returncode == 0, rec.stderr
    assert rec.stdout.splitlines() == [
        "scrcpy",
        "--window-title=VictorLinux Phone",
        "--no-audio",
        "--record=/tmp/out.mp4",
        "--serial=ABC",
        "--no-window",
    ]
    mir = run_headless(
        "mirror",
        "--dry-run",
        "--audio-source",
        "output",
        "--stay-awake",
        "--",
        "-b",
        "8M",
    )
    assert mir.returncode == 0, mir.stderr
    assert mir.stdout.splitlines() == [
        "scrcpy",
        "--window-title=VictorLinux Phone",
        "--audio-source=output",
        "--stay-awake",
        "-b",
        "8M",
    ]


def test_headless_record_needs_file():
    result = run_headless("record")
    assert result.returncode == 2
    assert "record needs a file path" in result.stderr


def test_headless_dry_run_does_not_need_scrcpy():
    env = _bin_env(Path("/tmp"))  # no scrcpy on this narrow path besides coreutils
    env["PATH"] = "/usr/bin:/bin"
    result = run_headless("mirror", "--dry-run", env=env)
    assert result.returncode == 0
    assert result.stdout.splitlines()[0] == "scrcpy"


def test_headless_missing_scrcpy_on_record(tmp_path):
    result = run_headless("record", str(tmp_path / "x.mp4"), env=_bin_env(tmp_path))
    assert result.returncode == 1
    assert "scrcpy is not installed" in result.stderr
    assert "Nothing is downloaded automatically" in result.stderr


def test_headless_devices_and_status_and_screencap(tmp_path):
    _write_exec(
        tmp_path / "adb",
        r'''#!/usr/bin/env python3
import sys
args = sys.argv[1:]
if args[:1] == ["-s"]:
    args = args[2:]
if args[:1] == ["devices"]:
    sys.stdout.write("List of devices attached\nABC123\tdevice product:panther model:Pixel_7\n")
elif args[:1] == ["wait-for-device"]:
    sys.exit(0)
elif args[:2] == ["exec-out", "screencap"]:
    sys.stdout.buffer.write(b"\x89PNG\r\n\x1a\nDATA")
else:
    sys.stderr.write("unexpected: %s\n" % (args,))
    sys.exit(9)
''',
    )
    env = _bin_env(tmp_path)
    devices = run_headless("devices", env=env)
    assert devices.returncode == 0, devices.stderr
    assert "Pixel 7 (ABC123)  ready" in devices.stdout

    status = run_headless("status", "--json", env=env)
    assert status.returncode == 0, status.stderr
    payload = json.loads(status.stdout)
    assert payload["adb"].endswith("/adb")
    assert payload["scrcpy"] is None
    assert payload["display"] is False
    assert payload["devices"][0]["serial"] == "ABC123"
    assert payload["devices"][0]["ready"] is True
    assert payload["hint"] is None

    shot = tmp_path / "screen.png"
    cap = run_headless("screencap", str(shot), "--serial", "ABC123", env=env)
    assert cap.returncode == 0, cap.stderr
    data = shot.read_bytes()
    assert data.startswith(b"\x89PNG")
    assert b"DATA" in data

    waited = run_headless("wait", env=env)
    assert waited.returncode == 0, waited.stderr
    assert "Phone connected" in waited.stdout


def test_headless_status_without_adb(tmp_path):
    result = run_headless("status", env=_bin_env(tmp_path))
    assert result.returncode == 1
    assert "adb is not installed" in result.stderr


def test_english_only_and_no_gettext():
    source = ARCH_LIB.read_text()
    assert "gettext" not in source
    assert "locale.setlocale" not in source
    for needle in ("VICTORLINUX  ·  PHONE", "Start mirroring", "No authorized phone was found"):
        assert needle in source


def test_arch_gui_packages_include_tk_and_keep_scrcpy_gui_only():
    text = ARCH_PACKAGES.read_text()
    base, gui = text.split('{% if type == "gui" %}', 1)
    def names(block: str) -> set[str]:
        out = set()
        for line in block.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or stripped.startswith("{%"):
                continue
            out.add(stripped)
        return out
    base_pkgs, gui_pkgs = names(base), names(gui)
    assert "android-tools" in base_pkgs
    assert "android-udev" in base_pkgs
    assert "scrcpy" not in base_pkgs
    assert "tk" not in base_pkgs
    assert "scrcpy" in gui_pkgs
    assert "tk" in gui_pkgs


def test_docs_launchers_and_ci_mention_gui_and_headless():
    docs = (ROOT / "docs/phone-mirroring.md").read_text()
    assert "victor-phone-gui" in docs
    assert "victor-phone-headless" in docs
    assert "VictorLinux Phone" in docs
    assert "Genymobile/scrcpy" in docs
    desktop = (ROOT / "airootfs/usr/share/applications/victor-phone.desktop").read_text()
    deb_desktop = (
        ROOT
        / "debian-edition/config/includes.chroot_after_packages/usr/share/applications/victor-phone.desktop"
    ).read_text()
    assert desktop == deb_desktop
    assert "Name=VictorLinux Phone" in desktop
    assert "Exec=/usr/local/bin/victor-phone-gui" in desktop
    menu = (ROOT / "airootfs/home/victor/.config/openbox/menu.xml").read_text()
    assert "victor-phone-gui" in menu
    assert "VictorLinux Phone" in menu
    workflow = (ROOT / ".github/workflows/validate.yml").read_text()
    assert "victor-phone-gui" in workflow
    assert "victor-phone-headless" in workflow
    assert "victorphone.py" in workflow
    notice = (ROOT / "NOTICE").read_text()
    assert "ToriLinux" in notice and "sasha0552" in notice
    assert "Genymobile/scrcpy" in notice


def test_gui_missing_tkinter_message(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "tkinter" or name.startswith("tkinter."):
            raise ImportError("No module named tkinter")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    rc = vp.main_gui([])
    assert rc == 1


def test_optional_hook_still_never_fails_the_build():
    text = HOOK.read_text()
    assert "set -e" not in text
    assert text.strip().endswith("exit 0")
