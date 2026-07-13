# EcoreX v1 protected live-acceptance runner contract

This runner is the last read-only product gate before any release asset is
published. It is not a generic CI worker and must use the protected GitHub
environment `ecorex-live-acceptance` on Windows x64.

The environment defines only:

- `ECOREX_LIVE_ACCEPTANCE_EXECUTABLE`: absolute path to the installed driver.
- `ECOREX_LIVE_ACCEPTANCE_EXECUTABLE_SHA256`: lowercase SHA-256 of that exact
  executable.
- `ECOREX_RELEASE_SIGNER_PUBLIC_KEY`: public verification key already used by
  the Candidate workflow.

Provider/session credentials must not be GitHub variables or process
environment entries. The acceptance account is provisioned through the real
EcoreX device flow and stored in Windows Credential Manager for the dedicated
runner identity. The driver updates a persistent acceptance installation with
the input Candidate, activates only after verification, launches it through the
signed Bootstrap, and performs real API plus Chrome CDP interactions.

The driver is invoked with no argv. It reads one canonical JSON line from stdin
containing the commit, workflow run, Candidate root and expected release
identity. Before the driver can start, the wrapper verifies the release manifest
signature, Candidate receipt signature and exact protected platform provenance.
It must terminate every Runtime/Chrome child before exit and emit one JSON
object on stdout. Any stderr is diagnostic-only and is discarded by the receipt
boundary.

The output contract is implemented by
`ecorex.release.live_acceptance.validate_live_acceptance_evidence`. It forbids
free-form prompts, responses, URLs, filesystem paths, credentials and binary
content. Screenshots, provider receipts, model output, traces and artifacts are
represented only by SHA-256. The required executions are:

- `live-model`: real GPT-5.6 SOL, medium reasoning, 272,000-token compaction,
  successful terminal event.
- `live-image`: ranked/non-exclusive Image 2 routing, all core tools still
  discoverable, at least four concurrent unique completions, and structured
  rectangle retouch with unchanged-region evidence.
- `cdp-acceptance`: the fixed 18-scenario real-user matrix, Chrome CDP, four
  responsive viewports and zero console/page/request errors.

Failure, timeout, partial output, wrong executable digest, Candidate drift,
missing scenario or any provider/browser error leaves all three receipts
absent. The `publish` job accepts only the post-gate
`ecorex-v1-accepted-<channel>` artifact.
