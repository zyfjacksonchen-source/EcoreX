---
name: find
description: Locate files and folders in the workspace or an allowed filesystem profile. Use when the user asks to find a file, locate paths, discover where code/config/docs live, search by filename pattern, or inspect repository structure before reading/editing files.
metadata: {"always":true}
---

# Find

Use this skill to locate files and folders before reading or editing them.

## Workflow

1. Use the `find` tool for filename or path-pattern discovery.
2. Use `ls` to inspect a promising directory.
3. Use `read` only after the relevant file is identified.
4. Use `web_search` only when the target is not local or the user explicitly asks for internet search.

## Patterns

- Find by extension: `find(pattern="*.py")`
- Find by name fragment: `find(pattern="*config*")`
- Find under a directory: `find(pattern="*.tsx", path="desktop/src")`
- Find directories: `find(pattern="*release*", type="dir")`

## Guardrails

- Prefer narrow `path`, `type`, and `max_depth` values on large repositories.
- Do not use shell `find`, `dir /s`, or recursive PowerShell scans when the `find` tool can do the job.
- Respect permission-profile failures. If a path is blocked, report the blocked boundary instead of trying a raw shell workaround.
