#!/usr/bin/env python3
"""Tori Control Center: local web UI + JSON API for managing AI services.

Listens on 127.0.0.1 only. Every API call needs the per-session token written
to ~/.cache/tori/cc.token (0600); the Host header is checked to defeat DNS
rebinding. Standard library only. Runs as the ordinary user 'tori'; the few
privileged operations go through the restricted helper `tori-priv` via sudo.
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
import toricommon as tc  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
PRIV = os.environ.get("TORI_PRIV", "/usr/local/lib/tori/tori-priv")
CC_PORT = int(os.environ.get("TORI_CC_PORT", "8787"))
UPSTREAM_PORT = 18081
HOME = os.path.expanduser("~")
TOKEN_FILE = os.path.join(os.environ.get("XDG_CACHE_HOME", os.path.join(HOME, ".cache")), "tori", "cc.token")

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
            d = "/tmp/tori-logs"
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

    FLEET_ID = re.compile(r"^[A-Za-z0-9._:-]{1,96}$")
    FLEET_ACTIONS = {"start": 0, "stop": 0, "pair": 0, "pause": 1, "resume": 1, "enable": 1, "disable": 1, "revoke": 1,
                     "approve": 1, "cancel": 1, "review": 1, "report": 1}

    def fleet(self, args, timeout=30):
        """Thin wrapper over the victor-fleet CLI (single coordinator authority). No shell; args are validated by the caller."""
        try:
            r = subprocess.run([os.environ.get("VICTOR_FLEET_BIN", "/usr/local/bin/victor-fleet"), *args], capture_output=True, text=True, timeout=timeout,
                               env=dict(os.environ, PYTHONPATH=os.environ.get("VICTOR_FLEET_LIB", "/usr/local/lib/victorfleet")))
        except (OSError, subprocess.TimeoutExpired) as e:
            return {"error": f"fleet CLI unavailable: {e}"}
        if r.returncode != 0:
            return {"error": (r.stderr or r.stdout).strip() or f"exit {r.returncode}"}
        try:
            return json.loads(r.stdout)
        except ValueError:
            return {"message": r.stdout.strip()}

    def fleet_action(self, b):
        cmd, arg = b.get("cmd"), b.get("arg")
        want = self.FLEET_ACTIONS.get(cmd)
        if want is None:
            return 400, {"error": "unknown fleet action"}
        if want and not (isinstance(arg, str) and self.FLEET_ID.match(arg)):
            return 400, {"error": "a valid device/plan id is required"}
        r = self.fleet([cmd] + ([arg] if want else []), timeout=60)
        return (400 if isinstance(r, dict) and r.get("error") else 200), r

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
        try:
            est = tc.estimate_memory(model, int(settings["ctx"]))
        except tc.GGUFError as e:
            return {"error": f"This file is not a valid GGUF model: {e}"}
        gl = tc.gpus()
        backend, reason = tc.choose_backend(settings["backend"], gl)
        if not backend:
            return {"error": reason}
        vram_free = None
        if backend != "cpu":
            for g in gl:
                if g.get("vram_total") is not None:
                    vram_free = g["vram_total"] - (g.get("vram_used") or 0)
                    break
        ram = tc.ram_info()
        fit = tc.fit_report(est, ram, vram_free if backend != "cpu" else None)
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
            if vram_free and est["total"] > vram_free * 0.92 and est.get("n_layer"):
                avail = max(0, vram_free * 0.88 - (est["kv_cache"] or 0) - est["overhead"])
                ngl = max(0, int(est["n_layer"] * avail / max(1, est["weights"])))
            else:
                ngl = 999
        return {"backend": backend, "reason": reason, "model": model, "estimate": est, "fit": fit,
                "ngl": ngl, "binary": tc.backends()[backend]}

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
                   "-c", str(int(settings["ctx"])), "-ngl", str(plan["ngl"]), "--api-key", key, "--alias", "tori-local"]
            # security-relevant flags: only passed when this llama-server build knows them
            if "--no-ui-mcp-proxy" in help_text:
                cmd.append("--no-ui-mcp-proxy")
            if "--jinja" in help_text:
                cmd.append("--jinja")
            # NOTE: '--tools' (built-in shell/file tools) is deliberately never passed.
            if int(settings.get("threads") or 0) > 0:
                cmd += ["-t", str(int(settings["threads"]))]
            idle = int(settings.get("idle_unload_s") or 0)
            if idle > 0 and "--sleep-idle-seconds" in help_text:
                cmd += ["--sleep-idle-seconds", str(idle)]
            # belt and braces: shared libs and dynamic ggml backends live next to the binary
            env = {"LD_LIBRARY_PATH": os.path.dirname(plan["binary"])}
            if plan["backend"] == "vulkan":
                env["GGML_VK_VISIBLE_DEVICES"] = os.environ.get("GGML_VK_VISIBLE_DEVICES", "")
                if not env["GGML_VK_VISIBLE_DEVICES"]:
                    env.pop("GGML_VK_VISIBLE_DEVICES")
            self.spawn(self.llm, cmd, env=env)
            self.llm_info = {"backend": plan["backend"], "reason": plan["reason"], "ngl": plan["ngl"],
                             "model": plan["model"], "estimate": plan["estimate"], "fit": plan["fit"]}
        time.sleep(1.5)
        if not self.llm.running():
            return {"ok": False, "error": "llama-server exited immediately. See the log below.", "log": self.tail("llama", 40)}
        gw = self.start_gateway(settings)
        if not gw.get("ok"):
            return {"ok": False, "error": "Model started but the API gateway did not: " + gw.get("error", "unknown"),
                    "log": self.tail("gateway", 20)}
        return {"ok": True, "plan": self.llm_info}

    def stop_llm(self):
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
            "gateway": {"running": self.gw.running()},
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
        key = "tori-" + pysecrets.token_urlsafe(32)
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
        lines = [f"Tori diagnostics {tc.now()}"]
        try:
            lines.append("Image: " + (open("/etc/tori-release").read().strip()))
        except OSError:
            lines.append("Image: (no /etc/tori-release)")
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
        lines.append(f"Selected backend: {be} - {why}")
        lines.append(f"Storage: {json.dumps(tc.persist_state())}")
        if self.llm.running():
            lines.append("Accel (from llama-server log): " + json.dumps(self.accel_report()))
        lines.append("--- llama-server log (tail) ---")
        lines.append(self.tail("llama", 40))
        lines.append("--- gateway log (tail) ---")
        lines.append(self.tail("gateway", 10))
        text = "\n".join(lines)
        return re.sub(r"(tori-[A-Za-z0-9_\-]{20,})", "tori-***", text)


APP = None


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "ToriCC"

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
        t = self.headers.get("X-Tori-Token", "")
        return hmac.compare_digest(t, APP.token)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > 1 << 20:
            raise ValueError("body too large")
        return json.loads(self.rfile.read(n) or b"{}") if n else {}

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
            if u.path == "/api/fleet":
                st = APP.fleet(["status"])
                st["plans"] = APP.fleet(["plans"]) if st.get("running") else []
                return self._send(200, st)
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
                if s["backend"] not in ("auto", "vulkan", "hip", "cuda", "cpu"):
                    return self._send(400, {"error": "unknown backend"})
                s["ctx"] = max(256, min(int(s["ctx"]), 1 << 20))
                for k in ("gpu_layers", "threads", "memory_limit_mb", "idle_unload_s"):
                    s[k] = int(s[k])
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
            if p == "/api/fleet/action":
                return self._send(*APP.fleet_action(b))
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
                r = subprocess.run([os.path.join(HERE, "..", "..", "bin", "tori-tls-cert"), host], capture_output=True, text=True)
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
                r = subprocess.run([os.path.join(HERE, "..", "..", "bin", "tori-capcheck")], capture_output=True, text=True, timeout=300)
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
        env = {"TORI_DATA": tc.data_root()}
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
    print(f"Tori Control Center on http://127.0.0.1:{CC_PORT}/ (token in {TOKEN_FILE})", flush=True)

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
