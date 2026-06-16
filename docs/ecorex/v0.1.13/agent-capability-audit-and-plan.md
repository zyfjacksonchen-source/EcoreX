# EcoreX v0.1.13 Agent Capability Audit And Plan

## Summary

This audit checks the user-requested capabilities against the current EcoreX source. The result is intentionally explicit: some capabilities are now enabled as built-ins, while product-level subagents and Codex-style goal tools are not present yet and require planned development.

## Enabled In v0.1.13

### find skill

- Status: implemented.
- Source added:
  - `agent/tools/find/find.py`
  - `agent/tools/find/__init__.py`
  - `skills/find/SKILL.md`
- Agent behavior: use the structured `find` tool to locate files/directories by pattern within the permission profile before reading files or falling back to shell.
- Permission boundary: every traversed root, directory, and returned file goes through `authorize_file_access("read", ...)`.

### skill-creator

- Status: already present as a built-in skill.
- Source present before v0.1.13:
  - `skills/skill-creator/SKILL.md`
- v0.1.13 keeps it in the desktop built-in ability list so users can ask EcoreX to create a reusable Skill without manual installation.

### EcoreX CLI preset

- Status: implemented.
- Source added:
  - `agent/tools/ecorex_cli/ecorex_cli.py`
  - `agent/tools/ecorex_cli/__init__.py`
- Purpose: expose safe, structured access to the bundled project CLI without asking the model to compose raw shell commands.
- Mutating/network actions are gated by the permission broker.

## Existing Related Capabilities

EcoreX already has several pieces that are adjacent to multi-agent and goal-led work:

- Multiple frontend sessions can exist.
- Backend active requests are tracked.
- Same-request SSE broadcast/replay exists.
- Session locks prevent unsafe same-session overlap.
- Project memory and Goal Ledger documentation exist.
- Host diagnostics can report runtime boundaries.

These pieces are useful foundations, but they are not the same as product-level subagents or an agent-callable goal tool.

## Subagent Audit

Status: not enabled, because the source does not contain a product-level subagent coordinator.

What exists:

- Concurrent sessions and active request bookkeeping.
- Current-process streaming and cancellation primitives.
- MCP tool namespacing.

What is missing:

- A first-party `subagent` tool/API that an agent can call.
- Durable child-run/session records.
- Parent/child run event linking.
- Child workspace/profile isolation.
- UI for spawned workers, status, cancellation, and result merge.
- Permission and quota attribution per child run.

Development plan:

1. Define the child-run data model: parent request ID, child request ID, objective, workspace/profile, status, result, token usage, and audit path.
2. Add a backend subagent coordinator that can spawn a child agent run with explicit permission and quota boundaries.
3. Expose a structured `subagent` tool with actions such as `start`, `status`, `cancel`, and `collect`.
4. Add durable event replay so child results survive renderer reloads and runtime restarts.
5. Add Desktop/WebUI views for spawned subagents and their final merge state.
6. Add tests for parent cancellation, child cancellation, same-workspace contention, quota exhaustion, and replay after reconnect.

## Goal Capability Audit

Status: not enabled, because Goal Ledger is documentation, not an agent-callable runtime goal tool.

What exists:

- `docs/ecorex/goal-ledger.md` tracks release goals and release notes.
- Project memory and dream files can persist context.
- Agents can read/write files through permission-aware tools.

What is missing:

- A runtime goal store with a strict state machine.
- Agent-callable `goal` tool actions.
- Goal budgets and completion/blocking enforcement.
- UI for active goal state.
- Restart-durable goal updates.

Development plan:

1. Add a goal store under workspace state, separate from docs, with active goal, status, objective, budget, timestamps, and audit trail.
2. Implement a `goal` tool with actions `get`, `create`, `update`, and `complete`, gated by permission and product policy.
3. Add state rules mirroring Codex-style behavior: only one active goal, explicit completion, and blocked status only after repeated impasse.
4. Surface active goal state in Desktop/WebUI without requiring users to open markdown docs.
5. Persist goal events so reconnects and runtime restarts can reconstruct current state.
6. Add tests for duplicate goal creation, invalid transitions, budget reporting, blocked threshold, and UI refresh.

## Host Diagnostics Boundary

v0.1.13 updates `host_diagnostics` so the agent can inspect these facts directly:

- `hasBuiltInSubagents: false`
- `subagentPlanRequired: true`
- `hasGoalTool: false`
- `goalPlanRequired: true`
- `availableStructuredCliTools: ["feishu_cli", "ecorex_cli"]`

This prevents the agent from pretending these capabilities are already available while still giving a clear product plan.
