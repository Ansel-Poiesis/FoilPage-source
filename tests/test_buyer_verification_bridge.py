# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""Official buyer authorization stays bound to the existing signed account."""
import json
import time
import urllib.request
import webbrowser

import pytest

from engine.device import device_id
from engine.license import sign_license
from test_account_session_lifecycle import bridge, Response, PRIVATE, PUBLIC, health


def start_reply(url="https://afdian.com/oauth2/authorize?state=public-fixture"):
    return {"ok": True, "status": "pending", "authorize_url": url, "session_id": "private-poll-fixture"}


def start(result, monkeypatch):
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: Response(start_reply()))
    monkeypatch.setattr(webbrowser, "open", lambda url: True)
    return result.buyer_verification_start()


def test_buyer_poll_nonce_is_memory_only_and_full_login_is_unchanged(tmp_path, monkeypatch):
    result, controller = bridge(tmp_path)
    value = start(result, monkeypatch)
    assert value == {"ok": True, "status": "pending"}
    assert "private-poll-fixture" not in json.dumps(controller.settings)
    assert "private-poll-fixture" not in json.dumps(value)
    assert controller.settings["account_login_token"] == "session-a"
    assert controller.settings["license_token"] == "old-license"


@pytest.mark.parametrize("url", ["https://afdian.com/account", "https://afdian.com.evil.example/oauth2/authorize",
                                 "file:///C:/untrusted.exe", "http://afdian.com/oauth2/authorize"])
def test_buyer_authorization_rejects_local_or_nonofficial_addresses(tmp_path, monkeypatch, url):
    result, _ = bridge(tmp_path)
    opened = []
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: Response(start_reply(url)))
    monkeypatch.setattr(webbrowser, "open", opened.append)
    assert not result.buyer_verification_start()["ok"]
    assert result._buyer_verification is None and not opened


def test_verified_buyer_is_confirmed_against_same_account_without_applying_unsigned_entitlement(tmp_path, monkeypatch):
    result, controller = bridge(tmp_path)
    start(result, monkeypatch)
    calls = []
    def request(req, **kwargs):
        calls.append((req.full_url, json.loads(req.data)))
        if req.full_url.endswith("/buyer/poll"):
            return Response({"ok": True, "status": "verified", "buyer_verified": True,
                             "access_token": "provider-secret"})
        return Response({"ok": True, "user_id": "user-a", "buyer_verified": True,
                         "credit_balance": 999999, "license_token": "unsigned"})
    monkeypatch.setattr(urllib.request, "urlopen", request)
    value = result.buyer_verification_poll()
    assert value["status"] == "verified" and value["buyer_verified"]
    assert calls[0][1] == {"session_id": "private-poll-fixture"}
    assert calls[1][1] == {"login_token": "session-a"}
    assert "provider-secret" not in json.dumps(value)
    assert controller.settings["credit_balance"] == 500
    assert controller.settings["license_token"] == "old-license"
    assert controller.settings["account_login_token"] == "session-a"
    assert result._buyer_verification is None


def test_buyer_verification_cannot_bind_a_different_account(tmp_path, monkeypatch):
    result, _ = bridge(tmp_path)
    start(result, monkeypatch)
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, **k: Response(
        {"ok": True, "status": "verified", "buyer_verified": True} if req.full_url.endswith("/poll")
        else {"ok": True, "user_id": "user-other", "buyer_verified": True}))
    assert not result.buyer_verification_poll()["ok"]


def test_logout_rejects_late_buyer_verification_response(tmp_path, monkeypatch):
    result, controller = bridge(tmp_path)
    start(result, monkeypatch)
    def delayed(req, **kwargs):
        result._clear_account()
        return Response({"ok": True, "status": "verified", "buyer_verified": True})
    monkeypatch.setattr(urllib.request, "urlopen", delayed)
    assert not result.buyer_verification_poll()["ok"]
    assert not controller.settings["account_login_token"]
    assert result._buyer_verification is None


def test_cancelling_buyer_verification_revokes_server_challenge_without_logging_out(tmp_path, monkeypatch):
    result, controller = bridge(tmp_path)
    start(result, monkeypatch)
    calls = []
    def cancel(req, **kwargs):
        calls.append((req.full_url, json.loads(req.data)))
        return Response({"ok": True, "status": "cancelled"})
    monkeypatch.setattr(urllib.request, "urlopen", cancel)
    assert result.buyer_verification_stop()["status"] == "cancelled"
    assert result._buyer_verification is None
    assert calls[0][0].endswith("/v1/account/buyer/cancel")
    assert calls[0][1]["session_id"] == "private-poll-fixture"
    assert calls[0][1]["login_token"] == "session-a"
    assert controller.settings["account_login_token"] == "session-a"


def test_cancelling_already_completed_buyer_verification_reports_completion(tmp_path, monkeypatch):
    result, _ = bridge(tmp_path)
    start(result, monkeypatch)
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, **k: Response(
        {"ok": True, "was_completed": True} if req.full_url.endswith("/cancel")
        else {"ok": True, "user_id": "user-a", "buyer_verified": True}))
    assert result.buyer_verification_stop()["status"] == "verified"
    assert result._buyer_verification is None


def test_late_poll_after_server_cancellation_cannot_mark_buyer_verified(tmp_path, monkeypatch):
    result, _ = bridge(tmp_path)
    start(result, monkeypatch)
    def request(req, **kwargs):
        if req.full_url.endswith("/poll"):
            result.buyer_verification_stop()
            return Response({"ok": True, "status": "verified", "buyer_verified": True})
        return Response({"ok": True, "was_completed": False})
    monkeypatch.setattr(urllib.request, "urlopen", request)
    assert not result.buyer_verification_poll()["ok"]
    assert result._buyer_verification is None


def test_account_change_during_cancellation_cannot_mark_new_account_verified(tmp_path, monkeypatch):
    result, controller = bridge(tmp_path)
    start(result, monkeypatch)
    def request(req, **kwargs):
        controller.save_settings({"account_login_token": "session-new", "account_user_id": "user-new"})
        return Response({"ok": True, "was_completed": True})
    monkeypatch.setattr(urllib.request, "urlopen", request)
    assert not result.buyer_verification_stop()["ok"]
    assert controller.settings["account_user_id"] == "user-new"


def test_cancelling_an_inflight_start_revokes_its_late_nonce_without_opening_browser(tmp_path, monkeypatch):
    result, _ = bridge(tmp_path)
    opened, cancelled = [], []
    def request(req, **kwargs):
        if req.full_url.endswith("/start"):
            result.buyer_verification_stop()
            return Response(start_reply())
        cancelled.append(json.loads(req.data)["session_id"])
        return Response({"ok": True, "was_completed": False})
    monkeypatch.setattr(urllib.request, "urlopen", request)
    monkeypatch.setattr(webbrowser, "open", opened.append)
    assert not result.buyer_verification_start()["ok"]
    assert cancelled == ["private-poll-fixture"]
    assert not opened and result._buyer_verification is None


def test_buyer_full_login_is_required_before_start(tmp_path, monkeypatch):
    result, _ = bridge(tmp_path, account_login_token="")
    calls = []
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: calls.append(a))
    assert not result.buyer_verification_start()["ok"]
    assert not calls


@pytest.mark.parametrize("review, expected_count", [
    ({"count": 1}, 1),
    ({"count": 1, "paused_orders": ["private-order-a", "private-order-b"]}, 1),
    ({"count": 0, "paused_orders": ["private-order-a"]}, 0),
    ({"paused_orders": 2}, 2),
    ({"paused_orders": ["private-order-a", "private-order-b"]}, 2),
    ({"count": True, "paused_orders": 2}, 2),
    ({"count": -1, "paused_orders": 3}, 3),
    ({"count": "1", "paused_orders": ["private-order-a"]}, 1),
    ({"count": True, "paused_orders": False}, 0),
    ({"count": "2", "paused_orders": "3"}, 0),
])
def test_signed_order_review_preserves_balance_but_disables_available_quota(tmp_path, review, expected_count):
    device = device_id(tmp_path)
    token = sign_license({"sub": "user-a", "plan_id": "standard", "credit_balance": 500,
        "device_id": device, "exp": time.time()+3600,
        "order_review": {"paused": True, **review}}, signing_key=PRIVATE, alg="EdDSA")
    result, controller = bridge(tmp_path, subscription_license_public_key=PUBLIC,
        license_token=token, subscription_expires_at="2099-01-01T00:00:00+00:00")
    result._subscription_service_base = "https://pay.example.com"
    result._subscription_service = {"agent_relay": {"configured": True, "stage_protocol": "v1"}}
    value = result.get_subscription_status()
    assert not value["active"]
    assert value["credit_balance"] == 500
    assert value["agent_usage_remaining"] == 0
    assert value["order_review"]["paused"]
    assert value["order_review"]["count"] == expected_count
    assert value["order_review"]["paused_orders"] == expected_count
    assert "private-order-" not in json.dumps(value)
    assert not value["hosted_agent_ready"]
    assert "复核" in value["hosted_agent_reason"]
    assert controller.settings["license_token"] == token


def test_unsigned_order_review_cannot_override_signed_account_state(tmp_path):
    device = device_id(tmp_path)
    token = sign_license({"sub": "user-a", "plan_id": "standard", "credit_balance": 500,
        "device_id": device, "exp": time.time()+3600}, signing_key=PRIVATE, alg="EdDSA")
    result, _ = bridge(tmp_path, subscription_license_public_key=PUBLIC,
        license_token=token, order_review={"paused": True},
        subscription_expires_at="2099-01-01T00:00:00+00:00")
    value = result.get_subscription_status()
    assert value["active"] and not value["order_review"]["paused"]
    assert value["credit_balance"] == 500


@pytest.mark.parametrize("buyer", [None, {}, {"required": "true", "configured": True, "provider": "afdian"},
    {"required": True, "configured": "true", "provider": "afdian"},
    {"required": False, "configured": True, "provider": "afdian"},
    {"required": True, "configured": True, "provider": "other"}])
def test_checkout_stays_closed_without_exact_buyer_verification_capability(tmp_path, monkeypatch, buyer):
    result, _ = bridge(tmp_path, subscription_license_public_key=PUBLIC)
    payload = health(); payload["buyer_verification"] = buyer
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: Response(payload))
    contract = result.check_subscription_service()
    assert not contract["checkout_ready"]
    assert "配置爱发电买家身份验证" in contract["gates"]
    assert not contract["buyer_verification"]["configured"]
