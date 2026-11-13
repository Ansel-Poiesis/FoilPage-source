# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""Device-management grants stay separate from signed desktop entitlement."""
import json
import urllib.error
import urllib.request
import pytest

from test_account_session_lifecycle import bridge, remote, Response


def limited():
    return {"ok": False, "status": "device_limit", "management_token": "manage-test",
            "account": {"device_management_only": True, "devices": ["old-one", "old-two"],
                        "current_device_id": "new-device", "max_devices": 2}}


def prepare(result):
    return result._remember_device_management(limited(), base="https://pay.example.com",
                                              expected_session="session-a", expected_pending="")


def test_management_grant_never_overwrites_paid_session_or_leaks_to_webview(tmp_path):
    result, controller = bridge(tmp_path)
    value = prepare(result)
    assert value["status"] == "device_limit"
    assert "management_token" not in value
    assert controller.settings["account_login_token"] == "session-a"
    assert controller.settings["license_token"] == "old-license"
    assert "manage-test" not in json.dumps(controller.settings)


def test_management_response_after_logout_is_ignored(tmp_path):
    result, controller = bridge(tmp_path)
    result._clear_account()
    assert not prepare(result)["ok"]
    assert result._account_management is None
    assert not controller.settings["license_token"]


def test_unsigned_account_view_does_not_increase_local_balance(tmp_path, monkeypatch):
    result, controller = bridge(tmp_path)
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: Response(
        {"ok": True, "credit_balance": 999999, "devices": ["current"]}))
    assert result.get_account_devices()["ok"]
    assert controller.settings["credit_balance"] == 500


def test_account_device_view_ignores_late_response_after_account_changes(tmp_path, monkeypatch):
    result, controller = bridge(tmp_path)
    def delayed(*args, **kwargs):
        controller.save_settings({"account_login_token": "session-new"})
        return Response({"ok": True, "devices": ["old"]})
    monkeypatch.setattr(urllib.request, "urlopen", delayed)
    assert not result.get_account_devices()["ok"]


def test_device_management_uses_restricted_token_without_license(tmp_path, monkeypatch):
    result, _ = bridge(tmp_path)
    prepare(result)
    def account(request, **kwargs):
        assert request.full_url.endswith("/v1/account")
        assert json.loads(request.data)["login_token"] == "manage-test"
        return Response({"ok": True, **limited()["account"]})
    monkeypatch.setattr(urllib.request, "urlopen", account)
    assert result.get_account_devices()["device_management_only"]


def test_complete_login_requires_signed_license_and_keeps_management_on_failure(tmp_path, monkeypatch):
    result, controller = bridge(tmp_path)
    prepare(result)
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: Response(
        {"ok": True, "login_token": "new-login", "license_token": "unsigned"}))
    assert not result.complete_account_login()["ok"]
    assert controller.settings["account_login_token"] == "session-a"
    assert result._account_management is not None


def test_still_full_complete_response_does_not_expose_management_token(tmp_path, monkeypatch):
    result, _ = bridge(tmp_path)
    prepare(result)
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: Response(limited()))
    value = result.complete_account_login()
    assert value["status"] == "device_limit"
    assert "management_token" not in value


def test_complete_login_applies_signed_license_then_discards_management(tmp_path, monkeypatch):
    result, controller = bridge(tmp_path)
    prepare(result)
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: Response(
        {**remote("user-new"), "login_token": "session-new"}))
    assert result.complete_account_login()["ok"]
    assert controller.settings["account_login_token"] == "session-new"
    assert controller.settings["account_user_id"] == "user-new"
    assert result._account_management is None


def test_service_change_cannot_send_management_token_to_new_host(tmp_path, monkeypatch):
    result, controller = bridge(tmp_path)
    prepare(result)
    controller.save_settings({"subscription_server_url": "https://new.example.com"})
    calls = []
    monkeypatch.setattr(urllib.request, "urlopen", lambda request, **k: calls.append(request))
    assert not result.complete_account_login()["ok"]
    assert result._account_management is None
    assert not calls


def test_current_device_unbind_clears_local_license(tmp_path, monkeypatch):
    result, controller = bridge(tmp_path)
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: Response(
        {"ok": True, "current_device_unbound": True}))
    assert result.unbind_account_device("current")["ok"]
    assert not controller.settings["account_login_token"]
    assert not controller.settings["license_token"]


def test_network_error_preserves_account_but_definitive_rejection_clears(tmp_path, monkeypatch):
    result, controller = bridge(tmp_path)
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: (_ for _ in ()).throw(urllib.error.URLError("offline")))
    assert not result.get_account_devices()["ok"]
    assert controller.settings["license_token"] == "old-license"
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: Response({"ok": False, "status": "invalid"}))
    assert not result.get_account_devices()["ok"]
    assert not controller.settings["license_token"]


def test_logout_revokes_current_pending_and_management_tokens(tmp_path, monkeypatch):
    result, controller = bridge(tmp_path, account_pending_login_token="pending-a")
    result._remember_device_management(limited(), base="https://pay.example.com",
                                      expected_session="session-a", expected_pending="pending-a")
    revoked = []
    def logout(request, **kwargs):
        revoked.append(json.loads(request.data)["login_token"])
        return Response({"ok": True})
    monkeypatch.setattr(urllib.request, "urlopen", logout)
    assert result.account_logout()["ok"]
    assert set(revoked) == {"session-a", "pending-a", "manage-test"}
    assert result._account_management is None
    assert not controller.settings["account_pending_login_token"]


def test_cancel_unfinished_login_preserves_signed_current_account(tmp_path, monkeypatch):
    result, controller = bridge(tmp_path)
    prepare(result)
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: Response({"ok": True}))
    assert result.cancel_account_login()["ok"]
    assert controller.settings["account_login_token"] == "session-a"
    assert controller.settings["license_token"] == "old-license"
    assert result._account_management is None


def test_email_binding_intent_is_explicit_and_invalid_intent_rejected(tmp_path, monkeypatch):
    result, _ = bridge(tmp_path)
    requests = []
    def verify(request, **kwargs):
        requests.append(json.loads(request.data))
        return Response({**remote(), "login_token": "new-session"})
    monkeypatch.setattr(urllib.request, "urlopen", verify)
    assert result.email_login_verify("fixture@example.com", "123456", "bind")["ok"]
    assert requests[0]["intent"] == "bind"
    assert not result.email_login_verify("fixture@example.com", "123456", "merge")["ok"]
    assert len(requests) == 1


def test_anonymous_logout_ignores_late_email_login_success(tmp_path, monkeypatch):
    result, controller = bridge(tmp_path, account_login_token="", account_pending_login_token="")
    def delayed(request, **kwargs):
        result._clear_account()
        return Response({**remote(), "login_token": "session-late"})
    monkeypatch.setattr(urllib.request, "urlopen", delayed)
    assert not result.email_login_verify("fixture@example.com", "123456")["ok"]
    assert not controller.settings["account_login_token"]
    assert not controller.settings["license_token"]


def test_oauth_start_after_cancellation_cannot_restore_pending_token(tmp_path, monkeypatch):
    import webbrowser
    result, controller = bridge(tmp_path, account_login_token="", account_pending_login_token="")
    def delayed(request, **kwargs):
        result._clear_account()
        return Response({"ok": True, "login_token": "pending-late", "auth_url": "https://accounts.google.com/"})
    opened = []
    monkeypatch.setattr(urllib.request, "urlopen", delayed)
    monkeypatch.setattr(webbrowser, "open", opened.append)
    assert not result.google_login_start()["ok"]
    assert not controller.settings["account_pending_login_token"]
    assert not opened


def test_changing_service_host_clears_its_private_authorization(tmp_path):
    result, controller = bridge(tmp_path, account_pending_login_token="pending-old")
    result.save_settings({"subscription_server_url": "https://new.example.com"})
    assert not controller.settings["account_login_token"]
    assert not controller.settings["account_pending_login_token"]
    assert not controller.settings["license_token"]


def test_failed_service_setting_save_preserves_account(tmp_path):
    result, controller = bridge(tmp_path)
    original = controller.save_settings
    def validate(values):
        if values.get("subscription_server_url") == "invalid":
            raise ValueError("invalid service URL")
        original(values)
    controller.save_settings = validate
    try:
        result.save_settings({"subscription_server_url": "invalid"})
    except ValueError:
        pass
    assert controller.settings["account_login_token"] == "session-a"
    assert controller.settings["license_token"] == "old-license"




@pytest.mark.parametrize("url", ["file:///C:/untrusted.exe", "javascript:alert(1)",
                                  "http://accounts.google.com/", "https://untrusted.example.com/"])
def test_oauth_cannot_open_a_local_file_or_untrusted_authorization_host(tmp_path, monkeypatch, url):
    import webbrowser
    result, controller = bridge(tmp_path, account_pending_login_token="")
    opened = []
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: Response(
        {"ok": True, "login_token": "pending-fixture", "auth_url": url}))
    monkeypatch.setattr(webbrowser, "open", opened.append)
    assert not result.google_login_start()["ok"]
    assert not opened
    assert not controller.settings["account_pending_login_token"]
