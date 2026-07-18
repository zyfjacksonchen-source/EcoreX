"""Fixed-origin, bounded OpenAI-compatible Chat Completions adapter."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Mapping
import hashlib
import json
import ssl
from types import MappingProxyType
from typing import Any

import httpx

from .models import (
    GatewayEvent,
    GatewayEventType,
    GatewayFunctionCallOutputInput,
    GatewayModelPolicy,
    GatewayUserMessageInput,
    ModelGatewayRequest,
    TOOL_PROJECTION_BUDGET_VERSION,
)
from .responses_provider import (
    ResponsesProviderConfigurationError,
    ResponsesProviderProtocolError,
    ResponsesProviderRejected,
    ResponsesProviderUnavailable,
    _canonical,
    _decode_object,
    _iter_sse_data,
    _provider_id,
    _provider_tools,
    normalize_https_origin,
)
from .server import GatewayPrincipal
from .handoff import ChatHandoffAuthority, ChatModelRevision, DurableChatHandoff

_MAX_REQUEST = 4 * 1024 * 1024
_MAX_BODY = 16 * 1024 * 1024
_MAX_ARGUMENTS = 1024 * 1024


class ManagedHTTPSChatCompletionsProvider:
    """Translate one tested Chat Completions revision into Gateway events.

    The endpoint and credential are deployment/revision authorities.  A POST is
    issued once, redirects/proxies/compression are disabled, and any uncertain
    transport outcome fails without a transparent retry.
    """

    def __init__(
        self,
        *,
        origin: str,
        allowed_origins: frozenset[str],
        model_mapping: Mapping[str, str],
        model_policies: Mapping[str, GatewayModelPolicy],
        bearer_token: Callable[[], str],
        handoff_authority: ChatHandoffAuthority,
        model_revision: ChatModelRevision,
        handoff_ttl_seconds: int = 3600,
        connect_timeout_seconds: float = 5.0,
        read_timeout_seconds: float = 30.0,
        total_timeout_seconds: float = 240.0,
        max_concurrency: int = 64,
        max_connections: int = 128,
        ssl_context: ssl.SSLContext | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        normalized = normalize_https_origin(origin)
        allowed = frozenset(normalize_https_origin(item) for item in allowed_origins)
        mapping = dict(model_mapping)
        policies = dict(model_policies)
        if (
            normalized not in allowed
            or not mapping
            or set(mapping) != set(policies)
            or any(
                policy.local_model_id != local
                or policy.upstream_model_id != mapping[local]
                for local, policy in policies.items()
            )
            or not callable(bearer_token)
            or not isinstance(model_revision, ChatModelRevision)
            or not 300 <= handoff_ttl_seconds <= 86_400
            or not 0.1 <= connect_timeout_seconds <= 30
            or not 0.5 <= read_timeout_seconds <= total_timeout_seconds <= 900
            or not 1 <= max_concurrency <= max_connections <= 512
        ):
            raise ResponsesProviderConfigurationError(
                "chat provider configuration is invalid"
            )
        self.origin = normalized
        self.allowed_origins = allowed
        self.model_mapping = MappingProxyType(mapping)
        self.model_policies = MappingProxyType(policies)
        self._bearer_token = bearer_token
        self._handoff_authority = handoff_authority
        self._model_revision = model_revision
        self.handoff_ttl_seconds = handoff_ttl_seconds
        self.connect_timeout_seconds = connect_timeout_seconds
        self.read_timeout_seconds = read_timeout_seconds
        self.total_timeout_seconds = total_timeout_seconds
        self._slots = asyncio.BoundedSemaphore(max_concurrency)
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(
                total_timeout_seconds,
                connect=connect_timeout_seconds,
                read=read_timeout_seconds,
                write=read_timeout_seconds,
                pool=connect_timeout_seconds,
            ),
            limits=httpx.Limits(
                max_connections=max_connections,
                max_keepalive_connections=max_connections,
                keepalive_expiry=15,
            ),
            follow_redirects=False,
            trust_env=False,
            http2=False,
            verify=ssl_context if ssl_context is not None else True,
        )

    async def stream(
        self, request: ModelGatewayRequest, principal: GatewayPrincipal
    ) -> AsyncIterator[GatewayEvent]:
        self.validate_request(request, principal)
        await asyncio.to_thread(
            self._handoff_authority.bind_chat_model_attempt,
            request,
            self._model_revision,
            ttl_seconds=self.handoff_ttl_seconds,
        )
        prior = await asyncio.to_thread(
            self._handoff_authority.consume_chat_handoff,
            request,
            self._model_revision,
        )
        payload, names = self._payload(request, prior=prior)
        encoded = _canonical(payload)
        if len(encoded) > _MAX_REQUEST:
            raise ResponsesProviderRejected("chat provider request is oversized")
        token = self._credential()
        outbound = self._client.build_request(
            "POST",
            self.origin + "/v1/chat/completions",
            headers={
                "Authorization": "Bearer " + token,
                "Accept": "text/event-stream, application/json",
                "Accept-Encoding": "identity",
                "Content-Type": "application/json",
                "Idempotency-Key": request.request_id,
                "User-Agent": "EcoreX-Model-Gateway/1.0",
            },
            content=encoded,
        )
        outbound.extensions["timeout"] = {
            "connect": self.connect_timeout_seconds,
            "read": self.read_timeout_seconds,
            "write": self.read_timeout_seconds,
            "pool": self.connect_timeout_seconds,
        }
        response: httpx.Response | None = None
        try:
            async with self._slots, asyncio.timeout(self.total_timeout_seconds):
                response = await self._client.send(
                    outbound, stream=True, follow_redirects=False
                )
                self._validate_response(response)
                parser = _ChatCompletionParser(
                    request.request_id,
                    expected_model_id=self.model_mapping[request.model_id],
                    tool_names=names,
                )
                media = response.headers.get("content-type", "").split(";", 1)[0].strip().casefold()
                if media == "text/event-stream":
                    async for data in _iter_sse_data(response):
                        if data == b"[DONE]":
                            terminal = parser.finish_stream()
                            if terminal is not None:
                                await self._stage_handoff(request, parser, terminal)
                                yield terminal
                            break
                        for event in parser.feed_stream(_decode_object(data)):
                            await self._stage_handoff(request, parser, event)
                            yield event
                else:
                    body = await self._bounded_body(response)
                    for event in parser.feed_response(_decode_object(body)):
                        await self._stage_handoff(request, parser, event)
                        yield event
                if not parser.terminal:
                    raise ResponsesProviderProtocolError(
                        "chat provider ended without a terminal response"
                    )
        except (ResponsesProviderRejected, ResponsesProviderProtocolError):
            raise
        except asyncio.CancelledError:
            raise
        except (TimeoutError, httpx.TimeoutException, httpx.TransportError):
            raise ResponsesProviderUnavailable(
                "managed Chat Completions provider is unavailable"
            ) from None
        finally:
            if response is not None:
                await response.aclose()

    async def health(self) -> None:
        request = self._client.build_request(
            "GET",
            self.origin + "/v1/models",
            headers={
                "Authorization": "Bearer " + self._credential(),
                "Accept": "application/json",
                "Accept-Encoding": "identity",
            },
        )
        response: httpx.Response | None = None
        try:
            async with self._slots, asyncio.timeout(self.read_timeout_seconds * 2):
                response = await self._client.send(request, stream=True, follow_redirects=False)
                self._validate_response(response, expected="application/json")
                value = _decode_object(await self._bounded_body(response, limit=1024 * 1024))
                data = value.get("data")
                visible = {
                    item.get("id") for item in data or [] if isinstance(item, dict)
                } if isinstance(data, list) and len(data) <= 10_000 else set()
                if not set(self.model_mapping.values()) <= visible:
                    raise ResponsesProviderUnavailable("managed chat models are unavailable")
        except (ResponsesProviderUnavailable, ResponsesProviderProtocolError):
            raise
        except (TimeoutError, httpx.TimeoutException, httpx.TransportError):
            raise ResponsesProviderUnavailable("managed chat provider is unavailable") from None
        finally:
            if response is not None:
                await response.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    def validate_request(self, request: ModelGatewayRequest, principal: GatewayPrincipal) -> None:
        policy = self.model_policies.get(request.model_id)
        if policy is None or request.model_id not in self.model_mapping:
            raise ResponsesProviderRejected("managed model is unavailable")
        if not principal.account_id or request.model_id not in principal.allowed_model_ids:
            raise ResponsesProviderRejected("managed model is not allowed")
        if request.model_policy != policy:
            raise ResponsesProviderRejected("managed model policy is not allowed")
        if request.tool_projection_budget_version != TOOL_PROJECTION_BUDGET_VERSION:
            raise ResponsesProviderRejected("managed tool budget policy is not allowed")

    def _payload(
        self,
        request: ModelGatewayRequest,
        *,
        prior: DurableChatHandoff | None,
    ) -> tuple[dict[str, Any], dict[str, str]]:
        tools, names = _provider_tools(
            request.direct_tools,
            disclosed_tool_ids=frozenset(request.disclosed_tool_ids),
        )
        messages: list[dict[str, Any]] = []
        if request.previous_response_id is not None:
            if prior is None and any(
                isinstance(item, GatewayFunctionCallOutputInput)
                for item in request.ordered_input_items()
            ):
                raise ResponsesProviderRejected("chat tool continuation is unavailable")
            if prior is not None:
                messages.append(prior.assistant_message())
        for item in request.ordered_input_items():
            if isinstance(item, GatewayUserMessageInput):
                messages.append({"role": "user", "content": item.content})
            elif isinstance(item, GatewayFunctionCallOutputInput):
                messages.append({
                    "role": "tool",
                    "tool_call_id": item.tool_call_id,
                    "content": json.dumps(item.output, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False),
                })
        projected = [
            {
                "type": "function",
                "function": {
                    "name": tool["name"],
                    "description": tool["description"],
                    "parameters": tool["parameters"],
                    "strict": False,
                },
            }
            for tool in tools
        ]
        payload: dict[str, Any] = {
            "model": self.model_mapping[request.model_id],
            "messages": messages,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if projected:
            payload["tools"] = projected
            payload["parallel_tool_calls"] = False
        return payload, names

    async def _stage_handoff(
        self,
        request: ModelGatewayRequest,
        parser: "_ChatCompletionParser",
        event: GatewayEvent,
    ) -> None:
        if event.event_type is not GatewayEventType.TOOL_CALL_REQUESTED:
            return
        message = parser.handoff_message
        if message is None:
            raise ResponsesProviderProtocolError("chat handoff context is missing")
        tool_calls = message.get("tool_calls")
        if not isinstance(tool_calls, list) or len(tool_calls) != 1:
            raise ResponsesProviderProtocolError("chat handoff context is invalid")
        function = tool_calls[0].get("function") if isinstance(tool_calls[0], dict) else None
        if not isinstance(function, dict):
            raise ResponsesProviderProtocolError("chat handoff context is invalid")
        await asyncio.to_thread(
            self._handoff_authority.stage_chat_handoff,
            request,
            self._model_revision,
            event,
            provider_tool_name=function.get("name"),
            arguments_json=_canonical(event.arguments).decode("utf-8"),
        )

    def _credential(self) -> str:
        try:
            token = self._bearer_token()
        except Exception:
            raise ResponsesProviderUnavailable("managed provider credential is unavailable") from None
        if not isinstance(token, str) or not 8 <= len(token) <= 8192 or any(
            character.isspace() or ord(character) < 33 for character in token
        ):
            raise ResponsesProviderUnavailable("managed provider credential is unavailable")
        return token

    @staticmethod
    async def _bounded_body(response: httpx.Response, *, limit: int = _MAX_BODY) -> bytes:
        body = bytearray()
        async for chunk in response.aiter_bytes(64 * 1024):
            body.extend(chunk)
            if len(body) > limit:
                raise ResponsesProviderProtocolError("chat provider response is oversized")
        return bytes(body)

    @staticmethod
    def _validate_response(response: httpx.Response, *, expected: str | None = None) -> None:
        if response.status_code == 429 or response.status_code >= 500:
            raise ResponsesProviderUnavailable("managed chat provider is unavailable")
        if not 200 <= response.status_code < 300:
            raise ResponsesProviderRejected("managed chat provider rejected the request")
        if response.headers.get("content-encoding", "identity").strip().casefold() not in {"", "identity"}:
            raise ResponsesProviderProtocolError("chat provider response encoding is unsupported")
        media = response.headers.get("content-type", "").split(";", 1)[0].strip().casefold()
        allowed = {expected} if expected else {"text/event-stream", "application/json"}
        if media not in allowed:
            raise ResponsesProviderProtocolError("chat provider response content type is invalid")


class _ChatCompletionParser:
    def __init__(self, request_id: str, *, expected_model_id: str, tool_names: Mapping[str, str]) -> None:
        self.request_id = request_id
        self.expected_model_id = expected_model_id
        self.tool_names = dict(tool_names)
        self.seq = 1
        self.response_id: str | None = None
        self.terminal = False
        self._call: dict[str, str] | None = None
        self.handoff_message: dict[str, Any] | None = None
        self._pending_finish: str | None = None

    def feed_stream(self, raw: Mapping[str, Any]) -> tuple[GatewayEvent, ...]:
        self._identity(raw)
        choices = raw.get("choices")
        if not isinstance(choices, list) or len(choices) > 1:
            raise ResponsesProviderProtocolError("chat provider choices are invalid")
        output: list[GatewayEvent] = []
        if choices:
            choice = choices[0]
            if not isinstance(choice, Mapping) or choice.get("index", 0) != 0:
                raise ResponsesProviderProtocolError("chat provider choice is invalid")
            delta = choice.get("delta")
            if not isinstance(delta, Mapping):
                raise ResponsesProviderProtocolError("chat provider delta is invalid")
            summary = delta.get("reasoning_summary")
            if isinstance(summary, str) and summary:
                output.append(self._event(GatewayEventType.REASONING_SUMMARY_DELTA, delta=summary, reasoning_id="reasoning_" + hashlib.sha256(self.request_id.encode()).hexdigest()[:24]))
            content = delta.get("content")
            if isinstance(content, str) and content:
                output.append(self._event(GatewayEventType.OUTPUT_TEXT_DELTA, delta=content))
            self._tool_delta(delta.get("tool_calls"))
            finish = choice.get("finish_reason")
            if finish is not None:
                if finish not in {"stop", "length", "tool_calls"}:
                    raise ResponsesProviderProtocolError("chat provider finish reason is invalid")
                self._pending_finish = finish
                if raw.get("usage") is not None:
                    output.append(self._terminal(finish, raw.get("usage")))
        elif self._pending_finish is not None and raw.get("usage") is not None:
            output.append(self._terminal(self._pending_finish, raw.get("usage")))
        return tuple(output)

    def finish_stream(self) -> GatewayEvent | None:
        if self.terminal:
            return None
        if self._pending_finish is None:
            raise ResponsesProviderProtocolError("chat provider stream is incomplete")
        return self._terminal(self._pending_finish, None)

    def feed_response(self, raw: Mapping[str, Any]) -> tuple[GatewayEvent, ...]:
        self._identity(raw)
        choices = raw.get("choices")
        if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], Mapping):
            raise ResponsesProviderProtocolError("chat provider response choices are invalid")
        choice = choices[0]
        message = choice.get("message")
        if not isinstance(message, Mapping):
            raise ResponsesProviderProtocolError("chat provider message is invalid")
        output: list[GatewayEvent] = []
        summary = message.get("reasoning_summary")
        if isinstance(summary, str) and summary:
            output.append(self._event(GatewayEventType.REASONING_SUMMARY_DELTA, delta=summary, reasoning_id="reasoning_" + hashlib.sha256(self.request_id.encode()).hexdigest()[:24]))
        content = message.get("content")
        if isinstance(content, str) and content:
            output.append(self._event(GatewayEventType.OUTPUT_TEXT_DELTA, delta=content))
        self._tool_delta(message.get("tool_calls"), complete=True)
        output.append(self._terminal(choice.get("finish_reason"), raw.get("usage")))
        return tuple(output)

    def _identity(self, raw: Mapping[str, Any]) -> None:
        if self.terminal:
            raise ResponsesProviderProtocolError("chat provider emitted data after terminal")
        response_id = raw.get("id")
        model = raw.get("model")
        if not _provider_id(response_id) or model != self.expected_model_id:
            raise ResponsesProviderProtocolError("chat provider identity is invalid")
        if self.response_id is not None and self.response_id != response_id:
            raise ResponsesProviderProtocolError("chat provider changed response identity")
        self.response_id = str(response_id)

    def _tool_delta(self, value: Any, *, complete: bool = False) -> None:
        if value is None:
            return
        if not isinstance(value, list) or len(value) != 1 or not isinstance(value[0], Mapping):
            raise ResponsesProviderProtocolError("parallel chat tool calls are unsupported")
        raw = value[0]
        if raw.get("index", 0) != 0:
            raise ResponsesProviderProtocolError("parallel chat tool calls are unsupported")
        function = raw.get("function")
        if not isinstance(function, Mapping):
            raise ResponsesProviderProtocolError("chat tool call is invalid")
        call_id = raw.get("id")
        name = function.get("name")
        arguments = function.get("arguments", "")
        if self._call is None:
            if not _provider_id(call_id) or not isinstance(name, str) or name not in self.tool_names:
                raise ResponsesProviderProtocolError("chat tool identity is invalid")
            self._call = {"id": str(call_id), "provider_name": name, "name": self.tool_names[name], "arguments": ""}
        elif call_id not in {None, self._call["id"]} or name not in {None, self._call["provider_name"]}:
            raise ResponsesProviderProtocolError("parallel chat tool calls are unsupported")
        if not isinstance(arguments, str):
            raise ResponsesProviderProtocolError("chat tool arguments are invalid")
        combined = arguments if complete else self._call["arguments"] + arguments
        if len(combined.encode()) > _MAX_ARGUMENTS:
            raise ResponsesProviderProtocolError("chat tool arguments are oversized")
        self._call["arguments"] = combined

    def _terminal(self, finish: Any, usage: Any) -> GatewayEvent:
        normalized = _chat_usage(usage)
        if finish == "tool_calls":
            if self._call is None:
                raise ResponsesProviderProtocolError("chat tool handoff is missing")
            arguments = _decode_object(self._call["arguments"].encode())
            self.handoff_message = {"role": "assistant", "content": None, "tool_calls": [{"id": self._call["id"], "type": "function", "function": {"name": self._call["provider_name"], "arguments": self._call["arguments"]}}]}
            event = self._event(
                GatewayEventType.TOOL_CALL_REQUESTED,
                tool_call_id=self._call["id"],
                tool_name=self._call["name"],
                arguments=arguments,
                idempotency_key="tool_" + hashlib.sha256((self.request_id + "\0" + self._call["id"]).encode()).hexdigest()[:48],
                usage=normalized,
            )
        elif finish in {"stop", "length"}:
            event = self._event(GatewayEventType.RESPONSE_COMPLETED, usage=normalized)
        else:
            raise ResponsesProviderProtocolError("chat provider finish reason is invalid")
        self.terminal = True
        return event

    def _event(self, event_type: GatewayEventType, **values: Any) -> GatewayEvent:
        if self.response_id is None:
            raise ResponsesProviderProtocolError("chat provider response identity is missing")
        event = GatewayEvent(seq=self.seq, event_type=event_type, response_id=self.response_id, **values)
        self.seq += 1
        return event


def _chat_usage(value: Any) -> dict[str, int] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ResponsesProviderProtocolError("chat provider usage is invalid")
    result: dict[str, int] = {}
    for source, target in (("prompt_tokens", "input_tokens"), ("completion_tokens", "output_tokens"), ("total_tokens", "total_tokens")):
        item = value.get(source)
        if item is not None:
            if isinstance(item, bool) or not isinstance(item, int) or not 0 <= item <= 10**12:
                raise ResponsesProviderProtocolError("chat provider usage is invalid")
            result[target] = item
    return result or None


__all__ = ["ManagedHTTPSChatCompletionsProvider"]
