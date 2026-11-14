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
