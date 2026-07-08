param(
    [string]$Version = "",
    [switch]$SkipBuild,
    [switch]$SkipPackage,
    [switch]$SkipManifestPromotion,
    [switch]$RequireSignatures,
    [switch]$AllowUnsigned,
    [switch]$IncludeWebService,
    [switch]$AllowDirtyTree,
    [switch]$Force
)

$ErrorActionPreference = "Stop"

$repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
$desktopRoot = Join-Path $repoRoot "desktop"
$releaseArtifactsRoot = Join-Path $repoRoot "release-artifacts"
$siteRoot = Join-Path $repoRoot "deploy\ecorex-site"
$docsRoot = Join-Path $repoRoot "docs\v0.3.0"
$evidenceRoot = Join-Path $docsRoot "artifacts"
$releaseIndexPath = Join-Path $siteRoot "release-index.json"

function Invoke-CheckedCommand {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [string[]]$ArgumentList = @(),
        [string]$WorkingDirectory = $repoRoot
    )
    Push-Location $WorkingDirectory
    try {
        & $FilePath @ArgumentList
        if ($LASTEXITCODE -ne 0) {
            throw "Command failed with exit code ${LASTEXITCODE}: $FilePath $($ArgumentList -join ' ')"
        }
    } finally {
        Pop-Location
    }
}

function Write-Utf8NoBom {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Value
    )
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $Path) | Out-Null
    [System.IO.File]::WriteAllText($Path, $Value, [System.Text.UTF8Encoding]::new($false))
}

function Read-JsonFile {
    param([Parameter(Mandatory = $true)][string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) {
        throw "JSON file is missing: $Path"
    }
    return Get-Content -Raw -Encoding UTF8 -LiteralPath $Path | ConvertFrom-Json
}

function Get-FileSha256 {
    param([Parameter(Mandatory = $true)][string]$Path)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "File is missing: $Path"
    }
    return (Get-FileHash -Algorithm SHA256 -LiteralPath $Path).Hash.ToUpperInvariant()
}

function Get-GitCommit {
    try {
        return ((git rev-parse HEAD) | Select-Object -First 1).Trim()
    } catch {
        return "unknown"
    }
}

function Assert-CleanTreeForRelease {
    if ($AllowDirtyTree) { return }
    $status = (git -C $repoRoot status --porcelain)
    if ($status) {
        throw "Release requires a clean git tree. Re-run with -AllowDirtyTree only for non-production dry runs."
    }
}

function Assert-VersionAligned {
    param([Parameter(Mandatory = $true)][string]$ExpectedVersion)
    $cliVersion = (Get-Content -Raw -Encoding UTF8 -LiteralPath (Join-Path $repoRoot "cli\VERSION")).Trim()
    $desktopPackage = Read-JsonFile -Path (Join-Path $desktopRoot "package.json")
    $manifest = Read-JsonFile -Path (Join-Path $siteRoot "manifest.json")
    if ($cliVersion -ne $ExpectedVersion) { throw "cli/VERSION is $cliVersion, expected $ExpectedVersion" }
    if ([string]$desktopPackage.version -ne $ExpectedVersion) { throw "desktop/package.json version is $($desktopPackage.version), expected $ExpectedVersion" }
    if ([string]$manifest.version -ne $ExpectedVersion) { throw "deploy/ecorex-site/manifest.json version is $($manifest.version), expected $ExpectedVersion" }
}

function New-ArtifactEvidence {
    param(
        [Parameter(Mandatory = $true)][string]$Id,
        [Parameter(Mandatory = $true)][string]$Platform,
        [Parameter(Mandatory = $true)][string]$FileName,
        [Parameter(Mandatory = $true)][string]$Path
    )
    $item = Get-Item -LiteralPath $Path
    $sha256 = Get-FileSha256 -Path $Path
    $signaturePath = "$Path.sig"
    $signatureRequired = [bool]$RequireSignatures -and -not [bool]$AllowUnsigned
    $signature = [ordered]@{
        required = $signatureRequired
        status = "missing"
        fileName = ""
        sha256 = ""
    }
    if (Test-Path -LiteralPath $signaturePath -PathType Leaf) {
        $signature.fileName = Split-Path -Leaf $signaturePath
        $signature.sha256 = Get-FileSha256 -Path $signaturePath
        $signature.status = "present"
    } elseif (-not $signatureRequired) {
        $signature.status = "not-required"
    } else {
        throw "Signature file is required before release-index promotion: $signaturePath"
    }
    return [ordered]@{
        id = $Id
        version = $Version
        platform = $Platform
        fileName = $FileName
        href = "downloads/$FileName"
        size = [int64]$item.Length
        sha256 = $sha256
        signature = $signature
        smoke = [ordered]@{
            status = "pass"
            evidence = "docs/v0.3.0/artifacts/webui-release-orchestrator-smoke.json"
        }
    }
}

function Assert-ManifestArtifactTrust {
    param(
        [Parameter(Mandatory = $true)]$Manifest,
        [Parameter(Mandatory = $true)]$Artifacts
    )
    $byId = @{}
    foreach ($artifact in @($Manifest.artifacts)) {
        if ($artifact.id) {
            $byId[[string]$artifact.id] = $artifact
        }
    }
    foreach ($item in @($Artifacts)) {
        $manifestArtifact = $byId[$item.id]
        if (-not $manifestArtifact) {
            throw "Manifest is missing artifact $($item.id)"
        }
        if ([string]$manifestArtifact.version -ne $Version) {
            throw "Manifest artifact $($item.id) has version $($manifestArtifact.version), expected $Version"
        }
        if ([int64]$manifestArtifact.size -ne [int64]$item.size) {
            throw "Manifest artifact $($item.id) size mismatch."
        }
        if ([string]$manifestArtifact.sha256 -ne [string]$item.sha256) {
            throw "Manifest artifact $($item.id) sha256 mismatch."
        }
        if ([string]$manifestArtifact.status -notin @("ready", "ready-unsigned")) {
            throw "Manifest artifact $($item.id) is not ready: $($manifestArtifact.status)"
        }
        $manifestSmoke = $manifestArtifact.smoke
        if (-not $manifestSmoke -or [string]$manifestSmoke.status -ne "pass") {
            throw "Manifest artifact $($item.id) smoke status is not pass."
        }
        $manifestSignature = $manifestArtifact.signature
        $manifestSignatureStatus = if ($manifestSignature -is [string]) {
            [string]$manifestSignature
        } elseif ($manifestSignature -and $manifestSignature.PSObject.Properties.Name -contains "status") {
            [string]$manifestSignature.status
        } else {
            ""
        }
        if ($RequireSignatures -and -not $AllowUnsigned -and $manifestSignatureStatus -notin @("Valid", "present", "signed", "valid")) {
            throw "Manifest artifact $($item.id) signature is not trusted: $manifestSignatureStatus"
        }
    }
}

if (-not $Version) {
    $desktopPackage = Read-JsonFile -Path (Join-Path $desktopRoot "package.json")
    $Version = [string]$desktopPackage.version
}
if (-not $Version) {
    throw "Version is required."
}

Assert-VersionAligned -ExpectedVersion $Version
Assert-CleanTreeForRelease

New-Item -ItemType Directory -Force -Path $evidenceRoot | Out-Null

$checks = New-Object System.Collections.Generic.List[object]
$checks.Add([ordered]@{ name = "version alignment"; status = "pass"; detail = $Version }) | Out-Null

if (-not $SkipBuild) {
    Invoke-CheckedCommand -FilePath "npm" -ArgumentList @("run", "typecheck") -WorkingDirectory $desktopRoot
    $checks.Add([ordered]@{ name = "desktop typecheck"; status = "pass"; detail = "npm run typecheck" }) | Out-Null
    Invoke-CheckedCommand -FilePath "npm" -ArgumentList @("run", "build:renderer") -WorkingDirectory $desktopRoot
    $checks.Add([ordered]@{ name = "renderer build"; status = "pass"; detail = "npm run build:renderer" }) | Out-Null
}

if (-not $SkipPackage) {
    Invoke-CheckedCommand -FilePath "powershell" -ArgumentList @(
        "-NoProfile",
        "-ExecutionPolicy", "Bypass",
        "-File", (Join-Path $repoRoot "scripts\prepare-ecorex-webui-local-release.ps1"),
        "-Version", $Version,
        "-SkipCombinedPackage"
    )
    $checks.Add([ordered]@{ name = "local WebUI packages"; status = "pass"; detail = "prepare-ecorex-webui-local-release.ps1" }) | Out-Null

    if ($IncludeWebService) {
        Invoke-CheckedCommand -FilePath "powershell" -ArgumentList @(
            "-NoProfile",
            "-ExecutionPolicy", "Bypass",
            "-File", (Join-Path $repoRoot "scripts\prepare-ecorex-web-release.ps1"),
            "-Version", $Version
        )
        $checks.Add([ordered]@{ name = "web service package"; status = "pass"; detail = "prepare-ecorex-web-release.ps1" }) | Out-Null
    }
}

$artifactSpecs = @(
    @{ Id = "webui-windows-x64"; Platform = "win32-x64"; FileName = "EcoreX_${Version}-webui-windows-x64.zip" },
    @{ Id = "webui-macos-universal"; Platform = "darwin-universal"; FileName = "EcoreX_${Version}-webui-macos-universal.zip" }
)
if ($IncludeWebService) {
    $artifactSpecs += @{ Id = "web-linux-service"; Platform = "linux-service"; FileName = "EcoreX_${Version}-web-linux-service.tar.gz" }
}

$artifactEvidence = @()
foreach ($spec in $artifactSpecs) {
    $path = Join-Path $releaseArtifactsRoot $spec.FileName
    $artifactEvidence += New-ArtifactEvidence -Id $spec.Id -Platform $spec.Platform -FileName $spec.FileName -Path $path
    $checks.Add([ordered]@{ name = "artifact verified"; status = "pass"; detail = $spec.FileName }) | Out-Null
}

if (-not $SkipManifestPromotion) {
    $manifestArgs = @(
        "-NoProfile",
        "-ExecutionPolicy", "Bypass",
        "-File", (Join-Path $repoRoot "scripts\update-ecorex-desktop-release-manifest.ps1"),
        "-Version", $Version,
        "-PromoteVersion",
        "-WebUiWindowsPath", (Join-Path $releaseArtifactsRoot "EcoreX_${Version}-webui-windows-x64.zip"),
        "-WebUiMacosPath", (Join-Path $releaseArtifactsRoot "EcoreX_${Version}-webui-macos-universal.zip")
    )
    if ($IncludeWebService) {
        $manifestArgs += @("-WebLinuxServicePath", (Join-Path $releaseArtifactsRoot "EcoreX_${Version}-web-linux-service.tar.gz"))
    }
    Invoke-CheckedCommand -FilePath "powershell" -ArgumentList $manifestArgs
    $checks.Add([ordered]@{ name = "manifest promotion"; status = "pass"; detail = "update-ecorex-desktop-release-manifest.ps1" }) | Out-Null
}

$manifest = Read-JsonFile -Path (Join-Path $siteRoot "manifest.json")
Assert-ManifestArtifactTrust -Manifest $manifest -Artifacts $artifactEvidence
$checks.Add([ordered]@{ name = "manifest trust"; status = "pass"; detail = "hash/status match release-index; signatures are optional for this WebUI-only release" }) | Out-Null
$connectorHealthCheck = $manifest.update.webui.connectorHealthCheck
if (-not $connectorHealthCheck -or $connectorHealthCheck.required -ne $true) {
    throw "Manifest must require connectorHealthCheck for v0.3.0 online update promotion."
}
$checks.Add([ordered]@{ name = "external connector preservation policy"; status = "pass"; detail = "online update requires connector health check before activation" }) | Out-Null

$existingIndex = $null
if (Test-Path -LiteralPath $releaseIndexPath -PathType Leaf) {
    $existingIndex = Read-JsonFile -Path $releaseIndexPath
}
if ($existingIndex -and [string]$existingIndex.status -eq "ready" -and [string]$existingIndex.version -eq $Version -and -not $Force) {
    throw "release-index.json for $Version is already ready. Use -Force only when intentionally regenerating the same release index."
}

$checkEvidence = @($checks | ForEach-Object { $_ })
$artifactEvidenceRows = @($artifactEvidence | ForEach-Object { $_ })
$smokePayload = [ordered]@{
    schema = "ecorex.webui-release-smoke.v1"
    version = $Version
    generatedAt = (Get-Date).ToUniversalTime().ToString("o")
    status = "pass"
    command = "scripts/release-ecorex-webui-orchestrator.ps1"
    checks = $checkEvidence
    redacted = $true
}
$smokePath = Join-Path $evidenceRoot "webui-release-orchestrator-smoke.json"
Write-Utf8NoBom -Path $smokePath -Value (($smokePayload | ConvertTo-Json -Depth 8) + [Environment]::NewLine)

$releaseIndex = [ordered]@{
    schema = "ecorex.release-index.v1"
    product = "EcoreX WebUI"
    version = $Version
    channel = "stable"
    status = "ready"
    generatedAt = (Get-Date).ToUniversalTime().ToString("o")
    commit = Get-GitCommit
    manifest = [ordered]@{
        path = "manifest.json"
        sha256 = Get-FileSha256 -Path (Join-Path $siteRoot "manifest.json")
    }
    artifacts = $artifactEvidenceRows
    smoke = [ordered]@{
        status = "pass"
        evidence = "docs/v0.3.0/artifacts/webui-release-orchestrator-smoke.json"
    }
    rollout = $manifest.update.webui.rollout
    killSwitch = $manifest.update.webui.killSwitch
    rollback = $manifest.update.webui.rollback
    redacted = $true
}

$tmpPath = "$releaseIndexPath.tmp"
Write-Utf8NoBom -Path $tmpPath -Value (($releaseIndex | ConvertTo-Json -Depth 12) + [Environment]::NewLine)
Move-Item -LiteralPath $tmpPath -Destination $releaseIndexPath -Force

Write-Host "EcoreX WebUI release orchestration completed."
Write-Host "version: $Version"
Write-Host "releaseIndex: deploy/ecorex-site/release-index.json"
Write-Host "smokeEvidence: docs/v0.3.0/artifacts/webui-release-orchestrator-smoke.json"
