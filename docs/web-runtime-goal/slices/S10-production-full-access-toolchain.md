# S10: Production Full-Access Toolchain Smoke

## Goal

Verify from the deployed Web surface that full-access mode makes core capabilities discoverable and callable, and that a multi-step image toolchain uses `gpt-image-2-pro` for both generation and editing instead of falling back to local Python image generation.

## Scope

- Public Web URL: production `/ecorex-agent` Web service.
- Web-only runtime package and Agent common runtime.
- No desktop/Electron validation.
- Secrets are loaded from server/admin runtime configuration and never written to artifacts.

## Changes

- Added `scripts/smoke-full-access-toolchain.py`.
- Smoke logs into Web, sets `/api/tool-permissions` to `full-access`, calls discovery endpoints, probes runtime dependencies, invokes OCR/Vision/Browser, and optionally runs real image generation plus image edit through `/api/image-jobs`.
- Smoke now initializes the deployed `config.json` like the Web process, uses the correct `OcrTool`, verifies nested OCR status, and uses the server-side generated artifact path for the edit step to avoid large JSON/data URL request bodies.
- Production runtime was repaired with OS/browser dependencies required by the Web package:
  - `libGL.so.1` and related Chromium/rapidocr libraries.
  - Playwright Chromium v1223 browser cache.

## Acceptance

- Full-access permission API returns `mode=full-access` with audit path.
- `/api/capabilities`, `/api/tools`, `/api/skills`, `/api/extensions`, `/api/models` all return OK and expose `imagegen`, `ocr`, `vision`, `browser`, `feishu`, `office-pdf`.
- Python, Node, npm, npx resolve from EcoreX state/runtime and execute.
- OCR extracts sample text through `rapidocr_onnxruntime`.
- Vision succeeds through configured model provider.
- Browser tool can navigate the production `/api/version` endpoint through Playwright fallback when CDP is not already running.
- `/api/image-jobs` generate and edit both complete, both emit `OpenAI/gpt-image-2-pro`, and neither reports model fallback.
- Production acceptance uses `--require-real-imagegen`, so real imagegen cannot be skipped.
- OCR acceptance requires the sample text to be recognized as `ECOREXOCR`, not just a successful tool wrapper status.

## Result

PASS_WITH_NOTES.

Notes:
- Production CDP endpoint was not already running and no system Chrome executable was present; Playwright fallback is now installed and verified. CDP auto-launch remains disabled in config, so this pass is for browser capability invocation through the configured fallback path.
- The edit step uses the server-local image job artifact path, matching server-side agent chaining. Browser-uploaded edit flows should continue sending uploaded file references rather than base64 data URLs to avoid request-size limits.
- S10 artifacts contain internal production host paths for traceability; external sharing should use redacted path-present/hash summaries.
