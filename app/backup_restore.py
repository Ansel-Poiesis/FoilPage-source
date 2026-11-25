# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""Publish a backup as one recoverable set of text, SQLite data and settings.

The recovery directory belongs to this installation, never to an input archive.
A prepared journal always means rollback; only the durable committed marker
permits keeping the imported set. Startup must recover before opening stores.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import stat
import tempfile
from contextlib import closing
from pathlib import Path

RECOVERY_DIRECTORY = ".backup-import-recovery"


class BackupRestoreError(RuntimeError):
    """An interrupted import cannot safely finish recovery yet."""


def _is_link(path: Path) -> bool:
    return path.is_symlink() or bool(
        path.exists() and getattr(path.stat(), "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    )


def _regular_target(path: Path) -> None:
    if _is_link(path) or path.exists() and not path.is_file():
        raise BackupRestoreError("恢复目标必须是普通文件")


def _text_target(root: Path, relative: str) -> Path:
    item = Path(relative)
    if (not relative or item.anchor or ".." in item.parts or item == Path(".")
            or any(":" in part or Path(part).is_reserved() for part in item.parts)):
        raise BackupRestoreError("恢复日志包含不安全的文本路径")
    target = root / item
    cursor = target
    while cursor != root:
        if _is_link(cursor):
            raise BackupRestoreError("恢复目标包含链接或重解析目录")
        cursor = cursor.parent
    _regular_target(target)
    return target


def _copy_durable(source: Path, target: Path) -> None:
    with source.open("rb") as src, target.open("xb") as dst:
        shutil.copyfileobj(src, dst)
        dst.flush()
        os.fsync(dst.fileno())
    shutil.copystat(source, target)


def _publish_file(source: Path, target: Path) -> None:
    """Atomic per-file publication, including when the output is on another disk."""
    _regular_target(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".backup-import-", dir=target.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as dst, source.open("rb") as src:
            shutil.copyfileobj(src, dst)
            dst.flush()
            os.fsync(dst.fileno())
        shutil.copystat(source, temporary)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def _backup_database(source: Path, target: Path) -> None:
    if not source.is_file():
        raise BackupRestoreError("数据库恢复快照不存在")
    _regular_target(target)
    with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as src, closing(sqlite3.connect(str(target))) as dst:
        src.backup(dst)


def _write_journal(directory: Path, journal: dict) -> None:
    temporary = directory / "journal.tmp"
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(journal, stream, ensure_ascii=False)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, directory / "journal.json")


def _same_contents(first: Path, second: Path) -> bool:
    if not second.is_file() or first.stat().st_size != second.stat().st_size:
        return False
    return _file_digest(first) == _file_digest(second)


def _file_digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _valid_digest(value) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def _recovery_directory(settings_store) -> Path:
    directory = settings_store.base_dir.resolve() / RECOVERY_DIRECTORY
    if _is_link(directory):
        raise BackupRestoreError("备份恢复目录不可为链接")
    return directory


def _load_journal(directory: Path, settings_store) -> dict:
    try:
        journal = json.loads((directory / "journal.json").read_text(encoding="utf-8"))
        if (type(journal["version"]) is not int or journal["version"] != 1
                or journal["phase"] not in {"prepared", "committed", "rolled_back"}
                or Path(journal["settings"]) != settings_store.path.resolve()
                or type(journal["settings_existed"]) is not bool
                or not isinstance(journal["files"], list)
                or not _valid_digest(journal["database_sha256"])
                or (not _valid_digest(journal["settings_sha256"]) if journal["settings_existed"]
                    else journal["settings_sha256"] is not None)):
            raise ValueError("invalid journal")
        for key in ("database", "output"):
            root = Path(journal[key])
            if not root.is_absolute() or root.resolve() != root:
                raise ValueError("invalid root")
        output = Path(journal["output"])
        seen = set()
        for item in journal["files"]:
            if (set(item) != {"path", "existed", "sha256"}
                    or type(item["existed"]) is not bool
                    or (not _valid_digest(item["sha256"]) if item["existed"] else item["sha256"] is not None)):
                raise ValueError("invalid file record")
            target = _text_target(output, item["path"])
            _check_reserved_target(target, directory, Path(journal["database"]), settings_store.path.resolve())
            if target in seen:
                raise ValueError("duplicate file record")
            seen.add(target)
        return journal
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise BackupRestoreError("备份恢复日志无法验证，请保留恢复目录并先修复，当前实例已停止启动") from exc


def _check_reserved_target(target: Path, directory: Path, database: Path, settings: Path) -> None:
    reserved = {database, Path(str(database) + "-wal"), Path(str(database) + "-shm"), settings}
    if target in reserved or target.is_relative_to(directory):
        raise BackupRestoreError("备份文本不能覆盖数据库、设置或本机恢复日志")


def _rollback(directory: Path, journal: dict) -> None:
    # Validate the complete local undo set before changing anything. A missing
    # or damaged snapshot must never silently become an empty SQLite database
    # or overwrite an otherwise recoverable current file.
    snapshots = [(directory / "library.original.db", journal["database_sha256"])]
    if journal["settings_existed"]:
        snapshots.append((directory / "settings.original", journal["settings_sha256"]))
    snapshots.extend((directory / "original" / str(index), item["sha256"])
                     for index, item in enumerate(journal["files"]) if item["existed"])
    for path, digest in snapshots:
        try:
            if _is_link(path) or not path.is_file() or _file_digest(path) != digest:
                raise ValueError("snapshot mismatch")
        except (OSError, ValueError) as exc:
            raise BackupRestoreError("本机备份恢复快照缺失或损坏，已停止恢复；请保留恢复目录与导入前备份") from exc
    failures = []
    settings = Path(journal["settings"])
    try:
        _regular_target(settings)
        if journal["settings_existed"]:
            original = directory / "settings.original"
            if not _same_contents(original, settings):
                _publish_file(original, settings)
        else:
            settings.unlink(missing_ok=True)
    except Exception as exc:
        failures.append(exc)
    try:
        _backup_database(directory / "library.original.db", Path(journal["database"]))
    except Exception as exc:
        failures.append(exc)
    for index, item in reversed(list(enumerate(journal["files"]))):
        try:
            target = _text_target(Path(journal["output"]), item["path"])
            if item["existed"]:
                original = directory / "original" / str(index)
                # A locked file whose replacement failed may still be unchanged.
                if not _same_contents(original, target):
                    _publish_file(original, target)
            else:
                target.unlink(missing_ok=True)
        except Exception as exc:
            failures.append(exc)
    if failures:
        raise BackupRestoreError("备份导入尚未恢复完整，已保留本机恢复记录；请解除文件占用后重启，恢复完成前禁止继续操作") from failures[0]


def _rollback_and_mark(directory: Path, journal: dict) -> None:
    _rollback(directory, journal)
    try:
        _write_journal(directory, {**journal, "phase": "rolled_back"})
    except Exception as exc:
        raise BackupRestoreError("资料已回滚，但恢复完成记录尚未保存；请保留本机恢复目录并重启后继续") from exc


def recover_interrupted_import(settings_store) -> bool:
    """Recover before loading settings/opening DB; raise if the set is still unsafe."""
    directory = _recovery_directory(settings_store)
    if not directory.exists():
        return False
    if (directory / "journal.json").exists():
        journal = _load_journal(directory, settings_store)
        if journal["phase"] == "prepared":
            _rollback_and_mark(directory, journal)
    # No journal means preparation never reached the first publication.
    shutil.rmtree(directory, ignore_errors=True)
    return True


def apply_backup_import(settings_store, database: Path, output: Path,
                        incoming_database: Path, incoming_output: Path, settings: dict) -> None:
    """Caller holds all application/store locks until publish or rollback finishes."""
    directory = _recovery_directory(settings_store)
    if directory.exists():
        raise BackupRestoreError("本机仍有待恢复的备份导入，请先重启完成恢复")
    database, output = database.resolve(), output.resolve()
    settings_path = settings_store.path.resolve()
    files = []
    for source in sorted(incoming_output.rglob("*")) if incoming_output.is_dir() else []:
        if source.is_file():
            relative = source.relative_to(incoming_output).as_posix()
            target = _text_target(output, relative)
            _check_reserved_target(target, directory, database, settings_path)
            files.append((source, relative, target))
    directory.mkdir(parents=True)
    journal = {"version": 1, "phase": "prepared", "database": str(database), "output": str(output),
               "settings": str(settings_path), "settings_existed": settings_path.exists(),
               "database_sha256": None, "settings_sha256": None, "files": []}
    prepared = False
    try:
        (directory / "original").mkdir()
        (directory / "incoming").mkdir()
        _backup_database(database, directory / "library.original.db")
        journal["database_sha256"] = _file_digest(directory / "library.original.db")
        if journal["settings_existed"]:
            _copy_durable(settings_path, directory / "settings.original")
            journal["settings_sha256"] = _file_digest(directory / "settings.original")
        for index, (source, relative, target) in enumerate(files):
            existed = target.is_file()
            if existed:
                _copy_durable(target, directory / "original" / str(index))
            _copy_durable(source, directory / "incoming" / str(index))
            journal["files"].append({"path": relative, "existed": existed,
                                     "sha256": _file_digest(directory / "original" / str(index)) if existed else None})
        _write_journal(directory, journal)
        prepared = True
        for index, (_, _, target) in enumerate(files):
            _publish_file(directory / "incoming" / str(index), target)
        _backup_database(incoming_database, database)
        settings_store.save(settings)
        journal["phase"] = "committed"
        _write_journal(directory, journal)
    except Exception:
        if prepared:
            _rollback_and_mark(directory, journal)
        shutil.rmtree(directory, ignore_errors=True)
        raise
    # A failed cleanup is harmless after commit; startup can remove this journal.
    shutil.rmtree(directory, ignore_errors=True)
