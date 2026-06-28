param(
    [string]$Version = "0.2.4",
    [string]$ManifestPath = "deploy/ecorex-site/manifest.json",
    [string]$WindowsInstallerPath = "",
    [string]$WindowsInstalledSmokePath = "",
    [string]$WindowsIa32InstallerPath = "",
    [string]$WindowsIa32InstalledSmokePath = "",
    [string]$MacArm64DmgPath = "",
    [string]$MacArm64InstallSmokePath = "",
    [string]$MacX64DmgPath = "",
    [string]$MacX64InstallSmokePath = "",
    [string]$WebUiWindowsPath = "",
    [string]$WebUiMacosPath = "",
    [string]$WebLinuxServicePath = "",
    [string]$UpdatedAt = (Get-Date -Format "yyyy-MM-dd"),
    [switch]$PromoteVersion,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"

$RequiredAuthNegativeStatusKeys = @(
    "messageNoToken",
    "messageWrongToken",
    "messageQueryTokenRejected",
    "streamNoToken",
    "streamWrongToken",
    "streamQueryTokenRejected",
    "fileStatNoToken",
    "fileStatWrongToken",
    "fileServeNoToken",
    "fileServeWrongToken",
    "openPathNoToken",
    "openPathWrongToken"
)

function Resolve-RequiredPath {
    param([Parameter(Mandatory = $true)][string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) {
        throw "Required path does not exist: $Path"
    }
    return (Resolve-Path -LiteralPath $Path).Path
}

function Read-JsonFile {
    param([Parameter(Mandatory = $true)][string]$Path)
    $resolved = Resolve-RequiredPath $Path
    $bytes = [System.IO.File]::ReadAllBytes($resolved)
    if ($bytes.Length -ge 3 -and $bytes[0] -eq 0xEF -and $bytes[1] -eq 0xBB -and $bytes[2] -eq 0xBF) {
        throw "JSON file must be UTF-8 without BOM: $resolved"
    }
    return [System.Text.Encoding]::UTF8.GetString($bytes) | ConvertFrom-Json
}

function Write-Utf8NoBom {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Value
    )
    [System.IO.File]::WriteAllText($Path, $Value, [System.Text.UTF8Encoding]::new($false))
}

function Get-EcoreXFileSha256 {
    param([Parameter(Mandatory = $true)][string]$Path)
    $resolved = Resolve-RequiredPath $Path
    if (Get-Command Get-FileHash -ErrorAction SilentlyContinue) {
        return (Get-FileHash -Algorithm SHA256 -LiteralPath $resolved).Hash.ToUpperInvariant()
    }
    $stream = [System.IO.File]::OpenRead($resolved)
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

function Get-Artifact {
    param(
        [Parameter(Mandatory = $true)]$Manifest,
        [Parameter(Mandatory = $true)][string]$Id
    )
    $artifact = @($Manifest.artifacts | Where-Object { [string]$_.id -eq $Id }) | Select-Object -First 1
    if (-not $artifact) {
        throw "Artifact '$Id' was not found in manifest."
    }
    return $artifact
}

function Get-FileMetadata {
    param([Parameter(Mandatory = $true)][string]$Path)
    $resolved = Resolve-RequiredPath $Path
    $item = Get-Item -LiteralPath $resolved
    return [pscustomobject]@{
        Path = $resolved
        FileName = $item.Name
        Size = [int64]$item.Length
        Sha256 = Get-EcoreXFileSha256 -Path $resolved
    }
}

function Assert-RequiredTrue {
    param(
        [Parameter(Mandatory = $true)]$Payload,
        [Parameter(Mandatory = $true)][string[]]$Keys,
        [Parameter(Mandatory = $true)][string]$Label
    )
    $missing = @($Keys | Where-Object { -not [bool]$Payload.$_ })
    if ($missing.Count -gt 0) {
        throw "$Label did not pass required flags: $($missing -join ', ')"
    }
}

function Get-SmokeFirstNonEmpty {
    param(
        [Parameter(Mandatory = $true)]$Payload,
        [Parameter(Mandatory = $true)][string[]]$Names
    )
    foreach ($name in $Names) {
        if ($Payload.PSObject.Properties.Name -contains $name) {
            $value = [string]$Payload.PSObject.Properties[$name].Value
            if ($value.Trim()) {
                return $value
            }
        }
    }
    return ""
}

function Assert-WindowsSmoke {
    param(
        [Parameter(Mandatory = $true)]$Smoke,
        [Parameter(Mandatory = $true)][string]$Version,
        [Parameter(Mandatory = $true)][string]$ArtifactId,
        [string]$Label = "Windows installed smoke"
    )
    Assert-RequiredTrue -Payload $Smoke -Keys @(
        "installed",
        "appStarted",
        "rendererReady",
        "sidecarReady",
        "authReady",
        "authRequired",
        "authNegativeReady",
        "cleaned"
    ) -Label $Label
    foreach ($name in @("installerSignatureStatus", "appSignatureStatus", "runtimePythonSignatureStatus")) {
        if ([string]$Smoke.$name -ne "Valid") {
            throw "$Label requires $name=Valid; got '$($Smoke.$name)'."
        }
    }
    if ([string]$Smoke.runtimeVersion -ne $Version) {
        throw "$Label runtimeVersion '$($Smoke.runtimeVersion)' does not match '$Version'."
    }
    $expectedWinArch = if ($ArtifactId -eq "windows-ia32") { "ia32" } else { "x64" }
    $expectedPythonBits = if ($ArtifactId -eq "windows-ia32") { 32 } else { 64 }
    if ([string]$Smoke.runtimeWinArch -ne $expectedWinArch) {
        throw "$Label runtimeWinArch '$($Smoke.runtimeWinArch)' does not match '$expectedWinArch'."
    }
    if ([int]$Smoke.runtimePythonBits -ne $expectedPythonBits) {
        throw "$Label runtimePythonBits '$($Smoke.runtimePythonBits)' does not match '$expectedPythonBits'."
    }
    if ([int]$Smoke.rendererRootHtmlLength -le 0) {
        throw "$Label rendererRootHtmlLength must be positive; got '$($Smoke.rendererRootHtmlLength)'."
    }
    if ([int]$Smoke.rendererBodyTextLength -le 0) {
        throw "$Label rendererBodyTextLength must be positive; got '$($Smoke.rendererBodyTextLength)'."
    }
    $negative = $Smoke.authNegativeStatuses
    if (-not $negative) {
        throw "$Label is missing authNegativeStatuses."
    }
    $negativeNames = @($negative.PSObject.Properties.Name)
    $missingNegative = @($RequiredAuthNegativeStatusKeys | Where-Object { $_ -notin $negativeNames })
    if ($missingNegative.Count -gt 0) {
        throw "$Label is missing authNegativeStatuses keys: $($missingNegative -join ', ')."
    }
    foreach ($property in $negative.PSObject.Properties) {
        if ([int]$property.Value -ne 401) {
            throw "$Label negative auth '$($property.Name)' returned $($property.Value), expected 401."
        }
    }
}

function Assert-MacInstallSmoke {
    param(
        [Parameter(Mandatory = $true)]$Smoke,
        [Parameter(Mandatory = $true)][string]$Version,
        [Parameter(Mandatory = $true)][string]$Sha256,
        [Parameter(Mandatory = $true)][string]$Arch,
        [Parameter(Mandatory = $true)][string]$FileName,
        [Parameter(Mandatory = $true)][int64]$Size
    )
    if ([string]$Smoke.status -ne "pass") {
        throw "macOS install smoke status must be pass; got '$($Smoke.status)'."
    }
    if ([string]$Smoke.version -ne $Version) {
        throw "macOS install smoke version '$($Smoke.version)' does not match '$Version'."
    }
    if (([string]$Smoke.sha256).ToUpperInvariant() -ne $Sha256.ToUpperInvariant()) {
        throw "macOS install smoke sha256 '$($Smoke.sha256)' does not match '$Sha256'."
    }
    if ([string]$Smoke.arch -ne $Arch) {
        throw "macOS install smoke arch '$($Smoke.arch)' does not match '$Arch'."
    }
    if ([string]$Smoke.artifact -ne $FileName) {
        throw "macOS install smoke artifact '$($Smoke.artifact)' does not match '$FileName'."
    }
    if ([int64]$Smoke.bytes -ne $Size) {
        throw "macOS install smoke bytes '$($Smoke.bytes)' does not match '$Size'."
    }
    $requiredTrue = @(
        "mounted",
        "appFound",
        "copied",
        "launched",
        "versionOk",
        "sidecarReady",
        "authReady",
        "authRequired",
        "authNegativeReady",
        "gatekeeperInstructionShown"
    )
    $missing = @($requiredTrue | Where-Object { -not [bool]$Smoke.$_ })
    if ($missing.Count -gt 0) {
        throw "macOS install smoke did not pass required flags: $($missing -join ', ')"
    }
    $instructions = Get-SmokeFirstNonEmpty -Payload $Smoke -Names @("gatekeeperInstructions", "instructions", "instructionsUrl", "instructions_url")
    if (-not $instructions) {
        throw "macOS install smoke requires gatekeeperInstructions, instructions, or instructionsUrl."
    }
    $evidence = Get-SmokeFirstNonEmpty -Payload $Smoke -Names @("runId", "run_id", "evidenceUrl", "evidence_url", "evidence")
    if (-not $evidence) {
        throw "macOS install smoke requires runId, evidenceUrl, or evidence."
    }
    $negative = $Smoke.authNegativeStatuses
    if (-not $negative) {
        throw "macOS install smoke is missing authNegativeStatuses."
    }
    $negativeNames = @($negative.PSObject.Properties.Name)
    $missingNegative = @($RequiredAuthNegativeStatusKeys | Where-Object { $_ -notin $negativeNames })
    if ($missingNegative.Count -gt 0) {
        throw "macOS install smoke is missing authNegativeStatuses keys: $($missingNegative -join ', ')."
    }
    foreach ($property in $negative.PSObject.Properties) {
        if ([int]$property.Value -ne 401) {
            throw "macOS install smoke negative auth '$($property.Name)' returned $($property.Value), expected 401."
        }
    }
    return [string]$evidence
}

function Set-ArtifactProperty {
    param(
        [Parameter(Mandatory = $true)]$Artifact,
        [Parameter(Mandatory = $true)][string]$Name,
        $Value
    )
    if ($Artifact.PSObject.Properties.Name -contains $Name) {
        $Artifact.$Name = $Value
    }
    else {
        $Artifact | Add-Member -NotePropertyName $Name -NotePropertyValue $Value
    }
}

function Update-CommonMetadata {
    param(
        [Parameter(Mandatory = $true)]$Artifact,
        [Parameter(Mandatory = $true)]$Metadata,
        [Parameter(Mandatory = $true)][string]$Status,
        [Parameter(Mandatory = $true)][string]$Signature,
        [Parameter(Mandatory = $true)][string]$Source
    )
    Set-ArtifactProperty $Artifact "version" $Version
    Set-ArtifactProperty $Artifact "fileName" $Metadata.FileName
    Set-ArtifactProperty $Artifact "href" "downloads/$($Metadata.FileName)"
    Set-ArtifactProperty $Artifact "size" $Metadata.Size
    Set-ArtifactProperty $Artifact "sha256" $Metadata.Sha256
    Set-ArtifactProperty $Artifact "status" $Status
    Set-ArtifactProperty $Artifact "signature" $Signature
    Set-ArtifactProperty $Artifact "updatedAt" $UpdatedAt
    Set-ArtifactProperty $Artifact "source" $Source
}

function Update-WindowsArtifact {
    param(
        [Parameter(Mandatory = $true)]$Manifest,
        [Parameter(Mandatory = $true)][string]$ArtifactId,
        [Parameter(Mandatory = $true)][string]$InstallerPath,
        [Parameter(Mandatory = $true)][string]$SmokePath
    )
    $metadata = Get-FileMetadata $InstallerPath
    $expectedFileName = if ($ArtifactId -eq "windows-ia32") { "EcoreX_${Version}_ia32-setup.exe" } else { "EcoreX_${Version}_x64-setup.exe" }
    if ($metadata.FileName -ne $expectedFileName) {
        throw "Windows artifact '$ArtifactId' file '$($metadata.FileName)' does not match expected '$expectedFileName'."
    }
    $signature = Get-AuthenticodeSignature -LiteralPath $metadata.Path
    if ($signature.Status -ne "Valid") {
        throw "Windows installer is not Authenticode Valid: $($signature.Status) $($signature.StatusMessage)"
    }
    $smoke = Read-JsonFile $SmokePath
    Assert-WindowsSmoke -Smoke $smoke -Version $Version -ArtifactId $ArtifactId -Label "$ArtifactId installed smoke"
    if ([string]$smoke.installer -and ([System.IO.Path]::GetFileName([string]$smoke.installer) -ne $metadata.FileName)) {
        throw "Windows installed smoke tested '$($smoke.installer)', not '$($metadata.FileName)'."
    }
    if ([string]$smoke.installerFileName -and [string]$smoke.installerFileName -ne $metadata.FileName) {
        throw "Windows installed smoke fileName '$($smoke.installerFileName)' does not match '$($metadata.FileName)'."
    }
    if (-not ([string]$smoke.installerSha256)) {
        throw "Windows installed smoke is missing installerSha256."
    }
    if (([string]$smoke.installerSha256).ToUpperInvariant() -ne $metadata.Sha256) {
        throw "Windows installed smoke installerSha256 '$($smoke.installerSha256)' does not match '$($metadata.Sha256)'."
    }
    if ([int64]$smoke.installerSize -ne $metadata.Size) {
        throw "Windows installed smoke installerSize '$($smoke.installerSize)' does not match '$($metadata.Size)'."
    }
    $artifact = Get-Artifact -Manifest $Manifest -Id $ArtifactId
    Update-CommonMetadata -Artifact $artifact -Metadata $metadata -Status "ready" -Signature "Valid" -Source "Signed v$Version $ArtifactId installer verified by installed smoke evidence."
}

function Update-ReadyFileArtifact {
    param(
        [Parameter(Mandatory = $true)]$Manifest,
        [Parameter(Mandatory = $true)][string]$ArtifactId,
        [Parameter(Mandatory = $true)][string]$ArtifactPath,
        [Parameter(Mandatory = $true)][string]$ExpectedFileName,
        [Parameter(Mandatory = $true)][string]$Source
    )
    $metadata = Get-FileMetadata $ArtifactPath
    if ($metadata.FileName -ne $ExpectedFileName) {
        throw "Artifact '$ArtifactId' file '$($metadata.FileName)' does not match expected '$ExpectedFileName'."
    }
    $artifact = Get-Artifact -Manifest $Manifest -Id $ArtifactId
    Set-ArtifactProperty $artifact "version" $Version
    Set-ArtifactProperty $artifact "fileName" $metadata.FileName
    Set-ArtifactProperty $artifact "href" "downloads/$($metadata.FileName)"
    Set-ArtifactProperty $artifact "size" $metadata.Size
    Set-ArtifactProperty $artifact "sha256" $metadata.Sha256
    Set-ArtifactProperty $artifact "status" "ready"
    Set-ArtifactProperty $artifact "updatedAt" $UpdatedAt
    Set-ArtifactProperty $artifact "source" $Source
}

function Update-MacArtifact {
    param(
        [Parameter(Mandatory = $true)]$Manifest,
        [Parameter(Mandatory = $true)][string]$ArtifactId,
        [Parameter(Mandatory = $true)][string]$DmgPath,
        [Parameter(Mandatory = $true)][string]$SmokePath
    )
    $metadata = Get-FileMetadata $DmgPath
    $smoke = Read-JsonFile $SmokePath
    $expectedArch = if ($ArtifactId -eq "macos-arm64-dmg") { "arm64" } elseif ($ArtifactId -eq "macos-x64-dmg") { "x64" } else { throw "Unsupported macOS artifact id: $ArtifactId" }
    $expectedFileName = "EcoreX_${Version}_${expectedArch}.dmg"
    if ($metadata.FileName -ne $expectedFileName) {
        throw "macOS artifact '$ArtifactId' file '$($metadata.FileName)' does not match expected '$expectedFileName'."
    }
    $evidence = Assert-MacInstallSmoke -Smoke $smoke -Version $Version -Sha256 $metadata.Sha256 -Arch $expectedArch -FileName $metadata.FileName -Size $metadata.Size
    $gatekeeperInstructions = Get-SmokeFirstNonEmpty -Payload $smoke -Names @("gatekeeperInstructions", "instructions", "instructionsUrl", "instructions_url")
    $smokeSignature = Get-SmokeFirstNonEmpty -Payload $smoke -Names @("signature")
    if (-not $smokeSignature) {
        $smokeSignature = "unsigned"
    }
    $artifact = Get-Artifact -Manifest $Manifest -Id $ArtifactId
    Update-CommonMetadata -Artifact $artifact -Metadata $metadata -Status "ready-unsigned" -Signature "unsigned" -Source "Unsigned v$Version macOS DMG verified by macOS install-smoke evidence."
    Set-ArtifactProperty $artifact "installSmoke" ([ordered]@{
        status = "pass"
        version = $Version
        sha256 = $metadata.Sha256
        artifact = $metadata.FileName
        arch = $expectedArch
        bytes = $metadata.Size
        runId = Get-SmokeFirstNonEmpty -Payload $smoke -Names @("runId", "run_id")
        evidenceUrl = Get-SmokeFirstNonEmpty -Payload $smoke -Names @("evidenceUrl", "evidence_url")
        evidence = $evidence
        signature = $smokeSignature
        gatekeeper = if ($smoke.PSObject.Properties.Name -contains "gatekeeper") { [string]$smoke.gatekeeper } else { "unsigned-user-approved" }
        gatekeeperInstructions = $gatekeeperInstructions
        instructions = $gatekeeperInstructions
        mounted = [bool]$smoke.mounted
        appFound = [bool]$smoke.appFound
        copied = [bool]$smoke.copied
        launched = [bool]$smoke.launched
        versionOk = [bool]$smoke.versionOk
        sidecarReady = [bool]$smoke.sidecarReady
        authReady = [bool]$smoke.authReady
        authRequired = [bool]$smoke.authRequired
        authNegativeReady = [bool]$smoke.authNegativeReady
        authNegativeStatuses = $smoke.authNegativeStatuses
        gatekeeperInstructionShown = [bool]$smoke.gatekeeperInstructionShown
    })
}

$manifestResolved = Resolve-RequiredPath $ManifestPath
$manifest = Read-JsonFile $manifestResolved
if ([string]$manifest.version -ne $Version) {
    if (-not $PromoteVersion) {
        throw "Manifest version '$($manifest.version)' does not match '$Version'. Pass -PromoteVersion to intentionally advance the public manifest."
    }
    Set-ArtifactProperty $manifest "version" $Version
    Set-ArtifactProperty $manifest "notes" "EcoreX v$Version WebUI-first release. Desktop updater is retired; Windows and macOS use manifest-verified WebUI packages for install/update."
}
Set-ArtifactProperty $manifest "updatedAt" $UpdatedAt

if ($WindowsInstallerPath -or $WindowsInstalledSmokePath) {
    if (-not $WindowsInstallerPath -or -not $WindowsInstalledSmokePath) {
        throw "Pass both -WindowsInstallerPath and -WindowsInstalledSmokePath."
    }
    Update-WindowsArtifact -Manifest $manifest -ArtifactId "windows-x64" -InstallerPath $WindowsInstallerPath -SmokePath $WindowsInstalledSmokePath
}

if ($WindowsIa32InstallerPath -or $WindowsIa32InstalledSmokePath) {
    if (-not $WindowsIa32InstallerPath -or -not $WindowsIa32InstalledSmokePath) {
        throw "Pass both -WindowsIa32InstallerPath and -WindowsIa32InstalledSmokePath."
    }
    Update-WindowsArtifact -Manifest $manifest -ArtifactId "windows-ia32" -InstallerPath $WindowsIa32InstallerPath -SmokePath $WindowsIa32InstalledSmokePath
}

if ($MacArm64DmgPath -or $MacArm64InstallSmokePath) {
    if (-not $MacArm64DmgPath -or -not $MacArm64InstallSmokePath) {
        throw "Pass both -MacArm64DmgPath and -MacArm64InstallSmokePath."
    }
    Update-MacArtifact -Manifest $manifest -ArtifactId "macos-arm64-dmg" -DmgPath $MacArm64DmgPath -SmokePath $MacArm64InstallSmokePath
}

if ($MacX64DmgPath -or $MacX64InstallSmokePath) {
    if (-not $MacX64DmgPath -or -not $MacX64InstallSmokePath) {
        throw "Pass both -MacX64DmgPath and -MacX64InstallSmokePath."
    }
    Update-MacArtifact -Manifest $manifest -ArtifactId "macos-x64-dmg" -DmgPath $MacX64DmgPath -SmokePath $MacX64InstallSmokePath
}

if ($WebUiWindowsPath) {
    Update-ReadyFileArtifact `
        -Manifest $manifest `
        -ArtifactId "webui-windows-x64" `
        -ArtifactPath $WebUiWindowsPath `
        -ExpectedFileName "EcoreX_${Version}-webui-windows-x64.zip" `
        -Source "Local v$Version WebUI Windows artifact built by prepare-ecorex-webui-local-release.ps1 and validated with release artifact checks."
}

if ($WebUiMacosPath) {
    Update-ReadyFileArtifact `
        -Manifest $manifest `
        -ArtifactId "webui-macos-universal" `
        -ArtifactPath $WebUiMacosPath `
        -ExpectedFileName "EcoreX_${Version}-webui-macos-universal.zip" `
        -Source "Local v$Version WebUI macOS artifact built by prepare-ecorex-webui-local-release.ps1 and validated with release artifact checks."
}

if ($WebLinuxServicePath) {
    Update-ReadyFileArtifact `
        -Manifest $manifest `
        -ArtifactId "web-linux-service" `
        -ArtifactPath $WebLinuxServicePath `
        -ExpectedFileName "EcoreX_${Version}-web-linux-service.tar.gz" `
        -Source "Local v$Version Web service tarball built by prepare-ecorex-web-release.ps1 and validated by release artifact checks."
}

$json = $manifest | ConvertTo-Json -Depth 12
if (-not $DryRun) {
    Write-Utf8NoBom -Path $manifestResolved -Value ($json + [Environment]::NewLine)
}

[ordered]@{
    ok = $true
    dryRun = [bool]$DryRun
    manifest = $manifestResolved
    version = $Version
    updatedAt = $UpdatedAt
} | ConvertTo-Json -Depth 4
