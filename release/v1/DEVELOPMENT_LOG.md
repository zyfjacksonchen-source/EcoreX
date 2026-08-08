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
- `DynamicManagedImageProvider` 当前会把底层 Provider 的 `usage.model_id`
  规范化为本地模型槽，`ProviderResult` 没有明确的 fallback provenance。
  本轮未获得修改 Provider/Core 合同的授权，因此绝不通过模型 ID 差异推测
  `gpt-image-2-pro → gpt-image-2`：记录 `requested_model_id`、
  `provider_reported_model_id` 和 `actual_provider_id`，`actual_model_id`、
  `fallback_from_model_id`、`fallback_used` 保持 NULL/“未公开”。若后续需要真实
  降级对账，必须另行明确授权 ProviderResult 增加不可歧义的 provenance 合同。
- 现有 Usage 面板继续用完整 provider fact ledger 对 lifetime token/image 余额
  对账；所选日期范围同时投影图片明细，包括租户、Job、请求槽、Provider 报告值、
  实际模型公开状态、降级公开状态和 Job/Result 状态。旧 v5/简化表事实仍可查询，
  NULL 降级状态保持 unknown，不转换成 false。

本轮定向验证：

```text
管理 schema / exact-once / 图片逐 Job / Usage 投影：27 passed
管理 schema 相关回归：134 passed，8 skipped
Python compile：passed
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
