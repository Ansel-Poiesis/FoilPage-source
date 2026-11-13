# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""Bounded, incremental discovery of explicitly selected local video folders.

Listing requests never wait for traversal, full-file hashing or media probes.
Discovery registers media only: no jobs, providers, file moves or deletion.
"""
from __future__ import annotations

import os
import stat
import threading
import time
from pathlib import Path
from typing import Mapping

from .media_store import MediaImportCancelled, VIDEO_EXTS, MediaStoreError


def _ordinary(path: Path) -> bool:
    try:
        info = path.lstat()
        return not stat.S_ISLNK(info.st_mode) and not getattr(info, "st_file_attributes", 0) & 0x400
    except OSError:
        return False


def scan_roots(settings: Mapping) -> tuple[Path, ...]:
    values = [settings.get("download_dir", ""), *settings.get("video_scan_directories", [])]
    roots: list[Path] = []
    for value in values:
        if not isinstance(value, str) or not value.strip():
            continue
        path = Path(value).expanduser().absolute()
        if not path.is_absolute() or not _ordinary(path):
            continue
        # Reject symlink/junction parents rather than discovering files outside
        # the user's explicitly chosen tree through a reparse alias.
        if any(not _ordinary(parent) for parent in path.parents):
            continue
        path = path.resolve()
        if not path.is_dir() or path in roots:
            continue
        roots.append(path)
    return tuple(roots)


class VideoCatalog:
    def __init__(self, media_store, *, clock=time.monotonic, wall_clock=time.time,
                 min_interval: float = 30.0, max_files: int = 20000, max_depth: int = 12):
        self.media = media_store
        self.clock, self.wall_clock = clock, wall_clock
        self.min_interval, self.max_files, self.max_depth = min_interval, max_files, max_depth
        self._lock = threading.RLock()
        self._closed = threading.Event()
        self._scan_cancel = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_request = float("-inf")
        self._configuration = None
        self._seen: dict[str, tuple[int, int, str]] = {}
        self._state = {"state": "idle", "running": False, "visited": 0, "created": 0,
                       "unchanged": 0, "skipped": 0, "errors": 0, "roots_count": 0,
                       "revision": 0, "started_at": None, "finished_at": None, "limited": False}

    def snapshot(self) -> dict:
        with self._lock:
            return dict(self._state)

    def request_scan(self, settings: Mapping, *, force: bool = False, exclude_paths=()) -> dict:
        # Resolve paths in the worker too; avoid network-mounted directories
        # making a UI-facing request wait for filesystem availability.
        values = (str(settings.get("download_dir", "")), tuple(settings.get("video_scan_directories", [])),
                  settings.get("video_scan_recursive", False) is True)
        excluded = {os.path.normcase(os.path.abspath(str(p))) for p in exclude_paths if p}
        with self._lock:
            if self._closed.is_set() or self._state["running"]:
                return dict(self._state)
            if not force and values == self._configuration and self.clock() - self._last_request < self.min_interval:
                return dict(self._state)
            self._last_request, self._configuration = self.clock(), values
            self._scan_cancel = threading.Event()
            self._state.update(state="scanning", running=True, visited=0, created=0, unchanged=0,
                               skipped=0, errors=0, roots_count=0, started_at=self.wall_clock(),
                               finished_at=None, limited=False)
            self._state["revision"] += 1
            frozen = {"download_dir": values[0], "video_scan_directories": values[1], "video_scan_recursive": values[2]}
            self._thread = threading.Thread(target=self._scan,
                                            args=(frozen, excluded, self._scan_cancel), daemon=True,
                                            name="shengjian-video-catalog")
            self._thread.start()
            return dict(self._state)

    def cancel_scan(self) -> dict:
        """Stop the current discovery pass without removing its partial imports."""
        with self._lock:
            if self._state["running"] and not self._closed.is_set():
                self._scan_cancel.set()
                self._state["state"] = "cancelling"
                self._state["revision"] += 1
            return dict(self._state)

    def _tick(self, **changes):
        with self._lock:
            for key, value in changes.items():
                self._state[key] += value
            self._state["revision"] += 1

    def _scan(self, settings, excluded, cancel_event):
        visited: set[str] = set()
        try:
            roots = scan_roots(settings)
            with self._lock:
                self._state["roots_count"] = len(roots)
            for root in roots:
                pending = [(root, 0)]
                while pending and not cancel_event.is_set():
                    folder, depth = pending.pop()
                    try:
                        with os.scandir(folder) as entries:
                            for entry in entries:
                                if cancel_event.is_set():
                                    break
                                path = Path(entry.path)
                                if not _ordinary(path):
                                    self._tick(skipped=1)
                                    continue
                                if entry.is_dir(follow_symlinks=False):
                                    if settings["video_scan_recursive"] and depth < self.max_depth:
                                        pending.append((path, depth + 1))
                                    continue
                                if not entry.is_file(follow_symlinks=False) or path.suffix.lower() not in VIDEO_EXTS:
                                    continue
                                key = os.path.normcase(str(path.absolute()))
                                if key in visited:
                                    continue
                                visited.add(key)
                                self._tick(visited=1)
                                if len(visited) > self.max_files:
                                    with self._lock:
                                        self._state["limited"] = True
                                    pending.clear()
                                    break
                                info = entry.stat(follow_symlinks=False)
                                if key in excluded or path.name.startswith(".") or info.st_size <= 0 or self.wall_clock() - info.st_mtime < 2:
                                    self._tick(skipped=1)
                                    continue
                                old = self._seen.get(key)
                                if old and old[:2] == (info.st_size, info.st_mtime_ns):
                                    cached = None
                                    try:
                                        cached = self.media.get_media(old[2])
                                    except MediaStoreError:
                                        pass
                                    # A complete prior scan may have marked this row missing.
                                    # If the file returned with the same size/mtime, let the
                                    # store fast path clear that flag instead of hiding it.
                                    if cached is not None and not cached["missing"]:
                                        self._tick(unchanged=1)
                                        continue
                                try:
                                    result = self.media.import_file(
                                        str(path),
                                        fast_if_unchanged=True,
                                        cancel_event=cancel_event,
                                    )
                                except MediaImportCancelled:
                                    break
                                except (OSError, MediaStoreError, ValueError):
                                    self._tick(errors=1)
                                    continue
                                self._seen[key] = (info.st_size, info.st_mtime_ns, result.item["id"])
                                self._tick(**{"created" if result.created else "unchanged": 1})
                    except OSError:
                        self._tick(errors=1)
                if len(visited) > self.max_files or cancel_event.is_set():
                    break
            # Keep records even when files disappear. Availability is metadata,
            # not authority to remove tasks, documents or original media.
            if not cancel_event.is_set():
                self.media.refresh_missing()
            with self._lock:
                state = "cancelled" if cancel_event.is_set() else "done"
                self._state.update(state=state, running=False, finished_at=self.wall_clock())
                self._state["revision"] += 1
        except Exception:
            with self._lock:
                state = "cancelled" if cancel_event.is_set() else "error"
                self._state.update(state=state, running=False, finished_at=self.wall_clock())
                if state == "error":
                    self._state["errors"] += 1
                self._state["revision"] += 1

    def close(self, *, timeout: float = 5.0) -> bool:
        with self._lock:
            self._closed.set()
            self._scan_cancel.set()
            thread = self._thread
        if thread is not None:
            thread.join(timeout=max(0.0, timeout))
        return thread is None or not thread.is_alive()
