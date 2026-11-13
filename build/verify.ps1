# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
[CmdletBinding()]
param()

$ErrorActionPreference = 'Continue'
$env:PYTHONUTF8 = '1'
$projectRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) { throw 'Create .venv and install the locked Python requirements first.' }
if (-not (Get-Command npm.cmd -ErrorAction SilentlyContinue)) { throw 'Node.js/npm is required for UI verification.' }
if (-not (Test-Path -LiteralPath (Join-Path $projectRoot 'node_modules\playwright'))) {
    throw 'Run npm ci and npx playwright install chromium first.'
}
Push-Location $projectRoot
try {
    & $python (Join-Path $PSScriptRoot 'check_source.py')
    if ($LASTEXITCODE -ne 0) { throw 'Source distribution checks failed' }
    & $python (Join-Path $PSScriptRoot 'version.py') --check
    if ($LASTEXITCODE -ne 0) { throw 'Release version markers are inconsistent' }
    & $python -m pip check
    if ($LASTEXITCODE -ne 0) { throw 'Python dependencies are inconsistent' }
    & $python -m ruff check app engine agent tests
    if ($LASTEXITCODE -ne 0) { throw "ruff lint failed" }
    & $python -m pytest -q
    if ($LASTEXITCODE -ne 0) { throw 'Python tests failed' }
    & npm.cmd test
    if ($LASTEXITCODE -ne 0) { throw 'JavaScript/onboarding tests failed' }
    & npm.cmd run test:ui
    if ($LASTEXITCODE -ne 0) { throw 'Product UI smoke failed' }
    Write-Output 'All verification gates passed.'
} finally {
    Pop-Location
}
