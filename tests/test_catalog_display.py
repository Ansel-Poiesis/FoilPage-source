# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""Bounded blocked prices can be shown without granting a payment capability."""
import json

import pytest

from app.bridge import _public_alipay_contract, _public_subscription_catalog


def blocked(**changes):
    return {"period": "month", "months": 1, "amount": "79.80", "credits": 3000,
            "currency": "CNY", "status": "merchant_limit", "purchasable": False, **changes}


def catalog(offers):
    return [{"id": "advanced", "status": "unavailable", "purchasable": False,
             "provider_secret": "not-public", "offers": offers}]


def test_blocked_prices_and_credits_survive_without_opening_purchase():
    value = {"provider": "alipay_page_pay", "configured": True, "enabled": True,
             "processing_enabled": True, "max_order_amount_minor": 5000,
             "catalog": catalog([blocked(), blocked(period="year", months=12,
                                                      amount="766.08", credits=36000)])}
    result = _public_alipay_contract(value)
    assert result["enabled"] is True
    plan = result["catalog"][0]
    assert plan["price"] == "¥79.8/月" and plan["annual_price"] == "¥766.08/年"
    assert plan["purchasable"] is False and plan["status"] == "unavailable"
    assert [(q["amount"], q["credits"], q["status"], q["purchasable"]) for q in plan["offers"]] == [
        ("79.80", 3000, "merchant_limit", False), ("766.08", 36000, "merchant_limit", False)]
    assert "not-public" not in json.dumps(result)


@pytest.mark.parametrize("changes", [
    {"amount": "NaN"}, {"amount": "-79.80"}, {"amount": "79.801"},
    {"amount": "100000000.01"}, {"amount": "0.00"}, {"amount": True},
    {"currency": "USD"}, {"credits": True}, {"credits": 0}, {"credits": -1},
    {"credits": "3000"}, {"credits": 2**53}, {"purchasable": "false"},
    {"purchasable": True}, {"months": True}, {"period": "year", "months": 1},
])
def test_invalid_blocked_price_is_not_disclosed_or_made_purchasable(changes):
    plan = _public_subscription_catalog(catalog([blocked(**changes)]))[0]
    assert not plan["purchasable"]
    assert all(q["amount"] is None and q["purchasable"] is False for q in plan["offers"])


def test_duplicate_blocked_prices_remain_ambiguous():
    plan = _public_subscription_catalog(catalog([blocked(), blocked(amount="99.80")]))[0]
    assert not plan["purchasable"]
    assert all(q["status"] == "ambiguous_price" and q["amount"] is None for q in plan["offers"])


def test_merchant_status_never_becomes_payable_even_if_plan_claims_ready():
    value = catalog([blocked()])
    value[0].update(status="ready", purchasable=True)
    result = _public_subscription_catalog(value)[0]
    assert result["offers"][0]["amount"] == "79.80"
    assert result["offers"][0]["purchasable"] is False
    assert result["purchasable"] is False
