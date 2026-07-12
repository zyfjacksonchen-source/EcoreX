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
import zipfile

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
import pytest

from ecorex.capabilities import (
    ApprovalRequiredError,
    CapabilityPackManifest,
    CapabilityService,
    ExecutionPolicy,
    PackToolBinding,
    PermissionProfile,
    RuntimeAvailability,
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
    SandboxLaunchPlan,
    SandboxProbe,
    UnavailableSandboxBackend,
)
from ecorex.integration.pack_python import build_pack_python_manifest
from ecorex.server.pack_resolver import production_pack_adapter_resolver
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

request = json.load(sys.stdin)
Path(request["context"]["workspace_roots"][0], ".pack-temp-observation").write_text(
    os.environ.get("TEMP", "") + "\\n" + os.environ.get("TMP", ""),
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
sys.stdout.write(json.dumps(response, sort_keys=True, separators=(",", ":")))
"""


class _UnitContractSandboxBackend:
    """Protocol test double; never used by the Product resolver."""

    def probe(self, **_kwargs):
        return SandboxProbe(
            backend_id="unit-contract-only",
            platform="test",
            ready=True,
            reason="ready",
            filesystem_read_scoped=True,
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
        tools=("fetch",),
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
        intent="fetch",
        explicit_tools=("fetch",),
        availability=RuntimeAvailability(
            platform="windows", installed_packs=frozenset({"browser"})
        ),
        policy=ExecutionPolicy(snapshot_id="policy-default"),
    )
    result = asyncio.run(
        service.tool_call(
            plan.snapshot_id,
            "fetch",
            {"url": "https://example.com/brief"},
            policy_snapshot_id="policy-default",
        )
    )
    assert result.value == {
        "tool_id": "fetch",
        "sandbox": "workspace-write",
        "approved": False,
        "idempotency_key": None,
        "workspace_count": 1,
        "parent_secret_present": False,
    }
    observed_temp, observed_tmp = (
        workspace / ".pack-temp-observation"
    ).read_text(encoding="utf-8").splitlines()
    assert observed_temp == observed_tmp
    assert Path(observed_temp).name.startswith("ecorex-pack-call-")
    assert not Path(observed_temp).exists()


def test_product_resolver_binds_browser_pack_only_with_resolved_workspace(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    pack = _verified_pack(
        tmp_path,
        pack_id="browser",
        tools=("fetch",),
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
    assert set(handlers) == {"fetch"}


def test_product_resolver_injects_the_slot_owned_windows_sandbox_helper(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    pack = _verified_pack(
        tmp_path,
        pack_id="sandbox",
        tools=("shell",),
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
                    "contract": "windows-appcontainer-stable-provision-v2",
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
    assert set(handlers) == {"shell"}


def test_sandbox_pack_receives_backend_authoritative_permission_snapshot(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    pack = _verified_pack(
        tmp_path,
        pack_id="sandbox",
        tools=("shell",),
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
        intent="shell",
        explicit_tools=("shell",),
        availability=RuntimeAvailability(
            platform="windows", installed_packs=frozenset({"sandbox"})
        ),
        policy=ExecutionPolicy(snapshot_id="policy-default"),
    )
    with pytest.raises(ApprovalRequiredError):
        asyncio.run(
            service.tool_call(
                plan.snapshot_id,
                "shell",
                {"command": "echo safe"},
                policy_snapshot_id="policy-default",
                idempotency_key="job:tool",
            )
        )
    result = asyncio.run(
        service.tool_call(
            plan.snapshot_id,
            "shell",
            {"command": "echo safe", "timeout_seconds": 5},
            policy_snapshot_id="policy-default",
            idempotency_key="job:tool",
            approved=True,
        )
    )
    assert result.value["sandbox"] == "workspace-write"
    assert result.value["approved"] is True
    assert result.value["idempotency_key"] == "job:tool"
    assert result.value["sandbox_os_enforced"] is True
    assert result.value["read_scope"] == "workspace-only"
    assert result.value["write_scope"] == "workspace-only"
    assert result.value["network_scope"] == "denied"
    assert result.value["process_tree_scope"] == "contained-inherited"
    assert result.value["stdout_limit_bytes"] == 4 * 1024 * 1024


def test_workspace_shell_is_fail_closed_without_a_verified_os_backend(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    pack = _verified_pack(
        tmp_path,
        pack_id="sandbox",
        tools=("shell",),
        main=ECHO_MAIN,
    )
    adapter = ProcessCapabilityPackAdapter(
        pack,
        workspace_roots=(workspace,),
        python_executable=sys.executable,
        sandbox_backend=UnavailableSandboxBackend("sandbox_probe_not_verified"),
    )
    assert adapter.sandbox_profile_availability == {
        "workspace-write": "sandbox_probe_not_verified",
        "danger-full-access": "windows_process_tree_supervisor_unavailable",
        "read-only": "shell_read_only_profile_unsupported",
    }
    service = CapabilityService(
        builtin_capability_registry(), handlers=adapter.handlers()
    )
    plan = service.create_plan(
        intent="shell",
        explicit_tools=("shell",),
        availability=RuntimeAvailability(
            platform="windows", installed_packs=frozenset({"sandbox"})
        ),
        policy=ExecutionPolicy(snapshot_id="default"),
    )
    with pytest.raises(CapabilityPackProcessError) as raised:
        asyncio.run(
            service.tool_call(
                plan.snapshot_id,
                "shell",
                {"command": "echo should-not-run"},
                policy_snapshot_id="default",
                idempotency_key="job:blocked",
                approved=True,
            )
        )
    assert raised.value.code == "workspace_sandbox_unavailable"


def test_explicit_full_access_uses_auditable_danger_contract_without_claiming_sandbox(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    pack = _verified_pack(
        tmp_path,
        pack_id="sandbox",
        tools=("shell",),
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
        intent="shell",
        explicit_tools=("shell",),
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
            "shell",
            {"command": "echo explicitly-unrestricted"},
            policy_snapshot_id="full",
            idempotency_key="job:danger",
        )
    )
    assert result.record.effective_sandbox == "danger-full-access"
    assert result.value["sandbox"] == "danger-full-access"
    assert result.value["sandbox_backend_id"] == "unit-contract-only"
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
        tools=("fetch",),
        main="raise SystemExit(7)\n",
    )
    adapter = ProcessCapabilityPackAdapter(
        pack, workspace_roots=(workspace,), python_executable=sys.executable
    )
    handler = adapter.handlers()["fetch"]
    service = CapabilityService(builtin_capability_registry(), handlers={"fetch": handler})
    plan = service.create_plan(
        intent="fetch",
        explicit_tools=("fetch",),
        availability=RuntimeAvailability(
            platform="windows", installed_packs=frozenset({"browser"})
        ),
        policy=ExecutionPolicy(snapshot_id="policy"),
    )
    with pytest.raises(CapabilityPackProcessError) as raised:
        asyncio.run(
            service.tool_call(
                plan.snapshot_id,
                "fetch",
                {"url": "https://example.com"},
                policy_snapshot_id="policy",
            )
        )
    assert raised.value.code == "pack_process_exited"


def test_descriptor_must_match_the_outer_signed_tool_contract(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    pack = _verified_pack(
        tmp_path,
        pack_id="browser",
        tools=("fetch",),
        descriptor_tools=("cdp",),
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
        tools=("fetch",),
        main=main,
    )
    adapter = ProcessCapabilityPackAdapter(
        pack, workspace_roots=(workspace,), python_executable=sys.executable
    )
    service = CapabilityService(
        builtin_capability_registry(), handlers=adapter.handlers()
    )
    plan = service.create_plan(
        intent="fetch",
        explicit_tools=("fetch",),
        availability=RuntimeAvailability(
            platform="windows", installed_packs=frozenset({"browser"})
        ),
        policy=ExecutionPolicy(snapshot_id="policy"),
    )
    with pytest.raises(CapabilityPackProcessError) as raised:
        asyncio.run(
            service.tool_call(
                plan.snapshot_id,
                "fetch",
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
        tools=("fetch",),
        main="import sys\nsys.stdout.write('x' * (5 * 1024 * 1024))\n",
    )
    adapter = ProcessCapabilityPackAdapter(
        pack, workspace_roots=(workspace,), python_executable=sys.executable
    )
    service = CapabilityService(
        builtin_capability_registry(), handlers=adapter.handlers()
    )
    plan = service.create_plan(
        intent="fetch",
        explicit_tools=("fetch",),
        availability=RuntimeAvailability(
            platform="windows", installed_packs=frozenset({"browser"})
        ),
        policy=ExecutionPolicy(snapshot_id="policy"),
    )
    with pytest.raises(CapabilityPackProcessError) as raised:
        asyncio.run(
            service.tool_call(
                plan.snapshot_id,
                "fetch",
                {"url": "https://example.com"},
                policy_snapshot_id="policy",
            )
        )
    assert raised.value.code == "pack_process_output_too_large"


def test_shell_timeout_kills_the_process_and_returns_one_bounded_failure(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    pack = _verified_pack(
        tmp_path,
        pack_id="sandbox",
        tools=("shell",),
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
        intent="shell",
        explicit_tools=("shell",),
        availability=RuntimeAvailability(
            platform="windows", installed_packs=frozenset({"sandbox"})
        ),
        policy=ExecutionPolicy(snapshot_id="timeout"),
    )
    with pytest.raises(CapabilityPackProcessError) as raised:
        asyncio.run(
            service.tool_call(
                plan.snapshot_id,
                "shell",
                {"command": "long-running-command"},
                policy_snapshot_id="timeout",
                idempotency_key="timeout:tool",
                approved=True,
            )
        )
    assert raised.value.code == "pack_process_timeout"


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
        tools=("shell",),
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
        intent="shell",
        explicit_tools=("shell",),
        availability=RuntimeAvailability(
            platform="windows", installed_packs=frozenset({"sandbox"})
        ),
        policy=ExecutionPolicy(snapshot_id="handshake"),
    )
    with pytest.raises(CapabilityPackProcessError) as raised:
        asyncio.run(
            service.tool_call(
                plan.snapshot_id,
                "shell",
                {"command": "echo no-forged-handshake"},
                policy_snapshot_id="handshake",
                idempotency_key="handshake:tool",
                approved=True,
            )
        )
    assert raised.value.code == "pack_sandbox_handshake_mismatch"


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS Seatbelt real-host contract")
def test_macos_workspace_sandbox_denies_outside_read_write_network_and_child_escape(
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
        tools=("shell",),
        main=main,
    )
    adapter = ProcessCapabilityPackAdapter(
        pack, workspace_roots=(workspace,), python_executable=sys.executable
    )
    if not adapter.sandbox_probe or not adapter.sandbox_probe.complete:
        pytest.skip(
            "this macOS host did not prove the full Seatbelt contract: "
            + (adapter.sandbox_probe.reason if adapter.sandbox_probe else "no probe")
        )
    service = CapabilityService(
        builtin_capability_registry(), handlers=adapter.handlers()
    )
    plan = service.create_plan(
        intent="shell",
        explicit_tools=("shell",),
        availability=RuntimeAvailability(
            platform="macos", installed_packs=frozenset({"sandbox"})
        ),
        policy=ExecutionPolicy(snapshot_id="macos-seatbelt"),
    )
    result = asyncio.run(
        service.tool_call(
            plan.snapshot_id,
            "shell",
            {"command": "boundary-probe"},
            policy_snapshot_id="macos-seatbelt",
            idempotency_key="macos:boundary",
            approved=True,
        )
    ).value
    assert result["inside_write"] is True
    assert result["outside_read"] is False
    assert result["outside_write"] is False
    assert result["child_escape"] is False
    assert result["network_errno"] in {1, 13}


def test_pack_bytes_are_revalidated_after_adapter_binding(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    pack = _verified_pack(
        tmp_path,
        pack_id="browser",
        tools=("fetch",),
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
        intent="fetch",
        explicit_tools=("fetch",),
        availability=RuntimeAvailability(
            platform="windows", installed_packs=frozenset({"browser"})
        ),
        policy=ExecutionPolicy(snapshot_id="policy"),
    )
    with pytest.raises(CapabilityPackProcessError) as raised:
        asyncio.run(
            service.tool_call(
                plan.snapshot_id,
                "fetch",
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
        tools=("fetch",),
        main=main,
    )
    adapter = ProcessCapabilityPackAdapter(
        pack, workspace_roots=(workspace,), python_executable=sys.executable
    )
    service = CapabilityService(
        builtin_capability_registry(), handlers=adapter.handlers()
    )
    plan = service.create_plan(
        intent="fetch",
        explicit_tools=("fetch",),
        availability=RuntimeAvailability(
            platform="windows", installed_packs=frozenset({"browser"})
        ),
        policy=ExecutionPolicy(snapshot_id="policy"),
    )
    with pytest.raises(CapabilityPackProcessError) as raised:
        asyncio.run(
            service.tool_call(
                plan.snapshot_id,
                "fetch",
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
