# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from agent.client import AgentClient, AgentError
from engine.agent_protocol import derive_agent_token


@contextmanager
def local_server(responder):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            request = {"path": self.path, "headers": dict(self.headers), "body": json.loads(self.rfile.read(length))}
            requests.append(request)
            status, headers, body = responder(request)
            self.send_response(status)
            for key, value in headers.items():
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1], requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def json_response(data, status=200):
    return status, {"Content-Type": "application/json"}, json.dumps(data, ensure_ascii=False).encode("utf-8")


def test_client_uses_loopback_and_scoped_auth_without_browser_headers_or_proxy(monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("NO_PROXY", "")
    token = derive_agent_token("synthetic-local-token")
    with local_server(lambda _request: json_response({"ok": True, "result": {"app": "FoilPage"}})) as (port, requests):
        result = AgentClient(port=port, token=token).call("info")
    assert result == {"app": "FoilPage"}
    assert requests[0]["path"] == "/v1/agent/call"
    assert requests[0]["headers"]["Authorization"] == "Bearer " + token
    assert "Origin" not in requests[0]["headers"]
    assert not any(key.lower().startswith("sec-fetch") for key in requests[0]["headers"])
    assert requests[0]["body"] == {"method": "info", "params": {}}


def test_redirect_never_forwards_credentials():
    with local_server(lambda _request: json_response({"ok": True, "result": {}})) as (destination, forwarded):
        with local_server(lambda _request: (307, {"Location": f"http://127.0.0.1:{destination}/stolen"}, b"")) as (port, _):
            with pytest.raises(AgentError, match="重定向") as failure:
                AgentClient(port=port, token="synthetic-token").call("info")
    assert failure.value.code == "redirect_refused"
    assert forwarded == []


def test_profile_read_does_not_migrate_write_or_decrypt_other_secrets(tmp_path: Path):
    profile = tmp_path / "settings.json"
    raw = {
        "schema_version": 1, "local_api_port": 18765, "local_api_token": "synthetic-local-token",
        "agent_api_key": "dpapi:v1:not-allowed-to-decrypt", "account_login_token": "dpapi:v1:invalid",
    }
    profile.write_text(json.dumps(raw), encoding="utf-8")
    before = profile.read_bytes()
    before_mtime = profile.stat().st_mtime_ns
    client = AgentClient.from_data_dir(tmp_path)
    assert client.port == 18765
    assert profile.read_bytes() == before
    assert profile.stat().st_mtime_ns == before_mtime
    assert client._token == derive_agent_token("synthetic-local-token")


def test_profile_reads_dpapi_only_under_the_original_windows_user(tmp_path: Path):
    from engine.settings import _protect_secret
    local_token = "synthetic-secure-connection-token"
    protected = _protect_secret(local_token)
    (tmp_path / "settings.json").write_text(json.dumps({"local_api_port": 18765, "local_api_token": protected}), encoding="utf-8")
    assert AgentClient.from_data_dir(tmp_path)._token == derive_agent_token(local_token)
    (tmp_path / "settings.json").write_text(json.dumps({"local_api_port": 18765, "local_api_token": "dpapi:v1:invalid"}), encoding="utf-8")
    with pytest.raises(AgentError) as failure:
        AgentClient.from_data_dir(tmp_path)
    assert failure.value.code == "profile_locked"
    assert "invalid" not in failure.value.message


@pytest.mark.parametrize("profile", [[], None, {"local_api_port": True, "local_api_token": "synthetic"}, {"local_api_port": "18765", "local_api_token": "synthetic"}, {"local_api_port": 18765, "local_api_token": ""}])
def test_invalid_profile_fails_without_side_effects(tmp_path: Path, profile):
    target = tmp_path / "settings.json"
    target.write_text(json.dumps(profile), encoding="utf-8")
    before = target.read_bytes()
    with pytest.raises(AgentError):
        AgentClient.from_data_dir(tmp_path)
    assert target.read_bytes() == before


def test_missing_profile_does_not_create_directories(tmp_path: Path):
    absent = tmp_path / "absent"
    with pytest.raises(AgentError) as failure:
        AgentClient.from_data_dir(absent)
    assert failure.value.code == "profile_missing"
    assert not absent.exists()


def test_oversized_settings_rejected_without_full_read(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("agent.client.MAX_SETTINGS_BYTES", 32)
    (tmp_path / "settings.json").write_bytes(b" " * 33)
    with pytest.raises(AgentError) as failure:
        AgentClient.from_data_dir(tmp_path)
    assert failure.value.code == "invalid_profile"


@pytest.mark.parametrize("status,body,code", [
    (200, b"<html>failure</html>", "invalid_response"),
    (200, b"[]", "invalid_response"),
    (200, b'{"ok":"true","result":{}}', "invalid_response"),
    (200, b'{"ok":true}', "invalid_response"),
    (403, b'{"error":{"code":"unauthorized","message":"Not authorized"}}', "unauthorized"),
    (500, b'{"error":"private internal data"}', "request_failed"),
])
def test_response_errors_are_structured_without_raw_body(status, body, code):
    with local_server(lambda _request: (status, {}, body)) as (port, _):
        with pytest.raises(AgentError) as failure:
            AgentClient(port=port, token="synthetic-token").call("info")
    assert failure.value.code == code
    assert "private internal data" not in failure.value.message


def test_response_limit_enforced(monkeypatch):
    monkeypatch.setattr("agent.client.MAX_RESPONSE_BYTES", 128)
    with local_server(lambda _request: (200, {}, b" " * 129)) as (port, _):
        with pytest.raises(AgentError) as failure:
            AgentClient(port=port, token="synthetic-token").call("info")
    assert failure.value.code == "response_too_large"


@pytest.mark.parametrize("method,params", [("settings.write", {}), ("info", []), ("info", {"nan": float("nan")})])
def test_arbitrary_routes_or_non_json_parameters_never_reach_network(method, params):
    with local_server(lambda _request: json_response({"ok": True, "result": {}})) as (port, requests):
        with pytest.raises(AgentError):
            AgentClient(port=port, token="synthetic-token").call(method, params)
    assert requests == []
