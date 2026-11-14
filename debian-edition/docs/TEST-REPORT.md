# VictorLinux (Debian edition) — test report

Every number below comes from a run performed for this report against an ISO built from the commit named here. Raw outputs are in
[`test-evidence/`](test-evidence/). Nothing was copied from an earlier report. Results are split into **passed**, **failed** and **unavailable (not run)**.

## Subject under test

| | |
|---|---|
| Source commit | `a921f06` on `claude/victorlinux-debian-test-report-6jvn9p` (main `17c3bb5` + TORIDATA label support + boot-menu branding) |
| ISO | `VictorLinux-debian-trixie-amd-amd64-20261114.iso`, 1,039,138,816 bytes |
| SHA-256 | `fa4c5e32216e3fb15128f740448ff1f65e0394fbbf532b36a426ee176c01b9f9` (`test-evidence/*.sha256`) |
| Built with | `debian-edition/build.sh --profile amd` (default), live-build 20250505+deb13u1, debootstrap 1.0.145, 842 packages, kernel 6.12.117-1 |
| llama.cpp | tag `b12375`, commit `9d25e0014c4b4b316873c01f4938c0e3c687287d`, backends `vulkan` + `cpu(all-variants)` |
| Build host | Linux 6.18 container, 4 vCPU, 15 GiB RAM, **no `/dev/kvm`** (QEMU ran under TCG), behind a TLS-intercepting proxy: built with `EXTRA_CA_BUNDLE`, `BUILD_HTTPS_PROXY` and `http://` mirror overrides (APT signatures still verified) |
| Test model | `ggml-org/models` `tinyllamas/stories260K.gguf`, 1,185,376 bytes, SHA-256 `270cba1bd5109f42d03350f60406024560464db173c0e387d91f0426d3bd256d` |

The hosted-runner CI artifact (`victorlinux-debian-amd`, run 43574797365) was **not** used: it is built from an older commit (`ef3f022`) and the download host is blocked from
this environment. The ISO was rebuilt locally instead.

## Passed

| Suite | Result | Evidence |
|---|---|---|
| Unit tests (`tests/test_*.py`) | **29 / 29** (27 existing + 2 new for the `TORIDATA` label policy) | `unit.log` |
| Static ISO verification (`scripts/verify-iso.sh`) | **14 / 14** (13 existing + new boot-menu branding check) | `verify-iso.log` |
| Chroot integration (`tests/integration-chroot.sh`, chroot from this build) | **14 / 14** | `integration-chroot.log` |
| QEMU lifecycle suite (`tests/vm_test.py full`) | **53 / 53** | `vm-full-report.json` |
| Bootloader boot menus (`tests/vm_test.py bootloader`), BIOS and UEFI (OVMF) | both menus reached; **screenshots inspected by eye**: splash and entries read "VictorLinux" (the script's own check only asserts a screenshot exists) | `bootmenu-bios.png`, `bootmenu-uefi.png` |

What the QEMU suite covers (all passed): boot to running state; owner account locked with no shared password and no `NOPASSWD: ALL`; SSH off; no AI service at boot;
VICTORDATA auto-mount by UUID; Xorg + Xfce + Control Center autostart; dark theme default and light/dark toggle persisted; first-run owner password; model import/selection;
settings; single-model rule; loopback API self-test including SSE streaming; remote mode with TLS + API key (401 without key, streaming over TLS with a pinned certificate);
firewall opening while remote mode is on and closing on revoke; Kali refuses to start without an installed pack and no Kali repositories on the host; rootless podman present;
reboot persistence (credentials, settings, model, API key, certificate, theme) and offline inference after reboot; no external connections while offline; missing / read-only storage
handling; temporary RAM session; GPU-less backend selection reports the CPU fallback with a reason.

Before/after branding evidence: `baseline-before-branding-bios.png` is the same ISO build before the change ("Debian GNU/Linux 13 (trixie)", "Live system (amd64)").

## Failed

* **Tool calling with the test model: failed, as expected.** The in-image capability check reports `tool calling: model returned no tool call` for `stories260K.gguf`
  and the suite verifies that the endpoint does **not** advertise it. This is a 260K-parameter story model; it says nothing about real models. Tool calling is therefore
  **untested with a capable model**.
* No test in the four suites above failed in the final runs.
* Issues found and fixed during this session (not present in the final ISO): the first local build failed on TLS verification behind the proxy (environment, solved with the
  documented overrides); the first draft of the branding hook assumed the wrong working directory, missed isolinux's fail-safe label, and left a stray "Live Boot Menu with GRUB"
  title and an overlapping splash line. The ISO check now fails if any `Live system` / `Debian GNU/Linux` / `live-build` text remains in the menus.

## Unavailable / not run

No claim is made for any of the following.

* **Any real GPU** (including the RX 6800 XT): the VM has no GPU, so inference ran on CPU. **GPU acceleration is not demonstrated**; Vulkan (Mesa RADV) is present in the image only.
* Real USB hardware; **no USB was written** to by these tests (the QEMU suite uses disposable disk images).
* Secure Boot (UEFI tests ran with OVMF's non-Secure-Boot firmware).
* The boot menus were screenshotted, but the **full suite boots via `-kernel/-initrd` extracted from the ISO** with test-only kernel arguments. Booting the ISO end to end through the
  bootloader to a desktop was not repeated in this run (it was checked in an earlier session, not re-run here).
* A Ventoy-prepared disk, `provision-usb.py` provisioning on virtual disks, and the `TORIDATA` label on a real shared drive (the label is unit-tested only).
* DroidCommand-AI, tool calling with a real model, Tailscale/Headscale, the NVIDIA profile build, ROCm (not shipped), Kali image pull/run, image-generation packs.
* Performance: no benchmarks were run; TCG results say nothing about speed.
* The GitHub workflow on the new commits (nothing from this session was pushed when the tests ran).

## Reproduce

```
sudo ./debian-edition/build.sh --profile amd                 # see docs/BUILD.md for proxy/mirror overrides
sudo ./debian-edition/scripts/verify-iso.sh out/debian/*.iso
python3 -I -m unittest discover -s debian-edition/tests -p 'test_*.py'
sudo ./debian-edition/tests/integration-chroot.sh <workdir>/chroot stories260K.gguf   # needs iproute2, e2fsprogs
sudo python3 -I debian-edition/tests/vm_test.py full out/debian/*.iso stories260K.gguf OUTDIR   # needs qemu, OVMF, xorriso
sudo python3 -I debian-edition/tests/vm_test.py bootloader out/debian/*.iso OUTDIR
```
