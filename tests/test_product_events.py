# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""Account notices are bounded display data, never an entitlement source."""
from __future__ import annotations

from datetime import datetime, timezone
import json

import pytest

from engine.product_events import ProductEvents, notice_subject


NOW = datetime(2026, 10, 9, tzinfo=timezone.utc).timestamp()


@pytest.fixture(autouse=True)
def freeze_notice_clock(monkeypatch):
    import engine.product_events as module
    class Fixed(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 10, 9, tzinfo=timezone.utc)
    monkeypatch.setattr(module, "datetime", Fixed)


def account_event(sequence=1, **changes):
    return {"id": sequence, "event_id": f"{sequence:064x}", "kind": "credits_granted",
            "title": "积分已到账", "message": "新增 500 积分", "created_at": "2026-10-08T23:00:00Z",
            "expires_at": "2026-10-10T00:00:00Z", **changes}


def announcement(**changes):
    return {"id": "stable-1.0.1", "kind": "software_update", "channel": "stable", "version": "1.0.1",
            "title": "有新版本", "message": "可以更新 FoilPage", "published_at": "2026-10-08T23:00:00Z",
            "expires_at": "2026-10-10T00:00:00Z",
            "url": "https://github.com/Ansel-Poiesis/FoilPage-source/releases/tag/v1.0.1", **changes}


def test_account_cursor_and_notices_are_isolated_by_account_and_server(tmp_path):
    inbox = ProductEvents(tmp_path / "inbox.json")
    one = notice_subject("https://pay.example.com/", "account-one")
    two = notice_subject("https://pay.example.com", "account-two")
    elsewhere = notice_subject("https://other.example.com", "account-one")
    assert len({one, two, elsewhere}) == 3
    assert one == notice_subject("https://pay.example.com", "account-one")
    fresh = inbox.accept_account(one, {"ok": True, "cursor": 1, "events": [account_event()]}, now=NOW)
    assert len(fresh) == 1 and inbox.cursor(one) == 1
    assert inbox.cursor(two) == inbox.cursor(elsewhere) == 0
    assert inbox.snapshot(two, "stable")["events"] == []
    assert inbox.snapshot(elsewhere, "stable")["events"] == []
    # A response advances only its own cursor; an empty repeat does not replay.
    assert inbox.accept_account(one, {"ok": True, "cursor": 1, "events": []}, now=NOW) == []
    assert len(inbox.snapshot(one, "stable")["events"]) == 1


@pytest.mark.parametrize("response", [
    {"ok": True, "cursor": 0, "events": []},
    {"ok": True, "cursor": True, "events": []},
    {"ok": True, "cursor": 3, "events": [account_event(2), account_event(2)]},
    {"ok": True, "cursor": 2, "events": [account_event(1)]},
    {"ok": True, "cursor": 2, "events": [account_event(3)]},
    {"ok": True, "cursor": 2, "events": [account_event(2, kind="software_update")]},
    {"ok": True, "cursor": 2, "events": [account_event(2, created_at="2026-10-09T00:10:00Z")]},
])
def test_invalid_account_batch_cannot_change_accepted_cursor_or_notices(tmp_path, response):
    inbox = ProductEvents(tmp_path / "inbox.json")
    subject = notice_subject("https://pay.example.com", "one")
    inbox.accept_account(subject, {"ok": True, "cursor": 1, "events": [account_event()]}, now=NOW)
    before = inbox.path.read_bytes()
    with pytest.raises(ValueError):
        inbox.accept_account(subject, response, now=NOW)
    assert inbox.path.read_bytes() == before


def test_expired_events_advance_cursor_without_persisting_visible_notice(tmp_path):
    inbox = ProductEvents(tmp_path / "inbox.json")
    subject = notice_subject("https://pay.example.com", "one")
    assert inbox.accept_account(subject, {"ok": True, "cursor": 1, "events": [
        account_event(expires_at="2026-10-08T23:30:00Z")]}, now=NOW) == []
    assert inbox.cursor(subject) == 1
    assert inbox.snapshot(subject, "stable")["events"] == []


@pytest.mark.parametrize("url", [
    "https://evil.example/download", "http://github.com/Ansel-Poiesis/FoilPage-source/releases",
    "https://github.com/other/shengjian-releases/releases", "https://github.com/Ansel-Poiesis/other/releases",
    "https://github.com/Ansel-Poiesis/FoilPage-source/releases?account=private-marker",
    "https://github.com/Ansel-Poiesis/FoilPage-source/releases#private-marker",
    "https://user:secret@github.com/Ansel-Poiesis/FoilPage-source/releases",
    "https://github.com.evil.example/Ansel-Poiesis/FoilPage-source/releases",
    "https://github.com/Ansel-Poiesis/FoilPage-source/releases-evil",
    "https://github.com/Ansel-Poiesis/FoilPage-source/releases/../issues",
    "https://github.com/Ansel-Poiesis/FoilPage-source/releases/%2e%2e/issues",
    "https://github.com/Ansel-Poiesis/FoilPage-source/releases/tag/v1.0.1/../../issues",
])
def test_update_target_cannot_direct_users_or_private_queries_elsewhere(tmp_path, url):
    inbox = ProductEvents(tmp_path / "inbox.json")
    with pytest.raises(ValueError):
        inbox.accept_announcements("stable", {"ok": True, "events": [announcement(url=url)]}, "1.0.0", now=NOW)
    assert inbox.snapshot("", "stable")["updates"] == []


def test_updates_are_channel_version_bounded_and_deduplicated(tmp_path):
    inbox = ProductEvents(tmp_path / "inbox.json")
    response = {"ok": True, "events": [announcement()]}
    assert len(inbox.accept_announcements("stable", response, "1.0.0", now=NOW)) == 1
    assert inbox.accept_announcements("stable", response, "1.0.0", now=NOW) == []
    assert inbox.snapshot("", "test")["updates"] == []
    assert len(inbox.snapshot("", "stable", "1.0.0")["updates"]) == 1
    # Persisted notices from an older installation stop offering the installed version.
    assert inbox.snapshot("", "stable", "1.0.1")["updates"] == []
    assert inbox.snapshot("", "stable", "1.1.0")["updates"] == []
    second = ProductEvents(tmp_path / "second.json")
    assert second.accept_announcements("stable", response, "1.0.1", now=NOW) == []
    with pytest.raises(ValueError):
        second.accept_announcements("test", response, "1.1.0", now=NOW)


@pytest.mark.parametrize("persisted", [
    [], {"subjects": [], "announcements": {}},
    {"subjects": {"a" * 64: None}, "announcements": {"stable": None}},
    {"subjects": {"a" * 64: {"cursor": "private-marker", "events": {"token": "private-marker"}}}, "announcements": {}},
    {"subjects": {"a" * 64: {"cursor": 1, "events": [{"event_id": "b" * 64, "kind": "arbitrary", "token": "private-marker"}]}},
     "announcements": {"stable": [{"url": "https://evil.example/private-marker", "token": "private-marker"}]}},
])
def test_tampered_nested_storage_is_fail_closed_and_not_returned(tmp_path, persisted):
    path = tmp_path / "inbox.json"
    path.write_text(json.dumps(persisted), encoding="utf-8")
    inbox = ProductEvents(path)
    assert inbox.cursor("a" * 64) >= 0
    public = inbox.snapshot("a" * 64, "stable")
    assert public["events"] == [] and public["updates"] == []
    assert "private-marker" not in json.dumps(public)


def test_persisted_notices_are_revalidated_at_read_time_and_have_no_extra_fields(tmp_path, monkeypatch):
    inbox = ProductEvents(tmp_path / "inbox.json")
    subject = notice_subject("https://pay.example.com", "one")
    inbox.accept_account(subject, {"ok": True, "cursor": 1, "events": [account_event(token="private-marker")]}, now=NOW)
    inbox.accept_announcements("stable", {"ok": True, "events": [announcement(token="private-marker")]}, "1.0.0", now=NOW)
    assert "private-marker" not in inbox.path.read_text(encoding="utf-8")
    # Test the clock-dependent snapshot through the module's clock source.
    import engine.product_events as module
    class Future(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 10, 11, tzinfo=timezone.utc)
    monkeypatch.setattr(module, "datetime", Future)
    result = inbox.snapshot(subject, "stable")
    assert result["events"] == [] and result["updates"] == []


def test_notice_http_is_bounded_and_redirects_cannot_forward_credentials(monkeypatch):
    from app.bridge import Bridge
    import urllib.request
    seen = {}
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): return None
        def read(self, maximum):
            seen["maximum"] = maximum
            return b'{"ok":true,"cursor":0,"events":[]}'
    class Opener:
        def open(self, request, timeout):
            seen.update(request=request, timeout=timeout)
            return Response()
    def create(*handlers):
        seen["handlers"] = handlers
        return Opener()
    monkeypatch.setattr(urllib.request, "build_opener", create)
    result = Bridge._product_request("https://pay.example.com", "/v1/account/notifications", {"login_token": "fixture-token"})
    assert result["ok"] is True
    assert seen["request"].method == "POST" and seen["request"].full_url == "https://pay.example.com/v1/account/notifications"
    assert seen["timeout"] == 12 and seen["maximum"] == 65537
    assert seen["handlers"][0]().redirect_request(None, None, 302, "", {}, "https://evil.example") is None


@pytest.mark.parametrize("base", ["http://pay.example.com", "https://user:secret@pay.example.com",
                                  "https://pay.example.com?token=fixture", "https://pay.example.com#fixture"])
def test_invalid_notice_service_does_not_make_request(monkeypatch, base):
    from app.bridge import Bridge
    import urllib.request
    monkeypatch.setattr(urllib.request, "build_opener", lambda *args: pytest.fail("invalid service must not be requested"))
    assert Bridge._product_request(base, "/v1/account/notifications", {"login_token": "fixture"})["ok"] is False


@pytest.mark.parametrize("wire", [b"x" * 65537, b"[]", b"not-json", b"\xff"],
                         ids=["oversized", "array", "malformed", "invalid_utf8"])
def test_bad_notice_wire_is_not_accepted_or_exposed(monkeypatch, wire):
    from app.bridge import Bridge
    from types import SimpleNamespace
    import urllib.request
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): return None
        def read(self, maximum): return wire
    monkeypatch.setattr(urllib.request, "build_opener", lambda *args: SimpleNamespace(open=lambda *a, **k: Response()))
    result = Bridge._product_request("https://pay.example.com", "/v1/announcements")
    assert result["ok"] is False
    assert "not-json" not in json.dumps(result)


def test_delayed_account_notice_is_discarded_after_account_changes(tmp_path):
    from app.bridge import Bridge
    from types import SimpleNamespace
    from unittest.mock import Mock
    import threading
    import time
    bridge = Bridge.__new__(Bridge)
    bridge._lock = threading.RLock()
    bridge.settings_store = SimpleNamespace(base_dir=tmp_path)
    bridge.controller = SimpleNamespace(product_events=ProductEvents(tmp_path / "inbox.json"), settings={"task_notifications": True})
    first = ("https://pay.example.com", "fixture-token-one", "one", 1, notice_subject("https://pay.example.com", "one"))
    second = ("https://pay.example.com", "fixture-token-two", "two", 2, notice_subject("https://pay.example.com", "two"))
    current = [first]
    bridge._notice_context = lambda: current[0]
    bridge._product_sync_busy = False
    bridge._product_sync_at = 0
    bridge._announcement_sync_at = time.monotonic()
    bridge._notifier = Mock()
    bridge.refresh_subscription = Mock()
    entered, released = threading.Event(), threading.Event()
    def fetch(*args, **kwargs):
        entered.set()
        assert released.wait(3)
        return {"ok": True, "cursor": 1, "events": [account_event()]}
    bridge._product_request = fetch
    bridge.sync_product_notifications_in_background()
    try:
        assert entered.wait(2)
        with bridge._lock: current[0] = second
    finally:
        released.set()
    deadline = time.monotonic() + 3
    while bridge._product_sync_busy and time.monotonic() < deadline: time.sleep(.005)
    assert bridge._product_sync_busy is False
    assert bridge.controller.product_events.snapshot(first[-1], "stable")["events"] == []
    assert bridge.controller.product_events.snapshot(second[-1], "stable")["events"] == []
    bridge._notifier.assert_not_called()
    bridge.refresh_subscription.assert_not_called()
