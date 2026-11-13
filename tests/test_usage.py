# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from engine.usage import (
    QuotaExceededError,
    STANDARD_CHAR_LIMIT,
    consume_usage,
    estimate_agent_units,
    normalize_usage_state,
    refund_usage,
    usage_period,
)


def test_standard_and_long_text_units() -> None:
    assert estimate_agent_units("字" * STANDARD_CHAR_LIMIT) == 1
    assert estimate_agent_units("字" * (STANDARD_CHAR_LIMIT + 1)) == 2
    assert estimate_agent_units("字", "deepseek-reasoner") == 3


def test_hosted_polish_model_charges_the_heavier_rate() -> None:
    """deepseek-flash 托管润色档按更重模型 ×3 计（与服务端政策一致）。"""
    assert estimate_agent_units("字" * STANDARD_CHAR_LIMIT, "deepseek-flash") == 3
    assert estimate_agent_units("字" * (STANDARD_CHAR_LIMIT + 1), "deepseek-flash") == 6
    # 普通档不受影响。
    assert estimate_agent_units("字" * STANDARD_CHAR_LIMIT, "qwen-plus") == 1


def test_month_rollover_resets_usage() -> None:
    state = {"usage_period": "2026-08", "usage_used": 99}
    result = normalize_usage_state(state, now=datetime(2026, 9, 1, tzinfo=timezone.utc))
    assert result["usage_period"] == "2026-09"
    assert result["usage_used"] == 0
    assert usage_period(datetime(2026, 9, 1, tzinfo=timezone.utc)) == "2026-09"


def test_consume_is_non_mutating_and_refund_restores_capacity() -> None:
    original = {"usage_period": "2026-08", "usage_used": 2}
    next_state = consume_usage(original, 5, 2, now=datetime(2026, 8, 23, tzinfo=timezone.utc))
    assert original["usage_used"] == 2
    assert next_state["usage_used"] == 4
    restored = refund_usage(next_state, 2, now=datetime(2026, 8, 23, tzinfo=timezone.utc))
    assert restored["usage_used"] == 2


def test_consume_rejects_over_quota_without_mutating_state() -> None:
    original = {"usage_period": "2026-08", "usage_used": 4}
    with pytest.raises(QuotaExceededError):
        consume_usage(original, 5, 2, now=datetime(2026, 8, 23, tzinfo=timezone.utc))
    assert original["usage_used"] == 4
