import json, os, subprocess, shutil, sys, time

RUN = os.environ.get("VICTOR_RUN", "/run/victor")
DATA_MP = f"{RUN}/data"          # persistent partition mountpoint
SESSION_MP = f"{RUN}/session"    # tmpfs (explicit temporary-session mode)
ROOT_LINK = f"{RUN}/root"        # symlink -> DATA_MP or SESSION_MP
STATE_FILE = f"{RUN}/storage.json"
LLAMA_DIR = os.environ.get("VICTOR_LLAMA_DIR", "/opt/victorlinux/llama.cpp")
LLAMA_SERVER = f"{LLAMA_DIR}/bin/llama-server"
LLAMA_BENCH = f"{LLAMA_DIR}/bin/llama-bench"
DATA_LABEL = "VICTORDATA"            # label written by provision-usb.py
DATA_LABELS = ("VICTORDATA", "TORIDATA")   # accepted labels (TORIDATA = the Arch edition, so one drive serves both)
SUBDIRS = ["models", "chat", "settings", "images", "llama", "kali", "documents", "logs", "secrets"]
MIN_FREE_BYTES = 256 * 1024 * 1024


class VictorError(Exception):
    """Error with an actionable message for the user."""


def run(cmd, timeout=30, check=False, **kw):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, **kw)
    except FileNotFoundError:
        return subprocess.CompletedProcess(cmd, 127, "", f"{cmd[0]}: not found")
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(cmd, 124, "", f"{cmd[0]}: timed out after {timeout}s")
    if check and p.returncode != 0:
        raise VictorError(f"{' '.join(cmd)} failed: {p.stderr.strip() or p.stdout.strip()}")
    return p


def read_json(path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def write_json_atomic(path, obj, mode=0o600):
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    tmp = f"{path}.tmp{os.getpid()}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w") as f:
        json.dump(obj, f, indent=2, sort_keys=True)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def human(n):
    if n is None:
        return "?"
    n = float(n)
    for u in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024 or u == "TiB":
            return f"{n:.0f} {u}" if u == "B" else f"{n:.1f} {u}"
        n /= 1024


def mem_info():
    d = {}
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                k, v = line.split(":", 1)
                d[k] = int(v.split()[0]) * 1024
    except OSError:
        pass
    return {"total": d.get("MemTotal"), "available": d.get("MemAvailable"), "swap_total": d.get("SwapTotal")}


def root_dir():
    """Return the active data root (persistent or session) or raise."""
    if os.path.isdir(ROOT_LINK):
        return os.path.realpath(ROOT_LINK)
    raise VictorError("No data location is active yet. Open Control Center > Storage and choose a VICTORDATA "
                      "partition or start a temporary session (data is lost at power-off).")


def data_path(*parts):
    return os.path.join(root_dir(), *parts)


def settings_file(name):
    return data_path("settings", name)
