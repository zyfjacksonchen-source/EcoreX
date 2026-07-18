# EcoreX v1 direct-release operator waiver

This path exists only for the administrator's explicit, release-scoped waiver.
It does not replace the protected release design and must never be reported as
a protected Environment, isolated Runner, managed CDP or KMS/HSM pass.

## Safety boundary

- Source must be the clean local `main` ref and the exact local
  `origin/main` commit supplied to the command. Untracked operator artifacts do
  not alter the source identity; any tracked or staged change blocks the build.
- Candidate assembly remains unchanged. Windows x64, macOS arm64 and macOS x64
  stage trees, stage receipts, platform gate evidence, staging provenance,
  dependency lock, Web tree and embedded trust stores are mandatory.
- The direct path uses two independent Ed25519 authorities. The release key is
  protected with Windows DPAPI and signs Candidate artifacts, the manifest,
  Candidate receipt and waiver. The publication key is generated and remains
  on the attested encrypted production volume; only its public description is
  copied to the build machine.
- This is explicitly a software-key waiver, not a KMS/HSM pass. Private key
  material is never accepted through argv/environment, included in evidence,
  printed or uploaded. Release and publication storage classes are recorded
  separately in the waiver.
- The waiver records `operator-waived`, all skipped gates with
  `represented_as_passed=false`, and the exact commit, release ID, build digest,
  manifest digest, Candidate receipt digest and hash of the operator's
  instruction. It says publication is incomplete and cannot authorize a live
  pointer.
- Stable immutable release bytes must be published to the signed domestic
  GitHub mirror. Canary additionally requires GitHub Releases and EcoreX CDN.
  The online verifier performs a full HTTPS GET and SHA-256 validation for
  every required source; a HEAD result is never digest evidence.
- The checked-in download site remains the canonical `unpublished` document.
  Only a staging copy containing a `published` index reproduced from the exact
  three-origin receipt and signed by both direct keys can pass the direct
  deployment checker.

## One-time key initialization

Run this as the same Windows operator identity that will sign the release. The
command refuses to rotate or overwrite an existing operator key pair.

```powershell
python scripts/ecorex-v1-dpapi-ed25519-signer.py initialize
python scripts/ecorex-v1-dpapi-ed25519-signer.py describe
```

`describe` returns only public material. Back up the DPAPI release key with the
operator's protected Windows profile. Initialize the independent production
server keyring only after its encrypted-volume attestation passes, then export
the public-only `publication` description with
`describe-v1-server-signing-public-key.py`.

## Build from real stages

First run the platform stager on its real Windows and macOS runners after
injecting a Runtime config whose release trust ring contains the described
release public key and whose publication trust ring contains exactly the
described publication public key. Merge all three target outputs and their
receipts into one immutable input root. Do not construct stage receipts by
hand.

Hash the exact operator instruction locally; only the digest enters evidence.

```powershell
$instruction = "operator explicitly waived protected release gates for this exact v1 release"
$instructionHash = [Convert]::ToHexString(
  [Security.Cryptography.SHA256]::HashData(
    [Text.Encoding]::UTF8.GetBytes($instruction)
  )
).ToLowerInvariant()
$commit = git rev-parse refs/remotes/origin/main

python scripts/build-v1-direct-operator-release.py `
  --recipe C:\ecorex-stage\recipe.json `
  --input-root C:\ecorex-stage `
  --web-dist C:\ecorex-stage\web-dist `
  --output C:\ecorex-release\1.0.0 `
  --receipt C:\ecorex-release\1.0.0-candidate.json `
  --waiver C:\ecorex-release\1.0.0-direct-waiver.json `
  --expected-commit $commit `
  --expected-staging-run-id 123456 `
  --staging-provenance provenance.json `
  --dependency-lock-manifest dependency-lock-manifest.json `
  --publication-key-description C:\secure-input\publication-public-key.json `
  --operator-instruction-sha256 $instructionHash
```

The success result deliberately reports:

```text
protected_pipeline_passed=false
publication_completed=false
live_pointer_authorized=false
```

## Publish immutable assets and build the staged site

Publish the exact Stable release directory to the writable domestic GitHub
mirror and verify that signed primary source before publishing its pointer.
Canary additionally publishes GitHub Release and EcoreX CDN bytes. Run
`verify-v1-online-publication.py` for every required source; do not upload the
public pointer before its canonical receipt exists. Then copy
`deploy/ecorex-site` to a separate staging directory and run the existing
`build-public-bootstrap-index` command with:

- the DPAPI release adapter for the authority signature;
- a digest-pinned production/SSH adapter for the server-resident publication
  freshness signer;
- the release key description in the release signer/trusted-key variables;
- the publication key description in the publication signer/trusted-key
  variables;
- the exact immutable three-origin publication receipt.

`DigestPinnedExternalSigner` verifies each signature against its expected
public key. Later freshness rotation runs inside Control Plane using the same
server-resident publication signer.

## Mandatory deployment preflight

Run this against the staged site, never the checked-in unpublished site:

```powershell
python scripts/check-v1-direct-release-deployable.py `
  --release-dir C:\ecorex-release\1.0.0 `
  --candidate-receipt C:\ecorex-release\1.0.0-candidate.json `
  --waiver C:\ecorex-release\1.0.0-direct-waiver.json `
  --publication-receipt C:\ecorex-release\1.0.0-publication.json `
  --site-root C:\ecorex-site-stage `
  --expected-commit $commit `
  --publication-key-description C:\secure-input\publication-public-key.json `
  --operator-instruction-sha256 $instructionHash
```

The checker re-verifies the complete release directory, all artifact
signatures/digests, Candidate/waiver binding, distinct key roles, exact
three-origin publication receipt, authority/freshness signatures, immutable
asset names and HTML references. It rejects `status=unpublished`, legacy static
admin authority, an altered receipt or any missing/extra release file.

Remote deployment is a separate atomic stage/switch/health/rollback operation.
These commands create no network publication and no production mutation.
