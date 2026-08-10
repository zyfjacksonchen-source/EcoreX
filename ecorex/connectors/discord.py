"""Built-in Discord Gateway transport for the product channel boundary."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import closing
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import random
import re
import sqlite3
import stat
import threading
import time
from typing import Any, Protocol
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

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


_TOKEN_RE = re.compile(r"^[A-Za-z0-9_.-]{24,256}$")
_SNOWFLAKE_RE = re.compile(r"^[0-9]{1,20}$")
_SESSION_RE = re.compile(r"^[A-Za-z0-9._:-]{1,256}$")
_ERROR_RE = re.compile(r"^discord_[a-z0-9_]{1,124}$")
_MAX_RESPONSE_BYTES = 1024 * 1024
_MAX_DISCORD_TEXT = 2_000
_MAX_INBOUND_TEXT = 1_000_000
# The adapter only accepts DMs and explicit bot mentions, whose content Discord
# exposes without the privileged MESSAGE_CONTENT intent.
_INTENTS = (1 << 9) | (1 << 12)
_NON_RESUMABLE_CLOSE_CODES = frozenset(
    {4003, 4004, 4005, 4007, 4009, 4010, 4011, 4012, 4013, 4014}
)


class _HTTPClient(Protocol):
    def get(self, path: str, *, headers: Mapping[str, str]) -> httpx.Response: ...

    def patch(
        self,
        path: str,
        *,
        headers: Mapping[str, str],
        json: Mapping[str, Any],
    ) -> httpx.Response: ...

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


class _DiscordFailure(RuntimeError):
    def __init__(
        self,
        code: str,
        *,
        uncertain: bool = False,
        permanent: bool = False,
        reset_session: bool = False,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.uncertain = uncertain
        self.permanent = permanent
        self.reset_session = reset_session


@dataclass(frozen=True, slots=True)
class _JournalEvent:
    event_key: str
    conversation_id: str
    message_id: str
    text: str


@dataclass(frozen=True, slots=True)
class _Session:
    session_id: str
    resume_url: str
    sequence: int


class _DiscordStore:
    """Tenant journal; Discord identifiers stay in this transport-private DB."""

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
                INSERT OR IGNORE INTO discord_events(
                    scope, event_key, conversation_id, message_id, text, state
                ) VALUES (?, ?, ?, ?, ?, 'received')
                """,
                (self.scope, event_key, conversation_id, message_id, text),
            )

    def received(self) -> tuple[_JournalEvent, ...]:
        with closing(self._connection()) as connection:
            rows = connection.execute(
                """
                SELECT event_key, conversation_id, message_id, text
                FROM discord_events
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
                UPDATE discord_events
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
                FROM discord_events
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
            raise ValueError("Discord event terminal state is invalid")
        if (state == "completed") != (error_code is None) or (
            error_code is not None and _ERROR_RE.fullmatch(error_code) is None
        ):
            raise ValueError("Discord event terminal error is invalid")
        with closing(self._connection()) as connection, connection:
            connection.execute(
                """
                UPDATE discord_events
                SET state = ?, error_code = ?, conversation_id = '', message_id = '', text = '',
                    channel_id = NULL, thread_id = NULL, turn_id = NULL,
                    client_message_id = NULL, conversation_sha256 = NULL
                WHERE scope = ? AND turn_id = ? AND state = 'outbound'
                """,
                (state, error_code, self.scope, turn_id),
            )

    def terminal_error(self) -> tuple[str, bool] | None:
        with closing(self._connection()) as connection:
            row = connection.execute(
                "SELECT state,error_code FROM discord_events WHERE scope = ? "
                "AND state = 'uncertain' LIMIT 1",
                (self.scope,),
            ).fetchone()
            delivery = connection.execute(
                "SELECT state FROM discord_deliveries WHERE scope=? "
                "AND state IN ('sending','uncertain') LIMIT 1",
                (self.scope,),
            ).fetchone()
        if row is None:
            return (
                ("discord_delivery_uncertain", True)
                if delivery is not None
                else None
            )
        uncertain = str(row[0]) == "uncertain"
        code = str(row[1] or "")
        return (
            code if _ERROR_RE.fullmatch(code) else (
                "discord_delivery_uncertain" if uncertain else "discord_delivery_rejected"
            ),
            uncertain,
        )

    def resolve_uncertain(self) -> None:
        with closing(self._connection()) as connection, connection:
            connection.execute(
                "UPDATE discord_events SET state='failed' "
                "WHERE scope=? AND state='uncertain'",
                (self.scope,),
            )
            connection.execute(
                "UPDATE discord_deliveries SET state='failed' "
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
                "SELECT state FROM discord_deliveries "
                "WHERE scope = ? AND delivery_key = ?",
                (self.scope, key),
            ).fetchone()
            if row is not None:
                state = str(row[0])
                if state == "sending":
                    connection.execute(
                        "UPDATE discord_deliveries "
                        "SET state = 'uncertain', updated_at = ? "
                        "WHERE scope = ? AND delivery_key = ?",
                        (now, self.scope, key),
                    )
                    return "uncertain"
                return state
            connection.execute(
                "INSERT INTO discord_deliveries(scope, delivery_key, state, updated_at) "
                "VALUES (?, ?, 'sending', ?)",
                (self.scope, key, now),
            )
        return "send"

    def mark_delivery(self, key: str, state: str) -> None:
        if state not in {"sent", "failed", "uncertain"}:
            raise ValueError("Discord delivery state is invalid")
        with closing(self._connection()) as connection, connection:
            connection.execute(
                "UPDATE discord_deliveries SET state = ?, updated_at = ? "
                "WHERE scope = ? AND delivery_key = ?",
                (state, int(time.time()), self.scope, key),
            )

    def release_delivery(self, key: str) -> None:
        with closing(self._connection()) as connection, connection:
            connection.execute(
                "DELETE FROM discord_deliveries "
                "WHERE scope = ? AND delivery_key = ? AND state = 'sending'",
                (self.scope, key),
            )

    def session(self) -> _Session | None:
        with closing(self._connection()) as connection:
            row = connection.execute(
                "SELECT session_id, resume_url, sequence FROM discord_sessions "
                "WHERE scope = ?",
                (self.scope,),
            ).fetchone()
        return _Session(str(row[0]), str(row[1]), int(row[2])) if row else None

    def save_session(self, session: _Session) -> None:
        with closing(self._connection()) as connection, connection:
            connection.execute(
                """
                INSERT INTO discord_sessions(scope, session_id, resume_url, sequence)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(scope) DO UPDATE SET
                    session_id = excluded.session_id,
                    resume_url = excluded.resume_url,
                    sequence = excluded.sequence
                """,
                (
                    self.scope,
                    session.session_id,
                    session.resume_url,
                    session.sequence,
                ),
            )

    def update_sequence(self, sequence: int) -> None:
        with closing(self._connection()) as connection, connection:
            connection.execute(
                "UPDATE discord_sessions SET sequence = ? WHERE scope = ?",
                (sequence, self.scope),
            )

    def clear_session(self) -> None:
        with closing(self._connection()) as connection, connection:
            connection.execute(
                "DELETE FROM discord_sessions WHERE scope = ?", (self.scope,)
            )

    def _connection(self) -> sqlite3.Connection:
        with self._lock:
            if self.path.exists() and stat.S_ISLNK(self.path.lstat().st_mode):
                raise RuntimeError("Discord state path is invalid")
            connection = sqlite3.connect(self.path, timeout=5)
            connection.execute("PRAGMA busy_timeout = 5000")
            connection.execute("PRAGMA secure_delete = ON")
            if not self._initialized:
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS discord_events(
                        scope TEXT NOT NULL,
                        event_key TEXT NOT NULL,
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
                        PRIMARY KEY(scope, event_key)
                    );
                    CREATE TABLE IF NOT EXISTS discord_deliveries(
                        scope TEXT NOT NULL,
                        delivery_key TEXT NOT NULL,
                        state TEXT NOT NULL CHECK(state IN ('sending','sent','failed','uncertain')),
                        updated_at INTEGER NOT NULL,
                        PRIMARY KEY(scope, delivery_key)
                    );
                    CREATE TABLE IF NOT EXISTS discord_sessions(
                        scope TEXT PRIMARY KEY,
                        session_id TEXT NOT NULL,
                        resume_url TEXT NOT NULL,
                        sequence INTEGER NOT NULL CHECK(sequence >= 0)
                    );
                    """
                )
                columns = {
                    str(row[1])
                    for row in connection.execute("PRAGMA table_info(discord_events)")
                }
                if "error_code" not in columns:
                    connection.execute(
                        "ALTER TABLE discord_events ADD COLUMN error_code TEXT"
                    )
                delivery_sql = str(
                    connection.execute(
                        "SELECT sql FROM sqlite_master WHERE type='table' "
                        "AND name='discord_deliveries'"
                    ).fetchone()[0]
                )
                if "'failed'" not in delivery_sql:
                    connection.executescript(
                        """
                        ALTER TABLE discord_deliveries RENAME TO discord_deliveries_v1;
                        CREATE TABLE discord_deliveries(
                            scope TEXT NOT NULL, delivery_key TEXT NOT NULL,
                            state TEXT NOT NULL CHECK(
                                state IN ('sending','sent','failed','uncertain')
                            ), updated_at INTEGER NOT NULL,
                            PRIMARY KEY(scope, delivery_key)
                        );
                        INSERT INTO discord_deliveries
                        SELECT * FROM discord_deliveries_v1;
                        DROP TABLE discord_deliveries_v1;
                        """
                    )
                connection.commit()
                os.chmod(self.path, 0o600)
                self._initialized = True
            return connection


class DiscordGatewayAdapter:
    """One Discord Gateway worker; no listener and no second Runtime."""

    def __init__(
        self,
        database_path: str | os.PathLike[str],
        *,
        client_factory: Callable[[], _HTTPClient] | None = None,
        socket_factory: Callable[[str], _Socket] | None = None,
        heartbeat_jitter: Callable[[], float] = random.random,
    ) -> None:
        self.database_path = Path(os.path.abspath(database_path))
        self.client_factory = client_factory or self._default_client
        self.socket_factory = socket_factory or self._default_socket
        self.heartbeat_jitter = heartbeat_jitter
        self._owner: ChannelCredentialOwner | None = None
        self._dispatcher: ChannelRuntimeDispatcher | None = None
        self._store: _DiscordStore | None = None
        self._client: _HTTPClient | None = None
        self._socket: _Socket | None = None
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._lock = threading.RLock()
        self._health = ConnectorHealth.DISABLED
        self._last_error: str | None = None
        self._bot_user_id: str | None = None
        self._token: str | None = None
        self._gateway_url: str | None = None
        self._session: _Session | None = None

    def bind_runtime(
        self,
        owner: ChannelCredentialOwner,
        dispatcher: ChannelRuntimeDispatcher,
    ) -> None:
        with self._lock:
            if self._dispatcher is not None and (
                self._owner != owner or self._dispatcher is not dispatcher
            ):
                raise RuntimeError("Discord Runtime is already bound")
            self._owner = owner
            self._dispatcher = dispatcher
            self._store = _DiscordStore(self.database_path, owner)

    def test(self, config: Mapping[str, Any]) -> ConnectorHealthResult:
        try:
            token = _token(config)
            client = self.client_factory()
            try:
                self._current_bot(client, token)
                self._discover_gateway(client, token, allow_limited_session=False)
            finally:
                _close(client)
            return ConnectorHealthResult(ConnectorHealth.CONNECTED)
        except _DiscordFailure as error:
            return ConnectorHealthResult(ConnectorHealth.ERROR, error.code)
        except Exception:
            return ConnectorHealthResult(
                ConnectorHealth.ERROR, "discord_transport_unavailable"
            )

    def start(self, config: Mapping[str, Any]) -> ConnectorHealthResult:
        try:
            token = _token(config)
            with self._lock:
                if self._dispatcher is None or self._store is None:
                    return ConnectorHealthResult(
                        ConnectorHealth.ERROR, "discord_runtime_unavailable"
                    )
                if self._thread is not None and self._thread.is_alive():
                    return ConnectorHealthResult(self._health, self._last_error)
            client = self.client_factory()
            socket: _Socket | None = None
            try:
                bot_user_id = self._current_bot(client, token, ensure_product_name=True)
                session = self._required_store().session()
                gateway_url = self._discover_gateway(
                    client, token, allow_limited_session=session is not None
                )
                socket, heartbeat_interval, next_heartbeat = self._new_socket(
                    session.resume_url if session else gateway_url,
                    token,
                    session,
                )
                store = self._required_store()
                store.has_uncertain()
                terminal = store.terminal_error()
                with self._lock:
                    self._client = client
                    self._socket = socket
                    self._bot_user_id = bot_user_id
                    self._token = token
                    self._gateway_url = gateway_url
                    self._session = session
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
                        args=(
                            client,
                            socket,
                            token,
                            gateway_url,
                            heartbeat_interval,
                            next_heartbeat,
                        ),
                        name="emate-discord-channel",
                        daemon=True,
                    )
                    self._thread.start()
            except Exception:
                _close_socket(socket)
                _close(client)
                raise
            return ConnectorHealthResult(self._health, self._last_error)
        except _DiscordFailure as error:
            return ConnectorHealthResult(ConnectorHealth.ERROR, error.code)
        except Exception:
            return ConnectorHealthResult(
                ConnectorHealth.ERROR, "discord_transport_unavailable"
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
        _close_socket(socket, code=1000)
        if thread is not None:
            thread.join(timeout_seconds)
        stopped = thread is None or not thread.is_alive()
        if stopped:
            store = self._store
            if store is not None:
                store.clear_session()
            with self._lock:
                self._thread = None
                self._client = None
                self._socket = None
                self._bot_user_id = None
                self._token = None
                self._gateway_url = None
                self._session = None
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
        if channel_id != "discord" or not isinstance(text, str) or not text:
            raise ValueError("Discord delivery is invalid")
        if _SNOWFLAKE_RE.fullmatch(conversation_id) is None:
            raise ValueError("Discord conversation is invalid")
        with self._lock:
            client = self._client
            store = self._store
            token = self._token
        if client is None or store is None or token is None:
            raise _DiscordFailure("discord_not_running", permanent=True)
        chunks = _chunks(text)
        for index, chunk in enumerate(chunks):
            key = f"{idempotency_key}:{index + 1}:{len(chunks)}"
            state = store.claim_delivery(key)
            if state == "sent":
                continue
            if state == "failed":
                raise _DiscordFailure("discord_delivery_rejected", permanent=True)
            if state == "uncertain":
                raise _DiscordFailure("discord_delivery_uncertain", uncertain=True)
            nonce = hashlib.sha256(key.encode()).hexdigest()[:25]
            try:
                payload = self._request(
                    client,
                    "POST",
                    f"channels/{conversation_id}/messages",
                    token=token,
                    body={
                        "content": chunk,
                        "nonce": nonce,
                        "enforce_nonce": True,
                        "allowed_mentions": {"parse": []},
                    },
                    delivery=True,
                )
                if (
                    _SNOWFLAKE_RE.fullmatch(str(payload.get("id") or "")) is None
                    or payload.get("channel_id") != conversation_id
                ):
                    raise _DiscordFailure(
                        "discord_delivery_uncertain", uncertain=True
                    )
            except _DiscordFailure as error:
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
        token: str,
        gateway_url: str,
        heartbeat_interval: float,
        next_heartbeat: float,
    ) -> None:
        backoff = 1.0
        current = socket
        awaiting_ack = False
        interval = heartbeat_interval
        next_due = next_heartbeat
        try:
            while not self._stop_event.is_set():
                try:
                    self._drain_received()
                    self._drain_outbound()
                    now = time.monotonic()
                    if now >= next_due:
                        if awaiting_ack:
                            raise _DiscordFailure("discord_heartbeat_unacknowledged")
                        self._send_gateway(current, 1, self._sequence())
                        awaiting_ack = True
                        next_due = now + interval
                    try:
                        frame = current.recv(timeout=min(0.25, max(0.01, next_due - now)))
                    except TimeoutError:
                        raise
                    except (ConnectionClosed, OSError) as error:
                        close_code = _close_code(error)
                        if close_code in _NON_RESUMABLE_CLOSE_CODES:
                            self._clear_session()
                        if close_code == 4004:
                            raise _DiscordFailure(
                                "discord_auth_rejected", permanent=True
                            ) from None
                        if close_code == 4011:
                            raise _DiscordFailure(
                                "discord_sharding_required", permanent=True
                            ) from None
                        if close_code in {4013, 4014}:
                            raise _DiscordFailure(
                                "discord_intents_rejected", permanent=True
                            ) from None
                        raise _DiscordFailure(
                            "discord_transport_unavailable",
                            reset_session=close_code in _NON_RESUMABLE_CLOSE_CODES,
                        ) from None
                    awaiting_ack = self._handle_frame(current, frame, awaiting_ack)
                    self._set_ready_health()
                    backoff = 1.0
                except TimeoutError:
                    self._set_ready_health()
                except _DiscordFailure as error:
                    if error.reset_session:
                        self._clear_session()
                    self._set_health(
                        ConnectorHealth.DEGRADED
                        if error.uncertain
                        else ConnectorHealth.ERROR,
                        error.code,
                    )
                    if error.permanent or self._stop_event.wait(backoff):
                        return
                    backoff = min(backoff * 2, 30.0)
                    _close_socket(current, code=4000)
                    session = self._session_value()
                    try:
                        current, interval, next_due = self._new_socket(
                            session.resume_url if session else gateway_url,
                            token,
                            session,
                        )
                        awaiting_ack = False
                    except _DiscordFailure as reconnect_error:
                        self._set_health(ConnectorHealth.ERROR, reconnect_error.code)
                        continue
                    with self._lock:
                        self._socket = current
                except Exception:
                    self._set_health(
                        ConnectorHealth.ERROR, "discord_runtime_dispatch_failed"
                    )
                    if self._stop_event.wait(backoff):
                        return
                    backoff = min(backoff * 2, 30.0)
                    _close_socket(current, code=4000)
                    session = self._session_value()
                    try:
                        current, interval, next_due = self._new_socket(
                            session.resume_url if session else gateway_url,
                            token,
                            session,
                        )
                        awaiting_ack = False
                    except _DiscordFailure as reconnect_error:
                        self._set_health(ConnectorHealth.ERROR, reconnect_error.code)
                        continue
                    with self._lock:
                        self._socket = current
        finally:
            _close_socket(current, code=1000 if self._stop_event.is_set() else 4000)
            _close(client)
            with self._lock:
                if self._client is client:
                    self._client = None
                    self._token = None
                    self._bot_user_id = None
                if self._socket is current:
                    self._socket = None

    def _handle_frame(
        self,
        socket: _Socket,
        frame: str | bytes,
        awaiting_ack: bool,
    ) -> bool:
        payload = _gateway_payload(frame)
        opcode = payload.get("op")
        if opcode == 11:
            return False
        if opcode == 1:
            self._send_gateway(socket, 1, self._sequence())
            return True
        if opcode == 7:
            raise _DiscordFailure("discord_reconnect_requested")
        if opcode == 9:
            resumable = payload.get("d") is True
            raise _DiscordFailure(
                "discord_session_invalid",
                reset_session=not resumable,
            )
        if opcode != 0:
            return awaiting_ack
        sequence = payload.get("s")
        event_type = payload.get("t")
        data = payload.get("d")
        if (
            not isinstance(sequence, int)
            or isinstance(sequence, bool)
            or sequence < 0
            or not isinstance(event_type, str)
            or not isinstance(data, Mapping)
        ):
            raise _DiscordFailure("discord_provider_response_invalid")
        if event_type == "READY":
            self._accept_ready(data, sequence)
        elif event_type == "MESSAGE_CREATE":
            event = self._message_event(data)
            if event is not None:
                self._required_store().record(
                    conversation_id=event.conversation_id,
                    message_id=event.message_id,
                    text=event.text,
                )
            # Persist the event before advancing the resumable Gateway cursor.
            # A replay after a crash is harmless because the journal key is
            # deterministic; advancing first can permanently skip the event.
            self._set_sequence(sequence)
            if event is not None:
                self._drain_received()
        else:
            self._set_sequence(sequence)
        return awaiting_ack

    def _accept_ready(self, data: Mapping[str, Any], sequence: int) -> None:
        user = data.get("user")
        session_id = data.get("session_id")
        resume_url = data.get("resume_gateway_url")
        if (
            not isinstance(user, Mapping)
            or user.get("id") != self._required_bot_user_id()
            or not isinstance(session_id, str)
            or _SESSION_RE.fullmatch(session_id) is None
            or not isinstance(resume_url, str)
        ):
            raise _DiscordFailure("discord_provider_response_invalid")
        resume_url = _gateway_url(resume_url)
        session = _Session(session_id, resume_url, sequence)
        self._required_store().save_session(session)
        with self._lock:
            self._session = session

    def _message_event(self, data: Mapping[str, Any]) -> _JournalEvent | None:
        message_id = data.get("id")
        conversation_id = data.get("channel_id")
        author = data.get("author")
        content = data.get("content")
        bot_user_id = self._required_bot_user_id()
        if (
            not isinstance(message_id, str)
            or _SNOWFLAKE_RE.fullmatch(message_id) is None
            or not isinstance(conversation_id, str)
            or _SNOWFLAKE_RE.fullmatch(conversation_id) is None
            or not isinstance(author, Mapping)
            or not isinstance(author.get("id"), str)
            or _SNOWFLAKE_RE.fullmatch(str(author["id"])) is None
            or author.get("id") == bot_user_id
            or author.get("bot") is True
            or data.get("webhook_id") is not None
            or not isinstance(content, str)
            or len(content) > _MAX_INBOUND_TEXT
            or "\x00" in content
        ):
            return None
        guild_id = data.get("guild_id")
        if guild_id is not None and (
            not isinstance(guild_id, str)
            or _SNOWFLAKE_RE.fullmatch(guild_id) is None
        ):
            return None
        guild_message = guild_id is not None
        mention = re.compile(rf"<@!?{re.escape(bot_user_id)}>")
        if guild_message and mention.search(content) is None:
            return None
        text = mention.sub("", content).strip()
        if not text:
            return None
        event_key = hashlib.sha256(
            f"{conversation_id}\0{message_id}".encode()
        ).hexdigest()
        return _JournalEvent(event_key, conversation_id, message_id, text)

    def _drain_received(self) -> None:
        dispatcher = self._required_dispatcher()
        store = self._required_store()
        for event in store.received():
            receipt = dispatcher.dispatch(
                ChannelInboundMessage(
                    channel_id="discord",
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
                    error.code.replace("channel_", "discord_", 1),
                )
                continue
            except _DiscordFailure as error:
                if error.uncertain:
                    store.finish(receipt.turn_id, "uncertain", error.code)
                    self._set_health(
                        ConnectorHealth.DEGRADED, "discord_delivery_uncertain"
                    )
                    continue
                if error.permanent:
                    store.finish(receipt.turn_id, "failed", error.code)
                    self._set_health(ConnectorHealth.ERROR, error.code)
                    continue
                raise
            if delivered:
                store.finish(receipt.turn_id, "completed")

    def _current_bot(
        self,
        client: _HTTPClient,
        token: str,
        *,
        ensure_product_name: bool = False,
    ) -> str:
        payload = self._request(
            client, "GET", "users/@me", token=token, body=None
        )
        user_id = payload.get("id")
        if (
            not isinstance(user_id, str)
            or _SNOWFLAKE_RE.fullmatch(user_id) is None
            or payload.get("bot") is not True
        ):
            raise _DiscordFailure("discord_provider_response_invalid")
        if ensure_product_name and payload.get("username") != "e-Mate":
            updated = self._request(
                client,
                "PATCH",
                "users/@me",
                token=token,
                body={"username": "e-Mate"},
            )
            if (
                updated.get("id") != user_id
                or updated.get("bot") is not True
                or updated.get("username") != "e-Mate"
            ):
                raise _DiscordFailure("discord_provider_response_invalid")
        return user_id

    def _discover_gateway(
        self,
        client: _HTTPClient,
        token: str,
        *,
        allow_limited_session: bool,
    ) -> str:
        payload = self._request(
            client, "GET", "gateway/bot", token=token, body=None
        )
        url = payload.get("url")
        shards = payload.get("shards")
        limit = payload.get("session_start_limit")
        if not isinstance(url, str) or not isinstance(limit, Mapping):
            raise _DiscordFailure("discord_provider_response_invalid")
        if not isinstance(shards, int) or isinstance(shards, bool) or shards < 1:
            raise _DiscordFailure("discord_provider_response_invalid")
        if shards != 1:
            raise _DiscordFailure("discord_sharding_required", permanent=True)
        remaining = limit.get("remaining")
        if not isinstance(remaining, int) or isinstance(remaining, bool):
            raise _DiscordFailure("discord_provider_response_invalid")
        if remaining <= 0 and not allow_limited_session:
            raise _DiscordFailure("discord_session_start_limited", permanent=True)
        return _gateway_url(url)

    def _new_socket(
        self,
        url: str,
        token: str,
        session: _Session | None,
    ) -> tuple[_Socket, float, float]:
        socket: _Socket | None = None
        try:
            socket = self.socket_factory(_gateway_url(url, query=True))
            hello = _gateway_payload(socket.recv(timeout=4))
            interval_ms = hello.get("d", {}).get("heartbeat_interval")
            if (
                hello.get("op") != 10
                or not isinstance(interval_ms, (int, float))
                or isinstance(interval_ms, bool)
                or not 1_000 <= interval_ms <= 300_000
            ):
                raise ValueError
            interval = float(interval_ms) / 1000
            jitter = float(self.heartbeat_jitter())
            if not 0 <= jitter <= 1:
                raise ValueError
            if session is None:
                self._send_gateway(
                    socket,
                    2,
                    {
                        "token": token,
                        "intents": _INTENTS,
                        "properties": {
                            "os": os.name,
                            "browser": "e-Mate",
                            "device": "e-Mate",
                        },
                    },
                )
            else:
                self._send_gateway(
                    socket,
                    6,
                    {
                        "token": token,
                        "session_id": session.session_id,
                        "seq": session.sequence,
                    },
                )
            return socket, interval, time.monotonic() + interval * jitter
        except _DiscordFailure:
            _close_socket(socket, code=4000)
            raise
        except Exception:
            _close_socket(socket, code=4000)
            raise _DiscordFailure("discord_transport_unavailable") from None

    def _request(
        self,
        client: _HTTPClient,
        method: str,
        path: str,
        *,
        token: str,
        body: Mapping[str, Any] | None,
        delivery: bool = False,
    ) -> Mapping[str, Any]:
        headers = {"Authorization": f"Bot {token}"}
        try:
            if method == "GET":
                response = client.get(path, headers=headers)
            elif method == "PATCH":
                response = client.patch(path, headers=headers, json=dict(body or {}))
            else:
                response = client.post(path, headers=headers, json=dict(body or {}))
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout):
            raise _DiscordFailure("discord_transport_unavailable") from None
        except (httpx.TimeoutException, httpx.TransportError):
            raise _DiscordFailure(
                "discord_delivery_uncertain"
                if delivery
                else "discord_transport_unavailable",
                uncertain=delivery,
            ) from None
        except Exception:
            raise _DiscordFailure(
                "discord_delivery_uncertain"
                if delivery
                else "discord_transport_unavailable",
                uncertain=delivery,
            ) from None
        if len(response.content) > _MAX_RESPONSE_BYTES:
            raise _DiscordFailure(
                "discord_delivery_uncertain"
                if delivery
                else "discord_provider_response_invalid",
                uncertain=delivery,
            )
        if response.status_code in {401, 403} and not delivery:
            raise _DiscordFailure("discord_auth_rejected", permanent=True)
        if response.status_code == 429:
            raise _DiscordFailure("discord_rate_limited")
        if response.status_code >= 500:
            raise _DiscordFailure(
                "discord_delivery_uncertain"
                if delivery
                else "discord_transport_unavailable",
                uncertain=delivery,
            )
        if not 200 <= response.status_code < 300:
            raise _DiscordFailure(
                "discord_delivery_rejected"
                if delivery
                else "discord_provider_rejected",
                permanent=delivery,
            )
        try:
            payload = response.json()
        except ValueError:
            raise _DiscordFailure(
                "discord_delivery_uncertain"
                if delivery
                else "discord_provider_response_invalid",
                uncertain=delivery,
            ) from None
        if not isinstance(payload, dict):
            raise _DiscordFailure(
                "discord_delivery_uncertain"
                if delivery
                else "discord_provider_response_invalid",
                uncertain=delivery,
            )
        return payload

    def _send_gateway(self, socket: _Socket, opcode: int, data: Any) -> None:
        try:
            socket.send(
                json.dumps(
                    {"op": opcode, "d": data},
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
        except Exception:
            raise _DiscordFailure("discord_transport_unavailable") from None

    def _sequence(self) -> int | None:
        with self._lock:
            return self._session.sequence if self._session else None

    def _set_sequence(self, sequence: int) -> None:
        with self._lock:
            session = self._session
            if session is None:
                return
            session = _Session(session.session_id, session.resume_url, sequence)
            self._session = session
        self._required_store().update_sequence(sequence)

    def _clear_session(self) -> None:
        self._required_store().clear_session()
        with self._lock:
            self._session = None

    def _session_value(self) -> _Session | None:
        with self._lock:
            return self._session

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

    def _set_health(self, health: ConnectorHealth, error: str | None) -> None:
        with self._lock:
            self._health = health
            self._last_error = error

    def _required_dispatcher(self) -> ChannelRuntimeDispatcher:
        with self._lock:
            if self._dispatcher is None:
                raise _DiscordFailure("discord_runtime_unavailable", permanent=True)
            return self._dispatcher

    def _required_store(self) -> _DiscordStore:
        with self._lock:
            if self._store is None:
                raise _DiscordFailure("discord_runtime_unavailable", permanent=True)
            return self._store

    def _required_bot_user_id(self) -> str:
        with self._lock:
            if self._bot_user_id is None:
                raise _DiscordFailure("discord_runtime_unavailable", permanent=True)
            return self._bot_user_id

    @staticmethod
    def _default_client() -> httpx.Client:
        return httpx.Client(
            base_url="https://discord.com/api/v10/",
            timeout=httpx.Timeout(connect=4, read=4, write=4, pool=4),
            limits=httpx.Limits(max_connections=2, max_keepalive_connections=1),
            follow_redirects=False,
            headers={"User-Agent": f"e-Mate/{__version__} DiscordChannel/1"},
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
            user_agent_header=f"e-Mate/{__version__} DiscordChannel/1",
        )


def _token(config: Mapping[str, Any]) -> str:
    if not isinstance(config, Mapping) or set(config) != {"discord_token"}:
        raise _DiscordFailure("discord_configuration_invalid", permanent=True)
    token = config.get("discord_token")
    if not isinstance(token, str) or _TOKEN_RE.fullmatch(token) is None:
        raise _DiscordFailure("discord_configuration_invalid", permanent=True)
    return token


def _gateway_payload(frame: str | bytes) -> Mapping[str, Any]:
    if not isinstance(frame, str) or len(frame.encode("utf-8")) > _MAX_RESPONSE_BYTES:
        raise _DiscordFailure("discord_provider_response_invalid")
    try:
        payload = json.loads(frame)
    except ValueError:
        raise _DiscordFailure("discord_provider_response_invalid") from None
    opcode = payload.get("op") if isinstance(payload, dict) else None
    if not isinstance(payload, dict) or not isinstance(opcode, int) or isinstance(opcode, bool):
        raise _DiscordFailure("discord_provider_response_invalid")
    return payload


def _gateway_url(value: str, *, query: bool = False) -> str:
    if not isinstance(value, str) or len(value) > 4096:
        raise _DiscordFailure("discord_provider_response_invalid")
    parsed = urlsplit(value)
    host = (parsed.hostname or "").casefold()
    if (
        parsed.scheme != "wss"
        or not (host == "discord.gg" or host.endswith(".discord.gg"))
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in {None, 443}
        or bool(parsed.fragment)
    ):
        raise _DiscordFailure("discord_provider_response_invalid")
    if not query:
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path or "/", "", ""))
    params = dict(parse_qsl(parsed.query, keep_blank_values=True))
    params.update({"v": "10", "encoding": "json"})
    return urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path or "/", urlencode(params), "")
    )


def _chunks(text: str) -> tuple[str, ...]:
    chunks: list[str] = []
    remaining = text
    while remaining:
        split = min(len(remaining), _MAX_DISCORD_TEXT)
        if split < len(remaining):
            newline = remaining.rfind("\n", _MAX_DISCORD_TEXT // 2, split)
            if newline >= 0:
                split = newline + 1
        chunks.append(remaining[:split])
        remaining = remaining[split:]
    return tuple(chunks)


def _close(client: _HTTPClient) -> None:
    try:
        client.close()
    except Exception:
        pass


def _close_socket(socket: _Socket | None, *, code: int = 1000) -> None:
    if socket is None:
        return
    try:
        socket.close(code=code, reason="e-Mate channel lifecycle")
    except Exception:
        pass


def _close_code(error: BaseException) -> int | None:
    received = getattr(error, "rcvd", None)
    return getattr(received, "code", None)


__all__ = ["DiscordGatewayAdapter"]
