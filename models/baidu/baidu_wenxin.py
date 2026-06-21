# encoding:utf-8

import requests
import json
from common import const
from models.bot import Bot
from models.session_manager import SessionManager
from bridge.context import ContextType
from bridge.reply import Reply, ReplyType
from common.log import logger
from config import conf
from models.baidu.baidu_wenxin_session import BaiduWenxinSession
from models.legacy_direct_chat_retry import (
    legacy_direct_chat_decision,
    legacy_direct_chat_exception_details,
    legacy_direct_chat_failure_result,
    legacy_direct_chat_response_details,
    run_legacy_direct_chat_retry_sleep,
)
from models.model_provider_errors import provider_error_response

BAIDU_API_KEY = conf().get("baidu_wenxin_api_key")
BAIDU_SECRET_KEY = conf().get("baidu_wenxin_secret_key")


class BaiduAccessTokenError(Exception):
    def __init__(self, response):
        super().__init__("Baidu Wenxin access token request failed")
        self.response = response


class BaiduWenxinBot(Bot):

    def __init__(self):
        super().__init__()
        wenxin_model = conf().get("baidu_wenxin_model")
        self.prompt_enabled = conf().get("baidu_wenxin_prompt_enabled")
        if self.prompt_enabled:
            self.prompt = conf().get("character_desc", "")
            if self.prompt == "":
                logger.warn("[BAIDU] Although you enabled model prompt, character_desc is not specified.")
        if wenxin_model is not None:
            wenxin_model = conf().get("baidu_wenxin_model") or "eb-instant"
        else:
            if conf().get("model") and conf().get("model") == const.WEN_XIN:
                wenxin_model = "completions"
            elif conf().get("model") and conf().get("model") == const.WEN_XIN_4:
                wenxin_model = "completions_pro"

        self.sessions = SessionManager(BaiduWenxinSession, model=wenxin_model)

    def reply(self, query, context=None):
        # acquire reply content
        if context and context.type:
            if context.type == ContextType.TEXT:
                logger.info("[BAIDU] query={}".format(query))
                session_id = context["session_id"]
                reply = None
                if query == "#清除记忆":
                    self.sessions.clear_session(session_id)
                    reply = Reply(ReplyType.INFO, "记忆已清除")
                elif query == "#清除所有":
                    self.sessions.clear_all_session()
                    reply = Reply(ReplyType.INFO, "所有人记忆已清除")
                else:
                    session = self.sessions.session_query(query, session_id)
                    result = self.reply_text(session)
                    total_tokens, completion_tokens, reply_content = (
                        result["total_tokens"],
                        result["completion_tokens"],
                        result["content"],
                    )
                    logger.debug(
                        "[BAIDU] new_query={}, session_id={}, reply_cont={}, completion_tokens={}".format(session.messages, session_id, reply_content, completion_tokens)
                    )

                    if total_tokens == 0:
                        reply = Reply(ReplyType.ERROR, reply_content)
                    else:
                        self.sessions.session_reply(reply_content, session_id, total_tokens)
                        reply = Reply(ReplyType.TEXT, reply_content)
                return reply
            elif context.type == ContextType.IMAGE_CREATE:
                ok, retstring = self.create_img(query, 0)
                reply = None
                if ok:
                    reply = Reply(ReplyType.IMAGE_URL, retstring)
                else:
                    reply = Reply(ReplyType.ERROR, retstring)
                return reply

    def reply_text(self, session: BaiduWenxinSession, retry_count=0, model_retry_sleep=None):
        try:
            logger.info("[BAIDU] model={}".format(session.model))
            access_token = self.get_access_token()
            if access_token == 'None':
                logger.warn("[BAIDU] access token 获取失败")
                details = provider_error_response(
                    {
                        "message": "Baidu Wenxin access token unavailable",
                        "code": "access_token_unavailable",
                        "type": "auth_error",
                    },
                    status_code=401,
                )
                decision = legacy_direct_chat_decision(details, retry_count=retry_count)
                return legacy_direct_chat_failure_result(
                    content="Baidu Wenxin access token unavailable",
                    details=details,
                    decision=decision,
                )
            url = "https://aip.baidubce.com/rpc/2.0/ai_custom/v1/wenxinworkshop/chat/" + session.model + "?access_token=" + access_token
            headers = {
                'Content-Type': 'application/json'
            }
            payload = {'messages': session.messages, 'system': self.prompt} if self.prompt_enabled else {'messages': session.messages}
            response = requests.request(
                "POST",
                url,
                headers=headers,
                data=json.dumps(payload),
                timeout=conf().get("request_timeout", 180),
            )
            if response.status_code != 200:
                details = self._response_error_details(response)
                decision = legacy_direct_chat_decision(details, retry_count=retry_count)
                if decision.should_retry:
                    run_legacy_direct_chat_retry_sleep(decision, model_retry_sleep)
                    return self.reply_text(
                        session,
                        retry_count + 1,
                        model_retry_sleep=model_retry_sleep,
                    )
                return legacy_direct_chat_failure_result(
                    content="Baidu Wenxin request failed",
                    details=details,
                    decision=decision,
                )

            response_text = self._load_response_json(response)
            logger.info(f"[BAIDU] response text={response_text}")
            if not isinstance(response_text, dict) or "result" not in response_text:
                details = self._body_error_details(response_text)
                decision = legacy_direct_chat_decision(details, retry_count=retry_count)
                if decision.should_retry:
                    run_legacy_direct_chat_retry_sleep(decision, model_retry_sleep)
                    return self.reply_text(
                        session,
                        retry_count + 1,
                        model_retry_sleep=model_retry_sleep,
                    )
                return legacy_direct_chat_failure_result(
                    content="Baidu Wenxin request failed",
                    details=details,
                    decision=decision,
                )
            res_content = response_text["result"]
            total_tokens = response_text["usage"]["total_tokens"]
            completion_tokens = response_text["usage"]["completion_tokens"]
            logger.info("[BAIDU] reply={}".format(res_content))
            return {
                "total_tokens": total_tokens,
                "completion_tokens": completion_tokens,
                "content": res_content,
            }
        except Exception as e:
            details = legacy_direct_chat_exception_details(e)
            decision = legacy_direct_chat_decision(details, retry_count=retry_count)
            if details.get("status_code") is None:
                logger.exception("[BAIDU] local adapter error: {}".format(e))
                self.sessions.clear_session(session.session_id)
            else:
                logger.warning("[BAIDU] provider/transport error: {}".format(e))
            if decision.should_retry:
                run_legacy_direct_chat_retry_sleep(decision, model_retry_sleep)
                return self.reply_text(
                    session,
                    retry_count + 1,
                    model_retry_sleep=model_retry_sleep,
                )
            return legacy_direct_chat_failure_result(
                content="Baidu Wenxin request failed",
                details=details,
                decision=decision,
            )

    def _load_response_json(self, response):
        json_loader = getattr(response, "json", None)
        if callable(json_loader):
            return json_loader()
        return json.loads(response.text)

    def _response_error_details(self, response):
        details = legacy_direct_chat_response_details(response)
        try:
            body = self._load_response_json(response)
        except Exception:
            return details
        if not isinstance(body, dict) or not (
            body.get("error_code") not in (None, "")
            or body.get("error_msg") not in (None, "")
        ):
            return details
        enriched = dict(body)
        enriched.setdefault("status_code", response.status_code)
        if details.get("retry_after") not in (None, ""):
            enriched.setdefault("retry_after", details.get("retry_after"))
        return self._body_error_details(enriched)

    def _body_error_details(self, body):
        if not isinstance(body, dict):
            return provider_error_response(
                {
                    "message": "Baidu Wenxin returned an unsupported response body",
                    "type": "invalid_response",
                },
                status_code=502,
            )

        message = (
            body.get("error_msg")
            or body.get("message")
            or body.get("msg")
            or body.get("error")
            or "Baidu Wenxin response missing result"
        )
        status_code = body.get("status_code") or body.get("http_code") or body.get("status")
        if status_code in (None, ""):
            status_code = self._infer_body_error_status(body, message)
        return provider_error_response(
            {
                "message": message,
                "code": body.get("error_code") or body.get("code") or "",
                "type": body.get("type") or body.get("error_type") or "provider_error",
                "status_code": status_code,
                "retry_after": body.get("retry_after"),
                "retry_after_seconds": body.get("retry_after_seconds"),
                "retry_after_ms": body.get("retry_after_ms"),
            },
            message=message,
            status_code=status_code,
        )

    def _infer_body_error_status(self, body, message):
        text = "{} {}".format(body.get("error_code", ""), message).lower()
        if ("rate" in text and "limit" in text) or "too many" in text or "qps" in text:
            return 429
        if "timeout" in text or "timed out" in text:
            return 408
        if "server" in text or "unavailable" in text or "busy" in text or "gateway" in text:
            return 500
        if "auth" in text or "access token" in text or "permission" in text:
            return 401
        return 400

    def get_access_token(self):
        """
        使用 AK，SK 生成鉴权签名（Access Token）
        :return: access_token，或是None(如果错误)
        """
        url = "https://aip.baidubce.com/oauth/2.0/token"
        params = {"grant_type": "client_credentials", "client_id": BAIDU_API_KEY, "client_secret": BAIDU_SECRET_KEY}
        response = requests.post(
            url,
            params=params,
            timeout=conf().get("request_timeout", 180),
        )
        if getattr(response, "status_code", 200) != 200:
            raise BaiduAccessTokenError(response)
        return str(response.json().get("access_token"))
