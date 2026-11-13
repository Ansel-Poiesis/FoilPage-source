# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""SQLite media library with content deduplication and relocation support."""
from __future__ import annotations

import hashlib
import os
import sqlite3
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable, Literal, Optional, TypedDict

from .audio import AUDIO_EXTS

MediaType = Literal["audio", "video"]
VIDEO_EXTS = {
    ".mp4", ".mkv", ".mov", ".avi", ".wmv", ".flv", ".m4v", ".mpeg",
    ".mpg", ".ts", ".mts", ".m2ts", ".3gp",
}
MEDIA_EXTS = AUDIO_EXTS | VIDEO_EXTS


class MediaItem(TypedDict):
    id: str
    source_path: str
    filename: str
    extension: str
    media_type: MediaType
    size_bytes: int
    modified_ns: int
    content_sha256: str
    duration_sec: Optional[float]
    missing: bool
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class ImportResult:
    item: MediaItem
    created: bool
    duplicate_of: Optional[str] = None


@dataclass
class ImportSummary:
    created: int = 0
    existing: int = 0
    duplicates: int = 0
    skipped: int = 0
    errors: list[tuple[str, str]] = field(default_factory=list)


class MediaStoreError(RuntimeError):
    pass


class MediaImportCancelled(MediaStoreError):
    pass


class MediaNotFoundError(MediaStoreError):
    pass


class MediaTypeError(MediaStoreError):
    pass


class RelocationMismatchError(MediaStoreError):
    pass


class DuplicateContentError(MediaStoreError):
    pass


_SCHEMA = """
CREATE TABLE IF NOT EXISTS app_schema (
    component TEXT PRIMARY KEY,
    version   INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS media (
    id             TEXT PRIMARY KEY,
    canonical_path TEXT NOT NULL UNIQUE COLLATE NOCASE,
    source_path    TEXT NOT NULL,
    filename       TEXT NOT NULL,
    extension      TEXT NOT NULL,
    media_type     TEXT NOT NULL,
    size_bytes     INTEGER NOT NULL,
    modified_ns    INTEGER NOT NULL,
    content_sha256 TEXT NOT NULL UNIQUE,
    duration_sec   REAL,
    missing        INTEGER NOT NULL DEFAULT 0,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_media_filename ON media(filename COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS idx_media_type_missing ON media(media_type, missing);
"""


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _sha256(path: Path, *, cancel_event: threading.Event | None = None) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            if cancel_event is not None and cancel_event.is_set():
                raise MediaImportCancelled("媒体读取已停止")
            digest.update(chunk)
    return digest.hexdigest()


def _media_type(path: Path) -> MediaType:
    extension = path.suffix.lower()
    if extension in AUDIO_EXTS:
        return "audio"
    if extension in VIDEO_EXTS:
        return "video"
    raise MediaTypeError(f"不支持的媒体格式: {path.suffix or '<无扩展名>'}")


def _display_filename(source: Path, display_name: str) -> str:
    """Return a safe library label without altering the source file on disk."""
    title = str(display_name or "").replace("\x00", "")
    title = " ".join(title.replace("/", " ").replace("\\", " ").split()).strip()
    if not title:
        return source.name
    suffix = source.suffix
    if title.casefold().endswith(suffix.casefold()):
        return title[:240]
    return f"{title[: max(1, 240 - len(suffix))]}{suffix}"


def _duration_sec(path: Path) -> Optional[float]:
    try:
        import av

        with av.open(str(path)) as container:
            if container.duration is None:
                return None
            return max(float(container.duration) / 1_000_000, 0.0)
    except Exception:
        return None


class MediaStore:
    def __init__(self, db_path: str, *, busy_timeout_ms: int = 5000):
        self.db_path = str(Path(db_path).resolve())
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            self.db_path,
            check_same_thread=False,
            timeout=max(busy_timeout_ms, 0) / 1000,
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        with self._conn:
            columns = {row[1] for row in self._conn.execute("PRAGMA table_info(media)")}
            if "original_content_sha256" not in columns:
                self._conn.execute("ALTER TABLE media ADD COLUMN original_content_sha256 TEXT")
                self._conn.execute("UPDATE media SET original_content_sha256=content_sha256")
            self._conn.execute(
                """
                INSERT INTO app_schema(component, version) VALUES ('media', 2)
                ON CONFLICT(component) DO UPDATE SET version=excluded.version
                """
            )

    def import_file(self, path: str, *, display_name: str = "", fast_if_unchanged: bool = False,
                    cancel_event: threading.Event | None = None) -> ImportResult:
        source = Path(path).expanduser().resolve(strict=True)
        if not source.is_file():
            raise FileNotFoundError(f"媒体文件不存在: {path}")
        media_type = _media_type(source)
        filename = _display_filename(source, display_name)
        stat = source.stat()
        canonical = os.path.normcase(str(source))
        # Directory refresh is an index operation, not a repeated full-file
        # content verification. Explicit imports keep their original hash check.
        if fast_if_unchanged:
            with self._lock, self._conn:
                cached = self._conn.execute("SELECT * FROM media WHERE canonical_path=?", (canonical,)).fetchone()
                if cached is not None and cached["size_bytes"] == stat.st_size and cached["modified_ns"] == stat.st_mtime_ns:
                    if cached["missing"]:
                        self._conn.execute("UPDATE media SET missing=0, updated_at=? WHERE id=?", (_now(), cached["id"]))
                    return ImportResult(self.get_media(cached["id"]), False)
        content_hash = _sha256(source) if cancel_event is None else _sha256(source, cancel_event=cancel_event)
        if cancel_event is not None and cancel_event.is_set():
            raise MediaImportCancelled("媒体读取已停止")
        duration = _duration_sec(source)
        if cancel_event is not None and cancel_event.is_set():
            raise MediaImportCancelled("媒体读取已停止")
        after = source.stat()
        if (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
            raise MediaStoreError("媒体仍在写入，请稍后重新读取")

        with self._lock, self._conn:
            by_path = self._conn.execute(
                "SELECT * FROM media WHERE canonical_path=?", (canonical,)
            ).fetchone()
            if by_path is not None:
                content_owner = self._conn.execute(
                    "SELECT id FROM media WHERE content_sha256=? AND id<>?",
                    (content_hash, by_path["id"]),
                ).fetchone()
                if content_owner is not None:
                    raise DuplicateContentError(
                        f"当前内容已属于媒体记录: {content_owner['id']}"
                    )
                self._conn.execute(
                    """
                    UPDATE media
                       SET source_path=?, filename=?, extension=?, media_type=?,
                           size_bytes=?, modified_ns=?, content_sha256=?,
                           duration_sec=?, missing=0, updated_at=?
                     WHERE id=?
                    """,
                    (
                        str(source), filename, source.suffix.lower(), media_type,
                        stat.st_size, stat.st_mtime_ns, content_hash, duration,
                        _now(), by_path["id"],
                    ),
                )
                return ImportResult(self.get_media(by_path["id"]), False)

            by_content = self._conn.execute(
                "SELECT * FROM media WHERE content_sha256=?", (content_hash,)
            ).fetchone()
            if by_content is not None:
                return ImportResult(
                    self._row_to_item(by_content),
                    False,
                    duplicate_of=by_content["id"],
                )

            media_id = uuid.uuid4().hex
            now = _now()
            self._conn.execute(
                """
                INSERT INTO media
                  (id, canonical_path, source_path, filename, extension, media_type,
                   size_bytes, modified_ns, content_sha256, duration_sec, missing,
                   created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
                """,
                (
                    media_id, canonical, str(source), filename,
                    source.suffix.lower(), media_type, stat.st_size,
                    stat.st_mtime_ns, content_hash, duration, now, now,
                ),
            )
            self._conn.execute("UPDATE media SET original_content_sha256=content_sha256 WHERE id=? AND original_content_sha256 IS NULL", (media_id,))
            return ImportResult(self.get_media(media_id), True)

    def import_directory(self, path: str, *, recursive: bool = True) -> ImportSummary:
        root = Path(path).expanduser().resolve(strict=True)
        if not root.is_dir():
            raise NotADirectoryError(f"目录不存在: {path}")
        summary = ImportSummary()
        iterator = root.rglob("*") if recursive else root.glob("*")
        ignored_db_paths = {
            os.path.normcase(self.db_path + suffix) for suffix in ("", "-wal", "-shm")
        }
        for candidate in sorted(iterator):
            if not candidate.is_file():
                continue
            if os.path.normcase(str(candidate.resolve())) in ignored_db_paths:
                continue
            if candidate.suffix.lower() not in MEDIA_EXTS:
                summary.skipped += 1
                continue
            try:
                result = self.import_file(str(candidate))
            except (OSError, MediaStoreError) as exc:
                summary.errors.append((str(candidate), str(exc)))
                continue
            if result.created:
                summary.created += 1
            elif result.duplicate_of:
                summary.duplicates += 1
            else:
                summary.existing += 1
        return summary

    def import_paths(
        self,
        paths: Iterable[str],
        *,
        progress: Callable[[int, int, str], None] | None = None,
        cancel_event: threading.Event | None = None,
    ) -> ImportSummary:
        candidates = list(dict.fromkeys(os.path.abspath(path) for path in paths))
        summary = ImportSummary()
        total = len(candidates)
        for index, candidate in enumerate(candidates, start=1):
            if cancel_event is not None and cancel_event.is_set():
                summary.skipped += total - index + 1
                break
            try:
                result = self.import_file(candidate)
            except (OSError, MediaStoreError) as exc:
                summary.errors.append((candidate, str(exc)))
            else:
                if result.created:
                    summary.created += 1
                elif result.duplicate_of:
                    summary.duplicates += 1
                else:
                    summary.existing += 1
            if progress is not None:
                progress(index, total, candidate)
        return summary

    def get_media(self, media_id: str) -> MediaItem:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM media WHERE id=?", (media_id,)
            ).fetchone()
            if row is None:
                raise MediaNotFoundError(f"媒体不存在: {media_id}")
            return self._row_to_item(row)

    def list_media(
        self,
        *,
        query: str = "",
        media_type: Optional[MediaType] = None,
        missing: Optional[bool] = None,
    ) -> list[MediaItem]:
        clauses: list[str] = []
        params: list[object] = []
        if query.strip():
            clauses.append("(filename LIKE ? OR source_path LIKE ?)")
            pattern = f"%{query.strip()}%"
            params.extend((pattern, pattern))
        if media_type is not None:
            if media_type not in ("audio", "video"):
                raise ValueError(f"media_type 非法: {media_type}")
            clauses.append("media_type=?")
            params.append(media_type)
        if missing is not None:
            clauses.append("missing=?")
            params.append(int(missing))
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM media{where} ORDER BY created_at DESC, filename",
                params,
            ).fetchall()
            return [self._row_to_item(row) for row in rows]

    def refresh_missing(self) -> int:
        missing_count = 0
        with self._lock, self._conn:
            rows = self._conn.execute("SELECT id, source_path, missing FROM media").fetchall()
            now = _now()
            for row in rows:
                missing = not Path(row["source_path"]).is_file()
                missing_count += int(missing)
                if missing != bool(row["missing"]):
                    self._conn.execute(
                        "UPDATE media SET missing=?, updated_at=? WHERE id=?",
                        (int(missing), now, row["id"]),
                    )
        return missing_count

    def relocate(self, media_id: str, new_path: str) -> MediaItem:
        current = self.get_media(media_id)
        target = Path(new_path).expanduser().resolve(strict=True)
        if not target.is_file():
            raise FileNotFoundError(f"重新定位目标不存在: {new_path}")
        media_type = _media_type(target)
        target_hash = _sha256(target)
        if target_hash != current["content_sha256"]:
            raise RelocationMismatchError("重新定位目标与原媒体内容哈希不一致")
        canonical = os.path.normcase(str(target))
        stat = target.stat()
        with self._lock, self._conn:
            conflict = self._conn.execute(
                "SELECT id FROM media WHERE canonical_path=? AND id<>?",
                (canonical, media_id),
            ).fetchone()
            if conflict is not None:
                raise RelocationMismatchError("重新定位目标已属于另一条媒体记录")
            self._conn.execute(
                """
                UPDATE media
                   SET canonical_path=?, source_path=?, filename=?, extension=?,
                       media_type=?, size_bytes=?, modified_ns=?, missing=0,
                       updated_at=?
                 WHERE id=?
                """,
                (
                    canonical, str(target), target.name, target.suffix.lower(),
                    media_type, stat.st_size, stat.st_mtime_ns, _now(), media_id,
                ),
            )
            return self.get_media(media_id)

    def delete_media(self, media_id: str) -> bool:
        with self._lock, self._conn:
            cursor = self._conn.execute("DELETE FROM media WHERE id=?", (media_id,))
            return cursor.rowcount == 1

    def clear_media(self) -> int:
        """Remove library index records while leaving user files and jobs intact."""
        with self._lock, self._conn:
            cursor = self._conn.execute("DELETE FROM media")
            return cursor.rowcount

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass

    @staticmethod
    def _row_to_item(row: sqlite3.Row) -> MediaItem:
        return {
            "id": row["id"],
            "source_path": row["source_path"],
            "filename": row["filename"],
            "extension": row["extension"],
            "media_type": row["media_type"],
            "size_bytes": row["size_bytes"],
            "modified_ns": row["modified_ns"],
            "content_sha256": row["content_sha256"],
            "original_content_sha256": row["original_content_sha256"],
            "duration_sec": row["duration_sec"],
            "missing": bool(row["missing"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
