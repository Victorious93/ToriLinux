"""Offline starting points, not a model benchmark or a compatibility guarantee.

Official Qwen GGUF cards were checked 2026-11-14. Memory budgets below are
deliberately conservative estimates for Q4_K_M, not download sizes. Nothing
in this module accesses the network or changes the owner's selected model.
"""
import os
import platform

GIB = 2**30
CATALOG = [("0.6B", .65), ("4B", 2.8), ("8B", 5.2), ("14B", 9.5), ("32B", 20.0)]


def cpu_info():
    name = platform.processor() or platform.machine()
    try:
        with open("/proc/cpuinfo") as f:
            name = next((line.split(":", 1)[1].strip() for line in f if line.startswith("model name")), name)
    except OSError:
        pass
    return {"name": name, "architecture": platform.machine(), "threads": os.cpu_count() or 1}


def recommend(ram_available, vram_free, backend, context=4096):
    """Use free memory on THIS host. Do not add RAM to VRAM or assume multi-GPU pooling."""
    context = max(256, min(int(context), 1 << 20))
    gpu = backend not in (None, "cpu") and vram_free is not None and vram_free > 0
    budget = max(0, (vram_free if gpu else ram_available) or 0) * (0.85 if gpu else 0.75)
    result = []
    for size, weights in CATALOG:
        total = int(weights * GIB * 1.1 + max(.5 * GIB, context * 512 * 1024))
        fit = "runtime unavailable" if backend is None else "recommended" if total <= budget else "too large / reduce context"
        memory = "dedicated GPU memory" if gpu else "available system memory"
        reason = f"Estimated fit against {memory}, with headroom. Test the imported GGUF before relying on it."
        if backend is None:
            reason = "Install or boot an image with a llama.cpp runtime first."
        elif fit != "recommended":
            reason = f"Estimate exceeds the reserved {memory} budget; try a smaller model or shorter context."
        result.append({"name": f"Qwen3-{size} Q4_K_M", "context": context, "estimated_total": total,
                       "fit": fit, "reason": reason, "source": f"https://huggingface.co/Qwen/Qwen3-{size}-GGUF"})
    return sorted(result, key=lambda m: (m["fit"] != "recommended", -m["estimated_total"]))


def report(ram, gpus, backend, context=4096):
    # Shared/integrated memory is not added to RAM. Unknown VRAM uses the conservative RAM path.
    eligible = [g for g in gpus if backend not in ("cuda", "hip") or
                g.get("vendor") == ("nvidia" if backend == "cuda" else "amd")]
    free = [max(0, g["vram_total"] - (g.get("vram_used") or 0)) for g in eligible if g.get("vram_total")]
    vram = max(free) if free else None
    return {"cpu": cpu_info(), "ram": ram, "vram_free": vram, "backend": backend,
            "models": recommend(ram.get("available", 0), vram, backend, context),
            "note": "Offline Q4_K_M starting points, not benchmarks. Memory is an estimate including context and headroom. "
                    "This PC supplies the CPU/GPU whether the ISO is on a USB stick or exposed by Victorious Flash on a phone. "
                    "The model must still be imported and tested; there are no automatic downloads."}
