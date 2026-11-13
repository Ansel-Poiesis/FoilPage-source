# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""客户端热切换配置测试：canonical 跨语言一致、封闭 schema、Ed25519 验签与桌面缓存降级。

Worker 端对应用例：payment_service/cloudflare/tests/client-config.test.mjs——
两端钉同一条 canonical 向量，保证签名输入逐字节一致。
"""
from __future__ import annotations

import base64
import copy
import json
from pathlib import Path

import pytest

from engine import ed25519
from engine.client_config import (
    DEFAULT_CLIENT_CONFIG,
    ClientConfigError,
    canonical_json,
    validate_schema,
    verify_payload,
)

# 与 Worker 测试同源：Python json.dumps(sort_keys, separators, ensure_ascii=False) 产出。
CANONICAL_FIXTURE = {
    "version": 7,
    "min_app_version": "1.1.0",
    "generated_at": "2026-10-21T00:00:00Z",
    "config": {
        "douyin_gallery": {
            "image_cdn_suffixes": ["douyinpic.com", "bytecdn.cn"],
            "images_field_names": ["images", "图集"],
            "watermark_markers": ["~watermark"],
            "max_images_per_task": 40,
        },
        "flags": {"covers_enabled": True, "douyin_gallery_enabled": False},
    },
}
CANONICAL_VECTOR = '{"config":{"douyin_gallery":{"image_cdn_suffixes":["douyinpic.com","bytecdn.cn"],"images_field_names":["images","图集"],"max_images_per_task":40,"watermark_markers":["~watermark"]},"flags":{"covers_enabled":true,"douyin_gallery_enabled":false}},"generated_at":"2026-10-21T00:00:00Z","min_app_version":"1.1.0","version":7}'


def _test_keypair() -> tuple[str, str]:
    seed = ed25519.generate_seed()
    return base64.b64encode(seed).decode("ascii"), base64.b64encode(ed25519.public_key(seed)).decode("ascii")


def _signed_payload(seed_b64: str, public_b64: str, *, version: int = 2,
                    min_app_version: str = "1.1.0", config: dict | None = None,
                    generated_at: str = "2026-10-21T08:00:00Z") -> dict:
    # 深拷贝默认值：篡改类用例会改写 config.flags，按引用共享会污染
    # 模块级 DEFAULT_CLIENT_CONFIG（曾令后续会话内 covers_enabled 误为 True）。
    config = copy.deepcopy(config or DEFAULT_CLIENT_CONFIG)
    body = {"version": version, "min_app_version": min_app_version,
            "generated_at": generated_at, "config": config}
    signature = ed25519.sign(base64.b64decode(seed_b64), canonical_json(body).encode("utf-8"))
    return {"ok": True, **body, "signature": base64.b64encode(signature).decode("ascii")}


def _config_with(**overrides) -> dict:
    config = copy.deepcopy(DEFAULT_CLIENT_CONFIG)
    for key, value in overrides.items():
        if "." in key:
            section, field = key.split(".", 1)
            config[section][field] = value
        else:
            config[key] = value
    return config


# ------------------------------------------------------------------ engine
def test_canonical_matches_pinned_cross_language_vector() -> None:
    assert canonical_json(CANONICAL_FIXTURE) == CANONICAL_VECTOR


def test_default_config_passes_closed_schema() -> None:
    assert validate_schema(DEFAULT_CLIENT_CONFIG) is not None
    bad_configs = [
        {**DEFAULT_CLIENT_CONFIG, "extra": 1},
        {"flags": DEFAULT_CLIENT_CONFIG["flags"]},
        _config_with(**{"flags": {"covers_enabled": True}}),
        _config_with(**{"flags.covers_enabled": "yes"}),
        _config_with(**{"douyin_gallery.max_images_per_task": 1.5}),
        _config_with(**{"douyin_gallery.max_images_per_task": True}),
        _config_with(**{"douyin_gallery.image_cdn_suffixes": ["ok", "x" * 129]}),
        _config_with(**{"douyin_gallery.watermark_markers": ["bad\x1bchar"]}),
        _config_with(**{"douyin_gallery.images_field_names": []}),
        None,
    ]
    for bad in bad_configs:
        with pytest.raises(ClientConfigError):
            validate_schema(bad)


def test_verify_payload_roundtrip_and_rejections() -> None:
    seed_b64, public_b64 = _test_keypair()
    payload = _signed_payload(seed_b64, public_b64)
    verified = verify_payload(payload, public_b64)
    assert verified["version"] == 2
    assert verified["config"]["flags"]["douyin_gallery_enabled"] is False

    rejects = [
        {**payload, "signature": base64.b64encode(b"x" * 64).decode("ascii")},  # 坏签名
        {**payload, "config": {"flags": payload["config"]["flags"]}},  # 缺 douyin_gallery
        {**payload, "ok": False},
        {**payload, "unexpected": True},
        {**payload, "version": "2"},
        {k: v for k, v in payload.items() if k != "signature"},
    ]
    for bad in rejects:
        with pytest.raises(ClientConfigError):
            verify_payload(bad, public_b64)


def test_tampered_config_fails_signature() -> None:
    seed_b64, public_b64 = _test_keypair()
    payload = _signed_payload(seed_b64, public_b64)
    tampered = json.loads(json.dumps(payload))
    tampered["config"]["flags"]["covers_enabled"] = True
    with pytest.raises(ClientConfigError):
        verify_payload(tampered, public_b64)


def test_fetch_config_rejects_non_https_and_oversize(monkeypatch: pytest.MonkeyPatch) -> None:
    import urllib.request

    from engine import client_config as module

    with pytest.raises(ClientConfigError):
        module.fetch_config("http://pay.example.com")
    with pytest.raises(ClientConfigError):
        module.fetch_config("")

    class FakeResponse:
        def __init__(self, size: int) -> None:
            self._size = size

        def read(self, limit: int = -1) -> bytes:
            return b"x" * self._size

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda request, timeout: FakeResponse(300_000))
    with pytest.raises(ClientConfigError):
        module.fetch_config("https://pay.example.com")


# -------------------------------------------------------------------- app
def _make_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, app_version: str = "1.1.0",
                payload_specs: list[dict | Exception] | None = None,
                cache_text: str | None = None, pair: tuple[str, str] | None = None):
    """接好假 fetch/verify 的存储；payload_specs 是签名覆写参数（或抛出的异常）。"""
    import app.client_config as store_module

    seed_b64, public_b64 = pair or _test_keypair()
    if cache_text is not None:
        (tmp_path / "client-config.json").write_text(cache_text, encoding="utf-8")

    def make_payload(**overrides):
        kwargs = {"version": 2, "min_app_version": "1.1.0",
                  "config": DEFAULT_CLIENT_CONFIG,
                  "generated_at": "2026-10-21T08:00:00Z"}
        kwargs.update(overrides)
        return _signed_payload(seed_b64, public_b64, **kwargs)

    specs = list(payload_specs or [])

    def fake_fetch(base_url: str, timeout: float = 12.0):
        if not specs:
            raise OSError("offline")
        spec = specs.pop(0)
        if isinstance(spec, Exception):
            raise spec
        return make_payload(**spec)

    monkeypatch.setattr(store_module, "fetch_config", fake_fetch)
    monkeypatch.setattr(store_module, "verify_payload",
                        lambda payload: verify_payload(payload, public_b64))
    store = store_module.ClientConfigStore(
        tmp_path, server_url=lambda: "https://pay.example.com", app_version=app_version)
    return store, make_payload


def test_store_starts_with_defaults_and_survives_offline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store, _ = _make_store(tmp_path, monkeypatch)
    snapshot = store.snapshot()
    assert snapshot["version"] == 0
    assert snapshot["source"] == "default"
    assert store.refresh_once() is False  # 离线：保留默认
    assert store.snapshot() == snapshot


def test_store_applies_verified_config_and_persists_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fixed_pair = _test_keypair()
    store, _ = _make_store(tmp_path, monkeypatch, pair=fixed_pair, payload_specs=[{
        "version": 4,
        "config": {"flags": {"covers_enabled": True, "douyin_gallery_enabled": False},
                   "douyin_gallery": DEFAULT_CLIENT_CONFIG["douyin_gallery"]},
    }])
    assert store.refresh_once() is True
    snapshot = store.snapshot()
    assert snapshot["version"] == 4
    assert snapshot["source"] == "server"
    assert snapshot["config"]["flags"]["covers_enabled"] is True
    # 缓存是完整签名响应：新实例（同一公钥）加载时重验签通过
    store2, _ = _make_store(tmp_path, monkeypatch, pair=fixed_pair)
    assert store2.snapshot()["version"] == 4
    assert store2.snapshot()["source"] == "cache"


def test_store_ignores_older_versions_and_min_app_version_gate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store, _ = _make_store(tmp_path, monkeypatch, payload_specs=[
        {"version": 5},
        {"version": 3},                              # 版本单调：旧版本被拒
        {"version": 9, "min_app_version": "1.9.9"},  # 版本门槛：本机 1.1.0 被拒
    ])
    assert store.refresh_once() is True
    assert store.refresh_once() is False
    assert store.snapshot()["version"] == 5
    assert store.refresh_once() is False
    assert store.snapshot()["version"] == 5


def test_source_version_reset_retains_signed_config_capability_gate(tmp_path, monkeypatch):
    from app.client_config import CLIENT_CONFIG_COMPATIBILITY_VERSION

    store, _ = _make_store(tmp_path, monkeypatch,
                          app_version=CLIENT_CONFIG_COMPATIBILITY_VERSION,
                          payload_specs=[{"version": 2, "min_app_version": "1.1.0"},
                                         {"version": 3, "min_app_version": "1.9.9"}])
    assert store.refresh_once() is True
    assert store.snapshot()["version"] == 2
    assert store.refresh_once() is False
    assert store.snapshot()["version"] == 2


def test_store_rejects_bad_signature_and_keeps_previous(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store, make_payload = _make_store(tmp_path, monkeypatch, payload_specs=[{"version": 5}])
    assert store.refresh_once() is True

    bad = make_payload(version=6)
    bad["signature"] = base64.b64encode(b"bad" * 21).decode("ascii")

    specs = [bad]
    import app.client_config as store_module

    monkeypatch.setattr(store_module, "fetch_config",
                        lambda base_url, timeout=12.0: specs.pop(0))
    assert store.refresh_once() is False
    assert store.snapshot()["version"] == 5  # 坏签名保留旧值


def test_store_tampered_cache_falls_back_to_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pair = _test_keypair()
    _, make_payload = _make_store(tmp_path, monkeypatch, pair=pair)
    payload = make_payload(version=6)
    payload["config"]["flags"]["covers_enabled"] = True  # 篡改后签名不再匹配
    store2, _ = _make_store(tmp_path, monkeypatch, pair=pair,
                            cache_text=json.dumps(payload, ensure_ascii=False))
    assert store2.snapshot()["version"] == 0  # 篡改缓存 → 回退默认
