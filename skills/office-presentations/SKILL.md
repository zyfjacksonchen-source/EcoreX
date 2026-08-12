---
name: office-presentations
description: Create, replacement-edit, and inspect simple PPTX decks through the verified Office Pack.
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

The packaged Cow tool supports these operations through the verified Office Pack:

- `create`: create a simple PPTX from bounded slide titles and bullets.
- `edit`: write a new `-edited.pptx` from complete title/bullets slides after the source opens successfully. Set `output_path` explicitly to replace that exact path atomically.
- `inspect`: extract bounded visible shape text and the slide count.

## Native calls

Create with:

`{"action":"create","path":"deck.pptx","title":"...","slides":[{"title":"...","bullets":["..."]}]}`

Edit with the same complete structured content and returned path:

`{"action":"edit","path":"deck.pptx","output_path":"deck-v2.pptx","title":"...","slides":[{"title":"...","bullets":["..."]}]}`

Inspect with `{"action":"inspect","path":"deck.pptx"}`.

Inspection resolves the supplied local path through the Cow file-access broker and opens it in the same verified Office Pack. Extracted text is not proof of layout, overlap, clipping, charts, images, or speaker-note fidelity.

Replacement edit rewrites the complete simple title/bullets deck; it does not preserve templates, charts, or complex layout. Do not claim those unsupported properties survived.

Created files receive structural-open validation only. Report that visual layout was not verified.
