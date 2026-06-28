# EcoreX v0.2.4 Baseline Scan

Generated from the first v0.2.4 long-goal scan on 2026-06-27.

## Repository State

- Branch: `codex/ecorex-v0.2.0`.
- Version metadata: `cli/VERSION`, `desktop/package.json`, `desktop/package-lock.json`, `common/ecorex_release_notes.py`, and public manifest report `0.2.3`.
- Local tag scan: `git tag --list *v0.2.3*` produced no matching output.
- Worktree: dirty, with many modified, deleted, and untracked files. Treat them as existing user/prior-run work; do not revert without explicit instruction.
- Existing v0.2.3 evidence: `docs/v0.2.3/` contains goal, acceptance checklist, review log, performance plan, regression pitfalls, and many artifacts.
- Concurrent v0.2.3 wrap-up thread `019f029a-d132-7b63-9c7b-d1a33b4cef16` was re-checked after the user reported that the other thread ended. Readback showed the thread is now idle, so the hard overlap brake is released. Keep normal dirty-worktree caution: do not revert unrelated v0.2.3 files and keep v0.2.4 evidence scoped.

## v0.2.3 Guardrails To Inherit

- RuntimeProjection and durable run events remain the state source of truth.
- Performance optimization must be measured and must not remove capabilities.
- Privacy scans must avoid raw prompts, OCR text, local paths, secrets, cookies, tokens, and full sensitive payloads.
- Session identity and request ownership must remain immutable and fail closed on mismatch.
- Final gates must include local/package/production or equivalent capability evidence where relevant.
- v0.2.4 scope update from the user: develop WebUI dual-end only. Treat shared renderer edits under `desktop/src` as WebUI source work, but do not start native desktop installer/DMG/NSIS/signing/notarization tasks.

## Skill And Mapping Surface

- Built-in EcoreX entries exist for `office-presentations`, `office-spreadsheets`, `office-documents`, `office-pdf`, and `image-generation`.
- Extra official entries exist in `skills/skills_config.json` for `Presentations`, `Spreadsheets`, `documents`, `pdf`, and `imagegen`.
- Loader precedence in code is builtin, extra, then custom. This supports a facade approach that keeps EcoreX names while adopting official workflow logic.
- Minimal debt found: `agent/skills/loader.py` comments/docstrings did not fully document the `extra` precedence layer. This was selected as an R24-00 low-risk fix.

## Runtime Surface

- `desktop/runtime-packs/capabilities.json` has `office-pdf` with `pypdf`, `pdfminer.six`, `python-docx`, `python-pptx`, `openpyxl`, `xlsxwriter`, and `markdownify`.
- `desktop/runtime-packs/core-requirements.txt` and `requirements.txt` include `rapidocr-onnxruntime`.
- `requirements.txt` and `desktop/runtime-packs/core-requirements.txt` also include `lark-oapi>=1.5.5`, but the user reports a live External Connections failure: correct Feishu App ID/Secret still shows `lark_oapi not installed`. R24-02A must verify the active WebUI Python environment and package/update path, not only the source requirement files.
- Existing office smoke checks old skill IDs and the `office-pdf` capability pack.
- Missing for v0.2.4: unified render tools, page/sheet/slide contact sheets, layout evidence, and a shared Office/PDF QA artifact schema.

## ImageGen Surface

- `agent/tools/imagegen/imagegen.py` wraps `skills/image-generation/scripts/generate.py --stdin`.
- `scripts/smoke-image-generation-tool-invocation.py` and `scripts/smoke-v023-imagegen-runtime-tool.py` cover text-to-image and edit paths against a fake GPT Image-compatible API.
- `agent/protocol/image_job_service.py` already supports image-job parallelism, OCR reuse/cache, cancellation, terminal cleanup, artifact redaction, and projection recovery.
- `skills/image-generation/scripts/generate.py` validates basic bytes, format, and dimensions before saving.
- Missing for v0.2.4: structural defect QA, visual semantic QA, reference fidelity QA, retry patching, and quality-preserving visual analysis acceleration.

## Session UI Surface

- `desktop/src/App.tsx` `renderSessionRow` now renders a leading visual only for running or unread rows.
- R24-03 keeps `ThinkingIndicator` for running rows and `session-unread-dot` for unread rows, but renders no robot/folder icon for normal project/general rows.
- Project rows and project picker icons remain out of R24-03 unless they are the session-row icon itself.

## Minimal Fix/Optimization Queue

- Fixed: skill loader precedence documentation now includes `extra` skills.
- Fixed: v0.2.4 trace files exist and the active goal ledger points to this run.
- Fixed locally: R24-03 session-row icon cleanup now has render-level browser smoke evidence under `docs/v0.2.4/artifacts/session-list-visual-cleanup-browser-smoke.json`.
- Fixed locally: R24-02A active WebUI runtime can import `lark_oapi` after installing it into the installed WebUI Python runtime; source/runtime-pack/package contracts now treat it as required for Feishu websocket readiness instead of an optional notice.
- Reuse existing v0.2.3 performance/privacy harness patterns for visual analysis acceleration instead of adding a parallel measurement system.
- Keep all following slices scoped to WebUI dual-end evidence and avoid native desktop packaging scope.
