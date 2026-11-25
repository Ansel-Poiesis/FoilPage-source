# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""Post-ASR Agent orchestration and durable article output."""
from __future__ import annotations

import json
import os
import re
import tempfile
import uuid
from contextlib import nullcontext
from pathlib import Path
from typing import Callable, Mapping

from engine.agent_client import AgentStage, rewrite_transcript, settle_agent_stages, agent_failure_metadata
from engine.contracts import Manifest, EngineError


def _fidelity_fallback_reason(result: object) -> str:
    """保真降级原因只接受固定分类；缺失或陌生的值一律记为空。"""
    value = str(getattr(result, "fidelity_fallback_reason", "") or "")
    return value if value in {"numbers", "short", "numbers+short"} else ""


def _load_segments(manifest: Manifest) -> list | None:
    """字幕分段是可选的视觉/定位侧车；缺失或损坏时绝不拖垮可用的原始转写。"""
    segments_path = str(manifest.get("segments_path") or "").strip()
    if not segments_path:
        return None
    try:
        value = json.loads(Path(segments_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, list) else None


def _raw_transcript_text(manifest: Manifest, segments: list | None) -> str:
    """读取原始转写；过程文件已按设置清理时，用字幕分段逐字重建。

    拼接规则与 engine.asr 一致（中日韩直接相连，其余语言按空格相连并折叠空白），
    所以开启"转写后删除过程文件"的任务仍可重新整理。两者都没有时保持原错误。
    """
    raw_path = Path(str(manifest.get("raw_path") or ""))
    if str(raw_path) and raw_path.is_file():
        return raw_path.read_text(encoding="utf-8")
    texts = [str(item.get("text") or "").strip() for item in segments or [] if str(item.get("text") or "").strip()]
    if not texts:
        raise FileNotFoundError(str(raw_path))
    if str(manifest.get("language") or "") in ("zh", "ja", "ko"):
        return "".join(texts)
    return re.sub(r"\s+", " ", " ".join(texts)).strip()


def apply_agent_translation(
    manifest: Manifest,
    settings: Mapping[str, object],
    *,
    source_title: str = "",
    cancel_check: Callable[[], bool] | None = None,
    output_run_id: str = "",
    write_manifest_file: bool = True,
) -> Manifest:
    """Create an article beside raw text and return an updated manifest."""
    raw_path = Path(manifest["raw_path"])
    segments = _load_segments(manifest)
    raw_text = _raw_transcript_text(manifest, segments)
    original_title = str(manifest.get("original_source_title") or source_title or manifest.get("source_title") or "").strip()
    current_title = str(source_title or manifest.get("source_title") or original_title).strip()
    stage = AgentStage(settings, "body", raw_text,
                       str(settings.get("_agent_operation_id") or uuid.uuid4().hex), cancel_check)
    article_path = _run_output_path(raw_path.with_name(_article_filename(raw_path, current_title)), output_run_id)
    completed = False
    try:
        with stage:
            result = rewrite_transcript(
                raw_text, stage.settings, title=current_title,
                source_path=str(manifest.get("source_path") or ""), segments=segments,
                duration_sec=float(manifest.get("duration_sec") or 0.0), cancel_check=cancel_check,
            )
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
                "prompt_version": "agent-v6-dense-paragraph-bold",
                "agent_prompt_version": "agent-v6-dense-paragraph-bold",
                "agent_editing_level": str(getattr(result, "editing_level", "") or settings.get("agent_editing_level") or "balanced"),
                "agent_profile": result.profile,
                "agent_model": stage.model if stage.mode == "hosted" else result.model,
                "agent_status": "done", "agent_tags": tags, "body_error_code": "", "body_error": "",
                "agent_vision_used": getattr(result, "vision_used", False) is True,
                "agent_vision_frames": vision_frames_value,
                "agent_fidelity_fallback": getattr(result, "fidelity_fallback", False) is True,
                "agent_fidelity_fallback_reason": _fidelity_fallback_reason(result),
            })
            if not stage.request_count:
                count = getattr(result, "request_count", 0)
                updated["body_request_count"] = count if isinstance(count, int) and not isinstance(count, bool) and count >= 0 else 0
            if write_manifest_file:
                _write_manifest_file(updated)
        completed = True
        return updated
    finally:
        if output_run_id and not completed:
            article_path.unlink(missing_ok=True)



def apply_title_optimization(manifest: Manifest, settings: Mapping[str, object], *, cancel_check: Callable[[], bool] | None = None,
                             output_run_id: str = "", write_manifest_file: bool = True) -> Manifest:
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
        text = _raw_transcript_text(manifest, _load_segments(manifest))
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
            final_path = _run_output_path(final_path, output_run_id)
            old_content = final_path.read_text(encoding="utf-8") if final_path.exists() else None
            changed_path = final_path
            _atomic_write(final_path, _with_title(content, title))
            updated.update({"source_title": title, "original_source_title": original_title,
                            "optimized_source_title": title, "title_enabled_snapshot": True,
                            "final_path": str(final_path), "title_status": "done", "title_skill_version": TITLE_SKILL_VERSION,
                            "title_model": stage.model if stage.mode == "hosted" else str(settings.get("title_model") or settings.get("agent_model") or ""),
                            "title_error": "", "title_quota_exceeded": False, **stage.metadata()})
            updated["title_error_code"] = ""
            if write_manifest_file:
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
    if write_manifest_file:
        _write_manifest_file(updated)
    return updated


def batch_apply_agent_translation(
    job_ids: list[str],
    queue: object,
    settings: Mapping[str, object],
    *,
    update_title: bool = True,
    delete_source: bool = False,
    cancel_check: Callable[[], bool] | None = None,
    progress: Callable[[dict], None] | None = None,
    publication_lock=None,
) -> dict[str, object]:
    """批量对已完成任务执行 Agent 整理，逐条回写 manifest 与标题。

    返回统计而不是抛出整体异常：单条失败只计入 failed，不影响其余任务。
    """
    done = skipped = failed = 0
    errors: list[str] = []
    failed_ids: list[str] = []
    processed_ids: list[str] = []
    guard = publication_lock if publication_lock is not None else nullcontext()

    def check_cancelled():
        if cancel_check is not None and cancel_check():
            raise EngineError("cancelled", "处理已中断，可重新执行未完成项")

    def record_progress(job_id):
        processed_ids.append(f"job:{job_id}")
        if progress is not None:
            progress({"done": done, "skipped": skipped, "failed": failed,
                      "errors": errors[:10], "failed_ids": [f"job:{value}" for value in failed_ids],
                      "processed_ids": list(processed_ids)})

    for index, job_id in enumerate(job_ids):
        if cancel_check is not None and cancel_check():
            failed_ids.extend(job_ids[index:])
            failed += len(job_ids) - index
            errors.append("处理已中断，可重新执行未完成项")
            break
        settlements = []
        outputs: set[Path] = set()
        published = counted = False
        try:
            job = queue.get_job(job_id)
            manifest = queue.get_manifest(job_id)
            if not job or job.get("status") != "done" or not manifest:
                with guard:
                    check_cancelled()
                    skipped += 1
                    counted = True
                    record_progress(job_id)
                continue
            effective = dict(settings)
            run_id = uuid.uuid4().hex
            effective["_agent_operation_id"] = run_id
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
                    cancel_check=cancel_check, output_run_id=run_id, write_manifest_file=False,
                )
                if Path(updated["final_path"]).stem.endswith(".foilpage-" + run_id):
                    outputs.add(Path(updated["final_path"]))
                updated["body_status"] = "done"
            except Exception as exc:
                body_failed = type(exc).__name__
                updated = {**stage_manifest, "body_status": "skipped",
                    "body_quota_exceeded": getattr(exc, "code", "") == "quota_exceeded",
                    **getattr(exc, "stage_metadata", {}), **agent_failure_metadata(exc, "body")}
            if settings.get("title_optimization_enabled"):
                updated = apply_title_optimization(updated, effective, cancel_check=cancel_check,
                                                   output_run_id=run_id, write_manifest_file=False)
                if Path(updated["final_path"]).stem.endswith(".foilpage-" + run_id):
                    outputs.add(Path(updated["final_path"]))
            with guard:
                check_cancelled()
                queue.replace_manifest(updated)
                published = True
                agent_tags = list(updated.get("agent_tags") or [])
                if agent_tags and not body_failed:
                    queue.set_tags(job_id, agent_tags)
                title = str(updated.get("source_title") or "").strip()
                if (title and updated.get("title_status") == "done" and update_title
                        and settings.get("update_title_after_transcribe", True)):
                    queue.set_source_title(job_id, title)
                if (delete_source and settings.get("delete_source_after_translation")
                        and (not body_failed or updated.get("title_status") == "done")):
                    from app.worker import Worker
                    Worker._delete_downloaded_source(job.get("source_path", ""), settings)
                if body_failed or updated.get("title_status") == "skipped":
                    failed += 1
                    failed_ids.append(job_id)
                    if len(errors) < 10:
                        errors.append(f"{job_id}: {body_failed or 'TitleIncomplete'}")
                else:
                    done += 1
                counted = True
                record_progress(job_id)
            # Provider settlement can wait on the network; never hold the
            # desktop publication lock while waiting for it.
            billing_metadata = settle_agent_stages(settlements, success=True)
            if billing_metadata:
                with guard:
                    if cancel_check is None or not cancel_check():
                        current = queue.get_manifest(job_id)
                        if current and current.get("final_path") == updated.get("final_path"):
                            queue.replace_manifest({**current, **billing_metadata})
        except Exception as exc:  # 单条失败不回滚整批
            if not counted:
                failed += 1
                failed_ids.append(job_id)
                if len(errors) < 10:
                    errors.append(f"{job_id}: {type(exc).__name__}")
                with guard:
                    if cancel_check is None or not cancel_check():
                        record_progress(job_id)
        finally:
            if settlements:
                settle_agent_stages(settlements, success=False)
            if not published:
                for output in outputs:
                    output.unlink(missing_ok=True)
    return {"done": done, "skipped": skipped, "failed": failed, "errors": errors, "failed_ids": failed_ids}


def _run_output_path(path: Path, run_id: str) -> Path:
    if not run_id:
        return path
    if not re.fullmatch(r"[a-f0-9]{24}|[a-f0-9]{32}", run_id):
        raise ValueError("文章运行标识无效")
    if path.stem.endswith(".foilpage-" + run_id):
        return path
    stem = re.sub(r"\.foilpage-(?:[a-f0-9]{24}|[a-f0-9]{32})$", "", path.stem)
    return path.with_name(f"{stem}.foilpage-{run_id}.md")


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
