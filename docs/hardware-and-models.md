# Hardware adaptation and model selection

Everything below is computed from **the PC that is booted right now**, every time a model starts. It does not matter which machine built the ISO or whether the ISO came from a USB stick or from a phone running Victorious Flash. Nothing measured is saved into `settings.json`, so a profile made on a strong PC cannot carry an oversized context or GPU-layer count onto a smaller one. Only the owner's *preferences* are saved.

## How the plan is made (`hostplan.py`)

1. **Backend**: `victorcommon.choose_backend` picks CUDA, Vulkan or CPU from what is physically present in the image and from real devices. Software Vulkan renderers (llvmpipe, lavapipe, SwiftShader) are never acceleration. A missing driver or unreadable `/dev/dri` node gives an explanation and a CPU fallback, never a claim of GPU use.
2. **Memory pool** (`select_device`), per selected device:
   * `dedicated`: a GPU that reports at least 1 GiB of its own VRAM. Budget = 88 % of its *free* VRAM.
   * `shared`: integrated graphics or a small carve-out (< 1 GiB, a heuristic). Budget comes from available system RAM, never from "VRAM".
   * `unknown`: a real GPU whose memory size the kernel does not report (some Intel drivers). Planned against RAM and flagged.
   * `system`: CPU inference. Budget = 75 % of available RAM.
   Dedicated VRAM and RAM are **never added together**, and different GPUs are **never pooled**. With several NVIDIA cards the one with the most free memory is used and pinned with `CUDA_VISIBLE_DEVICES` (PCI bus order). With several Vulkan devices the order cannot be matched to a card automatically, so the plan uses the *smallest* card's budget and says so; set `GGML_VK_VISIBLE_DEVICES` to pin one.
3. **Context** (`auto_context`): with *Auto context* on (the default for new installs) the largest standard size (2048 … 32768, never above the model's trained maximum) whose weights + KV cache + runtime overhead fit the budget. If KV size cannot be derived from the model's metadata it stays at 4096. Turn Auto off to use your own number. An existing `settings.json` that already had a `ctx` keeps it (Auto off).
4. **GPU layers**: everything if the whole estimate fits in 92 % of free VRAM, otherwise a proportional partial offload; shared/unknown memory offloads all and the runtime fails loudly if it cannot; CPU uses 0. A non-negative owner value always wins.
5. **Threads**: physical cores (via sysfs SMT siblings), minus one on machines with more than four. An owner value always wins.
6. **Storage / host profile**: the measured result is written to `/run/victor/host.json` (tmpfs, gone at shutdown) for display and diagnostics only.

Weights are not total memory. The estimate is `weights + KV cache + overhead`, from the real GGUF header (all shards of a split model are summed).

## Estimates versus proof

* `fit: fits | partial | too large | unusable` is an **estimate**.
* After a model starts, the Control Center sends one real one-token chat completion. Only if that succeeds is the file recorded in `config/verified.json` with its size, mtime, backend and device. The ranking then shows **verified-here**. A record made on other hardware shows **verified-elsewhere**; a changed file or never-run file shows **untested**.
* An architecture not in the known list is labelled so ("may not load, test before relying"). `.gguf` alone does not mean compatible.

## Models

* `GET /api/recommendations` (Control Center, loopback) returns the host facts, a ranked list of **your** local models, and the optional offline catalog (Qwen GGUF cards, official links). Nothing is downloaded automatically.
* **Split GGUF import** (`victor-model-import FIRST-00001-of-0000N.gguf`) now copies *every* shard as one all-or-nothing set: all shards must exist, each is header-checked, hashed while copying and size-checked; files are renamed into place only after every shard succeeded and any failure removes all partial files. A single checksum cannot cover several files, so per-shard SHA-256 values are printed instead.

## Limits (read these)

* A real GPU, a real model and a 20 GiB card have **not** been exercised in the development environment; those paths are covered by synthetic device tests. A CPU-only end-to-end run (real `llama-server`, real GGUF, real generation) was done; see `2026-11-14-victorlinux-testing.md`.
* The "universal" Arch selector builds the CUDA profile (CPU + Vulkan + CUDA runtimes, AMD/Intel Vulkan drivers). It does not make every GPU or any ARM PC work.
* Memory attribution for Vulkan multi-GPU is conservative by design (see above).
