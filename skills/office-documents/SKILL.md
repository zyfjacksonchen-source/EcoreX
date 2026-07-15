---
name: office-documents
description: Read, create, edit, summarize, redline, and verify Word/DOCX office documents. Use for .doc, .docx, Word, and Google Docs-targeted local document workflows.
mentionable: true
mention-category: document
user-invocable: true
compatibility-id: office-documents
adopts-official-skill: documents
ecorex-native-facade: true
quality-gates:
  - design-preset
  - structure-check
  - render-docx
  - table-geometry
  - visual-inspection
  - redline-preserve
metadata:
  cowagent:
    default_enabled: true
    requires:
      modules:
        - docx
---

# Office Documents

This is the EcoreX-native compatibility facade for the official Codex
`documents` workflow. Keep the public EcoreX skill ID `office-documents`
stable, but use the official `documents` skill as the authoritative workflow
when it is available in `<available_skills>`.

If both skills are visible, read this skill first for EcoreX compatibility
rules, then read `documents` for design presets, deterministic OOXML helpers,
render-to-PNG visual QA, Google Docs-targeted sanitization, comments, redlines,
and table geometry details. If the official skill is not visible, follow the
equivalent contract below and use the safest available local tools.

Use this skill when the user asks EcoreX to work with Word-style office documents: read, summarize, rewrite, create, edit, redline, comment on, or package a `.doc`/`.docx` deliverable.

## Default Workflow

1. Identify the user's goal: read-only analysis, targeted edit, new document, redline/comment, or export-ready deliverable.
2. Use local file tools for user-provided files. For web links, fetch the source first, save citations or source notes, then create/edit the document.
3. For existing documents, preserve the original structure and apply the smallest correct edit unless the user asks for a rewrite.
4. For new documents, choose an office-appropriate archetype such as memo, report, SOP, proposal, checklist, brief, or form before drafting.
5. For new documents or major rewrites, choose and apply one design preset with explicit typography, margins, spacing, list, table, header, footer, and color tokens.
6. Generate final files under the workspace output area or a user-requested path.
7. Verify before delivery. Render DOCX pages to PNG when possible, inspect every page, fix layout defects, and re-render until clean. If rendering is unavailable, run structural OOXML checks and disclose that visual QA was skipped.

## Quality Contract

- Do not treat raw extracted text as layout proof. Tables, lists, headers, footers, and page breaks must be checked.
- Use real document structure: headings, numbered lists, tables, comments, and metadata instead of visual text hacks.
- Tables must have explicit widths, table grids, cell widths, padding, and readable spacing. Avoid cramped all-grid layouts for prose-heavy material.
- Lists must use real Word numbering definitions rather than fake bullet text or manual numbering.
- For edits, preserve the original and make minimal local changes unless the user explicitly requests a rewrite.
- For redlines/comments, keep feedback anchored near the changed text rather than dumping all notes at the end.
- For Google Docs-targeted output, create and verify a local `.docx`, run the official title sanitizer when available, then import through the appropriate cloud-document route.
- If visual rendering is unavailable, clearly report that only structural verification passed; render-docx evidence must come from trusted runtime render output, not caller-provided metadata.
- Final response should link to the final `.docx` deliverable, not scratch files, unless the user asks for intermediates.

## EcoreX Adaptation

- This is a user-invocable office skill and should appear under the document category in `@skill`.
- Preserve compatibility with existing prompts, shortcuts, and automations that mention `office-documents`.
- Prefer official Codex workspace dependencies and helper scripts for DOCX creation, deterministic OOXML edits, render, comment, redline, title sanitization, and accessibility/redaction checks when the host exposes them.
- The `office-pdf` capability pack remains a fallback for legacy parsing/preview and small local edits, but high-quality document creation should follow the official `documents` render-and-verify workflow.
- If required authoring or rendering dependencies are unavailable, report the missing verification layer clearly instead of implying visual QA passed.
