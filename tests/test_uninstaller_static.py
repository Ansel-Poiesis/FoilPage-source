# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""卸载器无残留的静态合同（先生 2026-11-16）。

真实卸载行为由 build/vm-certify.ps1 -Uninstall 的残留断言在 VM/实机验
收；本文件锁定 installer.iss 必须携带的卸载能力，防止未来改动悄悄退化
回"没有任何卸载钩子"的旧形态。
"""
from __future__ import annotations

from pathlib import Path

ISS = Path(__file__).resolve().parents[1] / "build" / "installer.iss"


def _iss() -> str:
    return ISS.read_text(encoding="utf-8")


def test_uninstall_hook_exists_and_runs_at_both_steps() -> None:
    source = _iss()
    assert "procedure CurUninstallStepChanged" in source
    assert "usUninstall" in source, "卸载前必须杀进程/清自启值/确认数据策略"
    assert "usPostUninstall" in source, "文件删除后必须清理数据根/junction/TEMP"


def test_uninstall_kills_all_process_names_including_legacy() -> None:
    source = _iss()
    for name in (
        "FoilPage-Desktop.exe", "FoilPage-Worker.exe", "FoilPage-Agent.exe",
        "ShengJian-Agent.exe", "VideoToArticle-Desktop.exe", "VideoToArticle-Worker.exe",
    ):
        assert name in source, f"卸载杀进程清单缺少 {name}"
    # KillFoilPageProcesses 数组循环 + 卸载/升级两处调用。
    assert "taskkill /F /IM ' + Names[I]" in source
    assert source.count("KillFoilPageProcesses();") >= 2


def test_uninstall_removes_runtime_autostart_value() -> None:
    source = _iss()
    assert "RegDeleteValue(HKCU, 'Software\\Microsoft\\Windows\\CurrentVersion\\Run', 'ShengJian')" in source, (
        "运行时写入的开机自启值（app/startup.py）安装器不跟踪，卸载必须显式删除"
    )


def test_data_roots_only_deleted_after_explicit_confirmation() -> None:
    source = _iss()
    assert "DeleteUserData" in source
    assert "不可恢复" in source, "删除用户数据前必须明确警告"
    # 静默卸载（/VERYSILENT）下 MsgBox 仍会弹窗并无限等待——实装验收实测
    # 卡死；必须 UninstallSilent() 分流，静默默认保留数据。
    assert "UninstallSilent()" in source and "DeleteUserData := False" in source
    # DelTree 数据根必须由确认变量守卫；目录链接摘除用 rmdir 而非 DelTree。
    assert "if DeleteUserData then begin" in source
    assert "RemoveDirectoryLink" in source and "rmdir" in source, (
        "目录链接必须用 rmdir 摘除——DelTree 会穿进目标删真实数据"
    )


def test_junction_shim_removed_even_when_data_is_kept() -> None:
    source = _iss()
    assert "RemoveDirectoryLink(LegacyRoot);" in source
    # 链接摘除调用必须在 DeleteUserData 分支之外（无条件摘除）。
    body = source.split("usPostUninstall then begin", 1)[1]
    guard_pos = body.find("if DeleteUserData then begin")
    junction_pos = body.find("RemoveDirectoryLink(LegacyRoot);")
    assert 0 < junction_pos < guard_pos, "junction 摘除应先于且独立于数据删除分支"


def test_install_root_swept_after_logged_file_removal() -> None:
    """运行期落进 {app} 的散件不在卸载日志里，必须兜底清空安装根。"""
    source = _iss()
    assert "DelTree(ExpandConstant('{app}'), True, True, True)" in source
    assert "if DirExists(ExpandConstant('{app}')) then" in source


def test_cross_root_legacy_directory_swept_when_marked() -> None:
    """完整卸载后重装落默认根时，旧根成为跨根孤儿；带产品骨架则一并清。"""
    source = _iss()
    assert "LowerCase(ExpandConstant('{app}')) <> LowerCase(LegacyRoot)" in source
    assert "DirExists(LegacyRoot + '\_internal\app')" in source


def test_model_cache_is_never_touched() -> None:
    source = _iss()
    assert "huggingface" in source, "卸载文案必须说明模型缓存位置（不自动删除）"
    assert "DelTree" not in source.split("huggingface", 1)[1].split("\n", 1)[0]
