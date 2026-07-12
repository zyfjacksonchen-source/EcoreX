"""Fixed-origin HTTPS adapter for an OpenAI-compatible Responses stream.

This is the only first-party v1 text-model provider boundary.  It deliberately
does not accept a URL from a model request, follows no redirect, ignores proxy
environment variables, fetches a credential for each call and never retries a
possibly accepted provider request.  The gateway request id is the upstream
idempotency key; durable recovery is owned by :mod:`ecorex.gateway.server`.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Mapping
import hashlib
import ipaddress
import json
import re
from types import MappingProxyType
from typing import Any
from urllib.parse import urlsplit

import httpx

from ecorex.managed_model_policy import (
    ECOREX_CHAT_MODEL_POLICY,
    require_managed_chat_mapping,
)

from .models import (
    MAX_DISCLOSED_WORKING_SET,
    MAX_MODEL_VISIBLE_TOOLS,
    MAX_TOOL_DESCRIPTOR_BYTES,
    MAX_TOOL_SCHEMA_BATCH_BYTES,
    TOOL_PROJECTION_BUDGET_VERSION,
    GatewayEvent,
    GatewayEventType,
    GatewayFunctionCallOutputInput,
    GatewayModelPolicy,
    GatewayUserMessageInput,
    ModelGatewayRequest,
    ecorex_chat_gateway_policy,
)
from .server import GatewayPrincipal


_MODEL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_PROVIDER_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_FUNCTION_NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_MAX_PROVIDER_REQUEST_BYTES = 4 * 1024 * 1024
_MAX_PROVIDER_EVENT_BYTES = 1024 * 1024
_MAX_PROVIDER_STREAM_BYTES = 16 * 1024 * 1024
_MAX_PROVIDER_EVENTS = 20_000
_MAX_ARGUMENT_BYTES = 1024 * 1024
_MAX_HEALTH_BYTES = 1024 * 1024


class ResponsesProviderConfigurationError(RuntimeError):
    """The fixed provider boundary is absent or unsafe."""


class ResponsesProviderUnavailable(RuntimeError):
    """The provider or its workload credential is temporarily unavailable."""

    # A POST may already have crossed the provider boundary.  The Gateway must
    # not turn transport uncertainty into an automatic second billable call.
    retryable = False


class ResponsesProviderRejected(RuntimeError):
    """The provider rejected a request without exposing its response body."""

    retryable = False


class ResponsesProviderProtocolError(RuntimeError):
    """The provider returned an invalid or unbounded Responses stream."""

    retryable = False


def normalize_https_origin(value: str) -> str:
    """Return one canonical public HTTPS origin, rejecting URL capabilities."""

    if not isinstance(value, str) or not 1 <= len(value) <= 2048:
        raise ResponsesProviderConfigurationError("provider origin is invalid")
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.port not in {None, 443}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise ResponsesProviderConfigurationError("provider origin is invalid")
    host = parsed.hostname.rstrip(".").casefold()
    if not host or host == "localhost" or ":" in host:
        # IPv6 literals and localhost are not production provider identities;
        # tests inject an HTTP transport while retaining a real-looking host.
        raise ResponsesProviderConfigurationError("provider origin is invalid")
    try:
        host.encode("ascii")
    except UnicodeEncodeError:
        raise ResponsesProviderConfigurationError("provider origin is invalid") from None
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise ResponsesProviderConfigurationError("provider origin is invalid")
    return "https://" + host


class ManagedHTTPSResponsesProvider:
    """Translate one bounded provider SSE stream into gateway domain events."""

    def __init__(
        self,
        *,
        origin: str,
        allowed_origins: frozenset[str],
        model_mapping: Mapping[str, str],
        model_policies: Mapping[str, GatewayModelPolicy] | None = None,
        bearer_token: Callable[[], str],
        connect_timeout_seconds: float = 5.0,
        read_timeout_seconds: float = 30.0,
        total_timeout_seconds: float = 240.0,
        max_concurrency: int = 64,
        max_connections: int = 128,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        normalized_origin = normalize_https_origin(origin)
        try:
            normalized_allowlist = frozenset(
                normalize_https_origin(item) for item in allowed_origins
            )
        except TypeError:
            raise ResponsesProviderConfigurationError(
                "provider origin allowlist is invalid"
            ) from None
        if normalized_origin not in normalized_allowlist:
            raise ResponsesProviderConfigurationError("provider origin is not allowlisted")
        mapping = dict(model_mapping)
        try:
            require_managed_chat_mapping(mapping)
        except ValueError:
            raise ResponsesProviderConfigurationError(
                "provider model mapping violates managed model policy"
            ) from None
        policies = dict(
            model_policies
            if model_policies is not None
            else {
                ECOREX_CHAT_MODEL_POLICY.local_model_id: (
                    ecorex_chat_gateway_policy()
                )
            }
        )
        if (
            not 1 <= len(mapping) <= 128
            or any(
                not isinstance(local, str)
                or _MODEL_ID.fullmatch(local) is None
                or not isinstance(upstream, str)
                or _PROVIDER_ID.fullmatch(upstream) is None
                for local, upstream in mapping.items()
            )
            or len(set(mapping.values())) != len(mapping)
            or set(policies) != set(mapping)
            or any(
                not isinstance(policy, GatewayModelPolicy)
                or policy.local_model_id != local_model_id
                or policy.upstream_model_id != mapping[local_model_id]
                for local_model_id, policy in policies.items()
            )
            or not callable(bearer_token)
            or not 0.1 <= connect_timeout_seconds <= 30.0
            or not 0.5 <= read_timeout_seconds <= 120.0
            or not read_timeout_seconds <= total_timeout_seconds <= 900.0
            or not 1 <= max_concurrency <= max_connections <= 512
        ):
            raise ResponsesProviderConfigurationError(
                "provider configuration is invalid"
            )
        self.origin = normalized_origin
        self.allowed_origins = normalized_allowlist
        self.model_mapping = MappingProxyType(mapping)
        self.model_policies = MappingProxyType(policies)
        self._bearer_token = bearer_token
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
                keepalive_expiry=15.0,
            ),
            follow_redirects=False,
            trust_env=False,
            http2=False,
        )
        if not isinstance(self._client, httpx.AsyncClient):
            raise ResponsesProviderConfigurationError("provider HTTP client is invalid")

    async def stream(
        self,
        request: ModelGatewayRequest,
        principal: GatewayPrincipal,
    ) -> AsyncIterator[GatewayEvent]:
        self.validate_request(request, principal)
        payload, tool_name_mapping = self._payload(request, principal)
        encoded = _canonical(payload)
        if len(encoded) > _MAX_PROVIDER_REQUEST_BYTES:
            raise ResponsesProviderRejected("provider request is oversized")
        headers = self._headers()
        headers.update(
            {
                "Accept": "text/event-stream",
                "Accept-Encoding": "identity",
                "Content-Type": "application/json",
                "Idempotency-Key": request.request_id,
                "User-Agent": "EcoreX-Model-Gateway/1.0",
            }
        )
        outbound = self._client.build_request(
            "POST",
            self.origin + "/v1/responses",
            headers=headers,
            content=encoded,
        )
        outbound.extensions["timeout"] = self._timeout_extension()
        async with self._slots:
            response: httpx.Response | None = None
            try:
                async with asyncio.timeout(self.total_timeout_seconds):
                    response = await self._client.send(
                        outbound, stream=True, follow_redirects=False
                    )
                    self._validate_stream_response(response)
                    parser = _ResponsesEventParser(
                        request.request_id,
                        tool_name_mapping=tool_name_mapping,
                        expected_model_id=self.model_mapping[request.model_id],
                    )
                    pending_tool: GatewayEvent | None = None
                    async for data in _iter_sse_data(response):
                        if data == b"[DONE]":
                            if not parser.terminal:
                                raise ResponsesProviderProtocolError(
                                    "provider stream ended without a terminal response"
                                )
                            return
                        raw = _decode_object(data)
                        for event in parser.feed(raw):
                            if event.event_type is GatewayEventType.TOOL_CALL_REQUESTED:
                                if pending_tool is not None:
                                    raise ResponsesProviderProtocolError(
                                        "parallel provider tool calls are unsupported"
                                    )
                                pending_tool = event
                                continue
                            if (
                                pending_tool is not None
                                and event.event_type is GatewayEventType.RESPONSE_FAILED
                            ):
                                # The buffered handoff was never delivered, so
                                # close its sequence gap with the provider fact.
                                event = event.model_copy(
                                    update={"seq": pending_tool.seq}
                                )
                            if (
                                event.event_type is GatewayEventType.RESPONSE_COMPLETED
                                and pending_tool is not None
                            ):
                                # A Runtime tool call is a model-round handoff.
                                # Deliver it only after the provider has durably
                                # completed/stored this Responses object, and do
                                # not expose the provider completion as a second
                                # Gateway terminal for the same round.
                                yield pending_tool
                                return
                            if pending_tool is not None:
                                raise ResponsesProviderProtocolError(
                                    "provider emitted data after a tool handoff"
                                )
                            yield event
                            if event.event_type in {
                                GatewayEventType.RESPONSE_COMPLETED,
                                GatewayEventType.RESPONSE_FAILED,
                            }:
                                return
                    if not parser.terminal:
                        raise ResponsesProviderProtocolError(
                            "provider stream ended without a terminal response"
                        )
            except (ResponsesProviderRejected, ResponsesProviderProtocolError):
                raise
            except asyncio.CancelledError:
                raise
            except (
                TimeoutError,
                httpx.TimeoutException,
                httpx.TransportError,
            ):
                # The POST may have crossed the provider boundary.  The caller
                # persists an uncertain terminal and must not invoke us again.
                raise ResponsesProviderUnavailable(
                    "managed Responses provider is unavailable"
                ) from None
            finally:
                if response is not None:
                    try:
                        await response.aclose()
                    except Exception:
                        pass

    async def health(self) -> None:
        """Perform one authenticated, bounded, fixed-origin dependency probe."""

        headers = self._headers()
        headers.update(
            {
                "Accept": "application/json",
                "Accept-Encoding": "identity",
                "User-Agent": "EcoreX-Model-Gateway/1.0",
            }
        )
        outbound = self._client.build_request(
            "GET", self.origin + "/v1/models", headers=headers
        )
        outbound.extensions["timeout"] = self._timeout_extension()
        async with self._slots:
            response: httpx.Response | None = None
            try:
                async with asyncio.timeout(
                    min(self.total_timeout_seconds, self.read_timeout_seconds * 2)
                ):
                    response = await self._client.send(
                        outbound, stream=True, follow_redirects=False
                    )
                    if response.status_code != 200:
                        raise ResponsesProviderUnavailable(
                            "managed Responses provider is not ready"
                        )
                    self._validate_identity_encoding(response)
                    media_type = response.headers.get("content-type", "").split(";", 1)[0].strip().casefold()
                    if media_type != "application/json":
                        raise ResponsesProviderProtocolError(
                            "provider health content type is invalid"
                        )
                    body = await _bounded_body(response, _MAX_HEALTH_BYTES)
                    value = _decode_object(body)
                    data = value.get("data")
                    if not isinstance(data, list) or len(data) > 10_000:
                        raise ResponsesProviderProtocolError(
                            "provider model catalog is invalid"
                        )
                    visible = {
                        item.get("id")
                        for item in data
                        if isinstance(item, dict) and isinstance(item.get("id"), str)
                    }
                    if not set(self.model_mapping.values()) <= visible:
                        raise ResponsesProviderUnavailable(
                            "configured provider models are unavailable"
                        )
            except (
                ResponsesProviderUnavailable,
                ResponsesProviderProtocolError,
            ):
                raise
            except asyncio.CancelledError:
                raise
            except (
                TimeoutError,
                httpx.TimeoutException,
                httpx.TransportError,
            ):
                raise ResponsesProviderUnavailable(
                    "managed Responses provider is unavailable"
                ) from None
            finally:
                if response is not None:
                    try:
                        await response.aclose()
                    except Exception:
                        pass

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    def validate_request(
        self,
        request: ModelGatewayRequest,
        principal: GatewayPrincipal,
    ) -> None:
        """Reject Runtime attempts to weaken or replace cloud model policy."""

        policy = self.model_policies.get(request.model_id)
        if policy is None or request.model_id not in self.model_mapping:
            raise ResponsesProviderRejected("managed model is unavailable")
        if principal.account_id == "" or request.model_id not in principal.allowed_model_ids:
            raise ResponsesProviderRejected("managed model is not allowed")
        if request.model_policy != policy:
            raise ResponsesProviderRejected("managed model policy is not allowed")
        if request.tool_projection_budget_version != TOOL_PROJECTION_BUDGET_VERSION:
            raise ResponsesProviderRejected("managed tool budget policy is not allowed")

    def _payload(
        self, request: ModelGatewayRequest, principal: GatewayPrincipal
    ) -> tuple[dict[str, Any], dict[str, str]]:
        self.validate_request(request, principal)
        policy = self.model_policies[request.model_id]
        input_value: list[dict[str, Any]] = []
        for item in request.ordered_input_items():
            if isinstance(item, GatewayFunctionCallOutputInput):
                input_value.append(
                    {
                        "type": "function_call_output",
                        "call_id": item.tool_call_id,
                        "output": json.dumps(
                            item.output,
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                            allow_nan=False,
                        ),
                    }
                )
            elif isinstance(item, GatewayUserMessageInput):
                input_value.append(
                    {
                        "type": "message",
                        "role": "user",
                        "content": [
                            {
                                "type": "input_text",
                                "text": item.content,
                            }
                        ],
                    }
                )
            else:  # pragma: no cover - closed Pydantic union defense
                raise ResponsesProviderRejected("managed model input is invalid")
        provider_tools, tool_name_mapping = _provider_tools(
            request.direct_tools,
            disclosed_tool_ids=frozenset(request.disclosed_tool_ids),
        )
        tool_schema_bytes = (
            len(_canonical(request.direct_tools)) if request.direct_tools else 0
        )
        value: dict[str, Any] = {
            "model": self.model_mapping[request.model_id],
            "input": input_value,
            "stream": True,
            "store": True,
            "reasoning": {"effort": policy.reasoning_effort},
            # This is an actual Responses server-side compaction trigger, not
            # a local token estimate or audit-only annotation.  The provider
            # compacts when its rendered context crosses this exact threshold.
            "context_management": [
                {
                    "type": policy.context_management.type,
                    "compact_threshold": (
                        policy.context_management.compact_threshold_tokens
                    ),
                }
            ],
            "metadata": {
                "ecorex_account_id": principal.account_id,
                "ecorex_request_id": request.request_id,
                "ecorex_trace_id": request.trace_id,
                "ecorex_model_policy_id": policy.policy_id,
                "ecorex_model_policy_version": policy.policy_version,
                "ecorex_compact_threshold_tokens": str(
                    policy.context_management.compact_threshold_tokens
                ),
                "ecorex_tool_budget_version": TOOL_PROJECTION_BUDGET_VERSION,
                "ecorex_tool_schema_bytes": str(tool_schema_bytes),
            },
        }
        if provider_tools:
            value["tools"] = provider_tools
            # Runtime persists one tool handoff per model round.  Asking the
            # provider for serial calls prevents a second side effect from
            # racing past that durable boundary.
            value["parallel_tool_calls"] = False
        if request.previous_response_id is not None:
            value["previous_response_id"] = request.previous_response_id
        return value, tool_name_mapping

    def _headers(self) -> dict[str, str]:
        try:
            token = self._bearer_token()
        except Exception:
            raise ResponsesProviderUnavailable(
                "managed provider credential is unavailable"
            ) from None
        if (
            not isinstance(token, str)
            or not 24 <= len(token) <= 8192
            or any(character.isspace() or ord(character) < 33 for character in token)
        ):
            raise ResponsesProviderUnavailable(
                "managed provider credential is unavailable"
            )
        return {"Authorization": "Bearer " + token}

    def _timeout_extension(self) -> dict[str, float]:
        return {
            "connect": self.connect_timeout_seconds,
            "read": self.read_timeout_seconds,
            "write": self.read_timeout_seconds,
            "pool": self.connect_timeout_seconds,
        }

    @staticmethod
    def _validate_identity_encoding(response: httpx.Response) -> None:
        encoding = response.headers.get("content-encoding", "identity").strip().casefold()
        if encoding not in {"", "identity"}:
            raise ResponsesProviderProtocolError(
                "provider response encoding is unsupported"
            )

    def _validate_stream_response(self, response: httpx.Response) -> None:
        if response.status_code == 429 or response.status_code >= 500:
            raise ResponsesProviderUnavailable("managed Responses provider is unavailable")
        if response.status_code < 200 or response.status_code >= 300:
            raise ResponsesProviderRejected("managed Responses provider rejected the request")
        self._validate_identity_encoding(response)
        media_type = response.headers.get("content-type", "").split(";", 1)[0].strip().casefold()
        if media_type != "text/event-stream":
            raise ResponsesProviderProtocolError("provider stream content type is invalid")
        declared = response.headers.get("content-length")
        if declared is not None and (
            not declared.isdigit() or int(declared) > _MAX_PROVIDER_STREAM_BYTES
        ):
            raise ResponsesProviderProtocolError("provider stream is oversized")


class _ResponsesEventParser:
    _IGNORED = frozenset(
        {
            "response.created",
            "response.queued",
            "response.in_progress",
            "response.content_part.added",
            "response.content_part.done",
            "response.output_text.done",
            "response.output_text.annotation.added",
            "response.reasoning_summary_part.added",
            "response.reasoning_summary_part.done",
            "response.reasoning_summary_text.done",
            # Raw reasoning is never disclosed through the Gateway.  Only the
            # provider's explicit reasoning summary contract is projected.
            "response.reasoning_text.delta",
            "response.reasoning_text.done",
            "response.refusal.delta",
            "response.refusal.done",
        }
    )

    def __init__(
        self,
        request_id: str,
        *,
        tool_name_mapping: Mapping[str, str],
        expected_model_id: str,
    ) -> None:
        self.request_id = request_id
        self.tool_name_mapping = dict(tool_name_mapping)
        self.expected_model_id = expected_model_id
        self.seq = 1
        self._provider_seq: int | None = None
        self.response_id: str | None = None
        self.terminal = False
        self._calls: dict[str, dict[str, str]] = {}
        self._emitted_calls: set[str] = set()

    def feed(self, raw: dict[str, Any]) -> tuple[GatewayEvent, ...]:
        if self.terminal:
            raise ResponsesProviderProtocolError("provider emitted data after terminal")
        event_type = raw.get("type")
        if not isinstance(event_type, str) or len(event_type) > 128:
            raise ResponsesProviderProtocolError("provider event type is invalid")
        provider_seq = raw.get("sequence_number")
        if (
            isinstance(provider_seq, bool)
            or not isinstance(provider_seq, int)
            or not 0 <= provider_seq <= 10_000_000
            or (
                self._provider_seq is None
                and provider_seq not in {0, 1}
            )
            or (
                self._provider_seq is not None
                and provider_seq != self._provider_seq + 1
            )
        ):
            raise ResponsesProviderProtocolError(
                "provider event sequence is not contiguous"
            )
        self._provider_seq = provider_seq
        self._capture_response(raw)
        if event_type in self._IGNORED:
            return ()
        if event_type == "response.output_item.added":
            self._capture_call(raw.get("item"))
            return ()
        if event_type == "response.function_call_arguments.delta":
            self._argument_delta(raw)
            return ()
        if event_type in {
            "response.function_call_arguments.done",
            "response.output_item.done",
        }:
            event = self._call_done(raw)
            return () if event is None else (event,)
        if event_type == "response.output_text.delta":
            delta = raw.get("delta")
            if not isinstance(delta, str) or not delta:
                raise ResponsesProviderProtocolError("provider text delta is invalid")
            return (self._event(GatewayEventType.OUTPUT_TEXT_DELTA, delta=delta),)
        if event_type == "response.reasoning_summary_text.delta":
            delta = raw.get("delta")
            reasoning_id = raw.get("item_id")
            if not isinstance(delta, str) or not delta or not _provider_id(reasoning_id):
                raise ResponsesProviderProtocolError("provider reasoning summary is invalid")
            return (
                self._event(
                    GatewayEventType.REASONING_SUMMARY_DELTA,
                    delta=delta,
                    reasoning_id=reasoning_id,
                ),
            )
        if event_type == "response.completed":
            response = raw.get("response")
            if not isinstance(response, dict):
                raise ResponsesProviderProtocolError("provider completion is invalid")
            self._capture_response_object(response, require_model=True)
            if response.get("status") != "completed":
                raise ResponsesProviderProtocolError("provider completion is invalid")
            usage = _usage(response.get("usage"))
            event = self._event(GatewayEventType.RESPONSE_COMPLETED, usage=usage)
            self.terminal = True
            return (event,)
        if event_type in {"response.failed", "response.incomplete", "error"}:
            response = raw.get("response")
            if isinstance(response, dict):
                self._capture_response_object(response, require_model=False)
            if self.response_id is None:
                self.response_id = "response_" + hashlib.sha256(
                    self.request_id.encode("utf-8")
                ).hexdigest()[:32]
            retryable = event_type != "response.failed" or _retryable_error(raw)
            event = self._event(
                GatewayEventType.RESPONSE_FAILED,
                error_code="provider_response_failed",
                error_message="The managed model provider did not complete the request.",
                retryable=retryable,
            )
            self.terminal = True
            return (event,)
        raise ResponsesProviderProtocolError("provider emitted an unsupported event")

    def _capture_response(self, raw: Mapping[str, Any]) -> None:
        candidate = raw.get("response_id")
        if candidate is None:
            response = raw.get("response")
            if isinstance(response, Mapping):
                self._capture_response_object(response, require_model=False)
                return
        if candidate is not None:
            self._set_response_id(candidate)

    def _capture_response_object(
        self, response: Mapping[str, Any], *, require_model: bool
    ) -> None:
        self._set_response_id(response.get("id"))
        model = response.get("model")
        if (
            (require_model and model is None)
            or (model is not None and model != self.expected_model_id)
        ):
            raise ResponsesProviderProtocolError("provider response model changed")

    def _set_response_id(self, candidate: Any) -> None:
        if not _provider_id(candidate):
            raise ResponsesProviderProtocolError("provider response identity is invalid")
        if self.response_id is not None and self.response_id != candidate:
            raise ResponsesProviderProtocolError("provider changed response identity")
        self.response_id = candidate

    def _capture_call(self, item: Any) -> None:
        if not isinstance(item, dict) or item.get("type") != "function_call":
            return
        item_id = item.get("id")
        call_id = item.get("call_id")
        provider_name = item.get("name")
        arguments = item.get("arguments", "")
        if (
            not _provider_id(item_id)
            or not _provider_id(call_id)
            or not isinstance(provider_name, str)
            or _FUNCTION_NAME.fullmatch(provider_name) is None
            or not isinstance(arguments, str)
            or len(arguments.encode("utf-8")) > _MAX_ARGUMENT_BYTES
        ):
            raise ResponsesProviderProtocolError("provider tool call is invalid")
        name = self.tool_name_mapping.get(provider_name)
        if name is None:
            raise ResponsesProviderProtocolError("provider selected an unknown tool")
        self._calls[item_id] = {
            "call_id": call_id,
            "name": name,
            "provider_name": provider_name,
            "arguments": arguments,
        }

    def _argument_delta(self, raw: Mapping[str, Any]) -> None:
        item_id = raw.get("item_id")
        delta = raw.get("delta")
        if not _provider_id(item_id) or not isinstance(delta, str):
            raise ResponsesProviderProtocolError("provider tool arguments are invalid")
        call = self._calls.get(item_id)
        if call is None:
            raise ResponsesProviderProtocolError("provider tool call is missing")
        combined = call["arguments"] + delta
        if len(combined.encode("utf-8")) > _MAX_ARGUMENT_BYTES:
            raise ResponsesProviderProtocolError("provider tool arguments are oversized")
        call["arguments"] = combined

    def _call_done(self, raw: Mapping[str, Any]) -> GatewayEvent | None:
        item = raw.get("item")
        if isinstance(item, dict):
            if item.get("type") != "function_call":
                return None
            self._capture_call(item)
            item_id = item.get("id")
            done_arguments = item.get("arguments")
        else:
            item_id = raw.get("item_id")
            done_arguments = raw.get("arguments")
        if not _provider_id(item_id):
            # output_item.done for non-function content is intentionally ignored.
            if isinstance(item, dict) and item.get("type") != "function_call":
                return None
            raise ResponsesProviderProtocolError("provider tool call is invalid")
        call = self._calls.get(item_id)
        if call is None:
            raise ResponsesProviderProtocolError("provider tool call is missing")
        done_name = raw.get("name")
        if done_name is not None and done_name != call["provider_name"]:
            raise ResponsesProviderProtocolError("provider tool identity changed")
        if item_id in self._emitted_calls:
            return None
        if done_arguments is not None:
            if not isinstance(done_arguments, str):
                raise ResponsesProviderProtocolError("provider tool arguments are invalid")
            if call["arguments"] and call["arguments"] != done_arguments:
                raise ResponsesProviderProtocolError("provider tool arguments changed")
            call["arguments"] = done_arguments
        arguments = _decode_object(call["arguments"].encode("utf-8"))
        self._emitted_calls.add(item_id)
        idempotency = "tool_" + hashlib.sha256(
            (self.request_id + "\0" + call["call_id"]).encode("utf-8")
        ).hexdigest()[:48]
        return self._event(
            GatewayEventType.TOOL_CALL_REQUESTED,
            tool_call_id=call["call_id"],
            tool_name=call["name"],
            arguments=arguments,
            idempotency_key=idempotency,
        )

    def _event(self, event_type: GatewayEventType, **values: Any) -> GatewayEvent:
        response_id = self.response_id
        if response_id is None:
            raise ResponsesProviderProtocolError("provider response identity is missing")
        try:
            event = GatewayEvent(
                seq=self.seq,
                event_type=event_type,
                response_id=response_id,
                **values,
            )
        except (TypeError, ValueError):
            raise ResponsesProviderProtocolError("provider event contract is invalid") from None
        self.seq += 1
        return event


async def _iter_sse_data(response: httpx.Response) -> AsyncIterator[bytes]:
    buffer = bytearray()
    data_lines: list[bytes] = []
    total = 0
    events = 0
    async for chunk in response.aiter_bytes(64 * 1024):
        total += len(chunk)
        if total > _MAX_PROVIDER_STREAM_BYTES:
            raise ResponsesProviderProtocolError("provider stream is oversized")
        buffer.extend(chunk)
        if len(buffer) > _MAX_PROVIDER_EVENT_BYTES and b"\n" not in buffer:
            raise ResponsesProviderProtocolError("provider SSE line is oversized")
        while True:
            newline = buffer.find(b"\n")
            if newline < 0:
                break
            line = bytes(buffer[:newline])
            del buffer[: newline + 1]
            if line.endswith(b"\r"):
                line = line[:-1]
            if len(line) > _MAX_PROVIDER_EVENT_BYTES or b"\0" in line:
                raise ResponsesProviderProtocolError("provider SSE line is invalid")
            if not line:
                if data_lines:
                    events += 1
                    if events > _MAX_PROVIDER_EVENTS:
                        raise ResponsesProviderProtocolError("provider emitted too many events")
                    payload = b"\n".join(data_lines)
                    data_lines.clear()
                    if len(payload) > _MAX_PROVIDER_EVENT_BYTES:
                        raise ResponsesProviderProtocolError("provider SSE event is oversized")
                    yield payload
                continue
            if line.startswith(b":"):
                continue
            field, separator, value = line.partition(b":")
            if not separator:
                value = b""
            elif value.startswith(b" "):
                value = value[1:]
            if field == b"data":
                data_lines.append(value)
            elif field not in {b"event", b"id", b"retry"}:
                raise ResponsesProviderProtocolError("provider SSE field is invalid")
    if buffer:
        # Require a complete line.  This prevents ambiguous truncation from
        # being mistaken for a provider terminal event.
        raise ResponsesProviderProtocolError("provider SSE tail is truncated")
    if data_lines:
        events += 1
        if events > _MAX_PROVIDER_EVENTS:
            raise ResponsesProviderProtocolError("provider emitted too many events")
        payload = b"\n".join(data_lines)
        if len(payload) > _MAX_PROVIDER_EVENT_BYTES:
            raise ResponsesProviderProtocolError("provider SSE event is oversized")
        yield payload


async def _bounded_body(response: httpx.Response, maximum: int) -> bytes:
    declared = response.headers.get("content-length")
    if declared is not None and (not declared.isdigit() or int(declared) > maximum):
        raise ResponsesProviderProtocolError("provider response is oversized")
    body = bytearray()
    async for chunk in response.aiter_bytes(64 * 1024):
        if len(body) + len(chunk) > maximum:
            raise ResponsesProviderProtocolError("provider response is oversized")
        body.extend(chunk)
    return bytes(body)


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _decode_object(payload: bytes) -> dict[str, Any]:
    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise ValueError
            value[key] = item
        return value

    if len(payload) > _MAX_PROVIDER_EVENT_BYTES:
        raise ResponsesProviderProtocolError("provider JSON is oversized")
    try:
        value = json.loads(payload.decode("utf-8"), object_pairs_hook=unique)
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError):
        raise ResponsesProviderProtocolError("provider JSON is invalid") from None
    if not isinstance(value, dict):
        raise ResponsesProviderProtocolError("provider JSON is invalid")
    return value


def _provider_id(value: Any) -> bool:
    return isinstance(value, str) and _PROVIDER_ID.fullmatch(value) is not None


def _usage(value: Any) -> dict[str, int] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ResponsesProviderProtocolError("provider usage is invalid")
    output: dict[str, int] = {}
    for name in ("input_tokens", "output_tokens", "total_tokens"):
        item = value.get(name)
        if item is not None:
            if isinstance(item, bool) or not isinstance(item, int) or not 0 <= item <= 10**12:
                raise ResponsesProviderProtocolError("provider usage is invalid")
            output[name] = item
    return output or None


def _retryable_error(raw: Mapping[str, Any]) -> bool:
    error = raw.get("error")
    if not isinstance(error, Mapping):
        response = raw.get("response")
        if isinstance(response, Mapping):
            error = response.get("error")
    if not isinstance(error, Mapping):
        return False
    code = error.get("code") or error.get("type")
    return code in {
        "rate_limit_exceeded",
        "server_error",
        "timeout",
        "overloaded",
    }


def _provider_tools(
    descriptors: list[dict[str, Any]],
    *,
    disclosed_tool_ids: frozenset[str] = frozenset(),
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """Project trusted EcoreX descriptors into the only allowed tool shape."""

    # Pydantic validates the local Gateway request, but this provider boundary
    # deliberately repeats the limits.  ``model_copy(update=...)`` and future
    # alternate transports must not be able to bypass the network fence.
    if len(descriptors) > MAX_MODEL_VISIBLE_TOOLS:
        raise ResponsesProviderRejected("managed tool projection exceeds its count budget")
    if len(disclosed_tool_ids) > MAX_DISCLOSED_WORKING_SET:
        raise ResponsesProviderRejected(
            "managed disclosed tool projection exceeds its count budget"
        )
    try:
        if any(
            len(_canonical(descriptor)) > MAX_TOOL_DESCRIPTOR_BYTES
            for descriptor in descriptors
        ):
            raise ResponsesProviderRejected(
                "managed tool descriptor exceeds its byte budget"
            )
        batch_bytes = len(_canonical(descriptors)) if descriptors else 0
    except (TypeError, ValueError, UnicodeEncodeError):
        raise ResponsesProviderRejected("managed tool descriptor is invalid") from None
    if batch_bytes > MAX_TOOL_SCHEMA_BATCH_BYTES:
        raise ResponsesProviderRejected("managed tool projection exceeds its byte budget")

    provider_tools: list[dict[str, Any]] = []
    names: dict[str, str] = {}
    canonical_ids: set[str] = set()
    for descriptor in descriptors:
        if not isinstance(descriptor, dict) or set(descriptor) != {"spec", "decision"}:
            raise ResponsesProviderRejected("managed tool descriptor is invalid")
        spec = descriptor.get("spec")
        decision = descriptor.get("decision")
        if not isinstance(spec, dict) or not isinstance(decision, dict):
            raise ResponsesProviderRejected("managed tool descriptor is invalid")
        tool_id = spec.get("tool_id")
        description = spec.get("description")
        parameters = spec.get("input_schema")
        if (
            not isinstance(tool_id, str)
            or _MODEL_ID.fullmatch(tool_id) is None
            or tool_id in canonical_ids
            or not isinstance(description, str)
            or not 1 <= len(description) <= 4096
            or not isinstance(parameters, dict)
            or decision.get("tool_id") != tool_id
            or decision.get("tool_version") != spec.get("version")
            or decision.get("exposure")
            not in ({"direct", "deferred"} if tool_id in disclosed_tool_ids else {"direct"})
            or decision.get("eligible") is not True
        ):
            raise ResponsesProviderRejected("managed tool descriptor is invalid")
        provider_name = (
            tool_id
            if _FUNCTION_NAME.fullmatch(tool_id) is not None
            else "ecorex_" + hashlib.sha256(tool_id.encode("utf-8")).hexdigest()[:32]
        )
        if provider_name in names:
            raise ResponsesProviderRejected("managed tool name collision")
        names[provider_name] = tool_id
        canonical_ids.add(tool_id)
        provider_tools.append(
            {
                "type": "function",
                "name": provider_name,
                "description": description,
                "parameters": parameters,
                "strict": False,
            }
        )
    if not disclosed_tool_ids <= canonical_ids:
        raise ResponsesProviderRejected("disclosed tool projection is incomplete")
    return provider_tools, names


__all__ = [
    "ManagedHTTPSResponsesProvider",
    "ResponsesProviderConfigurationError",
    "ResponsesProviderProtocolError",
    "ResponsesProviderRejected",
    "ResponsesProviderUnavailable",
    "normalize_https_origin",
]
