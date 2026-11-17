# VictorLinux: Raspberry Pi edition

An arm64 SD-card image built from the Debian edition (Debian 13 "trixie") with Raspberry Pi firmware and kernels from
`archive.raspberrypi.com`. It reuses the Debian edition's `victor-*` tools, Tk Control Center, gateway and fleet
coordinator. Full guide, support levels and what has and has not been tested: **[docs/raspberry-pi.md](../docs/raspberry-pi.md)**.

```bash
sudo ./pi-edition/build.sh --profile pi-headless     # Pi 5 / 4 / Zero 2 W, no desktop
sudo ./pi-edition/build.sh --profile pi-desktop      # Pi 5 / 4, Xfce
```

| Path | What it is |
|---|---|
| `build.sh` | mmdebstrap build → `out/pi/*.img.xz` + `.sha256` + `.manifest.json` |
| `versions.env` | Pi archive URL/suite, pinned archive key fingerprint, image layout (Debian/llama.cpp pins come from `../debian-edition/versions.env`) |
| `keys/raspberrypi-archive.asc` | Raspberry Pi archive signing key (public), checked against the pinned fingerprint at build time |
| `apt/victor-rpi.pref` | apt pin: the Pi archive supplies only the boot chain, kernels and board firmware |
| `profiles/` | `common.list`, `pi-desktop.list`, `pi-headless.list` and per-profile switches |
| `hooks/` | Pi-only chroot hooks; the scrcpy and cleanup hooks are reused from `debian-edition/` at build time |
| `overlay/` | Pi-only files: boot config, first-boot setup (GUI / console / file), `victor-pi`, board detection, hooks |
| `scripts/mkimage.sh` | Loop-device-free image assembly (FAT boot + ext4 root + ext4 `VICTORDATA`) |
| `tests/` | Unit tests for the helpers, package lists, pins, hygiene, hooks, LLM guard, image layout, shellcheck |
