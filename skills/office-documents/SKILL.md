---
name: office-documents
description: Read, create, edit, summarize, redline, and verify Word/DOCX office documents. Use for .doc, .docx, Word, and Google Docs-targeted local document workflows.
mentionable: true
mention-category: document
user-invocable: true
metadata:
  cowagent:
    default_enabled: true
    requires:
      modules:
        - docx
---

# Office Documents

Use this skill when the user asks EcoreX to work with Word-style office documents: read, summarize, rewrite, create, edit, redline, comment on, or package a `.doc`/`.docx` deliverable.

## Default Workflow

1. Identify the user's goal: read-only analysis, targeted edit, new document, redline/comment, or export-ready deliverable.
2. Use local file tools for user-provided files. For web links, fetch the source first, save citations or source notes, then create/edit the document.
3. For existing documents, preserve the original structure and apply the smallest correct edit unless the user asks for a rewrite.
4. For new documents, choose an office-appropriate archetype such as memo, report, SOP, proposal, checklist, brief, or form before drafting.
5. Generate final files under the workspace output area or a user-requested path.
6. Verify before delivery. Prefer a render-to-PDF/PNG or structural OOXML check when rendering is unavailable.

## Quality Contract

- Do not treat raw extracted text as layout proof. Tables, lists, headers, footers, and page breaks must be checked.
- Use real document structure: headings, numbered lists, tables, comments, and metadata instead of visual text hacks.
- Tables must have explicit widths and readable spacing. Avoid cramped all-grid layouts for prose-heavy material.
- If visual rendering is unavailable, clearly report that only structural verification passed.
- Final response should link to the final `.docx` deliverable, not scratch files, unless the user asks for intermediates.

## EcoreX Adaptation

- This is a user-invocable office skill and should appear under the document category in `@skill`.
- Heavy document parsing and preview helpers are provided by the `office-pdf` capability pack when preinstalled.
- If required modules are missing, ask to enable/install the Office/PDF capability pack instead of guessing with unrelated libraries.
