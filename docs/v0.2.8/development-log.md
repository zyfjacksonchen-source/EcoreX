# EcoreX v0.2.8 Development Log

## Goal

Build production-grade runtime behavior for long agent tasks:

- task observation with health, timeout, and user intervention decisions;
- Codex-style same-session queueing so new user messages do not implicitly cancel the running task;
- durable evidence in code, tests, and release notes.

## Decisions

- Same-session input policy: queue-first. A new message sent during a running task is accepted as a queued run. It must not cancel the running request unless the user explicitly stops it.
- Queue storage: reuse RunLedger and runtime event ledger for v0.2.8. Do not introduce a second database in this slice.
- User surface: chat stream first. Queued requests appear as queued in active request snapshots and stream phases; the queued-message card exposes a direct `引导` action and does not ask the user to open Run Center.
- Task observation policy: additive event model first, then progressively wire long tools such as image generation into provider-level health.

## Execution Notes

- 2026-07-04: Created long goal and began implementation.
- 2026-07-04: Confirmed existing WebChannel behavior uses `interrupt_previous` and `_interrupt_and_wait_for_session_lock()` for busy same-session sends.
- 2026-07-04: Confirmed `RunLedger` already recognizes `queued` as active but did not provide real queue lifecycle semantics.
- 2026-07-04: Updated `RunLedger` so queued runs have no `started_at` until they leave queued state, and added `queued_snapshot()`.
- 2026-07-04: Added WebChannel same-session queue state, queue-first busy-session admission, automatic queued-run start after session lock release, and `/api/requests/{request_id}/queue-action`.
- 2026-07-04: Updated Desktop send behavior so queued messages do not steal the currently running stream.
- 2026-07-04: Added `TaskObserver` and wired tool execution heartbeat/deadline/timeout/end into additive `task.*` events.
- 2026-07-04: Added runtime projection support for `task_observations`.
- 2026-07-04: Added image-job job-level observation: `task.started`, progress heartbeats, intervention requests after soft/stall thresholds, and terminal `task.*` events without changing the existing `image_job.*` replay contract.
- 2026-07-04: Added image-job observation actions `continue`/`extend`/`background` alongside existing cancel/status/collect controls.
- 2026-07-04: Verification passed: Python compile check for image-job observation modules; `tests/test_v028_runtime_queue_observation.py`; focused queue/projection/image-job pytest slices; Desktop renderer and Electron builds.
- 2026-07-04: Added `runtime-observability-and-queue-architecture.md` to record the v0.2.8 architecture, acceptance checks, and remaining production gaps.
- 2026-07-04: Added file-backed queued request payload store under workspace `.ecorex/queued-requests`; queued runs can be recovered from RunLedger plus payload files after runtime restart.
- 2026-07-04: Active request snapshots now include projected `task_observations`/`image_jobs` summaries for Run Center without requiring raw event parsing.
- 2026-07-04: Run Center now shows compact task-observation state and exposes image-job `continue`/`background` controls when intervention is requested.
- 2026-07-04: Added RunLedger queued-run claim lease (`lease_owner`, `lease_expires_at`) so concurrent WebChannel runtimes cannot start the same queued message twice.
- 2026-07-04: Cleaned the queued payload save-failure path so rejected queue admissions do not leave stale in-memory payload state.
- 2026-07-04: Verification passed for the final v0.2.8 slice: task observation unit tests, queue/active/projection regression slice, image-job regression slice, Desktop renderer build, and Electron build.
- 2026-07-04: Added v0.2.8 runtime-observability/queue checks to the real release validation harness, multi-agent strategy, rerun strategy, release notes, package versions, and focused tests.
- 2026-07-04: Built v0.2.8 release artifacts and promoted local manifest hashes: Windows WebUI zip SHA256 `CE7BCE8A29E30DD0E378D23B10352EAF351581487C0BD6B78413A7D8724499A5`; macOS WebUI zip SHA256 `59197B1D6E08FBC8A1A551600A96D75CFB5766FA1498EC43747EBEC6751A15C3`; Linux Web service tarball SHA256 `38CA44DCB2D74EA43FEF7B93946182455293A335A8A42D1558484B6522411C94`.
- 2026-07-04: Ran the full real release gate after deployment. The deploy phase passed, but the product acceptance matrix finished `FAIL` with `561/576` passed checks; do not treat v0.2.8 as full product-acceptance green until the remaining v0.2.7-drift checks are triaged and rerun.
- 2026-07-04: Created GitHub Release `v0.2.8` in `zhangyifanjackson-dotcom/EcoreX-installers` and uploaded the three public download assets plus the Linux SHA256 sidecar. The installer repo checkout remained source-free.
- 2026-07-04: Switched download priority to domestic mirror first: `https://ghproxy.net/https://github.com/zhangyifanjackson-dotcom/EcoreX-installers/releases/download/v0.2.8` is first in `download.mirrors`; GitHub Release remains second as fallback.
- 2026-07-04: Fixed public-release mirror classification so GitHub proxy URLs are emitted as `asset-base` mirrors instead of duplicate `github-release` entries.
- 2026-07-04: Rebuilt and validated `release-artifacts/EcoreX_0.2.8-public-release.zip` with domestic-mirror-first manifest. Final public zip SHA256 is `BA75DAB74941EEF953FAB1036C172EDE3DC5C18C9168C1A8AA7208C457FE253D`.
- 2026-07-04: Promoted the updated public release to production. `docs/v0.2.8/artifacts/production-deploy-online.json` reports `PASS`; live manifest probe returned HTTP 200, version `0.2.8`, first mirror `ghproxy.net`, second mirror GitHub Release.
- 2026-07-04: Fixed the Windows WebUI installer `copy runtime` failure found in hand testing. The installer now installs into a versioned runtime slot (`runtime-0.2.8-<guid>`), writes `state/current-runtime.txt`, records `state/ecorex-webui.url`, stops existing WebUI Python processes under the install root before copy, and cleans old runtime directories only after the new runtime health check succeeds.
- 2026-07-04: Hardened the online Windows installer by updating its version markers/User-Agent to `0.2.8` and adding a .NET SHA256 fallback when `Get-FileHash` is unavailable in a spawned PowerShell process.
- 2026-07-04: Added an independent legacy WebUI online-upgrade smoke test for `0.2.7.1` and `0.2.7.2` (`scripts/smoke-v028-legacy-webui-online-upgrade.ps1`) and wired it into `scripts/真实发布校验.py`. The test is API/install based and does not depend on Edge login state; if UI browser validation is added later, use Chrome.
- 2026-07-04: Legacy WebUI online-upgrade test passed `7/7`: both `0.2.7.1` and `0.2.7.2` receive the v0.2.8 update notification, report `updateReason=version`, verify Windows artifact SHA256 `C8017FB0370C2DA1E2D9895250734E8E9BBB87D489D85888BF55832EB4836363`, and upgrade online into versioned runtime slots (`runtime-0.2.8-ca93b499`, `runtime-0.2.8-7bed0bfe`).
- 2026-07-04: Focused v0.2.8 installer-slot validation passed `89/89` in `docs/v0.2.8/artifacts/focused-v028-installer-slot-fix.json`.
- 2026-07-04: Rebuilt and republished v0.2.8 artifacts after the installer fix. Final hashes: Windows WebUI zip SHA256 `C8017FB0370C2DA1E2D9895250734E8E9BBB87D489D85888BF55832EB4836363`; macOS WebUI zip SHA256 `BEC15B9711C7AF597BDB633B798CBDDDC113B7586B06FDF48C1559BCE0174806`; Linux Web service tarball SHA256 `619C7B561972F19E0EA808328D8D90B6D413523BF6328C3809F6445D728366CA`; public release zip SHA256 `BD5B57B51CF1F853D857E018A1B5BF5E88EE58F1B188C21ECCE79A72AFE32365`.
- 2026-07-04: Updated GitHub Release `v0.2.8` in `zhangyifanjackson-dotcom/EcoreX-installers` and kept the domestic mirror first: `https://ghproxy.net/https://github.com/zhangyifanjackson-dotcom/EcoreX-installers/releases/download/v0.2.8`, with GitHub Release as fallback.
- 2026-07-04: Re-promoted the updated public release and Web service to production. `docs/v0.2.8/artifacts/production-deploy-online.json` reports `PASS`, public manifest version is `0.2.8`, service is active/enabled, and the promoted public zip is `BD5B57B51CF1F853D857E018A1B5BF5E88EE58F1B188C21ECCE79A72AFE32365`.
- 2026-07-04: Live download probes passed for the critical user path: production manifest HTTP 200; first Windows mirror (`ghproxy.net`) HTTP 200 with `Content-Length=550779391`; GitHub origin fallback HTTP 200 with `Content-Length=550779391`; first macOS mirror HTTP 200 with `Content-Length=652234115`.
- 2026-07-04: Full production agent product acceptance was rerun after the installer fix and finished `FAIL` with `575/577` checks passing. The remaining two failures are both in `v027-integrated-capabilities` Tongxin MPI accuracy/data-volume sampling (`tongxin_mpi_accuracy_zero_project_samples`: account/project sample count is 0). Do not mark the whole product gate green until this data-side blocker is addressed and the final gate is rerun.
- 2026-07-04: Added `docs/v0.2.8/artifacts/final-gate-blocked-tongxin-data-volume.json` to make the residual gate boundary explicit. This is the same external data-volume blocker previously recorded for v0.2.7; it requires a mounted non-empty read-only Tongxin SQLite database via `ECOREX_TONGXIN_DATABASE` or `tools.tongxin_cli.database_path`.
- 2026-07-04: Ran a read-only production Tongxin data-volume probe and recorded `docs/v0.2.8/artifacts/remote-tongxin-data-volume-probe.json`. The production service is active/enabled and Tongxin CLI status is configured, but the configured database file does not exist, default database paths are absent, and a root-filesystem SQLite search found `4` candidates with `0` project/account/report/MPI/XHS-related non-empty tables. This confirms the remaining full-gate failure is an external data mount/configuration blocker, not a v0.2.8 installer or release packaging regression.
- 2026-07-04: Refreshed rerun and multi-agent strategy artifacts. Rerun strategy selects `fresh-env`, `auth-first-use`, `stream-state-machine`, `context-session`, `tool-skill`, and `v027-integrated-capabilities`, with a final full gate required before product-acceptance promotion.
- 2026-07-04: Fixed second-turn session rendering gaps by auto-attaching active runtime requests from `runtimeSnapshot.activeRequests` back into the current chat state, including running/queued phases and stream resume.
- 2026-07-04: Restored session summary titles by stripping role prefixes such as `User:`/`Assistant:` and preferring generated summaries over raw user prompt fallbacks.
- 2026-07-04: Added queued-message `引导` behavior. The button calls `guide_queue`, lets WebChannel observe or reinsert a durable queued payload without preempting the current run, and no longer pushes the user toward Run Center for this flow.
- 2026-07-04: Updated image-job observation around the measured single-image baseline: one image defaults to a 120s soft/stall baseline, batches scale by parallel waves, and provider polling/waiting/retry/rate-limit/fallback status events extend the observation lease instead of looking like a silent hang.
- 2026-07-04: Improved batch image-generation speed without changing the default model route. When no explicit `max_parallel` is supplied, multi-image jobs now default to two bounded lanes while single-image jobs stay on one lane. Both Web ImageJobs and native `imagegen.tasks` use the same `resolve_image_job_parallelism_policy`; provider/config/hard caps still clamp concurrency.
- 2026-07-04: Fixed session-share 404 routing at the Admin API/proxy boundary. Share URLs now honor public base URL and forwarded prefix settings, infer `/ecorex-agent/client` for production hosts, and nginx/Caddy examples route historical `/client/session-shares/*` links to Admin API.
- 2026-07-04: Updated `scripts/light-real-release-validation.py` and the production acceptance static checks to cover queue guidance, 120s image observation, default two-lane batch image generation, share-link routing, and installer runtime-slot fixes. Lightweight validation passed `158/158` in `docs/v0.2.8/artifacts/real-release-light-validation.json`.
- 2026-07-04: Rebuilt v0.2.8 after the queue/share/image-speed fixes and uploaded the new GitHub Release assets. Final hashes: Windows WebUI zip SHA256 `3B810B8C5112E5DE3F860F338A88C449E2EC76BFA9E27002B2EAB122E3B238BD`; macOS WebUI zip SHA256 `ACD25F27C26F61F8F5CCC9DD97F74009597F646503AD08DF7B998930078464F2`; Linux Web service tarball SHA256 `4F1E0997C081203EC4AD1B3D166F96105FB6FA47091772C5D3D216EA70BD783F`; public release zip SHA256 `F789DDF7C54A9796568361C6C0BB4105212743FA31373508B056A08D916F37DD`.
- 2026-07-04: Re-promoted production after the final v0.2.8 rebuild. `docs/v0.2.8/artifacts/production-deploy-online.json` reports `PASS`; live manifest is version `0.2.8`, first mirror is `ghproxy.net`, and live artifact sizes/hashes match the GitHub Release assets.
- 2026-07-04: Patched the actual production nginx site config `/etc/nginx/conf.d/ecorex-mvdcm.conf` for historical bare `/client/session-shares/*` links and reloaded nginx after `nginx -t` passed. Both `https://mvdcm.ecoremedia.net/client/session-shares/sh_AVwExibMPbUAJek1` and `/ecorex-agent/client/session-shares/sh_AVwExibMPbUAJek1` now return the shared-session HTML with HTTP 200 instead of nginx default 404.
- 2026-07-04: Re-ran the independent legacy online-upgrade smoke after pulling missing legacy packages back into `release-artifacts`. `docs/v0.2.8/artifacts/legacy-webui-online-upgrade.json` reports `PASS` `7/7`; `0.2.7.1` and `0.2.7.2` both receive the v0.2.8 update notification with Windows SHA256 `3B810B8C5112E5DE3F860F338A88C449E2EC76BFA9E27002B2EAB122E3B238BD` and upgrade online into versioned runtime slots.
- 2026-07-04: Added `docs/v0.2.8/artifacts/v028-final-release-summary.json` as the final compact handoff artifact for this release slice, including hashes, GitHub Release URL, production deployment status, share-link probes, and validation summary.

## Acceptance Anchors

- Sending a second message while a request is running returns `same_session.policy = "queue"` and `decision = "queued"`.
- The previous request is not cancelled and no cancelled SSE event is pushed for it.
- The queued request starts automatically after the current request releases its session lock.
- Queued message cards expose `引导`, which asks the runtime to re-observe/reinsert the queued payload without interrupting the current run.
- Long image generation emits observation/intervention events instead of silently waiting; default image-job observation is based on a 120s per-image baseline and scales by batch parallel waves.
- Batch image generation defaults to two bounded parallel lanes when the caller omits `max_parallel`; single-image generation stays one lane.
- Image-job intervention can be acknowledged by extending observation, backgrounding the observer, or stopping the job.
- Queued message payload survives process-local memory loss and can start from a fresh WebChannel instance while the RunLedger row is still queued.
- Queued message start is protected by a durable claim lease; a second WebChannel instance observing the same queued row does not double-start it.
- Session-share links generated on production hosts resolve under `/ecorex-agent/client/session-shares/*`, and historical bare `/client/session-shares/*` links are reverse-proxied instead of returning nginx 404.
