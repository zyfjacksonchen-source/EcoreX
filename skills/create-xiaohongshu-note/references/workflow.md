# Workflow

## 1. PLAN Mode Intake

The Main Agent owns the first interaction. Start in PLAN mode when the host supports it. If it does not, emulate PLAN mode with a short, direct briefing conversation and do not produce final copy or images yet.

First ask:

- Whether the user has Xiaohongshu reference note links or share links.
- Whether project/client information is already in the project folder.

Before asking the user to repeat details, inspect any available project files, uploaded docs, client files, reference notes, and links. If project files exist, summarize the client/project facts and ask the user to confirm or correct them. If project files do not exist, ask only for materially missing inputs:

- Client name, brand/account identity, and project background.
- Product, service, offer, campaign, or topic area.
- Target audience and desired action.
- Reference notes, competitors, style preferences, and taboo examples.
- Must-include claims, proof points, scenes, keywords, or product details.
- Must-avoid wording, industries, images, or legal/compliance risks.
- Customer-provided cover/product/person/scene assets plus authorization and edit limits.
- Delivery destination if this is not a project conversation.

Keep the PLAN response concise: what is known, what is missing, what will happen next.

## 2. Reference Research And Topic Options

If the user provides Xiaohongshu reference links, read them and load `references/learning-logic-decomposition.md`. Read the Feishu learning/reference library schema through `lark-cli`, identify the `learning` logic format, and use that format to decompose formulas from the user's links on the spot. Do not copy, closely paraphrase, or use prewritten formula libraries.

If the user has no reference links and wants to start from zero, use the Feishu learning/reference library before proposing topics. Load `references/feishu-reference-library.md` and read the Base through EcoreX `feishu_cli` or direct `lark-cli` fallback. Do not make CDP/browser automation the primary reading path for Feishu documents or Base resources. Read only small read-only pages for reference selection; do not start append/sync/upload jobs during reference reading. Compact Base records with `scripts/select_feishu_references.py`, stop at 5-12 useful references, and do not keep paging only because `has_more=true`. When matching library notes, first filter for project relevance, then prefer records with the most recent note time/order. If recent relevant records are too few or weak, explicitly downgrade to older records and explain why.

If Feishu CLI is not enabled, configured, bound, or authorized, run the setup/auth loop in `references/feishu-reference-library.md` until field/record reading works or the user explicitly stops. If access still fails because of resource permissions, explain the blocker and ask for access, exported data, or explicit permission to use a fallback. Use `opencli` Xiaohongshu search only as a supplement or fallback after the Feishu path is unavailable or insufficient.

Then propose 3-5 topic options. Each option should include:

- Topic angle.
- Target reader.
- Why it can work for this client/project.
- Likely cover hook.
- Formula/reference signal used, such as user links decomposed through Feishu `learning` logic or matched Feishu records.
- Reference recency signal used, such as `日期`, publish time, created/update time, or explicit sequence field when available.

Keep the selected reference URLs or record links/ids. The final package must show the references used so the user can verify the work is not copied.

## 3. Demo Direction

After the user chooses or edits a topic, produce a Demo direction before final production:

- Selected topic and short strategy rationale.
- Selected formula decomposition and why it fits this client.
- Reference cover OCR/visual findings: visible text, layout, font style, colors, main visual, and visual devices from selected reference covers.
- Cover concept: main visual, cover text, composition, asset use, and how it stays different from references.
- Carousel decision: ask whether cover-following carousel inner pages are needed. If recommended, explain why. If included, provide page count, page purpose, key text, and design language matching the cover.
- Title direction: 3-5 title mechanisms or sample candidates, each no more than 20 characters.
- Body direction: opening hook, structure, proof/experience points, and call to comment/save.
- Tags direction: core category tags plus long-tail search tags.

Ask for confirmation or edits. Treat this as the last checkpoint before production. If the user changes the direction, revise the Demo direction and confirm again when the change is material.

## 4. Locked Brief

After the Demo direction is confirmed, produce a `LockedBrief` and show references plus the brief. The brief is the source of truth for all roles. Do not allow roles to invent claims beyond the locked brief.

Run `scripts/validate_locked_brief.py <brief.json>` when a JSON brief is created.

## 5. Production Execution

After confirmation, the Main Agent coordinates the roles.

If the runtime supports parallel subagents, run Designer Agent, Copy Master, and Audit Master concurrently with the same `LockedBrief`. If the runtime does not support parallel subagents, execute serially in this order:

1. Topic and brief lock.
2. Reference cover OCR/visual analysis, then cover and optional carousel inner-page plan.
3. Confirm cover direction, cover generation prompt, and carousel decision with the user.
4. Generate the real cover image. If carousel pages were requested, generate them using the same visual language as the cover.
5. Show generated image previews to the user for confirmation or modification.
6. Titles, body, tags, and first comment.
7. Audit and self-check.
8. Final merge.

Role work:

- Designer Agent writes reference-cover OCR analysis, cover hook text, layout instructions, image/design prompt, final cover image path after generation, and optional carousel inner-page prompts/assets based on the selected formula decomposition. Do not output a local cover draft image or placeholder preview.
- Copy Master writes 3-5 title candidates of <=20 characters, selected recommendation, full body with Xiaohongshu-native rhythm and natural emoji, tags, and first comment based on the selected formula decomposition.
- Audit Master checks plan drift, user requirements, ad/prohibited wording, unsupported claims, risky imagery, platform fit, whether final cover/inner pages were actually produced, and whether reference similarity is below 50%.
- Main Agent merges outputs, resolves conflicts, and decides what to ask the user next.

For slow final cover or inner-page generation, send short progress updates every 45-60 seconds when the host supports updates. Do not call the package complete until the required cover image exists and has been previewed. If no final cover is produced because generation is blocked, say so plainly, do not attach a placeholder preview, and stop before final delivery.

## 6. Final Package

Return the complete package:

- Confirmed topic and strategy rationale.
- Formula decomposition used for this run.
- References used, including links or record ids.
- Reference cover OCR/visual analysis summary.
- Produced final cover preview, path, design instructions, and prompt.
- Carousel decision. If requested, include produced inner-page previews, paths, prompts, and design notes. If declined, state `inner_pages_status=not_requested`.
- 3-5 title candidates and selected title.
- Full body copy.
- Tags.
- First comment.
- Audit Master self-check, including final cover production status, carousel requested/produced status, and reference similarity below 50% check.
- Local artifact paths and manifest when files were created.

## 7. Revision Loop

If the user requests changes, Main Agent translates the feedback into specific changes, updates the formula decomposition and brief if needed, and routes work to the relevant role. Do not let Designer, Copy Master, or Audit Master independently change the core strategy without Main Agent alignment.

Return a concise revised package and note what changed.

## 8. Approval, Delivery, And Project Memory

Local files may be created during image generation so the user can preview and revise. Only after the user approves the complete note package, perform external delivery:

- Render DOCX through WPS CLI when requested or useful.
- Create or update Feishu Bitable customer-review sheet when requested.
- Upload DOCX and images as attachments when delivery requires it.
- Return local paths and Feishu URL/status.

After final production and preview, ask whether to place the complete note package into a Feishu Bitable for customer review. If yes, follow `feishu-bitable-delivery.md`: same customer gets one reusable Base/link with a new table/sheet per output; different customers get separate Bases/links; internet-visible read permission is applied only after explicit confirmation.

After final approval, ask whether to save this note to project-level memory. If the user says yes, use the host's project memory mechanism when available. If no memory tool exists, ask before saving a concise manifest or note file in the project workspace. If the user says no, do not save it.
