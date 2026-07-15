# S4b Review Consensus - Web Multi-Model Selection

## Scope

Read-only multi-agent review for S4b Web chat model selection. Scope covers Web API model projection, renderer chat model switcher, admin-managed `gpt-5.5` routing, context policy/token estimation, provider connectivity evidence, secret-clean release packaging, and the invariant that image generation remains pinned to `gpt-image-2-pro`.

## Final Decision

`PASS_WITH_NOTES`

All blocking findings were fixed and re-reviewed. Remaining notes are non-blocking follow-up items for later release/runtime gate slices.

## Review Matrix

| Role | Result | Notes |
| --- | --- | --- |
| Architecture consistency | `PASS` | Initial `FAIL` found frontend alias masking: `deepseek-*` could display as `gpt-5.5`. Fixed by removing `EFFECTIVE_MODEL_ALIAS_PREFIXES`, adding a regression assertion, rebuilding, and re-reviewing to `PASS`. |
| Security / permissions | `PASS_WITH_NOTES` | No blocking issue. Verified backend rejects unconfigured chat switches, Web disables `configured=false` rows, admin OpenAI policy is read from cache, and release package secrets are not embedded. |
| Runtime / dependencies | `PASS_WITH_NOTES` | No blocking issue. Verified four menu models pass real connectivity. Note: `tiktoken` is importable in current package but remains optional in dependency declarations, so S2/S9 should decide whether local tokenizer support becomes a hard gate. |
| Web UX / observability | `PASS` | Verified provider icons, disabled unavailable rows, switch divider, dynamic context meter, `/api/models` provider alignment, and fixed image model projection. |
| Tests / release | `PASS_WITH_NOTES` | No blocking issue. Verified previous packaged-secret blocker is resolved, release-local config has empty secret fields, runtime files/assets are synced, and smoke/build evidence is present. |

## Evidence

- `docs/web-runtime-goal/artifacts/S04b-web-multi-model-tests.json`
- `docs/web-runtime-goal/artifacts/S04b-chat-model-connectivity.json`
- `docs/web-runtime-goal/artifacts/S04b-release-runtime-models.json`
- `docs/web-runtime-goal/slices/S04b-web-multi-model-selection.md`

## Final Invariants

- Chat selector keeps one main model per provider.
- `gpt-5.5` is preserved and uses the admin-managed model policy key/base.
- Follow-up hardening on 2026-07-01 tightened the real-connectivity smoke: `openai:gpt-5.5` now reports `credentialSource=admin_policy_cache` and no longer falls back to the local multi-model key note file for OpenAI.
- The main menu model set verified by real connectivity is:
  - `openai:gpt-5.5`
  - `deepseek:deepseek-v4-pro`
  - `gemini:gemini-3.1-pro-preview`
  - `doubao:doubao-seed-2-0-pro-260215`
- `doubao-seed-2.1-pro` remains diagnostic-only until the configured Ark key has entitlement.
- Image generation remains `openai:gpt-image-2-pro` and is not changed by chat model switching.
- Unconfigured model switches fail before config writes.
- Provider model names are displayed as-is; no `deepseek-*` aliasing to `gpt-5.5`.
- Release-local package config remains secret-clean; multi-vendor connectivity uses external/admin-managed credentials.

## Follow-Up Notes

- Decide in S2/S9 whether `tiktoken` should move from optional to core-required for packaged local-tokenizer status.
- Continue keeping real provider entitlement checks in deterministic smoke artifacts rather than prompt-driven diagnosis.
