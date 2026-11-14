import os, struct, json, hashlib, pytest
import victorcommon as tc


def make_gguf(path, arch="llama", layers=4, heads=4, kv=2, emb=64, pad=0):
    def s(x):
        b = x.encode(); return struct.pack("<Q", len(b)) + b
    kvs = [("general.architecture", 8, s(arch)), (f"{arch}.block_count", 4, struct.pack("<I", layers)),
           (f"{arch}.attention.head_count", 4, struct.pack("<I", heads)),
           (f"{arch}.attention.head_count_kv", 4, struct.pack("<I", kv)),
           (f"{arch}.embedding_length", 4, struct.pack("<I", emb)),
           ("tokenizer.ggml.tokens", 9, struct.pack("<IQ", 8, 2) + s("a") + s("b"))]
    body = b"".join(s(k) + struct.pack("<I", t) + v for k, t, v in kvs)
    with open(path, "wb") as f:
        f.write(b"GGUF" + struct.pack("<IQQ", 3, 0, len(kvs)) + body + b"\0" * pad)


def test_valid_header_and_estimate(tmp_path):
    p = tmp_path / "m.gguf"; make_gguf(p, pad=1000)
    h = tc.read_gguf_header(str(p))
    assert h["meta"]["llama.block_count"] == 4
    e = tc.estimate_memory(str(p), 1024)
    # 4 layers * 1024 ctx * 2 kv heads * (16+16) * 2 bytes
    assert e["kv_cache"] == 4 * 1024 * 2 * 32 * 2


def test_rejects_bad_files(tmp_path):
    p = tmp_path / "x.gguf"; p.write_bytes(b"NOPE" + b"\0" * 100)
    with pytest.raises(tc.GGUFError):
        tc.read_gguf_header(str(p))
    q = tmp_path / "t.gguf"; make_gguf(q); q.write_bytes(q.read_bytes()[:30])
    with pytest.raises(tc.GGUFError):
        tc.read_gguf_header(str(q))


def test_import_checksum(tmp_path):
    src = tmp_path / "a.gguf"; make_gguf(src, pad=5000)
    good = hashlib.sha256(src.read_bytes()).hexdigest()
    dest = tmp_path / "models"
    with pytest.raises(ValueError, match="mismatch"):
        tc.import_model(str(src), str(dest), "0" * 64)
    assert not list(dest.glob("*"))            # nothing left behind
    out, d = tc.import_model(str(src), str(dest), good)
    assert d == good and os.path.exists(out)
    with pytest.raises(ValueError, match="already exists"):
        tc.import_model(str(src), str(dest))


def test_import_rejects_non_gguf(tmp_path):
    p = tmp_path / "a.bin"; p.write_bytes(b"x")
    with pytest.raises(ValueError):
        tc.import_model(str(p), str(tmp_path / "d"))


def test_split_detection(tmp_path):
    p = tmp_path / "m-00001-of-00003.gguf"; make_gguf(p)
    ok, why = tc.check_split_complete(str(p))
    assert not ok and "missing" in why


def test_split_estimate_counts_all_shards_and_lists_first_only(tmp_path, monkeypatch):
    monkeypatch.setenv("VICTOR_DATA", str(tmp_path))
    models = tmp_path / "models"; models.mkdir()
    a, b = models / "m-00001-of-00002.gguf", models / "m-00002-of-00002.gguf"
    make_gguf(a, pad=1000); make_gguf(b, pad=2000)
    assert tc.estimate_memory(str(a), 1024)["weights"] == a.stat().st_size + b.stat().st_size
    assert [m["path"] for m in tc.list_models()] == [str(a)]


def test_split_import_never_silently_copies_only_one_shard(tmp_path):
    a, b = tmp_path / "m-00001-of-00002.gguf", tmp_path / "m-00002-of-00002.gguf"
    make_gguf(a); make_gguf(b)
    with pytest.raises(ValueError, match="complete shard directory"):
        tc.import_model(str(a), str(tmp_path / "dest"))
    assert not (tmp_path / "dest" / a.name).exists()


def test_fit_report():
    est = {"weights": 20 << 30, "total": 22 << 30, "kv_known": True}
    r = tc.fit_report(est, {"available": 8 << 30}, 16 << 30)
    assert r["level"] in ("warn", "bad")


def test_backend_choice_never_claims_missing(monkeypatch):
    monkeypatch.setattr(tc, "backends", lambda: {"vulkan": None, "hip": None, "cuda": None, "cpu": "/x"})
    b, why = tc.choose_backend("auto", [{"vendor": "amd"}], {"available": True, "devices": []})
    assert b == "cpu"
    b, why = tc.choose_backend("vulkan", [], None)
    assert b is None and "not included" in why


def test_settings_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("VICTOR_DATA", str(tmp_path))
    s = tc.load_settings(); s["ctx"] = 8192; tc.save_settings(s)
    assert tc.load_settings()["ctx"] == 8192
    assert oct(os.stat(tmp_path / "config" / "settings.json").st_mode)[-3:] == "600"


def test_legacy_saved_model_paths_follow_selected_storage(tmp_path, monkeypatch):
    monkeypatch.setenv("VICTOR_DATA", str(tmp_path))
    cfg = tmp_path / "config"; cfg.mkdir()
    (cfg / "settings.json").write_text(json.dumps({
        "model": "/mnt/toridata/tori/models/model.gguf",
        "extra_model_dirs": ["/mnt/toridata/tori/models/extra", "/media/other"],
    }))
    settings = tc.load_settings()
    assert settings["model"] == str(tmp_path / "models/model.gguf")
    assert settings["extra_model_dirs"] == [str(tmp_path / "models/extra"), "/media/other"]
