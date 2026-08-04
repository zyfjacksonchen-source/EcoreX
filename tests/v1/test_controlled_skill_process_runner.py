from __future__ import annotations

import asyncio
import hashlib
import io
import json
from pathlib import Path
import sys
import zipfile

from ecorex.extensions import (
    CONTROLLED_SKILL_PROCESS_PROTOCOL,
    ControlledSkillLaunchPlan,
    ControlledSkillProcessRunner,
    ControlledSkillRunRequest,
    LocalSkillBundleStore,
    TrustedSkillInterpreter,
)
from ecorex.integration.sandbox import (
    MacOSSandboxExecBackend,
    SandboxLaunchPlan,
    SandboxProbe,
)
from ecorex.server.skill_runner import create_production_controlled_skill_runner


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _bundle() -> bytes:
    runtime = json.dumps(
        {
            "schema_version": 1,
            "runtime": "python",
            "entrypoint": "scripts/main.py",
            "environment": [],
            "network_domains": [],
            "external_commands": [],
            "effects": ["read"],
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    script = """\
import json, sys
request = json.loads(sys.stdin.buffer.read().decode('utf-8'))
response = {
    'schema_version': 1,
    'protocol': 'emate-controlled-skill-process-v1',
    'contract_id': request['contract_id'],
    'status': 'completed',
    'result': {'bound': True},
}
sys.stdout.write(json.dumps(response, sort_keys=True, separators=(',', ':')))
"""
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr(
            "SKILL.md",
            "---\nname: Runner test\ndescription: Runner test\nversion: 1.0.0\n---\nRun it.\n",
        )
        archive.writestr("skill-runtime.json", runtime)
        archive.writestr("scripts/main.py", script)
    return stream.getvalue()


class _BoundTestBackend:
    reason = ""

    def supports(self, runtime: str) -> bool:
        return runtime == "python"

    def launch_plan(self, request):
        # Test-only transport double: production composition never installs
        # this backend and therefore cannot turn Skill content into host argv.
        return ControlledSkillLaunchPlan(
            (
                str(request.interpreter_path),
                "-I",
                str(request.entrypoint_path),
            ),
            "test-attested-backend",
            request.contract.contract_id,
        )


class _AttestedSandboxBackend:
    def __init__(self) -> None:
        self.probe_read_roots: tuple[Path, ...] = ()
        self.launch_read_roots: tuple[Path, ...] = ()

    def probe(self, **values):
        self.probe_read_roots = values["read_roots"]
        return SandboxProbe(
            backend_id="test-os-sandbox",
            platform="test",
            ready=True,
            reason="ready",
            filesystem_read_scope="runtime-cas-workspace",
            filesystem_write_scoped=True,
            network_denied=True,
            process_tree_contained=True,
        )

    def launch_plan(self, **values):
        self.launch_read_roots = values["read_roots"]
        return SandboxLaunchPlan(
            (
                str(values["python_executable"]),
                "-I",
                str(values["artifact_path"]),
            ),
            "test-os-sandbox",
        )


def test_typed_runner_binds_cas_entry_interpreter_and_protocol(tmp_path: Path) -> None:
    store = LocalSkillBundleStore(tmp_path / "cas")
    bundle = store.ingest_zip(_bundle())
    executable = Path(sys.executable).resolve(strict=True)
    runner = ControlledSkillProcessRunner(
        store,
        backend=_BoundTestBackend(),
        interpreters={
            "python": TrustedSkillInterpreter("python", executable, _digest(executable))
        },
    )
    fence_calls = 0

    def fence() -> None:
        nonlocal fence_calls
        fence_calls += 1

    result = asyncio.run(
        runner.run(
            ControlledSkillRunRequest(
                extension_id="local.runner-test",
                revision_id="revision-1",
                artifact_sha256=bundle.artifact_sha256,
                extension_generation=7,
                runtime="python",
                entrypoint="scripts/main.py",
                parameters={},
                environment={},
                network_domains=(),
                effects=("read",),
            ),
            state_fence=fence,
        )
    )
    assert dict(result.result) == {"bound": True}
    assert fence_calls >= 3
    assert CONTROLLED_SKILL_PROCESS_PROTOCOL == "emate-controlled-skill-process-v1"


def test_production_runner_exposes_platform_blocker_without_host_fallback(
    tmp_path: Path,
) -> None:
    store = LocalSkillBundleStore(tmp_path / "cas")
    windows = create_production_controlled_skill_runner(store, platform="Windows")
    macos = create_production_controlled_skill_runner(store, platform="Darwin")
    assert windows.supports("python") is False
    assert windows.supports("node") is False
    assert windows.unavailable_reason == "windows_skill_cas_read_authority_unavailable"
    assert macos.supports("python") is False
    assert macos.unavailable_reason == "macos_skill_file_read_scope_unavailable"


def test_production_runner_uses_attested_sandbox_and_exact_cas_revision(
    tmp_path: Path,
) -> None:
    store = LocalSkillBundleStore(tmp_path / "cas")
    bundle = store.ingest_zip(_bundle())
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    executable = Path(sys.executable).resolve(strict=True)
    sandbox = _AttestedSandboxBackend()
    identity = type("Identity", (), {"sha256": _digest(executable)})()
    runner = create_production_controlled_skill_runner(
        store,
        platform="Windows",
        sandbox_authority=(sandbox, executable, identity),
        workspace_roots=(workspace,),
    )

    result = asyncio.run(
        runner.run(
            ControlledSkillRunRequest(
                extension_id="local.runner-test",
                revision_id="revision-1",
                artifact_sha256=bundle.artifact_sha256,
                extension_generation=1,
                runtime="python",
                entrypoint="scripts/main.py",
                parameters={},
                environment={},
                network_domains=(),
                effects=("read",),
            ),
            state_fence=lambda: None,
        )
    )

    assert dict(result.result) == {"bound": True}
    assert sandbox.probe_read_roots == (store.root.resolve(strict=True),)
    entrypoint, _record = store.resolve_verified_file(
        bundle.artifact_sha256, "scripts/main.py"
    )
    assert sandbox.launch_read_roots == (entrypoint.parents[1],)


def test_macos_controlled_skill_policy_has_no_host_wide_read(tmp_path: Path) -> None:
    runtime = tmp_path / "payload" / "python" / "bin"
    runtime.mkdir(parents=True)
    executable = runtime / "python3"
    executable.write_bytes(b"python")
    cas = tmp_path / "state" / "extension-cas" / ("a" * 64)
    cas.mkdir(parents=True)
    entrypoint = cas / "main.py"
    entrypoint.write_text("pass", encoding="utf-8")
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    policy = MacOSSandboxExecBackend._policy(
        workspace_roots=(workspace,),
        read_roots=(cas,),
        python_executable=executable,
        artifact_path=entrypoint,
    )

    assert "(allow file-read*)" not in policy
    assert str(cas).replace("\\", "\\\\") in policy
    assert str(tmp_path / "payload").replace("\\", "\\\\") in policy
    assert "(deny network*)" in policy


def test_native_platform_sources_prove_current_fail_closed_boundary() -> None:
    root = Path(__file__).resolve().parents[2]
    windows_security = (
        root / "platform-staging/native/windows/ecorex_sandbox_security.cpp"
    ).read_text(encoding="utf-8")
    windows_process = (
        root / "platform-staging/native/windows/ecorex_sandbox_process.cpp"
    ).read_text(encoding="utf-8")
    macos_backend = (root / "ecorex/integration/sandbox.py").read_text(
        encoding="utf-8"
    )
    # Windows signed security receipts admit only the active slot and the
    # product-owned durable CAS; the process protocol admits Python -I file.
    assert 'request.install_root / L"state" / L"extension-cas"' in windows_security
    assert "(!slot_read && !cas_read)" in windows_security
    assert "windows-appcontainer-stable-provision-v4" in (
        root / "ecorex/integration/windows_sandbox_security.py"
    ).read_text(encoding="utf-8")
    assert "argc - child_index != 3" in windows_process
    assert 'std::wstring(argv[child_index + 1]) != L"-I"' in windows_process
    assert '"runtime-cas-workspace"' in macos_backend
    assert "allowed_reads" in macos_backend
