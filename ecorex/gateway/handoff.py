"""Typed durable authority contract for Chat Completions tool continuations."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Protocol

from .models import GatewayEvent, ModelGatewayRequest


@dataclass(frozen=True, slots=True)
class ChatModelRevision:
    config_id: str
    revision: int
    local_model_id: str
    upstream_model_id: str
    provider_protocol: str
    provider_origin_preset: str

    def __post_init__(self) -> None:
        safe = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
        if (
            any(
                not isinstance(value, str) or safe.fullmatch(value) is None
                for value in (
                    self.config_id,
                    self.local_model_id,
                    self.upstream_model_id,
                    self.provider_origin_preset,
                )
            )
            or isinstance(self.revision, bool)
            or self.revision < 1
            or self.provider_protocol != "openai_compatible_chat"
            or self.provider_origin_preset
            not in {"deepseek_chat", "gemini_chat", "doubao_chat"}
        ):
            raise ValueError("chat model revision identity is invalid")


@dataclass(frozen=True, slots=True)
class DurableChatHandoff:
    response_id: str
    tool_call_id: str
    provider_tool_name: str
    arguments_json: str

    def assistant_message(self) -> dict[str, object]:
        return {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": self.tool_call_id,
                    "type": "function",
                    "function": {
                        "name": self.provider_tool_name,
                        "arguments": self.arguments_json,
                    },
                }
            ],
        }


class ChatHandoffAuthority(Protocol):
    def bind_model_attempt(
        self,
        request: ModelGatewayRequest,
        *,
        config_id: str,
        config_revision: int,
        upstream_model_id: str,
        provider_protocol: str,
        provider_origin_preset: str,
        ttl_seconds: int,
    ) -> None: ...

    def bind_chat_model_attempt(
        self,
        request: ModelGatewayRequest,
        revision: ChatModelRevision,
        *,
        ttl_seconds: int,
    ) -> None: ...

    def stage_chat_handoff(
        self,
        request: ModelGatewayRequest,
        revision: ChatModelRevision,
        event: GatewayEvent,
        *,
        provider_tool_name: str,
        arguments_json: str,
    ) -> None: ...

    def consume_chat_handoff(
        self,
        request: ModelGatewayRequest,
        revision: ChatModelRevision,
        **kwargs: object,
    ) -> DurableChatHandoff | None: ...


__all__ = [
    "ChatHandoffAuthority",
    "ChatModelRevision",
    "DurableChatHandoff",
]
