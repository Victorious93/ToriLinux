#!/usr/bin/env python3
"""Measure llama.cpp server prompt-cache behaviour on the EXACT binary VictorLinux builds (stdlib only).

  scripts/cache-probe.py --server BIN --model GGUF --out results.json [--ctx 8192] [--turns 6]

Every scenario sends the FULL canonical conversation each turn (never just the newest message) and records what the server
itself reports: timings.prompt_n (tokens evaluated), timings.cache_n (tokens reused), per-request wall time, the /slots view,
and whether management endpoints are reachable without the API key. Tiny models make seconds meaningless for quality; the
token accounting (prompt_n/cache_n) is what transfers. Correctness is checked by asserting replies from cached and uncached
runs are identical at temperature 0 (cache_prompt can perturb logits across batch sizes, so this is reported, not assumed).
"""
import argparse, json, os, signal, subprocess, sys, tempfile, time, urllib.error, urllib.request

KEY = "probe-key-not-a-secret"


class Server:
    def __init__(self, binary, model, ctx, parallel, extra=()):
        self.port = 18000 + (os.getpid() % 1000)
        self.args = [binary, "-m", model, "--host", "127.0.0.1", "--port", str(self.port), "-c", str(ctx), "--parallel", str(parallel),
                     "--api-key", KEY, "--no-webui", "-ngl", "0", "--threads", "4", *extra]
        self.log = tempfile.NamedTemporaryFile("w+", delete=False, suffix=".log")
        self.p = subprocess.Popen(self.args, stdout=self.log, stderr=subprocess.STDOUT, env=dict(os.environ, LD_LIBRARY_PATH=os.path.dirname(binary)))
        for _ in range(240):
            time.sleep(0.5)
            try:
                if urllib.request.urlopen(f"http://127.0.0.1:{self.port}/health", timeout=2).status == 200:
                    return
            except Exception:
                if self.p.poll() is not None:
                    raise RuntimeError("server exited: " + open(self.log.name).read()[-800:])
        raise RuntimeError("server did not become healthy")

    def stop(self):
        self.p.send_signal(signal.SIGTERM)
        try: self.p.wait(15)
        except subprocess.TimeoutExpired: self.p.kill()

    def req(self, path, body=None, key=KEY, method=None):
        r = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=None if body is None else json.dumps(body).encode(),
                                   headers={"Content-Type": "application/json", **({"Authorization": f"Bearer {key}"} if key else {})}, method=method)
        t0 = time.time()
        try:
            with urllib.request.urlopen(r, timeout=300) as resp:
                return resp.status, json.loads(resp.read() or b"{}"), time.time() - t0
        except urllib.error.HTTPError as e:
            return e.code, {}, time.time() - t0

    def chat(self, messages, extra=None, max_tokens=24):
        body = {"model": "x", "messages": messages, "temperature": 0, "max_tokens": max_tokens, "seed": 1, **(extra or {})}
        st, j, dt = self.req("/v1/chat/completions", body)
        if st != 200:
            return {"status": st}
        t = j.get("timings", {})
        return {"status": st, "text": j["choices"][0]["message"]["content"], "prompt_n": t.get("prompt_n"), "cache_n": t.get("cache_n"),
                "prompt_ms": t.get("prompt_ms"), "wall_s": round(dt, 3), "prompt_tokens": j["usage"]["prompt_tokens"]}


def convo(tag, turns, replies):
    """Canonical growing history: system + (user, assistant)* + user. `replies` are what the model actually said."""
    sys_prompt = "You are a concise assistant. " + ("Context for conversation %s: the quick brown fox jumps over the lazy dog. " % tag) * 6
    msgs = [{"role": "system", "content": sys_prompt}]
    for i in range(turns):
        msgs.append({"role": "user", "content": f"[{tag}] question number {i}: tell me one short fact about the number {i}."})
        if i < len(replies):
            msgs.append({"role": "assistant", "content": replies[i]})
    return msgs


def run_conversation(srv, tag, turns, extra=None):
    replies, rows = [], []
    for i in range(1, turns + 1):
        msgs = convo(tag, i, replies)           # FULL history every turn
        r = srv.chat(msgs, extra)
        rows.append({k: r.get(k) for k in ("prompt_tokens", "prompt_n", "cache_n", "wall_s", "status")})
        replies.append(r.get("text", ""))
    return rows, replies


def reuse(rows):
    tot = sum(r["prompt_tokens"] or 0 for r in rows); c = sum(r["cache_n"] or 0 for r in rows); n = sum(r["prompt_n"] or 0 for r in rows)
    return {"total_prompt_tokens": tot, "evaluated": n, "reused": c, "reuse_ratio": round(c / tot, 3) if tot else None}


def interleaved(srv, turns, extra_for=lambda tag: None):
    a_r, b_r, rows = [], [], {"A": [], "B": []}
    for i in range(1, turns + 1):
        for tag, rep in (("A", a_r), ("B", b_r)):
            r = srv.chat(convo(tag, i, rep), extra_for(tag))
            rows[tag].append({k: r.get(k) for k in ("prompt_tokens", "prompt_n", "cache_n", "wall_s", "status")}); rep.append(r.get("text", ""))
    return rows, (a_r, b_r)


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--server", required=True); ap.add_argument("--model", required=True)
    ap.add_argument("--out", required=True); ap.add_argument("--ctx", type=int, default=8192); ap.add_argument("--turns", type=int, default=6)
    a = ap.parse_args()
    res = {"server": a.server, "model": os.path.basename(a.model), "ctx": a.ctx, "turns": a.turns, "cpus": os.cpu_count()}
    ver = subprocess.run([a.server, "--version"], capture_output=True, text=True, env=dict(os.environ, LD_LIBRARY_PATH=os.path.dirname(a.server)))
    res["version"] = (ver.stdout + ver.stderr).strip().splitlines()[-3:]

    # 1. single conversation, default server (native prefix reuse)
    s = Server(a.server, a.model, a.ctx, 1)
    rows, base_replies = run_conversation(s, "A", a.turns)
    res["single_default"] = {"rows": rows, **reuse(rows)}
    # management endpoints vs API key
    res["endpoint_auth"] = {p: {"no_key": s.req(p, key=None)[0], "with_key": s.req(p)[0]} for p in ("/slots", "/props", "/metrics", "/health", "/v1/models")}
    res["slots_sample"] = s.req("/slots")[1][:1] if isinstance(s.req("/slots")[1], list) else s.req("/slots")[1]
    # cache_prompt:false control (same history)
    rows_off, rep_off = run_conversation(s, "A", a.turns, {"cache_prompt": False})
    res["single_cache_prompt_false"] = {"rows": rows_off, **reuse(rows_off), "replies_equal_to_cached_run": rep_off == base_replies}
    s.stop()

    # 2. cache disabled in the server (--cache-ram 0) -> still a correct chat
    s = Server(a.server, a.model, a.ctx, 1, ["--cache-ram", "0"])
    rows0, rep0 = run_conversation(s, "A", a.turns)
    res["single_cache_ram_0"] = {"rows": rows0, **reuse(rows0), "replies_equal_to_default": rep0 == base_replies}
    s.stop()

    # 3. two interleaved conversations, ONE slot (default: --cache-ram prompt cache + idle-slot saving)
    s = Server(a.server, a.model, a.ctx, 1)
    rows, _ = interleaved(s, a.turns)
    res["interleaved_1slot_default"] = {k: {"rows": v, **reuse(v)} for k, v in rows.items()}
    s.stop()
    s = Server(a.server, a.model, a.ctx, 1, ["--cache-ram", "0"])
    rows, _ = interleaved(s, a.turns)
    res["interleaved_1slot_cache_ram_0"] = {k: {"rows": v, **reuse(v)} for k, v in rows.items()}
    s.stop()

    # 4. two interleaved conversations, TWO slots: unpinned vs id_slot pinned (chat endpoint)
    s = Server(a.server, a.model, a.ctx * 2, 2)
    rows, _ = interleaved(s, a.turns)
    res["interleaved_2slot_unpinned"] = {k: {"rows": v, **reuse(v)} for k, v in rows.items()}
    s.stop()
    s = Server(a.server, a.model, a.ctx * 2, 2)
    rows, _ = interleaved(s, a.turns, lambda tag: {"id_slot": {"A": 0, "B": 1}[tag]})
    res["interleaved_2slot_id_slot_pinned"] = {k: {"rows": v, **reuse(v)} for k, v in rows.items()}
    sl = s.req("/slots")[1]
    res["slots_after_pinned"] = [{"id": x.get("id"), "is_processing": x.get("is_processing"), "n_ctx": x.get("n_ctx")} for x in sl] if isinstance(sl, list) else sl
    # id_slot out of range wraps (documented in source) and a bogus type is handled
    res["id_slot_out_of_range"] = {k: s.chat(convo("Z", 1, []), {"id_slot": v}).get("status") for k, v in (("5", 5), ("-1", -1))}
    s.stop()

    # 5. restart: cache gone, conversation (full history) still answers identically
    s = Server(a.server, a.model, a.ctx, 1)
    r_before = s.chat(convo("A", a.turns, base_replies[:a.turns - 1]))
    s.stop()
    s = Server(a.server, a.model, a.ctx, 1)
    r_after = s.chat(convo("A", a.turns, base_replies[:a.turns - 1]))
    res["restart"] = {"before": {k: r_before.get(k) for k in ("prompt_n", "cache_n")}, "after_restart": {k: r_after.get(k) for k in ("prompt_n", "cache_n")},
                      "same_text": r_before.get("text") == r_after.get("text")}
    # 6. busy slot + id_slot: a second request pinned to a busy slot waits (head-of-line) -- measure
    import threading
    out = {}
    def go(name, extra):
        t0 = time.time(); r = s.chat(convo("L", 3, ["x", "y"]), extra, max_tokens=120); out[name] = round(time.time() - t0, 2)
    s.stop()
    s = Server(a.server, a.model, a.ctx * 2, 2)
    for label, extra in (("unpinned", None), ("pinned_same_slot", {"id_slot": 0})):
        ts = [threading.Thread(target=go, args=(f"{label}_{i}", extra)) for i in range(2)]
        t0 = time.time(); [t.start() for t in ts]; [t.join() for t in ts]; out[f"{label}_total"] = round(time.time() - t0, 2)
    res["concurrent_two_requests_2slots"] = out
    s.stop()
    json.dump(res, open(a.out, "w"), indent=2)
    print(json.dumps({k: v for k, v in res.items() if k.startswith(("single", "interleaved", "restart", "endpoint"))}, indent=1)[:3000])


if __name__ == "__main__":
    main()
