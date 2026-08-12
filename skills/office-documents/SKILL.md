---
name: office-documents
description: Create, replacement-edit, and inspect structurally valid DOCX files through the verified Office Pack.
mentionable: true
mention-category: document
user-invocable: true
compatibility-id: office-documents
adopts-official-skill: documents
ecorex-native-facade: true
quality-gates:
  - structural-open
  - artifact-integrity
metadata: {"default_enabled":true,"requires":{"modules":["docx"]}}
---

# Office Documents

The packaged Cow tool supports these operations through the verified Office Pack:

- `create`: create a new, simple DOCX from a title and sections.
- `edit`: write a new `-edited.docx` from complete title/sections content after the source opens successfully. Set `output_path` explicitly to replace that exact path atomically.
- `inspect`: extract text plus paragraph/table counts from an existing DOCX.

## Native calls

Create with `path`; edit the returned path with the same complete structured content:

`{"action":"create","path":"report.docx","title":"...","sections":[{"heading":"...","paragraphs":["..."]}]}`

`{"action":"edit","path":"report.docx","output_path":"report-v2.docx","title":"...","sections":[{"heading":"...","paragraphs":["..."]}]}`

Inspect with `{"action":"inspect","path":"report.docx"}`.

Inspection resolves the supplied local path through the Cow file-access broker and opens it in the same verified Office Pack. The returned text is suitable for content review and summarization, but it is not evidence of page layout.

Replacement edit rewrites the complete simple title/sections structure; it does not preserve complex formatting, comments, or redlines. Do not claim those unsupported properties survived.

Created files receive structural-open validation only. Report that visual layout was not verified.
