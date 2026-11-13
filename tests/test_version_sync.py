# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""Exercise channel and native-resource drift on an isolated source fixture."""
from pathlib import Path
import importlib.util
import shutil

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("release_version", ROOT / "build/version.py")
versioning = importlib.util.module_from_spec(spec)
spec.loader.exec_module(versioning)


def test_current_checkout_markers_are_consistent():
    assert versioning.synchronize(ROOT) == []


def test_channel_transition_and_fixed_resource_drift(tmp_path):
    for relative in ("app/bridge.py", "app/webview_ui/js/app.js", "app/webview_ui/js/settings.js",
                     "app/webview_ui/index.html", "browser-extension/manifest.json",
                     "browser-extension/content.js", "browser-extension/content-main-hook.js",
                     "browser-extension/content-main.js", "build/installer.iss",
                     "build/version_info_desktop.txt", "build/version_info_worker.txt"):
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / relative, target)
    versioning.synchronize(tmp_path, "1.2.0", "test", write=True)
    assert 'RELEASE_CHANNEL = "test"' in (tmp_path / "app/bridge.py").read_text(encoding="utf-8")
    assert versioning.synchronize(tmp_path) == []
    resource = tmp_path / "build/version_info_worker.txt"
    resource.write_text(resource.read_text(encoding="utf-8").replace("filevers=(1, 2, 0, 0)", "filevers=(0, 0, 0, 0)"), encoding="utf-8")
    with pytest.raises(ValueError, match="version_info_worker"):
        versioning.synchronize(tmp_path)
    versioning.synchronize(tmp_path, "1.1.0", "formal", write=True)
    assert versioning.synchronize(tmp_path) == []
