# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""数据自检与恢复（底二层）：悬空任务超时收敛、孤儿产物检测与清理。"""
from __future__ import annotations

import datetime as _dt
import time as _time
from pathlib import Path


STALE_BROWSER_STATUSES = frozenset({"queued", "received", "resolving", "awaiting_browser"})
DEFAULT_STALE_HOURS = 24.0


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


def orphan_output_dirs(controller) -> list[dict]:
    """输出根内没有对应任务记录的产物目录（只读检测，不删除）。"""
    out_root = Path(str(controller.settings.get("output_dir", ""))).expanduser().resolve()
    if not out_root.is_dir():
        return []
    known = {job["id"] for job in controller.queue.list_jobs()}
    orphans: list[dict] = []
    for entry in sorted(out_root.iterdir(), key=lambda p: p.name.casefold()):
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        if entry.name not in known:
            orphans.append({"name": entry.name, "path": str(entry)})
    return orphans


def orphan_download_files(controller) -> list[dict]:
    """下载根内没有任何记录引用的源文件（只读检测；可能有用户手动放入的文件，需确认后删）。"""
    dl_root = Path(str(controller.settings.get("download_dir", ""))).expanduser().resolve()
    if not dl_root.is_dir():
        return []
    known: set[str] = set()
    for item in controller.downloads.list(limit=1000):
        for key in ("downloaded_path", "source_path"):
            value = item.get(key)
            if value:
                try:
                    known.add(str(Path(str(value)).expanduser().resolve()))
                except OSError:
                    pass
    for media in controller.media.list_media():
        value = media.get("source_path")
        if value:
            try:
                known.add(str(Path(str(value)).expanduser().resolve()))
            except OSError:
                pass
    orphans: list[dict] = []
    for entry in sorted(dl_root.iterdir(), key=lambda p: p.name.casefold()):
        if not entry.is_file() or entry.name.startswith("."):
            continue
        try:
            if str(entry.resolve()) not in known:
                orphans.append({"name": entry.name, "path": str(entry)})
        except OSError:
            continue
    return orphans


def reconcile(controller, *, stale_hours: float = DEFAULT_STALE_HOURS) -> dict:
    """一次数据自检：先收敛悬空任务，再输出孤儿产物清单。"""
    fixed = fix_stale_browser_downloads(controller, stale_hours=stale_hours)
    return {
        "ok": True,
        "stale_fixed": fixed,
        "orphan_outputs": orphan_output_dirs(controller),
        "orphan_downloads": orphan_download_files(controller),
    }
