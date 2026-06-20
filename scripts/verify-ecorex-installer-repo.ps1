param(
    [Parameter(Mandatory = $true)][string]$Root
)

$ErrorActionPreference = "Stop"

$rootResolved = [System.IO.Path]::GetFullPath($Root)
if (-not (Test-Path -LiteralPath $rootResolved)) {
    throw "Installer repo root does not exist: $rootResolved"
}

$allowedNames = @(
    "README.md",
    ".gitattributes",
    "manifest.json",
    "SHA256SUMS.txt",
    "latest.yml"
)
$allowedExtensions = @(
    ".exe",
    ".dmg",
    ".pkg",
    ".msi",
    ".yml",
    ".yaml",
    ".blockmap",
    ".sha256"
)
$forbiddenExtensions = @(
    ".py",
    ".pyc",
    ".pyo",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".mjs",
    ".cjs",
    ".css",
    ".html",
    ".sh",
    ".ps1",
    ".bat",
    ".cmd",
    ".go",
    ".rs",
    ".java",
    ".cs",
    ".swift",
    ".kt",
    ".php",
    ".rb"
)
$forbiddenNames = @(
    "Dockerfile",
    "docker-compose.yml",
    "package.json",
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "pyproject.toml",
    "requirements.txt",
    ".env"
)
$forbiddenDirs = @(
    ".git",
    ".github",
    "agent",
    "bridge",
    "channel",
    "common",
    "desktop",
    "deploy",
    "server",
    "site",
    "src",
    "tests",
    "scripts",
    "node_modules",
    "__pycache__"
)

$repoGitDir = Join-Path $rootResolved ".git"
$isInsideRootGit = {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $repoGitDir)) {
        return $false
    }
    $fullPath = [System.IO.Path]::GetFullPath($Path)
    return $fullPath.StartsWith($repoGitDir + [System.IO.Path]::DirectorySeparatorChar, [System.StringComparison]::OrdinalIgnoreCase)
}

$files = Get-ChildItem -LiteralPath $rootResolved -Recurse -File -Force | Where-Object {
    -not (& $isInsideRootGit $_.FullName)
}
if (-not ($files | Where-Object { $_.Name -eq "README.md" })) {
    throw "README.md is required for the public installer repository."
}

foreach ($dir in Get-ChildItem -LiteralPath $rootResolved -Recurse -Directory -Force) {
    if ($dir.FullName.Equals($repoGitDir, [System.StringComparison]::OrdinalIgnoreCase) -or (& $isInsideRootGit $dir.FullName)) {
        continue
    }
    if ($forbiddenDirs -contains $dir.Name) {
        throw "Forbidden source directory in installer-only repo: $($dir.FullName)"
    }
}

foreach ($file in $files) {
    $name = $file.Name
    $ext = $file.Extension.ToLowerInvariant()
    if ($forbiddenNames -contains $name) {
        throw "Forbidden source/build metadata file in installer-only repo: $($file.FullName)"
    }
    if ($forbiddenExtensions -contains $ext) {
        throw "Forbidden source file extension in installer-only repo: $($file.FullName)"
    }
    if ($name.EndsWith(".tar.gz") -or $ext -in @(".zip", ".gz", ".tgz")) {
        throw "Archive packages are not allowed in the public installer-only repo because they may contain source code: $($file.FullName)"
    }
    if (-not (($allowedNames -contains $name) -or ($allowedExtensions -contains $ext))) {
        throw "Unexpected file in installer-only repo: $($file.FullName)"
    }
}

foreach ($exe in $files | Where-Object { $_.Extension.ToLowerInvariant() -eq ".exe" }) {
    if (-not (Get-Command Get-AuthenticodeSignature -ErrorAction SilentlyContinue)) {
        throw "Get-AuthenticodeSignature is required to verify Windows installers: $($exe.FullName)"
    }
    $signature = Get-AuthenticodeSignature -LiteralPath $exe.FullName
    if ($signature.Status -ne "Valid") {
        throw "Windows installer is not Authenticode signed for public release: $($exe.FullName) (status=$($signature.Status))"
    }
}

$manifestPath = Join-Path $rootResolved "manifest.json"
if (Test-Path -LiteralPath $manifestPath) {
    $manifest = Get-Content -Raw -Encoding UTF8 -LiteralPath $manifestPath | ConvertFrom-Json
    $version = [string]$manifest.version
    $windowsInstaller = @($manifest.files | Where-Object { $_.id -eq "windows-x64-installer" }) | Select-Object -First 1
    if ($windowsInstaller) {
        $installerPath = Join-Path $rootResolved ([string]$windowsInstaller.fileName)
        if (-not (Test-Path -LiteralPath $installerPath)) {
            throw "Manifest Windows installer is missing: $installerPath"
        }
        $installerItem = Get-Item -LiteralPath $installerPath
        if ([int64]$windowsInstaller.size -ne [int64]$installerItem.Length) {
            throw "Manifest Windows installer size does not match file: $($windowsInstaller.fileName)"
        }
        $actualSha256 = (Get-FileHash -LiteralPath $installerPath -Algorithm SHA256).Hash.ToUpperInvariant()
        if ($actualSha256 -ne ([string]$windowsInstaller.sha256).ToUpperInvariant()) {
            throw "Manifest Windows installer sha256 does not match file: $($windowsInstaller.fileName)"
        }
        $latestPath = Join-Path $rootResolved "latest.yml"
        if (-not (Test-Path -LiteralPath $latestPath)) {
            throw "latest.yml is required when a Windows installer is present."
        }
        $latest = Get-Content -Raw -Encoding UTF8 -LiteralPath $latestPath
        if ($latest -notmatch "(?m)^version:\s*['""]?$([regex]::Escape($version))['""]?\s*$") {
            throw "latest.yml version does not match manifest version $version."
        }
        if ($latest -notmatch [regex]::Escape([string]$windowsInstaller.fileName)) {
            throw "latest.yml does not reference Windows installer $($windowsInstaller.fileName)."
        }
        if ($latest -notmatch "(?m)^\s*size:\s*$([int64]$installerItem.Length)\s*$") {
            throw "latest.yml size does not match Windows installer size."
        }
        $sha512 = [System.Security.Cryptography.SHA512]::Create()
        try {
            $stream = [System.IO.File]::OpenRead($installerPath)
            try {
                $expectedSha512 = [Convert]::ToBase64String($sha512.ComputeHash($stream))
            }
            finally {
                $stream.Dispose()
            }
        }
        finally {
            $sha512.Dispose()
        }
        if ($latest -notmatch [regex]::Escape($expectedSha512)) {
            throw "latest.yml sha512 does not match Windows installer."
        }
        $windowsBlockmap = @($manifest.files | Where-Object { $_.id -eq "windows-blockmap" }) | Select-Object -First 1
        if (-not $windowsBlockmap) {
            throw "Windows blockmap is required when a Windows installer is present."
        }
        $blockmapPath = Join-Path $rootResolved ([string]$windowsBlockmap.fileName)
        if (-not (Test-Path -LiteralPath $blockmapPath)) {
            throw "Manifest Windows blockmap is missing: $($windowsBlockmap.fileName)"
        }
        $blockmapItem = Get-Item -LiteralPath $blockmapPath
        if ([int64]$windowsBlockmap.size -ne [int64]$blockmapItem.Length) {
            throw "Manifest Windows blockmap size does not match file: $($windowsBlockmap.fileName)"
        }
        $actualBlockmapSha256 = (Get-FileHash -LiteralPath $blockmapPath -Algorithm SHA256).Hash.ToUpperInvariant()
        if ($actualBlockmapSha256 -ne ([string]$windowsBlockmap.sha256).ToUpperInvariant()) {
            throw "Manifest Windows blockmap sha256 does not match file: $($windowsBlockmap.fileName)"
        }
    }
}

Write-Host "PASS installer-only repo validation: $rootResolved"
