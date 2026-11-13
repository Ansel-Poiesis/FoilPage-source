# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""客户端热切换配置：封闭 schema、跨语言 canonical JSON 与 Ed25519 验签。

Worker 端 ``payment_service/cloudflare/client-config.mjs`` 用同名规则签名，
本模块验签——两端必须产出逐字节一致的 canonical JSON：只允许
str/bool/int（禁浮点）/list/dict，键递归排序、无空白、UTF-8 原文。
未知字段一律拒绝（改版时新增字段必须两端同步发版，配置只做参数热切换）。
公钥内嵌本模块；私钥仅存 Worker secret 与 ``%LOCALAPPDATA%/ShengJianPayment/private.env``。
"""
from __future__ import annotations

import base64
import json
import re
from typing import Any

from engine import ed25519

CLIENT_CONFIG_PUBLIC_KEY_B64 = "u4MfHect5nQRDiz7mMLRYMpaTC3056EA3m+APHFDpYs="

# 与 Worker CLIENT_CONFIG 保持同构的离线默认值（version=0 表示默认而非已下发）。
DEFAULT_CLIENT_CONFIG: dict[str, Any] = {
    "flags": {"douyin_gallery_enabled": False, "covers_enabled": False},
    "douyin_gallery": {
        "images_field_names": ["images"],
        "image_cdn_suffixes": [],
        "watermark_markers": [],
        "max_images_per_task": 60,
    },
}

_FLAGS_KEYS = {"douyin_gallery_enabled", "covers_enabled"}
_GALLERY_LIST_KEYS = ("images_field_names", "image_cdn_suffixes", "watermark_markers")
_GALLERY_KEYS = {"images_field_names", "image_cdn_suffixes", "watermark_markers", "max_images_per_task"}
_TOP_KEYS = {"flags", "douyin_gallery"}
_PAYLOAD_KEYS = {"ok", "version", "min_app_version", "generated_at", "config", "signature"}
_GENERATED_AT_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


class ClientConfigError(ValueError):
    """配置结构或签名不合法；调用方应拒绝并保留旧值。"""


def canonical_json(value: Any) -> str:
    """与 client-config.mjs 的 canonicalClientConfigJson 逐字节一致。"""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _check_string(value: str, where: str, *, max_length: int) -> None:
    if not isinstance(value, str) or not value or len(value) > max_length:
        raise ClientConfigError(f"{where} 必须是 1-{max_length} 字符的字符串")
    if any(ord(ch) < 0x20 or 0xD800 <= ord(ch) <= 0xDFFF for ch in value):
        raise ClientConfigError(f"{where} 含控制字符或代理对")


def validate_schema(config: Any) -> dict[str, Any]:
    """封闭 schema 校验；非法即抛 ClientConfigError，绝不静默放行未知字段。"""
    if not isinstance(config, dict) or set(config) != _TOP_KEYS:
        raise ClientConfigError("config 顶层字段必须是 flags/douyin_gallery")
    flags = config["flags"]
    if not isinstance(flags, dict) or set(flags) != _FLAGS_KEYS:
        raise ClientConfigError("flags 字段必须是 douyin_gallery_enabled/covers_enabled")
    for key in _FLAGS_KEYS:
        if not isinstance(flags[key], bool):
            raise ClientConfigError(f"flags.{key} 必须是布尔")
    gallery = config["douyin_gallery"]
    if not isinstance(gallery, dict) or set(gallery) != _GALLERY_KEYS:
        raise ClientConfigError("douyin_gallery 字段必须是 " + "/".join(sorted(_GALLERY_KEYS)))
    for key in _GALLERY_LIST_KEYS:
        items = gallery[key]
        if not isinstance(items, list) or len(items) > 32:
            raise ClientConfigError(f"douyin_gallery.{key} 必须是不超过 32 项的数组")
        if key == "images_field_names" and not items:
            raise ClientConfigError("douyin_gallery.images_field_names 不能为空（图集探测字段）")
        for index, item in enumerate(items):
            _check_string(item, f"douyin_gallery.{key}[{index}]", max_length=128)
    limit = gallery["max_images_per_task"]
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 200:
        raise ClientConfigError("douyin_gallery.max_images_per_task 必须是 1-200 的整数")
    return config


def verify_payload(
    payload: Any,
    public_key_b64: str = CLIENT_CONFIG_PUBLIC_KEY_B64,
) -> dict[str, Any]:
    """校验 /v1/client-config 响应：结构闭合、Ed25519 签名有效。

    返回 ``{"version", "min_app_version", "generated_at", "config"}``（即签名覆盖的
    canonical 内容）；ok 仅为传输标记，不参与签名。
    """
    if not isinstance(payload, dict) or set(payload) != _PAYLOAD_KEYS:
        raise ClientConfigError("响应字段必须是 " + "/".join(sorted(_PAYLOAD_KEYS)))
    if payload.get("ok") is not True:
        raise ClientConfigError("响应 ok 必须为 true")
    version = payload["version"]
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise ClientConfigError("version 必须是正整数")
    _check_string(payload["min_app_version"], "min_app_version", max_length=32)
    if not _GENERATED_AT_RE.fullmatch(str(payload["generated_at"])):
        raise ClientConfigError("generated_at 格式无效")
    config = validate_schema(payload["config"])
    _check_string(payload["signature"], "signature", max_length=128)
    try:
        public = base64.b64decode(public_key_b64, validate=True)
        signature = base64.b64decode(payload["signature"], validate=True)
    except (ValueError, TypeError) as exc:
        raise ClientConfigError("公钥或签名不是合法 base64") from exc
    message = canonical_json({
        "version": version,
        "min_app_version": payload["min_app_version"],
        "generated_at": payload["generated_at"],
        "config": config,
    }).encode("utf-8")
    if not ed25519.verify(public, message, signature):
        raise ClientConfigError("配置签名验证失败")
    return {
        "version": version,
        "min_app_version": payload["min_app_version"],
        "generated_at": payload["generated_at"],
        "config": config,
    }


def fetch_config(base_url: str, timeout: float = 12.0) -> dict[str, Any]:
    """拉取 Worker 的 /v1/client-config 原始响应（不验签，交给 verify_payload）。"""
    import urllib.request

    from engine.service_http import payment_service_headers

    base = str(base_url or "").strip().rstrip("/")
    if not base.startswith("https://"):
        raise ClientConfigError("client-config 只接受 https 服务地址")
    request = urllib.request.Request(
        base + "/v1/client-config", method="GET", headers=payment_service_headers())
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read(262145)
    if len(raw) > 262144:
        raise ClientConfigError("client-config 响应超出大小限制")
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError) as exc:
        raise ClientConfigError("client-config 响应不是合法 JSON") from exc
