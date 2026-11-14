"""Repeatable inference benchmark with GPU-use evidence (does NOT claim acceleration from installed packages)."""
import json, os, threading, time
from .common import *
from . import hw, llama


def _sample(stop, out):
    while not stop.is_set():
        for v in hw.sysfs_vram() + hw.nvidia_smi():
            out.append((v.get("gpu_busy_percent") or 0, v.get("vram_used") or 0))
        time.sleep(0.5)


def run_bench(model_rel=None, pp=512, tg=128, reps=3):
    c = llama.load_config()
    model_rel = model_rel or c["model"]
    if not model_rel:
        raise VictorError("Select a model first.")
    if llama.is_running():
        raise VictorError("Stop the inference service before benchmarking (avoids two models in memory).")
    mp = os.path.join(data_path("models"), model_rel)
    devs = hw.llama_devices()
    backend = hw.select_backend(c["backend"], devs["devices"], None)
    base_vram = max([v.get("vram_used") or 0 for v in hw.sysfs_vram() + hw.nvidia_smi()] or [0])
    argv = [LLAMA_BENCH, "-m", mp, "-p", str(pp), "-n", str(tg), "-r", str(reps), "-o", "json"]
    argv += ["--device", backend["device"], "-ngl", "999"] if backend["accelerated"] else ["--device", "none", "-ngl", "0"]
    samples, stop = [], threading.Event()
    th = threading.Thread(target=_sample, args=(stop, samples), daemon=True); th.start()
    t0 = time.time()
    p = run(argv, timeout=3600)
    stop.set(); th.join(2)
    if p.returncode != 0:
        raise VictorError("llama-bench failed: " + (p.stderr.strip()[-400:] or p.stdout.strip()[-400:]))
    rows = json.loads(p.stdout)
    peak_busy = max([s[0] for s in samples] or [0])
    peak_vram = max([s[1] for s in samples] or [0])
    size = os.path.getsize(mp)
    gpu_evidence = backend["accelerated"] and (peak_busy >= 20 or (peak_vram - base_vram) >= 0.5 * size)
    return {"model": model_rel, "backend_selected": backend, "seconds": round(time.time() - t0, 1),
            "results": [{"test": f"pp{r.get('n_prompt')}/tg{r.get('n_gen')}", "tokens_per_second": round(r.get("avg_ts", 0), 2)} for r in rows],
            "gpu_busy_peak_percent": peak_busy, "vram_delta_bytes": peak_vram - base_vram,
            "gpu_use_verified": bool(gpu_evidence),
            "verdict": ("GPU use VERIFIED by utilisation/VRAM growth during the run" if gpu_evidence else
                        "GPU use NOT verified (CPU run, or no utilisation/VRAM evidence was observed)")}
