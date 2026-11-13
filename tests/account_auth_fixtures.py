# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""Real, device-bound EdDSA accounts for isolated local tests only.

The fixed seed is public fixture material, never a service credential.  Callers
must use a test-owned data directory so this cannot modify a real device ID.
"""
from __future__ import annotations

import base64
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

from engine import ed25519
from engine.device import device_id
from engine.license import sign_license


def grant_account(target, base_dir, *, user_id: str = "fixture-user",
                  device: str | None = None, expires_at: float | None = None) -> dict:
    """Add a real free-account certificate to a dict or SettingsStore.

    Other business settings remain intact.  A noncurrent ``device`` or expired
    ``expires_at`` deliberately produces a signed denial fixture.
    """
    seed = bytes(range(32))
    current_device = device if device is not None else device_id(str(Path(base_dir)))
    now = int(time.time())
    claims = {"sub": user_id, "device_id": current_device, "iat": now,
              "exp": now + 3600 if expires_at is None else expires_at,
              "plan_id": "free", "credit_balance": 0, "entitlements": [],
              "software_access": {"version": 1, "trial_started_at": datetime.fromtimestamp(now, timezone.utc).isoformat(),
                "trial_expires_at": (datetime.fromtimestamp(now, timezone.utc) + timedelta(days=15)).isoformat(),
                "unlocked": False, "review": False}}
    patch = {"account_login_token": "fixture-full-session",
             "account_user_id": user_id,
             "subscription_license_public_key": base64.b64encode(ed25519.public_key(seed)).decode("ascii"),
             "license_token": sign_license(claims, signing_key=base64.b64encode(seed).decode("ascii"), alg="EdDSA")}
    if hasattr(target, "load") and hasattr(target, "update"):
        return target.update(**patch)
    target.update(patch)
    return target
