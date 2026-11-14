import json, os, time
from .common import *
from . import hw, storage, llama


def collect():
    """Copy-pasteable diagnostics. Secrets (API key, TLS key, password hashes) are never included."""
    d = {"time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "kernel": os.uname().release,
         "image_facts": dict(l.strip().split("=", 1) for l in open("/usr/share/victorlinux/image-facts") if "=" in l) if os.path.exists("/usr/share/victorlinux/image-facts") else {},
         "llama_build": dict(l.strip().split("=", 1) for l in open("/usr/share/victorlinux/llamacpp.build-info") if "=" in l) if os.path.exists("/usr/share/victorlinux/llamacpp.build-info") else {},
         "hardware": hw.snapshot(), "storage": storage.status()}
    try:
        c = llama.load_config()
        c["remote"] = {k: v for k, v in c["remote"].items()}
        d["llama_config"] = c
        d["llama_status"] = llama.status()
    except VictorError as e:
        d["llama_config_error"] = str(e)
    j = run(["journalctl", "--user", "-u", llama.UNIT, "-n", "60", "--no-pager"], timeout=15)
    d["llama_log_tail"] = j.stdout.splitlines()[-60:]
    return d


def save_log_snapshot(max_files=10, max_bytes=1_000_000):
    """Bounded persistent logs: tail of the inference journal, rotated."""
    ld = data_path("logs")
    j = run(["journalctl", "--user", "-u", llama.UNIT, "-n", "5000", "--no-pager"], timeout=30).stdout
    name = os.path.join(ld, time.strftime("llama-%Y%m%dT%H%M%S.log"))
    with open(name, "w") as f:
        f.write(j[-max_bytes:])
    logs = sorted(x for x in os.listdir(ld) if x.startswith("llama-"))
    for old in logs[:-max_files]:
        os.unlink(os.path.join(ld, old))
    return name
