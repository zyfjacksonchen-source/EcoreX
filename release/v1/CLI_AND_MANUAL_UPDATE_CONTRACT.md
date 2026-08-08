# e-Mate v1 stable install and update command contract

## Authority and invariant

This file is the sole normative source of truth for producing, publishing and
activating an e-Mate v1 release. Other release notes, development logs, UI copy
and operator examples are evidence or explanation only. If they disagree with
this contract, this contract wins and the operation must fail closed.

The only supported production/update fast lane is:

```text
new Core delta -> reuse unchanged Packs by SHA-256 -> upload and read back all
immutable resources -> accept those exact bytes -> atomically switch the stable
update pointer last
```

- A product or WebUI change builds one new Core and its signed, exact-base
  CoreDelta. A full Core is the bounded fallback, not the first update path.
- An unchanged Pack is never rebuilt, downloaded or installed again merely
  because the product version changed. Its content-addressed published bytes
  and the user's verified cache are reused. A release-scoped immutable alias
  may be added only when an existing manifest contract requires that name.
- Every Core, delta, Pack, sidecar, manifest and platform archive is immutable,
  digest-checked after upload and publicly read back before acceptance.
- The candidate used for browser acceptance is byte-identical to the candidate
  being published. No rebuild is allowed between acceptance and activation.
- The stable update pointer is the sole mutable publication fact and is changed
  once, atomically, as the final operation. Until that succeeds, users continue
  to see and run the prior known-good release.

This contract freezes the v1.0.0 command surface. It deliberately does not copy
CowAgent's source checkout, `curl | bash`, `git pull`, runtime `pip install`, or
global proxy mutation. Those shortcuts make the user's Git, Python, package
index and shell part of the product and are the main source of non-reproducible
download and install failures.

## User surface

There is no user prerequisite CLI. The supported flow is:

1. Download one platform ZIP and verify the published SHA-256.
2. Extract the complete ZIP. Do not run an installer from inside the archive.
3. Run exactly one entry: `Install EcoreX WebUI.command` on macOS or
   `Install EcoreX WebUI.cmd` on Windows.
4. Use the installed e-Mate shortcut. Bootstrap owns process start, health,
   restart, slot rollback and dependency-pack activation.
5. For later releases, the WebUI shows one banner. The first check only reports
   availability. A user click starts the signed delta/full fallback download,
   shows install progress, verifies every byte, activates atomically and opens
   the new version. A blocked new window falls back to the current window.

Users do not run `start`, `stop`, `restart`, `update`, `pip`, `npm`, Playwright
or browser-install commands. Browser, OCR, Office and sandbox dependencies are
signed versioned Packs; unchanged verified content is reused from the local
cache.

## Product-owned internal commands

The native Bootstrap command vocabulary is fixed for v1:

| Purpose | Stable form |
|---|---|
| Verify packaged Bootstrap | `ecorex-bootstrap --self-test` |
| Install an authenticated local release | `ecorex-bootstrap --local-release RELEASE_DIR` |
| Open an isolated candidate | `ecorex-bootstrap --preview-local-release RELEASE_DIR --preview-port 18765` |
| Start the active installed slot | `ecorex-bootstrap --launch-installed` |
| Run an already verified Core | `ecorex serve --host 127.0.0.1 --port 8765` |

These are Bootstrap/packaging interfaces, not user setup instructions. The
Python console-script form and `python -m ecorex.<module>` fallback must enter
the same implementation. The legacy root `cli/` tree is excluded from the v1
distribution and is not a version, lifecycle or update authority.

## Manual WebUI release order

One immutable source commit and one output directory are used for the whole
run. Paths are absolute; credentials are supplied only through the current
process environment. The build command is:

```text
python3 scripts/build-v1-manual-webui.py \
  --source SOURCE \
  --commit-sha COMMIT_SHA \
  --web-dist SOURCE/desktop/dist \
  --base-windows BASE/EcoreX_0.3.2-webui-windows-x64.zip \
  --base-macos BASE/EcoreX_0.3.2-webui-macos-universal.zip \
  --go TOOLCHAIN/bin/go \
  --output OUTPUT
```

The builder rejects a dirty/wrong commit, wrong version, stale Web dist,
unexpected archive member, wrong base digest, missing target, failed Go test,
failed Bootstrap self-test, unsigned artifact or existing output. It writes the
output by one directory rename and reports a stable machine error code. Never
delete an accepted output to retry a network publication.

The release state order is fixed:

```text
built-and-verified -> draft-assets-staged -> public-readback-verified
-> local-browser-accepted -> update-pointer-active
```

- GitHub/CDN staging is digest-resumable: matching remote bytes are reused;
  conflicts fail closed; only missing bytes upload.
- The local proxy is passed to the publication process only. No Git remote,
  global Git proxy, system proxy or Keychain item is changed.
- A GitHub token is read from an environment variable for the current process,
  never from a command argument, repository file, log, receipt or Keychain.
- Candidate preview and live cutover reuse the same signed manifest and build
  digest. A different Candidate cannot consume the prepared transaction.
- The stable public update pointer is the last mutation. A failed download,
  upload, browser acceptance or readback leaves the installed version and the
  public pointer unchanged.

## Failure and retry contract

- Discovery probes ordered mirrors; artifact transfer uses bounded retry,
  exponential backoff and a per-source circuit breaker.
- A retry uses the same release/build identity and request identity. It resumes
  verified cache or Draft assets instead of rebuilding or re-uploading them.
- SHA-256, size, signature, target and exact-base mismatch are terminal trust
  failures, never retry candidates.
- Delta failure may fall back once to the signed full Core. Pack or full-Core
  verification failure preserves the active slot.
- The UI always reports a controlled reason and retry action. It never loops or
  silently reports success.
