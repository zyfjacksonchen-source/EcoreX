param(
    [string]$Version = "0.1.10",
    [string]$SiteRoot = "deploy/ecorex-site",
    [string]$AdminApiRoot = "deploy/ecorex-admin-api",
    [string]$InstallerPath = "desktop/release/EcoreX_0.1.10_x64-setup.exe",
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
$installerResolved = Resolve-RequiredPath $InstallerPath

$manifestPath = Join-Path $siteRootResolved "manifest.json"
$manifest = Get-Content -Raw -Encoding UTF8 -LiteralPath $manifestPath | ConvertFrom-Json
if ($manifest.version -ne $Version) {
    throw "Manifest version '$($manifest.version)' does not match expected '$Version'."
}

$windowsArtifact = @($manifest.artifacts | Where-Object { $_.id -eq "windows-x64" }) | Select-Object -First 1
if (-not $windowsArtifact) {
    throw "Manifest does not contain windows-x64 artifact."
}
if ($windowsArtifact.fileName -ne (Split-Path -Leaf $installerResolved)) {
    throw "Installer name '$(Split-Path -Leaf $installerResolved)' does not match manifest '$($windowsArtifact.fileName)'."
}

$installerItem = Get-Item -LiteralPath $installerResolved
$installerHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $installerResolved).Hash.ToUpperInvariant()
$manifestHash = [string]$windowsArtifact.sha256
if ($installerHash -ne $manifestHash.ToUpperInvariant()) {
    throw "Installer SHA256 $installerHash does not match manifest $manifestHash."
}
if ([int64]$installerItem.Length -ne [int64]$windowsArtifact.size) {
    throw "Installer size $($installerItem.Length) does not match manifest $($windowsArtifact.size)."
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
New-Item -ItemType Directory -Force -Path $siteOut, $adminOut | Out-Null

Get-ChildItem -LiteralPath $siteRootResolved -Force | Where-Object { $_.Name -ne "downloads" } | ForEach-Object {
    Copy-Item -LiteralPath $_.FullName -Destination $siteOut -Recurse -Force
}

$downloadOut = Join-Path $siteOut "downloads"
New-Item -ItemType Directory -Force -Path $downloadOut | Out-Null
Copy-Item -LiteralPath $installerResolved -Destination (Join-Path $downloadOut $windowsArtifact.fileName) -Force

$adminFiles = @("ecorex_admin_api.py", "Dockerfile", "README.md")
foreach ($fileName in $adminFiles) {
    $source = Join-Path $adminApiRootResolved $fileName
    if (Test-Path -LiteralPath $source) {
        Copy-Item -LiteralPath $source -Destination (Join-Path $adminOut $fileName) -Force
    }
}

$stagedInstaller = Join-Path $downloadOut $windowsArtifact.fileName
$stagedHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $stagedInstaller).Hash.ToUpperInvariant()
$stagedSize = (Get-Item -LiteralPath $stagedInstaller).Length
if ($stagedHash -ne $installerHash -or $stagedSize -ne $installerItem.Length) {
    throw "Staged installer verification failed."
}

$checksums = [ordered]@{
    product = "EcoreX"
    version = $Version
    generatedAt = (Get-Date).ToString("o")
    siteRoot = "site"
    adminApiRoot = "admin-api"
    windows = [ordered]@{
        fileName = $windowsArtifact.fileName
        relativePath = "site/downloads/$($windowsArtifact.fileName)"
        size = $stagedSize
        sha256 = $stagedHash
        authenticode = (Get-AuthenticodeSignature -LiteralPath $installerResolved).Status.ToString()
    }
    macos = "deferred to Mac validation"
}
$checksums | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath (Join-Path $stagingRoot "checksums.json") -Encoding UTF8

Compress-Archive -Path (Join-Path $stagingRoot "*") -DestinationPath $zipPath -Force

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
    windowsInstaller = [ordered]@{
        size = $installerItem.Length
        sha256 = $installerHash
        signature = (Get-AuthenticodeSignature -LiteralPath $installerResolved).Status.ToString()
    }
} | ConvertTo-Json -Depth 8
