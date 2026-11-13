# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""Machine-readable commands and stdio MCP launcher for the local Agent API."""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any, Sequence

from agent.client import AgentClient, AgentError, default_data_dir
from agent import validation as validate
from engine.agent_protocol import AGENT_MAX_BODY_BYTES, AGENT_MAX_TEXT_BYTES


class ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        # argparse's message may quote a supplied argument. Keep credentials,
        # accidental large text and paths out of the machine-readable error.
        raise AgentError("invalid_argument", "命令参数无效，请使用 --help 查看用法")


def _parser() -> argparse.ArgumentParser:
    parser = ArgumentParser(prog="shengjian-agent", description="控制同一用户已在后台运行的 FoilPage 实例")
    parser.add_argument("--data-dir", type=Path, help="FoilPage 数据根目录，默认使用当前用户的本地配置")
    parser.add_argument("--timeout", type=float, default=15.0, help="本机请求超时秒数（默认 15）")
    commands = parser.add_subparsers(dest="command", required=True, parser_class=ArgumentParser)
    commands.add_parser("info", help="读取本机连接与 Agent 能力")
    application = commands.add_parser("application", help="配置模型／提示词、管理应用、账户与用户订单").add_subparsers(
        dest="application_command", required=True, parser_class=ArgumentParser,
    )
    operations = application.add_parser("operations", help="查实际支持操作及严格参数")
    operations.add_argument("--category", default="")
    operation = application.add_parser("call", help="执行目录内应用操作；参数用 UTF-8 JSON 文件或标准输入")
    operation.add_argument("operation")
    operation.add_argument("--input-file", help="UTF-8 JSON 参数文件；- 从标准输入读取，秘密不放命令参数")
    operation.add_argument("--confirm", action="store_true", help="明确确认用户要求的删除、恢复或设备解绑")
    tasks = commands.add_parser("tasks", help="查询、取消或明确重试任务").add_subparsers(
        dest="task_command", required=True, parser_class=ArgumentParser,
    )
    listing = tasks.add_parser("list", help="搜索任务")
    listing.add_argument("--query", default="")
    listing.add_argument("--status", default="")
    listing.add_argument("--limit", type=int, default=50)
    for name in ("get", "cancel", "retry"):
        action = tasks.add_parser(name, help={"get": "读取任务", "cancel": "取消任务", "retry": "重试任务"}[name])
        action.add_argument("task_id")
        if name == "retry":
            action.add_argument("--allow-processing", action="store_true", help="明确授权按现有配置重新处理，可能计费")

    documents = commands.add_parser("documents", help="查询和编辑已有文本").add_subparsers(
        dest="document_command", required=True, parser_class=ArgumentParser,
    )
    docs_list = documents.add_parser("list", help="搜索文本库")
    docs_list.add_argument("--query", default="")
    docs_list.add_argument("--limit", type=int, default=50)
    for name in ("read", "metadata", "update", "update-metadata"):
        action = documents.add_parser(name, help={
            "read": "读取正文及 revision", "metadata": "读取标题、标签与 revision",
            "update": "保存正文", "update-metadata": "保存标题、标签或分类",
        }[name])
        action.add_argument("task_id")
        if name in ("read", "update"):
            action.add_argument("--tab", default="final", choices=("final", "raw"))
        if name in ("update", "update-metadata"):
            action.add_argument("--expected-revision", required=True, help="最近读取返回的 revision；过期时拒绝覆盖")
        if name == "update":
            action.add_argument("--input-file", required=True, help="UTF-8 正文文件；- 表示标准输入")
        if name == "update-metadata":
            action.add_argument("--title")
            action.add_argument("--category")
            tags = action.add_mutually_exclusive_group()
            tags.add_argument("--tag", action="append", dest="tags", help="可重复，替换整个标签集合")
            tags.add_argument("--clear-tags", action="store_true")

    mcp = commands.add_parser("mcp", help="启动 stdio MCP，默认完整控制；可选只读")
    mcp.add_argument("--read-only", action="store_true", help="只查询，拒绝设置、处理、登录及购买等更改")
    mcp.add_argument("--allow-write", action="store_true", help="兼容旧配置；默认完整控制，无需此标志")
    mcp.add_argument("--allow-processing", action="store_true", help="兼容旧配置；默认完整控制，无需此标志")
    config = commands.add_parser("mcp-config", help="生成无密钥的 MCP 配置，不修改其他 Agent 设置")
    config.add_argument("--format", choices=("json", "codex"), default="json")
    config.add_argument("--allow-write", action="store_true")
    config.add_argument("--allow-processing", action="store_true")
    config.add_argument("--read-only", action="store_true")
    return parser


def mcp_configuration(
    data_dir: Path, *, allow_write: bool = False, allow_processing: bool = False, read_only: bool = False,
) -> dict[str, Any]:
    if read_only and (allow_write or allow_processing):
        raise AgentError("invalid_argument", "只读模式不能同时指定旧版写入或处理标志")
    # Installed users use the bundled executable; developers use this package.
    # Paths only: credentials stay in the user's protected profile.
    args = ([] if getattr(sys, "frozen", False) else [str(Path(__file__).resolve().parent / "main.py")]) + ["--data-dir", str(data_dir.resolve()), "mcp"]
    if read_only:
        args.append("--read-only")
    return {"mcpServers": {"shengjian": {"command": sys.executable, "args": args}}}


def _read_input(path: str) -> str:
    try:
        if path == "-":
            stream = getattr(sys.stdin, "buffer", None)
            if stream is None:
                value = sys.stdin.read(AGENT_MAX_TEXT_BYTES + 1)
                return validate.text(value)
            encoded = stream.read(AGENT_MAX_TEXT_BYTES + 1)
        else:
            with Path(path).expanduser().open("rb") as stream:
                encoded = stream.read(AGENT_MAX_TEXT_BYTES + 1)
    except OSError:
        raise AgentError("input_unreadable", "无法读取指定正文文件") from None
    if len(encoded) > AGENT_MAX_TEXT_BYTES:
        raise AgentError("request_too_large", "正文超过 2 MB，请缩短内容")
    try:
        return validate.text(encoded.decode("utf-8-sig"))
    except UnicodeDecodeError:
        raise AgentError("invalid_argument", "正文文件须使用 UTF-8 编码") from None


def _read_arguments(path: str | None) -> dict[str, Any]:
    if path is None:
        return {}
    try:
        if path == "-":
            stream = getattr(sys.stdin, "buffer", None)
            raw = stream.read(AGENT_MAX_BODY_BYTES + 1) if stream is not None else sys.stdin.read(AGENT_MAX_BODY_BYTES + 1).encode("utf-8")
        else:
            with Path(path).expanduser().open("rb") as stream:
                raw = stream.read(AGENT_MAX_BODY_BYTES + 1)
    except (OSError, UnicodeError):
        raise AgentError("input_unreadable", "无法读取操作参数文件") from None
    if len(raw) > AGENT_MAX_BODY_BYTES:
        raise AgentError("request_too_large", "操作参数文件超过 3 MiB")

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate")
            result[key] = value
        return result

    def constant(_value):
        raise ValueError("non-json")

    def finite_float(value):
        result = float(value)
        if not math.isfinite(result):
            raise ValueError("non-finite")
        return result
    try:
        result = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=pairs, parse_constant=constant, parse_float=finite_float)
    except (ValueError, UnicodeError, RecursionError):
        raise AgentError("invalid_argument", "操作参数必须是严格 UTF-8 JSON，不允许重复字段或非有限数字") from None
    if type(result) is not dict:
        raise AgentError("invalid_argument", "操作参数必须是 JSON 对象")
    return result


def _operation(args: argparse.Namespace) -> tuple[str, dict[str, Any]]:
    if args.command == "info":
        return "info", {}
    if args.command == "application":
        if args.application_command == "operations":
            if len(args.category) > 40:
                raise AgentError("invalid_argument", "操作分类无效")
            return "operations.list", {"category": args.category}
        from app.agent_operations import OPERATIONS, AgentOperations, ApplicationOperationError
        if args.operation not in OPERATIONS:
            raise AgentError("unknown_operation", "不支持该应用操作，请先查看 application operations")
        if OPERATIONS[args.operation].destructive and args.confirm is not True:
            raise AgentError("confirmation_required", "此操作会删除、恢复或解绑，请明确设置 --confirm")
        arguments = _read_arguments(args.input_file)
        try:
            AgentOperations.validate(args.operation, arguments, confirm=args.confirm)
        except ApplicationOperationError as exc:
            raise AgentError("invalid_argument" if exc.code == "invalid_request" else exc.code, str(exc)) from None
        return "operations.call", {"operation": args.operation, "arguments": arguments, "confirm": args.confirm}
    if args.command == "tasks":
        if args.task_command == "list":
            params = validate.listing(args.query, args.limit)
            params["status"] = validate.status(args.status)
            return "tasks.list", params
        target = validate.task_id(args.task_id)
        return "tasks." + args.task_command, {"task_id": target}
    if args.command == "documents":
        operation = args.document_command
        if operation == "list":
            return "documents.list", validate.listing(args.query, args.limit)
        params = {"task_id": validate.task_id(args.task_id)}
        if operation in ("read", "update"):
            params["tab"] = validate.tab(args.tab)
        if operation in ("update", "update-metadata"):
            params["expected_revision"] = validate.revision(args.expected_revision)
        if operation == "update":
            params["text"] = _read_input(args.input_file)
        if operation == "update-metadata":
            params.update(validate.metadata_changes(
                title=args.title, category=args.category, tags=[] if args.clear_tags else args.tags,
            ))
        return "documents." + operation.replace("-", "_"), params
    raise AgentError("invalid_argument", "未知命令")


def _configure_utf8_stdio() -> None:
    # Frozen Windows programs don't reliably honor PYTHONIOENCODING. Configure
    # both streams before argparse, profile errors or the SDK can write. This
    # emits no text, so stdout remains exclusively the MCP wire while serving.
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="strict")


def _json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, allow_nan=False))


def main(argv: Sequence[str] | None = None) -> int:
    _configure_utf8_stdio()
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    serving_mcp = "mcp" in raw_argv
    try:
        args = _parser().parse_args(raw_argv)
        if args.command == "mcp-config":
            configuration = mcp_configuration(args.data_dir or default_data_dir(), allow_write=args.allow_write, allow_processing=args.allow_processing, read_only=args.read_only)
            if args.format == "codex":
                item = configuration["mcpServers"]["shengjian"]
                # JSON strings/arrays are valid TOML strings/arrays here, including
                # escaped Windows paths. Do not echo credentials or environment.
                print("[mcp_servers.shengjian]")
                print("command = " + json.dumps(item["command"], ensure_ascii=False))
                print("args = " + json.dumps(item["args"], ensure_ascii=False))
            else:
                _json(configuration)
            return 0
        if args.command == "mcp":
            serving_mcp = True
            if args.read_only and (args.allow_write or args.allow_processing):
                raise AgentError("invalid_argument", "只读模式不能同时指定旧版写入或处理标志")
            client = AgentClient.from_data_dir(args.data_dir, timeout=args.timeout)
            from agent.mcp_server import run_stdio
            run_stdio(client, allow_write=args.allow_write, allow_processing=args.allow_processing, read_only=args.read_only)
            return 0
        method, params = _operation(args)  # validate before profile/network access
        result = AgentClient.from_data_dir(args.data_dir, timeout=args.timeout).call(method, params)
        _json({"ok": True, "result": result})
        return 0
    except AgentError as exc:
        if serving_mcp:
            print(exc.code + ": " + exc.message, file=sys.stderr)
        else:
            _json({"ok": False, "error": exc.as_dict()})
        return 3 if exc.code in ("connection_failed", "connection_timeout", "profile_missing", "profile_locked") else 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
