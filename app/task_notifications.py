# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""Task completion and progress delivery independent of WebView visibility."""
from __future__ import annotations

from collections.abc import Callable, Iterable


class TaskNotifications:
    """Notify once per terminal outcome; active progress stays in task controls."""

    APP_TITLE = "FoilPage"

    @staticmethod
    def terminal_label(task: dict) -> tuple[str, bool]:
        if task.get("kind") == "download":
            failed = task.get("status") == "failed"
            return ("下载失败" if failed else "下载成功"), failed
        if task.get("status") == "failed":
            return "转写失败", True
        # 积分用完（正文/标题整理被跳过但转写本身完成）：明确原因与去处，
        # 不再笼统显示"处理失败"（先生 2026-10-14：需要积分耗尽的弹窗提醒）。
        if task.get("body_quota_exceeded") or task.get("title_quota_exceeded"):
            return "转写完成，云端积分已用完，请到订阅页充值后重试整理", True
        body = task.get("body_enabled_snapshot") is True
        title = task.get("title_enabled_snapshot") is True
        if not body and not title:
            return "转写成功", False
        body_ok = not body or task.get("body_status") == "done"
        title_ok = not title or task.get("title_status") == "done"
        if body_ok and title_ok:
            return ("正文与标题转写成功" if body and title else "正文转写成功" if body else "标题转写成功"), False
        if body and not body_ok and title and not title_ok:
            return "转写完成，正文与标题处理失败", True
        if not title_ok:
            return ("正文转写成功，标题处理失败" if body and body_ok else "转写完成，标题处理失败"), True
        return ("标题转写成功，正文处理失败" if title and title_ok else "转写完成，正文处理失败"), True

    def __init__(self, notify: Callable[[str, str], bool], beep: Callable[[str], None] | None = None) -> None:
        self.notify = notify
        # beep("success"|"error")：终端态提示音，成功/失败区分（Windows 注入 winsound）。
        self.beep = beep
        self._states: dict[str, tuple[str, str]] = {}
        self._primed = False

    def update(self, tasks: Iterable[dict], *, enabled: bool = True) -> list[dict]:
        events = []
        for task in tasks:
            key = str(task.get("task_id") or "")
            if not key:
                continue
            status = str(task.get("status") or "")
            stamp = str(task.get("updated_at") or "")
            current = (status, stamp)
            previous = self._states.get(key)
            self._states[key] = current
            title = str(task.get("original_source_title") or task.get("display_title") or "未命名任务")[:200]

            if not self._primed or not enabled or previous == current:
                continue
            if status not in {"done", "failed"}:
                continue
            if previous and previous[0] == status:
                continue
            title_label, failed = self.terminal_label(task)
            message = title
            if failed:
                message = f"{title}，请在任务页查看详情"
            delivered = bool(self.notify(title_label, message))
            if self.beep:
                try:
                    self.beep("error" if failed else "success")
                except Exception:
                    pass
            events.append({
                "title": title_label,
                "message": message,
                "delivered": delivered,
                "kind": "terminal",
            })
        self._primed = True
        return events
