# Victorious Flash: companion changes still needed

The companion repository [Victorious93/victorious-flash](https://github.com/Victorious93/victorious-flash) was **not available in the workspace** for this change
(this session had access to VictorLinux only), so **nothing in Flash was modified and none of the items below is done.** They are the precise changes needed so that
Flash's desktop and Android "Also add a persistent VICTORDATA partition" paths match [`scripts/victor_layout.py`](../scripts/victor_layout.py). The README of this repository previously described Flash's
current behaviour (raw write, then a 16-byte MBR slot-3 entry); that description is unchanged and was not re-verified against the Flash source.

| # | Change | Why |
|---|---|---|
| 1 | Replace the "add only the 16-byte MBR slot-3 entry" step with the same planner: compute start/length with arbitrary-precision (or checked 64-bit) arithmetic, 1 MiB alignment, the device's real **logical and physical** sector size, 1 GiB default reserve | MBR start/length are 32-bit: nothing above 2 TiB (512-byte sectors) can be described |
| 2 | Add a GPT mode that **keeps the image's own GPT** (including a non-standard entry-array LBA such as 20 and the image's entry count), writes the data entry, relocates the backup GPT to the last LBA, writes primary + backup CRCs, and adds a protective MBR entry when the image's MBR has none | Large drives; must not blindly rebuild a hybrid ISO's table |
| 3 | Reject before the first erase: 4Kn targets, plans below the minimum, MBR-only images that cannot be re-described as GPT (offer an explicit "limit to 2 TiB" choice, never a silent truncation) | Matches `victor_layout.py plan` exit code 3 |
| 4 | After the table is written, verify the **ISO payload after the 32 KiB system area** against the trusted SHA-256 (not the whole disk) and describe the disk as "partition table intentionally changed" | The disk is no longer byte-identical to the ISO |
| 5 | Locate the new partition by start sector (sysfs / `IOCTL`), not by number: with a GPT image the data partition is not necessarily #3 | Image GPT entries vary |
| 6 | UI: show the plan (table type, data size, unallocated space, minimum) before the type-the-name confirmation; allow an explicit data size | Owner control |
| 7 | Android (OTG): block devices are not 4Kn-aware through some USB stacks – read `BLKSSZGET`/`BLKPBSZGET` through the same code path used on desktop or refuse | Same refusal rule |
| 8 | Phone-hosted read-only ISO boot: unchanged; document that persistence comes from a **separate** writable data drive, which no longer needs the `VICTORDATA` label if the owner chooses it in VictorLinux | See [storage.md](storage.md) |

The reference implementation and its tests (`tests/test_layout_planner.py`, `tests/test_make_usb_loop.py`) can be ported or its logic reused; the algorithm is ~300 lines.
Until these are done, the README's statement that Flash prepares 64 GB / 128 GB media remains true, and Flash must **not** be advertised for drives above 2 TiB.
