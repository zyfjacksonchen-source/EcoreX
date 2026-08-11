from __future__ import annotations

import pytest

from ecorex.capabilities import (
    CapabilityService,
    ExecutionPolicy,
    Exposure,
    PermissionProfile,
    RuntimeAvailability,
    builtin_capability_registry,
    builtin_model_catalog,
)
from ecorex.capabilities.discovery import _truncate_utf8
from ecorex.capabilities.schema import SchemaInstanceError, validate_schema_instance
from ecorex.gateway import MAX_MODEL_VISIBLE_TOOLS, canonical_tool_schema_batch_bytes


def _complete_access_plan():
    service = CapabilityService(builtin_capability_registry())
    plan = service.create_plan(
        intent="",
        availability=RuntimeAvailability(
            platform="macos",
            installed_packs=frozenset(
                {"browser", "channels", "image", "ocr", "office", "sandbox"}
            ),
            online=True,
            selected_model_modalities=frozenset({"chat", "image"}),
            selected_model_capabilities={
                "chat": frozenset({"chat", "reasoning", "tools", "vision"}),
                "image": frozenset({"image_edit", "image_generation"}),
            },
        ),
        policy=ExecutionPolicy(
            snapshot_id="perm_complete_access_baseline",
            profile=PermissionProfile.FULL_ACCESS,
        ),
    )
    return service, plan


def test_complete_access_exposes_cowagent_baseline_without_keyword_discovery() -> None:
    service, plan = _complete_access_plan()
    direct = {decision.tool_id for decision in plan.direct}
    assert direct == {
        spec.tool_id
        for spec in service.registry.all()
        if spec.default_exposure is Exposure.DIRECT
    }
    assert {
        "browser",
        "web_fetch",
        "web_search",
        "imagegen",
        "read",
        "bash",
        "vision",
    } <= direct


def test_builtin_catalog_fits_one_model_working_set() -> None:
    service, plan = _complete_access_plan()
    decisions = (*plan.direct, *plan.deferred)
    descriptors = [
        service.tool_describe(plan.snapshot_id, decision.tool_id)
        for decision in decisions
    ]
    assert len(descriptors) <= MAX_MODEL_VISIBLE_TOOLS
    assert len(canonical_tool_schema_batch_bytes(descriptors)) <= 256 * 1024


def test_image_contracts_reject_requests_the_handlers_cannot_execute() -> None:
    registry = builtin_capability_registry()
    imagegen = registry.get("imagegen").input_schema
    vision = registry.get("vision").input_schema
    for invalid in ({}, {"instruction": "draw", "tasks": [{"instruction": "a"}, {"instruction": "b"}]}):
        with pytest.raises(SchemaInstanceError):
            validate_schema_instance(invalid, imagegen, label="imagegen")
    validate_schema_instance({"instruction": "draw"}, imagegen, label="imagegen")
    with pytest.raises(SchemaInstanceError):
        validate_schema_instance({"instruction": "inspect"}, vision, label="vision")
    validate_schema_instance(
        {"instruction": "inspect", "attachment_ids": ["attachment-1"]},
        vision,
        label="vision",
    )


def test_tool_search_is_reserved_for_extensions_absent_from_the_builtin_working_set() -> None:
    service, plan = _complete_access_plan()
    assert service.tool_search(
        plan.snapshot_id,
        "browser shell images and web search",
        exposure=Exposure.DEFERRED,
        model_catalog_payload=builtin_model_catalog().to_dict(),
    ) == ()


def test_untrusted_model_payload_metadata_is_bounded_and_control_safe() -> None:
    service = CapabilityService(builtin_capability_registry())
    plan = service.create_plan(
        intent="find a suitable capability",
        availability=RuntimeAvailability(
            platform="windows",
            installed_packs=frozenset({"image"}),
            selected_model_modalities=frozenset({"chat", "image"}),
            selected_model_capabilities={
                "chat": frozenset({"chat", "tools", "vision", "reasoning"}),
                "image": frozenset({"image_generation", "image_edit"}),
            },
        ),
        policy=ExecutionPolicy(snapshot_id="perm_untrusted_model_payload"),
    )
    payload = {
        "modalities": {
            "image": [
                {
                    "model_id": "other-image-model",
                    "aliases": [
                        "image\u202e2",
                        "x" * 129,
                        "bad\ud800alias",
                    ],
                    "capabilities": ["image_generation"],
                }
            ]
        }
    }

    assert service.tool_search(
        plan.snapshot_id,
        "image2",
        exposure=Exposure.DEFERRED,
        model_catalog_payload=payload,
    ) == ()


def test_evidence_utf8_truncation_never_splits_a_code_point() -> None:
    evidence = _truncate_utf8("证" * 100, 256)
    assert evidence
    assert len(evidence.encode("utf-8")) <= 256
    assert (evidence + "证").encode("utf-8")[:256].decode("utf-8", "ignore") == evidence
