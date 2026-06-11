param(
    [Parameter(Mandatory = $true)][string]$PackId,
    [string]$RuntimeDir = "",
    [string]$ManifestPath = "",
    [string]$IndexDir = ""
)

$ErrorActionPreference = "Stop"

if (-not $RuntimeDir) {
    $RuntimeDir = Join-Path $PSScriptRoot "..\runtime\ecorex-runtime"
}

$runtimeResolved = Resolve-Path -LiteralPath $RuntimeDir
$pythonExe = Join-Path $runtimeResolved "python\python.exe"
if (-not (Test-Path -LiteralPath $pythonExe)) {
    throw "EcoreX runtime python not found at $pythonExe"
}

if (-not $ManifestPath) {
    $runtimeManifest = Join-Path $runtimeResolved "capabilities.json"
    if (Test-Path -LiteralPath $runtimeManifest) {
        $ManifestPath = $runtimeManifest
    }
    else {
        $ManifestPath = Join-Path $PSScriptRoot "..\runtime-packs\capabilities.json"
    }
}

$scriptPath = Join-Path $PSScriptRoot "install-capability.py"
if (-not (Test-Path -LiteralPath $scriptPath)) {
    $scriptPath = Join-Path $runtimeResolved "scripts\install-capability.py"
}
if (-not (Test-Path -LiteralPath $scriptPath)) {
    throw "Capability installer not found"
}

$argsList = @(
    $scriptPath,
    "--pack-id", $PackId,
    "--runtime-dir", $runtimeResolved,
    "--manifest", (Resolve-Path -LiteralPath $ManifestPath)
)
if ($IndexDir) {
    $argsList += @("--index-dir", $IndexDir)
}

$previousNoUserSite = $env:PYTHONNOUSERSITE
$env:PYTHONNOUSERSITE = "1"
try {
    & $pythonExe @argsList
    exit $LASTEXITCODE
}
finally {
    $env:PYTHONNOUSERSITE = $previousNoUserSite
}
