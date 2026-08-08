---
name: office-pdf
description: Read, extract, summarize, create, inspect, and verify .pdf/PDF files, including office-exported PDFs where page layout matters.
mentionable: true
mention-category: document
user-invocable: true
compatibility-id: office-pdf
adopts-official-skill: pdf
ecorex-native-facade: true
quality-gates:
  - text-orientation
  - page-render
  - layout-inspection
  - table-structure
  - generation-verify
  - visual-diff
metadata:
  cowagent:
    default_enabled: true
    requires:
      modules:
        - pypdf
        - pdfminer
---

# Office PDF

This is the EcoreX-native compatibility facade for the official Codex `pdf`
workflow. Keep the public EcoreX skill ID `office-pdf` stable, but use the
official `pdf` skill as the authoritative workflow when it is available in
`<available_skills>`.

If both skills are visible, read this skill first for EcoreX compatibility
rules, then read `pdf` for Poppler rendering, PDF generation, extraction, and
visual verification details. If the official skill is not visible, follow the
equivalent contract below and use the safest available local tools.

Use this skill when the user asks EcoreX to read, summarize, extract, compare, create, or verify PDFs. Use it especially when page layout, tables, images, signatures, invoices, contracts, reports, or scanned/office-exported documents matter.

## Default Workflow

1. Classify the task as extraction, summary, review, comparison, conversion, or new PDF creation.
2. Use text extraction for quick orientation, but do not rely on text alone when layout matters.
3. Render or inspect pages when the user needs a layout-sensitive answer. Prefer Poppler `pdftoppm`/`pdfinfo` when available.
4. For scanned or image-heavy PDFs, use OCR/image analysis when text extraction is incomplete.
5. For generated PDFs, use reliable PDF creation libraries such as ReportLab when available, then render pages and verify that pages are legible, aligned, and free of clipping or broken glyphs before delivery.
6. When a reference PDF exists, run page-level visual-diff evidence before claiming the generated or converted PDF matches the reference layout.

## Quality Contract

- Mention extraction limitations when a PDF is scanned, protected, damaged, or image-only.
- Preserve citations to page numbers or source files when answering questions from a PDF.
- Tables should be extracted as structured data when possible, not as unreadable text blocks.
- Generated PDFs must pass visual page inspection: typography, margins, headers/footers, page numbers, tables, charts, and images should be aligned, sharp, and readable.
- Do not deliver a generated PDF with clipped text, overlapping objects, unreadable glyphs, broken tables, blank pages, or black-square rendering artifacts.
- Reference comparisons must pass the PDF QA evidence builder's page-count, page-size, orientation, text-length-bucket, table-candidate, and image-object checks before being reported as layout-equivalent.
- If rendering is unavailable, clearly report which visual verification layer could not run and avoid claiming page-level QA passed.
- Final response should link to final generated PDFs only when the user requested a deliverable.

## EcoreX Adaptation

- This is a user-invocable office skill and should appear under the document category in `@skill`.
- For a new PDF, call `skill_run` with this exact discovery ID and parameters shaped as `{"operation":"create","file_name":"report.pdf","title":"...","sections":[{"heading":"...","paragraphs":["..."]}]}`. The Runtime-owned Office Pack creates and structurally validates the file, then publishes the resulting Artifact; do not fall back to `pip` or an untracked shell output.
- The `office-pdf` capability pack provides PDF and Office parsing modules in packaged desktop builds.
- Preserve compatibility with existing prompts, shortcuts, and automations that mention `office-pdf`.
- Prefer official Codex workspace dependencies for PDF extraction, generation, Poppler render, and page-level QA when the host exposes them.
- The `office-pdf` capability pack remains the EcoreX runtime compatibility pack for legacy parsing/preview.
- Use the EcoreX PDF QA evidence builder for `text-orientation`, `page-render`, `layout-inspection`, `table-structure`, `generation-verify`, and `visual-diff`; render evidence must come from trusted runtime render output, not caller-provided metadata.
- If PDF modules or renderers are unavailable, continue with the safest available extraction method and clearly state the missing verification layer.
