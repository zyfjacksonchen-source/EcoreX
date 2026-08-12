# Design QA — authenticated artifact preview and image gallery

## Scope and references

- Reference A: `/Users/mac/.codex/attachments/69d5fd1b-05d0-405b-9dc7-5f47098a5711/codex-clipboard-f9b649e4-3271-49ab-b6a8-725281911d8b.png`
- Reference B: `/Users/mac/.codex/attachments/10c7e305-eb18-452b-96a5-d62d6ccf5e75/codex-clipboard-d7415a32-ac86-4b58-86a2-75d1af9ab5ee.png`
- Implementation capture: `/private/tmp/ecorex-artifact-preview-qa/implementation-1468-dark.png`
- Side-by-side visual QA sheet (references normalized to the implementation content width): `/private/tmp/ecorex-artifact-preview-qa/codex-emate-gallery-contact-sheet.png`
- Product truth: CowAgent 2.1.5 file cards open in an in-app preview surface; local files open through the desktop shell path. e-Mate keeps the existing authenticated Blob preview and backend-materialized local open/reveal paths.

## Visual comparison

| Criterion | Reference | Implementation | Result |
| --- | --- | --- | --- |
| Density | One compact row of near-square cards | One compact row; three cards measured 254.66 × 254.66 px inside the current conversation width | Pass |
| Card geometry | Clearly rounded, near-square cards with a small even gap | 1:1 cards, 18 px corner radius, tokenized 8 px gap | Pass |
| Image fit | Entire image visible; no cover crop | `object-fit: contain`; both ready thumbnails reported complete and uncropped | Pass |
| Controls | Navigation grouped at the lower-right of the current card | Previous/count/next overlay on the active card; disabled boundaries retained | Pass |
| Preview | Expanded viewer stays inside the desktop app | Click opened the authenticated `ArtifactPreviewDialog`; no private URL left the app | Pass |
| Accessibility | Keyboard navigation and state count | ArrowLeft/ArrowRight changed the live region from 1/3 → 2/3 → 1/3; buttons remain labelled | Pass |

## Evidence

- Runtime navigation regression rejects same-origin, `localhost`, `127.0.0.1`, and `::1` web URLs before `shell.openExternal`.
- Image cards invoke only the existing `preview` action; the preview request uses the Runtime client's authenticated headers and renders a Blob URL.
- Private `/api/v1/artifacts/...` Markdown links are rendered as inert text rather than bearer-less external links.
- Download remains a download action. Non-previewable file open/reveal remains the backend-authoritative local file path, matching Cow's desktop shell semantics.
- Browser QA reported zero horizontal overflow at the tested desktop viewport.

## Severity review

- P0: none.
- P1: none.
- P2: none.

Final result: **passed**.

---

## Prior QA record — compact task header

**Comparison target**

- Source visual truth: `/Users/mac/.codex/attachments/c18509fa-7b3d-4d37-8431-196b360e2cee/codex-clipboard-74843f6a-2e39-4429-adbc-3695ef56e176.png`
- Rendered implementation: `/var/folders/75/tqjysfjn4nz9hdy5gvv1m7d40000gn/T/com.openai.sky.CUAService/e-Mate Screenshot 2026-08-12 at 2.28.35 PM.jpeg`
- Focused implementation crop: `/private/tmp/emate-compact-header-implementation.jpeg`
- Viewport: desktop app window, 1250 x 768 pixels; focused header crop 1008 x 94 pixels.
- Density normalization: source is 1334 x 94 pixels; the focused implementation was cropped to the same 94-pixel height. The macOS title bar is platform chrome and was excluded from fidelity judgments.
- State: dark theme, authenticated task view, long generated task title, Runtime connected.

**Full-view comparison evidence**

- The header remains one row above the conversation and does not move or cover the persistent theme/share actions.
- The full task title is still available through the native title tooltip and accessibility tree.

**Focused region comparison evidence**

- The source lets the title consume almost the entire header width. The implementation caps it at `min(32rem, 50vw)` and uses a single trailing ellipsis, matching Codex's compact title treatment while leaving stable room for actions.
- Folder icon, connection status, dark surface, typography hierarchy, and two-line title/status rhythm remain unchanged.

**Required fidelity surfaces**

- Fonts and typography: existing system UI font, 600 title weight, single-line truncation, and status caption are preserved.
- Spacing and layout rhythm: title width is reduced without changing header height, padding, icon alignment, or action spacing.
- Colors and visual tokens: existing dark workspace, muted icon, success status, and rule tokens are unchanged.
- Image quality and asset fidelity: no raster assets were changed or approximated.
- Copy and content: visible title is shortened only by CSS ellipsis; full title and connection copy remain available.

**Findings**

- No actionable P0, P1, or P2 mismatch remains.

**Comparison history**

- Initial finding: the unbounded task title occupied nearly the full header and visually dominated the status/actions.
- Fix: added a responsive maximum width, preserved one-line ellipsis, and exposed the complete title through the native `title` attribute.
- Post-fix evidence: focused implementation crop shows a compact title ending after the useful task-identifying portion, with status and header actions unobstructed.

**Implementation checklist**

- [x] Compact desktop title width.
- [x] Compact narrow-window title width.
- [x] Preserve one-line ellipsis.
- [x] Preserve full-title discoverability.
- [x] Verify the real unpacked Electron app.

**Follow-up polish**

- None required for this scoped change.

final result: passed
