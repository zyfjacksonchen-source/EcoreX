# Customer Asset Handling

Use this whenever the customer provides fixed images, people, scenes, products, packaging, logos, or approved cover references.

## Priority

Customer-approved assets beat generated imagery. The cover workflow should be:

1. Inventory customer assets.
2. Confirm rights and edit limits.
3. Decide cover use: direct layout, crop, cleanup, color grade, background extension, or approved AI edit.
4. Create an annotated layout plan describing where each asset appears.
5. Produce final cover only after the user confirms asset use.

## Asset Inventory

Record:

- `path_or_url`
- `type`: product, person, scene, logo, cover_photo, reference_only, texture, other.
- `subject`
- `orientation`: portrait, landscape, square, unknown.
- `quality_notes`: sharp, dark, low-res, crowded, watermark, safe area issue.
- `usage`: main_visual, background, cutout, inner_page, reference_only.
- `authorization`: confirmed, user_supplied_assumed, unknown.
- `edit_limits`: crop, text_overlay, color_grade, background_cleanup, background_extend, no_face_change, no_product_change.

## Rules

- Never replace a fixed customer product/person/scene with a generated substitute unless the user explicitly approves.
- Do not alter faces, body shape, product packaging, logo, labels, or key scene details unless the user asks for that edit.
- Do not use private customer photos as generic model/style references for unrelated outputs.
- If the image has visible third-party logos, unauthorized people, watermarks, license doubts, or sensitive settings, flag it before final use.
- For product shots, preserve packaging, color, logo, texture, and proportions.
- For people, preserve identity and avoid beauty/body edits unless explicitly requested and appropriate.

## Decision Tree

- **Good portrait/cover image**: use direct layout with title overlay and safe-area checks.
- **Good product cutout**: use product as main visual; generate or design simple supporting background only if approved.
- **Good scene photo**: crop for 3:4 and add readable title block.
- **Landscape photo**: crop or extend background; do not distort the subject.
- **Low-res or cluttered photo**: describe the proposed crop/cleanup layout and ask for a better asset or approval for a cleanup/crop workaround.
- **Reference-only image**: extract layout/color/mood, not exact identity or protected design.

## How To Tell The User

Show:

- Asset inventory table.
- Recommended cover use.
- Edit limits/risks.
- Annotated layout plan labeled `客户指定素材`.
- Whether final cover will be `direct layout`, `edited customer image`, `generated background + customer asset`, or `fully generated` with approval.
