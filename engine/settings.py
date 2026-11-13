# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""Versioned application settings stored outside the Poiesis knowledge base."""
from __future__ import annotations

import base64
import json
import os
import secrets
import stat
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any, Mapping

try:
    import win32crypt
except Exception:  # pragma: no cover - non-Windows development fallback
    win32crypt = None


CURRENT_SETTINGS_VERSION = 20
APP_DIRECTORY_NAME = "FoilPage"
LEGACY_APP_DIRECTORY_NAME = "VideoToArticle"
LOCAL_API_PORT = 18765
# 任务终态提示音预设组（1.3.12）：app.notification_sounds 消费同一份枚举。
NOTIFICATION_SOUND_PACKS = ("system", "silent", "crisp", "soft", "deep", "custom")
# Public production connection values; no server signing or API secret ships
# with the desktop application.
DEFAULT_SUBSCRIPTION_SERVER_URL = "https://pay.vellumfeed.com"
DEFAULT_SUBSCRIPTION_LICENSE_PUBLIC_KEY = "Giz/dVXfLK0gGUYhtSbERp+HEWl1e6PTlWqr7gQazh8="
_SECRET_PREFIX = "dpapi:v1:"
# DPAPI 加密落盘的字段清单；导出/诊断脱敏（app.maintenance）必须引用同一份，
# 两处各自维护曾导致 license_token 明文进备份。
SECRET_FIELDS = ("agent_api_key", "local_api_token", "license_token", "account_access_token", "account_login_token", "account_pending_login_token", "subscription_verify_key")
_SECRET_FIELDS = SECRET_FIELDS


class SettingsError(RuntimeError):
    pass


class SettingsValidationError(SettingsError):
    pass


def _protect_secret(value: str) -> str:
    if not value or value.startswith(_SECRET_PREFIX):
        return value
    if win32crypt is None:
        if sys.platform == "win32":
            raise SettingsError("当前系统缺少 DPAPI 支持，无法安全保存密钥")
        return value
    blob = win32crypt.CryptProtectData(
        value.encode("utf-8"),
        "ShengJian settings secret",
        None,
        None,
        None,
        0,
    )
    return _SECRET_PREFIX + base64.b64encode(blob).decode("ascii")


def _unprotect_secret(value: str) -> str:
    if not value or not value.startswith(_SECRET_PREFIX):
        return value
    if win32crypt is None:
        raise SettingsError("当前系统缺少 DPAPI 支持，无法解密设置密钥")
    try:
        _, data = win32crypt.CryptUnprotectData(
            base64.b64decode(value[len(_SECRET_PREFIX) :]),
            None,
            None,
            None,
            0,
        )
    except Exception as exc:
        raise SettingsError(f"设置密钥解密失败: {exc}") from exc
    return data.decode("utf-8")


def default_app_data_dir(environment: Mapping[str, str] | None = None) -> Path:
    env = os.environ if environment is None else environment
    local = env.get("LOCALAPPDATA")
    if local:
        root = Path(local)
    else:
        profile = Path(env.get("USERPROFILE", str(Path.home())))
        root = profile / "AppData" / "Local"
    current = root / APP_DIRECTORY_NAME
    # 1.3.8 起目录名为 FoilPage；桌面端完成一次性改名迁移前，老用户资料仍在
    # VideoToArticle 旧根下，解析必须继续命中旧根，否则登录态与资料库"消失"。
    legacy = root / LEGACY_APP_DIRECTORY_NAME
    if not current.exists() and legacy.exists():
        return legacy
    return current


def migrate_legacy_data_root(environment: Mapping[str, str] | None = None) -> bool:
    """桌面端独有的一次性改名迁移：VideoToArticle → FoilPage（1.3.8）。

    把旧根原地重命名为新根（同卷原子操作，不复制、不改动任何文件），并在旧
    路径留下指向新根的 junction 垫片——回滚到 ≤1.3.7 的旧版本时，旧代码按旧
    路径读写仍能透过垫片命中同一份数据。失败时调用方按未迁移继续，解析回退
    旧根，下次启动重试。agent/client.py 刻意不迁移，只做同样的解析回退。
    """
    env = os.environ if environment is None else environment
    local = env.get("LOCALAPPDATA")
    if local:
        root = Path(local)
    else:
        profile = Path(env.get("USERPROFILE", str(Path.home())))
        root = profile / "AppData" / "Local"
    current = root / APP_DIRECTORY_NAME
    legacy = root / LEGACY_APP_DIRECTORY_NAME
    if current.exists() or not legacy.exists():
        return False
    if os.name == "nt":
        # Junction/symlink 旧根属于异常残留状态（重命名已被并发完成过），不接管。
        # lstat：junction 必须不穿透，stat 会跟到目标目录丢失 reparse 标志。
        attributes = os.lstat(legacy).st_file_attributes
        if attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            return False
    elif legacy.is_symlink():
        return False
    try:
        os.rename(legacy, current)
    except OSError:
        # 旧根被句柄或 CWD 占用（自锁、agent CLI、扫描器）：放弃本次迁移。解析
        # 继续回退旧根，应用照常工作，下次启动重试；绝不能让升级后启动失败。
        return False
    if os.name == "nt":
        try:
            import _winapi
            _winapi.CreateJunction(str(current), str(legacy))
        except OSError:
            # 重命名已成功；垫片缺失只影响回滚到旧版本的场景，不阻断本次启动。
            pass
    return True


def default_logs_dir(environment: Mapping[str, str] | None = None) -> Path:
    """Log 目录的唯一推导入口；改名只动这里，别处不允许自行拼接。"""
    return default_app_data_dir(environment) / "logs"


class SettingsStore:
    def __init__(
        self,
        *,
        base_dir: str | Path | None = None,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        self.base_dir = (
            Path(base_dir).expanduser().resolve()
            if base_dir is not None
            else default_app_data_dir(environment)
        )
        self.path = self.base_dir / "settings.json"
        self._lock = threading.RLock()

    def defaults(self) -> dict[str, Any]:
        return {
            "schema_version": CURRENT_SETTINGS_VERSION,
            "database_path": str(self.base_dir / "library.db"),
            "output_dir": str(self.base_dir / "output"),
            "whisper_model": "tiny",
            "device": "cuda",
            "language": "zh",
            "ui_language": "zh-CN",
            "model_dir": "",
            "asr_initial_prompt": "",
            "asr_hotwords": "",
            "asr_batched_enabled": True,
            "js_runtime_path": "",
            "allow_any_url": False,
            "poll_seconds": 2.0,
            "heartbeat_seconds": 5.0,
            "lease_timeout_seconds": 300.0,
            "max_attempts": 3,
            "worker_auto_start": True,
            "worker_auto_restart": True,
            "launch_at_startup": False,
            "ci_runner_wakeup": False,
            "close_to_tray": True,
            "task_notifications": True,
            "notification_sound_pack": "system",
            "notification_sound_success_path": "",
            "notification_sound_error_path": "",
            "theme": "dark",
            "background_style": "glow",
            "accent": "green",
            "font_scale": 1.15,
            "hf_endpoint": "https://hf-mirror.com",
            "download_dir": str(self.base_dir / "downloads"),
            "browser_download_dir": str(Path.home() / "Downloads"),
            "download_rate_limit_kib": 0,
            "download_min_free_gib": 5.0,
            "download_proxy_policy": "auto",
            "download_proxy_url": "",
            "local_api_enabled": True,
            "local_api_port": LOCAL_API_PORT,
            "local_api_token": "",
            "extension_origin": "",
            "agent_enabled": False,
            "agent_billing_mode": "byok",
            # 托管模型选择（1.3.12）：空=跟随服务端默认；显式值由服务端
            # 按已复核政策校验（与 /v1/health 的 agent_relay.models 对齐）。
            "agent_hosted_model": "",
            "agent_provider": "mimo",
            "agent_base_url": "https://api.xiaomimimo.com/v1",
            "agent_model": "mimo-v2.6-flash",
            "agent_api_key": "",
            "agent_profile": "auto",
            "agent_editing_level": "auto",
            "reorganize_no_remind": False,
            "reorganize_last_mode": "article",
            "reorganize_last_level": "auto",
            "agent_temperature": 0.6,
            "agent_timeout_seconds": 120.0,
            "agent_vision_enabled": True,
            "agent_vision_model": "",
            "agent_vision_frame_count": 6,
            "agent_custom_profiles": [],
            "delete_source_after_translation": False,
            "confirm_delete": True,
            "animations": True,
            "subscription_plan": "free",
            "subscription_active": False,
            "subscription_expires_at": None,
            "plan_expiry_last_notice_day": None,
            "subscription_expiry_notice_history": None,
            "subscription_checkout_url": "",
            "subscription_server_url": DEFAULT_SUBSCRIPTION_SERVER_URL,
            "license_token": "",
            "account_access_token": "",
            "subscription_license_public_key": DEFAULT_SUBSCRIPTION_LICENSE_PUBLIC_KEY,
            "subscription_verify_key": "",
            "license_expires_at": None,
            "account_login_token": "",
            "account_pending_login_token": "",
            "account_display_name": "",
            "account_avatar": "",
            "account_user_id": "",
            "credit_balance": 0,
            "terms_accepted_version": 0,
            "terms_cross_border_accepted_version": 0,
            "terms_accepted_service": "",
            "subscription_last_order_id": "",
            "agent_usage_period": "",
            "agent_usage_used": 0,
            "transcribe_after_download": False,
            "download_covers": True,
            "download_pending_timeout_minutes": 120,
            "delete_video_after_transcribe": False,
            "delete_process_files_after_transcribe": False,
            "update_title_after_transcribe": True,
            "title_optimization_enabled": False,
            "title_model": "",
            "title_skill_prompt": "",
            "task_sort_order": "newest",
            "setup_completed": False,
            "video_scan_directories": [],
            "video_scan_recursive": False,
            "failed_task_retention_days": 0,
            "failed_task_cleanup_interval_hours": 24,
            "superseded_failure_retention_hours": 24,
        "onboarding_completed_version": "",
        "onboarding_chapter_progress": {"schema_version": 1, "chapters": {}},
        }

    def load(self) -> dict[str, Any]:
        with self._lock:
            if not self.path.exists():
                return self.defaults()
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise SettingsError(f"设置文件无法读取: {self.path}: {exc}") from exc
            if not isinstance(raw, dict):
                raise SettingsError("设置文件根节点必须是 JSON object")
            decrypted = self._decrypt_settings(raw)
            migrated = self._migrate(decrypted)
            self.validate(migrated)
            needs_secret_upgrade = any(
                isinstance(raw.get(field), str)
                and bool(raw[field])
                and not raw[field].startswith(_SECRET_PREFIX)
                for field in _SECRET_FIELDS
            )
            if migrated != decrypted or needs_secret_upgrade:
                self.save(migrated)
            return migrated

    def save(self, settings: Mapping[str, Any]) -> dict[str, Any]:
        with self._lock:
            data = dict(settings)
            data["schema_version"] = CURRENT_SETTINGS_VERSION
            self.validate(data)
            disk_data = self._encrypt_settings(data)
            self.base_dir.mkdir(parents=True, exist_ok=True)
            temp_path: Path | None = None
            try:
                with tempfile.NamedTemporaryFile(
                    "w",
                    encoding="utf-8",
                    dir=self.base_dir,
                    prefix=".settings-",
                    suffix=".tmp",
                    delete=False,
                ) as stream:
                    json.dump(disk_data, stream, ensure_ascii=False, indent=2)
                    stream.write("\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                    temp_path = Path(stream.name)
                os.replace(temp_path, self.path)
                temp_path = None
            finally:
                if temp_path is not None:
                    try:
                        temp_path.unlink()
                    except OSError:
                        pass
            return data

    @staticmethod
    def _encrypt_settings(data: Mapping[str, Any]) -> dict[str, Any]:
        encrypted = dict(data)
        for field in _SECRET_FIELDS:
            value = encrypted.get(field)
            if isinstance(value, str):
                encrypted[field] = _protect_secret(value)
        return encrypted

    @staticmethod
    def _decrypt_settings(raw: Mapping[str, Any]) -> dict[str, Any]:
        decrypted = dict(raw)
        for field in _SECRET_FIELDS:
            value = decrypted.get(field)
            if isinstance(value, str):
                decrypted[field] = _unprotect_secret(value)
        return decrypted

    def update(self, **changes: Any) -> dict[str, Any]:
        with self._lock:
            settings = self.load()
            settings.update(changes)
            return self.save(settings)

    def ensure_runtime_directories(self) -> None:
        settings = self.load()
        self.base_dir.mkdir(parents=True, exist_ok=True)
        Path(settings["output_dir"]).expanduser().mkdir(parents=True, exist_ok=True)
        Path(settings["download_dir"]).expanduser().mkdir(parents=True, exist_ok=True)
        Path(settings["browser_download_dir"]).expanduser().mkdir(parents=True, exist_ok=True)
        Path(settings["database_path"]).expanduser().parent.mkdir(
            parents=True, exist_ok=True
        )

    def validate(self, settings: Mapping[str, Any]) -> None:
        for name in ("terms_accepted_version", "terms_cross_border_accepted_version"):
            if type(settings.get(name, 0)) is not int or not 0 <= settings.get(name, 0) <= 1_000_000:
                raise SettingsError("条款版本无效")
        if not isinstance(settings.get("terms_accepted_service", ""), str) or len(settings.get("terms_accepted_service", "")) > 2048:
            raise SettingsError("条款服务标识无效")
        required_strings = (
            "database_path", "output_dir", "whisper_model", "device", "language",
        )
        for field in required_strings:
            value = settings.get(field)
            if not isinstance(value, str) or not value.strip():
                raise SettingsValidationError(f"{field} 必须是非空字符串")
        for field in ("database_path", "output_dir", "download_dir", "browser_download_dir"):
            if not isinstance(settings.get(field), str) or not settings[field].strip():
                raise SettingsValidationError(f"{field} 必须是非空字符串")
            if any(part.casefold() == "poiesis" for part in Path(settings[field]).parts):
                raise SettingsValidationError(f"{field} 不得位于 Poiesis 目录")
        if settings["device"] not in ("cpu", "cuda"):
            raise SettingsValidationError("device 必须是 cpu 或 cuda")
        model_dir = settings.get("model_dir", "")
        if not isinstance(model_dir, str):
            raise SettingsValidationError("model_dir 必须是字符串")
        for field in ("asr_initial_prompt", "asr_hotwords", "js_runtime_path"):
            if not isinstance(settings.get(field), str):
                raise SettingsValidationError(f"{field} 必须是字符串")
        for field in ("asr_batched_enabled", "allow_any_url"):
            if not isinstance(settings.get(field), bool):
                raise SettingsValidationError(f"{field} 必须是 bool")
        self._positive_number(settings, "poll_seconds")
        self._positive_number(settings, "heartbeat_seconds")
        self._positive_number(settings, "lease_timeout_seconds")
        attempts = settings.get("max_attempts")
        if not isinstance(attempts, int) or isinstance(attempts, bool) or attempts < 1:
            raise SettingsValidationError("max_attempts 必须是 >= 1 的整数")
        # 悬挂任务判失败时限（先生 2026-10-25：默认两小时交给用户设置；
        # 页面采集窗口约两小时，超龄地址必然失效，早判早重试）。
        pending_timeout = settings.get("download_pending_timeout_minutes")
        if (
            not isinstance(pending_timeout, int) or isinstance(pending_timeout, bool)
            or not 5 <= pending_timeout <= 1440
        ):
            raise SettingsValidationError("download_pending_timeout_minutes 必须是 5–1440 的整数分钟")
        for field in (
            "worker_auto_start", "worker_auto_restart", "launch_at_startup", "ci_runner_wakeup",
            "close_to_tray", "confirm_delete", "transcribe_after_download", "download_covers",
            "delete_video_after_transcribe", "delete_process_files_after_transcribe",
            "update_title_after_transcribe", "title_optimization_enabled", "task_notifications",
        ):
            if not isinstance(settings.get(field), bool):
                raise SettingsValidationError(f"{field} 必须是 bool")
        if settings.get("task_sort_order") not in ("newest", "oldest"):
            raise SettingsValidationError("task_sort_order 必须是 newest 或 oldest")
        if settings.get("notification_sound_pack") not in NOTIFICATION_SOUND_PACKS:
            raise SettingsValidationError("notification_sound_pack 无效")
        for field in ("notification_sound_success_path", "notification_sound_error_path"):
            if not isinstance(settings.get(field), str):
                raise SettingsValidationError(f"{field} 必须是字符串")
        if settings.get("agent_editing_level") not in ("auto", "light", "balanced", "structured", "rewrite"):
            raise SettingsValidationError("整理力度无效")
        if not isinstance(settings.get("reorganize_no_remind"), bool):
            raise SettingsValidationError("重新转写提醒必须是 bool")
        if settings.get("reorganize_last_mode") not in ("default", "raw", "article"):
            raise SettingsValidationError("重新转写模式无效")
        if settings.get("reorganize_last_level") not in ("", "auto", "light", "balanced", "structured", "rewrite"):
            raise SettingsValidationError("重新转写整理力度无效")
        if settings.get("agent_hosted_model") not in ("", "qwen-plus", "deepseek-flash"):
            raise SettingsValidationError("托管模型选择无效")
        if settings.get("ui_language") not in ("zh-CN", "en"):
            raise SettingsValidationError("ui_language 必须是 zh-CN 或 en")
        if settings.get("theme") not in (
            "dark", "paper", "dracula", "tokyo-night", "nord", "mocha",
            "gruvbox", "rose-pine", "latte", "nord-light",
        ):
            raise SettingsValidationError("theme 非法")
        if settings.get("accent") not in ("green", "rose", "indigo", "ink"):
            raise SettingsValidationError("accent 非法")
        font_scale = settings.get("font_scale")
        if (
            not isinstance(font_scale, (int, float))
            or isinstance(font_scale, bool)
            or not 0.9 <= float(font_scale) <= 1.3
        ):
            raise SettingsValidationError("font_scale 必须在 0.9 到 1.3 之间")
        if not isinstance(settings.get("hf_endpoint"), str):
            raise SettingsValidationError("hf_endpoint 必须是字符串")
        rate_limit = settings.get("download_rate_limit_kib")
        if (
            not isinstance(rate_limit, int) or isinstance(rate_limit, bool)
            or not 0 <= rate_limit <= 1_048_576
        ):
            raise SettingsValidationError("download_rate_limit_kib 必须是 0 到 1048576 的整数")
        min_free = settings.get("download_min_free_gib")
        if (
            not isinstance(min_free, (int, float)) or isinstance(min_free, bool)
            or not 0 <= float(min_free) <= 1024
        ):
            raise SettingsValidationError("download_min_free_gib 必须在 0 到 1024 之间")
        if settings.get("download_proxy_policy") not in ("auto", "direct", "system", "custom"):
            raise SettingsValidationError("download_proxy_policy 必须是 auto/direct/system/custom")
        proxy_url = settings.get("download_proxy_url")
        if not isinstance(proxy_url, str) or len(proxy_url) > 512:
            raise SettingsValidationError("download_proxy_url 必须是不超过 512 字符的字符串")
        # 自定义代理仅在填了地址时校验形态；custom＋空地址在运行时按系统
        # 代理兜底（避免用户先切下拉框后填地址被保存顺序卡死）。
        if proxy_url.strip():
            import re as _re

            if not _re.fullmatch(
                r"(?:http|https|socks4|socks5|socks5h)://[A-Za-z0-9._~!$&*()\',;=:@%-]+(?::\d{1,5})?(?:/[!-~]*)?",
                proxy_url.strip(),
            ):
                raise SettingsValidationError(
                    "download_proxy_url 需形如 http://127.0.0.1:7890 或 socks5://host:1080"
                )
        if not isinstance(settings.get("local_api_enabled"), bool):
            raise SettingsValidationError("local_api_enabled 必须是 bool")
        port = settings.get("local_api_port")
        if not isinstance(port, int) or isinstance(port, bool) or not 1024 <= port <= 65535:
            raise SettingsValidationError("local_api_port 必须在 1024 到 65535 之间")
        for field in ("local_api_token", "extension_origin"):
            if not isinstance(settings.get(field), str):
                raise SettingsValidationError(f"{field} 必须是字符串")
        if not isinstance(settings.get("agent_enabled"), bool):
            raise SettingsValidationError("agent_enabled 必须是 bool")
        if settings.get("agent_billing_mode") not in ("byok", "hosted"):
            raise SettingsValidationError("agent_billing_mode 必须是 byok 或 hosted")
        if settings.get("agent_provider") not in ("mimo", "deepseek", "openai"):
            raise SettingsValidationError("agent_provider 必须是 mimo、deepseek 或 openai")
        for field in ("agent_base_url", "agent_model", "agent_api_key", "title_model", "title_skill_prompt"):
            if not isinstance(settings.get(field), str):
                raise SettingsValidationError(f"{field} 必须是字符串")
        if len(str(settings.get("title_skill_prompt") or "")) > 4000:
            raise SettingsValidationError("标题规则最长 4000 字符")
        profile = settings.get("agent_profile")
        builtin = ("auto", "emotional_copy", "consulting", "knowledge")
        if profile not in builtin and not (
            isinstance(profile, str)
            and profile.startswith("custom:")
            and bool(profile[len("custom:"):].strip())
        ):
            raise SettingsValidationError("agent_profile 非法")
        temperature = settings.get("agent_temperature")
        if (not isinstance(temperature, (int, float)) or isinstance(temperature, bool)
                or not 0 <= float(temperature) <= 2):
            raise SettingsValidationError("agent_temperature 必须在 0 到 2 之间")
        self._positive_number(settings, "agent_timeout_seconds")
        if not isinstance(settings.get("agent_vision_enabled"), bool):
            raise SettingsValidationError("agent_vision_enabled 必须是 bool")
        if not isinstance(settings.get("agent_vision_model"), str):
            raise SettingsValidationError("agent_vision_model 必须是字符串")
        frame_count = settings.get("agent_vision_frame_count")
        if (
            not isinstance(frame_count, int) or isinstance(frame_count, bool)
            or not 1 <= frame_count <= 12
        ):
            raise SettingsValidationError("agent_vision_frame_count 必须是 1 到 12 的整数")
        custom_profiles = settings.get("agent_custom_profiles")
        if not isinstance(custom_profiles, list):
            raise SettingsValidationError("agent_custom_profiles 必须是数组")
        seen_ids: set[str] = set()
        for item in custom_profiles:
            if not isinstance(item, dict):
                raise SettingsValidationError("agent_custom_profiles 每一项必须是对象")
            pid = str(item.get("id") or "").strip()
            name = str(item.get("name") or "").strip()
            prompt = str(item.get("system_prompt") or "").strip()
            if not pid or not name or not prompt:
                raise SettingsValidationError("自定义 Agent 必须有 id、名称和系统提示词")
            if pid in seen_ids:
                raise SettingsValidationError(f"自定义 Agent id 重复：{pid}")
            seen_ids.add(pid)
            if not isinstance(item.get("enabled", True), bool):
                raise SettingsValidationError(f"自定义 Agent {pid} 的 enabled 必须是 bool")
            model = item.get("model")
            if model is not None and not isinstance(model, str):
                raise SettingsValidationError(f"自定义 Agent {pid} 的 model 必须是字符串")
            custom_temp = item.get("temperature")
            if custom_temp is not None and (
                not isinstance(custom_temp, (int, float))
                or isinstance(custom_temp, bool)
                or not 0 <= float(custom_temp) <= 2
            ):
                raise SettingsValidationError(f"自定义 Agent {pid} 的 temperature 必须在 0 到 2 之间")
        if not isinstance(settings.get("delete_source_after_translation"), bool):
            raise SettingsValidationError("delete_source_after_translation 必须是 bool")
        version = settings.get("schema_version")
        if version != CURRENT_SETTINGS_VERSION:
            raise SettingsValidationError(
                f"schema_version 必须为 {CURRENT_SETTINGS_VERSION}"
            )
        if not isinstance(settings.get("subscription_server_url"), str):
            raise SettingsValidationError("subscription_server_url 必须是字符串")
        if not isinstance(settings.get("license_token"), str):
            raise SettingsValidationError("license_token 必须是字符串")
        if not isinstance(settings.get("onboarding_completed_version"), str):
            raise SettingsValidationError("onboarding_completed_version 必须是字符串")
        if type(settings.get("setup_completed")) is not bool:
            raise SettingsValidationError("setup_completed 必须是 bool")
        if settings.get("background_style") not in {"plain", "glow", "texture"}:
            raise SettingsValidationError("背景样式无效")
        directories = settings.get("video_scan_directories")
        if (not isinstance(directories, list) or len(directories) > 20
                or any(not isinstance(value, str) or not value.strip() or len(value) > 4096
                       or value.startswith(("\\\\", "//")) or "://" in value
                       or not Path(value).is_absolute() for value in directories)):
            raise SettingsValidationError("视频目录须为本机绝对路径，最多20项")
        if type(settings.get("video_scan_recursive")) is not bool:
            raise SettingsValidationError("video_scan_recursive 必须是 bool")
        for field, lower, upper in (("failed_task_retention_days", 0, 3650),
                                    ("failed_task_cleanup_interval_hours", 1, 720),
                                    ("superseded_failure_retention_hours", 0, 8760)):
            value = settings.get(field)
            if type(value) is not int or not lower <= value <= upper:
                raise SettingsValidationError("失败任务保留设置无效")
        progress = settings.get("onboarding_chapter_progress")
        bounds = {"environment": 4, "browser": 3, "application": 6}
        if (not isinstance(progress, dict) or progress.get("schema_version") != 1
                or isinstance(progress.get("schema_version"), bool)
                or not isinstance(progress.get("chapters"), dict)
                or set(progress) - {"schema_version", "chapters"}):
            raise SettingsValidationError("引导进度格式无效")
        for chapter, value in progress["chapters"].items():
            if (chapter not in bounds or not isinstance(value, dict)
                    or set(value) != {"step", "completed"}
                    or not isinstance(value.get("step"), int)
                    or isinstance(value.get("step"), bool)
                    or not 0 <= value["step"] <= bounds[chapter]
                    or not isinstance(value.get("completed"), bool)):
                raise SettingsValidationError("引导章节进度无效")

    def _migrate(self, raw: dict[str, Any]) -> dict[str, Any]:
        raw_version = raw.get("schema_version", 0)
        if not isinstance(raw_version, int) or isinstance(raw_version, bool):
            raise SettingsValidationError("schema_version 必须是整数")
        if raw_version > CURRENT_SETTINGS_VERSION:
            raise SettingsError(
                f"设置版本 {raw_version} 高于当前支持版本 {CURRENT_SETTINGS_VERSION}"
            )
        migrated = self.defaults()
        migrated.update(raw)
        self._resolve_legacy_runtime_paths(migrated)
        # Returning users have already configured this workstation. This flag
        # selects a landing page only; it never grants login or paid access.
        # v20（2026-11-02）：MiMo 旧旗舰名已于 2026-10-21 失效。视觉模型
        # 死名一律清空（跟随主模型或用户显式重配）；仅当用户仍停留在 MiMo
        # BYOK 且从未改过模型名时，把主模型刷成继任名，不碰其他选择。
        if raw_version < 20:
            if str(raw.get("agent_vision_model") or "") == "mimo-v2.5":
                migrated["agent_vision_model"] = ""
            if (str(migrated.get("agent_provider") or "") == "mimo"
                    and str(raw.get("agent_model") or "") == "mimo-v2.5"):
                migrated["agent_model"] = "mimo-v2.6-flash"
        if raw_version < 18 and "setup_completed" not in raw:
            migrated["setup_completed"] = bool(raw.get("account_login_token")
                and (raw.get("onboarding_completed_version")
                     or str(raw.get("whisper_model", "")).strip()))
        # Treat the service URL and verification key as one trust pair. Filling
        # a missing field from defaults could otherwise combine a custom server
        # with our key, or silently assign an existing session to our service.
        connection_fields = ("subscription_server_url", "subscription_license_public_key")
        for field in connection_fields:
            migrated[field] = raw.get(field, "")
        empty_connection = all(
            isinstance(migrated[field], str) and not migrated[field].strip()
            for field in connection_fields
        )
        account_fields = (
            "account_login_token", "account_pending_login_token", "account_user_id",
            "account_display_name", "account_avatar", "license_token", "account_access_token",
            "license_expires_at", "subscription_verify_key", "subscription_last_order_id",
            "subscription_expires_at", "subscription_checkout_url", "agent_usage_period",
        )
        unassociated = (
            all(raw.get(field, "") in ("", None) for field in account_fields)
            and raw.get("subscription_plan", "free") == "free"
            and raw.get("subscription_active", False) is False
            and raw.get("credit_balance", 0) == 0
            and raw.get("agent_usage_used", 0) == 0
        )
        # A one-time upgrade only. A later deliberate blank configuration stays
        # blank, and partial/custom connections always require explicit repair.
        if raw_version < 17 and empty_connection and unassociated:
            migrated["subscription_server_url"] = DEFAULT_SUBSCRIPTION_SERVER_URL
            migrated["subscription_license_public_key"] = DEFAULT_SUBSCRIPTION_LICENSE_PUBLIC_KEY
        legacy = {
            "model": "whisper_model",
            "lang": "language",
            "poll": "poll_seconds",
        }
        for old, new in legacy.items():
            if old in raw and new not in raw:
                migrated[new] = raw[old]
            migrated.pop(old, None)
        migrated.pop("douyin_browser_session_enabled", None)
        if raw_version < 2 and migrated.get("local_api_port") == 8765:
            migrated["local_api_port"] = LOCAL_API_PORT
        # v12 keeps a deliberate two-theme system. Preserve the intent of the
        # retired light theme with White Rock; a system-following setting must
        # not unexpectedly reintroduce light UI on startup.
        if migrated.get("theme") == "light":
            migrated["theme"] = "paper"
        elif migrated.get("theme") == "system":
            migrated["theme"] = "dark"
        migrated["schema_version"] = CURRENT_SETTINGS_VERSION
        for field in ("poll_seconds", "heartbeat_seconds", "lease_timeout_seconds"):
            value = migrated.get(field)
            if isinstance(value, int) and not isinstance(value, bool):
                migrated[field] = float(value)
        return migrated

    def _resolve_legacy_runtime_paths(self, settings: dict[str, Any]) -> None:
        # Older profiles keep paths through the compatibility junction after
        # the data-root rename. On affected Windows builds each alias open can
        # retain NtFC pool bytes, so use the real path for the same data.
        if self.base_dir.name != APP_DIRECTORY_NAME:
            return
        legacy_root = self.base_dir.parent / LEGACY_APP_DIRECTORY_NAME
        for field in ("database_path", "output_dir", "download_dir", "model_dir"):
            value = settings.get(field)
            if not isinstance(value, str) or not value:
                continue
            path = Path(value).expanduser()
            try:
                suffix = path.relative_to(legacy_root)
            except ValueError:
                continue
            target = self.base_dir / suffix
            if path.resolve() == target.resolve():
                settings[field] = str(target)

    def ensure_local_api_token(self) -> str:
        with self._lock:
            settings = self.load()
            token = settings.get("local_api_token", "").strip()
            if not token:
                token = secrets.token_urlsafe(32)
                settings["local_api_token"] = token
                self.save(settings)
            return token

    @staticmethod
    def _positive_number(settings: Mapping[str, Any], field: str) -> None:
        value = settings.get(field)
        if (
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or float(value) <= 0
        ):
            raise SettingsValidationError(f"{field} 必须是 > 0 的数字")
