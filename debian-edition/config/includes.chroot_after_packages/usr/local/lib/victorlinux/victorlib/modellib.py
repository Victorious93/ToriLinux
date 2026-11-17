"""Model library: integrity hashes, per-model settings that worked, and run history per PC.

Shared by both editions (standard library only, no edition imports): the Arch Control Center and the Debian
`victor-ctl model library|hash|verify|remember` use this same file. Nothing here downloads anything; it only reads
the GGUF files the owner already has and writes one JSON file (0600) on the owner's storage.
"""
import hashlib
import json
import os
import re
import threading
import time

VERSION = 1
HISTORY_MAX = 20
SPLIT_RE = re.compile(r"-(\d{5})-of-(\d{5})\.gguf$", re.I)
# llama.cpp LLAMA_FTYPE values (general.file_type) -> quantisation names
FTYPES = {0: "F32", 1: "F16", 2: "Q4_0", 3: "Q4_1", 7: "Q8_0", 8: "Q5_0", 9: "Q5_1", 10: "Q2_K", 11: "Q3_K_S",
          12: "Q3_K_M", 13: "Q3_K_L", 14: "Q4_K_S", 15: "Q4_K_M", 16: "Q5_K_S", 17: "Q5_K_M", 18: "Q6_K",
          19: "IQ2_XXS", 20: "IQ2_XS", 21: "Q2_K_S", 22: "IQ3_XS", 23: "IQ3_XXS", 24: "IQ1_S", 25: "IQ4_NL",
          26: "IQ3_S", 27: "IQ3_M", 28: "IQ2_S", 29: "IQ2_M", 30: "IQ4_XS", 31: "IQ1_M", 32: "BF16",
          36: "TQ1_0", 37: "TQ2_0", 38: "MXFP4"}
QUANT_RE = re.compile(r"(?<![A-Za-z0-9])(I?Q\d(?:_[A-Z0-9]+)*|BF16|F16|F32|MXFP4)(?![A-Za-z0-9])", re.I)
SETTING_KEYS = {"backend": str, "ctx": int, "ctx_auto": bool, "gpu_layers": int, "threads": int}
BACKENDS = ("auto", "vulkan", "hip", "cuda", "cpu")


def now():
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def pc_signature(cpu_name, gpu_names, ram_total):
    """Stable, non-identifying id for 'this PC': CPU model + GPU models + RAM rounded to GiB. No serials, no MACs."""
    gib = int(round((ram_total or 0) / 2**30))
    raw = "|".join([str(cpu_name or "?").strip()] + sorted(str(g).strip() for g in gpu_names or []) + [str(gib)])
    return hashlib.sha256(raw.encode()).hexdigest()[:12]


def split_parts(path):
    """All files of a split set (or [path]) and whether every part exists."""
    m = SPLIT_RE.search(path)
    if not m:
        return [path], os.path.isfile(path)
    total = int(m.group(2))
    if not 1 <= total <= 1024:
        return [path], False
    base = path[:m.start()]
    parts = [f"{base}-{i:05d}-of-{total:05d}.gguf" for i in range(1, total + 1)]
    return parts, all(os.path.isfile(p) for p in parts)


def quant_name(meta, filename):
    ft = (meta or {}).get("general.file_type")
    if isinstance(ft, int) and ft in FTYPES:
        return FTYPES[ft]
    m = QUANT_RE.search(os.path.basename(filename))
    return m.group(1).upper() if m else None


def _stat(parts):
    size, mtime = 0, 0
    for p in parts:
        st = os.stat(p)
        size += st.st_size
        mtime = max(mtime, int(st.st_mtime))
    return size, mtime


def sha256_file(path, progress=None, chunk=4 << 20, cancel=None):
    h = hashlib.sha256()
    done = 0
    with open(path, "rb") as f:
        while True:
            if cancel is not None and cancel.is_set():
                raise InterruptedError("cancelled")
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
            done += len(b)
            if progress:
                progress(len(b))
    return h.hexdigest()


class Library:
    """One JSON file: {"version", "models": {path: {...}}}. Paths are the first file of a set."""

    def __init__(self, path):
        self.path = path
        self.lock = threading.RLock()

    def load(self):
        try:
            with open(self.path) as f:
                d = json.load(f)
            if isinstance(d, dict) and isinstance(d.get("models"), dict):
                return d
        except (OSError, ValueError):
            pass
        return {"version": VERSION, "models": {}}

    def save(self, d):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = f"{self.path}.tmp{os.getpid()}.{threading.get_ident()}"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(d, f, indent=2, sort_keys=True)
        os.replace(tmp, self.path)

    def update(self, path, fn):
        with self.lock:
            d = self.load()
            e = d["models"].setdefault(path, {})
            fn(e)
            self.save(d)
            return e

    def entry(self, path):
        return self.load()["models"].get(path, {})

    # -- integrity --------------------------------------------------------------------------------------
    def integrity(self, path, entry=None):
        e = self.entry(path) if entry is None else entry
        parts, complete = split_parts(path)
        if not complete:
            return {"state": "incomplete", "label": "Split set is missing parts"}
        h = e.get("hash")
        if not h:
            return {"state": "unhashed", "label": "Not hashed yet"}
        try:
            size, mtime = _stat(parts)
        except OSError:
            return {"state": "missing", "label": "File not found"}
        if h.get("result") == "mismatch":
            return {"state": "mismatch", "label": "Checksum changed: file is corrupted or was replaced", "sha256": h.get("sha256")}
        if (size, mtime) != (h.get("size"), h.get("mtime")):
            return {"state": "changed", "label": "File changed since it was hashed; hash again", "sha256": h.get("sha256")}
        exp = e.get("expected")
        if exp and exp != h.get("sha256"):
            return {"state": "mismatch", "label": "Does not match the checksum you supplied", "sha256": h.get("sha256")}
        return {"state": "ok", "label": "Matches your checksum" if exp else "Hashed", "sha256": h.get("sha256"),
                "checked": h.get("checked"), "matches_expected": bool(exp)}

    def hash_model(self, path, progress=None, cancel=None, expected=None):
        """Hash every part. A previous digest that no longer matches an unchanged file is reported as a mismatch."""
        parts, complete = split_parts(path)
        if not complete:
            raise ValueError("Split set is missing parts; it cannot be hashed.")
        size, mtime = _stat(parts)
        digests = [sha256_file(p, progress, cancel=cancel) for p in parts]
        digest = digests[0] if len(digests) == 1 else hashlib.sha256("".join(digests).encode()).hexdigest()
        ent = self.entry(path)
        prev = ent.get("hash") or {}
        result = "ok"
        if prev.get("sha256") and (prev.get("size"), prev.get("mtime")) == (size, mtime) and prev["sha256"] != digest:
            result = "mismatch"
        exp = (expected or ent.get("expected") or "").strip().lower() or None
        if exp and not re.fullmatch(r"[0-9a-f]{64}", exp):
            raise ValueError("Expected SHA-256 must be 64 hexadecimal characters.")

        def upd(e):
            e["hash"] = {"sha256": digest if result == "ok" else prev["sha256"], "observed": digest, "parts": digests,
                         "size": size, "mtime": mtime, "checked": now(), "result": result}
            if exp:
                e["expected"] = exp
        self.update(path, upd)
        return self.integrity(path)

    def reset_hash(self, path):
        """Owner accepts the current file as the new reference (e.g. after replacing it on purpose)."""
        def upd(e):
            e.pop("hash", None)
            e.pop("expected", None)
        self.update(path, upd)

    def set_expected(self, path, sha):
        sha = (sha or "").strip().lower()
        if not re.fullmatch(r"[0-9a-f]{64}", sha):
            raise ValueError("Expected SHA-256 must be 64 hexadecimal characters.")
        self.update(path, lambda e: e.__setitem__("expected", sha))

    # -- remembered settings ----------------------------------------------------------------------------
    def remember(self, path, settings):
        clean = {}
        for k, typ in SETTING_KEYS.items():
            if k not in settings:
                continue
            v = settings[k]
            if typ is bool:
                if not isinstance(v, bool):
                    raise ValueError(f"{k} must be true or false")
            else:
                v = typ(v)
            clean[k] = v
        if "backend" in clean and clean["backend"] not in BACKENDS:
            raise ValueError("unknown backend")
        if "ctx" in clean and not 256 <= clean["ctx"] <= 1 << 20:
            raise ValueError("ctx out of range")
        for k in ("threads",):
            if k in clean and clean[k] < 0:
                raise ValueError(f"{k} must be >= 0")
        if "gpu_layers" in clean and clean["gpu_layers"] < -1:
            raise ValueError("gpu_layers must be -1 (auto) or more")
        self.update(path, lambda e: e.__setitem__("settings", clean | {"saved": now()}))
        return clean

    def forget_settings(self, path):
        self.update(path, lambda e: e.pop("settings", None))

    # -- run history ------------------------------------------------------------------------------------
    def record_run(self, path, pc, backend, device, ngl, ctx, ok=True, note=None):
        def upd(e):
            h = e.setdefault("history", [])
            h.append({"pc": pc, "backend": backend, "device": device, "ngl": ngl, "ctx": ctx, "ok": bool(ok),
                      "note": (note or "")[:200], "ts": now()})
            del h[:-HISTORY_MAX]
        self.update(path, upd)

    def worked_here(self, path, pc, entry=None):
        e = self.entry(path) if entry is None else entry
        runs = [r for r in e.get("history", []) if r.get("pc") == pc and r.get("ok")]
        return runs[-1] if runs else None

    # -- listing ----------------------------------------------------------------------------------------
    def describe(self, models, pc, meta_for=None):
        """models: [{"path","name","size"}]; meta_for(path) -> GGUF metadata dict (or raises)."""
        d = self.load()["models"]
        out = []
        for m in models:
            p = m["path"]
            e = d.get(p, {})
            parts, complete = split_parts(p)
            meta, err = {}, None
            if meta_for:
                try:
                    meta = meta_for(p) or {}
                except Exception as ex:  # noqa: BLE001 - shown to the owner, not fatal for the list
                    err = str(ex)
            try:
                total = sum(os.path.getsize(x) for x in parts if os.path.exists(x))
            except OSError:
                total = m.get("size")
            out.append({"path": p, "name": m.get("name") or os.path.basename(p), "dir": m.get("dir"),
                        "size": total, "parts": len(parts), "complete": complete,
                        "arch": meta.get("general.architecture"), "title": meta.get("general.name"),
                        "quant": quant_name(meta, p), "header_error": err,
                        "integrity": self.integrity(p, e), "settings": e.get("settings"),
                        "worked_here": self.worked_here(p, pc, e),
                        "runs": len(e.get("history", [])), "history": e.get("history", [])[-5:]})
        return out


class HashJob:
    """One background hashing queue. Progress is in bytes over the whole queue."""

    def __init__(self, library):
        self.lib = library
        self.cancel = threading.Event()
        self.state = {"state": "idle"}
        self.thread = None

    def status(self):
        return dict(self.state)

    def start(self, paths):
        if self.thread and self.thread.is_alive():
            raise RuntimeError("A hashing job is already running.")
        paths = [p for p in dict.fromkeys(paths)]
        total = 0
        for p in paths:
            parts, complete = split_parts(p)
            if complete:
                total += sum(os.path.getsize(x) for x in parts)
        self.cancel.clear()
        self.state = {"state": "running", "total": total, "done": 0, "queue": len(paths), "finished": 0,
                      "current": None, "results": {}, "started": now()}

        def prog(n):
            self.state["done"] += n

        def run():
            for p in paths:
                if self.cancel.is_set():
                    break
                self.state["current"] = os.path.basename(p)
                try:
                    self.state["results"][p] = self.lib.hash_model(p, prog, self.cancel)["state"]
                except InterruptedError:
                    break
                except (OSError, ValueError) as e:
                    self.state["results"][p] = "error: " + str(e)
                self.state["finished"] += 1
            self.state["state"] = "cancelled" if self.cancel.is_set() else "done"
            self.state["current"] = None
        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()
        return self.status()

    def stop(self):
        self.cancel.set()
