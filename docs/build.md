# Building VictorLinux

## Network: build time vs. run time

| Phase | Internet needed? | What is fetched |
|---|---|---|
| `scripts/prepare-sources.sh` | yes | llama.cpp at a pinned tag (verified against a pinned commit SHA) |
| `mkarchiso` (image build) | yes | Arch packages from an **Arch Linux Archive snapshot** (`arch_snapshot` in `.ci/options.ini`) |
| Optional packs | yes | pip/npm/git (PyTorch wheels, ComfyUI, ...) |
| **Booting and running the image** | **no** | Nothing. No package updates, downloads, telemetry or cloud calls are configured |

## Local build (documented, no secrets, no special runner)

```sh
scripts/build.sh [amd|cuda|rocm] [headless|gui|empty] [--packs a,b,c]
```

* With Docker/Podman it runs `archlinux:latest` privileged and calls `scripts/build-inner.sh`.
* On Arch as root it runs `scripts/build-inner.sh` directly.
* Disk: allow ~20 GB for the lean image, much more for CUDA/HIP/packs (tens of GB).
* Outputs in `out/`: `victorlinux-*.iso`, `*.iso.sha256`, `manifest-*.json`, `packages-*.txt`, `build-*.log`.

`scripts/check-packages.sh` (run in Arch) verifies every package exists in the pinned snapshot before a long build.

### Profiles

* **platform** `amd` (Vulkan + CPU), `cuda` (CUDA + Vulkan + CPU, NVIDIA), `rocm` (= `amd` + experimental `hip` pack)
* **type** `headless`, `gui` (Openbox, autostarts the Control Center), `empty` (OS + drivers only)
* **packs** `hip comfyui automatic invokeai sillytavern vllm` - all off by default

## Pinning and reproducibility

`.ci/options.ini` pins: llama.cpp tag **and** commit, ComfyUI, SD.Next, nvidia-pstated, and the Arch snapshot date.
The manifest records the git commit (and whether the tree was dirty), llama.cpp commit, every installed package
and version (`packages-*.txt`), GPU-stack package versions (kernel, linux-firmware, mesa, vulkan-radeon, ...), and the
build environment. `.ci/update.py` is a *manual* helper to look up newer upstream releases; nothing updates
automatically, and nothing updates at boot.

Python environments are isolated per application (venv per project, pipx for InvokeAI/vllm); llama.cpp has no
Python dependency at runtime. The Control Center uses only the Python standard library.

## GitHub Actions

* `.github/workflows/validate.yml` - renders every profile, shellcheck, python tests (no secrets).
* `.github/workflows/build-iso.yml` - manual build on a GitHub-hosted runner or any runner label you provide.
  GitHub-hosted runners have limited disk: fine for the lean image, use your own runner for CUDA/HIP/packs.
* The original maintainer's SSH secrets and self-hosted runner are **not** used anywhere.

## Verification gates

See [test-report.md](test-report.md). Do not publish an ISO as "tested" unless those gates pass for it.
