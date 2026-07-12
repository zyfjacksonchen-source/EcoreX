# EcoreX v1 Web bundle report

## Scope

This report records the production Web bundle split for the thin React client.
It covers only browser download, parse and feature-loading behavior. Backend
authority, Runtime contracts and the system-health Settings contract are
unchanged.

## Measured result

Measurements use the minified Vite production output on Windows x64 with the
locked workspace dependencies. Vite reports decimal kB; the release gate also
reports binary KiB from the final content-addressed files.

| Asset or path | Before | After | Initial load |
| --- | ---: | ---: | --- |
| Workspace entry | 517.68 kB / gzip 161.21 kB | 55.05 KiB / gzip 16.11 KiB | yes |
| Vendor Runtime | part of entry | 346.22 kB / gzip 108.00 kB | modulepreload |
| EcoreX API/state client | part of entry | 61.91 kB / gzip 19.17 kB | modulepreload |
| Shared UI primitives | part of entry | 1.04 kB / gzip 0.56 kB | modulepreload |
| Six low-frequency features | part of entry | 64.20 KiB / gzip 22.52 KiB total | no |

Final initial JavaScript is 454.64 KiB, gzip 140.61 KiB. Compared with the
single 517.68 kB / gzip 161.21 kB entry, initial JavaScript fell by about 11%
and the entry itself fell by about 89%. No production chunk is above 500 KiB,
so the previous Vite large-chunk advisory is gone.

Deferred feature output:

| Feature chunk | Minified | Gzip |
| --- | ---: | ---: |
| Artifact preview | 2.57 kB | 1.18 kB |
| Share | 8.09 kB | 3.09 kB |
| Replay and diagnostics | 8.47 kB | 3.22 kB |
| Settings, including system health and output location | 12.84 kB | 4.09 kB |
| Extension manager | 11.51 kB | 3.93 kB |
| Precise retouch workspace | 22.28 kB | 7.47 kB |

## Runtime behavior

- React loads each low-frequency feature only after first use. Pointer and
  keyboard intent warm common dialogs, while preview and retouch start from the
  backend-projected Artifact action.
- After first open, the feature subtree stays mounted when its dialog closes.
  Drafts and dialog-local state therefore behave as before on reopen.
- Suspense shows a bounded plain-language loading surface using the same Radix
  modal primitive as product dialogs, including focus containment, Escape and
  deterministic trigger restoration. A lazy resource or
  render failure is contained to that feature instead of crashing the task
  workspace. The recovery action refreshes the page, which also handles a
  content-addressed chunk replaced during an activated update.
- Radix focus/dialog behavior and Lucide tree-shaking remain intact. There is no
  per-feature or per-icon manual chunk list.

## Build graph and release safety

The content-addressing gate requires an acyclic dependency graph because every
filename contains its final content digest. Default Rollup placement caused a
lazy feature to import helpers from the entry while the entry dynamically
imported that feature. Instead of weakening the hash gate, Vite now uses three
stable architecture layers: third-party Runtime, EcoreX API/state client and
shared UI primitives. Feature names are not part of the manual chunk policy.

`tools/check-v1-bundle.mjs` runs after final SHA-256 asset rewriting and fails
the build when:

- the workspace entry exceeds 128 KiB;
- initial JavaScript exceeds 475 KiB or gzip 150 KiB;
- any chunk exceeds the 500 KiB advisory boundary;
- one of the six feature chunks is missing, duplicated or module-preloaded;
- the entry no longer references the deferred feature.

This is a regression budget, not a synthetic Core Web Vitals claim. Final LCP,
INP and device-level parse measurements remain part of the packaged Windows and
macOS release-candidate browser run.
