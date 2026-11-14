#!/usr/bin/env python3
"""End-to-end test: control center -> llama-server (real binary) -> gateway.

Usage: TORI_LLAMA_ROOT=<dir with cpu/bin/llama-server> live_test.py MODEL.gguf
Needs no GPU. Proves the API/auth/streaming/capability plumbing with real inference
on whatever backend is present (CPU here). It does NOT prove GPU acceleration.
"""
import http.client, json, os, shutil, subprocess, sys, tempfile, time

model = os.path.abspath(sys.argv[1])
tmp = tempfile.mkdtemp(prefix="toritest")
env = dict(os.environ, HOME=tmp, TORI_DATA=os.path.join(tmp, "data"), TORI_RUN_DIR=os.path.join(tmp, "run"),
           TORI_CC_PORT="18787", PYTHONUNBUFFERED="1")
os.makedirs(env["TORI_DATA"] + "/models")
root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "airootfs", "usr", "local")
cc = subprocess.Popen([sys.executable, "-I", f"{root}/lib/tori/control_center.py"], env=env,
                      stdout=open(tmp + "/cc.out", "w"), stderr=subprocess.STDOUT)
fails = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))
    if not cond:
        fails.append(name)


def call(port, method, path, body=None, headers=None, tls=False):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=120)
    h = dict(headers or {})
    if body is not None:
        h["Content-Type"] = "application/json"
    c.request(method, path, json.dumps(body) if body is not None else None, h)
    r = c.getresponse()
    data = r.read()
    try:
        return r.status, json.loads(data)
    except ValueError:
        return r.status, data


try:
    for _ in range(50):
        try:
            call(18787, "GET", "/"); break
        except OSError:
            time.sleep(0.2)
    tok = open(os.path.join(tmp, ".cache/tori/cc.token")).read()
    T = {"X-Tori-Token": tok}
    check("cc: API rejects missing token", call(18787, "GET", "/api/status")[0] == 401)
    check("cc: bad Host header rejected (DNS rebinding)", call(18787, "GET", "/", headers={"Host": "evil.example"})[0] == 403)
    shutil.copy(model, env["TORI_DATA"] + "/models/")
    mpath = env["TORI_DATA"] + "/models/" + os.path.basename(model)
    s, st = call(18787, "GET", "/api/status", headers=T)
    check("cc: status ok", s == 200 and st["backend"]["selected"] == "cpu", str(st)[:300])
    check("cc: CPU fallback explained", "CPU" in st["backend"]["reason"] or "cpu" in st["backend"]["reason"].lower())
    s, ms = call(18787, "GET", "/api/models", headers=T)
    check("cc: model listed", len(ms["models"]) == 1)
    s, e = call(18787, "GET", f"/api/estimate?path={mpath}&ctx=2048", headers=T)
    check("cc: memory estimate", s == 200 and e["estimate"]["total"] > 0, str(e)[:200])
    s, r = call(18787, "GET", f"/api/estimate?path=/etc/passwd&ctx=2048", headers=T)
    check("cc: estimate refuses arbitrary paths", s == 400)
    s, r = call(18787, "POST", "/api/settings", {"model": mpath, "ctx": 2048}, T)
    check("cc: save settings", s == 200, str(r))
    s, r = call(18787, "POST", "/api/llm/start", {}, T)
    check("cc: start llm", r.get("ok") is True, str(r)[:500])
    for _ in range(60):
        if call(8080, "GET", "/tori/health")[0] == 200:
            break
        time.sleep(1)
    s, h = call(8080, "GET", "/tori/health")
    check("gw: health ok, loopback no auth required", s == 200 and h["auth_required"] is False, str(h))
    s, r = call(8080, "POST", "/v1/chat/completions", {"model": "x", "max_tokens": 8, "messages": [{"role": "user", "content": "Once upon a time"}]})
    check("gw: real local completion via gateway", s == 200 and r["choices"][0]["message"] is not None, str(r)[:300])
    # streaming
    c = http.client.HTTPConnection("127.0.0.1", 8080, timeout=60)
    c.request("POST", "/v1/chat/completions", json.dumps({"stream": True, "max_tokens": 8, "messages": [{"role": "user", "content": "Hi"}]}), {"Content-Type": "application/json"})
    raw = c.getresponse().read().decode()
    check("gw: SSE streaming passes through", raw.count("data:") >= 3 and "[DONE]" in raw, raw[:200])
    # Ollama-compatible and llama.cpp-native routes (real model)
    s_, v = call(8080, "GET", "/api/version"); check("gw: ollama /api/version", s_ == 200, str(v))
    s_, tg = call(8080, "GET", "/api/tags"); check("gw: ollama /api/tags lists model", s_ == 200 and tg["models"][0]["name"] == "tori-local", str(tg))
    s_, g = call(8080, "POST", "/api/generate", {"model": "x", "prompt": "Once upon a time", "stream": False, "options": {"num_predict": 8}})
    check("gw: ollama /api/generate (non-stream, real inference)", s_ == 200 and g.get("done") is True and isinstance(g.get("response"), str) and g.get("eval_count"), str(g)[:300])
    c = http.client.HTTPConnection("127.0.0.1", 8080, timeout=60)
    c.request("POST", "/api/generate", json.dumps({"prompt": "Once", "options": {"num_predict": 6}}), {"Content-Type": "application/json"})
    lines = [json.loads(l) for l in c.getresponse().read().decode().splitlines() if l.strip()]
    check("gw: ollama /api/generate streams NDJSON ending done:true", len(lines) >= 3 and lines[-1]["done"] is True and lines[0]["done"] is False, str(lines)[:300])
    s_, ch = call(8080, "POST", "/api/chat", {"model": "x", "messages": [{"role": "user", "content": "Hi"}], "stream": False, "options": {"num_predict": 6}, "tools": [{"type": "function"}]})
    check("gw: ollama /api/chat works and ignores tools", s_ == 200 and ch.get("done") is True and "message" in ch, str(ch)[:300])
    s_, cm = call(8080, "POST", "/completion", {"prompt": "Once", "n_predict": 4, "stream": False})
    check("gw: llama.cpp native /completion", s_ == 200 and "content" in cm, str(cm)[:200])
    check("gw: unexposed path blocked", call(8080, "GET", "/api/anything", headers={})[0] == 404)
    s, caps = call(8080, "GET", "/tori/capabilities")
    check("caps: unverified before test", caps["tool_calling"] == "unverified" and caps["streaming"] == "unverified", str(caps))
    check("caps: never claims tool execution", caps["tool_execution_on_pc"] is False)
    # remote requires key
    s, r = call(18787, "POST", "/api/remote", {"enabled": True, "bind": "127.0.0.1"}, T)
    s, r = call(18787, "POST", "/api/remote", {"enabled": True, "bind": "0.0.0.0", "port": 8080}, T)
    check("cc: remote refused without API key", s == 200 and r.get("ok") is False, str(r))
    s, k = call(18787, "POST", "/api/remote/new-key", {}, T)
    key = k["api_key"]
    s, r = call(18787, "POST", "/api/remote", {"enabled": True, "bind": "127.0.0.1"}, T)
    check("cc: loopback never counts as remote", r["remote"]["enabled"] is False)
    s, r = call(18787, "POST", "/api/remote", {"enabled": True, "bind": "0.0.0.0", "port": 8080}, T)
    check("cc: remote enabled with key", r.get("ok") is True, str(r))
    time.sleep(2)
    check("gw: no key -> 401", call(8080, "POST", "/v1/chat/completions", {"messages": []})[0] == 401)
    check("gw: ollama route also needs key when remote", call(8080, "POST", "/api/generate", {"prompt": "x"})[0] == 401)
    check("gw: wrong key -> 401", call(8080, "GET", "/v1/models", headers={"Authorization": "Bearer nope"})[0] == 401)
    s, r = call(8080, "GET", "/v1/models", headers={"Authorization": "Bearer " + key})
    check("gw: right key -> 200", s == 200, str(r)[:200])
    check("gw: health open but minimal", call(8080, "GET", "/tori/health")[0] == 200)
    sec = json.load(open(env["TORI_DATA"] + "/config/secrets.json"))
    check("secrets: API key stored hashed, not plaintext", key not in json.dumps(sec) and oct(os.stat(env["TORI_DATA"] + "/config/secrets.json").st_mode)[-3:] == "600")
    # capability test (tiny model is expected NOT to pass tool calling)
    out = subprocess.run([sys.executable, "-I", f"{root}/bin/tori-capcheck"], env=env, capture_output=True, text=True)
    caps_file = json.load(open(env["TORI_DATA"] + "/server/capabilities.json"))
    check("capcheck: streaming verified by real test", caps_file["streaming"] is True, out.stdout[-500:] + out.stderr[-300:])
    print("INFO tool_calling result for this model:", caps_file["tool_calling"])
    s, caps = call(8080, "GET", "/tori/capabilities", headers={"Authorization": "Bearer " + key})
    check("caps: reflects recorded test", caps["streaming"] == "verified" and caps["tool_calling"] in ("verified", "failed-or-unsupported"), str(caps))
    s, r = call(18787, "POST", "/api/remote/revoke", {}, T)
    time.sleep(2)
    check("revoke: gateway back to loopback, no keys", call(8080, "GET", "/tori/health")[1].get("auth_required") is False)
    check("revoke: old key dead on remote reenable", json.load(open(env["TORI_DATA"] + "/config/secrets.json"))["api_keys"] == [])
    s, d = call(18787, "GET", "/api/diagnostics", headers=T)
    check("diag: produced, no key leak", s == 200 and key not in d["text"] and "Selected backend" in d["text"])
    log = call(18787, "GET", "/api/logs?name=llama", headers=T)[1]["log"]
    check("log: llama-server flags include no --tools, has no-mcp-proxy", "--tools" not in log.split("\n")[0] and "no-ui-mcp-proxy" in log.split("\n")[0], log[:300])
    call(18787, "POST", "/api/llm/stop", {}, T)
finally:
    cc.terminate()
    time.sleep(1)
    subprocess.run(["pkill", "-f", "llama-server"])
print("\nFAILED: %s" % fails if fails else "\nALL PASSED")
sys.exit(1 if fails else 0)
