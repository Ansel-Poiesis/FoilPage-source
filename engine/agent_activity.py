# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""Local, bounded Agent activity. @玩家川行出品

Records operation names and outcomes, never arguments, content or credentials.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import tempfile
import threading
from engine.local_file_lock import metadata_lock


class AgentActivity:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.lock = threading.RLock()

    def _read(self) -> dict:
        try:
            if self.path.stat().st_size > 200_000:
                return {"records": []}
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or not isinstance(data.get('records'), list):
                return {'records': []}
            records = []
            for item in data['records'][:200]:
                if (not isinstance(item, dict) or not isinstance(item.get('operation'), str)
                        or not re.fullmatch(r'[a-z][a-z0-9_.]{0,95}', item['operation'])
                        or not self._valid_time(item.get('at'))):
                    continue
                category = item.get('category')
                if not isinstance(category, str) or not re.fullmatch(r'[a-z_]{1,32}', category):
                    category = 'application'
                records.append({'operation': item['operation'], 'at': item['at'],
                                'ok': item.get('ok') is True, 'category': category})
            at, revision = data.get('last_saved_at'), data.get('last_revision')
            return {'records': records, 'last_saved_at': at if self._valid_time(at) else None,
                    'last_revision': revision if isinstance(revision, str) and re.fullmatch(r'[a-f0-9]{64}', revision) else ''}
        except (OSError, ValueError):
            return {"records": []}

    @staticmethod
    def _valid_time(value):
        if not isinstance(value, str) or len(value) > 40:
            return False
        try:
            return datetime.fromisoformat(value.replace('Z', '+00:00')).tzinfo is not None
        except ValueError:
            return False

    def record(self, operation: str, ok: bool, category: str = "application", revision: str = "") -> None:
        if not isinstance(operation, str) or not re.fullmatch(r"[a-z][a-z0-9_.]{0,95}", operation):
            return
        if not isinstance(category, str) or not re.fullmatch(r"[a-z_]{1,32}", category):
            category = "application"
        at = datetime.now(timezone.utc).isoformat()
        with self.lock, metadata_lock(self.path):
            data = self._read()
            data["records"] = [{"operation": operation, "at": at, "ok": ok is True, "category": category}, *data["records"][:199]]
            if operation in {"settings.saved", "settings.update"} and ok:
                data["last_saved_at"] = at
                data["last_revision"] = revision if isinstance(revision, str) and re.fullmatch(r"[a-f0-9]{64}", revision) else ""
            self.path.parent.mkdir(parents=True, exist_ok=True)
            name = None
            try:
                with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.path.parent, delete=False) as stream:
                    name = stream.name
                    json.dump(data, stream, ensure_ascii=False)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(name, self.path)
            finally:
                if name and os.path.exists(name):
                    os.unlink(name)

    def summary(self, *, enabled: bool) -> dict:
        with self.lock:
            data = self._read()
            # Revalidate even local records, rather than interpreting a JSON
            # file as instructions or exposing arbitrary injected fields.
            records = []
            for value in data.get("records", [])[:200]:
                if not isinstance(value, dict) or not re.fullmatch(r"[a-z][a-z0-9_.]{0,95}", str(value.get("operation", ""))):
                    continue
                records.append({"operation": value["operation"], "at": str(value.get("at", ""))[:40],
                                "ok": value.get("ok") is True, "category": str(value.get("category", ""))[:32]})
            return {"ok": True, "enabled": enabled is True, "last_saved_at": data.get("last_saved_at"),
                    "last_revision": str(data.get("last_revision", ""))[:64], "records": records}
