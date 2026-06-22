param(
    [string]$OutputPath = "..\docs\v0.1.19\local-path-safety-smoke.json"
)

$ErrorActionPreference = "Stop"

function Write-Utf8NoBom {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Value
    )
    [System.IO.File]::WriteAllText($Path, $Value, [System.Text.UTF8Encoding]::new($false))
}

$desktopRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
if ([System.IO.Path]::IsPathRooted($OutputPath)) {
    $outputResolved = [System.IO.Path]::GetFullPath($OutputPath)
}
else {
    $outputResolved = [System.IO.Path]::GetFullPath((Join-Path $desktopRoot $OutputPath))
}

$probeRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("ecorex-symlink-preflight-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Force -Path $probeRoot | Out-Null
$preflight = [ordered]@{
    powershellSymlink = [ordered]@{
        ok = $false
        error = ""
    }
    mklinkSymlink = [ordered]@{
        ok = $false
        exitCode = $null
        output = ""
    }
    developerMode = $null
    elevated = $false
}
try {
    $target = Join-Path $probeRoot "target.ps1"
    $link = Join-Path $probeRoot "looks-safe.txt"
    Set-Content -LiteralPath $target -Value "Write-Host target" -Encoding UTF8
    New-Item -ItemType SymbolicLink -Path $link -Target $target -ErrorAction Stop | Out-Null
    $symlinkPrivilege = $true
    $preflight.powershellSymlink.ok = $true
}
catch {
    $symlinkPrivilege = $false
    $errorMessage = $_.Exception.Message
    $preflight.powershellSymlink.error = $errorMessage
}

try {
    $mklinkTarget = Join-Path $probeRoot "mklink-target.ps1"
    $mklinkLink = Join-Path $probeRoot "mklink-safe.txt"
    Set-Content -LiteralPath $mklinkTarget -Value "Write-Host mklink" -Encoding UTF8
    $mklinkOutput = cmd /c mklink "$mklinkLink" "$mklinkTarget" 2>&1
    $preflight.mklinkSymlink.exitCode = $LASTEXITCODE
    $preflight.mklinkSymlink.output = (($mklinkOutput | Out-String).Trim())
    $preflight.mklinkSymlink.ok = ($LASTEXITCODE -eq 0 -and (Test-Path -LiteralPath $mklinkLink))
    if (-not $symlinkPrivilege -and $preflight.mklinkSymlink.ok) {
        $symlinkPrivilege = $true
    }
}
catch {
    $preflight.mklinkSymlink.exitCode = $LASTEXITCODE
    $preflight.mklinkSymlink.output = $_.Exception.Message
}
finally {
    try {
        $devMode = Get-ItemProperty -LiteralPath "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\AppModelUnlock" -ErrorAction Stop
        $preflight.developerMode = [bool]($devMode.AllowDevelopmentWithoutDevLicense -eq 1)
    }
    catch {
        $preflight.developerMode = $null
    }
    try {
        $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
        $principal = [Security.Principal.WindowsPrincipal]::new($identity)
        $preflight.elevated = $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
    }
    catch {
        $preflight.elevated = $false
    }
    Remove-Item -LiteralPath $probeRoot -Recurse -Force -ErrorAction SilentlyContinue
}

Push-Location $desktopRoot
try {
    & npm run build:electron
    if ($LASTEXITCODE -ne 0) {
        throw "build:electron failed with exit $LASTEXITCODE"
    }
    & node scripts/smoke-local-path-safety.mjs $outputResolved
    if ($LASTEXITCODE -ne 0) {
        throw "local path safety smoke failed with exit $LASTEXITCODE"
    }
}
finally {
    Pop-Location
}

$evidence = Get-Content -Raw -Encoding UTF8 -LiteralPath $outputResolved | ConvertFrom-Json
$symlinkCheck = @($evidence.checks | Where-Object { $_.name -eq "symlink realpath guard" } | Select-Object -First 1)[0]
if (
    $evidence.status -eq "pass" -and
    $null -ne $symlinkCheck -and
    $symlinkCheck.status -eq "pass" -and
    $symlinkCheck.symlinkAvailable -eq $true
) {
    [ordered]@{
        ok = $true
        status = "pass"
        output = $outputResolved
        symlinkPrivilege = $symlinkPrivilege
        symlinkCheck = $symlinkCheck.status
        preflight = $preflight
    } | ConvertTo-Json -Depth 4
    exit 0
}

$recovery = @(
    "STAB-003 file symlink evidence is still required for production promotion.",
    "Current user cannot create a file symbolic link: $errorMessage",
    "Smoke status: $($evidence.status); symlink check: $($symlinkCheck.status); symlinkAvailable: $($symlinkCheck.symlinkAvailable)",
    "Run this smoke from an elevated PowerShell session, or enable Windows Developer Mode for unprivileged symlink creation, then rerun:",
    "  cd `"$desktopRoot`"",
    "  npm run smoke:local-path:symlink:win -- -OutputPath `"$OutputPath`"",
    "The script already wrote the partial evidence JSON to: $outputResolved"
) -join [Environment]::NewLine

$sidecar = [ordered]@{
    ok = $false
    status = [string]$evidence.status
    output = $outputResolved
    symlinkPrivilege = $symlinkPrivilege
    preflight = $preflight
    recovery = $recovery
}

$sidecarPath = [System.IO.Path]::ChangeExtension($outputResolved, ".symlink-preflight.json")
Write-Utf8NoBom -Path $sidecarPath -Value (($sidecar | ConvertTo-Json -Depth 5) + [Environment]::NewLine)
Write-Error $recovery
