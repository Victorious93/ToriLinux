import json, os, struct, sys, tempfile, unittest
LIB = os.path.join(os.path.dirname(__file__), "..", "config", "includes.chroot_after_packages", "usr", "local", "lib", "victorlinux")
sys.path.insert(0, LIB)
from victorlib import hw, storage, models, llama
from victorlib.common import VictorError


def lsblk_fixture(parts):
    return {"blockdevices": [{"name": "sdb", "type": "disk", "model": "USB DISK", "serial": "S1", "tran": "usb", "rm": True, "size": 1,
                              "children": parts}]}


def part(uuid, label="VICTORDATA", fs="ext4"):
    return {"name": "sdb3", "path": "/dev/sdb3", "type": "part", "fstype": fs, "label": label, "uuid": uuid, "size": 100, "mountpoints": [None]}


class Backend(unittest.TestCase):
    rx = {"id": "Vulkan0", "name": "AMD Radeon RX 6800 XT (RADV NAVI21)", "total_mib": 16368, "free_mib": 16000}
    llvm = {"id": "Vulkan0", "name": "llvmpipe (LLVM 19)", "total_mib": 30000, "free_mib": 30000}

    def test_real_gpu_selected(self):
        b = hw.select_backend("auto", [self.rx], None)
        self.assertTrue(b["accelerated"]); self.assertEqual(b["device"], "Vulkan0")

    def test_software_vulkan_is_not_acceleration(self):
        b = hw.select_backend("auto", [self.llvm], None)
        self.assertFalse(b["accelerated"]); self.assertIn("software", b["reason"]); self.assertTrue(b["visible_warning"])

    def test_no_device_explains_cpu_fallback(self):
        b = hw.select_backend("auto", [], None)
        self.assertEqual(b["backend"], "cpu"); self.assertIn("no GPU", b["reason"])

    def test_user_forced_cpu(self):
        self.assertFalse(hw.select_backend("cpu", [self.rx], None)["accelerated"])

    def test_largest_gpu_wins(self):
        small = dict(self.rx, id="Vulkan1", total_mib=4096, name="Intel iGPU")
        self.assertEqual(hw.select_backend("auto", [small, self.rx], None)["device"], "Vulkan0")


class Storage(unittest.TestCase):
    def cands(self, *uuids):
        return storage.candidates(storage.parse_lsblk(lsblk_fixture([part(u) for u in uuids])))

    def test_only_label_and_ext4(self):
        c = storage.candidates(storage.parse_lsblk(lsblk_fixture([part("a", label="OTHER"), part("b", fs="exfat"), part("c")])))
        self.assertEqual([x["uuid"] for x in c], ["c"])

    def test_toridata_label_accepted_case_sensitive(self):
        c = storage.candidates(storage.parse_lsblk(lsblk_fixture([
            part("t", label="TORIDATA"), part("l", label="toridata"), part("v", label="VICTORDATA"), part("x", label="TORIDATA2")])))
        self.assertEqual(sorted(x["uuid"] for x in c), ["t", "v"])
        self.assertEqual({x["uuid"]: x["label"] for x in c}, {"t": "TORIDATA", "v": "VICTORDATA"})

    def test_toridata_alone_mounts_but_mixed_labels_need_explicit_choice(self):
        one = storage.candidates(storage.parse_lsblk(lsblk_fixture([part("t", label="TORIDATA")])))
        self.assertEqual(storage.decide(one), ("mount", "t", "single VICTORDATA/TORIDATA partition found"))
        both = storage.candidates(storage.parse_lsblk(lsblk_fixture([part("t", label="TORIDATA"), part("v")])))
        self.assertEqual(storage.decide(both)[:2], ("needs-selection", None))
        self.assertEqual(storage.decide(both, "t")[:2], ("mount", "t"))

    def test_single_mounts_none_missing_multiple_requires_choice(self):
        self.assertEqual(storage.decide(self.cands("u1"))[0], "mount")
        self.assertEqual(storage.decide([])[0], "missing")
        a, u, m = storage.decide(self.cands("u1", "u2"))
        self.assertEqual((a, u), ("needs-selection", None))

    def test_explicit_selection_and_vanished_selection(self):
        self.assertEqual(storage.decide(self.cands("u1", "u2"), "u2")[:2], ("mount", "u2"))
        self.assertEqual(storage.decide(self.cands("u1"), "gone")[0], "missing")

    def test_health_classification(self):
        self.assertEqual(storage.classify_mounted("/x", "rw,noatime", 10 * 2**30, True)[0], "mounted-rw")
        self.assertEqual(storage.classify_mounted("/x", "ro,noatime", 10 * 2**30, True)[0], "mounted-ro")
        self.assertEqual(storage.classify_mounted("/x", "rw", 10 * 2**30, False)[0], "mounted-ro")
        self.assertEqual(storage.classify_mounted("/x", "rw", 1024, True)[0], "full")


class Config(unittest.TestCase):
    def cfg(self, **kw):
        import json
        c = json.loads(json.dumps(llama.DEFAULTS)); c.update(kw); return c

    def test_default_valid_and_loopback(self):
        self.assertEqual(llama.validate(self.cfg()), [])

    def test_nonloopback_requires_explicit_remote(self):
        self.assertTrue(llama.validate(self.cfg(bind="192.168.1.5")))

    def test_remote_without_tls_needs_vpn_ack(self):
        c = self.cfg(bind="100.64.0.5"); c["remote"].update(enabled=True, tls=False)
        self.assertTrue(llama.validate(c))
        c["remote"]["vpn_plain_http_ack"] = True
        self.assertEqual(llama.validate(c), [])

    def test_wildcard_bind_is_never_implicit(self):
        self.assertTrue(llama.validate(self.cfg(bind="0.0.0.0")))

    def test_command_never_contains_tool_flags_and_remote_gets_key_and_tls(self):
        c = self.cfg(bind="10.0.0.2"); c["remote"].update(enabled=True)
        argv = llama.build_command(c, {"accelerated": False, "device": None}, "/m.gguf", "/k", ("/c.pem", "/k.pem"))
        for bad in llama.FORBIDDEN_FLAGS: self.assertNotIn(bad, argv)
        self.assertIn("--api-key-file", argv); self.assertIn("--ssl-cert-file", argv)
        self.assertEqual(argv[argv.index("--device") + 1], "none")

    def test_gpu_command(self):
        argv = llama.build_command(self.cfg(), {"accelerated": True, "device": "Vulkan0"}, "/m.gguf")
        self.assertEqual(argv[argv.index("--device") + 1], "Vulkan0"); self.assertNotIn("--api-key-file", argv)


class ApplySet(unittest.TestCase):
    def test_types_and_dotted_keys(self):
        import json
        c = json.loads(json.dumps(llama.DEFAULTS))
        llama.apply_set(c, ["ctx=2048", "jinja=1", "remote.enabled=true", "mem_limit_gb=2.5", "model=a.gguf", "remote.tls=0"])
        self.assertEqual((c["ctx"], c["jinja"], c["remote"]["enabled"], c["mem_limit_gb"], c["model"], c["remote"]["tls"]),
                         (2048, True, True, 2.5, "a.gguf", False))

    def test_unknown_and_malformed(self):
        import json
        c = json.loads(json.dumps(llama.DEFAULTS))
        for bad in (["nope=1"], ["remote.nope=1"], ["ctx"], ["ctx=abc"], ["model.x=1"]):
            with self.assertRaises(VictorError): llama.apply_set(json.loads(json.dumps(c)), bad)


class ModelFiles(unittest.TestCase):
    def write(self, data):
        f = tempfile.NamedTemporaryFile(delete=False, suffix=".gguf"); f.write(data); f.close(); self.addCleanup(os.unlink, f.name); return f.name

    def test_valid_header(self):
        p = self.write(b"GGUF" + struct.pack("<IQQ", 3, 10, 5) + b"\0" * 64)
        self.assertEqual(models.inspect_gguf(p)["gguf_version"], 3)

    def test_bad_magic_truncated_version_zero_tensors(self):
        for data, msg in ((b"nope" * 20, "Not a GGUF"), (b"GGUF" + b"\0" * 4, "truncated"),
                          (b"GGUF" + struct.pack("<IQQ", 9, 1, 1), "version"), (b"GGUF" + struct.pack("<IQQ", 3, 0, 1), "tensor")):
            with self.assertRaises(VictorError) as cm: models.inspect_gguf(self.write(data))
            self.assertIn(msg, str(cm.exception))

    def test_safetensors_hint(self):
        p = self.write(b"\x10\0\0\0\0\0\0\0{\"a\":1}")
        with self.assertRaises(VictorError) as cm: models.inspect_gguf(p)
        self.assertIn("GGUF", str(cm.exception))


class CacheRamTests(unittest.TestCase):
    GiB = 1 << 30

    def test_auto_is_bounded_by_free_ram_not_the_8gib_default(self):
        c = {"cache_ram_mb": "auto"}
        self.assertEqual(llama.resolve_cache_ram_mb(c, 4 * self.GiB, 64 * self.GiB), 8192)          # roomy host: capped at the old default
        self.assertEqual(llama.resolve_cache_ram_mb(c, 4 * self.GiB, 16 * self.GiB), 2560)         # (16-4-2)/4 GiB
        self.assertEqual(llama.resolve_cache_ram_mb(c, 14 * self.GiB, 16 * self.GiB), 0)           # nothing spare: cache off, never negative
        self.assertEqual(llama.resolve_cache_ram_mb(c, 4 * self.GiB, None), 1024)                  # unknown RAM: small, not 8 GiB

    def test_explicit_value_wins_and_zero_disables(self):
        self.assertEqual(llama.resolve_cache_ram_mb({"cache_ram_mb": 512}, 0, 1 << 40), 512)
        self.assertEqual(llama.resolve_cache_ram_mb({"cache_ram_mb": 0}, 0, 1 << 40), 0)

    def test_flag_passed_only_when_resolved_and_validated(self):
        c = json.loads(json.dumps(llama.DEFAULTS))
        self.assertNotIn("--cache-ram", llama.build_command(c, {"accelerated": False, "device": None}, "/m.gguf"))
        a = llama.build_command(c, {"accelerated": False, "device": None}, "/m.gguf", cache_ram_mb=2560)
        self.assertEqual(a[a.index("--cache-ram") + 1], "2560")
        for bad in (-1, 70000, "lots", True):
            c2 = json.loads(json.dumps(llama.DEFAULTS)); c2["cache_ram_mb"] = bad
            self.assertTrue(any("cache_ram_mb" in e for e in llama.validate(c2)), bad)
        self.assertFalse(any("cache_ram_mb" in e for e in llama.validate(c)))


if __name__ == "__main__":
    unittest.main()
