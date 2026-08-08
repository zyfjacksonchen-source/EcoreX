---
name: office-spreadsheets
description: Read, create, edit, analyze, calculate, and verify Excel/CSV spreadsheet workbooks. Use for .xlsx, .xlsm, .csv, .tsv, formulas, charts, tables, and data analysis; convert legacy .xls to .xlsx before structural verification.
mentionable: true
mention-category: document
user-invocable: true
compatibility-id: office-spreadsheets
adopts-official-skill: Spreadsheets
ecorex-native-facade: true
quality-gates:
  - typed-values
  - formula-audit
  - dashboard-structure
  - chart-render
  - render-preview
  - visual-inspection
  - export-verify
metadata: {"default_enabled":true,"requires":{"modules":["openpyxl","xlsxwriter"]}}
---

# Office Spreadsheets

This is the e-Mate-native compatibility facade for the official Codex
`Spreadsheets` workflow. Keep the public e-Mate skill ID
`office-spreadsheets` stable, but use the official `Spreadsheets` skill as the
authoritative workflow when it is available in `<available_skills>`.

If both skills are visible, read this skill first for e-Mate compatibility
rules, then read `Spreadsheets` for artifact-tool authoring, formatting,
formula, render, chart, and export details. If the official skill is not
visible, follow the equivalent contract below and use the safest available
local tools.

Use this skill when the user asks e-Mate to work with spreadsheets: Excel workbooks, CSV/TSV files, data cleanup, formulas, analysis tables, charts, dashboards, budgets, trackers, or model-style calculations.

## Default Workflow

1. Classify the request as read/analyze, clean/transform, edit existing workbook, create workbook, chart/dashboard, or export.
2. Preserve typed values. Numbers, dates, percentages, and currencies must stay machine-readable instead of becoming display-only text.
3. Keep formulas auditable. Use helper cells and cell references instead of hardcoded magic numbers inside calculation areas.
4. When editing an existing workbook, inspect current sheets, formulas, formats, table ranges, and chart dependencies before making changes.
5. For new or major workbook creation, use the official `Spreadsheets` artifact-tool workflow when available, with Codex bundled Node/runtime dependencies rather than unrelated global packages.
6. Save the final workbook as `.xlsx` unless the user explicitly asks for another format.
7. Verify key ranges, scan for formula errors, recalculate when needed, render the visible workbook/ranges, and inspect the output before delivery.

## Quality Contract

- No hidden formula errors such as `#REF!`, `#DIV/0!`, `#VALUE!`, `#NAME?`, or unintended circular references.
- Formulas should be consistent across rows/columns and easy for another person to audit.
- Use references instead of hardcoded assumptions inside calculation areas. Keep assumptions/raw data in dedicated input ranges.
- Store numbers, percentages, currency, and dates as typed spreadsheet values with invariant number formats, not preformatted strings.
- Charts and tables must have clear labels and readable formatting.
- Rendered tables, charts, dashboards, and key ranges must be legible, aligned, unclipped, and free of blank/broken chart surfaces; render-preview evidence must come from trusted runtime render output, not caller-provided metadata.
- Do not overwrite established workbook formatting unless the requested change requires it.
- For Google Sheets-targeted output, create and verify a local `.xlsx` first, then import through the appropriate cloud-document route when available.
- Final response should link to the final spreadsheet artifact, not scratch files, unless requested.

## e-Mate Adaptation

- This is a user-invocable office skill and should appear under the document category in `@skill`.
- For a new XLSX, call `skill_run` with this exact discovery ID and parameters shaped as `{"operation":"create","file_name":"workbook.xlsx","title":"...","sheets":[{"name":"Data","rows":[["Column",1]]}]}`. The Runtime-owned Office Pack creates and structurally validates the file, then publishes the resulting Artifact; do not fall back to `pip` or an untracked shell output.
- Preserve compatibility with existing prompts, shortcuts, and automations that mention `office-spreadsheets`.
- Prefer official Codex workspace dependencies for authoring, recalculation, render, and export QA when the host exposes them; do not silently swap to unrelated writer libraries for final workbook creation.
- The `office-pdf` capability pack remains a fallback for legacy parsing/preview and small local edits, but high-quality workbook creation should follow the official `Spreadsheets` artifact-tool workflow.
- If required authoring, recalculation, or rendering dependencies are unavailable, report the missing verification layer clearly instead of implying formula/render QA passed.
