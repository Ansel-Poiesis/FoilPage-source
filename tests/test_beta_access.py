# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""Signed beta grants on test-owned profiles; no service or paid calls."""
from __future__ import annotations

import base64
import copy
from datetime import datetime, timezone, timedelta
import io
import json
import time
from unittest import mock

import pytest

from engine import ed25519
from engine.device import device_id
from engine.license import signed_beta_access, sign_license
from engine.subscription import status
from test_bridge_subscription_contract import _redeem_bridge


SEED = bytes(range(32))  # Public, synthetic fixture material only.
SEED64 = base64.b64encode(SEED).decode()
PUBLIC = base64.b64encode(ed25519.public_key(SEED)).decode()
CARD = "sjbeta_" + "a" * 64
NOW = datetime(2026, 10, 9, 12, tzinfo=timezone.utc)


def grant(*, active=True, expires=None, revoked=False):
    return {"version": 1, "active": active, "unlimited_credits": active,
            "expires_at": expires, "revoked": revoked}


def fixture_account(tmp_path, *, now=None, beta=None, **claims_patch):
    current = NOW.timestamp() if now is None else now
    claims = {"sub": "beta-user", "device_id": device_id(str(tmp_path)), "iat": current,
              "exp": current + 900, "plan_id": "standard", "credit_balance": 498, "max_concurrency": 2,
              "software_access": {"version": 1, "trial_started_at": datetime.fromtimestamp(current-30*86400, timezone.utc).isoformat(),
                  "trial_expires_at": datetime.fromtimestamp(current-15*86400, timezone.utc).isoformat(),
                  "unlocked": True, "review": False}, "beta_access": grant() if beta is None else beta}
    claims.update(claims_patch)
    settings = {"account_user_id": "beta-user", "account_login_token": "synthetic-beta-session",
                "subscription_license_public_key": PUBLIC, "license_token": sign_license(claims, signing_key=SEED64, alg="EdDSA"),
                "subscription_plan": "standard", "subscription_active": True, "credit_balance": 498,
                "subscription_expires_at": datetime.fromtimestamp(current+86400, timezone.utc).isoformat(),
                "subscription_server_url": "https://fixture.invalid"}
    return settings, claims


def test_signed_beta_preserves_numeric_credits_and_concurrency(tmp_path):
    settings, _ = fixture_account(tmp_path)
    before = copy.deepcopy(settings)
    value = status(settings, now=NOW, device_id=device_id(str(tmp_path)))
    assert value["unlimited_credits"] is value["beta_access"]["active"] is True
    assert value["credit_balance"] == value["agent_usage_remaining"] == 498
    assert type(value["credit_balance"]) is int and value["max_concurrency"] == 2
    assert settings == before
    json.dumps(value, allow_nan=False)


@pytest.mark.parametrize("balance", [0, 498])
def test_beta_can_use_cloud_without_paid_plan_or_current_subscription(tmp_path, balance):
    settings, _ = fixture_account(tmp_path, plan_id="free", credit_balance=balance, max_concurrency=1)
    settings.update(subscription_active=False, subscription_expires_at="2020-01-01T00:00:00+00:00")
    value = status(settings, now=NOW, device_id=device_id(str(tmp_path)))
    assert value["active"] is False and value["plan_id"] == "free"
    assert value["unlimited_credits"] is True and value["credit_balance"] == balance
    assert value["agent_usage_remaining"] == balance and value["max_concurrency"] == 1


@pytest.mark.parametrize("change", ["wrong_user", "wrong_device", "missing_user", "missing_session", "expired_license", "tampered"])
def test_beta_needs_verified_current_account_device_and_live_certificate(tmp_path, change):
    settings, claims = fixture_account(tmp_path)
    if change == "wrong_user": settings["account_user_id"] = "another-user"
    elif change == "wrong_device": claims["device_id"] = "another-device"
    elif change == "missing_user": settings["account_user_id"] = ""
    elif change == "missing_session": settings["account_login_token"] = ""
    elif change == "expired_license": claims["exp"] = NOW.timestamp()
    if change == "tampered": settings["license_token"] = settings["license_token"][:-4] + "aaaa"
    elif change in {"wrong_device", "expired_license"}:
        settings["license_token"] = sign_license(claims, signing_key=SEED64, alg="EdDSA")
    assert status(settings, now=NOW, device_id=device_id(str(tmp_path)))["unlimited_credits"] is False


def test_unsigned_settings_and_unsigned_top_level_claim_cannot_grant_beta(tmp_path):
    settings, claims = fixture_account(tmp_path, beta=grant(active=False))
    claims["unlimited_credits"] = True
    settings.update(unlimited_credits=True, beta_access=grant())
    settings["license_token"] = sign_license(claims, signing_key=SEED64, alg="EdDSA")
    assert status(settings, now=NOW, device_id=device_id(str(tmp_path)))["unlimited_credits"] is False
    settings["subscription_license_public_key"] = ""
    assert status(settings, now=NOW, device_id=device_id(str(tmp_path)))["unlimited_credits"] is False


@pytest.mark.parametrize("beta", [grant(revoked=True), grant(active=False), grant(expires=NOW.isoformat()),
    grant(expires=(NOW-timedelta(seconds=1)).isoformat()), {**grant(), "version": True}, {**grant(), "active": 1},
    {**grant(), "unlimited_credits": "true"}, {**grant(), "revoked": 0}, {**grant(), "expires_at": "2026-10-10"},
    {**grant(), "expires_at": 9999999999}, {**grant(), "extra": True}, {**grant(), "unlimited_credits": False}, {}])
def test_revoked_expired_malformed_and_inconsistent_signed_grants_fail_closed(beta):
    value = signed_beta_access({"beta_access": beta}, now=NOW.timestamp())
    assert value["active"] is value["unlimited_credits"] is False
    json.dumps(value, allow_nan=False)


def test_review_pause_remains_stronger_than_signed_beta(tmp_path):
    settings, _ = fixture_account(tmp_path, order_review={"paused": True, "count": 1})
    value = status(settings, now=NOW, device_id=device_id(str(tmp_path)))
    assert value["unlimited_credits"] is False and value["order_review"]["paused"] is True
    assert value["credit_balance"] == 498 and value["agent_usage_remaining"] == 0


@pytest.mark.parametrize("bad_balance", [True, "498", float("inf"), -1])
def test_malformed_numeric_credit_cannot_leave_a_beta_grant_enabled(tmp_path, bad_balance):
    settings, _ = fixture_account(tmp_path, credit_balance=bad_balance)
    value = status(settings, now=NOW, device_id=device_id(str(tmp_path)))
    assert value["unlimited_credits"] is False
    json.dumps(value, allow_nan=False)


class Response:
    def __init__(self, value): self.raw = json.dumps(value).encode()
    def read(self, bound): return self.raw[:bound]
    def __enter__(self): return self
    def __exit__(self, *_a): return False


def wire_response(settings, claims, *, repeated=False, status_value="active"):
    return {"ok": True, "status": status_value, "repeated": repeated,
            "license_token": sign_license(claims, signing_key=SEED64, alg="EdDSA"),
            "account": {"user_id": claims["sub"], "active": True,
                        "expires_at": settings["subscription_expires_at"], "credit_balance": 999999,
                        "beta_access": grant(), "display_name": "Test account"}}


def install_opener(monkeypatch, response, *, on_open=None):
    import urllib.request
    calls = []
    class Opener:
        def open(self, request, timeout):
            calls.append((request, timeout))
            if on_open: on_open()
            return Response(response)
    def build(handler):
        assert issubclass(handler, urllib.request.HTTPRedirectHandler)
        assert handler().redirect_request(None, None, 302, None, None, "https://elsewhere.invalid") is None
        return Opener()
    monkeypatch.setattr(urllib.request, "build_opener", build)
    return calls


def test_bridge_card_redeem_applies_only_signed_balance_and_returns_unified_state(tmp_path, monkeypatch):
    settings, claims = fixture_account(tmp_path, now=time.time())
    bridge, controller = _redeem_bridge(tmp_path, settings)
    calls = install_opener(monkeypatch, wire_response(settings, claims, repeated=True))
    result = bridge.redeem_beta_card(CARD, "fixture-request-id")
    assert result["ok"] and result["unlimited_credits"] and result["repeated"]
    assert result["redemption_status"] == "active" and result["credit_balance"] == 498
    assert controller.settings["credit_balance"] == 498
    assert "beta_access" not in controller.settings and "unlimited_credits" not in controller.settings
    body = json.loads(calls[0][0].data)
    assert set(body) == {"login_token", "device_id", "card_code", "client_request_id"}
    assert body["card_code"] == CARD and calls[0][0].full_url == "https://fixture.invalid/v1/account/beta/redeem"
    assert calls[0][1] == 15 and CARD not in json.dumps(result)


@pytest.mark.parametrize("change", ["unsigned_beta", "other_user", "other_device", "bad_token", "missing_beta", "bad_balance"])
def test_bridge_bad_certificate_or_unsigned_beta_never_updates_existing_settings(tmp_path, monkeypatch, change):
    settings, claims = fixture_account(tmp_path, now=time.time())
    before = copy.deepcopy(settings)
    if change == "unsigned_beta": claims["beta_access"] = grant(active=False)
    elif change == "other_user": claims["sub"] = "other-user"
    elif change == "other_device": claims["device_id"] = "other-device"
    elif change == "missing_beta": claims.pop("beta_access")
    elif change == "bad_balance": claims["credit_balance"] = True
    response = wire_response(settings, claims)
    if change == "bad_token": response["license_token"] = "forged-beta-license"
    install_opener(monkeypatch, response)
    bridge, controller = _redeem_bridge(tmp_path, settings)
    result = bridge.redeem_beta_card(CARD)
    assert result["ok"] is False and controller.settings == before
    assert CARD not in json.dumps(result)


@pytest.mark.parametrize("change", ["account_user_id", "account_login_token", "subscription_server_url", "account_revision"])
def test_bridge_late_redemption_cannot_overwrite_switched_account_or_service(tmp_path, monkeypatch, change):
    settings, claims = fixture_account(tmp_path, now=time.time())
    bridge, controller = _redeem_bridge(tmp_path, settings)
    after = {}
    def switch():
        if change == "account_revision": bridge._account_revision += 1
        else: controller.settings[change] = "switched"
        after.update(copy.deepcopy(controller.settings))
    install_opener(monkeypatch, wire_response(settings, claims), on_open=switch)
    assert bridge.redeem_beta_card(CARD)["ok"] is False
    assert controller.settings == after


@pytest.mark.parametrize("bad_card", ["", "sjbeta_bad", 12, "sjbeta_"+"A"*64])
def test_bridge_validates_card_before_any_network(tmp_path, monkeypatch, bad_card):
    import urllib.request
    monkeypatch.setattr(urllib.request, "build_opener", lambda *_a: pytest.fail("invalid card sent"))
    bridge, _ = _redeem_bridge(tmp_path, {})
    assert bridge.redeem_beta_card(bad_card)["code"] == "invalid_beta_card"


@pytest.mark.parametrize("error_field", ["status", "code"])
def test_bridge_http_failure_does_not_echo_card_or_exception(tmp_path, monkeypatch, error_field):
    import urllib.request
    settings, _ = fixture_account(tmp_path, now=time.time())
    bridge, controller = _redeem_bridge(tmp_path, settings); before = copy.deepcopy(settings)
    def fail(*_a, **_k):
        raise urllib.error.HTTPError("https://fixture.invalid", 409, CARD, {}, io.BytesIO(json.dumps({error_field:"beta_already_redeemed", "error":CARD}).encode()))
    monkeypatch.setattr(urllib.request, "build_opener", lambda *_a: mock.Mock(open=fail))
    result = bridge.redeem_beta_card(CARD)
    assert result["ok"] is False and result["code"] == "beta_already_redeemed"
    assert CARD not in json.dumps(result) and controller.settings == before


@pytest.mark.parametrize("base", ["http://fixture.invalid", "https://user:password@fixture.invalid", "https://fixture.invalid?token=secret",
                                 "https://fixture.invalid#fragment", "https://fixture.invalid/private"])
def test_card_redeem_never_sends_secrets_to_unsafe_service_or_redirect(tmp_path, monkeypatch, base):
    import urllib.request
    settings, _ = fixture_account(tmp_path, now=time.time()); settings["subscription_server_url"] = base
    bridge, _ = _redeem_bridge(tmp_path, settings)
    monkeypatch.setattr(urllib.request, "build_opener", lambda *_a: pytest.fail("unsafe service called"))
    assert bridge.redeem_beta_card(CARD)["code"] == "invalid_service"


def test_card_redeem_rejects_oversized_response_without_settings_changes(tmp_path, monkeypatch):
    settings, _ = fixture_account(tmp_path, now=time.time()); before = copy.deepcopy(settings)
    bridge, controller = _redeem_bridge(tmp_path, settings)
    install_opener(monkeypatch, {"ok": True, "status": "active", "padding": "x"*70000})
    result = bridge.redeem_beta_card(CARD)
    assert result["ok"] is False and CARD not in json.dumps(result) and controller.settings == before


@pytest.mark.parametrize("kind", ["revoked", "expired"])
def test_signed_revoked_or_expired_refresh_removes_beta_but_keeps_original_balance(tmp_path, kind):
    settings, claims = fixture_account(tmp_path, now=time.time())
    bridge, controller = _redeem_bridge(tmp_path, settings)
    claims["beta_access"] = grant(active=False, revoked=kind=="revoked",
                                 expires=datetime.fromtimestamp(time.time()-1, timezone.utc).isoformat() if kind=="expired" else None)
    result = bridge._apply_remote_account(wire_response(settings, claims, status_value=kind),
        expected_session=settings["account_login_token"], expected_user_id=settings["account_user_id"])
    assert result["ok"] is True
    assert bridge.get_subscription_status()["unlimited_credits"] is False
    assert controller.settings["credit_balance"] == 498


def test_hosted_beta_charge_keeps_stage_protocol_and_no_local_credit_deduction(monkeypatch):
    from engine.agent_client import AgentStage
    settings = {"agent_billing_mode": "hosted", "subscription_server_url": "https://fixture.invalid",
                "_agent_device_id": "fixture-device", "account_login_token": "fixture-session", "credit_balance": 498}
    calls=[]
    def post(self, action, payload):
        calls.append(action)
        if action == "begin":
            return {"ok": True, "status": "pending", "request_id": "fixture-request", "receipt": "fixture-receipt",
                    "units": 0, "charge": "beta", "model": "fixture-model"}
        if action == "chat": return {"ok": True, "reply": "Synthetic reply"}
        return {"ok": True, "status": "committed"}
    monkeypatch.setattr(AgentStage, "_post", post)
    with AgentStage(settings, "body", "Synthetic source", "fixture-operation") as stage:
        assert stage.chat([], False, 0.2) == "Synthetic reply"
    assert calls == ["begin", "chat", "commit"]
    assert stage.charge == "beta" and stage.metadata()["body_usage_units"] == 0
    assert settings["credit_balance"] == 498
