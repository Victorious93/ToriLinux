# Roadmap

* **GUI and headless variants for both editions** (requested; deferred): the Arch edition already builds `gui`, headless and `empty` ISOs via its own workflows. For Debian add `build.sh --variant gui|headless`
  (headless = no Xorg/Xfce/Firefox/LightDM, Control Center replaced by a TUI over `victor-ctl`, SSH/API only). Not implemented in this release.
* Image-generation packs (ComfyUI first) with per-pack venvs and offline wheel caches.
* Real-hardware verification on the RX 6800 XT (Vulkan benchmark, thermals) and optional ROCm 6.x pack using AMD's repository if it proves better.
* Debian snapshot-pinned reproducible builds; signed ISOs.
* Full Kali-based ISO profile; `nvidia-pstated` port; SecureBoot-friendly Ventoy enrolment guide.
