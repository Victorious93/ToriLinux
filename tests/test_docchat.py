"""Offline document Q&A: chosen paths only, local extraction, BM25 + local embeddings, loopback-only model calls."""
import http.server
import json
import os
import subprocess
import sys
import threading
import time

import pytest

import docchat
from test_control_api import control, request  # noqa: F401  (fixture reuse)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEB_LIB = os.path.join(ROOT, "debian-edition/config/includes.chroot_after_packages/usr/local/lib/victorlinux")
DEB_CTL = os.path.join(ROOT, "debian-edition/config/includes.chroot_after_packages/usr/local/bin/victor-ctl")


class Stub(http.server.BaseHTTPRequestHandler):
    """Fake local llama-server: deterministic bag-of-words embeddings and a chat endpoint that echoes citations."""
    seen = []
    VOCAB = ["cat", "pet", "deposit", "bread", "flour", "yeast", "invoice", "tax"]

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        Stub.seen.append((self.path, self.headers.get("Authorization"), body))
        if self.path == "/v1/embeddings":
            data = [{"index": i, "embedding": [t.lower().count(w) + 0.01 for w in self.VOCAB]} for i, t in enumerate(body["input"])]
            out = {"data": data}
        else:
            out = {"choices": [{"message": {"content": "A cat is allowed [1]."}}]}
        raw = json.dumps(out).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


@pytest.fixture
def stub():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Stub)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    Stub.seen = []
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()
    srv.server_close()


def docs(tmp_path):
    d = tmp_path / "notes"
    (d / "sub").mkdir(parents=True)
    (d / "lease.md").write_text("# Lease\n\nPets: one cat is allowed with a deposit of 300 dollars.\n")
    (d / "sub" / "recipe.txt").write_text("Bread needs flour, water, salt and yeast.")
    (d / "page.html").write_text("<html><script>var tax=1</script><p>Invoice total is 40 dollars.</p></html>")
    (d / "photo.jpg").write_bytes(b"\xff\xd8\x00binary")
    (d / ".hidden").mkdir()
    (d / ".hidden" / "secret.txt").write_text("cat password")
    return d


def test_check_source_refuses_root_system_and_private(tmp_path):
    for bad in ("/", "/etc", "/proc", "/usr/share"):
        with pytest.raises(docchat.DocError):
            docchat.check_source(bad)
    priv = tmp_path / "secrets"; priv.mkdir()
    with pytest.raises(docchat.DocError):
        docchat.check_source(str(priv), [str(priv)])
    with pytest.raises(docchat.DocError):
        docchat.check_source(str(tmp_path / "missing"))
    with pytest.raises(docchat.DocError):
        docchat.check_source("  ")
    assert docchat.check_source(str(tmp_path)) == os.path.realpath(tmp_path)    # a parent of a private dir is fine; it is pruned


def test_extract_and_chunk(tmp_path):
    d = docs(tmp_path)
    assert "cat is allowed" in docchat.extract_text(str(d / "lease.md"))
    html = docchat.extract_text(str(d / "page.html"))
    assert "Invoice total" in html and "var tax" not in html
    assert docchat.extract_text(str(d / "photo.jpg")) is None
    chunks = docchat.chunk_text("word " * 1000, size=300, overlap=50)
    assert len(chunks) > 10 and all(len(c) <= 300 for c in chunks)
    assert docchat.chunk_text("") == []


def test_keyword_index_incremental_and_private_pruning(tmp_path):
    d = docs(tmp_path)
    private = d / "keys"; private.mkdir(); (private / "id.txt").write_text("cat secret key")
    os.symlink("/etc/hostname", d / "link.txt")
    idx = docchat.Index(str(tmp_path / "idx" / "index.sqlite"))
    assert oct(os.stat(idx.path).st_mode & 0o777) == "0o600"
    idx.add_source(str(d))
    st = idx.build(exclude=[str(private)])
    assert st["indexed"] == 3 and st["errors"] == 0
    paths = {h["path"] for h in idx.search("cat", k=10)}
    assert paths == {str(d / "lease.md")}                          # hidden dir, symlink and excluded dir never read
    hits = idx.search("flour yeast")
    assert hits[0]["path"].endswith("recipe.txt") and hits[0]["mode"] == "keyword"
    assert idx.build(exclude=[str(private)])["skipped"] == 3                             # unchanged files are not re-read
    time.sleep(1.1)
    (d / "lease.md").write_text("Pets are not allowed any more.")
    os.unlink(d / "sub" / "recipe.txt")
    st = idx.build(exclude=[str(private)])
    assert st["indexed"] == 1 and st["removed"] == 1
    assert idx.search("flour") == []
    assert idx.remove_source(str(d)) == 2 and idx.stats()["files"] == 0
    assert (d / "lease.md").exists()                               # removing from the index never touches files


def test_embedder_must_be_loopback():
    for bad in ("http://10.0.0.5:8080", "https://api.example.com", "http://127.0.0.1.evil.com:80", ""):
        with pytest.raises(docchat.DocError):
            docchat.Embedder(bad)
    with pytest.raises(docchat.DocError):
        docchat.answer("q", [], "http://example.com:80", "k")


def test_hybrid_search_and_answer_with_local_stub(tmp_path, stub):
    d = docs(tmp_path)
    idx = docchat.Index(str(tmp_path / "i.sqlite"))
    idx.add_source(str(d))
    emb = docchat.Embedder(stub, "upkey")
    st = idx.build(emb, "embed.gguf")
    assert st["embedded"] >= 3 and idx.stats()["embed_model"] == "embed.gguf" and idx.stats()["dim"] == 8
    hits = idx.search("pet deposit", embedder=emb)
    assert hits[0]["path"].endswith("lease.md") and hits[0]["mode"] == "hybrid"
    ans = docchat.answer("Can I keep a cat?", hits, stub, "upkey")
    assert ans == "A cat is allowed [1]."
    path, auth, body = Stub.seen[-1]
    assert path == "/v1/chat/completions" and auth == "Bearer upkey"
    assert "[1] (lease.md)" in body["messages"][1]["content"]
    # switching embedding model invalidates old vectors and re-embeds everything
    assert idx.build(emb, "other.gguf")["indexed"] == 3


def test_index_job_runs_in_background(tmp_path):
    d = docs(tmp_path)
    idx = docchat.Index(str(tmp_path / "j.sqlite"))
    idx.add_source(str(d))
    job = docchat.IndexJob()
    job.start(idx, lambda: (None, None))
    job.thread.join(10)
    assert job.state["state"] == "done" and job.state["semantic"] is False and job.state["result"]["indexed"] == 3


def test_docs_api_offline_flow(control, tmp_path):  # noqa: F811
    port, headers = control
    d = docs(tmp_path)
    st, raw = request(port, "GET", "/api/docs", None, {})
    assert st == 401
    st, raw = request(port, "POST", "/api/docs", {"action": "add", "path": "/etc"}, headers)
    assert st == 400 and b"system or private" in raw
    st, raw = request(port, "POST", "/api/docs", {"action": "add", "path": str(tmp_path / "cache")}, headers)
    assert st == 400                                                # session-token folder is private
    st, raw = request(port, "POST", "/api/docs", {"action": "add", "path": str(d)}, headers)
    assert st == 200, raw
    st, raw = request(port, "POST", "/api/docs", {"action": "index"}, headers)
    assert st == 200, raw
    import control_center as cc
    cc.APP.doc_job.thread.join(10)
    j = json.loads(request(port, "GET", "/api/docs", None, headers)[1])
    assert j["stats"]["files"] == 3 and j["job"]["state"] == "done" and j["embed"] == {"model": "", "running": False}
    j = json.loads(request(port, "POST", "/api/docs", {"action": "ask", "question": "is a cat allowed"}, headers)[1])
    assert j["hits"][0]["name"] == "lease.md" and "answer" not in j and "Start a model" in j["message"]
    assert request(port, "POST", "/api/docs", {"action": "ask", "question": ""}, headers)[0] == 400
    assert request(port, "POST", "/api/docs", {"action": "ask", "question": "x" * 2001}, headers)[0] == 400
    st, raw = request(port, "POST", "/api/docs", {"action": "set-embed", "model": "/tmp/not-a-model.gguf"}, headers)
    assert st == 400
    assert request(port, "POST", "/api/docs", {"action": "bogus"}, headers)[0] == 400
    idx = os.path.join(tmp_path, "data", "docs", "index.sqlite")
    assert oct(os.stat(idx).st_mode & 0o777) == "0o600"


def test_debian_uses_the_same_module():
    a = open(os.path.join(ROOT, "airootfs/usr/local/lib/victorlinux/docchat.py"), "rb").read()
    assert a == open(os.path.join(DEB_LIB, "victorlib/docchat.py"), "rb").read()


def test_debian_ctl_docs(tmp_path):
    d = docs(tmp_path)
    data = tmp_path / "data"
    for sub in ("models", "settings", "secrets"):
        (data / sub).mkdir(parents=True)
    os.symlink(data, tmp_path / "root")
    env = dict(os.environ, VICTOR_RUN=str(tmp_path), PYTHONPATH=DEB_LIB, HOME=str(tmp_path))

    def ctl(*args, rc=0):
        r = subprocess.run([sys.executable, DEB_CTL, "--json", *args], env=env, capture_output=True, text=True, timeout=60)
        assert r.returncode == rc, r.stderr
        return json.loads(r.stdout) if rc == 0 else r.stderr
    assert "system or private" in ctl("docs", "add", str(data / "secrets"), rc=2)
    assert ctl("docs", "add", str(d))["added"] == str(d)
    st = ctl("docs", "index")
    assert st["indexed"] == 3 and st["semantic"] is False
    r = ctl("docs", "ask", "flour and yeast")
    assert r["hits"][0]["path"].endswith("recipe.txt") and "answer" not in r
    assert ctl("docs", "list")["stats"]["files"] == 3
