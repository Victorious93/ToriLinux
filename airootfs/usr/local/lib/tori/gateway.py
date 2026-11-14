#!/usr/bin/env python3
"""ToriLinux inference gateway.

Fronts the local llama-server (which only ever listens on 127.0.0.1 with a
private key) and adds: bearer-token authentication, optional TLS, a health
endpoint and an honest capability report. It exposes inference ONLY - there is
no tool execution, file access or shell on this port.
"""
import argparse
import hashlib
import hmac
import http.client
import http.server
import json
import os
import socketserver
import ssl
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import toricommon as tc  # noqa: E402

OLLAMA_ROUTES = ("/api/version", "/api/tags", "/api/generate", "/api/chat")
ALLOWED_PREFIXES = ("/v1/", "/completion", "/completions", "/chat/completions", "/models", "/props",
                    "/tokenize", "/detokenize", "/slots", "/health", "/metrics")
# llama.cpp web UI static assets are served for GET / only when the UI is enabled
UI_PATHS_OK_GET = True
HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers",
       "transfer-encoding", "upgrade", "host", "content-length", "authorization"}


class State:
    last_request = time.time()
    lock = threading.Lock()


def capabilities(model_path=None):
    """Report only what a recorded real test proved for the CURRENT model."""
    caps = {
        "openai_compatible": True,
        "endpoints": ["/v1/models", "/v1/chat/completions", "/v1/completions", "/v1/embeddings (if model supports it)"],
        "ollama_compat": ["/api/version", "/api/tags", "/api/generate", "/api/chat"],
        "llamacpp_native": ["/completion", "/props", "/tokenize", "/detokenize"],
        "streaming": "unverified", "tool_calling": "unverified",
        "tool_execution_on_pc": False,
        "note": "Remote inference does not grant permission to execute tools on this PC. "
                "Tool calling is advertised only after `tori-capcheck` passes for the loaded model.",
    }
    p = tc.data_path("server", "capabilities.json")
    if p and os.path.exists(p) and model_path:
        try:
            with open(p) as f:
                rec = json.load(f)
            st = os.stat(model_path)
            if rec.get("model_path") == model_path and rec.get("model_size") == st.st_size \
                    and rec.get("model_mtime") == int(st.st_mtime):
                caps["streaming"] = "verified" if rec.get("streaming") else "failed"
                caps["tool_calling"] = "verified" if rec.get("tool_calling") else "failed-or-unsupported"
                caps["verified_at"] = rec.get("tested_at")
                caps["verified_server"] = rec.get("server_version")
            else:
                caps["note"] += " A previous test exists but was for a different model file."
        except (OSError, ValueError):
            pass
    return caps


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "ToriGateway"
    cfg = None

    def log_message(self, fmt, *a):   # keep logs free of tokens/URLs with queries
        sys.stderr.write("%s %s %s\n" % (tc.now(), self.client_address[0], fmt % a))

    # -- helpers
    def _json(self, code, obj, extra=None):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(b)

    def _authed(self):
        cfg = self.cfg
        if not cfg["require_auth"]:
            return True
        h = self.headers.get("Authorization", "")
        if h.lower().startswith("bearer "):
            digest = hashlib.sha256(h[7:].strip().encode()).hexdigest()
            # stored as sha256 hashes: a leaked config does not reveal usable keys
            return any(hmac.compare_digest(digest, k) for k in cfg["keys"])
        return False

    def _route(self):
        path = self.path.split("?", 1)[0]
        if path == "/tori/health":
            up = self._upstream_ok()
            return self._json(200 if up else 503, {"status": "ok" if up else "model_not_running",
                                                   "gateway": "ok", "auth_required": self.cfg["require_auth"]})
        if not self._authed():
            return self._json(401, {"error": {"message": "Missing or invalid API key. Send 'Authorization: Bearer <key>'.",
                                              "type": "authentication_error"}},
                              {"WWW-Authenticate": 'Bearer realm="tori"'})
        if path == "/tori/capabilities":
            return self._json(200, capabilities(self.cfg.get("model_path")))
        if path.startswith("/tori/"):
            return self._json(404, {"error": {"message": "unknown tori endpoint"}})
        if path in OLLAMA_ROUTES:
            return self._ollama(path)
        if path != "/" and not path.startswith(ALLOWED_PREFIXES):
            return self._json(404, {"error": {"message": "path not exposed by gateway"}})
        self._proxy()

    # ---- Ollama-compatible subset (so Ollama-only clients can use this PC) -------------------------
    # Translated onto llama-server. Deliberately NOT forwarded: tools/functions, images, model pull/push/delete/create.
    def _read_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > self.cfg["max_body"]:
            raise ValueError("request body too large")
        try:
            return json.loads(self.rfile.read(n) or b"{}") if n else {}
        except ValueError:
            raise ValueError("invalid JSON body")

    def _up(self, method, path, body=None):
        c = http.client.HTTPConnection("127.0.0.1", self.cfg["upstream_port"], timeout=600)
        c.request(method, path, body=None if body is None else json.dumps(body).encode(),
                  headers={"Authorization": "Bearer " + self.cfg["upstream_key"], "Content-Type": "application/json"})
        return c, c.getresponse()

    def _ndjson_start(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

    def _ndjson(self, obj):
        b = (json.dumps(obj) + "\n").encode()
        self.wfile.write(b"%x\r\n%s\r\n" % (len(b), b))
        self.wfile.flush()

    def _sse_events(self, r):
        buf = b""
        while True:
            chunk = r.read1(8192) if hasattr(r, "read1") else r.read(8192)
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                line = line.strip()
                if line.startswith(b"data:"):
                    data = line[5:].strip()
                    if data and data != b"[DONE]":
                        try:
                            yield json.loads(data)
                        except ValueError:
                            pass

    def _ollama(self, path):
        model = self.cfg.get("model_path")
        name = os.path.basename(model) if model else "tori-local"
        with State.lock:
            State.last_request = time.time()
        if path == "/api/version":
            return self._json(200, {"version": "tori-compat (not Ollama)"})
        if path == "/api/tags":
            size = os.path.getsize(model) if model and os.path.exists(model) else 0
            return self._json(200, {"models": [{"name": "tori-local", "model": "tori-local", "size": size,
                                                "details": {"family": "llama.cpp", "format": "gguf", "file": name}}]})
        if self.command != "POST":
            return self._json(405, {"error": "use POST"})
        try:
            b = self._read_json()
        except ValueError as e:
            return self._json(400, {"error": str(e)})
        stream = bool(b.get("stream", True))          # Ollama's default is streaming
        opts = b.get("options") or {}
        mdl = b.get("model") or "tori-local"
        try:
            if path == "/api/generate":
                up = {"prompt": str(b.get("prompt", "")), "stream": stream}
                if "num_predict" in opts: up["n_predict"] = int(opts["num_predict"])
                for k_o, k_u in (("temperature", "temperature"), ("top_p", "top_p"), ("top_k", "top_k"), ("seed", "seed")):
                    if k_o in opts: up[k_u] = opts[k_o]
                c, r = self._up("POST", "/completion", up)
            else:
                up = {"messages": b.get("messages") or [], "stream": stream}
                if "num_predict" in opts: up["max_tokens"] = int(opts["num_predict"])
                if "temperature" in opts: up["temperature"] = opts["temperature"]
                c, r = self._up("POST", "/v1/chat/completions", up)   # 'tools' intentionally dropped
        except (OSError, ValueError, TypeError):
            return self._json(503, {"error": "No model is loaded. Start the inference server from the Tori Control Center."})
        if r.status != 200:
            err = r.read()[:500].decode("utf-8", "replace")
            c.close()
            return self._json(r.status if r.status < 500 else 502, {"error": f"upstream {r.status}: {err}"})
        ts = lambda: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())  # noqa: E731
        gen = path == "/api/generate"
        try:
            if not stream:
                d = json.loads(r.read())
                if gen:
                    out = {"model": mdl, "created_at": ts(), "response": d.get("content", ""), "done": True,
                           "prompt_eval_count": d.get("tokens_evaluated"), "eval_count": d.get("tokens_predicted")}
                else:
                    u = d.get("usage") or {}
                    out = {"model": mdl, "created_at": ts(), "done": True,
                           "message": {"role": "assistant", "content": d["choices"][0]["message"].get("content") or ""},
                           "prompt_eval_count": u.get("prompt_tokens"), "eval_count": u.get("completion_tokens")}
                return self._json(200, out)
            self._ndjson_start()
            last = {}
            for ev in self._sse_events(r):
                if gen:
                    last = ev
                    self._ndjson({"model": mdl, "created_at": ts(), "response": ev.get("content", ""), "done": False})
                else:
                    ch = (ev.get("choices") or [{}])[0]
                    txt = (ch.get("delta") or {}).get("content") or ""
                    if ev.get("usage"): last = ev["usage"]
                    if txt:
                        self._ndjson({"model": mdl, "created_at": ts(), "message": {"role": "assistant", "content": txt}, "done": False})
            fin = {"model": mdl, "created_at": ts(), "done": True, "done_reason": "stop"}
            if gen:
                fin.update(response="", prompt_eval_count=last.get("tokens_evaluated"), eval_count=last.get("tokens_predicted"))
            else:
                fin.update(message={"role": "assistant", "content": ""}, prompt_eval_count=last.get("prompt_tokens"),
                           eval_count=last.get("completion_tokens"))
            self._ndjson(fin)
            self.wfile.write(b"0\r\n\r\n")
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            c.close()

    def _upstream_ok(self):
        try:
            c = http.client.HTTPConnection("127.0.0.1", self.cfg["upstream_port"], timeout=3)
            c.request("GET", "/health")
            ok = c.getresponse().status == 200
            c.close()
            return ok
        except OSError:
            return False

    def _proxy(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > self.cfg["max_body"]:
            return self._json(413, {"error": {"message": "request body too large"}})
        body = self.rfile.read(n) if n else None
        hdrs = {k: v for k, v in self.headers.items() if k.lower() not in HOP}
        hdrs["Authorization"] = "Bearer " + self.cfg["upstream_key"]
        with State.lock:
            State.last_request = time.time()
        try:
            c = http.client.HTTPConnection("127.0.0.1", self.cfg["upstream_port"], timeout=600)
            c.request(self.command, self.path, body=body, headers=hdrs)
            r = c.getresponse()
        except OSError:
            return self._json(503, {"error": {"message": "No model is loaded. Start the inference server from the "
                                                         "Tori Control Center (or it was unloaded while idle).",
                                              "type": "server_unavailable"}})
        self.send_response(r.status)
        for k, v in r.getheaders():
            if k.lower() not in HOP:
                self.send_header(k, v)
        # re-frame as chunked so streaming (SSE) passes through unbuffered
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        try:
            while True:
                chunk = r.read1(8192) if hasattr(r, "read1") else r.read(8192)
                if not chunk:
                    break
                self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
                self.wfile.flush()
            self.wfile.write(b"0\r\n\r\n")
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            c.close()

    do_GET = do_POST = do_OPTIONS = do_DELETE = do_PUT = _route


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def build_config(args):
    secrets = tc.load_secrets()
    st = tc.load_settings()
    remote = st["remote"]
    bind = args.bind or remote["bind"]
    loopback = bind in ("127.0.0.1", "::1", "localhost")
    keys = [k for k in (secrets.get("api_keys") or []) if k]
    # Auth is mandatory whenever the listener is not loopback-only, or remote access is enabled
    # (a local reverse tunnel would otherwise make remote clients look like loopback).
    require = (not loopback) or bool(remote.get("enabled"))
    if require and not keys:
        raise SystemExit("Refusing to start: remote access needs an API key. Create one in the Control Center.")
    return {
        "bind": bind, "port": args.port or remote["port"], "tls": remote.get("tls", False),
        "require_auth": require, "keys": keys,
        "upstream_port": args.upstream_port, "upstream_key": secrets.get("upstream_key", ""),
        "model_path": st.get("model") or None, "max_body": 64 << 20,
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--bind")
    ap.add_argument("--port", type=int)
    ap.add_argument("--upstream-port", type=int, default=18081)
    ap.add_argument("--cert")
    ap.add_argument("--key")
    args = ap.parse_args()
    cfg = build_config(args)
    Handler.cfg = cfg
    srv = Server((cfg["bind"], cfg["port"]), Handler)
    if cfg["tls"]:
        cert = args.cert or tc.data_path("server", "tls", "cert.pem")
        key = args.key or tc.data_path("server", "tls", "key.pem")
        if not (cert and key and os.path.exists(cert) and os.path.exists(key)):
            raise SystemExit("TLS enabled but certificate missing. Run: tori-tls-cert <hostname-or-ip>")
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_cert_chain(cert, key)
        srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    sys.stderr.write(f"{tc.now()} gateway listening on {cfg['bind']}:{cfg['port']} "
                     f"tls={cfg['tls']} auth={'required' if cfg['require_auth'] else 'off (loopback only)'}\n")
    sys.stderr.flush()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
