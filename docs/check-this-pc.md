# Check this PC

Use **Logs & Diagnostics → Check this PC** (or *Full PC check* on the Dashboard, or `victor-check` in a terminal) to
see in one list what this PC can do and what to fix.

| Check | What a problem looks like | Suggested fix |
|---|---|---|
| CPU | No AVX2 | Prefer a GPU, or small models at Q4 |
| Memory | Under 8 GiB | 1-4B models, small context |
| Driver | No driver, `nouveau`, legacy `radeon`, `vfio-pci` | Boot without `nomodeset`, use the CUDA image, `amdgpu.si_support=1`, remove passthrough |
| GPU access | `/dev/dri` missing or not readable | `usermod -aG render,video victor`, then log out and back in |
| Vulkan | Only llvmpipe/lavapipe | Software Vulkan is not acceleration; fix the rows above |
| Backend | CPU fallback | Collects the fixes above in one place |
| Storage | Temporary or missing, under 10 GiB free | Choose a drive in Storage |
| Speed test | Under 8 tokens/s | Smaller model or quantisation, or a GPU backend |

The **speed test** runs one short generation (64 tokens) on the model that is already loaded and reads
llama-server's own timings. It never starts, loads or downloads a model.

Each run is saved to `reports/` on your storage drive (mode 0600, 20 newest kept). `reports/profiles.json` keeps
the latest result for each PC, keyed by a short hash of CPU model, GPU models and RAM size (no serial numbers).
Nothing leaves the PC.

Debian edition: the same rules run through `victor-ctl diag check`, and from *Check this PC* on the Tk Diagnostics tab.
The speed test is Arch only for now; on Debian, use *Run benchmark*.
