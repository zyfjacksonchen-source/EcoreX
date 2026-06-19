param(
    [string]$Version = "0.1.16",
    [string]$RuntimeRoot = ".",
    [string]$SiteRoot = "deploy/ecorex-site",
    [string]$WebBuildRoot = "",
    [string]$OutputDir = "release-artifacts",
    [switch]$KeepStaging
)

$ErrorActionPreference = "Stop"

function Resolve-RequiredPath {
    param([Parameter(Mandatory = $true)][string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) {
        throw "Required path does not exist: $Path"
    }
    return (Resolve-Path -LiteralPath $Path).Path
}

function Resolve-UnderDirectory {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Base
    )
    $resolvedBase = [System.IO.Path]::GetFullPath($Base)
    $resolvedPath = [System.IO.Path]::GetFullPath($Path)
    if (-not $resolvedPath.StartsWith($resolvedBase, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Resolved path '$resolvedPath' is outside '$resolvedBase'"
    }
    return $resolvedPath
}

function Copy-IfExists {
    param(
        [Parameter(Mandatory = $true)][string]$Source,
        [Parameter(Mandatory = $true)][string]$Destination
    )
    if (Test-Path -LiteralPath $Source) {
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $Destination) | Out-Null
        Copy-Item -LiteralPath $Source -Destination $Destination -Force
    }
}

function Copy-DirectoryIfExists {
    param(
        [Parameter(Mandatory = $true)][string]$Source,
        [Parameter(Mandatory = $true)][string]$Destination
    )
    if (Test-Path -LiteralPath $Source) {
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $Destination) | Out-Null
        Copy-Item -LiteralPath $Source -Destination $Destination -Recurse -Force
    }
}

function Copy-DirectoryContentsIfExists {
    param(
        [Parameter(Mandatory = $true)][string]$Source,
        [Parameter(Mandatory = $true)][string]$Destination
    )
    if (Test-Path -LiteralPath $Source) {
        New-Item -ItemType Directory -Force -Path $Destination | Out-Null
        Copy-Item -Path (Join-Path $Source "*") -Destination $Destination -Recurse -Force
    }
}

function Remove-GeneratedNoise {
    param([Parameter(Mandatory = $true)][string]$Root)
    if (-not (Test-Path -LiteralPath $Root)) {
        return
    }
    Get-ChildItem -LiteralPath $Root -Recurse -Force -Directory |
        Where-Object { $_.Name -in "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache" } |
        ForEach-Object { Remove-Item -LiteralPath $_.FullName -Recurse -Force }
    Get-ChildItem -LiteralPath $Root -Recurse -Force -File |
        Where-Object { $_.Name -like "*.pyc" -or $_.Name -like "*.pyo" -or $_.Name -eq ".DS_Store" } |
        ForEach-Object { Remove-Item -LiteralPath $_.FullName -Force }
}

function Invoke-ReleaseRuntimeSanitizer {
    param([Parameter(Mandatory = $true)][string]$RuntimeDir)
    $sanitizer = Join-Path $repoRoot "scripts\sanitize-ecorex-release-runtime.py"
    if (-not (Test-Path -LiteralPath $sanitizer)) {
        throw "Release sanitizer missing: $sanitizer"
    }
    & python $sanitizer $RuntimeDir
    if ($LASTEXITCODE -ne 0) {
        throw "Release runtime sanitizer failed for $RuntimeDir"
    }
}

function Write-Utf8NoBom {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Value
    )
    $encoding = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($Path, $Value, $encoding)
}

function Get-ReleaseMigrationReadmeNote {
    $readmePath = Join-Path $repoRoot "desktop\build\README-migration.txt"
    if (-not (Test-Path -LiteralPath $readmePath)) {
        throw "Release migration README missing: $readmePath"
    }
    return (Get-Content -Raw -Encoding UTF8 -LiteralPath $readmePath).TrimEnd()
}

$repoRoot = (Resolve-Path -LiteralPath ".").Path
$runtimeRootResolved = Resolve-RequiredPath $RuntimeRoot
$siteRootResolved = Resolve-RequiredPath $SiteRoot
$outputResolved = [System.IO.Path]::GetFullPath((Join-Path $repoRoot $OutputDir))
New-Item -ItemType Directory -Force -Path $outputResolved | Out-Null

$desktopRoot = [System.IO.Path]::GetFullPath((Join-Path $repoRoot "desktop"))
$webBuildKindOverride = ""
if ($WebBuildRoot) {
    $webBuildResolved = Resolve-RequiredPath $WebBuildRoot
    $webBuildFull = [System.IO.Path]::GetFullPath($webBuildResolved)
} else {
    $desktopDist = Join-Path $desktopRoot "dist"
    if (Test-Path -LiteralPath (Join-Path $desktopDist "index.html")) {
        $webBuildResolved = Resolve-RequiredPath $desktopDist
        $webBuildKindOverride = "desktop-renderer-build"
    } else {
        $webBuildResolved = ""
    }
}

$stagingRoot = Resolve-UnderDirectory -Path (Join-Path $outputResolved "ecorex-web-linux-service-$Version") -Base $outputResolved
$tarPath = Resolve-UnderDirectory -Path (Join-Path $outputResolved "EcoreX_${Version}-web-linux-service.tar.gz") -Base $outputResolved
$shaPath = "$tarPath.sha256"

if (Test-Path -LiteralPath $stagingRoot) {
    Remove-Item -LiteralPath $stagingRoot -Recurse -Force
}
if (Test-Path -LiteralPath $tarPath) {
    Remove-Item -LiteralPath $tarPath -Force
}
if (Test-Path -LiteralPath $shaPath) {
    Remove-Item -LiteralPath $shaPath -Force
}

$runtimeOut = Join-Path $stagingRoot "runtime"
$scriptsOut = Join-Path $stagingRoot "scripts"
$serviceOut = Join-Path $stagingRoot "service"
$webBuildOut = Join-Path $stagingRoot "web-build"
$requirementsOut = Join-Path $stagingRoot "requirements"
New-Item -ItemType Directory -Force -Path $runtimeOut, $scriptsOut, $serviceOut, $webBuildOut, $requirementsOut | Out-Null

$webReleaseReadme = @"
EcoreX WebUI service release package

This archive contains the EcoreX WebUI service runtime, web build, installation helpers, service templates, and checksums for EcoreX $Version.

"@
Write-Utf8NoBom -Path (Join-Path $stagingRoot "README.txt") -Value $webReleaseReadme

$runtimeFiles = @(
    "app.py",
    "config.py",
    "config-template.json",
    "requirements.txt",
    "requirements-optional.txt",
    "pyproject.toml",
    "README.md",
    "LICENSE"
)
foreach ($fileName in $runtimeFiles) {
    Copy-IfExists -Source (Join-Path $runtimeRootResolved $fileName) -Destination (Join-Path $runtimeOut $fileName)
}

$runtimeDirs = @(
    "agent",
    "bridge",
    "channel",
    "cli",
    "common",
    "models",
    "plugins",
    "skills",
    "translate",
    "voice"
)
foreach ($dirName in $runtimeDirs) {
    Copy-DirectoryIfExists -Source (Join-Path $runtimeRootResolved $dirName) -Destination (Join-Path $runtimeOut $dirName)
}

Remove-GeneratedNoise -Root $runtimeOut
Invoke-ReleaseRuntimeSanitizer -RuntimeDir $runtimeOut

$appDir = Join-Path $runtimeOut "channel/web/static/app"
New-Item -ItemType Directory -Force -Path $appDir | Out-Null

$webBuildKind = "legacy-webui-fallback"
if ($webBuildResolved) {
    $webBuildKind = if ($webBuildKindOverride) { $webBuildKindOverride } else { "provided-web-build" }
    Copy-DirectoryIfExists -Source $webBuildResolved -Destination (Join-Path $webBuildOut "app")
    if (Test-Path -LiteralPath $appDir) {
        Remove-Item -LiteralPath $appDir -Recurse -Force
    }
    New-Item -ItemType Directory -Force -Path $appDir | Out-Null
    Copy-DirectoryContentsIfExists -Source $webBuildResolved -Destination $appDir
} elseif (Test-Path -LiteralPath (Join-Path $appDir "index.html")) {
    $webBuildKind = "source-static-app"
    Copy-DirectoryIfExists -Source $appDir -Destination (Join-Path $webBuildOut "app")
} else {
    $fallbackIndex = @'
<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>EcoreX WebUI</title>
    <meta http-equiv="refresh" content="0; url=../chat" />
    <script>
      window.location.replace(new URL("../chat", window.location.href).toString());
    </script>
  </head>
  <body>
    <a href="../chat">Open EcoreX WebUI</a>
  </body>
</html>
'@
    Set-Content -LiteralPath (Join-Path $appDir "index.html") -Value $fallbackIndex -Encoding UTF8
}

Invoke-ReleaseRuntimeSanitizer -RuntimeDir $runtimeOut

$legacyBuildOut = Join-Path $webBuildOut "legacy-webui"
New-Item -ItemType Directory -Force -Path $legacyBuildOut | Out-Null
Copy-IfExists -Source (Join-Path $runtimeOut "channel/web/chat.html") -Destination (Join-Path $legacyBuildOut "chat.html")
Copy-DirectoryIfExists -Source (Join-Path $runtimeOut "channel/web/static") -Destination (Join-Path $legacyBuildOut "static")

Copy-IfExists -Source (Join-Path $runtimeOut "requirements.txt") -Destination (Join-Path $requirementsOut "runtime-requirements.txt")
Copy-IfExists -Source (Join-Path $runtimeOut "requirements-optional.txt") -Destination (Join-Path $requirementsOut "runtime-requirements-optional.txt")

$scriptFiles = @(
    @{ Source = "scripts/install-ecorex-web.sh"; Target = "install-ecorex-web.sh" },
    @{ Source = "scripts/check-ecorex-web-release.sh"; Target = "check-ecorex-web-release.sh" }
)
foreach ($entry in $scriptFiles) {
    Copy-IfExists -Source (Join-Path $repoRoot $entry.Source) -Destination (Join-Path $scriptsOut $entry.Target)
}

$serviceFiles = @(
    @{ Source = "deploy/ecorex-site/caddy/Caddyfile.example"; Target = "caddy/Caddyfile.example" },
    @{ Source = "deploy/ecorex-site/caddy/ecorex-agent.routes.caddy"; Target = "caddy/ecorex-agent.routes.caddy" },
    @{ Source = "deploy/ecorex-site/caddy/ecorex-web.routes.caddy"; Target = "caddy/ecorex-web.routes.caddy" },
    @{ Source = "deploy/ecorex-site/nginx/ecorex-agent.conf.example"; Target = "nginx/ecorex-agent.conf.example" },
    @{ Source = "deploy/ecorex-site/nginx/ecorex-web.conf.example"; Target = "nginx/ecorex-web.conf.example" },
    @{ Source = "deploy/ecorex-site/systemd/ecorex-web.service.example"; Target = "systemd/ecorex-web.service.example" }
)
foreach ($entry in $serviceFiles) {
    Copy-IfExists -Source (Join-Path $repoRoot $entry.Source) -Destination (Join-Path $serviceOut $entry.Target)
}

$releaseJson = [ordered]@{
    product = "EcoreX"
    version = $Version
    artifactId = "web-linux-service"
    artifactFile = "EcoreX_${Version}-web-linux-service.tar.gz"
    generatedAt = (Get-Date).ToUniversalTime().ToString("o")
    runtimeRoot = "runtime"
    webBuild = $webBuildKind
    includesDesktopArtifacts = $false
    defaultInstallRoot = "/opt/ecorex-web"
    defaultWorkspaceRoot = "/srv/ecorex-agent-workspace"
    defaultWebPort = 9909
    serviceName = "ecorex-web"
}
Write-Utf8NoBom -Path (Join-Path $stagingRoot "release.json") -Value (($releaseJson | ConvertTo-Json -Depth 6) + "`n")

$fileRows = @()
$stagingPrefix = $stagingRoot.TrimEnd("\", "/") + [System.IO.Path]::DirectorySeparatorChar
Get-ChildItem -LiteralPath $stagingRoot -Recurse -File | Sort-Object FullName | ForEach-Object {
    $relativePath = $_.FullName.Substring($stagingPrefix.Length).Replace("\", "/")
    if ($relativePath -in "SHA256SUMS.txt", "checksums.json") {
        return
    }
    $hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $_.FullName).Hash.ToUpperInvariant()
    $fileRows += [pscustomobject]@{
        path = $relativePath
        size = $_.Length
        sha256 = $hash
    }
}

$shaLines = $fileRows | ForEach-Object { "$($_.sha256)  $($_.path)" }
$shaLines | Set-Content -LiteralPath (Join-Path $stagingRoot "SHA256SUMS.txt") -Encoding ASCII

$checksums = [ordered]@{
    product = "EcoreX"
    version = $Version
    artifactId = "web-linux-service"
    generatedAt = (Get-Date).ToUniversalTime().ToString("o")
    files = $fileRows
}
Write-Utf8NoBom -Path (Join-Path $stagingRoot "checksums.json") -Value (($checksums | ConvertTo-Json -Depth 8) + "`n")

if (-not (Get-Command tar -ErrorAction SilentlyContinue)) {
    throw "tar command not found; cannot create .tar.gz release artifact."
}

$stagingParent = Split-Path -Parent $stagingRoot
$stagingLeaf = Split-Path -Leaf $stagingRoot
& tar -czf $tarPath -C $stagingParent $stagingLeaf
if ($LASTEXITCODE -ne 0) {
    throw "tar failed with exit code $LASTEXITCODE"
}

$tarHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $tarPath).Hash.ToUpperInvariant()
$tarItem = Get-Item -LiteralPath $tarPath
"$tarHash  $(Split-Path -Leaf $tarPath)" | Set-Content -LiteralPath $shaPath -Encoding ASCII

if (-not $KeepStaging) {
    Remove-Item -LiteralPath $stagingRoot -Recurse -Force
}

[ordered]@{
    ok = $true
    version = $Version
    artifact = $tarPath
    size = $tarItem.Length
    sha256 = $tarHash
    webBuild = $webBuildKind
    includesDesktopArtifacts = $false
    sha256File = $shaPath
} | ConvertTo-Json -Depth 6
