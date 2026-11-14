"""The theme command must disregard persisted light preferences, without root."""
import os
from pathlib import Path
import subprocess

import pytest

THEME = Path(__file__).resolve().parents[1] / "debian-edition/config/includes.chroot_after_packages/usr/local/bin/victor-theme"


@pytest.mark.parametrize("action", ["status", "apply", "dark"])
def test_old_light_preference_cannot_restore_light_theme(tmp_path, action):
    config = tmp_path / ".config/victorlinux"
    config.mkdir(parents=True)
    (config / "theme").write_text("light\n")
    env = dict(os.environ, HOME=str(tmp_path), XDG_CONFIG_HOME=str(tmp_path / ".config"), VICTOR_RUN=str(tmp_path / "run"))
    env.pop("DISPLAY", None)
    result = subprocess.run(["bash", str(THEME), action], env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "dark"
    if action != "status":
        assert "gtk-application-prefer-dark-theme=true" in (tmp_path / ".config/gtk-3.0/settings.ini").read_text()


def test_light_request_is_rejected(tmp_path):
    result = subprocess.run(["bash", str(THEME), "light"], env=dict(os.environ, HOME=str(tmp_path)), capture_output=True, text=True)
    assert result.returncode != 0
