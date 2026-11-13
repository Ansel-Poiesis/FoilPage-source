# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""Desktop bridge keeps one-time capabilities private and never installs stale grants."""
import base64
import hashlib
import time
from types import SimpleNamespace
from unittest.mock import Mock

from app.bridge import Bridge
from engine import ed25519
from engine.device import device_id
from engine.license import sign_license

SEED = bytes(range(32))
PRIVATE = base64.b64encode(SEED).decode()
PUBLIC = base64.b64encode(ed25519.public_key(SEED)).decode()
POLL = 'poll-' + 'a' * 64
REQUEST = 'desktop-' + 'b' * 64
URL = 'https://foilpage.early-snipe-7202.chatgpt.site/app.html#desktop-login=' + REQUEST


class Controller:
    def __init__(self):
        self.settings = {'subscription_server_url':'https://pay.example.com', 'subscription_license_public_key':PUBLIC,
                         'account_login_token':'old-session', 'account_user_id':'original-user', 'credit_balance':498}

    def save_settings(self, changes):
        self.settings.update(changes)


def setup_bridge(tmp_path, monkeypatch, *, malicious=False):
    controller = Controller()
    bridge = Bridge(controller, Mock(), SimpleNamespace(base_dir=tmp_path), lambda:None, lambda:None,
                    local_api_provider=lambda:SimpleNamespace(running=True,address=('127.0.0.1',18765)))
    opened = []
    monkeypatch.setattr('webbrowser.open', lambda url: opened.append(url) or True)
    start = {'ok':True, 'session_id':POLL, 'request_id':REQUEST, 'automatic':True, 'authorize_url':URL}
    if malicious:
        start['authorize_url'] = 'https://evil.invalid/collect#' + POLL
    calls = []

    def request(_base, path, _payload):
        calls.append(path)
        if path.endswith('/start'):
            return start
        if path.endswith('/cancel'):
            return {'ok':True}
        return {'ok':True,'status':'pending'}

    bridge._account_request = request
    return bridge, controller, opened, calls


def result_for(bridge, *, login='login-' + 'c' * 64):
    device = device_id(bridge.settings_store.base_dir)
    license_token = sign_license({'sub':'original-user','device_id':device,'exp':time.time()+900,'plan_id':'standard','credit_balance':498},signing_key=PRIVATE,alg='EdDSA')
    account_token = sign_license({'kind':'account_access','sub':'original-user','device_id':device,'exp':time.time()+86400,
        'session_hash':'sha256:' + hashlib.sha256(('shengjian-auth-v1:' + login).encode()).hexdigest()},signing_key=PRIVATE,alg='EdDSA')
    return {'ok':True,'status':'ok','login_token':login,'license_token':license_token,'account_access_token':account_token,
            'account':{'user_id':'original-user','display_name':'Original','credit_balance':498,'active':True}}


def test_desktop_keeps_private_capability_and_opens_only_fixed_public_request(tmp_path, monkeypatch):
    bridge, controller, opened, _ = setup_bridge(tmp_path, monkeypatch)
    result = bridge.web_login_start()
    assert result == {'ok':True,'status':'pending','expires_in_seconds':600}
    assert controller.settings['account_pending_login_token'] == POLL
    assert opened == [URL + '&port=18765'] and POLL not in URL
    assert POLL not in str(result)
    proof = bridge._web_login_proof(REQUEST)
    assert proof['ok'] and len(proof['approval_token']) == 64
    assert proof['approval_token'] not in str(result) + str(opened) + str(controller.settings)


def test_local_proof_rejects_other_requests_expiry_cancel_and_account_change(tmp_path, monkeypatch):
    bridge, controller, _, _ = setup_bridge(tmp_path, monkeypatch)
    bridge.web_login_start()
    assert not bridge._web_login_proof('desktop-' + 'c' * 64)['ok']
    bridge._web_login_pending['expires_at'] = time.monotonic() - 1
    assert not bridge._web_login_proof(REQUEST)['ok']
    bridge.web_login_start()
    controller.settings['account_login_token'] = 'changed'
    assert not bridge._web_login_proof(REQUEST)['ok']
    bridge.web_login_start()
    bridge.web_login_cancel()
    assert not bridge._web_login_proof(REQUEST)['ok']


def test_malicious_authorization_url_is_never_opened_or_saved(tmp_path, monkeypatch):
    bridge, controller, opened, _ = setup_bridge(tmp_path, monkeypatch, malicious=True)
    assert bridge.web_login_start()['ok'] is False
    assert not opened and not controller.settings.get('account_pending_login_token')


def test_confirmed_grant_installs_matching_signed_login_and_paid_state_without_exposing_tokens(tmp_path, monkeypatch):
    bridge, controller, _, _ = setup_bridge(tmp_path, monkeypatch)
    bridge.web_login_start()
    value = result_for(bridge)
    bridge._account_request = lambda *args: value
    result = bridge.web_login_poll()
    assert result == {'ok':True,'status':'ok'}
    assert controller.settings['account_login_token'] == value['login_token']
    assert controller.settings['account_access_token'] == value['account_access_token']
    assert controller.settings['credit_balance'] == 498
    assert bridge.web_login_cancel()['ok'] is True
    assert controller.settings['account_login_token'] == value['login_token']


def test_cancelled_held_poll_cannot_replace_the_existing_login(tmp_path, monkeypatch):
    bridge, controller, _, _ = setup_bridge(tmp_path, monkeypatch)
    bridge.web_login_start()
    value = result_for(bridge)

    def request(_base, path, _payload):
        if path.endswith('/poll'):
            bridge.web_login_cancel()
            return value
        return {'ok':True}

    bridge._account_request = request
    assert bridge.web_login_poll()['status'] == 'invalid'
    assert controller.settings['account_login_token'] == 'old-session'
    assert controller.settings['credit_balance'] == 498


def test_background_refresh_preserves_pending_web_authorization_for_the_same_session(tmp_path, monkeypatch):
    bridge, controller, _, _ = setup_bridge(tmp_path, monkeypatch)
    bridge.web_login_start()
    value = result_for(bridge, login='old-session')
    assert bridge._apply_remote_account(value, expected_session='old-session')['ok'] is True
    assert controller.settings['account_pending_login_token'] == POLL
    assert bridge._web_login_current(bridge._web_login_pending)


def test_wrong_session_proof_cannot_install_new_account_or_entitlements(tmp_path, monkeypatch):
    bridge, controller, _, _ = setup_bridge(tmp_path, monkeypatch)
    bridge.web_login_start()
    value = result_for(bridge)
    value['login_token'] = 'another-session'
    bridge._account_request = lambda *args: value
    assert bridge.web_login_poll()['status'] == 'bad_license'
    assert controller.settings['account_login_token'] == 'old-session'
    assert controller.settings['credit_balance'] == 498
