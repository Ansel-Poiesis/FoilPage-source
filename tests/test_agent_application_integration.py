# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""Full operation control through real loopback HTTP and hidden CLI/MCP.

The fixture uses a real AppController/Bridge and temporary DPAPI profile. Only
safe local reads/settings execute business code. The all-operation dispatch
exercise supplies test-owned bindings, so no email, model, payment or OS UI
side effect can run merely to test the protocol.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

pytest.importorskip("mcp")

from app.agent_operations import AGENT_BRIDGE_METHODS, OPERATIONS
from app.bridge import Bridge
from app.local_api import LocalApiServer
from test_agent_security import agent_app, result, PRIVATE_SENTINELS  # noqa: F401 — agent_app 是 pytest 夹具
from test_agent_integration import cli, modern_session, tool_value
from test_agent_operations import operation_arguments


@pytest.fixture
def application_app(agent_app):
    app = agent_app
    app.server.stop()
    app.bridge = Bridge(app.controller, SimpleNamespace(running=False), app.store,
                        window_provider=lambda: None, on_quit=lambda: None)
    app.server = LocalApiServer(app.controller, app.coordinator, port=0,
                               agent_operations=app.bridge.agent_operations_facade())
    app.server.start()
    app.controller.save_settings({"local_api_port": app.server.address[1]})
    return app


def test_real_bridge_availability_contains_every_registered_operation(application_app):
    app = application_app
    assert set(app.bridge.agent_operations_facade()) == set(AGENT_BRIDGE_METHODS)
    catalogue = result(app, "operations.list")
    assert catalogue["total"] == len(OPERATIONS)
    assert {item["operation"] for item in catalogue["items"]} == set(OPERATIONS)
    assert all(item["available"] for item in catalogue["items"])
    _, info = cli(app, "info")
    assert info["result"]["capabilities"]["application_operations"] is True
    assert info["result"]["capabilities"]["payment_operations"] is True


def test_real_cli_settings_prompts_and_secrets_roundtrip_cas(application_app):
    app = application_app
    code, read = cli(app, "application", "call", "settings.read")
    assert code == 0
    snapshot = read["result"]
    assert "agent_api_key" not in snapshot["settings"]
    assert snapshot["secret_fields"]["agent_api_key"] is True
    changes = {"title_skill_prompt": "仅在下一项任务采用的后台提示词", "agent_api_key": "fixture-new-model-key"}
    body = json.dumps({"changes": changes, "expected_revision": snapshot["revision"]}, ensure_ascii=False)
    code, saved = cli(app, "application", "call", "settings.update", "--input-file", "-", stdin=body)
    assert code == 0 and saved["ok"] is True, saved
    assert app.store.load()["agent_api_key"] == "fixture-new-model-key"
    assert saved["result"]["settings"]["title_skill_prompt"] == changes["title_skill_prompt"]
    assert "fixture-new-model-key" not in json.dumps(saved)
    assert b"fixture-new-model-key" not in (app.store.base_dir / "settings.json").read_bytes()
    assert saved["result"]["revision"] != snapshot["revision"]
    code, stale = cli(app, "application", "call", "settings.update", "--input-file", "-", stdin=body)
    assert code == 2 and stale["error"]["code"] == "revision_conflict"
    protected = json.dumps({"changes": {"credit_balance": 999999}, "expected_revision": saved["result"]["revision"]})
    code, denied = cli(app, "application", "call", "settings.update", "--input-file", "-", stdin=protected)
    assert code == 2 and denied["error"]["code"] == "protected_setting"
    assert app.controller.settings["credit_balance"] != 999999
    changed_host = json.dumps({"changes": {"agent_base_url": "https://model-fixture.invalid/v1"},
                               "expected_revision": saved["result"]["revision"]})
    code, moved = cli(app, "application", "call", "settings.update", "--input-file", "-", stdin=changed_host)
    assert code == 0, moved
    assert app.store.load()["agent_api_key"] == ""
    assert moved["result"]["secret_fields"]["agent_api_key"] is False


def test_real_official_stdio_mcp_changes_settings_and_read_only_rejects(application_app):
    app = application_app
    async def full(session):
        catalogue = tool_value(await session.call_tool("list_application_operations", {"category": "settings"}))
        assert catalogue["mode"] == "full" and all(item["allowed"] for item in catalogue["items"])
        read = tool_value(await session.call_tool("read_application_state", {}))
        saved = tool_value(await session.call_tool("call_application_operation", {"operation": "settings.update",
            "arguments": {"changes": {"title_model": "fixture-title-model"}, "expected_revision": read["revision"]}}))
        assert saved["settings"]["title_model"] == "fixture-title-model"
        assert all(secret not in str(saved) for secret in PRIVATE_SENTINELS)
    modern_session(app, [], full)
    async def readonly(session):
        catalogue = tool_value(await session.call_tool("list_application_operations", {"category": "settings"}))
        assert catalogue["mode"] == "read"
        assert next(item for item in catalogue["items"] if item["operation"] == "settings.update")["allowed"] is False
        reply = await session.call_tool("call_application_operation", {"operation": "settings.update",
            "arguments": {"changes": {"title_model": "forbidden"}, "expected_revision": "a" * 64}})
        assert reply.is_error is True and "read_only" in str(reply.content)
    modern_session(app, ["--read-only"], readonly)
    assert app.store.load()["title_model"] == "fixture-title-model"


def _install_owned_dispatch(app):
    calls = []
    def bound(name):
        def invoke(**arguments):
            calls.append((name, arguments))
            return {"ok": True, "method": name}
        return invoke
    app.server.agent_control.operations.facade = {name: bound(name) for name in AGENT_BRIDGE_METHODS}
    return calls


def test_every_operation_reaches_exact_binding_through_hidden_cli(application_app):
    app = application_app
    calls = _install_owned_dispatch(app)
    for name, spec in OPERATIONS.items():
        arguments = operation_arguments(name)
        flags = ["--confirm"] if spec.destructive else []
        code, payload = cli(app, "application", "call", name, "--input-file", "-", *flags,
                            stdin=json.dumps(arguments, ensure_ascii=False))
        assert code == 0 and payload["result"] == {"ok": True, "method": spec.method}, (name, payload)
        assert calls[-1] == (spec.method, arguments)
    assert len(calls) == len(OPERATIONS)


def test_every_operation_reaches_exact_binding_through_official_stdio(application_app):
    app = application_app
    calls = _install_owned_dispatch(app)
    async def exercise(session):
        for name, spec in OPERATIONS.items():
            arguments = operation_arguments(name)
            reply = await session.call_tool("call_application_operation", {
                "operation": name, "arguments": arguments, "confirm": bool(spec.destructive)})
            assert tool_value(reply) == {"ok": True, "method": spec.method}, name
            assert calls[-1] == (spec.method, arguments)
    modern_session(app, [], exercise)
    assert len(calls) == len(OPERATIONS)
