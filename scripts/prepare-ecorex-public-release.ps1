param(
    [string]$Version = "0.1.11",
    [string]$SiteRoot = "deploy/ecorex-site",
    [string]$AdminApiRoot = "deploy/ecorex-admin-api",
    [string]$InstallerPath = "desktop/release/EcoreX_0.1.11_x64-setup.exe",
    [string]$MacArm64DmgPath = "",
    [string]$MacX64DmgPath = "",
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

$repoRoot = (Resolve-Path -LiteralPath ".").Path
$siteRootResolved = Resolve-RequiredPath $SiteRoot
$adminApiRootResolved = Resolve-RequiredPath $AdminApiRoot

$manifestPath = Join-Path $siteRootResolved "manifest.json"
$manifest = Get-Content -Raw -Encoding UTF8 -LiteralPath $manifestPath | ConvertFrom-Json
if ($manifest.version -ne $Version) {
    throw "Manifest version '$($manifest.version)' does not match expected '$Version'."
}

$artifactSources = @{}
$artifactSources["windows-x64"] = $InstallerPath
$artifactSources["macos-arm64-dmg"] = if ($MacArm64DmgPath) { $MacArm64DmgPath } else { Join-Path "desktop/release" "EcoreX_${Version}_arm64.dmg" }
$artifactSources["macos-x64-dmg"] = if ($MacX64DmgPath) { $MacX64DmgPath } else { Join-Path "desktop/release" "EcoreX_${Version}_x64.dmg" }

$windowsArtifact = @($manifest.artifacts | Where-Object { $_.id -eq "windows-x64" }) | Select-Object -First 1
if (-not $windowsArtifact) {
    throw "Manifest does not contain windows-x64 artifact."
}
if ($windowsArtifact.status -ne "ready") {
    throw "windows-x64 artifact must be ready before creating a public release."
}

$readyArtifacts = @()
foreach ($artifact in $manifest.artifacts) {
    if ($artifact.status -ne "ready") {
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
    }
}

$outputResolved = [System.IO.Path]::GetFullPath((Join-Path $repoRoot $OutputDir))
New-Item -ItemType Directory -Force -Path $outputResolved | Out-Null

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

Get-ChildItem -LiteralPath $siteRootResolved -Force | Where-Object { $_.Name -ne "downloads" } | ForEach-Object {
    Copy-Item -LiteralPath $_.FullName -Destination $siteOut -Recurse -Force
}

$downloadOut = Join-Path $siteOut "downloads"
New-Item -ItemType Directory -Force -Path $downloadOut | Out-Null
foreach ($ready in $readyArtifacts) {
    Copy-Item -LiteralPath $ready.Path -Destination (Join-Path $downloadOut $ready.Artifact.fileName) -Force
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
    windows = [ordered]@{
        fileName = $windowsReady.Artifact.fileName
        relativePath = "site/downloads/$($windowsReady.Artifact.fileName)"
        size = $windowsReady.Size
        sha256 = $windowsReady.Sha256
        authenticode = $windowsReady.Signature
    }
    macos = if ($macReadyCount -gt 0) { "included $macReadyCount dmg artifact(s); signing/notarization evidence is external" } else { "deferred to Mac validation" }
}
$checksums | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath (Join-Path $stagingRoot "checksums.json") -Encoding UTF8

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

[ordered]@{
    ok = $true
    version = $Version
    zipPath = $zipPath
    zipSize = $zipItem.Length
    zipSha256 = $zipHash
    artifacts = $checksumArtifacts
} | ConvertTo-Json -Depth 8
