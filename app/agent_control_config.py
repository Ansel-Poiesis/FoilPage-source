# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""Public local Agent connection recipes; never load runtime credentials."""
from __future__ import annotations

import json
from pathlib import Path
import sys


def build_agent_control_config(data_dir, *, allow_write=True, packaged=None,
                               executable=None, source_root=None):
    if not isinstance(allow_write, bool):
        return {"ok": False, "error": "权限选项无效"}
    frozen = bool(getattr(sys, "frozen", False)) if packaged is None else packaged
    binary = Path(executable or sys.executable).resolve()
    root = Path(source_root or Path(__file__).resolve().parent.parent).resolve()
    if frozen:
        command = binary.parent / "agent" / "FoilPage-Agent.exe"
        prefix = []
        available = command.is_file()
    else:
        command = root / ".agent-venv" / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
        prefix = ["-m", "agent"]
        available = command.is_file()
    args = prefix + ["--data-dir", str(Path(data_dir).resolve()), "mcp"]
    if not allow_write:
        args.append("--read-only")
    server = {"command": str(command), "args": args}
    if not frozen:
        server["cwd"] = str(root)
    # JSON basic strings/arrays are also valid TOML basic strings/arrays.
    toml = "[mcp_servers.foilpage]\ncommand = " + json.dumps(str(command), ensure_ascii=False)
    toml += "\nargs = " + json.dumps(args, ensure_ascii=False) + "\n"
    if not frozen:
        # Codex accepts cwd through the server environment, not a TOML cwd key.
        # Invoke an absolute script so clients need not inherit this source cwd.
        args = [str(root / "agent" / "main.py")] + args[2:]
        server["args"] = args
        server.pop("cwd", None)
        toml = "[mcp_servers.foilpage]\ncommand = " + json.dumps(str(command), ensure_ascii=False)
        toml += "\nargs = " + json.dumps(args, ensure_ascii=False) + "\n"
    return {"ok": True, "available": available, "permissions": "full" if allow_write else "read",
            "configuration": {"mcpServers": {"foilpage": server}}, "codex_toml": toml}
