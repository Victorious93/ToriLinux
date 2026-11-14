"""SIMULATED phone worker used only by tests. It speaks the real wire protocol over real HTTPS (pinned fingerprint),
with a real EC P-256 device key, but it is NOT a phone: it proves the coordinator, not Android behaviour."""
import base64, hashlib, http.client, json, os, ssl, subprocess, tempfile, uuid
from victorfleet.protocol import envelope


class SimWorker:
    def __init__(self, host, port, fingerprint, name="sim", ops=None, root=False, device_state=None, max_concurrent=1):
        self.host, self.port, self.fpr, self.name = host, port, fingerprint, name
        self.tmp = tempfile.mkdtemp()
        subprocess.run(["openssl", "ecparam", "-name", "prime256v1", "-genkey", "-noout", "-out", f"{self.tmp}/k.pem"], check=True, capture_output=True)
        self.pub = subprocess.run(["openssl", "ec", "-in", f"{self.tmp}/k.pem", "-pubout"], capture_output=True, check=True).stdout.decode()
        self.device_id = self.token = None
        self.ops = ops if ops is not None else {"hash_files": {"available": True, "granted": True}}
        self.root = {"available": root, "granted": root}
        self.device_state = device_state or {"battery_percent": 80, "charging": True, "thermal": "none", "free_storage_bytes": 10 << 30}
        self.max_concurrent = max_concurrent
        self.inbox, self.acks = [], []

    def _conn(self):
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
        c = http.client.HTTPSConnection(self.host, self.port, context=ctx, timeout=30)
        c.connect()
        got = hashlib.sha256(c.sock.getpeercert(binary_form=True)).hexdigest()
        if got != self.fpr:      # certificate PINNING: refuse any other coordinator identity
            c.close(); raise ssl.SSLError("coordinator fingerprint mismatch")
        return c

    def req(self, method, path, body=None, headers=None, raw=None):
        c = self._conn()
        h = dict(headers or {})
        if self.token: h["Authorization"] = "Bearer " + self.token
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        if data is not None and "Content-Type" not in h: h["Content-Type"] = "application/json"
        c.request(method, path, data, h)
        r = c.getresponse(); b = r.read(); c.close()
        return r.status, (json.loads(b) if r.getheader("Content-Type", "").startswith("application/json") else b), r

    def sign(self, msg: bytes):
        with open(f"{self.tmp}/m", "wb") as fh: fh.write(msg)
        sig = subprocess.run(["openssl", "dgst", "-sha256", "-sign", f"{self.tmp}/k.pem", f"{self.tmp}/m"], capture_output=True, check=True).stdout
        return base64.b64encode(sig).decode()

    def enroll(self, secret, nonce=None, sign_fpr=None):
        nonce = nonce or uuid.uuid4().hex
        sph = hashlib.sha256(secret.encode()).hexdigest()
        sig = self.sign(f"victorfleet-enroll-v1\n{sign_fpr or self.fpr}\n{sph}\n{nonce}".encode())
        env = envelope("enroll_request", "unenrolled", "coordinator", {"device_name": self.name, "public_key_pem": self.pub, "pairing_proof": secret,
                                                                       "signature_b64": sig, "nonce": nonce})
        st, body, _ = self.req("POST", "/v1/enroll", env)
        if st == 200:
            self.device_id = body["payload"]["device_id"]; self.token = body["payload"]["credential"]
        return st, body

    def caps(self):
        return {"operations": self.ops, "root": self.root, "device_state": self.device_state, "max_concurrent": self.max_concurrent}

    def msg(self, t, payload):
        return envelope(t, self.device_id, "coordinator", payload)

    def exchange(self, msgs=(), wait_s=0):
        st, body, _ = self.req("POST", "/v1/exchange", {"messages": list(msgs), "acks": self.acks, "wait_s": wait_s})
        if st == 200:
            self.acks = [m["msg_id"] for m in body["messages"]]
            self.inbox.extend(body["messages"])
        return st, body

    def hello(self, running=()):
        return self.exchange([self.msg("hello", {"capabilities": self.caps(), "app_version": "sim-1", "running": list(running)})])

    def take(self, mtype):
        """Pop (and return) buffered inbound messages of a type. Every exchange response feeds the inbox, like a real worker's queue."""
        out = [m for m in self.inbox if m["type"] == mtype]
        self.inbox = [m for m in self.inbox if m["type"] != mtype]
        return out

    def upload(self, job_id, attempt, name, data):
        sha = hashlib.sha256(data).hexdigest()
        st, b, _ = self.req("PUT", f"/v1/artifacts/{job_id}/{attempt}/{name}?sha256={sha}", raw=data, headers={"Content-Type": "application/octet-stream"})
        return st, b, sha

    def download(self, artifact_id):
        st, b, r = self.req("GET", f"/v1/artifacts/{artifact_id}")
        return st, b
