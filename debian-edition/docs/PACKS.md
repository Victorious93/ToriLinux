# Optional packs and feature parity with the Arch edition

| Arch edition feature | Debian edition status |
|---|---|
| llama.cpp | **Ported**, pinned newer (b12375 vs b4048), Vulkan + CPU; HTTPS + auth |
| ComfyUI, InvokeAI, SD.Next (`automatic`) | **Not ported.** They need large pip/torch stacks (several GB) fetched online and per-app venvs. Planned as explicit, separately-verified packs; nothing claims they work. |
| SillyTavern | **Not ported** (Node.js app; planned as a pack) |
| vLLM (CUDA only) | **Not ported**; not a goal for AMD |
| `nvidia-pstated` | **Not ported**; replaced by idle model unload. Needs a pinned, hash-verified binary or source build. |
| CUDA/NVIDIA ISO | NVIDIA **profile** configured (driver + Vulkan), untested; no CUDA toolkit |
| ROCm ISO | Not ported — Vulkan instead; ROCm is experimental (docs/GPU.md) |
| Headless/GUI/empty variants | Debian: GUI only for now; see ROADMAP |
| Ly + Openbox | replaced by LightDM + Xfce |

Pack rule: a pack is a separate download/import onto VICTORDATA (`images/`), each with its own Python venv, pinned revisions and a checksum, installed only on an explicit action.
