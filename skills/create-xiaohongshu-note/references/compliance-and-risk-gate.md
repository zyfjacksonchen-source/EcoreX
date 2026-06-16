# Compliance And Risk Gate

This is a creative risk checklist, not legal advice. Apply stricter review for finance, insurance, medical, health, cosmetics, education, mother/baby, and other regulated categories.

## Avoid

- Political/state symbol misuse, national leaders, territory disputes, insults, privacy leaks, minors risk.
- Gambling, violence, sexual content, superstition, discriminatory language.
- False or misleading content, non-existing products/services, unsupported results, fake research, fake data, or creative/landing-page mismatch.
- Absolute or top-ranking words such as `最`, `第一`, `唯一`, `国家级`, `顶级`, `永久`, `万能`, `无副作用`, `根治` unless clearly limited as subjective opinion and still low-risk.
- Medicalized cosmetics or health claims: `药妆`, `EGF`, `干细胞`, `医美护肤`, `治疗`, `治愈`, `疗效`, `X天见效`, `无副作用`.
- Unauthorized portraits, celebrity names, trademarks, brand partnerships, platform watermarks, or external diversion.

## Evidence Rules

- Data, statistics, rankings, and quotes must have source, scope, and date when material.
- If a result is personal experience, mark it as such.
- If evidence is not provided, soften the claim.

## Image Rules

Avoid unpleasant or risky visuals:

- Bloody, dirty, frightening, or gross close-ups.
- Severe acne/scars/body deformity as shock tactics.
- Overly revealing/sexualized bodies.
- Before/after medical or financial proof visuals unless cleared.

## Audit Result

Every final `audit_check` should include:

- `summary`
- `risks`
- `softened_claims`
- `needs_user_evidence`
- `final_cover_produced`
- `inner_pages_produced`
- `carousel_requested`
- `reference_similarity_under_50_percent`
- `title_length_check`
- `native_copy_check`

For a final complete package, `final_cover_produced` must be `true` and the cover image file must exist. If image generation is blocked, mark the package as blocked/incomplete instead of final.

If carousel inner pages were requested, `inner_pages_produced` must be `true` and the image files must exist. If the user declined carousel pages, set `carousel_requested=false` and `inner_pages_produced=false` with status `not_requested`.

If final cover or requested inner pages are only described but not generated/supplied, mark them as `false` / `blocked` or `not_yet_generated`. Do not imply the user has a visual preview.
