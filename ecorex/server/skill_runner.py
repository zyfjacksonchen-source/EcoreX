"""Production composition for the controlled Skill process boundary."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ecorex.extensions import (
    ControlledSkillProcessRunner,
    LocalSkillBundleStore,
    SandboxControlledSkillProcessBackend,
    TrustedSkillInterpreter,
    UnavailableControlledSkillProcessBackend,
)


def create_production_controlled_skill_runner(
    store: LocalSkillBundleStore,
    *,
    platform: str,
    sandbox_authority: tuple[Any, Path, Any] | None = None,
    workspace_roots: tuple[Path, ...] = (),
) -> ControlledSkillProcessRunner:
    """Bind the typed runner while failing closed on unproved OS authority."""

    if sandbox_authority is not None:
        sandbox, executable, identity = sandbox_authority
        interpreter = TrustedSkillInterpreter(
            runtime="python",
            executable=executable,
            sha256=str(identity.sha256),
        )
        backend = SandboxControlledSkillProcessBackend(
            sandbox,
            cas_root=store.root,
            interpreter=interpreter,
            workspace_roots=workspace_roots,
        )
        return ControlledSkillProcessRunner(
            store,
            backend=backend,
            interpreters={"python": interpreter},
        )

    normalized = platform.casefold()
    if normalized in {"windows", "win32"}:
        reason = "windows_skill_cas_read_authority_unavailable"
    elif normalized in {"macos", "darwin"}:
        reason = "macos_skill_file_read_scope_unavailable"
    else:
        reason = "controlled_skill_os_backend_unavailable"
    return ControlledSkillProcessRunner(
        store,
        backend=UnavailableControlledSkillProcessBackend(reason),
        interpreters={},
    )


__all__ = ["create_production_controlled_skill_runner"]
