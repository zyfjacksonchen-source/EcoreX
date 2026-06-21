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
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlparse

from common import const


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
                if isinstance(part, dict) and part.get("type") in ("output_text", "text"):
                    text_parts.append(part.get("text") or "")
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
