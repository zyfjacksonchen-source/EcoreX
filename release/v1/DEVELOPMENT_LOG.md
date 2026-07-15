# v1 update-chain development log

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
