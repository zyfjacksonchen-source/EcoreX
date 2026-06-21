# encoding:utf-8
"""Helpers for planning OpenAI Responses API calls.

The existing runtime still sends production agent traffic through
``/chat/completions``.  This module provides a narrow, testable adapter path for
the official OpenAI provider so stateful Responses features can be introduced
behind explicit gates instead of mixed into the OpenAI-compatible provider path.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional, Tuple
from urllib.parse import urlparse

from common import const
from common.log import logger


RESPONSES_CREATE_PATH = "/responses"
RESPONSES_COMPACT_PATH = "/responses/compact"
RESPONSES_INPUT_TOKENS_PATH = "/responses/input_tokens"
DEFAULT_OPENAI_API_BASE = "https://api.openai.com/v1"

_OPENAI_PROVIDER_IDS = {const.OPENAI, const.OPEN_AI, const.CHATGPT, "openai"}
_OPENAI_API_HOSTS = {"api.openai.com"}


@dataclass(frozen=True)
class ResponsesState:
    """State that can be carried across Responses API turns."""

    previous_response_id: Optional[str] = None
    prompt_cache_key: Optional[str] = None
    prompt_cache_retention: Optional[str] = None
    service_tier: Optional[str] = None
    truncation: str = "disabled"
    store: bool = True
    compacted_input: Optional[List[Dict[str, Any]]] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ResponsesRequestPlan:
    """Concrete request shapes for a Responses-backed model turn."""

    create_path: str
    create_payload: Dict[str, Any]
    compact_path: str
    compact_payload: Dict[str, Any]
    input_tokens_path: str
    input_tokens_payload: Dict[str, Any]
    state: ResponsesState = field(default_factory=ResponsesState)


@dataclass(frozen=True)
class ResponsesAdapterDecision:
    """Explicit gate result for deciding whether Responses can be used."""

    enabled: bool
    reason: str
    provider: str
    api_base: str


def is_official_openai_provider(provider: Optional[str], api_base: Optional[str]) -> bool:
    provider_id = str(provider or "").strip()
    if provider_id not in _OPENAI_PROVIDER_IDS:
        return False

    base = str(api_base or DEFAULT_OPENAI_API_BASE).strip() or DEFAULT_OPENAI_API_BASE
    try:
        parsed = urlparse(base)
        host = (parsed.hostname or "").lower()
        scheme = (parsed.scheme or "https").lower()
    except Exception:
        return False
    if scheme != "https":
        return False
    return host in _OPENAI_API_HOSTS


def decide_responses_adapter(api_config: Dict[str, Any], overrides: Optional[Dict[str, Any]] = None) -> ResponsesAdapterDecision:
    """Return an explicit decision instead of silently switching API surfaces."""

    config = dict(api_config or {})
    override_values = dict(overrides or {})
    provider = str(config.get("provider") or "")
    api_base = str(config.get("api_base") or DEFAULT_OPENAI_API_BASE)
    requested = override_values.get("use_responses_api", config.get("use_responses_api", False))

    if not requested:
        return ResponsesAdapterDecision(False, "disabled", provider, api_base)
    if not is_official_openai_provider(provider, api_base):
        return ResponsesAdapterDecision(False, "non_official_openai_provider", provider, api_base)
    return ResponsesAdapterDecision(True, "enabled", provider, api_base)


def build_responses_plan(
    *,
    model: str,
    messages: Iterable[Dict[str, Any]],
    tools: Optional[Iterable[Dict[str, Any]]] = None,
    stream: bool = False,
    state: Optional[ResponsesState] = None,
    **kwargs: Any,
) -> ResponsesRequestPlan:
    """Build create/compact/token-count payloads from chat-style inputs."""

    active_state = state or ResponsesState()
    input_items, discovered_instructions = chat_messages_to_responses_input(messages)
    active_input = _combine_compacted_input(active_state.compacted_input, input_items)
    instructions = kwargs.get("instructions") or kwargs.get("system") or discovered_instructions
    responses_tools = chat_tools_to_responses_tools(tools or [])

    create_payload: Dict[str, Any] = {
        "model": model,
        "input": active_input,
        "stream": bool(stream),
        "store": active_state.store,
        "truncation": kwargs.get("truncation", active_state.truncation),
    }
    if instructions:
        create_payload["instructions"] = instructions
    if active_state.previous_response_id:
        create_payload["previous_response_id"] = active_state.previous_response_id
    if active_state.prompt_cache_key:
        create_payload["prompt_cache_key"] = active_state.prompt_cache_key
    if active_state.prompt_cache_retention:
        create_payload["prompt_cache_retention"] = active_state.prompt_cache_retention
    if active_state.service_tier:
        create_payload["service_tier"] = active_state.service_tier
    if responses_tools:
        create_payload["tools"] = responses_tools
        create_payload["tool_choice"] = kwargs.get("tool_choice", "auto")

    _copy_known_responses_options(
        create_payload,
        kwargs,
        {
            "include",
            "max_tool_calls",
            "metadata",
            "parallel_tool_calls",
            "reasoning",
            "service_tier",
            "temperature",
            "text",
            "top_p",
        },
    )
    _copy_token_limit(create_payload, kwargs)

    clean_create = _drop_none_values(create_payload)
    compact_payload = build_responses_compact_payload(
        model=model,
        input_items=active_input,
        instructions=instructions,
    )
    input_tokens_payload = build_responses_input_tokens_payload(
        model=model,
        input_items=active_input,
        instructions=instructions,
    )

    return ResponsesRequestPlan(
        create_path=RESPONSES_CREATE_PATH,
        create_payload=clean_create,
        compact_path=RESPONSES_COMPACT_PATH,
        compact_payload=compact_payload,
        input_tokens_path=RESPONSES_INPUT_TOKENS_PATH,
        input_tokens_payload=input_tokens_payload,
        state=active_state,
    )


def build_responses_compact_payload(
    *,
    model: str,
    input_items: Iterable[Dict[str, Any]],
    instructions: Optional[str] = None,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "model": model,
        "input": list(input_items or []),
    }
    if instructions:
        payload["instructions"] = instructions
    return payload


def build_responses_input_tokens_payload(
    *,
    model: str,
    input_items: Iterable[Dict[str, Any]],
    instructions: Optional[str] = None,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "model": model,
        "input": list(input_items or []),
    }
    if instructions:
        payload["instructions"] = instructions
    return payload


def _combine_compacted_input(
    compacted_input: Optional[Iterable[Dict[str, Any]]],
    fresh_input: Iterable[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    if compacted_input:
        return list(compacted_input) + list(fresh_input or [])
    return list(fresh_input or [])


def extract_responses_state(response: Dict[str, Any], previous: Optional[ResponsesState] = None) -> ResponsesState:
    """Build next-turn state from a Responses or compaction response."""

    prior = previous or ResponsesState()
    is_compaction = isinstance(response, dict) and response.get("object") == "response.compaction"
    response_id = response.get("id") if isinstance(response, dict) and not is_compaction else None
    output = response.get("output") if isinstance(response, dict) else None
    compacted_input = output if isinstance(output, list) and is_compaction else prior.compacted_input

    return ResponsesState(
        previous_response_id=None if is_compaction else response_id or prior.previous_response_id,
        prompt_cache_key=prior.prompt_cache_key,
        prompt_cache_retention=prior.prompt_cache_retention,
        service_tier=response.get("service_tier") if isinstance(response, dict) and response.get("service_tier") else prior.service_tier,
        truncation=prior.truncation,
        store=prior.store,
        compacted_input=compacted_input,
    )


def chat_messages_to_responses_input(messages: Iterable[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """Convert OpenAI chat-style messages into Responses input items."""

    input_items: List[Dict[str, Any]] = []
    instruction_parts: List[str] = []

    for message in messages or []:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "").strip()
        content = message.get("content")

        if role in ("system", "developer"):
            text = _content_to_text(content)
            if text:
                instruction_parts.append(text)
            continue

        if role == "tool":
            input_items.append({
                "type": "function_call_output",
                "call_id": message.get("tool_call_id") or message.get("call_id") or "",
                "output": _content_to_text(content),
            })
            continue

        if role == "assistant":
            item = {
                "role": "assistant",
                "content": _content_to_responses_parts(content, output=True),
            }
            if message.get("id"):
                item["id"] = message["id"]
            if message.get("tool_calls"):
                input_items.append(item)
                input_items.extend(_chat_tool_calls_to_response_items(message.get("tool_calls") or []))
                continue
            input_items.append(item)
            continue

        if role == "user":
            input_items.append({
                "role": "user",
                "content": _content_to_responses_parts(content, output=False),
            })

    instructions = "\n\n".join(part for part in instruction_parts if part)
    return input_items, instructions or None


def chat_tools_to_responses_tools(tools: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Flatten Chat Completions function tools into Responses tool objects."""

    converted: List[Dict[str, Any]] = []
    for tool in tools or []:
        if not isinstance(tool, dict):
            continue
        if tool.get("type") == "function" and isinstance(tool.get("function"), dict):
            function = tool.get("function") or {}
            item = {
                "type": "function",
                "name": function.get("name"),
                "description": function.get("description"),
                "parameters": function.get("parameters") or {},
            }
            if "strict" in function:
                item["strict"] = function["strict"]
            converted.append(_drop_none_values(item))
            continue
        converted.append(dict(tool))
    return converted


def normalize_responses_output_to_chat(response: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize a non-stream Responses object into a Chat Completions shape."""

    text_parts: List[str] = []
    tool_calls: List[Dict[str, Any]] = []
    for item in response.get("output") or []:
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if item_type == "message":
            for part in item.get("content") or []:
                if isinstance(part, dict) and part.get("type") in ("output_text", "text", "refusal"):
                    text_parts.append(part.get("text") or part.get("refusal") or "")
        elif item_type in ("function_call", "custom_tool_call"):
            tool_calls.append(_response_function_call_to_chat_tool_call(item))

    message: Dict[str, Any] = {
        "role": "assistant",
        "content": "".join(text_parts),
    }
    if tool_calls:
        message["tool_calls"] = tool_calls

    return {
        "id": response.get("id"),
        "object": "chat.completion",
        "model": response.get("model"),
        "choices": [{
            "index": 0,
            "message": message,
            "finish_reason": _responses_finish_reason(response),
        }],
        "usage": _responses_usage_to_chat_usage(response.get("usage") or {}),
        "responses_api": {
            "status": response.get("status"),
            "previous_response_id": response.get("previous_response_id"),
            "service_tier": response.get("service_tier"),
        },
    }


def normalize_responses_stream_events_to_chat(
    events: Iterable[Dict[str, Any]],
    *,
    on_completed: Optional[Callable[[Dict[str, Any]], None]] = None,
) -> Iterator[Dict[str, Any]]:
    """Normalize Responses stream events into Chat Completions stream chunks.

    The agent runtime already consumes OpenAI chat-completion deltas. Keeping the
    conversion here lets official OpenAI Responses streaming use the existing
    retry, telemetry, tool-call, and cancellation paths without widening the
    agent stream contract.
    """

    normalizer = _ResponsesStreamNormalizer(on_completed=on_completed)
    yield from normalizer.normalize(events)


class _ResponsesStreamNormalizer:
    def __init__(self, *, on_completed: Optional[Callable[[Dict[str, Any]], None]] = None):
        self.on_completed = on_completed
        self.response_id = ""
        self.model = ""
        self.created = None
        self._next_tool_index = 0
        self._tool_by_key: Dict[Any, Dict[str, Any]] = {}
        self._content_by_key: Dict[Any, str] = {}
        self._emitted_output = False

    def normalize(self, events: Iterable[Dict[str, Any]]) -> Iterator[Dict[str, Any]]:
        for event in events:
            if not isinstance(event, dict):
                continue
            if event.get("choices"):
                self._emitted_output = True
                yield event
                continue
            if event.get("error"):
                yield _responses_stream_error_chunk(event)
                continue

            event_type = str(event.get("type") or event.get("event") or "").strip()
            response = event.get("response") if isinstance(event.get("response"), dict) else None
            if response:
                self._remember_response(response)

            if event_type in ("response.created", "response.in_progress", "response.queued"):
                continue

            if event_type == "response.output_item.added":
                chunk = self._handle_output_item(event.get("item"), event)
                if chunk:
                    self._emitted_output = True
                    yield chunk
                continue

            if event_type == "response.output_text.delta":
                chunk = self._handle_text_delta(event, category="text")
                if chunk:
                    self._emitted_output = True
                    yield chunk
                continue

            if event_type == "response.output_text.done":
                chunk = self._handle_text_done(event, category="text", value_key="text")
                if chunk:
                    self._emitted_output = True
                    yield chunk
                continue

            if event_type == "response.refusal.delta":
                chunk = self._handle_text_delta(event, category="refusal")
                if chunk:
                    self._emitted_output = True
                    yield chunk
                continue

            if event_type == "response.refusal.done":
                chunk = self._handle_text_done(event, category="refusal", value_key="refusal")
                if chunk:
                    self._emitted_output = True
                    yield chunk
                continue

            if event_type in (
                "response.reasoning_text.delta",
                "response.reasoning_summary_text.delta",
            ):
                delta = event.get("delta") or event.get("text") or ""
                if delta:
                    self._emitted_output = True
                    yield self._chunk({"reasoning_content": str(delta)})
                continue

            if event_type == "response.function_call_arguments.delta":
                chunk = self._handle_function_arguments_delta(event)
                if chunk:
                    self._emitted_output = True
                    yield chunk
                continue

            if event_type == "response.function_call_arguments.done":
                chunk = self._handle_function_arguments_done(event)
                if chunk:
                    self._emitted_output = True
                    yield chunk
                continue

            if event_type == "response.output_item.done":
                for chunk in self._handle_output_item_done(event.get("item"), event):
                    self._emitted_output = True
                    yield chunk
                continue

            if event_type in ("response.failed", "response.incomplete", "response.cancelled"):
                response = response or event
                yield _responses_status_error_chunk(response)
                continue

            if event_type == "response.completed":
                response = response or event
                self._remember_response(response)
                if not self._emitted_output:
                    for chunk in self._chunks_from_final_response(response):
                        yield chunk
                if self.on_completed:
                    self._notify_completed(response)
                yield self._chunk(
                    {},
                    finish_reason=_responses_finish_reason(response),
                    usage=response.get("usage") or {},
                    responses_api={
                        "status": response.get("status"),
                        "previous_response_id": response.get("previous_response_id"),
                        "service_tier": response.get("service_tier"),
                    },
                )
                continue

            if event_type == "error":
                yield _responses_stream_error_chunk(event)

    def _notify_completed(self, response: Dict[str, Any]) -> None:
        try:
            self.on_completed(response)
        except Exception as exc:
            logger.warning("[ResponsesAdapter] completed-stream state callback failed: %s", exc)

    def _remember_response(self, response: Dict[str, Any]) -> None:
        if response.get("id"):
            self.response_id = str(response.get("id") or "")
        if response.get("model"):
            self.model = str(response.get("model") or "")
        if response.get("created_at") is not None:
            self.created = response.get("created_at")

    def _chunk(
        self,
        delta: Dict[str, Any],
        *,
        finish_reason: Optional[str] = None,
        usage: Optional[Dict[str, Any]] = None,
        responses_api: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        chunk: Dict[str, Any] = {
            "id": self.response_id,
            "object": "chat.completion.chunk",
            "model": self.model,
            "choices": [{
                "index": 0,
                "delta": delta,
                "finish_reason": finish_reason,
            }],
        }
        if self.created is not None:
            chunk["created"] = self.created
        if usage:
            chunk["usage"] = usage
        if responses_api:
            chunk["responses_api"] = responses_api
        return chunk

    def _handle_output_item(self, item: Any, event: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if not isinstance(item, dict):
            return None
        if item.get("type") not in ("function_call", "custom_tool_call"):
            return None
        tool = self._remember_tool(item, event)
        return self._tool_chunk(
            tool,
            function={
                "name": tool.get("name") or "",
                "arguments": "",
            },
            include_id=True,
        )

    def _handle_text_delta(self, event: Dict[str, Any], *, category: str) -> Optional[Dict[str, Any]]:
        delta = event.get("delta")
        if delta is None:
            delta = event.get("text", "")
        if not delta:
            return None
        key = self._content_key(event, category)
        self._content_by_key[key] = self._content_by_key.get(key, "") + str(delta)
        return self._chunk({"content": str(delta)})

    def _handle_text_done(self, event: Dict[str, Any], *, category: str, value_key: str) -> Optional[Dict[str, Any]]:
        final_text = str(event.get(value_key) or event.get("text") or "")
        if not final_text:
            return None
        key = self._content_key(event, category)
        emitted = self._content_by_key.get(key, "")
        if final_text == emitted:
            return None
        delta = final_text[len(emitted):] if final_text.startswith(emitted) else final_text
        self._content_by_key[key] = emitted + delta
        return self._chunk({"content": delta})

    @staticmethod
    def _content_key(event: Dict[str, Any], category: str) -> Any:
        return (
            category,
            str(event.get("item_id") or ""),
            str(event.get("output_index") if event.get("output_index") is not None else ""),
            str(event.get("content_index") if event.get("content_index") is not None else ""),
        )

    def _handle_output_item_done(self, item: Any, event: Dict[str, Any]) -> Iterator[Dict[str, Any]]:
        if not isinstance(item, dict):
            return
        if item.get("type") not in ("function_call", "custom_tool_call"):
            return
        tool = self._remember_tool(item, event)
        name = item.get("name") or tool.get("name") or ""
        if name and name != tool.get("emitted_name"):
            tool["emitted_name"] = name
            yield self._tool_chunk(tool, function={"name": name}, include_id=not tool.get("emitted_id"))

        arguments = str(item.get("arguments") or "")
        emitted_args = str(tool.get("arguments") or "")
        if arguments and arguments != emitted_args:
            delta = arguments[len(emitted_args):] if arguments.startswith(emitted_args) else arguments
            tool["arguments"] = emitted_args + delta
            yield self._tool_chunk(tool, function={"arguments": delta}, include_id=False)

    def _handle_function_arguments_delta(self, event: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        tool = self._tool_for_event(event)
        delta = event.get("delta")
        if delta is None:
            delta = event.get("arguments_delta", "")
        if not delta:
            return None
        tool["arguments"] = str(tool.get("arguments") or "") + str(delta)
        return self._tool_chunk(tool, function={"arguments": str(delta)}, include_id=False)

    def _handle_function_arguments_done(self, event: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        tool = self._tool_for_event(event)
        function_delta: Dict[str, str] = {}
        include_id = not bool(tool.get("emitted_id"))
        name = str(event.get("name") or "")
        if name and name != tool.get("emitted_name"):
            tool["name"] = name
            tool["emitted_name"] = name
            function_delta["name"] = name
        arguments = str(event.get("arguments") or "")
        emitted_args = str(tool.get("arguments") or "")
        if arguments and arguments != emitted_args:
            delta = arguments[len(emitted_args):] if arguments.startswith(emitted_args) else arguments
            tool["arguments"] = emitted_args + delta
            function_delta["arguments"] = delta
        if not function_delta:
            return None
        return self._tool_chunk(tool, function=function_delta, include_id=include_id)

    def _tool_for_event(self, event: Dict[str, Any]) -> Dict[str, Any]:
        key = self._tool_key(event)
        tool = self._tool_by_key.get(key)
        if tool is not None:
            return tool
        tool = {
            "index": self._coerce_tool_index(event.get("output_index")),
            "id": str(event.get("call_id") or event.get("item_id") or ""),
            "name": str(event.get("name") or ""),
            "arguments": "",
            "emitted_id": False,
            "emitted_name": "",
        }
        self._tool_by_key[key] = tool
        return tool

    def _remember_tool(self, item: Dict[str, Any], event: Dict[str, Any]) -> Dict[str, Any]:
        key = self._tool_key(event, item)
        tool = self._tool_by_key.get(key)
        if tool is None:
            tool = {
                "index": self._coerce_tool_index(event.get("output_index")),
                "arguments": "",
                "emitted_id": False,
                "emitted_name": "",
            }
            self._tool_by_key[key] = tool
        tool["id"] = str(item.get("call_id") or item.get("id") or event.get("item_id") or tool.get("id") or "")
        tool["name"] = str(item.get("name") or tool.get("name") or "")
        return tool

    def _tool_key(self, event: Dict[str, Any], item: Optional[Dict[str, Any]] = None) -> Any:
        item = item or {}
        for value in (
            event.get("item_id"),
            item.get("id"),
            item.get("call_id"),
            event.get("call_id"),
        ):
            if value:
                return ("id", str(value))
        if event.get("output_index") is not None:
            return ("index", self._coerce_tool_index(event.get("output_index")))
        key = ("index", self._next_tool_index)
        self._next_tool_index += 1
        return key

    @staticmethod
    def _coerce_tool_index(value: Any) -> int:
        try:
            return max(0, int(value or 0))
        except (TypeError, ValueError):
            return 0

    def _tool_chunk(
        self,
        tool: Dict[str, Any],
        *,
        function: Dict[str, str],
        include_id: bool,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "index": self._coerce_tool_index(tool.get("index")),
            "type": "function",
            "function": function,
        }
        if include_id and tool.get("id"):
            payload["id"] = tool.get("id")
            tool["emitted_id"] = True
        if function.get("name"):
            tool["emitted_name"] = function["name"]
        return self._chunk({"tool_calls": [payload]})

    def _chunks_from_final_response(self, response: Dict[str, Any]) -> Iterator[Dict[str, Any]]:
        normalized = normalize_responses_output_to_chat(response)
        message = ((normalized.get("choices") or [{}])[0].get("message") or {})
        content = message.get("content") or ""
        if content:
            self._emitted_output = True
            yield self._chunk({"content": content})
        for index, call in enumerate(message.get("tool_calls") or []):
            function = call.get("function") if isinstance(call.get("function"), dict) else {}
            tool = {
                "index": index,
                "id": call.get("id") or "",
                "name": function.get("name") or "",
                "arguments": function.get("arguments") or "",
                "emitted_id": False,
                "emitted_name": "",
            }
            self._emitted_output = True
            yield self._tool_chunk(
                tool,
                function={
                    "name": tool["name"],
                    "arguments": tool["arguments"],
                },
                include_id=True,
            )


def _responses_stream_error_chunk(event: Dict[str, Any]) -> Dict[str, Any]:
    error = event.get("error")
    if isinstance(error, dict):
        message = error.get("message") or event.get("message") or "Responses API stream error"
        code = error.get("code") or ""
        error_type = error.get("type") or ""
    else:
        message = event.get("message") or str(error or "Responses API stream error")
        code = event.get("code") or event.get("error_code") or ""
        error_type = event.get("error_type") or ""
    return {
        "error": {
            "message": message,
            "code": code,
            "type": error_type,
        },
        "message": message,
        "status_code": event.get("status_code") or 500,
        "retry_after": event.get("retry_after"),
    }


def _responses_status_error_chunk(response: Dict[str, Any]) -> Dict[str, Any]:
    status = str(response.get("status") or response.get("type") or "failed").split(".")[-1]
    error = response.get("error") if isinstance(response.get("error"), dict) else {}
    incomplete = response.get("incomplete_details") if isinstance(response.get("incomplete_details"), dict) else {}
    message = (
        error.get("message")
        or incomplete.get("reason")
        or f"Responses API returned status: {status}"
    )
    status_code = response.get("status_code")
    if not status_code:
        status_code = 400 if status == "incomplete" else 499 if status == "cancelled" else 500
    return {
        "error": {
            "message": message,
            "code": error.get("code") or incomplete.get("reason") or status,
            "type": error.get("type") or f"responses_{status}",
        },
        "message": message,
        "status_code": status_code,
    }


def _copy_known_responses_options(payload: Dict[str, Any], kwargs: Dict[str, Any], keys: Iterable[str]) -> None:
    for key in keys:
        if key in kwargs:
            payload[key] = kwargs[key]


def _copy_token_limit(payload: Dict[str, Any], kwargs: Dict[str, Any]) -> None:
    if "max_output_tokens" in kwargs:
        payload["max_output_tokens"] = kwargs["max_output_tokens"]
    elif "max_tokens" in kwargs:
        payload["max_output_tokens"] = kwargs["max_tokens"]


def _drop_none_values(payload: Dict[str, Any]) -> Dict[str, Any]:
    return {key: value for key, value in payload.items() if value is not None}


def _content_to_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        pieces: List[str] = []
        for block in content:
            if not isinstance(block, dict):
                pieces.append(str(block))
                continue
            if block.get("type") in ("text", "input_text", "output_text"):
                pieces.append(str(block.get("text") or ""))
            elif "content" in block:
                pieces.append(_content_to_text(block.get("content")))
        return "\n".join(piece for piece in pieces if piece)
    return json.dumps(content, ensure_ascii=False)


def _content_to_responses_parts(content: Any, *, output: bool) -> List[Dict[str, Any]]:
    text_type = "output_text" if output else "input_text"
    if isinstance(content, list):
        parts: List[Dict[str, Any]] = []
        for block in content:
            if not isinstance(block, dict):
                parts.append({"type": text_type, "text": str(block)})
                continue
            block_type = block.get("type")
            if block_type in ("text", "input_text", "output_text"):
                parts.append({"type": text_type, "text": str(block.get("text") or "")})
            elif block_type == "image_url":
                image = block.get("image_url") or {}
                image_url = image.get("url") if isinstance(image, dict) else image
                parts.append({"type": "input_image", "image_url": image_url})
            elif block_type in ("input_image", "input_file"):
                parts.append(dict(block))
            else:
                text = _content_to_text(block)
                if text:
                    parts.append({"type": text_type, "text": text})
        return parts or [{"type": text_type, "text": ""}]
    return [{"type": text_type, "text": _content_to_text(content)}]


def _chat_tool_calls_to_response_items(tool_calls: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    for call in tool_calls or []:
        if not isinstance(call, dict):
            continue
        function = call.get("function") or {}
        if not isinstance(function, dict):
            function = {}
        items.append({
            "type": "function_call",
            "call_id": call.get("id") or call.get("call_id") or "",
            "name": function.get("name") or call.get("name") or "",
            "arguments": function.get("arguments") or call.get("arguments") or "{}",
            "status": call.get("status") or "completed",
        })
    return items


def _response_function_call_to_chat_tool_call(item: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": item.get("call_id") or item.get("id") or "",
        "type": "function",
        "function": {
            "name": item.get("name") or "",
            "arguments": item.get("arguments") or "{}",
        },
    }


def _responses_finish_reason(response: Dict[str, Any]) -> str:
    status = response.get("status")
    if status == "completed":
        return "stop"
    if status in ("failed", "cancelled", "incomplete"):
        return status
    return "stop"


def _responses_usage_to_chat_usage(usage: Dict[str, Any]) -> Dict[str, Any]:
    input_tokens = usage.get("input_tokens", usage.get("prompt_tokens", 0)) or 0
    output_tokens = usage.get("output_tokens", usage.get("completion_tokens", 0)) or 0
    total_tokens = usage.get("total_tokens", input_tokens + output_tokens) or 0
    prompt_details = usage.get("input_tokens_details") or usage.get("prompt_tokens_details") or {}
    completion_details = usage.get("output_tokens_details") or usage.get("completion_tokens_details") or {}
    return {
        "prompt_tokens": input_tokens,
        "completion_tokens": output_tokens,
        "total_tokens": total_tokens,
        "prompt_tokens_details": {
            "cached_tokens": prompt_details.get("cached_tokens", 0),
        },
        "completion_tokens_details": {
            "reasoning_tokens": completion_details.get("reasoning_tokens", 0),
        },
    }
