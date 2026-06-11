param(
    [string]$ArtifactsDir = "$PSScriptRoot\..\release",
    [string]$SignToolDir = "",
    [string]$SimplySignShortcut = "C:\Users\user\Desktop\SimplySign Desktop.lnk",
    [string]$Thumbprint = "0f678477dfc0a2bdaab88307126ef657faf8674f",
    [switch]$CoreAppOnly,
    [switch]$SetupOnly,
    [switch]$LaunchSimplySign
)

$ErrorActionPreference = "Stop"

if (-not $SignToolDir) {
    $signToolFolderName = -join ([char[]](0x811A, 0x672C, 0x7B7E, 0x540D, 0x5DE5, 0x5177))
    $SignToolDir = Join-Path "C:\" $signToolFolderName
}

$resolvedArtifacts = Resolve-Path -LiteralPath $ArtifactsDir -ErrorAction Stop
$signTool = Join-Path $SignToolDir "signtool.exe"

if (-not (Test-Path -LiteralPath $signTool)) {
    throw "signtool.exe not found at $signTool"
}

if ($LaunchSimplySign) {
    if (-not (Test-Path -LiteralPath $SimplySignShortcut)) {
        throw "SimplySign shortcut not found at $SimplySignShortcut"
    }
    Start-Process -FilePath $SimplySignShortcut
    Start-Sleep -Seconds 3
}

$targets = @()
if ($CoreAppOnly) {
    $coreRelative = @(
        "EcoreX.exe",
        "resources\elevate.exe",
        "resources\ecorex-runtime\python\python.exe",
        "resources\ecorex-runtime\python\pythonw.exe"
    )
    foreach ($relative in $coreRelative) {
        $targetPath = Join-Path $resolvedArtifacts $relative
        if (Test-Path -LiteralPath $targetPath) {
            $targets += Get-Item -LiteralPath $targetPath
        }
    }
}
else {
    $targets = Get-ChildItem -LiteralPath $resolvedArtifacts -Recurse -File |
        Where-Object { $_.Extension -in ".exe", ".msi" }
}

if ($SetupOnly) {
    $artifactRoot = [System.IO.Path]::GetFullPath([string]$resolvedArtifacts)
    $targets = $targets | Where-Object {
        [System.IO.Path]::GetFullPath($_.DirectoryName) -eq $artifactRoot -and $_.Name -like "*setup*.exe"
    }
}

if (-not $targets) {
    throw "No .exe or .msi artifacts found under $resolvedArtifacts"
}

foreach ($target in $targets) {
    $signature = Get-AuthenticodeSignature -LiteralPath $target.FullName
    if ($signature.Status -eq "Valid") {
        Write-Host "Already signed: $($target.FullName)"
        continue
    }
    Write-Host "Signing $($target.FullName) with SHA256 signature"
    & $signTool sign /v /fd sha256 /sha1 $Thumbprint /tr http://timestamp.digicert.com /td sha256 $target.FullName
    if ($LASTEXITCODE -ne 0) {
        throw "signtool failed with exit code $LASTEXITCODE for $($target.FullName)"
    }
    $signature = Get-AuthenticodeSignature -LiteralPath $target.FullName
    if ($signature.Status -ne "Valid") {
        throw "signature verification failed for $($target.FullName): $($signature.Status) $($signature.StatusMessage)"
    }
}

Write-Host "Windows signing complete. This script does not delete or modify certificates."
