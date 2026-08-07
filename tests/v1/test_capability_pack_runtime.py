from __future__ import annotations

import asyncio
import base64
from dataclasses import replace
import hashlib
from io import BytesIO
import os
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from ecorex.capabilities import (
    CapabilityPackBindingError,
    CapabilityPackManifest,
    CapabilityPackManifestError,
    CapabilityPackRuntime,
    CapabilityPackVerificationError,
    CapabilityService,
    ExecutionPolicy,
    PackServiceBinding,
    PackToolBinding,
    RuntimeAvailability,
    SandboxLevel,
    SchemaContractError,
    ToolArgumentsValidationError,
    ToolOutputValidationError,
    ToolSpec,
    ToolInvocationContext,
    WorkspaceReadError,
    WorkspaceReadHandler,
    build_capability_handler_set,
    builtin_capability_registry,
    builtin_pack_service_specs,
    tool_spec_digest,
    verify_capability_pack,
)
from ecorex.integration import (
    ImageGenerationToolHandler,
    ImageToolUnavailable,
    production_pack_adapter_resolver,
)
from ecorex.pack_catalog import CAPABILITY_PACK_SERVICE_IDS
from ecorex.server.pack_resolver import (
    production_pack_adapter_resolver as production_server_pack_adapter_resolver,
)
from ecorex.runtime import RuntimeSettings, create_app
from ecorex.update import Ed25519SignatureVerifier, SignatureEnvelope


def _signature(value: bytes = b"\0" * 64) -> SignatureEnvelope:
    return SignatureEnvelope(
        algorithm="ed25519",
        key_id="pack-key",
        value=base64.b64encode(value).decode("ascii"),
    )


def _signed_image_pack(tmp_path: Path):
    registry = builtin_capability_registry()
    artifact = tmp_path / "image-pack.bin"
    artifact.write_bytes(b"signed image dependency pack")
    binding = PackToolBinding(
        tool_id="imagegen",
        tool_version=registry.get("imagegen").version,
        spec_sha256=tool_spec_digest(registry.get("imagegen")),
    )
    unsigned = CapabilityPackManifest(
        schema_version=2,
        pack_id="image",
        version="1.0.0",
        runtime_api_version="1.0.0",
        platform="windows",
        architecture="x64",
        artifact_file_name=artifact.name,
        artifact_size_bytes=artifact.stat().st_size,
        artifact_sha256=hashlib.sha256(artifact.read_bytes()).hexdigest(),
        tools=(binding,),
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
    verifier = Ed25519SignatureVerifier({"pack-key": public})
    return registry, artifact, manifest, verifier


def _signed_service_pack(
    tmp_path: Path,
    *,
    pack_id: str,
    service_id: str,
    contract_sha256: str | None = None,
):
    registry = builtin_capability_registry()
    artifact = tmp_path / f"{pack_id}-pack.bin"
    artifact.write_bytes(f"signed {pack_id} dependency pack".encode())
    service = builtin_pack_service_specs()[service_id]
    binding = PackServiceBinding(
        service_id=service_id,
        service_version=service.version,
        contract_sha256=contract_sha256 or service.contract_sha256,
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
        tools=(),
        services=(binding,),
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
    verified = verify_capability_pack(
        manifest,
        artifact,
        verifier=Ed25519SignatureVerifier({"pack-key": public}),
        platform="windows",
        architecture="x64",
        runtime_api_version="1.0.0",
    )
    return registry, verified


def test_signed_pack_is_verified_and_bound_to_exact_tool_contract(
    tmp_path: Path,
) -> None:
    registry, artifact, manifest, verifier = _signed_image_pack(tmp_path)
    verified = verify_capability_pack(
        manifest,
        artifact,
        verifier=verifier,
        platform="windows",
        architecture="x64",
        runtime_api_version="1.0.0",
    )
    runtime = CapabilityPackRuntime(registry)
    runtime.bind(verified, {"imagegen": lambda arguments, context: {"ok": True}})

    assert runtime.installed_pack_ids == frozenset({"image"})
    assert set(runtime.handlers) == {"imagegen"}
    assert runtime.disabled_tools()["read"] == "verified_handler_not_installed"

    from ecorex.capabilities import CapabilityRegistry, ToolSpec

    original = registry.get("imagegen")
    mismatched = replace(
        original,
        description=original.description + " mismatched",
    )
    other = CapabilityPackRuntime(CapabilityRegistry((mismatched,)))
    with pytest.raises(CapabilityPackBindingError, match="contract"):
        other.bind(verified, {"imagegen": lambda _: {}})


@pytest.mark.parametrize(
    "pack_id",
    tuple(
        pack_id
        for pack_id, service_ids in CAPABILITY_PACK_SERVICE_IDS.items()
        if service_ids
    ),
)
def test_service_only_pack_binds_backend_contract_without_fake_tool(
    tmp_path: Path,
    pack_id: str,
) -> None:
    service_id = CAPABILITY_PACK_SERVICE_IDS[pack_id][0]
    registry, verified = _signed_service_pack(
        tmp_path,
        pack_id=pack_id,
        service_id=service_id,
    )
    handlers = production_server_pack_adapter_resolver(
        verified,
        workspace_roots=(tmp_path,),
        runtime_payload_root=tmp_path,
    )
    runtime = CapabilityPackRuntime(registry)
    runtime.bind(verified, handlers)

    assert handlers == {}
    assert runtime.installed_pack_ids == frozenset({pack_id})
    assert runtime.installed_service_ids == frozenset({service_id})
    assert runtime.handlers == {}


def test_service_only_pack_rejects_non_authoritative_contract_digest(
    tmp_path: Path,
) -> None:
    registry, verified = _signed_service_pack(
        tmp_path,
        pack_id="ocr",
        service_id=CAPABILITY_PACK_SERVICE_IDS["ocr"][0],
        contract_sha256="0" * 64,
    )

    with pytest.raises(CapabilityPackBindingError, match="service contract"):
        CapabilityPackRuntime(registry).bind(verified, {})


def test_signed_ocr_service_pack_composes_real_product_adapter(tmp_path: Path) -> None:
    registry, verified = _signed_service_pack(
        tmp_path,
        pack_id="ocr",
        service_id="ocr.extract",
    )
    runtime = CapabilityPackRuntime(registry)
    runtime.bind(verified, {})
    app = create_app(
        settings=RuntimeSettings(
            database_path=tmp_path / "runtime.db",
            installed_capability_packs=runtime.installed_pack_ids,
        )
    )
    ocr_runtime = app.state.runtime_composition.input_attachment_ocr_runtime
    assert ocr_runtime is not None
    assert ocr_runtime.provider.service_id == "ocr.extract"

    from PIL import Image, ImageDraw, ImageFont

    small = Image.new("L", (170, 30), 255)
    ImageDraw.Draw(small).text(
        (2, 5), "ECOREX OCR 4827", font=ImageFont.load_default(), fill=0
    )
    image = small.resize((1020, 180))
    content = BytesIO()
    image.save(content, "PNG")
    result = ocr_runtime.provider.extract(content.getvalue(), timeout_seconds=8.0)
    assert result["provider"] == "rapidocr_onnxruntime"
    assert "4827" in result["text"].replace(" ", "")


def test_production_pack_resolver_binds_executable_image_handler_and_fails_truthfully(
    tmp_path: Path,
) -> None:
    registry, artifact, manifest, verifier = _signed_image_pack(tmp_path)
    verified = verify_capability_pack(
        manifest,
        artifact,
        verifier=verifier,
        platform="windows",
        architecture="x64",
        runtime_api_version="1.0.0",
    )
    handlers = production_pack_adapter_resolver(verified)
    assert set(handlers) == {"imagegen"}
    assert isinstance(handlers["imagegen"], ImageGenerationToolHandler)

    runtime = CapabilityPackRuntime(registry)
    runtime.bind(verified, handlers)
    context = ToolInvocationContext(
        invocation_id="invoke-image-pack-0001",
        capability_snapshot_id="capability-image-pack-0001",
        policy_snapshot_id="permission-image-pack-0001",
        tool_id="imagegen",
        idempotency_key="image-pack-call-0001",
        approved=True,
        effective_sandbox=SandboxLevel.WORKSPACE_WRITE,
    )
    with pytest.raises(ImageToolUnavailable) as unavailable:
        asyncio.run(
            runtime.handlers["imagegen"]({"instruction": "create a dashboard"}, context)
        )
    assert unavailable.value.code == "managed_image_orchestration_not_configured"


def test_pack_parser_and_verifier_fail_closed_on_noncanonical_or_tampered_data(
    tmp_path: Path,
) -> None:
    _registry, artifact, manifest, verifier = _signed_image_pack(tmp_path)
    assert CapabilityPackManifest.from_bytes(manifest.to_bytes()) == manifest
    with pytest.raises(CapabilityPackManifestError, match="canonical"):
        CapabilityPackManifest.from_bytes(manifest.to_bytes() + b"\n")

    artifact.write_bytes(b"tampered but same-ish")
    with pytest.raises(CapabilityPackVerificationError):
        verify_capability_pack(
            manifest,
            artifact,
            verifier=verifier,
            platform="windows",
            architecture="x64",
            runtime_api_version="1.0.0",
        )


def test_workspace_read_is_bounded_and_does_not_leak_host_paths(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "brief.txt").write_text("abcdef", encoding="utf-8")
    (workspace / "binary.bin").write_bytes(b"\xff\x00\x01")
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    handler = WorkspaceReadHandler((workspace,), default_max_bytes=3)

    text = handler({"path": "brief.txt"})
    assert text["path"] == "workspace://0/brief.txt"
    assert str(workspace) not in repr(text)
    assert text["content"] == "abc"
    assert text["truncated"] is True
    assert text["next_offset_bytes"] == 3
    assert handler({"path": text["path"], "offset_bytes": 3})["content"] == "def"
    root = handler({"path": "."})
    assert root["path"] == "workspace://0/"
    assert [item["name"] for item in root["entries"]] == ["binary.bin", "brief.txt"]
    assert handler({"path": root["path"]}) == root
    assert handler({"path": "binary.bin"})["encoding"] == "base64"
    with pytest.raises(WorkspaceReadError):
        handler({"path": "../outside.txt"})
    with pytest.raises(WorkspaceReadError):
        handler({"path": str(outside)})
    linked = workspace / "linked.txt"
    try:
        os.symlink(outside, linked)
    except (OSError, NotImplementedError):
        pass
    else:
        with pytest.raises(WorkspaceReadError):
            handler({"path": "linked.txt"})


def test_handler_set_reports_real_executability_not_declared_pack_flags(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry, artifact, manifest, verifier = _signed_image_pack(tmp_path)
    verified = verify_capability_pack(
        manifest,
        artifact,
        verifier=verifier,
        platform="windows",
        architecture="x64",
        runtime_api_version="1.0.0",
    )
    packs = CapabilityPackRuntime(registry)
    packs.bind(verified, {"imagegen": lambda arguments, context: {"ok": True}})
    handlers = build_capability_handler_set(
        registry,
        workspace_roots=(workspace,),
        pack_runtime=packs,
    )

    assert set(handlers.handlers) == {"read", "imagegen"}
    assert handlers.installed_pack_ids == frozenset({"image"})
    # Progressive Skill, capability, Connector and Artifact discovery are all
    # present in the built-in catalog.  This low-level builder has not yet been
    # bound to RuntimeComposition's trusted Core handlers, so every missing
    # executable is reported fail-closed.  RuntimeComposition may clear only
    # these exact absence facts after binding its non-replaceable handlers.
    assert handlers.disabled_tools == {
        tool_id: "verified_handler_not_installed"
        for tool_id in {
            "fetch",
            "vision",
            "ocr",
            "cdp",
            "shell",
            "skill_search",
            "skill_read",
            "skill_run",
            "task_list",
            "tool_search",
            "tool_describe",
            "connector_search",
            "connector_describe",
            "connector_read",
            "connector_write",
            "artifact_read",
            "input_attachment_read",
        }
    }


def test_tool_arguments_and_outputs_are_enforced_before_crossing_handler_boundary() -> (
    None
):
    called = False

    def handler(arguments):
        nonlocal called
        called = True
        return {"answer": "ok"}

    spec = ToolSpec(
        tool_id="strict-read",
        version="1.0.0",
        display_name="Strict read",
        description="Strict schema test",
        input_schema={
            "type": "object",
            "properties": {"path": {"type": "string", "minLength": 1}},
            "required": ["path"],
            "additionalProperties": False,
        },
        output_schema={
            "type": "object",
            "properties": {"answer": {"type": "string"}},
            "required": ["answer"],
            "additionalProperties": False,
        },
    )
    from ecorex.capabilities import CapabilityRegistry

    service = CapabilityService(
        CapabilityRegistry((spec,)), handlers={spec.tool_id: handler}
    )
    plan = service.create_plan(
        intent="strict",
        explicit_tools=(spec.tool_id,),
        availability=RuntimeAvailability(platform="windows"),
        policy=ExecutionPolicy(snapshot_id="perm"),
    )
    with pytest.raises(ToolArgumentsValidationError):
        asyncio.run(
            service.tool_call(
                plan.snapshot_id,
                spec.tool_id,
                {"path": "brief", "secret": "must-not-cross"},
                policy_snapshot_id="perm",
            )
        )
    assert called is False
    result = asyncio.run(
        service.tool_call(
            plan.snapshot_id,
            spec.tool_id,
            {"path": "brief"},
            policy_snapshot_id="perm",
        )
    )
    assert result.value == {"answer": "ok"}

    broken = CapabilityService(
        service.registry,
        handlers={spec.tool_id: lambda _: {"unexpected": True}},
    )
    broken_plan = broken.create_plan(
        intent="strict",
        explicit_tools=(spec.tool_id,),
        availability=RuntimeAvailability(platform="windows"),
        policy=ExecutionPolicy(snapshot_id="perm"),
    )
    with pytest.raises(ToolOutputValidationError):
        asyncio.run(
            broken.tool_call(
                broken_plan.snapshot_id,
                spec.tool_id,
                {"path": "brief"},
                policy_snapshot_id="perm",
            )
        )

    with pytest.raises(SchemaContractError):
        ToolSpec(
            tool_id="unsafe-schema",
            version="1.0.0",
            display_name="Unsafe",
            description="Unsupported references",
            input_schema={"type": "object", "$ref": "https://attacker/schema"},
            output_schema={"type": "object"},
        )
