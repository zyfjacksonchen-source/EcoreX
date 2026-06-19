param(
    [string]$Version = "",
    [string]$OutputDir = "release-installers",
    [string]$ReadmeTemplate = "docs/ecorex/v0.1.15/installer-repo-README.md",
    [string]$WindowsInstaller = "",
    [string]$WindowsLatestYml = "",
    [string]$WindowsBlockmap = "",
    [string]$MacArm64Dmg = "",
    [string]$MacX64Dmg = "",
    [switch]$KeepExisting
)

$ErrorActionPreference = "Stop"

function Resolve-OptionalFile {
    param([string]$Path)
    if (-not $Path) { return "" }
    if (-not (Test-Path -LiteralPath $Path)) { return "" }
    return (Resolve-Path -LiteralPath $Path).Path
}

function Get-Sha256 {
    param([Parameter(Mandatory = $true)][string]$Path)
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToUpperInvariant()
}

function Write-Utf8NoBom {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Value
    )
    $encoding = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($Path, $Value, $encoding)
}

$repoRoot = (Resolve-Path -LiteralPath ".").Path
if (-not $Version) {
    $desktopPackagePath = Join-Path $repoRoot "desktop/package.json"
    if (-not (Test-Path -LiteralPath $desktopPackagePath)) {
        throw "Cannot infer version because desktop/package.json does not exist. Pass -Version explicitly."
    }
    $desktopPackage = Get-Content -Raw -Encoding UTF8 -LiteralPath $desktopPackagePath | ConvertFrom-Json
    $Version = [string]$desktopPackage.version
}

if (-not $WindowsInstaller) {
    $WindowsInstaller = Join-Path $repoRoot "desktop/release/EcoreX_${Version}_x64-setup.exe"
}
if (-not $WindowsLatestYml) {
    $WindowsLatestYml = Join-Path $repoRoot "desktop/release/latest.yml"
}
if (-not $WindowsBlockmap) {
    $WindowsBlockmap = Join-Path $repoRoot "desktop/release/EcoreX_${Version}_x64-setup.exe.blockmap"
}

$readmeResolved = Resolve-OptionalFile (Join-Path $repoRoot $ReadmeTemplate)
if (-not $readmeResolved) {
    throw "README template is required: $ReadmeTemplate"
}

$outputRoot = [System.IO.Path]::GetFullPath((Join-Path $repoRoot $OutputDir))
$targetRoot = Join-Path $outputRoot "EcoreX-installers-$Version"
if ((Test-Path -LiteralPath $targetRoot) -and -not $KeepExisting) {
    $resolvedOutput = [System.IO.Path]::GetFullPath($outputRoot)
    $resolvedTarget = [System.IO.Path]::GetFullPath($targetRoot)
    if (-not $resolvedTarget.StartsWith($resolvedOutput, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to delete target outside output dir: $resolvedTarget"
    }
    Remove-Item -LiteralPath $targetRoot -Recurse -Force
}
New-Item -ItemType Directory -Force -Path $targetRoot | Out-Null

Copy-Item -LiteralPath $readmeResolved -Destination (Join-Path $targetRoot "README.md") -Force

$inputs = @(
    @{ id = "windows-x64-installer"; path = Resolve-OptionalFile $WindowsInstaller },
    @{ id = "windows-latest-yml"; path = Resolve-OptionalFile $WindowsLatestYml },
    @{ id = "windows-blockmap"; path = Resolve-OptionalFile $WindowsBlockmap },
    @{ id = "macos-arm64-dmg"; path = Resolve-OptionalFile $MacArm64Dmg },
    @{ id = "macos-x64-dmg"; path = Resolve-OptionalFile $MacX64Dmg }
) | Where-Object { $_.path }

$manifestFiles = @()
$sumLines = @()
foreach ($input in $inputs) {
    $source = [string]$input.path
    $fileName = Split-Path -Leaf $source
    $dest = Join-Path $targetRoot $fileName
    Copy-Item -LiteralPath $source -Destination $dest -Force
    $hash = Get-Sha256 $dest
    $size = (Get-Item -LiteralPath $dest).Length
    $sumLines += "$hash  $fileName"
    $manifestFiles += [ordered]@{
        id = $input.id
        fileName = $fileName
        size = $size
        sha256 = $hash
    }
}

$manifest = [ordered]@{
    name = "EcoreX installer-only public repository"
    version = $Version
    generatedAt = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
    noSourceCodePolicy = "PUBLIC REPO MUST NOT CONTAIN SOURCE CODE. Only installers/packages, README, update metadata, manifest, and checksums are allowed."
    files = $manifestFiles
}
Write-Utf8NoBom -Path (Join-Path $targetRoot "manifest.json") -Value (($manifest | ConvertTo-Json -Depth 8) + [Environment]::NewLine)
Write-Utf8NoBom -Path (Join-Path $targetRoot "SHA256SUMS.txt") -Value (($sumLines -join [Environment]::NewLine) + [Environment]::NewLine)

& (Join-Path $repoRoot "scripts/verify-ecorex-installer-repo.ps1") -Root $targetRoot

Write-Host "Installer-only repository staging ready: $targetRoot"
