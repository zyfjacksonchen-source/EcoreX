"""Durable human-in-the-loop requests."""

from __future__ import annotations

import hashlib
import re
import sqlite3
from datetime import datetime, timezone
from typing import Any

from ecorex.protocol import (
    InteractionAction,
    InteractionActionStyle,
    InteractionActionType,
    InteractionContract,
    ConnectorInteractionState,
    InteractionKind,
    InteractionRequest,
    InteractionResponse,
    InteractionStatus,
    ItemStatus,
    JobStatus,
    TurnStatus,
)

from .database import SQLiteDatabase, json_dumps, json_loads
from .errors import (
    IdempotencyConflictError,
    InteractionResponseValidationError,
    InvalidTransitionError,
    NotFoundError,
)
from .event_store import EventStore
from .ids import new_id


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _store_time(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        raise ValueError("datetime values must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _read_time(value: str | None) -> datetime | None:
    if value is None:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _fingerprint(value: dict[str, Any]) -> str:
    return hashlib.sha256(json_dumps(value).encode("utf-8")).hexdigest()


_CLIENT_REQUEST_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,191}$")


def _default_contract(
    kind: InteractionKind,
    options: list[dict[str, Any]] | None,
) -> InteractionContract:
    supplied = options or []
    if not supplied:
        supplied = {
            InteractionKind.PERMISSION_APPROVAL: [
                {"id": "allow", "label": "允许一次"},
                {"id": "deny", "label": "拒绝"},
            ],
            InteractionKind.INFORMATION: [
                {"id": "continue", "label": "继续"},
                {"id": "cancel", "label": "取消"},
            ],
            InteractionKind.CONFLICT_RESOLUTION: [
                {"id": "retry", "label": "重试"},
                {"id": "skip", "label": "跳过"},
                {"id": "cancel", "label": "取消任务"},
            ],
        }.get(kind, [])
    if not supplied:
        raise ValueError(f"{kind.value} requires an explicit interaction contract")
    action_type_by_id = {
        "allow": InteractionActionType.ALLOW,
        "deny": InteractionActionType.DENY,
        "continue": InteractionActionType.CONTINUE,
        "cancel": InteractionActionType.CANCEL,
        "retry": InteractionActionType.RETRY,
        "skip": InteractionActionType.SKIP,
        "accept": InteractionActionType.ACCEPT,
        "request_changes": InteractionActionType.REQUEST_CHANGES,
        "submit": InteractionActionType.SUBMIT,
    }
    actions: list[InteractionAction] = []
    for index, option in enumerate(supplied):
        if not isinstance(option, dict) or set(option) - {"id", "label"}:
            raise ValueError("interaction options must contain only id and label")
        option_id = option.get("id")
        label = option.get("label")
        if not isinstance(option_id, str) or not isinstance(label, str):
            raise ValueError("interaction option id and label are required")
        action_type = action_type_by_id.get(option_id)
        if action_type is None:
            raise ValueError(
                "custom interaction actions require an explicit typed contract"
            )
        actions.append(
            InteractionAction(
                action_id=option_id,
                label=label,
                action_type=action_type,
                style=(
                    InteractionActionStyle.PRIMARY
                    if index == 0
                    else (
                        InteractionActionStyle.DANGER
                        if action_type is InteractionActionType.DENY
                        else InteractionActionStyle.SECONDARY
                    )
                ),
            )
        )
    titles = {
        InteractionKind.PERMISSION_APPROVAL: "权限确认",
        InteractionKind.INFORMATION: "需要补充信息",
        InteractionKind.CONFLICT_RESOLUTION: "需要处理冲突",
    }
    contract = InteractionContract(
        title=titles.get(kind, "需要你的操作"),
        actions=actions,
    )
    return contract.validate_for_kind(kind)


def _contract_options(contract: InteractionContract) -> list[dict[str, Any]]:
    return [
        {
            "id": action.action_id,
            "label": action.label,
            "action_type": action.action_type.value,
        }
        for action in contract.actions
    ]


class InteractionStore:
    def __init__(self, database: SQLiteDatabase | str, events: EventStore | None = None):
        self.database = (
            database if isinstance(database, SQLiteDatabase) else SQLiteDatabase(database)
        )
        self.events = events or EventStore(self.database)

    @staticmethod
    def _from_row(row: sqlite3.Row) -> InteractionRequest:
        contract = InteractionContract.model_validate(
            json_loads(row["contract_json"], {})
        ).validate_for_kind(InteractionKind(row["kind"]))
        raw_response = json_loads(row["response_json"])
        response = (
            None
            if raw_response is None
            else contract.validate_response(InteractionResponse.model_validate(raw_response))
        )
        return InteractionRequest(
            interaction_id=row["interaction_id"],
            kind=InteractionKind(row["kind"]),
            status=InteractionStatus(row["status"]),
            prompt=row["prompt"],
            contract=contract,
            options=json_loads(row["options_json"], []),
            response=response,
            response_client_request_id=row["response_client_request_id"],
            thread_id=row["thread_id"],
            turn_id=row["turn_id"],
            job_id=row["job_id"],
            idempotency_key=row["idempotency_key"],
            expires_at=_read_time(row["expires_at"]),
            created_at=_read_time(row["created_at"]),
            updated_at=_read_time(row["updated_at"]),
        )

    def create(
        self,
        *,
        kind: InteractionKind,
        prompt: str,
        thread_id: str,
        idempotency_key: str,
        turn_id: str | None = None,
        job_id: str | None = None,
        options: list[dict[str, Any]] | None = None,
        contract: InteractionContract | dict[str, Any] | None = None,
        expires_at: datetime | None = None,
    ) -> InteractionRequest:
        with self.database.transaction() as connection:
            return self.create_in_transaction(
                connection,
                kind=kind,
                prompt=prompt,
                thread_id=thread_id,
                idempotency_key=idempotency_key,
                turn_id=turn_id,
                job_id=job_id,
                options=options,
                contract=contract,
                expires_at=expires_at,
            )

    def create_in_transaction(
        self,
        connection: sqlite3.Connection,
        *,
        kind: InteractionKind,
        prompt: str,
        thread_id: str,
        idempotency_key: str,
        turn_id: str | None = None,
        job_id: str | None = None,
        options: list[dict[str, Any]] | None = None,
        contract: InteractionContract | dict[str, Any] | None = None,
        expires_at: datetime | None = None,
        interaction_id: str | None = None,
        now: datetime | None = None,
    ) -> InteractionRequest:
        if not connection.in_transaction:
            raise RuntimeError("create_in_transaction requires an active transaction")
        kind = InteractionKind(kind)
        if not prompt or len(prompt) > 2_000:
            raise ValueError("prompt is required")
        if not thread_id:
            raise ValueError("thread_id is required")
        if not idempotency_key:
            raise ValueError("idempotency_key is required")
        if expires_at is not None and expires_at.tzinfo is None:
            raise ValueError("expires_at must be timezone-aware")
        typed_contract = (
            _default_contract(kind, options)
            if contract is None
            else InteractionContract.model_validate(contract).validate_for_kind(kind)
        )
        projected_options = _contract_options(typed_contract)
        if options:
            supplied_projection = [
                {"id": option.get("id"), "label": option.get("label")}
                for option in options
                if isinstance(option, dict)
            ]
            declared_projection = [
                {"id": option["id"], "label": option["label"]}
                for option in projected_options
            ]
            if supplied_projection != declared_projection:
                raise ValueError("interaction options disagree with the typed contract")
        options = projected_options
        request = {
            "kind": kind.value,
            "prompt": prompt,
            "thread_id": thread_id,
            "turn_id": turn_id,
            "job_id": job_id,
            "options": options,
            "contract": typed_contract.model_dump(mode="json"),
            "expires_at": _store_time(expires_at),
        }
        request_fingerprint = _fingerprint(request)
        duplicate = connection.execute(
            "SELECT * FROM interactions WHERE idempotency_key = ?",
            (idempotency_key,),
        ).fetchone()
        if duplicate is not None:
            if duplicate["request_fingerprint"] != request_fingerprint:
                raise IdempotencyConflictError(
                    f"interaction idempotency key {idempotency_key!r} was reused"
                )
            return self._from_row(duplicate)

        now = now or _utc_now()
        if now.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        if expires_at is not None and expires_at <= now:
            raise ValueError("expires_at must be in the future")
        interaction_id = interaction_id or new_id("hitl")
        self.events.append_in_transaction(
            connection,
            thread_id=thread_id,
            turn_id=turn_id,
            job_id=job_id,
            item_id=interaction_id,
            event_type="interaction.requested",
            payload={
                "kind": kind.value,
                "prompt": prompt,
                "options": options,
                "contract": typed_contract.model_dump(mode="json"),
                "expires_at": _store_time(expires_at),
            },
            idempotency_key=f"{interaction_id}:requested",
            created_at=now,
        )
        timestamp = _store_time(now)
        connection.execute(
            """
            INSERT INTO interactions(
                interaction_id, kind, status, prompt, contract_version,
                contract_json, options_json, response_json,
                response_client_request_id, response_fingerprint,
                thread_id, turn_id, job_id, idempotency_key,
                request_fingerprint, expires_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, 1, ?, ?, NULL, NULL, NULL, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                interaction_id,
                kind.value,
                InteractionStatus.PENDING.value,
                prompt,
                json_dumps(typed_contract.model_dump(mode="json")),
                json_dumps(options),
                thread_id,
                turn_id,
                job_id,
                idempotency_key,
                request_fingerprint,
                _store_time(expires_at),
                timestamp,
                timestamp,
            ),
        )
        row = connection.execute(
            "SELECT * FROM interactions WHERE interaction_id = ?", (interaction_id,)
        ).fetchone()
        return self._from_row(row)

    def get(self, interaction_id: str) -> InteractionRequest:
        with self.database.reader() as connection:
            row = connection.execute(
                "SELECT * FROM interactions WHERE interaction_id = ?",
                (interaction_id,),
            ).fetchone()
        if row is None:
            raise NotFoundError(f"interaction {interaction_id!r} does not exist")
        return self._from_row(row)

    def list_pending(self, *, thread_id: str | None = None) -> list[InteractionRequest]:
        query = "SELECT * FROM interactions WHERE status = ?"
        parameters: list[Any] = [InteractionStatus.PENDING.value]
        if thread_id is not None:
            query += " AND thread_id = ?"
            parameters.append(thread_id)
        query += " ORDER BY created_at ASC, interaction_id ASC"
        with self.database.reader() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [self._from_row(row) for row in rows]

    def update_connector_state(
        self,
        interaction_id: str,
        state: ConnectorInteractionState,
    ) -> InteractionRequest:
        """Persist public OAuth progress without resolving the HITL request."""

        state = ConnectorInteractionState(state)
        now = _utc_now()
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM interactions WHERE interaction_id = ?",
                (interaction_id,),
            ).fetchone()
            if row is None:
                raise NotFoundError(f"interaction {interaction_id!r} does not exist")
            if (
                InteractionKind(row["kind"]) is not InteractionKind.CONNECTOR_LOGIN
                or InteractionStatus(row["status"]) is not InteractionStatus.PENDING
            ):
                raise InvalidTransitionError(
                    "only a pending connector login interaction can change state"
                )
            contract = InteractionContract.model_validate(
                json_loads(row["contract_json"], {})
            ).validate_for_kind(InteractionKind.CONNECTOR_LOGIN)
            if contract.connector is None:
                raise InvalidTransitionError(
                    "connector login interaction has no connector context"
                )
            if contract.connector.state is state:
                return self._from_row(row)
            revision_row = connection.execute(
                "SELECT COALESCE(MAX(CAST(json_extract(payload_json, "
                "'$.state_revision') AS INTEGER)), 0) AS revision "
                "FROM events WHERE item_id=? "
                "AND event_type='interaction.connector_state_changed'",
                (interaction_id,),
            ).fetchone()
            state_revision = int(revision_row["revision"]) + 1
            updated_contract = contract.model_copy(
                update={
                    "connector": contract.connector.model_copy(
                        update={"state": state}
                    )
                }
            ).validate_for_kind(InteractionKind.CONNECTOR_LOGIN)
            self.events.append_in_transaction(
                connection,
                thread_id=row["thread_id"],
                turn_id=row["turn_id"],
                job_id=row["job_id"],
                item_id=interaction_id,
                event_type="interaction.connector_state_changed",
                payload={
                    "from": contract.connector.state.value,
                    "to": state.value,
                    "connector_id": contract.connector.connector_id,
                    "state_revision": state_revision,
                },
                idempotency_key=(
                    f"{interaction_id}:connector-state:{state_revision}"
                ),
                created_at=now,
            )
            connection.execute(
                "UPDATE interactions SET contract_json=?, updated_at=? "
                "WHERE interaction_id=? AND status=?",
                (
                    json_dumps(updated_contract.model_dump(mode="json")),
                    _store_time(now),
                    interaction_id,
                    InteractionStatus.PENDING.value,
                ),
            )
            updated = connection.execute(
                "SELECT * FROM interactions WHERE interaction_id = ?",
                (interaction_id,),
            ).fetchone()
            return self._from_row(updated)

    def respond(
        self,
        interaction_id: str,
        response: InteractionResponse | dict[str, Any],
        *,
        client_request_id: str,
    ) -> InteractionRequest:
        now = _utc_now()
        with self.database.transaction() as connection:
            return self.respond_in_transaction(
                connection,
                interaction_id,
                response,
                client_request_id=client_request_id,
                now=now,
            )

    def _resume_dependents_in_transaction(
        self, connection: sqlite3.Connection, row: sqlite3.Row, now: datetime
    ) -> None:
        if row["job_id"] is not None:
            job = connection.execute(
                "SELECT * FROM jobs WHERE job_id = ?", (row["job_id"],)
            ).fetchone()
            if job is not None and JobStatus(job["status"]) == JobStatus.WAITING_HUMAN:
                self.events.append_in_transaction(
                    connection,
                    thread_id=row["thread_id"],
                    turn_id=row["turn_id"],
                    job_id=row["job_id"],
                    event_type="job.resumed",
                    payload={
                        "attempt": job["attempt"],
                        "interaction_id": row["interaction_id"],
                        "available_at": _store_time(now),
                    },
                    idempotency_key=f"{job['job_id']}:interaction:{row['interaction_id']}:resumed",
                    created_at=now,
                )
                connection.execute(
                    "UPDATE jobs SET status = ?, available_at = ?, lease_owner = NULL, "
                    "lease_token = NULL, lease_expires_at = NULL, heartbeat_at = NULL, "
                    "updated_at = ? WHERE job_id = ?",
                    (
                        JobStatus.QUEUED.value,
                        _store_time(now),
                        _store_time(now),
                        job["job_id"],
                    ),
                )
        if row["turn_id"] is not None:
            turn = connection.execute(
                "SELECT * FROM turns WHERE turn_id = ?", (row["turn_id"],)
            ).fetchone()
            if turn is not None and TurnStatus(turn["status"]) == TurnStatus.WAITING_HUMAN:
                self.events.append_in_transaction(
                    connection,
                    thread_id=row["thread_id"],
                    turn_id=row["turn_id"],
                    event_type="turn.status_changed",
                    payload={
                        "from": TurnStatus.WAITING_HUMAN.value,
                        "to": TurnStatus.PREPARING.value,
                        "reason": "interaction_resolved",
                    },
                    idempotency_key=f"{row['interaction_id']}:turn-resumed",
                    created_at=now,
                )
                connection.execute(
                    "UPDATE turns SET status = ?, terminal_reason = NULL, updated_at = ? "
                    "WHERE turn_id = ?",
                    (TurnStatus.PREPARING.value, _store_time(now), row["turn_id"]),
                )
        item = connection.execute(
            "SELECT * FROM items WHERE item_id = ?", (row["interaction_id"],)
        ).fetchone()
        if item is not None and ItemStatus(item["status"]) not in {
            ItemStatus.COMPLETED,
            ItemStatus.FAILED,
            ItemStatus.CANCELLED,
        }:
            self.events.append_in_transaction(
                connection,
                thread_id=row["thread_id"],
                turn_id=row["turn_id"],
                item_id=row["interaction_id"],
                event_type="item.status_changed",
                payload={
                    "from": item["status"],
                    "to": ItemStatus.COMPLETED.value,
                    "reason": "interaction_resolved",
                },
                idempotency_key=f"{row['interaction_id']}:item-resolved",
                created_at=now,
            )
            connection.execute(
                "UPDATE items SET status = ?, updated_at = ? WHERE item_id = ?",
                (ItemStatus.COMPLETED.value, _store_time(now), row["interaction_id"]),
            )

    @staticmethod
    def _validate_response(
        row: sqlite3.Row,
        response: InteractionResponse | dict[str, Any],
    ) -> InteractionResponse:
        try:
            contract = InteractionContract.model_validate(
                json_loads(row["contract_json"], {})
            ).validate_for_kind(InteractionKind(row["kind"]))
            return contract.validate_response(InteractionResponse.model_validate(response))
        except ValueError as error:
            raise InteractionResponseValidationError(str(error)) from error

    def respond_in_transaction(
        self,
        connection: sqlite3.Connection,
        interaction_id: str,
        response: InteractionResponse | dict[str, Any],
        *,
        client_request_id: str,
        now: datetime | None = None,
    ) -> InteractionRequest:
        if not connection.in_transaction:
            raise RuntimeError("respond_in_transaction requires an active transaction")
        now = now or _utc_now()
        if now.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        if not isinstance(client_request_id, str) or _CLIENT_REQUEST_ID.fullmatch(
            client_request_id
        ) is None:
            raise ValueError("client_request_id is required and invalid")
        raw_response = (
            response.model_dump(mode="json")
            if isinstance(response, InteractionResponse)
            else response
        )
        response_fingerprint = _fingerprint(
            {"interaction_id": interaction_id, "response": raw_response}
        )
        duplicate_request = connection.execute(
            "SELECT * FROM interactions WHERE response_client_request_id = ?",
            (client_request_id,),
        ).fetchone()
        if duplicate_request is not None:
            if duplicate_request["response_fingerprint"] != response_fingerprint:
                raise IdempotencyConflictError(
                    "client request ID was reused for a different interaction response"
                )
            return self._from_row(duplicate_request)
        row = connection.execute(
            "SELECT * FROM interactions WHERE interaction_id = ?",
            (interaction_id,),
        ).fetchone()
        if row is None:
            raise NotFoundError(f"interaction {interaction_id!r} does not exist")
        status = InteractionStatus(row["status"])
        if status == InteractionStatus.RESOLVED:
            raise IdempotencyConflictError(
                "a resolved interaction must be replayed with its original client request ID"
            )
        if status != InteractionStatus.PENDING:
            raise InvalidTransitionError(
                f"cannot respond to an interaction in {status.value}"
            )
        expires_at = _read_time(row["expires_at"])
        if expires_at is not None and expires_at <= now:
            return self._close_pending_in_transaction(
                connection,
                row,
                InteractionStatus.EXPIRED,
                "interaction.expired",
                {"expired_at": _store_time(now)},
                now,
            )
        validated_response = self._validate_response(row, raw_response)
        response_payload = validated_response.model_dump(mode="json")
        response_json = json_dumps(response_payload)
        response_fingerprint = _fingerprint(
            {"interaction_id": interaction_id, "response": response_payload}
        )
        self.events.append_in_transaction(
            connection,
            thread_id=row["thread_id"],
            turn_id=row["turn_id"],
            job_id=row["job_id"],
            item_id=interaction_id,
            event_type="interaction.resolved",
            payload={
                "response": response_payload,
                "client_request_id": client_request_id,
            },
            correlation_id=client_request_id,
            idempotency_key=f"{interaction_id}:resolved",
            created_at=now,
        )
        connection.execute(
            "UPDATE interactions SET status = ?, response_json = ?, "
            "response_client_request_id = ?, response_fingerprint = ?, updated_at = ? "
            "WHERE interaction_id = ?",
            (
                InteractionStatus.RESOLVED.value,
                response_json,
                client_request_id,
                response_fingerprint,
                _store_time(now),
                interaction_id,
            ),
        )
        self._resume_dependents_in_transaction(connection, row, now)
        updated = connection.execute(
            "SELECT * FROM interactions WHERE interaction_id = ?",
            (interaction_id,),
        ).fetchone()
        return self._from_row(updated)

    def cancel(self, interaction_id: str, *, reason: str = "cancelled") -> InteractionRequest:
        return self._close_pending(
            interaction_id,
            InteractionStatus.CANCELLED,
            "interaction.cancelled",
            {"reason": reason},
        )

    def _close_pending(
        self,
        interaction_id: str,
        target: InteractionStatus,
        event_type: str,
        payload: dict[str, Any],
    ) -> InteractionRequest:
        now = _utc_now()
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM interactions WHERE interaction_id = ?",
                (interaction_id,),
            ).fetchone()
            if row is None:
                raise NotFoundError(f"interaction {interaction_id!r} does not exist")
            return self._close_pending_in_transaction(
                connection, row, target, event_type, payload, now
            )

    def _close_pending_in_transaction(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        target: InteractionStatus,
        event_type: str,
        payload: dict[str, Any],
        now: datetime,
    ) -> InteractionRequest:
        status = InteractionStatus(row["status"])
        if status == target:
            return self._from_row(row)
        if status != InteractionStatus.PENDING:
            raise InvalidTransitionError(
                f"cannot close an interaction in {status.value}"
            )
        interaction_id = row["interaction_id"]
        self.events.append_in_transaction(
            connection,
            thread_id=row["thread_id"],
            turn_id=row["turn_id"],
            job_id=row["job_id"],
            item_id=interaction_id,
            event_type=event_type,
            payload=payload,
            idempotency_key=f"{interaction_id}:{target.value}",
            created_at=now,
        )
        connection.execute(
            "UPDATE interactions SET status = ?, updated_at = ? "
            "WHERE interaction_id = ?",
            (target.value, _store_time(now), interaction_id),
        )
        terminal_job = (
            JobStatus.FAILED
            if target == InteractionStatus.EXPIRED
            else JobStatus.CANCELLED
        )
        if row["job_id"] is not None:
            job = connection.execute(
                "SELECT * FROM jobs WHERE job_id = ?", (row["job_id"],)
            ).fetchone()
            if job is not None and JobStatus(job["status"]) == JobStatus.WAITING_HUMAN:
                self.events.append_in_transaction(
                    connection,
                    thread_id=row["thread_id"],
                    turn_id=row["turn_id"],
                    job_id=row["job_id"],
                    event_type=(
                        "job.failed"
                        if terminal_job == JobStatus.FAILED
                        else "job.cancelled"
                    ),
                    payload={
                        "attempt": job["attempt"],
                        "reason": target.value,
                        "interaction_id": interaction_id,
                    },
                    idempotency_key=f"{interaction_id}:job:{terminal_job.value}",
                    created_at=now,
                )
                connection.execute(
                    "UPDATE jobs SET status = ?, lease_owner = NULL, lease_token = NULL, "
                    "lease_expires_at = NULL, heartbeat_at = NULL, last_error = ?, "
                    "updated_at = ? WHERE job_id = ?",
                    (
                        terminal_job.value,
                        target.value,
                        _store_time(now),
                        row["job_id"],
                    ),
                )
        turn = None
        turn_target = None
        if row["turn_id"] is not None:
            candidate = connection.execute(
                "SELECT * FROM turns WHERE turn_id = ?", (row["turn_id"],)
            ).fetchone()
            if (
                candidate is not None
                and TurnStatus(candidate["status"]) == TurnStatus.WAITING_HUMAN
            ):
                turn = candidate
                turn_target = (
                    TurnStatus.FAILED
                    if target == InteractionStatus.EXPIRED
                    else TurnStatus.CANCELLED
                )
        item = connection.execute(
            "SELECT * FROM items WHERE item_id = ?", (interaction_id,)
        ).fetchone()
        if item is not None and ItemStatus(item["status"]) not in {
            ItemStatus.COMPLETED,
            ItemStatus.FAILED,
            ItemStatus.CANCELLED,
        }:
            item_target = (
                ItemStatus.FAILED
                if target == InteractionStatus.EXPIRED
                else ItemStatus.CANCELLED
            )
            self.events.append_in_transaction(
                connection,
                thread_id=row["thread_id"],
                turn_id=row["turn_id"],
                item_id=interaction_id,
                event_type="item.status_changed",
                payload={
                    "from": item["status"],
                    "to": item_target.value,
                    "reason": target.value,
                },
                idempotency_key=f"{interaction_id}:item:{item_target.value}",
                created_at=now,
            )
            connection.execute(
                "UPDATE items SET status = ?, updated_at = ? WHERE item_id = ?",
                (item_target.value, _store_time(now), interaction_id),
            )
        if turn is not None and turn_target is not None:
            dependent_job_target = (
                JobStatus.FAILED
                if turn_target == TurnStatus.FAILED
                else JobStatus.CANCELLED
            )
            dependent_item_target = (
                ItemStatus.FAILED
                if turn_target == TurnStatus.FAILED
                else ItemStatus.CANCELLED
            )
            jobs = connection.execute(
                "SELECT * FROM jobs WHERE turn_id = ?", (row["turn_id"],)
            ).fetchall()
            for job in jobs:
                if JobStatus(job["status"]) in {
                    JobStatus.COMPLETED,
                    JobStatus.FAILED,
                    JobStatus.CANCELLED,
                    JobStatus.DEAD_LETTER,
                }:
                    continue
                self.events.append_in_transaction(
                    connection,
                    thread_id=row["thread_id"],
                    turn_id=row["turn_id"],
                    job_id=job["job_id"],
                    event_type=(
                        "job.failed"
                        if dependent_job_target == JobStatus.FAILED
                        else "job.cancelled"
                    ),
                    payload={"attempt": job["attempt"], "reason": target.value},
                    idempotency_key=f"{interaction_id}:dependent-job:{job['job_id']}",
                    created_at=now,
                )
                connection.execute(
                    "UPDATE jobs SET status = ?, lease_owner = NULL, lease_token = NULL, "
                    "lease_expires_at = NULL, heartbeat_at = NULL, last_error = ?, "
                    "updated_at = ? WHERE job_id = ?",
                    (
                        dependent_job_target.value,
                        target.value,
                        _store_time(now),
                        job["job_id"],
                    ),
                )
            others = connection.execute(
                "SELECT * FROM interactions WHERE turn_id = ? AND status = ?",
                (row["turn_id"], InteractionStatus.PENDING.value),
            ).fetchall()
            for other in others:
                self.events.append_in_transaction(
                    connection,
                    thread_id=row["thread_id"],
                    turn_id=row["turn_id"],
                    job_id=other["job_id"],
                    item_id=other["interaction_id"],
                    event_type="interaction.cancelled",
                    payload={"reason": f"peer_{target.value}"},
                    idempotency_key=f"{interaction_id}:peer:{other['interaction_id']}",
                    created_at=now,
                )
                connection.execute(
                    "UPDATE interactions SET status = ?, updated_at = ? "
                    "WHERE interaction_id = ?",
                    (
                        InteractionStatus.CANCELLED.value,
                        _store_time(now),
                        other["interaction_id"],
                    ),
                )
            items = connection.execute(
                "SELECT * FROM items WHERE turn_id = ?", (row["turn_id"],)
            ).fetchall()
            for dependent in items:
                dependent_status = ItemStatus(dependent["status"])
                if dependent_status in {
                    ItemStatus.COMPLETED,
                    ItemStatus.FAILED,
                    ItemStatus.CANCELLED,
                }:
                    continue
                self.events.append_in_transaction(
                    connection,
                    thread_id=row["thread_id"],
                    turn_id=row["turn_id"],
                    item_id=dependent["item_id"],
                    event_type="item.status_changed",
                    payload={
                        "from": dependent_status.value,
                        "to": dependent_item_target.value,
                        "reason": target.value,
                    },
                    idempotency_key=f"{interaction_id}:dependent-item:{dependent['item_id']}",
                    created_at=now,
                )
                connection.execute(
                    "UPDATE items SET status = ?, updated_at = ? WHERE item_id = ?",
                    (
                        dependent_item_target.value,
                        _store_time(now),
                        dependent["item_id"],
                    ),
                )
            self.events.append_in_transaction(
                connection,
                thread_id=row["thread_id"],
                turn_id=row["turn_id"],
                event_type="turn.status_changed",
                payload={
                    "from": turn["status"],
                    "to": turn_target.value,
                    "reason": target.value,
                },
                idempotency_key=f"{interaction_id}:turn:{turn_target.value}",
                created_at=now,
            )
            connection.execute(
                "UPDATE turns SET status = ?, terminal_reason = ?, updated_at = ? "
                "WHERE turn_id = ?",
                (
                    turn_target.value,
                    target.value,
                    _store_time(now),
                    row["turn_id"],
                ),
            )
        updated = connection.execute(
            "SELECT * FROM interactions WHERE interaction_id = ?", (interaction_id,)
        ).fetchone()
        return self._from_row(updated)

    def expire_due(self, *, now: datetime | None = None) -> list[str]:
        now = now or _utc_now()
        if now.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        with self.database.transaction() as connection:
            return self.expire_due_in_transaction(connection, now=now)

    def expire_due_in_transaction(
        self,
        connection: sqlite3.Connection,
        *,
        now: datetime | None = None,
    ) -> list[str]:
        """Converge due HITL inside the caller's guarded commit boundary."""

        if not connection.in_transaction:
            raise RuntimeError("expire_due_in_transaction requires an active transaction")
        now = now or _utc_now()
        if now.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        rows = connection.execute(
            "SELECT * FROM interactions WHERE status = ? "
            "AND expires_at IS NOT NULL AND expires_at <= ? "
            "ORDER BY expires_at ASC, interaction_id ASC",
            (InteractionStatus.PENDING.value, _store_time(now)),
        ).fetchall()
        expired: list[str] = []
        for row in rows:
            self._close_pending_in_transaction(
                connection,
                row,
                InteractionStatus.EXPIRED,
                "interaction.expired",
                {"expired_at": _store_time(now)},
                now,
            )
            expired.append(row["interaction_id"])
        return expired
