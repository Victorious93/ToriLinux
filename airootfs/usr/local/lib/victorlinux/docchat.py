"""Private document Q&A: index folders the owner chose, search them, answer with the local model. Fully offline.

* Only paths the owner added are read (no system folders, no VictorLinux secrets). Symlinks are not followed.
* Text is extracted locally (plain text, Markdown, code, CSV/JSON, HTML; PDF only when `pdftotext` exists).
* Search is hybrid: BM25 keyword ranking always, plus cosine similarity when a local llama-server embeddings
  endpoint is available (a separate embedding GGUF, or the chat server if it was started with embeddings).
* The index is one SQLite file (0600) on the owner's storage. Nothing is uploaded; there is no cloud fallback.
Standard library only; shared by both editions.
"""
import array
import html.parser
import json
import math
import os
import re
import shutil
import sqlite3
import subprocess
import threading
import time
import urllib.request

TEXT_EXT = {".txt", ".md", ".markdown", ".rst", ".org", ".csv", ".tsv", ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg",
            ".log", ".py", ".sh", ".js", ".ts", ".c", ".h", ".cpp", ".hpp", ".rs", ".go", ".java", ".kt", ".swift", ".rb",
            ".php", ".sql", ".tex", ".xml", ".srt"}
HTML_EXT = {".html", ".htm", ".xhtml"}
PDF_EXT = {".pdf"}
MAX_FILE = 20 * 1024 * 1024
CHUNK, OVERLAP = 1200, 200
FORBIDDEN = ("/proc", "/sys", "/dev", "/run", "/boot", "/etc", "/usr", "/bin", "/sbin", "/lib", "/lib64", "/var/lib", "/root")
TOKEN_RE = re.compile(r"[A-Za-z0-9\u00C0-\u024F]{2,}")
STOP = frozenset("the and for are but not you all any can had her was one our out day get has him his how man new now old see two way "
                 "who boy did its let put say she too use that with have this will your from they know want been good much some time "
                 "what when which there their would about could into than then them these other more also only".split())


def now():
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


class DocError(ValueError):
    pass


def check_source(path, forbidden_extra=()):
    """Resolve and validate a folder or file the owner wants indexed."""
    if not isinstance(path, str) or not path.strip():
        raise DocError("Enter a folder or file path.")
    p = os.path.realpath(os.path.expanduser(path.strip()))
    if not os.path.exists(p):
        raise DocError(f"Not found: {p}")
    if p == "/":
        raise DocError("Choose a specific folder, not the whole filesystem.")
    for f in FORBIDDEN + tuple(forbidden_extra):
        f = os.path.realpath(f)
        if p == f or p.startswith(f.rstrip("/") + "/"):
            raise DocError(f"{p} is a system or private VictorLinux location and cannot be indexed.")
    return p


class _HTMLText(html.parser.HTMLParser):
    def __init__(self):
        super().__init__()
        self.out, self.skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.skip += 1
        elif tag in ("p", "br", "div", "li", "h1", "h2", "h3", "h4", "tr"):
            self.out.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self.skip:
            self.skip -= 1

    def handle_data(self, data):
        if not self.skip:
            self.out.append(data)


def extract_text(path):
    """Returns text, or None for unsupported/binary files."""
    ext = os.path.splitext(path)[1].lower()
    if os.path.getsize(path) > MAX_FILE:
        return None
    if ext in PDF_EXT:
        exe = shutil.which("pdftotext")
        if not exe:
            return None
        r = subprocess.run([exe, "-q", "-layout", path, "-"], capture_output=True, timeout=120)
        return r.stdout.decode("utf-8", "replace") if r.returncode == 0 else None
    if ext not in TEXT_EXT and ext not in HTML_EXT:
        return None
    with open(path, "rb") as f:
        raw = f.read()
    if b"\0" in raw[:4096]:
        return None
    text = raw.decode("utf-8", "replace")
    if ext in HTML_EXT:
        p = _HTMLText()
        p.feed(text)
        text = "".join(p.out)
    return text


def chunk_text(text, size=CHUNK, overlap=OVERLAP):
    text = re.sub(r"[ \t]+", " ", text.replace("\r", ""))
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    out, i = [], 0
    while i < len(text):
        end = min(len(text), i + size)
        if end < len(text):
            cut = max(text.rfind("\n\n", i, end), text.rfind(". ", i, end))
            if cut > i + size // 2:
                end = cut + 1
        piece = text[i:end].strip()
        if piece:
            out.append(piece)
        if end >= len(text):
            break
        i = max(end - overlap, i + 1)
    return out


def tokens(text):
    return [t for t in (w.lower() for w in TOKEN_RE.findall(text)) if t not in STOP]


def _pack(vec):
    return array.array("f", vec).tobytes()


def _unpack(blob):
    a = array.array("f")
    a.frombytes(blob)
    return a


def _norm(v):
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


class Embedder:
    """OpenAI-compatible /v1/embeddings on a local llama-server. base_url must be loopback."""

    def __init__(self, base_url, key=None, model="victor-embed", timeout=120):
        if not re.match(r"^http://(127\.0\.0\.1|localhost|\[::1\]):\d+$", base_url or ""):
            raise DocError("Embeddings must come from a local llama-server on 127.0.0.1.")
        self.base, self.key, self.model, self.timeout = base_url, key, model, timeout

    def embed(self, texts):
        req = urllib.request.Request(self.base + "/v1/embeddings", method="POST",
                                     data=json.dumps({"model": self.model, "input": list(texts)}).encode(),
                                     headers={"Content-Type": "application/json", **({"Authorization": "Bearer " + self.key} if self.key else {})})
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            body = json.loads(r.read().decode())
        data = sorted(body.get("data") or [], key=lambda d: d.get("index", 0))
        if len(data) != len(texts):
            raise DocError("Embedding server returned the wrong number of vectors.")
        return [_norm([float(x) for x in d["embedding"]]) for d in data]


class Index:
    def __init__(self, path):
        self.path = path
        self.lock = threading.RLock()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        new = not os.path.exists(path)
        if new:
            os.close(os.open(path, os.O_WRONLY | os.O_CREAT, 0o600))
        with self.db() as c:
            c.executescript("""
            CREATE TABLE IF NOT EXISTS sources(path TEXT PRIMARY KEY, added TEXT);
            CREATE TABLE IF NOT EXISTS files(path TEXT PRIMARY KEY, source TEXT, size INT, mtime INT, chunks INT, indexed TEXT, error TEXT);
            CREATE TABLE IF NOT EXISTS chunks(id INTEGER PRIMARY KEY, path TEXT, ord INT, text TEXT, vec BLOB, dim INT);
            CREATE INDEX IF NOT EXISTS chunks_path ON chunks(path);
            CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);""")

    def db(self):
        c = sqlite3.connect(self.path, timeout=30)
        c.execute("PRAGMA journal_mode=DELETE")
        return c

    # -- sources ---------------------------------------------------------------------------------------
    def sources(self):
        with self.db() as c:
            rows = c.execute("SELECT s.path, s.added, COUNT(f.path), COALESCE(SUM(f.chunks),0) FROM sources s "
                             "LEFT JOIN files f ON f.source = s.path GROUP BY s.path ORDER BY s.path").fetchall()
        return [{"path": p, "added": a, "files": n, "chunks": k, "exists": os.path.exists(p)} for p, a, n, k in rows]

    def add_source(self, path, forbidden_extra=()):
        p = check_source(path, forbidden_extra)
        with self.lock, self.db() as c:
            c.execute("INSERT OR IGNORE INTO sources VALUES(?,?)", (p, now()))
        return p

    def remove_source(self, path):
        with self.lock, self.db() as c:
            paths = [r[0] for r in c.execute("SELECT path FROM files WHERE source=?", (path,))]
            for fp in paths:
                c.execute("DELETE FROM chunks WHERE path=?", (fp,))
            c.execute("DELETE FROM files WHERE source=?", (path,))
            c.execute("DELETE FROM sources WHERE path=?", (path,))
        return len(paths)

    def stats(self):
        with self.db() as c:
            files = c.execute("SELECT COUNT(*), COALESCE(SUM(chunks),0) FROM files WHERE error IS NULL").fetchone()
            vec = c.execute("SELECT COUNT(*) FROM chunks WHERE vec IS NOT NULL").fetchone()[0]
            meta = dict(c.execute("SELECT key, value FROM meta").fetchall())
        return {"files": files[0], "chunks": files[1], "embedded": vec, "embed_model": meta.get("embed_model"), "dim": int(meta.get("dim") or 0)}

    # -- indexing --------------------------------------------------------------------------------------
    def walk(self, source, exclude=()):
        """Private/system subtrees inside a chosen parent folder are pruned, never read."""
        if os.path.isfile(source):
            yield source
            return
        skip = {os.path.realpath(x) for x in FORBIDDEN + tuple(exclude)}
        for root, dirs, files in os.walk(source, followlinks=False):
            dirs[:] = sorted(d for d in dirs if not d.startswith(".") and os.path.realpath(os.path.join(root, d)) not in skip)
            for fn in sorted(files):
                if fn.startswith("."):
                    continue
                p = os.path.join(root, fn)
                if os.path.islink(p):
                    continue
                ext = os.path.splitext(fn)[1].lower()
                if ext in TEXT_EXT or ext in HTML_EXT or ext in PDF_EXT:
                    yield p

    def build(self, embedder=None, embed_model=None, progress=None, cancel=None, batch=16, exclude=()):
        """Incremental: unchanged files are skipped, deleted files are dropped, new/changed files are re-chunked."""
        st = {"scanned": 0, "indexed": 0, "skipped": 0, "removed": 0, "errors": 0, "embedded": 0}
        with self.db() as c:
            meta = dict(c.execute("SELECT key, value FROM meta").fetchall())
        if embedder and meta.get("embed_model") and meta["embed_model"] != embed_model:
            with self.lock, self.db() as c:          # a different embedding model makes old vectors meaningless
                c.execute("UPDATE chunks SET vec=NULL, dim=NULL")
                c.execute("UPDATE files SET mtime=-1")
        seen = set()
        for src in [s["path"] for s in self.sources()]:
            if not os.path.exists(src):
                continue
            for p in self.walk(src, exclude):
                if cancel is not None and cancel.is_set():
                    return st | {"cancelled": True}
                seen.add(p)
                st["scanned"] += 1
                try:
                    fs = os.stat(p)
                except OSError:
                    continue
                with self.db() as c:
                    row = c.execute("SELECT size, mtime, error FROM files WHERE path=?", (p,)).fetchone()
                if row and row[0] == fs.st_size and row[1] == int(fs.st_mtime) and not row[2]:
                    st["skipped"] += 1
                    if progress:
                        progress(st)
                    continue
                err, pieces = None, []
                try:
                    text = extract_text(p)
                    pieces = chunk_text(text) if text else []
                except (OSError, subprocess.SubprocessError, UnicodeError) as e:
                    err = str(e)
                vecs = [None] * len(pieces)
                if embedder and pieces:
                    try:
                        vecs = []
                        for i in range(0, len(pieces), batch):
                            vecs += embedder.embed(pieces[i:i + batch])
                        st["embedded"] += len(vecs)
                    except (OSError, ValueError) as e:
                        vecs, err = [None] * len(pieces), None
                        st["embed_error"] = f"Embeddings unavailable ({e}); keyword search still works."
                        embedder = None
                with self.lock, self.db() as c:
                    c.execute("DELETE FROM chunks WHERE path=?", (p,))
                    c.executemany("INSERT INTO chunks(path, ord, text, vec, dim) VALUES(?,?,?,?,?)",
                                  [(p, i, t, _pack(v) if v else None, len(v) if v else None) for i, (t, v) in enumerate(zip(pieces, vecs))])
                    c.execute("INSERT OR REPLACE INTO files VALUES(?,?,?,?,?,?,?)",
                              (p, src, fs.st_size, int(fs.st_mtime), len(pieces), now(), err))
                    if any(vecs):
                        c.execute("INSERT OR REPLACE INTO meta VALUES('embed_model', ?)", (embed_model or "unknown",))
                        c.execute("INSERT OR REPLACE INTO meta VALUES('dim', ?)", (str(len(next(v for v in vecs if v))),))
                st["errors" if err else "indexed"] += 1
                if progress:
                    progress(st)
        with self.lock, self.db() as c:
            gone = [r[0] for r in c.execute("SELECT path FROM files") if r[0] not in seen]
            for p in gone:
                c.execute("DELETE FROM chunks WHERE path=?", (p,))
                c.execute("DELETE FROM files WHERE path=?", (p,))
            st["removed"] = len(gone)
        return st

    # -- search ----------------------------------------------------------------------------------------
    def search(self, query, k=6, embedder=None):
        q = tokens(query)
        with self.db() as c:
            rows = c.execute("SELECT id, path, ord, text, vec FROM chunks").fetchall()
        if not rows:
            return []
        docs = [tokens(r[3]) for r in rows]
        n = len(rows)
        avg = sum(len(d) for d in docs) / n or 1.0
        df = {}
        for d in docs:
            for t in set(d):
                df[t] = df.get(t, 0) + 1
        bm = []
        for d in docs:
            tf = {}
            for t in d:
                tf[t] = tf.get(t, 0) + 1
            s = 0.0
            for t in set(q):
                if t in tf:
                    idf = math.log(1 + (n - df[t] + 0.5) / (df[t] + 0.5))
                    s += idf * tf[t] * 2.2 / (tf[t] + 1.2 * (0.25 + 0.75 * len(d) / avg))
            bm.append(s)
        top = max(bm) or 1.0
        bm = [x / top for x in bm]
        cos = None
        mode = "keyword"
        if embedder is not None and any(r[4] for r in rows):
            try:
                qv = embedder.embed([query])[0]
                cos = []
                for r in rows:
                    v = _unpack(r[4]) if r[4] else None
                    cos.append(sum(a * b for a, b in zip(qv, v)) if v and len(v) == len(qv) else 0.0)
                mode = "hybrid"
            except (OSError, ValueError):
                cos = None
        scores = [(0.6 * cos[i] + 0.4 * bm[i]) if cos else bm[i] for i in range(n)]
        order = sorted(range(n), key=lambda i: scores[i], reverse=True)
        out = []
        for i in order[:k]:
            if scores[i] <= 0:
                break
            r = rows[i]
            out.append({"id": r[0], "path": r[1], "ord": r[2], "text": r[3], "score": round(scores[i], 4), "mode": mode})
        return out


def build_prompt(question, hits):
    ctx = "\n\n".join(f"[{i + 1}] ({os.path.basename(h['path'])})\n{h['text']}" for i, h in enumerate(hits))
    system = ("You answer questions using only the numbered excerpts from the owner's own documents. Cite excerpts like [1]. "
              "If the excerpts do not contain the answer, say so plainly. Do not invent sources.")
    return [{"role": "system", "content": system}, {"role": "user", "content": f"Excerpts:\n{ctx}\n\nQuestion: {question}"}]


def answer(question, hits, chat_url, key, max_tokens=512, timeout=600):
    """Ask the loaded local model. chat_url must be loopback."""
    if not re.match(r"^http://(127\.0\.0\.1|localhost|\[::1\]):\d+$", chat_url or ""):
        raise DocError("Answers must come from the local model on 127.0.0.1.")
    req = urllib.request.Request(chat_url + "/v1/chat/completions", method="POST",
                                 data=json.dumps({"model": "victor-local", "max_tokens": max_tokens, "temperature": 0.2,
                                                  "messages": build_prompt(question, hits)}).encode(),
                                 headers={"Content-Type": "application/json", "Authorization": "Bearer " + key})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = json.loads(r.read().decode())
    return body["choices"][0]["message"]["content"]


class IndexJob:
    def __init__(self):
        self.state = {"state": "idle"}
        self.thread = None
        self.cancel = threading.Event()

    def running(self):
        return bool(self.thread and self.thread.is_alive())

    def start(self, index, embedder_factory, embed_model=None, on_done=None, exclude=()):
        if self.running():
            raise RuntimeError("Indexing is already running.")
        self.cancel.clear()
        self.state = {"state": "running", "started": now(), "progress": {}}

        def run():
            emb, model = None, embed_model
            try:
                got = embedder_factory()
                emb, model = got if isinstance(got, tuple) else (got, embed_model)
            except Exception as e:  # noqa: BLE001 - keyword index still useful
                self.state["embed_error"] = str(e)
            try:
                res = index.build(emb, model, progress=lambda st: self.state.__setitem__("progress", dict(st)),
                                  cancel=self.cancel, exclude=exclude)
                self.state.update(state="cancelled" if res.get("cancelled") else "done", result=res, finished=now(), semantic=bool(emb))
            except Exception as e:  # noqa: BLE001
                self.state.update(state="error", error=f"{type(e).__name__}: {e}")
            finally:
                if on_done:
                    on_done()
        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()
        return dict(self.state)
