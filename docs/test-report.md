# Test report

Honest status of what was and was not verified for the ISO built from this branch. "Verified" means observed in this
session; "unverified" means not tested, **not** "works".

**Artifact:** `out/torilinux-amd-headless-2026-11-14_04-10-37-x86_64.iso` (profile `amd headless`, 1,906,081,792 bytes)
SHA-256 `261d68fae90559942e2bd6029c7dd9438b5d372daffdb912ac55033dc97adc88`; manifest, package list (401 packages) and
build log are next to it in `out/`. Pins: llama.cpp `b12374` = `9183a552e5fdcbdb5243f187f3a63204eca7dba9`, Arch snapshot `2026/11/13`.

Test environment: a cloud VM (4 vCPU, no KVM, **no GPU, no physical USB port**); the image was booted in QEMU (software emulation,
no network card attached unless stated).

## Verified

| Area | Evidence |
|---|---|
| Build | `scripts/build-inner.sh amd headless` ran to completion in an Arch chroot from the pinned snapshot; llama.cpp **CPU and Vulkan** backends compile (772/772 steps); ISO + `.sha256` + manifest + package list + log produced; ISO checksum verifies |
| Image contents | Both backends + embedded llama web UI present; `/etc/tori-release` stamp; `root` and `tori` locked (`!*`); sshd not enabled; sudoers = one helper only; `llama-server --version` = `build 12374, commit 9183a55` |
| Boot, UEFI | OVMF + the ISO's systemd-boot -> kernel -> auto-login (screenshot) |
| Boot, BIOS | SeaBIOS + ISOLINUX -> kernel -> auto-login (screenshot) |
| Offline | All VM tests ran with **no network interface**; boot reached `running`, 0 failed units |
| Listeners | After the fix: **no** TCP/UDP listener on non-loopback addresses (first build had LLMNR/mDNS on :5353/:5355; fixed and re-verified in the final ISO) |
| Storage, boot-time | `tori-boot-setup` found an emulated USB stick labelled `TORIDATA` (bare filesystem and as partition 3) and mounted it; no stick -> explicit "No persistent storage found" message (no silent RAM fallback) |
| Persistence across reboot | In the final ISO: model imported (checksum-verified), settings (`ctx=3333`) and a document written, clean power-off, reboot with same stick: all present |
| Import | Hot-plugged second USB stick -> mounted read-only -> `tori-model-import` with SHA-256 match; GGUF header validation; bad files / wrong checksum rejected (unit tests) |
| Control center + gateway + API | `tests/integration_live.py`: **36 checks pass inside the booted ISO** with the packaged llama-server (CPU, test model `stories15M-q4_0.gguf`, not bundled): token/Host checks, CPU-fallback explanation, estimate, start/stop, OpenAI + SSE streaming, Ollama-compatible + llama.cpp-native routes, remote needs API key (401 without/wrong, 200 with), key stored hashed, capability test run for real (streaming PASS), revoke, diagnostics without key leakage, llama-server started without `--tools` |
| Capabilities honesty | tool calling reported `unverified` until tested; the tiny test model **fails** the tool-calling test, which is recorded, never advertised |
| Diagnostics, no GPU | `tori-diag` correctly reports no GPU, Vulkan unavailable (with the loader error) and CPU fallback |
| Unit tests | 8 pytest tests; `tests/root_persist.sh` (7 checks on a real ext4 loop image); shellcheck clean; all 7 profiles render |
| USB layout A: hybrid ISO + data partition | `make-usb.sh write --layout raw` onto a 40 GB image: ISO read back and checksum-verified, `TORIDATA` created; **boots under UEFI** and finds `TORIDATA` on the same stick |
| USB layout B: Ventoy 1.1.18 + ISO + data partition | script built it and verified the copied ISO; **legacy BIOS mode boots** the ISO through Ventoy and finds `TORIDATA` |
| Safety logic of `make-usb.sh` | refused the system disk, a non-USB transport, a partition instead of a disk, and a serial mismatch; identity rechecked before writing |
| Gateway/API against other clients | VictorSuite adapter: 21 unit tests against stub servers (not a phone) |

## Failed / unresolved

* **Ventoy + UEFI did not boot this ISO in QEMU/OVMF** (both "normal" and "grub2" modes; the display went blank and the guest never
  mounted storage). It could be an emulator artefact or a real Ventoy/ISO incompatibility; unresolved. Hence `--layout raw` is the default.
  If you want Ventoy, test UEFI boot on your PC before relying on it.
* `systemd-boot` menu timeout/entries and the "safe graphics" entry were not exercised.

## Not verified (no hardware / out of scope here)

* **Any physical GPU.** No Vulkan device, VRAM reading, offload log (`offloaded N/M layers`) or tokens/s on the **RX 6800 XT** has been observed.
  The only performance number is a CPU smoke test: llama-bench, 4 vCPU, 17 MiB test model: pp128 ~11.8k t/s, tg32 ~1.56k t/s (meaningless for real models).
* Physical USB flashing (no removable drive was attached), Secure Boot, real firmware/USB-controller quirks, speed of a real stick.
* The `hip`/ROCm pack: a build was attempted and **failed** (the build host's disk allowance could not hold the ROCm SDK: "could not extract ... rocfft (Write failed)"). Unbuilt, unverified.
* All image-generation packs (ComfyUI, SD.Next, InvokeAI, SillyTavern, vllm): rendered only, **not built** (download.pytorch.org is unreachable from the build host).
* Boot of the `cuda` image: built but **not booted**; no NVIDIA GPU was available, so the CUDA backend has not run. It was built *before* the Control Center CSP fix (below) and still contains that bug.
* The "Problem loading page" second Firefox tab seen in the gui image was not investigated.

## Additional builds (build-only evidence)

| Image | Size (bytes) | SHA-256 |
|---|---|---|
| `torilinux-amd-gui-2026-11-14_06-16-04` | 2,213,314,560 | see `.sha256` (superseded: Control Center UI blank, see below) |
| `torilinux-amd-gui-2026-11-14_09-24-13` | 2,213,314,560 | `d912ffcc74582e944cd62198a0986432ea4309aa226f0f5f69ac44b537c0e2fe` |
| `torilinux-cuda-gui-2026-11-14_07-02-47` | 5,878,272,000 | `1372441e9b0f0c19a92d0994b366419807d7b0d876e8879b163090e7a797d77b` |

The CUDA image ships cuda 13.4.3 and nvidia-utils 620.70.06 from the pinned Arch snapshot; that driver branch supports Turing and newer only (Pascal dropped). The nvidia-pstated source is now prefetched and sha256-verified on the host (the in-chroot wget TLS failure is fixed).
* GitHub Actions workflows: written, not executed.
* HTTPS (`tori-tls-cert`) and SSH enable/disable on a booted image (key validation was tested on a host; sshd start was not).
* Tailscale/Headscale and the DroidCommand-AI app; VictorSuite adapter on a phone.
* Tool calling with a real tool-capable model.

## GUI image boot test (QEMU, TCG, no GPU, no network)

* Booted `amd-gui-2026-11-14_09-24-13` with a 512 MiB ext4 `TORIDATA` stick attached as USB: reached `running` in ~150 s, 0 failed units, auto-login, Xorg, Openbox, tint2, Firefox, wallpaper; `TORIDATA` mounted, Control Center reported "persistent storage".
* Control Center in Firefox rendered all five tabs; dashboard showed the CPU-fallback banner and "No GPU detected" (expected in the VM).
* **Bug found and fixed (PR #3):** the first gui build's Control Center sent `Content-Security-Policy: default-src 'self'`, which blocked its inline `<script>`; Firefox showed only the header. API-only tests had missed it. Fixed with a per-response CSP nonce; `tests/browser_ui.js` (headless Chromium against the real server) fails without the fix and passes with it. The rebuilt image above contains the fix and was booted to confirm.
* Not covered: Vulkan inference, model load, the Open chat button, real hardware.
