"""Trusted bridge from Connector outbox facts into the Runtime Event Store.

Connector persistence and the conversational Runtime share one SQLite database,
but they intentionally have different event streams.  This bridge is the only
place where a Connector fact may acquire conversational scope.  A caller-owned
``payload.runtime`` is therefore never copied through: it is parsed, matched to
the immutable Runtime execution facts, and projected onto the Event envelope.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
import sqlite3
from typing import Any, Mapping

from ecorex.connectors import ConnectorInvocationContext, ConnectorOutboxEvent
from ecorex.protocol import CreateThreadRequest, PublicToolActivity
from ecorex.runtime.kernel import RuntimeKernel


_RUNTIME_FIELDS = frozenset(
    {
        "job_id",
        "thread_id",
        "turn_id",
        "execution_batch_id",
        "tool_call_id",
        "capability_snapshot_id",
        "permission_snapshot_id",
        "connector_catalog_snapshot_id",
        "discovery_id",
    }
)
_SAFE_IDENTITY = re.compile(r"^[A-Za-z0-9][-A-Za-z0-9_.:]{0,255}$")
_SAFE_TOKEN = re.compile(r"^[A-Za-z0-9][-A-Za-z0-9_.:]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_DISCOVERY = re.compile(
    r"^connector:(?P<instance>[A-Za-z0-9][-A-Za-z0-9_.:]{0,255})@"
    r"(?P<connector>[A-Za-z0-9][-A-Za-z0-9_.:]{0,255})/"
    r"(?P<action>[A-Za-z0-9][-A-Za-z0-9_.:]{0,255})@"
    r"(?P<contract>[0-9a-f]{64})$"
)

_IDENTITY_FIELDS = frozenset(
    {"invocation_id", "instance_id", "connector_id", "action_id"}
)
_TOKEN_FIELDS = frozenset(
    {
        "status",
        "delivery",
        "resolution",
        "health",
        "error_code",
        "reason",
        "stage_status",
        "completion_path",
    }
)
_DIGEST_FIELDS = frozenset(
    {
        "input_sha256",
        "idempotency_key_sha256",
        "admission_policy_sha256",
        "result_envelope_sha256",
        "result_sha256",
    }
)
_MODEL_EVENT_PREFIX = "connector.invocation."
_CONNECTOR_TOOL_IDS = frozenset({"connector_read", "connector_write"})


class ConnectorEventScopeError(RuntimeError):
    """A Connector outbox fact attempted to claim an untrusted Runtime scope."""


@dataclass(frozen=True, slots=True)
class _RuntimeRoute:
    context: ConnectorInvocationContext
    item_id: str
    causation_id: str


class RuntimeConnectorEventSink:
    def __init__(self, kernel: RuntimeKernel, *, account_id: str) -> None:
        self.kernel = kernel
        self.account_id = account_id
        self._audit_thread_id: str | None = None

    def publish(self, event: ConnectorOutboxEvent) -> None:
        payload = self._payload(event)
        # Runtime authority validation and the user-thread Event append share
        # one SQLite write transaction.  This closes the validation/append
        # race without making direct Connector API events user-visible.
        with self.kernel.database.transaction() as connection:
            route = self._runtime_route(event, payload, connection=connection)
            if route is not None:
                safe_payload = self._safe_projection(event, payload, route)
                context = route.context
                # Do not pass trace/correlation/snapshot fields here.
                # EventStore binds them to the accepted Turn and rejects
                # snapshot drift transactionally.
                self.kernel.events.append_in_transaction(
                    connection,
                    thread_id=context.thread_id,
                    turn_id=context.turn_id,
                    item_id=route.item_id,
                    job_id=context.job_id,
                    tool_call_id=context.tool_call_id,
                    causation_id=route.causation_id,
                    event_type=event.event_type,
                    payload=safe_payload,
                    idempotency_key=f"connector:{event.event_id}",
                )
                return

        self.kernel.events.append(
            thread_id=self._audit_thread(),
            event_type=event.event_type,
            payload=self._safe_projection(event, payload, None),
            idempotency_key=f"connector:{event.event_id}",
        )

    @staticmethod
    def _payload(event: ConnectorOutboxEvent) -> dict[str, Any]:
        if not event.event_type.startswith("connector."):
            raise ConnectorEventScopeError("Connector outbox event type is invalid")
        if not _SAFE_IDENTITY.fullmatch(event.aggregate_id):
            raise ConnectorEventScopeError("Connector aggregate identity is invalid")
        if not isinstance(event.payload, Mapping):
            raise ConnectorEventScopeError("Connector outbox payload is invalid")
        return {str(key): value for key, value in event.payload.items()}

    def _runtime_route(
        self,
        event: ConnectorOutboxEvent,
        payload: Mapping[str, Any],
        *,
        connection: sqlite3.Connection,
    ) -> _RuntimeRoute | None:
        has_runtime = "runtime" in payload
        if has_runtime and not event.event_type.startswith(_MODEL_EVENT_PREFIX):
            raise ConnectorEventScopeError(
                "Only Connector invocation facts may carry Runtime scope"
            )

        current = self._parse_runtime(payload.get("runtime")) if has_runtime else None
        started, started_exists = self._started_runtime(
            connection, event.aggregate_id
        )
        if event.event_type == "connector.invocation.started" and current is None:
            raise ConnectorEventScopeError(
                "Connector invocation start is missing Runtime scope"
            )

        if current is None:
            if not event.event_type.startswith(_MODEL_EVENT_PREFIX):
                return None
            if not started_exists:
                # Direct Connector API invocations intentionally have no model
                # Runtime start fact and remain on the internal audit thread.
                return None
            if started is None:
                raise ConnectorEventScopeError(
                    "Connector invocation start has invalid Runtime scope"
                )
            current = started
        elif (
            started_exists
            and started is not None
            and event.event_type
            not in {"connector.invocation.started", "connector.invocation.replayed"}
            and current != started
        ):
            raise ConnectorEventScopeError(
                "Connector invocation Runtime scope changed after dispatch"
            )

        return self._validate_runtime_scope(
            event_type=event.event_type,
            aggregate_id=event.aggregate_id,
            payload=payload,
            context=current,
            connection=connection,
        )

    @staticmethod
    def _parse_runtime(value: Any) -> ConnectorInvocationContext:
        if not isinstance(value, Mapping) or set(value) != _RUNTIME_FIELDS:
            raise ConnectorEventScopeError("Connector Runtime scope shape is invalid")
        try:
            return ConnectorInvocationContext(
                **{key: value[key] for key in sorted(_RUNTIME_FIELDS)}
            )
        except (TypeError, ValueError):
            raise ConnectorEventScopeError(
                "Connector Runtime scope identity is invalid"
            ) from None

    def _started_runtime(
        self,
        connection: sqlite3.Connection,
        aggregate_id: str,
    ) -> tuple[ConnectorInvocationContext | None, bool]:
        row = connection.execute(
            "SELECT payload_json FROM connector_outbox "
            "WHERE aggregate_id=? "
            "AND event_type='connector.invocation.started' "
            "ORDER BY aggregate_seq LIMIT 1",
            (aggregate_id,),
        ).fetchone()
        if row is None:
            return None, False
        try:
            payload = json.loads(str(row["payload_json"]))
        except (TypeError, ValueError, json.JSONDecodeError):
            raise ConnectorEventScopeError(
                "Connector invocation start payload is invalid"
            ) from None
        if not isinstance(payload, dict) or "runtime" not in payload:
            return None, True
        return self._parse_runtime(payload["runtime"]), True

    def _validate_runtime_scope(
        self,
        *,
        event_type: str,
        aggregate_id: str,
        payload: Mapping[str, Any],
        context: ConnectorInvocationContext,
        connection: sqlite3.Connection,
    ) -> _RuntimeRoute:
        if payload.get("invocation_id") != aggregate_id:
            raise ConnectorEventScopeError(
                "Connector invocation aggregate identity is inconsistent"
            )
        discovery = _DISCOVERY.fullmatch(context.discovery_id)
        if discovery is None:
            raise ConnectorEventScopeError("Connector discovery identity is invalid")
        expected_identity = {
            "instance_id": discovery.group("instance"),
            "connector_id": discovery.group("connector"),
            "action_id": discovery.group("action"),
        }
        if any(payload.get(key) != value for key, value in expected_identity.items()):
            raise ConnectorEventScopeError(
                "Connector invocation does not match its discovery identity"
            )

        invocation = connection.execute(
            "SELECT invocation_id, instance_id, connector_id, action_id "
            "FROM connector_invocations WHERE invocation_id=?",
            (aggregate_id,),
        ).fetchone()
        accepted = connection.execute(
            "SELECT event_id "
            "FROM events WHERE thread_id=? AND turn_id=? "
            "AND event_type='turn.accepted' ORDER BY seq LIMIT 1",
            (context.thread_id, context.turn_id),
        ).fetchone()
        job = connection.execute(
            "SELECT thread_id, turn_id FROM jobs WHERE job_id=?",
            (context.job_id,),
        ).fetchone()
        batch = connection.execute(
            "SELECT batch.thread_id, batch.turn_id, "
            "batch.capability_snapshot_id, batch.permission_snapshot_id, "
            "config.kind AS config_kind, config.payload_json AS config_payload_json, "
            "config.payload_sha256 AS config_payload_sha256 "
            "FROM turn_execution_batches AS batch "
            "JOIN runtime_snapshots AS config "
            "ON config.snapshot_id=batch.config_snapshot_id "
            "WHERE batch.batch_id=?",
            (context.execution_batch_id,),
        ).fetchone()
        execution = connection.execute(
            "SELECT job_id, turn_id, execution_batch_id, "
            "capability_snapshot_id, policy_snapshot_id, tool_id, "
            "arguments_json FROM tool_executions WHERE tool_call_id=?",
            (context.tool_call_id,),
        ).fetchone()
        catalog = connection.execute(
            "SELECT kind FROM runtime_snapshots WHERE snapshot_id=?",
            (context.connector_catalog_snapshot_id,),
        ).fetchone()
        call_event = connection.execute(
            "SELECT event_id, item_id, payload_json FROM events "
            "WHERE thread_id=? AND turn_id=? AND tool_call_id=? "
            "AND event_type='tool.call_requested' ORDER BY seq LIMIT 1",
            (context.thread_id, context.turn_id, context.tool_call_id),
        ).fetchone()

        if invocation is None or any(
            str(invocation[key]) != value
            for key, value in {
                "invocation_id": aggregate_id,
                **expected_identity,
            }.items()
        ):
            raise ConnectorEventScopeError(
                "Connector invocation authority is unavailable"
            )
        if accepted is None:
            raise ConnectorEventScopeError("Accepted Turn authority is unavailable")
        if (
            job is None
            or job["thread_id"] != context.thread_id
            or job["turn_id"] != context.turn_id
        ):
            raise ConnectorEventScopeError("Connector Job scope is inconsistent")
        if (
            batch is None
            or batch["thread_id"] != context.thread_id
            or batch["turn_id"] != context.turn_id
            or batch["capability_snapshot_id"] != context.capability_snapshot_id
            or batch["permission_snapshot_id"] != context.permission_snapshot_id
        ):
            raise ConnectorEventScopeError(
                "Connector execution batch scope is inconsistent"
            )
        config_payload_json = str(batch["config_payload_json"])
        if (
            batch["config_kind"] != "config"
            or hashlib.sha256(config_payload_json.encode("utf-8")).hexdigest()
            != batch["config_payload_sha256"]
        ):
            raise ConnectorEventScopeError(
                "Connector execution batch config is inconsistent"
            )
        try:
            config_payload = json.loads(config_payload_json)
        except (TypeError, ValueError, json.JSONDecodeError):
            config_payload = None
        if (
            not isinstance(config_payload, dict)
            or config_payload.get("connector_catalog_snapshot_id")
            != context.connector_catalog_snapshot_id
        ):
            raise ConnectorEventScopeError(
                "Connector catalog snapshot is not bound to the execution batch"
            )
        if (
            execution is None
            or execution["job_id"] != context.job_id
            or execution["turn_id"] != context.turn_id
            or execution["execution_batch_id"] != context.execution_batch_id
            or execution["capability_snapshot_id"] != context.capability_snapshot_id
            or execution["policy_snapshot_id"] != context.permission_snapshot_id
            or execution["tool_id"] not in _CONNECTOR_TOOL_IDS
        ):
            raise ConnectorEventScopeError(
                "Connector tool execution scope is inconsistent"
            )
        try:
            arguments = json.loads(str(execution["arguments_json"]))
        except (TypeError, ValueError, json.JSONDecodeError):
            arguments = None
        if (
            not isinstance(arguments, dict)
            or arguments.get("discovery_id") != context.discovery_id
        ):
            raise ConnectorEventScopeError(
                "Connector tool execution discovery is inconsistent"
            )
        if catalog is None or catalog["kind"] != "connectors":
            raise ConnectorEventScopeError(
                "Connector catalog snapshot authority is unavailable"
            )
        if call_event is None or call_event["item_id"] is None:
            raise ConnectorEventScopeError(
                "Connector tool call Event authority is unavailable"
            )
        try:
            call_payload = json.loads(str(call_event["payload_json"]))
        except (TypeError, ValueError, json.JSONDecodeError):
            call_payload = None
        try:
            call_activity = PublicToolActivity.model_validate(
                call_payload.get("activity")
                if isinstance(call_payload, dict)
                else None
            )
        except ValueError:
            call_activity = None
        if (
            call_activity is None
            or call_activity.tool_id != execution["tool_id"]
            or call_activity.tool_call_id != context.tool_call_id
        ):
            raise ConnectorEventScopeError(
                "Connector tool call Event identity is inconsistent"
            )
        return _RuntimeRoute(
            context=context,
            item_id=str(call_event["item_id"]),
            causation_id=str(call_event["event_id"]),
        )

    def _safe_projection(
        self,
        event: ConnectorOutboxEvent,
        payload: Mapping[str, Any],
        route: _RuntimeRoute | None,
    ) -> dict[str, Any]:
        projection: dict[str, Any] = {
            "account_id": self.account_id,
            "aggregate_id": event.aggregate_id,
            "aggregate_seq": event.aggregate_seq,
        }
        for key in _IDENTITY_FIELDS:
            value = payload.get(key)
            if value is not None:
                if not isinstance(value, str) or not _SAFE_IDENTITY.fullmatch(value):
                    raise ConnectorEventScopeError(
                        f"Connector {key} is not a safe identity"
                    )
                projection[key] = value
        for key in _TOKEN_FIELDS:
            value = payload.get(key)
            if value is not None:
                if not isinstance(value, str) or not _SAFE_TOKEN.fullmatch(value):
                    raise ConnectorEventScopeError(
                        f"Connector {key} is not a safe protocol token"
                    )
                projection[key] = value
        for key in _DIGEST_FIELDS:
            value = payload.get(key)
            if value is not None:
                if not isinstance(value, str) or not _SHA256.fullmatch(value):
                    raise ConnectorEventScopeError(
                        f"Connector {key} is not a SHA-256 digest"
                    )
                projection[key] = value
        if route is not None:
            projection["discovery_id"] = route.context.discovery_id
            projection["outcome"] = event.event_type.removeprefix(
                _MODEL_EVENT_PREFIX
            )
        return projection

    def _audit_thread(self) -> str:
        if self._audit_thread_id is not None:
            return self._audit_thread_id
        with self.kernel.database.reader() as connection:
            row = connection.execute(
                "SELECT thread_id FROM threads WHERE client_request_id = ?",
                ("system:connector-audit",),
            ).fetchone()
        if row is None:
            thread = self.kernel.create_thread(
                CreateThreadRequest(
                    title="Connector audit",
                    metadata={"visibility": "internal", "system": "connectors"},
                    client_request_id="system:connector-audit",
                )
            )
            self._audit_thread_id = thread.thread_id
        else:
            self._audit_thread_id = str(row["thread_id"])
        return self._audit_thread_id


__all__ = ["ConnectorEventScopeError", "RuntimeConnectorEventSink"]
