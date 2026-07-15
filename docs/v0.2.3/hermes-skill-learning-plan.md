# Hermes Skill Learning Research for EcoreX v0.2.3

## Source Baseline

- Repository: `NousResearch/hermes-agent`
- Research HEAD: `a2b49e6`
- Research date: 2026-06-26
- Research agent: Maxwell, read-only explorer

## Hermes Files of Interest

- `agent/learn_prompt.py`: `/learn` is a normal agent turn that emits a standardized authoring prompt, then asks the agent to collect evidence and call skill management.
- `tools/skill_manager_tool.py`: central skill CRUD surface with validation, path controls, size limits, staging, approvals, and atomic writes.
- `tools/skills_guard.py`: static scanner for prompt injection, secrets, destructive commands, exfiltration, persistence, callback shells, path traversal, symlinks, and oversized files.
- `tools/skill_provenance.py`: provenance tracking for user-directed foreground writes versus background review/curation.
- `agent/curator.py`: background skill review/merge/archive loop. EcoreX should borrow review/report/rollback ideas, not the scheduler/runtime.
- `tools/skills_tool.py`, `agent/skill_commands.py`, `agent/skill_utils.py`: progressive disclosure, skill body loading, env requirements, disabled skills, and support-file handling.
- `agent/skill_preprocessing.py`: template variables and opt-in inline shell. EcoreX should not adopt inline shell.

## EcoreX Decision

EcoreX will not port Hermes active session, queue, gateway, or delivery runtime. The native path is:

1. A user task succeeds through ordinary EcoreX tools.
2. EcoreX proposes learning the verified workflow.
3. `agent_capability action=request_skill_learning` produces an authoring prompt.
4. `agent_capability action=create_skill_draft` creates a ledger-backed draft.
5. Draft validation, security scan, and role reviews write `skill_draft.*` events.
6. Approval calls `SkillService.add` to materialize the skill under workspace `skills/<name>`.
7. `RuntimeProjectionService` reduces the ledger into `skill_drafts` for refresh-safe UI state.

The skill file tree is a materialized cache. `RunEventLedger` and `RuntimeProjection` remain the canonical runtime state.

## Replacing `create-xiaohongshu-note`

The fixed built-in `create-xiaohongshu-note` is removed from source, active Codex skills, and active EcoreX workspace skills. Future Xiaohongshu workflows should run first through normal capabilities:

- CDP-first/browser link reading.
- Fast OCR URL extraction.
- Feishu/external connection readiness.
- Image generation through the generic image skill.
- Runtime events and review evidence.

After a successful run, EcoreX can generate a learned draft such as `xhs-note-workflow-<project>` with only the verified steps, required external connections, permission declarations, risk gates, and output contract.

## Risk Controls

- P0: Skill writes bypass ledger or projection. Mitigation: all self-learning lifecycle events use `RunEventLedger`, and projection exposes `skill_drafts`.
- P0: Learned skill stores secrets or destructive commands. Mitigation: secret-shaped text scan, high-risk command scan, path whitelist, size limit, and `SkillService` registration.
- P0: Old fixed skill silently returns through packaging. Mitigation: release validator now requires `skill-creator` instead of `create-xiaohongshu-note`; manager no longer treats XHS as a managed built-in.
- P1: First Xiaohongshu run quality drops after removing the giant skill. Mitigation: ordinary toolchain must pass the first-run acceptance scenario before learned skill registration.
- P1: Background learning creates low-quality skills. Mitigation: default is draft-only; approval requires explicit validation/security/role-review evidence.

## Acceptance

- `create-xiaohongshu-note` is not present in source built-in skills, active Codex skills, active EcoreX workspace skills, or managed built-in refresh markers.
- `skill-creator` remains bundled as the generic authoring base.
- `SkillLearningService` can create a draft, emit validation/security/review events, and project the draft through `RuntimeProjectionService`.
- Formal registration reuses `SkillService.add`; no generic file tool is the official skill materialization path.
