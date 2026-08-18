---
name: office-pdf
description: Create, replacement-edit, and inspect simple text PDFs through the verified Office Pack.
mentionable: true
mention-category: document
user-invocable: true
compatibility-id: office-pdf
adopts-official-skill: pdf
ecorex-native-facade: true
quality-gates:
  - structural-open
  - text-extraction
  - artifact-integrity
metadata: {"default_enabled":true,"requires":{"modules":["pypdf","reportlab"]}}
---

# Office PDF

The packaged Cow tool supports these operations through the verified Office Pack:

- `create`: create a simple text PDF from a title and sections.
- `edit`: write a new `-edited.pdf` from complete title/sections content after the source opens successfully. Set `output_path` explicitly to replace that exact path atomically.
- `inspect`: extract bounded text and page counts from an existing PDF.

## Native calls

Create with:

`{"action":"create","path":"report.pdf","title":"...","sections":[{"heading":"...","paragraphs":["..."]}]}`

Edit with the same complete structured content and returned path:

`{"action":"edit","path":"report.pdf","output_path":"report-v2.pdf","title":"...","sections":[{"heading":"...","paragraphs":["..."]}]}`

Inspect with `{"action":"inspect","path":"report.pdf"}`.

Inspection resolves the supplied local path through the Cow file-access broker and opens it in the same verified Office Pack. Pages without extractable text are reported; a parser failure aborts the operation. Extracted text is not proof of page layout.

Replacement edit rewrites the complete simple title/sections PDF; it does not preserve arbitrary PDF layout or signatures. Do not claim those unsupported properties survived.

Created files receive structural-open validation only. Report that page rendering and visual layout were not verified.
