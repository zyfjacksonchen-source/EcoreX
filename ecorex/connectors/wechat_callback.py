"""Managed-session Product adapter for cloud-hosted WeChat callbacks."""

from __future__ import annotations

from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import threading
import time
from typing import Any, Mapping, Protocol
from urllib.parse import urlsplit

import httpx

from ecorex.json_boundary import JSONComplexityError, validate_json_complexity

from .channel_runtime import (
    ChannelInboundMessage,
    ChannelRuntimeDispatcher,
    ChannelTurnReceipt,
    ChannelTurnTerminalFailure,
)
from .channel_self_service import ChannelCredentialOwner
from .models import ConnectorHealth, ConnectorHealthResult


_CHANNELS = frozenset(
    {"wechatcom_app", "wechat_kf", "wechatmp_service"}
)
_ERROR_RE = re.compile(r"^[a-z][a-z0-9_]{0,127}$")
_MAX_RESPONSE_BYTES = 1024 * 1024
_MAX_PULL_ITEMS = 20


class _ManagedSession(Protocol):
    def snapshot(self) -> Any: ...

    def bearer_token(self) -> str: ...


class _ManagedWechatError(RuntimeError):
    def __init__(
        self,
        code: str,
        *,
        retryable: bool = False,
        uncertain: bool = False,
    ) -> None:
        if _ERROR_RE.fullmatch(code) is None or (retryable and uncertain):
            raise ValueError("managed WeChat error is invalid")
        super().__init__(code)
        self.code = code
        self.retryable = retryable
        self.uncertain = uncertain


class ManagedWechatCallbackClient:
    def __init__(
        self,
        *,
        connector_endpoint: str,
        allowed_hosts: frozenset[str],
        session: _ManagedSession,
        client: httpx.Client | None = None,
    ) -> None:
        parsed = urlsplit(connector_endpoint)
        hosts = frozenset(host.casefold().rstrip(".") for host in allowed_hosts)
        host = (parsed.hostname or "").casefold().rstrip(".")
        if (
            parsed.scheme != "https"
            or host not in hosts
            or parsed.port not in {None, 443}
            or parsed.username
            or parsed.password
            or parsed.path.rstrip("/") != "/api/v1/connectors"
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("managed WeChat endpoint is invalid")
        self.root = f"https://{host}/api/v1/channels/wechat"
        self.session = session
        self._owns_client = client is None
        self.client = client or httpx.Client(
            timeout=httpx.Timeout(connect=10, read=35, write=30, pool=10),
            follow_redirects=False,
            trust_env=False,
        )

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def bind(self, channel_id: str, material: Mapping[str, Any]) -> dict[str, Any]:
        prefix = {
            "wechatcom_app": "wechatcomapp",
            "wechat_kf": "wechat_kf",
            "wechatmp": "wechatmp",
            "wechatmp_service": "wechatmp",
        }[channel_id]
        app_key = "wechatcom_corp_id" if channel_id == "wechatcom_app" else (
            "wechat_kf_corp_id" if channel_id == "wechat_kf" else "wechatmp_app_id"
        )
        secret_key = f"{prefix}_secret"
        body = {
            "channel_id": channel_id,
            "app_id": material.get(app_key),
            "agent_id": material.get("wechatcomapp_agent_id")
            if channel_id == "wechatcom_app"
            else None,
            "app_secret": material.get(secret_key),
            "token": material.get(f"{prefix}_token"),
            "encoding_aes_key": material.get(f"{prefix}_aes_key"),
        }
        result = self._post(
            "/bindings", body, idempotency_key="bind-" + hashlib.sha256(
                json.dumps(body, sort_keys=True).encode()
            ).hexdigest()
        )
        if (
            set(result)
            != {
                "binding_id",
                "channel_id",
                "callback_url",
                "status",
                "external_display_name",
                "setup_requirement",
            }
            or result.get("channel_id") != channel_id
            or any(
                not isinstance(result.get(key), str) or not result[key]
                for key in (
                    "binding_id",
                    "callback_url",
                    "status",
                    "external_display_name",
                    "setup_requirement",
                )
            )
        ):
            raise _ManagedWechatError(
                "managed_wechat_response_invalid", retryable=True
            )
        return result

    def pull(self, binding_id: str, lease_id: str) -> dict[str, Any]:
        result = self._post(
            "/inbox/pull",
            {"binding_id": binding_id, "lease_id": lease_id, "limit": 20},
        )
        if set(result) != {"binding_id", "lease_id", "items"}:
            raise _ManagedWechatError(
                "managed_wechat_response_invalid", retryable=True
            )
        items = result.get("items")
        if (
            result.get("binding_id") != binding_id
            or result.get("lease_id") != lease_id
            or not isinstance(items, list)
            or len(items) > _MAX_PULL_ITEMS
        ):
            raise _ManagedWechatError(
                "managed_wechat_response_invalid", retryable=True
            )
        required = {
            "event_id",
            "channel_id",
            "conversation_id",
            "message_id",
            "text",
            "created_at",
        }
        if any(
            not isinstance(item, dict)
            or set(item) != required
            or any(not isinstance(value, str) or not value for value in item.values())
            for item in items
        ):
            raise _ManagedWechatError(
                "managed_wechat_response_invalid", retryable=True
            )
        return result

    def ack(self, binding_id: str, event_id: str, lease_id: str) -> None:
        self._post(
            "/inbox/ack",
            {"binding_id": binding_id, "event_id": event_id, "lease_id": lease_id},
            parse_success=False,
        )

    def abandon(self, binding_id: str, event_id: str, lease_id: str) -> None:
        self._post(
            "/inbox/abandon",
            {"binding_id": binding_id, "event_id": event_id, "lease_id": lease_id},
            parse_success=False,
        )

    def send(
        self,
        binding_id: str,
        event_id: str,
        text: str,
        idempotency_key: str,
    ) -> None:
        result = self._post(
            "/outbound",
            {"binding_id": binding_id, "event_id": event_id, "text": text},
            idempotency_key=idempotency_key,
            delivery=True,
        )
        state = result.get("state")
        if state not in {"sent", "ready"}:
            code = result.get("error_code")
            raise _ManagedWechatError(
                str(code)
                if state == "failed"
                and isinstance(code, str)
                and _ERROR_RE.fullmatch(code)
                else "managed_wechat_delivery_uncertain",
                uncertain=state != "failed",
            )

    def _post(
        self,
        suffix: str,
        body: Mapping[str, Any],
        *,
        idempotency_key: str | None = None,
        delivery: bool = False,
        parse_success: bool = True,
    ) -> dict[str, Any]:
        try:
            before = self.session.snapshot()
            token = self.session.bearer_token()
            after = self.session.snapshot()
        except Exception:
            raise _ManagedWechatError(
                "managed_session_unavailable", retryable=True
            ) from None
        if (
            before.account_id != after.account_id
            or before.organization_id != after.organization_id
            or before.generation != after.generation
        ):
            raise _ManagedWechatError("managed_session_changed", retryable=True)
        headers = {"Authorization": "Bearer " + token, "Accept-Encoding": "identity"}
        if idempotency_key is not None:
            headers["Idempotency-Key"] = idempotency_key
        response: httpx.Response | None = None
        try:
            request = self.client.build_request(
                "POST", self.root + suffix, json=dict(body), headers=headers
            )
            response = self.client.send(request, stream=True)
            payload = bytearray()
            for chunk in response.iter_bytes():
                payload.extend(chunk)
                if len(payload) > _MAX_RESPONSE_BYTES:
                    raise _ManagedWechatError(
                        "managed_wechat_response_too_large",
                        retryable=not delivery,
                        uncertain=delivery,
                    )
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout):
            raise _ManagedWechatError(
                "managed_wechat_unavailable", retryable=True
            ) from None
        except (httpx.TimeoutException, httpx.TransportError):
            raise _ManagedWechatError(
                "managed_wechat_delivery_uncertain"
                if delivery
                else "managed_wechat_unavailable",
                retryable=not delivery,
                uncertain=delivery,
            ) from None
        finally:
            if response is not None:
                response.close()
        assert response is not None
        if response.is_redirect or response.history:
            raise _ManagedWechatError("managed_wechat_redirect_refused")
        value: Any = None
        if payload:
            try:
                value = json.loads(payload.decode("utf-8"))
                validate_json_complexity(value, max_depth=24, max_nodes=10_000)
            except (
                UnicodeDecodeError,
                json.JSONDecodeError,
                JSONComplexityError,
                RecursionError,
            ):
                value = None
        if not 200 <= response.status_code < 300:
            code = "managed_wechat_rejected"
            retryable = response.status_code in {408, 425, 429} or response.status_code >= 500
            uncertain = False
            detail = value.get("detail", {}) if isinstance(value, dict) else {}
            if isinstance(detail, dict):
                remote_code = detail.get("code")
                if isinstance(remote_code, str) and _ERROR_RE.fullmatch(remote_code):
                    code = remote_code
                if isinstance(detail.get("retryable"), bool):
                    retryable = detail["retryable"]
                if isinstance(detail.get("uncertain"), bool):
                    uncertain = detail["uncertain"]
            if uncertain:
                retryable = False
            raise _ManagedWechatError(
                code, retryable=retryable, uncertain=uncertain
            )
        if not parse_success:
            return {}
        if not isinstance(value, dict):
            raise _ManagedWechatError(
                "managed_wechat_delivery_uncertain"
                if delivery
                else "managed_wechat_response_invalid",
                retryable=not delivery,
                uncertain=delivery,
            )
        return value


class _Store:
    def __init__(self, path: str | os.PathLike[str], owner: ChannelCredentialOwner):
        self.path = Path(os.path.abspath(path))
        self.scope = hashlib.sha256(
            f"{owner.organization_id}\0{owner.account_id}".encode()
        ).hexdigest()
        self._initialized = False

    def record(self, binding_id: str, lease_id: str, item: Mapping[str, Any]) -> None:
        with closing(self._connection()) as connection, connection:
            connection.execute(
                "INSERT INTO managed_wechat_events(scope,event_id,binding_id,lease_id,"
                "conversation_id,message_id,text,state) VALUES(?,?,?,?,?,?,?,'received') "
                "ON CONFLICT(scope,event_id) DO UPDATE SET lease_id=excluded.lease_id",
                (
                    self.scope,
                    item["event_id"],
                    binding_id,
                    lease_id,
                    item["conversation_id"],
                    item["message_id"],
                    item["text"],
                ),
            )

    def received(self) -> tuple[sqlite3.Row, ...]:
        return self._rows("state='received'")

    def outbound(self) -> tuple[sqlite3.Row, ...]:
        return self._rows("state='outbound'")

    def set_outbound(self, event_id: str, receipt: ChannelTurnReceipt) -> None:
        with closing(self._connection()) as connection, connection:
            connection.execute(
                "UPDATE managed_wechat_events SET state='outbound',channel_id=?,"
                "thread_id=?,turn_id=?,client_message_id=?,conversation_sha256=? "
                "WHERE scope=? AND event_id=? AND state='received'",
                (
                    receipt.channel_id,
                    receipt.thread_id,
                    receipt.turn_id,
                    receipt.client_message_id,
                    receipt.conversation_sha256,
                    self.scope,
                    event_id,
                ),
            )

    def finish(
        self, turn_id: str, state: str, error_code: str | None = None
    ) -> None:
        if state not in {"completed", "failed", "uncertain"}:
            raise ValueError("managed WeChat terminal state is invalid")
        if (state == "completed") != (error_code is None) or (
            error_code is not None and _ERROR_RE.fullmatch(error_code) is None
        ):
            raise ValueError("managed WeChat terminal error is invalid")
        with closing(self._connection()) as connection, connection:
            connection.execute(
                "UPDATE managed_wechat_events SET state=?,error_code=?,binding_id='',"
                "conversation_id='',message_id='',text='',lease_id='',channel_id=NULL,thread_id=NULL,"
                "turn_id=NULL,client_message_id=NULL,conversation_sha256=NULL "
                "WHERE scope=? AND turn_id=? AND state='outbound'",
                (state, error_code, self.scope, turn_id),
            )

    def terminal_error(self) -> tuple[str, bool] | None:
        with closing(self._connection()) as connection:
            row = connection.execute(
                "SELECT state,error_code FROM managed_wechat_events WHERE scope=? "
                "AND state = 'uncertain' LIMIT 1",
                (self.scope,),
            ).fetchone()
        if row is None:
            return None
        uncertain = str(row[0]) == "uncertain"
        code = str(row[1] or "")
        return (
            code if _ERROR_RE.fullmatch(code) else (
                "managed_wechat_delivery_uncertain"
                if uncertain
                else "managed_wechat_delivery_failed"
            ),
            uncertain,
        )

    def resolve_uncertain(self) -> None:
        with closing(self._connection()) as connection, connection:
            connection.execute(
                "UPDATE managed_wechat_events SET state='failed' "
                "WHERE scope=? AND state='uncertain'",
                (self.scope,),
            )

    def _rows(self, predicate: str) -> tuple[sqlite3.Row, ...]:
        with closing(self._connection()) as connection:
            return tuple(
                connection.execute(
                    f"SELECT * FROM managed_wechat_events WHERE scope=? AND {predicate} "
                    "ORDER BY rowid",
                    (self.scope,),
                ).fetchall()
            )

    def _connection(self) -> sqlite3.Connection:
        if self.path.exists() and stat.S_ISLNK(self.path.lstat().st_mode):
            raise RuntimeError("managed WeChat state path is invalid")
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA secure_delete = ON")
        if not self._initialized:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS managed_wechat_events("
                "scope TEXT NOT NULL,event_id TEXT NOT NULL,binding_id TEXT NOT NULL,"
                "lease_id TEXT NOT NULL,conversation_id TEXT NOT NULL,message_id TEXT NOT NULL,"
                "text TEXT NOT NULL,state TEXT NOT NULL CHECK(state IN "
                "('received','outbound','completed','failed','uncertain')),channel_id TEXT,thread_id TEXT,"
                "turn_id TEXT,client_message_id TEXT,conversation_sha256 TEXT,"
                "error_code TEXT,"
                "PRIMARY KEY(scope,event_id))"
            )
            table_sql = str(
                connection.execute(
                    "SELECT sql FROM sqlite_master WHERE type='table' "
                    "AND name='managed_wechat_events'"
                ).fetchone()[0]
            )
            if "'uncertain'" not in table_sql:
                connection.executescript(
                    """
                    ALTER TABLE managed_wechat_events
                    RENAME TO managed_wechat_events_v1;
                    CREATE TABLE managed_wechat_events(
                        scope TEXT NOT NULL,event_id TEXT NOT NULL,
                        binding_id TEXT NOT NULL,lease_id TEXT NOT NULL,
                        conversation_id TEXT NOT NULL,message_id TEXT NOT NULL,
                        text TEXT NOT NULL,state TEXT NOT NULL CHECK(state IN(
                            'received','outbound','completed','failed','uncertain'
                        )),channel_id TEXT,thread_id TEXT,turn_id TEXT,
                        client_message_id TEXT,conversation_sha256 TEXT,
                        error_code TEXT,PRIMARY KEY(scope,event_id)
                    );
                    INSERT INTO managed_wechat_events(
                        scope,event_id,binding_id,lease_id,conversation_id,
                        message_id,text,state,channel_id,thread_id,turn_id,
                        client_message_id,conversation_sha256
                    ) SELECT scope,event_id,binding_id,lease_id,conversation_id,
                        message_id,text,state,channel_id,thread_id,turn_id,
                        client_message_id,conversation_sha256
                    FROM managed_wechat_events_v1;
                    DROP TABLE managed_wechat_events_v1;
                    """
                )
            elif "error_code" not in {
                str(row[1])
                for row in connection.execute(
                    "PRAGMA table_info(managed_wechat_events)"
                )
            }:
                connection.execute(
                    "ALTER TABLE managed_wechat_events ADD COLUMN error_code TEXT"
                )
            connection.commit()
            os.chmod(self.path, 0o600)
            self._initialized = True
        return connection


class ManagedWechatCallbackAdapter:
    def __init__(
        self,
        channel_id: str,
        database_path: str | os.PathLike[str],
        *,
        client: ManagedWechatCallbackClient,
        poll_seconds: float = 0.25,
    ) -> None:
        if channel_id not in _CHANNELS or not 0.1 <= poll_seconds <= 5:
            raise ValueError("managed WeChat adapter configuration is invalid")
        self.channel_id = channel_id
        self.database_path = Path(database_path)
        self.client = client
        self.poll_seconds = poll_seconds
        self._dispatcher: ChannelRuntimeDispatcher | None = None
        self._store: _Store | None = None
        self._binding_id: str | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._health = ConnectorHealth.DISABLED
        self._last_error: str | None = None
        self._reply = threading.local()

    def bind_runtime(
        self, owner: ChannelCredentialOwner, dispatcher: ChannelRuntimeDispatcher
    ) -> None:
        snapshot = self.client.session.snapshot()
        if snapshot.account_id != owner.account_id or snapshot.organization_id != owner.organization_id:
            raise RuntimeError("managed WeChat owner does not match managed session")
        self._dispatcher = dispatcher
        self._store = _Store(self.database_path, owner)

    def test(self, config: Mapping[str, Any]) -> ConnectorHealthResult:
        try:
            self.client.bind(self.channel_id, config)
            return ConnectorHealthResult(ConnectorHealth.CONNECTED)
        except _ManagedWechatError as error:
            return ConnectorHealthResult(ConnectorHealth.ERROR, error.code)
        except Exception:
            return ConnectorHealthResult(ConnectorHealth.ERROR, "managed_wechat_unavailable")

    def start(self, config: Mapping[str, Any]) -> ConnectorHealthResult:
        if self._dispatcher is None or self._store is None:
            return ConnectorHealthResult(ConnectorHealth.ERROR, "managed_wechat_runtime_unavailable")
        try:
            result = self.client.bind(self.channel_id, config)
            self._binding_id = str(result["binding_id"])
            terminal = self._store.terminal_error()
            self._stop = threading.Event()
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._health = (
                ConnectorHealth.CONNECTED
                if terminal is None
                else ConnectorHealth.DEGRADED
                if terminal[1]
                else ConnectorHealth.ERROR
            )
            self._last_error = terminal[0] if terminal else None
            self._thread.start()
        except _ManagedWechatError as error:
            self._health = ConnectorHealth.ERROR
            self._last_error = error.code
        except Exception:
            self._health = ConnectorHealth.ERROR
            self._last_error = "managed_wechat_unavailable"
        return ConnectorHealthResult(self._health, self._last_error)

    def health(self) -> ConnectorHealthResult:
        return ConnectorHealthResult(self._health, self._last_error)

    def resolve_uncertain(self) -> None:
        if self._store is None:
            raise RuntimeError("managed WeChat Runtime is unavailable")
        self._store.resolve_uncertain()

    def stop(self, timeout_seconds: float) -> bool:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout_seconds)
        stopped = self._thread is None or not self._thread.is_alive()
        if stopped:
            self._health = ConnectorHealth.DISABLED
        return stopped

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._cycle()
                self._set_ready_health()
            except _ManagedWechatError as error:
                self._health = (
                    ConnectorHealth.DEGRADED
                    if error.retryable or error.uncertain
                    else ConnectorHealth.ERROR
                )
                self._last_error = error.code
            except Exception:
                self._health = ConnectorHealth.DEGRADED
                self._last_error = "managed_wechat_unavailable"
            self._stop.wait(self.poll_seconds)

    def _set_ready_health(self) -> None:
        terminal = self._store.terminal_error() if self._store is not None else None
        if terminal is None:
            self._health = ConnectorHealth.CONNECTED
            self._last_error = None
            return
        self._last_error, uncertain = terminal
        self._health = (
            ConnectorHealth.DEGRADED if uncertain else ConnectorHealth.ERROR
        )

    def _cycle(self) -> None:
        assert self._binding_id and self._store and self._dispatcher
        lease_id = "wxlease_" + hashlib.sha256(
            f"{time.time_ns()}:{self._binding_id}".encode()
        ).hexdigest()
        result = self.client.pull(self._binding_id, lease_id)
        for item in result.get("items", []):
            if item.get("channel_id") != self.channel_id:
                raise _ManagedWechatError("managed_wechat_channel_mismatch")
            self._store.record(self._binding_id, lease_id, item)
        for row in self._store.received():
            receipt = self._dispatcher.dispatch(
                ChannelInboundMessage(
                    channel_id=self.channel_id,
                    conversation_id=str(row["conversation_id"]),
                    message_id=str(row["message_id"]),
                    text=str(row["text"]),
                )
            )
            self._store.set_outbound(str(row["event_id"]), receipt)
            try:
                self.client.ack(
                    self._binding_id,
                    str(row["event_id"]),
                    str(row["lease_id"]),
                )
            except _ManagedWechatError as error:
                if error.retryable or error.uncertain:
                    raise
                self._store.finish(receipt.turn_id, "failed", error.code)
        for row in self._store.outbound():
            receipt = ChannelTurnReceipt(
                channel_id=str(row["channel_id"]), thread_id=str(row["thread_id"]),
                turn_id=str(row["turn_id"]), client_message_id=str(row["client_message_id"]),
                conversation_sha256=str(row["conversation_sha256"]),
            )
            try:
                self.client.ack(
                    self._binding_id,
                    str(row["event_id"]),
                    str(row["lease_id"]),
                )
            except _ManagedWechatError as error:
                if error.retryable or error.uncertain:
                    raise
                self._store.finish(receipt.turn_id, "failed", error.code)
                continue
            self._reply.value = (str(row["event_id"]), str(row["lease_id"]))
            try:
                if self._dispatcher.deliver(
                    receipt, conversation_id=str(row["conversation_id"]), transport=self
                ):
                    self._store.finish(receipt.turn_id, "completed")
            except ChannelTurnTerminalFailure as error:
                self._store.finish(
                    receipt.turn_id,
                    "failed",
                    error.code.replace("channel_", "managed_wechat_", 1),
                )
                try:
                    self.client.abandon(
                        self._binding_id,
                        str(row["event_id"]),
                        str(row["lease_id"]),
                    )
                except Exception:
                    pass
            except _ManagedWechatError as error:
                if error.uncertain:
                    self._store.finish(receipt.turn_id, "uncertain", error.code)
                    self._health = ConnectorHealth.DEGRADED
                    self._last_error = error.code
                    continue
                if not error.retryable:
                    self._store.finish(receipt.turn_id, "failed", error.code)
                    self._health = ConnectorHealth.ERROR
                    self._last_error = error.code
                    continue
                raise
            finally:
                self._reply.value = None

    def send_text(
        self,
        *,
        channel_id: str,
        conversation_id: str,
        text: str,
        idempotency_key: str,
    ) -> None:
        del conversation_id
        if channel_id != self.channel_id:
            raise _ManagedWechatError("managed_wechat_channel_mismatch")
        if not self._binding_id or not getattr(self._reply, "value", None):
            raise _ManagedWechatError("managed_wechat_reply_context_unavailable")
        event_id, _lease_id = self._reply.value
        self.client.send(self._binding_id, event_id, text, idempotency_key)


__all__ = ["ManagedWechatCallbackAdapter", "ManagedWechatCallbackClient"]
