"""Thin e-Mate Runtime bridge for CowAgent's native channel lifecycle."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any, Mapping

from bridge.reply import Reply, ReplyType
from channel.runtime_bridge import (
    bind_runtime_bridge,
    current_runtime_bridge,
    unbind_runtime_bridge,
)
from config import conf

from .channel_catalog import CHANNEL_CATALOG, normalize_channel_name
from .channel_runtime import ChannelInboundMessage, ChannelRuntimeDispatcher


bind_cow_channel_runtime_bridge = bind_runtime_bridge
unbind_cow_channel_runtime_bridge = unbind_runtime_bridge
current_cow_channel_runtime_bridge = current_runtime_bridge


class CowChannelRuntimeBridge:
    """Translate only envelopes; Cow channels keep their native send semantics."""

    def __init__(
        self, dispatcher: ChannelRuntimeDispatcher, *, timeout_seconds: float = 900.0
    ) -> None:
        self.dispatcher = dispatcher
        self.timeout_seconds = timeout_seconds

    def __call__(self, query: str, context: Mapping[str, Any]) -> Reply:
        message = context.get("msg")
        message_id = str(
            getattr(message, "msg_id", None)
            or getattr(message, "message_id", None)
            or context.get("message_id")
            or context.get("request_id")
            or f"cow-{os.urandom(16).hex()}"
        )
        inbound = ChannelInboundMessage(
            channel_id=str(context.get("channel_type") or ""),
            conversation_id=str(context.get("session_id") or ""),
            message_id=message_id,
            text=query,
            receiver=str(context.get("receiver") or ""),
            is_group=bool(context.get("isgroup", False)),
        )
        receipt = self.dispatcher.dispatch(inbound)
        outbound = self.dispatcher.wait_for_reply(
            receipt, timeout_seconds=self.timeout_seconds
        )
        if outbound.attachment is None:
            return Reply(ReplyType.TEXT, outbound.text)
        attachment = outbound.attachment
        path = str(attachment["path"])
        content = (
            path
            if path.startswith(("file://", "http://", "https://"))
            else f"file://{path}"
        )
        reply = Reply(
            ReplyType.IMAGE_URL
            if attachment.get("file_type") == "image"
            else ReplyType.FILE,
            content,
        )
        if reply.type is ReplyType.FILE:
            reply.file_name = str(
                attachment.get("file_name") or Path(path.removeprefix("file://")).name
            )
        if outbound.text:
            reply.text_content = outbound.text
        return reply


class CowChannelService:
    """Own the official Cow ChannelManager independently of enterprise state."""

    def __init__(
        self,
        *,
        manager: Any | None = None,
        config: Mapping[str, Any] | None = None,
        config_path: str | Path | None = None,
        bridge: CowChannelRuntimeBridge | None = None,
    ) -> None:
        if manager is None:
            from channel.channel_manager import ChannelManager

            manager = ChannelManager()
        self.manager = manager
        self._config = dict(config) if config is not None else None
        self.config_path = (
            Path(config_path).expanduser().resolve() if config_path else None
        )
        self.bridge = bridge
        self.started = False

    def _settings(self) -> Mapping[str, Any]:
        if self._config is not None:
            return self._config
        if self.config_path is not None and self.config_path.is_file():
            loaded = json.loads(self.config_path.read_text(encoding="utf-8-sig"))
            if not isinstance(loaded, dict):
                raise ValueError("Cow channel config must be an object")
            conf().update(loaded)
        return conf()

    def start_sync(self) -> None:
        if self.started:
            return
        from channel.channel_manager import parse_channel_type

        channels = []
        for raw in parse_channel_type(self._settings().get("channel_type", "")):
            name = normalize_channel_name(raw)
            if name != "web" and name in CHANNEL_CATALOG and name not in channels:
                channels.append(name)
        if self.bridge is not None:
            bind_cow_channel_runtime_bridge(self.bridge)
        try:
            from channel.channel_manager import bind_channel_manager

            bind_channel_manager(self.manager)
            if channels:
                self.manager.start(channels, first_start=True)
        except Exception:
            bind_channel_manager(None)
            if self.bridge is not None:
                unbind_cow_channel_runtime_bridge(self.bridge)
            raise
        self.started = True

    def stop_sync(self) -> None:
        if not self.started:
            return
        try:
            self.manager.stop()
        finally:
            from channel.channel_manager import bind_channel_manager

            bind_channel_manager(None)
            if self.bridge is not None:
                unbind_cow_channel_runtime_bridge(self.bridge)
            self.started = False

    async def start(self) -> None:
        await asyncio.to_thread(self.start_sync)

    async def stop(self) -> None:
        await asyncio.to_thread(self.stop_sync)


__all__ = [
    "CowChannelRuntimeBridge",
    "CowChannelService",
    "bind_cow_channel_runtime_bridge",
    "current_cow_channel_runtime_bridge",
    "unbind_cow_channel_runtime_bridge",
]
