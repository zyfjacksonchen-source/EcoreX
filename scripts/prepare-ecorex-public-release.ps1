param(
    [string]$Version = "",
    [string]$SiteRoot = "deploy/ecorex-site",
    [string]$AdminApiRoot = "deploy/ecorex-admin-api",
    [string]$InstallerPath = "",
    [string]$InstallerIa32Path = "",
    [string]$MacArm64DmgPath = "",
    [string]$MacX64DmgPath = "",
    [string]$WebTarballPath = "",
    [string]$OutputDir = "release-artifacts",
    [string[]]$DownloadBaseUrls = @(),
    [string[]]$AssetDownloadBaseUrls = @(),
    [string]$GitHubReleaseMirrorUrl = "",
    [switch]$KeepStaging,
    [switch]$SkipValidation,
    [switch]$ExternalizeDownloads,
    [switch]$EmbedDownloads
)

$ErrorActionPreference = "Stop"

Write-Warning "This script prepares the internal/site public-release bundle and is NOT safe for the open installer-only GitHub repository. Use scripts/prepare-ecorex-installer-repo.ps1 for the public GitHub installer repo; that repo must not contain source code."

if ($ExternalizeDownloads -and $EmbedDownloads) {
    throw "Use either -ExternalizeDownloads or -EmbedDownloads, not both."
}

function Resolve-RequiredPath {
    param([Parameter(Mandatory = $true)][string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) {
        throw "Required path does not exist: $Path"
    }
    return (Resolve-Path -LiteralPath $Path).Path
}

function Resolve-UnderDirectory {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Base
    )
    $resolvedBase = [System.IO.Path]::GetFullPath($Base).TrimEnd(
        [System.IO.Path]::DirectorySeparatorChar,
        [System.IO.Path]::AltDirectorySeparatorChar
    )
    $resolvedPath = [System.IO.Path]::GetFullPath($Path)
    $baseWithSeparator = $resolvedBase + [System.IO.Path]::DirectorySeparatorChar
    if ($resolvedPath -ne $resolvedBase -and -not $resolvedPath.StartsWith($baseWithSeparator, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Resolved path '$resolvedPath' is outside '$resolvedBase'"
    }
    return $resolvedPath
}

function Write-Utf8NoBom {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Value
    )
    $encoding = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($Path, $Value, $encoding)
}

function Set-JsonObjectProperty {
    param(
        [Parameter(Mandatory = $true)][object]$Object,
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)]$Value
    )
    if ($Object.PSObject.Properties.Name -contains $Name) {
        $Object.PSObject.Properties[$Name].Value = $Value
    } else {
        Add-Member -InputObject $Object -NotePropertyName $Name -NotePropertyValue $Value -Force
    }
}

function Add-DownloadBaseUrl {
    param(
        [Parameter(Mandatory = $true)][AllowEmptyCollection()][System.Collections.ArrayList]$List,
        [string]$Url
    )
    $clean = ([string]$Url).Trim().TrimEnd("/")
    if (-not $clean -or $clean -notmatch '^https?://') {
        return
    }
    if (-not $List.Contains($clean)) {
        [void]$List.Add($clean)
    }
}

function Add-DownloadBaseUrls {
    param(
        [Parameter(Mandatory = $true)][AllowEmptyCollection()][System.Collections.ArrayList]$List,
        $Values
    )
    foreach ($value in @($Values)) {
        if ($null -eq $value) { continue }
        foreach ($part in ([string]$value -split "[,;`r`n]+")) {
            Add-DownloadBaseUrl -List $List -Url $part
        }
    }
}

function Get-ConfiguredDownloadBaseUrls {
    $urls = New-Object System.Collections.ArrayList
    Add-DownloadBaseUrls -List $urls -Values $DownloadBaseUrls
    Add-DownloadBaseUrls -List $urls -Values $env:ECOREX_DOWNLOAD_BASE_URLS
    return $urls.ToArray([string])
}

function Add-DownloadMirror {
    param(
        [Parameter(Mandatory = $true)][AllowEmptyCollection()][System.Collections.ArrayList]$List,
        [string]$Id,
        [string]$Kind,
        [string]$BaseUrl,
        [string]$PathMode
    )
    $clean = ([string]$BaseUrl).Trim().TrimEnd("/")
    if (-not $clean -or $clean -notmatch '^https?://') {
        return
    }
    $mode = if (([string]$PathMode).Trim() -ieq "fileName") { "fileName" } else { "href" }
    foreach ($item in $List) {
        if ([string]$item.baseUrl -eq $clean -and [string]$item.pathMode -eq $mode) {
            return
        }
    }
    [void]$List.Add([ordered]@{
        id = $Id
        kind = $Kind
        baseUrl = $clean
        pathMode = $mode
    })
}

function Get-ConfiguredDownloadMirrors {
    $mirrors = New-Object System.Collections.ArrayList
    $assetBases = New-Object System.Collections.ArrayList
    Add-DownloadBaseUrls -List $assetBases -Values $GitHubReleaseMirrorUrl
    Add-DownloadBaseUrls -List $assetBases -Values $env:ECOREX_GITHUB_RELEASE_MIRROR_URL
    Add-DownloadBaseUrls -List $assetBases -Values $AssetDownloadBaseUrls
    Add-DownloadBaseUrls -List $assetBases -Values $env:ECOREX_DOWNLOAD_ASSET_BASE_URLS
    Add-DownloadBaseUrls -List $assetBases -Values "https://github.com/zhangyifanjackson-dotcom/EcoreX-installers/releases/download/v$Version"
    foreach ($base in $assetBases) {
        $kind = if ([string]$base -match 'github\.com/.+/releases/download/') { "github-release" } else { "asset-base" }
        $id = if ($kind -eq "github-release") { "github-release-v$Version" } else { "asset-mirror" }
        Add-DownloadMirror -List $mirrors -Id $id -Kind $kind -BaseUrl $base -PathMode "fileName"
    }
    foreach ($base in @(Get-ConfiguredDownloadBaseUrls)) {
        Add-DownloadMirror -List $mirrors -Id "path-mirror" -Kind "path-compatible" -BaseUrl $base -PathMode "href"
    }
    return $mirrors.ToArray()
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

function Get-ReleaseMigrationReadmeNote {
    $readmePath = Join-Path $repoRoot "desktop\build\README-migration.txt"
    if (-not (Test-Path -LiteralPath $readmePath)) {
        throw "Release migration README missing: $readmePath"
    }
    return (Get-Content -Raw -Encoding UTF8 -LiteralPath $readmePath).TrimEnd()
}

function Invoke-ReleaseTextSanitizer {
    param([Parameter(Mandatory = $true)][string]$Root)
    $sanitizer = Join-Path $repoRoot "scripts\sanitize-ecorex-release-runtime.py"
    if (-not (Test-Path -LiteralPath $sanitizer)) {
        throw "Release sanitizer missing: $sanitizer"
    }
    & python $sanitizer $Root
    if ($LASTEXITCODE -ne 0) {
        throw "Release text sanitizer failed for $Root"
    }
}

function Test-ExternalArtifact {
    param([Parameter(Mandatory = $true)][object]$Artifact)
    $href = [string]$Artifact.href
    return ([bool]$Artifact.external) -or
        $href.StartsWith("https://", [System.StringComparison]::OrdinalIgnoreCase) -or
        $href.StartsWith("http://", [System.StringComparison]::OrdinalIgnoreCase)
}

function Assert-ExternalArtifactMetadata {
    param([Parameter(Mandatory = $true)][object]$Artifact)
    $href = [string]$Artifact.href
    $fileName = [string]$Artifact.fileName
    $sha256 = [string]$Artifact.sha256
    $size = [int64]$Artifact.size
    if (-not $fileName) {
        throw "External artifact '$($Artifact.id)' has no fileName."
    }
    if (-not (
        $href.StartsWith("https://", [System.StringComparison]::OrdinalIgnoreCase) -or
        $href.StartsWith("http://", [System.StringComparison]::OrdinalIgnoreCase)
    )) {
        throw "External artifact '$($Artifact.id)' href must be an absolute HTTP(S) URL."
    }
    if ($size -le 0) {
        throw "External artifact '$($Artifact.id)' must have a positive size."
    }
    if (-not ($sha256 -match '^[A-Fa-f0-9]{64}$')) {
        throw "External artifact '$($Artifact.id)' must have a 64-character SHA256."
    }
}

$repoRoot = (Resolve-Path -LiteralPath ".").Path
$desktopPackagePath = Join-Path $repoRoot "desktop/package.json"
if (-not $Version) {
    if (-not (Test-Path -LiteralPath $desktopPackagePath)) {
        throw "Cannot infer version because desktop/package.json does not exist. Pass -Version explicitly."
    }
    $desktopPackage = Get-Content -Raw -Encoding UTF8 -LiteralPath $desktopPackagePath | ConvertFrom-Json
    $Version = [string]$desktopPackage.version
}
$downloadsExternalized = -not [bool]$EmbedDownloads
if (-not $InstallerPath) {
    $InstallerPath = Join-Path "desktop/release" "EcoreX_${Version}_x64-setup.exe"
}
if (-not $InstallerIa32Path) {
    $InstallerIa32Path = Join-Path "desktop/release" "EcoreX_${Version}_ia32-setup.exe"
}
$siteRootResolved = Resolve-RequiredPath $SiteRoot
$adminApiRootResolved = Resolve-RequiredPath $AdminApiRoot

$manifestPath = Join-Path $siteRootResolved "manifest.json"
$manifest = Get-Content -Raw -Encoding UTF8 -LiteralPath $manifestPath | ConvertFrom-Json
if ($manifest.version -ne $Version) {
    throw "Manifest version '$($manifest.version)' does not match expected '$Version'."
}

$outputResolved = [System.IO.Path]::GetFullPath((Join-Path $repoRoot $OutputDir))
New-Item -ItemType Directory -Force -Path $outputResolved | Out-Null

$artifactSources = @{}
$artifactSources["windows-x64"] = $InstallerPath
$artifactSources["windows-ia32"] = $InstallerIa32Path
$artifactSources["webui-win-mac"] = Join-Path "release-artifacts" "EcoreX_${Version}-webui-win-mac.zip"
$artifactSources["webui-windows-x64"] = Join-Path "release-artifacts" "EcoreX_${Version}-webui-windows-x64.zip"
$artifactSources["webui-macos-universal"] = Join-Path "release-artifacts" "EcoreX_${Version}-webui-macos-universal.zip"
$artifactSources["macos-arm64-dmg"] = if ($MacArm64DmgPath) { $MacArm64DmgPath } else { Join-Path "desktop/release" "EcoreX_${Version}_arm64.dmg" }
$artifactSources["macos-x64-dmg"] = if ($MacX64DmgPath) { $MacX64DmgPath } else { Join-Path "desktop/release" "EcoreX_${Version}_x64.dmg" }
$artifactSources["web-linux-service"] = if ($WebTarballPath) { $WebTarballPath } else { Join-Path "release-artifacts" "EcoreX_${Version}-web-linux-service.tar.gz" }

$requiredAuthNegativeStatuses = @(
    "messageNoToken",
    "messageWrongToken",
    "messageQueryTokenRejected",
    "streamNoToken",
    "streamWrongToken",
    "streamQueryTokenRejected",
    "fileStatNoToken",
    "fileStatWrongToken",
    "fileServeNoToken",
    "fileServeWrongToken",
    "openPathNoToken",
    "openPathWrongToken"
)

function Test-PublishableArtifact($Artifact) {
    $status = [string]$Artifact.status
    $id = [string]$Artifact.id
    if ($status -eq "ready" -and $id.StartsWith("macos-") -and [string]$Artifact.signature -eq "unsigned") {
        throw "macOS unsigned artifact '$id' must use status=ready-unsigned with installSmoke evidence."
    }
    if ($status -eq "ready") {
        return $true
    }
    return ($status -eq "ready-unsigned" -and $id.StartsWith("macos-") -and [string]$Artifact.signature -eq "unsigned")
}

function Get-InstallSmoke($Artifact) {
    if ($Artifact.PSObject.Properties.Name -contains "installSmoke") {
        return $Artifact.installSmoke
    }
    if ($Artifact.PSObject.Properties.Name -contains "install_smoke") {
        return $Artifact.install_smoke
    }
    return $null
}

function Get-SmokeEvidence($Smoke) {
    foreach ($name in @("runId", "run_id", "evidenceUrl", "evidence_url", "evidence")) {
        if ($Smoke.PSObject.Properties.Name -contains $name) {
            $value = [string]$Smoke.PSObject.Properties[$name].Value
            if ($value.Trim()) {
                return $value
            }
        }
    }
    return ""
}

function Assert-AuthNegativeStatuses {
    param(
        [Parameter(Mandatory = $true)][object]$Smoke,
        [Parameter(Mandatory = $true)][string]$EvidenceName
    )
    if (-not ($Smoke.PSObject.Properties.Name -contains "authNegativeStatuses")) {
        throw "$EvidenceName requires authNegativeStatuses."
    }
    $statuses = $Smoke.authNegativeStatuses
    if (-not $statuses) {
        throw "$EvidenceName requires authNegativeStatuses."
    }
    $missing = @()
    $bad = @()
    foreach ($name in $requiredAuthNegativeStatuses) {
        if (-not ($statuses.PSObject.Properties.Name -contains $name)) {
            $missing += $name
            continue
        }
        if ([int]$statuses.PSObject.Properties[$name].Value -ne 401) {
            $bad += "$name=$($statuses.PSObject.Properties[$name].Value)"
        }
    }
    if ($missing.Count -gt 0 -or $bad.Count -gt 0) {
        throw "$EvidenceName authNegativeStatuses invalid; missing=$($missing -join ', ') bad=$($bad -join ', ')."
    }
}

function Assert-WindowsInstalledSmoke {
    param([Parameter(Mandatory = $true)][object]$Artifact)
    $artifactId = [string]$Artifact.id
    $smokeFile = if ($artifactId -eq "windows-ia32") { "win-ia32-installed-smoke.json" } else { "win-installed-smoke.json" }
    $smokePath = Join-Path $repoRoot "docs\v$Version\$smokeFile"
    if (-not (Test-Path -LiteralPath $smokePath)) {
        throw "Windows artifact '$($Artifact.fileName)' requires installed smoke evidence: $smokePath."
    }
    $smoke = Get-Content -Raw -Encoding UTF8 -LiteralPath $smokePath | ConvertFrom-Json
    $requiredTrue = @(
        "installed",
        "appStarted",
        "sidecarReady",
        "authReady",
        "authRequired",
        "authNegativeReady",
        "cleaned"
    )
    $missing = @($requiredTrue | Where-Object { -not [bool]$smoke.$_ })
    if ($missing.Count -gt 0) {
        throw "$artifactId installed smoke missing passed flags: $($missing -join ', ')."
    }
    if ([string]$smoke.runtimeVersion -ne $Version) {
        throw "$artifactId installed smoke runtimeVersion '$($smoke.runtimeVersion)' must be $Version."
    }
    $expectedWinArch = if ($artifactId -eq "windows-ia32") { "ia32" } else { "x64" }
    $expectedPythonBits = if ($artifactId -eq "windows-ia32") { 32 } else { 64 }
    if ([string]$smoke.runtimeWinArch -ne $expectedWinArch) {
        throw "$artifactId installed smoke runtimeWinArch '$($smoke.runtimeWinArch)' must be $expectedWinArch."
    }
    if ([int]$smoke.runtimePythonBits -ne $expectedPythonBits) {
        throw "$artifactId installed smoke runtimePythonBits '$($smoke.runtimePythonBits)' must be $expectedPythonBits."
    }
    if ([string]$smoke.installerFileName -ne [string]$Artifact.fileName) {
        throw "$artifactId installed smoke fileName '$($smoke.installerFileName)' does not match manifest '$($Artifact.fileName)'."
    }
    if (([string]$smoke.installerSha256).ToUpperInvariant() -ne ([string]$Artifact.sha256).ToUpperInvariant()) {
        throw "$artifactId installed smoke sha256 '$($smoke.installerSha256)' does not match manifest '$($Artifact.sha256)'."
    }
    if ([int64]$smoke.installerSize -ne [int64]$Artifact.size) {
        throw "$artifactId installed smoke installerSize '$($smoke.installerSize)' does not match manifest '$($Artifact.size)'."
    }
    foreach ($name in @("installerSignatureStatus", "appSignatureStatus", "runtimePythonSignatureStatus")) {
        if ([string]$smoke.$name -ne "Valid") {
            throw "$artifactId installed smoke requires $name=Valid."
        }
    }
    Assert-AuthNegativeStatuses -Smoke $smoke -EvidenceName "$artifactId installed smoke"
}

function Assert-MacUnsignedInstallSmoke($Artifact) {
    $smoke = Get-InstallSmoke $Artifact
    if (-not $smoke) {
        throw "macOS ready-unsigned artifact '$($Artifact.id)' requires installSmoke evidence."
    }
    if ([string]$smoke.status -ne "pass") {
        throw "macOS ready-unsigned artifact '$($Artifact.id)' requires installSmoke.status=pass."
    }
    if ([string]$smoke.version -ne $Version) {
        throw "macOS ready-unsigned artifact '$($Artifact.id)' installSmoke.version must be $Version."
    }
    if (([string]$smoke.sha256).ToUpperInvariant() -ne ([string]$Artifact.sha256).ToUpperInvariant()) {
        throw "macOS ready-unsigned artifact '$($Artifact.id)' installSmoke.sha256 must match manifest sha256."
    }
    if (-not (Get-SmokeEvidence $smoke)) {
        throw "macOS ready-unsigned artifact '$($Artifact.id)' installSmoke requires runId or evidenceUrl."
    }
    $expectedArch = if ([string]$Artifact.id -eq "macos-arm64-dmg") { "arm64" } elseif ([string]$Artifact.id -eq "macos-x64-dmg") { "x64" } else { "" }
    if ($expectedArch) {
        $expectedName = "EcoreX_${Version}_${expectedArch}.dmg"
        if ([string]$Artifact.fileName -ne $expectedName) {
            throw "macOS ready-unsigned artifact '$($Artifact.id)' fileName must be $expectedName."
        }
        if ([string]$smoke.artifact -ne $expectedName) {
            throw "macOS ready-unsigned artifact '$($Artifact.id)' installSmoke.artifact must be $expectedName."
        }
        if ([string]$smoke.arch -ne $expectedArch) {
            throw "macOS ready-unsigned artifact '$($Artifact.id)' installSmoke.arch must be $expectedArch."
        }
        if ([int64]$smoke.bytes -ne [int64]$Artifact.size) {
            throw "macOS ready-unsigned artifact '$($Artifact.id)' installSmoke.bytes must match artifact size."
        }
    }
    $requiredTrue = @(
        "mounted",
        "appFound",
        "copied",
        "launched",
        "versionOk",
        "sidecarReady",
        "authReady",
        "authRequired",
        "authNegativeReady",
        "gatekeeperInstructionShown"
    )
    $missing = @($requiredTrue | Where-Object { -not [bool]$smoke.$_ })
    if ($missing.Count -gt 0) {
        throw "macOS ready-unsigned artifact '$($Artifact.id)' installSmoke missing passed flags: $($missing -join ', ')."
    }
    Assert-AuthNegativeStatuses -Smoke $smoke -EvidenceName "macOS ready-unsigned artifact '$($Artifact.id)' installSmoke"
    $instructions = ""
    foreach ($name in @("gatekeeperInstructions", "instructions", "instructionsUrl", "instructions_url")) {
        if ($smoke.PSObject.Properties.Name -contains $name) {
            $value = [string]$smoke.PSObject.Properties[$name].Value
            if ($value.Trim()) {
                $instructions = $value
                break
            }
        }
    }
    if (-not $instructions) {
        throw "macOS ready-unsigned artifact '$($Artifact.id)' installSmoke requires Gatekeeper instructions evidence."
    }
}

$readyArtifacts = @()
foreach ($artifact in $manifest.artifacts) {
    if (-not (Test-PublishableArtifact $artifact)) {
        continue
    }
    if ([string]$artifact.id -like "windows-*" -and ([string]$artifact.signature).ToLowerInvariant().Contains("unsigned")) {
        throw "Windows artifact cannot be published while manifest signature is '$($artifact.signature)'."
    }
    if ([string]$artifact.id -like "windows-*" -and [string]$artifact.signature -ne "Valid") {
        throw "Windows artifact '$($artifact.fileName)' requires manifest signature=Valid before publication."
    }
    if ([string]$artifact.id -like "windows-*") {
        Assert-WindowsInstalledSmoke $artifact
    }
    if ([string]$artifact.status -eq "ready-unsigned") {
        Assert-MacUnsignedInstallSmoke $artifact
    }

    if (Test-ExternalArtifact $artifact) {
        Assert-ExternalArtifactMetadata $artifact
        $readyArtifacts += [pscustomobject]@{
            Artifact = $artifact
            Path = $null
            Size = [int64]$artifact.size
            Sha256 = ([string]$artifact.sha256).ToUpperInvariant()
            Signature = ""
            External = $true
        }
        continue
    }

    $sourceHint = $artifactSources[[string]$artifact.id]
    if (-not $sourceHint) {
        throw "No local source path configured for ready artifact '$($artifact.id)'."
    }
    $sourceResolved = Resolve-RequiredPath $sourceHint
    $sourceName = Split-Path -Leaf $sourceResolved
    if ($artifact.fileName -ne $sourceName) {
        throw "Artifact '$($artifact.id)' source name '$sourceName' does not match manifest '$($artifact.fileName)'."
    }

    $sourceItem = Get-Item -LiteralPath $sourceResolved
    $sourceHash = Get-EcoreXFileSha256 -Path $sourceResolved
    $manifestHash = [string]$artifact.sha256
    if ($sourceHash -ne $manifestHash.ToUpperInvariant()) {
        throw "Artifact '$($artifact.id)' SHA256 $sourceHash does not match manifest $manifestHash."
    }
    if ([int64]$sourceItem.Length -ne [int64]$artifact.size) {
        throw "Artifact '$($artifact.id)' size $($sourceItem.Length) does not match manifest $($artifact.size)."
    }

    $signatureStatus = ""
    if ($sourceItem.Extension -in ".exe", ".msi") {
        $signatureStatus = (Get-AuthenticodeSignature -LiteralPath $sourceResolved).Status.ToString()
    }
    if ($artifact.id -like "windows-*" -and $signatureStatus -ne "Valid") {
        throw "Windows artifact '$($artifact.fileName)' is not Authenticode signed: $signatureStatus."
    }

    $readyArtifacts += [pscustomobject]@{
        Artifact = $artifact
        Path = $sourceResolved
        Size = $sourceItem.Length
        Sha256 = $sourceHash
        Signature = $signatureStatus
        External = $false
    }
}
if ($readyArtifacts.Count -eq 0) {
    throw "Manifest has no ready artifacts to publish."
}

foreach ($ready in $readyArtifacts) {
    if ($ready.External) {
        continue
    }
    $canonicalArtifactPath = Resolve-UnderDirectory -Path (Join-Path $outputResolved $ready.Artifact.fileName) -Base $outputResolved
    if (-not ([System.IO.Path]::GetFullPath($ready.Path).Equals([System.IO.Path]::GetFullPath($canonicalArtifactPath), [System.StringComparison]::OrdinalIgnoreCase))) {
        Copy-Item -LiteralPath $ready.Path -Destination $canonicalArtifactPath -Force
    }
    if ($ready.Artifact.id -eq "windows-x64") {
        $sourceDir = Split-Path -Parent $ready.Path
        foreach ($feedSource in @((Join-Path $sourceDir "latest.yml"), "$($ready.Path).blockmap")) {
            if (Test-Path -LiteralPath $feedSource) {
                $feedTarget = Resolve-UnderDirectory -Path (Join-Path $outputResolved (Split-Path -Leaf $feedSource)) -Base $outputResolved
                Copy-Item -LiteralPath $feedSource -Destination $feedTarget -Force
            }
        }
    }
    elseif ($ready.Artifact.id -eq "windows-ia32") {
        $sourceDir = Split-Path -Parent $ready.Path
        $feedDir = Join-Path $sourceDir "ia32"
        $feedOut = Resolve-UnderDirectory -Path (Join-Path $outputResolved "ia32") -Base $outputResolved
        New-Item -ItemType Directory -Force -Path $feedOut | Out-Null
        foreach ($feedSource in @((Join-Path $feedDir "latest.yml"), (Join-Path $feedDir "$($ready.Artifact.fileName).blockmap"))) {
            if (Test-Path -LiteralPath $feedSource) {
                $feedTarget = Resolve-UnderDirectory -Path (Join-Path $feedOut (Split-Path -Leaf $feedSource)) -Base $outputResolved
                Copy-Item -LiteralPath $feedSource -Destination $feedTarget -Force
            }
        }
        Copy-Item -LiteralPath $ready.Path -Destination (Join-Path $feedOut $ready.Artifact.fileName) -Force
    }
}

$stagingRoot = Resolve-UnderDirectory -Path (Join-Path $outputResolved "ecorex-public-release-$Version") -Base $outputResolved
$zipPath = Resolve-UnderDirectory -Path (Join-Path $outputResolved "EcoreX_$Version-public-release.zip") -Base $outputResolved

if (Test-Path -LiteralPath $stagingRoot) {
    Remove-Item -LiteralPath $stagingRoot -Recurse -Force
}
if (Test-Path -LiteralPath $zipPath) {
    Remove-Item -LiteralPath $zipPath -Force
}

$siteOut = Join-Path $stagingRoot "site"
$adminOut = Join-Path $stagingRoot "admin-api"
$serverOut = Join-Path $stagingRoot "server"
New-Item -ItemType Directory -Force -Path $siteOut, $adminOut, $serverOut | Out-Null

$publicReadme = @"
EcoreX public release package

This archive contains the EcoreX public download site, Admin API deployment files, server helper scripts, and checksums for EcoreX $Version.

"@
Write-Utf8NoBom -Path (Join-Path $stagingRoot "README.txt") -Value $publicReadme

Get-ChildItem -LiteralPath $siteRootResolved -Force | Where-Object { $_.Name -ne "downloads" } | ForEach-Object {
    Copy-Item -LiteralPath $_.FullName -Destination $siteOut -Recurse -Force
}

$downloadOut = Join-Path $siteOut "downloads"
New-Item -ItemType Directory -Force -Path $downloadOut | Out-Null
$updateFeedFiles = @()
foreach ($ready in $readyArtifacts) {
    if ($ready.External) {
        continue
    }
    if ($downloadsExternalized) {
        continue
    }
    Copy-Item -LiteralPath $ready.Path -Destination (Join-Path $downloadOut $ready.Artifact.fileName) -Force
    if ($ready.Artifact.id -eq "windows-x64") {
        $sourceDir = Split-Path -Parent $ready.Path
        $latestSource = Join-Path $sourceDir "latest.yml"
        $blockmapSource = "$($ready.Path).blockmap"
        if (-not (Test-Path -LiteralPath $latestSource)) {
            throw "Windows update feed file missing: $latestSource"
        }
        if (-not (Test-Path -LiteralPath $blockmapSource)) {
            throw "Windows update blockmap missing: $blockmapSource"
        }
        foreach ($feedSource in @($latestSource, $blockmapSource)) {
            $feedName = Split-Path -Leaf $feedSource
            $feedTarget = Join-Path $downloadOut $feedName
            Copy-Item -LiteralPath $feedSource -Destination $feedTarget -Force
            $updateFeedFiles += [pscustomobject]@{
                FileName = $feedName
                RelativePath = "site/downloads/$feedName"
                Size = (Get-Item -LiteralPath $feedTarget).Length
                Sha256 = Get-EcoreXFileSha256 -Path $feedTarget
            }
        }
    }
    elseif ($ready.Artifact.id -eq "windows-ia32") {
        $sourceDir = Split-Path -Parent $ready.Path
        $feedDir = Join-Path $sourceDir "ia32"
        $latestSource = Join-Path $feedDir "latest.yml"
        $blockmapSource = Join-Path $feedDir "$($ready.Artifact.fileName).blockmap"
        if (-not (Test-Path -LiteralPath $latestSource)) {
            throw "Windows ia32 update feed file missing: $latestSource"
        }
        if (-not (Test-Path -LiteralPath $blockmapSource)) {
            throw "Windows ia32 update blockmap missing: $blockmapSource"
        }
        $ia32Out = Join-Path $downloadOut "ia32"
        New-Item -ItemType Directory -Force -Path $ia32Out | Out-Null
        Copy-Item -LiteralPath $ready.Path -Destination (Join-Path $ia32Out $ready.Artifact.fileName) -Force
        foreach ($feedSource in @($latestSource, $blockmapSource)) {
            $feedName = Split-Path -Leaf $feedSource
            $feedTarget = Join-Path $ia32Out $feedName
            Copy-Item -LiteralPath $feedSource -Destination $feedTarget -Force
            $updateFeedFiles += [pscustomobject]@{
                FileName = "ia32/$feedName"
                RelativePath = "site/downloads/ia32/$feedName"
                Size = (Get-Item -LiteralPath $feedTarget).Length
                Sha256 = Get-EcoreXFileSha256 -Path $feedTarget
            }
        }
        $ia32InstallerTarget = Join-Path $ia32Out $ready.Artifact.fileName
        $updateFeedFiles += [pscustomobject]@{
            FileName = "ia32/$($ready.Artifact.fileName)"
            RelativePath = "site/downloads/ia32/$($ready.Artifact.fileName)"
            Size = (Get-Item -LiteralPath $ia32InstallerTarget).Length
            Sha256 = Get-EcoreXFileSha256 -Path $ia32InstallerTarget
        }
    }
}

if ($downloadsExternalized) {
    $publicManifestPath = Join-Path $siteOut "manifest.json"
    $publicManifest = Get-Content -Raw -Encoding UTF8 -LiteralPath $publicManifestPath | ConvertFrom-Json
    foreach ($ready in $readyArtifacts) {
        if ($ready.External) {
            continue
        }
        $artifactId = [string]$ready.Artifact.id
        $fileName = [string]$ready.Artifact.fileName
        foreach ($publicArtifact in $publicManifest.artifacts) {
            if ([string]$publicArtifact.id -ne $artifactId) {
                continue
            }
            Set-JsonObjectProperty -Object $publicArtifact -Name "href" -Value "downloads/$fileName"
            break
        }
    }
    Set-JsonObjectProperty -Object $publicManifest -Name "downloadsExternalized" -Value $true
    $configuredDownloadMirrors = @(Get-ConfiguredDownloadMirrors)
    if ($configuredDownloadMirrors.Count -gt 0) {
        Set-JsonObjectProperty -Object $publicManifest -Name "download" -Value ([ordered]@{
            mode = "mirror-first-origin-fallback"
            mirrors = $configuredDownloadMirrors
            minimumTargetBytesPerSecond = 1048576
            integrity = "sha256"
            fallback = "origin"
        })
    }
    Write-Utf8NoBom -Path $publicManifestPath -Value (($publicManifest | ConvertTo-Json -Depth 12) + "`n")
}

$adminFiles = @("ecorex_admin_api.py", "Dockerfile", "README.md")
foreach ($fileName in $adminFiles) {
    $source = Join-Path $adminApiRootResolved $fileName
    if (Test-Path -LiteralPath $source) {
        Copy-Item -LiteralPath $source -Destination (Join-Path $adminOut $fileName) -Force
    }
}

$serverFiles = @(
    @{ Source = "scripts/install-ecorex-public-release.sh"; Target = "install-ecorex-public-release.sh" },
    @{ Source = "scripts/install-ecorex-web.sh"; Target = "install-ecorex-web.sh" },
    @{ Source = "scripts/check-ecorex-server-release.sh"; Target = "check-ecorex-server-release.sh" },
    @{ Source = "deploy/ecorex-site/caddy/Caddyfile.example"; Target = "caddy/Caddyfile.example" },
    @{ Source = "deploy/ecorex-site/caddy/ecorex-agent.routes.caddy"; Target = "caddy/ecorex-agent.routes.caddy" },
    @{ Source = "deploy/ecorex-site/nginx/ecorex-agent.conf.example"; Target = "nginx/ecorex-agent.conf.example" },
    @{ Source = "deploy/ecorex-admin-api/systemd/ecorex-admin-api.service.example"; Target = "systemd/ecorex-admin-api.service.example" }
)
foreach ($entry in $serverFiles) {
    $source = Resolve-UnderDirectory -Path (Join-Path $repoRoot $entry.Source) -Base $repoRoot
    if (Test-Path -LiteralPath $source) {
        $target = Join-Path $serverOut $entry.Target
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $target) | Out-Null
        Copy-Item -LiteralPath $source -Destination $target -Force
    }
}

$checksumArtifacts = [ordered]@{}
foreach ($ready in $readyArtifacts) {
    if ($ready.External) {
        $checksumArtifacts[$ready.Artifact.id] = [ordered]@{
            fileName = $ready.Artifact.fileName
            relativePath = [string]$ready.Artifact.href
            href = [string]$ready.Artifact.href
            size = $ready.Size
            sha256 = $ready.Sha256
            status = $ready.Artifact.status
            authenticode = $ready.Signature
            external = $true
        }
        continue
    }

    if ($downloadsExternalized) {
        $checksumArtifacts[$ready.Artifact.id] = [ordered]@{
            fileName = $ready.Artifact.fileName
            relativePath = "site/downloads/$($ready.Artifact.fileName)"
            href = "downloads/$($ready.Artifact.fileName)"
            size = $ready.Size
            sha256 = $ready.Sha256
            status = $ready.Artifact.status
            authenticode = $ready.Signature
            external = $true
            deploymentSourceFileName = $ready.Artifact.fileName
        }
        continue
    }

    $stagedPath = Join-Path $downloadOut $ready.Artifact.fileName
    $stagedHash = Get-EcoreXFileSha256 -Path $stagedPath
    $stagedSize = (Get-Item -LiteralPath $stagedPath).Length
    if ($stagedHash -ne $ready.Sha256 -or $stagedSize -ne $ready.Size) {
        throw "Staged artifact verification failed for $($ready.Artifact.id)."
    }
    $checksumArtifacts[$ready.Artifact.id] = [ordered]@{
        fileName = $ready.Artifact.fileName
        relativePath = "site/downloads/$($ready.Artifact.fileName)"
        size = $ready.Size
        sha256 = $ready.Sha256
        status = $ready.Artifact.status
        authenticode = $ready.Signature
    }
}

$windowsReady = $readyArtifacts | Where-Object { $_.Artifact.id -eq "windows-x64" } | Select-Object -First 1
$macReadyCount = @($readyArtifacts | Where-Object { $_.Artifact.id -like "macos-*" }).Count
$webuiWindowsReady = $readyArtifacts | Where-Object { $_.Artifact.id -eq "webui-windows-x64" } | Select-Object -First 1
$webuiMacosReady = $readyArtifacts | Where-Object { $_.Artifact.id -eq "webui-macos-universal" } | Select-Object -First 1

$checksums = [ordered]@{
    product = "EcoreX"
    version = $Version
    generatedAt = (Get-Date).ToString("o")
    downloadsExternalized = [bool]$downloadsExternalized
    downloadsSource = if ($downloadsExternalized) { "public-release-downloads-source" } else { "embedded" }
    siteRoot = "site"
    adminApiRoot = "admin-api"
    serverHelperRoot = "server"
    artifacts = $checksumArtifacts
    updateFeed = @($updateFeedFiles | ForEach-Object {
        [ordered]@{
            fileName = $_.FileName
            relativePath = $_.RelativePath
            size = $_.Size
            sha256 = $_.Sha256
        }
    })
    windows = [ordered]@{
        status = if ($windowsReady) { $windowsReady.Artifact.status } elseif ($webuiWindowsReady) { "webui-ready" } else { "not-included" }
        fileName = if ($windowsReady) { $windowsReady.Artifact.fileName } else { "" }
        relativePath = if ($windowsReady) { "site/downloads/$($windowsReady.Artifact.fileName)" } else { "" }
        size = if ($windowsReady) { $windowsReady.Size } else { 0 }
        sha256 = if ($windowsReady) { $windowsReady.Sha256 } else { "" }
        authenticode = if ($windowsReady) { $windowsReady.Signature } else { "" }
    }
    macos = if ($macReadyCount -gt 0) { "included $macReadyCount dmg artifact(s); signing/notarization evidence is external" } elseif ($webuiMacosReady) { "webui-ready" } else { "deferred to Mac validation" }
    webui = [ordered]@{
        windows = if ($webuiWindowsReady) {
            [ordered]@{
                status = $webuiWindowsReady.Artifact.status
                fileName = $webuiWindowsReady.Artifact.fileName
                relativePath = "site/downloads/$($webuiWindowsReady.Artifact.fileName)"
                size = $webuiWindowsReady.Size
                sha256 = $webuiWindowsReady.Sha256
            }
        } else {
            [ordered]@{ status = "not-included"; fileName = ""; relativePath = ""; size = 0; sha256 = "" }
        }
        macos = if ($webuiMacosReady) {
            [ordered]@{
                status = $webuiMacosReady.Artifact.status
                fileName = $webuiMacosReady.Artifact.fileName
                relativePath = "site/downloads/$($webuiMacosReady.Artifact.fileName)"
                size = $webuiMacosReady.Size
                sha256 = $webuiMacosReady.Sha256
            }
        } else {
            [ordered]@{ status = "not-included"; fileName = ""; relativePath = ""; size = 0; sha256 = "" }
        }
    }
}
Write-Utf8NoBom -Path (Join-Path $stagingRoot "checksums.json") -Value (($checksums | ConvertTo-Json -Depth 8) + "`n")

Invoke-ReleaseTextSanitizer -Root $stagingRoot

Add-Type -AssemblyName System.IO.Compression
Add-Type -AssemblyName System.IO.Compression.FileSystem
$archive = [System.IO.Compression.ZipFile]::Open($zipPath, [System.IO.Compression.ZipArchiveMode]::Create)
try {
    $stagingPrefix = $stagingRoot.TrimEnd("\", "/") + [System.IO.Path]::DirectorySeparatorChar
    Get-ChildItem -LiteralPath $stagingRoot -Recurse -File | ForEach-Object {
        $relativePath = $_.FullName.Substring($stagingPrefix.Length).Replace("\", "/")
        [System.IO.Compression.ZipFileExtensions]::CreateEntryFromFile(
            $archive,
            $_.FullName,
            $relativePath,
            [System.IO.Compression.CompressionLevel]::Optimal
        ) | Out-Null
    }
} finally {
    $archive.Dispose()
}

$zipHash = Get-EcoreXFileSha256 -Path $zipPath
$zipItem = Get-Item -LiteralPath $zipPath

if (-not $KeepStaging) {
    Remove-Item -LiteralPath $stagingRoot -Recurse -Force
}

if (-not $SkipValidation) {
    $validator = Join-Path $repoRoot "scripts\validate-ecorex-release-artifacts.py"
    if (Test-Path -LiteralPath $validator) {
        & python $validator --version $Version --public-zip $zipPath
        if ($LASTEXITCODE -ne 0) {
            throw "Release artifact validation failed."
        }
    }
}

[ordered]@{
    ok = $true
    version = $Version
    zipPath = $zipPath
    zipSize = $zipItem.Length
    zipSha256 = $zipHash
    artifacts = $checksumArtifacts
} | ConvertTo-Json -Depth 8
