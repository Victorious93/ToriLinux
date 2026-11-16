"""Encrypted backups of VictorLinux app state (settings, chats, small state files) and a guided, verified restore.

* Encryption: OpenPGP symmetric (gpg, AES-256, SHA-512 S2K with maximum iterations, integrity protected) with a
  passphrase the owner types. The passphrase goes to gpg through a private pipe: never on a command line, in a log,
  in the environment or on disk. gpg runs with a throwaway home and without passphrase caching.
* Secrets (API keys, TLS keys) are left out unless the owner explicitly includes them.
* Restore is two steps: inspect (decrypt in memory, verify every file hash) and restore with explicit confirmation.
  Files that would be overwritten are first copied to a pre-restore folder. Only known relative paths are written.
Standard library + gpg; shared by both editions. Models are not backed up (they are large and re-importable).
"""
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import time

FORMAT, VERSION = "victorlinux-backup", 1
SUFFIX = ".vlbak"
MIN_PASS = 10
MAX_TOTAL = 2 * 1024 ** 3
NAME_RE = re.compile(r"^victorlinux-backup-\d{8}-\d{6}" + re.escape(SUFFIX) + r"$")


class BackupError(RuntimeError):
    pass


def now():
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def check_passphrase(p):
    if not isinstance(p, str) or len(p) < MIN_PASS:
        raise BackupError(f"Use a passphrase of at least {MIN_PASS} characters. Keep it safe: without it the backup cannot be opened.")
    if len(set(p)) < 4:
        raise BackupError("That passphrase is too repetitive. Use a few unrelated words.")


def _gpg(args, data, passphrase):
    exe = shutil.which("gpg")
    if not exe:
        raise BackupError("gpg is not installed, so backups cannot be encrypted.")
    home = tempfile.mkdtemp(prefix="vlgpg-")
    os.chmod(home, 0o700)
    r_fd, w_fd = os.pipe()
    try:
        os.write(w_fd, passphrase.encode() + b"\n")
        os.close(w_fd)
        w_fd = None
        cmd = [exe, "--homedir", home, "--batch", "--yes", "--quiet", "--no-tty", "--no-symkey-cache", "--pinentry-mode", "loopback",
               "--passphrase-fd", str(r_fd), *args]
        p = subprocess.run(cmd, input=data, capture_output=True, pass_fds=(r_fd,), timeout=600)
    finally:
        if w_fd is not None:
            os.close(w_fd)
        os.close(r_fd)
        subprocess.run(["gpgconf", "--homedir", home, "--kill", "gpg-agent"], capture_output=True, timeout=20) if shutil.which("gpgconf") else None
        shutil.rmtree(home, ignore_errors=True)
    return p


def encrypt(data, passphrase):
    p = _gpg(["--symmetric", "--cipher-algo", "AES256", "--s2k-mode", "3", "--s2k-digest-algo", "SHA512", "--s2k-count", "65011712",
              "--compress-algo", "none", "--force-mdc", "-o", "-"], data, passphrase)
    if p.returncode != 0 or not p.stdout:
        raise BackupError("Encryption failed.")
    return p.stdout


def decrypt(blob, passphrase):
    p = _gpg(["--decrypt", "-o", "-"], blob, passphrase)
    if p.returncode != 0:
        raise BackupError("Wrong passphrase, or the backup file is damaged.")
    return p.stdout


def _sha(b):
    return hashlib.sha256(b).hexdigest()


def collect(root, items):
    """items: relative files or folders under root. Regular files only; symlinks skipped. Returns [(rel, bytes)]."""
    out, total = [], 0
    for item in items:
        base = os.path.join(root, item)
        if os.path.islink(base) or not os.path.exists(base):
            continue
        walk = [(os.path.dirname(base), [], [os.path.basename(base)])] if os.path.isfile(base) else os.walk(base, followlinks=False)
        for d, dirs, files in walk:
            dirs[:] = sorted(x for x in dirs if not os.path.islink(os.path.join(d, x)))
            for fn in sorted(files):
                p = os.path.join(d, fn)
                if os.path.islink(p) or not os.path.isfile(p) or ".tmp." in fn:
                    continue
                with open(p, "rb") as f:
                    b = f.read()
                total += len(b)
                if total > MAX_TOTAL:
                    raise BackupError("More than 2 GiB of app data; clean up chats/outputs first (models are never backed up).")
                out.append((os.path.relpath(p, root), b))
    return out


def create(root, out_dir, passphrase, items, secret_items=(), include_secrets=False, pc=None):
    check_passphrase(passphrase)
    files = collect(root, list(items) + (list(secret_items) if include_secrets else []))
    if not include_secrets:
        secret = {os.path.normpath(s) for s in secret_items}
        files = [(r, b) for r, b in files if not any(r == s or r.startswith(s + "/") for s in secret)]
    if not files:
        raise BackupError("Nothing to back up yet.")
    manifest = {"format": FORMAT, "version": VERSION, "created": now(), "pc": pc, "includes_secrets": bool(include_secrets),
                "files": [{"path": r, "size": len(b), "sha256": _sha(b)} for r, b in files]}
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as t:
        for name, b in [("manifest.json", json.dumps(manifest, indent=1).encode())] + [("data/" + r, b) for r, b in files]:
            ti = tarfile.TarInfo(name)
            ti.size, ti.mode, ti.mtime = len(b), 0o600, int(time.time())
            t.addfile(ti, io.BytesIO(b))
    blob = encrypt(buf.getvalue(), passphrase)
    os.makedirs(out_dir, mode=0o700, exist_ok=True)
    for _ in range(3):
        path = os.path.join(out_dir, "victorlinux-backup-" + time.strftime("%Y%m%d-%H%M%S") + SUFFIX)
        if not os.path.exists(path):
            break
        time.sleep(1.1)
    else:
        raise BackupError("Could not choose a backup file name.")
    fd = os.open(path + ".part", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(blob)
        f.flush()
        os.fsync(f.fileno())
    os.replace(path + ".part", path)
    return {"path": path, "size": len(blob), "files": len(files), "includes_secrets": bool(include_secrets), "created": manifest["created"]}


def list_backups(out_dir):
    try:
        names = sorted((n for n in os.listdir(out_dir) if NAME_RE.match(n)), reverse=True)
    except OSError:
        return []
    return [{"name": n, "path": os.path.join(out_dir, n), "size": os.path.getsize(os.path.join(out_dir, n))} for n in names]


def _safe(name, allowed):
    if not name.startswith("data/"):
        return None
    rel = name[5:]
    norm = os.path.normpath(rel)
    if rel != norm or norm.startswith(("/", "..")) or "/../" in "/" + norm + "/" or "\\" in norm or "\0" in norm:
        return None
    if not any(norm == a or norm.startswith(a.rstrip("/") + "/") for a in allowed):
        return None
    return norm


def open_backup(path, passphrase, allowed):
    """Decrypt in memory and verify. Returns (manifest, {rel: bytes}). Raises BackupError on any mismatch."""
    if not os.path.isfile(path):
        raise BackupError("Backup file not found.")
    with open(path, "rb") as f:
        raw = decrypt(f.read(), passphrase)
    files, manifest = {}, None
    try:
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as t:
            for m in t.getmembers():
                if not m.isfile():
                    raise BackupError(f"Unexpected entry in backup: {m.name}")
                b = t.extractfile(m).read()
                if m.name == "manifest.json":
                    manifest = json.loads(b)
                    continue
                rel = _safe(m.name, allowed)
                if rel is None:
                    raise BackupError(f"Refusing unsafe or unknown path in backup: {m.name}")
                files[rel] = b
    except (tarfile.TarError, ValueError, OSError) as e:
        raise BackupError(f"Backup content is damaged ({type(e).__name__}).")
    if not manifest or manifest.get("format") != FORMAT or manifest.get("version") != VERSION:
        raise BackupError("Not a VictorLinux backup (or a newer format).")
    listed = {e["path"]: e for e in manifest.get("files", [])}
    if set(listed) != set(files):
        raise BackupError("Backup file list does not match its manifest.")
    for rel, b in files.items():
        if _sha(b) != listed[rel]["sha256"]:
            raise BackupError(f"Checksum mismatch for {rel}.")
    return manifest, files


def inspect(path, passphrase, allowed):
    m, files = open_backup(path, passphrase, allowed)
    return {"created": m.get("created"), "pc": m.get("pc"), "includes_secrets": m.get("includes_secrets"), "verified": True,
            "files": [{"path": e["path"], "size": e["size"]} for e in m["files"]]}


def restore(path, passphrase, root, allowed, keep_dir, confirm=False):
    if confirm is not True:
        raise BackupError("Restore overwrites current settings and chats. Inspect the backup first, then confirm.")
    m, files = open_backup(path, passphrase, allowed)
    keep = os.path.join(keep_dir, "pre-restore-" + time.strftime("%Y%m%d-%H%M%S"))
    saved = 0
    for rel in files:
        cur = os.path.join(root, rel)
        if os.path.isfile(cur) and not os.path.islink(cur):
            dst = os.path.join(keep, rel)
            os.makedirs(os.path.dirname(dst), mode=0o700, exist_ok=True)
            shutil.copy2(cur, dst)
            os.chmod(dst, 0o600)
            saved += 1
    for rel, b in files.items():
        dst = os.path.join(root, rel)
        parent = os.path.dirname(dst)
        os.makedirs(parent, exist_ok=True)
        if os.path.islink(dst) or os.path.realpath(parent) != os.path.normpath(os.path.join(os.path.realpath(root), os.path.dirname(rel))):
            raise BackupError(f"Refusing to write through a symlink: {rel}")
        tmp = dst + ".tmp.restore"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(b)
        os.replace(tmp, dst)
    return {"restored": len(files), "previous_copies": saved, "previous_dir": keep if saved else None, "created": m.get("created")}
