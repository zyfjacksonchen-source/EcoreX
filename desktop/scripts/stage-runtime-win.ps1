param(
    [string]$RepoRoot = "",
    [string]$RuntimeDir = "",
    [string]$PythonHome = "",
    [string]$PythonEmbedVersion = "3.11.9",
    [string]$RuntimeCacheDir = "",
    [string[]]$PreinstallPacks = @(),
    [switch]$UseLocalPython,
    [switch]$SkipDependencyInstall
)

$ErrorActionPreference = "Stop"

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

function Save-Download {
    param(
        [Parameter(Mandatory = $true)][string]$Uri,
        [Parameter(Mandatory = $true)][string]$Destination
    )

    if (Test-Path -LiteralPath $Destination) {
        return
    }
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $Destination) | Out-Null
    Write-Host "Downloading $Uri"
    Invoke-WebRequest -Uri $Uri -OutFile $Destination -UseBasicParsing
}

function Enable-EmbeddedPythonSite {
    param([Parameter(Mandatory = $true)][string]$PythonDir)

    $pth = Get-ChildItem -LiteralPath $PythonDir -Filter "python*._pth" | Select-Object -First 1
    if (-not $pth) {
        return
    }
    $lines = Get-Content -LiteralPath $pth.FullName
    $hasImportSite = $false
    $updated = foreach ($line in $lines) {
        if ($line.Trim() -eq "import site") {
            $hasImportSite = $true
            $line
        }
        elseif ($line.Trim() -eq "#import site") {
            $hasImportSite = $true
            "import site"
        }
        else {
            $line
        }
    }
    if (-not $hasImportSite) {
        $updated += "import site"
    }
    if (-not ($updated | Where-Object { $_.Trim() -eq ".." })) {
        $insertAt = [Math]::Max(0, $updated.Count - 1)
        $before = @()
        $after = @()
        if ($insertAt -gt 0) {
            $before = @($updated[0..($insertAt - 1)])
        }
        if ($insertAt -lt $updated.Count) {
            $after = @($updated[$insertAt..($updated.Count - 1)])
        }
        $updated = $before + ".." + $after
    }
    Set-Content -LiteralPath $pth.FullName -Value $updated -Encoding ASCII
}

function Copy-OptionalLarkCli {
    param([Parameter(Mandatory = $true)][string]$TargetRuntime)

    Write-Host "Skipping bundled lark-cli for Windows runtime; Feishu/Lark connector remains discovery-only and routes installs through the built-in find skill/find-skill gate."
}

function Invoke-ReleaseRuntimeSanitizer {
    param([Parameter(Mandatory = $true)][string]$TargetRuntime)

    $sanitizer = Join-Path $repoRootResolved "scripts\sanitize-ecorex-release-runtime.py"
    if (-not (Test-Path -LiteralPath $sanitizer)) {
        throw "Release sanitizer missing: $sanitizer"
    }
    & python $sanitizer $TargetRuntime
    if ($LASTEXITCODE -ne 0) {
        throw "Release runtime sanitizer failed."
    }
}

$desktopRoot = Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")
if (-not $RepoRoot) {
    $RepoRoot = Resolve-Path -LiteralPath (Join-Path $desktopRoot "..")
}
if (-not $RuntimeDir) {
    $RuntimeDir = Join-Path $desktopRoot "runtime\ecorex-runtime"
}
if (-not $RuntimeCacheDir) {
    $RuntimeCacheDir = Join-Path $desktopRoot ".runtime-cache"
}

$repoRootResolved = Resolve-Path -LiteralPath $RepoRoot
$runtimeResolved = Resolve-UnderDirectory -Path $RuntimeDir -Base $desktopRoot

if (-not $PSBoundParameters.ContainsKey("PreinstallPacks")) {
    if ($null -ne $env:ECOREX_PREINSTALL_PACKS) {
        $PreinstallPacks = @($env:ECOREX_PREINSTALL_PACKS -split "," | ForEach-Object { $_.Trim() } | Where-Object { $_ })
    }
    else {
        $PreinstallPacks = @("office-pdf")
    }
}

if (Test-Path -LiteralPath $runtimeResolved) {
    Remove-Item -LiteralPath $runtimeResolved -Recurse -Force
}
New-Item -ItemType Directory -Force -Path $runtimeResolved | Out-Null

$sourceDirs = @(
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

$sourceFiles = @(
    "app.py",
    "config.py",
    "config-template.json",
    "requirements.txt",
    "requirements-optional.txt",
    "pyproject.toml",
    "LICENSE"
)

foreach ($dir in $sourceDirs) {
    $src = Join-Path $repoRootResolved $dir
    if (Test-Path -LiteralPath $src) {
        Copy-Item -LiteralPath $src -Destination (Join-Path $runtimeResolved $dir) -Recurse -Force
    }
}

foreach ($file in $sourceFiles) {
    $src = Join-Path $repoRootResolved $file
    if (Test-Path -LiteralPath $src) {
        Copy-Item -LiteralPath $src -Destination (Join-Path $runtimeResolved $file) -Force
    }
}

$desktopDist = Join-Path $desktopRoot "dist"
if (Test-Path -LiteralPath (Join-Path $desktopDist "index.html")) {
    $appDir = Join-Path $runtimeResolved "channel/web/static/app"
    if (Test-Path -LiteralPath $appDir) {
        Remove-Item -LiteralPath $appDir -Recurse -Force
    }
    New-Item -ItemType Directory -Force -Path $appDir | Out-Null
    Copy-Item -Path (Join-Path $desktopDist "*") -Destination $appDir -Recurse -Force
}

Copy-OptionalLarkCli -TargetRuntime $runtimeResolved

$runtimePackRoot = Join-Path $desktopRoot "runtime-packs"
Copy-Item -LiteralPath (Join-Path $runtimePackRoot "core-requirements.txt") -Destination (Join-Path $runtimeResolved "core-requirements.txt") -Force
Copy-Item -LiteralPath (Join-Path $runtimePackRoot "capabilities.json") -Destination (Join-Path $runtimeResolved "capabilities.json") -Force

$runtimeScripts = Join-Path $runtimeResolved "scripts"
New-Item -ItemType Directory -Force -Path $runtimeScripts | Out-Null
Copy-Item -LiteralPath (Join-Path $PSScriptRoot "install-capability.py") -Destination (Join-Path $runtimeScripts "install-capability.py") -Force
Copy-Item -LiteralPath (Join-Path $PSScriptRoot "install-capability-win.ps1") -Destination (Join-Path $runtimeScripts "install-capability-win.ps1") -Force

if ($env:ECOREX_DISABLE_ENTERPRISE_POLICY -ne "1") {
    $adminBase = $env:ECOREX_ADMIN_BASE_URL
    if (-not $adminBase) {
        $adminBase = "https://www.ecoreai.cn/ecorex-agent"
    }
    $adminBase = $adminBase.TrimEnd("/")
    $clientEventKey = $env:ECOREX_CLIENT_EVENT_KEY
    if (-not $clientEventKey) {
        $clientEventKey = "ecorex-desktop-v0.1.18"
    }
    $compatClientEventKeys = @(
        $clientEventKey,
        "ecorex-desktop-v0.1.18",
        "ecorex-desktop-v0.1.17",
        "ecorex-desktop-v0.1.16",
        "ecorex-desktop-v0.1.15",
        "ecorex-desktop-v0.1.14",
        "ecorex-desktop-v0.1.13",
        "ecorex-desktop-v0.1.12",
        "ecorex-desktop-v0.1.11",
        "ecorex-desktop-v0.1.10"
    ) | Select-Object -Unique
    $policy = [ordered]@{
        adminEventsUrl = $env:ECOREX_ADMIN_EVENTS_URL
        modelConfigUrl = $env:ECOREX_MODEL_CONFIG_URL
        capabilityPolicyUrl = $env:ECOREX_CAPABILITY_POLICY_URL
        clientEventKey = $clientEventKey
        compatClientEventKeys = $compatClientEventKeys
        userEmail = $env:ECOREX_USER_EMAIL
        deviceId = $env:ECOREX_DEVICE_ID
        orgId = $env:ECOREX_ORG_ID
    }
    if (-not $policy.adminEventsUrl) {
        $policy.adminEventsUrl = "$adminBase/client/events"
    }
    if (-not $policy.modelConfigUrl) {
        $policy.modelConfigUrl = "$adminBase/client/model-config"
    }
    if (-not $policy.capabilityPolicyUrl) {
        $policy.capabilityPolicyUrl = "$adminBase/client/capability-policy"
    }
    $emptyPolicyKeys = @($policy.Keys | Where-Object { [string]::IsNullOrWhiteSpace([string]$policy[$_]) })
    foreach ($key in $emptyPolicyKeys) {
        $policy.Remove($key)
    }
    $policyPath = Join-Path $runtimeResolved "enterprise-policy.json"
    $policyJson = $policy | ConvertTo-Json -Depth 4
    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($policyPath, $policyJson + [Environment]::NewLine, $utf8NoBom)
    Write-Host "Enterprise policy staged for EcoreX desktop."
}

$pythonRuntime = Join-Path $runtimeResolved "python"
if ($PythonHome -or $UseLocalPython) {
    if (-not $PythonHome) {
        $pythonCmd = Get-Command python -ErrorAction Stop
        $PythonHome = Split-Path -Parent $pythonCmd.Source
    }
    $pythonHomeResolved = Resolve-Path -LiteralPath $PythonHome
    $pythonExe = Join-Path $pythonHomeResolved "python.exe"
    if (-not (Test-Path -LiteralPath $pythonExe)) {
        throw "python.exe not found at $pythonExe"
    }
    Copy-Item -LiteralPath $pythonHomeResolved -Destination $pythonRuntime -Recurse -Force
    $pythonDistribution = "local-copy"
}
else {
    New-Item -ItemType Directory -Force -Path $pythonRuntime | Out-Null
    $pythonZip = Join-Path $RuntimeCacheDir "python-$PythonEmbedVersion-embed-amd64.zip"
    $pythonUrl = "https://www.python.org/ftp/python/$PythonEmbedVersion/python-$PythonEmbedVersion-embed-amd64.zip"
    Save-Download -Uri $pythonUrl -Destination $pythonZip
    Expand-Archive -LiteralPath $pythonZip -DestinationPath $pythonRuntime -Force
    Enable-EmbeddedPythonSite -PythonDir $pythonRuntime
    $pythonHomeResolved = $pythonRuntime
    $pythonDistribution = "python-embed-$PythonEmbedVersion"
}

$runtimePython = Join-Path $pythonRuntime "python.exe"
if (-not $SkipDependencyInstall) {
    $previousNoUserSite = $env:PYTHONNOUSERSITE
    $env:PYTHONNOUSERSITE = "1"
    try {
        if ($pythonDistribution -like "python-embed-*") {
            $getPip = Join-Path $RuntimeCacheDir "get-pip.py"
            Save-Download -Uri "https://bootstrap.pypa.io/get-pip.py" -Destination $getPip
            & $runtimePython $getPip --no-warn-script-location
            if ($LASTEXITCODE -ne 0) {
                throw "get-pip failed"
            }
        }
        & $runtimePython -m pip install --upgrade pip --no-cache-dir --no-warn-script-location
        if ($LASTEXITCODE -ne 0) {
            throw "pip upgrade failed"
        }
        & $runtimePython -m pip install --no-cache-dir --no-warn-script-location -r (Join-Path $runtimeResolved "core-requirements.txt")
        if ($LASTEXITCODE -ne 0) {
            throw "core dependency install failed"
        }
    }
    finally {
        $env:PYTHONNOUSERSITE = $previousNoUserSite
    }
}

foreach ($packId in $PreinstallPacks) {
    if (-not $packId) {
        continue
    }
    Write-Host "Preinstalling capability pack $packId"
    & $runtimePython (Join-Path $runtimeScripts "install-capability.py") --pack-id $packId --runtime-dir $runtimeResolved --manifest (Join-Path $runtimeResolved "capabilities.json") --fallback-index-url "https://pypi.tuna.tsinghua.edu.cn/simple"
    if ($LASTEXITCODE -ne 0) {
        throw "Capability pack preinstall failed: $packId"
    }
}

Get-ChildItem -LiteralPath $runtimeResolved -Recurse -Directory -Filter "__pycache__" -ErrorAction SilentlyContinue |
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue

$manifest = [ordered]@{
    product = "EcoreX"
    runtime = "compatible-agent-runtime"
    stagedAt = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
    pythonDistribution = $pythonDistribution
    dependencyInstall = -not $SkipDependencyInstall
    preinstalledPacks = $PreinstallPacks
}
$manifest | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath (Join-Path $runtimeResolved "runtime-manifest.json") -Encoding UTF8

Invoke-ReleaseRuntimeSanitizer -TargetRuntime $runtimeResolved

Write-Host "EcoreX runtime staged at $runtimeResolved"
