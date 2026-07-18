# EcoreX v1.0 verification ledger

## Baseline - 2026-07-10

| Check | Result | Evidence |
| --- | --- | --- |
| Git baseline | observed | branch `codex/ecorex-v0.3.0-hardening`, commit `9ac3b958` |
| Dirty worktree | expected | existing modified files and untracked `agent/core/` preserved |
| Python runtime | pass | Python 3.11.9 |
| Node runtime | pass | Node 24.15.0, npm 11.12.1 |
| v1 Python dependencies present locally | pass | FastAPI, Pydantic, Uvicorn, Cryptography, Pytest importable |
| Existing version consistency | fail, expected | Python 0.2.8, Web package 0.3.0, documentation older |

## Foundation version source - 2026-07-10

| Command | Exit | Result |
| --- | ---: | --- |
| `python -m pytest -q tests/v1/test_version_source.py` | 0 | 1 passed |
| `python -m json.tool docs/v1.0/progress.json` | 0 | valid JSON |
| `python -m compileall -q ecorex` | 0 | v1 foundation modules compiled |

## Foundation domain integration - 2026-07-10

| Command | Exit | Result |
| --- | ---: | --- |
| `python -m pytest -q tests/v1` | 0 | 48 passed, 1 third-party Starlette warning |
| `python -m compileall -q ecorex` | 0 | all v1 modules compiled |
| `git diff --check -- ecorex tests/v1 release/v1 docs/v1.0 pyproject.toml .hallmark/preflight.json` | 0 | no whitespace errors |
| `python -m pip wheel . --no-deps --no-build-isolation` | 1 | environment lacked the `bdist_wheel` command; build requirement corrected to declare `wheel>=0.42.0`, awaiting isolated rebuild |
| `python -m pip wheel . --no-deps --wheel-dir tmp/v1-wheel` | 0 | built `ecorex_agent_runtime-1.0.0-py3-none-any.whl` (96,794 bytes, SHA-256 `5c2df1703e6d98139c09d8825c5dcb697c5f8a6ac14ceaba9601894b52621d7a`) |
| `tar -tf tmp/v1-wheel/ecorex_agent_runtime-1.0.0-py3-none-any.whl` | 0 | package contains only legacy CLI compatibility plus the new `ecorex` domains and metadata |

## Capability and managed-model policy - 2026-07-10

| Command | Exit | Result |
| --- | ---: | --- |
| `python -m pytest -q tests/v1/test_capability_planner.py tests/v1/test_capability_invocation.py tests/v1/test_managed_model_catalog.py` | 0 | 12 passed; image intent is non-exclusive, progressive discovery works, permission matrix fails closed, and image2 aliases share one canonical image model |

Independent review note: the earlier `48 passed` foundation result is not a
release gate. Runtime, Artifact, and Update reviewers reproduced multiple
high-severity missing boundaries; their remediation suites must pass before
those domains can move from prototype to verified.

## Recording rule

For every batch, append:

```text
date/time
scope
command
exit code
summary
artifact/log path when applicable
known flaky or environment-dependent behavior
```

No slice is marked complete from source inspection alone. It needs an executable
test, contract check, build, or rendered visual verification proportional to its
risk.

## Hardened product slices - 2026-07-10

| Scope / command | Exit | Result |
| --- | ---: | --- |
| Independent Runtime review | 0 | 33 Runtime tests passed |
| Independent product-server review | 0 | 17 passed, 1 Windows symlink-permission skip |
| `python -m pytest -q tests/v1 -k artifact` | 0 | 88 passed, 129 deselected, 1 third-party warning |
| `python -m pytest -q tests/v1/test_runtime_composition.py` | 0 | 2 passed; backend catalogs and immutable snapshots verified |
| `python -m pytest -q tests/v1/test_runtime_artifact_integration.py` | 0 | 1 passed; auth/scope/outbox/Thread stream verified |
| Independent migration review | 0 | 12 passed; source unchanged, COW/idempotency/quarantine verified |
| Independent updater review | 0 | 42 passed, 2 platform skips |
| Independent release-builder review | 0 | 22 passed, 1 platform skip |
| Last quiescent full `tests/v1` reported by migration reviewer | 0 | 242 passed, 4 platform skips, 1 Starlette warning |
| `npm run typecheck` | 0 | v1-only TypeScript graph passed |
| `npm run test:v1` | 0 | 11 reducer/Runtime transport/Artifact client tests passed |
| `npm run build` | 0 | hashed Vite bundle: JS 376.40 kB (117.25 kB gzip), CSS 29.79 kB (5.76 kB gzip) |
| `npm audit` | 0 | 0 vulnerabilities after Electron/legacy dependency removal and pinned esbuild remediation |
| `python scripts/check-v1-design-system.py` | 0 | strict CSS passed; legacy debt counts all zero |

Current-tree caveat: a signed Web-manifest release-builder follow-up is active
and temporarily owns new tests/exports. Run and record a fresh full suite only
after that worker reaches a quiescent state; do not reuse the 242-pass result as
the final GA gate.

## Managed gateway and supervised Agent execution - 2026-07-10

| Scope / command | Exit | Result |
| --- | ---: | --- |
| `python -m pytest -q tests/v1/test_runtime_worker_supervisor.py tests/v1/test_agent_turn_worker.py tests/v1/test_runtime_kernel_api.py tests/v1/test_server_product_app.py` | 0 | 15 passed, 1 Windows permission skip; unconfigured/ready bootstrap states and ASGI Worker lifecycle verified |
| `python -m pytest -q tests/v1/test_runtime_composition.py tests/v1/test_runtime_hardening.py tests/v1/test_version_source.py` | 0 | 27 passed; immutable Turn context and Runtime security regressions green |
| `npm run typecheck` | 0 | Bootstrap `model_service` contract and unavailable-state UI typecheck passed |
| `npm run test:v1` | 0 | 11 tests passed |

The first combined Web command used the retired `npm test` name and exited 1
after TypeScript itself passed. It was a command-selection error, not a product
failure; the authoritative package script `npm run test:v1` was rerun above.

## Permission authority and real Web release pipeline - 2026-07-10

| Scope / command | Exit | Result |
| --- | ---: | --- |
| `python -m pytest -q tests/v1/test_runtime_permission_settings.py tests/v1/test_runtime_composition.py tests/v1/test_runtime_worker_supervisor.py tests/v1/test_capability_invocation.py` | 0 | 9 passed; persistent profile, immutable prior Turn policy, future-Turn policy, hard-deny and Worker regressions green |
| `python -m pytest -q tests/v1/test_runtime_permission_settings.py` | 0 | 2 passed; delayed idempotent retry cannot resurrect revoked full access |
| `npm run typecheck && npm run test:v1` | 0 | TypeScript passed; 15 tests including Runtime client and SHA dist security/identity tests |
| `npm run build` | 0 | real Vite dist rewritten to two final-byte SHA-256 named assets |
| `python scripts/check-v1-design-system.py` | 0 | strict design token gate passed with all legacy counts zero |
| `npm audit` | 0 | zero vulnerabilities |
| Real dist Web/release focused Python suite | 0 | 20 passed, 1 platform skip; Vite dist → signed Web/Release manifests → verified server loader |
| Extended release/server regression reported by Web worker | 0 | 37 passed, 2 platform skips |

## Durable Connector and Bootstrap integration - 2026-07-10

| Scope / command | Exit | Result |
| --- | ---: | --- |
| `python -m pytest -q tests/v1/test_connector_contract.py tests/v1/test_connector_persistence.py tests/v1/test_connector_vault.py tests/v1/test_connector_runtime_integration.py tests/v1/test_runtime_connector_mount.py tests/v1/test_bootstrap_supervisor.py` | 0 | 58 passed, 1 platform skip; durable Connector recovery/outbox/vault/API mount and signed Bootstrap supervisor verified |

The provider adapters and OS vault still require signed Windows/macOS real-
machine runs with production Feishu/Tencent credentials. This focused result is
an implementation gate, not the GA credential or rollout gate.

## Managed Model Gateway server boundary - 2026-07-10

| Scope / command | Exit | Result |
| --- | ---: | --- |
| `python -m pytest -q tests/v1/test_managed_gateway_server.py tests/v1/test_managed_gateway.py tests/v1/test_agent_turn_worker.py` | 0 | 16 passed; cloud auth/model allowlist/quota, durable stream replay, atomic terminal state, lease fencing, redaction, ledger tamper detection and Job-attempt request identity verified |

This remains an implementation gate. Independent review, production TLS,
account-session auth, provider credentials and external streaming soak are
separate release gates.

## Online update and Control Plane closure - 2026-07-10

| Scope / command | Exit | Result |
| --- | ---: | --- |
| Independent full `python -m pytest -q tests/v1` | 0 | 361 passed, 7 skipped, 5 dependency warnings after update/control-plane hardening |
| `python -m pytest -q tests/v1/test_update_transport.py tests/v1/test_runtime_update_service.py tests/v1/test_update_wss_e2e.py tests/v1/test_control_plane_release_flow.py` | 0 | 20 passed, 1 skipped; real TLS Uvicorn/WebSocket signal, feed auth, service activation and rollout policy reproduced by root |

The warnings are the existing Starlette multipart warning plus Uvicorn's
legacy `websockets` protocol adapter deprecations. The skipped case is platform
or environment dependent. Live public infrastructure and signed platform
package activation are not covered by this local network gate.

## Connector WebUI lifecycle - 2026-07-10

| Scope / command | Exit | Result |
| --- | ---: | --- |
| `npm run typecheck` | 0 | Connector client/session/popover integration passed TypeScript |
| `npm run test:v1` | 0 | 19 passed, including strict Connector routes, OAuth URL authority and health/tier projection |
| `python ../scripts/check-v1-design-system.py` | 0 | four strict CSS files; all six prohibited debt counters are zero |
| `npm run build` | 0 | Vite production build and final-byte SHA-256 rewrite completed; JS 398.58 kB, CSS 34.99 kB before gzip |

Browser visual/accessibility evidence is still required; this batch proves
contracts, reducer/client logic, token discipline and production build only.

## Thread catalog and deterministic history - 2026-07-10

| Scope / command | Exit | Result |
| --- | ---: | --- |
| `python -m pytest -q tests/v1/test_runtime_thread_catalog.py tests/v1/test_replay_observability.py tests/v1/test_runtime_kernel_api.py` | 0 | 14 passed; HMAC/canonical keyset cursor, first-Turn title, idempotent rename/archive/restore and Mock Replay parity verified |

The first focused run found a non-canonical Base64 cursor alias and an
incorrect test-side Replay envelope access. Cursor decoding was hardened and
the public response contract assertion corrected before the recorded green
rerun.

## Gateway, Connector and Replay independent closure - 2026-07-10

| Scope / command | Exit | Result |
| --- | ---: | --- |
| Independent Gateway-focused suite | 0 | 39 passed; strict bounded stream, atomic quota/lease/terminal ledger, cancellation and Worker recovery |
| Root combined Gateway/Worker/Job/Runtime/Capability reproduction | 0 | 57 passed, 1 third-party warning |
| Independent quiescent `python -m pytest -q tests/v1` after Connector closure | 0 | 411 passed, 7 platform skips, 5 dependency warnings |
| Root Connector-focused suite | 0 | 50 passed, 1 third-party warning; lifecycle idempotency, atomic reauthorization, vault recovery and OAuth callback hardening |
| Root Replay/observability plus Thread catalog | 0 | 11 passed, 1 third-party warning; deterministic Mock/Live contracts, trace, encrypted audit outbox and history parity |

The 411-pass full run predates the currently active retouch executor and latest
Share-list additions, so it is a strong integration checkpoint rather than the
final GA full-suite gate.

## Administrator release workflow - 2026-07-10

| Scope / command | Exit | Result |
| --- | ---: | --- |
| `python -m pytest -q tests/v1/test_control_plane_admin_client.py tests/v1/test_control_plane_admin_cli.py tests/v1/test_control_plane_release_flow.py` | 0 | 7 passed; strict HTTPS admin client, fail-closed response handling, cross-process journaled promotion and existing release flow |
| `python -m py_compile ...` and scoped `git diff --check` | 0 | new client/CLI modules compile and introduce no whitespace errors |

`python -m black --check ...` could not run because Black is not installed; it
exited 1 before examining files and is recorded as an environment/tooling gap,
not a passing formatting result.

## Supervised precise retouch and Thread/Share WebUI - 2026-07-10

| Scope / command | Exit | Result |
| --- | ---: | --- |
| Domain owner `python -m pytest -q tests/v1` | 0 | 435 passed, 7 skipped before Runtime lifespan wiring |
| `python -m pytest -q tests/v1/test_runtime_retouch_integration.py tests/v1/test_retouch_execution.py tests/v1/test_runtime_artifact_integration.py tests/v1/test_server_product_app.py` | 0 | 18 passed, 1 platform skip; worker lifecycle, new revision/Turn item and fail-before-orphan unavailable path |
| `npm run typecheck` | 0 | Thread/Share UI plus retouch readiness contract passed |
| `npm run test:v1` | 0 | 25 passed, including history transport/reducer and Clipboard rejection semantics |
| `python scripts/check-v1-design-system.py` | 0 | four strict CSS files, every prohibited debt counter zero |
| `npm run build` | 0 | Vite production bundle and two-asset final-byte content addressing passed; JS 417.86 kB, CSS 42.90 kB |

The first Runtime retouch integration run left the request queued because its
test source Turn intentionally had an earlier nonterminal Agent Job; this was
the scheduler's correct per-Thread FIFO behavior. The fixture was corrected to
settle the source Turn/Job exactly as a real completed image generation would,
then the authoritative green run above was recorded.

## ShareSnapshot independent security review - 2026-07-10

| Scope / command | Exit | Result |
| --- | ---: | --- |
| Independent ShareSnapshot-focused suite | 0 | 28 passed; account isolation, concurrency, expiry/revoke, transport limits, token/state/audit tamper resistance |
| Independent full `python -m pytest -q tests/v1` | 0 | 440 passed, 7 skipped after Share hardening |
| Root combined Share/Control Plane reproduction | 0 | 35 passed, 1 third-party warning; local/cloud share plus admin release clients remain compatible |

The full 440-pass checkpoint predates the active dedicated-Retouch-Turn and
managed-session batches, so the final full gate remains pending.

## Signed capability-pack and handler trust boundary - 2026-07-10

| Scope / command | Exit | Result |
| --- | ---: | --- |
| `python -m pytest -q tests/v1/test_capability_pack_runtime.py tests/v1/test_capability_invocation.py tests/v1/test_capability_planner.py tests/v1/test_capability_snapshots.py tests/v1/test_agent_turn_worker.py` | 0 | 27 passed; signed pack/artifact/tool-contract verification, tamper rejection, strict argument/result contracts, truthful handler availability, workspace confinement and Worker compatibility |

This verifies the independent trust boundary. Product Runtime wiring and real
platform provider packs are separate gates and remain open.

## Managed session, dedicated Retouch Turn and Product capabilities - 2026-07-10

| Scope / command | Exit | Result |
| --- | ---: | --- |
| Managed-session/Product owner suite | 0 | 23 passed, 1 platform skip; signed lease/vault/recovery/model filtering/logout and Product enforcement |
| Adjacent managed-session/Gateway/Runtime suite | 0 | 78 passed, 1 platform skip before Retouch provider wiring |
| `python -m pytest -q tests/v1/test_runtime_retouch_integration.py tests/v1/test_retouch_execution.py tests/v1/test_managed_session_runtime.py tests/v1/test_server_product_app.py` | 0 | 27 passed, 1 platform skip after fresh Retouch snapshot wiring |
| `python -m pytest -q tests/v1/test_retouch_execution.py tests/v1/test_runtime_retouch_integration.py tests/v1/test_runtime_event_store.py tests/v1/test_runtime_jobs_and_interactions.py tests/v1/test_replay_observability.py` | 0 | 28 passed; dedicated operation Turn, concurrency/restart idempotence, rollback and Replay |
| Product capability/readiness convergence | 0 | 23 passed, 1 skip, then 10 passed, 1 skip after pack/adapter fail-closed startup fencing |

## Capability-pack signed release construction - 2026-07-10

| Scope / command | Exit | Result |
| --- | ---: | --- |
| `python -m pytest -q tests/v1/test_release_capability_pack.py tests/v1/test_release_builder.py tests/v1/test_release_builder_security.py` | 0 | 25 passed, 1 platform skip; deterministic pack ZIP/sidecar, inner/outer signatures, SBOM/metadata and invalid-contract rejection |
| Focused pack Runtime + release round trip | 0 | 8 passed; verifier-only trust proof remains compatible with ReleaseBuilder output |

## Administrator Web restore integration - 2026-07-10

| Scope / command | Exit | Result |
| --- | ---: | --- |
| `python -m pytest -q tests/v1/test_control_plane_admin_web.py tests/v1/test_control_plane_release_flow.py tests/v1/test_control_plane_admin_client.py tests/v1/test_control_plane_admin_cli.py` | 0 | 19 passed; content-addressed console, auth, app rebuild refresh, explicit latest selection, atomic concurrent snapshot and release operations |

## Durable Share worker and managed device authorization - 2026-07-10

| Scope / command | Exit | Result |
| --- | ---: | --- |
| Share Durable Job focused/adjacent gates | 0 | 36 passed; then 35 passed, 1 platform skip; final 10-test cleanup gate passed |
| Independent `python -m pytest -q tests/v1` after Share worker convergence | 0 | 501 passed, 8 skipped |
| `python -m pytest -q tests/v1/test_managed_device_authorization.py tests/v1/test_managed_session_authority.py tests/v1/test_managed_session_runtime.py tests/v1/test_server_product_app.py tests/v1/test_managed_gateway_server.py` | 0 | 44 passed, 1 platform skip; signed lease install, vault-only secrets, durable poll lease, first-login Runtime composition and existing Product/Gateway authority remain compatible |
| `python -m py_compile ecorex/runtime/api.py ecorex/server/app.py ecorex/session/device.py ecorex/session/device_transport.py ecorex/session/api.py ecorex/protocol/models.py` | 0 | device routes, Runtime/Product composition and protocol models compile |
| `python -m pytest -q tests/v1/test_runtime_device_authorization.py tests/v1/test_managed_device_authorization.py tests/v1/test_managed_session_runtime.py tests/v1/test_server_product_app.py` | 0 | 21 passed, 1 platform skip; unauthenticated Product bootstrap, mutation denial, bearer/Origin/CSRF, secret non-persistence, signed grant, reload fence and reconstructed authenticated app |

The 501-pass run predates device-login, Product-entrypoint and latest WebUI
changes. It is retained as an integration checkpoint, not the final GA suite.
The live identity-provider tenant and packaged Bootstrap process-switch drill
remain real-environment gates.

## WebUI static, Mock Runtime and Hallmark GA - 2026-07-10

| Scope / command | Exit | Result |
| --- | ---: | --- |
| `npm run typecheck` | 0 | managed bootstrap/device login, queue/retry/HITL, Artifact and Retouch contracts typecheck |
| `npm run test:v1` | 0 | 39 passed; reducer, API, session, device-login, Artifact/Retouch presentation, contrast, Mock server and content rehash |
| Standalone GA Mock Runtime E2E | 0 | 2 passed; same-origin API/SSE/CSRF scenarios and reset contract |
| `python scripts/check-v1-design-system.py` | 0 | four strict style files; all six prohibited debt counters zero |
| `npm run build` | 0 | 1811 modules; exactly two final-byte content-addressed assets |
| Hallmark/static scoped checks | 0 | contrast, forbidden-pattern and diff gates passed; `.hallmark/log.json` and `.hallmark/browser-ga.json` are valid JSON evidence |

The actual Browser tool reported no available instance and inventory `[]`.
Accordingly the 1440/1024/768/390 light/dark screenshots, axe, real keyboard,
coarse pointer, forced-colors, reduced-motion and overflow sweeps are recorded
as `not_run_no_browser`, not silently replaced with a different browser tool.

## Packaged Product Runtime entrypoint - 2026-07-10

| Scope / command | Exit | Result |
| --- | ---: | --- |
| Product owner entrypoint/Release/Bootstrap/Session/Update convergence | 0 | 114 passed, 4 platform/environment skips; strict current-slot loading, first-login shell, device reload, deterministic Product Core and cleanup ownership |
| `python -m pytest -q tests/v1/test_product_runtime_entrypoint.py tests/v1/test_bootstrap_supervisor.py tests/v1/test_server_product_app.py tests/v1/test_managed_session_runtime.py tests/v1/test_runtime_worker_supervisor.py` | 0 | root reproduction 49 passed, 3 platform skips |
| Product Python compile/help/whitespace gates | 0 | v1 command imports, `python -m ecorex.server --help`, CLI secret redaction and scoped whitespace passed |

This proves the signed local product entry. The local post-restart health
receipt is closed by the later activation section; optional provider deployment
and packaged Windows/macOS lifecycle drills remain explicit gates.

## Artifact external actions and Product cloud Audit - 2026-07-10

| Scope / command | Exit | Result |
| --- | ---: | --- |
| Artifact/Retouch owner suites | 0 | 97 passed; authoritative action eligibility, CAS materialization, receipt recovery and safe platform launcher boundaries |
| Runtime/permission/managed-action adjacency | 0 | 42 passed; exact unauthenticated local exception still requires bearer, Origin, CSRF, account and public action policy |
| WebUI owner gates | 0 | 40 passed plus typecheck/build/design; More menu maps backend action results without local paths |
| `python -m pytest -q tests/v1/test_product_runtime_entrypoint.py tests/v1/test_cloud_audit_transport.py` | 0 | 33 passed, 1 platform skip; signed Audit service composition, managed-session fencing, fixed HTTPS route, retention bounds and lifecycle close |

The cloud transport suite uses a strict mock HTTPS boundary. A deployed Control
Plane endpoint, OS-keychain platform drill and sustained offline outbox recovery
remain real-environment gates rather than inferred evidence.

## Managed stable Connector Product composition - 2026-07-10

| Scope / command | Exit | Result |
| --- | ---: | --- |
| `python -m pytest -q tests/v1/test_managed_connector_gateway.py` | 0 | 8 passed; fixed HTTPS route, managed-session fence, PKCE/grant contract, action/revoke idempotency and malformed/redirect failure |
| Product/connector focused integration | 0 | 33 passed, 1 platform skip; signed Feishu/Tencent adapter selection, OS-vault lifecycle and ASGI cleanup remain compatible with connector Runtime APIs |
| Capability pack/planner/invocation/release adjacency | 0 | 20 passed; new browser/image/sandbox dependency declarations and workspace-write shell policy preserve snapshot and signature contracts |

No real third-party tenant was available in this workspace. Feishu and Tencent
Docs credentialed smoke tests remain an explicit release-blocking external gate.

## Legacy Python distribution retirement - 2026-07-10

| Scope / command | Exit | Result |
| --- | ---: | --- |
| Clean isolated `pip wheel` from a source tree containing both `ecorex/` and `cli/` | 0 | 1.0.0 wheel, 527244 bytes, 161 members, zero legacy package entries, no Click/Requests dependency |
| `python -m pytest -q tests/v1/test_product_packaging_surface.py ...release_builder... ...product_runtime_entrypoint...` | 0 | 42 passed, 2 platform skips; package discovery/import boundary and signed Core/Web exclusion remain green |

An earlier in-place wheel inherited stale files from the ignored historical
`build/` directory. It is retained as diagnostic evidence only; the clean-source
build above is the authoritative release-like result.

## Durable reasoning Item replacement - 2026-07-10

| Scope / command | Exit | Result |
| --- | ---: | --- |
| `python -m pytest -q tests/v1/test_reasoning_item_persistence.py tests/v1/test_agent_turn_worker.py tests/v1/test_runtime_kernel_api.py` | 0 | 17 passed; provider-summary validation, durable atom/revision, atomic replacement, restart and terminal archive |
| `python -m pytest -q tests/v1/test_reasoning_item_persistence.py tests/v1/test_replay_observability.py` | 0 | 12 passed; Event Store Mock Replay reconstructs the same reasoning contents and watermark without side effects |
| Runtime/Job/Gateway/Worker/Replay reasoning adjacency | 0 | 79 passed; terminal/deadline settlement, Gateway wire shape and existing event contracts remain compatible |
| `npm run typecheck` | 0 | reasoning Item contract, session selector and Timeline mapping typecheck |
| `npm run test:v1` | 0 | 43 passed; phase/tool persistence, single-event next-atom replacement, explicit terminal archive and projection resync |
| `npm run build` plus `python scripts/check-v1-design-system.py` | 0 | 1811 modules, two content-addressed assets and all six prohibited design-debt counters remain zero |

The disclosure contract contains provider-approved summaries only; it is not a
claim that hidden model chain-of-thought is available or persisted.

## Backend-authoritative precise-retouch workspace - 2026-07-10

| Scope / command | Exit | Result |
| --- | ---: | --- |
| `python -m pytest -q tests/v1 -k "artifact or retouch"` | 0 | 135 passed, 1 platform skip; immutable surface, six-kind mask, max-10 pinned references, CAS/version fencing, restart/submit recovery, exact result/original and existing Artifact/Retouch adjacency |
| `npm run typecheck` | 0 | strict workspace/edit-surface/mask/inspection contracts and thin React editor typecheck |
| `npm run test:v1` | 0 | 49 passed; workspace transport, canvas geometry/history, malformed inspection rejection, stale-list revision protection, inline result presentation and GA mock lifecycle |
| `npm run build` | 0 | 1812 modules; two content-addressed production assets |
| `python scripts/check-v1-design-system.py` | 0 | four strict CSS inputs; raw colors, arbitrary radii/shadows/z-index, layout transitions and `transition: all` remain zero |
| Managed image/Workspace/Retouch/Product cross-layer suite | 0 | 45 passed, 1 platform skip; CAS mask bytes reach the managed adapter by digest, canonical surface metadata stays structured, and Product composition remains compatible |

The Cowart comparison was clean-room only against commit `61f6daaf`; its source
tree has no explicit license, so no code, text or style was copied. Browser
screenshot regression against a real managed image endpoint remains a release
gate; the local GA server proves the new API lifecycle but not model quality.

## Multi-origin release asset publication - 2026-07-10

| Scope / command | Exit | Result |
| --- | ---: | --- |
| `python -m pytest -q tests/v1/test_release_asset_publication.py tests/v1/test_release_replica_publisher.py tests/v1/test_github_release_publisher.py tests/v1/test_release_upload_cli.py tests/v1/test_control_plane_admin_cli.py` | 0 | 21 passed; exact digest resume, strict HTTPS receipts, mirror/GitHub/CDN ordering, local tamper rejection and draft/public fencing |
| `python -m pytest -q tests/v1/test_release_upload_cli.py tests/v1/test_release_asset_publication.py tests/v1/test_release_replica_publisher.py` | 0 | 12 passed; one-command no-secret config construction and focused regression |
| ReleaseBuilder/Web/pack/publication complete adjacency | 0 | 64 passed, 2 platform skips; deterministic builders and existing signed Web/pack contracts remain green with the new origin publishers |
| Control Plane release/CLI/real TLS WSS after `cdn-sync` gate | 0 | 12 passed; the new third-origin blocking gate preserves candidate, rollout and live update notification contracts |

These suites use strict local HTTP doubles. Live origin credentials, replica
deployment authentication and an outage/failover drill remain external GA
evidence and are not inferred from the contract tests.

## Crash-contained browser/sandbox Capability Packs - 2026-07-10

| Scope / command | Exit | Result |
| --- | ---: | --- |
| `python -m pytest -q tests/v1/test_process_capability_pack.py tests/v1/test_capability_invocation.py tests/v1/test_capability_pack_runtime.py` | 0 | 15 passed; signed descriptor binding, minimized environment, backend permission snapshot, approval/idempotency, crash/path-leak/output-flood containment and existing pack semantics |
| Process pack/Capability/Product loader adjacency | 0 | 33 passed, 1 platform skip; resolved workspace roots reach the aggregate resolver and the packaged CLI selects it |
| Focused Product/process pack plus `python -m ecorex.server --help` | 0 | 25 passed, 1 platform skip; Product composition and closed CLI contract remain green |

This is protocol and local child-process evidence. Windows/macOS pack builds
must still prove their actual workspace sandbox and browser lifecycle under the
platform release matrix before those packs can pass GA.

## Cross-restart activation health receipt - 2026-07-10

| Scope / command | Exit | Result |
| --- | ---: | --- |
| `python -m pytest -q tests/v1/test_update_activation_health.py` | 0 | 19 passed; provisional intent, Runtime service handoff, exact nonce proof, probe-only Product, mutation gate, first install, probe/full launch failure, pre-barrier exit, rollback-intent power loss replay, timeout/spoof, parent/Bootstrap/confirmation crashes and roll-forward storage barrier |
| `python -m pytest -q tests/v1/test_update_manifest.py tests/v1/test_update_durability.py tests/v1/test_update_coordinator.py tests/v1/test_update_composition.py tests/v1/test_update_transport.py tests/v1/test_update_wss_e2e.py tests/v1/test_runtime_update_service.py tests/v1/test_bootstrap_supervisor.py tests/v1/test_product_runtime_entrypoint.py tests/v1/test_update_activation_health.py` | 0 | 119 passed, 5 platform/environment skips; coordinator, durability, manifest, transports, Runtime update service, strict Bootstrap and signed Product entrypoint remain compatible |

The suite verifies a real signed Product Core/Web slot and exact in-process
loopback contract. Packaged Windows/macOS process death, port ownership and
actual schema migration drills remain platform release gates.

## Unified managed image Product execution - 2026-07-10

| Scope / command | Exit | Result |
| --- | ---: | --- |
| `python -m pytest -q tests/v1/test_managed_image_integration.py` | 0 | 9 passed; exact session/protocol fencing, tenant CAS, digest download, recover-before-submit, shared scheduler, structured Retouch, two-phase Artifact recovery, renewable lease, malformed descriptor settlement and legacy-transport absence gate |
| Image Orchestrator/Product/Pack/Retouch convergence | 0 | 77 passed, 2 platform skips before the final heartbeat/descriptor additions; a final superseding convergence run is recorded below |
| Final Image Orchestrator/Product/Pack/Retouch convergence | 0 | 82 passed, 2 platform skips; includes aggregate process-pack resolver, publication heartbeat, stable malformed-result error and legacy-owner removal gate |
| Full Artifact/Retouch/Image adjacency | 0 | 131 passed; Artifact identity/CAS/classification/API/actions/outbox, Workspace, Runtime publication, Retouch and cloud image orchestration remain green together |
| Python compile and `git diff --check` | 0 | image/integration/server/runtime modules compile; no whitespace error |

The earlier checkpoint had no real shared-storage service. A later superseding
PostgreSQL/MinIO integration run is recorded below. Real managed-image
credentials, production object-storage TLS/KMS and provider quality remain
release-environment gates.

## Rotation-safe public ShareSnapshot keys - 2026-07-10

| Scope / command | Exit | Result |
| --- | ---: | --- |
| `python -m pytest -q tests/v1/test_cloud_share_key_rotation.py tests/v1/test_control_plane_sharing.py` | 0 | 12 passed; stable old URLs, active/retired key issuance, cross-key audit, revoke/expiry, immutable key identity, removed-key failure, wrong-key rollback and populated pre-keyring v1 migration |
| Runtime/local/Control Plane Share adjacency | 0 | 40 passed; snapshot, Durable Job, HTTPS transport and Runtime mount contracts remain green with key rotation |
| All tests referencing `create_control_plane_app` or `CloudShareRepository` | 0 | 34 passed; cloud Audit, release flow, Share and real TLS update notification remain compatible |
| `python -m py_compile ...` plus scoped `git diff --check` | 0 | keyring implementation/export and dedicated rotation suite compile; no whitespace error |

The tests retain historical keys in memory only. Production rotation still has
to prove KMS/HSM retrieval, active-key promotion, retired-key retention and
emergency key-removal behavior under the deployed Control Plane identity.

## Publication/process-pack adversarial hardening - 2026-07-10

| Scope / command | Exit | Result |
| --- | ---: | --- |
| `python -m pytest -q tests/v1/test_process_capability_pack.py tests/v1/test_capability_pack_runtime.py` | 0 | 17 passed; signed-byte execution fences, strict JSON, path normalization, parent-environment-independent system tools, crash/output containment and real Windows parent/descendant termination |
| Publication publisher/coordinator/CLI focused suites | 0 | 31 passed; exact GitHub 422 race recovery, mirror/CDN receipt fencing, signed-source preflight, receipt transaction lock, deterministic multi-resource cleanup and no-secret config/schema binding |
| Control Plane release and administrator CLI adjacency | 0 | 18 passed; immutable first publication time and exact same-receipt GitHub/mirror/CDN evidence binding |
| ReleaseBuilder/Web/Capability/Control Plane combined adjacency | 0 | 109 passed, 2 platform skips; deterministic signed builders and existing runtime contracts remain green |
| `python -m compileall -q ecorex/release ecorex/control_plane ecorex/integration/pack_process.py ecorex/server/pack_resolver.py` | 0 | all edited production modules compile |

HTTP origins and provider APIs use strict local doubles. Live origin
credentials, replica outage/failover, Windows/macOS signed package execution
and the actual Browser/Sandbox OS containment profile remain external GA
evidence; none is inferred from these local tests.

## Deterministic Replay WebUI exposure - 2026-07-10

| Scope / command | Exit | Result |
| --- | ---: | --- |
| `python -m pytest -q tests/v1/test_replay_observability.py` | 0 | 8 passed; read-only deterministic projection, watermark/digest integrity, backend-projected Live candidates, fork exclusion, explicit confirmation, current-permission replanning and idempotent Live Turn |
| Thread catalog/kernel/Replay adjacency | 0 | 14 passed; history, fork, projection and Replay contracts remain compatible |
| Capability and Replay governance adjacency | 0 | 16 passed; a missing signed pack keeps the tool ineligible/hidden without erasing current-policy `requires_approval`, so availability and Replay audit governance remain separate |
| `npm run typecheck` | 0 | strict Mock/Live contracts, Header More wiring, dialog and Runtime-session projection refresh typecheck |
| `npm run test:v1` | 0 | 56 passed; API transport, Replay UI reducer, stable retry identity, stale-snapshot Live fencing, backend candidate mapping and side-effect-free/idempotent GA Replay scenario |
| `npm run build` | 0 | 1814 modules; two content-addressed production Web assets |
| `python scripts/check-v1-design-system.py` | 0 | four strict CSS inputs; raw colours, arbitrary radii/shadows/z-index, layout transitions and `transition: all` remain zero |
| Scoped Python compile and `git diff --check` | 0 | Replay protocol/service compile and edited source has no whitespace error |

No Browser instance is available. These are contract, reducer, static design
and local GA Runtime results; they do not substitute for the remaining real
keyboard/touch/forced-colors/reduced-motion/axe/screenshot matrix.

## Managed OTLP/HTTP JSON trace export - 2026-07-10

| Scope / command | Exit | Result |
| --- | ---: | --- |
| `python -m pytest -q tests/v1/test_otlp_trace_exporter.py` | 0 | 15 passed; fixed HTTPS/443 `/v1/traces`, proto3 JSON shape/ID/time validation, partial-success rejection, numeric/HTTP-date Retry-After, terminal-only projection, encrypted bounded batches, pre-network redaction, paged restart backfill, retry, poison-row isolation and expired-lease crash recovery |
| Product tracing config/composition focused tests | 0 | 2 passed; canonical signed config, route/size rejection, exact managed-session authority and ASGI-owned transport lifecycle |
| OTLP + Cloud Audit + Product + Server + Replay adjacency | 0 | 66 passed, 2 platform/environment skips; existing managed Audit transport, Product composition, local Runtime and deterministic Replay remain compatible |
| `python -m py_compile ...` plus scoped trailing-whitespace scan | 0 | exporter, Runtime/Product composition, tests and exports compile with no whitespace error |

The OTLP wire tests use a strict local HTTP collector double. A deployed
collector credential/RBAC test, sustained offline-to-online recovery, remote
retention validation and alert/dashboard inspection remain external GA gates.

## Quiescent full local convergence - 2026-07-10

| Scope / command | Exit | Result |
| --- | ---: | --- |
| First `python -m pytest -q tests/v1` | 1 | 670 passed, 8 skipped, 3 stale fixture failures; all three assumed shell execution without the now-required signed sandbox pack |
| Truthful-availability fixture/Capability/Replay focused gate | 0 | 16 passed; tests explicitly bind the sandbox pack or assert missing-pack and admin-governance facts separately |
| Second `python -m pytest -q tests/v1` | 0 | 673 passed, 8 skipped; zero failures across Runtime, Product, Release, Update, Artifact, Image, Retouch, Share, Replay, Connector, Migration, Audit and OTLP |
| `npm run typecheck` and `npm run test:v1` | 0 | TypeScript clean; 56 passed including reasoning replacement, Replay confirmation, precise retouch, Share, queue/HITL and GA scenarios |
| `npm run build` plus Design System gate | 0 | 1814 modules, exactly two content-addressed assets; all six prohibited debt counters zero |
| Python compileall, 7 JSON files, Product/Release CLI help, `git diff --check`, v1 trailing-whitespace scan | 0 | all passed; no residual pytest/Node test process |

This is the final quiescent local checkpoint for the current source tree. It
does not satisfy the separately named real-browser, provider, origin,
collector, signed-platform-package or real-user-migration gates.

## Durable multi-instance Control Plane update signal bus - 2026-07-10

| Scope / command | Exit | Result |
| --- | ---: | --- |
| `python -m pytest -q tests/v1/test_control_plane_signal_bus.py` | 0 | 8 passed; two-app shared-database WSS fan-out, exact activation -> targeted `rollout.halted` -> `channel.killed` order, post-kill feed revocation, tenant fencing, transactional/idempotent signal facts and rollback, crash replay, restart cursor recovery, missed-hint feed fallback, retention gap/monotonic sequence and fail-closed instance identity checks |
| Signal bus + release flow + real TLS WSS + Runtime update service/transport authority closure | 0 | 33 passed, 1 environment skip; Hub has no rollout synthesis API, rejects an altered uncommitted event identity before queueing, and only emits exact committed facts |
| Control Plane, Share, cloud Audit, release/WSS and Runtime update transport/service adjacency | 0 | 72 passed, 1 environment skip; existing RBAC/feed behavior, admin clients/dashboard, real TLS WSS, durable client dedupe and poll fallback remain compatible |
| Production-module and dedicated-test `py_compile` | 0 | signal models, repository, instance poller, ASGI integration and tests compile |

The two-app test uses independent ASGI instances and repository connections on
one local WAL database. Deployed replica identity provisioning, shared database
failover, process termination between commit/fan-out/ack and a sustained network
partition remain explicit production drills rather than inferred evidence.

## Public Bootstrap discovery and static download site - 2026-07-10

| Scope / command | Exit | Result |
| --- | ---: | --- |
| `python scripts/check-v1-public-download-site.py` | 0 | canonical checked-in pointer is unpublished/null; five public assets match their 12-hex SHA-256 names; old manifest/index/install/service inputs are absent; HTML references resolve; Caddy/Nginx no-store + immutable policies are present |
| `python -m pytest -q tests/v1/test_public_bootstrap_index.py tests/v1/test_public_download_site.py` | 0 | 10 passed; exact manifest-byte/receipt/signature/source verification, strict schema/runtime/browser rejection, atomic-replace crash preservation, idempotent/crash-recoverable asset hashing, CLI digest output and JS three-origin bounded byte-check behavior |
| `node --check deploy/ecorex-site/site.<digest>.js` and schema JSON parse | 0 | public module syntax and strict Draft 2020-12 schema source parse cleanly |

These are local release-chain gates, not proof that a real stable release is
online. The immutable candidate still needs live CORS-capable mirror/GitHub/CDN
manifest responses, identical origin-byte checks and packaged Bootstrap
Ed25519/install tests on Windows and both macOS architectures.

## Legacy executable source cutoff - 2026-07-10

| Scope / command | Exit | Result |
| --- | ---: | --- |
| `python scripts/check-v1-legacy-cutoff.py` | 0 | Removed WebChannel/chat/copied bundle/overlay/Electron production inputs remain absent; source launchers are exit-78 tombstones; v1 has no legacy absolute imports |
| `python scripts/check-v1-legacy-cutoff.py --strict-production` | 0 | Source-era public/Web packagers, install entry and executable WebChannel service/Docker entrypoints are absent; the migrated LF-byte release contract remains in v1 ReleaseBuilder tests |
| Cutoff + Product packaging + copy-on-write migration focused tests | 0 | 18 passed; WebChannel has no import spec, migration loads no old Runtime module and package discovery remains `ecorex*` only |
| Migrated Artifact-action and non-exclusive image capability contracts | 0 | 8 passed; hover/focus/coarse-pointer Action Rail and imagegen + read/fetch/vision/CDP/shell discovery are bound in v1 tests before deleting their v0.3 test source |
| `python -m pytest --collect-only -q` | 0 | 698 tests collected, every collected node below `tests/v1`; retained historical mixed tests cannot become an accidental Product CI authority |
| Isolated PEP 517 wheel build and ZIP inventory | 0 | `ecorex_agent_runtime-1.0.0-py3-none-any.whl`, 605396 bytes, 174 entries, zero legacy members; an earlier host-only attempt failed because `bdist_wheel` was absent, then the declared isolated build dependencies succeeded |

Two obsolete GitHub workflows that invoked the deleted Electron/WebUI builders
were removed. Version-named read-only baseline/smoke programs and mixed
historical tests remain tracked for audit only and are outside the configured
pytest authority. Their residual references are listed in the implementation
log; they are not ReleaseBuilder inputs or evidence of a working v1 release
chain. The old static public download-site manifest is a separately named local
migration batch, not a Runtime/packager exception in the strict cutoff gate.

## Unified Extension platform - 2026-07-10

| Scope / command | Exit | Result |
| --- | ---: | --- |
| `python -m pytest -q tests/v1/test_extension_platform.py` | 0 | 15 passed, 1 platform symlink skip; canonical ZIP/directory CAS identity, per-file digests, frontmatter/path/static-content rejection, local lifecycle/restart/tamper fence, legacy permanent disable, detached signature rotation/re-verification, exact export IDs, Core declarations, dependency/provider revocation, stable MCP `2025-11-25`, thin API and Turn/Event/Job snapshot binding |
| Runtime kernel/composition/permission/Agent worker/Replay + Extension focused set | 0 | 46 passed, 1 platform skip; Bootstrap/API, permission revisions, immutable Turn contexts, Live Replay and just-in-time provider fence remain compatible |
| Product Server/Product Runtime/Extension composition focused set | 0 | 42 passed, 3 named platform/environment skips; signed Product Server composes Core/Pack/Connector declarations with exact platform, architecture, build digest and Ed25519 verifier |
| Extension + Runtime invariant/worker/composition + Product Server convergence after shared state-machine integration | 0 | 65 passed, 3 named platform/environment skips; transaction-safe shared SQLite/lease changes preserve Extension catalog admission, durable snapshot identity, provider revocation and Product composition |
| Extension API/Product Server adjacency after SystemObservability wiring | 0 | 52 passed, 3 named platform/environment skips; Runtime diagnostics/router/lifecycle/SSE instrumentation preserves authenticated Extension routes, Bootstrap parity, Product middleware/composition and provider fencing |
| `npm run typecheck` and `npm run test:v1` | 0 | TypeScript clean and 69 passed; local Skill ZIP request, backend action/revision fencing, Extension manager responsive/token contract, extension snapshot Event/Live Replay parity and all prior Web reducers/transports remain green |
| Scoped `compileall`, `py_compile`, `git diff --check` | 0 | Extension, Runtime, Protocol, Replay and Product composition compile; edited Extension slice has no whitespace error |

The local Skill test skips only when the current Windows test account cannot
create a symlink; ZIP link/special/executable rejection still executes on every
platform. A live signed publisher/admin extension, real MCP provider process,
real Capability Pack upgrade/rollback and Windows/macOS packaged install drill
remain release-environment evidence rather than claims derived from local
doubles.

## Runtime state-graph consistency and responsiveness - 2026-07-10

| Scope / command | Exit | Result |
| --- | ---: | --- |
| `python -m pytest -q tests/v1/test_runtime_state_machine_invariants.py` | 0 | 11 passed; pre/post-commit fault rollback, event/projection replay, snapshot drift rejection, SQLite transaction ownership, database lease-shape trigger, retry/final-attempt convergence, terminal-response recovery, cross-domain causation and worker/update event-loop responsiveness |
| Agent worker + supervisor + invariant convergence | 0 | 21 passed; model stream, reasoning, HITL, tool uncertainty/checkpoints, restart and off-loop repository calls remain green |
| Runtime/Permission/Session/Extension/Update adjacency | 0 | 144 passed, 4 environment skips before fixing one stale raw events-column fixture; the fixture-only rerun plus invariant suite then passed 10 |
| Retouch/Share/Artifact Durable Job adjacency | 0 | 40 passed; database lease-shape enforcement preserves operation-Turn, publication and Artifact outbox behavior |
| Runtime Update/activation/composition/transport after repository offload | 0 | 40 passed, 1 environment skip; prepare, confirmation, restart, Bootstrap health, rollback and transport recovery remain green |
| Extension fence + Agent worker + Runtime composition | 0 | 25 passed, 1 environment skip; immutable `extension_snapshot_id` and current revocation fence remain wired through off-loop tool execution |
| Audit/OTLP/Replay/Capability/Connector/Artifact repositories | 0 | 127 passed; transaction-preserving scripts remain compatible with encrypted outboxes, append-only snapshots, connector lifecycle and Artifact command/outbox contracts |
| Migration + Product Runtime entrypoint + server composition | 0 | 39 passed, 2 environment skips; copy-on-write migration, signed Product config, managed lifecycle and ASGI composition remain green with the shared Connection semantics |
| Scoped `py_compile` | 0 | Runtime database/connection/EventStore/Jobs/Kernel/Worker/invariant modules, protocol transitions and Update service compile |

The fault harness uses real SQLite WAL files and process-restart repository
instances but local in-process gateway/tool doubles. Sustained multi-process
write contention, forced process termination at every fault point and the
immutable signed-candidate full suite remain final GA/soak evidence.

## Plain-language Web boundary - 2026-07-10

| Scope / command | Exit | Result |
| --- | ---: | --- |
| `npm run typecheck` | 0 | App startup/connectivity/update/permission copy, connector/login/share technical disclosure and Artifact labels type-check cleanly |
| `npm run test:v1` | 0 | 72 passed; known service reasons translate, unknown backend literals do not enter primary copy, technical codes remain separately available, Artifact family/size labels and existing connector URL/clipboard contracts remain green |
| `npm run build` | 0 | Vite production build completed and rehashed 2 Web assets; the existing 515.27 kB chunk-size advisory remains a later code-splitting performance item, not a build failure |
| `python scripts/check-v1-design-system.py` | 0 | Token/shape/elevation/color/z-index/motion lock passed with zero findings after adding the collapsed technical disclosure styles |

The tests prove the local React contract and deterministic mappings. A browser
screen-reader pass for the disclosure elements and final localized-copy review
with representative office users remain release-candidate evidence.

## System health, streaming and continuity - 2026-07-10

| Scope / command | Exit | Result |
| --- | ---: | --- |
| `python -m pytest -q tests/v1/test_system_observability.py` | 0 | 5 passed; bounded/redacted samples, transition audit, restart persistence, non-blocking supervisor and authenticated Runtime health/SSE counters |
| System observability + Runtime hardening/kernel API adjacency | 0 | 31 passed; instrumentation preserves adaptive SSE polling, disconnect behavior, Bootstrap and Runtime mutations |
| Runtime client + GA mock focused Node tests | 0 | 20 passed; primary health omits metrics, technical/history endpoints are read-only and bounded, GA exposes health and reversible learned-memory reset |
| `npm run typecheck` and `npm run test:v1` before focused additions | 0 | TypeScript clean and 72 passed; frame-batched deltas, persistent reasoning, terminal thinking cleanup, task continuity, plain-language disclosure and full-window Artifact preview remain compatible |
| `python scripts/check-v1-public-download-site.py` | 0 | unpublished/null discovery pointer, five content-hashed assets, no legacy install inputs and correct immutable/no-store policies |
| Public discovery/release/control focused pytest set | 0 | 30 passed; exact manifest bytes, three-source receipts, atomic pointer and durable release signal behavior remain compatible |

The health checks are local deterministic evidence. Packaged event-loop/RSS/WAL
behavior under multi-hour Windows/macOS load, a real OTLP collector and
administrator-side correlated traces remain GA environment drills.

## Output authority - 2026-07-10

| Scope / command | Exit | Result |
| --- | ---: | --- |
| `python -m pytest -q tests/v1/test_output_service.py` | 0 | 14 passed, 1 skipped; account/single-database authority, preference CAS/idempotency, Turn-policy resolution, CAS integrity, root swap/link/reparse defense, 100-way names, crash recovery and internal-file rejection |
| `python -m pytest -q tests/v1/test_output_runtime_integration.py` | 0 | 1 passed; preference changed between two Turns, each Artifact materialized to its frozen destination and neither receipt exposed a host path |
| `npm run typecheck && npm run test:v1` | 0 | TypeScript clean and 80 passed; alias-only preferences, CSRF/revision/idempotency transport, default-location save receipt, GA behavior and all prior UI contracts remain green |

The symlink case is skipped only because the current Windows account lacks the
OS capability to create one; root replacement and reparse checks still run.
Real Known Folder redirection/OneDrive, network-backed workspaces and packaged
Windows/macOS permission prompts remain platform release drills.

## Thin Web bundle and lazy feature islands - 2026-07-11

| Scope / command | Exit | Result |
| --- | ---: | --- |
| Baseline `npm run build` | 0 | one 517.68 kB / gzip 161.21 kB JS entry, 1820 modules and Vite's over-500 kB advisory |
| `npm run typecheck` | 0 | lazy named exports, Suspense/error boundary and retained Settings system-health props type-check cleanly |
| `node --test tools/check-v1-bundle.test.mjs tools/lazy-feature-contract.test.mjs` | 0 | 6 passed; deferred import/boundary contract, system-health prop retention, no feature-name manual chunks, modulepreload rejection and entry-budget rejection |
| `npm run test:v1` | 0 | 80 passed; all prior reducer/API/Artifact/Retouch/Extension/Share/Replay/GA/rehash contracts, output-location transport and six new bundle tests remain green |
| `npm run build` | 0 | 1822 modules; entry 54.22 kB / gzip 15.93 kB, initial JS 452.51 KiB / gzip 140.10 KiB, six deferred features 64.13 KiB / gzip 22.47 KiB, 11 content-addressed assets and no large-chunk advisory |
| Post-rehash bundle gate | 0 | exact final assets satisfy 128 KiB entry, 475 KiB initial, 150 KiB gzip-initial and 500 KiB per-chunk budgets; no low-frequency feature is module-preloaded |
| `python scripts/check-v1-design-system.py` | 0 | zero hard-coded radius, shadow, raw color, numeric z-index, layout transition or `transition: all` findings after the loading/error surface addition |

The production build proves an acyclic content-addressed asset graph and bounded
download/parse inventory. It does not replace packaged browser evidence for
slow network fallback, focus restoration, screen-reader announcements or
Windows/macOS device-level LCP and INP.

## Session, Connector and Extension async hot paths - 2026-07-11

| Scope / command | Exit | Result |
| --- | ---: | --- |
| Managed Device + Session authority/Runtime, Connector contract/persistence/composition/gateway/mount and Extension platform focused pytest set | 0 | 102 passed, 1 Windows symlink-capability skip; lifecycle, credential secrecy, idempotency, uncertainty, restart, API and provider-revision fences remain compatible |
| New async responsiveness and backpressure cases | 0 | Slow SQLite/session/probe doubles do not stall the loop; Device timeout is lease-recoverable; a timed-out write remains uncertain; stuck outbox work has peak concurrency one; Extension timeout leaves the candidate inactive |
| `python -m compileall -q ecorex/session ecorex/connectors ecorex/extensions` | 0 | All edited production modules compile |
| Signed Product Runtime Device fixture rerun | 0 | 1 passed after the fixture supplied the Core `build_digest` required by Extension composition |
| Product Server + Runtime composition + system observability + Worker supervisor adjacency | 0 | 17 passed, 1 named environment skip; app lifespan, health aggregation and Worker supervision remain compatible |

The timeout tests intentionally use in-process deterministic doubles. They prove
bounded Runtime scheduling and durable local convergence, not that an arbitrary
third-party synchronous library can be force-killed. Production non-declarative
providers remain subject to the signed Capability-Pack/process boundary and the
final Windows/macOS outage and soak drills.

## v1 CI and reproducible bytes - 2026-07-11

| Scope / command | Exit | Result |
| --- | ---: | --- |
| Fresh venv `pip install -e ".[dev]"` | 0 | Installed EcoreX 1.0.0 plus pinned pytest 9.1.1, Ruff 0.15.21, jsonschema 4.26.0 and python-multipart 0.0.26 without system packages |
| Fresh-venv `python scripts/run-v1-lint.py` | 0 | v1 Runtime, tests and `check-v1-*` gates passed the configured correctness lint |
| Fresh-venv CI/public-index/Product focused pytest set | 0 | 41 passed, 2 named platform/environment skips; JSON Schema and FastAPI product imports are declared rather than inherited |
| `npm ci && npm audit --audit-level=high` | 0 | 132 packages audited, 0 vulnerabilities at execution time |
| `npm run typecheck && npm run test:v1 && npm run build` | 0 | typecheck passed, 87/87 tests passed, 11 production assets content-addressed; entry 55.05 KiB/gzip 16.11 KiB and initial JS 454.64 KiB/gzip 140.61 KiB remained within budget |
| Design + legacy cutoff + public download gates | 0 | zero design violations; legacy cutoff passed; canonical unpublished pointer and five public hashed assets passed |
| `python scripts/check-v1-reproducibility.py --web-dist desktop/dist` | 0 | canonical pointer, six LF v1 shell inputs, public JS/CSS and all built Web JS/CSS matched their recorded bytes/digest names |
| Workflow YAML parse + scoped `git diff --check` | 0 | new CI syntax parsed and edited files contain no whitespace errors |

The workflow defines Ubuntu x64, Windows x64, macOS arm64 and macOS x64 byte
contracts, but no hosted Actions run is claimed in this local ledger. The
runner labels follow GitHub's official hosted-runner reference. `npm audit` is
one dependency gate only; it is not SBOM, license, secret, release signature,
publication or installed-Runtime evidence. Native app notarization is outside
the WebUI scope.

## Complete image preview, focus and lazy-dialog accessibility - 2026-07-11

| Scope / command | Exit | Result |
| --- | ---: | --- |
| In-app Browser Artifact scenario at 1280×720 | 0 | card click opened one full-window preview; fit canvas/image both about 1213×505 px with no horizontal overflow; 125% produced a scrollable ~1498 px canvas; “显示完整图片” restored fit |
| In-app Browser focus checks | 0 | Settings close restored the Settings button; preview close restored the exact image Artifact card instead of `body` |
| `node --test tools/artifact-preview-contract.test.mjs` | 0 | 3 passed; card activation, zoom bounds/default fit and non-cropping `object-fit: contain` contract |
| `npm run typecheck && npm run test:v1` | 0 | TypeScript clean; 87 passed including visible-fallback focus checks and Radix loading/error modal semantics |
| `npm run build` | 0 | 1822 modules; 11 content-addressed assets; entry 55.05 KiB / gzip 16.11 KiB; initial JS 454.64 KiB / gzip 140.61 KiB; deferred features 64.20 KiB / gzip 22.52 KiB |
| Design + legacy cutoff + public site gates | 0 | all passed; six design-debt counters remain zero and the public pointer remains canonical unpublished/null |

Evidence files: `docs/v1.0/evidence/image-preview-fit-current.jpg`,
`image-preview-fit-1440.jpg`, `artifact-task-1440.jpg` and
`settings-output-health-1440.jpg`. These prove the named fine-pointer browser paths,
not the pending responsive/theme/touch/forced-colors/reduced-motion/axe matrix.

## Full-suite timing-harness repair - 2026-07-11

| Scope / command | Exit | Result |
| --- | ---: | --- |
| First current-tree `python -m pytest -q -p no:cacheprovider tests/v1` | 1 | 759 passed, 10 skipped, 2 failed under load; failures isolated to a collection-time frozen lease clock and a five-second Windows spawn readiness window |
| Immediate failing-node and file reruns | 0 | state invariant node passed; Update durability file passed 8 with 2 platform skips, confirming no reproduced product lock/lease failure |
| Repaired invariant + durability files | 0 | 19 passed, 2 named platform skips; clocks are scenario-local and child cleanup is bounded/diagnostic |
| Job/Update adjacency after repair | 0 | 42 passed, 2 named platform skips |
| Windows spawn readiness repeat | 0 | two spawn cases passed three consecutive runs; no assertion was skipped or weakened |

The initial full run is retained as failure evidence. It must not be described
as fully green; the authoritative post-repair full-suite result is recorded
only after the quiescent rerun completes.

## Historical quiescent local checkpoint (superseded) - 2026-07-11

| Scope / command | Exit | Result |
| --- | ---: | --- |
| `python -m pytest -q -p no:cacheprovider tests/v1` on a frozen worktree | 0 | 778 collected; 768 passed, 10 named platform/environment skips, 0 failed; 355.75 s |
| Python warning inventory | 0 | 1 Starlette multipart pending-deprecation and 4 real-WSS websockets/Uvicorn legacy-API deprecations; no product assertion warning |
| `npm run typecheck && npm run test:v1 && npm run build` | 0 | TypeScript clean; 87 passed; 11 content-addressed assets; all bundle budgets passed |
| CI/reproducibility gate | 0 | clean `.[dev]` environment, Ruff, 7 CI contracts, LF attributes including HTML, public/Web digest contract and four-runner workflow contract passed locally |
| `npm audit --audit-level=high` | 0 | 132 packages audited; 0 vulnerabilities at execution time |
| Design + legacy cutoff + public download + compile/CLI | 0 | all local static/compile/help gates passed; design debt counters remain zero |

This supersedes the old 673-Python/56-Web checkpoint as the current local
source-tree result. It is not a signed immutable-candidate, hosted Actions,
platform Runtime archive, real-provider, live-origin, OTLP or production migration
claim.

## Responsive fixed-viewport GA harness - 2026-07-11

| Scope / command | Exit | Result |
| --- | ---: | --- |
| `node --experimental-strip-types --test tools/ga-mock-server.test.mjs` | 0 | 5 passed; fixed viewport/theme matrix, production/frame CSP split, no-store responses and strict injection/recursion rejection |
| `npm run test:v1` | 0 | 89 passed; 2 new responsive harness contracts plus all prior Web v1 contracts |
| In-app Browser discovery in this subtask | N/A | No browser backend was exposed to the subtask; no screenshot or rendered-DOM pass is claimed here |

The canonical matrix endpoint is `/__ga/viewport-matrix`; each returned URL
renders one exact-size same-origin frame and publishes its inspection object as
`window.__ECOREX_GA_VIEWPORT_REPORT__`. Production `/` remains non-frameable.
See `responsive-ga-harness.md` for the operator procedure and security model.

## Real responsive/theme/axe Browser matrix - 2026-07-11

| Scope / command | Exit | Result |
| --- | ---: | --- |
| In-app Browser `/__ga/viewport-matrix` | 0 | 8/8 exact viewport/theme entries passed; all key controls present/visible and horizontal overflow 0 |
| axe-core 4.12.1 inside isolated GA frames | 0 | every entry reported 0 violations and 0 incomplete checks; 33–36 passing rules per frame |
| `npm run test:v1` after accessibility fixes | 0 | 89 passed; semantic page title, grouped controls, compact navigation names and overflow fixes retained |
| `npm audit --audit-level=high` | 0 | 133 packages audited; 0 vulnerabilities after adding axe as a development-only dependency |

Evidence: `evidence/responsive-axe-matrix.json` plus the eight
`evidence/responsive-*-{light,dark}.jpg` files. This closes the named responsive,
theme and axe matrix. Coarse-pointer hardware, forced-colors browser emulation,
screen-reader review and reduced-motion browser emulation remain distinct
gates; source contracts do not masquerade as those real-environment checks.

## Windows x64 signed WebUI Runtime candidate drill - 2026-07-11

| Scope / command | Exit | Result |
| --- | ---: | --- |
| `python scripts/drill_v1_windows_signed_candidate.py --timeout-seconds 300 --report docs/v1.0/evidence/windows-x64-signed-candidate-drill.json` | 0 | 267.7 s; Ed25519 ReleaseBuilder candidate installed, relaunched and returned authenticated bootstrap 200; separately signed fault candidate rolled back before data use and baseline again returned 200 |
| Product entrypoint/Bootstrap/drill focused suite | 0 | 45 passed, 2 named environment skips |
| Bootstrap/update activation adjacency | 0 | 37 passed, 1 named platform skip |
| Post-drill process/temp audit | 0 | candidate processes absent, temporary candidate directory removed, private key not persisted, external publication false |

The report binds release/build/Web/Core/manifest digests and explicitly records
`os_application_signing_used: false`. It proves a local Windows x64 WebUI
Runtime archive path, not Authenticode, Electron, an EXE installer, hosted CI or
live mirror/GitHub/CDN publication.

## Bounded media and timeline rendering - 2026-07-11

| Scope / command | Exit | Result |
| --- | ---: | --- |
| Artifact preview cache unit suite | 0 | 3 passed; request dedupe/concurrency, entry+byte LRU/revocation, revision abort and oversized rejection |
| Timeline window unit suite | 0 | 4 passed; latest 120 bound, history anchor stability under new deltas, stale-anchor recovery and invalid-limit rejection |
| Preview + timeline + interaction static contracts | 0 | 11 passed; full-fit preview, near-viewport media, dialog response fence, frame batching, DOM window, forced colors, reduced motion, coarse pointer and clipboard denial |
| Interim `npm run typecheck && npm run test:v1` | 0 | TypeScript clean; 97 passed before the generated-contract integration batch |
| Interim `npm run build` + design gate | 0 | 11 content-addressed assets; entry 56.69 KiB / gzip 16.65 KiB, initial JS 459.32 KiB / gzip 142.21 KiB; six design-debt counters 0 |
| `python scripts/check-v1-legacy-cutoff.py --strict-production` after WebUI-only cleanup | 0 | native build entitlements/icons/DMG notes and dead Electron bridge inputs remain absent |

This interim checkpoint is intentionally superseded by the next full Web run
after generated contracts are integrated; it remains useful as fault-localized
evidence for the rendering changes.

## Single v1 administrator/release Web path - 2026-07-11

| Scope / command | Exit | Result |
| --- | ---: | --- |
| `python scripts/build-v1-public-download-site.py` | 0 | retired-host logic moved to external module; atomically rebound `site.0aaef34e8429.js` and updated HTML digest |
| `python scripts/check-v1-public-download-site.py` | 0 | canonical unpublished pointer, five hashed assets, no inline code, only `/admin/` Control Plane link, no legacy public Runtime/Basic-Auth admin proxy, strict cache/CSP contract |
| `python -m pytest -q tests/v1/test_public_download_site.py tests/v1/test_control_plane_admin_web.py` | 0 | 12 passed; public parser/byte verification plus content-addressed CSP-safe admin console and durable resume projection |
| `python scripts/check-v1-legacy-cutoff.py --strict-production` | 0 | removed static admin, Admin API, usage panel, old proxy snippets and old install/check/migration scripts cannot return as v1 production inputs |

No live Control Plane deployment or administrator IdP is claimed: production
must inject the real authenticator/verifier/repository before the proxy target
at `127.0.0.1:18084` is considered ready.

## v0.3.0 copy-on-write migration closure - 2026-07-11

| Scope / command | Exit | Result |
| --- | ---: | --- |
| `python -m pytest -q tests/v1/test_migration_copy_on_write.py` | 0 | 16 passed; conversation/UI-cache, run/event, branch, recovery draft, scheduler, permission, live-WAL source isolation, Artifact/CAS, quarantine, idempotency, malformed-input and source-immutability coverage |
| `python -m ruff check ecorex/migration tests/v1/test_migration_copy_on_write.py` | 0 | migration implementation and focused tests clean |
| `python -m compileall -q ecorex/migration` | 0 | all migration modules compile |
| Local Git object audit (`f0750d24` vs `9ac3b958`) | 0 | seven released data/state source blobs are byte-identical; exact blob IDs are recorded in `evidence/v030-migration-baseline.json` |

This gate proves a fail-closed local migration implementation and synthetic
fixtures built from the released DDL. It does not claim that a historical
551+ MiB release archive was downloaded or re-hashed. Old active runs and
schedules are deliberately staged for confirmation, not resumed automatically.

## Candidate migration, image storage and generated contracts - 2026-07-11

| Scope / command | Exit | Result |
| --- | ---: | --- |
| Candidate storage migration focused + pre-data/roll-forward suites | 0 | 24 passed, 2 named Windows-link skips; signed v1→v2 CoW admission, poison-code non-execution, receipt binding and data-barrier ordering |
| Release/activation migration adjacency | 0 | 122 passed, 4 named environment/platform skips; the only interim failure was a concurrently changing Web bundle budget and was later removed |
| Image/orchestrator/retouch Runtime adjacency | 0 | 70 passed; local/shared topology, PostgreSQL schema validation, S3 CAS/ETag/tombstone recovery, bounded pool/upload/worker behavior |
| Generated contract codegen + Web suite before density batch | 0 | codegen current, TypeScript clean, 113 Web tests passed and production bundle remained under its 475 KiB initial-JS limit |

This was the pre-service checkpoint. The later real PostgreSQL/MinIO drill below
supersedes its shared-storage limitation; hosted provider credentials and a
multi-hour multi-node soak remain explicit release gates.

## Real shared image storage and crash recovery - 2026-07-12

| Scope / command | Exit | Result |
| --- | ---: | --- |
| `python -m pytest -q tests/v1/test_image_orchestrator_real_shared_storage.py` | 0 | real PostgreSQL 16.9 + MinIO run: 256 unique jobs, 48 workers and 16 duplicate submissions completed exactly once, preserving every reference and one shared result blob in 123.85 s |
| Real-service fault sequence inside the integration test | 0 | expired owner fenced and attempt 2 reclaimed; MinIO process pause failed closed in 7.297 s then recovered; PostgreSQL process restart produced bounded failure, stale-token fencing and attempt-2 recovery in 5.297 s; ETag/tombstone GC removed the object |
| `python -m pytest -q tests/v1/test_image_orchestrator_production_storage.py` | 0 | 56 passed against the real PostgreSQL catalog path |
| Focused image-orchestrator source suite | 0 | 90 passed, 2 environment skips |

Machine-readable evidence:
`evidence/image-shared-storage-real-2026-07-11.json`. The services were pinned by
container digest. This bounded Windows/Docker loopback drill used HTTP MinIO and
is not evidence for production HTTPS/private-bucket/KMS policy, a hosted image
provider or a multi-hour multi-node soak.

## ShareSnapshot v2, sparse controls and durable chat order - 2026-07-11

| Scope / command | Exit | Result |
| --- | ---: | --- |
| Sharing snapshots/jobs/transport/media/Control Plane/key rotation | 0 | media-before-snapshot, retry identity, account isolation, token/revoke/expiry, tamper, SVG, request limit/backpressure, orphan quota/reclaim and v1 compatibility passed |
| Web density/type gate | 0 | ordinary controls transparent at rest, semantic exceptions preserved, sparse message/connector/Artifact rows and system-font tokens enforced |
| Runtime projection + reducer order tests | 0 | equal timestamps and reverse-sorting opaque IDs retained first Event/projection order in Python and React |
| In-app Browser main WebUI | 0 | 1280×720; idle 13/20 ordinary control had transparent frame/surface; message order `你 → EcoreX`; media actions hidden and non-interactive at rest |
| In-app Browser real public share | 0 | desktop and 390×844; order `你的指令 → EcoreX`, image loaded from token-bound endpoint with preserved aspect/contain, zero horizontal overflow, zero page scripts |

Evidence: `evidence/share-chat-browser-audit.json`. The browser fixture is
loopback-only and uses the real Control Plane renderer/media route; no public
share or external Artifact bytes were published.

## Current-tree authoritative checkpoint - 2026-07-11

| Scope / command | Exit | Result |
| --- | ---: | --- |
| `python -m pytest -q tests/v1/test_sharing_snapshots.py tests/v1/test_sharing_durable_jobs.py tests/v1/test_sharing_transport.py tests/v1/test_sharing_media.py tests/v1/test_control_plane_sharing.py tests/v1/test_cloud_share_key_rotation.py` | 0 | 49 passed; media-before-snapshot, stable retry identity, quotas/reclaim, token/revoke/expiry, key rotation and schema-v1 canonical compatibility |
| Core storage + Output/Artifact adjacency | 0 | 29 core-hardening tests and 46 Output/Runtime/Artifact adjacency tests passed; non-empty unversioned, missing-column/index/trigger and same-name trigger drift remain unchanged and fail closed |
| `python -m pytest -q -p no:cacheprovider tests/v1` on the final frozen tree | 0 | 828 passed, 11 named platform/environment skips, 0 failed; 321.08 s; one Starlette and four real-WSS dependency deprecation warnings only |
| `npm run typecheck && npm run test:v1` | 0 | generated contracts current; TypeScript clean; 118/118 Web tests passed |
| `npm run build` | 0 | 1826 modules; 11 content-addressed production assets; entry 57.74 KiB/gzip 16.97, initial JS 474.02 KiB/gzip 146.51, deferred 64.21 KiB/gzip 22.53; all budgets passed |
| `python scripts/check-v1-design-system.py` | 0 | five strict files; hardcoded radius/shadow/layout transition/numeric z-index/raw color/`transition: all` counts all zero |
| v1 lint + strict legacy cutoff + public download + reproducibility | 0 | all passed; public pointer canonical unpublished with five hashed assets; byte contract contains only the current fail-closed v1 shell identity inputs and all built digest-named assets |
| `git diff --check` | 0 | no whitespace errors; checkout line-ending notices are informational |
| In-app Browser main/share paths | 0 | ordinary controls measured transparent at rest with 13/20 UI type; Event order `你 → EcoreX`; token-bound shared image retained contain/aspect and zero overflow at desktop and 390 px |

This checkpoint supersedes the earlier 768-Python/87-Web and interim
97/113-Web rows. It is authoritative for the current local source tree only;
hosted archives, immutable candidate rerun, real PostgreSQL/S3/provider/origin,
public Share deployment and real v0.3.0 corpus evidence remain separate gates.

## Protected Candidate automation - 2026-07-11

| Command / check | Result | Boundary |
| --- | --- | --- |
| `python -m pytest -q -p no:cacheprovider tests/v1/test_candidate_release_pipeline.py` | 0 | 11 passed; stdin-only external signer, executable/adapter digest substitution, 3 Core + 9 Pack ReleaseBuilder round trip, schema validation, mutation/placeholder/embedded-secret rejection, typed failure receipt, protected-workflow contract, staging provenance, release-scoped channel roots and exact evidence assembly |
| release builder/GitHub/replica/publication/storage-migration adjacency (eight focused files) | 0 | 77 passed, 2 environment/platform skips; deterministic source-bound identity, stable/unique-canary tags, double-signed Packs, three-origin publication and migration contracts remain green |
| `python scripts/check-v1-candidate-supply-chain.py preflight --repo . ...` | 0 | 21 Python runtime/direct+transitive packages and 278 Node packages classified with no denied license; 326 production files passed high-confidence secret patterns |
| `python scripts/run-v1-lint.py` and targeted `ruff`/`py_compile` | 0 | new release modules, operator scripts and tests pass source gates |
| Draft 2020-12 schema self-check + PyYAML parse | 0 | Candidate recipe/stage receipt schemas and both workflow YAML documents parse; this is syntax evidence, not GitHub Environment or runner execution evidence |

No live signing, upload or rollout was attempted locally. The protected
self-hosted platform/sign/publish runners, production stager, Windows helper,
KMS/HSM workload identity, mirror/CDN credentials and Control Plane remain
external evidence gates.

## Final local productization checkpoint - 2026-07-11

| Scope / command | Exit | Result |
| --- | ---: | --- |
| `python -m pytest -q -p no:cacheprovider tests/v1` | 0 | 1208 passed, 14 named platform/environment skips, 0 failed; 415.32 s; one Starlette and four real-WSS dependency deprecation warnings |
| `npm run typecheck && npm run test:v1 && npm run build` | 0 | generated contracts current; TypeScript clean; 138 tests; 17 content-addressed assets; entry 51.75 KiB/gzip 15.03, initial JS 474.05 KiB/gzip 146.64, deferred 77.48 KiB/gzip 28.40 |
| Post-rehash JS syntax gate | 0 | every final `.js` chunk reparsed from disk; corrupt-chunk and overlapping dynamic-import quote regressions pass |
| In-app Browser final WebUI | 0 | final production-hashed entry loaded with zero console error/warn; Artifact card opened full fit/contain preview; 390 px action target measured 44×44 px |
| Browser viewport/theme/axe matrix | 0 | 1440×900, 1024×768, 768×900 and 390×844 in light/dark passed 8/8; zero horizontal overflow and zero axe violations |
| `python scripts/run-v1-lint.py` | 0 | repository v1 source gate passed after routing/Pack/browser hardening |
| Runtime/server schema authority | 0 | 17 Runtime fragments and 7 server schema authorities; zero violations |
| strict legacy/public/design gates | 0 | legacy cutoff, public site and design debt all passed; radius/shadow/layout transition/numeric z-index/raw color/`transition: all` counters remain zero |
| dependency lock gate | 0 | bootstrap 3, Runtime 21, cloud 32, dev 33 and platform-stage 24 packages; manifest `c452d89bf9215c89c00638bc7bf39a0eed89a29fd3a63a5917c5abf3d691fa85` |
| candidate supply-chain preflight | 0 | lock/license/secret/size/workflow checks passed; report `evidence/candidate-supply-chain-local-final.json` |
| `npm audit --audit-level=high` | 0 | 278 locked Node packages; 0 reported vulnerabilities at execution time |
| reproducibility byte contract | 0 | final Web/public/lock/shell identities written to `evidence/byte-contract-local.json` |
| local Windows signed-candidate drill | 0 | 352.7 s; background download, explicit activation, bootstrap 200, immutable/no-store cache, signed fault rollback and cleanup passed |
| `git diff --check` | 0 | no whitespace errors; checkout line-ending notices remain informational |

Evidence: `evidence/webui-browser-qa-2026-07-11.json`,
`evidence/platform-pack-local-2026-07-11.json`,
`evidence/dependency-byte-contract-local.json`,
`evidence/candidate-supply-chain-local-final.json` and
`evidence/windows-signed-candidate-current.json`.

The Windows drill intentionally exercises the signed Core-only compatibility
branch. It does not replace a protected-runner Candidate containing the compiled
Windows helper and exact browser/image/sandbox Pack set. Real macOS, native
sandbox, PostgreSQL/S3/provider/connector/origin/OTLP and installed-v0.3.0 data
evidence remains external and keeps the long Goal active.

## Automatic Bootstrap freshness renewal - 2026-07-12

| Scope / command | Exit | Result |
| --- | ---: | --- |
| `python scripts/check-v1-server-schema-authority.py` | 0 | eight server schema authorities; zero DDL violations |
| Combined Bootstrap index/pointer/saga, Control Plane schema/release, admin client/CLI and production serve tests | 0 | 73 passed; same-target renewal, no-rollout, startup catch-up, durable idempotency, restart and post-activation crash recovery |
| `python -m pytest -q tests/v1/test_bootstrap_index_publication_saga.py` | 0 | 18 passed; near-expiry, not-due, KMS/publish/readback failure, concurrent lease ownership, missing signer and exact-byte resume |
| `python -m pytest -q tests/v1/test_control_plane_production_serve.py` | 0 | 14 passed; lifespan integration, missing-signer no-active readiness and digest-pinned external publication signer composition |
| Focused Ruff and `py_compile` | 0 | public-index, schema, repository, refresher, app, production, models, client and CLI clean |

This is deterministic local evidence. A credentialed production KMS/HSM
signature, real public HTTPS CAS/readback, alert delivery and multi-hour outage
recovery remain deployment gates; no release target or rollout was changed.

## Post-QA Bootstrap/release hardening - 2026-07-12

| Scope / command | Exit | Result |
| --- | ---: | --- |
| Freshness, production, public-index, admin client/CLI and bounded external signer tests | 0 | 58 passed; startup-without-pointer then late activation, task/heartbeat/error readiness, bounded retry, raw-key alias overlap, target drift and cross-process request replay |
| Final release/security/schema adjacency suite | 0 | 106 passed; includes Candidate signer boundary, ReleaseBuilder security, pointer publication, Control Plane release flow and schema manager |
| Focused Ruff + `py_compile` | 0 | release signing/index/external signer, update verifier, freshness scheduler, production composition, models and CLI clean |
| `python scripts/check-v1-server-schema-authority.py` | 0 | eight server authorities; zero violations |

External signer tests use real child/descendant processes on Windows and prove
timeout tree termination locally. Hosted KMS/HSM identity, CDN propagation and
multi-region object CAS/readback remain protected-environment gates.

## Batch-scoped Skill resource disclosure - 2026-07-12

| Scope / command | Exit | Result |
| --- | ---: | --- |
| Skill-focused Extension Runtime/Worker tests | 0 | 6 passed; exact discovery ID, no path/CAS leakage, canonical result-digest verification, cross-Skill/reference rejection, forged-result recomputation, cross-batch denial and restart reconstruction |
| Extension, capability, planner, composition and Tool disclosure adjacency | 0 | 165 passed, 1 existing environment skip |
| Agent Worker, durable admission, execution schema, state-machine and Turn-input adjacency | 0 | 61 passed |
| `py_compile` plus `python scripts/run-v1-lint.py --compile` | 0 | Skill/Runtime modules compile and the full v1 source gate passes |

The evidence proves the local Runtime contract and durable restart behavior. It
does not claim protected-runner Extension bundles, signed third-party Skill
publisher evidence or a long-duration multi-process production soak.

## Attempt-bound cross-runner Candidate gate - 2026-07-12

| Scope / command | Exit | Result |
| --- | ---: | --- |
| CI provenance, release integrity, dependency lock and Candidate pipeline focused suites | 0 | 45 passed; exact CI attempt metadata, four immutable Artifact IDs, archive/content digest domains, `@main` path normalization, signed-Candidate binding and raw-evidence rejection |
| Control Plane release/admin/WSS adjacency | 0 | 56 passed; adding the required `reproducibility` gate preserved candidate, rollout and update-notification contracts |
| `python scripts/check-v1-dependency-locks.py` | 0 | exact Candidate profile use updated to 3 cloud, 3 dev and 4 Runtime installs; lock manifest remains `c452d89bf9215c89c00638bc7bf39a0eed89a29fd3a63a5917c5abf3d691fa85` |
| `python scripts/run-v1-lint.py --compile` | 0 | new provenance selector/verifier, typed receipt logic and tests passed lint and compile inventory |
| PyYAML parse of all three v1 workflows | 0 | Candidate, CI and platform-stage workflow syntax parsed after exact-ID integration |

This is local contract evidence only. The protected-main CI run, its four
GitHub-hosted Artifact IDs and the protected Candidate/KMS workflow must still
execute before the release gate is factually passed.

## GPT-5.6 SOL managed chat policy - 2026-07-12

| Scope / command | Exit | Result |
| --- | ---: | --- |
| Capability/catalog/Runtime/worker/Gateway focused pytest set | 0 | 219 passed; stable `ecorex-chat` ID, `gpt-5.6-sol` alias/upstream policy, reasoning capability, frozen policy snapshots, exact Gateway request/event policy and progressive capability discovery compatibility |
| Managed Gateway/server/schema/supervisor/import adjacency | 0 | 57 passed, 1 existing environment skip; request identity, quota, server protocol, schema and cold imports preserved |
| Policy-focused catalog/composition/provider/worker pytest set | 0 | 66 passed; production wrong-upstream rejection, unchanged secret adapter, exact `medium` reasoning projection, exact 272000 compaction trigger and opaque compaction stream item acceptance |
| `python desktop/tools/generate-runtime-contracts.py --check` plus codegen test | 0 | authoritative Bootstrap schema regenerated; canonical schema digest `aab4214342291fab05d3b2cf1b2a3c6dd5a44ec96e2ed1d2cffd58b1cd7bd650` |
| WebUI `npm run typecheck` and focused Runtime-client/model-selection tests | 0 | TypeScript clean; 37 tests passed; backend model policy is mapped and validated without frontend routing authority |
| `python scripts/run-v1-lint.py --compile` | 0 | full v1 source lint and compile inventory passed after the policy and Web contract changes |

No real provider request or >272000-token long-context run was made. The
contract sends a genuine Responses server-side compaction trigger on every
request, but a credentialed run observing the provider's encrypted compaction
item is still required before marking production compaction execution proven.

## Batch-scoped Tool disclosure closure - 2026-07-12

| Scope / command | Exit | Result |
| --- | ---: | --- |
| Tool disclosure, Agent Worker, invocation admission and execution-schema focused suites | 0 | 42 tests collected; component runs all passed, covering exact discovery ID, forged/alias/stale/cross-batch rejection, restart reconstruction and immutable batch identity |
| Capability planning/invocation/discovery, Runtime composition, Pack, Extension and managed-image adjacency | 0 | 167 passed; progressive disclosure remained compatible with Core, Pack, MCP and image paths |
| Runtime state-machine, Turn input, Replay, product server and schema authority adjacency | 0 | 35 passed, 1 existing environment skip; Runtime schema authority reported 19 fragments and zero violations |
| Final composition/Pack/server/schema/admission recheck | 0 | 37 passed, 1 existing environment skip after the model-facing input was narrowed to `discovery_id` |
| `python scripts/run-v1-lint.py --compile` | 0 | full v1 lint and compile inventory passed |

This closes the Tool Search -> exact Describe -> durable Grant P0 only. Skill
resource grants remain a separate release blocker. The model-context budget was
not changed by that batch and is closed by the subsequent entry below.

## Model-visible Tool projection budget - 2026-07-12

| Scope / command | Exit | Result |
| --- | ---: | --- |
| Gateway, Worker, capability planning/discovery/invocation, deferred authority, MCP/Extension and admission adjacency | 0 | 242 passed; no failures; count/byte fences preserve Search -> Describe -> Call, permission admission and non-idempotent uncertainty semantics |
| Budget-focused Gateway/Provider/Worker tests | 0 | provider flood, exact descriptor limit, aggregate batch limit, 16/12 working-set limits, Core-first projection, restart determinism, observable suppression and pre-dispatch rejection passed |
| `python scripts/run-v1-lint.py --compile` | 0 | full v1 source lint and compile inventory passed |
| `python desktop/tools/generate-runtime-contracts.py --check` | 0 | generated thin-WebUI Runtime contracts remain current; the budget is Gateway/Worker authority, not frontend policy |

This evidence is deterministic local contract coverage. It does not claim a
live protected provider account was load-tested with a maximum-size tool
projection; that provider soak remains a deployment performance gate.

## Share image rendition fail-closed contract - 2026-07-12

| Scope / command | Exit | Result |
| --- | ---: | --- |
| Sharing snapshots/jobs/transport/media/Runtime integration/Cloud schema and object suites | 0 | 91 passed; normal token-bound image publication, retry identity, missing rendition, revoke/expiry and schema-v1 canonical compatibility preserved |
| All `test_control_plane_*.py` suites | 0 | 113 passed; typed missing/over-16-MiB/unsupported/over-64-MiB rejection, no secret echo, publish/revoke/expiry and release/admin adjacency passed |
| Focused share media/transport/Control Plane/key rotation | 0 | 45 passed; new issuance is schema v2, historical schema v1 stays readable, invalid images create no public snapshot |
| Web user-language test and `npm run typecheck` | 0 | 3 language tests passed; generated contract check and TypeScript passed |
| `python scripts/run-v1-lint.py --compile` | 0 | full v1 lint/compile inventory passed |

The tests use local CAS and the real local Control Plane object path. They do
not claim a deployed public origin or production S3 media lifecycle drill.

## Provider provenance and fair Tool Search - 2026-07-12

| Scope / command | Exit | Result |
| --- | ---: | --- |
| `python -m pytest -q tests/v1/test_capability_provider_provenance.py` | 0 | 4 passed; forged MCP-as-Core/reviewed claims fail, one 40-tool provider cannot monopolize a five-result search, exact `limit=1` wins, restart bytes/digests are stable, and exact verified manifest provenance contains no signature body |
| Capability planner + immutable snapshot focused suites | 0 | 104 passed after the discovery-policy version assertion was advanced to `1.2.0`; provider provenance survives repository reconstruction |
| Extension execution + platform suites | 0 | 48 passed, 1 existing platform skip; mandatory verified MCP binding, signature-evidence recheck, contribution snapshot and protocol/runtime fences remain green |
| Final capability/Pack/MCP/Worker/disclosure adjacency | 0 | 199 passed, 1 existing platform skip; no regression in durable Describe grants, invocation admission, model projection or Extension execution |
| `python scripts/run-v1-lint.py --compile` | 0 | full v1 source lint and compile inventory passed |
| Runtime schema authority + generated Runtime contract + `git diff --check` | 0 | 19 schema fragments, zero violations; generated thin-WebUI contract is current; no whitespace errors (checkout line-ending notices only) |

These are deterministic local Core-bundle and SQLite tests. They do not claim
third-party publisher onboarding, protected signing-key rotation or a
multi-process MCP flood/latency soak in a deployed environment.

## Image concurrency stability closure - 2026-07-12

| Scope / command | Exit | Result |
| --- | ---: | --- |
| `python -m pytest -q tests/v1/test_image_concurrency_stability.py` | 0 | 9 passed; 32-way queued-deadline/restart race emits one terminal event and releases capacity, PostgreSQL locked deadline/rate-limit contracts, 16 concurrent Workers admit one durable half-open probe, scope-wide 429 fencing then one probe, bounded/malformed Retry-After behavior, slow-CAS heartbeat, staged-result crash recovery and lost post-commit response resolution |
| Image orchestrator Store/Runtime/provider/schema adjacency (six focused files) | 0 | 117 passed, 1 existing environment skip; prior idempotency, fairness, lease fencing, shared-storage contracts, process drain, memory envelope and explicit schema authority remain green |
| Expanded image/Retouch/Artifact adjacency (eleven focused files) | 0 | 174 passed, 1 existing environment skip; includes structured Retouch execution, Runtime publication, Artifact CAS hardening and 100 same-minute unique Artifact names |
| `python scripts/run-v1-lint.py --compile` | 0 | full v1 lint and compile inventory passed after the concurrency closure |

The provider in the new concurrency suite is a deterministic controlled fake.
The tests prove local state-machine and recovery semantics, not production
throughput, real-provider billing deduplication, multi-region storage behavior
or a 24-hour soak. Those remain external GA gates in the image production
runbook.

## Hallmark WebUI interaction closure - 2026-07-12

| Scope / command | Exit | Result |
| --- | ---: | --- |
| `npm run test:e2e` | 0 | 20 passed; five viewports from 1440×900 through 320×568 in light/dark had zero overflow, wrapped clickable labels or axe violations; WorkspaceSurface, reasoning/terminal order, retry, HITL, 320px queue, Chinese share UI, image fit/zoom, forced-colors, reduced-motion and touch More passed |
| `npm run test:v1` and `npm run typecheck` | 0 | 154 Web tests passed; TypeScript and generated Runtime contracts are current |
| `npm run build` | 0 | 2080 modules; content-addressed build passed at 474.99 KiB initial JS (146.95 KiB gzip), below the unchanged 475 KiB ceiling |
| `python scripts/check-v1-design-system.py` | 0 | five strict CSS files; all six prohibited design-debt counters remain zero |

The audit found one production interaction defect: `<640px` hid the whole
steer/queue/replace selector. It remains visible in the stacked Composer and a
320px touch case now queues a Turn end to end. The GA fixture was also updated
to the current `gpt-5.6-sol` model policy and event envelope so contract drift
fails visibly. Evidence is local Chromium, not physical-device, screen-reader
or public-share-origin certification.

## Connector-login HITL Web closure (provisional) - 2026-07-12

| Scope / command | Exit | Result |
| --- | ---: | --- |
| `npm run test:e2e` | 0 | 25 passed; OAuth popup, device code, dedicated cancel, partial-scope reauthorization and interrupted-completion retry all passed with generic `/respond` count zero |
| Focused Runtime-client/HITL/GA tests | 0 | 50 passed; exact dedicated begin/check/cancel paths, CSRF, empty bodies, backend-authoritative projection and fail-closed routing covered |
| Direct `npx tsc --noEmit` | 0 | TypeScript clean against the in-flight ergonomic contract |
| Vite content-addressed build and bundle check | 0 | 2080 modules; initial JS 474.68 KiB / gzip 146.77 KiB, deferred features 91.61 KiB / gzip 33.04 KiB |
| `python scripts/check-v1-design-system.py` | 0 | five strict CSS files; all six prohibited design-debt counters remain zero |

These are interim Web results. `contracts:check`, full `test:v1` and the final
production build must be rerun after the connector Runtime schema/codegen is
frozen; no deployed OAuth provider was contacted by the GA harness.

## Connector progressive disclosure and crash fencing - 2026-07-12

| Scope / command | Exit | Result |
| --- | ---: | --- |
| Connector agent/crash/persistence/Runtime mount/composition/admission focused suite | 0 | 99 passed; exact Search/Describe/Call, informed write approval and descriptor-swap rejection, permission-race HITL, login generation recovery, late-success replay, operation reconciliation and autonomous disconnect recovery passed |
| `python scripts/check-v1-runtime-schema-authority.py` | 0 | 20 schema fragments, zero violations; `connector-agent-runtime` is registered authority |
| `python -m pytest -q tests/v1/test_runtime_schema_authority_gate.py` | 0 | 2 passed |
| Focused `python -m ruff check ...` | 0 | Connector, Runtime worker/API/repositories, tests and schema gate clean |
| `npm run contracts:generate` then `npm run contracts:check` | 0 | Runtime Web contract regenerated at `fab0419491028be73daa66b708f328b12bf7a2be4ec7897e1d7ce4f00f08a114` and byte-current |

Coverage uses deterministic local adapters and SQLite crash injection. It does
not certify live Feishu/Tencent tenants, a deployed multi-process provider soak,
or atomic promotion of oversized Connector results into Artifact CAS; those
remain explicit external/Phase-2 gates.

## Runtime consistency and bounded delivery closure - 2026-07-12

| Scope / command | Exit | Result |
| --- | ---: | --- |
| Job permit generation/rollback plus Job/Interaction/state/recovery adjacency | 0 | 13 permit tests and 28 adjacency tests passed; an old lease publisher cannot replace a newer permit, and rollback cannot retire a live permit |
| Permission/Turn create, queue, replace, Live Replay and Retouch concurrency | 0 | 67 combined tests passed; permission update cannot return before old acceptance, and a post-update stale snapshot is rejected before `turn.accepted` |
| Permission verified sample and invocation admission | 0 | 17 focused tests passed; one governance call uses 4 permission SELECTs instead of 12, the next call verifies afresh, and cross-process revocation causes zero provider calls |
| Artifact event outbox, Runtime gate/shutdown and observability | 0 | 46 combined tests passed; claim-to-dispatch epoch validation, lease heartbeat, provider timeout, pending recovery and nested commit guards covered |
| Connector complete adjacency after red-team fixes | 0 | 150 passed; generation single-flight, old daemon fencing, stuck circuit, consistent health snapshot, final flush hard deadline and phase ordering covered |
| Connector independent repeated red-team | 0 | 80/80 existing race repetitions plus ten rounds each for idle-boundary nudge, old-daemon return, sink-success/Gate-close, child-process stuck shutdown and backlog convergence |
| Cloud Share concurrent migration stress | 0 | 300 rounds x 8 callers = 2,400 successful migrations; identical receipts, one history row, WAL reasserted; non-lock I/O is not retried |
| Capability/RuntimeComposition/product-server handler authority | 0 | 146 adjacency tests passed; five non-replaceable Core handlers clear only proven stale-missing facts and preserve unrelated denials |

The Connector provider and EventStore used by these tests are deterministic
local implementations. The evidence proves bounded local lifecycle and crash
semantics, not a production tenant, remote sink or multi-hour deployed soak.

## Final frozen local v1 gate - 2026-07-12

| Scope / command | Exit | Result |
| --- | ---: | --- |
| `python -m pytest -q --junitxml=.candidate/quality/full-pytest-20260712-175948.xml` | 0 | 1,786 collected: 1,769 passed, 17 platform-condition skips, 0 failures, 0 errors; 805.827 seconds in JUnit / 806.06 seconds pytest summary |
| `python scripts/run-v1-lint.py --compile` | 0 | all v1 Python/static compile checks passed |
| Runtime and server schema authority | 0 | 20 Runtime fragments; 8 server authorities across 3 roots; zero violations |
| Strict design and legacy cutoff | 0 | five strict CSS files; all six debt counters zero; retired production Runtime cutoff passed |
| Dependency lock and public download gates | 0 | 282 npm packages; Python profiles 3/32/33/24/21; lock manifest `c452d89bf9215c89c00638bc7bf39a0eed89a29fd3a63a5917c5abf3d691fa85`; unpublished public pointer and five hashed public assets valid |
| Actual Candidate supply-chain preflight | 0 | `.candidate/quality/supply-chain-local-final.json`: dependency lock, Python/Node license inventory and 433-file secret scan passed; secret inventory SHA-256 `72a4b7695368a89728755a054ee01bc2c52696248a4287c2fd0833d5f8654d5a` |
| Local reproducibility and generated Runtime contract | 0 | byte contract contains 36 files / 17 Web assets; generated schema is current; `git diff --check` has zero whitespace errors |
| `npm run test:v1` | 0 | 158 passed; generated Runtime contract check included |
| `npm run typecheck` | 0 | TypeScript clean |
| `npm run build` | 0 | 2,080 modules; 17 content-addressed assets; entry 51.22 KiB, initial JS 471.92 KiB / gzip 145.82 KiB, deferred 94.56 KiB / gzip 33.81 KiB, 16 chunks |
| `npm run test:e2e` | 0 | 25 Chromium scenarios passed across 1440, 1024, 768, 390 and 320 widths, light/dark, axe, keyboard/touch, forced-colors and reduced-motion |
| `python scripts/check-v1-source-tree.py` | 1 | expected release blocker: 608 authoritative files, 3 tracked and 605 untracked; no gate weakening or implicit staging |
| Independent untracked source content pre-scan | 0 | all 608 are regular UTF-8 LF files; zero symlink, NUL/binary, CRLF, missing-final-LF or trailing-whitespace findings |

The source-tree gate is not replaced by the content pre-scan: Git admission
followed by the real gate is still required. No protected CI, Candidate
signature, publication or rollout is claimed by this local checkpoint.

## Managed GPT-5.6 Sol package boundary - 2026-07-12

| Scope / command | Exit | Result |
| --- | ---: | --- |
| Model catalog/Runtime/Worker/event/Gateway/provider policy suites | 0 | 111 passed; exact `ecorex-chat` -> `gpt-5.6-sol`, medium reasoning and 272000 `compact_threshold` survive every authority boundary |
| Managed model and production Gateway focused recheck | 0 | 40 passed; wrong mapping/policy is rejected and provider request shape is exact |
| Web model/bootstrap contracts | 0 | 43 passed plus generated-contract check |
| Package, Candidate and release-boundary audit | 0 | wheel top-level package is only `ecorex`; package cold import 7, dependency lock 4, Candidate pipeline 13, ReleaseBuilder 14 passed/1 skipped, gate integrity 8 and platform boundary 5 passed |

The existing provider logical-secret adapter remains unchanged. The cloud
Gateway reads `ECOREX_GATEWAY_PROVIDER_BEARER_TOKEN`; local Runtime packages do
not receive a provider API key. The pre-change cached wheel under
`tmp/v1-clean-wheel-output-20260710` is explicitly not Candidate evidence and
must not be signed or published.

## WebUI language boundary, task continuation and Settings flows - 2026-07-12

| Scope / command | Exit | Result |
| --- | ---: | --- |
| `npm run test:v1` | 0 | 161 passed; controlled server-error copy, product-language literals, Extension/Replay technical-detail folding, four-task projection fixture and all existing Web contracts passed |
| `npm run typecheck` | 0 | TypeScript and generated Runtime contract check passed |
| `npm run build` | 0 | 2,080 modules; 17 content-addressed assets; entry 51.23 KiB, initial JS 472.60 KiB / gzip 146.05 KiB, deferred 94.43 KiB / gzip 33.67 KiB, 16 chunks |
| `npm run test:e2e` | 0 | 30 Chromium scenarios passed, including task-ID success, deliberate 404 with original transcript preserved, delayed-old-response last-wins, Enter, mobile drawer, Output alias, memory reset/undo, full-access enable/revoke and task inspection/rerun with folded identifiers |
| `python scripts/check-v1-design-system.py` | 0 | five strict CSS files; all six prohibited design-debt counters remain zero |

This is local hashed-WebUI evidence against the deterministic same-origin GA
server. It does not claim physical assistive-technology certification,
packaged Known Folder permissions, a public provider, Candidate publication or
rollout activation.

## Artifact atomicity and system observability closure - 2026-07-12

| Scope / command | Exit | Result |
| --- | ---: | --- |
| Artifact API/outbox/feedback/Retouch/workspace and Runtime adjacency suite | 0 | 140 passed; feedback, external action, direct Retouch and workspace completion rollback/restart/idempotency paths covered |
| Critical-close system sample commit race | 0 | dirty sample commit rejected by Runtime permit guard; metric/state rows both remained zero |
| Focused `python -m ruff check ...` | 0 | Artifact, Retouch, outbox, observability, Runtime composition, legacy gate and tests clean |
| Python compile checks | 0 | all changed Artifact, integration, observability and Runtime modules compiled |
| `python scripts/check-v1-legacy-cutoff.py` | 0 | retired trees contain no executable/cached residue; `.pyc` regression test passed |

The publisher/restart tests use deterministic local sinks and fault injection.
They prove local SQLite atomicity and bounded recovery, not a deployed remote
collector or multi-hour production soak. No Candidate or rollout was changed.

## Runtime streaming checkpoint and Event notification closure - 2026-07-12

| Scope / command | Exit | Result |
| --- | ---: | --- |
| High-frequency and silent Agent stream tests | 0 | 128 mixed reasoning/text deltas completed and produced fewer than 32 heartbeat facts; latest terminal sequence, complete reasoning/text, completed Job, silent-provider renewal and contender exclusion passed |
| Event notification race/pressure suite | 0 | 5 passed; after-commit only, rollback silence, shared same-process hub, publish-before-wait, 24 waiters, thread isolation, cancellation, 16 SSE clients with no 300 ms repoll, page-to-wait injection and one-second-style fallback covered |
| Worker/EventStore/SSE/Kernel adjacency | 0 | 89 passed across Agent Worker, execution permits, supervisor, Event Store, Runtime hardening, jobs/interactions, kernel API and reasoning replay |
| Runtime lease/state/shutdown fencing | 0 | 43 passed on the complete rerun; a preceding cold child-process wall-clock check measured 3.687 s against its 3.5 s host bound, then passed in isolation and in this full rerun |
| Tool/HITL replay fencing | 0 | 18 passed; approval, uncertain execution, sandbox and interaction recovery semantics remain unchanged |
| Focused `python -m ruff check ...` | 0 | Worker, Event Store, Runtime API and focused tests clean |

The notification hub is an optimization, not a second event authority. Every
subscriber reads SQLite before waiting, and the one-second fallback covers
cross-process commits and lost local notification delivery. These tests do not
claim deployed multi-process backpressure or slow-network browser latency.

## Final local productization closure - 2026-07-12

| Scope / command | Exit | Result |
| --- | ---: | --- |
| Required Capability Pack and Candidate topology | 0 | Six required packs (`browser`, `channels`, `image`, `ocr`, `office`, `sandbox`) plus Core and Bootstrap produce eight receipts per target and 24 receipts across three targets; service-only bindings expose zero synthetic tools |
| Office/OCR pack truthfulness | 0 | `office.formats` performs DOCX/XLSX/PPTX/PDF create/read/validate and does not claim rendering; OCR smoke must recognize synthetic `ECOREX 2026` rather than merely import successfully |
| Update activation boundary | 0 | pointer-switch and interrupt coverage proves the old Runtime remains admission-drained after the durable activation boundary; pre-boundary timeout preserves the staged candidate at `awaiting_user` |
| Verified download CAS and administrator rollback | 0 | full Core, delta and pack reuse is signature/size/SHA-gated with digest single-flight, quarantine and bounded GC; rollback is audited, nonce-bound, single-use, older-known-good only and uses the normal confirm/drain/health chain |
| Supervisor monotonic deadline regression | 0 | early Windows timer wake no longer shortens restart backoff; the formerly failing boundary passed ten consecutive repetitions after the fix |
| Worker heartbeat clock/pressure correction | 0 | injected-clock, deadline, forced-boundary and slow-commit coverage passed; the complete Worker file passed 27 tests and repeated dense-stream rounds passed without weakening the 100–250 ms checkpoint contract |
| Connector admission/provider timeout race | 0 | deterministic pre-dispatch-delay regression, crash fencing and progressive/persistence timeout suites passed; combined Worker and Connector adjacency passed 119 tests with no late untracked provider dispatch |
| Final full Python run | 0 | 1,814 passed, 17 platform-condition skips, 0 failures, 0 errors; the two preceding full runs surfaced the timer issue and then the heartbeat/Connector issues recorded above, which were root-fixed before this final green run |
| `npm run test:v1` / typecheck / build | 0 | 161 Web tests passed; TypeScript clean; 2,080 modules produced 17 content-addressed assets and 16 chunks, with 472.60 KiB initial JavaScript / 146.05 KiB gzip |
| `npm run test:e2e` | 0 | 30 local Chromium Playwright scenarios passed across the supported responsive, accessibility, task-continuation, Settings and interaction fixtures |
| Final static gates | 0 | Runtime schema 20 fragments and server schema 8 authorities/3 roots passed; all six design-debt counters are zero; legacy cutoff, dependency locks, public download, reproducibility, supply-chain preflight and `git diff --check` passed |
| Source-tree Git admission | 1 | sole local gate blocker: 623 authoritative v1 files were inventoried, 3 tracked and 620 untracked; no implicit staging, commit, push, Candidate publication or rollout was performed |
| Independent authoritative-source content scan | 0 | all 623 files are regular UTF-8 LF text with final newlines; zero symlink, NUL/binary, CRLF, missing-final-LF or trailing-whitespace findings |

The verification boundary is deliberate. This workstation has no Go toolchain,
so Bootstrap Go tests are not claimed. Protected clean-runner execution,
KMS-backed signing, live release/mirror/CDN origins, real Feishu/Tencent tenants,
live model/image providers and production rollout health were not available and
are not represented as complete by this local ledger.

## Authorized Git admission pre-commit gate - 2026-07-12

| Scope / command | Exit | Result |
| --- | ---: | --- |
| `python scripts/check-v1-source-tree.py` | 0 | all 623 authoritative v1 source files are present in the local Git index and satisfy the regular UTF-8 LF source contract |
| Candidate supply-chain preflight | 0 | 443 production files scanned; runtime lock contains 21 packages and manifest SHA-256 is `2777443fb28ef39cc2a4fa7e4ba033899f3288624128709d032d7a42b0d2346d` |
| `python scripts/run-v1-lint.py --compile` | 0 | complete v1 Python lint and compile surface passed |
| Runtime/server schema authority | 0 | 20 Runtime fragments and 8 server authorities across 3 roots; zero violations |
| Design/dependency/legacy gates | 0 | all six design-debt counters remain zero; dependency locks and retired-chain cutoff passed |
| Staged path and whitespace audit | 0 | no `.candidate`, temporary, `desktop/dist` or `node_modules` path staged; `git diff --cached --check` passed after deterministic EOF cleanup |

This checkpoint records local Git admission only. It does not claim a commit,
push, published Candidate, protected-runner provenance or user acceptance yet.

## Local Candidate trust-role regression - 2026-07-12

| Scope / command | Exit | Result |
| --- | ---: | --- |
| First signed-Candidate attempt | 1 | failed closed before staging because the drill omitted the newly mandatory independent rollback verification role |
| Runtime config regression | 0 | release, rollback and session Ed25519 public keyrings are distinct; all private values remain process-only and covered by the persistence scan |
| Focused drill suite / Ruff / compile | 0 | 7 tests passed; changed build-chain files are lint- and compile-clean |

This correction does not yet claim the repeated end-to-end Windows drill; that
result is recorded only after the fresh committed-source run completes.

| Platform-stage timeout budget reproduction | 1 | the real local stager was still progressing at 1,804 seconds and failed closed at its former 30-minute nested deadline; no receipt or Candidate was accepted |
| Bounded timeout hierarchy correction | 0 | stager 45 minutes, repository wrapper 50 minutes and protected job 60 minutes preserve process-tree termination plus a ten-minute cleanup/receipt margin |
| Process boundary and Windows drill regression | 0 | 17 focused tests passed; Ruff and compile checks passed |

## Real-user CDP and provider admission audit - 2026-07-13

| Scope | Result | Evidence / boundary |
| --- | --- | --- |
| First-message model selection | PASS | The built React WebUI exposed `GPT-5.6 SOL · 中等推理` and the independent `Image 2` selector before the first message. The first Turn completed without residual thinking state. |
| Reasoning disclosure continuity | PASS | CDP observed `正在核对季度资料。` remain visible until the next atom arrived, then change directly to `资料已核对，正在整理结果。`; it did not flash away between atoms and archived only on the explicit terminal reasoning event. |
| Button, border and typography language | PASS | CDP computed style on an ordinary toolbar button was transparent at rest, 13/20 px, 10 px token radius and 32 px high, using the EcoreX system Chinese font stack. The control did not add a permanent card border. |
| Image intent and preview | PROTOCOL PASS | An Office-mode image request preferred Image 2 without removing Office mode; the result appeared inline and the preview opened fit-to-canvas with zoom retained. This same-origin GA provider is deterministic and does not certify a live provider. |
| Structured precise Retouch | PROTOCOL PASS | CDP created a normalized rectangle annotation, submitted a Retouch workspace, received a dedicated Retouch Turn, new revision, inline image, change summary, inspection regions and original/new/side-by-side comparison. The audit found and fixed missing `turn_id`/job linkage in the GA completion event before rerunning to green. Pixel accuracy remains provider-blocked. |
| Steer / queue / replace | PASS | All three composer dispositions were exercised through visible menus. Steer appended to the active Turn; queue created a queued Turn; replace superseded the old Turn and created a replacement. The GA server now retains bounded events, exposes event pages and confirms every operation by `client_message_id`. |
| Task-ID continuation | PASS | `thr_target_ga` restored the independent target projection, original user message and assistant response without contaminating the current task. |
| Output, memory and permissions | PASS | Default output changed from Documents to Downloads; learned memory reset from two to zero with a 24-hour undo affordance; Full Access became persistently visible and exposed one-click restore. |
| Extension and Connector management | PASS | The product UI exposed one governed catalog for Skill/MCP/tool/connector extensions with trust, health, enable/disable and rollback actions. Feishu and Tencent Docs appeared as stable connectors in the shared connection surface. No live tenant OAuth was claimed. |
| Public share conversation and image | PASS | The real Control Plane share renderer distinguished `你的指令` from `EcoreX`, attached the image to the assistant Turn and displayed it inline. The isolated media endpoint returned 200 `image/png`, 58,100 bytes, with ETag equal to the actual SHA-256 and no local path. |
| Capability planner, progressive disclosure, Worker and image orchestration | PASS | Focused backend rerun completed 176/176 tests across capability planning/discovery/invocation, Agent Worker, image concurrency stability, image orchestrator and managed-image integration. |
| Real `gpt-5.6-sol` inference | **BLOCKED** | The existing administrator policy credential reached the configured upstream and `/models` returned 200, but the catalog did not contain `gpt-5.6-sol`. One initial request plus three bounded retries returned 503. The old `gpt-5.5` control request also returned 503, proving an upstream inference outage in addition to the missing new mapping. |
| Real Image 2 generation / concurrency / pixel Retouch | **BLOCKED** | The real upstream catalog listed `gpt-image-2`, but a single low-quality generation was rejected with 503 `No available compatible accounts`. Concurrency was deliberately not amplified after failed single-flight admission. |

The provider failures are release blockers. No push, protected Candidate,
publication or rollout may be represented as complete until the upstream
catalog contains `gpt-5.6-sol`, chat inference succeeds with medium reasoning,
one real Image 2 request succeeds, bounded image concurrency completes without
duplicate billing/results, and the resulting image passes structured Retouch
inspection. No provider secret was copied into the Runtime, repository or this
ledger.

## Windows staging diagnostic closure - 2026-07-13

| Scope | Exit | Result |
| --- | ---: | --- |
| Fixed-commit Windows drill at `fc273766` | 1 | Source-pinned staging reached real Core native binaries, the isolated OCR dependency closure and browser Pack evidence before failing closed after about 28 minutes; no Candidate receipt was accepted. |
| Nested diagnostic propagation | 0 | The platform wrapper now retains only a bounded public adapter failure code in `stage-failure.json`; arbitrary detail and secret-like stderr cannot cross the receipt boundary. The drill reports that safe code instead of another generic rejection. |
| Host isolated-Python repair | 0 | The workstation had `pydantic` code 2.9.0 paired with metadata 2.12.5 and `pydantic_core` 2.41.5. Exact reinstall of the locked `pydantic==2.12.5` restored both ordinary and `python -I` imports without changing project dependencies. |
| Windows boundary and Pack rerun | 0 | 56 passed, 1 platform-condition skip across release process boundary, signed drill and platform Pack staging; Browser and managed Image Pack direct probes passed, Ruff/compile/diff were clean. |

The workstation repair is not a repository dependency change. Protected
runners must still provision the lock exactly and fail their isolated import
preflight before expensive native staging if their host interpreter is
inconsistent.

## Browser Pack Windows native cleanup closure - 2026-07-13

| Scope | Exit | Result |
| --- | ---: | --- |
| Third fixed-commit Windows drill | 1 | Safe diagnostic propagation identified the exact gate as `browser_pack_smoke_failed`; Core, native binaries, Browser archive and OCR closure had completed before the probe rejected the Candidate. |
| Direct vendored Pack traceback | 1 | Browser navigation itself completed, but child cleanup raised Windows `PermissionError` while unlinking the still-loaded `greenlet._greenlet.pyd`; stdio correctly reduced the private traceback to `pack_internal_failure`. |
| Parent-owned invocation TEMP/TMP | 0 | Core now creates one private temp domain per Pack call, injects it through the allowlisted child environment and removes it strictly after the child is reaped. The Browser child ignores only its Windows in-process DLL unlink error; no background cleaner or cross-call shared cache is used. |
| Real Browser-only vendored smoke | 0 | Staged Playwright 1.58.0/Chromium Pack returned `completed`, contained `ecorex-stage-ready`, preserved its four-distribution inventory and left zero new parent temp residues. |
| Pack/process/release boundary regression | 0 | 73 passed, 2 platform-condition skips; Ruff, Python compile and whitespace gates passed. A test observes equal private TEMP/TMP inside the child and proves the directory no longer exists after invocation returns. |

This fixes a real Windows lifecycle defect rather than weakening cleanup. The
parent can delete mapped native modules only after process exit, which is the
correct ownership boundary for all one-shot executable Capability Packs.

## Platform-stage lock convergence - 2026-07-13

| Scope | Exit | Result |
| --- | ---: | --- |
| Fourth fixed-commit Windows drill | 1 | The Browser smoke advanced successfully; signing then failed closed at `python_dependency_lock_mismatch`, proving the native cleanup fix and exposing the next independent supply-chain gate. |
| Drift diagnosis | 0 | `platform-stage.lock` pins Playwright 1.52.0 while the workstation had 1.58.0. Other Browser closure members matched. A runnable newer browser was not treated as signable evidence. |
| Staging toolchain convergence | 0 | Reinstalled exact Playwright 1.52.0 and its Chromium 1169 payload. Project dependencies and lock files were not changed. |
| Locked Browser-only smoke | 0 | Snapshot completed with `ecorex-stage-ready`; the four-package Browser inventory matched platform-stage lock manifest `2777443fb28ef39cc2a4fa7e4ba033899f3288624128709d032d7a42b0d2346d`, profile lock `3c7b26516bb4d18fc1a620e20ee92a922bf4858aee6e0ce0fa5fb89e491ddfd0`, and left no temp residue. |

Protected native runners must provision from the hashed lock before staging;
locally installed newer packages are intentionally rejected even when their
functional smoke succeeds.

## v0.3 workbench expansion checkpoint - 2026-07-13

| Scope | Exit | Result |
| --- | ---: | --- |
| Project Runtime tests | 0 | 2 passed; native project creation is canonicalized and thread metadata is backend-authoritative. |
| Web typecheck and generated contracts | 0 | TypeScript and Python-schema digest checks passed with `ProjectListResponse` included. |
| Web unit/design contracts | 0 | 161 passed; frameless controls, typography, accessibility, stream rendering and Runtime contracts remained locked. |
| Production Web build | 0 | 18 content-addressed assets; entry 44.41 KiB, initial JS 467.05 KiB and all bundle budgets passed. |
| Real browser project conversation | 0 | Selected `季度报告`, sent the first message, observed the Thread under the project group and verified user/Agent rows without avatars. |
| Real browser model surface | 0 | GPT-5.6 SOL medium reasoning is selectable before the first Turn; image mode exposes the independent `Image 2` selector. |

The first full 31-scenario E2E invocation exceeded the 180-second wrapper
timeout before producing a receipt, so it is not counted as a pass. It must be
rerun with the repository's bounded full-suite allowance after the remaining
Composer upload and usage-contract work lands.

## Composer attachment and usage closure - 2026-07-13

| Scope | Exit | Result |
| --- | ---: | --- |
| Input attachment service/API | 0 | 3 focused tests passed: opaque account-scoped upload, idempotency, internal Artifact visibility, cross-account denial and multipart Runtime route. |
| Usage projection | 0 | 2 focused tests passed: provider-reported daily/week aggregation, exact 272k model threshold, strict read-only API and missing-thread 404. |
| Runtime regression subset | 0 | 141 passed across attachment, usage, project, schema, capability planner/invocation and Agent Worker coverage. |
| Web unit/design contracts | 0 | 162 passed, including strict usage projection parsing and GA mock authority. |
| Typecheck / production build | 0 | TypeScript and contract digest passed; 18 content-addressed assets, 473.37 KiB initial JavaScript / 146.73 KiB gzip, all bundle gates passed. |
| Focused browser E2E | 0 | Playwright verified server-reported quota/usage/context rendering and non-vertical short user bubbles. |
| CDP manual browser pass | 0 | Opened a real built artifact conversation, verified `今日 5.2k` / `本周 22.6k` / `上下文 42.2k / 272k` / `额度 128次`, model selector and repaired user bubble layout. |

The prior full E2E wrapper timeout remains an uncredited receipt; only the
focused deterministic scenarios above are claimed. Live model/image provider
and release authority blockers remain unchanged.

## Composer placement and compact-navigation closure - 2026-07-13

| Scope | Exit | Result |
| --- | ---: | --- |
| Initial complete WebUI E2E rerun | 1 | 32/34 passed. Both 1024x768 themes failed closed: the compact project toggle had no discernible name after label hiding, and a plain project-session action yielded an indeterminate contrast check. |
| Composer structural regression | 0 | The normal-Thread Composer is within 2 CSS pixels of the Workspace bottom; the new-conversation Composer is rendered only inside the centered general/project chooser. |
| 320px touch queue regression | 0 | The queued disposition, stop and send controls remain reachable; the send button is inside the viewport and creates the queued message/Turn. |
| 1024x768 light and dark GA rerun | 0 | Both exact-frame reports passed after semantic compact-sidebar controls replaced the compressed plain-text action. |
| Full WebUI E2E matrix | 0 | 34/34 passed across 1440, 1024, 768, 390 and 320 CSS-pixel viewports; both themes, axe, forced colors, reduced motion, artifact touch actions, reasoning/HITL and Composer behavior passed. |
| Web unit/design contracts | 0 | 162/162 passed, including generated Runtime contracts, design density, GA harness, artifact preview, reducer and durable outbox coverage. |
| Typecheck / production build | 0 | TypeScript plus generated-contract check passed; 18 content-addressed production assets and all bundle gates passed. |

This closes the local WebUI placement and responsive-accessibility receipt. It
does not supersede the separate live managed-model/Image 2 provider admission
or protected release-authority blockers.

## Attachment-runtime availability and current-source candidate gate - 2026-07-13

| Scope | Exit | Result |
| --- | ---: | --- |
| Initial full Python candidate run | 1 | The interactive command transport terminated the process at its 60-second ceiling; this is explicitly uncredited and was not an assertion failure. |
| Attachment availability root-cause regression | 0 | 35 passed, 1 platform-conditioned skip. A bound `input_attachment_read` Core handler clears only the stale `verified_handler_not_installed` fact; an unbound reader reports `input_attachment_runtime_not_bound`, and policy denials remain authoritative. |
| Full Python v1 gate | 0 | 1,836 passed, 17 platform-conditioned skips, 5 third-party deprecation warnings, 745.98 seconds; JUnit receipt: `.candidate/quality/full-pytest-current.xml`. |
| Current-source tree gate | 0 | 631 authoritative source files; no legacy or source-tree policy violation. |
| Lint / Python compilation / whitespace | 0 | Repository lint and compile gate passed; `git diff --check` was clean. |
| Local supply-chain preflight | 0 | Dependency-lock, license and secret-scan gates passed; report: `.candidate/quality/supply-chain-local-post-attachment-runtime.json`. |

The gate proves the current local source is internally consistent. It does not
claim signed platform Candidate creation, live managed model/Image 2 success,
real connector authorization, external release publication or user activation.

## Windows signed-candidate provisional-startup diagnostic - 2026-07-13

| Scope | Exit | Result |
| --- | ---: | --- |
| Current-commit Windows x64 stage | 0 | Eight local receipts were generated for Core, Bootstrap, Browser, Channels, Image, OCR, Office and Sandbox on commit `75ac7b49`; each source-pinned stage reached content verification. |
| First-install provisional health | 1 | Correctly failed closed: the signed Bootstrap stopped the candidate before health readiness. Result had no child exit code or redacted startup-stage error, which is consistent with a still-running cold Pack verification path rather than an accepted unhealthy Runtime. |
| Second source-pinned first-install health | 1 | Commit `da840b11` again generated all eight local Windows receipts, then reached the 90-second provisional health limit without a child exit. This disproved a timeout-only fix and isolated duplicate Pack verification as the root cause. |
| Probe/full-runtime separation regression | 0 | 89 passed, 3 platform-conditioned skips across Bootstrap, activation, product-Runtime and storage-migration coverage. The provisional process does not open a credential vault; Bootstrap verifies Pack content before launch, and the full Runtime retains Pack binding before the data barrier. |

Neither failed attempt is a Candidate receipt. Their staging evidence is local
and disposable, and the next ceremony must run from the post-separation commit
before any candidate can be considered for protected-stage admission.

## Full-Runtime startup-stage diagnostic follow-up - 2026-07-13

| Scope | Exit | Result |
| --- | ---: | --- |
| Third local Windows ceremony from `37a7b5bc` | 1 | All eight platform-stage receipts and first-install activation passed. The probe confirmed the slot within the normal bounded window; the subsequent full Runtime exited with code `64` before HTTP readiness. The temporary install root and local keys were removed. No Candidate receipt or publication was created. |
| Safe diagnostic contract | 0 | Bootstrap now passes a fresh opaque launch token only through child environment, Runtime emits at most a schema/version/token/fixed-stage JSON record, and Bootstrap consumes then deletes it. No raw stderr, provider error, local path, key or token value enters the result. |
| Focused Runtime/Bootstrap/CLI/drill regression | 0 | 4 passed: nonce-bound stage propagation, safe deletion, CLI stage-only output, probe/full separation and the drill's bounded unavailable fallback. |

The next source-pinned ceremony must report the fixed stage from the real
full-Runtime failure. This observability addition is advisory only and cannot
alter activation, rollback, trust or release decisions.

## Browser Pack descriptor root-cause closure - 2026-07-13

| Scope | Exit | Result |
| --- | ---: | --- |
| Fourth local ceremony from `84207f69` | 1 | All eight Windows stage receipts and the nonce-bound first-install probe passed; full Runtime failed with exit `64` and the first safe stage `capability_pack_binding`. No Candidate receipt or publication was created. |
| Fifth local ceremony from `326526fb` | 1 | All eight Windows stage receipts, copy-on-write v0.3 migration and first-install provisional health passed. Full Runtime failed closed with exact stage `capability_pack_browser`; the temporary install root was removed and the worktree remained clean. |
| Real Browser-only reproduction | 1 | The locked Playwright 1.52.0/Chromium Pack was 190,153,573 bytes. Runtime inspection returned `pack_descriptor_invalid` in 161.312 seconds: the 129-byte descriptor had one trailing LF while the canonical contract was 128 bytes. |
| Post-fix real Browser Pack | 0 | A fresh locked Playwright 1.52.0/Chromium Pack was 190,153,571 bytes. Its generated descriptor was exactly 128 bytes with no LF; the production ZipApp inspector passed and Runtime bound exactly `cdp` and `fetch` in 116.969 seconds. |
| Independent Pack-Python check | 0 | A retained real Core matched 1,733 closure files, 61,188,898 bytes and manifest digest `c9aff462...0819fe`; this supporting diagnostic ruled out the interpreter closure algorithm for this failure. |
| Canonical descriptor regression | 0 | 4 passed. Browser and Sandbox source-style descriptors are normalized into exact Runtime bytes, accepted by the production ZipApp inspector, and semantic drift remains rejected. |
| Pack/Runtime/Bootstrap regression | 0 | 123 passed, 3 platform-conditioned skips across process Packs, platform staging, Pack Runtime, Product Runtime entrypoint, local Bootstrap install and activation health. |
| Current-source quality and supply chain | 0 | Source-tree policy passed with 632 authoritative files; dependency-lock, Ruff/compile, whitespace, progress-JSON, license and secret-scan preflight passed. Report: `.candidate/quality/supply-chain-local-browser-descriptor-fix.json`. |

The two failed ceremonies remain uncredited. The fix preserves Runtime's exact
descriptor check and changes the producer to emit its contract. A new fixed-
commit signed ceremony is required to prove first install, migration restart,
healthy update, bad-digest rejection and rollback end to end.

## Full Runtime application-composition diagnostic follow-up - 2026-07-13

| Scope | Exit | Result |
| --- | ---: | --- |
| Sixth local ceremony from `ada2c1f5` | 1 | All eight Windows receipts, released-v0.3 copy-on-write migration and nonce-bound first-install health passed. The full Runtime advanced beyond Browser Pack binding and failed closed at aggregate stage `server_configuration`; the disposable root was removed, the worktree remained clean and no report/publication was created. |
| Startup-layer diagnostic contract | 0 | Runtime load, ASGI application composition and Uvicorn configuration now emit only `runtime_composition`, `application_composition` or `http_server_configuration` when their bounded configuration/value boundary fails. Existing precise loader and trust stages are preserved. |
| Resource ownership regression | 0 | Managed transports transfer to the App only after HTTP configuration succeeds; application/HTTP failure closes the unstarted composition once and cannot orphan it. |
| Product Runtime entrypoint regression | 0 | Focused entrypoint: 32 passed, 1 platform-conditioned skip. Broader entrypoint, Bootstrap supervisor, activation-health and signed-drill set: 87 passed, 2 skips. Stage redaction, exact cleanup, signed-slot App construction and existing CLI contracts passed. |

The failed ceremony is evidence that the canonical Browser fix works in the
integrated package, not a Candidate receipt. The next fixed-commit run must
name the narrower startup layer before any implementation correction is
accepted.

## Packaged IANA timezone root-cause closure - 2026-07-13

| Scope | Exit | Result |
| --- | ---: | --- |
| Seventh local ceremony from `f6ca3ff1` | 1 | Eight Windows stage receipts, released-v0.3 copy-on-write migration and nonce-bound provisional health passed. Full Runtime exited `64` at exact safe stage `application_composition`; the disposable candidate was retained only for diagnosis and no report/publication was created. |
| Exact packaged-interpreter reproduction | 1 | The signed slot completed Runtime/Pack composition, then FastAPI application construction raised `ValueError: usage timezone is invalid`; the nested cause was `ZoneInfoNotFoundError: Asia/Shanghai` and `ModuleNotFoundError: tzdata`. Relevant packaged EcoreX modules matched source byte-for-byte. |
| Dependency and environment correction | 0 | Product and Runtime locks pin `tzdata==2026.2`; the Core closure includes it. The `-I` Core probe and product server explicitly reset `zoneinfo` to an empty search path and clear its cache; Bootstrap also sets `PYTHONTZPATH=""` for non-isolated paths. Lock manifest SHA-256 is `f05ecab2bac52bbbe61b9728ffb0ecc0166aeff8a3bb0e9016a66e8979592097`; Runtime profile contains 22 packages. |
| Source/runtime regression | 0 | `Asia/Shanghai` resolved with bundled `tzdata 2026.2`; dependency-lock, product entrypoint, Bootstrap environment, usage projection and platform-staging set passed 111 tests with 3 platform-conditioned skips. Ruff, Python compilation and `git diff --check` passed. |
| Supply-chain completeness correction | 0 | Candidate pipeline regression passed 15 tests. Preflight now derives license inventory from all Runtime lock entries and requires exact name/version coverage: 22 locked = 22 licensed, including `tzdata 2026.2 / Apache-2.0`; 449 production files passed secret scan with inventory digest `25a9ccb296bad4f8973985ea3eac07f523c85e2685fc610f2c452fc9aa5b5cc3`. Report: `.candidate/quality/supply-chain-local-tzdata-fix-v3.json`. |
| Final related source gate | 0 | Candidate/release integrity, dependency lock, Product Runtime entrypoint, Bootstrap, usage and platform-staging suites passed 134 tests with 3 platform-conditioned skips. Source-tree policy passed with 632 files; Ruff, compilation, progress JSON and whitespace gates passed. |
| Disposable full-closure side proof | 124 | Uncredited: a second real `_build_python_closure` did not emit a result before the 904-second command limit. Its partial 2,375-file, 61,841,500-byte temp tree was removed after exact-prefix validation and handle release. This does not replace or weaken the next full ceremony. |

The prior signed slot lacks the new dependency and cannot prove the fix by
mutation without invalidating its signature. A new fixed-commit ceremony must
build Core again, pass its new timezone probe and complete the entire signed
install/update/rollback drill. Live managed-model/Image 2 and protected macOS
admission remain separate release blockers.

## Packaged multipart route-dependency closure - 2026-07-13

| Scope | Exit | Result |
| --- | ---: | --- |
| Eighth local ceremony from `3c78d1d0` | 1 | The rebuilt Core passed the signed `Asia/Shanghai` probe; all eight Windows receipts, released-v0.3 copy-on-write migration and provisional health passed. Full Runtime then exited `70` at safe aggregate stage `software`; Bootstrap revoked the sandbox authorization and restored first-install pointers. No Candidate report or publication was created. |
| Exact packaged-interpreter diagnosis | 1 | In-process diagnostic temporarily bypassed only the rollback-revoked sandbox validation, restored the signed pointer for read-only composition and restored it on exit. FastAPI route registration failed with nested `ModuleNotFoundError` for `python-multipart`; signed payload bytes were not changed and this diagnostic is not Candidate evidence. |
| Runtime dependency correction | 0 | Existing reviewed `python-multipart==0.0.26` moved from dev-only to direct Runtime dependency. Runtime, Cloud and platform-stage locks gained only that exact hashed distribution; Core closure/probe imports `multipart.multipart.parse_options_header`. Application-construction `RuntimeError` maps to the redacted `application_composition` stage. Manifest SHA-256 is `5e59ad4e74a4e870f9d1c734a17ca04f28e1cd2e184c75eb1377083ebd47103c`; Runtime profile contains 23 packages. |
| Focused product regression | 0 | Entry point/platform staging/reproducibility: 89 passed, 2 platform skips. Product ASGI/upload/attachment/Artifact/dependency/Bootstrap: 55 passed, 2 platform skips. Exact import returned `0.0.26`; Ruff passed. |
| Source and contract gates | 0 | Source-tree policy reports 632 files; Runtime schema authority has 20 fragments and zero violations; server authority has 8 authorities/3 roots and zero violations; design-system strict set has zero hardcoded radii, shadows, colors, numeric z-index, layout transitions or `transition: all`. |
| Supply-chain preflight | 0 | 23 locked Runtime packages = 23 licensed packages, including `python-multipart 0.0.26 / Apache-2.0`; 449 production files passed the secret scan with inventory digest `546cf106c484ab3cf99000bce0757f21fc9e805d1d580f1e5560a9749d336653`. Report: `.candidate/quality/supply-chain-local-multipart-fix.json`. |

The retained eighth slot is root-cause evidence only. A ninth source-pinned
zero-publication ceremony must build the corrected closure and pass its full
install/update/rollback sequence. Live managed-model/Image 2 and protected
macOS evidence remain independent release blockers.

## Multi-activation Candidate deadline closure - 2026-07-13

| Scope | Exit | Result |
| --- | ---: | --- |
| Ninth local ceremony from `80a4d6c8` | 1 | All eight Windows receipts were emitted. Core dependency closure passed with 23 distributions including `python-multipart 0.0.26`; isolated Core identity was 2,388 files / 61,937,031 bytes / digest `68742f27189c69c16f68de846f20074f9093d83cefc6aba03bf16c423fce1822`. |
| Signed product Runtime proof | 0 | First full Runtime completed Pack binding, FastAPI multipart route construction and loopback readiness. v0.3 copy-on-write migration completed, activation receipt was `confirmed`, and two storage-migration receipts existed. This proves the multipart fix in the rebuilt signed Core. |
| Former total deadline | 1 | Explicit 3,600-second total expired during `post-migration source-removal Runtime restart` while the second Runtime was still progressing. At timeout the expected slot was current/known-good; failure cleanup revoked temporary security and restored final pointers to `current=null`, `previous=null`, `known_good=[]`. No report or publication was created. |
| Deadline hierarchy correction | 0 | Total default/max is truthfully 5,400 seconds; platform wrapper is independently capped at 3,000 seconds; each of four Runtime readiness windows is independently capped at 900 seconds and cannot exceed the total deadline. Successful evidence reports carry all three limits. |
| Regression | 0 | Candidate drill, release pipeline, storage migrations, process boundary and Bootstrap supervisor: 82 passed, 2 platform skips. Ruff, CLI help, source-tree and whitespace checks passed. Current-source supply chain remains 23 locked = 23 licensed Runtime packages and 449 scanned files, inventory digest `d0f6e32a8c877cc8666491b5c700e64dcca8d6b756139f3f23df80664e18e96c`; report `.candidate/quality/supply-chain-local-deadline-policy-v2.json`. |

This failed run is strong signed Runtime and migration evidence but is not a
Candidate receipt. A tenth committed-source ceremony must complete healthy
update, bad-digest rejection and rollback under the corrected bounded policy.

## Tenth ceremony and trust-scan performance closure - 2026-07-13

| Scope | Exit | Result |
| --- | ---: | --- |
| Tenth local ceremony from `078d0e81` | 1 | All eight Windows receipts emitted. Domestic-mirror-first fallback, background `awaiting_user`, explicit first activation, full Runtime readiness, released-v0.3 copy-on-write migration, source removal and source-removed Runtime restart passed. |
| Healthy update | 0 | A distinct same-version build staged in the background, waited for confirmation, activated, reached full Runtime health and completed its Bootstrap journal. |
| Bad digest and rollback | 0 | The bad digest was rejected before pointer mutation. A valid signed fault slot failed before the data barrier and emitted `bootstrap_health_failed_rolled_back`; the recovered healthy slot was `current` and `known_good`. |
| Aggregate deadline | 1 | The 5,400-second total expired during the final recovered-Runtime health wait after rollback. The child was still progressing and no error was emitted. Failed-ceremony cleanup restored `current=null`, `previous=null`, `known_good=[]`; no report or publication was created. |
| Serial root-cause lower bound | 124 | Two read-only serial scans of the retained 2,388-file / 61,937,031-byte Pack-Python closure did not finish within 904 seconds. This is diagnostic evidence only. |
| Representative import compaction | 0 | A temporary copy placed 1,952 zip-safe members / 21,609,256 uncompressed bytes into a deterministic 5,502,287-byte archive and reduced the physical closure to 437 files / 45,830,062 bytes. Critical isolated imports passed in 3.095 seconds. |
| Exact digest performance comparison | 0 | The compact closure first reproduced digest `212b286c...0efa6` serially in 181.044 seconds, then reproduced the same digest with bounded streaming workers in 0.274 seconds; its payload tree took 0.384 seconds. The original 2,388-file closure reproduced digest `68742f27...1822` in 1.470 seconds. Timings are same-host focused diagnostics, not a cold Candidate claim. |
| Trust and supply-chain regression | 0 | Canonical/case-fold-unique archive paths, encryption, links, member and expanded-size bounds are checked; bounded members remain secret-scanned; every file retains content, size, identity, mtime/ctime, path-mode and reparse-attribute TOCTOU checks; the isolated Core probe opens zipped administrator/CA resources; no process-persistent trust cache exists. The final versions of these checks are included in the complete affected regression below. |
| Complete affected regression | 0 | Platform staging, process Packs, atomic install, update durability/coordinator, Candidate pipeline, Runtime entrypoint, administrator Web and signed-drill set: 182 passed / 5 platform skips. Ruff, compilation and zipimport asset tests passed. |
| Current-source gates | 0 | 23 locked = 23 licensed Runtime packages; 449 files passed secret scan with inventory digest `7a1c2a1c...270e8` in `.candidate/quality/supply-chain-local-runtime-trust-scan-v2.json`. Source-tree count is 632; Runtime schema, Server authority and design-system gates report zero violations. |

The tenth run is not a Candidate receipt because its final health phase did not
finish before the aggregate deadline. The next authoritative action is an
eleventh ceremony from the committed performance correction. Production
publication remains forbidden while live provider and protected macOS gates
are unresolved.

## Eleventh ceremony and fault-fixture correction - 2026-07-14

| Scope | Exit | Result |
| --- | ---: | --- |
| Eleventh local ceremony from `6cd7ccd8` | 1 | Cold input compacted from 2,389 to 437 physical files; `python311.zip` is 5,503,338 bytes. Core, Bootstrap and six Pack staging completed. First install/fallback/confirmation/full health, released-v0.3 migration, source-removed restart and distinct healthy update health passed. |
| Failure boundary | 1 | At 1,450.3 seconds the rollback fixture assumed an unpacked `ecorex/server/__main__.py`; production correctly placed it in the import archive. Failure occurred before fault-release construction. No Candidate report/publication was created; disposable state and child processes were removed. |
| Root correction | 0 | Fault injection resolves exactly one directory or zipimport member, validates canonical/case-fold-unique paths, encryption, links/special files and size bounds, atomically rewrites the archive, then rebuilds and independently resolves `pack-python.json`. |
| Focused regression | 0 | Windows signed-drill suite: 15 passed. Ruff, compilation and whitespace checks pass. The test proves the archive closure digest changes and the rebound manifest resolves. |
| Complete affected regression | 0 | Staging, process Packs, atomic install, update durability/coordinator, Candidate pipeline, Runtime entrypoint, administrator Web and Windows drill: 184 passed / 5 platform skips. |
| Current-source supply chain | 0 | 23 locked = 23 licensed Runtime packages; 449 production files passed secret scan with inventory digest `29eab98f...90ff3` in `.candidate/quality/supply-chain-local-runtime-trust-scan-v3.json`. |

This run is not a Candidate receipt. The next authoritative action is a
twelfth ceremony from the committed archive-aware fixture; no release or
rollout is authorized by these local observations.

## Twelfth zero-publication Windows ceremony - 2026-07-14

| Scope | Exit | Result |
| --- | ---: | --- |
| Source-pinned local ceremony | 0 | Commit `7bf9d89b60ea2c8a8881a22bf8d855cc8bf46876`; schema-3 `local-windows-drill`; 1,308.297 seconds under 5,400-second total and four independent 900-second Runtime windows. |
| Cold Runtime and platform stages | 0 | Pack-Python compacted from 2,389 to 437 physical files; import archive 5,503,338 bytes. Core, Bootstrap, browser, channels, image, OCR, office and sandbox emitted eight signed Windows x64 receipts. |
| First install and migration | 0 | Domestic-mirror failure → GitHub fallback, `awaiting_user`, explicit activation, full Runtime HTTP 200 and registration passed. Released-v0.3 copy-on-write migration committed; source deletion and source-removed restart HTTP 200 passed. |
| Update and refresh | 0 | Distinct same-version update remained inactive before confirmation, then reached completed/current/known-good and full Runtime HTTP 200; hashed assets remained immutable and HTML `no-store`. |
| Digest rejection and rollback | 0 | Bad digest rejected with active slot unchanged. Rebound exit-70 fault Core activated provisionally, reached rollback terminal state, was discarded, restored the healthy slot and recovered full Runtime HTTP 200. |
| Cleanup and report | 0 | Disposable root removed; no child Runtime remained. Report SHA-256 `a6d823cc...cdc0b` at `.candidate/quality/windows-signed-candidate-local-twelfth.json`. |
| Production promotion gate | blocked | `fixed_gate_relaxed=false`, `promotion_claimed=false`; only 8/24 local stage receipts exist and 16 protected macOS receipts remain missing. No external mirror, GitHub Release, CDN, Control Plane, Model/Image Gateway, connector or OTLP endpoint was contacted. |
| Post-report source gate | 0 | Provenance wording and explicit exit-70/manifest-rebound fields pass 15 drill tests and Ruff. Supply chain remains 23 locked = 23 licensed / 449 files, inventory `df8027e8...374e4a`, report `.candidate/quality/supply-chain-local-runtime-trust-scan-v4.json`. |

This is the authoritative passing local Windows ceremony for commit `7bf9d89b`.
It does not supersede protected-runner, live-provider, CDP acceptance or real
installed-user migration gates and does not authorize deployment.

## Local WebUI/CDP acceptance and media-intent regression - 2026-07-14

| Scope | Exit | Result |
| --- | ---: | --- |
| In-app browser acceptance | 0 | New-task chooser Composer centered; normal/restored/project-after-first-message Composer bottom-anchored with `0.000030517578125px` measured delta. Model selector works before the first message; GPT-5.6 SOL medium reasoning and 272k context are projected. |
| Image routing | 0 | Intent policy `1.5.0` recognizes `主视觉` / `key visual`, ranks imagegen first and keeps read/fetch/vision/CDP/shell discoverable. Negative `主视觉设计方案` remains an office-planning request. Live provider success is not claimed. |
| Image preview and retouch | 0 | Preview opens complete in fit mode and retains bounded zoom. Structured retouch persisted normalized geometry, exact instruction, reference and global constraints; a new revision, preview and inspection region appeared. Post-fix global-only run rendered the exact submitted instruction in chat and canvas. |
| Share and conversation flow | 0 | Two share snapshot IDs were distinct; shared chat separates user instruction and EcoreX response, renders inline media and opens the full 1800x1100 image. Reasoning remains visible until replacement/terminal; steer, queue, replace and task-ID continuation passed. |
| Settings and extension product surface | 0 | Full-access confirmation/revoke, memory reset/undo, output location, update check, unified Skill/MCP/tool extension catalog and formal Feishu/Tencent Docs connector entries passed locally. No external credential was used. |
| Browser diagnostics | 0 | Main, share and raw-image tabs: zero warnings/errors. Evidence report `.candidate/quality/cdp/webui-local-acceptance-20260714.json`; seven screenshots have SHA-256 and byte length recorded there. |
| Focused image concurrency | 0 | 33 passed: deadlines, durable rate fences, half-open single probe, bounded retry, slow-CAS lease renewal, staged-result recovery and lost-commit reconciliation. |
| Focused structured retouch | 0 | 27 passed: workspace/version fencing, geometry, atomic submit/result, permission races, stable external identity, crash recovery and late-result rejection. |
| Capability and Runtime discovery | 0 | 265 passed / 1 environment skip: capability snapshots, search/describe grants, projection budgets, Agent Worker, Runtime composition, Skill/MCP/connector discovery and invocation. |
| Web contracts | 0 | 162 passed; TypeScript and generated Runtime contract checks pass. |
| Production Web build | 0 | 2,080 modules; 18 content-addressed assets; 17 chunks; entry gzip 14.40 KiB, initial JS gzip 146.78 KiB, deferred features gzip 33.67 KiB. |
| Playwright E2E | 0 | 34 passed in 68 seconds: five viewport classes in both themes, zero axe violations, frameless-until-interaction controls, Composer state placement, reasoning, share, full-image preview, forced colors, reduced motion, keyboard and touch parity. |
| Complete Python v1 regression | 0 | 1,860 passed / 17 explicit skips / 0 failures in 786.84 seconds; five third-party deprecation warnings. |
| Static gate | 0 | `python scripts/run-v1-lint.py` passed. |
| Publication gate | blocked | No publication attempted. Live `gpt-5.6-sol` and `gpt-image-2`, protected 24-receipt Candidate, real connectors/OTLP and external download-source evidence remain unresolved. |

This local acceptance closes the deterministic WebUI/CDP work item but is not
live managed-model or protected-platform evidence. It cannot authorize a push.

## Thirteenth zero-publication Windows ceremony - 2026-07-14

| Scope | Exit | Result |
| --- | ---: | --- |
| Source-pinned platform build | 0 | Clean product commit `89fab32ae0884e9df5549f2b92e3a76d63fe6de1`; source-pinned production Stager emitted Core, Bootstrap, browser, channels, image, OCR, office and sandbox Windows x64 receipts. |
| Signed release identity | 0 | 15 artifacts; build digest `67c9133d...bf4d`; Web bundle `a19c9094...3d99`; 19 Web files / 18 immutable assets; Ed25519 manifest and every artifact digest verified. |
| First install | 0 | Injected domestic-mirror failure → local GitHub fallback; background preparation stopped at `awaiting_user`; explicit activation reached `completed`, Runtime HTTP 200 and registration pin release. |
| Migration and source removal | 0 | Released-v0.3 schema imported copy-on-write to a committed receipt; source was deleted; packaged source-removed restart returned HTTP 200 and remained idempotent. |
| Update and refresh | 0 | Distinct same-version replacement stayed inactive at `awaiting_user`, activated only after confirmation, reached `completed` and Runtime HTTP 200; HTML remained `no-store` and assets immutable. |
| Bad digest | 0 | All three local sources rejected the corrupt artifact; active slot unchanged and corrupt slot never activated. |
| Pre-data rollback | 0 | Rebound fault Core passed exit-70 preflight, activated provisionally, reached terminal `rollback`, discarded the fault slot, restored the healthy slot and recovered Runtime HTTP 200. |
| Long-job drain | 0 | Three activations stopped admission and persisted three distinct durable checkpoint receipts before pointer mutation; no real external long-job claim is made. |
| Cleanup and report | 0 | Completed in 1,439.328 seconds; disposable directory removed and no drill Runtime remained. Report `.candidate/quality/windows-signed-candidate-local-thirteenth.json`, SHA-256 `7144c39a...eccc4`, 43,536 bytes. |
| Post-report gate | 0 | `test_windows_signed_candidate_drill.py`: 15 passed; v1 lint and report JSON validation passed. |
| Current-source supply chain | 0 | 23 locked Runtime packages have complete license inventory; 449 files passed secret scan, inventory `ce27b97b...0c0fa`; report `.candidate/quality/supply-chain-local-current-89fab32a.json`, SHA-256 `2df5d828...0a2db`. |
| Production promotion gate | blocked | `promotion_claimed=false`, `fixed_gate_relaxed=false`; 8 local Windows receipts do not replace the 16 missing protected macOS arm64/x64 receipts. No external publication or live service was contacted. |

This report supersedes the twelfth ceremony only as current local Windows
evidence. It still cannot satisfy protected-runner or live-provider gates.

## Protected live-acceptance publication contract - 2026-07-14

| Scope | Exit | Result |
| --- | ---: | --- |
| Root-cause audit | 0 | Confirmed that the former 21-gate Control Plane contract omitted real Model Gateway, real Image Gateway and post-build Chrome CDP acceptance. The old `publish` job depended directly on `build-and-sign`. |
| Fixed gate set | 0 | Added `live-model`, `live-image` and `cdp-acceptance` to the central required set for canary and stable. Promotion journals, missing-gate projections and evidence assembly inherit the same authority. |
| Post-signing workflow fence | 0 | Protected Windows x64 `live-acceptance` now needs `build-and-sign`; it downloads the exact Candidate, binds all three executions and emits `ecorex-v1-accepted-*`. `publish` needs this job and cannot download the earlier Candidate artifact. |
| Process/secret boundary | 0 | The signed manifest, signed Candidate receipt and exact protected staging provenance are authenticated before the driver can run. The executable is digest-pinned and rechecked before/after execution; stdin-only request, bounded stdout/stderr, process-tree kill, exact environment allowlist, redacted failure codes and exact JSON shape prevent argv/env/path evidence leakage or detached children. |
| Model/Image/CDP evidence | 0 | Validator requires GPT-5.6 SOL medium/272,000, four unique concurrent Image 2 completions, non-exclusive discovery of read/fetch/vision/CDP/shell/imagegen, structured precise retouch, 18 fixed CDP scenarios, four viewports and zero browser diagnostics. |
| New regression | 0 | `test_live_acceptance_release_gate.py`: 13 passed; malformed model, typed tool discovery/counts/digests, retouch, CDP and Candidate identity evidence all fail closed; pre-driver authentication and failure redaction are fixed contracts. |
| Affected release regression | 0 | Gate integrity, exact-byte promotion, Candidate pipeline, Control Plane release/admin CLI, real Web pipeline and new live gate: 52 passed / 0 failed. Python compilation, Ruff and YAML parse passed. |
| Broad release regression | 0 | Full v1 collection filtered to release/Candidate/Control Plane/live acceptance: 335 passed / 4 explicit platform skips / 1,551 deselected / 0 failed in 210.51 seconds. Source-tree gate: 635 files. |
| Current-source supply chain | 0 | 23 locked Runtime packages have complete license inventory; 451 production files passed secret scan, inventory `62582b16...80a3`. Report `.candidate/quality/supply-chain-local-live-acceptance-gates-final-v3.json`, SHA-256 `f9dced93...32b3`, 21,857 bytes. |
| Live execution | blocked | No protected driver configuration or live managed provider session exists in the current environment. No evidence was fabricated, no publication job was dispatched and no user rollout was activated. |

## Signed release-gate authority and administrator projection - 2026-07-14

| Scope | Exit | Result |
| --- | ---: | --- |
| Gate-bundle cryptography | 0 | Exact Candidate identity, phase-specific gate set, evidence token grammar, manifest-key convergence, canonical signature and tamper rejection are covered. The real signing-script integration uses a child signer adapter and verifies the resulting bundle locally. |
| Control Plane authority | 0 | Signed bundle import is atomic and immutable; manual pass, duplicate attestation, prepare-only stable publish, database drift and stored-bundle tamper all fail closed. Publication re-verifies the final bundle. |
| Administrator Web | 0 | Real content-addressed FastAPI assets render 24 read-only machine gates with no select/input/pass control or upload path. Desktop and 390 px browser cases preserve server-authoritative publish confirmation and have no horizontal page overflow. |
| Release workflow | 0 | Prepare/final unsigned bundles are assembled from immutable receipts, signed only after their respective evidence boundary, and every promotion command supplies a trusted key. Stable final evidence includes publication and Bootstrap readback. |
| Quality receipt | 0 | The parser requires exactly 36 passed Playwright tests, zero failed/skipped/flaky tests and eight named sentinel scenarios. The current 499,454-byte report passed with SHA-256 `f16ed3ca...a1f2`, including Composer placement, persistent reasoning, fit-first preview and both administrator cases. |
| Exact manifest-byte authority | 0 | Candidate registration persists the uploaded manifest-file SHA-256 separately from canonical JSON. CLI uses authenticated file bytes; administrator Web uses Web Crypto over the selected `ArrayBuffer`; mismatched signed bundle digest is rejected before any gate row is written and checked again at publication. |
| Broad affected regression | 0 | Release/Candidate/Control Plane/update selection: 342 passed / 4 explicit platform skips / 1,552 deselected / 0 failed in 259.48 seconds. Web contract: 162 passed. Full Playwright: 36 passed. TypeScript and production build passed; 18 content-addressed assets and 17 chunks were emitted. |
| Publication | blocked | The protected Windows/macOS Candidate, protected live provider/CDP receipts and external origin readback remain unavailable. No push, deployment, publication or user update was attempted. |

## Immutable Candidate handoff and publication recovery - 2026-07-14

| Scope | Exit | Result |
| --- | ---: | --- |
| Workflow authority split | 0 | Candidate has no `publish-assets`, origin write permission or Control Plane token. A separate `verify-only`-default workflow is the sole protected publication path. |
| Exact run selection | 0 | Handoff binds protected Candidate workflow path, successful run/attempt, current protected commit, repository/head repository IDs, empty PR association, timestamps, channel, exact accepted Artifact ID and non-expiration. Foreign, failed, stale, mixed and duplicate inputs fail closed. |
| Exact archive bytes | 0 | Both cross-workflow archives are fetched by Artifact ID and must match their SHA-256. Safe extraction rejects traversal, drive/backslash aliases, links/special files, duplicate/case-colliding members, unexpected roots, member/size limits and insufficient disk. |
| Candidate re-authentication | 0 | Mutation boundary re-verifies handoff, signed Candidate receipt, exact manifest bytes/signature, staging provenance and every required pre-publication gate against the original Candidate workflow run ID. |
| Lost-runner recovery | 0 | Promotion request IDs deterministically bind release, manifest, publication receipt, rollout target, preparation evidence and operation. Two empty journals reproduce the same IDs; different operation/target does not. |
| Focused regression | 0 | Handoff/workflow/live-gate/promotion/Control Plane selection: 57 passed. Broader release/Candidate/Control Plane/update/public-Bootstrap/live selection: 474 passed / 7 explicit platform skips / 1,431 deselected. Python compile/Ruff, workflow YAML and `git diff --check` pass. |
| Current-source supply chain | 0 | 23 locked/licensed Runtime packages; 459 production files pass the bounded secret scan, inventory `2f8f67ba...3ee05f`; all 646 source files are Git-admitted. Ignored report SHA-256 `8756db71...7b673e`, 21,857 bytes. |
| Publication | blocked | No protected 24-receipt accepted Candidate or live provider/CDP session exists. No origin, Control Plane, deployment or user update was contacted. |

## Complete signed-gate batch rerun and durable ordering correction - 2026-07-14

| Scope | Exit | Result |
| --- | ---: | --- |
| First full-suite diagnostic | 1 | 1,877 passed / 17 skipped / 2 failed. The failures were not waived: Candidate workflow Runtime lock-profile count had drifted from four to five, and same-tick random ULIDs reordered three Replay user Items after restart. |
| Root correction | 0 | Dependency-lock contract now fixes all five Candidate Runtime profile install sites. Shared ULIDs are monotonic under a process lock, use a fresh 80-bit seed per new millisecond, preserve order through clock rollback and reset across process identity change. |
| Replay stress | 0 | Coarse-clock and clock-regression identity tests pass; the real Live Replay revision/restart/idempotency case passes 20/20 independent iterations. |
| Complete Python v1 suite | 0 | Final current-source rerun: 1,881 passed / 17 explicit environment skips / 0 failed in 763.48 seconds. Five warnings are upstream deprecation notices only. |
| Current-source supply chain | 0 | 23 locked/licensed Runtime packages; 454 production files pass the bounded secret scan, inventory `86135129...91bf`; all 640 authoritative v1 source files are Git-admitted. Ignored local report SHA-256 `d4e32fd9...e100`, 21,857 bytes. |

## Split-workflow dependency convergence - 2026-07-14

| Scope | Exit | Result |
| --- | ---: | --- |
| Initial full-suite diagnostic | 1 | 1,894 passed / 17 skipped / 1 failed. The sole failure was the stale five-site Candidate Runtime-lock count after publication moved to its own workflow; it was not waived. |
| Workflow dependency authority | 0 | Candidate now requires exact dev/cloud/runtime and Node/npm inputs; publication independently requires exactly two Runtime-profile installs and zero Node/npm installs. CI/platform profiles, Python 3.11.9, Node 22.23.1 where applicable and digest-pinned Actions remain enforced. |
| Affected regression | 0 | Dependency, Candidate, immutable handoff, asset/Bootstrap publication and signed Windows drill selection: 59 passed / 0 failed. |
| Complete Python v1 suite | 0 | 1,895 passed / 17 explicit environment skips / 0 failed in 758.93 seconds. Five warnings are upstream deprecation notices only. |
| Static/source gates | 0 | Ruff, Python compilation, dependency-lock validation, `git diff --check` and the 646-file source-tree admission gate pass. Dependency inventory covers 23 Runtime and 282 npm packages. |
| Current-source supply chain | 0 | 23 locked/licensed Runtime packages; 459 production files pass the bounded secret scan, inventory `2f169bf3...4c9540`. Ignored 21,857-byte report SHA-256 `ac3ac6f9...c9b3a8`. |
| Publication | blocked | No protected 24-receipt Candidate or real managed Model/Image/CDP session exists. No workflow dispatch, origin write, Control Plane mutation or user update occurred. |

## Live GitHub repository governance audit - 2026-07-14

| Scope | Exit | Result |
| --- | ---: | --- |
| Remote inventory | 2 | Private repository default branch is `main` at `b52999b0...66e71`; four legacy workflows are active, while all four v1 workflows are absent. No Environment or self-hosted Runner exists. |
| Credential boundary | 1 | The branch push was rejected by GitHub because the active OAuth identity lacks `workflow`. The remote ref did not move and no partial workflow update occurred. |
| Actions policy | blocked | Actions are enabled with read-only default `GITHUB_TOKEN` and no PR-approval permission, but `allowed_actions=all`; three allowlist findings remain. |
| Repository contract | 0 | Exact workflows/status contexts, strict protected-main policy, six protected Environments, variable/Secret names and seven Runner roles are represented without Secret values. Signing/live/publication Runner overlap fails closed. |
| Governance mutation fence | 0 | Bootstrap requires exact repository confirmation, default-branch SHA and reviewer; a head race produces zero writes. Only idempotent Environment, Actions-policy and branch-protection PUTs are available. |
| Live audit receipt | 2 | 22 blockers: Actions 3, branch 1, credential 1, Environment 6, Runner 7, workflow 4. Report SHA-256 `a81a2f26...c0c15e`, 3,621 bytes. |
| Regression | 0 | Governance unit/transport tests: 9 passed. Affected package/release/Candidate/dependency/Control Plane selection: 51 passed. Ruff, Python compilation, dependency locks, diff and the 650-file source gate pass. Contract/evaluator and administration transport are separate modules. |
| Complete Python v1 suite | 0 | 1,904 passed / 17 explicit environment skips / 0 failed in 753.89 seconds. Five warnings are upstream deprecation notices only. |
| Current-source supply chain | 0 | 23 locked/licensed Runtime packages; 462 production files pass the bounded secret scan, inventory `46f9c75f...d32210`. Ignored 21,857-byte report SHA-256 `573c9141...5507a8`. |
| External mutation | 0 | No repository setting, ref, workflow dispatch, release asset, Control Plane state or user update was changed. |

## Ephemeral non-privileged release capacity - 2026-07-14

| Scope | Exit | Result |
| --- | ---: | --- |
| Platform mapping | 0 | Protected Windows x64 uses isolated `[self-hosted, windows, x64, ecorex-platform-windows]`; macOS arm64 uses hosted `macos-15`; macOS x64 uses hosted `macos-15-intel`. All remain protected-Environment-gated and emit content-bound stage receipts. |
| Hosted image soak | 0 | Candidate uses fresh `ubuntu-24.04`, retains PostgreSQL 16.9/MinIO, 256 jobs, 48 workers, two node IDs and the 14,400-second minimum inside the documented six-hour hosted-job ceiling. |
| Runtime config transport | 0 | Base64 is capped at GitHub's 48 KiB variable limit/36 KiB decoded; independent SHA-256, strict JSON/duplicate-key checks, exact output name, exclusive materialization, stable file identity and digest-fenced `always()` cleanup fail closed. No config bytes or path enter the receipt. |
| Isolation boundary | 0 | Repository readiness requires one non-privileged exact-toolchain Windows stage Runner plus three distinct privileged roles: external signing, Windows live Model/Image/CDP acceptance and publication. The four physical identities may not overlap. |
| Focused/affected regression | 0 | Transport/governance/gate/dependency: 32 passed. Platform/Candidate/package selection: 103 passed / 1 explicit platform skip / 0 failed. Workflow YAML parses. |
| Complete Python v1 suite | 0 | 1,915 passed / 17 explicit environment skips / 0 failed in 758.22 seconds. Five warnings are upstream deprecation notices only. |
| Static/source gates | 0 | Full Ruff over `ecorex`, `scripts`, `tests/v1`, Python compilation, dependency locks, `git diff --check` and all 653 Git-admitted source files pass. Five old dynamic-import E402 annotations were made explicit. |
| Current-source supply chain | 0 | 23 locked/licensed Runtime packages, 282 npm packages and 464 production files pass; inventory `5515c74a...9bf58`. Ignored 21,857-byte report SHA-256 `5ace12cb...a54e`. |
| Live repository audit | 2 | 18 blockers remain: Actions 3, branch 1, OAuth workflow scope 1, Environments 6, privileged Runners 3 and inactive v1 workflows 4. Report SHA-256 `c8d7d0f3...a114`, 3,226 bytes. |
| External mutation | 0 | No repository setting/ref, workflow dispatch, provider request, origin write, Control Plane mutation or user update occurred. |

## First hosted CI matrix correction - 2026-07-14

| Scope | Exit | Result |
| --- | ---: | --- |
| Initial remote CI | 1 | Run `29292576944` on commit `f772d0c1`: macOS arm64 and x64 passed; Ubuntu reported 13 failures; Windows x64 reported 27 failures from one native-build root. Cross-runner byte stability correctly stayed closed. |
| Windows first diagnostic | 0 | The builder now searches both standard VS 2022 SpecialFolder roots, rejects reparse roots, deduplicates candidates and still requires one exact manifest-pinned compiler/toolchain. |
| Ubuntu root causes | 0 | npm lock installation now precedes the real Web build inside pytest; inactive `colorama` remains license-accounted without host installation; Candidate/platform tests use staged identities and a resolved regular interpreter rather than host assumptions/symlinks. |
| Runtime consistency | 0 | POSIX output roots are held by Runtime-owned descriptors to prevent inode-reuse replacement; quarantine filesystem errors are normalized at the domain boundary. Output descriptors close on Runtime shutdown and intentionally survive logout. |
| Original failure selection | 0 | 12 passed / 1 explicit Windows symlink-privilege skip / 0 failed. |
| Affected regression | 0 | 171 passed / 5 explicit platform skips / 0 failed in 135.42 seconds. |
| Complete Python v1 suite | 0 | 1,916 passed / 17 explicit environment skips / 0 failed in 777.01 seconds. Five warnings are upstream deprecation notices only. |
| WebUI | 0 | npm lock install and audit passed with 0 vulnerabilities; TypeScript passed; 162 contract tests passed; production build emitted 18 content-addressed assets and 17 chunks. |
| Static/release gates | 0 | Ruff, Python compilation, workflow YAML, design system, legacy cutoff, public download, dependency locks, Runtime/Server schema authority, reproducibility, `git diff --check` and 653 source files pass. |
| Current-source supply chain | 0 | 23 locked/licensed Runtime packages, 282 npm packages and 464 production files pass; inventory `e3698863...4888fac1`; ignored report SHA-256 `3985d06e...62093ef0`. |
| Second remote CI | 1 | Run `29294544893` on commit `7411c561`: Ubuntu quality and macOS arm64/x64 passed; Windows failed closed because current `windows-latest` has VS 2026 and no VS 2022 installation; byte comparison correctly remained closed. |
| Third Windows diagnostic | 1 | Run `29294972413`, commit `ba595b5b`, reached VS 2022 on `windows-2022` but rejected its weekly-image `cl.exe` digest against the exact release manifest. The hosted label is not treated as immutable Candidate authority. |
| Windows CI contract | 0 | CI alone may request a `win22`-bounded compatibility build: source/MSVC family/SDK/layout/Microsoft Authenticode/file locks remain enforced, observed hashes are recorded, and its `github-hosted-ci-compatibility` receipt is rejected by the production stager. |
| Windows release contract | 0 | Protected stage targets the isolated `ecorex-platform-windows` Runner and retains exact caller-pinned tools, libraries, versions, thumbprints and hashes. Repository readiness adds that role and forbids overlap with sign/live/publication. |
| Local runner-contract regression | 0 | 19 focused tests pass; workflow YAML, progress JSON, dependency locks, 653-file admission, reproducibility and diff gates pass. Supply-chain preflight covers 23 Runtime, 282 npm and 464 production files; inventory `51324655...21d068`, ignored report `716078b0...057c9a`. |
| Local dual-mode regression | 0 | Default exact-mode selection passed 90 executable tests / 2 platform skips; corrected contracts passed 57 / 1 skip; simulated GitHub compatibility mode compiled real helpers and passed the native Runtime probe. Full Ruff/compile passes. |
| Complete current-source suite | 0 | 1,916 passed / 17 explicit environment-platform skips / 0 failed in 761.34 seconds. Five warnings are unchanged upstream Starlette/websockets deprecations. |
| Branch status authority | 0 | Repository governance now requires the actual expanded Job context `Windows x64 compatibility`; the stale `Windows compatibility` name cannot leave branch protection permanently unsatisfied. |
| Current-source supply chain | 0 | 23 Runtime packages, 282 npm packages and 464 production files pass; inventory `45083146...4b8528`; ignored 21,857-byte report `a087fbc2...3887ad`. |
| Live repository audit | 2 | 17 blockers: Actions 3, branch 1, Environments 6, isolated Runners 4 and inactive protected workflows 3. OAuth `workflow` scope and active v1 CI are now confirmed. Report 3,167 bytes, SHA-256 `d9eb1f47...38a2c8`. No mutation occurred. |
| Fourth remote Windows native gate | 0 | Run `29296280821`, commit `afcb166b`: Ubuntu quality, both macOS architectures and all 150 hosted Windows platform-sensitive Runtime/native tests passed. |
| Fourth remote Web gate | 1 | Windows failed only at generated TypeScript byte check: checkout used CRLF because `.ts/.tsx` were absent from `.gitattributes`; build and byte upload correctly stayed closed. |
| TypeScript byte policy | 0 | `*.ts` and `*.tsx` now require LF in Git attributes and the reproducibility gate. Generated contract check, reproducibility, 8 focused tests and Web typecheck pass; no generated schema bytes changed. |
| Current-source supply chain after EOL policy | 0 | 23 Runtime, 282 npm and 464 production files pass; inventory `cfb99101...d0e00`, ignored 21,857-byte report `9084448e...157a2`. |
| Fifth remote matrix | 0 | Run `29296609455`, exact commit `a70d65c3`: Ubuntu quality, Windows x64, macOS arm64/x64 and Cross-runner byte stability all passed. Windows completed contracts, TypeScript, content-addressed Web build and byte upload. |
| Evidence boundary | 0 | The green matrix is read-only CI evidence, not protected platform-stage, Candidate signing, live Model/Image/CDP acceptance, publication or rollout evidence. |

## Live upstream recovery and final browser rerun - 2026-07-14

| Scope | Exit | Result |
| --- | ---: | --- |
| Final Draft PR head | 0 | Hosted run `29296947260` on exact head `a11dbd884054130ecec145c0a2625ec4eb2c4cca` passed Ubuntu quality, Windows x64, macOS arm64/x64 and Cross-runner byte stability. |
| GPT-5.6 SOL catalog | 0 | Real upstream catalog returned 200 in 971 ms and now contains `gpt-5.6-sol`. No catalog contents, endpoint or credential were recorded. |
| GPT-5.6 SOL inference | 0 | One medium-reasoning Responses request returned 200 and a terminal completion in 2,839 ms under the 272,000-token policy. Response content was not recorded. |
| Image 2 single-flight | 0 | One bounded low-cost request completed in 48,284 ms as a 1,962,951-byte PNG, SHA-256 `43458986...12217`; load was not increased until this admission passed. |
| Image 2 bounded concurrency | 0 | Hard limit four, no automatic retry: 4/4 completed in 47,881 ms, four unique digests, zero 5xx. Individual durations were 22,818 / 47,866 / 43,384 / 45,918 ms. |
| Real rectangle retouch | 0 | Image 2 returned a 1,945,772-byte new revision in 55,424 ms. Target change score `0.182087`; non-target similarity `0.991565`; visual inspection confirmed the localized edit. |
| Current WebUI browser | 0 | 1440x900 in-app browser rerun passed first-message model selection, independent Image 2 mode, `0.000030517578125px` normal-Composer bottom delta, continuous reasoning replacement, fit-first preview and structured Retouch result. |
| Settings and extensions | 0 | Output preference changed to Downloads; memory 2 → 0 with 24-hour undo; Full Access became persistently visible with revoke; unified Skill/MCP/tool catalog and formal Feishu/Tencent Docs entries were visible. |
| Task continuation and share | 0 | `thr_target_ga` restored the exact independent projection. The real loopback Control Plane share renderer preserved `你的指令` then `EcoreX`, served a complete 1800x1100 image with `object-fit: contain`, zero overflow and no scripts. |
| Browser diagnostics | 0 | Main and share/raw-image tabs emitted zero console warnings or errors. |
| Tracked evidence | 0 | `evidence/live-provider-local-diagnostic-2026-07-14.json` records only policy identities, status, timing, byte counts, hashes and quality metrics; prompts, output text, provider origin and credentials are absent. |
| Publication authority | blocked | These are direct-upstream and loopback diagnostics, not Candidate-bound protected live-acceptance receipts. Repository audit still has 17 blockers; no Candidate, publication or rollout was attempted. |

## Generated Thread/Turn projection authority - 2026-07-14

| Scope | Exit | Result |
| --- | ---: | --- |
| FastAPI response authority | 0 | Eleven critical Thread/Turn mutation and projection routes declare exact Pydantic response models; OpenAPI status/schema references are executable assertions. |
| Generated contract | 0 | Deterministic schema digest `70cf8d1...a2ac6` includes Thread/List/Turn/Item/Job/Interaction, mutation, replace and full projection wire shapes plus Runtime enums. Generator freshness and digest tests pass. |
| Web transport boundary | 0 | A deferred strict validator rejects missing/extra fields, invalid states/timestamps, cross-Thread contamination and Job/Turn/replace identity drift before reducer state. Turn/Item `inherited` facts are no longer omitted. |
| Focused Runtime | 0 | 44 Runtime hardening, kernel API and projection-invariant integration tests passed. Ruff and Python compilation passed. |
| Complete Python v1 suite | 0 | 1,916 passed / 17 explicit environment-platform skips / 0 failed in 831.79 seconds. Five warnings are unchanged upstream Starlette/websockets deprecations. |
| WebUI | 0 | TypeScript passed; 163/163 Web contract tests passed; the content-addressed production build emitted 19 assets / 18 chunks. Initial JS is 474.65 KiB (147.02 KiB gzip) under the unchanged 475 KiB limit; deferred projection validation is 11.09 KiB. |
| Static/source gates | 0 | Design, legacy cutoff, public download, dependency locks, Runtime/Server schema authority, `git diff --check` and all 655 admitted source files pass. |
| Current-source supply chain | 0 | 23 locked/licensed Runtime packages, 282 npm packages and 466 production files pass; inventory `669aa0f4...e8394`. Ignored 21,857-byte report SHA-256 `f50da3a5...6857a`. npm audit reports zero vulnerabilities. |
| Exact hosted source head | 0 | Run `29301500258` on source-bearing commit `6d4c3030717ce078a6d5a74b830ec9a169a32d2e` passed Ubuntu quality, Windows x64, macOS arm64/x64 and cross-runner byte stability. All five Jobs completed successfully. |
| Publication | blocked | The 17 repository-governance/Runner/workflow prerequisites remain; no protected Candidate, publication or rollout was attempted. |

## Connector-login lifecycle projection authority - 2026-07-15

| Scope | Exit | Result |
| --- | ---: | --- |
| Root-cause reproduction | 1 | The first complete run exposed the prior completed-check replay leak: raw DurableJob fields failed the new response contract and the second check returned 500. The failed run is not counted as passing evidence. |
| Single-snapshot Runtime projection | 0 | Interaction, optional Turn/Job and event watermark are read in one SQLite reader transaction; Job crosses the public boundary only as the thirteen-field JobProjection allowlist. A regression test rejects lease, token, heartbeat, checkpoint, payload, idempotency and raw-error leakage. |
| FastAPI/OpenAPI authority | 0 | Connector begin/check/cancel declare exact Pydantic response models; check `200` and `202` both reference `ConnectorLoginCheckResponse`. State and nested Interaction/Connector/Thread/Turn/Job identities fail closed. |
| Generated Web boundary | 0 | Schema digest `310063327...d12195d` includes all three connector lifecycle responses plus InteractionMutationResponse. One deferred validator covers connector lifecycle and ordinary HITL mutation responses without duplicating initial-client code. |
| Focused Runtime | 0 | The exact failed replay test passes; 58 Runtime hardening, Connector integration/mount and progressive-discovery tests pass with one upstream warning. |
| Complete Python v1 suite | 0 | Fresh post-fix run: 1,916 passed / 17 explicit environment-platform skips / 0 failed in 818.30 seconds; five warnings are unchanged third-party deprecations. |
| WebUI | 0 | TypeScript passed; 164/164 Web contract tests passed; production build emitted 19 content-addressed assets / 18 chunks. Initial JS is 474.84 KiB (147.05 KiB gzip), deferred features 94.42 KiB (33.67 KiB gzip), strict projection chunk 15.40 KiB (4.04 KiB gzip). |
| Static/source gates | 0 | Ruff, Python compilation, generated-contract freshness, design, legacy cutoff, public download, dependency locks, Runtime/Server schema authority, reproducibility, `git diff --check` and all 655 admitted source files pass. |
| Current-source supply chain | 0 | 23 locked/licensed Runtime packages, 282 npm packages and 466 production files pass; inventory `33048f78...93dfc`; ignored 21,857-byte report SHA-256 `355cbb87...6f4d31`. npm audit reports zero vulnerabilities. |
| Exact hosted source head | 0 | Run `29357245885` on `3b9d684a311828d913f3c29c626f6b68f4e6cd95` passed Ubuntu quality, Windows x64, macOS arm64/x64 and Cross-runner byte stability; all five Jobs succeeded. Draft PR #2 remains CLEAN and Draft. |
| Live repository audit | 2 | Exactly 17 blockers remain: Actions 3, branch 1, Environments 6, isolated Runners 4 and inactive protected workflows 3. The 3,167-byte report is byte-identical to the prior audit, SHA-256 `d9eb1f47...38a2c8`; no governance mutation occurred. |
| Publication | blocked | Hosted CI is read-only source evidence. No protected Candidate, managed provider/CDP acceptance receipt, release, rollout or user update was attempted. |

## Reviewed Node 24 GitHub Actions closure - 2026-07-15

| Scope | Exit | Result |
| --- | ---: | --- |
| Action provenance | 0 | Six official `actions/*` releases use verified Node 24 commit SHAs. Declarative lock SHA-256 `4c6d80f5...144b5a97`; protected self-hosted Runner minimum is 2.327.1. |
| Workflow inventory | 0 | Exactly four v1 workflows remain. Every `.yml/.yaml`, `uses:` line and checkout credential setting is fail-closed; two inherited CowAgent Docker publishers are deleted and permanently retired. |
| Focused mutation regression | 0 | 49 dependency/release/Candidate tests pass; unreviewed workflow inventory, Action drift, unverified lock entries and checkout credential persistence are rejected. Workflow YAML parses. |
| Shutdown deadline diagnostic | 1 then 0 | First full run: 1,921 passed and one outer Windows wall-clock assertion hit the duplicated `3.5 < 3.5` boundary. Child functional shutdown budgets remained `<0.8s`; process exit remained bounded by `timeout=4`. After separating these authorities, the exact test passed 5/5 repeated runs. |
| Complete Python v1 suite | 0 | Exact current source: 1,922 passed / 17 explicit environment skips / 0 failed in 1,255.82 seconds. JUnit SHA-256 `2ab35f02...a51bbd5c`; five warnings are upstream deprecations only. |
| WebUI | 0 | npm audit reports zero vulnerabilities; TypeScript and 164/164 tests pass; production emits 19 content-addressed assets / 18 chunks with 474.84 KiB raw and 147.05 KiB gzip initial JS. |
| Static and byte gates | 0 | Ruff/compile, design, legacy, public download, dependency, Runtime/Server schema, source/diff and reproducibility gates pass across 656 admitted files. Byte contract SHA-256 `a6cc2b6c...14a64b36`. |
| Current-source supply chain | 0 | 23 locked/licensed Runtime packages, 282 npm packages and 467 production files pass; inventory `e488a5e9...0db67edb`; ignored report SHA-256 `0a0dc45a...2ea49dc`. |
| External mutation | 0 | This checkpoint does not claim hosted execution, governance mutation, protected Candidate, publication or rollout. |
| Exact hosted source head | 0 | Run `29382330122` on `fd05f42413b2563e34f15421e58991248f3bdee2` passed Ubuntu quality, Windows x64, macOS arm64/x64 and Cross-runner byte stability; all five Jobs succeeded. |
| Hosted Action diagnostics | 0 | All five check runs contain zero annotations; complete hosted logs contain no Node 20 forced-runtime or deprecated-Action warning. checkout/setup/upload/download Node 24 revisions executed successfully. |
| Draft PR state | 0 | PR #2 is Draft, CLEAN and MERGEABLE at exact source head `fd05f424...3bdee2`. |
| Live repository audit | 2 | The 3,167-byte report is byte-identical at exactly 17 blockers: Actions 3, branch 1, Environments 6, isolated Runners 4 and protected workflows inactive on main 3. SHA-256 `d9eb1f47...38a2c8`; no governance mutation occurred. |
| Publication | blocked | The two retired CowAgent workflows remain active on current main until reviewed merge. No protected Candidate, publication, rollout or user update was attempted. |

## Secondary settings and operations response authority - 2026-07-15

| Scope | Exit | Result |
| --- | ---: | --- |
| Root response boundary | 0 | Memory 3, migration quarantine 2, Output 5 and System 2 JSON routes all declare one of nine strict Pydantic response models; OpenAPI references and `additionalProperties: false` are executable assertions. |
| Generated schema | 0 | Canonical schema contains 36 contracts at digest `877c962e...654b0d50`; the dedicated generated settings manifest pins every nested field and enum without enlarging the bootstrap manifest. |
| Web transport | 0 | Nine progressively loaded validators reject missing/additional fields, unsafe integers, malformed timestamps/digests, aggregate or lifecycle drift, health worst-state drift and materialization identity drift before state admission. |
| Bundle root-cause checks | 1 then 0 | Eager validation failed the unchanged 475 KiB gate at 485.73 KiB; the first lazy graph exposed a reverse client dependency. The independent generated settings manifest and branded error removed both failures. |
| Timing-test root causes | 1 then 0 | Cold startup was removed from the shutdown process-exit clock; ready/start handshake passed 10/10. Fixed 40 ms maintenance sleep became a second-call Event and passed 10/10. Combined connector/shutdown files pass 26/26 without relaxing production deadlines. |
| Complete Python v1 suite | 0 | Current source: 1,926 passed / 17 explicit environment-platform skips / 0 failed. JUnit: 385,219 bytes, 1,943 cases, 1,680.264 seconds, SHA-256 `dc050061...e54f2db`. |
| WebUI | 0 | npm audit reports zero vulnerabilities; TypeScript and 167/167 Web tests pass; production build emits 20 content-addressed assets / 19 chunks. Initial JS is 474.99 KiB raw / 147.12 KiB gzip; deferred settings validation is 11.73 KiB / 3.59 KiB gzip. |
| Static and byte gates | 0 | Ruff/compile, generated freshness, design debt all-zero, legacy, public download, dependency locks, Runtime/Server schema, reproducibility, diff and 658-source-file admission pass. Byte contract is 7,380 bytes, SHA-256 `65eb5c81...1ff3ec5`. |
| Current-source supply chain | 0 | 23 Runtime and 282 npm packages are license-accounted; 468 production files pass secret scanning, inventory `a7b2ff6f...31a8d5a`. Ignored report is 21,857 bytes, SHA-256 `a742ef60...3862282`. |
| External mutation | 0 | This local checkpoint does not mutate governance, create a protected Candidate, call managed live acceptance, publish, roll out or update a user. Hosted exact-source evidence remains pending the source commit. |
| Exact hosted source head | 0 | Run `29390253811` on `ee8a7f8cc77830b66358af3acc9206f95cb5923b` passed Ubuntu quality, Windows x64, macOS arm64/x64 and Cross-runner byte stability; all five Jobs succeeded. |
| Hosted diagnostics | 0 | All five checks contain zero annotations; complete logs contain zero Node 20 forced-runtime or deprecated-Action warnings. |
| Draft PR and live audit | 2 | PR #2 is Draft, CLEAN and MERGEABLE at the exact source head. Read-only governance audit remains byte-identical at 17 blockers; 3,167-byte receipt SHA-256 `d9eb1f47...38a2c8`, action `none`. |
| Publication | blocked | Hosted CI is read-only source evidence. No protected Candidate, managed provider/CDP receipt, publication, rollout or user update was attempted. |

## Artifact response authority and measured Workbench surfaces - 2026-07-15

| Scope | Exit | Result |
| --- | ---: | --- |
| FastAPI response authority | 0 | Eleven Artifact/Retouch JSON routes declare six strict response families; five binary routes explicitly publish no JSON response model. Internal families, extra fields and cross-identity/lifecycle drift fail closed. |
| Stable failure boundary | 0 | An injected internal source-code projection returns the stable 500 API error without leaking Artifact ID, filename or family. |
| Generated schema | 0 | Canonical schema contains 42 contracts at digest `5face1da...3f12b`; a dedicated generated Artifact manifest owns nested wire fields and Retouch enums. |
| Web transport | 0 | Artifact request construction and validation load behind one delayed boundary. Missing/extra fields, count/digest/timestamp/geometry/mask/Job/workspace drift are rejected before React state. Optional brush width remains compatible with the backend default. |
| Production dependency graph | 1 then 0 | The first build exposed a deferred-validator ↔ initial-client cycle. A shared contract-core and delayed Artifact operation boundary restore a one-way content-addressable graph without increasing the budget. |
| Surface reference mapping | 0 | Measured light non-chat/chat/session/scrollbar are `#f7f7f7/#ffffff/#ebebeb/#e5e5e5`; dark are `#0f0f0f/#111111/#202020/#202020`. Light Composer equals chat; dark Composer equals current conversation and scrollbar. Components use semantic tokens only. |
| Focused Runtime | 0 | Ruff passes; 149 Artifact/Retouch Python tests pass with one unchanged upstream Starlette warning. |
| WebUI contracts | 0 | TypeScript passes; 176/176 Web contract tests pass, including generated freshness, strict Artifact boundaries, surface mapping, contrast, forced colours, density and progressive loading. |
| Production Web build | 0 | 24 assets / 23 JavaScript chunks are content-addressed. Initial JS is 474.22 KiB raw / 147.29 KiB gzip under the unchanged 475/150 KiB limits; delayed Artifact operations are 18.36/4.68 KiB. |
| Browser matrix | 0 | Chromium E2E passes 36/36 at 1440x900, 1024x768, 768x900, 390x844 and 320x568 in both themes with zero axe violations; interaction coverage includes Composer placement, sparse frames, task continuation, reasoning, HITL, share, fit-first preview and touch Artifact actions. |
| Pixel evidence | 0 | Captured desktop corners are exactly dark/light non-chat `#0f0f0f/#f7f7f7`; workspace pixels are exactly `#111111/#ffffff`. |
| Complete-suite evidence boundary | pending | The most recent complete 1,926-test Python and five-platform hosted run belongs to prior exact source `ee8a7f8c...ed19`; it is not claimed for this batch. |
| Publication | blocked | The live audit remains exactly 17 blockers. No protected Candidate, managed provider/CDP acceptance receipt, publication, rollout or user update was attempted. |

## Administrator operations, Skills and v0.3 view-more - 2026-07-15

| Scope | Exit | Result |
| --- | ---: | --- |
| Focused backend | 0 | 94 passed, 2 explicit skips, 1 upstream Starlette warning across administrator management, dynamic image revisions, admin Web, extensions, managed model/session catalog, Output, Thread catalog, Settings and device authorization. |
| Model secret boundary | 0 | AES-GCM revisions never return plaintext; failed tests cannot activate; successful test+default activation is transactional and idempotent. Chat drains old revisions; image Jobs remain bound to their admitted revision across retry/restart. |
| Skills authority | 0 | Runtime supplies v0.3-compatible category/icon/action reason; core required entries reject disable in the service. React contains no source/trust/category inference and the legacy modal is removed. |
| Web contracts | 0 | `npm run test:v1` passes 180/180, including generated schema freshness, backend category/action ownership, reload persistence, view-more fixture and progressive chunk boundaries. |
| Production Web build | 0 | 24 chunks; initial JavaScript 459.76 KiB raw / 146.20 KiB gzip, deferred features 136.30 KiB. Explicit named/default lazy exports prevent the production React #306 white screen found during the first full browser pass. |
| Browser matrix | 0 | Chromium passes 45/45. The 12-general/11-project fixture proves independent 8 → all → 8 history expansion and guarantees a running old Thread remains visible. Admin user/model/full rollout, Skills, logout, model icon, clipboard, forced colours and responsive paths also pass. |
| Static/source gates | 0 | Ruff, Python compilation, generated contracts, TypeScript, diff check and 675 admitted source files pass. Roughly 590 unreachable legacy extension CSS lines were removed before the exact build/browser rerun. |
| Current-source supply chain | 0 | 23 Runtime packages and 282 npm packages remain license-accounted; 483 production files pass secret scanning. Inventory `927cf173...15c125cd7`; ignored 21,857-byte report SHA-256 `e1239bd1...c9f18c95`. |
| Operator record | 0 | `admin-management-runbook.md` documents roles, exact enablement/origin/secret variables, hot activation semantics, single-node boundary, rollback and failure handling; Control Plane, Gateway and Image runbooks link to it. |
| Publication | blocked | This is uncommitted current-source evidence, not a protected Candidate. No administrator rollout, publication or installed-user update was attempted. |

## Hosted administrator-batch gate correction - 2026-07-16

| Scope | Exit | Result |
| --- | ---: | --- |
| First exact hosted run | 1 | Run `29430838857` at `efc9e21c30eb712d4c3cdbc6cb3b40f0cad9cbd5`: Windows x64 and macOS arm64/x64 passed; Ubuntu completed 1,925 tests and failed only the design-system and server-schema-authority gates. Cross-runner byte comparison correctly stayed closed. |
| Design root cause | 0 | Two undeclared compact-size aliases were replaced by existing locked tokens. Ten literal one-pixel focus shadows now use semantic subtle focus-ring tokens with forced-colour mappings; no arbitrary shadow, colour or radius exception was added. |
| Schema root cause | 0 | `management_schema.py` is explicitly the ninth migration authority. Business repositories remain DDL-free and the exact-small-allowlist regression was updated; no schema bytes or migration checksum changed. |
| Focused correction | 0 | Direct design and server authority gates pass; 13 management, dynamic-image and gate tests pass with one unchanged upstream warning. |
| Complete Python v1 suite | 0 | Fresh Windows run: 1,942 passed / 17 explicit environment-platform skips / 0 failed in 1,538.16 seconds. Five warnings are unchanged Starlette/websockets deprecations. |
| WebUI | 0 | TypeScript and 180/180 Web contract tests pass. The content-addressed build emits 24 chunks; initial JavaScript is 459.76 KiB raw / 146.20 KiB gzip and deferred features are 136.30 KiB. |
| Browser matrix | 0 | Fresh corrected-source Chromium run passes 45/45 in 2.6 minutes across both themes and all locked viewports, with zero axe violations. Focus rings, compact completion copy, v0.3 “查看更多”, reasoning, image/Retouch, Skills and administrator operations remain functional. |
| Static/source gates | 0 | Ruff, Python compilation, design debt all-zero, nine-authority server schema, 675-file source admission and `git diff --check` pass. |
| Current-source supply chain | 0 | npm audit reports zero vulnerabilities. 23 Runtime packages and 282 npm packages are license-accounted; 483 production files pass secret scanning. Inventory `9320f611...77ae19`; ignored 21,857-byte report SHA-256 `39696bfd...deed1e`. |
| Exact hosted correction | 0 | Run `29435356727` on exact source `f3142be9545c87ec461a9478f6c1771c14ea9266` passed Ubuntu quality, Windows x64, macOS arm64/x64 and Cross-runner byte stability; all five Jobs succeeded. |
| Draft PR state | 0 | PR #2 remains Draft, CLEAN and MERGEABLE with the five exact-head checks successful. |
| Publication | blocked | No protected Candidate, managed provider/CDP receipt, publication, rollout or installed-user update was attempted. |

## Main merge, repository governance and compensation - 2026-07-16

| Scope | Exit | Result |
| --- | ---: | --- |
| Reviewed merge | 0 | PR #2 was squash-merged; exact main is `c8fd385c5600664a2f9217c64773af5fed2fd21f`. |
| Main hosted matrix | 0 | Run `29436909984` passed Ubuntu quality, Windows x64, macOS arm64/x64 and Cross-runner byte stability; all five Jobs succeeded. |
| Actions/workflow governance | 0 | Actions are selected GitHub-owned only, workflow default permission is read, review approval is disabled and all four reviewed v1 workflows are active. |
| Main protection | 0 | Five strict checks, required PR review, administrator enforcement, linear history, conversation resolution and force-push/deletion denial are active. |
| Post-governance audit | 2 | Exactly 10 blockers remain: six protected Environments and four distinct online role-labelled Runners. The clean 2,669-byte report SHA-256 is `f7736921e2287080ef1425c356bd1accaa4705c41a1c490c9d5c5698d92404cf`. |
| Environment plan boundary | 1 | GitHub rejects required reviewers for this private user-owned repository plan with HTTP 422. The API had partially created one empty Environment; it was deleted and the inventory returned to zero. No reviewer-free workaround or visibility change occurred. |
| Focused governance hardening | 0 | 13 tests pass with one upstream warning; Ruff and Python compilation pass. New and pre-existing Environment failures, compensation failure and non-sensitive CLI receipts are covered. |
| Adjacent release contracts | 0 | 57 Candidate, publication-handoff, publisher, gate and evidence tests pass with one explicit environment skip. The 675-file source, dependency-lock and Runtime/server schema-authority gates pass. |
| Real compensation integration | 1 expected | Bootstrap against exact main reports `github_environment_reviewers_plan_unsupported`, `compensated=true`, action `none`, and leaves Environment count zero. |
| Exact hosted hardening | 0 | Draft PR #3 run `29438953446` on `f49f187d3a114aeb4312f62dfb0a5867221257bd` passed Ubuntu quality, Windows x64, macOS arm64/x64 and Cross-runner byte stability; all five Jobs succeeded. |
| v0.3 view-more retention | 0 | Existing 45/45 browser evidence continues to cover independent general/project 8 → all → 8 expansion and operational inclusion of an old running Thread. |
| Publication | blocked | Protected Candidate, live provider/CDP acceptance, release publication, rollout and installed-user update were not attempted. |

## Current-main Windows full-Pack Candidate drill - 2026-07-16

| Scope | Exit | Result |
| --- | ---: | --- |
| Exact main | 0 | PR #3 merged as `701aa4228635acb9584703592110193412dce600`; main run `29439964797` passed Ubuntu quality, Windows x64, macOS arm64/x64 and Cross-runner byte stability. |
| Exact-main quality | 0 | Hosted Ubuntu completed 1,931 Python tests / 32 explicit skips, 180/180 Web tests, the 24-chunk build at 459.76 KiB raw / 146.20 KiB gzip initial JS, and all static gates. |
| Post-merge governance audit | 2 | Action `none`; exactly six missing Environments plus four unavailable Runner roles. Environment and Runner inventories are empty. Report is 2,669 bytes, SHA-256 `c411da41...6d48`. |
| Clean Candidate input | 0 | Detached exact-main worktree remained clean after locked Web dependency install/typecheck/build; the user's `.artifacts/` was neither copied nor modified. |
| Windows platform stage | 0 | Go 1.26.5, MSVC 14.44/compiler 19.44 and Windows SDK 10.0.26100.0 produced eight source-pinned Core/Bootstrap/browser/channels/image/OCR/Office/sandbox receipts. Native-build receipt schema 2 passed. |
| Local signed Candidate | 0 | Full drill passed in 3,051.188s. Core is 21,526,655 bytes, Bootstrap 3,109,078, browser Pack 190,153,571, OCR Pack 94,993,855 and Office Pack 14,836,807; every signed artifact remained under its identity-specific limit. |
| Install/migration/update | 0 | First install and healthy replacement completed with HTTP 200; CoW migration receipt committed and restarted without its source; three durable drain checkpoints preceded activation; refresh completed. |
| Failure recovery | 0 | Mirror outage fell through to GitHub with zero cross-source partial reuse. Bad digest was rejected without activation. Fault replacement ended in rollback, prior Runtime returned HTTP 200 and the fault slot was discarded. |
| Evidence hygiene | 0 | Private signing key persisted=false, external publication=false and temporary candidate directory removed=true. Redacted report is 43,536 bytes, SHA-256 `3fd04faf...02cf3c`; tracked 3,716-byte summary SHA-256 is `3635925c...f800453`. |
| Focused regression | 0 | 71 Candidate/ReleaseBuilder/Updater/activation tests pass with one explicit environment skip; Ruff/compile, JSON, diff and 675-file source gates pass. A parallel pytest/compileall `.pyc` race produced WinError 5, then the same gates passed serially without relaxation. |
| Protected provenance boundary | blocked | Local evidence has 8/24 receipts. Sixteen macOS arm64/x64 receipts, protected clean-runner identity, real installed v0.3 corpus and live endpoints remain absent; promotion/publication/rollout were not claimed. |

## Deferred protected infrastructure; continued local live preflight - 2026-07-16

| Scope | Exit | Result |
| --- | ---: | --- |
| User decision | 0 | Six protected Environments and four isolated Runner roles are deferred for this iteration. The official provenance requirement remains visible and unsatisfied; no local evidence is renamed as protected evidence. |
| Exact source | 0 | Main is `84aeed15a81463ff9bfcdd7dceeda992ee692708`; hosted run `29445710112` passed all five required Jobs. |
| Browser preflight | 0 | Fresh Chromium run passed 45/45 in 167.3s. Both themes, 1440x900/1024x768/768x900/390x844/320x568, axe, forced colours, reduced motion, model/permission/reasoning/queue/Skills/view-more/share/image-preview/Retouch paths passed. |
| Runtime routing and concurrency | 0 | 226/226 focused tests passed in 172.4s with one unchanged upstream Starlette warning. Coverage includes GPT-5.6 SOL medium + 272k projection, progressive tool disclosure, ranked non-exclusive image routing, 128 concurrent image replay dedupe, lease/restart recovery and structured Retouch concurrency/crash fencing. |
| Installed v1 discovery | 0 | The machine has no existing v1 signed slot. The only active EcoreX process is the legacy `runtime-0.2.9.2-b909303a` WebUI Runtime; it is not accepted as v1 evidence and was not modified. |
| Native Chrome CDP | 0 | Repository driver commit `622921fbcc2be16d73209bfad2b7ff0cea19afc7` passed Chrome 150 via explicit `connectOverCDP`: fixed 18/18 scenarios, four viewports, 131 assertions, 24.261s and zero console/page/local-request/external-request failures. All 18 screenshots are represented only by SHA-256. |
| Driver failure/cleanup | 1 then 0 | First run exposed lost timeout identity; later runs exposed premature hover sampling and navigation-cancelled SSE misclassification. Per-scenario bounds and exact lifecycle handling fixed the harness. Final Chrome process and owned temporary-profile counts are zero. |
| Redacted evidence | 0 | `evidence/local-live-preflight-622921fb-2026-07-16.json` is 2,981 bytes, SHA-256 `7d67be5f...8020d1`, and explicitly denies Candidate binding/protected provenance. |
| Live boundary | pending | The same matrix against an installed signed v1 Runtime, real managed Model/Image Gateway connectivity, four provider-backed image completions and provider-backed Retouch precision scoring remain to be run. |
| Publication | blocked | No protected Candidate receipt, release publication, grey rollout or user update was created. |

## Real v0.2.9.2 migration and deleted-session exclusion - 2026-07-16

| Scope | Exit | Result |
| --- | ---: | --- |
| Exact source | 0 | Commit `0916bd04465a23504e989bbccf7960273827eadf` generalizes the versioned legacy authority to v0.2.9.2 and v0.3.0 while retaining the v0.3 compatibility entry point. |
| Real read-only corpus | 0 | Copy-on-write dry-run verified 897 entries / 459,541,787 bytes and source inventory SHA-256 `7bd10f20...15b6cf`; source inventory was unchanged before and after. |
| User data preservation | 0 | 54 live sessions, 1,029 messages, 54 summaries, two projects, three live project bindings, 580 Turns, 247 runs and 38,073 run events are present in the migration plan. |
| Deleted-session authority | 0 | The canonical legacy sessions database is authoritative. Ninety-three UI-cache-only session IDs were excluded; zero previously deleted sessions are restored. Cache titles, summaries and pins may enrich surviving sessions only. |
| Historical request IDs | 0 | Forty-two request IDs were reused across 169 Turn occurrences. All conversation Turns remain; ambiguity is explicit and no legacy run row is falsely bound to multiple Turns. |
| Secret boundary | 0 | Five secret-bearing entries are quarantined; the dry-run persisted no content, raw paths or secrets and published no target. |
| Regression | 0 | Focused v0.2.9.2 tests pass 2/2; full migration/activation/quarantine/storage set passes 68 with two explicit environment skips. Ruff, compile, JSON, diff and 676-file source gates pass. |
| Redacted evidence | 0 | `evidence/v0292-real-user-data-dry-run-0916bd04-2026-07-16.json` is 1,829 bytes with SHA-256 `ca606a5c...c066b8`; it stores aggregate counts and digests only, is not Candidate-bound and does not claim installed-v1 activation. |
| Release boundary | pending | Run the same authority during signed-v1 side-by-side installation, then verify activation health and post-activation counts before rollout. |

## Real v0.2.9.2 commit-mode import and replay - 2026-07-16

| Scope | Exit | Result |
| --- | ---: | --- |
| Exact main | 0 | PR #5 passed all five hosted Jobs and merged as `e1d874c51e6bd6f7d05844ed4c12ad40b9b57962`; exact-main push run `29461021847` independently passed Ubuntu quality, Windows x64, macOS arm64/x64 and Cross-runner byte stability. |
| Commit-mode import | 0 | The real installed v0.2.9.2 corpus was copy-on-write imported into a disposable v1 target using an ephemeral quarantine key; this was not a dry-run and did not replace the installed Runtime. |
| Target integrity | 0 | SQLite `integrity_check=ok`; target contains 54 Threads/session mappings, 1,029 messages, 54 summaries, 580 Turns, two Projects, three live bindings, 247 runs and 38,073 run events. |
| Deleted sessions | 0 | Report count `deleted_session_cache_excluded=93`; target contains only the 54 database-authoritative sessions and restores zero deleted sessions. |
| Idempotent replay | 0 | A second execution against the same target returns completed with `idempotent_replay=true` and retains one migration-run row. |
| Verifier correction | 1 then 0 | Initial post-import read-only check referenced nonexistent `items.item_type`; automatic temporary cleanup succeeded. The clean rerun used schema-authoritative `items.kind` and passed without changing product behavior. |
| Cleanup and privacy | 0 | Disposable target and ephemeral key were removed; source inventory digest remained `7bd10f20...15b6cf`. The 1,622-byte evidence SHA-256 is `60a7e8d8...ce892f` and contains no conversation content, raw IDs, user paths or secrets. |
| Activation boundary | pending | The import is proven, but signed-v1 slot activation, health check and post-activation count verification are still required before rollout. |

## Exact-main signed Runtime repeatability and bind hardening - 2026-07-16

| Scope | Exit | Result |
| --- | ---: | --- |
| Exact main | 0 | `3dee8fdc882984aaa00b2571859556f178f88aab`; hosted push run `29461827830` passed all five Ubuntu/Windows/macOS/byte-stability Jobs. |
| Clean Web build | 0 | Locked install found zero vulnerabilities; generated contracts, TypeScript and production build passed. 25 immutable assets / 24 chunks; initial JS 459.76 KiB raw / 146.20 KiB gzip. |
| First signed ceremony | 1 | Failed cleanly after 1,773.9s with Bootstrap launch 1, exit 70, old generic stage `software`; no success report, publication or installed-user mutation. |
| Repeat signed ceremony | 0 | Fresh exact-source run passed in 2,471.86s. First install, migration restart, update-and-refresh and rollback returned HTTP 200; three checkpoints, bad-digest rejection, fault-slot discard, non-persisted keys and cleanup passed. |
| Local artifacts | 0 | Core 21,528,488 bytes; Bootstrap 3,109,080; browser 190,153,571; OCR 94,993,855; Office 14,836,807. All tracked identities have SHA-256 in the evidence summary. |
| Root-cause status | inferred | The pre-hardening failure omitted ceremony phase. Uvicorn post-composition `SystemExit` plus an early-released bind(0) port and identical-source success support a bind TOCTOU inference, but it is not relabelled as directly proven. |
| Hardening | 0 | Drill reserves a non-ephemeral port through signed slot/Pack verification, releases immediately before spawn and includes ceremony phase; Runtime reports `http_server_bind` instead of generic `software` for post-composition Uvicorn SystemExit. This narrows rather than claims to eliminate every listener race. |
| Regression | 0 | Focused: 50 passed / one platform skip. Combined Bootstrap/update/Runtime/Candidate: 94 passed / two skips. Ruff, Python compilation and diff check pass. |
| Evidence | 0 | The 4,245-byte `evidence/windows-signed-candidate-main-3dee8fdc-2026-07-16-summary.json` SHA-256 is `66ef2a5d...fc6941`; it records both attempts and the 43,535-byte full report SHA-256 `1987dd38...85a31e`, with no secrets or user content. |
| Release boundary | blocked | Local evidence has 8/24 platform receipts and no managed Gateway/device session. Protected macOS, installed-live CDP/provider acceptance, publication and rollout remain absent. |

## Administrator Image 2 direct-provider contract - 2026-07-16

| Scope | Exit | Result |
| --- | ---: | --- |
| Root cause | 0 | Administrator image revisions were durable, but the default adapter incorrectly called the EcoreX `/v1/image/jobs` wire contract and never materialized structured Retouch CAS inputs for an OpenAI-compatible provider. |
| Direct generation | 0 | Frozen active revision calls only the allowlisted `/v1/images/generations` origin with exact model, inline PNG output and no legacy `response_format`. |
| Direct Retouch | 0 | Base/reference/mask digests are read and revalidated from shared CAS; the mask is resized to the first image, converted to RGBA and inverted from EcoreX selection pixels to transparent edit pixels. A non-PNG masked base is safely transcoded to matching PNG before multipart `/v1/images/edits` receives the bytes and structured instructions. |
| Large-image ROI integrity | 0 | A 3840×2160 edit surface accepts its deterministic 2048×1152 bounded mask after the cloud adapter re-compiles it from typed annotations and verifies bytes/digest/dimensions/coverage/regions. The resulting cloud Job freezes 3840×2160 rather than defaulting to 1024×1024; metadata drift, substituted PNG bytes or a changed provider output size fail closed. |
| Orientation and direct concurrency | 0 | A 1200×800 JPEG carrying EXIF orientation 6 is normalized to the user-visible 800×1200 surface before the same-size mask is applied. A simultaneous 32-submit adapter test completes all calls while measured upstream concurrency remains exactly at the configured hard bound of four. |
| Billing safety | 0 | Timeout/transport/408/425/5xx becomes uncertain; recovery performs zero upstream calls and never blind-resubmits. Explicit 429 is bounded to 1–3600 seconds. URL-only output is rejected without network fetch. |
| Crash envelope | 0 | Aggregate Retouch inputs cannot exceed one configured image envelope. Administrator direct mode validates a six-times-per-worker memory budget before startup and retains the existing durable queue, lease, fencing and circuit controls. |
| GPT Image 2 size authority | 0 | The direct adapter rejects before upstream unless both edges are 16-aligned, maximum edge is 3840px, aspect ratio is at most 3:1 and total pixels are 655,360–8,294,400. It separately requires decoded RGBA pixels to fit the configured byte envelope, avoiding known-invalid or unretainable billable requests. |
| Focused regression | 0 | 122 passed / two explicit skips; EXIF coordinates, direct-adapter concurrency, bounded high-resolution ROI integrity, official flexible-size boundaries, decoded-memory preflight, exact output dimensions, malformed/deep JSON, secret-safe transport failures, JPEG base conversion, mask alpha semantics and HTTP-date rate limits join the direct provider, dynamic revision, production runtime/storage, orchestrator and managed integration coverage. |
| Deleted-session non-resurrection | 0 | 22 adjacent v0.2.9.2 migration/product tests pass. The canonical session database remains deletion authority; cache-only IDs cannot recreate Threads. Real commit-mode evidence remains 54 retained sessions, 93 excluded stale IDs and zero restored deleted sessions. |
| Complete Python v1 suite | 1 then 0 | A duplicate delayed background launcher caused resource contention; the first run found one stale dev-dependency expectation and one 50 ms maintenance-thread scheduling miss. After synchronizing the pinned Pillow contract, the timing case passed 8/8 isolated repetitions. A clean single-process rerun passed 1,970 tests / 17 explicit environment-platform skips / 0 failed in 1,748.33 seconds. |
| WebUI | 0 | Locked dependency audit reports zero vulnerabilities; generated contracts and TypeScript pass; all 180 Web contract tests pass. Production build emits 25 content-addressed assets / 24 chunks with initial JavaScript 459.76 KiB raw / 146.20 KiB gzip under the fixed 475/150 KiB limits. |
| Static and byte gates | 0 | Ruff/compile, all-zero design debt, strict legacy cutoff, public download, dependency locks, Runtime/Server schema authority, reproducibility, diff and 678-source-file admission pass. Locked profiles contain Bootstrap 3, Cloud 34, Dev 35, Platform-stage 48, Runtime 23 and npm 282 packages. |
| Hosted exact implementation source | 0 | Draft PR #8 commit `9b893ce9079b2cb1b90b951a448b27bbea2620f2`; Actions run `29474142345` passed Ubuntu quality/deterministic build, Windows x64, macOS arm64/x64 and cross-runner byte stability. All five Jobs completed successfully. |
| Evidence boundary | pending | A real managed Image 2 endpoint still must prove model activation, four unique concurrent outputs, no duplicate billing and Retouch unchanged-region scoring on a signed installed Candidate. No publication or rollout occurred. |

## Exact-main signed Candidate and live-boundary audit - 2026-07-16

| Scope | Exit | Result |
| --- | ---: | --- |
| Exact main | 0 | PR #8 merged as `90539b2fce55f2bbd20c552d68b07135b75e7742`; main push run `29474876004` passed all five Ubuntu/Windows/macOS/byte-stability Jobs. |
| Local signed Candidate | 0 | Passed in 2,203.25s. First install, migration restart, update-and-refresh and rollback returned HTTP 200; three checkpoints, bad-digest rejection, fault-slot discard, non-persisted private key and cleanup passed. |
| Candidate artifacts | 0 | Core 21,535,788 bytes; Bootstrap 3,109,078; browser 190,153,571; image 1,376; OCR 94,993,855; Office 14,836,807. Every recorded artifact has an exact SHA-256 in the tracked receipt. |
| Direct GPT-5.6 diagnostic | 0 with boundary | Existing legacy administrator credential reached the configured HTTP provider; catalog returned 200 and contained `gpt-5.6-sol`, while Responses returned 200/terminal with medium reasoning and 272,000 compaction. No managed Gateway/session was used, so this is not official live acceptance. |
| HTTPS image boundary | blocked safely | The legacy provider origin is HTTP and the same host HTTPS probe failed to connect. The v1 direct image adapter remained fail-closed; Image 2 concurrency/Retouch was not rerun by bypassing its HTTPS origin contract. |
| Chrome control | blocked safely | Chrome and the ChatGPT Chrome Extension are installed; the extension is enabled, but Chrome is not running and the native messaging registration is missing. No registry repair, alternate automation or Candidate-bound CDP claim was made. |
| Deleted-session authority | 0 | Real v0.2.9.2 commit import still proves 54 retained authoritative sessions, 93 excluded cache-only deleted IDs and zero restored deleted sessions. This local Candidate used the released v0.3 fixture, so real-v0.2.9.2 signed activation remains pending. |
| Redacted evidence | 0 | `evidence/windows-signed-candidate-main-90539b2f-2026-07-16-summary.json` contains aggregate statuses/digests only. The untracked 43,534-byte full report SHA-256 is `9495383f...d5b3d2`; no credentials, origins, prompts, responses, paths or conversation content are retained. |
| Promotion/publication | blocked | Local evidence remains 8/24 receipts. Sixteen protected macOS receipts, managed HTTPS Gateway/device session and Candidate-bound Chrome control are absent; no publication, rollout or user update occurred. |

## Parallel blocker integration before next signed Candidate - 2026-07-16

| Scope | Exit | Result |
| --- | ---: | --- |
| Administrator activation authority | 0 | Model directory visibility is diagnostic only. Explicit activation performs Catalog plus exactly one frozen-revision Responses/Chat/Images Generation/Images Edit operation; only a contract-valid real result can atomically activate. |
| Uncertain billing boundary | 0 | Submitted POST timeout/transport/408/425/5xx is `provider_test_uncertain`, never auto-retried and never activates. A deterministic per-revision idempotency key supports provider reconciliation without implying client retry. Readiness makes zero model calls. |
| Production configuration | 0 | Fixed public HTTPS origins remain deployment-owned. Activation has a separate bounded `ECOREX_CP_MODEL_ACTIVATION_TIMEOUT_SECONDS` range of 30–600 seconds, default 180; provider result bodies are bounded and validated in memory only. |
| v0.2.9.2 signed-upgrade gate | 0 | Local Candidate defaults to exact tag `v0.2.9.2` / commit `b52999b07a753e103a993a4da9d3c83c3f366e71`. A user-selected source is copied into a disposable inventory-stable snapshot; migration never targets or deletes the source. |
| Deleted-session non-resurrection | 0 | The Candidate verifier reuses the production database candidate order and released conversation reader. Cache-only deleted IDs must have zero intersection with imported session mappings and the migration report count must match exactly. |
| Installed-signed CDP boundary | 0 with scope | Callback runs only after authoritative rollback to a signed current known-good sandboxed slot and rechecks all authorities after Chrome exits. It uses a bounded Job, isolated profile, Chrome-owned ephemeral debug port and bounded evidence. Its explicit scope is `unauthenticated-shell-smoke`; full office scenario acceptance and promotion are false. |
| Python complete suite | 0 | JUnit: 2,013 cases, 0 failures, 0 errors, 17 skipped; console summary 1,996 passed in 1,095.51 seconds. Changed-boundary regression: 106 passed. Compile/lint and all v1 static product gates pass. |
| WebUI | 0 | Generated contracts, TypeScript and 180/180 tests pass. Build emits 24 chunks; initial JavaScript is 459.76 KiB raw / 146.20 KiB gzip. Production dependency audit reports zero vulnerabilities. |
| Remaining acceptance | pending | Execute the exact committed source signed Candidate, then use a real managed HTTPS provider/session and restored browser plugin control for the authenticated image/tool/steer/Retouch matrix. Protected macOS receipts and publication remain absent. |

## Exact-source v0.2.9.2 signed activation ceremony - 2026-07-16

| Scope | Exit | Result |
| --- | ---: | --- |
| Exact source | 0 | Clean detached worktree at `d60d9cda8c2ef9d183b2f5b0e331e9cf8de36b7b`; locked Web install/build passed with zero vulnerabilities and no source dirtiness. |
| Local signed Candidate | 0 | Passed in 2,170.156s with 8 local Windows receipts. First install, post-migration restart, update-and-refresh and rollback each returned HTTP 200. |
| v0.2.9.2 migration | 0 | Exact tag schema commit `b52999b07a753e103a993a4da9d3c83c3f366e71`; copy-on-write fixture imported 2 Threads, 2 messages, 2 summaries, 1 Project and 1 binding. Integrity `ok`; one cache-only deleted ID excluded and zero restored. |
| Update safety | 0 | Three durable checkpoints; explicit activation required; bad digest rejected without pointer mutation; fault slot discarded; previous signed known-good slot restored. |
| Secret and cleanup boundary | 0 | Private key persisted=false; disposable legacy snapshot and complete Candidate directory removed; no external publication. |
| Evidence | 0 | Full report 44,008 bytes / SHA-256 `22c1078b...e5d37c` remains untracked. Aggregate tracked receipt: `evidence/windows-signed-candidate-d60d9cda-2026-07-16-summary.json`. |
| Real-user boundary | pending | Candidate used a deterministic exact-release fixture. Prior real v0.2.9.2 import remains 54 authoritative sessions retained, 93 cache-only deleted IDs excluded and zero restored, but is not relabelled as signed activation. |
| Live/protected boundary | blocked safely | Candidate-bound authenticated CDP, managed HTTPS Model/Image execution, 16 protected macOS receipts, publication and rollout remain absent. |
| Hosted validation | 0 | PR head `f7c14d1499a296ea52ef3822a4bd9846b92e8827`, run `29485934540`: Ubuntu quality/deterministic build, Windows x64, macOS arm64/x64 and cross-runner byte stability all passed (5/5 Jobs). |

## Direct-production publication unblock - 2026-07-16

| Scope | Exit | Result |
| --- | ---: | --- |
| Operator gate | WAIVED | User authorized direct production publication. Manual/CDP/HSM approval is not reported as passed; immutable signatures, health/readback and three-source byte verification remain mandatory. |
| First platform stage | 1 expected | Run `29506205694` failed safely and emitted no complete Candidate input: Windows registry mutation, unavailable macOS `onnxruntime 1.26.0`, and macOS Fetch header projection differences. |
| Stage corrections | 0 | Windows digest-pinned uv/Python 3.11.9 leaves registry unchanged and imports NumPy/ONNX/RapidOCR; `onnxruntime 1.23.2` macOS arm64/x64 wheel hashes are locked; Web tests pass 180/180. |
| Cloud target smoke | 0 with boundary | LUKS2, Python 3.11.9, PostgreSQL 15 TLS and four independent signer roles work. Old services/routes remain active; no new current slot or active release state exists. |
| Wheel resources | 0 | Package-data includes the Admin static allowlist; a built wheel contains and loads all four digest-verified resources without the smoke-only copy workaround. |
| Nginx two-level route | 0 | Real legacy and Candidate assemblies pass target-host `nginx -t`; legacy Admin remains active before health, switch moves two symlinks, and failure restores both. Unknown/external/tampered route targets fail closed. |
| PostgreSQL authority | 0 | Target uses `postgresql.service` and `/usr/bin/psql`; deployer and units match. The target drop-in has `RequiresMountsFor` in `[Unit]` and PostgreSQL remains active. |
| Public repository/source order | 0 | Candidate binds public `EcoreX-installers`; order is GitHub draft → CDN finalize → GitHub public → ghproxy full GET/SHA-256. Real ghproxy probe: 200, zero redirects, identity encoding, 105/105 bytes, matching digest. |
| Windows stable-file identity | 0 | 100 consecutive executable fixture reads and three formerly failing Candidate tests pass after separating descriptor-stable identity from path creation/mode identity. |
| Focused backend | 0 | Cloud/package/deployer: 27 passed with 3 Windows symlink skips; publication/repository/Candidate: 61 passed. Ruff, dependency-lock and diff gates pass. |
| WebUI | 0 | Generated contracts, TypeScript, 180/180 tests and content-addressed build pass; 24 chunks, 459.76 KiB raw / 146.20 KiB gzip initial JavaScript. |
| Availability waivers | WAIVED | Single-host local CAS is not HA; LUKS needs operator loop/unlock after whole-machine reboot; PostgreSQL migration/runtime currently share one role. |
| Publication | pending | Corrected main, same-run Windows/macOS stages, exact-main cloud artifact, model/identity migration, signed release, CDN/GitHub/ghproxy readback, Bootstrap activation and public traffic validation remain required. |

## Direct-production transaction closure - 2026-07-16

| Scope | Exit | Result |
| --- | ---: | --- |
| Hosted baseline | 0 | PR #12 head `6364f07b59c960f390516b82b4db5b1e79984d8b`, run `29511476979`, passed Ubuntu quality/deterministic build, Windows x64, macOS arm64/x64 and cross-runner byte stability (5/5 Jobs). The transaction-closure WIP is newer and must rerun this matrix after commit. |
| Direct admission | 0 | Domain-separated prepare/finalize signatures bind exact manifest, Candidate, operator waiver, three-source publication and Bootstrap proof. Only the three live-acceptance gates project `waived`; all other required gates remain `passed`-only. Append-only persistence, replay idempotency and drift rejection pass. The 32 MiB Nginx allowance is exact PUT-only; access-phase no-body auth verifies `release_admin` before buffering, ASGI reauthenticates and uses one memory slot, and 401/403/429 rejections do not read the body. |
| Cloud activation transaction | 0 | `migrating` precedes writer stops; `schema_ready` follows migration; no dual writer; source-schema checks and target roll-forward are deterministic. First-release migration failure restores the immutable legacy source. Focused result: 51 passed / 4 Windows platform skips. |
| Real environment migration dry-run | 0 | 40 active users retained / seven deleted excluded; eight eligible sessions retained / 248 revoked and 114 expired excluded; 2,061 usage aggregates retained from 2,088 rows with 27 excluded. Source remained unchanged and no target was generated. Unsafe historical public-HTTP provider credentials are disabled with `rotation_required`. |
| CDN replica and mirror | 0 | Current/next bearer tokens, exact digest/length/kind, content-addressed no-clobber writes, fsync/crash recovery and bounded retryable-only read-through are covered. Redirect, encoding, size and digest drift fail closed. |
| Provider bridge | 0 | Loopback-only TLS bridge, private-CA namespace, root-owned material, certificate/key/SAN/EKU validation, hosts ownership and real probe pass. Public/hostname HTTP origins are rejected; private-IP HTTP requires an explicit waiver. |
| Exact cloud artifact | 0 | Linux aarch64 wheel-only builder binds exact commit/Python 3.11.9/locks/modes/files. Windows DPAPI signs the detached domain payload; Linux rescans before attach. Real exact-main build/sign/attach remains a post-merge gate. |
| Public Web/Admin transaction | 0 | Release-key-signed exact-tree authorization, shared flock, root-owned 0755 download/slot tree, root-only staging/legacy state, symlink/hardlink/device fences, durable journal, atomic legacy/current switch and dual-parent fsync pass. Admin identity is derived from the same signed cloud manifest; readback requires exact index/CSS/JS/health bytes, no-store/CSP/immutable policies and the product-version header. Exact target is `/ecorex-agent/admin/`. Focused result: 40 passed / 5 Windows symlink skips. |
| Windows online file identity | 0 | 9 focused tests pass. Birth time provides stable path/handle identity; descriptor ChangeTime and final reopen retain same-size/restored-mtime mutation detection. |
| Combined changed boundary | 0 | 15 release domains: 206 passed / 10 explicit platform-conditioned skips / zero failures in 80.78 seconds. Ruff/compile, dependency locks, Runtime schema authority (20 fragments), server schema authority (11 explicit authorities), strict legacy cutoff and public-site gates pass. One unchanged Starlette multipart deprecation warning only. |
| Public traffic | pending | No source commit/merge, exact-main platform stage, signed cloud/client artifact, publication, rollout or Web/Admin cutover is claimed by this entry. |

## Linux semantic correction before merge - 2026-07-17

| Scope | Exit | Result |
| --- | ---: | --- |
| PR run `29521151721` | 1 expected | Windows x64 and macOS arm64/x64 passed. Ubuntu full Runtime found 14 Linux-only failures and stopped before Web/static/byte stages; cross-runner stability was skipped. No merge or publication occurred. |
| Cloud artifact and Admin route | 0 | Test fixtures now apply signed 0755/0644 modes. Admin Nginx validation checks seven exact location contracts while allowing unrelated CP routes; path/rewrite/header/upstream/duplicate/extra-location drift remains fail-closed. |
| Provider Bridge POSIX boundary | 0 | Production still requires root:root. Portable tests simulate and assert ownership; atomic order is ownership, final mode, short-write loop, file fsync, replace and parent fsync. Reload failure restores without masking the original error. |
| Public-site POSIX boundary | 0 | Portable tests simulate root:994 `lchown`; production owner/mode/link/device checks are unchanged. Legacy rollback, crash recovery and receipt-to-journal-clear recovery execute on Linux. |
| Windows affected domains | 0 | 81 passed / 12 explicit Linux-conditioned skips / zero failures. |
| WSL Ubuntu Python 3.11.9 | 0 | 93 passed / zero failures across cloud sidecar, Provider TLS Bridge and public-site deployment in 19.52 seconds. |
| Static correction gates | 0 | Ruff, Python compilation and `git diff --check` pass. Full hosted rerun remains pending. |

## Connector late-success ownership correction - 2026-07-17

| Scope | Exit | Result |
| --- | ---: | --- |
| PR run `29522376431` | 1 expected | Earlier 14 Linux failures were gone. Ubuntu completed with one connector late-success failure (2,178 passed / 35 skipped); Windows and both macOS jobs passed. Cross-runner stability was skipped and no artifact was promoted. |
| Root cause | 0 | Fixed sleep was nondeterministic and revealed that `outcome_unknown` ignored an active exclusive provider-completion lease, sending a safe retry directly to manual reconciliation. |
| Reservation/polling contract | 0 | Non-expired active provider fence returns `in_progress` and waits without redispatch. Expired or inactive fence remains `uncertain`; no unsafe retry was introduced. |
| Durable recovery delivery | 0 | `completion_path=late_provider_result` is authoritative even if the waiter finalizes the stage first. Recovery Tool Item/event, result and provider dispatch are exact-once; leases end at zero. |
| Deterministic regression | 0 | Explicit waiter-entry barrier replaces sleep/call_later. Result suite passes 19 on Windows and 19 on WSL Ubuntu/Python 3.11.9; all Connector regression passes 132. Ruff/compile/diff pass. |
| Promotion | pending | New commit and hosted full-suite/cross-runner rerun are mandatory. |

## Product update lock ownership correction - 2026-07-17

| Scope | Exit | Result |
| --- | ---: | --- |
| PR run `29524461343` | 1 expected | Connector correction passed; Windows x64 and macOS arm64/x64 passed. Ubuntu reached 2,180 passed / 35 skipped and exposed one product update-lock thread race; cross-runner stability was skipped. No merge, artifact promotion or public cutover occurred. |
| Root cause | 0 | Update polling read `current_release_identity` while Runtime startup recorded `mark_runtime_ready` on another worker thread. The same lock instance incorrectly rejected normal cross-thread ownership even when product serialization was required. |
| Lock contract | 0 | Same-thread re-entry remains depth-counted. Other threads use one condition/deadline across instance and OS acquisition. `timeout=0` stays fail-fast; production update composition explicitly uses `timeout=None`. Backend unlock and stream close complete before ownership handoff. |
| Failure cleanup | 0 | Injected backend-acquire, backend-release and stream-close failures always clear reservation/owner/stream state and notify waiters. Non-owner release is rejected without changing the live owner. |
| Deterministic race | 0 | Observable barriers fix the update identity reader inside the critical section and prove the readiness recorder waits before handoff; no sleeps or scheduler guesses are used. Implementing-agent evidence: 20/20 race repetitions and 250/250 thread stress repetitions. |
| WSL Ubuntu Python 3.11.9 | 0 | `test_update_composition.py` plus `test_update_durability.py`: 23 passed. Focused lock/product-barrier set: 18 passed. Ruff, compileall and `git diff --check` pass. |
| Independent review | 0 | No remaining P0/P1 in locking, production wiring or regression coverage. The original Linux full-suite path must still pass on a new PR head. |
| Promotion | pending | Commit/push and a new five-job hosted matrix are mandatory before protected-main merge or any production mutation. |

## Protected Stage and pre-signing contract closure - 2026-07-17

| Scope | Exit | Result |
| --- | ---: | --- |
| PR run `29526376684` | 0 | Head `a1cc16c8` passed Ubuntu quality/deterministic build, Windows x64, macOS arm64/x64 and cross-runner byte stability (5/5). PR #12 was squash-merged as exact main `de70b480f20acc1b5f19b740e67f6282f33037f8`. |
| Platform Stage `29526938093` | 1 expected | Both macOS jobs stopped at Web tests because a delayed `IncomingMessage.headers` closure lost CSP/Set-Cookie; Windows stopped before dependency install because the uv-managed base is PEP 668 protected. No Core/Bootstrap/Pack artifact was accepted or reused. |
| macOS GA helper correction | 0 | Raw on-wire headers are copied in the response callback. Exact Node 22.23.1: Web 180/180; GA file 3/3 repeated runs, eight tests each. |
| Windows Stage isolation | 0 | Exact uv-managed Python 3.11.9 creates a disposable `RUNNER_TEMP` venv; no registry mutation and no `--break-system-packages`. Fresh local venv installed all 53 locked packages, imported Packaging/Playwright/NumPy/ONNX Runtime and passed dependency-lock validation. |
| CDN canonical contract | 0 | Recipe, Candidate and replica agree on `https://dl.ecoremedia.net/ecorex-agent/releases/v1.0.0/<release_id>`. Recipe → signed manifest → real ASGI replica upload/finalize plus adjacent source tests: 4 passed. |
| Dynamic Bootstrap pointer | 0 | Pointer moved outside immutable site slots to a CP-owned atomic object. Exact Nginx/Caddy route is unique; current/legacy aliases are forbidden. Separate release/publication keyrings verify authority/freshness signatures and reject tamper, unknown key, expiry and immutable target/source drift. |
| Transactional legacy import | 0 | Explicit v0.2.9.2 migration authority, fixed cutoff/digests, stopped writers, idempotent Admin/identity commit and receipt-driven roll-forward are inside the activation journal. Failure before target write restores legacy; receipt after commit forbids source restart. Dry-run executes real read-only target preflight. |
| Unified Linux regression | 0 | WSL Ubuntu, Python 3.11.9: 179 passed across cloud sidecar, Admin/identity/device migration, public pointer/site, Control Plane, replica and reproducibility suites. One unchanged Starlette multipart warning. |
| Static gates | 0 | Ruff, compileall, source tree 753, dependency locks, 20 Runtime schema fragments, 11 server authorities, legacy cutoff, public-download gate and `git diff --check` pass. |
| Release boundary | pending | Fix branch still requires a new protected five-job matrix, protected-main merge and a wholly new same-run three-platform Stage. Live gates remain `WAIVED`, never `passed`; production remains unchanged. |

## Final Stage-correction audit - 2026-07-17

| Scope | Exit | Result |
| --- | ---: | --- |
| Live old-site readback | 0 read-only | `/ecorex-agent/` 200; legacy `/admin/` 401; public Bootstrap pointer 404. No route, file or service mutation. |
| First pointer continuity | 0 | Legacy exact route is retired only after stable source/target reads, schema or dual-key verification, and a second exact payload/size/SHA check. Missing old route initializes canonical `unpublished`; drift fails before Nginx mutation. |
| CDN URL mapping | 0 | Public URL is `/releases/v1.0.0/<release_id>/<asset>`; Nginx/Caddy derive the private channel segment from signed `release_id` and reject unmatched paths. |
| Windows Stage isolation | 0 | Venv path includes run ID, run attempt and matrix target; any residue at the exact path fails closed. |
| Combined Python boundary | 0 | Exact Python 3.11.9: 193 passed, 12 platform-conditioned skips; one unchanged Starlette multipart warning. |
| Candidate/CDN/public boundary | 0 | 40 passed, including recipe → signed manifest → real ASGI CDN finalize and canonical route checks. |
| Web boundary | 0 | Exact Node 22.23.1: 180/180 passed. |
| Independent review | 0 | No remaining P0/P1/P2; seed-to-retire TOCTOU, trust-role overlap, double-writer and rollback paths reviewed. |
| Production promotion | pending | Commit, protected five-job PR, protected-main merge, same-run three-platform Stage, signing and cloud/public activation remain outstanding. |

## Exact-main Stage runtime-source correction - 2026-07-17

| Scope | Exit | Result |
| --- | ---: | --- |
| Protected PR matrix | 0 | PR #13 run `29531945083`, attempt 3, passed quality/deterministic build, Windows x64, macOS arm64/x64 and cross-runner byte stability. Squash merge produced exact main `c042e4a3997e8289bd24b33ae600a2ba5b249a4c`. |
| Production drift preflight | 0 read-only | Old Web/Admin/services/Nginx and the 47-user legacy database remain healthy and unchanged; v1 pointer, keyrings, current slot and units are not active. |
| Exact-main Stage `29541415646` | 1 expected | Windows emitted typed `pack_python_probe_failed` after a venv launcher reported `No pyvenv.cfg file`; both macOS jobs failed three GA assertions because the test client observed empty CSP/Cookie headers. No successful Stage artifact was accepted or reused. |
| Windows Python closure | 0 | Interpreter, stdlib and DLLs are selected from one resolved `sys.base_prefix`; venv launchers, prefix escape, symlink/reparse and unstable identity fail closed. Platform-staging suite: 52 passed / 1 explicit skip. |
| macOS GA header projection | 0 | Response callback snapshots Node 22 `headersDistinct` with a raw-wire fallback; repeated Set-Cookie values remain distinct and accessors cannot mutate the snapshot. |
| Exact Web boundary | 0 | Node 22.23.1: 181/181 passed, including the new teardown-stable header test and the three assertions that failed on both macOS runners. |
| Static correction gates | 0 | Ruff, Python compilation and tracked whitespace checks pass locally. |
| Publication | pending | Fix commit, protected PR matrix, exact-main merge and a fresh same-run Windows/macOS Stage remain mandatory before signing or activation. |

## Platform Stage cross-shell fail-fast correction - 2026-07-17

| Scope | Exit | Result |
| --- | ---: | --- |
| Windows Stage forensics | 1 expected | The Windows log contained the same three GA Web-test failures as macOS, but multi-line PowerShell continued to a successful Web build and hid the native test exit code; the later Python closure probe became the reported terminal failure. No failed result is treated as passed. |
| Command boundary | 0 | Dependency/Chromium install and final Web build use one fixed, repository-owned Python command-group runner. The first launch or child failure is the step result on Windows and macOS; commands after it are never invoked. |
| Regression contract | 0 | A real child exits 23 before a marker-writing success child; the runner returns 23 and the marker remains absent. Static workflow coverage forbids restoring the vulnerable inline command blocks or `continue-on-error`. |
| Dependency-lock closure | 0 | The gate requires all three exact workflow runner bindings once, forbids adjacent inline dependency/build commands, AST-compares the nine-child-command literal catalog and pins the complete runner AST. Missing, duplicate, changed-argument and bypass mutations are rejected. |
| Promotion | pending | A new protected matrix and wholly new same-run three-platform Stage remain mandatory before signing or production activation. |

## Hermetic Web dist build-before-test correction - 2026-07-17

| Scope | Exit | Result |
| --- | ---: | --- |
| Platform Stage `29544524231` | 1 expected | Windows x64 and macOS arm64/x64 all stopped at GA `dist_missing`: the controlled Web catalog tested before its only build on a clean workspace. Fail-fast operated correctly and no Stage output was accepted. |
| Hermetic order | 0 | After fixed Python setup, cross-platform `clean-check` runs before dependency installation. The six-command Web catalog is `npm ci` → typecheck → one build → test → content-address validation → read-only bundle validation. GA and both validators consume the same digest-stable dist; no cached dist or repeated build is allowed. |
| Clean-workspace regression | 0 | Tests reject dirty Git porcelain, pre-existing/early-created dist, truncated or reordered command shapes, test mutation and validator mutation. The resolved executable, argv, cwd and order of all six Web commands must exactly match the dependency-lock catalog and complete runner AST pin. |
| CI/Candidate Web ownership | 0 | Each quality job performs exactly one workflow-owned build before Web, Playwright and Python suites. Canonical before/after byte contracts must compare exactly; the Python release contract is a read-only consumer and its exceptional path is guarded. |
| Promotion | pending | A fresh same-run three-platform Stage remains mandatory before signing or production activation. |

## Platform runtime closure and AppContainer correction - 2026-07-17

| Scope | Exit | Result |
| --- | ---: | --- |
| Protected baseline | 0 | PR #15 passed all five protected jobs and was squash-merged as exact main `996b4e1d46c1b5b00c8ce0913894f6e6df95a78e`. |
| Exact-main Stage `29547660871` | 1 expected | Both macOS jobs stopped with typed `stage_source_file_invalid`; Windows stopped with typed `sandbox_boundary_probe_failed` and helper stderr `ecorex_sandbox_probe:workspace_security`. The whole run is quarantined; no artifact or partial receipt may be reused. |
| macOS root cause | 0 | Official CPython Framework exposes `libpython*.dylib` aliases as symlinks and copied Mach-O files retained non-relocatable Framework/toolcache load paths. Generic source-file reads remain link-strict; only a base-prefix-confined regular libpython target is resolved. |
| macOS relocatable closure | 0 local contract | Thin/FAT Mach-O files are inspected per architecture. LC_ID, dependencies and RPATHs are normalized to the copied closure, unknown or escaping paths fail closed, modified files are ad-hoc signed without timestamps and strictly reverified. A Seatbelt negative canary must prove the source prefix is unreadable before the complete import probe runs. Platform staging: 66 passed / 7 platform skips. Real `otool`, `codesign` and `sandbox-exec` evidence remains Stage-owned. |
| Windows root cause | 0 | The helper attempted to set Low MIC on user-owned directories. A normal Medium-IL runner has no relabel authority, so the same design would fail first-install and repair, not only Stage. |
| Windows AppContainer v3 | 0 | The product never writes or deletes user SACLs. It converges only the exact Package SID DACL, read-only attests absent/Untrusted/Low/Medium MIC, rejects High/System or ambiguous labels, and verifies the suspended child AppContainer SID, Low token and empty capabilities before Job assignment and resume. Cleanup only revokes the exact Package SID ACE. |
| Real Windows native probe | 0 local manual | Strict MSVC/SDK build passed with source-set SHA-256 `eafd5f6a...bed051` and helper SHA-256 `83802d75...4cfe88`. A non-elevated real AppContainer probe returned ready with workspace-only write, outside read/write denied, loopback denied and process-tree containment. Parent workspace SDDL was byte-identical before and after. Polluted Package-SID Full Control failed strict attest; repair restored exact authority and attest passed. An independent local rerun also returned `ready=True`; this is not substituted for protected Stage evidence. |
| Focused regression | 0 | Platform staging plus Windows native source contract: 69 passed / 7 platform skips. Tracked whitespace check passed; `.artifacts/` remained untouched and excluded. |
| Promotion | pending | Commit, a new protected five-job matrix, protected-main merge and a wholly new same-run Windows/macOS Stage are mandatory. Only that new run may feed signing, publication and production activation; the old production site remains active. |

## CPython macOS native-dependency closure correction - 2026-07-17

| Scope | Exit | Result |
| --- | ---: | --- |
| Exact-main Stage `29551721211` | 1 expected | macOS x64 emitted typed `pack_python_macho_dependency_unresolved`; its first unresolved edge was `_curses.cpython-311-darwin.so` to the official Framework `libncursesw.5.dylib`. macOS arm64 independently stopped at the strict clean-check before dependencies; the discarded porcelain prevents naming the dirty path, so the check remains strict and a fresh hosted runner must prove it clean. Windows x64 completed successfully and uploaded artifact `8396700775`, but the entire mixed-result run is quarantined and no output may be promoted or reused. The ephemeral Windows runner unregistered after completion. |
| Native closure root cause | 0 | The official actions/python 3.11.9 Framework ships non-system OpenSSL, NCurses/Panel and Tcl/Tk dylibs beside libpython. Copying only libpython left stdlib extension load commands pointing outside the staged Core. Exact package inspection confirms the six required versioned dylibs are regular files rather than aliases; `readline.cpython-311-darwin.so` uses system libedit plus the already-covered NCurses dylib. |
| Recursive materialization | 0 local contract | Mach-O load commands are walked to a fixpoint. Only exact regular, non-link sources confined to the resolved CPython base prefix are copied at their source-relative paths. The official pkg, eight universal2 source dylibs and final relocated payloads are separately digest-bound. Absolute dependencies never fall back to basename matching; `@rpath` resolution requires one unique closure target. Pre-existing destinations, escape, ambiguity, reparse and unstable source identity fail closed. The target architecture remains independent from FAT slice iteration. |
| License and semantic SBOM boundary | 0 local contract | Exact installer `License.rtf` plus the upstream OpenSSL, NCurses, Tcl and Tk license texts are size/hash pinned, copied for the materialized components and bound through the inventory, Core file records, CycloneDX library components and Candidate supply-chain gate. The gate independently reads archive bytes, enforces the immutable component contract, exact immediate-dylib/inventory equality and release-wide native reference union. Missing, extra, mislabelled or tampered payloads fail before signing. |
| Early cleanup root cause | 0 | The workflow cleanup step is `always()` and can execute before dependencies exist. Its script now loads the stdlib-only leaf module directly after a regular/non-link check, avoiding the eager `ecorex.release` package graph and its Pydantic dependency. A `python -S remove` regression proves the pre-dependency path. |
| Focused regression | 0 | Platform staging, full Candidate pipeline, ReleaseBuilder/SBOM and Runtime-config materialization: 118 passed / 10 explicit platform skips. Ruff, Python compilation, license-resource digest checks and tracked whitespace checks pass. Independent final audit reports zero P0/P1; real `otool`, relocation, codesign and isolated imports remain owned by a fresh dual-macOS Stage. |
| Promotion | pending | A protected five-job PR matrix, protected-main merge and a wholly new same-run Windows/macOS Stage remain mandatory before signing, publication or production activation. |

## macOS isolated pack-probe observability correction - 2026-07-17

| Scope | Exit | Result |
| --- | ---: | --- |
| Exact-main Stage `29555840615` | 1 expected | Both macOS x64 and arm64 completed relocation and stopped at the former aggregate `pack_python_probe_failed`; Windows was still allowed to finish for evidence. The complete run is quarantined and no output may be reused. |
| Evidence boundary | 0 | The old code collapsed interpreter startup, native imports, ASGI imports, packaged resources, tzdata and final stdout into one typed code while discarding bounded child output. Existing tests exercised only mocked command shape, so the observed code cannot safely identify which dependency to change. |
| Fail-closed diagnostics | 0 local contract | Source-prefix and Python Framework denies remain unchanged. The fixed probe maps bootstrap, native-import, ASGI-import, resource and tzdata phases to stable exit codes without exporting stderr or host paths. Both the source-readable baseline and isolated probe deny writes to the completed Core. Profile execution, a positive in-Core read control and the negative source canary run before imports; the complete Core tree binding is reverified afterward. Unknown exit, timeout, bounded-output failure, canary ambiguity, mutation and output mismatch remain separate fail-closed errors. |
| Focused regression | 0 | Platform staging: 78 passed / 9 explicit platform skips. Ruff, Python compilation and tracked whitespace checks pass. |
| Promotion | pending | This change must pass the protected five-job matrix and merge to exact main. A fresh dual-macOS Stage must then supply the phase-specific real-host evidence before any root-cause correction or Candidate publication. |

## Cross-process pack-probe and dirty-checkout observability - 2026-07-17

| Scope | Exit | Result |
| --- | ---: | --- |
| Protected PR/main | 0 | PR #18 run `29556882653` and exact-main run `29557202704` each passed all five protected jobs. The squash merge is exact main `bf2d73282f7b380876ebc65559e870b2354ccefa`. |
| Exact-main Stage `29557698364` | 1 expected | Fresh macOS arm64 failed the strict pre-dependency clean-check; the same-run x64 passed clean-check and later returned `pack_python_probe_execution_failed`. The Windows job was cancelled after the run became ineligible. JIT runner 28 unregistered and the whole run remains quarantined. |
| Exit-code boundary root cause | 0 local contract | `sandbox-exec` may normalize the child status, so fixed Python phase exit codes alone do not survive the process boundary. Each caught phase now also emits one exact non-sensitive marker; the parent maps only the closed marker set. Separate minimal bootstrap probes run before full imports, with the security-bearing source-denied probe always preceding the source-readable diagnostic baseline. |
| Mutation boundary | 0 local contract | The undocumented Core write deny prevented a probe running in canonical Core from reaching a mappable phase. Canonical Core is now never executed: isolated and baseline probes each receive a separately copied, tree-bound snapshot and private TMPDIR/TMP/TEMP. Both profiles deny canonical read/map/write and mutually deny the peer snapshot root; snapshot mutation fails and canonical Core is rebound afterward. Source-prefix and Framework read/map denies remain unchanged. |
| Dirty checkout evidence | 0 local contract | Clean-check remains fail-closed and never cleans or ignores drift. NUL-delimited Git porcelain is reported only as bounded URL-safe base64 records plus byte/entry counts and SHA-256, preventing control-character log injection while preserving the next hosted-run root-cause evidence. The audited runner AST pin was regenerated with exact Python 3.11.9. |
| Focused regression | 0 | Platform pack staging, cross-shell Stage runner and dependency-lock contract: 108 passed / 9 explicit platform skips. Ruff, Python compilation and tracked whitespace checks pass. Independent dirty-check audit found no deterministic repository or setup-python writer. |
| Promotion | pending | A new protected PR/main matrix and a wholly new same-run three-platform Stage are required. No output from `29557698364` may feed Candidate signing or publication. |

## macOS copied-interpreter bootstrap classification - 2026-07-17

| Scope | Exit | Result |
| --- | ---: | --- |
| Protected PR/main baseline | 0 | PR #19 run `29559145479` and exact-main run `29559502846` passed quality/deterministic build, Windows x64, macOS arm64/x64 and cross-runner byte stability. The squash merge is exact main `618bb153d7a0f305dccaaa63b3607faa3e97bb33`. |
| Exact-main Stage `29559940799` | 1 expected | Fresh macOS arm64 passed profile parsing, the positive snapshot read and both source/canonical negative canaries, then failed the first isolated snapshot `python -I -B` bootstrap with typed `pack_python_sandbox_bootstrap_probe_execution_failed`. macOS x64 and Windows were cancelled immediately; JIT runner 29 was one-use and the whole run is quarantined. No output may be promoted or reused. |
| Evidence boundary | 0 local contract | The failure occurs before EcoreX, native wheel, ASGI, resource or tzdata imports. A normalized `sandbox-exec` status without a Python marker cannot distinguish source-prefix dependency, Framework dependency, copied-Mach-O signature failure or a combined Seatbelt interaction. stderr and host paths remain private. |
| Fail-closed classifier | 0 local contract | Only after the combined source-and-Framework denial has failed, copied Mach-O signatures are strictly reverified and independent baseline, source-only-denied and Framework-only-denied bootstrap diagnostics run with separate temporary directories. Canonical Core and the peer snapshot remain denied. Every classification still fails Stage, exports only one stable code and rebinds canonical plus both snapshot trees before returning the failure. |
| Focused regression | 0 | Platform staging plus Stage fail-fast and dependency-lock contracts: 115 passed / 9 explicit platform skips. Ruff, Python compilation and tracked whitespace checks passed; one unchanged Starlette multipart warning remains. The independent second-pass audit reports P0/P1/P2 = 0 after source/Framework write denial and three mutually isolated diagnostic snapshots were added. |
| Promotion | pending | The diagnostic must pass a protected PR/main matrix. A wholly new same-run dual-macOS/Windows Stage must then identify the real bootstrap dependency before a root-cause fix; signing and production remain blocked. |

## Copy-stable macOS Mach-O signing correction - 2026-07-17

| Scope | Exit | Result |
| --- | ---: | --- |
| Protected classifier baseline | 0 | PR #20 run `29561257331` and exact-main run `29562136181` passed all five protected jobs. The squash merge is exact main `a28ef909c158580020a2e955029434ecb0cbdd8a`. |
| Exact-main Stage `29562923294` | 1 expected | Fresh macOS arm64 again reached the first copied isolated interpreter bootstrap. The fail-closed classifier strictly verified every Mach-O in both bound snapshots and returned `pack_python_sandbox_bootstrap_snapshot_signature_invalid`. macOS x64 and Windows were cancelled; JIT runner 30 was one-use and the complete run is quarantined. |
| Root cause | 0 local contract | The source toolcache signature may verify in place while relying on metadata that the deterministic Core archive and normal extraction do not preserve. The old relocation loop created a new ad-hoc signature only for binaries changed by `install_name_tool`; untouched interpreter extensions or libraries could therefore retain a non-copy-stable source signature. |
| Canonical signature ownership | 0 local contract | After all load-command changes, every final Mach-O in canonical Core is unconditionally signed with `codesign --force --sign - --timestamp=none`, then every file is strictly verified before dependency, RPATH, inventory, manifest or snapshot binding. The probe still copies canonical bytes and never repairs a snapshot, so success proves the same signature bytes survive archive-equivalent copying. |
| Focused regression | 0 | Platform staging plus Stage fail-fast, dependency-lock and real codesign contracts: 115 passed / 10 explicit platform skips on Windows. The relocation test includes an untouched Mach-O and proves all relocation commands precede every signing command and every signing command precedes strict verification. A Darwin-only hosted test signs two ordinary copies, requires identical bytes and strictly verifies a third archive-equivalent copy. Ruff, Python compilation and tracked whitespace checks pass. |
| Independent review | 0 | P0/P1 = 0. The only initial P2 was missing real Darwin determinism/copy coverage; the protected platform matrix now runs the dedicated real `codesign` test on both macOS arm64 and x64. |
| Promotion | pending | A protected PR/main matrix and a wholly new same-run three-platform Stage remain mandatory before Candidate signing or production activation. |

## Hosted checkout line-ending normalization - 2026-07-17

| Scope | Exit | Result |
| --- | ---: | --- |
| Protected signing baseline | 0 | PR #21 run `29563677066` and exact-main run `29564066950` passed all five jobs, including real deterministic/copy-stable `codesign` on macOS arm64/x64. The squash merge is exact main `9b3a9cb8ca8fea3674306cddce06ca7776c09512`. |
| Exact-main Stage `29564828460` | 1 expected | Fresh macOS arm64 stopped before dependency installation with strict `stage_checkout_not_clean`. The bounded NUL-porcelain evidence identified exactly `docs/ecorex/goal-ledger.md` and `docs/ecorex/v0.1.12/release-manifest.md`; Windows and macOS x64 were cancelled, JIT runner 31 unregistered and the whole run is quarantined. |
| Root cause | 0 | Those are the repository's only two `i/mixed` Git blobs while `.gitattributes` declares `text eol=lf`. A hosted checkout may correctly normalize their working-tree bytes to LF and immediately appear dirty against the historical mixed-EOL index blob. No setup-python or product writer modified their semantic content. |
| Canonical correction | 0 local contract | `git add --renormalize` converts both index blobs to LF without ignoring, cleaning or resetting Stage drift. `git ls-files --eol` reports `i/lf` for both; staged word diff contains only newline normalization. The strict clean-check and bounded evidence contract remain unchanged. |
| Promotion | pending | A protected PR/main matrix and wholly new same-run three-platform Stage are required. No output from `29564828460` may be reused. |

## Portable macOS Runtime closure correction - 2026-07-17

| Scope | Exit | Result |
| --- | ---: | --- |
| Protected line-ending baseline | 0 | PR #22 run `29565652769` and exact-main run `29566081162` passed all five protected jobs. The squash merge is exact main `40233c745244facd444b66a85c961d0dbe5289f8`. |
| Exact-main Stage `29566583997` | 1 expected | Fresh macOS arm64 passed the strict checkout gate and reached the real Core build, then returned `pack_python_sandbox_bootstrap_snapshot_signature_invalid`. macOS x64 and Windows were cancelled immediately; JIT runner 32 was one-use and the complete run is quarantined. |
| Root cause | 0 local contract | The official CPython Framework `config-X.Y-darwin` directory contains link-time aliases named `libpython*.a` and `libpython*.dylib` plus Mach-O objects. Generic stdlib copying materializes both aliases as duplicate Framework image bytes under build-only names, and the four-byte magic scanner then treats them as Runtime Mach-O. Their in-place signature result is not sufficient for the byte-only snapshot/archive contract. The earlier real test covered only the loadable `python3` executable and could not prove the full closure. |
| Runtime-only closure | 0 local contract | Removal is allowed only for the exact 13-member CPython 3.11.9 product-materialized build-support contract. The real hosted-tree test reuses the production copier, including its explicit `__pycache__`/bytecode exclusion; cancelled discovery runs `29568251589` and `29568523851` exposed and removed assumptions from package-payload and generic `copytree` fixtures before merge. Both materialized libpython aliases must byte-match the canonical copied libpython; unknown members, unexpected Mach-O content, links, reparse points and non-regular entries fail closed. Members are identity-reverified before bottom-up deletion. Any remaining known compiler-object/archive suffix fails as unclassified. Runtime modules, dylibs and native extensions remain unchanged. |
| Portable-signature gate | 0 local contract | After final relocation, full Mach-O signing and strict canonical verification, the entire closure is copied with the same byte-and-mode contract used by snapshots and archives. Canonical and copied tree bindings must match and every copied Mach-O must strictly verify before inventory, manifest or sandbox execution. Detached xattrs are never copied and verification is not weakened. |
| Focused regression | 0 | Platform staging and Darwin CPython/codesign contracts: 94 passed / 11 explicit platform skips on Windows. The protected macOS matrix additionally runs the real official-CPython member contract and copy-stable codesign tests on arm64 and x64. Ruff, Python compilation and tracked diff checks pass. |
| Independent review | 0 | Final review reports P0/P1 = 0. Remaining P2 items concern future general Mach-O filetype classification and additional race fixtures; the exact official build-support contract plus full-closure portable-signature gate remain fail closed. |
| Promotion | pending | This correction requires protected PR/main verification and a wholly new same-run three-platform Stage. No output from `29566583997` may feed Candidate signing, publication or production. |

## macOS copied-interpreter execution classifier - 2026-07-17

| Scope | Exit | Result |
| --- | ---: | --- |
| Protected portable-closure baseline | 0 | PR #23 run `29568874467` and exact-main run `29569327362` passed all five protected jobs, including the real installed CPython materialization contract on macOS arm64/x64. The squash merge is exact main `b6e5b5a7cf683a8ae5f8bc1cded3bd340229ab06`. |
| Exact-main Stage `29569824355` | 1 expected | Fresh macOS arm64 passed the new complete-closure copy-signature gate, proving all canonical bytes and copied Mach-O signatures portable, then returned `pack_python_sandbox_bootstrap_snapshot_execution_failed`. macOS x64 and Windows were cancelled; the one-use Windows runner unregistered and the whole run is quarantined. |
| Evidence boundary | 0 local contract | The former code only proved that a signature-valid snapshot did not start inside an allow-default Seatbelt baseline. It did not distinguish whether the copied interpreter itself could execute without Seatbelt or whether the baseline policy caused the failure. stderr and host paths remain private. |
| Fail-closed classifier | 0 local contract | Seven fresh signature-verified snapshots and private TMPDIRs are created together. The first runs the fixed minimal bootstrap directly; the remaining six preserve canonical/original-snapshot/peer read-map-write denial while incrementally testing the boundary baseline, source write deny, Framework write deny, combined writes, source read/map deny and Framework read/map deny. Direct launch, baseline boundary, write dependency, read dependency and combined-policy failures receive distinct fixed codes. Canonical, original and all diagnostic bindings are checked before any result can leave. stderr, argv and host paths remain private, and every classification still fails Stage. |
| Focused regression | 0 | Platform staging: 100 passed / 9 explicit Windows skips. Ruff, Python compilation and tracked whitespace checks pass; one unchanged Starlette multipart warning remains. Direct failure invokes no sandbox, a boundary failure invokes exactly one profile, write failures cannot reach read diagnostics, and snapshot mutation takes priority over the stage result. |
| Independent review | 0 | Final read-only review reports P0/P1 = 0. Its sole P2 identified unconditional execution of all six sandbox diagnostics after an already classified failure; the classifier now short-circuits in boundary, write, combined-write and read stages while rebinding every tree before each result. |
| Promotion | pending | A protected PR/main matrix and wholly new same-run three-platform Stage must identify the execution boundary before any functional change, Candidate signing or production activation. No output from `29569824355` may be reused. |

## macOS Framework interpreter source correction - 2026-07-17

| Scope | Exit | Result |
| --- | ---: | --- |
| Protected classifier baseline | 0 | The seven-snapshot execution classifier passed its protected PR/main matrix before a fresh Stage was started from exact main. |
| Exact-main Stage `29572046960` | 1 expected | Fresh macOS arm64 passed strict checkout, closure materialization, Mach-O relocation, canonical signing and archive-equivalent signature verification. Its independent no-sandbox copied snapshot then returned the fixed `pack_python_bootstrap_snapshot_direct_execution_failed`; macOS x64 and Windows were cancelled and the whole run is quarantined. |
| Root cause | 0 | actions/setup-python 3.11.9 installs the official python.org Framework. `Versions/3.11/bin/python3.11` is a small trampoline whose embedded contract executes the sibling `Resources/Python.app/Contents/MacOS/Python`. The old source selector copied only that trampoline to `pack-python/bin/python3`; the required relative app bundle was intentionally absent, so every copied closure was structurally unable to start regardless of sandbox policy or signature validity. |
| Runtime source correction | 0 local contract | macOS closure construction now copies the real Framework app interpreter at `Resources/Python.app/Contents/MacOS/Python`, confined to the resolved base prefix and accepted only as a regular non-link/non-reparse file. It never falls back to the Framework trampoline. Existing architecture inspection, exact dependency materialization, relocation, full Mach-O signing, copy-stability, isolated bootstrap and complete import probes remain mandatory. |
| Focused regression | 0 | Platform staging: 101 passed / 11 explicit platform skips on Windows. Fixture tests prove real-app selection, trampoline non-fallback and link refusal. A Darwin-only protected test binds the actual installed Framework path and interpreter architecture. Ruff, Python compilation and tracked whitespace checks are required before promotion. |
| Promotion | pending | The correction must pass the protected five-job matrix and exact-main verification. Only a wholly new same-run three-platform Stage may supply release evidence; no output from `29572046960` may be reused. |

## SQLite image migration WAL serialization - 2026-07-17

| Scope | Exit | Result |
| --- | ---: | --- |
| Protected matrix observation | 1 expected | PR #25 run `29575525003` passed Windows x64 and both macOS compatibility jobs, then the quality job exposed one concurrent image-schema migration failure while 2311 other tests passed. Cross-runner stability was correctly withheld and the run is not release evidence. |
| Root cause | 0 | SQLite serializes `BEGIN EXCLUSIVE`, but `journal_mode=WAL` must run after that transaction commits. Independent manager instances in the same Runtime could enter a second schema transaction in the commit-to-WAL gap and make the first WAL transition fail with a transient lock. |
| Runtime correction | 0 local contract | A canonical-database process coordinator now covers validation, schema transaction, immutable history receipt and WAL activation as one Runtime operation. Independent databases remain concurrent. InstallCoordinator remains the product-level cross-process owner for deployment migrations. |
| Focused regression | 0 | The full image SQLite schema-manager suite passes 13 tests. A deterministic eight-manager overlap fixture proves that no schema/WAL phase overlaps for one database; an additional 50-round stress run completed 1600 migrations. Ruff and tracked whitespace checks pass. |
| Promotion | pending | The correction requires a fresh protected five-job PR matrix, exact-main matrix and wholly new same-run three-platform Stage before any Candidate or production action. |

## Relocatable Playwright browser payload correction - 2026-07-17

| Scope | Exit | Result |
| --- | ---: | --- |
| Protected baseline | 0 | PR #25 run `29576754881` and exact-main run `29577191018` passed quality/deterministic build, Windows x64, macOS arm64/x64 and cross-runner byte stability. The squash merge is exact main `047f98cc5a737f4900a08108126bcdce9a66f9b7`. |
| Exact-main Stage `29577801469` | 1 expected | Fresh macOS arm64 passed the corrected real Framework interpreter and reached the Browser Pack smoke, then returned `browser_pack_smoke_failed`. The entire run was immediately cancelled; macOS x64 and Windows outputs are quarantined, and the one-use Windows runner unregistered. |
| Root cause | 0 | Playwright 1.52 exposes full `Chromium.app` through `chromium.executable_path`. The app contains required Framework aliases, while the regular-file-only Pack copier skipped directory symlinks and materialized a large file alias. The result was structurally incomplete and exceeded the bounded extracted-runtime budget before it could serve the smoke page. |
| Runtime correction | 0 local contract | Browser staging now derives the same-revision `chromium_headless_shell-*` payload from the managed Playwright cache, requires one exact executable and rejects missing, ambiguous, linked, reparse or special entries. It never falls back to the full application. Stable allowlisted Pack failure codes are retained for diagnosis while unknown provider detail remains redacted. |
| Focused regression | 0 | Platform Pack staging passes 104 tests / 12 explicit platform skips. Fixtures cover revision binding, missing and ambiguous payloads, tree links/reparse refusal and bounded smoke-code disclosure. Ruff, Python compilation and tracked whitespace checks pass; a real Playwright 1.52 / Chromium 1169 Windows headless-shell data-URL smoke also passed. |
| Promotion | pending | A protected PR/main matrix and wholly new same-run three-platform Stage are required. No artifact or receipt from `29577801469` may be reused. |

## Browser Pack lifecycle phase classification - 2026-07-17

| Scope | Exit | Result |
| --- | ---: | --- |
| Protected relocation baseline | 0 | PR #26 run `29579519598` and exact-main run `29580040897` passed all five protected jobs. The squash merge is exact main `6382223818b1dc7d465861c88678129ac938d0e4`. |
| Exact-main Stage `29580528963` | 1 expected | Fresh macOS arm64 crossed the previous Browser Pack size boundary with the revision-matched headless shell, then returned the redacted `browser_pack_smoke_pack_internal_failure`. macOS x64 and Windows were cancelled; the one-use Windows runner unregistered and the full run is quarantined. |
| Evidence boundary | 0 local contract | The old Pack protocol intentionally collapsed every non-ContractError to one stable code. This proved the relocation correction advanced execution, but could not distinguish driver start/stop, launch, mandatory network guard, context/page setup, navigation, operation or private runtime cleanup. Guessing from provider exception text is prohibited. |
| Fail-closed classifier | 0 local contract | Every browser lifecycle boundary now maps unexpected operational exceptions to a fixed allowlisted phase code without exception text, arguments or host paths. Cleanup cannot replace an earlier primary failure. Unknown codes remain redacted, mandatory HTTP/WebSocket guards remain enabled, and every classified outcome still fails Stage. |
| Focused regression | 0 | Platform Pack staging passes 105 tests / 12 explicit platform skips. Phase injection covers driver, browser, context, guard, page, operation and runtime preparation/cleanup plus primary-error preservation. Ruff, Python compilation and tracked whitespace checks pass. |
| Promotion | pending | The classifier requires protected PR/main verification, followed by a new same-run Stage that identifies the remaining browser lifecycle boundary. No output from `29580528963` may be reused. |

## Observability WAL point-in-time sampling - 2026-07-17

| Scope | Exit | Result |
| --- | ---: | --- |
| Protected Browser classifier | 0 | PR #27 run `29581272844` passed all five protected jobs. |
| Exact-main observation | 1 expected | Exact-main run `29581812925` passed Windows x64 and both macOS jobs, then exposed one health-projection race in the quality job while 2317 other tests passed. Cross-runner stability was correctly withheld; the run is not release evidence. |
| Root cause | 0 | System observability sampled the database and WAL with `exists()` followed by `stat()`. SQLite may legally unlink a WAL after its last reader closes or during checkpointing, so the two filesystem calls formed a TOCTOU window that could turn a normal point-in-time transition into an HTTP 500. |
| Runtime correction | 0 local contract | Storage sampling now performs one `stat` syscall. A concurrent `FileNotFoundError` is a valid zero-byte point-in-time sample; permission and other real storage failures remain visible and are never reported as healthy. |
| Focused regression | 0 | System observability passes 9 tests, including deterministic WAL disappearance and non-disappearance error propagation. Ruff, Python compilation and tracked whitespace checks pass. |
| Promotion | pending | A fresh protected PR/main matrix and wholly new same-run three-platform Stage remain mandatory. |

## Portable macOS Browser Runtime signatures - 2026-07-17

| Scope | Exit | Result |
| --- | ---: | --- |
| Protected observability correction | 0 | PR #28 run `29582699977` and exact-main run `29583176487` passed all five protected jobs. The squash merge is exact main `a5170cc18fc5f1ecc65cfaf5f4ddd0fd1dbf93b9`. |
| Exact-main Stage `29583698189` | 1 expected | Fresh macOS arm64 crossed Browser runtime extraction and returned the new exact `browser_pack_smoke_browser_driver_start_failed`. macOS x64 and Windows were cancelled, the one-use Windows runner unregistered and the whole run is quarantined. |
| Root cause | 0 | The regular-file Browser payload copied Playwright's native driver/node, greenlet and Chromium Mach-O bytes without establishing the same archive-stable signature ownership already required for Core. A source signature may verify in the managed cache while failing after the signed ZIP representation is extracted. The driver therefore failed before browser launch. |
| Runtime correction | 0 local contract | macOS Browser staging requires the target architecture slice in every final Mach-O, unconditionally ad-hoc signs every native member, strictly verifies each signature, round-trips the exact regular-file ZIP mode/digest representation, compares complete tree bindings and strictly verifies every Mach-O again in the extracted snapshot before inventory or archive admission. Links, special entries, duplicates, path drift, mode drift, missing members and digest drift fail closed. |
| Focused regression | 0 | Platform Pack staging passes 108 tests / 12 explicit platform skips. Tests prove all-member signing and verification order, pre-sign architecture refusal, archive-equivalent binding and copied-signature refusal. Ruff, Python compilation and tracked whitespace checks pass. |
| Promotion | pending | A protected PR/main matrix and wholly new same-run three-platform Stage are required. No receipt from `29583698189` may be reused. |

## Playwright driver executable-mode normalization - 2026-07-17

| Scope | Exit | Result |
| --- | ---: | --- |
| Protected signature baseline | 0 | PR #29 run `29584582173` and exact-main run `29585141573` passed all five protected jobs. The squash merge is exact main `953fc7ee148d3c4a927bcb0fd02934717b6abe2a`. The Browser Mach-O signature ownership remains a valid supply-chain control, but the next Stage proved it was not the driver-start root cause. |
| Exact-main Stage `29585694303` | 1 expected | Fresh macOS arm64 again returned the exact `browser_pack_smoke_browser_driver_start_failed` after all native signature and archive-stability gates passed. macOS x64 and Windows were cancelled; all outputs are quarantined and the one-use Windows runner unregistered. |
| Root cause | 0 | Playwright 1.52 starts its private `playwright/driver/node` with `create_subprocess_exec`. Distribution closure staging intentionally normalized every copied member to data mode `0644`; it therefore removed the driver's required POSIX execute bit. Signing a non-executable Mach-O succeeds, so the signature gates correctly did not repair or classify this file-mode defect. |
| Runtime correction | 0 local contract | Browser staging now locates the one pinned Playwright driver path inside the copied closure, rejects missing, linked, reparse, special or escaping entries, and normalizes only that trusted executable to `0755` on POSIX before Mach-O signing, inventory and archive creation. Windows retains its native PE execution semantics. Arbitrary dependency members do not inherit unbound wheel execute modes. |
| Focused regression | 0 | Platform Pack staging passes 109 tests / 13 explicit Windows/platform skips. POSIX coverage proves driver `0755`, ordinary package member `0644`, tree-record binding and exact ZIP mode preservation; missing driver fails closed. Ruff, Python compilation and tracked whitespace checks pass. |
| Promotion | pending | The correction requires protected PR/main verification and a wholly new same-run three-platform Stage. No receipt from `29585694303` may be reused. |

## Stage and Candidate secret-scan semantic alignment - 2026-07-17

| Scope | Exit | Result |
| --- | ---: | --- |
| Protected driver-mode correction | 0 | PR #30 run `29587038301` and exact-main run `29587579488` passed all five protected jobs. The squash merge is exact main `2b54046575a53836563904aa76e24345921c31af`. |
| Exact-main Stage `29588232914` | 1 expected | macOS arm64 passed the Browser functional smoke and then failed closed at `stage_supply_chain_secret_match`; macOS x64 and Windows were cancelled, all outputs were quarantined and the one-use Windows runner unregistered. |
| Root cause | 0 | Stage applied textual AWS/GitHub/Slack token regular expressions to every small opaque Browser member after macOS relocation and ad-hoc signing. The authoritative Candidate scanner intentionally applied those token detectors only to canonical text/config paths, while still detecting complete PEM private keys everywhere. Offline scanning of the pinned original Playwright/greenlet/pyee/typing-extensions and Chromium rev1169 macOS arm64 payloads returned no match, isolating the drift to the transformed opaque Browser archive rather than source text. |
| Runtime correction | 0 local contract | One shared release scanner now owns Stage and Candidate semantics. Complete PEM private keys remain blocked in every payload. AWS/GitHub/Slack token shapes are scanned from raw bytes for the canonical text/config suffix and filename allowlist, so malformed UTF-8 and embedded NUL cannot bypass a text member. Opaque native/resource members remain governed by exact dependency locks, tree digests, architecture checks and portable signature gates rather than being misclassified as text. There is no dependency-specific or digest-specific exception. |
| Focused regression | 0 | Platform Pack staging passes 112 tests / 13 explicit platform skips. Candidate pipeline passes 16 tests with one local environment-only lock-profile test deselected; that test remains protected-CI owned. Coverage proves opaque native token-like bytes do not create a text false positive, complete PEM inside opaque bytes still blocks, malformed text still blocks, nested archives retain traversal/collision/encryption/size gates, and Stage/Candidate call the same policy. Ruff, Python compilation and tracked whitespace checks pass. |
| Promotion | pending | The correction requires protected PR/main verification and a wholly new same-run three-platform Stage. No receipt from `29588232914` may be reused. |

## Secret-scan non-disclosing locator - 2026-07-17

| Scope | Exit | Result |
| --- | ---: | --- |
| Protected shared-policy baseline | 0 | PR #31 run `29589850057` and exact-main run `29590654155` passed all five protected jobs. The squash merge is exact main `e12798b83a0be2268903396cf7712577800534cf`. |
| Exact-main Stage `29591312124` | 1 expected | macOS arm64 again passed Browser execution and failed closed at `stage_supply_chain_secret_match`; macOS x64 and Windows were cancelled, outputs were quarantined and the one-use Windows runner unregistered. This proves the remaining match is in a canonical text/config member or is a complete PEM shape, but the prior public code intentionally cannot identify which member. |
| Diagnostic boundary | 0 local contract | Stage now emits only a fixed detector id, `regular/archive_member` kind, SHA-256 of the canonical logical location and SHA-256 of the complete member content. It never emits the path, matching bytes, surrounding bytes, arguments or host location. The outer wrapper strictly validates the exact keys, detector allowlist and 64-hex hashes before printing the safe event; the signed failure receipt retains only the original public failure code. Malformed or extra diagnostic fields are discarded. |
| Focused regression | 0 | Platform staging plus process-boundary coverage passes 124 tests / 13 explicit platform skips. Tests bind the expected hashes, reject a raw path in the diagnostic, preserve the generic failure contract and prove no provider detail is surfaced. Ruff, Python compilation and tracked whitespace checks pass. |
| Promotion | pending | A fresh protected PR/main matrix and a new Stage are required to locate the match without weakening the secret gate. No receipt from `29591312124` may be reused. |

## Opaque native secret-scan boundary - 2026-07-17

| Scope | Exit | Result |
| --- | ---: | --- |
| Protected locator baseline | 0 with infrastructure retry | PR #32 run `29592091271` passed all five jobs. Exact-main run `29592680293` attempt 1 had a GitHub artifact-service 403 after the macOS x64 byte contract was built; attempt 2 reran the failed jobs and passed. The squash merge is exact main `ee99d68bba269b43bc5c29f3a7984001ef39c324`. |
| Exact-main Stage `29593568882` | 1 expected | The safe locator reported `private_key`, `regular`, location hash `8b70935c…` and content hash `2b8f602e…`; it emitted no path or matched bytes. macOS x64 and Windows were cancelled and all outputs are quarantined. |
| Deterministic root cause | 0 | The two hashes map exactly to the hash-locked macOS arm64 `opencv-python==5.0.0.93` member `runtime/python/cv2/.dylibs/libgnutls.30.dylib`. The complete content digest equals the original wheel member before staging, proving this is an upstream opaque native data fixture rather than a Runtime-injected credential or signing mutation. Candidate's historical policy already excluded opaque members from text-token scanning. |
| Scanner correction | 0 local contract | All credential detectors, including complete PEM shape detection, now run only on the shared canonical text/config path contract. Detection still operates on raw bytes, so malformed UTF-8 or NUL cannot evade a `.pem`, `.key`, source or config member. Opaque native/resource members receive no content-specific exception: they are admitted only through exact hashed dependency locks, complete tree/content binding, architecture/relocation checks and portable signature gates. Stage and Candidate use the same function. |
| Focused regression | 0 | Platform staging and Candidate pipeline pass 130 tests / 13 explicit platform skips with one local environment-only lock-profile test deselected. The exact opaque native fixture class passes without a text false positive; the same complete PEM bytes in `.pem` fail even with malformed encoding. Stage/Candidate parity, Ruff, Python compilation and tracked whitespace checks pass. |
| Promotion | pending | A protected PR/main matrix and wholly new same-run three-platform Stage are required. No receipt from `29593568882` may be reused. |

## macOS Seatbelt behavioral probe correction - 2026-07-17

| Scope | Exit | Result |
| --- | ---: | --- |
| Protected native-scan baseline | 0 | PR #33 run `29595087984` and exact-main run `29595617813` passed all five protected jobs. The squash merge is exact main `13d8a912a48f330ebe1d29896ba9d8e38818d289`. |
| Exact-main Stage `29596154340` | 1 expected | macOS arm64 crossed dependency, Browser and supply-chain gates, then failed closed at the exact `sandbox_boundary_probe_failed` code. macOS x64 and Windows were cancelled, all outputs are quarantined and the one-use Windows runner unregistered. |
| Root cause | 0 | The Seatbelt probe correctly denied the child outside-workspace write, but then attempted to reread the outside file from inside the same sandbox. Metadata access was allowed while content access was denied, so the unhandled second denial terminated the probe. Its network check also targeted a normally closed fixed port, making ordinary `ECONNREFUSED` indistinguishable from policy denial. |
| Runtime correction | 0 local contract | The host now owns a random outside canary and a live random loopback listener. The sandbox reports exact read, write, child-write and network errno evidence; only `EACCES`/`EPERM` with strict JSON types, an exact successful child completion, a safely opened regular child marker and an unchanged host canary establish readiness. Missing, extra, malformed, boolean, refusal, I/O, crash and non-zero evidence fail closed. The sandbox never rereads protected content after the denial. |
| Focused regression | 0 | Process Capability Pack passes 18 tests / one real-mac host skip on Windows; the broader platform/process release suites pass 184 tests with explicit platform skips. Ruff, Python compilation, an unsandboxed script-execution fail-closed check and tracked whitespace checks pass. The Darwin-only real-host test now asserts the full Seatbelt contract instead of skipping an incomplete probe. |
| Promotion | pending | The correction requires a fresh protected PR/main matrix and wholly new same-run three-platform Stage. No receipt from `29596154340` may be reused. |

## macOS Seatbelt socket-construction denial - 2026-07-17

| Scope | Exit | Result |
| --- | ---: | --- |
| Protected probe baseline | 0 | PR #34 run `29597667633` and exact-main run `29598187969` passed all five protected jobs. The squash merge is exact main `482712f0a88b62e42020e81f9fa147029f9efd68`. |
| Exact-main Stage `29598702668` | 1 expected | macOS arm64 again failed closed at `sandbox_boundary_probe_failed`; macOS x64 and Windows were immediately cancelled, the one-use runner unregistered and the whole run quarantined. No result from this run is release evidence. |
| Remaining evidence boundary | 0 | The public failure code intentionally did not expose the exact evidence field. Code-path audit found one unhandled valid-denial path: Seatbelt may reject the network boundary when the socket is created, before `connect_ex` is reached. The live-listener probe caught only a `connect_ex` errno, so a construction-time `EACCES`/`EPERM` terminated the probe process. This is distinct from the already removed `ECONNREFUSED` false positive. |
| Runtime correction | 0 local contract | Socket construction and connection now form one bounded `OSError` boundary. Either phase must report an exact typed denial errno; a successful construction followed by connection refusal, successful connection, malformed errno or unrelated error remains rejected by the unchanged evaluator. The socket is closed in all constructed cases. Every remaining failure branch now maps to a fixed non-disclosing reason code, and Stage permits only that explicit allowlist; no errno, path, command or provider text is emitted. |
| Focused regression | 0 | Process and platform staging pass 133 tests / 14 explicit platform skips. Tests cover every evidence classifier branch, every public Stage allowlist entry, arbitrary-reason collapse, script compilation, Ruff, dependency-lock integrity and tracked whitespace. An independent security review found no fail-open or disclosure path. |
| Promotion | pending | This correction requires fresh protected PR/main verification and another wholly new same-run Stage. No output from `29598702668` may be reused. |

## macOS Seatbelt host-evidence preservation - 2026-07-17

| Scope | Exit | Result |
| --- | ---: | --- |
| Protected socket baseline | 0 | PR #35 run `29599969941` and exact-main run `29600514955` passed all five jobs. The squash merge is exact main `7d7eb4f4ea3a8e3d7f69ece149f4136825fa8d4a`. |
| Exact-main Stage `29601034159` | 1 expected | macOS arm64 returned the new fixed `macos_seatbelt_probe_process_unavailable` code. macOS x64 and Windows were immediately cancelled, the one-use runner unregistered and the run quarantined. This excludes every evaluator errno branch but still combines host evidence-read failures with a genuinely unavailable bounded process. |
| Diagnostic-collapse root cause | 0 | The host validator attempted to open the child marker before calling the evaluator. A missing, refused or invalid marker raised `OSError`; the outer catch then replaced any already completed subprocess and typed JSON with `completed=None`. The classifier could therefore report process unavailable even when the real boundary was child or marker evidence. The same collapse existed for host canary revalidation and cleanup errors; the public Stage result cannot distinguish these from a genuinely unavailable bounded process. |
| Runtime correction | 0 local contract | Completed subprocess evidence is now immutable once captured. JSON failure becomes `evidence_invalid`; canary read failure becomes `canary_changed`; missing, linked, non-regular, unreadable or mismatched marker becomes `child_marker_invalid`; cleanup errors cannot replace the primary result. Only a genuine launch, timeout, transport or bounded-output failure remains `process_unavailable`. All outcomes stay fail closed. |
| Focused regression | 0 | Process and platform suites pass 137 tests / 14 explicit platform skips; the final focused probe file passes 23 / one real-mac skip. Parametrized host checks prove marker, canary, JSON and bounded-runner failures keep distinct codes. Ruff, Python compilation, dependency locks, tracked whitespace and independent review pass. |
| Promotion | pending | Fresh protected PR/main verification and a wholly new same-run Stage are required. No output from `29601034159` may be reused. |

## macOS Seatbelt in-probe runtime classification - 2026-07-17

| Scope | Exit | Result |
| --- | ---: | --- |
| Protected host-evidence baseline | 0 | PR #36 run `29601951339` and exact-main run `29602444293` passed all five jobs. The squash merge is exact main `ec3c7378857d9519e7749a7e77b2811fd4fea076`. |
| Exact-main Stage `29602976932` | 1 expected | macOS arm64 returned `macos_seatbelt_probe_process_nonzero`, proving the bounded sandbox process launched but the in-sandbox probe exited before complete JSON evidence. macOS x64 and Windows were immediately cancelled and the entire run quarantined. |
| Remaining evidence boundary | 0 | All direct filesystem, network-construction/connect, workspace-write and child-result operations were already structured. Two in-probe runtime operations could still escape the JSON contract: `subprocess.run` could raise during child launch, and closing a constructed socket could raise during cleanup. Either became an undifferentiated parent-process nonzero. |
| Runtime correction | 0 local contract | Child launch now records a strict zero-or-errno field and never fabricates a return code. Socket cleanup records an exact boolean and cannot replace the network-denial errno. The evaluator requires successful child launch and cleanup, rejects malformed or boolean errno values, and emits separate allowlisted `child_launch_failed` or `network_cleanup_failed` codes. Non-dict child JSON is rejected without raising. |
| Focused regression | 0 | Process and platform suites pass 137 tests / 14 explicit platform skips. The exact embedded parent and child scripts compile and execute unsandboxed while rejecting the host. All classifier and Stage allowlist branches, Ruff, Python compilation, dependency locks and tracked whitespace pass. |
| Promotion | pending | Fresh protected PR/main verification and a wholly new same-run Stage are required. No output from `29602976932` may be reused. |

## macOS Seatbelt interpreter handshake - 2026-07-18

| Scope | Exit | Result |
| --- | ---: | --- |
| Protected structured-runtime baseline | 0 | PR #37 run `29603785165` and exact-main run `29604273489` passed all five jobs. The squash merge is exact main `ecc3d849598fa2b5da0ce0fa04ef151a1889f8ea`. |
| Exact-main Stage `29604805336` | 1 expected | macOS arm64 still returned `macos_seatbelt_probe_process_nonzero`; macOS x64 and Windows were immediately cancelled and the entire run quarantined. Structured child launch and socket cleanup therefore did not identify the failure, leaving either pre-script interpreter startup or an abnormal in-script phase. |
| Observability root cause | 0 | A process return code alone cannot distinguish sandbox-exec/Python startup failure from an exception after Python begins executing. Enumerating individual operations cannot close that evidence gap and creates repeated generic failures. |
| Runtime correction | 0 local contract | The probe emits and flushes one fixed startup handshake as its first Python action. Successful output must contain exactly that marker plus one canonical JSON line. Every subsequent phase runs inside a top-level fail-closed envelope that emits only a fixed phase id; missing marker, interpreter-start failure and each fatal phase have explicit Stage allowlist codes. No exception text, stderr, path, argv, errno or provider detail is disclosed. |
| Focused regression | 0 | Process and platform suites pass 137 tests / 14 explicit platform skips. The exact parent and child scripts compile and execute, the handshake is parsed, an unsandboxed host is rejected, every fixed code is covered, and Ruff, Python compilation, dependency locks and tracked whitespace pass. |
| Promotion | pending | Fresh protected PR/main verification and a wholly new same-run Stage are required. No output from `29604805336` may be reused. |

## Truthful macOS workspace-write read contract - 2026-07-18

| Scope | Exit | Result |
| --- | ---: | --- |
| Protected handshake baseline | 0 | PR #38 run `29606392488` passed all five protected jobs. Exact-main run `29606908819` initially timed out in the quality job after the platform jobs passed; its failed-job rerun passed quality and cross-runner stability. The squash merge is exact main `e75209ef2114da52dec5d738d0138916c1e98d3a`. |
| Exact-main Stage `29609800335` | 1 expected | Fresh macOS arm64 returned `macos_seatbelt_probe_interpreter_start_failed`; macOS x64 and Windows were cancelled and all outputs are quarantined. The startup handshake proves the relocated interpreter could not execute its first Python line inside the deny-default, interpreter-specific read allowlist. |
| Root cause | 0 | The product profile is workspace-write: reads are allowed, writes are restricted to selected workspaces, and network is denied. The macOS policy instead attempted to enumerate the complete dynamic read closure of a signed Framework Python and its runtime. That closure is platform/build dependent and prevented a valid interpreter from starting. The wire contract also incorrectly advertised `workspace-only` reads. |
| Runtime correction | 0 local contract | macOS Seatbelt now permits host reads, restricts writes to workspace roots and denies network. The behavioral probe proves exact canary content through a digest comparison without emitting it, proves direct and inherited outside writes fail, proves workspace writes work and proves a live loopback connection is denied. `SandboxProbe` carries an explicit read scope; the Pack contract reports `host-unrestricted` on macOS and `workspace-and-runtime` for the existing scoped Windows backend. Per-invocation TEMP/TMP is created as a private hidden directory under the selected workspace and removed after the child is reaped. |
| Promotion | pending | This correction requires protected PR/main verification and a wholly new same-run three-platform Stage. No output from `29609800335` may be reused. |

## Bootstrap Go-test failure classification - 2026-07-18

| Scope | Exit | Result |
| --- | ---: | --- |
| Protected sandbox correction | 0 | PR #39 run `29611344947` and exact-main run `29611825251` passed all five protected jobs. The squash merge is exact main `b970e3889a3b78a038fa8cbec956746154fd5b86`. |
| Exact-main Stage `29612323015` | 1 expected | macOS arm64 crossed the corrected Seatbelt interpreter and behavioral boundary, then returned `bootstrap_test_failed`. macOS x64 and Windows were cancelled, the one-use Windows runner unregistered and the whole run is quarantined. This confirms the sandbox correction while exposing a later Bootstrap unit-test boundary. |
| Evidence boundary | 0 | The bounded Go test process retained output privately but collapsed every compilation, package and named product-test failure to one code. Provider text must remain private, but a source-owned test identity is safe and necessary to distinguish a product regression from toolchain or package startup failure. |
| Fail-closed classifier | 0 local contract | Bootstrap tests now use Go's JSON event stream inside the existing bounded process supervisor. Only one exact allowlisted source-owned test name maps to a fixed failure code; multiple, unknown/package, launch, timeout, malformed and output-overflow outcomes receive separate fixed codes. stderr, arbitrary output, paths and toolchain text never cross the Stage boundary. Every classified result still fails Stage. |
| Promotion | pending | The classifier requires protected PR/main verification and a wholly new same-run Stage. No output from `29612323015` may be reused. |

## Bootstrap multi-test fixed-code evidence - 2026-07-18

| Scope | Exit | Result |
| --- | ---: | --- |
| Protected classifier baseline | 0 | PR #40 run `29613128502` and exact-main run `29613597925` passed all five protected jobs. The squash merge is exact main `317360cdda278044ca3daf4b182ebe69720a4157`. |
| Exact-main Stage `29614152095` | 1 expected | macOS arm64 returned `bootstrap_test_multiple_failed`; macOS x64 and Windows were cancelled, the one-use Windows runner unregistered and the whole run is quarantined. The failure is therefore within at least two source-owned Bootstrap tests rather than package startup or one test. |
| Evidence refinement | 0 local contract | A multiple-test result may carry only a sorted, deduplicated set of fixed allowlisted public test codes and a bounded decimal count. The Stage error constructor independently revalidates both fields; unknown strings, paths, output, duplicates, oversized counts and arbitrary diagnostics are discarded. Go output and stderr remain private and the result still fails Stage. |
| Promotion | pending | This evidence refinement requires protected PR/main verification and a wholly new same-run Stage. No output from `29614152095` may be reused. |

## Platform-stager Bootstrap diagnostic forwarding - 2026-07-18

| Scope | Exit | Result |
| --- | ---: | --- |
| Protected fixed-set baseline | 0 | PR #41 run `29614785313` and exact-main run `29615227588` passed all five protected jobs. The squash merge is exact main `11aca8408f470cbabc7ef77850a048454b106aad`. |
| Exact-main Stage `29615646417` | 1 expected | macOS arm64 again returned `bootstrap_test_multiple_failed`, but the repository-owned invocation adapter emitted no safe diagnostic. macOS x64 and Windows were cancelled, the one-use Windows runner unregistered and the whole run is quarantined. |
| Protocol root cause | 0 | The stager constructed a validated fixed-code set, while its digest-pinned parent adapter forwarded only the older secret-scan hash diagnostic. The process boundary therefore intentionally discarded the new diagnostic before the workflow could observe it. |
| Adapter correction | 0 local contract | The parent adapter owns an independent closed copy of the public Bootstrap code set and revalidates exact keys, sorted uniqueness, membership, count syntax, count bounds and count-to-set consistency before forwarding. Arbitrary values, raw output, paths and malformed diagnostics are dropped. The Stage failure code remains authoritative and fail closed. |
| Promotion | pending | Protected PR/main verification and a wholly new same-run Stage are required. No output from `29615646417` may be reused. |

## Canonical macOS Bootstrap test roots - 2026-07-18

| Scope | Exit | Result |
| --- | ---: | --- |
| Protected forwarding baseline | 0 | PR #42 run `29616208127` and exact-main run `29616587415` passed all five protected jobs. The squash merge is exact main `0004afcbbce29305ead352ea0028d43de5c03cbf`. |
| Exact-main Stage `29617026723` | 1 expected | The safe fixed-code set identified exactly `bootstrap_test_local_migration_failed`, `bootstrap_test_pointer_authority_failed` and `bootstrap_test_pointer_freshness_failed`. macOS x64 and Windows were cancelled, the one-use Windows runner unregistered and the full run is quarantined. |
| Shared root cause | 0 | All three tests enter `ensureBootstrapStateDirectory`. On the hosted macOS arm64 image, Go's `t.TempDir()` is presented through a system temporary-path alias whose symlinks resolve to another canonical absolute path. The product security contract correctly rejects an install root containing a link, so the tests supplied an intentionally invalid fixture before reaching their assertions. Production's default user-data root is unchanged. |
| Test correction | 0 local contract | Security-sensitive Bootstrap tests now resolve their harness temp directory, require a real non-link directory and pass the canonical absolute path into the product contract. `ensureBootstrapStateDirectory` is not weakened and no production alias or link becomes trusted. |
| Promotion | pending | Protected PR/main verification and a wholly new same-run Stage are required. No output from `29617026723` may be reused. |

## Platform-stager stdout protocol isolation - 2026-07-18

| Scope | Exit | Result |
| --- | ---: | --- |
| Protected canonical-root baseline | 0 | PR #43 run `29617557321` and exact-main run `29617969135` passed all five protected jobs. The squash merge is exact main `a99c8fb55124a03b67e29a5039ad8e907bfeff14`. |
| Exact-main Stage `29618345433` | 1 expected | macOS arm64 completed the real stager with exit zero but the parent rejected its stdout as `platform_stager_response_invalid`. macOS x64 and Windows were cancelled, the one-use Windows runner unregistered and the full run is quarantined. |
| Root cause | 0 | The successful macOS Pack sandbox probe printed a progress marker to stdout immediately before `main` wrote the single success JSON object. The strict parent correctly rejects the resulting two-message stream rather than parsing the last line. |
| Protocol correction | 0 local contract | The progress print is removed; successful platform staging reserves stdout for exactly one protocol response. An AST contract gate rejects every future `print` not explicitly directed to stderr and requires exactly one fixed `sys.stdout.write` success response. Sandbox, probe and failure behavior are unchanged. |
| Promotion | pending | Protected PR/main verification and a wholly new same-run Stage are required. No output from `29618345433` may be reused. |

## Unified Stage secret-shape policy - 2026-07-18

| Scope | Exit | Result |
| --- | ---: | --- |
| Protected stdout baseline | 0 | PR #44 run `29618871855` and exact-main run `29619217940` passed all five protected jobs. The squash merge is exact main `5e5856daae6c7ae9bed7c604570bf6c90d1e6066`. |
| Exact-main Stage `29619611874` | 1 expected | macOS arm64 crossed the corrected single-response protocol and returned `stage_source_secret_detected` while the parent generated Stage receipts. macOS x64 and Windows were cancelled, the one-use Windows runner unregistered and the full run is quarantined. |
| Root cause | 0 | The stager supply-chain gate uses the shared path-aware secret policy and had passed the same final tree. Receipt generation instead applied raw token regexes to every opaque native byte stream, so signed Mach-O/runtime bytes could be classified as credentials even though the source-owned policy intentionally scans only bounded text contracts. |
| Policy correction | 0 local contract | Stage receipt hashing now uses the same centralized `detect_secret` policy and the same 4 MiB text-contract bound as the attested stager supply-chain gate. Hashing, identity checks and size limits remain streaming and unchanged; real secret shapes in text contracts still fail closed. A macOS opaque-native regression fixture proves token-shaped binary bytes are not treated as credentials. |
| Promotion | pending | Protected PR/main verification and a wholly new same-run Stage are required. No output from `29619611874` may be reused. |

## Local WebUI regression and deployment resumption - 2026-07-18

| Scope | Exit | Result |
| --- | ---: | --- |
| JSON and tracked-diff record validation | 0 | `python -m json.tool docs/v1.0/progress.json` and `git diff --check -- docs/v1.0/progress.json docs/v1.0/implementation-log.md` passed. The current operator instruction supersedes the temporary release deferral but does not change the fact that prior failed Stage outputs are quarantined. |
| WebUI type contract | 0 | `npm run typecheck` passed after generated Runtime contract verification. |
| WebUI product regression | 0 | `npm run test:v1` passed 182/182, including the live Turn timestamp regression, first-turn terminal cleanup, persistent reasoning, model selection, artifact/retouch, extension, share and interaction contracts. |
| Content-addressed production Web build | 0 | `npm run build` passed: 25 production assets, 24 chunks, 459.76 KiB initial JS / 146.20 KiB gzip and 136.30 KiB deferred / 49.78 KiB gzip. |
| Full v1 Runtime suite on this workstation | incomplete, not a product failure | Initial collection found the local `.venv` lacked the lock-required `Pillow`; the verified `cloud` profile was installed after restoring the environment's missing pip bootstrap. The rerun then exceeded the local command host's 120-second ceiling and was terminated by that host. Protected CI remains the full-suite authority. |
| Release authority | pending | The product owner explicitly restored authorization to deploy after verification. Commit/push, immutable-main CI and the authenticated controlled release path remain required before any online success claim. |

## Direct-production model-gateway gate - 2026-07-18

| Scope | Exit | Result |
| --- | ---: | --- |
| Legacy administrator import | 0 | Imported 40 live users and six encrypted, pending-test model slots; seven deleted users were excluded. Existing legacy public services remain active while the v1 route is not switched. |
| Retained-key revalidation | mixed | The normal audited stage/test/activate workflow reused no plaintext output. Doubao passed and became active. `gpt-5.6-sol`, Gemini, Image 2 and Image 2 Edit returned the safe `provider_test_unavailable` outcome; DeepSeek returned `provider_test_protocol`. |
| Provider bridge diagnosis | hold | The production host times out to the official OpenAI/Image and Google endpoints. Legacy OpenAI/Gemini bases are public HTTP-only, which v1 rejects rather than transmitting retained keys insecurely. |
| Web/Admin deployment | not attempted | Main WebUI and `/admin/` remain on the legacy public route. No partial frontend or Admin cutover is permitted while required model/image readiness is false. |
