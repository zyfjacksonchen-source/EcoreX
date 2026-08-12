"""CowAgent 2.1.5 channel lifecycle, shared by CLI and e-Mate Runtime."""

from __future__ import annotations

import ctypes
import importlib
import os
import threading
import time

from channel import channel_factory
from common import const
from common.log import logger
from config import conf
from plugins import PluginManager


DESKTOP_MODE = os.environ.get("COW_DESKTOP") == "1"
_channel_manager = None


def bind_channel_manager(manager) -> None:
    global _channel_manager
    _channel_manager = manager


def get_channel_manager():
    return _channel_manager


def parse_channel_type(raw) -> list[str]:
    if isinstance(raw, list):
        return [str(channel).strip() for channel in raw if str(channel).strip()]
    if isinstance(raw, str):
        return [channel.strip() for channel in raw.split(",") if channel.strip()]
    return []


class ChannelManager:
    """Run each configured Cow channel in its own daemon thread."""

    def __init__(self) -> None:
        self._channels = {}
        self._threads = {}
        self._primary_channel = None
        self._lock = threading.Lock()
        self.cloud_mode = False

    @property
    def channel(self):
        return self._primary_channel

    def get_channel(self, channel_name: str):
        return self._channels.get(channel_name)

    def start(self, channel_names: list[str], first_start: bool = False) -> None:
        for name in channel_names:
            if self._channels.get(name) is not None:
                logger.warning(
                    "[ChannelManager] Channel '%s' is already running, stopping it first",
                    name,
                )
                self.stop(name)

        with self._lock:
            channels = []
            for name in channel_names:
                channel = channel_factory.create_channel(name)
                channel.cloud_mode = self.cloud_mode
                self._channels[name] = channel
                channels.append((name, channel))
                if self._primary_channel is None and name != "web":
                    self._primary_channel = channel

            if self._primary_channel is None and channels:
                self._primary_channel = channels[0][1]

            if first_start:
                if DESKTOP_MODE:
                    threading.Thread(
                        target=PluginManager().load_plugins, daemon=True
                    ).start()
                else:
                    PluginManager().load_plugins()

                if conf().get("use_linkai") and (
                    os.environ.get("CLOUD_DEPLOYMENT_ID")
                    or conf().get("cloud_deployment_id")
                ):
                    try:
                        from common import cloud_client

                        threading.Thread(
                            target=cloud_client.start,
                            args=(self._primary_channel, self),
                            daemon=True,
                        ).start()
                    except Exception:
                        pass

            web_entry = None
            other_entries = []
            for entry in channels:
                if entry[0] == "web":
                    web_entry = entry
                else:
                    other_entries.append(entry)

            ordered = ([web_entry] if web_entry else []) + other_entries
            for index, (name, channel) in enumerate(ordered):
                if index > 0 and name != "web":
                    time.sleep(0.1)
                thread = threading.Thread(
                    target=self._run_channel,
                    args=(name, channel),
                    daemon=True,
                )
                self._threads[name] = thread
                thread.start()
                logger.debug("[ChannelManager] Channel '%s' started", name)

    @staticmethod
    def _run_channel(name: str, channel) -> None:
        try:
            channel.startup()
        except Exception as error:
            logger.error("[ChannelManager] Channel '%s' startup error: %s", name, error)
            logger.exception(error)

    def stop(self, channel_name: str | None = None) -> None:
        with self._lock:
            names = [channel_name] if channel_name else list(self._channels)
            to_stop = []
            for name in names:
                to_stop.append(
                    (
                        name,
                        self._channels.pop(name, None),
                        self._threads.pop(name, None),
                    )
                )
            if self._primary_channel not in self._channels.values():
                self._primary_channel = next(iter(self._channels.values()), None)

        for name, channel, thread in to_stop:
            if channel is None:
                continue
            graceful = False
            try:
                channel.stop()
                graceful = True
            except Exception as error:
                logger.warning(
                    "[ChannelManager] Error during channel '%s' stop: %s", name, error
                )
            if thread and thread.is_alive():
                thread.join(timeout=5)
                if thread.is_alive() and not graceful:
                    self._interrupt_thread(thread, name)

    @staticmethod
    def _interrupt_thread(thread: threading.Thread, name: str) -> None:
        try:
            if thread.ident is None:
                return
            result = ctypes.pythonapi.PyThreadState_SetAsyncExc(
                ctypes.c_ulong(thread.ident), ctypes.py_object(SystemExit)
            )
            if result > 1:
                ctypes.pythonapi.PyThreadState_SetAsyncExc(
                    ctypes.c_ulong(thread.ident), None
                )
            elif result == 1:
                logger.info("[ChannelManager] Interrupted channel '%s'", name)
        except Exception as error:
            logger.warning(
                "[ChannelManager] Thread interrupt error for '%s': %s", name, error
            )

    def restart(self, channel_name: str) -> None:
        self.stop(channel_name)
        _clear_singleton_cache(channel_name)
        time.sleep(1)
        self.start([channel_name], first_start=False)

    def add_channel(self, channel_name: str) -> None:
        if self.get_channel(channel_name) is not None:
            self.restart(channel_name)
            return
        _clear_singleton_cache(channel_name)
        self.start([channel_name], first_start=False)

    def remove_channel(self, channel_name: str) -> None:
        self.stop(channel_name)


def _clear_singleton_cache(channel_name: str) -> None:
    classes = {
        "wechatmp": "channel.wechatmp.wechatmp_channel.WechatMPChannel",
        "wechatmp_service": "channel.wechatmp.wechatmp_channel.WechatMPChannel",
        "wechatcom_app": "channel.wechatcom.wechatcomapp_channel.WechatComAppChannel",
        const.WECHAT_KF: "channel.wechat_kf.wechat_kf_channel.WechatKfChannel",
        const.FEISHU: "channel.feishu.feishu_channel.FeiShuChanel",
        const.DINGTALK: "channel.dingtalk.dingtalk_channel.DingTalkChanel",
        const.WECOM_BOT: "channel.wecom_bot.wecom_bot_channel.WecomBotChannel",
        const.QQ: "channel.qq.qq_channel.QQChannel",
        const.TELEGRAM: "channel.telegram.telegram_channel.TelegramChannel",
        const.SLACK: "channel.slack.slack_channel.SlackChannel",
        const.DISCORD: "channel.discord.discord_channel.DiscordChannel",
        const.WEIXIN: "channel.weixin.weixin_channel.WeixinChannel",
        "wx": "channel.weixin.weixin_channel.WeixinChannel",
    }
    module_path = classes.get(channel_name)
    if not module_path:
        return
    try:
        module_name, class_name = module_path.rsplit(".", 1)
        wrapper = getattr(importlib.import_module(module_name), class_name, None)
        for cell in getattr(wrapper, "__closure__", ()) or ():
            try:
                if isinstance(cell.cell_contents, dict):
                    cell.cell_contents.clear()
                    break
            except ValueError:
                pass
    except Exception as error:
        logger.warning(
            "[ChannelManager] Failed to clear singleton cache for '%s': %s",
            channel_name,
            error,
        )


__all__ = [
    "ChannelManager",
    "bind_channel_manager",
    "get_channel_manager",
    "parse_channel_type",
]
