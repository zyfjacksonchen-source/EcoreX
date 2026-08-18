---
name: office-spreadsheets
description: Create, replacement-edit, and inspect basic XLSX workbooks through the verified Office Pack.
mentionable: true
mention-category: document
user-invocable: true
compatibility-id: office-spreadsheets
adopts-official-skill: Spreadsheets
ecorex-native-facade: true
quality-gates:
  - structural-open
  - typed-values
  - artifact-integrity
metadata: {"default_enabled":true,"requires":{"modules":["openpyxl"]}}
---

# Office Spreadsheets

The packaged Cow tool supports these operations through the verified Office Pack:

- `create`: create a basic XLSX workbook from bounded sheets and rows.
- `edit`: write a new `-edited.xlsx` from complete sheets/rows content after the source opens successfully. Set `output_path` explicitly to replace that exact path atomically.
- `inspect`: extract bounded cell text, sheet names, row counts, and observed formula strings.

## Native calls

Create with:

`{"action":"create","path":"workbook.xlsx","title":"...","sheets":[{"name":"Data","rows":[["Column",1]]}]}`

Edit with the same complete structured content and the returned path:

`{"action":"edit","path":"workbook.xlsx","output_path":"workbook-v2.xlsx","title":"...","sheets":[{"name":"Data","rows":[["Column",2]]}]}`

Inspect with `{"action":"inspect","path":"workbook.xlsx"}`.

Inspection resolves the supplied local path through the Cow file-access broker and opens it in the same verified Office Pack. Formula strings are observable, but formulas are not recalculated and cached results are not treated as verified calculations.

Replacement edit rewrites the complete simple sheets/rows structure; it does not calculate formulas or preserve complex formatting/charts. Do not claim those unsupported properties survived.

Created files receive structural-open validation only. Report that calculation and visual layout were not verified.
