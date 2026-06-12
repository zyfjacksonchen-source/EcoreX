param(
    [string]$ExpectedGitHubCommit = "",
    [switch]$SkipGitHub,
    [switch]$AllowPublicBlocked
)

$ErrorActionPreference = "Stop"
$scriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$baseHarness = Join-Path $scriptRoot "test-ecorex-v0.1.10-acceptance.ps1"

$params = @{
    Version = "0.1.11"
    ReleaseZip = "release-artifacts/EcoreX_0.1.11-public-release.zip"
    WindowsInstaller = "desktop/release/EcoreX_0.1.11_x64-setup.exe"
    GitProductBranch = "codex/ecorex-v0.1.11-productization"
}
if ($ExpectedGitHubCommit) {
    $params.ExpectedGitHubCommit = $ExpectedGitHubCommit
}
if ($SkipGitHub) {
    $params.SkipGitHub = $true
}
if ($AllowPublicBlocked) {
    $params.AllowPublicBlocked = $true
}

& $baseHarness @params
