import importlib.util, os, sys, unittest
spec = importlib.util.spec_from_file_location("prov", os.path.join(os.path.dirname(__file__), "..", "scripts", "provision-usb.py"))
prov = importlib.util.module_from_spec(spec); spec.loader.exec_module(prov)
GiB, MiB = prov.GiB, prov.MiB


class Layout(unittest.TestCase):
    iso = int(2.5 * GiB)

    def test_alignment_and_fit_for_many_real_capacities(self):
        # marketed 64 GB / 128 GB sticks vary; include odd byte counts
        for total in (57_600_000_000, 59_800_000_123, 61_847_529_062, 64_000_000_000, 114_500_000_001,
                      119_200_000_000, 122_943_438_848, 127_990_025_420):
            l = prov.compute_layout(total, self.iso)
            self.assertEqual(l["data_partition_bytes"] % MiB, 0)
            self.assertEqual(l["ventoy_iso_partition_bytes"] % MiB, 0)
            used = (prov.PART_TABLE_OVERHEAD + l["ventoy_efi_bytes"] + l["ventoy_iso_partition_bytes"]
                    + l["data_partition_bytes"] + l["spare_unallocated_bytes"])
            self.assertLessEqual(used, total)
            self.assertGreaterEqual(l["data_partition_bytes"], prov.MIN_DATA)
            self.assertEqual(l["ventoy_reserve_mib"] * MiB, l["data_partition_bytes"] + l["spare_unallocated_bytes"])

    def test_larger_drive_gets_more_data_with_same_code(self):
        small = prov.compute_layout(int(57.6 * GiB), self.iso)
        big = prov.compute_layout(int(114.5 * GiB), self.iso)
        self.assertGreater(big["data_partition_bytes"], 2 * small["data_partition_bytes"] - 4 * GiB)
        self.assertEqual(big["ventoy_iso_partition_bytes"], small["ventoy_iso_partition_bytes"])  # base layout identical

    def test_iso_room_for_update_and_rollback(self):
        l = prov.compute_layout(int(57.6 * GiB), self.iso)
        self.assertGreaterEqual(l["ventoy_iso_partition_bytes"], 3 * self.iso)

    def test_rejects_too_small(self):
        with self.assertRaises(prov.Rejected):
            prov.compute_layout(8 * GiB, self.iso)
        with self.assertRaises(prov.Rejected):
            prov.compute_layout(int(10 * GiB), 6 * GiB)  # ISO too big for the drive

    def test_rejects_nonpositive(self):
        with self.assertRaises(prov.Rejected):
            prov.compute_layout(0, self.iso)

    def test_no_hardcoded_capacity_tables(self):
        src = open(os.path.join(os.path.dirname(__file__), "..", "scripts", "provision-usb.py")).read()
        for needle in ("64 *", "128 *", "== 64", "== 128"):
            self.assertNotIn(needle, src)

    def test_identity_token_includes_serial_and_bytes(self):
        d = {"model": "USB DISK 3.0", "serial": "ABC123", "size": 61847529062}
        self.assertEqual(prov.identity(d), "USB_DISK_3.0:ABC123:61847529062")


if __name__ == "__main__":
    unittest.main()
