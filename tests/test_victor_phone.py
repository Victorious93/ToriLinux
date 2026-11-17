"""Tests for the optional victor-phone helper and package-list wiring."""
from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ARCH_HELPER = ROOT / "airootfs/usr/local/bin/victor-phone"
DEB_HELPER = (
    ROOT
    / "debian-edition/config/includes.chroot_after_packages/usr/local/bin/victor-phone"
)
HOOK = ROOT / "debian-edition/config/hooks/live/0250-optional-scrcpy.hook.chroot"
ARCH_PACKAGES = ROOT / "packages.x86_64.jinja2"
DEB_DIAG = ROOT / "debian-edition/config/package-lists/40-dev-diag.list.chroot"
NOTICE = ROOT / "NOTICE"


def run_helper(*args, env=None, check=False):
    return subprocess.run(
        [str(ARCH_HELPER), *args],
        capture_output=True,
        text=True,
        env=env,
        check=check,
    )


def dry_run(*args):
    result = run_helper("--dry-run", *args)
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    return result.stdout.splitlines()


def test_arch_and_debian_helpers_are_identical():
    assert ARCH_HELPER.is_file()
    assert DEB_HELPER.is_file()
    assert ARCH_HELPER.read_bytes() == DEB_HELPER.read_bytes()
    for path in (ARCH_HELPER, DEB_HELPER, HOOK):
        mode = path.stat().st_mode
        assert mode & stat.S_IXUSR, f"{path} must be executable"


def test_dry_run_default_args():
    assert dry_run() == ["scrcpy", "--window-title=VictorLinux Phone"]


def test_dry_run_no_audio():
    assert dry_run("--no-audio") == [
        "scrcpy",
        "--window-title=VictorLinux Phone",
        "--no-audio",
    ]


def test_dry_run_audio_source_and_record_and_serial():
    assert dry_run(
        "--audio-source",
        "mic",
        "--record",
        "/tmp/phone-session.mp4",
        "--serial",
        "ABC123",
    ) == [
        "scrcpy",
        "--window-title=VictorLinux Phone",
        "--audio-source=mic",
        "--record=/tmp/phone-session.mp4",
        "--serial=ABC123",
    ]


def test_dry_run_equals_forms_and_extra_args():
    assert dry_run(
        "--audio-source=output",
        "--record=/tmp/out.mkv",
        "--window-title=Test Phone",
        "--",
        "--turn-screen-off",
        "-b",
        "8M",
    ) == [
        "scrcpy",
        "--window-title=Test Phone",
        "--audio-source=output",
        "--record=/tmp/out.mkv",
        "--turn-screen-off",
        "-b",
        "8M",
    ]


def test_no_audio_and_audio_source_conflict():
    result = run_helper("--no-audio", "--audio-source", "mic")
    assert result.returncode == 2
    assert "either --no-audio or --audio-source" in result.stderr


@pytest.mark.parametrize(
    "args, snippet",
    [
        (["--audio-source"], "--audio-source needs a value"),
        (["--record"], "--record needs a file path"),
        (["--serial"], "--serial needs a device serial"),
        (["--window-title"], "--window-title needs a value"),
        (["--record="], "--record needs a file path"),
        (["--unknown-flag"], "unknown option"),
        (["surprise"], "unexpected argument"),
    ],
)
def test_bad_args(args, snippet):
    result = run_helper(*args)
    assert result.returncode == 2
    assert snippet in result.stderr


def test_help_mentions_permissions_and_no_download():
    result = run_helper("--help")
    assert result.returncode == 0
    text = result.stdout
    assert "udev" in text.lower()
    assert "USB debugging" in text
    assert "Nothing is downloaded" in text
    assert "--record" in text


def _empty_path_env(bin_dir: Path | None = None):
    env = os.environ.copy()
    # Keep coreutils (cat, awk, grep) but do not inherit a full PATH that
    # might hide the missing-binary case we want to test.
    parts = [str(bin_dir)] if bin_dir is not None else []
    parts.extend(["/usr/bin", "/bin"])
    env["PATH"] = ":".join(parts)
    env.pop("DISPLAY", None)
    env.pop("WAYLAND_DISPLAY", None)
    return env


def test_missing_scrcpy_message(tmp_path):
    result = run_helper(env=_empty_path_env())
    assert result.returncode == 1
    err = result.stderr
    assert "scrcpy is not installed" in err
    assert "optional" in err.lower()
    assert "Nothing is downloaded automatically" in err
    assert "trixie-backports" in err


def test_missing_adb_message(tmp_path):
    scrcpy = tmp_path / "scrcpy"
    scrcpy.write_text("#!/bin/sh\nexit 0\n")
    scrcpy.chmod(0o755)
    result = run_helper(env=_empty_path_env(tmp_path))
    assert result.returncode == 1
    assert "adb is not installed" in result.stderr


def test_dry_run_does_not_need_scrcpy():
    result = run_helper("--dry-run", env=_empty_path_env())
    assert result.returncode == 0
    assert result.stdout.splitlines()[0] == "scrcpy"


def _package_names(block: str) -> set[str]:
    names = set()
    for line in block.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith("{%"):
            continue
        names.add(stripped)
    return names


def test_arch_package_template_marks_scrcpy_gui_only():
    text = ARCH_PACKAGES.read_text()
    # android-tools / android-udev are in the base list, before the GUI block.
    base, gui = text.split('{% if type == "gui" %}', 1)
    base_pkgs = _package_names(base)
    gui_pkgs = _package_names(gui)
    assert "android-tools" in base_pkgs
    assert "android-udev" in base_pkgs
    assert "scrcpy" not in base_pkgs
    assert "scrcpy" in gui_pkgs


def test_debian_package_list_keeps_scrcpy_optional():
    text = DEB_DIAG.read_text()
    packages = [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    assert "adb" in packages
    assert "android-sdk-platform-tools-common" in packages
    assert "scrcpy" not in packages
    assert "0250-optional-scrcpy.hook.chroot" in text


def test_optional_hook_never_fails_the_build():
    text = HOOK.read_text()
    assert "set -e" not in text
    assert "Never fail the image build" in text or "Never fail" in text
    assert text.strip().endswith("exit 0")
    # Backports source must not remain on the live image.
    assert "cleanup" in text
    assert "rm -f" in text
    proc = subprocess.run(["bash", "-n", str(HOOK)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


def test_notice_credits_scrcpy_and_keeps_torilinux():
    text = NOTICE.read_text()
    assert "ToriLinux" in text
    assert "sasha0552" in text
    assert "Genymobile/scrcpy" in text
    assert "Apache-2.0" in text


def test_docs_and_launchers_exist():
    assert (ROOT / "docs/phone-mirroring.md").is_file()
    docs = (ROOT / "docs/phone-mirroring.md").read_text()
    assert "Genymobile/scrcpy" in docs
    assert "Apache-2.0" in docs
    assert "No phone was connected" in docs
    arch_desktop = ROOT / "airootfs/usr/share/applications/victor-phone.desktop"
    deb_desktop = (
        ROOT
        / "debian-edition/config/includes.chroot_after_packages/usr/share/applications/victor-phone.desktop"
    )
    assert arch_desktop.read_text() == deb_desktop.read_text()
    assert "Name=VictorLinux Phone" in arch_desktop.read_text()
    assert "Exec=/usr/local/bin/victor-phone-gui" in arch_desktop.read_text()
    menu = (ROOT / "airootfs/home/victor/.config/openbox/menu.xml").read_text()
    assert "victor-phone-gui" in menu
    assert "VictorLinux Phone" in menu
    assert (ROOT / "docs/phone-mirroring.md").read_text().count("victor-phone") >= 1
