"""Pi edition tests: pure helpers, package lists/profiles, pins, hygiene, hook stubs, image assembly (if tools exist)."""
import importlib, os, re, shutil, subprocess, sys, tempfile, unittest
from pathlib import Path

PI = Path(__file__).resolve().parents[1]
REPO = PI.parent
LIB = PI / "overlay/usr/local/lib/victorlinux/pi"
sys.path.insert(0, str(LIB))
import victorpi  # noqa: E402


def pkgs(name):
    return [l.split("#")[0].strip() for l in (PI / "profiles" / name).read_text().splitlines() if l.split("#")[0].strip()]


class Board(unittest.TestCase):
    def test_classify(self):
        cases = [("Raspberry Pi 5 Model B Rev 1.0\x00", 8_000_000, "pi5", True),
                 ("Raspberry Pi 4 Model B Rev 1.5", 3_900_000, "pi4", True),
                 ("Raspberry Pi 4 Model B Rev 1.2", 900_000, "pi4", False),       # 1 GB Pi 4 -> node
                 ("Raspberry Pi 400 Rev 1.0", 3_900_000, "pi4", True),
                 ("Raspberry Pi Zero 2 W Rev 1.0", 427_000, "zero2w", False),
                 ("Raspberry Pi Zero 2 W Rev 1.0", 8_000_000, "zero2w", False),  # never an AI box, whatever RAM says
                 ("Raspberry Pi 3 Model B Rev 1.2", 900_000, "other-pi", False),
                 ("", 16_000_000, "unknown", False)]
        for model, mem, board, llm in cases:
            r = victorpi.classify(model, mem)
            self.assertEqual((r["board"], r["llm_allowed"]), (board, llm), model)
            self.assertEqual(r["role"], "ai-box" if llm else "node")
            self.assertEqual(r["recommended_model"] is None, not llm)

    def test_recommend_is_small(self):
        self.assertIn("3B", victorpi.recommend_model("pi5", 8_000_000)["name"])
        self.assertIn("1.5B", victorpi.recommend_model("pi4", 4_000_000)["name"])
        self.assertIn("0.5B", victorpi.recommend_model("pi4", 1_900_000)["name"])


class WifiAndFirstboot(unittest.TestCase):
    def test_cmdline_regdom(self):
        base = "console=tty1 root=PARTUUID=abcd1234-02 rootwait quiet\n"
        once = victorpi.set_cmdline_regdom(base, "GB")
        twice = victorpi.set_cmdline_regdom(once, "US")
        self.assertTrue(once.endswith("cfg80211.ieee80211_regdom=GB\n"))
        self.assertEqual(twice.count("ieee80211_regdom"), 1)
        self.assertIn("regdom=US", twice); self.assertEqual(twice.count("\n"), 1)
        for bad in ("usa", "U", "00", "", "u1"):
            with self.assertRaises(ValueError):
                victorpi.set_cmdline_regdom(base, bad)

    def test_firstboot_ok(self):
        cfg, errs = victorpi.parse_firstboot(
            "# c\nFULL_NAME='Don V'\nPASSWORD_HASH=$6$salt$abc\nWIFI_COUNTRY=US\nWIFI_SSID=Home\nWIFI_PSK=12345678\n"
            "SSH_AUTHORIZED_KEY=ssh-ed25519 AAAA a\nSSH_AUTHORIZED_KEY=ssh-ed25519 BBBB b\nSSH_ENABLE=yes\n")
        self.assertEqual(errs, [])
        self.assertEqual(cfg["FULL_NAME"], "Don V"); self.assertEqual(len(cfg["SSH_AUTHORIZED_KEY"]), 2)

    def test_firstboot_rejects(self):
        bad = {"PASSWORD=hunter22\n": "plain PASSWORD", "PASSWORD_HASH=abc\n": "crypt hash",
               "WIFI_SSID=x\nWIFI_PSK=12345678\n": "WIFI_COUNTRY is required", "WIFI_COUNTRY=usa\n": "2-letter",
               "SSH_ENABLE=yes\n": "needs at least one", "TOKEN=1\n": "unknown key", "garbage\n": "KEY=VALUE",
               "WIFI_COUNTRY=US\nWIFI_SSID=a\nWIFI_PSK=short\n": "8-63"}
        for text, why in bad.items():
            _, errs = victorpi.parse_firstboot(text)
            self.assertTrue(any(why in e for e in errs), (text, errs))

    def test_nm_keyfile(self):
        k = victorpi.nm_wifi_keyfile("Home", "correct horse")
        self.assertIn("ssid=Home", k); self.assertIn("key-mgmt=wpa-psk", k)
        with self.assertRaises(ValueError):
            victorpi.nm_wifi_keyfile("Home\nx", "12345678")


class Profiles(unittest.TestCase):
    def test_lists_clean(self):
        common, desk, head = pkgs("common.list"), pkgs("pi-desktop.list"), pkgs("pi-headless.list")
        for name, lst in (("common", common), ("desktop", desk), ("headless", head)):
            self.assertEqual(len(lst), len(set(lst)), f"duplicates in {name}")
            for p in lst:
                self.assertRegex(p, r"^[a-z0-9][a-z0-9.+-]+$")
        self.assertFalse(set(common) & set(desk)); self.assertFalse(set(common) & set(head))

    def test_required_hardware_packages(self):
        common = set(pkgs("common.list"))
        for p in ("raspi-firmware", "linux-image-rpi-v8", "linux-image-rpi-2712", "firmware-brcm80211", "bluez",
                  "bluez-firmware", "pi-bluetooth", "wireless-regdb", "network-manager", "openssh-server", "nftables",
                  "adb", "python3", "libgomp1", "whiptail"):
            self.assertIn(p, common)
        self.assertNotIn("linux-image-amd64", common)

    def test_desktop_vs_headless(self):
        desk, head = set(pkgs("pi-desktop.list")), set(pkgs("pi-headless.list"))
        self.assertTrue({"xfce4", "lightdm", "python3-tk", "blueman"} <= desk)
        self.assertFalse({"xorg", "xfce4", "lightdm", "firefox-esr"} & head)
        for prof in ("pi-desktop", "pi-headless"):
            env = (PI / "profiles" / f"{prof}.env").read_text()
            self.assertIn("PI_LLAMACPP=1", env)
            self.assertIn(f"PI_DESKTOP={'1' if prof == 'pi-desktop' else '0'}", env)

    def test_rpi_pin_covers_pi_packages(self):
        pref = (PI / "apt/victor-rpi.pref").read_text()
        self.assertIn("Pin-Priority: 100\n", pref)       # Pi archive cannot replace ordinary Debian packages
        pinned = pref.split("Pin-Priority: 100\n", 1)[1].split("\n")[1].split(":", 1)[1].split()
        for p in ("raspi-firmware", "linux-image-rpi-v8", "linux-image-rpi-2712", "firmware-brcm80211", "bluez-firmware", "pi-bluetooth"):
            self.assertTrue(any(re.fullmatch(g.replace("*", ".*"), p) for g in pinned), p)


class PinsAndBuild(unittest.TestCase):
    def env(self, path):
        return dict(l.split("#")[0].strip().split("=", 1) for l in path.read_text().splitlines() if "=" in l.split("#")[0])

    def test_versions(self):
        v = self.env(PI / "versions.env")
        self.assertEqual(v["RPI_ARCHIVE"], "https://archive.raspberrypi.com/debian")
        self.assertEqual(v["RPI_SUITE"], self.env(REPO / "debian-edition/versions.env")["DEBIAN_SUITE"])
        self.assertRegex(v["RPI_KEY_FPR"], r"^[0-9A-F]{40}$")
        self.assertNotIn("LLAMACPP_COMMIT", v, "llama.cpp pin must come from the Debian edition only")

    @unittest.skipUnless(shutil.which("gpg"), "gpg not installed")
    def test_key_fingerprint(self):
        out = subprocess.run(["gpg", "--show-keys", "--with-colons", str(PI / "keys/raspberrypi-archive.asc")],
                             capture_output=True, text=True).stdout
        fpr = next(l.split(":")[9] for l in out.splitlines() if l.startswith("fpr:"))
        self.assertEqual(fpr, self.env(PI / "versions.env")["RPI_KEY_FPR"])

    def test_build_reuses_shared_parts(self):
        b = (PI / "build.sh").read_text()
        for ref in ("config/hooks/live/0250-optional-scrcpy.hook.chroot", "config/hooks/live/9999-cleanup.hook.chroot",
                    "config/includes.chroot_after_packages", "fleet/install.sh"):
            self.assertIn(ref, b)
        for shared in ("debian-edition/config/hooks/live/0250-optional-scrcpy.hook.chroot",
                       "debian-edition/config/hooks/live/9999-cleanup.hook.chroot", "fleet/install.sh",
                       "airootfs/usr/local/lib/victorlinux/model_advisor.py"):
            self.assertTrue((REPO / shared).exists(), shared)
        for h in re.findall(r'\$HERE/hooks/([\w.-]+)', b):
            self.assertTrue((PI / "hooks" / h).exists(), h)

    def test_units_reference_existing_files(self):
        root = PI / "overlay"
        for unit in (root / "etc/systemd/system").glob("*.service"):
            for exe in re.findall(r"^Exec\w*=(/\S+)", unit.read_text(), re.M):
                if exe.startswith(("/usr/local", "/bin/systemctl")) and not exe.startswith("/bin/"):
                    self.assertTrue((root / exe.lstrip("/")).exists() or (REPO / "debian-edition/config/includes.chroot_after_packages" / exe.lstrip("/")).exists(),
                                    f"{unit.name}: {exe}")
        hook = (PI / "hooks/10-pi-config.sh").read_text()
        for unit in (root / "etc/systemd/system").glob("*.service"):
            self.assertIn(unit.name, hook, f"{unit.name} is never enabled")

    def test_boot_files(self):
        cmd = (PI / "overlay/boot/firmware/cmdline.txt").read_text()
        self.assertEqual(cmd.count("\n"), 1); self.assertIn("root=PARTUUID=@ROOT_PARTUUID@", cmd)
        cfg = (PI / "overlay/boot/firmware/config.txt").read_text()
        self.assertIn("arm_64bit=1", cfg)
        self.assertNotRegex(cfg, r"(?m)^dtoverlay=disable-(bt|wifi)")


class Hygiene(unittest.TestCase):
    FILES = [p for p in PI.rglob("*") if p.is_file() and p.suffix != ".asc" and "__pycache__" not in p.parts and p.parent.name != "tests"]

    def test_no_secrets(self):
        pat = re.compile(r"(BEGIN [A-Z ]*PRIVATE KEY|ghp_[A-Za-z0-9]{20}|github_pat_|xox[bp]-|AKIA[0-9A-Z]{16})")
        for p in self.FILES:
            self.assertIsNone(pat.search(p.read_text(errors="ignore")), p)

    def test_no_runtime_downloads_or_telemetry(self):
        for p in (PI / "overlay").rglob("*"):
            if p.is_file():
                t = p.read_text(errors="ignore")
                self.assertNotRegex(t, r"\b(curl|wget)\s", p)
                self.assertNotRegex(t, r"https?://(?!github\.com/Victorious93)", p)

    def test_brand_and_english(self):
        t = (PI / "overlay/usr/local/bin/victor-pi-setup").read_text()
        for c in ("#0B0F1A", "#00E5FF", "#FF2BD6"):
            self.assertIn(c, t)
        self.assertIn('LANG=en_US.UTF-8', (PI / "hooks/10-pi-config.sh").read_text())

    def test_sudoers(self):
        s = (PI / "overlay/etc/sudoers.d/victor-pi-priv").read_text()
        rules = [l for l in s.splitlines() if l.strip() and not l.startswith("#")]
        self.assertEqual(rules, ["victor ALL=(root) NOPASSWD: /usr/local/lib/victorlinux/pi/victor-pi-priv"])

    def test_off_by_default(self):
        hook = (PI / "hooks/10-pi-config.sh").read_text()
        self.assertIn("systemctl disable ssh.service", hook)
        self.assertIn("AutoEnable=false", hook)
        for u in ("sshswitch.service", "rpi-resize.service", "rpi-eeprom-update.service"):
            self.assertIn(u, hook)
        self.assertNotIn("tailscale", hook)


class Hooks(unittest.TestCase):
    RUN = PI / "overlay/usr/local/lib/victorlinux/pi/hooks/run-hook"

    def test_registry_matches_dispatcher(self):
        t = self.RUN.read_text()
        for n in victorpi.HOOKS:
            self.assertIn(f"  {n})", t)

    def test_todo_hooks_exit_3_and_change_nothing(self):
        for n in ("phone-keyboard-bt", "hotspot", "mesh"):
            r = subprocess.run(["bash", str(self.RUN), n], capture_output=True, text=True)
            self.assertEqual(r.returncode, 3, n); self.assertIn(f"TODO({n})", r.stderr)
        self.assertEqual(subprocess.run(["bash", str(self.RUN), "nope"], capture_output=True).returncode, 2)


class LlamaGuard(unittest.TestCase):
    def test_llm_disabled_flag_blocks_start(self):
        with tempfile.TemporaryDirectory() as d:
            os.environ["VICTOR_RUN"] = d
            sys.path.insert(0, str(REPO / "debian-edition/config/includes.chroot_after_packages/usr/local/lib/victorlinux"))
            try:
                for m in [m for m in sys.modules if m.startswith("victorlib")]:
                    del sys.modules[m]
                llama = importlib.import_module("victorlib.llama")
                common = importlib.import_module("victorlib.common")
                Path(d, "llm-disabled").write_text("Raspberry Pi Zero 2 W: node only\n")
                with self.assertRaises(common.VictorError) as e:
                    llama.start()
                self.assertIn("node only", str(e.exception))
                os.unlink(Path(d, "llm-disabled"))
                self.assertIsNone(llama.llm_disabled_reason())
            finally:
                os.environ.pop("VICTOR_RUN", None)
                for m in [m for m in sys.modules if m.startswith("victorlib")]:
                    del sys.modules[m]


class Shell(unittest.TestCase):
    @unittest.skipUnless(shutil.which("shellcheck"), "shellcheck not installed")
    def test_shellcheck(self):
        files = [PI / "build.sh", *sorted((PI / "scripts").glob("*.sh")), *sorted((PI / "hooks").glob("*.sh")),
                 PI / "overlay/usr/local/bin/victor-pi-setup-tui", LIB / "victor-pi-grow-data", LIB / "hooks/run-hook"]
        r = subprocess.run(["shellcheck", "-S", "warning", *map(str, files)], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout)

    def test_python_compiles(self):
        for f in ("overlay/usr/local/bin/victor-pi", "overlay/usr/local/bin/victor-pi-setup",
                  "overlay/usr/local/lib/victorlinux/pi/victor-pi-priv", "overlay/usr/local/lib/victorlinux/pi/victorpi.py"):
            compile((PI / f).read_text(), f, "exec")


@unittest.skipUnless(all(shutil.which(t) for t in ("mkfs.vfat", "mcopy", "mke2fs", "sfdisk")), "image tools not installed")
class ImageAssembly(unittest.TestCase):
    def test_mkimage_layout(self):
        with tempfile.TemporaryDirectory() as d:
            r = Path(d, "rootfs")
            (r / "boot/firmware").mkdir(parents=True); (r / "etc").mkdir()
            (r / "boot/firmware/kernel8.img").write_bytes(b"k" * 4096)
            (r / "boot/firmware/kernel_2712.img").write_bytes(b"k" * 4096)
            shutil.copy(PI / "overlay/boot/firmware/cmdline.txt", r / "boot/firmware/cmdline.txt")
            shutil.copy(PI / "overlay/boot/firmware/config.txt", r / "boot/firmware/config.txt")
            img = Path(d, "t.img")
            p = subprocess.run(["bash", str(PI / "scripts/mkimage.sh"), str(r), str(img), "64", "32"], capture_output=True, text=True)
            self.assertEqual(p.returncode, 0, p.stderr + p.stdout)
            dump = subprocess.run(["sfdisk", "-d", str(img)], capture_output=True, text=True).stdout
            self.assertEqual(len(re.findall(r"^\S+img\d : ", dump, re.M)), 3, dump)
            self.assertIn("type=c", dump)
            diskid = re.search(r"label-id: 0x([0-9a-f]{8})", dump).group(1)
            self.assertIn(f"PARTUUID={diskid}-02  /", (r / "etc/fstab").read_text())
            # the boot partition carries the patched cmdline.txt
            out = subprocess.run(["mtype", "-i", f"{img}@@{8 * 1024 * 1024}", "::cmdline.txt"], capture_output=True, text=True,
                                 env={**os.environ, "MTOOLS_SKIP_CHECK": "1"}).stdout
            self.assertIn(f"root=PARTUUID={diskid}-02", out)


if __name__ == "__main__":
    unittest.main()
