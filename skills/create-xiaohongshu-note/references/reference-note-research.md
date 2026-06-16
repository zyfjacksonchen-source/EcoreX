# Reference Note Research

Run this before topic options. The priority order is: user-provided Xiaohongshu links decomposed through the Feishu `learning` logic format, then the Feishu learning/reference library, then live Xiaohongshu search as a supplement or fallback.

## User-Provided Reference Notes

If the user provides any of these, skip broad search:

- Xiaohongshu note URLs.
- xhslink short links.
- A client document containing approved reference notes.

In that case, read the provided notes with:

```powershell
opencli xiaohongshu note <note-url> -f json --window background --site-session persistent
```

Then load `learning-logic-decomposition.md` and `cover-ocr-visual-analysis.md`, read the Feishu library schema through `lark-cli`, and mirror the Feishu `learning` logic format when decomposing formulas from the links. OCR/analyze reference covers before cover design. Do not copy or closely paraphrase the reference notes. Do not use prewritten formula libraries.

## Starting From Zero

If the user has no reference note links, use the Feishu learning/reference library before broad Xiaohongshu site search:

```text
https://my.feishu.cn/base/NiHSbUXiFaeIv3sE0D9cxQEhnUh?table=tblmATmmyspgmTAO&view=vewTMVcn8t
```

Read `references/feishu-reference-library.md` and use `lark-cli`/Feishu CLI first. Do not choose CDP/browser automation as the primary reading path for Feishu documents or Base resources.

Use the Feishu library to match industry, audience, product category, note type, `learning` logic, cover/copy mechanics, and note recency to the current client/project. Compact every Base page with `scripts/select_feishu_references.py` and stop once 5-12 useful records are selected; `has_more=true` alone is not a reason to keep paging. Then propose 3-5 topic options. Among relevant matches, prefer the most recent high-quality notes. Downgrade to older notes only when recent records are insufficient, missing the needed category, or weaker on learning/cover/copy fit.

If Feishu CLI is not enabled, configured, bound, or authorized, run the setup/auth loop in `references/feishu-reference-library.md` and verify field/record reading before continuing. Do not silently switch to browser automation.

## Live Search Supplement

Use live Xiaohongshu search only when:

- Feishu access remains blocked after the CLI setup/auth loop and the user cannot authorize, share access, or export the data.
- The Feishu library does not cover the category or learning format well enough.
- The user explicitly asks for live platform search in addition to the library.

When live search is needed, the Content Strategist must generate 3-5 keywords before topic options. Good keywords combine:

- Product/category: `通勤包`, `冷萃咖啡`, `油皮粉底液`.
- Scene: `办公室`, `早八`, `出差`, `约会`, `通勤`.
- Audience pain: `电脑压肩`, `东西太乱`, `不酸涩`, `不脱妆`.
- Long-tail search intent: `怎么选`, `避坑`, `清单`, `测评`, `真实体验`.

Run:

```powershell
python scripts/research_xhs_references.py --input <brief_or_context.json> --output <refs.json>
```

or directly:

```powershell
opencli xiaohongshu search "<keyword>" --limit 8 -f json --window background --site-session persistent
```

## How To Use Results

Extract only run-specific mechanics:

- Repeated reader pains.
- Title formulas visible in the current references.
- Cover formulas visible in the current references.
- Cover OCR/layout mechanics visible in the current references.
- Content structures that invite saving.
- Tag patterns and long-tail terms.
- Interaction cues from comments when useful.

Treat all references as market signal and structure inspiration only.

## Output Required

Before proposing topics, show a short research summary:

- Source used: user links, Feishu learning/reference library, live search, or fallback.
- Feishu fields/tables/views and inferred `learning` fields used when relevant.
- 5-12 useful reference notes with title, author, likes, URL, note date/sequence, or record identifier when available.
- Reference links or record IDs that must be repeated in the final package.
- Cover OCR/layout observations for selected references.
- 3-5 observed formulas decomposed for this run.
- Search/access gaps or uncertainty, especially if `lark-cli` auth, Feishu permissions, or `opencli` login was required.
