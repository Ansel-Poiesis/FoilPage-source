# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""订阅服务端合同本地验收测试：订单幂等、许可证签发、额度账本与完整闭环。"""
from __future__ import annotations

import threading

import pytest

from engine.subscription_server import (
    ContractError,
    MockSubscriptionHTTPServer,
    MockSubscriptionServerApp,
    SubscriptionServerClient,
    run_acceptance,
)


def _running_server(app):
    server = MockSubscriptionHTTPServer(app)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def test_checkout_is_idempotent_by_client_request_id() -> None:
    app = MockSubscriptionServerApp("key")
    first = app.checkout("standard", "req-1")
    second = app.checkout("standard", "req-1")
    assert first["ok"] is True
    assert first["order_id"] == second["order_id"]
    with pytest.raises(ContractError):
        app.checkout("nope", "req-2")


def test_payment_issues_verifiable_license_and_status() -> None:
    from engine.license import verify_license

    app = MockSubscriptionServerApp("key")
    order = app.checkout("advanced", "req-2")
    token = app.mark_paid(order["order_id"])
    payload = verify_license(token, signing_key="key")
    assert payload["plan_id"] == "advanced"
    assert payload["monthly_agent_units"] == 2000
    assert "hosted_agent" in payload["entitlements"]
    assert "export_enhanced" in payload["entitlements"]
    st = app.status(token)
    assert st["active"] is True
    assert st["usage_used"] == 0
    assert st["usage_remaining"] == 2000


def test_usage_consume_idempotent_and_refund() -> None:
    app = MockSubscriptionServerApp("key")
    order = app.checkout("standard", "req-3")
    token = app.mark_paid(order["order_id"])
    assert app.report_usage(token, "u1", 2, "consume")["usage_used"] == 2
    assert app.report_usage(token, "u1", 2, "consume")["usage_used"] == 2  # 幂等
    assert app.report_usage(token, "u2", 5, "consume")["usage_used"] == 7
    assert app.report_usage(token, "u1", 0, "refund")["usage_used"] == 5
    assert app.report_usage(token, "u1", 0, "refund")["usage_used"] == 5  # 重复回滚无效


def test_usage_rejects_quota_exceeded_and_bad_action() -> None:
    app = MockSubscriptionServerApp("key")
    order = app.checkout("standard", "req-4")
    token = app.mark_paid(order["order_id"])
    try:
        app.report_usage(token, "big", 1001, "consume")
        raise AssertionError("应拒绝超额扣减")
    except ContractError as exc:
        assert "额度不足" in str(exc)
    try:
        app.report_usage(token, "bad", 1, "explode")
        raise AssertionError("应拒绝未知 action")
    except ContractError as exc:
        assert "consume" in str(exc)


def test_http_client_full_loop_on_ephemeral_port() -> None:
    app = MockSubscriptionServerApp("key")
    server, thread = _running_server(app)
    try:
        client = SubscriptionServerClient(server.base_url())
        health = client._call("GET", "/v1/health")
        assert health.get("ok") is True
        checkout = client.checkout("advanced", "http-1")
        assert checkout["ok"] is True
        paid = client.simulate_payment(checkout["order_id"])
        assert paid.get("ok") is True
        token = paid["license_token"]
        st = client.status(token)
        assert st["plan_id"] == "advanced"
        assert st["monthly_agent_units"] == 2000
        used = client.report_usage(token, "http-u1", 3, "consume")
        assert used["usage_used"] == 3
        refunded = client.report_usage(token, "http-u1", 0, "refund")
        assert refunded["usage_used"] == 0
        bad = client.status("garbage")
        assert bad.get("ok") is False
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_run_acceptance_returns_evidence() -> None:
    evidence = run_acceptance()
    assert evidence["overall"] == "ok"
    assert evidence["checkout"]["ok"] is True
    assert evidence["checkout_idempotent"]["same_order"] is True
    assert evidence["payment"]["has_token"] is True
    assert evidence["license_verified"]["plan_id"] == "standard"
    assert evidence["license_verified"]["monthly_agent_units"] == 500
    assert evidence["license_verified"]["entitlements_match"] is True
    assert evidence["license_verified"]["expires_in_future"] is True
    assert evidence["usage_consume"]["used"] == 2
    assert evidence["usage_idempotent"]["used_unchanged"] is True
    assert evidence["usage_refund"]["rolled_back"] is True
