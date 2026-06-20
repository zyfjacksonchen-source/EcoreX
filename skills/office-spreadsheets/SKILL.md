---
name: office-spreadsheets
description: Read, create, edit, analyze, calculate, and verify Excel/CSV spreadsheet workbooks. Use for .xlsx, .xls, .csv, .tsv, formulas, charts, tables, and data analysis.
mentionable: true
mention-category: document
user-invocable: true
metadata:
  cowagent:
    default_enabled: true
    requires:
      modules:
        - openpyxl
        - xlsxwriter
---

# Office Spreadsheets

Use this skill when the user asks EcoreX to work with spreadsheets: Excel workbooks, CSV/TSV files, data cleanup, formulas, analysis tables, charts, dashboards, budgets, trackers, or model-style calculations.

## Default Workflow

1. Classify the request as read/analyze, clean/transform, edit existing workbook, create workbook, chart/dashboard, or export.
2. Preserve typed values. Numbers, dates, percentages, and currencies must stay machine-readable instead of becoming display-only text.
3. Keep formulas auditable. Use helper cells and cell references instead of hardcoded magic numbers inside calculation areas.
4. When editing an existing workbook, inspect current sheets, formulas, formats, table ranges, and chart dependencies before making changes.
5. Save the final workbook as `.xlsx` unless the user explicitly asks for another format.
6. Verify key ranges, scan for formula errors, and render or inspect the visible output before delivery.

## Quality Contract

- No hidden formula errors such as `#REF!`, `#DIV/0!`, `#VALUE!`, `#NAME?`, or unintended circular references.
- Formulas should be consistent across rows/columns and easy for another person to audit.
- Charts and tables must have clear labels and readable formatting.
- Do not overwrite established workbook formatting unless the requested change requires it.
- Final response should link to the final spreadsheet artifact, not scratch files, unless requested.

## EcoreX Adaptation

- This is a user-invocable office skill and should appear under the document category in `@skill`.
- Spreadsheet parsing and authoring helpers are provided by the `office-pdf` capability pack when preinstalled.
- If spreadsheet modules are unavailable, guide the user to enable/install the Office/PDF capability pack before continuing.
