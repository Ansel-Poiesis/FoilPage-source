# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
from __future__ import annotations

import json
import io
import sys
import tomllib
from pathlib import Path

import pytest

from agent import cli
from agent.client import AgentError


TASK = "job:123456abcdef"
REVISION = "a" * 64


@pytest.fixture
def recording_client(monkeypatch):
    calls = []

    class FakeClient:
        def call(self, method, params):
            calls.append((method, params))
            return {"accepted": True}

    monkeypatch.setattr(cli.AgentClient, "from_data_dir", lambda *_args, **_kwargs: FakeClient())
    return calls


def test_cli_edit_reads_utf8_file_and_keeps_revision(tmp_path: Path, recording_client, capsys):
    source = tmp_path / "正文.txt"
    source.write_bytes("本地正文\n第二行".encode("utf-8-sig"))
    exit_code = cli.main(["documents", "update", TASK, "--expected-revision", REVISION, "--input-file", str(source)])
    assert exit_code == 0
    assert json.loads(capsys.readouterr().out) == {"ok": True, "result": {"accepted": True}}
    assert recording_client == [("documents.update", {"task_id": TASK, "tab": "final", "expected_revision": REVISION, "text": "本地正文\n第二行"})]


def test_cli_explicit_retry_command_and_legacy_flag_both_dispatch(recording_client, capsys):
    assert cli.main(["tasks", "retry", TASK]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True
    assert recording_client == [("tasks.retry", {"task_id": TASK})]
    assert cli.main(["tasks", "retry", TASK, "--allow-processing"]) == 0
    assert recording_client[-1] == ("tasks.retry", {"task_id": TASK})


@pytest.mark.parametrize("arguments", [
    ["documents", "read", "../../settings.json"],
    ["documents", "update", TASK, "--expected-revision", "stale", "--input-file", "ignored.txt"],
    ["tasks", "list", "--limit", "201"],
    ["documents", "update-metadata", TASK, "--expected-revision", REVISION],
    ["documents", "update-metadata", TASK, "--expected-revision", REVISION, "--title", " "],
    ["info", "--token", "synthetic-sensitive-argument"],
])
def test_cli_invalid_arguments_return_json_without_connection_or_echo(arguments, recording_client, capsys):
    assert cli.main(arguments) == 2
    output = capsys.readouterr()
    assert json.loads(output.out)["ok"] is False
    assert "synthetic-sensitive-argument" not in output.out + output.err
    assert recording_client == []


def test_cli_partial_metadata_update_preserves_omitted_fields(recording_client, capsys):
    assert cli.main(["documents", "update-metadata", TASK, "--expected-revision", REVISION, "--title", "新标题"]) == 0
    assert recording_client == [("documents.update_metadata", {"task_id": TASK, "expected_revision": REVISION, "title": "新标题"})]
    capsys.readouterr()
    assert cli.main(["documents", "update-metadata", TASK, "--expected-revision", REVISION, "--clear-tags"]) == 0
    assert recording_client[-1][1]["tags"] == []


def test_cli_invalid_utf8_file_does_not_call_service(tmp_path: Path, recording_client, capsys):
    source = tmp_path / "invalid.txt"
    source.write_bytes(b"\xff\xfe\x00")
    assert cli.main(["documents", "update", TASK, "--expected-revision", REVISION, "--input-file", str(source)]) == 2
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "invalid_argument"
    assert recording_client == []


def test_cli_connection_errors_have_distinct_exit_code(monkeypatch, capsys):
    def missing(*_args, **_kwargs):
        raise AgentError("profile_missing", "未配置")
    monkeypatch.setattr(cli.AgentClient, "from_data_dir", missing)
    assert cli.main(["info"]) == 3
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "profile_missing"


def test_mcp_start_failure_uses_stderr_and_keeps_stdout_empty(monkeypatch, capsys):
    def missing(*_args, **_kwargs):
        raise AgentError("profile_missing", "未配置")
    monkeypatch.setattr(cli.AgentClient, "from_data_dir", missing)
    assert cli.main(["mcp"]) == 3
    output = capsys.readouterr()
    assert output.out == ""
    assert "profile_missing" in output.err


def test_mcp_start_failure_reconfigures_ascii_streams_to_strict_utf8(monkeypatch):
    stdout_bytes, stderr_bytes = io.BytesIO(), io.BytesIO()
    stdout = io.TextIOWrapper(stdout_bytes, encoding="ascii", errors="strict", write_through=True)
    stderr = io.TextIOWrapper(stderr_bytes, encoding="ascii", errors="strict", write_through=True)
    monkeypatch.setattr(sys, "stdout", stdout)
    monkeypatch.setattr(sys, "stderr", stderr)

    def missing(*_args, **_kwargs):
        raise AgentError("profile_missing", "尚未找到 FoilPage 配置，请先启动应用")
    monkeypatch.setattr(cli.AgentClient, "from_data_dir", missing)
    assert cli.main(["mcp"]) == 3
    assert stdout.encoding == stderr.encoding == "utf-8"
    assert stdout.errors == stderr.errors == "strict"
    assert stdout_bytes.getvalue() == b""
    assert stderr_bytes.getvalue().decode("utf-8").splitlines() == ["profile_missing: 尚未找到 FoilPage 配置，请先启动应用"]


def test_codex_config_reconfigures_ascii_output_without_initialization_noise(tmp_path, monkeypatch):
    stdout_bytes, stderr_bytes = io.BytesIO(), io.BytesIO()
    monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(stdout_bytes, encoding="ascii", write_through=True))
    monkeypatch.setattr(sys, "stderr", io.TextIOWrapper(stderr_bytes, encoding="ascii", write_through=True))
    directory = tmp_path / "FoilPage 资料"
    assert cli.main(["--data-dir", str(directory), "mcp-config", "--format", "codex"]) == 0
    output = stdout_bytes.getvalue().decode("utf-8")
    assert output.splitlines()[0] == "[mcp_servers.shengjian]"
    assert "FoilPage 资料" in output
    assert tomllib.loads(output)["mcp_servers"]["shengjian"]["args"][-1] == "mcp"
    assert stderr_bytes.getvalue() == b""


def test_mcp_config_does_not_read_profile_and_is_independent_of_cwd(tmp_path: Path, monkeypatch, capsys):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("config must not read credentials")
    monkeypatch.setattr(cli.AgentClient, "from_data_dir", forbidden)
    assert cli.main(["--data-dir", str(tmp_path), "mcp-config", "--allow-write"]) == 0
    result = json.loads(capsys.readouterr().out)["mcpServers"]["shengjian"]
    assert Path(result["command"]).is_absolute()
    assert Path(result["args"][0]).name == "main.py"
    assert Path(result["args"][0]).is_absolute()
    assert result["args"][-1] == "mcp"
    assert "env" not in result
    assert cli.main(["--data-dir", str(tmp_path), "mcp-config", "--format", "codex"]) == 0
    codex = tomllib.loads(capsys.readouterr().out)["mcp_servers"]["shengjian"]
    assert Path(codex["command"]).is_absolute()
    assert codex["args"][-1] == "mcp"


def test_config_conflicting_read_only_legacy_scope_is_rejected(capsys):
    assert cli.main(["mcp-config", "--read-only", "--allow-processing"]) == 2
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "invalid_argument"


def test_cli_application_arguments_read_from_utf8_file_not_commandline(tmp_path, recording_client, capsys):
    source = tmp_path / "设置.json"
    source.write_text(json.dumps({"changes": {"agent_system_prompt": "整理提示词", "agent_api_key": "private-key"},
                                 "expected_revision": REVISION}, ensure_ascii=False), encoding="utf-8")
    assert cli.main(["application", "call", "settings.update", "--input-file", str(source)]) == 0
    output = capsys.readouterr()
    assert "private-key" not in output.out + output.err
    assert recording_client[-1] == ("operations.call", {"operation": "settings.update", "arguments": {
        "changes": {"agent_system_prompt": "整理提示词", "agent_api_key": "private-key"}, "expected_revision": REVISION}, "confirm": False})


@pytest.mark.parametrize("body", ['{"name":"tiny","execute":"private-value"}', '{"name":"tiny","name":"base"}',
                                  '{"name":NaN}', '[]', '{"name":true}'])
def test_cli_strict_application_json_rejected_before_profile(tmp_path, recording_client, capsys, body):
    source = tmp_path / "invalid.json"
    source.write_text(body, encoding="utf-8")
    assert cli.main(["application", "call", "models.download", "--input-file", str(source)]) == 2
    output = capsys.readouterr()
    assert json.loads(output.out)["error"]["code"] == "invalid_argument"
    assert "private-value" not in output.out + output.err
    assert recording_client == []


def test_cli_destructive_action_requires_confirm_before_profile(recording_client, capsys):
    assert cli.main(["application", "call", "models.delete_all"]) == 2
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "confirmation_required"
    assert recording_client == []
    assert cli.main(["application", "call", "models.delete_all", "--confirm"]) == 0
    assert recording_client == [("operations.call", {"operation": "models.delete_all", "arguments": {}, "confirm": True})]


def test_cli_read_only_configuration_uses_one_flag_and_no_credentials(capsys):
    assert cli.main(["mcp-config", "--read-only"]) == 0
    server = json.loads(capsys.readouterr().out)["mcpServers"]["shengjian"]
    assert server["args"][-2:] == ["mcp", "--read-only"]
    assert "env" not in server


def test_cli_json_exponent_overflow_rejected_before_reading_profile(tmp_path, recording_client, capsys):
    source = tmp_path / "invalid.json"
    source.write_text('{"changes":{"agent_timeout_seconds":1e309},"expected_revision":"' + REVISION + '"}', encoding="utf-8")
    assert cli.main(["application", "call", "settings.update", "--input-file", str(source)]) == 2
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "invalid_argument"
    assert recording_client == []
