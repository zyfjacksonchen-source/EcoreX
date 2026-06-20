---
name: office-pdf
description: Read, extract, summarize, create, inspect, and verify .pdf/PDF files, including office-exported PDFs where page layout matters.
mentionable: true
mention-category: document
user-invocable: true
metadata:
  cowagent:
    default_enabled: true
    requires:
      modules:
        - pypdf
        - pdfminer
---

# Office PDF

Use this skill when the user asks EcoreX to read, summarize, extract, compare, create, or verify PDFs. Use it especially when page layout, tables, images, signatures, invoices, contracts, reports, or scanned/office-exported documents matter.

## Default Workflow

1. Classify the task as extraction, summary, review, comparison, conversion, or new PDF creation.
2. Use text extraction for quick orientation, but do not rely on text alone when layout matters.
3. Render or inspect pages when the user needs a layout-sensitive answer.
4. For scanned or image-heavy PDFs, use OCR/image analysis when text extraction is incomplete.
5. For generated PDFs, verify that pages are legible, aligned, and free of clipping or broken glyphs before delivery.

## Quality Contract

- Mention extraction limitations when a PDF is scanned, protected, damaged, or image-only.
- Preserve citations to page numbers or source files when answering questions from a PDF.
- Tables should be extracted as structured data when possible, not as unreadable text blocks.
- Final response should link to final generated PDFs only when the user requested a deliverable.

## EcoreX Adaptation

- This is a user-invocable office skill and should appear under the document category in `@skill`.
- The `office-pdf` capability pack provides PDF and Office parsing modules in packaged desktop builds.
- If PDF modules or renderers are unavailable, continue with the safest available extraction method and clearly state the missing verification layer.
