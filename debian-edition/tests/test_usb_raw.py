"""Raw hybrid-ISO layout and MBR inspection (pure functions; no device is touched)."""
import importlib.util, os, unittest

spec = importlib.util.spec_from_file_location("prov", os.path.join(os.path.dirname(__file__), "..", "scripts", "provision-usb.py"))
prov = importlib.util.module_from_spec(spec); spec.loader.exec_module(prov)
GiB, MiB = prov.GiB, prov.MiB


def mbr(entries, sig=b"\x55\xaa"):
    s = bytearray(512)
    for i, (ptype, start, size) in enumerate(entries):
        s[446 + 16 * i + 4] = ptype
        s[446 + 16 * i + 8: 446 + 16 * i + 12] = start.to_bytes(4, "little")
        s[446 + 16 * i + 12: 446 + 16 * i + 16] = size.to_bytes(4, "little")
    s[510:512] = sig
    return bytes(s)


# the shape of a real live-build hybrid ISO: ISO partition (type 0), EFI partition inside it, two free slots
HYBRID = [(0x00, 64, 2029504), (0xEF, 540, 6656), (0, 0, 0), (0, 0, 0)]
ISO_BYTES = 1039138816


class Mbr(unittest.TestCase):
    def test_hybrid_iso_has_free_slot_and_end(self):
        r = prov.inspect_iso_mbr(mbr(HYBRID), ISO_BYTES)
        self.assertEqual(r["free_slot"], 2)
        self.assertEqual(r["iso_end_sector"], 64 + 2029504)
        self.assertEqual(r["iso_end_sector"] * 512, ISO_BYTES)

    def test_rejects_non_hybrid_gpt_full_and_truncated(self):
        with self.assertRaises(prov.Rejected): prov.inspect_iso_mbr(mbr(HYBRID, sig=b"\x00\x00"), ISO_BYTES)
        with self.assertRaises(prov.Rejected): prov.inspect_iso_mbr(mbr([(0xEE, 1, 1000)]), ISO_BYTES)
        with self.assertRaises(prov.Rejected):
            prov.inspect_iso_mbr(mbr([(0x83, 64, 100), (0x83, 200, 100), (0x83, 400, 100), (0x83, 600, 100)]), 10**6)
        with self.assertRaises(prov.Rejected): prov.inspect_iso_mbr(mbr(HYBRID), 10 * MiB)   # entries far past the file
        with self.assertRaises(prov.Rejected): prov.inspect_iso_mbr(b"\x00" * 100, ISO_BYTES)

    def test_uses_first_free_slot_even_if_not_third(self):
        r = prov.inspect_iso_mbr(mbr([(0x00, 64, 2029504), (0, 0, 0), (0xEF, 540, 6656), (0, 0, 0)]), ISO_BYTES)
        self.assertEqual(r["free_slot"], 1)


class RawLayout(unittest.TestCase):
    end = 64 + 2029504

    def test_fits_and_is_aligned_for_odd_real_capacities(self):
        for total in (57_600_000_000, 59_800_000_123, 64_000_000_000, 128_035_676_160, 2_000_000_000_000):
            l = prov.compute_layout_raw(total, self.end)
            self.assertEqual(l["data_start_bytes"] % MiB, 0)
            self.assertEqual(l["data_partition_bytes"] % MiB, 0)
            self.assertGreaterEqual(l["data_start_bytes"], self.end * 512)       # never overlaps the ISO
            self.assertLessEqual(l["data_start_bytes"] + l["data_partition_bytes"], total)
            self.assertGreaterEqual(l["spare_unallocated_bytes"], prov.MIN_SPARE)
            self.assertEqual(l["data_start_sector"] * 512, l["data_start_bytes"])

    def test_rejects_too_small_and_nonpositive(self):
        with self.assertRaises(prov.Rejected): prov.compute_layout_raw(4 * GiB, self.end)
        with self.assertRaises(prov.Rejected): prov.compute_layout_raw(0, self.end)
        with self.assertRaises(prov.Rejected): prov.compute_layout_raw(64 * GiB, 0)

    def test_larger_drive_gets_more_data_with_same_code(self):
        a = prov.compute_layout_raw(32 * GiB, self.end)["data_partition_bytes"]
        b = prov.compute_layout_raw(128 * GiB, self.end)["data_partition_bytes"]
        self.assertGreater(b, a)

    def test_raw_gives_more_data_than_ventoy_for_same_drive(self):
        t = 64_000_000_000
        self.assertGreater(prov.compute_layout_raw(t, self.end)["data_partition_bytes"],
                           prov.compute_layout(t, ISO_BYTES)["data_partition_bytes"])

    def test_data_partition_capped_at_mbr_2tib_limit(self):
        l = prov.compute_layout_raw(4_000_000_000_000, self.end)
        self.assertLessEqual(l["data_start_sector"] + l["data_sectors"], 2**32)
        self.assertGreater(l["data_partition_bytes"], 1_900 * 10**9)


if __name__ == "__main__":
    unittest.main()
