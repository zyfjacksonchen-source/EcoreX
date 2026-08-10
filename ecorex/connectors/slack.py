"""Built-in Slack Socket Mode transport for the product channel boundary.

Users must enable Socket Mode, create an app-level ``connections:write`` token,
subscribe the bot to message/app-mention events, and grant ``chat:write`` plus
the matching history scopes.  No public listener or predecessor bridge exists
in this adapter.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import closing
from dataclasses import dataclass
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
from urllib.parse import urlsplit

import httpx
from websockets.exceptions import ConnectionClosed
from websockets.sync.client import connect as websocket_connect

from ecorex import __version__

from .channel_runtime import (
    ChannelInboundMessage,
    ChannelRuntimeDispatcher,
    ChannelTurnTerminalFailure,
    ChannelTurnReceipt,
)
from .channel_self_service import ChannelCredentialOwner
from .models import ConnectorHealth, ConnectorHealthResult


_BOT_TOKEN_RE = re.compile(r"^xoxb-[A-Za-z0-9-]{10,512}$")
_APP_TOKEN_RE = re.compile(r"^xapp-[A-Za-z0-9-]{10,512}$")
_SLACK_ID_RE = re.compile(r"^[A-Z][A-Z0-9]{1,63}$")
_TIMESTAMP_RE = re.compile(r"^[0-9]{1,20}\.[0-9]{1,20}$")
_ENVELOPE_RE = re.compile(r"^[A-Za-z0-9._:-]{1,256}$")
_ERROR_RE = re.compile(r"^slack_[a-z0-9_]{1,124}$")
_MAX_RESPONSE_BYTES = 1024 * 1024
_MAX_SLACK_TEXT = 4_000


class _HTTPClient(Protocol):
    def post(
        self,
        path: str,
        *,
        headers: Mapping[str, str],
        json: Mapping[str, Any],
    ) -> httpx.Response: ...

    def close(self) -> None: ...


class _Socket(Protocol):
    def recv(self, timeout: float | None = None) -> str | bytes: ...

    def send(self, message: str) -> None: ...

    def close(self, code: int = 1000, reason: str = "") -> None: ...


class _SlackFailure(RuntimeError):
    def __init__(self, code: str, *, uncertain: bool = False, permanent: bool = False):
        super().__init__(code)
        self.code = code
        self.uncertain = uncertain
        self.permanent = permanent


@dataclass(frozen=True, slots=True)
class _JournalEvent:
    event_key: str
    conversation_id: str
    message_id: str
    text: str


class _SlackStore:
    """Tenant journal; Slack identifiers stay only in this transport-private DB."""

    def __init__(self, path: str | os.PathLike[str], owner: ChannelCredentialOwner):
        self.path = Path(os.path.abspath(path))
        self.scope = hashlib.sha256(
            f"{owner.organization_id}\0{owner.account_id}".encode()
        ).hexdigest()
        self._lock = threading.RLock()
        self._initialized = False

    def record(
        self,
        *,
        envelope_id: str,
        conversation_id: str,
        message_id: str,
        text: str,
    ) -> None:
        event_key = hashlib.sha256(
            f"{conversation_id}\0{message_id}".encode()
        ).hexdigest()
        with closing(self._connection()) as connection, connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO slack_events(
                    scope, event_key, envelope_id, conversation_id,
                    message_id, text, state
                ) VALUES (?, ?, ?, ?, ?, ?, 'received')
                """,
                (
                    self.scope,
                    event_key,
                    envelope_id,
                    conversation_id,
                    message_id,
                    text,
                ),
            )

    def received(self) -> tuple[_JournalEvent, ...]:
        with closing(self._connection()) as connection:
            rows = connection.execute(
                """
                SELECT event_key, conversation_id, message_id, text
                FROM slack_events
                WHERE scope = ? AND state = 'received'
                ORDER BY rowid
                """,
                (self.scope,),
            ).fetchall()
        return tuple(_JournalEvent(*(str(value) for value in row)) for row in rows)

    def set_outbound(self, event_key: str, receipt: ChannelTurnReceipt) -> None:
        with closing(self._connection()) as connection, connection:
            connection.execute(
                """
                UPDATE slack_events
                SET state = 'outbound', channel_id = ?, thread_id = ?, turn_id = ?,
                    client_message_id = ?, conversation_sha256 = ?
                WHERE scope = ? AND event_key = ? AND state = 'received'
                """,
                (
                    receipt.channel_id,
                    receipt.thread_id,
                    receipt.turn_id,
                    receipt.client_message_id,
                    receipt.conversation_sha256,
                    self.scope,
                    event_key,
                ),
            )

    def outbound(self) -> tuple[tuple[ChannelTurnReceipt, str], ...]:
        with closing(self._connection()) as connection:
            rows = connection.execute(
                """
                SELECT channel_id, thread_id, turn_id, client_message_id,
                       conversation_sha256, conversation_id
                FROM slack_events
                WHERE scope = ? AND state = 'outbound'
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

    def finish(
        self, turn_id: str, state: str, error_code: str | None = None
    ) -> None:
        if state not in {"completed", "failed", "uncertain"}:
            raise ValueError("Slack event terminal state is invalid")
        if (state == "completed") != (error_code is None) or (
            error_code is not None and _ERROR_RE.fullmatch(error_code) is None
        ):
            raise ValueError("Slack event terminal error is invalid")
        with closing(self._connection()) as connection, connection:
            connection.execute(
                """
                UPDATE slack_events
                SET state = ?, error_code = ?, envelope_id = event_key,
                    conversation_id = '', message_id = '', text = '',
                    channel_id = NULL, thread_id = NULL, turn_id = NULL,
                    client_message_id = NULL, conversation_sha256 = NULL
                WHERE scope = ? AND turn_id = ? AND state = 'outbound'
                """,
                (state, error_code, self.scope, turn_id),
            )

    def terminal_error(self) -> tuple[str, bool] | None:
        with closing(self._connection()) as connection:
            row = connection.execute(
                "SELECT state,error_code FROM slack_events WHERE scope = ? "
                "AND state = 'uncertain' LIMIT 1",
                (self.scope,),
            ).fetchone()
            delivery = connection.execute(
                "SELECT state FROM slack_deliveries WHERE scope=? "
                "AND state IN ('sending','uncertain') LIMIT 1",
                (self.scope,),
            ).fetchone()
        if row is None:
            return (
                ("slack_delivery_uncertain", True)
                if delivery is not None
                else None
            )
        uncertain = str(row[0]) == "uncertain"
        code = str(row[1] or "")
        return (
            code if _ERROR_RE.fullmatch(code) else (
                "slack_delivery_uncertain" if uncertain else "slack_delivery_rejected"
            ),
            uncertain,
        )

    def resolve_uncertain(self) -> None:
        with closing(self._connection()) as connection, connection:
            connection.execute(
                "UPDATE slack_events SET state='failed' "
                "WHERE scope=? AND state='uncertain'",
                (self.scope,),
            )
            connection.execute(
                "UPDATE slack_deliveries SET state='failed' "
                "WHERE scope=? AND state IN ('sending','uncertain')",
                (self.scope,),
            )

    def has_uncertain(self) -> bool:
        terminal = self.terminal_error()
        return terminal is not None and terminal[1]

    def claim_delivery(self, key: str) -> str:
        now = int(time.time())
        with closing(self._connection()) as connection, connection:
            row = connection.execute(
                "SELECT state FROM slack_deliveries WHERE scope = ? AND delivery_key = ?",
                (self.scope, key),
            ).fetchone()
            if row is not None:
                state = str(row[0])
                if state == "sending":
                    connection.execute(
                        "UPDATE slack_deliveries SET state = 'uncertain', updated_at = ? "
                        "WHERE scope = ? AND delivery_key = ?",
                        (now, self.scope, key),
                    )
                    return "uncertain"
                return state
            connection.execute(
                "INSERT INTO slack_deliveries(scope, delivery_key, state, updated_at) "
                "VALUES (?, ?, 'sending', ?)",
                (self.scope, key, now),
            )
        return "send"

    def mark_delivery(self, key: str, state: str) -> None:
        if state not in {"sent", "failed", "uncertain"}:
            raise ValueError("Slack delivery state is invalid")
        with closing(self._connection()) as connection, connection:
            connection.execute(
                "UPDATE slack_deliveries SET state = ?, updated_at = ? "
                "WHERE scope = ? AND delivery_key = ?",
                (state, int(time.time()), self.scope, key),
            )

    def release_delivery(self, key: str) -> None:
        with closing(self._connection()) as connection, connection:
            connection.execute(
                "DELETE FROM slack_deliveries "
                "WHERE scope = ? AND delivery_key = ? AND state = 'sending'",
                (self.scope, key),
            )

    def _connection(self) -> sqlite3.Connection:
        with self._lock:
            if self.path.exists() and stat.S_ISLNK(self.path.lstat().st_mode):
                raise RuntimeError("Slack state path is invalid")
            connection = sqlite3.connect(self.path, timeout=5)
            connection.execute("PRAGMA busy_timeout = 5000")
            connection.execute("PRAGMA secure_delete = ON")
            if not self._initialized:
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS slack_events(
                        scope TEXT NOT NULL,
                        event_key TEXT NOT NULL,
                        envelope_id TEXT NOT NULL,
                        conversation_id TEXT NOT NULL,
                        message_id TEXT NOT NULL,
                        text TEXT NOT NULL,
                        state TEXT NOT NULL CHECK(
                            state IN ('received','outbound','completed','failed','uncertain')
                        ),
                        channel_id TEXT,
                        thread_id TEXT,
                        turn_id TEXT,
                        client_message_id TEXT,
                        conversation_sha256 TEXT,
                        error_code TEXT,
                        PRIMARY KEY(scope, event_key),
                        UNIQUE(scope, envelope_id)
                    );
                    CREATE TABLE IF NOT EXISTS slack_deliveries(
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
                    for row in connection.execute("PRAGMA table_info(slack_events)")
                }
                if "error_code" not in columns:
                    connection.execute(
                        "ALTER TABLE slack_events ADD COLUMN error_code TEXT"
                    )
                delivery_sql = str(
                    connection.execute(
                        "SELECT sql FROM sqlite_master WHERE type='table' "
                        "AND name='slack_deliveries'"
                    ).fetchone()[0]
                )
                if "'failed'" not in delivery_sql:
                    connection.executescript(
                        """
                        ALTER TABLE slack_deliveries RENAME TO slack_deliveries_v1;
                        CREATE TABLE slack_deliveries(
                            scope TEXT NOT NULL, delivery_key TEXT NOT NULL,
                            state TEXT NOT NULL CHECK(
                                state IN ('sending','sent','failed','uncertain')
                            ), updated_at INTEGER NOT NULL,
                            PRIMARY KEY(scope, delivery_key)
                        );
                        INSERT INTO slack_deliveries
                        SELECT * FROM slack_deliveries_v1;
                        DROP TABLE slack_deliveries_v1;
                        """
                    )
                connection.commit()
                os.chmod(self.path, 0o600)
                self._initialized = True
            return connection


class SlackSocketModeAdapter:
    """One Slack Socket Mode worker; no public listener or second Runtime."""

    def __init__(
        self,
        database_path: str | os.PathLike[str],
        *,
        client_factory: Callable[[], _HTTPClient] | None = None,
        socket_factory: Callable[[str], _Socket] | None = None,
    ) -> None:
        self.database_path = Path(os.path.abspath(database_path))
        self.client_factory = client_factory or self._default_client
        self.socket_factory = socket_factory or self._default_socket
        self._owner: ChannelCredentialOwner | None = None
        self._dispatcher: ChannelRuntimeDispatcher | None = None
        self._store: _SlackStore | None = None
        self._client: _HTTPClient | None = None
        self._socket: _Socket | None = None
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._lock = threading.RLock()
        self._health = ConnectorHealth.DISABLED
        self._last_error: str | None = None
        self._bot_user_id: str | None = None
        self._bot_token: str | None = None

    def bind_runtime(
        self,
        owner: ChannelCredentialOwner,
        dispatcher: ChannelRuntimeDispatcher,
    ) -> None:
        with self._lock:
            if self._dispatcher is not None and (
                self._owner != owner or self._dispatcher is not dispatcher
            ):
                raise RuntimeError("Slack Runtime is already bound")
            self._owner = owner
            self._dispatcher = dispatcher
            self._store = _SlackStore(self.database_path, owner)

    def test(self, config: Mapping[str, Any]) -> ConnectorHealthResult:
        try:
            bot_token, app_token = _tokens(config)
            client = self.client_factory()
            try:
                self._auth_test(client, bot_token)
                self._open_socket_url(client, app_token)
            finally:
                _close(client)
            return ConnectorHealthResult(ConnectorHealth.CONNECTED)
        except _SlackFailure as error:
            return ConnectorHealthResult(ConnectorHealth.ERROR, error.code)
        except Exception:
            return ConnectorHealthResult(
                ConnectorHealth.ERROR, "slack_transport_unavailable"
            )

    def start(self, config: Mapping[str, Any]) -> ConnectorHealthResult:
        try:
            bot_token, app_token = _tokens(config)
            with self._lock:
                if self._dispatcher is None or self._store is None:
                    return ConnectorHealthResult(
                        ConnectorHealth.ERROR, "slack_runtime_unavailable"
                    )
                if self._thread is not None and self._thread.is_alive():
                    return ConnectorHealthResult(self._health, self._last_error)
            client = self.client_factory()
            socket: _Socket | None = None
            try:
                bot_user_id = self._auth_test(client, bot_token)
                socket = self._new_socket(client, app_token)
                store = self._required_store()
                store.has_uncertain()
                terminal = store.terminal_error()
                with self._lock:
                    self._client = client
                    self._socket = socket
                    self._bot_user_id = bot_user_id
                    self._bot_token = bot_token
                    self._stop_event = threading.Event()
                    self._health = (
                        ConnectorHealth.CONNECTED
                        if terminal is None
                        else ConnectorHealth.DEGRADED
                        if terminal[1]
                        else ConnectorHealth.ERROR
                    )
                    self._last_error = terminal[0] if terminal else None
                    self._thread = threading.Thread(
                        target=self._run,
                        args=(client, socket, app_token),
                        name="emate-slack-channel",
                        daemon=True,
                    )
                    self._thread.start()
            except Exception:
                _close_socket(socket)
                _close(client)
                raise
            return ConnectorHealthResult(self._health, self._last_error)
        except _SlackFailure as error:
            return ConnectorHealthResult(ConnectorHealth.ERROR, error.code)
        except Exception:
            return ConnectorHealthResult(
                ConnectorHealth.ERROR, "slack_transport_unavailable"
            )

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
            socket = self._socket
            self._stop_event.set()
        _close_socket(socket)
        if thread is not None:
            thread.join(timeout_seconds)
        stopped = thread is None or not thread.is_alive()
        if stopped:
            with self._lock:
                self._thread = None
                self._client = None
                self._socket = None
                self._bot_user_id = None
                self._bot_token = None
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
        if channel_id != "slack" or not isinstance(text, str) or not text:
            raise ValueError("Slack delivery is invalid")
        channel, thread_ts = _split_conversation(conversation_id)
        with self._lock:
            client = self._client
            store = self._store
        if client is None or store is None:
            raise _SlackFailure("slack_not_running", permanent=True)
        for index, chunk in enumerate(_chunks(text)):
            key = f"{idempotency_key}:{index + 1}"
            state = store.claim_delivery(key)
            if state == "sent":
                continue
            if state == "failed":
                raise _SlackFailure("slack_delivery_rejected", permanent=True)
            if state == "uncertain":
                raise _SlackFailure("slack_delivery_uncertain", uncertain=True)
            body: dict[str, Any] = {
                "channel": channel,
                "text": chunk,
                "unfurl_links": False,
                "unfurl_media": False,
            }
            if thread_ts is not None:
                body["thread_ts"] = thread_ts
            try:
                payload = self._request(
                    client,
                    "chat.postMessage",
                    token=self._required_bot_token(),
                    body=body,
                    operation="delivery",
                )
                if (
                    payload.get("channel") != channel
                    or not isinstance(payload.get("ts"), str)
                    or _TIMESTAMP_RE.fullmatch(str(payload["ts"])) is None
                ):
                    raise _SlackFailure(
                        "slack_delivery_uncertain", uncertain=True
                    )
            except _SlackFailure as error:
                if error.uncertain:
                    store.mark_delivery(key, "uncertain")
                elif error.permanent:
                    store.mark_delivery(key, "failed")
                else:
                    store.release_delivery(key)
                raise
            store.mark_delivery(key, "sent")

    def _run(
        self,
        client: _HTTPClient,
        socket: _Socket,
        app_token: str,
    ) -> None:
        backoff = 1.0
        current = socket
        try:
            while not self._stop_event.is_set():
                try:
                    self._drain_received()
                    self._drain_outbound()
                    try:
                        frame = current.recv(timeout=0.25)
                    except (ConnectionClosed, OSError):
                        raise _SlackFailure("slack_transport_unavailable") from None
                    self._handle_frame(current, frame)
                    self._set_ready_health()
                    backoff = 1.0
                except TimeoutError:
                    self._set_ready_health()
                except _SlackFailure as error:
                    self._set_health(
                        ConnectorHealth.DEGRADED
                        if error.uncertain
                        else ConnectorHealth.ERROR,
                        error.code,
                    )
                    if error.permanent or self._stop_event.wait(backoff):
                        return
                    backoff = min(backoff * 2, 30.0)
                    _close_socket(current)
                    try:
                        current = self._new_socket(client, app_token)
                    except _SlackFailure as reconnect_error:
                        self._set_health(
                            ConnectorHealth.ERROR, reconnect_error.code
                        )
                        continue
                    with self._lock:
                        self._socket = current
                except Exception:
                    self._set_health(
                        ConnectorHealth.ERROR, "slack_runtime_dispatch_failed"
                    )
                    if self._stop_event.wait(backoff):
                        return
                    backoff = min(backoff * 2, 30.0)
                    _close_socket(current)
                    try:
                        current = self._new_socket(client, app_token)
                    except _SlackFailure as reconnect_error:
                        self._set_health(
                            ConnectorHealth.ERROR, reconnect_error.code
                        )
                        continue
                    with self._lock:
                        self._socket = current
        finally:
            _close_socket(current)
            _close(client)
            with self._lock:
                if self._client is client:
                    self._client = None
                    self._bot_token = None
                    self._bot_user_id = None
                if self._socket is current:
                    self._socket = None

    def _handle_frame(self, socket: _Socket, frame: str | bytes) -> None:
        if not isinstance(frame, str) or len(frame.encode("utf-8")) > _MAX_RESPONSE_BYTES:
            raise _SlackFailure("slack_provider_response_invalid")
        try:
            envelope = json.loads(frame)
        except ValueError:
            raise _SlackFailure("slack_provider_response_invalid") from None
        if not isinstance(envelope, dict):
            raise _SlackFailure("slack_provider_response_invalid")
        if envelope.get("type") == "hello":
            return
        if envelope.get("type") == "disconnect":
            raise _SlackFailure("slack_reconnect_requested")
        envelope_id = envelope.get("envelope_id")
        if not isinstance(envelope_id, str) or _ENVELOPE_RE.fullmatch(envelope_id) is None:
            return
        event = self._event(envelope)
        if event is not None:
            self._required_store().record(
                envelope_id=envelope_id,
                conversation_id=event.conversation_id,
                message_id=event.message_id,
                text=event.text,
            )
        try:
            socket.send(
                json.dumps(
                    {"envelope_id": envelope_id},
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
        except Exception:
            raise _SlackFailure("slack_acknowledgement_failed") from None
        if event is not None:
            self._drain_received()

    def _event(self, envelope: Mapping[str, Any]) -> _JournalEvent | None:
        if envelope.get("type") != "events_api":
            return None
        payload = envelope.get("payload")
        if not isinstance(payload, Mapping) or payload.get("type") != "event_callback":
            return None
        event = payload.get("event")
        if not isinstance(event, Mapping) or event.get("type") not in {
            "message",
            "app_mention",
        }:
            return None
        if event.get("type") == "message" and event.get("channel_type") != "im":
            return None
        if event.get("subtype") not in {None, ""} or event.get("hidden") is True:
            return None
        channel = event.get("channel")
        timestamp = event.get("ts")
        user = event.get("user")
        text = event.get("text")
        bot_user_id = self._required_bot_user_id()
        if (
            not isinstance(channel, str)
            or _SLACK_ID_RE.fullmatch(channel) is None
            or not isinstance(timestamp, str)
            or _TIMESTAMP_RE.fullmatch(timestamp) is None
            or not isinstance(user, str)
            or _SLACK_ID_RE.fullmatch(user) is None
            or user == bot_user_id
            or event.get("bot_id") is not None
            or not isinstance(text, str)
        ):
            return None
        text = re.sub(
            rf"<@{re.escape(bot_user_id)}(?:\|[^>]+)?>",
            "",
            text,
        ).strip()
        if not text:
            return None
        thread_ts = event.get("thread_ts")
        if thread_ts is not None and (
            not isinstance(thread_ts, str)
            or _TIMESTAMP_RE.fullmatch(thread_ts) is None
        ):
            return None
        conversation_id = channel if thread_ts is None else f"{channel}/{thread_ts}"
        event_key = hashlib.sha256(
            f"{conversation_id}\0{timestamp}".encode()
        ).hexdigest()
        return _JournalEvent(event_key, conversation_id, timestamp, text)

    def _drain_received(self) -> None:
        dispatcher = self._required_dispatcher()
        store = self._required_store()
        for event in store.received():
            receipt = dispatcher.dispatch(
                ChannelInboundMessage(
                    channel_id="slack",
                    conversation_id=event.conversation_id,
                    message_id=event.message_id,
                    text=event.text,
                )
            )
            store.set_outbound(event.event_key, receipt)

    def _drain_outbound(self) -> None:
        dispatcher = self._required_dispatcher()
        store = self._required_store()
        for receipt, conversation_id in store.outbound():
            try:
                delivered = dispatcher.deliver(
                    receipt,
                    conversation_id=conversation_id,
                    transport=self,
                )
            except ChannelTurnTerminalFailure as error:
                store.finish(
                    receipt.turn_id,
                    "failed",
                    error.code.replace("channel_", "slack_", 1),
                )
                continue
            except _SlackFailure as error:
                if error.uncertain:
                    store.finish(receipt.turn_id, "uncertain", error.code)
                    self._set_health(
                        ConnectorHealth.DEGRADED, "slack_delivery_uncertain"
                    )
                    continue
                if error.permanent:
                    store.finish(receipt.turn_id, "failed", error.code)
                    self._set_health(ConnectorHealth.ERROR, error.code)
                    continue
                raise
            if delivered:
                store.finish(receipt.turn_id, "completed")

    def _auth_test(self, client: _HTTPClient, token: str) -> str:
        payload = self._request(
            client, "auth.test", token=token, body={}, operation="auth"
        )
        user_id = payload.get("user_id")
        team_id = payload.get("team_id")
        if (
            not isinstance(user_id, str)
            or _SLACK_ID_RE.fullmatch(user_id) is None
            or not isinstance(team_id, str)
            or _SLACK_ID_RE.fullmatch(team_id) is None
        ):
            raise _SlackFailure("slack_provider_response_invalid")
        return user_id

    def _open_socket_url(self, client: _HTTPClient, token: str) -> str:
        payload = self._request(
            client,
            "apps.connections.open",
            token=token,
            body={},
            operation="socket",
        )
        url = payload.get("url")
        if not isinstance(url, str) or len(url) > 4096:
            raise _SlackFailure("slack_provider_response_invalid")
        parsed = urlsplit(url)
        host = (parsed.hostname or "").casefold()
        if (
            parsed.scheme != "wss"
            or not host.endswith(".slack.com")
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port not in {None, 443}
            or bool(parsed.fragment)
        ):
            raise _SlackFailure("slack_provider_response_invalid")
        return url

    def _new_socket(self, client: _HTTPClient, app_token: str) -> _Socket:
        url = self._open_socket_url(client, app_token)
        socket: _Socket | None = None
        try:
            socket = self.socket_factory(url)
            hello = socket.recv(timeout=4)
            if not isinstance(hello, str):
                raise ValueError
            payload = json.loads(hello)
            if not isinstance(payload, dict) or payload.get("type") != "hello":
                raise ValueError
            return socket
        except Exception:
            _close_socket(socket)
            raise _SlackFailure("slack_transport_unavailable") from None

    def _request(
        self,
        client: _HTTPClient,
        path: str,
        *,
        token: str,
        body: Mapping[str, Any],
        operation: str,
    ) -> Mapping[str, Any]:
        delivery = operation == "delivery"
        try:
            response = client.post(
                path,
                headers={"Authorization": f"Bearer {token}"},
                json=body,
            )
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout):
            raise _SlackFailure("slack_transport_unavailable") from None
        except (httpx.TimeoutException, httpx.TransportError):
            raise _SlackFailure(
                "slack_delivery_uncertain" if delivery else "slack_transport_unavailable",
                uncertain=delivery,
            ) from None
        except Exception:
            raise _SlackFailure(
                "slack_delivery_uncertain" if delivery else "slack_transport_unavailable",
                uncertain=delivery,
            ) from None
        if len(response.content) > _MAX_RESPONSE_BYTES:
            raise _SlackFailure(
                "slack_delivery_uncertain" if delivery else "slack_provider_response_invalid",
                uncertain=delivery,
            )
        if response.status_code >= 500:
            raise _SlackFailure(
                "slack_delivery_uncertain" if delivery else "slack_transport_unavailable",
                uncertain=delivery,
            )
        if response.status_code == 429:
            raise _SlackFailure("slack_rate_limited")
        if response.status_code != 200:
            raise _SlackFailure(
                "slack_delivery_rejected" if delivery else "slack_provider_rejected",
                permanent=delivery,
            )
        try:
            payload = response.json()
        except ValueError:
            raise _SlackFailure(
                "slack_delivery_uncertain" if delivery else "slack_provider_response_invalid",
                uncertain=delivery,
            ) from None
        if not isinstance(payload, dict):
            raise _SlackFailure(
                "slack_delivery_uncertain" if delivery else "slack_provider_response_invalid",
                uncertain=delivery,
            )
        if payload.get("ok") is True:
            return payload
        error = str(payload.get("error") or "")
        if operation == "auth" and error in {
            "account_inactive",
            "invalid_auth",
            "not_authed",
            "token_expired",
            "token_revoked",
        }:
            raise _SlackFailure("slack_auth_rejected", permanent=True)
        if operation == "socket":
            if error == "missing_scope":
                raise _SlackFailure("slack_socket_mode_scope_missing", permanent=True)
            if error in {
                "account_inactive",
                "invalid_auth",
                "not_allowed_token_type",
                "not_authed",
                "token_expired",
                "token_revoked",
            }:
                raise _SlackFailure("slack_socket_mode_rejected", permanent=True)
        if delivery:
            raise _SlackFailure("slack_delivery_rejected", permanent=True)
        raise _SlackFailure("slack_provider_rejected")

    def _set_ready_health(self) -> None:
        store = self._required_store()
        terminal = store.terminal_error()
        if terminal is None:
            self._set_health(ConnectorHealth.CONNECTED, None)
            return
        error, uncertain = terminal
        self._set_health(
            ConnectorHealth.DEGRADED if uncertain else ConnectorHealth.ERROR,
            error,
        )

    def _set_health(self, health: ConnectorHealth, error: str | None) -> None:
        with self._lock:
            self._health = health
            self._last_error = error

    def _required_dispatcher(self) -> ChannelRuntimeDispatcher:
        with self._lock:
            if self._dispatcher is None:
                raise _SlackFailure("slack_runtime_unavailable", permanent=True)
            return self._dispatcher

    def _required_store(self) -> _SlackStore:
        with self._lock:
            if self._store is None:
                raise _SlackFailure("slack_runtime_unavailable", permanent=True)
            return self._store

    def _required_bot_user_id(self) -> str:
        with self._lock:
            if self._bot_user_id is None:
                raise _SlackFailure("slack_runtime_unavailable", permanent=True)
            return self._bot_user_id

    def _required_bot_token(self) -> str:
        with self._lock:
            client = self._client
            token = self._bot_token
        if client is None:
            raise _SlackFailure("slack_not_running", permanent=True)
        # The HTTP client's headers never retain credentials. The enabled
        # instance's bot token is intentionally kept only in this live adapter.
        if not isinstance(token, str):
            raise _SlackFailure("slack_not_running", permanent=True)
        return token

    @staticmethod
    def _default_client() -> httpx.Client:
        return httpx.Client(
            base_url="https://slack.com/api/",
            timeout=httpx.Timeout(connect=4, read=4, write=4, pool=4),
            limits=httpx.Limits(max_connections=2, max_keepalive_connections=1),
            follow_redirects=False,
            headers={"User-Agent": f"e-Mate/{__version__} SlackChannel/1"},
        )

    @staticmethod
    def _default_socket(url: str) -> _Socket:
        return websocket_connect(
            url,
            open_timeout=4,
            ping_interval=20,
            ping_timeout=20,
            close_timeout=1,
            max_size=_MAX_RESPONSE_BYTES,
            max_queue=16,
            user_agent_header=f"e-Mate/{__version__} SlackChannel/1",
        )


def _tokens(config: Mapping[str, Any]) -> tuple[str, str]:
    if not isinstance(config, Mapping) or set(config) != {
        "slack_bot_token",
        "slack_app_token",
    }:
        raise _SlackFailure("slack_configuration_invalid", permanent=True)
    bot_token = config.get("slack_bot_token")
    app_token = config.get("slack_app_token")
    if (
        not isinstance(bot_token, str)
        or _BOT_TOKEN_RE.fullmatch(bot_token) is None
        or not isinstance(app_token, str)
        or _APP_TOKEN_RE.fullmatch(app_token) is None
    ):
        raise _SlackFailure("slack_configuration_invalid", permanent=True)
    return bot_token, app_token


def _split_conversation(value: str) -> tuple[str, str | None]:
    if not isinstance(value, str):
        raise ValueError("Slack conversation is invalid")
    channel, separator, thread_ts = value.partition("/")
    if _SLACK_ID_RE.fullmatch(channel) is None or (
        separator and _TIMESTAMP_RE.fullmatch(thread_ts) is None
    ):
        raise ValueError("Slack conversation is invalid")
    return channel, thread_ts if separator else None


def _chunks(text: str) -> tuple[str, ...]:
    chunks: list[str] = []
    remaining = text
    while remaining:
        split = min(len(remaining), _MAX_SLACK_TEXT)
        if split < len(remaining):
            newline = remaining.rfind("\n", _MAX_SLACK_TEXT // 2, split)
            if newline >= 0:
                split = newline + 1
        chunks.append(remaining[:split])
        remaining = remaining[split:]
    return tuple(chunks)


def _close(client: _HTTPClient | None) -> None:
    if client is None:
        return
    try:
        client.close()
    except Exception:
        pass


def _close_socket(socket: _Socket | None) -> None:
    if socket is None:
        return
    try:
        socket.close()
    except Exception:
        pass


__all__ = ["SlackSocketModeAdapter"]
