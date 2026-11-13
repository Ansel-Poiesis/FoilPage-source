# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""Cross-check signed account identity, logout races, revocation and actual purchase routing."""
from __future__ import annotations

import base64
import io
import json
import time
import urllib.error
import urllib.request
from unittest.mock import Mock

import pytest

from app.bridge import Bridge
from engine import ed25519
from engine.device import device_id
from engine.license import LicenseError, sign_license, verify_license
from engine.subscription import status


SEED = bytes(range(32))
PRIVATE = base64.urlsafe_b64encode(SEED).decode()
PUBLIC = base64.urlsafe_b64encode(ed25519.public_key(SEED)).decode()


class Controller:
    def __init__(self, settings):
        self.settings = dict(settings)

    def save_settings(self, values):
        self.settings.update(values)


class Response:
    def __init__(self, value):
        self.body = json.dumps(value).encode()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self.body


def bridge(tmp_path, **extra):
    settings = {"subscription_server_url": "https://pay.example.com", "subscription_verify_key": "dev-key",
                "account_login_token": "session-a", "account_user_id": "user-a", "credit_balance": 500,
                "subscription_active": True, "subscription_plan": "standard", "license_token": "old-license"}
    settings.update(extra)
    controller = Controller(settings)
    result = Bridge(controller, Mock(), type("Store", (), {"base_dir": str(tmp_path)})(), lambda: None, lambda: None)
    return result, controller


def remote(user="user-a", *, production_device=None):
    payload = {"sub": user, "plan_id": "standard", "credit_balance": 720, "exp": time.time() + 3600}
    if production_device is not None:
        payload["device_id"] = production_device
    token = sign_license(payload, signing_key=PRIVATE if production_device is not None else "dev-key",
                         alg="EdDSA" if production_device is not None else "HS256")
    return {"ok": True, "license_token": token, "account": {"user_id": user, "active": True,
            "display_name": user, "credit_balance": 720, "expires_at": "2099-01-01T00:00:00+00:00"}}


def health():
    return {"ok": True, "service": "shengjian-payment", "plans": ["standard"], "license_alg": "EdDSA",
            "public_catalog": [{"id": "standard", "purchasable": True, "offers": [
                {"period": "month", "months": 1, "amount": "21.00", "currency": "CNY", "credits": 500, "status": "ready", "purchasable": True}]}],
            "login_methods": {"email": True, "google": False, "wechat": False},
            "payments": {"redeem": True, "auto_reconcile": True},
            "buyer_verification": {"required": True, "configured": True, "provider": "afdian"},
            "agent_relay": {"configured": True, "stage_protocol": "v1", "model": "fixture-model"},
            "readiness": {"ready": True, "blocking": [], "live_verified": False}}


def test_production_license_must_be_device_bound():
    unbound = sign_license({"sub": "user", "exp": time.time()+3600}, signing_key=PRIVATE, alg="EdDSA")
    with pytest.raises(LicenseError, match="设备绑定"):
        verify_license(unbound, signing_key=PUBLIC, expected_alg="EdDSA", device_id="device-current")


def test_signed_license_for_another_device_cannot_raise_local_tier():
    value = remote(production_device="device-other")
    settings = {"subscription_license_public_key": PUBLIC, "license_token": value["license_token"],
                "subscription_plan": "standard", "subscription_active": True, "credit_balance": 720}
    assert status(settings, device_id="device-current")["active"] is False
    assert status(settings)["active"] is False
    assert status(settings, device_id="device-other")["active"] is True


def test_bridge_status_checks_current_local_device(tmp_path):
    value = remote(production_device="device-other")
    result, _ = bridge(tmp_path, subscription_license_public_key=PUBLIC, license_token=value["license_token"])
    assert result.get_subscription_status()["active"] is False
    result.controller.save_settings({"license_token": remote(production_device=device_id(tmp_path))["license_token"]})
    assert result.get_subscription_status()["active"] is True


def test_account_identity_must_match_license_subject(tmp_path):
    result, controller = bridge(tmp_path)
    value = remote()
    value["account"]["user_id"] = "user-b"
    assert result._apply_remote_account(value)["ok"] is False
    assert controller.settings["account_user_id"] == "user-a"


def test_definitive_session_rejection_clears_old_license(tmp_path, monkeypatch):
    result, controller = bridge(tmp_path)

    def rejected(*args, **kwargs):
        raise urllib.error.HTTPError("https://pay.example.com", 401, "expired", {}, None)

    monkeypatch.setattr(urllib.request, "urlopen", rejected)
    assert result.refresh_subscription()["ok"] is False
    assert controller.settings["account_login_token"] == ""
    assert controller.settings["license_token"] == ""
    assert controller.settings["subscription_active"] is False
    assert controller.settings["credit_balance"] == 0


@pytest.mark.parametrize("http_status,body", [
    (409, {"ok": False, "error": "账户刚发生变更，请重新同步"}),
    (409, {"ok": False, "status": "invalid", "error": "同步冲突，请重试"}),
    (403, "<html>Cloudflare quota or challenge</html>"),
    (403, {"ok": False, "status": "unavailable", "error": "服务暂不可达"}),
    (429, {"ok": False, "status": "rate_limited"}),
    (503, {"ok": False, "status": "unavailable"}),
    (409, {"ok": "false", "error": "设备已解绑，请重新登录"}),
    (409, {"ok": False, "error": "设备已解绑，请重新登录", "unexpected": True}),
    (409, {"ok": False, "status": "invalid", "error": {"message": "设备已解绑，请重新登录"}}),
    (409, ["设备已解绑，请重新登录"]),
])
def test_temporary_http_refresh_failures_preserve_signed_offline_login(tmp_path, monkeypatch, http_status, body):
    from engine.account_access import verified_account_access

    value = remote(production_device=device_id(tmp_path))
    result, controller = bridge(tmp_path, subscription_license_public_key=PUBLIC, license_token=value["license_token"])
    before = dict(controller.settings)
    assert verified_account_access(before, tmp_path)["allowed"] is True
    raw = body.encode() if isinstance(body, str) else json.dumps(body).encode()

    def rejected(*args, **kwargs):
        raise urllib.error.HTTPError("https://pay.example.com", http_status, "rejected", {}, io.BytesIO(raw))

    monkeypatch.setattr(urllib.request, "urlopen", rejected)
    response = result.refresh_subscription()
    assert response == {"ok": False, "error": "暂时无法同步，已保留本机登录；请稍后重试"}
    assert controller.settings == before
    assert verified_account_access(controller.settings, tmp_path)["allowed"] is True
    assert result.get_subscription_status()["active"] is True


@pytest.mark.parametrize("http_status,body", [
    (403, {"ok": False, "status": "device_limit", "error": "请先完成设备管理并重新登录"}),
    (409, {"ok": False, "error": "设备已解绑，请重新登录"}),
    (409, {"ok": False, "error": "登录已过期，请重新登录"}),
    (409, {"ok": False, "status": "invalid", "error": "登录已失效，请重新登录"}),
])
def test_explicit_structured_refresh_rejection_clears_old_authorization(tmp_path, monkeypatch, http_status, body):
    result, controller = bridge(tmp_path)

    def rejected(*args, **kwargs):
        raise urllib.error.HTTPError("https://pay.example.com", http_status, "rejected", {}, io.BytesIO(json.dumps(body).encode()))

    monkeypatch.setattr(urllib.request, "urlopen", rejected)
    assert result.refresh_subscription() == {"ok": False, "error": "登录已失效，请重新登录"}
    assert controller.settings["account_login_token"] == ""
    assert controller.settings["license_token"] == ""
    assert controller.settings["subscription_active"] is False
    assert controller.settings["credit_balance"] == 0


@pytest.mark.parametrize("body", [
    {"ok": False}, {"ok": False, "status": "invalid"},
    {"ok": False, "status": "unavailable", "error": "账户服务不可达"},
    None, [], {"ok": 1},
])
def test_ambiguous_refresh_response_preserves_existing_authorization(tmp_path, monkeypatch, body):
    result, controller = bridge(tmp_path)
    before = dict(controller.settings)
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: Response(body))
    assert result.refresh_subscription()["error"] == "暂时无法同步，已保留本机登录；请稍后重试"
    assert controller.settings == before


def test_explicit_refresh_response_revocation_is_not_ignored(tmp_path, monkeypatch):
    result, controller = bridge(tmp_path)
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: Response(
        {"ok": False, "status": "invalid", "error": "设备已解绑，请重新登录"}))
    assert result.refresh_subscription()["error"] == "登录已失效，请重新登录"
    assert not controller.settings["account_login_token"]
    assert not controller.settings["license_token"]


@pytest.mark.parametrize("change", [
    {"account_login_token": "session-b", "account_user_id": "user-b", "license_token": "new-license"},
    {"subscription_server_url": "https://other.example.com"},
])
@pytest.mark.parametrize("http_status", [401, 409])
def test_late_refresh_rejection_cannot_clear_changed_account_or_service(tmp_path, monkeypatch, change, http_status):
    result, controller = bridge(tmp_path)

    def rejected(*args, **kwargs):
        controller.save_settings(change)
        raise urllib.error.HTTPError("https://pay.example.com", http_status, "expired", {}, io.BytesIO(
            json.dumps({"ok": False, "error": "设备已解绑，请重新登录"}).encode()))

    monkeypatch.setattr(urllib.request, "urlopen", rejected)
    assert result.refresh_subscription() == {"ok": False, "error": "登录状态已变更，已忽略旧响应"}
    assert controller.settings == {**bridge(tmp_path)[1].settings, **change}


def test_error_response_is_bounded_and_not_rendered(tmp_path, monkeypatch):
    result, controller = bridge(tmp_path)
    before = dict(controller.settings)

    class ErrorBody(io.BytesIO):
        def read(self, limit):
            assert limit == 65537
            return super().read(limit)

    raw = json.dumps({"ok": False, "error": "设备已解绑，请重新登录", "padding": "x" * 65536}).encode()
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: (_ for _ in ()).throw(
        urllib.error.HTTPError("https://pay.example.com", 409, "rejected", {}, ErrorBody(raw))))
    response = result.refresh_subscription()
    assert response["ok"] is False and "padding" not in response["error"]
    assert controller.settings == before


def test_temporary_network_failure_preserves_valid_offline_state(tmp_path, monkeypatch):
    result, controller = bridge(tmp_path)
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: (_ for _ in ()).throw(urllib.error.URLError("offline")))
    assert result.refresh_subscription()["ok"] is False
    assert controller.settings["account_login_token"] == "session-a"
    assert controller.settings["license_token"] == "old-license"


def test_retained_expired_license_does_not_grant_offline_access(tmp_path, monkeypatch):
    from engine.account_access import verified_account_access

    expired = sign_license({"sub": "user-a", "device_id": device_id(tmp_path), "exp": time.time() - 1},
                           signing_key=PRIVATE, alg="EdDSA")
    result, controller = bridge(tmp_path, subscription_license_public_key=PUBLIC, license_token=expired)
    before = dict(controller.settings)
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: (_ for _ in ()).throw(urllib.error.URLError("offline")))
    response = result.refresh_subscription()
    assert response == {"ok": False, "error": "暂时无法同步，已保留本机登录；请稍后重试"}
    assert controller.settings == before
    assert verified_account_access(controller.settings, tmp_path)["allowed"] is False
    assert result.get_subscription_status()["active"] is False


def test_expired_old_session_does_not_cancel_a_new_oauth_attempt(tmp_path):
    result, controller = bridge(tmp_path, account_pending_login_token="pending-new")
    result._clear_account(expected_session="session-a")
    assert controller.settings["account_login_token"] == ""
    assert controller.settings["account_pending_login_token"] == "pending-new"


def test_logout_clears_local_paid_state(tmp_path, monkeypatch):
    result, controller = bridge(tmp_path)
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: Response({"ok": True}))
    assert result.account_logout()["ok"]
    assert not controller.settings["license_token"]
    assert not controller.settings["subscription_active"]
    assert not controller.settings["account_login_token"]


def test_late_refresh_cannot_revive_logged_out_account(tmp_path, monkeypatch):
    result, controller = bridge(tmp_path)

    def delayed(*args, **kwargs):
        result._clear_account()
        return Response(remote())

    monkeypatch.setattr(urllib.request, "urlopen", delayed)
    assert not result.refresh_subscription()["ok"]
    assert controller.settings["account_login_token"] == ""
    assert controller.settings["license_token"] == ""


def test_late_refresh_cannot_overwrite_new_account(tmp_path, monkeypatch):
    result, controller = bridge(tmp_path)

    def delayed(*args, **kwargs):
        controller.save_settings({"account_login_token": "session-b", "account_user_id": "user-b", "credit_balance": 30})
        return Response(remote())

    monkeypatch.setattr(urllib.request, "urlopen", delayed)
    assert not result.refresh_subscription()["ok"]
    assert controller.settings["account_user_id"] == "user-b"
    assert controller.settings["credit_balance"] == 30


def test_old_oauth_poll_cannot_replace_new_pending_login(tmp_path, monkeypatch):
    result, controller = bridge(tmp_path, account_pending_login_token="pending-a")

    def delayed(*args, **kwargs):
        controller.save_settings({"account_pending_login_token": "pending-b"})
        return Response({**remote(), "status": "ok", "login_token": "pending-a"})

    monkeypatch.setattr(urllib.request, "urlopen", delayed)
    assert not result.wechat_login_poll()["ok"]
    assert controller.settings["account_pending_login_token"] == "pending-b"


def test_purchase_uses_server_base_and_only_the_real_available_catalog(tmp_path, monkeypatch):
    result, _ = bridge(tmp_path, subscription_license_public_key=PUBLIC)
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: Response(health()))
    contract = result.check_subscription_service()
    assert contract["checkout_ready"]
    assert contract["available_plans"] == ["standard"]
    assert contract["production_ready"] is False
    assert result.begin_subscription("standard")["checkout_url"] == "https://pay.example.com/checkout?plan=standard&period=month"
    assert result.begin_subscription("advanced")["ok"] is False


@pytest.mark.parametrize("payments", [
    "missing", None, {}, [], True, "true", {"auto_reconcile": True},
    {"redeem": False}, {"redeem": None}, {"redeem": "true"}, {"redeem": 1},
])
def test_configured_service_does_not_open_purchase_without_explicit_payment_capability(tmp_path, monkeypatch, payments):
    result, _ = bridge(tmp_path, subscription_license_public_key=PUBLIC)
    payload = health()
    if payments == "missing":
        payload.pop("payments")
    else:
        payload["payments"] = payments
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: Response(payload))
    contract = result.check_subscription_service()
    assert contract["configuration_ready"] is True, "技术配置齐全不代表购买已经开放"
    assert contract["available_plans"] == ["standard"]
    assert contract["checkout_ready"] is False
    assert "购买与兑换暂未开放" in contract["gates"]
    assert result.begin_subscription("standard")["ok"] is False


def test_explicit_payment_capability_opens_only_configured_purchase(tmp_path, monkeypatch):
    result, _ = bridge(tmp_path, subscription_license_public_key=PUBLIC)
    payload = health()
    payload["payments"] = {"redeem": True, "auto_reconcile": False}
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: Response(payload))
    contract = result.check_subscription_service()
    assert contract["configuration_ready"] is True
    assert contract["checkout_ready"] is True
    assert "购买与兑换暂未开放" not in contract["gates"]
    assert result.begin_subscription("standard")["ok"] is True
    assert result.begin_subscription("advanced")["ok"] is False


@pytest.mark.parametrize("relay", [None, {}, {"configured": False, "stage_protocol": "v1"},
                                  {"configured": "true", "stage_protocol": "v1"},
                                  {"configured": True}, {"configured": True, "stage_protocol": "legacy"}])
def test_purchase_requires_an_available_stage_relay_even_if_service_reports_ready(tmp_path, monkeypatch, relay):
    result, _ = bridge(tmp_path, subscription_license_public_key=PUBLIC)
    value = health()
    value["agent_relay"] = relay
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: Response(value))
    contract = result.check_subscription_service()
    assert not contract["checkout_ready"]
    assert "配置可用的托管模型与分阶段接口" in contract["gates"]
    assert not result.begin_subscription("standard")["ok"]


@pytest.mark.parametrize("algorithm", [None, "HS256"])
def test_purchase_requires_explicit_production_license_algorithm(tmp_path, monkeypatch, algorithm):
    result, _ = bridge(tmp_path, subscription_license_public_key=PUBLIC)
    value = health()
    value["license_alg"] = algorithm
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: Response(value))
    assert not result.check_subscription_service()["checkout_ready"]
    assert not result.begin_subscription("standard")["ok"]


def test_changed_service_base_does_not_reuse_old_health_readiness(tmp_path, monkeypatch):
    result, controller = bridge(tmp_path, subscription_license_public_key=PUBLIC)
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: Response(health()))
    assert result.check_subscription_service()["checkout_ready"]
    controller.save_settings({"subscription_server_url": "https://other.example.com"})
    assert not result.get_subscription_contract()["checkout_ready"]


def test_false_readiness_with_no_blocking_messages_still_blocks_purchase(tmp_path, monkeypatch):
    result, _ = bridge(tmp_path, subscription_license_public_key=PUBLIC)
    value = health()
    value["readiness"] = {"ready": False, "blocking": []}
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: Response(value))
    assert not result.check_subscription_service()["checkout_ready"]


def test_catalog_drives_checkout_and_missing_catalog_closes_purchase(monkeypatch, tmp_path):
    result, controller = bridge(tmp_path, subscription_license_public_key=PUBLIC)
    controller.save_settings({"subscription_server_url": "https://pay.example"})
    monkeypatch.setattr("urllib.request.urlopen", lambda *args, **kwargs: Response(health()))
    contract = result.check_subscription_service()
    assert contract["public_catalog"][0]["price"] == "¥21/月"
    assert contract["public_catalog"][0]["offers"][0]["credits"] == 500
    purchase = result.begin_subscription("standard")
    assert purchase["ok"] and purchase["plan"]["price"] == "¥21/月"
    old_health = health()
    old_health.pop("public_catalog")
    monkeypatch.setattr("urllib.request.urlopen", lambda *args, **kwargs: Response(old_health))
    assert not result.check_subscription_service()["checkout_ready"]
    assert not result.begin_subscription("standard")["ok"]


def test_public_catalog_rejects_false_prices_and_strips_provider_fields():
    from app.bridge import _public_subscription_catalog
    catalog = _public_subscription_catalog([
        {"id": "standard", "token": "private", "purchasable": True, "offers": [
            {"period": "month", "months": 1, "amount": "NaN", "currency": "CNY", "status": "ready", "purchasable": True, "plan_id": "private"},
            {"period": "year", "months": 12, "amount": "228.00", "currency": "CNY", "credits": 6000, "status": "ready", "purchasable": True}]},
        {"id": "advanced", "purchasable": True, "offers": [
            {"period": "month", "months": 1, "amount": "39.00", "currency": "CNY", "credits": 5000, "status": "ready", "purchasable": True},
            {"period": "month", "months": 1, "amount": "49.00", "currency": "CNY", "credits": 5000, "status": "ready", "purchasable": True}]}])
    assert catalog[0]["price"] == "金额待配置" and catalog[0]["annual_price"] == "¥228/年"
    assert catalog[0]["purchasable"] and not catalog[1]["purchasable"]
    assert "private" not in json.dumps(catalog)


def test_public_catalog_unknown_credits_cannot_open_purchase():
    from app.bridge import _public_subscription_catalog
    value = health()["public_catalog"]
    value[0]["offers"][0].pop("credits")
    catalog = _public_subscription_catalog(value)
    assert not catalog[0]["purchasable"]
    assert catalog[0]["offers"][0]["status"] == "missing_credits"
