# S12 WebUI Follow-ups: Tencent Docs, Retouch Editor, Knowledge Graph

## Status

Local WebUI implementation completed. Production deployment is intentionally pending user hand test confirmation.

## Source

- Current thread follow-up on 2026-07-05.
- User boundary: WEBUI only.
- Explicit deployment gate: run local true-user checks first, open for hand test, deploy only after user confirms.

## Scope

- Fix Tencent Docs in-session side effects:
  - Session summary must not collapse to an icon-only row.
  - Clicking a session row with empty local messages should attempt history recovery instead of appearing blank.
  - Tencent Docs empty-text sends seed a readable title.
- Move Tencent Docs entry out of the composer:
  - Composer keeps only the local attachment button.
  - Tencent Docs appears under Settings > External Connections.
  - The primary action writes an agent-guided Tencent Docs connection prompt into the current chat.
  - Static WebUI overlay no longer injects the old composer button or memory star-map tab.
- Rework precise image retouch UI:
  - Reference direction: Cowart/canvas-hand style editor interaction.
  - Centered canvas, top bar, bottom floating toolbar, right floating style panel.
  - Red curved open-arrow annotations and small text labels replace the oversized filled arrow/boxed label style.
- Move knowledge graph out of Settings > Memory:
  - Memory keeps project memory and dream distillation entries.
  - Knowledge Graph becomes its own settings page with a larger graph, selected-node details, excerpt, path/category/degree, and related nodes.

## Changed Files

- `desktop/src/App.tsx`
- `desktop/src/components/ImageRetouchCanvas.tsx`
- `desktop/src/styles/app.css`
- `desktop/public/assets/logos/tencent-docs.png`
- `channel/web/static/app/assets/ecorex-v029-overlay.js`
- `channel/web/static/app/index.html`
- `common/ecorex_release_notes.py`
- `tests/test_v029_webui_followups.py`

## Verification

- `npm run build:renderer` in `desktop/`: PASS.
- `npm run typecheck` in `desktop/`: PASS.
- `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest tests/test_v029_webui_followups.py -q`: PASS, 4 tests.

## Deployment Note

No production deployment was run for this slice. The next action is to open the local WebUI for user hand testing; deploy only after the user confirms.
