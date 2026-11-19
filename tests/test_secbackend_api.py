"""HTTP-contract tests for secbackend.py's service layer, with a FAKE executor (no real
tool invoked) so these run everywhere, fast. Real-tool coverage lives in test_secmsf.py /
test_secset.py (live, tool-gated) and the end-to-end CLI run recorded in
docs/security-backend.md#full-service-verification (actually exercised against a real
msfrpcd and a real setoolkit on 2026-11-19 - this file is not a substitute for that, it is
the fast/always-on complement to it).
"""
import json
import threading
import time
from http.client import HTTPConnection

import pytest

import secbackend
import secjobs
import secprotocol as proto


class FakeExecutors:
    def __init__(self):
        self.calls = []

    def capabilities(self):
        return {"protocol": {"major": proto.PROTOCOL_MAJOR}, "operations": {
            "metasploit.module": {"available": False, "reason": "fake"},
            "setoolkit.attack": {"available": True, "supported_vectors": ["QRCODE_GENERATOR"]},
        }}

    def run(self, job):
        self.calls.append(job.job_id)
        job.append_output("fake output\n")
        if job.params.get("target_host") == "FAIL_ME":
            raise proto.ProtocolError("internal", "synthetic failure")
        return {"summary": "fake ok"}


@pytest.fixture
def server(tmp_path, monkeypatch):
    monkeypatch.setenv("VICTOR_DATA", str(tmp_path))
    import victorcommon as vc
    sec = vc.load_secrets()
    sec[secbackend.SECRETS_KEY_LIST] = []
    sec[secbackend.SECRETS_KEY_META] = {}
    vc.save_secrets(sec)
    key, pub = secbackend.create_security_key("test")

    ex = FakeExecutors()
    secbackend.Handler.executors = ex
    secbackend.Handler.store = secjobs.JobStore(ex.run)
    srv = secbackend.Server(("127.0.0.1", 0), secbackend.Handler)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield port, key, ex
    finally:
        srv.shutdown()


def _req(port, method, path, key=None, body=None):
    conn = HTTPConnection("127.0.0.1", port, timeout=10)
    headers = {}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    data = json.dumps(body).encode() if body is not None else None
    conn.request(method, path, body=data, headers=headers)
    resp = conn.getresponse()
    out = json.loads(resp.read())
    conn.close()
    return resp.status, out


def test_health_needs_no_auth(server):
    port, _key, _ex = server
    status, out = _req(port, "GET", "/security/v1/health")
    assert status == 200 and out == {"status": "ok"}


def test_capabilities_requires_auth(server):
    port, _key, _ex = server
    status, out = _req(port, "GET", "/security/v1/capabilities")
    assert status == 401
    assert out["error"]["code"] == "unauthorized"


def test_capabilities_with_valid_key(server):
    port, key, _ex = server
    status, out = _req(port, "GET", "/security/v1/capabilities", key=key)
    assert status == 200
    assert out["operations"]["setoolkit.attack"]["available"] is True


def test_wrong_key_rejected(server):
    port, _key, _ex = server
    status, out = _req(port, "GET", "/security/v1/capabilities", key="victor-totally-made-up")
    assert status == 401


def test_submit_and_poll_job(server):
    port, key, ex = server
    body = {"op": "setoolkit.attack", "idempotency_key": "a1",
            "params": {"attack_vector": "QRCODE_GENERATOR", "target_host": "https://example.com"}}
    status, out = _req(port, "POST", "/security/v1/jobs", key=key, body=body)
    assert status == 202
    job_id = out["job_id"]
    for _ in range(50):
        status, out = _req(port, "GET", f"/security/v1/jobs/{job_id}", key=key)
        if out["state"] in proto.TERMINAL:
            break
        time.sleep(0.05)
    assert out["state"] == "completed"
    assert out["result"] == {"summary": "fake ok"}
    assert ex.calls == [job_id]


def test_submit_rejects_unknown_op(server):
    port, key, _ex = server
    status, out = _req(port, "POST", "/security/v1/jobs", key=key,
                        body={"op": "nope", "idempotency_key": "a2", "params": {}})
    assert status == 404
    assert out["error"]["code"] == "unknown_op"


def test_job_isolated_by_owner(server, tmp_path, monkeypatch):
    port, key, _ex = server
    body = {"op": "setoolkit.attack", "idempotency_key": "a3",
            "params": {"attack_vector": "QRCODE_GENERATOR", "target_host": "https://example.com"}}
    status, out = _req(port, "POST", "/security/v1/jobs", key=key, body=body)
    job_id = out["job_id"]

    key2, _ = secbackend.create_security_key("someone-else")
    status, out = _req(port, "GET", f"/security/v1/jobs/{job_id}", key=key2)
    assert status == 403
    assert out["error"]["code"] == "not_job_owner"


def test_cancel_unknown_job_is_404(server):
    port, key, _ex = server
    status, out = _req(port, "POST", "/security/v1/jobs/job_nope/cancel", key=key)
    assert status == 404


def test_failed_job_reports_structured_error(server):
    port, key, _ex = server
    body = {"op": "setoolkit.attack", "idempotency_key": "a4",
            "params": {"attack_vector": "QRCODE_GENERATOR", "target_host": "FAIL_ME"}}
    status, out = _req(port, "POST", "/security/v1/jobs", key=key, body=body)
    job_id = out["job_id"]
    for _ in range(50):
        status, out = _req(port, "GET", f"/security/v1/jobs/{job_id}", key=key)
        if out["state"] in proto.TERMINAL:
            break
        time.sleep(0.05)
    assert out["state"] == "failed"
    assert out["error"]["code"] == "internal"
