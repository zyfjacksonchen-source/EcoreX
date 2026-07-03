# S13 Tongxin CLI Bundled Realtime Consensus

Generated: 2026-07-02

## Scope

Web-only Tongxin Assistant CLI root fix: bundle the verified read-only CLI package, expose `DATABASE` / `Database` / `database`, prefer bundled/local runtime paths over remote bootstrap, validate real realtime spend access, and deploy the refreshed v0.2.6 Web artifacts.

## Evidence

- `docs/web-runtime-goal/artifacts/S13-tongxin-cli-bundled-realtime.json`: PASS.
- `docs/v0.2.6/artifacts/production-tongxin-postdeploy.json`: 17/17 PASS.
- `docs/web-runtime-goal/artifacts/S12-production-final-gate.json`: 24/24 PASS.
- `docs/v0.2.6/artifacts/production-deploy-online.json`: PASS.

## Release Artifacts

- Windows WebUI: `F8E01999A4D09F05D997CB3C6E9CB8D5E3AE7DC3B56313BE2EAF05F0F15EFD2F`, size `127045258`.
- macOS WebUI: `D5F5CB900C3ED2E97D03DD1616133B282F0D30402525F2A816259D7DF3747560`, size `301423658`.
- Web Linux service: `72D075CE282754C3E611F8D2DA693AB91B5259AB65FFCAF1DAE3482E069527E2`, size `4068058`.
- Public release zip: `5E4B7D991F092B31E56A5A8AEA88B2191FC73061E5C1A1A6523E7E18E676DB23`, size `431511865`.

## Multi-Agent Review

- Architecture consistency: PASS_WITH_NOTES. No blockers. Note: production can remain configured to the state package because the bundled package is present and state/runtime compatibility is validated; one old remote-first wording note was fixed in `agent/tools/optional_abilities/optional_abilities.py`.
- Security/permissions: PASS_WITH_NOTES. No blockers. Notes: artifacts redact credentials/tokens/business IDs; wrapper remains read-only and allowlisted. Bundled CLI contains broader internal code, but EcoreX exposes only the structured read-only wrapper.
- Runtime dependency: PASS. No blockers. Notes: all 8 Tongxin files are present in Windows/macOS/Linux packages; `DATABASE` / `Database` / `database` / `get_db` resolve; `cryptography` is declared; bundled placeholder output is rejected as unhealthy.
- Web UX/observability: PASS_WITH_NOTES. No blockers. Notes: `tongxin_cli status` exposes bundled readiness and redacted path refs; generic capability UI can still be richer later.
- Test/release: PASS_WITH_NOTES. No blockers. Notes: final gate is 24/24 PASS; postdeploy is 17/17 PASS; include all S13 files in the release change set.

## Consensus

PASS_WITH_NOTES accepted. All notes are non-blocking after the wording fix and final rebuild/deploy.

Confirmed points:

- Latest packages are deployed to the configured EcoreX Web server and the public download manifest points to the rebuilt v0.2.6 artifacts.
- The production `/tmp` space issue was resolved by removing old EcoreX release temp directories; deploy was retried and passed.
- `tools/tongxin` is bundled in all release artifacts and no longer behaves like a lightweight placeholder.
- Existing production state package was updated only for no-secret compatibility files; server auth config was not overwritten.
- Server postdeploy found 42 numeric target accounts and sampled 3 realtime paths with `ok=3`; wrapper sample returned `direct_account_id`.
- Full earlier S13 validation covered 42/42 realtime accounts; exact spend and raw business identifiers are not persisted.

## Non-Blocking Follow-Ups

- Improve generic capability UI/action-plan wording so the bundled path readiness is as visible there as in `tongxin_cli status`.
