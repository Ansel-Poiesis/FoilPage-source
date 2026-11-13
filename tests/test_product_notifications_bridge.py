# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""Notification transport cannot bypass account or license boundaries."""
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace
from unittest.mock import Mock, patch
import pytest
from account_auth_fixtures import grant_account
from app.bridge import Bridge
from app.controller import AppController
from engine.product_events import notice_subject
from engine.settings import SettingsStore


@pytest.fixture
def desktop(tmp_path):
    store = SettingsStore(base_dir=tmp_path / 'profile')
    grant_account(store, store.base_dir)
    controller = AppController(store)
    notice = Mock(return_value=True)
    bridge = Bridge(controller, Mock(), store, window_provider=lambda: None, on_quit=lambda: None, notifier=notice)
    yield bridge, controller, notice
    controller.close()


def batch():
    now = datetime.now(timezone.utc)
    return {'ok': True, 'cursor': 1, 'events': [{'id': 1, 'event_id': 'a' * 64,
        'kind': 'credits_granted', 'title': '积分已到账', 'message': '新增 500 积分',
        'created_at': now.isoformat(), 'expires_at': (now + timedelta(days=1)).isoformat(),
        'data': {'credits_delta': 500}, 'untrusted_license': 'never-apply-this'}]}


def inline_threads(target, **kwargs):
    return SimpleNamespace(start=target)


def test_signed_account_notice_refreshes_license_once_and_never_grants_locally(desktop):
    bridge, controller, notice = desktop
    bridge._announcement_sync_at = __import__('time').monotonic()
    before = dict(controller.settings)
    with patch.object(bridge, '_product_request', return_value=batch()) as request, \
         patch.object(bridge, 'refresh_subscription', return_value={'ok': True}) as refresh, \
         patch('app.bridge.threading.Thread', side_effect=inline_threads):
        bridge.sync_product_notifications_in_background()
        request.assert_called_once()
        assert request.call_args.args[1] == '/v1/account/notifications'
        assert request.call_args.args[2]['after'] == 0 and request.call_args.args[2]['limit'] == 50
        refresh.assert_called_once(); notice.assert_called_once_with('积分已到账', '新增 500 积分')
    assert controller.settings == before  # A display event is not an entitlement.
    assert len(bridge.get_product_notifications()['events']) == 1
    assert 'untrusted_license' not in json.dumps(bridge.get_product_notifications())
    bridge._product_sync_at = 0
    with patch.object(bridge, '_product_request', return_value={'ok': True, 'cursor': 1, 'events': []}), \
         patch.object(bridge, 'refresh_subscription') as refresh, patch('app.bridge.threading.Thread', side_effect=inline_threads):
        bridge.sync_product_notifications_in_background()
        refresh.assert_not_called()
    assert notice.call_count == 1


def test_late_notice_after_account_switch_cannot_enter_new_inbox_or_notify(desktop):
    bridge, controller, notice = desktop
    old = notice_subject(controller.settings['subscription_server_url'], 'fixture-user')
    def response(*args):
        grant_account(controller.settings, controller.settings_store.base_dir, user_id='second-user')
        bridge._account_revision += 1
        return batch()
    with patch.object(bridge, '_product_request', side_effect=response), \
         patch.object(bridge, 'refresh_subscription') as refresh, patch('app.bridge.threading.Thread', side_effect=inline_threads):
        bridge.sync_product_notifications_in_background()
        refresh.assert_not_called(); notice.assert_not_called()
    assert bridge.get_product_notifications()['events'] == []
    assert controller.product_events.cursor(old) == 0


def test_logout_or_forged_subject_cannot_read_cached_other_account_notices(desktop):
    bridge, controller, _ = desktop
    subject = notice_subject(controller.settings['subscription_server_url'], 'fixture-user')
    controller.product_events.accept_account(subject, batch())
    assert len(bridge.get_product_notifications()['events']) == 1
    controller.settings['account_user_id'] = 'forged-user'
    assert bridge.get_product_notifications()['events'] == []
    controller.settings['account_user_id'] = 'fixture-user'
    controller.settings['account_login_token'] = ''
    assert bridge.get_product_notifications()['events'] == []


def test_directory_picker_is_read_only_and_update_links_are_repository_scoped(desktop):
    bridge, controller, _ = desktop
    before = dict(controller.settings)
    bridge._window = lambda: SimpleNamespace(create_file_dialog=lambda *_: ['D:/Videos'])
    assert bridge.pick_video_scan_directory() == {'ok': True, 'path': 'D:/Videos'}
    assert controller.settings == before
    with patch('webbrowser.open', return_value=True) as opened:
        assert bridge.open_url('https://evil.example/file')['ok'] is False
        opened.assert_not_called()
        assert bridge.open_url('https://github.com/Ansel-Poiesis/FoilPage-source/releases/tag/v1.0.0')['ok'] is True
        opened.assert_called_once()


@pytest.mark.parametrize('base', ['http://pay.example.com', 'https://secret@pay.example.com', 'https://pay.example.com?key=secret'])
def test_notification_request_cannot_send_credentials_to_unsafe_url(base):
    with patch('urllib.request.build_opener') as transport:
        assert Bridge._product_request(base, '/v1/account/notifications', {'login_token': 'private-marker'})['ok'] is False
        transport.assert_not_called()


def test_transport_is_bounded_and_never_follows_credential_redirects():
    response = Mock(); response.__enter__ = Mock(return_value=response); response.__exit__ = Mock(return_value=False)
    response.read.return_value = b' ' * 65537
    opener = Mock(); opener.open.return_value = response
    with patch('urllib.request.build_opener', return_value=opener) as build:
        result = Bridge._product_request('https://pay.example.com', '/v1/account/notifications', {'login_token': 'private-marker'})
        assert result['ok'] is False
        response.read.assert_called_once_with(65537)
        handler = build.call_args.args[0]()
        assert handler.redirect_request(None, None, 302, '', {}, 'https://other.example.com') is None
