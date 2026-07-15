# S15: macOS WebUI Install Hotfix

## Goal

Fix the macOS WebUI installer failure where `install-webui.sh` downloaded a valid package but `unzip` failed with `Illegal byte sequence` / truncated file warnings before the local installer could run.

## Root Cause

The WebUI runtime package copied repository development scripts into the release runtime. Four development-only Python scripts had non-ASCII filenames. Some macOS `unzip` environments failed while creating these filenames under the extracted `.app` bundle.

## Changes

- `scripts/sanitize-ecorex-release-runtime.py`
  - removes non-ASCII release paths during runtime sanitization.
  - fails `--check` if any non-portable path remains.
- `scripts/prepare-ecorex-webui-local-release.ps1`
  - keeps macOS installer process cleanup constrained to the current `$RUNTIME_DIR/app.py`.
  - writes `ecorex-webui.url` and performs a launcher-level browser-open fallback.
- `common/ecorex_release_notes.py`
  - bumps release notes revision to `2026-07-02-mac-webui-r4`.
  - records the macOS zip extraction fix.
- `deploy/ecorex-site/manifest.json`
  - points to regenerated v0.2.6 artifacts.

## Acceptance

- macOS WebUI zip contains zero non-ASCII path entries.
- Windows/macOS/Linux release artifacts match manifest size and SHA256.
- Public release validation passes.
- Online manifest and HEAD checks point to the regenerated artifacts.
- Multi-agent review has no blocking `FAIL`.
