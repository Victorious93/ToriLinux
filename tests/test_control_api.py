"""Exercise the real HTTP handlers without root privileges or an inference model."""
import http.client
import json
import threading
from contextlib import contextmanager

import pytest

import control_center as cc
import gateway


@contextmanager
def serving(handler):
    server = cc.Server(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_port
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


NO_BODY = object()


def request(port, method, path, body=NO_BODY, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request(method, path, body=json.dumps(body) if body is not NO_BODY else None, headers=headers or {})
        response = conn.getresponse()
        return response.status, response.read()
    finally:
        conn.close()


@pytest.fixture
def control(tmp_path, monkeypatch):
    monkeypatch.setattr(cc, "TOKEN_FILE", str(tmp_path / "cache" / "cc.token"))
    monkeypatch.setenv("VICTOR_DATA", str(tmp_path / "data"))
    monkeypatch.setattr(cc, "APP", cc.App(), raising=False)
    with serving(cc.Handler) as port:
        yield port, {"X-Victor-Token": cc.APP.token}


@pytest.mark.parametrize("body", [[], None, "hello", 5, False])
def test_settings_requires_json_object(control, body):
    port, headers = control
    status, data = request(port, "POST", "/api/settings", body, headers)
    assert status == 400, data


@pytest.mark.parametrize("key,value", [("threads", -1), ("memory_limit_mb", -1), ("idle_unload_s", -1), ("gpu_layers", -2)])
def test_negative_runtime_settings_are_rejected(control, key, value):
    port, headers = control
    status, data = request(port, "POST", "/api/settings", {key: value}, headers)
    assert status == 400, data


def test_gateway_serves_local_chat_assets_without_exposing_unknown_posts(monkeypatch):
    class Upstream(cc.Handler):
        def do_GET(self):
            self._send(200, "chat asset", "text/javascript")

    with serving(Upstream) as upstream:
        monkeypatch.setattr(gateway.Handler, "cfg", {
            "require_auth": False, "keys": [], "upstream_port": upstream,
            "upstream_key": "private", "max_body": 1024,
        })
        with serving(gateway.Handler) as port:
            status, data = request(port, "GET", "/assets/app.js")
            assert status == 200, data
            assert data == b"chat asset"
            assert request(port, "POST", "/assets/app.js", {})[0] == 404


def test_ctx_auto_must_be_boolean_and_round_trips(control):
    port, headers = control
    assert request(port, "POST", "/api/settings", {"ctx_auto": "yes"}, headers)[0] == 400
    status, _ = request(port, "POST", "/api/settings", {"ctx_auto": False, "ctx": 8192}, headers)
    assert status == 200
    status, raw = request(port, "GET", "/api/status", headers=headers)
    assert json.loads(raw)["settings"]["ctx_auto"] is False


def test_recommendations_include_host_and_ranked_local_models(control, monkeypatch, tmp_path):
    from test_common import make_gguf
    port, headers = control
    models = tmp_path / "data" / "models"
    models.mkdir(parents=True)
    make_gguf(models / "a.gguf", pad=2000)
    monkeypatch.setattr(cc.tc, "gpus", lambda: [])
    monkeypatch.setattr(cc.tc, "backends", lambda: {"vulkan": None, "hip": None, "cuda": None, "cpu": "/cpu"})
    status, raw = request(port, "GET", "/api/recommendations", headers=headers)
    body = json.loads(raw)
    assert status == 200 and body["host"]["memory_kind"] == "system"
    assert body["local"][0]["name"] == "a.gguf" and body["local"][0]["verified"] == "untested"


def test_plan_uses_auto_context_threads_and_never_reports_gpu_on_cpu(tmp_path, monkeypatch):
    from test_common import make_gguf
    monkeypatch.setenv("VICTOR_DATA", str(tmp_path / "data"))
    (tmp_path / "data" / "models").mkdir(parents=True)
    m = tmp_path / "data" / "models" / "a.gguf"; make_gguf(m, pad=2000)
    monkeypatch.setattr(cc.tc, "gpus", lambda: [])
    monkeypatch.setattr(cc.tc, "backends", lambda: {"vulkan": None, "hip": None, "cuda": None, "cpu": "/cpu"})
    monkeypatch.setattr(cc.hostplan, "physical_threads", lambda: 8)
    app = cc.App()
    s = cc.tc.load_settings(); s["model"] = str(m)
    plan = app.plan_llm(s)
    assert plan["ngl"] == 0 and plan["backend"] == "cpu" and plan["threads"] == 7
    assert plan["ctx"] >= 2048 and plan["device"]["kind"] == "system"
    s["ctx_auto"] = False; s["ctx"] = 1234; s["threads"] = 3
    plan = app.plan_llm(s)
    assert plan["ctx"] == 1234 and plan["threads"] == 3
