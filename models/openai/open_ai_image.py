import base64
import os
import tempfile
import time
import uuid

from common.log import logger
from common.token_bucket import TokenBucket
from config import conf
from models.openai.openai_compat import RateLimitError, wrap_http_error
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
        response = self._image_client.images_generate(
            api_key=api_key or None,
            api_base=api_base or None,
            **payload,
        )
        output_format = payload.get("output_format") or "png"
        return self._save_image_item(response["data"][0], output_format)

    def create_img(self, query, retry_count=0, api_key=None, api_base=None):
        try:
            if conf().get("rate_limit_dalle") and not self.tb4dalle.get_token():
                return False, "请求太快了，请休息一下再问我吧"
            logger.info("[OPEN_AI] image_query={}".format(query))
            model = self._normalize_image_model(conf().get("text_to_image") or self.DEFAULT_IMAGE_MODEL)
            try:
                image_url = self._create_img_once(query, model, api_key=api_key, api_base=api_base)
            except OpenAIHTTPError as http_err:
                if model == self.DEFAULT_IMAGE_MODEL and self._is_model_unavailable_error(http_err):
                    logger.warning(
                        "[OPEN_AI] image model %s unavailable, falling back to %s: %s",
                        model,
                        self.FALLBACK_IMAGE_MODEL,
                        http_err,
                    )
                    image_url = self._create_img_once(
                        query,
                        self.FALLBACK_IMAGE_MODEL,
                        api_key=api_key,
                        api_base=api_base,
                    )
                else:
                    raise
            logger.info("[OPEN_AI] image_url={}".format(image_url))
            return True, image_url
        except OpenAIHTTPError as http_err:
            mapped = wrap_http_error(http_err)
            if isinstance(mapped, RateLimitError):
                logger.warn(mapped)
                if retry_count < 1:
                    time.sleep(5)
                    logger.warn("[OPEN_AI] ImgCreate RateLimit exceed, 第{}次重试".format(retry_count + 1))
                    return self.create_img(query, retry_count + 1)
                return False, "画图出现问题，请休息一下再问我吧"
            logger.exception(mapped)
            return False, "画图出现问题，请休息一下再问我吧"
        except RateLimitError as e:
            logger.warn(e)
            if retry_count < 1:
                time.sleep(5)
                logger.warn("[OPEN_AI] ImgCreate RateLimit exceed, 第{}次重试".format(retry_count + 1))
                return self.create_img(query, retry_count + 1)
            return False, "画图出现问题，请休息一下再问我吧"
        except Exception as e:
            logger.exception(e)
            return False, "画图出现问题，请休息一下再问我吧"
