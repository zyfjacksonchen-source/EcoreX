# v1 update-chain development log

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
