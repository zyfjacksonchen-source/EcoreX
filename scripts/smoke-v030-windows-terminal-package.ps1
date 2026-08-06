param(
    [Parameter(Mandatory = $true)][string]$PackagePath,
    [Parameter(Mandatory = $true)][string]$ReceiptPath
)

$ErrorActionPreference = "Stop"
if ($env:GITHUB_ACTIONS -cne "true" -or $env:RUNNER_OS -cne "Windows") {
    throw "windows_hosted_user_smoke_boundary_invalid"
}

$package = [IO.Path]::GetFullPath($PackagePath)
$receipt = [IO.Path]::GetFullPath($ReceiptPath)
$root = Join-Path $env:RUNNER_TEMP "emate-windows-user-smoke"
if (Test-Path -LiteralPath $root) { throw "windows_user_smoke_root_not_clean" }
New-Item -ItemType Directory -Path $root | Out-Null
$install = $null

try {
    $expanded = Join-Path $root "package"
    [IO.Compression.ZipFile]::ExtractToDirectory($package, $expanded)
    $installer = Join-Path $expanded "Install EcoreX WebUI.cmd"
    if (-not (Test-Path -LiteralPath $installer -PathType Leaf)) {
        throw "windows_user_installer_missing"
    }

    $desktop = [Environment]::GetFolderPath("Desktop")
    New-Item -ItemType Directory -Path $desktop -Force | Out-Null
    $install = Start-Process -FilePath $env:ComSpec -ArgumentList @(
        "/d", "/c", ('"' + $installer + '"')
    ) -PassThru -WindowStyle Hidden

    $version = $null
    $deadline = (Get-Date).AddMinutes(8)
    while ((Get-Date) -lt $deadline) {
        try {
            $version = Invoke-RestMethod -Uri "http://127.0.0.1:8765/api/version" -TimeoutSec 3
            break
        } catch {
            Start-Sleep -Seconds 1
        }
    }
    if ($install.HasExited -and $install.ExitCode -ne 0) {
        throw "windows_user_installer_failed"
    }
    if ($null -eq $version -or $version.product -ne "e-Mate" -or $version.version -ne "0.3.2") {
        throw "windows_user_runtime_version_invalid"
    }

    $entry = Join-Path $desktop "e-Mate.lnk"
    if (-not (Test-Path -LiteralPath $entry -PathType Leaf)) {
        throw "windows_user_shortcut_missing"
    }
    $shell = New-Object -ComObject WScript.Shell
    $shortcut = $shell.CreateShortcut($entry)
    if (-not (Test-Path -LiteralPath $shortcut.TargetPath -PathType Leaf) -or
        $shortcut.Arguments -notlike "*--launch-installed*" -or
        $shortcut.Arguments -notlike "*--install-root*") {
        throw "windows_user_shortcut_invalid"
    }

    $launch = Start-Process -FilePath $entry -PassThru
    if (-not $launch.WaitForExit(60000)) {
        Stop-Process -Id $launch.Id -Force -ErrorAction SilentlyContinue
        throw "windows_user_shortcut_launch_timeout"
    }
    $after = Invoke-RestMethod -Uri "http://127.0.0.1:8765/api/version" -TimeoutSec 5
    if ($after.product -ne "e-Mate" -or $after.version -ne "0.3.2") {
        throw "windows_user_shortcut_launch_invalid"
    }

    $result = [ordered]@{
        schema_version = 1
        status = "passed"
        product = "e-Mate"
        version = "0.3.2"
        architecture = "x64"
        package_sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $package).Hash.ToLowerInvariant()
        installed_runtime_api = $true
        desktop_entry_launch = $true
        generated_at = (Get-Date).ToUniversalTime().ToString("o")
    }
    New-Item -ItemType Directory -Path (Split-Path -Parent $receipt) -Force | Out-Null
    [IO.File]::WriteAllText(
        $receipt,
        (($result | ConvertTo-Json -Compress) + "`n"),
        [Text.UTF8Encoding]::new($false)
    )
} finally {
    if ($null -ne $install -and -not $install.HasExited) {
        & taskkill.exe /PID $install.Id /T /F 2>$null | Out-Null
    }
}
