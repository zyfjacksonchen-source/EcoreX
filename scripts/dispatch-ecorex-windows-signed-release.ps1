param(
    [string]$Version = "0.1.19",
    [string]$InstallerPath = "",
    [string]$InstallerIa32Path = "",
    [string]$SmokePath = "",
    [string]$SmokeIa32Path = "",
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
        [ValidateSet("x64", "ia32")][string]$Arch = "x64",
        [string]$Path = ""
    )
    if ($Path) {
        if (-not (Test-Path -LiteralPath $Path)) {
            throw "Installer path does not exist: $Path"
        }
        return (Resolve-Path -LiteralPath $Path).Path
    }
    $default = Join-Path $RepoRoot "desktop\release\EcoreX_${Version}_${Arch}-setup.exe"
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
        [Parameter(Mandatory = $true)][string]$InstallerIa32Path,
        [Parameter(Mandatory = $true)][string]$SmokePath,
        [Parameter(Mandatory = $true)][string]$SmokeIa32Path,
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
            "npm run smoke:win:installed -- -InstallerPath `"$InstallerPath`" -OutputPath `"..\docs\v$Version\win-installed-smoke.json`" -ExpectedVersion $Version",
            "npm run smoke:win:installed -- -InstallerPath `"$InstallerIa32Path`" -OutputPath `"..\docs\v$Version\win-ia32-installed-smoke.json`" -ExpectedVersion $Version -ExpectedWinArch ia32",
            "cd `"$RepoRoot`"",
            "powershell -ExecutionPolicy Bypass -File scripts\update-ecorex-desktop-release-manifest.ps1 -Version $Version -WindowsInstallerPath `"$InstallerPath`" -WindowsInstalledSmokePath `"$SmokePath`" -WindowsIa32InstallerPath `"$InstallerIa32Path`" -WindowsIa32InstalledSmokePath `"$SmokeIa32Path`""
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
$resolvedSmokeIa32Path = if ($SmokeIa32Path) {
    [System.IO.Path]::GetFullPath($SmokeIa32Path)
} else {
    Join-Path $repoRoot "docs\v$Version\win-ia32-installed-smoke.json"
}
$expectedInstaller = if ($InstallerPath) {
    [System.IO.Path]::GetFullPath($InstallerPath)
} else {
    Join-Path $repoRoot "desktop\release\EcoreX_${Version}_x64-setup.exe"
}
$expectedInstallerIa32 = if ($InstallerIa32Path) {
    [System.IO.Path]::GetFullPath($InstallerIa32Path)
} else {
    Join-Path $repoRoot "desktop\release\EcoreX_${Version}_ia32-setup.exe"
}

if ($DryRun) {
    Write-Plan -RepoRoot $repoRoot -Version $Version -InstallerPath $expectedInstaller -InstallerIa32Path $expectedInstallerIa32 -SmokePath $resolvedSmokePath -SmokeIa32Path $resolvedSmokeIa32Path -LaunchSimplySign ([bool]$LaunchSimplySign)
    return
}

$preflightArgs = @("-ExecutionPolicy", "Bypass", "-File", "scripts\sign-win.ps1", "-PreflightOnly")
if ($LaunchSimplySign) {
    $preflightArgs += "-LaunchSimplySign"
}
try {
    Invoke-Step -WorkingDirectory $desktopDir -FilePath "powershell" -ArgumentList $preflightArgs
}
catch {
    if ($PreflightOnly) {
        throw
    }
    Write-Warning "Windows signing provider preflight failed; continuing to the actual signing steps because signtool may still be able to access the certificate. $($_.Exception.Message)"
}

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

$resolvedInstaller = Resolve-InstallerPath -RepoRoot $repoRoot -Version $Version -Arch "x64" -Path $InstallerPath
$resolvedInstallerIa32 = Resolve-InstallerPath -RepoRoot $repoRoot -Version $Version -Arch "ia32" -Path $InstallerIa32Path
if ($PackageOnly) {
    [ordered]@{
        ok = $true
        step = "package"
        installer = $resolvedInstaller
        installerIa32 = $resolvedInstallerIa32
        signatureStatus = [string](Get-AuthenticodeSignature -LiteralPath $resolvedInstaller).Status
        signatureIa32Status = [string](Get-AuthenticodeSignature -LiteralPath $resolvedInstallerIa32).Status
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
    $resolvedSmokePath,
    "-ExpectedVersion",
    $Version,
    "-ExpectedWinArch",
    "x64"
)

Invoke-Step -WorkingDirectory $desktopDir -FilePath "npm" -ArgumentList @(
    "run",
    "smoke:win:installed",
    "--",
    "-InstallerPath",
    $resolvedInstallerIa32,
    "-OutputPath",
    $resolvedSmokeIa32Path,
    "-ExpectedVersion",
    $Version,
    "-ExpectedWinArch",
    "ia32"
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
        $resolvedSmokePath,
        "-WindowsIa32InstallerPath",
        $resolvedInstallerIa32,
        "-WindowsIa32InstalledSmokePath",
        $resolvedSmokeIa32Path
    )
}

[ordered]@{
    ok = $true
    version = $Version
    installer = $resolvedInstaller
    installerIa32 = $resolvedInstallerIa32
    smoke = $resolvedSmokePath
    smokeIa32 = $resolvedSmokeIa32Path
    importedManifest = [bool]$ImportManifest
} | ConvertTo-Json -Depth 4
