param(
    [string]$RepoRoot = "",
    [string]$RuntimeDir = "",
    [string]$PythonHome = "",
    [string]$PythonEmbedVersion = "3.11.9",
    [string]$NodeVersion = "22.22.0",
    [ValidateSet("x64", "ia32")][string]$WinArch = "x64",
    [string]$RuntimeCacheDir = "",
    [string[]]$PreinstallPacks = @(),
    [switch]$UseLocalPython,
    [switch]$SkipDependencyInstall,
    [switch]$SkipNodeInstall,
    [switch]$SkipPlaywrightBrowserInstall
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

function Install-NodeRuntime {
    param(
        [Parameter(Mandatory = $true)][string]$TargetRuntime,
        [Parameter(Mandatory = $true)][string]$Version,
        [Parameter(Mandatory = $true)][string]$CacheDir,
        [Parameter(Mandatory = $true)][string]$Architecture
    )

    $nodeArch = if ($Architecture -eq "ia32") { "x86" } else { "x64" }
    $archiveName = "node-v$Version-win-$nodeArch.zip"
    $archivePath = Join-Path $CacheDir $archiveName
    $archiveUrl = "https://nodejs.org/dist/v$Version/$archiveName"
    Save-Download -Uri $archiveUrl -Destination $archivePath

    $extractRoot = Join-Path $CacheDir "node-v$Version-win-$nodeArch-extract"
    if (Test-Path -LiteralPath $extractRoot) {
        Remove-Item -LiteralPath $extractRoot -Recurse -Force
    }
    New-Item -ItemType Directory -Force -Path $extractRoot | Out-Null
    Expand-Archive -LiteralPath $archivePath -DestinationPath $extractRoot -Force

    $expanded = Join-Path $extractRoot "node-v$Version-win-$nodeArch"
    if (-not (Test-Path -LiteralPath (Join-Path $expanded "node.exe"))) {
        throw "Node archive did not contain node.exe at $expanded"
    }

    $targetNode = Join-Path $TargetRuntime "node"
    if (Test-Path -LiteralPath $targetNode) {
        Remove-Item -LiteralPath $targetNode -Recurse -Force
    }
    New-Item -ItemType Directory -Force -Path $targetNode | Out-Null
    Copy-Item -Path (Join-Path $expanded "*") -Destination $targetNode -Recurse -Force

    foreach ($required in @("node.exe", "npm.cmd", "npx.cmd")) {
        if (-not (Test-Path -LiteralPath (Join-Path $targetNode $required))) {
            throw "Staged Node runtime missing $required"
        }
    }
    Write-Host "Node runtime staged at $targetNode"
}

function Copy-OptionalLarkCli {
    param([Parameter(Mandatory = $true)][string]$TargetRuntime)

    Write-Host "Skipping bundled lark-cli for Windows runtime; Feishu/Lark connector installs on demand through the structured find-skill gated tool."
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
$pythonEmbedArch = if ($WinArch -eq "ia32") { "win32" } else { "amd64" }
$skippedCoreRequirements = @()

if (-not $PSBoundParameters.ContainsKey("PreinstallPacks")) {
    if ($null -ne $env:ECOREX_PREINSTALL_PACKS) {
        $PreinstallPacks = @($env:ECOREX_PREINSTALL_PACKS -split "," | ForEach-Object { $_.Trim() } | Where-Object { $_ })
    }
    else {
        $PreinstallPacks = if ($WinArch -eq "ia32") { @() } else { @("office-pdf") }
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

$tongxinSource = Join-Path $repoRootResolved "tools\tongxin"
if (Test-Path -LiteralPath (Join-Path $tongxinSource "xin_agent_cli.py")) {
    $tongxinTarget = Join-Path $runtimeResolved "tools\tongxin"
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $tongxinTarget) | Out-Null
    Copy-Item -LiteralPath $tongxinSource -Destination $tongxinTarget -Recurse -Force
    Get-ChildItem -LiteralPath $tongxinTarget -Recurse -Directory -Filter "__pycache__" -ErrorAction SilentlyContinue |
        Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
}

$runtimePackRoot = Join-Path $repoRootResolved "runtime-packs"
$coreRequirementsPath = Join-Path $runtimeResolved "core-requirements.txt"
Copy-Item -LiteralPath (Join-Path $runtimePackRoot "core-requirements.txt") -Destination $coreRequirementsPath -Force
if ($WinArch -eq "ia32") {
    $unsupportedCorePackages = @("playwright")
    $filteredRequirements = @()
    foreach ($line in Get-Content -LiteralPath $coreRequirementsPath -Encoding UTF8) {
        $trimmed = $line.Trim()
        $packageName = (($trimmed -split "[<>=; \t]") | Select-Object -First 1).ToLowerInvariant()
        if ($packageName -and $unsupportedCorePackages -contains $packageName) {
            $skippedCoreRequirements += $trimmed
            continue
        }
        $filteredRequirements += $line
    }
    if ($skippedCoreRequirements.Count -gt 0) {
        Write-Host "Skipping Win32-incompatible core requirement(s): $($skippedCoreRequirements -join ', ')"
        $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
        [System.IO.File]::WriteAllText($coreRequirementsPath, (($filteredRequirements -join "`n") + "`n"), $utf8NoBom)
    }
}
Copy-Item -LiteralPath (Join-Path $runtimePackRoot "capabilities.json") -Destination (Join-Path $runtimeResolved "capabilities.json") -Force

$runtimeScripts = Join-Path $runtimeResolved "scripts"
New-Item -ItemType Directory -Force -Path $runtimeScripts | Out-Null
Copy-Item -LiteralPath (Join-Path $repoRootResolved "scripts\install-capability.py") -Destination (Join-Path $runtimeScripts "install-capability.py") -Force
Copy-Item -LiteralPath (Join-Path $PSScriptRoot "install-capability-win.ps1") -Destination (Join-Path $runtimeScripts "install-capability-win.ps1") -Force

if ($env:ECOREX_DISABLE_ENTERPRISE_POLICY -ne "1") {
    $adminBase = $env:ECOREX_ADMIN_BASE_URL
    if (-not $adminBase) {
        $adminBase = "https://www.ecoreai.cn/ecorex-agent"
    }
    $adminBase = $adminBase.TrimEnd("/")
    $clientEventKey = $env:ECOREX_CLIENT_EVENT_KEY
    if (-not $clientEventKey) {
        $clientEventKey = "ecorex-desktop-v0.2.0"
    }
    $compatClientEventKeys = @(
        $clientEventKey,
        "ecorex-desktop-v0.2.0",
        "ecorex-desktop-v0.1.19",
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
    $pythonZip = Join-Path $RuntimeCacheDir "python-$PythonEmbedVersion-embed-$pythonEmbedArch.zip"
    $pythonUrl = "https://www.python.org/ftp/python/$PythonEmbedVersion/python-$PythonEmbedVersion-embed-$pythonEmbedArch.zip"
    Save-Download -Uri $pythonUrl -Destination $pythonZip
    Expand-Archive -LiteralPath $pythonZip -DestinationPath $pythonRuntime -Force
    Enable-EmbeddedPythonSite -PythonDir $pythonRuntime
    $pythonHomeResolved = $pythonRuntime
    $pythonDistribution = "python-embed-$PythonEmbedVersion-$pythonEmbedArch"
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

if (-not $SkipNodeInstall) {
    Install-NodeRuntime -TargetRuntime $runtimeResolved -Version $NodeVersion -CacheDir $RuntimeCacheDir -Architecture $WinArch
}

if ((-not $SkipPlaywrightBrowserInstall) -and ($WinArch -ne "ia32")) {
    $previousBrowsersPath = $env:PLAYWRIGHT_BROWSERS_PATH
    $env:PLAYWRIGHT_BROWSERS_PATH = Join-Path $runtimeResolved "playwright-browsers"
    try {
        & $runtimePython -m playwright install chromium
        if ($LASTEXITCODE -ne 0) {
            throw "Playwright Chromium install failed"
        }
    }
    finally {
        $env:PLAYWRIGHT_BROWSERS_PATH = $previousBrowsersPath
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
    winArch = $WinArch
    pythonDistribution = $pythonDistribution
    nodeVersion = if ($SkipNodeInstall) { $null } else { $NodeVersion }
    dependencyInstall = -not $SkipDependencyInstall
    playwrightBrowserInstall = (-not $SkipPlaywrightBrowserInstall) -and ($WinArch -ne "ia32")
    preinstalledPacks = $PreinstallPacks
    skippedCoreRequirements = $skippedCoreRequirements
    runtimeLimitations = if ($WinArch -eq "ia32" -and $skippedCoreRequirements.Count -gt 0) { @("playwright-unavailable-win32") } else { @() }
}
$manifest | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath (Join-Path $runtimeResolved "runtime-manifest.json") -Encoding UTF8

Invoke-ReleaseRuntimeSanitizer -TargetRuntime $runtimeResolved

Write-Host "EcoreX runtime staged at $runtimeResolved"
