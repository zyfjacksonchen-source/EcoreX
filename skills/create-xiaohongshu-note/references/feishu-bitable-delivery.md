# Feishu Bitable Customer Review Delivery

Use this only after the complete Xiaohongshu note package has been produced, including the final cover image and any requested carousel inner-page images.

Do not sync to Feishu automatically. Ask the user:

`是否需要把完整笔记放到飞书多维表格里给客户审核？默认设置为互联网获得链接可阅读。`

Only proceed after the user confirms.

## Review-Link Rules

- Same customer: reuse one Feishu Base/link and create a new table/sheet for each output.
- Different customer: create a separate Base/link.
- Base name: `客户名-小红书笔记审核`.
- New table/sheet name: `客户名-YYYYMMDDHHmm-待审核`.
- Newest output should be first when the API/CLI supports table ordering. If table ordering is unavailable, prefix the latest table name clearly and report the limitation.
- Default public permission after explicit confirmation: internet-visible read access, not edit access.

## Layout Rules

Mimic a real Xiaohongshu note review layout in Base as much as Bitable allows:

1. Images at the top: attachment field first, containing cover image first, then carousel images in order.
2. Title/copy below: selected title and title candidates.
3. Body + TAG below: body, tags, and first comment.
4. Review metadata: status, output time, audit notes, local artifact paths.
5. Default customer preview view is a Gallery view named `画册`, with `图片` as the cover field and visible fields limited to `图片`, `推荐标题`, `标题候选`, and `正文`.

Do not expose internal production prompts or reference-note links in the customer-review Base. Keep prompt text and reference links in the local note pack/manifest only unless the user explicitly asks to publish them.

Preferred field order:

- `图片`
- `推荐标题`
- `标题候选`
- `正文`
- `TAG`
- `首评`
- `审核自检`
- `状态`
- `产出时间`
- `审核意见`
- `本地路径`

Use one record for one complete note package. Upload cover first, then carousel inner pages in page order to the `图片` attachment field.

## CLI Flow

Preflight:

```powershell
lark-cli auth status
```

If unauthenticated, follow `lark-shared` and authorize Base/Drive permissions before continuing.

### Search Permission Recovery

Finding an existing customer Base requires `search:docs:read`. If `drive +search` returns `missing_scope` for `search:docs:read`, do not switch to browser automation. Use the CLI device-flow:

```powershell
lark-cli auth login --scope "search:docs:read" --no-wait --json
```

Then:

1. Read the returned `verification_url` and `device_code`.
2. Generate a QR code with the URL as a positional argument, not `--url`:

```powershell
lark-cli auth qrcode "<verification_url>" --output "search_docs_read_qr.png"
```

3. Show the raw verification URL first, then show the QR image. Treat the URL as an opaque string: do not encode, decode, shorten, or add punctuation.
4. Stop and wait for the user to confirm authorization. Do not show the URL and immediately block on `--device-code` in the same message turn.
5. After the user says authorization is done, complete the flow:

```powershell
lark-cli auth login --device-code "<device_code>"
```

6. Verify success with both:

```powershell
lark-cli auth status
lark-cli drive +search --as user --query "<客户名>-小红书笔记审核" --doc-types bitable --format json
```

If the user says the authorization page cannot load, generate a fresh no-wait auth link and QR code. Device codes expire and each new login attempt invalidates the previous code. If authorization still cannot be completed, create a new customer Base for this run, report that existing Base reuse could not be verified, and continue with delivery.

Find an existing customer Base:

```powershell
lark-cli drive +search --as user --query "<客户名>-小红书笔记审核" --doc-types bitable
```

If found, inspect the URL/token and use its Base token. If not found, create a new Base:

```powershell
lark-cli base +base-create --as user --name "<客户名>-小红书笔记审核" --time-zone Asia/Shanghai
```

Create a new table/sheet for this output. Prefer the bundled script because it builds the field/view JSON safely, creates the `画册` Gallery view, sets the image cover field, and removes the default empty table created by new Bases when safe:

```powershell
python scripts/sync_lark_base_review.py --brief <brief.json> --note-pack <note_pack.json> --manifest <manifest.json> --customer "<客户名>" --base-token <base_token> --public-read
```

If running manually, pass JSON strings directly to `--fields` and `--view`:

```powershell
lark-cli base +table-create --as user --base-token <base_token> --name "<客户名>-YYYYMMDDHHmm-待审核" --fields "<fields_json>" --view "<view_json>"
```

Create the note record first, then upload attachments:

```powershell
lark-cli base +record-upsert --as user --base-token <base_token> --table-id <table_id> --json @record.json
lark-cli base +record-upload-attachment --as user --base-token <base_token> --table-id <table_id> --record-id <record_id> --field-id "图片" --file <cover.png> --file <inner01.png>
```

Set internet-visible read access after explicit user confirmation:

```powershell
lark-cli drive permission.public patch --as user --params "{\"token\":\"<base_token>\",\"type\":\"bitable\"}" --data "{\"external_access\":true,\"link_share_entity\":\"anyone_readable\",\"share_entity\":\"anyone\",\"security_entity\":\"anyone_can_view\",\"comment_entity\":\"anyone_can_view\"}" --yes
```

Because this is a high-risk permission write, do not add `--yes` unless the user has confirmed Feishu review delivery and internet-visible permission.

## Known CLI Pitfalls

- PowerShell often strips JSON quotes when passing `--json`, `--params`, or `--data`. Prefer `@file.json` for manual commands, or pass JSON as subprocess list arguments from Python.
- `base +table-create` field schema must not use unsupported nested `property` keys. For customer review, use plain `text` fields for status instead of select fields unless the CLI schema confirms select options are accepted.
- `base +record-upsert` may return `record_id_list` rather than a top-level `record_id`; scripts must read both forms before uploading attachments.
- `base +record-upload-attachment` requires each `--file` to be a relative path inside the command working directory. Change `cwd` to the attachment directory and pass filenames/relative paths; absolute paths are rejected as unsafe.
- New Bases include a default empty table named `数据表`. After the real output table is created and verified, delete that default table by table ID when it is not the output table.
- Field deletion can temporarily return `OpenAPIDeleteField limited`. Retry serially after a short sleep; do not fan out repeated delete calls in parallel.
- Public permission calls should use `permission.public patch`, then verify with `permission.public get`. If tenant policy blocks internet-visible sharing, return the Base link and the exact permission error instead of claiming public access.
- Gallery configuration is two-step after table creation: call `+view-set-card` with `{"cover_field":"图片"}` and `+view-set-visible-fields` with `{"visible_fields":["图片","推荐标题","标题候选","正文"]}`.
- The review Base should not include `参考链接`, `封面提示词`, or `内页提示词`. Keep those in local artifacts only unless the user explicitly asks to expose them.

## Required Return

Return:

- Feishu Base URL/link.
- Table/sheet name and status `待审核`.
- Whether internet-visible read permission was applied.
- Any permission or table-order limitation.
- Local manifest path.

On failure, preserve local files and report the exact failing command and stderr. Do not pretend the customer-review link exists if creation or permission setup failed.
