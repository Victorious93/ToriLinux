"""Per-host runtime planning for VictorLinux (standard library only).

Everything here is computed from the machine that is *booted right now*. Nothing
measured is stored in the owner's settings, so a profile saved on a strong PC can
never carry an oversized context or GPU-layer count onto a smaller PC. The only
things persisted are owner preferences (settings.json) and the record of models
that really loaded and generated text (verified.json, tagged with the device).
"""
import json
import os

import victorcommon as tc

GIB = 2**30
GPU_FRACTION = 0.88          # share of free device memory we plan to use
RAM_FRACTION = 0.75          # share of available system RAM we plan to use
SHARED_MEM_MAX_DEDICATED = 1 * GIB   # [Likely] a "VRAM" carve-out this small means shared/system memory
CTX_STEPS = (131072, 65536, 32768, 24576, 16384, 12288, 8192, 6144, 4096, 3072, 2048)
CTX_AUTO_CAP = 32768         # automatic mode never goes above this; owners may set more manually
CTX_MIN = 2048

# Architectures llama.cpp has long supported. This is only a hint that the loader
# *knows the name*; the bundled build may still be older than a given model.
KNOWN_ARCHS = frozenset((
    "llama", "qwen2", "qwen2moe", "qwen3", "qwen3moe", "gemma", "gemma2", "gemma3", "phi2", "phi3",
    "mistral3", "deepseek2", "falcon", "gpt2", "gptneox", "starcoder2", "olmo", "olmo2", "granite",
    "command-r", "mamba", "internlm2", "minicpm", "stablelm", "baichuan", "bert",
))


# ------------------------------------------------------------- CPU --
def physical_threads():
    """Physical core count where sysfs reveals SMT siblings; otherwise logical CPUs."""
    try:
        logical = len(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        logical = os.cpu_count() or 1
    try:
        with open("/sys/devices/system/cpu/cpu0/topology/thread_siblings_list") as f:
            per_core = _count_cpu_list(f.read().strip())
        if per_core > 1:
            return max(1, logical // per_core)
    except (OSError, ValueError):
        pass
    return max(1, logical)


def _count_cpu_list(text):
    """'0,8' -> 2, '0-1' -> 2, '0' -> 1."""
    n = 0
    for part in text.split(","):
        if "-" in part:
            lo, hi = part.split("-", 1)
            n += int(hi) - int(lo) + 1
        elif part:
            n += 1
    return n


def default_threads():
    """Safe default: leave one core for the OS/UI on machines with more than 4."""
    n = physical_threads()
    return n if n <= 4 else n - 1


# ----------------------------------------------------------- device --
def _free(g):
    if g.get("vram_total") is None:
        return None
    return max(0, g["vram_total"] - (g.get("vram_used") or 0))


def _dedicated(g):
    return g.get("vram_total") is not None and g["vram_total"] >= SHARED_MEM_MAX_DEDICATED and not g.get("shared_memory")


def select_device(backend, gpu_list, ram, vk=None):
    """Pick the memory pool the model will really live in.

    Never adds VRAM to RAM, never pools different GPUs, never calls shared memory VRAM.
    Returns a dict: kind (dedicated|shared|system|unknown), budget (bytes), device, others, notes.
    """
    ram_avail = ram.get("available", 0) or 0
    ram_budget = int(ram_avail * RAM_FRACTION)
    if backend in (None, "cpu"):
        return {"kind": "system", "device": None, "free": ram_avail, "budget": ram_budget, "others": [],
                "ambiguous": False, "notes": ["CPU inference uses available system RAM."]}
    vendor_for = {"cuda": "nvidia", "hip": "amd"}.get(backend)
    cands = [g for g in gpu_list if vendor_for is None or g.get("vendor") == vendor_for]
    notes = []
    if vk is not None and backend == "vulkan":
        # Software renderers are not devices we may count; they never show up in gpu_list (sysfs/nvidia-smi) anyway.
        real = [d for d in vk.get("devices", []) if not any(
            x in (d.get("deviceName", "") or "").lower() for x in ("llvmpipe", "lavapipe", "swiftshader", "softpipe"))
            and "CPU" not in (d.get("deviceType", "") or "").upper()]
        if not real:
            return {"kind": "system", "device": None, "free": ram_avail, "budget": ram_budget, "others": [],
                    "ambiguous": False, "notes": ["No hardware Vulkan device; using system RAM."]}
    dedicated = [g for g in cands if _dedicated(g)]
    if dedicated:
        dedicated.sort(key=lambda g: _free(g) or 0, reverse=True)
        ambiguous = False
        if len(dedicated) > 1:
            if backend == "vulkan":
                # Vulkan enumeration order is not guaranteed to match sysfs order, and llama.cpp may split layers
                # across every visible device. Plan against the smallest so we cannot overcommit any one card.
                pick = dedicated[-1]
                ambiguous = True
                notes.append("Several GPUs: memory is not pooled. Planning against the smallest card because the "
                             "Vulkan device order cannot be matched to a card automatically; set GGML_VK_VISIBLE_DEVICES to pin one.")
            else:
                pick = dedicated[0]
                notes.append("Several GPUs: using the one with the most free memory; others are not counted.")
        else:
            pick = dedicated[0]
        free = _free(pick)
        return {"kind": "dedicated", "device": pick, "free": free, "budget": int(free * GPU_FRACTION),
                "others": [g for g in dedicated if g is not pick], "ambiguous": ambiguous, "notes": notes,
                "index": _device_index(pick)}
    if cands:
        g = cands[0]
        if g.get("vram_total") is None:
            notes.append("GPU memory size unknown: planning against system RAM, not assuming dedicated VRAM.")
            kind = "unknown"
        else:
            notes.append("Integrated/shared graphics memory is system RAM, not extra VRAM.")
            kind = "shared"
        return {"kind": kind, "device": g, "free": ram_avail, "budget": ram_budget, "others": cands[1:],
                "ambiguous": False, "notes": notes}
    return {"kind": "system", "device": None, "free": ram_avail, "budget": ram_budget, "others": [], "ambiguous": False,
            "notes": ["No GPU matching this backend was enumerated; using system RAM."]}


def _device_index(g):
    card = g.get("card", "")
    if card.startswith("nvidia") and card[6:].isdigit():
        return int(card[6:])
    return None


# ---------------------------------------------------------- context --
def auto_context(weights, kv_for, overhead, budget, ctx_train=None, floor=CTX_MIN):
    """Largest standard context whose weights + KV + overhead fit in `budget`.

    kv_for(ctx) -> bytes or None (None = unknown; then we stay at a conservative 4096).
    Returns (ctx, fits, note).
    """
    cap = min(CTX_AUTO_CAP, ctx_train) if ctx_train else CTX_AUTO_CAP
    probe = kv_for(4096)
    if probe is None:
        return min(4096, cap), weights + overhead <= budget, "KV-cache size unknown for this model; using a conservative 4096 context."
    for c in CTX_STEPS:
        if c > cap:
            continue
        if c < floor:
            break
        if weights + (kv_for(c) or 0) + overhead <= budget:
            return c, True, f"Automatic context {c} for this PC's free memory."
    return floor, weights + (kv_for(floor) or 0) + overhead <= budget, \
        "Even the minimum context is tight for this PC's free memory; expect partial offload or failure."


def plan_layers(est, device, ctx):
    """GPU layer count for the selected pool. 0 for CPU/shared/unknown; 999 = everything."""
    if device["kind"] == "system":
        return 0
    if device["kind"] != "dedicated":      # shared or unknown size: the runtime fails loudly at load if it cannot fit
        return 999
    free = device["free"]
    total = est["total"]
    if total <= free * 0.92:
        return 999
    if est.get("n_layer"):
        avail = max(0, free * GPU_FRACTION - (est["kv_cache"] or 0) - est["overhead"])
        return max(0, int(est["n_layer"] * avail / max(1, est["weights"])))
    return 0


# ------------------------------------------------------ verified runs --
def _verified_path():
    return tc.data_path("config", "verified.json")


def load_verified():
    p = _verified_path()
    try:
        with open(p) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def record_verified(model, backend, device_name, ngl, ctx):
    """Called only after a real completion succeeded on THIS host."""
    p = _verified_path()
    if not p:
        return False
    d = load_verified()
    try:
        st = os.stat(model)
    except OSError:
        return False
    d[model] = {"size": st.st_size, "mtime": int(st.st_mtime), "backend": backend, "device": device_name,
                "ngl": ngl, "ctx": ctx, "ts": tc.now()}
    tc.atomic_write(p, json.dumps(d, indent=2), 0o600)
    return True


def verified_state(model, backend, device_name, table=None):
    """'verified-here' | 'verified-elsewhere' | 'untested' (file changed or never run)."""
    table = load_verified() if table is None else table
    rec = table.get(model)
    if not rec:
        return "untested"
    try:
        st = os.stat(model)
    except OSError:
        return "untested"
    if rec.get("size") != st.st_size or rec.get("mtime") != int(st.st_mtime):
        return "untested"
    return "verified-here" if rec.get("backend") == backend and rec.get("device") == device_name else "verified-elsewhere"


# ------------------------------------------------------- local ranking --
def rank_local(models, ram, device, backend, ctx_pref=None):
    """Rank GGUF files already on disk for THIS host. Estimates are not load tests."""
    dev_name = (device.get("device") or {}).get("name") if device else None
    table = load_verified()
    out = []
    for m in models[:200]:
        row = {"path": m["path"], "name": m["name"], "size": m["size"]}
        try:
            est = tc.estimate_memory(m["path"], 4096)
        except (tc.GGUFError, OSError) as e:
            row.update(fit="unusable", reason=f"Not a readable GGUF: {e}", verified="untested", score=-1)
            out.append(row)
            continue
        pool = device["budget"]
        kv = lambda c, p=m["path"]: (tc.estimate_memory(p, c)["kv_cache"])  # noqa: E731
        ctx, fits, cnote = auto_context(est["weights"], kv, est["overhead"], pool, est.get("ctx_train"))
        if not fits and device["kind"] == "dedicated":
            # Partial offload: the rest lives in system RAM.
            ram_budget = int(ram.get("available", 0) * RAM_FRACTION)
            ctx, fits, cnote = auto_context(est["weights"], kv, est["overhead"], ram_budget + pool, est.get("ctx_train"))
            level = "partial" if fits else "too large"
        else:
            level = "fits" if fits else "too large"
        arch = est.get("arch")
        known = arch in KNOWN_ARCHS
        state = verified_state(m["path"], backend, dev_name, table)
        notes = [cnote]
        if not known:
            notes.append(f"Architecture '{arch}' is not in this build's known list; it may not load. Test it before relying on it.")
        if state == "verified-elsewhere":
            notes.append("Loaded OK on a different backend/device; re-test on this PC.")
        row.update(arch=arch, ctx=ctx, estimated_total=est["weights"] + (kv(ctx) or 0) + est["overhead"],
                   fit=level, arch_known=known, verified=state, reason=" ".join(notes))
        row["score"] = (3 if state == "verified-here" else 0) + (2 if level == "fits" else 1 if level == "partial" else -2) \
            + (1 if known else 0)
        out.append(row)
    out.sort(key=lambda r: (-r["score"], -r["size"]))
    return out


def host_profile(settings, gpu_list=None, ram=None, vk=None):
    """Snapshot of this boot's facts (for display/diagnostics only; never saved into settings)."""
    gl = tc.gpus() if gpu_list is None else gpu_list
    ram = tc.ram_info() if ram is None else ram
    backend, reason = tc.choose_backend(settings.get("backend", "auto"), gl)
    vkinfo = vk if vk is not None else (tc.vulkan_devices() if backend == "vulkan" else None)
    dev = select_device(backend, gl, ram, vkinfo)
    return {"measured_at": tc.now(), "backend": backend, "backend_reason": reason, "ram": ram,
            "device": dev, "threads_default": default_threads(), "physical_threads": physical_threads()}


def write_host_profile(profile):
    """Volatile copy in /run (tmpfs): gone at shutdown, so it cannot go stale on another PC."""
    try:
        os.makedirs(tc.RUN_DIR, exist_ok=True)
        tc.atomic_write(os.path.join(tc.RUN_DIR, "host.json"), json.dumps(profile, indent=2, default=str), 0o644)
    except OSError:
        pass
