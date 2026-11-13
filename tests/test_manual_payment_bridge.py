# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""Manual payment requests bind to signed sessions and cannot grant rights."""
from __future__ import annotations

import base64
import json
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from account_auth_fixtures import grant_account
from app.bridge import Bridge


REQUEST_ID = "0123456789abcdef0123456789abcdef"
TRANSACTION_ID = "2026100712345678901234567890"


class Controller:
    def __init__(self, settings):
        self.settings = settings

    def save_settings(self, fields):
        self.settings.update(fields)


@pytest.fixture
def desktop(tmp_path):
    settings = {"subscription_server_url": "https://pay.example.invalid", "credit_balance": 0,
                "subscription_active": False, "subscription_plan": "free"}
    grant_account(settings, tmp_path)
    controller = Controller(settings)
    bridge = Bridge(controller, Mock(), SimpleNamespace(base_dir=tmp_path), lambda: None, lambda: None)
    return bridge, controller


def payment_request(**fields):
    return {"request_id": REQUEST_ID, "association_code": "SJ-A1B2C3", "status": "pending_payment",
            "revision": 1, "created_at": "2026-10-07T01:00:00Z", "expires_at": "2026-10-08T01:00:00Z",
            "snapshot": {"id": "provider-internal-id", "plan_id": "standard", "period": "month", "months": 1,
                         "amount": "19.80", "credits": 500, "currency": "CNY"}, **fields}


def catalog():
    return [{"id": "standard", "status": "ready", "purchasable": True,
             "offers": [{"period": "month", "months": 1, "amount": "19.80", "credits": 500,
                         "currency": "CNY", "status": "ready", "purchasable": True}]}]


@pytest.mark.parametrize("action,path,fields", [
    (lambda b: b.get_manual_payment_config(), "config", {}),
    (lambda b: b.list_manual_payment_requests(), "list", {"limit": 20}),
    (lambda b: b.create_manual_payment_request("standard", "month", "fixture-request-001"), "create",
     {"plan_id": "standard", "period": "month", "client_request_id": "fixture-request-001"}),
    (lambda b: b.submit_manual_payment_request(REQUEST_ID, TRANSACTION_ID), "submit",
     {"request_id": REQUEST_ID, "transaction_id": TRANSACTION_ID}),
    (lambda b: b.get_manual_payment_request(REQUEST_ID), "status", {"request_id": REQUEST_ID}),
    (lambda b: b.cancel_manual_payment_request(REQUEST_ID), "cancel", {"request_id": REQUEST_ID}),
])
def test_all_payment_routes_use_current_full_session_without_account_or_amount_inputs(desktop, monkeypatch, action, path, fields):
    bridge, controller = desktop
    before = deepcopy(controller.settings)
    response = {"ok": True, "request": payment_request(status="credited"), "requests": [payment_request()],
                "configured": True, "enabled": True, "provider": "alipay_personal", "catalog": catalog(),
                "license_token": "untrusted-do-not-apply", "credit_balance": 999999}
    transport = Mock(return_value=response)
    monkeypatch.setattr(bridge, "_account_request", transport)
    assert action(bridge)["ok"] is True
    base, route, payload = transport.call_args.args
    assert base == "https://pay.example.invalid" and route == "/v1/manual-payments/" + path
    assert payload == {**fields, "login_token": before["account_login_token"],
                       "device_id": __import__("engine.device", fromlist=["device_id"]).device_id(bridge.settings_store.base_dir)}
    assert not {"user_id", "amount", "credits", "plan", "license_token"} & payload.keys()
    assert controller.settings == before, "A request, receipt or ledger status is not a signed entitlement"


@pytest.mark.parametrize("denial", ["logged_out", "forged", "other_user", "management_only"])
def test_manual_payments_require_verified_full_account_before_transport(desktop, monkeypatch, denial):
    bridge, controller = desktop
    if denial == "logged_out":
        controller.settings["account_login_token"] = ""
    elif denial == "forged":
        controller.settings["license_token"] = "untrusted"
    elif denial == "other_user":
        controller.settings["account_user_id"] = "other-user"
    else:
        monkeypatch.setattr(bridge, "_account_context", lambda: ("https://pay.example.invalid", "manage-only", {"token": "manage-only"}))
    transport = Mock()
    monkeypatch.setattr(bridge, "_account_request", transport)
    for action in [lambda: bridge.get_manual_payment_config(), lambda: bridge.list_manual_payment_requests(),
                   lambda: bridge.create_manual_payment_request("standard", "month", "fixture-request-001"),
                   lambda: bridge.submit_manual_payment_request(REQUEST_ID, TRANSACTION_ID),
                   lambda: bridge.get_manual_payment_request(REQUEST_ID), lambda: bridge.cancel_manual_payment_request(REQUEST_ID)]:
        assert action()["ok"] is False
    transport.assert_not_called()


@pytest.mark.parametrize("change", ["logout", "account", "account_user", "service", "revision", "expired"])
def test_old_account_response_cannot_reach_new_context(desktop, monkeypatch, change):
    bridge, controller = desktop

    def transport(*args):
        if change == "logout":
            bridge._clear_account()
        elif change == "account":
            controller.settings["account_login_token"] = "new-session"
        elif change == "account_user":
            controller.settings["account_user_id"] = "other-user"
        elif change == "service":
            controller.settings["subscription_server_url"] = "https://other.example.invalid"
        elif change == "revision":
            bridge._account_revision += 1
        else:
            grant_account(controller.settings, bridge.settings_store.base_dir, expires_at=0)
        return {"ok": True, "request": payment_request(status="credited")}

    monkeypatch.setattr(bridge, "_account_request", transport)
    result = bridge.get_manual_payment_request(REQUEST_ID)
    assert result["ok"] is False and result["code"] == "stale_account"
    assert "request" not in result


@pytest.mark.parametrize("action", [
    lambda b: b.create_manual_payment_request("free", "month", "fixture-request-001"),
    lambda b: b.create_manual_payment_request("standard", "months", "fixture-request-001"),
    lambda b: b.create_manual_payment_request("standard", "month", "short"),
    lambda b: b.create_manual_payment_request("standard", "month", "../../private-secret"),
    lambda b: b.get_manual_payment_request("../../private-secret"),
    lambda b: b.cancel_manual_payment_request("short"),
    lambda b: b.submit_manual_payment_request(REQUEST_ID, "user@example.com"),
    lambda b: b.submit_manual_payment_request(REQUEST_ID, "12345"),
    lambda b: b.submit_manual_payment_request(REQUEST_ID, "1" * 65),
    lambda b: b.submit_manual_payment_request(REQUEST_ID, "交易截图"),
])
def test_invalid_payment_input_never_calls_service(desktop, monkeypatch, action):
    bridge, _ = desktop
    transport = Mock()
    monkeypatch.setattr(bridge, "_account_request", transport)
    assert action(bridge)["ok"] is False
    transport.assert_not_called()


def test_public_manual_contract_is_separate_and_false_by_default(desktop):
    bridge, controller = desktop
    controller.settings["subscription_license_public_key"] = "A" * 43 + "="
    for value in [None, {}, {"configured": "true", "enabled": True, "provider": "alipay_personal"},
                  {"configured": True, "enabled": True, "provider": "afdian"}]:
        bridge._subscription_service_base = "https://pay.example.invalid"
        bridge._subscription_service = {"manual_payments": value}
        manual = bridge.get_subscription_contract()["manual_payments"]
        assert manual["configured"] is False and manual["enabled"] is False and manual["catalog"] == []
    bridge._subscription_service["manual_payments"] = {"configured": True, "enabled": True, "provider": "alipay_personal", "catalog": catalog()}
    contract = bridge.get_subscription_contract()
    assert contract["manual_payments"]["enabled"] is True
    assert contract["checkout_ready"] is False, "Manual readiness does not invent Afdian or model readiness"


@pytest.mark.parametrize("qr,accepted", [
    ("https://external.example.invalid/private-image.png", False),
    ("data:image/svg+xml;base64,PHN2Zz48L3N2Zz4=", False),
    ("data:image/png;base64," + base64.b64encode(b"not-a-png").decode(), False),
    ("data:image/jpeg;base64," + base64.b64encode(b"\xff\xd8\xfffixture").decode(), True),
    ("data:image/png;base64," + base64.b64encode(b"\x89PNG\r\n\x1a\nfixture").decode(), True),
    ("data:image/png;base64," + "A" * 131072, False),
], ids=["remote", "svg", "wrong-magic", "jpeg", "png", "oversized"])
def test_config_exposes_only_bounded_inert_qr_and_public_collection_fields(desktop, monkeypatch, qr, accepted):
    bridge, _ = desktop
    monkeypatch.setattr(bridge, "_account_request", lambda *args: {"ok": True, "configured": True, "enabled": True,
        "provider": "alipay_personal", "catalog": catalog(), "qr_data_url": qr, "instructions": "扫码后填写交易号", "recipient_label": "收款方",
        "admin_token": "do-not-expose", "user_id": "do-not-expose", "license_token": "do-not-expose"})
    result = bridge.get_manual_payment_config()
    assert result["qr_data_url"] == (qr if accepted else "")
    assert not {"admin_token", "user_id", "license_token"} & result.keys()


def test_requests_discard_account_fields_and_wrong_request_responses(desktop, monkeypatch):
    bridge, _ = desktop
    incoming = payment_request(user_id="private-user", transaction_id=TRANSACTION_ID, admin_note="private-note")
    incoming["snapshot"]["provider_secret"] = "private-secret"
    monkeypatch.setattr(bridge, "_account_request", lambda *args: {"ok": True, "request": incoming,
                                                               "login_token": "secret", "license_token": "forged"})
    result = bridge.get_manual_payment_request(REQUEST_ID)
    assert result["ok"] is True
    assert not {"login_token", "license_token"} & result.keys()
    assert not {"user_id", "transaction_id", "admin_note"} & result["request"].keys()
    assert "provider_secret" not in result["request"]["snapshot"]
    incoming["request_id"] = "abcdef0123456789abcdef0123456789"
    assert bridge.get_manual_payment_request(REQUEST_ID)["ok"] is False


@pytest.mark.parametrize("quote", [{"amount": "0"}, {"amount": "NaN"}, {"amount": 19.80},
                                   {"credits": True}, {"currency": "USD"}, {"period": "month", "months": 12}])
def test_malformed_quote_cannot_become_payment_request(desktop, monkeypatch, quote):
    bridge, _ = desktop
    incoming = payment_request()
    incoming["snapshot"].update(quote)
    monkeypatch.setattr(bridge, "_account_request", lambda *args: {"ok": True, "request": incoming})
    assert bridge.create_manual_payment_request("standard", "month", "fixture-request-001")["ok"] is False


def test_actual_http_transport_uses_json_session_device_and_snapshot_without_issuing_rights(desktop):
    bridge, controller = desktop
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            calls.append((self.path, payload, self.headers.get("User-Agent")))
            if self.path.endswith("/config"):
                reply = {"ok": True, "configured": True, "enabled": True, "provider": "alipay_personal", "catalog": catalog(),
                         "recipient_label": "签约收款方", "instructions": "填写交易号"}
            elif self.path.endswith("/list"):
                reply = {"ok": True, "requests": [payment_request()]}
            else:
                reply = {"ok": True, "request": payment_request(status="pending_review" if self.path.endswith("/submit") else "credited" if self.path.endswith("/status") else "pending_payment")}
            body = json.dumps(reply, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = Thread(target=server.serve_forever, daemon=True)
    worker.start()
    controller.settings["subscription_server_url"] = f"http://127.0.0.1:{server.server_address[1]}"
    before = deepcopy(controller.settings)
    try:
        assert bridge.get_manual_payment_config()["recipient_label"] == "签约收款方"
        assert bridge.list_manual_payment_requests()["requests"][0]["request_id"] == REQUEST_ID
        assert bridge.create_manual_payment_request("standard", "month", "fixture-request-001")["request"]["status"] == "pending_payment"
        assert bridge.submit_manual_payment_request(REQUEST_ID, TRANSACTION_ID)["request"]["status"] == "pending_review"
        assert bridge.get_manual_payment_request(REQUEST_ID)["request"]["status"] == "credited"
        assert controller.settings == before
        assert len(calls) == 5 and all(payload["login_token"] == before["account_login_token"] for _, payload, _ in calls)
        assert all(user_agent == "ShengJianDesktop" for _, _, user_agent in calls)
        assert all(not {"user_id", "amount", "credits", "license_token"} & payload.keys() for _, payload, _ in calls)
    finally:
        server.shutdown()
        server.server_close()
        worker.join(2)
