# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""FoilPage 许可证载荷：签发与验证。

两种算法、两种密钥语义：
- HS256（联调）：签发与验证共用一个对称密钥（subscription_verify_key），
  密钥会随客户端下发，可被提取——只用于本地开发闭环。
- EdDSA/Ed25519（生产）：服务端用私钥种子签发（PAYMENT_LICENSE_PRIVATE_KEY），
  客户端只内嵌公钥（subscription_license_public_key）验签——客户端二进制
  中不存在私钥，本地无法伪造许可证。签名原语见 engine/ed25519.py
  （RFC 8032，测试向量钉死）。
验证器在算法或密钥不匹配时明确拒绝，不静默降级。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import time
from datetime import datetime, timezone
from typing import Any, Mapping

from engine import ed25519


LOCAL_ALG = "HS256"
PRODUCTION_ALG = "EdDSA"
SUPPORTED_ALGS = (LOCAL_ALG, PRODUCTION_ALG)


class LicenseError(RuntimeError):
    pass


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def _encode_part(payload: Mapping[str, Any]) -> str:
    raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return _b64encode(raw)


def sign_license(
    payload: Mapping[str, Any],
    *,
    signing_key: str,
    alg: str = LOCAL_ALG,
) -> str:
    """签发许可证 token（header.payload.signature）。

    HS256：signing_key 为对称密钥字符串（联调）。
    EdDSA：signing_key 为 base64 编码的 32 字节私钥种子（仅服务端持有，
    `python -m payment_service.keygen` 生成密钥对）。
    """
    header = {"alg": alg, "typ": "JWT"}
    head = _encode_part(header)
    body = _encode_part(payload)
    signing_input = f"{head}.{body}".encode("ascii")
    if alg == LOCAL_ALG:
        signature = hmac.new(
            str(signing_key).encode("utf-8"),
            signing_input,
            hashlib.sha256,
        ).digest()
    elif alg == PRODUCTION_ALG:
        try:
            seed = _b64decode(str(signing_key))
        except (ValueError, TypeError) as exc:
            raise LicenseError("EdDSA 私钥不是合法 base64") from exc
        try:
            signature = ed25519.sign(seed, signing_input)
        except ValueError as exc:
            raise LicenseError(f"EdDSA 私钥无效: {exc}") from exc
    else:
        raise LicenseError(f"不支持的签发算法 {alg}；可用：{', '.join(SUPPORTED_ALGS)}")
    return f"{head}.{body}.{_b64encode(signature)}"


def verify_license(
    token: str,
    *,
    signing_key: str,
    now: float | None = None,
    device_id: str | None = None,
    expected_alg: str | None = None,
) -> dict[str, Any]:
    """验证签名与过期时间，返回 payload；任何失败抛出 LicenseError。

    HS256：signing_key 为对称密钥（联调）。
    EdDSA：signing_key 为 base64 公钥（32 字节，客户端内嵌）。
    服务端在 payload 写入 device_id 时按需传入当前设备标识核对，
    防止一张许可证复制到多台机器使用；未携带或未传入时保持兼容。
    """
    parts = str(token or "").split(".")
    if len(parts) != 3 or not all(parts):
        raise LicenseError("许可证格式无效")
    head_raw, body_raw, sig_raw = parts
    try:
        header = json.loads(_b64decode(head_raw).decode("utf-8"))
        payload = json.loads(_b64decode(body_raw).decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise LicenseError("许可证载荷无法解析") from exc
    if not isinstance(header, dict) or not isinstance(payload, dict):
        raise LicenseError("许可证载荷必须是对象")
    alg = header.get("alg")
    if expected_alg and alg != expected_alg:
        raise LicenseError("许可证算法与配置不匹配")
    if alg not in SUPPORTED_ALGS:
        raise LicenseError("许可证算法不支持或未启用本地验证")
    try:
        actual = _b64decode(sig_raw)
    except (ValueError, TypeError) as exc:
        raise LicenseError("许可证签名格式无效") from exc
    signing_input = f"{head_raw}.{body_raw}".encode("ascii")
    if alg == LOCAL_ALG:
        expected = hmac.new(
            str(signing_key).encode("utf-8"),
            signing_input,
            hashlib.sha256,
        ).digest()
        if not hmac.compare_digest(expected, actual):
            raise LicenseError("许可证签名无效")
    else:
        try:
            public = _b64decode(str(signing_key))
        except (ValueError, TypeError) as exc:
            raise LicenseError("EdDSA 公钥不是合法 base64") from exc
        if not ed25519.verify(public, signing_input, actual):
            raise LicenseError("许可证签名无效")
    exp = payload.get("exp")
    if expected_alg == PRODUCTION_ALG and exp is None:
        raise LicenseError("生产许可证缺少过期时间")
    if exp is not None:
        try:
            if isinstance(exp, bool) or not math.isfinite(float(exp)):
                raise LicenseError("许可证过期时间无效")
            if float(exp) <= float(now if now is not None else time.time()):
                raise LicenseError("许可证已过期")
        except (TypeError, ValueError) as exc:
            raise LicenseError("许可证过期时间无效") from exc
    if not isinstance(payload.get("sub"), str) or not payload["sub"]:
        raise LicenseError("许可证缺少账户标识")
    claimed_device = payload.get("device_id")
    if expected_alg == PRODUCTION_ALG and (not isinstance(claimed_device, str) or not claimed_device.strip()):
        raise LicenseError("生产许可证缺少设备绑定")
    if claimed_device and device_id is not None and str(claimed_device) != str(device_id):
        raise LicenseError("许可证与当前设备不匹配")
    return payload


def signed_max_concurrency(payload: Mapping[str, Any]) -> int:
    """Derive a bounded cap from an already verified license, never local flags.

    Old licenses without this claim continue to authenticate, but retain only
    the free cap until refreshed. A signed legacy cap above the new plan cap is
    bounded by the current 1/2/4 contract.
    """
    caps = {"free": 1, "standard": 2, "advanced": 4}
    value = payload.get("max_concurrency")
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return 1
    return min(value, caps.get(str(payload.get("plan_id") or "free"), 1))


def signed_beta_access(payload: Mapping[str, Any], *, now: float | None = None) -> dict[str, Any]:
    """Read a beta grant from an already verified account/device certificate.

    This never authenticates a payload itself. Callers must first verify EdDSA,
    expiry, current account and current device. Local flags and top-level
    ``unlimited_credits`` cannot grant access. Numeric credits stay separate.
    """
    denied = {"version": 1, "active": False, "unlimited_credits": False,
              "expires_at": None, "revoked": False}
    grant = payload.get("beta_access")
    if (not isinstance(grant, dict) or set(grant) != set(denied)
            or type(grant.get("version")) is not int or grant["version"] != 1
            or any(type(grant.get(key)) is not bool for key in ("active", "unlimited_credits", "revoked"))):
        return denied
    try:
        current = time.time() if now is None else float(now)
        if not math.isfinite(current):
            return denied
        expires = grant["expires_at"]
        if expires is not None:
            if not isinstance(expires, str) or len(expires) > 64:
                return denied
            parsed = datetime.fromisoformat(expires.replace("Z", "+00:00"))
            if parsed.tzinfo is None or not math.isfinite(parsed.timestamp()):
                return denied
            future = parsed.timestamp() > current
            expires = parsed.astimezone(timezone.utc).isoformat()
        else:
            future = True
        active = grant["active"] is True and grant["unlimited_credits"] is True and not grant["revoked"] and future
        return {**denied, "active": active, "unlimited_credits": active,
                "expires_at": expires, "revoked": grant["revoked"]}
    except (TypeError, ValueError, OverflowError):
        return denied
