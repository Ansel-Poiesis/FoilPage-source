# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""Native resizing and work-area maximization for the custom WebView caption."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
import logging


log = logging.getLogger("shengjian.desktop")


class NativeDragController:
    """Enable WebView2 caption hit testing before the first page navigation.

    The event must be subscribed on the GUI thread in ``before_show``. Keep
    this owner alive with the window so pythonnet retains the event delegate.
    Older SDKs/runtimes leave ``enabled`` false for the existing JS fallback.
    """

    def __init__(self, control) -> None:
        self.enabled = False
        self._control = control
        self._subscribed = False
        core = control.CoreWebView2
        if core is not None:
            self._enable(core)
        else:
            control.CoreWebView2InitializationCompleted += self._initialized
            self._subscribed = True

    def _enable(self, core) -> None:
        try:
            core.Settings.IsNonClientRegionSupportEnabled = True
            self.enabled = bool(core.Settings.IsNonClientRegionSupportEnabled)
        except Exception:
            # Unsupported native caption support falls back once, without
            # interfering with WebView initialization or window controls.
            log.warning("WebView2 native dragging unavailable; using fallback", exc_info=True)

    def _initialized(self, sender, event) -> None:
        if getattr(event, "IsSuccess", False):
            self._enable(sender.CoreWebView2)


def dpi_pixels(value: int, dpi: int = 96) -> int:
    """Map logical frame dimensions to the window's current physical DPI."""
    return max(1, round(value * max(96, dpi) / 96))


class POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


class MINMAXINFO(ctypes.Structure):
    _fields_ = [("reserved", POINT), ("max_size", POINT), ("max_position", POINT),
                ("min_track", POINT), ("max_track", POINT)]


class MONITORINFO(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("monitor", wintypes.RECT), ("work", wintypes.RECT), ("flags", wintypes.DWORD)]


def resize_hit(x: int, y: int, width: int, height: int, border: int = 8) -> int:
    left, right, top, bottom = x < border, x >= width - border, y < border, y >= height - border
    if top:
        return 13 if left else 14 if right else 12
    if bottom:
        return 16 if left else 17 if right else 15
    return 10 if left else 11 if right else 1


def install_resize_frame(hwnd: int):
    """Return a live callback owner; retain it until the native window dies."""
    if os.name != "nt" or not hwnd:
        return None
    # Own function objects so these strict signatures do not change pywebview's
    # cached windll calls (its SWP_NOSIZE move passes None for unused dimensions).
    user = ctypes.WinDLL("user32", use_last_error=True)
    comctl = ctypes.WinDLL("comctl32", use_last_error=True)
    pointer = ctypes.c_ssize_t
    callback_type = ctypes.WINFUNCTYPE(pointer, wintypes.HWND, wintypes.UINT, wintypes.WPARAM,
                                      wintypes.LPARAM, ctypes.c_size_t, ctypes.c_size_t)
    comctl.DefSubclassProc.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    comctl.DefSubclassProc.restype = pointer
    comctl.SetWindowSubclass.argtypes = [wintypes.HWND, callback_type, ctypes.c_size_t, ctypes.c_size_t]
    comctl.RemoveWindowSubclass.argtypes = [wintypes.HWND, callback_type, ctypes.c_size_t]
    user.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
    user.GetWindowLongPtrW.restype = pointer
    user.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, pointer]
    user.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT]
    user.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user.IsZoomed.argtypes = [wintypes.HWND]
    user.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
    user.MonitorFromWindow.restype = wintypes.HANDLE
    user.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.POINTER(MONITORINFO)]
    get_dpi = getattr(user, "GetDpiForWindow", None)
    if get_dpi:
        get_dpi.argtypes = [wintypes.HWND]
        get_dpi.restype = wintypes.UINT

    @callback_type
    def procedure(handle, message, wparam, lparam, _id, _data):
        try:
            if message == 0x0083 and wparam:  # WM_NCCALCSIZE: hide OS caption/frame.
                return 0
            if message == 0x0084 and not user.IsZoomed(handle):  # WM_NCHITTEST
                rect = wintypes.RECT()
                user.GetWindowRect(handle, ctypes.byref(rect))
                x, y = ctypes.c_short(lparam & 0xffff).value, ctypes.c_short((lparam >> 16) & 0xffff).value
                dpi = int(get_dpi(handle) or 96) if get_dpi else 96
                hit = resize_hit(x - rect.left, y - rect.top, rect.right - rect.left, rect.bottom - rect.top, dpi_pixels(8, dpi))
                if hit != 1:
                    return hit
            if message == 0x0024:  # WM_GETMINMAXINFO respects taskbar and min size.
                info = ctypes.cast(lparam, ctypes.POINTER(MINMAXINFO)).contents
                monitor = MONITORINFO(size=ctypes.sizeof(MONITORINFO))
                if user.GetMonitorInfoW(user.MonitorFromWindow(handle, 2), ctypes.byref(monitor)):
                    info.max_position = POINT(monitor.work.left - monitor.monitor.left, monitor.work.top - monitor.monitor.top)
                    info.max_size = POINT(monitor.work.right - monitor.work.left, monitor.work.bottom - monitor.work.top)
                    dpi = int(get_dpi(handle) or 96) if get_dpi else 96
                    info.min_track = POINT(dpi_pixels(560, dpi), dpi_pixels(500, dpi))
                    return 0
            if message == 0x0082:  # WM_NCDESTROY
                comctl.RemoveWindowSubclass(handle, procedure, 1)
        except Exception:
            pass
        return comctl.DefSubclassProc(handle, message, wparam, lparam)

    # Keep resize/maximize styles for Win+arrows, Alt+space and snap. Only
    # caption rendering is suppressed; the UI supplies its own drag surface.
    style = user.GetWindowLongPtrW(hwnd, -16)
    user.SetWindowLongPtrW(hwnd, -16, (style | 0x00040000 | 0x00080000 | 0x00030000) & ~0x00C00000)
    if not comctl.SetWindowSubclass(hwnd, procedure, 1, 0):
        return None
    user.SetWindowPos(hwnd, None, 0, 0, 0, 0, 0x0027)
    return procedure
