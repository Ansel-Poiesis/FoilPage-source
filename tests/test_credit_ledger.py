# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""积分账本：签名哈希链、幂等、余额与篡改检测。"""
from __future__ import annotations

from dataclasses import replace

import pytest

from engine.credit_ledger import (
    LedgerError,
    append,
    balance,
    make_signer,
    verify,
)


@pytest.fixture
def signer():
    return make_signer("device-bound-secret")


def _grant(entries, units, order_id, signer):
    return append(
        entries,
        id="order:" + order_id,
        type="grant",
        units=units,
        signer=signer,
        order_id=order_id,
    )[0]


def test_grant_consume_refund_keeps_chain_and_balance(signer):
    entries = _grant([], 1000, "af-001", signer)
    entries = append(entries, id="req-1", type="consume", units=3, signer=signer)[0]
    entries = append(entries, id="req-1-fail", type="refund", units=1, signer=signer)[0]
    assert balance(entries) == 998
    ok, broken = verify(entries, signer)
    assert ok and broken == 0
    assert [e.seq for e in entries] == [1, 2, 3]


def test_duplicate_id_is_idempotent(signer):
    entries = _grant([], 1000, "af-001", signer)
    grown, entry = append(entries, id="order:af-001", type="grant", units=1000, signer=signer, order_id="af-001")
    assert len(grown) == len(entries), "同一订单重复回调不得重复入账"
    assert entry.id == "order:af-001"
    assert balance(grown) == 1000


def test_consume_rejects_insufficient_balance_without_mutation(signer):
    entries = _grant([], 10, "af-001", signer)
    with pytest.raises(LedgerError, match="积分不足"):
        append(entries, id="req-x", type="consume", units=11, signer=signer)
    assert balance(entries) == 10
    ok, _ = verify(entries, signer)
    assert ok


def test_tampering_units_or_balance_breaks_chain(signer):
    entries = _grant([], 1000, "af-001", signer)
    entries = append(entries, id="req-1", type="consume", units=3, signer=signer)[0]
    forged = replace(entries[1], units=1, balance_after=999)
    ok, broken = verify([entries[0], forged], signer)
    assert not ok and broken == 2, "改小消耗必须被链校验拒绝"


def test_reordering_and_wrong_key_are_detected(signer):
    entries = _grant([], 1000, "af-001", signer)
    entries = append(entries, id="req-1", type="consume", units=3, signer=signer)[0]
    entries = append(entries, id="req-2", type="consume", units=4, signer=signer)[0]
    ok, broken = verify(list(reversed(entries)), signer)
    assert not ok and broken >= 1
    ok_other, _ = verify(entries, make_signer("attacker-key"))
    assert not ok_other, "换密钥重签的账本必须验证失败"


def test_invalid_entries_rejected(signer):
    with pytest.raises(LedgerError):
        append([], id="x", type="steal", units=1, signer=signer)
    with pytest.raises(LedgerError):
        append([], id="", type="grant", units=1, signer=signer)
    with pytest.raises(LedgerError):
        append([], id="x", type="grant", units=0, signer=signer)
    with pytest.raises(LedgerError, match="积分不足"):
        append([], id="x", type="consume", units=1, signer=signer)
