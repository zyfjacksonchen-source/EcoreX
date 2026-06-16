param(
    [string]$ArtifactsDir = "$PSScriptRoot\..\release",
    [string]$SignToolDir = "",
    [string]$SimplySignShortcut = "C:\Users\user\Desktop\SimplySign Desktop.lnk",
    [string]$Thumbprint = "0f678477dfc0a2bdaab88307126ef657faf8674f",
    [switch]$CoreAppOnly,
    [switch]$SetupOnly,
    [switch]$LaunchSimplySign,
    [switch]$PreflightOnly,
    [switch]$SkipProviderPreflight
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

function Quote-ExternalArg {
    param([Parameter(Mandatory = $true)][string]$Value)
    if ($Value -match '[\s"]') {
        return '"' + ($Value -replace '"', '\"') + '"'
    }
    return $Value
}

function Invoke-ExternalCapture {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [string[]]$ArgumentList = @(),
        [int]$TimeoutSeconds = 20
    )
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $FilePath
    $psi.Arguments = (($ArgumentList | ForEach-Object { Quote-ExternalArg $_ }) -join " ")
    $psi.UseShellExecute = $false
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $process = New-Object System.Diagnostics.Process
    $process.StartInfo = $psi
    [void]$process.Start()
    if (-not $process.WaitForExit($TimeoutSeconds * 1000)) {
        try { $process.Kill() } catch {}
        return [pscustomobject]@{
            ExitCode = -1
            StdOut = ""
            StdErr = "Timed out after $TimeoutSeconds second(s)."
            TimedOut = $true
        }
    }
    return [pscustomobject]@{
        ExitCode = $process.ExitCode
        StdOut = $process.StandardOutput.ReadToEnd()
        StdErr = $process.StandardError.ReadToEnd()
        TimedOut = $false
    }
}

function Assert-SigningProviderReady {
    param([Parameter(Mandatory = $true)][string]$ExpectedThumbprint)

    $normalized = ($ExpectedThumbprint -replace '\s', '').ToUpperInvariant()
    $cert = Get-ChildItem Cert:\CurrentUser\My -ErrorAction Stop |
        Where-Object { ($_.Thumbprint -replace '\s', '').ToUpperInvariant() -eq $normalized } |
        Select-Object -First 1
    if (-not $cert) {
        throw "Signing certificate $ExpectedThumbprint was not found in Cert:\CurrentUser\My."
    }
    if (-not $cert.HasPrivateKey) {
        throw "Signing certificate $ExpectedThumbprint is present but HasPrivateKey is false."
    }

    $smartCard = Get-Service SCardSvr -ErrorAction SilentlyContinue
    $certProp = Get-Service CertPropSvc -ErrorAction SilentlyContinue
    $smartCardStatus = if ($smartCard) { $smartCard.Status.ToString() } else { "not-found" }
    $certPropStatus = if ($certProp) { $certProp.Status.ToString() } else { "not-found" }

    $keyList = Invoke-ExternalCapture -FilePath "certutil.exe" -ArgumentList @("-user", "-key", "-csp", "SimplySign CSP") -TimeoutSeconds 25
    $combined = (($keyList.StdOut, $keyList.StdErr) -join "`n")
    $meaningfulLines = @($combined -split "\r?\n" | Where-Object {
        $line = $_.Trim()
        $line -and $line -notmatch '^CertUtil:'
    })
    if ($keyList.TimedOut -or $meaningfulLines.Count -eq 0) {
        throw @"
Signing provider preflight failed.
Certificate: $($cert.Subject)
Thumbprint: $($cert.Thumbprint)
Smart Card service: $smartCardStatus
Certificate Propagation service: $certPropStatus
SimplySign CSP key containers: none visible

Recovery:
1. Open/unlock Certum SimplySign Desktop and proCertum SmartSign, including any required PIN/login.
2. In an elevated shell, start the services if needed:
   Start-Service SCardSvr
   Start-Service CertPropSvc
3. Re-run:
   powershell -ExecutionPolicy Bypass -File desktop\scripts\sign-win.ps1 -PreflightOnly

Raw certutil output:
$combined
"@
    }

    Write-Host "Signing provider preflight passed for $($cert.Subject)"
    Write-Host "Smart Card service: $smartCardStatus; Certificate Propagation service: $certPropStatus"
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

$unsignedTargets = @($targets | Where-Object { (Get-AuthenticodeSignature -LiteralPath $_.FullName).Status -ne "Valid" })
if ($PreflightOnly -or (($unsignedTargets.Count -gt 0) -and -not $SkipProviderPreflight)) {
    Assert-SigningProviderReady -ExpectedThumbprint $Thumbprint
    if ($PreflightOnly) {
        return
    }
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
