# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""CI Runner 服务联动：应用启动时唤起本机的 GitHub Actions Runner Windows 服务。

仅开发机装有该服务（actions.runner.*，服务化安装见
C:\\Users\\mingc\\AppData\\Local\\ShengJianCI\\setup-runner-service.ps1）；
普通用户机器上没有该服务，wake_runner_service 安全空转返回 not-installed。
免提权 Start-Service 依赖安装服务时写入服务 DACL 的当前用户授权（RP 权限）。
"""
from __future__ import annotations

import base64
import logging
import os
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)

# 固定脚本，无任何动态插值进 PowerShell 表达式；查询与启动服务各一次。
_WAKE_SCRIPT = (
    "$ErrorActionPreference='Stop';"
    "$svc = Get-Service -Name 'actions.runner.*' -ErrorAction SilentlyContinue | Select-Object -First 1;"
    "if (-not $svc) { Write-Output 'not-installed'; exit 0 };"
    "if ($svc.Status -eq 'Running') { Write-Output 'already-running'; exit 0 };"
    "try { Start-Service -Name $svc.Name -ErrorAction Stop; "
    "Start-Sleep -Seconds 2; "
    "if ((Get-Service -Name $svc.Name).Status -eq 'Running') { Write-Output 'started' } "
    "else { Write-Output 'start-pending' } } catch { Write-Output 'start-failed' }"
)


def _powershell(script: str, timeout: int = 30) -> str | None:
    """执行一段固定 PowerShell 脚本并返回 stdout；失败返回 None（不抛异常）。"""
    if os.name != "nt":
        return None
    command = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    executable = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    try:
        result = subprocess.run(
            [str(executable), "-NoLogo", "-NoProfile", "-NonInteractive", "-EncodedCommand", command],
            capture_output=True, timeout=timeout, text=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def wake_runner_service() -> str:
    """确保 CI Runner 服务在运行。返回结果码，永不抛异常：

    skipped-non-windows / not-installed / already-running / started /
    start-pending / start-failed / probe-failed
    """
    if os.name != "nt":
        return "skipped-non-windows"
    out = _powershell(_WAKE_SCRIPT)
    if out is None:
        return "probe-failed"
    known = {"not-installed", "already-running", "started", "start-pending", "start-failed"}
    return out if out in known else "probe-failed"
