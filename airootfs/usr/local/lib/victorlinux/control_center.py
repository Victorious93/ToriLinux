#!/usr/bin/env python3
"""VictorLinux Control Center: local web UI + JSON API for managing AI services.

Listens on 127.0.0.1 only. Every API call needs the per-session token written
to ~/.cache/victorlinux/cc.token (0600); the Host header is checked to defeat DNS
rebinding. Standard library only. Runs as the ordinary user 'victor'; the few
privileged operations go through the restricted helper `victor-priv` via sudo.
"""
import hashlib
import hmac
import http.server
import json
import os
import re
import secrets as pysecrets
import shutil
import signal
import socketserver
import subprocess
import sys
import threading
import time
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import victorcommon as tc  # noqa: E402
import model_advisor  # noqa: E402
import hostplan  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
PRIV = os.environ.get("VICTOR_PRIV", "/usr/local/lib/victorlinux/victor-priv")
CC_PORT = int(os.environ.get("VICTOR_CC_PORT", "8787"))
UPSTREAM_PORT = 18081
HOME = os.path.expanduser("~")
TOKEN_FILE = os.path.join(os.environ.get("XDG_CACHE_HOME", os.path.join(HOME, ".cache")), "victorlinux", "cc.token")

# Optional image-generation / chat packs. Shown only if installed in the image.
SERVICES = {
    "comfyui": {"label": "ComfyUI", "icon": "image", "dir": "ComfyUI", "port": 8188, "vram_hint_gib": 6,
                "cmd": ["venv/bin/python", "main.py", "--listen", "127.0.0.1", "--port", "8188"], "url": "http://127.0.0.1:8188/"},
    "automatic": {"label": "SD.Next (automatic)", "icon": "image", "dir": "automatic", "port": 7860, "vram_hint_gib": 6,
                  "cmd": ["venv/bin/python", "launch.py", "--listen=false", "--port", "7860", "--skip-all"], "url": "http://127.0.0.1:7860/"},
    "sillytavern": {"label": "SillyTavern", "icon": "chat", "bin": "sillytavern", "port": 8000, "vram_hint_gib": 0,
                    "cmd": ["sillytavern"], "url": "http://127.0.0.1:8000/"},
}


class Proc:
    def __init__(self, name):
        self.name, self.p, self.started, self.log = name, None, None, None

    def running(self):
        return self.p is not None and self.p.poll() is None


class App:
    def __init__(self):
        self.lock = threading.RLock()
        self.llm = Proc("llama")
        self.gw = Proc("gateway")
        self.svc = {k: Proc(k) for k in SERVICES}
        self.import_job = {"state": "idle"}
        self.llm_info = {}
        self.token = pysecrets.token_urlsafe(32)
        os.makedirs(os.path.dirname(TOKEN_FILE), exist_ok=True)
        fd = os.open(TOKEN_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(self.token)

    # ---------------------------------------------------------- logging --
    def logdir(self):
        d = tc.data_path("logs") or os.path.join(tc.RUN_DIR, "logs")
        try:
            os.makedirs(d, exist_ok=True)
        except OSError:
            d = "/tmp/victor-logs"
            os.makedirs(d, exist_ok=True)
        return d

    def logpath(self, name):
        return os.path.join(self.logdir(), f"{name}.log")

    def spawn(self, proc, cmd, cwd=None, env=None):
        lp = self.logpath(proc.name)
        if os.path.exists(lp):
            try:
                os.replace(lp, lp + ".1")
            except OSError:
                pass
        logf = open(lp, "ab", buffering=0)
        logf.write(f"# {tc.now()} start: {' '.join(a if 'key' not in prev.lower() else '***' for prev, a in zip(['']+cmd, cmd))}\n".encode())
        e = dict(os.environ)
        e.update(env or {})
        proc.p = subprocess.Popen(cmd, cwd=cwd, env=e, stdout=logf, stderr=subprocess.STDOUT,
                                  stdin=subprocess.DEVNULL, start_new_session=True)
        proc.started = time.time()
        proc.log = lp

    def stop(self, proc, timeout=10):
        if proc.running():
            try:
                os.killpg(proc.p.pid, signal.SIGTERM)
                proc.p.wait(timeout)
            except subprocess.TimeoutExpired:
                os.killpg(proc.p.pid, signal.SIGKILL)
                proc.p.wait(5)
            except ProcessLookupError:
                pass

    def tail(self, name, n=200):
        p = self.logpath(name)
        try:
            with open(p, "rb") as f:
                f.seek(0, 2)
                size = f.tell()
                f.seek(max(0, size - 64 * 1024))
                return "\n".join(f.read().decode("utf-8", "replace").splitlines()[-n:])
        except OSError:
            return ""

    # ------------------------------------------------------------- state --
    def ensure_upstream_key(self):
        s = tc.load_secrets()
        if not s.get("upstream_key"):
            s["upstream_key"] = pysecrets.token_urlsafe(32)
            tc.save_secrets(s)
        return s["upstream_key"]

    def llama_flags(self, binary):
        try:
            r = subprocess.run([binary, "--help"], capture_output=True, text=True, timeout=30)
            return r.stdout + r.stderr
        except (OSError, subprocess.SubprocessError):
            return ""

    def plan_llm(self, settings):
        """Resolve backend, model, memory fit; returns dict with 'error' if it cannot start."""
        model = settings.get("model")
        if not model or not os.path.isfile(model):
            return {"error": "No model selected, or the file no longer exists. Select a model on the Models tab."}
        ok, why = tc.check_split_complete(model)
        if not ok:
            return {"error": why}
        gl = tc.gpus()
        backend, reason = tc.choose_backend(settings["backend"], gl)
        if not backend:
            return {"error": reason}
        ram = tc.ram_info()
        vk = tc.vulkan_devices() if backend == "vulkan" else None
        dev = hostplan.select_device(backend, gl, ram, vk)
        try:
            est = tc.estimate_memory(model, int(settings["ctx"]))
            ctx, ctx_note = int(settings["ctx"]), "Context set manually by the owner."
            if settings.get("ctx_auto", True):
                pool = dev["budget"] if dev["kind"] == "dedicated" and est["weights"] + est["overhead"] <= dev["budget"] \
                    else dev["budget"] + (int(ram.get("available", 0) * hostplan.RAM_FRACTION) if dev["kind"] == "dedicated" else 0)
                ctx, ok, ctx_note = hostplan.auto_context(
                    est["weights"], lambda c: tc.estimate_memory(model, c)["kv_cache"], est["overhead"], pool, est.get("ctx_train"))
                est = tc.estimate_memory(model, ctx)
        except tc.GGUFError as e:
            return {"error": f"This file is not a valid GGUF model: {e}"}
        vram_free = dev["free"] if dev["kind"] == "dedicated" else None
        fit = tc.fit_report(est, ram, vram_free)
        limit = int(settings.get("memory_limit_mb") or 0) * 1048576
        if limit and est["total"] > limit and not (vram_free and est["total"] <= vram_free):
            return {"error": f"Estimated memory {tc.human(est['total'])} exceeds your limit of {tc.human(limit)}. "
                             "Lower context length, pick a smaller quantisation, or raise the limit."}
        if fit["level"] == "bad":
            return {"error": "Not enough memory: " + " ".join(fit["notes"]), "estimate": est, "fit": fit}
        ngl = int(settings.get("gpu_layers", -1))
        if backend == "cpu":
            ngl = 0
        elif ngl < 0:
            ngl = hostplan.plan_layers(est, dev, ctx)
        threads = int(settings.get("threads") or 0) or hostplan.default_threads()
        return {"backend": backend, "reason": reason, "model": model, "estimate": est, "fit": fit,
                "ngl": ngl, "ctx": ctx, "ctx_note": ctx_note, "threads": threads,
                "device": {k: dev[k] for k in ("kind", "budget", "free", "ambiguous", "notes")} |
                          {"name": (dev["device"] or {}).get("name"), "index": dev.get("index"), "others": len(dev["others"])},
                "binary": tc.backends()[backend]}

    def start_llm(self, settings, force=False):
        with self.lock:
            if self.llm.running():
                return {"ok": True, "message": "Already running."}
            if tc.data_root() is None:
                return {"ok": False, "error": "No storage selected. Choose persistent storage or temporary-session mode first."}
            plan = self.plan_llm(settings)
            if "error" in plan:
                return {"ok": False, **plan}
            key = self.ensure_upstream_key()
            help_text = self.llama_flags(plan["binary"])
            cmd = [plan["binary"], "-m", plan["model"], "--host", "127.0.0.1", "--port", str(UPSTREAM_PORT),
                   "-c", str(plan["ctx"]), "-ngl", str(plan["ngl"]), "--api-key", key, "--alias", "victor-local"]
            # security-relevant flags: only passed when this llama-server build knows them
            if "--no-ui-mcp-proxy" in help_text:
                cmd.append("--no-ui-mcp-proxy")
            if "--jinja" in help_text:
                cmd.append("--jinja")
            # NOTE: '--tools' (built-in shell/file tools) is deliberately never passed.
            cmd += ["-t", str(plan["threads"])]
            idle = int(settings.get("idle_unload_s") or 0)
            if idle > 0 and "--sleep-idle-seconds" in help_text:
                cmd += ["--sleep-idle-seconds", str(idle)]
            # belt and braces: shared libs and dynamic ggml backends live next to the binary
            env = {"LD_LIBRARY_PATH": os.path.dirname(plan["binary"])}
            idx = plan["device"].get("index")
            if plan["backend"] == "cuda" and idx is not None and plan["device"]["others"]:
                env["CUDA_DEVICE_ORDER"] = "PCI_BUS_ID"          # matches nvidia-smi numbering
                env["CUDA_VISIBLE_DEVICES"] = str(idx)           # memory is attributed to exactly this card
            if plan["backend"] == "vulkan":
                env["GGML_VK_VISIBLE_DEVICES"] = os.environ.get("GGML_VK_VISIBLE_DEVICES", "")
                if not env["GGML_VK_VISIBLE_DEVICES"]:
                    env.pop("GGML_VK_VISIBLE_DEVICES")
            self.spawn(self.llm, cmd, env=env)
            self.llm_info = {"backend": plan["backend"], "reason": plan["reason"], "ngl": plan["ngl"],
                             "model": plan["model"], "estimate": plan["estimate"], "fit": plan["fit"],
                             "ctx": plan["ctx"], "ctx_note": plan["ctx_note"], "threads": plan["threads"], "device": plan["device"]}
            hostplan.write_host_profile(hostplan.host_profile(settings))
        time.sleep(1.5)
        if not self.llm.running():
            return {"ok": False, "error": "llama-server exited immediately. See the log below.", "log": self.tail("llama", 40)}
        gw = self.start_gateway(settings)
        if not gw.get("ok"):
            return {"ok": False, "error": "Model started but the API gateway did not: " + gw.get("error", "unknown"),
                    "log": self.tail("gateway", 20)}
        self.write_runtime(True, {"state": "waiting"})
        threading.Thread(target=self.verify_generation, args=(dict(self.llm_info), key), daemon=True).start()
        return {"ok": True, "plan": self.llm_info}

    def write_runtime(self, running, verify=None):
        """Volatile, secret-free runtime summary for the gateway's read-only companion report."""
        path = os.path.join(tc.RUN_DIR, "runtime.json")
        try:
            if not running:
                if os.path.exists(path):
                    os.unlink(path)
                return
            os.makedirs(tc.RUN_DIR, exist_ok=True)
            i = self.llm_info
            tc.atomic_write(path, json.dumps({"running": True, "model": i.get("model"), "backend": i.get("backend"),
                                              "ctx": i.get("ctx"), "ngl": i.get("ngl"), "verify": verify}), 0o644)
        except OSError:
            pass

    def verify_generation(self, info, key):
        """Mark a model 'verified' only after llama-server really produced a token on this PC."""
        import urllib.request
        base = f"http://127.0.0.1:{UPSTREAM_PORT}"
        self.llm_verify = {"state": "waiting"}
        for _ in range(120):
            if not self.llm.running():
                self.llm_verify = {"state": "failed", "error": "llama-server exited before it finished loading."}
                self.write_runtime(True, {"state": "failed"})
                return
            try:
                with urllib.request.urlopen(base + "/health", timeout=2) as r:
                    if r.status == 200:
                        break
            except OSError:
                pass
            time.sleep(1)
        else:
            self.llm_verify = {"state": "failed", "error": "Model did not become ready within 2 minutes."}
            self.write_runtime(True, {"state": "failed"})
            return
        try:
            req = urllib.request.Request(base + "/v1/chat/completions", method="POST",
                                         data=json.dumps({"model": "victor-local", "max_tokens": 1,
                                                          "messages": [{"role": "user", "content": "Hi"}]}).encode(),
                                         headers={"Content-Type": "application/json", "Authorization": "Bearer " + key})
            with urllib.request.urlopen(req, timeout=120) as r:
                body = json.loads(r.read().decode())
            if not body.get("choices"):
                raise ValueError("no choices in response")
        except (OSError, ValueError) as e:
            self.llm_verify = {"state": "failed", "error": f"Loaded but did not generate: {e}"}
            self.write_runtime(True, {"state": "failed"})
            return
        name = (info.get("device") or {}).get("name")
        hostplan.record_verified(info["model"], info["backend"], name, info["ngl"], info["ctx"])
        self.llm_verify = {"state": "verified", "backend": info["backend"], "device": name}
        self.write_runtime(True, {"state": "verified"})

    def stop_llm(self):
        self.write_runtime(False)
        with self.lock:
            self.stop(self.llm)
            self.stop(self.gw)
            self.llm_info = {}

    def start_gateway(self, settings):
        with self.lock:
            self.stop(self.gw)
            rm = settings["remote"]
            cmd = [sys.executable, "-I", os.path.join(HERE, "gateway.py"), "--upstream-port", str(UPSTREAM_PORT)]
            env = {"PYTHONPATH": HERE}
            try:
                self.spawn(self.gw, cmd, env=env)
            except OSError as e:
                return {"ok": False, "error": str(e)}
        time.sleep(0.7)
        if not self.gw.running():
            return {"ok": False, "error": "Gateway did not start: " + self.tail("gateway", 5)}
        return {"ok": True}

    def accel_report(self):
        """Derive real acceleration facts from the llama-server log, not from package presence."""
        text = self.tail("llama", 400)
        info = {"offloaded_layers": None, "total_layers": None, "devices": [], "errors": []}
        m = re.search(r"offloaded (\d+)/(\d+) layers to GPU", text)
        if m:
            info["offloaded_layers"], info["total_layers"] = int(m.group(1)), int(m.group(2))
        for line in text.splitlines():
            if re.search(r"ggml_vulkan: \d+ = |Vulkan\d+ \(|ggml_cuda_init: found|ROCm\d* \(", line):
                info["devices"].append(line.strip()[:200])
            if re.search(r"\b(error|failed|out of memory|cannot|unable)\b", line, re.I):
                info["errors"].append(line.strip()[:200])
        info["errors"] = info["errors"][-8:]
        info["devices"] = info["devices"][-6:]
        if info["offloaded_layers"] is None:
            info["gpu_accelerated"] = None
        else:
            info["gpu_accelerated"] = info["offloaded_layers"] > 0
        return info

    def status(self):
        s = tc.load_settings()
        gl = tc.gpus()
        be, reason = tc.choose_backend(s["backend"], gl)
        svcs = []
        for k, d in SERVICES.items():
            base = os.path.join(HOME, d["dir"]) if "dir" in d else None
            installed = (base and os.path.isdir(base)) or (d.get("bin") and shutil.which(d["bin"]))
            svcs.append({"name": k, "label": d["label"], "icon": d["icon"], "installed": bool(installed),
                         "running": self.svc[k].running(), "url": d["url"]})
        sec = tc.load_secrets()
        up = self.llm.running()
        return {
            "persist": tc.persist_state(), "data_root": tc.data_root(),
            "ram": tc.ram_info(), "gpus": gl, "render_nodes": tc.render_nodes(),
            "backends_present": tc.backends(), "backend": {"selected": be, "reason": reason},
            "llm": {"running": up, "info": self.llm_info if up else {}, "accel": self.accel_report() if up else None,
                    "uptime": int(time.time() - self.llm.started) if up else None},
            "gateway": {"running": self.gw.running()}, "verify": getattr(self, "llm_verify", None),
            "services": svcs, "settings": s, "has_api_key": bool(sec.get("api_keys")),
            "ssh": self.priv_json(["ssh-status"]),
            "offline_notice": "All inference is local. No telemetry, cloud inference or automatic downloads are configured.",
        }

    # -------------------------------------------------------------- priv --
    def priv(self, args, stdin=None, timeout=60):
        try:
            r = subprocess.run(["sudo", "-n", PRIV] + args, input=stdin, capture_output=True, text=True, timeout=timeout)
        except (OSError, subprocess.SubprocessError) as e:
            return 1, str(e)
        return r.returncode, (r.stdout.strip() if r.returncode == 0 else (r.stderr.strip() or r.stdout.strip()))

    def priv_json(self, args):
        rc, out = self.priv(args)
        if rc != 0:
            return {"error": out}
        try:
            return json.loads(out)
        except ValueError:
            return {"error": out}

    # ----------------------------------------------------------- remote --
    def set_remote(self, rem):
        s = tc.load_settings()
        r = s["remote"]
        bind = str(rem.get("bind", r["bind"])).strip()
        if not re.fullmatch(r"[0-9a-fA-F:.]+", bind):
            return {"ok": False, "error": "Bind must be an IP address (use 127.0.0.1 for local-only, a LAN/VPN address, or 0.0.0.0 for all interfaces)."}
        port = int(rem.get("port", r["port"]))
        if not 1024 <= port <= 65535:
            return {"ok": False, "error": "Port must be 1024-65535."}
        enabled = bool(rem.get("enabled", r["enabled"]))
        if enabled and not tc.load_secrets().get("api_keys"):
            return {"ok": False, "error": "Create an API key first (remote access always requires one)."}
        if enabled and bind in ("127.0.0.1", "::1"):
            enabled = False   # nothing remote about loopback; keep honest state
        tls = bool(rem.get("tls", r["tls"]))
        if tls and not (tc.data_path("server", "tls", "cert.pem") and os.path.exists(tc.data_path("server", "tls", "cert.pem"))):
            return {"ok": False, "error": "TLS needs a certificate. Generate one first (button below), or use a VPN/tunnel instead."}
        s["remote"] = {"enabled": enabled, "bind": bind if enabled else "127.0.0.1", "port": port, "tls": tls}
        tc.save_settings(s)
        if self.llm.running():
            self.start_gateway(s)
        return {"ok": True, "remote": s["remote"]}

    def new_api_key(self):
        key = "victor-" + pysecrets.token_urlsafe(32)
        sec = tc.load_secrets()
        sec["api_keys"] = [hashlib.sha256(key.encode()).hexdigest()]   # one active key; rotating revokes the old one
        tc.save_secrets(sec)
        s = tc.load_settings()
        if self.gw.running():
            self.start_gateway(s)
        return key

    def revoke_remote(self):
        sec = tc.load_secrets()
        sec["api_keys"] = []
        tc.save_secrets(sec)
        s = tc.load_settings()
        s["remote"] = {"enabled": False, "bind": "127.0.0.1", "port": s["remote"]["port"], "tls": False}
        tc.save_settings(s)
        if self.gw.running():
            self.start_gateway(s)
        return {"ok": True}

    # -------------------------------------------------------------- diag --
    def diagnostics(self):
        lines = [f"VictorLinux diagnostics {tc.now()}"]
        try:
            lines.append("Image: " + (open("/etc/victor-release").read().strip()))
        except OSError:
            lines.append("Image: (no /etc/victor-release)")
        lines.append("Kernel: " + os.uname().release)
        r = tc.ram_info()
        lines.append(f"RAM: total {tc.human(r['total'])}, available {tc.human(r['available'])}")
        gl = tc.gpus()
        if not gl:
            lines.append("GPU: none detected via sysfs/nvidia-smi")
        for g in gl:
            lines.append(f"GPU: {g['name']} [{g.get('pci_id','')}] driver={g.get('driver')} "
                         f"VRAM total={tc.human(g.get('vram_total'))} used={tc.human(g.get('vram_used'))}")
        for n in tc.render_nodes():
            lines.append(f"Render node: {n['path']} read/write={'yes' if n['accessible'] else 'NO - add user to video/render groups'}")
        lines.append("User groups: " + subprocess.run(["id", "-Gn"], capture_output=True, text=True).stdout.strip())
        vk = tc.vulkan_devices()
        lines.append(f"Vulkan: available={vk['available']} error={vk.get('error')}")
        for d in vk["devices"]:
            lines.append("  " + ", ".join(f"{k}={v}" for k, v in d.items()))
        be, why = tc.choose_backend(tc.load_settings()["backend"], gl, vk)
        lines.append(f"Backends present: {tc.backends()}")
        dev = hostplan.select_device(be, gl, r, vk if be == "vulkan" else None)
        lines.append(f"Memory pool: {dev['kind']} budget={tc.human(dev['budget'])} free={tc.human(dev['free'])} "
                     f"device={(dev['device'] or {}).get('name')} other_devices={len(dev['others'])}")
        for n in dev["notes"]:
            lines.append("  note: " + n)
        lines.append(f"Default threads: {hostplan.default_threads()} (physical {hostplan.physical_threads()})")
        if getattr(self, "llm_verify", None):
            lines.append("Generation check: " + json.dumps(self.llm_verify))
        lines.append(f"Selected backend: {be} - {why}")
        lines.append(f"Storage: {json.dumps(tc.persist_state())}")
        if self.llm.running():
            lines.append("Accel (from llama-server log): " + json.dumps(self.accel_report()))
        lines.append("--- llama-server log (tail) ---")
        lines.append(self.tail("llama", 40))
        lines.append("--- gateway log (tail) ---")
        lines.append(self.tail("gateway", 10))
        text = "\n".join(lines)
        return re.sub(r"(victor-[A-Za-z0-9_\-]{20,})", "victor-***", text)


APP = None


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "VictorCC"

    def log_message(self, fmt, *a):
        pass

    def _send(self, code, body, ctype="application/json", nonce=None):
        b = body if isinstance(body, bytes) else (json.dumps(body) if not isinstance(body, str) else body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        script_src = f"'self' 'nonce-{nonce}'" if nonce else "'self'"
        self.send_header("Content-Security-Policy", f"default-src 'self'; script-src {script_src}; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(b)

    def _host_ok(self):
        host = (self.headers.get("Host") or "").split(":")[0]
        return host in ("127.0.0.1", "localhost", "[::1]")

    def _auth(self):
        t = self.headers.get("X-Victor-Token", "")
        return hmac.compare_digest(t, APP.token)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n < 0 or n > 1 << 20:
            raise ValueError("body too large")
        body = json.loads(self.rfile.read(n) or b"{}") if n else {}
        if not isinstance(body, dict):
            raise ValueError("Request body must be a JSON object.")
        return body

    def do_GET(self):
        if not self._host_ok():
            return self._send(403, {"error": "bad host"})
        u = urllib.parse.urlparse(self.path)
        if u.path == "/":
            # The single page has one inline <script>; allow only that one via a per-response nonce.
            nonce = pysecrets.token_urlsafe(16)
            with open(os.path.join(HERE, "web", "index.html"), "rb") as f:
                page = f.read().replace(b"<script>", b'<script nonce="' + nonce.encode() + b'">')
            return self._send(200, page, "text/html; charset=utf-8", nonce=nonce)
        if not u.path.startswith("/api/"):
            return self._send(404, {"error": "not found"})
        if not self._auth():
            return self._send(401, {"error": "invalid token - reopen the Control Center from the desktop/menu entry"})
        q = urllib.parse.parse_qs(u.query)
        try:
            if u.path == "/api/status":
                return self._send(200, APP.status())
            if u.path == "/api/models":
                s = tc.load_settings()
                return self._send(200, {"models": tc.list_models(s["extra_model_dirs"]), "dirs": [tc.data_path("models")] + s["extra_model_dirs"]})
            if u.path == "/api/recommendations":
                gl = tc.gpus()
                s = tc.load_settings()
                backend, _reason = tc.choose_backend("auto", gl)
                ram = tc.ram_info()
                dev = hostplan.select_device(backend, gl, ram, tc.vulkan_devices() if backend == "vulkan" else None)
                rep = model_advisor.report(ram, gl, backend, s["ctx"])
                rep["host"] = {"backend": backend, "memory_kind": dev["kind"], "budget": dev["budget"], "notes": dev["notes"],
                               "device": (dev["device"] or {}).get("name"), "other_devices": len(dev["others"]),
                               "threads_default": hostplan.default_threads()}
                rep["local"] = hostplan.rank_local(tc.list_models(s["extra_model_dirs"]), ram, dev, backend)
                rep["local_note"] = ("Fit is an estimate. 'verified-here' means this exact file loaded and produced a token on "
                                     "this backend/device; 'untested' means it has not been proven on this PC.")
                return self._send(200, rep)
            if u.path == "/api/estimate":
                path = q["path"][0]
                ctx = int(q.get("ctx", ["4096"])[0])
                s = tc.load_settings()
                if path not in [m["path"] for m in tc.list_models(s["extra_model_dirs"])]:
                    return self._send(400, {"error": "path is not in a configured model directory"})
                est = tc.estimate_memory(path, ctx)
                gl = tc.gpus()
                vf = None
                for g in gl:
                    if g.get("vram_total") is not None:
                        vf = g["vram_total"] - (g.get("vram_used") or 0)
                        break
                return self._send(200, {"estimate": est, "fit": tc.fit_report(est, tc.ram_info(), vf), "vram_free": vf,
                                        "disk": shutil.disk_usage(os.path.dirname(path)).free})
            if u.path == "/api/logs":
                name = q.get("name", ["llama"])[0]
                if name not in ("llama", "gateway") + tuple(SERVICES):
                    return self._send(400, {"error": "unknown log"})
                return self._send(200, {"log": APP.tail(name, int(q.get("n", ["300"])[0]))})
            if u.path == "/api/diagnostics":
                return self._send(200, {"text": APP.diagnostics()})
            if u.path == "/api/import/status":
                return self._send(200, APP.import_job)
            if u.path == "/api/storage":
                rc, out = APP.priv(["list-storage"])
                return self._send(200 if rc == 0 else 500, json.loads(out) if rc == 0 else {"error": out})
        except tc.GGUFError as e:
            return self._send(400, {"error": f"Invalid GGUF: {e}"})
        except (KeyError, ValueError) as e:
            return self._send(400, {"error": str(e)})
        except Exception as e:  # noqa: BLE001 - surface to UI instead of silent failure
            return self._send(500, {"error": f"{type(e).__name__}: {e}"})
        self._send(404, {"error": "not found"})

    def do_POST(self):
        if not self._host_ok():
            return self._send(403, {"error": "bad host"})
        if not self._auth():
            return self._send(401, {"error": "invalid token"})
        p = urllib.parse.urlparse(self.path).path
        try:
            b = self._body()
            if p == "/api/settings":
                s = tc.load_settings()
                for k in ("backend", "model", "ctx", "gpu_layers", "threads", "memory_limit_mb", "idle_unload_s"):
                    if k in b:
                        s[k] = b[k]
                if "ctx_auto" in b:
                    if not isinstance(b["ctx_auto"], bool):
                        return self._send(400, {"error": "ctx_auto must be true or false."})
                    s["ctx_auto"] = b["ctx_auto"]
                if s["backend"] not in ("auto", "vulkan", "hip", "cuda", "cpu"):
                    return self._send(400, {"error": "unknown backend"})
                s["ctx"] = max(256, min(int(s["ctx"]), 1 << 20))
                for k in ("gpu_layers", "threads", "memory_limit_mb", "idle_unload_s"):
                    s[k] = int(s[k])
                    if s[k] < (-1 if k == "gpu_layers" else 0):
                        return self._send(400, {"error": f"Invalid {k}: use a nonnegative value (GPU layers also accepts -1 for automatic)."})
                if "extra_model_dirs" in b:
                    dirs = []
                    for d in b["extra_model_dirs"]:
                        d = os.path.realpath(d)
                        if not os.path.isdir(d):
                            return self._send(400, {"error": f"Not a directory: {d}"})
                        dirs.append(d)
                    s["extra_model_dirs"] = dirs
                if s["model"] and s["model"] not in [m["path"] for m in tc.list_models(s["extra_model_dirs"])]:
                    return self._send(400, {"error": "Model must be inside a configured model directory."})
                tc.save_settings(s)
                return self._send(200, {"ok": True})
            if p == "/api/llm/start":
                return self._send(200, APP.start_llm(tc.load_settings()))
            if p == "/api/llm/stop":
                APP.stop_llm()
                return self._send(200, {"ok": True})
            if p.startswith("/api/service/"):
                _, _, _, name, act = p.split("/")
                if name not in SERVICES or act not in ("start", "stop"):
                    return self._send(400, {"error": "unknown service/action"})
                return self._send(200, self.service(name, act, b))
            if p == "/api/import":
                return self._send(200, self.start_import(b))
            if p == "/api/storage/select":
                rc, out = APP.priv(["persist-mount", str(b.get("uuid", ""))])
                return self._send(200 if rc == 0 else 400, {"ok": rc == 0, "message" if rc == 0 else "error": out})
            if p == "/api/storage/temp":
                rc, out = APP.priv(["persist-temp"])
                return self._send(200 if rc == 0 else 400, {"ok": rc == 0, "message" if rc == 0 else "error": out})
            if p == "/api/storage/mount-ro":
                rc, out = APP.priv(["mount-ro", str(b.get("uuid", ""))])
                if rc == 0:
                    s = tc.load_settings()
                    if out not in s["extra_model_dirs"]:
                        s["extra_model_dirs"].append(out)
                        tc.save_settings(s)
                return self._send(200 if rc == 0 else 400, {"ok": rc == 0, "message" if rc == 0 else "error": out})
            if p == "/api/remote":
                return self._send(200, APP.set_remote(b))
            if p == "/api/remote/new-key":
                return self._send(200, {"ok": True, "api_key": APP.new_api_key(),
                                        "message": "Copy this key now - it is shown only once."})
            if p == "/api/remote/revoke":
                return self._send(200, APP.revoke_remote())
            if p == "/api/remote/tls-cert":
                host = str(b.get("host", "")).strip()
                if not re.fullmatch(r"[A-Za-z0-9.\-:]+", host):
                    return self._send(400, {"error": "Enter a hostname or IP address."})
                r = subprocess.run([os.path.join(HERE, "..", "..", "bin", "victor-tls-cert"), host], capture_output=True, text=True)
                return self._send(200 if r.returncode == 0 else 500, {"ok": r.returncode == 0, "message": r.stdout.strip() or r.stderr.strip()})
            if p == "/api/ssh":
                act = b.get("action")
                if act == "add-key":
                    rc, out = APP.priv(["ssh-add-key"], stdin=str(b.get("key", "")) + "\n")
                elif act in ("enable", "disable", "reset"):
                    args = ["ssh-" + act] + ([str(b.get("listen", "127.0.0.1"))] if act == "enable" else [])
                    rc, out = APP.priv(args)
                else:
                    return self._send(400, {"error": "unknown ssh action"})
                return self._send(200 if rc == 0 else 400, {"ok": rc == 0, "message" if rc == 0 else "error": out})
            if p == "/api/capcheck":
                r = subprocess.run([os.path.join(HERE, "..", "..", "bin", "victor-capcheck")], capture_output=True, text=True, timeout=300)
                return self._send(200, {"ok": r.returncode == 0, "output": (r.stdout + r.stderr)[-4000:]})
            if p == "/api/power":
                act = b.get("action")
                if act not in ("reboot", "poweroff"):
                    return self._send(400, {"error": "bad action"})
                rc, out = APP.priv(["power", act])
                return self._send(200, {"ok": rc == 0, "message": out})
        except tc.GGUFError as e:
            return self._send(400, {"error": f"Invalid GGUF: {e}"})
        except (KeyError, ValueError, RuntimeError) as e:
            return self._send(400, {"error": str(e)})
        except Exception as e:  # noqa: BLE001
            return self._send(500, {"error": f"{type(e).__name__}: {e}"})
        self._send(404, {"error": "not found"})

    def service(self, name, act, b):
        d = SERVICES[name]
        pr = APP.svc[name]
        if act == "stop":
            APP.stop(pr)
            return {"ok": True}
        if pr.running():
            return {"ok": True, "message": "Already running."}
        if tc.data_root() is None:
            return {"ok": False, "error": "Choose storage first."}
        if "dir" in d:
            cwd = os.path.join(HOME, d["dir"])
            if not os.path.isdir(cwd):
                return {"ok": False, "error": f"{d['label']} is not installed in this image (build it with the matching pack)."}
            cmd = [os.path.join(cwd, d["cmd"][0])] + d["cmd"][1:]
        else:
            cwd = HOME
            cmd = [shutil.which(d["bin"]) or d["bin"]] + d["cmd"][1:]
        # VRAM guard: do not silently start a second GPU consumer on top of a loaded LLM
        if d["vram_hint_gib"] and APP.llm.running() and not b.get("force"):
            for g in tc.gpus():
                if g.get("vram_total"):
                    free = (g["vram_total"] - (g.get("vram_used") or 0)) / 2**30
                    if free < d["vram_hint_gib"]:
                        return {"ok": False, "needs_force": True,
                                "error": f"Only {free:.1f} GiB VRAM free while the LLM is loaded; {d['label']} typically needs ~{d['vram_hint_gib']} GiB. "
                                         "Stop the LLM first, or start anyway."}
        # image tools keep their data on the persistent store
        env = {"VICTOR_DATA": tc.data_root()}
        try:
            APP.spawn(pr, cmd, cwd=cwd, env=env)
        except OSError as e:
            return {"ok": False, "error": str(e)}
        time.sleep(1.0)
        if not pr.running():
            return {"ok": False, "error": "Exited immediately.", "log": APP.tail(name, 30)}
        return {"ok": True}

    def start_import(self, b):
        job = APP.import_job
        if job.get("state") == "running":
            return {"ok": False, "error": "An import is already running."}
        src = str(b.get("src", ""))
        dest = tc.data_path("models")
        if not dest:
            return {"ok": False, "error": "Choose storage first."}
        sha = (b.get("sha256") or "").strip() or None

        def run():
            def prog(d, t):
                job.update(done=d, total=t)
            job.clear()
            job.update(state="running", src=src, done=0, total=0)
            try:
                path, digest = tc.import_model(src, dest, sha, prog)
                job.update(state="done", path=path, sha256=digest, verified=bool(sha))
            except (ValueError, OSError, tc.GGUFError) as e:
                job.update(state="error", error=str(e))
        threading.Thread(target=run, daemon=True).start()
        return {"ok": True}


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    global APP
    APP = App()
    srv = Server(("127.0.0.1", CC_PORT), Handler)
    print(f"VictorLinux Control Center on http://127.0.0.1:{CC_PORT}/ (token in {TOKEN_FILE})", flush=True)

    def bye(*_):
        APP.stop_llm()
        for p in APP.svc.values():
            APP.stop(p)
        os._exit(0)
    signal.signal(signal.SIGTERM, bye)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        bye()


if __name__ == "__main__":
    main()
