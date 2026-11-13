# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""Real isolated CLI and official SDK stdio, using synthetic redemption only."""
from __future__ import annotations

import json

import pytest
from test_agent_application_integration import application_app  # noqa: F401 — pytest 夹具
from test_agent_security import agent_app  # noqa: F401 — pytest 夹具

pytest.importorskip("mcp")

from app.agent_operations import OPERATIONS, public_result
from test_agent_integration import cli, modern_session, tool_value


CARD = "sjbeta_" + "c" * 64


def fake_redemption(app):
    calls = []
    def redeem(card_code, client_request_id=""):
        calls.append((card_code, client_request_id))
        return {"ok": True, "credit_balance": 498, "agent_usage_remaining": 498,
                "unlimited_credits": True, "repeated": False, "redemption_status": "active",
                "beta_access": {"version": 1, "active": True, "unlimited_credits": True, "expires_at": None, "revoked": False},
                "card_code": card_code, "message": "Synthetic echo: " + card_code}
    app.server.agent_control.operations.facade["redeem_beta_card"] = redeem
    return calls


def test_card_registry_is_secret_write_operation_and_public_results_strip_card():
    spec = OPERATIONS["payments.card_redeem"]
    assert spec.method == "redeem_beta_card" and spec.external and not spec.read_only
    assert spec.secret_inputs == ("card_code",)
    assert "card_code" not in public_result({"card_code": CARD, "credit_balance": 498})


def test_real_cli_redeems_from_stdin_with_no_secret_output_or_profile_changes(application_app):
    app = application_app
    calls = fake_redemption(app)
    before = app.store.load()["credit_balance"]
    code, result = cli(app, "application", "call", "payments.card_redeem", "--input-file", "-",
                       stdin=json.dumps({"card_code": CARD, "client_request_id": "fixture-beta-request"}))
    assert code == 0 and result["result"]["unlimited_credits"] is True
    assert result["result"]["credit_balance"] == 498 and CARD not in json.dumps(result)
    assert calls == [(CARD, "fixture-beta-request")]
    assert app.store.load()["credit_balance"] == before
    code, denied = cli(app, "application", "call", "payments.card_redeem", "--input-file", "-",
                       stdin=json.dumps({"card_code": CARD, "force": CARD}))
    assert code == 2 and denied["error"]["code"] == "invalid_argument"
    assert CARD not in json.dumps(denied) and len(calls) == 1


def test_real_official_mcp_full_redeems_and_read_only_refuses_without_secret_echo(application_app):
    app = application_app
    calls = fake_redemption(app)
    async def full(session):
        catalogue = tool_value(await session.call_tool("list_application_operations", {"category": "payments"}))
        item = next(x for x in catalogue["items"] if x["operation"] == "payments.card_redeem")
        assert item["allowed"] is True and item["secret_inputs"] == ["card_code"]
        reply = tool_value(await session.call_tool("call_application_operation", {
            "operation": "payments.card_redeem", "arguments": {"card_code": CARD}}))
        assert reply["unlimited_credits"] and reply["credit_balance"] == 498
        assert CARD not in json.dumps(reply)
    modern_session(app, [], full)
    async def readonly(session):
        catalogue = tool_value(await session.call_tool("list_application_operations", {"category": "payments"}))
        item = next(x for x in catalogue["items"] if x["operation"] == "payments.card_redeem")
        assert item["allowed"] is False
        reply = await session.call_tool("call_application_operation", {
            "operation": "payments.card_redeem", "arguments": {"card_code": CARD}})
        assert reply.is_error and "read_only" in str(reply.content) and CARD not in str(reply.content)
    modern_session(app, ["--read-only"], readonly)
    assert calls == [(CARD, "")]
