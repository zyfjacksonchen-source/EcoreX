---
name: office-documents
description: Create structurally valid DOCX files and inspect text and structure from existing e-Mate DOCX artifacts. Use for new Word-style reports or read-only review and summarization of a current same-thread DOCX artifact.
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

The packaged e-Mate native facade supports two operations in the first release:

- `create`: create a new, simple DOCX from a title and sections.
- `inspect`: extract text plus paragraph/table counts from an existing e-Mate DOCX Artifact so the model can review or summarize it.

## Native calls

Create with:

`{"operation":"create","file_name":"report.docx","title":"...","sections":[{"heading":"...","paragraphs":["..."]}]}`

Inspect with the exact current Artifact identities:

`{"operation":"inspect","artifact_id":"art_...","revision_id":"rev_..."}`

Inspection accepts only a ready DOCX Artifact owned by the current account and created in the current thread. It reads immutable Artifact bytes, never caller-provided filesystem paths. The returned text is suitable for content review and summarization, but it is not evidence of page layout.

## First-release boundary

The native facade does not edit an existing DOCX, preserve formatting during rewrite, add comments or redlines, import Google Docs, render pages, or perform visual QA. Do not claim those actions succeeded. When one is requested, state that the packaged native operation is unavailable instead of presenting a newly created file as an edit of the original.

Created files receive structural-open validation only. Report that visual layout was not verified.
