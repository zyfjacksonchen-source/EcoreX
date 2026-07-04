param(
    [string]$Version = "",
    [switch]$SkipWebuiPackage,
    [switch]$SkipManifestPromotion,
    [switch]$EmbedDownloads
)

$ErrorActionPreference = "Stop"

$repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
$desktopRoot = Join-Path $repoRoot "desktop"

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
    $encoding = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($Path, $Value, $encoding)
}

function Set-DefaultDownloadMirror {
    param([Parameter(Mandatory = $true)][string]$Version)
    $manifestPath = Join-Path $repoRoot "deploy\ecorex-site\manifest.json"
    if (-not (Test-Path -LiteralPath $manifestPath)) {
        throw "Download manifest is missing: $manifestPath"
    }
    $manifest = Get-Content -Raw -Encoding UTF8 -LiteralPath $manifestPath | ConvertFrom-Json
    $mirror = [ordered]@{
        id = "github-release-v$Version"
        kind = "github-release"
        baseUrl = "https://github.com/zhangyifanjackson-dotcom/EcoreX-installers/releases/download/v$Version"
        pathMode = "fileName"
    }
    $download = $manifest.download
    if (-not $download) {
        $download = [pscustomobject]@{}
        Add-Member -InputObject $manifest -NotePropertyName "download" -NotePropertyValue $download -Force
    }
    foreach ($entry in @(
        @{ Name = "mode"; Value = "mirror-first-origin-fallback" },
        @{ Name = "mirrors"; Value = @($mirror) },
        @{ Name = "minimumTargetBytesPerSecond"; Value = 1048576 },
        @{ Name = "integrity"; Value = "sha256" },
        @{ Name = "fallback"; Value = "origin" }
    )) {
        if ($download.PSObject.Properties.Name -contains $entry.Name) {
            $download.PSObject.Properties[$entry.Name].Value = $entry.Value
        } else {
            Add-Member -InputObject $download -NotePropertyName $entry.Name -NotePropertyValue $entry.Value -Force
        }
    }
    Write-Utf8NoBom -Path $manifestPath -Value (($manifest | ConvertTo-Json -Depth 12) + [Environment]::NewLine)
}

if (-not $Version) {
    $desktopPackagePath = Join-Path $desktopRoot "package.json"
    if (-not (Test-Path -LiteralPath $desktopPackagePath)) {
        throw "Cannot infer version because desktop/package.json does not exist. Pass -Version explicitly."
    }
    $desktopPackage = Get-Content -Raw -Encoding UTF8 -LiteralPath $desktopPackagePath | ConvertFrom-Json
    $Version = [string]$desktopPackage.version
}

if (-not $SkipWebuiPackage) {
    Invoke-CheckedCommand -FilePath "npm" -ArgumentList @("run", "webui:package") -WorkingDirectory $desktopRoot
    Invoke-CheckedCommand -FilePath "powershell" -ArgumentList @(
        "-NoProfile",
        "-ExecutionPolicy", "Bypass",
        "-File", (Join-Path $repoRoot "scripts\prepare-ecorex-web-release.ps1"),
        "-Version", $Version
    )
}

$webuiWindows = Join-Path $repoRoot "release-artifacts\EcoreX_${Version}-webui-windows-x64.zip"
$webuiMacos = Join-Path $repoRoot "release-artifacts\EcoreX_${Version}-webui-macos-universal.zip"
$webLinuxService = Join-Path $repoRoot "release-artifacts\EcoreX_${Version}-web-linux-service.tar.gz"
foreach ($requiredPath in @($webuiWindows, $webuiMacos, $webLinuxService)) {
    if (-not (Test-Path -LiteralPath $requiredPath)) {
        throw "Required WebUI package is missing: $requiredPath"
    }
}

if (-not $SkipManifestPromotion) {
    Invoke-CheckedCommand -FilePath "powershell" -ArgumentList @(
        "-NoProfile",
        "-ExecutionPolicy", "Bypass",
        "-File", (Join-Path $repoRoot "scripts\update-ecorex-desktop-release-manifest.ps1"),
        "-Version", $Version,
        "-PromoteVersion",
        "-WebUiWindowsPath", $webuiWindows,
        "-WebUiMacosPath", $webuiMacos,
        "-WebLinuxServicePath", $webLinuxService
    )
    Set-DefaultDownloadMirror -Version $Version
}

$mirrorUrl = "https://github.com/zhangyifanjackson-dotcom/EcoreX-installers/releases/download/v$Version"
$prepareArgs = @(
    "-NoProfile",
    "-ExecutionPolicy", "Bypass",
    "-File", (Join-Path $repoRoot "scripts\prepare-ecorex-public-release.ps1"),
    "-Version", $Version,
    "-GitHubReleaseMirrorUrl", $mirrorUrl
)
if ($EmbedDownloads) {
    $prepareArgs += "-EmbedDownloads"
}
Invoke-CheckedCommand -FilePath "powershell" -ArgumentList $prepareArgs

Write-Host "EcoreX default release package is ready."
Write-Host "version: $Version"
Write-Host "publicRelease: release-artifacts/EcoreX_${Version}-public-release.zip"
Write-Host "downloadMirror: $mirrorUrl"
