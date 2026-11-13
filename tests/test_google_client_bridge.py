# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""Client compatibility with private Google polling and explicit linking."""
import io
import base64
import json
import threading
import time
import urllib.error
from types import SimpleNamespace

import pytest

from app.bridge import Bridge


def client(monkeypatch, *, logged_in=False):
    bridge = Bridge.__new__(Bridge)
    bridge._lock = threading.RLock()
    bridge._account_revision = 0
    bridge._profile_revision = 0
    bridge._account_management = None
    bridge.controller = SimpleNamespace(settings={"subscription_server_url": "https://service.example.test",
        "account_login_token": "full-fixture" if logged_in else "", "account_pending_login_token": ""})
    bridge.controller.save_settings = lambda changes: bridge.controller.settings.update(changes)
    bridge.settings_store = SimpleNamespace(base_dir="fixture")
    monkeypatch.setattr("engine.device.device_id", lambda _: "device-fixture")
    opened = []
    monkeypatch.setattr("webbrowser.open", lambda url: opened.append(url) or True)
    return bridge, opened


def reply(monkeypatch, value):
    requests = []
    class Response(io.BytesIO):
        pass
    def request(req, timeout):
        requests.append((req.full_url, json.loads(req.data)))
        return Response(json.dumps(value).encode())
    monkeypatch.setattr("urllib.request.urlopen", request)
    return requests


def test_start_opens_only_google_and_stores_private_poll_capability(monkeypatch):
    bridge, opened = client(monkeypatch)
    requests = reply(monkeypatch, {"ok": True, "session_id": "private-poll-fixture",
        "authorize_url": "https://accounts.google.com/o/oauth2/v2/auth?state=public-state-fixture"})
    result = bridge.google_login_start()
    assert result["ok"] is True
    assert bridge.controller.settings["account_pending_login_token"] == "private-poll-fixture"
    assert bridge.controller.settings["account_login_token"] == ""
    assert requests[0][1] == {"device_id": "device-fixture", "intent": "login", "login_token": ""}
    assert opened == [result["auth_url"]]


def test_bind_passes_current_full_session_and_requires_login(monkeypatch):
    bridge, _ = client(monkeypatch)
    assert bridge.google_login_start("bind")["ok"] is False
    bridge, _ = client(monkeypatch, logged_in=True)
    requests = reply(monkeypatch, {"ok": True, "session_id": "bind-fixture", "authorize_url": "https://accounts.google.com/o/oauth2/v2/auth"})
    assert bridge.google_login_start("bind")["ok"] is True
    assert requests[0][1]["intent"] == "bind"
    assert requests[0][1]["login_token"] == "full-fixture"


@pytest.mark.parametrize("url", ["https://accounts.google.com.evil.test/auth", "http://accounts.google.com/auth", "https://evil.test/auth"])
def test_invalid_provider_url_does_not_open_or_save_a_challenge(monkeypatch, url):
    bridge, opened = client(monkeypatch)
    reply(monkeypatch, {"ok": True, "session_id": "private-fixture", "authorize_url": url})
    assert bridge.google_login_start()["ok"] is False
    assert not opened and not bridge.controller.settings["account_pending_login_token"]


def test_google_poll_is_bound_to_private_session_and_device(monkeypatch):
    bridge, _ = client(monkeypatch)
    bridge.controller.settings["account_pending_login_token"] = "private-fixture"
    requests = reply(monkeypatch, {"ok": True, "status": "pending"})
    assert bridge.wechat_login_poll()["status"] == "pending"
    assert requests[0][1]["session_id"] == "private-fixture"
    assert requests[0][1]["device_id"] == "device-fixture"
    assert not bridge.controller.settings["account_login_token"]


def test_cancel_invalidates_pending_google_only_and_keeps_full_login(monkeypatch):
    bridge, _ = client(monkeypatch, logged_in=True)
    reply(monkeypatch, {"ok":True,"session_id":"private-google","authorize_url":"https://accounts.google.com/o/oauth2/v2/auth"})
    assert bridge.google_login_start("bind")["ok"] is True
    requests = reply(monkeypatch, {"ok":True,"status":"cancelled"})
    assert bridge.google_login_cancel()["ok"] is True
    assert requests == [("https://service.example.test/v1/auth/google/cancel", {"session_id":"private-google","device_id":"device-fixture"})]
    assert bridge.controller.settings["account_login_token"] == "full-fixture"
    assert not bridge.controller.settings["account_pending_login_token"]


def test_cancel_does_not_clear_a_newer_pending_provider(monkeypatch):
    bridge, _ = client(monkeypatch)
    reply(monkeypatch, {"ok":True,"session_id":"private-old","authorize_url":"https://accounts.google.com/o/oauth2/v2/auth"})
    assert bridge.google_login_start()["ok"] is True
    bridge.controller.settings["account_pending_login_token"] = "newer-provider"
    calls = reply(monkeypatch, {"ok":True})
    assert bridge.google_login_cancel()["ok"] is True
    assert calls[0][0].endswith('/v1/auth/google/cancel')
    assert calls[0][1]['session_id'] == 'private-old'
    assert bridge.controller.settings["account_pending_login_token"] == "newer-provider"


def signed_result(bridge):
    from engine.ed25519 import public_key
    from engine.license import sign_license
    seed = bytes(range(32))
    bridge.controller.settings['subscription_license_public_key'] = base64.b64encode(public_key(seed)).decode()
    return {'ok':True, 'login_token':'full-new', 'account':{'user_id':'user-new'},
        'license_token':sign_license({'sub':'user-new','device_id':'device-fixture','exp':int(time.time())+3600},
            signing_key=base64.b64encode(seed).decode(),alg='EdDSA')}


def begin_google(monkeypatch, bridge):
    reply(monkeypatch, {'ok':True,'session_id':'private-google','authorize_url':'https://accounts.google.com/o/oauth2/v2/auth'})
    assert bridge.google_login_start()['ok'] is True


def test_cancel_blocks_held_poll_before_remote_request_returns(monkeypatch):
    bridge, _ = client(monkeypatch)
    begin_google(monkeypatch, bridge)
    revision = bridge._account_revision
    result = signed_result(bridge)
    def cancel(base, path, payload):
        assert not bridge.controller.settings['account_pending_login_token']
        applied = bridge._apply_remote_account(result, expected_pending='private-google', expected_revision=revision)
        assert applied['ok'] is False
        return {'ok':True}
    monkeypatch.setattr(bridge,'_account_request',cancel)
    assert bridge.google_login_cancel()['ok'] is True
    assert not bridge.controller.settings['account_login_token']


def test_consumed_google_session_is_not_revoked_by_later_cleanup(monkeypatch):
    bridge, _ = client(monkeypatch)
    begin_google(monkeypatch,bridge)
    assert bridge._apply_remote_account(signed_result(bridge),expected_pending='private-google')['ok'] is True
    calls = reply(monkeypatch,{'ok':True})
    assert bridge.google_login_cancel()['ok'] is True
    assert not calls and bridge.controller.settings['account_login_token'] == 'full-new'


def test_logout_revokes_unconsumed_google_challenge_after_clearing_local_login(monkeypatch):
    bridge, _ = client(monkeypatch,logged_in=True)
    begin_google(monkeypatch,bridge)
    calls = reply(monkeypatch,{'ok':True})
    assert bridge.account_logout()['ok'] is True
    assert any(url.endswith('/v1/auth/google/cancel') and body['session_id']=='private-google' for url,body in calls)
    assert not bridge.controller.settings['account_login_token']
    assert not bridge.controller.settings['account_pending_login_token']


def test_start_response_after_logout_cancels_remote_challenge(monkeypatch):
    bridge, opened = client(monkeypatch)
    calls = []
    def request(req, timeout):
        calls.append(req.full_url)
        if req.full_url.endswith('/start'):
            bridge._clear_account()
            body = {'ok':True,'session_id':'private-google','authorize_url':'https://accounts.google.com/o/oauth2/v2/auth'}
        else:
            body = {'ok':True}
        return io.BytesIO(json.dumps(body).encode())
    monkeypatch.setattr('urllib.request.urlopen',request)
    assert bridge.google_login_start()['ok'] is False
    assert calls[-1].endswith('/v1/auth/google/cancel')
    assert not opened and not bridge.controller.settings['account_pending_login_token']


def test_failed_cancel_retains_remote_retry_without_reopening_local_pending(monkeypatch):
    bridge, _ = client(monkeypatch)
    begin_google(monkeypatch,bridge)
    monkeypatch.setattr(bridge,'_account_request',lambda *args: {'ok':False})
    assert bridge.google_login_cancel()['ok'] is False
    assert not bridge.controller.settings['account_pending_login_token']
    assert bridge._google_login_pending['session_id'] == 'private-google'
    monkeypatch.setattr(bridge,'_account_request',lambda *args: {'ok':True})
    assert bridge.google_login_cancel()['ok'] is True
    assert bridge._google_login_pending is None


@pytest.mark.parametrize("status", ["cancelled", "account_link_required", "denied"])
def test_terminal_oauth_http_errors_are_not_polled_as_unreachable(monkeypatch, status):
    bridge, _ = client(monkeypatch)
    bridge.controller.settings["account_pending_login_token"] = "private-fixture"
    def request(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 409, "Conflict", {}, io.BytesIO(json.dumps({"ok":False,"status":status}).encode()))
    monkeypatch.setattr("urllib.request.urlopen", request)
    assert bridge.wechat_login_poll()["status"] == status
