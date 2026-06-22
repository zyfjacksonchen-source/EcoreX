---
name: create-xiaohongshu-note
description: Use when the user wants to 创作小红书笔记, 写小红书笔记, 帮我写小红书笔记, 生成小红书笔记, or build an end-to-end Xiaohongshu note workflow; creates a complete XHS note package from PLAN-mode topic selection, project/client briefing, reference-link and cover OCR analysis, demo direction, confirmed cover and optional carousel image generation, 20-character-max titles, emoji/native body copy, tags, compliance and similarity review, Feishu customer-review Bitable delivery, revision orchestration, and optional project-level memory saving.
---

# Create Xiaohongshu Note

Use this skill to create a complete Xiaohongshu note package, not just copy. The output should move from topic strategy to visual packaging, title/body/tags, risk review, revision handling, and optional project memory.

## Non-Negotiable Workflow

1. **Start in PLAN mode.** If the host has Plan Mode, enter it. If not, emulate it: keep the first response short, direct, and oriented around topic selection, client/project context, and execution plan.
2. **Ask for reference notes first.** Ask whether the user has Xiaohongshu reference note links or share links. If yes, read them, then use the Feishu document/Base `learning` logic format to decompose the formulas on the spot. Do not use prewritten formula libraries.
3. **Read project context before asking for it.** Ask whether project/client information is already in the project folder. If yes, inspect the project folder, summarize client/project facts, and ask the user to confirm. If no, ask for the client's name, business/project context, target audience, offer, constraints, and required tone.
4. **Use the Feishu learning/reference library when starting from zero.** If the user has no reference note links, use the Feishu library at `https://my.feishu.cn/base/NiHSbUXiFaeIv3sE0D9cxQEhnUh?table=tblmATmmyspgmTAO&view=vewTMVcn8t` before proposing topics. In EcoreX, call the `feishu_cli` tool first for Feishu/Base materials. Do not call raw `lark-cli` through `bash` unless `feishu_cli` has reported an unrecoverable setup issue and the host explicitly allows that fallback. Do not choose CDP/browser automation as the primary path. Use a finite Feishu setup flow: call `feishu_cli` `status` once, `ensure` once if the CLI is missing, and `auth_login` once if authorization is missing. If `auth_login` returns `authRequired`, stop Feishu reads, show the user the authorization link/QR instruction, and wait for the user to finish authorization before continuing. When the library contains note time/order fields, prioritize recent high-quality notes among relevant matches; only downgrade to older notes when recent records are insufficient or less relevant. Stop Feishu pagination once 5-12 relevant records are selected; do not continue just because `has_more=true`.
5. **Analyze reference covers before cover design.** For user links or Feishu reference records, OCR and visually analyze the reference cover(s): text, layout, hierarchy, fonts, colors, composition, main visual, stickers/arrows/frames, and CTA. Then combine that analysis with the selected cover formula. Do not copy a reference cover. Final cover images must not contain CTA text such as 私信、评论、领取、加微信、扫码; CTA can appear in carousel inner pages, body copy, tags, or first comment.
6. **Ask whether carousel inner pages are needed.** Before production, explicitly ask or recommend whether the note should include carousel pages after the cover. If inner pages are included, their prompts, layout, typography, colors, and visual devices must use the same design language as the cover.
7. **Show a Demo direction before production.** Present the overall thinking for topic, cover, optional carousel inner pages, title, and body structure. Include cover direction, cover generation prompt, and inner-page design direction/prompts when applicable. Ask for confirmation or edits before producing final assets.
8. **Generate real images after visual direction confirmation.** After the user confirms the cover direction and generation prompt, submit image generation for the cover. If carousel inner pages are requested, generate those too using the same visual language. Show the generated image previews to the user for confirmation or modification. A final complete note package is not complete without a produced cover image unless image generation is genuinely unavailable or blocked, in which case state the blocker and do not call it final.
8a. **Do not stall after explicit production direction.** If the user has already said to proceed with the default direction, specified the asset count, or explicitly declined carousel pages, treat that as production confirmation for those assets. Do not ask for another "confirm generation" turn unless there is a material risk, missing required input, or policy blocker. For "one cover only / no carousel" requests, continue directly to the single-cover production path and show visible progress.
9. **Run four roles.**
   - Main Agent / Content Strategist: owns plan, task split, constraints, handoffs, final assembly, and revision scheduling.
   - Designer Agent: owns reference-cover OCR analysis, cover design instructions, cover prompts, final cover status, and optional carousel inner-page design.
   - Copy Master: owns title candidates, body copy, tags, and first comment.
   - Audit Master: checks plan drift, ad/compliance risk, prohibited wording, claim support, final cover/inner-page production status, and similarity to references.
10. **Parallelize when possible.** If the runtime can call subagents concurrently, run Designer, Copy Master, and Audit Master in parallel under Main Agent orchestration. If not, run the same roles serially from topic to cover/inner pages to copy/tags to self-check.
11. **Do not output fake cover previews.** Before image generation, provide cover design instructions and prompts only. After generation, show only real final image files. Never attach a draft image or placeholder as a preview.
12. **Keep revisions centralized.** If the user gives modification feedback, Main Agent interprets the request, updates the brief if needed, and routes changes to the relevant role before returning a coherent revised package.
13. **Ask about Feishu customer-review delivery after final production.** After the complete note package is produced and previewed, ask whether to place it into a Feishu Bitable for customer review. Default permission for the review link is internet-visible read access after explicit user confirmation. Same customer reuses one Base/link with separate tables/sheets per output; different customers get separate Bases/links. The newest output sheet/table must be placed first when possible and named `客户名-YYYYMMDDHHmm-待审核`.
14. **Ask about project memory after final approval.** After final deliverables are ready, ask whether to save this note to project-level memory. If yes, use the host's project memory mechanism when available; if not available, save a concise project note/manifest in the project workspace only after the user agrees. If no, do not save it.

## What To Load

- For step-by-step execution, read `references/workflow.md`.
- For Feishu Base reference-library reading, read `references/feishu-reference-library.md`.
- For live formula decomposition using the Feishu `learning` logic, read `references/learning-logic-decomposition.md`.
- For cover OCR and visual layout analysis, read `references/cover-ocr-visual-analysis.md`.
- For real cover/carousel image generation and preview flow, read `references/visual-generation-and-carousel.md`.
- For Xiaohongshu-native copy rhythm, emoji handling, and title length, read `references/xhs-native-copy-rules.md`.
- For Xiaohongshu reference-note research and fallback live search, read `references/reference-note-research.md`.
- For role responsibilities and handoff contracts, read `references/role-contracts.md`.
- For required data shapes, read `references/locked-brief-schema.md`.
- For customer-provided cover/product/person/scene assets, read `references/customer-assets.md`.
- For risk checks, read `references/compliance-and-risk-gate.md`.
- For final output, read `references/wps-docx-delivery.md` and `references/feishu-bitable-delivery.md`; the Feishu reference includes the required customer-review Gallery view flow, `search:docs:read` authorization recovery, and known lark-cli pitfalls.
- For Codex/Claude portability, read `references/platform-compatibility.md`.

## Script Helpers

Use these scripts when deterministic checks or delivery artifacts are needed:

- `scripts/validate_locked_brief.py <brief.json>`
- `scripts/validate_note_pack.py <note_pack.json>`
- `scripts/select_feishu_references.py --input <record_json> --brief <brief_or_keywords.txt> --output <selected_refs.json> --limit 12` after Feishu Base reads to compact records and decide whether pagination should stop.
- `scripts/research_xhs_references.py --input <brief_or_context.json> --output <refs.json>` when Feishu references are unavailable or live Xiaohongshu search is explicitly needed.
- `scripts/generate_cover_image.py --prompt-file <prompt.txt> --output <cover_or_inner_page.png> --model gpt-image-2-pro --async`
- `scripts/render_wps_docx.py --brief <brief.json> --note-pack <note_pack.json> --output <note.docx>`
- `scripts/sync_lark_base_review.py --brief <brief.json> --note-pack <note_pack.json> --manifest <manifest.json> --customer <客户名> --public-read` after the user explicitly confirms Feishu customer-review delivery and internet-visible read permission.

## Runtime Defaults

- Image generation model: `gpt-image-2-pro` only.
- Image generation is final-image only: do not create local draft images, placeholder previews, Python/PIL/matplotlib mockups, or automatic fallback images. If `gpt-image-2-pro` is unavailable or fails, mark the visual deliverable `blocked` and do not call the note package final.
- Cover size: `1080x1440`.
- Carousel inner-page size: `1080x1440`, same design language as the cover.
- Title limit: every title candidate and selected title must be <=20 characters.
- Default Feishu learning/reference library: `base_token=NiHSbUXiFaeIv3sE0D9cxQEhnUh`, `table_id=tblmATmmyspgmTAO`, `view_id=vewTMVcn8t`.
- Feishu/Base reading: in EcoreX use `feishu_cli` action `status`, `ensure`, or `auth_login` as a bounded setup flow, then `feishu_cli` action `run` with `["base", "+field-list", "--as", "user", ...]` and `["base", "+record-list", "--as", "user", "--view-id", "vewTMVcn8t", "--limit", "10", "--format", "json", ...]`. Do not route `lark-cli` through raw `bash` while `feishu_cli` is available; the EcoreX host will reroute or block that path. Do not pass `--format json` to `base +field-list`; current lark-cli versions reject it.
- Feishu CLI setup/auth: follow `lark-shared` rules, but keep it finite. Run status once, ensure once if needed, and auth_login once if needed. If authorization is required, show QR/link to the user and stop until the user confirms authorization is complete; do not keep probing Feishu commands while waiting.
- Feishu reference reading is read-only. Do not start append/sync/upload scripts while "reading references". Any batch write or attachment upload to Feishu requires a separate explicit user confirmation and visible progress.
- Xiaohongshu site research fallback: `opencli xiaohongshu search <keyword> --limit 8 -f json --window background --site-session persistent`.
- WPS CLI root: `C:\EcoreX Artifact Desk\cli-anything-wps-master`.
- Feishu/Lark CLI root: `C:\EcoreX Artifact Desk\cli-main`; use global `lark-cli` when available.
- Generic output folder: ask the user. Project output folder: `deliverables/xhs-notes/<date>_<brand>_<topic>`.

## Final Package Contract

Every final package must include:

- Confirmed topic and short strategy rationale.
- Formula decomposition derived from the user's links and/or Feishu `learning` records for this run.
- Reference links used in this run, shown with the final package to make copying risk visible.
- 3-5 title candidates, each <=20 characters.
- One selected title recommendation.
- Full note body with Xiaohongshu-native rhythm and natural emoji inserted where appropriate.
- 5-10 tags.
- One first comment designed to invite replies.
- Produced final cover image preview and path, plus the confirmed prompt and design notes.
- Optional produced carousel inner pages when the user requested them or when the topic benefits from explanation, steps, comparison, list, or proof; include inner-page prompts/design notes and previews. If the user declines inner pages, state that none were requested.
- Customer-provided image/person/scene/product assets are used as the primary visual source when available; do not replace them with generated imagery unless the user explicitly approves.
- Audit Master self-check covering plan fit, ad/prohibited wording, claim support, user requirements, whether final cover/inner pages were produced, and whether reference similarity is below 50%.
- Local manifest describing all generated files and Feishu status.
- Post-production question: ask whether to sync the complete package into a Feishu Bitable customer-review link with internet-visible read permission.
