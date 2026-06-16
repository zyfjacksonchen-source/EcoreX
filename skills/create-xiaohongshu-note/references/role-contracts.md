# Role Contracts

## Main Agent / Content Strategist

Owns direction, evidence, topic selection, formula decomposition, task split, role handoffs, acceptance checks, revision routing, final summary, and project-memory decision.

Outputs:

- PLAN-mode intake summary.
- Topic options.
- Formula decomposition derived from user links and/or Feishu `learning` records.
- Demo direction.
- `LockedBrief`.
- Acceptance checklist.
- Final merged package.
- Revision notes.
- Project-memory save prompt after final approval.

Rules:

- Do not allow other roles to invent claims beyond the locked brief or use formulas not derived in this run.
- Keep the user-facing PLAN and Demo steps short and clear.
- If subagents are available, provide each role the same locked brief and merge their outputs.
- If subagents are not available, execute the same role sequence serially and keep the handoff boundaries explicit.
- If the user requests modifications, interpret and route the change rather than letting each role independently reshape the strategy.

## Designer Agent

Owns reference-cover OCR/visual analysis, feed-readability, cover design instructions, cover generation, carousel inner-page generation when requested, final visual status, and visual packaging.

Outputs:

- Cover hook text.
- 3:4 cover prompt for `gpt-image-2-pro`.
- Reference cover OCR and layout analysis.
- Detailed cover layout/style instructions.
- Final cover image preview/path after generation.
- Final cover status: `produced`, `blocked`, or `not_yet_generated`.
- Final cover path only when an actual final cover exists.
- Carousel decision and optional carousel inner-page structure, page copy blocks, layout notes, prompts, produced previews, and paths.
- Final image status and asset paths.

Rules:

- Main cover text should be short and readable in feed.
- The final cover must not contain CTA text such as 私信、评论、领取、扫码、加微信、咨询. Use proof labels or pain-point badges on the cover instead; reserve CTA for carousel inner pages, body copy, tags, or first comment.
- Use a strong visual anchor.
- Analyze reference cover elements before designing: OCR text, layout, font style, color, visual anchor, badges, arrows, frames, and CTA.
- Ask or recommend whether carousel inner pages are needed before production.
- Treat carousel inner pages as optional unless requested or strongly useful for the selected topic; if included, keep typography, colors, visual devices, and image treatment consistent with the cover. CTA can appear on carousel inner pages even though it must not appear on the cover.
- Follow the selected formula decomposition; do not introduce generic cover formulas from memory.
- If customer assets are provided, produce an asset usage plan first.
- Do not replace fixed customer people, products, scenes, or logos with generated substitutes unless explicitly approved.
- Do not output a local draft image or placeholder as a cover preview. Before confirmation, provide instructions and prompt only. After user confirms the visual direction and prompt, generate the real cover image and show the preview.
- A final complete note package requires a produced cover image. If generation is blocked, tell Main Agent and do not mark the package complete.
- Avoid platform-incompatible watermarks, fake logos, unauthorized portraits, or misleading before/after visuals.
- Keep final cover similarity to any single reference below 50%.

## Copy Master

Owns titles, body, tags, and first comment.

Outputs:

- 3-5 title candidates, each <=20 characters.
- Recommended selected title.
- Full body copy.
- 5-10 tags.
- First comment.

Rules:

- Match the brief and audience.
- Follow the selected formula decomposition and adapt it to confirmed client/project facts.
- Use `xhs-native-copy-rules.md`: titles <=20 characters, body has Xiaohongshu-native rhythm, and natural emoji are inserted where appropriate.
- Prefer concrete scenes, experience, contrast, checklist, problem-solution, or personal insight only when the decomposition supports that direction.
- Avoid unsupported absolute claims, medical/financial promises, and fake data.

## Audit Master

Owns plan fit, compliance, prohibited wording, evidence, and user-requirement checks.

Outputs:

- Plan-drift check: whether topic, cover, title, body, tags, and first comment match the Demo direction and `LockedBrief`.
- Formula-origin check: whether the selected formula is visibly derived from the user's links and/or Feishu `learning` records for this run.
- Title/native-copy check: whether every title is <=20 characters and body copy has natural Xiaohongshu rhythm/emoji.
- User-requirement check: what requirements are satisfied and what is missing.
- Ad/compliance risk check: unsupported claims, prohibited or high-risk wording, sensitive industries, medical/financial/guarantee claims, misleading before/after, unauthorized portraits/logos, and fake data.
- Production-status check: whether final cover and requested inner pages were actually produced; if required images are missing, state `blocked` or `not_yet_generated` and do not call the package final.
- Carousel-status check: whether carousel pages were requested, declined, produced, or blocked.
- Reference-similarity check: whether title/body/cover/inner-page mechanics are less than 50% similar to any one reference; if final cover or inner pages do not exist, mark visual similarity as not verifiable and explain.
- Revision recommendations, grouped by must-fix and nice-to-have.

Rules:

- Do not rewrite the whole note unless asked; identify precise fixes for Main Agent to route.
- If risk depends on a factual claim, mark the claim as requiring user proof rather than inventing proof.
- Run after production in serial mode, or as a parallel review role when subagents are available.
