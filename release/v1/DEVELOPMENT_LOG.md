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
