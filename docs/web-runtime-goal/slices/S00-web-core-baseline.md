# S0 Web Core Baseline

## Intent

Define the Web runtime baseline before changing more capability flows. The baseline distinguishes package/runtime failures from model credential gaps so users do not see "tool missing" when the real issue is credentials or a repairable optional backend.

## Implemented Changes

- Added `scripts/check-web-core-runtime-baseline.py`.
- The checker captures `coreRequired`, `optionalRepairable`, and `credentialRequired` dependency rows.
- The checker reports `releaseReady=false` when any `coreRequired` dependency is missing.
- The default artifact path is `docs/web-runtime-goal/artifacts/S0-web-core-runtime-current.json`.

## Web Core Required Dependencies

- Executables: `python`, `node`, `npm`, `npx`.
- Python packages: `pip`, `PIL`, `rapidocr_onnxruntime`, `onnxruntime`, `playwright`, `lark_oapi`, and office/PDF parsing modules.
- Tool entrypoints: `vision`, `ocr`, `imagegen`.

## Acceptance

- Running the checker without `--strict` writes a redacted JSON report.
- Running the checker with `--strict` exits nonzero when core dependencies are missing.
- Missing model credentials are reported as `missing_model_credentials`, not package/runtime failure.
- Optional native backends are marked `optionalRepairable` and do not block release when a repair action exists.
