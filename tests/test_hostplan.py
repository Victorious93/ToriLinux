"""Per-host planning: memory attribution, automatic context, rankings and verification records."""
import pytest
import victorcommon as tc
import hostplan as hp
from test_common import make_gguf

GIB = 2**30


def gpu(vendor, total, used=0, card="card0", name="GPU"):
    return {"vendor": vendor, "card": card, "name": name, "vram_total": total, "vram_used": used}


def ram(avail_gib):
    return {"total": 64 * GIB, "available": avail_gib * GIB}


def test_20_gib_gpu_budget_comes_from_detected_free_memory_not_a_model_name():
    d = hp.select_device("vulkan", [gpu("amd", 20 * GIB, 1 * GIB, name="Unknown future card")], ram(32),
                         {"devices": [{"deviceName": "Unknown future card", "deviceType": "DISCRETE_GPU"}]})
    assert d["kind"] == "dedicated" and d["free"] == 19 * GIB
    assert d["budget"] == int(19 * GIB * hp.GPU_FRACTION)


def test_rx_6800_xt_16_gib():
    d = hp.select_device("hip", [gpu("amd", 16 * GIB, name="RX 6800 XT")], ram(16))
    assert d["kind"] == "dedicated" and d["budget"] < 16 * GIB


def test_vram_and_ram_are_never_added_together():
    d = hp.select_device("cuda", [gpu("nvidia", 8 * GIB, card="nvidia0")], ram(64))
    assert d["budget"] <= 8 * GIB


def test_integrated_graphics_use_system_ram_not_vram():
    d = hp.select_device("vulkan", [gpu("amd", 512 * 2**20)], ram(16),
                         {"devices": [{"deviceName": "Radeon Graphics", "deviceType": "INTEGRATED_GPU"}]})
    assert d["kind"] == "shared" and d["free"] == 16 * GIB
    assert hp.plan_layers({"total": 1, "weights": 1, "overhead": 0, "kv_cache": 0, "n_layer": 4}, d, 4096) == 999


def test_unknown_gpu_memory_is_not_invented():
    d = hp.select_device("vulkan", [{"vendor": "intel", "card": "card0", "name": "Intel GPU", "vram_total": None}], ram(8),
                         {"devices": [{"deviceName": "Intel(R) Arc", "deviceType": "DISCRETE_GPU"}]})
    assert d["kind"] == "unknown" and d["free"] == 8 * GIB and any("unknown" in n.lower() for n in d["notes"])


def test_software_renderer_is_not_a_device():
    d = hp.select_device("vulkan", [gpu("amd", 16 * GIB)], ram(8),
                         {"devices": [{"deviceName": "llvmpipe", "deviceType": "CPU"}]})
    assert d["kind"] == "system" and d["device"] is None


def test_cpu_only_and_low_ram():
    d = hp.select_device("cpu", [], ram(2))
    assert d["kind"] == "system" and d["budget"] == int(2 * GIB * hp.RAM_FRACTION)


def test_multi_gpu_not_pooled_cuda_picks_most_free_and_pins():
    d = hp.select_device("cuda", [gpu("nvidia", 12 * GIB, 0, "nvidia0"), gpu("nvidia", 24 * GIB, 20 * GIB, "nvidia1")], ram(32))
    assert d["device"]["card"] == "nvidia0" and d["index"] == 0 and len(d["others"]) == 1
    assert d["budget"] < 12 * GIB


def test_multi_gpu_vulkan_plans_against_smallest_card():
    d = hp.select_device("vulkan", [gpu("amd", 16 * GIB, 0, "card0"), gpu("nvidia", 8 * GIB, 0, "card1")], ram(32),
                         {"devices": [{"deviceName": "a", "deviceType": "DISCRETE_GPU"}, {"deviceName": "b", "deviceType": "DISCRETE_GPU"}]})
    assert d["ambiguous"] and d["free"] == 8 * GIB


def test_auto_context_scales_with_budget_and_is_capped_by_training_context():
    kv = lambda c: c * 100_000          # noqa: E731  (100 kB per token)
    big, ok1, _ = hp.auto_context(4 * GIB, kv, 2**28, 64 * GIB, ctx_train=8192)
    assert big == 8192 and ok1
    small, ok2, _ = hp.auto_context(4 * GIB, kv, 2**28, int(4.6 * GIB), ctx_train=131072)
    assert small < big and ok2
    tight, ok3, _ = hp.auto_context(4 * GIB, kv, 2**28, 3 * GIB)
    assert tight == hp.CTX_MIN and not ok3


def test_auto_context_unknown_kv_stays_conservative():
    assert hp.auto_context(1, lambda c: None, 1, 10**12)[0] == 4096


def test_stronger_pc_profile_cannot_leak_to_smaller_pc(tmp_path, monkeypatch):
    """Measured facts are never in settings, so only the owner's preference moves between PCs."""
    monkeypatch.setenv("VICTOR_DATA", str(tmp_path))
    (tmp_path / "config").mkdir()
    s = tc.load_settings(); s["model"] = ""; tc.save_settings(s)
    assert not any(k in s for k in ("vram_free", "budget", "host", "device"))
    assert s["ctx_auto"] is True


def test_old_explicit_ctx_choice_is_preserved(tmp_path, monkeypatch):
    monkeypatch.setenv("VICTOR_DATA", str(tmp_path))
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "settings.json").write_text('{"ctx": 16384}')
    s = tc.load_settings()
    assert s["ctx"] == 16384 and s["ctx_auto"] is False


def test_cpu_list_parsing_and_default_threads(monkeypatch):
    assert hp._count_cpu_list("0,8") == 2 and hp._count_cpu_list("0-1") == 2 and hp._count_cpu_list("3") == 1
    monkeypatch.setattr(hp, "physical_threads", lambda: 16)
    assert hp.default_threads() == 15
    monkeypatch.setattr(hp, "physical_threads", lambda: 4)
    assert hp.default_threads() == 4


def test_local_ranking_marks_unknown_arch_and_verification(tmp_path, monkeypatch):
    monkeypatch.setenv("VICTOR_DATA", str(tmp_path))
    (tmp_path / "config").mkdir(); (tmp_path / "models").mkdir()
    a = tmp_path / "models" / "known.gguf"; make_gguf(a, arch="llama", pad=5000)
    b = tmp_path / "models" / "weird.gguf"; make_gguf(b, arch="zzzarch", pad=5000)
    c = tmp_path / "models" / "broken.gguf"; c.write_bytes(b"GGUF" + b"\0" * 8)
    dev = hp.select_device("cpu", [], ram(8))
    models = tc.list_models()
    rows = {r["name"]: r for r in hp.rank_local(models, ram(8), dev, "cpu")}
    assert rows["known.gguf"]["arch_known"] and rows["known.gguf"]["verified"] == "untested"
    assert not rows["weird.gguf"]["arch_known"] and "not in this build's known list" in rows["weird.gguf"]["reason"]
    assert rows["broken.gguf"]["fit"] == "unusable"
    assert hp.record_verified(str(a), "cpu", None, 0, 4096)
    rows = {r["name"]: r for r in hp.rank_local(models, ram(8), dev, "cpu")}
    assert rows["known.gguf"]["verified"] == "verified-here"
    assert hp.rank_local(models, ram(8), dev, "cpu")[0]["name"] == "known.gguf"
    # a record made on other hardware, or for a changed file, is not trusted here
    assert hp.verified_state(str(a), "vulkan", "Some GPU") == "verified-elsewhere"
    a.write_bytes(a.read_bytes() + b"x")
    assert hp.verified_state(str(a), "cpu", None) == "untested"


def test_intel_gpu_enumerated_from_sysfs(tmp_path, monkeypatch):
    drm = tmp_path / "card0" / "device"; drm.mkdir(parents=True)
    (drm / "vendor").write_text("0x8086\n"); (drm / "device").write_text("0x56a0\n"); (drm / "class").write_text("0x030000\n")
    real_isdir, real_listdir = tc.os.path.isdir, tc.os.listdir
    monkeypatch.setattr(tc.os.path, "isdir", lambda p: True if p == "/sys/class/drm" else real_isdir(p))
    monkeypatch.setattr(tc.os, "listdir", lambda p: ["card0"] if p == "/sys/class/drm" else real_listdir(p))
    orig_join = tc.os.path.join
    monkeypatch.setattr(tc.os.path, "join", lambda *a: orig_join(str(tmp_path), *a[1:]) if a[0] == "/sys/class/drm" else orig_join(*a))
    monkeypatch.setattr(tc.shutil, "which", lambda x: None)
    g = tc.gpus()
    assert g and g[0]["vendor"] == "intel" and g[0]["vram_total"] is None
