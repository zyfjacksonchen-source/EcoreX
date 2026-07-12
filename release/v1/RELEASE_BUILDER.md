# EcoreX v1 deterministic release builder

## Public API

The release pipeline constructs `ArtifactBuildInput` values, wraps them in one
`ReleaseBuildSpec`, injects a `ReleaseSigner`, and calls
`ReleaseBuilder.build(spec, destination)`. `destination` must not already exist.
The return value is a `BuiltRelease` containing the parsed signed manifest and
the final paths. There is intentionally no private-key CLI.

Pass exactly one `WebBundleBuildInput(dist_dir)` on `ReleaseBuildSpec.web_bundle`
to bind the production React dist. The builder emits `web-manifest.json` as the
reserved `ReleaseArtifact` ID `web-manifest` with target `all/all`; it is also
available through `BuiltRelease.artifact_paths["web-manifest"]`.

For a process-local ceremony, inject an existing
`cryptography.hazmat.primitives.asymmetric.ed25519.Ed25519PrivateKey` into
`Ed25519MemorySigner`. An HSM/KMS adapter can instead implement the small
`ReleaseSigner` protocol (`key_id` plus `sign(payload)`). A signer must return
exactly 64 Ed25519 signature bytes. The builder never serializes a private key,
accepts no private-key path, and redacts adapter failures without retaining a
potentially sensitive exception chain.

## Reproducible package rules

- Archive members are sorted by normalized POSIX path.
- Every ZIP timestamp is `1980-01-01 00:00:00`.
- Directories and explicitly declared executables use mode `0755`; every other
  file uses `0644`. Source filesystem mode and timestamp metadata are ignored.
- ZIP comments and per-entry extra fields are empty; DEFLATE level 9 is used.
- Symlinks, Windows reparse points, special files, unsafe/reserved path
  segments, NFKC/casefold collisions, and more than 50,000 members are rejected.
- The source is checked again while each file is streamed, so a changed file
  aborts the build instead of producing mixed metadata.
- Shell sources ending in `.sh` must already use LF line endings. The builder
  rejects carriage returns instead of silently rewriting signed input bytes;
  accepted shell bytes and their recorded SHA-256 remain exact.
- Core compressed size is at most 150 MiB; bootstrap compressed size is at most
  10 MiB. A separate 2 GiB unpacked hard limit bounds source expansion.

Byte-for-byte reproduction requires the same pinned Python and zlib toolchain.
The ZIP inputs and metadata are otherwise independent of source-root location,
mtime, and host filesystem permission bits.

## React dist and Web manifest rules

- The dist root must contain exactly `index.html` and `assets/`. Hidden files,
  unrelated root files, empty directories, links/reparse points, special files,
  source maps, and source-code extensions are rejected.
- Every asset is non-empty, uses an approved production media suffix, and has
  at least the first eight characters of its actual SHA-256 in its filename.
- `index.html` must contain exactly one
  `<!--__ECOREX_RUNTIME_CONFIG__-->` comment inside `<head>` and at least one
  same-origin hashed script reference.
- Inline scripts, inline script bodies, `<style>` elements, `style=` and event
  attributes, embedded frames/objects, `<base>`, meta CSP overrides, external
  asset URLs, cache-busting query strings, and unlisted references are rejected.
- The exact allowlist is the dependency graph reachable from `index.html`
  through generated JS/CSS/JSON/SVG/WebManifest references. Orphaned hash-named
  files and missing lazy dependencies fail the release instead of becoming
  silently trusted stale files.
- Known `chat.html`, `channel/web`, Electron output, and v0.29/v0.30 WebUI
  overlay references or content markers are forbidden even after renaming.
- File size, SHA-256 and immutable state are recorded in sorted order. The
  server's domain-separated `bundle_sha256` is computed over those records.

The source tree is scanned before packaging and again before atomic publication.
The product server subsequently rejects any installed Web-root file that is not
in this signed allowlist.

## Build identity and verification

`build_digest` is SHA-256 over the domain-separated canonical JSON material
`ecorex-build-v1`, containing product version, channel, ordered artifact
identity/target/name/size/SHA-256, and each packaged file's path, size, fixed
mode, and SHA-256. Production Candidates additionally bind the SHA-256 of the
validated `requirements/locks/manifest.json`; changing any Python lock therefore
changes both `build_digest` and `release_id`. Artifact signatures cover the updater's
`ecorex-artifact-v1` payload. The manifest signature covers
`ecorex-release-manifest-v1` canonical JSON. Both are detached from the bytes
they authenticate and carry the signer's key ID in a `SignatureEnvelope`.

For a Web release, `build_digest` includes the complete unsigned Web file
inventory, entrypoint and `bundle_sha256`. It intentionally does not hash the
final `web-manifest.json` directly because that JSON itself contains
`build_digest`. After the digest and `release_id` are fixed, the builder signs
`ecorex-web-manifest-v1`, hashes and signs the resulting JSON as the
`web-manifest` ReleaseArtifact, and finally signs the enclosing ReleaseManifest.
This removes the self-reference while preserving the full trust chain.

`release-metadata.json` records the exact SHA-256 of the manifest, SBOM and
Python dependency lock set plus the signed artifact records. `sbom.cdx.json`
uses CycloneDX 1.5 fields and lists each produced ZIP, each packaged file and
the content-addressed Python lock authority. Consumers must verify the manifest
signature with the configured public keyring, verify each selected artifact,
and recompute hashes before publication or installation.

## Atomicity and ceremony boundary

All packages and JSON records are created and fsynced in a sibling staging
directory. The builder refuses an existing destination and publishes by one
directory rename. The release orchestrator must still hold its product-level
publisher lock: portable Python has no cross-platform atomic
"rename-directory-if-absent" primitive for a hostile concurrent writer.

Production key custody, HSM/KMS authentication, two-person approval, key
rotation/revocation, public-key rollout, attested CI provenance, and mirror
upload are outside this library and remain release-operations responsibilities.

The production `desktop/dist` is emitted through the repository Web finalizer:
asset names are content-addressed and HTML contains the required runtime marker
and bounded inline theme bootstrap before ReleaseBuilder scans it. An arbitrary
raw Vite directory that has not crossed that finalizer remains rejected.
