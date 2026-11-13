# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""准备页：环境自检、模型档位推荐与一键配置（engine.setup_check + Bridge）。"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.bridge import Bridge
from engine import setup_check
from engine.model_manager import MODELS, ModelSpec, ModelState
from engine.setup_check import (
    MODEL_TIERS,
    VERDICT_CPU_SLOW,
    VERDICT_NO_DISK,
    VERDICT_READY,
    VERDICT_USABLE,
    check_rows,
    model_verdicts,
    plan_fixes,
)

GB = 1024 ** 3


def test_vram_probe_hides_console_and_keeps_output_timeout(monkeypatch):
    import subprocess
    captured = {}
    monkeypatch.setattr(setup_check.shutil, "which", lambda _: "nvidia-smi")

    def run(command, **kwargs):
        captured.update(kwargs)
        return subprocess.CompletedProcess(command, 0, stdout="8192\n12288\n", stderr="")

    monkeypatch.setattr(setup_check.subprocess, "run", run)
    assert setup_check.detect_vram_mb() == 12288
    assert captured["creationflags"] == getattr(subprocess, "CREATE_NO_WINDOW", 0)
    assert captured["capture_output"] and captured["text"] and captured["timeout"] == 5


def _state(name: str, verified: bool) -> ModelState:
    spec = ModelSpec(name, "repo/" + name, "约 1 GB", "测试")
    return ModelState(spec=spec, installed=verified, verified=verified, path=None, bytes_on_disk=0)


def _verdicts(**kwargs) -> dict[str, object]:
    return {v.name: v for v in model_verdicts(**kwargs)}


def test_tiers_match_model_manager_catalog():
    """档位表与模型目录同名同序，防止两表漂移。"""
    assert [t.name for t in MODEL_TIERS] == [m.name for m in MODELS]


def test_big_gpu_all_ready_prefers_installed_then_small():
    fresh = _verdicts(cuda_available=True, vram_mb=12 * 1024, disk_free_bytes=100 * GB, installed={})
    assert fresh["large-v3"].verdict == VERDICT_READY
    assert fresh["small"].recommended, "全新安装推荐 small 先试用"
    installed = _verdicts(
        cuda_available=True, vram_mb=12 * 1024, disk_free_bytes=100 * GB,
        installed={"large-v3": True},
    )
    assert installed["large-v3"].recommended, "已安装且可用时优先推荐已安装的最高档"


def test_mid_gpu_medium_int8_large_cpu_slow():
    m = _verdicts(cuda_available=True, vram_mb=4 * 1024, disk_free_bytes=100 * GB, installed={})
    assert m["medium"].verdict == VERDICT_READY
    assert "int8" in m["medium"].detail
    assert m["large-v3"].verdict == VERDICT_CPU_SLOW
    assert m["small"].recommended


def test_cpu_only_light_usable_heavy_slow():
    m = _verdicts(cuda_available=False, vram_mb=None, disk_free_bytes=100 * GB, installed={})
    assert m["tiny"].verdict == VERDICT_USABLE
    assert m["small"].verdict == VERDICT_USABLE
    assert m["small"].recommended
    assert m["medium"].verdict == VERDICT_CPU_SLOW
    assert m["large-v3"].verdict == VERDICT_CPU_SLOW


def test_low_disk_marks_no_disk_and_keeps_light_recommendation():
    tight = _verdicts(cuda_available=True, vram_mb=8 * 1024, disk_free_bytes=int(0.9 * GB), installed={})
    assert tight["medium"].verdict == VERDICT_NO_DISK
    assert tight["large-v3"].verdict == VERDICT_NO_DISK
    assert tight["small"].verdict == VERDICT_READY
    assert tight["small"].recommended
    empty = _verdicts(cuda_available=True, vram_mb=8 * 1024, disk_free_bytes=0, installed={})
    assert all(v.verdict == VERDICT_NO_DISK for v in empty.values())
    assert not any(v.recommended for v in empty.values())


def test_vram_unknown_stays_optimistic_on_cuda():
    m = _verdicts(cuda_available=True, vram_mb=None, disk_free_bytes=100 * GB, installed={})
    assert m["large-v3"].verdict == VERDICT_READY


def test_check_rows_statuses(tmp_path):
    rows = check_rows(
        cuda_probe={"available": False, "error": "未检测到 NVIDIA CUDA 设备"},
        vram_mb=None,
        disk_free_bytes=5 * GB,
        cache_root=tmp_path / "hf",
        settings={"hf_endpoint": "", "whisper_model": "tiny"},
        installed={},
    )
    by_key = {r["key"]: r for r in rows}
    assert by_key["gpu"]["status"] == "warn"
    assert by_key["disk"]["status"] == "ok"
    assert by_key["mirror"]["status"] == "warn"
    assert by_key["cache_dir"]["status"] == "warn"
    assert by_key["default_model"]["status"] == "warn"


def test_plan_fixes_covers_and_is_idempotent(tmp_path):
    cache = tmp_path / "hf"
    settings = {"hf_endpoint": "  ", "whisper_model": "tiny"}
    states = [_state("tiny", False), _state("base", True)]
    fixes = plan_fixes(cache_root=cache, settings=settings, states=states)
    assert {f["action"] for f in fixes} == {"mkdir", "set_endpoint", "switch_default"}
    switch = next(f for f in fixes if f["action"] == "switch_default")
    assert switch["value"] == "base"
    # 模拟一键配置落地后，再计划应为空（幂等）
    cache.mkdir(parents=True)
    settings.update({"hf_endpoint": setup_check.DEFAULT_HF_ENDPOINT, "whisper_model": "base"})
    assert plan_fixes(cache_root=cache, settings=settings, states=states) == []


def test_plan_fixes_prefers_highest_installed(tmp_path):
    settings = {"hf_endpoint": "https://x", "whisper_model": "medium"}
    states = [_state("small", True), _state("large-v3", True), _state("medium", False)]
    fixes = plan_fixes(cache_root=tmp_path, settings=settings, states=states)
    assert len(fixes) == 1
    assert fixes[0]["action"] == "switch_default"
    assert fixes[0]["value"] == "large-v3"


class _Controller:
    def __init__(self, settings):
        self.settings = settings
        self.saved = []

    def save_settings(self, changes):
        self.settings.update(changes)
        self.saved.append(dict(changes))
        return self.settings


class _Worker:
    running = False
    pid = None


def _make_bridge(tmp_path: Path, settings: dict):
    controller = _Controller(settings)
    store = type("Store", (), {"base_dir": tmp_path})()
    bridge = Bridge(controller, _Worker(), store, window_provider=lambda: None, on_quit=lambda: None)
    return bridge, controller


@pytest.fixture
def cpu_probes(monkeypatch):
    monkeypatch.setattr(
        "engine.asr.probe_cuda",
        lambda *args, **kwargs: {"available": False, "device_count": 0, "compute_types": [], "error": "测试环境无 CUDA"},
    )
    monkeypatch.setattr("engine.setup_check.detect_vram_mb", lambda: None)


def test_bridge_setup_check_and_apply(tmp_path, cpu_probes):
    settings = {
        "model_dir": str(tmp_path / "hf"),
        "hf_endpoint": "",
        "whisper_model": "tiny",
        "device": "cpu",
    }
    bridge, controller = _make_bridge(tmp_path, settings)
    check = bridge.get_setup_check()
    assert check["ok"]
    assert {r["key"] for r in check["rows"]} >= {"gpu", "mirror", "cache_dir", "default_model"}
    assert {f["action"] for f in check["fixes"]} == {"mkdir", "set_endpoint"}
    verdict = {v["name"]: v for v in check["tiers"]}
    assert verdict["small"]["recommended"]
    assert verdict["small"]["verdict"] == VERDICT_USABLE
    assert verdict["large-v3"]["verdict"] == VERDICT_CPU_SLOW

    result = bridge.apply_setup_fixes()
    assert sorted(result["applied"]) == sorted(f["label"] for f in check["fixes"])
    assert controller.settings["hf_endpoint"] == setup_check.DEFAULT_HF_ENDPOINT
    assert (tmp_path / "hf").is_dir()
    assert result["fixes"] == []
    assert bridge.apply_setup_fixes()["applied"] == []


def test_bridge_setup_check_recommends_per_hardware(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "engine.asr.probe_cuda",
        lambda *args, **kwargs: {"available": True, "device_count": 1, "compute_types": ["float16"]},
    )
    monkeypatch.setattr("engine.setup_check.detect_vram_mb", lambda: 12 * 1024)
    (tmp_path / "hf").mkdir()
    settings = {
        "model_dir": str(tmp_path / "hf"),
        "hf_endpoint": "https://hf-mirror.com",
        "whisper_model": "small",
        "device": "cuda",
    }
    bridge, _ = _make_bridge(tmp_path, settings)
    check = bridge.get_setup_check()
    assert check["ok"]
    assert check["vram_mb"] == 12 * 1024
    verdict = {v["name"]: v for v in check["tiers"]}
    assert verdict["large-v3"]["verdict"] == VERDICT_READY
    assert verdict["small"]["recommended"]
    gpu_row = next(r for r in check["rows"] if r["key"] == "gpu")
    assert gpu_row["status"] == "ok"
    assert check["fixes"] == []
