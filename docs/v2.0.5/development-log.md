# e-Mate 2.0.5 development log

Last updated: 2026-08-13 (Asia/Shanghai)

This is the recovery ledger for the 2.0.5 development train. Record only
observed evidence. A pending test, intended fix, or narrower proxy is not a
pass.

## Baseline identity

| Field | Observed value | Evidence |
| --- | --- | --- |
| Source base | `63a591d81ddc38ab2793236943f559a02f25eee9` | `origin/codex/e-mate-2.0.4` and the clean 2.0.4 worktree resolved to the same commit |
| Development branch | `codex/e-mate-2.0.5` | Clean worktree created directly from the source base |
| Desktop production Feed | `2.0.4`, unsigned manual, `release-stable-adbf7fe8e3ce46dca448fdaa`, source `63a591d81ddc38ab2793236943f559a02f25eee9` | Public `download-index.json` and the exact feed-stage receipt; all three domestic download URLs returned HTTP 200 with manifest-sized bodies |
| Cloud public health | `2.0.4`, ready | `GET https://mvdcm.ecoremedia.net/ecorex-agent/admin/health/ready` returned HTTP 200, `X-ECoreX-Product-Version: 2.0.4`, and `{"status":"ready"}` |
| Cloud active release ID | Not independently re-read | The supplied SSH password no longer authenticated, so this ledger does not infer the server-side `active.json` identity from public health |
| GitHub 2.0.4 release | Draft targets source `63a591d81ddc38ab2793236943f559a02f25eee9`; tag ref still resolves to older `e2067edc9ffdaef26e0802454b6af09a4a8a89d1` | `gh release view v2.0.4` plus `git ls-remote`; the tag is not used as the 2.0.5 source base |

## Development rules

- Preserve CowAgent 2.1.5 data-plane behavior. Enterprise code may add only
  authentication, audit, managed model publication, and release publication.
- Discover with product code read-only. A reproducible finding receives one
  bug ID, one smallest regression, one shared-root-cause fix, and independent
  replay evidence.
- Record macOS and Windows evidence separately. Windows UI work runs through
  `win-codex`; do not infer Windows behavior from macOS.
- Ordinary fixes stop after the exact regression and affected subsystem check.
  Build and publication belong to the final frozen 2.0.5 train.

## Computer Use test matrix

Allowed status values: `pending`, `passed`, `failed`, `blocked`.

| Case ID | Platform | Scenario | Status | Bug ID | Tested commit | Evidence |
| --- | --- | --- | --- | --- | --- | --- |
| CU-MAC-OFFICE | macOS | Word, Excel, PowerPoint, PDF create/edit/open/preview | failed | `MAC-OFFICE-001` | exact 2.0.4 arm64 DMG `c9dc0022…` | Word create probe succeeded, but two authoring attempts raised `OfficeAuthoringContractError`; repair assigned separately |
| CU-MAC-FEISHU | macOS | Feishu receive, reply, file, image, card, cancel/retry | pending | — | — | — |
| CU-MAC-TENCENT-DOCS | macOS | Tencent Docs connect, read, create, edit, handoff | pending | — | — | — |
| CU-MAC-WECHAT | macOS | WeChat receive, reply, file, image, reconnect | pending | — | — | — |
| CU-MAC-DINGTALK | macOS | DingTalk receive, reply, file, image, reconnect | pending | — | — | — |
| CU-MAC-OTHER-CHANNELS | macOS | Remaining shipped external channel contracts and configured live paths | pending | — | — | — |
| CU-WIN-OFFICE | Windows | Word, Excel, PowerPoint, PDF create/edit/open/preview | pending | — | — | — |
| CU-WIN-FEISHU | Windows | Feishu receive, reply, file, image, card, cancel/retry | pending | — | — | — |
| CU-WIN-TENCENT-DOCS | Windows | Tencent Docs connect, read, create, edit, handoff | pending | — | — | — |
| CU-WIN-WECHAT | Windows | WeChat receive, reply, file, image, reconnect | pending | — | — | — |
| CU-WIN-DINGTALK | Windows | DingTalk receive, reply, file, image, reconnect | pending | — | — | — |
| CU-WIN-OTHER-CHANNELS | Windows | Remaining shipped external channel contracts and configured live paths | pending | — | — | — |

## Bug ledger

| Bug ID | Found by case | Source commit | Reproduction | Severity | Fix branch | Fix commit | Verification | Evidence |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `CU-205-STARTUP-001` | macOS fresh-profile startup | `63a591d81ddc38ab2793236943f559a02f25eee9` | Deterministic delayed-owner contract | P1 | `codex/e-mate-2.0.5` | `cf14a545` | Exact regression plus all 14 Electron shell contracts passed | Same-data-dir, same-identity receipt could be rejected while its HMAC endpoint was still starting; unknown and cross-identity listeners remain rejected |
| `CU-205-WIN-UPDATER-001` | Windows exact 2.0.4 installed update path | `63a591d81ddc38ab2793236943f559a02f25eee9` | Both production hosts returned 404 for `latest.yml` while the unsigned-manual gate deliberately omitted it | P1 | `codex/fix-windows-updater-205` | `c744813e` | Exact Feed red test, 16 Electron contracts, 13 deploy/rollback tests, and public download contracts passed | The single electron-updater feed now uses the domestic CDN; Windows metadata remains atomic while unsigned macOS stays manual |
| `CU-205-FILE-001` | `CU-MAC-OFFICE` | `63a591d81ddc38ab2793236943f559a02f25eee9` | `205-表格验收.xlsx` was created, but the final row was plain text with no open/preview action | P1 | `codex/205-artifact-file-open` | `5aed338a` | Exact Cow projection regression, secure open/materialize checks, 18 Web Artifact contracts, and TypeScript passed | Cow path metadata is now admitted once into the existing account/thread-scoped Artifact store; office cards open natively while PDF/image cards retain in-app preview |
| `CU-205-OCR-001` | macOS exact 2.0.4 OCR cold profile | `63a591d81ddc38ab2793236943f559a02f25eee9` | First call failed after 31.90s with `DependencyPackProcessError`; the same attachment succeeded on the second call in 15.45s | P1 | `codex/fix-ocr-cold-timeout-205` | `2436e951` | Exact cold-deadline regression plus all 10 dependency Pack process tests passed | One verified OCR Pack process now receives a 38s hard wall-clock bound: 30s operation plus 8s Pack-Python startup; there is no retry or fallback execution path |
| `CU-205-CANCEL-001` | Cow Runtime robustness matrix | `58fd72e4fb845619238d21cb905df90ce5e25b36` | A silent/long Gateway stream remained open after the user cancelled because Cow's synchronous bridge blocked indefinitely on its result queue | P1 | `codex/fix-runtime-robustness-205` | Pending | Exact text/vision cancellation regressions plus the ten-test Cow history/attachment/subagent lifecycle suite passed | The turn's existing cancel Event now reaches every root/forked Cow Gateway model; cancellation closes the one in-flight future without retrying or repeating tools |
| `MAC-OFFICE-001` | `CU-MAC-OFFICE` | `63a591d81ddc38ab2793236943f559a02f25eee9` | Word create authoring inputs failed twice after the initial create probe | Pending triage | Assigned separately | — | Pending | Exact 2.0.4 arm64 DMG SHA-256 `c9dc0022a831f053081f3c0776f1480c2939811ec1ffde2d7f9a7996e4fa2582`; ConversationStore retained the complete tool chain |
| `CU-205-PDF-PREVIEW-001` | Windows exact 2.0.4 Office baseline | `63a591d81ddc38ab2793236943f559a02f25eee9` | `office_pdf` create/edit/inspect passed; the model-visible `render_preview` action deterministically returned `OfficePdfRuntimeError` | P1 | `codex/fix-pdf-preview-capability` | `97df920a` | Exact schema regression plus verified Pack create/edit/inspect regression passed | The exact Office Pack exports only `office.formats`; neither its Windows archive nor Core carries PyMuPDF, Poppler, or LibreOffice. Desktop preview already uses `ArtifactPreviewDialog` and the Runtime artifact preview Blob. |

Use `CU-205-<AREA>-NNN` for product findings. Do not allocate an ID for an
environment failure until the product failure reproduces from known state.

## Development entries

### 2026-08-13 BASE-205-001

- Created the clean 2.0.5 branch directly from exact 2.0.4 source
  `63a591d81ddc38ab2793236943f559a02f25eee9`; no dirty 2.0.4 workspace files
  were copied.
- Advanced the Python source of truth to `2.0.5`, synchronized the desktop
  package and lock with npm's native version command, and updated the release
  replica example contract.
- Red check: after advancing only the Python source, the focused version test
  failed on the stale `2.0.4` contract as expected.
- Green check: `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest -q
  tests/v1/test_version_source.py
  tests/v1/test_control_plane_release_replica_service.py::test_production_nginx_and_systemd_keep_replica_boundary_narrow`
  passed `3` tests; `git diff --check` passed. The warnings were existing
  environment/mark warnings and did not affect the selected checks.

### 2026-08-13 CU-205-STARTUP-001

- The exact 2.0.4 arm64 DMG (`c9dc0022a831f053081f3c0776f1480c2939811ec1ffde2d7f9a7996e4fa2582`)
  eventually exposed the expected Runtime owner HMAC and immutable identity,
  while Electron had already shown the generic Runtime startup error.
- Root cause: `BackendManager` probed owner proof only once. If the fixed
  loopback port was already listening while the same data directory's exact
  Runtime was still bringing up its owner endpoint, it was misclassified as
  an unknown process. `main.cjs` discarded that first exception and showed
  only the generic dialog.
- Red check: the delayed-owner regression failed with `Loopback port ... is
  occupied by a process not owned by e-Mate.` The fix retries for at most 15
  seconds only when the local receipt has the same immutable Runtime identity;
  adoption still requires valid HMAC proof. No nonce or data path is logged.
- Green checks: the exact regression passed in 3.1 seconds, then the affected
  Electron shell contract passed all 14 tests in 23.2 seconds. A controlled
  exact-only fresh launch separately reached listener readiness at about 55
  seconds and owner HTTP 204 at about 61 seconds, then showed the normal login
  UI; no old application was launched for repair verification.

### 2026-08-13 MAC-OFFICE-001

- On the same exact 2.0.4 arm64 DMG, the first Word create probe succeeded.
  Supplying sections, title, and output path then raised
  `OfficeAuthoringContractError` twice.
- The model next used `ls`, `search_files`, and `bash` to inspect the workspace
  and entered a loop; the user stopped the run. `ConversationStore` retained
  the complete user request and tool chain.
- This entry records evidence only. Office authoring repair is assigned to an
  independent agent and is not part of `CU-205-STARTUP-001`.

### 2026-08-13 CHANNEL-205-001

- Integrated the externally verified Cow channel sequence without reordering:
  contract tests `c1b4916f` -> `a820815b`, Weixin QR surface `5b145dee` ->
  `256787ab`, then expired authorization re-begin `d86d74cd` -> `57fa6245`.
- The earlier `web.py` dependency finding is withdrawn: CowAgent `0.76` exists
  on PyPI and the v1 lock at `0.61` is valid. No product or lock change was
  made for that report.
- Seven failures in the retired adapters suite are recorded as legacy-test
  debt outside the product hot path. The old adapters were not restored and
  do not replace the Cow channel runtime.
- Post-integration, the four Weixin QR/re-begin regressions plus data-plane
  admission and external-channel matrix passed `15` focused tests. A broader
  local-toolchain run passed `24` and failed only the constructor inventory
  because that local Python closure lacks the declared `web.py` package; this
  is environment evidence, not a reason to change the valid v1 lock.

### 2026-08-13 CU-205-PDF-PREVIEW-001

- On the exact 2.0.4 Windows installer, `office_pdf` create, replacement edit,
  and inspect succeeded, but the same public schema advertised
  `render_preview`; calling it failed through the legacy
  `common.office_pdf_runtime` path with `OfficePdfRuntimeError`.
- Artifact shape settled the boundary: the signed Office Pack manifest exports
  only `office.formats`, and its Windows archive plus Core contain no PyMuPDF,
  Poppler, or LibreOffice renderer. The desktop already previews the verified
  artifact Blob inside `ArtifactPreviewDialog`, so no second model-side PDF
  renderer or host fallback was added.
- Root cause: the four public Cow Office tools shared a stale model schema and
  execution branch for legacy analyze/render/quality/compare operations that
  were never transported by the verified Pack. Fix `97df920a` deletes those
  branches and exposes only `probe/status/create/edit/inspect`, matching the
  Pack and the four built-in Office skill instructions.
- Red check: the new schema regression failed because five unsupported actions
  remained visible. Green check:
  `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest -q
  tests/test_v024_skill_tool_exposure.py::test_v025_public_office_schema_matches_verified_formats_pack
  tests/test_v024_skill_tool_exposure.py::test_v024_public_cow_office_tools_create_edit_and_emit_artifacts`
  passed `2` tests; `git diff --check` passed. No App, package, signer, or
  deployment path was started.

### 2026-08-13 CU-205-FILE-001

- Exact 2.0.4 Computer Use reproduced an XLSX that existed under the Cow
  workspace while the assistant rendered only `下载 205-表格验收.xlsx`; the AX
  tree exposed text only, and clicking did not open WPS or an in-app preview.
- Root cause: the Cow executor emitted valid local file metadata, but the
  Runtime stored that legacy dictionary directly as an Artifact Item. The
  desktop correctly rejected it because it was not an `ArtifactProjection`.
- Fix `5aed338a` translates that one Cow event at the existing Runtime adapter:
  workspace outputs are read into the immutable Artifact CAS once, scoped to
  the authenticated account/thread/turn, and projected through the existing
  item/list/action contracts. No second file store or path-supplied open route
  was added. DOCX/XLSX/PPTX cards use the existing secure native `open` action;
  PDF and image cards keep authenticated in-app preview.
- Red check: the Cow office regression failed with `KeyError: 'artifact'`, and
  the UI regression failed because `artifactPrimaryAction` did not exist.
  Green checks: the exact Cow regression passed; the three focused Artifact
  ownership/open/API checks passed; `artifactActions` plus the preview contract
  passed `18`; TypeScript and `git diff --check` passed.
- A broader Cow contract file passed `20` product tests and failed only its
  unrelated package-closure fixture because this local validation environment
  could not resolve the declared `regex` distribution file. No package,
  installer, feed, or deployment was changed for this development fix.

### 2026-08-13 CU-205-OCR-001

- On the exact 2.0.4 macOS arm64 DMG, OCR of one attachment failed on its
  first cold call after 31.90 seconds with `DependencyPackProcessError`; the
  unchanged second call succeeded in 15.45 seconds. This isolated the failure
  to cold process startup rather than image input or OCR output handling.
- Root cause: Cow `OcrTool` correctly used the verified `ocr.extract` Pack,
  but the OCR adapter and shared dependency-process boundary collapsed ONNX
  model loading, Pack-Python startup, and inference into the same 30-second
  wall-clock ceiling. The shared boundary already intended an 8-second
  interpreter allowance, but its own 30-second cap discarded that allowance.
- Fix `2436e951` keeps the single Cow -> verified Pack path and one execution:
  OCR receives the existing 30-second operation budget, while the shared
  process supervisor applies a hard 38-second total ceiling. Pack exceptions
  still fail closed; there is no retry, host OCR fallback, or duplicate call.
- Red check: the cold-worker regression observed a 30.0-second deadline and
  failed its measured `31.9 < timeout <= 38.0` contract. Green checks: that
  exact regression passed, then the complete affected
  `tests/v1/test_dependency_pack_process.py` suite passed all `10` tests;
  `git diff --check` passed. No App, package, feed, or deployment was run.

### 2026-08-13 CU-205-WIN-UPDATER-001

- Exact 2.0.4 configured electron-updater against the `mvdcm` generic feed,
  while the production `latest.yml` route was 404 on both `mvdcm` and `dl`.
  The installer could therefore never discover an installed Windows upgrade.
- Root cause was the shared release gate: `--unsigned-manual` incorrectly
  removed Windows `latest.yml` together with unsigned macOS metadata, and the
  matching nginx/deploy contracts required that Windows pointer to remain 404.
- Fix `c744813e` keeps the existing electron-updater and one atomic candidate.
  Windows `latest.yml`, installer, and blockmap are verified against the same
  handoff and recorded in the same Feed receipt; only `latest-mac.yml` remains
  absent. The updater, electron-builder publish config, download index, and
  public download page now use `https://dl.ecoremedia.net/e-mate/update/`.
  GitHub and GH proxy download branches were removed from the public page.
- Red checks failed on the missing Windows pointer, stale updater URL, and
  stale download-index origin. Green checks: the exact Feed regression passed;
  all `16` Electron shell/update contracts passed; the complete deploy,
  rollback, and compensation suite passed `13`; the affected public download
  checks passed. `git diff --check` passed. No package, signer, deployment, or
  production mutation was started.

### 2026-08-13 CU-205-CANCEL-001

- The Cow robustness matrix isolated a cancellation gap without any provider,
  network, or account request: after one streamed delta, a silent Gateway
  generator stayed open when the consumer set the turn cancel Event.
- Root cause: `AgentTurnWorker` passed that Event to Cow's agent loop, but not
  to `_CowGatewayModel`; its synchronous bridge could therefore block forever
  on `Queue.get()` and leave the async Gateway request consuming a connection
  and Tokens. The same Event now reaches the root model and all forks, the
  bridge checks it every 100ms, cancels its one in-flight future, and raises
  Cow's existing `AgentCancelledError`. No retry or duplicate tool call was
  added.
- The matrix also proves a provider-failed prompt remains in durable Cow
  history, and switching the next turn's model preserves both image and office
  Artifact references. Existing Runtime `client_message_id` coverage remains
  the duplicate-submit authority. A repeated provider `tool_call_id` now reuses
  its first result only when tool name and arguments match; conflicting reuse
  fails without execution. Existing completed-tool-chain coverage proves a
  provider failure after a tool does not erase its fact.
- Green checks: the exact text/vision cancellation, duplicate-tool, and
  model-switch tests passed; the affected Cow
  history/attachment/subagent lifecycle suite passed all `10`
  tests; four adjacent Cow tool-round/fallback, Runtime idempotency, and Chat
  handoff tests passed. `git diff --check` passed. No App, package, feed,
  deployment, or external request was run.
