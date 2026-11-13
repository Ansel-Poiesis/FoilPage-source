# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
from __future__ import annotations

import tempfile
from pathlib import Path
from unittest import mock

from app.agent_service import batch_apply_agent_translation


class FakeQueue:
    def __init__(self, jobs: dict, manifests: dict) -> None:
        self.jobs = jobs
        self.manifests = manifests
        self.replaced: list[str] = []
        self.titles: dict[str, str] = {}

    def get_job(self, job_id: str):
        return self.jobs.get(job_id)

    def get_manifest(self, job_id: str):
        return self.manifests.get(job_id)

    def replace_manifest(self, manifest: dict) -> None:
        self.replaced.append(manifest["job_id"])

    def set_source_title(self, job_id: str, title: str) -> None:
        self.titles[job_id] = title


def _manifest(job_id: str, raw_text: str) -> dict:
    return {
        "job_id": job_id,
        "source_path": "source.mp4",
        "source_title": "原标题",
        "raw_path": "raw.md",
        "final_path": "final.md",
        "content_mode": "raw",
    }


def test_batch_applies_agent_to_done_jobs_and_skips_others() -> None:
    with tempfile.TemporaryDirectory(prefix="batch_agent_") as temp:
        raw = Path(temp) / "raw.md"
        raw.write_text("原始转写", encoding="utf-8")
        queue = FakeQueue(
            jobs={
                "a": {"status": "done", "source_title": "甲"},
                "b": {"status": "done", "source_title": "乙"},
                "c": {"status": "failed", "source_title": "丙"},
                "missing": {"status": "done"},
            },
            manifests={
                "a": _manifest("a", str(raw)),
                "b": _manifest("b", str(raw)),
                "c": _manifest("c", str(raw)),
            },
        )
        settings = {"update_title_after_transcribe": True}

        def fake_apply(manifest, settings, **kwargs):
            return {**manifest, "content_mode": "article", "source_title": "新标题" + manifest["job_id"]}

        with mock.patch("app.agent_service.apply_agent_translation", side_effect=fake_apply):
            result = batch_apply_agent_translation(
                ["a", "b", "c", "missing"], queue, settings,
            )

    assert result["done"] == 2
    assert result["skipped"] == 2  # c 非 done，missing 无 manifest
    assert result["failed"] == 0
    assert set(queue.replaced) == {"a", "b"}
    assert queue.titles == {}, "正文批量整理不得隐式回写任务标题"


def test_batch_isolates_single_failure() -> None:
    queue = FakeQueue(
        jobs={"a": {"status": "done", "source_title": "甲"}, "b": {"status": "done", "source_title": "乙"}},
        manifests={"a": _manifest("a", "text"), "b": _manifest("b", "text")},
    )

    def flaky(manifest, settings, **kwargs):
        if manifest["job_id"] == "a":
            raise RuntimeError("上游 500")
        return {**manifest, "content_mode": "article", "source_title": "成功标题"}

    with mock.patch("app.agent_service.apply_agent_translation", side_effect=flaky):
        result = batch_apply_agent_translation(["a", "b"], queue, {"update_title_after_transcribe": True})

    assert result["done"] == 1
    assert result["failed"] == 1
    assert "a: RuntimeError" in result["errors"][0]
    assert queue.replaced == ["a", "b"]  # 失败阶段也持久化，已有转写仍可用


def test_batch_honors_update_title_switch() -> None:
    queue = FakeQueue(
        jobs={"a": {"status": "done", "source_title": "甲"}},
        manifests={"a": _manifest("a", "text")},
    )
    with mock.patch(
        "app.agent_service.apply_agent_translation",
        return_value={**_manifest("a", "text"), "content_mode": "article", "source_title": "新标题"},
    ):
        batch_apply_agent_translation(["a"], queue, {"update_title_after_transcribe": False})
    assert queue.titles == {}
    assert queue.replaced == ["a"]
