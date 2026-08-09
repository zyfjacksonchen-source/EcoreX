# v1 update-chain development log

## 2026-08-08 — e-Mate Enterprise 2.0.0 implementation goal

- Created `codex/e-mate-2.0.0` from exact source commit
  `f2490888e2d5a3bc760c62dd37c4abeeb35ebf87`. The accepted e-Mate WebUI is
  the unchanged `desktop` tree `60c68c95f58f2bd2df8f347827f05a34cda44226`;
  its last UI-changing commit is `1d2b21f512b077e103353cf4a8090bf4bb218a84`.
- Inspected the official CowAgent `2.1.5` tag in a temporary checkout. The
  existing Agent/model implementation is already a hardened descendant of
  that runtime, so the conversion reuses its Electron/PyInstaller/NSIS/DMG and
  updater boundaries without importing CowAgent's Renderer or creating a
  second Agent core.
- Split implementation into non-overlapping desktop, tenant-management, and
  legacy-migration/brand-gate workstreams. The backend remains the only source
  of task, reasoning, tool, checkpoint, audit, and terminal state; the current
  e-Mate WebUI remains projection-only.
- Set the product source version to `2.0.0`. Enterprise defaults remain
  `gpt-5.6-luna` with `max` reasoning and image upstream
  `gpt-image-2-pro` with bounded `gpt-image-2` fallback. Optional tenant models
  are Luna, Sol, DeepSeek, Gemini and Doubao; provider credentials are server
  secrets and are never copied into source, logs, Web payloads, or artifacts.
- Extended the existing Usage projection rather than introducing another
  audit surface. It now reconciles administrator counters with immutable
  provider usage facts and reports per-model totals; the focused Usage panel
  suite passes (`12 passed`).
- Platform signing is deliberately deferred for this release. Internal
  Ed25519/SHA integrity remains mandatory; Windows keeps the updater flow,
  while unsigned macOS is limited to verified update discovery and manual
  install until a Developer ID is supplied.

### User-confirmed e-Mate information architecture and closure gates

The following requirements were added to the 2.0.0 release scope on
2026-08-08 and are release gates, not placeholders:

- Remove the Home “创意中心” entry and replace it with “定时任务”. The entry,
  list, creation/edit controls, status, and deletion/cancellation actions must
  use Runtime scheduler facts and form a complete user path.
- Replace the settings modal with a Codex-style full-window settings workspace.
  Its navigation is grouped into:
  - “个人资料”: preserve the existing account/password update flow with
    immediate session enforcement, and support personal avatar changes.
  - “常规设置”: include the default conversation save path, permission
    controls, and the applicable existing general preferences.
  - “知识” and “记忆”: expose the existing Runtime knowledge and memory
    capabilities without importing the legacy CowAgent Renderer.
  - “能力中心/通道”: move the existing connection/channel capability here;
    do not maintain a second “连接” route.
- Add an external-connection icon below the composer. Its tooltip explains the
  action and its click target opens the channel view in Capability Center.
- Preserve the existing e-Mate share-link flow and its publish/copy/revoke
  lifecycle.
- A native task-completion notification is required after a Runtime-confirmed
  completed terminal event: Windows uses the system notification area and
  macOS uses Notification Center. Notifications are deduplicated across
  reconnect/replay, and selecting one focuses the corresponding conversation.
- No shipped e-Mate control, button, component, route, or module may be an
  empty placeholder or lead to a path that cannot complete. The final Browser
  and desktop acceptance runs include keyboard/hover/click checks for every
  visible navigation and primary/secondary action in these surfaces.

The accepted visual system remains the current `desktop/src/v1` tree recorded
above. These user-authorized functional changes are applied on that system and
must not reintroduce CowAgent layout, styles, assets, or a parallel Renderer.

### Permanent upstream-core freeze

The product owner established a hard rule for this release and all subsequent
development: absent explicit, change-specific approval, CowAgent's core
architecture, module design, and core implementation are frozen. Product work
must compose the existing core through its published boundaries. The normative
Chinese-language rule, allowed extension boundaries, exception procedure, and
release gate are recorded in `docs/e-mate-development-standards.md`.

### Image concurrency stability exploration

An independent, non-release-blocking measurement track will determine the
stable one-shot concurrency ceiling for both generation and image editing with
the enterprise image models. It must reuse the existing orchestrator and retry
policy without changing the frozen core. The benchmark increases concurrency
progressively and records throughput, p50/p95 usable-result latency, invalid
result rate, 429/5xx rate, primary-to-fallback rate, and visual usability. It
stops on the configured cost/error breaker; credentials and prompt/image
content are excluded from source, logs, and benchmark receipts. A recommended
default is accepted only when repeated runs remain below the error/latency
threshold, rather than from a single peak-throughput run.

A separate read-only capability study evaluates whether the unchanged CowAgent
core can use existing subagent orchestration to generate or edit multiple
images in one user operation. This is distinct from provider request
concurrency. The study must trace current subagent, attachment, image-tool,
result aggregation, cancellation, quota, and audit contracts and recommend a
composition-only path when one exists. It must not add a batch engine or alter
core architecture without separate explicit approval.

#### Image concurrency measurement result

- The existing envelope is eight image workers, eight active jobs per tenant,
  provider concurrency 16, a 64 MiB image ceiling, and a 4 GiB worker memory
  budget. The provider's conservative memory calculation is about 3 GiB at
  eight simultaneous jobs; twelve jobs would require about 4.5 GiB and is
  outside the declared envelope.
- A temporary, credential-free harness ran 126 real Orchestrator jobs through
  the SQLite worker and CAS in three waves at concurrency 1/2/4/6/8. All jobs
  completed with structurally valid results. At eight lanes, generation reached
  71.65 jobs/s with 132.4 ms p95 and editing reached 43.48 jobs/s with 198.8 ms
  p95. These figures measure local orchestration and result validation, not
  provider latency or visual quality.
- A 429 injection opened the durable same-scope breaker before the following
  eight jobs reached the provider, so the client did not amplify the upstream
  failure. No 5xx or fallback was synthesized during the success measurement.
- The evidence-backed internal ceiling is eight. Release guidance is generation
  concurrency 4 normally, 6 during a measured rollout, and 8 only as a short
  ceiling; editing is 4 normally and 6 as the short ceiling. Values above eight
  are prohibited without a new capacity envelope and a real 1/2/4/6/8 provider
  staircase.
- Enterprise upstream capacity remains unmeasured. The supplied image endpoint
  is not TLS-capable, while the production provider correctly rejects non-HTTPS
  credential transport. No bearer token or paid request was sent. A production
  claim requires an enterprise HTTPS Gateway or VPN proxy followed by repeated
  latency, 429/5xx, fallback, cost, and human visual-quality acceptance.

#### Multiple-image and subagent capability result

- The Managed Image adapter can execute ordered tasks for both generation and
  editing, but the frozen Core `imagegen` ToolSpec initially exposed only one
  instruction. An adapter-only batch implementation therefore was not
  reachable by a real Agent and could not be claimed as a product capability.
- Independent image tool calls in one Agent turn remain sequential, and the
  managed Gateway deliberately disables provider parallel tool calls. Multiple
  subagents can run mechanically, but their child context does not carry the
  complete organization, attachment authorization, tenant model policy, or
  typed Artifact result. The current bridge also retains only the first file.
  Therefore subagents are not a release-grade multiple-image result path.
- The durable Managed Image execution pool already supplies bounded
  concurrency, backpressure, idempotency, recovery, cancellation, and one
  Artifact per execution. It is the correct reusable substrate, but there is no
  parent batch facade, ordered Gallery projection, or complete image settlement
  into the existing Usage facts today.
- The shortest enterprise implementation is an e-Mate-owned `tasks[]` adapter
  that derives stable child idempotency keys, submits existing Managed Image
  jobs, returns ordered typed Artifact/model/fallback/usage facts, and settles
  every completed child exactly once into the existing Usage authority. It must
  reuse the current execution pool and provider/account limits, not add another
  scheduler or batch engine.
- The product owner subsequently chose the single-Agent bounded call over
  subagent fan-out and explicitly approved one Core exception: extend only the
  existing `imagegen` ToolSpec to accept an ordered 2–8 item `tasks[]` contract
  while preserving the single-image contract. The general schema engine,
  Gateway parallel-tool setting, Agent/subagent design and provider/worker
  state machines remain frozen. The exact scope is recorded in
  `docs/e-mate-development-standards.md`.

### 2.0.0 implementation checkpoint

- The native Bootstrap now owns the complete Runtime process tree. Unix
  forwards `SIGINT`/`SIGTERM` to the Python supervisor and waits; Windows uses
  a fail-closed `KILL_ON_JOB_CLOSE` Job Object. Unix signal tests passed and
  the Windows `amd64` test binary and Bootstrap cross-build both produced valid
  PE32+ executables.
- Electron observes only authenticated Runtime facts for task-completion
  notifications. Completion is deduplicated by thread/turn terminal identity;
  notification text does not expose the task title on a lock screen, and a
  click focuses e-Mate and opens the verified thread through the isolated
  preload bridge. The Electron/notification contract set passed `7/7`.
- A fresh native signed install reproduced a successful first full Runtime
  launch. A second diagnostic composed the same production settings against
  empty storage and returned both `REGISTER_OK` and `CREATE_OK`. The earlier
  one-off `runtime_registration` failure could not be reproduced after its
  state had advanced; signed package/source bytes were identical and the
  completed database showed all 108 tables and initialization facts. In
  accordance with the upstream-core freeze, no speculative core change was
  made. A future recurrence requires a temporary re-signed diagnostic overlay
  that records only an exception class and fixed stage before any fix.
- The user-authorized Renderer update removed Creative Center, added the
  Runtime-backed scheduled-task entry path, replaced settings with the
  full-workspace Profile/General/Knowledge/Memory layout, moved connections to
  Capability Center channels, added the Composer channel shortcut, preserved
  sharing, and removed the empty notification control. Avatar data is strictly
  PNG/JPEG/WebP, at most 512 KiB, clearly device-local, and never uploaded.
- Renderer verification at this checkpoint: TypeScript passed; focused product,
  timeline, Electron, and notification contracts passed `27/27`; the two
  Playwright user paths for scheduled tasks/channels and settings/avatar/output/
  memory/permission passed `2/2`; the content-addressed Web bundle gate passed
  with 32 chunks; source/product branding scanned 504 files with zero findings;
  the Electron brand gate and `git diff --check` passed.

### CowAgent 2.1.5 first-install environment decision

- A read-only comparison used CowAgent tag `2.1.5` at exact commit
  `e3ac1b952500f60934862c6bf0bd0de91b415ed8`. CowAgent builds a PyInstaller
  onedir backend on the build host, then runs it directly from Electron
  Resources at fixed port 9876 with writable state in `~/.cow`; end users do
  not install Python, pip, or a venv.
- The useful contracts are already preserved by e-Mate: no system Python,
  read-only application payload separated from user data, one loopback origin,
  desktop single instance/tray/restart, and offline packaged dependencies.
- CowAgent's concrete runtime manager is rejected for e-Mate. It inherits the
  ambient environment and login-shell PATH, can kill an unknown process merely
  for owning its port, stores provider keys in local `config.json`, downloads
  some dependencies on first use, and has no signed slots, known-good rollback,
  owner proof, or complete process-tree ownership. Reusing it would weaken the
  existing enterprise Secret, signature, rollback, and lifecycle boundaries.
- The one product-boundary behavior worth adopting is first-install visibility:
  CowAgent creates its window before starting the backend, while the current
  e-Mate shell waits for `backend.start()` and may show no window during a long
  cold install. e-Mate should show its own lightweight verification/install/
  start progress and retry/exit actions, while retaining the native Bootstrap
  and refusing to kill an unverified port owner.

This behavior has now been adopted without the Cow runtime manager: Electron
creates the e-Mate window immediately, shows a static CSP-constrained startup
surface, and offers retry/exit only after a fixed backend failure. The same
window loads the loopback Renderer only after native Bootstrap owner proof;
the shell never kills an unverified port owner. Desktop identity, update,
notification, startup/retry and brand contracts pass `7/7`.

### Multi-image Gallery and frozen Core boundary

- The e-Mate Renderer now groups consecutive image Artifact facts in one Turn
  into an ordered Gallery. It retains the existing pending animation, projects
  only persisted `pending/ready/failed/deleted` states, supports touch/trackpad
  scroll snap, previous/next and keyboard arrows, an item indicator, responsive
  layout, reduced motion, and the existing preview/open/download actions.
- The e-Mate Managed Image adapter has a tested bounded `tasks[]` composition:
  stable parent/index/task-hash child identities, ordered partial-failure facts,
  a shared concurrency semaphore, cancellation propagation, and reuse of the
  existing durable Journal, Image Job, Artifact, and recovery path. Production
  composition also settles a completed durable image `job_id` exactly once into
  the existing Usage authority.
- On 2026-08-08 the product owner explicitly authorized the narrow ToolSpec
  exception. The implementation keeps `parallel_tool_calls=false` and routes
  one Agent call through the existing ToolExecution/ImageExecutionPool,
  permission, timeout, recovery and audit path. The adapter derives stable
  batch/task identities, shares the existing bounded image concurrency,
  publishes one Artifact fact per successful slot, persists partial failures,
  and returns an ordered `image_gallery` result. The image Skill now instructs
  the Agent to use one native `tasks[]` call for 2–8 requested results and never
  delegate image production to subagents.
- Renderer grouping consumes only the persisted
  `content.image_batch.{batch_id,parent_execution_id,index,count,task_id}`
  identity, sorts by backend index and refuses incomplete, duplicated or mixed
  facts. Consecutive independent image calls are never merged. Completed
  batches retain touch/trackpad scroll, keyboard/arrow navigation, indicators
  and the existing preview/open/download actions. Failed slots are projected
  only after `artifact.image.batch_task_failed` facts agree with the durable
  `artifact.image.batch_settled` count; SSE displays them live and the existing
  paged `/events` replay restores them after refresh. They are read-only failure
  slots, never fabricated Artifact Items.

Focused verification at this checkpoint: six Gallery/failure state tests,
29 Timeline/product/Electron contracts, TypeScript, production Web build and
bundle gate, and one Playwright partial-batch refresh path passed. Backend
batch/Usage/Capability Pack/Worker integration passed 72 focused tests; the
Agent batch produces ordered partial results through the real execution pool.

### 小芯身份合同补充

- Runtime 的既有模型身份指令现明确区分产品与智能体：e-Mate 是 Agent
  产品，助手身份固定为“智能体小芯”；不得自称 e-Mate、Claude、Codex、
  ChatGPT 或底层模型。
- 中文默认使用专业、严谨的语气并称呼用户为“同学”，除非用户明确要求其他
  称呼。用户询问能力时，只介绍当前请求实际可用的分析、研究、写作、文件、
  代码、数据、图片、办公、工具、通道或定时任务能力，不把未安装或被策略禁止
  的能力宣传为可用。
- 身份指令继续由 Runtime 注入每轮模型请求，Renderer 不伪造模型身份。身份、
  风格、称呼和能力边界的 3 项 Worker 聚焦回归通过。

### e-Mate application icon

- Replaced the inherited small mark with an original e-Mate mascot icon: a
  compact orange/graphite robot, upward execution eyes, and lightning antenna.
  The design deliberately uses broad shapes rather than the prior eye dot grid
  and avoids competitor cat, lobster, wave, and letter marks.
- The source is a 1254 px RGBA PNG with transparent corners and no chroma fringe.
  Visual checks at 16/32/64 px passed on white, dark, and orange backgrounds.
  The desktop build now consumes a multi-resolution Windows ICO and macOS ICNS
  generated from the same source, preventing fallback to Electron branding.

## 2026-07-20 — v1.0.8 WebUI recovery kickoff

- Re-scoped the release goal to a WebUI-led v1.0.8 recovery while explicitly allowing the minimum runtime / migration / connector linkage needed for old-session restore, one-click connector activation, project-workspace binding, and post-update state correctness.
- Consolidated the execution plan into `.claude/plans/sparkling-whistling-wreath.md`, including:
  - v0.2.9.2 UI parity goals (model selector, orange thinking state, sidebar running affordances, artifact thumbs, jump-to-bottom)
  - tool/function-calling recovery boundaries (tool registry, tool-schema exposure, provider function-calling projection)
  - legacy active-session restore rules (canonical DB authority; no resurrection of deleted sessions)
  - connector one-click completion hardening and update-chain verification gates
- Began TDD with the first failing browser-level regression for the restored jump-to-bottom affordance:
  - Added Playwright GA spec `timeline exposes a persistent jump-to-latest control after scrolling away from the bottom`
  - The test uses the artifact scenario, injects scroll filler into `.ex-timeline`, scrolls away from bottom, expects a `回到底部` control, then expects it to scroll back to the latest position and disappear.
- No production code was changed yet for this slice; the goal is to watch this test fail first, then implement the minimal Timeline / style changes.

## 2026-07-10 — first implementation slice

Implemented within `ecorex/update`:

- Strict `ReleaseManifest` schema v1 with immutable release identity, required
  source priority, SHA-256 metadata, and detached Ed25519 envelopes.
- Fail-closed signature verifier interface and optional real `cryptography`
  Ed25519 adapter. Missing crypto support is an explicit error.
- Cross-process product lock using `msvcrt.locking` on Windows and `flock` on
  macOS/POSIX.
- Hash-chained, fsynced NDJSON install journal with partial-tail recovery.
- Durable coordinator states from `resolving` through `completed`, `rollback`,
  or `failed`; downloads are resumable and corrupt mirrors fall through in the
  signed priority order.
- Persistent first-install version/build/artifact pin, released only after the
  active slot is validated and registration is marked complete.
- Safe ZIP staging, side-by-side integrity-checked slots, atomic authoritative pointer
  record, inspectable `current`/`previous` labels, health-gated activation, and
  restoration of prior pointers on failure.
- No network side effects: the included fetcher reads explicit local source
  directories; production networking is an injected boundary.

Verification command:

```powershell
python -m pytest -q tests\v1\test_update_manifest.py tests\v1\test_update_coordinator.py tests\v1\test_update_durability.py
```

Result at handoff: `11 passed`.

## 2026-07-10 — trust and recovery hardening

- Recovery at `awaiting_user` now remains staged and never drains or activates.
- Trust, drain, migration, pin-recovery, and rollback authorization boundaries
  require explicit boolean success.
- Manifest, package, and extracted payload are revalidated at every persistent
  staging/activation boundary; terminal transaction payloads are removed.
- Slot pointers track three ordered known-good releases. Human-readable labels
  are non-authoritative and cannot turn a committed switch into a false failure.
- Slot links/reparse points, portable-name collisions, Windows device names,
  unsafe ZIP paths, and package sizes above 150 MiB are rejected.
- macOS executable modes are restored without privileged mode bits.
- Host/channel/version admission, authorized rollback and failed-first-install
  pin recovery are explicit coordinator policies.

Hardening verification result: `44 passed, 2 skipped, 1 warning` for all
`test_update_*.py` suites plus the single-version-source contract. The skips are
platform-specific: POSIX executable-mode verification and an actual symlink
test on a Windows host without symlink privilege; Windows reparse-attribute
handling remains covered by a privilege-free unit test.

Deferred integration points:

- HTTP/WSS control-plane transport and release push handling.
- Platform capability-pack wiring for `cryptography` and production public keys.
- Runtime process drain/restart implementation and live health endpoint probe.
- Production signing ceremony and HSM/KMS custody; the release library accepts
  only an injected signer and deliberately does not load private-key files.

## 2026-07-10 — deterministic release builder and signer

Implemented within `ecorex/release`:

- Deterministic core/bootstrap ZIP construction with stable order, timestamp,
  declared executable modes, normalized portable paths, source-change checks,
  and symlink/reparse/special-file rejection.
- Supported product matrix limited to Windows x64 and macOS arm64/x64, with
  mirror → GitHub Releases → CDN source order validated before packaging.
- Hard compressed limits of 150 MiB for core and 10 MiB for bootstrap, plus
  bounded member count and unpacked source size.
- A build digest derived from the sole `ecorex._version` source and complete
  artifact/file inventory.
- Real deterministic Ed25519 artifact and manifest signatures through an
  injected signer. `Ed25519MemorySigner` accepts only an existing in-memory key;
  signer failures are redacted and private keys are never serialized.
- Atomic immutable release-directory publication containing the signed
  manifest, release metadata, artifacts, and deterministic CycloneDX 1.5 SBOM.
- Reserved output-name protection so a custom artifact cannot replace the
  manifest, metadata, or SBOM.

Focused verification at implementation time:

```powershell
$files = Get-ChildItem tests/v1 -Filter 'test_release_builder*.py' | % FullName
python -m pytest -q -p no:cacheprovider $files
```

Result: `22 passed, 1 skipped`. The skip is the actual symlink test when the
Windows host lacks symlink privilege; Windows reparse-attribute rejection also
has a privilege-free unit test.

Complete v1 regression at handoff: `227 passed, 4 skipped, 1 warning`. The
warning is Starlette's upstream `python_multipart` pending-deprecation notice;
the other skips are platform/privilege-specific tests.

## 2026-07-10 — signed React dist and Server release contract

- Added the single `WebBundleBuildInput` release input and reserved independent
  `web-manifest.json` / `web-manifest` artifact identity.
- Reused Server's `WebBundleManifest` and `WebFileRecord` contract without a
  Server-to-release dependency. File records, entrypoint and domain-separated
  bundle digest now match the production Server loader exactly.
- Added a non-circular build identity: the release build digest covers the
  complete unsigned Web file inventory; the signed Web JSON is then hashed and
  signed as a ReleaseArtifact under the same release/version/build identity.
- Added a production dist gate for exact root layout, SHA-256 asset names,
  reachability-based allowlisting, missing lazy dependencies, hidden/extra
  files, source-like suffixes, symlink/reparse/special entries, Runtime marker,
  CSP-incompatible inline code, and legacy bundle/overlay references/content.
- The Web tree is scanned again before publication. Release metadata, artifact
  paths and CycloneDX SBOM now include the Web manifest and allowlisted files.
- Confirmed the current repository `desktop/dist` fails closed on its
  non-SHA-256 Vite asset names; Web post-build transformation remains required.

Verification at this handoff:

```powershell
$web = Get-ChildItem tests/v1 -Filter 'test_release_web_bundle*.py' | % FullName
python -m pytest -q -p no:cacheprovider $web
python -m pytest -q -p no:cacheprovider tests/v1
```

- Web release tests: `19 passed, 1 skipped, 1 warning`.
- Release/Server/Updater contract set: `74 passed, 3 skipped, 1 warning`.
- Complete v1 regression: `270 passed, 5 skipped, 1 warning`.

The focused skip is the real Windows symlink case on a host without symlink
privilege; reparse-bit handling has a privilege-free test. The warning remains
Starlette's upstream `python_multipart` pending-deprecation notice.

## 2026-07-14 - hosted CI portability and output-root identity

- The first real GitHub-hosted matrix passed macOS arm64/x64 and identified
  deterministic Ubuntu and Windows gaps rather than release-product failures.
- Windows native discovery now accepts either standard VS 2022 installation
  root while preserving manifest digest, Authenticode, library and exact-one
  toolchain fences.
- Universal Runtime license accounting remains complete when a lock marker is
  inactive on the gate host; unknown missing packages still fail closed.
- CI installs the reviewed npm closure before Python release tests invoke Vite.
- Cross-platform tests bind to staged Candidate identity and resolved regular
  executables, removing host-path and host-platform coupling.
- POSIX output locations are descriptor-pinned for the Runtime lifetime so
  unlink/recreate inode reuse cannot bypass a frozen output policy.
- Verification: complete Python v1 suite `1,916 passed, 17 skipped`; Web
  contracts `162 passed`; npm vulnerabilities `0`; all static, schema,
  reproducibility, source and supply-chain gates pass. Remote revalidation is
  required before any promotion claim.

## 2026-07-14 - reviewed Windows runner identity

- The second hosted matrix passed Ubuntu quality and both macOS architectures,
  then failed closed before native compilation because GitHub's
  current `windows-latest` image no longer contains Visual Studio 2022.
- GitHub moved `windows-latest`/`windows-2025` to Visual Studio 2026 in June
  2026. The EcoreX native manifest remains bound to reviewed MSVC 14.44/19.44
  and Windows SDK 10.0.26100.0 rather than accepting a compatibility component.
- Read-only compatibility CI selects `windows-2022`; protected platform staging
  targets the isolated `ecorex-platform-windows` exact-toolchain Runner.
- The two-root VS discovery fix remains, along with exact digests,
  Authenticode, reparse rejection and the exact-one-toolchain fence.
- Local runner contracts pass 19 tests; workflow/YAML/JSON, dependency,
  reproducibility, source and supply-chain gates pass for the fixed labels.
- A third exact-commit remote matrix is required before any promotion claim.

## 2026-07-14 - hosted compatibility and release authority are separated

- GitHub's fixed OS label still points at a weekly mutable image; the current
  `windows-2022` compiler hash differs from the reviewed release manifest even
  though the VS/MSVC family is correct.
- CI therefore has an explicit GitHub `win22`-only compatibility mode. It pins
  sources, MSVC 14.44 and SDK layout, requires valid Microsoft Authenticode,
  locks tools/libraries for the build and records observed hashes.
- Its receipt is `github-hosted-ci-compatibility`; the production stager only
  accepts `caller-pinned`, so CI output cannot become a Candidate.
- Protected Windows staging targets a fourth, non-privileged isolated Runner
  labelled `ecorex-platform-windows`. Repository readiness forbids overlap with
  signing, live provider/CDP acceptance and publication hosts.
- Local exact-mode regression passed 90 executable tests with two platform
  skips; the only initial miss was corrected static text. Focused contracts then
  passed 57 tests with one skip, and a simulated GitHub compatibility build
  compiled and passed its native Runtime probe.
- Complete current-source v1 regression passed 1,916 tests with 17 explicit
  environment/platform skips and zero failures in 761.34 seconds. Five warnings
  are pre-existing upstream deprecations.
- Branch governance now names the actual `Windows x64 compatibility` Job
  context, eliminating a status-check label that GitHub could never satisfy.
- Final supply-chain preflight covers 23 Runtime packages, 282 npm packages and
  464 production files; inventory `45083146...4b8528`, ignored report
  `a087fbc2...3887ad`.
- A fresh read-only repository audit has 17 blockers: Actions 3, branch 1,
  Environments 6, isolated Runners 4 and protected workflows 3. OAuth workflow
  scope and active v1 CI are confirmed; no governance mutation was made.
- The fourth hosted Windows Job passed all 150 Runtime/native tests, then found a
  separate checkout-byte gap: generated TypeScript was CRLF on Windows because
  `.ts/.tsx` lacked an LF rule. Both extensions are now fixed to LF and enforced
  by the reproducibility gate; generated contract check and typecheck pass.
- Updated supply-chain preflight stays green for 23 Runtime, 282 npm and 464
  production files; inventory `cfb99101...d0e00`, report `9084448e...157a2`.
- Hosted run `29296609455` passed Ubuntu quality, Windows x64, both macOS
  architectures and final cross-runner byte stability on exact commit
  `a70d65c3`. No protected Candidate, publication or rollout was triggered.

## 2026-07-14 - recovered live provider diagnostic and final browser rerun

- Hosted run `29296947260` revalidated the final Draft PR head
  `a11dbd884054130ecec145c0a2625ec4eb2c4cca`: Ubuntu quality, Windows x64,
  macOS arm64/x64 and cross-runner byte stability all passed.
- A redaction-safe direct-upstream diagnostic returned HTTP 200 for the
  `gpt-5.6-sol` catalog and one medium-reasoning completion under the fixed
  272,000-token compaction threshold. No endpoint, credential, response text
  or catalog contents were recorded.
- Image 2 passed a single-flight admission, then a hard four-worker/no-retry
  run completed 4/4 unique images with zero 5xx. A rectangle-mask retouch
  produced a new revision with `0.991565` non-target similarity and passed
  visual inspection.
- The content-addressed Web build was exercised through the in-app browser at
  1440x900. Model selection before the first message, independent image mode,
  bottom-anchored active Composer, continuous reasoning replacement,
  fit-first preview, structured retouch, settings, extension/connector
  management, task-ID continuation and the loopback public-share renderer all
  passed with zero browser console warnings or errors.
- Durable redacted measurements are in
  `docs/v1.0/evidence/live-provider-local-diagnostic-2026-07-14.json`.
  This is diagnostic evidence only: it is not bound to an immutable Candidate,
  managed Gateway/device session or protected acceptance Runner. The live
  repository audit still has 17 blockers, so no publication or rollout was
  attempted.

## 2026-07-15 - reviewed Node 24 workflow dependency closure

- Upgraded every v1 official Action to a verified Node 24 release and retained
  full commit-SHA pins: checkout 7.0.0, setup-python 6.3.0, setup-node 7.0.0,
  setup-go 6.4.0, upload-artifact 7.0.1 and download-artifact 8.0.1.
- Added the canonical `requirements/locks/github-actions.json` authority and
  bound its six identities plus minimum Actions Runner 2.327.1 into dependency,
  source and cross-runner reproducibility gates.
- The workflow gate now inventories all YAML workflows, allows only the four
  v1 contracts, exact-matches Action SHA/release, and requires checkout
  `persist-credentials: false` everywhere.
- Removed both inherited CowAgent Docker publishers and made their paths
  permanent legacy-cutoff violations.
- The first full test exposed a duplicate outer process-wall threshold at the
  exact 3.5-second boundary. Functional shutdown remained under its 0.8-second
  budget and the child process remained under the independent 4-second timeout.
  Removing the cold-import-coupled duplicate assertion preserved both actual
  deadlines; the exact test then passed five consecutive runs.
- Final local evidence: 1,922 Python tests pass with 17 explicit skips in
  1,255.82 seconds; Web audit/typecheck/164 tests/build pass; all static gates
  and 656 admitted files pass. Supply-chain preflight covers 23 Runtime, 282 npm
  and 467 production files with inventory `e488a5e9...0db67edb`.
- This is local pre-push evidence only. Hosted multi-platform execution,
  repository governance, protected Candidate, publication and rollout remain
  separate gates.
- Hosted run `29382330122` independently passed Ubuntu quality, Windows x64,
  macOS arm64/x64 and final cross-runner byte stability on exact source commit
  `fd05f42413b2563e34f15421e58991248f3bdee2`. All five check runs have zero
  annotations and the full logs contain no Node 20/deprecated-Action warning.
- PR #2 is Draft, CLEAN and MERGEABLE. The read-only live audit remains the
  same 17-blocker receipt (`d9eb1f47...38a2c8`); no protected Candidate,
  governance mutation, publication or rollout was attempted.

## 2026-07-15 - generated secondary Runtime response contracts

- Replaced loose dictionary responses for Memory, Output, migration quarantine
  and System with nine strict Pydantic models across twelve JSON routes.
- Generated a separate settings Runtime contract from the 36-contract canonical
  schema and validate every affected response before React state admission.
- Kept low-frequency validation out of initial loading. Eager loading failed the
  475 KiB gate and the first lazy attempt exposed a dependency cycle; the final
  independent chunk is 11.73 KiB raw / 3.59 KiB gzip.
- Replaced two load-sensitive timing sleeps with ready/start and second-call
  Event synchronization. Each corrected boundary passed 10 consecutive runs;
  production shutdown and maintenance deadlines were unchanged.
- Final local evidence: 1,926 Python tests pass with 17 explicit skips;
  TypeScript and 167 Web tests pass; production emits 20 assets / 19 chunks at
  474.99 KiB raw initial JavaScript. All static/byte gates and 658 source files
  pass. Supply-chain preflight covers 23 Runtime, 282 npm and 468 production
  files with inventory `a7b2ff6f...31a8d5a`.
- This is local evidence only. Exact-source hosted CI, repository governance,
  protected Candidate, publication and rollout remain separate gates.
- Hosted run `29390253811` independently passed Ubuntu quality, Windows x64,
  macOS arm64/x64 and Cross-runner byte stability on exact source
  `ee8a7f8cc77830b66358af3acc9206f95cb5923b`; all five checks have zero
  annotations and zero Node 20/deprecated-Action log warnings.
- PR #2 remains Draft, CLEAN and MERGEABLE. The read-only repository audit is
  byte-identical at 17 blockers (`d9eb1f47...38a2c8`, action `none`), so no
  protected Candidate, governance mutation, publication or rollout occurred.

## 2026-07-20 - v1.0.8 Turn-model availability convergence

- Fixed the runtime invocation overlay so it retains the immutable model
  modality and capability selection captured by the admitted Turn while it
  still re-checks live packs, connectors, network state and permission policy.
- This prevents a model-enabled image capability from being falsely denied
  during execution merely because the shared live availability provider has no
  single global model selection.
- Added a focused regression that verifies `imagegen` remains invocable for a
  Turn that selected the signed image-generation/edit model, while a genuine
  live availability loss still tightens the policy and blocks execution.

## 2026-08-07 - v1.0.0 bundled-capability authority decision

- A source and Candidate audit confirmed that v1 has one intended capability
  authority chain: Core-owned `ToolSpec` and routing, `ecorex.pack_catalog`, the
  signed `ReleaseManifest`/Pack sidecars, then the frozen availability snapshot.
  `runtime-packs/` remains v0.3 compatibility data and must not participate in
  v1 admission, routing or release decisions.
- The v1 installer deliberately activates Core and the exact six-Pack host set
  as one rollback unit and rejects arbitrary partial sets. This is retained for
  v1.0.0 because the release requirement is that every advertised built-in
  capability is actually executable; changing the already verified Candidate
  to a partial or task-time-installed set would recreate the observed
  read/write/shell and capability-visibility failure class.
- The retained product-owned dependency closure is not permission for an agent
  to run npm/pip against the user's machine. OCR and browser/CDP remain signed,
  versioned product Packs. Feishu/Lark remains a managed Connector with OAuth
  state outside the release and is not an executable Pack. Image generation
  keeps one Core planner route plus the tiny signed image adapter and one
  just-in-time workflow Skill; there is no second Skill-owned intent router.
- The size cost is concentrated in browser and OCR (about 121 MB and 96 MB for
  the macOS arm64 Pack archives); channels, image and sandbox together are only
  about 11 KB. If a later release needs a thin online default, it must introduce
  an explicit signed online profile and an atomic, cached, rollback-capable Pack
  overlay before omitting heavy Packs. The v1.0.0 `full_offline` Candidate and
  its exact bytes remain unchanged.
- The manual v1.0.0 builder's digest-pinned v0.3.2 native/Python inputs are a
  build-time migration bridge only. v1.0.0 artifacts and lock receipts become
  the next release base; the compatibility bridge is not a continuing product
  fact source.

## 2026-08-07 - CowAgent WebUI review and fast manual release lane

- Reviewed the official CowAgent repository at immutable commit
  `46a9b8762443d6adb0b020326b11854b1588c44a`. Its source-WebUI update command is
  fast because it stops the service, performs `git pull`, installs Python
  requirements and restarts. That assumes user-owned Git/Python/pip and has no
  signed release manifest, atomic product slot or verified rollback, so this
  code path is not copied into e-Mate.
- Retained CowAgent's useful lifecycle decisions without importing its Electron
  shell: immutable assets are completed before a mutable release/feed switch;
  publishing and promotion are separate; a persistent browser dependency may
  prefer an acceptable system Chrome/Edge and otherwise install one pinned
  managed engine; a large optional SDK may be reduced to its reviewed closure,
  SHA-256 pinned, versioned and atomically installed with mirror fallback.
- e-Mate v1.0.0 remains the one full-offline baseline. Browser/CDP and OCR stay
  signed Packs so every advertised built-in works immediately. Feishu remains
  a Connector rather than a CLI Pack; a later reduced connector closure may
  use the pinned/atomic CowAgent pattern. Agents never run npm or pip on the
  user's machine.
- Patch releases do not need a second updater. The existing signed CoreDelta,
  exact-base binding, verified download cache, full-Core fallback, slot
  checkpoint and rollback code are the online update lane. The manual direct
  Candidate command already accepts `--delta-base-release-dir`; the previous
  accepted release directory is therefore a required retained input for patch
  builds. A heavy full baseline is rebuilt only when Bootstrap, dependency
  lock, platform Runtime or Pack bytes actually change.
- The current v1 Pack manifest intentionally uses release-versioned canonical
  filenames. Its content-addressed download cache already avoids re-downloading
  an unchanged Pack on the client, but GitHub still needs the versioned asset
  for each signed manifest. Changing Pack filenames or omitting release assets
  would be a manifest compatibility change, not a safe v1.0.0 optimization;
  independent content-addressed Pack publication is deferred until an older
  Runtime can consume the new contract.
- The shared GitHub publisher now resumes a batch from one remote Draft
  inventory instead of re-listing the Draft before every asset. Local bytes and
  SHA-256 identities are checked before the first network mutation, matching
  remote assets are reused, conflicts remain fail-closed, and only missing
  assets upload. Manual publication still leaves the release as Draft until
  CDN/readback and acceptance are complete; no CI or tag-triggered build is
  introduced.
- User-side reuse is also digest based, not version-name based. Every Core,
  delta, Pack archive and Pack sidecar enters the verified download cache only
  after manifest/artifact signature, size and SHA-256 checks. A later release
  that carries an unchanged Pack archive therefore performs no network request
  for that archive even though its release-scoped sidecar and filename change.
- A verified cache hit now prefers native APFS copy-on-write cloning and always
  verifies the independently addressed result; unsupported filesystems retain
  the bounded verified-copy fallback. Pack transaction files are then moved,
  rather than copied, into the still-private candidate slot. This removes both
  redundant Pack network transfer and the second 120 MB browser / 96 MB OCR
  local copy while preserving independent inodes, signed validation, atomic
  activation and rollback isolation. Hard links and shared writable payloads
  remain forbidden.
- Core updates reuse unchanged compressed content through the existing signed,
  exact-base CoreDelta and reconstruct/verify the final Core before staging.
  Platform Runtime or dependency-lock changes still require a new full Core;
  ordinary product/Web patches do not reinstall or fetch unchanged Capability
  Pack environments. A future physical split of the portable interpreter from
  product code is only justified if measured Core reconstruction remains the
  dominant patch cost; it is not a prerequisite for the v1.0.0 baseline.
- The resulting manual order is fixed: build/verify immutable Candidate once;
  stage immutable GitHub/CDN assets with resumable digest checks; perform public
  readback; run the real local-user browser matrix; then atomically switch the
  signed update pointer as the last operation. A failed stage never advertises
  incomplete bytes, and retry resumes rather than rebuilding or re-uploading
  verified work.

Focused verification for the new shared batch boundary:

```text
python -m pytest -q -p no:cacheprovider tests/v1/test_github_release_publisher.py
12 passed, 1 warning
```

The warning is Starlette's existing `python_multipart` pending deprecation.

User-side cache/Pack focused verification:

```text
python -m pytest -q -p no:cacheprovider \
  tests/v1/test_update_download_cache.py tests/v1/test_atomic_pack_install.py
18 passed, 1 warning
```

The macOS runtime probe confirmed APFS clone success, identical bytes and an
independent destination inode.

The production/update authority is now explicitly singular: the stable install
and update command contract is the only normative fact source. It freezes the
manual fast lane as a new signed Core delta, SHA-256 reuse of unchanged Packs,
complete immutable-resource upload and public readback, exact-byte acceptance,
and one final atomic stable-pointer switch. A small executable contract test
prevents release documentation from silently reintroducing a second order or
mutable authority.

## 2026-08-07 - side-by-side Runtime acceptance and short cutover

- Added a manual blue-green acceptance lane for an authenticated local release.
  The installed Runtime keeps its `127.0.0.1:8765` process and launch lock while
  the candidate runs from a release/build-addressed preview root on an
  independent loopback port (default `18765`). On macOS the Bootstrap asks the
  platform opener for a new browser instance so the candidate is not confused
  with the current WebUI tab.
- The candidate receives a bounded, consistent checkpoint of only `state/` and
  `workspace/`. SQLite uses the existing online-backup boundary; ordinary files
  prefer APFS copy-on-write and retain a verified independent-copy fallback.
  Links, special files, overlapping data roots, more than 100,000 files and more
  than 20 GiB fail closed. The previous valid preview checkpoint is preserved
  when preparation fails.
- Preview mode is Bootstrap-owned and cannot be enabled by an inherited Runtime
  environment variable. The WebUI displays a persistent candidate banner. Login,
  update activation, Connector/MCP OAuth mutations, external sharing and host
  open/reveal actions return a controlled `409`; managed session refresh,
  registration callbacks, update polling and share publishing are absent. Chat,
  image generation and local tools remain usable against the isolated database
  and workspace. Saved absolute project roots from the live database are not
  admitted into the preview authority.
- The exact verified candidate is also staged into the live install root as an
  `awaiting_user` transaction without changing the active slot pointer or
  desktop entry. After acceptance, the ordinary local-release command reuses
  that transaction and performs only the launch-lock handoff and pointer switch.
  A different manifest cannot consume the prepared transaction. Existing slot
  health/rollback logic remains the sole cutover authority.
- The ordinary manual local-release path now verifies, copies and expands signed
  artifacts before waiting for the current Runtime launch lock. This removes the
  long service outage from packaging work without adding a second installer or
  updater.

Focused verification at implementation time:

```text
Python acceptance/install/config/API slice: 88 passed, 10 skipped
Snapshot overlap and checkpoint slice: 9 passed
WebUI TypeScript + Runtime contracts: 225 passed
Go Bootstrap: go test ./... passed
Ruff and git diff checks: passed
```

The platform opener requests a distinct browser instance on macOS. Windows keeps
the normal default-browser opener for now; Runtime roots, ports and origin storage
are still independent there. No public update pointer or production install was
changed by this implementation slice.

### Real dual-port smoke test and credential boundary correction

- The signed macOS candidate from commit `19bd299` was started on
  `127.0.0.1:18765` while the installed 0.3.2 Runtime remained active on
  `127.0.0.1:8765`. The live slot pointer did not change, the preview checkpoint
  completed, and the browser rendered the persistent candidate banner with no
  console warnings or errors. The candidate was stopped immediately when the
  user requested that no further macOS Keychain prompts appear; this was a smoke
  test, not final acceptance.
- That run exposed an implicit credential-boundary defect: constructing the
  low-level Runtime API without a vault silently discovered the production OS
  vault, and acceptance-preview composition also invoked the platform-vault
  factory. Direct/test Runtime composition now uses an in-memory vault when
  unmanaged and a rejecting vault when managed. Normal product composition is
  unchanged and must explicitly inject its platform vault.
- Acceptance preview always receives a process-local in-memory vault and never
  calls the platform-vault factory. It therefore cannot read or write macOS
  Keychain credentials and begins unauthenticated unless a future explicit,
  ephemeral preview credential channel is provided. Avoiding surprise OS
  credential prompts takes precedence over copying the live login state.
- Regression coverage asserts both boundaries: the direct Runtime owns an
  in-memory vault, and the preview product still composes when its supplied
  platform-vault factory is a function that must never be called.

Focused verification after the correction:

```text
Runtime composition, agent worker, preview entrypoint and managed product: 4 passed
```

## 2026-08-07 - Stable CowAgent-derived command contract and user-driven update

- Reviewed CowAgent's `run.sh`, Click entrypoint and process commands at commit
  `46a9b8762443d6adb0b020326b11854b1588c44a`. Kept its useful fixed lifecycle
  vocabulary, visible progress, module-entry fallback and self-restart ordering.
  Rejected its source checkout installer, `git pull`, runtime dependency install
  and global Git proxy mutation because they make host tools mutable product
  dependencies and cannot provide signed exact-byte rollback.
- Froze the e-Mate v1 surface in
  `release/v1/CLI_AND_MANUAL_UPDATE_CONTRACT.md`. Ordinary users have one
  extracted platform installer entry and the WebUI; they do not install a CLI,
  Python, npm, Playwright or Pack dependencies. Bootstrap remains the sole
  process/install authority.
- Removed `cli/VERSION` from the v1 release preflight. `ecorex/_version.py` is
  the product version authority; desktop package/lock versions are required
  projections. The excluded legacy `cli/` tree is historical source only and
  cannot define v1 commands, packaging or releases.
- Product update composition now stops after signed discovery and reports
  `available`. Download/staging starts only after the user's banner/settings
  action, using the existing endpoint and signed CoreDelta/full fallback. The
  banner shows an honest indeterminate progress bar because the protocol has no
  byte counter; no fake percentage is projected.
- The user action reserves the new-version window synchronously, then downloads,
  verifies, stages and activates. After target health is observed it navigates
  that window to the updated Runtime; popup blocking falls back to the current
  document. Failure closes the placeholder window, preserves the active slot
  and shows a controlled error.
- Acceptance preview no longer touches the OS credential vault. Its copied
  SQLite checkpoint excludes only derived observability tables that are bound
  to the live vault key, while source state, conversations, projects and
  artifacts remain untouched. Preview observability uses its isolated local
  key, so no macOS Keychain prompt is required.

Focused verification at this checkpoint:

```text
Ruff: passed
Python Runtime/update/preview slice: 103 passed, 10 skipped
TypeScript typecheck: passed
Update state/handoff tests: 7 passed
Update WebUI contract tests: 2 passed
```

Final local gate rerun after the user-driven update and non-platform vault
fixes:

```text
Python 3.11.9 full suite: 2703 passed, 55 skipped
Web Runtime/unit/contract suite: 227 passed
Playwright Chromium E2E: 51 passed
Go Bootstrap: go test ./... passed
Production Web build and signed-bundle structural gate: passed
Ruff, compile, TypeScript and generated Runtime contracts: passed
Message/tool continuation/reasoning backend facts: 93 passed
Web durable-event reducer/timeline/error disclosure: 28 passed
```

The 55 Python skips are existing explicit platform/privilege/external-service
conditions. Seven warnings are upstream deprecations plus the intentional
duplicate-member tamper fixture. The first full run exposed only missing test
runner commands (`pip` and `node`); installing the digest-locked Bootstrap
packaging tools into the disposable Python 3.11 tree and supplying the pinned
Node path produced the clean full rerun above. No user or system environment was
modified.

## 2026-08-07 - Local release evidence contract repaired

- Exact-commit candidate preview exposed a release-contract conflict rather
  than the reported storage failure: the release builder always emits
  `release-metadata.json` and `sbom.cdx.json`, while Bootstrap's authenticated
  local-release inventory rejected every file not listed as a signed runtime
  artifact. The same directory produced by the builder therefore could not be
  consumed by `--local-release` or `--preview-local-release`.
- Bootstrap now admits only those two fixed evidence names in addition to the
  signed manifest/artifacts. Before admission it exact-decodes release metadata,
  binds its identity, manifest digest/signature and ordered artifact records to
  the already verified manifest, and recomputes the bounded SBOM digest. Any
  other extra file, link, directory, changed artifact or changed evidence file
  remains fail-closed.
- The manual builder's full publication directory contains both evidence files,
  while the downloadable platform ZIP deliberately contains neither and relies
  on the signed manifest/artifacts alone. Bootstrap accepts both fixed forms,
  validates evidence when both files are present, and rejects a partial pair.
- The controlled error mapper now classifies an invalid local-release inventory
  as a verification failure instead of claiming disk space or directory
  permissions are unavailable.
- The next executable preview reached the data-checkpoint stage and exposed a
  second strict-contract drift: Python correctly returned the redacted
  `observability_rows_removed` summary introduced by the no-Keychain preview,
  but Bootstrap's exact receipt schema had not projected it. Bootstrap now
  accepts and validates non-empty safe table/count entries, and reports an
  acceptance-checkpoint failure as such rather than as a generic Runtime start
  failure.
- Full Product Runtime startup then found a copied managed-session pointer whose
  credential was intentionally absent from the preview's in-memory vault. The
  checkpoint now advances and detaches only the copied active/pending managed
  session pointers, records `managed_session_cleared`, and leaves the live
  database unchanged. Conversations, projects, artifacts and other user state
  remain in the preview; authentication is explicitly unavailable there.
- The underlying active-session read path also leaked a vault `KeyError` when
  an OS credential had disappeared while its durable session record remained.
  Active reads now project that condition as controlled `SessionUnavailable`;
  the pending-install recovery path alone retains missing-material detection so
  it can abort the incomplete two-phase install deterministically.

Focused verification at this checkpoint:

```text
Go Bootstrap: go test -mod=readonly ./... passed
Static local-release fail-closed contract: 1 passed
Preview/session focused Python slice: 18 passed
Ruff focused check: passed
Diff whitespace check: passed
```

# 2026-08-07 — 真实浏览器验收：隔离登录可跨 Runtime 重启

- 真实用户登录首次命中 `acceptance_preview_external_mutation_blocked`，确认预览策略把密码登录与会修改正式账号/外部服务的操作一起拦截。
- 仅放行精确的 `POST /api/v1/session/login`；设备登录、更新、连接器、MCP、分享与本机打开/定位仍保持预览阻断。
- 复用现有 AES-GCM 依赖增加验收专用加密凭据仓：密钥仅由 Bootstrap 进程生成并跨受管 Runtime 重启传递，密文只写隔离预览目录，结束时清理；不访问 macOS 钥匙串，也不写正式数据目录。
- 增加密文不含明文、跨实例读取、错误密钥失败、Bootstrap 所有权、预览放行边界和前端受控中文错误映射检查。

本轮定向验证：

```text
受影响 Python 回归：120 passed, 10 skipped
验收预览 Web 契约：1 passed
用户错误文案单测：4 passed
Ruff、compileall、diff check、变更生产切片 secret scan：passed
```

真实外层 macOS 用户包的登录—重启验收随后暴露第二个边界：首进程以未登录占位账号创建的审计密文，在登录后的账号绑定进程中被当作另一正式账号的密文，因而在 `runtime_registration` 阶段按设计失败。验收预览现在使用一个 Bootstrap 生命周期固定的审计引用；正式模式仍按账号隔离。该引用的密钥仍只存在于验收加密凭据仓中，预览结束即失效。

# 2026-08-08 — 真实会话续接：Gateway 契约与验收令牌刷新

- 浏览器真实登录、受管 Runtime 重启和候选加密凭据恢复通过；Luna/max 的普通消息产生连续、无重复事件，并按 Runtime 指令回答为 e-Mate。
- 第一条真实消息先暴露生产 Gateway 与 v1 Runtime 的契约漂移：Gateway 的 `ModelGatewayRequest` 尚未接收 `instructions`，Nginx 回读为 HTTP 422。生产侧先保留原始三文件和不可变源目录，再以独立目标目录应用最小三点兼容补丁（请求字段、Responses 转发、Chat developer message），重启后健康检查、模型目录和契约回读通过。该临时补丁只用于解除验收阻断，最终发布前仍须由正式 v1 云制品替换。
- 工具调用首轮通过后，续接轮在访问令牌到期时返回 401。根因不是工具实现，而是验收预览把独立登录会话的令牌刷新与更新、分享等业务写入一起禁用了。验收预览现在保留会话刷新服务和 supervisor；更新、分享、OAuth、MCP 外部写入、正式凭据仓与正式数据库仍保持隔离。
- 已暂停生产稳定线中不可下载的 1.0.7 rollout；操作前在线 SQLite 备份、操作后 `rollout.paused` 信号、无 active stable rollout 和正式 0.3.2 客户端回到 `idle` 均已回读。

本轮定向验证：

```text
会话刷新与验收预览回归：5 passed
产品 Runtime/预览/设备会话切片：88 passed, 10 skipped
Ruff、diff check：passed
真实 Luna 回复：completed；31 个事件连续且无重复
生产 Gateway：ready；v1 instructions 契约已回读
```

# 2026-08-08 — 真实系统能力链：即时治理使用组合后可用性

- 刷新后的新候选版用真实账户重新登录成功，托管会话跨受管 Runtime
  重启恢复正常；全量 Python 门禁为 `2707 passed, 55 skipped`，供应链
  发布校验通过。
- 首条 `tool_search → shell → read` 浏览器任务没有再出现令牌 401，而是
  暴露了另一个共享根因：Turn 规划已经清除了 Core 在低层 Pack 构建期
  产生的 `verified_handler_not_installed` 旧事实，但即时调用治理重新读取
  原始可用性时漏掉了同一层规范化，导致已经投影给模型的 `tool_search`
  和 `task_list` 在执行前被拒绝。
- `current_invocation_availability()` 现在与 Turn 规划复用同一个已绑定处理器
  规范化步骤；没有改变管理员拒绝、离线、沙箱或外部能力不可用事实。
  契约测试同时覆盖连接器 Core handler 与 `tool_search`，防止规划和执行
  再次分叉。
- 本次 shell 最终错误 `shell_cwd_outside_workspace` 来自验收提示指定了
  `/tmp`，它正确证明沙箱未被“完全访问”绕过；后续真实复测改为用户选择
  的项目工作区，不以放宽工作区边界掩盖问题。

本轮定向验证：

```text
Runtime composition：17 passed
Ruff focused check、diff check：passed
真实失败终态：shell_cwd_outside_workspace（受控沙箱边界）
```

# 2026-08-08 — 小芯身份、B5 思考动效与透明头像

- 同一真实会话在系统能力任务完成后继续追问，模型无需再次调用工具即可准确
  回答先前创建文件的路径和内容；回复终态正常显示，证明上下文续接与最终文本
  投影有效。
- 真实执行 `skill_search → skill_read`，过程区依次显示两个已完成步骤并返回
  Skill 内容，确认 Skill Hub 的检索、读取和模型续答链路可用。
- 浏览器计算样式检查发现原有 shimmer 动画虽在运行，但后置颜色规则让透明文字
  被覆盖，视觉效果实际不可见。按产品要求复用 AICSS Orbs 的 B5 Routing 参数，
  在现有思考状态中加入三层 B5 动效，并提供 `prefers-reduced-motion` 静态降级；
  状态文案统一为“思考中”。
- 助手展示名统一为“小芯”，Runtime 身份指令也固定为“小芯”，禁止模型自称
  e-Mate、Claude、Codex、ChatGPT 或底层模型。头像使用用户提供的图 2；生成式
  去底稿经视觉检查会改变原图细节，因此最终从原始像素做确定性 chroma 去底，
  得到四角透明、完整保留造型和色彩的 RGBA 资源。
- 新增浏览器断言覆盖“小芯”标题、头像资源加载、B5 运动变化和减少动态效果模式；
  不引入新的运行时依赖。旧候选包和云制品因本次代码变更已作废，正式发布前必须
  基于新提交重新构建和签名。

本轮定向验证：

```text
Web production build / content-addressed asset gate：passed
TypeScript：passed
Playwright 小芯头像与 B5/reduced-motion：2 passed
Runtime identity/image guidance focused tests：3 passed, 40 deselected
Ruff focused check、diff check：passed
真实上下文续接与最终回复：passed
真实 Skill search/read/续答：passed
```

# 2026-08-08 — 蓝绿暂存使用隔离数据库副本

- 正式云候选在 `stage` 阶段稳定失败，受控诊断确认有两个共同根因：Control
  Plane 在新 Skill Hub 种子收敛前先执行只读检查；Gateway 的单节点 SQLite
  进程锁又被在线绿槽持有。两者都不是可重试故障，增加重试只会重复失败。
- 暂存流程现在先停止所有非活动槽进程，再用 SQLite 原生在线备份从绿槽数据库
  取得一致性副本。Control Plane 与 Gateway 的新版迁移、契约检查和蓝槽健康检查
  只访问副本；Gateway 和 Image 的管理目录也统一指向同一份 Control Plane
  副本，避免一个暂存流程出现多套管理事实。
- Control Plane 的备份、Share spool、Skill Hub CAS 和 Bootstrap pointer 均落在
  加密卷内的一次性目录；暂存期间关闭 Bootstrap freshness 自动发布。图片
  PostgreSQL 只执行既有只读兼容检查，不在暂存阶段迁移在线库。
- 无论暂存成功或失败，所有蓝槽进程都先停止，随后删除一次性副本并恢复标准
  slot 环境；在线数据库、Nginx 路由和 active release 状态不变。路径、符号链接、
  文件类型、设备边界、权限和环境值继续使用部署器现有的 fail-closed 约束。
- 之前从 `20067e5` 构建的 Web 与云候选已经失效，不得发布；本修复提交后必须
  重新生成同一提交绑定的制品、签名、SBOM 和发布收据。

本轮定向验证：

```text
Cloud sidecar deployer：101 passed, 7 skipped
云制品/部署/Control Plane/Gateway 联合切片：171 passed, 7 skipped
SQLite 暂存失败模拟：在线源库不变、一次性副本已清理
Ruff、compileall、diff check：passed
生产暂存/激活：尚待新提交云制品实测
```

# 2026-08-08 — 企业 Responses 无状态续接与副作用硬去重

- 对隔离候选进行真实浏览器系统能力验收时确认，`read`、`shell`、
  `tool_search` 等 Core 能力已经直接投影；`tool_search` 返回空列表是因为它按
  设计只检索延迟披露能力，并非系统工具未安装。模型指令现在明确区分直接工具
  与延迟发现，空结果视为已完成事实，不再用等价查询盲目重试。
- 生产企业 Responses 代理会接受首轮工具请求，但拒绝每一次
  `previous_response_id` 工具续接。旧恢复只把最后一个工具结果放入下一次新请求，
  工具链每前进一步就遗忘此前事实，最终重复搜索、重复写入并耗尽模型轮次。
- 第一次续接被拒绝后，整个 Turn 现在持续使用无状态模式；每轮都从持久化
  `tool_executions` 按顺序重建所有已完成工具结果。连续性记录有单结果和总量上限、
  去除视觉内部标记，并在最终预算轮仍保留完整事实；恢复状态写入工具运行、人工
  交互、工具完成和轮次间检查点，Runtime 重启后继续同一恢复方式。
- Runtime 在执行前增加同一 Turn/执行批次、同能力快照、同权限快照、同工具版本、
  同参数摘要的精确完成记录复用。即使模型换了新的 provider call ID，已经完成的
  shell、图片或其他副作用也只返回原结果，不会再次调用处理器；没有跨 Turn、模糊
  参数或过期快照复用。
- 非幂等工具的未知终态交互按实际工具执行 attempt 生成幂等键。用户选择重试后若
  再次丢失确认，会出现新的明确冲突卡；不再复用已解决卡并触发
  `IdempotencyConflictError`。`resume_uncertain` 返回的新 attempt 同步成为当前事实。

本轮定向验证：

```text
工具续接/调用治理/沙箱边界：68 passed
多工具累计恢复：供应商拒绝续接后完成两次读取，重启后仍携带两份结果
同批次副作用硬去重：两个不同 call ID 只调用一次 shell handler
未知终态连续失败：attempt 1/2 产生两个独立冲突卡
Ruff、compileall、diff check：passed
真实浏览器复验：待新候选构建
```

# 2026-08-08 — 真实浏览器拒绝候选与恢复上限硬收口

- 精确提交 `ff6effd` 的 Web 候选完成登录、Luna/max 普通回复和 B5“思考中”
  动效验收后，在系统能力链路被真实浏览器拒绝：测试请求读取候选工作区之外的
  仓库文档，`read` 正确返回 `workspace_read_failed`，但模型持续改变
  `max_bytes`，在熔断后仍发起等价调用，共产生 21 条恢复计划。该候选仅保留在
  云端蓝槽暂存态；在线路由、稳定更新指针和当前生产版本均未改变。
- 根因位于共享 Agent Worker：三次自动恢复上限此前只是发给模型的建议，没有
  Runtime 强制执行；失败指纹又包含完整参数摘要，使无关参数变化绕过死循环检测。
  现在同一工具与同一机器错误码组成方向指纹，第三次失败即持久化无工具收口
  检查点；下一模型轮不再投影任何工具，只允许根据已完成事实、错误和部分结果
  生成用户可见终答。若供应商仍返回工具请求，Runtime 直接触发有界终态。
- 确定性的参数、路径或授权失败不再增加服务熔断计数；只有显式可重试的服务失败
  才能打开熔断器。工具输出与错误事实改用有序 typed input 连同无工具收口指令
  回传，避免 finalization 只发文字指令却丢失最近工具事实。
- `force_text_response` 同时写入工具恢复和轮次间检查点，进程在两个写点任一之后
  重启都不会恢复工具投影。现有工具级指数退避、最大三次重试、缓存复用和 Agent
  级切换/分解策略保持不变，没有重复开发第二套恢复器。

本轮定向验证：

```text
Agent Worker 全文件：46 passed
参数变化仍触发三次同方向上限：passed
确定性失败不污染熔断器：passed
无工具终局轮继续请求工具时触发 Runtime 护栏：passed
云候选：staged only，live_routes_changed=false
```

第二轮真实候选 `4ef335b` 进一步证明硬护栏生效：原来的 21 条恢复计划降为
3 条，任务在 59 秒内完成明确终答，且 `agent.reflection_requested`、
`agent.loop_detected` 和无工具收口请求均有持久事件。但该终答只得到
`provider_rejected`，没有得到最近一次 `workspace_read_failed`；候选因此仍被拒绝。
共同根因是无状态续接摘要只重建 `tool_executions` 中已完成的结果，失败恢复输出
没有进入同一检查点。现有 `_StatelessContinuationRecovery` 现在附带最多 8 条有界
失败恢复事实，沿用同一连续性摘要和重启检查点；状态为 `recovery_required` 的内容
明确标记为失败事实，不能投影为完成。未增加第二套历史或事实源。

补充定向验证：失败事实与已完成结果可在供应商无状态续接后共同出现，并在 Worker
重启后保持；工具处理器没有重复调用。指数退避抖动的浮点上界同时收紧到契约规定
的 `1.2`，避免哈希取到最大字节时出现 `1.2000000000000002`。

# 2026-08-08 — 消息内事实编排与任务清单投影

- WebUI 不再在 Turn 终态后按“最长回复”重新折叠或重排过程。消息、推理、工具、
  交互和检查点继续严格按 Runtime 的 `created_seq` 投影；前端没有合成新的执行阶段、
  工具结果或完成状态，后端 Runtime 仍是唯一事实源。
- `task_list` 仍由 Runtime Item 提供，WebUI 只把同一事实从消息流移到输入框上方的
  可悬停、聚焦和点击清单；完成数、条目状态与中断状态均取自对应 Item/Turn，局部
  disclosure 状态只控制显示与隐藏。
- 复用侧栏已有的 `LoaderCircle` 并补齐共享 `.ex-spin` 动画；任务开始和终止仍由
  后端 Thread/Turn 投影决定。终态事件到达时仅刷新 Thread 目录，使侧栏运行指示
  能立即跟随后端终态消失。
- GA 增加 `codex-layout` 确定性场景，覆盖回复正文先于灰色动作事实、任务清单不在
  Timeline 重复出现、悬停面板位于输入框上方、侧栏运行指示和减少动态效果。真实
  应用内浏览器已验证消息顺序、面板层级、运行态开始/结束和 reduced-motion 行为。

本轮定向验证：

```text
TypeScript noEmit：passed
Timeline/GA mock tests：13 passed
Vite production build：passed
应用内浏览器：消息事实顺序、任务清单悬停、侧栏转圈与终态消失均 passed
完整真实能力验收：并行审计已于下节收敛
```

# 2026-08-08 — 商业化开箱能力并行审计与 Skill 元数据修复

- 三条独立只读验收线分别检查 Core 内建能力、商业 Agent 基线和
  Skill/MCP/图片/Office 扩展链。`builtin_tool_specs()` 的 19 项目录、Turn 冻结
  快照、组合后处理器和公开投影保持单一后端事实；工作区读取、Shell 沙箱、
  Tool/Skill/Connector 发现、附件与 Artifact 读取、图片生成/改图、MCP supervisor
  均有真实实现，没有建立第二套能力层。
- 本地确定性调用验证覆盖 19 项目录、真实 Shell 子进程、Skill search/read/run、
  MCP stdio JSON-RPC、图片引用编辑与 Artifact open。外部网页、登录浏览器、生产
  Connector 和付费图片仍必须在正式候选中用真实凭据验收，源码假实现不能替代。
- 审计确认 Office Skill 在没有 PyYAML 的随包解释器中会丢失顶层
  `quality-gates` 列表。共享 frontmatter fallback 现在只增加最小的顶层字符串列表
  支持；图片工作流同时补回声明的官方 `imagegen` 映射。正式随包 Python 下五个
  facade 的发现、提示词元数据和无 PyYAML 回归共 `3 passed`。
- Skill 管理页不再把所有 `ready` 扩展直接翻译成“可运行”；说明型 Skill 与可执行
  Skill 共用的后端 ready 事实现在显示为“可使用”，避免前端夸大能力。是否实际执行
  仍由 Runtime 的 Skill manifest、runner 和调用终态决定。
- 仍需修复的首要产品缺口是 Office：签名 `office.formats` Pack 当前只实现 `probe`，
  尚未把 create/read/edit/render/validate 连接到 Agent 与 ArtifactService。结构化
  edit/search、持久 Shell/浏览器会话和 MCP 自助注册是后续商业基线差距；现有
  Job/Worker 恢复、Artifact CAS、imagegen、MCP supervisor 与权限账本应直接复用。

本轮定向验证：

```text
Core/GA/Extension Web tests：20 passed
正式 content-addressed Web build：passed（40 assets，33 chunks）
Office/Image Skill facade（随包 Python、无 PyYAML）：3 passed
19 项 Core 能力并行审计：目录与本地处理器链 passed；外部服务待正式候选
```

# 2026-08-08 — Office Skill 原生执行与 Artifact 发布接线

- 没有新增第二套工具目录。四个内置 Office Skill 继续通过既有
  `skill_search → skill_read → skill_run` 渐进披露链执行；只有精确冻结的
  `skill.office-documents`、`skill.office-spreadsheets`、
  `skill.office-presentations` 和 `skill.office-pdf` 修订可进入 Runtime 原生后端。
- `office.formats` 现在由受管 Pack 适配器提供有界 `create` 操作。DOCX、XLSX、
  PPTX 和 PDF 分别由签名 Office Pack 内的 python-docx、openpyxl、python-pptx、
  ReportLab/pypdf 创建并重新打开做结构校验；Core 不导入 Pack 私有依赖，也不使用
  全局 Python 或 `pip` 兜底。
- Runtime 对输入参数、文件名、集合大小、单元格类型、MIME、文件签名、base64 和
  5 MiB 输出上限再次校验。Pack 的 base64 响应仍受既有 8 MiB 进程 stdout 边界
  约束。相同幂等键和相同请求只执行一次 Pack；同一幂等键若被不同请求复用会明确
  报冲突，不会返回旧文件。
- 生成字节通过现有 `ArtifactService` CAS、同一 Runtime 数据库事务、Artifact Item
  和 `artifact.office.created` 事件原子发布。WebUI 仍只投影后端 Artifact 事实。
  当前质量证据明确为“结构校验通过、视觉渲染验收未执行”，没有把 create 扩大宣传
  为尚未实现的 edit/render/视觉 QA。
- 四个 Skill 指令已加入精确 `skill_run.parameters` 合同，禁止模型回退到未追踪的
  shell 文件或临时安装依赖。

本轮定向验证：

```text
Skill/Pack/Artifact 定向回归：47 passed
Ruff、compileall、diff check：passed
真实随包依赖：DOCX 3 段、XLSX 1 表、PPTX 1 页、PDF 1 页均创建并重新打开
幂等发布：重复请求 1 次 Pack 调用、1 个 Artifact；参数冲突 fail closed
视觉渲染与系统 open：待正式候选浏览器验收
```

# 2026-08-08 — CowAgent Office 随包原则与安装态 Skill 链修复

- 以 CowAgent `daf231d05b62364f1886398d288446ee66a93caf` 的 WebUI 桌面构建为
  对照：其将 pypdf、python-docx、openpyxl 和 python-pptx 纳入冻结后端，并为
  延迟导入和模板数据显式补 PyInstaller 收集规则；重型浏览器类能力仍按需安装。
  e-Mate 采用同一产品原则，但保持更严格的模块边界：Office 格式依赖只存在于
  不可变、签名的 Office Capability Pack，Core 与共享 `pack-python` 不复制这些库。
- 真实 1.0.0 候选的 Office Pack 已安装且健康，格式依赖闭包完整；PPT 会话失败的
  共同根因是安装后的 Core payload 没有 `skills/`，同时能力快照只接受
  `LOCAL_BUNDLE`，把已经按 Core 签名根验证并写入 CAS 的 `CORE_BUNDLE` 过滤掉。
- 平台暂存器现在把仓库内置 Skill 复制到 Core payload；产品启动必须从所选安装槽
  显式提供真实、非链接的 `payload/skills` 根。SkillRuntime 只额外允许已验签的
  `CORE_BUNDLE`，没有放宽 Pack、用户目录或任意文件来源，也没有新增第二套运行时。
- 首次基于修复提交构建的候选通过了签名与 Bootstrap 自检，但终态包检查发现手动
  快车道只替换旧 Core 的 `ecorex/`，没有调用平台暂存器，因而仍缺 `skills/`；该
  候选被拒绝且未启动、未上传。唯一手动构建器现在用 Git 跟踪清单替换 Core 内的
  整棵 Skill 树，并删除基包可能残留的旧文件，避免源码测试与交付包事实分叉。
- 第二个候选已真实启动，安装态 14 个 Skill 全部 `enabled/healthy`，贡献快照也含
  `office-presentations`；但真实模型的三次自然语言检索仍返回空。根因是搜索器要求
  查询的每个词都同时出现在单个 Skill 元数据中，参数扩展反而收窄结果。搜索现在
  以显式 Skill 身份优先，再按命中词数排序；自然语言至少命中一词即可进入结果，
  未命中的显式引用仍可被精确解析。排序保持确定性，CAS/修订重验边界不变。
- 真实浏览器同时暴露 Shell 默认 120 秒与 Pack 收尾预算冲突。共享进程适配器现在
  让默认超时和显式超时统一经过既有 5 秒收尾预算；工具级失败仍由同一恢复器处理。
- 不变 Office Pack 在 16 个候选构建中的 archive SHA 均为
  `43abf96da24e9d3eb73bab95a7fbcf2a33a769ec48008722df45e0200d0a875c`，
  证明 Core-only 构建沿用确定性 Pack 字节；正式发布仍必须资源先上传并回读校验，
  最后原子切换稳定指针。

本轮定向验证：

```text
平台 Core skills staging + 安装态 skill_search/read/run + Office Artifact：6 passed
Product 构造器 + LOCAL_BUNDLE 回归：5 passed
Shell Pack 默认超时与真实 pwd：2 passed
旧候选真实 PPT 会话：正确返回部分结果且未伪造文件；因 Skill 空快照拒绝发布
新候选真实 PPT/PPTX 打开：待重新构建后验收
```

# 2026-08-09 — 图片用量事实与 Usage 对账投影

- 管理库 schema 从 v5 仅新增迁移到 v6；`admin_ops_provider_usage_facts`
  保留原表、原列、不可修改/删除触发器和旧查询语义，只追加租户、请求模型槽、
  Provider 报告模型、实际模型、Provider、降级来源/状态、Job/Result 状态字段。
  迁移在同一 SQLite 独占事务中执行，旧事实不回填、不覆盖，失败可整体回滚。
- 图片服务继续使用现有 `_AdminManagementImageUsageProvider` 产品边界装饰器；
  每个 durable `job_id` 只结算一条 immutable fact，submit/recover 重放保持
  exact-once，批量任务仍按每个 Job 独立记账。租户取结算事务内的用户事实，
  因此后续用户改租户不会重写历史归属。
- 用户随后明确批准一次最小 Core 合同例外，仅允许 `ProviderResult` 增加
  `actual_model_id`、`fallback_from_model_id`、`fallback_used` 三项事实；没有改变
  Provider、Dynamic Provider、Worker 或 Runtime 的模块设计。OpenAI 图片适配器
  在实际请求成功点写入 upstream model，在既有且唯一的确定性
  `gpt-image-2-pro → gpt-image-2` 安全降级点写入来源；Dynamic Provider 和管理结算
  只透传，不比较本地槽位、不推断。401/403 即使伪装成 `model_not_found` 也不得
  降级；没有 provenance 的其他 Provider 仍保持 NULL/“未公开”。
- 现有 Usage 面板继续用完整 provider fact ledger 对 lifetime token/image 余额
  对账；所选日期范围同时投影图片明细，包括租户、Job、请求槽、Provider 报告值、
  实际模型公开状态、降级公开状态和 Job/Result 状态。旧 v5/简化表事实仍可查询，
  NULL 降级状态保持 unknown，不转换成 false。

本轮定向验证：

```text
管理 schema / exact-once / 图片逐 Job / Usage 投影：27 passed
管理 schema 相关回归：134 passed，8 skipped
Python compile：passed
Provider provenance 主路/降级/401/403/Dynamic/exact-once：31 passed
图片 Provider/Orchestrator 扩展回归：66 passed，1 skipped
```

# 2026-08-09 — 安装包品牌门禁展开扫描

- 首个 macOS arm64 未签名候选已生成 DMG/ZIP、blockmap 与
  `latest-mac.yml`，但没有进入验收或发布。安装包展开扫描发现通用门禁会把
  Framework 的包内符号链接、Rust `Cow<str>` 类型名和 ZIP 压缩随机字节误报；
  同时也在 Core ZIP 的内置 Skills 中定位到 7 处真实旧品牌/命名空间残留。
- 门禁现在默认仍拒绝所有链接；仅制品模式允许解析后仍位于同一扫描根内的链接，
  并逐层读取 ZIP 成员（含内嵌 `python311.zip`），检查路径、UTF-8、UTF-16 和
  二进制字符串。压缩字节不再直接作为品牌事实；文本中的短品牌仍检查，原生二进制
  中通用的 copy-on-write `Cow` 类型不再误判。
- 六个内置 Skill 的旧命名空间已改为扁平 JSON metadata，随包无 PyYAML 的既有
  parser 仍能保留 `always`、`default_enabled` 与模块需求；Skill README、创建指令
  和 Office 提示词全部使用 e-Mate/能力中心/`~/.emate`，不再向用户暴露旧产品。
- GitHub Actions 在构建前扫描源码和 Web，在构建后再次扫描 `win-unpacked` 或
  `.app`，并通过随 `electron-builder` 安装的 ASAR CLI 扫描内部路径；任何真实
  旧品牌路径或内容都会阻止哈希与制品上传。

本轮定向验证：

```text
品牌门禁单测：5 passed
源码 + Skills + Web：540 files，0 violation
六个 Skill metadata（正式随包无 PyYAML parser）：preserved
首个 macOS 候选：因真实 Skills 泄漏被拒绝，必须基于新提交重签 Runtime 后重建
```

# 2026-08-09 — 2.0.0 macOS arm64 未签名候选

- 品牌修复提交为 `08b9bf80befe7840e394449afe0bc0eaedcff657`，Renderer 树保持
  `303c5bc4af1e2dcb432b7b2a704ce95defae2ed7`。该精确提交重新生成跨平台
  签名 Runtime seed；私钥未持久化，平台 Developer ID/公证均为 false。
- Runtime `release_id=release-stable-38e78d015cabadb5864b9940`，
  `build_digest=38e78d015cabadb5864b99408c135391034eb40e0d495997c12704ae6607141d`，
  `manifest_sha256=b78db66e96af61af78340b238ccf03259dccb12920ac7df1151ec6effd1ddcfc`。
  Windows Runtime ZIP SHA-256 为
  `36fe5001d2c9d24bf4b3b6635329ae6574c9446514a4c5559fb986a4dfd90f36`；
  macOS universal Runtime ZIP SHA-256 为
  `56286fd1f33e8ea897444f17ec63322a265109bbb2dde7b0454b0b79be2682fb`。
- macOS arm64 桌面候选使用 `electron-builder 25.1.8`、Electron 33.2.0，
  `identity=null`、Hardened Runtime false、未公证。包内主程序和 Bootstrap 都是
  arm64；`CFBundleIdentifier=net.ecoremedia.emate`，版本 `2.0.0`，图标 ICNS
  1024×1024 且带 alpha。Mach-O 仅保留工具链产生的 ad-hoc/linker signature，
  TeamIdentifier 为空，Gatekeeper 按预期拒绝直接信任。
- 候选 DMG SHA-256：
  `81307417e89922fe4a9643573bc7fddde6006c076015d41de1fb114bed78a06f`；
  ZIP SHA-256：
  `cd0d85145c0a44b9b14c0945ad9f31bda7d51b0c958dbaf4980537b770b3db67`；
  `latest-mac.yml` SHA-256：
  `46e3cc1b67417a371aa6d43ec233f61d074a03e9f201f13acccbfa85f390503e`。
  清单内两个文件的 size/SHA-512 已逐项从本地制品重算一致。
- 新 Runtime release 的 46 个文件以及 `.app` 展开的 118 个文件均通过 ZIP/二进制
  品牌门禁，0 violation。首个被拒绝候选的文件在同名重建时被覆盖；其哈希与拒绝
  原因仍在本日志，旧 DMG/ZIP 本体未保留。
- 尚未发布：Computer Use 已停在运行未识别来源应用的动作前，等待动作时确认；
  GitHub 发布技能要求本机存在并认证 `gh`，当前 `gh` 不存在，因此没有推送、没有
  Actions run、没有 Release、没有企业更新源写入，也没有推进 `latest`。

本轮定向验证：

```text
Runtime release 展开品牌扫描：46 files，0 violation
macOS .app 展开品牌扫描：118 files，0 violation
DMG/ZIP latest-mac SHA-512/size 回读：passed
Bundle ID / version / icon alpha / arm64 / unsigned state：passed
Computer Use、Browser、Windows、macOS x64、部署发布：pending
```

# 2026-08-09 — e-Mate 2.0 桌面 feed 离线发布门禁

- `.github/workflows/emate-2.0-desktop-release.yml` 现在只在
  `codex/e-mate-2.0.0` push 或手动 dispatch 时运行；不要求先合并 `main`，权限仍为
  `contents: read`，没有 Release、部署或更新指针写步骤。Actions 与 Python/Node/Go
  版本全部复用项目锁定值，checkout 不保留凭据。
- Windows、macOS arm64、macOS x64 各自生成完整 handoff SHA-256 清单，覆盖安装包、
  blockmap 和架构 metadata。两个 mac job 分别交付 `latest-mac-arm64.yml` 与
  `latest-mac-x64.yml`，避免同名覆盖；后置 `feed-gate` 下载 Runtime 加三个桌面
  artifact，逐项回算 SHA-256、metadata 内 size/SHA-512，并用 Bootstrap trust
  校验 Runtime manifest Ed25519 签名、每个 Runtime artifact 的内容和签名，最后才
  生成单一 `latest-mac.yml`。
- `scripts/prepare-emate-desktop-feed.py` 是同一离线门禁的本地复用入口。Actions 中
  首次运行不接收公开指针，receipt 必须停在
  `awaiting-public-bootstrap-index`；验收后的发布操作者必须把既有 Runtime 发布链
  生成并验证过的 `public-bootstrap-index.json` 作为
  `--public-bootstrap-index` 重新组装，只有此时 receipt 才是 `activation-ready`。
  示例输入如下，四个目录均为 Actions artifact 解包后的独立目录：

```bash
python scripts/prepare-emate-desktop-feed.py \
  --runtime-root handoff/runtime \
  --windows-root handoff/windows-x64 \
  --macos-arm64-root handoff/macos-arm64 \
  --macos-x64-root handoff/macos-x64 \
  --nginx-config deploy/e-mate/nginx/update-feed.conf \
  --public-bootstrap-index handoff/public-bootstrap-index.json \
  --output handoff/feed-v2.0.0 \
  --expected-version 2.0.0 \
  --expected-source-sha '<40 位已验收提交 SHA>'
```

- 输出 `feed-stage-receipt.json` 固定记录完整文件 inventory、feed build ID、候选目录、
  Nginx 配置哈希和原子切换合同。部署时先把候选放入独立的
  `/srv/e-mate-update/releases/<candidate>`，回读文件后才用同文件系统临时软链接原子
  替换 `/srv/e-mate-update/current`；`latest.yml`、`latest-mac.yml` 与公开 Bootstrap
  指针因此最后同时可见。切换/回滚 receipt 都必须包含 `operation`、
  `feed_build_id`、`previous_target`、`new_target`、`manifest_sha256`、
  `public_readback_sha256`、`completed_at`。回读失败时把 `current` 原子指回
  `previous_target`，不得推进或保留半切换的 latest。
- `deploy/e-mate/nginx/update-feed.conf` 使用独立
  `/srv/e-mate-update/current`，不覆盖生产现有 `/opt/e-mate/current` Web 产品；三个
  mutable pointer 使用 exact location，其余 feed 使用 `^~ /e-mate/update/` 静态
  alias。配置禁止 `index.html`/`@spa` fallback，文件不存在必须由 Nginx 返回 404，
  不再落到现有 `/e-mate/ -> 127.0.0.1:18080` SPA proxy。上线前仍需把该片段 include
  到 HTTPS server、执行 `nginx -t`，并用缺失文件探针确认 404 后才允许切换。
- 本轮只改发布 workflow、离线门禁、Nginx 片段、聚焦测试和锁文件 workflow 清单；
  未修改 Core 或产品 UI，未读取凭据，未进行任何外部写入、部署或 `latest` 切换。

本轮定向验证：

```text
桌面 feed 签名/合并/篡改/SPA 回退单测：2 passed
Python Ruff：passed
workflow dependency/action/toolchain lock：passed
workflow YAML parse：passed
生产部署与 latest 切换：not run
```

# 2026-08-09 — 本地 GA/CDP 与生产发布前只读基线

- 2.0 首页、能力中心、权限开关、项目会话和模型目录已经替换旧验收定位；
  `desktop/tools/run-local-live-preflight-cdp.mjs` 继续验证真实可操作控件和状态闭环，
  没有用“元素存在”代替行为断言，也没有修改产品 UI 或 Core。GA thinking fixture
  只延长事实停留窗口，以稳定观测既有 200 ms sticky replacement 合同。
- 完整本地 GA/CDP 通过 18 个场景、4 个视口、133 条断言；console、page、failed
  request 与 external request 计数均为 0。报告仍固定
  `candidate_bound=false`、`protected_provenance_claimed=false`，本证据只能证明
  `local-ga-contract-runtime`，不能替代真实企业 Runtime、安装包或账号验收。
- 使用当前签名 Runtime seed 的独立临时安装根完成首次安装、slot 激活和
  `bootstrap_health_confirmed`；停止测试时原生 Bootstrap 的 SIGTERM 转发也使整棵
  supervisor/Runtime 进程树退出，没有遗留 8765 监听。正式产品 Runtime 随后在
  macOS `SecItemCopyMatching` 等待 Keychain 授权，owner endpoint 尚未建立；没有
  绕过、删除或读取 Keychain secret。必须在用户确认运行未签名应用后，由
  Computer Use 进入真实桌面路径；若系统要求登录密码，交还用户本人输入。
- 对企业服务器只做了固定命令的只读盘点，没有上传、迁移、重启、reload 或指针
  写入。当前 green cloud 槽运行产品 `0.3.2`；Control Plane 与 Gateway 数据库
  `quick_check=ok`。上线前对账基线为 41 个管理用户、298 条管理审计、178 条
  Provider 用量事实、222 条 Gateway 请求、5 条 Gateway model attempt；现网管理
  schema 为 v4、Gateway schema 为 v3，本次只允许分别增量迁移到已验证的 v6/v4，
  上述历史数量不得减少。
- 现网 `/e-mate/` 仍整体反向代理到既有 Web 产品，因此不存在的
  `/e-mate/update/*` 会返回 971 字节 SPA HTML；独立的
  `/srv/e-mate-update/current` 静态配置尚未安装。现有 `/opt/e-mate/current`
  属于另一套 2.1.47 Web 产品，本次 feed 不得覆盖。Nginx 404 门禁、数据库备份、
  blue/green 迁移、Actions 跨平台构建、GitHub Release、企业 feed 回读和原子
  `latest` 切换仍全部 pending。

本轮定向验证：

```text
本地 GA/CDP：18 scenarios、4 viewports、133 assertions，0 browser/network errors
GA mock 合同：11 passed
签名 Runtime 首装/健康确认：passed；macOS Keychain 用户授权：pending
Bootstrap SIGTERM 进程树退出：passed
生产数据库 quick_check 与只读数量基线：passed
外部写入、部署、Release、latest：not run
```

# 2026-08-09 — 2.0.0 macOS x64 未签名本地候选

- 在 CI 尚未触发的情况下，继续复用 `08b9bf80befe7840e394449afe0bc0eaedcff657`
  的同一签名 Runtime seed，只抽取并暂存 `macos-x64` Bootstrap；没有重签 Runtime，
  没有生成或持久化私钥。当前 HEAD 相对该产品提交只增加发布/验收脚本和日志，
  Renderer 仍是 `303c5bc4af1e2dcb432b7b2a704ce95defae2ed7`。
- 现有 `desktop/node_modules` 原先是指向旧候选的 pnpm 目录链接，首次构建缺少当前
  Rolldown 原生绑定。按未改动的 `package-lock.json` 执行一次 `npm ci` 重建该忽略
  目录；锁文件和源码均未改变。ChatGPT 自带签名 Node 因 macOS Library Validation
  不能加载 npm 原生模块，最终使用项目已锁定的 Node `22.23.1` 工具链；这属于本机
  工具身份边界，不是产品回归。
- electron-builder 首次获取固定 Electron x64 archive 时 GitHub 连接超时；有界重试
  后从官方 `v33.2.0` Release 下载，并用同 Release 的 `SHASUMS256.txt` 验证 SHA-256
  一致，再只重跑 builder。正式 CI 仍会独立下载并构建，不复用本机缓存。
- x64 `.app` 主程序和随包 `emate-backend` 均为 Mach-O x86_64；
  `CFBundleIdentifier=net.ecoremedia.emate`、版本 `2.0.0`。bundle 没有可用签名，
  `spctl` 按预期返回 rejected/no usable signature；没有启动应用或触发 Gatekeeper。
- x64 DMG SHA-256 为
  `55124ee8f3da45604c750cd885f2c73aa3c306ee56028a1159e556aa2994794e`；
  ZIP SHA-256 为
  `b1195030f552b5a1a77e3898a70bb83a4373c30a13a62f876f6c68f436c8aba4`；
  DMG blockmap SHA-256 为
  `0efd48c1117a7157496dbba42e975f90022ae031c9ec33a72ce34d36b7e01825`；
  ZIP blockmap SHA-256 为
  `acbd04ce450f37b010dd953f7e5abeea86700c51d060b0613eff1740a891c7f1`；
  本架构 `latest-mac.yml` SHA-256 为
  `cebaf93b4547489871d0b12eeb1fe7ac90e7ea72171946b9a553bdaf40585422`。
  metadata 中两个文件的 size/SHA-512 已从实际字节回算一致。
- 展开的 x64 `.app` 扫描 118 个文件、0 品牌违规；ASAR 的 6,010 条路径、0 违规，
  只有一个 `/dist` Renderer、Electron 壳与小芯/e-Mate 资源。当前本机
  `latest-mac.yml` 仅代表 x64；正式双架构 `latest-mac.yml` 必须继续由 Actions
  `feed-gate` 合并，不得手工发布该单架构文件。

本轮定向验证：

```text
Web content-addressed build + Electron contracts + desktop brand：passed
macOS x64 DMG/ZIP/blockmap：built
x64 metadata size/SHA-512：passed
Bundle ID / version / x86_64 / unsigned state：passed
expanded app：118 files，0 violation
ASAR：6,010 paths，0 violation，single Renderer
启动、Computer Use、发布、latest：not run
```

# 2026-08-09 — e-Mate 桌面下载页还原与动态版本索引

- 下载页按用户提供的 1487×1058 视觉稿恢复为左侧转化、右侧产品预览和底部可信
  事实条的首屏。左上角直接复用桌面端 `emate-logo`；英文 Enterprise 版本胶囊改为
  “企业智能体桌面工作区”。主标题保留“每次继续，都从上次的进度开始”，下方改为
  “Agent工作新范式 / 从自己干到通过agent快速落地想法。”。
- 产品预览不是设计稿内的旧界面：从当前 Renderer 的 light/empty 工作区重新截取，
  图中导航已经是“定时任务 / 能力中心”，并展示当前小芯首页与输入区。旧下载页的
  两张 ECoreX 图片、五机器人轮播和不再使用的样式/脚本已删除。
- `prepare-emate-desktop-feed.py` 在既有四方制品门禁通过后新增生成
  `download-index.json`，只投影实际版本、发布时间、Windows x64、macOS arm64/x64
  的文件名、大小和 SHA-256。页面严格校验该索引并自动识别系统/芯片；无法可靠识别
  Mac 芯片时只提示选择，不会猜测架构。版本导航、当前版本和下载 URL 全部来自索引，
  页面源码不含固定产品版本。
- 企业更新 Nginx 为该索引新增独立 `no-store` JSON location，并只向正式下载站域名
  开放读取；索引与 `latest.yml`、`latest-mac.yml`、公开 Bootstrap 指针一起进入原子
  feed receipt。没有部署、没有切换 `latest`，本轮浏览器使用的发布索引仅为本地视觉
  验收夹具，不属于提交或发布制品。

本轮定向验证：

```text
下载页静态/动态索引与 feed 门禁：7 passed
Python Ruff 与 git diff --check：passed
Browser 1487×1058 同尺寸视觉比较：passed
Browser 390×844 响应式首屏：passed
macOS arm64 自动推荐、Windows 切换与卡片过滤：passed
生产部署、公开下载回读、latest 切换：not run
```

# 2026-08-09 — e-Mate 下载页最终视觉还原与立即下载

- 以用户最终提供的 1487×1058 参考图重新收敛首屏：保留当前真实 Renderer 截图，
  补齐透明黑色丝带、轨迹、图片/报告/图表卡、文件夹和全身小芯资产；没有把参考图
  整张当背景，也没有重新引入旧产品窗口。
- 主按钮动态文案改为“立即下载”；补齐下载、macOS 和 Windows 图标。下载图标复用
  已安装 Lucide 图标并在 `THIRD_PARTY_NOTICES.md` 补充 ISC 声明；平台图标直接取自
  用户视觉源。全部公开 CSS、JS、PNG/JPEG/SVG 继续使用内容寻址文件名。
- 动态版本、设备/芯片识别、三安装包索引和企业下载 URL 保持原实现。页面 DOM 不
  固定产品版本；当前浏览器 fixture 只用于验收，不进入提交。
- Browser 在 1487×1058 CSS viewport 验证产品框 x=597.1、y=218、w=742.7、
  h=538.2；390×844 视口无横向溢出。控制台 warning/error 为 0。

本轮定向验证：

```text
下载页 source/implementation 同画面对照：passed
macOS arm64 自动推荐与“立即下载”绝对 URL：passed
390×844 响应式、图标与无横向溢出：passed
公开资源内容寻址与静态门禁：passed
生产部署、GitHub Actions、latest 切换：not run
```

# 2026-08-09 — Gateway 管理改动下一请求即时生效

- Gateway 管理模式现在复用 Control Plane 已有的设备访问令牌权威，不新增令牌、
  数据表或并行认证链。每次请求在 Ed25519 验签后，按 JWT `jti` 回读既有设备租约、
  撤销事实和签发时 `auth_epoch`，再用当前用户 revision、密码 credential revision
  及租户模型策略重算 epoch；用户停权、密码修改或租户删模型后，旧 JWT 在下一次
  Gateway 请求即被拒绝。
- Control Plane 的 `ManagedDeviceIdentityBroker` 与 Gateway 共用同一个只读
  `ManagedAccessTokenAuthority`，原有登录、刷新、签发和 Provider 模块设计不变。
  Gateway 启动时校验共享管理库与设备身份 schema；管理模式没有可验证 `jti` 的
  非设备令牌一律失败关闭。

本轮定向验证：

```text
租户策略更新前/后同一已签发 JWT：允许 / 下一请求拒绝
Gateway 生产组合绑定 current-token authority：passed
Gateway/设备身份聚焦回归：39 passed
Ruff：passed
```

# 2026-08-09 — 真实桌面首验缺陷收敛与首版全量验收门禁

- 从 Actions `95df539` 的 macOS arm64 安装包真实安装并启动 e-Mate，完成测试账号
  登录；默认目录只展示 Luna/max、DeepSeek、豆包与 Sol，Gemini 继续保留为不可用
  槽位且不进入客户端目录。第一轮 Luna/max 真实请求成功，消息正文、小芯身份、会话
  和完成终态均由 Runtime 事实投影。
- 第二轮真实请求稳定失败为 `gatewayauthenticationerror`。生产只读对账确认第一轮
  Usage 结算把用户普通并发 revision 从 148 推进到 149，而设备令牌权威错误地把该
  revision 同时当作认证 epoch，因此一次成功用量就会废掉当前访问令牌。管理 schema
  仅新增 v7 `auth_revision` 并按旧 `revision` 回填；用户资料、状态、额度、密码和租户
  策略继续使旧令牌下一请求失效，Provider Token/图片用量结算只推进并发 revision，
  不再把正常使用误判为权限变更。该修复位于企业管理适配层，没有改 CowAgent Core
  架构或 Gateway 协议。
- 正式聊天 Composer 的模型、外部连接、完全访问、用量和发送动作已经收进同一个
  输入框工具栏；窄窗口禁止把这些元控件换行堆到输入框外。设置页把个人资料、常规、
  知识和记忆改为互斥独立页面；记忆继续使用真实 snapshot/reset/undo API，知识继续
  通过当前会话的 Agent/Skill 管理，不伪造 Runtime 尚未提供的知识树、图谱和导入列表。
- Skill 用途分类不再把绝对路径中的 `Documents` 等目录名当作办公语义；批量图片继续
  使用稳定 `t任务序-i图片序` 命名。首版真实验收新增连续两轮上下文、生图后“上一张”
  续改、批量后“第 N 张”续改，以及刷新/重启后的 thread、parent execution、artifact
  reference 和 revision lineage 对账。
- 默认身份文案统一为“我是智能体小芯，来自 e-Mate Agent”，默认专业严谨并称呼用户
  为“同学”；旧 Provider 自报身份的可见清洗结果也不再回写旧产品名。
- 客户端审计上传合同与生产 `/api/v1/audit/records` 已经存在，但 predecessor
  `runtime-config.audit` 一直为 null。2.0 builder 现在只在 Product 边界注入该现有
  HTTPS 端点，继续使用已登录 managed session，不新增或下发审计 Secret；本地加密
  outbox、30 天原始事实和 180 天聚合保留合同保持不变。
- 通道真实预检确认 13/13 connector 均因 `runtime-config.connectors=null` 而安全关闭；
  飞书/腾讯文档 begin 均在本地以 503 `connector_unavailable` 失败，未触达外部、未写入
  凭据。仓库已有完整 Managed Connector 客户端合同，但没有远端 Connector Gateway、
  OAuth client 凭据或签名 Runtime 配置；在这些真实外部依赖补齐前，连接、断线、重试
  和撤销仍是发布阻断，不能用可点击但必失败的假适配器代替。

本轮定向验证：

```text
管理 schema v1/v5/v6 -> v7 与连续令牌权威：17 passed
Gateway 生产认证/管理组合：24 passed
Renderer Composer/设置聚焦 E2E：2 passed
Renderer 产品语言/功能合同：23 passed；TypeScript：passed
Skill/Office/imagegen 联合回归：38 passed
Gallery/失败槽位/恢复：13 passed
连续上下文与生图续改合同：5 passed
默认身份回归：6 passed
真实通道目录与 fail-closed：passed；真实 OAuth/断连/重试：blocked
新候选构建、真实连续对话、生改图、发布、latest：pending
```

# 2026-08-09 — 客户端能力合同与生产注入对账

- 按 Runtime 外部服务合同、Actions 环境配置与公网路由三方对账。模型
  Gateway、账号登录、生图、分享和更新发现已有生产路由；Gemini 只保留
  租户可选槽位，没有活动配置或可用 Key，按要求不进客户端目录。
- 审计上传服务与密钥已在 Control Plane，缺口仅是客户端
  `runtime-config.audit=null`；2.0 builder 已在既有 Product 边界注入，无需新凭据。
- 账号改密 `/v1/account/password` 与 Skill Hub
  `/ecorex-agent/client/skill-hub/v1/*` 的客户端/服务端合同都已存在，但
  生产 Nginx 未转发，公网只读探测分别返回 405 和 404。现只在现有
  Control Plane 路由边界补两个精确反代，限制方法/请求大小并关闭请求缓冲，
  未改 Core。
- 通道合同仍缺 Connector Gateway、签名 Runtime 配置及 OAuth 应用凭据；
  远程 MCP/OAuth 合同仍无任何已验证 binding/registration；OTLP 追踪合同仍无
  collector 与 Runtime 配置。三者继续失败关闭，不伪造可用状态。
- 2.0 更新 feed 合同已存在，但 GitHub 环境只有发布公钥和旧平台配置，
  没有可调用的 2.0 public-bootstrap authority signer；`v2.0.0` Release 仍未存在，
  不得在缺少 authority/freshness 签名时切换 `latest`。

本轮定向证据：

```text
安装候选 Runtime config：audit/tracing/connectors = null
Actions 三平台保护配置：audit/tracing/connectors = null
公网无凭据路由探测：gateway/device/image/share/audit/release = 服务端路由存在
公网缺口：account/password = 405；skill-hub = 404；connectors = 404；OTLP = 无 collector
GitHub：v2.0.0 Release = absent；Connector/OAuth 凭据名 = absent
```

# 2026-08-09 — 飞书首个真实 Managed Connector Product/Cloud 闭环

- 未改 CowAgent Core、ConnectorService 或既有 `ManagedConnectorGatewayAdapter`
  合同。飞书 Gateway 直接挂载到现有 Control Plane 的
  `/api/v1/connectors/feishu/*`，复用同一 managed-session JWT、账号/组织
  principal、设备令牌即时撤销权威、SQLite WAL 和 Usage 已用的 Cloud Audit，
  不新增域名、身份系统或假 adapter。
- OAuth 使用当前飞书 v2 合同：授权入口
  `https://accounts.feishu.cn/open-apis/authen/v1/authorize`，query 使用
  `client_id`、固定 `S256` PKCE；换取和刷新均使用
  `POST https://open.feishu.cn/open-apis/authen/v2/oauth/token`。刷新令牌按单次
  轮换保存，提前失效的 access token 只强制刷新并重试一次；其他 4xx 不绕过，
  429/5xx 保持既有 retryable 事实。
- 客户端只得到随机 `fgrant_*` 不透明句柄。`access_token`、`refresh_token`、
  app secret 和幂等响应均使用独立 32-byte 服务端密钥加密落库，不进入响应、
  日志、审计、Runtime 配置或安装包；revoke 会立即销毁 token envelope 并按
  账号及 `organization_id` 拒绝后续访问。
- 首版真实动作闭环为 `documents.read`、`documents.write`、`drive.search` 和
  `messages.send`，返回继续满足现有 closed schemas。为避免跨 HTTP 的伪事务
  造成用户数据丢失，文档正文只允许写入用户预先创建且当前无正文块的文档；已含正文的
  覆盖请求在任何写入前以 `document_content_replace_unsupported` 失败关闭，
  现有文档标题仍可真实更新。后续若要正文替换，必须先引入可恢复的持久阶段合同。
- Runtime 默认仍为 `connectors=null`；正式 2.0 CI 显式设置
  `ECOREX_V1_FEISHU_CONNECTOR_ENABLED=true`，并逐个解包 Windows x64、macOS
  arm64/x64 的 Core，断言 endpoint、allowlist 和 `enabled_connectors=[feishu]`。
  服务端仅在 `ECOREX_CP_FEISHU_CONNECTOR_ENABLED=true` 且三个 Secret 全部存在
  时组合 Gateway；否则不建路由、不迁移该增量 schema 并继续失败关闭。

飞书开放平台首次真实验收前必须配置：

```text
Redirect URI:
http://127.0.0.1:8765/api/v1/connectors/oauth/callback

User OAuth scopes:
docx:document
docx:document:readonly
drive:drive:readonly
im:message
im:message.send_as_user
offline_access

Server-only Secret names:
ECOREX_CP_FEISHU_APP_ID
ECOREX_CP_FEISHU_APP_SECRET
ECOREX_CP_FEISHU_TOKEN_ENCRYPTION_KEY_B64
```

飞书应用还必须启用机器人能力、发布一个包含上述权限的应用版本，并把扫码测试
账号加入应用可用范围；发送消息固定使用该用户 OAuth 的 user access token，不与
tenant/bot token 混用。首版只支持部署方在服务端配置的一个自建飞书应用，由飞书
应用自身的可用范围限制可登录租户与用户；不支持租户 BYO App，也不增加租户到
app_id/app_secret 的映射表。真实扫码、凭据注入和生产激活留给有凭据的验收步骤。
正式候选的发布顺序是硬门禁：先向服务端注入 Secret、启用 Gateway 并确认
`/health/ready` 返回 200，再分发已内置 `enabled_connectors=[feishu]` 的桌面端；
Cloud 尚未就绪时不得让客户端可见合同先于服务端上线。
现有冻结 Core 把搜索动作声明为 `drive:drive:readonly`；飞书另有更小的
`drive:drive.search:readonly`，但本次不越过用户设定的 Core 冻结规则修改既有
required-scopes 合同。该最小权限债务需在产品所有者单独批准 Core 合同变更后处理，
验收不得把专用 scope 伪报成已经授予。
当前单节点 Gateway 允许 5 分钟后接管残留的 active 幂等记录，但还没有独立 attempt
owner/lease；这是进程极慢或暂停超过 5 分钟时的重复执行风险。现有写动作继续依赖
飞书 `client_token`/消息 `uuid` 去重，后续若观察到真实超时再在 Product 数据层补租约，
本次不为未观测的多副本场景扩写 Core 或新协调服务。

本轮定向验证：

```text
Gateway OAuth/PKCE/v2 exchange/refresh rotate/四动作/revoke/租户隔离/密文落库：2 passed
manual Runtime config 与既有 managed adapter 回归：16 passed
Control Plane 生产组合（含 Feishu enabled/disabled）回归：20 passed
上述聚焦联合：38 passed
Python compile + git diff check：passed
真实扫码/飞书 API/生产激活：not run（等待用户凭据）
```

# 2026-08-09 — 用户级远程 MCP 自助配置与租户隔离

- 复用既有 `ManagedHTTPMCPTransport`、OAuth 2.1/S256 PKCE、OS Credential
  Vault 与 `MCPClientSupervisor`。未改 Agent planner、Turn、tool dispatch 或
  worker 架构，也没有给用户配置开放 stdio、command、环境变量或动态 Python
  import；首版只接受一个无 userinfo/query/fragment 的显式 HTTPS endpoint，禁止
  本机/私网 IP，连接前再次解析 DNS 并拒绝任何非公网地址，HTTP redirect 继续由
  原 transport 失败关闭。
- 新增 `/api/v1/mcp/servers` Product API，覆盖注册、编辑、删除、真实握手测试、
  启用和停用。测试会执行 MCP `initialize`、`notifications/initialized` 与有界
  `tools/list`，冻结并复用原 Core schema/tool-name 安全合同；用户远程工具统一按
  `read+write+network`、`non_idempotent`、`approval=always` 的保守权限进入现有
  supervisor。目录或连接配置改变后先禁用，重新测试后才允许启用。
- MCP 可执行 Provider 按既有架构只在 Runtime 启动时组成，因此成功变更明确返回
  `restart_required=true` 并调用现有 controlled reload requester，不引入第二条热加载
  路径。用户明确批准的最小 Core 合同例外仅是
  `user-mcp-config-sha256` 与最低优先级 `user_configured` provenance：它只证明本机
  用户授权的精确 HTTPS 配置与冻结工具目录，不能加载本地代码，不能冒充
  administrator 或 Ed25519 发布者签名。
- 非 Secret 配置存入独立 `user-mcp-v1.db`，主键为
  `account_id+organization_id+server_id`。Bearer、OAuth access/refresh token 和动态
  client secret 只进入现有 OS Vault；API 只返回 `credential_configured`，Pydantic
  使用 secret 类型且所有可能携带合法 secret 的约束在服务边界转为固定错误码，
  避免验证错误回显原值。编辑为其他认证类型和删除时清理旧凭据。
- 原 MCP/OAuth namespace 只哈希 `account_id`，同账号跨组织会共用 token/session。
  现统一哈希 `[account_id, organization_id]`，OAuth vault reference、supervisor session、
  circuit/lock 都使用相同复合 namespace；个人账号以固定 `personal` scope 参与哈希。

本轮定向验证：

```text
自助 CRUD/租户隔离/Secret 不落库不回显/HTTPS 与 DNS SSRF/Product mount：6 passed
ManagedHTTP 真握手、目录冻结、原 MCP supervisor 工具调用：passed
原 MCP/OAuth 聚焦回归：27 passed
Extension/Capability provenance、Product Runtime、managed-session 回归：167 passed
Ruff + Python compile + git diff check：passed
真实第三方远程 MCP OAuth：not run（由用户在生产端配置自己的账户后验收）
```

# 2026-08-09 — 消息通道自助连接边界与 CowAgent 复用审计

- CowAgent 2.1.5 的开箱通道链为：全局 `config.json` 读取 `channel_type`，
  `ChannelManager` 用 `channel_factory` 构造平台实例并在线程中运行 `startup()`，
  `wait_startup()` 投影就绪，平台 SDK 自行负责长连接和重连；重启会重建全局实例，
  停止超时还会向线程注入 `SystemExit`。e-Mate 只复用了目录和生命周期语义，没有
  复用明文配置、全局单例、环境继承、首用下载依赖或强杀线程。
- 新增 `/api/v1/connectors/channels` typed Product API，覆盖目录、一次性凭据保存、
  真实连接测试、启用、停用、健康、重试和断开。实例以
  `account_id+organization_id+channel_id` 隔离，公开投影只返回字段是否已配置；配置与
  Secret 一并进入现有 OS 加密 Vault，不进入 SQLite、日志、审计正文或 API 响应。
  生命周期事实经既有 Connector Event Sink 进入审计 outbox，停止失败保留 Vault 记录，
  不会删除仍被运行中 adapter 使用的凭据。
- 服务端按 `CHANNEL_CATALOG` 校验字段并只接受已注入的真实
  `ChannelLifecycleAdapter`。未打包 adapter 的通道目录明确返回
  `adapter_available=false`、`unavailable_reason=adapter_not_packaged`，保存、测试、
  启用、健康和重试全部失败关闭，字段完整不等于连接健康。飞书继续走已有 Product
  OAuth；只有真实 Feishu adapter 已注入时才开放 `auth_begin`。
- 当前签名 v1 `channels` capability pack 只有 Feishu/Tencent Docs contracts，没有
  11 个 CowAgent 消息通道的可执行 adapter；Runtime lock 也没有 `lark-oapi`、
  `dingtalk_stream`、`wechatpy`、`web.py`、`pycryptodome`、
  `python-telegram-bot`、`slack_bolt` 或 `discord.py` 等闭包。因此“11 通道生产可运行”
  仍是发布阻断，必须补齐锁定/供应链审计后的签名 adapter pack 并通过 Runtime 注入，
  不得用首次启动 `pip install` 或字段完整性伪造测试成功。

本轮定向验证：

```text
通道目录/租户隔离/Secret 单向性/真实生命周期/恢复/停止失败保护/审计桥：passed
Channel/Vault/Product Runtime/Connector integration/User MCP 聚焦回归：53 passed
Ruff 0.15.21 + Python compile + git diff check：passed
11 通道真实供应商连接：blocked（签名 adapter/SDK closure 尚不存在）
```

## 2026-08-09 补充 — 通道 Runtime 投递合同与自助配置收口

- 新增 `channel-runtime-dispatch-v1` 产品边界，不引入 Cow `Bridge` 或
  `AgentBridge`。平台入站消息只以组织、账号、通道、外部会话和消息标识的哈希生成
  确定性 Thread/Turn 请求，并复用现有
  `composition.admit_turn -> kernel.create_turn -> worker.notify`；同一平台消息重放不会
  生成第二个 Turn，连续消息复用同一 Thread。外部会话和消息原值不写入 Runtime。
- 出站只读取终态 Runtime projection 中已完成的 assistant message，生成确定性 delivery
  idempotency key，再交给平台 transport。平台 transport 必须先持久去重再确认供应商发送；
  当前没有签名 transport/SDK 的通道继续 `adapter_available=false`，不会因该合同存在而
  伪装可用。
- 正式 Runtime 的 `python311.zip` 只包含 `ecorex`。为避免安装包启动时隐式依赖旧 Cow
  源码树，通道目录在 `ecorex.connectors` 内保留运行时安全投影，并用测试逐项锁定
  CowAgent 2.1.5 的通道顺序、别名、名称、说明、图标和字段合同。
- 已启用通道必须先停用才能修改配置或密钥；文本字段不再把数组、对象等结构值静默
  转成字符串。前端同步禁用运行中编辑，并移除配置面板与通道卡片重复显示的健康状态。
- 远程 MCP 的公网检查从“连接前 DNS 预检”加强为 socket connect 时重新解析、拒绝
  混合/私网答案并连接到已验证 IP，TLS 仍以原 hostname 验证，关闭 DNS rebinding 的
  检查到连接时间差；远程测试结果同时按配置 revision 写入，不能覆盖测试期间发生的
  配置变更。

补充验证：

```text
Channel self-service + Runtime dispatcher + User MCP：18 passed
上述功能与 MCP/Extension/managed-session 10 文件联合回归：91 passed；2 个 Skill 搜索断言在未修改的
b7a89c1 基线也失败，已隔离复现，未越权修改冻结 Skill Core
RuntimeClient：53 passed
TypeScript + Runtime contract codegen check：passed
Web production build/bundle gate：passed
通道与远程 MCP GA Chromium：2 passed
ecorex-only python311.zip 隔离导入：passed
```

## 2026-08-09 补充 — GitHub 发布网络通道

- 按产品所有者要求，后续 GitHub API、Git push、Actions 制品上传与下载优先走
  本机系统代理 `127.0.0.1:7993`，避免大制品直连短读和超时。已完成的原子文件不重复
  下载；只有未完成连接和 Range 重试切换到代理。
- PAT 仍只在进程内存中使用。GitHub API 请求可携带授权头；重定向到对象存储的制品
  下载不得转发 PAT。所有下载继续以 API size、ZIP CRC、SHA-256 和签名验证为完成
  条件，代理可用性不会放宽任何完整性门禁。

## 2026-08-09 补充 — Telegram 用户自配消息通道

- 在既有 `ChannelSelfService`、`ChannelRuntimeDispatcher` 和 Agent worker 上增加
  Telegram Bot 长轮询 transport；没有新 Runtime、旧 Bridge 或本地公网 listener。
  Bot Token 只经 OS Credential Vault 单向保存，Product 未登录或 acceptance preview
  不注入 adapter，无 Agent worker 时组合直接失败关闭。
- 连接测试真实调用 Telegram `getMe` 与 `getWebhookInfo`。发现用户已经设置 webhook
  时返回 `telegram_webhook_active`，绝不自动删除用户原有 webhook。启用后以最长 2 秒
  `getUpdates` 持久推进 offset，入站消息进入同一 Runtime Thread/Turn；终态回复经
  `sendMessage` 发送。
- transport-private SQLite 按 `organization_id+account_id` 哈希分区并使用 0600 权限，
  仅它保留投递所需 chat id；Runtime 事实只保存会话/消息哈希。出站按确定性 key
  持久去重，供应商响应不确定时标记 uncertain 并停止盲重试；网络断开投影为降级并
  有界退避，停止时间保持在 5 秒生命周期门禁内。

定向验证：

```text
Telegram transport/lifecycle/tenant/idempotency/webhook/Runtime/Product/preview：46 passed
Python compile + git diff check：passed
真实 Telegram Bot Token：not run（由用户在生产客户端自行配置后验收）
```

## 2026-08-09 补充 — 飞书、钉钉、Slack 与 Discord 用户自配消息通道

- 飞书继续使用 Cow 兼容的 `feishu` 通道 ID，但不再把消息 Bot 错分为 OAuth。
  ConnectorService 仍独立管理“文档与云空间授权”；ChannelSelfService 管理“消息 Bot”
  的企业自建应用 App ID 与 App Secret，同一卡片分区展示且凭据互不复用。消息 Bot
  使用官方 `lark-channel-sdk==1.2.0` WebSocket 通道，官方 wheel SHA-256 为
  `c08690572a099377cdeddc3a2a1402d9645879ad137e780d80060053dc8c1570`；MIT、
  内含 Protocol Buffers BSD-3-Clause 声明、四套 Python 锁和 Runtime 闭包均已固定。
- 钉钉复用 CowAgent 2.1.5 已验证的 Stream Mode open/sessionWebhook 协议，但将
  入站消息接入唯一 ChannelRuntimeDispatcher，不复用旧 Bridge。实现直接使用已锁定的
  httpx 与 websockets，未为同一 wire contract 增加 aiohttp/官方 SDK 依赖；回调先写入
  0600 租户 journal 再 ACK，sessionWebhook 仅接受钉钉官方 HTTPS 主机。
- Slack 使用官方 Socket Mode 合同：xapp token 只建立
  `apps.connections.open` WebSocket，xoxb token 只用于 `auth.test` 与
  `chat.postMessage`。事件先进入 0600 租户 journal 再 ACK，重放不重复创建 Turn；
  断线重新申请 Socket URL，一次启动只创建一个 Socket，发送响应不确定时不盲重试。
- Discord 使用 Gateway v10 与 REST v10；只接收私聊或明确提及 Bot 的群消息，不申请
  `MESSAGE_CONTENT` 特权 Intent。Gateway session/sequence、入站去重与 delivery nonce
  持久化在 0600 租户数据库；心跳 ACK、Resume、供应商重连和有界停止均复用同一
  ChannelRuntimeDispatcher，不引入公网 listener 或第二 Runtime。
- 企微智能机器人使用官方 AI Bot 长连接合同
  `wss://openws.work.weixin.qq.com`，通过 `aibot_subscribe` 校验用户 Bot ID/Secret，
  处理 `aibot_msg_callback` 并用 `aibot_send_msg` 回发终态 Markdown。被新的客户端接管
  时按 `disconnected_event` 停止，不与新连接争抢；文本、语音转写与混合消息文本进入
  Runtime，当前 Runtime 不支持的图片/文件消息保持忽略且不生成虚假占位。
- QQ 机器人通过官方 AccessToken、Gateway WebSocket 和被动消息 REST 合同接入 C2C
  与群聊 @ 消息。出站按官方 5,000 UTF-16 单元分块，C2C 最多 4 块、群聊最多 5 块，
  `msg_seq` 从 1 稳定递增；超过官方总容量在首次发送前失败关闭，不截断、不切换主动
  消息。每块使用独立持久 delivery key，后续块响应不确定时不会重发已确认的前块。
- 三个平台的 Secret 都只经现有 OS Credential Vault 单向保存；Runtime 仅保留外部会话
  与消息标识哈希，原始平台会话标识只存在于各 transport-private 数据库。Product 只在
  已认证且 Agent worker 可用时注入真实 adapter；acceptance preview 和未登录状态继续
  失败关闭。

定向验证：

```text
Feishu/DingTalk/Telegram/Slack/Discord/WeCom Bot/QQ + ChannelSelfService + Runtime dispatcher + Product：passed
DingTalk + ChannelSelfService + Runtime dispatcher：16 passed
QQ transport + Runtime dispatcher（含 UTF-16 分块/回复次数/不确定恢复）：12 passed
飞书官方 SDK 在锁定 httpx 0.27.2 / websockets 15.0.1 闭包内导入与配置：passed
真实平台凭据与供应商消息往返：not run（由用户在生产客户端配置后逐平台验收）
```

## 2026-08-09 补充 — 微信系回调与扫码通道生产边界

- 通过强制代理 `127.0.0.1:7993` 逐文件核对 CowAgent 2.1.5
  `e3ac1b952500f60934862c6bf0bd0de91b415ed8`。其企微自建应用、微信客服、公众号和
  公众号客服都在本机绑定 `0.0.0.0` 等待公网回调，并把平台密钥写入全局
  `config.json`；这不是桌面安装后即可获得的公网能力，e-Mate 不复用本机公网监听、
  明文密钥或旧 ChannelManager。
- 当前四个回调通道在没有受管 HTTPS ingress、provider 签名/AES 校验、opaque binding
  的租户路由、落盘后应答、离线 inbox 和出站幂等之前保持
  `adapter_not_packaged`。微信客服还需要按 `open_kfid` 持久 cursor；公众号被动回复
  只能有界等待真实 Runtime 终态或直接 ACK，不能照搬旧实现的“思考中”假进度；同一
  App ID 的公众号被动模式与客服模式必须互斥。
- 微信个人号 `weixin` 使用 iLink 设备扫码和长轮询，不需要公网回调，但现有自助通道
  合同尚缺 begin/poll/cancel/refresh/confirmed 设备授权动作和 QR 投影。在同一
  ChannelSelfService 补齐该合同之前继续失败关闭；不得另走 ConnectorService 形成两个
  身份/状态源。后续 token 只能进入 OS Vault，cursor 与最新 `context_token` 只能进入
  组织和账号分区的 0600 transport 数据库。
- 前端对上述未形成闭环的通道只显示非交互状态说明，移除了“暂不可连接/暂不可配置”
  等禁用按钮；可执行按钮只在后端真实 action 为 true 时出现。

定向验证：

```text
微信系未打包通道失败关闭（无实例、无 action、拒绝写入凭据）：1 passed
真实微信/企微/公众号凭据与公网回调：not run（需受管 ingress 与用户自有账户）
```

## 2026-08-09 补充 — 外部通道 e-Mate 身份与微信闭环

- 外部通道不再继承 CowAgent 身份。Telegram 在启用时通过官方 `setMyName` 把 Bot
  名称设置为 `e-Mate`；Discord 在启用时通过官方 `PATCH /users/@me` 校验并设置 Bot
  用户名为 `e-Mate`。两项改名都属于连接成功的硬条件，供应商拒绝时不会继续伪装成
  已连接。
- 飞书、钉钉、Slack、企微智能机器人、QQ 及微信系平台没有与当前用户凭据相匹配的
  受支持改名 API，客户端在保存连接前明确要求用户在对应平台的应用、机器人或账号
  资料中把显示名设为 `e-Mate`；不得通过消息正文前缀伪造发送者名称。微信/企微受管
  回调绑定返回同一 `external_display_name=e-Mate` 设置要求，Secret 不进入响应。
- 微信个人号已在同一 ChannelSelfService 补齐 iLink begin/poll/cancel/refresh/confirmed
  设备授权合同和真实二维码投影，确认后 token 只写入 OS Vault，cursor、最新
  `context_token` 与投递幂等事实仅写入租户 0600 transport 数据库；`-14` 会强制重登。
- 企微自建应用、微信客服和公众号客服改由受管 HTTPS callback gateway 完成签名/AES
  校验、opaque binding 租户路由、durable inbox/lease、KF cursor、出站幂等与审计
  outbox，再投影到唯一 ChannelRuntimeDispatcher。公众号被动回复仍因真实终态时间窗
  不可保证而保持失败关闭，不生成“思考中”假进度。

定向验证：

```text
Telegram + Discord 外部身份、transport 与投递回归：12 passed
微信 iLink 后端聚焦：17 passed；Renderer TypeScript/语言合同/GA 路径：passed
微信回调 schema/gateway/catalog：11 passed；Server Product + Control Plane：40 passed
产品语言合同：12 passed；Ruff + git diff check：passed
真实平台账号显示名与消息往返：not run（由用户在生产客户端配置或扫码后验收）
```

## 2026-08-09 补充 — 桌面 Feed receipt 围栏激活器

- 新增独立的 `scripts/deploy-emate-desktop-feed.py`，只接受
  `status=activation-ready` 的 `feed-stage-receipt.json`。激活前逐项核对版本、源码提交、
  release/build/manifest/feed 身份，并验证候选目录完整 inventory 的路径、角色、size 与
  SHA-256；候选树中的额外文件、链接/reparse、越界路径、跨文件系统目标均失败关闭。
- 激活复用 `ecorex.update.locking.ProductFileLock`，用候选目录的相对 symlink 经临时路径
  `os.replace` 原子切换 `current`。显式 command 或 HTTP(S) readback 必须返回与候选
  `public-bootstrap-index.json` 完全一致的字节；失败时仅在 `current` 仍指向本候选的围栏
  成立后原子恢复 previous，并输出严格七字段 activate/rollback receipt。
- 本次仅新增本地部署执行器、聚焦测试和本记录；没有调用签名器、生成 public index、
  连接生产、部署或切换 `/srv/e-mate-update/current`。

定向验证：

```text
e-Mate feed activation/rollback/inventory/HTTP readback：8 passed
Ruff + git diff check：passed
生产 readback 与 current 切换：not run（本次范围明确禁止连接生产）
```

## 2026-08-09 补充 — 下载页最终文案与桌面发布前置门禁

- 下载页按最终验收稿删除“每次继续/上次的进度”标题，主标题固定为
  “Agent工作新范式”，副文案保留“从自己干到通过agent快速落地想法。”；产品展示继续使用
  当前真实 e-Mate 桌面截图。下载按钮和三平台版本只读取受校验的
  `download-index.json`，没有把版本号写入页面代码。
- 设备识别新增移动 Apple 设备失败关闭：iPhone、iPad、iPod 不再误判为 macOS；未知设备
  展示三平台选择而不自动下载错误安装包。重建后的脚本使用内容哈希
  `site.be768b5d99e6.js`，旧哈希入口已删除。
- `emate-2.0-desktop-release.yml` 在签名 Runtime 与三平台打包之前新增源码/交互门禁：下载页、
  Feed 激活器、11 个真实通道组合、公众号被动模式失败关闭、TypeScript 合同、GA mock、产品
  语言以及“定时任务→外部通道”Playwright 路径。测试只使用临时目录，不调用生产 Feed
  激活命令。
- Codex 内置 Browser 本轮返回 `Browser is not available: iab`，本机 8765 也没有可信 Runtime
  监听，因此没有伪造真实 UI 通过记录。公开站点与更新路径仍由旧 SPA 回退为 HTML；在
  public bootstrap 与 Feed 达到 `activation-ready`、公开回读逐字节通过前不得切换 `latest`。
- 前端全量合同复跑时同步清理了三处漂移：图片 Gallery 的预览媒体按钮作为结构化媒体控件
  进入密度门禁例外；设置分区导航继续复用统一控件基线；MCP 空态合同改为验证当前真实的
  `UserMCPPanel` 读取、测试后启用链路，不再引用已删除的旧扩展目录分支。

定向验证：

```text
下载页合同/静态门禁：5 passed；hashed assets=10
Feed 激活/回滚：8 passed；通道 Product 组合：3 passed
GA mock + 产品语言：23 passed；TypeScript：passed；目标 Playwright：1 passed
Workflow YAML/依赖锁/飞书部署合同：passed
桌面 v1 全量合同：237 passed
真实 Browser/桌面安装态：not run（Browser backend 与可信 Runtime 均不可用）
```

## 2026-08-09 补充 — 生产 Schema 与 Usage/Audit 只读对账

- 通过仓库既有受限生产 operator 在主机 loopback 执行只读回读；未读取或输出 Secret、用户
  明细、模型 Key，未执行迁移、服务重启或流量切换。当前云端活动槽为 `blue`，release
  `emate-cloud-v2.0.0-95df539`，源码提交
  `95df539e63e681299ff8c844d7e60b91082936a5`；Control Plane、Gateway、Image API 和
  Image Worker 均为 active，两份 SQLite `quick_check=ok`。
- 生产 Schema 当前为 Control 1、Admin Management 6、Device Identity 2、Gateway 4；
  当前源码要求 Admin Management 7，且 Connector Gateway 与微信回调 Schema 尚未部署。
  旧基线数据没有减少：用户 41、管理审计 300、Provider facts 179、Gateway requests 223、
  model attempts 6；租户策略 41。该证据只证明数据保留与现有服务健康，不能替代 v7 迁移和
  Connector/微信真实凭据验收。
- Usage Panel 仍指向 `20260805223000-v0.3.0-emate-c5ed3e1a`，服务版本 1.0.5。对
  `[2026-08-01T00:00:00+08:00, 2026-08-10T00:00:00+08:00)` 执行同窗
  `/api/data` 与 `/api/runtime-audit` 对账时正确失败关闭：Usage 为 256 tasks /
  1,671,474 tokens，Audit 为 396 tasks / 888,638 tokens；canonical records 分别为
  411 与 303，KPI 和 reconciliation 均不相等。发布终门禁保持阻断，必须先部署当前
  Usage 投影、完成 Admin v7 增量迁移并重新生成完全相等的脱敏 receipt，禁止用历史
  2026-08-04 对账结果冒充本次通过。

定向验证：

```text
生产四服务 active；Control/Gateway SQLite quick_check：passed
历史计数不下降：passed
Admin v7 / Connector Gateway / 微信回调 Schema：not deployed
当前 Usage/Audit 同窗对账：failed closed（projection_mismatch）
```

## 2026-08-09 补充 — 未签名 macOS 首次安装 Runtime 闭包修复

- 经用户动作时确认后，用 Computer Use 分别以现有数据目录和全新隔离 HOME 启动 CI 的
  `e-Mate.app`。两次都在登录前由 Bootstrap 正确失败关闭；没有出现 Keychain 密码框，
  没有改写现有 `~/.emate`。直接复现 `ecorex.bootstrap.install_local` 捕获到首个原始异常为
  `ModuleNotFoundError: qrcode`，不是 Gatekeeper 或空的 macOS sandbox helper identity。
- 根因是 2.0 手工 WebUI builder 只替换 predecessor `python311.zip` 中的 `ecorex/`，但新增
  微信扫码和飞书消息通道依赖没有进入签名 Core。builder 现在从已安装的 hash-lock 版本覆盖
  纯 Python 的 qrcode、lark-channel、requests 依赖，并按目标平台使用同一
  `runtime.lock --require-hashes` 提取 PyCryptodome；不在首启联网安装依赖。
- 微信二维码依赖改为授权动作内按需导入，避免任何可选通道依赖再次阻断 Bootstrap 安装器。
  与宿主架构匹配的签名 Core 在构建签名前必须隔离导入安装器、lark-channel、qrcode，并
  实际生成 PNG data URL；失败则不产生 Runtime seed。

定向验证：

```text
原 CI macOS arm64 首启（现有/全新 HOME）：均失败关闭，原始根因 qrcode missing
修复后 predecessor Core 本地覆盖探针：install_local/lark_channel/qrcode import passed
修复后二维码 PNG data URL：passed
macOS arm64/x64 + Windows x64 hash-lock wheel resolution：passed
微信 + Bootstrap 聚焦回归：10 passed
Ruff / py_compile / git diff check：passed
新签名三平台候选与 Computer Use 复验：pending CI rebuild
```

## 2026-08-09 补充 — 未签名 macOS 改用 e-Mate 本机加密凭据库

- `5e240d7` 的新签名 macOS arm64 候选再次完成 Bootstrap 健康确认，但 Runtime 首次写入
  新 e-Mate Keychain 服务时得到 OSStatus `-60006`。本机 SDK 与 `security error` 均确认该
  状态是“授权被用户取消”；界面没有出现可接管的密码框，因此未继续依赖系统钥匙串。
- 按用户确认，正式 macOS 产品组合改为 e-Mate 数据目录内的本机加密凭据库。凭据正文使用
  现有 AES-GCM 文件 Vault，密钥与密文分别以 owner-only `0600` 文件持久化，原子替换、
  `O_NOFOLLOW`、单硬链接与长度门禁继续生效；前端、错误和日志不返回 Secret。CowAgent 的
  明文 `config.json` 方案没有复用。
- 改动只发生在 macOS Product 凭据组合边界；Windows Credential Manager、Agent Runtime、
  通道/MCP/审计合同均未改。旧 CowAgent 本地模型 Key/Web 密码仍不迁移。

定向验证：

```text
本机加密 Vault 重启恢复、密文无明文、key/vault 权限 0600：passed
macOS Product 默认组合不调用 Keychain：passed
聚焦回归：2 passed
新签名候选首次安装与 Computer Use：pending CI rebuild
```

## 2026-08-09 补充 — 生产 Usage/Audit 2.0 同窗对账完成

- 上一轮 2.0 Usage Panel 替换后能用纯日期完成同投影验证，但正式对账脚本传入带时区的 ISO
  值；URL 中 `+08:00` 经 query 解码成为空格。`runtime-audit` 原本截取日期前十位，`data`
  却把完整字符串判为非法并静默回退到 6 月默认窗口，造成两个端点比较了不同日期。根因不是
  ledger 漂移或生产数据丢失。
- `build_data_request_payload` 现在与审计端点使用相同的日期前缀合同。生产 deployer 不再
  硬编码旧 1.0.5/v0.3.0 标识，改由 operator 显式传入预期产品版本与投影，并在回滚 receipt
  中保留固定、脱敏的 schema/HTTP/KPI 失败码。
- 生产源已通过备份、原子替换、systemd 重启、loopback 健康与同窗核对后保持激活；未触及
  Control Plane/Gateway 数据库或流量路由。最终时间窗
  `[2026-08-01T00:00:00+08:00, 2026-08-09T00:00:00+08:00)` 的 Usage 与 Runtime Audit
  完全相等：395 tasks、887,608 tokens、302 canonical records，余额 mismatch/delta 均为 0。
  `missing_provider_usage_count=42` 与 `unassociated_record_count=203` 是被显式保留的历史覆盖
  缺口，不被伪装成完整 Provider 归因。

定向验证：

```text
Usage/Audit ISO 日期合同与 deployer 当前版本合同：2 passed
生产部署 receipt：status=passed；rolled_back=false；projection=e-mate-2.0-usage-1
生产同窗 receipt：usage_audit_match=true；KPI/reconciliation exact match
生产余额对账：account_balance_mismatch_count=0；token/image delta=0
Ruff / git diff check：passed
```

## 2026-08-09 补充 — e-Mate OS 凭据命名空间首次启动修复

- CI run `31318549117` 的 Runtime seed、Windows x64、macOS arm64/x64 与 Feed 汇总五个
  job 全部通过；macOS arm64 Actions artifact SHA-256 为
  `71a2b89c79289ac82286fdfb99fc3977857a8c63d7ff73337c5170d151f28a69`。解包后应用为
  arm64、版本 2.0.0、bundle id `net.ecoremedia.emate`、ad-hoc 签名；内嵌 Runtime manifest
  与 15 个目标平台 Artifact 的 Ed25519/文件校验全部通过。
- 经用户确认后，Computer Use 在全新隔离 HOME 真实运行该未签名应用。Bootstrap 完成
  manifest、Artifact、slot 与启动健康校验，journal sequence 12 为
  `bootstrap_health_confirmed`；随后 Runtime 在 `runtime_registration` 失败关闭。
- 对同一签名 slot 做不落 Secret 的诊断复现，原始异常为 `credential vault write failed`。
  本机 Keychain 已有旧服务 `com.ecorex.connector-credentials` 的同一审计引用，而新的
  e-Mate 服务尚不存在；未签名新 Runtime 无权更新旧项。Win/mac 生产 Vault 现在使用
  `e-Mate:`、用户名 `e-Mate` 与 `net.ecoremedia.emate.connector-credentials`，不迁移或
  读取旧本地密钥，也不改 CredentialVault 架构。

定向验证：

```text
Connector Vault 聚焦回归：7 passed
git diff check：passed
新签名三平台候选与 Computer Use 复验：pending CI rebuild
```
