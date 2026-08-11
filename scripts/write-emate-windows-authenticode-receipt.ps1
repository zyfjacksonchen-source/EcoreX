param(
    [Parameter(Mandatory = $true)][string]$Installer,
    [Parameter(Mandatory = $true)][string]$UnsignedSha256,
    [Parameter(Mandatory = $true)][string]$ExpectedSignerThumbprint,
    [Parameter(Mandatory = $true)][string]$Version,
    [Parameter(Mandatory = $true)][string]$SourceCommit,
    [Parameter(Mandatory = $true)][string]$BaseFeedBuildId,
    [Parameter(Mandatory = $true)][string]$Output
)

$ErrorActionPreference = "Stop"
$path = (Resolve-Path -LiteralPath $Installer).Path
$signature = Get-AuthenticodeSignature -LiteralPath $path
if ($signature.Status -ne "Valid") { throw "Windows installer Authenticode status is $($signature.Status)." }
$thumbprint = ([string]$signature.SignerCertificate.Thumbprint).ToUpperInvariant()
if ($thumbprint -ne $ExpectedSignerThumbprint.ToUpperInvariant()) { throw "Windows installer signer is not the expected certificate." }
$signedSha256 = (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant()
$unsigned = $UnsignedSha256.ToLowerInvariant()
if ($signedSha256 -eq $unsigned) { throw "Authenticode signing did not change the installer bytes." }
if (Test-Path -LiteralPath $Output) { throw "Receipt output already exists: $Output" }

$receipt = [ordered]@{
    schema_version = 1
    document_type = "emate.windows-authenticode-receipt"
    status = "verified"
    version = $Version
    source_commit = $SourceCommit.ToLowerInvariant()
    base_feed_build_id = $BaseFeedBuildId.ToLowerInvariant()
    file_name = [System.IO.Path]::GetFileName($path)
    unsigned_sha256 = $unsigned
    signed_sha256 = $signedSha256
    signed_size_bytes = (Get-Item -LiteralPath $path).Length
    signature_status = "Valid"
    signer_certificate_thumbprint = $thumbprint
}
[System.IO.File]::WriteAllText($Output, (($receipt | ConvertTo-Json -Compress) + "`n"), (New-Object System.Text.UTF8Encoding($false)))
