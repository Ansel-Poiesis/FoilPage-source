# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""同版本号热修（先生 2026-11-18）：/health 的扩展内容指纹驱动扩展自重载。

指纹只依赖目录内容的哈希：升版本号不再是热修前置；目录签名未变时走
30s 缓存，/health 高频调用不重算。"""
from pathlib import Path

import app.local_api as local_api


def _make_extension_dir(tmp_path: Path) -> Path:
    root = tmp_path / "browser-extension"
    (root / "icons").mkdir(parents=True)
    (root / "manifest.json").write_text('{"version": "0.1.5"}', encoding="utf-8")
    (root / "background.js").write_text("console.log('a')", encoding="utf-8")
    (root / "icons" / "icon-16.png").write_bytes(b"png16")
    return root


def test_revision_is_stable_for_unchanged_content(tmp_path, monkeypatch):
    root = _make_extension_dir(tmp_path)
    monkeypatch.setattr(local_api, "_extension_dir", lambda: root)
    first = local_api._bundled_extension_revision()
    assert len(first) == 16 and first != ""
    assert local_api._bundled_extension_revision() == first


def test_revision_changes_with_content_and_is_order_independent(tmp_path, monkeypatch):
    root = _make_extension_dir(tmp_path)
    monkeypatch.setattr(local_api, "_extension_dir", lambda: root)
    first = local_api._bundled_extension_revision()
    (root / "background.js").write_text("console.log('b')", encoding="utf-8")
    assert local_api._bundled_extension_revision() != first
    (root / "extra.js").write_text("added", encoding="utf-8")
    third = local_api._bundled_extension_revision()
    # 同内容重建（先删后写）不改变指纹：目录签名变化触发重算，结果仍一致。
    (root / "extra.js").unlink()
    (root / "extra.js").write_text("added", encoding="utf-8")
    assert local_api._bundled_extension_revision() == third


def test_missing_directory_yields_empty_revision(tmp_path, monkeypatch):
    monkeypatch.setattr(local_api, "_extension_dir", lambda: tmp_path / "absent")
    assert local_api._bundled_extension_revision() == ""


def _capture_diagnostic_body() -> dict:
    return {
        "outcome": "url_captured", "reason": "", "url_source": "captured_media",
        "elapsed_ms": 1, "main_world_present": True, "main_world_stale": False,
        "main_world_version": "0.1.5", "detail_requests": 1, "detail_responses": 1,
        "detail_json_responses": 1, "detail_json_failures": 0, "media_items": 1,
        "response_aweme_nodes": 1, "response_video_nodes": 1,
        "response_play_addr_nodes": 1, "response_url_candidates": 1,
        "response_https_candidates": 1, "response_allowed_urls": 1,
        "captured_targets": 1, "target_captured": True,
        "player_present": True, "player_ready_state": 4, "player_paused": False,
    }


def test_capture_diagnostic_accepts_age_and_refresh_observability():
    body = _capture_diagnostic_body()
    body.update({
        "newest_candidate_age_ms": 489137,
        "candidate_ages_ms": [489137, 489137, 489137],
        "capture_refresh": "fail:replay_rejected",
    })
    cleaned = local_api._sanitize_capture_diagnostic(body)
    assert cleaned["newest_candidate_age_ms"] == 489137
    assert cleaned["candidate_ages_ms"] == [489137] * 3
    assert cleaned["capture_refresh"] == "fail:replay_rejected"
    # 旧扩展缺省：三个新字段均可省略且不报错。
    legacy = local_api._sanitize_capture_diagnostic(_capture_diagnostic_body())
    assert legacy["newest_candidate_age_ms"] is None
    assert legacy["candidate_ages_ms"] == []
    assert legacy["capture_refresh"] == ""


def test_capture_diagnostic_rejects_bad_age_observability():
    import pytest
    for bad in (
        {"newest_candidate_age_ms": -1},
        {"newest_candidate_age_ms": True},
        {"candidate_ages_ms": [1, 2, "3"]},
        {"candidate_ages_ms": list(range(9))},
        {"capture_refresh": "x" * 41},
    ):
        body = _capture_diagnostic_body() | bad
        with pytest.raises(ValueError):
            local_api._sanitize_capture_diagnostic(body)


def test_share_short_links_canonicalize_to_detail_pages(monkeypatch):
    monkeypatch.setattr(local_api, "_resolve_share_redirect",
        lambda url: "https://www.douyin.com/video/7678647295793307065?previous_page=web_code_link")
    assert local_api.canonicalize_douyin_share_url("https://v.douyin.com/BuwG3cVrBtI/") \
        == "https://www.douyin.com/video/7678647295793307065"
    # 非分享链接原样透传（规范化绝不碰正常详情页）。
    canonical = "https://www.douyin.com/video/7709327519954128162"
    assert local_api.canonicalize_douyin_share_url(canonical) == canonical


def test_note_share_links_and_failures(monkeypatch):
    monkeypatch.setattr(local_api, "_resolve_share_redirect",
        lambda url: "https://www.iesdouyin.com/share/note/7678647295793307065/")
    assert local_api.canonicalize_douyin_share_url("https://v.douyin.com/noteXYZ/") \
        == "https://www.douyin.com/note/7678647295793307065"


def test_unresolvable_share_link_fails_fast(monkeypatch):
    import pytest
    from engine.downloader import DownloadAdapterError

    def boom(url):
        raise OSError("network down")

    monkeypatch.setattr(local_api, "_resolve_share_redirect", boom)
    with pytest.raises(DownloadAdapterError) as raised:
        local_api.canonicalize_douyin_share_url("https://v.douyin.com/BuwG3cVrBtI/")
    assert raised.value.code == "douyin_share_unresolvable"

    monkeypatch.setattr(local_api, "_resolve_share_redirect",
        lambda url: "https://www.iesdouyin.com/share/user/MS4wLjABAAAA")
    with pytest.raises(DownloadAdapterError) as raised:
        local_api.canonicalize_douyin_share_url("https://v.douyin.com/BuwG3cVrBtI/")
    assert raised.value.code == "douyin_share_unresolvable"


def _controls_hidden_body(reason="live_or_ad"):
    body = _capture_diagnostic_body()
    body.update({"outcome": "controls_hidden", "reason": reason, "url_source": "controls_sync"})
    return body


def test_controls_hidden_diagnostics_are_accepted_with_reason_and_source():
    # 先生 2026-11-19：此前 controls_hidden/desktop_offline_queued 的 outcome
    # 不在白名单，这类上报全部被 400 丢弃——按钮消失零留痕的根因。
    cleaned = local_api._sanitize_capture_diagnostic(_controls_hidden_body("invisible_container"))
    assert cleaned["outcome"] == "controls_hidden"
    assert cleaned["reason"] == "invisible_container"
    offline = _capture_diagnostic_body()
    offline.update({"outcome": "desktop_offline_queued", "reason": "", "url_source": "offline_queue"})
    cleaned_offline = local_api._sanitize_capture_diagnostic(offline)
    assert cleaned_offline["outcome"] == "desktop_offline_queued"


def test_controls_hidden_inconsistent_combos_are_rejected():
    import pytest
    for bad in (
        _controls_hidden_body(""),                       # 缺 reason
        {**_controls_hidden_body(), "url_source": ""},   # 缺固定来源
    ):
        with pytest.raises(ValueError):
            local_api._sanitize_capture_diagnostic(bad)
