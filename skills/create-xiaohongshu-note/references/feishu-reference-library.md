# Feishu Learning/Reference Library

Use this reference when the user has no Xiaohongshu reference note links and wants the agent to start from zero.

## Default Library

URL:

```text
https://my.feishu.cn/base/NiHSbUXiFaeIv3sE0D9cxQEhnUh?table=tblmATmmyspgmTAO&view=vewTMVcn8t
```

Parsed identifiers:

- `base_token`: `NiHSbUXiFaeIv3sE0D9cxQEhnUh`
- `table_id`: `tblmATmmyspgmTAO`
- `view_id`: `vewTMVcn8t`

## Access Rules

- In EcoreX, use the `feishu_cli` tool first for Feishu Docs, Wiki, Drive, Sheets, and Base materials. If that tool is unavailable, use `lark-cli` directly.
- Do not choose CDP/browser automation as the primary path for this library.
- If `lark-cli` is unavailable, unconfigured, unbound, or unauthorized, run the finite setup/auth flow below once, then stop and wait for the user when authorization or setup is required.
- If CLI permissions fail after auth, explain the blocker and ask the user to grant access, share the Base, or provide an export. Browser automation is a last-resort fallback only after the CLI path is unavailable or explicitly approved.
- Use `--as user` by default for Base resources.
- If authentication is missing, follow the `lark-shared` split-flow auth pattern instead of blocking the turn.

## Setup And Auth Loop

Before falling back to any other reading method, make the CLI path usable.

1. In EcoreX, call `feishu_cli` with `{"action":"ensure"}`. It can discover or install `@larksuite/cli` when Node/npm is available. If `feishu_cli` is unavailable, check whether `lark-cli` exists. If it is not on PATH, look for a known CLI root such as `C:\cli-main` or ask the user to install/enable Feishu CLI.
2. Check auth/config status:

```powershell
feishu_cli({"action":"status"})
# fallback only:
# lark-cli auth status
```

3. If config is missing, follow `lark-shared` and start configuration:

```powershell
lark-cli config init --new
```

When the command returns a verification URL, treat it as opaque, generate a QR code with `lark-cli auth qrcode`, show the QR/link to the user, and wait for completion.

4. If user auth for Base is missing, start split-flow auth:

```powershell
feishu_cli({"action":"auth_login", "domain":"base"})
# fallback only:
# lark-cli auth login --domain base --no-wait --json
```

Return the verification URL/QR to the user and pause. After the user says authorization is complete, finish the flow:

```powershell
feishu_cli({"action":"run", "args":["auth", "login", "--device-code", "<device_code>"]})
# fallback only:
# lark-cli auth login --device-code <device_code>
```

5. Verify usability by reading the library field list and at least one page of records. Only continue to topic options after this verification succeeds:

```powershell
feishu_cli({"action":"run","args":["base","+field-list","--as","user","--base-token","NiHSbUXiFaeIv3sE0D9cxQEhnUh","--table-id","tblmATmmyspgmTAO"],"timeout":30})
feishu_cli({"action":"run","args":["base","+record-list","--as","user","--base-token","NiHSbUXiFaeIv3sE0D9cxQEhnUh","--table-id","tblmATmmyspgmTAO","--view-id","vewTMVcn8t","--limit","10","--format","json"],"timeout":45})
```

If verification fails, continue the auth/permission loop with the user instead of silently switching to browser automation.

## Read Path

Run read/list commands serially. Base list commands should not be parallelized.

Check fields:

```powershell
feishu_cli({"action":"run","args":["base","+field-list","--as","user","--base-token","NiHSbUXiFaeIv3sE0D9cxQEhnUh","--table-id","tblmATmmyspgmTAO"],"timeout":30})
# fallback only:
# lark-cli base +field-list --as user --base-token NiHSbUXiFaeIv3sE0D9cxQEhnUh --table-id tblmATmmyspgmTAO
```

Do not pass `--format json` to `base +field-list`; current lark-cli versions reject that flag.

Read records from the specified view. Include note time/order fields and `封面`/`内页` fields when cover or carousel analysis is needed. Start with a small page and project only useful fields after the schema is known:

```powershell
feishu_cli({"action":"run","args":["base","+record-list","--as","user","--base-token","NiHSbUXiFaeIv3sE0D9cxQEhnUh","--table-id","tblmATmmyspgmTAO","--view-id","vewTMVcn8t","--limit","10","--format","json"],"timeout":45})
# fallback only:
# lark-cli base +record-list --as user --base-token NiHSbUXiFaeIv3sE0D9cxQEhnUh --table-id tblmATmmyspgmTAO --view-id vewTMVcn8t --limit 10 --format json
```

Do not keep paging only because `has_more` or an equivalent pagination signal appears. Continue serially with `--offset <n>` only when fewer than 5 high-relevance records were found, an essential category/scene is missing, or the page is obviously off-topic. Stop once 5-12 useful records are collected.

Reference-library reading is strictly read-only. Do not launch scripts that append records, sync a review Base, or upload attachments while this section is being executed. Batch writes/uploads are delivery steps and require explicit user confirmation.

After seeing field names, use repeated `--field-id <field name or id>` to project only useful fields for the current brief. Always include the best available time/order field when present, such as `日期`, `发布时间`, `发布日`, `笔记时间`, `时间`, `created_time`, `update_time`, `产出时间`, `ID`, or any explicit sequence/order column.

## Convergence Rules

The Feishu reference read must converge before topic options:

- Never dump full raw pages into the model when a compact selected list is enough.
- Use `scripts/select_feishu_references.py --input <record_json> --brief <brief_or_keywords.txt> --output <selected_refs.json> --limit 12 --page-count <pages_read> --max-pages 3` after each page or after combining pages.
- Treat `has_more=true` as a possible pagination hint, not a requirement to continue.
- Stop immediately when the selector finds 5-12 records that match the current industry/audience/scene and include usable learning or cover/copy mechanics.
- Fetch one more page only when the selected list has fewer than 5 usable records or misses a required category/scene from the brief.
- Hard stop after three pages unless the user explicitly asks for exhaustive research. Report the gap instead of looping.
- Keep the final research summary compact: selected records, why they match, learning fields/mechanics, recency field, and any gaps.

For selected reference records, download cover/inner-page attachments before cover design:

```powershell
feishu_cli({"action":"run","args":["base","+record-download-attachment","--as","user","--base-token","NiHSbUXiFaeIv3sE0D9cxQEhnUh","--table-id","tblmATmmyspgmTAO","--record-id","<record_id>","--output","<dir>"],"timeout":45,"expected_paths":["<dir>"]})
# fallback only:
# lark-cli base +record-download-attachment --as user --base-token NiHSbUXiFaeIv3sE0D9cxQEhnUh --table-id tblmATmmyspgmTAO --record-id <record_id> --output <dir>
```

Then inspect/OCR the downloaded cover images as described in `cover-ocr-visual-analysis.md`.
If the command exits 0 but `<dir>` is empty, treat the attachment download as unavailable and proceed with text learning fields; do not loop over more download attempts unless the user explicitly asks.

## How To Select References And Learning Logic

Match library records to the project by:

- Industry/category.
- Audience pain or desire.
- Product/service scene.
- Note type, such as experience, checklist, comparison, tutorial, story, Q&A, or pitfall avoidance.
- Note time/order: among relevant matches, prioritize more recent notes or later sequence values because they better reflect current platform style and market signal.
- Cover mechanic, such as contrast, number/list, strong phrase, before/after, scene, product close-up, or persona.
- Body mechanic, such as first-person experience, problem-solution, numbered steps, proof points, decision criteria, or comment prompt.
- Learning/decomposition fields, such as `learning`, `学习逻辑`, `拆解`, `公式`, `结构`, or similar schema fields.
- Cover and inner-page attachments that can be OCR-analyzed for layout, fonts, colors, and visual devices.

Do not copy library copy. Do not apply static built-in formulas. Extract the learning logic present in the returned records and adapt it to the confirmed client/project facts.

## Recency Ranking And Downgrade

Use recency as a ranking factor after basic relevance is established:

1. Identify available time/order fields from `+field-list`; prefer real note dates over record created/update time. If only an `ID` or sequence field exists and it clearly increases over time, use it as a weak recency signal.
2. Normalize dates when possible and sort relevant candidates newest first.
3. Select the newest high-quality records that still match the brief's industry, audience, note type, and learning logic.
4. If the newest records are off-category, missing useful `learning`, lacking usable cover/inner-page assets, or too few for 5-12 references, downgrade to older records that fit better.
5. In the research summary, state which time/order field was used and whether any older records were included as a downgrade fallback.

Do not choose a recent but irrelevant note over an older note that is clearly more useful for the current client. The intended behavior is relevance first, then recency among relevant high-quality references.

## Output Summary

Before topic options, summarize:

- CLI source used: base token, table, and view.
- 5-12 relevant reference records, using title/name/record id, note date/sequence, and any available industry or metric fields.
- Reference links and/or record ids that will be shown in the final package.
- The `learning` fields or inferred learning schema used.
- The recency field used, newest-first ordering, and any older-record downgrade rationale.
- Cover OCR/layout findings for selected references.
- 3-5 run-specific formulas or mechanics extracted from those records.
- Gaps, such as missing category coverage, partial pagination, or permission limits.
