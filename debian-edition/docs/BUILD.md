# Building

## Prerequisites
Debian or Ubuntu build host, root, **≥ 18 GiB free** (the script checks), ≥ 8 GiB RAM recommended (4 cores: ~25 min for llama.cpp + ISO assembly),
internet during the build only. Tools: `live-build` (reference: Debian trixie `20250505+deb13u1`), `debootstrap` ≥ 1.0.145, `xorriso`, `squashfs-tools`, `git`.
Ubuntu's own `live-build` package is an old 3.x fork: install Debian's `.deb` as `.github/workflows/debian-iso.yml` does (SHA-256 pinned).

## Local build
```sh
sudo ./debian-edition/build.sh [--profile amd|nvidia] [--workdir DIR] [--out DIR] [--clean]
```
Outputs (in `out/debian/`): `VictorLinux-debian-trixie-<profile>-amd64-<date>.iso`, `.sha256`, `.manifest.json` (ISO identity, pins, kernel/firmware/driver/runtime
versions, full package list, build environment) and `build-<timestamp>.log`. Any hook or `lb` failure aborts with a non-zero exit (`set -Eeuo pipefail` everywhere).

## What is pinned
`versions.env`: Debian suite, llama.cpp **tag and full commit** (the hook refuses a commit mismatch), Kali image **digest**, Ventoy version.
Debian packages come from the live `trixie` mirrors at build time; the exact versions used are recorded in the manifest (not frozen — for bit-for-bit
reproducibility point `DEBIAN_MIRROR` at a snapshot mirror you control, e.g. `https://snapshot.debian.org/archive/debian/<timestamp>/`).
Python environments: the image uses distro Python only; no pip-installed ML stacks are in the default image, so there is no cross-project dependency conflict to isolate.
Optional packs (image generation) must each use their own venv — see PACKS.md.

## Overrides
`DEBIAN_MIRROR`, `DEBIAN_SECURITY_MIRROR`; behind a TLS-intercepting proxy: `EXTRA_CA_BUNDLE=ca.pem BUILD_HTTPS_PROXY=...` (used only inside the build chroot and
deleted by hook `9999`, never shipped).

## CI
`.github/workflows/debian-iso.yml`: static checks + unit tests, then the ISO build on a hosted `ubuntu-24.04` runner or your own (`runner` input / repo variable `VICTOR_RUNNER`).
Hosted-runner limits: 4 vCPU/16 GB, ~14 GB root disk (build runs on `/mnt`), 6 h cap, no GPU, so **GPU tests cannot run in CI**. Nothing depends on the original
maintainer's secrets or runner. (The Arch ROCm workflows still do; they are untouched.)
**This workflow has not been executed on GitHub in this session** (nothing was pushed); the same commands were run locally.

## Hooks
`0100` build llama.cpp (build deps installed then purged), `0200` system configuration (owner user, services, sudoers, journald limits), `9999` cleanup + hard
failure if a default credential, `NOPASSWD: ALL`, baked SSH host key or machine-id slipped in.
