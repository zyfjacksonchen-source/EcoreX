param(
    [string[]]$LegacyVersions = @("0.2.7.1", "0.2.7.2"),
    [string]$TargetVersion = "0.2.8",
    [string]$BaseUrl = "https://mvdcm.ecoremedia.net/ecorex-agent",
    [string]$ArtifactDir = "release-artifacts",
    [string]$OutputPath = "docs/v0.2.8/artifacts/legacy-webui-online-upgrade.json",
    [string]$WorkRoot = "",
    [int]$Port = 9909,
    [switch]$KeepWorkDir
)

$ErrorActionPreference = "Stop"

Add-Type -AssemblyName System.IO.Compression.FileSystem

function Test-TcpPortOpen {
    param([int]$Port)
    $client = [System.Net.Sockets.TcpClient]::new()
    try {
        $async = $client.BeginConnect("127.0.0.1", $Port, $null, $null)
        if (-not $async.AsyncWaitHandle.WaitOne(300)) {
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

function Invoke-EcoreXJson {
    param(
        [Parameter(Mandatory = $true)][string]$Url,
        [int]$TimeoutSec = 20
    )
    return Invoke-RestMethod -UseBasicParsing -Uri $Url -TimeoutSec $TimeoutSec
}

function Wait-WebUiReady {
    param(
        [Parameter(Mandatory = $true)][string]$BaseUrl,
        [int]$TimeoutSeconds = 90
    )
    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    do {
        try {
            $payload = Invoke-EcoreXJson -Url ($BaseUrl.TrimEnd("/") + "/api/version") -TimeoutSec 3
            if ($payload) {
                return $payload
            }
        } catch {
        }
        Start-Sleep -Seconds 1
    } while ([DateTime]::UtcNow -lt $deadline)
    throw "EcoreX WebUI did not become ready at $BaseUrl"
}

function Stop-IsolatedProcesses {
    param([Parameter(Mandatory = $true)][string]$Root)
    $currentPid = $PID
    try {
        Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
            Where-Object {
                $_.ProcessId -ne $currentPid `
                    -and $_.CommandLine `
                    -and $_.CommandLine.IndexOf($Root, [StringComparison]::OrdinalIgnoreCase) -ge 0
            } |
            ForEach-Object {
                Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
            }
    } catch {
    }
}

function Expand-ZipToDirectory {
    param(
        [Parameter(Mandatory = $true)][string]$ZipPath,
        [Parameter(Mandatory = $true)][string]$Destination
    )
    if (Test-Path -LiteralPath $Destination) {
        Remove-Item -LiteralPath $Destination -Recurse -Force
    }
    New-Item -ItemType Directory -Force -Path $Destination | Out-Null
    [System.IO.Compression.ZipFile]::ExtractToDirectory((Resolve-Path -LiteralPath $ZipPath).Path, $Destination)
}

function Get-StaticUpdateMarkers {
    param([Parameter(Mandatory = $true)][string]$ExtractRoot)
    $assetText = ""
    $assetRoot = Join-Path $ExtractRoot "runtime\channel\web\static\app\assets"
    if (Test-Path -LiteralPath $assetRoot -PathType Container) {
        foreach ($file in Get-ChildItem -LiteralPath $assetRoot -Filter "*.js" -ErrorAction SilentlyContinue) {
            try {
                $assetText += [System.IO.File]::ReadAllText($file.FullName)
            } catch {
            }
        }
    }
    return [ordered]@{
        pollsUpdateCheck = $assetText.Contains("/api/update-check")
        rendersUpdateText = $assetText.Contains("runtimeUpdateCheck")
        opensUpdatePage = $assetText.Contains("openRuntimeUpdatePage")
        assetBytes = [Text.Encoding]::UTF8.GetByteCount($assetText)
    }
}

function Get-CurrentRuntimeInfo {
    param([Parameter(Mandatory = $true)][string]$StateDir)
    $currentRuntimePath = Join-Path $StateDir "current-runtime.txt"
    $runtimeDir = ""
    if (Test-Path -LiteralPath $currentRuntimePath) {
        $runtimeDir = ((Get-Content -LiteralPath $currentRuntimePath -ErrorAction Stop | Select-Object -First 1) -as [string]).Trim()
    }
    $configPath = if ($runtimeDir) { Join-Path $runtimeDir "config.json" } else { "" }
    $config = $null
    if ($configPath -and (Test-Path -LiteralPath $configPath)) {
        $config = Get-Content -Raw -LiteralPath $configPath | ConvertFrom-Json
    }
    $urlFile = Join-Path $StateDir "ecorex-webui.url"
    $url = ""
    if (Test-Path -LiteralPath $urlFile) {
        $url = (Get-Content -Raw -LiteralPath $urlFile).Trim()
    }
    return [ordered]@{
        currentRuntimePath = $currentRuntimePath
        currentRuntime = $runtimeDir
        currentRuntimeLeaf = if ($runtimeDir) { Split-Path -Leaf $runtimeDir } else { "" }
        currentRuntimeExists = [bool]($runtimeDir -and (Test-Path -LiteralPath $runtimeDir -PathType Container))
        configPath = $configPath
        port = if ($config -and $config.web_port) { [int]$config.web_port } else { 9909 }
        urlFile = $urlFile
        urlFileExists = Test-Path -LiteralPath $urlFile
        url = $url
    }
}

function Add-Check {
    param(
        [System.Collections.ArrayList]$Checks,
        [string]$Version,
        [string]$Name,
        [bool]$Ok,
        $Detail = @{}
    )
    [void]$Checks.Add([ordered]@{
        version = $Version
        name = $Name
        status = if ($Ok) { "PASS" } else { "FAIL" }
        detail = $Detail
    })
}

$started = Get-Date
$checks = [System.Collections.ArrayList]::new()
$results = [System.Collections.ArrayList]::new()
$repoRoot = (Resolve-Path ".").Path
$artifactRoot = Join-Path $repoRoot $ArtifactDir
$outputResolved = Join-Path $repoRoot $OutputPath
if (-not $WorkRoot) {
    $WorkRoot = Join-Path ([System.IO.Path]::GetPathRoot($repoRoot)) "ecx-upgrade-smoke"
}
$workRootResolved = [System.IO.Path]::GetFullPath($WorkRoot)
New-Item -ItemType Directory -Force -Path $workRootResolved | Out-Null
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $outputResolved) | Out-Null

if (Test-TcpPortOpen -Port $Port) {
    Add-Check -Checks $checks -Version "preflight" -Name "configured port $Port is free" -Ok $false -Detail @{ errorType = "configured_port_busy"; port = $Port }
} else {
    Add-Check -Checks $checks -Version "preflight" -Name "configured port $Port is free" -Ok $true -Detail @{ port = $Port }
}

foreach ($legacyVersion in $LegacyVersions) {
    $testRoot = Join-Path $workRootResolved (("v" + ($legacyVersion -replace "[^0-9A-Za-z]+", "") + "-" + [Guid]::NewGuid().ToString("N").Substring(0, 8)))
    $extractRoot = Join-Path $testRoot "legacy-package"
    $localAppData = Join-Path $testRoot "LocalAppData"
    $userProfile = Join-Path $testRoot "UserProfile"
    $stateDir = Join-Path $localAppData "EcoreX WebUI\state"
    $oldLocalAppData = $env:LOCALAPPDATA
    $oldUserProfile = $env:USERPROFILE
    $oldUpdateMode = $env:ECOREX_UPDATE_MODE
    $row = [ordered]@{
        legacyVersion = $legacyVersion
        targetVersion = $TargetVersion
        testRoot = $testRoot
        status = "FAIL"
    }
    try {
        if (Test-TcpPortOpen -Port $Port) {
            throw "default_port_busy_before_${legacyVersion}"
        }
        New-Item -ItemType Directory -Force -Path $localAppData, $userProfile | Out-Null
        $env:LOCALAPPDATA = $localAppData
        $env:USERPROFILE = $userProfile
        Remove-Item Env:\ECOREX_UPDATE_MODE -ErrorAction SilentlyContinue

        $legacyZip = Join-Path $artifactRoot "EcoreX_${legacyVersion}-webui-windows-x64.zip"
        if (-not (Test-Path -LiteralPath $legacyZip)) {
            throw "missing legacy package: $legacyZip"
        }
        Expand-ZipToDirectory -ZipPath $legacyZip -Destination $extractRoot
        $legacyInstaller = Get-ChildItem -LiteralPath $extractRoot -Recurse -Filter "install-ecorex-webui-win.ps1" | Select-Object -First 1
        if (-not $legacyInstaller) {
            throw "legacy installer missing in $legacyZip"
        }
        $staticMarkers = Get-StaticUpdateMarkers -ExtractRoot $extractRoot
        Add-Check -Checks $checks -Version $legacyVersion -Name "legacy renderer includes update-check polling hook" -Ok ([bool]$staticMarkers.pollsUpdateCheck) -Detail $staticMarkers

        & powershell -NoProfile -ExecutionPolicy Bypass -File $legacyInstaller.FullName -Port $Port -NoBrowser
        if ($LASTEXITCODE -ne 0) {
            throw "legacy installer exited with $LASTEXITCODE"
        }
        $legacyBase = "http://127.0.0.1:$Port"
        $legacyVersionPayload = Wait-WebUiReady -BaseUrl $legacyBase
        $updatePayload = Invoke-EcoreXJson -Url ($legacyBase + "/api/update-check?platform=win32") -TimeoutSec 30
        $artifact = $updatePayload.artifact
        $noticeOk = [bool](
            $updatePayload.status -eq "success" `
                -and $updatePayload.hasUpdate -eq $true `
                -and [string]$updatePayload.currentVersion -eq $legacyVersion `
                -and [string]$updatePayload.latestVersion -eq $TargetVersion `
                -and [string]$updatePayload.updateReason -eq "version" `
                -and $artifact `
                -and [string]$artifact.id -eq "webui-windows-x64"
        )
        Add-Check -Checks $checks -Version $legacyVersion -Name "legacy runtime receives v0.2.8 update notification" -Ok $noticeOk -Detail @{
            currentVersion = $updatePayload.currentVersion
            latestVersion = $updatePayload.latestVersion
            hasUpdate = $updatePayload.hasUpdate
            updateReason = $updatePayload.updateReason
            artifactId = if ($artifact) { $artifact.id } else { "" }
            artifactSha256 = if ($artifact) { $artifact.sha256 } else { "" }
            message = $updatePayload.message
        }

        $onlineInstaller = Join-Path $testRoot "install-webui.ps1"
        Invoke-WebRequest -UseBasicParsing -Uri ($BaseUrl.TrimEnd("/") + "/install-webui.ps1") -OutFile $onlineInstaller -TimeoutSec 60
        $env:ECOREX_UPDATE_MODE = "background"
        & powershell -NoProfile -ExecutionPolicy Bypass -File $onlineInstaller -Version $TargetVersion -BaseUrl $BaseUrl -Port $Port -NoBrowser
        if ($LASTEXITCODE -ne 0) {
            throw "online installer exited with $LASTEXITCODE"
        }

        $runtimeInfo = Get-CurrentRuntimeInfo -StateDir $stateDir
        $upgradedBase = "http://127.0.0.1:$($runtimeInfo.port)"
        $upgradedVersionPayload = Wait-WebUiReady -BaseUrl $upgradedBase
        $sameUrlRefreshPayload = $null
        try {
            $sameUrlRefreshPayload = Wait-WebUiReady -BaseUrl $legacyBase -TimeoutSeconds 30
        } catch {
            $sameUrlRefreshPayload = $null
        }
        $updateStatePath = Join-Path $stateDir "update-state.json"
        $updateState = @{}
        if (Test-Path -LiteralPath $updateStatePath) {
            $updateState = Get-Content -Raw -LiteralPath $updateStatePath | ConvertFrom-Json
        }
        $sameUrlRefreshOk = [bool](
            [int]$runtimeInfo.port -eq [int]$Port `
                -and $sameUrlRefreshPayload `
                -and [string]$sameUrlRefreshPayload.version -eq $TargetVersion
        )
        $upgradeOk = [bool](
            [string]$upgradedVersionPayload.version -eq $TargetVersion `
                -and $runtimeInfo.currentRuntimeExists `
                -and [string]$runtimeInfo.currentRuntimeLeaf -like "runtime-$TargetVersion-*" `
                -and $runtimeInfo.urlFileExists `
                -and [string]$updateState.status -eq "installed" `
                -and [string]$updateState.mode -eq "background" `
                -and $sameUrlRefreshOk
        )
        Add-Check -Checks $checks -Version $legacyVersion -Name "legacy runtime upgrades online and refreshes in place" -Ok $upgradeOk -Detail @{
            upgradedVersion = $upgradedVersionPayload.version
            upgradedBaseUrl = $upgradedBase
            originalBaseUrl = $legacyBase
            preferredPort = $Port
            upgradedPort = $runtimeInfo.port
            sameUrlRefreshVersion = if ($sameUrlRefreshPayload) { $sameUrlRefreshPayload.version } else { "" }
            sameUrlRefreshOk = $sameUrlRefreshOk
            runtimeLeaf = $runtimeInfo.currentRuntimeLeaf
            urlFileExists = $runtimeInfo.urlFileExists
            updateStateStatus = $updateState.status
            updateStateMode = $updateState.mode
            browserAction = $updateState.browserAction
        }

        $row.status = if ($noticeOk -and $upgradeOk) { "PASS" } else { "FAIL" }
        $row.legacyVersionPayload = @{
            version = $legacyVersionPayload.version
            url = $legacyVersionPayload.url
        }
        $row.updateCheck = @{
            currentVersion = $updatePayload.currentVersion
            latestVersion = $updatePayload.latestVersion
            hasUpdate = $updatePayload.hasUpdate
            updateReason = $updatePayload.updateReason
            artifactId = if ($artifact) { $artifact.id } else { "" }
            artifactSha256 = if ($artifact) { $artifact.sha256 } else { "" }
        }
        $row.upgrade = @{
            version = $upgradedVersionPayload.version
            runtime = $runtimeInfo
            sameUrlRefreshVersion = if ($sameUrlRefreshPayload) { $sameUrlRefreshPayload.version } else { "" }
            sameUrlRefreshOk = $sameUrlRefreshOk
            updateStateStatus = $updateState.status
            updateStateMode = $updateState.mode
        }
    } catch {
        $row.errorType = $_.Exception.GetType().Name
        $row.error = $_.Exception.Message
        Add-Check -Checks $checks -Version $legacyVersion -Name "legacy online upgrade scenario completes" -Ok $false -Detail @{
            errorType = $_.Exception.GetType().Name
            message = $_.Exception.Message
        }
    } finally {
        $env:LOCALAPPDATA = $oldLocalAppData
        $env:USERPROFILE = $oldUserProfile
        if ($null -eq $oldUpdateMode) {
            Remove-Item Env:\ECOREX_UPDATE_MODE -ErrorAction SilentlyContinue
        } else {
            $env:ECOREX_UPDATE_MODE = $oldUpdateMode
        }
        Stop-IsolatedProcesses -Root $testRoot
        Start-Sleep -Seconds 1
        Stop-IsolatedProcesses -Root $testRoot
        if (-not $KeepWorkDir) {
            Remove-Item -LiteralPath $testRoot -Recurse -Force -ErrorAction SilentlyContinue
        }
        [void]$results.Add($row)
    }
}

$failures = @($checks | Where-Object { $_.status -ne "PASS" })
$report = [ordered]@{
    schemaVersion = "ecorex.v0.2.8.legacy-webui-online-upgrade.v1"
    status = if ($failures.Count -eq 0) { "PASS" } else { "FAIL" }
    startedAt = $started.ToUniversalTime().ToString("o")
    finishedAt = (Get-Date).ToUniversalTime().ToString("o")
    durationSeconds = [Math]::Round(((Get-Date) - $started).TotalSeconds, 2)
    baseUrl = $BaseUrl
    targetVersion = $TargetVersion
    legacyVersions = $LegacyVersions
    checkCount = $checks.Count
    passCount = @($checks | Where-Object { $_.status -eq "PASS" }).Count
    failCount = $failures.Count
    checks = $checks
    results = $results
}

$json = $report | ConvertTo-Json -Depth 16
[System.IO.File]::WriteAllText($outputResolved, $json + [Environment]::NewLine, [System.Text.UTF8Encoding]::new($false))
$summary = [ordered]@{
    status = $report.status
    artifact = $outputResolved
    checkCount = $report.checkCount
    passCount = $report.passCount
    failCount = $report.failCount
    durationSeconds = $report.durationSeconds
}
$summary | ConvertTo-Json -Depth 6
if ($report.status -ne "PASS") {
    exit 1
}
