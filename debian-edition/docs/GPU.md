# GPU and inference backends

* **Primary: llama.cpp + Vulkan** (Mesa RADV for AMD; works on NVIDIA's proprietary driver too). Target: Radeon RX 6800 XT 16 GB (RDNA2/Navi 21).
* **CPU fallback**: automatic when llama.cpp finds no hardware Vulkan device. The Control Center and `victor-ctl hw backend` print *why* (no `/dev/dri` access, no driver/firmware,
  or only the software renderer `llvmpipe`, which is deliberately treated as "not accelerated").
* **ROCm/HIP: not in the image.** Debian trixie ships an old ROCm (hipcc 5.7.1, rocBLAS 5.5.1). The exact combination llama.cpp-pinned + Debian ROCm 5.x + gfx1030 has **not** been
  built or run here, so it is *experimental/unverified* and not enabled. Vulkan is the supported AMD path. (vLLM is not required for AMD.)
* **NVIDIA profile**: `build.sh --profile nvidia` adds `nvidia-driver` (non-free, 550 series in trixie), `nvidia-persistenced`, firmware. Inference uses Vulkan on that driver. It is **configured but
  not built or tested** in this session. The Arch edition's `nvidia-pstated` performance-state switcher is *not ported* (see PACKS.md); idle VRAM is released by unloading the model instead
  (`--sleep-idle-seconds`, default 15 min); no GPU clock/voltage changes are made.

## What "verified" means here
Installed packages prove nothing. Acceleration is reported only from evidence:
1. `llama-server --list-devices` lists a non-software device (name + MiB total/free).
2. During `victor-ctl bench run`, sampled amdgpu `gpu_busy_percent` / VRAM growth (or `nvidia-smi`) must show real use (≥20 % busy or ≥50 % of the model size in new VRAM).
   Otherwise the verdict is "GPU use NOT verified".
3. Render-node permissions (`/dev/dri/renderD*`, group `render`) and `/dev/kfd` presence are reported, as are RAM, VRAM, kernel and driver.

## Verifying on the RX 6800 XT (to be done on the real PC)
Boot the USB, select the model (≥ a few hundred MB of VRAM-fit GGUF), then `victor-ctl hw snapshot`, `victor-ctl llama start`, `victor-ctl api selftest`, `victor-ctl bench run`.
**This was not performed in this session: the build environment has no GPU.** See TEST-REPORT.md.

## Memory control
One model is loaded at a time (a second start is refused); the service runs in a transient systemd unit with `MemoryMax` (default 85 % of available RAM, or your limit) and no swap; CPU
starts are refused up-front when the estimate exceeds the allowance; GPU starts warn when the estimate exceeds free VRAM. The model unloads itself after the idle timeout.
