# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""DNS 黑洞回退（DoH）：本地系统解析可能被 VPN/过滤工具劫持——对部分平台
域名直接返回 0.0.0.0/::（2026-10-19 实机：Windscribe WireGuard 的 DNS 对
bilibili.com 黑洞，同时明文 53 端口出站被拦，桌面端直连解析无路可走；浏览器
借 DoH 照常访问，造成「浏览器能看、FoilPage 下不动」的错位）。

本模块给平台解析装上与浏览器同款的逃生门：系统解析抛错或结果全为黑洞地址时，
经国内 DoH（AliDNS 223.5.5.5 / DNSPod 120.53.53.53，均为 IP 直连、无需自举
解析）补出真实地址。连接仍以原主机名完成（TLS/SNI/证书不受影响）。正常环境
零行为变化：解析成功即原样透传，DoH 仅在异常时触发并带短缓存。
"""
from __future__ import annotations

import json
import logging
import re
import socket
import threading
import time
import urllib.request
from urllib.parse import quote
from ipaddress import ip_address

log = logging.getLogger("shengjian.dns")

# 明文 DNS 被拦时仍可用的 JSON DoH（RFC 8484 之外的 Google 风格 JSON API）。
_DOH_ENDPOINTS = (
    "https://223.5.5.5/resolve?name={name}&type=1",
    "https://120.53.53.53/dns-query?name={name}&type=1",
)
_BLACKHOLE_ADDRS = {"0.0.0.0", "::"}
_CACHE_TTL_SECONDS = 300.0
_CACHE_LIMIT = 256
_QUERY_TIMEOUT_SECONDS = 4.0
_MAX_RESPONSE_BYTES = 65536
_install_lock = threading.Lock()

_installed = False
_original_getaddrinfo = None
_cache: dict[str, tuple[tuple[str, ...], float]] = {}


def _public_hostname(host) -> str:
    """Only DNS hostnames, never local names, IP literals or URL fragments."""
    try:
        name = host.decode("ascii") if isinstance(host, bytes) else str(host or "")
        name = name.rstrip(".").lower().encode("idna").decode("ascii")
    except (UnicodeError, ValueError):
        return ""
    if ("." not in name or len(name) > 253
            or name.endswith((".localhost", ".local", ".internal", ".lan"))
            or not all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
                       for label in name.split("."))):
        return ""
    try:
        ip_address(name)
    except ValueError:
        return name
    return ""


def _doh_resolve(host: str) -> tuple[str, ...]:
    # A proxy may itself need DNS. Bypass ambient proxies to avoid recursively
    # resolving that proxy through this same fallback.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for endpoint in _DOH_ENDPOINTS:
        url = endpoint.format(name=quote(host, safe=""))
        try:
            request = urllib.request.Request(url, headers={"Accept": "application/dns-json"})
            with opener.open(request, timeout=_QUERY_TIMEOUT_SECONDS) as response:
                body = response.read(_MAX_RESPONSE_BYTES + 1)
                if len(body) > _MAX_RESPONSE_BYTES:
                    continue
                payload = json.loads(body.decode("utf-8", "replace"))
            if not isinstance(payload, dict) or payload.get("Status", 0) != 0:
                continue
            answers = payload.get("Answer") if isinstance(payload, dict) else None
            if not isinstance(answers, list):
                continue
            ips = []
            for item in answers:
                if not isinstance(item, dict) or item.get("type") != 1:
                    continue
                value = str(item.get("data") or "")
                try:
                    address = ip_address(value)
                except ValueError:
                    continue
                if address.version == 4 and address.is_global:
                    ips.append(str(address))
            if ips:
                return tuple(dict.fromkeys(ips))
        except Exception:  # noqa: BLE001 - DoH 不可达/被拦时换下一个端点
            continue
    return ()


def _looks_blackholed(result: list) -> bool:
    if not result:
        return False
    for entry in result:
        sockaddr = entry[4] if len(entry) >= 5 else ()
        if not sockaddr:
            continue
        address = str(sockaddr[0])
        if address not in _BLACKHOLE_ADDRS:
            return False
    return True


def _guarded_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    assert _original_getaddrinfo is not None
    system_error = None
    try:
        result = _original_getaddrinfo(host, port, family, type, proto, flags)
    except socket.gaierror as exc:
        system_error = exc
        result = None
    else:
        if not _looks_blackholed(result):
            return result
    name = _public_hostname(host)
    if not name or family not in (0, socket.AF_UNSPEC, socket.AF_INET) or flags & socket.AI_NUMERICHOST:
        if system_error is not None:
            raise system_error
        return result
    now = time.monotonic()
    cached = _cache.get(name)
    if cached and cached[1] > now:
        ips = cached[0]
    else:
        ips = _doh_resolve(name)
        if not ips:
            # 回退也无解：维持系统解析的原样行为（异常或黑洞由上层按网络错误处理）。
            if system_error is not None:
                raise system_error
            return result
        if len(_cache) >= _CACHE_LIMIT:
            _cache.clear()
        _cache[name] = (ips, now + _CACHE_TTL_SECONDS)
        log.info("dns fallback via doh: %s -> %s", name, ",".join(ips[:3]))
    entries = []
    for ip in ips:
        # Let the OS resolve service names, socket type/protocol and flags for
        # the literal address. Constructing sockaddr ourselves breaks 'https'
        # ports and UDP/AI_* semantics.
        entries.extend(_original_getaddrinfo(ip, port, family, type, proto, flags | socket.AI_NUMERICHOST))
    if not entries:
        if result is None:
            raise socket.gaierror(socket.EAI_NONAME, "Name or service not known (no usable DoH address family)")
        return result
    return entries


def install_dns_fallback() -> bool:
    """幂等安装进程级 getaddrinfo 回退。返回是否本次真正安装。"""
    global _installed, _original_getaddrinfo
    with _install_lock:
        if _installed:
            return False
        _original_getaddrinfo = socket.getaddrinfo
        socket.getaddrinfo = _guarded_getaddrinfo
        _installed = True
    log.info("dns fallback guard installed")
    return True


def reset_dns_fallback_for_tests() -> None:
    """仅测试用：卸载守卫并清空缓存。"""
    global _installed, _original_getaddrinfo
    if _original_getaddrinfo is not None:
        socket.getaddrinfo = _original_getaddrinfo
    _installed = False
    _original_getaddrinfo = None
    _cache.clear()
