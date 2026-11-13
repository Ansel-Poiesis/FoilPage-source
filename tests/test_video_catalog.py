# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
from __future__ import annotations

import os
import time

import pytest

from engine.media_store import MediaStore
from engine.video_catalog import VideoCatalog, scan_roots


def video(path, content=b"synthetic-video"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    os.utime(path, (time.time() - 10, time.time() - 10))
    return path


def done(catalog):
    deadline = time.monotonic() + 5
    while catalog.snapshot()["running"] and time.monotonic() < deadline:
        time.sleep(0.005)
    assert not catalog.snapshot()["running"]
    assert catalog.snapshot()["state"] == "done"
    return catalog.snapshot()


def test_default_and_explicit_folders_incremental_restart_no_rehash(tmp_path, monkeypatch):
    from engine import media_store
    root, extra = tmp_path / "downloads", tmp_path / "extra"
    first = video(root / "one.mp4")
    video(extra / "two.MP4", b"different-video")
    video(root / "child" / "nested.mp4", b"nested-video")
    store = MediaStore(str(tmp_path / "library.db"))
    settings = {"download_dir": str(root), "video_scan_directories": [str(extra)]}
    catalog = VideoCatalog(store)
    try:
        catalog.request_scan(settings)
        state = done(catalog)
        assert state["created"] == 2
        assert len(store.list_media()) == 2
        monkeypatch.setattr(media_store, "_sha256", lambda path: pytest.fail("unchanged content must not be read"))
        catalog.request_scan(settings, force=True)
        assert done(catalog)["unchanged"] == 2
        catalog.close()
        catalog = VideoCatalog(store)
        catalog.request_scan(settings)
        assert done(catalog)["unchanged"] == 2
        # A missing source is retained as an unavailable record.
        original_mtime_ns = first.stat().st_mtime_ns
        first.unlink()
        catalog.request_scan(settings, force=True)
        done(catalog)
        assert len(store.list_media()) == 2
        missing = store.list_media(missing=True)
        assert len(missing) == 1
        original_id = missing[0]["id"]
        # Keep the source's exact size and timestamp so the in-process cache
        # path is exercised when the same file returns.
        first.write_bytes(b"synthetic-video")
        os.utime(first, ns=(original_mtime_ns, original_mtime_ns))
        catalog.request_scan(settings, force=True)
        done(catalog)
        restored = store.get_media(original_id)
        assert restored["missing"] is False
        assert restored["id"] == original_id
        assert len(store.list_media(missing=True)) == 0
    finally:
        catalog.close()
        store.close()


def test_recursive_overlap_exclusion_and_file_bound(tmp_path):
    root = tmp_path / "downloads"
    video(root / "a.mp4", b"a")
    video(root / "child" / "b.mp4", b"b")
    video(root / ".partial.mp4", b"partial")
    video(root / "zero.mp4", b"")
    video(root / "active.mp4", b"active")
    store = MediaStore(str(tmp_path / "library.db"))
    catalog = VideoCatalog(store)
    settings = {"download_dir": str(root), "video_scan_directories": [str(root), str(root / "child")], "video_scan_recursive": True}
    try:
        catalog.request_scan(settings, exclude_paths=[root / "active.mp4"])
        result = done(catalog)
        assert result["visited"] == 5
        assert result["created"] == 2
        assert result["skipped"] == 3
        assert len(scan_roots(settings)) == 2
        catalog.close()
        catalog = VideoCatalog(store, max_files=1)
        catalog.request_scan(settings)
        assert done(catalog)["limited"] is True
    finally:
        catalog.close()
        store.close()


def test_no_ui_wait_for_hashing_and_request_throttle(tmp_path, monkeypatch):
    import threading
    store = MediaStore(str(tmp_path / "library.db"))
    root = tmp_path / "downloads"
    video(root / "a.mp4")
    entered, release = threading.Event(), threading.Event()
    real = store.import_file
    def blocked(*args, **kwargs):
        entered.set()
        assert release.wait(3)
        return real(*args, **kwargs)
    monkeypatch.setattr(store, "import_file", blocked)
    catalog = VideoCatalog(store)
    try:
        before = time.monotonic()
        result = catalog.request_scan({"download_dir": str(root)})
        assert time.monotonic() - before < .5 and result["running"]
        assert entered.wait(2)
        revision = catalog.snapshot()["revision"]
        catalog.request_scan({"download_dir": str(root)}, force=True)
        assert catalog.snapshot()["revision"] == revision
        release.set()
        done(catalog)
        revision = catalog.snapshot()["revision"]
        catalog.request_scan({"download_dir": str(root)})
        assert catalog.snapshot()["revision"] == revision
    finally:
        release.set()
        catalog.close()
        store.close()


def test_mutating_media_during_hash_does_not_enter_library(tmp_path, monkeypatch):
    from engine import media_store
    path = video(tmp_path / "a.mp4", b"before")
    original = media_store._sha256
    def change(candidate):
        value = original(candidate)
        candidate.write_bytes(b"different after hash")
        return value
    monkeypatch.setattr(media_store, "_sha256", change)
    store = MediaStore(str(tmp_path / "library.db"))
    try:
        with pytest.raises(media_store.MediaStoreError):
            store.import_file(str(path))
        assert store.list_media() == []
    finally:
        store.close()


def test_cancelled_hash_never_registers_media(tmp_path):
    import threading
    from engine.media_store import MediaImportCancelled
    path = video(tmp_path / "a.mp4", b"content")
    stop = threading.Event(); stop.set()
    store = MediaStore(str(tmp_path / "library.db"))
    try:
        with pytest.raises(MediaImportCancelled): store.import_file(str(path), cancel_event=stop)
        assert store.list_media() == []
    finally:
        store.close()
