# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""Keep integrity metadata valid when a fixture tests a different restore boundary."""
import hashlib
import json


def refresh_backup_checksums(contents):
    manifest = json.loads(contents["backup_manifest.json"])
    manifest["files"] = {
        name: {"size_bytes": len(content), "sha256": hashlib.sha256(content).hexdigest()}
        for name, content in contents.items() if name != "backup_manifest.json"
    }
    contents["backup_manifest.json"] = json.dumps(manifest).encode("utf-8")
