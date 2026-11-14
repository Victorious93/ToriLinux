# USB drive layout (Ventoy + persistent data)

`scripts/make-usb.sh` implements this layout with Ventoy's own installer plus a dedicated ext4 partition:

```
1  exFAT  "Ventoy"    ISO storage (>= 8 GiB or ~2x the ISO). Put extra ISOs here too.
2  FAT    "VTOYEFI"   32 MiB, Ventoy
3  ext4   "TORIDATA"  everything else except ~1 GiB left unallocated  -> models, chats, settings, outputs ...
```

Sizing is computed from the drive's **measured byte size** (a "64 GB" stick is ~59 GiB; "128 GB" ~119 GiB) and the
measured ISO size - not from the label. Drives that would leave < 8 GiB for data are refused.

## Safety model of `make-usb.sh`

* `list` shows USB/removable disks (model, serial, size, transport, mounts); the system disk and the disk holding
  the repo/workspace are excluded or flagged.
* `write` needs the **exact device path and its serial** (`--confirm-serial`). It rechecks identity right before
  writing, requires the ISO's `.sha256` to match, refuses mounted devices and non-USB transports, installs a
  checksum-verified Ventoy release, creates `TORIDATA`, copies the ISO, **re-verifies the copied ISO's SHA-256**,
  flushes and unmounts.
* It erases the entire target device.

## What persists across reboots

| Persists (on `TORIDATA`) | Does **not** persist |
|---|---|
| Models (`models/`), chat history & app settings (`config/`, `chats/`) | The operating system, installed packages, `/etc` changes |
| Image-workflow outputs (`images/`, `outputs/`), documents, server config, TLS cert, API-key hash, logs | Anything outside the Tori data tree (home dir, `/tmp`, shell history) |

The partition is found by **label `TORIDATA`** at boot (`tori-boot-setup.service`), mounted by UUID, and exposed at
`/mnt/toridata/tori` (symlink `~/tori-data`). If it is missing, read-only, or ambiguous the Control Center says so and
offers an explicit **temporary session** (RAM only; data lost on reboot). Ventoy's generic persistence plugin is **not**
used: it has not been verified with this ISO, and application-level persistence is simpler and safer.

## Booting

UEFI recommended. If Ventoy shows the ISO as unsupported or the boot hangs, try Ventoy's "GRUB2 mode"
(Shift/`G` when selecting). Whether this ISO boots under Ventoy on **your** hardware is listed in the test report.
