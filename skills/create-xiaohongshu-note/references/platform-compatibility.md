# Platform Compatibility

This skill is intentionally portable:

- `SKILL.md` uses only `name` and `description` frontmatter.
- Codex metadata lives separately in `agents/openai.yaml`.
- Scripts are plain Python and avoid Codex-only APIs.
- Role orchestration is written as behavior, not as a hard dependency on one agent framework.

Install/copy targets:

- Codex: copy `create-xiaohongshu-note` into `~/.codex/skills` when desired.
- Claude Code: copy `create-xiaohongshu-note` into `~/.claude/skills` when desired.

If the runtime has native subagents, map Designer Agent, Copy Master, and Audit Master to subagents under Main Agent orchestration. If not, run the roles sequentially while preserving the same outputs and acceptance checks.
