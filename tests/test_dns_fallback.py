# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""DNS 黑洞回退（DoH）守卫测试：正常透传、黑洞/解析失败回退、缓存与幂等。"""
import socket
from unittest import mock

import pytest

from engine import dns_fallback


@pytest.fixture(autouse=True)
def clean_guard():
    dns_fallback.reset_dns_fallback_for_tests()
    real = socket.getaddrinfo
    yield
    dns_fallback.reset_dns_fallback_for_tests()
    socket.getaddrinfo = real


def _install_with_system(system_impl):
    # 直接赋值安装（勿用 mock.patch：其退出时的还原会连守卫一起覆盖）。
    real = socket.getaddrinfo

    def system(host, port, *args, **kwargs):
        if host in ("148.153.45.10", "148.153.46.90", "111.6.167.134"):
            return real(host, port, *args, **kwargs)
        return system_impl(host, port, *args, **kwargs)

    socket.getaddrinfo = system
    assert dns_fallback.install_dns_fallback() is True


def test_normal_resolution_passes_through_untouched():
    original = [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("1.2.3.4", 443))]
    calls = []

    def system(*args, **kwargs):
        calls.append(args)
        return original

    _install_with_system(system)
    with mock.patch.object(dns_fallback, "_doh_resolve") as doh:
        result = socket.getaddrinfo("ok.example", 443)
    assert result == original
    doh.assert_not_called()
    assert calls and calls[0][0] == "ok.example"


def test_blackholed_resolution_falls_back_to_doh():
    blackhole = [
        (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("0.0.0.0", 443)),
        (socket.AF_INET6, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("::", 443, 0, 0)),
    ]
    _install_with_system(lambda *a, **k: blackhole)
    with mock.patch.object(dns_fallback, "_doh_resolve", return_value=("148.153.45.10", "148.153.46.90")) as doh:
        result = socket.getaddrinfo("www.bilibili.com", 443)
    doh.assert_called_once_with("www.bilibili.com")
    assert [entry[4][0] for entry in result] == ["148.153.45.10", "148.153.46.90"]
    assert all(entry[0] == socket.AF_INET and entry[4][1] == 443 for entry in result)


def test_gaierror_falls_back_to_doh():
    def system(*a, **k):
        raise socket.gaierror("resolve failed")

    _install_with_system(system)
    with mock.patch.object(dns_fallback, "_doh_resolve", return_value=("111.6.167.134",)):
        result = socket.getaddrinfo("www.bilibili.com", 443)
    assert [entry[4][0] for entry in result] == ["111.6.167.134"]


def test_gaierror_without_doh_answer_reraises():
    def system(*a, **k):
        raise socket.gaierror("resolve failed")

    _install_with_system(system)
    with mock.patch.object(dns_fallback, "_doh_resolve", return_value=()):
        with pytest.raises(socket.gaierror):
            socket.getaddrinfo("gone.example", 443)


def test_blackhole_without_doh_answer_keeps_system_result():
    blackhole = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("0.0.0.0", 443))]
    _install_with_system(lambda *a, **k: blackhole)
    with mock.patch.object(dns_fallback, "_doh_resolve", return_value=()):
        assert socket.getaddrinfo("blocked.example", 443) == blackhole


def test_ip_literal_and_localhost_never_trigger_doh():
    def system(*a, **k):
        raise socket.gaierror("no")

    _install_with_system(system)
    with mock.patch.object(dns_fallback, "_doh_resolve") as doh:
        for host in ("223.5.5.5", "::1", "localhost"):
            with pytest.raises(socket.gaierror):
                socket.getaddrinfo(host, 53)
    doh.assert_not_called()


def test_doh_positive_result_is_cached_per_host():
    def system(*a, **k):
        raise socket.gaierror("no")

    _install_with_system(system)
    with mock.patch.object(dns_fallback, "_doh_resolve", return_value=("148.153.45.10",)) as doh:
        socket.getaddrinfo("www.bilibili.com", 443)
        socket.getaddrinfo("www.bilibili.com", 443)
    assert doh.call_count == 1


def test_install_is_idempotent():
    _install_with_system(lambda *a, **k: [])
    assert dns_fallback.install_dns_fallback() is False


def test_doh_resolve_parses_json_answers():
    body = ('{"Status":0,"Answer":['
            '{"type":5,"data":"cdn.example"},'
            '{"type":1,"data":"148.153.45.10"},'
            '{"type":1,"data":"148.153.45.10"},'
            '{"type":1,"data":"not-an-ip"}]}').encode("utf-8")
    with mock.patch.object(dns_fallback.urllib.request, "build_opener") as opener:
        opener.return_value.open.return_value.__enter__.return_value.read.return_value = body
        assert dns_fallback._doh_resolve("x.example") == ("148.153.45.10",)
