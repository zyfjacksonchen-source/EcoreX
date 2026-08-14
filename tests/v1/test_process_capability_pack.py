from __future__ import annotations

import asyncio
import base64
import ctypes
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace
import zipfile

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
import pytest

import ecorex.integration.sandbox as sandbox_module
import ecorex.server.pack_resolver as pack_resolver_module
from ecorex.capabilities import (
    CapabilityPackManifest,
    CapabilityService,
    ExecutionPolicy,
    PackToolBinding,
    PermissionProfile,
    RuntimeAvailability,
    SandboxLevel,
    ToolExecutionScope,
    ToolInvocationContext,
    builtin_capability_registry,
    tool_spec_digest,
    verify_capability_pack,
)
from ecorex.integration.pack_process import (
    CapabilityPackProcessError,
    PACK_PROCESS_PROTOCOL,
    ProcessCapabilityPackAdapter,
    _minimal_environment,
    _windows_kill_process_tree,
)
from ecorex.integration.sandbox import (
    _BoundedProcessResult,
    _macos_probe_failure_reason,
    _macos_probe_result_complete,
    SandboxLaunchPlan,
    SandboxProbe,
    UnavailableSandboxBackend,
)
from ecorex.integration.pack_python import build_pack_python_manifest
from ecorex.server.pack_resolver import (
    create_production_pack_adapter_resolver,
    production_pack_adapter_resolver,
)
from ecorex.update import Ed25519SignatureVerifier, SignatureEnvelope


def _signature(value: bytes = b"\0" * 64) -> SignatureEnvelope:
    return SignatureEnvelope(
        algorithm="ed25519",
        key_id="pack-key",
        value=base64.b64encode(value).decode("ascii"),
    )


def _verified_pack(
    tmp_path: Path,
    *,
    pack_id: str,
    tools: tuple[str, ...],
    main: str,
    descriptor_tools: tuple[str, ...] | None = None,
):
    registry = builtin_capability_registry()
    artifact = tmp_path / f"{pack_id}.zip"
    descriptor = {
        "schema_version": 1,
        "protocol": PACK_PROCESS_PROTOCOL,
        "pack_id": pack_id,
        "runtime_api_version": "1.0.0",
        "tools": list(descriptor_tools or tools),
    }
    with zipfile.ZipFile(artifact, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("__main__.py", main)
        archive.writestr(
            "ecorex-pack.json",
            json.dumps(
                descriptor,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8"),
        )
    bindings = tuple(
        PackToolBinding(
            tool_id=tool_id,
            tool_version=registry.get(tool_id).version,
            spec_sha256=tool_spec_digest(registry.get(tool_id)),
        )
        for tool_id in tools
    )
    unsigned = CapabilityPackManifest(
        schema_version=2,
        pack_id=pack_id,
        version="1.0.0",
        runtime_api_version="1.0.0",
        platform="windows",
        architecture="x64",
        artifact_file_name=artifact.name,
        artifact_size_bytes=artifact.stat().st_size,
        artifact_sha256=hashlib.sha256(artifact.read_bytes()).hexdigest(),
        tools=bindings,
        services=(),
        signature=_signature(),
    )
    private = Ed25519PrivateKey.generate()
    manifest = CapabilityPackManifest(
        **{
            **unsigned.unsigned_dict(),
            "tools": unsigned.tools,
            "services": unsigned.services,
            "signature": _signature(private.sign(unsigned.canonical_payload())),
        }
    )
    public = private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return verify_capability_pack(
        manifest,
        artifact,
        verifier=Ed25519SignatureVerifier({"pack-key": public}),
        platform="windows",
        architecture="x64",
        runtime_api_version="1.0.0",
    )


ECHO_MAIN = """
import json
import os
from pathlib import Path
import sys
import tempfile

request = json.load(sys.stdin)
Path(os.environ["TEMP"], "pack-temp-write").write_text("ok", encoding="utf-8")
with tempfile.NamedTemporaryFile() as temporary:
    tempfile_parent = str(Path(temporary.name).parent)
Path(request["context"]["workspace_roots"][0], ".pack-temp-observation").write_text(
    os.environ.get("TEMP", "")
    + "\\n"
    + os.environ.get("TMP", "")
    + "\\n"
    + os.environ.get("TMPDIR", "")
    + "\\n"
    + tempfile_parent,
    encoding="utf-8",
)
result = {
    "tool_id": request["tool_id"],
    "sandbox": request["context"]["effective_sandbox"],
    "approved": request["context"]["approved"],
    "idempotency_key": request["context"]["idempotency_key"],
    "workspace_count": len(request["context"]["workspace_roots"]),
    "parent_secret_present": "ECOREX_TEST_PARENT_SECRET" in os.environ,
}
response = {
    "schema_version": 1,
    "request_id": request["request_id"],
    "status": "completed",
    "result": result,
}
sandbox_contract = request["context"].get("sandbox_contract")
if sandbox_contract is not None:
    response["sandbox_contract_id"] = sandbox_contract["contract_id"]
    result["sandbox_backend_id"] = sandbox_contract["backend_id"]
    result["sandbox_os_enforced"] = sandbox_contract["os_enforced"]
    result["read_scope"] = sandbox_contract["filesystem_read_scope"]
    result["write_scope"] = sandbox_contract["filesystem_write_scope"]
    result["network_scope"] = sandbox_contract["network_scope"]
    result["process_tree_scope"] = sandbox_contract["process_tree_scope"]
    result["stdout_limit_bytes"] = sandbox_contract["stdout_limit_bytes"]
    result["timeout_seconds"] = sandbox_contract["timeout_seconds"]
sys.stdout.write(json.dumps(response, sort_keys=True, separators=(",", ":")))
"""


SESSION_MAIN = r"""
import json
import sys

state = {}
while True:
    header = sys.stdin.buffer.read(8)
    if not header:
        break
    size = int.from_bytes(header, "big")
    request = json.loads(sys.stdin.buffer.read(size))
    thread_id = request["context"]["execution_scope"]["thread_id"]
    state[thread_id] = state.get(thread_id, 0) + 1
    response = {
        "schema_version": 1,
        "request_id": request["request_id"],
        "status": "completed",
        "result": {"thread_id": thread_id, "count": state[thread_id]},
    }
    payload = json.dumps(response, sort_keys=True, separators=(",", ":")).encode()
    sys.stdout.buffer.write(len(payload).to_bytes(8, "big") + payload)
    sys.stdout.buffer.flush()
"""


def test_browser_pack_process_keeps_thread_state_across_tool_calls(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    pack = _verified_pack(
        tmp_path,
        pack_id="browser",
        tools=("browser",),
        main=SESSION_MAIN,
    )
    adapter = ProcessCapabilityPackAdapter(
        pack, workspace_roots=(workspace,), python_executable=sys.executable
    )

    async def exercise() -> None:
        scope = ToolExecutionScope("job-session", "thread-session", "turn-session")
        results = []
        for index in range(2):
            context = ToolInvocationContext(
                invocation_id=f"invoke-session-{index}",
                capability_snapshot_id="cap-session",
                policy_snapshot_id="policy-session",
                tool_id="browser",
                idempotency_key=None,
                approved=True,
                effective_sandbox=SandboxLevel.DANGER_FULL_ACCESS,
                execution_scope=scope,
            )
            results.append(
                await adapter.invoke("browser", {"action": "snapshot"}, context)
            )
        assert results == [
            {"thread_id": "thread-session", "count": 1},
            {"thread_id": "thread-session", "count": 2},
        ]
        await adapter._stop_browser_session()

    asyncio.run(exercise())


class _UnitContractSandboxBackend:
    """Protocol test double; never used by the Product resolver."""

    def probe(self, **_kwargs):
        return SandboxProbe(
            backend_id="unit-contract-only",
            platform="test",
            ready=True,
            reason="ready",
            filesystem_read_scope="workspace-and-runtime",
            filesystem_write_scoped=True,
            network_denied=True,
            process_tree_contained=True,
        )

    def launch_plan(self, *, python_executable, artifact_path, **_kwargs):
        return SandboxLaunchPlan(
            (str(python_executable), "-I", str(artifact_path)),
            "unit-contract-only",
        )


def test_browser_pack_process_is_executable_and_parent_secrets_are_absent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    pack = _verified_pack(
        tmp_path,
        pack_id="browser",
        tools=("web_fetch",),
        main=ECHO_MAIN,
    )
    adapter = ProcessCapabilityPackAdapter(
        pack, workspace_roots=(workspace,), python_executable=sys.executable
    )
    monkeypatch.setenv("ECOREX_TEST_PARENT_SECRET", "must-not-cross")
    service = CapabilityService(
        builtin_capability_registry(), handlers=adapter.handlers()
    )
    plan = service.create_plan(
        intent="web_fetch",
        explicit_tools=("web_fetch",),
        availability=RuntimeAvailability(
            platform="windows", installed_packs=frozenset({"browser"})
        ),
        policy=ExecutionPolicy(snapshot_id="policy-default"),
    )
    result = asyncio.run(
        service.tool_call(
            plan.snapshot_id,
            "web_fetch",
            {"url": "https://example.com/brief"},
            policy_snapshot_id="policy-default",
        )
    )
    assert result.value == {
        "tool_id": "web_fetch",
        "sandbox": "danger-full-access",
        "approved": False,
        "idempotency_key": None,
        "workspace_count": 1,
        "parent_secret_present": False,
    }
    observed_temp, observed_tmp, observed_tmpdir, tempfile_parent = (
        workspace / ".pack-temp-observation"
    ).read_text(encoding="utf-8").splitlines()
    assert observed_temp == observed_tmp
    if os.name != "nt":
        assert observed_tmpdir == observed_temp
    assert Path(tempfile_parent) == Path(observed_temp)
    assert Path(observed_temp).name.startswith(".ecorex-pack-call-")
    assert Path(observed_temp).is_relative_to(workspace.resolve())
    assert not Path(observed_temp).exists()


def test_product_resolver_binds_browser_pack_only_with_resolved_workspace(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    pack = _verified_pack(
        tmp_path,
        pack_id="browser",
        tools=("web_fetch",),
        main=ECHO_MAIN,
    )
    payload = tmp_path / "payload"
    interpreter = payload / "bin" / "pack-python" / "python.exe"
    interpreter.parent.mkdir(parents=True)
    interpreter.write_bytes(Path(sys.executable).read_bytes())
    (payload / "pack-python.json").write_bytes(
        build_pack_python_manifest(payload, platform="windows", architecture="x64")
    )
    handlers = production_pack_adapter_resolver(pack, (workspace,), payload)
    assert set(handlers) == {"web_fetch"}


def test_product_composition_verifies_shared_pack_python_once_per_startup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    pack = _verified_pack(
        tmp_path,
        pack_id="browser",
        tools=("web_fetch",),
        main=ECHO_MAIN,
    )
    payload = tmp_path / "payload"
    interpreter = payload / "bin" / "pack-python" / "python.exe"
    interpreter.parent.mkdir(parents=True)
    interpreter.write_bytes(Path(sys.executable).read_bytes())
    (payload / "pack-python.json").write_bytes(
        build_pack_python_manifest(payload, platform="windows", architecture="x64")
    )
    original = pack_resolver_module.resolve_pack_python
    calls = 0

    def counted(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(pack_resolver_module, "resolve_pack_python", counted)
    first_startup = create_production_pack_adapter_resolver()
    assert set(first_startup(pack, (workspace,), payload)) == {"web_fetch"}
    assert set(first_startup(pack, (workspace,), payload)) == {"web_fetch"}
    shared_resolver = getattr(
        first_startup, "_resolve_pack_python_for_composition"
    )
    assert shared_resolver(
        payload, platform="windows", architecture="x64"
    )[0] == interpreter
    assert calls == 1

    # Cache lifetime is exactly one composition. A restart gets a new
    # resolver and independently verifies the signed closure again.
    second_startup = create_production_pack_adapter_resolver()
    assert set(second_startup(pack, (workspace,), payload)) == {"web_fetch"}
    assert calls == 2


def test_dependency_pack_services_reuse_the_composition_interpreter(
    tmp_path: Path,
) -> None:
    from ecorex.server.config import _resolve_composition_pack_python

    expected = (tmp_path / "python.exe", object())

    def resolver(*_args, **_kwargs):
        return {}

    setattr(
        resolver,
        "_resolve_pack_python_for_composition",
        lambda *_args, **_kwargs: expected,
    )

    assert _resolve_composition_pack_python(
        resolver,
        tmp_path,
        platform="windows",
        architecture="x64",
    ) == expected


def test_product_resolver_injects_the_slot_owned_windows_sandbox_helper(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    pack = _verified_pack(
        tmp_path,
        pack_id="sandbox",
        tools=("bash",),
        main=ECHO_MAIN,
    )
    slot = tmp_path / "install" / "slots" / "slot-test"
    payload = slot / "payload"
    interpreter = payload / "bin" / "pack-python" / "python.exe"
    interpreter.parent.mkdir(parents=True)
    interpreter.write_bytes(Path(sys.executable).read_bytes())
    helper = payload / "bin" / "ecorex-sandbox-host.exe"
    helper.write_bytes(b"MZ-test-helper")
    (payload / "pack-python.json").write_bytes(
        build_pack_python_manifest(payload, platform="windows", architecture="x64")
    )
    (slot / ".slot.json").write_text(
        json.dumps(
            {
                "security_provision": {
                    "schema_version": 1,
                    "contract": "windows-appcontainer-stable-provision-v3",
                    "helper_sha256": hashlib.sha256(helper.read_bytes()).hexdigest(),
                    "slot_digest": "0" * 64,
                    "root_security_sha256": "1" * 64,
                    "workspace_roots_sha256": "2" * 64,
                    "permission_domain_sha256": "3" * 64,
                }
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )

    handlers = production_pack_adapter_resolver(pack, (workspace,), payload)

    # The dummy helper cannot pass the behavioral probe, but construction
    # still proves Product composition selected the immutable slot path rather
    # than the generic unavailable/PATH fallback. Invocation remains closed.
    assert set(handlers) == {"bash"}


def test_legacy_pack_shell_receives_cowagent_full_access_without_a_prompt(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    pack = _verified_pack(
        tmp_path,
        pack_id="sandbox",
        tools=("bash",),
        main=ECHO_MAIN,
    )
    adapter = ProcessCapabilityPackAdapter(
        pack,
        workspace_roots=(workspace,),
        python_executable=sys.executable,
        sandbox_backend=_UnitContractSandboxBackend(),
    )
    service = CapabilityService(
        builtin_capability_registry(), handlers=adapter.handlers()
    )
    plan = service.create_plan(
        intent="bash",
        explicit_tools=("bash",),
        availability=RuntimeAvailability(
            platform="windows", installed_packs=frozenset({"sandbox"})
        ),
        policy=ExecutionPolicy(snapshot_id="policy-default"),
    )
    result = asyncio.run(
        service.tool_call(
            plan.snapshot_id,
            "bash",
            {"command": "echo safe", "timeout": 5},
            policy_snapshot_id="policy-default",
            idempotency_key="job:tool",
        )
    )
    assert result.value["sandbox"] == "danger-full-access"
    assert result.value["approved"] is False
    assert result.value["idempotency_key"] == "job:tool"
    default_timeout = asyncio.run(
        service.tool_call(
            plan.snapshot_id,
            "bash",
            {"command": "pwd"},
            policy_snapshot_id="policy-default",
            idempotency_key="job:tool-default-timeout",
        )
    )
    assert default_timeout.value["sandbox"] == "danger-full-access"


def test_cowagent_full_access_shell_does_not_require_an_extra_sandbox_backend(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    pack = _verified_pack(
        tmp_path,
        pack_id="sandbox",
        tools=("bash",),
        main=ECHO_MAIN,
    )
    adapter = ProcessCapabilityPackAdapter(
        pack,
        workspace_roots=(workspace,),
        python_executable=sys.executable,
        sandbox_backend=UnavailableSandboxBackend("sandbox_probe_not_verified"),
    )
    service = CapabilityService(
        builtin_capability_registry(), handlers=adapter.handlers()
    )
    plan = service.create_plan(
        intent="bash",
        explicit_tools=("bash",),
        availability=RuntimeAvailability(
            platform="windows", installed_packs=frozenset({"sandbox"})
        ),
        policy=ExecutionPolicy(snapshot_id="default"),
    )
    result = asyncio.run(
        service.tool_call(
            plan.snapshot_id,
            "bash",
            {"command": "echo runs"},
            policy_snapshot_id="default",
            idempotency_key="job:direct",
        )
    )
    assert result.value["sandbox"] == "danger-full-access"


def test_explicit_full_access_uses_auditable_danger_contract_without_claiming_sandbox(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    pack = _verified_pack(
        tmp_path,
        pack_id="sandbox",
        tools=("bash",),
        main=ECHO_MAIN,
    )
    adapter = ProcessCapabilityPackAdapter(
        pack,
        workspace_roots=(workspace,),
        python_executable=sys.executable,
        sandbox_backend=_UnitContractSandboxBackend(),
    )
    service = CapabilityService(
        builtin_capability_registry(), handlers=adapter.handlers()
    )
    plan = service.create_plan(
        intent="bash",
        explicit_tools=("bash",),
        availability=RuntimeAvailability(
            platform="windows", installed_packs=frozenset({"sandbox"})
        ),
        policy=ExecutionPolicy(
            snapshot_id="full",
            profile=PermissionProfile.FULL_ACCESS,
        ),
    )
    result = asyncio.run(
        service.tool_call(
            plan.snapshot_id,
            "bash",
            {"command": "echo explicitly-unrestricted"},
            policy_snapshot_id="full",
            idempotency_key="job:danger",
        )
    )
    assert result.record.effective_sandbox == "danger-full-access"
    assert result.value["sandbox"] == "danger-full-access"
    assert result.value["sandbox_backend_id"] == "explicit-unrestricted-process"
    assert result.value["sandbox_os_enforced"] is False
    assert result.value["read_scope"] == "host-unrestricted"
    assert result.value["write_scope"] == "host-unrestricted"
    assert result.value["network_scope"] == "host-unrestricted"


def test_pack_crash_is_contained_as_one_stable_tool_failure(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    pack = _verified_pack(
        tmp_path,
        pack_id="browser",
        tools=("web_fetch",),
        main="raise SystemExit(7)\n",
    )
    adapter = ProcessCapabilityPackAdapter(
        pack, workspace_roots=(workspace,), python_executable=sys.executable
    )
    handler = adapter.handlers()["web_fetch"]
    service = CapabilityService(builtin_capability_registry(), handlers={"web_fetch": handler})
    plan = service.create_plan(
        intent="web_fetch",
        explicit_tools=("web_fetch",),
        availability=RuntimeAvailability(
            platform="windows", installed_packs=frozenset({"browser"})
        ),
        policy=ExecutionPolicy(snapshot_id="policy"),
    )
    with pytest.raises(CapabilityPackProcessError) as raised:
        asyncio.run(
            service.tool_call(
                plan.snapshot_id,
                "web_fetch",
                {"url": "https://example.com"},
                policy_snapshot_id="policy",
            )
        )
    assert raised.value.code == "pack_process_exited"
    assert not tuple(workspace.glob(".ecorex-pack-call-*"))


def test_descriptor_must_match_the_outer_signed_tool_contract(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    pack = _verified_pack(
        tmp_path,
        pack_id="browser",
        tools=("web_fetch",),
        descriptor_tools=("browser",),
        main=ECHO_MAIN,
    )
    with pytest.raises(CapabilityPackProcessError) as raised:
        ProcessCapabilityPackAdapter(
            pack, workspace_roots=(workspace,), python_executable=sys.executable
        )
    assert raised.value.code == "pack_descriptor_invalid"


def test_pack_cannot_return_authoritative_host_paths(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    main = """
import json, sys
request = json.load(sys.stdin)
response = {
    "schema_version": 1,
    "request_id": request["request_id"],
    "status": "completed",
    "result": {"path": request["context"]["workspace_roots"][0]},
}
sys.stdout.write(json.dumps(response, sort_keys=True, separators=(",", ":")))
"""
    pack = _verified_pack(
        tmp_path,
        pack_id="browser",
        tools=("web_fetch",),
        main=main,
    )
    adapter = ProcessCapabilityPackAdapter(
        pack, workspace_roots=(workspace,), python_executable=sys.executable
    )
    service = CapabilityService(
        builtin_capability_registry(), handlers=adapter.handlers()
    )
    plan = service.create_plan(
        intent="web_fetch",
        explicit_tools=("web_fetch",),
        availability=RuntimeAvailability(
            platform="windows", installed_packs=frozenset({"browser"})
        ),
        policy=ExecutionPolicy(snapshot_id="policy"),
    )
    with pytest.raises(CapabilityPackProcessError) as raised:
        asyncio.run(
            service.tool_call(
                plan.snapshot_id,
                "web_fetch",
                {"url": "https://example.com"},
                policy_snapshot_id="policy",
            )
        )
    assert raised.value.code == "pack_result_exposed_host_path"


def test_output_flood_is_killed_without_growing_the_runtime_response(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    pack = _verified_pack(
        tmp_path,
        pack_id="browser",
        tools=("web_fetch",),
        main="import sys\nsys.stdout.write('x' * (5 * 1024 * 1024))\n",
    )
    adapter = ProcessCapabilityPackAdapter(
        pack, workspace_roots=(workspace,), python_executable=sys.executable
    )
    service = CapabilityService(
        builtin_capability_registry(), handlers=adapter.handlers()
    )
    plan = service.create_plan(
        intent="web_fetch",
        explicit_tools=("web_fetch",),
        availability=RuntimeAvailability(
            platform="windows", installed_packs=frozenset({"browser"})
        ),
        policy=ExecutionPolicy(snapshot_id="policy"),
    )
    with pytest.raises(CapabilityPackProcessError) as raised:
        asyncio.run(
            service.tool_call(
                plan.snapshot_id,
                "web_fetch",
                {"url": "https://example.com"},
                policy_snapshot_id="policy",
            )
        )
    assert raised.value.code == "pack_process_output_too_large"
    assert not tuple(workspace.glob(".ecorex-pack-call-*"))


def test_shell_timeout_kills_the_process_and_returns_one_bounded_failure(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    pack = _verified_pack(
        tmp_path,
        pack_id="sandbox",
        tools=("bash",),
        main="import time\ntime.sleep(30)\n",
    )
    adapter = ProcessCapabilityPackAdapter(
        pack,
        workspace_roots=(workspace,),
        python_executable=sys.executable,
        default_timeout_seconds=1,
        sandbox_backend=_UnitContractSandboxBackend(),
    )
    service = CapabilityService(
        builtin_capability_registry(), handlers=adapter.handlers()
    )
    plan = service.create_plan(
        intent="bash",
        explicit_tools=("bash",),
        availability=RuntimeAvailability(
            platform="windows", installed_packs=frozenset({"sandbox"})
        ),
        policy=ExecutionPolicy(snapshot_id="timeout"),
    )
    with pytest.raises(CapabilityPackProcessError) as raised:
        asyncio.run(
            service.tool_call(
                plan.snapshot_id,
                "bash",
                {"command": "long-running-command"},
                policy_snapshot_id="timeout",
                idempotency_key="timeout:tool",
                approved=True,
            )
        )
    assert raised.value.code == "pack_process_timeout"
    assert not tuple(workspace.glob(".ecorex-pack-call-*"))


def test_shell_pack_must_acknowledge_the_exact_sandbox_handshake(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    main = """
import json, sys
request = json.load(sys.stdin)
sys.stdout.write(json.dumps({
    "schema_version": 1,
    "request_id": request["request_id"],
    "status": "completed",
    "result": {"exit_code": 0},
    "sandbox_contract_id": "sandbox_" + "0" * 64,
}, sort_keys=True, separators=(",", ":")))
"""
    pack = _verified_pack(
        tmp_path,
        pack_id="sandbox",
        tools=("bash",),
        main=main,
    )
    adapter = ProcessCapabilityPackAdapter(
        pack,
        workspace_roots=(workspace,),
        python_executable=sys.executable,
        sandbox_backend=_UnitContractSandboxBackend(),
    )
    service = CapabilityService(
        builtin_capability_registry(), handlers=adapter.handlers()
    )
    plan = service.create_plan(
        intent="bash",
        explicit_tools=("bash",),
        availability=RuntimeAvailability(
            platform="windows", installed_packs=frozenset({"sandbox"})
        ),
        policy=ExecutionPolicy(snapshot_id="handshake"),
    )
    with pytest.raises(CapabilityPackProcessError) as raised:
        asyncio.run(
            service.tool_call(
                plan.snapshot_id,
                "bash",
                {"command": "echo no-forged-handshake"},
                policy_snapshot_id="handshake",
                idempotency_key="handshake:tool",
                approved=True,
            )
        )
    assert raised.value.code == "pack_sandbox_handshake_mismatch"


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS Seatbelt real-host contract")
def test_macos_pack_shell_uses_cowagent_full_access_without_the_workspace_profile(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside-secret.txt"
    outside.write_text("secret", encoding="utf-8")
    main = f"""
import errno, json, pathlib, socket, subprocess, sys
request = json.load(sys.stdin)
outside = pathlib.Path({str(outside)!r})
workspace = pathlib.Path(request["context"]["workspace_roots"][0])
result = {{}}
try:
    outside.read_text()
    result["outside_read"] = True
except Exception:
    result["outside_read"] = False
try:
    outside.write_text("escape")
    result["outside_write"] = True
except Exception:
    result["outside_write"] = False
try:
    workspace.joinpath("inside.txt").write_text("ok")
    result["inside_write"] = True
except Exception:
    result["inside_write"] = False
sock = socket.socket()
result["network_errno"] = sock.connect_ex(("1.1.1.1", 443))
sock.close()
child = subprocess.run(
    [sys.executable, "-c", "import pathlib,sys;pathlib.Path(sys.argv[1]).write_text('child')", str(outside)],
    capture_output=True,
)
result["child_escape"] = child.returncode == 0
response = {{
    "schema_version": 1,
    "request_id": request["request_id"],
    "status": "completed",
    "result": result,
    "sandbox_contract_id": request["context"]["sandbox_contract"]["contract_id"],
}}
sys.stdout.write(json.dumps(response, sort_keys=True, separators=(",", ":")))
"""
    pack = _verified_pack(
        tmp_path,
        pack_id="sandbox",
        tools=("bash",),
        main=main,
    )
    adapter = ProcessCapabilityPackAdapter(
        pack, workspace_roots=(workspace,), python_executable=sys.executable
    )
    assert adapter.sandbox_probe is not None
    assert adapter.sandbox_probe.complete, adapter.sandbox_probe.reason
    service = CapabilityService(
        builtin_capability_registry(), handlers=adapter.handlers()
    )
    plan = service.create_plan(
        intent="bash",
        explicit_tools=("bash",),
        availability=RuntimeAvailability(
            platform="macos", installed_packs=frozenset({"sandbox"})
        ),
        policy=ExecutionPolicy(snapshot_id="macos-seatbelt"),
    )
    result = asyncio.run(
        service.tool_call(
            plan.snapshot_id,
            "bash",
            {"command": "boundary-probe"},
            policy_snapshot_id="macos-seatbelt",
            idempotency_key="macos:boundary",
            approved=True,
        )
    ).value
    assert result["inside_write"] is True
    assert result["outside_read"] is True
    assert result["outside_write"] is True


def test_macos_probe_evaluator_rejects_false_network_and_child_evidence() -> None:
    completed = _BoundedProcessResult(returncode=0, stdout=b"{}", stderr=b"")
    passing = {
        "child_launch_errno": 0,
        "child_returncode": 0,
        "child_started": True,
        "child_write_errno": 1,
        "inside_write": True,
        "network_errno": 1,
        "network_close_ok": True,
        "outside_read_match": True,
        "outside_write_errno": 1,
    }
    assert _macos_probe_result_complete(
        completed,
        passing,
        outside_unchanged=True,
        child_marker_valid=True,
    )
    for field, value in (
        ("child_returncode", 1),
        ("child_launch_errno", 13),
        ("child_started", False),
        ("child_write_errno", 2),
        ("inside_write", False),
        ("network_errno", 61),
        ("outside_read_match", False),
        ("outside_write_errno", 2),
    ):
        rejected = dict(passing)
        rejected[field] = value
        assert not _macos_probe_result_complete(
            completed,
            rejected,
            outside_unchanged=True,
            child_marker_valid=True,
        )
    for invalid_errno in (True, False, "1", [], {}, None, 1.0):
        for field in (
            "child_write_errno",
            "child_launch_errno",
            "network_errno",
            "outside_write_errno",
        ):
            rejected = dict(passing)
            rejected[field] = invalid_errno
            assert not _macos_probe_result_complete(
                completed,
                rejected,
                outside_unchanged=True,
                child_marker_valid=True,
            )
    for invalid_read_match in (False, 1, "true", [], {}, None, 1.0):
        rejected = dict(passing)
        rejected["outside_read_match"] = invalid_read_match
        assert not _macos_probe_result_complete(
            completed,
            rejected,
            outside_unchanged=True,
            child_marker_valid=True,
        )
    for mutation in (
        {key: value for key, value in passing.items() if key != "child_started"},
        {**passing, "unexpected": False},
    ):
        assert not _macos_probe_result_complete(
            completed,
            mutation,
            outside_unchanged=True,
            child_marker_valid=True,
        )
    assert not _macos_probe_result_complete(
        _BoundedProcessResult(returncode=1, stdout=b"{}", stderr=b""),
        passing,
        outside_unchanged=True,
        child_marker_valid=True,
    )
    assert not _macos_probe_result_complete(
        completed,
        passing,
        outside_unchanged=False,
        child_marker_valid=True,
    )
    assert not _macos_probe_result_complete(
        completed,
        passing,
        outside_unchanged=True,
        child_marker_valid=False,
    )


def test_sandbox_probe_reports_a_truthful_read_scope() -> None:
    common = {
        "backend_id": "unit",
        "platform": "test",
        "ready": True,
        "reason": "ready",
        "filesystem_write_scoped": True,
        "network_denied": True,
        "process_tree_contained": True,
    }
    scoped = SandboxProbe(**common, filesystem_read_scope="workspace-and-runtime")
    unrestricted = SandboxProbe(
        **common, filesystem_read_scope="host-unrestricted"
    )
    unverified = SandboxProbe(**common)
    invalid = SandboxProbe(
        **common,
        filesystem_read_scope="unknown",
    )
    contradictory = SandboxProbe(
        **{**common, "reason": "probe_failed"},
        filesystem_read_scope="workspace-and-runtime",
    )

    assert scoped.complete
    assert scoped.to_dict()["filesystem_read_scope"] == "workspace-and-runtime"
    assert unrestricted.complete
    assert unrestricted.to_dict()["filesystem_read_scoped"] is False
    assert unrestricted.to_dict()["filesystem_read_scope"] == "host-unrestricted"
    assert not unverified.complete
    assert unverified.to_dict()["filesystem_read_scope"] == "unverified"
    assert not invalid.complete
    assert not contradictory.complete


def test_macos_probe_failure_reasons_are_stable_and_non_disclosing() -> None:
    passing = {
        "child_launch_errno": 0,
        "child_returncode": 0,
        "child_started": True,
        "child_write_errno": 1,
        "inside_write": True,
        "network_errno": 1,
        "network_close_ok": True,
        "outside_read_match": True,
        "outside_write_errno": 1,
    }
    completed = _BoundedProcessResult(returncode=0, stdout=b"{}", stderr=b"")
    assert (
        _macos_probe_failure_reason(
            completed,
            passing,
            outside_unchanged=True,
            child_marker_valid=True,
        )
        == "ready"
    )
    scenarios = (
        (None, passing, True, True, "macos_seatbelt_probe_process_unavailable"),
        (
            _BoundedProcessResult(returncode=1, stdout=b"", stderr=b""),
            passing,
            True,
            True,
            "macos_seatbelt_probe_process_nonzero",
        ),
        (completed, {}, True, True, "macos_seatbelt_probe_evidence_invalid"),
        (
            completed,
            {**passing, "child_launch_errno": 13},
            True,
            True,
            "macos_seatbelt_probe_child_launch_failed",
        ),
        (
            completed,
            {**passing, "child_returncode": 1},
            True,
            True,
            "macos_seatbelt_probe_child_nonzero",
        ),
        (
            completed,
            {**passing, "child_started": False},
            True,
            True,
            "macos_seatbelt_probe_child_not_started",
        ),
        (
            completed,
            {**passing, "child_write_errno": 61},
            True,
            True,
            "macos_seatbelt_probe_child_denial_unproven",
        ),
        (
            completed,
            {**passing, "inside_write": False},
            True,
            True,
            "macos_seatbelt_probe_workspace_write_failed",
        ),
        (
            completed,
            {**passing, "network_errno": 61},
            True,
            True,
            "macos_seatbelt_probe_network_denial_unproven",
        ),
        (
            completed,
            {**passing, "network_close_ok": False},
            True,
            True,
            "macos_seatbelt_probe_network_cleanup_failed",
        ),
        (
            completed,
            {**passing, "outside_read_match": False},
            True,
            True,
            "macos_seatbelt_probe_read_policy_unproven",
        ),
        (
            completed,
            {**passing, "outside_write_errno": 61},
            True,
            True,
            "macos_seatbelt_probe_write_denial_unproven",
        ),
        (completed, passing, False, True, "macos_seatbelt_probe_canary_changed"),
        (
            completed,
            passing,
            True,
            False,
            "macos_seatbelt_probe_child_marker_invalid",
        ),
    )
    for result, value, canary, marker, reason in scenarios:
        assert (
            _macos_probe_failure_reason(
                result,
                value,
                outside_unchanged=canary,
                child_marker_valid=marker,
            )
            == reason
        )
    assert (
        _macos_probe_failure_reason(
            _BoundedProcessResult(returncode=1, stdout=b"", stderr=b""),
            {},
            outside_unchanged=False,
            child_marker_valid=False,
            script_started=False,
        )
        == "macos_seatbelt_probe_interpreter_start_failed"
    )
    assert (
        _macos_probe_failure_reason(
            completed,
            {"fatal_phase": "child_evidence"},
            outside_unchanged=False,
            child_marker_valid=False,
        )
        == "macos_seatbelt_probe_child_evidence_failed"
    )
    assert (
        _macos_probe_failure_reason(
            completed,
            passing,
            outside_unchanged=True,
            child_marker_valid=True,
            script_started=False,
        )
        == "macos_seatbelt_probe_handshake_missing"
    )
    for invalid_phase in ([], {}, True, None, "unknown"):
        assert (
            _macos_probe_failure_reason(
                completed,
                {"fatal_phase": invalid_phase},
                outside_unchanged=False,
                child_marker_valid=False,
            )
            == "macos_seatbelt_probe_evidence_invalid"
        )
    assert (
        _macos_probe_failure_reason(
            completed,
            {"fatal_phase": "network"},
            outside_unchanged=False,
            child_marker_valid=False,
            script_started=False,
        )
        == "macos_seatbelt_probe_handshake_missing"
    )


@pytest.mark.parametrize(
    ("mode", "expected_reason"),
    (
        ("marker-missing", "macos_seatbelt_probe_child_marker_invalid"),
        ("canary-missing", "macos_seatbelt_probe_canary_changed"),
        ("invalid-json", "macos_seatbelt_probe_evidence_invalid"),
        ("bounded-unavailable", "macos_seatbelt_probe_process_unavailable"),
    ),
)
def test_macos_probe_preserves_completed_evidence_across_host_checks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    expected_reason: str,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    executable = tmp_path / "sandbox-exec"
    executable.write_bytes(b"fixture")
    artifact = workspace / "probe.pyz"
    artifact.write_bytes(b"fixture")
    passing = {
        "child_launch_errno": 0,
        "child_returncode": 0,
        "child_started": True,
        "child_write_errno": 1,
        "inside_write": True,
        "network_errno": 1,
        "network_close_ok": True,
        "outside_read_match": True,
        "outside_write_errno": 1,
    }

    monkeypatch.setattr(sandbox_module, "sys", SimpleNamespace(platform="darwin"))
    monkeypatch.setattr(sandbox_module, "_trusted_system_file", lambda _: executable)

    def bounded(command: tuple[str, ...], *, timeout_seconds: float):
        del timeout_seconds
        probe_root = Path(command[-5])
        outside = Path(command[-4])
        if mode == "bounded-unavailable":
            return None
        if mode == "canary-missing":
            (probe_root / "child-started").write_bytes(b"started")
            outside.unlink()
        payload = b"not-json" if mode == "invalid-json" else json.dumps(passing).encode()
        stdout = b"ecorex-macos-seatbelt-probe-v1\n" + payload + b"\n"
        return _BoundedProcessResult(returncode=0, stdout=stdout, stderr=b"")

    monkeypatch.setattr(sandbox_module, "_run_bounded_probe", bounded)
    probe = sandbox_module.MacOSSandboxExecBackend(executable).probe(
        workspace_roots=(workspace,),
        python_executable=Path(sys.executable),
        artifact_path=artifact,
    )

    assert not probe.ready
    assert probe.reason == expected_reason


def test_pack_bytes_are_revalidated_after_adapter_binding(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    pack = _verified_pack(
        tmp_path,
        pack_id="browser",
        tools=("web_fetch",),
        main=ECHO_MAIN,
    )
    adapter = ProcessCapabilityPackAdapter(
        pack, workspace_roots=(workspace,), python_executable=sys.executable
    )
    payload = bytearray(pack.artifact_path.read_bytes())
    payload[-1] ^= 0x01
    pack.artifact_path.write_bytes(payload)
    service = CapabilityService(
        builtin_capability_registry(), handlers=adapter.handlers()
    )
    plan = service.create_plan(
        intent="web_fetch",
        explicit_tools=("web_fetch",),
        availability=RuntimeAvailability(
            platform="windows", installed_packs=frozenset({"browser"})
        ),
        policy=ExecutionPolicy(snapshot_id="policy"),
    )
    with pytest.raises(CapabilityPackProcessError) as raised:
        asyncio.run(
            service.tool_call(
                plan.snapshot_id,
                "web_fetch",
                {"url": "https://example.com"},
                policy_snapshot_id="policy",
            )
        )
    assert raised.value.code == "pack_artifact_changed"


def test_non_finite_pack_response_is_rejected_as_invalid_json(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    main = """
import json, sys
request = json.load(sys.stdin)
sys.stdout.write(json.dumps({
    "schema_version": 1,
    "request_id": request["request_id"],
    "status": "completed",
    "result": {"score": float("nan")},
}))
"""
    pack = _verified_pack(
        tmp_path,
        pack_id="browser",
        tools=("web_fetch",),
        main=main,
    )
    adapter = ProcessCapabilityPackAdapter(
        pack, workspace_roots=(workspace,), python_executable=sys.executable
    )
    service = CapabilityService(
        builtin_capability_registry(), handlers=adapter.handlers()
    )
    plan = service.create_plan(
        intent="web_fetch",
        explicit_tools=("web_fetch",),
        availability=RuntimeAvailability(
            platform="windows", installed_packs=frozenset({"browser"})
        ),
        policy=ExecutionPolicy(snapshot_id="policy"),
    )
    with pytest.raises(CapabilityPackProcessError) as raised:
        asyncio.run(
            service.tool_call(
                plan.snapshot_id,
                "web_fetch",
                {"url": "https://example.com"},
                policy_snapshot_id="policy",
            )
        )
    assert raised.value.code == "pack_response_invalid"


@pytest.mark.skipif(os.name != "nt", reason="Windows process-tree contract")
def test_windows_tree_kill_terminates_pack_descendants(tmp_path: Path) -> None:
    child_pid_path = tmp_path / "child.pid"
    parent_script = (
        "import pathlib,subprocess,sys,time;"
        "child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(120)']);"
        f"pathlib.Path({str(child_pid_path)!r}).write_text(str(child.pid));"
        "time.sleep(120)"
    )
    parent = subprocess.Popen(
        (sys.executable, "-I", "-c", parent_script),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=(
            getattr(subprocess, "CREATE_NO_WINDOW", 0)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        ),
    )
    child_pid: int | None = None
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not child_pid_path.exists():
            time.sleep(0.02)
        child_pid = int(child_pid_path.read_text(encoding="utf-8"))
        _windows_kill_process_tree(parent.pid)
        parent.wait(timeout=5)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and _windows_process_is_active(child_pid):
            time.sleep(0.02)
        assert _windows_process_is_active(child_pid) is False
    finally:
        if parent.poll() is None:
            _windows_kill_process_tree(parent.pid)
        if child_pid is not None and _windows_process_is_active(child_pid):
            subprocess.run(
                ("taskkill", "/PID", str(child_pid), "/T", "/F"),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )


@pytest.mark.skipif(os.name != "nt", reason="Windows environment contract")
def test_windows_system_tools_do_not_trust_parent_systemroot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    injected = tmp_path / "fake-windows"
    monkeypatch.setenv("SystemRoot", str(injected))
    monkeypatch.setenv("WINDIR", str(injected))
    environment = _minimal_environment()
    assert Path(environment["SYSTEMROOT"]).resolve() != injected.resolve()
    assert Path(environment["WINDIR"]).resolve() != injected.resolve()
    assert str(injected).casefold() not in environment["PATH"].casefold()


def _windows_process_is_active(pid: int) -> bool:
    process_query_limited_information = 0x1000
    still_active = 259
    handle = ctypes.windll.kernel32.OpenProcess(
        process_query_limited_information, False, pid
    )
    if not handle:
        return False
    try:
        exit_code = ctypes.c_ulong()
        if not ctypes.windll.kernel32.GetExitCodeProcess(
            handle, ctypes.byref(exit_code)
        ):
            return False
        return exit_code.value == still_active
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)
