# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""Offline inventory and selection must agree at the actual save/load boundary."""
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
import os
import struct
import sys

import pytest

from engine.asr import load_whisper_model
from engine.model_manager import ModelManager, ModelManagerError
from model_cache_fixture import create_model_cache, model_binary, write_model_snapshot
from test_bridge import make_bridge


@pytest.fixture
def bridge(tmp_path, monkeypatch):
    monkeypatch.setattr("engine.asr.probe_cuda", lambda: {"available": False})
    monkeypatch.setattr("engine.setup_check.detect_vram_mb", lambda: None)
    instance, _, _ = make_bridge(settings={"model_dir": str(tmp_path), "whisper_model": "tiny", "device": "cpu", "theme": "dark"})
    return instance


def test_missing_model_selection_rejects_entire_patch_but_unrelated_settings_work(bridge):
    before = dict(bridge.controller.settings)
    result = bridge.save_settings({"whisper_model": "small", "theme": "paper"})
    assert result["ok"] is False and result["code"] == "model_not_ready"
    assert bridge.controller.settings == before and bridge.controller.saved == []
    assert bridge.save_settings({"theme": "paper"})["theme"] == "paper"


@pytest.mark.parametrize("name", ["unknown", "../../tiny", " tiny ", None, True, 3])
def test_unsupported_models_cannot_be_saved(bridge, name):
    assert bridge.save_settings({"whisper_model": name})["code"] == "invalid_model"
    assert bridge.controller.saved == []


@pytest.mark.parametrize("layout", ["direct", "legacy"])
def test_verified_offline_snapshot_can_be_selected_and_used_by_asr(bridge, tmp_path, layout):
    snapshot = create_model_cache(tmp_path, "small", layout=layout)
    result = bridge.save_settings({"whisper_model": "small"})
    assert result["whisper_model"] == "small"
    captured = []
    constructor = lambda *args, **kwargs: captured.append((args, kwargs)) or object()
    with mock.patch.dict(sys.modules, {"faster_whisper": SimpleNamespace(WhisperModel=constructor)}):
        load_whisper_model("small", "cpu", str(tmp_path))
    assert captured[0][0] == (str(snapshot),)
    assert captured[0][1]["local_files_only"] is True
    assert captured[0][1]["download_root"] == str(tmp_path)


def test_downloading_snapshot_cannot_be_selected_even_with_complete_files(bridge, tmp_path):
    create_model_cache(tmp_path, "small")
    create_model_cache(tmp_path, "base")
    bridge._model_download = {"name": "small", "progress": .99, "message": "最后验证", "error": ""}
    result = bridge.save_settings({"whisper_model": "small"})
    assert result["ok"] is False and "下载" in result["error"]
    assert bridge.controller.settings["whisper_model"] == "tiny"
    assert bridge.save_settings({"whisper_model": "base"})["whisper_model"] == "base"
    for payload in (bridge.get_models(), bridge.get_setup_check()):
        item = next(item for item in payload["models"] if item["name"] == "small")
        assert item["verified"] is True and item["ready"] is False and item["reason"] == "下载中"
        assert payload["download"]["progress"] == .99
    bridge._model_download["name"] = ""
    assert bridge.save_settings({"whisper_model": "small"})["whisper_model"] == "small"


@pytest.mark.parametrize("damage", ["empty_weight", "truncated_weight", "invalid_config", "array_config", "missing_tokenizer", "invalid_tokenizer", "missing_vocabulary", "empty_vocabulary"])
def test_corrupt_cache_is_not_ready_in_inventory_setup_or_save(bridge, tmp_path, damage):
    snapshot = create_model_cache(tmp_path, "small")
    if damage == "empty_weight": (snapshot / "model.bin").write_bytes(b"")
    elif damage == "truncated_weight": (snapshot / "model.bin").write_bytes(model_binary()[:-7])
    elif damage == "invalid_config": (snapshot / "config.json").write_text("{broken", encoding="utf-8")
    elif damage == "array_config": (snapshot / "config.json").write_text("[]", encoding="utf-8")
    elif damage == "missing_tokenizer": (snapshot / "tokenizer.json").unlink()
    elif damage == "invalid_tokenizer": (snapshot / "tokenizer.json").write_text('{"model":{"type":"Unknown"}}', encoding="utf-8")
    elif damage == "missing_vocabulary": (snapshot / "vocabulary.txt").unlink()
    else: (snapshot / "vocabulary.txt").write_text("", encoding="utf-8")
    for payload in (bridge.get_models(), bridge.get_setup_check()):
        item = next(item for item in payload["models"] if item["name"] == "small")
        assert item["installed"] is True and item["verified"] is False and item["ready"] is False and item["reason"]
    assert bridge.save_settings({"whisper_model": "small"})["ok"] is False
    assert bridge.controller.saved == []


def test_model_is_rechecked_after_the_ui_snapshot(bridge, tmp_path):
    snapshot = create_model_cache(tmp_path, "small")
    assert next(m for m in bridge.get_models()["models"] if m["name"] == "small")["ready"] is True
    (snapshot / "model.bin").write_bytes(b"partial")
    assert bridge.save_settings({"whisper_model": "small"})["ok"] is False
    assert bridge.controller.settings["whisper_model"] == "tiny"


def test_directory_and_model_patch_verifies_destination_atomically(bridge, tmp_path):
    create_model_cache(tmp_path, "small")
    destination = tmp_path / "other-cache"
    before = dict(bridge.controller.settings)
    assert bridge.save_settings({"model_dir": str(destination), "whisper_model": "small"})["ok"] is False
    assert bridge.controller.settings == before
    create_model_cache(destination, "small")
    result = bridge.save_settings({"model_dir": str(destination), "whisper_model": "small"})
    assert result["model_dir"] == str(destination) and result["whisper_model"] == "small"


def test_setup_fix_rechecks_instead_of_saving_a_stale_ready_state(bridge, tmp_path):
    snapshot = create_model_cache(tmp_path, "small")
    manager = bridge._model_manager()
    ready = manager.list_models()
    (snapshot / "model.bin").write_bytes(b"broken after check")
    with mock.patch.object(manager, "list_models", return_value=ready), mock.patch.object(bridge, "_model_manager", return_value=manager):
        result = bridge.apply_setup_fixes()
    assert bridge.controller.settings["whisper_model"] == "tiny"
    assert not any("whisper_model" in patch for patch in bridge.controller.saved)
    assert not any("切到" in label for label in result["applied"])


def test_inspection_failure_does_not_save_or_change_account_settings(bridge):
    before = dict(bridge.controller.settings)
    with mock.patch.object(bridge, "_model_manager", side_effect=OSError("unreadable cache")):
        result = bridge.save_settings({"whisper_model": "small", "theme": "paper"})
    assert result["ok"] is False and bridge.controller.settings == before
    assert bridge.controller.saved == []


@pytest.mark.parametrize("version,dtype", [(2, 0), (3, 1), (4, 2), (6, 0), (6, 1), (6, 2), (6, 3), (6, 4), (6, 5)])
@pytest.mark.parametrize("vocabulary", ["txt", "json"])
def test_old_and_quantized_serializations_keep_local_compatibility(tmp_path, version, dtype, vocabulary):
    create_model_cache(tmp_path, version=version, dtype=dtype, vocabulary=vocabulary)
    assert ModelManager(str(tmp_path)).state("tiny").verified is True


def test_default_hf_home_and_explicit_root_resolve_the_same_legacy_cache(tmp_path, monkeypatch):
    snapshot = create_model_cache(tmp_path, layout="legacy")
    monkeypatch.setenv("HF_HOME", str(tmp_path))
    assert ModelManager().state("tiny").path == str(snapshot)
    assert ModelManager(str(tmp_path)).state("tiny").path == str(snapshot)


def test_binary_check_skips_large_tensor_payload(tmp_path, monkeypatch):
    path = tmp_path / "model.bin"
    template = model_binary(elements=1)
    # Sparse payload: metadata inspection should read bytes only around it.
    length = 100 * 1024 * 1024
    header = template[:-8]
    header = header[:-4] + struct.pack("<I", length)
    # Replace the one-dimensional shape to match this float32 payload.
    header = header[:-9] + struct.pack("<I", length // 4) + header[-5:]
    with path.open("wb") as handle:
        handle.write(header); handle.seek(length, os.SEEK_CUR); handle.write(struct.pack("<I", 0))
    read_bytes = []
    original = Path.open
    class Reader:
        def __init__(self, handle): self.handle = handle
        def __enter__(self): return self
        def __exit__(self, *args): self.handle.close()
        def __getattr__(self, name): return getattr(self.handle, name)
        def read(self, count=-1):
            assert 0 <= count <= 65535
            data = self.handle.read(count); read_bytes.append(len(data)); return data
    monkeypatch.setattr(Path, "open", lambda self, *args, **kwargs: Reader(original(self, *args, **kwargs)))
    ModelManager._check_binary(path)
    assert sum(read_bytes) < 512


def test_delete_is_blocked_while_model_is_downloading(bridge, tmp_path):
    snapshot = create_model_cache(tmp_path)
    bridge._model_download["name"] = "tiny"
    assert bridge.delete_model("tiny")["ok"] is False
    assert bridge.delete_all_models()["ok"] is False
    assert snapshot.is_dir()


def test_unknown_download_does_not_start_background_work(bridge):
    with mock.patch.object(bridge, "_download_model_worker") as worker:
        assert bridge.download_model("../../bad")["ok"] is False
    worker.assert_not_called()
    assert not bridge._model_download["name"]


@pytest.mark.parametrize("explicit", [True, False])
def test_download_uses_the_cache_layout_that_inventory_and_runtime_resolve(tmp_path, monkeypatch, explicit):
    monkeypatch.setenv("HF_HOME", str(tmp_path))
    manager = ModelManager(str(tmp_path) if explicit else None)
    expected = tmp_path if explicit else tmp_path / "hub"
    downloads = []
    def download(**kwargs):
        downloads.append(kwargs)
        snapshot = Path(kwargs["cache_dir"]) / "models--Systran--faster-whisper-tiny" / "snapshots" / "fixture"
        write_model_snapshot(snapshot)
        return str(snapshot / kwargs["filename"])
    api = SimpleNamespace(sha="fixture", siblings=[SimpleNamespace(rfilename="model.bin", size=0)])
    hub = SimpleNamespace(HfApi=lambda **kwargs: SimpleNamespace(model_info=lambda *args, **kw: api), hf_hub_download=download)
    with mock.patch.dict(sys.modules, {"huggingface_hub": hub}):
        state = manager.download("tiny")
    assert state.verified is True
    assert Path(state.path).is_relative_to(expected)
    assert downloads[0]["cache_dir"] == str(expected)
    assert manager.state("tiny").path == state.path


@pytest.mark.parametrize("bad", [b"", b"model", model_binary()[:-1], model_binary() + b"trailing", b"version https://git-lfs.github.com/spec/v1"])
def test_invalid_weight_records_never_count_as_verified(tmp_path, bad):
    snapshot = create_model_cache(tmp_path)
    (snapshot / "model.bin").write_bytes(bad)
    assert ModelManager(str(tmp_path)).state("tiny").verified is False


def test_cache_reference_cannot_select_a_snapshot_outside_its_repo(tmp_path):
    outside = write_model_snapshot(tmp_path / "outside")
    root = tmp_path / "models--Systran--faster-whisper-tiny"
    (root / "refs").mkdir(parents=True)
    (root / "refs" / "main").write_text("../../outside", encoding="utf-8")
    assert outside.is_dir()
    assert ModelManager(str(tmp_path)).state("tiny").verified is False


def mock_pinned_upgrade(monkeypatch, snapshot):
    files = ["config.json", "model.bin", "tokenizer.json", "vocabulary.txt"]
    calls = []
    def download(**kwargs):
        calls.append(kwargs)
        assert kwargs["revision"] == snapshot.name
        assert Path(kwargs["cache_dir"]) == snapshot.parents[2]
        # A commit-pinned Hugging Face download does not rewrite refs/main.
        return str(snapshot / kwargs["filename"])
    info = SimpleNamespace(sha=snapshot.name, siblings=[SimpleNamespace(rfilename=name, size=0) for name in files])
    hub = SimpleNamespace(HfApi=lambda **kw: SimpleNamespace(model_info=lambda *args, **kw: info), hf_hub_download=download)
    monkeypatch.setitem(sys.modules, "huggingface_hub", hub)
    return calls


@pytest.mark.parametrize("layout", ["direct", "legacy"])
def test_pinned_upgrade_commits_verified_new_snapshot_and_keeps_old_cache(tmp_path, monkeypatch, layout):
    old = create_model_cache(tmp_path, layout=layout)
    ref = old.parents[1] / "refs" / "main"
    new = write_model_snapshot(old.parent / "next-revision")
    calls = mock_pinned_upgrade(monkeypatch, new)
    progress = []
    manager = ModelManager(str(tmp_path))
    state = manager.download("tiny", progress=lambda message, value: progress.append((message, value)))
    assert state.verified is True and state.path == str(new)
    assert ref.read_text(encoding="utf-8") == new.name
    assert manager.state("tiny").path == str(new)
    assert old.is_dir() and ModelManager._verification_error(old) == ""
    assert len(calls) == 4 and progress[-1] == ("模型已就绪", 1.0)


@pytest.mark.parametrize("layout", ["direct", "legacy"])
def test_broken_pinned_upgrade_never_commits_or_reports_old_cache_as_download_success(tmp_path, monkeypatch, layout):
    old = create_model_cache(tmp_path, layout=layout)
    ref = old.parents[1] / "refs" / "main"
    old_reference, old_weight = ref.read_bytes(), (old / "model.bin").read_bytes()
    new = write_model_snapshot(old.parent / "next-revision")
    (new / "model.bin").write_bytes(b"partial")
    mock_pinned_upgrade(monkeypatch, new)
    progress = []
    manager = ModelManager(str(tmp_path))
    with pytest.raises(ModelManagerError, match="缓存不完整"):
        manager.download("tiny", progress=lambda message, value: progress.append((message, value)))
    assert ref.read_bytes() == old_reference and (old / "model.bin").read_bytes() == old_weight
    assert manager.state("tiny").verified is True and manager.state("tiny").path == str(old)
    assert all(value != 1.0 for _, value in progress)


def test_reference_commit_failure_preserves_old_pointer_and_cleans_only_temporary_ref(tmp_path, monkeypatch):
    old = create_model_cache(tmp_path)
    ref = old.parents[1] / "refs" / "main"
    old_reference = ref.read_bytes()
    new = write_model_snapshot(old.parent / "next-revision")
    mock_pinned_upgrade(monkeypatch, new)
    monkeypatch.setattr("engine.model_manager.os.replace", mock.Mock(side_effect=PermissionError("locked reference")))
    progress = []
    manager = ModelManager(str(tmp_path))
    with pytest.raises(ModelManagerError, match="版本引用保存失败"):
        manager.download("tiny", progress=lambda message, value: progress.append((message, value)))
    assert ref.read_bytes() == old_reference
    assert manager.state("tiny").path == str(old) and old.is_dir() and new.is_dir()
    assert not list(ref.parent.glob(".main-*"))
    assert all(value != 1.0 for _, value in progress)


def actual_hub_fixture(monkeypatch, revision, good):
    """Keep the installed HF cache implementation; replace only remote I/O."""
    from huggingface_hub import HfApi, file_download
    from huggingface_hub.utils import _headers
    content = {name: (good / name).read_bytes() for name in ["config.json", "model.bin", "tokenizer.json", "vocabulary.txt"]}
    monkeypatch.setattr(_headers, "get_token", lambda: None)
    monkeypatch.setattr(HfApi, "model_info", lambda *args, **kw: SimpleNamespace(sha=revision,
        siblings=[SimpleNamespace(rfilename=name, size=len(data)) for name, data in content.items()]))
    metadata_calls, transfers = [], []
    def metadata(**kwargs):
        name = kwargs["filename"]; metadata_calls.append(name)
        return "https://fixture.invalid/" + name, "fixture-etag-" + name, revision, len(content[name]), None, None
    def transfer(**kwargs):
        transfers.append(kwargs)
        kwargs["destination_path"].write_bytes(content[kwargs["filename"]])
    monkeypatch.setattr(file_download, "_get_metadata_or_catch_error", metadata)
    monkeypatch.setattr(file_download, "_download_to_tmp_and_move", transfer)
    # Deterministic Windows fallback: actual copied snapshot files, no symlink
    # privileges, cache implementation or model validation mocked away.
    monkeypatch.setattr(file_download, "are_symlinks_supported", lambda *args, **kwargs: False)
    return metadata_calls, transfers


@pytest.mark.parametrize("layout", ["direct", "legacy"])
def test_same_sha_repair_replaces_copied_corrupt_cache_using_actual_hf_download(tmp_path, monkeypatch, layout):
    previous = create_model_cache(tmp_path, layout=layout)
    good = write_model_snapshot(tmp_path / "fixture-source")
    revision = "a" * 40
    snapshot = write_model_snapshot(previous.parent / revision)
    (snapshot / "model.bin").write_bytes(b"partial")
    ref = previous.parents[1] / "refs" / "main"; ref.write_text(revision, encoding="utf-8")
    old_bytes = (previous / "model.bin").read_bytes()
    metadata, transfers = actual_hub_fixture(monkeypatch, revision, good)
    assert ModelManager(str(tmp_path)).state("tiny").verified is False
    state = ModelManager(str(tmp_path)).download("tiny")
    assert state.verified is True and state.path == str(snapshot)
    assert (snapshot / "model.bin").read_bytes() == (good / "model.bin").read_bytes()
    assert len(metadata) == len(transfers) == 4 and all(call["force_download"] is True for call in transfers)
    # Hugging Face may add a Windows extended-length prefix. Verify the
    # actual directory identity, retaining every transfer/content assertion.
    assert all(call["destination_path"].parent.samefile(snapshot) for call in transfers)
    assert ref.read_text(encoding="utf-8") == revision
    assert (previous / "model.bin").read_bytes() == old_bytes


@pytest.mark.parametrize("kind", ["already_valid", "new_revision"])
def test_valid_or_new_sha_does_not_force_download_or_change_old_weight(tmp_path, monkeypatch, kind):
    previous = create_model_cache(tmp_path)
    revision = "b" * 40
    if kind == "already_valid":
        snapshot = write_model_snapshot(previous.parent / revision)
        (previous.parents[1] / "refs" / "main").write_text(revision, encoding="utf-8")
    else:
        snapshot = previous.parent / revision
    good = write_model_snapshot(tmp_path / "fixture-source")
    old_bytes = (previous / "model.bin").read_bytes()
    metadata, transfers = actual_hub_fixture(monkeypatch, revision, good)
    state = ModelManager(str(tmp_path)).download("tiny")
    assert state.verified is True and state.path == str(snapshot)
    if kind == "already_valid":
        assert metadata == transfers == []
    else:
        assert len(metadata) == len(transfers) == 4 and all(call["force_download"] is False for call in transfers)
    assert (previous / "model.bin").read_bytes() == old_bytes


def test_failed_same_sha_repair_keeps_valid_previous_reference_and_weight(tmp_path, monkeypatch):
    previous = create_model_cache(tmp_path)
    ref = previous.parents[1] / "refs" / "main"
    old_reference, old_weight = ref.read_bytes(), (previous / "model.bin").read_bytes()
    revision = "c" * 40
    snapshot = write_model_snapshot(previous.parent / revision)
    (snapshot / "model.bin").write_bytes(b"partial")
    good = write_model_snapshot(tmp_path / "fixture-source")
    actual_hub_fixture(monkeypatch, revision, good)
    def failure(**kwargs):
        raise OSError("offline fixture transfer failure")
    monkeypatch.setattr("huggingface_hub.file_download._download_to_tmp_and_move", failure)
    progress = []
    with pytest.raises(ModelManagerError, match="获取失败"):
        ModelManager(str(tmp_path)).download("tiny", progress=lambda message, value: progress.append((message, value)))
    assert ref.read_bytes() == old_reference and (previous / "model.bin").read_bytes() == old_weight
    assert ModelManager(str(tmp_path)).state("tiny").path == str(previous)
    assert all(value != 1.0 for _, value in progress)


def test_same_sha_symlink_repair_preserves_shared_blob_and_old_valid_model(tmp_path, monkeypatch):
    previous = create_model_cache(tmp_path)
    revision = "d" * 40
    snapshot = write_model_snapshot(previous.parent / revision)
    blob = previous.parents[1] / "blobs" / "broken-fixture"
    blob.parent.mkdir(exist_ok=True)
    blob.write_bytes(b"partial")
    pointer = snapshot / "model.bin"
    pointer.unlink()
    try:
        pointer.symlink_to(blob)
    except OSError as exc:
        if os.name == "nt" and getattr(exc, "winerror", None) == 1314:
            pytest.skip("Windows symlink privilege unavailable")
        raise
    ref = previous.parents[1] / "refs" / "main"
    ref.write_text(revision, encoding="utf-8")
    old_weight = (previous / "model.bin").read_bytes()
    good = write_model_snapshot(tmp_path / "fixture-source")
    actual_hub_fixture(monkeypatch, revision, good)
    state = ModelManager(str(tmp_path)).download("tiny")
    assert state.verified is True and state.path == str(snapshot)
    assert not pointer.is_symlink() and pointer.read_bytes() == (good / "model.bin").read_bytes()
    assert blob.read_bytes() == b"partial"
    assert (previous / "model.bin").read_bytes() == old_weight
