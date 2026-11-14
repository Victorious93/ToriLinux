"""Shared helpers for the VictorLinux control center, gateway and CLI tools.

Part of VictorLinux (fork maintained by Victorious / Victorious Framework).
Original ToriLinux by sasha0552, MIT licensed. Standard library only, so it
works offline with no extra Python packages.
"""
import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import time

RUN_DIR = os.environ.get("VICTOR_RUN_DIR", "/run/victor")
PERSIST_STATE = os.path.join(RUN_DIR, "persist.json")
TEMP_DATA = "/run/victor-temp-data"          # explicit temporary-session storage (RAM)
LLAMA_ROOT = os.environ.get("VICTOR_LLAMA_ROOT", "/opt/llama.cpp")
SUBDIRS = ("models", "chats", "config", "images", "outputs", "documents", "logs", "server")

DEFAULT_SETTINGS = {
    "backend": "auto",            # auto | vulkan | hip | cuda | cpu
    "model": "",                  # absolute path to a .gguf
    "ctx": 4096,
    "gpu_layers": -1,             # -1 = decide automatically from VRAM estimate
    "threads": 0,                 # 0 = llama.cpp default
    "memory_limit_mb": 0,         # 0 = no cap (systemd-run/ulimit style soft cap enforced by estimate check)
    "idle_unload_s": 0,           # 0 = never
    "extra_model_dirs": [],
    "remote": {"enabled": False, "bind": "127.0.0.1", "port": 8080, "tls": False},
}


# ---------------------------------------------------------------- storage --
def data_root():
    """Directory holding persistent (or explicitly temporary) user data, or None."""
    env = os.environ.get("VICTOR_DATA")
    if env:
        return env
    st = persist_state()
    return st.get("data_root") or None


def persist_state():
    try:
        with open(PERSIST_STATE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"mode": "unknown", "message": "Storage has not been initialised yet.", "data_root": None}


def data_path(*parts):
    root = data_root()
    if not root:
        return None
    return os.path.join(root, *parts)


def settings_path():
    p = data_path("config", "settings.json")
    return p


def load_settings():
    s = json.loads(json.dumps(DEFAULT_SETTINGS))
    p = settings_path()
    if p and os.path.exists(p):
        try:
            with open(p) as f:
                user = json.load(f)
            for k, v in user.items():
                if k == "remote" and isinstance(v, dict):
                    s["remote"].update(v)
                else:
                    s[k] = v
        except (OSError, ValueError):
            pass
    # Reuse old Arch data without moving it. Translate only known legacy paths.
    root = data_root()
    if root:
        old = "/mnt/toridata/tori"
        def relocate(path):
            return root + path[len(old):] if isinstance(path, str) and (path == old or path.startswith(old + "/")) else path
        s["model"] = relocate(s.get("model", ""))
        s["extra_model_dirs"] = [relocate(d) for d in s.get("extra_model_dirs", [])]
    return s


def atomic_write(path, data, mode=0o600):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp.{os.getpid()}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def save_settings(s):
    p = settings_path()
    if not p:
        raise RuntimeError("No storage selected: choose persistent storage or temporary-session mode first.")
    atomic_write(p, json.dumps(s, indent=2), 0o600)


def secrets_path():
    return data_path("config", "secrets.json")


def load_secrets():
    p = secrets_path()
    if p and os.path.exists(p):
        try:
            with open(p) as f:
                return json.load(f)
        except (OSError, ValueError):
            pass
    return {}


def save_secrets(d):
    p = secrets_path()
    if not p:
        raise RuntimeError("No storage selected.")
    atomic_write(p, json.dumps(d, indent=2), 0o600)


# ---------------------------------------------------------------- hardware --
def read_meminfo():
    out = {}
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                k, v = line.split(":", 1)
                out[k] = int(v.split()[0]) * 1024
    except OSError:
        pass
    return out


def ram_info():
    m = read_meminfo()
    return {"total": m.get("MemTotal", 0), "available": m.get("MemAvailable", 0)}


def _read(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None


# PCI ids are informational only; a match does NOT prove inference works.
KNOWN_AMD = {"73bf": "Radeon RX 6800 / 6800 XT / 6900 XT (Navi 21)"}


def gpus():
    """Enumerate GPUs from sysfs (AMD) and nvidia-smi (NVIDIA)."""
    found = []
    base = "/sys/class/drm"
    if os.path.isdir(base):
        for name in sorted(os.listdir(base)):
            if not re.fullmatch(r"card\d+", name):
                continue
            dev = os.path.join(base, name, "device")
            vendor = _read(os.path.join(dev, "vendor"))
            if vendor != "0x1002":
                continue
            did = (_read(os.path.join(dev, "device")) or "").replace("0x", "").lower()
            total = _read(os.path.join(dev, "mem_info_vram_total"))
            used = _read(os.path.join(dev, "mem_info_vram_used"))
            found.append({
                "vendor": "amd", "card": name, "pci_id": f"1002:{did}",
                "name": KNOWN_AMD.get(did, f"AMD GPU {did}"),
                "driver": os.path.basename(os.path.realpath(os.path.join(dev, "driver"))) if os.path.exists(os.path.join(dev, "driver")) else None,
                "vram_total": int(total) if total and total.isdigit() else None,
                "vram_used": int(used) if used and used.isdigit() else None,
            })
    if shutil.which("nvidia-smi"):
        try:
            r = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.total,memory.used", "--format=csv,noheader,nounits"],
                               capture_output=True, text=True, timeout=10)
            if r.returncode == 0:
                for i, line in enumerate(r.stdout.strip().splitlines()):
                    n, t, u = [x.strip() for x in line.split(",")]
                    found.append({"vendor": "nvidia", "card": f"nvidia{i}", "name": n,
                                  "vram_total": int(t) * 1048576, "vram_used": int(u) * 1048576})
        except (OSError, subprocess.SubprocessError, ValueError):
            pass
    return found


def render_nodes():
    """Report /dev/dri access so permission problems are actionable."""
    res = []
    d = "/dev/dri"
    if os.path.isdir(d):
        for n in sorted(os.listdir(d)):
            if not re.fullmatch(r"(card|renderD)\d+", n):
                continue          # skip the by-path/ directory etc.
            p = os.path.join(d, n)
            res.append({"path": p, "accessible": os.access(p, os.R_OK | os.W_OK)})
    return res


def vulkan_devices():
    if not shutil.which("vulkaninfo"):
        return {"available": False, "devices": [], "error": "vulkaninfo not installed"}
    try:
        r = subprocess.run(["vulkaninfo", "--summary"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as e:
        return {"available": False, "devices": [], "error": str(e)}
    devs, cur = [], None
    for line in r.stdout.splitlines():
        line = line.strip()
        m = re.match(r"GPU\d+:", line)
        if m:
            cur = {}
            devs.append(cur)
        elif cur is not None and "=" in line:
            k, v = [x.strip() for x in line.split("=", 1)]
            if k in ("deviceName", "driverName", "deviceType", "apiVersion", "driverInfo"):
                cur[k] = v
    return {"available": r.returncode == 0 and bool(devs), "devices": devs,
            "error": None if r.returncode == 0 else (r.stderr.strip()[-300:] or "vulkaninfo failed")}


def backends():
    """Which llama-server builds are physically present in the image."""
    out = {}
    for b in ("vulkan", "hip", "cuda", "cpu"):
        p = os.path.join(LLAMA_ROOT, b, "bin", "llama-server")
        out[b] = p if os.path.isfile(p) and os.access(p, os.X_OK) else None
    return out


def choose_backend(requested, gpu_list=None, vk=None):
    """Return (backend, reason). Never claims acceleration that is not present."""
    avail = backends()
    gpu_list = gpus() if gpu_list is None else gpu_list
    if requested and requested != "auto":
        if avail.get(requested):
            return requested, f"Backend '{requested}' selected manually."
        return None, f"Backend '{requested}' is not included in this image (available: {', '.join(k for k, v in avail.items() if v) or 'none'})."
    has_nv = any(g["vendor"] == "nvidia" for g in gpu_list)
    if has_nv and avail["cuda"]:
        return "cuda", "NVIDIA GPU detected."
    if avail["vulkan"]:
        vk = vulkan_devices() if vk is None else vk
        real = [d for d in vk.get("devices", []) if "CPU" not in d.get("deviceType", "").upper() and
                not any(x in d.get("deviceName", "").lower() for x in ("llvmpipe", "lavapipe", "swiftshader", "softpipe"))]
        if vk["available"] and real:
            return "vulkan", "Hardware Vulkan device detected: " + ", ".join(d.get("deviceName", "GPU") for d in real)
        return ("cpu" if avail["cpu"] else None,
                "No usable hardware Vulkan device (" + (vk.get("error") or "none enumerated; software rendering is not acceleration") + "); falling back to CPU. Run diagnostics.")
    if avail["cpu"]:
        return "cpu", "No supported GPU detected; running on CPU (slow for large models)."
    return None, "No llama-server build found in this image."


# ------------------------------------------------------------------- GGUF --
_GGUF_TYPES = {0: ("<B", 1), 1: ("<b", 1), 2: ("<H", 2), 3: ("<h", 2), 4: ("<I", 4), 5: ("<i", 4),
               6: ("<f", 4), 7: ("<?", 1), 10: ("<Q", 8), 11: ("<q", 8), 12: ("<d", 8)}
_WANTED_SUFFIXES = (".block_count", ".attention.head_count", ".attention.head_count_kv",
                    ".embedding_length", ".context_length", ".attention.key_length",
                    ".attention.value_length", ".expert_count")


class GGUFError(ValueError):
    pass


def _rd(f, fmt, n):
    b = f.read(n)
    if len(b) != n:
        raise GGUFError("truncated GGUF header")
    return struct.unpack(fmt, b)[0]


def _rd_str(f):
    n = _rd(f, "<Q", 8)
    if n > 1 << 24:
        raise GGUFError("implausible string length in GGUF header")
    b = f.read(n)
    if len(b) != n:
        raise GGUFError("truncated GGUF header")
    return b.decode("utf-8", "replace")


def _skip_value(f, t):
    if t in _GGUF_TYPES:
        f.seek(_GGUF_TYPES[t][1], 1)
    elif t == 8:
        _rd_str(f)
    elif t == 9:
        et = _rd(f, "<I", 4)
        n = _rd(f, "<Q", 8)
        if et in _GGUF_TYPES:
            f.seek(_GGUF_TYPES[et][1] * n, 1)
        else:
            for _ in range(n):
                _skip_value(f, et)
    else:
        raise GGUFError(f"unknown GGUF value type {t}")


def read_gguf_header(path):
    """Parse the GGUF metadata header. Raises GGUFError on structural problems.

    This validates structure only - it cannot detect corrupted tensor data.
    Use a checksum for integrity.
    """
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        if f.read(4) != b"GGUF":
            raise GGUFError("not a GGUF file (bad magic)")
        ver = _rd(f, "<I", 4)
        if ver not in (2, 3):
            raise GGUFError(f"unsupported GGUF version {ver}")
        n_tensors = _rd(f, "<Q", 8)
        n_kv = _rd(f, "<Q", 8)
        if n_tensors > 1_000_000 or n_kv > 1_000_000:
            raise GGUFError("implausible tensor/metadata count")
        meta = {}
        for _ in range(n_kv):
            key = _rd_str(f)
            t = _rd(f, "<I", 4)
            keep = key == "general.architecture" or key == "general.name" or key == "general.file_type" \
                or key.endswith(_WANTED_SUFFIXES)
            if keep and t in _GGUF_TYPES:
                fmt, n = _GGUF_TYPES[t]
                meta[key] = _rd(f, fmt, n)
            elif keep and t == 8:
                meta[key] = _rd_str(f)
            else:
                _skip_value(f, t)
        pos = f.tell()
        if pos > size:
            raise GGUFError("metadata runs past end of file")
    return {"version": ver, "tensors": n_tensors, "kv": n_kv, "meta": meta, "size": size}


def _arch_key(meta, suffix):
    arch = meta.get("general.architecture", "")
    return meta.get(f"{arch}{suffix}")


def estimate_memory(path, ctx, kv_bytes_per_elem=2):
    """Estimate weights + KV cache + overhead. Heuristic: real use varies by backend."""
    hdr = read_gguf_header(path)
    m = hdr["meta"]
    weights = hdr["size"]
    if _SPLIT_RE.search(path):
        ok, why = check_split_complete(path)
        if not ok:
            raise GGUFError(why)
        match = _SPLIT_RE.search(path)
        base = path[:match.start()]
        count = int(match.group(2))
        weights = sum(os.path.getsize(f"{base}-{i:05d}-of-{count:05d}.gguf") for i in range(1, count + 1))
    n_layer = _arch_key(m, ".block_count")
    n_head = _arch_key(m, ".attention.head_count")
    n_kv = _arch_key(m, ".attention.head_count_kv") or n_head
    emb = _arch_key(m, ".embedding_length")
    klen = _arch_key(m, ".attention.key_length")
    vlen = _arch_key(m, ".attention.value_length") or klen
    kv = None
    if n_layer and n_kv and (klen or (emb and n_head)):
        kd = klen or emb // n_head
        vd = vlen or kd
        # n_kv may be a list on some archs; only scalars are handled here
        if isinstance(n_kv, int):
            kv = int(n_layer * ctx * n_kv * (kd + vd) * kv_bytes_per_elem)
    overhead = int(max(300 * 1048576, weights * 0.05))
    total = weights + (kv or 0) + overhead
    return {"weights": weights, "kv_cache": kv, "overhead": overhead, "total": total,
            "kv_known": kv is not None, "arch": m.get("general.architecture"),
            "name": m.get("general.name"), "n_layer": n_layer,
            "ctx_train": _arch_key(m, ".context_length")}


def fit_report(est, ram, vram_free):
    """Human-readable fit verdict for pre-load display."""
    t = est["total"]
    notes, level = [], "ok"
    if vram_free and t <= vram_free * 0.92:
        notes.append("Fits in free VRAM: full GPU offload is plausible.")
    elif vram_free and est["weights"] > vram_free:
        notes.append("Model weights exceed free VRAM: only partial offload is possible; the rest runs from system RAM (slower).")
        level = "warn"
    elif vram_free:
        notes.append("Weights fit but weights + KV cache are tight in VRAM: reduce context length or offload fewer layers.")
        level = "warn"
    else:
        notes.append("No GPU memory information: assuming CPU/RAM only.")
    if t > ram.get("available", 0):
        if not (vram_free and t <= vram_free):
            notes.append("Estimated need exceeds available RAM: loading may fail or swap/OOM.")
            level = "bad"
    if not est["kv_known"]:
        notes.append("KV-cache size could not be derived from this model's metadata; figure excludes it.")
    return {"level": level, "notes": notes}


def sha256_file(path, progress=None, chunk=4 << 20):
    h = hashlib.sha256()
    done, size = 0, os.path.getsize(path)
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
            done += len(b)
            if progress:
                progress(done, size)
    return h.hexdigest()


def list_models(extra_dirs=()):
    dirs = []
    d = data_path("models")
    if d:
        dirs.append(d)
    dirs.extend(extra_dirs)
    seen, out = set(), []
    for base in dirs:
        if not os.path.isdir(base):
            continue
        for root, _dn, files in os.walk(base):
            for fn in sorted(files):
                if not fn.lower().endswith(".gguf"):
                    continue
                p = os.path.join(root, fn)
                split = _SPLIT_RE.search(fn)
                if split and int(split.group(1)) != 1:
                    continue
                if p in seen:
                    continue
                seen.add(p)
                try:
                    out.append({"path": p, "name": fn, "size": os.path.getsize(p), "dir": base})
                except OSError:
                    pass
    return out


_SPLIT_RE = re.compile(r"-(\d{5})-of-(\d{5})\.gguf$", re.I)


def check_split_complete(path):
    m = _SPLIT_RE.search(path)
    if not m:
        return True, None
    total = int(m.group(2))
    if total < 1 or total > 1024 or not 1 <= int(m.group(1)) <= total:
        return False, "Invalid split-model part/count (maximum 1024 parts)."
    base = path[:m.start()]
    missing = [i for i in range(1, total + 1)
               if not os.path.exists(f"{base}-{i:05d}-of-{total:05d}.gguf")]
    if missing:
        return False, f"Split model is missing parts: {missing}"
    return True, None


def import_model(src, dest_dir, expected_sha256=None, progress=None, overwrite=False):
    """Validate a GGUF, check free space, copy atomically, verify copy. Returns dest path."""
    if not os.path.isfile(src):
        raise ValueError(f"Source file not found: {src}")
    if not src.lower().endswith(".gguf"):
        raise ValueError("Only .gguf files are supported by the bundled llama.cpp runtime.")
    if _SPLIT_RE.search(src):
        raise ValueError("Split-model import requires the complete shard directory. Keep all shards together on a mounted drive "
                         "and add that directory under Models; select part 00001. Importing only one shard would produce a broken model.")
    hdr = read_gguf_header(src)          # raises GGUFError
    ok, why = check_split_complete(src)
    if not ok:
        raise ValueError(why)
    if expected_sha256:
        expected_sha256 = expected_sha256.strip().lower()
        if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
            raise ValueError("Checksum must be 64 hex characters (SHA-256).")
    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.join(dest_dir, os.path.basename(src))
    if os.path.exists(dest) and not overwrite:
        raise ValueError(f"A model named {os.path.basename(src)} already exists in the destination.")
    free = shutil.disk_usage(dest_dir).free
    if hdr["size"] + (64 << 20) > free:
        raise ValueError(f"Not enough free space: need {hdr['size']} bytes, have {free}.")
    tmp = dest + ".part"
    h = hashlib.sha256()
    done = 0
    try:
        with open(src, "rb") as fi, open(tmp, "wb") as fo:
            while True:
                b = fi.read(8 << 20)
                if not b:
                    break
                fo.write(b)
                h.update(b)
                done += len(b)
                if progress:
                    progress(done, hdr["size"])
            fo.flush()
            os.fsync(fo.fileno())
        got = h.hexdigest()
        if expected_sha256 and got != expected_sha256:
            raise ValueError(f"Checksum mismatch: expected {expected_sha256}, got {got}. Nothing was imported.")
        read_gguf_header(tmp)
        os.replace(tmp, dest)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return dest, got


# ----------------------------------------------------------------- memory --
def human(n):
    if n is None:
        return "?"
    for u in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(n) < 1024 or u == "TiB":
            return f"{n:.1f} {u}" if u != "B" else f"{n} B"
        n /= 1024.0


def now():
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")
