# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""提示音（1.3.12）：预设组解析、播放与回退语义。

回退规则：未知预设组/缺失文件/非 WAV 自定义路径一律回退系统音；
silent 完全静音；播放失败不允许向任务通知链路抛错。
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

from app.notification_sounds import play_notification_sound, resolve_notification_sound

ROOT = Path(__file__).resolve().parents[1]


def _fake_winsound():
    calls: list[tuple] = []
    module = SimpleNamespace(
        SND_FILENAME=0x20000,
        SND_ASYNC=0x0001,
        MB_ICONHAND=0x0010,
        MB_ICONASTERISK=0x0040,
        PlaySound=lambda path, flags: calls.append(("play", path, flags)),
        MessageBeep=lambda kind: calls.append(("beep", kind)),
    )
    return module, calls


def test_bundled_pack_resolves_to_repo_asset() -> None:
    mode, path = resolve_notification_sound("crisp", "success", resource_root=ROOT)
    assert mode == "file"
    assert path == ROOT / "assets" / "sounds" / "crisp" / "success.wav"
    mode, path = resolve_notification_sound("deep", "error", resource_root=ROOT)
    assert (mode, path) == ("file", ROOT / "assets" / "sounds" / "deep" / "error.wav")


def test_all_bundled_pack_files_exist() -> None:
    for pack in ("crisp", "soft", "deep"):
        for kind in ("success", "error"):
            mode, path = resolve_notification_sound(pack, kind, resource_root=ROOT)
            assert mode == "file", f"{pack}/{kind} 缺少音频资产"
            assert path is not None and path.is_file()


def test_system_unknown_and_bad_kind_fall_back_to_system() -> None:
    assert resolve_notification_sound("system", "error") == ("system", None)
    assert resolve_notification_sound("bogus", "error")[0] == "system"
    assert resolve_notification_sound("crisp", "weird") == ("system", None)


def test_silent_pack_plays_nothing() -> None:
    assert resolve_notification_sound("silent", "success") == ("silent", None)
    assert resolve_notification_sound("silent", "error") == ("silent", None)


def test_custom_pack_requires_existing_wav() -> None:
    wav = ROOT / "assets" / "sounds" / "soft" / "error.wav"
    mode, path = resolve_notification_sound("custom", "error", str(wav))
    assert (mode, path) == ("file", wav)
    # 空路径、不存在、非 WAV 后缀都回退系统音。
    assert resolve_notification_sound("custom", "success", "")[0] == "system"
    assert resolve_notification_sound("custom", "success", "C:/nope.wav")[0] == "system"
    assert resolve_notification_sound("custom", "success", str(wav).replace(".wav", ".mp3"))[0] == "system"


def test_bundled_pack_with_missing_resource_root_falls_back() -> None:
    mode, _ = resolve_notification_sound(
        "crisp", "success", resource_root=Path("Z:/missing-root")
    )
    assert mode == "system"


def test_play_bundled_pack_uses_playsound(monkeypatch) -> None:
    module, calls = _fake_winsound()
    monkeypatch.setitem(sys.modules, "winsound", module)
    play_notification_sound("success", {"notification_sound_pack": "soft"}, resource_root=ROOT)
    assert len(calls) == 1
    assert calls[0][0] == "play"
    assert calls[0][1].endswith(r"assets\sounds\soft\success.wav") or calls[0][1].endswith("assets/sounds/soft/success.wav")
    assert calls[0][2] == module.SND_FILENAME | module.SND_ASYNC


def test_play_system_pack_uses_message_beep(monkeypatch) -> None:
    module, calls = _fake_winsound()
    monkeypatch.setitem(sys.modules, "winsound", module)
    play_notification_sound("success", {})
    play_notification_sound("error", {"notification_sound_pack": "system"})
    assert calls == [("beep", module.MB_ICONASTERISK), ("beep", module.MB_ICONHAND)]


def test_play_silent_and_missing_winsound_never_raise(monkeypatch) -> None:
    module, calls = _fake_winsound()
    monkeypatch.setitem(sys.modules, "winsound", module)
    play_notification_sound("success", {"notification_sound_pack": "silent"})
    assert calls == []
    # winsound 不可用（非 Windows）：静默跳过，不抛错。
    monkeypatch.setitem(sys.modules, "winsound", None)
    play_notification_sound("error", {"notification_sound_pack": "crisp"}, resource_root=ROOT)
    play_notification_sound("success", {})


def test_play_custom_missing_file_falls_back_to_system_beep(monkeypatch) -> None:
    module, calls = _fake_winsound()
    monkeypatch.setitem(sys.modules, "winsound", module)
    play_notification_sound("success", {
        "notification_sound_pack": "custom",
        "notification_sound_success_path": "C:/missing.wav",
    })
    assert calls == [("beep", module.MB_ICONASTERISK)]


def test_play_backend_failure_falls_back_silently(monkeypatch) -> None:
    module = SimpleNamespace(
        SND_FILENAME=0x20000,
        SND_ASYNC=0x0001,
        MB_ICONHAND=0x0010,
        MB_ICONASTERISK=0x0040,
        PlaySound=lambda *_a, **_k: (_ for _ in ()).throw(OSError("device busy")),
        MessageBeep=lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("no sound")),
    )
    monkeypatch.setitem(sys.modules, "winsound", module)
    # 两条路径都失败：不得抛错。
    play_notification_sound("success", {"notification_sound_pack": "crisp"}, resource_root=ROOT)
    play_notification_sound("error", {})
