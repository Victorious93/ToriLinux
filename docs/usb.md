# Boot media: USB drives of any size

`scripts/make-usb.sh` prepares a **boot stick**. It is the destructive workflow: it erases the whole target drive. Attaching an existing data
drive, and keeping files on your internal SSD or external drive, is a different and non-destructive workflow: see [storage.md](storage.md).

## Capacity: computed from the real device, not a table

Nothing is hard-coded for 64 GB or 128 GB and there is no upper limit. The plan uses the drive's **measured byte size**, its **logical and
physical sector size**, the **ISO's own partition table** and 1 MiB alignment, in unbounded integer arithmetic. The whole plan is validated
**before** `wipefs`, any partition write, any raw write or any `mkfs`. An unrepresentable layout is refused while the drive is still untouched.

Minimum for the default layout = image + 2 GiB data (adjustable `--min-data-gib`) + 1 GiB unallocated reserve (adjustable `--reserve-mib`).
For an image of about 1.8 GiB that is **≈ 4.8 GiB**, so a nominal 4 GB stick (3.7 GiB) is refused and an 8 GB stick works with ≈ 4.7 GiB of data.
(No production ISO was built for this change; `plan` prints the exact minimum for *your* ISO.)

Planner output for an 1800 MiB hybrid image (default options; measured drive size = marketed decimal size):

| Marketed size | Real size | Partition table | Data partition |
|---|---|---|---|
| 4 GB | 3.7 GiB | refused: too small | – |
| 8 GB | 7.5 GiB | MBR | 4.7 GiB |
| 16 GB | 14.9 GiB | MBR | 12.1 GiB |
| 64 GB | 59.6 GiB | MBR | 56.8 GiB |
| 128 GB | 119.2 GiB | MBR | 116.5 GiB |
| 1 TB | 931.3 GiB | MBR | 928.6 GiB |
| 2 TB | 1,862.6 GiB | MBR | 1,859.9 GiB |
| 4 TB | 3,725.3 GiB | **GPT** | 3,722.5 GiB |
| 8 TB | 7,450.6 GiB | **GPT** | 7,447.8 GiB |
| 16 TB | 14,901.2 GiB | **GPT** | 14,898.4 GiB |
| 20 TB | 18,626.5 GiB | **GPT** | 18,623.7 GiB |

The data partition is formatted ext4 and labelled `VICTORDATA`. ext4 itself can address 1 EiB. Practical limits are those of the drive, the USB bridge, the firmware and the kernel.
Boot-media creation is not the place to put a 16 TB archive; you can pick `--data-gib N` and keep the rest unallocated, or leave the large drive as a plain data drive and boot from a small stick ([storage.md](storage.md)).

## Why GPT, and what is preserved

MBR partition entries have 32-bit start/length fields: with 512-byte sectors nothing can be described beyond 2 TiB. The previous script packed the data partition into those 32-bit fields only *after* it had erased the drive and written the image, so a larger drive would have failed at that late point. Now:

* `--table auto` (default): keeps the proven MBR layout (data partition in the image's free MBR slot 3) **whenever it is fully representable and the image's MBR is not
  a protective MBR**; otherwise it builds a GPT. `--table mbr|gpt` forces one.
* GPT mode **does not convert or rebuild the image's partition table.** The image's own GPT entries, entry-array location (xorriso/GRUB images place it at LBA 20 beside Apple Partition Map blocks), boot code and
  ESP partition are kept byte for byte; the backup GPT is relocated to the real end of the drive; the data partition is appended; a protective MBR entry is added if the MBR has none.
  An MBR-only image whose partitions start inside the area a GPT needs (LBA < 34), or whose LBA 1–33 hold boot-loader data, is **refused** for GPT with an explanation.
  `--allow-mbr-cap` lets you accept, explicitly, a data partition limited to the MBR-addressable space instead; the rest of the drive stays unallocated. It is never done silently.
* After the table change, the **image payload** (everything after the 32 KiB system area) is read back and compared with the ISO. The disk as a whole is intentionally **not** byte-identical to the ISO
  (partition tables changed); the script says so.
* Sector size: **512-byte logical (including 512e drives with 4096-byte physical sectors) is supported**. **4Kn** (4096-byte logical) is refused in preflight for boot media: the hybrid image's
  partition table and ESP are expressed in 512-byte sectors and are not bootable when written raw to a 4Kn device. A 4Kn drive remains fine as a *data* drive.

## Commands

```sh
sudo scripts/make-usb.sh list
sudo scripts/make-usb.sh plan  --iso out/EXACT.iso --device /dev/sdX
# Only after checking the plan and backing up the target:
sudo scripts/make-usb.sh write --iso out/EXACT.iso --device /dev/sdX --confirm-serial ACTUAL_SERIAL
```

Options for the default (`raw`) layout:

| Option | Meaning |
|---|---|
| `--table auto|mbr|gpt` | partition table (default `auto`) |
| `--data-gib N` | data partition size; default all usable space; the remainder stays unallocated |
| `--reserve-mib N` | unallocated space kept at the end (default 1024) |
| `--min-data-gib N` | refuse drives that would leave less data space (default 2) |
| `--allow-mbr-cap` | accept a data partition limited to MBR's range when GPT is impossible for this image |

Safety (unchanged): exact device **and serial** required; identity re-read right before writing; system disk, the workspace disk, mounted devices and non-USB transports refused; ISO `.sha256` verified;
ISO read back from the device and compared; flush before the table is written; payload re-verified afterwards. `--layout ventoy` still works as before (Ventoy writes a GPT itself); its UEFI
boot of this ISO remains **unresolved** and is not treated as verified.

A phone-hosted read-only ISO is unaffected: persistence for it comes from a **separate writable drive**, which can now be any ordinary drive chosen with `victor-storage state DRIVE` (needs ext4/btrfs/xfs) plus destinations on anything else.

## What was actually tested

See [the storage test report](2026-11-15-storage-testing.md): planner unit tests (small drives, 64/128 GB, 1/2/4/8/16 TB, non-round sizes, boundaries around 2³²), real `make-usb.sh write` on file-backed loop devices
(MBR, and a sparse **3 TiB** GPT whose data partition ends beyond LBA 2³², validated by `sgdisk`), 4Kn and too-small refusals leaving the target untouched, and QEMU UEFI+BIOS boots of a **generic GRUB test image** written
with the same code. The VictorLinux ISO itself was **not** built or booted, no physical drive was written, and firmware behaviour on real hardware is untested.

## What persists across reboots

| Persists | Does **not** persist |
|---|---|
| On the state drive (`VICTORDATA` or any chosen ext4/btrfs/xfs volume): settings, chats, models imported there, outputs, server config, TLS cert, API-key hash, logs | The operating system, installed packages, `/etc` changes |
| Files on destination drives you chose (models, documents, downloads, outputs, backups) | Anything outside those locations (home directory, `/tmp`, shell history) |

The state partition is found by label `VICTORDATA`/`TORIDATA`, or as the single Linux volume that already has a `victorlinux/` or `tori/` folder, mounted by UUID, and exposed at
`/mnt/victordata/victorlinux` (symlink `~/victor-data`). If missing, read-only or ambiguous, the Control Center says so and offers an explicit **temporary session** (RAM only; lost on reboot).
Ventoy's generic persistence plugin is **not** used.

## Booting

UEFI recommended. If Ventoy shows the ISO as unsupported or the boot hangs, try Ventoy's "GRUB2 mode". Whether this ISO boots under Ventoy on **your** hardware is not verified.
