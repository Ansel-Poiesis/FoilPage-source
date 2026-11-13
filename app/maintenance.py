# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""配置备份/迁移与诊断包导出（底二层）。"""
from __future__ import annotations

import io
import json
import platform
import shutil
import sqlite3
import tempfile
import zipfile
import re
from contextlib import closing
from datetime import datetime
from pathlib import Path


from engine.settings import SECRET_FIELDS as _SETTINGS_SECRET_FIELDS, default_logs_dir

TEXT_SUFFIXES = frozenset({".md", ".txt", ".json", ".srt", ".vtt"})
LOG_TAIL_LINES = 500

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
    src = sqlite3.connect(db_path)
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
    """导出可迁移备份：脱敏设置 + 数据库一致副本 + 文本产物 + 清单。"""
    dest = Path(dest_path)
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        settings = _sanitize_settings(dict(controller.settings))
        manifest = {
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "content": ["settings.json", "library.db", "output/", "model_inventory.json", "download_inventory.json"],
        }
        with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("settings.json", json.dumps(settings, ensure_ascii=False, indent=2))
            db_bytes = _sqlite_backup_bytes(str(controller.settings["database_path"]))
            zf.writestr("library.db", db_bytes)
            out_root = Path(str(controller.settings["output_dir"])).expanduser().resolve()
            if out_root.is_dir():
                for path in sorted(out_root.rglob("*")):
                    # Committed text deletions may retain a recovery staging
                    # directory after an OS file lock. Never restore that text
                    # through a later backup.
                    if any(part.startswith(".text-delete-") for part in path.relative_to(out_root).parts):
                        continue
                    if path.is_file() and path.suffix.lower() in TEXT_SUFFIXES:
                        try:
                            zf.write(path, f"output/{path.relative_to(out_root).as_posix()}")
                        except OSError:
                            continue
            zf.writestr("model_inventory.json", json.dumps(_model_inventory(controller), ensure_ascii=False, indent=2))
            zf.writestr("download_inventory.json", json.dumps(_download_inventory(controller), ensure_ascii=False, indent=2))
            zf.writestr("backup_manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        return {"ok": True, "path": str(dest), "size_bytes": dest.stat().st_size}
    except Exception as exc:
        return {"ok": False, "error": f"备份导出失败: {exc}"}


def import_backup(controller, settings_store, zip_path: str) -> dict:
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
        return {"ok": False, "error": f"导入前备份失败，已中止: {pre.get('error')}"}
    temp_dir = Path(tempfile.mkdtemp(prefix="shengjian-import-"))
    try:
        with zipfile.ZipFile(source) as zf:
            names = set(zf.namelist())
            if "settings.json" not in names or "library.db" not in names:
                return {"ok": False, "error": "备份缺少 settings.json / library.db", "pre_backup": str(pre_path)}
            _safe_extract(zf, temp_dir)
        settings_raw = json.loads((temp_dir / "settings.json").read_text(encoding="utf-8"))
        old_output = Path(str(settings_raw.get("output_dir", ""))).expanduser()
        new_output = Path(str(controller.settings["output_dir"])).expanduser()
        # Restore into this installation, never switch to a path embedded by another machine.
        for key in ("database_path", "output_dir", "download_dir", "browser_download_dir", "model_dir", *_SETTINGS_SECRET_FIELDS):
            settings_raw[key] = controller.settings.get(key, "")
        settings_store.validate(settings_raw)
        with closing(sqlite3.connect(str(temp_dir / "library.db"))) as incoming:
            if incoming.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise ValueError("备份数据库损坏")
            tables = {row[0] for row in incoming.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not {"jobs", "manifests", "media", "download_jobs"} <= tables:
                raise ValueError("备份数据库缺少必要数据表")
            for table in ("jobs", "download_jobs"):
                if any(row[0] in active for row in incoming.execute(f"SELECT status FROM {table}")):
                    raise ValueError("备份含未完成任务，请先在原设备完成或取消后重新导出")
            # Text files travel with the archive; their database references must
            # follow this installation's output root as well.
            if old_output.is_absolute() and old_output != new_output:
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
                        segments = metadata.get("segments_path") if isinstance(metadata, dict) else None
                        if isinstance(segments, str) and Path(segments).is_relative_to(old_output):
                            metadata["segments_path"] = str(new_output / Path(segments).relative_to(old_output))
                            incoming.execute(
                                "UPDATE manifests SET metadata_json=? WHERE rowid=?",
                                (json.dumps(metadata, ensure_ascii=False), rowid),
                            )
                incoming.commit()
        db_target = Path(str(controller.settings["database_path"])).expanduser()
        db_target.parent.mkdir(parents=True, exist_ok=True)
        out_root = Path(str(controller.settings["output_dir"])).expanduser()
        out_root.mkdir(parents=True, exist_ok=True)
        imported_dir = temp_dir / "output"
        if imported_dir.is_dir():
            for path in imported_dir.rglob("*"):
                if path.is_file():
                    rel = path.relative_to(imported_dir)
                    target = (out_root / rel).resolve()
                    if target.is_relative_to(out_root.resolve()):
                        target.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(path, target)
        # SQLite's backup transaction cooperates with open WAL readers. Replacing
        # the database file directly can leave stale WAL pages attached to it.
        with controller.media._lock, controller.queue._lock, controller.downloads._lock:
            with closing(sqlite3.connect(str(temp_dir / "library.db"))) as incoming, closing(sqlite3.connect(str(db_target))) as destination:
                incoming.backup(destination)
        settings_store.save(settings_raw)
        return {
            "ok": True,
            "pre_backup": str(pre_path),
            "restart_required": True,
            "message": "导入完成，请重启 FoilPage 后生效",
        }
    except Exception as exc:
        return {"ok": False, "error": f"导入失败: {exc}", "pre_backup": str(pre_path)}
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
