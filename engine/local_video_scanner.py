# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""Iterative local-video discovery with Windows drive and permission guards."""
from __future__ import annotations

import ctypes
import os
from dataclasses import dataclass, field
from pathlib import Path
from threading import Event
from typing import Callable, Iterable

from .media_store import VIDEO_EXTS

ScanProgress = Callable[[int, int, str], None]
_scandir = os.scandir

SKIPPED_DIRECTORY_NAMES = frozenset({
    "$recycle.bin",
    "system volume information",
    "windows",
    "program files",
    "program files (x86)",
    "programdata",
    "appdata",
    ".git",
    ".venv",
    "node_modules",
    "__pycache__",
})


@dataclass
class ScanResult:
    paths: list[str] = field(default_factory=list)
    directories_scanned: int = 0
    skipped_directories: int = 0
    errors: list[tuple[str, str]] = field(default_factory=list)
    cancelled: bool = False


def fixed_drives() -> list[str]:
    """Return mounted Windows fixed drives; fall back to the home anchor elsewhere."""
    if os.name != "nt":
        anchor = Path.home().anchor or os.path.sep
        return [anchor]
    try:
        kernel32 = ctypes.windll.kernel32
        length = kernel32.GetLogicalDriveStringsW(0, None)
        if not length:
            return []
        buffer = ctypes.create_unicode_buffer(length + 1)
        kernel32.GetLogicalDriveStringsW(length, buffer)
        drives = [value for value in buffer[:length].split("\0") if value]
        return [drive for drive in drives if kernel32.GetDriveTypeW(drive) == 3]
    except (AttributeError, OSError):
        anchor = Path.home().anchor
        return [anchor] if anchor else []


def _is_reparse_point(entry: os.DirEntry[str]) -> bool:
    if entry.is_symlink():
        return True
    attributes = getattr(entry.stat(follow_symlinks=False), "st_file_attributes", 0)
    return bool(attributes & 0x400)


def scan_local_videos(
    roots: Iterable[str | os.PathLike[str]] | None = None,
    *,
    progress: ScanProgress | None = None,
    cancel_event: Event | None = None,
) -> ScanResult:
    """Find videos without following links, junctions, or protected system trees."""
    result = ScanResult()
    stack = [os.path.abspath(os.fspath(root)) for root in (roots or fixed_drives())]
    seen_directories: set[str] = set()
    seen_files: set[str] = set()

    while stack:
        if cancel_event is not None and cancel_event.is_set():
            result.cancelled = True
            break
        current = stack.pop()
        directory_key = os.path.normcase(current)
        if directory_key in seen_directories:
            continue
        seen_directories.add(directory_key)
        try:
            entries = sorted(_scandir(current), key=lambda item: item.name.casefold())
        except OSError as exc:
            result.errors.append((current, str(exc)))
            continue

        result.directories_scanned += 1
        for entry in entries:
            if cancel_event is not None and cancel_event.is_set():
                result.cancelled = True
                break
            try:
                if _is_reparse_point(entry):
                    if entry.is_dir(follow_symlinks=True):
                        result.skipped_directories += 1
                    continue
                if entry.is_dir(follow_symlinks=False):
                    if entry.name.casefold() in SKIPPED_DIRECTORY_NAMES:
                        result.skipped_directories += 1
                    else:
                        stack.append(entry.path)
                    continue
                if (
                    entry.name.casefold().endswith(tuple(VIDEO_EXTS))
                    and entry.is_file(follow_symlinks=False)
                ):
                    path = os.path.abspath(entry.path)
                    file_key = os.path.normcase(path)
                    if file_key not in seen_files:
                        seen_files.add(file_key)
                        result.paths.append(path)
            except OSError as exc:
                result.errors.append((entry.path, str(exc)))
        if progress is not None:
            progress(result.directories_scanned, len(result.paths), current)

    result.paths.sort(key=str.casefold)
    return result
