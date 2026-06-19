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
    ".json",
    ".txt",
    ".md",
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

$files = Get-ChildItem -LiteralPath $rootResolved -Recurse -File -Force
if (-not ($files | Where-Object { $_.Name -eq "README.md" })) {
    throw "README.md is required for the public installer repository."
}

foreach ($dir in Get-ChildItem -LiteralPath $rootResolved -Recurse -Directory -Force) {
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

Write-Host "PASS installer-only repo validation: $rootResolved"
