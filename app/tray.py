# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""Windows notification-area controller, isolated from the Tk event loop."""
from __future__ import annotations

from pathlib import Path
from threading import Event
from typing import Callable
import logging
import os
from app.native_notifications import APP_ID, APP_NAME
from app.windows_icons import WindowsIconApi

log = logging.getLogger("shengjian.desktop")


def windows_file_icon_type(base, icon_path: Path, *, api=None):
    """Adapt pinned pystray 0.19.5; preserve its menu/thread/teardown lifecycle.

    Its Windows loader otherwise re-encodes the largest PIL frame into a new
    ICO and drops authored DPI frames. Unknown backend shapes use that existing
    compatible path, as does a failed direct file load.
    """
    required = ("_assert_icon_handle", "_release_icon", "_show", "_hide",
                "_on_display_change", "_on_taskbarcreated")
    if os.name != "nt" or not all(hasattr(base, name) for name in required):
        return base
    try:
        native = api or WindowsIconApi()
    except Exception:
        log.warning("Native tray ICO loader unavailable; using pystray compatible image loader", exc_info=True)
        return base

    class FileIcon(base):
        def _assert_icon_handle(self):
            if self._icon_handle:
                return
            try:
                size = native.size(native.tray_dpi(), small=True)
                self._icon_handle = native.load(icon_path, size)
            except Exception:
                log.warning("Could not load original tray ICO; using pystray compatible image loader", exc_info=True)
                super()._assert_icon_handle()

        def _on_display_change(self, wparam, lparam):
            if self.visible:
                self._hide()
                self._release_icon()
                self._show()

        def _on_taskbarcreated(self, wparam, lparam):
            if self.visible:
                self._release_icon()
                self._show()

    return FileIcon


class TrayController:
    def __init__(
        self,
        *,
        icon_path: Path,
        show: Callable[[], None],
        quit_app: Callable[[], None],
        version: str | None = None,
    ) -> None:
        self.icon_path = icon_path
        self.show = show
        self.quit_app = quit_app
        self.version = (version or "").strip()
        self._icon = None

    @property
    def available(self) -> bool:
        return bool(self._icon is not None and self._icon.visible)

    def start(self) -> bool:
        """Start the notification-area icon, returning whether it is usable.

        A missing bundled icon used to be swallowed here while the desktop
        window still treated the tray as available.  Closing the window then
        made the application unreachable.  Keep this method as the single
        availability gate so callers can retain a visible exit path.
        """
        if not self.icon_path.is_file():
            return False
        try:
            import pystray
            from PIL import Image

            # Detach the image from its file descriptor before pystray starts
            # its native Windows message-loop thread.
            with Image.open(self.icon_path) as source:
                image = source.convert("RGBA")
            version_suffix = f" {self.version}" if self.version else ""
            menu_items = [
                pystray.MenuItem(
                    f"\u6253\u5f00\u58f0\u7b3a{version_suffix}", lambda *_args: self.show(), default=True
                ),
            ]
            if self.version:
                menu_items.append(pystray.MenuItem(f"\u7248\u672c {self.version}", None, enabled=False))
            menu_items.append(
                pystray.MenuItem("\u9000\u51fa\u58f0\u7b3a", lambda *_args: self.quit_app())
            )
            menu = pystray.Menu(*menu_items)
            icon_type = windows_file_icon_type(pystray.Icon, self.icon_path)
            self._icon = icon_type(APP_ID, image, f"{APP_NAME}{version_suffix}", menu)
            ready = Event()

            def setup(icon) -> None:
                icon.visible = True
                ready.set()

            self._icon.run_detached(setup=setup)
            # pystray starts a non-daemon Windows message-loop thread.  If
            # shutdown runs before that loop is ready, ``stop`` is a no-op and
            # the process leaks in the background.  Do not report a usable
            # tray until its setup callback has completed.
            if not ready.wait(timeout=2):
                self.stop()
                return False
            return bool(self._icon.visible)
        except Exception:
            self.stop()
            self._icon = None
            return False

    def stop(self) -> None:
        if self._icon is not None:
            try:
                self._icon.remove_notification()
                self._icon.visible = False
            except Exception:
                pass
            try:
                self._icon.stop()
            finally:
                self._icon = None

    def notify(self, title: str, message: str) -> bool:
        """Show a notification-area bubble. Returns True when the tray is
        available and the notification was handed off.

        Windows owns the display duration. The call may come from a background
        thread, so retain a local reference while checking icon availability.
        """
        icon = self._icon
        if icon is None or not getattr(icon, "visible", False):
            return False
        try:
            icon.notify(str(message or "")[:200], title=str(title or APP_NAME)[:64])
            # Let Windows own display time. A stale timer from a previous
            # notification must not remove the next task's completion bubble.
            return True
        except Exception:
            return False
