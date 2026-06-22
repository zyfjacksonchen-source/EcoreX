# encoding:utf-8

import json

from models.openai.openai_compat import (
    error as openai_error,
    OpenAIError,
    RateLimitError,
    Timeout,
    APIError,
    APIConnectionError,
    wrap_http_error,
)
from models.openai.openai_http_client import OpenAIHTTPClient, OpenAIHTTPError
import requests
from common import const
from common.i18n import t as _t
from models.bot import Bot
from models.openai_compatible_bot import OpenAICompatibleBot
from models.openai.legacy_reply_retry import (
    build_retry_decision,
    legacy_adapter_error_details,
    legacy_reply_failure_result,
    legacy_reply_max_retries,
    openai_legacy_error_details,
    run_legacy_reply_retry_sleep,
)
from models.chatgpt.chat_gpt_session import ChatGPTSession
from models.openai.open_ai_image import OpenAIImage
from models.model_image_retry import (
    ModelImageCallError,
    clear_create_img_error,
    create_img_with_retry,
    image_error_details_from_exception,
    image_error_from_response,
    set_create_img_error,
)
from models.model_provider_errors import http_error_response, provider_error_response
from models.model_retry import sleep_for_retry
from models.session_manager import SessionManager
from models.model_telemetry import ModelCallSpan
from models.model_capabilities import get_model_capabilities, sanitize_chat_payload
from bridge.context import ContextType
from bridge.reply import Reply, ReplyType
from common.log import logger
from common.token_bucket import TokenBucket
from config import conf, load_config
from models.baidu.baidu_wenxin_session import BaiduWenxinSession

LEGACY_REPLY_IMAGE_API_PATH = "/legacy/reply_image"

# OpenAI对话模型API (可用)
class ChatGPTBot(Bot, OpenAIImage, OpenAICompatibleBot):
    def __init__(self):
        super().__init__()
        # Resolve api key / base from config (no global SDK state anymore).
        self._ecorex_route_bot_type = (
            conf().get("bot_type")
            or (const.CHATGPTONAZURE if conf().get("use_azure_chatgpt", False) else const.OPENAI)
        )
        self._configure_http_client_for_route()
        if conf().get("rate_limit_chatgpt"):
            self.tb4chatgpt = TokenBucket(conf().get("rate_limit_chatgpt", 20))
        conf_model = conf().get("model") or "gpt-3.5-turbo"
        self.sessions = SessionManager(ChatGPTSession, model=conf().get("model") or "gpt-3.5-turbo")
        # o1相关模型不支持system prompt，暂时用文心模型的session

        self.args = {
            "model": conf_model,  # 对话模型的名称
            "temperature": conf().get("temperature", 0.9),  # 值在[0,1]之间，越大表示回复越具有不确定性
            # "max_tokens":4096,  # 回复最大的字符数
            "top_p": conf().get("top_p", 1),
            "frequency_penalty": conf().get("frequency_penalty", 0.0),  # [-2,2]之间，该值越大则更倾向于产生不同的内容
            "presence_penalty": conf().get("presence_penalty", 0.0),  # [-2,2]之间，该值越大则更倾向于产生不同的内容
            "request_timeout": conf().get("request_timeout", None),  # 请求超时时间，openai接口默认设置为600，对于难问题一般需要较长时间
            "timeout": conf().get("request_timeout", None),  # 重试超时时间，在这个时间内，将会自动重试
        }
        api_config = self.get_api_config()
        capability_provider = self._capability_provider_id(api_config, api_config.get("api_base"))
        capabilities = get_model_capabilities(conf_model, provider=capability_provider)
        self.args, _removed_params = sanitize_chat_payload(self.args, capabilities)
        if not capabilities.supports_system_messages:
            self.sessions = SessionManager(BaiduWenxinSession, model=conf().get("model") or const.O1_MINI)

    def configure_model_route(self, bot_type: str):
        """Bind this bot to an explicit AgentBridge route provider."""
        self._ecorex_route_bot_type = bot_type or conf().get("bot_type") or const.OPENAI
        self._configure_http_client_for_route()
        return self

    def _effective_route_bot_type(self) -> str:
        return (
            getattr(self, "_ecorex_route_bot_type", None)
            or conf().get("bot_type")
            or (const.CHATGPTONAZURE if conf().get("use_azure_chatgpt", False) else const.OPENAI)
        )

    def _is_custom_route(self) -> bool:
        return self._effective_route_bot_type() == const.CUSTOM

    def _configure_http_client_for_route(self) -> None:
        if self._is_custom_route():
            self._api_key = conf().get("custom_api_key", "")
            self._api_base = conf().get("custom_api_base") or None
        else:
            self._api_key = conf().get("open_ai_api_key")
            self._api_base = conf().get("open_ai_api_base") or None
        self._proxy = conf().get("proxy") or None
        self._http_client = OpenAIHTTPClient(
            api_key=self._api_key,
            api_base=self._api_base,
            proxy=self._proxy,
        )

    def get_api_config(self):
        """Get API configuration for OpenAI-compatible base class"""
        is_custom = self._is_custom_route()
        route = self._effective_route_bot_type()
        provider = "custom" if is_custom else const.CHATGPTONAZURE if route == const.CHATGPTONAZURE else "openai"
        return {
            'provider': provider,
            'api_key': conf().get("custom_api_key") if is_custom else conf().get("open_ai_api_key"),
            'api_base': conf().get("custom_api_base") if is_custom else conf().get("open_ai_api_base"),
            'model': conf().get("model", "gpt-3.5-turbo"),
            'default_temperature': conf().get("temperature", 0.9),
            'default_top_p': conf().get("top_p", 1.0),
            'default_frequency_penalty': conf().get("frequency_penalty", 0.0),
            'default_presence_penalty': conf().get("presence_penalty", 0.0),
        }

    def _get_http_client(self) -> OpenAIHTTPClient:
        """Override the default HTTP client to reuse our pre-configured one."""
        return self._http_client
    
    def reply(self, query, context=None):
        # acquire reply content
        if context.type == ContextType.TEXT:
            logger.info("[CHATGPT] query={}".format(query))

            session_id = context["session_id"]
            reply = None
            clear_memory_commands = conf().get("clear_memory_commands", ["#清除记忆"])
            if query in clear_memory_commands:
                self.sessions.clear_session(session_id)
                reply = Reply(ReplyType.INFO, _t("记忆已清除", "Memory cleared"))
            elif query == "#清除所有":
                self.sessions.clear_all_session()
                reply = Reply(ReplyType.INFO, _t("所有人记忆已清除", "All memories cleared"))
            elif query == "#更新配置":
                load_config()
                reply = Reply(ReplyType.INFO, _t("配置已更新", "Config updated"))
            if reply:
                return reply
            session = self.sessions.session_query(query, session_id)
            logger.debug("[CHATGPT] session query={}".format(session.messages))

            api_key = context.get("openai_api_key")
            model = context.get("gpt_model")
            new_args = None
            if model:
                new_args = self.args.copy()
                new_args["model"] = model
            # if context.get('stream'):
            #     # reply in stream
            #     return self.reply_text_stream(query, new_query, session_id)

            reply_content = self.reply_text(session, api_key, args=new_args)
            logger.debug(
                "[CHATGPT] new_query={}, session_id={}, reply_cont={}, completion_tokens={}".format(
                    session.messages,
                    session_id,
                    reply_content["content"],
                    reply_content["completion_tokens"],
                )
            )
            if reply_content["completion_tokens"] == 0 and len(reply_content["content"]) > 0:
                reply = Reply(ReplyType.ERROR, reply_content["content"])
            elif reply_content["completion_tokens"] > 0:
                self.sessions.session_reply(reply_content["content"], session_id, reply_content["total_tokens"])
                reply = Reply(ReplyType.TEXT, reply_content["content"])
            else:
                reply = Reply(ReplyType.ERROR, reply_content["content"])
                logger.debug("[CHATGPT] reply {} used 0 tokens.".format(reply_content))
            return reply

        elif context.type == ContextType.IMAGE_CREATE:
            ok, retstring = self.create_img(query, 0)
            reply = None
            if ok:
                reply = Reply(ReplyType.IMAGE_URL, retstring)
            else:
                reply = Reply(ReplyType.ERROR, retstring)
            return reply
        elif context.type == ContextType.IMAGE:
            logger.info("[CHATGPT] Image message received")
            reply = self.reply_image(context)
            return reply
        else:
            reply = Reply(ReplyType.ERROR, _t("Bot不支持处理{}类型的消息", "Bot does not support message type {}").format(context.type))
            return reply

    def reply_image(self, context):
        """
        Process image message using OpenAI Vision API
        """
        import base64
        import os
        
        try:
            image_path = context.content
            logger.info(f"[CHATGPT] Processing image: {image_path}")
            
            # Check if file exists
            if not os.path.exists(image_path):
                logger.error(f"[CHATGPT] Image file not found: {image_path}")
                return Reply(ReplyType.ERROR, _t("图片文件不存在", "Image file not found"))
            
            # Read and encode image
            with open(image_path, "rb") as f:
                image_data = f.read()
                image_base64 = base64.b64encode(image_data).decode("utf-8")
            
            # Detect image format
            extension = os.path.splitext(image_path)[1].lower()
            mime_type_map = {
                ".jpg": "image/jpeg",
                ".jpeg": "image/jpeg", 
                ".png": "image/png",
                ".gif": "image/gif",
                ".webp": "image/webp"
            }
            mime_type = mime_type_map.get(extension, "image/jpeg")
            
            # Get model and API config
            is_custom = self._is_custom_route()
            model = context.get("gpt_model") or conf().get("model", "gpt-4o")
            api_key = context.get("openai_api_key") or (conf().get("custom_api_key") if is_custom else conf().get("open_ai_api_key"))
            api_base = conf().get("custom_api_base") if is_custom else conf().get("open_ai_api_base")
            provider = "custom" if is_custom else "openai"
            
            # Build vision request
            messages = [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "请描述这张图片的内容"},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:{mime_type};base64,{image_base64}"
                            }
                        }
                    ]
                }
            ]
            
            logger.info(f"[CHATGPT] Calling vision API with model: {model}")
            
            # Call OpenAI-compatible API via HTTP
            span = ModelCallSpan(
                provider=provider,
                model=model,
                stream=False,
                retry_count=0,
                api_path=LEGACY_REPLY_IMAGE_API_PATH,
            )
            try:
                response = self._http_client.chat_completions(
                    api_key=api_key or None,
                    api_base=api_base or None,
                    model=model,
                    messages=messages,
                    max_tokens=1000,
                )
                span.observe_response(response)
                content = response["choices"][0]["message"]["content"]
                logger.info(f"[CHATGPT] Vision API response: {content[:100]}...")
                span.finish_completed()
            except Exception as call_error:
                if isinstance(call_error, OpenAIHTTPError):
                    error_value = call_error.body.get("error") if isinstance(call_error.body, dict) else {}
                    error_payload = error_value if isinstance(error_value, dict) else {}
                    span.finish_error(
                        message=call_error.message,
                        status_code=call_error.status_code,
                        error_code=error_payload.get("code", ""),
                        error_type=error_payload.get("type", ""),
                    )
                else:
                    span.finish_error(message=str(call_error), status_code=500)
                raise

            # Clean up temp file
            try:
                os.remove(image_path)
                logger.debug(f"[CHATGPT] Removed temp image file: {image_path}")
            except Exception:
                pass
            
            return Reply(ReplyType.TEXT, content)
            
        except Exception as e:
            logger.error(f"[CHATGPT] Image processing error: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return Reply(ReplyType.ERROR, _t("图片识别失败: ", "Image recognition failed: ") + str(e))

    def reply_text(self, session: ChatGPTSession, api_key=None, args=None, retry_count=0, model_retry_sleep=None) -> dict:
        """
        call openai's ChatCompletion to get the answer
        :param session: a conversation session
        :param session_id: session id
        :param retry_count: retry count
        :return: {}
        """
        try:
            if conf().get("rate_limit_chatgpt") and not self.tb4chatgpt.get_token():
                raise RateLimitError("RateLimitError: rate limit exceeded")
            # If api_key is None, the per-instance default key will be used.
            if args is None:
                args = self.args
            # Translate old SDK kwargs to HTTP client params:
            # - request_timeout / timeout -> per-call timeout
            call_args = dict(args)
            timeout = call_args.pop("request_timeout", None) or call_args.pop("timeout", None)
            response = self._http_client.chat_completions(
                api_key=api_key or None,
                timeout=timeout,
                messages=session.messages,
                **call_args,
            )
            logger.info("[ChatGPT] reply={}, total_tokens={}".format(
                response["choices"][0]["message"]["content"],
                response["usage"]["total_tokens"]
            ))
            return {
                "total_tokens": response["usage"]["total_tokens"],
                "completion_tokens": response["usage"]["completion_tokens"],
                "content": response["choices"][0]["message"]["content"],
            }
        except OpenAIHTTPError as http_err:
            return self._handle_reply_error(
                wrap_http_error(http_err), session, api_key, args, retry_count, model_retry_sleep
            )
        except Exception as e:
            return self._handle_reply_error(e, session, api_key, args, retry_count, model_retry_sleep)

    def _handle_reply_error(self, e, session, api_key, args, retry_count, model_retry_sleep=None):
        """Map exception to user-facing reply with retry/backoff (mirrors SDK behavior)."""
        max_retries = legacy_reply_max_retries(default=2)
        try:
            attempt = max(0, int(retry_count or 0))
        except (TypeError, ValueError):
            attempt = 0
        details = openai_legacy_error_details(e)
        decision = build_retry_decision(
            details,
            attempt=attempt,
            max_retries=max_retries,
        )
        result = {"completion_tokens": 0, "content": _t("我现在有点累了，等会再来吧", "I'm a bit tired right now. Please try again later.")}
        if isinstance(e, RateLimitError):
            logger.warn("[CHATGPT] RateLimitError: {}".format(e))
            result["content"] = _t("提问太快啦，请休息一下再问我吧", "You're asking too fast. Please take a short break and try again.")
        elif isinstance(e, Timeout):
            logger.warn("[CHATGPT] Timeout: {}".format(e))
            result["content"] = _t("我没有收到你的消息", "I didn't receive your message")
        elif isinstance(e, APIConnectionError):
            logger.warn("[CHATGPT] APIConnectionError: {}".format(e))
            result["content"] = _t("我连接不到你的网络", "I can't reach your network")
        elif isinstance(e, APIError):
            logger.warn("[CHATGPT] Bad Gateway: {}".format(e))
            result["content"] = _t("请再问我一次", "Please ask me again")
        elif isinstance(e, OpenAIError):
            logger.warn("[CHATGPT] OpenAIError: {}".format(e))
        else:
            logger.exception("[CHATGPT] Exception: {}".format(e))
            self.sessions.clear_session(session.session_id)
            details = legacy_adapter_error_details(e)
            decision = build_retry_decision(details, attempt=attempt, max_retries=0)

        if decision.should_retry:
            logger.warn("[CHATGPT] 第{}次重试".format(retry_count + 1))
            run_legacy_reply_retry_sleep(decision, model_retry_sleep)
            return self.reply_text(
                session,
                api_key,
                args,
                retry_count + 1,
                model_retry_sleep=model_retry_sleep,
            )
        return legacy_reply_failure_result(
            content=result["content"],
            details=details,
            decision=decision,
        )

class AzureChatGPTBot(ChatGPTBot):
    """Azure OpenAI variant.

    Azure's HTTP shape differs from public OpenAI:
      URL    : {endpoint}/openai/deployments/{deployment}/chat/completions
      Auth   : api-key header (not Bearer)
      Query  : ?api-version={version}
    We model that with a dedicated HTTP client and override _get_http_client
    so the OpenAICompatibleBot streaming/tool path uses it transparently.
    """

    def __init__(self):
        super().__init__()
        self._azure_api_version = conf().get("azure_api_version", "2023-06-01-preview")
        self._azure_deployment_id = conf().get("azure_deployment_id")
        # Drop legacy SDK kwarg; Azure deployment is encoded in the URL now.
        self.args.pop("deployment_id", None)
        self._configure_azure_http_client()

    def configure_model_route(self, bot_type: str):
        self._ecorex_route_bot_type = bot_type or conf().get("bot_type") or const.CHATGPTONAZURE
        self._configure_http_client_for_route()
        self._azure_api_version = conf().get("azure_api_version", "2023-06-01-preview")
        self._azure_deployment_id = conf().get("azure_deployment_id")
        self._configure_azure_http_client()
        return self

    def _configure_azure_http_client(self) -> None:
        endpoint = (self._api_base or "").rstrip("/")
        deployment = self._azure_deployment_id or ""
        # Build a base that already includes /openai/deployments/{deployment}.
        # /chat/completions will be appended by the client.
        azure_base = (
            f"{endpoint}/openai/deployments/{deployment}" if endpoint and deployment else endpoint
        )
        self._http_client = _AzureChatHTTPClient(
            api_key=self._api_key,
            api_base=azure_base,
            api_version=self._azure_api_version,
            proxy=self._proxy,
        )

    @staticmethod
    def _config_value(config, key, fallback_key=None, default=None):
        value = config.get(key, None)
        if value in (None, "") and fallback_key:
            value = config.get(fallback_key, default)
        return default if value in (None, "") else value

    @staticmethod
    def _coerce_positive_int(value, default):
        try:
            return max(1, int(value))
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _coerce_non_negative_float(value, default):
        try:
            return max(0.0, float(value))
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _azure_image_error(message, status_code=502, error_code="", error_type="provider_protocol_error"):
        return provider_error_response(
            {
                "message": message,
                "code": error_code,
                "type": error_type,
            },
            message=message,
            status_code=status_code,
        )

    @staticmethod
    def _azure_first_image_url(data):
        if not isinstance(data, dict):
            return None
        items = data.get("data")
        if isinstance(items, list) and items:
            first = items[0]
            if isinstance(first, dict):
                return first.get("url")
        result = data.get("result")
        if isinstance(result, dict):
            return AzureChatGPTBot._azure_first_image_url(result)
        return None

    def _azure_dalle_endpoint(self, config):
        endpoint = self._config_value(
            config,
            "azure_openai_dalle_api_base",
            "open_ai_api_base",
            "",
        )
        endpoint = str(endpoint or "").rstrip("/")
        return "{}/".format(endpoint) if endpoint else ""

    def _azure_dalle_api_key(self, config, api_key=None):
        key = self._config_value(config, "azure_openai_dalle_api_key", default="")
        if key in (None, ""):
            key = api_key
        if key in (None, ""):
            key = config.get("open_ai_api_key", "")
        return key

    def _azure_dalle_failure(self):
        return _t("鍥剧墖鐢熸垚澶辫触", "Image generation failed")

    def _azure_dalle_request_timeout(self, config, default):
        return self._coerce_non_negative_float(
            config.get("azure_dalle_request_timeout", config.get("request_timeout", default)),
            default,
        )

    def _azure_dalle_max_retries(self, config):
        return config.get("model_max_retries", config.get("max_model_retries"))

    def _create_azure_dalle2_image(self, query, retry_count=0, api_key=None, model_retry_sleep=None):
        config = conf()
        model = "dall-e-2"
        api_version = "2023-06-01-preview"
        endpoint = self._azure_dalle_endpoint(config)
        url = "{}openai/images/generations:submit?api-version={}".format(endpoint, api_version)
        headers = {
            "api-key": self._azure_dalle_api_key(config, api_key),
            "Content-Type": "application/json",
        }
        timeout = self._azure_dalle_request_timeout(config, 120)
        body = {
            "prompt": query,
            "size": config.get("image_create_size", "256x256"),
            "n": 1,
        }

        def submit_task():
            submission = requests.post(url, headers=headers, json=body, timeout=timeout)
            if submission.status_code < 200 or submission.status_code >= 300:
                raise image_error_from_response(submission)
            operation_location = (
                submission.headers.get("operation-location")
                or submission.headers.get("Operation-Location")
            )
            if not operation_location:
                raise ModelImageCallError(self._azure_image_error(
                    "Azure DALL-E submit response missing operation-location",
                    error_code="missing_operation_location",
                ))
            return operation_location

        ok, operation_location = create_img_with_retry(
            self,
            submit_task,
            provider="azure_openai",
            model=model,
            retry_count=retry_count,
            max_model_retries=self._azure_dalle_max_retries(config),
            retry_sleep=model_retry_sleep,
            failure_message=self._azure_dalle_failure(),
        )
        if not ok:
            return False, operation_location

        max_wait_times = self._coerce_positive_int(
            config.get("azure_dalle_poll_max_wait_times", 60),
            60,
        )
        poll_interval = self._coerce_non_negative_float(
            config.get("azure_dalle_poll_interval", 2),
            2.0,
        )
        for attempt in range(max_wait_times):
            if poll_interval > 0:
                sleep_for_retry(poll_interval, model_retry_sleep)
            try:
                response = requests.get(operation_location, headers=headers, timeout=timeout)
                if response.status_code < 200 or response.status_code >= 300:
                    details = http_error_response(response)
                    decision = build_retry_decision(
                        details,
                        attempt=attempt,
                        max_retries=max_wait_times - 1,
                    )
                    set_create_img_error(self, details, decision)
                    if decision.should_retry:
                        sleep_for_retry(decision.delay_seconds, model_retry_sleep)
                        continue
                    return False, self._azure_dalle_failure()
                data = response.json()
            except Exception as exc:
                details = image_error_details_from_exception(exc)
                decision = build_retry_decision(
                    details,
                    attempt=attempt,
                    max_retries=max_wait_times - 1,
                )
                set_create_img_error(self, details, decision)
                if decision.should_retry:
                    sleep_for_retry(decision.delay_seconds, model_retry_sleep)
                    continue
                return False, self._azure_dalle_failure()

            status = str(data.get("status") or "").lower()
            if status == "succeeded":
                image_url = self._azure_first_image_url(data)
                if image_url:
                    clear_create_img_error(self)
                    return True, image_url
                set_create_img_error(self, self._azure_image_error(
                    "Azure DALL-E task succeeded without image URL",
                    error_code="missing_image_url",
                ))
                return False, self._azure_dalle_failure()
            if status in ("failed", "cancelled", "canceled"):
                error_payload = data.get("error") if isinstance(data.get("error"), dict) else {}
                error_message = (
                    error_payload.get("message")
                    or data.get("message")
                    or "Azure DALL-E image task failed"
                )
                error_payload = dict(error_payload)
                error_payload.setdefault("message", error_message)
                error_payload.setdefault("code", "task_failed")
                error_payload.setdefault("type", "task_failed")
                set_create_img_error(
                    self,
                    provider_error_response(
                        error_payload,
                        message=error_message,
                        status_code=data.get("status_code") or 500,
                    ),
                )
                return False, self._azure_dalle_failure()

        set_create_img_error(self, self._azure_image_error(
            "Azure DALL-E image task timed out",
            status_code=504,
            error_code="task_timeout",
            error_type="timeout",
        ))
        return False, self._azure_dalle_failure()

    def _create_azure_dalle3_image(self, query, retry_count=0, api_key=None, model_retry_sleep=None):
        config = conf()
        model = "dall-e-3"
        api_version = config.get("azure_api_version", "2024-02-15-preview")
        endpoint = self._azure_dalle_endpoint(config)
        deployment = self._config_value(
            config,
            "azure_openai_dalle_deployment_id",
            "text_to_image",
            model,
        )
        url = "{}openai/deployments/{}/images/generations?api-version={}".format(
            endpoint,
            deployment,
            api_version,
        )
        headers = {
            "api-key": self._azure_dalle_api_key(config, api_key),
            "Content-Type": "application/json",
        }
        timeout = self._azure_dalle_request_timeout(config, 120)
        body = {
            "prompt": query,
            "size": config.get("image_create_size", "1024x1024"),
            "quality": config.get("dalle3_image_quality", "standard"),
        }

        def invoke_generation():
            response = requests.post(url, headers=headers, json=body, timeout=timeout)
            if response.status_code < 200 or response.status_code >= 300:
                raise image_error_from_response(response)
            image_url = self._azure_first_image_url(response.json())
            if not image_url:
                raise ModelImageCallError(self._azure_image_error(
                    "Azure DALL-E response missing image URL",
                    error_code="missing_image_url",
                ))
            return image_url

        return create_img_with_retry(
            self,
            invoke_generation,
            provider="azure_openai",
            model=model,
            retry_count=retry_count,
            max_model_retries=self._azure_dalle_max_retries(config),
            retry_sleep=model_retry_sleep,
            failure_message=self._azure_dalle_failure(),
        )

    def create_img(self, query, retry_count=0, api_key=None, model_retry_sleep=None):
        config = conf()
        text_to_image_model = OpenAIImage._normalize_image_model(config.get("text_to_image") or OpenAIImage.DEFAULT_IMAGE_MODEL)
        if OpenAIImage._is_gpt_image_model(text_to_image_model):
            return OpenAIImage.create_img(
                self,
                query,
                retry_count,
                api_key=config.get("open_ai_api_key") or api_key,
                api_base=config.get("open_ai_api_base"),
                model_retry_sleep=model_retry_sleep,
            )
        if text_to_image_model == "dall-e-2":
            return self._create_azure_dalle2_image(
                query,
                retry_count=retry_count,
                api_key=api_key,
                model_retry_sleep=model_retry_sleep,
            )
        if text_to_image_model == "dall-e-3":
            return self._create_azure_dalle3_image(
                query,
                retry_count=retry_count,
                api_key=api_key,
                model_retry_sleep=model_retry_sleep,
            )
        return False, "Image generation failed: text_to_image is not configured"

class _AzureChatHTTPClient(OpenAIHTTPClient):
    """Subclass that injects Azure's ``api-version`` query param and ``api-key``
    header on every chat-completion request, and accepts the deployment-scoped
    base URL set by :class:`AzureChatGPTBot`.
    """

    def __init__(self, api_key, api_base, api_version, proxy=None, timeout=None):
        super().__init__(
            api_key=api_key, api_base=api_base, proxy=proxy, timeout=timeout
        )
        self._api_version = api_version

    def _build_headers(self, api_key, extra_headers, url=None):
        # Azure uses api-key header, not Bearer token. No attribution
        # headers — Azure deployments are the customer's own tenant.
        key = api_key if api_key is not None else self.api_key
        headers = {"Content-Type": "application/json"}
        if key:
            headers["api-key"] = key
        if self.extra_headers:
            headers.update(self.extra_headers)
        if extra_headers:
            headers.update(extra_headers)
        return headers

    def chat_completions(self, **kwargs):
        # Always force api-version query param for Azure.
        eq = dict(kwargs.get("extra_query") or {})
        eq.setdefault("api-version", self._api_version)
        kwargs["extra_query"] = eq
        return super().chat_completions(**kwargs)
