# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""Actual hidden CLI/MCP children against a test-owned loopback desktop API.

No desktop window, provider or worker engine is launched.  The modern protocol
is negotiated with the installed official SDK; legacy stdio uses raw JSON-RPC.
"""
from __future__ import annotations

import asyncio
import json
import os
import queue
import subprocess
import sys
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

pytest.importorskip("mcp", reason="the isolated Agent environment installs the official MCP SDK")
import anyio

from test_agent_security import (agent_app, add_job, result, TASK_ID, JOB_ID,  # noqa: F401 — agent_app 是 pytest 夹具，符号须在本模块可见
                                 LOCAL_TOKEN, PRIVATE_SENTINELS, agent_bearer)
from account_auth_fixtures import grant_account


ROOT = Path(__file__).resolve().parents[1]
ENTRY = ROOT / "agent" / "main.py"
READ_TOOLS = {"shengjian_info", "list_tasks", "get_task", "list_documents",
              "read_document", "get_document_metadata", "list_application_operations",
              "read_application_state", "call_application_operation"}
WRITE_TOOLS = {"update_document", "update_document_metadata", "cancel_task", "retry_task"}


def child_environment(tmp_path):
    # No model, token, Python hook or proxy from the real process is forwarded.
    names = ("SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "PATH", "PATHEXT",
             "PROCESSOR_ARCHITECTURE", "TEMP", "TMP")
    env = {key: os.environ[key] for key in names if key in os.environ}
    for key in ("USERPROFILE", "LOCALAPPDATA", "APPDATA", "HOME"):
        env[key] = str(tmp_path / ("child-" + key.lower()))
    env.update(PYTHONUTF8="1", PYTHONIOENCODING="utf-8", PYTHONNOUSERSITE="1")
    return env


def argv(app, *arguments):
    executable = os.environ.get("SHENGJIAN_AGENT_TEST_EXE", "")
    if executable:
        binary = Path(executable)
        assert binary.is_absolute() and binary.is_file(), "Agent EXE fixture must be an existing absolute path"
        prefix = [str(binary)]
    else:
        prefix = [sys.executable, str(ENTRY)]
    return [*prefix, "--data-dir", str(app.store.base_dir),
            "--timeout", "5", *arguments]


def assert_no_connection_secrets(text):
    for secret in (LOCAL_TOKEN, agent_bearer(), *PRIVATE_SENTINELS, "cookie-private-fixture"):
        assert secret not in text


def cli(app, *arguments, stdin=None):
    process = subprocess.run(argv(app, *arguments), input=stdin, encoding="utf-8",
        capture_output=True, timeout=15, cwd=app.tmp_path, env=child_environment(app.tmp_path),
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), shell=False)
    assert_no_connection_secrets(process.stdout + process.stderr)
    assert process.stderr == "", process.stderr
    lines = process.stdout.splitlines()
    assert len(lines) == 1, process.stdout
    return process.returncode, json.loads(lines[0])


def test_real_cli_discovers_background_tasks_reads_utf8_and_never_migrates_profile(agent_app):
    add_job(agent_app.controller)
    settings = agent_app.store.base_dir / "settings.json"
    before = settings.read_bytes()
    code, info = cli(agent_app, "info")
    assert code == 0 and info["ok"] is True
    assert info["result"]["capabilities"]["payment_operations"] is False
    code, listed = cli(agent_app, "documents", "list", "--query", "测试")
    assert code == 0 and listed["result"]["items"][0]["task_id"] == TASK_ID
    code, document = cli(agent_app, "documents", "read", TASK_ID)
    assert code == 0 and document["result"]["text"] == "原始测试正文"
    assert document["result"]["size"] == len("原始测试正文".encode("utf-8"))
    assert settings.read_bytes() == before


def test_real_cli_body_and_metadata_edit_respect_independent_cas_and_stale_conflicts(agent_app):
    target = add_job(agent_app.controller)
    _, read = cli(agent_app, "documents", "read", TASK_ID)
    revision = read["result"]["revision"]
    code, saved = cli(agent_app, "documents", "update", TASK_ID, "--expected-revision", revision,
                      "--input-file", "-", stdin="后台编辑：保留中文\n第二行")
    assert code == 0 and saved["ok"] is True
    assert target.read_text(encoding="utf-8") == "后台编辑：保留中文\n第二行"
    code, stale = cli(agent_app, "documents", "update", TASK_ID, "--expected-revision", revision,
                      "--input-file", "-", stdin="过期内容")
    assert code == 2 and stale["error"]["code"] == "revision_conflict"
    _, metadata = cli(agent_app, "documents", "metadata", TASK_ID)
    meta_revision = metadata["result"]["revision"]
    code, changed = cli(agent_app, "documents", "update-metadata", TASK_ID,
        "--expected-revision", meta_revision, "--title", "后台标题", "--tag", "标签", "--category", "后台")
    assert code == 0 and changed["result"]["title"] == "后台标题"
    assert changed["result"]["tags"] == ["标签"]
    code, conflict = cli(agent_app, "documents", "update-metadata", TASK_ID,
        "--expected-revision", meta_revision, "--title", "过期标题")
    assert code == 2 and conflict["error"]["code"] == "revision_conflict"
    assert result(agent_app, "documents.metadata", {"task_id": TASK_ID})["title"] == "后台标题"


def test_real_cli_retry_command_retains_verified_account_gate_but_does_not_run_engine(agent_app):
    add_job(agent_app.controller, status="failed", attempts=2)
    before = agent_app.controller.queue.get_job(JOB_ID)
    code, gate = cli(agent_app, "tasks", "retry", TASK_ID)
    assert code == 2 and gate["error"]["code"] == "login_required"
    assert agent_app.controller.queue.get_job(JOB_ID) == before
    agent_app.controller.settings = grant_account(agent_app.store, agent_app.store.base_dir)
    code, accepted = cli(agent_app, "tasks", "retry", TASK_ID, "--allow-processing")
    assert code == 0 and accepted["result"]["status"] == "queued"
    current = agent_app.controller.queue.get_job(JOB_ID)
    assert current["attempts"] == 2 and current["max_attempts"] == 3
    assert len(agent_app.controller.queue.list_jobs()) == 1


def test_real_cli_rejects_path_id_and_keeps_private_arguments_out_of_error(agent_app):
    code, failure = cli(agent_app, "documents", "read", "../" + PRIVATE_SENTINELS[0])
    assert code == 2 and failure["error"]["code"] == "invalid_argument"
    code, config = cli(agent_app, "mcp-config")
    assert code == 0
    entry = config["mcpServers"]["shengjian"]
    if os.environ.get("SHENGJIAN_AGENT_TEST_EXE"):
        assert entry["command"] == str(Path(os.environ["SHENGJIAN_AGENT_TEST_EXE"]))
        assert entry["args"][0] == "--data-dir"
    else:
        assert entry["command"] == sys.executable
        assert entry["args"][0] == str(ENTRY)
    assert entry["args"][-1] == "mcp" and "env" not in entry


def tool_value(reply):
    assert_no_connection_secrets(str(reply))
    assert reply.is_error is not True, reply
    if reply.structured_content is not None:
        value = reply.structured_content
        return value.get("result", value) if isinstance(value, dict) else value
    texts = [item.text for item in reply.content if getattr(item, "type", None) == "text"]
    assert len(texts) == 1
    value = json.loads(texts[0])
    return value.get("result", value) if isinstance(value, dict) else value


def modern_session(app, flags, exercise):
    from mcp.client.session import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client
    async def run():
        command = argv(app, "mcp", *flags)
        parameters = StdioServerParameters(command=command[0], args=command[1:],
            env=child_environment(app.tmp_path), cwd=str(app.tmp_path))
        # Windows subprocess creation requires a real OS stderr handle.
        with (app.tmp_path / "sdk-child-stderr.log").open("w+", encoding="utf-8") as stderr:
            with anyio.fail_after(30):
                async with stdio_client(parameters, errlog=stderr) as (reader, writer):
                    async with ClientSession(reader, writer, read_timeout_seconds=5) as session:
                        discovered = await session.discover()
                        assert "2026-07-28" in discovered.supported_versions
                        assert session.protocol_version == "2026-07-28"
                        await exercise(session)
            stderr.seek(0)
            assert_no_connection_secrets(stderr.read())
    asyncio.run(run())


def test_official_sdk_modern_handshake_can_opt_into_readonly_tools(agent_app):
    target = add_job(agent_app.controller)
    async def exercise(session):
        tools = (await session.list_tools()).tools
        assert {tool.name for tool in tools} == READ_TOOLS
        assert all(tool.annotations.read_only_hint is True for tool in tools)
        assert all(tool.input_schema.get("additionalProperties") is False for tool in tools)
        read = tool_value(await session.call_tool("read_document", {"task_id": TASK_ID}))
        assert read["text"] == "原始测试正文"
        malformed = await session.call_tool("list_tasks", {"limit": True})
        assert malformed.is_error is True
        unknown_input = await session.call_tool("read_document", {"task_id": TASK_ID, "path": "C:/private"})
        assert unknown_input.is_error is True and "invalid_argument" in str(unknown_input.content)
        extra_token = await session.call_tool("list_tasks", {"token": PRIVATE_SENTINELS[0]})
        assert extra_token.is_error is True and PRIVATE_SENTINELS[0] not in str(extra_token)
        assert tool_value(await session.call_tool("read_document", {"task_id": TASK_ID}))["text"] == read["text"]
        denied = await session.call_tool("update_document", {"task_id": TASK_ID, "text": "越权",
            "expected_revision": read["revision"]})
        assert denied.is_error is True
        resources = await session.list_resources()
        assert str(resources.resources[0].uri) == "shengjian://agent-guide"
    modern_session(agent_app, ["--read-only"], exercise)
    assert target.read_text(encoding="utf-8") == "原始测试正文"


def test_official_sdk_full_scope_cas_and_metadata_retains_processing_login_gate(agent_app):
    target = add_job(agent_app.controller)
    async def exercise(session):
        tools = (await session.list_tools()).tools
        assert {tool.name for tool in tools} == READ_TOOLS | WRITE_TOOLS
        read = tool_value(await session.call_tool("read_document", {"task_id": TASK_ID}))
        forced = await session.call_tool("update_document", {"task_id": TASK_ID,
            "text": "未知字段不应保存", "expected_revision": read["revision"], "force": True})
        assert forced.is_error is True and "invalid_argument" in str(forced.content)
        assert target.read_text(encoding="utf-8") == "原始测试正文"
        saved = tool_value(await session.call_tool("update_document", {"task_id": TASK_ID,
            "text": "MCP 中文编辑", "expected_revision": read["revision"]}))
        assert saved["revision"] != read["revision"]
        conflict = await session.call_tool("update_document", {"task_id": TASK_ID,
            "text": "MCP 旧版本", "expected_revision": read["revision"]})
        assert conflict.is_error is True and "revision_conflict" in str(conflict.content)
        metadata = tool_value(await session.call_tool("get_document_metadata", {"task_id": TASK_ID}))
        changed = tool_value(await session.call_tool("update_document_metadata", {"task_id": TASK_ID,
            "expected_revision": metadata["revision"], "title": "MCP 标题", "tags": ["测试"], "category": "后台"}))
        assert changed["title"] == "MCP 标题" and changed["tags"] == ["测试"]
        retry = await session.call_tool("retry_task", {"task_id": TASK_ID})
        assert retry.is_error is True and "login_required" in str(retry.content)
    modern_session(agent_app, [], exercise)
    assert target.read_text(encoding="utf-8") == "MCP 中文编辑"


def test_official_sdk_processing_tool_retains_login_gate_and_queue_state(agent_app):
    add_job(agent_app.controller, status="failed", attempts=2)
    before = agent_app.controller.queue.get_job(JOB_ID)
    async def exercise(session):
        assert {tool.name for tool in (await session.list_tools()).tools} == READ_TOOLS | WRITE_TOOLS
        reply = await session.call_tool("retry_task", {"task_id": TASK_ID})
        assert reply.is_error is True and "login_required" in str(reply.content)
    modern_session(agent_app, ["--allow-write", "--allow-processing"], exercise)
    assert agent_app.controller.queue.get_job(JOB_ID) == before


@contextmanager
def legacy_stdio(app, flags=()):
    process = subprocess.Popen(argv(app, "mcp", *flags), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, encoding="utf-8", cwd=app.tmp_path,
        env=child_environment(app.tmp_path), creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), shell=False)
    replies = queue.Queue()
    seen, errors = [], []
    def collect_stdout():
        try:
            for line in process.stdout:
                assert len(line.encode("utf-8")) <= 3 * 1024 * 1024
                seen.append(line)
                replies.put(json.loads(line))
        except BaseException as exc:
            replies.put(exc)
    def collect_stderr():
        for line in process.stderr:
            errors.append(line)
    readers = [threading.Thread(target=collect_stdout, daemon=True), threading.Thread(target=collect_stderr, daemon=True)]
    for reader in readers:
        reader.start()
    def send(message, expect_id=None):
        process.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
        process.stdin.flush()
        if expect_id is None:
            return None
        for _ in range(20):
            reply = replies.get(timeout=8)
            if isinstance(reply, BaseException):
                raise reply
            if reply.get("id") == expect_id:
                return reply
        raise AssertionError("bounded stdio reply limit exceeded")
    try:
        yield send
    finally:
        process.stdin.close()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()  # This exact test-owned child only.
            process.wait(timeout=5)
        for reader in readers:
            reader.join(timeout=2)
        assert_no_connection_secrets("".join(seen + errors))
        assert all(json.loads(line).get("jsonrpc") == "2.0" for line in seen)
        process.stdout.close()
        process.stderr.close()


def test_legacy_2025_stdio_handshake_can_list_and_read_but_cannot_write(agent_app):
    add_job(agent_app.controller)
    with legacy_stdio(agent_app, ["--read-only"]) as send:
        initialize = send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-11-25", "capabilities": {},
            "clientInfo": {"name": "independent-test-fixture", "version": "1"}}}, 1)
        assert initialize["result"]["protocolVersion"] == "2025-11-25"
        send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        tools = send({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}, 2)
        assert {tool["name"] for tool in tools["result"]["tools"]} == READ_TOOLS
        read = send({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": "read_document", "arguments": {"task_id": TASK_ID}}}, 3)
        assert "原始测试正文" in json.dumps(read, ensure_ascii=False)
        denied = send({"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {
            "name": "update_document", "arguments": {"task_id": TASK_ID, "text": "越权", "expected_revision": "a" * 64}}}, 4)
        assert denied["result"]["isError"] is True


def test_legacy_stdio_rejects_extra_tool_fields_without_writing_or_breaking_session(agent_app):
    target = add_job(agent_app.controller)
    revision = result(agent_app, "documents.read", {"task_id": TASK_ID})["revision"]
    with legacy_stdio(agent_app, ["--allow-write"]) as send:
        init = send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-11-25", "capabilities": {},
            "clientInfo": {"name": "strict-input-fixture", "version": "1"}}}, 1)
        assert init["result"]["protocolVersion"] == "2025-11-25"
        send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        for number, name, arguments in [
            (2, "read_document", {"task_id": TASK_ID, "path": "C:/private"}),
            (3, "list_tasks", {"token": PRIVATE_SENTINELS[0]}),
            (4, "update_document", {"task_id": TASK_ID, "text": "未知字段不应保存",
                                    "expected_revision": revision, "force": True}),
        ]:
            denied = send({"jsonrpc": "2.0", "id": number, "method": "tools/call",
                "params": {"name": name, "arguments": arguments}}, number)
            assert denied["result"]["isError"] is True and "invalid_argument" in json.dumps(denied)
            assert_no_connection_secrets(json.dumps(denied))
        read = send({"jsonrpc": "2.0", "id": 5, "method": "tools/call",
            "params": {"name": "read_document", "arguments": {"task_id": TASK_ID}}}, 5)
        assert "原始测试正文" in json.dumps(read, ensure_ascii=False)
        assert target.read_text(encoding="utf-8") == "原始测试正文"


def test_actual_loopback_client_refuses_redirect_and_does_not_forward_bearer_or_retry(agent_app):
    from agent.client import AgentClient, AgentError
    calls = []
    class Trap(BaseHTTPRequestHandler):
        def do_GET(self):
            calls.append(("redirect-target", self.headers.get("Authorization")))
            self.send_response(200)
            self.end_headers()
        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            assert json.loads(body) == {"method": "info", "params": {}}
            calls.append(("initial", self.headers.get("Authorization")))
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{self.server.server_address[1]}/trap")
            self.send_header("Content-Length", "0")
            self.end_headers()
        def log_message(self, *_):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Trap)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with pytest.raises(AgentError, match="重定向") as error:
            AgentClient(port=server.server_address[1], token=agent_bearer(), timeout=2).call("info")
        assert error.value.code == "redirect_refused"
        assert calls == [("initial", "Bearer " + agent_bearer())]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_unavailable_profile_mcp_exits_with_stderr_and_keeps_stdout_protocol_clean(agent_app):
    command = argv(agent_app, "mcp")
    command[command.index("--data-dir") + 1] = str(agent_app.tmp_path / "missing-test-owned-profile")
    # Decode synchronously and strictly: Windows communicate() otherwise hides
    # a pipe-reader UnicodeDecodeError in a background thread and returns None.
    process = subprocess.run(command, capture_output=True, timeout=10,
        cwd=agent_app.tmp_path, env=child_environment(agent_app.tmp_path),
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), shell=False)
    assert process.returncode == 3 and process.stdout == b""
    stderr = process.stderr.decode("utf-8", errors="strict")
    assert stderr.startswith("profile_missing:")
    assert_no_connection_secrets(stderr)
