# e-Mate v0.3.0 Design QA

## Sources

- Confirmed latest dark reference: `C:\e-Mate-正式版\.tmp\e-mate-latest-home-2.1.47.png`
- Latest implementation source: `C:\e-Mate-正式版\packages\desktop\src\renderer\pages\guid\GuidPage.tsx` and its 2.1.47 theme/components
- Light reference using the same e-Mate layout/token family: `C:\e-Mate-正式版\.tmp\screenshots\e-mate-2.1.44-home-light-1440x900.png`
- Dark implementation: `C:\EcoreX-Agent生产版\docs\v0.3.0\artifacts\design-qa\home-dark-implementation-1440x900.png`
- Light implementation: `C:\EcoreX-Agent生产版\docs\v0.3.0\artifacts\design-qa\home-light-implementation-1440x900.png`

## Capture state

- In-app Browser, `1440 × 900`, device pixel ratio `1`
- Authenticated managed account, connected Runtime, new-task home
- Dark and light themes captured from the same production build and same browser session
- Console errors: none

## Comparison

The reference and implementation were emitted together at the same viewport for direct comparison. The five-robot asset, hero scale, workspace/sidebar split, composer placement, overview cards, trend row, radii, rules, typography hierarchy and e-Mate palette align with the confirmed source.

Intentional product-preserving differences:

- The Electron title bar/window controls are omitted because v0.3.0 remains WebUI.
- The retained WebUI Composer continues to expose its real connector, Luna-high, permission and send controls.
- Projects, tasks and Usage values use current Runtime facts instead of screenshot fixtures.
- The existing session search remains available in the sidebar header.

## Interaction QA

- Settings opens from the restored sidebar row and exposes current/new/confirm password fields.
- User Center remains a separate sidebar entry with the authenticated account menu.
- Creative Center returns to the Composer; an empty draft is prefilled, an existing draft is preserved, and the textarea receives focus.
- Capability Center exposes Discover/Installed/Custom, search/category/tag/source, detail, upload, download, install, configuration, enable/disable and authoritative uninstall actions.
- Theme toggle, model selector, permission control, project selector and notification/settings actions remain operable.

## Iterations

1. Restored the source sidebar footer hierarchy and connected home status/action styling.
2. Corrected logo dimensions/position and removed the extra visible version label.
3. Found that Creative Center unmounted Composer and invalidated the draft guard; moved draft ownership to the main workspace and verified both empty/existing draft paths in the browser.

final result: passed
