# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""Current-user Windows startup registration for FoilPage."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "ShengJian"


def background_command(
    executable: str | Path | None = None,
    *,
    frozen: bool | None = None,
    project_root: str | Path | None = None,
) -> str:
    """Return the quoted command registered for silent background startup."""
    exe = Path(executable or sys.executable).resolve()
    is_frozen = bool(getattr(sys, "frozen", False)) if frozen is None else frozen
    if is_frozen:
        return subprocess.list2cmdline([str(exe), "--background"])
    root = Path(project_root or Path(__file__).resolve().parent.parent).resolve()
    return subprocess.list2cmdline([str(exe), str(root / "app" / "desktop_web.py"), "--background"])


def enable_startup(command: str | None = None) -> str:
    if os.name != "nt":
        raise RuntimeError("开机自启仅支持 Windows")
    import winreg

    value = command or background_command()
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, RUN_VALUE, 0, winreg.REG_SZ, value)
    return value


def disable_startup() -> None:
    if os.name != "nt":
        return
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, RUN_VALUE)
    except FileNotFoundError:
        return


def startup_enabled(command: str | None = None) -> bool:
    if os.name != "nt":
        return False
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_QUERY_VALUE) as key:
            value, kind = winreg.QueryValueEx(key, RUN_VALUE)
    except FileNotFoundError:
        return False
    expected = command or background_command()
    return kind == winreg.REG_SZ and str(value).casefold() == expected.casefold()
