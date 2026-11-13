# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""Authored ICO frame selection, native ownership and pinned pystray adapter."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
from pathlib import Path
from unittest import mock

import pytest

from app.windows_icons import NativeWindowIcons, WindowsIconApi
from app.tray import windows_file_icon_type

ROOT = Path(__file__).resolve().parents[1]
ICO = ROOT / "assets/icons/foilpage.ico"


def fake_api():
    api = mock.Mock()
    api.window_dpi.return_value = 144
    api.tray_dpi.return_value = 144
    api.size.side_effect = lambda dpi, small: (round((16 if small else 32) * dpi / 96),) * 2
    api.load.side_effect = [101, 102, 103, 104]
    return api


def test_native_icons_replace_owned_frames_and_close_once():
    api = fake_api()
    owner = NativeWindowIcons(77, ICO, api=api)
    assert api.load.call_args_list == [mock.call(ICO, (48, 48)), mock.call(ICO, (24, 24))]
    assert api.set_window.call_args_list == [mock.call(77, 101, small=False), mock.call(77, 102, small=True)]
    api.destroy.assert_not_called()
    assert owner.refresh()
    assert api.destroy.call_args_list == [mock.call(101), mock.call(102)]
    owner.close(); owner.close()
    assert api.destroy.call_args_list == [mock.call(101), mock.call(102), mock.call(103), mock.call(104)]
    assert not owner.refresh() and owner.handles == {}


def test_failed_second_frame_retains_both_previous_icons_and_releases_partial_load(caplog):
    api = fake_api()
    owner = NativeWindowIcons(77, ICO, api=api)
    api.load.side_effect = [103, OSError("frame unavailable")]
    assert not owner.refresh()
    assert owner.handles == {False: 101, True: 102}
    assert api.set_window.call_count == 2
    api.destroy.assert_called_once_with(103)
    assert "retaining existing icons" in caplog.text
    owner.close()


class BackendIcon:
    """The private surface used by the pinned pystray Windows backend."""
    def __init__(self):
        self._icon_handle = None
        self.visible = True
        self.events = []
    def _assert_icon_handle(self):
        self.events.append("compatible_loader")
        self._icon_handle = 500
    def _release_icon(self):
        self.events.append(("release", self._icon_handle))
        self._icon_handle = None
    def _show(self):
        self._assert_icon_handle(); self.events.append("show")
    def _hide(self): self.events.append("hide")
    def _on_display_change(self, *args): pass
    def _on_taskbarcreated(self, *args): pass


def test_tray_reads_original_ico_and_reloads_after_display_or_explorer_change(monkeypatch):
    monkeypatch.setattr("app.tray.os.name", "nt")
    api = fake_api()
    icon = windows_file_icon_type(BackendIcon, ICO, api=api)()
    icon._assert_icon_handle(); icon._assert_icon_handle()
    api.load.assert_called_once_with(ICO, (24, 24))
    assert icon._icon_handle == 101
    icon._on_display_change(0, 0)
    assert icon.events == ["hide", ("release", 101), "show"]
    assert icon._icon_handle == 102
    icon._on_taskbarcreated(0, 0)
    assert icon.events[-2:] == [("release", 102), "show"]
    assert icon._icon_handle == 103


def test_tray_direct_load_failure_preserves_compatible_loader(monkeypatch, caplog):
    monkeypatch.setattr("app.tray.os.name", "nt")
    api = fake_api(); api.load.side_effect = OSError("missing ICO")
    icon = windows_file_icon_type(BackendIcon, ICO, api=api)()
    icon._assert_icon_handle()
    assert icon._icon_handle == 500 and icon.events == ["compatible_loader"]
    assert "using pystray compatible image loader" in caplog.text
    assert windows_file_icon_type(object, ICO, api=api) is object


def test_tray_native_api_initialization_failure_preserves_backend(monkeypatch, caplog):
    monkeypatch.setattr("app.tray.os.name", "nt")
    monkeypatch.setattr("app.tray.WindowsIconApi", mock.Mock(side_effect=OSError("API unavailable")))
    assert windows_file_icon_type(BackendIcon, ICO) is BackendIcon
    assert "Native tray ICO loader unavailable" in caplog.text


def test_native_subscription_failure_keeps_owned_handles_for_teardown(monkeypatch, caplog):
    api = fake_api()
    monkeypatch.setattr("app.windows_icons.WindowsIconApi", lambda: api)
    monkeypatch.setattr(NativeWindowIcons, "_install_dpi_callback", mock.Mock(side_effect=OSError("subscription denied")))
    owner = NativeWindowIcons(77, ICO)
    assert owner.handles == {False: 101, True: 102}
    api.destroy.assert_not_called()
    owner.close()
    assert api.destroy.call_args_list == [mock.call(101), mock.call(102)]
    assert "Could not subscribe" in caplog.text


def icon_pixels(handle):
    """Read a copied HICON bitmap without taking ownership of the HICON."""
    from PIL import Image
    class ICONINFO(ctypes.Structure):
        _fields_ = [("icon", wintypes.BOOL), ("x", wintypes.DWORD), ("y", wintypes.DWORD),
                    ("mask", wintypes.HBITMAP), ("color", wintypes.HBITMAP)]
    class BITMAP(ctypes.Structure):
        _fields_ = [("type", wintypes.LONG), ("width", wintypes.LONG), ("height", wintypes.LONG),
                    ("stride", wintypes.LONG), ("planes", wintypes.WORD), ("bits", wintypes.WORD), ("data", ctypes.c_void_p)]
    class INFO(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("width", wintypes.LONG), ("height", wintypes.LONG),
                    ("planes", wintypes.WORD), ("bits", wintypes.WORD), ("compression", wintypes.DWORD),
                    ("bytes", wintypes.DWORD), ("x", wintypes.LONG), ("y", wintypes.LONG),
                    ("used", wintypes.DWORD), ("important", wintypes.DWORD)]
    user, gdi = ctypes.WinDLL("user32"), ctypes.WinDLL("gdi32")
    user.GetIconInfo.argtypes = [wintypes.HICON, ctypes.POINTER(ICONINFO)]
    gdi.GetObjectW.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p]
    gdi.CreateCompatibleDC.argtypes = [wintypes.HDC]; gdi.CreateCompatibleDC.restype = wintypes.HDC
    gdi.GetDIBits.argtypes = [wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT, ctypes.c_void_p, ctypes.POINTER(INFO), wintypes.UINT]
    gdi.DeleteObject.argtypes = [wintypes.HANDLE]; gdi.DeleteDC.argtypes = [wintypes.HDC]
    info = ICONINFO()
    assert user.GetIconInfo(handle, ctypes.byref(info))
    try:
        bitmap = BITMAP(); gdi.GetObjectW(info.color, ctypes.sizeof(bitmap), ctypes.byref(bitmap))
        header = INFO(ctypes.sizeof(INFO), bitmap.width, -bitmap.height, 1, 32, 0, 0, 0, 0, 0, 0)
        pixels = (ctypes.c_ubyte * (bitmap.width * bitmap.height * 4))()
        dc = gdi.CreateCompatibleDC(None)
        try: assert gdi.GetDIBits(dc, info.color, 0, bitmap.height, pixels, ctypes.byref(header), 0)
        finally: gdi.DeleteDC(dc)
        return Image.frombytes("RGBA", (bitmap.width, bitmap.height), bytes(pixels), "raw", "BGRA")
    finally:
        if info.color: gdi.DeleteObject(info.color)
        if info.mask: gdi.DeleteObject(info.mask)


@pytest.mark.skipif(os.name != "nt", reason="Native Windows authored HICON frames")
def test_real_native_and_pinned_tray_handles_use_authored_frames_at_each_dpi(tmp_path):
    from PIL import Image
    import pystray
    from importlib.metadata import version
    assert version("pystray") == "0.19.5"
    api = WindowsIconApi()
    user = api.user
    user.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                                   ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                   wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, ctypes.c_void_p]
    user.CreateWindowExW.restype = wintypes.HWND
    hwnd = user.CreateWindowExW(0, "STATIC", "isolated icon fixture", 0, 0, 0, 50, 50, None, None, None, None)
    assert hwnd
    owner = None
    owned_handles = []
    evidence = []
    try:
        owner = NativeWindowIcons(hwnd, ICO)
        for small, handle in owner.handles.items():
            assert user.SendMessageW(hwnd, 0x007f, 0 if small else 1, 0) == handle
        # Exercise the actual subclass callback at a controlled DPI without
        # changing the monitor or the user's process/display settings.
        owner.api.window_dpi = lambda _: 144
        rect = wintypes.RECT(0, 0, 50, 50)
        user.SendMessageW(hwnd, 0x02E0, 144 | (144 << 16), ctypes.addressof(rect))
        assert icon_pixels(owner.handles[False]).size == (48, 48)
        assert icon_pixels(owner.handles[True]).size == (24, 24)
        owned_handles = list(owner.handles.values())
        for dpi in (96, 120, 144, 192):
            for small in (False, True):
                size = api.size(dpi, small=small)
                handle = api.load(ICO, size)
                try:
                    actual = icon_pixels(handle)
                    with Image.open(ICO) as authored:
                        expected = authored.ico.getimage(size).convert("RGBA")
                    assert actual.size == size
                    assert actual.getchannel("A").tobytes() == expected.getchannel("A").tobytes()
                    # GetDIBits preserves the authored PNG RGBA pixels.
                    assert actual.tobytes() == expected.tobytes()
                    actual.save(tmp_path / f"frame-{dpi}-{'small' if small else 'large'}.png")
                    evidence.append((dpi, small, size))
                finally: api.destroy(handle)
        icon_type = windows_file_icon_type(pystray.Icon, ICO)
        assert icon_type is not pystray.Icon
        with Image.open(ICO) as source:
            icon = icon_type("isolated-icon-fixture", source.convert("RGBA"))
        try:
            icon._assert_icon_handle()
            actual = icon_pixels(icon._icon_handle)
            size = api.size(api.tray_dpi(), small=True)
            with Image.open(ICO) as authored:
                assert actual.tobytes() == authored.ico.getimage(size).convert("RGBA").tobytes()
            actual.save(tmp_path / "tray-original-frame.png")
            assert actual.size == size
        finally: icon._release_icon()
        assert len(evidence) == 8
    finally:
        # Destroy our hidden fixture only; no live desktop/tray instance starts.
        user.DestroyWindow.argtypes = [wintypes.HWND]
        user.DestroyWindow(hwnd)
        if owner: owner.close()
    assert owner.closed and not owner.handles
    for handle in owned_handles:
        with pytest.raises(AssertionError):
            icon_pixels(handle)
