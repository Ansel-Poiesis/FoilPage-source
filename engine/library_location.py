# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""Shared profile location and identity checks, without opening a new library."""
from __future__ import annotations

import os
import re
import sqlite3
import sys
import uuid
from contextlib import closing
from pathlib import Path
from typing import Mapping

INITIALIZED_MARKER = ".library-initialized"
_IDENTITY = re.compile(r"[a-f0-9]{32}\Z")


class LibraryLocationError(RuntimeError):
    """The intended library cannot safely be opened at this location."""


def _local_root(environment: Mapping[str, str] | None = None) -> Path:
    env = os.environ if environment is None else environment
    if env.get("LOCALAPPDATA"):
        return Path(env["LOCALAPPDATA"]).expanduser().resolve()
    return Path(env.get("USERPROFILE", str(Path.home()))).expanduser().resolve() / "AppData" / "Local"


def _has_files(directory: Path) -> bool:
    try:
        return directory.is_dir() and next(directory.iterdir(), None) is not None
    except OSError as exc:
        raise LibraryLocationError(f"资料位置暂不可读取: {directory}") from exc


def _has_profile_data(directory: Path) -> bool:
    return (any((directory / name).exists() for name in (
        "settings.json", "library.db", INITIALIZED_MARKER, ".backup-import-recovery",
    )) or any(_has_files(directory / name) for name in ("output", "downloads", "backups")))


def default_library_root(environment: Mapping[str, str] | None = None) -> Path:
    """Keep an existing root; never rename it or choose between two real libraries."""
    root = _local_root(environment)
    current, legacy = root / "FoilPage", root / "VideoToArticle"
    try:
        if current.exists() and legacy.exists() and current.resolve() == legacy.resolve():
            return current.resolve()
        current_data, legacy_data = _has_profile_data(current), _has_profile_data(legacy)
        if current_data and legacy_data:
            raise LibraryLocationError(
                f"发现两个不同的资料目录，请明确选择要打开的资料库: {current}；{legacy}"
            )
        if legacy_data or not current.exists() and legacy.exists():
            return legacy
        return current
    except OSError as exc:
        raise LibraryLocationError("原资料目录暂不可用，请检查磁盘和访问权限") from exc


def validate_storage_path(path: str | Path, *, environment: Mapping[str, str] | None = None) -> Path:
    """Reject owned data and backup destinations inside an application install."""
    value = Path(path).expanduser()
    if not value.is_absolute():
        raise LibraryLocationError("资料位置必须是绝对路径")
    value = value.resolve()
    local = _local_root(environment)
    installs = [local / "Programs" / name for name in ("FoilPage", "VideoToArticle")]
    if getattr(sys, "frozen", False):
        binary = Path(sys.executable).resolve().parent
        installs.append(binary.parent if binary.name.casefold() in {"worker", "agent"} else binary)
    if any(value == install.resolve() or value.is_relative_to(install.resolve()) for install in installs):
        raise LibraryLocationError("资料和备份不能保存在程序安装目录，请选择其他位置")
    return value


def _identity(connection: sqlite3.Connection, *, create: bool, expected_id: str = "") -> str:
    if expected_id and (not isinstance(expected_id, str) or not _IDENTITY.fullmatch(expected_id)):
        raise LibraryLocationError("资料库身份记录无效")
    registered = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='library_identity'"
    ).fetchone()
    identity = ""
    if registered:
        rows = connection.execute("SELECT singleton,id FROM library_identity").fetchall()
        if len(rows) != 1 or rows[0][0] != 1 or not isinstance(rows[0][1], str) or not _IDENTITY.fullmatch(rows[0][1]):
            raise LibraryLocationError("资料库身份记录损坏，已停止打开")
        identity = rows[0][1]
    if expected_id and identity != expected_id:
        raise LibraryLocationError("此位置不是配置记录的资料库，已停止打开，请选择原资料位置或恢复备份")
    if create and not identity:
        identity = uuid.uuid4().hex
        connection.execute(
            "CREATE TABLE library_identity (singleton INTEGER PRIMARY KEY CHECK(singleton=1), id TEXT NOT NULL)"
        )
        connection.execute("INSERT INTO library_identity(singleton,id) VALUES (1,?)", (identity,))
    return identity


def read_library_identity(database: str | Path | sqlite3.Connection) -> str:
    """Read an existing database; old databases without an ID return an empty string."""
    if isinstance(database, sqlite3.Connection):
        return _identity(database, create=False)
    path = Path(database).expanduser().resolve()
    try:
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as connection:
            return _identity(connection, create=False)
    except (OSError, sqlite3.Error) as exc:
        raise LibraryLocationError(f"原资料库暂不可读取，已停止打开: {path}") from exc


def bind_library_identity(database: str | Path | sqlite3.Connection, expected_id: str = "") -> str:
    """Bind an already opened library; a supplied connection keeps its caller's transaction."""
    if isinstance(database, sqlite3.Connection):
        return _identity(database, create=True, expected_id=expected_id)
    path = validate_storage_path(database)
    try:
        with closing(sqlite3.connect(path.as_uri() + "?mode=rw", uri=True)) as connection:
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                return _identity(connection, create=True, expected_id=expected_id)
    except (OSError, sqlite3.Error) as exc:
        raise LibraryLocationError(f"资料库身份无法保存，已停止打开: {path}") from exc


def preflight_library(settings_store, settings: Mapping) -> None:
    """Check before token, directory or SQLite creation; perform no writes."""
    base = validate_storage_path(settings_store.base_dir)
    paths = {name: validate_storage_path(settings[name]) for name in (
        "database_path", "output_dir", "download_dir", "browser_download_dir",
    )}
    if settings.get("model_dir"):
        validate_storage_path(settings["model_dir"])
    initialized = (base / INITIALIZED_MARKER).exists()
    if not settings_store.path.is_file() and (initialized or _has_profile_data(base)):
        raise LibraryLocationError("原资料库的设置文件缺失，已停止创建空库，请恢复设置或备份")
    expected = settings.get("library_id", "")
    database = paths["database_path"]
    if database.is_file():
        identity = read_library_identity(database)
        if expected and identity != expected:
            raise LibraryLocationError("此位置不是配置记录的资料库，已停止打开，请选择原资料位置或恢复备份")
        return
    previous_data = (initialized or bool(expected) or bool(settings.get("setup_completed"))
                     or bool(settings.get("onboarding_completed_version"))
                     or any(_has_files(paths[name]) for name in ("output_dir", "download_dir")))
    if database.exists() or previous_data:
        raise LibraryLocationError(f"原资料库暂不可用，已停止创建空库: {database}")


def mark_library_initialized(settings_store) -> None:
    """Keep only an existence receipt, so deleting settings cannot look like first use."""
    marker = validate_storage_path(settings_store.base_dir) / INITIALIZED_MARKER
    with marker.open("ab"):
        pass
