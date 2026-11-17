"""Brand palette (#0B0F1A / #00E5FF / #FF2BD6) across both editions, readable contrast, no external assets."""
import importlib.machinery
import importlib.util
import re
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "airootfs/usr/local/lib/victorlinux/web/index.html"
DEB = ROOT / "debian-edition/config/includes.chroot_after_packages"
CONTROL = DEB / "usr/local/bin/victor-control"
BG, CYAN, MAGENTA = "#0b0f1a", "#00e5ff", "#ff2bd6"


def luminance(h):
    c = [int(h[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    c = [x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4 for x in c]
    return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]


def contrast(a, b):
    hi, lo = sorted((luminance(a), luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def css_vars():
    root = re.search(r":root\{([^}]*)\}", WEB.read_text()).group(1)
    return dict(re.findall(r"--([\w-]+):(#[0-9a-fA-F]{6})", root))


def test_web_palette_is_brand():
    v = {k: x.lower() for k, x in css_vars().items()}
    assert v["bg"] == BG and v["cyan"] == CYAN and v["mag"] == MAGENTA


@pytest.mark.parametrize("fg,bg", [("fg", "bg"), ("mut", "s1"), ("mut", "s2"), ("cyan", "s1"), ("on-acc", "cyan"),
                                   ("mag", "bg"), ("bad", "s1"), ("ok", "s1"), ("warn", "s1")])
def test_web_contrast_meets_wcag_aa(fg, bg):
    v = css_vars()
    assert contrast(v[fg], v[bg]) >= 4.5, (fg, bg)


def test_web_has_no_external_assets_or_light_surfaces():
    html = WEB.read_text()
    assert "<link" not in html and "@import" not in html
    assert not re.search(r"""(src|href)=["']https?://""", html)
    assert not re.search(r"url\(\s*['\"]?https?:", html)
    assert not re.search(r"background(-color)?:\s*(#fff\b|#ffffff\b|white\b)", html, re.I)


def load_control():
    """Import the Tk Control Center with tkinter stubbed (CI has no display)."""
    tk = types.ModuleType("tkinter")
    tk.Tk = type("Tk", (), {})
    for name in ("ttk", "filedialog", "messagebox", "scrolledtext"):
        sub = types.ModuleType("tkinter." + name)
        setattr(tk, name, sub)
        sys.modules["tkinter." + name] = sub
    saved = sys.modules.get("tkinter")
    sys.modules["tkinter"] = tk
    try:
        loader = importlib.machinery.SourceFileLoader("victor_control", str(CONTROL))
        spec = importlib.util.spec_from_loader("victor_control", loader)
        mod = importlib.util.module_from_spec(spec)
        loader.exec_module(mod)
        return mod
    finally:
        if saved is not None:
            sys.modules["tkinter"] = saved
        else:
            sys.modules.pop("tkinter", None)
        for name in ("ttk", "filedialog", "messagebox", "scrolledtext"):
            sys.modules.pop("tkinter." + name, None)


def test_tk_palette_is_brand_and_readable():
    P = load_control().PALETTES["dark"]
    assert (P["bg"], P["accent"], P["accent2"]) == (BG, CYAN, MAGENTA)
    for fg, bg in (("fg", "bg"), ("muted", "card"), ("muted", "panel"), ("on_accent", "accent"), ("accent", "card"),
                   ("ok", "card"), ("warn", "card"), ("bad", "card")):
        assert contrast(P[fg], P[bg]) >= 4.5, (fg, bg)


def test_tk_status_tiles():
    mod = load_control()
    tiles = {}
    fake = types.SimpleNamespace(tile=lambda k, v, s="": tiles.__setitem__(k, (v, s)))
    hw = {"ram": {"total": 16 * 2**30, "available": 10 * 2**30},
          "sysfs_vram": [{"vram_used": 2**30, "vram_total": 8 * 2**30, "card": "card0"}]}
    st = {"state": "persistent", "usage": {"free": 100 * 2**30}}
    ll = {"running": True, "runtime": {"model": "/m/qwen.gguf", "backend": {"backend": "vulkan", "reason": "AMD GPU"}}}
    cfg = {"model": "/m/qwen.gguf", "bind": "127.0.0.1", "port": 8080, "remote": {"enabled": False}}
    mod.App.update_tiles(fake, hw, st, ll, cfg)
    assert tiles["model"] == ("qwen.gguf", "loaded")
    assert tiles["backend"][0] == "Vulkan"
    assert tiles["api"] == ("On", "127.0.0.1:8080 · local only")
    assert tiles["storage"] == ("Persistent", "100.0 GiB free")
    assert tiles["ram"][0] == "6.0 GiB / 16.0 GiB"
    assert tiles["vram"] == ("1.0 GiB / 8.0 GiB", "card0")
    mod.App.update_tiles(fake, {"ram": {}, "sysfs_vram": []}, {"state": "missing", "message": "no drive"},
                         {"running": False}, {"remote": {"enabled": True}, "bind": "10.0.0.2", "port": 9000})
    assert tiles["model"] == ("None selected", "pick one in Models")
    assert tiles["api"] == ("Off", "10.0.0.2:9000 · remote")
    assert tiles["vram"][0] == "No VRAM info"


@pytest.mark.parametrize("path,needle", [
    ("airootfs/home/victor/.config/openbox/autostart", 'xsetroot -solid "#0b0f1a"'),
    ("airootfs/home/victor/.local/share/konsole/VictorLinux.profile", "ColorScheme=VictorLinux"),
    ("airootfs/home/victor/.local/share/konsole/VictorLinux.colorscheme", "Color=11,15,26"),
    ("debian-edition/config/includes.chroot_after_packages/etc/skel/.config/xfce4/terminal/terminalrc", "ColorBackground=#0b0f1a"),
    ("debian-edition/config/includes.chroot_after_packages/etc/lightdm/lightdm-gtk-greeter.conf", "background=#0b0f1a"),
    ("debian-edition/config/includes.chroot_after_packages/usr/local/bin/victor-theme", "'ColorBackground': '#0b0f1a'"),
    ("debian-edition/config/includes.chroot_after_packages/usr/local/bin/victor-firstrun", '"#0b0f1a"'),
])
def test_desktop_theme_files_use_brand_background(path, needle):
    assert needle in (ROOT / path).read_text()
