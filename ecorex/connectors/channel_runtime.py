"""Minimal boundary between packaged message transports and the Agent Runtime."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import hashlib
from typing import Any, Protocol

from ecorex.protocol import (
    CreateThreadRequest,
    CreateTurnRequest,
    ItemKind,
    ItemStatus,
    TERMINAL_TURN_STATUSES,
    ThreadProjection,
    ThreadProjectionResponse,
    TurnMutationResponse,
)

from .channel_catalog import CHANNEL_CATALOG, normalize_channel_name
from .channel_self_service import ChannelCredentialOwner


_CONTRACT_VERSION = "channel-runtime-dispatch-v1"
_MAX_EXTERNAL_ID = 512
_MAX_MESSAGE_TEXT = 1_000_000


class _PreparedTurn(Protocol):
    request: CreateTurnRequest
    snapshot_context: Any


class _RuntimeAdmission(Protocol):
    permission_account_id: str

    def admit_turn(
        self,
        request: CreateTurnRequest,
        accept: Callable[[_PreparedTurn], TurnMutationResponse],
        *,
        thread_id: str | None = None,
    ) -> TurnMutationResponse: ...


class _RuntimeKernel(Protocol):
    def create_thread(self, request: CreateThreadRequest) -> ThreadProjection: ...

    def create_turn(
        self,
        thread_id: str,
        request: CreateTurnRequest,
        *,
        snapshot_context: Any = None,
        permission_account_id: str | None = None,
        causation_id: str | None = None,
        correlation_id: str | None = None,
    ) -> TurnMutationResponse: ...

    def projection(self, thread_id: str) -> ThreadProjectionResponse: ...


class _WorkerWake(Protocol):
    def notify(self) -> None: ...


class ChannelTextTransport(Protocol):
    """Pack transport contract.

    Implementations own platform-safe chunking and must durably deduplicate
    ``idempotency_key`` before acknowledging the vendor send.
    """

    def send_text(
        self,
        *,
        channel_id: str,
        conversation_id: str,
        text: str,
        idempotency_key: str,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class ChannelInboundMessage:
    channel_id: str
    conversation_id: str
    message_id: str
    text: str

    def __post_init__(self) -> None:
        channel_id = normalize_channel_name(self.channel_id)
        if channel_id not in CHANNEL_CATALOG:
            raise ValueError("channel is unknown")
        object.__setattr__(self, "channel_id", channel_id)
        _external_id(self.conversation_id, "conversation")
        _external_id(self.message_id, "message")
        if (
            not isinstance(self.text, str)
            or not self.text.strip()
            or len(self.text) > _MAX_MESSAGE_TEXT
            or "\x00" in self.text
        ):
            raise ValueError("channel message text is invalid")


@dataclass(frozen=True, slots=True)
class ChannelTurnReceipt:
    channel_id: str
    thread_id: str
    turn_id: str
    client_message_id: str
    conversation_sha256: str


@dataclass(frozen=True, slots=True)
class ChannelOutboundText:
    channel_id: str
    turn_id: str
    item_id: str
    text: str
    idempotency_key: str


class ChannelRuntimeDispatcher:
    """Admit channel messages and project replies through existing Runtime facts."""

    def __init__(
        self,
        *,
        owner: ChannelCredentialOwner,
        composition: _RuntimeAdmission,
        kernel: _RuntimeKernel,
        worker: _WorkerWake,
    ) -> None:
        self.owner = owner
        self.composition = composition
        self.kernel = kernel
        self.worker = worker

    def dispatch(self, message: ChannelInboundMessage) -> ChannelTurnReceipt:
        conversation_sha256 = self._digest(
            "conversation", message.channel_id, message.conversation_id
        )
        message_sha256 = self._digest(
            "message",
            message.channel_id,
            message.conversation_id,
            message.message_id,
        )
        thread = self.kernel.create_thread(
            CreateThreadRequest(
                title=_channel_title(message.channel_id),
                client_request_id=f"channel-thread-{conversation_sha256}",
                metadata={
                    "channel": {
                        "contract_version": _CONTRACT_VERSION,
                        "channel_id": message.channel_id,
                        "conversation_sha256": conversation_sha256,
                    }
                },
            )
        )
        client_message_id = f"channel-message-{message_sha256}"
        request = CreateTurnRequest(
            input=message.text,
            client_message_id=client_message_id,
            metadata={
                "channel": {
                    "contract_version": _CONTRACT_VERSION,
                    "channel_id": message.channel_id,
                    "conversation_sha256": conversation_sha256,
                    "message_sha256": message_sha256,
                }
            },
        )
        accepted = self.composition.admit_turn(
            request,
            lambda prepared: self.kernel.create_turn(
                thread.thread_id,
                prepared.request,
                snapshot_context=prepared.snapshot_context,
                permission_account_id=self.composition.permission_account_id,
                causation_id=client_message_id,
                correlation_id=f"channel-thread-{conversation_sha256}",
            ),
            thread_id=thread.thread_id,
        )
        self.worker.notify()
        return ChannelTurnReceipt(
            channel_id=message.channel_id,
            thread_id=thread.thread_id,
            turn_id=accepted.turn.turn_id,
            client_message_id=client_message_id,
            conversation_sha256=conversation_sha256,
        )

    def project_outbound(
        self, receipt: ChannelTurnReceipt
    ) -> ChannelOutboundText | None:
        projection = self.kernel.projection(receipt.thread_id)
        turn = next(
            (turn for turn in projection.turns if turn.turn_id == receipt.turn_id),
            None,
        )
        if turn is None:
            raise ValueError("channel Turn is missing")
        if turn.status not in TERMINAL_TURN_STATUSES:
            return None
        assistant = next(
            (
                item
                for item in reversed(projection.items)
                if item.turn_id == receipt.turn_id
                and item.kind is ItemKind.MESSAGE
                and item.status is ItemStatus.COMPLETED
                and item.content.get("role") == "assistant"
                and isinstance(item.content.get("text"), str)
                and item.content["text"]
            ),
            None,
        )
        if assistant is None:
            return None
        text = str(assistant.content["text"])
        return ChannelOutboundText(
            channel_id=receipt.channel_id,
            turn_id=receipt.turn_id,
            item_id=assistant.item_id,
            text=text,
            idempotency_key="channel-delivery-"
            + self._digest("delivery", receipt.turn_id, assistant.item_id, text),
        )

    def deliver(
        self,
        receipt: ChannelTurnReceipt,
        *,
        conversation_id: str,
        transport: ChannelTextTransport,
    ) -> bool:
        _external_id(conversation_id, "conversation")
        if self._digest("conversation", receipt.channel_id, conversation_id) != (
            receipt.conversation_sha256
        ):
            raise ValueError("channel conversation does not match the Turn")
        outbound = self.project_outbound(receipt)
        if outbound is None:
            return False
        transport.send_text(
            channel_id=outbound.channel_id,
            conversation_id=conversation_id,
            text=outbound.text,
            idempotency_key=outbound.idempotency_key,
        )
        return True

    def _digest(self, *parts: str) -> str:
        scoped = (
            self.owner.organization_id,
            self.owner.account_id,
            *parts,
        )
        return hashlib.sha256("\x00".join(scoped).encode("utf-8")).hexdigest()


def _external_id(value: Any, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > _MAX_EXTERNAL_ID
        or any(character in value for character in ("\x00", "\r", "\n"))
    ):
        raise ValueError(f"channel {label} id is invalid")
    return value


def _channel_title(channel_id: str) -> str:
    label = CHANNEL_CATALOG[channel_id].get("label", {})
    return f"{label.get('zh') or label.get('en') or channel_id} 会话"


__all__ = [
    "ChannelInboundMessage",
    "ChannelOutboundText",
    "ChannelRuntimeDispatcher",
    "ChannelTextTransport",
    "ChannelTurnReceipt",
]
