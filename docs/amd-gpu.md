# AMD GPU path (Radeon RX 6800 XT)

* **Primary: llama.cpp Vulkan (Mesa RADV).** Requires only the kernel `amdgpu` driver, `linux-firmware` and
  `vulkan-radeon`, all in the image. No ROCm needed. RDNA2 (Navi 21, PCI ID `1002:73bf`) is a mainstream RADV target.
* **Optional: HIP/ROCm** (`--packs hip`, builds llama.cpp with `-DGGML_HIP=ON`, target `gfx1030`). Experimental:
  it increases the image by many GB and is only worth it if it benchmarks better on your card.
* **CPU fallback** is always present. When no usable GPU/Vulkan device is found the UI shows a banner explaining why.
* vLLM is not part of the AMD path.

## Never trust "driver present"

The Control Center reports acceleration from evidence: after a model starts it parses llama-server's log for
`offloaded N/M layers to GPU` and device lines, and warns if a GPU backend was selected but 0 layers were offloaded.
`tori-diag` prints: GPU/VRAM from sysfs, `/dev/dri` permissions, `vulkaninfo --summary` devices, the chosen backend
and why, storage state and the log tail.

## Measuring

`tori-bench vulkan model.gguf` (llama-bench, 3 repetitions) writes results next to your data. Record them in
[test-report.md](test-report.md) - the hardware results in that file are what counts.

## Power/clock

No clock/voltage manipulation is shipped. Idle behaviour is handled by unloading: set *Idle unload (s)* and
llama-server sleeps/unloads the model after that idle time; *Stop / unload* frees VRAM immediately.
`nvidia-pstated` is still installed on the `cuda` platform only.
