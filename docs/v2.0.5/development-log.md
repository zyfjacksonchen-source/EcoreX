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
| CU-MAC-OFFICE | macOS | Word, Excel, PowerPoint, PDF create/edit/open/preview | pending | — | — | — |
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
