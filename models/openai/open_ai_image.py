import base64
import os
import tempfile
import threading
import uuid

from common.log import logger
from common.token_bucket import TokenBucket
from config import conf
from models.model_retry import build_retry_decision, coerce_max_retries, sleep_for_retry
from models.openai.openai_compat import RateLimitError
from models.openai.openai_http_client import OpenAIHTTPClient, OpenAIHTTPError


# OpenAI image generation API wrapper
class OpenAIImage(object):
    DEFAULT_IMAGE_MODEL = "gpt-image-2-pro"
    FALLBACK_IMAGE_MODEL = "gpt-image-2"
    IMAGE_MODEL_ALIASES = {
        "image-2-pro": "gpt-image-2-pro",
        "image-2": "gpt-image-2",
    }

    def __init__(self):
        # Lazy default client; subclasses (ChatGPTBot/OpenAIBot) typically
        # construct their own _http_client and override _get_image_client().
        self._image_api_key = conf().get("open_ai_api_key")
        self._image_api_base = conf().get("open_ai_api_base") or None
        self._image_proxy = conf().get("proxy") or None
        self._image_client = OpenAIHTTPClient(
            api_key=self._image_api_key,
            api_base=self._image_api_base,
            proxy=self._image_proxy,
        )
        if conf().get("rate_limit_dalle"):
            self.tb4dalle = TokenBucket(conf().get("rate_limit_dalle", 50))

    @staticmethod
    def _normalize_image_model(model: str) -> str:
        value = str(model or "").strip()
        return OpenAIImage.IMAGE_MODEL_ALIASES.get(value, value)

    @staticmethod
    def _is_gpt_image_model(model: str) -> bool:
        return OpenAIImage._normalize_image_model(model).startswith("gpt-image")

    @staticmethod
    def _is_model_unavailable_error(exc: Exception) -> bool:
        text = str(exc).lower()
        return any(
            marker in text
            for marker in (
                "model_not_found",
                "model not found",
                "does not exist",
                "do not have access",
                "don't have access",
                "not have access",
                "unsupported model",
                "invalid model",
            )
        )

    @staticmethod
    def _image_output_path(output_format: str = "png") -> str:
        suffix = "." + (output_format or "png").strip(".")
        root = os.path.join(tempfile.gettempdir(), "ecorex-images")
        os.makedirs(root, exist_ok=True)
        return os.path.join(root, f"image-{uuid.uuid4().hex}{suffix}")

    def _save_image_item(self, item: dict, output_format: str = "png") -> str:
        if item.get("url"):
            return item["url"]
        if not item.get("b64_json"):
            raise RuntimeError("Image response had neither url nor b64_json")
        path = self._image_output_path(output_format)
        with open(path, "wb") as handle:
            handle.write(base64.b64decode(item["b64_json"]))
        return f"file://{path}"

    def _build_image_payload(self, query: str, model: str) -> dict:
        model = self._normalize_image_model(model)
        payload = {
            "prompt": query,
            "n": 1,
            "model": model,
        }
        size = conf().get("image_create_size")
        if size:
            payload["size"] = size
        if self._is_gpt_image_model(model):
            quality = conf().get("image_create_quality") or conf().get("dalle3_image_quality") or "auto"
            if quality:
                payload["quality"] = quality
            output_format = conf().get("image_output_format") or "png"
            if output_format in {"png", "jpeg", "webp"}:
                payload["output_format"] = output_format
            background = conf().get("image_background") or "auto"
            if background in {"auto", "opaque", "transparent"}:
                payload["background"] = background
            moderation = conf().get("image_moderation") or "auto"
            if moderation in {"auto", "low"}:
                payload["moderation"] = moderation
        elif model == "dall-e-3":
            payload["quality"] = conf().get("dalle3_image_quality", "standard")
            style = conf().get("dalle3_image_style")
            if style:
                payload["style"] = style
            payload.setdefault("size", "1024x1024")
        else:
            payload.setdefault("size", "256x256")
            payload["response_format"] = "url"
        return payload

    def _create_img_once(self, query: str, model: str, api_key=None, api_base=None) -> str:
        payload = self._build_image_payload(query, model)
        response = self._get_image_client().images_generate(
            api_key=api_key or None,
            api_base=api_base or None,
            **payload,
        )
        output_format = payload.get("output_format") or "png"
        return self._save_image_item(response["data"][0], output_format)

    def _get_image_client(self) -> OpenAIHTTPClient:
        client = getattr(self, "_image_client", None)
        if client is not None:
            return client
        self._image_api_key = conf().get("open_ai_api_key")
        self._image_api_base = conf().get("open_ai_api_base") or None
        self._image_proxy = conf().get("proxy") or None
        self._image_client = OpenAIHTTPClient(
            api_key=self._image_api_key,
            api_base=self._image_api_base,
            proxy=self._image_proxy,
        )
        return self._image_client

    def _get_dalle_token_bucket(self):
        bucket = getattr(self, "tb4dalle", None)
        if bucket is None:
            bucket = TokenBucket(conf().get("rate_limit_dalle", 50))
            self.tb4dalle = bucket
        return bucket

    @staticmethod
    def _image_http_error_details(exc: Exception) -> dict:
        if isinstance(exc, OpenAIHTTPError):
            body = exc.body if isinstance(exc.body, dict) else {}
            error_value = body.get("error") if isinstance(body, dict) else {}
            error = error_value if isinstance(error_value, dict) else {}
            return {
                "message": exc.message or str(exc),
                "status_code": exc.status_code,
                "error_code": error.get("code") or body.get("code") or "",
                "error_type": error.get("type") or body.get("type") or "",
                "retry_after": exc.retry_after,
            }
        if isinstance(exc, RateLimitError):
            return {
                "message": str(exc),
                "status_code": 429,
                "error_code": "rate_limit",
                "error_type": "rate_limit",
            }
        return {
            "message": str(exc),
            "status_code": 500,
            "error_code": "",
            "error_type": type(exc).__name__,
        }

    def _set_create_img_error(self, details: dict, decision=None) -> None:
        stored = dict(details or {})
        if decision is not None:
            stored.update({
                "error_taxonomy": decision.taxonomy,
                "retryable": decision.retryable,
                "retry_attempt": decision.attempt,
                "retry_attempts": decision.attempt,
                "max_retries": decision.max_retries,
                "retry_exhausted": decision.retryable and not decision.should_retry,
            })
            if decision.retry_after_seconds is not None:
                stored["retry_after_seconds"] = decision.retry_after_seconds
        state = getattr(self, "_ecorex_create_img_error_state", None)
        if state is None:
            state = threading.local()
            self._ecorex_create_img_error_state = state
        state.details = stored

    def _clear_create_img_error(self) -> None:
        state = getattr(self, "_ecorex_create_img_error_state", None)
        if state is not None:
            state.details = None

    def _create_img_with_model_fallback(self, query, model, api_key=None, api_base=None):
        try:
            return self._create_img_once(query, model, api_key=api_key, api_base=api_base)
        except OpenAIHTTPError as http_err:
            if model == self.DEFAULT_IMAGE_MODEL and self._is_model_unavailable_error(http_err):
                logger.warning(
                    "[OPEN_AI] image model %s unavailable, falling back to %s: %s",
                    model,
                    self.FALLBACK_IMAGE_MODEL,
                    http_err,
                )
                return self._create_img_once(
                    query,
                    self.FALLBACK_IMAGE_MODEL,
                    api_key=api_key,
                    api_base=api_base,
                )
            raise

    def create_img(
        self,
        query,
        retry_count=0,
        api_key=None,
        api_base=None,
        model_retry_sleep=None,
    ):
        self._clear_create_img_error()
        try:
            if conf().get("rate_limit_dalle") and not self._get_dalle_token_bucket().get_token():
                self._set_create_img_error({
                    "message": "请求太快了，请休息一下再问我吧",
                    "status_code": 429,
                    "error_code": "local_rate_limit",
                    "error_type": "rate_limit",
                })
                return False, "请求太快了，请休息一下再问我吧"
        except Exception as e:
            logger.exception(e)
            self._set_create_img_error(self._image_http_error_details(e))
            return False, "画图出现问题，请休息一下再问我吧"

        logger.info("[OPEN_AI] image_query={}".format(query))
        model = self._normalize_image_model(conf().get("text_to_image") or self.DEFAULT_IMAGE_MODEL)
        max_retries = coerce_max_retries(
            conf().get("model_max_retries", conf().get("max_model_retries", 1)),
            default=1,
        )
        try:
            attempt = max(0, int(retry_count or 0))
        except (TypeError, ValueError):
            attempt = 0

        while True:
            try:
                image_url = self._create_img_with_model_fallback(
                    query,
                    model,
                    api_key=api_key,
                    api_base=api_base,
                )
                self._clear_create_img_error()
                logger.info("[OPEN_AI] image_url={}".format(image_url))
                return True, image_url
            except (OpenAIHTTPError, RateLimitError) as exc:
                details = self._image_http_error_details(exc)
                decision = build_retry_decision(
                    details,
                    attempt=attempt,
                    max_retries=max_retries,
                )
                self._set_create_img_error(details, decision)
                if decision.should_retry:
                    logger.warning(
                        "[OPEN_AI] retrying image generation after %.3fs "
                        "(attempt=%s/%s taxonomy=%s)",
                        decision.delay_seconds,
                        attempt + 1,
                        max_retries,
                        decision.taxonomy,
                    )
                    sleep_for_retry(decision.delay_seconds, model_retry_sleep)
                    attempt += 1
                    continue
                logger.warning(
                    "[OPEN_AI] image generation failed without retry: %s",
                    details.get("message") or exc,
                )
                return False, "画图出现问题，请休息一下再问我吧"
            except Exception as e:
                logger.exception(e)
                self._set_create_img_error(self._image_http_error_details(e))
                return False, "画图出现问题，请休息一下再问我吧"
