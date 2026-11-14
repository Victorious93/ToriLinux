# Roadmap

* **Headless variant**: implemented as `build.sh --variant headless` (see HEADLESS.md). Still missing: a TUI front end over `victor-ctl`, linger/unattended service start, hardware testing.
* Image-generation packs (ComfyUI first) with per-pack venvs and offline wheel caches.
* Real-hardware verification on the RX 6800 XT (Vulkan benchmark, thermals) and optional ROCm 6.x pack using AMD's repository if it proves better.
* Debian snapshot-pinned reproducible builds; signed ISOs.
* Full Kali-based ISO profile; `nvidia-pstated` port; SecureBoot-friendly Ventoy enrolment guide.
