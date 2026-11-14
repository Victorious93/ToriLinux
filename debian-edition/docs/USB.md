# USB provisioning (Ventoy + VICTORDATA)

`scripts/provision-usb.py` computes the layout from the drive's **real byte capacity** (kernel sector count, cross-checked with `lsblk -b`) and the **measured ISO size**.
There are no 64 GB / 128 GB tables; a marketed "64 GB" stick is typically ~57–60 GiB and varies by unit.

| Partition | Size rule |
|---|---|
| Ventoy exFAT (holds ISOs) | `3 × ISO + 1 GiB` (current + previous for rollback + incoming update), MiB-aligned |
| VTOYEFI | 32 MiB (fixed by Ventoy) |
| **VICTORDATA** ext4 | **everything else** minus alignment overhead and a spare |
| Unallocated spare | `max(256 MiB, 1 %)` (flash wear levelling / future use) |

The data space is reserved with Ventoy's own mechanism (`Ventoy2Disk.sh -r <MiB>`), then the ext4 partition is created in it. The layout is rejected (exit 3) if data would be < 4 GiB.
Example (2.5 GiB ISO, from `plan`): 57.6 GiB stick → 48.7 GiB data; 114.5 GiB stick → 105.0 GiB data; same ISO partition on both. Base functionality is identical; optional packs are never auto-filled.

## Commands
```sh
./scripts/fetch-ventoy.sh /opt/ventoy                    # pinned version, checksum verified
sudo ./scripts/provision-usb.py list --iso ISO           # identity, bytes, transport, mounts, ELIGIBLE/EXCLUDED
sudo ./scripts/provision-usb.py plan  --device /dev/sdX --iso ISO     # prints layout + the exact confirmation string
sudo ./scripts/provision-usb.py write --device /dev/sdX --iso ISO --confirm 'ERASE <model>:<serial>:<bytes>'
```
Safety: the target is never chosen by size or "removable"; the host system disk, disks backing the working directory/ISO/Ventoy dir and non-disks are excluded; all tools are checked **before**
any write; identity (model:serial:bytes) is re-read right before writing and between steps — a replaced/vanished device aborts; only that disk's mounts are unmounted; the ISO's `.sha256` is
verified before and after the copy. Bootloaders of internal disks are never touched. Ventoy is the upstream binary (GPLv3), unmodified.

Ventoy legacy-BIOS+UEFI boots the ISO file; `secure boot` needs Ventoy's key enrollment (see Ventoy docs). Ventoy's generic persistence plugin is **not used** — see PERSISTENCE.md.

## Updating the ISO without touching data
Copy the new ISO (and its `.sha256`) into the Ventoy partition next to the old one; verify `sha256sum -c`; boot the new one from Ventoy's menu; delete the old one only when satisfied. The
partition holds three generations by construction. VICTORDATA is never repartitioned for an update. If the Ventoy partition is full, remove the oldest ISO.

## Multiple drives
Each drive is prepared separately; the ISO contains no identity. VICTORDATA gets its own filesystem UUID and `victor-data.json` device id; the owner password, API key, TLS certificate and SSH
host keys are generated per drive on first use. Writing multiple attached drives in one command is not supported by design.

## Hardware-dependent limits
exFAT mounting needs a kernel `exfat` driver (Linux ≥ 5.7) or `exfat-fuse` on the preparing host. Some cheap sticks misreport capacity: the tool trusts the kernel number, run `f3` first if in doubt.
