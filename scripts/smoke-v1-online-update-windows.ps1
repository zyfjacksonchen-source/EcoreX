param(
    [Parameter(Mandatory = $true)][string]$Version,
    [Parameter(Mandatory = $true)][string]$FromVersion,
    [Parameter(Mandatory = $true)][string]$Receipt
)
$ErrorActionPreference = "Stop"
[void][Reflection.Assembly]::LoadWithPartialName("System.IO.Compression.FileSystem")
$root = Join-Path $env:RUNNER_TEMP "ecorex-online-update"
if (Test-Path -LiteralPath $root) { throw "online_update_root_not_clean" }
New-Item -ItemType Directory -Path $root | Out-Null
$oldProcess = $null
$newProcess = $null
$previousLocalAppData = $env:LOCALAPPDATA
try {
    $env:LOCALAPPDATA = Join-Path $root "LocalAppData"
    New-Item -ItemType Directory -Path $env:LOCALAPPDATA | Out-Null
    $manifest = Invoke-RestMethod -Uri "https://dl.ecoremedia.net/ecorex-agent/manifest.json" -TimeoutSec 60
    $artifact = @($manifest.artifacts) | Where-Object { $_.id -eq "webui-windows-x64" -and $_.status -eq "ready" } | Select-Object -First 1
    if ($manifest.version -ne $Version -or $null -eq $artifact) { throw "public_manifest_target_invalid" }
    $oldPackage = Join-Path $root "old.zip"
    $newPackage = Join-Path $root "new.zip"
    Invoke-WebRequest -Uri "https://github.com/zyfjacksonchen-source/EcoreX-installers/releases/download/v$FromVersion/EcoreX_$FromVersion-webui-windows-x64.zip" -OutFile $oldPackage -TimeoutSec 300
    Invoke-WebRequest -Uri "https://dl.ecoremedia.net/ecorex-agent/downloads/$($artifact.fileName)" -OutFile $newPackage -TimeoutSec 300
    if ((Get-Item $newPackage).Length -ne [int64]$artifact.size -or (Get-FileHash -Algorithm SHA256 $newPackage).Hash.ToLowerInvariant() -ne ([string]$artifact.sha256).ToLowerInvariant()) { throw "public_package_integrity_invalid" }
    $oldRoot = Join-Path $root "old"
    [IO.Compression.ZipFile]::ExtractToDirectory($oldPackage, $oldRoot)
    $oldInstaller = Join-Path $oldRoot "Install EcoreX WebUI.cmd"
    $oldProcess = Start-Process -FilePath $env:ComSpec -ArgumentList @("/d", "/c", ('"' + $oldInstaller + '"')) -PassThru -WindowStyle Hidden
    $oldVersion = $null
    $deadline = (Get-Date).AddMinutes(8)
    while ((Get-Date) -lt $deadline) {
        try { $oldVersion = Invoke-RestMethod -Uri "http://127.0.0.1:8765/api/version" -TimeoutSec 2; break } catch { Start-Sleep 1 }
    }
    $installRoot = Join-Path $env:LOCALAPPDATA "EcoreX"
    $browserPath = Join-Path $installRoot "bootstrap\browser-opened.json"
    if ($oldVersion.version -ne $FromVersion) { throw "source_install_invalid" }
    $notice = $null
    $deadline = (Get-Date).AddMinutes(5)
    while ((Get-Date) -lt $deadline) {
        try {
            $notice = Invoke-RestMethod -Uri "http://127.0.0.1:8765/api/update-check?platform=win32" -TimeoutSec 2
            if ($notice.currentVersion -eq $FromVersion -and $notice.latestVersion -eq $Version -and $notice.hasUpdate -eq $true -and $notice.artifact.id -eq "webui-windows-x64") { break }
        } catch {}
        Start-Sleep 1
    }
    if ($null -eq $notice -or $notice.hasUpdate -ne $true -or $notice.latestVersion -ne $Version) { throw "update_notification_not_observed" }
    & taskkill.exe /PID $oldProcess.Id /T /F 2>$null | Out-Null
    $oldProcess = $null
    $deadline = (Get-Date).AddMinutes(1)
    while ((Get-Date) -lt $deadline) {
        try { Invoke-RestMethod -Uri "http://127.0.0.1:8765/api/version" -TimeoutSec 1 | Out-Null; Start-Sleep 1 } catch { break }
    }
    $entry = Get-Content -Raw (Join-Path $installRoot "bootstrap\desktop-entry.json") | ConvertFrom-Json
    $bootstrap = [IO.Path]::GetFullPath([string]$entry.launcher_path)
    $versionRoot = [IO.Path]::GetFullPath((Join-Path $installRoot "bootstrap\versions"))
    if (-not $bootstrap.StartsWith($versionRoot + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase) -or [IO.Path]::GetFileName($bootstrap) -ne "ecorex-bootstrap.exe" -or -not (Test-Path -LiteralPath $bootstrap -PathType Leaf)) { throw "online_bootstrap_invalid" }
    $newProcess = Start-Process -FilePath $bootstrap -ArgumentList @("--install-root", $installRoot) -PassThru -WindowStyle Hidden
    $newVersion = $null
    $deadline = (Get-Date).AddMinutes(20)
    while ((Get-Date) -lt $deadline) {
        try { $newVersion = Invoke-RestMethod -Uri "http://127.0.0.1:8765/api/version" -TimeoutSec 2; break } catch { Start-Sleep 1 }
    }
    $browser = Get-Content -Raw $browserPath | ConvertFrom-Json
    $slots = @(Get-ChildItem -LiteralPath (Join-Path $installRoot "slots") -Directory)
    if ($newVersion.version -ne $Version -or $browser.status -ne "opened" -or $browser.version -ne $Version -or $browser.url -ne "http://127.0.0.1:8765/" -or $slots.Count -lt 2) { throw "target_update_invalid" }
    $result = [ordered]@{
        schema_version = 1; status = "passed"; target = "windows-x64"
        from_version = $FromVersion; version = $Version
        downloaded_from_public_production = $true; notification_observed = $true
        online_bootstrap_executed = $true; source_slot_retained = $true
        automatic_browser_open = $true; browser_url = $browser.url
        browser_receipt_sha256 = (Get-FileHash -Algorithm SHA256 $browserPath).Hash.ToLowerInvariant()
        completed_at = (Get-Date).ToUniversalTime().ToString("o")
    }
    New-Item -ItemType Directory -Path (Split-Path -Parent $Receipt) -Force | Out-Null
    [IO.File]::WriteAllText($Receipt, (($result | ConvertTo-Json -Compress) + "`n"), [Text.UTF8Encoding]::new($false))
} finally {
    foreach ($process in @($newProcess, $oldProcess)) {
        if ($null -ne $process -and -not $process.HasExited) { & taskkill.exe /PID $process.Id /T /F 2>$null | Out-Null }
    }
    $env:LOCALAPPDATA = $previousLocalAppData
    Remove-Item -LiteralPath $root -Recurse -Force -ErrorAction SilentlyContinue
}
