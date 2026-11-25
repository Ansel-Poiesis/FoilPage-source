# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""数据自检与恢复（底二层）：悬空任务超时收敛、孤儿产物检测与清理。"""
from __future__ import annotations

import datetime as _dt
import json
import re
import shutil
import stat
import time as _time
import uuid
from collections import defaultdict
from pathlib import Path


STALE_BROWSER_STATUSES = frozenset({"queued", "received", "resolving", "awaiting_browser"})
DEFAULT_STALE_HOURS = 24.0
_ACTIVE_JOBS = frozenset({"queued", "running", "retryable"})
_JOB_NAME = re.compile(r"[0-9a-f]{12}")
_TRANSCRIBE_STAGE = re.compile(r"\.([0-9a-f]{12})-([a-z0-9_]{8}|old-[0-9a-f]{32})")
_DELETION_CACHE = re.compile(r"\.deletion-cache-([0-9a-f]{12})-[0-9a-f]{32}")
_LEGACY_DELETE_STAGE = re.compile(r"\.text-delete-[a-z0-9_]{8}")
_BROWSER_DOWNLOAD = re.compile(r"(?:browser-media-\d{13}|[0-9a-f]{12}-.+)\.[a-z0-9]+", re.I)
_MIN_STAGE_AGE = 3600


def _parse_ts(value: object) -> float:
    """解析 'YYYY-MM-DD HH:MM:SS' 时间戳，失败返回 0（视为未知，不误伤）。"""
    if not isinstance(value, str):
        return 0.0
    try:
        return _dt.datetime.fromisoformat(value).timestamp()
    except ValueError:
        try:
            return _dt.datetime.strptime(value.replace("T", " "), "%Y-%m-%d %H:%M:%S").timestamp()
        except ValueError:
            return 0.0


def stale_browser_downloads(controller, *, stale_hours: float = DEFAULT_STALE_HOURS) -> list[dict]:
    """列出超过阈值仍未收到浏览器结果的下载任务。"""
    now = _time.time()
    stale: list[dict] = []
    for item in controller.downloads.list(limit=1000):
        if item.get("status") not in STALE_BROWSER_STATUSES:
            continue
        updated = _parse_ts(item.get("updated_at"))
        if updated and (now - updated) > stale_hours * 3600:
            stale.append(dict(item))
    return stale


def fix_stale_browser_downloads(
    controller, *, stale_hours: float = DEFAULT_STALE_HOURS,
) -> list[dict]:
    """把超时悬空任务收敛为可重试失败，返回已修复列表。"""
    fixed: list[dict] = []
    for item in stale_browser_downloads(controller, stale_hours=stale_hours):
        try:
            was_queued = item["status"] == "queued"
            if item["status"] == "received":
                # received 不能直接置 failed，先经 resolving 再收敛。
                controller.downloads.transition(item["id"], "resolving", progress=0.0)
            controller.downloads.transition(
                item["id"],
                "failed",
                error=(
                    {
                        "code": "queue_timeout",
                        "retryable": True,
                        "message": "任务提交后长时间未被处理（应用未运行或通道未启动），已收敛为可重试",
                    }
                    if was_queued
                    else {
                        "code": "browser_timeout",
                        "retryable": True,
                        "message": "等待浏览器提交超时，可重试",
                    }
                ),
            )
            fixed.append({"id": item["id"], "title": item.get("title", ""), "status": "failed"})
        except Exception:
            continue
    return fixed


def _resolved(value) -> Path | None:
    if not value:
        return None
    try:
        return Path(value).expanduser().resolve()
    except (OSError, ValueError, RuntimeError):
        return None


def _paths(values) -> set[Path]:
    return {path for value in values if (path := _resolved(value)) is not None}


def _plain_stat(path: Path):
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
        raise ValueError("链接路径")
    return info


def _candidate(entry: Path, kind: str) -> dict:
    """Reject links and count bytes without traversing any reparse point."""
    def size(path: Path) -> int:
        info = _plain_stat(path)
        if stat.S_ISDIR(info.st_mode):
            return sum(size(child) for child in path.iterdir())
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("非常规文件")
        return info.st_size

    return {"name": entry.name, "path": str(entry), "kind": kind, "bytes": size(entry)}


def _references(controller, jobs, downloads) -> dict[Path, set[str | None]]:
    media_paths = [media["source_path"] for media in controller.media.list_media()]
    media_paths.extend(job.get(key) for job in jobs for key in ("source_path", "output_dir"))
    media_paths.extend(
        download.get(key) for download in downloads for key in ("downloaded_path", "source_path", "cover_path")
    )
    output_paths = controller.queue.manifest_file_paths()
    output_paths.extend((job["id"], job[key]) for job in jobs
                        for key in ("raw_path", "final_path") if job.get(key))
    owned_paths = [(None, path) for path in media_paths]
    owned_paths.extend(output_paths)
    references = defaultdict(set)
    for owner, value in owned_paths:
        if path := _resolved(value):
            for ancestor in (path, *path.parents):
                references[ancestor].add(owner)
    return references


def _known_output_contents(entry: Path, job_id: str, cleared: bool, *, process_only: bool = False) -> bool:
    """Recognize generated artifacts without treating arbitrary user files as cache."""
    files = list(entry.iterdir())
    stems = set()
    allowed = set()
    process_files = set()
    for path in files:
        if not path.name.endswith("_manifest.json") or not stat.S_ISREG(_plain_stat(path).st_mode):
            continue
        try:
            with path.open(encoding="utf-8") as stream:
                info = json.loads(stream.read(256 * 1024))
        except (ValueError, OSError):
            continue
        if isinstance(info, dict) and info.get("job_id") == job_id:
            stem = path.name.removesuffix("_manifest.json")
            stems.add(stem)
            process_files.update(stem + suffix for suffix in ("_manifest.json", "_segments.json", ".srt", ".vtt"))
            allowed.update(stem + suffix for suffix in ("_raw.md", "_raw.txt", ".md", ".txt"))
            for key in ("raw_path", "final_path", "segments_path", "translation_path", "translation_segments_path"):
                value = info.get(key)
                if isinstance(value, str) and Path(value).suffix.lower() in {".md", ".txt", ".json"}:
                    allowed.add(Path(value).name)
    allowed.update(process_files)
    for path in files:
        mode = _plain_stat(path).st_mode
        if stat.S_ISDIR(mode):
            if not cleared or not (_LEGACY_DELETE_STAGE.fullmatch(path.name) or _DELETION_CACHE.fullmatch(path.name)):
                return False
        elif process_only and path.name not in process_files:
            return False
        elif path.name not in allowed and not (stems and re.fullmatch(
            r".+\.foilpage-(?:[a-f0-9]{24}|[a-f0-9]{32})\.md", path.name
        )) and not any(
            re.fullmatch(re.escape(stem) + r"_译文_[a-z]{2}(?:\.[a-f0-9]{24}|\.[a-f0-9]{32})?(?:\.md|\.segments\.json)", path.name) for stem in stems
        ):
            return False
    return True


def _registered_text_missing(job: dict, manifest: dict, directory: Path) -> bool:
    paths = {
        Path(record[key]).expanduser() for record in (job, manifest)
        for key in ("raw_path", "final_path", "translation_path", "translation_segments_path") if record.get(key)
    }
    for path in paths:
        if not path.is_absolute() or path.parent.resolve() != directory:
            return False
        try:
            path.lstat()
        except FileNotFoundError:
            continue
        return False
    return bool(paths)


def _children(root: Path, only: Path | None):
    if only is not None:
        if only.is_relative_to(root) and only != root:
            yield root / only.relative_to(root).parts[0]
    elif root.is_dir():
        yield from sorted(root.iterdir(), key=lambda p: p.name.casefold())


def _output_candidates(controller, jobs, downloaded, *, only=None) -> list[dict]:
    roots = _paths((controller.settings["output_dir"], controller.settings_store.base_dir / "output"))
    tracked = {}
    for job in jobs:
        for root in _paths((job.get("output_dir"),)):
            roots.add(root)
            tracked[root / job["id"]] = job
    referenced = _references(controller, jobs, downloaded)
    candidates = {}
    for root in sorted(roots):
        for entry in _children(root, only):
            try:
                entry_info = _plain_stat(entry)
                if not stat.S_ISDIR(entry_info.st_mode):
                    continue
                cache = _DELETION_CACHE.fullmatch(entry.name)
                stage = _TRANSCRIBE_STAGE.fullmatch(entry.name)
                job = tracked.get(root / (cache or stage)[1]) if cache or stage else tracked.get(entry)
                if job and job["status"] in _ACTIVE_JOBS:
                    continue
                kind = None
                owners = referenced.get(entry, set())
                if cache:
                    kind = "deletion_cache"
                elif stage and _time.time() - entry_info.st_mtime >= _MIN_STAGE_AGE:
                    # A backup can be the last copy when replacing the output was interrupted.
                    if not stage[2].startswith("old-") or (not job and _known_output_contents(entry, stage[1], False)):
                        kind = "output_staging"
                elif _JOB_NAME.fullmatch(entry.name):
                    manifest = (controller.queue.get_manifest(job["id"]) or {}) if job else {}
                    cleared = bool(manifest.get("text_outputs_cleared")
                                   and not (job.get("raw_path") or job.get("final_path")))
                    legacy = bool(job and not cleared and _registered_text_missing(job, manifest, entry))
                    if legacy:
                        owners = owners - {job["id"]}
                    if (not job or cleared or legacy) and _known_output_contents(
                        entry, entry.name, cleared, process_only=legacy,
                    ):
                        kind = "cleared_output" if job else "output"
                if kind and not owners and (only is None or only == entry):
                    candidates[entry] = _candidate(entry, kind)
                    continue
                if _JOB_NAME.fullmatch(entry.name):
                    for child in _children(entry, only):
                        cache = _DELETION_CACHE.fullmatch(child.name)
                        if cache and cache[1] == entry.name and child not in referenced and (only is None or only == child):
                            candidates[child] = _candidate(child, "deletion_cache")
            except (OSError, ValueError):
                continue
    return list(candidates.values())


def _download_candidates(controller, jobs, downloads, *, only=None) -> list[dict]:
    from engine.downloader import MEDIA_EXTENSIONS

    roots = _paths((controller.settings["download_dir"], controller.settings_store.base_dir / "downloads",
                    *(item["options"].get("managed_download_root") for item in downloads)))
    referenced = _references(controller, jobs, downloads)
    download_ids = {item["id"] for item in downloads}
    active_jobs = {job["id"] for job in jobs if job["status"] in _ACTIVE_JOBS}
    candidates = []
    for root in sorted(roots):
        for entry in _children(root, only):
            if entry in referenced or (only is not None and only != entry):
                continue
            try:
                info = _plain_stat(entry)
                cache = _DELETION_CACHE.fullmatch(entry.name)
                if cache and cache[1] not in active_jobs and stat.S_ISDIR(info.st_mode):
                    candidates.append(_candidate(entry, "deletion_cache"))
                elif (
                    stat.S_ISREG(info.st_mode) and _BROWSER_DOWNLOAD.fullmatch(entry.name)
                    and entry.suffix.lower() in MEDIA_EXTENSIONS | {".jpg"}
                    and entry.name.split("-", 1)[0] not in download_ids
                    and _time.time() - info.st_mtime >= _MIN_STAGE_AGE
                ):
                    candidates.append(_candidate(entry, "download"))
            except (OSError, ValueError):
                continue
    return candidates


def orphan_output_dirs(controller) -> list[dict]:
    """Read-only scan for unreferenced app-generated output and deletion residue."""
    with controller.media_action_lock:
        return _output_candidates(controller, controller.queue.list_jobs(), controller.downloads.list(limit=None))


def orphan_download_files(controller) -> list[dict]:
    """Only recognize download names FoilPage owns and copies a download record no longer references."""
    with controller.media_action_lock:
        return _download_candidates(controller, controller.queue.list_jobs(), controller.downloads.list(limit=None))


def purge_orphan(controller, path_value: object, *, output: bool) -> bool:
    if not isinstance(path_value, str) or not path_value:
        return False
    path = Path(path_value)
    scan = _output_candidates if output else _download_candidates
    with controller.media_action_lock:
        try:
            candidates = scan(controller, controller.queue.list_jobs(), controller.downloads.list(limit=None), only=path)
            if not candidates:
                return False
            if stat.S_ISDIR(_plain_stat(path).st_mode):
                if candidates[0]["kind"] != "deletion_cache":
                    # A partial rmtree can remove the sidecar that identifies the rest.
                    # Move the confirmed directory to the existing retryable cache namespace.
                    stage = _TRANSCRIBE_STAGE.fullmatch(path.name)
                    job_id = stage[1] if stage else path.name
                    path = path.rename(path.with_name(f".deletion-cache-{job_id}-{uuid.uuid4().hex}"))
                shutil.rmtree(path)
            else:
                path.unlink()
        except (OSError, ValueError):
            return False
    return True


def reconcile(controller, *, stale_hours: float = DEFAULT_STALE_HOURS) -> dict:
    """一次数据自检：先收敛悬空任务，再输出孤儿产物清单。"""
    fixed = fix_stale_browser_downloads(controller, stale_hours=stale_hours)
    return {
        "ok": True,
        "stale_fixed": fixed,
        "orphan_outputs": orphan_output_dirs(controller),
        "orphan_downloads": orphan_download_files(controller),
    }
