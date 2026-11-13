# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""平台下载冒烟测试（网络依赖，默认跳过）。

每月 yt-dlp 升级节拍的验收闸门：pins 升级后在有网环境运行

    SHENGJIAN_SMOKE=1 python -m pytest tests/test_ytdlp_smoke.py -v

覆盖 FoilPage 白名单平台的真实解析（skip_download，只走 extractor 元数据轨，
不消耗下载带宽）。失败意味着上游反爬格局变化，需要评估后再发版。
视频地址可用环境变量覆盖（默认为平台公开、长期稳定的视频）。
"""
from __future__ import annotations

import os

import pytest

from engine.downloader import YtDlpAdapter, detect_platform

_requires_smoke = pytest.mark.skipif(
    os.environ.get("SHENGJIAN_SMOKE", "") != "1",
    reason="需要真实网络；设置 SHENGJIAN_SMOKE=1 启用平台冒烟测试",
)


def _smoke_url(env_key: str, default: str) -> str:
    return os.environ.get(env_key, "").strip() or default


@_requires_smoke
def test_bilibili_extractor_resolves_formats() -> None:
    """B 站走本地 yt-dlp 下载轨：extractor 必须仍能解析出可下载格式。"""
    import yt_dlp

    url = _smoke_url("SHENGJIAN_SMOKE_BILI_URL", "https://www.bilibili.com/video/BV1GJ411x7h7")
    assert detect_platform(url) == "bilibili"
    options = {
        "format": YtDlpAdapter._format_selector(url, audio_only=False),
        "skip_download": True,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 20,
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
            "Referer": "https://www.bilibili.com/",
        },
    }
    with yt_dlp.YoutubeDL(options) as client:
        info = client.extract_info(url, download=False)
    formats = [f for f in (info or {}).get("formats") or [] if f.get("url")]
    assert formats, "B 站 extractor 未解析到任何可下载格式；评估上游变化后再升级发版"


@_requires_smoke
def test_douyin_extractor_page_resolution() -> None:
    """抖音详情页解析轨。平台常要求登录态，失败时给出可操作提示而非误报。"""
    import yt_dlp

    url = _smoke_url(
        "SHENGJIAN_SMOKE_DOUYIN_URL",
        "https://www.douyin.com/video/7419155496211438683",
    )
    assert detect_platform(url) == "douyin"
    options = {
        "format": YtDlpAdapter._format_selector(url, audio_only=False),
        "skip_download": True,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 20,
    }
    try:
        with yt_dlp.YoutubeDL(options) as client:
            info = client.extract_info(url, download=False)
    except Exception as exc:  # noqa: BLE001 - 登录态缺失属预期，明确跳过而非失败
        message = str(exc).casefold()
        pytest.skip(f"抖音解析被平台拒绝（常见：无浏览器登录态）: {exc}") if (
            "fresh cookies" in message or "login" in message or "412" in message
        ) else pytest.fail(f"抖音解析失败: {exc}")
    assert (info or {}).get("title"), "抖音 extractor 返回了空元数据"


@_requires_smoke
def test_youtube_extractor_resolves_formats() -> None:
    """YouTube 解析轨：需要网络可达 YouTube；网络受限环境会在此显式失败。"""
    import yt_dlp

    url = _smoke_url(
        "SHENGJIAN_SMOKE_YT_URL",
        "https://www.youtube.com/watch?v=jNQXAC9IVRw",
    )
    assert detect_platform(url) == "youtube"
    options = {
        "format": YtDlpAdapter._format_selector(url, audio_only=False),
        "skip_download": True,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 20,
    }
    with yt_dlp.YoutubeDL(options) as client:
        info = client.extract_info(url, download=False)
    formats = [f for f in (info or {}).get("formats") or [] if f.get("url")]
    assert formats, "YouTube extractor 未解析到任何可下载格式；检查 JS 运行时与上游变化"
