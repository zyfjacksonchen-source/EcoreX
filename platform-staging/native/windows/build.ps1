param(
  [Parameter(Mandatory = $true)][string]$OutputDirectory,
  [string]$SourceDirectory = '',
  [string]$ToolchainManifest = '',
  [string]$ExpectedToolchainManifestSha256 = '',
  [string]$ExpectedSourceSetSha256 = '',
  [switch]$GitHubHostedCompatibility
)

$ErrorActionPreference = 'Stop'
$compatibilityMode = $GitHubHostedCompatibility.IsPresent
if ($compatibilityMode -and
    ($env:GITHUB_ACTIONS -cne 'true' -or
     $env:RUNNER_OS -cne 'Windows' -or
     $env:ImageOS -cne 'win22' -or
     $env:GITHUB_EVENT_NAME -notin @('pull_request', 'push', 'workflow_dispatch'))) {
  throw 'github_hosted_compatibility_boundary_invalid'
}
$output = [System.IO.Path]::GetFullPath($OutputDirectory)
New-Item -ItemType Directory -Force -Path $output | Out-Null
$outputItem = Get-Item -LiteralPath $output -Force -ErrorAction Stop
if (-not $outputItem.PSIsContainer -or
    (($outputItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0)) {
  throw 'native_output_directory_invalid'
}
$script:publishedTargets = @(
  (Join-Path $output 'native-build-receipt.json'),
  (Join-Path $output 'ecorex.exe'),
  (Join-Path $output 'ecorex-sandbox-host.exe'),
  (Join-Path $output 'msvcp140.dll')
)
foreach ($target in $script:publishedTargets) {
  if ([System.IO.Path]::GetDirectoryName([System.IO.Path]::GetFullPath($target)) -cne $output) {
    throw 'native_output_target_escaped'
  }
}
function script:Remove-PublishedNativeOutputs {
  foreach ($target in $script:publishedTargets) {
    try {
      if ([System.IO.File]::Exists($target)) {
        [System.IO.File]::Delete($target)
      }
    }
    catch { }
  }
}
Remove-PublishedNativeOutputs
trap {
  foreach ($target in $script:publishedTargets) {
    try {
      if ([System.IO.File]::Exists($target)) {
        [System.IO.File]::Delete($target)
      }
    }
    catch { }
  }
  throw $_
}

$injectionVariables = @(
  'CL', '_CL_', 'LINK', '_LINK_', 'LIB', 'LIBPATH', 'INCLUDE',
  'CL_MPCount', 'UseEnv', 'LINK_REPRO', 'LINK_FULLPATHRSP'
)
foreach ($name in $injectionVariables) {
  $value = [Environment]::GetEnvironmentVariable($name, 'Process')
  if (-not [string]::IsNullOrEmpty($value)) {
    throw ('native_toolchain_environment_injection:' + $name)
  }
}
if (-not [Environment]::Is64BitProcess) {
  throw 'native_toolchain_requires_x64_process'
}
$securityModule = Join-Path $PSHOME 'Modules\Microsoft.PowerShell.Security\Microsoft.PowerShell.Security.psd1'
$env:PSModulePath = Join-Path $PSHOME 'Modules'
Import-Module -Name $securityModule -Force -ErrorAction Stop

$scriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
if ([string]::IsNullOrWhiteSpace($SourceDirectory)) {
  $SourceDirectory = $scriptRoot
}
$sourceRoot = [System.IO.Path]::GetFullPath($SourceDirectory)
if ([string]::IsNullOrWhiteSpace($ToolchainManifest)) {
  $ToolchainManifest = Join-Path $sourceRoot 'toolchain-manifest.json'
}
if ($ExpectedToolchainManifestSha256 -notmatch '^[0-9a-f]{64}$' -or
    $ExpectedSourceSetSha256 -notmatch '^[0-9a-f]{64}$') {
  throw 'caller_pinned_native_authority_required'
}
$receiptPath = Join-Path $output 'native-build-receipt.json'
$script:fileLeases = [System.Collections.Generic.List[System.IO.FileStream]]::new()

function Get-Sha256Hex([string]$Path) {
  $stream = [System.IO.File]::OpenRead([System.IO.Path]::GetFullPath($Path))
  $sha = [System.Security.Cryptography.SHA256]::Create()
  try {
    return (($sha.ComputeHash($stream) | ForEach-Object { $_.ToString('x2') }) -join '')
  }
  finally {
    $sha.Dispose()
    $stream.Dispose()
  }
}

function Get-TextSha256([string]$Value) {
  $sha = [System.Security.Cryptography.SHA256]::Create()
  try {
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($Value)
    return (($sha.ComputeHash($bytes) | ForEach-Object { $_.ToString('x2') }) -join '')
  }
  finally { $sha.Dispose() }
}

function Get-BytesSha256([byte[]]$Value) {
  $sha = [System.Security.Cryptography.SHA256]::Create()
  try {
    return (($sha.ComputeHash($Value) | ForEach-Object { $_.ToString('x2') }) -join '')
  }
  finally { $sha.Dispose() }
}

function Assert-ExactProperties($Value, [string[]]$Expected, [string]$Label) {
  if ($null -eq $Value) { throw ($Label + '_missing') }
  $actual = @($Value.PSObject.Properties.Name | Sort-Object)
  $wanted = @($Expected | Sort-Object)
  if ($actual.Count -ne $wanted.Count -or @(Compare-Object $actual $wanted).Count -ne 0) {
    throw ($Label + '_fields_invalid')
  }
}

function Assert-RealRegularFile([string]$Path, [string]$Label) {
  $item = Get-Item -LiteralPath $Path -Force -ErrorAction Stop
  if ($item.PSIsContainer -or
      (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) -or
      $item.Length -le 0) {
    throw ($Label + '_path_invalid')
  }
  return $item.FullName
}

function Assert-RealDirectory([string]$Path, [string]$Label) {
  $item = Get-Item -LiteralPath $Path -Force -ErrorAction Stop
  if (-not $item.PSIsContainer -or
      (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0)) {
    throw ($Label + '_path_invalid')
  }
  return $item.FullName.TrimEnd('\')
}

function Lock-AuthorityFile([string]$Path, [string]$Label) {
  $resolved = Assert-RealRegularFile $Path $Label
  $stream = [System.IO.File]::Open(
    $resolved,
    [System.IO.FileMode]::Open,
    [System.IO.FileAccess]::Read,
    [System.IO.FileShare]::Read
  )
  try {
    $bytes = [byte[]]::new($stream.Length)
    $offset = 0
    while ($offset -lt $bytes.Length) {
      $read = $stream.Read($bytes, $offset, $bytes.Length - $offset)
      if ($read -le 0) { throw ($Label + '_short_read') }
      $offset += $read
    }
    $stream.Position = 0
    $item = Get-Item -LiteralPath $resolved -Force
    $script:fileLeases.Add($stream)
    return [pscustomobject]@{
      Path = $resolved
      Bytes = $bytes
      Sha256 = Get-BytesSha256 $bytes
      Length = [int64]$item.Length
      LastWriteTimeUtcTicks = [int64]$item.LastWriteTimeUtc.Ticks
    }
  }
  catch {
    $stream.Dispose()
    throw
  }
}

function Assert-AuthorityUnchanged($Authority, [string]$Label) {
  $resolved = Assert-RealRegularFile $Authority.Path $Label
  $item = Get-Item -LiteralPath $resolved -Force
  if ($item.Length -ne $Authority.Length -or
      $item.LastWriteTimeUtc.Ticks -ne $Authority.LastWriteTimeUtcTicks -or
      (Get-Sha256Hex $resolved) -cne $Authority.Sha256) {
    throw ($Label + '_changed_during_build')
  }
}

function Resolve-TrustedTool($Descriptor, [string]$Path, [string]$Label) {
  Assert-ExactProperties $Descriptor @(
    'file_name', 'file_version', 'product_version', 'sha256',
    'authenticode_subject', 'authenticode_thumbprint'
  ) ($Label + '_manifest')
  if ($Descriptor.file_name -notmatch '^(cl|link)\.exe$|^(c1xx|c2|msvcp140)\.dll$' -or
      $Descriptor.sha256 -notmatch '^[0-9a-f]{64}$' -or
      $Descriptor.file_version -notmatch '^[0-9]+(?:\.[0-9]+){3}$' -or
      $Descriptor.product_version -notmatch '^[0-9]+(?:\.[0-9]+){3}$' -or
      $Descriptor.authenticode_thumbprint -notmatch '^[0-9a-f]{40}$' -or
      [string]::IsNullOrWhiteSpace($Descriptor.authenticode_subject)) {
    throw ($Label + '_manifest_invalid')
  }
  $authority = Lock-AuthorityFile $Path $Label
  $resolved = $authority.Path
  if ([IO.Path]::GetFileName($resolved) -cne [string]$Descriptor.file_name) {
    throw ($Label + '_file_name_untrusted')
  }
  $version = [System.Diagnostics.FileVersionInfo]::GetVersionInfo($resolved)
  $signature = Get-AuthenticodeSignature -LiteralPath $resolved
  if ($authority.Sha256 -cne [string]$Descriptor.sha256 -or
      $version.FileVersion -cne [string]$Descriptor.file_version -or
      $version.ProductVersion -cne [string]$Descriptor.product_version -or
      $signature.Status -ne [System.Management.Automation.SignatureStatus]::Valid -or
      $null -eq $signature.SignerCertificate -or
      $signature.SignerCertificate.Subject -cne [string]$Descriptor.authenticode_subject -or
      $signature.SignerCertificate.Thumbprint.ToLowerInvariant() -cne [string]$Descriptor.authenticode_thumbprint) {
    throw ($Label + '_identity_untrusted')
  }
  $authority | Add-Member -NotePropertyName AuthenticodeThumbprint -NotePropertyValue $signature.SignerCertificate.Thumbprint.ToLowerInvariant()
  return $authority
}

function Resolve-GitHubHostedCompatibilityTool($Descriptor, [string]$Path, [string]$Label) {
  Assert-ExactProperties $Descriptor @(
    'file_name', 'file_version', 'product_version', 'sha256',
    'authenticode_subject', 'authenticode_thumbprint'
  ) ($Label + '_manifest')
  if ($Descriptor.file_name -notmatch '^(cl|link)\.exe$|^(c1xx|c2|msvcp140)\.dll$' -or
      $Descriptor.sha256 -notmatch '^[0-9a-f]{64}$' -or
      $Descriptor.file_version -notmatch '^[0-9]+(?:\.[0-9]+){3}$' -or
      $Descriptor.product_version -notmatch '^[0-9]+(?:\.[0-9]+){3}$' -or
      $Descriptor.authenticode_thumbprint -notmatch '^[0-9a-f]{40}$' -or
      [string]::IsNullOrWhiteSpace($Descriptor.authenticode_subject)) {
    throw ($Label + '_compatibility_manifest_invalid')
  }
  $authority = Lock-AuthorityFile $Path $Label
  $resolved = $authority.Path
  if ([IO.Path]::GetFileName($resolved) -cne [string]$Descriptor.file_name) {
    throw ($Label + '_file_name_untrusted')
  }
  $version = [System.Diagnostics.FileVersionInfo]::GetVersionInfo($resolved)
  $signature = Get-AuthenticodeSignature -LiteralPath $resolved
  $expectedFile = @(([string]$Descriptor.file_version).Split('.'))
  $actualFile = @(([string]$version.FileVersion).Split('.'))
  $expectedProduct = @(([string]$Descriptor.product_version).Split('.'))
  $actualProduct = @(([string]$version.ProductVersion).Split('.'))
  if ($actualFile.Count -ne 4 -or $actualProduct.Count -ne 4 -or
      $actualFile[0] -cne $expectedFile[0] -or
      $actualFile[1] -cne $expectedFile[1] -or
      $actualProduct[0] -cne $expectedProduct[0] -or
      $actualProduct[1] -cne $expectedProduct[1] -or
      $signature.Status -ne [System.Management.Automation.SignatureStatus]::Valid -or
      $null -eq $signature.SignerCertificate -or
      $signature.SignerCertificate.Subject -cne [string]$Descriptor.authenticode_subject) {
    throw ($Label + '_github_hosted_compatibility_untrusted')
  }
  $authority | Add-Member -NotePropertyName AuthenticodeThumbprint -NotePropertyValue $signature.SignerCertificate.Thumbprint.ToLowerInvariant()
  $authority | Add-Member -NotePropertyName AuthenticodeSubject -NotePropertyValue $signature.SignerCertificate.Subject
  $authority | Add-Member -NotePropertyName FileVersion -NotePropertyValue $version.FileVersion
  $authority | Add-Member -NotePropertyName ProductVersion -NotePropertyValue $version.ProductVersion
  return $authority
}

$sourceRoot = Assert-RealDirectory $sourceRoot 'source_root'
$manifestAuthority = Lock-AuthorityFile ([System.IO.Path]::GetFullPath($ToolchainManifest)) 'toolchain_manifest'
$toolchainManifestPath = $manifestAuthority.Path
if ($manifestAuthority.Length -gt 64KB) {
  throw 'toolchain_manifest_oversized'
}
if ($manifestAuthority.Sha256 -cne $ExpectedToolchainManifestSha256) {
  throw 'toolchain_manifest_caller_authority_mismatch'
}
try {
  $strictUtf8 = [System.Text.UTF8Encoding]::new($false, $true)
  $toolchain = $strictUtf8.GetString($manifestAuthority.Bytes) | ConvertFrom-Json
}
catch { throw 'toolchain_manifest_unreadable' }
Assert-ExactProperties $toolchain @(
  'schema_version', 'target', 'msvc_tools_version', 'windows_sdk_version',
  'tools', 'libraries', 'runtime_libraries'
) 'toolchain_manifest'
if ($toolchain.schema_version -ne 2 -or
    $toolchain.target -cne 'windows-x64-msvc' -or
    $toolchain.msvc_tools_version -notmatch '^14\.[0-9]+\.[0-9]+$' -or
    $toolchain.windows_sdk_version -notmatch '^10\.0\.[0-9]+\.0$') {
  throw 'toolchain_manifest_contract_invalid'
}
Assert-ExactProperties $toolchain.tools @('compiler', 'linker', 'c1xx', 'c2') 'toolchain_tools'
Assert-ExactProperties $toolchain.runtime_libraries @('msvcp140.dll') 'toolchain_runtime_libraries'

$sourceNames = @(
  'ecorex_launcher.cpp', 'ecorex_sandbox_host.cpp',
  'ecorex_sandbox_security.cpp', 'ecorex_sandbox_process.cpp',
  'ecorex_sandbox_host_internal.h'
)
$sourceSnapshot = Join-Path $output ('.native-source-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $sourceSnapshot -ErrorAction Stop | Out-Null
$sourceAuthorities = [ordered]@{}
foreach ($name in $sourceNames) {
  $authority = Lock-AuthorityFile (Join-Path $sourceRoot $name) ('source_' + $name)
  [System.IO.File]::WriteAllBytes((Join-Path $sourceSnapshot $name), $authority.Bytes)
  $sourceAuthorities[$name] = $authority
}
$sourceBinding = ($sourceNames | Sort-Object | ForEach-Object {
  $_ + '=' + $sourceAuthorities[$_].Sha256
}) -join "`0"
$sourceSetSha256 = Get-TextSha256 $sourceBinding
if ($sourceSetSha256 -cne $ExpectedSourceSetSha256) {
  throw 'source_set_caller_authority_mismatch'
}

$programFileRoots = @(
  [Environment]::GetFolderPath([Environment+SpecialFolder]::ProgramFiles),
  [Environment]::GetFolderPath([Environment+SpecialFolder]::ProgramFilesX86)
) | Where-Object { -not [string]::IsNullOrWhiteSpace($_) } | Select-Object -Unique
$vsRoots = @()
foreach ($programFiles in $programFileRoots) {
  $candidateRoot = Join-Path $programFiles 'Microsoft Visual Studio\2022'
  if (Test-Path -LiteralPath $candidateRoot -PathType Container) {
    $vsRoots += Assert-RealDirectory $candidateRoot 'visual_studio_root'
  }
}
if ($vsRoots.Count -lt 1) { throw 'trusted_visual_studio_layout_unavailable' }
$matches = @()
foreach ($vsRoot in $vsRoots) {
  foreach ($edition in Get-ChildItem -LiteralPath $vsRoot -Directory -Force) {
    if (($edition.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) { continue }
    $candidate = Join-Path $edition.FullName ('VC\Tools\MSVC\' + $toolchain.msvc_tools_version)
    $compiler = Join-Path $candidate 'bin\HostX64\x64\cl.exe'
    $compilerMatches = (Test-Path -LiteralPath $compiler -PathType Leaf) -and
      ($compatibilityMode -or
       (Get-Sha256Hex $compiler) -ceq [string]$toolchain.tools.compiler.sha256)
    if ($compilerMatches) {
      $matches += $candidate
    }
  }
}
$matches = @($matches | Select-Object -Unique)
if ($matches.Count -ne 1) { throw 'trusted_msvc_layout_unavailable' }
$msvcRoot = Assert-RealDirectory $matches[0] 'msvc_root'
$toolBin = Assert-RealDirectory (Join-Path $msvcRoot 'bin\HostX64\x64') 'msvc_bin'
$toolResolver = if ($compatibilityMode) {
  ${function:Resolve-GitHubHostedCompatibilityTool}
} else {
  ${function:Resolve-TrustedTool}
}
$compilerAuthority = & $toolResolver $toolchain.tools.compiler (Join-Path $toolBin 'cl.exe') 'compiler'
$linkerAuthority = & $toolResolver $toolchain.tools.linker (Join-Path $toolBin 'link.exe') 'linker'
$c1xxAuthority = & $toolResolver $toolchain.tools.c1xx (Join-Path $toolBin 'c1xx.dll') 'c1xx'
$c2Authority = & $toolResolver $toolchain.tools.c2 (Join-Path $toolBin 'c2.dll') 'c2'
$vcRoot = Split-Path -Parent (Split-Path -Parent (Split-Path -Parent $msvcRoot))
$redistVersionPath = Assert-RealRegularFile (Join-Path $vcRoot 'Auxiliary\Build\Microsoft.VCRedistVersion.default.txt') 'msvc_redist_version'
$redistVersion = ([System.IO.File]::ReadAllText($redistVersionPath)).Trim()
if ($redistVersion -notmatch '^14\.[0-9]+\.[0-9]+$') { throw 'trusted_msvc_redist_version_invalid' }
$redistRoot = Assert-RealDirectory (Join-Path $vcRoot ('Redist\MSVC\' + $redistVersion)) 'msvc_redist_root'
$msvcpPath = Join-Path $redistRoot 'x64\Microsoft.VC143.CRT\msvcp140.dll'
$msvcpAuthority = & $toolResolver $toolchain.runtime_libraries.'msvcp140.dll' $msvcpPath 'msvcp140'
[System.IO.File]::WriteAllBytes((Join-Path $output 'msvcp140.dll'), $msvcpAuthority.Bytes)
$clPath = $compilerAuthority.Path
$linkPath = $linkerAuthority.Path

$kitsProperty = Get-ItemProperty -LiteralPath 'HKLM:\SOFTWARE\Microsoft\Windows Kits\Installed Roots'
$sdkRoot = Assert-RealDirectory ([string]$kitsProperty.KitsRoot10) 'windows_sdk_root'
$sdkVersion = [string]$toolchain.windows_sdk_version
$includeRoots = @(
  (Assert-RealDirectory (Join-Path $msvcRoot 'include') 'msvc_include'),
  (Assert-RealDirectory (Join-Path $sdkRoot ('Include\' + $sdkVersion + '\ucrt')) 'sdk_ucrt_include'),
  (Assert-RealDirectory (Join-Path $sdkRoot ('Include\' + $sdkVersion + '\shared')) 'sdk_shared_include'),
  (Assert-RealDirectory (Join-Path $sdkRoot ('Include\' + $sdkVersion + '\um')) 'sdk_um_include'),
  (Assert-RealDirectory (Join-Path $sdkRoot ('Include\' + $sdkVersion + '\winrt')) 'sdk_winrt_include')
)
$msvcLibRoot = Assert-RealDirectory (Join-Path $msvcRoot 'lib\x64') 'msvc_lib'
$ucrtLibRoot = Assert-RealDirectory (Join-Path $sdkRoot ('Lib\' + $sdkVersion + '\ucrt\x64')) 'sdk_ucrt_lib'
$umLibRoot = Assert-RealDirectory (Join-Path $sdkRoot ('Lib\' + $sdkVersion + '\um\x64')) 'sdk_um_lib'

$expectedLibraries = @(
  'advapi32.lib', 'bcrypt.lib', 'kernel32.lib', 'libcmt.lib', 'libcpmt.lib',
  'libucrt.lib', 'libvcruntime.lib', 'oldnames.lib', 'shell32.lib',
  'userenv.lib', 'ws2_32.lib'
)
Assert-ExactProperties $toolchain.libraries $expectedLibraries 'toolchain_libraries'
$libraries = [ordered]@{}
$libraryAuthorities = [ordered]@{}
$observedLibraryDigests = [ordered]@{}
foreach ($name in $expectedLibraries) {
  $root = if ($name -in @('libcmt.lib', 'libcpmt.lib', 'libvcruntime.lib', 'oldnames.lib')) {
    $msvcLibRoot
  } elseif ($name -eq 'libucrt.lib') {
    $ucrtLibRoot
  } else {
    $umLibRoot
  }
  $authority = Lock-AuthorityFile (Join-Path $root $name) ('library_' + $name)
  $expected = [string]$toolchain.libraries.PSObject.Properties[$name].Value
  if ($expected -notmatch '^[0-9a-f]{64}$' -or
      (-not $compatibilityMode -and $authority.Sha256 -cne $expected)) {
    throw ('library_identity_untrusted:' + $name)
  }
  $libraries[$name] = $authority.Path
  $libraryAuthorities[$name] = $authority
  $observedLibraryDigests[$name] = $authority.Sha256
}

# No compiler/linker option or search path is inherited by either child.
foreach ($name in $injectionVariables) { Remove-Item ('Env:' + $name) -ErrorAction SilentlyContinue }
$windowsRoot = [Environment]::GetFolderPath([Environment+SpecialFolder]::Windows)
$windowsRoot = Assert-RealDirectory $windowsRoot 'windows_root'
$env:SystemRoot = $windowsRoot
$env:WINDIR = $windowsRoot
$env:PATH = $toolBin + ';' + (Join-Path $windowsRoot 'System32') + ';' + $windowsRoot
$common = @(
  '/nologo', '/std:c++20', '/O2', '/GL', '/GS', '/guard:cf', '/sdl', '/Brepro',
  '/DUNICODE', '/D_UNICODE', '/EHsc', '/W4', '/WX', '/MT', '/DNDEBUG', '/X'
)
foreach ($root in $includeRoots) { $common += ('/I' + $root) }
$linker = @(
  '/NOLOGO', '/INCREMENTAL:NO', '/LTCG', '/OPT:REF', '/OPT:ICF', '/DYNAMICBASE',
  '/HIGHENTROPYVA', '/NXCOMPAT', '/CETCOMPAT', '/GUARD:CF', '/Brepro', '/DEBUG:NONE',
  '/MANIFESTUAC:NO', '/APPCONTAINER:NO', '/WX', '/NODEFAULTLIB'
)
$runtimeLibraries = @(
  $libraries['libcmt.lib'], $libraries['libcpmt.lib'],
  $libraries['libvcruntime.lib'], $libraries['libucrt.lib'],
  $libraries['oldnames.lib'], $libraries['kernel32.lib']
)

$launcherObject = Join-Path $output 'ecorex_launcher.obj'
$sandboxHostObject = Join-Path $output 'ecorex_sandbox_host.obj'
$sandboxSecurityObject = Join-Path $output 'ecorex_sandbox_security.obj'
$sandboxProcessObject = Join-Path $output 'ecorex_sandbox_process.obj'
& $clPath @common '/c' ('/Fo:' + $launcherObject) (Join-Path $sourceSnapshot 'ecorex_launcher.cpp')
if ($LASTEXITCODE -ne 0) { throw 'runtime_launcher_build_failed' }
& $linkPath ('/OUT:' + (Join-Path $output 'ecorex.exe')) $launcherObject @runtimeLibraries $libraries['shell32.lib'] @linker
if ($LASTEXITCODE -ne 0) { throw 'runtime_launcher_link_failed' }
& $clPath @common '/c' ('/Fo:' + $sandboxHostObject) (Join-Path $sourceSnapshot 'ecorex_sandbox_host.cpp')
if ($LASTEXITCODE -ne 0) { throw 'sandbox_helper_build_failed' }
& $clPath @common '/c' ('/Fo:' + $sandboxSecurityObject) (Join-Path $sourceSnapshot 'ecorex_sandbox_security.cpp')
if ($LASTEXITCODE -ne 0) { throw 'sandbox_security_build_failed' }
& $clPath @common '/c' ('/Fo:' + $sandboxProcessObject) (Join-Path $sourceSnapshot 'ecorex_sandbox_process.cpp')
if ($LASTEXITCODE -ne 0) { throw 'sandbox_process_build_failed' }
& $linkPath ('/OUT:' + (Join-Path $output 'ecorex-sandbox-host.exe')) $sandboxHostObject $sandboxSecurityObject $sandboxProcessObject @runtimeLibraries $libraries['advapi32.lib'] $libraries['bcrypt.lib'] $libraries['userenv.lib'] $libraries['ws2_32.lib'] @linker
if ($LASTEXITCODE -ne 0) { throw 'sandbox_helper_link_failed' }
Remove-Item -LiteralPath $launcherObject, $sandboxHostObject, $sandboxSecurityObject, $sandboxProcessObject -Force -ErrorAction Stop
Remove-Item -LiteralPath $sourceSnapshot -Recurse -Force -ErrorAction Stop

$libraryBinding = ($expectedLibraries | Sort-Object | ForEach-Object {
  $digest = if ($compatibilityMode) {
    [string]$observedLibraryDigests[$_]
  } else {
    [string]$toolchain.libraries.PSObject.Properties[$_].Value
  }
  $_ + '=' + $digest
}) -join "`0"
Assert-AuthorityUnchanged $manifestAuthority 'toolchain_manifest'
foreach ($name in $sourceNames) {
  Assert-AuthorityUnchanged $sourceAuthorities[$name] ('source_' + $name)
}
foreach ($name in $expectedLibraries) {
  Assert-AuthorityUnchanged $libraryAuthorities[$name] ('library_' + $name)
}
$toolChecks = @(
  [pscustomobject]@{ Authority = $compilerAuthority; Descriptor = $toolchain.tools.compiler; Label = 'compiler' }
  [pscustomobject]@{ Authority = $linkerAuthority; Descriptor = $toolchain.tools.linker; Label = 'linker' }
  [pscustomobject]@{ Authority = $c1xxAuthority; Descriptor = $toolchain.tools.c1xx; Label = 'c1xx' }
  [pscustomobject]@{ Authority = $c2Authority; Descriptor = $toolchain.tools.c2; Label = 'c2' }
)
foreach ($entry in $toolChecks) {
  $authority = $entry.Authority
  $descriptor = $entry.Descriptor
  $label = [string]$entry.Label
  Assert-AuthorityUnchanged $authority $label
  $postSignature = Get-AuthenticodeSignature -LiteralPath $authority.Path
  $postVersion = [System.Diagnostics.FileVersionInfo]::GetVersionInfo($authority.Path)
  $expectedThumbprint = if ($compatibilityMode) {
    [string]$authority.AuthenticodeThumbprint
  } else {
    [string]$descriptor.authenticode_thumbprint
  }
  $expectedFileVersion = if ($compatibilityMode) {
    [string]$authority.FileVersion
  } else {
    [string]$descriptor.file_version
  }
  $expectedProductVersion = if ($compatibilityMode) {
    [string]$authority.ProductVersion
  } else {
    [string]$descriptor.product_version
  }
  if ($postSignature.Status -ne [System.Management.Automation.SignatureStatus]::Valid -or
      $null -eq $postSignature.SignerCertificate -or
      $postSignature.SignerCertificate.Subject -cne [string]$descriptor.authenticode_subject -or
      $postSignature.SignerCertificate.Thumbprint.ToLowerInvariant() -cne $expectedThumbprint -or
      $postVersion.FileVersion -cne $expectedFileVersion -or
      $postVersion.ProductVersion -cne $expectedProductVersion) {
    throw ($label + '_changed_during_build')
  }
}
Assert-AuthorityUnchanged $msvcpAuthority 'msvcp140'
$msvcpSignature = Get-AuthenticodeSignature -LiteralPath $msvcpAuthority.Path
$msvcpVersion = [System.Diagnostics.FileVersionInfo]::GetVersionInfo($msvcpAuthority.Path)
$expectedMsvcpThumbprint = if ($compatibilityMode) {
  [string]$msvcpAuthority.AuthenticodeThumbprint
} else {
  [string]$toolchain.runtime_libraries.'msvcp140.dll'.authenticode_thumbprint
}
if ($msvcpSignature.Status -ne [System.Management.Automation.SignatureStatus]::Valid -or
    $null -eq $msvcpSignature.SignerCertificate -or
    $msvcpSignature.SignerCertificate.Thumbprint.ToLowerInvariant() -cne $expectedMsvcpThumbprint -or
    (Get-Sha256Hex (Join-Path $output 'msvcp140.dll')) -cne $msvcpAuthority.Sha256) {
  throw 'msvcp140_changed_during_build'
}
$receipt = [ordered]@{
  schema_version = 2
  status = 'passed'
  target = 'windows-x64'
  authority_mode = if ($compatibilityMode) { 'github-hosted-ci-compatibility' } else { 'caller-pinned' }
  toolchain_manifest_sha256 = $manifestAuthority.Sha256
  source_set_sha256 = $sourceSetSha256
  msvc_tools_version = [string]$toolchain.msvc_tools_version
  windows_sdk_version = $sdkVersion
  msvc_root_sha256 = Get-TextSha256 $msvcRoot.ToLowerInvariant()
  windows_sdk_root_sha256 = Get-TextSha256 $sdkRoot.ToLowerInvariant()
  include_roots_sha256 = Get-TextSha256 (($includeRoots | ForEach-Object { $_.ToLowerInvariant() }) -join "`0")
  library_roots_sha256 = Get-TextSha256 ((@($msvcLibRoot, $ucrtLibRoot, $umLibRoot) | ForEach-Object { $_.ToLowerInvariant() }) -join "`0")
  library_set_sha256 = Get-TextSha256 $libraryBinding
  compiler_sha256 = $compilerAuthority.Sha256
  compiler_file_version = [System.Diagnostics.FileVersionInfo]::GetVersionInfo($clPath).FileVersion
  compiler_authenticode_thumbprint = $compilerAuthority.AuthenticodeThumbprint
  linker_sha256 = $linkerAuthority.Sha256
  linker_file_version = [System.Diagnostics.FileVersionInfo]::GetVersionInfo($linkPath).FileVersion
  linker_authenticode_thumbprint = $linkerAuthority.AuthenticodeThumbprint
  c1xx_sha256 = $c1xxAuthority.Sha256
  c1xx_authenticode_thumbprint = $c1xxAuthority.AuthenticodeThumbprint
  c2_sha256 = $c2Authority.Sha256
  c2_authenticode_thumbprint = $c2Authority.AuthenticodeThumbprint
  runtime_launcher_sha256 = Get-Sha256Hex (Join-Path $output 'ecorex.exe')
  sandbox_helper_sha256 = Get-Sha256Hex (Join-Path $output 'ecorex-sandbox-host.exe')
  msvcp140_sha256 = Get-Sha256Hex (Join-Path $output 'msvcp140.dll')
  msvcp140_file_version = $msvcpVersion.FileVersion
  msvcp140_authenticode_thumbprint = $expectedMsvcpThumbprint
}
$json = $receipt | ConvertTo-Json -Compress
[System.IO.File]::WriteAllText($receiptPath, $json + "`n", [System.Text.UTF8Encoding]::new($false))
