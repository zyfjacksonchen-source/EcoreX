"""Managed-session Product adapter for cloud-hosted WeChat callbacks."""

from __future__ import annotations

from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import threading
import time
from typing import Any, Mapping, Protocol
from urllib.parse import urlsplit

import httpx

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


class _ManagedSession(Protocol):
    def snapshot(self) -> Any: ...

    def bearer_token(self) -> str: ...


class _ManagedWechatError(RuntimeError):
    def __init__(self, code: str, *, uncertain: bool = False) -> None:
        super().__init__(code)
        self.code = code
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
        return self._post(
            "/bindings", body, idempotency_key="bind-" + hashlib.sha256(
                json.dumps(body, sort_keys=True).encode()
            ).hexdigest()
        )

    def pull(self, binding_id: str, lease_id: str) -> dict[str, Any]:
        return self._post(
            "/inbox/pull",
            {"binding_id": binding_id, "lease_id": lease_id, "limit": 20},
        )

    def ack(self, binding_id: str, event_id: str, lease_id: str) -> None:
        self._post(
            "/inbox/ack",
            {"binding_id": binding_id, "event_id": event_id, "lease_id": lease_id},
        )

    def abandon(self, binding_id: str, event_id: str, lease_id: str) -> None:
        self._post(
            "/inbox/abandon",
            {"binding_id": binding_id, "event_id": event_id, "lease_id": lease_id},
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
        )
        if result.get("state") not in {"sent", "ready"}:
            raise _ManagedWechatError("managed_wechat_delivery_failed")

    def _post(
        self,
        suffix: str,
        body: Mapping[str, Any],
        *,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        before = self.session.snapshot()
        token = self.session.bearer_token()
        after = self.session.snapshot()
        if (
            before.account_id != after.account_id
            or before.organization_id != after.organization_id
            or before.generation != after.generation
        ):
            raise _ManagedWechatError("managed_session_changed")
        headers = {"Authorization": "Bearer " + token, "Accept-Encoding": "identity"}
        if idempotency_key is not None:
            headers["Idempotency-Key"] = idempotency_key
        try:
            response = self.client.post(self.root + suffix, json=dict(body), headers=headers)
        except (httpx.TimeoutException, httpx.TransportError):
            raise _ManagedWechatError("managed_wechat_unavailable") from None
        if response.is_redirect or response.history:
            raise _ManagedWechatError("managed_wechat_redirect_refused")
        if response.status_code not in {200, 201, 202}:
            uncertain = False
            try:
                uncertain = response.json().get("detail", {}).get("uncertain") is True
            except Exception:
                pass
            raise _ManagedWechatError("managed_wechat_rejected", uncertain=uncertain)
        value = response.json()
        if not isinstance(value, dict):
            raise _ManagedWechatError("managed_wechat_response_invalid")
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

    def finish(self, turn_id: str, state: str) -> None:
        with closing(self._connection()) as connection, connection:
            connection.execute(
                "UPDATE managed_wechat_events SET state=?,conversation_id='',"
                "message_id='',text='',lease_id='',channel_id=NULL,thread_id=NULL,"
                "turn_id=NULL,client_message_id=NULL,conversation_sha256=NULL "
                "WHERE scope=? AND turn_id=? AND state='outbound'",
                (state, self.scope, turn_id),
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
        if not self._initialized:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS managed_wechat_events("
                "scope TEXT NOT NULL,event_id TEXT NOT NULL,binding_id TEXT NOT NULL,"
                "lease_id TEXT NOT NULL,conversation_id TEXT NOT NULL,message_id TEXT NOT NULL,"
                "text TEXT NOT NULL,state TEXT NOT NULL CHECK(state IN "
                "('received','outbound','completed','failed')),channel_id TEXT,thread_id TEXT,"
                "turn_id TEXT,client_message_id TEXT,conversation_sha256 TEXT,"
                "PRIMARY KEY(scope,event_id))"
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
        except Exception:
            return ConnectorHealthResult(ConnectorHealth.ERROR, "managed_wechat_unavailable")

    def start(self, config: Mapping[str, Any]) -> ConnectorHealthResult:
        if self._dispatcher is None or self._store is None:
            return ConnectorHealthResult(ConnectorHealth.ERROR, "managed_wechat_runtime_unavailable")
        try:
            result = self.client.bind(self.channel_id, config)
            self._binding_id = str(result["binding_id"])
            self._stop = threading.Event()
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()
            self._health = ConnectorHealth.CONNECTED
            self._last_error = None
        except Exception:
            self._health = ConnectorHealth.ERROR
            self._last_error = "managed_wechat_unavailable"
        return ConnectorHealthResult(self._health, self._last_error)

    def health(self) -> ConnectorHealthResult:
        return ConnectorHealthResult(self._health, self._last_error)

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
            except Exception:
                self._health = ConnectorHealth.DEGRADED
                self._last_error = "managed_wechat_unavailable"
            self._stop.wait(self.poll_seconds)

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
            self.client.ack(self._binding_id, str(row["event_id"]), str(row["lease_id"]))
        for row in self._store.outbound():
            self.client.ack(
                self._binding_id, str(row["event_id"]), str(row["lease_id"])
            )
            receipt = ChannelTurnReceipt(
                channel_id=str(row["channel_id"]), thread_id=str(row["thread_id"]),
                turn_id=str(row["turn_id"]), client_message_id=str(row["client_message_id"]),
                conversation_sha256=str(row["conversation_sha256"]),
            )
            self._reply.value = (str(row["event_id"]), str(row["lease_id"]))
            try:
                if self._dispatcher.deliver(
                    receipt, conversation_id=str(row["conversation_id"]), transport=self
                ):
                    self._store.finish(receipt.turn_id, "completed")
            except ChannelTurnTerminalFailure:
                self.client.abandon(
                    self._binding_id, str(row["event_id"]), str(row["lease_id"])
                )
                self._store.finish(receipt.turn_id, "failed")
            finally:
                self._reply.value = None

    def send_text(
        self,
        *,
        conversation_id: str,
        text: str,
        idempotency_key: str,
    ) -> None:
        del conversation_id
        if not self._binding_id or not getattr(self._reply, "value", None):
            raise _ManagedWechatError("managed_wechat_reply_context_unavailable")
        event_id, _lease_id = self._reply.value
        self.client.send(self._binding_id, event_id, text, idempotency_key)


__all__ = ["ManagedWechatCallbackAdapter", "ManagedWechatCallbackClient"]
