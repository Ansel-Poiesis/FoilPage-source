# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
# PyInstaller spec for the WebView2 desktop workbench.
import os

from PyInstaller.utils.hooks import collect_all, collect_data_files

block_cipher = None
ROOT = os.path.dirname(SPECPATH)

yt_datas, yt_binaries, yt_hiddenimports = collect_all("yt_dlp")
# yt-dlp[default] 的解析依赖多为惰性导入/数据驱动，模块图追不全，显式收集：
# - yt_dlp_ejs：YouTube 挑战求解脚本（包数据，缺了 JS 运行时也无解可用）
# - pycryptodomex / mutagen / requests / urllib3 / websockets / brotli：
#   YouTube 格式解密与 yt-dlp 网络层（hiddenimports 补 trace 盲区）
ejs_datas = collect_data_files("yt_dlp_ejs")

datas = yt_datas + ejs_datas + [
    (os.path.join(ROOT, "docs", "agent-setup.md"), "docs"),
    (os.path.join(ROOT, "docs", "agent-control.md"), "docs"),
    (os.path.join(ROOT, "docs", "local-data-continuity.md"), "docs"),
    (os.path.join(ROOT, "docs", "release-security.md"), "docs"),
    (os.path.join(ROOT, "app", "skills"), "app/skills"),
    (os.path.join(ROOT, "app", "webview_ui"), "app/webview_ui"),
    (os.path.join(ROOT, "assets", "fonts", "LXGWWenKaiScreen.ttf"), "assets/fonts"),
    (os.path.join(ROOT, "assets", "fonts", "LXGWWenKaiScreen.woff2"), "assets/fonts"),
    (os.path.join(ROOT, "assets", "fonts", "OFL.txt"), "assets/fonts"),
    (os.path.join(ROOT, "assets", "icons", "foilpage.ico"), "assets/icons"),
    (os.path.join(ROOT, "assets", "icons", "foilpage.png"), "assets/icons"),
]
# 任务终态提示音预设组（1.3.12）：build/generate_sounds.py 重新生成。
datas += [
    (os.path.join(ROOT, "assets", "sounds", pack, kind + ".wav"),
     os.path.join("assets", "sounds", pack))
    for pack in ("crisp", "soft", "deep")
    for kind in ("success", "error")
]

a = Analysis(
    [os.path.join(ROOT, "app", "desktop_web.py")],
    pathex=[ROOT],
    binaries=yt_binaries,
    datas=datas,
    hiddenimports=["huggingface_hub", "engine.agent_client", "app.agent_service", "pystray", "webview", "webview.platforms.edgechromium", "proxy_tools", "bottle", "yt_dlp_ejs", "Cryptodome", "Cryptodome.Cipher.AES", "mutagen", "requests", "urllib3", "websockets", "brotli", *yt_hiddenimports],
    hookspath=[],
    runtime_hooks=[],
    excludes=[
        "torch", "tensorflow", "faster_whisper", "ctranslate2",
        "onnxruntime", "av", "flet", "numpy", "imageio_ffmpeg", "opencc",
        "PyInstaller", "pytest", "_pytest", "setuptools", "fontTools",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)


# Per-Monitor V2 DPI awareness: keeps text/controls vector-crisp at every
# Windows scaling level instead of letting the OS bitmap-stretch the window.
DPI_MANIFEST = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<assembly xmlns="urn:schemas-microsoft-com:asm.v1" manifestVersion="1.0">
  <assemblyIdentity version="1.0.0.0" processorArchitecture="*" name="Python" type="win32"/>
  <dependency>
    <dependentAssembly>
      <assemblyIdentity type="win32" name="Microsoft.Windows.Common-Controls" version="6.0.0.0" processorArchitecture="*" publicKeyToken="6595b64144ccf1df" language="*"/>
    </dependentAssembly>
  </dependency>
  <trustInfo xmlns="urn:schemas-microsoft-com:asm.v3">
    <security>
      <requestedPrivileges>
        <requestedExecutionLevel level="asInvoker" uiAccess="false"/>
      </requestedPrivileges>
    </security>
  </trustInfo>
  <compatibility xmlns="urn:schemas-microsoft-com:compatibility.v1">
    <application>
      <supportedOS Id="{8e0f7a12-bfb3-4fe8-b9a5-48fd50a15a9a}"/>
      <supportedOS Id="{1f676c76-80e1-4239-95bb-83d0f6d0da78}"/>
      <supportedOS Id="{4a2f28e3-53b9-4441-ba9c-d69d4a4a6e38}"/>
      <supportedOS Id="{35138b9a-5d96-4fbd-8e2d-a2440225f93a}"/>
    </application>
  </compatibility>
  <application xmlns="urn:schemas-microsoft-com:asm.v3">
    <windowsSettings>
      <dpiAware xmlns="http://schemas.microsoft.com/SMI/2005/WindowsSettings">true/PM</dpiAware>
      <dpiAwareness xmlns="http://schemas.microsoft.com/SMI/2016/WindowsSettings">PerMonitorV2</dpiAwareness>
    </windowsSettings>
  </application>
</assembly>
"""

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="FoilPage-Desktop",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    icon=os.path.join(ROOT, "assets", "icons", "foilpage.ico"),
    version=os.path.join(ROOT, "build", "version_info_desktop.txt"),
    manifest=DPI_MANIFEST,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    name="FoilPage-Desktop",
)
