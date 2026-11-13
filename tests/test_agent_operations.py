# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""Each registered operation uses a fixed callable and exact arguments.

Only synthetic data/callbacks run here. Real HTTP and child-process roundtrips
are exercised separately, without provider, model, payment or OS commands.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from app.agent_operations import (AGENT_BRIDGE_METHODS, OPERATIONS, AgentOperations,
                                  ApplicationOperationError, public_result)


def operation_arguments(name):
    result = {}
    for key, (schema, required) in OPERATIONS[name].fields.items():
        if not required:
            continue
        result[key] = {"string": "fixture", "boolean": True, "object": {"theme": "sage"},
                       "array": ["fixture"]}[schema["type"]]
        if key in ("zip_path", "dest_path"):
            result[key] = "C:/agent-fixtures/test.zip"
        elif key in ("paths", "outputs", "downloads"):
            result[key] = ["C:/agent-fixtures/test.mp4"]
        elif key == "expected_revision":
            result[key] = "a" * 64
        elif key == "task_id":
            result[key] = "job:123456abcdef"
        elif key == "task_ids":
            result[key] = ["job:123456abcdef", "dl:123456abcdef"]
        elif key == "media_id":
            result[key] = "a" * 32
        elif key == "download_id":
            result[key] = "123456abcdef"
        elif key == "url":
            result[key] = "https://example.com/video/fixture"
    return result


@pytest.fixture
def operations():
    calls = []
    def callback(name):
        def run(**arguments):
            calls.append((name, arguments))
            return {"ok": True, "method": name}
        return run
    adapter = AgentOperations({name: callback(name) for name in AGENT_BRIDGE_METHODS})
    return adapter, calls


@pytest.mark.parametrize("name", tuple(OPERATIONS))
def test_every_registered_operation_dispatches_exact_arguments(name, operations):
    adapter, calls = operations
    arguments = operation_arguments(name)
    assert adapter.call(name, arguments, confirm=True) == {"ok": True, "method": OPERATIONS[name].method}
    assert calls == [(OPERATIONS[name].method, arguments)]


def test_registry_matches_real_bridge_signatures_without_importing_desktop():
    root = Path(__file__).parents[1]
    module = ast.parse((root / "app" / "bridge.py").read_text(encoding="utf-8"))
    bridge = next(node for node in module.body if isinstance(node, ast.ClassDef) and node.name == "Bridge")
    methods = {node.name: node for node in bridge.body if isinstance(node, ast.FunctionDef)}
    # 拆分后的域方法住在混入基类里（第一阶段：bridge_account.py 的登录/账号域）。
    # 注册表必须与「Bridge 实例实际可见的方法面」一致，因此把基类类体并入 AST 比对。
    base_files = {"AccountBridgeMixin": "bridge_account.py"}
    for base in bridge.bases:
        name = getattr(base, "id", None)
        if name in base_files:
            mixin_module = ast.parse((root / "app" / base_files[name]).read_text(encoding="utf-8"))
            mixin = next(n for n in mixin_module.body if isinstance(n, ast.ClassDef) and n.name == name)
            methods.update({node.name: node for node in mixin.body if isinstance(node, ast.FunctionDef)})
    for name, spec in OPERATIONS.items():
        assert spec.method in methods, name
        method = methods[spec.method]
        accepted = {arg.arg for arg in method.args.args[1:]} | {arg.arg for arg in method.args.kwonlyargs}
        required = {arg.arg for arg in method.args.args[1:len(method.args.args) - len(method.args.defaults)]}
        assert set(spec.fields).issubset(accepted), name
        assert required.issubset(spec.fields), name


@pytest.mark.parametrize("name", tuple(OPERATIONS))
def test_unknown_fields_are_rejected_for_every_operation_before_effects(name, operations):
    adapter, calls = operations
    arguments = {**operation_arguments(name), "execute": "synthetic-sensitive-value"}
    with pytest.raises(ApplicationOperationError) as caught:
        adapter.call(name, arguments, confirm=True)
    assert caught.value.code == "invalid_request"
    assert "synthetic-sensitive-value" not in str(caught.value)
    assert calls == []


@pytest.mark.parametrize("name", [name for name, spec in OPERATIONS.items() if spec.destructive])
def test_destructive_operations_require_exact_true(name, operations):
    adapter, calls = operations
    arguments = operation_arguments(name)
    with pytest.raises(ApplicationOperationError) as caught:
        adapter.call(name, arguments)
    assert caught.value.code == "confirmation_required"
    for invalid in (1, "true", None):
        with pytest.raises(ApplicationOperationError) as caught:
            adapter.call(name, arguments, confirm=invalid)
        assert caught.value.code == "invalid_request"
    assert calls == []


@pytest.mark.parametrize("name,arguments", [
    ("settings.update", {"changes": {}, "expected_revision": "a" * 64}),
    ("settings.update", {"changes": {"theme": "sage"}, "expected_revision": "stale"}),
    ("settings.launch_at_startup", {"enabled": "false"}),
    ("settings.launch_at_startup", {"enabled": 1}),
    ("media.preview", {"media_id": "../secrets"}),
    ("media.import", {"paths": []}),
    ("media.import", {"paths": "C:/fixture.mp4"}),
    ("media.import", {"paths": ["relative.mp4"]}),
    ("media.import", {"paths": ["\\\\server\\private.mp4"]}),
    ("media.import", {"paths": ["https://example.com/private.mp4"]}),
    ("downloads.transcribe", {"download_id": "job:123456abcdef"}),
    ("tasks.cancel_batch", {"task_ids": []}),
    ("tasks.cancel_batch", {"task_ids": [True]}),
    ("tasks.cancel_batch", {"task_ids": ["../private"]}),
    ("account.email_start", {"email": "a" * 241}),
    ("account.email_verify", {"email": "fixture@example.test", "code": "\ud800"}),
    ("maintenance.export_backup", {"dest_path": "../overwrite.zip"}),
    ("maintenance.import_backup", {"zip_path": "C:relative.zip"}),
])
def test_invalid_arguments_are_refused_without_callback(name, arguments, operations):
    adapter, calls = operations
    with pytest.raises(ApplicationOperationError) as caught:
        adapter.call(name, arguments, confirm=True)
    assert caught.value.code == "invalid_request"
    assert calls == []


def test_only_explicit_registry_bindings_are_visible():
    adapter = AgentOperations({"os_shell": lambda **_kw: "bad", "get_models": lambda: []})
    assert set(adapter.facade) == {"get_models"}
    catalogue = adapter.list(category="models")
    assert catalogue["total"] == len(catalogue["items"]) == 4
    assert {item["operation"] for item in catalogue["items"] if item["available"]} == {"models.list"}
    for item in catalogue["items"]:
        assert item["arguments_schema"]["additionalProperties"] is False
    with pytest.raises(ApplicationOperationError) as caught:
        adapter.call("os_shell", {})
    assert caught.value.code == "operation_not_found"
    with pytest.raises(ApplicationOperationError) as caught:
        adapter.call("settings.read", {})
    assert caught.value.code == "operation_unavailable"


@pytest.mark.parametrize("changes", [{"delete_source_after_translation": True}, {"delete_video_after_transcribe": True},
                                    {"delete_process_files_after_transcribe": True}, {"confirm_delete": False}])
def test_enable_automatic_deletion_requires_confirmation(changes, operations):
    adapter, calls = operations
    arguments = {"changes": changes, "expected_revision": "a" * 64}
    with pytest.raises(ApplicationOperationError) as caught:
        adapter.call("settings.update", arguments)
    assert caught.value.code == "confirmation_required" and calls == []
    adapter.call("settings.update", arguments, confirm=True)
    assert len(calls) == 1


def test_settings_cannot_select_arbitrary_executable_or_coerce_deletion_flag(operations):
    adapter, calls = operations
    for changes, code in [({"js_runtime_path": "C:/private/execute.exe"}, "protected_setting"),
                          ({"delete_source_after_translation": "true"}, "invalid_request")]:
        with pytest.raises(ApplicationOperationError) as caught:
            adapter.call("settings.update", {"changes": changes, "expected_revision": "a" * 64}, confirm=True)
        assert caught.value.code == code
    assert calls == []


def test_public_results_strip_nested_auth_credentials_and_signed_urls():
    safe = public_result({"ok": True, "account": {"login_token": "secret-login", "license_token": "secret-license",
        "email": "fixture@example.test"}, "settings": {"agent_api_key": "secret-key"},
        "secret_fields": {"agent_api_key": True}, "items": [{"cashier_url": "https://pay.example/?auth=secret"}],
        "message": "source https://source.example/path?sign=secret end", "finite": float("nan")})
    assert safe["account"] == {"email": "fixture@example.test"}
    assert safe["settings"] == {}
    assert safe["secret_fields"] == {"agent_api_key": True}
    assert "secret" not in str(safe).replace("secret_fields", "")
    assert safe["finite"] is None


@pytest.mark.parametrize("name", ["account.email_verify", "settings.update", "payments.manual_submit"])
def test_secret_input_cannot_be_reflected_under_alternative_keys(name):
    secret = "private-input-123"
    arguments = operation_arguments(name)
    if name == "account.email_verify":
        arguments["code"] = secret
    elif name == "settings.update":
        arguments["changes"]["agent_api_key"] = secret
    else:
        arguments["transaction_id"] = secret
    adapter = AgentOperations({OPERATIONS[name].method: lambda **_kwargs: {"ok": True, "detail": "echo: " + secret}})
    assert secret not in str(adapter.call(name, arguments))


def test_upstream_exception_and_rejected_result_never_echo_secret():
    def failing(**_arguments):
        raise RuntimeError("secret-key https://private.example/?credential=secret")
    adapter = AgentOperations({"get_models": failing})
    with pytest.raises(ApplicationOperationError) as caught:
        adapter.call("models.list", {})
    assert caught.value.code == "operation_failed"
    assert "secret" not in str(caught.value)
    adapter = AgentOperations({"email_login_verify": lambda **_kw: {"ok": False, "code": "invalid_code", "error": "echo-private-code"}})
    with pytest.raises(ApplicationOperationError) as caught:
        adapter.call("account.email_verify", {"email": "fixture@example.test", "code": "private-code"})
    assert caught.value.code == "invalid_code"
    assert "private-code" not in str(caught.value)
