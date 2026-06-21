# encoding:utf-8

"""
OpenAI-Compatible Bot Base Class

Provides a common implementation for bots that are compatible with OpenAI's API format.
This includes: OpenAI, LinkAI, Azure OpenAI, and many third-party providers.
"""

import json
import requests
from typing import Optional
from common.log import logger
from agent.protocol.message_utils import drop_orphaned_tool_results_openai
from models.model_capabilities import (
    get_model_capabilities,
    normalize_reasoning_effort,
    sanitize_chat_payload,
)
from models.model_telemetry import (
    ModelCallSpan,
    chunk_has_model_output,
    extract_error_details,
    instrument_model_stream,
    is_model_error_response,
)
from models.model_retry import (
    annotate_retry_evidence,
    build_retry_decision,
    coerce_max_retries,
    sleep_for_retry,
)
from models.openai.openai_http_client import OpenAIHTTPClient, OpenAIHTTPError


class OpenAICompatibleBot:
    """
    Base class for OpenAI-compatible bots.
    
    Provides common tool calling implementation that can be inherited by:
    - ChatGPTBot
    - LinkAIBot  
    - OpenAIBot
    - AzureChatGPTBot
    - Other OpenAI-compatible providers
    
    Subclasses only need to override get_api_config() to provide their specific API settings.
    """
    
    def get_api_config(self):
        """
        Get API configuration for this bot.
        
        Subclasses should override this to provide their specific config.
        
        Returns:
            dict: {
                'api_key': str,
                'api_base': str (optional),
                'model': str,
                'default_temperature': float,
                'default_top_p': float,
                'default_frequency_penalty': float,
                'default_presence_penalty': float,
            }
        """
        raise NotImplementedError("Subclasses must implement get_api_config()")
    
    def call_with_tools(self, messages, tools=None, stream=False, **kwargs):
        """
        Call OpenAI-compatible API with tool support for agent integration
        
        This method handles:
        1. Format conversion (Claude format → OpenAI format)
        2. System prompt injection
        3. API calling with proper configuration
        4. Error handling
        
        Args:
            messages: List of messages (may be in Claude format from agent)
            tools: List of tool definitions (may be in Claude format from agent)
            stream: Whether to use streaming
            **kwargs: Additional parameters (max_tokens, temperature, system, etc.)
            
        Returns:
            Formatted response in OpenAI format or generator for streaming
        """
        try:
            # Get API configuration from subclass
            api_config = self.get_api_config()
            
            # Convert messages from Claude format to OpenAI format
            messages = self._convert_messages_to_openai_format(messages)
            
            # Convert tools from Claude format to OpenAI format
            if tools:
                tools = self._convert_tools_to_openai_format(tools)
            
            # Handle system prompt (OpenAI uses system message, Claude uses separate parameter)
            system_prompt = kwargs.get('system')
            if system_prompt:
                # Add system message at the beginning if not already present
                if not messages or messages[0].get('role') != 'system':
                    messages = [{"role": "system", "content": system_prompt}] + messages
                else:
                    # Replace existing system message
                    messages[0] = {"role": "system", "content": system_prompt}
            
            # Build request parameters
            model_name = kwargs.get("model", api_config.get('model', 'gpt-5.4'))
            api_key = api_config.get('api_key')
            api_base = api_config.get('api_base')
            provider_id = self._capability_provider_id(api_config, api_base)
            capabilities = get_model_capabilities(model_name, provider=provider_id)
            if not capabilities.supports_system_messages:
                messages = self._coerce_system_messages_to_user(messages)

            request_params = {
                "model": model_name,
                "messages": messages,
                "temperature": kwargs.get("temperature", api_config.get('default_temperature', 0.9)),
                "top_p": kwargs.get("top_p", api_config.get('default_top_p', 1.0)),
                "frequency_penalty": kwargs.get("frequency_penalty", api_config.get('default_frequency_penalty', 0.0)),
                "presence_penalty": kwargs.get("presence_penalty", api_config.get('default_presence_penalty', 0.0)),
                "stream": stream
            }
            # Add max_tokens if specified
            if kwargs.get("max_tokens"):
                request_params["max_tokens"] = kwargs["max_tokens"]
            reasoning_effort = normalize_reasoning_effort(
                kwargs.get("reasoning_effort"),
                capabilities,
            )
            if reasoning_effort:
                request_params["reasoning_effort"] = reasoning_effort
            verbosity = kwargs.get("verbosity")
            if verbosity not in (None, ""):
                request_params["verbosity"] = verbosity
            
            # Add tools if provided
            if tools:
                request_params["tools"] = tools
                request_params["tool_choice"] = kwargs.get("tool_choice", "auto")
            
            request_params, removed_params = sanitize_chat_payload(request_params, capabilities)
            if removed_params:
                logger.debug(
                    f"[{self.__class__.__name__}] stripped unsupported model params "
                    f"for {capabilities.provider}/{model_name}: {removed_params}"
                )

            try:
                retry_count = int(kwargs.get("retry_count") or 0)
            except (TypeError, ValueError):
                retry_count = 0
            retry_count = max(0, retry_count)
            max_model_retries = coerce_max_retries(
                kwargs.get("model_max_retries", kwargs.get("max_model_retries", None))
            )
            retry_sleep = kwargs.get("model_retry_sleep")
            responses_plan = self.plan_responses_api_call(
                messages,
                tools=tools,
                stream=stream,
                **{
                    **kwargs,
                    "model": model_name,
                    "temperature": request_params.get("temperature"),
                    "top_p": request_params.get("top_p"),
                    "max_tokens": (
                        request_params.get("max_tokens")
                        or request_params.get("max_completion_tokens")
                    ),
                    **self._responses_control_overrides(kwargs, capabilities),
                },
            )

            if responses_plan is not None and not stream:
                return self._responses_sync_response_with_retry(
                    responses_plan,
                    api_key,
                    api_base,
                    capabilities.provider,
                    model_name,
                    retry_count,
                    max_model_retries,
                    retry_sleep,
                    session_id=str(kwargs.get("session_id") or ""),
                    workspace=kwargs.get("workspace") or kwargs.get("workspace_dir"),
                )

            if stream:
                if responses_plan is not None:
                    on_completed = self._responses_stream_state_callback(
                        responses_plan,
                        provider=capabilities.provider,
                        model_name=model_name,
                        session_id=str(kwargs.get("session_id") or ""),
                        workspace=kwargs.get("workspace") or kwargs.get("workspace_dir"),
                    )
                    response_stream = self._handle_responses_stream_response(
                        responses_plan,
                        api_key,
                        api_base,
                        on_completed=on_completed,
                    )
                    return self._stream_response_with_retry(
                        response_stream,
                        request_params,
                        api_key,
                        api_base,
                        capabilities.provider,
                        model_name,
                        retry_count,
                        max_model_retries,
                        retry_sleep,
                        api_path=responses_plan.create_path,
                        stream_factory=lambda: self._handle_responses_stream_response(
                            responses_plan,
                            api_key,
                            api_base,
                            on_completed=on_completed,
                        ),
                    )

                response_stream = self._handle_stream_response(request_params, api_key, api_base)
                if isinstance(response_stream, dict):
                    return self._record_single_response_attempt(
                        response_stream,
                        capabilities.provider,
                        model_name,
                        stream=True,
                        retry_count=retry_count,
                    )
                return self._stream_response_with_retry(
                    response_stream,
                    request_params,
                    api_key,
                    api_base,
                    capabilities.provider,
                    model_name,
                    retry_count,
                    max_model_retries,
                    retry_sleep,
                )
            else:
                return self._sync_response_with_retry(
                    request_params,
                    api_key,
                    api_base,
                    capabilities.provider,
                    model_name,
                    retry_count,
                    max_model_retries,
                    retry_sleep,
                )
                
        except Exception as e:
            error_msg = str(e)
            logger.error(f"[{self.__class__.__name__}] call_with_tools error: {error_msg}")
            if stream:
                def error_generator():
                    yield {
                        "error": True,
                        "message": error_msg,
                        "status_code": 500
                    }
                return error_generator()
            else:
                return {
                    "error": True,
                    "message": error_msg,
                    "status_code": 500
                }
    
    def _get_http_client(self) -> OpenAIHTTPClient:
        """Build an HTTP client honoring the global proxy config.

        Subclasses can override this for custom auth headers (e.g. Azure's
        ``api-key`` header) by returning a pre-configured client.
        """
        from config import conf
        proxy = conf().get("proxy") or None
        return OpenAIHTTPClient(proxy=proxy)

    @staticmethod
    def _capability_provider_id(api_config, api_base):
        from common import const
        from models.openai.responses_adapter import DEFAULT_OPENAI_API_BASE, is_official_openai_provider

        provider_id = (api_config or {}).get("provider") or ""
        if not provider_id:
            base = api_base or DEFAULT_OPENAI_API_BASE
            if is_official_openai_provider(const.OPENAI, base):
                return const.OPENAI
            return "openai_compatible"
        official_ids = {const.OPENAI, const.OPEN_AI, const.CHATGPT, "openai"}
        if provider_id == const.CHATGPTONAZURE:
            return const.CHATGPTONAZURE
        if provider_id in official_ids:
            base = api_base or DEFAULT_OPENAI_API_BASE
            if not is_official_openai_provider(provider_id, base):
                return "openai_compatible"
            return const.OPENAI
        return provider_id

    @staticmethod
    def _coerce_system_messages_to_user(messages):
        coerced = []
        for message in messages or []:
            if isinstance(message, dict) and message.get("role") == "system":
                patched = dict(message)
                patched["role"] = "user"
                coerced.append(patched)
            else:
                coerced.append(message)
        return coerced

    @staticmethod
    def _responses_control_overrides(kwargs, capabilities):
        overrides = {}
        if "reasoning" not in (kwargs or {}):
            effort = normalize_reasoning_effort(
                (kwargs or {}).get("reasoning_effort"),
                capabilities,
            )
            if effort:
                overrides["reasoning"] = {"effort": effort}
        if "text" not in (kwargs or {}):
            verbosity = (kwargs or {}).get("verbosity")
            if capabilities.supports_verbosity and verbosity not in (None, ""):
                overrides["text"] = {"verbosity": verbosity}
        return overrides

    def plan_responses_api_call(self, messages, tools=None, stream=False, **kwargs):
        """Build a Responses API request plan when explicitly enabled.

        The adapter is still gated to official OpenAI hosts. Streaming traffic
        stays on /chat/completions until Responses stream events are normalized
        into the existing agent stream contract.
        """
        from models.openai.responses_adapter import (
            build_responses_plan,
            decide_responses_adapter,
        )

        api_config = self.get_api_config()
        decision = decide_responses_adapter(api_config, kwargs)
        if not decision.enabled:
            return None

        converted_messages = self._convert_messages_to_openai_format(messages)
        converted_tools = self._convert_tools_to_openai_format(tools) if tools else None
        model_name = kwargs.get("model", api_config.get("model", "gpt-5.4"))
        plan_kwargs = dict(kwargs)
        plan_kwargs.pop("model", None)
        plan_kwargs.pop("use_responses_api", None)
        raw_state = plan_kwargs.pop("responses_state", None)
        session_id = str(plan_kwargs.get("session_id") or "")
        workspace = plan_kwargs.get("workspace") or plan_kwargs.get("workspace_dir")
        input_scope = str(plan_kwargs.pop("responses_input_scope", "") or "").strip().lower()
        if raw_state is None and session_id:
            from models.openai.responses_state_store import load_responses_state

            loaded_state = load_responses_state(
                session_id=session_id,
                provider=decision.provider or api_config.get("provider") or "openai",
                model=model_name,
                workspace=workspace,
            )
            if input_scope == "fresh":
                raw_state = loaded_state
            else:
                raw_state = {
                    "prompt_cache_key": loaded_state.prompt_cache_key,
                    "prompt_cache_retention": loaded_state.prompt_cache_retention,
                    "service_tier": loaded_state.service_tier,
                    "truncation": loaded_state.truncation,
                    "store": loaded_state.store,
                }
        state = self._coerce_responses_state(api_config, raw_state, plan_kwargs)
        return build_responses_plan(
            model=model_name,
            messages=converted_messages,
            tools=converted_tools,
            stream=stream,
            state=state,
            **plan_kwargs,
        )

    @staticmethod
    def _coerce_responses_state(api_config, raw_state, kwargs):
        from models.openai.responses_adapter import ResponsesState

        allowed_keys = {
            "previous_response_id",
            "prompt_cache_key",
            "prompt_cache_retention",
            "service_tier",
            "truncation",
            "store",
            "compacted_input",
        }
        values = {}
        if isinstance(raw_state, ResponsesState):
            values.update({key: value for key, value in raw_state.to_dict().items() if key in allowed_keys})
        elif isinstance(raw_state, dict):
            values.update({key: raw_state[key] for key in allowed_keys if key in raw_state})
        for key in allowed_keys:
            if key in kwargs:
                values[key] = kwargs[key]
        if not values.get("service_tier") and (api_config or {}).get("responses_service_tier"):
            values["service_tier"] = (api_config or {}).get("responses_service_tier")
        if not values.get("prompt_cache_retention") and (api_config or {}).get("responses_prompt_cache_retention"):
            values["prompt_cache_retention"] = (api_config or {}).get("responses_prompt_cache_retention")
        return ResponsesState(**values)

    def _new_model_call_span(self, provider, model_name, *, stream, retry_count, api_path="/chat/completions"):
        return ModelCallSpan(
            provider=provider,
            model=model_name,
            stream=bool(stream),
            retry_count=max(0, int(retry_count or 0)),
            api_path=api_path,
        )

    def _record_single_response_attempt(self, response, provider, model_name, *, stream, retry_count):
        span = self._new_model_call_span(
            provider,
            model_name,
            stream=stream,
            retry_count=retry_count,
        )
        span.observe_response(response)
        if is_model_error_response(response):
            span.finish_error(**extract_error_details(response))
        else:
            span.finish_completed()
        return response

    def _responses_sync_response_with_retry(
        self,
        plan,
        api_key,
        api_base,
        provider,
        model_name,
        base_retry_count,
        max_model_retries,
        retry_sleep,
        *,
        session_id="",
        workspace=None,
    ):
        from models.openai.responses_adapter import (
            extract_responses_state,
            normalize_responses_output_to_chat,
        )
        from models.openai.responses_state_store import save_responses_state

        last_response = {}
        for attempt in range(max_model_retries + 1):
            retry_count = base_retry_count + attempt
            span = self._new_model_call_span(
                provider,
                model_name,
                stream=False,
                retry_count=retry_count,
                api_path=plan.create_path,
            )
            raw_response = self._handle_responses_sync_response(
                plan.create_payload,
                api_key,
                api_base,
            )
            if is_model_error_response(raw_response):
                response = raw_response
            elif self._responses_status_error(raw_response):
                response = self._responses_status_error(raw_response)
            else:
                response = normalize_responses_output_to_chat(raw_response)
            span.observe_response(response)
            if not is_model_error_response(response):
                span.finish_completed()
                if session_id:
                    next_state = extract_responses_state(raw_response, plan.state)
                    save_responses_state(
                        session_id=session_id,
                        provider=provider,
                        model=model_name,
                        state=next_state,
                        workspace=workspace,
                    )
                return response

            details = extract_error_details(response)
            decision = build_retry_decision(
                details,
                attempt=attempt,
                max_retries=max_model_retries,
            )
            span.finish_error(**details)
            annotated = annotate_retry_evidence(response, decision)
            last_response = annotated
            if decision.should_retry:
                logger.warning(
                    f"[{self.__class__.__name__}] retrying Responses model call "
                    f"after {decision.delay_seconds:.3f}s "
                    f"(attempt {attempt + 1}/{max_model_retries}, "
                    f"taxonomy={decision.taxonomy})"
                )
                sleep_for_retry(decision.delay_seconds, retry_sleep)
                continue
            return annotated
        return last_response

    def _responses_stream_state_callback(self, plan, *, provider, model_name, session_id="", workspace=None):
        if not session_id:
            return None

        def persist_completed_response(raw_response):
            from models.openai.responses_adapter import extract_responses_state
            from models.openai.responses_state_store import save_responses_state

            next_state = extract_responses_state(raw_response, plan.state)
            save_responses_state(
                session_id=session_id,
                provider=provider,
                model=model_name,
                state=next_state,
                workspace=workspace,
            )

        return persist_completed_response

    @staticmethod
    def _responses_status_error(response):
        if not isinstance(response, dict):
            return None
        status = str(response.get("status") or "").strip().lower()
        if status not in {"failed", "cancelled", "incomplete"}:
            return None
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

    def _sync_response_with_retry(
        self,
        request_params,
        api_key,
        api_base,
        provider,
        model_name,
        base_retry_count,
        max_model_retries,
        retry_sleep,
    ):
        for attempt in range(max_model_retries + 1):
            retry_count = base_retry_count + attempt
            span = self._new_model_call_span(
                provider,
                model_name,
                stream=False,
                retry_count=retry_count,
            )
            response = self._handle_sync_response(request_params, api_key, api_base)
            span.observe_response(response)
            if not is_model_error_response(response):
                span.finish_completed()
                return response

            details = extract_error_details(response)
            decision = build_retry_decision(
                details,
                attempt=attempt,
                max_retries=max_model_retries,
            )
            span.finish_error(**details)
            annotated = annotate_retry_evidence(response, decision)
            if decision.should_retry:
                logger.warning(
                    f"[{self.__class__.__name__}] retrying sync model call "
                    f"after {decision.delay_seconds:.3f}s "
                    f"(attempt {attempt + 1}/{max_model_retries}, "
                    f"taxonomy={decision.taxonomy})"
                )
                sleep_for_retry(decision.delay_seconds, retry_sleep)
                continue
            return annotated
        return response

    def _stream_response_with_retry(
        self,
        first_stream,
        request_params,
        api_key,
        api_base,
        provider,
        model_name,
        base_retry_count,
        max_model_retries,
        retry_sleep,
        *,
        api_path="/chat/completions",
        stream_factory=None,
    ):
        def retrying_stream():
            attempt = 0
            while attempt <= max_model_retries:
                raw_stream = (
                    first_stream
                    if attempt == 0
                    else (
                        stream_factory()
                        if stream_factory is not None
                        else self._handle_stream_response(request_params, api_key, api_base)
                    )
                )
                span = self._new_model_call_span(
                    provider,
                    model_name,
                    stream=True,
                    retry_count=base_retry_count + attempt,
                    api_path=api_path,
                )
                stream = instrument_model_stream(raw_stream, span)
                buffered_chunks = []
                output_started = False
                retry_next = False
                retry_decision = None
                try:
                    for chunk in stream:
                        if isinstance(chunk, dict) and is_model_error_response(chunk):
                            details = extract_error_details(chunk)
                            retry_decision = build_retry_decision(
                                details,
                                attempt=attempt,
                                max_retries=max_model_retries,
                            )
                            annotated = annotate_retry_evidence(chunk, retry_decision)
                            if not output_started and retry_decision.should_retry:
                                retry_next = True
                                self._close_stream(stream)
                                break
                            if output_started and retry_decision.should_retry:
                                annotated = self._mark_stream_retry_suppressed(annotated)
                            for buffered in buffered_chunks:
                                yield buffered
                            yield annotated
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
                except GeneratorExit:
                    self._close_stream(stream)
                    raise
                except Exception as e:
                    if output_started:
                        raise
                    details = {"message": str(e), "status_code": 500}
                    retry_decision = build_retry_decision(
                        details,
                        attempt=attempt,
                        max_retries=max_model_retries,
                    )
                    if retry_decision.should_retry:
                        retry_next = True
                    else:
                        raise

                if retry_next and retry_decision is not None:
                    logger.warning(
                        f"[{self.__class__.__name__}] retrying stream model call "
                        f"after {retry_decision.delay_seconds:.3f}s "
                        f"(attempt {attempt + 1}/{max_model_retries}, "
                        f"taxonomy={retry_decision.taxonomy})"
                    )
                    sleep_for_retry(retry_decision.delay_seconds, retry_sleep)
                    attempt += 1
                    continue
                return

        return retrying_stream()

    @staticmethod
    def _mark_stream_retry_suppressed(response):
        annotated = dict(response or {})
        annotated["retry_suppressed"] = True
        annotated["retry_suppressed_reason"] = "stream_output_started"
        error_value = annotated.get("error")
        if isinstance(error_value, dict):
            nested = dict(error_value)
            nested["retry_suppressed"] = True
            nested["retry_suppressed_reason"] = "stream_output_started"
            annotated["error"] = nested
        return annotated

    @staticmethod
    def _close_stream(stream):
        close = getattr(stream, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass

    def _handle_sync_response(self, request_params, api_key, api_base):
        """Handle synchronous chat-completion via HTTP."""
        params = dict(request_params)
        params.pop("stream", None)
        # Translate legacy SDK timeout kwarg to our HTTP client kwarg.
        timeout = params.pop("request_timeout", None) or params.pop("timeout", None)
        try:
            client = self._get_http_client()
            return client.chat_completions(
                api_key=api_key,
                api_base=api_base,
                timeout=timeout,
                stream=False,
                **params,
            )
        except OpenAIHTTPError as e:
            error_code = ""
            error_type = ""
            if isinstance(e.body, dict):
                err = e.body.get("error") or {}
                if isinstance(err, dict):
                    error_code = err.get("code") or ""
                    error_type = err.get("type") or ""
            logger.error(
                f"[{self.__class__.__name__}] sync response error: "
                f"HTTP {e.status_code}: {e.message}"
            )
            return {
                "error": {
                    "message": e.message,
                    "code": error_code,
                    "type": error_type,
                },
                "message": e.message,
                "status_code": e.status_code or 500,
                "retry_after": e.retry_after,
            }
        except Exception as e:
            logger.error(f"[{self.__class__.__name__}] sync response error: {e}")
            return {
                "error": True,
                "message": str(e),
                "status_code": 500,
            }

    def _handle_responses_sync_response(self, request_params, api_key, api_base):
        """Handle a non-streaming official OpenAI Responses API request."""
        params = dict(request_params)
        params.pop("stream", None)
        timeout = params.pop("request_timeout", None) or params.pop("timeout", None)
        try:
            client = self._get_http_client()
            return client.responses_create(
                api_key=api_key,
                api_base=api_base,
                timeout=timeout,
                stream=False,
                **params,
            )
        except OpenAIHTTPError as e:
            error_code = ""
            error_type = ""
            if isinstance(e.body, dict):
                err = e.body.get("error") or {}
                if isinstance(err, dict):
                    error_code = err.get("code") or ""
                    error_type = err.get("type") or ""
            logger.error(
                f"[{self.__class__.__name__}] Responses sync error: "
                f"HTTP {e.status_code}: {e.message}"
            )
            return {
                "error": {
                    "message": e.message,
                    "code": error_code,
                    "type": error_type,
                },
                "message": e.message,
                "status_code": e.status_code or 500,
                "retry_after": e.retry_after,
            }
        except Exception as e:
            logger.error(f"[{self.__class__.__name__}] Responses sync error: {e}")
            return {
                "error": True,
                "message": str(e),
                "status_code": 500,
            }

    def _handle_responses_stream_response(self, plan, api_key, api_base, *, on_completed=None):
        """Handle official OpenAI Responses streaming and normalize events."""
        from models.openai.responses_adapter import normalize_responses_stream_events_to_chat

        params = dict(plan.create_payload)
        params.pop("stream", None)
        timeout = params.pop("request_timeout", None) or params.pop("timeout", None)
        try:
            client = self._get_http_client()
            stream = client.responses_create(
                api_key=api_key,
                api_base=api_base,
                timeout=timeout,
                stream=True,
                **params,
            )
            yield from normalize_responses_stream_events_to_chat(
                stream,
                on_completed=on_completed,
            )
        except OpenAIHTTPError as e:
            logger.error(
                f"[{self.__class__.__name__}] Responses stream error: "
                f"HTTP {e.status_code}: {e.message}"
            )
            yield {
                "error": {
                    "message": e.message,
                    "code": "",
                    "type": "",
                },
                "message": e.message,
                "status_code": e.status_code or 500,
                "retry_after": e.retry_after,
            }
        except Exception as e:
            logger.error(f"[{self.__class__.__name__}] Responses stream error: {e}")
            yield {
                "error": True,
                "message": str(e),
                "status_code": 500,
            }

    def _handle_stream_response(self, request_params, api_key, api_base):
        """Handle streaming chat-completion via HTTP (SSE).

        Yields dict chunks in OpenAI's standard streaming shape:
          {"choices": [{"delta": {...}, "finish_reason": ...}], ...}
        On error, yields a single ``{"error": ..., "status_code": ...}`` chunk
        — the same contract :mod:`agent.protocol.agent_stream` already handles.
        """
        params = dict(request_params)
        params.pop("stream", None)
        timeout = params.pop("request_timeout", None) or params.pop("timeout", None)
        try:
            client = self._get_http_client()
            stream = client.chat_completions(
                api_key=api_key,
                api_base=api_base,
                timeout=timeout,
                stream=True,
                **params,
            )
            for chunk in stream:
                yield chunk
        except OpenAIHTTPError as e:
            logger.error(
                f"[{self.__class__.__name__}] stream response error: "
                f"HTTP {e.status_code}: {e.message}"
            )
            yield {
                "error": True,
                "message": e.message,
                "status_code": e.status_code or 500,
            }
        except Exception as e:
            logger.error(f"[{self.__class__.__name__}] stream response error: {e}")
            yield {
                "error": True,
                "message": str(e),
                "status_code": 500,
            }
    
    def _convert_tools_to_openai_format(self, tools):
        """
        Convert tools from Claude format to OpenAI format
        
        Claude format: {name, description, input_schema}
        OpenAI format: {type: "function", function: {name, description, parameters}}
        """
        if not tools:
            return None
        
        openai_tools = []
        for tool in tools:
            # Check if already in OpenAI format
            if 'type' in tool and tool['type'] == 'function':
                openai_tools.append(tool)
            else:
                # Convert from Claude format
                openai_tools.append({
                    "type": "function",
                    "function": {
                        "name": tool.get("name"),
                        "description": tool.get("description"),
                        "parameters": tool.get("input_schema", {})
                    }
                })
        
        return openai_tools
    
    def _convert_messages_to_openai_format(self, messages):
        """
        Convert messages from Claude format to OpenAI format

        Claude content blocks (tool_use / tool_result / thinking) → OpenAI
        tool_calls / tool role / reasoning_content. Some thinking-mode
        providers require reasoning_content on assistant messages after a
        tool_call appears in history; back-fill with empty string when the
        trace was not captured.
        """
        if not messages:
            return []

        # Detect any prior tool-call turn — gates reasoning_content back-fill below.
        has_tool_call_history = False
        for msg in messages:
            if msg.get("role") != "assistant":
                continue
            if msg.get("tool_calls"):
                has_tool_call_history = True
                break
            inner = msg.get("content")
            if isinstance(inner, list) and any(
                isinstance(b, dict) and b.get("type") == "tool_use" for b in inner
            ):
                has_tool_call_history = True
                break

        openai_messages = []

        for msg in messages:
            role = msg.get("role")
            content = msg.get("content")

            # Handle string content (already in correct format)
            if isinstance(content, str):
                if (role == "assistant" and has_tool_call_history
                        and isinstance(msg, dict)
                        and "reasoning_content" not in msg):
                    patched = dict(msg)
                    patched["reasoning_content"] = ""
                    openai_messages.append(patched)
                else:
                    openai_messages.append(msg)
                continue

            # Handle list content (Claude format with content blocks)
            if isinstance(content, list):
                # Check if this is a tool result message (user role with tool_result blocks)
                if role == "user" and any(block.get("type") == "tool_result" for block in content):
                    # Separate text content and tool_result blocks
                    text_parts = []
                    tool_results = []

                    for block in content:
                        if block.get("type") == "text":
                            text_parts.append(block.get("text", ""))
                        elif block.get("type") == "tool_result":
                            tool_results.append(block)

                    # First, add tool result messages (must come immediately after assistant with tool_calls)
                    for block in tool_results:
                        tool_call_id = block.get("tool_use_id") or ""
                        if not tool_call_id:
                            logger.warning(f"[OpenAICompatible] tool_result missing tool_use_id, using empty string")
                        # Ensure content is a string (some providers require string content)
                        result_content = block.get("content", "")
                        if not isinstance(result_content, str):
                            result_content = json.dumps(result_content, ensure_ascii=False)
                        openai_messages.append({
                            "role": "tool",
                            "tool_call_id": tool_call_id,
                            "content": result_content
                        })

                    # Then, add text content as a separate user message if present
                    if text_parts:
                        openai_messages.append({
                            "role": "user",
                            "content": " ".join(text_parts)
                        })

                # Check if this is an assistant message with tool_use blocks
                elif role == "assistant":
                    text_parts = []
                    tool_calls = []
                    reasoning_parts = []

                    for block in content:
                        if not isinstance(block, dict):
                            continue
                        btype = block.get("type")
                        if btype == "text":
                            text_parts.append(block.get("text", ""))
                        elif btype == "tool_use":
                            tool_id = block.get("id") or ""
                            if not tool_id:
                                logger.warning(f"[OpenAICompatible] tool_use missing id for '{block.get('name')}'")
                            tool_calls.append({
                                "id": tool_id,
                                "type": "function",
                                "function": {
                                    "name": block.get("name"),
                                    "arguments": json.dumps(block.get("input", {}))
                                }
                            })
                        elif btype == "thinking":
                            reasoning_parts.append(block.get("thinking", ""))

                    # Build OpenAI format assistant message
                    openai_msg = {
                        "role": "assistant",
                        "content": " ".join(text_parts) if text_parts else None
                    }

                    if tool_calls:
                        openai_msg["tool_calls"] = tool_calls

                    # Round-trip reasoning_content; empty string when missing
                    # after a tool-call turn keeps strict providers happy.
                    if reasoning_parts:
                        openai_msg["reasoning_content"] = "\n".join(reasoning_parts)
                    elif has_tool_call_history:
                        openai_msg["reasoning_content"] = ""

                    if msg.get("_gemini_raw_parts"):
                        openai_msg["_gemini_raw_parts"] = msg["_gemini_raw_parts"]

                    openai_messages.append(openai_msg)
                else:
                    # Other list content, keep as is
                    openai_messages.append(msg)
            else:
                # Other formats, keep as is
                openai_messages.append(msg)

        return drop_orphaned_tool_results_openai(openai_messages)

    def call_vision(self, image_url: str, question: str,
                    model: Optional[str] = None,
                    max_tokens: int = 1000) -> dict:
        """Analyze an image using the OpenAI-compatible /chat/completions endpoint."""
        try:
            api_config = self.get_api_config()
            vision_model = model or api_config.get("model", "gpt-4o")
            api_key = api_config.get("api_key", "")
            api_base = (api_config.get("api_base") or "https://api.openai.com/v1").rstrip("/")

            payload = {
                "model": vision_model,
                "messages": [{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": question},
                        {"type": "image_url", "image_url": {"url": image_url}},
                    ],
                }],
            }
            headers = {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            }
            resp = requests.post(
                f"{api_base}/chat/completions",
                headers=headers, json=payload, timeout=180,
            )
            if resp.status_code != 200:
                body = resp.text[:500]
                logger.error(f"[{self.__class__.__name__}] call_vision HTTP {resp.status_code}: {body}")
                return {"error": True, "message": f"HTTP {resp.status_code}: {body}"}
            data = resp.json()
            content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
            usage = data.get("usage", {})
            return {
                "model": vision_model,
                "content": content,
                "usage": {
                    "prompt_tokens": usage.get("prompt_tokens", 0),
                    "completion_tokens": usage.get("completion_tokens", 0),
                    "total_tokens": usage.get("total_tokens", 0),
                },
            }
        except Exception as e:
            logger.error(f"[{self.__class__.__name__}] call_vision error: {e}")
            return {"error": True, "message": str(e)}
