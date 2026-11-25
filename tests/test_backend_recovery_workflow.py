# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""Twelve-source workflow through real desktop services, SQLite and Worker.

Only the ASR model/backend and translation model response are replaced. This
does not validate model quality, GPU execution, GUI or a packaged installation.
"""
from __future__ import annotations

import json
import math
import shutil
import struct
import wave
import zipfile
from collections import Counter
from pathlib import Path

from app import worker
from app.bridge import Bridge
from app.controller import AppController
from engine import asr
from engine.contracts import EngineError
from engine.postprocess_store import PostprocessStore
from engine.settings import SettingsStore
from account_auth_fixtures import grant_account
import run_engine


def _open(base: Path):
    controller = AppController(SettingsStore(base_dir=base))
    bridge = Bridge(controller, None, controller.settings_store, lambda: None, lambda: None,
                    postprocess_store=PostprocessStore(controller.settings["database_path"]))
    return controller, bridge


def _wav(path: Path, frequency: int) -> None:
    with wave.open(str(path), "wb") as stream:
        stream.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        stream.writeframes(b"".join(struct.pack("<h", int(10000 * math.sin(index * 2 * math.pi * frequency / 16000)))
                                    for index in range(800)))


def _documents(controller):
    result = {}
    for job in controller.queue.list_jobs():
        manifest = controller.queue.get_manifest(job["id"])
        assert job["status"] == "done" and manifest is not None
        paths = ("raw_path", "final_path", "segments_path", "translation_path", "translation_segments_path")
        result[job["id"]] = {key: Path(manifest[key]).read_bytes() for key in paths if manifest.get(key)}
    return result


def test_twelve_sources_repair_reopen_translate_restore_and_export(tmp_path, monkeypatch):
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    paths = []
    for index in range(11):
        path = inputs / f"item-{index:02d}.wav"
        _wav(path, 200 + index * 23)
        paths.append(path)
    duplicate = inputs / "same-content-new-name.wav"
    shutil.copy2(paths[0], duplicate)
    paths.append(duplicate)

    repaired = [False]
    calls = []
    def recognize(wav, *_args, **_kwargs):
        name = Path(wav).stem
        calls.append(name)
        if name == "item-10" and not repaired[0]:
            raise EngineError("corrupt_media", "synthetic ASR rejection until the recognition fixture is repaired")
        text = f"Synthetic transcript for {name}."
        return {"text": text, "language": "en", "language_probability": 0.99,
                "duration_sec": 0.05, "avg_logprob": -0.1, "no_speech_prob": 0.01,
                "backend": "local", "model": "tiny",
                "segments": [{"start": 0.0, "end": 0.05, "text": text}]}
    run_engine.clear_model_cache()
    monkeypatch.setattr(asr, "load_whisper_model", lambda *_a, **_k: (object(), "cpu"))
    monkeypatch.setattr(asr, "transcribe_audio", recognize)

    source, source_bridge = _open(tmp_path / "source-data")
    target = target_bridge = None
    evidence = {}
    try:
        source.save_settings(grant_account(dict(source.settings), source.settings_store.base_dir))
        source.save_settings({"device": "cpu", "whisper_model": "tiny", "language": "en",
                              "agent_enabled": False, "title_optimization_enabled": False})
        imported = source_bridge.import_files([str(path) for path in paths])
        assert imported["ok"] and imported["imported"] == 11 and imported["skipped"] == 1
        assert imported["failed"] == 0 and len(imported["enqueued"]) == 11
        initial_ids = set(imported["enqueued"])
        assert len(source.media.list_media()) == 11
        executor = worker.Worker(source.queue, settings_store=source.settings_store, worker_id="workflow")
        for _ in range(11):
            assert executor.step()
        assert not executor.step()
        statuses = Counter(job["status"] for job in source.queue.list_jobs())
        assert statuses == {"done": 10, "failed": 1}
        failed = next(job for job in source.queue.list_jobs() if job["status"] == "failed")
        evidence["first_pass"] = {"input_count": 12, "unique": 11, "skipped_duplicate": imported["skipped"], **statuses}

        repaired[0] = True
        retry = source_bridge.requeue_tasks(["job:" + failed["id"]])
        assert retry["ok"] and retry["requeued"] == ["job:" + failed["id"]]
        assert executor.step() and not executor.step()
        assert Counter(job["status"] for job in source.queue.list_jobs()) == {"done": 11}
        assert {job["id"] for job in source.queue.list_jobs()} == initial_ids
        assert len(calls) == 12 and calls.count("item-10") == 2
        evidence["repaired_pass"] = {"done": 11, "same_job_identities": True, "asr_calls": len(calls)}

        translated_job = sorted(initial_ids)[0]
        source.save_settings({"agent_enabled": True, "agent_billing_mode": "byok", "agent_api_key": "offline-fixture"})
        monkeypatch.setattr("app.translation_service.chat_completion",
                            lambda *_a, **_k: json.dumps({"translations": ["用于连续验收的合成译文。"]}, ensure_ascii=False))
        accepted = source_bridge.translate_text("job:" + translated_job, "en-zh")
        assert accepted["ok"] and accepted["started"]
        for thread in source_bridge._postprocess_threads:
            thread.join(timeout=5)
            assert not thread.is_alive()
        state = source_bridge.get_translation_status("job:" + translated_job)
        assert state["status"] == "done" and state["has_translation"]
        manifest = source.queue.get_manifest(translated_job)
        pairs = json.loads(Path(manifest["translation_segments_path"]).read_text(encoding="utf-8"))
        assert pairs == [{"src": Path(manifest["final_path"]).read_text(encoding="utf-8"),
                          "dst": "用于连续验收的合成译文。"}]
        expected = _documents(source)
        receipt_id = state["run_id"]
        source_bridge.close_postprocessing()
        source.close()
        source = source_bridge = None

        source, source_bridge = _open(tmp_path / "source-data")
        assert _documents(source) == expected
        assert source_bridge.get_translation_status("job:" + translated_job)["run_id"] == receipt_id
        archive = tmp_path / "source-backup.zip"
        assert source_bridge.export_backup(str(archive))["ok"]

        target, target_bridge = _open(tmp_path / "target-data")
        target.save_settings({"output_dir": str(tmp_path / "custom-restored-output")})
        restored = target_bridge.import_backup(str(archive))
        assert restored["ok"] and restored["restart_required"]
        assert _documents(target) == expected
        for job in target.queue.list_jobs():
            record = target.queue.get_manifest(job["id"])
            for key in expected[job["id"]]:
                assert Path(record[key]).is_relative_to(Path(target.settings["output_dir"]))
        assert target_bridge.get_translation_status("job:" + translated_job)["has_translation"]
        assert target_bridge.get_translation_status("job:" + translated_job)["run_id"] == receipt_id
        exported = tmp_path / "restored-export.zip"
        assert target_bridge.export_backup(str(exported))["ok"]
        with zipfile.ZipFile(exported) as zf:
            assert "library.db" in zf.namelist()
            for job in target.queue.list_jobs():
                record = target.queue.get_manifest(job["id"])
                for key, content in expected[job["id"]].items():
                    relative = Path(record[key]).relative_to(Path(target.settings["output_dir"])).as_posix()
                    assert zf.read("output/" + relative) == content
        evidence.update(restored_documents=11, translation_receipt_persisted=True,
                        raw_final_and_sidecars_equal=True, reexport_verified=True,
                        boundary="real Controller/Bridge/SQLite/Worker/run_engine; ASR and translation model fixtures; no GUI, installer, live provider or GPU")
        (tmp_path / "workflow-evidence.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
    finally:
        if target_bridge is not None:
            target_bridge.close_postprocessing()
        if target is not None:
            target.close()
        if source_bridge is not None:
            source_bridge.close_postprocessing()
        if source is not None:
            source.close()
        run_engine.clear_model_cache()
