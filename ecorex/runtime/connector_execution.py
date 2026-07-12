"""Batch-bound progressive disclosure for model-invoked Connector actions.

The WebUI connector catalog is intentionally not an invocation authority.  This
module projects one immutable Runtime snapshot into a bounded Search -> Describe
-> Call protocol and rechecks mutable connector state immediately before an
adapter can observe inputs.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from collections import deque
import hashlib
import json
import re
from typing import Any

from ecorex.capabilities import ToolExecutionScope, ToolInvocationContext
from ecorex.capabilities.schema import (
    SchemaInstanceError,
    canonical_json_value,
    validate_schema_instance,
)
from ecorex.connectors import (
    ConnectorCatalogItem,
    ConnectorEffect,
    ConnectorHealth,
    ConnectorInputInvalid,
    ConnectorPermissionDenied,
    ConnectorRegistry,
    ConnectorService,
    ConnectorUnavailable,
)

from .snapshots import RuntimeSnapshot
from .tool_executions import ToolExecutionRecord, ToolExecutionRepository


_MAX_CONNECTOR_RESULT_BYTES = 512 * 1024
_DISCOVERY_ID_RE = re.compile(
    r"^connector:(?P<instance>[A-Za-z0-9_.:-]{1,256})@"
    r"(?P<connector>[a-z][a-z0-9_.-]{1,127})/"
    r"(?P<action>[a-z][a-z0-9_.-]{1,127})@"
    r"(?P<contract>[0-9a-f]{64})$"
)


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def connector_catalog_snapshot_payload(
    registry: ConnectorRegistry,
    catalog: tuple[ConnectorCatalogItem, ...] | None,
) -> dict[str, Any]:
    """Return the secret-free canonical catalog captured for a Turn.

    Instance projections never contain ``credential_ref`` or provider tokens.
    The complete action schemas stay local in the immutable snapshot; Search
    exposes only summaries and Describe reveals one exact action contract.
    """

    by_id = {
        item.definition.connector_id: item
        for item in (catalog or ())
    }
    items: list[dict[str, Any]] = []
    for definition in registry.definitions():
        item = by_id.get(definition.connector_id)
        if item is None:
            items.append(
                {
                    "definition": definition.to_dict(),
                    "adapter_available": registry.has_adapter(definition.connector_id),
                    "instances": [],
                    "unavailable_reason": (
                        None
                        if registry.has_adapter(definition.connector_id)
                        else "adapter_not_installed"
                    ),
                }
            )
        else:
            items.append(item.to_dict())
    return {
        "schema_version": 1,
        "connector_contract_version": "1.0",
        "agent_disclosure_policy": {
            "policy_id": "ecorex.connector-progressive-disclosure",
            "policy_version": "1.0.0",
            "fairness": "connector-reserved-then-round-robin",
            "max_results": 50,
        },
        "items": items,
    }


class ConnectorAgentRuntime:
    """Trusted handlers for the generic Connector meta-tools."""

    def __init__(
        self,
        service: ConnectorService,
        *,
        tool_executions: ToolExecutionRepository,
        snapshot_resolver: Callable[[ToolExecutionScope], RuntimeSnapshot],
        turn_intent_resolver: Callable[[ToolExecutionScope], str],
        admin_hard_denies_provider: Callable[[], frozenset[str]],
        frozen_admin_hard_denies_resolver: Callable[[str], frozenset[str]],
    ) -> None:
        self.service = service
        self.tool_executions = tool_executions
        self.snapshot_resolver = snapshot_resolver
        self.turn_intent_resolver = turn_intent_resolver
        self.admin_hard_denies_provider = admin_hard_denies_provider
        self.frozen_admin_hard_denies_resolver = frozen_admin_hard_denies_resolver

    def handlers(self) -> dict[str, Callable[..., Any]]:
        return {
            "connector_search": self.search,
            "connector_describe": self.describe,
            "connector_read": self.read,
            "connector_write": self.write,
        }

    def search(
        self,
        arguments: Mapping[str, Any],
        context: ToolInvocationContext,
    ) -> dict[str, Any]:
        scope = self._scope(context)
        snapshot = self._snapshot(scope)
        return self._search_projection(
            snapshot,
            arguments,
            turn_intent=self.turn_intent_resolver(scope),
            hard_denies=self.frozen_admin_hard_denies_resolver(
                context.policy_snapshot_id
            ),
        )

    def describe(
        self,
        arguments: Mapping[str, Any],
        context: ToolInvocationContext,
    ) -> dict[str, Any]:
        scope = self._scope(context)
        snapshot = self._snapshot(scope)
        discovery_id = arguments.get("discovery_id")
        if self.parse_discovery_id(discovery_id) is None:
            return self._not_found(snapshot, "invalid_discovery_id")
        search = self.tool_executions.completed_connector_search_for_discovery(
            execution_scope=scope,
            capability_snapshot_id=context.capability_snapshot_id,
            policy_snapshot_id=context.policy_snapshot_id,
            connector_catalog_snapshot_id=snapshot.snapshot_id,
            discovery_id=str(discovery_id),
        )
        if search is None or search.result_sha256 is None:
            return self._not_found(snapshot, "not_discovered_in_batch")
        expected_search = self._search_projection(
            snapshot,
            search.arguments,
            turn_intent=self.turn_intent_resolver(scope),
            hard_denies=self.frozen_admin_hard_denies_resolver(
                context.policy_snapshot_id
            ),
        )
        if expected_search != search.result or _sha256(search.result) != search.result_sha256:
            return self._not_found(snapshot, "search_fact_invalid")
        candidate = next(
            (
                item
                for item in expected_search["actions"]
                if item.get("discovery_id") == discovery_id
            ),
            None,
        )
        if not isinstance(candidate, dict):
            return self._not_found(snapshot, "not_discovered_in_batch")
        action = self._action_description(snapshot, candidate)
        return {
            "schema_version": 1,
            "connector_catalog_snapshot_id": snapshot.snapshot_id,
            "found": True,
            "available": True,
            "reason": "available",
            "discovery_id": discovery_id,
            "search_tool_call_id": search.tool_call_id,
            "search_result_sha256": search.result_sha256,
            "action": action,
        }

    async def read(
        self,
        arguments: Mapping[str, Any],
        context: ToolInvocationContext,
    ) -> Any:
        return await self._call(arguments, context, expected_tool_id="connector_read")

    async def write(
        self,
        arguments: Mapping[str, Any],
        context: ToolInvocationContext,
    ) -> Any:
        return await self._call(arguments, context, expected_tool_id="connector_write")

    async def _call(
        self,
        arguments: Mapping[str, Any],
        context: ToolInvocationContext,
        *,
        expected_tool_id: str,
    ) -> Any:
        if context.tool_id != expected_tool_id:
            raise ConnectorPermissionDenied("connector call endpoint is inconsistent")
        scope = self._scope(context)
        snapshot = self._snapshot(scope)
        discovery_id = arguments.get("discovery_id")
        parsed = self.parse_discovery_id(discovery_id)
        if parsed is None:
            raise ConnectorPermissionDenied(
                "connector action was not disclosed for this execution"
            )
        grant = self.tool_executions.completed_connector_describe_for_discovery(
            execution_scope=scope,
            capability_snapshot_id=context.capability_snapshot_id,
            policy_snapshot_id=context.policy_snapshot_id,
            connector_catalog_snapshot_id=snapshot.snapshot_id,
            discovery_id=str(discovery_id),
            call_tool_id=expected_tool_id,
        )
        if grant is None:
            raise ConnectorPermissionDenied(
                "connector action was not disclosed for this execution"
            )
        describe_record, search_record = grant
        if search_record.result_sha256 is None:
            raise ConnectorPermissionDenied("connector search fact is invalid")
        expected_search = self._search_projection(
            snapshot,
            search_record.arguments,
            turn_intent=self.turn_intent_resolver(scope),
            hard_denies=self.frozen_admin_hard_denies_resolver(
                context.policy_snapshot_id
            ),
        )
        if (
            expected_search != search_record.result
            or _sha256(search_record.result) != search_record.result_sha256
        ):
            raise ConnectorPermissionDenied("connector search fact is invalid")
        candidate = next(
            (
                item
                for item in expected_search["actions"]
                if item.get("discovery_id") == discovery_id
            ),
            None,
        )
        if not isinstance(candidate, dict):
            raise ConnectorPermissionDenied("connector action is outside the frozen catalog")
        expected_describe = {
            "schema_version": 1,
            "connector_catalog_snapshot_id": snapshot.snapshot_id,
            "found": True,
            "available": True,
            "reason": "available",
            "discovery_id": discovery_id,
            "search_tool_call_id": search_record.tool_call_id,
            "search_result_sha256": search_record.result_sha256,
            "action": self._action_description(snapshot, candidate),
        }
        if describe_record.result != expected_describe:
            raise ConnectorPermissionDenied("connector describe grant is invalid")
        description = expected_describe["action"]
        if description["call_tool_id"] != expected_tool_id:
            raise ConnectorPermissionDenied("connector grant cannot change action effects")

        instance_id, connector_id, action_id, contract_sha256 = parsed
        current_instance = self.service.repository.get_instance(instance_id)
        if (
            current_instance is None
            or not current_instance.enabled
            or current_instance.connector_id != connector_id
        ):
            raise ConnectorUnavailable("connector account is disconnected")
        if current_instance.health not in {
            ConnectorHealth.CONNECTED,
            ConnectorHealth.DEGRADED,
        }:
            raise ConnectorUnavailable("connector account requires reauthorization")
        if not self.service.registry.has_adapter(connector_id):
            raise ConnectorUnavailable("connector adapter is unavailable")
        try:
            current_action = self.service.registry.definition(connector_id).action(action_id)
        except KeyError:
            raise ConnectorUnavailable("connector action contract changed") from None
        if _sha256(current_action.to_dict()) != contract_sha256:
            raise ConnectorUnavailable("connector action contract changed")
        if current_action.required_scopes - current_instance.granted_scopes:
            raise ConnectorPermissionDenied("connector account scope is no longer sufficient")
        hard_denies = frozenset(
            str(value).casefold() for value in self.admin_hard_denies_provider()
        )
        if (
            connector_id.casefold() in hard_denies
            or action_id.casefold() in hard_denies
        ):
            raise ConnectorPermissionDenied(
                "connector action is blocked by administrator policy"
            )
        expected_current_tool = self._call_tool_id(
            [effect.value for effect in current_action.effects]
        )
        if expected_current_tool != expected_tool_id:
            raise ConnectorPermissionDenied("connector action effects changed")

        raw_input = arguments.get("input")
        try:
            canonical_input = canonical_json_value(
                raw_input,
                label="connector dynamic action input",
            )
            validate_schema_instance(
                canonical_input,
                description["input_schema"],
                label="connector dynamic action input",
            )
        except (SchemaInstanceError, TypeError, ValueError):
            raise ConnectorInputInvalid("connector action input is invalid") from None
        if not isinstance(canonical_input, dict):
            raise ConnectorInputInvalid("connector action input is invalid")

        result = await self.service.invoke(
            instance_id,
            action_id,
            canonical_input,
            # Model-originated reads and writes both need an exact replay key:
            # after the provider returns, publication may be recovered locally
            # from Connector result staging without a second provider dispatch.
            idempotency_key=self._stable_idempotency_key(context.idempotency_key),
            admin_hard_denies=hard_denies,
            admin_hard_denies_provider=self.admin_hard_denies_provider,
            runtime_context={
                "job_id": scope.job_id,
                "thread_id": scope.thread_id,
                "turn_id": scope.turn_id,
                "execution_batch_id": scope.execution_batch_id,
                "tool_call_id": context.tool_call_id,
                "capability_snapshot_id": context.capability_snapshot_id,
                "permission_snapshot_id": context.policy_snapshot_id,
                "connector_catalog_snapshot_id": snapshot.snapshot_id,
                "discovery_id": discovery_id,
            },
            max_result_bytes=_MAX_CONNECTOR_RESULT_BYTES,
        )
        return result

    def _search_projection(
        self,
        snapshot: RuntimeSnapshot,
        arguments: Mapping[str, Any],
        *,
        turn_intent: str,
        hard_denies: frozenset[str],
    ) -> dict[str, Any]:
        query = str(arguments.get("query", ""))
        limit = int(arguments.get("limit", 20))
        if not query.strip() or not 1 <= limit <= 50:
            raise ConnectorInputInvalid("connector search request is invalid")
        query_terms = tuple(
            term for term in re.split(r"\s+", query.casefold().strip()) if term
        )
        intent = turn_intent.casefold()
        actions: list[dict[str, Any]] = []
        waiting: list[dict[str, Any]] = []
        for item in self._items(snapshot):
            definition = item["definition"]
            connector_id = str(definition["connector_id"])
            connector_name = str(definition["display_name"])
            connector_text = " ".join(
                (
                    connector_id,
                    connector_name,
                    str(definition.get("description", "")),
                )
            ).casefold()
            if connector_id.casefold() in hard_denies:
                continue
            explicit_boost = 120 if (
                connector_id.casefold() in intent or connector_name.casefold() in intent
            ) else 0
            connector_match = sum(30 for term in query_terms if term in connector_text)
            available_count = 0
            action_specs = {
                str(action["action_id"]): action
                for action in definition.get("actions", [])
                if isinstance(action, dict) and isinstance(action.get("action_id"), str)
            }
            specifically_requested_actions = sorted(
                action_id
                for action_id, action in action_specs.items()
                if action_id.casefold() not in hard_denies
                and self._action_requested(
                    query,
                    turn_intent=turn_intent,
                    action_id=action_id,
                    action=action,
                    connector_id=connector_id,
                    connector_name=connector_name,
                )
            )
            required_action_ids = specifically_requested_actions[:32]
            available_action_ids: set[str] = set()
            for instance in item.get("instances", []):
                if not isinstance(instance, dict):
                    continue
                if instance.get("health") not in {"connected", "degraded"}:
                    continue
                available_actions = instance.get("available_actions")
                if not isinstance(available_actions, list):
                    continue
                for action_id in available_actions:
                    if str(action_id).casefold() in hard_denies:
                        continue
                    action = action_specs.get(str(action_id))
                    if action is None:
                        continue
                    available_action_ids.add(str(action_id))
                    effects = action.get("effects")
                    if not isinstance(effects, list) or not effects:
                        continue
                    call_tool_id = self._call_tool_id(effects)
                    action_text = " ".join(
                        (
                            str(action_id),
                            str(action.get("display_name", "")),
                            str(action.get("description", "")),
                        )
                    ).casefold()
                    instance_text = " ".join(
                        (
                            str(instance.get("instance_id", "")),
                            str(instance.get("account_display_name", "")),
                        )
                    ).casefold()
                    action_match = sum(50 for term in query_terms if term in action_text)
                    instance_match = sum(
                        70 for term in query_terms if term in instance_text
                    )
                    exact = 200 if query.casefold().strip() in {
                        str(action_id).casefold(),
                        connector_id.casefold(),
                        connector_name.casefold(),
                    } else 0
                    score = (
                        exact
                        + connector_match
                        + action_match
                        + instance_match
                        + explicit_boost
                    )
                    if score <= 0:
                        continue
                    contract_sha256 = _sha256(action)
                    instance_id = str(instance.get("instance_id", ""))
                    if _DISCOVERY_ID_RE.fullmatch(
                        f"connector:{instance_id}@{connector_id}/{action_id}@{contract_sha256}"
                    ) is None:
                        continue
                    available_count += 1
                    actions.append(
                        {
                            "discovery_id": (
                                f"connector:{instance_id}@{connector_id}/"
                                f"{action_id}@{contract_sha256}"
                            ),
                            "connector_id": connector_id,
                            "connector_name": connector_name,
                            "instance_id": instance_id,
                            "account_name": str(instance.get("account_display_name", "账号")),
                            "action_id": str(action_id),
                            "action_name": str(action.get("display_name", action_id)),
                            "description": str(action.get("description", action_id)),
                            "effects": sorted(str(effect) for effect in effects),
                            "call_tool_id": call_tool_id,
                            "score": score,
                        }
                    )
            missing_required = bool(required_action_ids) and not set(
                required_action_ids
            ).issubset(available_action_ids)
            if (
                (available_count == 0 or missing_required)
                and (connector_match > 0 or explicit_boost > 0)
            ):
                instances = [value for value in item.get("instances", []) if isinstance(value, dict)]
                if item.get("adapter_available") is not True:
                    reason = "adapter_not_installed"
                elif not instances:
                    reason = "login_required"
                elif missing_required:
                    reason = "reauthorization_required"
                elif any(
                    value.get("health") in {"error", "unconfigured"}
                    for value in instances
                ):
                    reason = "reauthorization_required"
                else:
                    reason = "connector_unavailable"
                waiting.append(
                    {
                        "kind": "connector_login",
                        "connector_id": connector_id,
                        "connector_name": connector_name,
                        "reason": reason,
                        "required_action_ids": required_action_ids,
                    }
                )
        actions.sort(
            key=lambda item: (
                -int(item["score"]),
                str(item["connector_id"]),
                str(item["instance_id"]),
                str(item["action_id"]),
            )
        )
        actions = self._fair_actions(actions, limit=limit)
        waiting.sort(key=lambda item: (str(item["connector_id"]), str(item["reason"])))
        result: dict[str, Any] = {
            "schema_version": 1,
            "connector_catalog_snapshot_id": snapshot.snapshot_id,
            "connector_catalog_sha256": snapshot.payload_sha256,
            "query": query,
            "actions": actions,
            "waiting": waiting[:limit],
        }
        explicitly_requested = next(
            (
                item
                for item in waiting
                if self._explicit_connector_request(
                    turn_intent,
                    connector_id=str(item["connector_id"]),
                    connector_name=str(item["connector_name"]),
                )
            ),
            None,
        )
        if explicitly_requested is not None:
            state = (
                "reauthorization_required"
                if explicitly_requested["reason"] == "reauthorization_required"
                else "authorization_required"
            )
            result["_ecorex_interaction"] = {
                "schema_version": 1,
                "kind": "connector_login",
                "prompt": (
                    f"需要先连接{explicitly_requested['connector_name']}，"
                    "登录完成后才能继续执行这项操作。"
                ),
                "contract": {
                    "schema_version": 1,
                    "title": f"连接{explicitly_requested['connector_name']}",
                    "fields": [],
                    "actions": [
                        {
                            "action_id": "begin_login",
                            "label": "开始连接",
                            "action_type": "connector_begin_login",
                            "style": "primary",
                            "submits_form": False,
                        },
                        {
                            "action_id": "check_status",
                            "label": "检查连接状态",
                            "action_type": "connector_check_status",
                            "style": "secondary",
                            "submits_form": False,
                        },
                        {
                            "action_id": "cancel",
                            "label": "取消",
                            "action_type": "cancel",
                            "style": "secondary",
                            "submits_form": False,
                        },
                    ],
                    "connector": {
                        "connector_id": explicitly_requested["connector_id"],
                        "display_name": explicitly_requested["connector_name"],
                        "state": state,
                        "required_action_ids": explicitly_requested[
                            "required_action_ids"
                        ],
                    },
                },
            }
        return result

    @staticmethod
    def _action_requested(
        query: str,
        *,
        turn_intent: str,
        action_id: str,
        action: Mapping[str, Any],
        connector_id: str,
        connector_name: str,
    ) -> bool:
        """Match explicit action intent without whitespace-dependent CJK tokens."""

        normalized_query = re.sub(r"[\s_./:-]+", "", query.casefold())
        normalized_intent = re.sub(r"[\s_./:-]+", "", turn_intent.casefold())
        normalized_id = re.sub(r"[\s_./:-]+", "", action_id.casefold())
        display = str(action.get("display_name", "")).casefold()
        description = str(action.get("description", "")).casefold()
        phrases = {
            re.sub(r"[\s_./:-]+", "", value)
            for value in (
                action_id,
                display,
                description,
                *tuple(str(value) for value in action.get("intent_aliases", [])),
            )
            if value
        }
        for connector in (connector_id.casefold(), connector_name.casefold()):
            compact_connector = re.sub(r"[\s_./:-]+", "", connector)
            phrases.update(
                phrase.replace(compact_connector, "")
                for phrase in tuple(phrases)
                if compact_connector
            )
        negations = ("不要", "不用", "无需", "别", "without", "donot")

        def matched(text: str) -> bool:
            for phrase in phrases:
                if len(phrase) < 2:
                    continue
                offset = text.find(phrase)
                if offset < 0:
                    continue
                prefix = text[max(0, offset - 8) : offset]
                if not any(prefix.endswith(value) for value in negations):
                    return True
            return False

        if normalized_id and normalized_id in normalized_intent:
            return True
        if matched(normalized_intent):
            return True
        return matched(normalized_query)

    def _action_description(
        self,
        snapshot: RuntimeSnapshot,
        candidate: Mapping[str, Any],
    ) -> dict[str, Any]:
        parsed = self.parse_discovery_id(candidate.get("discovery_id"))
        if parsed is None:
            raise ConnectorPermissionDenied("connector discovery identity is invalid")
        instance_id, connector_id, action_id, contract_sha256 = parsed
        for item in self._items(snapshot):
            definition = item["definition"]
            if definition.get("connector_id") != connector_id:
                continue
            instance = next(
                (
                    value
                    for value in item.get("instances", [])
                    if isinstance(value, dict) and value.get("instance_id") == instance_id
                ),
                None,
            )
            action = next(
                (
                    value
                    for value in definition.get("actions", [])
                    if isinstance(value, dict) and value.get("action_id") == action_id
                ),
                None,
            )
            if not isinstance(instance, dict) or not isinstance(action, dict):
                break
            if _sha256(action) != contract_sha256:
                break
            return {
                "connector_id": connector_id,
                "connector_name": str(definition["display_name"]),
                "instance_id": instance_id,
                "account_name": str(instance.get("account_display_name", "账号")),
                "action_id": action_id,
                "action_name": str(action["display_name"]),
                "description": str(action["description"]),
                "contract_version": str(definition["contract_version"]),
                "action_contract_sha256": contract_sha256,
                "effects": sorted(str(value) for value in action["effects"]),
                "requires_idempotency_key": bool(
                    action.get("requires_idempotency_key", False)
                ),
                "call_tool_id": str(candidate["call_tool_id"]),
                "input_schema": dict(action["input_schema"]),
                "output_schema": dict(action["output_schema"]),
                "result_envelope_version": 1,
            }
        raise ConnectorPermissionDenied("connector action is outside the frozen catalog")

    @staticmethod
    def _call_tool_id(effects: list[object]) -> str:
        normalized = frozenset(str(value) for value in effects)
        if normalized == {"read"}:
            return "connector_read"
        if normalized & {"write", "subscribe"}:
            return "connector_write"
        raise ConnectorPermissionDenied("connector action effects are unsupported")

    @staticmethod
    def _explicit_connector_request(
        intent: str,
        *,
        connector_id: str,
        connector_name: str,
    ) -> bool:
        normalized = intent.casefold()
        if connector_id.casefold() not in normalized and connector_name.casefold() not in normalized:
            return False
        if any(
            phrase in normalized
            for phrase in (
                "不要用",
                "不要使用",
                "别用",
                "不使用",
                "不用",
                "无需",
                "不连接",
                "禁止使用",
                "do not use",
                "don't use",
                "without",
                "without using",
            )
        ):
            return False
        return any(
            phrase in normalized
            for phrase in (
                "用",
                "使用",
                "连接",
                "读取",
                "搜索",
                "编辑",
                "写入",
                "发送",
                "创建",
                "use",
                "connect",
                "read",
                "search",
                "edit",
                "write",
                "send",
                "create",
            )
        )

    @staticmethod
    def _fair_actions(
        actions: list[dict[str, Any]],
        *,
        limit: int,
    ) -> list[dict[str, Any]]:
        """Reserve one slot per matching connector, then round-robin accounts."""

        by_connector: dict[str, dict[str, list[dict[str, Any]]]] = {}
        connector_rank: dict[str, tuple[int, str]] = {}
        for action in actions:
            connector_id = str(action["connector_id"])
            instance_id = str(action["instance_id"])
            by_connector.setdefault(connector_id, {}).setdefault(instance_id, []).append(action)
            rank = (-int(action["score"]), connector_id)
            connector_rank[connector_id] = min(
                connector_rank.get(connector_id, rank), rank
            )
        connector_order = sorted(by_connector, key=lambda value: connector_rank[value])
        connector_queues: dict[str, deque[dict[str, Any]]] = {}
        for connector_id in connector_order:
            instances = by_connector[connector_id]
            instance_order = sorted(
                instances,
                key=lambda value: (-int(instances[value][0]["score"]), value),
            )
            queue: deque[dict[str, Any]] = deque()
            cursors = {instance_id: 0 for instance_id in instance_order}
            while True:
                progressed = False
                for instance_id in instance_order:
                    index = cursors[instance_id]
                    bucket = instances[instance_id]
                    if index >= len(bucket):
                        continue
                    queue.append(bucket[index])
                    cursors[instance_id] = index + 1
                    progressed = True
                if not progressed:
                    break
            connector_queues[connector_id] = queue
        selected: list[dict[str, Any]] = []
        while len(selected) < limit:
            progressed = False
            for connector_id in connector_order:
                queue = connector_queues[connector_id]
                if not queue:
                    continue
                selected.append(queue.popleft())
                progressed = True
                if len(selected) >= limit:
                    break
            if not progressed:
                break
        return selected

    @staticmethod
    def parse_discovery_id(value: object) -> tuple[str, str, str, str] | None:
        if not isinstance(value, str) or len(value) > 512:
            return None
        match = _DISCOVERY_ID_RE.fullmatch(value)
        if match is None:
            return None
        return (
            match.group("instance"),
            match.group("connector"),
            match.group("action"),
            match.group("contract"),
        )

    @staticmethod
    def _stable_idempotency_key(value: str | None) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ConnectorPermissionDenied(
                "connector write has no durable idempotency identity"
            )
        return "ecorex-connector-" + hashlib.sha256(value.encode("utf-8")).hexdigest()

    @staticmethod
    def _scope(context: ToolInvocationContext) -> ToolExecutionScope:
        scope = context.execution_scope
        if (
            not isinstance(scope, ToolExecutionScope)
            or not isinstance(scope.execution_batch_id, str)
            or not scope.execution_batch_id
        ):
            raise ConnectorPermissionDenied("connector execution has no durable batch")
        return scope

    def _snapshot(self, scope: ToolExecutionScope) -> RuntimeSnapshot:
        snapshot = self.snapshot_resolver(scope)
        if snapshot.kind != "connectors":
            raise ConnectorPermissionDenied("connector catalog snapshot is invalid")
        self._items(snapshot)
        return snapshot

    @staticmethod
    def _items(snapshot: RuntimeSnapshot) -> list[dict[str, Any]]:
        payload = snapshot.payload
        items = payload.get("items") if isinstance(payload, Mapping) else None
        if (
            payload.get("schema_version") != 1
            or payload.get("connector_contract_version") != "1.0"
            or not isinstance(items, list)
            or len(items) > 256
            or any(not isinstance(item, dict) for item in items)
        ):
            raise ConnectorPermissionDenied("connector catalog snapshot is invalid")
        return items

    @staticmethod
    def _not_found(snapshot: RuntimeSnapshot, reason: str) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "connector_catalog_snapshot_id": snapshot.snapshot_id,
            "found": False,
            "available": False,
            "reason": reason,
        }


__all__ = [
    "ConnectorAgentRuntime",
    "connector_catalog_snapshot_payload",
]
