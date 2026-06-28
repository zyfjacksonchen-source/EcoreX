---
name: office-presentations
description: Create, edit, summarize, and verify PowerPoint/PPTX presentation decks. Use for .ppt, .pptx, slide outlines, speaker decks, visual story flow, and Google Slides-targeted local deck workflows.
mentionable: true
mention-category: document
user-invocable: true
compatibility-id: office-presentations
adopts-official-skill: Presentations
ecorex-native-facade: true
quality-gates:
  - story-flow
  - artifact-tool-authoring
  - layout-bounds
  - font-size-check
  - chart-integrity
  - render-preview
  - overlap-check
  - visual-inspection
metadata:
  cowagent:
    default_enabled: true
    requires:
      modules:
        - pptx
---

# Office Presentations

This is the EcoreX-native compatibility facade for the official Codex
`Presentations` workflow. Keep the public EcoreX skill ID
`office-presentations` stable, but use the official `Presentations` skill as
the authoritative workflow when it is available in `<available_skills>`.

If both skills are visible, read this skill first for EcoreX compatibility
rules, then read `Presentations` for implementation details, asset-tool usage,
layout library guidance, and QA commands. If the official skill is not visible,
follow the equivalent contract below and use the safest available local tools.

Use this skill when the user asks EcoreX to work with presentation decks: create slides, edit a `.ppt`/`.pptx`, summarize a deck, convert notes into slides, polish layout, or prepare a local PowerPoint deliverable.

## Default Workflow

1. Identify the deck job: read/summarize, edit existing deck, create from outline, convert a document/table into slides, or prepare a polished deliverable.
2. Establish the story flow before authoring slides. Each slide should have one clear purpose and the deck must have a coherent slide-to-slide narrative.
3. For existing decks, preserve the template, brand, typography, footers, page markers, and layout conventions unless the user asks to restyle.
4. For new decks, use the official `Presentations` composition route when available: `@oai/artifact-tool` JavaScript ES modules, Codex Grid layouts when there is no stronger template, and project-safe scratch/output paths.
5. Keep slide text concise. Shorten content before shrinking fonts. Do not allow intended one-line titles or banners to wrap.
6. Use real visual assets when visuals are needed. Do not fake final slide visuals with decorative placeholders or Python-drawn images.
7. Verify the deck visually before delivery: render previews/contact sheets, inspect for overlap/clipping/wrapping/chart issues, and iterate until clean.

## Quality Contract

- No unintended overlap, clipped text, broken charts, or empty placeholder slides.
- Slide titles should be readable and should not wrap unexpectedly.
- When no template controls typography, use at least 50pt deck title, 35pt slide titles, 24pt subheads/callouts, and 16pt body text.
- Run the EcoreX presentation QA evidence builder after authoring or editing. Treat `story-flow`, `layout-bounds`, `font-size-check`, `chart-integrity`, `render-preview`, `overlap-check`, and `visual-inspection` failures as blockers.
- Treat programmatic overlap warnings as blockers until visually inspected and resolved.
- Use real images or chart outputs when visuals are needed; do not rely on decorative placeholders.
- For data-heavy decks, keep source tables or notes available in the workspace.
- For Google Slides-targeted output, create and verify a local `.pptx` first, then import through the appropriate cloud-document route when available.
- Final response should link to the final `.pptx` deliverable, not scratch files, unless requested.

## EcoreX Adaptation

- This is a user-invocable office skill and should appear under the document category in `@skill`.
- Preserve compatibility with existing prompts, shortcuts, and automations that mention `office-presentations`.
- Prefer official Codex workspace dependencies for authoring and render/QA when the host exposes them; do not silently use unrelated global packages for final deck creation.
- The `office-pdf` capability pack remains a fallback for legacy parsing/preview, but high-quality new deck creation should follow the official `Presentations` artifact-tool workflow.
- If required authoring or rendering dependencies are unavailable, report the missing verification layer clearly instead of implying the deck passed visual QA.
