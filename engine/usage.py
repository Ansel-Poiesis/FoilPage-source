# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""托管 Agent 用量计量的纯函数内核。

BYOK 调用不应进入这个模块；服务端授权层负责判断调用来源，客户端只复用
这里的计量规则展示预估和执行本地开发验收。

积分粒度按本机实测校准：184 条真实转写的字/分钟中位数为 345（长视频
语速更慢），一分钟口播 ≈400 字、十分钟 ≈3,500 字、一小时 ≈14,000 字，
对应 1/2/8 积分（见 engine.subscription.QUOTA_EXAMPLES）。
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any, Mapping


STANDARD_CHAR_LIMIT = 2_000
# 更重模型 ×3 积分（先生 2026-11-03 口径）：deepseek-flash 为托管润色档，
# 服务端政策同步按 3 倍积分计费（relay-cost-policy credit_multiplier）。
_EXPENSIVE_MODEL_MARKERS = ("reasoner", "deepseek-r1", "deepseek-flash", "o1", "o3", "opus")


class UsageError(ValueError):
    """用量参数或额度状态无效。"""


class QuotaExceededError(UsageError):
    """本月额度不足。"""


def usage_period(now: datetime | None = None) -> str:
    """返回 UTC 自然月键，避免客户端本地时区变化导致重复计量。"""
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc).strftime("%Y-%m")


def estimate_agent_units(raw_text: str, model: str = "") -> int:
    """按输入长度和模型成本估算一次托管 Agent 调用消耗的标准单位。"""
    text = str(raw_text or "")
    if not text.strip():
        raise UsageError("原始转写为空，无法估算 Agent 用量")
    chunks = max(1, math.ceil(len(text) / STANDARD_CHAR_LIMIT))
    model_name = str(model or "").casefold()
    multiplier = 3 if any(marker in model_name for marker in _EXPENSIVE_MODEL_MARKERS) else 1
    return chunks * multiplier


def normalize_usage_state(state: Mapping[str, Any] | None, *, now: datetime | None = None) -> dict[str, Any]:
    """切换自然月时清零已用额度，保留不可变的额度上限字段。"""
    result = dict(state or {})
    period = usage_period(now)
    if result.get("usage_period") != period:
        result["usage_period"] = period
        result["usage_used"] = 0
    result["usage_used"] = max(0, int(result.get("usage_used") or 0))
    return result


def consume_usage(
    state: Mapping[str, Any] | None,
    quota: int,
    units: int,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """原子语义地预扣额度；不足时不修改调用方状态。"""
    if int(quota) < 0 or int(units) < 1:
        raise UsageError("额度上限必须非负，消耗单位必须为正数")
    result = normalize_usage_state(state, now=now)
    used = int(result["usage_used"])
    if used + int(units) > int(quota):
        raise QuotaExceededError(f"本月额度不足：还剩 {max(0, int(quota) - used)} 单位")
    result["usage_used"] = used + int(units)
    return result


def refund_usage(
    state: Mapping[str, Any] | None,
    units: int,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """对失败、取消或服务端错误回滚已预扣的额度。"""
    if int(units) < 1:
        raise UsageError("回滚单位必须为正数")
    result = normalize_usage_state(state, now=now)
    result["usage_used"] = max(0, int(result["usage_used"]) - int(units))
    return result
