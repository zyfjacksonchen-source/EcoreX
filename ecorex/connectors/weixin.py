"""Weixin iLink device login and long-poll transport for the Product Runtime."""

from __future__ import annotations

import base64
from io import BytesIO
from collections.abc import Callable, Mapping
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import hashlib
import os
from pathlib import Path
import secrets
import sqlite3
import stat
import threading
import time
from typing import Any, Protocol
from urllib.parse import urlsplit

import httpx
from ecorex import __version__

from .channel_runtime import (
    ChannelInboundMessage,
    ChannelRuntimeDispatcher,
    ChannelTurnReceipt,
    ChannelTurnTerminalFailure,
)
from .channel_self_service import (
    ChannelCredentialOwner,
    ChannelDeviceAuthorization,
    ChannelDeviceAuthorizationError,
)
from .models import ConnectorHealth, ConnectorHealthResult


_DEFAULT_BASE_URL = "https://ilinkai.weixin.qq.com"
_CLIENT_VERSION = "131072"
_CHANNEL_VERSION = __version__
_FLOW_TTL = timedelta(minutes=8)
_MAX_RESPONSE_BYTES = 1024 * 1024
_MAX_TEXT = 4000
_SESSION_EXPIRED = -14


class _HTTPClient(Protocol):
    def get(self, path: str, *, params: Mapping[str, Any]) -> httpx.Response: ...

    def post(self, path: str, *, json: Mapping[str, Any]) -> httpx.Response: ...

    def close(self) -> None: ...


class _WeixinFailure(RuntimeError):
    def __init__(
        self,
        code: str,
        *,
        uncertain: bool = False,
        permanent: bool = False,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.uncertain = uncertain
        self.permanent = permanent


@dataclass(slots=True)
class _DeviceFlow:
    flow_id: str
    status: str
    verification_url: str | None
    qr_image_data_url: str | None
    qrcode: str | None
    expires_at: datetime
    config: dict[str, str] | None = None
    credentials: dict[str, str] | None = None

    def projection(self) -> ChannelDeviceAuthorization:
        return ChannelDeviceAuthorization(
            flow_id=self.flow_id,
            status=self.status,
            verification_url=self.verification_url,
            qr_image_data_url=self.qr_image_data_url,
            expires_at=self.expires_at,
            config=dict(self.config) if self.config is not None else None,
            secrets=dict(self.credentials) if self.credentials is not None else None,
        )


class _WeixinStore:
    """Tenant-private cursor, context and delivery journal in one 0600 DB."""

    def __init__(self, path: str | os.PathLike[str], owner: ChannelCredentialOwner):
        self.path = Path(os.path.abspath(path))
        self.scope = hashlib.sha256(
            f"{owner.organization_id}\0{owner.account_id}".encode()
        ).hexdigest()
        self._lock = threading.RLock()
        self._initialized = False

    def cursor(self) -> str:
        with closing(self._connection()) as connection:
            row = connection.execute(
                "SELECT cursor FROM weixin_state WHERE scope = ?", (self.scope,)
            ).fetchone()
        return str(row[0]) if row else ""

    def advance(self, cursor: str) -> None:
        cursor = _bounded(cursor, "weixin cursor", 64 * 1024, allow_empty=True)
        with closing(self._connection()) as connection, connection:
            connection.execute(
                """
                INSERT INTO weixin_state(scope, cursor) VALUES (?, ?)
                ON CONFLICT(scope) DO UPDATE SET cursor = excluded.cursor
                """,
                (self.scope, cursor),
            )

    def add_pending(
        self,
        receipt: ChannelTurnReceipt,
        conversation_id: str,
        context_token: str,
    ) -> None:
        context_token = _bounded(context_token, "weixin context token", 64 * 1024)
        with closing(self._connection()) as connection, connection:
            connection.execute(
                """
                INSERT INTO weixin_context(scope, conversation_id, context_token)
                VALUES (?, ?, ?)
                ON CONFLICT(scope, conversation_id) DO UPDATE
                SET context_token = excluded.context_token
                """,
                (self.scope, conversation_id, context_token),
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO weixin_pending(
                    scope, turn_id, channel_id, thread_id, client_message_id,
                    conversation_sha256, conversation_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    self.scope,
                    receipt.turn_id,
                    receipt.channel_id,
                    receipt.thread_id,
                    receipt.client_message_id,
                    receipt.conversation_sha256,
                    conversation_id,
                ),
            )

    def context_token(self, conversation_id: str) -> str | None:
        with closing(self._connection()) as connection:
            row = connection.execute(
                "SELECT context_token FROM weixin_context "
                "WHERE scope = ? AND conversation_id = ?",
                (self.scope, conversation_id),
            ).fetchone()
        return str(row[0]) if row else None

    def pending(self) -> tuple[tuple[ChannelTurnReceipt, str], ...]:
        with closing(self._connection()) as connection:
            rows = connection.execute(
                """
                SELECT channel_id, thread_id, turn_id, client_message_id,
                       conversation_sha256, conversation_id
                FROM weixin_pending WHERE scope = ? ORDER BY rowid
                """,
                (self.scope,),
            ).fetchall()
        return tuple(
            (
                ChannelTurnReceipt(
                    channel_id=str(row[0]),
                    thread_id=str(row[1]),
                    turn_id=str(row[2]),
                    client_message_id=str(row[3]),
                    conversation_sha256=str(row[4]),
                ),
                str(row[5]),
            )
            for row in rows
        )

    def complete_pending(self, turn_id: str) -> None:
        with closing(self._connection()) as connection, connection:
            connection.execute(
                "DELETE FROM weixin_pending WHERE scope = ? AND turn_id = ?",
                (self.scope, turn_id),
            )

    def claim_delivery(self, key: str) -> str:
        now = int(time.time())
        with closing(self._connection()) as connection, connection:
            row = connection.execute(
                "SELECT state FROM weixin_deliveries "
                "WHERE scope = ? AND delivery_key = ?",
                (self.scope, key),
            ).fetchone()
            if row is not None:
                state = str(row[0])
                if state == "sending":
                    connection.execute(
                        "UPDATE weixin_deliveries SET state = 'uncertain', updated_at = ? "
                        "WHERE scope = ? AND delivery_key = ?",
                        (now, self.scope, key),
                    )
                    return "uncertain"
                return state
            connection.execute(
                "INSERT INTO weixin_deliveries(scope, delivery_key, state, updated_at) "
                "VALUES (?, ?, 'sending', ?)",
                (self.scope, key, now),
            )
        return "send"

    def mark_delivery(self, key: str, state: str) -> None:
        if state not in {"sent", "uncertain"}:
            raise ValueError("weixin delivery state is invalid")
        with closing(self._connection()) as connection, connection:
            connection.execute(
                "UPDATE weixin_deliveries SET state = ?, updated_at = ? "
                "WHERE scope = ? AND delivery_key = ?",
                (state, int(time.time()), self.scope, key),
            )

    def release_delivery(self, key: str) -> None:
        with closing(self._connection()) as connection, connection:
            connection.execute(
                "DELETE FROM weixin_deliveries "
                "WHERE scope = ? AND delivery_key = ? AND state = 'sending'",
                (self.scope, key),
            )

    def expire_session(self) -> None:
        with closing(self._connection()) as connection, connection:
            for table in (
                "weixin_state",
                "weixin_context",
                "weixin_pending",
                "weixin_deliveries",
            ):
                connection.execute(f"DELETE FROM {table} WHERE scope = ?", (self.scope,))

    def _connection(self) -> sqlite3.Connection:
        with self._lock:
            if self.path.exists() and stat.S_ISLNK(self.path.lstat().st_mode):
                raise RuntimeError("weixin state path is invalid")
            connection = sqlite3.connect(self.path, timeout=5)
            connection.execute("PRAGMA busy_timeout = 5000")
            if not self._initialized:
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS weixin_state(
                        scope TEXT PRIMARY KEY,
                        cursor TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS weixin_context(
                        scope TEXT NOT NULL,
                        conversation_id TEXT NOT NULL,
                        context_token TEXT NOT NULL,
                        PRIMARY KEY(scope, conversation_id)
                    );
                    CREATE TABLE IF NOT EXISTS weixin_pending(
                        scope TEXT NOT NULL,
                        turn_id TEXT NOT NULL,
                        channel_id TEXT NOT NULL,
                        thread_id TEXT NOT NULL,
                        client_message_id TEXT NOT NULL,
                        conversation_sha256 TEXT NOT NULL,
                        conversation_id TEXT NOT NULL,
                        PRIMARY KEY(scope, turn_id)
                    );
                    CREATE TABLE IF NOT EXISTS weixin_deliveries(
                        scope TEXT NOT NULL,
                        delivery_key TEXT NOT NULL,
                        state TEXT NOT NULL CHECK(state IN ('sending','sent','uncertain')),
                        updated_at INTEGER NOT NULL,
                        PRIMARY KEY(scope, delivery_key)
                    );
                    """
                )
                connection.commit()
                os.chmod(self.path, 0o600)
                self._initialized = True
            return connection


class WeixinILinkAdapter:
    """One iLink worker bound to the existing Agent Runtime and OS Vault."""

    def __init__(
        self,
        database_path: str | os.PathLike[str],
        *,
        client_factory: Callable[[str, str], _HTTPClient] | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.database_path = Path(os.path.abspath(database_path))
        self.client_factory = client_factory or self._default_client
        self.now = now or (lambda: datetime.now(UTC))
        self._owner: ChannelCredentialOwner | None = None
        self._dispatcher: ChannelRuntimeDispatcher | None = None
        self._store: _WeixinStore | None = None
        self._client: _HTTPClient | None = None
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._lock = threading.RLock()
        self._flow: _DeviceFlow | None = None
        self._health = ConnectorHealth.DISABLED
        self._last_error: str | None = None

    def bind_runtime(
        self,
        owner: ChannelCredentialOwner,
        dispatcher: ChannelRuntimeDispatcher,
    ) -> None:
        with self._lock:
            if self._dispatcher is not None and (
                self._owner != owner or self._dispatcher is not dispatcher
            ):
                raise RuntimeError("weixin Runtime is already bound")
            self._owner = owner
            self._dispatcher = dispatcher
            self._store = _WeixinStore(self.database_path, owner)

    def begin_authorization(self) -> ChannelDeviceAuthorization:
        with self._lock:
            flow = self._flow
            if (
                flow is not None
                and flow.status in {"pending", "scanned"}
                and flow.expires_at > self.now()
            ):
                return flow.projection()
        return self._new_flow(None).projection()

    def poll_authorization(self, flow_id: str) -> ChannelDeviceAuthorization:
        with self._lock:
            flow = self._required_flow(flow_id)
            if flow.status in {"confirmed", "cancelled", "expired"}:
                return flow.projection()
            if flow.expires_at <= self.now():
                flow.status = "expired"
                flow.verification_url = None
                flow.qr_image_data_url = None
                flow.qrcode = None
                return flow.projection()
            qrcode = flow.qrcode
        if qrcode is None:
            raise ChannelDeviceAuthorizationError("weixin_device_flow_invalid", 409)
        payload = self._qr_request(
            "ilink/bot/get_qrcode_status", {"qrcode": qrcode}
        )
        status = payload.get("status", "wait")
        with self._lock:
            flow = self._required_flow(flow_id)
            if flow.qrcode != qrcode:
                raise ChannelDeviceAuthorizationError("weixin_device_flow_conflict", 409)
            if status == "wait":
                flow.status = "pending"
            elif status == "scaned":
                flow.status = "scanned"
            elif status == "expired":
                flow.status = "expired"
                flow.verification_url = None
                flow.qr_image_data_url = None
                flow.qrcode = None
            elif status == "confirmed":
                token = _bounded(payload.get("bot_token"), "weixin token", 64 * 1024)
                bot_id = _bounded(payload.get("ilink_bot_id"), "weixin bot id", 512)
                user_id = _bounded(payload.get("ilink_user_id", "unknown"), "weixin user id", 512)
                base_url = _base_url(payload.get("baseurl") or _DEFAULT_BASE_URL)
                flow.status = "confirmed"
                flow.verification_url = None
                flow.qr_image_data_url = None
                flow.qrcode = None
                flow.config = {
                    "weixin_base_url": base_url,
                    "weixin_bot_id": bot_id,
                    "weixin_user_id": user_id,
                }
                flow.credentials = {"weixin_token": token}
            else:
                raise ChannelDeviceAuthorizationError(
                    "weixin_provider_response_invalid"
                )
            return flow.projection()

    def cancel_authorization(self, flow_id: str) -> ChannelDeviceAuthorization:
        with self._lock:
            flow = self._required_flow(flow_id)
            if flow.status == "confirmed":
                raise ChannelDeviceAuthorizationError(
                    "weixin_device_flow_confirmed", 409
                )
            flow.status = "cancelled"
            flow.verification_url = None
            flow.qr_image_data_url = None
            flow.qrcode = None
            flow.config = None
            flow.credentials = None
            return flow.projection()

    def refresh_authorization(self, flow_id: str) -> ChannelDeviceAuthorization:
        with self._lock:
            flow = self._required_flow(flow_id)
            if flow.status == "confirmed":
                raise ChannelDeviceAuthorizationError(
                    "weixin_device_flow_confirmed", 409
                )
        return self._new_flow(flow_id).projection()

    def consume_authorization(self, flow_id: str) -> None:
        with self._lock:
            flow = self._required_flow(flow_id)
            if flow.status != "confirmed":
                raise RuntimeError("weixin device flow is not confirmed")
            flow.config = None
            flow.credentials = None

    def test(self, config: Mapping[str, Any]) -> ConnectorHealthResult:
        try:
            base_url, token = _configuration(config)
            client = self.client_factory(base_url, token)
            try:
                self._updates(client, "")
            finally:
                _close(client)
            return ConnectorHealthResult(ConnectorHealth.CONNECTED)
        except _WeixinFailure as error:
            return ConnectorHealthResult(ConnectorHealth.ERROR, error.code)
        except Exception:
            return ConnectorHealthResult(
                ConnectorHealth.ERROR, "weixin_transport_unavailable"
            )

    def start(self, config: Mapping[str, Any]) -> ConnectorHealthResult:
        try:
            base_url, token = _configuration(config)
            with self._lock:
                if self._dispatcher is None or self._store is None:
                    return ConnectorHealthResult(
                        ConnectorHealth.ERROR, "weixin_runtime_unavailable"
                    )
                if self._thread is not None and self._thread.is_alive():
                    return ConnectorHealthResult(self._health, self._last_error)
                client = self.client_factory(base_url, token)
                self._client = client
                self._stop_event = threading.Event()
                self._health = ConnectorHealth.CONNECTED
                self._last_error = None
                self._thread = threading.Thread(
                    target=self._run,
                    args=(client,),
                    name="emate-weixin-channel",
                    daemon=True,
                )
                self._thread.start()
            return ConnectorHealthResult(ConnectorHealth.CONNECTED)
        except _WeixinFailure as error:
            return ConnectorHealthResult(ConnectorHealth.ERROR, error.code)
        except Exception:
            return ConnectorHealthResult(
                ConnectorHealth.ERROR, "weixin_transport_unavailable"
            )

    def health(self) -> ConnectorHealthResult:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return ConnectorHealthResult(self._health, self._last_error)
            return ConnectorHealthResult(
                self._health if self._last_error else ConnectorHealth.DISABLED,
                self._last_error,
            )

    def stop(self, timeout_seconds: float) -> bool:
        with self._lock:
            thread = self._thread
            client = self._client
            self._stop_event.set()
        if client is not None:
            _close(client)
        if thread is not None:
            thread.join(timeout_seconds)
        stopped = thread is None or not thread.is_alive()
        if stopped:
            with self._lock:
                self._thread = None
                self._client = None
                self._health = ConnectorHealth.DISABLED
                self._last_error = None
        return stopped

    def send_text(
        self,
        *,
        channel_id: str,
        conversation_id: str,
        text: str,
        idempotency_key: str,
    ) -> None:
        if channel_id != "weixin" or not isinstance(text, str) or not text:
            raise ValueError("weixin delivery is invalid")
        with self._lock:
            client = self._client
            store = self._store
        if client is None or store is None:
            raise _WeixinFailure("weixin_not_running")
        chunks = _chunks(text)
        for index, chunk in enumerate(chunks):
            key = f"{idempotency_key}:{index + 1}:{len(chunks)}"
            state = store.claim_delivery(key)
            if state == "sent":
                continue
            if state == "uncertain":
                raise _WeixinFailure("weixin_delivery_uncertain", uncertain=True)
            context_token = store.context_token(conversation_id)
            if context_token is None:
                store.release_delivery(key)
                raise _WeixinFailure("weixin_context_token_missing", permanent=True)
            body = {
                "msg": {
                    "from_user_id": "",
                    "to_user_id": conversation_id,
                    "client_id": hashlib.sha256(key.encode()).hexdigest()[:16],
                    "message_type": 2,
                    "message_state": 2,
                    "item_list": [{"type": 1, "text_item": {"text": chunk}}],
                    "context_token": context_token,
                }
            }
            try:
                self._request(client, "ilink/bot/sendmessage", body, delivery=True)
            except _WeixinFailure as error:
                if error.code == "weixin_reauthentication_required":
                    store.expire_session()
                if error.uncertain:
                    store.mark_delivery(key, "uncertain")
                else:
                    store.release_delivery(key)
                raise
            store.mark_delivery(key, "sent")

    def _new_flow(self, flow_id: str | None) -> _DeviceFlow:
        payload = self._qr_request("ilink/bot/get_bot_qrcode", {"bot_type": "3"})
        qrcode = _bounded(payload.get("qrcode"), "weixin qrcode", 8192)
        verification_url = _bounded(
            payload.get("qrcode_img_content"), "weixin verification URL", 8192
        )
        flow = _DeviceFlow(
            flow_id=flow_id or f"wxauth_{secrets.token_hex(16)}",
            status="pending",
            verification_url=verification_url,
            qr_image_data_url=_qr_png_data_url(verification_url),
            qrcode=qrcode,
            expires_at=self.now() + _FLOW_TTL,
        )
        with self._lock:
            self._flow = flow
        return flow

    def _qr_request(self, path: str, params: Mapping[str, Any]) -> Mapping[str, Any]:
        client = self.client_factory(_DEFAULT_BASE_URL, "")
        try:
            return self._request(client, path, params, get=True)
        except _WeixinFailure as error:
            raise ChannelDeviceAuthorizationError(error.code) from None
        finally:
            _close(client)

    def _run(self, client: _HTTPClient) -> None:
        backoff = 1.0
        try:
            while not self._stop_event.is_set():
                try:
                    self._drain_pending()
                    store = self._required_store()
                    payload = self._updates(client, store.cursor())
                    messages = payload.get("msgs", [])
                    if not isinstance(messages, list) or len(messages) > 100:
                        raise _WeixinFailure("weixin_provider_response_invalid")
                    for message in messages:
                        if not isinstance(message, dict):
                            raise _WeixinFailure("weixin_provider_response_invalid")
                        self._accept_message(message)
                    cursor = payload.get("get_updates_buf")
                    if cursor is not None:
                        store.advance(
                            _bounded(cursor, "weixin cursor", 64 * 1024, allow_empty=True)
                        )
                    self._drain_pending()
                    self._set_health(ConnectorHealth.CONNECTED, None)
                    backoff = 1.0
                except _WeixinFailure as error:
                    if error.code == "weixin_reauthentication_required":
                        self._required_store().expire_session()
                    self._set_health(
                        ConnectorHealth.DEGRADED
                        if error.uncertain
                        else ConnectorHealth.ERROR,
                        error.code,
                    )
                    if error.permanent:
                        return
                    self._stop_event.wait(backoff)
                    backoff = min(backoff * 2, 30.0)
                except Exception:
                    self._set_health(
                        ConnectorHealth.ERROR, "weixin_runtime_dispatch_failed"
                    )
                    self._stop_event.wait(backoff)
                    backoff = min(backoff * 2, 30.0)
        finally:
            _close(client)
            with self._lock:
                if self._client is client:
                    self._client = None

    def _accept_message(self, message: Mapping[str, Any]) -> None:
        if message.get("message_type") != 1:
            return
        conversation = message.get("from_user_id")
        message_id = message.get("message_id", message.get("seq"))
        context_token = message.get("context_token")
        text = _message_text(message.get("item_list"))
        if (
            not isinstance(conversation, str)
            or not conversation
            or not isinstance(message_id, (str, int))
            or isinstance(message_id, bool)
            or not isinstance(context_token, str)
            or not context_token
            or not text
        ):
            return
        receipt = self._required_dispatcher().dispatch(
            ChannelInboundMessage(
                channel_id="weixin",
                conversation_id=conversation,
                message_id=str(message_id),
                text=text,
            )
        )
        self._required_store().add_pending(receipt, conversation, context_token)

    def _drain_pending(self) -> None:
        dispatcher = self._required_dispatcher()
        store = self._required_store()
        for receipt, conversation_id in store.pending():
            try:
                delivered = dispatcher.deliver(
                    receipt,
                    conversation_id=conversation_id,
                    transport=self,
                )
            except ChannelTurnTerminalFailure:
                store.complete_pending(receipt.turn_id)
                continue
            if delivered:
                store.complete_pending(receipt.turn_id)

    def _updates(self, client: _HTTPClient, cursor: str) -> Mapping[str, Any]:
        return self._request(
            client,
            "ilink/bot/getupdates",
            {"get_updates_buf": cursor},
        )

    def _request(
        self,
        client: _HTTPClient,
        path: str,
        body: Mapping[str, Any],
        *,
        get: bool = False,
        delivery: bool = False,
    ) -> Mapping[str, Any]:
        try:
            if get:
                response = client.get(path, params=body)
            else:
                payload = dict(body)
                payload.setdefault("base_info", {"channel_version": _CHANNEL_VERSION})
                response = client.post(path, json=payload)
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout):
            raise _WeixinFailure("weixin_transport_unavailable") from None
        except (httpx.TimeoutException, httpx.TransportError):
            raise _WeixinFailure(
                "weixin_delivery_uncertain" if delivery else "weixin_transport_unavailable",
                uncertain=delivery,
            ) from None
        except Exception:
            raise _WeixinFailure(
                "weixin_delivery_uncertain" if delivery else "weixin_transport_unavailable",
                uncertain=delivery,
            ) from None
        if len(response.content) > _MAX_RESPONSE_BYTES:
            raise _WeixinFailure(
                "weixin_delivery_uncertain" if delivery else "weixin_provider_response_invalid",
                uncertain=delivery,
            )
        if response.status_code in {401, 403}:
            raise _WeixinFailure(
                "weixin_reauthentication_required", permanent=True
            )
        if response.status_code >= 500:
            raise _WeixinFailure(
                "weixin_delivery_uncertain" if delivery else "weixin_transport_unavailable",
                uncertain=delivery,
            )
        if response.status_code != 200:
            raise _WeixinFailure("weixin_provider_rejected")
        try:
            payload = response.json()
        except ValueError:
            raise _WeixinFailure(
                "weixin_delivery_uncertain" if delivery else "weixin_provider_response_invalid",
                uncertain=delivery,
            ) from None
        if not isinstance(payload, dict):
            raise _WeixinFailure("weixin_provider_response_invalid")
        ret = payload.get("ret", 0)
        errcode = payload.get("errcode", 0)
        if ret == _SESSION_EXPIRED or errcode == _SESSION_EXPIRED:
            raise _WeixinFailure(
                "weixin_reauthentication_required", permanent=True
            )
        if ret not in {0, None} or errcode not in {0, None}:
            raise _WeixinFailure("weixin_provider_rejected")
        return payload

    def _required_flow(self, flow_id: str) -> _DeviceFlow:
        if self._flow is None or self._flow.flow_id != flow_id:
            raise ChannelDeviceAuthorizationError("weixin_device_flow_not_found", 404)
        return self._flow

    def _required_dispatcher(self) -> ChannelRuntimeDispatcher:
        with self._lock:
            if self._dispatcher is None:
                raise _WeixinFailure("weixin_runtime_unavailable", permanent=True)
            return self._dispatcher

    def _required_store(self) -> _WeixinStore:
        with self._lock:
            if self._store is None:
                raise _WeixinFailure("weixin_runtime_unavailable", permanent=True)
            return self._store

    def _set_health(self, health: ConnectorHealth, error: str | None) -> None:
        with self._lock:
            self._health = health
            self._last_error = error

    @staticmethod
    def _default_client(base_url: str, token: str) -> httpx.Client:
        headers = {
            "AuthorizationType": "ilink_bot_token",
            "X-WECHAT-UIN": base64.b64encode(
                str(secrets.randbelow(2**32)).encode()
            ).decode(),
            "iLink-App-Id": "bot",
            "iLink-App-ClientVersion": _CLIENT_VERSION,
            "User-Agent": f"e-Mate/{__version__} WeixinChannel/1",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return httpx.Client(
            base_url=base_url.rstrip("/") + "/",
            timeout=httpx.Timeout(connect=4, read=40, write=4, pool=4),
            limits=httpx.Limits(max_connections=2, max_keepalive_connections=1),
            follow_redirects=False,
            headers=headers,
        )


def _configuration(config: Mapping[str, Any]) -> tuple[str, str]:
    expected = {
        "weixin_base_url",
        "weixin_bot_id",
        "weixin_user_id",
        "weixin_token",
    }
    if not isinstance(config, Mapping) or set(config) != expected:
        raise _WeixinFailure("weixin_configuration_invalid", permanent=True)
    base_url = _base_url(config.get("weixin_base_url"))
    _bounded(config.get("weixin_bot_id"), "weixin bot id", 512)
    _bounded(config.get("weixin_user_id"), "weixin user id", 512)
    token = _bounded(config.get("weixin_token"), "weixin token", 64 * 1024)
    return base_url, token


def _base_url(value: Any) -> str:
    text = _bounded(value, "weixin base URL", 2048).rstrip("/")
    parsed = urlsplit(text)
    host = (parsed.hostname or "").casefold()
    if (
        parsed.scheme != "https"
        or not host.endswith(".weixin.qq.com")
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in {None, 443}
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise _WeixinFailure("weixin_configuration_invalid", permanent=True)
    return text


def _bounded(value: Any, label: str, limit: int, *, allow_empty: bool = False) -> str:
    if (
        not isinstance(value, str)
        or (not value and not allow_empty)
        or len(value) > limit
        or any(character in value for character in ("\x00", "\r", "\n"))
    ):
        raise _WeixinFailure(f"{label.replace(' ', '_')}_invalid")
    return value


def _message_text(value: Any) -> str:
    if not isinstance(value, list) or len(value) > 100:
        return ""
    parts: list[str] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        if item.get("type") == 1:
            text = item.get("text_item", {}).get("text")
        elif item.get("type") == 3:
            text = item.get("voice_item", {}).get("text")
        else:
            continue
        if isinstance(text, str) and text.strip():
            parts.append(text.strip())
    return "\n".join(parts)


def _chunks(text: str) -> tuple[str, ...]:
    chunks: list[str] = []
    remaining = text
    while remaining:
        split = min(len(remaining), _MAX_TEXT)
        if split < len(remaining):
            newline = remaining.rfind("\n", _MAX_TEXT // 2, split)
            if newline >= 0:
                split = newline + 1
        chunks.append(remaining[:split])
        remaining = remaining[split:]
    return tuple(chunks)


def _qr_png_data_url(value: str) -> str:
    try:
        import qrcode
    except ImportError:
        raise ChannelDeviceAuthorizationError("weixin_qrcode_dependency_missing") from None
    qr = qrcode.QRCode(
        error_correction=qrcode.constants.ERROR_CORRECT_L,
        box_size=6,
        border=4,
    )
    qr.add_data(value)
    qr.make(fit=True)
    image = qr.make_image(fill_color="black", back_color="white")
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode()


def _close(client: _HTTPClient) -> None:
    try:
        client.close()
    except Exception:
        pass


__all__ = ["WeixinILinkAdapter"]
