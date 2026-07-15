# EcoreX v1 Capability Pack sources

These are the product-owned sources consumed by the protected platform
stager. They are not installed from a user's machine and they do not select
tools or policy. The backend-owned signed `ToolSpec` and frozen Turn snapshot
remain authoritative.

- `browser` exposes only bounded `fetch` plus a fixed Playwright lifecycle for
  an allowlisted set of page operations. The stager vendors one exact Chromium
  and Playwright closure as a digest-indexed nested archive.
- `image` is a non-provider handshake. It proves that image generation and
  vision are supplied by `core-managed-image-v1`; provider URLs and credentials
  are deliberately absent.
- `sandbox` translates the one `shell` ToolSpec into a fixed OS shell. It runs
  only after acknowledging the complete Core-authored sandbox contract. The
  Windows AppContainer/Job Object helper or macOS Seatbelt owns enforcement.

All executable packs parse one bounded canonical request, reject unknown
fields, emit one canonical response, inherit a reduced environment and never
load a module or executable named by the user.
