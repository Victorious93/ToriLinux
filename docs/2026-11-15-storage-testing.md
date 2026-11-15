# Storage upgrade: what was tested (2026-11-15)

Base: `Victorious93/VictorLinux` `main` at `23db4f4`, branch `claude/new-session-lhpgb4`. Environment: a cloud container (Ubuntu 24.04, kernel 6.18, root, loop devices,
QEMU 8.2 + OVMF, `sgdisk`, ext4 only). **This is a source-level and emulator verification. No ISO of this revision was built, no physical drive was written, no real hardware was booted.**

## RAN – passed

| Area | How | Result |
|---|---|---|
| Layout planner | `tests/test_layout_planner.py`: small drives, 8/16/59/119 GiB, 1/2/4/8/16 TB marketed sizes, non-round sizes, minimum enforcement, exact 2³² boundary, 512e, 4Kn, explicit data size, non-hybrid image | pass |
| GPT structures | Sparse **4 TiB** file-backed disk: primary/backup headers, CRCs, entry array at the image's own LBA, usable range, partition ends beyond 2³²; re-validated by an independent parser **and by `sgdisk -v`** on a real GRUB/xorriso image | pass |
| `make-usb.sh write` (real script, loop devices) | MBR image on 6 GiB; GRUB GPT hybrid on sparse **3 TiB** (data partition ≈ 3 TiB, ends beyond LBA 2³²); refusals leave the target byte-identical (4Kn, too small, wrong serial, missing checksum, MBR-only image on >2 TiB) | pass |
| Boot after the table change | QEMU **UEFI (OVMF) and legacy BIOS** boots of the 6 GiB and 3 TiB GPT results of a *generic GRUB test image* printed its marker | pass – proves the preserved boot structures, **not** the VictorLinux ISO, **not** hardware |
| Storage logic | `tests/test_storage.py` (41): subpath validation/escapes, identity vs reordered device names, duplicate UUID/label, missing/unmounted/read-only/locked destinations without fallback, FAT32 4 GiB limit, split GGUF in place, discovery of internal/USB/SD kinds, removed-state-drive, links/bookmarks with spaces & Unicode, privileged-helper options (`nosuid,nodev,noexec`, no force) and absence of format/partition commands, API↔CLI parity, no storage route on the public gateway | pass |
| Real block devices | `tests/test_storage_loop.py` (8): USB-state + unlabelled "SSD" + "external HDD" used **simultaneously**; files saved; unmount/detach/re-attach in a different order; destinations, settings and models found again; pre-existing files byte-identical; UUID/label unchanged; a >4 GiB sparse file on ext4; unplug → no path & no stale mount directory; clones with one UUID refused (this found a real bug: `victor-priv` took the first match); read-only device untouched; full volume refused before copy; unlabelled drive with an existing `victorlinux/` found; existing folder owned by another user not re-owned; CLI == API data | pass |
| Interface | `node tests/browser_ui.js` in Chromium: Storage tab with six drives, three destinations (one missing), FAT32/locked/duplicate flags, 3 viewport widths, dark theme, no JS errors; screenshot in `docs/media/storage-tab.png` (fixture data) | pass |
| Regression | Pre-existing suites unchanged | pass |
| Static | `bash -n`, ShellCheck (`-S warning`) on `make-usb.sh`, pyflakes on new Python | clean (one pre-existing unused variable elsewhere) |

Totals at the final commit: `python3 -m pytest -q tests debian-edition/tests/test_logic.py debian-edition/tests/test_layout.py` → **165 passed** (≈2 min, running as root with loop devices, `sgdisk`, QEMU and `grub-mkrescue` present; where those are absent the loop/boot tests skip); `node tests/browser_ui.js` → 7 PASS lines, no failures. Baseline before this work: 108 Python tests.

Bugs found by these tests and fixed: 32-bit MBR overflow discovered only after erase (old design); `blkid` cache returning a previous drive's UUID when device names are reused; `blkid` export escaping (`Big\ Drive`);
first-match selection for duplicate UUIDs in the privileged helper; stale volume cache after remount; planner assumptions about GPT entry-array location (real GRUB images use LBA 20 with Apple Partition Map blocks around it).

## NOT RUN / BLOCKED

| Item | Reason | To finish |
|---|---|---|
| exFAT, NTFS (`ntfs3`), FAT32, Btrfs, XFS **mounting, read/write, large-file refusal on a real FAT32 mount** | the sandbox kernel has none of those drivers (only ext4 mounted); FUSE fallbacks would not test the packaged kernel path | run `tests/test_storage_loop.py`-style scenarios on a booted VictorLinux or any Linux with those modules; the mount option strings and FAT32 refusal logic are unit-tested only |
| Build of an ISO containing these changes (`dosfstools` package line, `victor-storage`, new helper) | no Arch build environment | `scripts/build.sh amd gui`, then `scripts/check-packages.sh` |
| `make-usb.sh` on the **real** VictorLinux ISO (does its system area carry a GPT? is slot 3 free?) | no ISO | `sudo scripts/make-usb.sh plan --iso … --device …` prints the table choice and any refusal |
| Physical USB/NVMe/SATA/SD, 4Kn hardware, real 2 TB external drive, UEFI/BIOS firmware, Secure Boot | no hardware | repeat on hardware |
| Ventoy layout and its UEFI issue | unchanged and unverified | – |
| Desktop file chooser/Save As in a booted session; Xfce/Thunar on Debian | no booted desktop | open Firefox's Save As and check the `VictorLinux …` bookmarks |
| Encrypted-volume unlock | no such feature exists in VictorLinux; volumes are shown as locked | – |
| Debian edition destinations | not implemented (see storage.md Limitations) | port `victorstorage.py` into the Debian helper |
| Victorious Flash changes | repository not available | [flash-companion-changes.md](flash-companion-changes.md) |
| GPU inference, phone-hosted boot, fleet | out of scope of this change | – |
