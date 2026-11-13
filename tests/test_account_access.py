# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""First entry requires a verified license; later starts reuse that license."""
import base64
import hashlib
import json
import threading
import time
from types import SimpleNamespace

import pytest

from engine import ed25519
from engine.account_access import account_access
from engine.license import sign_license


def settings_for(payload=None):
    seed = bytes(range(32))
    public = base64.b64encode(ed25519.public_key(seed)).decode()
    claims = {"sub": "user-1", "device_id": "device-1", "exp": 2000, "iat": 900,
              "plan_id": "free", "credit_balance": 0}
    if payload is not None:
        claims.update(payload)
    return {"account_login_token": "opaque-session", "account_user_id": "user-1",
            "subscription_license_public_key": public,
            "license_token": sign_license(claims, signing_key=base64.b64encode(seed).decode(), alg="EdDSA")}


def test_valid_free_account_keeps_local_entry_without_paid_entitlements():
    settings = settings_for()
    result = account_access(settings, device_id="device-1", now=1000)
    assert result == {"required": True, "allowed": True, "expires_at": 2000, "reason": "verified"}
    assert settings_for() == settings
    assert "opaque-session" not in str(result)


def entry_settings(**claims):
    settings = settings_for({'exp': 999})
    payload = {'kind':'account_access', 'sub':'user-1', 'device_id':'device-1', 'exp':3000,
               'session_hash':'sha256:' + hashlib.sha256(b'shengjian-auth-v1:opaque-session').hexdigest(), **claims}
    settings['account_access_token'] = sign_license(payload, signing_key=base64.b64encode(bytes(range(32))).decode(), alg='EdDSA')
    return settings


def test_remembered_login_outlives_short_entitlement_license_without_granting_paid_rights():
    from engine.subscription import status
    settings = entry_settings()
    assert account_access(settings, device_id='device-1', now=1000)['allowed'] is True
    assert status(settings, device_id='device-1')['active'] is False
    assert status(settings, device_id='device-1')['credit_balance'] == 0


@pytest.mark.parametrize('claims', [{'kind':'other'}, {'sub':'other'}, {'device_id':'other'}, {'exp':999}, {'session_hash':'other'}])
def test_remembered_login_proof_is_bound_to_current_session_account_device_and_expiry(claims):
    assert account_access(entry_settings(**claims), device_id='device-1', now=1000)['allowed'] is False


def test_local_logout_cannot_reuse_a_previously_signed_remembered_login():
    settings = entry_settings()
    settings['account_login_token'] = ''
    assert account_access(settings, device_id='device-1', now=1000)['allowed'] is False


@pytest.mark.parametrize("changes", [{"account_login_token": ""}, {"account_user_id": "other"},
    {"subscription_license_public_key": ""}, {"license_token": "forged"},
    {"subscription_license_public_key": "not-a-public-key"}])
def test_edited_local_flags_or_missing_identity_cannot_unlock(changes):
    settings = settings_for()
    settings.update(changes, account_access={"allowed": True}, subscription_active=True)
    assert account_access(settings, device_id="device-1", now=1000)["allowed"] is False


@pytest.mark.parametrize("payload", [{"exp": 1000}, {"device_id": "other"}, {"sub": "other"}, {"device_id": None}])
def test_expired_wrong_account_and_unbound_licenses_cannot_unlock(payload):
    assert account_access(settings_for(payload), device_id="device-1", now=1000)["allowed"] is False


def test_local_hs256_development_license_does_not_unlock_production_entry():
    settings = settings_for()
    settings["license_token"] = sign_license({"sub": "user-1", "device_id": "device-1", "exp": 2000}, signing_key="dev")
    assert account_access(settings, device_id="device-1", now=1000)["allowed"] is False


def test_legacy_numeric_expiry_is_normalized_for_frontend_entry():
    assert account_access(settings_for({"exp": "2000"}), device_id="device-1", now=1000)["expires_at"] == 2000.0


def test_runtime_settings_view_recomputes_access_instead_of_trusting_saved_flags(monkeypatch):
    from app.bridge import _settings_view
    monkeypatch.setattr('engine.device.device_id',lambda _: 'device-1')
    settings = settings_for({'exp':int(time.time())+3600})
    settings['account_access'] = {'allowed':False,'revision':999}
    view = _settings_view(SimpleNamespace(settings=settings),SimpleNamespace(base_dir='fixture'),4)
    assert view['account_access']['allowed'] is True
    assert view['account_access']['revision'] == 4
    settings['license_token'] = 'invalid'
    settings['account_access']['allowed'] = True
    assert _settings_view(SimpleNamespace(settings=settings),SimpleNamespace(base_dir='fixture'),5)['account_access']['allowed'] is False


def test_background_push_relocks_revoked_account_without_exposing_identity(monkeypatch):
    from app.bridge import Bridge
    monkeypatch.setattr('engine.device.device_id',lambda _: 'device-1')
    settings = settings_for({'exp':int(time.time())+3600})
    script = []
    bridge = Bridge.__new__(Bridge)
    bridge._lock = threading.RLock()
    bridge._account_revision = 2
    bridge.controller = SimpleNamespace(settings=settings,task_rows=lambda:[])
    bridge.settings_store = SimpleNamespace(base_dir='fixture')
    bridge._window = lambda:SimpleNamespace(evaluate_js=script.append)
    bridge._tasks = lambda rows:[]
    bridge._summary = lambda rows:{}
    bridge._worker_state = lambda:{}
    bridge.get_window_chrome = lambda:{'enabled':False}
    bridge._launch_context = lambda:{'mode':'manual'}
    bridge.push_state()
    payload = json.loads(script[-1].removeprefix('window.__onState(').removesuffix(');'))
    assert payload['account_access']['allowed'] is True
    settings['account_login_token'] = ''
    bridge._account_revision = 3
    bridge.push_state()
    payload = json.loads(script[-1].removeprefix('window.__onState(').removesuffix(');'))
    assert payload['account_access'] == {'required':True,'allowed':False,'expires_at':None,'reason':'login_required','revision':3}
    assert all(field not in payload for field in ('account_login_token','account_user_id','license_token','settings'))
    assert 'opaque-session' not in script[-1] and 'user-1' not in script[-1]
