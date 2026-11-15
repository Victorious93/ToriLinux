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

import victorstorage as vs

RUN_DIR = os.environ.get("VICTOR_RUN_DIR", "/run/victor")
PERSIST_STATE = os.path.join(RUN_DIR, "persist.json")
TEMP_DATA = "/run/victor-temp-data"          # explicit temporary-session storage (RAM)
LLAMA_ROOT = os.environ.get("VICTOR_LLAMA_ROOT", "/opt/llama.cpp")
SUBDIRS = ("models", "chats", "config", "images", "outputs", "documents", "logs", "server")

DEFAULT_SETTINGS = {
    "backend": "auto",            # auto | vulkan | hip | cuda | cpu
    "model": "",                  # absolute path to a .gguf
    "ctx": 4096,                  # used only when ctx_auto is false
    "ctx_auto": True,             # size the context from THIS PC's free memory at every start
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
            st = json.load(f)
    except (OSError, ValueError):
        return {"mode": "unknown", "message": "Storage has not been initialised yet.", "data_root": None}
    # A drive that was unplugged leaves an empty directory behind. Writing there would silently land in RAM (or on another
    # disk), so a persistent state whose mount is gone is reported as missing and data_root becomes None until it returns.
    mount = st.get("mount")
    if st.get("mode") == "persistent" and mount and not os.environ.get("VICTOR_DATA"):
        try:
            os.stat(st.get("data_root") or mount)
            alive = os.path.ismount(mount)
        except OSError:
            alive = False
        if not alive:
            return {"mode": "missing", "data_root": None, "was": st,
                    "message": "The persistent storage drive was disconnected. Saving is stopped. Reconnect it (it is found again "
                               "automatically) or choose other storage in the Storage tab; nothing is written to RAM instead."}
    return st


def data_path(*parts):
    root = data_root()
    if not root:
        return None
    return os.path.join(root, *parts)


# ------------------------------------------------------- storage destinations --
_VOL_CACHE = {"t": 0.0, "v": [], "sig": None}


def volume_snapshot(max_age=2.0):
    """Connected volumes, cached briefly so per-request checks stay cheap but still notice a removed drive."""
    now = time.monotonic()
    mounts = vs.mount_table()
    sig = tuple(sorted((k, tuple(m["mountpoint"] + ("ro" if m["ro"] else "rw") for m in v)) for k, v in mounts.items()))
    if now - _VOL_CACHE["t"] > max_age or sig != _VOL_CACHE["sig"]:       # a mount/unmount invalidates immediately
        try:
            boot = set()
            for mp in ("/run/archiso/bootmnt", "/run/archiso/copytoram"):
                r = subprocess.run(["findmnt", "-n", "-o", "SOURCE", "--target", mp], capture_output=True, text=True)
                if r.returncode == 0 and r.stdout.strip().startswith("/dev/"):
                    boot.add(os.path.realpath(r.stdout.strip()))
            _VOL_CACHE["v"] = [v for v in vs.volumes_from_tree(vs.lsblk_tree(), mounts, boot) if not v["boot_medium"]]
        except (vs.StorageError, OSError, ValueError):
            _VOL_CACHE["v"] = []
        _VOL_CACHE["t"], _VOL_CACHE["sig"] = now, sig
    return _VOL_CACHE["v"]


PRIV = os.environ.get("VICTOR_PRIV", "/usr/local/lib/victorlinux/victor-priv")


def priv(args, stdin=None, timeout=120):
    """Run the restricted privileged helper. Returns (returncode, text)."""
    cmd = [PRIV] + list(args) if os.geteuid() == 0 else ["sudo", "-n", PRIV] + list(args)
    try:
        r = subprocess.run(cmd, input=stdin, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as e:
        return 1, str(e)
    return r.returncode, (r.stdout.strip() if r.returncode == 0 else (r.stderr.strip() or r.stdout.strip()))


def storage_use(role, selector, folder="", readonly=False):
    """Choose where `role` files go: any connected volume + folder. Shared by the CLI and the Control Center.
    Mounts the drive and creates ONLY the requested folder. Never formats, relabels or changes other contents."""
    vols = volume_snapshot(0)
    v = vs.select_candidates(vols, selector)
    cfg = load_storage_config()
    vs.set_destination(cfg, role, v, folder, readonly_ok=readonly)
    if not v.get("uuid") or not vs.UUID_RE.match(v["uuid"]):
        raise vs.StorageError("That volume has no filesystem UUID, so it cannot be remembered reliably.")
    if readonly:
        if role not in vs.MULTI_ROLES:
            raise vs.StorageError("Read-only use is for model libraries; other destinations must be writable.")
        rc, out = priv(["volume-mount", v["uuid"], "ro"])
        if rc != 0:
            raise vs.StorageError(out)
        mp = json.loads(out)["mountpoint"]
        if not os.path.isdir(vs.contained_path(mp, folder)):
            raise vs.StorageError(f"Folder '{folder}' does not exist on that drive (read-only use never creates folders).")
    else:
        rc, out = priv(["dest-prepare", v["uuid"], folder])
        if rc != 0:
            raise vs.StorageError(out)
    durable = save_storage_config(cfg)
    _VOL_CACHE["t"] = 0.0
    link_destinations()
    return {"ok": True, "durable": durable, "status": storage_status()["destinations"].get(role, []),
            "message": "Saved." if durable else "Saved for this session only: there is no persistent storage, so this choice is lost on reboot."}


def storage_clear(role, uuid=None, subpath=None):
    cfg = load_storage_config()
    vs.clear_destination(cfg, role, uuid, subpath)
    durable = save_storage_config(cfg)
    link_destinations()
    return {"ok": True, "durable": durable}


def link_destinations(home=None):
    """Make the choices visible to ordinary desktop programs: ~/victor-storage/<role> links and GTK file-chooser bookmarks.
    Dangling links (drive removed) fail writes with an error rather than writing somewhere else. Existing real folders
    and files in the home directory are never replaced."""
    home = home or os.path.expanduser("~")
    base = os.path.join(home, "victor-storage")
    try:
        os.makedirs(base, exist_ok=True)
        rows = vs.destination_status(load_storage_config(), volume_snapshot(0))
        wanted = {}
        for role, lst in rows.items():
            for i, r in enumerate(lst):
                if r["path"]:
                    wanted[role if i == 0 else f"{role}-{i + 1}"] = r["path"]
        for name in os.listdir(base):
            full = os.path.join(base, name)
            if os.path.islink(full) and name not in wanted:
                os.unlink(full)
        for name, target in wanted.items():
            full = os.path.join(base, name)
            if os.path.islink(full):
                if os.readlink(full) == target:
                    continue
                os.unlink(full)
            if not os.path.lexists(full):
                os.symlink(target, full)
        lines = []
        for name, target in sorted(wanted.items()):
            from urllib.parse import quote
            lines.append(f"file://{quote(target)} VictorLinux {name}")
        bm_dir = os.path.join(home, ".config", "gtk-3.0")
        os.makedirs(bm_dir, exist_ok=True)
        keep = []
        bm = os.path.join(bm_dir, "bookmarks")
        if os.path.exists(bm):
            keep = [l for l in open(bm).read().splitlines() if l and " VictorLinux " not in l]
        atomic_write(bm, "\n".join(keep + lines) + "\n", 0o644)
    except OSError:
        pass


def storage_config_path():
    return data_path("config", "storage.json")


def load_storage_config():
    p = storage_config_path()
    return vs.load_config(p) if p else vs.empty_config()


def save_storage_config(cfg):
    p = storage_config_path()
    if not p:
        raise vs.StorageError("No storage state is active, so there is nowhere to save these choices. Choose persistent storage or a temporary session first.")
    atomic_write(p, json.dumps(cfg, indent=2), 0o600)
    return persist_state().get("mode") == "persistent"      # False = saved to RAM only (temporary session)


def storage_status():
    return vs.summarize(load_storage_config(), volume_snapshot(0), persist_state())


def dest_dir(role, need_write=True):
    """Where files for `role` go right now, or None. Never falls back to RAM, the root disk or another drive."""
    rows = vs.destination_status(load_storage_config(), volume_snapshot()).get(role)
    return vs.usable_path(rows, need_write)


def storage_model_dirs():
    rows = vs.destination_status(load_storage_config(), volume_snapshot()).get("models", [])
    return [r["path"] for r in rows if r["status"] in ("active", "readonly") and r["path"]]


def fstype_of_path(path):
    """Filesystem type of the mount that holds `path` (longest matching mount point)."""
    path = os.path.realpath(path)
    best, fs = "", None
    for entries in vs.mount_table().values():
        for m in entries:
            mp = m["mountpoint"]
            if (path == mp or path.startswith(mp.rstrip("/") + "/")) and len(mp) > len(best):
                best, fs = mp, m["fstype"]
    return {"ntfs3": "ntfs", "fuseblk": "ntfs"}.get(fs, fs)


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
            if "ctx" in user and "ctx_auto" not in user:
                s["ctx_auto"] = False       # an owner's explicit older choice is preserved, not overridden
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
            if vendor == "0x8086" and (_read(os.path.join(dev, "class")) or "").startswith("0x03"):
                did = (_read(os.path.join(dev, "device")) or "").replace("0x", "").lower()
                total = _read(os.path.join(dev, "mem_info_vram_total"))   # exposed by some Intel dGPU drivers only
                found.append({
                    "vendor": "intel", "card": name, "pci_id": f"8086:{did}", "name": f"Intel GPU {did}",
                    "driver": os.path.basename(os.path.realpath(os.path.join(dev, "driver"))) if os.path.exists(os.path.join(dev, "driver")) else None,
                    "vram_total": int(total) if total and total.isdigit() else None, "vram_used": None,
                    "shared_memory": None,
                })
                continue
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
    dirs.extend(x for x in storage_model_dirs() if x not in dirs)      # model libraries chosen in Storage, used in place
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
        if expected_sha256:
            raise ValueError("A single checksum cannot verify a multi-shard model. Import without it; every shard's SHA-256 is reported.")
        return import_split_set(src, dest_dir, progress, overwrite)
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
    _check_dest_limits(dest_dir, [hdr["size"]])
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


def _check_dest_limits(dest_dir, sizes):
    """Refuse BEFORE copying when a file cannot fit the destination filesystem (FAT32: 4 GiB minus one byte per file)."""
    fs = fstype_of_path(dest_dir)
    try:
        for n in sizes:
            vs.check_fits(n, fs)
    except vs.StorageError as e:
        raise ValueError(str(e)) from None


def split_members(first):
    """Absolute shard paths of a complete split set, or raise ValueError. Order is part 1..N."""
    m = _SPLIT_RE.search(first)
    if not m:
        raise ValueError("Not a split-model shard name.")
    ok, why = check_split_complete(first)
    if not ok:
        raise ValueError(why)
    count = int(m.group(2))
    base = first[:m.start()]
    return [f"{base}-{i:05d}-of-{count:05d}.gguf" for i in range(1, count + 1)]


def import_split_set(first, dest_dir, progress=None, overwrite=False):
    """Copy EVERY shard of a split GGUF as one all-or-nothing set.

    Shards are copied to temporary names, hashed while copying, header-checked, and only
    renamed into place after all of them succeeded. Any failure removes every partial file.
    Returns (path_of_part_1, 'name=sha256;...').
    """
    members = split_members(first)
    sizes = [os.path.getsize(p) for p in members]
    for p in members:
        read_gguf_header(p)
    os.makedirs(dest_dir, exist_ok=True)
    dests = [os.path.join(dest_dir, os.path.basename(p)) for p in members]
    clash = [d for d in dests if os.path.exists(d)]
    if clash and not overwrite:
        raise ValueError(f"Destination already has shard(s): {', '.join(os.path.basename(c) for c in clash)}")
    total = sum(sizes)
    free = shutil.disk_usage(dest_dir).free
    if total + (64 << 20) > free:
        raise ValueError(f"Not enough free space: need {total} bytes for {len(members)} shards, have {free}.")
    _check_dest_limits(dest_dir, sizes)
    tmps = [d + ".part" for d in dests]
    digests, done = [], 0
    try:
        for src, tmp, size in zip(members, tmps, sizes):
            h = hashlib.sha256()
            with open(src, "rb") as fi, open(tmp, "wb") as fo:
                while True:
                    b = fi.read(8 << 20)
                    if not b:
                        break
                    fo.write(b)
                    h.update(b)
                    done += len(b)
                    if progress:
                        progress(done, total)
                fo.flush()
                os.fsync(fo.fileno())
            if os.path.getsize(tmp) != size:
                raise ValueError(f"Short copy of {os.path.basename(src)}: the source changed or the drive failed.")
            if os.path.getsize(src) != size:
                raise ValueError(f"{os.path.basename(src)} changed while copying; nothing was imported.")
            read_gguf_header(tmp)
            digests.append(h.hexdigest())
        for tmp, d in zip(tmps, dests):
            os.replace(tmp, d)
    finally:
        for tmp in tmps:
            if os.path.exists(tmp):
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
    return dests[0], ";".join(f"{os.path.basename(d)}={g}" for d, g in zip(dests, digests))


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
