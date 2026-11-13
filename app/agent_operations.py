# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""Explicit user-side application operations; never reflect arbitrary methods."""
from __future__ import annotations

from dataclasses import dataclass
import math
import ntpath
from pathlib import Path
import re
from typing import Any, Callable, Mapping


class ApplicationOperationError(RuntimeError):
    def __init__(self, status: int, code: str, message: str):
        self.status, self.code = status, code
        super().__init__(message)


def _field(kind: str, *, required: bool = True, maximum: int | None = None, items: str | None = None):
    value = {"type": kind}
    if maximum is not None:
        value["maxLength" if kind == "string" else "maxItems"] = maximum
    if items:
        value["items"] = {"type": items}
    return value, required


STR = _field("string", maximum=4096)
IDS = _field("array", maximum=200, items="string")
PATHS = _field("array", maximum=200, items="string")
NAME = _field("string", maximum=240)
REVISION = _field("string", maximum=64)


@dataclass(frozen=True)
class Operation:
    method: str
    category: str
    description: str
    fields: Mapping[str, tuple[dict, bool]]
    read_only: bool = False
    destructive: bool = False
    external: bool = False
    processing: bool = False
    secret_inputs: tuple[str, ...] = ()

    @property
    def schema(self) -> dict:
        return {"type": "object", "properties": {key: value[0] for key, value in self.fields.items()},
                "required": [key for key, value in self.fields.items() if value[1]], "additionalProperties": False}


def _op(method, category, description, fields=None, **flags):
    return Operation(method, category, description, fields or {}, **flags)


# Each callable must be supplied by the running desktop's public Bridge facade.
# Adding a Bridge helper does not add an Agent operation automatically.
OPERATIONS: dict[str, Operation] = {
    "media.relink_preview": _op("preview_media_relink", "media", "校验丢失媒体的新路径及 SHA256，返回关联 revision，不移动文件", {
        "media_id": NAME, "candidate_path": STR}, read_only=True),
    "media.relink_apply": _op("apply_media_relink", "media", "按预览 revision 恢复媒体路径，保留所有文本和文件，返回回滚回执", {
        "media_id": NAME, "candidate_path": STR, "expected_revision": REVISION}, destructive=True),
    "account.notifications": _op("get_product_notifications", "account", "读取当前账户的到账与软件更新通知", read_only=True),
    "settings.activity": _op("get_agent_activity", "settings", "最近的本地控制记录，不包含正文和凭据", read_only=True),
    "media.scan_status": _op("get_video_scan_status", "media", "读取视频索引进度，不重新扫描", read_only=True),
    "tasks.retention_preview": _op("get_failed_task_retention", "tasks", "预览失败任务保留策略，不执行删除", read_only=True),
    "application.summary": _op("get_summary", "application", "任务数量与处理汇总", read_only=True),
    "application.worker": _op("get_worker", "application", "后台 Worker 状态", read_only=True),
    "application.window": _op("get_window_chrome", "application", "窗口状态", read_only=True),
    "application.update_check": _op("check_update", "application", "检查应用更新", read_only=True, external=True),
    "application.quit": _op("quit_app", "application", "正常退出应用及本实例 Worker", destructive=True),
    "window.minimize": _op("minimize_window", "application", "最小化应用窗口"),
    "window.maximize": _op("toggle_maximize_window", "application", "切换窗口最大化"),
    "window.hide": _op("hide_window", "application", "驻留托盘"),
    "settings.read": _op("get_agent_settings", "settings", "读取日常设置、模型和提示词及 revision；密钥只显示是否配置", read_only=True),
    "settings.update": _op("save_agent_settings", "settings", "按 revision 保存指定设置；密钥仅写入 DPAPI，不返回", {
        "changes": _field("object"), "expected_revision": REVISION}, secret_inputs=("changes.agent_api_key",)),
    "settings.launch_at_startup": _op("set_launch_at_startup", "settings", "设置开机自启", {"enabled": _field("boolean")}),
    "settings.native_theme": _op("set_native_theme", "settings", "同步原生主题", {"theme": NAME}),
    "environment.check": _op("get_setup_check", "environment", "检查 GPU、模型、目录与磁盘", read_only=True),
    "environment.apply_fixes": _op("apply_setup_fixes", "environment", "按现有检测方案配置环境，不自动下载模型"),
    "environment.install_gpu_pack": _op("install_gpu_pack", "environment", "从本地 ZIP 安装 GPU 加速包，替换现有包", {"zip_path": STR}, destructive=True),
    "models.list": _op("get_models", "models", "模型完整性、安装状态及下载进度", read_only=True),
    "models.download": _op("download_model", "models", "下载指定受支持模型，返回提交状态", {"name": NAME}, external=True),
    "models.delete": _op("delete_model", "models", "删除指定模型缓存", {"name": NAME}, destructive=True),
    "models.delete_all": _op("delete_all_models", "models", "删除全部模型缓存", destructive=True),
    "worker.toggle": _op("worker_toggle", "application", "按当前状态启动或停止 Worker；可能继续队列处理", processing=True),
    "browser.status": _op("browser_status", "browser", "浏览器与扩展连接状态", read_only=True),
    "browser.pair": _op("begin_browser_pairing", "browser", "开始现有扩展配对流程"),
    "browser.extensions": _op("open_chrome_extensions", "browser", "打开扩展管理页", external=True),
    "media.list": _op("get_downloaded_videos", "media", "已有视频及可执行操作", read_only=True),
    "media.preview": _op("get_video_preview", "media", "取得登记视频的本地预览地址", {"media_id": NAME}, read_only=True),
    "media.open_location": _op("open_video_location", "media", "打开登记视频所在位置", {"media_id": NAME}),
    "media.close_preview": _op("close_media_preview", "media", "关闭本实例媒体预览服务"),
    "media.delete": _op("delete_video", "media", "按应用规则删除下载视频或移除导入记录，保留转写文本", {"media_id": NAME}, destructive=True),
    "media.import": _op("import_files", "media", "导入本地媒体并按既有配置入队", {"paths": PATHS}, processing=True),
    "downloads.submit": _op("download_from_url", "tasks", "提交视频下载，可独立选择随后转写", {
        "url": STR, "title": _field("string", required=False, maximum=240), "transcribe": _field("boolean", required=False)}, external=True, processing=True),
    "downloads.retry": _op("retry_downloads", "tasks", "重试指定下载", {"task_ids": IDS}, external=True, processing=True),
    "downloads.transcribe": _op("transcribe_download", "tasks", "为已有下载提交转写", {"download_id": NAME}, processing=True),
    "tasks.cancel_batch": _op("cancel_tasks", "tasks", "取消指定任务并保留历史文件", {"task_ids": IDS}),
    "tasks.requeue_batch": _op("requeue_tasks", "tasks", "重新处理指定任务，沿用业务额度和模型配置", {"task_ids": IDS}, processing=True),
    "tasks.retranscribe_batch": _op("retranscribe_tasks", "tasks", "重新转写指定任务", {"task_ids": IDS}, processing=True),
    "tasks.translate_batch": _op("batch_agent_translate", "tasks", "批量整理已有正文，可能收费并按现有设置删除源视频", {"task_ids": IDS}, processing=True, external=True, destructive=True),
    "tasks.translation_status": _op("get_batch_agent_status", "tasks", "批量整理实际进度", read_only=True),
    "tasks.open_source": _op("open_video_page", "tasks", "打开指定任务的原视频页", {"task_id": NAME}, external=True),
    "tasks.set_category": _op("set_category", "tasks", "为明确选中的任务设置分类", {"task_ids": IDS, "category": NAME}),
    "tasks.delete": _op("delete_tasks", "tasks", "永久删除指定任务记录与应用所属产物", {"task_ids": IDS}, destructive=True),
    "tasks.delete_texts": _op("delete_texts", "tasks", "删除指定文本产物，保留视频与任务", {"task_ids": IDS}, destructive=True),
    "tasks.delete_files": _op("delete_files", "tasks", "删除指定任务所属文件", {"task_ids": IDS}, destructive=True),
    "tasks.archive_completed": _op("clear_completed", "tasks", "归档已完成任务，保留文本和文件"),
    "documents.tags": _op("get_tag_index", "documents", "已有标签目录", read_only=True),
    "documents.export_subtitles": _op("export_subtitles", "documents", "导出指定文稿字幕，目标同名文件会被替换", {"task_id": NAME, "fmt": NAME, "dest_path": STR}, destructive=True),
    "account.status": _op("get_subscription_status", "account", "当前账户与订阅状态，不返回会话或许可证", read_only=True),
    "account.email_start": _op("email_login_start", "account", "向用户指定邮箱请求验证码", {"email": NAME}, external=True),
    "account.email_verify": _op("email_login_verify", "account", "提交用户输入验证码完成登录或绑定", {
        "email": NAME, "code": _field("string", maximum=20), "intent": _field("string", required=False, maximum=20)}, external=True, secret_inputs=("code",)),
    "account.google_start": _op("google_login_start", "account", "打开 Google 登录或绑定页面，由用户完成认证", {"intent": _field("string", required=False, maximum=20)}, external=True),
    "account.google_cancel": _op("google_login_cancel", "account", "取消当前 Google 登录请求", external=True),
    "account.web_start": _op("web_login_start", "account", "打开网页授权桌面登录，需核对确认码", external=True),
    "account.web_poll": _op("web_login_poll", "account", "领取本桌面的网页授权结果", external=True),
    "account.web_cancel": _op("web_login_cancel", "account", "取消本轮网页授权，保留现有账户", external=True),
    "account.wechat_start": _op("wechat_login_start", "account", "开始服务实际支持的微信登录", external=True),
    "account.login_poll": _op("wechat_login_poll", "account", "查询当前快捷登录实际状态", external=True),
    "account.login_complete": _op("complete_account_login", "account", "完成已验证身份的设备绑定登录", external=True),
    "account.login_cancel": _op("cancel_account_login", "account", "取消尚未完成登录，保留已有账户", external=True),
    "account.refresh": _op("refresh_subscription", "account", "刷新实际账户及验签权益", external=True),
    "account.logout": _op("account_logout", "account", "退出当前账户并清除本地会话", external=True),
    "account.profile_update": _op("update_account_profile", "account", "保存昵称和可选头像", {"display_name": NAME, "avatar": _field("string", required=False, maximum=400000)}, external=True),
    "account.devices": _op("get_account_devices", "account", "查看当前账户绑定设备", read_only=True, external=True),
    "account.device_unbind": _op("unbind_account_device", "account", "解绑指定设备，当前设备被解绑将退出登录", {"target_device": NAME}, destructive=True, external=True),
    "account.buyer_start": _op("buyer_verification_start", "account", "打开爱发电买家授权，由用户完成", external=True),
    "account.buyer_poll": _op("buyer_verification_poll", "account", "查询买家验证并按实际结果更新账户", external=True),
    "account.buyer_cancel": _op("buyer_verification_stop", "account", "取消当前买家授权请求", external=True),
    "payments.plans": _op("get_subscription_plans", "payments", "当前展示套餐", read_only=True),
    "payments.service_check": _op("check_subscription_service", "payments", "检查服务实际登录与支付能力", external=True),
    "payments.contract": _op("get_subscription_contract", "payments", "读取当前购买合同与服务状态", read_only=True),
    "payments.alipay_config": _op("get_alipay_payment_config", "payments", "读取实际支付宝购买目录", read_only=True, external=True),
    "payments.alipay_orders": _op("list_alipay_orders", "payments", "查看当前用户已有订单", read_only=True, external=True),
    "payments.alipay_create": _op("create_alipay_order", "payments", "按服务端报价创建当前用户订单，用户亲自付款", {"plan_id": NAME, "period": NAME, "client_request_id": NAME}, external=True),
    "payments.alipay_get": _op("get_alipay_order", "payments", "读取已有订单，可恢复该订单付款入口", {"order_id": NAME, "resume_payment": _field("boolean", required=False)}, external=True),
    "payments.alipay_query": _op("query_alipay_order", "payments", "官方查询订单并按实际结果刷新权益", {"order_id": NAME}, external=True),
    "payments.alipay_open": _op("open_alipay_cashier", "payments", "打开已验证订单收银台，由用户付款", {"order_id": NAME}, external=True),
    "payments.checkout": _op("begin_subscription", "payments", "准备受服务允许的套餐购买入口", {"plan_id": NAME, "period": _field("string", required=False, maximum=20)}, external=True),
    "payments.redeem": _op("redeem_subscription_order", "payments", "核验当前用户的真实爱发电订单并兑换", {"order_no": NAME}, external=True),
    "payments.card_redeem": _op("redeem_beta_card", "payments", "为当前登录账户兑换共享内测卡，资格以签名许可证为准", {
        "card_code": _field("string", maximum=80), "client_request_id": _field("string", required=False, maximum=128)},
        external=True, secret_inputs=("card_code",)),
    "payments.manual_config": _op("get_manual_payment_config", "payments", "读取实际人工核验收款能力", read_only=True, external=True),
    "payments.manual_requests": _op("list_manual_payment_requests", "payments", "当前用户收款申请", read_only=True, external=True),
    "payments.manual_create": _op("create_manual_payment_request", "payments", "创建固定套餐申请，用户付款后申报", {"plan_id": NAME, "period": NAME, "client_request_id": NAME}, external=True),
    "payments.manual_get": _op("get_manual_payment_request", "payments", "查看当前用户申请", {"request_id": NAME}, read_only=True, external=True),
    "payments.manual_submit": _op("submit_manual_payment_request", "payments", "提交用户提供交易号，等待实际账单核验", {"request_id": NAME, "transaction_id": NAME}, external=True, secret_inputs=("transaction_id",)),
    "payments.manual_cancel": _op("cancel_manual_payment_request", "payments", "取消尚未到账申请", {"request_id": NAME}, external=True),
    "maintenance.reconcile": _op("data_reconcile", "maintenance", "核对登记并收敛悬空任务，返回孤立产物"),
    "maintenance.cache": _op("cache_status", "maintenance", "缓存状态", read_only=True),
    "maintenance.cleanup_cache": _op("cleanup_cache", "maintenance", "清理应用缓存", destructive=True),
    "maintenance.cleanup_orphans": _op("cleanup_orphans", "maintenance", "删除明确指定的孤立输出和下载文件", {"outputs": PATHS, "downloads": PATHS}, destructive=True),
    "maintenance.export_backup": _op("export_backup", "maintenance", "备份到本地 ZIP，目标同名文件会被替换", {"dest_path": STR}, destructive=True),
    "maintenance.import_backup": _op("import_backup", "maintenance", "恢复指定备份，替换本实例相应资料", {"zip_path": STR}, destructive=True),
    "maintenance.export_diagnostics": _op("export_diagnostics", "maintenance", "导出脱敏诊断，目标同名文件会被替换", {"dest_path": STR}, destructive=True),
}
AGENT_BRIDGE_METHODS = frozenset(spec.method for spec in OPERATIONS.values())

_SECRET_KEYS = frozenset({
    "agent_api_key", "local_api_token", "account_login_token", "account_pending_login_token", "license_token",
    "subscription_verify_key", "login_token", "management_token", "pending_token", "refresh_token", "access_token",
    "api_key", "secret", "private_key", "cookie", "cookies", "dev_code", "otp", "code_verifier", "session_id",
    "cashier_url", "checkout_url", "authorize_url", "auth_url", "source_url", "resolved_url", "url", "card_code",
})
_AUTO_DELETE_SETTINGS = frozenset({"delete_source_after_translation", "delete_video_after_transcribe",
                                    "delete_process_files_after_transcribe"})


def public_result(value: Any, *, redact: tuple[str, ...] = (), depth: int = 0) -> Any:
    """Strip secret fields and short-lived auth/payment/media URLs recursively."""
    if depth > 14:
        return None
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if type(key) is not str or key.casefold() in _SECRET_KEYS or key.casefold().endswith(("_token", "_secret", "_private_key")):
                continue
            if key == "secret_fields" and isinstance(item, dict):
                result[key] = {name: configured is True for name, configured in item.items() if type(name) is str}
                continue
            result[key] = public_result(item, redact=redact, depth=depth + 1)
        return result
    if isinstance(value, (list, tuple)):
        return [public_result(item, redact=redact, depth=depth + 1) for item in value[:1000]]
    if isinstance(value, str):
        for secret in redact:
            if secret:
                value = value.replace(secret, "[已隐藏]")
        # Raw secret values can occur in upstream provider error strings. Replace
        # signed/auth URL query strings even when embedded in prose.
        value = re.sub(r"https?://[^\s<>\"]*\?[^\s<>\"]+", "[临时地址已隐藏]", value)
        return value[:2_000_000]
    if value is None or type(value) in (bool, int):
        return value
    if type(value) is float:
        return value if math.isfinite(value) else None
    return None


def _validate_value(value: Any, schema: dict) -> None:
    expected = {"string": str, "object": dict, "array": list, "boolean": bool, "integer": int}[schema["type"]]
    if type(value) is not expected:
        raise ApplicationOperationError(400, "invalid_request", "应用操作参数类型无效")
    if isinstance(value, str):
        try:
            value.encode("utf-8")
        except UnicodeError:
            raise ApplicationOperationError(400, "invalid_request", "应用操作参数须使用有效 UTF-8") from None
        if len(value) > schema.get("maxLength", 2_000_000):
            raise ApplicationOperationError(400, "invalid_request", "应用操作参数过长")
    elif isinstance(value, list):
        if len(value) > schema.get("maxItems", 200):
            raise ApplicationOperationError(400, "invalid_request", "操作项目过多")
        for item in value:
            _validate_value(item, {**schema.get("items", {"type": "string"}), "maxLength": 4096})


def _local_path(value: str) -> None:
    drive, _ = ntpath.splitdrive(value)
    absolute = Path(value).is_absolute() or (bool(re.fullmatch(r"[A-Za-z]:", drive)) and ntpath.isabs(value))
    if (not value or value.startswith(("\\\\", "//")) or "\x00" in value or
            not absolute or "://" in value):
        raise ApplicationOperationError(400, "invalid_request", "文件参数须为本机绝对路径，不能使用网络路径或地址")


class AgentOperations:
    def __init__(self, facade: Mapping[str, Callable[..., Any]]):
        # Copy only explicitly registered callable bindings; never arbitrary
        # attributes, evaluation, OS commands or service administration.
        self.facade = {name: function for name, function in facade.items() if name in AGENT_BRIDGE_METHODS and callable(function)}

    def list(self, *, category: str = "") -> dict:
        if type(category) is not str or len(category) > 40:
            raise ApplicationOperationError(400, "invalid_request", "操作分类无效")
        items = []
        for name, spec in OPERATIONS.items():
            if category and spec.category != category:
                continue
            items.append({"operation": name, "category": spec.category, "description": spec.description,
                          "available": spec.method in self.facade, "read_only": spec.read_only,
                          "requires_confirmation": spec.destructive, "external": spec.external,
                          "confirmation_conditions": ["启用处理后自动删除、启用失败记录定期清理，或关闭删除确认时需要 confirm=true"] if name == "settings.update" else [],
                          "may_process_media": spec.processing, "secret_inputs": list(spec.secret_inputs),
                          "arguments_schema": spec.schema})
        return {"items": items, "total": len(items), "categories": sorted({spec.category for spec in OPERATIONS.values()})}

    @staticmethod
    def validate(operation: Any, arguments: Any, *, confirm: bool = False) -> Operation:
        if type(operation) is not str or operation not in OPERATIONS:
            raise ApplicationOperationError(404, "operation_not_found", "应用操作不存在")
        if type(arguments) is not dict or type(confirm) is not bool:
            raise ApplicationOperationError(400, "invalid_request", "应用操作参数无效")
        spec = OPERATIONS[operation]
        allowed = set(spec.fields)
        required = {key for key, field in spec.fields.items() if field[1]}
        if set(arguments) - allowed or not required.issubset(arguments):
            raise ApplicationOperationError(400, "invalid_request", "应用操作参数与已声明字段不匹配")
        for name, value in arguments.items():
            _validate_value(value, spec.fields[name][0])
        for name in ("zip_path", "dest_path"):
            if name in arguments:
                _local_path(arguments[name])
        for name in ("paths", "outputs", "downloads"):
            for value in arguments.get(name, []):
                _local_path(value)
        for name in ("task_id",):
            if name in arguments and not re.fullmatch(r"(?:job|dl):[a-f0-9]{12}", arguments[name]):
                raise ApplicationOperationError(400, "invalid_request", "请使用实际查询的任务 ID")
        for value in arguments.get("task_ids", []):
            if not re.fullmatch(r"(?:job|dl):[a-f0-9]{12}", value):
                raise ApplicationOperationError(400, "invalid_request", "请使用实际查询的任务 ID")
        if any(name in arguments and not arguments[name] for name in ("task_ids", "paths")):
            raise ApplicationOperationError(400, "invalid_request", "请明确指定至少一个操作项目")
        if "media_id" in arguments and not re.fullmatch(r"[a-f0-9]{32}", arguments["media_id"]):
            raise ApplicationOperationError(400, "invalid_request", "请使用视频库查询的媒体 ID")
        if "download_id" in arguments and not re.fullmatch(r"[a-f0-9]{12}", arguments["download_id"]):
            raise ApplicationOperationError(400, "invalid_request", "请使用下载查询的 ID")
        if operation == "settings.update" and not arguments["changes"]:
            raise ApplicationOperationError(400, "invalid_request", "请明确指定要修改的设置字段")
        if operation == "settings.update":
            changes = arguments["changes"]
            if ("failed_task_retention_days" in changes and changes['failed_task_retention_days'] != 0
                    and confirm is not True):
                raise ApplicationOperationError(409, "confirmation_required", "开启定期清理需要明确设置 confirm=true")
            if "js_runtime_path" in changes:
                raise ApplicationOperationError(400, "protected_setting", "执行器路径须由用户在桌面设置中配置")
            if any(key in changes and type(changes[key]) is not bool for key in _AUTO_DELETE_SETTINGS | {"confirm_delete"}):
                raise ApplicationOperationError(400, "invalid_request", "删除设置必须使用布尔值")
            if confirm is not True and (any(changes.get(key) is True for key in _AUTO_DELETE_SETTINGS) or changes.get("confirm_delete") is False):
                raise ApplicationOperationError(409, "confirmation_required", "启用自动删除或关闭删除确认需要明确设置 confirm=true")
        if spec.destructive and confirm is not True:
            raise ApplicationOperationError(409, "confirmation_required", "此操作会删除、替换或解绑数据，请明确设置 confirm=true")
        if "expected_revision" in arguments and not re.fullmatch(r"[a-f0-9]{64}", arguments["expected_revision"]):
            raise ApplicationOperationError(400, "invalid_request", "请使用最近读取的设置 revision")
        return spec

    def call(self, operation: Any, arguments: Any, *, confirm: bool = False) -> Any:
        spec = self.validate(operation, arguments, confirm=confirm)
        callback = self.facade.get(spec.method)
        if callback is None:
            raise ApplicationOperationError(409, "operation_unavailable", "当前运行实例不支持此操作，请更新应用")
        secrets: list[str] = []
        for path in spec.secret_inputs:
            current: Any = arguments
            for part in path.split("."):
                current = current.get(part) if isinstance(current, dict) else None
            if isinstance(current, str) and current:
                secrets.append(current)
        try:
            result = callback(**arguments)
        except Exception:
            # Provider exceptions can embed credentials or source URLs. Keep
            # error details in the application, never expose repr/tracebacks.
            raise ApplicationOperationError(409, "operation_failed", "应用未完成此操作，请读取相关状态后再处理") from None
        result = public_result(result, redact=tuple(secrets))
        if isinstance(result, dict) and result.get("ok") is False:
            code = result.get("code")
            code = code if isinstance(code, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", code) else "operation_rejected"
            message = result.get("error") or result.get("message") or "应用拒绝了此操作"
            raise ApplicationOperationError(409, code, str(message)[:500])
        return result if result is not None else {"ok": True}
