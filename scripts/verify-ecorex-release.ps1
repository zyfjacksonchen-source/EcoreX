param(
    [string]$PublicBaseUrl = "https://www.ecoreai.cn/ecorex-agent",
    [string]$ExpectedVersion = "0.1.15",
    [string]$LocalWindowsInstaller = "",
    [string]$LocalMacArm64Dmg = "",
    [string]$LocalMacX64Dmg = "",
    [string]$ClientEventKey = "",
    [string]$ClientUserToken = "",
    [string]$ClientDeviceId = "verify-device",
    [string]$GitRemoteUrl = "https://github.com/zhangyifanjackson-dotcom/EcoreX.git",
    [string]$GitProductBranch = "codex/ecorex-v0.1.15",
    [string]$ExpectedGitHubCommit = "",
    [switch]$SkipMacArtifacts = $true,
    [switch]$SkipGitRemoteCheck
)

$ErrorActionPreference = "Stop"

[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

function New-Check {
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

function Invoke-Head {
    param([string]$Url)

    try {
        $headerText = & curl.exe -I -L --silent --show-error --max-time 60 $Url
        if ($LASTEXITCODE -ne 0) {
            throw "curl exited with $LASTEXITCODE"
        }
        $statusMatches = [regex]::Matches(($headerText -join "`n"), "HTTP/\S+\s+(\d+)")
        $lengthMatches = [regex]::Matches(($headerText -join "`n"), "(?im)^Content-Length:\s*(\d+)")
        $statusCode = $null
        $contentLength = $null
        if ($statusMatches.Count -gt 0) {
            $statusCode = [int]$statusMatches[$statusMatches.Count - 1].Groups[1].Value
        }
        if ($lengthMatches.Count -gt 0) {
            $contentLength = [int64]$lengthMatches[$lengthMatches.Count - 1].Groups[1].Value
        }
        return [ordered]@{
            ok = ($statusCode -ge 200 -and $statusCode -lt 300)
            statusCode = $statusCode
            contentLength = $contentLength
        }
    } catch {
        return [ordered]@{
            ok = $false
            statusCode = $null
            error = $_.Exception.Message
        }
    }
}

function Invoke-GetJson {
    param([string]$Url)

    $jsonText = & curl.exe -L --fail --silent --show-error --max-time 60 $Url
    if ($LASTEXITCODE -ne 0) {
        throw "curl exited with $LASTEXITCODE while reading $Url"
    }
    return ($jsonText -join "`n") | ConvertFrom-Json
}

function Invoke-Status {
    param([string]$Url)

    $status = & curl.exe -L --silent --show-error --max-time 60 -o NUL -w "%{http_code}" $Url
    if ($LASTEXITCODE -ne 0) {
        throw "curl exited with $LASTEXITCODE while checking $Url"
    }
    return [int]($status -join "")
}

function Invoke-ClientJson {
    param(
        [string]$Url,
        [string]$Key,
        [string]$UserToken = "",
        [string]$DeviceId = ""
    )

    $headers = @("-H", "X-EcoreX-Client-Key: $Key")
    if ($UserToken) {
        $headers += @("-H", "X-EcoreX-User-Token: $UserToken")
    }
    if ($DeviceId) {
        $headers += @("-H", "X-EcoreX-Device-Id: $DeviceId")
    }

    $jsonText = & curl.exe -L --fail --silent --show-error --max-time 60 @headers $Url
    if ($LASTEXITCODE -ne 0) {
        throw "curl exited with $LASTEXITCODE while checking $Url"
    }
    return ($jsonText -join "`n") | ConvertFrom-Json
}

function Invoke-ClientStatus {
    param(
        [string]$Url,
        [string]$Key,
        [string]$UserToken = "",
        [string]$DeviceId = ""
    )

    $headers = @("-H", "X-EcoreX-Client-Key: $Key")
    if ($UserToken) {
        $headers += @("-H", "X-EcoreX-User-Token: $UserToken")
    }
    if ($DeviceId) {
        $headers += @("-H", "X-EcoreX-Device-Id: $DeviceId")
    }

    $status = & curl.exe -L --silent --show-error --max-time 60 -o NUL -w "%{http_code}" @headers $Url
    if ($LASTEXITCODE -ne 0) {
        throw "curl exited with $LASTEXITCODE while checking $Url"
    }
    return [int]($status -join "")
}

function Get-GitHubRepoParts {
    param([string]$RemoteUrl)

    if ($RemoteUrl -match "github\.com[:/](?<owner>[^/]+)/(?<repo>[^/.]+)(\.git)?$") {
        return [ordered]@{
            owner = $Matches.owner
            repo = $Matches.repo
        }
    }

    return $null
}

function Get-GitHubApiToken {
    foreach ($name in @("ECOREX_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN")) {
        $value = [Environment]::GetEnvironmentVariable($name)
        if ($value) {
            return $value
        }
    }
    if (Get-Command git -ErrorAction SilentlyContinue) {
        $previousErrorActionPreference = $ErrorActionPreference
        try {
            $ErrorActionPreference = "Continue"
            $credential = "protocol=https`nhost=github.com`n`n" | git credential fill 2>$null
            if ($LASTEXITCODE -eq 0 -and $credential) {
                $password = (($credential -split "`n") | Where-Object { $_ -like "password=*" } | Select-Object -First 1) -replace "^password=", ""
                if ($password) {
                    return $password
                }
            }
        } finally {
            $ErrorActionPreference = $previousErrorActionPreference
        }
    }
    return ""
}

function Get-RemoteBranchHead {
    param(
        [string]$RemoteUrl,
        [string]$Branch
    )

    $ref = "refs/heads/$Branch"
    $previousErrorActionPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        $gitOutput = git ls-remote $RemoteUrl $ref 2>$null
        $gitExitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }
    if ($gitExitCode -eq 0 -and $gitOutput) {
        $sha = (($gitOutput -join "`n") -split "\s+")[0]
        if ($sha) {
            return [ordered]@{
                sha = $sha
                source = "git ls-remote"
            }
        }
    }

    $repoParts = Get-GitHubRepoParts $RemoteUrl
    if (-not $repoParts) {
        throw "git ls-remote failed and $RemoteUrl is not a supported GitHub remote URL"
    }

    $token = Get-GitHubApiToken
    $headers = @{
        "Accept" = "application/vnd.github+json"
        "User-Agent" = "ecorex-release-verifier"
    }
    if ($token) {
        $headers["Authorization"] = "Bearer $token"
    }

    # GitHub's "Get a reference" endpoint is singular /git/ref/{ref}.
    # Branch names with slashes stay as path segments; encoding slash as %2F
    # or using plural /git/refs/... returns 404.
    $branchPath = $Branch
    $url = "https://api.github.com/repos/$($repoParts.owner)/$($repoParts.repo)/git/ref/heads/$branchPath"
    try {
        $response = Invoke-WebRequest -UseBasicParsing -Uri $url -Headers $headers -TimeoutSec 60
        $payload = $response.Content | ConvertFrom-Json
        if ($payload.object.sha) {
            return [ordered]@{
                sha = [string]$payload.object.sha
                source = "GitHub API"
            }
        }
    } catch {
        throw "git ls-remote failed and GitHub API fallback failed for ${Branch}: $($_.Exception.Message)"
    }

    throw "No SHA returned for $Branch"
}

function Assert-Hash {
    param(
        [string]$Path,
        [string]$ExpectedHash
    )

    if (-not (Test-Path -LiteralPath $Path)) {
        return New-Check "Local artifact hash: $Path" "missing" "File does not exist." "warn"
    }

    $hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $Path).Hash.ToUpperInvariant()
    if ($hash -eq $ExpectedHash.ToUpperInvariant()) {
        return New-Check "Local artifact hash: $Path" "pass" "SHA256 $hash"
    }

    return New-Check "Local artifact hash: $Path" "fail" "Expected $ExpectedHash but got $hash." "blocker"
}

$checks = New-Object System.Collections.Generic.List[object]
$base = $PublicBaseUrl.TrimEnd("/")

$manifestUrl = "$base/manifest.json"
$manifest = Invoke-GetJson $manifestUrl

if ($manifest.product -eq "EcoreX" -and $manifest.version -eq $ExpectedVersion) {
    $checks.Add((New-Check "Manifest product/version" "pass" "$manifestUrl returned EcoreX $ExpectedVersion."))
} else {
    $checks.Add((New-Check "Manifest product/version" "fail" "$manifestUrl returned product=$($manifest.product), version=$($manifest.version)." "blocker"))
}

$artifacts = @{}
foreach ($artifact in $manifest.artifacts) {
    $artifacts[$artifact.id] = $artifact
}

foreach ($requiredId in @("windows-x64", "macos-arm64-dmg", "macos-x64-dmg")) {
    if ($SkipMacArtifacts -and $requiredId.StartsWith("macos-")) {
        if ($artifacts.ContainsKey($requiredId)) {
            $artifact = $artifacts[$requiredId]
            $checks.Add((New-Check "Manifest artifact: $requiredId" "skipped" "$($artifact.fileName) is deferred to Mac validation." "warn"))
        } else {
            $checks.Add((New-Check "Manifest artifact: $requiredId" "skipped" "Deferred to Mac validation." "warn"))
        }
        continue
    }
    if ($artifacts.ContainsKey($requiredId)) {
        $artifact = $artifacts[$requiredId]
        $checks.Add((New-Check "Manifest artifact: $requiredId" "pass" "$($artifact.fileName), $($artifact.size), $($artifact.sha256)"))
    } else {
        $checks.Add((New-Check "Manifest artifact: $requiredId" "fail" "Missing from manifest." "blocker"))
    }
}

foreach ($artifact in $manifest.artifacts) {
    if (($artifact.status -like "pending-*") -or ($SkipMacArtifacts -and $artifact.id -like "macos-*")) {
        $checks.Add((New-Check "Public download: $($artifact.fileName)" "skipped" "Artifact status is $($artifact.status); not required in this verification pass." "warn"))
        continue
    }
    $downloadUrl = "$base/$($artifact.href)"
    $head = Invoke-Head $downloadUrl
    if ($head.ok -and $head.statusCode -eq 200) {
        $checks.Add((New-Check "Public download: $($artifact.fileName)" "pass" "HTTP 200, Content-Length $($head.contentLength)."))
    } else {
        $checks.Add((New-Check "Public download: $($artifact.fileName)" "fail" "HTTP $($head.statusCode): $($head.error)" "blocker"))
    }
}

$adminHead = Invoke-Head "$base/admin/"
if (-not $adminHead.ok -and $adminHead.statusCode -eq 401) {
    $checks.Add((New-Check "Admin page auth gate" "pass" "Unauthenticated request returned HTTP 401."))
} else {
    $checks.Add((New-Check "Admin page auth gate" "fail" "Expected HTTP 401, got $($adminHead.statusCode)." "blocker"))
}

try {
    $statusCode = Invoke-Status "$base/client/model-config"
    if ($statusCode -eq 403) {
        $checks.Add((New-Check "Client model-config auth gate" "pass" "Unauthenticated request returned HTTP 403."))
    } else {
        $checks.Add((New-Check "Client model-config auth gate" "fail" "Expected HTTP 403, got $statusCode." "blocker"))
    }
} catch {
    $checks.Add((New-Check "Client model-config auth gate" "fail" "Unauthenticated request unexpectedly succeeded." "blocker"))
}

try {
    $statusCode = Invoke-Status "$base/client/capability-policy"
    if ($statusCode -eq 403) {
        $checks.Add((New-Check "Client capability-policy auth gate" "pass" "Unauthenticated request returned HTTP 403."))
    } else {
        $checks.Add((New-Check "Client capability-policy auth gate" "fail" "Expected HTTP 403, got $statusCode." "blocker"))
    }
} catch {
    $checks.Add((New-Check "Client capability-policy auth gate" "fail" "Capability policy unauthenticated check failed: $($_.Exception.Message)" "blocker"))
}

if ($ClientEventKey) {
    try {
        $statusCode = Invoke-ClientStatus "$base/client/model-config" $ClientEventKey
        if ($statusCode -eq 401) {
            $checks.Add((New-Check "Client model-config user-token gate" "pass" "Client key without user token returned HTTP 401."))
        } else {
            $checks.Add((New-Check "Client model-config user-token gate" "fail" "Expected HTTP 401 with client key only, got $statusCode." "blocker"))
        }
    } catch {
        $checks.Add((New-Check "Client model-config user-token gate" "fail" $_.Exception.Message "blocker"))
    }

    if ($ClientUserToken) {
        try {
            $modelConfig = Invoke-ClientJson "$base/client/model-config" $ClientEventKey $ClientUserToken $ClientDeviceId
            if ($modelConfig.configured -and $modelConfig.settings) {
                $checks.Add((New-Check "Client model-config authenticated session" "pass" "Configured model policy returned for an authenticated enterprise session."))
            } else {
                $checks.Add((New-Check "Client model-config authenticated session" "fail" "Model policy did not return a configured settings object." "blocker"))
            }
        } catch {
            $checks.Add((New-Check "Client model-config authenticated session" "fail" $_.Exception.Message "blocker"))
        }
    } else {
        $checks.Add((New-Check "Client model-config authenticated session" "skipped" "Pass -ClientUserToken with a valid enterprise session token to verify configured model delivery." "warn"))
    }

    try {
        $capabilityPolicy = Invoke-ClientJson "$base/client/capability-policy" $ClientEventKey
        if ($capabilityPolicy.ok -and $capabilityPolicy.policy -and $capabilityPolicy.capabilities) {
            $checks.Add((New-Check "Client capability-policy authenticated" "pass" "Mode $($capabilityPolicy.policy.mode), capabilities $($capabilityPolicy.capabilities.Count)."))
        } else {
            $checks.Add((New-Check "Client capability-policy authenticated" "fail" "Capability policy did not return policy and capabilities." "blocker"))
        }
    } catch {
        $checks.Add((New-Check "Client capability-policy authenticated" "fail" $_.Exception.Message "blocker"))
    }
} else {
    $checks.Add((New-Check "Client authenticated policy checks" "skipped" "Pass -ClientEventKey to verify authenticated model and capability policy endpoints." "warn"))
}

if ($LocalWindowsInstaller) {
    $checks.Add((Assert-Hash $LocalWindowsInstaller $artifacts["windows-x64"].sha256))
    if (Test-Path -LiteralPath $LocalWindowsInstaller) {
        $signature = Get-AuthenticodeSignature -LiteralPath $LocalWindowsInstaller
        if ($signature.Status -eq "Valid") {
            $checks.Add((New-Check "Windows Authenticode signature" "pass" "Signature verified."))
        } else {
            $checks.Add((New-Check "Windows Authenticode signature" "fail" "$($signature.Status): $($signature.StatusMessage)" "blocker"))
        }
    }
} else {
    $checks.Add((New-Check "Windows local installer signature" "skipped" "Pass -LocalWindowsInstaller to verify local Authenticode signature." "warn"))
}

if ($SkipMacArtifacts) {
    $checks.Add((New-Check "macOS local DMG hash" "skipped" "macOS signing/notarization/Gatekeeper validation is deferred to a Mac." "warn"))
} elseif ($LocalMacArm64Dmg) {
    $checks.Add((Assert-Hash $LocalMacArm64Dmg $artifacts["macos-arm64-dmg"].sha256))
} else {
    $checks.Add((New-Check "macOS arm64 local DMG hash" "skipped" "Pass -LocalMacArm64Dmg to verify local artifact hash." "warn"))
}

if ($SkipMacArtifacts) {
    $checks.Add((New-Check "macOS x64 local DMG hash" "skipped" "macOS validation is deferred to a Mac." "warn"))
} elseif ($LocalMacX64Dmg) {
    $checks.Add((Assert-Hash $LocalMacX64Dmg $artifacts["macos-x64-dmg"].sha256))
} else {
    $checks.Add((New-Check "macOS x64 local DMG hash" "skipped" "Pass -LocalMacX64Dmg to verify local artifact hash." "warn"))
}

if ($SkipGitRemoteCheck) {
    $checks.Add((New-Check "GitHub main sync" "skipped" "Skipped by -SkipGitRemoteCheck." "warn"))
} else {
    try {
        $localHead = git rev-parse HEAD
        if ($LASTEXITCODE -ne 0) {
            throw "git rev-parse failed"
        }
        $remoteRef = Get-RemoteBranchHead $GitRemoteUrl "main"
        $remoteHead = $remoteRef.sha
        $productRef = Get-RemoteBranchHead $GitRemoteUrl $GitProductBranch
        $productHead = $productRef.sha
        $dirty = git status --porcelain
        if ($LASTEXITCODE -ne 0) {
            throw "git status failed"
        }
        if ($dirty) {
            $checks.Add((New-Check "GitHub main sync" "fail" "Worktree has uncommitted changes." "blocker"))
        } elseif ($productHead -ne $remoteHead) {
            $checks.Add((New-Check "GitHub main sync" "fail" "Remote main $remoteHead differs from $GitProductBranch $productHead." "blocker"))
        } elseif ($ExpectedGitHubCommit) {
            if ($remoteHead -eq $ExpectedGitHubCommit) {
                $checks.Add((New-Check "GitHub main sync" "pass" "Remote main and $GitProductBranch match expected commit $ExpectedGitHubCommit. Sources: main=$($remoteRef.source), product=$($productRef.source). Worktree is clean."))
            } else {
                $checks.Add((New-Check "GitHub main sync" "fail" "Remote main $remoteHead differs from expected $ExpectedGitHubCommit." "blocker"))
            }
        } elseif ($localHead -eq $remoteHead) {
            $checks.Add((New-Check "GitHub main sync" "pass" "Local HEAD matches remote main: $localHead and worktree is clean."))
        } else {
            $checks.Add((New-Check "GitHub main sync" "fail" "Local HEAD $localHead differs from remote main $remoteHead. Pass -ExpectedGitHubCommit when the remote was synced as a clean snapshot/API commit." "blocker"))
        }
    } catch {
        $checks.Add((New-Check "GitHub main sync" "fail" $_.Exception.Message "blocker"))
    }
}

$blockers = @($checks | Where-Object { $_.status -eq "fail" -and $_.severity -eq "blocker" })
$warnings = @($checks | Where-Object { $_.status -in @("missing", "skipped") -or $_.severity -eq "warn" })

$result = [ordered]@{
    product = "EcoreX"
    version = $ExpectedVersion
    publicBaseUrl = $base
    generatedAt = (Get-Date).ToString("o")
    summary = [ordered]@{
        total = $checks.Count
        blockers = $blockers.Count
        warnings = $warnings.Count
    }
    checks = $checks
}

$result | ConvertTo-Json -Depth 8

if ($blockers.Count -gt 0) {
    exit 1
}
