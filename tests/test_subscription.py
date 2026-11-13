# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from engine.subscription import (
    FREE_ENTITLEMENTS,
    PLAN_MAP,
    activate_local,
    checkout,
    plans,
    status,
)


NOW = datetime(2026, 8, 15, tzinfo=timezone.utc)


def _active_settings(plan_id: str = "standard", *, used: int = 0, period: str = "2026-08", balance: int = 1000) -> dict:
    return {
        "subscription_plan": plan_id,
        "subscription_active": True,
        "subscription_expires_at": None,
        "agent_usage_period": period,
        "agent_usage_used": used,
        "credit_balance": balance,
    }


def test_plans_match_product_contract() -> None:
    """目录按章程收敛为标准/专业两个可购档位（永久版等暂缓，不在目录中）。"""
    table = {p["id"]: p for p in plans()}
    assert [p["id"] for p in plans()] == ["standard", "advanced"]
    assert [p["name"] for p in plans()] == ["标准版", "专业版"]
    assert [p["tier"] for p in plans()] == [1, 2]
    assert table["standard"]["price"] == "¥19.80/月"
    assert table["standard"]["annual_price"] == ""
    assert table["advanced"]["price"] == "¥49.80/月"
    assert table["advanced"]["annual_price"] == ""
    assert table["standard"]["monthly_agent_units"] == 500
    assert table["advanced"]["monthly_agent_units"] == 2000
    assert table["standard"]["audience"] == "个人高频用户"
    assert table["standard"]["unit_label"] == "积分"
    assert "1 积分" in table["standard"]["quota_note"]
    assert table["advanced"]["audience"] == "内容生产者"
    for plan in plans():
        assert plan["features"], plan["id"] + " 需要面向订阅页的权益列表"
        assert "hosted_agent" in plan["entitlements"]
        assert "visual_proofread" in plan["entitlements"]
    assert "export_enhanced" not in table["standard"]["entitlements"]
    assert "priority_support" in table["advanced"]["entitlements"]
    assert table["standard"]["max_concurrency"] == 2
    assert table["advanced"]["max_concurrency"] == 4
    advertised = table["advanced"]["description"] + " ".join(table["advanced"]["features"])
    assert "导出增强" not in advertised and "优先支持" not in advertised
    assert "包含标准版全部权益" in table["advanced"]["features"]
    assert "4 路并发转写" in table["advanced"]["features"]


def test_free_status_reports_zero_quota_and_local_entitlements() -> None:
    st = status({"subscription_plan": "free", "subscription_active": False}, now=NOW)
    assert st["active"] is False
    assert st["plan_id"] == "free"
    assert st["plan"] is None
    assert st["monthly_agent_units"] == 0
    assert st["agent_usage_used"] == 0
    assert st["agent_usage_remaining"] == 0
    assert st["entitlements"] == list(FREE_ENTITLEMENTS)
    assert "byok_agent" in st["entitlements"]
    assert st["credit_balance"] == 0
    assert st["max_concurrency"] == 1, "免费层并发为 1"
    assert st["agent_usage_period"]


def test_active_plan_reports_quota_used_and_remaining() -> None:
    st = status(_active_settings("standard", used=30), now=NOW)
    assert st["active"] is True
    assert st["plan_id"] == "standard"
    assert st["monthly_agent_units"] == 500
    assert st["credit_balance"] == 1000
    assert st["agent_usage_used"] == 30
    assert st["agent_usage_remaining"] == 1000, "剩余积分=购买余额，与当月统计无关"


def test_status_rolls_over_month_and_resets_used() -> None:
    st = status(_active_settings("advanced", used=700, period="2026-07", balance=5000), now=NOW)
    assert st["agent_usage_period"] == "2026-08"
    assert st["agent_usage_used"] == 0, "统计口径按自然月清零"
    assert st["credit_balance"] == 5000
    assert st["agent_usage_remaining"] == 5000, "购买积分余额不按月清零"


def test_retired_plan_no_longer_resolves_to_paid_state() -> None:
    """下架的 super/lifetime 不再激活；旧激活记录按免费层回落。"""
    st = status(_active_settings("super", used=10), now=NOW)
    assert st["active"] is False
    assert st["plan_id"] == "free"
    assert st["monthly_agent_units"] == 0
    assert st["entitlements"] == list(FREE_ENTITLEMENTS)
    assert checkout("lifetime")["ok"] is False


def test_expired_monthly_plan_falls_back_to_free() -> None:
    settings = _active_settings("advanced", used=5)
    settings["subscription_expires_at"] = (NOW - timedelta(days=1)).isoformat()
    st = status(settings, now=NOW)
    assert st["active"] is False
    assert st["plan_id"] == "free"
    assert st["monthly_agent_units"] == 0
    assert st["credit_balance"] == 0, "到期后余额冻结展示为 0，续购后恢复"
    assert st["plan_expiry_notice"]["expired"] is True


def test_expired_plan_notice_survives_refreshed_inactive_account() -> None:
    settings = _active_settings()
    settings.update(subscription_active=False, subscription_expires_at=(NOW-timedelta(days=1)).isoformat())
    st = status(settings, now=NOW)
    assert st["active"] is False
    assert st["plan_expiry_notice"]["expired"] is True


def test_expiry_notice_rejects_unverified_license_and_accepts_legacy_utc_date() -> None:
    settings = _active_settings()
    settings["subscription_expires_at"] = (NOW+timedelta(hours=6)).replace(tzinfo=None).isoformat()
    assert status(settings, now=NOW)["plan_expiry_notice"]["days"] == 0
    settings.update(subscription_license_public_key="invalid-public-key", license_token="invalid")
    assert status(settings, now=NOW, device_id="fixture")["plan_expiry_notice"] is None


def test_expired_notice_history_is_only_metadata_and_bound_to_account_service() -> None:
    settings = {"subscription_plan": "free", "subscription_active": False,
        "account_user_id": "fixture", "account_login_token": "synthetic",
        "subscription_server_url": "https://fixture.invalid",
        "subscription_expiry_notice_history": {"account": "fixture", "service": "https://fixture.invalid",
            "expires_at": (NOW-timedelta(days=1)).isoformat()}}
    value = status(settings, now=NOW)
    assert value["plan_expiry_notice"]["expired"] is True
    assert value["active"] is False and value["credit_balance"] == 0
    settings["account_user_id"] = "other"
    assert status(settings, now=NOW)["plan_expiry_notice"] is None
    settings["account_user_id"] = "fixture"
    settings["subscription_server_url"] = "https://other.invalid"
    assert status(settings, now=NOW)["plan_expiry_notice"] is None
    settings["subscription_server_url"] = "https://fixture.invalid"
    settings["subscription_expiry_notice_history"]["expires_at"] = (NOW+timedelta(hours=6)).isoformat()
    assert status(settings, now=NOW)["plan_expiry_notice"] is None


def test_active_renewal_takes_precedence_over_expired_notice_history() -> None:
    settings = _active_settings()
    settings["account_user_id"] = "fixture"
    settings["account_login_token"] = "synthetic"
    settings["subscription_server_url"] = "https://fixture.invalid"
    settings["subscription_expires_at"] = (NOW+timedelta(days=30)).isoformat()
    settings["subscription_expiry_notice_history"] = {
        "account": "fixture",
        "service": "https://fixture.invalid",
        "expires_at": (NOW-timedelta(days=1)).isoformat(),
    }
    assert status(settings, now=NOW)["plan_expiry_notice"] is None


def test_checkout_rejects_unknown_plan_and_builds_url() -> None:
    assert checkout("missing")["ok"] is False
    result = checkout("standard", checkout_base_url="https://pay.example.com/")
    assert result["ok"] is True
    assert result["checkout_url"] == "https://pay.example.com/checkout?plan=standard&period=month"
    assert result["provider"] == "afdian"
    assert checkout("standard", checkout_base_url="")["checkout_url"] == ""


def test_activate_local_requires_known_plan_and_uses_monthly_clock() -> None:
    with pytest.raises(ValueError):
        activate_local({}, "missing")
    with pytest.raises(ValueError):
        activate_local({}, "lifetime")
    activated = activate_local({}, "advanced")
    assert activated["subscription_plan"] == "advanced"
    assert activated["subscription_active"] is True
    assert activated["subscription_expires_at"], "演示激活同样按 30 天计时，到期回落免费层"
    expiry = datetime.fromisoformat(activated["subscription_expires_at"])
    st = status(activated, now=expiry + timedelta(days=1))
    assert st["active"] is False and st["plan_id"] == "free"


def test_extend_subscription_stacks_from_current_expiry_and_is_idempotent() -> None:
    from engine.subscription import extend_subscription

    first = extend_subscription({}, "standard", 1, now=NOW, order_id="af-1001")
    expires_first = datetime.fromisoformat(first["subscription_expires_at"])
    assert expires_first == NOW + timedelta(days=30)

    # 未过期时续费自到期时间顺延，剩余时长不丢失
    renewed = extend_subscription(first, "standard", 2, now=NOW + timedelta(days=10), order_id="af-1002")
    expires_renewed = datetime.fromisoformat(renewed["subscription_expires_at"])
    assert expires_renewed == expires_first + timedelta(days=60)

    # 同一订单号重复回调（网络重试/重复 webhook）不得重复发放
    again = extend_subscription(renewed, "standard", 2, now=NOW + timedelta(days=10), order_id="af-1002")
    assert again["subscription_expires_at"] == renewed["subscription_expires_at"]

    # 过期后再买从当前时间起算
    expired = dict(first, subscription_expires_at=(NOW - timedelta(days=1)).isoformat())
    late = extend_subscription(expired, "standard", 1, now=NOW + timedelta(days=5), order_id="af-1003")
    assert datetime.fromisoformat(late["subscription_expires_at"]) == NOW + timedelta(days=35)

    with pytest.raises(ValueError):
        extend_subscription({}, "super", 1)
    with pytest.raises(ValueError):
        extend_subscription({}, "standard", 0)


def test_consume_credits_deducts_balance_and_rejects_overdraft() -> None:
    from engine.subscription import consume_credits
    from engine.usage import QuotaExceededError

    state = _active_settings("standard", balance=500)
    after = consume_credits(state, 8, now=NOW)
    assert after["credit_balance"] == 492
    assert status(after, now=NOW)["agent_usage_remaining"] == 492
    assert state["credit_balance"] == 500, "扣减不得修改调用方状态"
    with pytest.raises(QuotaExceededError):
        consume_credits(after, 493, now=NOW)
    assert after["credit_balance"] == 492


def test_repeat_purchase_stacks_balance_and_expiry_idempotently() -> None:
    from engine.subscription import apply_purchase

    first = apply_purchase({}, "standard", order_id="af-001", now=NOW)
    assert first["credit_balance"] == 500
    # 重复购买：余额累加、时长顺延
    second = apply_purchase(first, "standard", order_id="af-002", now=NOW + timedelta(days=5))
    assert second["credit_balance"] == 1000
    assert datetime.fromisoformat(second["subscription_expires_at"]) == NOW + timedelta(days=60)
    # 同一订单重复核销（webhook 重推/用户重试）不得重复发放
    again = apply_purchase(second, "standard", order_id="af-002", now=NOW + timedelta(days=5))
    assert again["credit_balance"] == 1000
    with pytest.raises(ValueError):
        apply_purchase({}, "standard", order_id="")


def test_plan_map_exposes_same_objects_as_plans() -> None:
    assert PLAN_MAP["advanced"]["monthly_agent_units"] == 2000
    assert len(PLAN_MAP) == 2


def test_existing_signed_professional_balance_is_not_capped_by_new_purchase_default():
    import base64
    from engine import ed25519
    from engine.license import sign_license, verify_license
    seed = bytes(range(32))
    public = base64.b64encode(ed25519.public_key(seed)).decode()
    token = sign_license({"sub":"legacy-account","plan_id":"advanced","credit_balance":5000,
        "monthly_agent_units":5000,"entitlements":["hosted_agent","export_enhanced","priority_support"],
        "device_id":"legacy-device","exp":int((NOW+timedelta(days=30)).timestamp())},
        signing_key=base64.b64encode(seed).decode(),alg="EdDSA")
    settings = {**_active_settings("advanced",balance=5000),"subscription_license_public_key":public,"license_token":token}
    value = status(settings,now=NOW,device_id="legacy-device")
    assert value["active"] and value["credit_balance"]==5000 and value["agent_usage_remaining"]==5000
    assert PLAN_MAP["advanced"]["monthly_agent_units"]==2000
    payload=verify_license(token,signing_key=public,expected_alg="EdDSA",now=NOW.timestamp(),device_id="legacy-device")
    assert payload["entitlements"]==["hosted_agent","export_enhanced","priority_support"]


def test_plan_expiry_notice_windows_and_permanence():
    """套餐到期预警窗口（先生 2026-10-14 拍板）：0-3 天内/已过期返回载荷，
    永久与远期返回 None。"""
    from datetime import datetime, timedelta, timezone
    from engine.subscription import plan_expiry_notice
    now = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)
    assert plan_expiry_notice(None) is None
    assert plan_expiry_notice("") is None
    assert plan_expiry_notice("not-a-date") is None
    # 永久权益常见 None；远期（4 天以上）静默。
    far = (now + timedelta(days=4, hours=1)).isoformat()
    assert plan_expiry_notice(far, now=now) is None
    # 3 天边界内：返回剩余整天数与本地日期。
    soon = (now + timedelta(days=2, hours=3)).isoformat()
    notice = plan_expiry_notice(soon, now=now)
    assert notice == {"days": 2, "date": notice["date"], "expired": False}
    # 当天到期：days=0、未过期（还有小时余量）。
    tonight = (now + timedelta(hours=6)).isoformat()
    assert plan_expiry_notice(tonight, now=now) == {"days": 0, "date": plan_expiry_notice(tonight, now=now)["date"], "expired": False}
    # 已过期：expired=True。
    past = (now - timedelta(hours=2)).isoformat()
    expired = plan_expiry_notice(past, now=now)
    assert expired["expired"] is True and expired["days"] == 0
    # Z 后缀与无时区串均可解析。
    zulu = (now + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert plan_expiry_notice(zulu, now=now)["days"] == 1
