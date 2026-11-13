# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""Runtime location accuracy and secret exclusion for the Agent guide."""
import json
from unittest import mock

from app.agent_setup_guide import build_agent_setup_guide


def _snapshot(result):
    assert result["ok"] is True
    return json.loads(result["markdown"].split("```json\n", 1)[1].split("\n```", 1)[0])


def test_source_guide_uses_actual_checkout_and_custom_data_without_writes(tmp_path):
    source = tmp_path / "new checkout"
    data = tmp_path / "custom data"
    guide = tmp_path / "guide.md"
    guide.write_text("# 配置步骤\n\n保留原值，验证并回滚。", encoding="utf-8")
    before = sorted(tmp_path.rglob("*"))
    result = build_agent_setup_guide({"output_dir": str(tmp_path / "article output"),
                                      "download_dir": str(tmp_path / "videos"),
                                      "database_path": str(tmp_path / "library elsewhere.db"),
                                      "model_dir": str(tmp_path / "models")},
                                     data_dir=data, app_version="9.8.7", packaged=False,
                                     executable=tmp_path / ".venv" / "python.exe", source_root=source, guide_path=guide)
    snapshot = _snapshot(result)
    assert snapshot["source_root"] == str(source.resolve())
    assert snapshot["app_version"] == "9.8.7"
    assert snapshot["mode"] == "源码开发版"
    assert snapshot["paths"]["settings_file"] == str(data / "settings.json")
    assert snapshot["paths"]["database_path"] == str(tmp_path / "library elsewhere.db")
    assert snapshot["paths"]["model_cache_dir"] == str(tmp_path / "models")
    assert sorted(tmp_path.rglob("*")) == before


def test_packaged_guide_reads_bundled_document_and_uses_executable_location(tmp_path, monkeypatch):
    bundle = tmp_path / "_internal"
    (bundle / "docs").mkdir(parents=True)
    (bundle / "docs" / "agent-setup.md").write_text("安装用户无需 Python 或 Node。", encoding="utf-8")
    monkeypatch.setattr("app.agent_setup_guide.sys._MEIPASS", str(bundle), raising=False)
    result = build_agent_setup_guide({}, data_dir=tmp_path / "data", app_version="0.17.0", packaged=True,
                                     executable=tmp_path / "installed" / "FoilPage-Desktop.exe")
    snapshot = _snapshot(result)
    assert snapshot["mode"] == "安装版"
    assert snapshot["installation_dir"] == str(tmp_path / "installed")
    assert snapshot["source_root"] == "安装版不含可编辑开发源码"
    assert "安装用户无需 Python 或 Node" in result["markdown"]


def test_secret_fields_are_not_even_read_and_urls_remove_credentials(tmp_path):
    class SafeSettings(dict):
        def get(self, key, default=None):
            assert key not in {"agent_api_key", "local_api_token", "license_token", "account_login_token",
                               "account_pending_login_token", "subscription_verify_key", "cookie", "account_display_name"}
            return super().get(key, default)

    settings = SafeSettings(agent_api_key="never-read-provider-key", local_api_token="never-read-token",
                            cookie="never-read-cookie", account_login_token="never-read-login",
                            agent_base_url="https://user:password@api.example/v1?api_key=never-read-query#secret-fragment",
                            subscription_server_url="https://pay.example/token/never-read-path",
                            title_model="sk-never-read-model", agent_model="mimo-v2.5", agent_enabled=True)
    result = build_agent_setup_guide(settings, data_dir=tmp_path, app_version="test")
    snapshot = _snapshot(result)
    assert snapshot["configuration"]["agent_base_url"] == "https://api.example/v1"
    assert snapshot["configuration"]["agent_enabled"] is True
    assert snapshot["configuration"]["agent_model"] == "mimo-v2.5"
    for secret in ("never-read", "password", "secret-fragment"):
        assert secret not in result["markdown"]


def test_model_cache_and_desktop_log_paths_follow_current_runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("HF_HOME", str(tmp_path / "hf cache"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "profile"))
    snapshot = _snapshot(build_agent_setup_guide({}, data_dir=tmp_path / "separate data", app_version="test"))
    assert snapshot["paths"]["model_cache_dir"] == str(tmp_path / "hf cache")
    assert snapshot["paths"]["worker_log_dir"] == str(tmp_path / "separate data" / "logs")
    assert snapshot["paths"]["desktop_log_dir"] == str(tmp_path / "profile" / "FoilPage" / "logs")


def test_missing_or_empty_document_fails_without_inventing_content(tmp_path):
    missing = tmp_path / "missing.md"
    result = build_agent_setup_guide({}, data_dir=tmp_path, app_version="test", guide_path=missing)
    assert result["ok"] is False and "markdown" not in result
    missing.write_text("", encoding="utf-8")
    assert build_agent_setup_guide({}, data_dir=tmp_path, app_version="test", guide_path=missing)["ok"] is False


def test_invalid_public_values_are_not_exported_verbatim(tmp_path):
    snapshot = _snapshot(build_agent_setup_guide({"device": {"unsafe": "value"}, "local_api_port": True,
                                                  "agent_timeout_seconds": float("nan"), "agent_model": "<script>boom</script>"},
                                                 data_dir=tmp_path, app_version="test"))
    assert snapshot["configuration"]["device"] == "未配置或需检查"
    assert snapshot["configuration"]["local_api_port"] == "端口需检查"
    assert snapshot["configuration"]["agent_timeout_seconds"] == "超时需检查"
    assert snapshot["configuration"]["agent_model"] == "名称需在本机检查"


def test_guide_generation_does_not_launch_probes_or_inspect_settings_file(tmp_path):
    with mock.patch("subprocess.run", side_effect=AssertionError("no subprocess")):
        result = build_agent_setup_guide({}, data_dir=tmp_path / "nonexistent", app_version="test")
    assert result["ok"] is True
    assert not (tmp_path / "nonexistent").exists()
    assert "apply_setup_fixes" in result["markdown"]
    assert "未实测" in result["markdown"]
