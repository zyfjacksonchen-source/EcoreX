# e-Mate v0.3.1 下载页 Design QA

## Visual truth and implementation evidence

- Source 1 — remove the visible wordmark/version lockup: `C:\Users\user\AppData\Local\Temp\codex-clipboard-39680160-c641-43cc-8693-c5a83a2d46c4.png` (`108 × 51`).
- Source 2 — replace the large product name and terminal-oriented hero copy: `C:\Users\user\AppData\Local\Temp\codex-clipboard-e26899c8-11bb-49ee-88aa-d4d1daa57a6a.png` (`960 × 246`).
- Source 3 — replace the update-policy panel with user-facing capability language: `C:\Users\user\AppData\Local\Temp\codex-clipboard-8c165de6-f8b7-485b-9e1c-83eaa2cadf8c.png` (`1827 × 375`).
- Source 4 — keep all five robots visible while changing the primary robot and its capability: `C:\Users\user\AppData\Local\Temp\codex-clipboard-890e714d-a1f6-45ce-886b-2274f043947f.png` (`1536 × 423`).
- Desktop implementation: `C:\EcoreX-Agent生产版\docs\v0.3.1\artifacts\visual\download-page-five-robots-1440x900.png` (`1440 × 900` viewport override, CSS client width `1425`, DPR `1`).
- Mobile implementation: `C:\EcoreX-Agent生产版\docs\v0.3.1\artifacts\visual\download-page-five-robots-390x844.png` (`390 × 844` viewport override, CSS client width `375`, DPR `1`).
- Capability implementation: `C:\EcoreX-Agent生产版\docs\v0.3.1\artifacts\visual\download-page-capabilities-1440x900.png` (`1425 × 868`).

The three sources are focused crops rather than full-page viewports, so density normalization was not applied. Each source crop and its matching implementation region were scaled proportionally and padded, without stretching, into a single comparison image:

- `C:\EcoreX-Agent生产版\docs\v0.3.1\artifacts\visual\compare-download-topbar-source-implementation.png`
- `C:\EcoreX-Agent生产版\docs\v0.3.1\artifacts\visual\compare-download-hero-source-implementation.png`
- `C:\EcoreX-Agent生产版\docs\v0.3.1\artifacts\visual\compare-download-capabilities-source-implementation.png`

State: dark theme, unpublished download discovery, creative robot primary. Source 4 and the final desktop implementation were inspected together in one visual comparison input; both retain the same five-robot order, center emphasis, black ground and orange/teal/yellow/purple palette. Focused comparisons were required because the source screenshots do not contain a common full-page state.

## Findings

- No remaining P0/P1/P2 findings.
- Fonts and typography: the existing system Chinese sans stack is retained. The new capability headings have clear display weight, body copy keeps readable line height, and desktop/mobile wrapping is complete without truncation.
- Spacing and layout rhythm: the `1180px` page shell and hero remain inside the final desktop client width (`scrollWidth 1425 <= clientWidth 1425`). All five robots stay in the hero while the active partner moves to the larger center position. Carousel controls remain grouped around the pagination indicators.
- Colors and visual tokens: the existing black/orange e-Mate hero, light/dark theme tokens, borders and focus color are reused. Text and controls remain legible in the dark capture.
- Image quality and asset fidelity: all five visible partners reuse the exact checked-in `emate-team-hero` raster. Measured percentage crops keep each partner stable at desktop and mobile sizes; no generated asset, placeholder, SVG, emoji or CSS-drawn robot was added. The final lineup intentionally preserves the overlapping circles and relative visual hierarchy of Source 4.
- Copy and content: visible `e-Mate` wordmark/version text is removed from the top brand area, leaving the existing mark and accessible product label. The small orange partner label above the hero heading is also removed in all five states. The terminal-oriented hero paragraph and update-policy language are absent. Each robot now maps directly to a plain-language work outcome; the capability section describes planning, making, collaboration and follow-through in user language.
- Icons: no new icon dependency or substitute artwork was introduced. The brand uses the existing mark; carousel controls use text labels and semantic buttons.
- Responsiveness: the final mobile capture has `scrollWidth 375 <= clientWidth 375`; all five robots remain visible, the active robot remains distinct, and the heading, body, actions, pagination and controls fit without clipping.
- Accessibility and behavior: the lineup is a named group of five semantic buttons with alt text, `aria-pressed`, visible focus, reduced-motion handling and `aria-current` pagination. Clicking a robot, controls/dots, keyboard arrows/Home/End and a real horizontal drag were exercised in the in-app browser. The selection wraps at both ends and updates exactly one capability panel. Console warnings/errors: none.

## Comparison history

1. P1 — the first pass still used the wordmark plus a visible version. Replaced it with the existing mark-only asset and removed the version node; the topbar comparison proves the visible text is gone.
2. P1 — the first full-page evidence looked horizontally clipped and repeated the sticky hero. Re-captured the implementation as a normal `1440 × 900` viewport and checked live geometry: shell and hero `x=122..1302`, copy `x=709..1209`, `scrollWidth 1425`, `clientWidth 1425`. No layout overflow remains.
3. P2 — the first robot crop exposed a blue edge from the neighboring source robot. Re-measured the five circle bounds and tightened every crop; the final first and creative evidence contains one robot per slide.
4. P2 — pagination briefly emitted an empty `aria-current` attribute, so the active pill lost its style after navigation. It now writes `aria-current="true"`; browser state and the active pill update together.
5. P2 — previous/next controls were spread across the hero and the first “上一位” looked active. The final controls are grouped with the dots and disabled endpoints are visibly dimmed.
6. P1 — the first carousel interpretation displayed one robot per slide. The clarified implementation keeps all five robots visible, rotates the selected partner into the primary center position and changes only the matching capability copy.
7. P2 — the orange helper label above each capability heading was visually redundant. Removed the label node and its styling across all five states; desktop/mobile captures confirm `robot-kicker` count `0`.

## Primary interactions and checks

- Clicking a visible robot or named pagination button moves it to the primary center position and updates the matching capability copy.
- “上一位”/“下一位” and keyboard arrows wrap through all five partners; `Home`/`End` select the first/last partner.
- A real horizontal drag changed the active partner from index 4 to index 0 and updated the copy from “把琐碎工作交给靠谱搭档” to “把散乱资料变成清楚的成果”.
- Theme and anchor navigation remain available; the capability navigation reaches the rewritten section.
- `tests/v1/test_public_download_site.py`: `6 passed`.
- `scripts/check-v1-public-download-site.py`: `status=passed`, `hashed_asset_count=7`, no errors.
- Public JavaScript syntax check: passed.

## Follow-up polish

- None required for this slice.

final result: passed
