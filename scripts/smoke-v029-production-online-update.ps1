param(
    [string]$BaseUrl = "https://mvdcm.ecoremedia.net/ecorex-agent",
    [string]$Version = "0.2.9",
    [string]$OutputPath = "docs/v0.2.9/artifacts/production-online-update-real-smoke.json",
    [string]$WorkRoot = "",
    [switch]$KeepWorkDir
)

$ErrorActionPreference = "Stop"

function Get-Sha256 {
    param([Parameter(Mandatory = $true)][string]$Path)
    return (Get-FileHash -Algorithm SHA256 -LiteralPath $Path).Hash.ToUpperInvariant()
}

function Get-TextHash {
    param([AllowNull()][string]$Text)
    if ($null -eq $Text) { $Text = "" }
    $bytes = [Text.Encoding]::UTF8.GetBytes($Text)
    $sha = [Security.Cryptography.SHA256]::Create()
    try {
        return [BitConverter]::ToString($sha.ComputeHash($bytes)).Replace("-", "")
    } finally {
        $sha.Dispose()
    }
}

function Get-TailText {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [int]$MaxChars = 2400
    )
    if (-not (Test-Path -LiteralPath $Path)) { return "" }
    $text = [IO.File]::ReadAllText($Path)
    if ($text.Length -le $MaxChars) { return $text }
    return $text.Substring($text.Length - $MaxChars)
}

function Stop-IsolatedProcesses {
    param([Parameter(Mandatory = $true)][string]$Needle)
    $currentPid = $PID
    $stopped = 0
    $processes = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
        Where-Object {
            $_.ProcessId -ne $currentPid -and
            $_.CommandLine -and
            $_.CommandLine.IndexOf($Needle, [StringComparison]::OrdinalIgnoreCase) -ge 0
        }
    foreach ($process in $processes) {
        try {
            Stop-Process -Id $process.ProcessId -Force -ErrorAction Stop
            $stopped += 1
        } catch {
        }
    }
    return $stopped
}

function Resolve-AppRuntimeUrl {
    param(
        [Parameter(Mandatory = $true)][string]$AppUrl,
        [Parameter(Mandatory = $true)][string]$Path
    )
    $baseUri = [Uri]$AppUrl
    $appPath = $baseUri.AbsolutePath
    $appIndex = $appPath.IndexOf("/app", [StringComparison]::OrdinalIgnoreCase)
    $prefix = if ($appIndex -ge 0) { $appPath.Substring(0, $appIndex) } else { $appPath.TrimEnd("/") }
    $relativePath = if ($Path.StartsWith("/")) { $Path } else { "/" + $Path }
    $builder = [UriBuilder]$baseUri
    $builder.Path = $prefix.TrimEnd("/") + $relativePath
    $builder.Query = ""
    $builder.Fragment = ""
    return $builder.Uri.AbsoluteUri
}

function Read-AppAssetText {
    param(
        [Parameter(Mandatory = $true)][string]$AppUrl,
        [Parameter(Mandatory = $true)][string]$Pattern
    )
    $html = Invoke-WebRequest -UseBasicParsing -Uri $AppUrl -TimeoutSec 25
    $text = ""
    foreach ($match in [regex]::Matches($html.Content, $Pattern)) {
        $relative = $match.Groups[1].Value
        $assetUrl = [Uri]::new([Uri]$AppUrl, $relative).AbsoluteUri
        $text += (Invoke-WebRequest -UseBasicParsing -Uri $assetUrl -TimeoutSec 45).Content
    }
    return $text
}

$repoRoot = (Resolve-Path -LiteralPath ".").Path
$outputResolved = [IO.Path]::GetFullPath((Join-Path $repoRoot $OutputPath))
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $outputResolved) | Out-Null

if (-not $WorkRoot) {
    $stamp = Get-Date -Format "yyyyMMddHHmmss"
    $WorkRoot = Join-Path $repoRoot "tmp/online-update-smoke-final-$stamp"
}
$workRootResolved = [IO.Path]::GetFullPath($WorkRoot)
$localAppData = Join-Path $workRootResolved "LocalAppData"
$userProfile = Join-Path $workRootResolved "UserProfile"
$stdoutPath = Join-Path $workRootResolved "install.stdout.log"
$stderrPath = Join-Path $workRootResolved "install.stderr.log"
$installerPath = Join-Path $workRootResolved "install-webui.ps1"

$oldLocalAppData = $env:LOCALAPPDATA
$oldUserProfile = $env:USERPROFILE
$oldUpdateMode = $env:ECOREX_UPDATE_MODE
$oldAssetBases = $env:ECOREX_DOWNLOAD_ASSET_BASE_URLS
$oldDownloadBases = $env:ECOREX_DOWNLOAD_BASE_URLS

$payload = $null
$cleanup = [ordered]@{}
try {
    New-Item -ItemType Directory -Force -Path $workRootResolved, $localAppData, $userProfile | Out-Null

    $manifestUrl = $BaseUrl.TrimEnd("/") + "/manifest.json"
    $manifest = Invoke-RestMethod -UseBasicParsing -Uri $manifestUrl -TimeoutSec 30
    $artifact = @($manifest.artifacts) |
        Where-Object { $_.id -eq "webui-windows-x64" -and $_.status -eq "ready" } |
        Select-Object -First 1
    if (-not $artifact) {
        throw "Ready webui-windows-x64 artifact was not found in manifest."
    }

    Invoke-WebRequest -UseBasicParsing -Uri ($BaseUrl.TrimEnd("/") + "/install-webui.ps1") -OutFile $installerPath -TimeoutSec 60

    $env:LOCALAPPDATA = $localAppData
    $env:USERPROFILE = $userProfile
    $env:ECOREX_UPDATE_MODE = "background"
    Remove-Item Env:\ECOREX_DOWNLOAD_ASSET_BASE_URLS -ErrorAction SilentlyContinue
    Remove-Item Env:\ECOREX_DOWNLOAD_BASE_URLS -ErrorAction SilentlyContinue

    $args = @(
        "-NoProfile",
        "-ExecutionPolicy", "Bypass",
        "-File", $installerPath,
        "-Version", $Version,
        "-BaseUrl", $BaseUrl,
        "-NoBrowser"
    )
    $proc = Start-Process -FilePath "powershell" -ArgumentList $args -Wait -PassThru -WindowStyle Hidden -RedirectStandardOutput $stdoutPath -RedirectStandardError $stderrPath
    $exitCode = [int]$proc.ExitCode

    $stateDir = Join-Path (Join-Path $localAppData "EcoreX WebUI") "state"
    $downloadPath = Join-Path (Join-Path $localAppData "EcoreX WebUI\downloads") ([string]$artifact.fileName)
    $urlFile = Join-Path $stateDir "ecorex-webui.url"
    $currentRuntimePath = Join-Path $stateDir "current-runtime.txt"
    $updateStatePath = Join-Path $stateDir "update-state.json"
    $webuiUrl = if (Test-Path -LiteralPath $urlFile) { (Get-Content -Raw -LiteralPath $urlFile).Trim() } else { "" }
    $runtimeDir = if (Test-Path -LiteralPath $currentRuntimePath) { (Get-Content -Raw -LiteralPath $currentRuntimePath).Trim() } else { "" }
    $updateState = if (Test-Path -LiteralPath $updateStatePath) { Get-Content -Raw -LiteralPath $updateStatePath | ConvertFrom-Json } else { $null }
    $apiVersion = if ($webuiUrl) { Invoke-RestMethod -UseBasicParsing -Uri (Resolve-AppRuntimeUrl -AppUrl $webuiUrl -Path "/api/version") -TimeoutSec 30 } else { $null }

    $stdoutAll = if (Test-Path -LiteralPath $stdoutPath) { [IO.File]::ReadAllText($stdoutPath) } else { "" }
    $stderrAll = if (Test-Path -LiteralPath $stderrPath) { [IO.File]::ReadAllText($stderrPath) } else { "" }
    $downloadExists = Test-Path -LiteralPath $downloadPath
    $downloadSize = if ($downloadExists) { (Get-Item -LiteralPath $downloadPath).Length } else { 0 }
    $downloadSha = if ($downloadExists) { Get-Sha256 $downloadPath } else { "" }
    $expectedSha = ([string]$artifact.sha256).ToUpperInvariant()
    $firstDownloadSourceIsCdn = [bool]($stdoutAll -match "Using primary CDN download source:\s+https://dl\.ecoremedia\.net/")

    $checks = [ordered]@{
        installerExitCode = $exitCode -eq 0
        manifestVersion = [string]$manifest.version -eq $Version
        firstDownloadSourceIsCdn = $firstDownloadSourceIsCdn
        downloadedPackage = $downloadExists -and $downloadSize -eq [int64]$artifact.size -and $downloadSha -eq $expectedSha
        runtimeReady = $null -ne $apiVersion -and [string]$apiVersion.version -eq $Version
        updateStateInstalled = $null -ne $updateState -and [string]$updateState.status -eq "installed" -and [string]$updateState.version -eq $Version
    }
    $failed = @($checks.GetEnumerator() | Where-Object { -not [bool]$_.Value })
    $status = if ($failed.Count -eq 0) { "PASS" } else { "FAIL" }

    $payload = [ordered]@{
        status = $status
        scope = "production-online-update-real-smoke"
        version = $Version
        generatedAt = (Get-Date).ToUniversalTime().ToString("o")
        baseUrl = $BaseUrl
        smokeRoot = $workRootResolved
        smokeRootHash = Get-TextHash $workRootResolved
        manifest = [ordered]@{
            statusCode = 200
            version = $manifest.version
            downloadMode = $manifest.download.mode
            firstMirrorBaseUrl = $manifest.download.mirrors[0].baseUrl
        }
        artifact = [ordered]@{
            id = $artifact.id
            fileName = $artifact.fileName
            href = $artifact.href
            status = $artifact.status
            size = [int64]$artifact.size
            sha256 = $artifact.sha256
        }
        steps = [ordered]@{
            installScriptDownloaded = [ordered]@{
                ok = Test-Path -LiteralPath $installerPath
                size = (Get-Item -LiteralPath $installerPath).Length
                sha256 = Get-Sha256 $installerPath
            }
        }
        install = [ordered]@{
            exitCode = $exitCode
            firstDownloadSourceIsCdn = $firstDownloadSourceIsCdn
            stdoutHash = Get-TextHash $stdoutAll
            stderrHash = Get-TextHash $stderrAll
            stdoutTail = Get-TailText -Path $stdoutPath -MaxChars 3200
            stderrTail = Get-TailText -Path $stderrPath -MaxChars 1800
        }
        downloadedPackage = [ordered]@{
            exists = $downloadExists
            size = $downloadSize
            sha256 = $downloadSha
            sizeMatchesManifest = $downloadSize -eq [int64]$artifact.size
            sha256MatchesManifest = $downloadSha -eq $expectedSha
        }
        runtime = [ordered]@{
            webuiUrl = $webuiUrl
            runtimeDirHash = Get-TextHash $runtimeDir
            apiVersionStatus = if ($apiVersion) { 200 } else { 0 }
            apiVersion = if ($apiVersion) { $apiVersion.version } else { "" }
            updateState = $updateState
            uiMarkersCoveredBy = "scripts/smoke-v029-webui-followups-cdp.mjs"
        }
        checks = $checks
        cleanup = [ordered]@{}
    }
    $payload | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath $outputResolved -Encoding UTF8
    if ($status -ne "PASS") {
        throw "Online update smoke failed: $($failed.Name -join ', ')"
    }
} finally {
    $stopped = Stop-IsolatedProcesses -Needle $workRootResolved
    Start-Sleep -Milliseconds 500
    $removeError = $null
    if (-not $KeepWorkDir) {
        try {
            Remove-Item -LiteralPath $workRootResolved -Recurse -Force -ErrorAction Stop
        } catch {
            $removeError = $_.Exception.GetType().Name
        }
    }
    if ($null -eq $oldLocalAppData) { Remove-Item Env:\LOCALAPPDATA -ErrorAction SilentlyContinue } else { $env:LOCALAPPDATA = $oldLocalAppData }
    if ($null -eq $oldUserProfile) { Remove-Item Env:\USERPROFILE -ErrorAction SilentlyContinue } else { $env:USERPROFILE = $oldUserProfile }
    if ($null -eq $oldUpdateMode) { Remove-Item Env:\ECOREX_UPDATE_MODE -ErrorAction SilentlyContinue } else { $env:ECOREX_UPDATE_MODE = $oldUpdateMode }
    if ($null -eq $oldAssetBases) { Remove-Item Env:\ECOREX_DOWNLOAD_ASSET_BASE_URLS -ErrorAction SilentlyContinue } else { $env:ECOREX_DOWNLOAD_ASSET_BASE_URLS = $oldAssetBases }
    if ($null -eq $oldDownloadBases) { Remove-Item Env:\ECOREX_DOWNLOAD_BASE_URLS -ErrorAction SilentlyContinue } else { $env:ECOREX_DOWNLOAD_BASE_URLS = $oldDownloadBases }
    if (Test-Path -LiteralPath $outputResolved) {
        $current = Get-Content -Raw -LiteralPath $outputResolved | ConvertFrom-Json
        $current.cleanup = [ordered]@{
            stoppedProcessCount = $stopped
            tempKept = [bool]$KeepWorkDir
            tempRemoved = if ($KeepWorkDir) { $false } else { -not (Test-Path -LiteralPath $workRootResolved) }
            tempRemoveError = $removeError
            performedAt = (Get-Date).ToUniversalTime().ToString("o")
        }
        $current | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath $outputResolved -Encoding UTF8
    }
}

Get-Content -Raw -LiteralPath $outputResolved
