# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""Offline application entry from a verified, device-bound account license."""
from __future__ import annotations

from typing import Any, Mapping
import hashlib
import hmac

from engine.license import LicenseError, verify_license


class AccountLoginRequired(RuntimeError):
    """A new work request needs the application's verified account."""
    code = "login_required"

    def __init__(self):
        super().__init__("请先登录 FoilPage，登录后继续")


def verified_account_access(settings: Mapping[str, Any], base_dir, *, now: float | None = None) -> dict:
    """Recompute entry from the signed license and this installation's device."""
    current_device = ""
    if str(settings.get("account_login_token") or "").strip() and base_dir is not None:
        from engine.device import device_id
        try:
            current_device = device_id(str(base_dir))
        except (OSError, TypeError, ValueError):
            pass
    return account_access(settings, device_id=current_device, now=now)


def require_account_access(settings: Mapping[str, Any], base_dir, *, now: float | None = None) -> dict:
    access = verified_account_access(settings, base_dir, now=now)
    if not access["allowed"]:
        raise AccountLoginRequired()
    return access


def account_access(settings: Mapping[str, Any], *, device_id: str = "", now: float | None = None) -> dict:
    """Keep a valid login across launches without trusting editable local flags.

    This is a local entry check, not proof that a remote session is unrevoked.
    The existing background refresh verifies remote revocation. Paid capabilities
    remain guarded by their separate signed entitlements and server ledger.
    """
    denied = {"required": True, "allowed": False, "expires_at": None}
    if not str(settings.get("account_login_token") or "").strip():
        return {**denied, "reason": "login_required"}
    public_key = str(settings.get("subscription_license_public_key") or "").strip()
    user_id = str(settings.get("account_user_id") or "").strip()
    if not public_key or not user_id or not device_id:
        return {**denied, "reason": "verification_required"}
    try:
        entry = str(settings.get('account_access_token') or '')
        payload = verify_license(entry or str(settings.get("license_token") or ""),
            signing_key=public_key, expected_alg="EdDSA", device_id=device_id, now=now)
        if payload.get("sub") != user_id or payload.get("device_id") != device_id:
            raise LicenseError("账户或设备不匹配")
        if entry:
            session_hash = 'sha256:' + hashlib.sha256(('shengjian-auth-v1:' + str(settings['account_login_token'])).encode()).hexdigest()
            if payload.get('kind') != 'account_access' or not isinstance(payload.get('session_hash'), str) or not hmac.compare_digest(payload['session_hash'], session_hash):
                raise LicenseError('登录凭证与当前会话不匹配')
    except (LicenseError, TypeError, ValueError):
        return {**denied, "reason": "verification_required"}
    return {"required": True, "allowed": True, "expires_at": float(payload["exp"]), "reason": "verified"}
