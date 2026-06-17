param(
    [string]$Version = "0.1.14",
    [string]$RuntimeRoot = "desktop/runtime/ecorex-runtime",
    [string]$OutputDir = "release-artifacts",
    [switch]$KeepStaging
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

function Copy-OptionalLarkCliWindows {
    param([Parameter(Mandatory = $true)][string]$RuntimeDir)
    $candidates = @()
    if ($env:ECOREX_LARK_CLI_EXE) {
        $candidates += $env:ECOREX_LARK_CLI_EXE
    }
    $candidates += "C:\cli-main\bin\lark-cli.exe"
    foreach ($candidate in $candidates) {
        if ($candidate -and (Test-Path -LiteralPath $candidate)) {
            $targetDir = Join-Path $RuntimeDir "tools\bin"
            New-Item -ItemType Directory -Force -Path $targetDir | Out-Null
            Copy-Item -LiteralPath $candidate -Destination (Join-Path $targetDir "lark-cli.exe") -Force
            Write-Host "Bundled lark-cli for Windows WebUI runtime: $candidate"
            return
        }
    }
    Write-Host "lark-cli.exe was not found; WebUI runtime will use npm auto-install fallback."
}

function Copy-OptionalLarkCliMac {
    param([Parameter(Mandatory = $true)][string]$RuntimeDir)
    if ($env:ECOREX_LARK_CLI_DARWIN -and (Test-Path -LiteralPath $env:ECOREX_LARK_CLI_DARWIN)) {
        $targetDir = Join-Path $RuntimeDir "tools/bin"
        New-Item -ItemType Directory -Force -Path $targetDir | Out-Null
        Copy-Item -LiteralPath $env:ECOREX_LARK_CLI_DARWIN -Destination (Join-Path $targetDir "lark-cli") -Force
        Write-Host "Bundled lark-cli for macOS WebUI runtime: $env:ECOREX_LARK_CLI_DARWIN"
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
  /usr/bin/osascript -e 'display notification "EcoreX WebUI is running and the browser has been opened." with title "EcoreX WebUI"' >/dev/null 2>&1 || true
) >> "$LOG_FILE" 2>> "$ERR_FILE" || {
  /usr/bin/osascript -e "display dialog \"EcoreX WebUI installation failed. Check the log: $ERR_FILE\" buttons {\"OK\"} default button \"OK\" with icon caution" >/dev/null 2>&1 || true
} &
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
        [string[]]$ExecutableRelativePaths = @()
    )

    $python = @'
import os
import sys
import time
import zipfile

source = os.path.abspath(sys.argv[1])
destination = os.path.abspath(sys.argv[2])
executable = {
    item.replace("\\", "/").strip("/")
    for item in sys.argv[3:]
    if item
}
base = os.path.dirname(source)

with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
    for root, dirs, files in os.walk(source):
        dirs.sort()
        files.sort()
        for name in files:
            path = os.path.join(root, name)
            rel = os.path.relpath(path, base).replace(os.sep, "/")
            rel_in_source = os.path.relpath(path, source).replace(os.sep, "/")
            stat_result = os.stat(path)
            info = zipfile.ZipInfo(rel, time.localtime(stat_result.st_mtime)[:6])
            mode = 0o100755 if (
                rel_in_source in executable
                or rel_in_source.endswith(".command")
                or rel_in_source.endswith(".sh")
            ) else 0o100644
            info.external_attr = mode << 16
            with open(path, "rb") as handle:
                archive.writestr(info, handle.read(), compress_type=zipfile.ZIP_DEFLATED)
'@

    $helperPath = Join-Path ([System.IO.Path]::GetTempPath()) ("ecorex-zip-" + [guid]::NewGuid().ToString("N") + ".py")
    $encoding = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($helperPath, $python, $encoding)
    try {
        & python $helperPath $SourceRoot $DestinationPath $ExecutableRelativePaths
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
        [string]$Destination
    )
    $requirementsPath = Resolve-RequiredPath "desktop/runtime-packs/core-requirements.txt"
    $requirementsHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $requirementsPath).Hash.ToUpperInvariant()
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
    if (Test-Path -LiteralPath $path) {
        Remove-Item -LiteralPath $path -Recurse -Force
    }
}
New-Item -ItemType Directory -Force -Path $windowsStage, $macStage, $combinedStage | Out-Null

$winRuntime = Join-Path $windowsStage "runtime"
Copy-Item -LiteralPath $runtimeRootResolved -Destination $winRuntime -Recurse -Force
Remove-GeneratedNoise -Root $winRuntime
Sync-DesktopWebBuild -RuntimeDir $winRuntime
Copy-OptionalLarkCliWindows -RuntimeDir $winRuntime
Invoke-ReleaseRuntimeSanitizer -RuntimeDir $winRuntime

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
    [switch]$NoBrowser
)

$ErrorActionPreference = "Stop"

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

function Ensure-LarkCli {
    param(
        [Parameter(Mandatory = $true)][string]$RuntimeDir,
        [Parameter(Mandatory = $true)][string]$StateDir
    )
    $pathCandidates = @((Join-Path $RuntimeDir "tools\bin"))
    foreach ($base in @($env:APPDATA, $env:LOCALAPPDATA)) {
        if ($base) { $pathCandidates += (Join-Path $base "npm") }
    }
    if ($env:ProgramFiles) { $pathCandidates += (Join-Path $env:ProgramFiles "nodejs") }
    $pathParts = $pathCandidates | Where-Object { $_ -and (Test-Path -LiteralPath $_) }
    if ($pathParts.Count -gt 0) {
        $env:Path = ($pathParts -join [System.IO.Path]::PathSeparator) + [System.IO.Path]::PathSeparator + $env:Path
    }
    if (Get-Command lark-cli -ErrorAction SilentlyContinue) {
        return
    }
    $npm = Get-Command npm.cmd -ErrorAction SilentlyContinue
    if (-not $npm) {
        $npm = Get-Command npm -ErrorAction SilentlyContinue
    }
    if (-not $npm) {
        "npm not found; lark-cli will be installed by runtime fallback if possible." | Out-File -FilePath (Join-Path $StateDir "lark-cli-install.log") -Encoding utf8 -Append
        return
    }
    try {
        & $npm.Source install -g "@larksuite/cli@1.0.40" *> (Join-Path $StateDir "lark-cli-install.log")
    } catch {
        "lark-cli install failed: $($_.Exception.Message)" | Out-File -FilePath (Join-Path $StateDir "lark-cli-install.log") -Encoding utf8 -Append
    }
}

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$packageRoot = Split-Path -Parent $scriptDir
$sourceRuntime = Join-Path $packageRoot "runtime"
$installRoot = Join-Path $env:LOCALAPPDATA "EcoreX WebUI"
$runtimeDir = Join-Path $installRoot "runtime"
$stateDir = Join-Path $installRoot "state"
$workspaceRoot = Join-Path $env:USERPROFILE "EcoreX"
$logPath = Join-Path $stateDir "ecorex-webui.log"
$errorLogPath = Join-Path $stateDir "ecorex-webui.err.log"

New-Item -ItemType Directory -Force -Path $installRoot, $stateDir, $workspaceRoot | Out-Null
robocopy $sourceRuntime $runtimeDir /MIR /XD __pycache__ .pytest_cache .mypy_cache .ruff_cache /XF *.pyc *.pyo config.json | Out-Null
if ($LASTEXITCODE -gt 7) {
    throw "Failed to copy runtime to $runtimeDir; robocopy exit code $LASTEXITCODE"
}

$effectivePort = Get-FreePort -Preferred $Port
$config = [ordered]@{
    cow_lang = "auto"
    channel_type = "web"
    web_console = $true
    web_host = "127.0.0.1"
    web_port = $effectivePort
    web_password = ""
    agent = $true
    self_evolution_enabled = $false
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
            cdp_auto_launch = $false
            cdp_fallback = $true
            persistent = $true
        }
        feishu_cli = [ordered]@{
            package = "@larksuite/cli@1.0.40"
            auto_install = $false
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

$python = Join-Path $runtimeDir "python\python.exe"
if (-not (Test-Path -LiteralPath $python)) {
    throw "Packaged Python runtime is missing: $python"
}

$existing = Get-CimInstance Win32_Process -Filter "Name = 'python.exe' OR Name = 'pythonw.exe'" |
    Where-Object { $_.CommandLine -like "*$runtimeDir*" -and $_.CommandLine -like "*app.py*" }
if (-not $existing) {
    Start-Process -FilePath $python `
        -ArgumentList "app.py" `
        -WorkingDirectory $runtimeDir `
        -WindowStyle Hidden `
        -RedirectStandardOutput $logPath `
        -RedirectStandardError $errorLogPath
}

$url = "http://127.0.0.1:$effectivePort/app/"
Wait-WebUi -Url $url

$shortcut = Join-Path ([Environment]::GetFolderPath("Desktop")) "EcoreX WebUI.url"
"[InternetShortcut]`r`nURL=$url`r`n" | Set-Content -LiteralPath $shortcut -Encoding ASCII

if (-not $NoBrowser) {
    Start-Process $url
}

Write-Host "EcoreX WebUI is ready: $url"
'@

New-Item -ItemType Directory -Force -Path (Join-Path $windowsStage "scripts") | Out-Null
Write-Utf8NoBom -Path (Join-Path $windowsStage "Install EcoreX WebUI.cmd") -Value $windowsCmd
Write-Utf8NoBom -Path (Join-Path $windowsStage "scripts/install-ecorex-webui-win.ps1") -Value $windowsPs1
Write-Utf8NoBom -Path (Join-Path $windowsStage "release.json") -Value (New-ReleaseJson -ArtifactId "webui-windows-x64" -Platform "Windows x64" -InstallEntry "Install EcoreX WebUI.cmd")
Write-Utf8NoBom -Path (Join-Path $windowsStage "README.txt") -Value "Double-click Install EcoreX WebUI.cmd. The installer copies EcoreX WebUI to your local app data, starts the local service, and opens http://127.0.0.1:9909/app/ in your browser.`n"

Compress-Archive -Path (Join-Path $windowsStage "*") -DestinationPath $windowsZip -CompressionLevel Optimal -Force

$macRuntime = Join-Path $macStage "runtime"
Copy-Item -LiteralPath $runtimeRootResolved -Destination $macRuntime -Recurse -Force
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
Invoke-PipDownload -Platform "macosx_11_0_arm64" -Destination $wheelArm
Invoke-PipDownload -Platform "macosx_11_0_x86_64" -Destination $wheelX64

New-Item -ItemType Directory -Force -Path (Join-Path $macStage "python"), (Join-Path $macStage "wheelhouse") | Out-Null
Copy-Item -LiteralPath $pyArm -Destination (Join-Path $macStage "python/$(Split-Path -Leaf $pyArm)") -Force
Copy-Item -LiteralPath $pyX64 -Destination (Join-Path $macStage "python/$(Split-Path -Leaf $pyX64)") -Force
Copy-Item -LiteralPath $wheelArm -Destination (Join-Path $macStage "wheelhouse/mac-arm64") -Recurse -Force
Copy-Item -LiteralPath $wheelX64 -Destination (Join-Path $macStage "wheelhouse/mac-x64") -Recurse -Force

$macInstall = @'
#!/usr/bin/env bash
set -euo pipefail

VERSION="0.1.14"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PACKAGE_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
INSTALL_ROOT="${ECOREX_WEBUI_INSTALL_ROOT:-$HOME/Library/Application Support/EcoreX WebUI}"
RUNTIME_DIR="$INSTALL_ROOT/runtime"
STATE_DIR="$INSTALL_ROOT/state"
WORKSPACE_ROOT="${ECOREX_WORKSPACE_ROOT:-$HOME/EcoreX}"
PORT="${ECOREX_WEB_PORT:-9909}"
OPEN_BROWSER="${OPEN_BROWSER:-1}"
PYTHON_HOME="$INSTALL_ROOT/python"

clear_quarantine() {
  if command -v xattr >/dev/null 2>&1 && [[ -e "$1" ]]; then
    xattr -dr com.apple.quarantine "$1" >/dev/null 2>&1 || true
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

clear_quarantine "$PACKAGE_ROOT"
clear_quarantine "$INSTALL_ROOT"

if [[ -f "$STATE_DIR/ecorex-webui.pid" ]]; then
  old_pid="$(cat "$STATE_DIR/ecorex-webui.pid" 2>/dev/null || true)"
  if [[ -n "$old_pid" ]] && kill -0 "$old_pid" >/dev/null 2>&1; then
    kill "$old_pid" >/dev/null 2>&1 || true
    sleep 1
  fi
fi
pkill -f "$RUNTIME_DIR/app.py" >/dev/null 2>&1 || true

rsync -a --delete --exclude python --exclude __pycache__ --exclude .pytest_cache --exclude .mypy_cache --exclude .ruff_cache --exclude config.json "$PACKAGE_ROOT/runtime/" "$RUNTIME_DIR/"

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
  PIP_NO_CACHE_DIR=1 "$PYTHON" -m pip install --no-index --no-cache-dir --no-compile --find-links "$WHEEL_DIR" -r "$RUNTIME_DIR/core-requirements.txt"
  date -u +"%Y-%m-%dT%H:%M:%SZ" > "$DEPS_STAMP"
fi

export PATH="$RUNTIME_DIR/tools/bin:$HOME/.npm-global/bin:$HOME/.npm/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
if ! command -v lark-cli >/dev/null 2>&1; then
  if command -v npm >/dev/null 2>&1; then
    npm install -g "@larksuite/cli@1.0.40" >> "$STATE_DIR/lark-cli-install.log" 2>&1 || true
  else
    echo "npm not found; lark-cli will be installed by runtime fallback if possible." >> "$STATE_DIR/lark-cli-install.log"
  fi
fi

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
import pathlib
import sys

config_path = pathlib.Path(sys.argv[1])
port = int(sys.argv[2])
workspace = pathlib.Path(sys.argv[3]).expanduser()
state = pathlib.Path(sys.argv[4]).expanduser()
payload = {
    "cow_lang": "auto",
    "channel_type": "web",
    "web_console": True,
    "web_host": "127.0.0.1",
    "web_port": port,
    "web_password": "",
    "agent": True,
    "self_evolution_enabled": False,
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
            "cdp_auto_launch": False,
            "cdp_fallback": True,
            "persistent": True,
        },
        "feishu_cli": {
            "package": "@larksuite/cli@1.0.40",
            "auto_install": False,
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

if ! pgrep -f "$RUNTIME_DIR/app.py" >/dev/null 2>&1; then
  (
    cd "$RUNTIME_DIR"
    PYTHONPATH="$RUNTIME_DIR:${PYTHONPATH:-}" nohup "$PYTHON" app.py > "$STATE_DIR/ecorex-webui.log" 2> "$STATE_DIR/ecorex-webui.err.log" &
    echo $! > "$STATE_DIR/ecorex-webui.pid"
  )
fi

URL="http://127.0.0.1:$EFFECTIVE_PORT/app/"
"$PYTHON" - "$URL" <<'PY'
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

if [[ "$OPEN_BROWSER" == "1" ]]; then
  open "$URL"
fi

echo "EcoreX WebUI is ready: $URL"
'@

New-Item -ItemType Directory -Force -Path (Join-Path $macStage "scripts") | Out-Null
Write-Utf8NoBom -Path (Join-Path $macStage "scripts/install-ecorex-webui-mac.sh") -Value $macInstall
New-MacInstallerApp -AppRoot (Join-Path $macStage "Install EcoreX WebUI.app") -InstallScriptRelative "scripts/install-ecorex-webui-mac.sh"
Write-Utf8NoBom -Path (Join-Path $macStage "release.json") -Value (New-ReleaseJson -ArtifactId "webui-macos-universal" -Platform "macOS arm64/x64" -InstallEntry "Install EcoreX WebUI.app")
Write-Utf8NoBom -Path (Join-Path $macStage "README.txt") -Value "Double-click Install EcoreX WebUI.app. The installer runs without opening Terminal, writes logs to ~/Library/Application Support/EcoreX WebUI/state, starts the local service, and opens http://127.0.0.1:9909/app/ in your browser.`n"

$macStandaloneStage = Join-Path $stagingRoot "$macLeaf-standalone"
$macStandaloneApp = Join-Path $macStandaloneStage "Install EcoreX WebUI.app"
$macStandalonePackage = Join-Path $macStandaloneApp "Contents/Resources/package"
New-MacInstallerApp -AppRoot $macStandaloneApp -InstallScriptRelative "scripts/install-ecorex-webui-mac.sh" -SelfContainedResources
New-Item -ItemType Directory -Force -Path $macStandalonePackage | Out-Null
foreach ($entry in @("runtime", "python", "wheelhouse", "scripts")) {
    Copy-Item -LiteralPath (Join-Path $macStage $entry) -Destination (Join-Path $macStandalonePackage $entry) -Recurse -Force
}
Copy-Item -LiteralPath (Join-Path $macStage "release.json") -Destination (Join-Path $macStandalonePackage "release.json") -Force
Copy-Item -LiteralPath (Join-Path $macStage "README.txt") -Destination (Join-Path $macStandalonePackage "README.txt") -Force

Compress-ZipWithUnixPermissions `
    -SourceRoot $macStandaloneApp `
    -DestinationPath $macZip `
    -ExecutableRelativePaths @(
        "Install EcoreX WebUI.app/Contents/MacOS/Install EcoreX WebUI",
        "Install EcoreX WebUI.app/Contents/Resources/package/scripts/install-ecorex-webui-mac.sh",
        "Install EcoreX WebUI.app/Contents/Resources/package/runtime/tools/bin/lark-cli"
    )

$combinedWindows = Join-Path $combinedStage "windows"
$combinedMac = Join-Path $combinedStage "macos"
New-Item -ItemType Directory -Force -Path $combinedWindows, $combinedMac | Out-Null
Copy-Item -Path (Join-Path $windowsStage "*") -Destination $combinedWindows -Recurse -Force
Copy-Item -Path (Join-Path $macStage "*") -Destination $combinedMac -Recurse -Force

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

$artifacts = [ordered]@{}
foreach ($entry in @(
    @{ id = "webui-win-mac"; path = $combinedZip },
    @{ id = "webui-windows-x64"; path = $windowsZip },
    @{ id = "webui-macos-universal"; path = $macZip }
)) {
    $item = Get-Item -LiteralPath $entry.path
    $artifacts[$entry.id] = [ordered]@{
        fileName = $item.Name
        path = $item.FullName
        size = $item.Length
        sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $item.FullName).Hash.ToUpperInvariant()
    }
}

if (-not $KeepStaging) {
    Remove-Item -LiteralPath $stagingRoot -Recurse -Force
}

[ordered]@{
    ok = $true
    version = $Version
    artifacts = $artifacts
} | ConvertTo-Json -Depth 8
