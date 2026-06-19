param(
    [string]$Version = "",
    [string]$SiteRoot = "deploy/ecorex-site",
    [string]$AdminApiRoot = "deploy/ecorex-admin-api",
    [string]$InstallerPath = "",
    [string]$MacArm64DmgPath = "",
    [string]$MacX64DmgPath = "",
    [string]$WebTarballPath = "",
    [string]$OutputDir = "release-artifacts",
    [switch]$KeepStaging,
    [switch]$SkipValidation
)

$ErrorActionPreference = "Stop"

Write-Warning "This script prepares the internal/site public-release bundle and is NOT safe for the open installer-only GitHub repository. Use scripts/prepare-ecorex-installer-repo.ps1 for the public GitHub installer repo; that repo must not contain source code."

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
    $resolvedBase = [System.IO.Path]::GetFullPath($Base)
    $resolvedPath = [System.IO.Path]::GetFullPath($Path)
    if (-not $resolvedPath.StartsWith($resolvedBase, [System.StringComparison]::OrdinalIgnoreCase)) {
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
if (-not $InstallerPath) {
    $InstallerPath = Join-Path "desktop/release" "EcoreX_${Version}_x64-setup.exe"
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
$artifactSources["webui-win-mac"] = Join-Path "release-artifacts" "EcoreX_${Version}-webui-win-mac.zip"
$artifactSources["webui-windows-x64"] = Join-Path "release-artifacts" "EcoreX_${Version}-webui-windows-x64.zip"
$artifactSources["webui-macos-universal"] = Join-Path "release-artifacts" "EcoreX_${Version}-webui-macos-universal.zip"
$artifactSources["macos-arm64-dmg"] = if ($MacArm64DmgPath) { $MacArm64DmgPath } else { Join-Path "desktop/release" "EcoreX_${Version}_arm64.dmg" }
$artifactSources["macos-x64-dmg"] = if ($MacX64DmgPath) { $MacX64DmgPath } else { Join-Path "desktop/release" "EcoreX_${Version}_x64.dmg" }
$artifactSources["web-linux-service"] = if ($WebTarballPath) { $WebTarballPath } else { Join-Path "release-artifacts" "EcoreX_${Version}-web-linux-service.tar.gz" }

$publishableStatuses = @("ready")
$readyArtifacts = @()
foreach ($artifact in $manifest.artifacts) {
    if ([string]$artifact.status -notin $publishableStatuses) {
        continue
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
    $sourceHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $sourceResolved).Hash.ToUpperInvariant()
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
                Sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $feedTarget).Hash.ToUpperInvariant()
            }
        }
    }
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

    $stagedPath = Join-Path $downloadOut $ready.Artifact.fileName
    $stagedHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $stagedPath).Hash.ToUpperInvariant()
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

$checksums = [ordered]@{
    product = "EcoreX"
    version = $Version
    generatedAt = (Get-Date).ToString("o")
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
        status = if ($windowsReady) { $windowsReady.Artifact.status } else { "not-included" }
        fileName = if ($windowsReady) { $windowsReady.Artifact.fileName } else { "" }
        relativePath = if ($windowsReady) { "site/downloads/$($windowsReady.Artifact.fileName)" } else { "" }
        size = if ($windowsReady) { $windowsReady.Size } else { 0 }
        sha256 = if ($windowsReady) { $windowsReady.Sha256 } else { "" }
        authenticode = if ($windowsReady) { $windowsReady.Signature } else { "" }
    }
    macos = if ($macReadyCount -gt 0) { "included $macReadyCount dmg artifact(s); signing/notarization evidence is external" } else { "deferred to Mac validation" }
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

$zipHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $zipPath).Hash.ToUpperInvariant()
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
