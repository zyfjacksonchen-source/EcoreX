# Installed authenticated Runtime CDP acceptance

`desktop/tools/run-installed-authenticated-runtime-cdp.mjs` verifies an already-running,
installed EcoreX Runtime through real Google Chrome CDP. It does not start a mock Runtime.
With no `--groups` argument it fails closed unless every group passes:

- uploaded-image local thumbnail, authenticated thumbnail and full preview;
- real OCR, vision, read, shell, fetch and CDP Tool events, plus a no-explicit-tool
  progressive-discovery turn that must complete `tool_search` before OCR;
- ranked image generation, four concurrent unique outputs, an overlapping normal task,
  and structured rectangle retouch producing a new revision;
- every visible chat model and a real model-selector switch, including the GPT-5.6 SOL
  policy and 272,000-token threshold;
- persisted theme selection, unique published Share snapshots, and a public Share page
  with distinct user/assistant messages and a decodable generated-image preview.

Success requires ordered Runtime facts. Tool scenarios need matching
`tool.call_requested` and completed `tool.result` events with the same call identity.
An assistant sentence claiming that a command ran is not evidence.

## Secret boundary

The runner accepts account credentials only through `ECOREX_ACCEPTANCE_IDENTIFIER` and
`ECOREX_ACCEPTANCE_PASSWORD`. It deletes both environment entries after reading them,
never accepts them on argv, and never writes them to stdout or its evidence object. A
protected runner should populate these values from Windows Credential Manager immediately
before launch. Do not place credentials in a checked-in script or shell history.

The Runtime bearer comes from the installed page's frozen bridge. The current CSRF value
comes from `/api/v1/bootstrap`. Password login rotates the Runtime process, so the runner
waits for restart, reloads the page, and obtains a new bridge and CSRF value before any
authenticated scenario.

## Inputs and invocation

The fixture variables are mandatory for the default full run. Their values are consumed
but neither paths nor URLs are reported:

- `ECOREX_ACCEPTANCE_IMAGE_FIXTURE`: a small image containing the expected OCR token and
  a visually unambiguous subject.
- `ECOREX_ACCEPTANCE_OCR_EXPECTED` and `ECOREX_ACCEPTANCE_VISION_EXPECTED`.
- `ECOREX_ACCEPTANCE_READ_FIXTURE` and `ECOREX_ACCEPTANCE_READ_EXPECTED`.
- `ECOREX_ACCEPTANCE_FETCH_URL` and `ECOREX_ACCEPTANCE_FETCH_EXPECTED`.
- `ECOREX_ACCEPTANCE_CDP_URL` and `ECOREX_ACCEPTANCE_CDP_EXPECTED`.

Run from `desktop/` after the installed Runtime is listening:

```powershell
node tools/run-installed-authenticated-runtime-cdp.mjs `
  --base-url=http://127.0.0.1:8765 `
  --expected-release-id=<release-id> `
  --expected-version=1.0.9
```

For a bounded diagnostic rerun, select comma-separated groups such as
`--groups=attachments,tools`. Omitting the option always restores the full matrix. The
`ui` group depends on `image`, because it validates that a shared conversation renders
the generated image.

The single JSON line on stdout contains release identity, hashed model/Thread/Turn/
Artifact/Share identities, statuses, durations, event hashes, binary hashes and a
screenshot hash. It contains no credential, Runtime secret, prompt, model response,
public URL or local path.
