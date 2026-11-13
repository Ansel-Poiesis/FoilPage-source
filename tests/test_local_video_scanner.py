# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""Local video discovery and progressive batch-import tests."""
from __future__ import annotations

import os
import sys
import tempfile
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import engine.local_video_scanner as scanner  # noqa: E402
from engine.media_store import MediaStore  # noqa: E402


def test_scan_finds_videos_and_skips_heavy_directories() -> None:
    with tempfile.TemporaryDirectory(prefix="local_video_scan_") as temp:
        root = Path(temp)
        nested = root / "Media" / "Trips"
        nested.mkdir(parents=True)
        first = root / "first.MP4"
        second = nested / "second.mkv"
        first.write_bytes(b"video-one")
        second.write_bytes(b"video-two")
        (nested / "notes.txt").write_text("ignore", encoding="utf-8")
        ignored = root / "node_modules"
        ignored.mkdir()
        (ignored / "hidden.mov").write_bytes(b"hidden")
        progress = []

        result = scanner.scan_local_videos(
            [root, root],
            progress=lambda directories, found, path: progress.append(
                (directories, found, path)
            ),
        )

        assert [str(Path(path).resolve()) for path in result.paths] == sorted(
            [str(first.resolve()), str(second.resolve())], key=str.casefold
        )
        assert result.skipped_directories == 1
        assert result.directories_scanned == 3
        assert progress and progress[-1][0] == 3


def test_scan_does_not_follow_directory_links() -> None:
    with tempfile.TemporaryDirectory(prefix="local_video_link_") as temp:
        root = Path(temp)
        target = root / "target"
        target.mkdir()
        (target / "linked.mp4").write_bytes(b"linked")
        link = root / "alias"
        try:
            link.symlink_to(target, target_is_directory=True)
        except OSError:
            pytest.skip("当前 Windows 策略不允许创建目录链接")

        result = scanner.scan_local_videos([root])

        # 安全不变量优先于“是否识别链接”的实现细节：无论本机 OS 能否把目录
        # 符号链接识别为 reparse 点（is_symlink / 0x400 属性），扫描都必须终止、
        # 不重复、且不遗漏目标文件。seen_directories/seen_files 负责防环与去重。
        assert result.cancelled is False
        assert [Path(path).resolve() for path in result.paths] == [(target / "linked.mp4").resolve()]
        assert len(result.paths) == len(
            {os.path.normcase(p) for p in result.paths}
        ), "链接不应导致重复文件"
        # 链接不应触发失控递归：扫描目录数有上界（root + target + 可能的 link）
        assert result.directories_scanned <= 3
        # 若本机 OS 能识别该目录链接，扫描器应计数跳过
        alias_entry = next(e for e in os.scandir(str(root)) if e.name == "alias")
        if scanner._is_reparse_point(alias_entry):
            assert result.skipped_directories >= 1


def test_scan_records_permission_error_and_can_cancel(monkeypatch) -> None:
    with tempfile.TemporaryDirectory(prefix="local_video_permission_") as temp:
        root = Path(temp)
        blocked = root / "blocked"
        blocked.mkdir()
        (blocked / "private.mp4").write_bytes(b"private")
        visible = root / "visible.mp4"
        visible.write_bytes(b"visible")
        original_scandir = scanner._scandir

        def guarded_scandir(path):
            if Path(path).resolve() == blocked.resolve():
                raise PermissionError("denied for test")
            return original_scandir(path)

        monkeypatch.setattr(scanner, "_scandir", guarded_scandir)
        result = scanner.scan_local_videos([root])
        assert [Path(path).resolve() for path in result.paths] == [visible.resolve()]
        assert result.errors and Path(result.errors[0][0]).resolve() == blocked.resolve()

        cancelled = threading.Event()
        cancelled.set()
        stopped = scanner.scan_local_videos([root], cancel_event=cancelled)
        assert stopped.cancelled is True
        assert stopped.directories_scanned == 0


def test_progressive_batch_import_reports_new_and_existing_files() -> None:
    with tempfile.TemporaryDirectory(prefix="local_video_import_") as temp:
        root = Path(temp)
        first = root / "first.mp4"
        second = root / "second.mov"
        first.write_bytes(b"first-content")
        second.write_bytes(b"second-content")
        store = MediaStore(str(root / "library.db"))
        events = []
        try:
            initial = store.import_paths(
                [str(first), str(second)],
                progress=lambda index, total, path: events.append((index, total, path)),
            )
            repeated = store.import_paths([str(first), str(second)])
            assert initial.created == 2
            assert repeated.existing == 2
            assert [event[:2] for event in events] == [(1, 2), (2, 2)]
        finally:
            store.close()
