# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""Post-ASR Agent orchestration and durable article output."""
from __future__ import annotations

import json
import os
import re
import tempfile
import uuid
from pathlib import Path
from typing import Callable, Mapping

from engine.agent_client import AgentStage, rewrite_transcript, settle_agent_stages, agent_failure_metadata
from engine.contracts import Manifest, EngineError


def apply_agent_translation(
    manifest: Manifest,
    settings: Mapping[str, object],
    *,
    source_title: str = "",
    cancel_check: Callable[[], bool] | None = None,
) -> Manifest:
    """Create an article beside raw text and return an updated manifest."""
    raw_path = Path(manifest["raw_path"])
    raw_text = raw_path.read_text(encoding="utf-8")
    segments = None
    segments_path = str(manifest.get("segments_path") or "").strip()
    if segments_path:
        try:
            value = json.loads(Path(segments_path).read_text(encoding="utf-8"))
            if isinstance(value, list):
                segments = value
        except (OSError, json.JSONDecodeError):
            # Visual review is optional; an old or manually moved sidecar
            # must never make a usable raw transcript fail.
            segments = None
    original_title = str(manifest.get("original_source_title") or source_title or manifest.get("source_title") or "").strip()
    current_title = str(source_title or manifest.get("source_title") or original_title).strip()
    stage = AgentStage(settings, "body", raw_text,
                       str(settings.get("_agent_operation_id") or uuid.uuid4().hex), cancel_check)
    with stage:
        result = rewrite_transcript(
            raw_text, stage.settings, title=current_title,
            source_path=str(manifest.get("source_path") or ""), segments=segments,
            duration_sec=float(manifest.get("duration_sec") or 0.0), cancel_check=cancel_check,
        )
        article_path = raw_path.with_name(_article_filename(raw_path, current_title))
        # Naming is a separate opt-in operation. Body AI cannot rename the H1.
        content = _with_title(result.markdown, current_title) if current_title else result.markdown
        _atomic_write(article_path, content)
        updated: Manifest = dict(manifest)
        updated.update({"final_path": str(article_path), "source_title": current_title,
                        "original_source_title": original_title, "body_status": "done",
                        "body_enabled_snapshot": True, "body_quota_exceeded": False, **stage.metadata()})
        raw_tags = getattr(result, "tags", ())
        tags = list(raw_tags) if isinstance(raw_tags, (list, tuple)) else []
        vision_frames_value = getattr(result, "vision_frames", 0)
        if not isinstance(vision_frames_value, int) or vision_frames_value < 0:
            vision_frames_value = 0
        updated.update({
            "content_mode": "article",
            "prompt_version": "agent-v3-readable-fidelity",
            "agent_prompt_version": "agent-v3-readable-fidelity",
            "agent_editing_level": str(getattr(result, "editing_level", "") or settings.get("agent_editing_level") or "balanced"),
            "agent_profile": result.profile,
            "agent_model": stage.model if stage.mode == "hosted" else result.model,
            "agent_status": "done", "agent_tags": tags, "body_error_code": "", "body_error": "",
            "agent_vision_used": getattr(result, "vision_used", False) is True,
            "agent_vision_frames": vision_frames_value,
            "agent_fidelity_fallback": getattr(result, "fidelity_fallback", False) is True,
        })
        if not stage.request_count:
            count = getattr(result, "request_count", 0)
            updated["body_request_count"] = count if isinstance(count, int) and not isinstance(count, bool) and count >= 0 else 0
        _write_manifest_file(updated)
    return updated



def apply_title_optimization(manifest: Manifest, settings: Mapping[str, object], *, cancel_check: Callable[[], bool] | None = None) -> Manifest:
    """An optional, isolated request. Failure preserves usable ASR/article output."""
    from engine.title_optimizer import TITLE_SKILL_VERSION, optimize_title
    updated: Manifest = dict(manifest)
    if not settings.get("title_optimization_enabled"):
        return updated
    original_title = str(manifest.get("original_source_title") or manifest.get("source_title") or "")
    stage = None
    old_content = None
    changed_path = None
    try:
        raw_path = Path(manifest["raw_path"])
        text = raw_path.read_text(encoding="utf-8")
        stage = AgentStage(settings, "title", text,
                           str(settings.get("_agent_operation_id") or uuid.uuid4().hex), cancel_check)
        with stage:
            title = optimize_title(text, stage.settings, original_title=original_title, cancel_check=cancel_check)
            if cancel_check and cancel_check():
                raise EngineError("cancelled", "作业已取消")
            final_path = Path(manifest["final_path"])
            content = final_path.read_text(encoding="utf-8")
            if final_path.resolve() == raw_path.resolve():
                final_path = raw_path.with_name(raw_path.stem + "_标题.md")
            old_content = final_path.read_text(encoding="utf-8") if final_path.exists() else None
            changed_path = final_path
            _atomic_write(final_path, _with_title(content, title))
            updated.update({"source_title": title, "original_source_title": original_title,
                            "optimized_source_title": title, "title_enabled_snapshot": True,
                            "final_path": str(final_path), "title_status": "done", "title_skill_version": TITLE_SKILL_VERSION,
                            "title_model": stage.model if stage.mode == "hosted" else str(settings.get("title_model") or settings.get("agent_model") or ""),
                            "title_error": "", "title_quota_exceeded": False, **stage.metadata()})
            updated["title_error_code"] = ""
            _write_manifest_file(updated)
    except Exception as exc:
        if changed_path:
            if old_content is not None:
                _atomic_write(changed_path, old_content)
            else:
                changed_path.unlink(missing_ok=True)
        updated = dict(manifest)
        if cancel_check and cancel_check():
            raise EngineError("cancelled", "作业已取消") from None
        # Provider errors and generated payloads may contain source text or secrets.
        updated.update({"title_status": "skipped", "title_skill_version": TITLE_SKILL_VERSION,
                        "title_enabled_snapshot": True, "title_usage_units": 0,
                        "title_request_count": stage.request_count if stage else 0,
                        "title_billing_mode": str(settings.get("agent_billing_mode") or "byok"),
                        "title_quota_exceeded": getattr(exc, "code", "") == "quota_exceeded",
                        **agent_failure_metadata(exc, "title")})
    _write_manifest_file(updated)
    return updated


def batch_apply_agent_translation(
    job_ids: list[str],
    queue: object,
    settings: Mapping[str, object],
    *,
    update_title: bool = True,
    delete_source: bool = False,
) -> dict[str, object]:
    """批量对已完成任务执行 Agent 整理，逐条回写 manifest 与标题。

    返回统计而不是抛出整体异常：单条失败只计入 failed，不影响其余任务。
    """
    done = skipped = failed = 0
    errors: list[str] = []
    for job_id in job_ids:
        settlements = []
        try:
            job = queue.get_job(job_id)
            manifest = queue.get_manifest(job_id)
            if not job or job.get("status") != "done" or not manifest:
                skipped += 1
                continue
            effective = dict(settings)
            effective["_agent_operation_id"] = uuid.uuid4().hex
            effective["_agent_stage_settlements"] = settlements
            stage_manifest = dict(manifest)
            stage_manifest.update({"body_enabled_snapshot": True,
                "title_enabled_snapshot": bool(settings.get("title_optimization_enabled")),
                "body_status": "pending", "title_status": "pending" if settings.get("title_optimization_enabled") else "disabled",
                "body_quota_exceeded": False, "title_quota_exceeded": False,
                "body_error_code": "", "body_error": "", "title_error_code": "", "title_error": "",
                "body_usage_units": 0, "title_usage_units": 0, "body_request_count": 0, "title_request_count": 0,
                "body_billing_mode": str(settings.get("agent_billing_mode") or "byok"),
                "title_billing_mode": str(settings.get("agent_billing_mode") or "byok") if settings.get("title_optimization_enabled") else "none"})
            body_failed = None
            try:
                updated = apply_agent_translation(
                    stage_manifest, effective, source_title=job.get("source_title", ""),
                )
                updated["body_status"] = "done"
            except Exception as exc:
                if not settings.get("title_optimization_enabled"):
                    # Persist failure facts without rewriting any usable output.
                    stage_manifest.update({"body_status": "skipped",
                        "body_quota_exceeded": getattr(exc, "code", "") == "quota_exceeded",
                        **getattr(exc, "stage_metadata", {}), **agent_failure_metadata(exc, "body")})
                    queue.replace_manifest(stage_manifest)
                    raise
                body_failed = type(exc).__name__
                updated = {**stage_manifest, "body_status": "skipped",
                    "body_quota_exceeded": getattr(exc, "code", "") == "quota_exceeded",
                    **getattr(exc, "stage_metadata", {}), **agent_failure_metadata(exc, "body")}
            if settings.get("title_optimization_enabled"):
                updated = apply_title_optimization(updated, effective)
            queue.replace_manifest(updated)
            billing_metadata = settle_agent_stages(settlements, success=True)
            if billing_metadata:
                updated.update(billing_metadata)
                queue.replace_manifest(updated)
            agent_tags = list(updated.get("agent_tags") or [])
            if agent_tags:
                queue.set_tags(job_id, agent_tags)
            title = str(updated.get("source_title") or "").strip()
            if (
                title
                and updated.get("title_status") == "done"
                and update_title
                and settings.get("update_title_after_transcribe", True)
            ):
                queue.set_source_title(job_id, title)
            if delete_source and settings.get("delete_source_after_translation"):
                from app.worker import Worker
                Worker._delete_downloaded_source(job.get("source_path", ""), settings)
            if body_failed or updated.get("title_status") == "skipped":
                failed += 1
                if len(errors) < 10:
                    errors.append(f"{job_id}: {body_failed or 'TitleIncomplete'}")
            else:
                done += 1
        except Exception as exc:  # 单条失败不回滚整批
            failed += 1
            if len(errors) < 10:
                errors.append(f"{job_id}: {type(exc).__name__}")
        finally:
            if settlements:
                settle_agent_stages(settlements, success=False)
    return {"done": done, "skipped": skipped, "failed": failed, "errors": errors}


def _write_manifest_file(manifest: Manifest) -> None:
    raw_path = Path(manifest["raw_path"])
    stem = raw_path.stem.removesuffix("_raw")
    _atomic_write(raw_path.parent / f"{stem}_manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.stem}-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def _article_filename(raw_path: Path, title: str) -> str:
    value = " ".join(str(title or "").replace("\x00", "").split()).strip()
    value = value.replace("/", " ").replace("\\", " ")
    value = "".join(" " if char in '<>:"|?*' else char for char in value).strip(" .")
    return f"{(value or raw_path.stem.removesuffix('_raw'))[:100]}_文章.md"


def _with_title(content: str, title: str) -> str:
    clean = " ".join(str(title).split()).strip()
    if re.match(r"\A\s*# [^\n]*", content):
        return re.sub(r"\A\s*# [^\n]*", lambda _: "# " + clean, content, count=1)
    return "# " + clean + "\n\n" + content
