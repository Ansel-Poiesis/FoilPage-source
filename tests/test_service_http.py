# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from engine.service_http import (
    WORKER_NETWORK_POLICY_ENV,
    ServiceHttpError,
    network_policy_for,
    request_json,
    worker_environment,
    worker_network_policy,
)


@contextmanager
def local_server(responder):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append({"path": self.path, "headers": dict(self.headers)})
            status, headers, body = responder(requests[-1])
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


def json_response(value, status=200, headers=None):
    body = json.dumps(value).encode("utf-8")
    return status, {"Content-Type": "application/json", **(headers or {})}, body


def test_direct_route_ignores_environment_proxy(monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("NO_PROXY", "")
    with local_server(lambda _request: json_response({"ok": True})) as (port, requests):
        value = request_json(
            urllib.request.Request(f"http://127.0.0.1:{port}/direct"),
            timeout=2,
            service="test",
            operation="direct",
            proxy_route="",
        )
    assert value == {"ok": True}
    assert requests[0]["path"] == "/direct"


def test_loopback_route_ignores_environment_proxy(monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("NO_PROXY", "")
    monkeypatch.setattr(urllib.request, "_opener", None)
    diagnostics = []
    with local_server(lambda _request: json_response({"ok": True})) as (port, requests):
        value = request_json(
            urllib.request.Request(f"http://127.0.0.1:{port}/loopback"),
            timeout=2,
            service="test",
            operation="loopback",
            diagnostic=diagnostics.append,
        )
    assert value == {"ok": True}
    assert [request["path"] for request in requests] == ["/loopback"]
    assert diagnostics[-1]["route"] == "direct"


def test_custom_http_proxy_receives_absolute_request_without_origin_dns(monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("NO_PROXY", "")
    with local_server(lambda request: json_response({"path": request["path"]})) as (port, requests):
        value = request_json(
            urllib.request.Request("http://origin.invalid/asset?token=private"),
            timeout=2,
            service="test",
            operation="custom",
            proxy_route=f"http://127.0.0.1:{port}",
        )
    assert value == {"path": "http://origin.invalid/asset?token=private"}
    assert requests[0]["headers"]["Host"] == "origin.invalid"


def test_system_route_uses_environment_proxy(monkeypatch):
    with local_server(lambda _request: json_response({"ok": True})) as (port, requests):
        monkeypatch.setenv("HTTP_PROXY", f"http://127.0.0.1:{port}")
        monkeypatch.setenv("NO_PROXY", "")
        monkeypatch.setattr(urllib.request, "_opener", None)
        value = request_json(
            urllib.request.Request("http://origin.invalid/system"),
            timeout=2,
            service="test",
            operation="system",
        )
    assert value == {"ok": True}
    assert requests[0]["path"] == "http://origin.invalid/system"


@pytest.mark.parametrize("status,code,retryable", [
    (401, "unauthorized", False),
    (403, "forbidden", False),
    (408, "request_timeout", True),
    (409, "conflict", False),
    (429, "rate_limited", True),
    (503, "service_unavailable", True),
])
def test_http_failures_are_classified_without_implicit_retry(status, code, retryable):
    with local_server(lambda _request: json_response({"error": {"code": "server_code"}}, status, {"Retry-After": "7"})) as (port, requests):
        with pytest.raises(ServiceHttpError) as caught:
            request_json(
                urllib.request.Request(f"http://127.0.0.1:{port}/failure"),
                timeout=2,
                service="test",
                operation="failure",
                proxy_route="",
            )
    assert caught.value.code == code
    assert caught.value.status == status
    assert caught.value.retryable is retryable
    assert caught.value.retry_after == 7
    assert caught.value.payload == {"error": {"code": "server_code"}}
    assert len(requests) == 1


def test_diagnostics_omit_url_headers_and_response_body():
    diagnostics = []
    with local_server(lambda _request: json_response({"ok": True})) as (port, _):
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/private?token=secret",
            headers={"Authorization": "Bearer private-key"},
        )
        request_json(
            request,
            timeout=2,
            service="model",
            operation="chat",
            proxy_route="",
            diagnostic=diagnostics.append,
        )
    assert diagnostics == [{
        "service": "model",
        "operation": "chat",
        "route": "direct",
        "stage": "complete",
        "elapsed_ms": diagnostics[0]["elapsed_ms"],
        "status": 200,
        "error_code": "",
        "retryable": False,
    }]
    assert "secret" not in json.dumps(diagnostics)
    assert "private-key" not in json.dumps(diagnostics)


def test_cancel_before_connect_sends_no_request():
    calls = []
    with local_server(lambda _request: json_response({"ok": True})) as (port, requests):
        with pytest.raises(ServiceHttpError) as caught:
            request_json(
                urllib.request.Request(f"http://127.0.0.1:{port}/cancelled"),
                timeout=2,
                service="test",
                operation="cancel",
                proxy_route="",
                cancel_check=lambda: True,
                opener=lambda *args, **kwargs: calls.append((args, kwargs)),
            )
    assert caught.value.code == "cancelled"
    assert caught.value.retryable is False
    assert calls == [] and requests == []


def test_invalid_json_and_oversized_response_are_bounded():
    with local_server(lambda _request: (200, {}, b"not-json")) as (port, _):
        with pytest.raises(ServiceHttpError) as caught:
            request_json(
                urllib.request.Request(f"http://127.0.0.1:{port}/invalid"),
                timeout=2,
                service="test",
                operation="invalid",
                proxy_route="",
            )
    assert caught.value.code == "invalid_response"

    with local_server(lambda _request: (200, {}, b"x" * 64)) as (port, _):
        with pytest.raises(ServiceHttpError) as caught:
            request_json(
                urllib.request.Request(f"http://127.0.0.1:{port}/large"),
                timeout=2,
                service="test",
                operation="large",
                proxy_route="",
                max_response_bytes=32,
            )
    assert caught.value.code == "response_too_large"


@pytest.mark.parametrize("failure", [
    TimeoutError("private timeout detail"),
    urllib.error.URLError(TimeoutError("private timeout detail")),
])
def test_nested_timeout_reasons_are_classified_without_leaking_details(failure):
    def fail(*_args, **_kwargs):
        raise failure

    with pytest.raises(ServiceHttpError) as caught:
        request_json(
            urllib.request.Request("http://origin.invalid/timeout"),
            timeout=2,
            service="test",
            operation="timeout",
            opener=fail,
        )
    assert caught.value.code == "network_timeout"
    assert caught.value.retryable is True
    assert "private" not in str(caught.value)


def test_unsupported_socks_route_fails_before_network():
    with pytest.raises(ServiceHttpError) as caught:
        request_json(
            urllib.request.Request("http://origin.invalid/socks"),
            timeout=2,
            service="test",
            operation="socks",
            proxy_route="socks5://127.0.0.1:1080",
        )
    assert caught.value.code == "proxy_unsupported"
    assert caught.value.retryable is False


def test_network_policy_preserves_three_route_semantics():
    assert network_policy_for("direct", "").proxy_route == ""
    assert network_policy_for("system", "").proxy_route is None
    assert network_policy_for("auto", "").proxy_route is None
    custom = network_policy_for("custom", " http://127.0.0.1:7890 ")
    assert custom.mode == "custom"
    assert custom.proxy_route == "http://127.0.0.1:7890"
    assert network_policy_for("custom", "").proxy_route is None
    assert network_policy_for("unknown", "").mode == "auto"


def test_worker_direct_environment_removes_proxy_variants_and_keeps_runtime_settings():
    environment = {
        "HTTP_PROXY": "http://127.0.0.1:1",
        "http_proxy": "http://127.0.0.1:2",
        "HTTPS_PROXY": "http://127.0.0.1:3",
        "ALL_PROXY": "socks5://127.0.0.1:4",
        "NO_PROXY": "fixture.invalid",
        "PATH": "keep-path",
        "HF_HOME": "keep-cache",
    }
    result = worker_environment(environment, network_policy_for("direct", ""))
    assert result["NO_PROXY"] == "*"
    assert result[WORKER_NETWORK_POLICY_ENV] == "direct"
    assert result["PATH"] == "keep-path" and result["HF_HOME"] == "keep-cache"
    assert not any(key.upper() in {"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"} for key in result)
    assert environment["HTTP_PROXY"] == "http://127.0.0.1:1"


def test_worker_custom_environment_uses_canonical_proxy_and_loopback_bypass():
    result = worker_environment(
        {"http_proxy": "http://127.0.0.1:1", "NO_PROXY": "old"},
        network_policy_for("custom", "http://127.0.0.1:7890"),
    )
    assert result["HTTP_PROXY"] == result["HTTPS_PROXY"] == "http://127.0.0.1:7890"
    assert result["ALL_PROXY"] == "http://127.0.0.1:7890"
    assert result["NO_PROXY"] == "localhost,127.0.0.1,::1"
    assert result[WORKER_NETWORK_POLICY_ENV] == "custom"
    assert "http_proxy" not in result


def test_worker_system_environment_canonicalizes_inherited_proxy_without_changing_values():
    result = worker_environment(
        {"http_proxy": "http://127.0.0.1:7890", "no_proxy": "fixture.invalid"},
        network_policy_for("system", ""),
    )
    assert result["HTTP_PROXY"] == "http://127.0.0.1:7890"
    assert result["NO_PROXY"] == "fixture.invalid"
    assert result[WORKER_NETWORK_POLICY_ENV] == "system"
    assert "http_proxy" not in result and "no_proxy" not in result


def test_worker_policy_round_trip_uses_environment_snapshot_not_latest_settings():
    route = "http://fixture-user:fixture-pass@127.0.0.1:7890"
    policy = network_policy_for("custom", route)
    environment = worker_environment({}, policy)
    restored = worker_network_policy(environment)
    assert restored == policy
    assert restored.proxy_route == route
    assert "fixture-user" not in repr(policy)
    assert "fixture-pass" not in repr(policy)


def test_worker_auto_and_invalid_custom_fall_back_to_system_snapshot():
    for mode, custom_url in (("auto", ""), ("custom", ""), ("custom", "file:///tmp/proxy")):
        environment = worker_environment(
            {"HTTP_PROXY": "http://127.0.0.1:7890"},
            network_policy_for(mode, custom_url),
        )
        assert environment[WORKER_NETWORK_POLICY_ENV] == "system"
        restored = worker_network_policy(environment)
        assert restored.mode == "system" and restored.proxy_route is None


def test_worker_policy_rejects_invalid_or_incomplete_snapshot():
    with pytest.raises(ValueError, match="网络策略快照无效"):
        worker_network_policy({WORKER_NETWORK_POLICY_ENV: "auto"})
    with pytest.raises(ValueError, match="网络策略快照无效"):
        worker_network_policy({WORKER_NETWORK_POLICY_ENV: "custom", "ALL_PROXY": ""})
