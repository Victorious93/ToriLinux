"""'Check this PC': turn hardware facts into a readable report with concrete fixes, and save a hardware profile.

Pure evaluation (standard library only) shared by both editions. Each edition collects the facts its own way
(Arch: victorcommon; Debian: victorlib.hw) and passes one dictionary to evaluate(). Nothing is sent anywhere;
the report is shown locally and, when storage exists, written to the owner's drive.
"""
import hashlib
import json
import os
import re
import time

GIB = 2**30
SOFTWARE_VK = ("llvmpipe", "lavapipe", "swiftshader", "softpipe")
GOOD_DRIVERS = {"amd": ("amdgpu",), "nvidia": ("nvidia",), "intel": ("i915", "xe")}
ORDER = {"bad": 0, "warn": 1, "info": 2, "ok": 3}


def now():
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def pc_signature(cpu_name, gpu_names, ram_total):
    """Same formula as the model library: CPU model + GPU models + RAM in GiB. No serials or addresses."""
    gib = int(round((ram_total or 0) / GIB))
    raw = "|".join([str(cpu_name or "?").strip()] + sorted(str(g).strip() for g in gpu_names or []) + [str(gib)])
    return hashlib.sha256(raw.encode()).hexdigest()[:12]


def cpu_flags(cpuinfo_text):
    m = re.search(r"^flags\s*:\s*(.*)$", cpuinfo_text or "", re.M)
    return set(m.group(1).split()) if m else set()


def _c(cid, title, status, detail, fix=None):
    return {"id": cid, "title": title, "status": status, "detail": detail, "fix": fix}


def evaluate(f):
    """f: facts dict (see collect_* in each edition). Returns {"checks": [...], "summary": {...}}."""
    checks = []
    cpu = f.get("cpu") or {}
    flags = set(cpu.get("flags") or [])
    simd = [x for x in ("avx512f", "avx2", "avx", "f16c", "fma") if x in flags]
    if not flags:
        checks.append(_c("cpu", "CPU", "info", f"{cpu.get('name', '?')} · {cpu.get('threads', '?')} threads (instruction sets unknown)"))
    elif "avx2" not in flags:
        checks.append(_c("cpu", "CPU", "warn", f"{cpu.get('name', '?')} · {cpu.get('threads', '?')} threads · no AVX2",
                         "CPU inference will be very slow on this processor. Prefer a GPU backend, or small (1-4B) models with Q4 quantisation."))
    else:
        checks.append(_c("cpu", "CPU", "ok", f"{cpu.get('name', '?')} · {cpu.get('threads', '?')} threads · {' '.join(simd).upper()}"))

    ram = f.get("ram") or {}
    tot, av = ram.get("total") or 0, ram.get("available") or 0
    if tot and tot < 8 * GIB:
        checks.append(_c("ram", "Memory", "warn", f"{tot / GIB:.1f} GiB total, {av / GIB:.1f} GiB available",
                         "Under 8 GiB, stay with 1-4B models at Q4 and keep the context small (2048-4096)."))
    else:
        checks.append(_c("ram", "Memory", "ok" if tot else "info", f"{tot / GIB:.1f} GiB total, {av / GIB:.1f} GiB available" if tot else "Unknown"))

    gpus = f.get("gpus") or []
    if not gpus:
        checks.append(_c("gpu", "Graphics card", "info", "No GPU detected (AMD/Intel via sysfs, NVIDIA via nvidia-smi).",
                         "If this PC has a graphics card, check it is seated and powered, and that the BIOS does not disable it. Inference uses the CPU."))
    for g in gpus:
        vendor, drv = (g.get("vendor") or "").lower(), g.get("driver")
        name = g.get("name") or "GPU"
        vram = f" · {g['vram_total'] / GIB:.1f} GiB VRAM" if g.get("vram_total") else ""
        if vendor == "nvidia" and not drv:
            drv = "nvidia"          # only nvidia-smi enumerates NVIDIA cards, so the proprietary driver is loaded
        if not drv:
            checks.append(_c("driver", f"Driver: {name}", "bad", "No kernel driver is bound to this card.",
                             "Boot without 'nomodeset' (it disables GPU drivers). If it persists, the card may be unsupported by this kernel."))
        elif drv == "nouveau":
            checks.append(_c("driver", f"Driver: {name}", "warn", "Open-source nouveau driver: no CUDA and very limited Vulkan.",
                             "Use the CUDA image of VictorLinux (it ships the NVIDIA driver) for GPU inference on this card."))
        elif drv == "radeon":
            checks.append(_c("driver", f"Driver: {name}", "warn", "Legacy radeon driver: no Vulkan compute.",
                             "Older GCN 1.0/1.1 cards can use amdgpu with the kernel options 'radeon.si_support=0 amdgpu.si_support=1' (or cik_support). Otherwise use the CPU."))
        elif drv in ("vfio-pci", "pci-stub"):
            checks.append(_c("driver", f"Driver: {name}", "warn", f"Card is reserved for passthrough ({drv}).", "Remove the passthrough binding to use it here."))
        elif drv in GOOD_DRIVERS.get(vendor, (drv,)):
            checks.append(_c("driver", f"Driver: {name}", "ok", f"{drv}{vram}"))
        else:
            checks.append(_c("driver", f"Driver: {name}", "info", f"{drv}{vram}"))

    nodes = f.get("render_nodes") or []
    blocked = [n["path"] for n in nodes if not n.get("accessible")]
    if gpus and not nodes:
        checks.append(_c("render", "GPU access (/dev/dri)", "bad", "No /dev/dri render nodes exist.",
                         "The GPU driver did not create device nodes. Boot without 'nomodeset' and check the driver row above."))
    elif blocked:
        checks.append(_c("render", "GPU access (/dev/dri)", "bad", "Not readable/writable by this user: " + ", ".join(blocked),
                         "Add the user to the render and video groups (sudo usermod -aG render,video victor), then log out and in."))
    elif nodes:
        checks.append(_c("render", "GPU access (/dev/dri)", "ok", f"{len(nodes)} node(s) accessible"))

    vk = f.get("vulkan")
    if vk is not None:
        devs = vk.get("devices") or []
        real = [d for d in devs if "CPU" not in str(d.get("deviceType", "")).upper()
                and not any(s in str(d.get("deviceName", "")).lower() for s in SOFTWARE_VK)]
        if real:
            checks.append(_c("vulkan", "Vulkan", "ok", ", ".join(f"{d.get('deviceName')} ({d.get('driverName', '?')})" for d in real)))
        elif devs:
            checks.append(_c("vulkan", "Vulkan", "warn", "Only a software renderer: " + ", ".join(str(d.get("deviceName")) for d in devs),
                             "Software Vulkan is not acceleration. Fix the GPU driver/access rows above; on NVIDIA use the CUDA image."))
        else:
            checks.append(_c("vulkan", "Vulkan", "warn" if gpus else "info", "No Vulkan device: " + str(vk.get("error") or "none enumerated"),
                             "Check the driver and /dev/dri rows above." if gpus else None))

    present = [k for k, v in (f.get("backends") or {}).items() if v]
    sel = f.get("selected") or {}
    be = sel.get("backend")
    if not present:
        checks.append(_c("backend", "Inference backend", "bad", "No llama-server build in this image.", "Use a VictorLinux image that includes llama.cpp."))
    elif be == "cpu" or not be:
        fixes = [c["fix"] for c in checks if c["status"] in ("bad", "warn") and c["id"] in ("driver", "render", "vulkan") and c["fix"]]
        checks.append(_c("backend", "Inference backend", "warn" if gpus else "info",
                         f"CPU fallback: {sel.get('reason', '')} (builds present: {', '.join(present)})",
                         " ".join(fixes) or ("Use the CUDA image for NVIDIA cards." if any((g.get('vendor') or '') == 'nvidia' for g in gpus) and "cuda" not in present else None)))
    else:
        checks.append(_c("backend", "Inference backend", "ok", f"{be.upper() if be != 'vulkan' else 'Vulkan'}: {sel.get('reason', '')}"))

    st = f.get("storage") or {}
    mode = st.get("mode")
    if mode == "persistent":
        free = st.get("free")
        checks.append(_c("storage", "Storage", "warn" if free is not None and free < 10 * GIB else "ok",
                         "Persistent" + (f" · {free / GIB:.1f} GiB free" if free is not None else ""),
                         "Less than 10 GiB free: models will not fit. Free space or choose a models drive in Storage." if free is not None and free < 10 * GIB else None))
    elif mode == "temporary":
        checks.append(_c("storage", "Storage", "warn", "Temporary session: everything is lost at power-off.", "Choose a drive in the Storage tab to keep models, settings and chats."))
    else:
        checks.append(_c("storage", "Storage", "warn", f"No storage selected ({mode or 'unknown'}).", "Open the Storage tab and choose a drive, or start a temporary session."))

    sp = f.get("speed")
    if sp and sp.get("tokens_per_s"):
        tps = sp["tokens_per_s"]
        checks.append(_c("speed", "Speed test", "ok" if tps >= 8 else "warn",
                         f"{tps:.1f} tokens/s generation" + (f", {sp['prompt_per_s']:.0f} tokens/s prompt" if sp.get("prompt_per_s") else "")
                         + f" · {sp.get('backend', '?')} · {os.path.basename(str(sp.get('model', '')))}",
                         None if tps >= 8 else "Under 8 tokens/s feels slow. Try a smaller model or quantisation, fewer context tokens, or a GPU backend."))
    elif sp and sp.get("error"):
        checks.append(_c("speed", "Speed test", "info", sp["error"]))

    worst = min((ORDER[c["status"]] for c in checks), default=3)
    verdict = {0: "bad", 1: "warn", 2: "ok", 3: "ok"}[worst]
    counts = {k: sum(1 for c in checks if c["status"] == k) for k in ORDER}
    return {"checks": sorted(checks, key=lambda c: ORDER[c["status"]]), "summary": {"verdict": verdict, "counts": counts, "backend": be}}


def speed_from_response(body, wall_s, n_tokens_requested):
    """llama-server returns 'timings' (predicted_per_second, prompt_per_second); fall back to wall clock."""
    t = (body or {}).get("timings") or {}
    if t.get("predicted_per_second"):
        return {"tokens_per_s": float(t["predicted_per_second"]), "prompt_per_s": float(t.get("prompt_per_second") or 0) or None,
                "source": "llama-server timings"}
    used = ((body or {}).get("usage") or {}).get("completion_tokens") or n_tokens_requested
    return {"tokens_per_s": used / wall_s if wall_s > 0 else None, "prompt_per_s": None, "source": "wall clock"}


def save_profile(directory, facts, report):
    """Write the latest report for this PC (profiles.json keyed by PC signature) and a dated copy. Returns paths."""
    os.makedirs(directory, exist_ok=True)
    pc = pc_signature((facts.get("cpu") or {}).get("name"), [g.get("name") for g in facts.get("gpus") or []], (facts.get("ram") or {}).get("total"))
    entry = {"pc": pc, "saved": now(), "facts": facts, "report": report}
    dated = os.path.join(directory, f"pc-check-{pc}-{time.strftime('%Y%m%d-%H%M%S')}.json")
    for path, data in ((dated, entry),):
        _write(path, data)
    idx = os.path.join(directory, "profiles.json")
    try:
        with open(idx) as fh:
            prof = json.load(fh)
        if not isinstance(prof, dict):
            prof = {}
    except (OSError, ValueError):
        prof = {}
    prof[pc] = {"saved": entry["saved"], "cpu": (facts.get("cpu") or {}).get("name"), "gpus": [g.get("name") for g in facts.get("gpus") or []],
                "ram_total": (facts.get("ram") or {}).get("total"), "verdict": report["summary"]["verdict"],
                "backend": report["summary"]["backend"], "speed": facts.get("speed"), "report": os.path.basename(dated)}
    _write(idx, prof)
    # keep the 20 newest dated reports
    olds = sorted(x for x in os.listdir(directory) if x.startswith("pc-check-") and x.endswith(".json"))
    for x in olds[:-20]:
        try:
            os.unlink(os.path.join(directory, x))
        except OSError:
            pass
    return {"pc": pc, "report": dated, "profiles": idx}


def _write(path, data):
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)
    os.replace(tmp, path)


def as_text(report):
    mark = {"ok": "OK  ", "info": "INFO", "warn": "WARN", "bad": "FAIL"}
    lines = [f"VictorLinux PC check: {report['summary']['verdict'].upper()}"]
    for c in report["checks"]:
        lines.append(f"[{mark[c['status']]}] {c['title']}: {c['detail']}")
        if c.get("fix"):
            lines.append(f"       fix: {c['fix']}")
    return "\n".join(lines)
