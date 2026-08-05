param(
    [string]$PackagePath = "release-artifacts/EcoreX_0.3.1-webui-windows-x64.zip",
    [string]$OutputPath = "docs/v0.3.1/artifacts/webui-package-runtime-smoke.json",
    [string]$SmokeRoot = "tmp/v030-webui-package-smoke",
    [string]$ExpectedVersion = "0.3.1",
    [int]$Port = 9929,
    [int]$TimeoutSeconds = 90
)

$ErrorActionPreference = "Stop"

function Resolve-FullPath {
    param([Parameter(Mandatory = $true)][string]$Path)
    if ([System.IO.Path]::IsPathRooted($Path)) {
        return [System.IO.Path]::GetFullPath($Path)
    }
    return [System.IO.Path]::GetFullPath((Join-Path (Get-Location) $Path))
}

function Assert-PathInside {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Base
    )
    $full = [System.IO.Path]::GetFullPath($Path)
    $baseFull = [System.IO.Path]::GetFullPath($Base).TrimEnd("\", "/")
    if (-not ($full.Equals($baseFull, [System.StringComparison]::OrdinalIgnoreCase) -or $full.StartsWith($baseFull + [System.IO.Path]::DirectorySeparatorChar, [System.StringComparison]::OrdinalIgnoreCase))) {
        throw "Refusing to operate outside expected base. Path=$full Base=$baseFull"
    }
    return $full
}

function Get-FileSha256Upper {
    param([Parameter(Mandatory = $true)][string]$Path)
    return (Get-FileHash -Algorithm SHA256 -LiteralPath $Path).Hash.ToUpperInvariant()
}

function Write-Utf8NoBom {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Value
    )
    $parent = Split-Path -Parent $Path
    if ($parent) {
        New-Item -ItemType Directory -Force -Path $parent | Out-Null
    }
    $encoding = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($Path, ($Value -replace "`r`n", "`n") + "`n", $encoding)
}

function Get-RelativePathCompat {
    param(
        [Parameter(Mandatory = $true)][string]$Base,
        [Parameter(Mandatory = $true)][string]$Path
    )
    $baseFull = [System.IO.Path]::GetFullPath($Base).TrimEnd("\", "/") + [System.IO.Path]::DirectorySeparatorChar
    $pathFull = [System.IO.Path]::GetFullPath($Path)
    $baseUri = New-Object System.Uri($baseFull)
    $pathUri = New-Object System.Uri($pathFull)
    $relativeUri = $baseUri.MakeRelativeUri($pathUri)
    return [System.Uri]::UnescapeDataString($relativeUri.ToString()).Replace("/", [System.IO.Path]::DirectorySeparatorChar)
}

function Wait-Http {
    param(
        [Parameter(Mandatory = $true)][string]$Url,
        [Parameter(Mandatory = $true)][int]$TimeoutSeconds
    )
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    $lastError = ""
    while ((Get-Date) -lt $deadline) {
        try {
            $response = Invoke-WebRequest -UseBasicParsing -Uri $Url -TimeoutSec 5
            if ($response.StatusCode -lt 500) {
                return $response
            }
        }
        catch {
            $lastError = $_.Exception.Message
        }
        Start-Sleep -Milliseconds 800
    }
    throw "Endpoint did not become ready: $Url. Last error: $lastError"
}

$repoRoot = Resolve-FullPath "."
$tmpRoot = Resolve-FullPath "tmp"
$packageResolved = Resolve-FullPath $PackagePath
$smokeRootResolved = Assert-PathInside -Path (Resolve-FullPath $SmokeRoot) -Base $tmpRoot
$outputResolved = Resolve-FullPath $OutputPath

if (-not (Test-Path -LiteralPath $packageResolved)) {
    throw "Package not found: $packageResolved"
}

if (Test-Path -LiteralPath $smokeRootResolved) {
    Remove-Item -LiteralPath $smokeRootResolved -Recurse -Force
}
New-Item -ItemType Directory -Force -Path $smokeRootResolved | Out-Null

Add-Type -AssemblyName System.IO.Compression.FileSystem
[System.IO.Compression.ZipFile]::ExtractToDirectory($packageResolved, $smokeRootResolved)

$runtimeDir = Join-Path $smokeRootResolved "runtime"
$appPy = Join-Path $runtimeDir "app.py"
$runtimeManifest = Join-Path $runtimeDir "runtime-manifest.json"
$releaseJsonPath = Join-Path $smokeRootResolved "release.json"
if (-not (Test-Path -LiteralPath $appPy)) {
    throw "Extracted runtime app.py missing: $appPy"
}
if (-not (Test-Path -LiteralPath $runtimeManifest)) {
    throw "Extracted runtime-manifest.json missing: $runtimeManifest"
}

$stateDir = Join-Path $smokeRootResolved "state"
$workspaceDir = Join-Path $smokeRootResolved "workspace"
New-Item -ItemType Directory -Force -Path $stateDir, $workspaceDir | Out-Null

$config = [ordered]@{
    cow_lang = "auto"
    channel_type = "web"
    web_console = $true
    web_host = "127.0.0.1"
    web_port = $Port
    web_password = ""
    web_auto_open = $false
    agent = $true
    self_evolution_enabled = $false
    scheduler_enabled = $false
    mcp_auto_start = $false
    agent_workspace = $workspaceDir
    web_file_serve_root = $workspaceDir
    appdata_dir = (Join-Path $stateDir "appdata")
    use_linkai = $false
    debug = $false
    tools = @{
        browser = @{
            cdp_endpoint = "http://127.0.0.1:9222"
            cdp_auto_launch = $false
            cdp_fallback = $true
            persistent = $true
        }
    }
}
Write-Utf8NoBom -Path (Join-Path $runtimeDir "config.json") -Value ($config | ConvertTo-Json -Depth 10)

$pythonExe = Join-Path $runtimeDir "python\python.exe"
if (-not (Test-Path -LiteralPath $pythonExe)) {
    $pythonCmd = Get-Command python -ErrorAction Stop
    $pythonExe = $pythonCmd.Source
}

$stdoutLogPath = Join-Path $runtimeDir "run.stdout.log"
$stderrLogPath = Join-Path $runtimeDir "run.stderr.log"
$process = $null
$result = [ordered]@{
    schema = "ecorex.v0.3.1.package-runtime-smoke.v1"
    version = $ExpectedVersion
    status = "fail"
    generatedAt = (Get-Date).ToUniversalTime().ToString("o")
    port = $Port
    package = Get-RelativePathCompat -Base $repoRoot -Path $packageResolved
    packageSha256 = Get-FileSha256Upper $packageResolved
    packageSizeBytes = (Get-Item -LiteralPath $packageResolved).Length
    smokeRoot = Get-RelativePathCompat -Base $repoRoot -Path $smokeRootResolved
    runtimeDir = Get-RelativePathCompat -Base $repoRoot -Path $runtimeDir
    stdoutLog = Get-RelativePathCompat -Base $repoRoot -Path $stdoutLogPath
    stderrLog = Get-RelativePathCompat -Base $repoRoot -Path $stderrLogPath
    redacted = $true
}

try {
    $oldWebNoBrowser = $env:ECOREX_WEB_NO_BROWSER
    $env:ECOREX_WEB_NO_BROWSER = "1"
    try {
        $process = Start-Process -FilePath $pythonExe -ArgumentList @($appPy) -WorkingDirectory $runtimeDir -PassThru -WindowStyle Hidden -RedirectStandardOutput $stdoutLogPath -RedirectStandardError $stderrLogPath
    } finally {
        if ($null -eq $oldWebNoBrowser) {
            Remove-Item Env:\ECOREX_WEB_NO_BROWSER -ErrorAction SilentlyContinue
        } else {
            $env:ECOREX_WEB_NO_BROWSER = $oldWebNoBrowser
        }
    }
    $result.processId = $process.Id

    $versionResponse = Wait-Http -Url "http://127.0.0.1:$Port/api/version" -TimeoutSeconds $TimeoutSeconds
    $versionJson = $versionResponse.Content | ConvertFrom-Json
    $result.apiVersionStatus = [int]$versionResponse.StatusCode
    $result.runtimeVersion = [string]$versionJson.version
    if ($result.runtimeVersion -ne $ExpectedVersion) {
        throw "Unexpected runtime version: $($result.runtimeVersion)"
    }

    $appResponse = Wait-Http -Url "http://127.0.0.1:$Port/app/" -TimeoutSeconds 15
    $result.appStatus = [int]$appResponse.StatusCode
    $result.appContainsRoot = ($appResponse.Content -match 'id="root"')
    if (-not $result.appContainsRoot) {
        throw "App response did not contain the renderer root."
    }

    $manifestJson = Get-Content -Raw -Encoding UTF8 -LiteralPath $runtimeManifest | ConvertFrom-Json
    $result.runtimeManifestVersion = [string]$manifestJson.version
    $result.runtimeManifestPlatform = [string]$manifestJson.platform
    if ($releaseJsonPath -and (Test-Path -LiteralPath $releaseJsonPath)) {
        $releaseJson = Get-Content -Raw -Encoding UTF8 -LiteralPath $releaseJsonPath | ConvertFrom-Json
        $result.releaseJsonVersion = [string]$releaseJson.version
        $result.releaseJsonPlatform = [string]$releaseJson.platform
    }
    $result.status = "pass"
}
finally {
    if ($process -and -not $process.HasExited) {
        Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
    }
    $result.cleaned = $true
    Write-Utf8NoBom -Path $outputResolved -Value ($result | ConvertTo-Json -Depth 10)
}

$result | ConvertTo-Json -Depth 10
