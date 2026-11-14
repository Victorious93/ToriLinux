"""The single authoritative persistent store (SQLite, WAL, synchronous=FULL).

Only the coordinator process opens it for writing. All mutations happen in transactions so that
restart at any point leaves a consistent state; leases, attempts and fences are persisted.
"""
import hashlib, json, os, secrets, sqlite3, threading, time
from contextlib import contextmanager

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS devices(
  id TEXT PRIMARY KEY, name TEXT NOT NULL, pubkey_pem TEXT NOT NULL, key_fpr TEXT NOT NULL,
  token_hash TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'paired',      -- paired | revoked
  enabled INTEGER NOT NULL DEFAULT 1, paused INTEGER NOT NULL DEFAULT 0,
  created_ms INTEGER NOT NULL, last_seen_ms INTEGER, caps_json TEXT, caps_ms INTEGER,
  device_state_json TEXT, cred_version INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS pairings(secret_hash TEXT PRIMARY KEY, expires_ms INTEGER NOT NULL, used INTEGER NOT NULL DEFAULT 0, label TEXT);
CREATE TABLE IF NOT EXISTS plans(id TEXT PRIMARY KEY, title TEXT, state TEXT NOT NULL, created_ms INTEGER NOT NULL, approved_ms INTEGER, spec_json TEXT);
CREATE TABLE IF NOT EXISTS jobs(
  id TEXT PRIMARY KEY, plan_id TEXT, op TEXT NOT NULL, args_json TEXT NOT NULL, priority INTEGER NOT NULL DEFAULT 5,
  deadline_ms INTEGER, state TEXT NOT NULL, deps_json TEXT NOT NULL DEFAULT '[]', inputs_json TEXT NOT NULL DEFAULT '[]',
  device_pin TEXT, requires_root INTEGER NOT NULL DEFAULT 0, ui_exclusive INTEGER NOT NULL DEFAULT 0,
  idempotent INTEGER NOT NULL DEFAULT 0, max_attempts INTEGER NOT NULL DEFAULT 1, attempts INTEGER NOT NULL DEFAULT 0,
  min_battery INTEGER NOT NULL DEFAULT 0, min_free_bytes INTEGER NOT NULL DEFAULT 0,
  result_json TEXT, error_json TEXT, note TEXT, cancel_requested INTEGER NOT NULL DEFAULT 0,
  coordinator_local INTEGER NOT NULL DEFAULT 0, created_ms INTEGER NOT NULL, updated_ms INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS jobs_state ON jobs(state);
CREATE TABLE IF NOT EXISTS attempts(
  job_id TEXT NOT NULL, n INTEGER NOT NULL, device_id TEXT NOT NULL, state TEXT NOT NULL,
  lease_expires_ms INTEGER, started_ms INTEGER NOT NULL, finished_ms INTEGER, progress REAL, note TEXT,
  PRIMARY KEY(job_id, n));
CREATE TABLE IF NOT EXISTS artifacts(
  id TEXT PRIMARY KEY, job_id TEXT NOT NULL, attempt INTEGER NOT NULL, device_id TEXT, name TEXT NOT NULL,
  size INTEGER NOT NULL, sha256 TEXT NOT NULL, rel_path TEXT NOT NULL, created_ms INTEGER NOT NULL, owner_job_final INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS outbox(seq INTEGER PRIMARY KEY AUTOINCREMENT, device_id TEXT NOT NULL, msg_id TEXT NOT NULL UNIQUE,
  body TEXT NOT NULL, created_ms INTEGER NOT NULL, delivered_ms INTEGER, acked INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS seen(msg_id TEXT NOT NULL, sender TEXT NOT NULL, at_ms INTEGER NOT NULL, PRIMARY KEY(sender, msg_id));
CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT, at_ms INTEGER NOT NULL, level TEXT NOT NULL, kind TEXT NOT NULL, job_id TEXT, device_id TEXT, detail TEXT);
"""
SEEN_RETENTION_MS = 7 * 24 * 3600 * 1000


def sha256_hex(s):
    return hashlib.sha256(s.encode() if isinstance(s, str) else s).hexdigest()


class Store:
    def __init__(self, directory):
        self.dir = directory
        os.makedirs(directory, mode=0o700, exist_ok=True)
        os.makedirs(os.path.join(directory, "artifacts"), mode=0o700, exist_ok=True)
        self.path = os.path.join(directory, "fleet.db")
        self._lock = threading.RLock()
        self.db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None, timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript(SCHEMA)
        os.chmod(self.path, 0o600)

    @contextmanager
    def tx(self):
        with self._lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                yield self.db
            except BaseException:
                self.db.execute("ROLLBACK")
                raise
            else:
                self.db.execute("COMMIT")

    def q(self, sql, args=()):
        with self._lock:
            return self.db.execute(sql, args).fetchall()

    def one(self, sql, args=()):
        with self._lock:
            return self.db.execute(sql, args).fetchone()

    def close(self):
        with self._lock:
            self.db.close()

    # --- meta / events ---
    def meta(self, k, default=None):
        r = self.one("SELECT v FROM meta WHERE k=?", (k,))
        return r["v"] if r else default

    def set_meta(self, k, v):
        with self.tx() as db:
            db.execute("INSERT INTO meta(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, str(v)))

    def event(self, kind, level="info", job_id=None, device_id=None, detail=None, db=None):
        d = db or self.db
        d.execute("INSERT INTO events(at_ms,level,kind,job_id,device_id,detail) VALUES(?,?,?,?,?,?)",
                  (int(time.time() * 1000), level, kind, job_id, device_id, detail))

    # --- pairing / devices ---
    def create_pairing(self, ttl_s=600, label=None):
        secret = secrets.token_urlsafe(24)
        with self.tx() as db:
            db.execute("INSERT INTO pairings(secret_hash,expires_ms,label) VALUES(?,?,?)",
                       (sha256_hex(secret), int((time.time() + ttl_s) * 1000), label))
            self.event("pairing_created", db=db)
        return secret

    def consume_pairing(self, secret, db):
        """Single-use: atomically mark used if present, unexpired and unused."""
        h = sha256_hex(secret)
        cur = db.execute("UPDATE pairings SET used=1 WHERE secret_hash=? AND used=0 AND expires_ms>?",
                         (h, int(time.time() * 1000)))
        return cur.rowcount == 1

    def new_credential(self):
        tok = secrets.token_urlsafe(32)
        return tok, sha256_hex(tok)

    def device_by_token(self, token):
        return self.one("SELECT * FROM devices WHERE token_hash=?", (sha256_hex(token),))

    def prune_seen(self):
        with self.tx() as db:
            db.execute("DELETE FROM seen WHERE at_ms<?", (int(time.time() * 1000) - SEEN_RETENTION_MS,))

    def mark_seen(self, sender, msg_id, db):
        """Return True if this is the first time (persisted dedup)."""
        try:
            db.execute("INSERT INTO seen(msg_id,sender,at_ms) VALUES(?,?,?)", (msg_id, sender, int(time.time() * 1000)))
            return True
        except sqlite3.IntegrityError:
            return False
