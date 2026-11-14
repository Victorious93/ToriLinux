# VictorLinux - portable offline AI workstation on a USB drive

**Your portable, private AI workstation.** Boot a PC from a USB drive or from an ISO exposed by **Victorious Flash** on a supported rooted Android phone. In both cases, inference runs on the **PC's CPU, RAM and GPU**.

VictorLinux is maintained by **Victorious / Victorious Framework**. It is the renamed Victorious fork of [sasha0552/ToriLinux](https://github.com/sasha0552/ToriLinux), with original MIT attribution preserved in [LICENSE](LICENSE) and [NOTICE](NOTICE). The repository URL remains [Victorious93/ToriLinux](https://github.com/Victorious93/ToriLinux); this source change does not rename the GitHub repository.

## Choose an edition

| | Arch edition (repository root) | [Debian edition](debian-edition/README.md) |
|---|---|---|
| Desktop | Openbox + local web Control Center | Xfce + Tk Control Center |
| Appearance | Dark GTK/browser/terminal settings, rounded responsive web tabs/cards | Dark theme enforced at login; rounded Tk tab backgrounds; dark terminal, greeter and chat browser |
| Inference | Separate CPU/Vulkan builds; optional NVIDIA CUDA and HIP builds | CPU + Vulkan; optional NVIDIA driver profile |
| Launch | `victor-open` | `victor-control` |
| Hardware advice | Dashboard → **Check this PC & suggest models**, or `victor-hardware` | Diagnostics → **Suggest models for this PC**, or `victor-ctl hw recommend` |
| Storage | `VICTORDATA` → `victorlinux/` (legacy `TORIDATA`/`tori/` reused) | `VICTORDATA` or `TORIDATA`, Debian's own layout at the partition root |
| Variants | GUI, headless, empty | GUI; headless work is on the [roadmap](debian-edition/docs/ROADMAP.md) |

The editions can recognize the same data partition label, but have **different configuration and chat layouts**. They do not automatically migrate or synchronize each other's settings.

## What it does

- Detects hardware on the PC you actually booted. In automatic mode it selects a packaged hardware backend, calculates GPU offload at model start and uses llama.cpp's CPU defaults for that PC. A software Vulkan renderer is reported as CPU fallback.
- Offers an **offline model advisor** with conservative memory estimates, context/headroom allowances and official Qwen GGUF model-card links. A 20 GiB GPU is evaluated using available memory; it is not treated as a promise that every 20 GB model will fit. Weights, context, runtime buffers, free RAM and driver support all matter. Model import and a real inference test are still required.
- Uses rounded navigation, generous spacing, visible focus states and layouts checked at 1366, 768 and 390 pixels. VictorLinux's own interface always stays dark, including when the host/browser prefers light. Third-party web content can use its own styles.
- Runs local inference without telemetry, automatic model downloads or a cloud fallback. Models are supplied separately as GGUF files.
- Stores models, chats, settings, documents and logs on selected persistent storage. Changes to the live operating system itself are **not** persistent.
- Exposes an optional authenticated OpenAI-compatible API for DroidCommand-AI, VictorSuite and other clients. The administrative Control Center stays local to the PC.

## Build and boot the Arch edition

Build-time internet is required. The built image can boot and infer offline. Use Docker/Podman or a root Arch build environment; see [build details](docs/build.md).

```sh
scripts/build.sh amd gui
# out/victorlinux-*.iso plus checksum, manifest, package list and build log

# Broader AMD/Intel/NVIDIA portability: includes the CUDA profile's NVIDIA stack.
scripts/build.sh universal gui
# universal is an alias for cuda; the artifact profile is named cuda.
```

The AMD profile includes AMD/Intel Vulkan drivers. The broader profile also includes the pinned NVIDIA driver and CUDA stack; its supported NVIDIA generations are constrained by that driver. Neither image can guarantee acceleration on arbitrary hardware or boot an ARM PC.

To prepare a **physical USB**, first read [USB preparation](docs/usb.md). The write command **erases the selected drive**; use its plan to confirm capacity, model and serial. Replace the ISO path with one exact file, especially if `out/` contains several builds.

```sh
sudo scripts/make-usb.sh list
sudo scripts/make-usb.sh plan --iso out/EXACT-victorlinux-image.iso --device /dev/sdX
# Only after checking the plan and backing up the target:
sudo scripts/make-usb.sh write --iso out/EXACT-victorlinux-image.iso --device /dev/sdX --confirm-serial ACTUAL_SERIAL
```

The standard raw layout appends persistent `VICTORDATA` storage and computes sizes for the actual drive, including 64 GB and 128 GB drives. Ventoy is optional; the earlier Ventoy/UEFI failure remains unresolved until retested. Boot from the PC firmware's boot menu, then open `victor-open`, select storage, import a GGUF and press **Start**.

## Boot media: a USB stick **or** a phone (same ISO, same PC hardware)

The PC always runs VictorLinux and supplies the CPU and GPU. The ISO can reach it two ways; verify the **same SHA-256** for both.

1. **USB stick** written by [Victorious Flash](https://github.com/Victorious93/victorious-flash) (desktop, or Android over OTG): pick the ISO (a downloaded one, or **a local image** such as your own build with its published SHA-256), choose the stick, type its name to confirm, and wait for the read-back verification. Tick **Also add a persistent VICTORDATA partition** to add the data partition in the same run; the layout is computed from the stick's real size and the image's own partition table (about 1 GiB is left unallocated), for 64 GB and 128 GB media alike. Raw ISO writing and persistence are separate steps inside one run: the ISO read-back hash is verified first, then only the 16-byte slot-3 entry of the MBR is added and `VICTORDATA` is formatted ext4.
2. **Phone as the boot medium** (rooted Android, kernel USB-gadget support): Flash re-hashes the ISO against the trusted SHA-256 at the moment you press *Expose*, exposes it read-only as a USB disk or CD-ROM, watches the file while it is exposed (if it changes, USB is restored), and restores the phone's USB configuration when you stop, when the app is closed, or if the expose fails. MTP, ADB and tethering stop while exposed. Keep the phone connected until the PC has shut down.

Persistence with the phone as the boot medium is **not** available on that same medium (emulation is read-only). Use a separate writable data drive carrying the `VICTORDATA` label, or an explicitly temporary session. A second writable data LUN on the phone was deliberately **not** implemented.

Phone gadget mode (the phone is a USB *device* to the PC) and OTG (the phone is a USB *host* to a stick) are different roles; one port cannot do both at once.

The no-root HTTP/EFI path in Flash is Ubuntu/casper-only and is **not supported for VictorLinux**; Flash says so instead of trying. Flash does not install VictorLinux onto an internal disk. No VictorLinux download exists in Flash's catalog because no release has been published yet; use a local image.

Boot support for this revised ISO has **not** been verified on a real phone or PC; no ISO was built in the development environment (see the test report).

## Hardware adaptation, models and the phone companion

At every model start the PC measures itself and plans accordingly: [hardware and model selection](docs/hardware-and-models.md) (device memory pools, automatic context, threads, GPU layers, ranked local models, estimates vs verified generation, atomic split-GGUF import). Check it with `victor-hardware` or *Check this PC & suggest models* in the Control Center.

[Victorious Browser](https://github.com/Victorious93/victorious-browser) (Android) is the phone companion: **Tools → VictorLinux PC**. It reads a read-only, authenticated report from the inference gateway and chats with the PC's local model; see [the companion API](docs/companion-api.md). It is an Android app, not a Linux desktop browser; on a phone `127.0.0.1` is the phone. Keep Firefox as the PC browser. The Control Center stays on `127.0.0.1:8787` and is never published.

## Commands after the rename

| Previous command | VictorLinux command |
|---|---|
| `tori-open` | `victor-open` |
| `tori-control-center` | `victor-control-center` |
| `tori-diag` | `victor-diag` |
| `tori-model-import` | `victor-model-import` |
| `tori-bench` | `victor-bench` |
| `tori-capcheck` | `victor-capcheck` |
| `tori-tls-cert` | `victor-tls-cert` |

Implementation directory: `/usr/local/lib/victorlinux`. Arch account: `victor`; cache: `$XDG_CACHE_HOME/victorlinux` or `~/.cache/victorlinux`. Developer overrides use `VICTOR_DATA`, `VICTOR_RUN_DIR`, `VICTOR_LLAMA_ROOT`, `VICTOR_CC_PORT` and `VICTOR_PRIV`. New public gateway routes are `/victor/health` and `/victor/capabilities`; the corresponding `/tori/` routes remain compatibility aliases for existing clients. Existing data is reused without relabelling or moving it.

## Verification and manuals

Read [the current dated test report](docs/2026-11-14-victorlinux-testing.md) before treating these changes as a release. [The older ISO report](docs/test-report.md) describes earlier artifacts and does **not** certify this revision. New ISO builds, physical GPU inference, USB writes, native Debian UI rendering and phone-hosted firmware boots must be checked separately.

For coordinated use with Flash and Browser, see the [integration guide](docs/fleet-integration.md). The companion handoff ZIP includes a complete user manual, an offline HTML manual and one Claude Code master prompt to finish and verify the coordinated release against the current repositories.

Developer checks:

```sh
python3 -m pytest -q tests debian-edition/tests/test_logic.py debian-edition/tests/test_layout.py
# Requires Playwright and its Chromium installation:
node tests/browser_ui.js
```

[Build](docs/build.md) · [USB/storage](docs/usb.md) · [AMD GPU](docs/amd-gpu.md) · [Remote access](docs/remote-access.md) · [Security](docs/security.md) · [Maintenance](docs/maintenance.md)

## Upstream credits

Original ToriLinux: [sasha0552](https://github.com/sasha0552). Runtime/build components include [llama.cpp](https://github.com/ggml-org/llama.cpp), [Arch Linux](https://archlinux.org/), [Debian](https://www.debian.org/), [Ventoy](https://github.com/ventoy/Ventoy) and the optional ComfyUI, SD.Next, InvokeAI, SillyTavern, vLLM and nvidia-pstated packs. Each component retains its own license.
