# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""订阅服务端合同的 Bridge 接口测试（合同就绪状态 + 本地验收）。"""
from __future__ import annotations

from unittest import mock

from app.bridge import Bridge


class FakeStore:
    base_dir = "C:/fake"


class FakeController:
    def __init__(self, settings=None):
        self.settings = dict(settings or {})

    def save_settings(self, changes):
        self.settings.update(changes)
        return self.settings


def make_bridge(settings=None):
    controller = FakeController(settings=settings)
    bridge = Bridge(
        controller,
        worker=mock.Mock(),
        settings_store=FakeStore(),
        window_provider=lambda: None,
        on_quit=lambda: None,
    )
    return bridge, controller


def test_contract_readiness_reports_implemented_but_not_production_ready() -> None:
    bridge, _ = make_bridge({"subscription_server_url": ""})
    contract = bridge.get_subscription_contract()
    assert contract["ok"] is True
    assert contract["client"] == "implemented"
    assert contract["server_url"] == ""
    assert contract["provider"] == "afdian"
    assert contract["production_ready"] is False
    assert set(contract["gates"]) == {"配置服务地址", "配置许可证公钥", "检查生产服务配置", "真实登录和订单验收"}


def test_contract_readiness_exposes_configured_server_url() -> None:
    bridge, _ = make_bridge({"subscription_server_url": "https://api.example.com"})
    contract = bridge.get_subscription_contract()
    assert contract["server_url"] == "https://api.example.com"


def test_run_acceptance_stores_evidence_and_reports_ok() -> None:
    bridge, _ = make_bridge({})
    evidence = {
        "overall": "ok",
        "checkout": {"ok": True},
        "checkout_idempotent": {"same_order": True},
        "payment": {"ok": True, "has_token": True},
        "license_verified": {"plan_id": "standard"},
        "usage_consume": {"used": 2},
        "usage_idempotent": {"used_unchanged": True},
        "usage_refund": {"rolled_back": True},
    }
    with mock.patch("engine.subscription_server.run_acceptance", return_value=evidence):
        result = bridge.run_subscription_acceptance()
    assert result["ok"] is True
    assert result["evidence"]["overall"] == "ok"
    contract = bridge.get_subscription_contract()
    assert contract["acceptance"]["overall"] == "ok"


def test_run_acceptance_failure_is_readable() -> None:
    bridge, _ = make_bridge({})
    with mock.patch("engine.subscription_server.run_acceptance", side_effect=RuntimeError("boom")):
        result = bridge.run_subscription_acceptance()
    assert result["ok"] is False
    assert "boom" in result["error"]
    contract = bridge.get_subscription_contract()
    assert contract["acceptance"]["overall"] == "failed"


class _FakeResponse:
    def __init__(self, payload):
        import json as _json
        self._body = _json.dumps(payload, ensure_ascii=False).encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _redeem_bridge(tmp_path, settings):

    controller = FakeController(settings=settings)
    store = type("Store", (), {"base_dir": str(tmp_path)})()
    b = Bridge(controller, mock.Mock(), store, window_provider=lambda: None, on_quit=lambda: None)
    return b, controller


def test_redeem_order_requires_server_url(tmp_path) -> None:
    bridge, _ = _redeem_bridge(tmp_path, {})
    assert bridge.redeem_subscription_order("o1") == {"ok": False, "error": "尚未配置付费服务地址（subscription_server_url）"}
    result = bridge.redeem_subscription_order("")
    assert result["ok"] is False and "订单号" in result["error"]


def test_redeem_order_verifies_license_and_updates_settings(tmp_path, monkeypatch) -> None:
    import time as _time
    import urllib.request

    from engine.license import sign_license

    token = sign_license(
        {
            "sub": "afdian:user-777",
            "plan_id": "standard",
            "period": "month",
            "monthly_agent_units": 500,
            "credit_balance": 500,
            "entitlements": ["transcribe", "byok_agent", "hosted_agent"],
            "exp": int(_time.time()) + 3600,
        },
        signing_key="dev-key",
    )
    response = _FakeResponse({
        "ok": True, "repeated": False, "granted": 500, "sub": "afdian:user-777",
        "license_token": token,
        "status": {"active": True, "plan_id": "standard", "expires_at": "2027-01-26T00:00:00+00:00", "credit_balance": 500},
    })
    monkeypatch.setattr(urllib.request, "urlopen", lambda request, timeout: response)
    bridge, controller = _redeem_bridge(tmp_path, {
        "subscription_server_url": "https://pay.example.com",
        "subscription_verify_key": "dev-key",
    })
    result = bridge.redeem_subscription_order("202609261234")
    assert result["ok"] is True
    assert result["credit_balance"] == 500
    assert controller.settings["subscription_plan"] == "standard"
    assert controller.settings["credit_balance"] == 500
    assert controller.settings["subscription_last_order_id"] == "202609261234"
    assert controller.settings["subscription_active"] is True


def test_redeem_order_rejects_tampered_license(tmp_path, monkeypatch) -> None:
    import urllib.request

    response = _FakeResponse({
        "ok": True, "repeated": False,
        "license_token": "not-a-token",
        "status": {"active": True, "plan_id": "advanced", "expires_at": "2027-01-26T00:00:00+00:00", "credit_balance": 9999},
    })
    monkeypatch.setattr(urllib.request, "urlopen", lambda request, timeout: response)
    bridge, controller = _redeem_bridge(tmp_path, {
        "subscription_server_url": "https://pay.example.com",
        "subscription_verify_key": "dev-key",
    })
    result = bridge.redeem_subscription_order("o1")
    assert result["ok"] is False and "许可证校验失败" in result["error"]
    assert controller.settings.get("credit_balance") is None, "校验失败不得入账"


def test_public_key_cannot_be_used_as_hmac_license_key(tmp_path):
    import time
    from engine.license import sign_license
    bridge, controller = _redeem_bridge(tmp_path, {"subscription_license_public_key":"known-public-key"})
    forged = sign_license({"sub":"attacker", "plan_id":"advanced", "credit_balance":99999, "exp":time.time()+3600}, signing_key="known-public-key")
    result = bridge._apply_remote_account({"license_token":forged, "account":{"active":True}})
    assert not result["ok"]
    assert controller.settings.get("credit_balance") is None


def test_unsigned_remote_balance_is_never_accepted(tmp_path):
    bridge, controller = _redeem_bridge(tmp_path, {})
    result = bridge._apply_remote_account({"account":{"active":True,"credit_balance":99999}})
    assert not result["ok"]
    assert controller.settings.get("credit_balance") is None


def test_refresh_stores_verified_license_account_and_balance(tmp_path, monkeypatch):
    import time
    import urllib.request
    from engine.license import sign_license
    token=sign_license({"sub":"user", "plan_id":"standard", "credit_balance":720, "exp":time.time()+3600}, signing_key="dev-key")
    response=_FakeResponse({"ok":True, "license_token":token, "account":{"user_id":"user", "display_name":"使用者", "active":True,"credit_balance":720, "expires_at":"2099-01-01T00:00:00+00:00"}})
    monkeypatch.setattr(urllib.request, "urlopen", lambda request, timeout:response)
    bridge, controller=_redeem_bridge(tmp_path, {"subscription_server_url":"https://pay.example.com","subscription_verify_key":"dev-key","account_login_token":"session"})
    assert bridge.refresh_subscription()["ok"]
    assert controller.settings["license_token"] == token
    assert controller.settings["credit_balance"] == 720
    assert controller.settings["account_user_id"] == "user"
    assert controller.settings["subscription_expiry_notice_history"] == {
        "account": "user", "service": "https://pay.example.com", "expires_at": "2099-01-01T00:00:00+00:00"}


def test_free_refresh_keeps_only_account_bound_expiry_reminder_then_logout_clears_it(tmp_path):
    import time
    from datetime import datetime, timezone
    from engine.license import sign_license
    from engine.subscription import status
    bridge, controller = _redeem_bridge(tmp_path, {"subscription_server_url": "https://fixture.invalid",
        "subscription_verify_key": "dev-key", "account_user_id": "fixture", "account_login_token": "synthetic",
        "subscription_plan": "standard", "subscription_active": True,
        "subscription_expires_at": "2026-01-01T00:00:00+00:00"})
    token = sign_license({"sub": "fixture", "plan_id": "free", "credit_balance": 720,
        "exp": time.time()+3600}, signing_key="dev-key")
    assert bridge._apply_remote_account({"license_token": token,
        "account": {"user_id": "fixture", "active": False, "expires_at": None}})["ok"]
    value = status(controller.settings, now=datetime(2026, 10, 15, tzinfo=timezone.utc))
    assert value["active"] is False and value["credit_balance"] == 0
    assert value["plan_expiry_notice"]["expired"] is True
    bridge._clear_account()
    assert controller.settings["subscription_expiry_notice_history"] is None
    assert status(controller.settings)["plan_expiry_notice"] is None


def test_expired_license_with_live_token_self_heals_via_refresh() -> None:
    """license 过期但登录令牌仍在：自动同步续期一次再复评，不弹登录墙。"""
    bridge, controller = make_bridge({
        "subscription_server_url": "https://api.example.com",
        "subscription_license_public_key": "Giz/dVXfLK0gGUYhtSbERp+HEWl1e6PTlWqr7gQazh8=",
        "subscription_active": True,
        "subscription_plan": "standard",
        "subscription_expires_at": "2099-01-01T00:00:00+00:00",
        "account_login_token": "login-fixedtoken",
        "account_user_id": "u-fixed",
        "license_token": "not-a-valid-license",
    })
    controller.settings["license_token"] = "not-a-valid-license"
    with mock.patch.object(bridge, "refresh_subscription", return_value={"ok": True}) as refresh:
        status = bridge.get_subscription_status()
    refresh.assert_called_once()
    assert status["license_verified"] is False  # 续期后（mock 未真正换牌）仍是失效牌
    # 60 秒冷却内不重复自愈
    with mock.patch.object(bridge, "refresh_subscription", return_value={"ok": True}) as again:
        bridge.get_subscription_status()
    again.assert_not_called()


def test_valid_license_never_triggers_selfheal() -> None:
    """license 正常（无公钥的本地验收模式）时不做任何网络自愈。"""
    bridge, _ = make_bridge({
        "subscription_server_url": "https://api.example.com",
        "subscription_active": True, "account_login_token": "login-fixedtoken",
    })
    with mock.patch.object(bridge, "refresh_subscription", return_value={"ok": True}) as refresh:
        bridge.get_subscription_status()
    refresh.assert_not_called()


def test_missing_token_does_not_selfheal() -> None:
    """未登录（无令牌）时不自愈，保持原登录引导路径。"""
    bridge, _ = make_bridge({
        "subscription_server_url": "https://api.example.com",
        "subscription_license_public_key": "Giz/dVXfLK0gGUYhtSbERp+HEWl1e6PTlWqr7gQazh8=",
        "license_token": "not-a-valid-license",
    })
    with mock.patch.object(bridge, "refresh_subscription", return_value={"ok": True}) as refresh:
        bridge.get_subscription_status()
    refresh.assert_not_called()
