# S05 Default Identity Injection

## Status

Completed.

## Intent

Make the out-of-box assistant identity stable and avoid asking users to redefine identity.

## Decisions

- Assistant display identity is `小芯`.
- User address is `同学`.
- Default style is professional and rigorous.
- First-run identity-definition questioning is disabled or bypassed.

## Implementation

- Updated default runtime `character_desc` to identify the assistant as `小芯`, address the user as `同学`, and use a professional, rigorous, concise style.
- Added migration for the previous EcoreX default persona so existing default installs move to the new identity while custom personas are preserved.
- Updated desktop sidecar config creation/migration to use the new default persona.
- Updated `AGENT.md` workspace template to seed `小芯 / 同学 / 专业严谨` directly.
- Updated `USER.md` template default preferred name to `同学`.
- Updated first-run `BOOTSTRAP.md` templates to avoid proactive identity-definition questions and to use the default identity instead.
- Updated user-facing WebUI conversation entry copy from `EcoreX` to `小芯` where it refers to chatting with the assistant.
- Updated legacy assistant self-name sanitization so old `CowAgent` self-references become `小芯`.

## Verification

- `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest tests/test_v029_default_identity.py -q`
- `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest tests/test_ecorex_web_parallel_backend.py::TestWebParallelHandlers::test_v022_hotfix_auth_identity_feishu_and_artifact_contracts -q`
- `npm run typecheck`
- `npm run build:renderer`
