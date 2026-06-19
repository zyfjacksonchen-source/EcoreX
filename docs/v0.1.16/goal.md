# v0.1.16 Production Goal

## Objective

v0.1.16 is a production-grade desktop iteration, not a development-only bugfix batch. The release must make the desktop app stable under long responses, session switches, local artifact generation, runtime restarts, slow startup, and long-running usage.

## User-Visible Problems

- F01: Streaming Markdown must render progressively without exposing raw Markdown blocks until completion.
- F02: `@skill` discovery must include all enabled skill sources and expose diagnostics for missing or invalid skills.
- F03: A newly created session must show an input caret immediately and keep focus stable.
- F04: Sending a message must produce an immediate accepted/phase response and must not require switching sessions to reveal results.
- F05: Local artifact links must open reliably for Windows absolute paths, relative paths, spaces, Chinese characters, and missing-file cases.

## Production Hardening

- F06: Reduce renderer work during streaming and long-session usage.
- F07: Make request lifecycle durable and replayable so runtime crashes, SSE reconnects, and late artifacts are recoverable.
- F08: Make sidecar supervision production-grade with process identity, port validation, graceful shutdown, and process-tree cleanup.
- F09: Add diagnostics, review evidence, and release gates that prove the build can be hand-tested and operated safely.

## Agent Process

- Implementation agents and review agents must be separate.
- Every feature needs parallel review from frontend/UX performance, backend/runtime stability, QA/release, and production/SRE perspectives.
- P0/P1 review disagreements block completion until reviewers converge on PASS.
- All evidence must be recorded in this folder before release.

