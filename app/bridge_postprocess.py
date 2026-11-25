# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""Desktop translation and batch-processing lifecycle behind the Bridge."""
from __future__ import annotations

import hashlib
import threading
from pathlib import Path


def _prefixed_ids(task_ids: list, prefix: str) -> list[str]:
    if not task_ids:
        return []
    return [t[len(prefix):] for t in task_ids if isinstance(t, str) and t.startswith(prefix)]


class PostprocessBridgeMixin:
    def _init_postprocessing(self, postprocess_store) -> None:
        from engine.postprocess_store import PostprocessStore

        self._postprocess = postprocess_store if postprocess_store is not None else PostprocessStore()
        self._recover_postprocess()
        self._postprocess_stop = threading.Event()
        self._postprocess_threads: list[threading.Thread] = []
        self._postprocess_closed = False
        self._batch_agent = self._postprocess.latest("batch") or {
            "running": False, "done": 0, "skipped": 0, "failed": 0, "errors": [], "message": "",
        }
        self._translation: dict = self._postprocess.latest("translation") or {
            "running": False, "task_id": "", "status": "", "error": "", "lang": "",
        }
        self._translation_failures: dict[str, str] = {}

    def _recover_postprocess(self) -> None:
        # Publishing the manifest can finish just before the desktop exits.
        # Its run identity proves delivery even if the receipt was not updated.
        for pending in self._postprocess.unfinished("translation"):
            manifest = self.controller.queue.get_manifest(pending["subject"])
            if (isinstance(manifest, dict) and manifest.get("translation_run_id") == pending["run_id"]
                    and all(Path(str(manifest.get(key) or "")).is_file()
                            for key in ("translation_path", "translation_segments_path"))):
                self._postprocess.update(pending["run_id"], running=False, status="done", error="",
                                         code="", retryable=False, lang=manifest.get("translation_lang", ""))
        if self._restore_entry_error() is None:
            self._postprocess.interrupt_running()


    def close_postprocessing(self) -> None:
        with self._lock:
            if self._postprocess_closed:
                return
            self._postprocess_stop.set()
            threads = list(self._postprocess_threads)
            accepted = bool(self._translation.get("running") or self._batch_agent.get("running"))
        for thread in threads:
            if thread is not threading.current_thread():
                thread.join(timeout=2)
        if self._restore_entry_error() is None:
            self._postprocess.interrupt_running()
        if (not accepted or threads) and not any(thread.is_alive() for thread in threads):
            self._postprocess.close()
            self._postprocess_closed = True


    def translate_text(self, task_id: str, direction: str = "auto") -> dict:
        """对单篇已完成文本发起中英翻译（先生 2026-10-25）。

        托管模式计费搭既有 body 阶段积分；BYOK 走用户自配 API。同一时间仅
        一个翻译在跑，进度经 get_translation_status 轮询。
        """
        with self._lock:
            if error := self._restore_entry_error():
                return error
            if self._postprocess_stop.is_set():
                return {"ok": False, "error": "应用正在退出，请重新打开后再翻译"}
            bare = task_id[4:] if isinstance(task_id, str) and task_id.startswith("job:") else task_id
            if not isinstance(bare, str) or not bare:
                return {"ok": False, "error": "任务不存在"}
            if direction not in ("auto", "zh-en", "en-zh"):
                return {"ok": False, "error": "翻译方向无效"}
            job = self.controller.queue.get_job(bare)
            if job is None or job.get("status") != "done":
                return {"ok": False, "error": "只有已完成的文本才能翻译"}
            manifest = self.controller.queue.get_manifest(bare)
            if not manifest or not (manifest.get("final_path") or manifest.get("raw_path")):
                return {"ok": False, "error": "未找到可翻译的文本"}
            settings = self.controller.settings
            if not settings.get("agent_enabled"):
                return {"ok": False, "error": "请先在设置 - 整理与转译中启用 Agent 转译"}
            if settings.get("agent_billing_mode") == "hosted":
                if not self.get_subscription_status().get("hosted_agent_ready"):
                    return {"ok": False, "error": "请先检查托管模型服务与账户积分"}
            elif not str(settings.get("agent_api_key") or "").strip():
                return {"ok": False, "error": "请先在设置 - 整理与转译中配置 API Key"}
            if self._translation.get("running"):
                return {"ok": False, "error": "已有翻译进行中，请稍候"}
            if self._batch_agent.get("running") and f"job:{bare}" in self._batch_agent.get("task_ids", []):
                return {"ok": False, "error": "该文稿正在批量整理，请结束后再翻译"}
            self._translation_failures.pop(bare, None)
            self._translation = self._postprocess.start("translation", bare, {
                "task_id": bare, "direction": direction, "error": "", "lang": "",
            })
            run_id = self._translation["run_id"]
        thread = threading.Thread(
            target=self._translation_worker, args=(bare, direction, run_id), daemon=True,
            name=f"translate-{bare}",
        )
        with self._lock:
            if self._postprocess_stop.is_set():
                self._postprocess.interrupt_running()
                return {"ok": False, "error": "应用正在退出，请重新打开后再翻译"}
            thread.start()
            self._postprocess_threads.append(thread)
        return {"ok": True, "started": True, "run_id": run_id}


    def get_translation_status(self, task_id: str) -> dict:
        bare = task_id.removeprefix("job:") if isinstance(task_id, str) else ""
        with self._lock:
            manifest = self.controller.queue.get_manifest(bare) if bare else None
            previous_failure = self._translation_failures.get(bare)
            if bare and self._translation.get("task_id") == bare:
                state = dict(self._translation)
            else:
                state = self._postprocess.latest("translation", bare) or {
                    "running": False, "task_id": bare, "status": "failed" if previous_failure else "",
                    "error": previous_failure or "", "lang": ""}
        if manifest and manifest.get("translation_status") == "done":
            state.update({
                "lang": str(manifest.get("translation_lang") or ""),
                "direction": str(manifest.get("translation_direction") or ""),
                "has_translation": self._translation_exists(manifest),
            })
            if not state["status"]:
                state["status"] = "done"
        return state


    @staticmethod
    def _translation_exists(manifest: dict) -> bool:
        artifact = manifest.get("translation_path")
        return manifest.get("translation_status") == "done" and bool(
            artifact and Path(artifact).expanduser().is_file()
        )


    def _record_translation(self, run_id: str, **changes) -> dict:
        value = self._postprocess.update(run_id, **changes)
        # A published run may still be settling while the user starts another.
        if self._translation.get("run_id") == run_id:
            self._translation = value
        return value


    def _translation_worker(self, job_id: str, direction: str, run_id: str = "") -> None:
        from app.translation_service import discard_translation, settle_agent_stages, translate_article

        settlements: list = []
        settings = dict(self.controller.settings)
        with self._lock:
            if not run_id:
                self._translation = self._postprocess.start("translation", job_id, {
                    "task_id": job_id, "direction": direction, "error": "", "lang": "",
                })
                run_id = self._translation["run_id"]
        settings["_agent_operation_id"] = f"translate:{run_id}"
        settings["_translation_run_id"] = run_id
        settings["_cancel_check"] = self._postprocess_stop.is_set
        settings["_translation_checkpoint"] = lambda **values: self._postprocess.update(run_id, **values)
        settings["_agent_stage_settlements"] = settlements
        if settings.get("agent_billing_mode") == "hosted":
            from engine.device import device_id
            settings["_agent_device_id"] = device_id(str(self.settings_store.base_dir))
        updated = None
        published = False
        try:
            with self._lock:
                manifest = self.controller.queue.get_manifest(job_id)
            if not manifest:
                raise RuntimeError("任务不存在")
            updated = translate_article(manifest, settings, direction)
            with self._lock, self.controller.media_action_lock:
                if self._postprocess_stop.is_set():
                    raise RuntimeError("应用退出中，翻译已中断，可重新执行")
                if self._restore_entry_error() is not None:
                    raise RuntimeError("备份恢复尚未完成")
                if source_revision := updated.get("translation_source_revision"):
                    source_path = Path(str(updated.get(f"{updated['translation_source_tab']}_path") or ""))
                    current = source_path.read_text(encoding="utf-8")
                    if hashlib.sha256(current.encode("utf-8")).hexdigest() != source_revision:
                        raise RuntimeError("翻译期间原文已修改，请重新翻译当前版本")
                self.controller.queue.replace_manifest(updated)
                published = True
                self._record_translation(run_id, running=False, status="done",
                    error="", code="", retryable=False, lang=str(updated.get("translation_lang") or ""))
            settle_agent_stages(settlements, success=True)
            discard_translation(manifest)
            with self._lock:
                self._translation_failures.pop(job_id, None)
                self._record_translation(run_id, running=False, status="done",
                    error="", code="", retryable=False, lang=str(updated.get("translation_lang") or ""))
        except Exception as exc:  # 失败不落 manifest，保留上一次的可用译文
            if updated is not None and not published:
                discard_translation(updated)
            if settlements:
                settle_agent_stages(settlements, success=False)
            message = str(getattr(exc, "message", "") or exc)[:300] or "翻译失败"
            with self._lock:
                self._translation_failures[job_id] = message
                self._record_translation(run_id, running=False, status="failed",
                    error=message, retryable=True, lang="")


    def batch_agent_translate(self, task_ids: list[str], editing_level: str = "", profile: str = "") -> dict:
        """对选中的已完成任务批量生成文章；可指定整理力度/画像重跑（先生
        2026-11-02：文本库中可重新选择模式再转写）。空串表示沿用当前设置。"""
        if editing_level and editing_level not in ("auto", "light", "balanced", "structured", "rewrite"):
            return {"ok": False, "error": "整理力度无效"}
        valid_profiles = {"", "auto", "emotional_copy", "consulting", "knowledge"}
        if profile and (profile not in valid_profiles and not profile.startswith("custom:")):
            return {"ok": False, "error": "内容策略无效"}
        with self._lock:
            if error := self._processing_entry_error():
                return error
            if self._batch_agent.get("running"):
                return {"ok": False, "error": "已有批量整理进行中，请稍候"}
            if self._postprocess_stop.is_set():
                return {"ok": False, "error": "应用正在退出，请重新打开后再整理"}
            jobs = _prefixed_ids(task_ids, "job:")
            if self._translation.get("running") and self._translation.get("task_id") in jobs:
                return {"ok": False, "error": "该文稿正在翻译，请结束后再整理"}
            settings = dict(self.controller.settings)
            if editing_level:
                settings["agent_editing_level"] = editing_level
            if profile:
                settings["agent_profile"] = profile
            if not settings.get("agent_enabled"):
                return {"ok": False, "error": "请先在设置 - 整理与转译中启用 Agent 转译"}
            if settings.get("agent_billing_mode") == "hosted":
                if not self.get_subscription_status().get("hosted_agent_ready"):
                    return {"ok": False, "error": "请先检查托管模型服务与账户积分"}
            elif not str(settings.get("agent_api_key") or "").strip():
                return {"ok": False, "error": "请先在设置 - 整理与转译中配置 API Key"}
            if not jobs:
                return {"ok": False, "error": "没有可整理的已完成任务"}
            self._batch_agent = self._postprocess.start("batch", "", {
                "running": True,
                "task_ids": [f"job:{job_id}" for job_id in jobs],
                "done": 0,
                "skipped": 0,
                "failed": 0,
                "errors": [],
                "failed_ids": [],
                "message": f"批量整理已开始，共 {len(jobs)} 项",
            })
            run_id = self._batch_agent["run_id"]
        thread = threading.Thread(
            target=self._batch_agent_worker, args=(jobs, settings), daemon=True
        )
        with self._lock:
            if self._postprocess_stop.is_set():
                self._postprocess.interrupt_running()
                return {"ok": False, "error": "应用正在退出，请重新打开后再整理"}
            thread.start()
            self._postprocess_threads.append(thread)
        return {"ok": True, "started": True, "count": len(jobs), "run_id": run_id}


    def get_batch_agent_status(self) -> dict:
        with self._lock:
            return dict(self._batch_agent)


    def _batch_agent_worker(self, job_ids: list[str], settings: dict | None = None) -> None:
        from app.agent_service import batch_apply_agent_translation

        settings = settings if settings is not None else dict(self.controller.settings)
        with self._lock:
            if not self._batch_agent.get("run_id") or not self._batch_agent.get("running"):
                self._batch_agent = self._postprocess.start("batch", "", {
                    "task_ids": [f"job:{job_id}" for job_id in job_ids],
                    "done": 0, "skipped": 0, "failed": 0, "errors": [], "failed_ids": [],
                })
            run_id = self._batch_agent["run_id"]
        def progress(result):
            with self._lock:
                self._batch_agent = self._postprocess.update(run_id, **result)
        if settings.get("agent_billing_mode") == "hosted":
            from engine.device import device_id
            settings["_agent_device_id"] = device_id(str(self.settings_store.base_dir))
        try:
            result = batch_apply_agent_translation(
                job_ids,
                self.controller.queue,
                settings,
                delete_source=True,
                cancel_check=self._postprocess_stop.is_set,
                progress=progress,
                publication_lock=self._lock,
            )
        except Exception as exc:  # 整批失败兜底，保证状态可读
            with self._lock:
                partial = dict(self._batch_agent)
            processed = set(partial.get("processed_ids", []))
            pending = [job_id for job_id in job_ids if f"job:{job_id}" not in processed]
            result = {"done": partial.get("done", 0), "skipped": partial.get("skipped", 0),
                      "failed": int(partial.get("failed", 0)) + len(pending),
                      "errors": [*partial.get("errors", []), str(exc)],
                      "failed_ids": [value.removeprefix("job:") for value in partial.get("failed_ids", [])] + pending}
        with self._lock:
            self._batch_agent = self._postprocess.update(run_id,
                running=False,
                status="failed" if result.get("failed") else "done",
                code="interrupted" if self._postprocess_stop.is_set() else "",
                retryable=bool(result.get("failed")),
                done=int(result.get("done") or 0),
                skipped=int(result.get("skipped") or 0),
                failed=int(result.get("failed") or 0),
                errors=list(result.get("errors") or [])[:10],
                failed_ids=[f"job:{job_id}" for job_id in result.get("failed_ids", [])],
                message=(
                    f"批量整理完成：成功 {result.get('done', 0)}，"
                    f"跳过 {result.get('skipped', 0)}，失败 {result.get('failed', 0)}"
                ),
            )


    def _library_deletion_busy(self, job_ids: list[str]) -> dict | None:
        if self._batch_agent.get("running"):
            return {"ok": False, "error": "正在整理文本，请完成后再删除"}
        if self._translation.get("running") and self._translation.get("task_id") in job_ids:
            return {"ok": False, "error": "该文本正在翻译，请完成后再删除"}
        return None

