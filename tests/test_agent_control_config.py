# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
from pathlib import Path
import json
import tomllib

from app.agent_control_config import build_agent_control_config


def test_packaged_recipe_has_no_secret_and_explicit_permissions(tmp_path):
    executable = tmp_path / "FoilPage 程序" / "desktop.exe"
    agent = executable.parent / "agent" / "FoilPage-Agent.exe"
    agent.parent.mkdir(parents=True)
    agent.touch()
    data = tmp_path / "用户资料"
    read = build_agent_control_config(data, allow_write=False, packaged=True, executable=executable)
    edit = build_agent_control_config(data, allow_write=True, packaged=True, executable=executable)
    assert read["available"] is True
    server = read["configuration"]["mcpServers"]["foilpage"]
    assert server == {"command": str(agent.resolve()), "args": ["--data-dir", str(data.resolve()), "mcp", "--read-only"]}
    assert edit["configuration"]["mcpServers"]["foilpage"]["args"][-1] == "mcp"
    assert read["permissions"] == "read" and edit["permissions"] == "full"
    assert "--allow-processing" not in json.dumps(edit)
    assert tomllib.loads(read["codex_toml"])["mcp_servers"]["foilpage"] == server
    assert not (data / "settings.json").exists()


def test_source_recipe_is_independent_of_client_working_directory(tmp_path):
    recipe = build_agent_control_config(tmp_path / "data", source_root=tmp_path, packaged=False)
    args = recipe["configuration"]["mcpServers"]["foilpage"]["args"]
    assert Path(args[0]).is_absolute() and args[0].endswith("main.py")
    assert recipe["available"] is False
    assert "cwd" not in recipe["configuration"]["mcpServers"]["foilpage"]
    assert "token" not in json.dumps(recipe)


def test_config_rejects_truthy_non_boolean_scope(tmp_path):
    assert build_agent_control_config(tmp_path, allow_write="false")["ok"] is False
