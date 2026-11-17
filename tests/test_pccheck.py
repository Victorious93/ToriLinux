"""Check this PC: evaluation rules, fixes for CPU fallback, saved profiles, API, Debian CLI and module parity."""
import json
import os
import subprocess
import sys

import pytest

import pccheck
from test_control_api import control, request  # noqa: F401

GIB = 2**30


def facts(**over):
    f = {"cpu": {"name": "Ryzen 7 5800X", "threads": 16, "flags": ["avx", "avx2", "fma", "f16c"]},
         "ram": {"total": 32 * GIB, "available": 24 * GIB},
         "gpus": [{"vendor": "amd", "name": "RX 6800 XT", "driver": "amdgpu", "vram_total": 16 * GIB}],
         "render_nodes": [{"path": "/dev/dri/renderD128", "accessible": True}],
         "vulkan": {"available": True, "devices": [{"deviceName": "AMD Radeon RX 6800 XT (RADV NAVI21)", "deviceType": "DISCRETE_GPU", "driverName": "radv"}]},
         "backends": {"vulkan": True, "cpu": True}, "selected": {"backend": "vulkan", "reason": "Hardware Vulkan device detected"},
         "storage": {"mode": "persistent", "free": 500 * GIB}, "speed": None}
    f.update(over)
    return f


def by_id(r):
    return {c["id"]: c for c in r["checks"]}


def test_healthy_pc_is_ok():
    r = pccheck.evaluate(facts())
    assert r["summary"]["verdict"] == "ok" and r["summary"]["counts"]["bad"] == 0
    assert by_id(r)["backend"]["status"] == "ok"


def test_cpu_fallback_collects_concrete_fixes():
    r = pccheck.evaluate(facts(render_nodes=[{"path": "/dev/dri/renderD128", "accessible": False}],
                               vulkan={"available": True, "devices": [{"deviceName": "llvmpipe (LLVM 19)", "deviceType": "CPU"}]},
                               selected={"backend": "cpu", "reason": "No usable hardware Vulkan device"}))
    c = by_id(r)
    assert c["render"]["status"] == "bad" and "usermod -aG render,video" in c["render"]["fix"]
    assert c["vulkan"]["status"] == "warn" and "Software Vulkan is not acceleration" in c["vulkan"]["fix"]
    assert c["backend"]["status"] == "warn" and "usermod" in c["backend"]["fix"]
    assert r["checks"][0]["status"] == "bad" and r["summary"]["verdict"] == "bad"


@pytest.mark.parametrize("driver,status,needle", [("nouveau", "warn", "CUDA image"), ("radeon", "warn", "amdgpu.si_support"),
                                                  (None, "bad", "nomodeset"), ("vfio-pci", "warn", "passthrough")])
def test_driver_rules(driver, status, needle):
    vendor = "nvidia" if driver == "nouveau" else "amd"
    g = [{"vendor": vendor, "name": "GPU", "driver": driver}]
    c = by_id(pccheck.evaluate(facts(gpus=g)))["driver"]
    assert c["status"] == status and needle in (c["fix"] or "")


def test_nvidia_from_nvidia_smi_counts_as_driver_loaded():
    c = by_id(pccheck.evaluate(facts(gpus=[{"vendor": "nvidia", "name": "RTX 4070", "vram_total": 12 * GIB}])))["driver"]
    assert c["status"] == "ok"


def test_weak_cpu_low_ram_temporary_storage_and_no_gpu():
    r = by_id(pccheck.evaluate(facts(cpu={"name": "Celeron", "threads": 2, "flags": ["sse4_2"]}, ram={"total": 4 * GIB, "available": 2 * GIB},
                                     gpus=[], render_nodes=[], vulkan=None, selected={"backend": "cpu", "reason": "No GPU"},
                                     storage={"mode": "temporary"})))
    assert r["cpu"]["status"] == "warn" and r["ram"]["status"] == "warn" and r["storage"]["status"] == "warn"
    assert r["gpu"]["status"] == "info" and r["backend"]["status"] == "info"


def test_no_llama_build_is_bad():
    assert by_id(pccheck.evaluate(facts(backends={"cpu": False})))["backend"]["status"] == "bad"


def test_speed_parsing_and_rating():
    sp = pccheck.speed_from_response({"timings": {"predicted_per_second": 42.5, "prompt_per_second": 900}}, 2.0, 64)
    assert sp["tokens_per_s"] == 42.5 and sp["source"] == "llama-server timings"
    sp2 = pccheck.speed_from_response({"usage": {"completion_tokens": 10}}, 2.0, 64)
    assert sp2["tokens_per_s"] == 5.0
    c = by_id(pccheck.evaluate(facts(speed=dict(sp2, backend="cpu", model="/m/x.gguf"))))["speed"]
    assert c["status"] == "warn" and "5.0 tokens/s" in c["detail"]


def test_cpu_flags():
    assert {"avx2", "fma"} <= pccheck.cpu_flags("processor: 0\nflags\t\t: fpu sse avx2 fma\n")


def test_save_profile_keeps_latest_per_pc_and_prunes(tmp_path):
    f = facts()
    r = pccheck.evaluate(f)
    for _ in range(3):
        saved = pccheck.save_profile(str(tmp_path), f, r)
    prof = json.loads((tmp_path / "profiles.json").read_text())
    assert list(prof) == [saved["pc"]] and prof[saved["pc"]]["verdict"] == "ok"
    assert oct(os.stat(saved["report"]).st_mode & 0o777) == "0o600"
    for i in range(30):
        (tmp_path / f"pc-check-aaaa-2000010{i:02d}.json").write_text("{}")
    pccheck.save_profile(str(tmp_path), f, r)
    assert len([x for x in os.listdir(tmp_path) if x.startswith("pc-check-")]) == 20
    text = pccheck.as_text(r)
    assert text.startswith("VictorLinux PC check: OK")


def test_pccheck_api(control, tmp_path):  # noqa: F811
    port, headers = control
    assert request(port, "GET", "/api/pccheck")[0] == 401
    st, body = request(port, "GET", "/api/pccheck", headers=headers)
    j = json.loads(body)
    assert st == 200 and j["report"]["checks"] and j["saved"]["report"].startswith(str(tmp_path / "data" / "reports"))
    st, body = request(port, "POST", "/api/pccheck/speed", {}, headers)
    assert st == 400 and "Start a model first" in json.loads(body)["error"]


ROOT = os.path.join(os.path.dirname(__file__), "..")
DEB = os.path.join(ROOT, "debian-edition/config/includes.chroot_after_packages/usr/local")


def test_debian_uses_the_same_module():
    assert open(os.path.join(ROOT, "airootfs/usr/local/lib/victorlinux/pccheck.py"), "rb").read() == \
        open(os.path.join(DEB, "lib/victorlinux/victorlib/pccheck.py"), "rb").read()


def test_debian_ctl_diag_check(tmp_path):
    data = tmp_path / "data"; data.mkdir()
    os.symlink(data, tmp_path / "root")
    env = dict(os.environ, VICTOR_RUN=str(tmp_path), PYTHONPATH=os.path.join(DEB, "lib/victorlinux"), VICTOR_LLAMA_DIR=str(tmp_path / "nollama"))
    r = subprocess.run([sys.executable, os.path.join(DEB, "bin/victor-ctl"), "--json", "diag", "check"], env=env, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr
    j = json.loads(r.stdout)
    assert j["text"].startswith("VictorLinux PC check:") and any(c["id"] == "backend" and c["status"] == "bad" for c in j["report"]["checks"])
    assert j["saved"]["report"].startswith(str(data / "reports"))
