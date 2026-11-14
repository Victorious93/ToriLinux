"""Read-only companion report for the Victorious Browser Android app.

This is the ONLY thing the phone can learn about the PC besides inference itself, and it is served by the
gateway (never by the loopback-only Control Center) behind the same bearer key as inference. It contains no
secrets, no file paths, no chat content and cannot change anything. Measured facts come from /run (this
boot only), so a different PC can never present another PC's hardware.
"""
import hashlib
import json
import os
import socket
import ssl
import uuid

import victorcommon as tc

SCHEMA = 1


def _read_json(path):
    try:
        with open(path) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def pc_identity():
    """Stable random id for THIS VictorLinux data store; hashed machine-id when no storage is selected.

    The phone pins this id on first pairing. A different id at the same address means a different PC
    (or re-provisioned storage) and the app must say so instead of silently trusting it.
    """
    p = tc.data_path("config", "companion.json")
    if p:
        d = _read_json(p)
        if isinstance(d.get("pc_id"), str) and len(d["pc_id"]) == 36:
            return d["pc_id"]
        d = {"pc_id": str(uuid.uuid4()), "created": tc.now()}
        try:
            tc.atomic_write(p, json.dumps(d), 0o600)
            return d["pc_id"]
        except OSError:
            pass
    try:
        with open("/etc/machine-id") as f:
            return "m-" + hashlib.sha256(("victor-companion:" + f.read().strip()).encode()).hexdigest()[:32]
    except OSError:
        return "unknown"


def tls_fingerprint(cert_path=None):
    """SHA-256 of the gateway's DER certificate so the owner can compare it on both screens."""
    cert_path = cert_path or tc.data_path("server", "tls", "cert.pem")
    if not cert_path or not os.path.exists(cert_path):
        return None
    try:
        with open(cert_path) as f:
            der = ssl.PEM_cert_to_DER_cert(f.read())
        return hashlib.sha256(der).hexdigest()
    except (OSError, ValueError):
        return None


def report(cfg=None):
    host = _read_json(os.path.join(tc.RUN_DIR, "host.json"))
    runtime = _read_json(os.path.join(tc.RUN_DIR, "runtime.json"))
    ram = tc.ram_info()
    gpus = [{"vendor": g.get("vendor"), "name": g.get("name"), "vram_total": g.get("vram_total")} for g in tc.gpus()]
    dev = (host.get("device") or {})
    info = {
        "schema": SCHEMA,
        "pc": {"id": pc_identity(), "name": socket.gethostname()},
        "generated_at": tc.now(),
        "hardware": {
            "ram_total": ram.get("total"), "ram_available": ram.get("available"), "gpus": gpus,
            "backend": host.get("backend"), "backend_reason": host.get("backend_reason"),
            "memory_pool": dev.get("kind"), "memory_budget": dev.get("budget"),
            "threads_default": host.get("threads_default"),
        },
        "runtime": {
            "running": bool(runtime.get("running")),
            "model": os.path.basename(runtime["model"]) if runtime.get("model") else None,
            "backend": runtime.get("backend"), "context": runtime.get("ctx"), "gpu_layers": runtime.get("ngl"),
            "generation_check": runtime.get("verify"),
        },
        "transport": {"tls": bool((cfg or {}).get("tls")), "tls_fingerprint_sha256": tls_fingerprint()},
        "privacy": "Inference stays on this PC. No telemetry, no cloud fallback. This report is read-only and contains no keys, paths or chats.",
    }
    return info
