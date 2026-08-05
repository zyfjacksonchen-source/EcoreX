"""
Agent Bridge - Integrates Agent system with existing COW bridge
"""

import os
import hashlib
import json
import traceback
from typing import Any, Dict, Optional, List

from agent.protocol import Agent, LLMModel, LLMRequest, get_cancel_registry
from bridge.agent_event_handler import AgentEventHandler
from bridge.agent_initializer import AgentInitializer
from bridge.bridge import Bridge
from bridge.context import Context
from bridge.reply import Reply, ReplyType
from common import const
from common.ecorex_identity import sanitize_assistant_identity, sanitize_messages_identity
from common.log import logger
from common.utils import expand_path
from config import conf
from models.legacy_reply_gateway import suppress_legacy_reply_text_telemetry
from models.model_fallback import (
    ModelFallbackRoute,
    annotate_fallback_result,
    configured_model_fallback_routes,
    should_try_model_fallback,
)
from models.model_gateway import call_native_model_with_gateway
from models.model_telemetry import chunk_has_model_output, is_model_error_response
from models.openai_compatible_bot import OpenAICompatibleBot


def _exception_log_summary(value: Any) -> Dict[str, Any]:
    text = "" if value is None else str(value)
    return {
        "redacted": bool(text),
        "hash": hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:16] if text else "",
        "chars": len(text),
        "bytes": len(text.encode("utf-8", errors="replace")),
        "type": type(value).__name__ if value is not None else "",
    }


def _public_exception_message(prefix: str, value: Any) -> str:
    summary = _exception_log_summary(value)
    if not summary["hash"]:
        return prefix
    return (
        f"{prefix} Details redacted "
        f"(type={summary['type']}, hash={summary['hash']}, "
        f"chars={summary['chars']}, bytes={summary['bytes']})."
    )


def _exception_diagnostic_snapshot(value: BaseException, *, session_id: str = "", request_id: str = "") -> Dict[str, Any]:
    frames = []
    try:
        extracted = traceback.extract_tb(value.__traceback__)
        for frame in extracted[-12:]:
            frames.append({
                "file": os.path.basename(frame.filename or ""),
                "line": int(frame.lineno or 0),
                "name": frame.name or "",
            })
    except Exception:
        frames = []
    model = ""
    provider = ""
    try:
        model = str(conf().get("model") or "")
        from models.model_capabilities import infer_provider_id

        provider = infer_provider_id(
            model,
            configured_bot_type=str(conf().get("bot_type") or ""),
            use_linkai=bool(conf().get("use_linkai", False)),
            has_linkai_key=bool(conf().get("linkai_api_key")),
            use_azure_chatgpt=bool(conf().get("use_azure_chatgpt", False)),
            gemini_api_base=conf().get("gemini_api_base") or "",
            has_gemini_key=bool(conf().get("gemini_api_key")),
            gemini_api_key=conf().get("gemini_api_key") or "",
            custom_api_base=conf().get("custom_api_base") or "",
            custom_api_key=conf().get("custom_api_key") or "",
        )
    except Exception:
        provider = str(conf().get("bot_type") or "")
    return {
        **_exception_log_summary(value),
        "session_id": session_id or "",
        "request_id": request_id or "",
        "model": model,
        "provider": provider,
        "tracebackFrames": frames,
    }


def _clear_responses_state_for_session(session_id: str) -> None:
    if not session_id:
        return
    try:
        from models.openai.responses_state_store import clear_responses_state_for_session

        removed = clear_responses_state_for_session(session_id)
        if removed:
            logger.info(f"[AgentBridge] Cleared Responses state: session={session_id}, removed={removed}")
    except Exception as e:
        logger.warning(f"[AgentBridge] Failed to clear Responses state for {session_id}: {_exception_log_summary(e)}")


def _assistant_message_text(message: Dict[str, Any]) -> str:
    if not isinstance(message, dict) or message.get("role") != "assistant":
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: List[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = str(block.get("text") or "").strip()
                if text:
                    parts.append(text)
        return "\n".join(parts).strip()
    return ""


def _ensure_final_response_in_messages(agent: Agent, new_messages: List[Dict[str, Any]], response: str) -> List[Dict[str, Any]]:
    if getattr(agent, "_last_run_final_response_persistable", True) is not True:
        return new_messages
    text = sanitize_assistant_identity((response or "").strip())
    if not text:
        return new_messages
    normalized = text.strip().lower()
    if normalized in {"_(cancelled)_", "_(cancelled by user)_"} or "cancelled by user" in normalized:
        return new_messages
    for message in reversed(new_messages):
        if sanitize_assistant_identity(_assistant_message_text(message)).strip() == text:
            return new_messages

    synthetic_message = {
        "role": "assistant",
        "content": [{
            "type": "text",
            "text": text,
        }],
    }
    new_messages.append(synthetic_message)

    try:
        with agent.messages_lock:
            latest_assistant_text = ""
            for message in reversed(agent.messages):
                if isinstance(message, dict) and message.get("role") == "assistant":
                    latest_assistant_text = sanitize_assistant_identity(_assistant_message_text(message)).strip()
                    break
            if latest_assistant_text != text:
                agent.messages.append(dict(synthetic_message))
    except Exception as exc:
        logger.warning(f"[AgentBridge] Failed to mirror synthetic final response into memory: {_exception_log_summary(exc)}")

    return new_messages


def _failed_run_messages_for_persistence(
    messages: List[Dict[str, Any]], *, user_pre_persisted: bool
) -> List[Dict[str, Any]]:
    preserved = list(messages)
    if not user_pre_persisted or not preserved or preserved[0].get("role") != "user":
        return preserved
    content = preserved[0].get("content")
    text = "\n".join(
        str(block.get("text") or "")
        for block in (content if isinstance(content, list) else [])
        if isinstance(block, dict) and block.get("type") == "text"
    )
    marker = text.find("[e-Mate Runtime continuity note:")
    if marker < 0:
        return preserved[1:]
    preserved[0] = {
        "role": "assistant",
        "content": [{"type": "text", "text": text[marker:]}],
    }
    return preserved


def add_openai_compatible_support(bot_instance):
    """
    Dynamically add OpenAI-compatible tool calling support to a bot instance.
    
    This allows any bot to gain tool calling capability without modifying its code,
    as long as it uses OpenAI-compatible API format.
    
    Note: Some bots like ZHIPUAIBot have native tool calling support and don't need enhancement.
    """
    if hasattr(bot_instance, 'call_with_tools'):
        # Bot already has tool calling support (e.g., ZHIPUAIBot)
        logger.debug(f"[AgentBridge] {type(bot_instance).__name__} already has native tool calling support")
        return bot_instance

    # Create a temporary mixin class that combines the bot with OpenAI compatibility
    class EnhancedBot(bot_instance.__class__, OpenAICompatibleBot):
        """Dynamically enhanced bot with OpenAI-compatible tool calling"""

        def get_api_config(self):
            """
            Infer API config from common configuration patterns.
            Most OpenAI-compatible bots use similar configuration.
            """
            from config import conf

            return {
                'api_key': conf().get("open_ai_api_key"),
                'api_base': conf().get("open_ai_api_base"),
                'model': conf().get("model", "gpt-3.5-turbo"),
                'default_temperature': conf().get("temperature", 0.9),
                'default_top_p': conf().get("top_p", 1.0),
                'default_frequency_penalty': conf().get("frequency_penalty", 0.0),
                'default_presence_penalty': conf().get("presence_penalty", 0.0),
            }

    # Change the bot's class to the enhanced version
    bot_instance.__class__ = EnhancedBot
    logger.info(
        f"[AgentBridge] Enhanced {bot_instance.__class__.__bases__[0].__name__} with OpenAI-compatible tool calling")

    return bot_instance


class AgentLLMModel(LLMModel):
    """
    LLM Model adapter that uses COW's existing bot infrastructure
    """

    _MODEL_BOT_TYPE_MAP = {
        "wenxin": const.BAIDU, "wenxin-4": const.BAIDU,
        "xunfei": const.XUNFEI, const.QWEN: const.QWEN_DASHSCOPE,
        const.QIANFAN: const.QIANFAN,
        const.MODELSCOPE: const.MODELSCOPE,
    }
    _MODEL_PREFIX_MAP = [
        ("qwen", const.QWEN_DASHSCOPE), ("qwq", const.QWEN_DASHSCOPE), ("qvq", const.QWEN_DASHSCOPE),
        ("gemini", const.GEMINI), ("glm", const.ZHIPU_AI), ("claude", const.CLAUDEAPI),
        ("moonshot", const.MOONSHOT), ("kimi", const.MOONSHOT),
        ("doubao", const.DOUBAO), ("deepseek", const.DEEPSEEK),
        ("ernie", const.QIANFAN),
        ("mimo-", const.MIMO),
    ]

    def __init__(self, bridge: Bridge, bot_type: str = "chat"):
        super().__init__(model=conf().get("model", const.GPT_41))
        self.bridge = bridge
        self.bot_type = bot_type
        self._bot = None
        self._bot_model = None
        self._bot_cache = {}
        self._bot_type = None

    @property
    def model(self):
        return conf().get("model", const.GPT_41)

    @model.setter
    def model(self, value):
        pass

    def _resolve_bot_type(self, model_name: str) -> str:
        """Resolve bot type from model name, matching Bridge.__init__ logic."""
        from models.model_capabilities import infer_provider_id

        return infer_provider_id(
            model_name,
            configured_bot_type=conf().get("bot_type") or "",
            use_linkai=bool(conf().get("use_linkai", False)),
            has_linkai_key=bool(conf().get("linkai_api_key")),
            use_azure_chatgpt=bool(conf().get("use_azure_chatgpt", False)),
            gemini_api_base=conf().get("gemini_api_base") or "",
            has_gemini_key=bool(conf().get("gemini_api_key")),
            gemini_api_key=conf().get("gemini_api_key") or "",
            custom_api_base=conf().get("custom_api_base") or "",
            custom_api_key=conf().get("custom_api_key") or "",
        )

    @property
    def bot(self):
        """Lazy load the bot, re-create when model or bot_type changes"""
        cur_model = self.model
        cur_bot_type = self._resolve_bot_type(cur_model)
        if self._bot is None or self._bot_model != cur_model or getattr(self, '_bot_type', None) != cur_bot_type:
            self._bot = self._create_bot(cur_bot_type)
            self._bot_model = cur_model
            self._bot_type = cur_bot_type
        return self._bot

    def reset_route_cache(self):
        """Drop cached bot instances while preserving the owning Agent memory."""
        self._bot = None
        self._bot_model = None
        self._bot_type = None
        self._bot_cache = {}

    @staticmethod
    def _create_bot(bot_type: str):
        from models.bot_factory import create_bot

        bot = add_openai_compatible_support(create_bot(bot_type))
        if bot_type:
            setattr(bot, "_ecorex_route_bot_type", bot_type)
            configure_route = getattr(bot, "configure_model_route", None)
            if callable(configure_route):
                configure_route(bot_type)
        return bot

    @staticmethod
    def _uses_shared_openai_gateway(bot) -> bool:
        return getattr(type(bot), "call_with_tools", None) is OpenAICompatibleBot.call_with_tools

    def _model_max_retries_for_request(self, request: LLMRequest):
        value = getattr(request, "model_max_retries", None)
        if value is None:
            value = getattr(request, "max_model_retries", None)
        return value

    def _primary_model_route(self) -> ModelFallbackRoute:
        bot_type = self._resolve_bot_type(self.model)
        return ModelFallbackRoute(
            model=self.model,
            bot_type=bot_type,
            provider=bot_type,
            reason="primary",
            index=0,
        )

    def _model_call_routes(self) -> List[ModelFallbackRoute]:
        primary = self._primary_model_route()
        fallbacks = configured_model_fallback_routes(
            conf(),
            primary_model=primary.model,
            primary_bot_type=primary.bot_type,
        )
        return [primary] + fallbacks

    def _get_bot_for_route(self, route: ModelFallbackRoute):
        if route.index == 0:
            return self.bot
        cache = getattr(self, "_bot_cache", None)
        if cache is None:
            cache = {}
            self._bot_cache = cache
        bot_type = route.bot_type or route.provider or self._resolve_bot_type(route.model)
        key = (route.model, bot_type)
        if key not in cache:
            cache[key] = self._create_bot(bot_type)
        return cache[key]

    def _build_call_kwargs(
        self,
        request: LLMRequest,
        *,
        stream: bool,
        model_name: str,
        provider: Optional[str] = None,
    ) -> Dict[str, Any]:
        from models.model_capabilities import get_model_capabilities, normalize_reasoning_effort

        capabilities = get_model_capabilities(
            model_name,
            provider=provider or self._resolve_bot_type(model_name),
        )
        kwargs: Dict[str, Any] = {
            'messages': request.messages,
            'tools': getattr(request, 'tools', None),
            'stream': stream,
            'model': model_name,
            'retry_count': getattr(request, 'retry_count', 0),
        }
        if request.max_tokens is not None:
            kwargs['max_tokens'] = request.max_tokens
        retry_sleep = getattr(request, 'model_retry_sleep', None)
        if callable(retry_sleep):
            kwargs['model_retry_sleep'] = retry_sleep
        max_model_retries = self._model_max_retries_for_request(request)
        if max_model_retries is not None:
            kwargs['model_max_retries'] = max_model_retries

        system_prompt = getattr(request, 'system', None)
        if system_prompt:
            kwargs['system'] = system_prompt

        channel_type = getattr(self, 'channel_type', None) or ''
        if channel_type:
            kwargs['channel_type'] = channel_type
        session_id = getattr(self, 'session_id', None)
        if session_id:
            kwargs['session_id'] = session_id

        thinking_enabled = bool(conf().get("enable_thinking", False))
        if capabilities.supports_thinking_param:
            kwargs['thinking'] = (
                {"type": "enabled"} if thinking_enabled
                else {"type": "disabled"}
            )
        if thinking_enabled and capabilities.supports_reasoning_effort:
            effort = normalize_reasoning_effort(
                conf().get("reasoning_effort", "max"),
                capabilities,
            )
            if effort:
                kwargs['reasoning_effort'] = effort
        verbosity = getattr(request, "verbosity", None)
        if verbosity in (None, ""):
            verbosity = conf().get("verbosity", None)
        if capabilities.supports_verbosity and verbosity not in (None, ""):
            kwargs["verbosity"] = verbosity
        return kwargs

    def _call_bot_with_gateway(
        self,
        bot,
        kwargs,
        *,
        stream: bool,
        route: Optional[ModelFallbackRoute] = None,
    ):
        if self._uses_shared_openai_gateway(bot):
            return bot.call_with_tools(**kwargs)

        base_retry_count = kwargs.get("retry_count", 0)

        def invoke(absolute_retry_count):
            attempt_kwargs = dict(kwargs)
            attempt_kwargs["retry_count"] = absolute_retry_count
            with suppress_legacy_reply_text_telemetry():
                return bot.call_with_tools(**attempt_kwargs)

        provider = (
            (route.provider or route.bot_type)
            if route is not None
            else getattr(self, "_bot_type", None) or self._resolve_bot_type(self.model)
        )
        model_name = route.model if route is not None else self.model
        return call_native_model_with_gateway(
            invoke,
            provider=provider,
            model=model_name,
            stream=stream,
            retry_count=base_retry_count,
            max_model_retries=kwargs.get("model_max_retries"),
            retry_sleep=kwargs.get("model_retry_sleep"),
        )

    @staticmethod
    def _close_model_stream(stream) -> None:
        close = getattr(stream, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass

    def call(self, request: LLMRequest):
        """
        Call the model using COW's bot infrastructure
        """
        try:
            routes = self._model_call_routes()
            primary_route = routes[0]
            last_response = None
            last_route = primary_route
            for route_index, route in enumerate(routes):
                bot = self._get_bot_for_route(route)
                if not hasattr(bot, 'call_with_tools'):
                    bot_type = type(bot).__name__
                    raise NotImplementedError(
                        f"Bot {bot_type} does not support call_with_tools. Please add the method."
                    )
                kwargs = self._build_call_kwargs(
                    request,
                    stream=False,
                    model_name=route.model,
                    provider=route.provider or route.bot_type,
                )
                response = self._call_bot_with_gateway(bot, kwargs, stream=False, route=route)
                last_response = response
                last_route = route
                should_fallback = (
                    route_index < len(routes) - 1
                    and should_try_model_fallback(response)
                )
                if should_fallback:
                    next_route = routes[route_index + 1]
                    logger.warning(
                        f"[AgentLLMModel] model fallback: "
                        f"{route.provider}/{route.model} -> "
                        f"{next_route.provider}/{next_route.model}"
                    )
                    continue
                if route_index > 0 and isinstance(response, dict):
                    response = annotate_fallback_result(
                        response,
                        from_route=primary_route,
                        to_route=route,
                        exhausted=should_try_model_fallback(response),
                    )
                return self._format_response(response)

            if isinstance(last_response, dict) and len(routes) > 1:
                last_response = annotate_fallback_result(
                    last_response,
                    from_route=primary_route,
                    to_route=last_route,
                    exhausted=True,
                )
            return self._format_response(last_response)
                
        except Exception as e:
            logger.error(f"AgentLLMModel call error: {_exception_log_summary(e)}")
            raise
    
    def call_stream(self, request: LLMRequest):
        """
        Call the model with streaming using COW's bot infrastructure
        """
        try:
            routes = self._model_call_routes()
            primary_route = routes[0]
            for route_index, route in enumerate(routes):
                bot = self._get_bot_for_route(route)
                if not hasattr(bot, 'call_with_tools'):
                    bot_type = type(bot).__name__
                    raise NotImplementedError(
                        f"Bot {bot_type} does not support call_with_tools. Please add the method."
                    )
                kwargs = self._build_call_kwargs(
                    request,
                    stream=True,
                    model_name=route.model,
                    provider=route.provider or route.bot_type,
                )
                stream = self._call_bot_with_gateway(bot, kwargs, stream=True, route=route)
                buffered_chunks = []
                output_started = False
                fallback_next = False
                try:
                    for raw_chunk in stream:
                        chunk = self._format_stream_chunk(raw_chunk)
                        if route_index > 0 and isinstance(chunk, dict):
                            chunk = annotate_fallback_result(
                                chunk,
                                from_route=primary_route,
                                to_route=route,
                                exhausted=(
                                    is_model_error_response(chunk)
                                    and should_try_model_fallback(chunk)
                                ),
                            )
                        if isinstance(chunk, dict) and is_model_error_response(chunk):
                            can_fallback = (
                                not output_started
                                and route_index < len(routes) - 1
                                and should_try_model_fallback(chunk)
                            )
                            if can_fallback:
                                fallback_next = True
                                break
                            for buffered in buffered_chunks:
                                yield buffered
                            yield chunk
                            return

                        if chunk_has_model_output(chunk):
                            output_started = True
                            for buffered in buffered_chunks:
                                yield buffered
                            buffered_chunks = []
                            yield chunk
                        elif output_started:
                            yield chunk
                        else:
                            buffered_chunks.append(chunk)
                    else:
                        for buffered in buffered_chunks:
                            yield buffered
                        return
                except Exception as e:
                    error_chunk = {
                        "error": True,
                        "message": _public_exception_message("Model stream failed.", e),
                        **_exception_log_summary(e),
                        "status_code": 500,
                    }
                    if (
                        not output_started
                        and route_index < len(routes) - 1
                        and should_try_model_fallback(error_chunk)
                    ):
                        fallback_next = True
                    else:
                        for buffered in buffered_chunks:
                            yield buffered
                        yield error_chunk
                        return
                finally:
                    self._close_model_stream(stream)

                if fallback_next:
                    next_route = routes[route_index + 1]
                    logger.warning(
                        f"[AgentLLMModel] stream model fallback before output: "
                        f"{route.provider}/{route.model} -> "
                        f"{next_route.provider}/{next_route.model}"
                    )
                    continue
                return
                
        except Exception as e:
            logger.error(f"AgentLLMModel call_stream error: {_exception_log_summary(e)}")
            raise
    
    def _format_response(self, response):
        """Format Claude response to our expected format"""
        # This would need to be implemented based on Claude's response format
        return response
    
    def _format_stream_chunk(self, chunk):
        """Format Claude stream chunk to our expected format"""
        # This would need to be implemented based on Claude's stream format
        return chunk


class AgentBridge:
    """
    Bridge class that integrates super Agent with COW
    Manages multiple agent instances per session for conversation isolation
    """
    
    def __init__(self, bridge: Bridge):
        self.bridge = bridge
        self.agents = {}  # session_id -> Agent instance mapping
        self.default_agent = None  # For backward compatibility (no session_id)
        self.agent: Optional[Agent] = None
        self.scheduler_initialized = False
        
        # Create helper instances
        self.initializer = AgentInitializer(bridge, self)

        if conf().get("scheduler_enabled", False):
            try:
                from agent.tools.scheduler.integration import init_scheduler
                if init_scheduler(self):
                    self.scheduler_initialized = True
            except Exception as e:
                logger.warning(f"[AgentBridge] Eager scheduler init failed: {_exception_log_summary(e)}")
        else:
            logger.info("[AgentBridge] Scheduler startup skipped; scheduler_enabled is disabled")

        if conf().get("self_evolution_enabled", False):
            try:
                from agent.evolution.trigger import start_evolution_trigger
                start_evolution_trigger(self)
            except Exception as e:
                logger.warning(f"[AgentBridge] Evolution trigger init failed: {_exception_log_summary(e)}")
        else:
            logger.info("[AgentBridge] Self-evolution startup skipped; self_evolution_enabled is disabled")

    def create_agent(self, system_prompt: str, tools: List = None, **kwargs) -> Agent:
        """
        Create the super agent with COW integration
        
        Args:
            system_prompt: System prompt
            tools: List of tools (optional)
            **kwargs: Additional agent parameters
            
        Returns:
            Agent instance
        """
        # Create LLM model that uses COW's bot infrastructure
        model = AgentLLMModel(self.bridge)
        
        # Default tools if none provided
        if tools is None:
            # Use ToolManager to load all available tools
            from agent.tools import ToolManager
            tool_manager = ToolManager()
            tool_manager.load_tools()
            
            tools = []
            workspace_dir = kwargs.get("workspace_dir")
            for tool_name in tool_manager.tool_classes.keys():
                try:
                    tool = tool_manager.create_tool(tool_name)
                    if tool:
                        if workspace_dir:
                            tool.cwd = workspace_dir
                        tools.append(tool)
                except Exception as e:
                    logger.warning(f"[AgentBridge] Failed to load tool {tool_name}: {_exception_log_summary(e)}")
        
        # Create agent instance
        agent = Agent(
            system_prompt=system_prompt,
            description=kwargs.get("description", "AI Super Agent"),
            model=model,
            tools=tools,
            max_steps=kwargs.get("max_steps", 15),
            output_mode=kwargs.get("output_mode", "logger"),
            workspace_dir=kwargs.get("workspace_dir"),
            skill_manager=kwargs.get("skill_manager"),
            enable_skills=kwargs.get("enable_skills", True),
            memory_manager=kwargs.get("memory_manager"),
            max_context_tokens=kwargs.get("max_context_tokens"),
            context_reserve_tokens=kwargs.get("context_reserve_tokens"),
            runtime_info=kwargs.get("runtime_info"),
        )

        # Log skill loading details
        if agent.skill_manager:
            logger.debug(f"[AgentBridge] SkillManager initialized with {len(agent.skill_manager.skills)} skills")

        return agent
    
    def get_agent(self, session_id: str = None) -> Optional[Agent]:
        """
        Get agent instance for the given session
        
        Args:
            session_id: Session identifier (e.g., user_id). If None, returns default agent.
        
        Returns:
            Agent instance for this session
        """
        # If no session_id, use default agent (backward compatibility)
        if session_id is None:
            if self.default_agent is None:
                self._init_default_agent()
            return self.default_agent
        
        # Check if agent exists for this session
        if session_id not in self.agents:
            self._init_agent_for_session(session_id)
        
        return self.agents[session_id]
    
    def _init_default_agent(self):
        """Initialize default super agent"""
        agent = self.initializer.initialize_agent(session_id=None)
        self.default_agent = agent
    
    def _init_agent_for_session(self, session_id: str):
        """Initialize agent for a specific session"""
        agent = self.initializer.initialize_agent(session_id=session_id)
        self.agents[session_id] = agent

    def reset_model_routes(self) -> int:
        """Reset LLM route caches for existing agents without clearing messages."""
        seen_ids = set()
        reset_count = 0
        for agent in [self.default_agent, *self.agents.values()]:
            if agent is None:
                continue
            identity = id(agent)
            if identity in seen_ids:
                continue
            seen_ids.add(identity)
            model = getattr(agent, "model", None)
            reset = getattr(model, "reset_route_cache", None)
            if callable(reset):
                reset()
                reset_count += 1
        logger.info(f"[AgentBridge] Reset model route caches for {reset_count} agents")
        return reset_count

    def sync_session_messages_from_store(self, session_id: str) -> int:
        """Reload an agent's in-memory ``messages`` list from the persistent
        conversation store.

        Used after an external mutation (e.g. user edits / deletes a message
        via the web console) so the agent's next turn sees the same history
        as the database. The operation is a no-op when the agent has not been
        instantiated yet for the session.

        Returns:
            Number of messages now held in the agent's memory. Returns -1 if
            the agent does not exist or has no compatible ``messages`` attr.
        """
        if not session_id or session_id not in self.agents:
            return -1
        agent = self.agents[session_id]
        if not (hasattr(agent, "messages") and hasattr(agent, "messages_lock")):
            return -1
        try:
            from agent.memory import get_conversation_store
            store = get_conversation_store()
            # No turn cap here: we want a faithful mirror of what the store
            # has for this session after deletion.
            remaining = store.load_messages(session_id, max_turns=10**6)
        except Exception as e:
            logger.warning(
                f"[AgentBridge] Failed to load messages for sync (session={session_id}): {_exception_log_summary(e)}"
            )
            return -1
        with agent.messages_lock:
            agent.messages.clear()
            for msg in remaining:
                agent.messages.append({
                    "role": msg["role"],
                    "content": msg["content"],
                })
            count = len(agent.messages)
        logger.info(
            f"[AgentBridge] Synced agent memory for session={session_id}, messages={count}"
        )
        return count

    def agent_reply(self, query: str, context: Context = None, 
                   on_event=None, clear_history: bool = False) -> Reply:
        """
        Use super agent to reply to a query
        
        Args:
            query: User query
            context: COW context (optional, contains session_id for user isolation)
            on_event: Event callback (optional)
            clear_history: Whether to clear conversation history
            
        Returns:
            Reply object
        """
        session_id = None
        agent = None
        request_id = None
        cancel_event = None
        token_key = None
        agentbridge_owns_cancel_token = False
        pre_persisted = False
        internal_action = False
        try:
            # Extract session_id from context for user isolation
            if context:
                session_id = context.kwargs.get("session_id") or context.get("session_id")
                request_id = context.kwargs.get("request_id") or context.get("request_id")
                token_owner = context.kwargs.get("cancel_token_owner") or context.get("cancel_token_owner")
            else:
                token_owner = None

            # Register a cancel token. Prefer per-turn request_id (web),
            # fall back to session_id (IM channels). The Event is polled by
            # AgentStreamExecutor at safe checkpoints.
            registry = get_cancel_registry()
            token_key = request_id or session_id
            if token_key:
                external_owner = token_owner in {"web_channel", "scheduler", "subagent"} and bool(request_id)
                existing_cancel_event = registry.get_event(token_key) if external_owner else None
                cancel_event = registry.register(token_key, session_id=session_id)
                agentbridge_owns_cancel_token = not (external_owner and existing_cancel_event is not None)

            # Get agent for this session (will auto-initialize if needed)
            agent = self.get_agent(session_id=session_id)
            if not agent:
                if token_key and agentbridge_owns_cancel_token:
                    try:
                        registry.unregister(token_key)
                    except Exception:
                        pass
                return Reply(ReplyType.ERROR, "Failed to initialize super agent")
            
            # Create event handler for logging and channel communication
            event_handler = AgentEventHandler(context=context, original_callback=on_event)
            
            # Filter tools based on context
            original_tools = agent.tools
            filtered_tools = original_tools
            
            # If this is a scheduled task execution, exclude scheduler tool to prevent recursion
            if context and context.get("is_scheduled_task"):
                filtered_tools = [tool for tool in agent.tools if tool.name != "scheduler"]
                agent.tools = filtered_tools
                logger.info(f"[AgentBridge] Scheduled task execution: excluded scheduler tool ({len(filtered_tools)}/{len(original_tools)} tools)")
            else:
                # Attach context to scheduler tool if present
                if context and agent.tools:
                    for tool in agent.tools:
                        if tool.name == "scheduler":
                            try:
                                from agent.tools.scheduler.integration import attach_scheduler_to_tool
                                attach_scheduler_to_tool(tool, context)
                            except Exception as e:
                                logger.warning(f"[AgentBridge] Failed to attach context to scheduler: {_exception_log_summary(e)}")
                            break
            
            # Pass context metadata to model for downstream API requests
            if context and hasattr(agent, 'model'):
                agent.model.channel_type = context.get("channel_type", "")
                agent.model.session_id = session_id or ""

            # Store request identity for diagnostics and continuity.
            agent._current_session_id = session_id
            agent._current_request_id = request_id or ""

            # Bound the in-memory context for scheduler sessions before each run.
            # Scheduler sessions are stable per-task and append every trigger,
            # so without trimming they would grow unbounded across runs and
            # blow up prompt cost. Regular user chats are not touched here —
            # the agent's own context manager handles that path.
            if session_id and session_id.startswith("scheduler_"):
                scheduler_keep_turns = max(
                    1, int(conf().get("agent_max_context_turns", 20)) // 5
                )
                self._trim_in_memory_to_turns(agent, scheduler_keep_turns)

            # Eagerly persist the user message BEFORE running the agent so the
            # session and the user's bubble are immediately visible — even if
            # the user switches away or refreshes before the reply finishes.
            # The reply (assistant/tool messages) is appended once the run
            # completes; the final persist skips this already-stored user turn.
            pre_persisted = self._pre_persist_user_message(
                session_id, query, context, clear_history
            )
            internal_action = bool(context and context.get("internal_action"))

            try:
                # Use agent's run_stream method with event handler
                response = agent.run_stream(
                    user_message=query,
                    on_event=event_handler.handle_event,
                    clear_history=clear_history,
                    cancel_event=cancel_event,
                )
            finally:
                # Restore original tools
                if context and context.get("is_scheduled_task"):
                    agent.tools = original_tools

                # Log execution summary
                event_handler.log_summary()

                # Release cancel token; keep registry bounded.
                if token_key and agentbridge_owns_cancel_token:
                    try:
                        registry.unregister(token_key)
                    except Exception:
                        pass

            # Persist new messages generated during this run
            if session_id:
                channel_type = (context.get("channel_type") or "") if context else ""
                new_messages = list(getattr(agent, '_last_run_new_messages', []))
                sanitize_messages_identity(new_messages)
                # The leading user turn was already persisted eagerly above;
                # drop it here so it isn't stored twice.
                if (pre_persisted or internal_action) and new_messages and new_messages[0].get("role") == "user":
                    new_messages = new_messages[1:]
                elif (not pre_persisted and new_messages and new_messages[0].get("role") == "user"
                      and context and context.get("visible_message")):
                    new_messages[0] = {
                        **new_messages[0],
                        "content": [{
                            "type": "text",
                            "text": str(context.get("visible_message") or "").strip()
                        }]
                    }
                new_messages = _ensure_final_response_in_messages(agent, new_messages, response)
                if new_messages:
                    self._persist_messages(
                        session_id,
                        list(new_messages),
                        channel_type,
                        context.get("project_context_meta") if context else None,
                    )
            
            # Record this user turn for the self-evolution idle trigger. Skip
            # scheduler-injected / scheduled-task sessions so internal runs do
            # not count as user activity.
            if conf().get("self_evolution_enabled", False) and session_id and not session_id.startswith("scheduler_") and not (
                context and (context.get("is_scheduled_task") or context.get("internal_action"))
            ):
                try:
                    from agent.evolution.trigger import note_user_turn
                    ch = (context.get("channel_type") or "") if context else ""
                    rcv = (context.get("receiver") or "") if context else ""
                    is_group = bool(context.get("isgroup")) if context else False
                    # Only enable proactive push for single chats (group push is
                    # noisy); group sessions still evolve, just without notify.
                    note_user_turn(agent, channel_type=ch, receiver=(rcv if not is_group else ""))
                except Exception:
                    pass

            # Post-message hot-reload: detect edits to ~/cow/mcp.json and
            # sync any new/removed MCP tools into the live agent in the
            # background. Off the critical path so user latency is unaffected;
            # changes take effect on the user's next message.
            self._schedule_mcp_hot_reload(agent)

            response = sanitize_assistant_identity(response)

            # Check if there are files to send (from send/read tool)
            if hasattr(agent, 'stream_executor') and hasattr(agent.stream_executor, 'files_to_send'):
                files_to_send = agent.stream_executor.files_to_send
                if files_to_send:
                    # Send the first file (for now, handle one file at a time)
                    file_info = files_to_send[0]
                    logger.info(
                        "[AgentBridge] Sending file attachment: "
                        f"name={file_info.get('file_name') or os.path.basename(str(file_info.get('path') or ''))}, "
                        f"type={file_info.get('file_type') or 'file'}"
                    )
                    
                    # Clear files_to_send for next request
                    agent.stream_executor.files_to_send = []
                    
                    # Return file reply based on file type
                    return self._create_file_reply(file_info, response, context)
            
            return Reply(ReplyType.TEXT, response)
            
        except Exception as e:
            public_error = _public_exception_message("Agent error.", e)
            logger.error(
                "Agent reply error diagnostics: "
                + json.dumps(
                    _exception_diagnostic_snapshot(e, session_id=session_id or "", request_id=request_id or ""),
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
            # Persist tool facts produced before a model-format failure. The
            # leading user row may already have been eagerly persisted.
            if session_id and agent:
                try:
                    failed_messages = list(getattr(agent, "_last_run_new_messages", []))
                    sanitize_messages_identity(failed_messages)
                    failed_messages = _failed_run_messages_for_persistence(
                        failed_messages,
                        user_pre_persisted=bool(pre_persisted or internal_action),
                    )
                    if failed_messages:
                        self._persist_messages(
                            session_id,
                            failed_messages,
                            (context.get("channel_type") or "") if context else "",
                            context.get("project_context_meta") if context else None,
                        )
                except Exception as db_err:
                    logger.warning(f"[AgentBridge] Failed to preserve recovery context: {_exception_log_summary(db_err)}")
            # Release cancel token on error path too (idempotent).
            if cancel_event is not None and (request_id or session_id) and agentbridge_owns_cancel_token:
                try:
                    get_cancel_registry().unregister(request_id or session_id)
                except Exception:
                    pass
            return Reply(ReplyType.ERROR, public_error)
    
    def _schedule_mcp_hot_reload(self, agent):
        """
        Fire-and-forget: detect mcp.json edits and reconcile the agent's
        tool dict in the background. Runs after the user's reply is sent,
        so any cost (file stat, hash, server boot) never adds to user latency.
        Failures are isolated and never raise into the message pipeline.
        """
        import threading
        from agent.tools import ToolManager

        def _run():
            try:
                tm = ToolManager()
                ensure_mcp = getattr(tm, "ensure_mcp_configured_loaded", None)
                if callable(ensure_mcp):
                    ensure_mcp(wait_seconds=0.0)
                else:
                    tm.refresh_mcp_if_changed()
                added, removed = tm.sync_mcp_into_agent(agent)
                if added or removed:
                    logger.info(
                        f"[AgentBridge] Agent tools synced — "
                        f"added={added}, removed={removed}"
                    )
            except Exception as e:
                logger.warning(f"[AgentBridge] MCP hot-reload failed (non-fatal): {_exception_log_summary(e)}")

        threading.Thread(target=_run, daemon=True, name="mcp-hot-reload").start()

    def _create_file_reply(self, file_info: dict, text_response: str, context: Context = None) -> Reply:
        """
        Create a reply for sending files
        
        Args:
            file_info: File metadata from read tool
            text_response: Text response from agent
            context: Context object
            
        Returns:
            Reply object for file sending
        """
        file_type = file_info.get("file_type", "file")
        file_path = file_info.get("path")

        # For images, use IMAGE_URL type (channel will handle upload)
        if file_type == "image":
            # Convert local path to file:// URL for channel processing
            file_url = f"file://{file_path}"
            logger.info(f"[AgentBridge] Sending image: {file_url}")
            reply = Reply(ReplyType.IMAGE_URL, file_url)
            # Attach text message if present (for channels that support text+image)
            if text_response:
                reply.text_content = text_response  # Store accompanying text
            return reply
        
        # For all file types (document, video, audio), use FILE type
        if file_type in ["document", "video", "audio"]:
            file_url = f"file://{file_path}"
            logger.info(f"[AgentBridge] Sending {file_type}: {file_url}")
            reply = Reply(ReplyType.FILE, file_url)
            reply.file_name = file_info.get("file_name", os.path.basename(file_path))
            # Attach text message if present
            if text_response:
                reply.text_content = text_response
            return reply
        
        # For all other file types (tar.gz, zip, etc.), also use FILE type
        file_url = f"file://{file_path}"
        logger.info(f"[AgentBridge] Sending generic file: {file_url}")
        reply = Reply(ReplyType.FILE, file_url)
        reply.file_name = file_info.get("file_name", os.path.basename(file_path))
        if text_response:
            reply.text_content = text_response
        return reply
    
    def _migrate_config_to_env(self, workspace_root: str):
        """
        Sync API keys from config.json to .env file.
        Adds new keys and updates changed values on each startup.

        Args:
            workspace_root: Workspace directory path (not used, kept for compatibility)
        """
        from config import conf
        import os
        
        key_mapping = {
            "open_ai_api_key": "OPENAI_API_KEY",
            "open_ai_api_base": "OPENAI_API_BASE",
            "gemini_api_key": "GEMINI_API_KEY",
            "claude_api_key": "CLAUDE_API_KEY",
            "linkai_api_key": "LINKAI_API_KEY",
        }
        
        env_file = expand_path("~/.cow/.env")
        
        # Read existing env vars (key -> value)
        existing_env_vars = {}
        if os.path.exists(env_file):
            try:
                with open(env_file, 'r', encoding='utf-8') as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith('#') and '=' in line:
                            key, val = line.split('=', 1)
                            existing_env_vars[key.strip()] = val.strip()
            except Exception as e:
                logger.warning(f"[AgentBridge] Failed to read .env file: {_exception_log_summary(e)}")
        
        # Sync config.json values into .env (add/update/remove)
        updated = False
        for config_key, env_key in key_mapping.items():
            raw = conf().get(config_key, "")
            value = raw.strip() if raw else ""
            old_value = existing_env_vars.get(env_key)

            if value:
                if old_value == value:
                    continue
                existing_env_vars[env_key] = value
                os.environ[env_key] = value
                updated = True
            else:
                if old_value is None:
                    continue
                existing_env_vars.pop(env_key, None)
                os.environ.pop(env_key, None)
                updated = True
            updated = True

        if updated:
            try:
                env_dir = os.path.dirname(env_file)
                os.makedirs(env_dir, exist_ok=True)

                with open(env_file, 'w', encoding='utf-8') as f:
                    f.write('# Environment variables for agent\n')
                    f.write('# Auto-managed - synced from config.json on startup\n\n')
                    for key, value in sorted(existing_env_vars.items()):
                        f.write(f'{key}={value}\n')

                logger.info("[AgentBridge] Synced API keys from config.json to .env")
            except Exception as e:
                logger.warning(f"[AgentBridge] Failed to sync API keys: {_exception_log_summary(e)}")
    
    def _pre_persist_user_message(
        self, session_id: str, query: str, context: Context, clear_history: bool
    ) -> bool:
        """Persist the user's message before the agent runs.

        This makes a brand-new session (and the user's bubble) visible even if
        the reply hasn't finished — switching away or refreshing no longer
        loses the in-flight session. Returns True when the user turn was
        stored, so the caller can skip it in the post-run persist.

        Best-effort: any failure is swallowed and reported as not-persisted.
        """
        if not session_id or not query:
            return False
        if context and context.get("pre_persisted_user_message"):
            return True
        # Only real user turns: skip scheduler-injected / scheduled-task runs.
        if session_id.startswith("scheduler_") or (
            context and context.get("is_scheduled_task")
        ):
            return False
        if context and context.get("internal_action"):
            return False
        if clear_history:
            _clear_responses_state_for_session(session_id)
        try:
            from config import conf
            if not conf().get("conversation_persistence", True):
                return False
            from agent.memory import get_conversation_store
            store = get_conversation_store()
            # clear_history starts a fresh transcript: wipe the store first so
            # the eager user turn becomes seq 0, matching in-memory state.
            if clear_history:
                store.clear_session(session_id)
            channel_type = (context.get("channel_type") or "") if context else ""
            visible_query = ""
            if context and context.get("visible_message"):
                visible_query = str(context.get("visible_message") or "").strip()
            if not visible_query:
                visible_query = query
            user_msg = {
                "role": "user",
                "content": [{"type": "text", "text": visible_query}],
            }
            attachments = context.get("attachments") if context else None
            if isinstance(attachments, list):
                cleaned = []
                for att in attachments[:20]:
                    if not isinstance(att, dict):
                        continue
                    item = {}
                    for key in ("file_path", "file_name", "file_type", "preview_url"):
                        value = att.get(key)
                        if value:
                            item[key] = str(value)
                    if item.get("file_path"):
                        cleaned.append(item)
                if cleaned:
                    user_msg["extras"] = {"attachments": cleaned}
            store.append_messages(
                session_id,
                [user_msg],
                channel_type=channel_type,
                project_context=context.get("project_context_meta") if context else None,
            )
            return True
        except Exception as e:
            if getattr(e, "code", "") == "SESSION_OWNER_CONFLICT":
                logger.warning(
                    f"[AgentBridge] Refused to pre-persist user message due to session owner conflict: reason={getattr(e, 'reason', 'unknown')}"
                )
                return False
            logger.warning(
                f"[AgentBridge] Failed to pre-persist user message for session={session_id}: {_exception_log_summary(e)}"
            )
            return False

    def _persist_messages(
        self, session_id: str, new_messages: list, channel_type: str = "", project_context: dict = None
    ) -> None:
        """
        Persist new messages to the conversation store after each agent run.

        Failures are logged but never propagate — they must not interrupt replies.
        """
        if not new_messages:
            return
        try:
            from config import conf
            if not conf().get("conversation_persistence", True):
                return
            # When deep-thinking display is disabled, strip "thinking" content
            # blocks before persisting so they don't resurface on history reload.
            # The in-memory message list keeps them intact for this run's
            # multi-turn LLM context.
            thinking_enabled = bool(conf().get("enable_thinking", False))
        except Exception:
            thinking_enabled = False

        messages_to_store = new_messages
        if not thinking_enabled:
            messages_to_store = self._strip_thinking_blocks(new_messages)

        try:
            from agent.memory import get_conversation_store
            get_conversation_store().append_messages(
                session_id,
                messages_to_store,
                channel_type=channel_type,
                project_context=project_context,
            )
        except Exception as e:
            if getattr(e, "code", "") == "SESSION_OWNER_CONFLICT":
                logger.warning(
                    f"[AgentBridge] Refused to persist messages due to session owner conflict: reason={getattr(e, 'reason', 'unknown')}"
                )
                return
            logger.warning(
                f"[AgentBridge] Failed to persist messages for session={session_id}: {_exception_log_summary(e)}"
            )

    # Marker used to identify scheduler-injected user messages so we can apply
    # a sliding window without touching real user turns. The legacy prefix
    # "Scheduled task" (written by the v2 PR) is also recognised when pruning,
    # so old data can be aged out instead of leaking forever.
    _SCHEDULED_MARKER = "[SCHEDULED]"
    _SCHEDULED_LEGACY_MARKERS = ("Scheduled task",)

    def remember_scheduled_output(
        self,
        session_id: str,
        content: str,
        channel_type: str = "",
        task_description: str = "",
    ) -> None:
        """Add the visible output of a scheduled task to the receiver's session.

        Scheduled task execution uses an isolated session so internal planning and
        tool calls do not leak into the user's chat. The final message is still
        part of the conversation from the user's point of view, so keep a small
        visible turn in the receiver session for follow-up questions.

        Configuration:
            scheduler_inject_to_session (bool, default True):
                Master switch. When False, this method is a no-op.
            scheduler_inject_max_per_session (int, default 3):
                Maximum scheduler-injected user/assistant pairs retained per
                session. Older injections are pruned automatically.

        Content is truncated to 2000 chars to prevent a single high-volume task
        from bloating one entry.
        """
        from config import conf
        if not conf().get("scheduler_inject_to_session", True):
            return
        if not session_id or not content:
            return

        max_len = 2000
        if len(content) > max_len:
            content = content[:max_len] + "..."

        user_text = self._SCHEDULED_MARKER
        if task_description:
            user_text = f"{self._SCHEDULED_MARKER} {task_description}"

        messages = [
            {"role": "user", "content": [{"type": "text", "text": user_text}]},
            {"role": "assistant", "content": [{"type": "text", "text": content}]},
        ]

        # Persist first so the new pair gets a stable seq, then prune old
        # scheduler pairs in DB, then sync the in-memory agent.messages buffer.
        self._persist_messages(session_id, messages, channel_type)

        keep_last_n = max(int(conf().get("scheduler_inject_max_per_session", 3) or 0), 0)
        try:
            from agent.memory import get_conversation_store
            deleted = get_conversation_store().prune_scheduled_messages(
                session_id, keep_last_n=keep_last_n
            )
            if deleted:
                logger.debug(
                    f"[AgentBridge] Pruned {deleted} old scheduler messages "
                    f"for session={session_id} (keep_last_n={keep_last_n})"
                )
        except Exception as e:
            logger.warning(
                f"[AgentBridge] Failed to prune scheduled messages "
                f"for session={session_id}: {_exception_log_summary(e)}"
            )

        agent = self.agents.get(session_id)
        if agent:
            try:
                with agent.messages_lock:
                    agent.messages.extend(messages)
                    self._prune_scheduled_in_memory(agent, keep_last_n)
            except Exception as e:
                logger.warning(
                    f"[AgentBridge] Failed to update in-memory scheduled output "
                    f"for session={session_id}: {_exception_log_summary(e)}"
                )

    @staticmethod
    def _trim_in_memory_to_turns(agent, keep_turns: int) -> None:
        """Bound ``agent.messages`` to the most recent ``keep_turns`` real
        user/assistant turns, dropping older history together with any
        intermediate tool_use/tool_result blocks that belonged to it.

        A "real" user message is any user message whose content is not solely a
        tool_result block — matches the heuristic used elsewhere when filtering
        history (see ``AgentInitializer._filter_text_only_messages``).

        No-op when the session is already within budget. Caller does not need
        to hold the lock; this method acquires it itself.
        """
        if keep_turns <= 0:
            return

        def _is_real_user(msg) -> bool:
            if not isinstance(msg, dict) or msg.get("role") != "user":
                return False
            content = msg.get("content")
            if isinstance(content, list):
                if any(
                    isinstance(b, dict) and b.get("type") == "tool_result"
                    for b in content
                ):
                    return False
                return any(
                    isinstance(b, dict) and b.get("type") == "text" and b.get("text")
                    for b in content
                )
            if isinstance(content, str):
                return bool(content.strip())
            return False

        with agent.messages_lock:
            msgs = agent.messages
            real_user_indices = [i for i, m in enumerate(msgs) if _is_real_user(m)]
            if len(real_user_indices) <= keep_turns:
                return

            # Cut at the (k-th from the end) real user message; keep everything
            # from there onwards so the surviving slice is still a valid
            # user/assistant sequence.
            cut_idx = real_user_indices[-keep_turns]
            if cut_idx == 0:
                return

            kept = msgs[cut_idx:]
            msgs.clear()
            msgs.extend(kept)
            logger.debug(
                f"[AgentBridge] Trimmed in-memory messages to last "
                f"{keep_turns} turns ({len(kept)} messages remain)"
            )

    @classmethod
    def _prune_scheduled_in_memory(cls, agent, keep_last_n: int) -> None:
        """Mirror conversation_store.prune_scheduled_messages on agent.messages.

        Caller must hold ``agent.messages_lock``.
        """
        if keep_last_n < 0:
            keep_last_n = 0

        markers = (cls._SCHEDULED_MARKER,) + cls._SCHEDULED_LEGACY_MARKERS

        def _is_marker_user(msg) -> bool:
            if not isinstance(msg, dict) or msg.get("role") != "user":
                return False
            content = msg.get("content")
            text = ""
            if isinstance(content, str):
                text = content
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        text = block.get("text", "")
                        break
            return any(text.startswith(m) for m in markers)

        msgs = agent.messages
        pair_indices = []  # list of (user_idx, assistant_idx_or_None)
        for idx, msg in enumerate(msgs):
            if not _is_marker_user(msg):
                continue
            assistant_idx = None
            if idx + 1 < len(msgs):
                nxt = msgs[idx + 1]
                if isinstance(nxt, dict) and nxt.get("role") == "assistant":
                    assistant_idx = idx + 1
            pair_indices.append((idx, assistant_idx))

        if len(pair_indices) <= keep_last_n:
            return

        to_drop = pair_indices[: len(pair_indices) - keep_last_n]
        drop_set = set()
        for u_idx, a_idx in to_drop:
            drop_set.add(u_idx)
            if a_idx is not None:
                drop_set.add(a_idx)

        # Rebuild the list in place to keep external references stable.
        kept = [m for i, m in enumerate(msgs) if i not in drop_set]
        msgs.clear()
        msgs.extend(kept)

    @staticmethod
    def _strip_thinking_blocks(messages: list) -> list:
        """Return a shallow copy of messages with assistant "thinking" blocks removed."""
        cleaned = []
        for msg in messages:
            if not isinstance(msg, dict):
                cleaned.append(msg)
                continue
            if msg.get("role") != "assistant":
                cleaned.append(msg)
                continue
            content = msg.get("content")
            if not isinstance(content, list):
                cleaned.append(msg)
                continue
            filtered_blocks = [
                b for b in content
                if not (isinstance(b, dict) and b.get("type") == "thinking")
            ]
            if len(filtered_blocks) == len(content):
                cleaned.append(msg)
            else:
                new_msg = dict(msg)
                new_msg["content"] = filtered_blocks
                cleaned.append(new_msg)
        return cleaned

    def clear_session(self, session_id: str):
        """
        Clear a specific session's agent and conversation history
        
        Args:
            session_id: Session identifier to clear
        """
        if session_id in self.agents:
            logger.info(f"[AgentBridge] Clearing session: {session_id}")
            del self.agents[session_id]
        _clear_responses_state_for_session(session_id)
    
    def clear_all_sessions(self):
        """Clear all agent sessions"""
        logger.info(f"[AgentBridge] Clearing all sessions ({len(self.agents)} total)")
        self.agents.clear()
        self.default_agent = None
    
    def refresh_all_skills(self) -> int:
        """
        Refresh skills and conditional tools in all agent instances after
        environment variable changes. This allows hot-reload without restarting.

        Returns:
            Number of agent instances refreshed
        """
        import os
        from dotenv import load_dotenv
        from config import conf

        # Reload environment variables from .env file
        workspace_root = expand_path(conf().get("agent_workspace", "~/cow"))
        env_file = os.path.join(workspace_root, '.env')

        if os.path.exists(env_file):
            load_dotenv(env_file, override=True)
            logger.info(f"[AgentBridge] Reloaded environment variables from {env_file}")

        refreshed_count = 0

        # Collect all agent instances to refresh
        agents_to_refresh = []
        if self.default_agent:
            agents_to_refresh.append(("default", self.default_agent))
        for session_id, agent in self.agents.items():
            agents_to_refresh.append((session_id, agent))

        for label, agent in agents_to_refresh:
            # Refresh skills
            if hasattr(agent, 'skill_manager') and agent.skill_manager:
                agent.skill_manager.refresh_skills()

            # Refresh conditional tools (e.g. web_search depends on API keys)
            self._refresh_conditional_tools(agent)

            refreshed_count += 1

        if refreshed_count > 0:
            logger.info(f"[AgentBridge] Refreshed skills & tools in {refreshed_count} agent instance(s)")

        return refreshed_count

    @staticmethod
    def _refresh_conditional_tools(agent):
        """
        Add or remove conditional tools based on current environment variables.
        For example, web_search should only be present when BOCHA_API_KEY or
        LINKAI_API_KEY is set.
        """
        try:
            from agent.tools.web_search.web_search import WebSearch

            has_tool = any(t.name == "web_search" for t in agent.tools)
            available = WebSearch.is_available()

            if available and not has_tool:
                # API key was added - inject the tool
                tool = WebSearch()
                tool.model = agent.model
                agent.tools.append(tool)
                logger.info("[AgentBridge] web_search tool added (API key now available)")
            elif not available and has_tool:
                # API key was removed - remove the tool
                agent.tools = [t for t in agent.tools if t.name != "web_search"]
                logger.info("[AgentBridge] web_search tool removed (API key no longer available)")
        except Exception as e:
            logger.debug(f"[AgentBridge] Failed to refresh conditional tools: {_exception_log_summary(e)}")
