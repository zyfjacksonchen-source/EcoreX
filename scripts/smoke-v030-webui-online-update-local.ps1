param(
    [string]$PackagePath = "release-artifacts/EcoreX_0.3.1-webui-windows-x64.zip",
    [string]$OutputPath = "docs/v0.3.1/artifacts/user-online-update-local-smoke.json",
    [string]$SmokeRoot = "tmp/v030-webui-online-update-smoke",
    [string]$Version = "0.3.1",
    [int]$SourcePort = 9979,
    [int]$RuntimePort = 9939,
    [int]$TimeoutSeconds = 120,
    [switch]$KeepWorkDir
)

$ErrorActionPreference = "Stop"

function Resolve-FullPath {
    param([Parameter(Mandatory = $true)][string]$Path)
    if ([System.IO.Path]::IsPathRooted($Path)) {
        return [System.IO.Path]::GetFullPath($Path)
    }
    return [System.IO.Path]::GetFullPath((Join-Path (Get-Location) $Path))
}
function Assert-PathInside {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Base
    )
    $full = [System.IO.Path]::GetFullPath($Path)
    $baseFull = [System.IO.Path]::GetFullPath($Base).TrimEnd("\", "/")
    if (-not ($full.Equals($baseFull, [System.StringComparison]::OrdinalIgnoreCase) -or $full.StartsWith($baseFull + [System.IO.Path]::DirectorySeparatorChar, [System.StringComparison]::OrdinalIgnoreCase))) {
        throw "Refusing to operate outside expected base. Path=$full Base=$baseFull"
    }
    return $full
}

function Get-FileSha256Upper {
    param([Parameter(Mandatory = $true)][string]$Path)
    return (Get-FileHash -Algorithm SHA256 -LiteralPath $Path).Hash.ToUpperInvariant()
}

function Write-Utf8NoBom {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][AllowEmptyString()][string]$Value
    )
    $parent = Split-Path -Parent $Path
    if ($parent) {
        New-Item -ItemType Directory -Force -Path $parent | Out-Null
    }
    $encoding = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($Path, ($Value -replace "`r`n", "`n") + "`n", $encoding)
}

function Test-TcpPortOpen {
    param([int]$Port)
    $client = [System.Net.Sockets.TcpClient]::new()
    try {
        $async = $client.BeginConnect("127.0.0.1", $Port, $null, $null)
        if (-not $async.AsyncWaitHandle.WaitOne(250)) {
            return $false
        }
        $client.EndConnect($async)
        return $true
    } catch {
        return $false
    } finally {
        $client.Dispose()
    }
}

function Get-FreePort {
    param([int]$StartPort)
    for ($port = $StartPort; $port -lt ($StartPort + 100); $port++) {
        if (-not (Test-TcpPortOpen -Port $port)) {
            return $port
        }
    }
    throw "No free TCP port found near $StartPort"
}

function Wait-HttpJson {
    param(
        [Parameter(Mandatory = $true)][string]$Url,
        [int]$TimeoutSeconds = 60,
        [int]$RequestTimeoutSeconds = 10
    )
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    $lastError = ""
    while ((Get-Date) -lt $deadline) {
        try {
            return Invoke-RestMethod -UseBasicParsing -Uri $Url -TimeoutSec $RequestTimeoutSeconds
        } catch {
            $lastError = $_.Exception.Message
        }
        Start-Sleep -Milliseconds 800
    }
    throw "Timed out waiting for JSON endpoint: $Url. Last error: $lastError"
}

function Wait-HttpText {
    param(
        [Parameter(Mandatory = $true)][string]$Url,
        [int]$TimeoutSeconds = 60
    )
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    $lastError = ""
    while ((Get-Date) -lt $deadline) {
        try {
            $response = Invoke-WebRequest -UseBasicParsing -Uri $Url -TimeoutSec 5
            if ($response.StatusCode -lt 500) {
                return $response
            }
        } catch {
            $lastError = $_.Exception.Message
        }
        Start-Sleep -Milliseconds 800
    }
    throw "Timed out waiting for HTTP endpoint: $Url. Last error: $lastError"
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
    $pathOnly = $Path
    $query = ""
    $queryIndex = $Path.IndexOf("?")
    if ($queryIndex -ge 0) {
        $pathOnly = $Path.Substring(0, $queryIndex)
        $query = $Path.Substring($queryIndex + 1)
    }
    $relativePath = if ($pathOnly.StartsWith("/")) { $pathOnly } else { "/" + $pathOnly }
    $builder = [UriBuilder]$baseUri
    $builder.Path = $prefix.TrimEnd("/") + $relativePath
    $builder.Query = $query
    $builder.Fragment = ""
    return $builder.Uri.AbsoluteUri
}

function Stop-IsolatedProcesses {
    param([Parameter(Mandatory = $true)][string]$Needle)
    $currentPid = $PID
    try {
        Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
            Where-Object {
                $_.ProcessId -ne $currentPid -and
                $_.CommandLine -and
                $_.CommandLine.IndexOf($Needle, [StringComparison]::OrdinalIgnoreCase) -ge 0
            } |
            ForEach-Object {
                Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
            }
    } catch {
    }
}

function Get-SmokeBrowserProcesses {
    param([Parameter(Mandatory = $true)][int]$Port)
    try {
        return @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
            Where-Object {
                $_.Name -like "msedge*" -and
                $_.CommandLine -and
                (
                    $_.CommandLine.IndexOf("localhost:$Port", [StringComparison]::OrdinalIgnoreCase) -ge 0 -or
                    $_.CommandLine.IndexOf("127.0.0.1:$Port", [StringComparison]::OrdinalIgnoreCase) -ge 0
                )
            } |
            Select-Object ProcessId,Name,CommandLine)
    } catch {
        return @()
    }
}

function Remove-SmokeRootWithTimeout {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [int]$TimeoutSeconds = 30
    )
    if (-not (Test-Path -LiteralPath $Path)) {
        return @{ cleaned = $true; reason = "already_missing" }
    }
    $job = Start-Job -ScriptBlock {
        param($target)
        Remove-Item -LiteralPath $target -Recurse -Force -ErrorAction Stop
    } -ArgumentList $Path
    try {
        $completed = Wait-Job -Job $job -Timeout $TimeoutSeconds
        if ($completed) {
            Receive-Job -Job $job -ErrorAction Stop | Out-Null
            return @{ cleaned = -not (Test-Path -LiteralPath $Path); reason = "removed" }
        }
        return @{ cleaned = $false; reason = "cleanup_timeout" }
    } catch {
        return @{ cleaned = $false; reason = $_.Exception.Message }
    } finally {
        Stop-Job -Job $job -ErrorAction SilentlyContinue
        Remove-Job -Job $job -Force -ErrorAction SilentlyContinue
    }
}

function Wait-UpdateInstalledState {
    param(
        [Parameter(Mandatory = $true)][string]$UpdateStatePath,
        [Parameter(Mandatory = $true)]$InstallerProcess,
        [int]$TimeoutSeconds = 360
    )
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    $lastStatus = ""
    while ((Get-Date) -lt $deadline) {
        if (Test-Path -LiteralPath $UpdateStatePath) {
            try {
                $state = Get-Content -Raw -Encoding UTF8 -LiteralPath $UpdateStatePath | ConvertFrom-Json
                $lastStatus = [string]$state.status
                if ($lastStatus -eq "installed" -or $lastStatus -eq "activated") {
                    return $state
                }
                if ($lastStatus -eq "failed" -or $lastStatus -eq "rollback") {
                    throw "Installer reached terminal failure state: $lastStatus"
                }
            } catch {
                if ($_.Exception.Message -like "Installer reached terminal failure state:*") {
                    throw
                }
            }
        }
        if ($InstallerProcess -and $InstallerProcess.HasExited -and [int]$InstallerProcess.ExitCode -ne 0) {
            throw "Installer process exited with $($InstallerProcess.ExitCode) before installed state. Last status=$lastStatus"
        }
        Start-Sleep -Seconds 1
    }
    throw "Timed out waiting for update-state installed. Last status=$lastStatus"
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

function Add-Check {
    param(
        [System.Collections.ArrayList]$Checks,
        [string]$Name,
        [bool]$Ok,
        $Detail = @{}
    )
    [void]$Checks.Add([ordered]@{
        name = $Name
        status = if ($Ok) { "PASS" } else { "FAIL" }
        detail = $Detail
    })
}

function Write-StepTrace {
    param([Parameter(Mandatory = $true)][string]$Step)
    if (-not $script:TracePath) { return }
    try {
        $line = "[{0}] {1}" -f (Get-Date).ToUniversalTime().ToString("o"), $Step
        Add-Content -LiteralPath $script:TracePath -Value $line -Encoding UTF8
    } catch {
    }
}

$repoRoot = Resolve-FullPath "."
$tmpRoot = Resolve-FullPath "tmp"
$packageResolved = Resolve-FullPath $PackagePath
$smokeRootResolved = Assert-PathInside -Path (Resolve-FullPath $SmokeRoot) -Base $tmpRoot
$outputResolved = Resolve-FullPath $OutputPath

if (-not (Test-Path -LiteralPath $packageResolved)) {
    throw "Package not found: $packageResolved"
}

if (Test-Path -LiteralPath $smokeRootResolved) {
    Remove-Item -LiteralPath $smokeRootResolved -Recurse -Force
}

$webRoot = Join-Path $smokeRootResolved "source"
$downloadRoot = Join-Path $webRoot "downloads"
$localAppData = Join-Path $smokeRootResolved "LocalAppData"
$userProfile = Join-Path $smokeRootResolved "UserProfile"
$stdoutPath = Join-Path $smokeRootResolved "install.stdout.log"
$stderrPath = Join-Path $smokeRootResolved "install.stderr.log"
$serverStdout = Join-Path $smokeRootResolved "source.stdout.log"
$serverStderr = Join-Path $smokeRootResolved "source.stderr.log"
$script:TracePath = Join-Path $smokeRootResolved "smoke.trace.log"
New-Item -ItemType Directory -Force -Path $webRoot, $downloadRoot, $localAppData, $userProfile | Out-Null
Write-StepTrace "prepared smoke directories"

$sourcePort = Get-FreePort -StartPort $SourcePort
$runtimePort = Get-FreePort -StartPort $RuntimePort
$baseUrl = "http://127.0.0.1:$sourcePort"
$packageFileName = Split-Path -Leaf $packageResolved
$packageSha = Get-FileSha256Upper $packageResolved
$packageSize = (Get-Item -LiteralPath $packageResolved).Length
$sourcePackage = Join-Path $downloadRoot $packageFileName
Copy-Item -LiteralPath $packageResolved -Destination $sourcePackage -Force
Copy-Item -LiteralPath (Join-Path $repoRoot "deploy\ecorex-site\install-webui.ps1") -Destination (Join-Path $webRoot "install-webui.ps1") -Force

$sourceManifest = Get-Content -Raw -Encoding UTF8 -LiteralPath (Join-Path $repoRoot "deploy\ecorex-site\manifest.json") | ConvertFrom-Json
$manifestPayload = [ordered]@{
    version = $Version
    channel = "local-smoke"
    updatedAt = (Get-Date).ToUniversalTime().ToString("o")
    releaseName = "EcoreX 0.3.1 local WebUI update smoke"
    notes = "Local user-path online update smoke manifest for v0.3.1."
    releaseIndex = "release-index.json"
    download = [ordered]@{
        mode = "local-smoke"
        mirrors = @(
            [ordered]@{ id = "local-file"; baseUrl = "$baseUrl/downloads"; pathMode = "fileName" }
        )
    }
    trust = [ordered]@{
        artifactSignaturesRequired = $false
        integrityRequired = $true
        signaturePolicy = "not-required-for-webui-local-packages"
    }
    recommendedDownloads = [ordered]@{
        win32 = [ordered]@{ webui = "webui-windows-x64"; primary = "webui-windows-x64" }
        windows = [ordered]@{ webui = "webui-windows-x64"; primary = "webui-windows-x64" }
        web = [ordered]@{ webui = "webui-windows-x64"; primary = "webui-windows-x64" }
    }
    artifacts = @(
        [ordered]@{
            id = "webui-windows-x64"
            label = "EcoreX WebUI Windows x64"
            platform = "windows-x64"
            fileName = $packageFileName
            href = "downloads/$packageFileName"
            status = "ready"
            size = $packageSize
            sha256 = $packageSha
            contentFingerprint = $packageSha
            signature = [ordered]@{ status = "not-required" }
        }
    )
    update = $sourceManifest.update
}

Write-Utf8NoBom -Path (Join-Path $webRoot "manifest.json") -Value ($manifestPayload | ConvertTo-Json -Depth 20)
Write-Utf8NoBom -Path (Join-Path $webRoot "release-index.json") -Value (@{
    schema = "ecorex.release-index.v1"
    version = $Version
    status = "local-smoke"
    artifacts = @($manifestPayload.artifacts[0])
    manifest = @{ path = "manifest.json"; sha256 = Get-FileSha256Upper (Join-Path $webRoot "manifest.json") }
} | ConvertTo-Json -Depth 10)

$python = (Get-Command python -ErrorAction Stop).Source
$server = $null
$oldLocalAppData = $env:LOCALAPPDATA
$oldUserProfile = $env:USERPROFILE
$oldUpdateMode = $env:ECOREX_UPDATE_MODE
$oldReleaseManifestUrl = $env:ECOREX_RELEASE_MANIFEST_URL
$oldWebNoBrowser = $env:ECOREX_WEB_NO_BROWSER
$oldDownloadDisableParallel = $env:ECOREX_DOWNLOAD_DISABLE_PARALLEL
$checks = [System.Collections.ArrayList]::new()
$report = [ordered]@{
    schema = "ecorex.v0.3.1.user-online-update-local-smoke.v1"
    status = "FAIL"
    version = $Version
    generatedAt = (Get-Date).ToUniversalTime().ToString("o")
    baseUrl = $baseUrl
    sourcePort = $sourcePort
    runtimePort = $runtimePort
    package = [ordered]@{
        fileName = $packageFileName
        size = $packageSize
        sha256 = $packageSha
    }
    redacted = $true
}

try {
    $server = Start-Process -FilePath $python -ArgumentList @("-m", "http.server", "$sourcePort", "--bind", "127.0.0.1", "--directory", $webRoot) -PassThru -WindowStyle Hidden -RedirectStandardOutput $serverStdout -RedirectStandardError $serverStderr
    $report.sourceProcessId = $server.Id
    Wait-HttpText -Url "$baseUrl/manifest.json" -TimeoutSeconds 30 | Out-Null
    Write-StepTrace "local source ready"

    $env:LOCALAPPDATA = $localAppData
    $env:USERPROFILE = $userProfile
    $env:ECOREX_UPDATE_MODE = "background"
    $env:ECOREX_RELEASE_MANIFEST_URL = "$baseUrl/manifest.json"
    $env:ECOREX_WEB_NO_BROWSER = "1"
    $env:ECOREX_DOWNLOAD_DISABLE_PARALLEL = "1"

    $installerPath = Join-Path $webRoot "install-webui.ps1"
    $installerArgs = @(
        "-NoProfile",
        "-ExecutionPolicy", "Bypass",
        "-File", $installerPath,
        "-Version", $Version,
        "-BaseUrl", $baseUrl,
        "-Port", "$runtimePort",
        "-NoBrowser"
    )
    Write-StepTrace "starting installer"
    Write-Utf8NoBom -Path $stdoutPath -Value "Installer output is intentionally not redirected because the installed runtime keeps inherited redirection handles open on Windows. This smoke validates the user path through package hash, update-state, runtime /api/version, and /api/update-check.`n"
    Write-Utf8NoBom -Path $stderrPath -Value ""
    $stateDir = Join-Path (Join-Path $localAppData "EcoreX WebUI") "state"
    $downloadPath = Join-Path (Join-Path $localAppData "EcoreX WebUI\downloads") $packageFileName
    $urlFile = Join-Path $stateDir "ecorex-webui.url"
    $currentRuntimeFile = Join-Path $stateDir "current-runtime.txt"
    $updateStatePath = Join-Path $stateDir "update-state.json"
    $install = Start-Process -FilePath "powershell" -ArgumentList $installerArgs -PassThru -WindowStyle Hidden
    $report.installerProcessId = $install.Id
    $updateState = Wait-UpdateInstalledState -UpdateStatePath $updateStatePath -InstallerProcess $install -TimeoutSeconds ([Math]::Max(300, $TimeoutSeconds))
    if (-not $install.HasExited) {
        $report.installerWrapperStoppedAfterRuntimeReady = $true
        Stop-Process -Id $install.Id -Force -ErrorAction SilentlyContinue
    }
    $installExitCode = if ($install.HasExited) { [int]$install.ExitCode } else { 0 }
    Write-StepTrace "installer reached installed state exit=$installExitCode wrapperStopped=$($report.installerWrapperStoppedAfterRuntimeReady)"
    $report.installExitCode = $installExitCode
    Add-Check -Checks $checks -Name "installer reaches installed state" -Ok ($null -ne $updateState -and [string]$updateState.status -eq "installed") -Detail @{
        exitCode = $installExitCode
        wrapperStoppedAfterRuntimeReady = [bool]$report.installerWrapperStoppedAfterRuntimeReady
        status = if ($updateState) { $updateState.status } else { "" }
    }

    $downloadExists = Test-Path -LiteralPath $downloadPath
    $downloadSize = if ($downloadExists) { (Get-Item -LiteralPath $downloadPath).Length } else { 0 }
    Write-StepTrace "download exists=$downloadExists size=$downloadSize"
    $downloadSha = if ($downloadExists) { Get-FileSha256Upper $downloadPath } else { "" }
    Write-StepTrace "download hash complete"
    Add-Check -Checks $checks -Name "downloaded package matches manifest" -Ok ($downloadExists -and $downloadSize -eq $packageSize -and $downloadSha -eq $packageSha) -Detail @{
        exists = $downloadExists
        size = $downloadSize
        sha256 = $downloadSha
    }

    $webuiUrl = if (Test-Path -LiteralPath $urlFile) { (Get-Content -Raw -LiteralPath $urlFile).Trim() } else { "http://127.0.0.1:$runtimePort/app/" }
    $runtimeDir = if (Test-Path -LiteralPath $currentRuntimeFile) { (Get-Content -Raw -LiteralPath $currentRuntimeFile).Trim() } else { "" }
    $versionUrl = Resolve-AppRuntimeUrl -AppUrl $webuiUrl -Path "/api/version"
    Write-StepTrace "checking version $versionUrl"
    $versionPayload = Wait-HttpJson -Url $versionUrl -TimeoutSeconds $TimeoutSeconds
    Write-StepTrace "version ok $($versionPayload.version)"
    Add-Check -Checks $checks -Name "installed runtime responds with v0.3.1" -Ok ([string]$versionPayload.version -eq $Version) -Detail @{
        version = $versionPayload.version
        url = $versionUrl
        runtimeLeaf = if ($runtimeDir) { Split-Path -Leaf $runtimeDir } else { "" }
    }

    Write-StepTrace "update-state loaded"
    Add-Check -Checks $checks -Name "update state records background install" -Ok ($null -ne $updateState -and [string]$updateState.status -eq "installed" -and [string]$updateState.mode -eq "background" -and [string]$updateState.version -eq $Version) -Detail @{
        status = if ($updateState) { $updateState.status } else { "" }
        mode = if ($updateState) { $updateState.mode } else { "" }
        browserAction = if ($updateState) { $updateState.browserAction } else { "" }
        autoLaunchBrowser = if ($updateState) { $updateState.autoLaunchBrowser } else { "" }
    }
    $browserProcesses = Get-SmokeBrowserProcesses -Port $runtimePort
    Write-StepTrace "browser process check count=$($browserProcesses.Count)"
    Add-Check -Checks $checks -Name "background update does not auto-open browser" -Ok ($browserProcesses.Count -eq 0 -and $null -ne $updateState -and [string]$updateState.autoLaunchBrowser -eq "never-in-background") -Detail @{
        autoLaunchBrowser = if ($updateState) { $updateState.autoLaunchBrowser } else { "" }
        processCount = $browserProcesses.Count
        processIds = @($browserProcesses | ForEach-Object { $_.ProcessId })
    }

    $updateCheckUrl = Resolve-AppRuntimeUrl -AppUrl $webuiUrl -Path "/api/update-check?platform=win32"
    Write-StepTrace "checking update endpoint $updateCheckUrl"
    $updatePayload = Wait-HttpJson -Url $updateCheckUrl -TimeoutSeconds 90 -RequestTimeoutSeconds 30
    Write-StepTrace "update endpoint ok latest=$($updatePayload.latestVersion)"
    $connectorPolicy = $updatePayload.update.webui.connectorHealthCheck
    Add-Check -Checks $checks -Name "runtime update-check sees local v0.3.1 manifest" -Ok ([string]$updatePayload.status -eq "success" -and [string]$updatePayload.latestVersion -eq $Version -and [string]$updatePayload.artifact.id -eq "webui-windows-x64") -Detail @{
        status = $updatePayload.status
        currentVersion = $updatePayload.currentVersion
        latestVersion = $updatePayload.latestVersion
        hasUpdate = $updatePayload.hasUpdate
        artifactId = $updatePayload.artifact.id
    }
    Add-Check -Checks $checks -Name "update policy preserves external connectors" -Ok ($null -ne $connectorPolicy -and [bool]$connectorPolicy.required -eq $true -and @($connectorPolicy.preserve).Count -gt 0) -Detail @{
        required = if ($connectorPolicy) { $connectorPolicy.required } else { $false }
        failureAction = if ($connectorPolicy) { $connectorPolicy.failureAction } else { "" }
        preserve = if ($connectorPolicy) { @($connectorPolicy.preserve) } else { @() }
    }

    $report.webuiUrl = $webuiUrl
    $report.currentRuntimeLeaf = if ($runtimeDir) { Split-Path -Leaf $runtimeDir } else { "" }
    $report.updateState = @{
        status = if ($updateState) { $updateState.status } else { "" }
        mode = if ($updateState) { $updateState.mode } else { "" }
        version = if ($updateState) { $updateState.version } else { "" }
        browserAction = if ($updateState) { $updateState.browserAction } else { "" }
        externalConnections = if ($updateState) { $updateState.externalConnections } else { $null }
    }
    $report.updateCheck = @{
        currentVersion = $updatePayload.currentVersion
        latestVersion = $updatePayload.latestVersion
        hasUpdate = $updatePayload.hasUpdate
        updateReason = $updatePayload.updateReason
        artifactId = $updatePayload.artifact.id
        connectorHealthCheckRequired = if ($connectorPolicy) { $connectorPolicy.required } else { $false }
    }
    Write-StepTrace "report payload populated"
} finally {
    Write-StepTrace "entering finally"
    if ($null -eq $oldLocalAppData) { Remove-Item Env:\LOCALAPPDATA -ErrorAction SilentlyContinue } else { $env:LOCALAPPDATA = $oldLocalAppData }
    if ($null -eq $oldUserProfile) { Remove-Item Env:\USERPROFILE -ErrorAction SilentlyContinue } else { $env:USERPROFILE = $oldUserProfile }
    if ($null -eq $oldUpdateMode) { Remove-Item Env:\ECOREX_UPDATE_MODE -ErrorAction SilentlyContinue } else { $env:ECOREX_UPDATE_MODE = $oldUpdateMode }
    if ($null -eq $oldReleaseManifestUrl) { Remove-Item Env:\ECOREX_RELEASE_MANIFEST_URL -ErrorAction SilentlyContinue } else { $env:ECOREX_RELEASE_MANIFEST_URL = $oldReleaseManifestUrl }
    if ($null -eq $oldWebNoBrowser) { Remove-Item Env:\ECOREX_WEB_NO_BROWSER -ErrorAction SilentlyContinue } else { $env:ECOREX_WEB_NO_BROWSER = $oldWebNoBrowser }
    if ($null -eq $oldDownloadDisableParallel) { Remove-Item Env:\ECOREX_DOWNLOAD_DISABLE_PARALLEL -ErrorAction SilentlyContinue } else { $env:ECOREX_DOWNLOAD_DISABLE_PARALLEL = $oldDownloadDisableParallel }
    if ($server -and -not $server.HasExited) {
        Stop-Process -Id $server.Id -Force -ErrorAction SilentlyContinue
    }
    Stop-IsolatedProcesses -Needle $smokeRootResolved
    foreach ($process in Get-SmokeBrowserProcesses -Port $runtimePort) {
        Stop-Process -Id $process.ProcessId -Force -ErrorAction SilentlyContinue
    }
    Write-StepTrace "finally complete"
}

$failures = @($checks | Where-Object { $_.status -ne "PASS" })
$report.checkCount = $checks.Count
$report.passCount = @($checks | Where-Object { $_.status -eq "PASS" }).Count
$report.failCount = $failures.Count
$report.checks = $checks
$report.install = @{
    stdoutTail = Get-TailText -Path $stdoutPath -MaxChars 3200
    stderrTail = Get-TailText -Path $stderrPath -MaxChars 1800
    sourceStderrTail = Get-TailText -Path $serverStderr -MaxChars 1200
    traceTail = Get-TailText -Path $script:TracePath -MaxChars 2400
}
$report.status = if ($failures.Count -eq 0) { "PASS" } else { "FAIL" }
$report.smokeRoot = $smokeRootResolved
$report.smokeRootKept = $true

Write-StepTrace "writing evidence before cleanup status=$($report.status)"
Write-Utf8NoBom -Path $outputResolved -Value ($report | ConvertTo-Json -Depth 20)

if (-not $KeepWorkDir) {
    Write-StepTrace "starting cleanup"
    $cleanupResult = Remove-SmokeRootWithTimeout -Path $smokeRootResolved -TimeoutSeconds 30
    $report.cleanup = $cleanupResult
    $report.smokeRootKept = -not [bool]$cleanupResult.cleaned
} else {
    $report.cleanup = @{ cleaned = $false; reason = "keep_work_dir" }
}

Write-StepTrace "writing final evidence"
Write-Utf8NoBom -Path $outputResolved -Value ($report | ConvertTo-Json -Depth 20)
$report | ConvertTo-Json -Depth 20
if ($report.status -ne "PASS") {
    exit 1
}
