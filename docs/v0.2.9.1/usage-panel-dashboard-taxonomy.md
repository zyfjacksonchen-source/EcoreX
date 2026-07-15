# Usage Panel Dashboard Taxonomy Hotfix

- Date: 2026-07-06
- Target: `/ecorex-agent/usage-panel/`
- Source brief: Tencent Docs `DUmFHU0JncG1UYnRn`, title `看板优化`

## Changes

- Added a top dashboard definition band for artifact validity, task status, and scenario taxonomy.
- Treated user thumbs-down feedback as invalid artifact definition; invalid artifacts are excluded from effective artifacts.
- Split task status into `成功`, `失败`, and `中止`; `中止` means the user actively stopped the task.
- Remapped usage scenarios to seven categories: `创作内容`, `制作素材`, `搜索查询`, `处理数据`, `编辑文档`, `交付通知`, `系统维护`.
- Moved the original `v0.2.9 审计补充` panel below the main dashboard and made it collapsed by default.
- Updated the scenario chart interaction so each scenario row keeps the count on the right, and clicking the row expands definition text in the form `创作内容：创作文案/标题/报告/脚本等，占当前筛选内容的 30%`.
- Removed the duplicate native title tooltip from scenario rows; hover/click detail no longer repeats the right-side count.
- Removed the custom hover tooltip from scenario rows so long definitions are not clipped; full scenario definitions now appear in the click-expanded detail area with wrapping.

## Verification

- `python -m py_compile deploy/ecorex-usage-panel/usage_panel_api.py`
- `node --check deploy/ecorex-usage-panel/app.js`
- Local browser smoke passed: definition band exists, audit details are collapsed by default, failure/stopped/invalid artifact columns render, and console error count is zero.
- Production deploy evidence: `docs/v0.2.9.1/artifacts/production-usage-panel-dashboard-taxonomy.json`
