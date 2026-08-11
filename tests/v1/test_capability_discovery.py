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


def _search(query: str, *, policy: ExecutionPolicy | None = None):
    service = CapabilityService(builtin_capability_registry())
    plan = service.create_plan(
        intent="find a suitable capability",
        availability=RuntimeAvailability(
            platform="windows",
            installed_packs=frozenset({"browser", "image", "sandbox"}),
            selected_model_modalities=frozenset({"chat", "image"}),
            selected_model_capabilities={
                "chat": frozenset({"chat", "tools", "vision", "reasoning"}),
                "image": frozenset({"image_generation", "image_edit"}),
            },
        ),
        policy=policy or ExecutionPolicy(snapshot_id="perm_discovery_matrix"),
    )
    return service.tool_search(
        plan.snapshot_id,
        query,
        exposure=Exposure.DEFERRED,
        model_catalog_payload=builtin_model_catalog().to_dict(),
    )


@pytest.mark.parametrize(
    ("query", "tool_id"),
    (
        ("please inspect image", "vision"),
        ("run a shell command", "shell"),
        ("read a web page", "fetch"),
        ("浏览器", "cdp"),
        ("执行命令", "shell"),
        ("读取网页", "fetch"),
        ("陌生图像识别", "vision"),
        ("design a poster", "imagegen"),
        ("设计海报", "imagegen"),
        ("image_2", "imagegen"),
    ),
)
def test_discovery_recall_uses_reviewed_units_and_phrase_groups(
    query: str,
    tool_id: str,
) -> None:
    results = _search(query)
    assert results and results[0].tool_id == tool_id


def test_storage_query_discovers_shell_without_bypassing_permission_policy() -> None:
    query = "查看本机或当前设备的磁盘、存储空间、文件系统容量和剩余空间（只读）"
    full_access = _search(
        query,
        policy=ExecutionPolicy(
            snapshot_id="perm_full_storage_discovery",
            profile=PermissionProfile.FULL_ACCESS,
        ),
    )
    assert [result.tool_id for result in full_access] == ["shell"]
    assert full_access[0].exposure is Exposure.DEFERRED
    assert full_access[0].requires_approval is False

    default = _search(
        query,
        policy=ExecutionPolicy(snapshot_id="perm_default_storage_discovery"),
    )
    assert [result.tool_id for result in default] == ["shell"]
    assert default[0].requires_approval is True

    denied = _search(
        query,
        policy=ExecutionPolicy(
            snapshot_id="perm_denied_storage_discovery",
            profile=PermissionProfile.FULL_ACCESS,
            admin_hard_denies=frozenset({"bash"}),
        ),
    )
    assert denied == ()


@pytest.mark.parametrize(
    "query",
    (
        "design",
        "设计",
        "poster",
        "海报",
    ),
)
def test_discovery_does_not_flatten_or_substring_infer_media_intent(
    query: str,
) -> None:
    assert _search(query) == ()


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
