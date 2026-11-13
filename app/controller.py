# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""UI-facing application service combining settings, media and queue stores."""
from __future__ import annotations

import threading
import json
import logging
import uuid
import re
import tempfile
import time
from dataclasses import dataclass, field
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
import shutil

from engine.media_store import ImportSummary, MediaStore
from engine.download_store import DownloadStore
from engine.queue_store import QueueStateError, QueueStore, QueueStoreError
from engine.settings import SettingsStore
from engine.agent_protocol import (
    AGENT_MAX_TEXT_BYTES, DocumentRevisionConflict, DocumentTooLargeError, DocumentUnavailableError,
    document_metadata, document_revision,
)

_LOG = logging.getLogger(__name__)


@dataclass
class QueueResult:
    created: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)


class AppController:
    def __init__(self, settings_store: SettingsStore | None = None) -> None:
        self._settings_lock = threading.RLock()
        self.media_action_lock = threading.RLock()
        self.settings_store = settings_store or SettingsStore()
        from engine.agent_activity import AgentActivity
        self.agent_activity = AgentActivity(self.settings_store.base_dir / "agent-activity.json")
        from engine.product_events import ProductEvents
        self.product_events = ProductEvents(self.settings_store.base_dir / "product-notifications.json")
        self.settings = self.settings_store.load()
        if not self.settings.get("local_api_token"):
            self.settings["local_api_token"] = self.settings_store.ensure_local_api_token()
        self.settings_store.ensure_runtime_directories()
        self.media = MediaStore(self.settings["database_path"])
        self.queue = QueueStore(self.settings["database_path"])
        self.downloads = DownloadStore(self.settings["database_path"])
        from engine.video_catalog import VideoCatalog
        from engine.retention import FailedTaskRetention
        self.video_catalog = VideoCatalog(self.media)
        self.failed_task_retention = FailedTaskRetention(self.settings["database_path"])
        self._backfill_download_titles()
        self._recover_browser_downloads()

    def _backfill_download_titles(self) -> None:
        for item in self.downloads.list(limit=500):
            job_id = item.get("job_id")
            title = item.get("title", "")
            if not job_id or not title:
                continue
            job = self.queue.get_job(job_id)
            if job and not job.get("source_title"):
                self.queue.set_source_title(job_id, title)

    def _recover_browser_downloads(self) -> None:
        """Re-link completed browser downloads whose media or ASR rows were lost."""
        for item in self.downloads.list(limit=500):
            try:
                self._recover_one_download(item)
            except Exception:
                continue

    def _recover_one_download(self, item: dict) -> None:
        if item.get("options", {}).get("video_library_removed"):
            return
        status = item["status"]
        linked_id = item.get("job_id")
        if status in ("imported", "queued", "transcribing"):
            if linked_id and self.queue.get_job(linked_id):
                return
        elif status == "done":
            if not bool(item["options"].get("transcribe_after_download", True)):
                return
            if linked_id and self.queue.get_job(linked_id):
                return
        else:
            return

        media_id = item.get("media_id")
        try:
            media = self.media.get_media(media_id) if media_id else None
        except Exception:
            media = None
        source_path = item.get("downloaded_path")
        if (media is None or media.get("missing")) and source_path and Path(source_path).is_file():
            imported = self.import_files([source_path], display_names=[item.get("title", "")])[0]
            media_id = imported.item["id"]
            media = imported.item
        if media is None or media.get("missing"):
            return
        source_path = media.get("source_path")
        if not source_path or not Path(source_path).is_file():
            return

        active = self._active_job_for_source(source_path)
        if not active:
            queued = self.enqueue_media([media_id])
            active = queued.created[0] if queued.created else self._active_job_for_source(source_path)
        if not active:
            return
        title = item.get("title", "").strip()
        if title:
            self.queue.set_source_title(active, title)
        self.downloads.transition(item["id"], "queued", media_id=media_id, job_id=active)

    def _active_job_for_source(self, source_path: str) -> str | None:
        for job in reversed(self.queue.list_jobs()):
            if job["source_path"] == source_path and job["status"] in (
                "queued", "running", "retryable", "done",
            ):
                return job["id"]
        return None

    def import_files(self, paths: list[str], *, display_names: list[str] | None = None):
        with self.media_action_lock:
            labels = display_names or []
            return [
                self.media.import_file(path, display_name=labels[index] if index < len(labels) else "")
                for index, path in enumerate(paths)
            ]

    def import_folder(self, path: str, *, recursive: bool = True) -> ImportSummary:
        return self.media.import_directory(path, recursive=recursive)

    def import_paths(self, paths, *, progress=None, cancel_event=None) -> ImportSummary:
        return self.media.import_paths(
            paths, progress=progress, cancel_event=cancel_event
        )

    def media_rows(self, *, query: str = "", media_type=None) -> list[dict]:
        latest: dict[str, dict] = {}
        for job in self.queue.list_jobs():
            latest[job["source_path"]] = job
        rows = []
        for item in self.media.list_media(query=query, media_type=media_type):
            row = dict(item)
            job = latest.get(item["source_path"])
            row["job_id"] = job["id"] if job else None
            row["job_status"] = job["status"] if job else "ready"
            rows.append(row)
        return rows

    def clear_media_library(self) -> int:
        """Clear imported-media records without touching source files or jobs."""
        return self.media.clear_media()

    def job_rows(self, include_archived: bool = False) -> list[dict]:
        """Return jobs with a stable human title, including pre-migration jobs.

        Archived jobs are hidden from the workbench by default; the library
        reads them via ``include_archived=True`` so cleared results survive.
        """
        media_names = {
            item["source_path"]: Path(item["filename"]).stem
            for item in self.media.list_media()
        }
        download_names = {
            item["job_id"]: item["title"]
            for item in self.downloads.list(limit=500)
            if item.get("job_id") and item.get("title")
        }
        rows = []
        for job in self.queue.list_jobs():
            if job.get("archived") and not include_archived:
                continue
            row = dict(job)
            row["display_title"] = (
                job.get("source_title") or download_names.get(job["id"])
                or media_names.get(job["source_path"])
                or Path(job["source_path"]).stem
            )
            rows.append(row)
        return rows

    def task_rows(self, include_archived: bool = False) -> list[dict]:
        """Combine visible browser and transcription work without duplicate rows.

        ``linked_jobs`` is computed over every job (archived included) so a
        download row linked to an archived job is not shown standalone in the
        workbench; the job itself is skipped unless ``include_archived``.
        """
        rows: list[dict] = []
        linked_jobs: set[str] = set()
        for job in self.queue.list_jobs():
            linked_jobs.add(job["id"])
        # TASK-8.17：转写行从关联下载行继承封面（下载行被并入隐藏后，封面
        # 仍要在同一张任务卡上展示）。
        download_rows = self.downloads.list(limit=500)
        linked_covers = {
            item["job_id"]: item.get("cover_path")
            for item in download_rows
            if item.get("job_id") and item.get("cover_path")
        }
        for job in self.job_rows(include_archived=include_archived):
            row = dict(job)
            row.update({
                "kind": "transcription",
                "task_id": job["id"],
                "source": "本地转写",
                "progress": float(job.get("progress", 0.0)),
                "result_category": job.get("result_category", "未分类"),
                "cover_path": linked_covers.get(job["id"]),
            })
            rows.append(row)
        for download in download_rows:
            if download.get("job_id") in linked_jobs:
                continue
            row = dict(download)
            row.update({
                "kind": "download",
                "task_id": download["id"],
                "source": "浏览器下载",
                "display_title": download.get("title") or download["url"],
                "result_category": "浏览器下载",
                # Historical completed retries may retain a transient error.
                # Preserve the stored evidence without presenting it as failure.
                "error": None if download["status"] == "done" else download.get("error"),
                "final_path": None,
                "raw_path": None,
            })
            rows.append(row)
        return rows

    def clear_completed(self) -> int:
        """Archive finished (done) jobs: hidden from the workbench, library keeps results.

        Only the queue record is flagged; text artifacts and source videos are
        left untouched, so library results and future re-transcription survive.
        """
        cleared = 0
        for job in self.queue.list_jobs():
            if job.get("status") == "done" and not job.get("archived"):
                self.queue.set_archived(job["id"], True)
                cleared += 1
        return cleared

    def set_result_categories(self, job_ids: list[str], category: str) -> list[str]:
        updated: list[str] = []
        with self.media_action_lock:
            for job_id in job_ids:
                try:
                    self.queue.set_result_category(job_id, category)
                    updated.append(job_id)
                except QueueStateError:
                    continue
        return updated

    def set_job_tags(self, job_id: str, tags: list[str]) -> list[str]:
        """Set the tag list for a single completed job; returns normalized tags."""
        with self.media_action_lock:
            return self.queue.set_tags(job_id, tags)

    def _document_path(self, job_id: str, tab: str) -> Path:
        if tab not in ("final", "raw"):
            raise ValueError("仅整理稿和原始转写可以编辑")
        job = self.queue.get_job(job_id)
        if job is None or job.get("status") != "done":
            raise QueueStateError("仅已完成任务可以编辑")
        value = job.get("final_path" if tab == "final" else "raw_path")
        if not value or not self.settings.get("output_dir"):
            raise DocumentUnavailableError("文本文件不可用")
        path = Path(str(value)).expanduser()
        output_root = Path(self.settings["output_dir"]).expanduser()
        if (path.suffix.lower() not in {".md", ".txt"}
                or not output_root.is_dir() or not self._is_within(path, output_root)
                or not path.is_file()):
            raise DocumentUnavailableError("文本文件不可用")
        return path

    def read_document_text(self, job_id: str, tab: str = "final") -> dict:
        """Read a registered completed asset with the revision used by all editors."""
        with self.media_action_lock:
            path = self._document_path(job_id, tab)
            with path.open("rb") as stream:
                encoded = stream.read(AGENT_MAX_TEXT_BYTES + 1)
            if len(encoded) > AGENT_MAX_TEXT_BYTES:
                raise DocumentTooLargeError("文本过大，无法在应用内读取")
            text = encoded.decode("utf-8")
            return {"path": str(path), "size": len(encoded), "text": text,
                    "tab": tab, "revision": document_revision(job_id, tab, encoded)}

    def read_document_metadata(self, job_id: str) -> dict:
        with self.media_action_lock:
            job = self.queue.get_job(job_id)
            if job is None or job.get("status") != "done":
                raise QueueStateError("仅已完成任务可以编辑文稿")
            return document_metadata(job)

    def update_document_metadata(
        self, job_id: str, *, expected_revision: str,
        title: str | None = None, tags: list[str] | None = None, category: str | None = None,
    ) -> dict:
        with self.media_action_lock:
            return self.queue.update_document_metadata(
                job_id, expected_revision=expected_revision, title=title, tags=tags, category=category,
            )

    def update_document_text(
        self, job_id: str, tab: str, text: str, *, expected_revision: str | None = None,
    ) -> dict:
        """Atomically update a completed Markdown asset owned by the output folder."""
        if tab not in ("final", "raw"):
            raise ValueError("仅整理稿和原始转写可以编辑")
        if not isinstance(text, str):
            raise ValueError("文本格式无效")
        encoded = text.encode("utf-8")
        if len(encoded) > AGENT_MAX_TEXT_BYTES:
            raise DocumentTooLargeError("文本过大，无法在应用内保存")
        if expected_revision is not None and (
            not isinstance(expected_revision, str) or not re.fullmatch(r"[a-f0-9]{64}", expected_revision)
        ):
            raise ValueError("文稿版本无效")
        with self.media_action_lock:
            path = self._document_path(job_id, tab)
            if expected_revision is not None:
                current = self.read_document_text(job_id, tab)
                if current["revision"] != expected_revision:
                    raise DocumentRevisionConflict(current["revision"])
            self._atomic_write_text(path, text)
            # Hash the committed bytes: Windows text mode may normalize CRLF.
            committed = self.read_document_text(job_id, tab)
            return {"path": str(path), "size": committed["size"], "revision": committed["revision"]}

    @staticmethod
    def _atomic_write_text(path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="\n", delete=False,
            dir=path.parent, prefix=f".{path.stem}-", suffix=".tmp",
        ) as stream:
            stream.write(text)
            temp_path = Path(stream.name)
        try:
            temp_path.replace(path)
        finally:
            if temp_path.exists():
                temp_path.unlink(missing_ok=True)

    # ------------------------------------------------------------ 文件清理
    def _is_within(self, path: Path, root: Path) -> bool:
        try:
            path.resolve().relative_to(root.resolve())
            return True
        except ValueError:
            return False

    def _safe_remove_downloaded_source(self, path_value: object) -> None:
        """删除 FoilPage 下载目录内的源文件副本；用户自有文件绝不删除。

        图集任务的产物是任务子目录（TASK-8.18），同样只在下载目录内整目录移除。
        """
        if not path_value:
            return
        try:
            path = Path(str(path_value)).expanduser().resolve()
            root = Path(self.settings.get("download_dir", "")).expanduser().resolve()
            if self._is_within(path, root) and path != root:
                if path.is_dir():
                    shutil.rmtree(path, ignore_errors=True)
                elif path.is_file():
                    path.unlink(missing_ok=True)
        except OSError:
            pass

    def _purge_job_output_dir(self, job: dict) -> None:
        """删除该任务在输出根下的产物文件夹（raw / final / manifest json / 文章）。

        job.output_dir 记录的是输出根目录，实际产物位于 ``<output_dir>/<job_id>``；
        只删除该 job 自己的子目录，绝不触碰输出根或其他任务。
        """
        output_dir = job.get("output_dir") or ""
        job_id = job.get("id") or ""
        if output_dir and job_id:
            try:
                out_root = Path(str(output_dir)).expanduser().resolve()
                target = (out_root / str(job_id)).resolve()
                if target != out_root and self._is_within(target, out_root):
                    shutil.rmtree(target, ignore_errors=True)
            except OSError:
                pass

    def _purge_job_files(self, job: dict) -> None:
        """删除任务产物文件夹与下载目录内的源文件（彻底清理）。"""
        self._purge_job_output_dir(job)
        self._safe_remove_downloaded_source(job.get("source_path"))

    def purge_orphan_output(self, path_value: object) -> bool:
        """清理输出根内的孤儿产物目录（数据自检用，路径必须位于 output_dir 内）。"""
        if not path_value:
            return False
        try:
            target = Path(str(path_value)).expanduser().resolve()
            out_root = Path(self.settings["output_dir"]).expanduser().resolve()
            if target == out_root or not self._is_within(target, out_root):
                return False
            if not target.is_dir():
                return False
            shutil.rmtree(target, ignore_errors=True)
            return True
        except OSError:
            return False

    def purge_orphan_download(self, path_value: object) -> bool:
        """删除下载根内的孤儿源文件（路径必须位于 download_dir 内）。"""
        if not path_value:
            return False
        try:
            target = Path(str(path_value)).expanduser().resolve()
            root = Path(self.settings["download_dir"]).expanduser().resolve()
            if not self._is_within(target, root) or not target.is_file():
                return False
            target.unlink(missing_ok=True)
            return True
        except OSError:
            return False

    # ------------------------------------------------------------ 缓存清理
    @staticmethod
    def _dir_size(path: Path) -> int:
        return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())

    def _cache_candidates(self) -> list[Path]:
        """可再生的缓存候选：转写/下载临时残留与日志轮转备份。

        仅匹配本应用命名前缀；最近 1 小时内修改的目录视为可能正在使用，跳过。
        """
        now = time.time()
        candidates: list[Path] = []
        temp_root = Path(tempfile.gettempdir())
        for pattern in ("v2a_*", ".resolve-*"):
            candidates.extend(temp_root.glob(pattern))
        download_root = Path(self.settings["download_dir"]).expanduser()
        for pattern in (".direct-media-*", ".download-*"):
            candidates.extend(download_root.glob(pattern))
        log_dir = Path(self.settings_store.base_dir) / "logs"
        if log_dir.is_dir():
            for item in log_dir.iterdir():
                if item.is_file() and re.fullmatch(r".+\.log\.\d+", item.name):
                    candidates.append(item)
        fresh: list[Path] = []
        for item in candidates:
            try:
                if (now - item.stat().st_mtime) < 3600:
                    continue
            except OSError:
                continue
            fresh.append(item)
        return fresh

    def cache_status(self) -> dict:
        """缓存占用统计：可安全清理的临时/日志大小与模型缓存大小。"""
        cleanable = 0
        for item in self._cache_candidates():
            try:
                cleanable += self._dir_size(item) if item.is_dir() else item.stat().st_size
            except OSError:
                continue
        model_bytes = 0
        try:
            from engine.model_manager import ModelManager
            manager = ModelManager(
                self.settings.get("model_dir", "") or None,
                endpoint=self.settings.get("hf_endpoint", ""),
            )
            model_bytes = sum(state.bytes_on_disk for state in manager.list_models())
        except Exception:
            pass
        return {
            "ok": True,
            "cleanable_bytes": cleanable,
            "model_bytes": model_bytes,
        }

    def cleanup_cache(self) -> dict:
        """清理可再生的缓存（临时残留与日志备份）；绝不触碰用户数据。"""
        freed = 0
        removed: list[str] = []
        for item in self._cache_candidates():
            try:
                if item.is_dir():
                    size = self._dir_size(item)
                    shutil.rmtree(item, ignore_errors=True)
                elif item.is_file():
                    size = item.stat().st_size
                    item.unlink(missing_ok=True)
                else:
                    continue
                if not item.exists():
                    freed += size
                    removed.append(str(item))
            except OSError:
                continue
        return {"ok": True, "freed_bytes": freed, "removed": removed}

    def delete_texts(self, job_ids: list[str], *, force: bool = True) -> list[str]:
        """仅删除文本产物，保留任务、manifest、媒体及下载登记。

        ``force`` remains accepted for existing callers, but never bypasses
        the active-worker guard for text-only deletion.
        """
        deleted: list[str] = []
        with self.media_action_lock:
            for job_id in dict.fromkeys(job_ids):
                try:
                    if self.queue.clear_text_outputs(job_id, remove_files=self._remove_job_text_outputs):
                        deleted.append(job_id)
                except (QueueStoreError, OSError, ValueError):
                    continue
        return deleted

    @contextmanager
    def _remove_job_text_outputs(self, job: dict, manifest: dict | None):
        """Stage exact text assets; restore them if preparation or SQL fails."""
        output_root = Path(self.settings["output_dir"]).expanduser().resolve()
        recorded_root = Path(job["output_dir"]).expanduser().resolve()
        job_id = job["id"]
        if recorded_root != output_root or Path(job_id).name != job_id or job_id in (".", ".."):
            raise ValueError("文本产物目录不可用")
        job_dir = (recorded_root / job_id).resolve()
        if job_dir != recorded_root / job_id or job_dir.parent != output_root:
            raise ValueError("文本产物目录不可用")
        if job_dir.exists() and not job_dir.is_dir():
            raise ValueError("文本产物目录不可用")

        def owned_file(value: str, extensions: set[str]) -> Path:
            candidate = Path(value).expanduser()
            if candidate.is_symlink():
                raise ValueError("文本产物路径不可用")
            path = candidate.resolve()
            if path.parent != job_dir or path.suffix.lower() not in extensions:
                raise ValueError("文本产物路径不可用")
            if path.exists() and not path.is_file():
                raise ValueError("文本产物路径不可用")
            if path == Path(job["source_path"]).expanduser().resolve():
                raise ValueError("文本产物路径不可用")
            return path

        def segments_file(value: str) -> Path:
            path = owned_file(value, {".json"})
            if not path.name.endswith("_segments.json"):
                raise ValueError("字幕产物路径不可用")
            return path

        assets: set[Path] = set()
        stems: set[str] = set()
        for record in (job, manifest or {}):
            for key in ("raw_path", "final_path"):
                if record.get(key):
                    path = owned_file(record[key], {".md", ".txt"})
                    assets.add(path)
                    stems.add(path.stem.removesuffix("_raw") if key == "raw_path" else path.stem)
                    # Agent replaces final_path with a classified article, but
                    # the engine's initial final copy is still a text artifact.
                    if key == "raw_path" and path.stem.endswith("_raw"):
                        base = path.stem.removesuffix("_raw")
                        for suffix in (".md", ".txt"):
                            assets.add(owned_file(str(job_dir / (base + suffix)), {suffix}))
        if manifest and manifest.get("segments_path"):
            assets.add(segments_file(manifest["segments_path"]))

        sidecars: list[tuple[Path, str]] = []
        for stem in stems:
            for suffix in (".srt", ".vtt"):
                assets.add(owned_file(str(job_dir / (stem + suffix)), {suffix}))
            sidecar = owned_file(str(job_dir / (stem + "_manifest.json")), {".json"})
            if not sidecar.exists():
                continue
            value = json.loads(sidecar.read_text(encoding="utf-8"))
            if not isinstance(value, dict) or value.get("job_id") != job_id:
                raise ValueError("文本产物登记不匹配")
            for key in ("raw_path", "final_path"):
                if value.get(key):
                    assets.add(owned_file(value[key], {".md", ".txt"}))
            if value.get("segments_path"):
                assets.add(segments_file(value["segments_path"]))
            value.update(raw_path="", final_path="", segments_path="", text_outputs_cleared=True)
            sidecars.append((sidecar, json.dumps(value, ensure_ascii=False, indent=2) + "\n"))
        # All roots, types and sidecars have been checked before the first move.
        to_move = sorted({path for path in assets if path.exists()} | {path for path, _ in sidecars})
        if not to_move:
            yield
            return
        stage = Path(tempfile.mkdtemp(prefix=".text-delete-", dir=job_dir))
        if stage.resolve().parent != job_dir:
            raise ValueError("文本临时目录不可用")
        moved: list[tuple[Path, Path]] = []
        try:
            for path in to_move:
                backup = stage / path.name
                path.replace(backup)
                moved.append((path, backup))
            for path, content in sidecars:
                self._atomic_write_text(path, content)
            yield
        except BaseException:
            failed = []
            for path, backup in reversed(moved):
                try:
                    backup.replace(path)
                except OSError:
                    failed.append(path.name)
            if failed:
                _LOG.error("文本删除回滚未完成，已保留恢复暂存（任务 %s）", job_id)
                raise QueueStoreError("文本恢复未完成，请保留任务目录并重试")
            stage.rmdir()
            raise
        else:
            try:
                for _, backup in moved:
                    backup.unlink(missing_ok=True)
                stage.rmdir()
            except OSError:
                # The DB and public paths are committed; never try to restore
                # references now. Preserve any remaining exact originals.
                _LOG.warning("文本产物暂存尚未清理，已保留受控恢复目录（任务 %s）", job_id)

    def delete_files(self, job_ids: list[str]) -> list[str]:
        """删除文件：移除文本产物文件夹与下载目录内视频，保留数据库记录。"""
        with self.media_action_lock:
            return self._delete_files_locked(job_ids)

    def _delete_files_locked(self, job_ids: list[str]) -> list[str]:
        purged: list[str] = []
        for job_id in job_ids:
            job = self.queue.get_job(job_id)
            if job is None:
                continue
            self._purge_job_files(job)
            purged.append(job_id)
        return purged

    def delete_tasks(self, job_ids: list[str], *, force: bool = True) -> list[str]:
        """删除任务记录，并连同转写产物文件夹与 FoilPage 下载目录内的源文件一并移除。"""
        with self.media_action_lock:
            return self._delete_tasks_locked(job_ids, force=force)

    def _delete_tasks_locked(self, job_ids: list[str], *, force: bool = True) -> list[str]:
        deleted: list[str] = []
        for job_id in job_ids:
            try:
                job = self.queue.get_job(job_id)
                if self.queue.delete_job(job_id, force=force):
                    if job is not None:
                        self._purge_job_files(job)
                    deleted.append(job_id)
            except QueueStoreError:
                continue
        # 浏览器下载行若已关联到被删任务，一并移除，避免出现幻影任务。
        if deleted:
            removed_ids = set(deleted)
            for download in self.downloads.list(limit=500):
                if download.get("job_id") in removed_ids:
                    try:
                        self.downloads.delete(download["id"], force=True)
                        self._safe_remove_downloaded_source(download.get("downloaded_path"))
                        self._safe_remove_downloaded_source(download.get("cover_path"))
                    except Exception:
                        continue
        return deleted

    def delete_download_tasks(self, download_ids: list[str], *, force: bool = True) -> list[str]:
        """删除下载任务记录，并连同 FoilPage 下载目录内的文件一并移除。"""
        deleted: list[str] = []
        for download_id in download_ids:
            try:
                download = self.downloads.get(download_id)
                if self.downloads.delete(download_id, force=force):
                    if download is not None:
                        self._safe_remove_downloaded_source(download.get("downloaded_path"))
                        self._safe_remove_downloaded_source(download.get("cover_path"))
                    deleted.append(download_id)
            except Exception:
                continue
        return deleted

    def delete_download_files(self, download_ids: list[str]) -> list[str]:
        """仅删除下载文件（视频），保留下载记录。"""
        purged: list[str] = []
        for download_id in download_ids:
            try:
                download = self.downloads.get(download_id)
                if download is not None:
                    self._safe_remove_downloaded_source(download.get("downloaded_path"))
                    self._safe_remove_downloaded_source(download.get("cover_path"))
                    purged.append(download_id)
            except Exception:
                continue
        return purged

    def queue_download_for_transcription(self, download_id: str) -> QueueResult:
        with self.media_action_lock:
            return self._queue_download_for_transcription_locked(download_id)

    def _queue_download_for_transcription_locked(self, download_id: str) -> QueueResult:
        """Promote a completed browser download without downloading it again."""
        result = QueueResult()
        download = self.downloads.get(download_id)
        if (
            not download
            or download.get("status") != "done"
            or download.get("job_id")
            or not download.get("media_id")
        ):
            result.skipped.append(download_id)
            return result

        media = self.media.get_media(download["media_id"])
        if not media or media.get("missing"):
            result.skipped.append(download_id)
            return result

        result = self.enqueue_media([download["media_id"]])
        linked_id = result.created[0] if result.created else None
        if not linked_id:
            source_path = media["source_path"]
            for job in reversed(self.queue.list_jobs()):
                if job["source_path"] == source_path and job["status"] in (
                    "queued", "running", "retryable", "done",
                ):
                    linked_id = job["id"]
                    break
        if not linked_id:
            result.skipped.append(download_id)
            return result

        title = download.get("title", "").strip()
        if title:
            self.queue.set_source_title(linked_id, title)
        self.downloads.transition(download_id, "queued", job_id=linked_id)
        return result

    def enqueue_media(self, media_ids: list[str]) -> QueueResult:
        with self.media_action_lock:
            return self._enqueue_media_locked(media_ids)

    def _enqueue_media_locked(self, media_ids: list[str]) -> QueueResult:
        result = QueueResult()
        active_sources = {
            job["source_path"]
            for job in self.queue.list_jobs()
            if job["status"] in ("queued", "running", "retryable")
        }
        for media_id in media_ids:
            item = self.media.get_media(media_id)
            if item["missing"] or item["source_path"] in active_sources:
                result.skipped.append(media_id)
                continue
            now = datetime.now().isoformat(timespec="seconds")
            job_id = uuid.uuid4().hex[:12]
            self.queue.enqueue({
                "id": job_id,
                "source_path": item["source_path"],
                "output_dir": self.settings["output_dir"],
                "status": "queued",
                "whisper_model": self.settings["whisper_model"],
                "device": self.settings["device"],
                "language": self.settings["language"],
                "content_mode": "raw",
                "source_title": Path(item["filename"]).stem,
                "created_at": now,
                "updated_at": now,
                "max_attempts": self.settings["max_attempts"],
            })
            active_sources.add(item["source_path"])
            result.created.append(job_id)
        return result

    def cancel_jobs(self, job_ids: list[str]) -> None:
        for job_id in job_ids:
            self.queue.request_cancel(job_id)

    def cancel_download_tasks(self, download_ids: list[str]) -> list[str]:
        """Request cancellation for browser work that has not reached ASR."""
        cancelled: list[str] = []
        for download_id in download_ids:
            try:
                self.downloads.request_cancel(download_id)
                cancelled.append(download_id)
            except Exception:
                continue
        return cancelled

    def retry_download_tasks(self, download_ids: list[str]) -> list[str]:
        """Return recoverable browser downloads to the receiving state."""
        retried: list[str] = []
        for download_id in download_ids:
            try:
                self.downloads.retry(download_id)
                retried.append(download_id)
            except Exception:
                continue
        return retried

    def requeue_jobs(self, job_ids: list[str]) -> list[str]:
        with self.media_action_lock:
            return self._requeue_jobs_locked(job_ids)

    def _requeue_jobs_locked(self, job_ids: list[str]) -> list[str]:
        requeued = []
        for job_id in job_ids:
            try:
                self.queue.requeue(job_id)
                requeued.append(job_id)
            except QueueStateError:
                continue
        return requeued

    RETRANSCRIBE_MODES = ("default", "raw", "article")

    def retranscribe_jobs(self, job_ids: list[str], *, mode: str = "default",
                          editing_level: str = "") -> QueueResult:
        if mode not in self.RETRANSCRIBE_MODES:
            raise ValueError(f"未知的重新转写模式: {mode}")
        if editing_level and editing_level not in ("auto", "light", "balanced", "structured", "rewrite"):
            raise ValueError(f"未知的整理力度: {editing_level}")
        with self.media_action_lock:
            return self._retranscribe_jobs_locked(job_ids, mode=mode, editing_level=editing_level)

    def _retranscribe_jobs_locked(self, job_ids: list[str], *, mode: str = "default",
                                   editing_level: str = "") -> QueueResult:
        """Create fresh queued jobs for terminal work using current settings.

        mode（TASK-8.24，先生 2026-10-25）：default=沿用全局 Agent 设置；
        raw=仅原稿（本次不跑 AI 整理）；article=原稿＋AI 整理稿。后两者经
        body_enabled_override 按单任务覆盖，不再受全局开关牵制。
        """
        result = QueueResult()
        active_sources = {
            job["source_path"]
            for job in self.queue.list_jobs()
            if job["status"] in ("queued", "running", "retryable")
        }
        for previous_id in job_ids:
            previous = self.queue.get_job(previous_id)
            if previous is None or previous["status"] not in (
                "done", "failed", "retryable", "cancelled",
            ):
                result.skipped.append(previous_id)
                continue
            source = previous["source_path"]
            if source in active_sources or not Path(source).is_file():
                result.skipped.append(previous_id)
                continue
            now = datetime.now().isoformat(timespec="seconds")
            new_id = uuid.uuid4().hex[:12]
            self.queue.enqueue({
                "id": new_id,
                "source_path": source,
                "output_dir": self.settings["output_dir"],
                "status": "queued",
                "whisper_model": self.settings["whisper_model"],
                "device": self.settings["device"],
                "language": self.settings["language"],
                "content_mode": "article" if mode == "article" else "raw",
                "source_title": previous.get("source_title", "") or Path(source).stem,
                "created_at": now,
                "updated_at": now,
                "max_attempts": self.settings["max_attempts"],
                **({"body_enabled_override": mode != "raw"} if mode != "default" else {}),
                **({"agent_level_override": editing_level} if editing_level else {}),
            })
            active_sources.add(source)
            result.created.append(new_id)
        return result

    def save_settings(self, changes: dict) -> dict:
        with self._settings_lock:
            # R11：字段白名单——未知键不落盘，避免配置被污染（旧配置读取不受影响）
            allowed = set(self.settings_store.defaults().keys())
            changes = {k: v for k, v in (changes or {}).items() if k in allowed}
            self.settings = self.settings_store.update(**changes)
            return self.settings

    def close(self) -> None:
        if not self.video_catalog.close(timeout=5):
            _LOG.warning("Video index is still stopping; leave its owned stores open until process shutdown")
            return
        self.downloads.close()
        self.queue.close()
        self.media.close()
