# Design QA — authenticated artifact preview and image gallery

## Native window and system context menu addendum

- Source visual truth:
  - `/Users/mac/.codex/attachments/05420805-986f-4fb9-98a1-cc63f3bcddab/codex-clipboard-ddecb157-393a-4dea-bd36-cb65853d4489.png`
  - `/Users/mac/.codex/attachments/606fd341-e97b-451b-9734-374f53a060bd/codex-clipboard-90b1eba6-9a14-4f95-a7be-d9aa52aa0f8d.png`
- Implementation screenshot: `/private/tmp/emate-native-window-smoke.Hj6E7N/product-native-window.png`
- Combined comparison: `/private/tmp/emate-native-window-smoke.Hj6E7N/native-window-contact-sheet.png`
- User-confirmed context-menu evidence: `/Users/mac/.codex/attachments/0fa118f8-ab35-49a3-9297-85b01ee9a907/codex-clipboard-4ec3ac2a-51b1-417c-993b-cfffc85e10f2.png`
- Viewport: 1280 × 840 CSS px, device scale factor 1; source title-bar crops were normalized to 960 px wide in the combined comparison.
- State: macOS, dark theme, authenticated artifact conversation; native traffic lights and artifact context menu visible in separate evidence captures.

### Full-view comparison evidence

- The Electron window uses native macOS traffic lights over the application canvas; the former light system title strip is absent.
- The existing 48 px workspace header remains the sole product title row. Sidebar and workspace surfaces continue the existing e-Mate dark tokens to the top edge.
- The left brand slot reuses `emate-logo.png` at a compact 112 × 26 px inside the 64 px sidebar header. The application icon remains the separately approved black/orange robot, but no robot avatar is used as window chrome.
- Windows keeps Electron's native title-bar overlay and caption-button semantics; no renderer-drawn system buttons were added.

### Focused-region and interaction evidence

- Source and implementation top-edge crops were compared together in `native-window-contact-sheet.png`; the implementation preserves the native traffic-light cluster and removes the source's detached white strip.
- The user-confirmed artifact screenshot shows the native menu items “复制”, “复制文件名”, and “保存并复制文件路径”.
- Text selection keeps Electron's native copy role; images use `webContents.copyImageAt`; web links expose “复制链接地址”. Artifact paths are obtained only after authenticated Runtime materialization and main-process verification under Documents/Downloads `EcoreX`, rejecting symlinks.

### Required fidelity surfaces

- Fonts and typography: existing system UI font, title sizes, weights, ellipsis, and sidebar hierarchy are unchanged.
- Spacing and layout rhythm: native traffic lights receive a dedicated 76 px left inset; e-Mate wordmark is 112 × 26 px; workspace 8 px inset, 16 px radius, and 48 px header remain unchanged.
- Colors and tokens: startup and BrowserWindow backgrounds are `#171719`; renderer chrome continues the existing dark canvas/workspace/rule tokens, eliminating the white flash/title strip.
- Image quality and asset fidelity: the exact repository `emate-logo.png` wordmark is reused for the title area; no SVG/CSS approximation and no `emate-mark.png` robot avatar is used there.
- Copy and content: existing task titles and controls remain unchanged. Context-menu copy labels are native and concise.

### Findings and comparison history

- Initial P1: default Electron title bar produced a detached white strip above the dark application. Fixed with platform-native integrated chrome and a dark startup/window background.
- Initial P1: the initial compact brand slot reused the robot mark. Fixed by retaining the approved robot only as the app icon and reusing the e-Mate wordmark in the title area.
- Initial P1: renderer and main `context-menu` events could race. Fixed by deferring popup until the renderer target IPC arrives, with a same-tick fallback for ordinary text/image/link menus. User real-device evidence confirms the artifact entries appear.
- P0/P1/P2 remaining: none.

final result: passed

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
