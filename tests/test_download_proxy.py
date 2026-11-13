# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""下载分域代理策略（TASK-8.22）：

- ``proxy_route_for_platform`` 的策略矩阵（auto/direct/system/custom）；
- 设置层默认值、校验与 v18 → v19 迁移；
- ``YtDlpAdapter`` 把路由结果写进 yt-dlp ``proxy`` 选项（桩 YoutubeDL 捕获）；
- 直连媒体通道 ``_open_media`` 的直连优先语义。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine import dns_fallback
from engine.downloader import (
    YtDlpAdapter,
    _open_media,
    proxy_route_for_platform,
)
from engine.settings import SettingsStore, SettingsValidationError


@pytest.fixture(autouse=True)
def _clean_dns_guard():
    dns_fallback.reset_dns_fallback_for_tests()
    yield
    dns_fallback.reset_dns_fallback_for_tests()


# --------------------------------------------------------------- 路由矩阵

def test_auto_policy_routes_domestic_direct_and_foreign_via_system() -> None:
    assert proxy_route_for_platform("bilibili") == ""
    assert proxy_route_for_platform("douyin") == ""
    assert proxy_route_for_platform("youtube") is None
    assert proxy_route_for_platform("unknown") is None


def test_explicit_policies_override_domain_routing() -> None:
    assert proxy_route_for_platform("youtube", policy="direct") == ""
    assert proxy_route_for_platform("bilibili", policy="system") is None
    assert (
        proxy_route_for_platform("bilibili", policy="custom", custom_url="socks5://127.0.0.1:1080")
        == "socks5://127.0.0.1:1080"
    )


def test_custom_policy_without_usable_url_falls_back_to_system() -> None:
    # 空/形态非法的自定义地址退回系统代理，与设置层「先切模式后填地址」的
    # 保存顺序兼容。
    assert proxy_route_for_platform("youtube", policy="custom", custom_url="") is None
    assert proxy_route_for_platform("youtube", policy="custom", custom_url="not a url") is None


def test_unknown_policy_behaves_like_auto() -> None:
    assert proxy_route_for_platform("douyin", policy="weird") == ""
    assert proxy_route_for_platform("youtube", policy="weird") is None


# --------------------------------------------------------------- 设置层

def test_proxy_settings_defaults_and_validation(tmp_path: Path) -> None:
    store = SettingsStore(base_dir=tmp_path)
    settings = store.load()
    assert settings["download_proxy_policy"] == "auto"
    assert settings["download_proxy_url"] == ""

    updated = store.update(download_proxy_policy="custom", download_proxy_url="http://127.0.0.1:7890")
    assert updated["download_proxy_policy"] == "custom"
    assert updated["download_proxy_url"] == "http://127.0.0.1:7890"

    with pytest.raises(SettingsValidationError):
        store.update(download_proxy_policy="vpn")
    with pytest.raises(SettingsValidationError):
        store.update(download_proxy_url="ftp://nope")
    with pytest.raises(SettingsValidationError):
        store.update(download_proxy_url="x" * 600)


def test_v18_settings_migrate_to_v19_with_proxy_defaults(tmp_path: Path) -> None:
    legacy = SettingsStore(base_dir=tmp_path).load()
    legacy["schema_version"] = 18
    legacy.pop("download_proxy_policy", None)
    legacy.pop("download_proxy_url", None)
    store = SettingsStore(base_dir=tmp_path)
    store.path.write_text(json.dumps({**legacy, "schema_version": 18}), encoding="utf-8")
    migrated = store.load()
    assert migrated["schema_version"] == 20
    assert migrated["download_proxy_policy"] == "auto"
    assert migrated["download_proxy_url"] == ""


# --------------------------------------------------------------- 适配器布线

class _StubYoutubeDL:
    """捕获 options 的 YoutubeDL 桩：resolve 不触网。"""

    captured: list[dict] = []

    def __init__(self, options: dict) -> None:
        self.options = options
        _StubYoutubeDL.captured.append(options)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def extract_info(self, url: str, download: bool = False):
        return {"title": "样本", "url": "https://www.douyin.com/aweme/v1/play/7301234567890123456"}


@pytest.fixture()
def stub_ytdlp(monkeypatch):
    _StubYoutubeDL.captured = []
    import yt_dlp

    monkeypatch.setattr(yt_dlp, "YoutubeDL", _StubYoutubeDL)
    return _StubYoutubeDL


def _bilibili_url() -> str:
    return "https://www.bilibili.com/video/BV1xx411c7mD"


def test_resolve_forces_direct_for_bilibili_under_auto(stub_ytdlp) -> None:
    YtDlpAdapter().resolve(_bilibili_url(), proxy_policy="auto")
    assert stub_ytdlp.captured, "YoutubeDL 未被调用"
    assert stub_ytdlp.captured[-1]["proxy"] == ""


def test_resolve_respects_system_proxy_for_youtube_under_auto(stub_ytdlp) -> None:
    YtDlpAdapter().resolve("https://www.youtube.com/watch?v=abc", proxy_policy="auto")
    assert "proxy" not in stub_ytdlp.captured[-1]


def test_resolve_custom_policy_applies_to_all_platforms(stub_ytdlp) -> None:
    YtDlpAdapter().resolve(
        _bilibili_url(),
        proxy_policy="custom",
        proxy_url="socks5://127.0.0.1:1080",
    )
    assert stub_ytdlp.captured[-1]["proxy"] == "socks5://127.0.0.1:1080"


def test_open_media_direct_route_skips_default_opener() -> None:
    # 直连路由必须用无代理 opener，而不是默认 urlopen（会再撞一次系统代理）。
    direct = mock.Mock(name="direct-opener")
    default = mock.Mock(name="default-urlopen")
    with mock.patch("urllib.request.urlopen", default), \
            mock.patch("engine.downloader._no_proxy_opener", return_value=direct):
        _open_media("https://v.douyinvod.com/x.mp4", {}, proxy_route="")
    direct.open.assert_called_once()
    default.assert_not_called()


def test_open_media_socks_route_falls_back_to_default_behaviour() -> None:
    # urllib 不支持 SOCKS：SOCKS 路由在直提通道按系统默认处理。
    default = mock.Mock(name="default-urlopen")
    with mock.patch("urllib.request.urlopen", default):
        _open_media("https://v.douyinvod.com/x.mp4", {}, proxy_route="socks5://127.0.0.1:1080")
    default.assert_called_once()
