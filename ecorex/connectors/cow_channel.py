"""Thin e-Mate Runtime bridge for CowAgent's native channel lifecycle."""

from __future__ import annotations

import asyncio
import base64
from datetime import UTC, datetime, timedelta
from io import BytesIO
import json
import os
from pathlib import Path
import threading
import time
from typing import Any, Mapping

from bridge.context import Context
from bridge.reply import Reply, ReplyType
from channel.runtime_bridge import (
    bind_runtime_bridge,
    current_runtime_bridge,
    unbind_runtime_bridge,
)
from config import conf

from .channel_catalog import CHANNEL_CATALOG, normalize_channel_name
from .channel_runtime import ChannelInboundMessage, ChannelRuntimeDispatcher
from .channel_self_service import (
    ChannelDeviceAuthorization,
    ChannelDeviceAuthorizationError,
)


_WEIXIN_FLOW_TTL = timedelta(seconds=480)
_WEIXIN_QR_READY_SECONDS = 15.0


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
        self._lock = threading.RLock()
        self._weixin_flow_id: str | None = None
        self._weixin_flow_expires_at: datetime | None = None
        self._weixin_flow_terminal: str | None = None
        self._weixin_qr_cache: tuple[str, str] | None = None

    @staticmethod
    def _sync_live_config(settings: Mapping[str, Any]) -> None:
        live = conf()
        managed = {"channel_type"}
        for name, definition in CHANNEL_CATALOG.items():
            managed.add(f"{name}_display_name")
            managed.update(str(field["key"]) for field in definition.get("fields", ()))
        for key in managed - settings.keys():
            live.pop(key, None)
        live.update(settings)

    def _settings(self) -> Mapping[str, Any]:
        if self._config is not None:
            loaded = dict(self._config)
        elif self.config_path is not None and self.config_path.is_file():
            loaded = json.loads(self.config_path.read_text(encoding="utf-8-sig"))
            if not isinstance(loaded, dict):
                raise ValueError("Cow channel config must be an object")
        elif self.config_path is not None:
            loaded = {}
        else:
            loaded = dict(conf())
        self._sync_live_config(loaded)
        return loaded

    def _write_settings(self, settings: Mapping[str, Any]) -> None:
        value = dict(settings)
        if self._config is not None:
            self._config = value
        elif self.config_path is not None:
            self.config_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.config_path.with_name(
                f".{self.config_path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
            )
            try:
                with temporary.open("x", encoding="utf-8") as stream:
                    json.dump(value, stream, ensure_ascii=False, indent=2, sort_keys=True)
                    stream.write("\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                temporary.chmod(0o600)
                os.replace(temporary, self.config_path)
            finally:
                temporary.unlink(missing_ok=True)
        self._sync_live_config(value)

    @staticmethod
    def _channels(settings: Mapping[str, Any]) -> list[str]:
        from channel.channel_manager import parse_channel_type

        channels: list[str] = []
        for raw in parse_channel_type(settings.get("channel_type", "")):
            name = normalize_channel_name(raw)
            if name != "web" and name in CHANNEL_CATALOG and name not in channels:
                channels.append(name)
        return channels

    @staticmethod
    def _channel(channel_id: str) -> tuple[str, Mapping[str, Any]]:
        name = normalize_channel_name(channel_id)
        if name not in CHANNEL_CATALOG:
            raise ValueError("Cow channel is unknown")
        return name, CHANNEL_CATALOG[name]

    @staticmethod
    def _configured_fields(
        definition: Mapping[str, Any], settings: Mapping[str, Any]
    ) -> tuple[list[str], list[str]]:
        configured: list[str] = []
        missing: list[str] = []
        for field in definition.get("fields", ()):
            key = str(field["key"])
            value = settings.get(key, field.get("default"))
            if key in settings and value not in (None, ""):
                configured.append(key)
            elif field.get("required") is not False and "default" not in field:
                missing.append(key)
        return configured, missing

    def _projection(
        self, channel_id: str, settings: Mapping[str, Any]
    ) -> dict[str, Any] | None:
        name, definition = self._channel(channel_id)
        configured, missing = self._configured_fields(definition, settings)
        enabled = name in self._channels(settings)
        exists = bool(configured or enabled)
        if not exists:
            return None
        channel = self.manager.get_channel(name)
        running = channel is not None
        authenticating = bool(
            name == "weixin"
            and running
            and getattr(channel, "login_status", "") != "logged_in"
        )
        return {
            "instance_id": f"cow-channel-{name}",
            "channel_id": name,
            "display_name": str(
                settings.get(f"{name}_display_name")
                or definition.get("label", {}).get("zh")
                or name
            ),
            "configured_fields": configured,
            "missing_fields": missing,
            "enabled": enabled,
            "state": (
                "unconfigured"
                if missing
                else "starting"
                if authenticating
                else "connected"
                if running
                else "stopped"
            ),
            "health": (
                "unconfigured"
                if missing
                else "authenticating"
                if authenticating
                else "connected"
                if running
                else "disabled"
            ),
            "last_error_code": None,
            "updated_at": None,
        }

    def catalog(self) -> dict[str, Any]:
        with self._lock:
            settings = self._settings()
            items = []
            for name, definition in CHANNEL_CATALOG.items():
                instance = self._projection(name, settings)
                configured = not self._configured_fields(definition, settings)[1]
                enabled = bool(instance and instance["enabled"])
                device = name == "weixin"
                items.append(
                    {
                        "channel_id": name,
                        "label": str(definition.get("label", {}).get("zh") or name),
                        "description": str(definition.get("description") or ""),
                        "icon": str(definition.get("icon") or ""),
                        "auth_kind": (
                            "device_code"
                            if name == "weixin"
                            else "api_token"
                            if name in {"telegram", "slack", "discord"}
                            else "app_credentials"
                        ),
                        "adapter_available": True,
                        "unavailable_reason": None,
                        "fields": [
                            {
                                "key": str(field["key"]),
                                "label": str(field.get("label") or field["key"]),
                                "type": str(field.get("type") or "text"),
                                "required": bool(
                                    field.get("required") is not False
                                    and "default" not in field
                                ),
                                "secret": field.get("type") == "secret",
                                "configured": bool(
                                    instance
                                    and str(field["key"])
                                    in instance["configured_fields"]
                                ),
                                **(
                                    {"default": field["default"]}
                                    if "default" in field
                                    and field.get("type") != "secret"
                                    else {}
                                ),
                            }
                            for field in definition.get("fields", ())
                        ],
                        "instance": instance,
                        "actions": {
                            "save": not device,
                            "test": configured,
                            "enable": configured and not enabled and not device,
                            "disable": enabled,
                            "retry": configured and enabled,
                            "disconnect": instance is not None,
                            "auth_begin": device,
                        },
                    }
                )
            return {"contract_version": "channel-self-service-v1", "items": items}

    def begin_authorization(self, channel_id: str) -> ChannelDeviceAuthorization:
        with self._lock:
            self._require_weixin(channel_id)
            if self._weixin_flow_id is not None:
                current = self._weixin_authorization()
                if current.status in {"pending", "scanned"}:
                    return current if current.verification_url else self._await_weixin_qr()
            self._new_weixin_flow()
            if self.manager.get_channel("weixin") is None:
                if not self.started:
                    raise ChannelDeviceAuthorizationError(
                        "weixin_runtime_not_started", 503
                    )
                self.enable("weixin")
            return self._await_weixin_qr()

    def poll_authorization(
        self, channel_id: str, flow_id: str
    ) -> ChannelDeviceAuthorization:
        with self._lock:
            self._require_weixin_flow(channel_id, flow_id)
            return self._weixin_authorization()

    def cancel_authorization(
        self, channel_id: str, flow_id: str
    ) -> ChannelDeviceAuthorization:
        with self._lock:
            self._require_weixin_flow(channel_id, flow_id)
            current = self._weixin_authorization()
            if current.status == "confirmed":
                raise ChannelDeviceAuthorizationError(
                    "weixin_device_flow_confirmed", 409
                )
            self.disable("weixin")
            self._weixin_flow_terminal = "cancelled"
            return self._weixin_authorization()

    def refresh_authorization(
        self, channel_id: str, flow_id: str
    ) -> ChannelDeviceAuthorization:
        with self._lock:
            self._require_weixin_flow(channel_id, flow_id)
            if self._weixin_authorization().status == "confirmed":
                raise ChannelDeviceAuthorizationError(
                    "weixin_device_flow_confirmed", 409
                )
            self._new_weixin_flow(flow_id)
            if not self.started:
                raise ChannelDeviceAuthorizationError(
                    "weixin_runtime_not_started", 503
                )
            if self.manager.get_channel("weixin") is None:
                self.enable("weixin")
            else:
                self.manager.restart("weixin")
            return self._await_weixin_qr()

    @staticmethod
    def _require_weixin(channel_id: str) -> None:
        if normalize_channel_name(channel_id) != "weixin":
            raise ChannelDeviceAuthorizationError(
                "channel_device_authorization_unsupported", 409
            )

    def _require_weixin_flow(self, channel_id: str, flow_id: str) -> None:
        self._require_weixin(channel_id)
        if flow_id != self._weixin_flow_id:
            raise ChannelDeviceAuthorizationError(
                "channel_device_flow_invalid", 422
            )

    def _new_weixin_flow(self, flow_id: str | None = None) -> None:
        self._weixin_flow_id = flow_id or f"wxauth_{os.urandom(16).hex()}"
        self._weixin_flow_expires_at = datetime.now(UTC) + _WEIXIN_FLOW_TTL
        self._weixin_flow_terminal = None
        self._weixin_qr_cache = None

    def _await_weixin_qr(self) -> ChannelDeviceAuthorization:
        deadline = time.monotonic() + _WEIXIN_QR_READY_SECONDS
        while True:
            result = self._weixin_authorization()
            if result.status == "confirmed" or result.verification_url:
                return result
            if time.monotonic() >= deadline:
                raise ChannelDeviceAuthorizationError(
                    "weixin_qrcode_unavailable", 502
                )
            time.sleep(0.05)

    def _weixin_authorization(self) -> ChannelDeviceAuthorization:
        if self._weixin_flow_id is None or self._weixin_flow_expires_at is None:
            raise ChannelDeviceAuthorizationError(
                "channel_device_flow_invalid", 422
            )
        channel = self.manager.get_channel("weixin")
        login_status = str(getattr(channel, "login_status", ""))
        if self._weixin_flow_terminal is not None:
            status = self._weixin_flow_terminal
        elif login_status == "logged_in":
            status = "confirmed"
        elif login_status == "scanned":
            status = "scanned"
        elif datetime.now(UTC) >= self._weixin_flow_expires_at:
            status = "expired"
        else:
            status = "pending"
        verification_url = (
            str(getattr(channel, "_current_qr_url", "") or "")
            if status in {"pending", "scanned"}
            else ""
        )
        qr_image_data_url = None
        if verification_url:
            if self._weixin_qr_cache is None or self._weixin_qr_cache[0] != verification_url:
                self._weixin_qr_cache = (
                    verification_url,
                    _qr_png_data_url(verification_url),
                )
            qr_image_data_url = self._weixin_qr_cache[1]
        return ChannelDeviceAuthorization(
            flow_id=self._weixin_flow_id,
            status=status,
            verification_url=verification_url or None,
            qr_image_data_url=qr_image_data_url,
            expires_at=self._weixin_flow_expires_at,
        )

    def save(
        self,
        channel_id: str,
        *,
        display_name: str,
        config: Mapping[str, Any],
        secrets: Mapping[str, str],
    ) -> dict[str, Any]:
        with self._lock:
            name, definition = self._channel(channel_id)
            fields = {str(field["key"]): field for field in definition.get("fields", ())}
            values = {**dict(config), **dict(secrets)}
            if set(values) - set(fields):
                raise ValueError("Cow channel config field is invalid")
            settings = dict(self._settings())
            for key, value in values.items():
                field = fields[key]
                if field.get("type") == "number":
                    if isinstance(value, bool) or not isinstance(value, int):
                        raise ValueError("Cow channel config value is invalid")
                elif not isinstance(value, str) or not value or any(
                    character in value for character in ("\x00", "\r", "\n")
                ):
                    raise ValueError("Cow channel config value is invalid")
                settings[key] = value
            if display_name:
                settings[f"{name}_display_name"] = display_name
            self._write_settings(settings)
            if self.started and name in self._channels(settings):
                self.manager.restart(name)
            return self._projection(name, settings) or {}

    def enable(self, channel_id: str) -> dict[str, Any]:
        with self._lock:
            name, definition = self._channel(channel_id)
            settings = dict(self._settings())
            _, missing = self._configured_fields(definition, settings)
            if missing:
                raise ValueError("Cow channel is not configured")
            channels = self._channels(settings)
            if name not in channels:
                channels.append(name)
                settings["channel_type"] = ",".join(channels)
                self._write_settings(settings)
                if self.started:
                    self.manager.add_channel(name)
            return self._projection(name, settings) or {}

    def disable(self, channel_id: str) -> dict[str, Any]:
        with self._lock:
            name, _ = self._channel(channel_id)
            settings = dict(self._settings())
            channels = self._channels(settings)
            if name in channels:
                channels.remove(name)
                if channels:
                    settings["channel_type"] = ",".join(channels)
                else:
                    settings.pop("channel_type", None)
                self._write_settings(settings)
                if self.started:
                    self.manager.remove_channel(name)
            return self._projection(name, settings) or {}

    def restart(self, channel_id: str) -> dict[str, Any]:
        with self._lock:
            name, _ = self._channel(channel_id)
            settings = dict(self._settings())
            if name not in self._channels(settings):
                raise ValueError("Cow channel is disabled")
            if self.started:
                self.manager.restart(name)
            return self._projection(name, settings) or {}

    def health(self, channel_id: str) -> dict[str, Any]:
        with self._lock:
            name, _ = self._channel(channel_id)
            return self._projection(name, self._settings()) or {}

    def remove(self, channel_id: str) -> None:
        with self._lock:
            name, definition = self._channel(channel_id)
            settings = dict(self._settings())
            if self.started:
                self.manager.remove_channel(name)
            channels = self._channels(settings)
            if name in channels:
                channels.remove(name)
            if channels:
                settings["channel_type"] = ",".join(channels)
            else:
                settings.pop("channel_type", None)
            settings.pop(f"{name}_display_name", None)
            for field in definition.get("fields", ()):
                settings.pop(str(field["key"]), None)
            self._write_settings(settings)

    def send_outbound(
        self,
        channel_id: str,
        *,
        conversation_id: str,
        receiver: str,
        is_group: bool,
        text: str = "",
        attachment: Mapping[str, Any] | None = None,
    ) -> None:
        name, _ = self._channel(channel_id)
        channel = self.manager.get_channel(name)
        if channel is None:
            raise RuntimeError("Cow channel is not running")
        context = Context(
            kwargs={
                "channel_type": name,
                "session_id": conversation_id,
                "receiver": receiver or conversation_id,
                "isgroup": bool(is_group),
                "msg": None,
            }
        )
        destination = receiver or conversation_id
        if name == "telegram":
            context["telegram_chat_id"] = destination
        elif name == "slack":
            context["slack_channel"] = destination
        elif name == "discord":
            context["discord_channel_id"] = (
                int(destination) if destination.isdecimal() else destination
            )
        if name == "feishu":
            context["receive_id_type"] = "chat_id" if is_group else "open_id"
        if text:
            channel.send(Reply(ReplyType.TEXT, text), context)
        if attachment:
            path = str(attachment.get("path") or "")
            if not path:
                raise ValueError("Cow channel attachment path is missing")
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
                    attachment.get("file_name")
                    or Path(path.removeprefix("file://")).name
                )
            channel.send(reply, context)

    def start_sync(self) -> None:
        if self.started:
            return
        channels = self._channels(self._settings())
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


def _qr_png_data_url(value: str) -> str:
    try:
        import qrcode
    except ImportError:
        raise ChannelDeviceAuthorizationError(
            "weixin_qrcode_dependency_missing"
        ) from None
    qr = qrcode.QRCode(
        error_correction=qrcode.constants.ERROR_CORRECT_L,
        box_size=6,
        border=4,
    )
    qr.add_data(value)
    qr.make(fit=True)
    image = qr.make_image(fill_color="black", back_color="white")
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode()


__all__ = [
    "CowChannelRuntimeBridge",
    "CowChannelService",
    "bind_cow_channel_runtime_bridge",
    "current_cow_channel_runtime_bridge",
    "unbind_cow_channel_runtime_bridge",
]
