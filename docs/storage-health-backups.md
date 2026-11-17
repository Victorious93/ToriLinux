# Storage health and encrypted backups

## Storage health

*Storage → Storage health* in the Control Center, or `victor-ctl storage health` on Debian.

- **Free space** is checked wherever VictorLinux saves: the data drive and any extra model folders. Below 5 % or 5 GiB free shows a warning. Below 1 % or 1 GiB free is flagged as a problem, because saving models, chats or settings can fail.
- **SMART** is read-only (`smartctl -j -H -A -i`, through the restricted root helper `victor-priv smart /dev/DISK`). The helper accepts only whole local disks (`sdX`, `nvmeXnY`, `vdX`, `mmcblkX`); partitions map to their disk. It reports:
  - a failed overall status
  - reallocated, pending and uncorrectable sectors
  - CRC errors (usually the cable)
  - NVMe wear, spare blocks and media errors
  - temperature
- Every finding comes with a plain fix. If a USB adapter hides SMART data, the drive shows **Unknown**, never "healthy".
- Nothing is ever written to a disk. `smartmontools` is now part of both images.

## Encrypted backups

*Storage → Encrypted backups*, or `victor-ctl backup list|create|inspect|restore` on Debian.

- **Included:** settings, chats, document-search settings and PC reports. On Arch these are `config/`, `chats/`, `docs/config.json` and `reports/`. On Debian they are `settings/` and `chat/`. Models are never included; re-import them.
- **Secrets** (API keys and TLS keys: Arch `config/secrets.json` and `server/`, Debian `secrets/`) are left out unless you tick *also include API keys and TLS keys* (`--include-secrets`).
- **Encryption:** OpenPGP symmetric through `gpg`, AES-256, SHA-512 S2K with maximum iterations, integrity-protected. The passphrase must be at least 10 characters. It reaches gpg through a private pipe: never on a command line, in the environment, in a log or on disk. On Debian the CLI reads it from stdin or a hidden prompt. gpg runs with a throwaway home directory and no passphrase cache.
- **Files:** `victorlinux-backup-YYYYMMDD-HHMMSS.vlbak` (0600) in the `backups` destination. You can point that destination at an external drive in Storage.
- **There is no recovery without the passphrase.**

### Guided restore

1. **Check backup.** The backup is decrypted in memory, its manifest is read and every file's SHA-256 is verified. Unknown paths, `..`, absolute paths and non-file entries are refused.
2. **Restore.** This needs the confirmation box plus a dialog (CLI: `--yes`). Files that would be replaced are first copied to `backups/pre-restore-<time>/`. Restart the model afterwards to use the restored settings.
