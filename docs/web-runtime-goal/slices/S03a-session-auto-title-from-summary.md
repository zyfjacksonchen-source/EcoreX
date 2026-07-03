# S3a Session Auto Title From Summary

## Intent

Fix session auto-naming so Web sessions are titled from the overall session summary/topic instead of the latest or first raw message text.

## Implemented Changes

- `agent.chat.session_service.generate_session_title` now accepts recent conversation messages and optional `session_summary`.
- Title prompt prioritizes whole-session context and explicitly avoids using only the latest message.
- `SessionService.gen_title` loads recent display history from `ConversationStore` before generating the title.
- Web `/api/sessions/{id}/generate_title` no longer requires `user_message`; it uses stored session history and still respects manual title locks.
- Legacy Web `console.js` now sends an empty title-generation payload after first-turn completion, instead of passing the current user text as the title source.
- Attachment-only first turns now also trigger auto-title generation.
- Title generation logs record hashes/lengths instead of title text.

## Acceptance

- Auto title uses stored session history containing multiple turns.
- A stale/latest payload string does not appear in the title generation prompt when session history exists.
- Zero-byte empty Web title-generation body succeeds.
- Attachment-only first Web message triggers title generation.
- Title logs do not include generated title text.
- Manual title lock continues to block generated updates.

## Evidence

- `docs/web-runtime-goal/artifacts/S03a-session-auto-title-tests.json`
