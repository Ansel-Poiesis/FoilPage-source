# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""Branded Windows toasts, using the OS WinRT API without extra packages."""
from __future__ import annotations

import base64
import ctypes
import os
import subprocess
from pathlib import Path
from xml.etree.ElementTree import Element, SubElement, tostring

APP_ID = "Ansel.ShengJian.Desktop"
APP_NAME = "FoilPage"


def register_identity(icon_path: Path) -> bool:
    if os.name != "nt":
        return False
    try:
        import winreg
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, rf"Software\Classes\AppUserModelId\{APP_ID}") as key:
            winreg.SetValueEx(key, "DisplayName", 0, winreg.REG_SZ, APP_NAME)
            winreg.SetValueEx(key, "IconUri", 0, winreg.REG_SZ, str(icon_path.resolve()))
            winreg.SetValueEx(key, "ShowInSettings", 0, winreg.REG_DWORD, 1)
        setter = ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID
        setter.argtypes = [ctypes.c_wchar_p]
        setter.restype = ctypes.c_long
        setter(APP_ID)
        return True
    except (OSError, AttributeError):
        return False


def toast_xml(title: str, message: str) -> str:
    root = Element("toast", {"duration": "short"})
    binding = SubElement(SubElement(root, "visual"), "binding", {"template": "ToastGeneric"})
    # The Windows app header supplies APP_NAME. These are the next two rows.
    SubElement(binding, "text").text = str(title or "")[:64]
    SubElement(binding, "text").text = str(message or "")[:200]
    # TaskNotifications owns the success/error beep; do not play two sounds.
    SubElement(root, "audio", {"silent": "true"})
    return tostring(root, encoding="unicode")


def show_toast(title: str, message: str) -> bool:
    """True means OS submission succeeded; focus assist can suppress display."""
    if os.name != "nt":
        return False
    payload = base64.b64encode(toast_xml(title, message).encode("utf-8")).decode("ascii")
    # Only base64 and a fixed app id enter executable code. Neither source
    # titles nor XML are interpolated as PowerShell expressions.
    script = (
        "$ErrorActionPreference='Stop';"
        "[Windows.UI.Notifications.ToastNotificationManager,Windows.UI.Notifications,ContentType=WindowsRuntime] > $null;"
        "[Windows.Data.Xml.Dom.XmlDocument,Windows.Data.Xml.Dom.XmlDocument,ContentType=WindowsRuntime] > $null;"
        "$doc=New-Object Windows.Data.Xml.Dom.XmlDocument;"
        f"$doc.LoadXml([Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{payload}')));"
        "$toast=[Windows.UI.Notifications.ToastNotification]::new($doc);"
        f"[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('{APP_ID}').Show($toast);"
    )
    command = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    executable = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    try:
        result = subprocess.run([str(executable), "-NoLogo", "-NoProfile", "-NonInteractive", "-EncodedCommand", command],
                                capture_output=True, timeout=8, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False
