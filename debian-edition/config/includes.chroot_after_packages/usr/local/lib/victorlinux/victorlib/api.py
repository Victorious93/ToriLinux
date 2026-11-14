"""OpenAI-compatible endpoint (llama-server): connection info, capability verification, self-test."""
import hashlib, json, os, ssl, time, urllib.request, urllib.error
from .common import *
from . import llama


def _base(c):
    scheme = "https" if (c["remote"]["enabled"] and c["remote"]["tls"]) else "http"
    host = "127.0.0.1" if c["bind"] in ("0.0.0.0", "::") else c["bind"]
    return f"{scheme}://{host}:{c['port']}"


def _ctx(c):
    if c["remote"]["enabled"] and c["remote"]["tls"]:
        cert, _ = llama.tls_paths()
        return ssl.create_default_context(cafile=cert)  # PIN to our own cert; verification stays on
    return None


def _req(c, path, key=None, body=None, timeout=60, stream=False):
    h = {"Content-Type": "application/json"}
    if key:
        h["Authorization"] = f"Bearer {key}"
    rq = urllib.request.Request(_base(c) + path, data=json.dumps(body).encode() if body is not None else None, headers=h)
    # never route checks of our own endpoint through any configured proxy
    ctx = _ctx(c)
    handlers = [urllib.request.ProxyHandler({})] + ([urllib.request.HTTPSHandler(context=ctx)] if ctx else [])
    return urllib.request.build_opener(*handlers).open(rq, timeout=timeout)


def connection_info(reveal_key=False):
    c = llama.load_config()
    remote = c["remote"]["enabled"]
    info = {"base_url": _base(c) + "/v1", "health_url": _base(c) + "/health", "auth": "Bearer API key" if remote else "none (loopback only)",
            "tls": bool(remote and c["remote"]["tls"]), "tls_cert_sha256": llama.cert_fingerprint() if remote else None,
            "scope": "LAN/VPN" if remote else "localhost only",
            "droidcommand_assumption": "Configure DroidCommand-AI with an OpenAI-compatible provider: base URL + API key. "
                                       "No pairing protocol is implemented or assumed here."}
    if remote and os.path.exists(llama.key_path()):
        info["api_key"] = open(llama.key_path()).read().strip() if reveal_key else "<hidden; run 'victor-ctl api connection --reveal-key'>"
    return info


def _fingerprint(c, props):
    mp = os.path.join(data_path("models"), c["model"])
    st = os.stat(mp)
    tmpl = hashlib.sha256((props.get("chat_template") or "").encode()).hexdigest()[:16]
    commit = ""
    try:
        commit = dict(l.strip().split("=", 1) for l in open("/usr/share/victorlinux/llamacpp.build-info") if "=" in l)["llama.cpp_commit"]
    except Exception:
        pass
    return f"{c['model']}|{st.st_size}|{int(st.st_mtime)}|tmpl={tmpl}|llama={commit[:12]}"


def capabilities():
    c = llama.load_config()
    store = read_json(settings_file("capabilities.json"), {})
    cap = {"models_endpoint": "untested", "chat_completions": "untested", "streaming": "untested", "tool_calling": "untested"}
    try:
        cap.update(store.get(_fingerprint(c, _props(c)), {}))
    except Exception:
        pass
    return cap


def _props(c):
    key = open(llama.key_path()).read().strip() if c["remote"]["enabled"] else None
    with _req(c, "/props", key, timeout=15) as r:
        return json.load(r)


def selftest(test_tools=False):
    """Real requests against the running server. Results are recorded per exact model+template+build."""
    c = llama.load_config()
    if not llama.is_running():
        raise VictorError("The inference service is not running. Start it first.")
    remote = c["remote"]["enabled"]
    key = open(llama.key_path()).read().strip() if remote else None
    res, cap = [], {}

    def step(name, fn):
        t = time.time()
        try:
            ok, detail = fn()
        except Exception as e:
            ok, detail = False, f"{type(e).__name__}: {e}"
        res.append({"check": name, "ok": ok, "detail": detail, "seconds": round(time.time() - t, 2)})
        return ok

    def health():
        with _req(c, "/health", None, timeout=30) as r:
            return r.status == 200, f"HTTP {r.status}"

    def auth_enforced():
        if not remote:
            return True, "skipped (loopback-only mode, no key required)"
        try:
            _req(c, "/v1/models", None, timeout=15)
            return False, "request WITHOUT a key was accepted"
        except urllib.error.HTTPError as e:
            return e.code == 401, f"HTTP {e.code} without key"

    def models_ep():
        with _req(c, "/v1/models", key) as r:
            d = json.load(r)
        return bool(d.get("data")), f"{len(d.get('data', []))} model(s): " + ",".join(m["id"] for m in d.get("data", []))

    body = {"model": c["model"], "messages": [{"role": "user", "content": "Say hi."}], "max_tokens": 8, "temperature": 0}

    def chat():
        with _req(c, "/v1/chat/completions", key, body, timeout=300) as r:
            d = json.load(r)
        return bool(d["choices"][0]["message"].get("content") is not None), f"usage={d.get('usage')}"

    def stream():
        b = dict(body, stream=True)
        n, done = 0, False
        with _req(c, "/v1/chat/completions", key, b, timeout=300) as r:
            for line in r:
                line = line.decode().strip()
                if line.startswith("data: "):
                    if line[6:] == "[DONE]":
                        done = True
                    else:
                        n += 1
        return n > 1 and done, f"{n} SSE chunks, [DONE]={done}"

    step("health", health)
    step("auth_enforced_for_remote", auth_enforced)
    cap["models_endpoint"] = "verified" if step("models list", models_ep) else "failed"
    cap["chat_completions"] = "verified" if step("chat completion", chat) else "failed"
    cap["streaming"] = "verified" if step("streaming SSE", stream) else "failed"
    if test_tools:
        def tools():
            if not c["jinja"]:
                return False, "server started without --jinja; enable 'Chat template (jinja)' in Models"
            tdef = [{"type": "function", "function": {"name": "get_time", "description": "Return the current time",
                     "parameters": {"type": "object", "properties": {"tz": {"type": "string"}}, "required": ["tz"]}}}]
            b = {"model": c["model"], "messages": [{"role": "user", "content": "What time is it in UTC? Use the tool."}],
                 "tools": tdef, "tool_choice": "required", "max_tokens": 128, "temperature": 0}
            with _req(c, "/v1/chat/completions", key, b, timeout=300) as r:
                d = json.load(r)
            tc = d["choices"][0]["message"].get("tool_calls") or []
            if not tc:
                return False, "model returned no tool call"
            args = json.loads(tc[0]["function"]["arguments"])
            return tc[0]["function"]["name"] == "get_time" and "tz" in args, f"tool_call={tc[0]['function']}"
        cap["tool_calling"] = "verified" if step("tool calling", tools) else "failed"
    # NOTE: a tool_call RESPONSE never authorises execution; nothing in VictorLinux executes tool calls.
    try:
        fp = _fingerprint(c, _props(c))
        store = read_json(settings_file("capabilities.json"), {})
        store.setdefault(fp, {}).update({k: v for k, v in cap.items() if not (k == "tool_calling" and not test_tools)})
        store[fp]["tested_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        write_json_atomic(settings_file("capabilities.json"), store, 0o644)
    except Exception as e:
        res.append({"check": "record result", "ok": False, "detail": str(e), "seconds": 0})
    return {"results": res, "capabilities": cap, "all_ok": all(r["ok"] for r in res)}
