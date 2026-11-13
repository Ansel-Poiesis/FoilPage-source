# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""Application settings location, migration, validation and atomic-write tests."""
from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine.settings import (  # noqa: E402
    CURRENT_SETTINGS_VERSION,
    DEFAULT_SUBSCRIPTION_LICENSE_PUBLIC_KEY,
    DEFAULT_SUBSCRIPTION_SERVER_URL,
    SettingsStore,
    SettingsValidationError,
    default_app_data_dir,
    migrate_legacy_data_root,
)


def test_default_path_uses_localappdata_without_poiesis() -> None:
    with tempfile.TemporaryDirectory(prefix="settings_root_") as temp:
        app_dir = default_app_data_dir({"LOCALAPPDATA": temp})
        assert app_dir == Path(temp) / "FoilPage"
        assert "poiesis" not in str(app_dir).lower()
        store = SettingsStore(environment={"LOCALAPPDATA": temp})
        settings = store.load()
        assert settings["device"] == "cuda"
        assert settings["font_scale"] == 1.15
        assert settings["download_rate_limit_kib"] == 0
        assert settings["download_min_free_gib"] == 5.0
        assert Path(settings["database_path"]).parent == app_dir
        assert Path(settings["output_dir"]).parent == app_dir
        assert Path(settings["download_dir"]).parent == app_dir
        assert settings["local_api_enabled"] is True
        assert settings["local_api_port"] == 18765
        assert settings["close_to_tray"] is True
        assert Path(settings["browser_download_dir"]).name == "Downloads"
        assert settings["delete_video_after_transcribe"] is False
        assert settings["delete_process_files_after_transcribe"] is False


def test_save_is_atomic_and_roundtrips() -> None:
    with tempfile.TemporaryDirectory(prefix="settings_save_") as temp:
        store = SettingsStore(base_dir=temp)
        settings = store.load()
        settings["whisper_model"] = "small"
        settings["language"] = "auto"
        settings["font_scale"] = 1.2
        store.save(settings)
        loaded = store.load()
        assert loaded["whisper_model"] == "small"
        assert loaded["language"] == "auto"
        assert loaded["font_scale"] == 1.2
        assert loaded["schema_version"] == CURRENT_SETTINGS_VERSION
        assert not list(Path(temp).glob(".settings-*.tmp"))


def test_legacy_keys_are_migrated() -> None:
    with tempfile.TemporaryDirectory(prefix="settings_migrate_") as temp:
        path = Path(temp) / "settings.json"
        path.write_text(
            json.dumps({
                "model": "base", "lang": "en", "poll": 4,
                "douyin_browser_session_enabled": True,
            }),
            encoding="utf-8",
        )
        store = SettingsStore(base_dir=temp)
        migrated = store.load()
        assert migrated["schema_version"] == CURRENT_SETTINGS_VERSION
        assert migrated["whisper_model"] == "base"
        assert migrated["language"] == "en"
        assert migrated["poll_seconds"] == 4.0
        assert "model" not in migrated and "lang" not in migrated
        assert "douyin_browser_session_enabled" not in migrated


def test_retired_light_and_system_themes_are_migrated() -> None:
    with tempfile.TemporaryDirectory(prefix="settings_theme_migrate_") as temp:
        store = SettingsStore(base_dir=temp)
        store.path.parent.mkdir(parents=True, exist_ok=True)

        legacy_light = store.defaults()
        legacy_light.update({"schema_version": 11, "theme": "light"})
        store.path.write_text(json.dumps(legacy_light), encoding="utf-8")
        assert store.load()["theme"] == "paper"

        legacy_system = store.defaults()
        legacy_system.update({"schema_version": 11, "theme": "system"})
        store.path.write_text(json.dumps(legacy_system), encoding="utf-8")
        assert store.load()["theme"] == "dark"

        invalid = store.load()
        invalid["theme"] = "light"
        with pytest.raises(SettingsValidationError):
            store.save(invalid)


def test_v1_local_api_port_is_migrated_away_from_conflicting_service() -> None:
    with tempfile.TemporaryDirectory(prefix="settings_port_migrate_") as temp:
        store = SettingsStore(base_dir=temp)
        legacy = store.defaults()
        legacy.update({"schema_version": 1, "local_api_port": 8765})
        store.path.parent.mkdir(parents=True, exist_ok=True)
        store.path.write_text(json.dumps(legacy), encoding="utf-8")
        migrated = store.load()
        assert migrated["schema_version"] == CURRENT_SETTINGS_VERSION
        assert migrated["local_api_port"] == 18765


def test_invalid_settings_are_rejected_without_overwrite() -> None:
    with tempfile.TemporaryDirectory(prefix="settings_invalid_") as temp:
        store = SettingsStore(base_dir=temp)
        valid = store.load()
        store.save(valid)
        before = store.path.read_bytes()
        invalid = dict(valid)
        invalid["device"] = "quantum"
        try:
            store.save(invalid)
        except SettingsValidationError:
            pass
        else:
            raise AssertionError("非法 device 应被拒绝")
        assert store.path.read_bytes() == before


def test_cuda_device_setting_roundtrips() -> None:
    with tempfile.TemporaryDirectory(prefix="settings_cuda_") as temp:
        store = SettingsStore(base_dir=temp)
        saved = store.update(device="cuda")
        assert saved["device"] == "cuda"
        assert store.load()["device"] == "cuda"


def test_custom_agent_profiles_roundtrip_and_validate() -> None:
    with tempfile.TemporaryDirectory(prefix="settings_custom_agent_") as temp:
        store = SettingsStore(base_dir=temp)
        profiles = [
            {
                "id": "movie",
                "name": "电影解说",
                "system_prompt": "你是电影解说员。",
                "enabled": True,
                "model": "deepseek-reasoner",
                "temperature": 0.1,
            }
        ]
        saved = store.update(agent_custom_profiles=profiles, agent_profile="custom:movie")
        assert saved["agent_custom_profiles"] == profiles
        assert store.load()["agent_custom_profiles"] == profiles

        cases = [
            [{"id": "", "name": "A", "system_prompt": "p", "enabled": True}],
            [{"id": "a", "name": "", "system_prompt": "p", "enabled": True}],
            [{"id": "a", "name": "A", "system_prompt": "", "enabled": True}],
            [{"id": "a", "name": "A", "system_prompt": "p", "enabled": "yes"}],
            [{"id": "a", "name": "A", "system_prompt": "p", "enabled": True, "temperature": 3}],
            [{"id": "a", "name": "A", "system_prompt": "p", "enabled": True, "model": 5}],
            [{"id": "a", "name": "A", "system_prompt": "p", "enabled": True}, {"id": "a", "name": "B", "system_prompt": "q", "enabled": True}],
        ]
        for profiles in cases:
            invalid = store.load()
            invalid["agent_custom_profiles"] = profiles
            with pytest.raises(SettingsValidationError):
                store.save(invalid)


def test_custom_agent_profile_key_validation() -> None:
    with tempfile.TemporaryDirectory(prefix="settings_custom_profile_key_") as temp:
        store = SettingsStore(base_dir=temp)
        valid = store.load()
        valid["agent_custom_profiles"] = [{"id": "x", "name": "X", "system_prompt": "p", "enabled": True}]
        valid["agent_profile"] = "custom:x"
        store.save(valid)

        invalid = store.load()
        invalid["agent_profile"] = "custom:"
        with pytest.raises(SettingsValidationError):
            store.save(invalid)
        invalid = store.load()
        invalid["agent_profile"] = "unknown_style"
        with pytest.raises(SettingsValidationError):
            store.save(invalid)


def test_delete_source_after_translation_defaults_off_and_roundtrips() -> None:
    with tempfile.TemporaryDirectory(prefix="settings_delete_source_") as temp:
        store = SettingsStore(base_dir=temp)
        assert store.load()["delete_source_after_translation"] is False
        assert store.update(delete_source_after_translation=True)["delete_source_after_translation"] is True
        invalid = store.load()
        invalid["delete_source_after_translation"] = "yes"
        with pytest.raises(SettingsValidationError):
            store.save(invalid)


def test_ci_runner_wakeup_defaults_off_and_roundtrips() -> None:
    with tempfile.TemporaryDirectory(prefix="settings_ci_runner_") as temp:
        store = SettingsStore(base_dir=temp)
        assert store.load()["ci_runner_wakeup"] is False
        assert store.update(ci_runner_wakeup=True)["ci_runner_wakeup"] is True
        invalid = store.load()
        invalid["ci_runner_wakeup"] = "yes"
        with pytest.raises(SettingsValidationError):
            store.save(invalid)


def test_download_limits_roundtrip_and_validate_ranges() -> None:
    with tempfile.TemporaryDirectory(prefix="settings_download_limits_") as temp:
        store = SettingsStore(base_dir=temp)
        saved = store.update(download_rate_limit_kib=2048, download_min_free_gib=8.5)
        assert saved["download_rate_limit_kib"] == 2048
        assert saved["download_min_free_gib"] == 8.5

        for field, value in (
            ("download_rate_limit_kib", -1),
            ("download_rate_limit_kib", 1.5),
            ("download_rate_limit_kib", True),
            ("download_min_free_gib", -0.1),
            ("download_min_free_gib", 1025),
            ("download_min_free_gib", False),
        ):
            invalid = store.load()
            invalid[field] = value
            with pytest.raises(SettingsValidationError):
                store.save(invalid)


def test_browser_download_directory_roundtrips_and_rejects_poiesis() -> None:
    with tempfile.TemporaryDirectory(prefix="settings_browser_download_") as temp:
        store = SettingsStore(base_dir=temp)
        selected = str(Path(temp) / "ChromeDownloads")
        assert store.update(browser_download_dir=selected)["browser_download_dir"] == selected
        invalid = store.load()
        invalid["browser_download_dir"] = "C:/Data/Poiesis/Downloads"
        with pytest.raises(SettingsValidationError):
            store.save(invalid)


def test_update_preserves_unknown_future_keys() -> None:
    with tempfile.TemporaryDirectory(prefix="settings_update_") as temp:
        store = SettingsStore(base_dir=temp)
        current = store.load()
        current["future_flag"] = True
        store.save(current)
        updated = store.update(language="zh", max_attempts=5)
        assert updated["future_flag"] is True
        assert updated["language"] == "zh"
        assert updated["max_attempts"] == 5


def test_future_version_and_poiesis_runtime_paths_are_rejected() -> None:
    with tempfile.TemporaryDirectory(prefix="settings_guard_") as temp:
        store = SettingsStore(base_dir=temp)
        future = store.defaults()
        future["schema_version"] = CURRENT_SETTINGS_VERSION + 1
        store.path.parent.mkdir(parents=True, exist_ok=True)
        store.path.write_text(json.dumps(future), encoding="utf-8")
        try:
            store.load()
        except Exception as exc:
            assert "高于当前支持版本" in str(exc)
        else:
            raise AssertionError("未来设置版本不应被静默降级")

        invalid = store.defaults()
        invalid["database_path"] = "C:/Data/Poiesis/library.db"
        try:
            store.save(invalid)
        except SettingsValidationError:
            pass
        else:
            raise AssertionError("运行数据库不得写入 Poiesis")

        for field in ("output_dir", "download_dir", "browser_download_dir"):
            invalid = store.defaults()
            invalid[field] = f"C:/Data/Poiesis/{field}"
            with pytest.raises(SettingsValidationError):
                store.save(invalid)


def test_token_is_persistent_and_concurrent_updates_do_not_clobber() -> None:
    with tempfile.TemporaryDirectory(prefix="settings_concurrent_") as temp:
        store = SettingsStore(base_dir=temp)
        tokens: list[str] = []
        token_barrier = threading.Barrier(6)

        def get_token() -> None:
            token_barrier.wait()
            tokens.append(store.ensure_local_api_token())

        workers = [threading.Thread(target=get_token) for _ in range(6)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()
        assert len(set(tokens)) == 1 and len(tokens[0]) >= 32
        assert store.load()["local_api_token"] == tokens[0]

        update_barrier = threading.Barrier(2)

        def update(field: str, value) -> None:
            update_barrier.wait()
            store.update(**{field: value})

        first = threading.Thread(target=update, args=("theme", "dark"))
        second = threading.Thread(target=update, args=("language", "en"))
        first.start()
        second.start()
        first.join()
        second.join()
        final = store.load()
        assert final["theme"] == "dark" and final["language"] == "en"


@pytest.mark.skipif(sys.platform != "win32", reason="DPAPI is Windows-only")
def test_secret_fields_are_encrypted_on_disk_and_roundtrip_in_memory() -> None:
    with tempfile.TemporaryDirectory(prefix="settings_dpapi_") as temp:
        store = SettingsStore(base_dir=temp)
        store.update(
            agent_api_key="sk-test-secret",
            local_api_token="local-token-secret",
        )
        raw = json.loads(store.path.read_text(encoding="utf-8"))
        assert raw["agent_api_key"].startswith("dpapi:v1:")
        assert raw["local_api_token"].startswith("dpapi:v1:")
        assert "sk-test-secret" not in store.path.read_text(encoding="utf-8")
        loaded = store.load()
        assert loaded["agent_api_key"] == "sk-test-secret"
        assert loaded["local_api_token"] == "local-token-secret"


@pytest.mark.skipif(sys.platform != "win32", reason="DPAPI is Windows-only")
def test_legacy_plaintext_secrets_are_migrated_on_load() -> None:
    with tempfile.TemporaryDirectory(prefix="settings_dpapi_migrate_") as temp:
        store = SettingsStore(base_dir=temp)
        legacy = store.defaults()
        legacy["schema_version"] = 7
        legacy["agent_api_key"] = "sk-legacy-secret"
        legacy["local_api_token"] = "legacy-token-secret"
        store.path.parent.mkdir(parents=True, exist_ok=True)
        store.path.write_text(json.dumps(legacy), encoding="utf-8")

        migrated = store.load()
        assert migrated["schema_version"] == CURRENT_SETTINGS_VERSION
        assert migrated["agent_api_key"] == "sk-legacy-secret"
        assert migrated["local_api_token"] == "legacy-token-secret"
        raw = json.loads(store.path.read_text(encoding="utf-8"))
        assert raw["agent_api_key"].startswith("dpapi:v1:")
        assert raw["local_api_token"].startswith("dpapi:v1:")
        assert "sk-legacy-secret" not in store.path.read_text(encoding="utf-8")


if __name__ == "__main__":
    test_default_path_uses_localappdata_without_poiesis()
    test_save_is_atomic_and_roundtrips()
    test_legacy_keys_are_migrated()
    test_invalid_settings_are_rejected_without_overwrite()
    test_update_preserves_unknown_future_keys()
    test_future_version_and_poiesis_runtime_paths_are_rejected()
    test_secret_fields_are_encrypted_on_disk_and_roundtrip_in_memory()
    test_legacy_plaintext_secrets_are_migrated_on_load()
    print("OK SettingsStore tests passed")


def test_new_preferences_defaults_and_roundtrip() -> None:
    with tempfile.TemporaryDirectory(prefix="prefs_") as temp:
        store = SettingsStore(base_dir=Path(temp) / "data")
        d = store.defaults()
        assert d["transcribe_after_download"] is False
        assert d["update_title_after_transcribe"] is True
        assert d["task_sort_order"] == "newest"

        saved = store.update(
            transcribe_after_download=True,
            update_title_after_transcribe=False,
            task_sort_order="oldest",
        )
        assert saved["transcribe_after_download"] is True
        assert saved["update_title_after_transcribe"] is False
        assert saved["task_sort_order"] == "oldest"

        loaded = SettingsStore(base_dir=Path(temp) / "data").load()
        assert loaded["transcribe_after_download"] is True
        assert loaded["update_title_after_transcribe"] is False
        assert loaded["task_sort_order"] == "oldest"


def test_invalid_task_sort_order_rejected() -> None:
    with tempfile.TemporaryDirectory(prefix="sort_invalid_") as temp:
        store = SettingsStore(base_dir=Path(temp) / "data")
        with pytest.raises(SettingsValidationError):
            store.update(task_sort_order="random")


def test_notification_sound_pack_roundtrip_and_validation() -> None:
    with tempfile.TemporaryDirectory(prefix="sound_pack_") as temp:
        store = SettingsStore(base_dir=Path(temp) / "data")
        d = store.defaults()
        assert d["notification_sound_pack"] == "system"
        assert d["notification_sound_success_path"] == ""
        assert d["notification_sound_error_path"] == ""

        saved = store.update(
            notification_sound_pack="custom",
            notification_sound_success_path="C:/sounds/done.wav",
            notification_sound_error_path="C:/sounds/fail.wav",
        )
        assert saved["notification_sound_pack"] == "custom"
        assert saved["notification_sound_success_path"] == "C:/sounds/done.wav"

        loaded = SettingsStore(base_dir=Path(temp) / "data").load()
        assert loaded["notification_sound_pack"] == "custom"
        assert loaded["notification_sound_error_path"] == "C:/sounds/fail.wav"

        with pytest.raises(SettingsValidationError):
            store.update(notification_sound_pack="bogus")
        with pytest.raises(SettingsValidationError):
            store.update(notification_sound_success_path=123)


def test_agent_hosted_model_roundtrip_and_validation() -> None:
    with tempfile.TemporaryDirectory(prefix="hosted_model_") as temp:
        store = SettingsStore(base_dir=Path(temp) / "data")
        assert store.defaults()["agent_hosted_model"] == ""
        saved = store.update(agent_hosted_model="deepseek-flash")
        assert saved["agent_hosted_model"] == "deepseek-flash"
        assert SettingsStore(base_dir=Path(temp) / "data").load()["agent_hosted_model"] == "deepseek-flash"
        assert store.update(agent_hosted_model="")["agent_hosted_model"] == ""
        with pytest.raises(SettingsValidationError):
            store.update(agent_hosted_model="deepseek-chat")


def test_github_theme_palette_roundtrips_and_unknown_theme_rejected() -> None:
    with tempfile.TemporaryDirectory(prefix="settings_themes_") as temp:
        store = SettingsStore(base_dir=temp)
        for theme in (
            "dracula", "tokyo-night", "nord", "mocha",
            "gruvbox", "rose-pine", "latte", "nord-light",
        ):
            assert store.update(theme=theme)["theme"] == theme
        # 旧主题保留，坏值仍拒绝
        assert store.update(theme="paper")["theme"] == "paper"
        with pytest.raises(SettingsValidationError):
            store.update(theme="windows98")


def test_subscription_contract_settings_defaults_and_roundtrip() -> None:
    with tempfile.TemporaryDirectory(prefix="settings_contract_") as temp:
        store = SettingsStore(base_dir=temp)
        defaults = store.defaults()
        assert defaults["subscription_server_url"] == DEFAULT_SUBSCRIPTION_SERVER_URL
        assert defaults["subscription_license_public_key"] == DEFAULT_SUBSCRIPTION_LICENSE_PUBLIC_KEY
        assert defaults["license_token"] == ""
        saved = store.update(
            subscription_server_url="https://api.example.com",
            license_token="abc.def.ghi",
        )
        assert saved["subscription_server_url"] == "https://api.example.com"
        loaded = SettingsStore(base_dir=temp).load()
        assert loaded["subscription_server_url"] == "https://api.example.com"
        assert loaded["license_token"] == "abc.def.ghi"


def test_fresh_profile_has_public_login_connection_without_account_or_disk_write(tmp_path) -> None:
    import base64

    store = SettingsStore(base_dir=tmp_path)
    settings = store.load()
    assert settings["subscription_server_url"] == "https://pay.vellumfeed.com"
    assert settings["subscription_license_public_key"] == "Giz/dVXfLK0gGUYhtSbERp+HEWl1e6PTlWqr7gQazh8="
    assert len(base64.b64decode(settings["subscription_license_public_key"], validate=True)) == 32
    assert settings["account_login_token"] == settings["account_user_id"] == settings["license_token"] == ""
    assert settings["subscription_verify_key"] == ""
    assert settings["subscription_active"] is False and settings["credit_balance"] == 0
    assert not store.path.exists()


@pytest.mark.parametrize("pair", [
    {},
    {"subscription_server_url": "", "subscription_license_public_key": ""},
    {"subscription_server_url": " \t", "subscription_license_public_key": "\n "},
    {"subscription_server_url": ""},
    {"subscription_license_public_key": ""},
])
def test_legacy_unconfigured_profile_gets_pair_once_and_preserves_preferences(tmp_path, pair) -> None:
    store = SettingsStore(base_dir=tmp_path)
    raw = {"schema_version": 16, "theme": "paper", "language": "en", **pair}
    store.path.write_text(json.dumps(raw), encoding="utf-8")
    loaded = store.load()
    assert loaded["subscription_server_url"] == DEFAULT_SUBSCRIPTION_SERVER_URL
    assert loaded["subscription_license_public_key"] == DEFAULT_SUBSCRIPTION_LICENSE_PUBLIC_KEY
    assert loaded["theme"] == "paper" and loaded["language"] == "en"
    assert loaded["schema_version"] == CURRENT_SETTINGS_VERSION
    before = store.path.read_bytes()
    assert store.load() == loaded
    assert store.path.read_bytes() == before
    assert not list(tmp_path.glob(".settings-*.tmp"))


@pytest.mark.parametrize("pair", [
    {"subscription_server_url": "https://custom.example.com"},
    {"subscription_license_public_key": "custom-public-key"},
    {"subscription_server_url": "https://custom.example.com", "subscription_license_public_key": "custom-public-key"},
    {"subscription_server_url": DEFAULT_SUBSCRIPTION_SERVER_URL},
    {"subscription_license_public_key": DEFAULT_SUBSCRIPTION_LICENSE_PUBLIC_KEY},
    {"subscription_server_url": DEFAULT_SUBSCRIPTION_SERVER_URL, "subscription_license_public_key": "custom-public-key"},
])
def test_legacy_partial_or_custom_connection_never_mixes_trust_pair(tmp_path, pair) -> None:
    store = SettingsStore(base_dir=tmp_path)
    store.path.write_text(json.dumps({"schema_version": 16, **pair}), encoding="utf-8")
    loaded = store.load()
    for field in ("subscription_server_url", "subscription_license_public_key"):
        assert loaded[field] == pair.get(field, "")
    assert store.load() == loaded


@pytest.mark.parametrize("association", [
    {"account_login_token": "synthetic-session"},
    {"account_pending_login_token": "synthetic-pending"},
    {"account_user_id": "synthetic-user"},
    {"license_token": "synthetic-license"},
    {"subscription_verify_key": "synthetic-local-key"},
    {"account_display_name": "Synthetic account"},
    {"account_avatar": "synthetic-avatar"},
    {"license_expires_at": "2099-01-01T00:00:00Z"},
    {"subscription_expires_at": "2099-01-01T00:00:00Z"},
    {"subscription_plan": "standard"},
    {"subscription_active": True},
    {"credit_balance": 500},
    {"subscription_last_order_id": "synthetic-order"},
    {"subscription_checkout_url": "https://custom.example.com/checkout"},
    {"agent_usage_period": "2026-10"},
    {"agent_usage_used": 1},
])
@pytest.mark.parametrize("pair", [{}, {"subscription_server_url": "", "subscription_license_public_key": ""}])
def test_unknown_existing_account_is_not_assigned_official_service(tmp_path, association, pair) -> None:
    store = SettingsStore(base_dir=tmp_path)
    store.path.write_text(json.dumps({"schema_version": 16, **association, **pair}), encoding="utf-8")
    loaded = store.load()
    assert loaded["subscription_server_url"] == loaded["subscription_license_public_key"] == ""
    for field, value in association.items():
        assert loaded[field] == value
    # Secret upgrades may encrypt synthetic fields, but must not change their
    # meaning or add a service URL on the next load.
    assert store.load() == loaded


def test_current_profile_can_deliberately_clear_connection_without_refill(tmp_path) -> None:
    store = SettingsStore(base_dir=tmp_path)
    store.update(subscription_server_url="", subscription_license_public_key="")
    before = store.path.read_bytes()
    loaded = store.load()
    assert loaded["subscription_server_url"] == loaded["subscription_license_public_key"] == ""
    assert store.path.read_bytes() == before


@pytest.mark.parametrize("bad", [123, None, False])
def test_invalid_legacy_service_value_is_not_repaired_or_overwritten(tmp_path, bad) -> None:
    store = SettingsStore(base_dir=tmp_path)
    store.path.write_text(json.dumps({"schema_version": 16, "subscription_server_url": bad}), encoding="utf-8")
    before = store.path.read_bytes()
    with pytest.raises(SettingsValidationError):
        store.load()
    assert store.path.read_bytes() == before


def test_onboarding_completion_marker_defaults_and_roundtrip() -> None:
    with tempfile.TemporaryDirectory(prefix="settings_onboarding_") as temp:
        store = SettingsStore(base_dir=temp)
        defaults = store.defaults()
        # 空串 = 从未读完引导：首次启动时 WebView 层据此展示新手引导。
        assert defaults["onboarding_completed_version"] == ""
        saved = store.update(onboarding_completed_version="0.11.0")
        assert saved["onboarding_completed_version"] == "0.11.0"
        assert SettingsStore(base_dir=temp).load()["onboarding_completed_version"] == "0.11.0"
        # 非字符串标记必须被拒绝，避免坏数据让引导永远不再出现/反复出现。
        with pytest.raises(SettingsValidationError):
            store.update(onboarding_completed_version=123)


@pytest.mark.skipif(sys.platform != "win32", reason="DPAPI is Windows-only")
def test_license_token_is_protected_on_disk() -> None:
    with tempfile.TemporaryDirectory(prefix="settings_license_secret_") as temp:
        store = SettingsStore(base_dir=temp)
        store.update(license_token="tok-123")
        raw = json.loads(store.path.read_text(encoding="utf-8"))
        assert raw["license_token"].startswith("dpapi:v1:")
        assert "tok-123" not in store.path.read_text(encoding="utf-8")
        assert store.load()["license_token"] == "tok-123"


def test_invalid_contract_settings_rejected() -> None:
    with tempfile.TemporaryDirectory(prefix="settings_contract_invalid_") as temp:
        store = SettingsStore(base_dir=temp)
        with pytest.raises(SettingsValidationError):
            store.update(subscription_server_url=123)
        with pytest.raises(SettingsValidationError):
            store.update(license_token=None)


# ------------------------------------------------------------- 升级维护改造

def test_v15_asr_and_download_tuning_settings_persist() -> None:
    with tempfile.TemporaryDirectory(prefix="settings_v15_tuning_") as temp:
        store = SettingsStore(base_dir=temp)
        saved = store.update(
            asr_initial_prompt="以下是播客语音，两位主讲人。",
            asr_hotwords="CTranslate2 Deno faster-whisper",
            asr_batched_enabled=False,
            js_runtime_path=r"C:\tools\deno.exe",
            allow_any_url=True,
        )
        assert saved["asr_initial_prompt"].startswith("以下是播客语音")
        assert saved["js_runtime_path"].endswith("deno.exe")
        reloaded = SettingsStore(base_dir=temp).load()
        assert reloaded["asr_batched_enabled"] is False
        assert reloaded["allow_any_url"] is True
        # 旧版设置文件（v14，无新键）加载后自动补默认值并落盘到当前版本。
        raw = json.loads(store.path.read_text(encoding="utf-8"))
        raw.pop("asr_hotwords")
        raw["schema_version"] = 14
        store.path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        migrated = store.load()
        assert migrated["asr_hotwords"] == ""
        assert migrated["schema_version"] == store.defaults()["schema_version"]
        # 类型防线：新键不接受坏类型。
        with pytest.raises(SettingsValidationError):
            store.update(asr_batched_enabled="yes")
        with pytest.raises(SettingsValidationError):
            store.update(js_runtime_path=123)


def test_default_path_falls_back_to_legacy_root_until_migrated() -> None:
    with tempfile.TemporaryDirectory(prefix="settings_root_") as temp:
        legacy = Path(temp) / "VideoToArticle"
        legacy.mkdir()
        assert default_app_data_dir({"LOCALAPPDATA": temp}) == legacy
        (Path(temp) / "FoilPage").mkdir()
        assert default_app_data_dir({"LOCALAPPDATA": temp}) == Path(temp) / "FoilPage"


@pytest.mark.skipif(sys.platform != "win32", reason="junction shim is Windows-only")
def test_migrate_legacy_data_root_renames_and_leaves_junction_shim() -> None:
    with tempfile.TemporaryDirectory(prefix="settings_root_") as temp:
        legacy = Path(temp) / "VideoToArticle"
        (legacy / "logs").mkdir(parents=True)
        (legacy / "library.db").write_bytes(b"db")
        assert migrate_legacy_data_root({"LOCALAPPDATA": temp}) is True
        current = Path(temp) / "FoilPage"
        assert (current / "library.db").read_bytes() == b"db"
        assert os.lstat(legacy).st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT
        # 回滚垫片：旧路径写入即落入新根，≤1.3.7 的旧版本仍能读写同一份数据。
        (legacy / "via-shim.txt").write_text("x", encoding="utf-8")
        assert (current / "via-shim.txt").read_text(encoding="utf-8") == "x"
        assert default_app_data_dir({"LOCALAPPDATA": temp}) == current
        assert migrate_legacy_data_root({"LOCALAPPDATA": temp}) is False


def test_migrate_legacy_data_root_skips_when_current_exists_or_no_legacy() -> None:
    with tempfile.TemporaryDirectory(prefix="settings_root_") as temp:
        assert migrate_legacy_data_root({"LOCALAPPDATA": temp}) is False
        (Path(temp) / "FoilPage").mkdir()
        (Path(temp) / "VideoToArticle").mkdir()
        assert migrate_legacy_data_root({"LOCALAPPDATA": temp}) is False


@pytest.mark.skipif(sys.platform != "win32", reason="Windows junction regression")
def test_current_profile_resolves_legacy_runtime_paths_to_same_data(tmp_path):
    import _winapi
    from engine.queue_store import QueueStore

    current = tmp_path / "FoilPage"
    current.mkdir()
    legacy = tmp_path / "VideoToArticle"
    _winapi.CreateJunction(str(current), str(legacy))
    store = SettingsStore(base_dir=current)
    settings = store.defaults()
    for field, suffix in (("database_path", "library.db"), ("output_dir", "output"),
                          ("download_dir", "downloads"), ("model_dir", "models")):
        settings[field] = str(legacy / suffix)
    settings["browser_download_dir"] = str(tmp_path / "UserDownloads")
    store.save(settings)
    migrated = store.load()
    for field in ("database_path", "output_dir", "download_dir", "model_dir"):
        assert Path(migrated[field]).is_relative_to(current)
    assert migrated["browser_download_dir"] == settings["browser_download_dir"]
    assert store.load() == migrated
    queue = QueueStore(str(legacy / "library.db"))
    try:
        assert Path(queue.db_path) == current / "library.db"
        assert (current / "library.db").is_file()
    finally:
        queue.close()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows file-handle lock semantics")
def test_migrate_returns_false_while_legacy_root_holds_open_file() -> None:
    """旧根内存在未 FILE_SHARE_DELETE 的打开句柄（生产案例：日志文件自锁）时，
    迁移必须失败不致命——升级后启动永远不能因迁移崩溃。句柄释放后下次重试成功。"""
    with tempfile.TemporaryDirectory(prefix="settings_root_") as temp:
        legacy = Path(temp) / "VideoToArticle"
        legacy.mkdir()
        (legacy / "logs").mkdir()
        handle = (legacy / "logs" / "desktop.log").open("a")
        try:
            assert migrate_legacy_data_root({"LOCALAPPDATA": temp}) is False
            assert legacy.is_dir()
            assert not (Path(temp) / "FoilPage").exists()
        finally:
            handle.close()
        assert migrate_legacy_data_root({"LOCALAPPDATA": temp}) is True


def test_v20_migration_clears_dead_mimo_names(tmp_path):
    """v19 存量：视觉死名清空；仍停在 MiMo BYOK 默认的主模型刷成继任名。"""
    import json as _json
    store = SettingsStore(base_dir=tmp_path)
    legacy = store.defaults()
    legacy.update(schema_version=19, agent_model="mimo-v2.5", agent_vision_model="mimo-v2.5")
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text(_json.dumps(legacy, ensure_ascii=False), encoding="utf-8")
    migrated = store.load()
    assert migrated["agent_vision_model"] == ""
    assert migrated["agent_model"] == "mimo-v2.6-flash"
    # 用户自选的其他模型名不受迁移影响
    custom = store.defaults()
    custom.update(schema_version=19, agent_provider="deepseek",
                  agent_model="deepseek-v4-flash", agent_vision_model="glm-4v-plus")
    store.path.write_text(_json.dumps(custom, ensure_ascii=False), encoding="utf-8")
    kept = store.load()
    assert kept["agent_model"] == "deepseek-v4-flash" and kept["agent_vision_model"] == "glm-4v-plus"
