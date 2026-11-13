# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""Packaged GPU diagnostics and replacement failures use isolated fixtures."""
from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import threading
import zipfile

import pytest

from app.bridge import Bridge


DLLS = {
    "cublas/bin/cublas64_12.dll": b"cublas",
    "cuda_nvrtc/bin/nvrtc64_120_0.dll": b"nvrtc",
    "cudnn/bin/cudnn64_9.dll": b"cudnn",
}
GPU_REPORT = {
    "available": True, "device_count": 1,
    "compute_types": ["float16", "int8_float16"],
    "dll_dirs": ["fixture-worker/nvidia/cudnn/bin"], "error": "",
}


def bridge_for(internal: Path) -> Bridge:
    bridge = object.__new__(Bridge)
    bridge._lock = threading.RLock()
    bridge._worker_internal_dir = lambda: internal
    return bridge


def write_runtime(root: Path, prefix: bytes = b"old-") -> None:
    for relative, value in DLLS.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(prefix + value)


def write_archive(path: Path, members: dict[str, bytes]) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        for name, content in members.items():
            archive.writestr(name, content)


def assert_runtime(root: Path, prefix: bytes) -> None:
    assert {path.relative_to(root).as_posix(): path.read_bytes()
            for path in root.rglob("*") if path.is_file()} == {
                relative: prefix + value for relative, value in DLLS.items()
            }


def packaged_worker(tmp_path, monkeypatch) -> Path:
    desktop = tmp_path / "FoilPage-Desktop.exe"
    worker = tmp_path / "worker" / "FoilPage-Worker.exe"
    worker.parent.mkdir()
    worker.write_bytes(b"fixture")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(desktop))
    return worker


def test_source_probe_keeps_existing_asr_runtime(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    monkeypatch.setattr("engine.asr.probe_cuda", lambda: GPU_REPORT)
    monkeypatch.setattr("app.bridge.subprocess.run", lambda *args, **kwargs: pytest.fail("source probe spawned Worker"))
    assert bridge_for(tmp_path)._probe_cuda() == GPU_REPORT


def test_packaged_probe_uses_worker_without_loading_desktop_asr(tmp_path, monkeypatch):
    worker = packaged_worker(tmp_path, monkeypatch)
    monkeypatch.setattr("engine.asr.probe_cuda", lambda: pytest.fail("packaged Desktop imported ASR backend"))
    captured = {}

    def run(command, **kwargs):
        captured.update(command=command, **kwargs)
        return subprocess.CompletedProcess(command, 0, json.dumps(GPU_REPORT), "")

    monkeypatch.setattr("app.bridge.subprocess.run", run)
    assert bridge_for(tmp_path)._probe_cuda() == GPU_REPORT
    assert captured["command"][0] == str(worker)
    assert captured["command"][-1] == "--probe-gpu"
    assert "--settings-dir" not in captured["command"]
    assert captured["timeout"] == 10 and captured["capture_output"] and captured["text"]
    assert captured["creationflags"] == getattr(subprocess, "CREATE_NO_WINDOW", 0)


@pytest.mark.parametrize("stdout,code", [
    ("not json", 0), ("{}", 0), (json.dumps({**GPU_REPORT, "available": "true"}), 0),
    (json.dumps({**GPU_REPORT, "device_count": 0}), 0), (json.dumps(GPU_REPORT), 1),
])
def test_invalid_worker_probe_never_reports_gpu_ready(tmp_path, monkeypatch, stdout, code):
    packaged_worker(tmp_path, monkeypatch)
    monkeypatch.setattr("app.bridge.subprocess.run", lambda command, **kwargs:
                        subprocess.CompletedProcess(command, code, stdout, "fixture private stderr"))
    result = bridge_for(tmp_path)._probe_cuda()
    assert result["available"] is False and result["error"]
    assert "private stderr" not in result["error"]


def test_worker_probe_timeout_is_bounded_and_returns_diagnostic(tmp_path, monkeypatch):
    packaged_worker(tmp_path, monkeypatch)

    def timeout(command, **kwargs):
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr("app.bridge.subprocess.run", timeout)
    result = bridge_for(tmp_path)._probe_cuda()
    assert result["available"] is False and "超时" in result["error"]


def test_missing_packaged_worker_returns_diagnostic(tmp_path, monkeypatch):
    worker = packaged_worker(tmp_path, monkeypatch)
    worker.unlink()
    assert bridge_for(tmp_path)._probe_cuda()["available"] is False


def test_gpu_pack_state_requires_and_reports_all_three_components(tmp_path):
    internal = tmp_path / "worker" / "_internal"
    bridge = bridge_for(internal)
    path = internal / "nvidia" / "cublas/bin/cublas64_12.dll"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"cublas")
    assert bridge._gpu_pack_state()["components"] == ["cublas"]
    assert bridge._gpu_pack_state()["installed"] is False
    write_runtime(internal / "nvidia")
    state = bridge._gpu_pack_state()
    assert state["installed"] is True
    assert state["components"] == ["cublas", "cuda_nvrtc", "cudnn"]


@pytest.mark.parametrize("members", [
    {"nvidia/cublas/bin/cublas64_12.dll": b"fixture"},
    {"nvidia/cublas/bin/cublas64_placeholder.txt": b"fixture"},
    {**{"nvidia/" + name: value for name, value in DLLS.items()}, "../escape.dll": b"fixture"},
    {**{"nvidia/" + name: value for name, value in DLLS.items()}, "other/file.txt": b"fixture"},
    {**{"nvidia/" + name: value for name, value in DLLS.items()}, "nvidia/../escape.dll": b"fixture"},
])
def test_invalid_pack_does_not_touch_existing_runtime(tmp_path, members):
    internal = tmp_path / "worker" / "_internal"
    write_runtime(internal / "nvidia")
    archive = tmp_path / "invalid.zip"
    write_archive(archive, members)
    assert bridge_for(internal).install_gpu_pack(str(archive))["ok"] is False
    assert_runtime(internal / "nvidia", b"old-")
    assert not list(internal.glob(".gpu-pack-*"))


def test_valid_pack_replaces_runtime_and_preserves_previous_version(tmp_path):
    internal = tmp_path / "worker" / "_internal"
    write_runtime(internal / "nvidia")
    archive = tmp_path / "valid.zip"
    write_archive(archive, {"nvidia/" + name: b"new-" + value for name, value in DLLS.items()})
    result = bridge_for(internal).install_gpu_pack(str(archive))
    assert result["ok"] and result["installed"] and result["needs_restart"]
    assert result["components"] == ["cublas", "cuda_nvrtc", "cudnn"]
    assert_runtime(internal / "nvidia", b"new-")
    assert_runtime(Path(result["previous_runtime"]), b"old-")
    assert not list(internal.glob(".gpu-pack-staging-*"))


def test_replacement_failure_restores_old_runtime(tmp_path, monkeypatch):
    internal = tmp_path / "worker" / "_internal"
    write_runtime(internal / "nvidia")
    archive = tmp_path / "valid.zip"
    write_archive(archive, {"nvidia/" + name: b"new-" + value for name, value in DLLS.items()})
    original_rename = Path.rename

    def fail_staged_rename(path, target):
        if path.parent.name.startswith(".gpu-pack-staging-"):
            raise OSError("fixture replacement failure")
        return original_rename(path, target)

    monkeypatch.setattr(Path, "rename", fail_staged_rename)
    result = bridge_for(internal).install_gpu_pack(str(archive))
    assert result["ok"] is False and "fixture replacement failure" in result["error"]
    assert_runtime(internal / "nvidia", b"old-")
    assert not list(internal.glob(".gpu-pack-*"))


def test_failed_rollback_preserves_old_runtime_at_reported_path(tmp_path, monkeypatch):
    internal = tmp_path / "worker" / "_internal"
    write_runtime(internal / "nvidia")
    archive = tmp_path / "valid.zip"
    write_archive(archive, {"nvidia/" + name: b"new-" + value for name, value in DLLS.items()})
    original_rename = Path.rename

    def fail_replacement_and_rollback(path, target):
        if (path.parent.name.startswith(".gpu-pack-staging-")
                or path.name.startswith(".gpu-pack-previous-")):
            raise OSError("fixture locked directory")
        return original_rename(path, target)

    monkeypatch.setattr(Path, "rename", fail_replacement_and_rollback)
    result = bridge_for(internal).install_gpu_pack(str(archive))
    assert result["ok"] is False and "旧运行库已保留" in result["error"]
    assert_runtime(Path(result["previous_runtime"]), b"old-")
    assert not list(internal.glob(".gpu-pack-staging-*"))


def test_existing_symbolic_link_is_not_replaced(tmp_path):
    internal = tmp_path / "worker" / "_internal"
    internal.mkdir(parents=True)
    external = tmp_path / "external-runtime"
    write_runtime(external)
    target = internal / "nvidia"
    try:
        target.symlink_to(external, target_is_directory=True)
    except OSError:
        pytest.skip("Windows symlink privilege unavailable")
    archive = tmp_path / "valid.zip"
    write_archive(archive, {"nvidia/" + name: value for name, value in DLLS.items()})
    result = bridge_for(internal).install_gpu_pack(str(archive))
    assert result["ok"] is False and "目录是链接" in result["error"]
    assert target.is_symlink()
    assert_runtime(external, b"old-")
    assert not list(internal.glob(".gpu-pack-*"))


@pytest.mark.skipif(os.name != "nt", reason="Windows junction safety")
def test_existing_windows_junction_is_not_replaced(tmp_path):
    internal = tmp_path / "worker" / "_internal"
    internal.mkdir(parents=True)
    external = tmp_path / "external-runtime"
    write_runtime(external)
    target = internal / "nvidia"
    script = tmp_path / "create-junction.ps1"
    script.write_text("param([string]$FixtureLink, [string]$FixtureTarget)\n"
                      "New-Item -ItemType Junction -Path $FixtureLink -Target $FixtureTarget -ErrorAction Stop | Out-Null\n",
                      encoding="utf-8")
    # Absolute fixture paths are separate named arguments, never shell text.
    completed = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
         "-File", str(script),
         "-FixtureLink", str(target), "-FixtureTarget", str(external)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert completed.returncode == 0, completed.stderr
    assert target.lstat().st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT
    archive = tmp_path / "valid.zip"
    write_archive(archive, {"nvidia/" + name: value for name, value in DLLS.items()})
    result = bridge_for(internal).install_gpu_pack(str(archive))
    assert result["ok"] is False and "目录是链接" in result["error"]
    assert target.lstat().st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT
    assert_runtime(external, b"old-")
    assert not list(internal.glob(".gpu-pack-*"))
