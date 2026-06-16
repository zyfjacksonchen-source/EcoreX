# Visual Generation And Carousel

Use this after reference-cover analysis and before final package delivery.

## Cover Flow

1. Present cover direction and the exact generation prompt in Demo direction.
2. Ask the user to confirm or edit the direction and prompt.
3. After confirmation, generate the real final cover image. Do not stop at prompt-only output.
4. Show the generated cover preview to the user and ask for confirmation or modification.
5. If the user requests edits, update the prompt and regenerate. Keep the latest approved cover path in the note pack.

If image generation is unavailable, blocked, or fails repeatedly, state the blocker and do not call the package final or complete.

## Carousel Decision

Before production, explicitly ask or recommend whether the note should include carousel inner pages after the cover.

Use carousel pages when the topic benefits from:

- Steps or checklists.
- Before/after or comparison.
- Proof points, case evidence, or process explanation.
- A decision framework that is hard to fit into the cover/body.

Skip carousel pages when the note is intentionally single-image or when the user declines.

Record the decision:

```json
"carousel": {
  "requested": true,
  "page_count": 4,
  "reason": "needed for checklist/proof",
  "status": "produced"
}
```

## Inner-Page Design Language

When carousel inner pages are included, they must use the same design language as the cover:

- Same canvas size: `1080x1440`.
- Same typography family and hierarchy.
- Same color system and accent color.
- Same image treatment, texture, stickers, labels, frames, and spacing logic.
- Same visual language. CTA is allowed on carousel inner pages when it helps conversion, but the cover itself must not contain CTA text.

Inner pages may vary layout, but should feel like one Xiaohongshu note set.

## Inner-Page Output

For each inner page, include:

- Page purpose.
- Page text.
- Layout instructions.
- Image/design prompt.
- Produced image path and preview, after generation.

If inner pages were requested, final delivery is incomplete until all requested inner pages are produced or the user explicitly reduces/removes them.

## Final Visual Status

Final package visual status must distinguish:

- `cover_status: produced`
- `carousel_requested: true | false`
- `inner_pages_status: produced | not_requested | blocked`

Do not mark `final_cover_produced=true` unless an actual image file exists and has been shown to the user.
