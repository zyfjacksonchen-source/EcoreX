param(
    [string]$Version = "0.2.0",
    [string]$Repo = "zhangyifanjackson-dotcom/EcoreX",
    [string]$Ref = "codex/ecorex-v0.2.0",
    [string]$Workflow = "ecorex-desktop-release.yml",
    [ValidateSet("all", "arm64", "x64")]
    [string]$MacArch = "all",
    [switch]$Notarize,
    [string]$OutputDir = "release-artifacts\macos-dmg-workflow",
    [switch]$DispatchOnly,
    [switch]$DownloadOnly,
    [int64]$RunId = 0,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"

function Resolve-RepoRoot {
    $root = (& git rev-parse --show-toplevel 2>$null)
    if ($LASTEXITCODE -ne 0 -or -not $root) {
        throw "Run this script from inside the EcoreX git checkout."
    }
    return [System.IO.Path]::GetFullPath($root.Trim())
}

function Assert-GhReady {
    if (-not (Get-Command gh -ErrorAction SilentlyContinue)) {
        throw "GitHub CLI 'gh' was not found on PATH."
    }
    $hasEnvToken = -not [string]::IsNullOrWhiteSpace($env:GH_TOKEN) -or -not [string]::IsNullOrWhiteSpace($env:GITHUB_TOKEN)
    if ($hasEnvToken) {
        return
    }
    $previousErrorActionPreference = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        & gh auth status --hostname github.com *> $null
        $exitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }
    if ($exitCode -ne 0) {
        throw "GitHub CLI is not authenticated and no GH_TOKEN/GITHUB_TOKEN environment variable is set. Authenticate with 'gh auth login' or set a token in the shell environment; do not pass tokens as script arguments."
    }
}

function Invoke-GhJson {
    param([Parameter(Mandatory = $true)][string[]]$Arguments)
    $output = & gh @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "gh $($Arguments -join ' ') failed with exit code $LASTEXITCODE"
    }
    return $output | ConvertFrom-Json
}

function Get-LatestWorkflowDispatchRun {
    param(
        [Parameter(Mandatory = $true)][string]$Repo,
        [Parameter(Mandatory = $true)][string]$Workflow,
        [Parameter(Mandatory = $true)][string]$Ref
    )
    $runs = Invoke-GhJson -Arguments @(
        "run", "list",
        "--repo", $Repo,
        "--workflow", $Workflow,
        "--branch", $Ref,
        "--event", "workflow_dispatch",
        "--limit", "10",
        "--json", "databaseId,headBranch,status,conclusion,createdAt,displayTitle,url"
    )
    $run = @($runs | Where-Object { [string]$_.headBranch -eq $Ref } | Sort-Object createdAt -Descending | Select-Object -First 1)
    if (-not $run) {
        throw "Could not find a workflow_dispatch run for $Workflow on ref $Ref."
    }
    return $run
}

function Download-MacArtifacts {
    param(
        [Parameter(Mandatory = $true)][string]$Repo,
        [Parameter(Mandatory = $true)][int64]$RunId,
        [Parameter(Mandatory = $true)][string]$Destination,
        [Parameter(Mandatory = $true)][string]$MacArch
    )
    $resolved = [System.IO.Path]::GetFullPath($Destination)
    New-Item -ItemType Directory -Path $resolved -Force | Out-Null
    $names = if ($MacArch -eq "all") { @("ecorex-macos-arm64", "ecorex-macos-x64") } else { @("ecorex-macos-$MacArch") }
    foreach ($name in $names) {
        $target = Join-Path $resolved $name
        if (Test-Path -LiteralPath $target) {
            Remove-Item -LiteralPath $target -Recurse -Force
        }
        New-Item -ItemType Directory -Path $target -Force | Out-Null
        & gh run download $RunId --repo $Repo --name $name --dir $target
        if ($LASTEXITCODE -ne 0) {
            throw "Failed to download GitHub artifact '$name' from run $RunId."
        }
    }
    return $resolved
}

function Write-ImportHint {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string]$Destination,
        [Parameter(Mandatory = $true)][string]$Version,
        [Parameter(Mandatory = $true)][string]$MacArch
    )
    $armDir = Join-Path $Destination "ecorex-macos-arm64"
    $x64Dir = Join-Path $Destination "ecorex-macos-x64"
    $armDmg = Join-Path $armDir "EcoreX_${Version}_arm64.dmg"
    $armSmoke = Join-Path $armDir "ecorex-macos-arm64-install-smoke.json"
    $x64Dmg = Join-Path $x64Dir "EcoreX_${Version}_x64.dmg"
    $x64Smoke = Join-Path $x64Dir "ecorex-macos-x64-install-smoke.json"
    $parts = @("powershell -ExecutionPolicy Bypass -File scripts\update-ecorex-desktop-release-manifest.ps1 -Version $Version")
    if ($MacArch -eq "all" -or $MacArch -eq "arm64") {
        $parts += "-MacArm64DmgPath `"$armDmg`" -MacArm64InstallSmokePath `"$armSmoke`""
    }
    if ($MacArch -eq "all" -or $MacArch -eq "x64") {
        $parts += "-MacX64DmgPath `"$x64Dmg`" -MacX64InstallSmokePath `"$x64Smoke`""
    }
    [ordered]@{
        repoRoot = $Root
        artifactDir = $Destination
        importCommand = ($parts -join " ")
    } | ConvertTo-Json -Depth 4
}

$root = Resolve-RepoRoot
Set-Location $root
$destination = [System.IO.Path]::GetFullPath((Join-Path $root $OutputDir))

$notarizeValue = if ($Notarize.IsPresent) { "true" } else { "false" }
$dispatchArgs = @(
    "workflow", "run", $Workflow,
    "--repo", $Repo,
    "--ref", $Ref,
    "-f", "mac_arch=$MacArch",
    "-f", "notarize=$notarizeValue"
)

if ($DryRun) {
    [ordered]@{
        dryRun = $true
        repo = $Repo
        workflow = $Workflow
        ref = $Ref
        macArch = $MacArch
        notarize = [bool]$Notarize
        outputDir = $destination
        dispatchCommand = "gh $($dispatchArgs -join ' ')"
    } | ConvertTo-Json -Depth 4
    return
}

Assert-GhReady

if (-not $DownloadOnly) {
    & gh @dispatchArgs
    if ($LASTEXITCODE -ne 0) {
        throw "Workflow dispatch failed for $Workflow on $Ref."
    }
    Start-Sleep -Seconds 8
    $run = Get-LatestWorkflowDispatchRun -Repo $Repo -Workflow $Workflow -Ref $Ref
    $RunId = [int64]$run.databaseId
    Write-Host "Dispatched macOS DMG workflow run ${RunId}: $($run.url)"
    if ($DispatchOnly) {
        [ordered]@{
            ok = $true
            runId = $RunId
            url = $run.url
            status = $run.status
            conclusion = $run.conclusion
        } | ConvertTo-Json -Depth 4
        return
    }
}

if ($RunId -le 0) {
    throw "Pass -RunId when using -DownloadOnly."
}

& gh run watch $RunId --repo $Repo --exit-status
if ($LASTEXITCODE -ne 0) {
    throw "GitHub workflow run $RunId did not complete successfully."
}

$downloadedTo = Download-MacArtifacts -Repo $Repo -RunId $RunId -Destination $destination -MacArch $MacArch
Write-ImportHint -Root $root -Destination $downloadedTo -Version $Version -MacArch $MacArch
