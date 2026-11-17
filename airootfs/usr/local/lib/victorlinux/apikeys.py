"""Named API keys for the gateway (expiry, per-key rate limit, usage log) and opt-in phone pairing.

Keys are stored only as SHA-256 hashes in secrets.json (`api_keys`, unchanged format) with metadata in
`api_key_meta` (name, created, expiry, requests-per-minute limit). A new key is returned exactly once.

Phone pairing (`victorpc://pair`, VICTORPC_PAIRING v1 encodings) is OFF until the owner presses *Pair phone*.
The QR carries the gateway URL, the gateway's TLS certificate fingerprint and a single-use 16-byte token that
expires in 10 minutes. It NEVER carries an API key: the phone redeems the token over TLS (pinned to the
fingerprint) at POST /victor/pair and receives its own named key once. Three wrong tokens close the window.
Standard library only.
"""
import base64
import hashlib
import hmac
import json
import os
import re
import secrets as pysecrets
import threading
import time
import urllib.parse

NAME_RE = re.compile(r"^[\w .:@()-]{1,48}$")
PAIR_TTL = 600
PAIR_MAX_ATTEMPTS = 3
B32 = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"


def sha(s):
    return hashlib.sha256(s.encode()).hexdigest()


def b64url(b):
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def key_id(h):
    return h[:12]


def migrate(sec):
    """Older installs have hashes without metadata: give them a visible name so they can be revoked."""
    meta = sec.setdefault("api_key_meta", {})
    for h in sec.get("api_keys") or []:
        meta.setdefault(h, {"name": "Key created before named keys", "created": None, "expires": None, "rpm": 0})
    for h in list(meta):
        if h not in (sec.get("api_keys") or []):
            meta.pop(h)
    return sec


def create(sec, name, expires_days=0, rpm=0, now=None):
    name = (name or "").strip() or "Unnamed key"
    if not NAME_RE.match(name):
        raise ValueError("Key name: up to 48 letters, digits, spaces and . : @ ( ) - _")
    expires_days, rpm = int(expires_days or 0), int(rpm or 0)
    if not 0 <= expires_days <= 3650 or not 0 <= rpm <= 100000:
        raise ValueError("Expiry must be 0-3650 days and the limit 0-100000 requests per minute (0 = none).")
    migrate(sec)
    now = now or time.time()
    key = "victor-" + pysecrets.token_urlsafe(32)
    h = sha(key)
    sec.setdefault("api_keys", []).append(h)
    sec["api_key_meta"][h] = {"name": name, "created": int(now), "expires": int(now + expires_days * 86400) if expires_days else None, "rpm": rpm}
    return key, public(h, sec["api_key_meta"][h])


def public(h, m, now=None):
    now = now or time.time()
    exp = m.get("expires")
    return {"id": key_id(h), "name": m.get("name"), "created": m.get("created"), "expires": exp, "rpm": m.get("rpm") or 0,
            "expired": bool(exp and now >= exp)}


def listing(sec, now=None):
    migrate(sec)
    return [public(h, sec["api_key_meta"][h], now) for h in sec.get("api_keys") or []]


def revoke(sec, kid):
    migrate(sec)
    hit = [h for h in sec.get("api_keys") or [] if key_id(h) == kid]
    if not hit:
        raise ValueError("No such key.")
    for h in hit:
        sec["api_keys"].remove(h)
        sec["api_key_meta"].pop(h, None)
    return len(hit)


def check(sec, presented, now=None):
    """-> (hash, meta, None) or (None, None, reason). Constant-time compare against every stored hash."""
    if not presented:
        return None, None, "missing"
    d = sha(presented)
    match = None
    for h in sec.get("api_keys") or []:
        if hmac.compare_digest(d, h):
            match = h
    if not match:
        return None, None, "invalid"
    m = (sec.get("api_key_meta") or {}).get(match) or {}
    if m.get("expires") and (now or time.time()) >= m["expires"]:
        return None, None, "expired"
    return match, m, None


class RateLimiter:
    def __init__(self):
        self.lock = threading.Lock()
        self.hits = {}

    def allow(self, kid, rpm, now=None):
        if not rpm:
            return True
        now = now or time.time()
        with self.lock:
            q = [t for t in self.hits.get(kid, []) if now - t < 60]
            ok = len(q) < rpm
            if ok:
                q.append(now)
            self.hits[kid] = q
            return ok


class UsageLog:
    """Append-only JSON lines (0600): time, key id/name, method, path without query, status. Never a key or body."""

    def __init__(self, path, max_bytes=1 << 20):
        self.path, self.max = path, max_bytes
        self.lock = threading.Lock()

    def add(self, kid, name, method, path, status, client=None):
        if not self.path:
            return
        rec = {"t": int(time.time()), "key": kid, "name": name, "m": method, "p": path.split("?", 1)[0][:120], "s": status, "ip": client}
        with self.lock:
            try:
                os.makedirs(os.path.dirname(self.path), exist_ok=True)
                if os.path.exists(self.path) and os.path.getsize(self.path) > self.max:
                    os.replace(self.path, self.path + ".1")
                fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                with os.fdopen(fd, "a") as f:
                    f.write(json.dumps(rec) + "\n")
            except OSError:
                pass


def usage_summary(path, since=None):
    since = since or (time.time() - 86400)
    out = {}
    for p in (path + ".1", path):
        try:
            with open(p) as f:
                for line in f:
                    try:
                        r = json.loads(line)
                    except ValueError:
                        continue
                    k = out.setdefault(r.get("key") or "-", {"name": r.get("name"), "total": 0, "last_24h": 0, "errors_24h": 0, "last": None})
                    k["total"] += 1
                    k["last"] = max(k["last"] or 0, r.get("t") or 0)
                    if (r.get("t") or 0) >= since:
                        k["last_24h"] += 1
                        k["errors_24h"] += int((r.get("s") or 0) >= 400)
        except OSError:
            pass
    return out


def recent(path, n=50):
    try:
        with open(path) as f:
            lines = f.readlines()[-n:]
    except OSError:
        return []
    rows = []
    for line in reversed(lines):
        try:
            rows.append(json.loads(line))
        except ValueError:
            pass
    return rows


# ------------------------------------------------------------------------------------------ pairing --
def profile_id(sec):
    if not re.fullmatch(f"[{B32}]{{12}}", sec.get("pair_profile_id") or ""):
        sec["pair_profile_id"] = "".join(pysecrets.choice(B32) for _ in range(12))
    return sec["pair_profile_id"]


def fingerprint_b64(der_sha256_hex):
    """Certificate SHA-256 in the v1 host-key style: 'SHA256:' + standard base64 without padding."""
    return "SHA256:" + base64.b64encode(bytes.fromhex(der_sha256_hex)).decode().rstrip("=")


def start_pairing(sec, now=None, ttl=PAIR_TTL):
    now = int(now or time.time())
    raw = pysecrets.token_bytes(16)
    t = b64url(raw)
    sec["pairing"] = {"hash": sha(t), "expires": now + ttl, "attempts": 0}
    return t, now + ttl


def cancel_pairing(sec):
    sec.pop("pairing", None)


def pairing_state(sec, now=None):
    p = sec.get("pairing")
    if not p or (now or time.time()) >= p.get("expires", 0) or p.get("attempts", 0) >= PAIR_MAX_ATTEMPTS:
        return {"active": False}
    return {"active": True, "expires": p["expires"]}


def pair_uri(pid, name, gateway_url, cert_sha256_hex, token, expires):
    """victorpc://pair, VICTORPC_PAIRING v1 encodings (base64url token `t`, epoch-seconds `x`, 12-char base32 `id`,
    percent-encoded values with '+' as %2B). Gateway profile keys: `gw` (gateway URL) and `gfp` (TLS certificate
    SHA-256). No API key is ever included."""
    if not re.fullmatch(r"https://[A-Za-z0-9.\-\[\]:]+:\d{1,5}", gateway_url or ""):
        raise ValueError("Pairing needs the gateway on HTTPS with a reachable address.")
    fields = [("v", "1"), ("id", pid), ("n", name[:64]), ("gw", gateway_url), ("gfp", fingerprint_b64(cert_sha256_hex)), ("t", token), ("x", str(int(expires)))]
    return "victorpc://pair?" + "&".join(f"{k}={urllib.parse.quote(v, safe=':/[]')}" for k, v in fields)


def parse_uri(uri):
    """Strict parser used by tests (and usable by clients): duplicate keys refused, unknown keys ignored."""
    u = urllib.parse.urlsplit(uri)
    if u.scheme != "victorpc" or u.netloc != "pair":
        raise ValueError("not a victorpc://pair URI")
    out = {}
    for part in u.query.split("&"):
        k, _, v = part.partition("=")
        if k in out:
            raise ValueError("duplicate key " + k)
        out[k] = urllib.parse.unquote(v)        # never decodes '+' as space
    if out.get("v") != "1":
        raise ValueError("unsupported version")
    return out


def redeem(sec, token, device, now=None):
    """Consume the single-use token and mint a named key. -> (key, public meta) or raises PairError(code)."""
    now = now or time.time()
    p = sec.get("pairing")
    if not p:
        raise PairError("none")
    if now >= p.get("expires", 0):
        cancel_pairing(sec)
        raise PairError("expired")
    if p.get("attempts", 0) >= PAIR_MAX_ATTEMPTS:
        cancel_pairing(sec)
        raise PairError("too_many_attempts")
    if not isinstance(token, str) or not hmac.compare_digest(sha(token), p["hash"]):
        p["attempts"] = p.get("attempts", 0) + 1
        if p["attempts"] >= PAIR_MAX_ATTEMPTS:
            cancel_pairing(sec)
        raise PairError("bad_token")
    cancel_pairing(sec)                         # single use: consumed before the key is minted
    dev = re.sub(r"[^\w .:@()-]", "", str(device or "Phone"))[:40].strip() or "Phone"
    return create(sec, "Phone: " + dev, now=now)


class PairError(Exception):
    pass
