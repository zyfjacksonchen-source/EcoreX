# S3 Runtime Packs And Installer

## Intent

Stop treating `desktop/runtime-packs` as the implicit shared source. Web release, runtime manifest, capability service entrypoints, and legacy installers should use a shared public runtime-pack contract and one installer implementation.

## Implemented Changes

- Added `runtime-packs/` as the shared source of truth for `capabilities.json` and `core-requirements.txt`.
- Web Linux service release now copies runtime packs from `runtime-packs/`.
- Agent optional abilities now discover packaged `capabilities.json`, then `runtime-packs/capabilities.json`, then legacy desktop fallback.
- Public `scripts/install-capability.py` now supports `--action status|install|repair|doctor`.
- Public installer defaults state to `ECOREX_CAPABILITY_STATE_DIR` or `$RUNTIME_DIR/capability-state`.
- Public installer defaults packages to `ECOREX_CAPABILITY_TARGET_DIR/{pack}` or `$RUNTIME_DIR/capability-packages/{pack}`.
- Discovery-only status no longer writes raw `sourceUrl` / `mirrorUrls` into state; it writes `sourceConfigured` / `mirrorConfigured`.
- Installer output and logs redact credentials in pip index / mirror URLs.
- Installer state, package, and browser paths are confined to EcoreX owned state/runtime directories.
- Installer status/log/lock file names use sanitized pack ids to prevent path traversal from a manifest id.
- Module probes no longer fall back to host Python/global `sys.path`; they only inspect the capability target and owned runtime site-packages.
- `configureOnly` packs now report `needs_configuration` / `nextAction=configure` and cannot pass as an empty successful install.
- Legacy `desktop/scripts/install-capability.py` is now a thin wrapper to the shared installer.
- Windows/macOS runtime staging copies runtime packs and installer from the shared public locations.
- Local WebUI packaging contract reads public `runtime-packs/` instead of the desktop path.

## Acceptance

- Public runtime-packs and legacy compatibility copies remain byte-identical.
- `install-capability.py --action status` writes unified state and target-dir metadata.
- `install-capability.py --action doctor` writes `capability-doctor.json`.
- Discovery-only packs do not leak raw source or mirror URLs into installer state.
- Secret pip index URLs do not leak into installer logs or raised command errors.
- Malicious pack ids cannot write status/log/lock files outside `capability-state`.
- Configure-only packs cannot be misreported as installed by an empty `moduleChecks`.
- Web Linux service tarball contains public installer actions, runtime packs, and the S3a title fix.

## Evidence

- `docs/web-runtime-goal/artifacts/S03-runtime-packs-installer-tests.json`

## Remaining Notes

- Direct Node download introduced in S2 still needs to move into shared runtime-pack provenance semantics in a later S3/S9 continuation.
- Generated runtime copies under `desktop/runtime/ecorex-runtime/` were mechanically synced to the public installer and runtime-pack source after review found stale copies.
