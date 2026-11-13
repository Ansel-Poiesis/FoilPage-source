# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""pywebview/WebView2 js_api bridge for FoilPage 0.8.

Exposes the AppController data model to the HTML frontend as plain JSON.
All methods are serialized behind a lock because pywebview invokes js_api
methods on a background thread while the controller uses shared SQLite
connections.

Python -> JS push happens through window.evaluate_js("window.__onState(...)").
"""
from __future__ import annotations

import json
import logging
import hashlib
import hmac
import secrets
import stat
from decimal import Decimal, InvalidOperation
from datetime import datetime, timezone
import os
import re
import shutil
import subprocess
import sys
import threading
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable

from app.controller import AppController
from app.bridge_account import AccountBridgeMixin, _safe_progress
from engine.agent_protocol import document_revision
from app.data_health import reconcile
from app.maintenance import (
    export_backup as _export_backup,
    export_diagnostics as _export_diagnostics,
    import_backup as _import_backup,
)
from engine.media import find_external_media_player, probe_video_codecs
from engine.service_http import payment_service_headers
from engine.subscription import activate_local, checkout, plans, status as subscription_status

log = logging.getLogger(__name__)


ACTIVE_STATUSES = {"queued", "running", "retryable", "received", "resolving", "awaiting_browser", "downloading", "imported", "transcribing"}
TERMINAL_STATUSES = {"done", "failed", "cancelled"}

APP_VERSION = "0.1.0"
RELEASE_CHANNEL = "formal"
UPDATE_URL = "https://api.github.com/repos/Ansel-Poiesis/FoilPage-source/releases/latest"

_GPU_RUNTIME_DLLS = {
    "cublas": "cublas64_*.dll",
    "cuda_nvrtc": "nvrtc64_*.dll",
    "cudnn": "cudnn64_*.dll",
}


def _gpu_runtime_components(runtime_root: Path) -> list[str]:
    return sorted(component for component, pattern in _GPU_RUNTIME_DLLS.items()
                  if any(path.is_file() and path.stat().st_size > 0
                         for path in (runtime_root / component / "bin").glob(pattern)))


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


def _parse_version(value: object) -> tuple[int, ...]:
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", str(value or ""))
    if not match:
        return ()
    return tuple(int(part) for part in match.groups())


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


def _public_manual_payment_contract(value: object) -> dict:
    """A separate, explicit collection capability; never infer it from Afdian."""
    value = value if isinstance(value, dict) else {}
    configured = value.get("configured") is True and value.get("provider") == "alipay_personal"
    return {"configured": configured,
            "enabled": configured and value.get("enabled") is True,
            "provider": "alipay_personal", "catalog": _public_subscription_catalog(value.get("catalog")) if configured else []}


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


def _public_alipay_contract(value: object) -> dict:
    value = value if isinstance(value, dict) else {}
    maximum = value.get("max_order_amount_minor")
    maximum = maximum if isinstance(maximum, int) and not isinstance(maximum, bool) and 0 < maximum <= 10000000000 else None
    configured = value.get("configured") is True and value.get("provider") == "alipay_page_pay" and maximum is not None
    result = {"configured": configured, "enabled": configured and value.get("enabled") is True,
            "processing_enabled": value.get("provider") == "alipay_page_pay" and value.get("processing_enabled") is True,
            "max_order_amount_minor": maximum,
            "provider": "alipay_page_pay", "catalog": _public_subscription_catalog(value.get("catalog")) if configured else []}
    return result


def _public_alipay_order(value: object) -> dict | None:
    """An order is only a public snapshot, never a license or payment proof."""
    if not isinstance(value, dict):
        return None
    identifier, quote, revision = value.get("order_id"), value.get("snapshot"), value.get("revision")
    if (not isinstance(identifier, str) or not re.fullmatch(r"[A-Za-z0-9_-]{12,64}", identifier)
            or value.get("status") not in ("pending_payment", "waiting", "closed", "credited", "refund_review")
            or isinstance(revision, bool) or not isinstance(revision, int) or revision < 0 or not isinstance(quote, dict)):
        return None
    amount, minor, credits, period = quote.get("amount"), quote.get("amount_minor"), quote.get("credits"), quote.get("period")
    if (quote.get("plan_id") not in ("standard", "advanced") or period not in ("month", "year")
            or quote.get("product_kind", "cloud_credits") != "cloud_credits"
            or isinstance(quote.get("months"), bool) or quote.get("months") != (1 if period == "month" else 12)
            or quote.get("currency") != "CNY" or not isinstance(amount, str)
            or not re.fullmatch(r"(?:0|[1-9][0-9]{0,8})\.[0-9]{2}", amount)
            or isinstance(minor, bool) or not isinstance(minor, int) or not 0 < minor <= 10000000000
            or Decimal(amount) * 100 != minor or isinstance(credits, bool) or not isinstance(credits, int)
            or not 0 < credits <= 2**53-1):
        return None
    result = {"order_id": identifier, "status": value["status"], "revision": revision,
              "snapshot": {"plan_id": quote["plan_id"], "period": period, "months": quote["months"],
                           "amount": amount, "amount_minor": minor, "credits": credits, "currency": "CNY"},
              "created_at": str(value.get("created_at") or "")[:64], "updated_at": str(value.get("updated_at") or "")[:64]}
    if quote.get("product_kind") == "cloud_credits":
        result["snapshot"]["product_kind"] = "cloud_credits"
    if isinstance(value.get("receipt_last4"), str) and re.fullmatch(r"[0-9]{4}", value["receipt_last4"]):
        result["receipt_last4"] = value["receipt_last4"]
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


def _prefixed_ids(task_ids: list, prefix: str) -> list[str]:
    """提取指定前缀的任务 id；过滤非字符串，避免桥接层崩溃。"""
    if not task_ids:
        return []
    return [t[len(prefix):] for t in task_ids if isinstance(t, str) and t.startswith(prefix)]


_PASTED_URL_PATTERN = re.compile(
    r"https?://[^\s<>\"'\u3000-\u303f\u4e00-\u9fff\uff00-\uffef]+",
    re.IGNORECASE,
)


def _extract_pasted_url(value: object) -> str:
    """Extract the first URL from platform share text pasted into the app."""
    text = str(value or "").strip()
    match = _PASTED_URL_PATTERN.search(text)
    if not match:
        return text
    return match.group(0).rstrip(".,;:!?)]}）】》」』")


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


def _compact_title(value: object, limit: int = 62) -> str:
    text = str(value or "")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _task_media_meta(task: dict) -> tuple[str, str]:
    kind = task.get("kind")
    if kind == "download":
        return "↓", "浏览器下载"
    return "▶", f"媒体（{task.get('device', '')}）"


def _settings_view(controller, settings_store, account_revision: int = 0) -> dict:
    """Settings dict enriched with runtime-only fields for the frontend."""
    view = dict(controller.settings)
    view["data_dir"] = str(settings_store.base_dir)
    from engine.account_access import account_access
    from engine.device import device_id
    current_device = device_id(str(settings_store.base_dir)) if view.get("account_login_token") else ""
    view["account_access"] = account_access(view, device_id=current_device)
    view["account_access"]["revision"] = account_revision
    return view


def _browser_extension_dir() -> Path:
    """Bundled MV3 browser extension directory (dev source or frozen install)."""
    if getattr(sys, "frozen", False):
        base = Path(sys.executable).resolve().parent
    else:
        base = Path(__file__).resolve().parents[1]
    return base / "browser-extension"


class Bridge(AccountBridgeMixin):
    def __init__(
        self,
        controller: AppController,
        worker: Any,
        settings_store: Any,
        window_provider: Callable[[], Any],
        on_quit: Callable[[], None],
        local_api_provider: Callable[[], Any] | None = None,
        native_theme_applier: Callable[[str], None] | None = None,
        notifier: Callable[[str, str], bool] | None = None,
        launch_context_provider: Callable[[], dict] | None = None,
        native_drag_provider: Callable[[], bool] | None = None,
    ) -> None:
        self.controller = controller
        self.worker = worker
        self.settings_store = settings_store
        self._window = window_provider
        self._on_quit = on_quit
        self._local_api_provider = local_api_provider or (lambda: None)
        self._native_theme_applier = native_theme_applier
        self._notifier = notifier
        self._launch_context = launch_context_provider or (lambda: {"mode": "manual", "sequence": 0})
        self._native_drag = native_drag_provider or (lambda: False)
        self._media_drop_element = None
        self._lock = threading.RLock()
        self._log: list[str] = []
        self._model_download = {
            "name": "",
            "message": "",
            "progress": None,
            "error": "",
        }
        self._batch_agent = {
            "running": False,
            "done": 0,
            "skipped": 0,
            "failed": 0,
            "errors": [],
            "message": "",
        }
        # TASK-8.24：单篇翻译任务态（同一时间仅一个翻译在跑，同批量整理）。
        self._translation: dict = {"running": False, "task_id": "", "status": "", "error": "", "lang": ""}
        self._subscription_acceptance: dict | None = None
        self._subscription_service: dict | None = None
        self._subscription_service_base = ""
        self._subscription_sync_at = 0.0
        self._license_selfheal_at = 0.0
        # A verified identity waiting for a free device slot is not an account
        # session. Keep its short-lived management token out of settings/disk.
        self._account_management: dict | None = None
        self._google_login_pending: dict | None = None
        self._account_revision = 0
        self._profile_revision = 0
        self._profile_busy = False
        self._buyer_verification: dict | None = None
        self._buyer_revision = 0
        self._media_preview = None
        self._product_sync_busy = False
        self._product_sync_at = 0.0
        self._announcement_sync_at = 0.0

    # ---------------------------------------------------------------- state
    def _work_entry_error(self) -> dict | None:
        from engine.account_access import AccountLoginRequired, require_account_access
        try:
            require_account_access(dict(self.controller.settings), self.settings_store.base_dir)
        except AccountLoginRequired as exc:
            return {"ok": False, "code": exc.code, "error": str(exc)}
        return None

    def _processing_entry_error(self) -> dict | None:
        # TASK-8.29：解锁门槛下线，处理入口只保留登录校验（与 _work_entry_error 同口径）。
        return self._work_entry_error()

    def agent_operations_facade(self) -> dict:
        """Bind only the explicitly reviewed Agent operation registry."""
        from app.agent_operations import AGENT_BRIDGE_METHODS
        facade = {name: getattr(self, name) for name in AGENT_BRIDGE_METHODS
                if callable(getattr(self, name, None))}
        facade["quit_app"] = self._agent_request_quit
        return facade

    def _agent_request_quit(self) -> dict:
        """Let the loopback response complete before normal application exit."""
        timer = threading.Timer(0.25, self.quit_app)
        timer.daemon = True
        timer.start()
        return {"ok": True, "accepted": True}

    def _settings_revision(self) -> str:
        if not hasattr(self, "_agent_settings_revision_key"):
            self._agent_settings_revision_key = secrets.token_bytes(32)
        content = json.dumps(self.controller.settings, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        return hmac.new(self._agent_settings_revision_key, content, hashlib.sha256).hexdigest()

    def _frontend_settings_view(self) -> dict:
        view = _settings_view(self.controller, self.settings_store, getattr(self, "_account_revision", 0))
        view["agent_settings_revision"] = self._settings_revision()
        return view

    def get_bootstrap(self) -> dict:
        with self._lock:
            rows = self.controller.task_rows()
            return {
                "settings": self._frontend_settings_view(),
                "tasks": self._tasks(rows),
                "summary": self._summary(rows),
                "worker": self._worker_state(),
                "version": APP_VERSION,
                "release_channel": RELEASE_CHANNEL,
                "window_chrome": self.get_window_chrome(),
                "launch_context": dict(self._launch_context()),
            }

    def get_tasks(self) -> list[dict]:
        with self._lock:
            return self._tasks()

    def get_notification_tasks(self) -> list[dict]:
        """Include linked downloads so their completion is not hidden by ASR."""
        with self._lock:
            rows = []
            for job in self.controller.job_rows(include_archived=True):
                rows.append({
                    "task_id": f"job:{job['id']}", "kind": "transcription",
                    "status": job["status"], "updated_at": job.get("updated_at", ""),
                    "display_title": job.get("display_title") or job.get("source_title") or "转写任务",
                    "progress": job.get("progress"),
                    **{name: job.get(name) for name in (
                        "body_enabled_snapshot", "title_enabled_snapshot", "body_status", "title_status",
                        "original_source_title", "optimized_source_title",
                    ) if name in job},
                })
            for download in self.controller.downloads.list(limit=500):
                downloaded = bool(download.get("downloaded_path"))
                rows.append({
                    "task_id": f"dl:{download['id']}", "kind": "download",
                    "status": "done" if downloaded else download["status"],
                    "updated_at": download.get("updated_at", ""),
                    "display_title": download.get("title") or "下载任务",
                    # 下载进度里程碑 toast 依赖该字段；缺失则只报开始/终态。
                    "progress": download.get("progress"),
                })
            return rows

    def get_summary(self) -> dict:
        with self._lock:
            return self._summary()

    def get_settings(self) -> dict:
        with self._lock:
            return self._frontend_settings_view()

    # ------------------------------------------------------------ subscription
    def _manual_payment_request(self, path: str, fields: dict | None = None) -> dict:
        """Bind manual requests to the verified full session, ignoring stale replies.

        A request or a submitted receipt never grants local entitlements. Only the
        existing signed subscription refresh may apply confirmed ledger changes.
        """
        from engine.device import device_id

        with self._lock:
            base, token, management = self._account_context()
            if not base or not token or management is not None:
                return {"ok": False, "code": "login_required", "error": "请先登录 FoilPage"}
            if error := self._work_entry_error():
                return error
            revision = self._account_revision
            user = str(self.controller.settings.get("account_user_id") or "")
            payload = {**(fields or {}), "login_token": token,
                       "device_id": device_id(str(self.settings_store.base_dir))}
        result = self._account_request(base, "/v1/manual-payments/" + path, payload)
        with self._lock:
            if (revision != self._account_revision or not self._account_context_current(base, token, management)
                    or user != str(self.controller.settings.get("account_user_id") or "")
                    or self._work_entry_error() is not None):
                return {"ok": False, "code": "stale_account", "error": "登录已变更，请重新打开支付申请"}
        if result.get("ok") is not True:
            return {"ok": False, "error": str(result.get("error") or "支付申请未完成")[:300],
                    **({"status": "invalid"} if result.get("status") == "invalid" else {})}
        if path == "config":
            return result
        if path == "list":
            if not isinstance(result.get("requests"), list):
                return {"ok": False, "error": "支付申请响应无效"}
            return {"ok": True, "requests": [public for item in result["requests"][:20]
                    if (public := _public_manual_payment_request(item)) is not None]}
        public = _public_manual_payment_request(result.get("request"))
        if public is None or (fields and fields.get("request_id") and public["request_id"] != fields["request_id"]):
            return {"ok": False, "error": "支付申请响应无效"}
        if path == "create" and (public["snapshot"]["plan_id"] != fields["plan_id"] or public["snapshot"]["period"] != fields["period"]):
            return {"ok": False, "error": "支付报价与所选套餐不一致"}
        return {"ok": True, "request": public, "repeated": result.get("repeated") is True}

    def get_manual_payment_config(self) -> dict:
        result = self._manual_payment_request("config")
        if result.get("ok") is not True:
            return result
        contract = _public_manual_payment_contract(result)
        qr = result.get("qr_data_url")
        # No remote image requests or active SVG supplied through payment config.
        safe_qr = ""
        if isinstance(qr, str) and len(qr) <= 131072:
            match = re.fullmatch(r"data:image/(png|jpeg);base64,([A-Za-z0-9+/]+={0,2})", qr)
            if match:
                import base64
                try:
                    pixels = base64.b64decode(match[2], validate=True)
                    if ((match[1] == "png" and pixels.startswith(b"\x89PNG\r\n\x1a\n"))
                            or (match[1] == "jpeg" and pixels.startswith(b"\xff\xd8\xff"))):
                        safe_qr = qr
                except ValueError:
                    pass
        return {"ok": True, **contract, "qr_data_url": safe_qr,
                "instructions": str(result.get("instructions") or "")[:2000],
                "recipient_label": str(result.get("recipient_label") or "")[:100]}

    def list_manual_payment_requests(self) -> dict:
        return self._manual_payment_request("list", {"limit": 20})

    def create_manual_payment_request(self, plan_id: str, period: str, client_request_id: str) -> dict:
        if plan_id not in ("standard", "advanced") or period not in ("month", "year"):
            return {"ok": False, "error": "请选择已开放的套餐与周期"}
        if not isinstance(client_request_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{12,64}", client_request_id):
            return {"ok": False, "error": "支付申请编号无效"}
        return self._manual_payment_request("create", {"plan_id": plan_id, "period": period,
                                                       "client_request_id": client_request_id})

    def submit_manual_payment_request(self, request_id: str, transaction_id: str) -> dict:
        if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{12,64}", request_id):
            return {"ok": False, "error": "支付申请编号无效"}
        receipt = str(transaction_id or "").strip()
        if not re.fullmatch(r"[0-9]{16,64}", receipt):
            return {"ok": False, "error": "请填写支付宝账单中的完整交易号"}
        return self._manual_payment_request("submit", {"request_id": request_id, "transaction_id": receipt})

    def get_manual_payment_request(self, request_id: str) -> dict:
        if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{12,64}", request_id):
            return {"ok": False, "error": "支付申请编号无效"}
        return self._manual_payment_request("status", {"request_id": request_id})

    def cancel_manual_payment_request(self, request_id: str) -> dict:
        if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{12,64}", request_id):
            return {"ok": False, "error": "支付申请编号无效"}
        return self._manual_payment_request("cancel", {"request_id": request_id})

    def get_subscription_plans(self) -> list[dict]:
        return plans()

    def _alipay_order_request(self, action: str, fields: dict | None = None) -> dict:
        from engine.device import device_id
        with self._lock:
            base, token, management = self._account_context()
            if not base or not token or management is not None:
                return {"ok": False, "code": "login_required", "error": "请先登录 FoilPage"}
            if error := self._work_entry_error():
                return error
            context = (base, token, str(self.controller.settings.get("account_user_id") or ""), self._account_revision)
            payload = {**(fields or {}), "login_token": token, "device_id": device_id(str(self.settings_store.base_dir))}
        response = self._account_request(base, "/v1/alipay/orders/" + action, payload)
        with self._lock:
            if (not self._account_context_current(base, token, management)
                    or context[2:] != (str(self.controller.settings.get("account_user_id") or ""), self._account_revision)
                    or self._work_entry_error() is not None):
                self._alipay_cashiers = {}
                return {"ok": False, "code": "stale_account", "error": "登录已变更，请重新打开支付宝"}
            if response.get("ok") is not True:
                return {"ok": False, "error": str(response.get("error") or "支付操作未完成")[:300]}
            if action == "config":
                return {"ok": True, **_public_alipay_contract(response)}
            if action == "list":
                if not isinstance(response.get("orders"), list):
                    return {"ok": False, "error": "订单响应无效"}
                return {"ok": True, "orders": [public for item in response["orders"][:20]
                        if (public := _public_alipay_order(item)) is not None]}
            order = _public_alipay_order(response.get("order"))
            if (order is None or (fields and fields.get("order_id") and fields["order_id"] != order["order_id"])
                    or (action == "create" and (order["snapshot"]["plan_id"] != fields["plan_id"]
                                               or order["snapshot"]["period"] != fields["period"]))):
                return {"ok": False, "error": "订单响应与所选套餐不一致"}
            cache = {identifier: value for identifier, value in getattr(self, "_alipay_cashiers", {}).items()
                     if value["context"] == context and value["deadline"] > datetime.now(timezone.utc).timestamp()}
            if order["status"] in ("closed", "credited", "refund_review"):
                cache.pop(order["order_id"], None)
            elif "cashier_url" in response:
                deadline = _alipay_cashier_expiry(base, response.get("cashier_url"), response.get("cashier_expires_at"))
                if deadline is None:
                    cache.pop(order["order_id"], None)
                    self._alipay_cashiers = cache
                    return {"ok": False, "error": "收银台地址无效，请重新获取订单"}
                cache[order["order_id"]] = {"context": context, "url": response["cashier_url"], "deadline": deadline}
                if len(cache) > 20:
                    cache.pop(next(iter(cache)))
            self._alipay_cashiers = cache
            cached = cache.get(order["order_id"])
            available = bool(cached and cached["context"] == context
                             and cached["deadline"] > datetime.now(timezone.utc).timestamp())
            result = {"ok": True, "order": order, "repeated": response.get("repeated") is True,
                      "cashier_available": available, "query_pending": response.get("query_pending") is True}
            if available:
                result["cashier_expires_at"] = datetime.fromtimestamp(cached["deadline"], timezone.utc).isoformat()
            delay = response.get("retry_after")
            if isinstance(delay, int) and not isinstance(delay, bool) and 0 < delay <= 300:
                result["retry_after"] = delay
            return result

    def get_alipay_payment_config(self) -> dict:
        return self._alipay_order_request("config")

    def list_alipay_orders(self) -> dict:
        return self._alipay_order_request("list", {"limit": 20})

    def create_alipay_order(self, plan_id: str, period: str, client_request_id: str) -> dict:
        if plan_id not in ("standard", "advanced") or period not in ("month", "year"):
            return {"ok": False, "error": "请选择已开放的套餐与周期"}
        if not isinstance(client_request_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{12,64}", client_request_id):
            return {"ok": False, "error": "订单申请编号无效"}
        return self._alipay_order_request("create", {"plan_id": plan_id, "period": period, "client_request_id": client_request_id})

    def get_alipay_order(self, order_id: str, resume_payment: bool = False) -> dict:
        if not isinstance(order_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{12,64}", order_id):
            return {"ok": False, "error": "订单编号无效"}
        if not isinstance(resume_payment, bool):
            return {"ok": False, "error": "支付操作无效"}
        return self._alipay_order_request("status", {"order_id": order_id,
                                                  **({"resume_payment": True} if resume_payment else {})})

    def query_alipay_order(self, order_id: str) -> dict:
        if not isinstance(order_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{12,64}", order_id):
            return {"ok": False, "error": "订单编号无效"}
        return self._alipay_order_request("query", {"order_id": order_id})

    def open_alipay_cashier(self, order_id: str) -> dict:
        import webbrowser
        if not isinstance(order_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{12,64}", order_id):
            return {"ok": False, "error": "订单编号无效"}
        with self._lock:
            base, token, management = self._account_context()
            context = (base, token, str(self.controller.settings.get("account_user_id") or ""), self._account_revision)
            cached = getattr(self, "_alipay_cashiers", {}).get(order_id) if isinstance(order_id, str) else None
            if (not token or management is not None or self._work_entry_error() is not None
                    or (cached and cached["context"] != context)):
                self._alipay_cashiers = {}
                return {"ok": False, "error": "请重新打开订单获取收银台"}
            if not cached:
                return {"ok": False, "error": "请重新打开订单获取收银台"}
            if cached["deadline"] <= datetime.now(timezone.utc).timestamp():
                self._alipay_cashiers.pop(order_id, None)
                return {"ok": False, "error": "收银台已过期，请重新获取"}
            # The page supplies an order ID, never an arbitrary browser URL.
            try:
                opened = webbrowser.open(cached["url"])
            except Exception:
                opened = False
            return {"ok": bool(opened), **({"error": "无法打开浏览器，请重试"} if not opened else {})}

    def get_subscription_status(self) -> dict:
        result = self._subscription_status_locked()
        # 自愈（2026-11-02）：本地 license 过期/校验失败但登录令牌仍有效时，
        # 先同步续期一次再复评——登录会话有 30 天，掉的只是 license 时
        # 不应把用户顶到登录墙。
        if result.get("license_verified") is False and str(
                self.controller.settings.get("account_login_token") or "").strip():
            import time as _time
            with self._lock:
                due = _time.monotonic() - self._license_selfheal_at >= 60
                if due:
                    self._license_selfheal_at = _time.monotonic()
            if due:
                log.info("本地 license 失效但登录令牌存在，尝试同步续期自愈")
                self.refresh_subscription()
                result = self._subscription_status_locked()
        return result

    def _subscription_status_locked(self) -> dict:
        from engine.device import device_id
        with self._lock:
            current_device = device_id(getattr(self.settings_store, "base_dir", "") or ".") if self.controller.settings.get("subscription_license_public_key") else None
            result = subscription_status(self.controller.settings, device_id=current_device)
            base = str(self.controller.settings.get("subscription_server_url") or "").strip().rstrip("/")
            service = self._subscription_service if self._subscription_service_base == base else None
            raw_relay = (service or {}).get("agent_relay")
            relay = raw_relay if isinstance(raw_relay, dict) else {}
            ready = bool(base and relay.get("configured") is True and relay.get("stage_protocol") == "v1")
            paused = result.get("order_review", {}).get("paused") is True
            ready = ready and not paused
            # 托管模型目录（1.3.12）：服务端 /v1/health 下发的已复核模型，
            # 驱动设置页托管模型下拉；旧服务端无该字段时仅剩服务端默认。
            hosted_models = [
                {"model": str(item.get("model") or ""), "policy_version": str(item.get("policy_version") or "")}
                for item in (relay.get("models") if isinstance(relay.get("models"), list) else [])
                if isinstance(item, dict) and item.get("model")
            ]
            result.update(hosted_agent_ready=ready, hosted_agent_reason="" if ready else (
                "订单权益正在复核，托管已暂停" if paused else "请先检查托管服务" if base and not service else "托管模型尚未配置"
            ), hosted_agent_models=hosted_models)
            return result

    # ---------------------------------------------------- local video / chrome
    def _preview_server(self):
        from app.media_preview import MediaPreviewServer
        if self._media_preview is None:
            base = Path(str(self.settings_store.base_dir))
            self._media_preview = MediaPreviewServer(self.controller.media, base / "cache" / "video-covers",
                                                     ffmpeg_path=str(self.controller.settings.get("ffmpeg_path") or ""))
        return self._media_preview

    def get_downloaded_videos(self, force_scan: bool = False) -> dict:
        if type(force_scan) is not bool:
            return {"ok": False, "error": "视频刷新参数无效"}
        with self._lock:
            all_downloads = self.controller.downloads.list(limit=100000)
            jobs = self.controller.queue.list_jobs()
            excluded = [row['source_path'] for row in jobs
                        if row['status'] in {'queued', 'running', 'retryable'}]
            excluded += [row.get('downloaded_path') for row in all_downloads
                         if row.get('downloaded_path') and row['status'] in ACTIVE_STATUSES]
            scan = self.controller.video_catalog.request_scan(self.controller.settings, force=force_scan, exclude_paths=excluded)
            downloads = {row.get("media_id"): row for row in reversed(all_downloads) if row.get("media_id")}
            items = []
            for media in self.controller.media.list_media(media_type="video"):
                download = downloads.get(media["id"], {})
                available = Path(media["source_path"]).is_file()
                try:
                    urls = self._preview_server().urls(media["id"]) if available else {}
                except (FileNotFoundError, OSError, ValueError):
                    urls, available = {}, False
                action = self._video_delete_policy(media, all_downloads, jobs=jobs)
                items.append({"id": media["id"], "title": download.get("title") or Path(media["filename"]).stem,
                              "source": download.get("platform") or "本地导入", "source_url": download.get("url") or "",
                              "downloaded_at": download.get("updated_at") or media.get("created_at"),
                              "duration_seconds": media.get("duration_sec"), "available": available,
                              "thumbnail_url": urls.get("thumbnail_url", ""), **action})
            return {"ok": True, "items": items, "total": len(items), "scan": scan}

    def get_video_scan_status(self) -> dict:
        scan = self.controller.video_catalog.snapshot()
        return {"ok": True, "scan": scan, "catalog_revision": scan['revision']}

    def cancel_video_scan(self) -> dict:
        scan = self.controller.video_catalog.cancel_scan()
        return {"ok": True, "scan": scan, "catalog_revision": scan['revision']}

    def get_failed_task_retention(self) -> dict:
        return self.controller.failed_task_retention.preview(self.controller.settings)

    def _video_delete_policy(self, media: dict, downloads: list[dict], *, jobs: list[dict] | None = None) -> dict:
        from engine.queue_store import _source_paths_equivalent
        path = Path(media["source_path"]).expanduser().absolute()
        active = any(job["status"] in {"queued", "running", "retryable"} and
                     _source_paths_equivalent(job["source_path"], str(path))
                     for job in (self.controller.queue.list_jobs() if jobs is None else jobs))
        active = active or any(row.get("media_id") == media["id"] and row.get("status") not in TERMINAL_STATUSES for row in downloads)
        if active:
            return {"deletable": False, "delete_label": "删除视频", "deletion_note": "视频正在转写，请在任务结束或取消后删除。"}
        owned = False
        for row in downloads:
            if row.get("media_id") != media["id"] or not row.get("downloaded_path"):
                continue
            registered = Path(row["downloaded_path"]).expanduser().absolute()
            root = (row.get("options") or {}).get("managed_download_root")
            roots = [Path(self.controller.settings["download_dir"]), Path(self.settings_store.base_dir) / "downloads"]
            if root:
                roots.append(Path(root))
            if (row.get("options") or {}).get("mode") == "browser" and path.parent.name == "FoilPage":
                roots.append(path.parent)
            original_hash = (row.get("options") or {}).get("download_content_sha256") or media.get("original_content_sha256")
            if registered == path and path.resolve() == path and original_hash == media["content_sha256"] and any(r.expanduser().resolve() in path.parents for r in roots):
                owned = True
                break
        available = path.is_file()
        label = "删除视频" if owned and available else ("从视频库移除" if available else "移除记录")
        note = ("删除下载的视频文件，保留转写任务与文本结果。" if owned and available else
                "从视频库移除，保留本地原文件、转写任务与文本结果。" if available else
                "文件已不在原位置，仅移除视频库记录，保留转写任务与文本结果。")
        return {"deletable": True, "delete_label": label, "deletion_note": note, "delete_file": owned and available}

    def open_video_location(self, media_id: str) -> dict:
        from engine.media_store import MediaStoreError
        with self._lock:
            try:
                media = self.controller.media.get_media(str(media_id))
                if media["media_type"] != "video":
                    raise ValueError("not a video")
                return self.open_path(media["source_path"])
            except (OSError, ValueError, MediaStoreError):
                return {"ok": False, "error": "视频文件已移走或不存在。"}

    def delete_video(self, media_id: str) -> dict:
        from engine.media_store import MediaStoreError, _sha256
        with self._lock, self.controller.media_action_lock:
            try:
                media = self.controller.media.get_media(str(media_id))
                if media["media_type"] != "video":
                    raise ValueError("not a video")
                downloads = self.controller.downloads.list(limit=100000)
                policy = self._video_delete_policy(media, downloads)
                if not policy["deletable"]:
                    return {"ok": False, "error": policy["deletion_note"]}
                if policy["delete_file"]:
                    path = Path(media["source_path"])
                    if _sha256(path) != media["content_sha256"]:
                        return {"ok": False, "error": "文件内容已改变，请重新导入后再处理。"}
                    path.unlink()
                self.controller.media.delete_media(media["id"])
                for row in downloads:
                    if row.get("media_id") == media["id"]:
                        self.controller.downloads.transition(row["id"], row["status"], media_id=None,
                            downloaded_path=None if policy["delete_file"] else row.get("downloaded_path"),
                            options={**row["options"], "video_library_removed": True})
                result = self.get_downloaded_videos()
                return {"ok": True, "items": result["items"], "tasks": self._tasks(),
                        "message": "视频已删除" if policy["delete_file"] else "已从视频库移除"}
            except (OSError, ValueError, MediaStoreError):
                return {"ok": False, "error": "无法删除视频，请关闭播放器后重试。"}

    def get_video_preview(self, media_id: str) -> dict:
        from engine.media_store import MediaStoreError
        import mimetypes
        with self._lock:
            try:
                media = self.controller.media.get_media(str(media_id))
                urls = self._preview_server().urls(str(media_id))
                return {"ok": True, "title": Path(media["filename"]).stem,
                        "mime_type": mimetypes.guess_type(media["filename"])[0] or "video/mp4", **urls}
            except (OSError, ValueError, MediaStoreError):
                return {"ok": False, "error": "视频文件已移走或无法播放，请在任务中重新下载。"}

    def _gallery_source_row(self, task_id: str):
        raw = str(task_id or "")
        if raw.startswith("dl:"):
            return self.controller.downloads.get(raw[3:])
        if raw.startswith("job:"):
            job_id = raw[4:]
            for item in self.controller.downloads.list(limit=500):
                if item.get("job_id") == job_id:
                    return item
        return None

    def get_task_cover(self, task_id: str) -> dict:
        """工作台任务卡封面（TASK-8.17）：只服务下载库登记的 cover_path。

        转写行经关联下载行取同一封面；URL 由本地预览服务器带会话 token
        签发，不暴露文件路径。
        """
        from engine.media_store import MediaStoreError
        with self._lock:
            row = self._gallery_source_row(task_id)
            cover_path = str((row or {}).get("cover_path") or "")
            if not row or not cover_path:
                return {"ok": False, "error": "该任务没有封面"}
            try:
                url = self._preview_server().task_cover_url(str(row["id"]), Path(cover_path))
                return {"ok": True, "url": url}
            except (OSError, ValueError, MediaStoreError):
                return {"ok": False, "error": "封面文件已移走或无法读取。"}

    def get_task_gallery(self, task_id: str) -> dict:
        """检查器图集缩略网格（TASK-8.18）：列任务目录内已落盘的图片。"""
        from engine.media_store import MediaStoreError
        with self._lock:
            row = self._gallery_source_row(task_id)
            if not row or not (row.get("options") or {}).get("gallery_images"):
                return {"ok": False, "error": "该任务不是图集"}
            gallery_dir = row.get("downloaded_path")
            if not gallery_dir:
                return {"ok": False, "error": "图集尚未下载"}
            directory = Path(str(gallery_dir))
            if not directory.is_dir():
                return {"ok": False, "error": "图集目录已移走。"}
            files = sorted(
                (item for item in directory.iterdir()
                 if item.is_file() and item.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp", ".gif"}
                 and item.name != "cover.jpg"),
                key=lambda item: item.name,
            )[:60]
            if not files:
                return {"ok": False, "error": "图集目录为空"}
            try:
                urls = self._preview_server().gallery_image_urls(str(row["id"]), files)
                return {"ok": True, "count": len(files), "urls": urls}
            except (OSError, ValueError, MediaStoreError):
                return {"ok": False, "error": "图集图片无法读取。"}

    def close_media_preview(self) -> None:
        server, self._media_preview = self._media_preview, None
        if server:
            server.stop()

    def get_window_chrome(self) -> dict:
        window = self._window()
        enabled = bool(window is not None and getattr(window, "frameless", False) is True)
        maximized = False
        if enabled:
            try:
                import ctypes
                native = getattr(window, "native", None)
                if native is not None:
                    user = ctypes.windll.user32
                    user.IsZoomed.argtypes = [ctypes.c_void_p]
                    maximized = bool(user.IsZoomed(int(native.Handle.ToInt64())))
            except (AttributeError, OSError, TypeError):
                pass
        return {"enabled": enabled, "maximized": maximized, "native_drag": enabled and bool(self._native_drag())}

    def get_agent_setup_guide(self) -> dict:
        from app.agent_setup_guide import build_agent_setup_guide
        return build_agent_setup_guide(
            self.controller.settings, data_dir=self.settings_store.base_dir,
            app_version=APP_VERSION,
        )

    def get_agent_control_config(self, allow_write: bool = False) -> dict:
        from app.agent_control_config import build_agent_control_config
        return build_agent_control_config(self.settings_store.base_dir, allow_write=allow_write)

    def minimize_window(self) -> dict:
        window = self._window()
        if window is None:
            return {"ok": False}
        window.minimize()
        return {"ok": True}

    def toggle_maximize_window(self) -> dict:
        window = self._window()
        if window is None:
            return {"ok": False, "maximized": False}
        state = self.get_window_chrome()
        window.restore() if state["maximized"] else window.maximize()
        return {"ok": True, "maximized": not state["maximized"]}

    def hide_window(self) -> dict:
        window = self._window()
        if window is None:
            return {"ok": False}
        # destroy() raises the existing closing event; it may hide to tray.
        window.destroy()
        return {"ok": True}

    def begin_subscription(self, plan_id: str, period: str = "month") -> dict:
        plan_id = str(plan_id or "").strip()
        if period != "month":
            return {"ok": False, "error": "购买周期暂未开放", "message": "购买周期暂未开放"}
        contract = self.check_subscription_service()
        if not contract.get("checkout_ready") or plan_id not in contract.get("available_plans", []):
            return {"ok": False, "error": "该套餐暂未开放购买", "message": "该套餐暂未开放购买"}
        catalog_item = next((item for item in contract.get("public_catalog", []) if item["id"] == plan_id), None)
        matching = [offer for offer in (catalog_item or {}).get("offers", []) if offer.get("period") == period]
        if len(matching) != 1 or matching[0].get("status") != "ready" or matching[0].get("purchasable") is not True:
            return {"ok": False, "error": "该购买周期暂未开放", "message": "该购买周期暂未开放"}
        offer = matching[0]
        with self._lock:
            base = str(self.controller.settings.get("subscription_checkout_url") or self.controller.settings.get("subscription_server_url") or "")
            result = checkout(plan_id, checkout_base_url=base, period=period)
            if result.get("ok"):
                result["plan"].update(price=offer["price"], annual_price=catalog_item["annual_price"],
                                      period=period, credits=offer["credits"], months=offer["months"])
                result["message"] = "订阅支付接口尚未配置" if not result.get("checkout_url") else "正在打开支付页面"
            return result

    def activate_subscription_demo(self, plan_id: str) -> dict:
        with self._lock:
            updated = activate_local(self.controller.settings, str(plan_id or "").strip())
            self.controller.save_settings({k: updated[k] for k in ("subscription_plan", "subscription_active", "subscription_expires_at", "credit_balance")})
            return self.get_subscription_status()

    def redeem_beta_card(self, card_code: str, client_request_id: str = "") -> dict:
        """Redeem a shared beta card without trusting an unsigned grant or balance."""
        import urllib.error
        from engine.device import device_id

        if not isinstance(card_code, str) or not re.fullmatch(r"sjbeta_[a-f0-9]{64}", card_code.strip()):
            return {"ok": False, "code": "invalid_beta_card", "error": "请输入完整内测卡密"}
        if (not isinstance(client_request_id, str) or client_request_id
                and not re.fullmatch(r"[A-Za-z0-9_.:-]{16,128}", client_request_id)):
            return {"ok": False, "code": "invalid_request", "error": "兑换请求标识无效"}
        card = card_code.strip()
        request_id = client_request_id or secrets.token_hex(16)
        with self._lock:
            settings = dict(self.controller.settings)
            session = str(settings.get("account_login_token") or "").strip()
            user = str(settings.get("account_user_id") or "").strip()
            base = str(settings.get("subscription_server_url") or "").strip().rstrip("/")
            revision, profile_revision = self._account_revision, self._profile_revision
        if not session or not user:
            return {"ok": False, "code": "login_required", "error": "请先登录 FoilPage"}
        target = urllib.parse.urlsplit(base)
        if (target.scheme != "https" or not target.hostname or target.username or target.password
                or target.query or target.fragment or target.path not in ("", "/")):
            return {"ok": False, "code": "invalid_service", "error": "请先配置安全的 FoilPage 服务地址"}
        if not str(settings.get("subscription_license_public_key") or "").strip():
            return {"ok": False, "code": "verification_required", "error": "请先检查 FoilPage 服务连接"}
        body = {"login_token": session, "device_id": device_id(str(self.settings_store.base_dir)),
                "card_code": card, "client_request_id": request_id}
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                return None
        try:
            request = urllib.request.Request(base + "/v1/account/beta/redeem", data=json.dumps(body).encode("utf-8"),
                                             headers=payment_service_headers(), method="POST")
            with urllib.request.build_opener(NoRedirect).open(request, timeout=15) as response:
                raw = response.read(65537)
            if len(raw) > 65536:
                raise ValueError("beta response too large")
            result = json.loads(raw.decode("utf-8"))
        except urllib.error.HTTPError as exc:
            messages = {"beta_card_invalid": "内测卡不可用，请确认卡密或联系提供者", "beta_already_redeemed": "当前账户已兑换过内测卡",
                        "login_required": "请重新登录 FoilPage", "beta_disabled": "内测兑换暂不可用",
                        "rate_limited": "兑换过于频繁，请稍后重试", "beta_access_broken": "内测资格暂不可用，请联系支持"}
            code = "beta_redeem_failed"
            try:
                failure = json.loads(exc.read(65536).decode("utf-8"))
                remote_code = (failure.get("status") or failure.get("code")) if isinstance(failure, dict) else None
                if remote_code in messages:
                    code = remote_code
            except (TypeError, ValueError, OSError):
                pass
            return {"ok": False, "code": code, "error": messages.get(code, "暂时无法兑换内测卡，请稍后重试")}
        except Exception:
            return {"ok": False, "code": "beta_redeem_failed", "error": "暂时无法兑换内测卡，请稍后重试"}
        if not isinstance(result, dict) or result.get("ok") is not True or result.get("status") not in {"active", "revoked", "expired"}:
            return {"ok": False, "code": "beta_redeem_failed", "error": "内测兑换响应无效，请稍后重试"}
        applied = self._apply_remote_account(result, expected_session=session, expected_base=base,
            expected_revision=revision, expected_profile_revision=profile_revision, expected_user_id=user,
            expected_beta_status=result["status"])
        if not applied.get("ok"):
            return applied
        return {**self.get_subscription_status(), "ok": True, "repeated": result.get("repeated") is True,
                "redemption_status": result["status"]}

    def redeem_subscription_order(self, order_no: str) -> dict:
        """兑换爱发电订单：核销由付费服务反查完成，本地只验签与入账。

        subscription_server_url 指向付费服务；subscription_verify_key 为
        本地联调密钥（生产构建内嵌服务端公钥，不随设置下发）。
        """
        import urllib.request

        from engine.device import device_id

        order = str(order_no or "").strip()
        if not order:
            return {"ok": False, "error": "订单号不能为空"}
        settings = self.controller.settings
        base = str(settings.get("subscription_server_url") or "").strip().rstrip("/")
        if not base:
            return {"ok": False, "error": "尚未配置付费服务地址（subscription_server_url）"}
        device = device_id(getattr(self.settings_store, "base_dir", "") or ".")
        existing_session = str(settings.get("account_login_token") or "")
        profile_revision = self._profile_revision
        body = json.dumps({"order_no": order, "device_id": device, "login_token": existing_session}, ensure_ascii=False).encode("utf-8")
        try:
            request = urllib.request.Request(
                base + "/v1/redeem",
                data=body,
                headers=payment_service_headers(),
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=15) as response:
                result = json.loads(response.read().decode("utf-8"))
        except Exception as exc:  # 网络错误/非 200 响应都归为服务不可达
            return {"ok": False, "error": f"兑换服务不可达: {exc}"}
        if not isinstance(result, dict) or not result.get("ok"):
            return {"ok": False, "error": str((result or {}).get("error") or "订单核销失败")}
        applied = self._apply_remote_account(result, order_no=order, expected_session=existing_session,
            expected_base=base, expected_profile_revision=profile_revision)
        if not applied.get("ok"):
            return applied
        with self._lock:
            status = self.get_subscription_status()
        status["ok"] = True
        status["repeated"] = bool(result.get("repeated"))
        return status

    # ---------------------------------------------- 订阅服务端合同
    def _apply_remote_account(self, result: dict, *, order_no: str = "",
                              expected_session: str | None = None, expected_pending: str | None = None,
                              expected_base: str | None = None, expected_revision: int | None = None,
                              expected_profile_revision: int | None = None, expected_user_id: str | None = None,
                              expected_beta_status: str | None = None) -> dict:
        """Verify the signed license before updating account or balance locally."""
        from engine.device import device_id
        from engine.license import LicenseError, verify_license, signed_beta_access
        settings = self.controller.settings
        public_key = str(settings.get("subscription_license_public_key") or "").strip()
        key = public_key or str(settings.get("subscription_verify_key") or "").strip()
        if not key:
            return {"ok": False, "error": "尚未配置许可证公钥"}
        token = str(result.get("license_token") or "")
        try:
            payload = verify_license(token, signing_key=key,
                expected_alg="EdDSA" if public_key else "HS256",
                device_id=device_id(str(getattr(self.settings_store, "base_dir", "") or ".")))
            account = result.get("account") or result.get("status") or {}
            if not isinstance(account, dict):
                raise ValueError("账户格式无效")
            if account.get("user_id") and str(account["user_id"]) != payload["sub"]:
                raise ValueError("账户与许可证不一致")
            if expected_user_id is not None and payload["sub"] != expected_user_id:
                raise ValueError("许可证与当前登录账户不一致")
            if expected_beta_status is not None:
                beta = signed_beta_access(payload)
                if (expected_beta_status == "active" and beta["active"] is not True
                        or expected_beta_status == "revoked" and beta["revoked"] is not True
                        or expected_beta_status == "expired" and (beta["active"] or not beta["expires_at"]
                            or datetime.fromisoformat(beta["expires_at"]) > datetime.now(timezone.utc))):
                    raise ValueError("内测资格与签名许可证不一致")
            balance = payload.get("credit_balance", 0)
            if type(balance) is not int or balance < 0:
                raise ValueError("积分余额无效")
        except (LicenseError, TypeError, ValueError):
            return {"ok": False, "error": "许可证校验失败，请检查服务配置"}
        changes = {
            "license_token": token, "license_expires_at": payload.get("exp"),
            "subscription_plan": payload.get("plan_id") or "free",
            "subscription_active": bool(account.get("active")),
            "subscription_expires_at": account.get("expires_at"), "credit_balance": balance,
        }
        if result.get('account_access_token'):
            from engine.account_access import account_access
            access_settings = {**settings, **changes, 'account_user_id':payload['sub'],
                'account_login_token':result.get('login_token') or settings.get('account_login_token') or '',
                'account_access_token':result['account_access_token']}
            if not account_access(access_settings, device_id=device_id(str(self.settings_store.base_dir)))['allowed']:
                return {'ok':False,'error':'登录凭证校验失败，请重试'}
            changes['account_access_token'] = result['account_access_token']
        elif result.get('login_token') and result['login_token'] != settings.get('account_login_token'):
            changes['account_access_token'] = ''
        if result.get("login_token"):
            changes["account_login_token"] = str(result["login_token"])
            if expected_pending is not None or result['login_token'] != settings.get('account_login_token'):
                changes["account_pending_login_token"] = ""
        if account.get("user_id"):
            changes["account_user_id"] = str(account["user_id"])
            changes["account_display_name"] = str(account.get("display_name") or "爱发电账户")
            from engine.account_profile import validate_avatar
            try:
                changes["account_avatar"] = validate_avatar(account.get("avatar") or "") or ""
            except ValueError:
                # A malformed image cannot turn a verified login into a failure.
                changes["account_avatar"] = ""
        if order_no:
            changes["subscription_last_order_id"] = order_no
        with self._lock:
            current = self.controller.settings
            if expected_revision is not None and self._account_revision != expected_revision:
                return {"ok": False, "error": "登录请求已变更，已忽略旧响应"}
            if expected_session is not None and str(current.get("account_login_token") or "") != expected_session:
                return {"ok": False, "error": "账户已变更，已忽略旧响应"}
            if expected_user_id is not None and str(current.get("account_user_id") or "").strip() != expected_user_id:
                return {"ok": False, "error": "账户已变更，已忽略旧响应"}
            if expected_pending is not None and str(current.get("account_pending_login_token") or "") != expected_pending:
                return {"ok": False, "error": "登录请求已变更，已忽略旧响应"}
            if expected_base is not None and str(current.get("subscription_server_url") or "").strip().rstrip("/") != expected_base:
                return {"ok": False, "error": "服务配置已变更，已忽略旧响应"}
            current_key = str(current.get("subscription_license_public_key") or "").strip() or str(current.get("subscription_verify_key") or "").strip()
            if current_key != key:
                return {"ok": False, "error": "许可证配置已变更，已忽略旧响应"}
            if (expected_profile_revision is not None and self._profile_revision != expected_profile_revision
                    and str(current.get("account_user_id") or "") == payload["sub"]):
                # A request issued before a successful profile edit may still
                # carry an older account view. Apply its verified entitlement,
                # while retaining the more recently saved name and avatar.
                changes.pop("account_display_name", None)
                changes.pop("account_avatar", None)
            history = current.get("subscription_expiry_notice_history")
            service = str(current.get("subscription_server_url") or "").rstrip("/")
            if not isinstance(history, dict) or history.get("account") != payload["sub"] or history.get("service") != service:
                history = None
            observed_expiry = account.get("expires_at") if account.get("active") and payload.get("plan_id") in {"standard", "advanced"} else None
            if not observed_expiry and current.get("account_user_id") == payload["sub"] and current.get("subscription_plan") in {"standard", "advanced"}:
                observed_expiry = current.get("subscription_expires_at")
            if observed_expiry:
                history = {"account": payload["sub"], "service": service, "expires_at": observed_expiry}
            changes["subscription_expiry_notice_history"] = history
            self.controller.save_settings(changes)
            if "account_login_token" in changes:
                attempt = getattr(self, "_google_login_pending", None)
                if attempt and expected_pending == attempt["session_id"]:
                    # This session was successfully consumed. A later cleanup
                    # must never revoke the newly installed full login.
                    self._google_login_pending = None
                self._account_management = None
                self._account_revision += 1
                web_attempt = getattr(self,'_web_login_pending',None)
                if web_attempt and self.controller.settings.get('account_pending_login_token') == web_attempt['session_id']:
                    web_attempt['revision'] = self._account_revision
                self._buyer_verification = None
        return {"ok": True}

    def refresh_subscription(self) -> dict:
        """Synchronize repeat purchases and rolling licenses; never trust unsigned status."""
        import urllib.request
        import urllib.error
        settings = self.controller.settings
        base = str(settings.get("subscription_server_url") or "").strip().rstrip("/")
        token = str(settings.get("account_login_token") or "").strip()
        profile_revision = self._profile_revision
        if not base or not token:
            return {"ok": False, "error": "登录后可同步积分"}

        def rejected_session():
            # A late failure from another service/session cannot clear the new
            # account. _clear_account also checks the captured session under
            # this same reentrant lock.
            with self._lock:
                current_base = str(self.controller.settings.get("subscription_server_url") or "").strip().rstrip("/")
                current_session = str(self.controller.settings.get("account_login_token") or "")
                if current_base != base or current_session != token:
                    return {"ok": False, "error": "登录状态已变更，已忽略旧响应"}
                self._clear_account(expected_session=token)
            return {"ok": False, "error": "登录已失效，请重新登录"}

        temporary_failure = {"ok": False, "error": "暂时无法同步，已保留本机登录；请稍后重试"}
        def temporary(reason: str) -> dict:
            # 此前完全静默：license 同步连续失败一小时无任何日志（2026-11-02）。
            log.warning("订阅同步失败（保留本机登录）：%s", reason)
            return temporary_failure
        try:
            body = json.dumps({"login_token": token}).encode("utf-8")
            request = urllib.request.Request(base + "/v1/license/refresh", data=body,
                headers=payment_service_headers(), method="POST")
            with urllib.request.urlopen(request, timeout=12) as response:
                result = json.loads(response.read().decode("utf-8"))
            if not isinstance(result, dict) or result.get("ok") is not True:
                return rejected_session() if _subscription_session_rejected(result) else temporary("响应不是成功 JSON")
            applied = self._apply_remote_account(result, expected_session=token, expected_base=base,
                expected_profile_revision=profile_revision)
            return {**applied, "status": self.get_subscription_status()} if applied.get("ok") else applied
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                return rejected_session()
            if exc.code in (403, 409):
                try:
                    raw = exc.read(65537)
                    failure = json.loads(raw.decode("utf-8")) if len(raw) <= 65536 else None
                    if _subscription_session_rejected(failure):
                        return rejected_session()
                except (OSError, UnicodeError, ValueError, TypeError):
                    pass
            return temporary(f"HTTP {exc.code}")
        except Exception as exc:
            return temporary(type(exc).__name__)

    def sync_subscription_in_background(self) -> None:
        import time
        with self._lock:
            settings = self.controller.settings
            if not settings.get("subscription_server_url") or not settings.get("account_login_token"):
                return
            if time.monotonic() - self._subscription_sync_at < 300:
                return
            self._subscription_sync_at = time.monotonic()
        threading.Thread(target=self.refresh_subscription, name="subscription-sync", daemon=True).start()

    def _notice_context(self):
        from engine.product_events import notice_subject
        settings = self.controller.settings
        base = str(settings.get('subscription_server_url') or '').strip().rstrip('/')
        token, user = str(settings.get('account_login_token') or ''), str(settings.get('account_user_id') or '')
        valid = bool(token and user and self._work_entry_error() is None)
        return base, token, user, self._account_revision, notice_subject(base, user) if valid else ''

    @staticmethod
    def _product_request(base, path, payload=None):
        # Never redirect a credential-bearing request or accept an unbounded
        # public feed. Production notices come from the configured HTTPS service.
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                return None
        target = urllib.parse.urlparse(base)
        if target.scheme != 'https' or not target.hostname or target.username or target.password or target.query or target.fragment:
            return {'ok': False, 'error': '更新服务地址无效'}
        try:
            request = urllib.request.Request(base + path,
                data=json.dumps(payload).encode('utf-8') if payload is not None else None,
                headers=payment_service_headers(), method='POST' if payload is not None else 'GET')
            with urllib.request.build_opener(NoRedirect).open(request, timeout=12) as response:
                raw = response.read(65537)
            if len(raw) > 65536:
                raise ValueError('notice response too large')
            value = json.loads(raw.decode('utf-8'))
            return value if isinstance(value, dict) else {'ok': False}
        except Exception:
            return {'ok': False, 'error': '暂时无法读取通知'}

    def get_product_notifications(self) -> dict:
        with self._lock:
            context = self._notice_context()
            inbox = getattr(self.controller, 'product_events', None)
            return inbox.snapshot(context[-1], 'test' if RELEASE_CHANNEL == 'test' else 'stable', APP_VERSION) if inbox else {'ok': True, 'events': [], 'updates': []}

    def sync_product_notifications_in_background(self) -> None:
        import time
        with self._lock:
            context = self._notice_context()
            if not context[0] or self._product_sync_busy or time.monotonic() - self._product_sync_at < 60:
                return
            self._product_sync_busy, self._product_sync_at = True, time.monotonic()
            fetch_announcements = self._announcement_sync_at == 0 or time.monotonic() - self._announcement_sync_at >= 21600
            if fetch_announcements:
                self._announcement_sync_at = time.monotonic()
        def run():
            fresh = []
            try:
                base, token, user, revision, subject = context
                if subject:
                    from engine.device import device_id
                    result = self._product_request(base, '/v1/account/notifications', {
                        'login_token': token, 'device_id': device_id(str(self.settings_store.base_dir)),
                        'after': self.controller.product_events.cursor(subject), 'limit': 50})
                    with self._lock:
                        if context != self._notice_context():
                            return
                        if result.get('ok') is True:
                            fresh.extend(self.controller.product_events.accept_account(subject, result))
                    if fresh:
                        self.refresh_subscription()  # Rights come only from the signed license.
                if fetch_announcements:
                    fresh.extend(self._fetch_announcements(base))
                with self._lock:
                    if context != self._notice_context():
                        return
                    if self._notifier and self.controller.settings.get('task_notifications', True):
                        for event in fresh:
                            self._notifier(event['title'], event['message'])
            except (ValueError, OSError):
                pass  # Notices cannot invalidate a session or grant entitlements.
            finally:
                with self._lock:
                    self._product_sync_busy = False
        threading.Thread(target=run, name='product-notifications', daemon=True).start()

    def _fetch_announcements(self, base):
        channel = 'test' if RELEASE_CHANNEL == 'test' else 'stable'
        response = self._product_request(base, '/v1/announcements?channel=' + channel)
        return self.controller.product_events.accept_announcements(channel, response, APP_VERSION) if response.get('ok') is True else []

    def check_subscription_service(self) -> dict:
        import urllib.request
        base = str(self.controller.settings.get("subscription_server_url") or "").strip().rstrip("/")
        if not base:
            return self.get_subscription_contract()
        try:
            request = urllib.request.Request(base + "/v1/health", headers=payment_service_headers())
            with urllib.request.urlopen(request, timeout=10) as response:
                value = json.loads(response.read().decode("utf-8"))
            if not isinstance(value, dict) or value.get("service") != "shengjian-payment" or value.get("ok") is not True:
                raise ValueError("服务响应异常")
            with self._lock:
                if str(self.controller.settings.get("subscription_server_url") or "").strip().rstrip("/") == base:
                    self._subscription_service = value
                    self._subscription_service_base = base
        except Exception:
            with self._lock:
                self._subscription_service = {"readiness": {"ready": False, "blocking": ["服务暂时不可达"]}}
                self._subscription_service_base = base
        return self.get_subscription_contract()

    def get_subscription_contract(self) -> dict:
        """Configuration readiness is distinct from an actual paid-order acceptance."""
        with self._lock:
            settings = self.controller.settings
            server_url = str(settings.get("subscription_server_url") or "").strip()
            gates = []
            if not server_url:
                gates.append("配置服务地址")
            if not settings.get("subscription_license_public_key"):
                gates.append("配置许可证公钥")
            else:
                import base64
                public = str(settings["subscription_license_public_key"]).strip()
                try:
                    if len(base64.b64decode(public + "=" * (-len(public) % 4), altchars=b"-_", validate=True)) != 32:
                        raise ValueError
                except ValueError:
                    gates.append("检查许可证公钥格式")
            service = self._subscription_service if self._subscription_service_base == server_url.rstrip("/") else None
            service = service or {}
            readiness = service.get("readiness") if isinstance(service.get("readiness"), dict) else {}
            if not service:
                gates.append("检查生产服务配置")
            elif not readiness.get("ready"):
                gates.extend(str(item.get("message") or item.get("code") or "生产服务配置未完成") if isinstance(item, dict) else str(item) for item in (readiness.get("blocking") or ["生产服务配置未完成"]))
            if service and service.get("license_alg") != "EdDSA":
                gates.append("服务端需启用生产许可证")
            relay = service.get("agent_relay") if isinstance(service.get("agent_relay"), dict) else {}
            if service and (relay.get("configured") is not True or relay.get("stage_protocol") != "v1"):
                gates.append("配置可用的托管模型与分阶段接口")
            buyer = service.get("buyer_verification") if isinstance(service.get("buyer_verification"), dict) else {}
            buyer_ready = buyer.get("required") is True and buyer.get("configured") is True and buyer.get("provider") == "afdian"
            if service and not buyer_ready:
                gates.append("配置爱发电买家身份验证")
            catalog = _public_subscription_catalog(service.get("public_catalog"))
            declared_plans = service.get("plans") if isinstance(service.get("plans"), list) else []
            available_plans = [item["id"] for item in catalog if item["purchasable"] and item["id"] in declared_plans]
            if service and not available_plans:
                gates.append("配置服务端价目")
            gates.append("真实登录和订单验收")
            configuration_ready = len(gates) == 1
            payments = service.get("payments") if isinstance(service.get("payments"), dict) else {}
            purchase_open = payments.get("redeem") is True
            if service and not purchase_open:
                gates.append("购买与兑换暂未开放")
            return {"ok": True, "client": "implemented", "server_url": server_url,
                "provider": "afdian", "configuration_ready": configuration_ready,
                "checkout_ready": configuration_ready and bool(available_plans) and purchase_open,
                "available_plans": available_plans, "public_catalog": catalog, "readiness": readiness,
                "production_ready": False, "login_methods": {name: methods.get(name) is True for name in ("email", "google", "github", "web", "wechat")}
                    if isinstance(methods := service.get("login_methods"), dict) else {},
                "payments": payments, "gates": gates,
                "terms": {"version": value} if type(value := service.get("terms", {}).get("version") if isinstance(service.get("terms"), dict) else None) is int and 0 < value <= 1_000_000 else {},
                "manual_payments": _public_manual_payment_contract(service.get("manual_payments")),
                "alipay_payments": _public_alipay_contract(service.get("alipay_payments")),
                "buyer_verification": {"required": True, "configured": buyer_ready, "provider": "afdian"},
                "acceptance": dict(self._subscription_acceptance) if self._subscription_acceptance else None}

    def run_subscription_acceptance(self) -> dict:
        """运行本地闭环验收（仅 127.0.0.1 临时端口，不接触外部网络）。"""
        from engine.subscription_server import run_acceptance

        with self._lock:
            if self._subscription_acceptance is not None and self._subscription_acceptance.get("running"):
                return {"ok": False, "error": "验收正在运行，请稍候"}
            self._subscription_acceptance = {"running": True}
        try:
            evidence = run_acceptance()
            evidence["running"] = False
            evidence["ok"] = evidence.get("overall") == "ok"
            with self._lock:
                self._subscription_acceptance = evidence
            return {"ok": True, "evidence": evidence}
        except Exception as exc:
            failed = {"running": False, "ok": False, "overall": "failed", "error": str(exc)}
            with self._lock:
                self._subscription_acceptance = failed
            return {"ok": False, "error": str(exc), "evidence": failed}

    # ------------------------------------------------------------ 更新
    def check_update(self) -> dict:
        """Public binaries live separately from the private source repository."""
        from engine.product_events import release_url
        try:
            self._fetch_announcements(self._notice_context()[0])
            updates = self.get_product_notifications()['updates']
            if updates:
                newest = max(updates, key=lambda item: _parse_version(item['version']))
                return {'ok': True, 'current': APP_VERSION, 'latest': newest['version'],
                        'is_latest': _parse_version(APP_VERSION) >= _parse_version(newest['version']), 'url': newest['url']}
            request = urllib.request.Request(
                UPDATE_URL if RELEASE_CHANNEL != 'test' else UPDATE_URL.removesuffix('/latest'),
                headers={"User-Agent": "shengjian-desktop", "Accept": "application/vnd.github+json"},
            )
            with urllib.request.urlopen(request, timeout=6) as response:
                data = json.loads(response.read().decode("utf-8"))
            if RELEASE_CHANNEL == 'test':
                data = max((item for item in data if isinstance(item, dict) and item.get('prerelease') is True),
                           key=lambda item: _parse_version(item.get('tag_name')), default={})
            latest_tag = str(data.get("tag_name") or "").lstrip("v")
            latest_url = str(data.get("html_url") or "")
            if not release_url(latest_url) or not _parse_version(latest_tag):
                raise ValueError('invalid release')
            current = _parse_version(APP_VERSION)
            latest = _parse_version(latest_tag)
            return {
                "ok": True,
                "current": APP_VERSION,
                "latest": latest_tag or "unknown",
                "is_latest": bool(current and latest and current >= latest),
                "url": latest_url,
            }
        except Exception:
            return {"ok": False, "error": "无法连接更新服务", "current": APP_VERSION}

    # ------------------------------------------------------- 数据自检
    def data_reconcile(self) -> dict:
        """数据自检：悬空任务收敛 + 孤儿产物清单。"""
        with self._lock:
            return reconcile(self.controller)

    def cleanup_orphans(self, outputs: list, downloads: list) -> dict:
        """按用户确认清理孤儿产物（路径经 controller 白名单校验）。"""
        removed_outputs: list[str] = []
        removed_downloads: list[str] = []
        with self._lock:
            for value in outputs or []:
                if isinstance(value, str) and self.controller.purge_orphan_output(value):
                    removed_outputs.append(value)
            for value in downloads or []:
                if isinstance(value, str) and self.controller.purge_orphan_download(value):
                    removed_downloads.append(value)
        return {
            "ok": True,
            "removed_outputs": removed_outputs,
            "removed_downloads": removed_downloads,
        }

    # ------------------------------------------------------- 备份与诊断
    def pick_save_file(self, default_name: str = "") -> dict:
        import webview

        window = self._window()
        if window is None:
            return {"ok": False, "error": "窗口不可用"}
        result = window.create_file_dialog(webview.FileDialog.SAVE, save_filename=default_name or "shengjian.zip")
        if not result:
            return {"ok": False, "cancelled": True}
        return {"ok": True, "path": str(result[0])}

    def pick_open_file(self) -> dict:
        import webview

        window = self._window()
        if window is None:
            return {"ok": False, "error": "窗口不可用"}
        result = window.create_file_dialog(webview.FileDialog.OPEN, file_types=("ZIP 备份 (*.zip)",))
        if not result:
            return {"ok": False, "cancelled": True}
        return {"ok": True, "path": str(result[0])}

    def export_backup(self, dest_path: str) -> dict:
        with self._lock:
            return _export_backup(self.controller, self.settings_store, dest_path)

    def import_backup(self, zip_path: str) -> dict:
        with self._lock:
            if self._worker_state()["running"]:
                return {"ok": False, "error": "请先在设置 → 模型停止 Worker，再导入备份"}
            if self._batch_agent.get("running"):
                return {"ok": False, "error": "请先等待批量整理结束，再导入备份"}
            api = self._local_api()
            was_running = api is not None and api.running
            if was_running:
                api.stop()
            try:
                return _import_backup(self.controller, self.settings_store, zip_path)
            finally:
                if was_running:
                    api.start()

    def export_diagnostics(self, dest_path: str) -> dict:
        with self._lock:
            return _export_diagnostics(
                self.controller, self.settings_store, dest_path, app_version=APP_VERSION,
            )

    def get_worker(self) -> dict:
        with self._lock:
            return self._worker_state()

    # -------------------------------------------------------------- tasks
    def _tasks(self, rows: list[dict] | None = None) -> list[dict]:
        # rows 允许调用方（push_state/get_bootstrap）复用一次 task_rows 查询，
        # 避免 2s 推送里 _tasks + _summary 各查一遍全量任务。
        if rows is None:
            rows = self.controller.task_rows()
        rows_out = []
        for task in rows:
            error = task.get("error")
            if not error:
                for stage in ("body", "title"):
                    if task.get(f"{stage}_status") == "skipped" and task.get(f"{stage}_error_code"):
                        error = {"code": task[f"{stage}_error_code"], "message": task.get(f"{stage}_error", "AI 整理未完成"),
                                 "retryable": True, "diagnostic": {"phase": stage, "transcript_preserved": True}}
                        break
            error_details = _safe_task_error_details(error)
            error_message = (
                error_details.get("message", "") if error_details
                else (str(error) if error else "")
            )
            rows_out.append({
                "task_id": (f"job:{task['task_id']}" if task.get("kind") == "transcription" else f"dl:{task['task_id']}"),
                "kind": task.get("kind", "transcription"),
                "status": task.get("status", ""),
                "platform": task.get("platform_hint", ""),
                "display_title": str(task.get("display_title") or "未命名"),
                "source": task.get("source", ""),
                "progress": _safe_progress(task.get("progress")),
                "progress_text": _task_progress_text(task),
                "result_category": task.get("result_category", "未分类"),
                "updated_at": str(task.get("updated_at") or "").replace("T", " "),
                "error": str(error_message or ""),
                "error_details": error_details,
                "final_path": task.get("final_path") or "",
                "raw_path": task.get("raw_path") or "",
                "source_path": task.get("source_path") or "",
                "downloaded_path": task.get("downloaded_path") or "",
                "cover_path": task.get("cover_path") or "",
                "is_gallery": bool((task.get("options") or {}).get("gallery_images")),
                "icon": _task_media_meta(task)[0],
                "meta": _task_media_meta(task)[1],
            })
        return rows_out

    def _summary(self, rows: list[dict] | None = None) -> dict:
        tasks = rows if rows is not None else self.controller.task_rows()
        total = len(tasks)
        active = sum(t["status"] in ACTIVE_STATUSES for t in tasks)
        terminal = sum(t["status"] in TERMINAL_STATUSES for t in tasks)
        completed = sum(t["status"] == "done" for t in tasks)
        # 总体百分比只表示成功完成的任务，避免把“转写中的 46%”
        # 与已完成任务平均后误读成整体已完成度；失败和取消不算完成。
        value = round(completed / total, 4) if total else 0.0
        return {
            "total": total,
            "active": active,
            "terminal": terminal,
            "completed": completed,
            "progress": value,
            "text": f"完成度 {value:.0%}（{completed}/{total} 已完成" + (f"，{active} 进行中" if active else "") + "）",
        }

    def cancel_tasks(self, task_ids: list[str]) -> dict:
        with self._lock:
            trans = _prefixed_ids(task_ids, "job:")
            downs = _prefixed_ids(task_ids, "dl:")
            self.controller.cancel_jobs(trans)
            self.controller.cancel_download_tasks(downs)
            return {"ok": True}

    def requeue_tasks(self, task_ids: list[str]) -> dict:
        with self._lock:
            if error := self._processing_entry_error():
                return error
            jobs = _prefixed_ids(task_ids, "job:")
            requeued = self.controller.requeue_jobs(jobs)
            return {"ok": True, "requeued": [f"job:{j}" for j in requeued]}

    def retranscribe_tasks(self, task_ids: list[str], mode: str = "default",
                           editing_level: str = "") -> dict:
        """重新转写（TASK-8.24 起可选模式：default=沿用全局 / raw=仅原稿 /
        article=原稿＋AI 整理稿，后两者按单任务覆盖，不受全局开关牵制）。
        editing_level（2026-11-02）：本次整理力度覆盖，空串沿用全局设置。"""
        if mode not in ("default", "raw", "article"):
            return {"ok": False, "error": "未知的重新转写模式"}
        if editing_level and editing_level not in ("auto", "light", "balanced", "structured", "rewrite"):
            return {"ok": False, "error": "整理力度无效"}
        with self._lock:
            if error := self._processing_entry_error():
                return error
            jobs = _prefixed_ids(task_ids, "job:")
            result = self.controller.retranscribe_jobs(jobs, mode=mode, editing_level=editing_level)
            return {"ok": True, "created": result.created, "skipped": result.skipped}

    def retry_downloads(self, task_ids: list[str]) -> dict:
        """重试失败的下载行。

        - 非抖音平台：走本地重试（yt-dlp 重新解析，B 站/YouTube 可成）。
        - 抖音失败行：签名/播放地址有时效且进程内直链已失，桌面端无法
          自行恢复——返回 needs_page 让前端引导回视频页重新点击下载。
          （此前检查器的「重试」对下载行误调转写接口，id 被静默丢弃，
          点击无任何效果与提示。）
        """
        with self._lock:
            if error := self._processing_entry_error():
                return error
            downs = _prefixed_ids(task_ids, "dl:")
            retried: list[str] = []
            needs_page: list[dict] = []
            skipped = 0
            for download_id in downs:
                item = self.controller.downloads.get(download_id)
                if item is None:
                    skipped += 1
                    continue
                if item.get("status") == "done":
                    skipped += 1
                    continue
                if item.get("platform_hint") == "douyin":
                    needs_page.append({
                        "task_id": f"dl:{download_id}",
                        "url": item.get("url", ""),
                        "title": item.get("display_title") or item.get("title") or "",
                    })
                    continue
                if self.controller.retry_download_tasks([download_id]):
                    retried.append(f"dl:{download_id}")
                else:
                    skipped += 1
            return {
                "ok": True,
                "retried": retried,
                "needs_page": needs_page,
                "skipped": skipped,
            }

    def open_chrome_extensions(self) -> dict:
        """打开浏览器的扩展管理页（chrome://extensions 或 edge://extensions）。

        先找 Chrome（注册表 App Paths），其次 Edge，都没有时退回打开
        本应用的扩展目录（explorer），保证连接向导永远有下一步可走。
        """
        import winreg

        def _app_path(exe: str) -> str:
            for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
                try:
                    with winreg.OpenKey(
                        root,
                        r"Software\Microsoft\Windows\CurrentVersion\App Paths" + "\\" + exe,
                    ) as key:
                        value, _ = winreg.QueryValueEx(key, None)
                        if value and Path(value).is_file():
                            return value
                except OSError:
                    continue
            return ""

        candidates = [
            (_app_path("chrome.exe"), "chrome://extensions/"),
            (_app_path("msedge.exe"), "edge://extensions/"),
        ]
        for exe, url in candidates:
            if exe:
                try:
                    subprocess.Popen([exe, url], close_fds=True)
                    return {"ok": True, "browser": Path(exe).stem}
                except OSError:
                    continue
        try:
            os.startfile(str(_browser_extension_dir()))  # noqa: S606 - explorer fallback
            return {"ok": True, "browser": "explorer"}
        except OSError:
            return {"ok": False, "error": "未找到可用的浏览器"}

    def open_video_page(self, task_id: str) -> dict:
        """在系统浏览器打开任务的视频页（仅 http/https，供失败行回页重下）。"""
        bare = task_id[3:] if isinstance(task_id, str) and task_id.startswith("dl:") else task_id
        with self._lock:
            item = self.controller.downloads.get(bare) if bare else None
        url = str((item or {}).get("url") or "")
        try:
            parsed = urllib.parse.urlparse(url)
            if parsed.scheme not in ("http", "https") or not parsed.netloc:
                return {"ok": False, "error": "该任务没有可打开的视频页地址"}
        except ValueError:
            return {"ok": False, "error": "该任务没有可打开的视频页地址"}
        import webbrowser
        webbrowser.open(url)
        return {"ok": True}

    # ------------------------------------------------------- 单篇翻译（TASK-8.24）
    def translate_text(self, task_id: str, direction: str = "auto") -> dict:
        """对单篇已完成文本发起中英翻译（先生 2026-10-25）。

        托管模式计费搭既有 body 阶段积分；BYOK 走用户自配 API。同一时间仅
        一个翻译在跑，进度经 get_translation_status 轮询。
        """
        with self._lock:
            bare = task_id[4:] if isinstance(task_id, str) and task_id.startswith("job:") else task_id
            if not isinstance(bare, str) or not bare:
                return {"ok": False, "error": "任务不存在"}
            if direction not in ("auto", "zh-en", "en-zh"):
                return {"ok": False, "error": "翻译方向无效"}
            job = self.controller.queue.get_job(bare)
            if job is None or job.get("status") != "done":
                return {"ok": False, "error": "只有已完成的文本才能翻译"}
            manifest = self.controller.queue.get_manifest(bare)
            if not manifest or not (manifest.get("final_path") or manifest.get("raw_path")):
                return {"ok": False, "error": "未找到可翻译的文本"}
            settings = self.controller.settings
            if not settings.get("agent_enabled"):
                return {"ok": False, "error": "请先在设置 - 整理与转译中启用 Agent 转译"}
            if settings.get("agent_billing_mode") == "hosted":
                if not self.get_subscription_status().get("hosted_agent_ready"):
                    return {"ok": False, "error": "请先检查托管模型服务与账户积分"}
            elif not str(settings.get("agent_api_key") or "").strip():
                return {"ok": False, "error": "请先在设置 - 整理与转译中配置 API Key"}
            if self._translation.get("running"):
                return {"ok": False, "error": "已有翻译进行中，请稍候"}
            self._translation = {"running": True, "task_id": bare, "status": "running",
                                 "error": "", "lang": ""}
        threading.Thread(
            target=self._translation_worker, args=(bare, direction), daemon=True,
            name=f"translate-{bare}",
        ).start()
        return {"ok": True, "started": True}

    def get_translation_status(self, task_id: str) -> dict:
        with self._lock:
            bare = task_id[4:] if isinstance(task_id, str) and task_id.startswith("job:") else task_id
            state = dict(self._translation)
            manifest = self.controller.queue.get_manifest(bare) if bare else None
        if state.get("task_id") and bare and state["task_id"] != bare:
            state = {"running": False, "task_id": bare, "status": "", "error": "", "lang": ""}
        if manifest and manifest.get("translation_status") == "done":
            state.update({
                "status": "done",
                "lang": str(manifest.get("translation_lang") or ""),
                "direction": str(manifest.get("translation_direction") or ""),
                "has_translation": True,
            })
        return state

    def _translation_worker(self, job_id: str, direction: str) -> None:
        from app.translation_service import settle_agent_stages, translate_article

        settlements: list = []
        settings = dict(self.controller.settings)
        settings["_agent_operation_id"] = f"translate:{job_id}:{direction}"
        settings["_agent_stage_settlements"] = settlements
        if settings.get("agent_billing_mode") == "hosted":
            from engine.device import device_id
            settings["_agent_device_id"] = device_id(str(self.settings_store.base_dir))
        try:
            with self._lock:
                manifest = self.controller.queue.get_manifest(job_id)
            if not manifest:
                raise RuntimeError("任务不存在")
            updated = translate_article(manifest, settings, direction)
            with self._lock:
                self.controller.queue.replace_manifest(updated)
            settle_agent_stages(settlements, success=True)
            with self._lock:
                self._translation = {"running": False, "task_id": job_id, "status": "done",
                                     "error": "", "lang": str(updated.get("translation_lang") or "")}
        except Exception as exc:  # 失败不落 manifest，保留上一次的可用译文
            if settlements:
                settle_agent_stages(settlements, success=False)
            message = str(getattr(exc, "message", "") or exc)[:300]
            with self._lock:
                self._translation = {"running": False, "task_id": job_id, "status": "failed",
                                     "error": message, "lang": ""}

    # ------------------------------------------------------- 批量 Agent 整理
    def batch_agent_translate(self, task_ids: list[str], editing_level: str = "", profile: str = "") -> dict:
        """对选中的已完成任务批量生成文章；可指定整理力度/画像重跑（先生
        2026-11-02：文本库中可重新选择模式再转写）。空串表示沿用当前设置。"""
        if editing_level and editing_level not in ("auto", "light", "balanced", "structured", "rewrite"):
            return {"ok": False, "error": "整理力度无效"}
        valid_profiles = {"", "auto", "emotional_copy", "consulting", "knowledge"}
        if profile and (profile not in valid_profiles and not profile.startswith("custom:")):
            return {"ok": False, "error": "内容策略无效"}
        with self._lock:
            if error := self._processing_entry_error():
                return error
            if self._batch_agent.get("running"):
                return {"ok": False, "error": "已有批量整理进行中，请稍候"}
            jobs = _prefixed_ids(task_ids, "job:")
            settings = dict(self.controller.settings)
            if editing_level:
                settings["agent_editing_level"] = editing_level
            if profile:
                settings["agent_profile"] = profile
            if not settings.get("agent_enabled"):
                return {"ok": False, "error": "请先在设置 - 整理与转译中启用 Agent 转译"}
            if settings.get("agent_billing_mode") == "hosted":
                if not self.get_subscription_status().get("hosted_agent_ready"):
                    return {"ok": False, "error": "请先检查托管模型服务与账户积分"}
            elif not str(settings.get("agent_api_key") or "").strip():
                return {"ok": False, "error": "请先在设置 - 整理与转译中配置 API Key"}
            if not jobs:
                return {"ok": False, "error": "没有可整理的已完成任务"}
            self._batch_agent = {
                "running": True,
                "done": 0,
                "skipped": 0,
                "failed": 0,
                "errors": [],
                "message": f"批量整理已开始，共 {len(jobs)} 项",
            }
        threading.Thread(
            target=self._batch_agent_worker, args=(jobs, settings), daemon=True
        ).start()
        return {"ok": True, "started": True, "count": len(jobs)}

    def get_batch_agent_status(self) -> dict:
        with self._lock:
            return dict(self._batch_agent)

    def _batch_agent_worker(self, job_ids: list[str], settings: dict | None = None) -> None:
        from app.agent_service import batch_apply_agent_translation

        settings = settings if settings is not None else dict(self.controller.settings)
        if settings.get("agent_billing_mode") == "hosted":
            from engine.device import device_id
            settings["_agent_device_id"] = device_id(str(self.settings_store.base_dir))
        try:
            result = batch_apply_agent_translation(
                job_ids,
                self.controller.queue,
                settings,
                delete_source=True,
            )
        except Exception as exc:  # 整批失败兜底，保证状态可读
            result = {"done": 0, "skipped": 0, "failed": len(job_ids), "errors": [str(exc)]}
        with self._lock:
            self._batch_agent.update(
                running=False,
                done=int(result.get("done") or 0),
                skipped=int(result.get("skipped") or 0),
                failed=int(result.get("failed") or 0),
                errors=list(result.get("errors") or [])[:10],
                message=(
                    f"批量整理完成：成功 {result.get('done', 0)}，"
                    f"跳过 {result.get('skipped', 0)}，失败 {result.get('failed', 0)}"
                ),
            )

    def delete_tasks(self, task_ids: list[str]) -> dict:
        """彻底删除：记录 + 文本产物 + 视频文件一并移除。"""
        with self._lock:
            trans = _prefixed_ids(task_ids, "job:")
            downs = _prefixed_ids(task_ids, "dl:")
            removed: list[str] = []
            if trans:
                removed += self.controller.delete_tasks(trans)
            if downs:
                removed += self.controller.delete_download_tasks(downs)
            return {"ok": True, "removed": removed}

    def delete_texts(self, task_ids: list[str]) -> dict:
        """只清理文本产物，保留任务、关联登记与视频文件。"""
        with self._lock:
            if self._batch_agent.get("running"):
                return {"ok": False, "error": "正在整理文本，请完成后再删除"}
            trans = _prefixed_ids(task_ids, "job:")
            removed = self.controller.delete_texts(trans) if trans else []
            if trans and len(removed) != len(set(trans)):
                return {"ok": False, "removed": removed,
                        "error": "部分文本未能删除，请确认任务已停止且文件可访问"}
            return {"ok": True, "removed": removed}

    def delete_files(self, task_ids: list[str]) -> dict:
        """仅删除文件：文本产物与视频移除，保留数据库记录。"""
        with self._lock:
            trans = _prefixed_ids(task_ids, "job:")
            downs = _prefixed_ids(task_ids, "dl:")
            purged = self.controller.delete_files(trans) if trans else []
            purged += self.controller.delete_download_files(downs) if downs else []
            return {"ok": True, "removed": purged}

    def clear_completed(self) -> dict:
        """清除完成：将已完成的转写任务从工作台归档，文本库结果保留。"""
        with self._lock:
            cleared = self.controller.clear_completed()
            return {"ok": True, "removed": cleared}

    def set_category(self, task_ids: list[str], category: str) -> dict:
        with self._lock:
            jobs = _prefixed_ids(task_ids, "job:")
            updated = self.controller.set_result_categories(jobs, category)
            return {"ok": True, "updated": updated}

    def transcribe_download(self, download_id: str) -> dict:
        """Promote a completed download-only task into the ASR queue."""
        with self._lock:
            if error := self._processing_entry_error():
                return error
            # The WebView bridge historically passes ``dl:<12-hex-id>``,
            # while the Agent operation contract accepts the raw query ID.
            # Normalize both at this boundary so the Agent path does not
            # silently filter a valid ID as if it were missing its prefix.
            value = download_id.strip() if isinstance(download_id, str) else ""
            if value.startswith("dl:"):
                raw = [value[3:]]
            elif re.fullmatch(r"[a-f0-9]{12}", value):
                raw = [value]
            else:
                raw = []
            if not raw:
                return {"ok": False, "error": "无效的下载任务"}
            result = self.controller.queue_download_for_transcription(raw[0])
            # 该按钮只出现在已完成行上，skipped 在此处即「已在转写队列」
            # （先生 2026-10-18：重复点击要有明确反馈，不能落成裸「无法开始转写」）。
            already = not result.created and result.skipped
            return {
                "ok": bool(result.created),
                "created": [f"job:{j}" for j in result.created],
                "skipped": len(result.skipped),
                "already": bool(already),
                **({"error": "该任务已在转写队列中，无需重复提交"} if already else {}),
            }

    # ------------------------------------------------------------- import
    def import_files(self, paths: list[str]) -> dict:
        from engine.media_store import MediaStoreError
        if error := self._processing_entry_error():
            return error
        if not isinstance(paths, list) or not paths or len(paths) > 2000:
            return {"ok": False, "error": "请选择媒体文件，每次最多导入 2000 项"}
        with self._lock:
            imported = 0
            skipped = 0
            errors = []
            enqueued: list[str] = []
            seen = set()
            media_ids = set()
            for path in paths:
                if not isinstance(path, str) or not path or "\x00" in path:
                    errors.append("无效文件路径")
                    continue
                key = os.path.normcase(os.path.abspath(path))
                if key in seen:
                    skipped += 1
                    continue
                seen.add(key)
                try:
                    for entry in self.controller.import_files([path]):
                        # AppController returns ImportResult, not a media dictionary.
                        item = entry.item if hasattr(entry, "item") else entry
                        if item["id"] in media_ids:
                            skipped += 1
                            continue
                        media_ids.add(item["id"])
                        imported += 1
                        result = self.controller.enqueue_media([item["id"]])
                        enqueued.extend(result.created)
                        skipped += len(result.skipped)
                except (OSError, ValueError, MediaStoreError):
                    errors.append(f"{Path(path).name[:160]}：文件不可用或格式不支持")
            return {"ok": bool(imported), "imported": imported, "enqueued": enqueued,
                    "skipped": skipped, "failed": len(errors), "errors": errors[:20],
                    "error": "未导入媒体，请检查文件或选择支持的视频、音频" if not imported else ""}

    def _bind_media_drop(self, window: Any) -> None:
        """pywebview's native drop event supplies local paths via WebView2 File objects."""
        from webview.dom import DOMEventHandler
        element = window.dom.body
        element.on("drop", DOMEventHandler(self._on_media_drop, prevent_default=True))
        self._media_drop_element = element

    def _on_media_drop(self, event: dict) -> None:
        window = self._window()
        if window is None:
            return
        try:
            # The frontend owns presentation and a single import transaction per page.
            if window.evaluate_js("window.Tools?.beginNativeDrop() === true") is not True:
                return
            files = event.get("dataTransfer", {}).get("files", [])
            paths = [file["pywebviewFullPath"] for file in files
                     if isinstance(file, dict) and isinstance(file.get("pywebviewFullPath"), str)]
            result = self.import_files(paths) if paths else {
                "ok": False, "error": "未能读取本地文件，请使用「选择文件」重新导入"}
            window.evaluate_js("window.Tools?.finishNativeDrop(" + json.dumps(result, ensure_ascii=True) + ")")
        except Exception:
            self._log.append("拖入媒体未能完成，请使用选择文件重试")
            try:
                window.evaluate_js('window.Tools?.finishNativeDrop({ok:false,error:"导入未完成，请使用选择文件重试"})')
            except Exception:
                pass

    def pick_and_import(self) -> dict:
        if error := self._processing_entry_error():
            return error
        import webview

        window = self._window()
        if window is None:
            return {"ok": False, "error": "窗口不可用"}
        result = window.create_file_dialog(
            webview.FileDialog.OPEN,
            allow_multiple=True,
            file_types=("媒体文件 (*.mp4;*.mkv;*.mov;*.avi;*.wmv;*.flv;*.m4v;*.mpeg;*.mpg;*.ts;*.mts;*.m2ts;*.3gp;*.mp3;*.wav;*.m4a;*.aac;*.flac;*.ogg;*.opus;*.webm)",),
        )
        if not result:
            return {"ok": False, "cancelled": True}
        return self.import_files(list(result))

    def pick_folder_and_import(self) -> dict:
        if error := self._processing_entry_error():
            return error
        # 对话框常量在 webview 模块上，Window 实例没有这些属性。
        import webview

        window = self._window()
        if window is None:
            return {"ok": False, "error": "窗口不可用"}
        result = window.create_file_dialog(webview.FileDialog.FOLDER)
        if not result:
            return {"ok": False, "cancelled": True}
        from engine.media_store import MEDIA_EXTS
        try:
            paths = [str(path) for path in Path(result[0]).rglob("*")
                     if path.is_file() and path.suffix.lower() in MEDIA_EXTS]
        except OSError:
            return {"ok": False, "error": "无法读取文件夹，请检查访问权限"}
        return self.import_files(paths)

    # ------------------------------------------------------------ library
    def get_documents(self, query: str = "", filter_name: str = "全部") -> list[dict]:
        with self._lock:
            tasks = [t for t in self.controller.task_rows(include_archived=True) if t.get("kind") == "transcription"]
            tasks.sort(key=lambda t: (t.get("status") != "done", str(t.get("updated_at") or "")))
            q = (query or "").strip().lower()
            docs = []
            for task in tasks:
                title = str(task.get("display_title") or "")
                preview = self._document_preview(task)
                tags = list(task.get("tags") or [])
                if q and q not in (title + preview + " ".join(tags)).lower():
                    continue
                has_final = bool(task.get("final_path")) and Path(task["final_path"]).expanduser().is_file()
                has_raw = bool(task.get("raw_path")) and Path(task["raw_path"]).expanduser().is_file()
                if not (has_final or has_raw):
                    continue
                if filter_name == "已整理" and not has_final:
                    continue
                if filter_name == "仅转写" and has_final:
                    continue
                manifest = self.controller.queue.get_manifest(task["task_id"]) or {}
                docs.append({
                    "task_id": f"job:{task['task_id']}",
                    "title": title,
                    "preview": preview[:64],
                    "status": task.get("status", ""),
                    "updated_at": str(task.get("updated_at") or "").replace("T", " "),
                    "has_final": has_final,
                    "has_raw": has_raw,
                    "has_translation": manifest.get("translation_status") == "done",
                    "translation_lang": str(manifest.get("translation_lang") or ""),
                    "source_path": task.get("source_path") or "",
                    "tags": tags,
                })
            return docs

    def set_job_tags(self, task_id: str, tags: list[str]) -> dict:
        """Set tags for a completed text asset; accepts ``job:`` / ``dl:`` ids."""
        with self._lock:
            if isinstance(task_id, str) and task_id.startswith("job:"):
                bare_id = task_id[4:]
            elif isinstance(task_id, str) and task_id.startswith("dl:"):
                bare_id = task_id[3:]
            else:
                bare_id = task_id
            try:
                normalized = self.controller.set_job_tags(bare_id, tags)
            except Exception as exc:  # noqa: BLE001 - bridge 返回结构化错误
                return {"ok": False, "error": str(exc)}
            return {"ok": True, "tags": normalized}

    def get_tag_index(self) -> list[dict]:
        """Return all distinct tags with occurrence counts for the library filter."""
        with self._lock:
            counts: dict[str, int] = {}
            for task in self.controller.task_rows(include_archived=True):
                if task.get("kind") != "transcription":
                    continue
                if not any(task.get(key) and Path(task[key]).expanduser().is_file()
                           for key in ("final_path", "raw_path")):
                    continue
                for tag in task.get("tags") or []:
                    counts[tag] = counts.get(tag, 0) + 1
            return [
                {"tag": tag, "count": count}
                for tag, count in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
            ]

    def _reader_translation(self, task_id: str, task: dict) -> dict:
        """TASK-8.24：译文页——整篇译文与逐段对照数据一次带回。"""
        manifest = self.controller.queue.get_manifest(task.get("task_id") or "")
        if not manifest or manifest.get("translation_status") != "done":
            return {"ok": False, "error": "该文本尚未完成翻译", "task_id": task_id}
        article = Path(str(manifest.get("translation_path") or "")).expanduser()
        segments_file = Path(str(manifest.get("translation_segments_path") or "")).expanduser()
        out_root = Path(self.controller.settings.get("output_dir", "")).expanduser().resolve()
        for candidate in (article, segments_file):
            if not candidate.is_file() or not out_root.is_dir() or not self._path_within(candidate, out_root):
                return {"ok": False, "error": "译文文件不可用", "task_id": task_id}
        try:
            text = article.read_text(encoding="utf-8", errors="replace")
            segments = json.loads(segments_file.read_text(encoding="utf-8"))
            if not isinstance(segments, list):
                segments = []
        except (OSError, UnicodeError, json.JSONDecodeError):
            return {"ok": False, "error": "译文读取失败", "task_id": task_id}
        pairs = [
            {"src": str(item.get("src") or ""), "dst": str(item.get("dst") or "")}
            for item in segments if isinstance(item, dict)
        ]
        return {
            "ok": True,
            "task_id": task_id,
            "title": str(task.get("display_title") or article.stem),
            "tab": "translation",
            "text": text,
            "segments": pairs,
            "lang": str(manifest.get("translation_lang") or ""),
            "direction": str(manifest.get("translation_direction") or ""),
            "path": str(article),
            "size": article.stat().st_size,
            "truncated": False,
            "revision": None,
        }

    def get_reader(self, task_id: str, tab: str = "final") -> dict:
        with self._lock, self.controller.media_action_lock:
            if isinstance(task_id, str) and task_id.startswith("job:"):
                bare_id = task_id[4:]
            elif isinstance(task_id, str) and task_id.startswith("dl:"):
                bare_id = task_id[3:]
            else:
                bare_id = task_id
            task = next(
                (t for t in self.controller.task_rows(include_archived=True) if t.get("task_id") == bare_id),
                None,
            )
            if task is None:
                return {"ok": False, "error": "任务不存在"}
            if tab == "translation":
                return self._reader_translation(task_id, task)
            key = {"final": "final_path", "raw": "raw_path", "source": "source_path"}.get(tab, "final_path")
            path_value = task.get(key)
            path = Path(path_value).expanduser() if path_value else None
            if path is None or not path.is_file():
                return {"ok": False, "error": "文件不可用", "task_id": task_id}
            # R10：转写产物（final/raw）必须位于 output_dir 内；源媒体是用户自有文件，
            # 可能在任何位置，不在此限制内（source 页只返回路径元数据，不读正文）。
            if tab != "source":
                out_root = Path(self.controller.settings.get("output_dir", "")).expanduser().resolve()
                if not out_root.is_dir() or not self._path_within(path, out_root):
                    return {"ok": False, "error": "文件不可用", "task_id": task_id}
            if tab == "source":
                return {
                    "ok": True,
                    "task_id": task_id,
                    "title": str(task.get("display_title") or path.stem),
                    "text": "",
                    "path": str(path),
                    "size": path.stat().st_size if path.is_file() else 0,
                    "tab": tab,
                }
            try:
                raw_bytes = path.read_bytes()
                text = raw_bytes.decode("utf-8", errors="replace")
            except (OSError, UnicodeError):
                return {"ok": False, "error": "读取失败", "task_id": task_id}
            encoded = text.encode("utf-8")
            truncated = len(encoded) > 2_000_000
            visible_text = encoded[:2_000_000].decode("utf-8", errors="ignore") if truncated else text
            return {
                "ok": True,
                "task_id": task_id,
                "title": str(task.get("display_title") or path.stem),
                "text": visible_text,
                "path": str(path),
                "size": path.stat().st_size,
                "tab": tab,
                "truncated": truncated,
                "revision": document_revision(str(bare_id), tab, raw_bytes) if not truncated else None,
            }

    def update_reader_text(self, task_id: str, tab: str, text: str, expected_revision: str | None = None) -> dict:
        """Save an edited Markdown asset after resolving the public task id."""
        with self._lock:
            bare_id = task_id[4:] if isinstance(task_id, str) and task_id.startswith("job:") else task_id
            try:
                result = self.controller.update_document_text(
                    str(bare_id), str(tab), text, expected_revision=expected_revision,
                )
            except Exception as exc:  # noqa: BLE001 - bridge returns user-safe errors
                if hasattr(exc, "current_revision"):
                    return {"ok": False, "code": "revision_conflict", "error": "文本已被更新，草稿已保留；请重新读取后合并修改"}
                return {"ok": False, "error": str(exc)}
            return {"ok": True, **result}

    def export_subtitles(self, task_id: str, fmt: str, dest_path: str) -> dict:
        """把任务的转写时间轴导出为 SRT/VTT 字幕文件。

        数据源是任务目录里的 ``*_segments.json``（资产化第一刀保留的时间轴）。
        老任务在字幕功能之前转写、没有该文件时返回 ``no_segments``，
        引导用户重新转写后再导出。
        """
        from engine.subtitles import build_subtitles, load_segments_file

        fmt = (fmt or "").lower().lstrip(".")
        if fmt not in ("srt", "vtt"):
            return {"ok": False, "code": "bad_format", "error": "只支持 SRT 或 VTT"}
        with self._lock:
            if isinstance(task_id, str) and task_id.startswith("job:"):
                bare_id = task_id[4:]
            elif isinstance(task_id, str) and task_id.startswith("dl:"):
                bare_id = task_id[3:]
            else:
                bare_id = task_id
            task = next(
                (
                    t
                    for t in self.controller.task_rows(include_archived=True)
                    if t.get("task_id") == bare_id
                ),
                None,
            )
            if task is None:
                return {"ok": False, "code": "no_task", "error": "任务不存在"}
            raw_value = task.get("raw_path") or ""
            if not raw_value:
                return {
                    "ok": False,
                    "code": "no_segments",
                    "error": "该任务没有转写文本，无法导出字幕",
                }
            raw_path = Path(raw_value).expanduser()
            segments_file = raw_path.with_name(
                raw_path.name[:-len("_raw.md")] + "_segments.json"
            )
            if not segments_file.is_file():
                return {
                    "ok": False,
                    "code": "no_segments",
                    "error": "该文本转写于字幕导出功能之前，没有时间轴；请重新转写后再导出",
                }
            try:
                segments = load_segments_file(segments_file)
                content, ext = build_subtitles(segments, fmt)
                if not content.strip():
                    return {
                        "ok": False,
                        "code": "empty",
                        "error": "没有可导出的字幕内容",
                    }
                dest = Path(dest_path).expanduser()
                if dest.suffix.lower() not in (".srt", ".vtt"):
                    dest = dest.with_suffix("." + ext)
                dest.parent.mkdir(parents=True, exist_ok=True)
                # BOM 提升 Windows 播放器/剪辑软件对中文 SRT 的兼容性。
                dest.write_text(content, encoding="utf-8-sig")
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                return {
                    "ok": False,
                    "code": "export_failed",
                    "error": f"导出失败：{exc}",
                }
            return {
                "ok": True,
                "path": str(dest),
                "format": fmt,
                "count": len(segments),
            }

    @staticmethod
    def _path_within(path: Path, root: Path) -> bool:
        try:
            path.expanduser().resolve().relative_to(root.resolve())
            return True
        except ValueError:
            return False

    def _document_preview(self, task: dict) -> str:
        out_root = Path(self.controller.settings.get("output_dir", "")).expanduser().resolve()
        for key in ("final_path", "raw_path"):
            path_value = task.get(key)
            if not path_value:
                continue
            path = Path(path_value).expanduser()
            if not path.is_file():
                continue
            # R10：预览同样只读 output_dir 内的转写产物
            if not out_root.is_dir() or not self._path_within(path, out_root):
                continue
            try:
                with path.open(encoding="utf-8", errors="replace") as handle:
                    text = handle.read(4096)
            except OSError:
                continue
            text = text.strip()
            if text:
                from app.ui.markdown_render import strip_markdown
                return strip_markdown(text)[:64]
        return ""

    # ------------------------------------------------------------ settings
    def _agent_editable_settings(self) -> set[str]:
        from engine.settings import SECRET_FIELDS
        protected = {"schema_version", "database_path", "extension_origin", "js_runtime_path", "credit_balance", "license_expires_at",
                     "subscription_plan", "subscription_active", "subscription_expires_at", "subscription_last_order_id",
                     "terms_accepted_version", "terms_cross_border_accepted_version", "terms_accepted_service",
                     "plan_expiry_last_notice_day", "subscription_expiry_notice_history"}
        return {key for key in self.settings_store.defaults()
                if key not in protected and key not in SECRET_FIELDS
                and not key.startswith(("account_", "agent_usage_"))} | {"agent_api_key"}

    def _agent_settings_snapshot(self) -> dict:
        settings = self.controller.settings
        allowed = self._agent_editable_settings()
        public = {key: settings.get(key) for key in sorted(allowed - {"agent_api_key"})}
        # An opaque keyed revision detects all concurrent changes, including a
        # key replacement, without exposing a guessable digest of that secret.
        return {"ok": True, "settings": public, "revision": self._settings_revision(),
                "secret_fields": {"agent_api_key": bool(settings.get("agent_api_key"))}}

    def get_agent_settings(self) -> dict:
        with self._lock, self.controller._settings_lock:
            return self._agent_settings_snapshot()

    def get_agent_activity(self) -> dict:
        enabled = bool(self.controller.settings.get("agent_enabled"))
        return {**self.controller.agent_activity.summary(enabled=enabled), 'processing_enabled': enabled, 'control_available': True}

    def preview_media_relink(self, media_id: str, candidate_path: str) -> dict:
        from engine.media_relink import MediaRelinker, MediaRelinkError
        try:
            with self.controller.media_action_lock:
                return MediaRelinker(self.controller.settings['database_path']).preview(media_id, candidate_path)
        except (MediaRelinkError, OSError) as exc:
            return {'ok': False, 'code': getattr(exc, 'code', 'unavailable'), 'error': str(exc)}

    def apply_media_relink(self, media_id: str, candidate_path: str, expected_revision: str) -> dict:
        from engine.media_relink import MediaRelinker, MediaRelinkError
        try:
            with self.controller.media_action_lock:
                return MediaRelinker(self.controller.settings['database_path']).apply(media_id, candidate_path, expected_revision=expected_revision)
        except (MediaRelinkError, OSError) as exc:
            return {'ok': False, 'code': getattr(exc, 'code', 'unavailable'), 'error': str(exc)}

    def save_agent_settings(self, changes: dict, expected_revision: str) -> dict:
        from engine.settings import SettingsError
        if (type(changes) is not dict or not changes or len(changes) > 100 or
                type(expected_revision) is not str or not re.fullmatch(r"[a-f0-9]{64}", expected_revision)):
            return {"ok": False, "code": "invalid_request", "error": "请提供设置变更及当前 revision"}
        with self._lock, self.controller._settings_lock:
            if any(type(key) is not str or key not in self._agent_editable_settings() for key in changes):
                return {"ok": False, "code": "protected_setting", "error": "该设置由账户、许可证或连接服务管理"}
            if not hmac.compare_digest(expected_revision, self._agent_settings_snapshot()["revision"]):
                return {"ok": False, "code": "revision_conflict", "error": "设置已修改，请重新读取后保存"}
            changes = dict(changes)
            if "agent_api_key" not in changes and any(key in changes and changes[key] != self.controller.settings.get(key)
                                                      for key in ("agent_provider", "agent_base_url")):
                # Repointing a write-only provider must never forward its old
                # secret to a new endpoint. Explicit replacement stays possible.
                changes["agent_api_key"] = ""
            try:
                result = self.save_settings(changes)
            except (SettingsError, TypeError, ValueError):
                return {"ok": False, "code": "invalid_setting", "error": "设置值无效，请按设置文档修改"}
            if result.get("ok") is False:
                return {key: result[key] for key in ("ok", "code", "error") if key in result}
            return self._agent_settings_snapshot()

    def save_settings(self, changes: dict) -> dict:
        with self._lock:
            if changes and "whisper_model" in changes:
                from engine.model_manager import MODELS
                name = changes["whisper_model"]
                if not isinstance(name, str) or name not in {spec.name for spec in MODELS}:
                    return {"ok": False, "code": "invalid_model", "error": "请选择支持的转写模型"}
                if self._model_download.get("name") == name:
                    return {"ok": False, "code": "model_not_ready", "error": "模型正在下载，完成后再选择"}
                try:
                    # A directory and model supplied together are one atomic
                    # patch; verify in the destination rather than the old root.
                    manager = self._model_manager({**self.controller.settings, **changes})
                    state = manager.state(name)
                except Exception:
                    return {"ok": False, "code": "model_not_ready", "error": "无法确认模型状态，请重新检查"}
                if state.verified is not True:
                    reason = getattr(state, "verification_error", "") or "模型缓存不完整，请重新下载"
                    return {"ok": False, "code": "model_not_ready", "error": reason}
            clear_account = False
            if changes and "subscription_server_url" in changes:
                old_base = str(self.controller.settings.get("subscription_server_url") or "").strip().rstrip("/")
                new_base = str(changes.get("subscription_server_url") or "").strip().rstrip("/")
                clear_account = old_base != new_base
            self.controller.save_settings(changes or {})
            if clear_account:
                self._clear_account()
            elif changes and "subscription_license_public_key" in changes:
                self._account_revision += 1
            settings = self.controller.settings
        if changes and "theme" in changes:
            self._sync_native_theme(settings.get("theme", "dark"))
        with self._lock:
            view = self._frontend_settings_view()
            activity = getattr(self.controller, 'agent_activity', None)
            if changes and activity:
                try:
                    activity.record('settings.saved', True, 'settings', view['agent_settings_revision'])
                except OSError:
                    pass
            return view

    # ------------------------------------------------------------- 模型
    def _model_manager(self, settings=None):
        from engine.model_manager import ModelManager

        settings = self.controller.settings if settings is None else settings
        return ModelManager(
            settings.get("model_dir", "") or None,
            endpoint=settings.get("hf_endpoint", ""),
        )

    @staticmethod
    def _model_view(state, download: dict) -> dict:
        downloading = download.get("name") == state.spec.name
        ready = state.verified is True and not downloading
        reason = "下载中" if downloading else "" if ready else (
            getattr(state, "verification_error", "") or
            ("缓存不完整，请重新下载" if state.installed else "未下载")
        )
        return {
            "name": state.spec.name, "size_label": state.spec.size_label,
            "description": state.spec.description, "installed": state.installed,
            "verified": state.verified, "ready": ready, "reason": reason,
            "bytes_on_disk": state.bytes_on_disk,
        }

    def get_models(self) -> dict:
        """五档转写模型的本地缓存状态，供设置页展示与手动下载。"""
        manager = self._model_manager()
        try:
            states = manager.list_models()
        except Exception as exc:
            return {"ok": False, "error": f"读取模型状态失败: {exc}"}
        with self._lock:
            download = dict(self._model_download)
        raw_root = getattr(manager, "cache_root", "") or ""
        try:
            cache_root = Path(str(raw_root))
        except TypeError:
            cache_root = Path(".")
        probe = cache_root if cache_root.exists() else Path(cache_root.anchor)
        try:
            free_bytes = shutil.disk_usage(str(probe)).free
        except OSError:
            free_bytes = 0
        return {
            "ok": True,
            "cache_root": str(manager.cache_root),
            "free_bytes": free_bytes,
            "download": download,
            "models": [self._model_view(state, download) for state in states],
        }

    def download_model(self, name: str) -> dict:
        """手动下载模型到 model_dir/HF 缓存；不进安装包，由用户主动选择。"""
        if not isinstance(name, str) or not name.strip():
            return {"ok": False, "error": "模型名称无效"}
        name = name.strip()
        from engine.model_manager import MODELS
        if name not in {spec.name for spec in MODELS}:
            return {"ok": False, "error": "不支持的模型"}
        with self._lock:
            if self._model_download.get("name"):
                return {
                    "ok": False,
                    "error": f"已有模型下载中：{self._model_download['name']}",
                }
            self._model_download = {
                "name": name,
                "message": "准备模型缓存",
                "progress": 0.0,
                "error": "",
            }
        threading.Thread(
            target=self._download_model_worker, args=(name,), daemon=True
        ).start()
        return {"ok": True, "started": True, "name": name}

    def _download_model_worker(self, name: str) -> None:
        manager = self._model_manager()

        def on_progress(message: str, value: float | None) -> None:
            with self._lock:
                self._model_download["message"] = message
                if value is not None:
                    self._model_download["progress"] = value

        try:
            manager.download(name, progress=on_progress)
        except Exception as exc:
            with self._lock:
                self._model_download["error"] = str(exc)
        finally:
            with self._lock:
                # 前端轮询以 name 清空为停止信号，error 必须跨过重置保留下来，
                # 否则失败原因永远观察不到；开始下一次下载时才整体重置。
                error = self._model_download.get("error", "")
                self._model_download = {
                    "name": "",
                    "message": "",
                    "progress": None,
                    "error": error,
                }

    def delete_model(self, name: str) -> dict:
        """删除本地模型缓存（仅限模型缓存根内路径）。"""
        if not isinstance(name, str) or not name.strip():
            return {"ok": False, "error": "模型名称无效"}
        with self._lock:
            if self._model_download.get("name") == name.strip():
                return {"ok": False, "error": "模型正在下载，完成后再删除"}
            manager = self._model_manager()
            try:
                removed = manager.delete(name.strip())
            except Exception as exc:
                return {"ok": False, "error": str(exc)}
        result = self.get_models()
        result["removed"] = removed
        return result

    def delete_all_models(self) -> dict:
        """删除全部已安装的模型缓存（含不完整缓存），删除后需重新下载。"""
        with self._lock:
            if self._model_download.get("name"):
                return {"ok": False, "error": "模型正在下载，完成后再删除"}
            manager = self._model_manager()
            try:
                removed = manager.delete_all()
            except Exception as exc:
                return {"ok": False, "error": str(exc)}
        result = self.get_models()
        result["removed"] = removed
        return result

    # ------------------------------------------------------------- 准备页
    def get_setup_check(self) -> dict:
        """准备页数据源：环境自检行 + 模型档位判定 + 一键配置修复计划。"""
        from engine import setup_check

        settings = self.controller.settings
        try:
            manager = self._model_manager()
            states = manager.list_models()
        except Exception as exc:
            return {"ok": False, "error": f"读取模型状态失败: {exc}"}
        try:
            cuda_probe = self._probe_cuda()
        except Exception as exc:  # 探测失败不挡准备页，按无 CUDA 展示
            cuda_probe = {"available": False, "error": str(exc)}
        with self._lock:
            download = dict(self._model_download)
        installed = {state.spec.name: self._model_view(state, download)["ready"] for state in states}
        try:
            cache_root = Path(str(getattr(manager, "cache_root", "") or "."))
        except TypeError:
            cache_root = Path(".")
        try:
            probe = cache_root if cache_root.exists() else Path(cache_root.anchor)
            free_bytes = shutil.disk_usage(str(probe)).free
        except OSError:
            free_bytes = None
        vram_mb = setup_check.detect_vram_mb()
        verdicts = setup_check.model_verdicts(
            cuda_available=bool(cuda_probe.get("available")),
            vram_mb=vram_mb,
            disk_free_bytes=free_bytes,
            installed=installed,
        )
        gpu_pack = self._gpu_pack_state()
        return {
            "ok": True,
            "cuda": cuda_probe,
            "vram_mb": vram_mb,
            "free_bytes": free_bytes,
            "gpu_pack": gpu_pack,
            "cache_root": str(cache_root),
            "settings": {
                "download_dir": settings.get("download_dir", ""),
                "whisper_model": settings.get("whisper_model", "tiny"),
                "device": settings.get("device", "cuda"),
                "hf_endpoint": settings.get("hf_endpoint", ""),
                "model_dir": settings.get("model_dir", ""),
            },
            "rows": setup_check.check_rows(
                cuda_probe=cuda_probe,
                vram_mb=vram_mb,
                disk_free_bytes=free_bytes,
                cache_root=cache_root,
                settings=settings,
                installed=installed,
            ),
            "models": [self._model_view(state, download) for state in states],
            "tiers": [
                {
                    "name": v.name,
                    "tier": v.tier,
                    "tier_label": v.tier_label,
                    "verdict": v.verdict,
                    "detail": v.detail,
                    "recommended": v.recommended,
                }
                for v in verdicts
            ],
            "fixes": setup_check.plan_fixes(
                cache_root=cache_root, settings=settings,
                states=[state for state in states if state.spec.name != download.get("name")],
            ),
            "download": download,
        }

    def _probe_cuda(self) -> dict:
        # ASR dependencies live in the packaged Worker, not in Desktop.
        # Source runs share the Python runtime and keep the local probe.
        if not getattr(sys, "frozen", False):
            from engine.asr import probe_cuda

            return probe_cuda()
        failure = {"available": False, "device_count": 0, "compute_types": [], "dll_dirs": [], "error": ""}
        try:
            from app.process_manager import worker_command

            command = worker_command(Path(__file__).resolve().parents[1], ":memory:")
            completed = subprocess.run(
                [*command, "--probe-gpu"], capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=10, check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if completed.returncode != 0:
                raise ValueError("Worker GPU 检测未正常完成")
            report = json.loads(completed.stdout)
            if (not isinstance(report, dict) or set(report) != set(failure)
                    or not isinstance(report["available"], bool)
                    or not isinstance(report["device_count"], int) or isinstance(report["device_count"], bool)
                    or report["device_count"] < 0
                    or any(not isinstance(report[key], list) or any(not isinstance(item, str) for item in report[key])
                           for key in ("compute_types", "dll_dirs"))
                    or not isinstance(report["error"], str)
                    or report["available"] and (report["device_count"] == 0 or "float16" not in report["compute_types"])):
                raise ValueError("Worker GPU 检测结果格式无效")
            return report
        except subprocess.TimeoutExpired:
            failure["error"] = "Worker GPU 检测超时，请稍后重试"
        except (OSError, ValueError, TypeError):
            failure["error"] = "无法读取 Worker GPU 检测结果，请检查安装文件后重试"
        return failure

    # ------------------------------------------------- GPU 加速包（A2 拆包）
    def _worker_internal_dir(self) -> Path:
        """worker 的 _internal 目录：安装根/worker/_internal（与 get_ffmpeg
        的既有布局约定一致）；源码/开发布局退回 dist/FoilPage-Worker。"""
        root = Path(sys.executable).resolve().parent
        if root.name.lower() == "worker":
            return root / "_internal"
        for candidate in (root / "worker" / "_internal",
                          root / "FoilPage-Worker" / "_internal",
                          root / "VideoToArticle-Worker" / "_internal"):
            if candidate.is_dir():
                return candidate
        return root / "worker" / "_internal"

    def _gpu_pack_state(self) -> dict:
        """GPU 加速包状态：Worker 必须同时具备三个运行库组件。"""
        try:
            internal = self._worker_internal_dir()
            libs = _gpu_runtime_components(internal / "nvidia")
            return {"installed": set(libs) == set(_GPU_RUNTIME_DLLS), "components": libs, "target": str(internal / "nvidia")}
        except OSError:
            return {"installed": False, "components": [], "target": ""}

    def pick_gpu_pack(self) -> dict:
        """选择 GPU 加速包 zip（与安装器配套发布，先生 2026-10-08 拆包）。"""
        import webview

        window = self._window()
        if window is None:
            return {"ok": False, "error": "窗口不可用"}
        result = window.create_file_dialog(
            webview.FileDialog.OPEN, file_types=("GPU 加速包 (*.zip)",),
        )
        if not result:
            return {"ok": False, "cancelled": True}
        return {"ok": True, "path": str(result[0])}

    def install_gpu_pack(self, zip_path: str) -> dict:
        """把 GPU 加速包解压到 worker _internal/nvidia（engine/dll.py 自动发现）。

        安装器只含核心刚需程序（CPU 可完整转写）；GPU 用户在此自选安装。
        校验三个组件 DLL 与 nvidia 子树；换包失败恢复旧库，成功保留旧库供回滚。
        安装后需重启 FoilPage（worker 重新加载 DLL）。
        """
        import tempfile
        import zipfile

        source = Path(str(zip_path or "")).expanduser()
        if not source.is_file() or source.suffix.lower() != ".zip":
            return {"ok": False, "error": "请选择 GPU 加速包 zip 文件"}
        with self._lock:
            staging = None
            previous = None
            try:
                internal = self._worker_internal_dir()
                target = internal / "nvidia"
                if os.path.lexists(target):
                    attributes = getattr(target.lstat(), "st_file_attributes", 0)
                    if target.is_symlink() or attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0):
                        return {"ok": False, "error": "运行库目录是链接，无法安全替换，请检查实际安装目录"}
                with zipfile.ZipFile(source) as bundle:
                    names = bundle.namelist()
                    for name in names:
                        parts = Path(name).parts
                        if (not name or not name.startswith("nvidia/")
                                or ".." in parts or chr(92) in name or Path(name).is_absolute()):
                            return {"ok": False, "error": "包内路径不合法，拒绝安装"}
                    internal.mkdir(parents=True, exist_ok=True)
                    staging = Path(tempfile.mkdtemp(prefix=".gpu-pack-staging-", dir=internal))
                    bundle.extractall(staging)
                staged_runtime = staging / "nvidia"
                if set(_gpu_runtime_components(staged_runtime)) != set(_GPU_RUNTIME_DLLS):
                    return {"ok": False, "error": "GPU 加速包缺少完整的 cuBLAS、cuDNN 或 NVRTC 运行库，请使用与安装器配套的包"}
                if target.exists():
                    previous = internal / (".gpu-pack-previous-" + secrets.token_hex(8))
                    target.rename(previous)
                try:
                    staged_runtime.rename(target)
                except OSError:
                    if previous is not None:
                        try:
                            previous.rename(target)
                        except OSError:
                            return {"ok": False, "error": "安装失败，旧运行库已保留，请恢复旧目录后重试",
                                    "previous_runtime": str(previous)}
                    raise
                state = self._gpu_pack_state()
                return {
                    "ok": True,
                    "installed": state["installed"],
                    "components": state["components"],
                    "needs_restart": True,
                    "previous_runtime": str(previous) if previous is not None else "",
                    "message": "GPU 加速库已安装，重启 FoilPage 后生效",
                }
            except (OSError, zipfile.BadZipFile) as exc:
                return {"ok": False, "error": f"安装失败：{exc}"}
            finally:
                if staging is not None:
                    shutil.rmtree(staging, ignore_errors=True)

    def apply_setup_fixes(self) -> dict:
        """一键配置环境：创建缓存目录、补默认镜像、默认模型切到已安装档位。

        与 plan_fixes 一一对应、幂等；绝不自动下载模型（下载始终由用户点击）。
        """
        from engine import setup_check

        settings = self.controller.settings
        try:
            manager = self._model_manager()
            states = manager.list_models()
        except Exception as exc:
            return {"ok": False, "error": f"读取模型状态失败: {exc}"}
        cache_root = Path(str(getattr(manager, "cache_root", "") or "."))
        applied: list[str] = []
        for fix in setup_check.plan_fixes(
            cache_root=cache_root, settings=settings, states=states
        ):
            action = fix.get("action")
            if action == "mkdir":
                try:
                    cache_root.mkdir(parents=True, exist_ok=True)
                    applied.append(fix["label"])
                except OSError:
                    continue
            elif action == "set_endpoint":
                with self._lock:
                    self.controller.save_settings(
                        {"hf_endpoint": setup_check.DEFAULT_HF_ENDPOINT}
                    )
                applied.append(fix["label"])
            elif action == "switch_default":
                result = self.save_settings({"whisper_model": str(fix.get("value") or "small")})
                if result.get("ok") is not False:
                    applied.append(fix["label"])
        result = self.get_setup_check()
        result["applied"] = applied
        return result

    def cache_status(self) -> dict:
        """缓存占用统计：可清理临时/日志大小与模型缓存大小。"""
        with self._lock:
            return self.controller.cache_status()

    def cleanup_cache(self) -> dict:
        """一键清理可再生的临时缓存与日志备份；不触碰用户数据。"""
        with self._lock:
            return self.controller.cleanup_cache()

    def set_native_theme(self, theme: str) -> dict:
        """前端主题切换后通知原生标题栏随主题变色。"""
        self._sync_native_theme(theme)
        return {"ok": True}

    def _sync_native_theme(self, theme: str) -> None:
        applier = self._native_theme_applier
        if not applier:
            return
        try:
            applier(theme)
        except Exception:
            pass

    # -------------------------------------------------------------- worker
    def _worker_state(self) -> dict:
        try:
            running = bool(self.worker.running)
        except Exception:
            running = False
        pid = None
        if running:
            try:
                pid = self.worker.pid
            except Exception:
                pid = None
        entry_error = self._processing_entry_error() if running else None
        waiting = entry_error is not None
        return {
            "running": running,
            "pid": pid,
            "account_waiting": waiting,
            "label": "等待登录" if waiting else "Worker 运行中" if running else "Worker 已停止",
            "meta": "任务已保留，登录后继续" if waiting else str(self.controller.settings.get("whisper_model") or "small") + "（" + ("GPU" if self.controller.settings.get("device", "cuda") != "cpu" else "CPU") + "）",
        }

    def worker_toggle(self) -> dict:
        if self.worker.running:
            self.worker.stop()
        else:
            if error := self._processing_entry_error():
                return error
            self.worker.start()
        return self._worker_state()

    # ---------------------------------------------------------- browser
    def _local_api(self):
        api = self._local_api_provider()
        return api if api is not None else None

    def download_from_url(self, url: str, title: str = "", transcribe: bool = True) -> dict:
        """Submit an external video URL (bilibili / YouTube / douyin share link)
        to the local yt-dlp download rail. Requires the local API to be running,
        which also powers the browser extension.

        Unlike the extension's browser handoff (Chrome downloads), this path
        downloads inside the desktop worker, so it works for any supported
        host — bilibili included — without reading any browser cookies.
        """
        if error := self._processing_entry_error():
            return error
        api = self._local_api()
        if api is None or not api.running:
            return {"ok": False, "error": "本地 API 未运行；请确认本地服务已开启（浏览器配对依赖同一服务）"}
        from engine.downloader import DownloadAdapterError, detect_platform, is_direct_media_url
        source_url = _extract_pasted_url(url)
        try:
            if detect_platform(source_url) == "douyin" and not is_direct_media_url(source_url):
                # The desktop WebView cannot read browser cookies. Ask the
                # paired extension over its authenticated local socket so it
                # reads a Douyin-only cookie set only for this click.
                item = api.submit_douyin_link_from_desktop(
                    source_url,
                    title=str(title or "").strip()[:240],
                    transcribe=bool(transcribe),
                )
            else:
                item = api.coordinator.submit(
                    source_url,
                    title=str(title or "").strip()[:240],
                    options={"transcribe_after_download": bool(transcribe)},
                    mode="local",
                )
            return {"ok": True, "job": item}
        except (DownloadAdapterError, ValueError) as exc:
            result = {
                "ok": False,
                "code": getattr(exc, "code", "invalid_request"),
                "error": str(exc),
            }
            download_id = str(getattr(exc, "download_id", "") or "")
            if re.fullmatch(r"[a-f0-9]{12}", download_id):
                result["task_id"] = f"dl:{download_id}"
            return result

    def browser_status(self) -> dict:
        """Browser extension pairing status (local API running / extension pinned)."""
        api = self._local_api()
        running = bool(api is not None and api.running)
        return {
            "ok": True,
            "running": running,
            "configured": bool(api is not None and getattr(getattr(api, "origin_policy", None), "origin", "")),
            "origin": (getattr(getattr(api, "origin_policy", None), "origin", "") if api is not None else "") or "",
            "port": (api.port if api is not None else self.controller.settings.get("local_api_port", 18765)),
            "extension_dir": str(_browser_extension_dir()),
        }

    def begin_browser_pairing(self) -> dict:
        """Open a pairing window for the browser extension and return guidance."""
        api = self._local_api()
        if api is None:
            return {"ok": False, "error": "本地 API 未启用，请在设置中开启"}
        if not api.running:
            return {"ok": False, "error": "本地 API 未运行，请在 设置 → 高级 中启用本地 API"}
        seconds = api.begin_pairing()
        return {
            "ok": True,
            "seconds": seconds,
            "port": api.port,
            "extension_dir": str(_browser_extension_dir()),
        }

    # --------------------------------------------------------------- files
    def open_path(self, path_value: str) -> dict:
        path = Path(path_value).expanduser() if path_value else None
        if not path or not path.is_file():
            return {"ok": False, "error": "文件不存在"}
        try:
            if os.name == "nt":
                subprocess.Popen(["explorer.exe", "/select,", str(path)])
            else:
                os.startfile(path.parent)
            return {"ok": True}
        except (OSError, ValueError) as exc:
            return {"ok": False, "error": str(exc)}

    def show_in_folder(self, path_value: str) -> dict:
        return self.open_path(path_value)

    def play_media(self, task: dict) -> dict:
        path_value = task.get("downloaded_path") if task.get("kind") == "download" else task.get("source_path")
        path = Path(path_value).expanduser() if path_value else None
        if not path or not path.is_file():
            return {"ok": False, "error": "媒体文件尚未生成或已移除"}
        try:
            codecs = probe_video_codecs(str(path))
            if "hevc" in codecs:
                player = find_external_media_player()
                if not player:
                    return {"ok": False, "error": "视频为 HEVC 编码，请安装 PotPlayer/VLC 后重试"}
                subprocess.Popen([player, str(path)])
                return {"ok": True, "player": "external"}
            os.startfile(path)
            return {"ok": True}
        except (OSError, ValueError) as exc:
            return {"ok": False, "error": f"视频无法打开：{exc}"}

    def open_directory(self, path_value: str) -> dict:
        path = Path(path_value).expanduser() if path_value else None
        if not path or not path.is_dir():
            return {"ok": False, "error": "目录不存在"}
        try:
            os.startfile(str(path))
            return {"ok": True}
        except (OSError, ValueError) as exc:
            return {"ok": False, "error": str(exc)}

    def open_output_dir(self) -> dict:
        path = Path(self.controller.settings.get("output_dir", "")).expanduser()
        if path.is_dir():
            os.startfile(str(path))
            return {"ok": True}
        return {"ok": False, "error": "输出目录不存在"}

    # ------------------------------------------------------- launch/app
    def set_launch_at_startup(self, enabled: bool) -> dict:
        """Toggle the Windows Run-key registration and persist the setting."""
        from app.startup import disable_startup, enable_startup

        if enabled:
            enable_startup()
        else:
            disable_startup()
        with self._lock:
            self.controller.save_settings({"launch_at_startup": bool(enabled)})
            return self.controller.settings

    _PICKABLE_DIR_KEYS = frozenset({
        "output_dir", "download_dir", "browser_download_dir", "model_dir",
    })

    def pick_video_scan_directory(self) -> dict:
        import webview

        window = self._window()
        if window is None:
            return {'ok': False, 'error': '窗口不可用'}
        result = window.create_file_dialog(webview.FileDialog.FOLDER)
        if not result:
            return {'ok': False, 'cancelled': True}
        value = str(result[0])
        if not Path(value).is_absolute() or value.startswith(('\\\\', '//')):
            return {'ok': False, 'error': '请选择本地文件夹'}
        return {'ok': True, 'path': value}

    def open_author_website(self) -> dict:
        """Open the fixed author URL without expanding the release URL allowlist."""
        import webbrowser
        return {'ok': bool(webbrowser.open('https://keeppoiesis.com/'))}

    def open_url(self, url: str) -> dict:
        from engine.product_events import release_url
        if not release_url(url):
            return {'ok': False, 'error': '更新链接无效'}
        import webbrowser
        return {'ok': bool(webbrowser.open(url))}

    def pick_setting_dir(self, key: str) -> dict:
        """Open a folder picker and store the result under a whitelisted key."""
        if key not in self._PICKABLE_DIR_KEYS:
            return {"ok": False, "error": "不支持的设置项"}
        import webview

        window = self._window()
        if window is None:
            return {"ok": False, "error": "窗口不可用"}
        result = window.create_file_dialog(webview.FileDialog.FOLDER)
        if not result:
            return {"ok": False, "cancelled": True}
        with self._lock:
            # The native picker may stay open while another operation starts.
            # Check again at the save boundary, including archived active jobs.
            if key == "model_dir":
                busy_error = "模型正在使用或下载，请完成后再更改缓存目录"
                if self._model_download.get("name"):
                    return {"ok": False, "error": busy_error}
                try:
                    tasks = self.controller.task_rows(include_archived=True)
                    in_use = any(
                        task.get("status") in {"transcribing", "processing"}
                        or (task.get("kind") == "transcription" and task.get("status") == "running")
                        for task in tasks
                    )
                except Exception:
                    return {"ok": False, "error": "无法确认模型使用状态，请稍后重试"}
                if in_use:
                    return {"ok": False, "error": busy_error}
            self.controller.save_settings({key: str(result[0])})
            return {"ok": True, "settings": self.controller.settings}

    # --------------------------------------------------------------- misc
    def system_notify(self, title: str, message: str) -> dict:
        """Show a notification-area bubble for a completed/failed task."""
        if self._notifier is None:
            return {"ok": False, "error": "通知不可用"}
        shown = self._notifier(str(title or ""), str(message or ""))
        return {"ok": bool(shown)}

    def pick_notification_sound(self, kind: str) -> dict:
        """选择自定义提示音 WAV（success/error），选择后立即保存。"""
        if kind not in ("success", "error"):
            return {"ok": False, "error": "kind 无效"}
        import webview

        window = self._window()
        if window is None:
            return {"ok": False, "error": "窗口不可用"}
        result = window.create_file_dialog(
            webview.FileDialog.OPEN, file_types=("WAVE 音频 (*.wav)",)
        )
        if not result:
            return {"ok": False, "cancelled": True}
        with self._lock:
            self.controller.save_settings(
                {f"notification_sound_{kind}_path": str(result[0])}
            )
            return {"ok": True, "settings": self.controller.settings}

    def preview_notification_sound(
        self, kind: str, pack: str = "", custom_path: str = ""
    ) -> dict:
        """试听提示音（不落盘）：按当前设置叠加显式 pack/自定义路径。"""
        if kind not in ("success", "error"):
            return {"ok": False, "error": "kind 无效"}
        from app.notification_sounds import play_notification_sound

        with self._lock:
            settings = dict(self.controller.settings)
        if pack:
            settings["notification_sound_pack"] = pack
        if custom_path:
            settings[f"notification_sound_{kind}_path"] = custom_path
        play_notification_sound(kind, settings)
        return {"ok": True}

    def quit_app(self) -> None:
        self._on_quit()

    def push_state(self, notifications: list[dict] | None = None) -> None:
        """Called by the UI timer to stream fresh state to the frontend."""
        window = self._window()
        if window is None:
            return
        try:
            with self._lock:
                rows = self.controller.task_rows()
                payload = {
                    "tasks": self._tasks(rows),
                    "summary": self._summary(rows),
                    "worker": self._worker_state(),
                    "notifications": notifications or [],
                    "product_notifications": self.get_product_notifications(),
                    "window_chrome": self.get_window_chrome(),
                    "launch_context": dict(self._launch_context()),
                    "account_access": self._frontend_settings_view()["account_access"],
                    "agent_settings_revision": self._settings_revision(),
                    "ts": int(__import__("time").time() * 1000),
                }
            js = "window.__onState(" + json.dumps(payload, ensure_ascii=False) + ");"
            window.evaluate_js(js)
        except Exception:
            # Window may be tearing down; ignore transient pushes.
            pass
