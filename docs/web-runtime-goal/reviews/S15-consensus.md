# S15 Review Consensus

## Decision

PASS_WITH_NOTES.

## Review Results

- Runtime installer review: `PASS_WITH_NOTES`.
  - macOS launcher no longer exits before the installer finishes.
  - installer writes `ecorex-webui.url`, opens the browser from both installer and launcher fallback, and constrains old-service cleanup to the current runtime path.
- Web UX / release notes review: `PASS_WITH_NOTES`.
  - release notes now use `version:revision` as the local seen key.
  - revision `2026-07-02-mac-webui-r4` makes same-version hotfixes visible once and suppresses repeats after dismissal.
- Test / release review: `PASS_WITH_NOTES`.
  - regenerated artifacts, deploy downloads, manifest, and public release are aligned.
  - macOS WebUI zip has zero non-ASCII path entries.
- Security review: `PASS_WITH_NOTES`.
  - non-ASCII paths are removed before packaging and checked fail-closed.
  - stale PID and port cleanup only kills processes whose command line matches the current `$RUNTIME_DIR/app.py`.
- Architecture review: `PASS_WITH_NOTES`.
  - release notes revision `r4` is present in source and all packages.
  - manifest, deploy downloads, release artifacts, and public release form one consistent path.
  - shared renderer / Electron `POST /api/models` remains a known non-blocking desktop compatibility risk, but S15 is WebUI package/install only and does not depend on Electron.

## Notes

- Historical downloads remain in `deploy/ecorex-site/downloads` for old direct links, but the current manifest ready chain points only to regenerated v0.2.6 artifacts.
- A full 300MB online mac zip body download from this workstation timed out; online HEAD, manifest, API revision, local public release SHA, package validation, and local zip entry audit passed.
