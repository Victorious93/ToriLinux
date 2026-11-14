# ToriLinux (Victorious fork) - portable offline AI workstation on a USB drive

Arch Linux live image that boots from USB and runs local LLM inference, with first-class support for
**AMD Radeon GPUs through llama.cpp's Vulkan backend** (target hardware: Radeon RX 6800 XT 16 GB), a local
**Control Center**, persistent storage for models/chats/settings, and an optional authenticated
**OpenAI-compatible server** for other devices (for example the DroidCommand-AI Android app).

> Fork of [sasha0552/ToriLinux](https://github.com/sasha0552/ToriLinux) (MIT). Original work and
> attribution are preserved; modifications by **Victorious / Victorious Framework**.
> Original project and issue tracker: <https://github.com/sasha0552/ToriLinux>.
> This fork: <https://github.com/Victorious93/ToriLinux> (report fork issues
> [here](https://github.com/Victorious93/ToriLinux/issues/new)).

**Status honesty:** see [docs/test-report.md](docs/test-report.md) for exactly what has and has not been
verified. GPU acceleration on physical AMD hardware has **not** been verified by the author of this change set
unless that report says so.

## What it does

| Area | Behaviour |
|---|---|
| Inference | llama.cpp built from a pinned release. Backends: **Vulkan** (default for AMD), **CPU** (always present, visible fallback), CUDA (NVIDIA platform), HIP/ROCm (optional experimental pack) |
| Control Center | `tori-open` / desktop menu. Start/stop, model + backend + context + RAM cap, RAM/VRAM meters, logs, copyable diagnostics, storage chooser, remote-access settings |
| Offline | Boots and infers with no network. Models are imported from disk/USB, never auto-downloaded. No telemetry, no cloud fallback |
| Persistence | Models, chats, settings, images, outputs, documents, logs live on an ext4 partition labelled `TORIDATA`. **The operating system itself is not persistent** |
| Server | OpenAI-compatible API on `127.0.0.1` by default; LAN/VPN only by explicit owner setting, always API-key protected, optional HTTPS |
| Access | No shared passwords. SSH off until you add a public key. AI apps run as an unprivileged user; one restricted sudo helper |

## Quick start

```sh
# 1. Build (needs internet at BUILD time only; Docker/Podman or Arch root)
scripts/build.sh amd gui            # lean: llama.cpp (Vulkan+CPU) + control center + Openbox
# -> out/torilinux-*.iso, .sha256, manifest-*.json, packages-*.txt, build-*.log

# 2. Prepare the USB (ERASES the chosen drive; read docs/usb.md first). Default layout: hybrid ISO + TORIDATA data partition.
#    (--layout ventoy for a Ventoy multi-ISO stick; see docs/usb.md for its UEFI caveat)
sudo scripts/make-usb.sh list
sudo scripts/make-usb.sh plan  --iso out/torilinux-*.iso --device /dev/sdX
sudo scripts/make-usb.sh write --iso out/torilinux-*.iso --device /dev/sdX --confirm-serial <SERIAL>
```

Boot the stick (UEFI or BIOS), then run `tori-open`. Details:

* [docs/build.md](docs/build.md) - build profiles, packs, pins, CI, reproducibility
* [docs/usb.md](docs/usb.md) - Ventoy layout, sizing for 64/128 GB, what persists
* [docs/remote-access.md](docs/remote-access.md) - server, HTTPS, Tailscale/Headscale, DroidCommand-AI connection details
* [docs/security.md](docs/security.md) - access model, credentials reset
* [docs/amd-gpu.md](docs/amd-gpu.md) - Vulkan vs HIP, RX 6800 XT notes, diagnostics, benchmarking
* [docs/maintenance.md](docs/maintenance.md) - developer notes

## Profiles and optional packs

Default image = lean (no image-generation apps). Existing upstream applications remain available as **packs**
you opt into at build time: `comfyui`, `automatic` (SD.Next), `invokeai` (CUDA), `sillytavern`, `vllm` (CUDA),
and `hip` (ROCm). They are experimental in this fork until separately verified.

```sh
scripts/build.sh amd gui --packs comfyui,sillytavern
```

## Upstream credits

ToriLinux was created by [sasha0552](https://github.com/sasha0552). It bundles/uses
[llama.cpp](https://github.com/ggml-org/llama.cpp), [ComfyUI](https://github.com/comfyanonymous/ComfyUI),
[SD.Next (automatic)](https://github.com/vladmandic/automatic), [InvokeAI](https://github.com/invoke-ai/InvokeAI),
[SillyTavern](https://github.com/SillyTavern/SillyTavern), [vllm](https://github.com/vllm-project/vllm),
[nvidia-pstated](https://github.com/sasha0552/nvidia-pstated), [Ventoy](https://github.com/ventoy/Ventoy) and
[Arch Linux](https://archlinux.org/). The original ROCm build server was provided by
[@Sepera-okeq](https://github.com/Sepera-okeq/); this fork no longer depends on it.
