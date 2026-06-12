param(
    [string]$InstallerPath = "$PSScriptRoot\..\release\EcoreX_0.1.11_x64-setup.exe",
    [string]$InstallDir = "",
    [int]$Port = 19131,
    [switch]$KeepInstall
)

$ErrorActionPreference = "Stop"

function Resolve-FullPath {
    param([Parameter(Mandatory = $true)][string]$Path)
    return [System.IO.Path]::GetFullPath($Path)
}

function Stop-ProcessesFromInstallDir {
    param([Parameter(Mandatory = $true)][string]$Dir)

    $needle = $Dir.Replace("\", "\\")
    Get-CimInstance Win32_Process |
        Where-Object {
            $_.CommandLine -and (
                $_.CommandLine.Contains($Dir) -or $_.CommandLine.Contains($needle)
            )
        } |
        ForEach-Object {
            try {
                Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
            }
            catch {}
        }
}

function Wait-ForSidecar {
    param(
        [Parameter(Mandatory = $true)][int]$WebPort,
        [int]$TimeoutSeconds = 60
    )

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    $lastError = ""
    while ((Get-Date) -lt $deadline) {
        try {
            $response = Invoke-WebRequest -UseBasicParsing "http://127.0.0.1:$WebPort/auth/check" -TimeoutSec 5
            if ($response.StatusCode -eq 200) {
                return $response.Content
            }
        }
        catch {
            $lastError = $_.Exception.Message
        }
        Start-Sleep -Seconds 2
    }
    throw "Sidecar did not become ready on port $WebPort. Last error: $lastError"
}

$installerResolved = Resolve-Path -LiteralPath $InstallerPath -ErrorAction Stop
$installerSignature = Get-AuthenticodeSignature -LiteralPath $installerResolved
if ($installerSignature.Status -ne "Valid") {
    throw "Installer signature is not valid: $($installerSignature.StatusMessage)"
}

if (-not $InstallDir) {
    $InstallDir = Join-Path ([System.IO.Path]::GetTempPath()) ("EcoreX-smoke-" + [System.Guid]::NewGuid().ToString("N"))
}
$installResolved = Resolve-FullPath -Path $InstallDir
$tempRoot = Resolve-FullPath -Path ([System.IO.Path]::GetTempPath())
$isSafeTempInstall = $installResolved.StartsWith($tempRoot, [System.StringComparison]::OrdinalIgnoreCase) -and
    ((Split-Path -Leaf $installResolved) -like "EcoreX-smoke-*")

if (Test-Path -LiteralPath $installResolved) {
    throw "InstallDir already exists: $installResolved"
}

$result = [ordered]@{
    installer = [string]$installerResolved
    installDir = $installResolved
    webPort = $Port
    installed = $false
    appStarted = $false
    sidecarReady = $false
    cleaned = $false
}

try {
    $installArgs = @("/S", "/D=$installResolved")
    $install = Start-Process -FilePath $installerResolved -ArgumentList $installArgs -Wait -PassThru -WindowStyle Hidden
    if ($install.ExitCode -ne 0) {
        throw "Installer failed with exit code $($install.ExitCode)"
    }
    $result.installed = $true

    $appExe = Join-Path $installResolved "EcoreX.exe"
    $runtimePython = Join-Path $installResolved "resources\ecorex-runtime\python\python.exe"
    $runtimeApp = Join-Path $installResolved "resources\ecorex-runtime\app.py"
    $capabilityManifest = Join-Path $installResolved "resources\ecorex-runtime\capabilities.json"

    foreach ($path in @($appExe, $runtimePython, $runtimeApp, $capabilityManifest)) {
        if (-not (Test-Path -LiteralPath $path)) {
            throw "Expected installed file not found: $path"
        }
    }

    foreach ($signedPath in @($appExe, $runtimePython)) {
        $signature = Get-AuthenticodeSignature -LiteralPath $signedPath
        if ($signature.Status -ne "Valid") {
            throw "Installed binary signature is not valid: $signedPath - $($signature.StatusMessage)"
        }
    }

    $previousPort = $env:ECOREX_WEB_PORT
    $previousSkip = $env:ECOREX_SKIP_SIDECAR
    $env:ECOREX_WEB_PORT = [string]$Port
    Remove-Item Env:ECOREX_SKIP_SIDECAR -ErrorAction SilentlyContinue
    try {
        $app = Start-Process -FilePath $appExe -PassThru -WindowStyle Hidden
        $result.appStarted = $true
        $body = Wait-ForSidecar -WebPort $Port -TimeoutSeconds 75
        if ($body -notmatch '"status"\s*:\s*"success"') {
            throw "Unexpected sidecar response: $body"
        }
        $result.sidecarReady = $true
        $result.sidecarBody = $body
    }
    finally {
        if ($app -and -not $app.HasExited) {
            Stop-Process -Id $app.Id -Force -ErrorAction SilentlyContinue
        }
        Stop-ProcessesFromInstallDir -Dir $installResolved
        $env:ECOREX_WEB_PORT = $previousPort
        if ($null -eq $previousSkip) {
            Remove-Item Env:ECOREX_SKIP_SIDECAR -ErrorAction SilentlyContinue
        }
        else {
            $env:ECOREX_SKIP_SIDECAR = $previousSkip
        }
    }
}
finally {
    if (-not $KeepInstall -and (Test-Path -LiteralPath $installResolved)) {
        Stop-ProcessesFromInstallDir -Dir $installResolved
        $uninstaller = Join-Path $installResolved "Uninstall EcoreX.exe"
        if (Test-Path -LiteralPath $uninstaller) {
            $uninstall = Start-Process -FilePath $uninstaller -ArgumentList "/S" -Wait -PassThru -WindowStyle Hidden
            if ($uninstall.ExitCode -ne 0) {
                Write-Warning "Uninstaller returned exit code $($uninstall.ExitCode)"
            }
        }
        if ($isSafeTempInstall -and (Test-Path -LiteralPath $installResolved)) {
            Remove-Item -LiteralPath $installResolved -Recurse -Force -ErrorAction SilentlyContinue
        }
        $result.cleaned = -not (Test-Path -LiteralPath $installResolved)
    }
}

$result | ConvertTo-Json -Depth 6
