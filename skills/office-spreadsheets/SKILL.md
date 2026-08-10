---
name: office-spreadsheets
description: Create basic XLSX workbooks and inspect cell text, sheet structure, and observed formulas from existing e-Mate XLSX artifacts. Use for new tabular workbooks or read-only review of a current same-thread workbook Artifact.
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

The packaged e-Mate native facade supports two operations in the first release:

- `create`: create a basic XLSX workbook from bounded sheets and rows.
- `inspect`: extract bounded cell text, sheet names, row counts, and observed formula strings from an existing e-Mate XLSX Artifact.

## Native calls

Create with:

`{"operation":"create","file_name":"workbook.xlsx","title":"...","sheets":[{"name":"Data","rows":[["Column",1]]}]}`

Inspect with the exact current Artifact identities:

`{"operation":"inspect","artifact_id":"art_...","revision_id":"rev_..."}`

Inspection accepts only a ready XLSX Artifact owned by the current account and created in the current thread. It reads immutable Artifact bytes, never caller-provided filesystem paths. Formula strings are observable, but formulas are not recalculated and cached results are not treated as verified calculations.

## First-release boundary

The native facade does not edit an existing workbook, calculate or audit formulas, preserve complex formatting, create charts or dashboards, convert CSV/XLS/XLSM, import Google Sheets, render ranges, or perform visual QA. Do not claim those actions succeeded. When one is requested, state that the packaged native operation is unavailable instead of presenting a newly created workbook as an edit of the original.

Created files receive structural-open validation only. Report that calculation and visual layout were not verified.
