param(
    [string]$Version = "0.3.0",
    [string]$RuntimeRoot = "desktop/runtime/ecorex-runtime",
    [string]$OutputDir = "release-artifacts",
    [switch]$KeepStaging,
    [switch]$SkipCombinedPackage
)

$ErrorActionPreference = "Stop"

function Resolve-RequiredPath {
    param([Parameter(Mandatory = $true)][string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) {
        throw "Required path does not exist: $Path"
    }
    return (Resolve-Path -LiteralPath $Path).Path
}

function Write-Utf8NoBom {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Value
    )
    $encoding = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($Path, ($Value -replace "`r`n", "`n"), $encoding)
}

function Get-EcoreXFileSha256 {
    param([Parameter(Mandatory = $true)][string]$Path)
    $resolved = Resolve-RequiredPath $Path
    if (Get-Command Get-FileHash -ErrorAction SilentlyContinue) {
        return (Get-FileHash -Algorithm SHA256 -LiteralPath $resolved).Hash.ToUpperInvariant()
    }
    $stream = [System.IO.File]::OpenRead($resolved)
    try {
        $sha = [System.Security.Cryptography.SHA256]::Create()
        try {
            return (($sha.ComputeHash($stream) | ForEach-Object { $_.ToString("x2") }) -join "").ToUpperInvariant()
        } finally {
            $sha.Dispose()
        }
    } finally {
        $stream.Dispose()
    }
}

function Remove-GeneratedNoise {
    param([Parameter(Mandatory = $true)][string]$Root)
    Get-ChildItem -LiteralPath $Root -Recurse -Force -Directory -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -in "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache" } |
        ForEach-Object { Remove-Item -LiteralPath $_.FullName -Recurse -Force }
    Get-ChildItem -LiteralPath $Root -Recurse -Force -File -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -like "*.pyc" -or $_.Name -like "*.pyo" -or $_.Name -eq ".DS_Store" -or $_.Name -eq "config.json" } |
        ForEach-Object { Remove-Item -LiteralPath $_.FullName -Force }
}

function Invoke-ReleaseRuntimeSanitizer {
    param([Parameter(Mandatory = $true)][string]$RuntimeDir)
    $sanitizer = Join-Path $repoRoot "scripts\sanitize-ecorex-release-runtime.py"
    if (-not (Test-Path -LiteralPath $sanitizer)) {
        throw "Release sanitizer missing: $sanitizer"
    }
    & python $sanitizer $RuntimeDir
    if ($LASTEXITCODE -ne 0) {
        throw "Release runtime sanitizer failed for $RuntimeDir"
    }
}

function Write-V025RuntimeManifest {
    param(
        [Parameter(Mandatory = $true)][string]$RuntimeDir,
        [Parameter(Mandatory = $true)][string]$PackageRoot,
        [Parameter(Mandatory = $true)][string]$Platform
    )
    $writer = Join-Path $repoRoot "scripts\write-v025-runtime-manifest.py"
    $checker = Join-Path $repoRoot "scripts\check-v025-runtime-manifest.py"
    if (-not (Test-Path -LiteralPath $writer)) {
        throw "v0.2.5 runtime manifest writer missing: $writer"
    }
    if (-not (Test-Path -LiteralPath $checker)) {
        throw "v0.2.5 runtime manifest checker missing: $checker"
    }
    & python $writer --runtime-root $RuntimeDir --package-root $PackageRoot --version $Version --platform $Platform
    if ($LASTEXITCODE -ne 0) {
        throw "v0.2.5 runtime manifest generation failed for $Platform"
    }
    & python $checker (Join-Path $RuntimeDir "runtime-manifest.json") --platform $Platform --version $Version --runtime-root $RuntimeDir --package-root $PackageRoot
    if ($LASTEXITCODE -ne 0) {
        throw "v0.2.5 runtime manifest check failed for $Platform"
    }
}

function Sync-DesktopWebBuild {
    param([Parameter(Mandatory = $true)][string]$RuntimeDir)

    $desktopDist = Join-Path $repoRoot "desktop/dist"
    if (-not (Test-Path -LiteralPath (Join-Path $desktopDist "index.html"))) {
        return
    }

    $appDir = Join-Path $RuntimeDir "channel/web/static/app"
    if (Test-Path -LiteralPath $appDir) {
        Remove-Item -LiteralPath $appDir -Recurse -Force
    }
    New-Item -ItemType Directory -Force -Path $appDir | Out-Null
    Copy-Item -Path (Join-Path $desktopDist "*") -Destination $appDir -Recurse -Force
}

function Sync-CurrentRuntimeSources {
    param([Parameter(Mandatory = $true)][string]$RuntimeDir)

    $sourceItems = @(
        "agent",
        "bridge",
        "channel",
        "cli",
        "common",
        "models",
        "plugins",
        "scripts",
        "skills",
        "tools",
        "translate",
        "voice",
        "app.py",
        "config.py",
        "config-template.json",
        "pyproject.toml",
        "requirements.txt",
        "requirements-optional.txt",
        "enterprise-policy.json",
        "LICENSE"
    )

    foreach ($item in $sourceItems) {
        $source = Join-Path $repoRoot $item
        if (-not (Test-Path -LiteralPath $source)) {
            continue
        }
        $destination = Join-Path $RuntimeDir $item
        if (Test-Path -LiteralPath $destination) {
            Remove-Item -LiteralPath $destination -Recurse -Force
        }
        Copy-Item -LiteralPath $source -Destination $destination -Recurse -Force
    }

    $runtimePackFiles = @(
        @{ Source = "runtime-packs/capabilities.json"; Target = "capabilities.json" },
        @{ Source = "runtime-packs/core-requirements.txt"; Target = "core-requirements.txt" }
    )
    foreach ($entry in $runtimePackFiles) {
        $source = Join-Path $repoRoot $entry.Source
        if (-not (Test-Path -LiteralPath $source)) {
            throw "Required runtime-pack file missing: $source"
        }
        Copy-Item -LiteralPath $source -Destination (Join-Path $RuntimeDir $entry.Target) -Force
    }
}

function Copy-OptionalLarkCliWindows {
    param([Parameter(Mandatory = $true)][string]$RuntimeDir)
    Write-Host "Skipping bundled lark-cli for Windows WebUI runtime; Feishu/Lark connector is discovery-only and installs through find-skill plus on-demand @larksuite/cli."
}

function Copy-OptionalLarkCliMac {
    param([Parameter(Mandatory = $true)][string]$RuntimeDir)
    Write-Host "Skipping bundled lark-cli for macOS WebUI runtime; Feishu/Lark connector is discovery-only and installs through find-skill plus on-demand @larksuite/cli."
}

function Copy-DirectoryWithRobocopy {
    param(
        [Parameter(Mandatory = $true)][string]$Source,
        [Parameter(Mandatory = $true)][string]$Destination
    )
    New-Item -ItemType Directory -Force -Path $Destination | Out-Null
    robocopy $Source $Destination /MIR /R:2 /W:1 /XD __pycache__ .pytest_cache .mypy_cache .ruff_cache /XF *.pyc *.pyo config.json | Out-Null
    if ($LASTEXITCODE -gt 7) {
        throw "Failed to copy $Source to $Destination; robocopy exit code $LASTEXITCODE"
    }
    $global:LASTEXITCODE = 0
}

function ConvertTo-WindowsLongPath {
    param([Parameter(Mandatory = $true)][string]$Path)
    $full = [System.IO.Path]::GetFullPath($Path)
    if ($env:OS -ne "Windows_NT" -or $full.StartsWith("\\?\")) {
        return $full
    }
    if ($full.StartsWith("\\")) {
        return "\\?\UNC\" + $full.TrimStart("\")
    }
    return "\\?\" + $full
}

function Remove-PathRobust {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Base
    )
    if (-not (Test-Path -LiteralPath $Path)) {
        return
    }
    $full = [System.IO.Path]::GetFullPath($Path)
    $baseFull = [System.IO.Path]::GetFullPath($Base).TrimEnd("\", "/")
    if (-not ($full.Equals($baseFull, [System.StringComparison]::OrdinalIgnoreCase) -or $full.StartsWith($baseFull + [System.IO.Path]::DirectorySeparatorChar, [System.StringComparison]::OrdinalIgnoreCase))) {
        throw "Refusing to remove path outside release output directory: $full"
    }
    try {
        Remove-Item -LiteralPath $full -Recurse -Force
    } catch {
        $long = ConvertTo-WindowsLongPath $full
        if ([System.IO.Directory]::Exists($long)) {
            [System.IO.Directory]::Delete($long, $true)
        } elseif ([System.IO.File]::Exists($long)) {
            [System.IO.File]::Delete($long)
        } else {
            throw
        }
    }
}

function Save-Download {
    param(
        [Parameter(Mandatory = $true)][string]$Uri,
        [Parameter(Mandatory = $true)][string]$Destination
    )
    if (Test-Path -LiteralPath $Destination) {
        return
    }
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $Destination) | Out-Null
    Write-Host "Downloading $Uri"
    Invoke-WebRequest -UseBasicParsing -Uri $Uri -OutFile $Destination
}

function New-ReleaseJson {
    param(
        [string]$ArtifactId,
        [string]$Platform,
        [string]$InstallEntry
    )
    return ([ordered]@{
        product = "EcoreX"
        version = $Version
        artifactId = $ArtifactId
        platform = $Platform
        installEntry = $InstallEntry
        generatedAt = (Get-Date).ToUniversalTime().ToString("o")
        defaultUrl = "http://127.0.0.1:9909/app/"
        bindHost = "127.0.0.1"
        auth = "disabled for local-only install"
    } | ConvertTo-Json -Depth 6) + "`n"
}

function Get-ReleaseMigrationReadmeNote {
    $readmePath = Join-Path $repoRoot "desktop\build\README-migration.txt"
    if (-not (Test-Path -LiteralPath $readmePath)) {
        throw "Release migration README missing: $readmePath"
    }
    return (Get-Content -Raw -Encoding UTF8 -LiteralPath $readmePath).TrimEnd()
}

function New-MacInstallerApp {
    param(
        [Parameter(Mandatory = $true)][string]$AppRoot,
        [Parameter(Mandatory = $true)][string]$InstallScriptRelative,
        [switch]$SelfContainedResources
    )

    $contentsDir = Join-Path $AppRoot "Contents"
    $macOsDir = Join-Path $contentsDir "MacOS"
    New-Item -ItemType Directory -Force -Path $macOsDir | Out-Null

    $plist = @'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleExecutable</key>
  <string>Install EcoreX WebUI</string>
  <key>CFBundleIdentifier</key>
  <string>cn.ecoreai.ecorex.webui.installer</string>
  <key>CFBundleName</key>
  <string>Install EcoreX WebUI</string>
  <key>CFBundlePackageType</key>
  <string>APPL</string>
  <key>CFBundleShortVersionString</key>
  <string>1.0</string>
  <key>LSUIElement</key>
  <true/>
</dict>
</plist>
'@
    Write-Utf8NoBom -Path (Join-Path $contentsDir "Info.plist") -Value $plist

    $launcher = @'
#!/usr/bin/env bash
set -euo pipefail

APP_EXEC_DIR="$(cd "$(dirname "$0")" && pwd)"
PACKAGE_ROOT="$(__PACKAGE_ROOT_COMMAND__)"
INSTALL_SCRIPT="$PACKAGE_ROOT/__INSTALL_SCRIPT_RELATIVE__"
STATE_DIR="$HOME/Library/Application Support/EcoreX WebUI/state"
LOG_FILE="$STATE_DIR/install.log"
ERR_FILE="$STATE_DIR/install.err.log"

mkdir -p "$STATE_DIR"
/usr/bin/osascript -e 'display notification "Installing and starting the local WebUI. Your browser will open when it is ready." with title "EcoreX WebUI"' >/dev/null 2>&1 || true

(
  set -euo pipefail
  echo "==== EcoreX WebUI install started $(date -u +%Y-%m-%dT%H:%M:%SZ) ===="
  if command -v xattr >/dev/null 2>&1; then
    xattr -dr com.apple.quarantine "$PACKAGE_ROOT" >/dev/null 2>&1 || true
    xattr -dr com.apple.quarantine "$HOME/Library/Application Support/EcoreX WebUI" >/dev/null 2>&1 || true
  fi
  bash "$INSTALL_SCRIPT"
  URL_FILE="$STATE_DIR/ecorex-webui.url"
  if [[ "${OPEN_BROWSER:-1}" == "1" && -f "$URL_FILE" ]]; then
    WEBUI_URL="$(cat "$URL_FILE" 2>/dev/null || true)"
    if [[ -n "$WEBUI_URL" ]]; then
      /usr/bin/open "$WEBUI_URL" >/dev/null 2>&1 || true
    fi
  fi
  /usr/bin/osascript -e 'display notification "EcoreX WebUI is running and the browser has been opened." with title "EcoreX WebUI"' >/dev/null 2>&1 || true
) >> "$LOG_FILE" 2>> "$ERR_FILE" || {
  /usr/bin/osascript -e "display dialog \"EcoreX WebUI installation failed. Check the log: $ERR_FILE\" buttons {\"OK\"} default button \"OK\" with icon caution" >/dev/null 2>&1 || true
  exit 1
}
'@
    $packageRootCommand = if ($SelfContainedResources) {
        'cd "$APP_EXEC_DIR/../Resources/package" && pwd'
    } else {
        'cd "$APP_EXEC_DIR/../../.." && pwd'
    }
    $launcher = $launcher.Replace("__PACKAGE_ROOT_COMMAND__", $packageRootCommand)
    $launcher = $launcher.Replace("__INSTALL_SCRIPT_RELATIVE__", $InstallScriptRelative)
    Write-Utf8NoBom -Path (Join-Path $macOsDir "Install EcoreX WebUI") -Value $launcher
}

function Compress-ZipWithUnixPermissions {
    param(
        [Parameter(Mandatory = $true)][string]$SourceRoot,
        [Parameter(Mandatory = $true)][string]$DestinationPath,
        [string[]]$ExecutableRelativePaths = @(),
        [switch]$ExcludeRootDirectory,
        [int]$CompressionLevel = 6
    )

    $python = @'
import os
import shutil
import sys
import time
import zipfile

def fs_path(path):
    resolved = os.path.abspath(path)
    if os.name != "nt":
        return resolved
    if resolved.startswith("\\\\?\\"):
        return resolved
    if resolved.startswith("\\\\"):
        return "\\\\?\\UNC\\" + resolved.lstrip("\\")
    return "\\\\?\\" + resolved

mode = "--include-root"
arg_offset = 3
if len(sys.argv) > 3 and sys.argv[3].startswith("--"):
    mode = sys.argv[3]
    arg_offset = 5

source = os.path.abspath(sys.argv[1])
destination = os.path.abspath(sys.argv[2])
compression_level = int(sys.argv[4]) if arg_offset == 5 else 6
source_fs = fs_path(source)
destination_fs = fs_path(destination)
executable = {
    item.replace("\\", "/").strip("/")
    for item in sys.argv[arg_offset:]
    if item
}
base = source if mode == "--exclude-root" else os.path.dirname(source)
base_fs = fs_path(base)

with zipfile.ZipFile(destination_fs, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=compression_level) as archive:
    for root, dirs, files in os.walk(source_fs):
        dirs.sort()
        files.sort()
        for name in files:
            path = os.path.join(root, name)
            rel = os.path.relpath(path, base_fs).replace(os.sep, "/")
            rel_in_source = os.path.relpath(path, source_fs).replace(os.sep, "/")
            stat_result = os.stat(path)
            info = zipfile.ZipInfo(rel, time.localtime(stat_result.st_mtime)[:6])
            mode = 0o100755 if (
                rel in executable
                or
                rel_in_source in executable
                or rel_in_source.endswith(".command")
                or rel_in_source.endswith(".sh")
            ) else 0o100644
            info.external_attr = mode << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            info._compresslevel = compression_level
            with open(path, "rb") as handle, archive.open(info, "w", force_zip64=True) as target:
                shutil.copyfileobj(handle, target, length=1024 * 1024)
'@

    $helperPath = Join-Path ([System.IO.Path]::GetTempPath()) ("ecorex-zip-" + [guid]::NewGuid().ToString("N") + ".py")
    $encoding = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($helperPath, $python, $encoding)
    try {
        $mode = if ($ExcludeRootDirectory) { "--exclude-root" } else { "--include-root" }
        & python $helperPath $SourceRoot $DestinationPath $mode $CompressionLevel $ExecutableRelativePaths
        if ($LASTEXITCODE -ne 0) {
            throw "zip packaging failed for $SourceRoot"
        }
    } finally {
        Remove-Item -LiteralPath $helperPath -Force -ErrorAction SilentlyContinue
    }
}

function Invoke-PipDownload {
    param(
        [string]$Platform,
        [string]$Destination,
        [string]$RequirementsPath = "runtime-packs/core-requirements.txt"
    )
    $requirementsPath = Resolve-RequiredPath $RequirementsPath
    $requirementsHash = Get-EcoreXFileSha256 -Path $requirementsPath
    $stampPath = Join-Path $Destination ".requirements-$Platform.sha256"
    $expectedStamp = "$Platform $requirementsHash"
    if (
        (Test-Path -LiteralPath $stampPath) -and
        ((Get-Content -Raw -LiteralPath $stampPath).Trim() -eq $expectedStamp) -and
        @(Get-ChildItem -LiteralPath $Destination -Filter "*.whl" -ErrorAction SilentlyContinue).Count -gt 0
    ) {
        return
    }
    if (Test-Path -LiteralPath $Destination) {
        Remove-Item -LiteralPath $Destination -Recurse -Force
    }
    New-Item -ItemType Directory -Force -Path $Destination | Out-Null
    & python -m pip download `
        -r $requirementsPath `
        --platform $Platform `
        --python-version 311 `
        --implementation cp `
        --abi cp311 `
        --only-binary=:all: `
        --dest $Destination
    if ($LASTEXITCODE -ne 0) {
        throw "pip download failed for $Platform"
    }
    Write-Utf8NoBom -Path $stampPath -Value ($expectedStamp + "`n")
}

function Test-PackagedPythonModule {
    param(
        [Parameter(Mandatory = $true)][string]$Python,
        [Parameter(Mandatory = $true)][string]$ModuleName
    )
    & $Python -s -c "import importlib.util, sys; sys.exit(0 if importlib.util.find_spec('$ModuleName') else 1)"
    return ($LASTEXITCODE -eq 0)
}

function Install-WindowsRuntimeDependency {
    param(
        [Parameter(Mandatory = $true)][string]$RuntimeDir,
        [Parameter(Mandatory = $true)][string]$ModuleName,
        [Parameter(Mandatory = $true)][string]$PackageSpec,
        [Parameter(Mandatory = $true)][string]$Reason
    )
    $python = Join-Path $RuntimeDir "python\python.exe"
    if (-not (Test-Path -LiteralPath $python)) {
        throw "Packaged Windows Python runtime is missing: $python"
    }
    if (Test-PackagedPythonModule -Python $python -ModuleName $ModuleName) {
        return
    }
    Write-Host "Preinstalling Windows Python dependency for ${Reason}: $PackageSpec"
    $oldNoUserSite = $env:PYTHONNOUSERSITE
    $oldDontWriteBytecode = $env:PYTHONDONTWRITEBYTECODE
    $env:PYTHONNOUSERSITE = "1"
    $env:PYTHONDONTWRITEBYTECODE = "1"
    try {
        & $python -s -m pip install --disable-pip-version-check --no-cache-dir --prefer-binary --no-compile --no-warn-script-location --timeout 60 --retries 2 $PackageSpec
        if ($LASTEXITCODE -ne 0) {
            throw "Failed to preinstall Windows Python dependency '$PackageSpec' for $Reason"
        }
    } finally {
        if ($null -eq $oldNoUserSite) {
            Remove-Item Env:\PYTHONNOUSERSITE -ErrorAction SilentlyContinue
        } else {
            $env:PYTHONNOUSERSITE = $oldNoUserSite
        }
        if ($null -eq $oldDontWriteBytecode) {
            Remove-Item Env:\PYTHONDONTWRITEBYTECODE -ErrorAction SilentlyContinue
        } else {
            $env:PYTHONDONTWRITEBYTECODE = $oldDontWriteBytecode
        }
    }
    if (-not (Test-PackagedPythonModule -Python $python -ModuleName $ModuleName)) {
        throw "Windows Python dependency '$PackageSpec' installed but module '$ModuleName' is still unavailable."
    }
}

function New-LocalMacCoreRequirements {
    param([Parameter(Mandatory = $true)][string]$Destination)
    $source = Resolve-RequiredPath "runtime-packs/core-requirements.txt"
    $lines = Get-Content -Encoding UTF8 -LiteralPath $source | Where-Object {
        $normalized = $_.Trim().ToLowerInvariant()
        $normalized -and
            -not $normalized.StartsWith("rapidocr-onnxruntime")
    }
    Write-Utf8NoBom -Path $Destination -Value (($lines -join "`n") + "`n")
    return $Destination
}

$repoRoot = (Resolve-Path -LiteralPath ".").Path
$runtimeRootResolved = Resolve-RequiredPath $RuntimeRoot
$outputResolved = [System.IO.Path]::GetFullPath((Join-Path $repoRoot $OutputDir))
$stagingRoot = Join-Path $outputResolved "ecorex-webui-local-$Version"
$cacheRoot = Join-Path $outputResolved "webui-local-cache"
$windowsLeaf = "ecorex-webui-windows-x64-$Version"
$macLeaf = "ecorex-webui-macos-universal-$Version"
$combinedLeaf = "ecorex-webui-win-mac-$Version"
$windowsStage = Join-Path $stagingRoot $windowsLeaf
$macStage = Join-Path $stagingRoot $macLeaf
$combinedStage = Join-Path $stagingRoot $combinedLeaf
$windowsZip = Join-Path $outputResolved "EcoreX_${Version}-webui-windows-x64.zip"
$macZip = Join-Path $outputResolved "EcoreX_${Version}-webui-macos-universal.zip"
$combinedZip = Join-Path $outputResolved "EcoreX_${Version}-webui-win-mac.zip"

New-Item -ItemType Directory -Force -Path $outputResolved, $cacheRoot | Out-Null
foreach ($path in @($stagingRoot, $windowsZip, $macZip, $combinedZip)) {
    Remove-PathRobust -Path $path -Base $outputResolved
}
New-Item -ItemType Directory -Force -Path $windowsStage, $macStage, $combinedStage | Out-Null

$winRuntime = Join-Path $windowsStage "runtime"
Copy-Item -LiteralPath $runtimeRootResolved -Destination $winRuntime -Recurse -Force
Sync-CurrentRuntimeSources -RuntimeDir $winRuntime
Remove-GeneratedNoise -Root $winRuntime
Sync-DesktopWebBuild -RuntimeDir $winRuntime
Copy-OptionalLarkCliWindows -RuntimeDir $winRuntime
Invoke-ReleaseRuntimeSanitizer -RuntimeDir $winRuntime
Install-WindowsRuntimeDependency -RuntimeDir $winRuntime -ModuleName "web" -PackageSpec "web.py>=0.76,<0.77" -Reason "WebUI web.py runtime"
Install-WindowsRuntimeDependency -RuntimeDir $winRuntime -ModuleName "chardet" -PackageSpec "chardet>=5.1.0" -Reason "WebUI request encoding detection"
Install-WindowsRuntimeDependency -RuntimeDir $winRuntime -ModuleName "numpy" -PackageSpec "numpy>=1.21" -Reason "WebUI runtime data processing"
Install-WindowsRuntimeDependency -RuntimeDir $winRuntime -ModuleName "lark_oapi" -PackageSpec "lark-oapi>=1.5.5" -Reason "Feishu/Lark websocket external connection"
Remove-GeneratedNoise -Root $winRuntime
Write-V025RuntimeManifest -RuntimeDir $winRuntime -PackageRoot $windowsStage -Platform "windows-x64"

$windowsCmd = @'
@echo off
setlocal
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\install-ecorex-webui-win.ps1"
if errorlevel 1 (
  echo.
  echo EcoreX WebUI installation failed. Keep this window open and send the error above to support.
  pause
)
'@

$windowsPs1 = @'
param(
    [int]$Port = 9909,
    [switch]$NoBrowser,
    [ValidateSet("manual", "background")]
    [string]$UpdateMode = ""
)

$ErrorActionPreference = "Stop"

Write-Host "EcoreX WebUI package installer: __ECOREX_VERSION__"

if (-not $UpdateMode) {
    $UpdateMode = if ($env:ECOREX_UPDATE_MODE) { [string]$env:ECOREX_UPDATE_MODE } else { "manual" }
}
$backgroundUpdate = $UpdateMode -ieq "background"

function Test-PortAvailable {
    param([int]$Port)
    $listener = $null
    try {
        $listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Parse("127.0.0.1"), $Port)
        $listener.Start()
        return $true
    } catch {
        return $false
    } finally {
        if ($listener) { $listener.Stop() }
    }
}

function Get-FreePort {
    param([int]$Preferred)
    for ($candidate = $Preferred; $candidate -lt ($Preferred + 50); $candidate++) {
        if (Test-PortAvailable -Port $candidate) {
            return $candidate
        }
    }
    throw "No free local port found near $Preferred"
}

function Wait-PortAvailable {
    param(
        [int]$Port,
        [int]$TimeoutSeconds = 30
    )
    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    do {
        if (Test-PortAvailable -Port $Port) {
            return $true
        }
        Start-Sleep -Milliseconds 500
    } while ([DateTime]::UtcNow -lt $deadline)
    return $false
}

function Wait-WebUi {
    param([string]$Url)
    for ($i = 0; $i -lt 60; $i++) {
        try {
            Invoke-WebRequest -UseBasicParsing -Uri $Url -TimeoutSec 2 | Out-Null
            return
        } catch {
            Start-Sleep -Seconds 1
        }
    }
    throw "EcoreX WebUI did not become ready at $Url"
}

$script:PreUpdateExternalConnections = [ordered]@{ status = "not_checked"; configuredIds = @(); connectedIds = @(); callableIds = @(); reason = "not_started"; redacted = $true }
$script:PostUpdateExternalConnections = [ordered]@{ status = "not_checked"; configuredIds = @(); connectedIds = @(); callableIds = @(); reason = "not_started"; redacted = $true }
$script:ExternalConnectionMissingIds = @()

function Get-ExternalConnectionSnapshot {
    param(
        [Parameter(Mandatory = $true)][string]$StateDir,
        [string]$BaseUrl = "",
        [string]$Reason = ""
    )
    $base = $BaseUrl
    if (-not $base) {
        $base = Get-WebUiBaseUrl -StateDir $StateDir
    }
    if (-not $base) {
        return [ordered]@{ status = "unavailable"; configuredIds = @(); connectedIds = @(); callableIds = @(); reason = "base_url_missing"; redacted = $true }
    }
    try {
        $root = $base.TrimEnd("/")
        $projection = Invoke-RestMethod -UseBasicParsing -Uri ($root + "/api/external-connections") -TimeoutSec 8
        $connections = @($projection.connections)
        $configured = @($connections | Where-Object { $_.configured } | ForEach-Object { [string]$_.id } | Where-Object { $_ } | Sort-Object -Unique)
        $connected = @($connections | Where-Object { $_.connected } | ForEach-Object { [string]$_.id } | Where-Object { $_ } | Sort-Object -Unique)
        $callable = @($connections | Where-Object { $_.callable } | ForEach-Object { [string]$_.id } | Where-Object { $_ } | Sort-Object -Unique)
        try {
            $tencentDocs = Invoke-RestMethod -UseBasicParsing -Uri ($root + "/api/tencent-docs/status?start=1") -TimeoutSec 12
            $capability = $tencentDocs.capability
            if ($capability -and $capability.configured) {
                $configured += "tencent-docs"
                if ($capability.connected -or [int]($capability.toolCount) -gt 0) {
                    $connected += "tencent-docs"
                    $callable += "tencent-docs"
                }
            }
        } catch {
            Write-Warning "Tencent Docs connector health probe skipped: $($_.Exception.Message)"
        }
        return [ordered]@{
            status = "pass"
            reason = $Reason
            configuredIds = @($configured | Sort-Object -Unique)
            connectedIds = @($connected | Sort-Object -Unique)
            callableIds = @($callable | Sort-Object -Unique)
            checkedAt = [DateTime]::UtcNow.ToString("o")
            redacted = $true
        }
    } catch {
        return [ordered]@{
            status = "unavailable"
            reason = if ($Reason) { $Reason } else { "probe_failed" }
            error = $_.Exception.GetType().Name
            configuredIds = @()
            connectedIds = @()
            callableIds = @()
            checkedAt = [DateTime]::UtcNow.ToString("o")
            redacted = $true
        }
    }
}

function Compare-ExternalConnectionSnapshots {
    param(
        [Parameter(Mandatory = $true)]$Before,
        [Parameter(Mandatory = $true)]$After
    )
    if (-not $Before -or [string]$Before.status -ne "pass") { return @() }
    if (-not $After -or [string]$After.status -ne "pass") { return @($Before.configuredIds + $Before.connectedIds + $Before.callableIds | Where-Object { $_ } | Sort-Object -Unique) }
    $required = @($Before.configuredIds + $Before.connectedIds + $Before.callableIds | Where-Object { $_ } | Sort-Object -Unique)
    if (-not $required.Count) { return @() }
    $available = @($After.configuredIds + $After.connectedIds + $After.callableIds | Where-Object { $_ } | Sort-Object -Unique)
    return @($required | Where-Object { $available -notcontains $_ })
}

function Get-ExternalConnectionHealthPayload {
    param([Parameter(Mandatory = $true)][string]$Status)
    $terminalPass = $Status -in @("installed", "activated")
    $terminalFail = $Status -in @("failed", "rollback")
    $missing = @($script:ExternalConnectionMissingIds | Sort-Object -Unique)
    $baselineStatus = [string]$script:PreUpdateExternalConnections.status
    $healthStatus = if ($missing.Count -gt 0 -or $terminalFail) {
        "failed"
    } elseif ($terminalPass -and $baselineStatus -eq "pass") {
        "pass"
    } elseif ($terminalPass) {
        "not_applicable"
    } else {
        "pending"
    }
    return [ordered]@{
        required = $true
        status = $healthStatus
        passed = ($healthStatus -in @("pass", "not_applicable"))
        policy = "preserve configured and connected external tools across online update"
        preservedRoots = @("workspace mcp.json", "state appdata", "state tools")
        before = $script:PreUpdateExternalConnections
        after = $script:PostUpdateExternalConnections
        missingIds = $missing
        redacted = $true
    }
}

function Write-UpdateState {
    param(
        [Parameter(Mandatory = $true)][string]$StateDir,
        [Parameter(Mandatory = $true)][string]$Status,
        [string]$Reason = "",
        [string]$Url = ""
    )
    try {
        New-Item -ItemType Directory -Force -Path $StateDir | Out-Null
        $payload = [ordered]@{
            product = "EcoreX WebUI"
            version = "__ECOREX_VERSION__"
            mode = $UpdateMode
            status = $Status
            reason = $Reason
            url = $Url
            browserAction = if ($backgroundUpdate) { "defer-to-existing-tab-soft-refresh" } elseif ($NoBrowser) { "none" } else { "open-default-browser" }
            activationPolicy = if ($backgroundUpdate) { "prompt-soft-refresh-existing-tab" } else { "manual-open-browser" }
            autoLaunchBrowser = if ($backgroundUpdate) { "never-in-background" } elseif ($NoBrowser) { "disabled-by-user" } else { "manual-install-only" }
            healthCheck = [ordered]@{
                endpoint = "/api/version"
                status = if ($Status -eq "installed" -or $Status -eq "activated") { "pass" } elseif ($Status -eq "failed" -or $Status -eq "rollback") { "failed" } else { "pending" }
                passed = ($Status -eq "installed" -or $Status -eq "activated")
            }
            externalConnections = Get-ExternalConnectionHealthPayload -Status $Status
            generatedAt = [DateTime]::UtcNow.ToString("o")
        }
        [System.IO.File]::WriteAllText((Join-Path $StateDir "update-state.json"), (($payload | ConvertTo-Json -Depth 8) + [Environment]::NewLine), [System.Text.UTF8Encoding]::new($false))
    } catch {
        Write-Warning "Could not write update state: $($_.Exception.Message)"
    }
}

function Get-WebUiBaseUrl {
    param([Parameter(Mandatory = $true)][string]$StateDir)
    $urlFile = Join-Path $StateDir "ecorex-webui.url"
    if (-not (Test-Path -LiteralPath $urlFile)) { return "" }
    try {
        $raw = (Get-Content -Raw -LiteralPath $urlFile).Trim()
        if (-not $raw) { return "" }
        $uri = [Uri]$raw
        return $uri.GetLeftPart([System.UriPartial]::Authority)
    } catch {
        return ""
    }
}

function Get-ActiveRequestCount {
    param([Parameter(Mandatory = $true)][string]$StateDir)
    $base = Get-WebUiBaseUrl -StateDir $StateDir
    if (-not $base) { return 0 }
    try {
        $snapshot = Invoke-RestMethod -UseBasicParsing -Uri ($base.TrimEnd("/") + "/api/active-requests") -TimeoutSec 3
        return @($snapshot.requests).Count
    } catch {
        return -1
    }
}

function Ensure-LarkCli {
    param(
        [Parameter(Mandatory = $true)][string]$RuntimeDir,
        [Parameter(Mandatory = $true)][string]$StateDir
    )
    "lark-cli preinstall skipped. The structured feishu_cli tool remains visible and installs @larksuite/cli@1.0.56 on demand into the writable state directory after the find-skill gate; npmjs.org timeout should fall back to https://registry.npmmirror.com." | Out-File -FilePath (Join-Path $StateDir "lark-cli-install.log") -Encoding utf8 -Append
}

function Test-PythonModule {
    param(
        [Parameter(Mandatory = $true)][string]$Python,
        [Parameter(Mandatory = $true)][string]$ModuleName
    )
    & $Python -s -c "import importlib.util, sys; sys.exit(0 if importlib.util.find_spec('$ModuleName') else 1)"
    return ($LASTEXITCODE -eq 0)
}

function Write-OptionalPythonDependencyNotice {
    param(
        [Parameter(Mandatory = $true)][string]$StateDir,
        [Parameter(Mandatory = $true)][string]$ModuleName,
        [Parameter(Mandatory = $true)][string]$PackageSpec,
        [Parameter(Mandatory = $true)][string]$Reason
    )
    $logPath = Join-Path $StateDir "python-deps-install.log"
    "Optional Python dependency skipped during first-run install for ${Reason}: ${PackageSpec}. Module ${ModuleName} will be installed or reported by the Feishu External Connection flow when that feature is configured." | Out-File -FilePath $logPath -Encoding utf8 -Append
}

function Test-TextContains {
    param(
        [string]$Text,
        [string]$Needle
    )
    if ([string]::IsNullOrWhiteSpace($Text) -or [string]::IsNullOrWhiteSpace($Needle)) {
        return $false
    }
    return $Text.IndexOf($Needle, [StringComparison]::OrdinalIgnoreCase) -ge 0
}

function Test-WebUiProcessCommandLine {
    param(
        [string]$CommandLine,
        [string]$RuntimeDir,
        [string]$InstallRoot
    )
    if (-not (Test-TextContains -Text $CommandLine -Needle "app.py")) {
        return $false
    }
    if (Test-TextContains -Text $CommandLine -Needle $RuntimeDir) {
        return $true
    }
    if (Test-TextContains -Text $CommandLine -Needle $InstallRoot) {
        return $true
    }
    return $false
}

function Get-WebUiPythonProcesses {
    param(
        [string]$RuntimeDir = "",
        [string]$InstallRoot = ""
    )
    try {
        return Get-CimInstance Win32_Process -Filter "Name = 'python.exe' OR Name = 'pythonw.exe'" -ErrorAction Stop |
            Where-Object { Test-WebUiProcessCommandLine -CommandLine $_.CommandLine -RuntimeDir $RuntimeDir -InstallRoot $InstallRoot }
    } catch {
        return Get-WmiObject Win32_Process -Filter "Name = 'python.exe' OR Name = 'pythonw.exe'" -ErrorAction SilentlyContinue |
            Where-Object { Test-WebUiProcessCommandLine -CommandLine $_.CommandLine -RuntimeDir $RuntimeDir -InstallRoot $InstallRoot }
    }
}

function Stop-ExistingWebUi {
    param(
        [string]$RuntimeDir = "",
        [string]$InstallRoot = ""
    )
    $processes = @(Get-WebUiPythonProcesses -RuntimeDir $RuntimeDir -InstallRoot $InstallRoot)
    if ($processes.Count -eq 0) { return }

    Write-Host "Stopping existing EcoreX WebUI local service..."
    $processIds = @()
    foreach ($process in $processes) {
        try {
            $processIds += [int]$process.ProcessId
            Stop-Process -Id $process.ProcessId -Force -ErrorAction Stop
        } catch {
            Write-Warning "Could not stop process $($process.ProcessId): $($_.Exception.Message)"
        }
    }

    $deadline = [DateTime]::UtcNow.AddSeconds(12)
    do {
        $remaining = @($processIds | Where-Object { Get-Process -Id $_ -ErrorAction SilentlyContinue })
        if ($remaining.Count -eq 0) { return }
        Start-Sleep -Milliseconds 400
    } while ([DateTime]::UtcNow -lt $deadline)

    $remaining = @($processIds | Where-Object { Get-Process -Id $_ -ErrorAction SilentlyContinue })
    if ($remaining.Count -gt 0) {
        $remainingText = ($remaining -join ", ")
        throw "Timed out stopping EcoreX WebUI process(es): $remainingText"
    }
}

function Remove-OldRuntimeDirs {
    param(
        [Parameter(Mandatory = $true)][string]$InstallRoot,
        [Parameter(Mandatory = $true)][string]$CurrentRuntimeDir
    )
    if (-not (Test-Path -LiteralPath $InstallRoot -PathType Container)) { return }
    $currentFull = [System.IO.Path]::GetFullPath($CurrentRuntimeDir)
    foreach ($dir in Get-ChildItem -LiteralPath $InstallRoot -Directory -ErrorAction SilentlyContinue) {
        if ($dir.Name -ne "runtime" -and $dir.Name -notlike "runtime-*") { continue }
        $dirFull = [System.IO.Path]::GetFullPath($dir.FullName)
        if ($dirFull -ieq $currentFull) { continue }
        try {
            Remove-Item -LiteralPath $dir.FullName -Recurse -Force -ErrorAction Stop
        } catch {
            Write-Warning "Could not remove old runtime directory $($dir.FullName): $($_.Exception.Message)"
        }
    }
}

function Add-DesktopCandidate {
    param(
        [System.Collections.Generic.List[string]]$Dirs,
        [string]$Path,
        [switch]$ExistingOnly
    )
    if ([string]::IsNullOrWhiteSpace($Path)) { return }
    try {
        $expanded = [Environment]::ExpandEnvironmentVariables($Path)
        if ([string]::IsNullOrWhiteSpace($expanded)) { return }
        $full = [System.IO.Path]::GetFullPath($expanded)
        if ($ExistingOnly -and -not (Test-Path -LiteralPath $full -PathType Container)) { return }
        if (-not ($Dirs | Where-Object { $_ -ieq $full })) {
            $Dirs.Add($full) | Out-Null
        }
    } catch {
    }
}

function Get-DesktopShortcutPaths {
    $dirs = [System.Collections.Generic.List[string]]::new()
    Add-DesktopCandidate -Dirs $dirs -Path ([Environment]::GetFolderPath([Environment+SpecialFolder]::DesktopDirectory))
    Add-DesktopCandidate -Dirs $dirs -Path ([Environment]::GetFolderPath([Environment+SpecialFolder]::Desktop))
    Add-DesktopCandidate -Dirs $dirs -Path (Join-Path $env:USERPROFILE "Desktop")

    foreach ($name in @("OneDriveCommercial", "OneDriveConsumer", "OneDrive")) {
        $root = [Environment]::GetEnvironmentVariable($name)
        if ($root) {
            Add-DesktopCandidate -Dirs $dirs -Path (Join-Path $root "Desktop") -ExistingOnly
        }
    }

    try {
        $registryDesktop = (Get-ItemProperty -Path "HKCU:\Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders" -Name Desktop -ErrorAction Stop).Desktop
        Add-DesktopCandidate -Dirs $dirs -Path $registryDesktop
    } catch {
    }

    return @($dirs.ToArray() | ForEach-Object { Join-Path $_ "EcoreX WebUI.lnk" })
}

function Write-WebUiLauncher {
    param([Parameter(Mandatory = $true)][string]$InstallRoot)
    $launcherPath = Join-Path $InstallRoot "Launch EcoreX WebUI.ps1"
    $launcherLines = @(
        'param([switch]$NoBrowser)',
        '$ErrorActionPreference = "Stop"',
        '$InstallRoot = Split-Path -Parent $MyInvocation.MyCommand.Path',
        '$StateDir = Join-Path $InstallRoot "state"',
        '$LogPath = Join-Path $StateDir "ecorex-webui.reopen.log"',
        '$ErrorLogPath = Join-Path $StateDir "ecorex-webui.reopen.err.log"',
        'New-Item -ItemType Directory -Force -Path $StateDir | Out-Null',
        '',
        'function Get-CurrentRuntimeDir {',
        '    $currentRuntimePath = Join-Path $StateDir "current-runtime.txt"',
        '    if (Test-Path -LiteralPath $currentRuntimePath) {',
        '        $candidate = (Get-Content -Raw -LiteralPath $currentRuntimePath).Trim()',
        '        if ($candidate -and (Test-Path -LiteralPath $candidate -PathType Container)) { return $candidate }',
        '    }',
        '    $candidates = @(Get-ChildItem -LiteralPath $InstallRoot -Directory -Filter "runtime-*" -ErrorAction SilentlyContinue | Sort-Object LastWriteTimeUtc -Descending)',
        '    if ($candidates.Count -gt 0) { return $candidates[0].FullName }',
        '    $legacy = Join-Path $InstallRoot "runtime"',
        '    if (Test-Path -LiteralPath $legacy -PathType Container) { return $legacy }',
        '    return ""',
        '}',
        '',
        'function Get-WebUiPort {',
        '    param([Parameter(Mandatory = $true)][string]$RuntimeDir)',
        '    $configPath = Join-Path $RuntimeDir "config.json"',
        '    if (Test-Path -LiteralPath $configPath) {',
        '        try {',
        '            $config = Get-Content -Raw -LiteralPath $configPath | ConvertFrom-Json',
        '            if ($config.web_port) { return [int]$config.web_port }',
        '        } catch {',
        '        }',
        '    }',
        '    return 9909',
        '}',
        '',
        'function Test-WebUiReady {',
        '    param([Parameter(Mandatory = $true)][string]$Url)',
        '    try {',
        '        $response = Invoke-WebRequest -UseBasicParsing -Uri $Url -TimeoutSec 2',
        '        return [int]$response.StatusCode -lt 500',
        '    } catch {',
        '        if ($_.Exception.Response -and [int]$_.Exception.Response.StatusCode -lt 500) { return $true }',
        '        return $false',
        '    }',
        '}',
        '',
        'function Wait-WebUiReady {',
        '    param([Parameter(Mandatory = $true)][string]$Url)',
        '    for ($i = 0; $i -lt 60; $i++) {',
        '        if (Test-WebUiReady -Url $Url) { return }',
        '        Start-Sleep -Seconds 1',
        '    }',
        '    throw "EcoreX WebUI did not become ready at $Url"',
        '}',
        '',
        '$runtimeDir = Get-CurrentRuntimeDir',
        'if (-not $runtimeDir) { throw "No installed EcoreX WebUI runtime was found. Run the installer once before using this shortcut." }',
        '$python = Join-Path $runtimeDir "python\python.exe"',
        'if (-not (Test-Path -LiteralPath $python)) { throw "Packaged Python runtime is missing: $python" }',
        '$port = Get-WebUiPort -RuntimeDir $runtimeDir',
        '$runtimeLeaf = Split-Path -Leaf $runtimeDir',
        '$cacheBust = [Uri]::EscapeDataString($runtimeLeaf)',
        '$url = "http://127.0.0.1:$port/app/?runtime=$cacheBust"',
        '[System.IO.File]::WriteAllText((Join-Path $StateDir "ecorex-webui.url"), $url + [Environment]::NewLine, [System.Text.UTF8Encoding]::new($false))',
        'if (-not (Test-WebUiReady -Url $url)) {',
        '    Start-Process -FilePath $python -ArgumentList "app.py" -WorkingDirectory $runtimeDir -WindowStyle Hidden -RedirectStandardOutput $LogPath -RedirectStandardError $ErrorLogPath',
        '}',
        'Wait-WebUiReady -Url $url',
        'if (-not $NoBrowser) { Start-Process $url }'
    )
    [System.IO.File]::WriteAllText($launcherPath, (($launcherLines -join [Environment]::NewLine) + [Environment]::NewLine), [System.Text.UTF8Encoding]::new($false))
    return $launcherPath
}

function Write-WebUiShortcuts {
    param(
        [Parameter(Mandatory = $true)][string]$Url,
        [Parameter(Mandatory = $true)][string]$LauncherPath
    )
    $written = @()
    foreach ($path in Get-DesktopShortcutPaths) {
        try {
            $dir = Split-Path -Parent $path
            if (-not (Test-Path -LiteralPath $dir)) {
                New-Item -ItemType Directory -Force -Path $dir | Out-Null
            }
            $powershellExe = Join-Path $PSHOME "powershell.exe"
            if (-not (Test-Path -LiteralPath $powershellExe)) {
                $powershellExe = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
            }
            $shell = New-Object -ComObject WScript.Shell
            $shortcut = $shell.CreateShortcut($path)
            $shortcut.TargetPath = $powershellExe
            $shortcut.Arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$LauncherPath`""
            $shortcut.WorkingDirectory = Split-Path -Parent $LauncherPath
            $shortcut.WindowStyle = 7
            $shortcut.Description = "Start or reopen EcoreX WebUI"
            $shortcut.Save()
            $written += $path
        } catch {
            $cmdPath = [System.IO.Path]::ChangeExtension($path, ".cmd")
            try {
                $cmdBody = "@echo off`r`n`"$powershellExe`" -NoProfile -ExecutionPolicy Bypass -File `"$LauncherPath`"`r`n"
                [System.IO.File]::WriteAllText($cmdPath, $cmdBody, [System.Text.Encoding]::ASCII)
                $written += $cmdPath
                Write-Warning "Created fallback desktop launcher at ${cmdPath}: $($_.Exception.Message)"
            } catch {
                Write-Warning "Could not create desktop shortcut at ${path}: $($_.Exception.Message)"
            }
        }
        try {
            $legacyUrl = [System.IO.Path]::ChangeExtension($path, ".url")
            Remove-Item -LiteralPath $legacyUrl -Force -ErrorAction SilentlyContinue
        } catch {
        }
    }
    if (-not $written.Count) {
        throw "Could not create EcoreX WebUI desktop shortcut."
    }
    return $written
}

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$packageRoot = Split-Path -Parent $scriptDir
$sourceRuntime = Join-Path $packageRoot "runtime"
$installRoot = Join-Path $env:LOCALAPPDATA "EcoreX WebUI"
$stateDir = Join-Path $installRoot "state"
$workspaceRoot = Join-Path $env:USERPROFILE "EcoreX"
$logPath = Join-Path $stateDir "ecorex-webui.log"
$errorLogPath = Join-Path $stateDir "ecorex-webui.err.log"
$runtimeSlot = "runtime-__ECOREX_VERSION__-" + [Guid]::NewGuid().ToString("N").Substring(0, 8)
$runtimeDir = Join-Path $installRoot $runtimeSlot
$currentRuntimePath = Join-Path $stateDir "current-runtime.txt"
$python = Join-Path $runtimeDir "python\python.exe"

New-Item -ItemType Directory -Force -Path $installRoot, $stateDir, $workspaceRoot | Out-Null
$script:PreUpdateExternalConnections = Get-ExternalConnectionSnapshot -StateDir $stateDir -Reason "pre_update"
Write-UpdateState -StateDir $stateDir -Status "available" -Reason "installer_started"
if ($backgroundUpdate) {
    $existing = @(Get-WebUiPythonProcesses -RuntimeDir $runtimeDir -InstallRoot $installRoot)
    $activeCount = Get-ActiveRequestCount -StateDir $stateDir
    if ($activeCount -gt 0) {
        Write-UpdateState -StateDir $stateDir -Status "deferred" -Reason "active_requests"
        Write-Host "Background update deferred because EcoreX WebUI has $activeCount active request(s)."
        exit 75
    }
    if ($activeCount -lt 0 -and $existing.Count -gt 0) {
        Write-UpdateState -StateDir $stateDir -Status "deferred" -Reason "active_requests_unavailable"
        Write-Host "Background update deferred because active request state is unavailable while the local service is running."
        exit 75
    }
}
Write-UpdateState -StateDir $stateDir -Status "downloading" -Reason "package_local_staging"
Stop-ExistingWebUi -RuntimeDir $runtimeDir -InstallRoot $installRoot
if ($backgroundUpdate -and -not (Wait-PortAvailable -Port $Port -TimeoutSeconds 30)) {
    Write-UpdateState -StateDir $stateDir -Status "failed" -Reason "preferred_port_busy_after_stop"
    throw "Preferred EcoreX WebUI port $Port is still busy after stopping the previous runtime."
}

Write-Host "Copying EcoreX WebUI runtime to $runtimeDir..."
robocopy $sourceRuntime $runtimeDir /MIR /R:2 /W:1 /XD __pycache__ .pytest_cache .mypy_cache .ruff_cache /XF *.pyc *.pyo config.json | Out-Null
if ($LASTEXITCODE -gt 7) {
    Remove-Item -LiteralPath $runtimeDir -Recurse -Force -ErrorAction SilentlyContinue
    throw "Failed to copy runtime to $runtimeDir; robocopy exit code $LASTEXITCODE"
}
$global:LASTEXITCODE = 0
Write-UpdateState -StateDir $stateDir -Status "verified" -Reason "runtime_copied"

$effectivePort = Get-FreePort -Preferred $Port
$config = [ordered]@{
    cow_lang = "auto"
    channel_type = "web"
    web_console = $true
    web_host = "127.0.0.1"
    web_port = $effectivePort
    web_password = ""
    web_auto_open = (-not $NoBrowser -and -not $backgroundUpdate)
    agent = $true
    self_evolution_enabled = $true
    scheduler_enabled = $false
    mcp_auto_start = $false
    agent_workspace = $workspaceRoot
    web_file_serve_root = $workspaceRoot
    appdata_dir = (Join-Path $stateDir "appdata")
    use_linkai = $false
    debug = $false
    tools = [ordered]@{
        browser = [ordered]@{
            cdp_endpoint = "http://127.0.0.1:9222"
            cdp_auto_launch = $true
            cdp_fallback = $true
            persistent = $true
        }
        feishu_cli = [ordered]@{
            package = "@larksuite/cli@1.0.56"
            auto_install = $false
            allow_system_node = $false
            install_root = (Join-Path $stateDir "tools\lark-cli")
        }
        tongxin_cli = [ordered]@{
            script_path = ""
            python_path = $python
            database_path = (Join-Path $stateDir "tongxin.sqlite3")
            read_only = $true
            auth_url = "https://mvdcm.ecoremedia.net/ecorex-agent/client/tongxin/auth"
            bootstrap_manifest_url = ""
            bootstrap_url = ""
            bootstrap_sha256 = ""
            bootstrap_token = ""
            bootstrap_dir = (Join-Path $stateDir "tools\tongxin")
        }
    }
    mcp_servers = @(
        [ordered]@{
            name = "chrome-devtools"
            type = "stdio"
            command = "npx.cmd"
            args = @("chrome-devtools-mcp@latest", "--browserUrl", "http://127.0.0.1:9222", "--no-usage-statistics")
            timeout = 30
        }
    )
}
$configJson = $config | ConvertTo-Json -Depth 8
$utf8NoBom = [System.Text.UTF8Encoding]::new($false)
[System.IO.File]::WriteAllText((Join-Path $runtimeDir "config.json"), $configJson + [Environment]::NewLine, $utf8NoBom)

Ensure-LarkCli -RuntimeDir $runtimeDir -StateDir $stateDir

if (-not (Test-Path -LiteralPath $python)) {
    throw "Packaged Python runtime is missing: $python"
}
if (-not (Test-PythonModule -Python $python -ModuleName "lark_oapi")) {
    Write-Warning "Bundled lark_oapi is unavailable; Feishu/Lark External Connections will show runtime remediation, but WebUI startup will continue."
    Write-OptionalPythonDependencyNotice -StateDir $stateDir -ModuleName "lark_oapi" -PackageSpec "lark-oapi>=1.5.5" -Reason "Feishu/Lark websocket external connection"
}

$url = "http://127.0.0.1:$effectivePort/app/"
[System.IO.File]::WriteAllText((Join-Path $stateDir "ecorex-webui.url"), $url + [Environment]::NewLine, $utf8NoBom)
Write-UpdateState -StateDir $stateDir -Status "staged" -Reason "runtime_ready_to_start" -Url $url
Write-Host "Starting EcoreX WebUI local service: $url"
$oldWebNoBrowser = $env:ECOREX_WEB_NO_BROWSER
if ($NoBrowser -or $backgroundUpdate) {
    $env:ECOREX_WEB_NO_BROWSER = "1"
}
try {
    Start-Process -FilePath $python `
        -ArgumentList "app.py" `
        -WorkingDirectory $runtimeDir `
        -WindowStyle Hidden `
        -RedirectStandardOutput $logPath `
        -RedirectStandardError $errorLogPath
} finally {
    if ($null -eq $oldWebNoBrowser) {
        Remove-Item Env:\ECOREX_WEB_NO_BROWSER -ErrorAction SilentlyContinue
    } else {
        $env:ECOREX_WEB_NO_BROWSER = $oldWebNoBrowser
    }
}

Write-Host "Waiting for EcoreX WebUI to become ready..."
try {
    Wait-WebUi -Url $url
} catch {
    Write-UpdateState -StateDir $stateDir -Status "failed" -Reason "runtime_health_check_failed" -Url $url
    Stop-ExistingWebUi -RuntimeDir $runtimeDir -InstallRoot $installRoot
    throw
}
$script:PostUpdateExternalConnections = Get-ExternalConnectionSnapshot -StateDir $stateDir -BaseUrl $url -Reason "post_update"
$script:ExternalConnectionMissingIds = Compare-ExternalConnectionSnapshots -Before $script:PreUpdateExternalConnections -After $script:PostUpdateExternalConnections
if (@($script:ExternalConnectionMissingIds).Count -gt 0) {
    Write-UpdateState -StateDir $stateDir -Status "rollback" -Reason "external_connections_missing_after_update" -Url $url
    throw "External connection health check failed after update. Missing: $($script:ExternalConnectionMissingIds -join ', ')"
}
[System.IO.File]::WriteAllText($currentRuntimePath, $runtimeDir + [Environment]::NewLine, $utf8NoBom)
Remove-OldRuntimeDirs -InstallRoot $installRoot -CurrentRuntimeDir $runtimeDir

$launcherPath = Write-WebUiLauncher -InstallRoot $installRoot
$shortcuts = Write-WebUiShortcuts -Url $url -LauncherPath $launcherPath
foreach ($shortcut in $shortcuts) {
    Write-Host "Desktop shortcut updated: $shortcut"
}

if ((-not $NoBrowser) -and (-not $backgroundUpdate)) {
    Start-Process $url
}

$finalStatus = if ($backgroundUpdate) { "installed" } else { "activated" }
Write-UpdateState -StateDir $stateDir -Status $finalStatus -Url $url
Write-Host "EcoreX WebUI is ready: $url"
if ($backgroundUpdate) {
    Write-Host "Background update installed. Existing browser tabs should soft-refresh after they observe the new runtime version."
} else {
    Write-Host "If the browser did not open, double-click a desktop EcoreX WebUI shortcut above or open $url manually."
}
exit 0
'@

New-Item -ItemType Directory -Force -Path (Join-Path $windowsStage "scripts") | Out-Null
$windowsPs1 = $windowsPs1.Replace('__ECOREX_VERSION__', $Version)
Write-Utf8NoBom -Path (Join-Path $windowsStage "Install EcoreX WebUI.cmd") -Value $windowsCmd
Write-Utf8NoBom -Path (Join-Path $windowsStage "scripts/install-ecorex-webui-win.ps1") -Value $windowsPs1
Write-Utf8NoBom -Path (Join-Path $windowsStage "release.json") -Value (New-ReleaseJson -ArtifactId "webui-windows-x64" -Platform "Windows x64" -InstallEntry "Install EcoreX WebUI.cmd")
$windowsReadme = "Double-click Install EcoreX WebUI.cmd. The installer copies EcoreX WebUI to your local app data, starts the local service, and opens http://127.0.0.1:9909/app/ in your browser.`n"
Write-Utf8NoBom -Path (Join-Path $windowsStage "README.txt") -Value $windowsReadme

Write-Host "Packaging Windows WebUI zip: $windowsZip"
Compress-ZipWithUnixPermissions `
    -SourceRoot $windowsStage `
    -DestinationPath $windowsZip `
    -ExcludeRootDirectory `
    -ExecutableRelativePaths @("Install EcoreX WebUI.cmd")

$macRuntime = Join-Path $macStage "runtime"
Copy-Item -LiteralPath $runtimeRootResolved -Destination $macRuntime -Recurse -Force
Sync-CurrentRuntimeSources -RuntimeDir $macRuntime
$macLocalCoreRequirements = New-LocalMacCoreRequirements -Destination (Join-Path $cacheRoot "core-requirements-local-macos.txt")
Copy-Item -LiteralPath $macLocalCoreRequirements -Destination (Join-Path $macRuntime "core-requirements.txt") -Force
Remove-Item -LiteralPath (Join-Path $macRuntime "python") -Recurse -Force -ErrorAction SilentlyContinue
Remove-GeneratedNoise -Root $macRuntime
Sync-DesktopWebBuild -RuntimeDir $macRuntime
Copy-OptionalLarkCliMac -RuntimeDir $macRuntime
Invoke-ReleaseRuntimeSanitizer -RuntimeDir $macRuntime

$pyArmUrl = "https://github.com/astral-sh/python-build-standalone/releases/download/20260602/cpython-3.11.15%2B20260602-aarch64-apple-darwin-install_only_stripped.tar.gz"
$pyX64Url = "https://github.com/astral-sh/python-build-standalone/releases/download/20260602/cpython-3.11.15%2B20260602-x86_64-apple-darwin-install_only_stripped.tar.gz"
$pyArm = Join-Path $cacheRoot "cpython-3.11.15+20260602-aarch64-apple-darwin-install_only_stripped.tar.gz"
$pyX64 = Join-Path $cacheRoot "cpython-3.11.15+20260602-x86_64-apple-darwin-install_only_stripped.tar.gz"
Save-Download -Uri $pyArmUrl -Destination $pyArm
Save-Download -Uri $pyX64Url -Destination $pyX64

$wheelArm = Join-Path $cacheRoot "wheelhouse/mac-arm64"
$wheelX64 = Join-Path $cacheRoot "wheelhouse/mac-x64"
Invoke-PipDownload -Platform "macosx_11_0_arm64" -Destination $wheelArm -RequirementsPath $macLocalCoreRequirements
Invoke-PipDownload -Platform "macosx_11_0_x86_64" -Destination $wheelX64 -RequirementsPath $macLocalCoreRequirements

New-Item -ItemType Directory -Force -Path (Join-Path $macStage "python"), (Join-Path $macStage "wheelhouse") | Out-Null
Copy-Item -LiteralPath $pyArm -Destination (Join-Path $macStage "python/$(Split-Path -Leaf $pyArm)") -Force
Copy-Item -LiteralPath $pyX64 -Destination (Join-Path $macStage "python/$(Split-Path -Leaf $pyX64)") -Force
Copy-Item -LiteralPath $wheelArm -Destination (Join-Path $macStage "wheelhouse/mac-arm64") -Recurse -Force
Copy-Item -LiteralPath $wheelX64 -Destination (Join-Path $macStage "wheelhouse/mac-x64") -Recurse -Force
Write-V025RuntimeManifest -RuntimeDir $macRuntime -PackageRoot $macStage -Platform "macos-universal"

$macInstall = @'
#!/usr/bin/env bash
set -euo pipefail

VERSION="__ECOREX_VERSION__"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PACKAGE_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
INSTALL_ROOT="${ECOREX_WEBUI_INSTALL_ROOT:-$HOME/Library/Application Support/EcoreX WebUI}"
STATE_DIR="$INSTALL_ROOT/state"
CURRENT_RUNTIME_PATH="$STATE_DIR/current-runtime.txt"
PREVIOUS_RUNTIME_DIR=""
if [[ -f "$CURRENT_RUNTIME_PATH" ]]; then
  PREVIOUS_RUNTIME_DIR="$(cat "$CURRENT_RUNTIME_PATH" 2>/dev/null || true)"
  if [[ -n "$PREVIOUS_RUNTIME_DIR" && ! -d "$PREVIOUS_RUNTIME_DIR" ]]; then
    PREVIOUS_RUNTIME_DIR=""
  fi
fi
RUNTIME_SLOT="runtime-$VERSION-$(date -u +%Y%m%d%H%M%S)-$$"
RUNTIME_DIR="$INSTALL_ROOT/$RUNTIME_SLOT"
LAUNCHER_PATH="$INSTALL_ROOT/Launch EcoreX WebUI.command"
WORKSPACE_ROOT="${ECOREX_WORKSPACE_ROOT:-$HOME/EcoreX}"
PORT="${ECOREX_WEB_PORT:-9909}"
OPEN_BROWSER="${OPEN_BROWSER:-1}"
UPDATE_MODE="${ECOREX_UPDATE_MODE:-manual}"
PYTHON_HOME="$INSTALL_ROOT/python"
BACKGROUND_UPDATE=0
if [[ "$UPDATE_MODE" == "background" ]]; then
  BACKGROUND_UPDATE=1
  OPEN_BROWSER=0
fi

echo "EcoreX WebUI package installer: $VERSION"

clear_quarantine() {
  if command -v xattr >/dev/null 2>&1 && [[ -e "$1" ]]; then
    xattr -dr com.apple.quarantine "$1" >/dev/null 2>&1 || true
  fi
}

wait_for_pid_exit() {
  pid="$1"
  for _ in 1 2 3 4 5; do
    if ! kill -0 "$pid" >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  return 1
}

PRE_UPDATE_EXTERNAL_CONNECTIONS_JSON='{"status":"not_checked","configuredIds":[],"connectedIds":[],"callableIds":[],"reason":"not_started","redacted":true}'
POST_UPDATE_EXTERNAL_CONNECTIONS_JSON='{"status":"not_checked","configuredIds":[],"connectedIds":[],"callableIds":[],"reason":"not_started","redacted":true}'
EXTERNAL_CONNECTION_MISSING_IDS_JSON='[]'

write_update_state() {
  status="$1"
  reason="${2:-}"
  url="${3:-}"
  browser_action="open-default-browser"
  if [[ "$BACKGROUND_UPDATE" == "1" ]]; then
    browser_action="defer-to-existing-tab-soft-refresh"
  elif [[ "$OPEN_BROWSER" != "1" ]]; then
    browser_action="none"
  fi
  mkdir -p "$STATE_DIR"
  PRE_UPDATE_EXTERNAL_CONNECTIONS_JSON="$PRE_UPDATE_EXTERNAL_CONNECTIONS_JSON" POST_UPDATE_EXTERNAL_CONNECTIONS_JSON="$POST_UPDATE_EXTERNAL_CONNECTIONS_JSON" EXTERNAL_CONNECTION_MISSING_IDS_JSON="$EXTERNAL_CONNECTION_MISSING_IDS_JSON" "$PYTHON_HOME/bin/python3" - "$STATE_DIR/update-state.json" "$VERSION" "$UPDATE_MODE" "$status" "$reason" "$url" "$browser_action" <<'PY' || true
import json
import os
import pathlib
import sys
from datetime import datetime, timezone

target = pathlib.Path(sys.argv[1])
def env_json(name, fallback):
    try:
        return json.loads(os.environ.get(name, "") or fallback)
    except Exception:
        return json.loads(fallback)

missing_ids = env_json("EXTERNAL_CONNECTION_MISSING_IDS_JSON", "[]")
pre_update = env_json("PRE_UPDATE_EXTERNAL_CONNECTIONS_JSON", '{"status":"not_checked","configuredIds":[],"connectedIds":[],"callableIds":[],"redacted":true}')
post_update = env_json("POST_UPDATE_EXTERNAL_CONNECTIONS_JSON", '{"status":"not_checked","configuredIds":[],"connectedIds":[],"callableIds":[],"redacted":true}')
terminal_pass = sys.argv[4] in ("installed", "activated")
terminal_fail = sys.argv[4] in ("failed", "rollback")
if missing_ids or terminal_fail:
    external_status = "failed"
elif terminal_pass and pre_update.get("status") == "pass":
    external_status = "pass"
elif terminal_pass:
    external_status = "not_applicable"
else:
    external_status = "pending"
payload = {
    "product": "EcoreX WebUI",
    "version": sys.argv[2],
    "mode": sys.argv[3],
    "status": sys.argv[4],
    "reason": sys.argv[5],
    "url": sys.argv[6],
    "browserAction": sys.argv[7],
    "activationPolicy": "prompt-soft-refresh-existing-tab" if sys.argv[3] == "background" else "manual-open-browser",
    "autoLaunchBrowser": "never-in-background" if sys.argv[3] == "background" else ("disabled-by-user" if sys.argv[7] == "none" else "manual-install-only"),
    "healthCheck": {
        "endpoint": "/api/version",
        "status": "pass" if sys.argv[4] in ("installed", "activated") else ("failed" if sys.argv[4] in ("failed", "rollback") else "pending"),
        "passed": sys.argv[4] in ("installed", "activated"),
    },
    "externalConnections": {
        "required": True,
        "status": external_status,
        "passed": external_status in ("pass", "not_applicable"),
        "policy": "preserve configured and connected external tools across online update",
        "preservedRoots": ["workspace mcp.json", "state appdata", "state tools"],
        "before": pre_update,
        "after": post_update,
        "missingIds": missing_ids,
        "redacted": True,
    },
    "generatedAt": datetime.now(timezone.utc).isoformat(),
}
target.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
PY
}

write_launch_script() {
  mkdir -p "$INSTALL_ROOT" "$STATE_DIR"
  cat > "$LAUNCHER_PATH" <<'LAUNCHER'
#!/usr/bin/env bash
set -euo pipefail

INSTALL_ROOT="${ECOREX_WEBUI_INSTALL_ROOT:-$HOME/Library/Application Support/EcoreX WebUI}"
STATE_DIR="$INSTALL_ROOT/state"
PYTHON_HOME="$INSTALL_ROOT/python"
CURRENT_RUNTIME_PATH="$STATE_DIR/current-runtime.txt"

current_runtime_dir() {
  if [[ -f "$CURRENT_RUNTIME_PATH" ]]; then
    candidate="$(cat "$CURRENT_RUNTIME_PATH" 2>/dev/null || true)"
    if [[ -n "$candidate" && -d "$candidate" ]]; then
      printf '%s\n' "$candidate"
      return 0
    fi
  fi
  latest="$(find "$INSTALL_ROOT" -maxdepth 1 -type d -name 'runtime-*' -print 2>/dev/null | sort | tail -n 1 || true)"
  if [[ -n "$latest" ]]; then
    printf '%s\n' "$latest"
    return 0
  fi
  legacy="$INSTALL_ROOT/runtime"
  if [[ -d "$legacy" ]]; then
    printf '%s\n' "$legacy"
    return 0
  fi
  return 1
}

mkdir -p "$STATE_DIR"
if [[ -x "$PYTHON_HOME/bin/python3" ]]; then
  PYTHON="$PYTHON_HOME/bin/python3"
elif [[ -x "$PYTHON_HOME/bin/python" ]]; then
  PYTHON="$PYTHON_HOME/bin/python"
else
  echo "Packaged Python runtime is missing under $PYTHON_HOME." >&2
  exit 1
fi
RUNTIME_DIR="$(current_runtime_dir || true)"
if [[ -z "$RUNTIME_DIR" ]]; then
  echo "No installed EcoreX WebUI runtime was found. Run the installer once before using this shortcut." >&2
  exit 1
fi

PORT="$("$PYTHON" - "$RUNTIME_DIR/config.json" <<'PY' 2>/dev/null || echo 9909
import json
import os
import pathlib
import sys

try:
    payload = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
    print(int(payload.get("web_port") or 9909))
except Exception:
    print(9909)
PY
)"
URL="http://127.0.0.1:$PORT/app/"
echo "$URL" > "$STATE_DIR/ecorex-webui.url"

is_ready() {
  curl -fsS --connect-timeout 2 --max-time 2 "$URL" >/dev/null 2>&1
}

if ! is_ready; then
  (
    cd "$RUNTIME_DIR"
    PYTHONPATH="$RUNTIME_DIR:${PYTHONPATH:-}" nohup "$PYTHON" "$RUNTIME_DIR/app.py" > "$STATE_DIR/ecorex-webui.reopen.log" 2> "$STATE_DIR/ecorex-webui.reopen.err.log" &
    echo $! > "$STATE_DIR/ecorex-webui.pid"
  )
fi

for _ in $(seq 1 60); do
  if is_ready; then
    if command -v open >/dev/null 2>&1; then
      open "$URL" >/dev/null 2>&1 || true
    else
      echo "Open this URL manually: $URL"
    fi
    exit 0
  fi
  sleep 1
done

echo "EcoreX WebUI did not become ready at $URL" >&2
exit 1
LAUNCHER
  chmod +x "$LAUNCHER_PATH" || true
}

webui_base_url() {
  url_file="$STATE_DIR/ecorex-webui.url"
  [[ -f "$url_file" ]] || return 0
  raw_url="$(cat "$url_file" 2>/dev/null || true)"
  [[ -n "$raw_url" ]] || return 0
  python3 - "$raw_url" <<'PY' 2>/dev/null || true
import sys
from urllib.parse import urlparse

parsed = urlparse(sys.argv[1])
if parsed.scheme and parsed.netloc:
    print(f"{parsed.scheme}://{parsed.netloc}")
PY
}

active_request_count() {
  base_url="$(webui_base_url)"
  if [[ -z "$base_url" ]]; then
    echo 0
    return 0
  fi
  if ! payload="$(curl -fsSL --connect-timeout 2 --max-time 3 "$base_url/api/active-requests" 2>/dev/null)"; then
    echo -1
    return 0
  fi
  python3 - "$payload" <<'PY' 2>/dev/null || echo -1
import json
import sys

try:
    payload = json.loads(sys.argv[1])
    print(len(payload.get("requests") or []))
except Exception:
    print(-1)
PY
}

external_connection_snapshot() {
  base_url="${1:-}"
  reason="${2:-probe}"
  if [[ -z "$base_url" ]]; then
    base_url="$(webui_base_url)"
  fi
  if [[ -z "$base_url" ]]; then
    printf '{"status":"unavailable","configuredIds":[],"connectedIds":[],"callableIds":[],"reason":"base_url_missing","redacted":true}\n'
    return 0
  fi
  connections_payload="$(curl -fsSL --connect-timeout 3 --max-time 8 "$base_url/api/external-connections" 2>/dev/null || true)"
  tencent_docs_payload="$(curl -fsSL --connect-timeout 3 --max-time 12 "$base_url/api/tencent-docs/status?start=1" 2>/dev/null || true)"
  if [[ -z "$connections_payload" ]]; then
    printf '{"status":"unavailable","configuredIds":[],"connectedIds":[],"callableIds":[],"reason":"probe_failed","redacted":true}\n'
    return 0
  fi
  probe_python="${PYTHON:-}"
  if [[ -z "$probe_python" || ! -x "$probe_python" ]]; then
    if [[ -x "$PYTHON_HOME/bin/python3" ]]; then
      probe_python="$PYTHON_HOME/bin/python3"
    else
      probe_python="$(command -v python3 || true)"
    fi
  fi
  if [[ -z "$probe_python" ]]; then
    printf '{"status":"unavailable","configuredIds":[],"connectedIds":[],"callableIds":[],"reason":"python_missing","redacted":true}\n'
    return 0
  fi
  "$probe_python" - "$reason" "$connections_payload" "$tencent_docs_payload" <<'PY' 2>/dev/null || printf '{"status":"unavailable","configuredIds":[],"connectedIds":[],"callableIds":[],"reason":"parse_failed","redacted":true}\n'
import json
import sys
from datetime import datetime, timezone

reason = sys.argv[1]
connections_payload = json.loads(sys.argv[2])
configured = set()
connected = set()
callable_ids = set()
for item in connections_payload.get("connections") or []:
    if not isinstance(item, dict):
        continue
    cid = str(item.get("id") or "")
    if not cid:
        continue
    if item.get("configured"):
        configured.add(cid)
    if item.get("connected"):
        connected.add(cid)
    if item.get("callable"):
        callable_ids.add(cid)
try:
    docs = json.loads(sys.argv[3]) if sys.argv[3] else {}
    cap = docs.get("capability") if isinstance(docs, dict) else {}
    if isinstance(cap, dict) and cap.get("configured"):
        configured.add("tencent-docs")
        if cap.get("connected") or int(cap.get("toolCount") or 0) > 0:
            connected.add("tencent-docs")
            callable_ids.add("tencent-docs")
except Exception:
    pass
print(json.dumps({
    "status": "pass",
    "reason": reason,
    "configuredIds": sorted(configured),
    "connectedIds": sorted(connected),
    "callableIds": sorted(callable_ids),
    "checkedAt": datetime.now(timezone.utc).isoformat(),
    "redacted": True,
}, ensure_ascii=False))
PY
}

compare_external_connection_snapshots() {
  before_json="$1"
  after_json="$2"
  probe_python="${PYTHON:-}"
  if [[ -z "$probe_python" || ! -x "$probe_python" ]]; then
    if [[ -x "$PYTHON_HOME/bin/python3" ]]; then
      probe_python="$PYTHON_HOME/bin/python3"
    else
      probe_python="$(command -v python3 || true)"
    fi
  fi
  if [[ -z "$probe_python" ]]; then
    printf '[]\n'
    return 0
  fi
  "$probe_python" - "$before_json" "$after_json" <<'PY' 2>/dev/null || printf '[]\n'
import json
import sys

before = json.loads(sys.argv[1] or "{}")
after = json.loads(sys.argv[2] or "{}")
if before.get("status") != "pass":
    print("[]")
    raise SystemExit(0)
required = set(before.get("configuredIds") or []) | set(before.get("connectedIds") or []) | set(before.get("callableIds") or [])
if after.get("status") != "pass":
    print(json.dumps(sorted(required), ensure_ascii=False))
    raise SystemExit(0)
available = set(after.get("configuredIds") or []) | set(after.get("connectedIds") or []) | set(after.get("callableIds") or [])
print(json.dumps(sorted(required - available), ensure_ascii=False))
PY
}

command_for_pid() {
  ps -p "$1" -o command= 2>/dev/null || true
}

pid_matches_ecorex_webui() {
  pid="$1"
  cmd="$(command_for_pid "$pid")"
  [[ -n "$cmd" ]] || return 1
  if [[ "$cmd" != *"$RUNTIME_DIR/app.py"* ]]; then
    if [[ -z "${PREVIOUS_RUNTIME_DIR:-}" || "$cmd" != *"$PREVIOUS_RUNTIME_DIR/app.py"* ]]; then
      return 1
    fi
  fi
  case "$cmd" in
    *"$PYTHON_HOME/bin/python3"*|*"$PYTHON_HOME/bin/python"*|*"python"*"app.py"*)
      return 0
      ;;
  esac
  return 1
}

append_ecorex_webui_pid() {
  pid="$1"
  [[ -n "$pid" ]] || return 0
  if kill -0 "$pid" >/dev/null 2>&1 && pid_matches_ecorex_webui "$pid"; then
    pids="$pids $pid"
  fi
}

stop_existing_webui() {
  pids=""
  if [[ -f "$STATE_DIR/ecorex-webui.pid" ]]; then
    old_pid="$(cat "$STATE_DIR/ecorex-webui.pid" 2>/dev/null || true)"
    append_ecorex_webui_pid "$old_pid"
  fi

  runtime_pids="$(pgrep -f -- "$RUNTIME_DIR/app.py" 2>/dev/null || true)"
  for runtime_pid in $runtime_pids; do
    append_ecorex_webui_pid "$runtime_pid"
  done
  python_app_pids="$(pgrep -f -- "$PYTHON_HOME/bin/python.*$RUNTIME_DIR/app.py" 2>/dev/null || true)"
  for python_app_pid in $python_app_pids; do
    append_ecorex_webui_pid "$python_app_pid"
  done
  if [[ -n "${PREVIOUS_RUNTIME_DIR:-}" ]]; then
    previous_runtime_pids="$(pgrep -f -- "$PREVIOUS_RUNTIME_DIR/app.py" 2>/dev/null || true)"
    for previous_runtime_pid in $previous_runtime_pids; do
      append_ecorex_webui_pid "$previous_runtime_pid"
    done
    previous_python_pids="$(pgrep -f -- "$PYTHON_HOME/bin/python.*$PREVIOUS_RUNTIME_DIR/app.py" 2>/dev/null || true)"
    for previous_python_pid in $previous_python_pids; do
      append_ecorex_webui_pid "$previous_python_pid"
    done
  fi
  if command -v lsof >/dev/null 2>&1; then
    for port_pid in $(lsof -tiTCP:"$PORT" -sTCP:LISTEN 2>/dev/null || true); do
      append_ecorex_webui_pid "$port_pid"
    done
  fi

  pids="$(printf '%s\n' $pids | awk 'NF && !seen[$1]++ { print $1 }' || true)"
  if [[ -z "$pids" ]]; then
    rm -f "$STATE_DIR/ecorex-webui.pid"
    return 0
  fi

  echo "Stopping existing EcoreX WebUI local service..."
  for pid in $pids; do
    kill "$pid" >/dev/null 2>&1 || true
  done
  for pid in $pids; do
    if ! wait_for_pid_exit "$pid"; then
      kill -9 "$pid" >/dev/null 2>&1 || true
      wait_for_pid_exit "$pid" || true
    fi
  done
  rm -f "$STATE_DIR/ecorex-webui.pid"
}

ROLLBACK_URL=""

restart_previous_runtime() {
  reason="${1:-rollback}"
  ROLLBACK_URL=""
  if [[ -z "${PREVIOUS_RUNTIME_DIR:-}" || ! -d "$PREVIOUS_RUNTIME_DIR" ]]; then
    echo "No previous EcoreX WebUI runtime is available for rollback ($reason)." >&2
    return 1
  fi
  if [[ ! -x "${PYTHON:-}" ]]; then
    PYTHON="$PYTHON_HOME/bin/python3"
  fi
  if [[ ! -x "$PYTHON" ]]; then
    echo "Packaged Python runtime is missing for rollback: $PYTHON" >&2
    return 1
  fi
  previous_port="$("$PYTHON" - "$PREVIOUS_RUNTIME_DIR/config.json" <<'PY' 2>/dev/null || echo 9909
import json
import pathlib
import sys
try:
    print(int(json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8")).get("web_port") or 9909))
except Exception:
    print(9909)
PY
)"
  ROLLBACK_URL="http://127.0.0.1:$previous_port/app/"
  echo "$ROLLBACK_URL" > "$STATE_DIR/ecorex-webui.url"
  echo "$PREVIOUS_RUNTIME_DIR" > "$CURRENT_RUNTIME_PATH"
  (
    cd "$PREVIOUS_RUNTIME_DIR"
    export ECOREX_WEB_NO_BROWSER=1
    PYTHONPATH="$PREVIOUS_RUNTIME_DIR:${PYTHONPATH:-}" nohup "$PYTHON" "$PREVIOUS_RUNTIME_DIR/app.py" > "$STATE_DIR/ecorex-webui.rollback.log" 2> "$STATE_DIR/ecorex-webui.rollback.err.log" &
    echo $! > "$STATE_DIR/ecorex-webui.pid"
  )
  for _ in $(seq 1 30); do
    if curl -fsS --connect-timeout 2 --max-time 2 "$ROLLBACK_URL" >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  echo "Previous EcoreX WebUI runtime did not become ready at $ROLLBACK_URL" >&2
  return 1
}

add_unique_dir() {
  candidate="$1"
  [[ -n "$candidate" ]] || return 0
  candidate="${candidate%/}"
  for existing in "${DESKTOP_CANDIDATES[@]:-}"; do
    [[ "$existing" == "$candidate" ]] && return 0
  done
  DESKTOP_CANDIDATES+=("$candidate")
}

desktop_shortcut_dirs() {
  DESKTOP_CANDIDATES=()
  if command -v osascript >/dev/null 2>&1; then
    finder_desktop="$(osascript -e 'POSIX path of (path to desktop folder)' 2>/dev/null || true)"
    add_unique_dir "$finder_desktop"
  fi
  add_unique_dir "$HOME/Desktop"
  printf '%s\n' "${DESKTOP_CANDIDATES[@]}"
}

write_desktop_shortcuts() {
  url="$1"
  written=0
  while IFS= read -r desktop_dir; do
    [[ -n "$desktop_dir" ]] || continue
    if [[ ! -d "$desktop_dir" ]]; then
      mkdir -p "$desktop_dir" 2>/dev/null || true
    fi
    [[ -d "$desktop_dir" ]] || continue
    shortcut_path="$desktop_dir/EcoreX WebUI.command"
    quoted_launcher="$(printf '%q' "$LAUNCHER_PATH")"
    {
      echo '#!/usr/bin/env bash'
      printf 'exec %s "$@"\n' "$quoted_launcher"
    } > "$shortcut_path"
    chmod +x "$shortcut_path" || true
    rm -f "$desktop_dir/EcoreX WebUI.webloc" 2>/dev/null || true
    echo "Desktop shortcut updated: $shortcut_path"
    written=1
  done < <(desktop_shortcut_dirs)

  if [[ "$written" != "1" ]]; then
    echo "Could not create EcoreX WebUI desktop shortcut." >&2
    return 1
  fi
}

case "$(uname -m)" in
  arm64|aarch64)
    PY_ARCHIVE="$PACKAGE_ROOT/python/cpython-3.11.15+20260602-aarch64-apple-darwin-install_only_stripped.tar.gz"
    WHEEL_DIR="$PACKAGE_ROOT/wheelhouse/mac-arm64"
    ;;
  x86_64|amd64)
    PY_ARCHIVE="$PACKAGE_ROOT/python/cpython-3.11.15+20260602-x86_64-apple-darwin-install_only_stripped.tar.gz"
    WHEEL_DIR="$PACKAGE_ROOT/wheelhouse/mac-x64"
    ;;
  *)
    echo "Unsupported macOS architecture: $(uname -m)" >&2
    exit 1
    ;;
esac

mkdir -p "$INSTALL_ROOT" "$STATE_DIR" "$WORKSPACE_ROOT"
PRE_UPDATE_EXTERNAL_CONNECTIONS_JSON="$(external_connection_snapshot "" "pre_update")"
write_update_state "available" "installer_started"

clear_quarantine "$PACKAGE_ROOT"
clear_quarantine "$INSTALL_ROOT"
if [[ "$BACKGROUND_UPDATE" == "1" ]]; then
  existing_pid_count=0
  if [[ -f "$STATE_DIR/ecorex-webui.pid" ]]; then
    old_pid="$(cat "$STATE_DIR/ecorex-webui.pid" 2>/dev/null || true)"
    if [[ -n "$old_pid" ]] && kill -0 "$old_pid" >/dev/null 2>&1; then
      existing_pid_count=1
    fi
  fi
  active_count="$(active_request_count)"
  if [[ "$active_count" =~ ^[0-9]+$ ]] && [[ "$active_count" -gt 0 ]]; then
    write_update_state "deferred" "active_requests"
    echo "Background update deferred because EcoreX WebUI has $active_count active request(s)."
    exit 75
  fi
  if [[ "$active_count" == "-1" && "$existing_pid_count" == "1" ]]; then
    write_update_state "deferred" "active_requests_unavailable"
    echo "Background update deferred because active request state is unavailable while the local service is running."
    exit 75
  fi
fi
write_update_state "downloading" "package_local_staging"
stop_existing_webui

echo "Copying EcoreX WebUI runtime..."
rsync -a --delete --exclude python --exclude __pycache__ --exclude .pytest_cache --exclude .mypy_cache --exclude .ruff_cache --exclude config.json "$PACKAGE_ROOT/runtime/" "$RUNTIME_DIR/"
write_update_state "verified" "runtime_copied"

if [[ ! -x "$PYTHON_HOME/bin/python3" ]]; then
  [[ -f "$PY_ARCHIVE" ]] || { echo "Missing bundled Python archive: $PY_ARCHIVE" >&2; exit 1; }
  tmp_dir="$(mktemp -d)"
  trap 'rm -rf "$tmp_dir"' EXIT
  tar -xzf "$PY_ARCHIVE" -C "$tmp_dir"
  source_root=""
  if [[ -x "$tmp_dir/python/install/bin/python3" ]]; then
    source_root="$tmp_dir/python/install"
  elif [[ -x "$tmp_dir/python/bin/python3" ]]; then
    source_root="$tmp_dir/python"
  else
    found="$(find "$tmp_dir" -path "*/bin/python3" -type f -perm +111 2>/dev/null | head -n 1 || true)"
    [[ -n "$found" ]] || { echo "Could not locate python3 in bundled archive." >&2; exit 1; }
    source_root="$(cd "$(dirname "$found")/.." && pwd)"
  fi
  rm -rf "$PYTHON_HOME"
  mv "$source_root" "$PYTHON_HOME"
  chmod +x "$PYTHON_HOME/bin/python3" || true
  clear_quarantine "$PYTHON_HOME"
fi

PYTHON="$PYTHON_HOME/bin/python3"
clear_quarantine "$PYTHON"
DEPS_STAMP="$STATE_DIR/deps-$VERSION.ok"
if [[ ! -f "$DEPS_STAMP" ]]; then
  echo "Installing Python dependencies from bundled wheelhouse..."
  PIP_NO_CACHE_DIR=1 "$PYTHON" -m pip install --no-index --no-cache-dir --no-compile --find-links "$WHEEL_DIR" -r "$RUNTIME_DIR/core-requirements.txt"
  date -u +"%Y-%m-%dT%H:%M:%SZ" > "$DEPS_STAMP"
fi

export PATH="$RUNTIME_DIR/tools/bin:$HOME/.npm-global/bin:$HOME/.npm/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
echo "lark-cli preinstall skipped. The structured feishu_cli tool remains visible and installs @larksuite/cli@1.0.56 on demand into the writable state directory after the find-skill gate; npmjs.org timeout should fall back to https://registry.npmmirror.com." >> "$STATE_DIR/lark-cli-install.log"

EFFECTIVE_PORT="$("$PYTHON" - "$PORT" <<'PY'
import socket
import sys

preferred = int(sys.argv[1])
for port in range(preferred, preferred + 50):
    sock = socket.socket()
    try:
        sock.bind(("127.0.0.1", port))
        print(port)
        break
    except OSError:
        pass
    finally:
        sock.close()
else:
    raise SystemExit("No free local port found")
PY
)"

"$PYTHON" - "$RUNTIME_DIR/config.json" "$EFFECTIVE_PORT" "$WORKSPACE_ROOT" "$STATE_DIR" <<'PY'
import json
import os
import pathlib
import sys

config_path = pathlib.Path(sys.argv[1])
port = int(sys.argv[2])
workspace = pathlib.Path(sys.argv[3]).expanduser()
state = pathlib.Path(sys.argv[4]).expanduser()
runtime = config_path.parent
python = runtime / "python" / "bin" / "python3"
if not python.exists():
    python = runtime / "python" / "bin" / "python"
payload = {
    "cow_lang": "auto",
    "channel_type": "web",
    "web_console": True,
    "web_host": "127.0.0.1",
    "web_port": port,
    "web_password": "",
    "web_auto_open": os.environ.get("ECOREX_UPDATE_MODE", "manual") != "background" and os.environ.get("ECOREX_WEB_NO_BROWSER", "") not in ("1", "true", "yes", "on"),
    "agent": True,
    "self_evolution_enabled": True,
    "scheduler_enabled": False,
    "mcp_auto_start": False,
    "agent_workspace": str(workspace),
    "web_file_serve_root": str(workspace),
    "appdata_dir": str(state / "appdata"),
    "use_linkai": False,
    "debug": False,
    "tools": {
        "browser": {
            "cdp_endpoint": "http://127.0.0.1:9222",
            "cdp_auto_launch": True,
            "cdp_fallback": True,
            "persistent": True,
        },
        "feishu_cli": {
            "package": "@larksuite/cli@1.0.56",
            "auto_install": False,
            "allow_system_node": False,
            "install_root": str(state / "tools" / "lark-cli"),
        },
        "tongxin_cli": {
            "script_path": "",
            "python_path": str(python),
            "database_path": str(state / "tongxin.sqlite3"),
            "read_only": True,
            "auth_url": "https://mvdcm.ecoremedia.net/ecorex-agent/client/tongxin/auth",
            "bootstrap_manifest_url": "",
            "bootstrap_url": "",
            "bootstrap_sha256": "",
            "bootstrap_dir": str(state / "tools" / "tongxin"),
        }
    },
    "mcp_servers": [
        {
            "name": "chrome-devtools",
            "type": "stdio",
            "command": "npx",
            "args": ["chrome-devtools-mcp@latest", "--browserUrl", "http://127.0.0.1:9222", "--no-usage-statistics"],
            "timeout": 30,
        }
    ],
}
config_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
PY

URL="http://127.0.0.1:$EFFECTIVE_PORT/app/"
echo "$URL" > "$STATE_DIR/ecorex-webui.url"
write_update_state "staged" "runtime_ready_to_start" "$URL"
echo "Starting EcoreX WebUI local service: $URL"
(
  cd "$RUNTIME_DIR"
  if [[ "$OPEN_BROWSER" != "1" || "$BACKGROUND_UPDATE" == "1" ]]; then
    export ECOREX_WEB_NO_BROWSER=1
  fi
  PYTHONPATH="$RUNTIME_DIR:${PYTHONPATH:-}" nohup "$PYTHON" "$RUNTIME_DIR/app.py" > "$STATE_DIR/ecorex-webui.log" 2> "$STATE_DIR/ecorex-webui.err.log" &
  echo $! > "$STATE_DIR/ecorex-webui.pid"
)

echo "Waiting for EcoreX WebUI to become ready..."
if ! "$PYTHON" - "$URL" <<'PY'
import sys
import time
import urllib.error
import urllib.request

url = sys.argv[1]
for _ in range(60):
    try:
        with urllib.request.urlopen(url, timeout=2) as response:
            if response.status < 500:
                raise SystemExit(0)
    except urllib.error.HTTPError as exc:
        if exc.code < 500:
            raise SystemExit(0)
    except Exception:
        time.sleep(1)
raise SystemExit(f"EcoreX WebUI did not become ready at {url}")
PY
then
  stop_existing_webui
  if restart_previous_runtime "runtime_health_check_failed"; then
    write_update_state "rollback" "runtime_health_check_failed" "$ROLLBACK_URL"
  else
    write_update_state "failed" "runtime_health_check_failed" "$URL"
  fi
  exit 1
fi

POST_UPDATE_EXTERNAL_CONNECTIONS_JSON="$(external_connection_snapshot "$URL" "post_update")"
EXTERNAL_CONNECTION_MISSING_IDS_JSON="$(compare_external_connection_snapshots "$PRE_UPDATE_EXTERNAL_CONNECTIONS_JSON" "$POST_UPDATE_EXTERNAL_CONNECTIONS_JSON")"
missing_count="$("$PYTHON" - "$EXTERNAL_CONNECTION_MISSING_IDS_JSON" <<'PY' 2>/dev/null || echo 0
import json
import sys
try:
    print(len(json.loads(sys.argv[1] or "[]")))
except Exception:
    print(0)
PY
)"
if [[ "$missing_count" != "0" ]]; then
  stop_existing_webui
  if restart_previous_runtime "external_connections_missing_after_update"; then
    write_update_state "rollback" "external_connections_missing_after_update" "$ROLLBACK_URL"
  else
    write_update_state "failed" "external_connections_missing_after_update" "$URL"
  fi
  echo "External connection health check failed after update: $EXTERNAL_CONNECTION_MISSING_IDS_JSON" >&2
  exit 1
fi

echo "$RUNTIME_DIR" > "$CURRENT_RUNTIME_PATH"

open_browser() {
  url="$1"
if [[ "$OPEN_BROWSER" != "1" ]]; then
    return 0
  fi
  echo "Opening EcoreX WebUI in your default browser: $url"
  if command -v open >/dev/null 2>&1; then
    open "$url" >/dev/null 2>&1 || echo "Could not auto-open browser. Open this URL manually: $url" >&2
  else
    echo "Could not find macOS open command. Open this URL manually: $url" >&2
  fi
}

write_launch_script
write_desktop_shortcuts "$URL"

if [[ "$OPEN_BROWSER" == "1" && "$BACKGROUND_UPDATE" != "1" ]]; then
  open_browser "$URL"
fi

final_status="activated"
if [[ "$BACKGROUND_UPDATE" == "1" ]]; then
  final_status="installed"
fi
write_update_state "$final_status" "" "$URL"
echo "EcoreX WebUI is ready: $URL"
if [[ "$BACKGROUND_UPDATE" == "1" ]]; then
  echo "Background update installed. Existing browser tabs should soft-refresh after they observe the new runtime version."
else
  echo "If the browser did not open, double-click a desktop EcoreX WebUI shortcut above or open $URL manually."
fi
'@

New-Item -ItemType Directory -Force -Path (Join-Path $macStage "scripts") | Out-Null
$macInstall = $macInstall.Replace('__ECOREX_VERSION__', $Version)
foreach ($requiredMacMarker in @(
    'EcoreX WebUI package installer:',
    'ecorex-webui.url',
    'write_launch_script',
    'write_desktop_shortcuts "$URL"',
    'open_browser "$URL"'
)) {
    if (-not $macInstall.Contains($requiredMacMarker)) {
        throw "Generated macOS WebUI installer is missing marker: $requiredMacMarker"
    }
}
if ($macInstall.Contains('resume_args')) {
    throw "Generated macOS WebUI installer still contains retired resume_args code"
}
Write-Utf8NoBom -Path (Join-Path $macStage "scripts/install-ecorex-webui-mac.sh") -Value $macInstall
New-MacInstallerApp -AppRoot (Join-Path $macStage "Install EcoreX WebUI.app") -InstallScriptRelative "scripts/install-ecorex-webui-mac.sh"
Write-Utf8NoBom -Path (Join-Path $macStage "release.json") -Value (New-ReleaseJson -ArtifactId "webui-macos-universal" -Platform "macOS arm64/x64" -InstallEntry "Install EcoreX WebUI.app")
$macReadme = "Double-click Install EcoreX WebUI.app. The installer runs without opening Terminal, writes logs to ~/Library/Application Support/EcoreX WebUI/state, starts the local service, and opens http://127.0.0.1:9909/app/ in your browser.`n"
Write-Utf8NoBom -Path (Join-Path $macStage "README.txt") -Value $macReadme

$macStandaloneStage = Join-Path $stagingRoot "$macLeaf-standalone"
$macStandaloneApp = Join-Path $macStandaloneStage "Install EcoreX WebUI.app"
$macStandalonePackage = Join-Path $macStandaloneApp "Contents/Resources/package"
New-MacInstallerApp -AppRoot $macStandaloneApp -InstallScriptRelative "scripts/install-ecorex-webui-mac.sh" -SelfContainedResources
New-Item -ItemType Directory -Force -Path $macStandalonePackage | Out-Null
foreach ($entry in @("runtime", "python", "wheelhouse", "scripts")) {
    Copy-DirectoryWithRobocopy -Source (Join-Path $macStage $entry) -Destination (Join-Path $macStandalonePackage $entry)
}
Copy-Item -LiteralPath (Join-Path $macStage "release.json") -Destination (Join-Path $macStandalonePackage "release.json") -Force
Copy-Item -LiteralPath (Join-Path $macStage "README.txt") -Destination (Join-Path $macStandalonePackage "README.txt") -Force

Compress-ZipWithUnixPermissions `
    -SourceRoot $macStandaloneApp `
    -DestinationPath $macZip `
    -ExecutableRelativePaths @(
        "Install EcoreX WebUI.app/Contents/MacOS/Install EcoreX WebUI",
        "Install EcoreX WebUI.app/Contents/Resources/package/scripts/install-ecorex-webui-mac.sh"
    )

$artifactEntries = @(
    @{ id = "webui-windows-x64"; path = $windowsZip },
    @{ id = "webui-macos-universal"; path = $macZip }
)

if (-not $SkipCombinedPackage) {
    $combinedWindows = Join-Path $combinedStage "windows"
    $combinedMac = Join-Path $combinedStage "macos"
    New-Item -ItemType Directory -Force -Path $combinedWindows, $combinedMac | Out-Null
    Copy-DirectoryWithRobocopy -Source $windowsStage -Destination $combinedWindows
    Copy-DirectoryWithRobocopy -Source $macStage -Destination $combinedMac

    $combinedCmd = @'
@echo off
setlocal
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0windows\scripts\install-ecorex-webui-win.ps1"
if errorlevel 1 (
  echo.
  echo EcoreX WebUI installation failed. Keep this window open and send the error above to support.
  pause
)
'@

    $combinedReadme = @'
EcoreX WebUI dual-platform package

Windows:
  Double-click "Install EcoreX WebUI.cmd".

macOS:
  Double-click "Install EcoreX WebUI.app".
  The installer runs without opening Terminal. If Gatekeeper blocks the first launch,
  right-click the app and choose Open once.

The installer copies EcoreX WebUI to the user-local app data folder, starts the local service, and opens http://127.0.0.1:9909/app/ in the default browser.
'@

    Write-Utf8NoBom -Path (Join-Path $combinedStage "Install EcoreX WebUI.cmd") -Value $combinedCmd
    New-MacInstallerApp -AppRoot (Join-Path $combinedStage "Install EcoreX WebUI.app") -InstallScriptRelative "macos/scripts/install-ecorex-webui-mac.sh"
    Write-Utf8NoBom -Path (Join-Path $combinedStage "README.txt") -Value $combinedReadme
    Write-Utf8NoBom -Path (Join-Path $combinedStage "release.json") -Value (New-ReleaseJson -ArtifactId "webui-win-mac" -Platform "Windows x64 + macOS arm64/x64" -InstallEntry "Install EcoreX WebUI.cmd / Install EcoreX WebUI.app")

    Compress-ZipWithUnixPermissions `
        -SourceRoot $combinedStage `
        -DestinationPath $combinedZip `
        -ExecutableRelativePaths @(
            "Install EcoreX WebUI.app/Contents/MacOS/Install EcoreX WebUI",
            "macos/Install EcoreX WebUI.app/Contents/MacOS/Install EcoreX WebUI",
            "macos/scripts/install-ecorex-webui-mac.sh",
            "macos/runtime/tools/bin/lark-cli"
    )
    $artifactEntries = @(@{ id = "webui-win-mac"; path = $combinedZip }) + $artifactEntries
}

$artifacts = [ordered]@{}
foreach ($entry in $artifactEntries) {
    $item = Get-Item -LiteralPath $entry.path
    $artifacts[$entry.id] = [ordered]@{
        fileName = $item.Name
        path = $item.FullName
        size = $item.Length
        sha256 = Get-EcoreXFileSha256 -Path $item.FullName
    }
}

if (-not $KeepStaging) {
    Remove-PathRobust -Path $stagingRoot -Base $outputResolved
}

[ordered]@{
    ok = $true
    version = $Version
    artifacts = $artifacts
} | ConvertTo-Json -Depth 8
