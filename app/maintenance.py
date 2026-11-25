# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""配置备份/迁移与诊断包导出（底二层）。"""
from __future__ import annotations

import io
import hashlib
import json
import os
import platform
import shutil
import sqlite3
import stat
import tempfile
import zipfile
import re
from contextlib import closing, contextmanager
from datetime import datetime
from pathlib import Path


from engine.settings import SECRET_FIELDS as _SETTINGS_SECRET_FIELDS, default_logs_dir
from engine.queue_store import SCHEMA_VERSION
from app.backup_restore import (
    BackupRestoreError, RECOVERY_DIRECTORY, apply_backup_import,
)

TEXT_SUFFIXES = frozenset({".md", ".txt", ".json", ".srt", ".vtt"})
LOG_TAIL_LINES = 500
MANIFEST_ARTIFACT_PATHS = ("segments_path", "translation_path", "translation_segments_path")
BACKUP_FORMAT_VERSION = 2
_BACKUP_EXCLUDED_PARTS = (".text-delete-", ".deletion-cache-", RECOVERY_DIRECTORY)


class _MissingBackupFiles(ValueError):
    def __init__(self, paths: list[str]) -> None:
        self.paths = paths
        super().__init__(f"备份缺少 {len(paths)} 个已登记文本文件；请先找回文件或修复资料引用，原备份未替换")


def _database_text_paths(connection) -> list[tuple[str, str]]:
    """Use the same live text fields as the library, including older databases."""
    paths = [(job_id, value) for job_id, raw, final in connection.execute(
        "SELECT id, raw_path, final_path FROM jobs") for value in (raw, final) if value]
    columns = {row[1] for row in connection.execute("PRAGMA table_info(manifests)")}
    metadata_column = "metadata_json" if "metadata_json" in columns else "'{}'"
    for job_id, raw, final, payload in connection.execute(
        f"SELECT job_id, raw_path, final_path, {metadata_column} FROM manifests"
    ):
        metadata = json.loads(payload or "{}")
        if not isinstance(metadata, dict):
            raise ValueError("文稿元数据必须是 JSON object")
        if metadata.get("text_outputs_cleared"):
            continue
        paths.extend((job_id, value) for value in (raw, final) if value)
        paths.extend((job_id, metadata[key]) for key in MANIFEST_ARTIFACT_PATHS if metadata.get(key))
    return paths


@contextmanager
def _backup_snapshot(controller, settings_store):
    """Block desktop edits and new Worker claims while copying the complete set."""
    with (controller.media_action_lock, controller._settings_lock, settings_store._lock,
          controller.media._lock, controller.queue._lock, controller.downloads._lock):
        if getattr(controller, "_backup_restore_blocked", False):
            raise ValueError("备份恢复尚未完成，请先重启完成恢复")
        database = Path(str(controller.settings["database_path"])).expanduser().resolve()
        with closing(sqlite3.connect(database.as_uri() + "?mode=rw", uri=True, timeout=2)) as gate:
            gate.execute("BEGIN IMMEDIATE")
            try:
                if gate.execute("SELECT 1 FROM jobs WHERE status='running' LIMIT 1").fetchone():
                    raise ValueError("请先停止 Worker，再导出完整备份")
                if gate.execute("SELECT 1 FROM download_jobs WHERE status IN "
                                "('resolving','awaiting_browser','downloading','imported','transcribing') LIMIT 1").fetchone():
                    raise ValueError("请先完成或取消进行中的下载，再导出完整备份")
                if gate.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='postprocess_runs'").fetchone():
                    if gate.execute("SELECT 1 FROM postprocess_runs WHERE status='running' LIMIT 1").fetchone():
                        raise ValueError("请先等待翻译或批量整理结束，再导出完整备份")
                yield gate
            finally:
                gate.rollback()


def _plain_backup_file(path: Path) -> None:
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)
            or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
        raise ValueError(f"备份文本必须是普通文件，不能是链接: {path}")


def _file_hash(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _backup_text_inventory(controller, connection) -> tuple[dict[str, Path], list[dict], dict[str, str]]:
    current = Path(str(controller.settings["output_dir"])).expanduser().absolute()
    jobs = connection.execute("SELECT id, output_dir FROM jobs").fetchall()
    original_roots = {current}
    scan_directories = {current.resolve()}
    for job_id, value in jobs:
        if not isinstance(value, str) or not value or not Path(value).expanduser().is_absolute():
            raise ValueError("任务输出目录必须是本机绝对路径")
        root = Path(value).expanduser().absolute()
        original_roots.add(root)
        if not root.resolve().is_relative_to(current.resolve()):
            if not isinstance(job_id, str) or Path(job_id).name != job_id or job_id in {".", ".."}:
                raise ValueError("任务身份不能作为输出子目录使用")
            scan_directories.add(root.resolve() / job_id)
    references = _database_text_paths(connection)
    missing = []
    job_records = {job_id: {"id": job_id, "output_dir": value} for job_id, value in jobs}
    for job_id, value in references:
        if not isinstance(value, str) or not Path(value).expanduser().is_absolute():
            raise ValueError("已登记文本路径必须是本机绝对路径")
        path = Path(value).expanduser()
        if not path.is_file():
            missing.append(value)
        elif not controller.is_available_job_output(job_records.get(job_id, {}), path, current):
            raise ValueError(f"已登记文本不在当前输出根或所属任务目录: {path}")
    if missing:
        raise _MissingBackupFiles(sorted(set(missing)))

    current_real = current.resolve()
    roots = {}
    for root in sorted(original_roots, key=str):
        real = root.resolve()
        destination = (real.relative_to(current_real).as_posix() if real.is_relative_to(current_real)
                       else ".foilpage-history/" + hashlib.sha256(str(real).casefold().encode("utf-8")).hexdigest()[:16])
        if destination == ".":
            destination = ""
        roots[root] = (real, destination)
        roots[real] = (real, destination)

    files: dict[str, Path] = {}
    sources: dict[Path, str] = {}
    reserved = {Path(controller.settings["database_path"]).expanduser().resolve(),
                controller.settings_store.path.resolve()}

    def include(path):
        _plain_backup_file(path)
        real = path.resolve()
        if real in reserved:
            return
        matching = [(root, destination) for root, destination in roots.values() if real.is_relative_to(root)]
        root, destination = max(matching, key=lambda item: len(item[0].parts))
        relative = real.relative_to(root)
        if any(part.startswith(_BACKUP_EXCLUDED_PARTS) for part in relative.parts):
            return
        # Never walk a nested junction or symlink to collect unrelated files.
        boundaries = [candidate for candidate in roots if path.absolute().is_relative_to(candidate)]
        boundary = max(boundaries, key=lambda candidate: len(candidate.parts))
        cursor = path.parent
        while cursor != boundary and cursor != cursor.parent:
            info = cursor.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                raise ValueError(f"备份文本目录包含链接: {path}")
            cursor = cursor.parent
        name = "output/" + "/".join(part for part in (destination, relative.as_posix()) if part)
        if name in files and files[name].resolve() != real:
            raise ValueError("不同输出目录的备份文件名冲突，请保留现场并调整输出目录")
        files[name] = path
        sources[real] = name

    for root in sorted(scan_directories, key=str):
        if root.is_dir():
            for path in sorted(root.rglob("*")):
                if any(part.startswith(_BACKUP_EXCLUDED_PARTS) for part in path.relative_to(root).parts):
                    continue
                if path.is_file() and path.suffix.lower() in TEXT_SUFFIXES:
                    include(path)
    for _, value in references:
        include(Path(value).expanduser())
    path_map = {value: sources[Path(value).expanduser().resolve()] for _, value in references}
    output_roots = [{"source": str(root), "destination": destination}
                    for root, (_, destination) in sorted(roots.items(), key=lambda item: str(item[0]))]
    return files, output_roots, path_map


def _verify_archive(zip_file) -> dict | None:
    """Legacy archives remain readable; new archives verify every member first."""
    names = [item.filename for item in zip_file.infolist() if not item.is_dir()]
    if len(names) != len(set(name.casefold() for name in names)):
        raise ValueError("备份包含重复文件名")
    if "backup_manifest.json" not in names:
        return None
    manifest = json.loads(zip_file.read("backup_manifest.json"))
    if not isinstance(manifest, dict):
        raise ValueError("备份清单必须是 JSON object")
    if "format_version" not in manifest:
        if {"files", "references", "output_roots", "kind", "library_id", "complete", "includes_media"} & manifest.keys():
            raise ValueError("备份清单缺少格式版本，不能按旧备份导入")
        return None
    if manifest.get("format_version") != BACKUP_FORMAT_VERSION or manifest.get("kind") != "document_backup":
        raise ValueError("备份格式版本不受支持，请升级 FoilPage 后再导入")
    if manifest.get("complete") is not True or manifest.get("includes_media") is not False:
        raise ValueError("备份必须是完整文稿备份，不能将媒体清单当作完整文件")
    if not isinstance(manifest.get("library_id"), str) or not re.fullmatch(r"[a-f0-9]{32}", manifest["library_id"]):
        raise ValueError("备份缺少有效资料库身份")
    files = manifest.get("files")
    if not isinstance(files, dict) or set(files) != set(names) - {"backup_manifest.json"}:
        raise ValueError("备份文件清单不完整")
    for name, expected in files.items():
        if (not isinstance(expected, dict) or type(expected.get("size_bytes")) is not int
                or not isinstance(expected.get("sha256"), str)):
            raise ValueError("备份文件摘要无效")
        digest = hashlib.sha256()
        size = 0
        with zip_file.open(name) as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                size += len(chunk)
                digest.update(chunk)
        if size != expected["size_bytes"] or digest.hexdigest() != expected["sha256"]:
            raise ValueError(f"备份文件缺失或摘要不符: {name}")
    references = manifest.get("references")
    roots = manifest.get("output_roots")
    if not isinstance(references, dict) or not isinstance(roots, list):
        raise ValueError("备份文本路径清单无效")
    for source, name in references.items():
        if (not isinstance(source, str) or not Path(source).is_absolute()
                or not isinstance(name, str) or not name.startswith("output/") or name not in files):
            raise ValueError("备份文本引用不完整")
    for root in roots:
        if (not isinstance(root, dict) or set(root) != {"source", "destination"}
                or not isinstance(root["source"], str) or not Path(root["source"]).is_absolute()
                or not isinstance(root["destination"], str)):
            raise ValueError("备份输出根无效")
        relative = Path(root["destination"])
        if relative.anchor or ".." in relative.parts or any(":" in part or Path(part).is_reserved() for part in relative.parts):
            raise ValueError("备份输出根包含不安全路径")
    root_mappings = {}
    for root in roots:
        source, destination = Path(root["source"]), Path(root["destination"])
        if source in root_mappings and root_mappings[source] != destination:
            raise ValueError("备份包含冲突的输出根映射")
        root_mappings[source] = destination
    for value, name in references.items():
        source = Path(value)
        matching = [root for root in root_mappings if source.is_relative_to(root)]
        if not matching:
            raise ValueError("备份文本引用缺少所属输出根")
        root = max(matching, key=lambda candidate: len(candidate.parts))
        expected = Path("output") / root_mappings[root] / source.relative_to(root)
        if Path(name) != expected:
            raise ValueError("备份文本引用与输出根不符，已停止恢复以避免串稿")
    return manifest


def _restore_text_references(connection, manifest: dict, output: Path) -> None:
    """Map exact registered files; resolve directory overlaps by longest prefix."""
    references = manifest["references"]
    for _, value in _database_text_paths(connection):
        if value not in references:
            raise ValueError(f"备份缺少已登记文本文件: {value}")

    def file_path(value):
        if not value:
            return value
        return str(output / references[value].removeprefix("output/"))

    roots = [(Path(item["source"]), Path(item["destination"])) for item in manifest["output_roots"]]
    for rowid, value, raw, final in connection.execute(
        "SELECT rowid, output_dir, raw_path, final_path FROM jobs"
    ).fetchall():
        source = Path(value)
        matches = [(root, destination) for root, destination in roots if source.is_relative_to(root)]
        if not matches:
            raise ValueError(f"备份缺少任务输出根映射: {value}")
        root, destination = max(matches, key=lambda item: len(item[0].parts))
        connection.execute("UPDATE jobs SET output_dir=?, raw_path=?, final_path=? WHERE rowid=?",
                           (str(output / destination / source.relative_to(root)), file_path(raw), file_path(final), rowid))
    columns = {row[1] for row in connection.execute("PRAGMA table_info(manifests)")}
    has_metadata = "metadata_json" in columns
    metadata_column = "metadata_json" if has_metadata else "'{}'"
    for rowid, raw, final, payload in connection.execute(
        f"SELECT rowid, raw_path, final_path, {metadata_column} FROM manifests"
    ).fetchall():
        metadata = json.loads(payload or "{}")
        if not metadata.get("text_outputs_cleared"):
            for key in MANIFEST_ARTIFACT_PATHS:
                if metadata.get(key):
                    metadata[key] = file_path(metadata[key])
            connection.execute("UPDATE manifests SET raw_path=?, final_path=? WHERE rowid=?",
                               (file_path(raw), file_path(final), rowid))
        if has_metadata:
            connection.execute("UPDATE manifests SET metadata_json=? WHERE rowid=?",
                               (json.dumps(metadata, ensure_ascii=False), rowid))

# 诊断包日志脱敏：诊断包会被用户转发给开发者，观看记录痕迹不能随包外流。
# 保留域名（定位问题够用），去掉路径与查询（签名/视频号常在里面）；
# 15-20 位纯数字（抖音 aweme 号等）与 BV 号打码；Windows 用户名打码。
_RE_URL_TAIL = re.compile(r"(https?://[^/?\s\"']+)[^\s\"']*")
_RE_LONG_NUMERIC_ID = re.compile(r"\b\d{15,20}\b")
_RE_BILI_ID = re.compile(r"\bBV[0-9A-Za-z]{8,12}\b")
_RE_WIN_USER = re.compile(r"([A-Za-z]:\\Users\\)[^\\\s\"']+")


def _redact_line(line: str) -> str:
    line = _RE_URL_TAIL.sub(r"\1/…", line)
    line = _RE_BILI_ID.sub("BV…", line)
    line = _RE_LONG_NUMERIC_ID.sub("[id]", line)
    line = _RE_WIN_USER.sub(r"\1*", line)
    return line


def _sanitize_settings(settings: dict) -> dict:
    data = dict(settings)
    for key in _SETTINGS_SECRET_FIELDS:
        if key in data:
            data[key] = ""
    return data


def _sqlite_backup_bytes(db_path: str) -> bytes:
    """用 SQLite backup API 生成一致副本（运行中也可安全备份）。"""
    src = sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True)
    buffer = io.BytesIO()
    dst = sqlite3.connect(":memory:")
    try:
        src.backup(dst)
        dst.commit()
        # 从内存库转储为二进制：使用临时文件再读回
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
            tmp_path = tmp.name
        tmp_conn = sqlite3.connect(tmp_path)
        try:
            dst.backup(tmp_conn)
        finally:
            tmp_conn.close()
        buffer.write(Path(tmp_path).read_bytes())
        return buffer.getvalue()
    finally:
        src.close()
        dst.close()
        try:
            Path(tmp_path).unlink(missing_ok=True)
        except (NameError, OSError):
            pass


def _model_inventory(controller) -> dict:
    """已装转写模型清单（读取失败降级为空列表）。"""
    try:
        from engine.model_manager import ModelManager

        settings = controller.settings
        manager = ModelManager(
            settings.get("model_dir", "") or None,
            endpoint=settings.get("hf_endpoint", ""),
        )
        states = manager.list_models()
        return {
            "cache_root": str(getattr(manager, "cache_root", "") or ""),
            "installed": [
                {"name": state.spec.name, "size_label": state.spec.size_label}
                for state in states
                if getattr(state, "installed", False)
            ],
        }
    except Exception as exc:
        return {"error": str(exc), "installed": []}


def _download_inventory(controller) -> list[dict]:
    dl_root = Path(str(controller.settings.get("download_dir", ""))).expanduser()
    rows: list[dict] = []
    if dl_root.is_dir():
        for entry in sorted(dl_root.iterdir(), key=lambda p: p.name.casefold()):
            if entry.is_file():
                try:
                    rows.append({"name": entry.name, "size_bytes": entry.stat().st_size})
                except OSError:
                    continue
    return rows


def _library_stats(controller) -> dict:
    from collections import Counter

    counts: dict[str, object] = {"jobs": dict(Counter(j["status"] for j in controller.queue.list_jobs()))}
    counts["downloads"] = dict(Counter(d["status"] for d in controller.downloads.list(limit=1000)))
    counts["media"] = len(controller.media.list_media())
    return counts


def _safe_extract(zip_file: zipfile.ZipFile, target: Path) -> None:
    """防 zip 路径穿越的解压。"""
    target = target.resolve()
    for member in zip_file.infolist():
        name = member.filename.replace("\\", "/")
        if name.startswith("/") or ".." in Path(name).parts:
            raise ValueError(f"备份内含不安全路径: {name!r}")
        dest = (target / name).resolve()
        if not dest.is_relative_to(target):
            raise ValueError(f"备份路径越界: {name!r}")
    zip_file.extractall(target)


def export_backup(controller, settings_store, dest_path: str) -> dict:
    """Export a verified document backup; media/model files are inventory only."""
    temporary = None
    try:
        from engine.library_location import read_library_identity, validate_storage_path

        dest = validate_storage_path(dest_path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        with _backup_snapshot(controller, settings_store) as snapshot:
            files, roots, references = _backup_text_inventory(controller, snapshot)
            reserved = {settings_store.path.resolve(), Path(controller.settings["database_path"]).resolve(),
                        *(path.resolve() for path in files.values())}
            if dest.resolve() in reserved:
                raise ValueError("备份目标不能覆盖当前资料文件")
            fd, name = tempfile.mkstemp(prefix=".foilpage-backup-", suffix=".tmp", dir=dest.parent)
            os.close(fd)
            temporary = Path(name)
            manifest = {
                "format_version": BACKUP_FORMAT_VERSION, "kind": "document_backup",
                "complete": True, "includes_media": False,
                "library_id": read_library_identity(snapshot),
                "created_at": datetime.now().isoformat(timespec="seconds"),
                "content": ["settings.json", "library.db", "output/", "model_inventory.json", "download_inventory.json"],
                "files": {}, "output_roots": roots, "references": references,
            }
            with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED) as zf:
                def write_bytes(member, content):
                    if not isinstance(content, bytes):
                        content = json.dumps(content, ensure_ascii=False, indent=2).encode("utf-8")
                    zf.writestr(member, content)
                    manifest["files"][member] = {"size_bytes": len(content), "sha256": hashlib.sha256(content).hexdigest()}

                write_bytes("settings.json", _sanitize_settings(dict(controller.settings)))
                write_bytes("library.db", _sqlite_backup_bytes(str(controller.settings["database_path"])))
                for member, path in sorted(files.items()):
                    before = path.stat()
                    digest = _file_hash(path)
                    zf.write(path, member)
                    after = path.stat()
                    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns) or _file_hash(path) != digest:
                        raise ValueError(f"备份期间文本发生变化，请重试: {path}")
                    manifest["files"][member] = {"size_bytes": after.st_size, "sha256": digest}
                write_bytes("model_inventory.json", _model_inventory(controller))
                write_bytes("download_inventory.json", _download_inventory(controller))
                zf.writestr("backup_manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
            with zipfile.ZipFile(temporary) as zf:
                _verify_archive(zf)
            with temporary.open("r+b") as stream:
                os.fsync(stream.fileno())
            os.replace(temporary, dest)
        return {"ok": True, "complete": True, "kind": "document_backup", "includes_media": False,
                "path": str(dest), "size_bytes": dest.stat().st_size, "missing_files": []}
    except Exception as exc:
        return {"ok": False, "complete": False, "error": f"备份导出失败: {exc}",
                "missing_files": exc.paths if isinstance(exc, _MissingBackupFiles) else []}
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def import_backup(controller, settings_store, zip_path: str) -> dict:
    """Serialize the complete restore against application, settings and store writes."""
    with (controller.media_action_lock, controller._settings_lock, settings_store._lock,
          controller.media._lock, controller.queue._lock, controller.downloads._lock):
        if getattr(controller, "_backup_restore_blocked", False):
            return {"ok": False, "recovery_required": True,
                    "error": "备份导入尚未恢复完整，请解除文件占用后重启完成恢复"}
        return _import_backup(controller, settings_store, zip_path)


def _import_backup(controller, settings_store, zip_path: str) -> dict:
    """导入备份：先自动备份现状，再恢复设置/数据库/文本产物（重启后完全生效）。"""
    active = {"queued", "running", "retryable", "received", "resolving", "awaiting_browser", "downloading", "imported", "transcribing"}
    if any(task.get("status") in active for task in controller.task_rows()):
        return {"ok": False, "error": "请先完成或取消进行中的任务，再导入备份"}
    source = Path(zip_path)
    if not source.is_file():
        return {"ok": False, "error": "备份文件不存在"}
    pre_dir = Path(str(settings_store.base_dir)) / "backups"
    pre_dir.mkdir(parents=True, exist_ok=True)
    pre_path = pre_dir / f"pre-import-{datetime.now():%Y%m%d-%H%M%S-%f}.zip"
    pre = export_backup(controller, settings_store, str(pre_path))
    if not pre.get("ok"):
        return {"ok": False, "complete": False, "missing_files": pre.get("missing_files", []),
                "error": f"导入前备份失败，已中止: {pre.get('error')}"}
    temp_dir = Path(tempfile.mkdtemp(prefix="shengjian-import-"))
    try:
        with zipfile.ZipFile(source) as zf:
            names = set(zf.namelist())
            if "settings.json" not in names or "library.db" not in names:
                return {"ok": False, "error": "备份缺少 settings.json / library.db", "pre_backup": str(pre_path)}
            manifest = _verify_archive(zf)
            _safe_extract(zf, temp_dir)
        settings_raw = json.loads((temp_dir / "settings.json").read_text(encoding="utf-8"))
        old_output = Path(str(settings_raw.get("output_dir", ""))).expanduser()
        new_output = Path(str(controller.settings["output_dir"])).expanduser()
        # Restore into this installation, never switch to a path embedded by another machine.
        for key in ("database_path", "output_dir", "download_dir", "browser_download_dir", "model_dir", *_SETTINGS_SECRET_FIELDS):
            settings_raw[key] = controller.settings.get(key, "")
        defaults = settings_store.defaults()
        for key in ("residue_cleanup_default", "confirm_residue_cleanup"):
            settings_raw.setdefault(key, defaults[key])
        settings_store.validate(settings_raw)
        with closing(sqlite3.connect(str(temp_dir / "library.db"))) as incoming:
            if incoming.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise ValueError("备份数据库损坏")
            version = incoming.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                raise ValueError(f"备份数据库版本 {version} 高于当前支持版本 {SCHEMA_VERSION}，请升级FoilPage后再导入")
            from app.controller import AppController
            AppController._check_database_versions(str(temp_dir / "library.db"))
            from engine.postprocess_store import PostprocessStore
            PostprocessStore.validate_database(incoming)
            tables = {row[0] for row in incoming.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not {"jobs", "manifests", "media", "download_jobs"} <= tables:
                raise ValueError("备份数据库缺少必要数据表")
            from engine.library_location import bind_library_identity
            settings_raw["library_id"] = bind_library_identity(incoming)
            if manifest and manifest["library_id"] != settings_raw["library_id"]:
                raise ValueError("备份资料库身份与数据库不符")
            settings_store.validate(settings_raw)
            for table in ("jobs", "download_jobs"):
                if any(row[0] in active for row in incoming.execute(f"SELECT status FROM {table}")):
                    raise ValueError("备份含未完成任务，请先在原设备完成或取消后重新导出")
            # Text files travel with the archive; their database references must
            # follow this installation's output root as well.
            if manifest:
                _restore_text_references(incoming, manifest, new_output)
            elif old_output.is_absolute() and old_output != new_output:
                for table, columns in (("jobs", ("output_dir", "raw_path", "final_path")),
                                       ("manifests", ("raw_path", "final_path"))):
                    for column in columns:
                        for rowid, value in incoming.execute(f"SELECT rowid, {column} FROM {table}").fetchall():
                            if value and Path(value).is_relative_to(old_output):
                                rebased = new_output / Path(value).relative_to(old_output)
                                incoming.execute(f"UPDATE {table} SET {column}=? WHERE rowid=?", (str(rebased), rowid))
                manifest_columns = {
                    row[1] for row in incoming.execute("PRAGMA table_info(manifests)")
                }
                if "metadata_json" in manifest_columns:
                    for rowid, value in incoming.execute(
                        "SELECT rowid, metadata_json FROM manifests"
                    ).fetchall():
                        try:
                            metadata = json.loads(value or "{}")
                        except (TypeError, json.JSONDecodeError):
                            continue
                        changed = False
                        if isinstance(metadata, dict):
                            for key in MANIFEST_ARTIFACT_PATHS:
                                value = metadata.get(key)
                                if isinstance(value, str) and Path(value).is_relative_to(old_output):
                                    metadata[key] = str(new_output / Path(value).relative_to(old_output))
                                    changed = True
                        if changed:
                            incoming.execute(
                                "UPDATE manifests SET metadata_json=? WHERE rowid=?",
                                (json.dumps(metadata, ensure_ascii=False), rowid),
                            )
            incoming.commit()
        db_target = Path(str(controller.settings["database_path"])).expanduser()
        db_target.parent.mkdir(parents=True, exist_ok=True)
        out_root = Path(str(controller.settings["output_dir"])).expanduser()
        out_root.mkdir(parents=True, exist_ok=True)
        apply_backup_import(settings_store, db_target, out_root, temp_dir / "library.db",
                            temp_dir / "output", settings_raw)
        controller.settings["library_id"] = settings_raw["library_id"]
        return {
            "ok": True,
            "pre_backup": str(pre_path),
            "restart_required": True,
            "message": "导入完成，请重启 FoilPage 后生效",
        }
    except Exception as exc:
        recovery_required = isinstance(exc, BackupRestoreError) and (
            settings_store.base_dir / RECOVERY_DIRECTORY).exists()
        if recovery_required:
            controller._backup_restore_blocked = True
            # Existing background coordinators share these connections. Keep
            # reads available, but prevent direct store writes until restart.
            for store in (controller.media, controller.queue, controller.downloads):
                store._conn.execute("PRAGMA query_only=ON")
        return {"ok": False, "error": f"导入失败: {exc}", "pre_backup": str(pre_path),
                "recovery_required": recovery_required}
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def _tail_lines(path: Path, limit: int = LOG_TAIL_LINES) -> str:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(_redact_line(line) for line in lines[-limit:])
    except OSError:
        return ""


def export_diagnostics(controller, settings_store, dest_path: str, *, app_version: str = "") -> dict:
    """导出诊断包：版本/平台/脱敏设置/日志尾部/库统计/模型清单。"""
    dest = Path(dest_path)
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        log_root = default_logs_dir()
        with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(
                "diagnostic_manifest.json",
                json.dumps(
                    {
                        "created_at": datetime.now().isoformat(timespec="seconds"),
                        "app_version": app_version,
                        "python": sys_version(),
                        "platform": platform.platform(),
                        # 日志尾部已脱敏：URL 只留域名，长数字视频号/BV 号/Windows
                        # 用户名打码；设置中的密钥字段同样为空。
                        "log_redaction": "urls: host only; video ids, windows usernames masked",
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
            )
            zf.writestr(
                "settings.json",
                json.dumps(_sanitize_settings(dict(controller.settings)), ensure_ascii=False, indent=2),
            )
            zf.writestr("library_stats.json", json.dumps(_library_stats(controller), ensure_ascii=False, indent=2))
            zf.writestr("model_inventory.json", json.dumps(_model_inventory(controller), ensure_ascii=False, indent=2))
            for log_name in ("desktop.log", "worker.log", "desktop_crash.log"):
                content = _tail_lines(log_root / log_name)
                if content:
                    zf.writestr(f"logs/{log_name}", content)
        return {"ok": True, "path": str(dest), "size_bytes": dest.stat().st_size}
    except Exception as exc:
        return {"ok": False, "error": f"诊断包导出失败: {exc}"}


def sys_version() -> str:
    import sys

    return sys.version.split()[0]
