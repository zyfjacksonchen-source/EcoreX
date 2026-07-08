param(
    [string]$BaseUrl = "https://dl.ecoremedia.net/ecorex-agent",
    [string[]]$DownloadBaseUrls = @(),
    [string[]]$AssetDownloadBaseUrls = @(),
    [string]$Version = "",
    [int]$Port = 9909,
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

function Add-DownloadBaseUrl {
    param(
        [Parameter(Mandatory = $true)][AllowEmptyCollection()][System.Collections.ArrayList]$List,
        [string]$Url
    )
    $clean = ([string]$Url).Trim().TrimEnd("/")
    if (-not $clean -or $clean -notmatch '^https?://') {
        return
    }
    if (-not $List.Contains($clean)) {
        [void]$List.Add($clean)
    }
}

function Add-DownloadBaseUrls {
    param(
        [Parameter(Mandatory = $true)][AllowEmptyCollection()][System.Collections.ArrayList]$List,
        $Values
    )
    foreach ($value in @($Values)) {
        if ($null -eq $value) { continue }
        foreach ($part in ([string]$value -split "[,;`r`n]+")) {
            Add-DownloadBaseUrl -List $List -Url $part
        }
    }
}

function Add-DownloadUrlForBase {
    param(
        [Parameter(Mandatory = $true)][AllowEmptyCollection()][System.Collections.ArrayList]$List,
        [string]$BaseUrl,
        [string]$PathMode,
        [Parameter(Mandatory = $true)]$Artifact
    )
    $clean = ([string]$BaseUrl).Trim().TrimEnd("/")
    if (-not $clean -or $clean -notmatch '^https?://') {
        return
    }
    $mode = ([string]$PathMode).Trim()
    $path = if ($mode -ieq "fileName") { [string]$Artifact.fileName } else { [string]$Artifact.href }
    if (-not $path) {
        return
    }
    Add-DownloadBaseUrl -List $List -Url (Join-Url $clean $path)
}

function Add-DownloadUrlsForBases {
    param(
        [Parameter(Mandatory = $true)][AllowEmptyCollection()][System.Collections.ArrayList]$List,
        $Values,
        [string]$PathMode,
        [Parameter(Mandatory = $true)]$Artifact
    )
    foreach ($value in @($Values)) {
        if ($null -eq $value) { continue }
        foreach ($part in ([string]$value -split "[,;`r`n]+")) {
            Add-DownloadUrlForBase -List $List -BaseUrl $part -PathMode $PathMode -Artifact $Artifact
        }
    }
}

function Get-DownloadUrls {
    param(
        [Parameter(Mandatory = $true)]$Manifest,
        [Parameter(Mandatory = $true)]$Artifact,
        [Parameter(Mandatory = $true)][string]$OriginBaseUrl,
        [string[]]$ExplicitBaseUrls = @(),
        [string[]]$ExplicitAssetBaseUrls = @()
    )
    $urls = New-Object System.Collections.ArrayList
    Add-DownloadUrlsForBases -List $urls -Values $ExplicitAssetBaseUrls -PathMode "fileName" -Artifact $Artifact
    Add-DownloadUrlsForBases -List $urls -Values $env:ECOREX_DOWNLOAD_ASSET_BASE_URLS -PathMode "fileName" -Artifact $Artifact
    Add-DownloadUrlsForBases -List $urls -Values $ExplicitBaseUrls -PathMode "href" -Artifact $Artifact
    Add-DownloadUrlsForBases -List $urls -Values $env:ECOREX_DOWNLOAD_BASE_URLS -PathMode "href" -Artifact $Artifact
    if ($Manifest.download -and $Manifest.download.mirrors) {
        foreach ($mirror in @($Manifest.download.mirrors)) {
            Add-DownloadUrlForBase -List $urls -BaseUrl ([string]$mirror.baseUrl) -PathMode ([string]$mirror.pathMode) -Artifact $Artifact
        }
    }
    if ($Manifest.download -and $Manifest.download.baseUrls) {
        Add-DownloadUrlsForBases -List $urls -Values $Manifest.download.baseUrls -PathMode "href" -Artifact $Artifact
    }
    Add-DownloadUrlForBase -List $urls -BaseUrl $OriginBaseUrl -PathMode "href" -Artifact $Artifact
    return $urls.ToArray([string])
}

function Get-ArtifactChunkBaseUrls {
    param(
        [Parameter(Mandatory = $true)]$Manifest,
        [Parameter(Mandatory = $true)]$Artifact,
        [Parameter(Mandatory = $true)][string]$OriginBaseUrl
    )
    $urls = New-Object System.Collections.ArrayList
    if (-not ($Artifact.PSObject.Properties.Name -contains "chunked") -or -not $Artifact.chunked) {
        return $urls.ToArray([string])
    }
    $baseHref = ([string]$Artifact.chunked.baseHref).Trim().Trim("/")
    if (-not $baseHref) {
        return $urls.ToArray([string])
    }
    if ($Manifest.download -and $Manifest.download.mirrors) {
        foreach ($mirror in @($Manifest.download.mirrors)) {
            $base = [string]$mirror.baseUrl
            $pathMode = [string]$mirror.pathMode
            if (-not $base) { continue }
            $path = if ($pathMode -ieq "fileName") { $baseHref } else { "downloads/$baseHref" }
            Add-DownloadUrlForBase -List $urls -BaseUrl $base -PathMode "href" -Artifact ([pscustomobject]@{ href = $path; fileName = $path })
        }
    }
    Add-DownloadUrlForBase -List $urls -BaseUrl $OriginBaseUrl -PathMode "href" -Artifact ([pscustomobject]@{ href = "downloads/$baseHref"; fileName = $baseHref })
    return $urls.ToArray([string])
}

function Format-Mib {
    param([int64]$Bytes)
    return ("{0:N1} MiB" -f ($Bytes / 1MB))
}

function Get-Sha256 {
    param([string]$Path)
    if (Get-Command Get-FileHash -ErrorAction SilentlyContinue) {
        return (Get-FileHash -Algorithm SHA256 -LiteralPath $Path).Hash.ToUpperInvariant()
    }
    $stream = [System.IO.File]::OpenRead($Path)
    try {
        $sha = [System.Security.Cryptography.SHA256]::Create()
        try {
            return (($sha.ComputeHash($stream) | ForEach-Object { $_.ToString("x2") }) -join "").ToUpperInvariant()
        } finally {
            $sha.Dispose()
        }
    } finally {
        $stream.Dispose()
    }
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

function Test-CurlRetryAllErrors {
    param([Parameter(Mandatory = $true)][string]$CurlPath)
    try {
        $help = & $CurlPath --help all 2>$null
        return (($help -join "`n") -match "--retry-all-errors")
    } catch {
        return $false
    }
}

function Try-SaveUrlWithCurl {
    param(
        [Parameter(Mandatory = $true)][string]$Uri,
        [Parameter(Mandatory = $true)][string]$CachePath,
        [Parameter(Mandatory = $true)][string]$PartialPath,
        [Parameter(Mandatory = $true)][string]$ExpectedSha256
    )

    $curl = Get-Command curl.exe -ErrorAction SilentlyContinue
    if (-not $curl) {
        return $null
    }

    Write-Host "Using curl.exe accelerated download with resume support."
    if (Test-Path -LiteralPath $PartialPath) {
        Write-Host ("Existing partial package found: {0}" -f (Format-Mib ((Get-Item -LiteralPath $PartialPath).Length)))
    }

    $curlArgs = @(
        "--fail",
        "--location",
        "--retry", "8",
        "--retry-delay", "2",
        "--retry-max-time", "3600",
        "--connect-timeout", "20",
        "--progress-bar",
        "--continue-at", "-",
        "--output", $PartialPath,
        $Uri
    )
    if (Test-CurlRetryAllErrors -CurlPath $curl.Source) {
        $curlArgs = @("--retry-all-errors") + $curlArgs
    }

    & $curl.Source @curlArgs
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "curl.exe download did not complete (exit $LASTEXITCODE); falling back to PowerShell streaming download."
        return $false
    }

    Write-Host "Verifying SHA256..."
    $actual = Get-Sha256 -Path $PartialPath
    if ($actual -ne $ExpectedSha256.ToUpperInvariant()) {
        Remove-Item -LiteralPath $PartialPath -Force -ErrorAction SilentlyContinue
        throw "SHA256 mismatch for downloaded package: $actual"
    }
    Move-Item -LiteralPath $PartialPath -Destination $CachePath -Force
    Write-Host "Download verified: $CachePath"
    return $true
}

function Get-ParallelPartCount {
    $value = $env:ECOREX_DOWNLOAD_PARALLEL_PARTS
    $parsed = 0
    if ([int]::TryParse([string]$value, [ref]$parsed) -and $parsed -gt 0) {
        return [Math]::Max(2, [Math]::Min(32, $parsed))
    }
    return 16
}

function Format-Duration {
    param([double]$Seconds)
    if ($Seconds -lt 0 -or [double]::IsInfinity($Seconds) -or [double]::IsNaN($Seconds)) {
        return "--:--"
    }
    $span = [TimeSpan]::FromSeconds([Math]::Max(0, [Math]::Round($Seconds)))
    if ($span.TotalHours -ge 1) {
        return $span.ToString("hh\:mm\:ss")
    }
    return $span.ToString("mm\:ss")
}

function Get-ParallelDownloadedBytes {
    param($Jobs)
    $total = [int64]0
    foreach ($job in @($Jobs)) {
        if (Test-Path -LiteralPath $job.Path) {
            try {
                $total += (Get-Item -LiteralPath $job.Path).Length
            } catch {
            }
        }
    }
    return $total
}

function Get-DownloadChunkSize {
    $value = $env:ECOREX_DOWNLOAD_CHUNK_MIB
    $parsed = 0
    if ([int]::TryParse([string]$value, [ref]$parsed) -and $parsed -gt 0) {
        return [int64]([Math]::Max(1, [Math]::Min(32, $parsed)) * 1MB)
    }
    return [int64](4MB)
}

function Start-CurlProcess {
    param(
        [Parameter(Mandatory = $true)][string]$CurlPath,
        [Parameter(Mandatory = $true)][string[]]$Arguments
    )
    $startInfo = New-Object System.Diagnostics.ProcessStartInfo
    $startInfo.FileName = $CurlPath
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.RedirectStandardError = $true
    $startInfo.RedirectStandardOutput = $true
    $usedArgumentList = $false
    try {
        if ($null -ne $startInfo.ArgumentList) {
            foreach ($argument in $Arguments) {
                [void]$startInfo.ArgumentList.Add($argument)
            }
            $usedArgumentList = $true
        }
    } catch {
        $usedArgumentList = $false
    }
    if (-not $usedArgumentList) {
        $quoted = foreach ($argument in $Arguments) {
            $value = [string]$argument
            if ($value -notmatch '[\s"]') {
                $value
            } else {
                '"' + ($value -replace '"', '\"') + '"'
            }
        }
        $startInfo.Arguments = ($quoted -join " ")
    }
    $process = New-Object System.Diagnostics.Process
    $process.StartInfo = $startInfo
    [void]$process.Start()
    return $process
}

function Stop-ParallelCurlJobs {
    param($Jobs)
    foreach ($job in @($Jobs)) {
        try {
            if ($job.Process -and -not $job.Process.HasExited) {
                $job.Process.Kill()
            }
        } catch {
        }
    }
}

function Try-SaveUrlWithParallelCurl {
    param(
        [Parameter(Mandatory = $true)][string]$Uri,
        [Parameter(Mandatory = $true)][string]$CachePath,
        [Parameter(Mandatory = $true)][string]$PartialPath,
        [Parameter(Mandatory = $true)][string]$ExpectedSha256,
        [int64]$ExpectedSize = 0
    )

    if ($env:ECOREX_DOWNLOAD_DISABLE_PARALLEL -in @("1", "true", "yes", "on")) {
        return $null
    }
    try {
        $hostName = ([Uri]$Uri).Host.ToLowerInvariant()
        if ($hostName -in @("127.0.0.1", "localhost", "::1")) {
            return $null
        }
    } catch {
        return $null
    }

    $curl = Get-Command curl.exe -ErrorAction SilentlyContinue
    if (-not $curl -or $ExpectedSize -lt 67108864) {
        return $null
    }
    $workerCount = Get-ParallelPartCount
    $chunkSize = Get-DownloadChunkSize
    $chunkCount = [int][Math]::Ceiling($ExpectedSize / [double]$chunkSize)
    if ($chunkCount -lt 2) {
        return $null
    }
    $partDir = "$PartialPath.parts"
    Remove-Item -LiteralPath $partDir -Recurse -Force -ErrorAction SilentlyContinue
    New-Item -ItemType Directory -Force -Path $partDir | Out-Null

    $retryAll = Test-CurlRetryAllErrors -CurlPath $curl.Source
    $chunks = New-Object System.Collections.ArrayList
    Write-Host ("Using adaptive CDN download: {0} workers, {1} chunks x {2}, total {3}." -f $workerCount, $chunkCount, (Format-Mib $chunkSize), (Format-Mib $ExpectedSize))
    for ($index = 0; $index -lt $chunkCount; $index++) {
        $start = [int64]($index * $chunkSize)
        $end = [int64]([Math]::Min($ExpectedSize - 1, (($index + 1) * $chunkSize) - 1))
        if ($start -gt $end) { continue }
        $partPath = Join-Path $partDir ("part-{0:D3}" -f $index)
        [void]$chunks.Add([pscustomobject]@{
            Index = $index
            Start = $start
            End = $end
            Path = $partPath
            Attempts = 0
            Process = $null
            Status = "queued"
            LastSize = [int64]0
            LastProgressAt = [DateTime]::UtcNow
        })
    }

    function Start-NextChunk {
        param($Chunk)
        Remove-Item -LiteralPath $Chunk.Path -Force -ErrorAction SilentlyContinue
        $Chunk.Attempts += 1
        $Chunk.Status = "running"
        $Chunk.LastSize = [int64]0
        $Chunk.LastProgressAt = [DateTime]::UtcNow
        $args = @(
            "--fail",
            "--location",
            "--retry", "2",
            "--retry-delay", "1",
            "--retry-max-time", "180",
            "--connect-timeout", "15",
            "--silent",
            "--show-error",
            "--range", "$($Chunk.Start)-$($Chunk.End)",
            "--output", $Chunk.Path,
            $Uri
        )
        if ($retryAll) {
            $args = @("--retry-all-errors") + $args
        }
        $Chunk.Process = Start-CurlProcess -CurlPath $curl.Source -Arguments $args
    }

    $startedAt = [DateTime]::UtcNow
    $lastStamp = $startedAt
    $lastBytes = [int64]0
    $lastPrintedAt = $startedAt.AddSeconds(-10)
    while (@($chunks | Where-Object { $_.Status -ne "done" }).Count -gt 0) {
        while (@($chunks | Where-Object { $_.Status -eq "running" }).Count -lt $workerCount) {
            $next = $chunks | Where-Object { $_.Status -eq "queued" } | Sort-Object Index | Select-Object -First 1
            if ($null -eq $next) {
                break
            }
            Start-NextChunk -Chunk $next
        }
        Start-Sleep -Seconds 1
        $now = [DateTime]::UtcNow
        foreach ($chunk in @($chunks | Where-Object { $_.Status -eq "running" })) {
            $size = [int64]0
            if (Test-Path -LiteralPath $chunk.Path) {
                try {
                    $size = (Get-Item -LiteralPath $chunk.Path).Length
                } catch {
                    $size = [int64]0
                }
            }
            if ($size -gt $chunk.LastSize) {
                $chunk.LastSize = $size
                $chunk.LastProgressAt = $now
            }
            $expectedPartSize = [int64]($chunk.End - $chunk.Start + 1)
            if ($chunk.Process.HasExited) {
                if ($chunk.Process.ExitCode -eq 0 -and (Test-Path -LiteralPath $chunk.Path) -and (Get-Item -LiteralPath $chunk.Path).Length -eq $expectedPartSize) {
                    $chunk.Status = "done"
                } elseif ($chunk.Attempts -lt 4) {
                    try {
                        $err = $chunk.Process.StandardError.ReadToEnd()
                        if ($err) {
                            Write-Warning ("Chunk {0} retry {1}/4: {2}" -f $chunk.Index, ($chunk.Attempts + 1), $err.Trim())
                        }
                    } catch {
                    }
                    $chunk.Status = "queued"
                    $chunk.Process = $null
                    Remove-Item -LiteralPath $chunk.Path -Force -ErrorAction SilentlyContinue
                } else {
                    try {
                        $err = $chunk.Process.StandardError.ReadToEnd()
                        if ($err) {
                            Write-Warning ("Chunk {0} failed: {1}" -f $chunk.Index, $err.Trim())
                        }
                    } catch {
                    }
                    Write-Progress -Activity "Downloading EcoreX WebUI from CDN" -Completed
                    Write-Warning "Adaptive CDN download did not complete; falling back to single-connection resume."
                    return $false
                }
            } elseif ($size -lt $expectedPartSize -and ($now - $chunk.LastProgressAt).TotalSeconds -ge 35) {
                try {
                    $chunk.Process.Kill()
                } catch {
                }
                if ($chunk.Attempts -lt 4) {
                    Write-Warning ("Chunk {0} stalled; retry {1}/4." -f $chunk.Index, ($chunk.Attempts + 1))
                    $chunk.Status = "queued"
                    $chunk.Process = $null
                    Remove-Item -LiteralPath $chunk.Path -Force -ErrorAction SilentlyContinue
                } else {
                    Write-Progress -Activity "Downloading EcoreX WebUI from CDN" -Completed
                    Write-Warning "Adaptive CDN download stalled; falling back to single-connection resume."
                    return $false
                }
            }
        }
        $downloaded = [int64]0
        foreach ($chunk in $chunks) {
            $expectedPartSize = [int64]($chunk.End - $chunk.Start + 1)
            if ($chunk.Status -eq "done") {
                $downloaded += $expectedPartSize
            } elseif (Test-Path -LiteralPath $chunk.Path) {
                try {
                    $downloaded += [Math]::Min($expectedPartSize, (Get-Item -LiteralPath $chunk.Path).Length)
                } catch {
                }
            }
        }
        $elapsed = [Math]::Max(0.1, ($now - $startedAt).TotalSeconds)
        $deltaSeconds = [Math]::Max(0.1, ($now - $lastStamp).TotalSeconds)
        $instantSpeed = [int64](($downloaded - $lastBytes) / $deltaSeconds)
        $averageSpeed = [int64]($downloaded / $elapsed)
        $speed = if ($instantSpeed -gt 0) { $instantSpeed } else { $averageSpeed }
        $percent = [Math]::Min(100, [Math]::Round(($downloaded * 100.0) / $ExpectedSize, 1))
        $eta = if ($speed -gt 0) { Format-Duration (($ExpectedSize - $downloaded) / [double]$speed) } else { "--:--" }
        $status = "{0}%  {1} / {2}  {3}/s  ETA {4}" -f $percent, (Format-Mib $downloaded), (Format-Mib $ExpectedSize), (Format-Mib $speed), $eta
        Write-Progress -Activity "Downloading EcoreX WebUI from CDN" -Status $status -PercentComplete $percent
        if ($downloaded -ne $lastBytes -or ($now - $lastPrintedAt).TotalSeconds -ge 5) {
            Write-Host ("CDN download progress: {0}" -f $status)
            $lastPrintedAt = $now
        }
        $lastStamp = $now
        $lastBytes = $downloaded
    }
    Write-Progress -Activity "Downloading EcoreX WebUI from CDN" -Completed
    foreach ($chunk in $chunks) {
        $expectedPartSize = [int64]($chunk.End - $chunk.Start + 1)
        if (-not (Test-Path -LiteralPath $chunk.Path) -or (Get-Item -LiteralPath $chunk.Path).Length -ne $expectedPartSize) {
            Write-Warning "Adaptive CDN range was not honored; falling back to single-connection resume."
            return $false
        }
    }

    Remove-Item -LiteralPath $PartialPath -Force -ErrorAction SilentlyContinue
    $target = [System.IO.File]::Open($PartialPath, [System.IO.FileMode]::Create, [System.IO.FileAccess]::Write, [System.IO.FileShare]::None)
    try {
        foreach ($chunk in ($chunks | Sort-Object Index)) {
            $source = [System.IO.File]::OpenRead($chunk.Path)
            try {
                $source.CopyTo($target)
            } finally {
                $source.Dispose()
            }
        }
    } finally {
        $target.Dispose()
    }

    Write-Host "Verifying SHA256..."
    $actual = Get-Sha256 -Path $PartialPath
    if ($actual -ne $ExpectedSha256.ToUpperInvariant()) {
        Remove-Item -LiteralPath $PartialPath -Force -ErrorAction SilentlyContinue
        Write-Warning "Parallel CDN download SHA256 mismatch; falling back to single-connection resume."
        return $false
    }
    Move-Item -LiteralPath $PartialPath -Destination $CachePath -Force
    Remove-Item -LiteralPath $partDir -Recurse -Force -ErrorAction SilentlyContinue
    Write-Host "Download verified: $CachePath"
    return $true
}

function Save-ArtifactChunks {
    param(
        [Parameter(Mandatory = $true)]$Manifest,
        [Parameter(Mandatory = $true)]$Artifact,
        [Parameter(Mandatory = $true)][string]$OriginBaseUrl,
        [Parameter(Mandatory = $true)][string]$CachePath,
        [Parameter(Mandatory = $true)][string]$PartialPath,
        [Parameter(Mandatory = $true)][string]$ExpectedSha256
    )
    if (-not ($Artifact.PSObject.Properties.Name -contains "chunked") -or -not $Artifact.chunked) {
        return $null
    }
    if ((Test-Path -LiteralPath $CachePath) -and (Get-Sha256 -Path $CachePath) -eq $ExpectedSha256.ToUpperInvariant()) {
        Write-Host "Using cached verified package: $CachePath"
        return $CachePath
    }
    $chunked = $Artifact.chunked
    $chunkList = @($chunked.chunks)
    if ($chunkList.Count -le 0) {
        return $null
    }
    $chunkBases = Get-ArtifactChunkBaseUrls -Manifest $Manifest -Artifact $Artifact -OriginBaseUrl $OriginBaseUrl
    $chunkBase = $chunkBases | Where-Object { -not [string]::IsNullOrWhiteSpace($_) } | Select-Object -First 1
    if (-not $chunkBase) {
        return $null
    }
    $curl = Get-Command curl.exe -ErrorAction SilentlyContinue
    if (-not $curl) {
        return $null
    }

    $chunkDir = "$CachePath.chunk-files"
    New-Item -ItemType Directory -Force -Path $chunkDir | Out-Null
    $workerCount = Get-ParallelPartCount
    $expectedSize = [int64]$Artifact.size
    $retryAll = Test-CurlRetryAllErrors -CurlPath $curl.Source
    $items = New-Object System.Collections.ArrayList
    Write-Host ("Using CDN chunked package download: {0} files, {1} workers, total {2}." -f $chunkList.Count, $workerCount, (Format-Mib $expectedSize))

    foreach ($entry in $chunkList) {
        $fileName = [string]$entry.fileName
        $path = Join-Path $chunkDir $fileName
        $item = [pscustomobject]@{
            Index = [int]$entry.index
            Url = (Join-Url $chunkBase $fileName)
            Path = $path
            TempPath = "$path.part"
            Size = [int64]$entry.size
            Sha256 = ([string]$entry.sha256).ToUpperInvariant()
            Attempts = 0
            Process = $null
            Status = "queued"
            LastSize = [int64]0
            LastProgressAt = [DateTime]::UtcNow
        }
        if ((Test-Path -LiteralPath $path) -and (Get-Item -LiteralPath $path).Length -eq $item.Size) {
            try {
                if ((Get-Sha256 -Path $path) -eq $item.Sha256) {
                    $item.Status = "done"
                }
            } catch {
            }
        }
        [void]$items.Add($item)
    }

    function Start-ChunkDownload {
        param($Chunk)
        Remove-Item -LiteralPath $Chunk.TempPath -Force -ErrorAction SilentlyContinue
        $Chunk.Attempts += 1
        $Chunk.Status = "running"
        $Chunk.LastSize = [int64]0
        $Chunk.LastProgressAt = [DateTime]::UtcNow
        $args = @(
            "--fail",
            "--location",
            "--retry", "8",
            "--retry-delay", "2",
            "--retry-max-time", "600",
            "--connect-timeout", "20",
            "--silent",
            "--show-error",
            "--output", $Chunk.TempPath,
            $Chunk.Url
        )
        if ($retryAll) {
            $args = @("--retry-all-errors") + $args
        }
        $Chunk.Process = Start-CurlProcess -CurlPath $curl.Source -Arguments $args
    }

    $startedAt = [DateTime]::UtcNow
    $lastStamp = $startedAt
    $lastBytes = [int64]0
    $lastPrintedAt = $startedAt.AddSeconds(-10)
    while (@($items | Where-Object { $_.Status -ne "done" }).Count -gt 0) {
        while (@($items | Where-Object { $_.Status -eq "running" }).Count -lt $workerCount) {
            $next = $items | Where-Object { $_.Status -eq "queued" } | Sort-Object Index | Select-Object -First 1
            if ($null -eq $next) { break }
            Start-ChunkDownload -Chunk $next
        }
        Start-Sleep -Seconds 1
        $now = [DateTime]::UtcNow
        foreach ($chunk in @($items | Where-Object { $_.Status -eq "running" })) {
            $size = [int64]0
            if (Test-Path -LiteralPath $chunk.TempPath) {
                try { $size = (Get-Item -LiteralPath $chunk.TempPath).Length } catch { $size = [int64]0 }
            }
            if ($size -gt $chunk.LastSize) {
                $chunk.LastSize = $size
                $chunk.LastProgressAt = $now
            }
            if ($chunk.Process.HasExited) {
                $ok = $false
                if ($chunk.Process.ExitCode -eq 0 -and (Test-Path -LiteralPath $chunk.TempPath) -and (Get-Item -LiteralPath $chunk.TempPath).Length -eq $chunk.Size) {
                    try { $ok = ((Get-Sha256 -Path $chunk.TempPath) -eq $chunk.Sha256) } catch { $ok = $false }
                }
                if ($ok) {
                    Move-Item -LiteralPath $chunk.TempPath -Destination $chunk.Path -Force
                    $chunk.Status = "done"
                } elseif ($chunk.Attempts -lt 6) {
                    Write-Warning ("Chunk file {0} retry {1}/6." -f $chunk.Index, ($chunk.Attempts + 1))
                    $chunk.Status = "queued"
                    $chunk.Process = $null
                    Remove-Item -LiteralPath $chunk.TempPath -Force -ErrorAction SilentlyContinue
                } else {
                    throw "CDN chunked download failed at chunk $($chunk.Index)."
                }
            } elseif ($size -lt $chunk.Size -and ($now - $chunk.LastProgressAt).TotalSeconds -ge 45) {
                try { $chunk.Process.Kill() } catch {}
                if ($chunk.Attempts -lt 6) {
                    Write-Warning ("Chunk file {0} stalled; retry {1}/6." -f $chunk.Index, ($chunk.Attempts + 1))
                    $chunk.Status = "queued"
                    $chunk.Process = $null
                    Remove-Item -LiteralPath $chunk.TempPath -Force -ErrorAction SilentlyContinue
                } else {
                    throw "CDN chunked download stalled at chunk $($chunk.Index)."
                }
            }
        }

        $downloaded = [int64]0
        foreach ($chunk in $items) {
            if ($chunk.Status -eq "done") {
                $downloaded += $chunk.Size
            } elseif (Test-Path -LiteralPath $chunk.TempPath) {
                try { $downloaded += [Math]::Min($chunk.Size, (Get-Item -LiteralPath $chunk.TempPath).Length) } catch {}
            }
        }
        $deltaSeconds = [Math]::Max(0.1, ($now - $lastStamp).TotalSeconds)
        $elapsed = [Math]::Max(0.1, ($now - $startedAt).TotalSeconds)
        $instantSpeed = [int64](($downloaded - $lastBytes) / $deltaSeconds)
        $averageSpeed = [int64]($downloaded / $elapsed)
        $speed = if ($instantSpeed -gt 0) { $instantSpeed } else { $averageSpeed }
        $percent = [Math]::Min(100, [Math]::Round(($downloaded * 100.0) / $expectedSize, 1))
        $eta = if ($speed -gt 0) { Format-Duration (($expectedSize - $downloaded) / [double]$speed) } else { "--:--" }
        $status = "{0}%  {1} / {2}  {3}/s  ETA {4}" -f $percent, (Format-Mib $downloaded), (Format-Mib $expectedSize), (Format-Mib $speed), $eta
        Write-Progress -Activity "Downloading EcoreX WebUI chunk files from CDN" -Status $status -PercentComplete $percent
        if ($downloaded -ne $lastBytes -or ($now - $lastPrintedAt).TotalSeconds -ge 5) {
            Write-Host ("CDN chunk download progress: {0}" -f $status)
            $lastPrintedAt = $now
        }
        $lastStamp = $now
        $lastBytes = $downloaded
    }
    Write-Progress -Activity "Downloading EcoreX WebUI chunk files from CDN" -Completed

    Remove-Item -LiteralPath $PartialPath -Force -ErrorAction SilentlyContinue
    $target = [System.IO.File]::Open($PartialPath, [System.IO.FileMode]::Create, [System.IO.FileAccess]::Write, [System.IO.FileShare]::None)
    try {
        foreach ($chunk in ($items | Sort-Object Index)) {
            $source = [System.IO.File]::OpenRead($chunk.Path)
            try { $source.CopyTo($target) } finally { $source.Dispose() }
        }
    } finally {
        $target.Dispose()
    }

    Write-Host "Verifying SHA256..."
    $actual = Get-Sha256 -Path $PartialPath
    if ($actual -ne $ExpectedSha256.ToUpperInvariant()) {
        Remove-Item -LiteralPath $PartialPath -Force -ErrorAction SilentlyContinue
        throw "SHA256 mismatch for chunked package: $actual"
    }
    Move-Item -LiteralPath $PartialPath -Destination $CachePath -Force
    Remove-Item -LiteralPath $chunkDir -Recurse -Force -ErrorAction SilentlyContinue
    Write-Host "Chunked download verified: $CachePath"
    return $CachePath
}

function ConvertTo-EcoreXLongPath {
    param([Parameter(Mandatory = $true)][string]$Path)
    $full = [System.IO.Path]::GetFullPath($Path)
    if ($full.StartsWith("\\?\")) {
        return $full
    }
    if ($full.StartsWith("\\")) {
        return "\\?\UNC\" + $full.TrimStart("\")
    }
    return "\\?\" + $full
}

function Expand-EcoreXZip {
    param(
        [Parameter(Mandatory = $true)][string]$ZipPath,
        [Parameter(Mandatory = $true)][string]$DestinationPath
    )

    if (Test-Path -LiteralPath $DestinationPath) {
        Remove-Item -LiteralPath $DestinationPath -Recurse -Force -ErrorAction SilentlyContinue
    }
    New-Item -ItemType Directory -Force -Path $DestinationPath | Out-Null

    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $destinationFull = [System.IO.Path]::GetFullPath($DestinationPath)
    if (-not $destinationFull.EndsWith([System.IO.Path]::DirectorySeparatorChar.ToString())) {
        $destinationFull += [System.IO.Path]::DirectorySeparatorChar
    }

    $archive = [System.IO.Compression.ZipFile]::OpenRead($ZipPath)
    try {
        foreach ($entry in $archive.Entries) {
            $entryName = $entry.FullName.Replace("\", "/")
            if ([string]::IsNullOrWhiteSpace($entryName)) {
                continue
            }

            $targetPath = [System.IO.Path]::GetFullPath((Join-Path $DestinationPath $entryName))
            if (-not $targetPath.StartsWith($destinationFull, [System.StringComparison]::OrdinalIgnoreCase)) {
                throw "Blocked unsafe zip entry: $entryName"
            }
            $targetIoPath = ConvertTo-EcoreXLongPath -Path $targetPath

            if ($entryName.EndsWith("/")) {
                if ([System.IO.File]::Exists($targetIoPath)) {
                    [System.IO.File]::Delete($targetIoPath)
                }
                [System.IO.Directory]::CreateDirectory($targetIoPath) | Out-Null
                continue
            }

            $parent = Split-Path -Parent $targetPath
            if ($parent) {
                [System.IO.Directory]::CreateDirectory((ConvertTo-EcoreXLongPath -Path $parent)) | Out-Null
            }
            if ([System.IO.Directory]::Exists($targetIoPath)) {
                [System.IO.Directory]::Delete($targetIoPath, $true)
            } elseif ([System.IO.File]::Exists($targetIoPath)) {
                [System.IO.File]::Delete($targetIoPath)
            }
            $source = $entry.Open()
            $target = [System.IO.File]::Open($targetIoPath, [System.IO.FileMode]::CreateNew, [System.IO.FileAccess]::Write, [System.IO.FileShare]::None)
            try {
                $source.CopyTo($target)
            } finally {
                $target.Dispose()
                $source.Dispose()
            }
        }
    } finally {
        $archive.Dispose()
    }
}

function Find-WebUIInstaller {
    param([Parameter(Mandatory = $true)][string]$ExtractRoot)

    $relativeInstaller = Join-Path "scripts" "install-ecorex-webui-win.ps1"
    $directInstaller = Join-Path $ExtractRoot $relativeInstaller
    if (Test-Path -LiteralPath $directInstaller) {
        return Get-Item -LiteralPath $directInstaller
    }

    foreach ($child in @(Get-ChildItem -LiteralPath $ExtractRoot -Directory -ErrorAction Stop)) {
        $candidate = Join-Path $child.FullName $relativeInstaller
        if (Test-Path -LiteralPath $candidate) {
            return Get-Item -LiteralPath $candidate
        }
    }

    throw "Windows WebUI installer was not found in the downloaded package."
}

function Save-UrlWithProgress {
    param(
        [Parameter(Mandatory = $true)][string]$Uri,
        [Parameter(Mandatory = $true)][string]$CachePath,
        [Parameter(Mandatory = $true)][string]$WorkDir,
        [Parameter(Mandatory = $true)][string]$ExpectedSha256,
        [int64]$ExpectedSize = 0,
        [int]$Retries = 3
    )

    if (Test-ExpectedHash -Path $CachePath -ExpectedSha256 $ExpectedSha256) {
        Write-Host "Using cached package: $CachePath"
        return $CachePath
    }

    $cacheDir = Split-Path -Parent $CachePath
    New-Item -ItemType Directory -Force -Path $cacheDir, $WorkDir | Out-Null
    $partialPath = "$CachePath.part"
    $parallelResult = Try-SaveUrlWithParallelCurl -Uri $Uri -CachePath $CachePath -PartialPath $partialPath -ExpectedSha256 $ExpectedSha256 -ExpectedSize $ExpectedSize
    if ($parallelResult -eq $true) {
        return $CachePath
    }
    $curlResult = Try-SaveUrlWithCurl -Uri $Uri -CachePath $CachePath -PartialPath $partialPath -ExpectedSha256 $ExpectedSha256
    if ($curlResult -eq $true) {
        return $CachePath
    }

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
            $request.UserAgent = "EcoreX-WebUI-Installer/0.3.0"
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

function Test-DownloadSourceAvailable {
    param([Parameter(Mandatory = $true)][string]$Uri)
    try {
        $request = [System.Net.HttpWebRequest][System.Net.WebRequest]::Create($Uri)
        $request.Method = "HEAD"
        $request.UserAgent = "EcoreX-WebUI-Installer/0.3.0"
        $request.Timeout = 10000
        $request.ReadWriteTimeout = 10000
        $request.AllowAutoRedirect = $true
        $response = $request.GetResponse()
        try {
            return ([int]$response.StatusCode -lt 400)
        } finally {
            $response.Dispose()
        }
    } catch [System.Net.WebException] {
        $webResponse = $_.Exception.Response
        if ($webResponse) {
            $statusCode = [int]$webResponse.StatusCode
            $webResponse.Dispose()
            if ($statusCode -eq 404) { return $false }
            if ($statusCode -eq 405) { return $true }
        }
        return $true
    } catch {
        return $true
    }
}

function Save-UrlWithFallback {
    param(
        [Parameter(Mandatory = $true)][string[]]$Urls,
        [Parameter(Mandatory = $true)][string]$CachePath,
        [Parameter(Mandatory = $true)][string]$WorkDir,
        [Parameter(Mandatory = $true)][string]$ExpectedSha256,
        [int64]$ExpectedSize = 0
    )
    $cleanUrls = @($Urls | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
    if ($cleanUrls.Count -eq 0) {
        throw "No download source was configured."
    }
    $failures = New-Object System.Collections.ArrayList
    for ($i = 0; $i -lt $cleanUrls.Count; $i++) {
        $url = [string]$cleanUrls[$i]
        $sourceLabel = if ($i -eq 0) { "primary" } else { "fallback $($i + 1)" }
        if (-not (Test-DownloadSourceAvailable -Uri $url)) {
            Write-Warning "Skipping unavailable $sourceLabel download source: $url"
            [void]$failures.Add("${url}: unavailable")
            continue
        }
        Write-Host "Using $sourceLabel download source: $url"
        try {
            return Save-UrlWithProgress -Uri $url -CachePath $CachePath -WorkDir $WorkDir -ExpectedSha256 $ExpectedSha256 -ExpectedSize $ExpectedSize
        } catch {
            Write-Warning "$sourceLabel download failed: $url"
            Write-Warning $_.Exception.Message
            Write-Warning "Trying the next configured source when available."
            [void]$failures.Add("${url}: $($_.Exception.Message)")
        }
    }
    throw "All EcoreX WebUI download sources failed: $($failures -join ' | ')"
}

$manifestUrl = Join-Url $BaseUrl "manifest.json"
Write-Host "Fetching EcoreX manifest: $manifestUrl"
$manifest = Invoke-RestMethod -Uri $manifestUrl -UseBasicParsing -TimeoutSec 30
Write-Host "EcoreX WebUI installer script: 0.3.0"
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
    $packagePath = Save-ArtifactChunks -Manifest $manifest -Artifact $artifact -OriginBaseUrl $BaseUrl -CachePath $zipPath -PartialPath "$zipPath.part" -ExpectedSha256 ([string]$artifact.sha256)
    if (-not $packagePath) {
        $downloadUrls = Get-DownloadUrls -Manifest $manifest -Artifact $artifact -OriginBaseUrl $BaseUrl -ExplicitBaseUrls $DownloadBaseUrls -ExplicitAssetBaseUrls $AssetDownloadBaseUrls
        $packagePath = Save-UrlWithFallback -Urls $downloadUrls -CachePath $zipPath -WorkDir $tempRoot -ExpectedSha256 ([string]$artifact.sha256) -ExpectedSize ([int64]$artifact.size)
    }

    Write-Host "Extracting package..."
    Expand-EcoreXZip -ZipPath $packagePath -DestinationPath $extractRoot
    Write-Host "Package extracted."

    $installer = Find-WebUIInstaller -ExtractRoot $extractRoot

    Write-Host "Starting EcoreX WebUI local installer..."
    $args = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $installer.FullName)
    $args += @("-Port", $Port)
    if ($NoBrowser) { $args += "-NoBrowser" }
    & powershell @args
    if ($LASTEXITCODE -ne 0) {
        throw "EcoreX WebUI installer failed with exit code $LASTEXITCODE."
    }
    Write-Host "EcoreX WebUI install command finished. If the browser did not open, double-click the desktop EcoreX WebUI shortcut; it will start the local service and reopen the browser."
} finally {
    Remove-Item -LiteralPath $tempRoot -Recurse -Force -ErrorAction SilentlyContinue
}
