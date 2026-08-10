"""User-managed Feishu message Bot transport for the product Runtime."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
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
from typing import Any, Protocol

import httpx

from ecorex import __version__

from .channel_runtime import (
    ChannelInboundMessage,
    ChannelRuntimeDispatcher,
    ChannelTurnTerminalFailure,
    ChannelTurnReceipt,
)
from .channel_self_service import ChannelCredentialOwner
from .models import ConnectorHealth, ConnectorHealthResult


_APP_ID_RE = re.compile(r"^cli_[A-Za-z0-9_-]{6,128}$")
_ERROR_RE = re.compile(r"^feishu_bot_[a-z0-9_]{1,124}$")
_MAX_RESPONSE_BYTES = 1024 * 1024
_MAX_TEXT_CHARS = 3500


class _HTTPClient(Protocol):
    def post(
        self,
        path: str,
        *,
        params: Mapping[str, str] | None = None,
        json: Mapping[str, Any],
        headers: Mapping[str, str] | None = None,
    ) -> httpx.Response: ...

    def close(self) -> None: ...


class _MessageChannel(Protocol):
    @property
    def is_ready(self) -> bool: ...

    def on(self, name: str, handler: Callable[..., Any]) -> Callable[[], Any]: ...

    async def start_background(self, *, timeout: float | None = 30.0) -> None: ...

    def stop(self, *, join_timeout: float = 5.0) -> None: ...


class _FeishuFailure(RuntimeError):
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


class _FeishuAPI:
    """Bounded credential validation, token refresh and text delivery."""

    def __init__(
        self,
        app_id: str,
        app_secret: str,
        client: _HTTPClient,
    ) -> None:
        self._app_id = app_id
        self._app_secret = app_secret
        self._client = client
        self._token = ""
        self._token_expires_at = 0.0
        self._lock = threading.RLock()

    def validate(self) -> None:
        self._access_token(force=True)

    def send_text(self, chat_id: str, text: str, idempotency_key: str) -> str:
        response = self._request(
            "/open-apis/im/v1/messages",
            params={"receive_id_type": "chat_id"},
            body={
                "receive_id": chat_id,
                "msg_type": "text",
                "content": json.dumps(
                    {"text": text},
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                "uuid": hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()[:32],
            },
            headers={"Authorization": "Bearer " + self._access_token()},
            delivery=True,
        )
        data = response.get("data")
        message_id = data.get("message_id") if isinstance(data, dict) else None
        if not isinstance(message_id, str) or not message_id:
            raise _FeishuFailure("feishu_bot_delivery_uncertain", uncertain=True)
        return message_id

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:
            pass

    def _access_token(self, *, force: bool = False) -> str:
        with self._lock:
            now = time.monotonic()
            if not force and self._token and now < self._token_expires_at:
                return self._token
            response = self._request(
                "/open-apis/auth/v3/tenant_access_token/internal",
                body={"app_id": self._app_id, "app_secret": self._app_secret},
                credential_request=True,
            )
            token = response.get("tenant_access_token")
            expire = response.get("expire")
            if (
                not isinstance(token, str)
                or not token
                or isinstance(expire, bool)
                or not isinstance(expire, int)
                or expire <= 0
            ):
                raise _FeishuFailure("feishu_bot_provider_response_invalid")
            self._token = token
            self._token_expires_at = now + max(1, expire - 300)
            return token

    def _request(
        self,
        path: str,
        *,
        body: Mapping[str, Any],
        params: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
        credential_request: bool = False,
        delivery: bool = False,
    ) -> Mapping[str, Any]:
        try:
            response = self._client.post(
                path,
                params=params,
                json=body,
                headers=headers,
            )
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout):
            raise _FeishuFailure("feishu_bot_transport_unavailable") from None
        except (httpx.WriteTimeout, httpx.ReadTimeout):
            raise _FeishuFailure(
                "feishu_bot_delivery_uncertain"
                if delivery
                else "feishu_bot_transport_unavailable",
                uncertain=delivery,
            ) from None
        except (httpx.TimeoutException, httpx.TransportError, OSError):
            raise _FeishuFailure(
                "feishu_bot_delivery_uncertain"
                if delivery
                else "feishu_bot_transport_unavailable",
                uncertain=delivery,
            ) from None
        except Exception:
            raise _FeishuFailure(
                "feishu_bot_delivery_uncertain"
                if delivery
                else "feishu_bot_transport_unavailable",
                uncertain=delivery,
            ) from None
        if len(response.content) > _MAX_RESPONSE_BYTES:
            raise _FeishuFailure(
                "feishu_bot_delivery_uncertain"
                if delivery
                else "feishu_bot_provider_response_invalid",
                uncertain=delivery,
            )
        if response.status_code in {401, 403}:
            raise _FeishuFailure("feishu_bot_auth_rejected", permanent=True)
        if response.status_code == 429:
            raise _FeishuFailure("feishu_bot_rate_limited")
        if response.status_code >= 500:
            raise _FeishuFailure(
                "feishu_bot_delivery_uncertain"
                if delivery
                else "feishu_bot_transport_unavailable",
                uncertain=delivery,
            )
        if response.status_code != 200:
            raise _FeishuFailure(
                "feishu_bot_auth_rejected"
                if credential_request
                else "feishu_bot_provider_rejected",
                permanent=credential_request or delivery,
            )
        try:
            payload = response.json()
        except ValueError:
            raise _FeishuFailure(
                "feishu_bot_delivery_uncertain"
                if delivery
                else "feishu_bot_provider_response_invalid",
                uncertain=delivery,
            ) from None
        if not isinstance(payload, dict):
            raise _FeishuFailure(
                "feishu_bot_delivery_uncertain"
                if delivery
                else "feishu_bot_provider_response_invalid",
                uncertain=delivery,
            )
        if payload.get("code") != 0:
            raise _FeishuFailure(
                "feishu_bot_auth_rejected"
                if credential_request
                else "feishu_bot_provider_rejected",
                permanent=credential_request or delivery,
            )
        return payload


class _FeishuStore:
    """Tenant journal; raw chat IDs stay inside this transport-private DB."""

    def __init__(self, path: str | os.PathLike[str], owner: ChannelCredentialOwner):
        self.path = Path(os.path.abspath(path))
        self.scope = hashlib.sha256(
            f"{owner.organization_id}\0{owner.account_id}".encode("utf-8")
        ).hexdigest()
        self._lock = threading.RLock()
        self._initialized = False

    def add_pending(self, receipt: ChannelTurnReceipt, conversation_id: str) -> None:
        with closing(self._connection()) as connection, connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO feishu_pending(
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

    def pending(self) -> tuple[tuple[ChannelTurnReceipt, str], ...]:
        with closing(self._connection()) as connection:
            rows = connection.execute(
                """
                SELECT channel_id, thread_id, turn_id, client_message_id,
                       conversation_sha256, conversation_id
                FROM feishu_pending WHERE scope = ? AND state = 'pending'
                ORDER BY rowid
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

    def finish_pending(
        self, turn_id: str, state: str, error_code: str | None = None
    ) -> None:
        if state not in {"completed", "failed", "uncertain"}:
            raise ValueError("feishu pending terminal state is invalid")
        if (state == "completed") != (error_code is None) or (
            error_code is not None and _ERROR_RE.fullmatch(error_code) is None
        ):
            raise ValueError("feishu pending terminal error is invalid")
        with closing(self._connection()) as connection, connection:
            connection.execute(
                "UPDATE feishu_pending SET state = ?, error_code = ?, "
                "channel_id = '', thread_id = '', client_message_id = '', "
                "conversation_sha256 = '', conversation_id = '' "
                "WHERE scope = ? AND turn_id = ? AND state = 'pending'",
                (state, error_code, self.scope, turn_id),
            )

    def terminal_error(self) -> tuple[str, bool] | None:
        with closing(self._connection()) as connection:
            row = connection.execute(
                "SELECT state,error_code FROM feishu_pending WHERE scope = ? "
                "AND state = 'uncertain' LIMIT 1",
                (self.scope,),
            ).fetchone()
            delivery = connection.execute(
                "SELECT state FROM feishu_deliveries WHERE scope=? "
                "AND state IN ('sending','uncertain') LIMIT 1",
                (self.scope,),
            ).fetchone()
        if row is None:
            return (
                ("feishu_bot_delivery_uncertain", True)
                if delivery is not None
                else None
            )
        uncertain = str(row[0]) == "uncertain"
        code = str(row[1] or "")
        return (
            code if _ERROR_RE.fullmatch(code) else (
                "feishu_bot_delivery_uncertain"
                if uncertain
                else "feishu_bot_delivery_rejected"
            ),
            uncertain,
        )

    def resolve_uncertain(self) -> None:
        with closing(self._connection()) as connection, connection:
            connection.execute(
                "UPDATE feishu_pending SET state='failed' "
                "WHERE scope=? AND state='uncertain'",
                (self.scope,),
            )
            connection.execute(
                "UPDATE feishu_deliveries SET state='failed' "
                "WHERE scope=? AND state IN ('sending','uncertain')",
                (self.scope,),
            )

    def claim_delivery(self, key: str) -> str:
        now = int(time.time())
        with closing(self._connection()) as connection, connection:
            row = connection.execute(
                "SELECT state FROM feishu_deliveries WHERE scope = ? AND delivery_key = ?",
                (self.scope, key),
            ).fetchone()
            if row is not None:
                state = str(row[0])
                if state == "sending":
                    connection.execute(
                        "UPDATE feishu_deliveries SET state = 'uncertain', updated_at = ? "
                        "WHERE scope = ? AND delivery_key = ?",
                        (now, self.scope, key),
                    )
                    return "uncertain"
                return state
            connection.execute(
                "INSERT INTO feishu_deliveries(scope, delivery_key, state, updated_at) "
                "VALUES (?, ?, 'sending', ?)",
                (self.scope, key, now),
            )
        return "send"

    def mark_delivery(self, key: str, state: str) -> None:
        if state not in {"sent", "failed", "uncertain"}:
            raise ValueError("feishu delivery state is invalid")
        with closing(self._connection()) as connection, connection:
            connection.execute(
                "UPDATE feishu_deliveries SET state = ?, updated_at = ? "
                "WHERE scope = ? AND delivery_key = ?",
                (state, int(time.time()), self.scope, key),
            )

    def release_delivery(self, key: str) -> None:
        with closing(self._connection()) as connection, connection:
            connection.execute(
                "DELETE FROM feishu_deliveries "
                "WHERE scope = ? AND delivery_key = ? AND state = 'sending'",
                (self.scope, key),
            )

    def _connection(self) -> sqlite3.Connection:
        with self._lock:
            if self.path.exists() and stat.S_ISLNK(self.path.lstat().st_mode):
                raise RuntimeError("feishu state path is invalid")
            connection = sqlite3.connect(self.path, timeout=5)
            connection.execute("PRAGMA busy_timeout = 5000")
            connection.execute("PRAGMA secure_delete = ON")
            if not self._initialized:
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS feishu_pending(
                        scope TEXT NOT NULL,
                        turn_id TEXT NOT NULL,
                        channel_id TEXT NOT NULL,
                        thread_id TEXT NOT NULL,
                        client_message_id TEXT NOT NULL,
                        conversation_sha256 TEXT NOT NULL,
                        conversation_id TEXT NOT NULL,
                        state TEXT NOT NULL DEFAULT 'pending' CHECK(
                            state IN ('pending','completed','failed','uncertain')
                        ),
                        error_code TEXT,
                        PRIMARY KEY(scope, turn_id)
                    );
                    CREATE TABLE IF NOT EXISTS feishu_deliveries(
                        scope TEXT NOT NULL,
                        delivery_key TEXT NOT NULL,
                        state TEXT NOT NULL CHECK(state IN ('sending','sent','failed','uncertain')),
                        updated_at INTEGER NOT NULL,
                        PRIMARY KEY(scope, delivery_key)
                    );
                    """
                )
                columns = {
                    str(row[1])
                    for row in connection.execute("PRAGMA table_info(feishu_pending)")
                }
                if "state" not in columns:
                    connection.execute(
                        "ALTER TABLE feishu_pending ADD COLUMN state TEXT NOT NULL "
                        "DEFAULT 'pending' CHECK(state IN "
                        "('pending','completed','failed','uncertain'))"
                    )
                if "error_code" not in columns:
                    connection.execute(
                        "ALTER TABLE feishu_pending ADD COLUMN error_code TEXT"
                    )
                delivery_sql = str(
                    connection.execute(
                        "SELECT sql FROM sqlite_master WHERE type='table' "
                        "AND name='feishu_deliveries'"
                    ).fetchone()[0]
                )
                if "'failed'" not in delivery_sql:
                    connection.executescript(
                        """
                        ALTER TABLE feishu_deliveries RENAME TO feishu_deliveries_v1;
                        CREATE TABLE feishu_deliveries(
                            scope TEXT NOT NULL, delivery_key TEXT NOT NULL,
                            state TEXT NOT NULL CHECK(
                                state IN ('sending','sent','failed','uncertain')
                            ), updated_at INTEGER NOT NULL,
                            PRIMARY KEY(scope, delivery_key)
                        );
                        INSERT INTO feishu_deliveries
                        SELECT * FROM feishu_deliveries_v1;
                        DROP TABLE feishu_deliveries_v1;
                        """
                    )
                connection.commit()
                os.chmod(self.path, 0o600)
                self._initialized = True
            return connection


class FeishuMessageBotAdapter:
    """One official Feishu websocket worker; no listener or second Runtime."""

    def __init__(
        self,
        database_path: str | os.PathLike[str],
        *,
        channel_factory: Callable[[str, str], _MessageChannel] | None = None,
        client_factory: Callable[[], _HTTPClient] | None = None,
        connect_timeout_seconds: float = 10.0,
    ) -> None:
        if not 1 <= connect_timeout_seconds <= 30:
            raise ValueError("feishu connect timeout is invalid")
        self.database_path = Path(os.path.abspath(database_path))
        self.channel_factory = channel_factory or _default_channel
        self.client_factory = client_factory or _default_client
        self.connect_timeout_seconds = float(connect_timeout_seconds)
        self._owner: ChannelCredentialOwner | None = None
        self._dispatcher: ChannelRuntimeDispatcher | None = None
        self._store: _FeishuStore | None = None
        self._channel: _MessageChannel | None = None
        self._api: _FeishuAPI | None = None
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._lock = threading.RLock()
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
                raise RuntimeError("feishu Runtime is already bound")
            self._owner = owner
            self._dispatcher = dispatcher
            self._store = _FeishuStore(self.database_path, owner)

    def test(self, config: Mapping[str, Any]) -> ConnectorHealthResult:
        api: _FeishuAPI | None = None
        try:
            app_id, app_secret = _credentials(config)
            api = _FeishuAPI(app_id, app_secret, self.client_factory())
            api.validate()
            return ConnectorHealthResult(ConnectorHealth.CONNECTED)
        except _FeishuFailure as error:
            return ConnectorHealthResult(ConnectorHealth.ERROR, error.code)
        except Exception:
            return ConnectorHealthResult(
                ConnectorHealth.ERROR, "feishu_bot_transport_unavailable"
            )
        finally:
            if api is not None:
                api.close()

    def start(self, config: Mapping[str, Any]) -> ConnectorHealthResult:
        api: _FeishuAPI | None = None
        try:
            app_id, app_secret = _credentials(config)
            with self._lock:
                if self._dispatcher is None or self._store is None:
                    return ConnectorHealthResult(
                        ConnectorHealth.ERROR, "feishu_bot_runtime_unavailable"
                    )
                if self._thread is not None and self._thread.is_alive():
                    return ConnectorHealthResult(self._health, self._last_error)
            api = _FeishuAPI(app_id, app_secret, self.client_factory())
            api.validate()
            channel = self.channel_factory(app_id, app_secret)
            channel.on("message", self._accept_message)
            channel.on("reconnecting", self._reconnecting)
            channel.on("reconnected", self._reconnected)
            channel.on("error", self._channel_error)
            ready = threading.Event()
            with self._lock:
                self._api = api
                self._channel = channel
                self._stop_event = threading.Event()
                self._health = ConnectorHealth.AUTHENTICATING
                self._last_error = None
                self._thread = threading.Thread(
                    target=self._run,
                    args=(channel, ready),
                    name="emate-feishu-message-channel",
                    daemon=True,
                )
                self._thread.start()
            api = None
            if not ready.wait(self.connect_timeout_seconds + 1):
                self.stop(2)
                return ConnectorHealthResult(
                    ConnectorHealth.ERROR, "feishu_bot_connect_timeout"
                )
            with self._lock:
                result = ConnectorHealthResult(self._health, self._last_error)
            if result.health is ConnectorHealth.ERROR:
                self.stop(2)
            return result
        except _FeishuFailure as error:
            return ConnectorHealthResult(ConnectorHealth.ERROR, error.code)
        except Exception:
            return ConnectorHealthResult(
                ConnectorHealth.ERROR, "feishu_bot_transport_unavailable"
            )
        finally:
            if api is not None:
                api.close()

    def health(self) -> ConnectorHealthResult:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return ConnectorHealthResult(self._health, self._last_error)
            return ConnectorHealthResult(
                self._health if self._last_error else ConnectorHealth.DISABLED,
                self._last_error,
            )

    def resolve_uncertain(self) -> None:
        self._required_store().resolve_uncertain()

    def stop(self, timeout_seconds: float) -> bool:
        with self._lock:
            thread = self._thread
            channel = self._channel
            self._stop_event.set()
        if channel is not None:
            try:
                channel.stop(join_timeout=min(2.0, timeout_seconds))
            except Exception:
                pass
        if thread is not None:
            thread.join(timeout_seconds)
        stopped = thread is None or not thread.is_alive()
        if stopped:
            with self._lock:
                api = self._api
                self._thread = None
                self._channel = None
                self._api = None
                self._health = ConnectorHealth.DISABLED
                self._last_error = None
            if api is not None:
                api.close()
        return stopped

    def send_text(
        self,
        *,
        channel_id: str,
        conversation_id: str,
        text: str,
        idempotency_key: str,
    ) -> None:
        if (
            channel_id != "feishu"
            or not isinstance(conversation_id, str)
            or not conversation_id
            or len(conversation_id) > 512
            or not isinstance(text, str)
            or not text
            or not isinstance(idempotency_key, str)
            or not idempotency_key
        ):
            raise ValueError("feishu delivery is invalid")
        with self._lock:
            api = self._api
            store = self._store
        if api is None or store is None:
            raise _FeishuFailure("feishu_bot_not_running")
        chunks = _chunks(text)
        for index, chunk in enumerate(chunks):
            key = f"{idempotency_key}:{index + 1}:{len(chunks)}"
            state = store.claim_delivery(key)
            if state == "sent":
                continue
            if state == "failed":
                raise _FeishuFailure(
                    "feishu_bot_delivery_rejected", permanent=True
                )
            if state == "uncertain":
                raise _FeishuFailure(
                    "feishu_bot_delivery_uncertain", uncertain=True
                )
            try:
                api.send_text(conversation_id, chunk, key)
            except _FeishuFailure as error:
                if error.uncertain:
                    store.mark_delivery(key, "uncertain")
                elif error.permanent:
                    store.mark_delivery(key, "failed")
                else:
                    store.release_delivery(key)
                raise
            store.mark_delivery(key, "sent")

    def _run(self, channel: _MessageChannel, ready: threading.Event) -> None:
        async def serve() -> None:
            backoff = 0.1
            try:
                await channel.start_background(timeout=self.connect_timeout_seconds)
                if not channel.is_ready:
                    raise _FeishuFailure("feishu_bot_connect_timeout")
                self._set_ready_health()
                ready.set()
                while not self._stop_event.is_set():
                    try:
                        self._drain_pending()
                        self._set_ready_health()
                        backoff = 0.1
                    except _FeishuFailure as error:
                        self._set_health(
                            ConnectorHealth.DEGRADED
                            if error.uncertain
                            else ConnectorHealth.ERROR,
                            error.code,
                        )
                        if error.permanent:
                            return
                        await asyncio.sleep(backoff)
                        backoff = min(backoff * 2, 30.0)
                        continue
                    except Exception:
                        self._set_health(
                            ConnectorHealth.ERROR,
                            "feishu_bot_runtime_dispatch_failed",
                        )
                        await asyncio.sleep(backoff)
                        backoff = min(backoff * 2, 30.0)
                        continue
                    await asyncio.sleep(0.05)
            except _FeishuFailure as error:
                self._set_health(ConnectorHealth.ERROR, error.code)
                ready.set()
            except Exception:
                self._set_health(
                    ConnectorHealth.ERROR, "feishu_bot_transport_unavailable"
                )
                ready.set()
            finally:
                try:
                    channel.stop(join_timeout=2.0)
                except Exception:
                    pass

        try:
            asyncio.run(serve())
        finally:
            ready.set()

    def _accept_message(self, message: Any) -> None:
        try:
            message_id = getattr(message, "message_id", None)
            conversation_id = getattr(message, "chat_id", None)
            text = (
                getattr(message, "body_text", None)
                or getattr(message, "safe_content_text", None)
                or getattr(message, "content_text", None)
            )
            if not all(isinstance(value, str) and value for value in (
                message_id,
                conversation_id,
                text,
            )):
                return
            receipt = self._required_dispatcher().dispatch(
                ChannelInboundMessage(
                    channel_id="feishu",
                    conversation_id=conversation_id,
                    message_id=message_id,
                    text=text,
                )
            )
            self._required_store().add_pending(receipt, conversation_id)
        except Exception:
            self._set_health(
                ConnectorHealth.ERROR, "feishu_bot_runtime_dispatch_failed"
            )

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
            except ChannelTurnTerminalFailure as error:
                store.finish_pending(
                    receipt.turn_id,
                    "failed",
                    error.code.replace("channel_", "feishu_bot_", 1),
                )
                continue
            except _FeishuFailure as error:
                if error.uncertain:
                    store.finish_pending(receipt.turn_id, "uncertain", error.code)
                    self._set_health(ConnectorHealth.DEGRADED, error.code)
                    continue
                if error.permanent:
                    store.finish_pending(receipt.turn_id, "failed", error.code)
                    self._set_health(ConnectorHealth.ERROR, error.code)
                    continue
                raise
            if delivered:
                store.finish_pending(receipt.turn_id, "completed")

    def _set_ready_health(self) -> None:
        terminal = self._required_store().terminal_error()
        if terminal is None:
            self._set_health(ConnectorHealth.CONNECTED, None)
            return
        error, uncertain = terminal
        self._set_health(
            ConnectorHealth.DEGRADED if uncertain else ConnectorHealth.ERROR,
            error,
        )

    def _reconnecting(self) -> None:
        self._set_health(ConnectorHealth.DEGRADED, "feishu_bot_reconnecting")

    def _reconnected(self) -> None:
        self._set_health(ConnectorHealth.CONNECTED, None)

    def _channel_error(self, _error: Any) -> None:
        self._set_health(ConnectorHealth.ERROR, "feishu_bot_transport_unavailable")

    def _set_health(self, health: ConnectorHealth, error: str | None) -> None:
        with self._lock:
            self._health = health
            self._last_error = error

    def _required_dispatcher(self) -> ChannelRuntimeDispatcher:
        with self._lock:
            if self._dispatcher is None:
                raise _FeishuFailure(
                    "feishu_bot_runtime_unavailable", permanent=True
                )
            return self._dispatcher

    def _required_store(self) -> _FeishuStore:
        with self._lock:
            if self._store is None:
                raise _FeishuFailure(
                    "feishu_bot_runtime_unavailable", permanent=True
                )
            return self._store


def _credentials(config: Mapping[str, Any]) -> tuple[str, str]:
    if not isinstance(config, Mapping) or set(config) != {
        "feishu_app_id",
        "feishu_app_secret",
    }:
        raise _FeishuFailure("feishu_bot_configuration_invalid", permanent=True)
    app_id = config.get("feishu_app_id")
    app_secret = config.get("feishu_app_secret")
    if (
        not isinstance(app_id, str)
        or _APP_ID_RE.fullmatch(app_id) is None
        or not isinstance(app_secret, str)
        or not 8 <= len(app_secret) <= 512
        or not app_secret.isascii()
        or any(character.isspace() for character in app_secret)
    ):
        raise _FeishuFailure("feishu_bot_configuration_invalid", permanent=True)
    return app_id, app_secret


def _chunks(text: str) -> tuple[str, ...]:
    chunks: list[str] = []
    remaining = text
    while remaining:
        split = min(len(remaining), _MAX_TEXT_CHARS)
        if split < len(remaining):
            newline = remaining.rfind("\n", _MAX_TEXT_CHARS // 2, split)
            if newline >= 0:
                split = newline + 1
        chunks.append(remaining[:split])
        remaining = remaining[split:]
    return tuple(chunks)


def _default_client() -> httpx.Client:
    return httpx.Client(
        base_url="https://open.feishu.cn",
        timeout=httpx.Timeout(connect=5, read=10, write=10, pool=5),
        limits=httpx.Limits(max_connections=2, max_keepalive_connections=2),
        follow_redirects=False,
        trust_env=False,
        headers={"User-Agent": f"e-Mate/{__version__} FeishuMessageBot/1"},
    )


def _default_channel(app_id: str, app_secret: str) -> _MessageChannel:
    try:
        from lark_channel import FeishuChannel
        from lark_channel.channel.config import (
            InboundConfig,
            MediaCapabilities,
            SecurityConfig,
            TransportConfig,
        )
    except ImportError:
        raise _FeishuFailure(
            "feishu_bot_dependency_missing", permanent=True
        ) from None
    return FeishuChannel(
        app_id=app_id,
        app_secret=app_secret,
        inbound=InboundConfig(
            media_capabilities=MediaCapabilities(
                image=False,
                audio=False,
                video=False,
                file=False,
                sticker=False,
            ),
            include_raw=False,
            emit_raw_events=False,
        ),
        security=SecurityConfig(mode="strict", strict_content_text=True),
        transport=TransportConfig(
            kind="ws",
            auto_reconnect=True,
            trust_env_proxy=False,
            handshake_timeout_seconds=8,
        ),
    )


__all__ = ["FeishuMessageBotAdapter"]
