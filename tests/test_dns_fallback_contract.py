# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""Resolver fallback preserves socket contracts without external network calls."""
import socket
from email.message import Message
from io import BytesIO
from unittest import mock
from urllib.response import addinfourl

import pytest

from engine import dns_fallback


@pytest.fixture(autouse=True)
def guard():
    dns_fallback.reset_dns_fallback_for_tests()
    real = socket.getaddrinfo

    def system(host, port, *args):
        if host in ("blocked.example", b"blocked.example"):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("0.0.0.0", port))]
        return real(host, port, *args)

    with mock.patch.object(socket, "getaddrinfo", system):
        dns_fallback.install_dns_fallback()
        yield
        dns_fallback.reset_dns_fallback_for_tests()


def test_service_name_is_resolved_to_numeric_port():
    with mock.patch.object(dns_fallback, "_doh_resolve", return_value=("1.2.3.4",)):
        result = socket.getaddrinfo("blocked.example", "https", type=socket.SOCK_STREAM)
    assert result[0][4] == ("1.2.3.4", 443)


def test_bytes_hostname_is_decoded_before_doh_lookup():
    with mock.patch.object(dns_fallback, "_doh_resolve", return_value=("1.2.3.4",)) as doh:
        socket.getaddrinfo(b"blocked.example", 443)
    doh.assert_called_once_with("blocked.example")


def test_private_and_numeric_only_names_do_not_escape_to_doh():
    with mock.patch.object(dns_fallback, "_original_getaddrinfo", side_effect=socket.gaierror("system")):
        with mock.patch.object(dns_fallback, "_doh_resolve") as doh:
            for host, flags in (("intranet", 0), ("service.local", 0),
                                ("api.localhost", 0), ("blocked.example", socket.AI_NUMERICHOST)):
                with pytest.raises(socket.gaierror):
                    socket.getaddrinfo(host, 443, flags=flags)
    doh.assert_not_called()


def test_doh_does_not_accept_blackhole_or_ipv6_disguised_as_a_record():
    body = b'{"Status":0,"Answer":[{"type":1,"data":"0.0.0.0"},{"type":1,"data":"::1"}]}'
    with mock.patch.object(dns_fallback.urllib.request, "urlopen") as urlopen:
        urlopen.return_value.__enter__.return_value.read.return_value = body
        with mock.patch.object(dns_fallback.urllib.request, "build_opener") as opener:
            opener.return_value.open.return_value.__enter__.return_value.read.return_value = body
            assert dns_fallback._doh_resolve("blocked.example") == ()


def test_doh_transport_bypasses_an_unresolvable_environment_proxy():
    destinations = []

    def transfer(request):
        destinations.append(request.host)
        assert request.host == "223.5.5.5", "DoH must not resolve or connect to the proxy"
        response = addinfourl(BytesIO(b'{"Status":0,"Answer":[{"type":1,"data":"1.2.3.4"}]}'),
                              Message(), request.full_url, 200)
        response.msg = "OK"
        return response

    with mock.patch.object(dns_fallback.urllib.request, "getproxies", return_value={"https": "http://proxy.invalid:8080"}):
        with mock.patch.object(dns_fallback.urllib.request.HTTPSHandler, "https_open", side_effect=transfer):
            assert dns_fallback._doh_resolve("blocked.example") == ("1.2.3.4",)
    assert destinations == ["223.5.5.5"]
