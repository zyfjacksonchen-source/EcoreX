# v0.1.19 Review Log

Independent review agents reviewed the implementation read-only. The writing
agent did not count as a reviewer.

| Reviewer | Focus | Status | Findings |
| --- | --- | --- | --- |
| Aristotle | UI/UX, clipping, visual smoke | PASS | Initial P2s found missing artifact-menu and add-to-chat visual evidence. Fixed with `artifact-menu-mobile` and `chat-file-context` smoke actions plus checklist evidence; re-review PASS with no P0/P1/P2. |
| Fermat | Runtime reconnect, retry, interrupt-send | PASS | Initial P2 found recovered request ids were not surfaced in `same_session`. Fixed with `accepted_after_recovery`, frontend consumption, and `/message` admission regression; re-review PASS with no P0/P1. |
| Hypatia | Cross-platform, security, path and lock handling | PASS | Initial P1 found live stale locks could be removed; P2s found `file://localhost`, `/uploads` non-image preview, and menu clamp issues. Fixed by dead-owner-only recovery, live-stale lock preservation, local file URL handling, preview-only filtering, and max-height menu clamp; re-review PASS with no P0/P1/P2. |
| Pascal | XHS image routing | PASS | Initial P1 found cached existing images could relabel old placeholder/Python output as final. Fixed by requiring matching OpenAI `gpt-image-2-pro` provenance, prompt hash, output path, and SHA256 before cache reuse; re-review PASS. |
| Ptolemy | Generic image-generation routing | PASS | Initial P1 found provider hints could route `gpt-image-2-pro` to Gemini/DashScope/Qwen. Fixed by restricting GPT Image model requests to OpenAI/LinkAI compatible providers and fail-closed behavior; re-review PASS. |
| Wegener | Evidence/docs/release guards | PASS | Confirmed pytest coverage and release validator markers for XHS/generic image routing. P2 noted default release artifact validator targets v0.1.18 public artifacts; documented as not applicable until v0.1.19 installers are produced. |

Consensus: PASS. No P0/P1 findings remain.
