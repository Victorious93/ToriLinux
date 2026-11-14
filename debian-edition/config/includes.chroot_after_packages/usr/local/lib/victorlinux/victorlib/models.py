"""Model import/validation. GGUF only (what the bundled llama.cpp loads)."""
import hashlib, os, shutil, struct
from .common import *

GGUF_MAGIC = b"GGUF"
KNOWN_FOREIGN = {
    b"PK\x03\x04": "a ZIP/safetensors-bundle or PyTorch checkpoint",
    b"\x80\x02": "a pickled PyTorch checkpoint",
}


def inspect_gguf(path):
    """Validate header. Returns dict; raises VictorError with an actionable message."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            head = f.read(24)
    except OSError as e:
        raise VictorError(f"Cannot read {path}: {e.strerror}")
    if head[:4] != GGUF_MAGIC:
        if path.lower().endswith(".safetensors") or head[:8].isdigit() or head[8:9] == b"{":
            raise VictorError("This is a safetensors file. Only GGUF models can be loaded; convert it with "
                              "llama.cpp's convert_hf_to_gguf.py on a machine with the source model.")
        for sig, what in KNOWN_FOREIGN.items():
            if head.startswith(sig):
                raise VictorError(f"This looks like {what}, not GGUF. Convert it to GGUF first.")
        raise VictorError("Not a GGUF file (bad magic bytes). Only .gguf models are supported.")
    if len(head) < 24:
        raise VictorError("GGUF header is truncated; the file is incomplete or damaged.")
    version, n_tensors, n_kv = struct.unpack("<IQQ", head[4:24])
    if version not in (2, 3):
        raise VictorError(f"Unsupported GGUF version {version} (supported: 2, 3).")
    if n_tensors == 0 or n_tensors > 10_000_000:
        raise VictorError("GGUF header reports an implausible tensor count; file is damaged.")
    return {"size": size, "gguf_version": version, "tensors": n_tensors, "metadata_keys": n_kv}


def sha256_file(path, progress=None):
    h = hashlib.sha256()
    done = 0
    with open(path, "rb") as f:
        while chunk := f.read(8 * 1024 * 1024):
            h.update(chunk)
            done += len(chunk)
            if progress:
                progress(done)
    return h.hexdigest()


def estimate(size_bytes, ctx=4096):
    """Rough requirement: weights + KV/compute overhead. Documented as an ESTIMATE, not a guarantee."""
    overhead = int(size_bytes * 0.10) + ctx * 512 * 1024 // 1  # ~0.5 MiB/token upper-bound-ish for mid-size models
    return {"weights": size_bytes, "estimated_total": size_bytes + overhead,
            "note": "estimate; real KV-cache size depends on architecture, context and cache type"}


def list_models():
    try:
        d = data_path("models")
    except VictorError:
        return []
    out = []
    for root, _, files in os.walk(d):
        for fn in sorted(files):
            if fn.lower().endswith(".gguf"):
                p = os.path.join(root, fn)
                out.append({"name": os.path.relpath(p, d), "size": os.path.getsize(p)})
    return sorted(out, key=lambda m: m["name"])


def import_model(src, sha256=None, move=False, ctx=4096, progress=None):
    if not os.path.isfile(src):
        raise VictorError(f"{src} is not a file. Select a .gguf file (browse USB/external disks under /media).")
    info = inspect_gguf(src)
    dst_dir = data_path("models")
    st = os.statvfs(dst_dir)
    free = st.f_bavail * st.f_frsize
    if info["size"] + MIN_FREE_BYTES > free:
        raise VictorError(f"Not enough space: model needs {human(info['size'])}, only {human(free)} free "
                          f"(keeping {human(MIN_FREE_BYTES)} reserve).")
    if sha256:
        got = sha256_file(src, progress)
        if got.lower() != sha256.lower().strip():
            raise VictorError(f"Checksum mismatch: expected {sha256}, got {got}. The file is corrupt or not the "
                              "file you meant; it was NOT imported.")
    dst = os.path.join(dst_dir, os.path.basename(src))
    if os.path.exists(dst):
        raise VictorError(f"A model named {os.path.basename(src)} already exists; remove it first.")
    tmp = dst + ".part"
    shutil.copyfile(src, tmp)
    if os.path.getsize(tmp) != info["size"]:
        os.unlink(tmp)
        raise VictorError("Copy size mismatch; the destination may be failing. Nothing was imported.")
    os.fsync(os.open(tmp, os.O_RDONLY))
    os.replace(tmp, dst)
    if move:
        os.unlink(src)
    info["imported_to"] = dst
    info["checksum_verified"] = bool(sha256)
    info["estimate"] = estimate(info["size"], ctx)
    return info
