# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
[CmdletBinding()]
param(
    [switch]$SkipTests,
    [switch]$SkipInstaller,
    [string]$AgentPython = ''
)

$ErrorActionPreference = 'Continue'  # native stderr (PyInstaller/ISCC INFO) must not fail the script; key steps use explicit -ErrorAction Stop and $LASTEXITCODE checks
$env:PYTHONUTF8 = '1'

$projectRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $projectRoot '.venv\Scripts\python.exe'
$desktopDist = Join-Path $projectRoot 'dist\FoilPage-Desktop'
$workerDist = Join-Path $projectRoot 'dist\FoilPage-Worker'
$workerBundle = Join-Path $desktopDist 'worker'
$agentDist = Join-Path $projectRoot 'dist\FoilPage-Agent'
$agentBundle = Join-Path $desktopDist 'agent'
if (-not $AgentPython) { $AgentPython = Join-Path $projectRoot '.agent-venv\Scripts\python.exe' }
$extensionSource = Join-Path $projectRoot 'browser-extension'
$extensionBundle = Join-Path $desktopDist 'browser-extension'

function Assert-ReleaseTarget {
    param([string]$Target, [string]$Boundary)
    $resolved = [IO.Path]::GetFullPath($Target)
    $allowed = [IO.Path]::GetFullPath($Boundary).TrimEnd('\') + '\'
    if (-not $resolved.StartsWith($allowed, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Release target is outside its output boundary: $resolved"
    }
    # Check existing ancestors too: a lexical child may point outside through a junction.
    $cursor = $resolved
    while ($cursor) {
        if (Test-Path -LiteralPath $cursor) {
            $item = Get-Item -LiteralPath $cursor -Force -ErrorAction Stop
            if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
                throw "Release target has a reparse-point ancestor: $cursor"
            }
        }
        $parent = Split-Path -Parent $cursor
        if ($parent -eq $cursor) { break }
        $cursor = $parent
    }
}

if (-not (Test-Path -LiteralPath $python)) {
    throw "Project virtual environment is missing: $python"
}
if (-not (Test-Path -LiteralPath $AgentPython)) {
    throw 'Create the isolated .agent-venv using requirements-agent.lock.txt and requirements-agent-build.txt; see docs/agent-control.md.'
}

Push-Location $projectRoot
try {
    & $python (Join-Path $PSScriptRoot 'version.py') --check
    if ($LASTEXITCODE -ne 0) { throw 'Release version markers are inconsistent' }
    & node (Join-Path $PSScriptRoot 'sync-extension-main.cjs') --check
    if ($LASTEXITCODE -ne 0) { throw 'The self-contained MAIN entry is stale' }
    # ---- 版本单一事实源门禁 ----
    # app/bridge.py 的 APP_VERSION 是唯一权威版本号。其余所有标记必须与之一致，
    # 否则产出的安装器无法追溯到确定的源码状态——0.8.0 的发布锚点错位与
    # version_info 中 FixedFileInfo 停留在 0.7.26 都是这种分散硬编码的结果。
    $bridgeSource = Get-Content -LiteralPath (Join-Path $projectRoot 'app\bridge.py') -Raw -Encoding UTF8
    if ($bridgeSource -notmatch 'APP_VERSION\s*=\s*"(\d+\.\d+\.\d+)"') {
        throw 'APP_VERSION was not found in app\bridge.py'
    }
    $appVersion = $Matches[1]
    $expectedFileVersion = "$appVersion.0"
    $expectedTuple = ((($appVersion.Split('.')) | ForEach-Object { [int]$_ }) -join ', ') + ', 0'

    $versionMarkers = @(
        @{ File = 'build\version_info_desktop.txt'; Needle = "filevers=($expectedTuple)" },
        @{ File = 'build\version_info_desktop.txt'; Needle = "prodvers=($expectedTuple)" },
        @{ File = 'build\version_info_desktop.txt'; Needle = "'FileVersion', '$expectedFileVersion'" },
        @{ File = 'build\version_info_desktop.txt'; Needle = "'ProductVersion', '$expectedFileVersion'" },
        @{ File = 'build\version_info_worker.txt'; Needle = "filevers=($expectedTuple)" },
        @{ File = 'build\version_info_worker.txt'; Needle = "prodvers=($expectedTuple)" },
        @{ File = 'build\version_info_worker.txt'; Needle = "'FileVersion', '$expectedFileVersion'" },
        @{ File = 'build\version_info_worker.txt'; Needle = "'ProductVersion', '$expectedFileVersion'" },
        @{ File = 'build\installer.iss'; Needle = "#define AppVersion `"$appVersion`"" },
        @{ File = 'build\installer.iss'; Needle = "VersionInfoVersion=$expectedFileVersion" },
        @{ File = 'browser-extension\manifest.json'; Needle = "`"version`": `"$appVersion`"" },
        @{ File = 'browser-extension\content.js'; Needle = "EXTENSION_VERSION = `"$appVersion`"" },
        @{ File = 'browser-extension\content-main.js'; Needle = "MAIN_MARK_VERSION = `"$appVersion`"" },
        @{ File = 'app\webview_ui\js\app.js'; Needle = "window.APP_VERSION = `"$appVersion`"" },
        @{ File = 'app\webview_ui\index.html'; Needle = "id=`"brandVer`">v$appVersion<" },
        @{ File = 'app\webview_ui\js\settings.js'; Needle = "window.APP_VERSION || `"$appVersion`"" }
    )
    foreach ($marker in $versionMarkers) {
        $markerPath = Join-Path $projectRoot $marker.File
        if (-not (Test-Path -LiteralPath $markerPath)) {
            throw "Version marker file is missing: $($marker.File)"
        }
        $markerText = Get-Content -LiteralPath $markerPath -Raw -Encoding UTF8
        if ($markerText -notlike "*$($marker.Needle)*") {
            throw "Version marker mismatch: $($marker.File) must contain '$($marker.Needle)' for APP_VERSION $appVersion"
        }
    }
    Write-Output "Version markers consistent with APP_VERSION $appVersion"

    # ---- 工作区干净门禁 ----
    # 0.8.0 带着 175 行未提交改动进入了发布流程，档案因此记录了无法复现的验收
    # 数字。发布必须在干净的树上进行，否则构建产物没有对应的提交可追溯。
    $gitCommand = Get-Command git -ErrorAction SilentlyContinue
    if ($gitCommand) {
        $dirty = & git -C $projectRoot status --porcelain
        if ($LASTEXITCODE -ne 0) { throw 'git status failed; cannot confirm a clean release tree' }
        if ($dirty) {
            throw "Release blocked: working tree is not clean.`n$($dirty -join "`n")"
        }
        Write-Output 'Working tree is clean'
        $releaseHead = & git -C $projectRoot rev-parse HEAD
        if ($LASTEXITCODE -ne 0) { throw 'Cannot lock the release HEAD' }
        $releaseTree = & git -C $projectRoot rev-parse 'HEAD^{tree}'
        if ($LASTEXITCODE -ne 0) { throw 'Cannot lock the release tree' }
    } else {
        throw 'git is required to lock and verify the release source'
    }

    if (-not $SkipTests) {
        & (Join-Path $PSScriptRoot 'verify.ps1')
    }

    foreach ($buildTarget in @($workerDist, $desktopDist, $agentDist)) {
        Assert-ReleaseTarget -Target $buildTarget -Boundary (Join-Path $projectRoot 'dist')
    }
    foreach ($workName in @('FoilPage-Worker', 'VideoToArticle-Worker', 'FoilPage-Desktop', 'VideoToArticle-Desktop', 'FoilPage-Agent', 'ShengJian-Agent')) {
        Assert-ReleaseTarget -Target (Join-Path $PSScriptRoot $workName) -Boundary $PSScriptRoot
    }
    & $python -m PyInstaller --noconfirm build\worker.spec 2>&1
    if ($LASTEXITCODE -ne 0) { throw 'Worker build failed' }
    & $python -m PyInstaller --noconfirm build\desktop.spec 2>&1
    if ($LASTEXITCODE -ne 0) { throw 'Desktop build failed' }
    & $AgentPython -m PyInstaller --noconfirm build\agent.spec 2>&1
    if ($LASTEXITCODE -ne 0) { throw 'Agent control build failed' }

    # Validate recursive replacement targets before deleting previous bundles.
    $distBoundary = [IO.Path]::GetFullPath($desktopDist).TrimEnd('\') + '\'
    foreach ($bundleTarget in @($workerBundle, $extensionBundle, $agentBundle)) {
        Assert-ReleaseTarget -Target $bundleTarget -Boundary $desktopDist
        $resolvedTarget = [IO.Path]::GetFullPath($bundleTarget)
        if (-not $resolvedTarget.StartsWith($distBoundary, [StringComparison]::OrdinalIgnoreCase)) {
            throw "Bundle path is outside desktop dist: $resolvedTarget"
        }
    }
    if (Test-Path -LiteralPath $workerBundle) {
        Remove-Item -LiteralPath $workerBundle -Recurse -Force -ErrorAction Stop
    }
    Copy-Item -LiteralPath $workerDist -Destination $workerBundle -Recurse -ErrorAction Stop
    if (Test-Path -LiteralPath $agentBundle) {
        Remove-Item -LiteralPath $agentBundle -Recurse -Force -ErrorAction Stop
    }
    Copy-Item -LiteralPath $agentDist -Destination $agentBundle -Recurse -ErrorAction Stop
    # 1.3.8 前的 MCP 配方把 agent\ShengJian-Agent.exe 写进了用户客户端配置；
    # 同名副本共用 _internal，让存量旧配置在过渡期继续可用。
    Copy-Item -LiteralPath (Join-Path $agentBundle 'FoilPage-Agent.exe') `
        -Destination (Join-Path $agentBundle 'ShengJian-Agent.exe') -ErrorAction Stop
    if (Test-Path -LiteralPath $extensionBundle) {
        Remove-Item -LiteralPath $extensionBundle -Recurse -Force -ErrorAction Stop
    }
    Copy-Item -LiteralPath $extensionSource -Destination $extensionBundle -Recurse -ErrorAction Stop

    # Slim: cuDNN precompiled engines are rebuilt at first run by runtime_compiled
    # and can be removed safely (verified via packaged GPU smoke with tiny/small
    # models). ffmpeg 0.18.0 起只保留包内副本（worker.spec 不再打根副本，
    # 此处也不再删包内副本）。
    $slimTargets = @(
        (Join-Path $workerBundle '_internal\nvidia\cudnn\bin\cudnn_engines_precompiled64_9.dll')
    )
    foreach ($slimTarget in $slimTargets) {
        Assert-ReleaseTarget -Target $slimTarget -Boundary $workerBundle
        if (Test-Path -LiteralPath $slimTarget) {
            Remove-Item -LiteralPath $slimTarget -Force -ErrorAction Stop
        }
    }

    # ── GPU 拆包（先生 2026-10-08：安装器只含核心刚需程序）─────────────────
    # 默认（SHENGJIAN_BUNDLE_CUDA 未设 1）：worker 不带 NVIDIA 运行库（约省
    # 700MB 压缩体积），并从 venv 现场生成配套 GPU 加速包 zip——准备页自选
    # 安装，解压回 worker _internal/nvidia 即被 engine/dll.py 发现。全量模式
    # 校验库确实在包里。
    $bundleCuda = $env:SHENGJIAN_BUNDLE_CUDA -eq '1'
    $nvidiaInBundle = Join-Path $workerBundle '_internal\nvidia'
    if (-not $bundleCuda) {
        if (Test-Path -LiteralPath $nvidiaInBundle) {
            throw 'Slim build unexpectedly contains _internal/nvidia; check worker.spec excludes'
        }
        $gpuStage = Join-Path $projectRoot 'dist\gpu-pack-stage'
        Assert-ReleaseTarget -Target $gpuStage -Boundary (Join-Path $projectRoot 'dist')
        if (Test-Path -LiteralPath $gpuStage) { Remove-Item -LiteralPath $gpuStage -Recurse -Force -ErrorAction Stop }
        $sitePackages = Join-Path $projectRoot '.venv\Lib\site-packages'
        foreach ($component in @('cublas', 'cuda_nvrtc', 'cudnn')) {
            $binDir = Join-Path $sitePackages ("nvidia\" + $component + "\bin")
            if (-not (Test-Path -LiteralPath $binDir)) { throw "GPU pack source missing: $binDir" }
            $dest = Join-Path $gpuStage ("nvidia\" + $component)
            New-Item -ItemType Directory -Path $dest -Force | Out-Null
            Copy-Item -LiteralPath $binDir -Destination $dest -Recurse -ErrorAction Stop
        }
        $gpuPack = Join-Path $projectRoot ("dist_installer\FoilPage-GPU-Pack-$appVersion.zip")
        Assert-ReleaseTarget -Target $gpuPack -Boundary (Join-Path $projectRoot 'dist_installer')
        & $python (Join-Path $PSScriptRoot 'collect_release_notices.py') --output (Join-Path $gpuStage 'nvidia\third-party-notices') --gpu-only
        if ($LASTEXITCODE -ne 0) { throw 'GPU pack third-party materials could not be collected' }
        New-Item -ItemType Directory -Path (Split-Path -Parent $gpuPack) -Force | Out-Null
        if (Test-Path -LiteralPath $gpuPack) { Remove-Item -LiteralPath $gpuPack -Force -ErrorAction Stop }
        Compress-Archive -Path (Join-Path $gpuStage '*') -DestinationPath $gpuPack -ErrorAction Stop
        Assert-ReleaseTarget -Target $gpuStage -Boundary (Join-Path $projectRoot 'dist')
        Remove-Item -LiteralPath $gpuStage -Recurse -Force -ErrorAction Stop
        Write-Output "GPU pack: $gpuPack"
    } else {
        if (-not (Test-Path -LiteralPath $nvidiaInBundle)) {
            throw 'SHENGJIAN_BUNDLE_CUDA=1 build is missing _internal/nvidia'
        }
    }

    & $python (Join-Path $PSScriptRoot 'collect_release_notices.py') --output (Join-Path $desktopDist 'third-party-notices')
    if ($LASTEXITCODE -ne 0) { throw 'Desktop third-party materials could not be collected' }
    Copy-Item -LiteralPath (Join-Path $projectRoot 'LICENSE') -Destination $desktopDist -ErrorAction Stop
    Copy-Item -LiteralPath (Join-Path $projectRoot 'THIRD_PARTY_NOTICES.md') -Destination $desktopDist -ErrorAction Stop
    'https://github.com/Ansel-Poiesis/FoilPage-source' | Set-Content -LiteralPath (Join-Path $desktopDist 'SOURCE.txt') -Encoding utf8

    $desktopExe = Join-Path $desktopDist 'FoilPage-Desktop.exe'
    $workerExe = Join-Path $workerBundle 'FoilPage-Worker.exe'
    $agentExe = Join-Path $agentBundle 'FoilPage-Agent.exe'
    & $agentExe mcp-config --format json | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Packaged Agent control configuration smoke failed' }
    $extensionManifest = Join-Path $extensionBundle 'manifest.json'
    $extensionFont = Join-Path $extensionBundle 'fonts\LXGWWenKaiScreen.woff2'
    $extensionIcon = Join-Path $extensionBundle 'icons\icon-128.png'
    $desktopTitleSkill = Join-Path $desktopDist '_internal\app\skills\concise-accurate-title\SKILL.md'
    $workerTitleSkill = Join-Path $workerBundle '_internal\app\skills\concise-accurate-title\SKILL.md'
    foreach ($required in ($desktopExe, $workerExe, $agentExe, $extensionManifest, $extensionFont, $extensionIcon, $desktopTitleSkill, $workerTitleSkill)) {
        if (-not (Test-Path -LiteralPath $required)) {
            throw "Release file is missing: $required"
        }
    }

    # PowerShell may expose the version-resource string as empty while the
    # numeric raw value is populated (this occurs with PyInstaller resources
    # that declare a non-default translation table). Prefer the readable value
    # and fall back to FileVersionRaw so the gate checks the resource itself.
    $desktopVersionInfo = (Get-Item -LiteralPath $desktopExe).VersionInfo
    $workerVersionInfo = (Get-Item -LiteralPath $workerExe).VersionInfo
    $desktopFileVersion = if ($desktopVersionInfo.FileVersion) { $desktopVersionInfo.FileVersion } else { [string]$desktopVersionInfo.FileVersionRaw }
    $workerFileVersion = if ($workerVersionInfo.FileVersion) { $workerVersionInfo.FileVersion } else { [string]$workerVersionInfo.FileVersionRaw }
    if ($desktopFileVersion -ne $expectedFileVersion -or $workerFileVersion -ne $expectedFileVersion) {
        throw "EXE version resource mismatch: Desktop=$desktopFileVersion Worker=$workerFileVersion (expected $expectedFileVersion)"
    }

    # The extension is loaded manually by the user from the installed folder, so its
    # manifest version is the only way to tell which extension build is in use.
    $bundledManifest = Get-Content -LiteralPath $extensionManifest -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($bundledManifest.version -ne $appVersion) {
        throw "Bundled extension manifest version mismatch: $($bundledManifest.version) (expected $appVersion)"
    }

    $forbidden = Get-ChildItem -LiteralPath $desktopDist -Recurse -File | Where-Object {
        $_.Name -in @('settings.json', 'library.db', 'queue.db', 'model.bin') -or
        $_.Name -like '*.db-wal' -or $_.Name -like '*.db-shm' -or
        $_.Extension -eq '.log'
    }
    if ($forbidden) {
        throw "Runtime data or model found in release: $($forbidden.FullName -join ', ')"
    }

    $smokeData = Join-Path $env:TEMP ("shengjian-packaged-smoke-$appVersion-" + [guid]::NewGuid().ToString('N'))
    if (Test-Path -LiteralPath $smokeData) { throw 'Packaged smoke data must start in a new directory' }
    New-Item -ItemType Directory -Path $smokeData -ErrorAction Stop | Out-Null
    Write-Output "Isolated packaged smoke data: $smokeData"
    # Desktop configures its default logger before parsing --data-dir. Isolate
    # that environment in the child too, without changing this build's profile.
    $smokeStart = New-Object System.Diagnostics.ProcessStartInfo
    $smokeStart.FileName = $desktopExe
    $smokeStart.Arguments = '--smoke --smoke-preview --data-dir "' + $smokeData + '"'
    $smokeStart.UseShellExecute = $false
    $smokeStart.CreateNoWindow = $true
    $smokeStart.WindowStyle = [Diagnostics.ProcessWindowStyle]::Hidden
    $smokeStart.EnvironmentVariables['LOCALAPPDATA'] = Join-Path $smokeData 'local-app-data'
    $desktopSmoke = [Diagnostics.Process]::Start($smokeStart)
    $desktopSmoke.WaitForExit()
    if ($desktopSmoke.ExitCode -ne 0) { throw 'Packaged Desktop smoke failed' }
    & $workerExe --help 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Packaged Worker smoke failed' }

    if (-not $SkipInstaller) {
        # ProgramFiles(x86) 在部分会话为空；Join-Path(null) 会直接抛错并吞掉
        # 已找到的 LOCALAPPDATA 候选——逐个探测，不要在数组字面量里硬绑。
        $isccCandidates = @(
            (Get-Command ISCC.exe -ErrorAction SilentlyContinue).Source
            Join-Path $env:LOCALAPPDATA 'Programs\Inno Setup 6\ISCC.exe'
            'C:\Program Files (x86)\Inno Setup 6\ISCC.exe'
            'C:\Program Files\Inno Setup 6\ISCC.exe'
        ) | Where-Object { $_ -and (Test-Path -LiteralPath $_) }
        $iscc = $isccCandidates | Select-Object -First 1
        if (-not $iscc) { throw 'Inno Setup 6 ISCC.exe was not found' }
        Write-Output "ISCC: $iscc"
        Assert-ReleaseTarget -Target (Join-Path $projectRoot "dist_installer\FoilPage-Setup-$appVersion.exe") -Boundary (Join-Path $projectRoot 'dist_installer')
        & $iscc build\installer.iss 2>&1
        if ($LASTEXITCODE -ne 0) { throw 'Installer build failed' }
    }
    $finalHead = & git -C $projectRoot rev-parse HEAD
    $finalTree = & git -C $projectRoot rev-parse 'HEAD^{tree}'
    if ($LASTEXITCODE -ne 0 -or $finalHead -ne $releaseHead -or $finalTree -ne $releaseTree) {
        throw 'Release source HEAD or tree changed during the build'
    }
    if (-not $SkipInstaller -and -not $bundleCuda) {
        & $python (Join-Path $PSScriptRoot 'release_manifest.py') --expected-head $releaseHead --expected-tree $releaseTree --smoke-data $smokeData
        if ($LASTEXITCODE -ne 0) { throw 'Release provenance and artifact manifest failed' }
    } else {
        Write-Output 'Dual-artifact release manifest is not applicable with -SkipInstaller or SHENGJIAN_BUNDLE_CUDA=1.'
    }
}
finally {
    Pop-Location
}
