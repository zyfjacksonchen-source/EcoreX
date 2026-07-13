# EcoreX v1.0 release runbook

This runbook is the administrator-facing Product path. User machines never run
Git, npm, pip or a release script. Tokens stay in the release service/CI
environment and are never written into this file or process arguments.

## One-time publication configuration

Copy `release/v1/publication-config.example.json` to an administrator-owned
location and replace every `.invalid` host plus the GitHub owner/repository.
The checked-in file contains no credential values; it only names the
environment variables from which credentials are read at request time. The
strict companion contract is `release/v1/publication-config.schema.json`.
Source IDs and public hosts must match the three sources in every signed
release manifest.

The mirror/CDN endpoints are upload control roots, not public download roots.
Each service returns the public asset URL; the coordinator rejects it unless it
exactly equals the corresponding signed manifest source plus the asset name.

## Candidate asset publication

Export the three credentials through the variable names above, then run one
resumable command. `--trusted-key` contains a public verification key, not a
secret.

```powershell
ecorex-release publish-assets `
  --release-dir C:\releases\1.0.0 `
  --publication-config C:\ecorex-admin\publication.json `
  --receipt C:\ecorex-admin\receipts\1.0.0-publication.json `
  --trusted-key release-2026=BASE64_ED25519_PUBLIC_KEY `
  --publish-github
```

The command verifies the exact local directory and all signatures/digests,
then performs domestic mirror, GitHub draft, CDN and GitHub visibility in that
order. Re-running resumes matching assets. A conflicting same-name asset or any
receipt mismatch stops the command; it never overwrites remote bytes.
The atomic receipt records the exact manifest digest and three validated source
projections; its SHA-256 is printed for the Control Plane release-gate evidence.
The `github-release`, `mirror-sync`, and `cdn-sync` entries in
`release-evidence.json` must all use the identical value
`publication-receipt:sha256:<printed receipt SHA-256>`. Promotion reopens that
receipt, verifies its release/manifest identity, exact three source URLs and
cross-origin asset digests, and refuses unrelated or draft-only evidence.

## Control Plane Web console deployment boundary

The public Bootstrap site does not contain an administrator application and
does not proxy any user's local Agent Runtime. `/admin/` and
`/api/v1/admin/*` are reverse-proxied to the v1 Control Plane service on its
loopback listener; the Control Plane serves the content-addressed console and
rechecks the in-memory administrator bearer on every mutation. The checked-in
Caddy/Nginx examples use `127.0.0.1:18084` as that listener.

The service deployment must inject a real `ControlPlaneAuthenticator`, trusted
release verifier and persistent Control Plane repository. The built-in
rejecting authenticator is intentionally not a development bypass. Do not
restore the removed v0.3 static admin, Basic-Auth SQLite monolith, usage panel,
`/message`/`/upload` proxy, or public port 9909 Runtime route. Those were a
second release authority and are rejected by both the legacy cutoff and public
site gates.

`upload-github` is a recovery/diagnostic primitive and always leaves a draft.
It cannot make a public release or bypass replica readiness.

## Public Bootstrap discovery page

The repository deliberately ships
`deploy/ecorex-site/public-bootstrap-index.json` as a canonical
`unpublished` document with `release: null`. It contains no provisional URL,
signature or “ready” state. After `publish-assets` has made the same bytes
available at all three origins, generate the live discovery pointer from that
immutable receipt:

```powershell
python scripts/build-v1-public-download-site.py
python scripts/check-v1-public-download-site.py

ecorex-release build-public-bootstrap-index `
  --release-dir C:\releases\1.0.0 `
  --publication-receipt C:\ecorex-admin\receipts\1.0.0-publication.json `
  --output C:\srv\ecorex-agent-download\next\public-bootstrap-index.json `
  --trusted-key release-2026=BASE64_ED25519_PUBLIC_KEY
```

The command reads the exact manifest through one bounded stable file
descriptor, proves its byte SHA-256 against release metadata and the
publication receipt, verifies the manifest plus the three supported Bootstrap
signatures, and then performs a locked same-directory atomic replace. A failed
parse, verification, schema check or replace leaves the previous pointer
untouched. Persistent process-lock files live outside the served directory, so
the public site cannot expose a lock artifact alongside discovery JSON.

The asset builder writes new digest-named JS/CSS before atomically rebinding
`index.html`, then removes the old names; rerunning also cleans an unreferenced
asset left by a crash before the HTML switch. `index.html` and
`public-bootstrap-index.json` must be served with `no-store`.
The content-addressed JS/CSS/images and immutable release assets use a one-year
immutable cache. The page retries the three manifest origins and compares the
exact response bytes with the projected SHA-256 before it renders download
links. This browser check detects stale/corrupt origin bytes but is not a trust
root: the downloaded Bootstrap must still verify Ed25519 signatures with its
embedded keys before it installs anything.

## Control Plane promotion

After CI has produced exact evidence for every required gate, promote the same
signed manifest. The journal makes retries reuse the same request identities.
Journal schema v3 also binds a domain-separated digest of the complete rollout
target: channel, percentage, sorted organization/account sets and minimum
compatible version. Reusing a journal with different target parameters fails
before any Control Plane request rather than silently reusing an old rollout.

```powershell
ecorex-release `
  --endpoint https://control.example/api/v1/admin `
  --allowed-host control.example `
  promote `
  --manifest C:\releases\1.0.0\release-manifest.json `
  --evidence C:\releases\1.0.0\release-evidence.json `
  --publication-receipt C:\ecorex-admin\receipts\1.0.0-publication.json `
  --journal C:\ecorex-admin\journals\release-stable.json `
  --percentage 1 `
  --activate
```

Activation publishes the rollout signal. Clients receive it over their outbound
WSS or the five-minute poll fallback, download in the signed source order and
wait at `awaiting_user`. Only the user's “更新并刷新” action drains/checkpoints
work, switches the side-by-side slot and reloads the unchanged local URL.

## CI evidence and release boundary

Every pull request and `main` change runs `.github/workflows/ecorex-v1-ci.yml`.
The quality job executes the supported local commands:

```powershell
python scripts/install-v1-python-profile.py --profile dev
python scripts/check-v1-dependency-locks.py
python scripts/check-v1-source-tree.py
python scripts/run-v1-lint.py --compile
python -m pytest -q
cd desktop
npm ci
npm audit --audit-level=high
npm run typecheck
npm run test:v1
npm run build
npm run test:e2e
cd ..
python scripts/check-v1-reproducibility.py --web-dist desktop/dist
```

Windows x64, macOS arm64 and macOS x64 compatibility jobs build the same
WebUI/Runtime tree; they do not build a native desktop application.
Together with Ubuntu they upload timestamp-free canonical byte contracts; the
final CI job rejects any checkout, JSON, HTML or JS/CSS digest difference. The
runner mapping follows GitHub's official
[hosted-runner reference](https://docs.github.com/en/actions/reference/runners/github-hosted-runners):
`windows-latest` is x64, `macos-15` is arm64 and `macos-15-intel` is x64.

This workflow is deliberately read-only. It does not receive release-signing
or origin credentials, create Releases, publish a pointer, or state that a
Runtime archive is Ed25519-signed or installed. Native-app signing,
notarization, DMG and Electron outputs are outside v1 scope. A green
`npm audit` also does not replace SBOM, license or secret scanning.
Administrators must attach those exact candidate results plus Windows/macOS
WebUI Runtime install/update/rollback evidence before promotion.

The protected Candidate adds gates that ordinary read-only CI cannot claim. It
fetches and verifies the complete fixed v0.3.0 Git object set, executes the
full copy-on-write/Product/quarantine/released-schema/activation-rollback
migration contract, and starts isolated digest-pinned PostgreSQL 16.9 and MinIO
for a 256-job/48-worker/two-node-ID image run. A dedicated
`ecorex-image-soak` protected runner repeats that real integration for at least
four hours. If that runner is unavailable, Candidate construction remains
blocked; there is no allowed skip receipt. Browser E2E and image evidence are
converted to release gates only after they are bound to the signed Candidate's
commit, workflow run, `release_id`, `build_digest` and Windows platform-stage
receipt set.

Candidate dispatch also requires `ci_run_id` and `ci_run_attempt` from one
successful, completed protected-main `EcoreX v1 CI` run for the exact Candidate
commit. A read-only provenance job downloads exactly the four named Ubuntu,
Windows x64, macOS arm64 and macOS x64 byte-contract artifacts from that run.
It rejects a stale/foreign/PR/fork run, a different attempt, missing or extra
artifacts, links/hardlinks/TOCTOU changes and any cross-runner byte difference.
Artifact-list timestamps must fall within the selected attempt, and downloads
use the four validated immutable Artifact IDs rather than names. The evidence
retains each ID, archive digest and timestamp as well as each downloaded
byte-contract digest.

Do not use a partial-job rerun as Candidate evidence: GitHub can retain
artifacts from jobs that were not rerun, and EcoreX deliberately rejects that
mixed-attempt set. Use **Re-run all jobs** (which replaces the complete
four-artifact set) or dispatch a new CI run, then pass that exact successful
attempt to Candidate.
Only after Candidate signing is that typed result bound to the signed Candidate
receipt, manifest and Web tree. The unbound comparison cannot mint the required
`reproducibility` release gate.

Python resolution is repository-owned under `requirements/locks/`. CI and
Candidate jobs install the `bootstrap` lock and exactly one hash-locked profile,
require wheels, then install the checked-out EcoreX source with `--no-deps` and
`--no-build-isolation`. The gate rejects floating `pyproject.toml` dependencies,
missing hashes, VCS/path requirements, npm lock drift, non-`npm ci` workflow
installs and unpinned GitHub Actions. Candidate Python is fixed to 3.11.9 and
the Web build uses the exact Node.js 22.23.1 LTS toolchain;
the installer refuses a different patch-level interpreter because the platform
stager packages that interpreter into the Core. The platform matrix is also the external
wheel-availability gate: if the exact Windows x64, macOS arm64 or macOS x64
wheel closure cannot be installed, staging fails before any receipt is issued.

## Protected automated Candidate chain

The protected chain has two deliberate administrator boundaries:

- `.github/workflows/ecorex-v1-candidate.yml` builds, externally signs and runs
  protected Model/Image/CDP acceptance. It can only emit an immutable
  `ecorex-v1-accepted-<channel>` Artifact; it has no origin or Control Plane
  publication job.
- `.github/workflows/ecorex-v1-promote-candidate.yml` is the sole remote
  publication entrypoint. It accepts the exact successful Candidate run ID,
  attempt and accepted Artifact ID, authenticates all three against the current
  protected commit, then waits at `ecorex-release-publication-<channel>` for the
  administrator's separate publication approval.

Both workflows are `workflow_dispatch` only, hold channel-wide non-cancelling
concurrency locks, require `github.ref_protected` and have no PR trigger.
Configure `ecorex-release-signing-canary/stable`, `ecorex-live-acceptance` and
`ecorex-release-publication-canary/stable` as protected GitHub Environments.
An approval to create/test a Candidate is never authority to mutate an origin
or activate users.

The Candidate dispatch names immutable upstream evidence:

- `channel=canary`;
- `staging_run_id`: the successful protected platform-stage run for the same
  commit;
- `ci_run_id`: the successful protected `EcoreX v1 CI` run for the same commit;
- `ci_run_attempt`: the exact successful attempt of `ci_run_id`.

After the accepted Artifact exists, dispatch the publication workflow with its
exact `candidate_run_id`, `candidate_run_attempt` and `candidate_artifact_id`.
Its default `publication_mode=verify-only` performs no remote mutation.
`create` publishes exact bytes and creates a paused 1–100% rollout;
`create-and-activate` is the only option that activates that rollout. The
selected Artifact archive and the second same-run handoff archive are fetched
by immutable Artifact ID, checked against their SHA-256 values and safely
extracted. Digest mismatch, expiration, duplicate names, mixed attempts,
symlinks, case-colliding members, path traversal, insufficient disk or an
unexpected root fails before publication.

Before Candidate dispatch, run
`.github/workflows/ecorex-v1-platform-stage.yml` on the same protected commit.
Its three protected self-hosted runners invoke the repository-owned
`platform-staging/stager.py` through a wrapper that binds the exact interpreter
and adapter SHA-256 before and after execution. The stager must emit real
redistributable Runtime trees and all six required Capability Pack trees;
the runner wrapper records content-bound typed receipts only after the fixed
platform probes pass. Missing launchers, missing Pack code/contracts, symlinks,
changed bytes or placeholder directories produce a typed failure receipt and
fail the workflow. The Candidate verifies the staging workflow ID, exact
commit, same repository, `workflow_dispatch` origin, empty PR association and
successful conclusion before downloading its artifacts.

Each stage must also attach a passing per-tree supply-chain receipt. This is
where bundled Python/native/browser/provider dependencies are inventoried and
license/secret/SBOM policy is enforced; the repository scan alone is not
accepted as evidence for opaque platform payloads.

The Candidate always contains:

- Windows x64, macOS arm64 and macOS x64 WebUI Runtime Core archives;
- one dependency-free Bootstrap archive for each of those three targets;
- browser, channels, image, OCR, Office and sandbox Capability Packs for all
  three targets;
- a single content-addressed React build bound into every product Core;
- signed Pack sidecars, CycloneDX SBOM, release metadata and manifest;
- immutable stage/build/signature/supply-chain evidence receipts.

The external KMS/HSM adapter is configured only through protected Environment
variables. Both its host executable and optional adapter file are rehashed
before and after every signing call. Exact canonical payload bytes travel over
stdin; stdout may contain only one raw Ed25519 signature encoded as Base64.
Private keys are never accepted as a CLI input, file, environment value or
loggable response by EcoreX. Workload identity/OIDC may be inherited by the
digest-pinned adapter. The returned signature is independently checked against
the protected public key before ReleaseBuilder accepts it.

The Candidate workflow runs lint, compile, full unit/contract/integration/E2E
suites, WebUI audit/typecheck/tests/build, migration dry-run, schema authority,
reproducibility, license, secret, SBOM, signature, size and protected live
acceptance gates. The separate publication workflow re-authenticates the signed
Candidate and every gate before reusing `ReleaseAssetPublicationCoordinator`,
which finalizes the domestic mirror first, creates/uploads GitHub second,
finalizes CDN third and makes the GitHub draft public only after all three
contain the same signed bytes. The three publication gates share one immutable
publication-receipt digest.

Required protected configuration is deliberately operational, not committed:

- stage environment: target-qualified
  `ECOREX_STAGE_RUNTIME_CONFIG_<TARGET>_PATH` and `_SHA256` values for
  `WINDOWS_X64`, `MACOS_ARM64` and `MACOS_X64`; each protected runner also
  provides the matching native toolchain, while the locked platform-stage
  Python profile and Chromium are installed by the workflow;
- signing environments: release signer executable/digest, optional adapter/
  digest, signer key ID/public key and version-qualified mirror/CDN base URLs;
- live-acceptance environment: digest-pinned Windows acceptance driver and the
  managed Model/Image/CDP test session held outside repository files;
- publication environments: publication config, mirror/CDN/Bootstrap
  credentials, Control Plane URL/host allowlist/token and required reviewers.

The stager, production Windows sandbox helper, platform launchers and Pack
implementations are repository-owned sources. Their compiled bytes and the
real Chromium/Python closure exist only after the named protected runners
produce passing receipts. Missing MSVC/clang, failed AppContainer/Seatbelt
behavior, a non-relocatable Python closure or a browser smoke failure keeps the
workflow red; local fixtures do not satisfy this GA gate. Runner network
installation/build is permitted under the dependency lock. Published archives
never run `git pull`, `npm build` or `pip install` on a user's machine.

Core and all six required Packs are one release slot. A signed manifest that
declares any Pack must contain the exact host archive+sidecar set; all twelve
files download with independent source failover and are verified before the
candidate slot is visible. The user pointer changes only after explicit
“更新并刷新”. Bootstrap revalidates the composite slot before launch, and a
pre-data health failure restores the prior Core+Pack slot atomically. The full
contract and failure matrix are recorded in
`capability-pack-platform-staging.md`.

ReleaseBuilder turns those channel roots into immutable public identities only
after `build_digest` exists: mirror/CDN append `release_id`; stable uses GitHub
tag `v1.0.0`; canary uses `v1.0.0-canary-<24-hex-build-prefix>`. The source
roots and scoping mode are themselves digest material, so rerouting a build
cannot silently reuse a release identity.

## Failure handling

- Publication failure: fix the unavailable origin or credential and rerun the
  same command; do not rebuild or rename the release.
- Gate failure: do not promote. Build a new immutable release identity after
  fixing code or evidence.
- Rollout incident: pause or halt the rollout/Kill Switch from the Control
  Plane. Client health failure before data use rolls back automatically; after
  new data use, ship a roll-forward repair.
- First-install/update collision: InstallCoordinator's product mutex and pinned
  target digest own the operation. Do not delete staging or CAS files manually.
