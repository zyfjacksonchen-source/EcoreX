param(
    [string]$Version = "0.1.17",
    [string]$InstallerPath = "",
    [string]$SmokePath = "",
    [switch]$PreflightOnly,
    [switch]$PackageOnly,
    [switch]$SmokeOnly,
    [switch]$ImportManifest,
    [switch]$LaunchSimplySign,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"

function Resolve-RepoRoot {
    $root = (& git rev-parse --show-toplevel 2>$null)
    if ($LASTEXITCODE -ne 0 -or -not $root) {
        throw "Run this script from inside the EcoreX git checkout."
    }
    return [System.IO.Path]::GetFullPath($root.Trim())
}

function Invoke-Step {
    param(
        [Parameter(Mandatory = $true)][string]$WorkingDirectory,
        [Parameter(Mandatory = $true)][string]$FilePath,
        [string[]]$ArgumentList = @()
    )
    Push-Location $WorkingDirectory
    try {
        & $FilePath @ArgumentList
        if ($LASTEXITCODE -ne 0) {
            throw "$FilePath $($ArgumentList -join ' ') failed with exit code $LASTEXITCODE"
        }
    }
    finally {
        Pop-Location
    }
}

function Resolve-InstallerPath {
    param(
        [Parameter(Mandatory = $true)][string]$RepoRoot,
        [Parameter(Mandatory = $true)][string]$Version,
        [string]$Path = ""
    )
    if ($Path) {
        if (-not (Test-Path -LiteralPath $Path)) {
            throw "Installer path does not exist: $Path"
        }
        return (Resolve-Path -LiteralPath $Path).Path
    }
    $default = Join-Path $RepoRoot "desktop\release\EcoreX_${Version}_x64-setup.exe"
    if (-not (Test-Path -LiteralPath $default)) {
        throw "Signed installer was not found at $default"
    }
    return (Resolve-Path -LiteralPath $default).Path
}

function Write-Plan {
    param(
        [Parameter(Mandatory = $true)][string]$RepoRoot,
        [Parameter(Mandatory = $true)][string]$Version,
        [Parameter(Mandatory = $true)][string]$InstallerPath,
        [Parameter(Mandatory = $true)][string]$SmokePath,
        [Parameter(Mandatory = $true)][bool]$LaunchSimplySign
    )
    $desktopDir = Join-Path $RepoRoot "desktop"
    $preflight = "npm run sign:win:preflight"
    if ($LaunchSimplySign) {
        $preflight = "powershell -ExecutionPolicy Bypass -File scripts\sign-win.ps1 -PreflightOnly -LaunchSimplySign"
    }
    [ordered]@{
        dryRun = $true
        desktopDir = $desktopDir
        version = $Version
        installer = $InstallerPath
        smoke = $SmokePath
        steps = @(
            "cd `"$desktopDir`"",
            $preflight,
            "npm run package:win:signed",
            "npm run smoke:win:installed -- -InstallerPath `"$InstallerPath`" -OutputPath `"..\docs\v$Version\win-installed-smoke.json`"",
            "cd `"$RepoRoot`"",
            "powershell -ExecutionPolicy Bypass -File scripts\update-ecorex-desktop-release-manifest.ps1 -Version $Version -WindowsInstallerPath `"$InstallerPath`" -WindowsInstalledSmokePath `"$SmokePath`""
        )
    } | ConvertTo-Json -Depth 5
}

$repoRoot = Resolve-RepoRoot
$desktopDir = Join-Path $repoRoot "desktop"
$resolvedSmokePath = if ($SmokePath) {
    [System.IO.Path]::GetFullPath($SmokePath)
} else {
    Join-Path $repoRoot "docs\v$Version\win-installed-smoke.json"
}
$expectedInstaller = if ($InstallerPath) {
    [System.IO.Path]::GetFullPath($InstallerPath)
} else {
    Join-Path $repoRoot "desktop\release\EcoreX_${Version}_x64-setup.exe"
}

if ($DryRun) {
    Write-Plan -RepoRoot $repoRoot -Version $Version -InstallerPath $expectedInstaller -SmokePath $resolvedSmokePath -LaunchSimplySign ([bool]$LaunchSimplySign)
    return
}

$preflightArgs = @("-ExecutionPolicy", "Bypass", "-File", "scripts\sign-win.ps1", "-PreflightOnly")
if ($LaunchSimplySign) {
    $preflightArgs += "-LaunchSimplySign"
}
Invoke-Step -WorkingDirectory $desktopDir -FilePath "powershell" -ArgumentList $preflightArgs

if ($PreflightOnly) {
    [ordered]@{
        ok = $true
        step = "preflight"
        version = $Version
    } | ConvertTo-Json -Depth 4
    return
}

if (-not $SmokeOnly) {
    Invoke-Step -WorkingDirectory $desktopDir -FilePath "npm" -ArgumentList @("run", "package:win:signed")
}

$resolvedInstaller = Resolve-InstallerPath -RepoRoot $repoRoot -Version $Version -Path $InstallerPath
if ($PackageOnly) {
    [ordered]@{
        ok = $true
        step = "package"
        installer = $resolvedInstaller
        signatureStatus = [string](Get-AuthenticodeSignature -LiteralPath $resolvedInstaller).Status
    } | ConvertTo-Json -Depth 4
    return
}

Invoke-Step -WorkingDirectory $desktopDir -FilePath "npm" -ArgumentList @(
    "run",
    "smoke:win:installed",
    "--",
    "-InstallerPath",
    $resolvedInstaller,
    "-OutputPath",
    "..\docs\v$Version\win-installed-smoke.json"
)

if ($ImportManifest) {
    Invoke-Step -WorkingDirectory $repoRoot -FilePath "powershell" -ArgumentList @(
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        "scripts\update-ecorex-desktop-release-manifest.ps1",
        "-Version",
        $Version,
        "-WindowsInstallerPath",
        $resolvedInstaller,
        "-WindowsInstalledSmokePath",
        $resolvedSmokePath
    )
}

[ordered]@{
    ok = $true
    version = $Version
    installer = $resolvedInstaller
    smoke = $resolvedSmokePath
    importedManifest = [bool]$ImportManifest
} | ConvertTo-Json -Depth 4
