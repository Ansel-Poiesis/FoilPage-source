# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""Isolated desktop order and browser-opening boundaries; no real payment."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from account_auth_fixtures import grant_account
from app.bridge import Bridge, _public_alipay_contract

ORDER = "0123456789abcdef0123456789abcdef"
BASE = "https://pay.example.invalid"
URL = BASE + "/v1/alipay/cashier?token=cap_alipay_" + "a" * 64


def order(**fields):
    return {"order_id": ORDER, "status": "pending_payment", "revision": 1,
            "created_at": "2026-10-07T01:00:00Z", "updated_at": "2026-10-07T01:00:00Z",
            "snapshot": {"plan_id": "standard", "period": "month", "months": 1,
                         "amount": "19.80", "amount_minor": 1980, "credits": 500, "currency": "CNY"}, **fields}


def response(**fields):
    return {"ok": True, "order": order(), "cashier_url": URL,
            "cashier_expires_at": (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(), **fields}


@pytest.fixture
def desktop(tmp_path):
    settings = {"subscription_server_url": BASE, "credit_balance": 0}
    grant_account(settings, tmp_path)
    controller = SimpleNamespace(settings=settings, save_settings=lambda changes: settings.update(changes))
    return Bridge(controller, Mock(), SimpleNamespace(base_dir=tmp_path), lambda: None, lambda: None), controller


@pytest.mark.parametrize("action,path,fields", [
    (lambda b: b.get_alipay_payment_config(), "config", {}),
    (lambda b: b.list_alipay_orders(), "list", {"limit": 20}),
    (lambda b: b.create_alipay_order("standard", "month", "fixture-request-001"), "create",
     {"plan_id": "standard", "period": "month", "client_request_id": "fixture-request-001"}),
    (lambda b: b.get_alipay_order(ORDER), "status", {"order_id": ORDER}),
    (lambda b: b.get_alipay_order(ORDER, True), "status", {"order_id": ORDER, "resume_payment": True}),
    (lambda b: b.query_alipay_order(ORDER), "query", {"order_id": ORDER}),
])
def test_fixed_routes_full_account_and_no_local_grant(desktop, monkeypatch, action, path, fields):
    bridge, c = desktop
    before = deepcopy(c.settings)
    transport = Mock(return_value=response(orders=[order()], order=order(status="credited"), configured=True,
        enabled=True, processing_enabled=True, provider="alipay_page_pay", catalog=[], user_id="private",
        license_token="untrusted-license", trade_no="2026100712345678901234567"))
    monkeypatch.setattr(bridge, "_account_request", transport)
    result = action(bridge)
    assert result["ok"] is True
    base, route, payload = transport.call_args.args
    assert base == BASE and route == "/v1/alipay/orders/" + path
    assert payload == {**fields, "login_token": before["account_login_token"],
                       "device_id": __import__("engine.device", fromlist=["device_id"]).device_id(bridge.settings_store.base_dir)}
    assert c.settings == before
    assert not {"cashier_url", "trade_no", "license_token", "user_id", "account"} & result.keys()


@pytest.mark.parametrize("denial", ["logout", "forged", "other_user", "management"])
def test_requires_signed_full_login_before_network_or_browser(desktop, monkeypatch, denial):
    bridge, c = desktop
    if denial == "logout": c.settings["account_login_token"] = ""
    elif denial == "forged": c.settings["license_token"] = "forged"
    elif denial == "other_user": c.settings["account_user_id"] = "other"
    else: monkeypatch.setattr(bridge, "_account_context", lambda: (BASE, "management", {"token": "management"}))
    network, browser = Mock(), Mock()
    monkeypatch.setattr(bridge, "_account_request", network)
    monkeypatch.setattr("webbrowser.open", browser)
    for action in [lambda: bridge.get_alipay_payment_config(), lambda: bridge.list_alipay_orders(),
                   lambda: bridge.create_alipay_order("standard", "month", "fixture-request-001"),
                   lambda: bridge.get_alipay_order(ORDER), lambda: bridge.query_alipay_order(ORDER),
                   lambda: bridge.open_alipay_cashier(ORDER)]: assert action()["ok"] is False
    network.assert_not_called(); browser.assert_not_called()


@pytest.mark.parametrize("change", ["logout", "session", "user", "service", "revision", "expired"])
def test_late_response_does_not_reach_other_account(desktop, monkeypatch, change):
    bridge, c = desktop
    def transport(*args):
        if change == "logout": bridge._clear_account()
        elif change == "session": c.settings["account_login_token"] = "next"
        elif change == "user": c.settings["account_user_id"] = "next"
        elif change == "service": c.settings["subscription_server_url"] = "https://other.example.invalid"
        elif change == "revision": bridge._account_revision += 1
        else: grant_account(c.settings, bridge.settings_store.base_dir, expires_at=0)
        return response()
    monkeypatch.setattr(bridge, "_account_request", transport)
    result = bridge.create_alipay_order("standard", "month", "fixture-request-001")
    assert result["ok"] is False and result["code"] == "stale_account"
    assert not getattr(bridge, "_alipay_cashiers", {})


@pytest.mark.parametrize("url", [
    "http://pay.example.invalid/v1/alipay/cashier?token=cap_alipay_" + "a" * 64,
    "https://evil.invalid/v1/alipay/cashier?token=cap_alipay_" + "a" * 64,
    "https://user@pay.example.invalid/v1/alipay/cashier?token=cap_alipay_" + "a" * 64,
    URL + "&login_token=private", URL + "#fragment", URL.replace("/cashier", "/return"),
    URL.replace("cap_alipay_", "login_"), URL.replace("example.invalid", "example.invalid:444"),
    URL.replace("/cashier", "/%63ashier"),
    "\n" + URL, URL + "\r\n", URL.replace("/cashier", "\\cashier"),
])
def test_only_exact_service_cashier_capability_can_be_cached(desktop, monkeypatch, url):
    bridge, _ = desktop
    browser = Mock()
    monkeypatch.setattr("webbrowser.open", browser)
    monkeypatch.setattr(bridge, "_account_request", Mock(return_value=response(cashier_url=url)))
    assert bridge.create_alipay_order("standard", "month", "fixture-request-001")["ok"] is False
    assert bridge.open_alipay_cashier(ORDER)["ok"] is False
    browser.assert_not_called()


def test_browser_uses_cached_order_capability_only_and_clears_on_logout(desktop, monkeypatch):
    bridge, _ = desktop
    monkeypatch.setattr(bridge, "_account_request", Mock(return_value=response()))
    browser = Mock(return_value=True); monkeypatch.setattr("webbrowser.open", browser)
    result = bridge.create_alipay_order("standard", "month", "fixture-request-001")
    assert result["cashier_available"] is True and "cashier_url" not in result
    assert bridge.open_alipay_cashier(URL)["ok"] is False
    assert bridge.open_alipay_cashier(ORDER)["ok"] is True
    browser.assert_called_once_with(URL)
    bridge._clear_account()
    assert bridge._alipay_cashiers == {} and bridge.open_alipay_cashier(ORDER)["ok"] is False


@pytest.mark.parametrize("mode", ["expiry", "paid", "refund", "closed", "browser_fail"])
def test_expired_or_final_orders_cannot_reopen_payment(desktop, monkeypatch, mode):
    bridge, _ = desktop
    transport = Mock(return_value=response());monkeypatch.setattr(bridge, "_account_request", transport)
    browser = Mock(return_value=False);monkeypatch.setattr("webbrowser.open", browser)
    bridge.create_alipay_order("standard", "month", "fixture-request-001")
    if mode == "expiry": bridge._alipay_cashiers[ORDER]["deadline"] = 0
    elif mode != "browser_fail":
        transport.return_value = response(order=order(status={"paid":"credited", "refund":"refund_review", "closed":"closed"}[mode]))
        assert bridge.query_alipay_order(ORDER)["ok"] is True
    assert bridge.open_alipay_cashier(ORDER)["ok"] is False
    assert browser.call_count == (1 if mode == "browser_fail" else 0)


@pytest.mark.parametrize("bad", ["mismatch_id", "mismatch_plan", "bad_minor", "bool_revision", "fake_state"])
def test_rejects_corrupt_or_unrelated_order_snapshot(desktop, monkeypatch, bad):
    bridge, _ = desktop
    value = order()
    if bad == "mismatch_id": value["order_id"] = "different-order-id"
    elif bad == "mismatch_plan": value["snapshot"]["plan_id"] = "advanced"
    elif bad == "bad_minor": value["snapshot"]["amount_minor"] = 1
    elif bad == "bool_revision": value["revision"] = True
    else: value["status"] = "paid"
    monkeypatch.setattr(bridge, "_account_request", Mock(return_value=response(order=value)))
    result = bridge.get_alipay_order(ORDER) if bad == "mismatch_id" else bridge.create_alipay_order("standard", "month", "fixture-request-001")
    assert result["ok"] is False


@pytest.mark.parametrize("value", [{}, {"configured":"true","enabled":True,"provider":"alipay_page_pay"},
    {"configured":True,"enabled":True,"provider":"alipay_personal"}])
def test_payment_capability_never_inferred_or_coerced(value):
    assert _public_alipay_contract(value)["enabled"] is False


@pytest.mark.parametrize("deadline", ["2020-01-01T00:00:00Z", "2099-01-01T00:00:00Z", "not-a-date", "2026-10-07T12:00:00"])
def test_expired_long_lived_or_naive_cashier_time_is_rejected(desktop, monkeypatch, deadline):
    bridge, _ = desktop
    monkeypatch.setattr(bridge, "_account_request", Mock(return_value=response(cashier_expires_at=deadline)))
    assert bridge.create_alipay_order("standard", "month", "fixture-request-001")["ok"] is False


@pytest.mark.parametrize("action", [
    lambda b: b.create_alipay_order("free", "month", "fixture-request-001"),
    lambda b: b.create_alipay_order("standard", "months", "fixture-request-001"),
    lambda b: b.create_alipay_order("standard", "month", "short"),
    lambda b: b.get_alipay_order("../../private-token"),
    lambda b: b.get_alipay_order(ORDER, "true"),
    lambda b: b.query_alipay_order("short"),
])
def test_invalid_frontend_input_is_rejected_before_transport(desktop, monkeypatch, action):
    bridge, _ = desktop
    transport = Mock();monkeypatch.setattr(bridge, "_account_request", transport)
    assert action(bridge)["ok"] is False
    transport.assert_not_called()


@pytest.mark.parametrize("limit", [None, False, True, "5000", 0, -1, 10000000001])
def test_unknown_merchant_limit_never_enables_purchase(limit):
    assert _public_alipay_contract({"configured": True, "enabled": True,
                                  "provider": "alipay_page_pay", "max_order_amount_minor": limit})["enabled"] is False


@pytest.mark.parametrize("enabled", [True, False])
def test_private_account_config_preserves_scoped_sales_and_history_without_public_gate(desktop, monkeypatch, enabled):
    bridge, c = desktop
    before = deepcopy(c.settings)
    catalog = [{"id": "standard", "status": "ready", "purchasable": enabled, "offers": [
        {"period": "month", "months": 1, "amount": "19.80", "credits": 500,
         "currency": "CNY", "status": "ready", "purchasable": enabled}]}]
    transport = Mock(return_value={"ok": True, "provider": "alipay_page_pay", "configured": True,
        "enabled": enabled, "processing_enabled": True, "max_order_amount_minor": 5000,
        "catalog": catalog, "user_id": before["account_user_id"], "test_user_ids": [before["account_user_id"]],
        "sales_mode": "test", "login_token": before["account_login_token"]})
    monkeypatch.setattr(bridge, "_account_request", transport)
    result = bridge.get_alipay_payment_config()
    assert result["ok"] is True and result["configured"] is True
    assert result["enabled"] is enabled and result["processing_enabled"] is True
    assert result["max_order_amount_minor"] == 5000
    assert result["catalog"][0]["offers"][0]["purchasable"] is enabled
    assert not {"user_id", "test_user_ids", "sales_mode", "login_token"} & result.keys()
    assert transport.call_args.args[1] == "/v1/alipay/orders/config"
    assert c.settings == before


@pytest.mark.parametrize("change", ["logout", "session", "user", "service", "revision", "expired"])
def test_private_account_config_late_reply_is_discarded_and_clears_old_cashier(desktop, monkeypatch, change):
    bridge, c = desktop
    bridge._alipay_cashiers = {ORDER: {"context": "old-account"}}
    def transport(*args):
        if change == "logout": bridge._clear_account()
        elif change == "session": c.settings["account_login_token"] = "next"
        elif change == "user": c.settings["account_user_id"] = "next"
        elif change == "service": c.settings["subscription_server_url"] = "https://other.example.invalid"
        elif change == "revision": bridge._account_revision += 1
        else: grant_account(c.settings, bridge.settings_store.base_dir, expires_at=0)
        return {"ok": True, "provider": "alipay_page_pay", "configured": True,
                "enabled": True, "processing_enabled": True, "max_order_amount_minor": 5000, "catalog": []}
    monkeypatch.setattr(bridge, "_account_request", transport)
    result = bridge.get_alipay_payment_config()
    assert result["ok"] is False and result["code"] == "stale_account"
    assert bridge._alipay_cashiers == {}
