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
| `MAC-OFFICE-001` | `CU-MAC-OFFICE` | `63a591d81ddc38ab2793236943f559a02f25eee9` | Word create authoring inputs failed twice after the initial create probe | Pending triage | Assigned separately | — | Pending | Exact 2.0.4 arm64 DMG SHA-256 `c9dc0022a831f053081f3c0776f1480c2939811ec1ffde2d7f9a7996e4fa2582`; ConversationStore retained the complete tool chain |

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
