# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""Load authored ICO frames directly, with explicit Windows DPI and ownership."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import logging
from pathlib import Path

log = logging.getLogger("shengjian.desktop")


class WindowsIconApi:
    def __init__(self) -> None:
        self.user = ctypes.WinDLL("user32", use_last_error=True)
        self.user.LoadImageW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT,
                                        ctypes.c_int, ctypes.c_int, wintypes.UINT]
        self.user.LoadImageW.restype = wintypes.HICON
        self.user.DestroyIcon.argtypes = [wintypes.HICON]
        self.user.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        self.user.SendMessageW.restype = ctypes.c_ssize_t
        self.user.GetDpiForWindow.argtypes = [wintypes.HWND]
        self.user.GetDpiForWindow.restype = wintypes.UINT
        self.user.GetDpiForSystem.restype = wintypes.UINT
        self.user.GetSystemMetricsForDpi.argtypes = [ctypes.c_int, wintypes.UINT]
        self.user.GetSystemMetricsForDpi.restype = ctypes.c_int

    def window_dpi(self, hwnd: int) -> int:
        return int(self.user.GetDpiForWindow(hwnd) or 96)

    def tray_dpi(self) -> int:
        # Called again when Explorer/display changes, after the GUI has set its
        # DPI awareness. No global/system DPI settings are changed here.
        return int(self.user.GetDpiForSystem() or 96)

    def size(self, dpi: int, *, small: bool) -> tuple[int, int]:
        indexes = (49, 50) if small else (11, 12)
        fallback = max(1, round((16 if small else 32) * dpi / 96))
        return tuple(int(self.user.GetSystemMetricsForDpi(index, dpi) or fallback) for index in indexes)

    def load(self, path: Path, size: tuple[int, int]) -> int:
        # Do not use LR_SHARED for a file: replacement/teardown owns this HICON.
        handle = self.user.LoadImageW(None, str(path), 1, *size, 0x0010)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        return int(handle)

    def destroy(self, handle: int) -> None:
        if handle:
            self.user.DestroyIcon(handle)

    def set_window(self, hwnd: int, handle: int, *, small: bool) -> None:
        self.user.SendMessageW(hwnd, 0x0080, 0 if small else 1, handle)  # WM_SETICON


class NativeWindowIcons:
    """Retain both HICONs; update on DPI change and release on native teardown."""
    def __init__(self, hwnd: int, path: Path, *, api=None) -> None:
        self.hwnd, self.path = hwnd, Path(path)
        self.api = api or WindowsIconApi()
        self.handles: dict[bool, int] = {}
        self.closed = False
        self._callback = None
        self.refresh()
        if api is None:
            try:
                self._install_dpi_callback()
            except Exception:
                # Keep this live owner and its already-installed HICONs even
                # when the OS refuses a DPI subscription.
                log.warning("Could not subscribe native application icon DPI updates", exc_info=True)

    def refresh(self) -> bool:
        if self.closed:
            return False
        replacements = {}
        try:
            dpi = self.api.window_dpi(self.hwnd)
            for small in (False, True):
                replacements[small] = self.api.load(self.path, self.api.size(dpi, small=small))
        except Exception:
            for handle in replacements.values():
                self.api.destroy(handle)
            log.warning("Could not load native application ICO frames; retaining existing icons", exc_info=True)
            return False
        for small, handle in replacements.items():
            self.api.set_window(self.hwnd, handle, small=small)
        previous, self.handles = self.handles, replacements
        for handle in previous.values():
            self.api.destroy(handle)
        return True

    def _install_dpi_callback(self) -> None:
        self._comctl = ctypes.WinDLL("comctl32", use_last_error=True)
        callback_type = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT,
                                          wintypes.WPARAM, wintypes.LPARAM, ctypes.c_size_t, ctypes.c_size_t)
        self._comctl.DefSubclassProc.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        self._comctl.DefSubclassProc.restype = ctypes.c_ssize_t
        self._comctl.SetWindowSubclass.argtypes = [wintypes.HWND, callback_type, ctypes.c_size_t, ctypes.c_size_t]
        self._comctl.RemoveWindowSubclass.argtypes = [wintypes.HWND, callback_type, ctypes.c_size_t]

        @callback_type
        def procedure(hwnd, message, wparam, lparam, _id, _data):
            result = self._comctl.DefSubclassProc(hwnd, message, wparam, lparam)
            if message == 0x02E0:  # WM_DPICHANGED: default handling updates DPI first.
                self.refresh()
            elif message == 0x0082:  # WM_NCDESTROY: no window uses our handles now.
                self.close()
            return result

        if self._comctl.SetWindowSubclass(self.hwnd, procedure, 2, 0):
            self._callback = procedure
        else:
            log.warning("Could not subscribe native application icon DPI updates")

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        if self._callback is not None:
            self._comctl.RemoveWindowSubclass(self.hwnd, self._callback, 2)
        for handle in self.handles.values():
            self.api.destroy(handle)
        self.handles.clear()
