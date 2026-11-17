"""Wi-Fi keyboard/mouse receiver for the Victorious Keyboard Android app.

Off by default. The phone authenticates with a per-device Bearer token minted
through the VICTORPC_PAIRING v1 `victorpc://pair` QR flow (additive keys `iw`
and `ifp`). Tokens are stored only as SHA-256 hashes and are never printed.
Events are injected through uinput (US layout first). Standard library only.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets as pysecrets
import ssl
import struct
import threading
import time
import urllib.parse
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import apikeys

PROTOCOL_VERSION = 1
DEFAULT_PORT = 8473
ADMIN_PORT = 8472
PAIR_TTL = apikeys.PAIR_TTL
PAIR_MAX_ATTEMPTS = apikeys.PAIR_MAX_ATTEMPTS
TS_SKEW_MS = 30_000
NONCE_CACHE = 2048
RATE_PER_SEC = 250
MSG_MAX = 4096
NAME_RE = apikeys.NAME_RE
TOKEN_PREFIX = "victor-input-"

# linux/input-event-codes.h (subset used for US keyboard + relative mouse)
EV_SYN, EV_KEY, EV_REL = 0, 1, 2
SYN_REPORT = 0
REL_X, REL_Y, REL_WHEEL, REL_HWHEEL = 0, 1, 8, 6
BTN_LEFT, BTN_RIGHT, BTN_MIDDLE = 0x110, 0x111, 0x112

# UI Events `code` names -> evdev KEY_* (US ANSI).
US_KEYMAP = {
    "KeyA": 30, "KeyB": 48, "KeyC": 46, "KeyD": 32, "KeyE": 18, "KeyF": 33,
    "KeyG": 34, "KeyH": 35, "KeyI": 23, "KeyJ": 36, "KeyK": 37, "KeyL": 38,
    "KeyM": 50, "KeyN": 49, "KeyO": 24, "KeyP": 25, "KeyQ": 16, "KeyR": 19,
    "KeyS": 31, "KeyT": 20, "KeyU": 22, "KeyV": 47, "KeyW": 17, "KeyX": 45,
    "KeyY": 21, "KeyZ": 44,
    "Digit0": 11, "Digit1": 2, "Digit2": 3, "Digit3": 4, "Digit4": 5,
    "Digit5": 6, "Digit6": 7, "Digit7": 8, "Digit8": 9, "Digit9": 10,
    "Enter": 28, "Escape": 1, "Backspace": 14, "Tab": 15, "Space": 57,
    "Minus": 12, "Equal": 13, "BracketLeft": 26, "BracketRight": 27,
    "Backslash": 43, "Semicolon": 39, "Quote": 40, "Backquote": 41,
    "Comma": 51, "Period": 52, "Slash": 53, "CapsLock": 58,
    "F1": 59, "F2": 60, "F3": 61, "F4": 62, "F5": 63, "F6": 64,
    "F7": 65, "F8": 66, "F9": 67, "F10": 68, "F11": 87, "F12": 88,
    "PrintScreen": 99, "ScrollLock": 70, "Pause": 119, "Insert": 110,
    "Home": 102, "PageUp": 104, "Delete": 111, "End": 107, "PageDown": 109,
    "ArrowRight": 106, "ArrowLeft": 105, "ArrowDown": 108, "ArrowUp": 103,
    "NumLock": 69, "NumpadDivide": 98, "NumpadMultiply": 55, "NumpadSubtract": 74,
    "NumpadAdd": 78, "NumpadEnter": 96, "NumpadDecimal": 83,
    "Numpad0": 82, "Numpad1": 79, "Numpad2": 80, "Numpad3": 81, "Numpad4": 75,
    "Numpad5": 76, "Numpad6": 77, "Numpad7": 71, "Numpad8": 72, "Numpad9": 73,
    "ShiftLeft": 42, "ShiftRight": 54, "ControlLeft": 29, "ControlRight": 97,
    "AltLeft": 56, "AltRight": 100, "MetaLeft": 125, "MetaRight": 126,
    "ContextMenu": 127,
}
BUTTON_MAP = {"left": BTN_LEFT, "right": BTN_RIGHT, "middle": BTN_MIDDLE}
WSS_RE = re.compile(r"^wss://(?:\[[A-Fa-f0-9:]+\]|[A-Za-z0-9.\-]+):\d{1,5}$")


class ProtocolError(ValueError):
    def __init__(self, code, message=None):
        super().__init__(message or code)
        self.code = code


class AuthError(ProtocolError):
    pass


# -------------------------------------------------------------------------------- pairing --
def pair_uri(pid, name, ws_url, cert_sha256_hex, token, expires):
    """victorpc://pair v1 encodings with additive input keys `iw` (wss URL) and `ifp` (TLS fingerprint).

    Never includes a Bearer token or its hash. `iw` is WSS only (TLS required, same rule as the gateway QR).
    """
    if not WSS_RE.match(ws_url or ""):
        raise ValueError("Pairing needs the input service on WSS with a reachable address.")
    fields = [
        ("v", "1"), ("id", pid), ("n", name[:64]),
        ("iw", ws_url), ("ifp", apikeys.fingerprint_b64(cert_sha256_hex)),
        ("t", token), ("x", str(int(expires))),
    ]
    return "victorpc://pair?" + "&".join(f"{k}={urllib.parse.quote(v, safe=':/[]')}" for k, v in fields)


def parse_uri(uri):
    return apikeys.parse_uri(uri)


def start_pairing(sec, now=None, ttl=PAIR_TTL):
    return apikeys.start_pairing(sec, now=now, ttl=ttl)


def cancel_pairing(sec):
    apikeys.cancel_pairing(sec)


def pairing_state(sec, now=None):
    return apikeys.pairing_state(sec, now=now)


def _devices(sec):
    return sec.setdefault("input_devices", {})


def create_device(sec, name, now=None):
    name = (name or "").strip() or "Phone"
    if not NAME_RE.match(name):
        raise ValueError("Device name: up to 48 letters, digits, spaces and . : @ ( ) - _")
    now = now or time.time()
    token = TOKEN_PREFIX + pysecrets.token_urlsafe(32)
    h = apikeys.sha(token)
    _devices(sec)[h] = {"name": name, "created": int(now), "revoked": False}
    return token, public_device(h, _devices(sec)[h])


def public_device(h, meta):
    return {"id": apikeys.key_id(h), "name": meta.get("name"), "created": meta.get("created")}


def listing(sec):
    return [public_device(h, m) for h, m in _devices(sec).items() if not m.get("revoked")]


def revoke(sec, kid):
    hit = [h for h, m in _devices(sec).items() if apikeys.key_id(h) == kid and not m.get("revoked")]
    if not hit:
        raise ValueError("No such device.")
    for h in hit:
        _devices(sec)[h]["revoked"] = True
    return len(hit)


def check_token(sec, presented):
    """-> (hash, meta, None) or (None, None, reason). Constant-time against every stored hash."""
    if not presented:
        return None, None, "missing"
    d = apikeys.sha(presented)
    match = None
    for h, m in _devices(sec).items():
        if hmac.compare_digest(d, h):
            match = h
            meta = m
    if not match:
        return None, None, "invalid"
    if meta.get("revoked"):
        return None, None, "revoked"
    return match, meta, None


def redeem(sec, token, device, now=None):
    """Consume the single-use pairing token and mint a per-device Bearer token."""
    now = now or time.time()
    p = sec.get("pairing")
    if not p:
        raise apikeys.PairError("none")
    if now >= p.get("expires", 0):
        cancel_pairing(sec)
        raise apikeys.PairError("expired")
    if p.get("attempts", 0) >= PAIR_MAX_ATTEMPTS:
        cancel_pairing(sec)
        raise apikeys.PairError("too_many_attempts")
    if not isinstance(token, str) or not hmac.compare_digest(apikeys.sha(token), p["hash"]):
        p["attempts"] = p.get("attempts", 0) + 1
        if p["attempts"] >= PAIR_MAX_ATTEMPTS:
            cancel_pairing(sec)
        raise apikeys.PairError("bad_token")
    cancel_pairing(sec)
    dev = re.sub(r"[^\w .:@()-]", "", str(device or "Phone"))[:40].strip() or "Phone"
    return create_device(sec, "Phone: " + dev, now=now)


# -------------------------------------------------------------------------------- protocol --
def parse_message(raw, now_ms=None):
    """Parse one JSON input message. Raises ProtocolError with a short code."""
    if raw is None:
        raise ProtocolError("bad_message", "empty message")
    if isinstance(raw, dict):
        msg = raw
    else:
        if isinstance(raw, (bytes, bytearray)):
            if len(raw) > MSG_MAX:
                raise ProtocolError("bad_message", "message too large")
            try:
                raw = raw.decode("utf-8")
            except UnicodeDecodeError as e:
                raise ProtocolError("bad_message", "not utf-8") from e
        if not isinstance(raw, str) or len(raw) > MSG_MAX:
            raise ProtocolError("bad_message", "message too large")
        try:
            msg = json.loads(raw)
        except ValueError as e:
            raise ProtocolError("bad_message", "invalid JSON") from e
    if not isinstance(msg, dict):
        raise ProtocolError("bad_message", "JSON object required")
    if msg.get("v") != PROTOCOL_VERSION:
        raise ProtocolError("bad_message", "unsupported version")
    typ = msg.get("type")
    if typ not in ("auth", "key", "pointer", "ping"):
        raise ProtocolError("bad_message", "unknown type")
    if typ == "auth":
        tok = msg.get("token")
        if not isinstance(tok, str) or not tok:
            raise AuthError("unauthorized", "missing token")
        return {"type": "auth", "token": tok, "device": str(msg.get("device") or "Phone")[:48]}
    ts, nonce = msg.get("ts"), msg.get("n")
    if not isinstance(ts, int) or isinstance(ts, bool) or ts < 0:
        raise ProtocolError("bad_message", "ts must be a unix time in milliseconds")
    if not isinstance(nonce, str) or not (16 <= len(nonce) <= 64) or not re.fullmatch(r"[A-Za-z0-9_\-]+", nonce):
        raise ProtocolError("replay", "n must be a 16-64 character unique nonce")
    now_ms = int(now_ms if now_ms is not None else time.time() * 1000)
    if abs(now_ms - ts) > TS_SKEW_MS:
        raise ProtocolError("replay", "timestamp outside allowed window")
    if typ == "ping":
        return {"type": "ping", "ts": ts, "n": nonce}
    if typ == "key":
        down, up = msg.get("down") or [], msg.get("up") or []
        if not isinstance(down, list) or not isinstance(up, list) or len(down) + len(up) > 16:
            raise ProtocolError("bad_message", "key lists invalid")
        for name in down + up:
            if name not in US_KEYMAP:
                raise ProtocolError("bad_message", f"unknown key {name}")
        return {"type": "key", "ts": ts, "n": nonce, "down": list(down), "up": list(up)}
    # pointer
    def _int(name, lo, hi):
        v = msg.get(name, 0)
        if not isinstance(v, int) or isinstance(v, bool) or not lo <= v <= hi:
            raise ProtocolError("bad_message", f"{name} out of range")
        return v
    buttons = msg.get("buttons") or {}
    if not isinstance(buttons, dict) or any(k not in BUTTON_MAP for k in buttons):
        raise ProtocolError("bad_message", "unknown mouse button")
    if any(v not in (0, 1, True, False) for v in buttons.values()):
        raise ProtocolError("bad_message", "button values must be 0 or 1")
    return {
        "type": "pointer", "ts": ts, "n": nonce,
        "dx": _int("dx", -4096, 4096), "dy": _int("dy", -4096, 4096),
        "wheel": _int("wheel", -16, 16), "hwheel": _int("hwheel", -16, 16),
        "buttons": {k: int(bool(v)) for k, v in buttons.items()},
    }


class ReplayGuard:
    """Reject a nonce that was already seen, per device."""

    def __init__(self, size=NONCE_CACHE):
        self.size = size
        self.lock = threading.Lock()
        self.seen = {}

    def check(self, device_id, nonce):
        with self.lock:
            q, s = self.seen.get(device_id, (deque(), set()))
            if nonce in s:
                return False
            q.append(nonce)
            s.add(nonce)
            while len(q) > self.size:
                s.discard(q.popleft())
            self.seen[device_id] = (q, s)
            return True


class RateLimiter:
    """At most RATE_PER_SEC messages in any rolling one-second window, per device."""

    def __init__(self, rate=RATE_PER_SEC):
        self.rate = rate
        self.lock = threading.Lock()
        self.hits = {}

    def allow(self, device_id, now=None):
        now = now if now is not None else time.time()
        with self.lock:
            q = [t for t in self.hits.get(device_id, []) if now - t < 1.0]
            if len(q) >= self.rate:
                self.hits[device_id] = q
                return False
            q.append(now)
            self.hits[device_id] = q
            return True


# -------------------------------------------------------------------------------- uinput --
class MemorySink:
    """Test double: records evdev (type, code, value) tuples. No /dev/uinput."""

    def __init__(self):
        self.events = []

    def emit(self, ev_type, code, value):
        self.events.append((int(ev_type), int(code), int(value)))

    def syn(self):
        self.emit(EV_SYN, SYN_REPORT, 0)

    def close(self):
        pass


# uinput ioctls: _IOW('U', n, int) on Linux.
_UI_IOW = lambda n: (1 << 30) | (ord("U") << 8) | n | (4 << 16)
UI_SET_EVBIT = _UI_IOW(100)
UI_SET_KEYBIT = _UI_IOW(101)
UI_SET_RELBIT = _UI_IOW(102)
UI_DEV_CREATE = (ord("U") << 8) | 1
UI_DEV_DESTROY = (ord("U") << 8) | 2


class UInputSink:
    """Real /dev/uinput keyboard + relative mouse. `dev` may be a file-like for tests."""

    def __init__(self, path="/dev/uinput", dev=None, name="VictorLinux Phone Keyboard"):
        self.path = path
        self._own = dev is None
        if dev is None:
            self.dev = open(path, "wb", buffering=0)
        else:
            self.dev = dev
        fd = self.dev.fileno()
        import fcntl
        fcntl.ioctl(fd, UI_SET_EVBIT, EV_KEY)
        fcntl.ioctl(fd, UI_SET_EVBIT, EV_REL)
        fcntl.ioctl(fd, UI_SET_EVBIT, EV_SYN)
        for code in set(US_KEYMAP.values()) | set(BUTTON_MAP.values()):
            fcntl.ioctl(fd, UI_SET_KEYBIT, code)
        for code in (REL_X, REL_Y, REL_WHEEL, REL_HWHEEL):
            fcntl.ioctl(fd, UI_SET_RELBIT, code)
        # struct uinput_user_dev: name[80], input_id (4x u16), ff_effects_max, abs arrays skipped as zeros
        buf = bytearray(80 + 6 * 2 + 4 + 64 * 4 * 4)
        buf[:len(name)] = name.encode()[:79]
        struct.pack_into("HHHHH", buf, 80, 3, 0x0001, 0x0001, 1, 0)  # BUS_USB, vendor, product, version
        self.dev.write(buf)
        fcntl.ioctl(fd, UI_DEV_CREATE)

    def emit(self, ev_type, code, value):
        # struct input_event: timeval (2x long) + type u16 + code u16 + value s32
        if struct.calcsize("@llHHi") == struct.calcsize("@QQHHi"):
            fmt = "@QQHHi"
        else:
            fmt = "@llHHi"
        now = time.time()
        sec, usec = int(now), int((now - int(now)) * 1_000_000)
        self.dev.write(struct.pack(fmt, sec, usec, ev_type, code, value))

    def syn(self):
        self.emit(EV_SYN, SYN_REPORT, 0)

    def close(self):
        try:
            import fcntl
            fcntl.ioctl(self.dev.fileno(), UI_DEV_DESTROY)
        except OSError:
            pass
        if self._own:
            try:
                self.dev.close()
            except OSError:
                pass


def apply_event(sink, event):
    """Map a parsed event onto a uinput sink. Returns the number of evdev events written (including SYN)."""
    typ = event["type"]
    if typ == "ping":
        return 0
    n = 0
    if typ == "key":
        for name in event["down"]:
            sink.emit(EV_KEY, US_KEYMAP[name], 1)
            n += 1
        for name in event["up"]:
            sink.emit(EV_KEY, US_KEYMAP[name], 0)
            n += 1
    elif typ == "pointer":
        if event["dx"]:
            sink.emit(EV_REL, REL_X, event["dx"]); n += 1
        if event["dy"]:
            sink.emit(EV_REL, REL_Y, event["dy"]); n += 1
        if event["wheel"]:
            sink.emit(EV_REL, REL_WHEEL, event["wheel"]); n += 1
        if event["hwheel"]:
            sink.emit(EV_REL, REL_HWHEEL, event["hwheel"]); n += 1
        for btn, val in event["buttons"].items():
            sink.emit(EV_KEY, BUTTON_MAP[btn], val); n += 1
    else:
        raise ProtocolError("bad_message", "not an input event")
    if n:
        sink.syn()
        n += 1
    return n


# -------------------------------------------------------------------------------- store / tls --
def default_state_dir():
    return os.environ.get("VICTOR_INPUT_DIR") or os.environ.get("VICTOR_DATA") or "/var/lib/victorlinux/input"


def secrets_path(root=None):
    return os.path.join(root or default_state_dir(), "input-secrets.json")


def load_secrets(root=None):
    path = secrets_path(root)
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except ValueError:
        return {}


def save_secrets(sec, root=None):
    path = secrets_path(root)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    data = json.dumps(sec, indent=2, sort_keys=True)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def tls_paths(root=None):
    d = os.path.join(root or default_state_dir(), "tls")
    return os.path.join(d, "cert.pem"), os.path.join(d, "key.pem")


def cert_sha256_hex(cert_pem_path):
    from datetime import datetime, timezone
    try:
        from datetime import UTC  # noqa
    except ImportError:
        UTC = timezone.utc
    # Parse PEM and hash the DER certificate (same as the gateway fingerprint).
    pem = open(cert_pem_path, "rb").read()
    start, end = b"-----BEGIN CERTIFICATE-----", b"-----END CERTIFICATE-----"
    a, b = pem.find(start), pem.find(end)
    if a < 0 or b < 0:
        raise ValueError("TLS certificate could not be read.")
    der = base64.b64decode(b"".join(pem[a + len(start):b].split()))
    return hashlib.sha256(der).hexdigest()


def ensure_tls(root=None, hostname="victorlinux"):
    """Create a self-signed certificate if missing. Never downloads anything."""
    cert, key = tls_paths(root)
    if os.path.exists(cert) and os.path.exists(key):
        return cert, key, cert_sha256_hex(cert)
    os.makedirs(os.path.dirname(cert), exist_ok=True)
    # Prefer openssl already on the image; fall back to a tiny self-signed cert via stdlib if present.
    import subprocess
    host = re.sub(r"[^A-Za-z0-9.\-]", "", hostname) or "victorlinux"
    cmd = [
        "openssl", "req", "-x509", "-newkey", "rsa:2048", "-sha256", "-days", "825", "-nodes",
        "-keyout", key, "-out", cert, "-subj", f"/CN={host}",
        "-addext", f"subjectAltName=DNS:{host},DNS:localhost,IP:127.0.0.1",
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError("Could not create a local TLS certificate for the input service.")
    os.chmod(cert, 0o644)
    os.chmod(key, 0o600)
    return cert, key, cert_sha256_hex(cert)


def ssl_context(root=None):
    cert, key, fp = ensure_tls(root)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(cert, key)
    return ctx, fp, cert


# -------------------------------------------------------------------------------- websocket (subset of RFC 6455, text frames) --
GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def ws_accept(key):
    return base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()


def ws_read(sock, limit=MSG_MAX + 16):
    def recvn(n):
        buf = b""
        while len(buf) < n:
            chunk = sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("closed")
            buf += chunk
        return buf
    hdr = recvn(2)
    opcode, ln = hdr[0] & 0x0F, hdr[1] & 0x7F
    masked = bool(hdr[1] & 0x80)
    if ln == 126:
        ln = struct.unpack("!H", recvn(2))[0]
    elif ln == 127:
        ln = struct.unpack("!Q", recvn(8))[0]
    if ln > limit:
        raise ProtocolError("bad_message", "message too large")
    mask = recvn(4) if masked else b""
    data = recvn(ln)
    if masked:
        data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
    if opcode == 0x8:  # close
        return None
    if opcode == 0x9:  # ping -> pong handled by caller as empty control; ignore payload
        return b""
    if opcode != 0x1:
        raise ProtocolError("bad_message", "text frames only")
    return data


def ws_send(sock, text):
    data = text.encode() if isinstance(text, str) else text
    hdr = bytearray([0x81])
    n = len(data)
    if n < 126:
        hdr.append(n)
    elif n < 65536:
        hdr.append(126)
        hdr += struct.pack("!H", n)
    else:
        hdr.append(127)
        hdr += struct.pack("!Q", n)
    sock.sendall(hdr + data)


def _bearer(headers, query=None):
    auth = headers.get("Authorization") or headers.get("authorization") or ""
    if auth.lower().startswith("bearer "):
        return auth.split(" ", 1)[1].strip()
    if query:
        return query.get("access_token", [None])[0]
    return None


# -------------------------------------------------------------------------------- session handling --
class InputSession:
    def __init__(self, sec, sink, store_root, now_fn=None):
        self.sec = sec
        self.sink = sink
        self.store_root = store_root
        self.replay = ReplayGuard()
        self.rate = RateLimiter()
        self.lock = threading.Lock()
        self.now_fn = now_fn or time.time

    def handle(self, raw, device_id=None, now_ms=None):
        msg = parse_message(raw, now_ms=now_ms)
        if msg["type"] == "auth":
            h, meta, err = check_token(self.sec, msg["token"])
            if err:
                raise AuthError("unauthorized", err)
            return {"v": 1, "type": "ok", "device_id": apikeys.key_id(h), "name": meta.get("name")}
        if not device_id:
            raise AuthError("unauthorized", "missing token")
        if not self.replay.check(device_id, msg["n"]):
            raise ProtocolError("replay", "replayed nonce")
        if not self.rate.allow(device_id, now=self.now_fn()):
            raise ProtocolError("rate_limited", "too many events")
        apply_event(self.sink, msg)
        return {"v": 1, "type": "ack", "n": msg["n"]}


def error_payload(err):
    code = getattr(err, "code", "bad_message")
    return {"v": 1, "type": "error", "err": code}


def default_off_status():
    return {
        "enabled": False, "running": False, "bind": "127.0.0.1", "port": DEFAULT_PORT,
        "devices": [], "pairing": {"active": False}, "default_off": True,
        "bluetooth_default": "off",
    }
