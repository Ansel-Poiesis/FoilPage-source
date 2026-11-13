# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""Exercise the release path guard without running any build or deletion."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows junction and release path semantics")
def test_release_guard_accepts_output_children_and_rejects_escape_and_junction(tmp_path):
    shell = shutil.which("pwsh") or shutil.which("powershell")
    if not shell:
        pytest.skip("PowerShell is required")
    safe = tmp_path / "dist"
    outside = tmp_path / "outside"
    safe.mkdir()
    outside.mkdir()
    environment = dict(os.environ, SHENGJIAN_GUARD_SCRIPT=str(ROOT / "build/release.ps1"),
                       SHENGJIAN_GUARD_ROOT=str(safe), SHENGJIAN_GUARD_OUTSIDE=str(outside))
    script = r'''
$ErrorActionPreference = 'Stop'
$tokens=$null; $errors=$null
$ast=[System.Management.Automation.Language.Parser]::ParseFile($env:SHENGJIAN_GUARD_SCRIPT,[ref]$tokens,[ref]$errors)
if ($errors.Count) { throw 'Release script does not parse' }
$definition=$ast.Find({param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Assert-ReleaseTarget'}, $true)
if (-not $definition) { throw 'Missing release path guard' }
. ([scriptblock]::Create($definition.Extent.Text))
Assert-ReleaseTarget -Target (Join-Path $env:SHENGJIAN_GUARD_ROOT 'new/child') -Boundary $env:SHENGJIAN_GUARD_ROOT
foreach ($target in @($env:SHENGJIAN_GUARD_ROOT, $env:SHENGJIAN_GUARD_OUTSIDE, ($env:SHENGJIAN_GUARD_ROOT + '-sibling/child'), (Join-Path $env:SHENGJIAN_GUARD_ROOT '../outside/child'))) {
  $denied=$false
  try { Assert-ReleaseTarget -Target $target -Boundary $env:SHENGJIAN_GUARD_ROOT } catch { $denied=$true }
  if (-not $denied) { throw 'An escaping release target was accepted' }
}
$junction=Join-Path $env:SHENGJIAN_GUARD_ROOT 'linked'
New-Item -ItemType Junction -Path $junction -Target $env:SHENGJIAN_GUARD_OUTSIDE | Out-Null
$denied=$false
try { Assert-ReleaseTarget -Target (Join-Path $junction 'child') -Boundary $env:SHENGJIAN_GUARD_ROOT } catch { $denied=$true }
if (-not $denied) { throw 'A junction ancestor was accepted' }
'''
    result = subprocess.run([shell, "-NoProfile", "-NonInteractive", "-Command", script],
                            env=environment, capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
