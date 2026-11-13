# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""Purchase links retain the exact advertised cycle after a real HTTP health check."""
from __future__ import annotations

import base64
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.parse import parse_qs, urlsplit

import pytest

from app.bridge import Bridge
from engine.subscription import checkout


@pytest.fixture
def purchase_service(tmp_path):
    state = {"open": True, "periods": ["month", "year"], "requests": []}
    offers = {
        "month": {"period": "month", "months": 1, "amount": "19.80", "credits": 500},
        "year": {"period": "year", "months": 12, "amount": "190.08", "credits": 6000},
    }

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            state["requests"].append(self.path)
            value = {"ok": True, "service": "shengjian-payment", "plans": ["standard"], "license_alg": "EdDSA",
                "public_catalog": [{"id": "standard", "purchasable": True, "offers": [
                    {**offers[period], "currency": "CNY", "status": "ready", "purchasable": True}
                    for period in state["periods"]]}],
                "payments": {"redeem": state["open"]}, "login_methods": {"email": True},
                "buyer_verification": {"required": True, "configured": True, "provider": "afdian"},
                "agent_relay": {"configured": True, "stage_protocol": "v1"},
                "readiness": {"ready": True, "blocking": []}}
            wire = json.dumps(value).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(wire)))
            self.end_headers()
            self.wfile.write(wire)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    controller = SimpleNamespace(settings={"subscription_server_url": base,
        "subscription_license_public_key": base64.b64encode(bytes(32)).decode()})
    bridge = Bridge(controller, Mock(), SimpleNamespace(base_dir=str(tmp_path)), lambda: None, lambda: None)
    try:
        yield bridge, state, base
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


@pytest.mark.parametrize("period,months,credits,price", [
    ("month", 1, 500, "¥19.8/月"),
])
def test_bridge_links_exact_confirmed_period(purchase_service, period, months, credits, price):
    bridge, state, base = purchase_service
    result = bridge.begin_subscription("standard", period)
    assert result["ok"] is True
    assert result["period"] == period
    assert result["plan"]["period"] == period
    assert result["plan"]["price"] == price
    assert result["plan"]["credits"] == credits and result["plan"]["months"] == months
    link = urlsplit(result["checkout_url"])
    assert result["checkout_url"].startswith(base + "/checkout?")
    assert parse_qs(link.query) == {"plan": ["standard"], "period": [period]}
    assert state["requests"] == ["/v1/health"]


def test_year_only_does_not_fall_back_to_month(purchase_service):
    bridge, state, _ = purchase_service
    state["periods"] = ["year"]
    assert bridge.begin_subscription("standard")["ok"] is False
    annual = bridge.begin_subscription("standard", "year")
    assert annual['ok'] is False and 'checkout_url' not in annual


@pytest.mark.parametrize("period", ["", "year", "months", "lifetime", "year&plan=advanced", None])
def test_invalid_cycle_has_no_link_or_service_request(purchase_service, period):
    bridge, state, _ = purchase_service
    assert bridge.begin_subscription("standard", period)["ok"] is False
    assert checkout("standard", period=period)["ok"] is False
    assert not state["requests"]


def test_unavailable_or_duplicate_cycle_cannot_return_checkout_link(purchase_service):
    bridge, state, _ = purchase_service
    state["periods"] = ["month"]
    assert bridge.begin_subscription("standard", "year")["ok"] is False
    state["periods"] = ["month", "year", "year"]
    assert bridge.begin_subscription("standard", "year")["ok"] is False
    assert bridge.begin_subscription("standard", "month")["ok"] is True


def test_complete_configuration_with_payment_switch_off_has_no_link(purchase_service):
    bridge, state, _ = purchase_service
    state["open"] = False
    for period in ("month", "year"):
        result = bridge.begin_subscription("standard", period)
        assert result["ok"] is False and "checkout_url" not in result
