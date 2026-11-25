# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""Shared outbound HTTP policy for model, account and service requests."""
from __future__ import annotations

import errno
from ipaddress import ip_address
import json
import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Callable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, Request, build_opener, urlopen

_MAX_RESPONSE_BYTES = 8 * 1024 * 1024
_ERROR_BODY_BYTES = 65536
DiagnosticSink = Callable[[dict[str, Any]], None]
WORKER_NETWORK_POLICY_ENV = "FOILPAGE_WORKER_NETWORK_POLICY"
_NETWORK_POLICIES = frozenset({"auto", "direct", "system", "custom"})
_PROXY_ENV_KEYS = frozenset({
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
})


@dataclass(frozen=True)
class NetworkPolicy:
    mode: str
    proxy_route: str | None = field(default=None, repr=False)


def network_policy_for(mode: str | None, custom_url: str | None) -> NetworkPolicy:
    selected = str(mode or "auto").strip().lower()
    if selected not in _NETWORK_POLICIES:
        selected = "auto"
    if selected == "direct":
        return NetworkPolicy(selected, "")
    if selected == "custom":
        route = str(custom_url or "").strip()
        parsed = urlsplit(route)
        if route and parsed.scheme.lower() in ("http", "https", "socks4", "socks5", "socks5h") and parsed.hostname:
            return NetworkPolicy(selected, route)
    return NetworkPolicy(selected, None)


def worker_environment(environment: Mapping[str, str], policy: NetworkPolicy) -> dict[str, str]:
    result = dict(environment)
    for key in list(result):
        if key.upper() in _PROXY_ENV_KEYS:
            del result[key]
    if policy.proxy_route == "":
        result["NO_PROXY"] = "*"
    elif policy.proxy_route is not None:
        scheme = urlsplit(policy.proxy_route).scheme.lower()
        if scheme in ("http", "https"):
            result["HTTP_PROXY"] = policy.proxy_route
            result["HTTPS_PROXY"] = policy.proxy_route
        result["ALL_PROXY"] = policy.proxy_route
        result["NO_PROXY"] = "localhost,127.0.0.1,::1"
    else:
        for canonical in _PROXY_ENV_KEYS:
            for key, value in environment.items():
                if key.upper() == canonical:
                    result[canonical] = value
                    break
    if policy.proxy_route is None:
        effective_mode = "system"
    elif policy.proxy_route == "":
        effective_mode = "direct"
    else:
        effective_mode = "custom"
    result[WORKER_NETWORK_POLICY_ENV] = effective_mode
    return result


def worker_network_policy(environment: Mapping[str, str]) -> NetworkPolicy | None:
    mode = environment.get(WORKER_NETWORK_POLICY_ENV)
    if mode is None:
        return None
    if mode not in ("direct", "system", "custom"):
        raise ValueError("网络策略快照无效")
    if mode == "custom":
        route = environment.get("ALL_PROXY")
        policy = network_policy_for("custom", route)
        if policy.proxy_route is None:
            raise ValueError("网络策略快照无效")
        return policy
    return network_policy_for(mode, None)


class ServiceHttpError(RuntimeError):
    """A bounded failure classification that never contains credentials or URLs."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status: int | None = None,
        retryable: bool = False,
        retry_after: float | None = None,
        diagnostic: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status = status
        self.retryable = retryable
        self.retry_after = retry_after
        self.diagnostic = diagnostic or {}
        self.payload = payload


def payment_service_headers() -> dict[str, str]:
    # A stable product name avoids generic Python clients being rejected by
    # API gateways. Do not include device IDs, account data or credentials.
    return {
        "User-Agent": "ShengJianDesktop",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


def _check_cancel(cancel_check: Callable[[], bool] | None) -> None:
    if cancel_check is not None and cancel_check():
        raise ServiceHttpError("cancelled", "请求已取消")


def _timeout(value: float) -> float:
    if isinstance(value, bool):
        raise ServiceHttpError("invalid_timeout", "请求超时配置无效")
    try:
        timeout = float(value)
    except (TypeError, ValueError):
        raise ServiceHttpError("invalid_timeout", "请求超时配置无效") from None
    if not math.isfinite(timeout) or timeout <= 0:
        raise ServiceHttpError("invalid_timeout", "请求超时配置无效")
    return timeout


def _route_name(proxy_route: str | None) -> str:
    if proxy_route is None:
        return "system"
    return "direct" if proxy_route == "" else "custom"


def _request_route(request: Request, proxy_route: str | None) -> str | None:
    hostname = urlsplit(request.full_url).hostname or ""
    try:
        if ip_address(hostname).is_loopback:
            return ""
    except ValueError:
        if hostname.casefold() == "localhost":
            return ""
    return proxy_route


def _open(request: Request, *, timeout: float, proxy_route: str | None):
    if proxy_route is None:
        return urlopen(request, timeout=timeout)
    if proxy_route == "":
        return build_opener(ProxyHandler({})).open(request, timeout=timeout)
    if proxy_route.startswith(("http://", "https://")):
        opener = build_opener(ProxyHandler({"http": proxy_route, "https": proxy_route}))
        return opener.open(request, timeout=timeout)
    raise ServiceHttpError("proxy_unsupported", "当前出站通道不支持该代理类型")


def open_response(
    request: Request,
    *,
    timeout: float,
    proxy_route: str | None = None,
    cancel_check: Callable[[], bool] | None = None,
):
    """Open a response under one explicit route; local loopback callers stay direct."""
    _check_cancel(cancel_check)
    return _open(request, timeout=_timeout(timeout), proxy_route=_request_route(request, proxy_route))


def _read_limited(stream: Any, limit: int) -> bytes:
    try:
        raw = stream.read(limit + 1)
    except TypeError:
        raw = stream.read()
    return raw if isinstance(raw, bytes) else bytes(raw or b"")


def _payload(raw: bytes) -> dict[str, Any] | None:
    if len(raw) > _ERROR_BODY_BYTES:
        return None
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _retry_after(headers: Any) -> float | None:
    value = str(headers.get("Retry-After") if headers is not None else "").strip()
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(value)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return max(0.0, (parsed - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return None


def _http_error(exc: HTTPError) -> ServiceHttpError:
    try:
        raw = _read_limited(exc, _ERROR_BODY_BYTES)
    except (AttributeError, OSError):
        raw = b""
    payload = _payload(raw)
    status = int(exc.code)
    if status == 401:
        code, retryable = "unauthorized", False
    elif status == 403:
        code, retryable = "forbidden", False
    elif status == 404:
        code, retryable = "not_found", False
    elif status == 408:
        code, retryable = "request_timeout", True
    elif status == 409:
        code, retryable = "conflict", False
    elif status == 422:
        code, retryable = "validation_failed", False
    elif status == 429:
        code, retryable = "rate_limited", True
    elif status >= 500:
        code, retryable = "service_unavailable", True
    else:
        code, retryable = "http_error", False
    return ServiceHttpError(
        code,
        "服务拒绝请求" if status < 500 else "服务暂不可用",
        status=status,
        retryable=retryable,
        retry_after=_retry_after(exc.headers),
        payload=payload,
    )


def request_json(
    request: Request,
    *,
    timeout: float,
    service: str,
    operation: str,
    proxy_route: str | None = None,
    cancel_check: Callable[[], bool] | None = None,
    diagnostic: DiagnosticSink | None = None,
    opener: Callable[..., Any] | None = None,
    max_response_bytes: int = _MAX_RESPONSE_BYTES,
) -> Any:
    """Read one JSON response without implicit retries or secret-bearing diagnostics."""
    proxy_route = _request_route(request, proxy_route)
    started = time.monotonic()
    stage = "connect"
    status: int | None = None

    def emit(code: str = "", *, retryable: bool = False, complete: bool = False) -> None:
        if diagnostic is None:
            return
        diagnostic({
            "service": service,
            "operation": operation,
            "route": _route_name(proxy_route),
            "stage": "complete" if complete else stage,
            "elapsed_ms": round((time.monotonic() - started) * 1000),
            "status": status,
            "error_code": code,
            "retryable": retryable,
        })

    try:
        _check_cancel(cancel_check)
        timeout = _timeout(timeout)
        if opener is None:
            response = open_response(
                request,
                timeout=timeout,
                proxy_route=proxy_route,
                cancel_check=cancel_check,
            )
        else:
            response = opener(request, timeout=timeout)
        stage = "read"
        with response:
            status = getattr(response, "status", None) or getattr(response, "code", None)
            raw = _read_limited(response, max_response_bytes)
        if len(raw) > max_response_bytes:
            raise ServiceHttpError("response_too_large", "服务响应超过大小限制")
        _check_cancel(cancel_check)
        stage = "decode"
        value = json.loads(raw.decode("utf-8"))
    except HTTPError as exc:
        failure = _http_error(exc)
        status = failure.status
        failure.diagnostic = {
            "service": service,
            "operation": operation,
            "route": _route_name(proxy_route),
            "stage": stage,
            "elapsed_ms": round((time.monotonic() - started) * 1000),
            "status": status,
            "error_code": failure.code,
            "retryable": failure.retryable,
        }
        emit(failure.code, retryable=failure.retryable)
        raise failure from None
    except ServiceHttpError as exc:
        status = exc.status
        exc.diagnostic = {
            "service": service,
            "operation": operation,
            "route": _route_name(proxy_route),
            "stage": stage,
            "elapsed_ms": round((time.monotonic() - started) * 1000),
            "status": status,
            "error_code": exc.code,
            "retryable": exc.retryable,
        }
        emit(exc.code, retryable=exc.retryable)
        raise
    except (URLError, TimeoutError, OSError) as exc:
        reason = exc.reason if isinstance(exc, URLError) else exc
        code = "network_timeout" if (
            isinstance(reason, TimeoutError)
            or getattr(reason, "errno", None) == errno.ETIMEDOUT
            or "timed out" in str(reason).lower()
        ) else "network_error"
        failure = ServiceHttpError(code, "网络连接失败", retryable=True)
        failure.diagnostic = {
            "service": service,
            "operation": operation,
            "route": _route_name(proxy_route),
            "stage": stage,
            "elapsed_ms": round((time.monotonic() - started) * 1000),
            "status": status,
            "error_code": code,
            "retryable": True,
        }
        emit(code, retryable=True)
        raise failure from None
    except (UnicodeError, json.JSONDecodeError):
        failure = ServiceHttpError("invalid_response", "服务返回的 JSON 无效")
        failure.diagnostic = {
            "service": service,
            "operation": operation,
            "route": _route_name(proxy_route),
            "stage": stage,
            "elapsed_ms": round((time.monotonic() - started) * 1000),
            "status": status,
            "error_code": failure.code,
            "retryable": False,
        }
        emit(failure.code)
        raise failure from None
    emit(complete=True)
    return value
