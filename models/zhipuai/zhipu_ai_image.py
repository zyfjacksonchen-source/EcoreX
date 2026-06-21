from common.log import logger
from config import conf
from models.model_image_retry import create_img_with_retry, set_create_img_error


# ZhipuAI提供的画图接口

class ZhipuAIImage(object):
    def __init__(self):
        from zai import ZhipuAiClient
        # 初始化客户端，支持自定义 API base URL（例如智谱国际版 z.ai）
        api_key = conf().get("zhipu_ai_api_key")
        api_base = conf().get("zhipu_ai_api_base")
        
        if api_base:
            self.client = ZhipuAiClient(api_key=api_key, base_url=api_base)
        else:
            self.client = ZhipuAiClient(api_key=api_key)

    def create_img(self, query, retry_count=0, api_key=None, api_base=None, model_retry_sleep=None):
        if conf().get("rate_limit_dalle"):
            set_create_img_error(
                self,
                {
                    "message": "\u8bf7\u6c42\u592a\u5feb\u4e86\uff0c\u8bf7\u4f11\u606f\u4e00\u4e0b\u518d\u95ee\u6211\u5427",
                    "status_code": 429,
                    "error_code": "local_rate_limit",
                    "error_type": "rate_limit",
                },
            )
            return False, "\u8bf7\u6c42\u592a\u5feb\u4e86\uff0c\u8bf7\u4f11\u606f\u4e00\u4e0b\u518d\u95ee\u6211\u5427"

        logger.info("[ZHIPU_AI] image_query={}".format(query))
        model = conf().get("text_to_image") or "cogview-3"
        size = conf().get("image_create_size", "1024x1024")

        def invoke():
            response = self.client.images.generations(
                prompt=query,
                n=1,
                model=model,
                size=size,
                quality="standard",
            )
            image_url = response.data[0].url
            logger.info("[ZHIPU_AI] image_url={}".format(image_url))
            return image_url

        return create_img_with_retry(
            self,
            invoke,
            provider="zhipu",
            model=model,
            retry_count=retry_count,
            max_model_retries=conf().get("model_max_retries", conf().get("max_model_retries", 1)),
            retry_sleep=model_retry_sleep,
            failure_message="\u753b\u56fe\u51fa\u73b0\u95ee\u9898\uff0c\u8bf7\u4f11\u606f\u4e00\u4e0b\u518d\u95ee\u6211\u5427",
            error_normalizer=getattr(self, "_provider_error_from_exception", None),
        )
