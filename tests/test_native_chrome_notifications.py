# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
from xml.etree.ElementTree import fromstring

from app.native_notifications import APP_NAME, toast_xml
from app.window_chrome import NativeDragController, dpi_pixels, resize_hit
from types import SimpleNamespace


def test_notification_xml_keeps_title_and_original_caption_as_separate_plain_text():
    root = fromstring(toast_xml("下载成功", "原视频 <script> & ' $() 标题"))
    texts = root.findall("./visual/binding/text")
    assert [node.text for node in texts] == ["下载成功", "原视频 <script> & ' $() 标题"]
    assert root.find("audio").get("silent") == "true"
    assert APP_NAME == "FoilPage"


def test_frame_resizes_edges_corners_but_not_content():
    assert resize_hit(1, 1, 1280, 800) == 13
    assert resize_hit(1279, 1, 1280, 800) == 14
    assert resize_hit(1, 799, 1280, 800) == 16
    assert resize_hit(1279, 799, 1280, 800) == 17
    assert resize_hit(640, 1, 1280, 800) == 12
    assert resize_hit(640, 799, 1280, 800) == 15
    assert resize_hit(1, 400, 1280, 800) == 10
    assert resize_hit(1279, 400, 1280, 800) == 11
    assert resize_hit(640, 52, 1280, 800) == 1


class InitializationEvent:
    def __init__(self):
        self.handlers = []

    def __iadd__(self, handler):
        self.handlers.append(handler)
        return self


def test_native_drag_enables_after_initialization_before_navigation():
    event = InitializationEvent()
    control = SimpleNamespace(CoreWebView2=None, CoreWebView2InitializationCompleted=event)
    drag = NativeDragController(control)
    assert not drag.enabled and len(event.handlers) == 1
    core = SimpleNamespace(Settings=SimpleNamespace(IsNonClientRegionSupportEnabled=False))
    control.CoreWebView2 = core
    event.handlers[0](control, SimpleNamespace(IsSuccess=False))
    assert not drag.enabled and not core.Settings.IsNonClientRegionSupportEnabled
    event.handlers[0](control, SimpleNamespace(IsSuccess=True))
    assert drag.enabled and core.Settings.IsNonClientRegionSupportEnabled


def test_native_drag_unsupported_runtime_keeps_fallback(caplog):
    class OldSettings:
        __slots__ = ()
    drag = NativeDragController(SimpleNamespace(CoreWebView2=SimpleNamespace(Settings=OldSettings())))
    assert not drag.enabled
    assert "using fallback" in caplog.text


def test_native_frame_dimensions_scale_with_window_dpi():
    assert dpi_pixels(560, 144) == 840
    assert dpi_pixels(500, 144) == 750
    assert dpi_pixels(8, 192) == 16
    assert dpi_pixels(8, 0) == 8
