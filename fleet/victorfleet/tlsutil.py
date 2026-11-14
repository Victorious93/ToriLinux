"""Self-signed coordinator certificate (EC P-256) generated with the openssl CLI, plus ECDSA verification.
The cert is never shared fleet-wide as a secret: phones PIN its SHA-256 fingerprint, which the owner verifies
out-of-band during pairing (shown on the coordinator screen and inside the pairing payload)."""
import base64, hashlib, os, ssl, subprocess, tempfile


def _openssl(*args, input=None):
    r = subprocess.run(["openssl", *args], capture_output=True, input=input, timeout=30)
    if r.returncode != 0:
        raise RuntimeError(f"openssl {args[0]} failed: {r.stderr.decode(errors='replace').strip()}")
    return r.stdout


def ensure_cert(tls_dir, names):
    """names: iterable of IPs/hostnames for subjectAltName. Regenerates if the SAN set changed."""
    os.makedirs(tls_dir, mode=0o700, exist_ok=True)
    crt, key, san_f = (os.path.join(tls_dir, n) for n in ("coordinator.crt", "coordinator.key", "san.txt"))
    san = ",".join(sorted({("IP:" if n.replace(".", "").replace(":", "").isdigit() or ":" in n else "DNS:") + n for n in names} | {"DNS:localhost", "IP:127.0.0.1"}))
    if not (os.path.exists(crt) and os.path.exists(key) and open(san_f).read() == san if os.path.exists(san_f) else False):
        old_umask = os.umask(0o077)
        try:
            _openssl("req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes", "-keyout", key, "-out", crt,
                     "-days", "825", "-subj", "/CN=victor-fleet-coordinator/O=Victorious Framework", "-addext", f"subjectAltName={san}")
            open(san_f, "w").write(san)
        finally:
            os.umask(old_umask)
    return crt, key


def fingerprint(crt_path):
    der = ssl.PEM_cert_to_DER_cert(open(crt_path).read())
    return hashlib.sha256(der).hexdigest()


def server_context(crt, key):
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(crt, key)
    return ctx


def verify_ecdsa(pubkey_pem, message: bytes, signature_b64: str) -> bool:
    """SHA256withECDSA (DER), as produced by Android Keystore. Verified with the openssl CLI."""
    try:
        sig = base64.b64decode(signature_b64, validate=True)
    except Exception:
        return False
    with tempfile.TemporaryDirectory() as d:
        pk, sg, mg = (os.path.join(d, n) for n in ("pub.pem", "sig.der", "msg"))
        for path, mode, val in ((pk, "w", pubkey_pem), (sg, "wb", sig), (mg, "wb", message)):
            with open(path, mode) as fh: fh.write(val)
        r = subprocess.run(["openssl", "dgst", "-sha256", "-verify", pk, "-signature", sg, mg], capture_output=True, timeout=15)
        return r.returncode == 0


def key_fingerprint(pubkey_pem):
    try:
        der = _openssl("pkey", "-pubin", "-outform", "DER", input=pubkey_pem.encode())
    except RuntimeError:
        return None
    return hashlib.sha256(der).hexdigest()


def spki_fingerprint(crt_path):
    """SHA-256 of the DER SubjectPublicKeyInfo: the value DroidCommand's existing CertificatePinner pins (key, not chain)."""
    pub = _openssl("x509", "-in", crt_path, "-pubkey", "-noout")
    der = _openssl("pkey", "-pubin", "-outform", "DER", input=pub)
    return hashlib.sha256(der).hexdigest()
