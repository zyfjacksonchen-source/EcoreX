param(
    [string]$BaseUrl = "https://www.ecoreai.cn/ecorex-agent",
    [string]$Version = "",
    [switch]$NoBrowser
)

$ErrorActionPreference = "Stop"

try {
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
} catch {
}

function Join-Url {
    param([string]$Base, [string]$Path)
    if ($Path -match '^https?://') { return $Path }
    return ($Base.TrimEnd('/') + '/' + $Path.TrimStart('/'))
}

function Format-Mib {
    param([int64]$Bytes)
    return ("{0:N1} MiB" -f ($Bytes / 1MB))
}

function Get-Sha256 {
    param([string]$Path)
    return (Get-FileHash -Algorithm SHA256 -LiteralPath $Path).Hash.ToUpperInvariant()
}

function Test-ExpectedHash {
    param(
        [string]$Path,
        [string]$ExpectedSha256
    )
    if (-not (Test-Path -LiteralPath $Path)) { return $false }
    try {
        $actual = Get-Sha256 -Path $Path
        return ($actual -eq $ExpectedSha256.ToUpperInvariant())
    } catch {
        return $false
    }
}

function Write-DownloadStatus {
    param(
        [int64]$Downloaded,
        [int64]$Total
    )
    if ($Total -gt 0) {
        $percent = [Math]::Min(100, [Math]::Round(($Downloaded * 100.0) / $Total, 1))
        Write-Progress -Activity "Downloading EcoreX WebUI" -Status "$(Format-Mib $Downloaded) / $(Format-Mib $Total)" -PercentComplete $percent
        Write-Host ("Downloaded {0} / {1} ({2}%)" -f (Format-Mib $Downloaded), (Format-Mib $Total), $percent)
    } else {
        Write-Progress -Activity "Downloading EcoreX WebUI" -Status "$(Format-Mib $Downloaded)"
        Write-Host ("Downloaded {0}" -f (Format-Mib $Downloaded))
    }
}

function Save-UrlWithProgress {
    param(
        [Parameter(Mandatory = $true)][string]$Uri,
        [Parameter(Mandatory = $true)][string]$CachePath,
        [Parameter(Mandatory = $true)][string]$WorkDir,
        [Parameter(Mandatory = $true)][string]$ExpectedSha256,
        [int]$Retries = 3
    )

    if (Test-ExpectedHash -Path $CachePath -ExpectedSha256 $ExpectedSha256) {
        Write-Host "Using cached package: $CachePath"
        return $CachePath
    }

    $cacheDir = Split-Path -Parent $CachePath
    New-Item -ItemType Directory -Force -Path $cacheDir, $WorkDir | Out-Null
    $partialPath = "$CachePath.part"

    for ($attempt = 1; $attempt -le $Retries; $attempt++) {
        try {
            $resumeFrom = [int64]0
            if (Test-Path -LiteralPath $partialPath) {
                $resumeFrom = (Get-Item -LiteralPath $partialPath).Length
            }
            Write-Host "Downloading $Uri"
            Write-Host "Attempt $attempt of $Retries"
            if ($resumeFrom -gt 0) {
                Write-Host ("Resuming from {0}" -f (Format-Mib $resumeFrom))
            }

            $request = [System.Net.HttpWebRequest][System.Net.WebRequest]::Create($Uri)
            $request.Method = "GET"
            $request.UserAgent = "EcoreX-WebUI-Installer/0.2.0"
            $request.Timeout = 30000
            $request.ReadWriteTimeout = 30000
            $request.AllowAutoRedirect = $true
            if ($resumeFrom -gt 0) {
                $request.AddRange($resumeFrom)
            }

            try {
                $response = $request.GetResponse()
            } catch [System.Net.WebException] {
                $webResponse = $_.Exception.Response
                $statusCode = if ($webResponse) { [int]$webResponse.StatusCode } else { 0 }
                if ($resumeFrom -gt 0 -and $statusCode -eq 416) {
                    if ($webResponse) { $webResponse.Dispose() }
                    try {
                        $actual = Get-Sha256 -Path $partialPath
                        if ($actual -eq $ExpectedSha256.ToUpperInvariant()) {
                            Move-Item -LiteralPath $partialPath -Destination $CachePath -Force
                            Write-Host "Partial package was already complete; verified SHA256: $CachePath"
                            return $CachePath
                        }
                    } catch {
                    }
                    Remove-Item -LiteralPath $partialPath -Force -ErrorAction SilentlyContinue
                    throw "Server rejected the resume range; the stale partial package was removed and the next attempt will restart from byte 0."
                }
                if ($webResponse) { $webResponse.Dispose() }
                throw
            }
            if ($resumeFrom -gt 0 -and [int]$response.StatusCode -ne 206) {
                $response.Dispose()
                Remove-Item -LiteralPath $partialPath -Force -ErrorAction SilentlyContinue
                throw "Server did not resume the partial download; retrying from the beginning."
            }
            try {
                $contentLength = [int64]$response.ContentLength
                $total = if ($resumeFrom -gt 0 -and $contentLength -gt 0) { $resumeFrom + $contentLength } else { $contentLength }
                $stream = $response.GetResponseStream()
                $fileMode = if ($resumeFrom -gt 0) { [System.IO.FileMode]::Append } else { [System.IO.FileMode]::Create }
                $file = [System.IO.File]::Open($partialPath, $fileMode, [System.IO.FileAccess]::Write, [System.IO.FileShare]::None)
                try {
                    $buffer = New-Object byte[] (1024 * 1024)
                    $downloaded = $resumeFrom
                    $lastStatus = [DateTime]::UtcNow.AddSeconds(-2)
                    while ($true) {
                        $read = $stream.Read($buffer, 0, $buffer.Length)
                        if ($read -le 0) { break }
                        $file.Write($buffer, 0, $read)
                        $downloaded += $read

                        $now = [DateTime]::UtcNow
                        if (($now - $lastStatus).TotalMilliseconds -ge 1000) {
                            Write-DownloadStatus -Downloaded $downloaded -Total $total
                            $lastStatus = $now
                        }
                    }
                    Write-DownloadStatus -Downloaded $downloaded -Total $total
                    Write-Progress -Activity "Downloading EcoreX WebUI" -Completed
                } finally {
                    $file.Dispose()
                    $stream.Dispose()
                }
            } finally {
                $response.Dispose()
            }

            Write-Host "Verifying SHA256..."
            $actual = Get-Sha256 -Path $partialPath
            if ($actual -ne $ExpectedSha256.ToUpperInvariant()) {
                Remove-Item -LiteralPath $partialPath -Force -ErrorAction SilentlyContinue
                throw "SHA256 mismatch for downloaded package: $actual"
            }
            Move-Item -LiteralPath $partialPath -Destination $CachePath -Force
            Write-Host "Download verified: $CachePath"
            return $CachePath
        } catch {
            Write-Progress -Activity "Downloading EcoreX WebUI" -Completed
            if ($attempt -ge $Retries) {
                throw
            }
            Write-Warning "Download failed: $($_.Exception.Message)"
            Write-Warning "Partial downloads are kept at $partialPath and the installer will resume when possible."
            Start-Sleep -Seconds ([Math]::Min(10, 2 * $attempt))
        }
    }
}

$manifestUrl = Join-Url $BaseUrl "manifest.json"
Write-Host "Fetching EcoreX manifest: $manifestUrl"
$manifest = Invoke-RestMethod -Uri $manifestUrl -UseBasicParsing -TimeoutSec 30
Write-Host "EcoreX WebUI installer script: 0.2.0"
Write-Host "EcoreX WebUI manifest version: $($manifest.version)"
if ($Version -and [string]$manifest.version -ne $Version) {
    throw "Manifest version '$($manifest.version)' does not match requested '$Version'."
}

$artifact = @($manifest.artifacts) | Where-Object { $_.id -eq "webui-windows-x64" -and $_.status -eq "ready" } | Select-Object -First 1
if (-not $artifact) {
    throw "Ready webui-windows-x64 artifact was not found in manifest."
}

$localAppData = $env:LOCALAPPDATA
if (-not $localAppData) {
    $localAppData = [System.IO.Path]::GetTempPath()
}
$cacheRoot = Join-Path (Join-Path $localAppData "EcoreX WebUI") "downloads"
$zipPath = Join-Path $cacheRoot $artifact.fileName
$tempRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("ecorex-webui-install-" + [Guid]::NewGuid().ToString("N"))
$extractRoot = Join-Path $tempRoot "extract"
New-Item -ItemType Directory -Force -Path $tempRoot, $extractRoot, $cacheRoot | Out-Null

try {
    $artifactUrl = Join-Url $BaseUrl $artifact.href
    $packagePath = Save-UrlWithProgress -Uri $artifactUrl -CachePath $zipPath -WorkDir $tempRoot -ExpectedSha256 ([string]$artifact.sha256)

    Write-Host "Extracting package..."
    Expand-Archive -LiteralPath $packagePath -DestinationPath $extractRoot -Force
    Write-Host "Package extracted."

    $installer = Get-ChildItem -LiteralPath $extractRoot -Recurse -Filter "install-ecorex-webui-win.ps1" | Select-Object -First 1
    if (-not $installer) {
        throw "Windows WebUI installer was not found in the downloaded package."
    }

    Write-Host "Starting EcoreX WebUI local installer..."
    $args = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $installer.FullName)
    if ($NoBrowser) { $args += "-NoBrowser" }
    & powershell @args
    if ($LASTEXITCODE -ne 0) {
        throw "EcoreX WebUI installer failed with exit code $LASTEXITCODE."
    }
    Write-Host "EcoreX WebUI install command finished. If the browser did not open, double-click the desktop EcoreX WebUI.url shortcut or rerun this command."
} finally {
    Remove-Item -LiteralPath $tempRoot -Recurse -Force -ErrorAction SilentlyContinue
}
