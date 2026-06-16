# Cover OCR And Visual Analysis

Use this before proposing or producing a cover when reference notes, Feishu records, or cover attachments are available.

## Rule

Do not create a local draft image or placeholder preview. During Demo direction, before the user confirms image generation, return only:

- Cover design direction.
- Detailed layout instructions.
- Cover text.
- Image/design prompt.
- `final_cover_status: not_yet_generated`.

Show an image preview only when the real final cover image exists.

After the user confirms the cover direction and prompt, generate the real cover image. A final complete note package requires a produced cover image unless generation is blocked; if blocked, say so and do not call the package final.

## Source Handling

For Feishu Base references:

- Read fields including `封面`, `内页`, `标题`, `learning`, `链接`, `核心选题`, `利益点`, and `正文` when available.
- In EcoreX, use `feishu_cli` for cover or inner-page attachments from selected reference records, with `expected_paths` pointing at the output directory. If direct `lark-cli base +record-download-attachment` is used as a fallback, manually verify the output directory is non-empty.
- If attachment download exits successfully but produces no files, treat that reference as text-only and proceed with the learning fields. Do not repeat empty downloads in a loop unless the user explicitly asks.
- Inspect downloaded images with available image/OCR tools. If OCR is unavailable, visually inspect and manually transcribe visible text.

For Xiaohongshu links:

- Use available note-reading tools to get cover image URLs, image list, title, body, and tags.
- OCR or manually inspect cover/first image and any inner pages used as references.

## Analysis Fields

For each reference cover, extract:

- `visible_text`: exact visible text from cover OCR, preserving line breaks when possible.
- `text_hierarchy`: primary headline, secondary line, proof badge, CTA, bottom labels.
- `layout`: top/middle/bottom placement, grid, margins, safe area, image/text ratio.
- `font_style`: thick sans, handwritten, serif, rounded, outline, shadow, sticker text, approximate weight.
- `color`: background, main text color, accent color, warning color, contrast.
- `main_visual`: real person, room, construction site, material close-up, before/after, floorplan, product, or screenshot.
- `visual_devices`: arrows, red frames, circles, stickers, checklist marks, contrast split, number badges.
- `feed_readability`: whether main text is readable at small size and why.
- `do_not_copy`: elements that would be too similar or risky to reuse.

## Derive A New Cover

Combine the OCR/layout analysis with the selected cover formula. The new cover should keep useful mechanics but change enough surface detail:

- Change headline wording.
- Change visual composition or crop.
- Change color/accent system.
- Change badge positions and shapes.
- Avoid copying exact text, same line breaks, same sticker phrases, same visual arrangement, or same before/after framing.

Target similarity to any single reference: below 50%.

## Output Contract

Include this block in final package:

```json
"cover_design": {
  "reference_cover_analysis": [],
  "cover_text": {
    "headline": "",
    "subline": "",
    "badge_or_proof": ""
  },
  "layout_instructions": "",
  "style_instructions": "",
  "image_prompt": "",
  "final_cover_status": "produced | blocked | not_yet_generated",
  "final_cover_path": ""
}
```

If final cover is not produced after generation is attempted, state this plainly, mark it `blocked`, and do not attach a draft image or placeholder as a preview.

## Cover CTA Rule

Reference-cover analysis should still record whether the reference uses CTA, but the new final cover must not include CTA text such as `私信`, `评论`, `领取`, `扫码`, `加微信`, `咨询`, or similar direct action prompts. Use a proof badge, pain-point label, number, or credibility cue instead. Put CTA on carousel inner pages, body copy, tags, or first comment when needed.
