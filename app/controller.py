# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""UI-facing application service combining settings, media and queue stores."""
from __future__ import annotations

import threading
import logging
import stat
import uuid
import re
import sqlite3
import tempfile
import time
from dataclasses import dataclass, field
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
import shutil

from engine.media_store import ImportSummary, MediaStore, MediaStoreError, SCHEMA_VERSION as MEDIA_SCHEMA_VERSION
from engine.download_store import DownloadStore, DownloadStoreError, SCHEMA_VERSION as DOWNLOAD_SCHEMA_VERSION
from engine.queue_store import (
    QueueStateError, QueueStore, QueueStoreError, _source_paths_equivalent,
    SCHEMA_VERSION as QUEUE_SCHEMA_VERSION,
)
from engine.settings import SettingsStore, SettingsValidationError
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
        from app.backup_restore import recover_interrupted_import
        recover_interrupted_import(self.settings_store)
        self.settings = self.settings_store.load()
        self._check_database_versions(self.settings["database_path"])
        from engine.library_location import (
            bind_library_identity, mark_library_initialized, preflight_library,
        )
        preflight_library(self.settings_store, self.settings)
        if not self.settings.get("local_api_token"):
            self.settings["local_api_token"] = self.settings_store.ensure_local_api_token()
        self.settings_store.ensure_runtime_directories()
        self.media = MediaStore(self.settings["database_path"])
        self.queue = QueueStore(self.settings["database_path"])
        self.downloads = DownloadStore(self.settings["database_path"])
        db_setting = str(self.settings.get("database_path") or "")
        self._shared_db_connections = bool(
            db_setting and db_setting != ":memory:" and not db_setting.startswith("file::memory:")
        )
        try:
            library_id = bind_library_identity(
                self.settings["database_path"], self.settings.get("library_id", ""),
            )
            if self.settings.get("library_id") != library_id:
                self.settings["library_id"] = library_id
                self.settings = self.settings_store.save(self.settings)
            mark_library_initialized(self.settings_store)
        except Exception:
            self.downloads.close()
            self.queue.close()
            self.media.close()
            raise
        from engine.agent_activity import AgentActivity
        self.agent_activity = AgentActivity(self.settings_store.base_dir / "agent-activity.json")
        from engine.product_events import ProductEvents
        self.product_events = ProductEvents(self.settings_store.base_dir / "product-notifications.json")
        from engine.video_catalog import VideoCatalog
        from engine.retention import FailedTaskRetention
        self.video_catalog = VideoCatalog(self.media)
        self.failed_task_retention = FailedTaskRetention(self.settings["database_path"])
        self._backfill_download_titles()
        self._recover_browser_downloads()

    def _require_writable(self) -> None:
        if getattr(self, "_backup_restore_blocked", False):
            raise QueueStateError("备份恢复尚未完成，暂时不能修改资料；请重启应用完成恢复后再操作")

    @contextmanager
    def _writable_action(self):
        with self.media_action_lock:
            self._require_writable()
            yield

    @staticmethod
    def _check_database_versions(db_path: str) -> None:
        """Refuse any known future component before opening stores that can migrate it."""
        path = Path(db_path).expanduser().resolve()
        if not path.is_file():
            return
        connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
        try:
            queue_version = connection.execute("PRAGMA user_version").fetchone()[0]
            if queue_version > QUEUE_SCHEMA_VERSION:
                raise QueueStoreError(
                    f"资料数据库版本 {queue_version} 高于当前支持版本 {QUEUE_SCHEMA_VERSION}，请更新 FoilPage 后再打开"
                )
            registered = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='app_schema'"
            ).fetchone()
            if not registered:
                return
            versions = dict(connection.execute("SELECT component, version FROM app_schema"))
            for component, supported, error_type in (
                ("media", MEDIA_SCHEMA_VERSION, MediaStoreError),
                ("downloads", DOWNLOAD_SCHEMA_VERSION, DownloadStoreError),
            ):
                version = versions.get(component, 0)
                if version > supported:
                    raise error_type(
                        f"{component} 资料库版本 {version} 高于当前支持版本 {supported}，请更新 FoilPage 后再打开"
                    )
        finally:
            connection.close()

    def _backfill_download_titles(self) -> None:
        for item in self.downloads.list(limit=None):
            job_id = item.get("job_id")
            title = item.get("title", "")
            if not job_id or not title:
                continue
            job = self.queue.get_job(job_id)
            if job and not job.get("source_title"):
                self.queue.set_source_title(job_id, title)

    def _recover_browser_downloads(self) -> None:
        """Re-link completed browser downloads whose media or ASR rows were lost."""
        for item in self.downloads.recovery_candidates():
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
        with self._writable_action():
            labels = display_names or []
            return [
                self.media.import_file(path, display_name=labels[index] if index < len(labels) else "")
                for index, path in enumerate(paths)
            ]

    def import_folder(self, path: str, *, recursive: bool = True) -> ImportSummary:
        with self._writable_action():
            return self.media.import_directory(path, recursive=recursive)

    def import_paths(self, paths, *, progress=None, cancel_event=None) -> ImportSummary:
        with self._writable_action():
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
        with self._writable_action():
            return self.media.clear_media()

    def db_change_token(self) -> tuple[int, int, int] | None:
        """Lightweight SQLite change stamp across the three distinct DB connections.

        In SQLite WAL mode, PRAGMA data_version increments on every *other*
        connection after any committed write (local or external Worker/Agent).
        Reading all three distinct connections therefore detects every commit
        without scanning any tables. Returns None for in-memory or mocked stores.
        """
        if not getattr(self, "_shared_db_connections", False):
            return None
        stores = (self.queue, self.downloads, self.media)
        conns = [getattr(store, "_conn", None) for store in stores]
        if any(not isinstance(conn, sqlite3.Connection) for conn in conns):
            return None
        if len({id(conn) for conn in conns}) < 2:
            return None
        versions: list[int] = []
        for store, conn in zip(stores, conns):
            lock = getattr(store, "_lock", None)
            if lock is not None:
                with lock:
                    versions.append(int(conn.execute("PRAGMA data_version").fetchone()[0]))
            else:
                versions.append(int(conn.execute("PRAGMA data_version").fetchone()[0]))
        return (versions[0], versions[1], versions[2])

    @staticmethod
    def _assemble_job_rows(
        jobs: list[dict],
        media_items: list[dict],
        download_rows: list[dict],
        *,
        include_archived: bool = False,
    ) -> list[dict]:
        media_names = {
            item["source_path"]: Path(item["filename"]).stem
            for item in media_items
        }
        download_names = {
            item["job_id"]: item["title"]
            for item in download_rows
            if item.get("job_id") and item.get("title")
        }
        rows = []
        for job in jobs:
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

    def job_rows(self, include_archived: bool = False) -> list[dict]:
        """Return jobs with a stable human title, including pre-migration jobs.

        Archived jobs are hidden from the workbench by default; the library
        reads them via ``include_archived=True`` so cleared results survive.
        """
        return self._assemble_job_rows(
            self.queue.list_jobs(),
            self.media.list_media(),
            self.downloads.list(limit=None),
            include_archived=include_archived,
        )

    def task_rows(self, include_archived: bool = False) -> list[dict]:
        """Combine visible browser and transcription work without duplicate rows.

        ``linked_jobs`` is computed over every job (archived included) so a
        download row linked to an archived job is not shown standalone in the
        workbench; the job itself is skipped unless ``include_archived``.
        """
        rows: list[dict] = []
        all_jobs = self.queue.list_jobs()
        linked_jobs: set[str] = {job["id"] for job in all_jobs}
        # TASK-8.17：转写行从关联下载行继承封面（下载行被并入隐藏后，封面
        # 仍要在同一张任务卡上展示）。
        download_rows = self.downloads.list(limit=None)
        media_items = self.media.list_media()
        linked_covers = {
            item["job_id"]: item.get("cover_path")
            for item in download_rows
            if item.get("job_id") and item.get("cover_path")
        }
        for job in self._assemble_job_rows(
            all_jobs, media_items, download_rows, include_archived=include_archived
        ):
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
            error = download.get("error") or {}
            retry_in_progress = (
                download["status"] in {"received", "resolving", "downloading"}
                or (download["status"] == "awaiting_browser"
                    and (error.get("diagnostic") or {}).get("fallback_pending"))
            )
            row = dict(download)
            row.update({
                "kind": "download",
                "task_id": download["id"],
                "source": "浏览器下载",
                "display_title": download.get("title") or download["url"],
                "result_category": "浏览器下载",
                "error": None if retry_in_progress or download["status"] == "done" else download.get("error"),
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
        with self._writable_action():
            cleared = 0
            for job in self.queue.list_jobs():
                if job.get("status") == "done" and not job.get("archived"):
                    self.queue.set_archived(job["id"], True)
                    cleared += 1
            return cleared

    def set_result_categories(self, job_ids: list[str], category: str) -> list[str]:
        updated: list[str] = []
        with self._writable_action():
            for job_id in job_ids:
                try:
                    self.queue.set_result_category(job_id, category)
                    updated.append(job_id)
                except QueueStateError:
                    continue
        return updated

    def set_job_tags(self, job_id: str, tags: list[str]) -> list[str]:
        """Set the tag list for a single completed job; returns normalized tags."""
        with self._writable_action():
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
        if (path.suffix.lower() not in {".md", ".txt"}
                or not self.is_available_job_output(job, path, self.settings["output_dir"])):
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
        with self._writable_action():
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
        with self._writable_action():
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
        with self._writable_action():
            from app.data_health import purge_orphan

            return purge_orphan(self, path_value, output=True)

    def purge_orphan_download(self, path_value: object) -> bool:
        with self._writable_action():
            from app.data_health import purge_orphan

            return purge_orphan(self, path_value, output=False)

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
        with self._writable_action():
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
        """删除文本和过程文件，保留任务、数据库 manifest、媒体及下载登记。

        ``force`` remains accepted for existing callers, but never bypasses
        the active-worker guard for text-only deletion.
        """
        deleted: list[str] = []
        with self._writable_action():
            for job_id in dict.fromkeys(job_ids):
                try:
                    if self.queue.clear_text_outputs(job_id, remove_files=self._remove_job_text_outputs):
                        deleted.append(job_id)
                except (QueueStoreError, OSError, ValueError):
                    continue
        return deleted

    @staticmethod
    def _job_output_directory(job: dict) -> Path:
        recorded_root = Path(job.get("output_dir") or "").expanduser()
        job_id = job.get("id") or ""
        if not recorded_root.is_absolute() or not re.fullmatch(r"[a-f0-9]{12}", job_id):
            raise ValueError("文本产物目录不可用")
        job_dir = recorded_root.resolve() / job_id
        if job_dir.is_symlink() or job_dir.resolve() != job_dir or job_dir.exists() and not job_dir.is_dir():
            raise ValueError("文本产物目录不可用")
        return job_dir

    @staticmethod
    def is_available_job_output(job: dict, path: Path, output_root: str | Path) -> bool:
        """Resolve current and recorded output locations consistently for every reader."""
        try:
            if not path.is_file():
                return False
            root = Path(output_root).expanduser() if output_root else None
            in_current_root = (root is not None and root.is_dir()
                               and path.resolve().is_relative_to(root.resolve()))
            return in_current_root or AppController.is_recorded_job_output(job, path)
        except (OSError, ValueError, RuntimeError):
            return False

    @staticmethod
    def is_recorded_job_output(job: dict, path: Path) -> bool:
        """Allow registered artifacts in an exact job folder after the output setting changes."""
        try:
            return not path.is_symlink() and path.resolve().parent == AppController._job_output_directory(job)
        except (OSError, ValueError, RuntimeError):
            return False

    @staticmethod
    def _job_text_paths(job: dict, manifest: dict | None, job_dir: Path) -> set[Path]:
        from app.translation_service import translation_paths

        source = Path(job["source_path"]).expanduser().resolve()

        def owned_file(value: str, extensions: set[str]) -> Path:
            candidate = Path(value).expanduser()
            if candidate.is_symlink():
                raise ValueError("文本产物路径不可用")
            path = candidate.resolve()
            if path.parent != job_dir or path.suffix.lower() not in extensions:
                raise ValueError("文本产物路径不可用")
            if path.exists() and not path.is_file():
                raise ValueError("文本产物路径不可用")
            if path == source:
                raise ValueError("文本产物路径不可用")
            return path

        def segments_file(value: str) -> Path:
            path = owned_file(value, {".json"})
            if not path.name.endswith("_segments.json"):
                raise ValueError("字幕产物路径不可用")
            return path

        assets: set[Path] = set()
        raw_stems: set[str] = set()
        final_stems: set[str] = set()
        for record in (job, manifest or {}):
            for key in ("raw_path", "final_path"):
                if record.get(key):
                    path = owned_file(record[key], {".md", ".txt"})
                    assets.add(path)
                    if key == "raw_path":
                        raw_stems.add(path.stem.removesuffix("_raw"))
                    else:
                        final_stems.add(path.stem)
                    # Agent replaces final_path with a classified article, but
                    # the engine's initial final copy is still a text artifact.
                    if key == "raw_path" and path.stem.endswith("_raw"):
                        base = path.stem.removesuffix("_raw")
                        for suffix in (".md", ".txt"):
                            assets.add(owned_file(str(job_dir / (base + suffix)), {suffix}))
        if manifest and manifest.get("segments_path"):
            assets.add(segments_file(manifest["segments_path"]))
        if manifest:
            for key, extensions in (("translation_path", {".md"}), ("translation_segments_path", {".json"})):
                if manifest.get(key):
                    assets.add(owned_file(manifest[key], extensions))

        for stem in raw_stems or final_stems:
            for suffix in (".srt", ".vtt"):
                assets.add(owned_file(str(job_dir / (stem + suffix)), {suffix}))
            assets.add(segments_file(str(job_dir / (stem + "_segments.json"))))
            # A portable restore can retain stale absolute paths in this sidecar.
            # Its filename is generated, and its contents never authorize deletion.
            assets.add(owned_file(str(job_dir / (stem + "_manifest.json")), {".json"}))
            for language in ("zh", "en"):
                article, segments = translation_paths(job_dir / (stem + "_raw.md"), language)
                assets.add(owned_file(str(article), {".md"}))
                assets.add(owned_file(str(segments), {".json"}))
            if job_dir.is_dir():
                generated_translation = re.compile(
                    re.escape(stem) + r"_译文_(?:zh|en)(?:\.(?:[a-f0-9]{24}|[a-f0-9]{32}))?\.(?:md|segments\.json)"
                )
                for path in job_dir.iterdir():
                    if generated_translation.fullmatch(path.name):
                        assets.add(owned_file(str(path), {".md", ".json"}))
        if assets and job_dir.is_dir():
            stems = {path.stem for path in assets}
            for path in job_dir.iterdir():
                if re.fullmatch(r".+\.foilpage-(?:[a-f0-9]{24}|[a-f0-9]{32})\.md", path.name):
                    assets.add(owned_file(str(path), {".md"}))
                temp = re.fullmatch(r"\.(.+)-[a-z0-9_]{8}\.tmp", path.name)
                if temp and temp[1] in stems:
                    assets.add(owned_file(str(path), {".tmp"}))
        return {path for path in assets if path.is_file()}

    @contextmanager
    def _stage_library_removal(self, job_id: str, job_dir: Path, assets: set[Path]):
        """Restore staged files if the database or an earlier move fails."""
        stages: dict[Path, Path] = {}
        moved: list[tuple[Path, Path]] = []
        try:
            for path in sorted(assets):
                if path.parent not in stages:
                    stages[path.parent] = Path(tempfile.mkdtemp(prefix=".text-delete-", dir=path.parent))
                backup = stages[path.parent] / path.name
                path.replace(backup)
                moved.append((path, backup))
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
            for stage in stages.values():
                stage.rmdir()
            raise
        else:
            for stage in stages.values():
                try:
                    for backup in stage.iterdir():
                        try:
                            backup.unlink(missing_ok=True)
                        except PermissionError:
                            attributes = getattr(backup.stat(), "st_file_attributes", 0)
                            if not (attributes & stat.FILE_ATTRIBUTE_READONLY) or backup.stat().st_nlink != 1:
                                raise
                            backup.chmod(stat.S_IWRITE)
                            backup.unlink(missing_ok=True)
                    stage.rmdir()
                except OSError:
                    # This name distinguishes committed debris from a failed rollback.
                    try:
                        stage.rename(stage.with_name(f".deletion-cache-{job_id}-{uuid.uuid4().hex}"))
                    except OSError:
                        pass
                    _LOG.warning("删除暂存尚未清理（任务 %s）", job_id)
            try:
                job_dir.rmdir()
            except OSError:
                pass  # Other files in the job directory belong to the user.

    def _remove_job_text_outputs(self, job: dict, manifest: dict | None):
        job_dir = self._job_output_directory(job)
        return self._stage_library_removal(job["id"], job_dir, self._job_text_paths(job, manifest, job_dir))

    def delete_library_items(self, job_ids: list[str], *, remove_records: bool) -> list[str]:
        """Remove a library item's files and managed media, optionally its task record."""
        from functools import partial

        removed: list[str] = []
        stage = partial(self._remove_library_item_files, remove_record=remove_records)
        with self._writable_action():
            for job_id in dict.fromkeys(job_ids):
                try:
                    if self.queue.remove_library_outputs(job_id, remove_record=remove_records, stage=stage):
                        removed.append(job_id)
                except (QueueStoreError, OSError, ValueError):
                    continue
        return removed

    @contextmanager
    def _remove_library_item_files(self, db, job: dict, manifest: dict | None, *, remove_record: bool):
        job_dir = self._job_output_directory(job)
        assets = self._job_text_paths(job, manifest, job_dir)
        related, media, owned_files, shared_job = self._library_media_to_remove(db, job)
        with self._stage_library_removal(job["id"], job_dir, assets | owned_files):
            if media is not None:
                db.execute("DELETE FROM media WHERE id=?", (media["id"],))
                for row in db.execute("SELECT id, downloaded_path, cover_path FROM download_jobs WHERE media_id=?",
                                      (media["id"],)).fetchall():
                    if row["downloaded_path"] and _source_paths_equivalent(row["downloaded_path"], job["source_path"]):
                        cover = Path(row["cover_path"]).expanduser().absolute() if row["cover_path"] else None
                        db.execute("UPDATE download_jobs SET media_id=NULL, downloaded_path=NULL, cover_path=? WHERE id=?",
                                   (None if cover in owned_files else row["cover_path"], row["id"]))
                    else:
                        db.execute("UPDATE download_jobs SET media_id=NULL WHERE id=?", (row["id"],))
            if remove_record:
                for row in related:
                    if shared_job and row.get("downloaded_path") and _source_paths_equivalent(
                        row["downloaded_path"], job["source_path"],
                    ):
                        db.execute("UPDATE download_jobs SET job_id=? WHERE id=?", (shared_job, row["id"]))
                        continue
                    db.execute(
                        """INSERT OR IGNORE INTO cancelled_download_requests(client_request_id)
                           SELECT client_request_id FROM download_requests WHERE download_id=?""",
                        (row["id"],),
                    )
                    db.execute("DELETE FROM download_jobs WHERE id=?", (row["id"],))
            yield

    def is_managed_download(self, path: Path, media, row: dict) -> bool:
        """Check that a download registration still owns this exact local media path."""
        if row.get("media_id") != media["id"] or not row.get("downloaded_path"):
            return False
        registered = Path(row["downloaded_path"]).expanduser().absolute()
        if registered != path or path.is_symlink() or path.resolve() != path:
            return False
        options = row.get("options") or {}
        roots = [Path(self.settings["download_dir"]), Path(self.settings_store.base_dir) / "downloads"]
        if options.get("managed_download_root"):
            roots.append(Path(options["managed_download_root"]))
        if options.get("mode") == "browser" and path.parent.name == "FoilPage":
            roots.append(path.parent)
        expected = options.get("download_content_sha256") or media.get("original_content_sha256")
        return bool(expected and expected == media["content_sha256"]
                    and any(root.expanduser().resolve() in path.parents for root in roots))

    def _library_media_to_remove(self, db, job: dict):
        from engine.media_store import _sha256

        source = Path(job["source_path"]).expanduser().absolute()
        rows = [DownloadStore._from_row(row) for row in db.execute("SELECT * FROM download_jobs")]
        related = [row for row in rows if row.get("job_id") == job["id"]]
        registrations = [
            row for row in rows if row.get("downloaded_path") and
            _source_paths_equivalent(row["downloaded_path"], str(source))
        ]
        terminal = {"done", "failed", "cancelled"}
        if any(row["status"] not in terminal for row in related):
            raise QueueStateError("视频仍在处理，请稍后再删除")
        shared_job = next((
            row["id"] for row in db.execute("SELECT id, source_path FROM jobs WHERE id<>? ORDER BY created_at DESC, id DESC",
                                           (job["id"],))
            if _source_paths_equivalent(row["source_path"], str(source))
        ), None)
        if shared_job or any(row["status"] not in terminal for row in registrations):
            return related, None, set(), shared_job
        for row in registrations:
            if not row.get("media_id"):
                continue
            media = db.execute("SELECT * FROM media WHERE id=?", (row["media_id"],)).fetchone()
            if (media is None or not _source_paths_equivalent(media["source_path"], str(source))
                    or not self.is_managed_download(source, dict(media), row)):
                continue
            if source.exists() and (not source.is_file() or _sha256(source) != media["content_sha256"]):
                continue
            cover = source.with_suffix(".jpg")
            owned_files = {source} if source.is_file() else set()
            if (row.get("cover_path") and Path(row["cover_path"]).expanduser().absolute() == cover
                    and cover.is_file() and not cover.is_symlink() and cover.resolve() == cover):
                owned_files.add(cover)
            return related, media, owned_files, None
        return related, None, set(), None

    def delete_files(self, job_ids: list[str]) -> list[str]:
        """删除文件：移除文本产物文件夹与下载目录内视频，保留数据库记录。"""
        with self._writable_action():
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
        with self._writable_action():
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
            for download in self.downloads.list(limit=None):
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
        with self._writable_action():
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
        with self._writable_action():
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
        with self._writable_action():
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
        with self._writable_action():
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
        with self._writable_action():
            for job_id in job_ids:
                self.queue.request_cancel(job_id)

    def cancel_download_tasks(self, download_ids: list[str]) -> list[str]:
        """Request cancellation for browser work that has not reached ASR."""
        with self._writable_action():
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
        with self._writable_action():
            retried: list[str] = []
            for download_id in download_ids:
                try:
                    self.downloads.retry(download_id)
                    retried.append(download_id)
                except Exception:
                    continue
            return retried

    def requeue_jobs(self, job_ids: list[str]) -> list[str]:
        with self._writable_action():
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
        with self._writable_action():
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
        with self._writable_action():
            with self._settings_lock:
                # R11：字段白名单——未知键不落盘，避免配置被污染（旧配置读取不受影响）
                allowed = set(self.settings_store.defaults().keys())
                changes = {k: v for k, v in (changes or {}).items() if k in allowed}
                if any(key in changes and changes[key] != self.settings.get(key)
                       for key in ("library_id", "database_path")):
                    raise SettingsValidationError("资料库身份和数据库位置不能通过普通设置更改，请使用资料恢复入口")
                self.settings = self.settings_store.update(**changes)
                return self.settings

    def close(self) -> None:
        if not self.video_catalog.close(timeout=5):
            _LOG.warning("Video index is still stopping; leave its owned stores open until process shutdown")
            return
        self.downloads.close()
        self.queue.close()
        self.media.close()
