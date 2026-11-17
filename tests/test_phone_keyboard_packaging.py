"""Packages, hooks and both editions stay off-by-default for Bluetooth and victor-input."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _read(*parts):
    return (ROOT.joinpath(*parts)).read_text()


def test_packages_include_bluez_and_gui_blueman():
    arch = _read("packages.x86_64.jinja2")
    assert "bluez\n" in arch and "bluez-utils" in arch
    assert arch.index("bluez\n") < arch.index("{% if type == \"gui\" %}")
    assert "blueman" in arch
    assert arch.index("blueman") > arch.index("{% if type == \"gui\" %}")
    assert "bluez" in _read("debian-edition/config/package-lists/10-base.list.chroot")
    assert "blueman" in _read("debian-edition/config/package-lists/20-desktop.list.chroot")


def test_hooks_disable_bluetooth_and_do_not_enable_input():
    arch = _read("airootfs/root/customize_airootfs.sh.jinja2")
    deb = _read("debian-edition/config/hooks/live/0200-victor-config.hook.chroot")
    for text in (arch, deb):
        assert "systemctl disable bluetooth.service" in text
        assert "systemctl disable victor-input.service" in text
        assert "enable bluetooth" not in text
        assert "enable victor-input" not in text
    assert "Hidden=true" in deb


def test_debian_copy_matches_arch_module_and_helpers():
    pairs = [
        ("airootfs/usr/local/lib/victorlinux/victorinput.py",
         "debian-edition/config/includes.chroot_after_packages/usr/local/lib/victorlinux/victorinput.py"),
        ("airootfs/usr/local/bin/victor-input",
         "debian-edition/config/includes.chroot_after_packages/usr/local/bin/victor-input"),
        ("airootfs/usr/local/bin/victor-bt-pair",
         "debian-edition/config/includes.chroot_after_packages/usr/local/bin/victor-bt-pair"),
        ("airootfs/etc/systemd/system/victor-input.service",
         "debian-edition/config/includes.chroot_after_packages/etc/systemd/system/victor-input.service"),
    ]
    for a, b in pairs:
        left, right = ROOT / a, ROOT / b
        assert left.read_text() == right.read_text(), a


def test_unit_is_not_wanted_at_boot_and_docs_exist():
    unit = _read("airootfs/etc/systemd/system/victor-input.service")
    assert "WantedBy=multi-user.target" in unit
    assert "systemctl enable" not in unit
    assert (ROOT / "docs/VICTOR_INPUT_PROTOCOL.md").is_file()
    assert (ROOT / "docs/phone-as-keyboard.md").is_file()
    assert "Likely" in _read("docs/phone-as-keyboard.md")
