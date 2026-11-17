"""Model library: hashes, split sets, change/corruption detection, remembered settings, per-PC history, API."""
import json
import os
import time

import pytest

import modellib
from test_common import make_gguf
from test_control_api import control, request  # noqa: F401  (fixture reuse)


def wait_job(job, timeout=10):
    t = time.time()
    while job.status()["state"] == "running" and time.time() - t < timeout:
        time.sleep(0.02)
    return job.status()


def test_hash_detects_change_and_corruption(tmp_path):
    m = tmp_path / "a-Q4_K_M.gguf"
    make_gguf(m, pad=5000)
    lib = modellib.Library(str(tmp_path / "lib.json"))
    assert lib.integrity(str(m))["state"] == "unhashed"
    assert lib.hash_model(str(m))["state"] == "ok"
    assert oct(os.stat(tmp_path / "lib.json").st_mode & 0o777) == "0o600"
    # same size and mtime but different bytes: silent corruption
    st = os.stat(m)
    data = bytearray(m.read_bytes()); data[-1] ^= 1; m.write_bytes(bytes(data))
    os.utime(m, (st.st_atime, st.st_mtime))
    assert lib.hash_model(str(m))["state"] == "mismatch"
    lib.reset_hash(str(m))
    assert lib.hash_model(str(m))["state"] == "ok"
    with open(m, "ab") as f:
        f.write(b"x")
    assert lib.integrity(str(m))["state"] == "changed"


def test_expected_checksum(tmp_path):
    m = tmp_path / "b.gguf"; make_gguf(m)
    lib = modellib.Library(str(tmp_path / "lib.json"))
    with pytest.raises(ValueError):
        lib.set_expected(str(m), "abc")
    lib.set_expected(str(m), "0" * 64)
    assert lib.hash_model(str(m))["state"] == "mismatch"
    lib.reset_hash(str(m))
    import hashlib
    lib.set_expected(str(m), hashlib.sha256(m.read_bytes()).hexdigest())
    r = lib.hash_model(str(m))
    assert r["state"] == "ok" and r["matches_expected"]


def test_split_sets(tmp_path):
    first = tmp_path / "big-00001-of-00002.gguf"; make_gguf(first)
    lib = modellib.Library(str(tmp_path / "lib.json"))
    parts, complete = modellib.split_parts(str(first))
    assert len(parts) == 2 and not complete
    assert lib.integrity(str(first))["state"] == "incomplete"
    with pytest.raises(ValueError):
        lib.hash_model(str(first))
    (tmp_path / "big-00002-of-00002.gguf").write_bytes(b"\0" * 100)
    assert lib.hash_model(str(first))["state"] == "ok"
    assert len(lib.entry(str(first))["hash"]["parts"]) == 2


def test_quant_names():
    assert modellib.quant_name({"general.file_type": 15}, "x.gguf") == "Q4_K_M"
    assert modellib.quant_name({}, "/m/Qwen3-8B-IQ4_XS.gguf") == "IQ4_XS"
    assert modellib.quant_name({}, "/m/model-bf16.gguf") == "BF16"
    assert modellib.quant_name({}, "/m/model.gguf") is None


def test_remember_validates_and_history_is_per_pc(tmp_path):
    lib = modellib.Library(str(tmp_path / "lib.json"))
    p = str(tmp_path / "m.gguf")
    for bad in ({"backend": "metal"}, {"ctx": 10}, {"ctx_auto": "yes"}, {"gpu_layers": -5}, {"threads": -1}):
        with pytest.raises(ValueError):
            lib.remember(p, bad)
    assert lib.remember(p, {"backend": "vulkan", "ctx": 8192, "ctx_auto": False, "gpu_layers": 20, "junk": 1}) == \
        {"backend": "vulkan", "ctx": 8192, "ctx_auto": False, "gpu_layers": 20}
    a = modellib.pc_signature("Ryzen 7", ["RX 7800"], 32 * 2**30)
    b = modellib.pc_signature("Ryzen 7", ["RX 7800"], 16 * 2**30)
    assert a != b and len(a) == 12 and modellib.pc_signature("Ryzen 7", ["RX 7800"], 32.2 * 2**30) == a
    lib.record_run(p, a, "vulkan", "RX 7800", 99, 8192, True)
    lib.record_run(p, b, "cpu", None, 0, 4096, False, "oom")
    assert lib.worked_here(p, a)["backend"] == "vulkan"
    assert lib.worked_here(p, b) is None
    for _ in range(30):
        lib.record_run(p, a, "vulkan", "RX 7800", 99, 8192, True)
    assert len(lib.entry(p)["history"]) == modellib.HISTORY_MAX


def test_hash_job_and_describe(tmp_path):
    ms = []
    for i in range(3):
        m = tmp_path / f"m{i}-Q8_0.gguf"; make_gguf(m, pad=20000); ms.append(str(m))
    lib = modellib.Library(str(tmp_path / "lib.json"))
    job = modellib.HashJob(lib)
    job.start(ms)
    with pytest.raises(RuntimeError):
        if job.thread.is_alive():
            job.start(ms)
        else:
            raise RuntimeError
    st = wait_job(job)
    assert st["state"] == "done" and st["done"] == st["total"] and st["finished"] == 3
    rows = lib.describe([{"path": p, "name": os.path.basename(p), "size": 1} for p in ms], "pc1", lambda p: {"general.architecture": "llama"})
    assert all(r["integrity"]["state"] == "ok" and r["quant"] == "Q8_0" and r["arch"] == "llama" for r in rows)


def test_library_api(control, tmp_path):  # noqa: F811
    port, headers = control
    models = tmp_path / "data" / "models"; models.mkdir(parents=True)
    m = models / "tiny-Q4_K_M.gguf"; make_gguf(m, pad=4000)
    st, body = request(port, "GET", "/api/library")
    assert st == 401
    st, body = request(port, "GET", "/api/library", headers=headers)
    j = json.loads(body)
    assert st == 200 and j["models"][0]["integrity"]["state"] == "unhashed" and j["models"][0]["quant"] == "Q4_K_M"
    st, body = request(port, "POST", "/api/library", {"action": "remember", "path": "/etc/passwd"}, headers)
    assert st == 400
    st, body = request(port, "POST", "/api/library", {"action": "hash-all"}, headers)
    assert st == 200, body
    import control_center as cc
    wait_job(cc.APP.hash_job)
    st, body = request(port, "POST", "/api/library", {"action": "remember", "path": str(m),
                                                      "settings": {"backend": "cpu", "ctx": 2048, "ctx_auto": False}}, headers)
    assert st == 200, body
    st, body = request(port, "POST", "/api/library", {"action": "apply", "path": str(m)}, headers)
    assert st == 200
    j = json.loads(request(port, "GET", "/api/library", headers=headers)[1])
    row = j["models"][0]
    assert row["integrity"]["state"] == "ok" and row["settings"]["ctx"] == 2048 and j["selected"] == str(m)
    st, body = request(port, "POST", "/api/library", {"action": "hash", "path": str(m), "expected": "nothex"}, headers)
    assert st == 400


ROOT = os.path.join(os.path.dirname(__file__), "..")
DEB_LIB = os.path.join(ROOT, "debian-edition/config/includes.chroot_after_packages/usr/local/lib/victorlinux")
DEB_CTL = os.path.join(ROOT, "debian-edition/config/includes.chroot_after_packages/usr/local/bin/victor-ctl")


def test_debian_uses_the_same_module():
    a = open(os.path.join(ROOT, "airootfs/usr/local/lib/victorlinux/modellib.py"), "rb").read()
    assert a == open(os.path.join(DEB_LIB, "victorlib/modellib.py"), "rb").read()


def test_debian_ctl_library_hash_remember(tmp_path):
    import subprocess
    import sys
    data = tmp_path / "data"; (data / "models").mkdir(parents=True); (data / "settings").mkdir()
    os.symlink(data, tmp_path / "root")
    make_gguf(data / "models" / "tiny-Q5_K_M.gguf", pad=3000)
    env = dict(os.environ, VICTOR_RUN=str(tmp_path), PYTHONPATH=DEB_LIB)

    def ctl(*args):
        r = subprocess.run([sys.executable, DEB_CTL, "--json", *args], env=env, capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, r.stderr
        return json.loads(r.stdout)
    rows = ctl("model", "library")
    assert rows[0]["name"] == "tiny-Q5_K_M.gguf" and rows[0]["quant"] == "Q5_K_M" and rows[0]["integrity"]["state"] == "unhashed"
    assert ctl("model", "hash")["tiny-Q5_K_M.gguf"]["state"] == "ok"
    assert ctl("model", "remember", "tiny-Q5_K_M.gguf", "--set", "backend=cpu", "ctx=2048") == {"backend": "cpu", "ctx": 2048}
    assert ctl("model", "library")[0]["settings"]["ctx"] == 2048
