"""
飞书通道接入

支持两种事件接收模式:
1. webhook模式: 通过HTTP服务器接收事件(需要公网IP)
2. websocket模式: 通过长连接接收事件(本地开发友好)

通过配置项 feishu_event_mode 选择模式: "webhook" 或 "websocket"

@author Saboteur7
@Date 2023/11/19
"""

import json
import logging
import hashlib
import hmac
import os
import ssl
import threading
# -*- coding=utf-8 -*-
import uuid

import requests
import web

from bridge.context import Context
from bridge.context import ContextType
from bridge.reply import Reply, ReplyType
from channel.chat_channel import ChatChannel, check_prefix
from channel.feishu.feishu_message import FeishuMessage
from channel.feishu.feishu_progress_card import FeishuProgressState
from channel.feishu.feishu_scheduler_card import (
    build_scheduler_card,
    handle_scheduler_action,
    tasks_for_receivers,
)
from channel.feishu.feishu_static_card import (
    build_text_delivery,
    resolve_markdown_images,
    upload_public_image_to_feishu,
)
from common import utils
from common.expired_dict import ExpiredDict
from common.feishu_register_credentials import (
    extract_feishu_register_credentials,
    summarize_feishu_register_result_shape,
)
from common.feishu_runtime_readiness import lark_oapi_available, probe_lark_oapi
from common.ecorex_public_payload import mask_sensitive_text
from common.log import logger
from common.singleton import singleton
from config import conf

# Suppress verbose logs from Lark SDK
logging.getLogger("Lark").setLevel(logging.WARNING)

URL_VERIFICATION = "url_verification"

# Lazy-check lark_oapi without importing it at module level. The full import can
# be slow, so actual SDK import remains deferred to the path that needs it.
lark = None  # will be populated on first use via _ensure_lark_imported()
LARK_OAPI_DISCOVERY_GUIDANCE = (
    "legacy 飞书消息通道需要 lark-oapi SDK，当前运行时不会自动安装该旧依赖。"
    "飞书/Lark CLI、skill 和 connector 安装请统一先走内置 find skill / find-skill；"
    "真实 CLI 操作按需安装官方 @larksuite/cli，npmjs.org 超时后降级到 https://registry.npmmirror.com。"
)

FEISHU_LOG_HMAC_KEY = b"ecorex-feishu-log-v1"


def _feishu_log_text(value, max_chars: int = 500) -> str:
    return mask_sensitive_text(value, max_chars=max_chars)


def _feishu_log_presence(value) -> str:
    return "present" if str(value or "").strip() else "missing"


def _feishu_log_ref(value) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    digest = hmac.new(FEISHU_LOG_HMAC_KEY, raw.encode("utf-8", errors="replace"), hashlib.sha256).hexdigest()[:16]
    return f"hmac:{digest}"


def _feishu_event_log_summary(event: dict) -> dict:
    if not isinstance(event, dict):
        return {"shape": type(event).__name__}
    msg = event.get("message") if isinstance(event.get("message"), dict) else {}
    sender = event.get("sender") if isinstance(event.get("sender"), dict) else {}
    sender_id = sender.get("sender_id") if isinstance(sender.get("sender_id"), dict) else {}
    mentions = msg.get("mentions") if isinstance(msg.get("mentions"), list) else []
    return {
        "eventKeys": sorted(str(key) for key in event.keys())[:12],
        "messageType": str(msg.get("message_type") or ""),
        "chatType": str(msg.get("chat_type") or ""),
        "messageRef": _feishu_log_ref(msg.get("message_id")),
        "chatRef": _feishu_log_ref(msg.get("chat_id")),
        "senderRef": _feishu_log_ref(sender_id.get("open_id") or sender_id.get("user_id") or sender_id.get("union_id")),
        "mentionCount": len(mentions),
        "hasContent": bool(msg.get("content")),
    }


def _feishu_register_status_summary(info: dict) -> dict:
    if not isinstance(info, dict):
        return {"shape": type(info).__name__}
    return {
        "status": _feishu_log_text(info.get("status") or info.get("state") or ""),
        "code": _feishu_log_text(info.get("code") or ""),
        "message": _feishu_log_text(info.get("message") or info.get("msg") or info.get("error") or ""),
        "qrReady": bool(info.get("url") or info.get("qr_url") or info.get("qrcode") or info.get("qrCode")),
    }


def _feishu_api_response_log_summary(payload) -> dict:
    if not isinstance(payload, dict):
        return {"shape": type(payload).__name__}
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    return {
        "code": payload.get("code"),
        "msg": _feishu_log_text(payload.get("msg") or payload.get("message") or ""),
        "requestRef": _feishu_log_ref(payload.get("request_id") or payload.get("requestId")),
        "imageRef": _feishu_log_ref(data.get("image_key") or data.get("imageKey")),
        "fileRef": _feishu_log_ref(data.get("file_key") or data.get("fileKey")),
        "messageRef": _feishu_log_ref(data.get("message_id") or data.get("messageId")),
    }


def _ensure_lark_imported():
    """Import lark_oapi on first use (takes 4-10s due to 10k+ source files)."""
    global lark
    if lark is None:
        import lark_oapi as _lark
        lark = _lark
    return lark


def _print_qr_to_terminal(qr_url: str):
    """Record QR readiness without leaking the one-time QR payload."""
    logger.info(
        "[FeiShu] One-click 飞书应用创建二维码已生成：qr_ready=%s, expires_in_seconds=%s",
        bool(qr_url),
        600,
    )


def _persist_feishu_credentials(app_id: str, app_secret: str) -> bool:
    """Write feishu_app_id / feishu_app_secret + ensure feishu in channel_type into config.json.

    Returns True on success, False on failure (e.g. config.json missing or unwritable).
    """
    try:
        config_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
            "config.json",
        )
        if os.path.exists(config_path):
            with open(config_path, "r", encoding="utf-8") as f:
                file_cfg = json.load(f)
        else:
            file_cfg = {}

        file_cfg["feishu_app_id"] = app_id
        file_cfg["feishu_app_secret"] = app_secret

        # 保证 channel_type 中包含 feishu（用户可能纯通过 CLI 启动单通道）
        ch_type = file_cfg.get("channel_type", conf().get("channel_type", "")) or ""
        existing = [s.strip() for s in ch_type.split(",") if s.strip()]
        if "feishu" not in existing:
            existing.append("feishu")
            file_cfg["channel_type"] = ",".join(existing)

        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(file_cfg, f, indent=4, ensure_ascii=False)

        # 同步到内存中的 conf()，让本次启动直接生效
        conf()["feishu_app_id"] = app_id
        conf()["feishu_app_secret"] = app_secret
        if "channel_type" in file_cfg:
            conf()["channel_type"] = file_cfg["channel_type"]

        try:
            os.chmod(config_path, 0o600)
        except Exception:
            pass
        return True
    except Exception as e:
        logger.error(
            "[FeiShu] Failed to persist credentials to config.json, config_ref=%s, error_type=%s",
            _feishu_log_ref(config_path),
            type(e).__name__,
        )
        return False


def _register_via_qr_in_terminal() -> bool:
    """CLI-side one-click app creation via lark_oapi.register_app.

    Blocks the calling thread (typically the channel startup thread) until the user
    finishes scanning, the QR code expires, or registration is cancelled.

    Returns True if credentials were obtained AND persisted; False otherwise.
    The caller should fall back to the original "missing credentials" error in that case.
    """
    sdk_probe = probe_lark_oapi()
    if not sdk_probe.get("sdkPresent"):
        logger.error(
            "[FeiShu] 缺少 feishu_app_id / feishu_app_secret。"
            "未安装 lark-oapi SDK，无法在终端发起扫码创建。"
            f"{LARK_OAPI_DISCOVERY_GUIDANCE} 或手动在 config.json 中填入凭据。"
        )
        return False

    try:
        lark_mod = _ensure_lark_imported()
    except Exception as e:
        logger.error("[FeiShu] Import lark_oapi failed, error_type=%s", type(e).__name__)
        return False

    # register_app 是 lark-oapi 1.5.5 才引入的能力，旧版本调用会得到难以理解的
    # AttributeError。提前显式检查，给出明确的升级提示。
    if not hasattr(lark_mod, "register_app"):
        try:
            from importlib.metadata import version as _pkg_version
            installed = _pkg_version("lark-oapi")
        except Exception:
            installed = "unknown"
        logger.error(
            f"[FeiShu] 当前 lark-oapi 版本 ({installed}) 不支持一键创建应用，需要 >= 1.5.5。"
            f"{LARK_OAPI_DISCOVERY_GUIDANCE} 或手动在 config.json 中填入凭据。"
        )
        return False

    logger.info("[FeiShu] 检测到尚未配置 feishu_app_id / feishu_app_secret，"
                "正在向飞书申请一键创建应用...")

    def _on_qr(info):
        url = info.get("url", "")
        if url:
            _print_qr_to_terminal(url)

    def _on_status(info):
        # 过滤 polling 心跳（每 5 秒一次），保留 slow_down / domain_switched 等
        status = info.get("status")
        if status == "polling":
            return
        logger.info("[FeiShu] register_app status: %s", _feishu_register_status_summary(info))

    try:
        result = lark_mod.register_app(
            on_qr_code=_on_qr,
            on_status_change=_on_status,
            source="cowagent",
        )
    except Exception as e:
        err_cls = e.__class__.__name__
        if "Expired" in err_cls:
            logger.error("[FeiShu] 二维码已过期，请重启程序后重试。")
        elif "Denied" in err_cls:
            logger.error("[FeiShu] 已取消授权。")
        else:
            logger.error("[FeiShu] 一键创建失败, error_type=%s", type(e).__name__)
        return False

    app_id, app_secret = extract_feishu_register_credentials(result)
    if not app_id or not app_secret:
        logger.error(
            "[FeiShu] 创建结果缺少 app_id/app_secret，无法继续。返回字段形态: %s",
            summarize_feishu_register_result_shape(result),
        )
        return False

    if not _persist_feishu_credentials(app_id, app_secret):
        logger.error(
            "[FeiShu] 应用创建成功但写入 config.json 失败；"
            "请在飞书开发者后台重新查看并手动写入凭据。"
            "app_id=%s, app_secret=%s",
            _feishu_log_text(app_id),
            _feishu_log_presence(app_secret),
        )
        return False

    logger.info("[FeiShu] 应用创建成功，凭据已写入 config.json (app_id=%s)。", _feishu_log_text(app_id))
    return True


@singleton
class FeiShuChanel(ChatChannel):
    feishu_app_id = conf().get('feishu_app_id')
    feishu_app_secret = conf().get('feishu_app_secret')
    feishu_token = conf().get('feishu_token')
    feishu_event_mode = conf().get('feishu_event_mode', 'websocket')  # webhook 或 websocket
    # 覆盖父类默认值 [ReplyType.VOICE, ReplyType.IMAGE]。
    # 飞书原生支持发送音频（opus 格式，通过文件上传接口）和图片，
    # 所有回复类型均已处理，置为空列表以启用语音和图片回复。
    NOT_SUPPORT_REPLYTYPE = []

    def __init__(self):
        super().__init__()
        # 历史消息id暂存，用于幂等控制
        self.receivedMsgs = ExpiredDict(60 * 60 * 7.1)
        self._message_sessions = ExpiredDict(60 * 60 * 7.1)
        self._http_server = None
        self._ws_client = None
        self._ws_thread = None
        self._bot_open_id = None  # cached bot open_id for @-mention matching
        logger.debug(
            "[FeiShu] config status app_id=%s, app_secret=%s, verification_token=%s, event_mode=%s",
            _feishu_log_text(self.feishu_app_id),
            _feishu_log_presence(self.feishu_app_secret),
            _feishu_log_presence(self.feishu_token),
            _feishu_log_text(self.feishu_event_mode),
        )
        # 无需群校验和前缀
        conf()["group_name_white_list"] = ["ALL_GROUP"]
        conf()["single_chat_prefix"] = [""]

        # 验证配置
        if self.feishu_event_mode == 'websocket' and not lark_oapi_available():
            logger.error(f"[FeiShu] websocket mode requires lark_oapi. {LARK_OAPI_DISCOVERY_GUIDANCE}")
            raise Exception("lark_oapi not installed")

    def startup(self):
        self.feishu_app_id = conf().get('feishu_app_id')
        self.feishu_app_secret = conf().get('feishu_app_secret')
        self.feishu_token = conf().get('feishu_token')
        self.feishu_event_mode = conf().get('feishu_event_mode', 'websocket')

        # 命令行启动场景：缺少凭据时尝试通过 lark.register_app 在终端弹二维码
        # 引导用户扫码创建应用。Web 控制台启动同样会走到这里，但控制台用户通常
        # 已经通过 /api/feishu/register 完成了创建并写回 config.json。
        if not self.feishu_app_id or not self.feishu_app_secret:
            if _register_via_qr_in_terminal():
                self.feishu_app_id = conf().get('feishu_app_id')
                self.feishu_app_secret = conf().get('feishu_app_secret')
            else:
                err = "[FeiShu] feishu_app_id 与 feishu_app_secret 缺失，无法启动通道"
                logger.error(err)
                self.report_startup_error(err)
                return

        self._fetch_bot_open_id()
        if self.feishu_event_mode == 'websocket':
            self._startup_websocket()
        else:
            self._startup_webhook()

    def _fetch_bot_open_id(self):
        """Fetch the bot's own open_id via API so we can match @-mentions without feishu_bot_name."""
        try:
            access_token = self.fetch_access_token()
            if not access_token:
                logger.warning("[FeiShu] Cannot fetch bot info: no access_token")
                return
            headers = {"Authorization": "Bearer " + access_token}
            resp = requests.get("https://open.feishu.cn/open-apis/bot/v3/info/", headers=headers, timeout=5)
            if resp.status_code == 200:
                data = resp.json()
                if data.get("code") == 0:
                    self._bot_open_id = data.get("bot", {}).get("open_id")
                    logger.info("[FeiShu] Bot open_id fetched: open_ref=%s", _feishu_log_ref(self._bot_open_id))
                else:
                    logger.warning(
                        "[FeiShu] Fetch bot info failed: code=%s, msg=%s",
                        data.get("code"),
                        _feishu_log_text(data.get("msg")),
                    )
        except Exception as e:
            logger.warning("[FeiShu] Fetch bot open_id error, error_type=%s", type(e).__name__)

    def stop(self):
        import ctypes
        logger.info("[FeiShu] stop() called")
        ws_client = self._ws_client
        self._ws_client = None
        ws_thread = self._ws_thread
        self._ws_thread = None
        # Interrupt the ws thread first so its blocking start() unblocks
        if ws_thread and ws_thread.is_alive():
            try:
                tid = ws_thread.ident
                if tid:
                    res = ctypes.pythonapi.PyThreadState_SetAsyncExc(
                        ctypes.c_ulong(tid), ctypes.py_object(SystemExit)
                    )
                    if res == 1:
                        logger.info("[FeiShu] Interrupted ws thread via ctypes")
                    elif res > 1:
                        ctypes.pythonapi.PyThreadState_SetAsyncExc(ctypes.c_ulong(tid), None)
            except Exception as e:
                logger.warning("[FeiShu] Error interrupting ws thread, error_type=%s", type(e).__name__)
        # lark.ws.Client has no stop() method; thread interruption above is sufficient
        if self._http_server:
            try:
                self._http_server.stop()
                logger.info("[FeiShu] HTTP server stopped")
            except Exception as e:
                logger.warning("[FeiShu] Error stopping HTTP server, error_type=%s", type(e).__name__)
            self._http_server = None
        logger.info("[FeiShu] stop() completed")

    def _startup_webhook(self):
        """启动HTTP服务器接收事件(webhook模式)"""
        logger.debug("[FeiShu] Starting in webhook mode...")
        urls = (
            '/', 'channel.feishu.feishu_channel.FeishuController'
        )
        app = web.application(urls, globals(), autoreload=False)
        port = conf().get("feishu_port", 9891)
        func = web.httpserver.StaticMiddleware(app.wsgifunc())
        func = web.httpserver.LogMiddleware(func)
        server = web.httpserver.WSGIServer(("0.0.0.0", port), func)
        self._http_server = server
        try:
            server.start()
        except (KeyboardInterrupt, SystemExit):
            server.stop()

    def _startup_websocket(self):
        """启动长连接接收事件(websocket模式)"""
        _ensure_lark_imported()
        from lark_oapi.event.callback.model.p2_card_action_trigger import (
            P2CardActionTriggerResponse,
        )
        logger.debug("[FeiShu] Starting in websocket mode...")

        # 创建事件处理器
        def handle_message_event(data: lark.im.v1.P2ImMessageReceiveV1) -> None:
            """处理接收消息事件 v2.0"""
            try:
                event_dict = json.loads(lark.JSON.marshal(data))
                event = event_dict.get("event", {})
                msg = event.get("message", {})

                # Skip group messages that don't @-mention the bot (reduce log noise)
                if msg.get("chat_type") == "group" and not msg.get("mentions") and msg.get("message_type") == "text":
                    return

                logger.debug("[FeiShu] websocket receive event summary: %s", _feishu_event_log_summary(event))

                # 处理消息
                self._handle_message_event(event)

            except Exception as e:
                logger.error("[FeiShu] websocket handle message error, error_type=%s", type(e).__name__)

        def handle_message_recalled_event(data: lark.im.v1.P2ImMessageRecalledV1) -> None:
            try:
                event_dict = json.loads(lark.JSON.marshal(data))
                self._handle_message_recalled_event(event_dict.get("event", {}))
            except Exception as e:
                logger.error("[FeiShu] websocket recall error, error_type=%s", type(e).__name__)

        def handle_card_action(data):
            try:
                event_dict = json.loads(lark.JSON.marshal(data))
                return P2CardActionTriggerResponse(
                    self._handle_card_action_event(event_dict.get("event", {}))
                )
            except Exception as e:
                logger.error("[FeiShu] websocket card action error, error_type=%s", type(e).__name__)
                return P2CardActionTriggerResponse(
                    {"toast": {"type": "error", "content": "任务更新失败"}}
                )

        # 构建事件分发器
        event_handler = (
            lark.EventDispatcherHandler.builder("", "")
            .register_p2_im_message_receive_v1(handle_message_event)
            .register_p2_im_message_recalled_v1(handle_message_recalled_event)
            .register_p2_card_action_trigger(handle_card_action)
            .build()
        )

        def start_client_with_retry():
            """Run ws client in this thread with its own event loop to avoid conflicts."""
            import asyncio
            import ssl as ssl_module
            original_create_default_context = ssl_module.create_default_context

            def create_unverified_context(*args, **kwargs):
                context = original_create_default_context(*args, **kwargs)
                context.check_hostname = False
                context.verify_mode = ssl.CERT_NONE
                return context

            # lark_oapi.ws.client captures the event loop at module-import time as a module-
            # level global variable.  When a previous ws thread is force-killed via ctypes its
            # loop may still be marked as "running", which causes the next ws_client.start()
            # call (in this new thread) to raise "This event loop is already running".
            # Fix: replace the module-level loop with a brand-new, idle loop before starting.
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                import lark_oapi.ws.client as _lark_ws_client_mod
                _lark_ws_client_mod.loop = loop
            except Exception:
                pass

            startup_error = None
            for attempt in range(2):
                try:
                    if attempt == 1:
                        logger.warning("[FeiShu] Retrying with SSL verification disabled...")
                        ssl_module.create_default_context = create_unverified_context
                        ssl_module._create_unverified_context = create_unverified_context

                    ws_client = lark.ws.Client(
                        self.feishu_app_id,
                        self.feishu_app_secret,
                        event_handler=event_handler,
                        log_level=lark.LogLevel.WARNING
                    )
                    self._ws_client = ws_client
                    logger.debug("[FeiShu] Websocket client starting...")
                    ws_client.start()
                    break

                except (SystemExit, KeyboardInterrupt):
                    logger.info("[FeiShu] Websocket thread received stop signal")
                    break
                except Exception as e:
                    error_msg = str(e)
                    is_ssl_error = ("CERTIFICATE_VERIFY_FAILED" in error_msg
                                    or "certificate verify failed" in error_msg.lower())
                    if is_ssl_error and attempt == 0:
                        logger.warning("[FeiShu] SSL error during websocket startup, error_type=%s, retrying...", type(e).__name__)
                        continue
                    logger.error("[FeiShu] Websocket client error, error_type=%s", type(e).__name__)
                    startup_error = error_msg
                    ssl_module.create_default_context = original_create_default_context
                    break
            if startup_error:
                self.report_startup_error(startup_error)
            try:
                loop.close()
            except Exception:
                pass
            logger.info("[FeiShu] Websocket thread exited")

        ws_thread = threading.Thread(target=start_client_with_retry, daemon=True)
        self._ws_thread = ws_thread
        ws_thread.start()
        logger.info("[FeiShu] ✅ Websocket thread started, ready to receive messages")
        ws_thread.join()

    def _is_mention_bot(self, mentions: list) -> bool:
        """Check whether any mention in the list refers to this bot.

        Priority:
        1. Match by open_id (obtained from /bot/v3/info at startup, no config needed)
        2. Fallback to feishu_bot_name config for backward compatibility
        3. If neither is available, assume the first mention is the bot (Feishu only
           delivers group messages that @-mention the bot, so this is usually correct)
        """
        if self._bot_open_id:
            return any(
                m.get("id", {}).get("open_id") == self._bot_open_id
                for m in mentions
            )
        bot_name = conf().get("feishu_bot_name")
        if bot_name:
            return any(m.get("name") == bot_name for m in mentions)
        # Feishu event subscription only delivers messages that @-mention the bot,
        # so reaching here means the bot was indeed mentioned.
        return True

    def _get_scheduler_task_store(self):
        """Reuse the live scheduler store, falling back to its standard path."""
        from agent.tools.scheduler.integration import get_task_store

        task_store = get_task_store()
        if task_store is not None:
            return task_store

        from agent.tools.scheduler.task_store import TaskStore

        workspace_root = utils.expand_path(conf().get("agent_workspace", "~/cow"))
        return TaskStore(os.path.join(workspace_root, "scheduler", "tasks.json"))

    def _send_scheduler_card(self, feishu_msg, is_group: bool, receive_id_type: str) -> bool:
        task_store = self._get_scheduler_task_store()
        tasks = tasks_for_receivers(task_store.list_tasks(), {feishu_msg.other_user_id})
        content = json.dumps(build_scheduler_card(tasks), ensure_ascii=False)
        headers = {
            "Authorization": "Bearer " + feishu_msg.access_token,
            "Content-Type": "application/json",
        }
        if is_group and feishu_msg.msg_id:
            url = f"https://open.feishu.cn/open-apis/im/v1/messages/{feishu_msg.msg_id}/reply"
            response = requests.post(
                url,
                headers=headers,
                json={"msg_type": "interactive", "content": content},
                timeout=(5, 10),
            )
        else:
            url = "https://open.feishu.cn/open-apis/im/v1/messages"
            response = requests.post(
                url,
                headers=headers,
                params={"receive_id_type": receive_id_type},
                json={
                    "receive_id": feishu_msg.other_user_id,
                    "msg_type": "interactive",
                    "content": content,
                },
                timeout=(5, 10),
            )
        result = response.json()
        if result.get("code") == 0:
            logger.info("[FeiShu] scheduler card sent")
            return True
        logger.error("[FeiShu] scheduler card failed, summary=%s", _feishu_api_response_log_summary(result))
        return False

    def _handle_card_action_event(self, event: dict) -> dict:
        action = event.get("action") or {}
        value = action.get("value") or {}
        if value.get("cowagent") != "scheduler":
            return {}

        callback_context = event.get("context") or {}
        operator = event.get("operator") or {}
        callback_receivers = {
            receiver
            for receiver in (callback_context.get("open_chat_id"), operator.get("open_id"))
            if receiver
        }
        target_receiver = value.get("receiver")
        allowed_receivers = (
            {target_receiver}
            if target_receiver and target_receiver in callback_receivers
            else set()
        )
        response = handle_scheduler_action(
            value,
            self._get_scheduler_task_store(),
            allowed_receivers,
        )
        logger.info(
            "[FeiShu] scheduler action=%s task_ref=%s",
            _feishu_log_text(value.get("action")),
            _feishu_log_ref(value.get("task_id")),
        )
        return response

    def _handle_message_recalled_event(self, event: dict):
        message_id = event.get("message_id")
        if not message_id:
            logger.warning("[FeiShu] invalid message recall event")
            return 0, False
        session_id = self._message_sessions.get(message_id)
        if not session_id:
            logger.info("[FeiShu] ignored unknown recall, message_ref=%s", _feishu_log_ref(message_id))
            return 0, False
        result = self.cancel_message(session_id, message_id)
        self._message_sessions.pop(message_id, None)
        logger.info(
            "[FeiShu] recall cancelled, message_ref=%s session_ref=%s queued=%s active=%s",
            _feishu_log_ref(message_id),
            _feishu_log_ref(session_id),
            result[0],
            result[1],
        )
        return result

    def _handle_message_event(self, event: dict):
        """
        处理消息事件的核心逻辑
        webhook和websocket模式共用此方法
        """
        if not event.get("message") or not event.get("sender"):
            logger.warning("[FeiShu] invalid message, event_summary=%s", _feishu_event_log_summary(event))
            return

        msg = event.get("message")

        # 幂等判断
        msg_id = msg.get("message_id")
        if self.receivedMsgs.get(msg_id):
            logger.warning("[FeiShu] repeat msg filtered, message_ref=%s", _feishu_log_ref(msg_id))
            return
        self.receivedMsgs[msg_id] = True

        # Filter out stale messages from before channel startup (offline backlog)
        import time as _time
        create_time_ms = msg.get("create_time")
        if create_time_ms:
            msg_age_s = _time.time() - int(create_time_ms) / 1000
            if msg_age_s > 60:
                logger.warning("[FeiShu] stale msg filtered (age=%.0fs), message_ref=%s", msg_age_s, _feishu_log_ref(msg_id))
                return

        is_group = False
        chat_type = msg.get("chat_type")

        if chat_type == "group":
            if not msg.get("mentions") and msg.get("message_type") == "text":
                # 群聊中未@不响应
                return
            if msg.get("mentions") and msg.get("message_type") == "text":
                if not self._is_mention_bot(msg.get("mentions")):
                    return
            # 群聊
            is_group = True
            receive_id_type = "chat_id"
        elif chat_type == "p2p":
            receive_id_type = "open_id"
        else:
            logger.warning("[FeiShu] message ignore")
            return

        # 构造飞书消息对象
        feishu_msg = FeishuMessage(event, is_group=is_group, access_token=self.fetch_access_token())
        if not feishu_msg:
            return

        if feishu_msg.ctype == ContextType.TEXT and feishu_msg.content.strip().lower() == "/tasks":
            if not self._send_scheduler_card(feishu_msg, is_group, receive_id_type):
                logger.warning("[FeiShu] /tasks card delivery failed")
            return

        # 处理文件缓存逻辑
        from channel.file_cache import get_file_cache
        file_cache = get_file_cache()

        # 获取 session_id（用于缓存关联）
        if is_group:
            if conf().get("group_shared_session", True):
                session_id = msg.get("chat_id")  # 群共享会话
            else:
                session_id = feishu_msg.from_user_id + "_" + msg.get("chat_id")
        else:
            session_id = feishu_msg.from_user_id

        # 如果是单张图片消息，缓存起来
        if feishu_msg.ctype == ContextType.IMAGE:
            if hasattr(feishu_msg, 'image_path') and feishu_msg.image_path:
                file_cache.add(session_id, feishu_msg.image_path, file_type='image')
                logger.info(
                    "[FeiShu] Image cached for session_ref=%s, file_ref=%s, waiting for user query...",
                    _feishu_log_ref(session_id),
                    _feishu_log_ref(feishu_msg.image_path),
                )
            # 单张图片不直接处理，等待用户提问
            return

        # 如果是文件消息，触发实际下载并缓存，等待用户后续提问时一并带上。
        # 与 wecom_bot 行为对齐：发文件后静默缓存（飞书客户端会显示"已读"），
        # 用户下一条文本消息会自动 attach 上文件路径给 agent。
        if feishu_msg.ctype == ContextType.FILE:
            try:
                feishu_msg.prepare()
                # prepare 通过 _prepared 标记保证幂等，重复调用安全
                if not os.path.exists(feishu_msg.content):
                    raise FileNotFoundError(feishu_msg.content)
            except Exception as e:
                logger.warning(
                    "[FeiShu] prepare file failed: file_ref=%s, error_type=%s",
                    _feishu_log_ref(getattr(feishu_msg, "content", "")),
                    e.__class__.__name__,
                )
                # 文件下载失败时主动通知用户，避免静默丢失
                try:
                    err_reply = Reply(ReplyType.TEXT, "文件下载失败，请重新发送。")
                    self._send(err_reply, self._compose_context(
                        ContextType.TEXT, "",
                        isgroup=is_group, msg=feishu_msg,
                        receive_id_type=receive_id_type, no_need_at=True,
                    ))
                except Exception:
                    pass
                return
            file_cache.add(session_id, feishu_msg.content, file_type='file')
            logger.info(
                "[FeiShu] File cached for session_ref=%s, file_ref=%s",
                _feishu_log_ref(session_id),
                _feishu_log_ref(feishu_msg.content),
            )
            return

        # 如果是文本消息，检查是否有缓存的文件
        if feishu_msg.ctype == ContextType.TEXT:
            cached_files = file_cache.get(session_id)
            if cached_files:
                # 将缓存的文件附加到文本消息中
                file_refs = []
                for file_info in cached_files:
                    file_path = file_info['path']
                    file_type = file_info['type']
                    if file_type == 'image':
                        file_refs.append(f"[图片: {file_path}]")
                    elif file_type == 'video':
                        file_refs.append(f"[视频: {file_path}]")
                    else:
                        file_refs.append(f"[文件: {file_path}]")

                feishu_msg.content = feishu_msg.content + "\n" + "\n".join(file_refs)
                logger.info(f"[FeiShu] Attached {len(cached_files)} cached file(s) to user query")
                # 清除缓存
                file_cache.clear(session_id)

        context = self._compose_context(
            feishu_msg.ctype,
            feishu_msg.content_with_quote(),
            isgroup=is_group,
            msg=feishu_msg,
            receive_id_type=receive_id_type,
            no_need_at=True
        )
        if context:
            context["request_id"] = msg_id
            self._message_sessions[msg_id] = context["session_id"]
            # 流式回复模式：向 context 注入 on_event 回调，agent 每产出一段文字时会调用它。
            # 回调内部先发送一条占位消息获取 message_id，之后通过 PATCH 接口原地更新内容，
            # 实现打字机效果。回调结束时设置 context["feishu_streamed"]=True，
            # 让 send() 跳过重复发送，避免最终完整回复再被重复投递一次。
            # 默认开启流式打字机回复。需机器人开通 cardkit:card:write 权限且飞书客户端 7.20+，
            # 任意环节失败会自动降级为非流式文本回复。
            if conf().get("feishu_stream_reply", True):
                context["on_event"] = self._make_feishu_stream_callback(context, feishu_msg.access_token)
            self.produce(context)
        logger.debug(
            "[FeiShu] query received: type=%s, content_chars=%s",
            feishu_msg.ctype,
            len(str(feishu_msg.content or "")),
        )

    def send(self, reply: Reply, context: Context):
        # 如果文本回复已通过流式传输发送，则跳过重复发送
        if reply.type == ReplyType.TEXT and context.get("feishu_streamed"):
            logger.debug("[FeiShu] streaming already delivered text reply, skipping send()")
            return
        msg = context.get("msg")
        is_group = context["isgroup"]
        if msg:
            access_token = msg.access_token
        else:
            access_token = self.fetch_access_token()
        headers = {
            "Authorization": "Bearer " + access_token,
            "Content-Type": "application/json",
        }
        msg_type = "text"
        logger.debug(
            "[FeiShu] sending reply, type=%s, content_chars=%s",
            context.type,
            len(str(reply.content or "")),
        )
        reply_content = reply.content
        content_key = "text"
        prepared_content_json = None
        if reply.type == ReplyType.TEXT:
            delivery_text = resolve_markdown_images(
                reply.content,
                lambda url: upload_public_image_to_feishu(url, access_token),
            )
            msg_type, prepared_content_json = build_text_delivery(delivery_text)
        elif reply.type == ReplyType.IMAGE_URL:
            # 图片上传
            reply_content = self._upload_image_url(reply.content, access_token)
            if not reply_content:
                logger.warning("[FeiShu] upload image failed")
                return
            msg_type = "image"
            content_key = "image_key"
        elif reply.type == ReplyType.FILE:
            # 如果有附加的文本内容，先发送文本
            if hasattr(reply, 'text_content') and reply.text_content:
                logger.info("[FeiShu] Sending text before file: text_chars=%s", len(str(reply.text_content or "")))
                text_reply = Reply(ReplyType.TEXT, reply.text_content)
                self._send(text_reply, context)
                import time
                time.sleep(0.3)  # 短暂延迟，确保文本先到达

            # 判断是否为视频文件
            file_path = reply.content
            if file_path.startswith("file://"):
                file_path = file_path[7:]

            is_video = file_path.lower().endswith(('.mp4', '.avi', '.mov', '.wmv', '.flv'))

            if is_video:
                # 视频上传（包含duration信息）
                upload_data = self._upload_video_url(reply.content, access_token)
                if not upload_data or not upload_data.get('file_key'):
                    logger.warning("[FeiShu] upload video failed")
                    return

                # 视频使用 media 类型（根据官方文档）
                # 错误码 230055 说明：上传 mp4 时必须使用 msg_type="media"
                msg_type = "media"
                reply_content = upload_data  # 完整的上传响应数据（包含file_key和duration）
                logger.info(
                    "[FeiShu] Sending video: file_ref=%s, duration=%sms",
                    _feishu_log_ref(upload_data.get("file_key")),
                    upload_data.get("duration"),
                )
                content_key = None  # 直接序列化整个对象
            else:
                # 其他文件使用 file 类型
                file_key = self._upload_file_url(reply.content, access_token)
                if not file_key:
                    logger.warning("[FeiShu] upload file failed")
                    return
                reply_content = file_key
                msg_type = "file"
                content_key = "file_key"

        elif reply.type == ReplyType.VOICE:
            # 语音回复：上传音频文件到飞书，然后发送 audio 类型消息
            file_key = self._upload_audio(reply.content, access_token)
            if not file_key:
                logger.warning("[FeiShu] upload audio failed")
                return
            reply_content = file_key
            msg_type = "audio"
            content_key = "file_key"

        # Check if we can reply to an existing message (need msg_id)
        can_reply = is_group and msg and hasattr(msg, 'msg_id') and msg.msg_id

        # Build content JSON
        content_json = prepared_content_json or (
            json.dumps(reply_content, ensure_ascii=False)
            if content_key is None
            else json.dumps({content_key: reply_content}, ensure_ascii=False)
        )
        logger.debug("[FeiShu] Sending message: msg_type=%s, content_chars=%s", msg_type, len(content_json))

        if can_reply:
            # 群聊中回复已有消息
            url = f"https://open.feishu.cn/open-apis/im/v1/messages/{msg.msg_id}/reply"
            data = {
                "msg_type": msg_type,
                "content": content_json
            }
            res = requests.post(url=url, headers=headers, json=data, timeout=(5, 10))
        else:
            # 发送新消息（私聊或群聊中无msg_id的情况，如定时任务）
            url = "https://open.feishu.cn/open-apis/im/v1/messages"
            params = {"receive_id_type": context.get("receive_id_type") or "open_id"}
            data = {
                "receive_id": context.get("receiver"),
                "msg_type": msg_type,
                "content": content_json
            }
            res = requests.post(url=url, headers=headers, params=params, json=data, timeout=(5, 10))
        res = res.json()
        if res.get("code") == 0:
            logger.info(f"[FeiShu] send message success")
        elif msg_type == "interactive" and reply.type == ReplyType.TEXT:
            logger.warning(
                "[FeiShu] Markdown card failed, falling back to text, summary=%s",
                _feishu_api_response_log_summary(res),
            )
            fallback_data = {
                "msg_type": "text",
                "content": json.dumps({"text": reply.content}, ensure_ascii=False),
            }
            if can_reply:
                fallback_res = requests.post(
                    url=url,
                    headers=headers,
                    json=fallback_data,
                    timeout=(5, 10),
                )
            else:
                fallback_data["receive_id"] = context.get("receiver")
                fallback_res = requests.post(
                    url=url,
                    headers=headers,
                    params=params,
                    json=fallback_data,
                    timeout=(5, 10),
                )
            fallback_body = fallback_res.json()
            if fallback_body.get("code") == 0:
                logger.info("[FeiShu] text fallback sent successfully")
            else:
                logger.error(
                    "[FeiShu] text fallback failed, summary=%s",
                    _feishu_api_response_log_summary(fallback_body),
                )
        else:
            logger.error(
                "[FeiShu] send message failed, code=%s, msg=%s",
                res.get("code"),
                _feishu_log_text(res.get("msg")),
            )

    def _make_feishu_stream_callback(self, context, access_token):
        """Route to detailed or plain streaming callback based on config.

        feishu_detailed_card 默认开启：普通对话使用带状态头、
        思考/工具面板与耗时的详细卡片。关闭后回退到原有的打字机文本卡片
        (_make_feishu_stream_callback_plain)。
        """
        if conf().get("feishu_detailed_card", True):
            return self._make_feishu_stream_callback_progress(context, access_token)
        return self._make_feishu_stream_callback_plain(context, access_token)

    def _make_feishu_stream_callback_progress(self, context, access_token):
        """
        基于飞书官方"流式更新卡片"API 实现打字机回复。

        流程：
        1. agent_start → POST /cardkit/v1/cards 创建带 streaming_mode 的状态卡片，
           随后用 POST /im/v1/messages（或 reply）以 card_id 把卡片发出去
        2. 后续 message_update → PUT /cardkit/v1/cards/{id}/elements/{eid}/content
           传入"当前轮"的全量文本，飞书平台自动计算增量并以打字机效果上屏
           （流式模式下不受 10 QPS 限制）
        3. message_end（本轮触发工具调用）→ 原地刷新 Reasoning / Tools 面板，
           后续 turn 继续复用同一张卡片
        4. agent_end → 用 final_response、最终状态和耗时整卡更新，关闭 streaming_mode，
           标记 context["feishu_streamed"]=True 让 chat_channel 跳过普通 send()

        前提条件：
        - 机器人已开通 cardkit:card:write 权限
        - 飞书客户端 7.20+

        失败降级：
        - 创建卡片实体失败（缺权限、网络等）→ 不设置 feishu_streamed 标记，让 chat_channel
          走普通文本回复路径，用户收到完整回复但无打字机效果，并打 warning 日志
        """
        # 共享状态（受 lock 保护）。一个 agent run 始终复用同一张卡片；
        # reasoning、tools、最终正文和状态头均由 progress_state 统一渲染。
        progress_state = FeishuProgressState()
        card_id = [None]
        message_id = [None]
        # 占位发送是同步进行的，但用一个 in-flight 标记防止并发的多条 message_update
        # 事件各自触发一次创建+发送，导致发出多张卡片。
        init_in_flight = [False]
        # 一旦初始化失败就长期标记为 disabled，本次回复不再尝试任何流式调用
        disabled = [False]
        lock = threading.Lock()

        # ---- 异步推送队列 ----------------------------------------------------
        # 同步 requests.put 单次 100~300ms，会阻塞 LLM stream 线程读下一个 chunk。
        # 把推送丢给独立 worker 线程消费 queue，回调本身只做内存追加，立即返回。
        # 队列里只放"最新累积文本"的快照；worker 用 deduplication 避免重复推同一个
        # 内容（高频 chunk 场景下队列会堆积，只推最后一个就够了）。
        import queue as _queue
        push_queue: "_queue.Queue[str | None]" = _queue.Queue()

        def _push_worker():
            while True:
                snapshot = push_queue.get()
                if snapshot is None:
                    push_queue.task_done()
                    return
                # 合并队列中已堆积的快照：只推最后一个，省 PUT 次数同时降低延迟
                merged_count = 1
                stop = False
                while True:
                    try:
                        nxt = push_queue.get_nowait()
                    except _queue.Empty:
                        break
                    merged_count += 1
                    if nxt is None:
                        stop = True
                        break
                    snapshot = nxt
                try:
                    _stream_update_text(snapshot)
                finally:
                    for _ in range(merged_count):
                        push_queue.task_done()
                if stop:
                    return

        push_thread = threading.Thread(target=_push_worker, daemon=True, name="feishu-stream-push")
        push_thread.start()

        def _drain_push_queue():
            """等当前队列里所有 PUT 都完成。message_end/agent_end 在做最终定型前必须 drain，
            否则 worker 里堆积的旧快照可能在 final_text PUT 之后到达，把最终内容覆盖掉。"""
            try:
                push_queue.join()
            except Exception:
                pass

        msg = context.get("msg")
        is_group = context.get("isgroup", False)
        receiver = context.get("receiver")
        receive_id_type = context.get("receive_id_type", "open_id")
        headers = {
            "Authorization": "Bearer " + access_token,
            "Content-Type": "application/json; charset=utf-8",
        }
        # 卡片中富文本组件的 element_id，后续所有 PUT 流式更新都打到这个组件
        ELEMENT_ID = "stream_md"
        # 操作序号，每次 PUT 必须严格递增（飞书要求）
        sequence = [0]

        def _next_sequence():
            with lock:
                sequence[0] += 1
                return sequence[0]

        def _build_card_json():
            """Build the initial streaming Card 2.0 payload."""
            with lock:
                card = progress_state.build_card(streaming=True)
            return json.dumps(card, ensure_ascii=False)

        def _create_and_send_card():
            """同步执行：创建卡片实体 → 发送消息。任意一步失败则 disabled=True 触发降级"""
            try:
                # 步骤 1: 创建卡片实体
                create_url = "https://open.feishu.cn/open-apis/cardkit/v1/cards"
                create_body = {"type": "card_json", "data": _build_card_json()}
                res = requests.post(
                    create_url, headers=headers, json=create_body, timeout=(5, 10)
                )
                res_json = res.json()
                if res_json.get("code") != 0:
                    logger.warning(
                        "[FeiShu] Stream: create card failed, summary=%s. "
                        "本次回复已自动降级为普通文本；请检查 cardkit:card:write 权限。",
                        _feishu_api_response_log_summary(res_json),
                    )
                    with lock:
                        disabled[0] = True
                    return
                cid = res_json["data"]["card_id"]
                with lock:
                    card_id[0] = cid

                # 步骤 2: 通过 card_id 发送消息（群聊优先用 reply，单聊直接 send）
                content_payload = json.dumps(
                    {"type": "card", "data": {"card_id": cid}}, ensure_ascii=False
                )
                can_reply = is_group and msg and hasattr(msg, "msg_id") and msg.msg_id
                if can_reply:
                    send_url = (
                        f"https://open.feishu.cn/open-apis/im/v1/messages/"
                        f"{msg.msg_id}/reply"
                    )
                    send_body = {"msg_type": "interactive", "content": content_payload}
                    send_res = requests.post(
                        send_url, headers=headers, json=send_body, timeout=(5, 10)
                    )
                else:
                    send_url = "https://open.feishu.cn/open-apis/im/v1/messages"
                    params = {"receive_id_type": receive_id_type}
                    send_body = {
                        "receive_id": receiver,
                        "msg_type": "interactive",
                        "content": content_payload,
                    }
                    send_res = requests.post(
                        send_url, headers=headers, params=params, json=send_body,
                        timeout=(5, 10),
                    )
                send_json = send_res.json()
                if send_json.get("code") != 0:
                    logger.warning(
                        "[FeiShu] Stream: send card failed, summary=%s. 降级为普通文本。",
                        _feishu_api_response_log_summary(send_json),
                    )
                    with lock:
                        disabled[0] = True
                    return
                mid = send_json["data"]["message_id"]
                with lock:
                    message_id[0] = mid
                logger.info(
                    "[FeiShu] Stream: card created and sent, card_ref=%s, message_ref=%s",
                    _feishu_log_ref(cid),
                    _feishu_log_ref(mid),
                )
            except Exception as e:
                logger.warning(
                    "[FeiShu] Stream: create/send card exception, error_type=%s. 降级为普通文本。",
                    type(e).__name__,
                )
                with lock:
                    disabled[0] = True
            finally:
                with lock:
                    init_in_flight[0] = False

        def _stream_update_text(full_text):
            """PUT 流式更新文本组件。content 必须是当前组件的全量文本。"""
            with lock:
                cid = card_id[0]
            if not cid:
                return
            url = (
                f"https://open.feishu.cn/open-apis/cardkit/v1/cards/"
                f"{cid}/elements/{ELEMENT_ID}/content"
            )
            body = {
                "content": full_text,
                "sequence": _next_sequence(),
            }
            try:
                res = requests.put(url, headers=headers, json=body, timeout=(5, 10))
                res_json = res.json()
                if res_json.get("code") != 0:
                    logger.warning(
                        "[FeiShu] Stream: update text failed, summary=%s",
                        _feishu_api_response_log_summary(res_json),
                    )
            except Exception as e:
                logger.warning("[FeiShu] Stream: update text exception, error_type=%s", type(e).__name__)

        def _update_full_card(streaming: bool):
            """Refresh panels, header, footer and streaming state in one update."""
            with lock:
                cid = card_id[0]
                full_card = progress_state.build_card(streaming=streaming)
            if not cid:
                return
            put_url = f"https://open.feishu.cn/open-apis/cardkit/v1/cards/{cid}"
            put_body = {
                "card": {"type": "card_json", "data": json.dumps(full_card, ensure_ascii=False)},
                "sequence": _next_sequence(),
            }
            try:
                res = requests.put(put_url, headers=headers, json=put_body, timeout=(5, 10))
                res_json = res.json()
                if res_json.get("code") != 0:
                    logger.warning(
                        "[FeiShu] Stream: full card update failed, summary=%s",
                        _feishu_api_response_log_summary(res_json),
                    )
            except Exception as e:
                logger.warning(
                    "[FeiShu] Stream: full card update exception, error_type=%s",
                    type(e).__name__,
                )

        def on_event(event: dict):
            event_type = event.get("type")
            data = event.get("data", {})

            # 一旦降级，本次回复不再做任何流式操作
            with lock:
                if disabled[0]:
                    return

            if event_type == "agent_start":
                with lock:
                    progress_state.consume(event)
                    if card_id[0] is None and not init_in_flight[0]:
                        init_in_flight[0] = True
                _create_and_send_card()
                return

            if event_type in ("turn_start", "reasoning_update"):
                with lock:
                    progress_state.consume(event)
                return

            if event_type == "message_update":
                delta = data.get("delta", "")
                if not delta:
                    return

                # 第一段：判断是否需要初始化（创建卡片 + 发送）
                need_init = False
                with lock:
                    if card_id[0] is None and not init_in_flight[0]:
                        init_in_flight[0] = True
                        need_init = True

                if need_init:
                    _create_and_send_card()
                    # 初始化失败已标记 disabled，下次循环直接 return
                    with lock:
                        if disabled[0]:
                            return

                # 第二段：累加文本，把快照丢给 push worker 异步推送。
                # 这里不能直接 requests.put，否则会阻塞 LLM stream 线程读下一个 chunk
                # （实测 DeepSeek 高频小 chunk 场景每个 PUT ~150ms，累积起来非常卡）。
                snapshot = ""
                should_push = False
                with lock:
                    progress_state.consume(event)
                    if card_id[0]:
                        snapshot = progress_state.current_text
                        should_push = True

                if should_push:
                    push_queue.put(snapshot)

            elif event_type in ("tool_execution_start", "tool_execution_end"):
                # Refresh the Tools panel as each tool starts/finishes so users
                # see live status and per-tool elapsed time.
                with lock:
                    progress_state.consume(event)
                    has_card = card_id[0] is not None
                if has_card:
                    _drain_push_queue()
                    _update_full_card(streaming=True)

            elif event_type == "message_end":
                # 工具轮结束后原地刷新 Reasoning / Tools 面板，保留同一张卡片。
                tool_calls = data.get("tool_calls", []) or []
                with lock:
                    progress_state.consume(event)
                if not tool_calls:
                    return
                _drain_push_queue()
                _update_full_card(streaming=True)

            elif event_type == "agent_cancelled":
                with lock:
                    progress_state.consume(event)

            elif event_type == "agent_end":
                # Finalize the same card with a status header and elapsed footer.
                with lock:
                    progress_state.consume(event)
                    final_text = progress_state.current_text
                    has_card = card_id[0] is not None
                    init_busy = init_in_flight[0]
                final_text = resolve_markdown_images(
                    final_text,
                    lambda url: upload_public_image_to_feishu(url, access_token),
                )
                with lock:
                    progress_state.current_text = final_text
                context["feishu_streamed"] = True

                if not has_card and not init_busy:
                    with lock:
                        init_in_flight[0] = True
                    _create_and_send_card()
                    with lock:
                        if disabled[0]:
                            return

                _drain_push_queue()
                _stream_update_text(final_text)
                _update_full_card(streaming=False)
                push_queue.put(None)

        return on_event


    def _make_feishu_stream_callback_plain(self, context, access_token):
        """
        基于飞书官方"流式更新卡片"API 实现打字机回复。

        流程：
        1. message_update 首次到达 → POST /cardkit/v1/cards 创建带 streaming_mode 的卡片实体，
           随后用 POST /im/v1/messages（或 reply）以 card_id 把卡片发出去
        2. 后续 message_update → PUT /cardkit/v1/cards/{id}/elements/{eid}/content
           传入"当前轮"的全量文本，飞书平台自动计算增量并以打字机效果上屏
           （流式模式下不受 10 QPS 限制）
        3. message_end（一轮 LLM 输出结束，且本轮触发了工具调用）→ 把 current 累计到 committed
           并加入分隔符；下一轮 message_update 又从空白开始，避免多轮内容串到一起
        4. agent_end → 用 final_response 强制覆盖卡片，再 PATCH /cardkit/v1/cards/{id}/settings
           关闭 streaming_mode，标记 context["feishu_streamed"]=True 让 chat_channel 跳过普通 send()

        前提条件：
        - 机器人已开通 cardkit:card:write 权限
        - 飞书客户端 7.20+

        失败降级：
        - 创建卡片实体失败（缺权限、网络等）→ 不设置 feishu_streamed 标记，让 chat_channel
          走普通文本回复路径，用户收到完整回复但无打字机效果，并打 warning 日志
        """
        # 共享状态（受 lock 保护）
        # 多轮 agent 模式下，每个"中间过场消息"会作为一张独立卡片发送。
        # current_text 只承载当前正在流式渲染的那张卡片的内容；message_end / agent_end
        # 时会把它定型并 reset。
        current_text = [""]                # 当前卡片正在累加的 LLM 输出
        card_id = [None]                   # 当前流式卡片的实体 ID（每段独立）
        message_id = [None]                # 当前卡片发送后的消息 ID（仅日志用）
        # 占位发送是同步进行的，但用一个 in-flight 标记防止并发的多条 message_update
        # 事件各自触发一次创建+发送，导致发出多张卡片。
        init_in_flight = [False]
        # 一旦初始化失败就长期标记为 disabled，本次回复不再尝试任何流式调用
        disabled = [False]
        # True after agent_cancelled: agent_end stops rewriting the card
        # with stale final_response and just finalizes current content.
        cancelled = [False]
        lock = threading.Lock()

        # ---- 异步推送队列 ----------------------------------------------------
        # 同步 requests.put 单次 100~300ms，会阻塞 LLM stream 线程读下一个 chunk。
        # 把推送丢给独立 worker 线程消费 queue，回调本身只做内存追加，立即返回。
        # 队列里只放"最新累积文本"的快照；worker 用 deduplication 避免重复推同一个
        # 内容（高频 chunk 场景下队列会堆积，只推最后一个就够了）。
        import queue as _queue
        push_queue: "_queue.Queue[str | None]" = _queue.Queue()

        def _push_worker():
            while True:
                snapshot = push_queue.get()
                if snapshot is None:
                    push_queue.task_done()
                    return
                # 合并队列中已堆积的快照：只推最后一个，省 PUT 次数同时降低延迟
                merged_count = 1
                stop = False
                while True:
                    try:
                        nxt = push_queue.get_nowait()
                    except _queue.Empty:
                        break
                    merged_count += 1
                    if nxt is None:
                        stop = True
                        break
                    snapshot = nxt
                try:
                    _stream_update_text(snapshot)
                finally:
                    for _ in range(merged_count):
                        push_queue.task_done()
                if stop:
                    return

        push_thread = threading.Thread(target=_push_worker, daemon=True, name="feishu-stream-push")
        push_thread.start()

        def _drain_push_queue():
            """等当前队列里所有 PUT 都完成。message_end/agent_end 在做最终定型前必须 drain，
            否则 worker 里堆积的旧快照可能在 final_text PUT 之后到达，把最终内容覆盖掉。"""
            try:
                push_queue.join()
            except Exception:
                pass

        msg = context.get("msg")
        is_group = context.get("isgroup", False)
        receiver = context.get("receiver")
        receive_id_type = context.get("receive_id_type", "open_id")
        # 客户端打字机渲染参数（飞书 App 侧实际"出字"速度）：
        #   - print_freq_ms：每次刷新的间隔
        #   - print_step：每次刷新出多少个字符
        # 当前 40ms × 4 字 ≈ 100 字/秒，接近 ChatGPT/DeepSeek 网页端的节奏。
        print_freq_ms = 40
        print_step = 4
        print_strategy = "fast"

        headers = {
            "Authorization": "Bearer " + access_token,
            "Content-Type": "application/json; charset=utf-8",
        }
        # 卡片中富文本组件的 element_id，后续所有 PUT 流式更新都打到这个组件
        ELEMENT_ID = "stream_md"
        # 操作序号，每次 PUT 必须严格递增（飞书要求）
        sequence = [0]

        def _next_sequence():
            sequence[0] += 1
            return sequence[0]

        def _build_card_json():
            """卡片 JSON 2.0 结构 + streaming_mode + 单 markdown 组件"""
            return json.dumps({
                "schema": "2.0",
                "config": {
                    "streaming_mode": True,
                    "summary": {"content": "[正在生成回复...]"},
                    "streaming_config": {
                        "print_frequency_ms": {"default": print_freq_ms},
                        "print_step": {"default": print_step},
                        "print_strategy": print_strategy,
                    },
                },
                "body": {
                    "elements": [
                        {
                            "tag": "markdown",
                            "content": "...",
                            "element_id": ELEMENT_ID,
                        }
                    ],
                },
                # 注意：JSON 2.0 不支持自定义 fallback 字段（传入会报错）。
                # 客户端 < 7.20 时，飞书会自动展示"请升级客户端"占位，无需配置。
            }, ensure_ascii=False)

        def _create_and_send_card():
            """同步执行：创建卡片实体 → 发送消息。任意一步失败则 disabled=True 触发降级"""
            try:
                # 步骤 1: 创建卡片实体
                create_url = "https://open.feishu.cn/open-apis/cardkit/v1/cards"
                create_body = {"type": "card_json", "data": _build_card_json()}
                res = requests.post(
                    create_url, headers=headers, json=create_body, timeout=(5, 10)
                )
                res_json = res.json()
                if res_json.get("code") != 0:
                    logger.warning(
                        "[FeiShu] Stream: create card failed, summary=%s. "
                        "本次回复已自动降级为普通文本回复（一次性返回完整内容）。"
                        "如需开启流式打字机效果与完整 Markdown 渲染，请到飞书开放平台 "
                        "https://open.feishu.cn/app 给机器人开通 cardkit:card:write 权限"
                        "（创建与更新卡片）并重新发布版本，同时确保飞书客户端 >= 7.20。",
                        _feishu_api_response_log_summary(res_json),
                    )
                    with lock:
                        disabled[0] = True
                    return
                cid = res_json["data"]["card_id"]
                with lock:
                    card_id[0] = cid

                # 步骤 2: 通过 card_id 发送消息（群聊优先用 reply，单聊直接 send）
                content_payload = json.dumps(
                    {"type": "card", "data": {"card_id": cid}}, ensure_ascii=False
                )
                can_reply = is_group and msg and hasattr(msg, "msg_id") and msg.msg_id
                if can_reply:
                    send_url = (
                        f"https://open.feishu.cn/open-apis/im/v1/messages/"
                        f"{msg.msg_id}/reply"
                    )
                    send_body = {"msg_type": "interactive", "content": content_payload}
                    send_res = requests.post(
                        send_url, headers=headers, json=send_body, timeout=(5, 10)
                    )
                else:
                    send_url = "https://open.feishu.cn/open-apis/im/v1/messages"
                    params = {"receive_id_type": receive_id_type}
                    send_body = {
                        "receive_id": receiver,
                        "msg_type": "interactive",
                        "content": content_payload,
                    }
                    send_res = requests.post(
                        send_url, headers=headers, params=params, json=send_body,
                        timeout=(5, 10),
                    )
                send_json = send_res.json()
                if send_json.get("code") != 0:
                    logger.warning(
                        "[FeiShu] Stream: send card failed, summary=%s. 降级为普通文本。",
                        _feishu_api_response_log_summary(send_json),
                    )
                    with lock:
                        disabled[0] = True
                    return
                mid = send_json["data"]["message_id"]
                with lock:
                    message_id[0] = mid
                logger.info(
                    "[FeiShu] Stream: card created and sent, "
                    "card_ref=%s, message_ref=%s",
                    _feishu_log_ref(cid),
                    _feishu_log_ref(mid),
                )
            except Exception as e:
                logger.warning(
                    "[FeiShu] Stream: create/send card exception, error_type=%s. 降级为普通文本。",
                    type(e).__name__,
                )
                with lock:
                    disabled[0] = True
            finally:
                with lock:
                    init_in_flight[0] = False

        def _stream_update_text(full_text):
            """PUT 流式更新文本组件。content 必须是当前组件的全量文本。"""
            with lock:
                cid = card_id[0]
            if not cid:
                return
            url = (
                f"https://open.feishu.cn/open-apis/cardkit/v1/cards/"
                f"{cid}/elements/{ELEMENT_ID}/content"
            )
            body = {
                "content": full_text,
                "sequence": _next_sequence(),
            }
            try:
                res = requests.put(url, headers=headers, json=body, timeout=(5, 10))
                res_json = res.json()
                if res_json.get("code") != 0:
                    logger.warning(
                        "[FeiShu] Stream: update text failed, summary=%s",
                        _feishu_api_response_log_summary(res_json),
                    )
            except Exception as e:
                logger.warning("[FeiShu] Stream: update text exception, error_type=%s", type(e).__name__)

        def _close_streaming_mode(final_text: str = ""):
            """关闭流式模式（卡片转入"普通"状态，可被转发）。

            同时通过整卡更新接口把 summary 改成最终内容的预览，否则飞书会话列表
            会一直显示创建卡片时的占位摘要（"[正在生成回复...]"）。
            """
            with lock:
                cid = card_id[0]
            if not cid:
                return

            # 1) 通过整卡更新接口把 streaming_mode 关掉，并改写 summary
            #    （settings 接口的 config 不接受 summary 字段，会报 code=2200）
            preview_src = (final_text or "").strip().replace("\n", " ")
            preview = preview_src[:30] if preview_src else ""
            full_card = {
                "schema": "2.0",
                "config": {
                    "streaming_mode": False,
                    "summary": {"content": preview or " "},
                },
                "body": {
                    "elements": [
                        {
                            "tag": "markdown",
                            "content": final_text or " ",
                            "element_id": ELEMENT_ID,
                        }
                    ],
                },
            }
            put_url = f"https://open.feishu.cn/open-apis/cardkit/v1/cards/{cid}"
            put_body = {
                "card": {"type": "card_json", "data": json.dumps(full_card, ensure_ascii=False)},
                "sequence": _next_sequence(),
            }
            try:
                res = requests.put(put_url, headers=headers, json=put_body, timeout=(5, 10))
                res_json = res.json()
                if res_json.get("code") != 0:
                    logger.warning(
                        "[FeiShu] Stream: finalize card (close+summary) failed, summary=%s",
                        _feishu_api_response_log_summary(res_json),
                    )
            except Exception as e:
                logger.warning("[FeiShu] Stream: finalize card exception, error_type=%s", type(e).__name__)

        def on_event(event: dict):
            event_type = event.get("type")
            data = event.get("data", {})

            # 一旦降级，本次回复不再做任何流式操作
            with lock:
                if disabled[0]:
                    return

            if event_type == "message_update":
                delta = data.get("delta", "")
                if not delta:
                    return

                # 第一段：判断是否需要初始化（创建卡片 + 发送）
                need_init = False
                with lock:
                    if card_id[0] is None and not init_in_flight[0]:
                        init_in_flight[0] = True
                        need_init = True

                if need_init:
                    _create_and_send_card()
                    # 初始化失败已标记 disabled，下次循环直接 return
                    with lock:
                        if disabled[0]:
                            return

                # 第二段：累加文本，把快照丢给 push worker 异步推送。
                # 这里不能直接 requests.put，否则会阻塞 LLM stream 线程读下一个 chunk
                # （实测 DeepSeek 高频小 chunk 场景每个 PUT ~150ms，累积起来非常卡）。
                snapshot = ""
                should_push = False
                with lock:
                    current_text[0] += delta
                    if card_id[0]:
                        snapshot = current_text[0]
                        should_push = True

                if should_push:
                    push_queue.put(snapshot)

            elif event_type == "message_end":
                # 一轮 LLM 输出结束。如果本轮触发了工具调用，说明当前轮的文本是
                # "中间过场消息"（如"来看看！"），应该作为独立卡片定型，然后为下一轮
                # 重新创建一张新卡片。这样最终用户看到的是：
                #   [卡片1: 中间过场1]
                #   [卡片2: 中间过场2]
                #   ...
                #   [卡片N: 最终回复]
                # 与 wecom_bot 的多消息流式体验对齐。
                tool_calls = data.get("tool_calls", []) or []
                if not tool_calls:
                    # 没有工具调用：本轮即最终回复，留给 agent_end 统一处理。
                    return

                with lock:
                    text_to_finalize = current_text[0].rstrip()
                    current_text[0] = ""

                if not text_to_finalize:
                    return

                # 等异步队列里堆积的快照都推完，避免它们晚于 final 文本到达把内容覆盖掉
                _drain_push_queue()
                # 用最终文本覆盖当前卡片并关闭流式模式（凝固成普通卡片，
                # 同时把会话列表的 summary 改成预览，不再显示"正在生成回复..."）
                _stream_update_text(text_to_finalize)
                _close_streaming_mode(text_to_finalize)

                # 重置卡片状态，下一段 message_update 会触发新卡片的创建
                with lock:
                    card_id[0] = None
                    message_id[0] = None
                    sequence[0] = 0

            elif event_type == "agent_cancelled":
                # Lock channel into "no-rewrite" mode: the subsequent
                # agent_end's final_response is from the last *completed*
                # turn (the user already saw it), so rewriting the card
                # would duplicate it visually.
                with lock:
                    cancelled[0] = True

            elif event_type == "agent_end":
                # 最终回复：用 final_response 覆盖当前流式卡片，然后关闭流式模式。
                final_response = data.get("final_response", "")
                # 标记 streamed 让 chat_channel 跳过 send()
                context["feishu_streamed"] = True

                with lock:
                    was_cancelled = cancelled[0]
                    has_card = card_id[0] is not None
                    init_busy = init_in_flight[0]
                    pending_text = current_text[0]

                if was_cancelled:
                    # Cancelled path: finalize the in-flight card with
                    # partial output (or a short marker if empty); drop
                    # stale final_response to avoid duplicating last turn.
                    if has_card:
                        _drain_push_queue()
                        partial = (pending_text or "").rstrip()
                        final_text = partial or "_(已中止)_"
                        _stream_update_text(final_text)
                        _close_streaming_mode(final_text)
                    push_queue.put(None)
                    return

                if not final_response:
                    return
                final_text = str(final_response)

                # 罕见情况：agent_end 触发时还没创建过卡片（极快返回 / 没有
                # message_update），主动创建一张承载 final_text。
                if not has_card and not init_busy:
                    with lock:
                        init_in_flight[0] = True
                    _create_and_send_card()
                    with lock:
                        if disabled[0]:
                            return

                _drain_push_queue()
                _stream_update_text(final_text)
                _close_streaming_mode(final_text)
                # 通知 push worker 退出（本次回复彻底结束）
                push_queue.put(None)

        return on_event

    def fetch_access_token(self) -> str:
        url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal/"
        headers = {
            "Content-Type": "application/json"
        }
        req_body = {
            "app_id": self.feishu_app_id,
            "app_secret": self.feishu_app_secret
        }
        data = bytes(json.dumps(req_body), encoding='utf8')
        response = requests.post(url=url, data=data, headers=headers)
        if response.status_code == 200:
            res = response.json()
            if res.get("code") != 0:
                logger.error(
                    "[FeiShu] get tenant_access_token error, code=%s, msg=%s",
                    res.get("code"),
                    _feishu_log_text(res.get("msg")),
                )
                return ""
            else:
                return res.get("tenant_access_token")
        else:
            logger.error("[FeiShu] fetch token error, status=%s", response.status_code)

    def _upload_image_url(self, img_url, access_token):
        logger.debug("[FeiShu] start process image, image_source_ref=%s", _feishu_log_ref(img_url))

        # Check if it's a local file path (file:// protocol)
        if img_url.startswith("file://"):
            local_path = img_url[7:]  # Remove "file://" prefix
            logger.info("[FeiShu] uploading local image: path_ref=%s", _feishu_log_ref(local_path))

            if not os.path.exists(local_path):
                logger.error("[FeiShu] local image not found: path_ref=%s", _feishu_log_ref(local_path))
                return None

            # Upload directly from local file
            upload_url = "https://open.feishu.cn/open-apis/im/v1/images"
            data = {'image_type': 'message'}
            headers = {'Authorization': f'Bearer {access_token}'}

            with open(local_path, "rb") as file:
                upload_response = requests.post(upload_url, files={"image": file}, data=data, headers=headers)
                response_data = upload_response.json()
                logger.info(
                    "[FeiShu] upload image response, status=%s, summary=%s",
                    upload_response.status_code,
                    _feishu_api_response_log_summary(response_data),
                )
                if response_data.get("code") == 0:
                    return response_data.get("data").get("image_key")
                else:
                    logger.error(
                        "[FeiShu] upload image failed, summary=%s",
                        _feishu_api_response_log_summary(response_data),
                    )
                    return None

        # Original logic for HTTP URLs
        response = requests.get(img_url)
        suffix = utils.get_path_suffix(img_url)
        temp_name = str(uuid.uuid4()) + "." + suffix
        if response.status_code == 200:
            # 将图片内容保存为临时文件
            with open(temp_name, "wb") as file:
                file.write(response.content)

        # upload
        upload_url = "https://open.feishu.cn/open-apis/im/v1/images"
        data = {
            'image_type': 'message'
        }
        headers = {
            'Authorization': f'Bearer {access_token}',
        }
        with open(temp_name, "rb") as file:
            upload_response = requests.post(upload_url, files={"image": file}, data=data, headers=headers)
            os.remove(temp_name)
            response_data = upload_response.json()
            logger.info(
                "[FeiShu] upload image response, status=%s, summary=%s",
                upload_response.status_code,
                _feishu_api_response_log_summary(response_data),
            )
            return response_data.get("data").get("image_key")

    def _get_video_duration(self, file_path: str) -> int:
        """
        获取视频时长（毫秒）
        
        Args:
            file_path: 视频文件路径
        
        Returns:
            视频时长（毫秒），如果获取失败返回0
        """
        try:
            import subprocess

            # 使用 ffprobe 获取视频时长
            cmd = [
                'ffprobe',
                '-v', 'error',
                '-show_entries', 'format=duration',
                '-of', 'default=noprint_wrappers=1:nokey=1',
                file_path
            ]

            result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            if result.returncode == 0:
                duration_seconds = float(result.stdout.strip())
                duration_ms = int(duration_seconds * 1000)
                logger.info(f"[FeiShu] Video duration: {duration_seconds:.2f}s ({duration_ms}ms)")
                return duration_ms
            else:
                stderr_bytes = len((getattr(result, "stderr", "") or "").encode("utf-8", errors="ignore"))
                logger.warning(
                    "[FeiShu] Failed to get video duration via ffprobe, path_ref=%s, returncode=%s, stderr_bytes=%s",
                    _feishu_log_ref(file_path),
                    result.returncode,
                    stderr_bytes,
                )
                return 0
        except FileNotFoundError:
            logger.warning("[FeiShu] ffprobe not found, video duration will be 0. Install ffmpeg to fix this.")
            return 0
        except Exception as e:
            logger.warning(
                "[FeiShu] Failed to get video duration, path_ref=%s, error_type=%s",
                _feishu_log_ref(file_path),
                type(e).__name__,
            )
            return 0

    def _upload_video_url(self, video_url, access_token):
        """
        Upload video to Feishu and return video info (file_key and duration)
        Supports:
        - file:// URLs for local files
        - http(s):// URLs (download then upload)
        
        Returns:
            dict with 'file_key' and 'duration' (milliseconds), or None if failed
        """
        local_path = None
        temp_file = None

        try:
            # For file:// URLs (local files), upload directly
            if video_url.startswith("file://"):
                local_path = video_url[7:]  # Remove file:// prefix
                if not os.path.exists(local_path):
                    logger.error("[FeiShu] local video file not found: path_ref=%s", _feishu_log_ref(local_path))
                    return None
            else:
                # For HTTP URLs, download first
                logger.info("[FeiShu] Downloading video from URL: url_ref=%s", _feishu_log_ref(video_url))
                response = requests.get(video_url, timeout=(5, 60))
                if response.status_code != 200:
                    logger.error(f"[FeiShu] download video failed, status={response.status_code}")
                    return None

                # Save to temp file
                import uuid
                file_name = os.path.basename(video_url) or "video.mp4"
                temp_file = str(uuid.uuid4()) + "_" + file_name

                with open(temp_file, "wb") as file:
                    file.write(response.content)

                logger.info(f"[FeiShu] Video downloaded, size={len(response.content)} bytes")
                local_path = temp_file

            # Get video duration
            duration = self._get_video_duration(local_path)

            # Upload to Feishu
            file_name = os.path.basename(local_path)
            file_ext = os.path.splitext(file_name)[1].lower()
            file_type_map = {'.mp4': 'mp4'}
            file_type = file_type_map.get(file_ext, 'mp4')

            upload_url = "https://open.feishu.cn/open-apis/im/v1/files"
            data = {
                'file_type': file_type,
                'file_name': file_name
            }
            # Add duration only if available (required for video/audio)
            if duration:
                data['duration'] = duration  # Must be int, not string

            headers = {'Authorization': f'Bearer {access_token}'}

            logger.info(
                "[FeiShu] Uploading video: file_name_ref=%s, duration=%sms",
                _feishu_log_ref(file_name),
                duration,
            )

            with open(local_path, "rb") as file:
                upload_response = requests.post(
                    upload_url,
                    files={"file": file},
                    data=data,
                    headers=headers,
                    timeout=(5, 60)
                )
                response_data = upload_response.json()
                logger.info(
                    "[FeiShu] upload video response, status=%s, summary=%s",
                    upload_response.status_code,
                    _feishu_api_response_log_summary(response_data),
                )
                if response_data.get("code") == 0:
                    # Add duration to the response data (API doesn't return it)
                    upload_data = response_data.get("data")
                    upload_data['duration'] = duration  # Add our calculated duration
                    logger.info(
                        "[FeiShu] Upload complete: file_ref=%s, duration=%sms",
                        _feishu_log_ref(upload_data.get("file_key")),
                        duration,
                    )
                    return upload_data
                else:
                    logger.error(
                        "[FeiShu] upload video failed, summary=%s",
                        _feishu_api_response_log_summary(response_data),
                    )
                    return None

        except Exception as e:
            logger.error("[FeiShu] upload video exception, error_type=%s", type(e).__name__)
            return None

        finally:
            # Clean up temp file
            if temp_file and os.path.exists(temp_file):
                try:
                    os.remove(temp_file)
                except Exception as e:
                    logger.warning(
                        "[FeiShu] Failed to remove temp file, path_ref=%s, error_type=%s",
                        _feishu_log_ref(temp_file),
                        type(e).__name__,
                    )

    def _upload_audio(self, audio_path, access_token):
        """
        Upload a local audio file to Feishu and return file_key.
        audio_path is a plain local file path (no file:// prefix).
        Feishu audio messages only support opus format; non-opus files are converted first.
        """
        logger.debug("[FeiShu] start upload audio, path_ref=%s", _feishu_log_ref(audio_path))

        if not os.path.exists(audio_path):
            logger.error("[FeiShu] audio file not found: path_ref=%s", _feishu_log_ref(audio_path))
            return None

        # Feishu only plays audio messages in opus format.
        # Convert if the TTS engine produced a different format (e.g. mp3 from OpenAI TTS).
        upload_path = audio_path
        if not audio_path.lower().endswith('.opus'):
            opus_path = os.path.splitext(audio_path)[0] + '.opus'
            try:
                from pydub import AudioSegment
                audio = AudioSegment.from_file(audio_path)
                audio.export(opus_path, format='opus')
                upload_path = opus_path
                logger.info("[FeiShu] Converted audio to opus: path_ref=%s", _feishu_log_ref(opus_path))
            except Exception as e:
                logger.warning(
                    "[FeiShu] Failed to convert audio to opus, uploading original, path_ref=%s, error_type=%s",
                    _feishu_log_ref(audio_path),
                    type(e).__name__,
                )
                upload_path = audio_path

        file_name = os.path.splitext(os.path.basename(upload_path))[0] + '.opus'
        upload_url = "https://open.feishu.cn/open-apis/im/v1/files"
        data = {'file_type': 'opus', 'file_name': file_name}
        headers = {'Authorization': f'Bearer {access_token}'}

        try:
            with open(upload_path, "rb") as f:
                upload_response = requests.post(
                    upload_url,
                    files={"file": f},
                    data=data,
                    headers=headers,
                    timeout=(5, 30)
                )
                response_data = upload_response.json()
                logger.info(
                    "[FeiShu] upload audio response, status=%s, summary=%s",
                    upload_response.status_code,
                    _feishu_api_response_log_summary(response_data),
                )
                if response_data.get("code") == 0:
                    return response_data.get("data").get("file_key")
                else:
                    logger.error(
                        "[FeiShu] upload audio failed, summary=%s",
                        _feishu_api_response_log_summary(response_data),
                    )
                    return None
        except Exception as e:
            logger.error("[FeiShu] upload audio exception, path_ref=%s, error_type=%s", _feishu_log_ref(upload_path), type(e).__name__)
            return None
        finally:
            # 无论上传成功与否都清理转换产生的临时 opus 文件，避免失败路径下磁盘堆积。
            if upload_path != audio_path and os.path.exists(upload_path):
                try:
                    os.remove(upload_path)
                except Exception as e:
                    logger.warning(
                        "[FeiShu] Failed to remove temp opus file, path_ref=%s, error_type=%s",
                        _feishu_log_ref(upload_path),
                        type(e).__name__,
                    )

    def _upload_file_url(self, file_url, access_token):
        """
        Upload file to Feishu
        Supports both local files (file://) and HTTP URLs
        """
        logger.debug("[FeiShu] start process file, file_source_ref=%s", _feishu_log_ref(file_url))

        # Check if it's a local file path (file:// protocol)
        if file_url.startswith("file://"):
            local_path = file_url[7:]  # Remove "file://" prefix
            logger.info("[FeiShu] uploading local file: path_ref=%s", _feishu_log_ref(local_path))

            if not os.path.exists(local_path):
                logger.error("[FeiShu] local file not found: path_ref=%s", _feishu_log_ref(local_path))
                return None

            # Get file info
            file_name = os.path.basename(local_path)
            file_ext = os.path.splitext(file_name)[1].lower()

            # Determine file type for Feishu API
            # Feishu supports: opus, mp4, pdf, doc, xls, ppt, stream (other types)
            file_type_map = {
                '.opus': 'opus',
                '.mp4': 'mp4',
                '.pdf': 'pdf',
                '.doc': 'doc', '.docx': 'doc',
                '.xls': 'xls', '.xlsx': 'xls',
                '.ppt': 'ppt', '.pptx': 'ppt',
            }
            file_type = file_type_map.get(file_ext, 'stream')  # Default to stream for other types

            # Upload file to Feishu
            upload_url = "https://open.feishu.cn/open-apis/im/v1/files"
            data = {'file_type': file_type, 'file_name': file_name}
            headers = {'Authorization': f'Bearer {access_token}'}

            try:
                with open(local_path, "rb") as file:
                    upload_response = requests.post(
                        upload_url,
                        files={"file": file},
                        data=data,
                        headers=headers,
                        timeout=(5, 30)  # 5s connect, 30s read timeout
                    )
                    response_data = upload_response.json()
                    logger.info(
                        "[FeiShu] upload file response, status=%s, summary=%s",
                        upload_response.status_code,
                        _feishu_api_response_log_summary(response_data),
                    )
                    if response_data.get("code") == 0:
                        return response_data.get("data").get("file_key")
                    else:
                        logger.error(
                            "[FeiShu] upload file failed, summary=%s",
                            _feishu_api_response_log_summary(response_data),
                        )
                        return None
            except Exception as e:
                logger.error(
                    "[FeiShu] upload file exception, file_ref=%s, error_type=%s",
                    _feishu_log_ref(local_path),
                    type(e).__name__,
                )
                return None

        # For HTTP URLs, download first then upload
        try:
            response = requests.get(file_url, timeout=(5, 30))
            if response.status_code != 200:
                logger.error(f"[FeiShu] download file failed, status={response.status_code}")
                return None

            # Save to temp file
            import uuid
            file_name = os.path.basename(file_url)
            temp_name = str(uuid.uuid4()) + "_" + file_name

            with open(temp_name, "wb") as file:
                file.write(response.content)

            # Upload
            file_ext = os.path.splitext(file_name)[1].lower()
            file_type_map = {
                '.opus': 'opus', '.mp4': 'mp4', '.pdf': 'pdf',
                '.doc': 'doc', '.docx': 'doc',
                '.xls': 'xls', '.xlsx': 'xls',
                '.ppt': 'ppt', '.pptx': 'ppt',
            }
            file_type = file_type_map.get(file_ext, 'stream')

            upload_url = "https://open.feishu.cn/open-apis/im/v1/files"
            data = {'file_type': file_type, 'file_name': file_name}
            headers = {'Authorization': f'Bearer {access_token}'}

            with open(temp_name, "rb") as file:
                upload_response = requests.post(upload_url, files={"file": file}, data=data, headers=headers)
                response_data = upload_response.json()
                logger.info(
                    "[FeiShu] upload file response, status=%s, summary=%s",
                    upload_response.status_code,
                    _feishu_api_response_log_summary(response_data),
                )
                os.remove(temp_name)  # Clean up temp file

                if response_data.get("code") == 0:
                    return response_data.get("data").get("file_key")
                else:
                    logger.error(
                        "[FeiShu] upload file failed, summary=%s",
                        _feishu_api_response_log_summary(response_data),
                    )
                    return None
        except Exception as e:
            logger.error("[FeiShu] upload file from URL exception, file_source_ref=%s, error_type=%s", _feishu_log_ref(file_url), type(e).__name__)
            return None

    def _compose_context(self, ctype: ContextType, content, **kwargs):
        context = Context(ctype, content)
        context.kwargs = kwargs
        if "channel_type" not in context:
            context["channel_type"] = self.channel_type
        if "origin_ctype" not in context:
            context["origin_ctype"] = ctype

        cmsg = context["msg"]

        # Set session_id based on chat type
        if cmsg.is_group:
            # Group chat: check if group_shared_session is enabled
            if conf().get("group_shared_session", True):
                # All users in the group share the same session context
                context["session_id"] = cmsg.other_user_id  # group_id
            else:
                # Each user has their own session within the group
                # This ensures:
                # - Same user in different groups have separate conversation histories
                # - Same user in private chat and group chat have separate histories
                context["session_id"] = f"{cmsg.from_user_id}:{cmsg.other_user_id}"
        else:
            # Private chat: use user_id only
            context["session_id"] = cmsg.from_user_id

        context["receiver"] = cmsg.other_user_id

        if ctype == ContextType.TEXT:
            # 1.文本请求
            # 图片生成处理
            img_match_prefix = check_prefix(content, conf().get("image_create_prefix"))
            if img_match_prefix:
                content = content.replace(img_match_prefix, "", 1)
                context.type = ContextType.IMAGE_CREATE
            else:
                context.type = ContextType.TEXT
            context.content = content.strip()
            # Text input opts into voice replies only when the always-on toggle is set.
            if "desire_rtype" not in context and conf().get("always_reply_voice"):
                context["desire_rtype"] = ReplyType.VOICE

        elif context.type == ContextType.VOICE:
            # 2.语音请求: voice input replies with voice if either
            # voice_reply_voice (mirror reply) or always_reply_voice is on.
            if "desire_rtype" not in context and (
                conf().get("voice_reply_voice") or conf().get("always_reply_voice")
            ):
                context["desire_rtype"] = ReplyType.VOICE

        return context


class FeishuController:
    """
    HTTP服务器控制器，用于webhook模式
    """
    # 类常量
    FAILED_MSG = '{"success": false}'
    SUCCESS_MSG = '{"success": true}'
    MESSAGE_RECEIVE_TYPE = "im.message.receive_v1"
    MESSAGE_RECALLED_TYPE = "im.message.recalled_v1"
    CARD_ACTION_TYPE = "card.action.trigger"

    def GET(self):
        return "Feishu service start success!"

    def POST(self):
        try:
            channel = FeiShuChanel()

            request = json.loads(web.data().decode("utf-8"))
            header = request.get("header") if isinstance(request.get("header"), dict) else {}
            logger.debug(
                "[FeiShu] receive webhook request summary: %s",
                {
                    "type": _feishu_log_text(request.get("type") or ""),
                    "eventType": _feishu_log_text(header.get("event_type") or ""),
                    "token": _feishu_log_presence(header.get("token")),
                    "event": _feishu_event_log_summary(request.get("event") if isinstance(request.get("event"), dict) else {}),
                },
            )

            # 1.事件订阅回调验证
            if request.get("type") == URL_VERIFICATION:
                varify_res = {"challenge": request.get("challenge")}
                return json.dumps(varify_res)

            # 2.校验回调；卡片回调可能把 token 放在 event 或顶层。
            event = request.get("event") or {}
            event_type = header.get("event_type") or request.get("type")
            callback_token = (
                header.get("token")
                or event.get("token")
                or request.get("token")
            )
            if callback_token != channel.feishu_token:
                return self.FAILED_MSG

            if event_type == self.CARD_ACTION_TYPE and event:
                return json.dumps(
                    channel._handle_card_action_event(event),
                    ensure_ascii=False,
                )

            if event_type == self.MESSAGE_RECEIVE_TYPE and event:
                channel._handle_message_event(event)
            elif event_type == self.MESSAGE_RECALLED_TYPE and event:
                channel._handle_message_recalled_event(event)

            return self.SUCCESS_MSG

        except Exception as e:
            logger.error("[FeiShu] webhook request error, error_type=%s", type(e).__name__)
            return self.FAILED_MSG
