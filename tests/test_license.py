# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""许可证签发/验证测试：签名往返、篡改、过期与算法边界。"""
from __future__ import annotations

import time

import pytest

from engine.license import LicenseError, sign_license, verify_license


def _payload(**overrides):
    payload = {
        "sub": "sub-accept-001",
        "plan_id": "standard",
        "period": "month",
        "monthly_agent_units": 200,
        "entitlements": ["transcribe", "basic_agent"],
        "exp": int(time.time()) + 3600,
    }
    payload.update(overrides)
    return payload


def test_sign_and_verify_roundtrip() -> None:
    token = sign_license(_payload(), signing_key="dev-key")
    decoded = verify_license(token, signing_key="dev-key")
    assert decoded["sub"] == "sub-accept-001"
    assert decoded["plan_id"] == "standard"
    assert decoded["monthly_agent_units"] == 200


def test_verify_rejects_wrong_key_and_tampered_payload() -> None:
    token = sign_license(_payload(), signing_key="dev-key")
    with pytest.raises(LicenseError):
        verify_license(token, signing_key="other-key")
    head, body, sig = token.split(".")
    import base64
    tampered_body = base64.urlsafe_b64encode(
        (base64.urlsafe_b64decode(body + "==").replace(b'"standard"', b'"super"'))
    ).rstrip(b"=").decode("ascii")
    with pytest.raises(LicenseError):
        verify_license(f"{head}.{tampered_body}.{sig}", signing_key="dev-key")


def test_verify_rejects_expired_license() -> None:
    token = sign_license(_payload(exp=int(time.time()) - 10), signing_key="dev-key")
    with pytest.raises(LicenseError, match="已过期"):
        verify_license(token, signing_key="dev-key")


def test_verify_rejects_malformed_and_missing_subject() -> None:
    with pytest.raises(LicenseError):
        verify_license("not-a-token", signing_key="dev-key")
    token = sign_license(_payload(sub=""), signing_key="dev-key")
    with pytest.raises(LicenseError, match="缺少账户标识"):
        verify_license(token, signing_key="dev-key")


def test_eddsa_sign_requires_a_real_seed() -> None:
    # 对称口径的密钥不是 Ed25519 种子：明确拒绝，而不是静默签出错误形态。
    with pytest.raises(LicenseError):
        sign_license(_payload(), signing_key="dev-key", alg="EdDSA")


def test_license_can_bind_to_device() -> None:
    token = sign_license(_payload(device_id="device-a"), signing_key="dev-key")
    decoded = verify_license(token, signing_key="dev-key", device_id="device-a")
    assert decoded["device_id"] == "device-a"
    with pytest.raises(LicenseError, match="设备不匹配"):
        verify_license(token, signing_key="dev-key", device_id="device-b")


def test_license_without_device_claim_stays_compatible() -> None:
    token = sign_license(_payload(), signing_key="dev-key")
    decoded = verify_license(token, signing_key="dev-key", device_id="device-a")
    assert decoded["plan_id"] == "standard"


# ---------------------------------------------------------------------------
# Ed25519 / EdDSA（生产形态）：RFC 8032 向量 + 许可证往返
# ---------------------------------------------------------------------------
def test_ed25519_rfc8032_vectors() -> None:
    from engine import ed25519

    seed = bytes.fromhex("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60")
    assert ed25519.public_key(seed).hex() == (
        "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a"
    )
    sig = ed25519.sign(seed, b"")
    assert sig.hex() == (
        "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e06522490155"
        "5fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b"
    )
    assert ed25519.verify(ed25519.public_key(seed), b"", sig)
    assert not ed25519.verify(ed25519.public_key(seed), b"x", sig)


def test_eddsa_license_roundtrip_and_tamper() -> None:
    import base64

    from engine import ed25519

    seed = ed25519.generate_seed()
    seed_b64 = base64.b64encode(seed).decode("ascii")
    pub_b64 = base64.b64encode(ed25519.public_key(seed)).decode("ascii")

    token = sign_license(_payload(), signing_key=seed_b64, alg="EdDSA")
    decoded = verify_license(token, signing_key=pub_b64)
    assert decoded["sub"] == "sub-accept-001"

    # 私钥不在手上（只有公钥）时无法签出可验证的许可证——伪造即拒。
    with pytest.raises(LicenseError):
        verify_license(token, signing_key=base64.b64encode(ed25519.public_key(ed25519.generate_seed())).decode("ascii"))
    head, body, sig = token.split(".")
    with pytest.raises(LicenseError):
        verify_license(f"{head}.{body[:-2]}aa.{sig}", signing_key=pub_b64)
    # 算法不混用：EdDSA 令牌不能用 HS256 密钥口径验，反之亦然。
    with pytest.raises(LicenseError):
        verify_license(token, signing_key="dev-key")
    hs_token = sign_license(_payload(), signing_key="dev-key")
    with pytest.raises(LicenseError):
        verify_license(hs_token, signing_key=pub_b64)


def test_eddsa_license_expiry_and_device_binding() -> None:
    import base64

    from engine import ed25519

    seed = ed25519.generate_seed()
    seed_b64 = base64.b64encode(seed).decode("ascii")
    pub_b64 = base64.b64encode(ed25519.public_key(seed)).decode("ascii")
    expired = sign_license(_payload(exp=int(time.time()) - 10), signing_key=seed_b64, alg="EdDSA")
    with pytest.raises(LicenseError):
        verify_license(expired, signing_key=pub_b64)
    bound = sign_license(_payload(device_id="device-a"), signing_key=seed_b64, alg="EdDSA")
    with pytest.raises(LicenseError):
        verify_license(bound, signing_key=pub_b64, device_id="device-b")
    assert verify_license(bound, signing_key=pub_b64, device_id="device-a")["device_id"] == "device-a"


def test_production_status_rejects_expired_or_forged_local_entitlements():
    from engine.subscription import status
    state = status({"subscription_plan":"advanced", "subscription_active":True,
        "subscription_license_public_key":"known-public", "license_token":"forged", "credit_balance":999999})
    assert not state["active"]
    assert state["plan_id"] == "free"
    assert state["credit_balance"] == 0
