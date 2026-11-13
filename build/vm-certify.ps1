# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$InstallerPath,
    [string]$EvidenceDir = (Join-Path $env:USERPROFILE 'Desktop\FoilPage-VM-Evidence'),
    [switch]$Uninstall
)

$ErrorActionPreference = 'Stop'
$env:PYTHONUTF8 = '1'
$installer = (Resolve-Path -LiteralPath $InstallerPath).Path
$evidence = [System.IO.Path]::GetFullPath($EvidenceDir)
New-Item -ItemType Directory -Path $evidence -Force | Out-Null

$os = Get-CimInstance Win32_OperatingSystem
$record = [ordered]@{
    started_at = (Get-Date).ToString('o')
    computer = $env:COMPUTERNAME
    os_caption = $os.Caption
    os_version = $os.Version
    os_build = $os.BuildNumber
    architecture = $os.OSArchitecture
    installer_path = $installer
    installer_sha256 = (Get-FileHash -LiteralPath $installer -Algorithm SHA256).Hash
    install_exit_code = $null
    desktop_smoke_exit_code = $null
    worker_help_exit_code = $null
    uninstall_exit_code = $null
    manual_checks = [ordered]@{
        first_model_download = 'pending'
        offline_transcription = 'pending'
        defender = 'pending'
        smartscreen = 'pending'
    }
}

$installLog = Join-Path $evidence 'install.log'
$install = Start-Process -FilePath $installer -Wait -PassThru -ArgumentList @(
    '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', "/LOG=$installLog"
)
$record.install_exit_code = $install.ExitCode
if ($install.ExitCode -ne 0) { throw "安装失败，退出码 $($install.ExitCode)" }

$appDir = Join-Path $env:LOCALAPPDATA 'Programs\FoilPage'
$desktop = Join-Path $appDir 'FoilPage-Desktop.exe'
$worker = Join-Path $appDir 'worker\VideoToArticle-Worker.exe'
foreach ($required in ($desktop, $worker)) {
    if (-not (Test-Path -LiteralPath $required -PathType Leaf)) {
        throw "安装文件缺失: $required"
    }
}

$smokeData = Join-Path $env:LOCALAPPDATA 'VideoToArticle-VM-Cert'
$desktopSmoke = Start-Process -FilePath $desktop -Wait -PassThru -ArgumentList @(
    '--smoke', '--data-dir', $smokeData
)
$record.desktop_smoke_exit_code = $desktopSmoke.ExitCode
if ($desktopSmoke.ExitCode -ne 0) { throw 'Desktop --smoke 失败' }

$workerOut = Join-Path $evidence 'worker-help.txt'
$workerHelp = Start-Process -FilePath $worker -Wait -PassThru `
    -ArgumentList '--help' -RedirectStandardOutput $workerOut
$record.worker_help_exit_code = $workerHelp.ExitCode
if ($workerHelp.ExitCode -ne 0) { throw 'Worker --help 失败' }

if ($Uninstall) {
    $uninstaller = Join-Path $appDir 'unins000.exe'
    if (-not (Test-Path -LiteralPath $uninstaller -PathType Leaf)) {
        throw "卸载器缺失: $uninstaller"
    }
    $uninstallLog = Join-Path $evidence 'uninstall.log'
    $remove = Start-Process -FilePath $uninstaller -Wait -PassThru -ArgumentList @(
        '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', "/LOG=$uninstallLog"
    )
    $record.uninstall_exit_code = $remove.ExitCode
    if ($remove.ExitCode -ne 0) { throw "卸载失败，退出码 $($remove.ExitCode)" }
}

$record.completed_at = (Get-Date).ToString('o')
$record | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath (Join-Path $evidence 'certification.json') -Encoding UTF8
$record
