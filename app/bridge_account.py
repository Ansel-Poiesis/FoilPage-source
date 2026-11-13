# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""登录与账号域的 Bridge 混入（bridge.py 第一阶段拆分）。

微信/谷歌/邮箱登录、设备管理与账号上下文相关的方法集中于此，通过
AccountBridgeMixin 混入 Bridge，实例 API 面不变；模块级常量与公共
辅助函数仍在 app/bridge.py 单处维护。
"""
from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation
from datetime import datetime, timezone
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from engine.service_http import payment_service_headers


TERMINAL_STATUSES = {"done", "failed", "cancelled"}

RELEASE_CHANNEL = "formal"


def _subscription_session_rejected(value: object) -> bool:
    """Only an explicit account-service rejection invalidates offline login.

    A platform/WAF 403 or a concurrent-license 409 is not a session verdict.
    Keep compatibility with the Python service's exact legacy error messages.
    """
    if (not isinstance(value, dict) or value.get("ok") is not False
            or set(value) - {"ok", "status", "error"}
            or any(key in value and not isinstance(value[key], str) for key in ("status", "error"))):
        return False
    if value.get("status") == "device_limit":
        return True
    if "status" in value and value["status"] != "invalid":
        return False
    return value.get("error") in {
        "未登录，请重新登录", "登录已过期，请重新登录", "登录已失效，请重新登录",
        "设备已解绑，请重新登录", "请先完成设备管理并重新登录", "未登录，无法刷新许可证",
    }


def _public_subscription_catalog(value: object) -> list[dict]:
    """Accept bounded public prices, never provider IDs or arbitrary health fields."""
    if not isinstance(value, list):
        return []
    catalog = []
    seen = set()
    labels = {"missing_plan": "暂未开放", "missing_price": "金额待配置",
              "ambiguous_price": "方案待确认", "unsupported_period": "暂未开放", "missing_credits": "积分额度待确认",
              "merchant_limit": "超过单笔支付限额"}
    for item in value[:10]:
        if not isinstance(item, dict) or item.get("id") not in ("standard", "advanced"):
            continue
        if item["id"] in seen:
            continue
        seen.add(item["id"])
        offers = []
        for offer in (item.get("offers") if isinstance(item.get("offers"), list) else [])[:12]:
            if not isinstance(offer, dict):
                continue
            period, months = offer.get("period"), offer.get("months")
            if isinstance(months, bool) or not isinstance(months, int) or not 1 <= months <= 120:
                continue
            if period not in ("month", "year", "months") or (period == "month" and months != 1) or (period == "year" and months != 12):
                continue
            state = str(offer.get("status") or "missing_price")
            amount = None
            ready = False
            blocked_price = state == "merchant_limit" and offer.get("purchasable") is False
            if (state == "ready" and offer.get("purchasable") is True or blocked_price) and period in ("month", "year") and offer.get("currency") == "CNY":
                raw = offer.get("amount")
                if isinstance(raw, str) and re.fullmatch(r"\d{1,12}(?:\.\d{1,2})?", raw):
                    try:
                        numeric = Decimal(raw)
                        if numeric > 0 and (not blocked_price or numeric <= Decimal("100000000.00")):
                            amount = format(numeric.quantize(Decimal("0.01")), "f")
                            ready = not blocked_price
                    except InvalidOperation:
                        pass
            credits = offer.get("credits")
            credits = credits if isinstance(credits, int) and not isinstance(credits, bool) and 0 < credits <= 2**53-1 else None
            if blocked_price and (amount is None or credits is None):
                state = "missing_price" if amount is None else "missing_credits"
                amount = None
            if ready and credits is None:
                ready = False
                state = "missing_credits"
            if not ready and state not in labels:
                state = "missing_price"
            suffix = "月" if period == "month" else "年" if period == "year" else f"{months}个月"
            display_amount = amount.rstrip("0").rstrip(".") if amount and "." in amount else amount
            offers.append({"period": period, "months": months, "amount": amount, "currency": "CNY", "credits": credits,
                           "price": f"¥{display_amount}/{suffix}" if ready or state == "merchant_limit" and amount is not None else labels.get(state, "金额待配置"),
                           "status": "ready" if ready else state, "purchasable": ready})
        # Duplicate offers for a period make the advertised price ambiguous.
        for period in ("month", "year"):
            matching = [offer for offer in offers if offer["period"] == period]
            if len(matching) > 1:
                for offer in matching:
                    offer.update(amount=None, price="金额待确认", status="ambiguous_price", purchasable=False)
        month = next((offer for offer in offers if offer["period"] == "month"), None)
        year = next((offer for offer in offers if offer["period"] == "year"), None)
        purchasable = item.get("purchasable") is True and any(offer["purchasable"] for offer in offers)
        catalog.append({"id": item["id"], "price": month["price"] if month else "月付暂未开放",
                        "annual_price": year["price"] if year else "年付暂未开放",
                        "offers": offers, "purchasable": purchasable,
                        "status": "ready" if purchasable else "unavailable"})
    return catalog


def _public_manual_payment_request(value: object) -> dict | None:
    """Only a bounded payment snapshot reaches the WebView, never account data."""
    if not isinstance(value, dict):
        return None
    request_id, association = value.get("request_id"), value.get("association_code")
    revision, state, quote = value.get("revision"), value.get("status"), value.get("snapshot")
    if (not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{12,64}", request_id)
            or not isinstance(association, str) or not re.fullmatch(r"[A-Za-z0-9_-]{4,64}", association)
            or isinstance(revision, bool) or not isinstance(revision, int) or revision < 0
            or state not in ("pending_payment", "pending_review", "credited", "cancelled", "rejected", "expired")
            or not isinstance(quote, dict)):
        return None
    period, months, amount, credits = quote.get("period"), quote.get("months"), quote.get("amount"), quote.get("credits")
    if (quote.get("plan_id") not in ("standard", "advanced") or period not in ("month", "year")
            or isinstance(months, bool) or months != (1 if period == "month" else 12) or quote.get("currency") != "CNY"
            or not isinstance(amount, str) or not re.fullmatch(r"\d{1,12}(?:\.\d{1,2})?", amount)
            or Decimal(amount) <= 0 or isinstance(credits, bool) or not isinstance(credits, int) or not 0 < credits <= 2**53-1):
        return None
    result = {"request_id": request_id, "association_code": association, "status": state, "revision": revision,
              "snapshot": {"plan_id": quote["plan_id"], "period": period, "months": months,
                           "amount": amount, "credits": credits, "currency": "CNY"},
              "created_at": str(value.get("created_at") or "")[:64], "expires_at": str(value.get("expires_at") or "")[:64]}
    last = value.get("receipt_last4")
    if isinstance(last, str) and re.fullmatch(r"[0-9]{4}", last):
        result["receipt_last4"] = last
    return result



def _alipay_cashier_expiry(base: str, url: object, expires: object) -> float | None:
    """Only a short capability at the configured HTTPS service can be opened."""
    if (not isinstance(url, str) or len(url) > 2048 or re.search(r"[\x00-\x20\x7f\\]", url + base)
            or not isinstance(expires, str) or len(expires) > 64):
        return None
    try:
        source, target = urllib.parse.urlsplit(base), urllib.parse.urlsplit(url)
        if (source.scheme != "https" or target.scheme != "https" or not source.hostname
                or source.username or source.password or target.username or target.password
                or source.hostname != target.hostname or source.port != target.port
                or target.path != "/v1/alipay/cashier" or target.fragment
                or not re.fullmatch(r"token=cap_alipay_[0-9a-f]{64}", target.query)):
            return None
        deadline = datetime.fromisoformat(expires.replace("Z", "+00:00"))
        if deadline.tzinfo is None:
            return None
        seconds = deadline.timestamp()
        remaining = seconds - datetime.now(timezone.utc).timestamp()
        return seconds if 0 < remaining <= 660 else None
    except (ValueError, OverflowError):
        return None


def _safe_task_error_details(error: object) -> dict[str, Any] | None:
    """Expose structured failure data to the UI without leaking signed media URLs."""
    if not isinstance(error, dict):
        return None

    def clean(value: object, depth: int = 0) -> object:
        if depth > 3:
            return "…"
        if isinstance(value, dict):
            result = {}
            for raw_key, child in list(value.items())[:32]:
                key = str(raw_key)[:64]
                if re.search(r"url|cookie|authorization|token|signature|header", key, re.IGNORECASE):
                    continue
                result[key] = clean(child, depth + 1)
            return result
        if isinstance(value, (list, tuple)):
            return [clean(child, depth + 1) for child in value[:10]]
        if isinstance(value, str):
            text = re.sub(r"https?://[^\s<>\"']+", "[URL 已脱敏]", value, flags=re.IGNORECASE)
            text = re.sub(
                r"(?i)(cookie|authorization|token|signature|x-bogus|mstoken|verifyfp)\s*[:=]\s*[^\s,;&]+",
                r"\1=[已脱敏]", text,
            )
            return text[:1000]
        if value is None or isinstance(value, (bool, int, float)):
            return value
        return str(value)[:200]

    result = {
        "code": clean(error.get("code", "")),
        "message": clean(error.get("message", "")),
        "retryable": bool(error.get("retryable", False)),
    }
    diagnostic = error.get("diagnostic")
    if isinstance(diagnostic, dict):
        result["diagnostic"] = clean(diagnostic)
    reports = error.get("incident_reports")
    if isinstance(reports, list):
        result["incident_reports"] = clean(reports)
    return result


_PASTED_URL_PATTERN = re.compile(
    r"https?://[^\s<>\"'\u3000-\u303f\u4e00-\u9fff\uff00-\uffef]+",
    re.IGNORECASE,
)


def _task_progress_text(task: dict) -> str:
    status = task.get("status", "")
    if task.get("kind") == "download":
        if status == "done":
            return "已下载" if not (task.get("final_path") or task.get("raw_path")) else "已完成"
        return {
            "received": "等待浏览器提交", "resolving": "解析链接",
            "awaiting_browser": "等待浏览器下载",
            "downloading": f"正在下载 {_safe_progress(task.get('progress')):.0%}",
            "imported": "正在入库", "queued": "等待转写",
            "transcribing": f"正在转写 {_safe_progress(task.get('progress')):.0%}",
            "running": f"正在转写 {_safe_progress(task.get('progress')):.0%}",
            "failed": "失败", "retryable": "等待重试",
            "cancelled": "已取消",
        }.get(status, "处理中")
    return {
        "done": "已完成", "failed": "失败", "cancelled": "已取消",
        "queued": "等待中", "retryable": "等待重试",
        "transcribing": f"正在转写 {_safe_progress(task.get('progress')):.0%}",
        "running": f"正在转写 {_safe_progress(task.get('progress')):.0%}",
    }.get(status, "-")


def _task_media_meta(task: dict) -> tuple[str, str]:
    kind = task.get("kind")
    if kind == "download":
        return "↓", "浏览器下载"
    return "▶", f"媒体（{task.get('device', '')}）"


def _browser_extension_dir() -> Path:
    """Bundled MV3 browser extension directory (dev source or frozen install)."""
    if getattr(sys, "frozen", False):
        base = Path(sys.executable).resolve().parent
    else:
        base = Path(__file__).resolve().parents[1]
    return base / "browser-extension"



def _safe_progress(value: object) -> float:
    """Non-finite / non-numeric progress must never crash the bridge.

    定义权随账号域迁到本模块；app/bridge.py 反向引用同一实现。
    """
    try:
        num = float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0
    if num != num or num in (float("inf"), float("-inf")):  # NaN / inf
        return 0.0
    return round(max(0.0, min(1.0, num)), 4)


# 版本与状态常量、公共视图辅助的唯一事实源在 app/bridge.py（release.ps1
# 逐字校验 APP_VERSION），本文件不得复制这些定义。

class AccountBridgeMixin:
    def wechat_login_start(self) -> dict:
        """发起微信扫码登录：服务端生成会话并在系统浏览器打开二维码页。"""
        import urllib.request
        import webbrowser

        from engine.device import device_id

        settings = self.controller.settings
        base = str(settings.get("subscription_server_url") or "").strip().rstrip("/")
        if not base:
            return {"ok": False, "error": "尚未配置付费服务地址（subscription_server_url）"}
        existing_session = str(settings.get("account_login_token") or "")
        existing_pending = str(settings.get("account_pending_login_token") or "")
        revision = self._account_revision
        device = device_id(str(getattr(self.settings_store, "base_dir", "") or "."))
        body = json.dumps({"device_id": device}, ensure_ascii=False).encode("utf-8")
        try:
            request = urllib.request.Request(
                base + "/v1/auth/wechat/start", data=body,
                headers=payment_service_headers(), method="POST",
            )
            with urllib.request.urlopen(request, timeout=15) as response:
                result = json.loads(response.read().decode("utf-8"))
        except Exception as exc:
            return {"ok": False, "error": f"登录服务不可达: {exc}"}
        if not isinstance(result, dict) or not result.get("ok"):
            return {"ok": False, "error": str((result or {}).get("error") or "微信登录暂不可用")}
        if not self._valid_account_auth_url(result.get("auth_url"), "wechat"):
            return {"ok": False, "error": "登录授权地址无效，请检查服务配置"}
        applied = self._save_pending_account_login(result, base, existing_session, existing_pending, revision)
        if not applied["ok"]:
            return applied
        try:
            webbrowser.open(str(result.get("auth_url") or ""))
        except Exception:
            pass
        result["ok"] = True
        return result

    def wechat_login_poll(self) -> dict:
        """轮询登录结果；成功即验证服务端许可证并写入本地订阅状态。"""
        import urllib.request

        from engine.device import device_id

        settings = self.controller.settings
        base = str(settings.get("subscription_server_url") or "").strip().rstrip("/")
        pending = str(settings.get("account_pending_login_token") or "").strip()
        existing_session = str(settings.get("account_login_token") or "")
        revision = self._account_revision
        profile_revision = self._profile_revision
        token = pending or str(settings.get("account_login_token") or "").strip()
        if not base or not token:
            return {"ok": False, "status": "invalid"}
        body = json.dumps({"login_token": token, "session_id": pending or token,
            "device_id": device_id(str(getattr(self.settings_store, "base_dir", "") or "."))}, ensure_ascii=False).encode("utf-8")
        try:
            request = urllib.request.Request(
                base + "/v1/auth/poll", data=body,
                headers=payment_service_headers(), method="POST",
            )
            with urllib.request.urlopen(request, timeout=15) as response:
                result = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            try:
                failure = json.loads(exc.read(65536).decode("utf-8"))
                if isinstance(failure, dict) and failure.get("status") in ("cancelled", "denied", "error", "account_link_required", "invalid"):
                    return failure
            except Exception:
                pass
            return {"ok": False, "status": "error", "error": "授权未完成，请重新登录"}
        except Exception as exc:
            return {"ok": False, "status": "unreachable", "error": f"登录服务不可达: {exc}"}
        if not isinstance(result, dict) or result.get("status") != "ok":
            if isinstance(result, dict) and result.get("status") == "device_limit":
                return self._remember_device_management(result, base=base,
                    expected_session=existing_session,
                    expected_pending=pending, expected_revision=revision)
            return result or {"ok": False, "status": "invalid"}
        applied = self._apply_remote_account(result, expected_pending=token if pending else None,
                                              expected_session=token if not pending else None, expected_base=base,
                                              expected_revision=revision, expected_profile_revision=profile_revision)
        if not applied.get("ok"):
            return {**applied, "status": "bad_license"}
        result["ok"] = True
        return result

    def email_login_start(self, email: str) -> dict:
        """请求邮箱登录验证码（联调 DEV 模式下返回 dev_code）。"""
        import urllib.request

        from engine.device import device_id

        settings = self.controller.settings
        base = str(settings.get("subscription_server_url") or "").strip().rstrip("/")
        if not base:
            return {"ok": False, "error": "尚未配置付费服务地址（subscription_server_url）"}
        body = json.dumps({
            "email": str(email or "").strip(),
            "device_id": device_id(str(getattr(self.settings_store, "base_dir", "") or ".")),
        }, ensure_ascii=False).encode("utf-8")
        try:
            request = urllib.request.Request(
                base + "/v1/auth/email/start", data=body,
                headers=payment_service_headers(), method="POST",
            )
            with urllib.request.urlopen(request, timeout=15) as response:
                result = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            try:
                return json.loads(exc.read().decode("utf-8"))
            except Exception:
                return {"ok": False, "error": f"验证码发送失败（HTTP {exc.code}）"}
        except Exception as exc:
            return {"ok": False, "error": f"登录服务不可达: {exc}"}
        return result if isinstance(result, dict) else {"ok": False, "error": "服务端响应异常"}

    def email_login_verify(self, email: str, code: str, intent: str = "login") -> dict:
        """校验验证码：验证服务端许可证后写入本地订阅状态。"""
        import urllib.request

        from engine.device import device_id

        settings = self.controller.settings
        base = str(settings.get("subscription_server_url") or "").strip().rstrip("/")
        if not base:
            return {"ok": False, "error": "尚未配置付费服务地址（subscription_server_url）"}
        if intent not in ("login", "bind"):
            return {"ok": False, "error": "邮箱验证操作无效"}
        device = device_id(str(getattr(self.settings_store, "base_dir", "") or "."))
        existing_session = str(settings.get("account_login_token") or "")
        pending_session = str(settings.get("account_pending_login_token") or "")
        revision = self._account_revision
        profile_revision = self._profile_revision
        body = json.dumps({
            "email": str(email or "").strip(),
            "code": str(code or "").strip(),
            "device_id": device,
            "login_token": str(settings.get("account_login_token") or ""),
            "intent": intent,
        }, ensure_ascii=False).encode("utf-8")
        try:
            request = urllib.request.Request(
                base + "/v1/auth/email/verify", data=body,
                headers=payment_service_headers(), method="POST",
            )
            with urllib.request.urlopen(request, timeout=15) as response:
                result = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            try:
                return json.loads(exc.read().decode("utf-8"))
            except Exception:
                return {"ok": False, "error": f"验证失败（HTTP {exc.code}）"}
        except Exception as exc:
            return {"ok": False, "error": f"登录服务不可达: {exc}"}
        if not isinstance(result, dict) or not result.get("ok"):
            if isinstance(result, dict) and result.get("status") == "device_limit":
                return self._remember_device_management(result, base=base,
                    expected_session=existing_session, expected_pending=pending_session,
                    expected_revision=revision)
            return result if isinstance(result, dict) else {"ok": False, "error": "服务端响应异常"}
        applied = self._apply_remote_account(result, expected_session=existing_session,
                                              expected_pending=pending_session, expected_base=base,
                                              expected_revision=revision, expected_profile_revision=profile_revision)
        if not applied.get("ok"):
            return applied
        result["ok"] = True
        return result

    def web_login_start(self) -> dict:
        """Open a one-time request; only the desktop retains the polling capability."""
        import webbrowser
        import hashlib
        import secrets
        import time
        from engine.device import device_id
        with self._lock:
            current = self.controller.settings
            base = str(current.get('subscription_server_url') or '').strip().rstrip('/')
            session = str(current.get('account_login_token') or '')
            pending = str(current.get('account_pending_login_token') or '')
            revision = self._account_revision
        if not base:
            return {'ok':False,'error':'尚未配置登录服务'}
        api = self._local_api()
        if api is None or not api.running:
            return {'ok':False,'error':'请在设置中启用本机连接后重试'}
        port = int(api.address[1])
        if not 1024 <= port <= 65535:
            return {'ok':False,'error':'本机连接端口无效'}
        device = device_id(self.settings_store.base_dir)
        approval = secrets.token_hex(32)
        approval_hash = 'sha256:' + hashlib.sha256(('shengjian-auth-v1:desktop-approval:' + approval).encode()).hexdigest()
        result = self._account_request(base, '/v1/auth/desktop/start', {'device_id':device,'approval_hash':approval_hash})
        if result.get('ok') is not True:
            return {'ok':False,'error':result.get('error') or '网页授权暂不可用'}
        poll = result.get('session_id')
        request_id = result.get('request_id')
        expected_url = 'https://foilpage.early-snipe-7202.chatgpt.site/app.html#desktop-login=' + str(request_id)
        if (not isinstance(poll,str) or not re.fullmatch(r'poll-[a-f0-9]{64}',poll)
                or not isinstance(request_id,str) or not re.fullmatch(r'desktop-[a-f0-9]{64}',request_id)
                or result.get('automatic') is not True
                or result.get('authorize_url') != expected_url):
            return {'ok':False,'error':'网页授权请求无效，请重试'}
        with self._lock:
            applied = self._save_pending_account_login({'login_token':poll},base,session,pending,revision)
            attempt = {'base':base,'session_id':poll,'device_id':device,'session':session,'revision':self._account_revision,
                       'request_id':request_id,'approval_token':approval,'expires_at':time.monotonic()+600}
            if applied.get('ok'):
                self._web_login_pending = attempt
        if not applied.get('ok'):
            self._account_request(base,'/v1/auth/desktop/cancel',{'session_id':poll,'device_id':device})
            return applied
        try:
            if webbrowser.open(expected_url + '&port=' + str(port)) is False:
                raise RuntimeError('browser unavailable')
        except Exception:
            self._cancel_web_login(attempt)
            return {'ok':False,'error':'未能打开浏览器，请重试'}
        return {'ok':True,'status':'pending','expires_in_seconds':600}

    def _web_login_proof(self, request_id: str) -> dict:
        """Only the exact official web origin may obtain this pending local proof."""
        import time
        with self._lock:
            attempt = getattr(self,'_web_login_pending',None)
            if (not attempt or not self._web_login_current(attempt) or attempt['request_id'] != request_id
                    or time.monotonic() >= attempt['expires_at']):
                return {'ok':False,'error':'请从本机 FoilPage 重新发起登录'}
            return {'ok':True,'request_id':request_id,'approval_token':attempt['approval_token']}

    def _web_login_current(self, attempt: dict) -> bool:
        current = self.controller.settings
        return (getattr(self,'_web_login_pending',None) is attempt and self._account_revision == attempt['revision']
                and str(current.get('subscription_server_url') or '').strip().rstrip('/') == attempt['base']
                and str(current.get('account_login_token') or '') == attempt['session']
                and current.get('account_pending_login_token') == attempt['session_id'])

    def web_login_poll(self) -> dict:
        with self._lock:
            attempt = getattr(self,'_web_login_pending',None)
            if not attempt or not self._web_login_current(attempt):
                return {'ok':False,'status':'invalid','error':'登录请求已变更'}
            profile_revision = self._profile_revision
        result = self._account_request(attempt['base'],'/v1/auth/desktop/poll',{'session_id':attempt['session_id'],'device_id':attempt['device_id']})
        if not self._web_login_current(attempt):
            return {'ok':False,'status':'invalid','error':'登录请求已变更，已忽略旧响应'}
        if result.get('ok') is True and result.get('status') == 'pending':
            return {'ok':True,'status':'pending'}
        if result.get('ok') is not True:
            return {'ok':False,'status':result.get('status') or 'error','error':result.get('error') or '网页登录未完成'}
        applied = self._apply_remote_account(result,expected_session=attempt['session'],expected_pending=attempt['session_id'],
            expected_base=attempt['base'],expected_revision=attempt['revision'],expected_profile_revision=profile_revision)
        if applied.get('ok'):
            self._web_login_pending = None
            window = self._window()
            if window is not None:
                try:
                    window.show()
                    window.restore()
                except Exception:
                    pass
            return {'ok':True,'status':'ok'}
        return {**applied,'status':'bad_license'}

    def _cancel_web_login(self, attempt: dict | None) -> dict:
        if not attempt:
            return {'ok':True}
        with self._lock:
            if self.controller.settings.get('account_pending_login_token') == attempt['session_id']:
                self.controller.save_settings({'account_pending_login_token':''})
                self._account_revision += 1
        result = self._account_request(attempt['base'],'/v1/auth/desktop/cancel',{'session_id':attempt['session_id'],'device_id':attempt['device_id']})
        if result.get('ok') and getattr(self,'_web_login_pending',None) is attempt:
            self._web_login_pending = None
        return {'ok':result.get('ok') is True,**({} if result.get('ok') else {'error':result.get('error') or '取消尚未确认'})}

    def web_login_cancel(self) -> dict:
        return self._cancel_web_login(getattr(self,'_web_login_pending',None))

    def google_login_start(self, intent: str = "login") -> dict:
        """发起 Google 快捷登录：系统浏览器打开授权页，应用内轮询 wechat_login_poll。"""
        import urllib.request
        import webbrowser

        from engine.device import device_id

        settings = self.controller.settings
        base = str(settings.get("subscription_server_url") or "").strip().rstrip("/")
        if not base:
            return {"ok": False, "error": "尚未配置付费服务地址（subscription_server_url）"}
        if intent not in ("login", "bind"):
            return {"ok": False, "error": "Google 登录操作无效"}
        existing_session = str(settings.get("account_login_token") or "")
        if intent == "bind" and not existing_session:
            return {"ok": False, "error": "请先登录，再关联 Google"}
        existing_pending = str(settings.get("account_pending_login_token") or "")
        revision = self._account_revision
        body = json.dumps({
            "device_id": device_id(str(getattr(self.settings_store, "base_dir", "") or ".")),
            "intent": intent,
            "login_token": existing_session if intent == "bind" else "",
        }, ensure_ascii=False).encode("utf-8")
        try:
            request = urllib.request.Request(
                base + "/v1/auth/google/start", data=body,
                headers=payment_service_headers(), method="POST",
            )
            with urllib.request.urlopen(request, timeout=15) as response:
                result = json.loads(response.read().decode("utf-8"))
        except Exception as exc:
            return {"ok": False, "error": f"登录服务不可达: {exc}"}
        if not isinstance(result, dict) or not result.get("ok"):
            return result if isinstance(result, dict) else {"ok": False, "error": "服务端响应异常"}
        result = {**result, "auth_url": result.get("authorize_url") or result.get("auth_url"),
            "login_token": result.get("session_id") or result.get("login_token")}
        if not self._valid_account_auth_url(result.get("auth_url"), "google"):
            return {"ok": False, "error": "登录授权地址无效，请检查服务配置"}
        with self._lock:
            applied = self._save_pending_account_login(result, base, existing_session, existing_pending, revision)
            if applied["ok"]:
                self._google_login_pending = {"base": base, "session_id": result["login_token"],
                    "device_id": device_id(str(getattr(self.settings_store, "base_dir", "") or "."))}
        if not applied["ok"]:
            # A logout or service switch during the request must also invalidate
            # the newly created remote challenge, not just reject it locally.
            self._account_request(base, "/v1/auth/google/cancel", {
                "session_id": result["login_token"],
                "device_id": device_id(str(getattr(self.settings_store, "base_dir", "") or ".")),
            })
            return applied
        try:
            webbrowser.open(str(result.get("auth_url") or ""))
        except Exception:
            pass
        return result

    def google_login_cancel(self) -> dict:
        """Invalidate only this client's pending Google challenge; keep full login."""
        with self._lock:
            attempt = getattr(self, "_google_login_pending", None)
        return self._cancel_google_login(attempt)

    def _cancel_google_login(self, attempt: dict | None) -> dict:
        with self._lock:
            current = self.controller.settings
            if not attempt:
                return {"ok": True, "status": "cancelled"}
            # Close local acceptance before the network request, so a held poll
            # cannot install a full session while cancellation is in flight.
            if (current.get("account_pending_login_token") == attempt["session_id"]
                    and str(current.get("subscription_server_url") or "").strip().rstrip("/") == attempt["base"]):
                self.controller.save_settings({"account_pending_login_token": ""})
                self._account_revision += 1
        result = self._account_request(attempt["base"], "/v1/auth/google/cancel",
            {"session_id": attempt["session_id"], "device_id": attempt["device_id"]})
        if result.get("ok") is not True:
            return result
        with self._lock:
            if getattr(self, "_google_login_pending", None) == attempt:
                self._google_login_pending = None
        return result

    @staticmethod
    def _valid_account_auth_url(value: object, provider: str) -> bool:
        if not isinstance(value, str) or len(value) > 8192:
            return False
        try:
            parsed = urllib.parse.urlsplit(value)
            return (parsed.scheme == "https" and parsed.hostname == {
                "google": "accounts.google.com", "wechat": "open.weixin.qq.com"}.get(provider)
                and parsed.port in (None, 443) and not parsed.username and not parsed.password
                and not parsed.fragment)
        except ValueError:
            return False

    def _save_pending_account_login(self, result: dict, base: str, session: str, pending: str, revision: int) -> dict:
        token = str(result.get("login_token") or "")
        if not token:
            return {"ok": False, "error": "登录请求缺少有效会话"}
        with self._lock:
            settings = self.controller.settings
            if (self._account_revision != revision
                    or str(settings.get("subscription_server_url") or "").strip().rstrip("/") != base
                    or str(settings.get("account_login_token") or "") != session
                    or str(settings.get("account_pending_login_token") or "") != pending):
                return {"ok": False, "error": "登录请求已变更，已忽略旧响应"}
            self.controller.save_settings({"account_pending_login_token": token})
            self._account_management = None
            self._account_revision += 1
            self._buyer_verification = None
            self._alipay_cashiers = {}
        return {"ok": True}

    def account_logout(self) -> dict:
        """退出账户（清除本地登录会话；服务端令牌一并注销）。"""
        import urllib.request

        with self._lock:
            settings = self.controller.settings
            base = str(settings.get("subscription_server_url") or "").strip().rstrip("/")
            token = str(settings.get("account_login_token") or "").strip()
            pending = str(settings.get("account_pending_login_token") or "").strip()
            management = self._account_management or {}
            tokens = {token, pending}
            if management.get("base") == base:
                tokens.add(str(management.get("token") or ""))
            google_attempt = getattr(self, "_google_login_pending", None)
            web_attempt = getattr(self, '_web_login_pending', None)
            self._clear_account()
        # The public logout endpoint handles full sessions; a Google polling
        # capability has a separate cancellation endpoint, including callbacks
        # that completed remotely but have not been accepted by this client.
        self._cancel_google_login(google_attempt)
        self._cancel_web_login(web_attempt)
        for token in tokens - {""} if base else ():
            body = json.dumps({"login_token": token}, ensure_ascii=False).encode("utf-8")
            try:
                request = urllib.request.Request(
                    base + "/v1/auth/logout", data=body,
                    headers=payment_service_headers(), method="POST",
                )
                urllib.request.urlopen(request, timeout=10).read()
            except Exception:
                pass
        return {"ok": True}

    def _clear_account(self, *, expected_session: str | None = None) -> None:
        with self._lock:
            if expected_session is not None and str(self.controller.settings.get("account_login_token") or "") != expected_session:
                return
            self._account_revision += 1
            self._buyer_verification = None
            self._alipay_cashiers = {}
            changes = {"account_login_token": "",
                "account_display_name": "", "account_avatar": "", "account_user_id": "", "license_token": "", "account_access_token":"",
                "license_expires_at": None, "subscription_plan": "free", "subscription_active": False,
                "subscription_expires_at": None, "credit_balance": 0, "subscription_last_order_id": "",
                "subscription_expiry_notice_history": None}
            if expected_session is None:
                changes["account_pending_login_token"] = ""
                self._account_management = None
            elif self._account_management and self._account_management["session"] == expected_session:
                self._account_management["session"] = ""
            self.controller.save_settings(changes)

    def _remember_device_management(self, result: dict, *, base: str,
                                    expected_session: str, expected_pending: str,
                                    expected_revision: int | None = None) -> dict:
        token = str(result.get("management_token") or "")
        account = result.get("account")
        if not token or not isinstance(account, dict) or account.get("device_management_only") is not True:
            return {"ok": False, "error": "设备管理响应无效，请重新登录"}
        with self._lock:
            current = self.controller.settings
            if ((expected_revision is not None and self._account_revision != expected_revision)
                    or str(current.get("subscription_server_url") or "").strip().rstrip("/") != base
                    or str(current.get("account_login_token") or "") != expected_session
                    or str(current.get("account_pending_login_token") or "") != expected_pending):
                return {"ok": False, "error": "登录请求已变更，已忽略旧响应"}
            self._account_management = {"token": token, "base": base,
                "session": expected_session, "pending": expected_pending}
        return {"ok": False, "status": "device_limit", "account": account,
                "error": "设备数量已达上限，请解绑一台旧设备后继续登录"}

    def _account_context(self) -> tuple[str, str, dict | None]:
        with self._lock:
            current = self.controller.settings
            base = str(current.get("subscription_server_url") or "").strip().rstrip("/")
            session = str(current.get("account_login_token") or "")
            management = self._account_management
            if management and (management["base"] != base or management["session"] != session
                    or management["pending"] != str(current.get("account_pending_login_token") or "")):
                self._account_management = management = None
            return base, str(management["token"] if management else session), management

    @staticmethod
    def _account_request(base: str, path: str, payload: dict) -> dict:
        import urllib.error
        import urllib.request
        try:
            request = urllib.request.Request(base + path,
                data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                headers=payment_service_headers(), method="POST")
            with urllib.request.urlopen(request, timeout=12) as response:
                result = json.loads(response.read().decode("utf-8"))
            return result if isinstance(result, dict) else {"ok": False, "error": "服务端响应无效"}
        except urllib.error.HTTPError as exc:
            try:
                result = json.loads(exc.read().decode("utf-8"))
            except Exception:
                result = {}
            if not isinstance(result, dict):
                result = {}
            return {**result, "ok": False, "error": str(result.get("error") or "账户操作未完成"),
                    **({"status": "invalid"} if exc.code in (401, 403) else {})}
        except Exception:
            return {"ok": False, "error": "暂时无法连接账户服务，请稍后重试"}

    def _account_context_current(self, base: str, token: str, management: dict | None) -> bool:
        return self._account_context() == (base, token, management)

    def update_account_profile(self, display_name: str, avatar: str | None = None) -> dict:
        """Save only profile fields for the current full session, without granting rights."""
        from engine.account_profile import normalize_display_name, validate_avatar

        try:
            name = normalize_display_name(display_name)
            image = validate_avatar(avatar)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        with self._lock:
            if self._profile_busy:
                return {"ok": False, "error": "资料正在保存，请稍候"}
            base, token, management = self._account_context()
            user_id = str(self.controller.settings.get("account_user_id") or "")
            revision = self._account_revision
            if management or not base or not token or not user_id:
                return {"ok": False, "error": "请完成账户登录后编辑资料"}
            self._profile_busy = True
        try:
            return self._save_account_profile(base, token, management, user_id, revision, name, image)
        finally:
            with self._lock:
                self._profile_busy = False

    def _save_account_profile(self, base, token, management, user_id, revision, name, image) -> dict:
        from engine.account_profile import normalize_display_name, validate_avatar

        result = self._account_request(base, "/v1/account/profile", {
            "login_token": token, "display_name": name, "avatar": image,
        })
        with self._lock:
            if (self._account_revision != revision
                    or not self._account_context_current(base, token, management)
                    or str(self.controller.settings.get("account_user_id") or "") != user_id):
                return {"ok": False, "error": "账户已变更，已忽略旧响应"}
            if result.get("ok") is not True:
                if result.get("status") == "invalid":
                    self._clear_account(expected_session=token)
                return result
            account = result.get("account")
            if not isinstance(account, dict) or account.get("user_id") != user_id:
                return {"ok": False, "error": "账户资料响应无效，请刷新重试"}
            try:
                saved_name = normalize_display_name(account.get("display_name"))
                if not isinstance(account.get("avatar"), str):
                    raise ValueError
                saved_avatar = validate_avatar(account["avatar"])
                if saved_name != name or (image is not None and saved_avatar != image):
                    raise ValueError
            except ValueError:
                return {"ok": False, "error": "账户资料响应无效，请刷新重试"}
            self.controller.save_settings({"account_display_name": saved_name, "account_avatar": saved_avatar})
            self._profile_revision += 1
            return {"ok": True, "account": {"user_id": user_id, "display_name": saved_name, "avatar": saved_avatar}}

    def get_account_devices(self) -> dict:
        """Read account devices; an unsigned view never grants local entitlement."""
        base, token, management = self._account_context()
        if not base or not token:
            return {"ok": False, "error": "登录后可查看设备"}
        result = self._account_request(base, "/v1/account", {"login_token": token})
        if not self._account_context_current(base, token, management):
            return {"ok": False, "error": "账户已变更，已忽略旧响应"}
        if result.get("status") == "invalid":
            if management:
                self.cancel_account_login()
            else:
                self._clear_account(expected_session=token)
        return result

    def unbind_account_device(self, target_device: str) -> dict:
        base, token, management = self._account_context()
        target = str(target_device or "").strip()
        if not base or not token or not target:
            return {"ok": False, "error": "登录后可解绑指定设备"}
        result = self._account_request(base, "/v1/devices/unbind",
            {"login_token": token, "device_id": target})
        if not self._account_context_current(base, token, management):
            return {"ok": False, "error": "账户已变更，已忽略旧响应"}
        if result.get("ok") and result.get("current_device_unbound") is True and not management:
            self._clear_account(expected_session=token)
        elif result.get("status") == "invalid":
            if management:
                self.cancel_account_login()
            else:
                self._clear_account(expected_session=token)
        return result

    def complete_account_login(self) -> dict:
        base, token, management = self._account_context()
        revision = self._account_revision
        profile_revision = self._profile_revision
        if not management:
            return {"ok": False, "error": "请重新验证身份后登录"}
        result = self._account_request(base, "/v1/auth/complete", {"management_token": token})
        if not self._account_context_current(base, token, management):
            return {"ok": False, "error": "登录请求已变更，已忽略旧响应"}
        if not result.get("ok"):
            return {key: value for key, value in result.items() if key != "management_token"}
        applied = self._apply_remote_account(result, expected_session=management["session"],
            expected_pending=management["pending"], expected_base=base, expected_revision=revision,
            expected_profile_revision=profile_revision)
        return result if applied.get("ok") else applied

    def cancel_account_login(self) -> dict:
        """Cancel unfinished identity verification without logging out a current account."""
        web = self.web_login_cancel()
        if web.get('ok') is False:
            return web
        google = self.google_login_cancel()
        if google.get("ok") is False:
            return google
        with self._lock:
            current = self.controller.settings
            base = str(current.get("subscription_server_url") or "").strip().rstrip("/")
            pending = str(current.get("account_pending_login_token") or "")
            management = self._account_management or {}
            tokens = {pending}
            if management.get("base") == base:
                tokens.add(str(management.get("token") or ""))
            self._account_management = None
            self._account_revision += 1
            self._buyer_verification = None
            self.controller.save_settings({"account_pending_login_token": ""})
        for token in tokens - {""} if base else ():
            self._account_request(base, "/v1/auth/logout", {"login_token": token})
        return {"ok": True}

    def _buyer_context_current(self, attempt: dict) -> bool:
        with self._lock:
            current = self.controller.settings
            return (self._account_revision == attempt["revision"]
                and str(current.get("subscription_server_url") or "").strip().rstrip("/") == attempt["base"]
                and str(current.get("account_login_token") or "") == attempt["session"]
                and str(current.get("account_user_id") or "") == attempt["user_id"]
                and self._account_management is None)

    def buyer_verification_start(self) -> dict:
        """Open official buyer authorization; the private polling nonce stays in memory."""
        import webbrowser
        from engine.device import device_id
        with self._lock:
            current = self.controller.settings
            self._buyer_revision += 1
            attempt = {"base": str(current.get("subscription_server_url") or "").strip().rstrip("/"),
                "session": str(current.get("account_login_token") or ""),
                "user_id": str(current.get("account_user_id") or ""), "revision": self._account_revision,
                "buyer_revision": self._buyer_revision}
        if not attempt["base"] or not attempt["session"] or not attempt["user_id"] or self._account_management:
            return {"ok": False, "error": "请先登录 FoilPage 账户，再验证爱发电买家身份"}
        result = self._account_request(attempt["base"], "/v1/account/buyer/start",
            {"login_token": attempt["session"], "device_id": device_id(str(getattr(self.settings_store, "base_dir", "") or "."))})
        if not self._buyer_context_current(attempt):
            return {"ok": False, "error": "账户已变更，已忽略旧授权响应"}
        if not result.get("ok"):
            return {"ok": False, "error": str(result.get("error") or "爱发电身份验证暂不可用")}
        url = result.get("authorize_url")
        nonce = result.get("session_id")
        try:
            parsed = urllib.parse.urlsplit(url) if isinstance(url, str) and len(url) <= 8192 else None
            valid = bool(parsed and parsed.scheme == "https" and parsed.hostname == "afdian.com"
                and parsed.path == "/oauth2/authorize" and parsed.port in (None, 443)
                and not parsed.username and not parsed.password and not parsed.fragment)
        except ValueError:
            valid = False
        if not valid or not isinstance(nonce, str) or not nonce or len(nonce) > 512:
            return {"ok": False, "error": "爱发电授权地址或会话无效，请检查服务配置"}
        with self._lock:
            if not self._buyer_context_current(attempt):
                return {"ok": False, "error": "账户已变更，已忽略旧授权响应"}
            superseded = self._buyer_revision != attempt["buyer_revision"]
            if not superseded:
                self._buyer_verification = {**attempt, "nonce": nonce}
        if superseded:
            self._cancel_buyer_nonce(attempt, nonce)
            return {"ok": False, "error": "授权请求已取消，已忽略旧响应"}
        try:
            opened = webbrowser.open(url)
            if opened is False:
                raise RuntimeError("browser unavailable")
        except Exception:
            self.buyer_verification_stop()
            return {"ok": False, "error": "浏览器未能打开，请重试爱发电验证"}
        return {"ok": True, "status": "pending"}

    def buyer_verification_poll(self) -> dict:
        attempt = self._buyer_verification
        if not attempt or not self._buyer_context_current(attempt):
            self._buyer_verification = None
            return {"ok": False, "status": "error", "error": "验证会话已失效，请重新发起"}
        result = self._account_request(attempt["base"], "/v1/account/buyer/poll", {"session_id": attempt["nonce"]})
        if self._buyer_verification is not attempt or not self._buyer_context_current(attempt):
            return {"ok": False, "status": "error", "error": "账户或授权请求已变更，已忽略旧响应"}
        if result.get("status") == "pending" and result.get("ok"):
            return {"ok": True, "status": "pending"}
        if result.get("status") != "verified" or result.get("buyer_verified") is not True:
            return {"ok": False, "status": "error", "error": str(result.get("error") or "爱发电尚未确认身份验证，请重试")}
        account = self._account_request(attempt["base"], "/v1/account", {"login_token": attempt["session"]})
        if self._buyer_verification is not attempt or not self._buyer_context_current(attempt):
            return {"ok": False, "status": "error", "error": "账户已变更，已忽略旧响应"}
        if not account.get("ok") or account.get("buyer_verified") is not True or account.get("user_id") != attempt["user_id"]:
            return {"ok": False, "status": "error", "error": "买家身份尚未绑定到当前账户，请刷新后重试"}
        self._buyer_verification = None
        return {"ok": True, "status": "verified", "buyer_verified": True,
            "account": {key: account[key] for key in ("user_id", "display_name", "buyer_verified", "order_review") if key in account}}

    def buyer_verification_stop(self) -> dict:
        with self._lock:
            attempt = self._buyer_verification
            self._buyer_verification = None
            self._buyer_revision += 1
        if not attempt or not self._buyer_context_current(attempt):
            return {"ok": True, "status": "cancelled"}
        result = self._cancel_buyer_nonce(attempt, attempt["nonce"])
        if not self._buyer_context_current(attempt):
            return {"ok": False, "error": "账户已变更，已忽略旧取消响应"}
        if not result.get("ok"):
            return {"ok": False, "error": "无法确认服务器取消状态，请在浏览器取消授权"}
        if result.get("status") in ("verified", "completed") or result.get("was_completed") is True:
            account = self._account_request(attempt["base"], "/v1/account", {"login_token": attempt["session"]})
            if (not self._buyer_context_current(attempt) or not account.get("ok")
                    or account.get("buyer_verified") is not True or account.get("user_id") != attempt["user_id"]):
                return {"ok": False, "error": "请刷新账户确认买家验证状态"}
            return {"ok": True, "status": "verified"}
        return {"ok": True, "status": "cancelled"}

    def _cancel_buyer_nonce(self, attempt: dict, nonce: str) -> dict:
        from engine.device import device_id
        return self._account_request(attempt["base"], "/v1/account/buyer/cancel",
            {"session_id": nonce, "login_token": attempt["session"],
             "device_id": device_id(str(getattr(self.settings_store, "base_dir", "") or "."))})
