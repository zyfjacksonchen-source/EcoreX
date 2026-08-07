"""Fail-closed public projection for internal Tool executions.

This module never redacts or recursively walks arbitrary values.  A reviewed
Core policy chooses fixed user-facing summaries and exact Artifact identity
locations.  Unknown Core tools and every third-party/MCP tool receive only a
generic action label, lifecycle state and canonical digests.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import re
from typing import Any, Literal, Mapping

from ecorex.capabilities import (
    CapabilityEffect,
    SandboxLevel,
    ToolProviderKind,
    ToolSpec,
)
from ecorex.protocol import ItemStatus, PublicArtifactRef, PublicToolActivity

from .database import json_dumps


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ARTIFACT_ID = re.compile(r"^[A-Za-z0-9][-A-Za-z0-9_.:]{0,127}$")
_ArtifactMode = Literal[
    "none",
    "root_pair",
    "nested_artifact",
    "artifact_ids",
    "reference_artifact_ids",
]


@dataclass(frozen=True, slots=True)
class _CorePublicPolicy:
    argument_summary: str
    result_summary: str
    argument_artifacts: _ArtifactMode = "none"
    result_artifacts: _ArtifactMode = "none"


_CORE_POLICIES: dict[str, _CorePublicPolicy] = {
    "tool_search": _CorePublicPolicy("正在查找可用能力", "已完成能力查找"),
    "tool_describe": _CorePublicPolicy("正在查看能力说明", "已确认能力说明"),
    "task_list": _CorePublicPolicy("正在更新任务清单", "已更新任务清单"),
    "skill_search": _CorePublicPolicy("正在查找办公技能", "已完成技能查找"),
    "skill_read": _CorePublicPolicy("正在读取技能说明", "已读取技能说明"),
    "skill_run": _CorePublicPolicy("正在运行技能", "已完成技能运行"),
    "connector_search": _CorePublicPolicy(
        "正在查找可用连接器操作", "已完成连接器操作查找"
    ),
    "connector_describe": _CorePublicPolicy(
        "正在确认连接器操作", "已确认连接器操作"
    ),
    "connector_read": _CorePublicPolicy(
        "正在从已连接的应用读取信息",
        "已完成连接器读取",
        result_artifacts="nested_artifact",
    ),
    "connector_write": _CorePublicPolicy(
        "正在向已连接的应用提交操作",
        "已完成连接器操作",
        result_artifacts="nested_artifact",
    ),
    "artifact_read": _CorePublicPolicy(
        "正在读取办公产物",
        "已读取办公产物",
        argument_artifacts="root_pair",
        result_artifacts="root_pair",
    ),
    "input_attachment_read": _CorePublicPolicy(
        "正在读取本条消息的附件", "已读取消息附件"
    ),
    "read": _CorePublicPolicy("正在读取工作资料", "已读取工作资料"),
    "fetch": _CorePublicPolicy("正在查找在线资料", "已获取在线资料"),
    "vision": _CorePublicPolicy(
        "正在查看图片内容",
        "已完成图片检查",
        argument_artifacts="artifact_ids",
    ),
    "ocr": _CorePublicPolicy("正在识别图片文字", "已完成图片文字识别"),
    "cdp": _CorePublicPolicy("正在浏览网页", "已完成网页操作"),
    "shell": _CorePublicPolicy("正在执行已批准的命令", "命令执行已完成"),
    "imagegen": _CorePublicPolicy(
        "正在生成或修改图片",
        "图片已生成并保存",
        argument_artifacts="reference_artifact_ids",
        result_artifacts="root_pair",
    ),
}


class PublicToolProjectionError(ValueError):
    """Internal Tool data failed the public projection contract."""


def _digest(value: Any) -> str:
    try:
        encoded = json_dumps(value).encode("utf-8")
    except (TypeError, ValueError):
        raise PublicToolProjectionError(
            "Tool value is not canonical JSON"
        ) from None
    return hashlib.sha256(encoded).hexdigest()


def _risk(spec: ToolSpec) -> Literal["low", "medium", "high"]:
    if (
        spec.required_sandbox is SandboxLevel.DANGER_FULL_ACCESS
        or spec.effects
        & {
            CapabilityEffect.WRITE,
            CapabilityEffect.EXECUTE,
            CapabilityEffect.UI_AUTOMATION,
        }
    ):
        return "high"
    if spec.effects & {
        CapabilityEffect.NETWORK,
        CapabilityEffect.GENERATE_MEDIA,
    }:
        return "medium"
    return "low"


def _artifact_ref(value: Any) -> PublicArtifactRef | None:
    if not isinstance(value, Mapping):
        return None
    artifact_id = value.get("artifact_id")
    revision_id = value.get("revision_id")
    if (
        not isinstance(artifact_id, str)
        or _ARTIFACT_ID.fullmatch(artifact_id) is None
        or (
            revision_id is not None
            and (
                not isinstance(revision_id, str)
                or _ARTIFACT_ID.fullmatch(revision_id) is None
            )
        )
    ):
        return None
    return PublicArtifactRef(
        artifact_id=artifact_id,
        revision_id=revision_id,
    )


def _artifact_refs(value: Any, mode: _ArtifactMode) -> list[PublicArtifactRef]:
    if mode == "none" or not isinstance(value, Mapping):
        return []
    if mode == "root_pair":
        reference = _artifact_ref(value)
        return [] if reference is None else [reference]
    if mode == "nested_artifact":
        reference = _artifact_ref(value.get("artifact"))
        return [] if reference is None else [reference]
    field = (
        "artifact_ids" if mode == "artifact_ids" else "reference_artifact_ids"
    )
    raw = value.get(field)
    if not isinstance(raw, (list, tuple)):
        return []
    result: list[PublicArtifactRef] = []
    for artifact_id in raw[:20]:
        if (
            isinstance(artifact_id, str)
            and _ARTIFACT_ID.fullmatch(artifact_id) is not None
        ):
            result.append(PublicArtifactRef(artifact_id=artifact_id))
    return result


def _merge_refs(
    *groups: list[PublicArtifactRef],
) -> list[PublicArtifactRef]:
    result: list[PublicArtifactRef] = []
    seen: set[tuple[str, str | None]] = set()
    for reference in (item for group in groups for item in group):
        identity = (reference.artifact_id, reference.revision_id)
        if identity in seen:
            continue
        seen.add(identity)
        result.append(reference)
        if len(result) == 20:
            break
    return result


class PublicToolActivityProjector:
    """Build deterministic public facts without copying arbitrary Tool data."""

    @staticmethod
    def _policy(spec: ToolSpec) -> _CorePublicPolicy | None:
        if spec.provider.kind is not ToolProviderKind.CORE:
            return None
        return _CORE_POLICIES.get(spec.tool_id)

    def requested(
        self,
        spec: ToolSpec,
        *,
        tool_call_id: str,
        arguments: Mapping[str, Any],
    ) -> PublicToolActivity:
        policy = self._policy(spec)
        return PublicToolActivity(
            tool_call_id=tool_call_id,
            tool_id=spec.tool_id,
            tool_name=spec.tool_id,
            display_label=(
                spec.display_name if policy is not None else "使用已连接的应用"
            ),
            phase="requested",
            status="in_progress",
            effects=sorted(effect.value for effect in spec.effects),
            risk=_risk(spec),
            argument_summary=(
                policy.argument_summary
                if policy is not None
                else "正在使用已连接的应用"
            ),
            argument_sha256=_digest(arguments),
            artifact_refs=(
                []
                if policy is None
                else _artifact_refs(arguments, policy.argument_artifacts)
            ),
        )

    def completed(
        self,
        spec: ToolSpec,
        *,
        tool_call_id: str,
        arguments: Mapping[str, Any],
        result: Any,
        execution_status: str = "completed",
    ) -> PublicToolActivity:
        requested = self.requested(
            spec,
            tool_call_id=tool_call_id,
            arguments=arguments,
        )
        policy = self._policy(spec)
        skipped = execution_status == "skipped"
        return requested.model_copy(
            update={
                "phase": "completed",
                "status": "completed",
                "result_summary": (
                    "已跳过此操作"
                    if skipped
                    else policy.result_summary
                    if policy is not None
                    else "已完成应用操作"
                ),
                "result_sha256": _digest(result),
                "artifact_refs": _merge_refs(
                    requested.artifact_refs,
                    (
                        []
                        if policy is None
                        else _artifact_refs(result, policy.result_artifacts)
                    ),
                ),
            }
        )

    @staticmethod
    def transition(
        activity: PublicToolActivity,
        target: ItemStatus,
    ) -> PublicToolActivity:
        state = {
            ItemStatus.CREATED: ("requested", "created", None),
            ItemStatus.IN_PROGRESS: ("running", "in_progress", None),
            ItemStatus.WAITING_HUMAN: (
                "waiting_human",
                "waiting_human",
                "等待你确认后继续",
            ),
            ItemStatus.COMPLETED: (
                "completed",
                "completed",
                activity.result_summary or "此步骤已完成",
            ),
            ItemStatus.FAILED: ("failed", "failed", "此步骤未完成"),
            ItemStatus.CANCELLED: ("cancelled", "cancelled", "此步骤已取消"),
        }.get(target)
        if state is None:
            raise PublicToolProjectionError(
                "Tool Item target has no public lifecycle state"
            )
        phase, status, summary = state
        return activity.model_copy(
            update={
                "phase": phase,
                "status": status,
                "result_summary": summary,
            }
        )

    @staticmethod
    def recovered_connector(
        *,
        tool_call_id: str,
        tool_id: Literal["connector_read", "connector_write"],
        argument_sha256: str,
        result_envelope: Mapping[str, Any],
    ) -> PublicToolActivity:
        if _SHA256.fullmatch(argument_sha256) is None:
            raise PublicToolProjectionError(
                "Recovered Connector argument digest is invalid"
            )
        is_write = tool_id == "connector_write"
        return PublicToolActivity(
            tool_call_id=tool_call_id,
            tool_id=tool_id,
            tool_name=tool_id,
            display_label=(
                "执行连接器写入操作" if is_write else "执行连接器只读操作"
            ),
            phase="completed",
            status="completed",
            effects=(
                ["network", "write"] if is_write else ["network", "read"]
            ),
            risk="high" if is_write else "medium",
            argument_summary=(
                "正在向已连接的应用提交操作"
                if is_write
                else "正在从已连接的应用读取信息"
            ),
            result_summary="连接器结果已恢复并可继续使用",
            argument_sha256=argument_sha256,
            result_sha256=_digest(result_envelope),
            artifact_refs=_artifact_refs(
                result_envelope,
                "nested_artifact",
            ),
        )


def validate_public_tool_event_payload(
    event_type: str,
    payload: Mapping[str, Any],
    *,
    tool_call_id: str | None = None,
) -> dict[str, Any]:
    """Validate the exact public body for Tool-related Event ingress."""

    if event_type == "item.created":
        if payload.get("kind") != "tool_call":
            return dict(payload)
        if set(payload) != {"kind", "status", "content"}:
            raise PublicToolProjectionError(
                "Tool Item creation requires an exact public payload"
            )
        content = payload.get("content")
        try:
            activity = PublicToolActivity.model_validate(content)
        except ValueError:
            raise PublicToolProjectionError(
                "Tool Item creation requires PublicToolActivity"
            ) from None
        if payload.get("status") != activity.status:
            raise PublicToolProjectionError(
                "Tool Item status differs from its public activity"
            )
        return {
            "kind": "tool_call",
            "status": payload.get("status"),
            "content": activity.model_dump(mode="json"),
        }
    if event_type not in {"tool.call_requested", "tool.result"}:
        return dict(payload)
    if set(payload) != {"activity"}:
        raise PublicToolProjectionError(
            "Tool Event requires an exact PublicToolActivity payload"
        )
    try:
        activity = PublicToolActivity.model_validate(payload.get("activity"))
    except ValueError:
        raise PublicToolProjectionError(
            "Tool Event PublicToolActivity is invalid"
        ) from None
    expected_phase = (
        {"requested", "running"}
        if event_type == "tool.call_requested"
        else {"completed", "failed", "cancelled"}
    )
    if activity.phase not in expected_phase:
        raise PublicToolProjectionError(
            "Tool Event PublicToolActivity phase is invalid"
        )
    if tool_call_id != activity.tool_call_id:
        raise PublicToolProjectionError(
            "Tool Event envelope identity differs from its public activity"
        )
    return {"activity": activity.model_dump(mode="json")}


__all__ = [
    "PublicToolActivityProjector",
    "PublicToolProjectionError",
    "validate_public_tool_event_payload",
]
