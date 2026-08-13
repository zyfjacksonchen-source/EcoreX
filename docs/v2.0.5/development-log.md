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

The frozen macOS and Windows candidates use one fixed acceptance contract:

- invoke each Cow Hard19 tool from the real desktop UI and retain the
  `tool_call -> executor -> tool_result -> terminal-visible -> next-turn`
  evidence chain; a catalog/schema probe or model-only answer is not a pass;
- exercise complex cross-tool and cross-turn chains, including model/thread
  switching, context continuation, failure recovery, and stateful Browser;
- replay every behavior changed in the 2.0.5 train against the same immutable
  candidate bytes; and
- verify Office4 separately from Hard19 so extra tools never hide a missing
  first-party capability.

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
| `CU-205-CANCEL-001` | Cow Runtime robustness matrix | `58fd72e4fb845619238d21cb905df90ce5e25b36` | A silent/long Gateway stream remained open after the user cancelled because Cow's synchronous bridge blocked indefinitely on its result queue | P1 | `codex/fix-runtime-robustness-205` | `80a9ee1f` (integrated as `3dfd65f8`) | Exact text/vision cancellation regressions plus the ten-test Cow history/attachment/subagent lifecycle suite passed | The turn's existing cancel Event now reaches every root/forked Cow Gateway model; cancellation closes the one in-flight future without retrying or repeating tools |
| `MAC-OFFICE-001` | `CU-MAC-OFFICE` | `63a591d81ddc38ab2793236943f559a02f25eee9` | Word create authoring inputs failed twice after the initial create probe | P1 | `codex/205-office-authoring-contract` | `1feef175` (integrated as `28fb7017`) | Exact heading-only/level create-edit-inspect regression, Runtime Office suite, and verified Pack process contract passed; final 2.0.5 UI replay remains pending | Exact 2.0.4 arm64 DMG SHA-256 `c9dc0022a831f053081f3c0776f1480c2939811ec1ffde2d7f9a7996e4fa2582`; ConversationStore retained the complete tool chain |
| `CU-205-PDF-PREVIEW-001` | Windows exact 2.0.4 Office baseline | `63a591d81ddc38ab2793236943f559a02f25eee9` | `office_pdf` create/edit/inspect passed; the model-visible `render_preview` action deterministically returned `OfficePdfRuntimeError` | P1 | `codex/fix-pdf-preview-capability` | `97df920a` | Exact schema regression plus verified Pack create/edit/inspect regression passed | The exact Office Pack exports only `office.formats`; neither its Windows archive nor Core carries PyMuPDF, Poppler, or LibreOffice. Desktop preview already uses `ArtifactPreviewDialog` and the Runtime artifact preview Blob. |
| `CU-205-MODEL-SWITCH-001` | macOS exact 2.0.4 model-switch continuity | `63a591d81ddc38ab2793236943f559a02f25eee9` | The same three-image conversation succeeded on Luna, then DeepSeek and Doubao both failed before any tool call with `provider_rejected` | P1 | `codex/fix-model-switch-image-context-205` | `f693156a` (integrated as `a07bc35b`) | Exact history-envelope regression plus affected Chat Completions, multimodal, and image fallback checks passed `19` | Both providers accepted `system` and rejected `developer` in bounded direct protocol probes; all three Artifact URLs and complete Cow history remain present. |
| `CU-205-TIMELINE-BOTTOM-001` | macOS exact 2.0.4 model-switch UI | `63a591d81ddc38ab2793236943f559a02f25eee9` | After model-switch reprojection the composer and real bottom were visible, but the floating `回到底部` button remained | P1 | `codex/fix-model-switch-image-context-205` | `56f61374` (integrated as `c721ecdb`) | Timeline plus interaction renderer contracts passed `23` | Virtuoso height changes now remeasure the same real scroll parent; an active upward scroll remains paused and is not forced to the bottom. |
| `CU-205-IMAGE-BATCH-SESSION-001` | macOS exact 2.0.4 three-image edit | `63a591d81ddc38ab2793236943f559a02f25eee9` | One `imagegen(tasks=[3])` returned two Artifacts and failed item 3 with `managed_image_session_changed` after 328.32 seconds | P1 | `codex/fix-model-switch-image-context-205` | `67496696` | Historical evidence only; superseded by `CU-205-IMAGEGEN-CODEX-SINGLE-001` | The 2.0.5 public contract removed `tasks`; this batch implementation is not a current capability. |
| `CU-205-IMAGEGEN-CODEX-SINGLE-001` | macOS 2.0.4 screenshot plus source-level replay | `00a765a6` | A completed batch tool card projected seven synthetic failed gallery slots and invited a second paid request despite no successful Artifact facts | P1 | `codex/fix-image-batch-false-success-205` | `0cf95cd1` | Exact and affected Python checks passed `11`; Artifact-only renderer checks passed `4`; TypeScript and Python compile passed | Public imagegen now accepts one prompt/reference set and returns at most one real Artifact. Multiple assets are independent Cow tool calls; failed calls cannot create empty gallery slots. |
| `CU-205-SETTINGS-INSET-001` | macOS exact 2.0.4 Settings | `63a591d81ddc38ab2793236943f559a02f25eee9` | Opening Settings consistently placed its title under the native red/yellow/green controls | P1 | `codex/fix-settings-inset-password-205` | `d3ad5f3a` | Exact renderer red/green plus `17` affected layout/product contracts passed | The full-screen Settings workspace omitted the macOS native-chrome inset; the shared header now reuses the existing Darwin-only `76px` safe area and Windows receives no offset. |

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
### 2026-08-13 CU-205-MODEL-SWITCH-001

- Exact 2.0.4 thread `thr_01KZWN2HKGPMWF2JCE8Y0W5P26` retained the complete
  three-image Luna turn. Switching to `ecorex-deepseek-v4-pro` and then
  `ecorex-doubao-seed-2.0-pro` failed in zero to one second, before a tool call,
  retry, or side effect. Switching back to Luna recovered the three original
  Artifact IDs and made one batch edit call, proving history storage itself was
  intact.
- Root cause: the shared Chat Completions adapter projected Cow instructions as
  role `developer`. Bounded calls through the active production provider
  configurations returned HTTP 200 for `system`, while DeepSeek returned HTTP
  400 with its accepted-role list and Doubao returned HTTP 400
  `InvalidParameter` for `developer`. Fix `f693156a` uses the providers'
  supported `system` role without dropping, truncating, or rewriting Cow
  history, image URLs, or tool schemas.
- Red check reproduced the four-message envelope with all three Artifact URLs;
  both providers rejected the old role. Green checks covered the exact
  envelope, both affected provider adapters, multimodal history, and the
  existing controlled image-model fallback rules: `19 passed` in 6.05 seconds.

### 2026-08-13 CU-205-TIMELINE-BOTTOM-001

- Root cause: `Timeline.tsx` synchronized follow state only on native `scroll`.
  A model-switch separator or message reprojection can change Virtuoso's total
  height without emitting that event, leaving `showJumpToLatest` stale even
  when the real scroll parent is already at the bottom.
- Fix `56f61374` reuses the existing bottom threshold and state machine from
  `totalListHeightChanged`. A layout change clears the stale pause only when
  the measured parent is at the bottom; a user who remains above the threshold
  stays paused, with no timer or forced scroll added.
- The new exact renderer regression and the affected interaction contract both
  passed: `23` Node tests in 0.14 seconds; `git diff --check` passed.

### 2026-08-13 CU-205-IMAGE-BATCH-SESSION-001

> Historical evidence only. The current 2.0.5 imagegen contract removes the
> `tasks` batch route; `CU-205-IMAGEGEN-CODEX-SINGLE-001` supersedes this path.

- Exact evidence: the Luna recovery call started once at 12:22:38 with three
  edit tasks. Two new Artifacts completed; item 3 failed with
  `managed_image_session_changed`, and the batch settled `2/3` after 328.32
  seconds. The managed-session audit shows a normal signed credential refresh
  at 12:28:05 from generation `4`, revision `1000441` to generation `5`,
  revision `1000442`; account, organization, roles, model allowlist, quota,
  admin denies, and lease expiry were unchanged.
- Root cause: each child fenced the credential generation, lease digest, and
  revision instead of the stable signed session authority. Fix `67496696`
  admits one batch under one logical session scope, keeps Turn image-model and
  child idempotency identities frozen, and permits only a policy-equivalent
  credential rotation. Logout, account/policy change, or authentication loss
  aborts outstanding siblings; completed children are never submitted again.
- The smallest red check changed the credential revision during one admitted
  execution and reproduced `managed_image_session_changed`. Green checks cover
  equivalent refresh, cross-account fencing, one-scope three-child admission,
  cancellation on true session change, ordered partial results, and recovery:
  both affected test files passed `24` tests in 6.95 seconds.
- Image routing is independently green, not inferred from the public label.
  Production usage facts bind single generate `imgjob_d70f…`, single edit
  `imgjob_b502…`, and the successful three-item batch jobs `imgjob_238b…`,
  `imgjob_7729…`, `imgjob_89bd…` to
  `actual_model_id=gpt-image-2-pro` and `fallback_used=false`. The public/local
  result may remain `gpt-image-2` / e-Mate Image 2. The original batch made one
  tool call at 12:11:33, completed all three independent Artifacts at 12:15:55
  in 261.04 seconds, and made no second tool call or retry.

### 2026-08-13 CU-205-IMAGEGEN-CODEX-SINGLE-001

- The screenshot showed a completed imagegen tool row followed by seven
  synthetic “图片未完成” gallery slots. The supplied isolated Runtime database
  and log did not contain the named conversation, so no provider state was
  inferred and no replacement image request was sent.
- Upstream CowAgent exact `e3ac1b952500f60934862c6bf0bd0de91b415ed8`
  has one prompt-based image script and no native `tasks`, batch executor,
  partial-failure, or concurrency contract. The selected Codex-style boundary
  is one prompt plus optional references per call and one generated result;
  multiple assets require separate imagegen calls.
- Fix `0cf95cd1` removes `tasks` from both model-visible schemas and deletes the
  Runtime batch executor/events. A stale `tasks` payload fails before provider
  execution or Artifact publication. The existing Cow loop executes multiple
  imagegen calls independently, preserving each call ID and its existing
  idempotent publication boundary; `gpt-image-2-pro` routing is unchanged.
- Renderer galleries now derive only from ready image Artifact facts in the
  same Turn. Three successes form a three-image gallery; two successes and one
  failed call form a two-image gallery; a failed call without an Artifact forms
  no gallery. Retouch results and single-image cards keep their existing paths.
  Old `image_batch` metadata is used only to order already-written successful
  Artifacts in Cow history and cannot enter a new call.
- The public/local provider boundary also keeps only the first image if a
  provider unexpectedly returns several results. Exact and affected Python
  checks passed `11`, the renderer gallery checks passed `4`, TypeScript and
  Python compile passed, and no live provider request, package build, or
  publication was performed.

### 2026-08-13 CU-205-SETTINGS-INSET-001

- Exact 2.0.4 Computer Use consistently showed the native macOS traffic lights
  overlapping the Settings title. Root cause: the fixed, full-window Settings
  workspace replaced the normal sidebar/header chrome but its shared header had
  no Darwin safe inset.
- Fix `d3ad5f3a` adds the same `76px` macOS-only inset already used by the
  native sidebar brand to `.ex-settings-page-header`. It does not special-case
  the title and introduces no Windows offset or JavaScript layout branch.
- Red check: the renderer contract failed because no Darwin Settings-header
  inset existed. Green checks: the exact product-language contract passed all
  `14` tests; the affected product and login/layout pair passed `17` tests.
- The same change set tightened the existing password acceptance contract. An
  isolated account exercised the real HTTPS account route: the old password
  stopped authenticating, the new password authenticated, all previous access
  was revoked, credential version advanced `1→2` while the stable user policy
  revision stayed `1`, and one `account.password.changed` audit entry preserved
  chain integrity. Neither password appeared in the SQLite bytes. The complete
  affected device-identity test file passed `9` tests.
- The renderer contract proves the success message and reload occur only after
  the authenticated Runtime receipt; the UI does not manufacture success.
  Production account credentials were not touched. The frozen 2.0.5 candidate
  must perform one user-handoff password change and immediate restoration of
  the original password, because Computer Use must not type credentials.

### 2026-08-13 CU-205-COW-MEMORY-DREAM-001

- CowAgent 2.1.5 exact `e3ac1b952500f60934862c6bf0bd0de91b415ed8`
  uses the local `self_evolution_enabled` switch, enabled by default. Its idle
  learner scans every 60 seconds after 10 idle minutes and six user turns (or
  80% context pressure); its nightly timer runs at a random local time from
  23:50 through 23:55. The same switch now controls both paths in e-Mate.
- Root cause: e-Mate exposed neither Cow's switch nor its config API, the direct
  Runtime rebuilt agents without carrying evolution counters and never started
  or notified Cow's idle trigger, and nightly dream ignored the switch. A
  legacy noninteractive permission broker additionally rejected Cow's local
  memory writes. Fix `386ef7dc` persists the switch atomically in the local Cow
  `config.json`, binds it on Runtime restart, restores direct-worker trigger
  state, and keeps memory reads/writes confined to the active Cow workspace.
  The enterprise plane has no hide, deny, or remote-control path for it.
- Controlled local tests used fake models and a shortened idle threshold. With
  the switch on, the real evolution executor produced
  `memory/evolution/YYYY-MM-DD.md`; nightly consolidation produced `MEMORY.md`
  and `memory/dreams/YYYY-MM-DD.md`. Turning it off stopped both paths without
  a write, turning it back on worked without restart, turning it off again
  stopped subsequent writes, and a new Runtime composition restored the saved
  off state. No model provider, network, production account, package, feed, or
  deployment was used.
- Green checks: the focused memory/API/runtime suite passed `10`; Cow's original
  evolution scenario harness passed all `13` cases plus undo; the Settings
  contract and TypeScript check passed; the affected direct Cow spine suite
  passed `19` with two package-only cases excluded. `git diff --check` passed.

### 2026-08-13 CU-205-TENCENT-DOCS-001

- Exact 2.0.4 Computer Use showed the stable Tencent Docs card as
  `adapter_not_installed` while the same Capability Center already exposed the
  native Cow Remote MCP self-service. The managed Connector projection was a
  stale second entrance, not a missing Runtime component.
- Fix `3e44c4ad` keeps one execution path. The stable card now opens the generic
  `UserMCPPanel` with the official `https://docs.qq.com/openapi/mcp` Bearer
  preset. An existing server at that endpoint is edited in place; otherwise
  the same Cow MCP create form opens. Save, secret redaction, real test,
  enable/disable, delete, dynamic ToolManager discovery, and execution remain
  owned by `CowMCPSettingsService` and the current workspace `mcp.json`.
- The exact GA regression first reproduced the misleading missing-component
  state, then passed the card-to-preset, secret non-echo, real-test, enable,
  disable, and credential-delete flow. The adjacent generic Remote MCP E2E
  passed unchanged. The native ToolManager regression now makes two sequential
  calls through one loaded MCP Runtime; both focused Python checks passed.
  The lazy-feature contract passed all `12` tests, Vite compiled successfully,
  and `git diff --check` passed. Repository-wide TypeScript remains blocked by
  the pre-existing `Timeline.tsx` scroll-listener signature errors at lines
  684 and 696; none of the changed Tencent Docs/MCP files emitted a type error.
- No managed Tencent adapter, second executor, connector-vault write,
  enterprise policy, real token, external request, package, or deployment was
  added or run.

### 2026-08-13 CU-205-SCHEDULER-SKILLS-001

- Root cause: the capability-center Extension authority could stage and enable
  a local or Hub Skill, but Cow's `SkillManager` scanned only its built-in and
  workspace directories. The UI could therefore show a Skill as enabled while
  the model could not discover it. The local upload API also stopped at
  `staged`. Fix `6d58f82a` contributes only enabled, ready, digest-reverified CAS
  roots to Cow's original refresh; local upload now activates the same
  Extension revision. Disabling removes it on the next Cow refresh. Pack
  switches remain separate and cannot hide or rewrite Cow first-party tools.
- A two-principal loopback test uploaded from account A, discovered and
  downloaded from account B, checked the immutable digest, installed into B's
  independent `runtime.db` and CAS, and proved Cow could discover and read the
  complete `SKILL.md`. Duplicate slug/version with changed bytes was rejected;
  account-bound signed intents, expiry, path traversal, executable/symlink
  input, CAS tampering, signature evidence, timeout, idempotency conflict, and
  offline-upgrade preservation all stayed fail-closed. No production identity
  or external message was used.
- Scheduler now exposes Cow-local `edit` and `run_now` through the existing
  single workspace service. Edits preserve the captured delivery target;
  run-now records the attempt without changing the next scheduled time. The
  task store serializes read-modify-write, uses atomic replacement, and repairs
  an interrupted primary from its last-good backup instead of silently erasing
  tasks. Due failures retain their scheduled time for the next bounded tick;
  success advances once; restart and delete remain durable. The desktop change
  adds only two instruction-prefill cards and no scheduler state machine.
- Green checks after rebasing onto `bb744aa3`: the exact Scheduler/Skill suite
  and affected security/Hub/Cow boundary set passed `29`; existing Skill
  governance and Scheduler singleton/isolation checks passed `11`; TypeScript
  contracts and `tsc --noEmit` passed; `git diff --check` passed. The complete
  23-tool initializer check could not run in this local Python toolchain because
  its pre-existing `regex` distribution is absent (`search_files` is the sole
  missing load); the focused switch test proves the loaded tool set is byte-for-
  byte unchanged and retains all four Office tools. No package, feed, deploy, or
  external network path was started.

### 2026-08-13 CU-205-USAGE-GENERATION-001 — blocked before edit

- Requested outcome: retain historical EcoreX records while adding an
  `e-Mate / EcoreX` filter and computing task, completion, and Token summaries
  independently for both product generations.
- The production Usage panel is protected by HTTP Basic Authentication. The
  approved in-app Browser refused this origin at its authentication boundary,
  so no panel page or `/api/data` payload was read. Credentials were not put in
  a URL, shell command, repository file, or log, and no production mutation was
  attempted.
- The current source projection is insufficient to classify historical tasks
  without guessing. A task exposes user/date/request/session/status/duration,
  scenario/tool and Token fields, but no product generation, client version,
  Runtime version, or source-release identity. `sync_events` and managed
  `gateway_requests` are merged by request key, so table presence is not a
  stable product-generation field. Legacy Token baselines and managed Gateway
  facts can be distinguished by `source_service`, but that does not classify
  task completion records.
- Per the production-data rule, implementation is blocked rather than using a
  date cutoff, task label, source string, or missing-Gateway heuristic. The
  smallest unblocking evidence is a redacted real `/api/data` task/usage field
  shape containing an authoritative generation/version marker, or a producer
  change that records that marker for new facts plus an explicit mapping for
  retained legacy records. Until then the existing data and service remain
  unchanged.

### 2026-08-13 CU-205-USAGE-GENERATION-001 — producer contract resolved

- Source/schema reconciliation confirmed there was no authoritative product
  generation in `sync_events`, `usage_events`, `gateway_requests`, or the task
  projection. `source_service` is a Token-fact origin only and is not used to
  classify tasks. The repair therefore records immutable
  `product_generation` and `product_version` at the two new-fact authorities:
  Gateway requests and provider-usage facts.
- Gateway v4→v5 and Admin Management v7→v8 are transactional, fingerprinted
  migrations. Existing rows are retained byte-for-byte and gain the explicit
  compatibility values `ecorex` / `legacy`; newly admitted 2.0.5 facts write
  `emate` plus the exact package version. Identity triggers prevent later
  generation/version rewrites. No date, task-name, table-presence, or Token
  source heuristic is used.
- `/api/data` accepts only `all`, `emate`, or `ecorex`. Task and Token rows use
  the same filter and the all-view breakdown equals the two independent
  generations without double-counting a request. The UI exposes “全部”, “新版
  e-Mate”, and the honest compatibility label “旧版 EcoreX / 历史未标识”. The
  selector is sent only to the data projection; Runtime-audit details are not
  falsely presented as generation-filtered.
- The task JSON contract is locked: identity/status/scenario/version fields are
  strings; booleans remain booleans; event/Token counters remain integers; and
  duration remains numeric or null. Exact v4→v5 and v7→v8 fixtures prove old
  task/usage rows survive with the compatibility identity while new Gateway
  and provider facts persist `emate` and the exact version.
- Green evidence: the four affected test files passed `71` tests; the focused
  generation/migration/producer set passed `17`; JavaScript syntax and
  `git diff --check` passed. No production credential, database, service,
  package, feed, deployment, or external request was used.

### 2026-08-13 CU-205-STARTUP-PARALLEL-001

- Exact 2.0.4 evidence separated Electron process start, startup-page load,
  Runtime spawn, and Runtime readiness. The Runtime itself reached its first
  scheduler log about 11 seconds after spawn; the longer visible delay came
  before spawn because `launch()` awaited the startup-page Renderer first.
- Fix `c71161c3` starts the existing owner-proofed BackendManager promise before
  awaiting the local startup page, then awaits that same promise. Runtime
  ownership, failure dialog, retry, and shutdown semantics are unchanged.
- The ordering regression failed before the fix and passed after it; the
  affected Electron suite passed `19/19`. The 2.0.4 App was not restarted; the
  final wall-clock result remains a frozen 2.0.5 candidate acceptance gate.

### 2026-08-13 CU-205-MAC-ICON-001

- The existing macOS ICNS used an opaque 1024x1024 square, so Launchpad rendered
  it larger and squarer than native macOS icons. The e-Mate robot artwork and
  orange/black brand pixels were not regenerated.
- The ICNS now uses the macOS 824x824 live area centered on a transparent
  1024x1024 canvas with a rounded-square silhouette. Windows ICO bytes remain
  unchanged.
- The focused asset check proves transparent corners, the exact live-area
  bounds, an opaque center, and sub-one-channel mean drift against the existing
  Windows brand artwork after normalization.
### 2026-08-13 CU-205-WINDOWS-READINESS-001

- Windows source readiness used temporary source checkpoint `89862285921e`
  only; it is not the frozen 2.0.5 identity. The existing isolated worktree was
  fast-forwarded by a complete Git bundle and never launched. The host retains
  an installed 2.0.4 application for the final Session 1 upgrade replay.
- A checksum-verified official Node `22.23.1` archive (SHA-256
  `7df0bc9375723f4a86b3aa1b7cc73342423d9677a8df4538aca31a049e309c29`)
  provides npm `10.9.8`. uv's cached CPython `3.11.9` created an acceptance-only
  virtual environment, and the hash-locked dev profile synchronized all 66
  packages. HOME, profile, app-data, data, and temp paths stayed under the
  disposable acceptance root; no user configuration or scheduled task changed.
- Source checks exposed three stale release-test boundaries and one real
  Windows release-store defect. The package surface omitted its already
  packaged `models.model_capabilities` dependency; the channel constructor
  fixture blocked Windows' loopback `socketpair`; the release receipt still
  tested the legacy enterprise catalog instead of Cow's 19 hard tools plus the
  four Office tools; and `ReleaseRunStore` unconditionally called unavailable
  `os.fchmod`, then could not close/delete the Windows temporary file.
- Fixes `e01eed93`, `2581ba03`, `962dc44e`, and `bc724a80` respectively correct
  those shared boundaries. The final receipt now requires exact Cow Hard19 plus
  Office4, all with completed visible terminal states; enterprise Connector
  policy is not substituted for Cow tools. POSIX retains descriptor chmod while
  Windows uses its native inherited ACL. The complete receipt suite passed `7`
  tests on Windows and `7` on macOS; the packaging suite passed `4`, and the
  channel regression passed on both platforms.
- On the temporary checkpoint, the selected Python source/package-shape sets
  passed `32` and `31` cases apart from the two corrected test defects. Exact
  Node type-check passed; Electron/updater/staging-shape checks passed `21` with
  one POSIX-permission case correctly skipped on Windows, and Runtime codegen
  passed both cases after binding the isolated Python executable. Browser
  state/search/fetch, OCR, Office Artifact/schema, Scheduler/Skill, channel,
  Feed, and Windows postsign paths were included in the passing source set.
- No candidate was built. Final proof still requires the one frozen source
  SHA, its immutable Windows Bootstrap and release directory, installer plus
  `latest.yml` and blockmap, and a Session 1 installed 2.0.4 to 2.0.5 upgrade
  followed by real Hard19+Office4, Browser, OCR, Office, Scheduler, Skill, and
  channel replay. Production `latest.yml` remains intentionally untouched until
  same-byte Feed publication.

### 2026-08-13 CU-205-WINDOWS-ICON-001

- Windows previously packaged the original fully opaque square artwork in all
  seven ICO frames, while macOS now used a transparent native rounded
  silhouette. The Windows asset therefore appeared visibly square on desktop,
  Start, and taskbar surfaces which do not apply a mask for the application.
- The existing orange/black e-Mate robot pixels were reused without redrawing.
  Each 16, 24, 32, 48, 64, 128, and 256 pixel ICO frame is now generated at the
  same proportional live area as the macOS icon, on a transparent canvas.
- The shared asset regression verifies transparent corners, an opaque center,
  the 256-pixel live bounds, and bounded pixel drift between normalized macOS
  and Windows visible artwork. It passed `1/1`; all seven native ICO frame
  sizes were then inspected for transparent corners and non-empty centers.

### 2026-08-13 CU-205-ECOREX-HISTORY-001

- Real packaged data evidence identified the disappearing history as a brand
  root split: macOS ECoreX retained its schema-v1 Runtime database under
  `Library/Application Support/ECoreX`, while e-Mate uses `.emate`; Windows
  uses the corresponding `APPDATA/ECoreX` root. This is a same-generation
  v1-to-v2 recovery, not the old v0.3 sessions/messages import.
- First desktop startup now makes a byte-stable snapshot of the old database,
  upgrades only that private copy through the existing signed v1-to-v2 storage
  migration, and late-merges the closed conversation graph into a copy of the
  current e-Mate database. It retains current threads and copies only the
  referenced execution/snapshot closure; managed sessions, credentials,
  usage/audit, scheduler, and update state are not imported. Any identity or
  uniqueness conflict fails closed instead of overwriting current history.
- The old database and its WAL are never modified or deleted. The current
  database is checkpointed before staging, the merged copy must pass SQLite
  foreign keys and the full Runtime invariant auditor, and publication removes
  only proven-empty target sidecars before an atomic database replacement.
  A retained pre-restore snapshot and idempotent replay recover a crash after
  publish but before its receipt.
- The observed old profile has no Artifact rows. This change therefore rejects
  a source containing Artifact/upload rows instead of silently restoring broken
  attachment references; no unproven attachment format was invented.
- Focused recovery and desktop-startup checks passed `5/5`, including source
  byte preservation, existing-history retention, collision rollback, nonempty
  WAL preservation, crash replay, macOS/Windows discovery, and invariant-valid
  restored history. A disposable copy of the real old/new databases restored
  5 threads, 12 turns, 31 items, and 1,437 events while preserving all 9 current
  threads; the resulting Runtime invariant report was clean. No real profile
  was changed.

### 2026-08-13 CU-205-THREAD-SWITCH-JITTER-001

- The message Timeline retained one Virtuoso measurement owner while switching
  between conversations of very different heights. Stale row measurements and
  the new conversation's scroll correction then competed, visibly moving only
  the message region while the composer stayed fixed.
- The Virtuoso list is now keyed by `timelineThreadId`, so each conversation
  receives fresh measurement and scroll state without remounting the composer.
  No animation was disabled and no timeout-based correction was added.
- The exact renderer contract passed `15/15`. A focused source E2E switched a
  48-turn conversation to a 2-turn conversation and back, observed the expected
  measurement owner each time, and verified unchanged composer geometry; it
  passed `1/1` in 1.9 seconds. TypeScript and the Vite source build also passed.

### 2026-08-13 CU-205-COW-RUNTIME-LATENCY-001

- The comparison source is official CowAgent 2.1.5 exact
  `e3ac1b952500f60934862c6bf0bd0de91b415ed8`. Its `AgentBridge.get_agent`
  initializes one Agent only when a session is first seen, then reuses that
  live instance. e-Mate instead rebuilt the complete Agent, tools, Skills,
  memory, prompt, and restored history before every Turn.
- e-Mate now keeps one Cow Agent per thread. Later Turns update only the
  turn-scoped Gateway model, memory-summary model, request identity, and Cow
  tool context. The workspace-bound ToolManager and MCP OAuth redirect remain
  initialized on the first Turn; no tool or schema was removed.
- Turn admission now uses the last validated immutable local model catalog.
  Remote `/models` refresh remains on bootstrap/model discovery, while the
  Gateway continues to fence execution against its active revision. The red
  regression observed one additional `/models` call per submitted Turn; the
  fixed path observes zero.
- In-process create, queue, and replace producers now wake the durable Worker
  immediately. A wake that arrives just before the Worker begins its idle wait
  is preserved instead of being cleared. With a deliberately exaggerated
  five-second poll and a local fake provider, the unfixed path had not reached
  the Gateway after 750 ms. The fixed single-run profile measured first/warm
  admission at 104.18/40.89 ms and submission-to-Gateway at 406.47/148.10 ms.
- The three focused regressions failed `3/3` against unmodified `9e4f8cdd` and
  passed `3/3` after the fix. The deterministic Agent regression injects a
  100 ms initializer: before the fix it runs twice; after the fix initialization
  count is one and the later Turn stays below 50 ms. The final Agent/catalog/
  supervisor/MCP focused set passed `10/10` in 10.53 seconds without a real
  model or network request.
- The affected three-file suite passed `21` cases and retained one unrelated
  baseline failure: a stale managed-session assertion still expects a remote
  `shell` hard deny while the Cow boundary intentionally projects none. The
  same assertion fails unchanged on baseline `9e4f8cdd`. No build, package,
  publication, or CI run was started.

### 2026-08-13 CU-205-WINDOWS-RUNTIME-STARTUP-001

- The reported dialog proves Electron started but not why its Runtime child
  failed. The user's exact failure remains unclassified until a new candidate
  emits its fixed diagnostic code; x64 alone is not a root-cause signal.
- A shared P1 reproduced the same dialog when an unrelated process owned the
  fixed port 8765. Desktop now preserves 8765 by default, moves an unknown
  listener to a free loopback port, and retries a bounded two additional ports
  only when the child reports `http_server_bind`. The selected origin reaches
  Electron navigation, notifications, Connector OAuth, and Cow MCP OAuth.
- Direct desktop packaging had also bypassed the existing nonce-bound startup
  stage evidence because that helper accepted only Bootstrap `slots` layouts.
  It now accepts the immutable `resources/runtime/payload` layout and writes
  only fixed stage, spawn errno, or exit classifications. No path, token,
  nonce, or raw stderr is retained. Consecutive failures replace
  `~/.emate/diagnostics/runtime-startup.json`; a successful start removes it.
- The exact Electron contract passed `17/17`. Focused dynamic-port, bind-race,
  same-origin, consecutive-diagnostic, direct-package-stage, Connector OAuth,
  and Cow MCP OAuth checks passed `5/5`. No installer was built.

### 2026-08-13 CU-205-WINDOWS-MSVCP-CLOSURE-001

- Static inspection of the exact 2.0.4 Windows Core found that
  `greenlet/_greenlet.cp311-win_amd64.pyd` imports `MSVCP140.dll`, but Core
  ships only the VCRuntime DLLs. Machines with a system VC runtime hide this
  package defect. This is a Browser package-closure P1, not proof of the
  reported whole-Runtime exit.
- The Windows native stage now takes `msvcp140.dll` only from the locked MSVC
  14.44 x64 app-local Redist path, validates its Microsoft signature, version,
  SHA-256, and caller-pinned manifest identity, records it in the native build
  receipt, and installs it beside Pack Python. No host DLL was copied into the
  repository.
- The final Pack-Python probe imports `greenlet` and, on Windows, proves the
  loaded `msvcp140.dll` came from Pack Python rather than System32. Receipt,
  package-shape, and signed-candidate drill checks passed `3/3`; the affected
  native set passed `5` with `11` Windows-only source tests skipped on macOS.
  PowerShell source parsing passed on Windows. The frozen candidate still owns
  the actual Windows native build and installed-machine replay.

### 2026-08-13 CU-205-COW-DESKTOP-PLATFORM-PARITY-001

- The platform comparison source is official CowAgent 2.1.5 exact
  `e3ac1b952500f60934862c6bf0bd0de91b415ed8`. Its desktop launches one
  PyInstaller onedir backend from a user-writable data directory, pipes child
  output into the shell, and waits on an unauthenticated health endpoint.
- Cow desktop clears its fixed port by terminating every discovered listener.
  e-Mate does not copy that unsafe behavior: it proves an owned Runtime with
  the existing HMAC receipt, replaces only that exact owner, and moves an
  unknown listener to a free loopback port. Its fixed startup-stage diagnostic
  retains less information than Cow's raw output but remains useful without
  persisting paths, tokens, nonces, or arbitrary stderr.
- Cow's PyInstaller `Analysis` and `COLLECT` own the Windows native dependency
  closure. e-Mate retains its immutable Core/Pack staging and now binds the
  previously missing `MSVCP140.dll` explicitly, so a developer machine's
  System32 can no longer make an incomplete package appear healthy.
- On macOS, Cow restores the login-shell PATH because Finder/Dock starts GUI
  applications with a minimal environment. e-Mate now does the same once per
  Electron process and retains `~/.local/bin`, Homebrew, and standard system
  locations; Windows PATH behavior is unchanged. The exact desktop contract
  passed, followed by the remaining affected Electron cases `8/8`.
- e-Mate already verifies the target architecture and every staged Mach-O,
  ships the matched Playwright Chromium runtime, and keeps writable state under
  `EMATE_DATA_DIR`; these stronger existing paths were not replaced. Developer
  ID signing, hardened Runtime, and notarization also remain outside this
  unsigned release train as previously selected, rather than being simulated.

### 2026-08-13 CU-205-LOCAL-SKILL-AUTHORITY-001

- The first frozen source gate stopped because `SkillManager.is_skill_enabled`
  still consulted an e-Mate enterprise projection before its Cow local
  `skills_config`. A valid local Skill could therefore be disabled remotely,
  contrary to the CowAgent data-plane boundary.
- The enterprise lookup is deleted. Local enablement is again owned only by
  Cow's local configuration; disabling or removing a distributed Extension
  root affects discovery of that root, not the meaning of the local enabled
  flag. No replacement policy or compatibility path was added.
- The exact CI regression changed from red to green. Focused and affected
  Scheduler/Skill/Hub checks passed `34` cases on the isolated fix branch.
