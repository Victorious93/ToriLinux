"""Named API keys (expiry, limits, usage log, live revoke) and opt-in phone pairing that never puts a key in the QR."""
import base64
import json
import os
import subprocess
import time

import pytest

import apikeys
import control_center as cc
import gateway
import qrsvg
import victorcommon as tc
from test_control_api import control, request, serving  # noqa: F401  (fixture reuse)


def test_everything_network_related_is_off_by_default(tmp_path, monkeypatch):
    monkeypatch.setenv("VICTOR_DATA", str(tmp_path / "data"))
    s = tc.load_settings()
    assert s["remote"] == {"enabled": False, "bind": "127.0.0.1", "port": 8080, "tls": False}
    assert apikeys.pairing_state(tc.load_secrets()) == {"active": False}
    monkeypatch.setattr(cc, "TOKEN_FILE", str(tmp_path / "cc.token"))
    st = cc.App().keys_status()
    assert st["pairing"] == {"active": False} and st["lan_discovery"] is False and st["keys"] == []
    assert st["pair_ready"]["remote"] is False and st["pair_ready"]["tls"] is False
    cfg = gateway.build_config(type("A", (), {"bind": None, "port": None, "upstream_port": 18081})())
    assert cfg["bind"] == "127.0.0.1" and cfg["require_auth"] is False


def test_key_lifecycle_expiry_and_migration():
    sec = {"api_keys": ["a" * 64]}
    assert apikeys.listing(sec)[0]["name"].startswith("Key created before")
    key, pub = apikeys.create(sec, "Laptop", expires_days=1, rpm=5, now=1000)
    assert key.startswith("victor-") and key not in json.dumps(sec)               # only the hash is stored
    assert apikeys.check(sec, key, now=1001)[1]["name"] == "Laptop"
    assert apikeys.check(sec, key, now=1000 + 86400)[2] == "expired"
    assert apikeys.check(sec, "victor-wrong")[2] == "invalid" and apikeys.check(sec, "")[2] == "missing"
    with pytest.raises(ValueError):
        apikeys.create(sec, "<script>")
    with pytest.raises(ValueError):
        apikeys.create(sec, "x", expires_days=-1)
    apikeys.revoke(sec, pub["id"])
    assert apikeys.check(sec, key)[2] == "invalid"
    rl = apikeys.RateLimiter()
    assert [rl.allow("k", 2, now=t) for t in (0, 1, 2, 61)] == [True, True, False, True]


def test_pair_uri_format_and_no_key():
    sec = {}
    key, _ = apikeys.create(sec, "Main")
    tok, exp = apikeys.start_pairing(sec, now=1000)
    assert len(tok) == 22 and len(base64.urlsafe_b64decode(tok + "==")) == 16 and exp == 1600
    uri = apikeys.pair_uri(apikeys.profile_id(sec), "Don's PC+1", "https://192.168.1.42:8443", "ab" * 32, tok, exp)
    assert uri.startswith("victorpc://pair?v=1&id=") and "%2B" in uri and "+" not in uri
    f = apikeys.parse_uri(uri)
    assert set(f) == {"v", "id", "n", "gw", "gfp", "t", "x"}
    assert f["gw"] == "https://192.168.1.42:8443" and f["gfp"].startswith("SHA256:") and f["t"] == tok and f["x"] == "1600"
    assert f["n"] == "Don's PC+1" and len(f["id"]) == 12
    for secret in [key, key[7:]] + sec["api_keys"]:
        assert secret not in uri                                                     # no long-lived key, not even its hash
    assert "victor-" not in uri
    with pytest.raises(ValueError):
        apikeys.parse_uri(uri + "&t=again")
    with pytest.raises(ValueError):
        apikeys.pair_uri("ABCDEFGHIJKL", "n", "http://192.168.1.42:8080", "ab" * 32, tok, exp)   # plain HTTP refused


def test_redeem_single_use_and_attempt_limit():
    sec = {}
    tok, _ = apikeys.start_pairing(sec, now=100)
    for _ in range(3):
        with pytest.raises(apikeys.PairError):
            apikeys.redeem(sec, "AAAAAAAAAAAAAAAAAAAAAA", "Pixel", now=101)
    with pytest.raises(apikeys.PairError, match="none"):
        apikeys.redeem(sec, tok, "Pixel", now=102)                                   # window closed after 3 bad tokens
    tok, _ = apikeys.start_pairing(sec, now=200)
    with pytest.raises(apikeys.PairError, match="expired"):
        apikeys.redeem(sec, tok, "Pixel", now=200 + apikeys.PAIR_TTL)
    tok, _ = apikeys.start_pairing(sec, now=300)
    key, pub = apikeys.redeem(sec, tok, "Pixel 8<script>", now=301)
    assert pub["name"] == "Phone: Pixel 8script" and apikeys.check(sec, key)[1]
    with pytest.raises(apikeys.PairError):
        apikeys.redeem(sec, tok, "Pixel", now=302)                                   # single use


def test_qr_encoder_decodes():
    cv2 = pytest.importorskip("cv2")
    np = pytest.importorskip("numpy")
    det = cv2.QRCodeDetector()
    for text in ("hi", "victorpc://pair?v=1&id=7Q2M4KX9TB5N&n=VictorLinux&gw=https://192.168.1.42:8443&gfp=SHA256:"
                 "gHC80Ray5GnftiotQW/JWkdaUZrT2zhJoAgvVkpna9w&t=AAECAwQFBgcICQoLDA0ODw&x=1762755600", "z" * 400):
        m = qrsvg.matrix(text)
        n = len(m)
        a = np.full(((n + 8) * 8,) * 2, 255, np.uint8)
        for r in range(n):
            for c in range(n):
                if m[r][c]:
                    a[(r + 4) * 8:(r + 5) * 8, (c + 4) * 8:(c + 5) * 8] = 0
        assert det.detectAndDecode(a)[0] == text
    assert qrsvg.svg("hi").startswith("<svg") and "script" not in qrsvg.svg("hi")


def _cert(d):
    d.mkdir(parents=True, exist_ok=True)
    subprocess.run(["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes", "-keyout",
                    str(d / "key.pem"), "-out", str(d / "cert.pem"), "-days", "2", "-subj", "/CN=victorlinux"], check=True, capture_output=True)


def test_pairing_api_and_gateway_redeem(control, tmp_path, monkeypatch):  # noqa: F811
    port, headers = control
    st, raw = request(port, "POST", "/api/keys", {"action": "pair-start", "host": "192.168.1.42"}, headers)
    assert st == 400 and b"Pairing stays off" in raw                                # off until remote + TLS are set up
    st, raw = request(port, "POST", "/api/keys", {"action": "create", "name": "Laptop", "expires_days": 0, "rpm": 2}, headers)
    laptop = json.loads(raw)["api_key"]
    assert json.loads(request(port, "GET", "/api/keys", None, headers)[1])["keys"][0]["name"] == "Laptop"
    assert laptop not in request(port, "GET", "/api/keys", None, headers)[1].decode()  # never shown again
    _cert(tmp_path / "data" / "server" / "tls")
    assert json.loads(request(port, "POST", "/api/remote", {"enabled": True, "bind": "192.168.1.42", "port": 8443, "tls": True}, headers)[1])["ok"]
    st, raw = request(port, "POST", "/api/keys", {"action": "pair-start", "host": "192.168.1.42"}, headers)
    assert st == 200, raw
    j = json.loads(raw)
    f = apikeys.parse_uri(j["uri"])
    assert f["gw"] == "https://192.168.1.42:8443" and laptop not in raw.decode() and "victor-" not in j["uri"]
    assert f["gfp"] == apikeys.fingerprint_b64(j["fingerprint_sha256"]) and "<svg" in j["svg"]
    # the phone redeems the token at the gateway (loopback here so the test needs no TLS client)
    monkeypatch.setattr(gateway.Handler, "cfg", {"require_auth": True, "keys": [], "upstream_port": 1, "upstream_key": "U", "max_body": 1024,
                                                 "tls": False, "bind": "127.0.0.1", "model_path": None})
    monkeypatch.setattr(gateway.State, "usage", apikeys.UsageLog(str(tmp_path / "data" / "logs" / "api-usage.jsonl")))
    gateway.State.sec_cache = {"mtime": None, "sec": {}}
    with serving(gateway.Handler) as gp:
        st, raw = request(gp, "POST", "/victor/pair", {"v": 1, "t": f["t"], "device": "Pixel 8"})
        assert st == 200, raw
        phone = json.loads(raw)["api_key"]
        assert phone.startswith("victor-") and phone != laptop
        assert request(gp, "POST", "/victor/pair", {"v": 1, "t": f["t"], "device": "Pixel 8"})[0] == 403    # single use
        auth = {"Authorization": "Bearer " + phone}
        assert request(gp, "GET", "/victor/capabilities", None, auth)[0] == 200
        lap = {"Authorization": "Bearer " + laptop}
        assert [request(gp, "GET", "/victor/capabilities", None, lap)[0] for _ in range(3)] == [200, 200, 429]   # per-key limit
        kid = [k for k in json.loads(request(port, "GET", "/api/keys", None, headers)[1])["keys"] if k["name"] == "Phone: Pixel 8"][0]["id"]
        time.sleep(0.01)
        assert request(port, "POST", "/api/keys", {"action": "revoke", "id": kid}, headers)[0] == 200
        assert request(gp, "GET", "/victor/capabilities", None, auth)[0] == 401                                  # revoked: immediate
    log = open(tmp_path / "data" / "logs" / "api-usage.jsonl").read()
    assert "Laptop" in log and "Phone: Pixel 8" in log and laptop not in log and phone not in log and f["t"] not in log
    assert oct(os.stat(tmp_path / "data" / "logs" / "api-usage.jsonl").st_mode & 0o777) == "0o600"
    k = json.loads(request(port, "GET", "/api/keys", None, headers)[1])
    assert k["keys"][0]["usage"]["last_24h"] >= 2 and k["recent"]


def test_gateway_pair_requires_tls_off_loopback(monkeypatch, tmp_path):
    monkeypatch.setenv("VICTOR_DATA", str(tmp_path / "data"))
    monkeypatch.setattr(gateway.Handler, "cfg", {"require_auth": True, "keys": [], "upstream_port": 1, "upstream_key": "U", "max_body": 1024,
                                                 "tls": False, "bind": "0.0.0.0", "model_path": None})
    with serving(gateway.Handler) as gp:
        st, raw = request(gp, "POST", "/victor/pair", {"v": 1, "t": "x", "device": "p"})
        assert st == 403 and b"tls_required" in raw
