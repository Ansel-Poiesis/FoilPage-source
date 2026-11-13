# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
# Build with the independent .agent-venv, keeping ASR/WebView out of the MCP host.
import os
from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata

ROOT = os.path.dirname(SPECPATH)
# The optional mcp.cli exits at import when its developer extra is absent.
# Our own CLI uses argparse; collect only the SDK transport/server modules.
mcp_imports = []
for package in ("mcp.server", "mcp.client", "mcp.shared", "mcp.types"):
    mcp_imports += collect_submodules(package)
mcp_datas = (collect_data_files("mcp") + copy_metadata("mcp", recursive=True)
             + copy_metadata("cryptography", recursive=True) + copy_metadata("pywin32"))
a = Analysis(
    [os.path.join(ROOT, "agent", "main.py")], pathex=[ROOT],
    binaries=[],
    datas=mcp_datas + [(os.path.join(ROOT, "docs", "agent-control.md"), "docs")],
    hiddenimports=["win32crypt", *mcp_imports],
    excludes=["pytest", "_pytest", "PyInstaller", "torch", "numpy", "webview", "faster_whisper"],
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="FoilPage-Agent",
          console=True, hide_console="hide-early", upx=False,
          icon=os.path.join(ROOT, "assets", "icons", "foilpage.ico"))
coll = COLLECT(exe, a.binaries, a.datas, name="FoilPage-Agent", upx=False)
