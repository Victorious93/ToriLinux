import importlib.util
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest
import victorcommon as tc

ROOT = Path(__file__).resolve().parents[1]


def test_intel_hardware_can_select_vulkan(monkeypatch):
    monkeypatch.setattr(tc, "backends", lambda: {"vulkan": "/vk", "cpu": "/cpu", "cuda": None, "hip": None})
    backend, _ = tc.choose_backend("auto", [{"vendor": "intel"}], {
        "available": True, "devices": [{"deviceName": "Intel Arc", "deviceType": "DISCRETE_GPU"}],
    })
    assert backend == "vulkan"


def test_software_vulkan_is_never_claimed_as_gpu(monkeypatch):
    monkeypatch.setattr(tc, "backends", lambda: {"vulkan": "/vk", "cpu": "/cpu", "cuda": None, "hip": None})
    backend, _ = tc.choose_backend("auto", [{"vendor": "amd"}], {
        "available": True, "devices": [{"deviceName": "llvmpipe", "deviceType": "CPU"}],
    })
    assert backend == "cpu"


def test_new_and_legacy_data_directories_are_reused(tmp_path):
    path = ROOT / "airootfs/usr/local/lib/victorlinux/victor-priv"
    spec = importlib.util.spec_from_loader("victor_priv_test", SourceFileLoader("victor_priv_test", str(path)))
    helper = importlib.util.module_from_spec(spec); spec.loader.exec_module(helper)
    assert helper.persistence_root(str(tmp_path)) == str(tmp_path / "victorlinux")
    (tmp_path / "tori").mkdir()
    assert helper.persistence_root(str(tmp_path)) == str(tmp_path / "tori")
    (tmp_path / "victorlinux").mkdir()
    with pytest.raises(ValueError, match="Both"):
        helper.persistence_root(str(tmp_path))


def test_20_gib_gpu_recommends_14b_and_explains_32b_limit():
    import model_advisor as advisor
    rec = advisor.recommend(32 * 2**30, 20 * 2**30, "vulkan", 4096)
    models = {m["name"]: m for m in rec}
    assert models["Qwen3-14B Q4_K_M"]["fit"] == "recommended"
    assert models["Qwen3-32B Q4_K_M"]["fit"] != "recommended"


def test_cpu_and_missing_runtime_do_not_promise_gpu():
    import model_advisor as advisor
    for model in advisor.recommend(3 * 2**30, None, "cpu", 4096):
        assert "GPU" not in model["reason"]
    assert all(m["fit"] == "runtime unavailable" for m in advisor.recommend(32 * 2**30, 20 * 2**30, None, 4096))
