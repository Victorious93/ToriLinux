"""Build-variant plumbing (gui|headless): argument validation and overlay contents. No build is run here."""
import os, subprocess, unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class Variants(unittest.TestCase):
    def test_build_rejects_unknown_variant(self):
        p = subprocess.run(["bash", os.path.join(HERE, "build.sh"), "--variant", "tiny"], capture_output=True, text=True)
        self.assertEqual(p.returncode, 2)
        self.assertIn("variant must be gui or headless", p.stderr)

    def test_headless_overlay_files(self):
        o = os.path.join(HERE, "config/variants/headless")
        for f in ("etc/systemd/system/getty@tty1.service.d/autologin.conf", "etc/profile.d/victor-headless.sh"):
            self.assertTrue(os.path.isfile(os.path.join(o, f)), f)
        # autologin is the unprivileged owner only, never root
        conf = open(os.path.join(o, "etc/systemd/system/getty@tty1.service.d/autologin.conf")).read()
        self.assertIn("--autologin victor", conf)
        self.assertNotIn("autologin root", conf)

    def test_profile_script_syntax(self):
        p = subprocess.run(["bash", "-n", os.path.join(HERE, "config/variants/headless/etc/profile.d/victor-headless.sh")])
        self.assertEqual(p.returncode, 0)

    def test_firstrun_text_mode_does_not_need_tk_at_import(self):
        src = open(os.path.join(HERE, "config/includes.chroot_after_packages/usr/local/bin/victor-firstrun")).read()
        head = src.split('if "--text" in sys.argv')[0]
        self.assertNotIn("tkinter", head)

    def test_desktop_list_exists_for_gui_and_build_drops_it_for_headless(self):
        self.assertTrue(os.path.isfile(os.path.join(HERE, "config/package-lists/20-desktop.list.chroot")))
        b = open(os.path.join(HERE, "build.sh")).read()
        self.assertIn("rm -f config/package-lists/20-desktop.list.chroot", b)
        self.assertIn("s/^wireshark$/tshark/", b)


if __name__ == "__main__":
    unittest.main()
