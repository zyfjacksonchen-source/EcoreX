---
name: office-pdf
description: Create simple text PDFs and inspect extractable text and page structure from existing e-Mate PDF artifacts. Use for a new section-based PDF or read-only review and summarization of a current same-thread text PDF Artifact.
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

The packaged e-Mate native facade supports two operations in the first release:

- `create`: create a simple text PDF from a title and sections.
- `inspect`: extract bounded text and page counts from an existing e-Mate PDF Artifact so the model can review or summarize it.

## Native calls

Create with:

`{"operation":"create","file_name":"report.pdf","title":"...","sections":[{"heading":"...","paragraphs":["..."]}]}`

Inspect with the exact current Artifact identities:

`{"operation":"inspect","artifact_id":"art_...","revision_id":"rev_..."}`

Inspection accepts only a ready, unencrypted PDF Artifact owned by the current account and created in the current thread. It reads immutable Artifact bytes, never caller-provided filesystem paths. Pages without extractable text are reported; a parser failure aborts the operation. Extracted text is not proof of page layout.

## First-release boundary

The native facade does not edit an existing PDF, OCR scanned pages, extract tables as structured data, compare layouts, render pages, validate signatures, or perform visual QA. Do not claim those actions succeeded. When one is requested, state that the packaged native operation is unavailable.

Created files receive structural-open validation only. Report that page rendering and visual layout were not verified.
