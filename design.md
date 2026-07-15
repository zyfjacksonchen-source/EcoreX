# Design — EcoreX

A locked design system for the EcoreX v1 app. Every product surface reads this
file before emitting UI. Amend this file when the system needs to grow; do not
invent per-page colours, radii, shadows, type, or motion.

## Genre

Modern-minimal · quiet utilitarian office workbench.

## Macrostructure family

- App pages: Workbench — navigation rail, one clipped workspace surface,
  projection-driven timeline, contextual inspector or sheet.
- Admin/release pages: Workbench — dense status table and progressive detail;
  no dashboard card grid.
- Content/onboarding pages: Long Document — narrow readable column with inline
  product evidence; no marketing enrichment.

## Theme

Codex tonal DNA supplies the canvas, ink, blue interaction accent, contrast,
and semantic colours. EcoreX orange remains the restrained brand/action note;
the two colours never compete inside one control. Chromatic colour occupies at
most 5% of a viewport. Product surfaces use the measured reference swatches
below through semantic tokens; components never carry their own raw colours.

- Light Codex base: surface `#ffffff` → `oklch(1 0 0)`, ink `#1a1c1f` →
  `oklch(0.225591 0.006566 258.364)`, accent `#339cff` →
  `oklch(0.682034 0.173444 251.11)`, contrast setting `45`.
- Dark Codex base: surface `#111111` → `oklch(0.177638 0 0)`, ink `#fcfcfc` →
  `oklch(0.991069 0 0)`, accent `#0169cc` →
  `oklch(0.528649 0.173447 254.975)`, contrast setting `60`.
- Light surface map: non-chat canvas `#f7f7f7`, chat/workspace and Composer
  `#ffffff`, current conversation `#ebebeb`, scrollbar thumb `#e5e5e5`.
- Dark surface map: non-chat canvas `#0f0f0f`, chat/workspace `#111111`, and
  Composer/current conversation/scrollbar thumb share `#202020` exactly.
- Blue owns focus, links, selection, toggles, and capability state. EcoreX
  orange owns primary product actions such as Send and branded emphasis.
  Conversation selection is the neutral session-emphasis surface; blue remains
  reserved for interactive focus and capability state.
- Semantic diff and Skill colours are exported from the supplied Codex theme;
  components consume named tokens only.

## Typography

- Display: system CJK display stack, weight 600, normal.
- Body: system CJK UI stack, weight 400.
- Mono: system monospace stack, weight 400.
- Body `14px / 22px`; UI `13px / 20px`; title `15px / 20px / 600`;
  caption `12px / 16px`. Tabular data uses `tabular-nums`.
- Product UI does not use oversized marketing display text.

## Spacing and shape

- Four-point named scale only: `4, 8, 12, 16, 20, 24, 32, 40, 48, 64px`.
- Shape lock: compact `8px`, control `10px`, card `12px`, panel `16px`,
  dialog `18px`; pill only for tags, status, toggles, and circular controls.
- One `WorkspaceSurface` owns all four outer corners and clips children.
  Header, timeline, and composer never draw competing outer radii.

## Surface and elevation

- Named surfaces only: canvas, surface, raised, overlay, hover, selected.
- Ordinary navigation, messages, artifact rows, and cards have no shadow.
- Only popover, dialog, and toast may use their named elevation token.
- One visual container layer per region; card-in-card is forbidden.

## Motion

- Motion-cut. Functional feedback uses opacity or transform only.
- Durations: fast `120ms`, base `180ms`, slow `240ms`; ease-out
  `cubic-bezier(0.16, 1, 0.3, 1)`.
- Artifact actions: `opacity + translateY(2px)`, `120–160ms`, with no layout
  shift. Focus rings and keyboard navigation are instant.
- Reduced motion: spatial motion removed; opacity crossfade at most `150ms`.

## Microinteractions stance

- Silent success when the result is visible. Failures name what failed, why,
  and what to do next.
- Hover is wrapped in `@media (hover: hover)` and always has focus/tap parity.
- Tooltip delay: pointer `800–1000ms`, keyboard focus `0ms`.
- No `transition: all`, scale-on-everything, bounce, glow, parallax, cursor
  follower, auto-carousel, or layout-property animation.
- Desktop targets are at least `32×32px`; touch targets are at least `44×44px`.

## CTA and control voice

- Every button reserves a transparent 1px border at rest so layout never
  shifts; no button draws a persistent outline. Hover, keyboard focus, and
  active states may reveal one subtle semantic border.
- Primary: EcoreX brand fill, matching brand ink, control radius, specific verb;
  its idle border remains transparent and its hover border follows the fill.
- Secondary: transparent at rest, with a quiet tonal surface and rule only on
  hover/focus/active. Danger actions use red text without an idle box.
- Selected controls may use the selected tonal surface, but not a permanent
  outline. Separators are reserved for region structure, not button clusters.
- Icon-only actions use Lucide, a visible tooltip, and an accessible name.
- Every control covers default, hover, focus, active, disabled, loading, error,
  and success where the state is applicable.

## Per-page allowances

- App and admin pages must not use decorative enrichment; function carries the
  surface.
- Retouch is a viewport-inset dialog/workspace using the same theme, shape,
  type, focus, and elevation rules.
- Touch replaces hover rails with one visible More action and a bottom sheet.

## What pages MUST share

- Codex tonal base, EcoreX brand anchor, system CJK type, Lucide stroke voice,
  and four-point rhythm.
- Shape, surface, elevation, z-index, motion, focus, and responsive contracts.
- Backend projections are authoritative; UI components never infer business
  state from filenames, local storage, or optimistic routing guesses.

## Responsive contract

- `>=1200px`: full navigation and workspace.
- `840–1199px`: compact navigation; secondary header state moves to More.
- `<840px`: navigation drawer and full-width workspace (`radius: 0`).
- `<640px`: single-column artifacts; composer and HITL stack vertically;
  hover action rails become touch sheets.
- Artifact, message, and composer internals use container queries.

## Exports

`desktop/src/styles/tokens.css` is the canonical CSS export until the v1 WebUI
directory replaces the legacy `desktop` name. Release builds consume that file
once and pin its hashed bundle; overlays and copied bundles are forbidden.

CSS stamp for every app surface:

```css
/* Hallmark · genre: modern-minimal · macrostructure: Workbench · design-system: design.md · designed-as-app */
```
