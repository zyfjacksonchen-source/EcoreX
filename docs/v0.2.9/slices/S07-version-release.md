# S08 Version and Release Metadata

## Status

Completed.

## Intent

Update version metadata and release notes for v0.2.9.

## Decisions

- Version target is `0.2.9`.
- Keep WebUI packaging path focused on WebUI release.
- Public manifest WebUI artifacts were initially promoted to `0.2.9` metadata without reusing v0.2.8 hashes, then S09 promoted them to `ready` with real package size/SHA evidence.

## Implementation

- Updated CLI, desktop package metadata, WebUI local packager defaults, public installer script markers, Admin API version, WebChannel enterprise client keys, and WebChannel current User-Agent strings to `0.2.9`.
- Rewrote current WebUI release notes for the v0.2.9 audit, effective artifact, feedback trace, knowledge graph, default identity, thinking motion, and scheduler UI work.
- Updated public manifest root version, notes, mirrors, and WebUI Windows/macOS artifact names to `0.2.9`; S09 later filled real artifact size/SHA and promoted WebUI artifacts to `ready`.
- Kept historical hidden non-WebUI/service artifacts unchanged when they represent older validated packages.

## Verification

- `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest tests/test_v029_release_metadata.py tests/test_v025_runtime_manifest.py::test_v027_webui_installers_keep_windows_macos_user_flow_consistent tests/test_ecorex_admin_device_id.py::AdminReleaseStateTest::test_client_release_notice_endpoint_returns_admin_data_notice tests/test_ecorex_web_parallel_backend.py::TestWebParallelHandlers::test_enterprise_release_notice_uses_current_client_key_after_legacy_key -q`
  - Passed: 6 tests.
- `npm run typecheck`
  - Passed.
- Plain pytest collection without `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1` remains blocked by the local `langsmith` / `pydantic-core` mismatch.
