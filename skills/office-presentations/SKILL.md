---
name: office-presentations
description: Create simple PPTX decks and inspect visible text and slide counts from existing e-Mate PPTX artifacts. Use for a new title-and-bullets deck or read-only review and summarization of a current same-thread presentation Artifact.
mentionable: true
mention-category: document
user-invocable: true
compatibility-id: office-presentations
adopts-official-skill: Presentations
ecorex-native-facade: true
quality-gates:
  - structural-open
  - artifact-integrity
metadata: {"default_enabled":true,"requires":{"modules":["pptx"]}}
---

# Office Presentations

The packaged e-Mate native facade supports two operations in the first release:

- `create`: create a simple PPTX from bounded slide titles and bullets.
- `inspect`: extract bounded visible shape text and the slide count from an existing e-Mate PPTX Artifact so the model can review or summarize it.

## Native calls

Create with:

`{"operation":"create","file_name":"deck.pptx","title":"...","slides":[{"title":"...","bullets":["..."]}]}`

Inspect with the exact current Artifact identities:

`{"operation":"inspect","artifact_id":"art_...","revision_id":"rev_..."}`

Inspection accepts only a ready PPTX Artifact owned by the current account and created in the current thread. It reads immutable Artifact bytes, never caller-provided filesystem paths. Extracted text is not proof of layout, overlap, clipping, charts, images, or speaker-note fidelity.

## First-release boundary

The native facade does not edit or restyle an existing deck, preserve its template, generate charts or visual assets, import Google Slides, render slides, detect overlap, or perform visual QA. Do not claim those actions succeeded. When one is requested, state that the packaged native operation is unavailable instead of presenting a newly created deck as an edit of the original.

Created files receive structural-open validation only. Report that visual layout was not verified.
