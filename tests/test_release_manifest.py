# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""Release provenance uses real Git state and detects changed distributables."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "build" / "release_manifest.py"


@pytest.fixture
def checkout(tmp_path):
    if not shutil.which("git"):
        pytest.skip("Git is required to validate release provenance")
    root = tmp_path / "source with spaces"
    (root / "app").mkdir(parents=True)
    (root / "dist_installer").mkdir()
    (root / "app" / "bridge.py").write_text('APP_VERSION = "9.8.7"\n', encoding="utf-8")
    (root / ".gitignore").write_text("dist_installer/\n", encoding="utf-8")
    for name, payload in (("FoilPage-Setup-9.8.7.exe", b"installer"),
                          ("FoilPage-GPU-Pack-9.8.7.zip", b"gpu pack")):
        (root / "dist_installer" / name).write_bytes(payload)
    for command in (("init",), ("config", "user.name", "Release Test"),
                    ("config", "user.email", "release@example.invalid"),
                    ("add", "."), ("commit", "-m", "test release")):
        subprocess.run(["git", "-C", str(root), *command], check=True, capture_output=True)
    return root


def run(root, *args):
    return subprocess.run([sys.executable, str(SCRIPT), "--root", str(root), *args],
                          capture_output=True, text=True, encoding="utf-8", check=False)


def test_records_real_head_and_both_artifact_bytes_without_overwriting(checkout):
    root = checkout
    assert run(root).returncode == 0
    path = root / "dist_installer" / "RELEASE-MANIFEST-9.8.7.json"
    original = path.read_bytes()
    facts = json.loads(original)
    expected_head = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()
    assert facts["source"]["head_commit"] == expected_head
    assert facts["version"] == "9.8.7"
    assert {(row["role"], row["size_bytes"], row["sha256"]) for row in facts["artifacts"]} == {
        ("installer", 9, hashlib.sha256(b"installer").hexdigest()),
        ("gpu_pack", 8, hashlib.sha256(b"gpu pack").hexdigest()),
    }
    assert run(root, "--check").returncode == 0
    assert run(root, "--check", "--expected-head", "0" * 40).returncode != 0
    assert run(root, "--check", "--expected-tree", "0" * 40).returncode != 0
    assert run(root).returncode != 0
    assert path.read_bytes() == original
    (root / "dist_installer" / "FoilPage-Setup-9.8.7.exe").write_bytes(b"tampered")
    assert run(root, "--check").returncode != 0
    assert path.read_bytes() == original


def test_rejects_dirty_checkout_and_missing_companion_pack(checkout):
    root = checkout
    bridge = root / "app" / "bridge.py"
    bridge.write_text('APP_VERSION = "9.8.7"\n# uncommitted\n', encoding="utf-8")
    assert run(root).returncode != 0
    bridge.write_text('APP_VERSION = "9.8.7"\n', encoding="utf-8")
    (root / "dist_installer" / "FoilPage-GPU-Pack-9.8.7.zip").unlink()
    assert run(root).returncode != 0
    assert not (root / "dist_installer" / "RELEASE-MANIFEST-9.8.7.json").exists()


@pytest.mark.parametrize("flag", ["--skip-worktree", "--assume-unchanged"])
def test_hidden_tracked_source_changes_do_not_receive_provenance(checkout, flag):
    root = checkout
    subprocess.run(["git", "-C", str(root), "update-index", flag, "app/bridge.py"], check=True)
    (root / "app" / "bridge.py").write_text('APP_VERSION = "9.8.7"\n# hidden source drift\n', encoding="utf-8")
    assert not subprocess.check_output(["git", "-C", str(root), "status", "--porcelain"], text=True).strip()
    assert run(root).returncode != 0
    assert not (root / "dist_installer" / "RELEASE-MANIFEST-9.8.7.json").exists()


def test_smoke_data_records_directory_name_without_build_machine_path(checkout):
    root = checkout
    missing = root.parent / "shengjian-packaged-smoke-missing"
    assert run(root, "--smoke-data", str(missing)).returncode != 0
    assert not (root / "dist_installer" / "RELEASE-MANIFEST-9.8.7.json").exists()
    smoke = root.parent / "shengjian-packaged-smoke-9.8.7-sample"
    smoke.mkdir()
    assert run(root, "--smoke-data", str(smoke)).returncode == 0
    facts = json.loads(
        (root / "dist_installer" / "RELEASE-MANIFEST-9.8.7.json").read_text(encoding="utf-8"))
    assert facts["smoke_data"] == smoke.name
    assert run(root, "--check").returncode == 0
