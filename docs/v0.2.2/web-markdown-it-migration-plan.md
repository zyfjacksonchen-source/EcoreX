# Web Markdown-it Migration Reference Plan

## Memory Anchor

This document is the durable plan for the independent v0.2.2 Markdown-it slice. If the chat context is compacted or lost, restart from this file plus `goal.md` R22-18.

Non-negotiable direction:

- This is a Web-only slice.
- CowAgent Web console rendering is the reference behavior.
- The frontend may keep transient streaming render state, but backend runtime events/projections remain the source of truth for message content.
- Do not create a second Markdown parser, hand-rolled final renderer, or frontend-owned canonical message stream.
- Do not edit compiled Web app bundles by hand; update source files and harnesses only.

## Reference Snapshot

Reference source: [zhayujie/CowAgent](https://github.com/zhayujie/CowAgent.git).

The reference must be reproducible without relying on this chat context:

```powershell
$refRoot = Join-Path $env:TEMP 'cowagent-reference'
git clone --depth 1 https://github.com/zhayujie/CowAgent.git $refRoot
git -C $refRoot rev-parse HEAD
```

Snapshot inspected for this plan:

- Commit: `915edbe145aee033b6b5f31dcf57ba4c554a248e`
- Commit date: `2026-06-24 19:40:55 +0800`
- Commit subject: `fix(tools): make web SSRF protection opt-in, disabled by default`

Reference files:

- `%TEMP%/cowagent-reference/channel/web/chat.html`
- `%TEMP%/cowagent-reference/channel/web/static/js/console.js`
- `%TEMP%/cowagent-reference/channel/web/static/css/console.css`
- `%TEMP%/cowagent-reference/channel/web/static/vendor/markdown-it/markdown-it.min.js`
- `%TEMP%/cowagent-reference/channel/web/static/vendor/highlightjs/**`

The reference checkout is not committed into this repo. If it is missing, rerun the commands above.

## Slice Scope

This is an independent v0.2.2 Web-only slice. Desktop code is out of scope for this slice unless a later user request explicitly reopens it.

Goal: make Web chat streaming and final Markdown rendering consistently follow the original CowAgent Web console renderer instead of mixing raw text previews, partial Markdown paths, or hand-rolled parsing behavior.

Primary repo scope:

- `channel/web/chat.html`
- `channel/web/static/js/console.js`
- `channel/web/static/css/console.css`
- `channel/web/static/vendor/markdown-it/markdown-it.min.js`
- `channel/web/static/vendor/highlightjs/**`
- `tests/test_ecorex_web_parallel_backend.py` and any focused Web/browser harness added for this slice

Secondary Web surfaces that must be explicitly audited:

- live SSE assistant deltas
- `assistant.delta` / `assistant.snapshot` projection updates
- terminal `done` / `message_end`
- history replay
- projection-owned history refresh
- reconnect recovery from request/session projection
- long-answer collapsed preview and expanded view
- thinking/tool/content-step Markdown blocks
- memory and knowledge viewers
- artifact, image, video, and local-file preview rewrites
- copy-message raw Markdown metadata

## CowAgent Baseline

Baseline asset contract:

- `chat.html` loads vendored `assets/vendor/markdown-it/markdown-it.min.js`.
- `chat.html` loads vendored `highlight.js`, light/dark highlight styles, and focused language packs for Python, JavaScript, Java, Go, and Bash.
- No CDN dependency is required for Markdown rendering.

Baseline renderer contract from CowAgent `createMd()`:

```js
const md = markdownit({
  html: false,
  breaks: true,
  linkify: true,
  typographer: true,
  highlight: function(str, lang) {
    if (lang && hljsLib.getLanguage(lang)) {
      try { return hljsLib.highlight(str, { language: lang }).value; } catch (_) {}
    }
    return hljsLib.highlightAuto(str).value;
  }
});
```

Required baseline behaviors to preserve:

- Raw HTML in model/user content is escaped, not executed.
- Soft line breaks are visible because `breaks:true`.
- Plain URLs are linkified.
- Typographic punctuation follows `typographer:true`.
- Rendered links get `target="_blank"` and `rel="noopener noreferrer"`.
- Code blocks are highlighted through `highlight.js`.
- After Markdown render, local image/video previews are injected through the existing safe media rewrite pipeline.
- Code block language headers and copy buttons are added by DOM post-processing, not by putting ad hoc HTML into the Markdown source.
- Copy-message behavior uses the raw Markdown stored in `answer-content.dataset.rawMd`, not the rendered HTML.

Baseline CSS contract from CowAgent `.msg-content`:

- Paragraphs: `margin: 0.5em 0`, `line-height: 1.7`, first/last paragraph margins collapsed.
- Headings: `h1`/`h2`/`h3`/`h4`/`h5`/`h6` have stable margins, font weight, and line height.
- Lists: `ul` and `ol` have `padding-left: 1.8em`, visible list styles, and stable item margins.
- Code: `pre` uses rounded container, horizontal overflow, padding, and theme-aware background; inline code uses the green-tinted token style.
- Blockquote: green left border, subtle background, rounded trailing corners.
- Table: collapsed borders, full width, stable cell padding, theme-aware header/background.
- Images: max width 100 percent, rounded corners, stable vertical margin.
- Links: green underline with hover color.
- Code block wrapper: language label plus copy button after render.

## Current Web State To Audit

Current Web already includes vendored `markdown-it` and `highlight.js`, so this migration is not just "add a library". The work is to make all answer surfaces converge on one renderer contract and remove inconsistent raw/preview paths.

Known paths that must be audited before implementation PASS:

- `createMd()` and `renderMarkdown(text)` in `channel/web/static/js/console.js`.
- `renderAnswerHtml(...)`, long-answer collapse/preview, and history replay paths.
- Streaming paths around `contentEl.innerHTML`, `message_end`, `done`, and `message_update`/`delta`.
- Existing `streaming-markdown-preview` and `answer-stream-pre` paths.
- Thinking/tool intermediate content rendering, where Markdown may be intentionally limited for performance.
- Memory/Knowledge Markdown viewer paths that use the same renderer.
- Media rewrite helpers: `_rewriteLocalImgSrc`, `injectVideoPlayers`, `injectImagePreviews`.
- `applyHighlighting(container)` and `_addCodeBlockHeaders(container)` idempotency.
- Copy buttons and `dataset.rawMd` preservation.

## Migration Design

1. Keep one Web Markdown renderer.

   `renderMarkdown(text)` remains the only final renderer entry point. Any new helper must wrap it instead of creating a second Markdown implementation.

2. Add a streaming-safe render wrapper.

   Add a Web helper such as `renderStreamingMarkdown(text)` that:

   - splits content into a stable prefix and unstable tail;
   - renders the stable prefix with the same `markdown-it` instance;
   - hides or inertly buffers incomplete structural markers instead of showing raw `#`, raw fenced-code markers, half table delimiters, or dangling list syntax;
   - preserves normal prose, symbols, emoji, punctuation, and line breaks while content is still streaming.

3. Treat open code fences specially.

   While a code fence is open, the UI may show the code body in a stable code container, but the raw opening/closing fence markers must not be visible. Finalization must use the same `markdown-it` + `highlight.js` + code-header/copy path as completed messages.

4. Keep security behavior identical or stricter.

   - `html:false` is mandatory.
   - Link `target` and `rel` attributes are mandatory.
   - No direct insertion of model HTML is allowed outside sanitized Markdown output and existing safe media rewrite.
   - Local file/image/video preview rewrite must remain bounded to the existing trusted URL/path handling.

5. Keep Web projection direction intact.

   This slice must not introduce frontend-only authoritative message state. Streaming render state can be a view concern only. Final message truth remains backend event/projection driven.

6. Keep performance bounded.

   Rendering may be throttled or requestAnimationFrame-batched for streaming deltas. Any throttling must not change final content or hide terminal messages.

7. Keep final/streaming parity testable.

   Every user-visible answer surface must be traceable to either `renderMarkdown(text)` for final/static content or `renderStreamingMarkdown(text)` for live content. Any bounded exception must be documented in this plan before code is accepted.

## Complete Migration Surface Matrix

| Surface | Required renderer | State ownership | Pass condition |
| --- | --- | --- | --- |
| Live assistant text deltas | `renderStreamingMarkdown(...)` | view-only streaming state | no raw lone `#`, dangling list marker, raw fence marker, or partial table delimiter is visible |
| Final assistant answer | `renderMarkdown(...)` via `renderAnswerHtml(...)` | backend projection/history content | final DOM matches CowAgent Markdown-it/highlight behavior |
| Runtime projection refresh | `renderMarkdown(...)` or `renderStreamingMarkdown(...)` based on terminal state | backend projection | refresh/reconnect does not duplicate text or regress rendered layout |
| History replay | `renderMarkdown(...)` | backend history overlaid by runtime projection | restored messages render like live final messages |
| Long-answer preview | full `renderMarkdown(...)` before CSS clipping | backend content | preview does not slice raw Markdown or create broken HTML |
| Tool/content-step Markdown | same renderer unless performance-bounded exception is documented | backend event/projection payload | no ad hoc raw HTML insertion; code/link/media safety preserved |
| Memory/knowledge viewer | same renderer contract and CSS family | backend-loaded documents | headings/lists/tables/code/media match chat content behavior |
| Artifact/media rewrite | post-render safe rewrite only | backend artifact DTOs | safe URLs, escaped attributes, no scriptable raw HTML |
| Copy message | raw Markdown in `dataset.rawMd` | original message content | rendered HTML never replaces the raw-copy payload |

## Harness Plan

Golden Markdown fixtures:

- headings `#`, `##`, `###`
- unordered and ordered lists
- blockquote
- inline code and fenced code with language
- table
- horizontal rule
- links and bare URLs
- emoji and symbols
- Chinese and English mixed text with soft line breaks
- raw HTML/XSS attempt, expected escaped
- local image/video Markdown and bare media URLs, expected safe preview behavior

Streaming fixtures:

- chunks `["#", " Title"]` never show a raw lone `#`; final DOM contains a heading.
- chunks for `["- ", "item"]` do not show dangling list syntax; final DOM contains a list.
- chunks for code fence opening/body/closing never show raw triple backticks; final DOM has highlighted code, language header, and copy button.
- partial table delimiter does not flicker raw pipes into the user-facing message.
- emoji and punctuation survive every incremental render.

Source contract tests:

- Web `chat.html` still loads vendored `markdown-it` and `highlight.js`.
- Web final render path uses `renderMarkdown`.
- Web streaming render path uses the same `markdown-it` instance through the streaming wrapper.
- No Web answer surface falls back to `marked.js`, a desktop renderer, or a hand-written Markdown parser.
- `answer-content.dataset.rawMd` remains raw Markdown for copy.
- `applyHighlighting` and `_addCodeBlockHeaders` remain idempotent.

## Current CowAgent Delta To Preserve

The current repo has local v0.2.2 extensions around the CowAgent renderer. These must be kept unless a test proves they violate the reference behavior:

- `_toWebUrl(...)` routes local Windows paths, `/api/file`, `file:///`, and runtime-prefixed URLs safely.
- `_buildArtifactHtml(...)`, `_buildArtifactActionsHtml(...)`, `_buildImageHtml(...)`, and `_buildVideoHtml(...)` add artifact actions around rendered media.
- `renderAnswerHtml(...)`, long-answer preview/full view, runtime projection render, history projection render, memory viewer, and knowledge viewer already call `renderMarkdown(...)`.
- `renderStreamingMarkdown(...)` is the only allowed streaming wrapper. It may buffer incomplete structure, but it must call `renderMarkdown(...)` for stable Markdown.
- Runtime projection state remains backend-owned. This slice is view rendering only; it must not add frontend-only canonical message truth.

## Independent Slice Breakdown

WMD-00: Reference lock.

- Record source repo, commit hash, clone command, and baseline file list in this document.
- Acceptance: a future agent can recreate the reference snapshot without reading prior chat history.

WMD-01: Renderer unification.

- Audit every Web Markdown-producing path and route final/static content through `renderMarkdown(...)`.
- Remove or quarantine any answer path that inserts raw streaming text as authoritative final HTML.
- Acceptance: source harness proves there is one CowAgent-compatible renderer path for final Markdown.

WMD-02: Streaming wrapper.

- Keep streaming as a rendering concern only.
- Use `renderStreamingMarkdown(...)` to split stable prefix from unstable tail and render stable content through `renderMarkdown(...)`.
- Hide or inertly buffer lone headings, dangling list markers, partial table delimiters, and raw fence markers.
- Acceptance: streaming fixtures prove no raw lone `#`, raw triple backticks, dangling list syntax, or partial table separator reaches visible output.

WMD-03: Code block parity.

- Preserve CowAgent `highlight.js` behavior and DOM post-processing for language labels and copy buttons.
- Keep `_addCodeBlockHeaders(...)` idempotent.
- Ensure open-fence streaming previews do not break final code-block wrapping.
- Acceptance: code fixtures cover known language, unknown language, open fence, final fence, copy button, and duplicate-header prevention.

WMD-04: Media/security parity.

- Preserve `html:false`, secure link attributes, safe local media rewrite, image/video preview injection, and raw Markdown copy metadata.
- Keep XSS attempts escaped in both streaming and final render.
- Acceptance: fixture includes raw HTML, scriptable image attributes, bare image/video links, Markdown images, local paths, and plain URLs.

WMD-05: Layout parity.

- Keep `.msg-content` spacing compatible with CowAgent: paragraphs, headings, lists, code, blockquotes, tables, images, links, horizontal rules, and user-bubble overrides.
- Do not introduce large layout shifts during streaming/final swap.
- Acceptance: source/CSS harness plus browser screenshot smoke compare key DOM classes and visible no-raw-syntax output.

WMD-06: Projection integration.

- Ensure history projection, active request projection, reconnect recovery, and long-answer collapse all render the same Markdown output after backend projection refresh.
- Acceptance: hard refresh/network reconnect smoke proves projection-derived messages render like live streaming/final messages.

Browser smoke:

- Start the Web console locally.
- Send or inject the golden fixture answer.
- Capture the rendered answer area.
- Confirm no raw `#`, raw triple backticks, or broken list/table markers appear during streaming.
- Confirm final rendered layout matches CowAgent-style `.msg-content` spacing and code block controls.
- Confirm dark/light highlight CSS remains usable.

Review gates:

- Implementer cannot grant PASS.
- Required review angles: Web renderer/state, harness/test, security/XSS, release/regression, and UX/readability.
- PASS requires agreement that the Web renderer follows the CowAgent baseline and does not regress the backend-led v0.2.2 runtime direction.

## Implementation Order For A Fresh Agent

1. Recreate or verify the CowAgent reference snapshot and commit hash.
2. Inspect `channel/web/static/js/console.js` from `// Markdown Renderer` through answer/history/projection rendering paths.
3. Inspect `channel/web/static/css/console.css` for `.msg-content`, streaming, long-answer, table, code, media, memory, and knowledge styles.
4. Add or update source harnesses before broad edits so regressions are visible.
5. Unify final render paths on `renderMarkdown(...)`.
6. Route live assistant streaming through `renderStreamingMarkdown(...)`.
7. Add golden fixtures for final Markdown, streaming instability, XSS/link safety, code headers/copy, media rewrites, and long-answer preview.
8. Run Python/Node source harnesses, then browser smoke when Browser tooling is available.
9. Send the slice to independent multi-agent review. The implementer records findings and fixes but cannot self-PASS.
10. Update `acceptance-checklist.md`, `evidence-ledger.md`, and `review-log.md` with exact command output summaries and reviewer status.

## Acceptance Status Vocabulary

- `REFERENCE-PLAN-WRITTEN`: this document exists and points to concrete CowAgent baseline files.
- `REFERENCE-PLAN-LOCKED`: this document records a reproducible CowAgent repo URL, commit hash, file list, and current-repo deltas.
- `LOCAL-PASS-REVIEW-PENDING`: implementation and local Web tests are green, but independent review has not granted PASS.
- `LOCAL-PASS-REVIEWED-BROWSER-PENDING`: implementation, local harnesses, and independent review have no remaining P0/P1/P2, but browser smoke is still pending.
- `BROWSER-PASS-REVIEW-REFRESH-PENDING`: local implementation and browser smoke pass, but fresh independent review of the browser harness/evidence has not converged yet.
- `PASS`: local implementation, browser smoke, and independent multi-angle review all converge with no open P0/P1.
