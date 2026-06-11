param(
    [string]$Version = "0.1.10",
    [string]$PublicBaseUrl = "https://www.ecoreai.cn/ecorex-agent",
    [string]$ReleaseZip = "release-artifacts/EcoreX_0.1.10-public-release.zip",
    [string]$WindowsInstaller = "desktop/release/EcoreX_0.1.10_x64-setup.exe",
    [string]$SiteManifest = "deploy/ecorex-site/manifest.json",
    [string]$ExpectedGitHubCommit = "",
    [string]$GitRemoteUrl = "https://github.com/zhangyifanjackson-dotcom/EcoreX.git",
    [string]$GitProductBranch = "codex/ecorex-v0.1.10-productization",
    [switch]$AllowPublicBlocked,
    [switch]$SkipLinuxInstallSmoke
)

$ErrorActionPreference = "Stop"

function New-Result {
    param(
        [string]$Name,
        [string]$Status,
        [string]$Evidence,
        [string]$Severity = "info"
    )
    [ordered]@{
        name = $Name
        status = $Status
        severity = $Severity
        evidence = $Evidence
    }
}

function Get-FileSha256 {
    param([string]$Path)
    return (Get-FileHash -Algorithm SHA256 -LiteralPath $Path).Hash.ToUpperInvariant()
}

function Invoke-HeadStatus {
    param([string]$Url)
    try {
        $response = Invoke-WebRequest -UseBasicParsing -Uri $Url -Method Head -MaximumRedirection 5 -TimeoutSec 30 -ErrorAction Stop
        return [ordered]@{ statusCode = [int]$response.StatusCode; ok = $true; error = "" }
    } catch {
        $statusCode = $null
        if ($_.Exception.Response) {
            $statusCode = [int]$_.Exception.Response.StatusCode
        }
        return [ordered]@{ statusCode = $statusCode; ok = $false; error = $_.Exception.Message }
    }
}

function Get-GitHubToken {
    foreach ($name in @("ECOREX_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN")) {
        $value = [Environment]::GetEnvironmentVariable($name)
        if ($value) {
            return $value
        }
    }
    return ""
}

function Get-RemoteHead {
    param([string]$Branch)

    $ref = "refs/heads/$Branch"
    $remoteOutput = git ls-remote $GitRemoteUrl $ref 2>$null
    if ($LASTEXITCODE -eq 0 -and $remoteOutput) {
        return [ordered]@{
            sha = (($remoteOutput -join "`n") -split "\s+")[0]
            source = "git ls-remote"
        }
    }

    if ($GitRemoteUrl -notmatch "github\.com[:/](?<owner>[^/]+)/(?<repo>[^/.]+)(\.git)?$") {
        throw "git ls-remote failed and GitHub API fallback cannot parse $GitRemoteUrl"
    }

    $branchPath = $Branch -replace "/", "%2F"
    $headers = @{
        "Accept" = "application/vnd.github+json"
        "User-Agent" = "ecorex-acceptance"
    }
    $token = Get-GitHubToken
    if ($token) {
        $headers.Authorization = "Bearer $token"
    }
    $response = Invoke-WebRequest -UseBasicParsing -Uri "https://api.github.com/repos/$($Matches.owner)/$($Matches.repo)/git/refs/heads/$branchPath" -Headers $headers -TimeoutSec 60
    $payload = $response.Content | ConvertFrom-Json
    return [ordered]@{
        sha = [string]$payload.object.sha
        source = "GitHub API"
    }
}

function ConvertTo-BashPath {
    param([string]$Path)
    $fullPath = [System.IO.Path]::GetFullPath($Path)
    if ($fullPath -match "^(?<drive>[A-Za-z]):\\(?<rest>.*)$") {
        $drive = $Matches.drive.ToLowerInvariant()
        $rest = $Matches.rest.Replace("\", "/")
        return "/mnt/$drive/$rest"
    }
    return $fullPath.Replace("\", "/")
}

$results = New-Object System.Collections.Generic.List[object]

if (-not (Test-Path -LiteralPath $SiteManifest)) {
    $results.Add((New-Result "Site manifest exists" "fail" "$SiteManifest is missing." "blocker"))
} else {
    $manifest = Get-Content -Raw -Encoding UTF8 -LiteralPath $SiteManifest | ConvertFrom-Json
    if ($manifest.product -eq "EcoreX" -and $manifest.version -eq $Version) {
        $results.Add((New-Result "Site manifest version" "pass" "Manifest product/version is EcoreX $Version."))
    } else {
        $results.Add((New-Result "Site manifest version" "fail" "Manifest product=$($manifest.product), version=$($manifest.version)." "blocker"))
    }
}

$windowsArtifact = $manifest.artifacts | Where-Object { $_.id -eq "windows-x64" } | Select-Object -First 1
if (-not $windowsArtifact) {
    $results.Add((New-Result "Windows artifact manifest" "fail" "windows-x64 is missing from manifest." "blocker"))
} elseif (-not (Test-Path -LiteralPath $WindowsInstaller)) {
    $results.Add((New-Result "Windows installer exists" "fail" "$WindowsInstaller is missing." "blocker"))
} else {
    $installerItem = Get-Item -LiteralPath $WindowsInstaller
    $installerHash = Get-FileSha256 $WindowsInstaller
    if ([int64]$windowsArtifact.size -eq [int64]$installerItem.Length -and $installerHash -eq ([string]$windowsArtifact.sha256).ToUpperInvariant()) {
        $results.Add((New-Result "Windows installer manifest match" "pass" "Size $($installerItem.Length), SHA256 $installerHash."))
    } else {
        $results.Add((New-Result "Windows installer manifest match" "fail" "Manifest size/hash does not match local installer." "blocker"))
    }
    $signature = Get-AuthenticodeSignature -LiteralPath $WindowsInstaller
    if ($signature.Status -eq "Valid") {
        $results.Add((New-Result "Windows installer signature" "pass" "Authenticode signature is valid."))
    } else {
        $results.Add((New-Result "Windows installer signature" "fail" "$($signature.Status): $($signature.StatusMessage)" "blocker"))
    }
}

if (-not (Test-Path -LiteralPath $ReleaseZip)) {
    $results.Add((New-Result "Public release zip exists" "fail" "$ReleaseZip is missing." "blocker"))
} else {
    Add-Type -AssemblyName System.IO.Compression
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $zipItem = Get-Item -LiteralPath $ReleaseZip
    $zipHash = Get-FileSha256 $ReleaseZip
    $zip = [System.IO.Compression.ZipFile]::OpenRead((Resolve-Path -LiteralPath $ReleaseZip).Path)
    try {
        $entries = @($zip.Entries | ForEach-Object { $_.FullName })
        $requiredEntries = @(
            "checksums.json",
            "site/index.html",
            "site/manifest.json",
            "site/admin/index.html",
            "site/downloads/EcoreX_0.1.10_x64-setup.exe",
            "admin-api/ecorex_admin_api.py",
            "server/install-ecorex-public-release.sh",
            "server/nginx/ecorex-agent.conf.example",
            "server/systemd/ecorex-admin-api.service.example"
        )
        $missingEntries = @($requiredEntries | Where-Object { $entries -notcontains $_ })
        $backslashEntries = @($entries | Where-Object { $_ -match "\\" })
        if ($missingEntries.Count -eq 0 -and $backslashEntries.Count -eq 0) {
            $results.Add((New-Result "Public release zip contents" "pass" "Size $($zipItem.Length), SHA256 $zipHash, required entries present, no backslash paths."))
        } else {
            $detail = "Missing: $($missingEntries -join ', '); backslash entries: $($backslashEntries.Count)"
            $results.Add((New-Result "Public release zip contents" "fail" $detail "blocker"))
        }
    } finally {
        $zip.Dispose()
    }
}

if ($SkipLinuxInstallSmoke) {
    $results.Add((New-Result "Linux install script smoke" "skipped" "Skipped by -SkipLinuxInstallSmoke." "warn"))
} else {
    $bash = Get-Command bash -ErrorAction SilentlyContinue
    if (-not $bash) {
        $results.Add((New-Result "Linux install script smoke" "skipped" "bash is not available on this machine." "warn"))
    } else {
        $smokeRoot = Join-Path (Resolve-Path -LiteralPath ".").Path "tmp/ecorex-acceptance-install"
        $smokeRootUnix = ConvertTo-BashPath $smokeRoot
        $releaseRoot = "$smokeRootUnix/download"
        $adminRoot = "$smokeRootUnix/admin"
        $zipUnix = ConvertTo-BashPath (Resolve-Path -LiteralPath $ReleaseZip).Path
        $expectedZip = Get-FileSha256 $ReleaseZip
        $script = @"
set -euo pipefail
rm -rf "$smokeRootUnix"
mkdir -p "$smokeRootUnix"
RELEASE_ROOT="$releaseRoot" ADMIN_ROOT="$adminRoot" RESTART_SERVICE=0 EXPECTED_SHA256=$expectedZip bash scripts/install-ecorex-public-release.sh "$zipUnix" >/tmp/ecorex-acceptance-install.log
test -f "$releaseRoot/current/manifest.json" || test -f "$releaseRoot/releases/"*/manifest.json
test -f "$adminRoot/app/ecorex_admin_api.py"
test -f "$adminRoot/env/ecorex-admin-api.env"
"@
        $script = $script -replace "\\", "/"
        $previousErrorActionPreference = $ErrorActionPreference
        try {
            $ErrorActionPreference = "Continue"
            $output = & bash -lc $script 2>&1
            $bashExitCode = $LASTEXITCODE
        } finally {
            $ErrorActionPreference = $previousErrorActionPreference
        }
        if ($bashExitCode -eq 0) {
            $results.Add((New-Result "Linux install script smoke" "pass" "Release zip installed into tmp/ecorex-acceptance-install."))
        } else {
            $results.Add((New-Result "Linux install script smoke" "fail" (($output -join "`n") | Select-Object -First 1) "blocker"))
        }
    }
}

$base = $PublicBaseUrl.TrimEnd("/")
$manifestHead = Invoke-HeadStatus "$base/manifest.json"
$rootHead = Invoke-HeadStatus "$base/"
$adminHead = Invoke-HeadStatus "$base/admin/"
if ($manifestHead.statusCode -eq 200 -and $rootHead.statusCode -eq 200) {
    $results.Add((New-Result "Public static route" "pass" "$base serves manifest and root."))
} else {
    $severity = if ($AllowPublicBlocked) { "warn" } else { "blocker" }
    $status = if ($AllowPublicBlocked) { "blocked" } else { "fail" }
    $results.Add((New-Result "Public static route" $status "manifest=$($manifestHead.statusCode), root=$($rootHead.statusCode). $($manifestHead.error)" $severity))
}
if ($adminHead.statusCode -eq 401) {
    $results.Add((New-Result "Public admin auth route" "pass" "$base/admin/ returns HTTP 401 without credentials."))
} else {
    $results.Add((New-Result "Public admin auth route" "blocked" "$base/admin/ returned HTTP $($adminHead.statusCode)." "warn"))
}

try {
    $localHead = (git rev-parse HEAD).Trim()
    $dirty = git status --porcelain
    $remoteMain = Get-RemoteHead "main"
    $remoteProduct = Get-RemoteHead $GitProductBranch
    if ($dirty) {
        $results.Add((New-Result "Git worktree" "fail" "Worktree has uncommitted changes." "blocker"))
    } else {
        $results.Add((New-Result "Git worktree" "pass" "Worktree is clean at $localHead."))
    }
    if ($remoteMain.sha -eq $remoteProduct.sha -and (!$ExpectedGitHubCommit -or $remoteMain.sha -eq $ExpectedGitHubCommit)) {
        $results.Add((New-Result "GitHub refs" "pass" "main and $GitProductBranch both point to $($remoteMain.sha) via $($remoteMain.source)."))
    } else {
        $results.Add((New-Result "GitHub refs" "fail" "main=$($remoteMain.sha), $GitProductBranch=$($remoteProduct.sha), expected=$ExpectedGitHubCommit." "blocker"))
    }
} catch {
    $results.Add((New-Result "GitHub refs" "fail" $_.Exception.Message "blocker"))
}

$blockers = @($results | Where-Object { $_.status -eq "fail" -and $_.severity -eq "blocker" })
$blocked = @($results | Where-Object { $_.status -eq "blocked" })
$warnings = @($results | Where-Object { $_.severity -eq "warn" -or $_.status -eq "skipped" })
$summary = [ordered]@{
    product = "EcoreX"
    version = $Version
    generatedAt = (Get-Date).ToString("o")
    releaseZip = $ReleaseZip
    publicBaseUrl = $base
    totals = [ordered]@{
        checks = $results.Count
        blockers = $blockers.Count
        blocked = $blocked.Count
        warnings = $warnings.Count
    }
    checks = $results
}

$summary | ConvertTo-Json -Depth 8

if ($blockers.Count -gt 0 -or ($blocked.Count -gt 0 -and -not $AllowPublicBlocked)) {
    exit 1
}
