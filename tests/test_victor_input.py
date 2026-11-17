"""Protocol, auth, replay/rate limits and uinput mapping for the Wi-Fi input receiver."""
import json
import time

import pytest

import apikeys
import victorinput as vi


def test_default_is_off():
    st = vi.default_off_status()
    assert st["enabled"] is False and st["running"] is False and st["default_off"] is True
    assert st["pairing"] == {"active": False} and st["devices"] == []
    assert st["bluetooth_default"] == "off"
    assert vi.pairing_state({}) == {"active": False}
    assert vi.listing({}) == []


def test_pair_uri_v1_encodings_and_no_token():
    sec = {}
    tok, exp = vi.start_pairing(sec, now=1000)
    uri = vi.pair_uri(apikeys.profile_id(sec), "Don's PC+1", "wss://192.168.1.42:8473", "ab" * 32, tok, exp)
    assert uri.startswith("victorpc://pair?v=1&id=") and "%2B" in uri and "+" not in uri
    f = vi.parse_uri(uri)
    assert f["v"] == "1" and f["iw"] == "wss://192.168.1.42:8473" and f["ifp"].startswith("SHA256:")
    assert f["t"] == tok and f["x"] == "1600" and f["n"] == "Don's PC+1"
    assert tok not in json.dumps(sec)          # only the hash of t is stored
    # Single-use pairing token t is in the QR (same as the gateway flow). The long-lived Bearer token is not.
    assert "victor-input-" not in uri
    with pytest.raises(ValueError):
        vi.parse_uri(uri + "&t=again")
    with pytest.raises(ValueError, match="WSS"):
        vi.pair_uri("ABCDEFGHIJKL", "n", "ws://192.168.1.42:8473", "ab" * 32, tok, exp)
    with pytest.raises(ValueError):
        vi.pair_uri("ABCDEFGHIJKL", "n", "wss://192.168.1.42", "ab" * 32, tok, exp)


def test_redeem_mints_bearer_once_and_rejects_replay():
    sec = {}
    tok, _ = vi.start_pairing(sec, now=300)
    bearer, pub = vi.redeem(sec, tok, "Pixel 8", now=301)
    assert bearer.startswith("victor-input-") and pub["name"] == "Phone: Pixel 8"
    assert bearer not in json.dumps(sec) and apikeys.sha(bearer) in sec["input_devices"]
    with pytest.raises(apikeys.PairError, match="none"):
        vi.redeem(sec, tok, "Pixel 8", now=302)     # single use
    h, meta, err = vi.check_token(sec, bearer)
    assert err is None and meta["name"] == "Phone: Pixel 8"
    assert vi.check_token(sec, "victor-input-nope")[2] == "invalid"
    assert vi.check_token(sec, "")[2] == "missing"
    vi.revoke(sec, pub["id"])
    assert vi.check_token(sec, bearer)[2] == "revoked"
    tok, _ = vi.start_pairing(sec, now=400)
    for _ in range(3):
        with pytest.raises(apikeys.PairError):
            vi.redeem(sec, "AAAAAAAAAAAAAAAAAAAAAA", "x", now=401)
    with pytest.raises(apikeys.PairError, match="none"):
        vi.redeem(sec, tok, "x", now=402)


def test_parse_message_key_pointer_and_rejects():
    now = 1_700_000_000_000
    key = vi.parse_message(json.dumps({
        "v": 1, "type": "key", "ts": now, "n": "n" * 16, "down": ["KeyA", "ShiftLeft"], "up": ["KeyB"],
    }), now_ms=now)
    assert key["down"] == ["KeyA", "ShiftLeft"] and key["up"] == ["KeyB"]
    ptr = vi.parse_message({
        "v": 1, "type": "pointer", "ts": now, "n": "m" * 16, "dx": -3, "dy": 8, "wheel": 1,
        "buttons": {"left": 1},
    }, now_ms=now)
    assert ptr["dx"] == -3 and ptr["dy"] == 8 and ptr["wheel"] == 1 and ptr["buttons"]["left"] == 1
    auth = vi.parse_message('{"v":1,"type":"auth","token":"victor-input-abc","device":"Pixel"}')
    assert auth["token"].startswith("victor-input-")
    with pytest.raises(vi.ProtocolError) as e:
        vi.parse_message('{"v":2,"type":"ping","ts":1,"n":"' + "n" * 16 + '"}')
    assert e.value.code == "bad_message"
    with pytest.raises(vi.ProtocolError) as e:
        vi.parse_message(json.dumps({"v": 1, "type": "key", "ts": now, "n": "n" * 16, "down": ["KeyÄ"]}), now_ms=now)
    assert e.value.code == "bad_message"
    with pytest.raises(vi.ProtocolError) as e:
        vi.parse_message(json.dumps({"v": 1, "type": "key", "ts": now - vi.TS_SKEW_MS - 1, "n": "n" * 16}), now_ms=now)
    assert e.value.code == "replay"
    with pytest.raises(vi.ProtocolError) as e:
        vi.parse_message(json.dumps({"v": 1, "type": "key", "ts": now, "n": "short"}), now_ms=now)
    assert e.value.code == "replay"
    with pytest.raises(vi.ProtocolError):
        vi.parse_message(json.dumps({"v": 1, "type": "pointer", "ts": now, "n": "n" * 16, "dx": 99999}), now_ms=now)
    with pytest.raises(vi.ProtocolError):
        vi.parse_message(json.dumps({"v": 1, "type": "pointer", "ts": now, "n": "n" * 16, "buttons": {"thumb": 1}}), now_ms=now)
    with pytest.raises(vi.AuthError):
        vi.parse_message('{"v":1,"type":"auth"}')
    with pytest.raises(vi.ProtocolError):
        vi.parse_message("not-json")
    with pytest.raises(vi.ProtocolError):
        vi.parse_message("x" * (vi.MSG_MAX + 1))


def test_replay_and_rate_limits():
    g = vi.ReplayGuard(size=4)
    assert g.check("d1", "a" * 16) and not g.check("d1", "a" * 16)
    assert g.check("d2", "a" * 16)          # other device may reuse
    for i in range(4):
        assert g.check("d1", f"{i:016d}")
    # first nonce evicted after cache fills
    assert g.check("d1", "a" * 16)
    rl = vi.RateLimiter(rate=3)
    assert [rl.allow("d", now=0.0 + i * 0.1) for i in range(4)] == [True, True, True, False]
    assert rl.allow("d", now=1.2) is True
    assert rl.allow("other", now=0.0) is True


def test_uinput_mapping_with_memory_sink():
    sink = vi.MemorySink()
    now = 1_700_000_000_000
    ev = vi.parse_message(json.dumps({
        "v": 1, "type": "key", "ts": now, "n": "n" * 16, "down": ["KeyA", "ShiftLeft"], "up": ["KeyA"],
    }), now_ms=now)
    assert vi.apply_event(sink, ev) == 4   # 3 keys + SYN
    assert sink.events == [
        (vi.EV_KEY, vi.US_KEYMAP["KeyA"], 1),
        (vi.EV_KEY, vi.US_KEYMAP["ShiftLeft"], 1),
        (vi.EV_KEY, vi.US_KEYMAP["KeyA"], 0),
        (vi.EV_SYN, vi.SYN_REPORT, 0),
    ]
    sink.events.clear()
    ev = vi.parse_message(json.dumps({
        "v": 1, "type": "pointer", "ts": now, "n": "p" * 16,
        "dx": 12, "dy": -4, "wheel": -1, "hwheel": 1, "buttons": {"left": 1, "right": 0},
    }), now_ms=now)
    vi.apply_event(sink, ev)
    assert (vi.EV_REL, vi.REL_X, 12) in sink.events
    assert (vi.EV_REL, vi.REL_Y, -4) in sink.events
    assert (vi.EV_REL, vi.REL_WHEEL, -1) in sink.events
    assert (vi.EV_REL, vi.REL_HWHEEL, 1) in sink.events
    assert (vi.EV_KEY, vi.BTN_LEFT, 1) in sink.events
    assert (vi.EV_KEY, vi.BTN_RIGHT, 0) in sink.events
    assert sink.events[-1] == (vi.EV_SYN, vi.SYN_REPORT, 0)
    assert vi.apply_event(sink, {"type": "ping", "ts": now, "n": "q" * 16}) == 0


def test_session_auth_replay_and_rate(tmp_path):
    sec = {}
    tok, _ = vi.start_pairing(sec, now=10)
    bearer, pub = vi.redeem(sec, tok, "Pixel", now=11)
    sink = vi.MemorySink()
    sess = vi.InputSession(sec, sink, str(tmp_path), now_fn=lambda: 100.0)
    now = 1_700_000_000_000
    with pytest.raises(vi.AuthError):
        sess.handle(json.dumps({"v": 1, "type": "key", "ts": now, "n": "n" * 16, "down": ["KeyA"]}), now_ms=now)
    ok = sess.handle(json.dumps({"v": 1, "type": "auth", "token": bearer, "device": "Pixel"}))
    assert ok["type"] == "ok" and ok["device_id"] == pub["id"]
    with pytest.raises(vi.AuthError):
        sess.handle(json.dumps({"v": 1, "type": "auth", "token": "victor-input-wrong"}))
    body = {"v": 1, "type": "key", "ts": now, "n": "n" * 16, "down": ["KeyA"], "up": ["KeyA"]}
    assert sess.handle(json.dumps(body), device_id=pub["id"], now_ms=now)["type"] == "ack"
    with pytest.raises(vi.ProtocolError) as e:
        sess.handle(json.dumps(body), device_id=pub["id"], now_ms=now)
    assert e.value.code == "replay"
    sess.rate = vi.RateLimiter(rate=1)
    ev2 = {"v": 1, "type": "key", "ts": now, "n": "m" * 16, "down": ["KeyB"]}
    ev3 = {"v": 1, "type": "key", "ts": now, "n": "o" * 16, "down": ["KeyC"]}
    sess.handle(json.dumps(ev2), device_id=pub["id"], now_ms=now)
    with pytest.raises(vi.ProtocolError) as e:
        sess.handle(json.dumps(ev3), device_id=pub["id"], now_ms=now)
    assert e.value.code == "rate_limited"
    # errors never include the bearer token
    assert bearer not in json.dumps(vi.error_payload(e.value))


def test_wss_pair_and_admin_localhost(tmp_path):
    """Live WSS + localhost admin: redeem a token, reject a replay, keep admin off the LAN."""
    import http.client
    import ssl
    import urllib.request

    app = vi.InputApp(str(tmp_path), sink=vi.MemorySink(), bind="127.0.0.1", port=0, admin_port=0)
    app.start_background()
    try:
        for _ in range(40):
            if app.running and app.port:
                break
            time.sleep(0.05)
        assert app.running and app.port
        started = app.start_pairing_uri("127.0.0.1")
        assert started["uri"].startswith("victorpc://pair?v=1") and "victor-input-" not in started["uri"]
        fields = vi.parse_uri(started["uri"])
        ctx = ssl._create_unverified_context()
        # Redeem once over TLS.
        conn = http.client.HTTPSConnection("127.0.0.1", app.port, context=ctx, timeout=5)
        body = json.dumps({"v": 1, "t": fields["t"], "device": "Pixel 8"}).encode()
        conn.request("POST", "/victor/pair", body=body, headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        data = json.loads(resp.read())
        assert resp.status == 200 and data["ok"] is True and data["token"].startswith("victor-input-")
        token = data["token"]
        # Replay of the pairing token is refused.
        conn.request("POST", "/victor/pair", body=body, headers={"Content-Type": "application/json"})
        resp2 = conn.getresponse()
        data2 = json.loads(resp2.read())
        assert resp2.status == 403 and data2["ok"] is False and data2["err"] == "none"
        conn.close()
        # Localhost admin reports the device and never includes the Bearer token.
        st = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{app.admin_port}/status", timeout=5).read())
        assert st["running"] is True and st["devices"][0]["name"] == "Phone: Pixel 8"
        assert token not in json.dumps(st)
        # Admin is bound to loopback only.
        assert app._admin.server_address[0] == "127.0.0.1"
        # Unauthenticated input message is rejected.
        conn = http.client.HTTPSConnection("127.0.0.1", app.port, context=ctx, timeout=5)
        conn.request("GET", "/health")
        assert conn.getresponse().status == 200
        conn.close()
    finally:
        app.stop()
        time.sleep(0.05)
