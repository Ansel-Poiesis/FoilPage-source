# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""网络环境诊断：识别把国内平台流量绕到境外出口的系统级 VPN 隧道。

2026-11-01 实机取证：Windscribe WireGuard 隧道在网卡层接管路由（用
0.0.0.0/1 + 128.0.0.0/1 双半段路由实现全局接管），应用的「国内平台强制
直连」策略（engine.downloader.proxy_route_for_platform 只能决定是否使用
HTTP 代理）无法改变路由表——所谓直连实际从美国机房出口，B 站以 412 风控
秒拒、抖音 CDN 单连接吞吐跌至 0.4 MiB/s。这类故障此前在任务里只显示
「平台暂时拒绝」，用户看不到真实原因。

探测用 ``Find-NetRoute``（Windows 自身的最长前缀选路，能穿透半段路由与
本地化输出）：查询一个稳定的国内地址（AliDNS 223.5.5.5）实际从哪个适配
器出去，若该适配器名称带 VPN/隧道特征即报告。诊断是提示不是门禁：探测
失败一律静默返回空串，绝不让失败路径再失败。
"""
from __future__ import annotations

import os
import subprocess
import threading
import time

# 仅识别常见 VPN/隧道适配器的名称特征；命中即报告，未命中不排除一切代理
# 形态。
_VPN_NAME_MARKERS = (
    "wireguard", "wintun", "tailscale", "openvpn", "anyconnect", "vpn",
    "tunnel", "clash", "sing-box", "singbox", "v2ray", "xray", "shadowsocks",
    "zerotier", "warp", "tunbuilder",
)
# 代表「国内平台目的地」的稳定地址；不解析平台域名，避免 DNS 依赖。
_DOMESTIC_PROBE_IP = "223.5.5.5"
_PROBE_CACHE_SECONDS = 600.0
_PROBE_TIMEOUT_SECONDS = 6.0

_find_net_route_command = [
    "powershell", "-NoProfile", "-NonInteractive", "-Command",
    "(Find-NetRoute -RemoteIPAddress " + _DOMESTIC_PROBE_IP
    + " | Select-Object -First 1).InterfaceAlias",
]

_probe_lock = threading.Lock()
_probe_cache: tuple[str, float] | None = None


def active_vpn_tunnel() -> str:
    """国内地址若经 VPN/隧道适配器出网，返回其接口名；否则返回空串。

    结果缓存 10 分钟：重试风暴下不反复起 PowerShell（首次约 0.5–1.5s），
    只在 412/网络类失败落库时调用。
    """
    global _probe_cache
    with _probe_lock:
        if _probe_cache is not None and _probe_cache[1] > time.monotonic():
            return _probe_cache[0]
    result = _probe_default_route_tunnel()
    with _probe_lock:
        _probe_cache = (result, time.monotonic() + _PROBE_CACHE_SECONDS)
    return result


def _probe_default_route_tunnel() -> str:
    if os.name != "nt":
        return ""
    try:
        completed = subprocess.run(
            _find_net_route_command, capture_output=True, timeout=_PROBE_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if completed.returncode != 0:
        return ""
    alias = completed.stdout.decode("utf-8", errors="replace").strip()
    if not alias or "\n" in alias:
        return ""
    if any(marker in alias.lower() for marker in _VPN_NAME_MARKERS):
        return alias
    return ""


def reset_vpn_probe_cache_for_tests() -> None:
    global _probe_cache
    with _probe_lock:
        _probe_cache = None
