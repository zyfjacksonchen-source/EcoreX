---
name: office-presentations
description: Create, edit, summarize, and verify PowerPoint/PPTX presentation decks. Use for .ppt, .pptx, slide outlines, speaker decks, visual story flow, and Google Slides-targeted local deck workflows.
mentionable: true
mention-category: document
user-invocable: true
metadata:
  cowagent:
    default_enabled: true
    requires:
      modules:
        - pptx
---

# Office Presentations

Use this skill when the user asks EcoreX to work with presentation decks: create slides, edit a `.ppt`/`.pptx`, summarize a deck, convert notes into slides, polish layout, or prepare a local PowerPoint deliverable.

## Default Workflow

1. Identify the deck job: read/summarize, edit existing deck, create from outline, convert a document/table into slides, or prepare a polished deliverable.
2. Establish the story flow before authoring slides. Each slide should have one clear purpose.
3. For existing decks, preserve the template, brand, typography, footers, and layout conventions unless the user asks to restyle.
4. For new decks, choose a practical office style: readable hierarchy, consistent margins, and useful visuals without overcrowding.
5. Keep slide text concise. Shorten content before shrinking fonts.
6. Verify the deck visually or structurally before delivery; fix unintended overlap and unreadable text.

## Quality Contract

- No unintended overlap, clipped text, broken charts, or empty placeholder slides.
- Slide titles should be readable and should not wrap unexpectedly.
- Use real images or chart outputs when visuals are needed; do not rely on decorative placeholders.
- For data-heavy decks, keep source tables or notes available in the workspace.
- Final response should link to the final `.pptx` deliverable, not scratch files, unless requested.

## EcoreX Adaptation

- This is a user-invocable office skill and should appear under the document category in `@skill`.
- Basic deck parsing/editing helpers are provided by the `office-pdf` capability pack when preinstalled.
- If presentation modules are unavailable, guide the user to enable/install the Office/PDF capability pack before continuing.
