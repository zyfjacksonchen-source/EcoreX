# Learning Logic Decomposition

Use this reference whenever the user provides Xiaohongshu note links or when Feishu library records contain a `learning` / `学习` / `拆解` / `公式` / `结构` field.

## Principle

Do not use prewritten Xiaohongshu formulas, local course methods, or fixed playbooks. Derive the working formula for this run from:

- The user's provided note links.
- The Feishu document/Base `learning` logic format.
- Confirmed client/project facts.

The decomposition is an analysis artifact, not copy to publish.

## Read The Learning Format

Before decomposing user links, read the Feishu library fields and records through `lark-cli` as described in `feishu-reference-library.md`.

Identify fields whose names imply learning logic, such as:

- `learning`, `Learning`, `学习`, `学习逻辑`.
- `拆解`, `拆解逻辑`, `复盘`.
- `公式`, `内容公式`, `标题公式`, `封面公式`.
- `结构`, `笔记结构`, `写作结构`.
- `亮点`, `钩子`, `封面`, `标题`, `正文`, `互动`, `标签`.

If the exact field names differ, infer the learning fields from the returned schema and state the inference in the research summary.

Also load `cover-ocr-visual-analysis.md` and analyze reference cover images or cover attachments before deriving the cover formula.

## Decompose User Links

For each user-provided reference note, extract only reusable structure:

- `topic_formula`: the subject, audience, problem/desire, and promise.
- `cover_formula`: main visual, cover text, contrast, proof cue, and feed-readability move.
- `cover_ocr_layout`: visible cover text, hierarchy, layout, font style, color, and visual devices.
- `title_formula`: syntax pattern, hook type, keyword placement, and curiosity/benefit device.
- `body_formula`: opening, proof/experience sequence, paragraph rhythm, and save/comment trigger.
- `carousel_formula`: optional page logic if the reference uses multiple images or is suitable for a carousel.
- `tag_formula`: category, scene, audience, pain, and long-tail search tags.
- `risk_notes`: claims, sensitive words, unsupported data, or tactics not suitable for the current client.

Use the Feishu `learning` format as the output structure. If it has a different schema, mirror that schema instead of forcing the fields above.

## Output Format

Before topic options or Demo direction, provide a concise decomposition table:

| Reference | What It Does | Formula Decomposed From It | Adaptation For This Client | Risk/Do Not Copy |
| --- | --- | --- | --- | --- |

Then synthesize 2-4 candidate formulas for this project. Name them descriptively, such as:

- `避坑清单型`
- `真实体验反差型`
- `人群场景解决型`
- `步骤教程收藏型`

Only use formula names that are supported by the current references. Do not invent evergreen formulas unrelated to the links or Feishu learning records.

Keep the source links or record IDs with the final package. This is required for the user to compare against references and avoid accidental copying.

## Production Use

The selected formula should guide the Demo direction, cover, optional carousel, title, body, tags, and audit. If the user changes direction, update the formula decomposition before production.
