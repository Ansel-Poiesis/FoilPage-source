# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""FoilPage 订阅套餐与本地权益模型。

支付供应商只负责把订单状态回调到本地；正式环境应由服务端签发
subscription_status/entitlements，本模块不保存任何支付密钥。

套餐目录按《正式上架付费功能章程》（2026-09-19）收敛为两个可购档位：
标准版与专业版；永久版/团队版/跨设备同步在退款、盗用与迁移规则稳定后再开放。

额度以「积分」计：1 积分 = 2,000 字标准整理（重模型 ×3，见 engine.usage）。
积分是用量天花板而非成本预算——托管模型由服务端指定，实际成本按真实
用量分布与公平使用条款控制（章程「用量账本」与放行门槛）。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from engine.usage import normalize_usage_state

# 免费层常驻权益：未订阅时的本地能力（含 BYOK），对应章程「产品边界」。
FREE_ENTITLEMENTS: tuple[str, ...] = ("transcribe", "byok_agent", "local_batch")

# 转写并发档位（先生 2026-09-26 定调）：免费 1、标准 2、专业 4。
FREE_CONCURRENCY = 1

# 订阅月长度：付费计时按 30 天自然月叠加（见 extend_subscription）。
SUBSCRIPTION_MONTH_DAYS = 30

QUOTA_NOTE = "1 积分 ≈ 2,000 字整理，更重模型 ×3 消耗"

# 换算示例按实测口径组织：本机 184 条真实转写，字/分钟中位 345（长视频语速更慢）。
QUOTA_EXAMPLES: tuple[tuple[str, str, str], ...] = (
    ("一分钟口播", "≈400 字", "1 积分"),
    ("十分钟视频", "≈3,500 字", "2 积分"),
    ("一小时长视频", "≈14,000 字", "8 积分"),
)

PLANS: tuple[dict[str, Any], ...] = (
    {
        "id": "standard",
        "name": "标准版",
        "tier": 1,
        "audience": "个人高频用户",
        "price": "¥19.80/月",
        "annual_price": "",
        "period": "month",
        "monthly_agent_units": 500,
        "max_concurrency": 2,
        "unit_label": "积分",
        "quota_note": QUOTA_NOTE,
        "description": "每次购买 500 积分，可重复购买；视觉校对与批量整理",
        "features": (
            "本地转写与自带密钥整理",
            "500 积分/次，可重复购买",
            "字幕视觉校对",
            "批量整理任务",
            "2 路并发转写",
        ),
        "entitlements": (
            "transcribe", "byok_agent", "local_batch",
            "hosted_agent", "visual_proofread", "batch_organize",
        ),
    },
    {
        "id": "advanced",
        "name": "专业版",
        "tier": 2,
        "audience": "内容生产者",
        "price": "¥49.80/月",
        "annual_price": "",
        "period": "month",
        "monthly_agent_units": 2000,
        "max_concurrency": 4,
        "unit_label": "积分",
        "quota_note": QUOTA_NOTE,
        "description": "每次购买 2,000 积分，可重复购买；标准版权益与 4 路并发转写",
        "features": (
            "包含标准版全部权益",
            "2,000 积分/次，可重复购买",
            "4 路并发转写",
        ),
        "entitlements": (
            "transcribe", "byok_agent", "local_batch",
            "hosted_agent", "visual_proofread", "batch_organize",
            # Legacy license tags remain compatible; no implemented export or
            # priority-support capability is advertised by these tags.
            "export_enhanced", "priority_support",
        ),
    },
)
PLAN_MAP = {p["id"]: p for p in PLANS}


def plans() -> list[dict[str, Any]]:
    return [
        dict(
            p,
            entitlements=list(p["entitlements"]),
            features=list(p["features"]),
            quota_examples=[list(example) for example in QUOTA_EXAMPLES],
        )
        for p in PLANS
    ]


def status(settings: Mapping[str, Any], *, now: datetime | None = None, device_id: str | None = None) -> dict[str, Any]:
    """Return the current entitlement state at ``now`` (UTC by default).

    Keeping the clock injectable makes the natural-month contract deterministic
    for tests and for callers that need to reconcile a recorded event.
    """
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    current = current.astimezone(timezone.utc)
    plan_id = str(settings.get("subscription_plan") or "free")
    active = bool(settings.get("subscription_active", False))
    notice_eligible = plan_id in PLAN_MAP
    verified_balance = None
    signed_concurrency = None
    beta_access = {"version": 1, "active": False, "unlimited_credits": False, "expires_at": None, "revoked": False}
    order_review = {"paused": False, "count": 0, "paused_orders": 0, "message": ""}
    public_key = str(settings.get("subscription_license_public_key") or "").strip()
    license_verified = not public_key
    if public_key:
        # Production entitlements come from a current signed license. A stale
        # local boolean or edited balance cannot grant paid capabilities.
        from engine.license import LicenseError, verify_license, signed_max_concurrency, signed_beta_access
        try:
            if not device_id:
                raise LicenseError("未提供当前设备标识")
            payload = verify_license(str(settings.get("license_token") or ""),
                signing_key=public_key, expected_alg="EdDSA", now=current.timestamp(), device_id=device_id)
            expected_account = str(settings.get("account_user_id") or "").strip()
            if expected_account and payload.get("sub") != expected_account:
                raise LicenseError("许可证与当前账户不匹配")
            signed_concurrency = signed_max_concurrency(payload)
            if (expected_account and str(settings.get("account_login_token") or "").strip()
                    and payload.get("sub") == expected_account and payload.get("device_id") == device_id):
                beta_access = signed_beta_access(payload, now=current.timestamp())
            plan_id = str(payload.get("plan_id") or "free")
            notice_eligible = plan_id in PLAN_MAP
            raw_balance = payload.get("credit_balance", 0)
            if type(raw_balance) is not int or raw_balance < 0:
                raise LicenseError("签名积分余额无效")
            verified_balance = raw_balance
            license_verified = True
            review = payload.get("order_review")
            if isinstance(review, dict) and review.get("paused") is True:
                # The public service exposes a count. Older signed licenses
                # used an integer (or an order list); never expose order IDs.
                count = review.get("count")
                if not isinstance(count, int) or isinstance(count, bool) or count < 0:
                    legacy = review.get("paused_orders", 0)
                    count = len(legacy) if isinstance(legacy, list) else legacy
                if not isinstance(count, int) or isinstance(count, bool) or count < 0:
                    count = 0
                order_review = {"paused": True,
                    "count": count, "paused_orders": count,
                    "message": "订单权益正在复核，托管已暂停，积分记录保留，请联系支持"}
                active = False
                beta_access = {**beta_access, "active": False, "unlimited_credits": False}
        except (LicenseError, TypeError, ValueError, OverflowError):
            active = False
            notice_eligible = False
            beta_access = {**beta_access, "active": False, "unlimited_credits": False}
    plan = PLAN_MAP.get(plan_id) if active else None
    if active and plan is None:
        # 已下架/未知套餐不再视为已订阅，回落免费层。
        active = False
    expires = settings.get("subscription_expires_at")
    if plan and plan["period"] != "lifetime" and expires:
        try:
            deadline = datetime.fromisoformat(str(expires).replace("Z", "+00:00"))
            if deadline.tzinfo is None:
                deadline = deadline.replace(tzinfo=timezone.utc)
            active = deadline > current
        except ValueError:
            active = False
    state = normalize_usage_state({
        "usage_period": settings.get("agent_usage_period"),
        "usage_used": settings.get("agent_usage_used", 0),
    }, now=current)
    quota = int(plan.get("monthly_agent_units", 0)) if active and plan else 0
    # 余额模型：购买积分入账（credit_balance），消耗直接扣减；月度用量仅作统计。
    balance = (verified_balance if verified_balance is not None else int(settings.get("credit_balance", 0) or 0)) if active else (
        verified_balance if (order_review["paused"] or beta_access["active"]) and verified_balance is not None else 0)
    notice = plan_expiry_notice(expires, now=current) if notice_eligible else None
    # A refreshed expired account is signed as free and has no expiry. Keep only
    # an account/service-bound reminder of a previously observed paid deadline;
    # this metadata never restores balance, concurrency or paid entitlements.
    history = settings.get("subscription_expiry_notice_history")
    if not active and not notice and license_verified and isinstance(history, dict):
        if (history.get("account") == settings.get("account_user_id")
                and history.get("service") == str(settings.get("subscription_server_url") or "").rstrip("/")
                and settings.get("account_login_token")):
            previous = plan_expiry_notice(history.get("expires_at"), now=current)
            if previous and previous["expired"]:
                notice = previous
    if not license_verified or order_review["paused"] or beta_access["active"]:
        notice = None
    return {
        "active": active,
        # 本地 license 是否通过验签（含有效期）。False + 仍有登录令牌 =
        # 可自愈状态：续期 license 即可，不应把用户顶到登录墙。
        "license_verified": license_verified,
        "plan_id": plan_id if active else "free",
        "plan": dict(plan) if active and plan else None,
        "expires_at": expires if active else None,
        "entitlements": list(plan["entitlements"]) if active and plan else list(FREE_ENTITLEMENTS),
        "monthly_agent_units": quota,
        "max_concurrency": (signed_concurrency if signed_concurrency is not None else int(plan["max_concurrency"])) if active and plan else FREE_CONCURRENCY,
        "credit_balance": balance,
        "beta_access": beta_access,
        "unlimited_credits": beta_access["unlimited_credits"],
        "order_review": order_review,
        "plan_expiry_notice": notice,
        "agent_usage_period": state["usage_period"],
        "agent_usage_used": state["usage_used"],
        "agent_usage_remaining": 0 if order_review["paused"] else balance,
    }


def checkout(plan_id: str, *, checkout_base_url: str = "", period: str = "month") -> dict[str, Any]:
    if plan_id not in PLAN_MAP:
        return {"ok": False, "error": "套餐不存在"}
    if period != "month":
        return {"ok": False, "error": "购买周期暂未开放"}
    # 占位 URL：部署支付服务后将 checkout_base_url 指向后端，不在客户端放密钥。
    url = (checkout_base_url.rstrip("/") + "/checkout?plan=" + plan_id + "&period=" + period) if checkout_base_url.strip() else ""
    return {"ok": True, "plan": dict(PLAN_MAP[plan_id]), "period": period, "checkout_url": url, "provider": "afdian"}


def activate_local(settings: Mapping[str, Any], plan_id: str) -> dict[str, Any]:
    """仅供本地开发验收；生产环境必须由支付服务核销订单后调用。

    演示激活等价一次购买：+30 天有效期并按档位入账积分，可重复执行
    以验证「重复购买」链路。
    """
    import uuid

    return apply_purchase(settings, plan_id, order_id=f"local-{uuid.uuid4().hex}")


def extend_subscription(
    settings: Mapping[str, Any],
    plan_id: str,
    months: int,
    *,
    now: datetime | None = None,
    order_id: str = "",
) -> dict[str, Any]:
    """付费计时：按 30 天/月叠加有效期；同一订单号幂等，重复回调不重复发放。

    生产环境由支付回调在服务端调用（或由许可证携带）；续费自当前到期时间
    顺延，未过期的剩余时长不丢失。
    """
    if plan_id not in PLAN_MAP:
        raise ValueError("套餐不存在")
    if int(months) < 1:
        raise ValueError("购买月数必须为正数")
    if order_id and str(settings.get("subscription_last_order_id") or "") == str(order_id):
        return dict(settings)
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    current = current.astimezone(timezone.utc)
    base = current
    existing = settings.get("subscription_expires_at")
    if existing:
        try:
            previous = datetime.fromisoformat(str(existing).replace("Z", "+00:00"))
            if previous.tzinfo is None:
                previous = previous.replace(tzinfo=timezone.utc)
            if previous > current:
                base = previous
        except ValueError:
            pass
    expires = base + timedelta(days=SUBSCRIPTION_MONTH_DAYS * int(months))
    result = dict(settings)
    result.update({
        "subscription_plan": plan_id,
        "subscription_active": True,
        "subscription_expires_at": expires.astimezone(timezone.utc).isoformat(),
    })
    if order_id:
        result["subscription_last_order_id"] = str(order_id)
    return result


def apply_purchase(
    settings: Mapping[str, Any],
    plan_id: str,
    *,
    order_id: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """一次购买 = 时长 +30 天顺延 + 档位积分入账；同一订单号幂等。

    标准版 500 积分/次、专业版 2,000 积分/次，均可重复购买：余额不清零、
    按次累加，到期未用完的积分冻结至下次购买（时长顺延后恢复可用）。
    生产环境由支付服务在核销订单后调用。
    """
    if not str(order_id or "").strip():
        raise ValueError("购买必须携带订单号")
    if str(settings.get("subscription_last_order_id") or "") == str(order_id):
        return dict(settings)
    result = extend_subscription(settings, plan_id, 1, now=now, order_id=order_id)
    grant = int(PLAN_MAP[plan_id]["monthly_agent_units"])
    result["credit_balance"] = int(result.get("credit_balance", 0) or 0) + grant
    return result


def plan_expiry_notice(expires_at: object, *, now: datetime | None = None) -> dict | None:
    """套餐到期预警（先生 2026-10-14 拍板）：距到期不足 3 天或已过期时返回
    {"days": 剩余整天数, "date": 本地日期, "expired": 是否已过期}；
    永久/无日期/距离尚远返回 None。调用方负责"每天最多提示一次"的去重。"""
    raw = str(expires_at or "").strip()
    if not raw:
        return None
    try:
        deadline = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=timezone.utc)
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    if deadline <= moment:
        return {"days": 0, "date": deadline.astimezone().strftime("%Y-%m-%d"), "expired": True}
    remaining = (deadline - moment).days
    if remaining >= 3:
        return None
    return {"days": remaining, "date": deadline.astimezone().strftime("%Y-%m-%d"), "expired": False}


def consume_credits(
    settings: Mapping[str, Any],
    units: int,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """消耗购买积分（余额扣减）；不足时抛 QuotaExceededError 且不修改状态。

    agent_usage_used 同步累加，仅作自然月统计口径，不再决定剩余额度。
    """
    if int(units) < 1:
        raise ValueError("消耗积分必须为正数")
    balance = int(settings.get("credit_balance", 0) or 0)
    if balance < int(units):
        from engine.usage import QuotaExceededError

        raise QuotaExceededError(f"积分不足：剩余 {balance}，需 {int(units)}")
    result = dict(settings)
    result["credit_balance"] = balance - int(units)
    state = normalize_usage_state({
        "usage_period": settings.get("agent_usage_period"),
        "usage_used": settings.get("agent_usage_used", 0),
    }, now=now)
    result["agent_usage_period"] = state["usage_period"]
    result["agent_usage_used"] = int(state["usage_used"]) + int(units)
    return result
