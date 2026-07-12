# Cross-runner reproducibility Candidate gate

## Trust boundary

`check-v1-reproducibility.py` proves that four byte contracts are identical,
but equality alone does not prove which repository, commit, workflow run or
attempt produced them.  Candidate therefore consumes two independent facts:

1. the GitHub Actions run API response, validated as one recent completed and
   successful configured-protected-main `push` or `workflow_dispatch` execution of
   `.github/workflows/ecorex-v1-ci.yml`; and
2. the GitHub artifact-list API response plus the exact four downloaded
   artifacts from that same run attempt:
   `ubuntu-x64`, `windows-x64`, `macos-arm64`, and `macos-x64`.

The verifier fixes the accepted ref to `refs/heads/main`; branch-protection
enforcement itself remains an administrative GitHub repository/environment
boundary and the Candidate dispatch must still require `github.ref_protected`.
GitHub may return the workflow path as either the base path or the exact
`@main`-qualified path; those are the only two accepted forms and evidence
always records the canonical base path. It rejects `@feature`, any other
suffix/path, PR/`pull_request_target` events, fork repositories, a
non-empty PR association, a branch other than `main`, a mismatched run ID or
attempt, a stale/future run, links/reparse points/hardlinks, path replacement,
missing targets, duplicate/extra files and non-identical canonical contracts.
It snapshots the four safely-read files and then calls the checked-in
`check-v1-reproducibility.py` comparison instead of implementing a second
equality rule.

The typed source evidence is not a Candidate receipt and has no release
authority.  `bind-v1-reproducibility-evidence.py` only reads an already signed
schema-v2 Candidate receipt and signed release manifest.  It verifies both
with the configured release public key and requires the byte-contract-derived
canonical Web tree digest to equal the receipt's signed `web_tree_sha256`.
It cannot create, modify or re-sign a Candidate.

## Candidate workflow integration

The protected Candidate workflow now requires two decimal dispatch inputs:

- `ci_run_id`: successful `EcoreX v1 CI` run ID for `${{ github.sha }}`.
- `ci_run_attempt`: exact attempt number of that run.

In a read-only job with `contents: read` and `actions: read`:

1. Fetch the run and artifact-list responses without normalization:

   ```bash
   gh api "repos/${GITHUB_REPOSITORY}/actions/runs/${CI_RUN_ID}/attempts/${CI_RUN_ATTEMPT}" \
     > .candidate/ci/run.json
   gh api "repos/${GITHUB_REPOSITORY}/actions/runs/${CI_RUN_ID}/artifacts?per_page=100" \
     > .candidate/ci/artifacts.json
   ```

2. Before download, validate that the artifact list contains exactly four
   unique, non-expired expected names. Each artifact must bind the selected
   run ID, repository IDs, main head SHA, a SHA-256 archive digest and
   `created_at`/`updated_at` timestamps no earlier than this attempt's
   `run_started_at`. The selector emits the four immutable artifact IDs;
   `actions/download-artifact` consumes `artifact-ids`, never a mutable name
   pattern. It preserves the default per-artifact directories under
   `.candidate/ci/contracts`.  The resulting tree must be exactly:

   ```text
   ecorex-v1-byte-ubuntu-x64/byte-contract.json
   ecorex-v1-byte-windows-x64/byte-contract.json
   ecorex-v1-byte-macos-arm64/byte-contract.json
   ecorex-v1-byte-macos-x64/byte-contract.json
   ```

   Candidate intentionally accepts only an exact four-artifact set. A complete
   GitHub rerun replaces that set; a partial job rerun can retain artifacts
   from jobs that did not rerun, so it is rejected by the current-attempt
   timestamp rule. Operators must use **Re-run all jobs** or start a new CI run
   before dispatching Candidate. This fail-closed rule prevents mixing runner
   bytes from different attempts.

3. Generate the typed evidence:

   ```bash
   python scripts/verify-v1-ci-provenance.py \
     --run-metadata .candidate/ci/run.json \
     --artifact-metadata .candidate/ci/artifacts.json \
     --contracts-root .candidate/ci/contracts \
     --repository "${GITHUB_REPOSITORY}" \
     --commit-sha "${GITHUB_SHA}" \
     --workflow-run-id "${CI_RUN_ID}" \
     --run-attempt "${CI_RUN_ATTEMPT}" \
     --protected-ref refs/heads/main \
     --max-run-age-seconds 86400 \
     --output .candidate/ci/reproducibility.json
   ```

The output is canonical
`ecorex-cross-runner-reproducibility` schema 2. It binds repository IDs,
commit, canonical workflow path, run ID/attempt/event/times, the raw run and
artifact-metadata SHA-256 values, every selected artifact ID/archive digest/
size/time, a target-keyed map of all four downloaded contract SHA-256 values,
and the canonical Web bundle tree SHA-256. The archive digest authenticates
GitHub's immutable artifact container; the downloaded byte-contract digest is
recorded separately because they are intentionally different byte domains.

After the immutable Candidate and its signed receipt exist, create the
separate release-bound evidence:

```bash
python scripts/bind-v1-reproducibility-evidence.py \
  --evidence .candidate/ci/reproducibility.json \
  --candidate-receipt .candidate/output/candidate-build-receipt.json \
  --release-manifest .candidate/output/release/release-manifest.json \
  --trusted-public-key "${RELEASE_PUBLIC_KEY_B64}" \
  --output .candidate/output/reproducibility-release-bound.json
```

The bound output records the source-evidence digest, four contract digests,
CI identity, signed Candidate receipt digest, signed manifest digest,
`release_id`, `build_digest`, signing key ID and equal canonical/signed Web
tree digests. The typed gate writer accepts only this specialized bound schema:

```bash
python scripts/write-v1-gate-receipts.py \
  --gate reproducibility \
  --evidence-file .candidate/output/reproducibility-release-bound.json \
  --manifest .candidate/output/release/release-manifest.json \
  --commit-sha "${GITHUB_SHA}" \
  --workflow-run-id "${GITHUB_RUN_ID}" \
  --output-dir .candidate/gates
```

It rejects the unbound source evidence, a different release identity, unequal
canonical/signed Web trees, a non-main CI ref, an incomplete target set or
non-identical contract digests. `reproducibility` is part of
`REQUIRED_RELEASE_GATES` for both canary and stable. The source and bound
evidence are retained with the immutable Candidate artifact, and the release
assembler requires its same-Candidate-run gate receipt before promotion.
Source evidence alone is never a release gate.
