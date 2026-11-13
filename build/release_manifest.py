# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""Record local release provenance and both artifact hashes without installing.

Run after release.ps1 from the same clean checkout. The source fields record the
checkout at capture time; they do not claim that a historical binary embeds or
independently proves a source commit. Existing manifests are never overwritten.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys


def git(root: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", "-C", str(root), *args], text=True, encoding="utf-8", timeout=30,
        stderr=subprocess.PIPE,
    ).strip()


def source_identity(root: Path) -> dict:
    if Path(git(root, "rev-parse", "--show-toplevel")).resolve() != root:
        raise ValueError("The release root must be the Git checkout root")
    if git(root, "status", "--porcelain", "--untracked-files=normal"):
        raise ValueError("Release provenance requires a clean working tree")
    flags = git(root, "ls-files", "-v").splitlines()
    if any(line and (line[0].islower() or line[0] == "S") for line in flags):
        raise ValueError("Tracked files marked assume-unchanged or skip-worktree cannot prove a clean release")
    text = (root / "app" / "bridge.py").read_text(encoding="utf-8")
    match = re.search(r'^APP_VERSION\s*=\s*"(\d+\.\d+\.\d+)"', text, re.M)
    if not match:
        raise ValueError("APP_VERSION is missing or invalid")
    return {"app_version": match.group(1), "head_commit": git(root, "rev-parse", "HEAD"),
            "tree": git(root, "rev-parse", "HEAD^{tree}"), "clean": True}


def artifact_identity(root: Path, name: str, role: str) -> dict:
    artifact = root / "dist_installer" / name
    before = artifact.stat()
    digest = hashlib.sha256()
    with artifact.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    after = artifact.stat()
    if (before.st_size, before.st_mtime_ns, before.st_ino) != (
            after.st_size, after.st_mtime_ns, after.st_ino):
        raise ValueError(f"Artifact changed while hashing: {name}")
    return {"role": role, "file": name, "size_bytes": after.st_size,
            "sha256": digest.hexdigest()}


def collect(root: Path) -> dict:
    source = source_identity(root)
    version = source["app_version"]
    artifacts = [artifact_identity(root, f"FoilPage-Setup-{version}.exe", "installer"),
                 artifact_identity(root, f"FoilPage-GPU-Pack-{version}.zip", "gpu_pack")]
    if source_identity(root) != source:
        raise ValueError("Source changed while recording release provenance")
    return {"schema_version": 1, "version": version, "source": source,
            "source_evidence": "clean checkout captured after local build; not binary-embedded provenance",
            "artifacts": artifacts}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--check", action="store_true", help="Verify the existing manifest against this checkout and both files")
    parser.add_argument("--expected-head", help="HEAD captured before the build")
    parser.add_argument("--expected-tree", help="Git tree captured before the build")
    parser.add_argument("--smoke-data", help="Fresh isolated packaged-smoke data directory")
    args = parser.parse_args(argv)
    try:
        root = args.root.resolve(strict=True)
        facts = collect(root)
        for argument, field in ((args.expected_head, "head_commit"), (args.expected_tree, "tree")):
            if argument is not None and facts["source"][field] != argument:
                raise ValueError("Release source differs from the build-start identity")
        output = root / "dist_installer" / f"RELEASE-MANIFEST-{facts['version']}.json"
        if args.check:
            recorded = json.loads(output.read_text(encoding="utf-8"))
            recorded.pop("created_at", None)
            recorded.pop("smoke_data", None)
            if recorded != facts:
                raise ValueError("Release manifest differs from the checkout or artifact bytes")
            print(f"Verified {output}")
        else:
            facts["created_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            if args.smoke_data:
                # The manifest is published; only the directory name is provenance,
                # the build-machine path must not enter the public record.
                facts["smoke_data"] = Path(args.smoke_data).resolve(strict=True).name
            encoded = json.dumps(facts, ensure_ascii=False, indent=2) + "\n"
            with output.open("x", encoding="utf-8", newline="\n") as stream:
                stream.write(encoded)
            print(f"Recorded {output}")
        return 0
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"Release manifest failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
