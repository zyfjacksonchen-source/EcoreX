"""Channel settings API; product mode delegates lifecycle to Cow ChannelManager."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
import hashlib
import json
import re
import secrets
import threading
from typing import Any, Protocol

from fastapi import APIRouter, Header, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict, Field

from .channel_catalog import CHANNEL_CATALOG, normalize_channel_name
from .models import ConnectorAuthKind, ConnectorHealth, ConnectorHealthResult
from .repository import ConnectorOutboxEvent
from .vault import CredentialVault


_CONTRACT_VERSION = "channel-self-service-v1"
_ERROR_CODE_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,191}$")


class ChannelState(StrEnum):
    UNCONFIGURED = "unconfigured"
    STOPPED = "stopped"
    STARTING = "starting"
    CONNECTED = "connected"
    DEGRADED = "degraded"
    ERROR = "error"
    STOPPING = "stopping"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class ChannelCredentialOwner:
    account_id: str
    organization_id: str

    def __post_init__(self) -> None:
        for value in (self.account_id, self.organization_id):
            if (
                not isinstance(value, str)
                or not value.strip()
                or len(value) > 256
                or any(character in value for character in ("\x00", "\r", "\n"))
            ):
                raise ValueError("channel credential owner is invalid")


@dataclass(frozen=True, slots=True)
class ChannelAuditEvent:
    account_id: str
    organization_id: str
    channel_id: str
    action: str
    outcome: str
    request_id: str | None
    field_names: tuple[str, ...]
    error_code: str | None
    created_at: datetime

    def to_dict(self) -> dict[str, Any]:
        return {
            "account_id": self.account_id,
            "organization_id": self.organization_id,
            "channel_id": self.channel_id,
            "action": self.action,
            "outcome": self.outcome,
            "request_id": self.request_id,
            "field_names": list(self.field_names),
            "error_code": self.error_code,
            "created_at": self.created_at.isoformat(),
        }


class ChannelLifecycleAdapter(Protocol):
    """A packaged adapter; network checks must be real, never projections."""

    def test(self, config: Mapping[str, Any]) -> ConnectorHealthResult:
        ...

    def start(self, config: Mapping[str, Any]) -> ConnectorHealthResult:
        ...

    def health(self) -> ConnectorHealthResult:
        ...

    def stop(self, timeout_seconds: float) -> bool:
        ...


@dataclass(frozen=True, slots=True)
class ChannelDeviceAuthorization:
    """One short-lived public device flow plus private confirmed material."""

    flow_id: str
    status: str
    verification_url: str | None
    qr_image_data_url: str | None
    expires_at: datetime
    config: Mapping[str, Any] | None = None
    secrets: Mapping[str, str] | None = None


class ChannelDeviceLifecycleAdapter(ChannelLifecycleAdapter, Protocol):
    def begin_authorization(self) -> ChannelDeviceAuthorization:
        ...

    def poll_authorization(self, flow_id: str) -> ChannelDeviceAuthorization:
        ...

    def cancel_authorization(self, flow_id: str) -> ChannelDeviceAuthorization:
        ...

    def refresh_authorization(self, flow_id: str) -> ChannelDeviceAuthorization:
        ...

    def consume_authorization(self, flow_id: str) -> None:
        ...

    def prepare_replace(self, config: Mapping[str, Any]) -> ConnectorHealthResult:
        """Validate a confirmed session without exposing inbound messages."""
        ...

    def commit_replace(self, timeout_seconds: float) -> ConnectorHealthResult:
        """Select the staged session after its credential generation is durable."""
        ...

    def abort_replace(self) -> None:
        ...


class ChannelDeviceAuthorizationError(RuntimeError):
    def __init__(self, code: str, http_status: int = 502) -> None:
        super().__init__(code)
        self.code = code
        self.http_status = http_status


class ChannelSelfServiceError(RuntimeError):
    def __init__(self, code: str, http_status: int) -> None:
        super().__init__(code)
        self.code = code
        self.http_status = http_status


@dataclass(frozen=True, slots=True)
class _StoredChannel:
    display_name: str
    enabled: bool
    config: Mapping[str, Any]
    secrets: Mapping[str, str]
    updated_at: datetime


class ChannelSelfService:
    """UI contract adapter; CowChannelService is product lifecycle authority."""

    def __init__(
        self,
        *,
        owner: ChannelCredentialOwner,
        vault: CredentialVault,
        adapters: Mapping[str, ChannelLifecycleAdapter] | None = None,
        audit_sink: Callable[[ChannelAuditEvent], None] | None = None,
        oauth_channels: frozenset[str] = frozenset(),
        oauth_available: frozenset[str] = frozenset(),
        stop_timeout_seconds: float = 5.0,
        native_service: Any | None = None,
    ) -> None:
        if not 0.1 <= stop_timeout_seconds <= 300:
            raise ValueError("channel stop timeout is invalid")
        self.owner = owner
        self.vault = vault
        self.adapters = {
            normalize_channel_name(channel_id): adapter
            for channel_id, adapter in dict(adapters or {}).items()
        }
        unknown = set(self.adapters) - set(CHANNEL_CATALOG)
        if unknown:
            raise ValueError("unknown channel lifecycle adapter")
        self.audit_sink = audit_sink
        self.oauth_channels = frozenset(
            normalize_channel_name(channel_id) for channel_id in oauth_channels
        )
        self.oauth_available = frozenset(
            normalize_channel_name(channel_id) for channel_id in oauth_available
        )
        if self.oauth_available - self.oauth_channels:
            raise ValueError("OAuth availability includes a non-OAuth channel")
        self.stop_timeout_seconds = float(stop_timeout_seconds)
        self.native_service = native_service
        self._states: dict[str, tuple[ChannelState, ConnectorHealth, str | None]] = {}
        self._lock = threading.RLock()
        self._device_lock = threading.Lock()

    def catalog(self) -> dict[str, Any]:
        if self.native_service is not None:
            return self.native_service.catalog()
        items: list[dict[str, Any]] = []
        for channel_id, definition in CHANNEL_CATALOG.items():
            stored = self._read(channel_id)
            fields = self._fields(channel_id)
            oauth = channel_id in self.oauth_channels
            device = self._auth_kind(channel_id) is ConnectorAuthKind.DEVICE_CODE
            adapter_available = (
                channel_id in self.oauth_available
                if oauth
                else channel_id in self.adapters
            )
            if not adapter_available:
                unavailable_reason = "adapter_not_packaged"
            else:
                unavailable_reason = None
            instance = self._projection(channel_id, stored) if stored else None
            configured = bool(instance and not instance["missing_fields"])
            items.append(
                {
                    "channel_id": channel_id,
                    "label": str(
                        (definition.get("label") or {}).get("zh") or channel_id
                    ),
                    "description": str(definition.get("description") or ""),
                    "icon": str(definition.get("icon") or ""),
                    "auth_kind": self._auth_kind(channel_id).value,
                    "adapter_available": adapter_available,
                    "unavailable_reason": unavailable_reason,
                    "fields": [
                        self._public_field(field, stored)
                        for field in fields
                        if not oauth
                    ],
                    "instance": instance,
                    "actions": {
                        "save": bool(adapter_available and not oauth and not device),
                        "test": bool(adapter_available and configured and not oauth and not device),
                        "enable": bool(
                            adapter_available
                            and configured
                            and not oauth
                            and not device
                            and not instance["enabled"]
                        ),
                        "disable": bool(
                            adapter_available
                            and configured
                            and not oauth
                            and instance["enabled"]
                        ),
                        "retry": bool(adapter_available and configured and not oauth and not device),
                        "disconnect": bool(instance and not oauth),
                        "auth_begin": bool((oauth or device) and adapter_available),
                    },
                }
            )
        return {"contract_version": _CONTRACT_VERSION, "items": items}

    async def start(self) -> None:
        if self.native_service is not None:
            return
        await asyncio.to_thread(self._restore_enabled)

    async def stop(self) -> None:
        if self.native_service is not None:
            return
        await asyncio.to_thread(self._shutdown)

    def _restore_enabled(self) -> None:
        """Restore only previously enabled channels with packaged adapters."""

        for channel_id in self.adapters:
            if self._auth_kind(channel_id) is ConnectorAuthKind.DEVICE_CODE:
                try:
                    self._recover_device_transition(channel_id)
                except ChannelSelfServiceError as error:
                    with self._lock:
                        self._states[channel_id] = (
                            ChannelState.ERROR,
                            ConnectorHealth.ERROR,
                            error.code,
                        )
                    continue
            stored = self._read(channel_id)
            if (
                stored is None
                or not stored.enabled
                or self._missing_fields(channel_id, stored)
            ):
                continue
            try:
                result = self._call_adapter(
                    channel_id,
                    "auto_start",
                    None,
                    lambda adapter=self.adapters[channel_id], record=stored: adapter.start(
                        self._material(record)
                    ),
                )
            except ChannelSelfServiceError:
                continue
            with self._lock:
                self._states[channel_id] = (
                    _state_for_health(result.health),
                    result.health,
                    _error_code(result.error_code),
                )
            self._audit(
                channel_id, "auto_start", "succeeded", None, (), result.error_code
            )

    def _shutdown(self) -> None:
        """Gracefully stop owned adapters; never inject an async exception."""

        failed = False
        for channel_id in self.adapters:
            state = self._states.get(channel_id)
            if state is None or state[0] not in {
                ChannelState.STARTING,
                ChannelState.CONNECTED,
                ChannelState.DEGRADED,
                ChannelState.ERROR,
            }:
                continue
            try:
                self._stop(channel_id, action="shutdown", request_id=None)
            except ChannelSelfServiceError:
                failed = True
        if failed:
            raise RuntimeError("channel_shutdown_failed")

    def save(
        self,
        channel_id: str,
        *,
        display_name: str,
        config: Mapping[str, Any],
        secrets: Mapping[str, str],
        request_id: str | None,
    ) -> dict[str, Any]:
        if self.native_service is not None:
            channel_id = self._writable_channel(channel_id)
            request_id = _request_id(request_id)
            try:
                result = self.native_service.save(
                    channel_id,
                    display_name=_display_name(display_name, channel_id),
                    config=config,
                    secrets=secrets,
                )
            except ValueError:
                raise ChannelSelfServiceError(
                    "channel_config_value_invalid", 422
                ) from None
            except RuntimeError:
                raise ChannelSelfServiceError("channel_adapter_failed", 502) from None
            self._audit(
                channel_id,
                "save",
                "succeeded",
                request_id,
                tuple(sorted(set(config) | set(secrets))),
                None,
            )
            return result
        channel_id = self._writable_channel(channel_id)
        if self._auth_kind(channel_id) is ConnectorAuthKind.DEVICE_CODE:
            raise ChannelSelfServiceError("channel_device_authorization_required", 409)
        if channel_id not in self.adapters:
            raise ChannelSelfServiceError("channel_adapter_unavailable", 503)
        request_id = _request_id(request_id)
        with self._lock:
            previous = self._read(channel_id)
            if previous is not None and previous.enabled:
                raise ChannelSelfServiceError("channel_must_be_disabled", 409)
            public_fields, secret_fields = self._field_maps(channel_id)
            if set(config) - set(public_fields) or set(secrets) - set(secret_fields):
                raise ChannelSelfServiceError("channel_config_field_invalid", 422)
            clean_config = dict(previous.config) if previous else {}
            clean_secrets = dict(previous.secrets) if previous else {}
            for key, value in config.items():
                clean_config[key] = self._field_value(public_fields[key], value)
            for key, value in secrets.items():
                clean_secrets[key] = _secret_value(value)
            now = datetime.now(UTC)
            stored = _StoredChannel(
                display_name=_display_name(display_name, channel_id),
                enabled=bool(previous.enabled) if previous else False,
                config=clean_config,
                secrets=clean_secrets,
                updated_at=now,
            )
            self._vault_put(channel_id, stored)
            missing = self._missing_fields(channel_id, stored)
            self._states[channel_id] = (
                ChannelState.UNCONFIGURED
                if missing
                else (
                    ChannelState.STOPPED
                    if channel_id in self.adapters
                    else ChannelState.UNAVAILABLE
                ),
                ConnectorHealth.DISABLED
                if not stored.enabled or channel_id not in self.adapters
                else ConnectorHealth.UNCONFIGURED,
                None,
            )
        self._audit(
            channel_id,
            "save",
            "succeeded",
            request_id,
            tuple(sorted(set(config) | set(secrets))),
            None,
        )
        return self._projection(channel_id, stored)

    def test(self, channel_id: str, *, request_id: str | None) -> dict[str, Any]:
        if self.native_service is not None:
            return self.health(channel_id, request_id=request_id)
        channel_id, stored, adapter = self._ready(channel_id, request_id)
        result = self._call_adapter(
            channel_id,
            "test",
            request_id,
            lambda: adapter.test(self._material(stored)),
        )
        state = ChannelState.STOPPED if result.health == ConnectorHealth.CONNECTED else _state_for_health(result.health)
        with self._lock:
            self._states[channel_id] = (state, result.health, _error_code(result.error_code))
        self._audit(channel_id, "test", "succeeded", request_id, (), result.error_code)
        return self._projection(channel_id, stored)

    def enable(self, channel_id: str, *, request_id: str | None) -> dict[str, Any]:
        if self.native_service is not None:
            channel_id = self._writable_channel(channel_id)
            request_id = _request_id(request_id)
            try:
                result = self.native_service.enable(channel_id)
            except ValueError:
                raise ChannelSelfServiceError("channel_not_configured", 409) from None
            except RuntimeError:
                raise ChannelSelfServiceError("channel_adapter_failed", 502) from None
            self._audit(channel_id, "enable", "succeeded", request_id, (), None)
            return result
        channel_id, stored, adapter = self._ready(channel_id, request_id)
        stored = self._set_enabled(channel_id, stored, True)
        with self._lock:
            self._states[channel_id] = (
                ChannelState.STARTING,
                ConnectorHealth.AUTHENTICATING,
                None,
            )
        result = self._call_adapter(
            channel_id,
            "enable",
            request_id,
            lambda: adapter.start(self._material(stored)),
        )
        with self._lock:
            self._states[channel_id] = (
                _state_for_health(result.health),
                result.health,
                _error_code(result.error_code),
            )
        self._audit(channel_id, "enable", "succeeded", request_id, (), result.error_code)
        return self._projection(channel_id, stored)

    def disable(self, channel_id: str, *, request_id: str | None) -> dict[str, Any]:
        if self.native_service is not None:
            channel_id = self._writable_channel(channel_id)
            request_id = _request_id(request_id)
            try:
                result = self.native_service.disable(channel_id)
            except RuntimeError:
                raise ChannelSelfServiceError("channel_adapter_failed", 502) from None
            self._audit(channel_id, "disable", "succeeded", request_id, (), None)
            return result
        channel_id = self._writable_channel(channel_id)
        request_id = _request_id(request_id)
        stored = self._required_stored(channel_id)
        self._stop(channel_id, action="disable", request_id=request_id)
        stored = self._set_enabled(channel_id, stored, False)
        with self._lock:
            self._states[channel_id] = (
                ChannelState.STOPPED,
                ConnectorHealth.DISABLED,
                None,
            )
        self._audit(channel_id, "disable", "succeeded", request_id, (), None)
        return self._projection(channel_id, stored)

    def retry(self, channel_id: str, *, request_id: str | None) -> dict[str, Any]:
        if self.native_service is not None:
            channel_id = self._writable_channel(channel_id)
            request_id = _request_id(request_id)
            try:
                result = self.native_service.restart(channel_id)
            except ValueError:
                raise ChannelSelfServiceError("channel_not_enabled", 409) from None
            except RuntimeError:
                raise ChannelSelfServiceError("channel_adapter_failed", 502) from None
            self._audit(channel_id, "retry", "succeeded", request_id, (), None)
            return result
        channel_id, stored, adapter = self._ready(channel_id, request_id)
        self._stop(channel_id, action="retry", request_id=request_id)
        resolve_uncertain = getattr(adapter, "resolve_uncertain", None)
        if callable(resolve_uncertain):
            try:
                resolve_uncertain()
            except Exception:
                self._audit(
                    channel_id,
                    "retry",
                    "failed",
                    request_id,
                    (),
                    "channel_recovery_failed",
                )
                raise ChannelSelfServiceError("channel_recovery_failed", 503) from None
        stored = self._set_enabled(channel_id, stored, True)
        with self._lock:
            self._states[channel_id] = (
                ChannelState.STARTING,
                ConnectorHealth.AUTHENTICATING,
                None,
            )
        result = self._call_adapter(
            channel_id,
            "retry",
            request_id,
            lambda: adapter.start(self._material(stored)),
        )
        with self._lock:
            self._states[channel_id] = (
                _state_for_health(result.health),
                result.health,
                _error_code(result.error_code),
            )
        self._audit(channel_id, "retry", "succeeded", request_id, (), result.error_code)
        return self._projection(channel_id, stored)

    def health(self, channel_id: str, *, request_id: str | None) -> dict[str, Any]:
        if self.native_service is not None:
            channel_id = self._writable_channel(channel_id)
            request_id = _request_id(request_id)
            result = self.native_service.health(channel_id)
            self._audit(channel_id, "health", "succeeded", request_id, (), None)
            return result
        channel_id, stored, adapter = self._ready(channel_id, request_id)
        result = self._call_adapter(
            channel_id, "health", request_id, adapter.health
        )
        with self._lock:
            self._states[channel_id] = (
                _state_for_health(result.health),
                result.health,
                _error_code(result.error_code),
            )
        self._audit(channel_id, "health", "succeeded", request_id, (), result.error_code)
        return self._projection(channel_id, stored)

    def disconnect(self, channel_id: str, *, request_id: str | None) -> None:
        if self.native_service is not None:
            channel_id = self._writable_channel(channel_id)
            request_id = _request_id(request_id)
            try:
                self.native_service.remove(channel_id)
            except RuntimeError:
                raise ChannelSelfServiceError("channel_adapter_failed", 502) from None
            self._audit(channel_id, "disconnect", "succeeded", request_id, (), None)
            return
        channel_id = self._writable_channel(channel_id)
        request_id = _request_id(request_id)
        if self._read(channel_id) is None:
            self._audit(channel_id, "disconnect", "succeeded", request_id, (), None)
            return
        self._stop(channel_id, action="disconnect", request_id=request_id)
        self._vault_delete(channel_id)
        with self._lock:
            self._states.pop(channel_id, None)
        self._audit(channel_id, "disconnect", "succeeded", request_id, (), None)

    def begin_authorization(
        self, channel_id: str, *, request_id: str | None
    ) -> dict[str, Any]:
        channel_id, adapter = self._device_adapter(channel_id)
        request_id = _request_id(request_id)
        result = self._call_device(
            channel_id, "auth_begin", request_id, adapter.begin_authorization
        )
        self._audit(channel_id, "auth_begin", "succeeded", request_id, (), None)
        return self._device_projection(channel_id, result)

    def poll_authorization(
        self, channel_id: str, flow_id: str, *, request_id: str | None
    ) -> dict[str, Any]:
        channel_id, adapter = self._device_adapter(channel_id)
        request_id = _request_id(request_id)
        flow_id = _device_flow_id(flow_id)
        result = self._call_device(
            channel_id,
            "auth_poll",
            request_id,
            lambda: adapter.poll_authorization(flow_id),
        )
        instance = None
        if result.status == "confirmed":
            with self._device_lock:
                replacing = self._read(channel_id)
                if result.config is None and result.secrets is None:
                    stored = self._required_stored(channel_id)
                else:
                    stored = self._confirmed_device_record(channel_id, result)
                    old_reference = self._active_generation_reference(channel_id)
                    if replacing is not None and old_reference is None:
                        old_reference = self._new_generation_reference(channel_id)
                    new_reference = self._new_generation_reference(channel_id)
                    self._begin_device_transition(
                        channel_id,
                        flow_id,
                        old_reference,
                        new_reference,
                    )
                    pointer_committed = False
                    try:
                        if replacing is not None and self._active_generation_reference(
                            channel_id
                        ) is None:
                            assert old_reference is not None
                            self._vault_put_raw(
                                old_reference, self._encode(channel_id, replacing)
                            )
                            self._set_generation_pointer(channel_id, old_reference)
                        prepared = self._call_adapter(
                            channel_id,
                            "auth_prepare",
                            request_id,
                            lambda: adapter.prepare_replace(self._material(stored)),
                        )
                        if prepared.health is not ConnectorHealth.CONNECTED:
                            raise ChannelSelfServiceError(
                                _error_code(prepared.error_code)
                                or "channel_device_prepare_failed",
                                502,
                            )
                        self._consume_device_authorization(
                            channel_id,
                            flow_id,
                            request_id,
                            adapter,
                        )
                        self._vault_put_raw(
                            new_reference, self._encode(channel_id, stored)
                        )
                        self._set_generation_pointer(channel_id, new_reference)
                        pointer_committed = True
                        started = self._call_adapter(
                            channel_id,
                            "auth_commit",
                            request_id,
                            lambda: adapter.commit_replace(
                                self.stop_timeout_seconds
                            ),
                        )
                        if started.health is not ConnectorHealth.CONNECTED:
                            raise ChannelSelfServiceError(
                                _error_code(started.error_code)
                                or "channel_device_start_failed",
                                502,
                            )
                    except Exception:
                        try:
                            adapter.abort_replace()
                        except Exception:
                            pass
                        if pointer_committed:
                            try:
                                if old_reference is None:
                                    self._vault_delete_raw(self._reference(channel_id))
                                else:
                                    self._set_generation_pointer(
                                        channel_id, old_reference
                                    )
                            except Exception:
                                try:
                                    adapter.stop(self.stop_timeout_seconds)
                                except Exception:
                                    pass
                                raise ChannelSelfServiceError(
                                    "channel_device_rollback_failed", 503
                                ) from None
                        if self._delete_generation(new_reference):
                            self._clear_device_transition(channel_id)
                        raise
                    with self._lock:
                        self._states[channel_id] = (
                            _state_for_health(started.health),
                            started.health,
                            _error_code(started.error_code),
                        )
                    cleaned = old_reference is None or self._delete_generation(
                        old_reference
                    )
                    if cleaned:
                        self._clear_device_transition(channel_id)
                instance = self._projection(channel_id, stored)
        self._audit(channel_id, "auth_poll", "succeeded", request_id, (), None)
        projection = self._device_projection(channel_id, result)
        if instance is not None:
            projection["instance"] = instance
        return projection

    def cancel_authorization(
        self, channel_id: str, flow_id: str, *, request_id: str | None
    ) -> dict[str, Any]:
        channel_id, adapter = self._device_adapter(channel_id)
        request_id = _request_id(request_id)
        result = self._call_device(
            channel_id,
            "auth_cancel",
            request_id,
            lambda: adapter.cancel_authorization(_device_flow_id(flow_id)),
        )
        self._audit(channel_id, "auth_cancel", "succeeded", request_id, (), None)
        return self._device_projection(channel_id, result)

    def refresh_authorization(
        self, channel_id: str, flow_id: str, *, request_id: str | None
    ) -> dict[str, Any]:
        channel_id, adapter = self._device_adapter(channel_id)
        request_id = _request_id(request_id)
        result = self._call_device(
            channel_id,
            "auth_refresh",
            request_id,
            lambda: adapter.refresh_authorization(_device_flow_id(flow_id)),
        )
        self._audit(channel_id, "auth_refresh", "succeeded", request_id, (), None)
        return self._device_projection(channel_id, result)

    def _device_adapter(
        self, channel_id: str
    ) -> tuple[str, ChannelDeviceLifecycleAdapter]:
        channel_id = normalize_channel_name(channel_id)
        if channel_id not in CHANNEL_CATALOG:
            raise ChannelSelfServiceError("channel_not_found", 404)
        if self._auth_kind(channel_id) is not ConnectorAuthKind.DEVICE_CODE:
            raise ChannelSelfServiceError("channel_device_authorization_unsupported", 409)
        adapter = self.adapters.get(channel_id)
        if adapter is None or not all(
            callable(getattr(adapter, name, None))
            for name in (
                "begin_authorization",
                "poll_authorization",
                "cancel_authorization",
                "refresh_authorization",
                "consume_authorization",
                "prepare_replace",
                "commit_replace",
                "abort_replace",
            )
        ):
            raise ChannelSelfServiceError("channel_adapter_unavailable", 503)
        return channel_id, adapter  # type: ignore[return-value]

    def _call_device(
        self,
        channel_id: str,
        operation: str,
        request_id: str | None,
        call: Callable[[], ChannelDeviceAuthorization],
    ) -> ChannelDeviceAuthorization:
        try:
            result = call()
        except ChannelDeviceAuthorizationError as error:
            self._audit(channel_id, operation, "failed", request_id, (), error.code)
            raise ChannelSelfServiceError(error.code, error.http_status) from None
        except Exception:
            self._audit(
                channel_id,
                operation,
                "failed",
                request_id,
                (),
                "channel_adapter_failed",
            )
            raise ChannelSelfServiceError("channel_adapter_failed", 502) from None
        _validate_device_authorization(result)
        return result

    def _confirmed_device_record(
        self, channel_id: str, result: ChannelDeviceAuthorization
    ) -> _StoredChannel:
        if result.config is None or result.secrets is None:
            raise ChannelSelfServiceError("channel_adapter_result_invalid", 502)
        public_fields, secret_fields = self._stored_field_maps(channel_id)
        if set(result.config) != set(public_fields) or set(result.secrets) != set(secret_fields):
            raise ChannelSelfServiceError("channel_adapter_result_invalid", 502)
        stored = _StoredChannel(
            display_name=_display_name("", channel_id),
            enabled=True,
            config={
                key: self._field_value(public_fields[key], value)
                for key, value in result.config.items()
            },
            secrets={key: _secret_value(value) for key, value in result.secrets.items()},
            updated_at=datetime.now(UTC),
        )
        return stored

    def _consume_device_authorization(
        self,
        channel_id: str,
        flow_id: str,
        request_id: str | None,
        adapter: ChannelDeviceLifecycleAdapter,
    ) -> None:
        try:
            adapter.consume_authorization(flow_id)
        except ChannelDeviceAuthorizationError as error:
            self._audit(
                channel_id,
                "auth_consume",
                "failed",
                request_id,
                (),
                error.code,
            )
            raise ChannelSelfServiceError(error.code, error.http_status) from None
        except Exception:
            self._audit(
                channel_id,
                "auth_consume",
                "failed",
                request_id,
                (),
                "channel_adapter_failed",
            )
            raise ChannelSelfServiceError("channel_adapter_failed", 502) from None

    def _begin_device_transition(
        self,
        channel_id: str,
        flow_id: str,
        old_reference: str | None,
        new_reference: str,
    ) -> None:
        try:
            self.vault.put(
                self._transition_reference(channel_id),
                {
                    "schema_version": "1",
                    "channel_id": channel_id,
                    "state": "preparing",
                    "flow_sha256": hashlib.sha256(flow_id.encode()).hexdigest(),
                    "old_generation_reference": old_reference or "",
                    "new_generation_reference": new_reference,
                    "updated_at": datetime.now(UTC).isoformat(),
                },
            )
        except Exception:
            raise ChannelSelfServiceError("channel_vault_unavailable", 503) from None

    def _clear_device_transition(self, channel_id: str) -> None:
        try:
            self.vault.delete(self._transition_reference(channel_id))
        except Exception:
            # The channel record remains the authority; a later startup retries cleanup.
            pass

    def _recover_device_transition(self, channel_id: str) -> None:
        try:
            transition = self.vault.get(self._transition_reference(channel_id))
        except KeyError:
            return
        except Exception:
            raise ChannelSelfServiceError("channel_vault_unavailable", 503) from None
        expected = {
            "schema_version",
            "channel_id",
            "state",
            "flow_sha256",
            "old_generation_reference",
            "new_generation_reference",
            "updated_at",
        }
        try:
            if (
                set(transition) != expected
                or transition["schema_version"] != "1"
                or transition["channel_id"] != channel_id
                or transition["state"] != "preparing"
            ):
                raise ValueError
            old_reference = transition["old_generation_reference"] or None
            new_reference = transition["new_generation_reference"]
            if old_reference is not None:
                self._validate_generation_reference(channel_id, old_reference)
            self._validate_generation_reference(channel_id, new_reference)
        except Exception:
            raise ChannelSelfServiceError(
                "channel_device_transition_invalid", 503
            ) from None
        active = self._active_generation_reference(channel_id)
        if active == new_reference:
            cleaned = old_reference is None or self._delete_generation(old_reference)
        else:
            cleaned = self._delete_generation(new_reference)
            if active is None and old_reference is not None:
                cleaned = self._delete_generation(old_reference) and cleaned
        if cleaned:
            self._clear_device_transition(channel_id)

    def _device_projection(
        self, channel_id: str, result: ChannelDeviceAuthorization
    ) -> dict[str, Any]:
        return {
            "channel_id": channel_id,
            "flow_id": result.flow_id,
            "status": result.status,
            "verification_url": (
                result.verification_url
                if result.status in {"pending", "scanned"}
                else None
            ),
            "qr_image_data_url": (
                result.qr_image_data_url
                if result.status in {"pending", "scanned"}
                else None
            ),
            "expires_at": result.expires_at.astimezone(UTC).isoformat(),
        }

    def _ready(
        self, channel_id: str, request_id: str | None
    ) -> tuple[str, _StoredChannel, ChannelLifecycleAdapter]:
        channel_id = self._writable_channel(channel_id)
        _request_id(request_id)
        stored = self._required_stored(channel_id)
        if self._missing_fields(channel_id, stored):
            raise ChannelSelfServiceError("channel_not_configured", 409)
        adapter = self.adapters.get(channel_id)
        if adapter is None:
            raise ChannelSelfServiceError("channel_adapter_unavailable", 503)
        return channel_id, stored, adapter

    def _stop(
        self,
        channel_id: str,
        *,
        action: str,
        request_id: str | None,
    ) -> None:
        adapter = self.adapters.get(channel_id)
        if adapter is None:
            return
        with self._lock:
            self._states[channel_id] = (
                ChannelState.STOPPING,
                ConnectorHealth.DEGRADED,
                None,
            )
        try:
            stopped = bool(adapter.stop(self.stop_timeout_seconds))
        except Exception:
            stopped = False
        if not stopped:
            with self._lock:
                self._states[channel_id] = (
                    ChannelState.ERROR,
                    ConnectorHealth.ERROR,
                    "channel_stop_timeout",
                )
            self._audit(
                channel_id,
                action,
                "failed",
                request_id,
                (),
                "channel_stop_timeout",
            )
            raise ChannelSelfServiceError("channel_stop_timeout", 503)

    def _call_adapter(
        self,
        channel_id: str,
        operation: str,
        request_id: str | None,
        call: Callable[[], ConnectorHealthResult],
    ) -> ConnectorHealthResult:
        try:
            result = call()
        except Exception:
            with self._lock:
                self._states[channel_id] = (
                    ChannelState.ERROR,
                    ConnectorHealth.ERROR,
                    "channel_adapter_failed",
                )
            self._audit(
                channel_id,
                operation,
                "failed",
                request_id,
                (),
                "channel_adapter_failed",
            )
            raise ChannelSelfServiceError("channel_adapter_failed", 502) from None
        if not isinstance(result, ConnectorHealthResult):
            self._audit(
                channel_id,
                operation,
                "failed",
                request_id,
                (),
                "channel_adapter_result_invalid",
            )
            raise ChannelSelfServiceError("channel_adapter_result_invalid", 502)
        _error_code(result.error_code)
        return result

    def _set_enabled(
        self, channel_id: str, stored: _StoredChannel, enabled: bool
    ) -> _StoredChannel:
        updated = _StoredChannel(
            display_name=stored.display_name,
            enabled=enabled,
            config=dict(stored.config),
            secrets=dict(stored.secrets),
            updated_at=datetime.now(UTC),
        )
        self._vault_put(channel_id, updated)
        return updated

    def _required_stored(self, channel_id: str) -> _StoredChannel:
        stored = self._read(channel_id)
        if stored is None:
            raise ChannelSelfServiceError("channel_instance_not_found", 404)
        return stored

    def _read(self, channel_id: str) -> _StoredChannel | None:
        try:
            payload = self.vault.get(self._reference(channel_id))
        except KeyError:
            return None
        except RuntimeError:
            raise ChannelSelfServiceError("channel_vault_unavailable", 503) from None
        if set(payload) == {"schema_version", "channel_id", "generation_reference"}:
            try:
                if payload["schema_version"] != "2" or payload["channel_id"] != channel_id:
                    raise ValueError
                generation_reference = payload["generation_reference"]
                self._validate_generation_reference(channel_id, generation_reference)
                payload = self.vault.get(generation_reference)
            except KeyError:
                raise ChannelSelfServiceError(
                    "channel_vault_record_invalid", 503
                ) from None
            except ChannelSelfServiceError:
                raise
            except RuntimeError:
                raise ChannelSelfServiceError(
                    "channel_vault_unavailable", 503
                ) from None
            except Exception:
                raise ChannelSelfServiceError(
                    "channel_vault_record_invalid", 503
                ) from None
        return self._decode(channel_id, payload)

    def _decode(
        self, channel_id: str, payload: Mapping[str, str]
    ) -> _StoredChannel:
        try:
            if set(payload) != {
                "schema_version",
                "channel_id",
                "display_name",
                "enabled",
                "config_json",
                "secrets_json",
                "updated_at",
            }:
                raise ValueError
            if (
                payload["schema_version"] != "1"
                or payload["channel_id"] != channel_id
                or payload["enabled"] not in {"0", "1"}
            ):
                raise ValueError
            config = json.loads(payload["config_json"])
            secrets = json.loads(payload["secrets_json"])
            if not isinstance(config, dict) or not isinstance(secrets, dict):
                raise ValueError
            public_fields, secret_fields = self._stored_field_maps(channel_id)
            if set(config) - set(public_fields) or set(secrets) - set(secret_fields):
                raise ValueError
            config = {
                key: self._field_value(public_fields[key], value)
                for key, value in config.items()
            }
            updated_at = datetime.fromisoformat(payload["updated_at"])
            if updated_at.tzinfo is None:
                raise ValueError
            return _StoredChannel(
                display_name=_display_name(payload["display_name"], channel_id),
                enabled=payload["enabled"] == "1",
                config=config,
                secrets={key: _secret_value(value) for key, value in secrets.items()},
                updated_at=updated_at.astimezone(UTC),
            )
        except Exception:
            raise ChannelSelfServiceError("channel_vault_record_invalid", 503) from None

    def _encode(
        self, channel_id: str, stored: _StoredChannel
    ) -> dict[str, str]:
        return {
            "schema_version": "1",
            "channel_id": channel_id,
            "display_name": stored.display_name,
            "enabled": "1" if stored.enabled else "0",
            "config_json": json.dumps(
                stored.config, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ),
            "secrets_json": json.dumps(
                stored.secrets, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ),
            "updated_at": stored.updated_at.isoformat(),
        }

    def _vault_put(self, channel_id: str, stored: _StoredChannel) -> None:
        reference = self._active_generation_reference(channel_id)
        self._vault_put_raw(
            reference or self._reference(channel_id), self._encode(channel_id, stored)
        )

    def _vault_delete(self, channel_id: str) -> None:
        generation_reference = self._active_generation_reference(channel_id)
        try:
            self.vault.delete(self._reference(channel_id))
            if generation_reference is not None:
                self.vault.delete(generation_reference)
            self.vault.delete(self._transition_reference(channel_id))
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            raise ChannelSelfServiceError("channel_vault_unavailable", 503) from None

    def _vault_put_raw(self, reference: str, payload: Mapping[str, str]) -> None:
        try:
            self.vault.put(reference, payload)
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            raise ChannelSelfServiceError("channel_vault_unavailable", 503) from None

    def _vault_delete_raw(self, reference: str) -> None:
        try:
            self.vault.delete(reference)
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            raise ChannelSelfServiceError("channel_vault_unavailable", 503) from None

    def _active_generation_reference(self, channel_id: str) -> str | None:
        try:
            payload = self.vault.get(self._reference(channel_id))
        except KeyError:
            return None
        except Exception:
            raise ChannelSelfServiceError("channel_vault_unavailable", 503) from None
        if set(payload) != {"schema_version", "channel_id", "generation_reference"}:
            return None
        try:
            if payload["schema_version"] != "2" or payload["channel_id"] != channel_id:
                raise ValueError
            reference = payload["generation_reference"]
            self._validate_generation_reference(channel_id, reference)
            return reference
        except Exception:
            raise ChannelSelfServiceError("channel_vault_record_invalid", 503) from None

    def _set_generation_pointer(self, channel_id: str, reference: str) -> None:
        self._validate_generation_reference(channel_id, reference)
        self._vault_put_raw(
            self._reference(channel_id),
            {
                "schema_version": "2",
                "channel_id": channel_id,
                "generation_reference": reference,
            },
        )

    def _new_generation_reference(self, channel_id: str) -> str:
        return f"{self._generation_prefix(channel_id)}{secrets.token_hex(16)}"

    def _generation_prefix(self, channel_id: str) -> str:
        return f"ecorex/channel-generations/{self._instance_id(channel_id)}/"

    def _validate_generation_reference(self, channel_id: str, reference: Any) -> None:
        prefix = self._generation_prefix(channel_id)
        if (
            not isinstance(reference, str)
            or not reference.startswith(prefix)
            or re.fullmatch(r"[0-9a-f]{32}", reference[len(prefix) :]) is None
        ):
            raise ValueError("channel generation reference is invalid")

    def _delete_generation(self, reference: str) -> bool:
        try:
            self.vault.delete(reference)
            return True
        except Exception:
            # The transition marker makes orphan cleanup recoverable on restart.
            return False

    def _projection(
        self, channel_id: str, stored: _StoredChannel
    ) -> dict[str, Any]:
        fields = self._fields(channel_id)
        configured = sorted(
            str(field["key"])
            for field in fields
            if self._has_field(stored, field)
        )
        missing = self._missing_fields(channel_id, stored)
        default_state = (
            ChannelState.UNCONFIGURED
            if missing
            else (ChannelState.UNAVAILABLE if channel_id not in self.adapters else ChannelState.STOPPED)
        )
        default_health = (
            ConnectorHealth.UNCONFIGURED
            if missing
            else (
                ConnectorHealth.DISABLED
                if not stored.enabled or channel_id not in self.adapters
                else ConnectorHealth.DEGRADED
            )
        )
        state, health, error_code = self._states.get(
            channel_id, (default_state, default_health, None)
        )
        return {
            "instance_id": self._instance_id(channel_id),
            "channel_id": channel_id,
            "display_name": stored.display_name,
            "configured_fields": configured,
            "missing_fields": missing,
            "enabled": stored.enabled,
            "state": state.value,
            "health": health.value,
            "last_error_code": error_code,
            "updated_at": stored.updated_at.isoformat(),
        }

    def _public_field(
        self,
        field: Mapping[str, Any],
        stored: _StoredChannel | None,
    ) -> dict[str, Any]:
        result: dict[str, Any] = {
            "key": str(field["key"]),
            "label": str(field.get("label") or field["key"]),
            "type": str(field.get("type") or "text"),
            "required": _field_required(field),
            "secret": field.get("type") == "secret",
            "configured": bool(stored and self._has_field(stored, field)),
        }
        if "default" in field and field.get("type") != "secret":
            result["default"] = field["default"]
        return result

    def _field_maps(
        self, channel_id: str
    ) -> tuple[dict[str, Mapping[str, Any]], dict[str, Mapping[str, Any]]]:
        public: dict[str, Mapping[str, Any]] = {}
        secret: dict[str, Mapping[str, Any]] = {}
        for field in self._fields(channel_id):
            target = secret if field.get("type") == "secret" else public
            target[str(field["key"])] = field
        return public, secret

    def _stored_field_maps(
        self, channel_id: str
    ) -> tuple[dict[str, Mapping[str, Any]], dict[str, Mapping[str, Any]]]:
        public, secret = self._field_maps(channel_id)
        if channel_id == "weixin":
            public.update(
                {
                    key: {"key": key, "type": "text"}
                    for key in ("weixin_base_url", "weixin_bot_id", "weixin_user_id")
                }
            )
            secret["weixin_token"] = {"key": "weixin_token", "type": "secret"}
        return public, secret

    def _fields(self, channel_id: str) -> tuple[Mapping[str, Any], ...]:
        definition = CHANNEL_CATALOG[channel_id]
        fields = tuple(
            field
            for field in definition.get("fields", ())
            if isinstance(field, Mapping) and field.get("key")
        )
        if any(str(field.get("type") or "text") not in {"text", "secret", "number"} for field in fields):
            raise RuntimeError("channel catalog field type is unsupported")
        return fields

    @staticmethod
    def _field_value(field: Mapping[str, Any], value: Any) -> Any:
        if field.get("type") == "number":
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 65535:
                raise ChannelSelfServiceError("channel_config_value_invalid", 422)
            return value
        if not isinstance(value, str):
            raise ChannelSelfServiceError("channel_config_value_invalid", 422)
        text = value
        if not text or len(text) > 8192 or any(character in text for character in ("\x00", "\r", "\n")):
            raise ChannelSelfServiceError("channel_config_value_invalid", 422)
        return text

    @staticmethod
    def _has_field(stored: _StoredChannel, field: Mapping[str, Any]) -> bool:
        key = str(field["key"])
        if field.get("type") == "secret":
            return key in stored.secrets and bool(stored.secrets[key])
        if key in stored.config and stored.config[key] not in ("", None):
            return True
        return "default" in field

    def _missing_fields(
        self, channel_id: str, stored: _StoredChannel
    ) -> list[str]:
        return [
            str(field["key"])
            for field in self._fields(channel_id)
            if _field_required(field) and not self._has_field(stored, field)
        ]

    def _material(self, stored: _StoredChannel) -> dict[str, Any]:
        return {**dict(stored.config), **dict(stored.secrets)}

    def _writable_channel(self, value: str) -> str:
        channel_id = normalize_channel_name(value)
        if channel_id not in CHANNEL_CATALOG:
            raise ChannelSelfServiceError("channel_not_found", 404)
        if channel_id in self.oauth_channels:
            raise ChannelSelfServiceError("channel_oauth_required", 409)
        return channel_id

    def _reference(self, channel_id: str) -> str:
        organization = hashlib.sha256(
            self.owner.organization_id.encode("utf-8")
        ).hexdigest()[:32]
        account = hashlib.sha256(self.owner.account_id.encode("utf-8")).hexdigest()[:32]
        return f"ecorex/channel-instances/{organization}/{account}/{channel_id}"

    def _transition_reference(self, channel_id: str) -> str:
        return f"ecorex/channel-transitions/{self._instance_id(channel_id)}"

    def _instance_id(self, channel_id: str) -> str:
        source = "\x00".join(
            (self.owner.organization_id, self.owner.account_id, channel_id)
        )
        return "channel_" + hashlib.sha256(source.encode("utf-8")).hexdigest()[:32]

    def _auth_kind(self, channel_id: str) -> ConnectorAuthKind:
        if channel_id in self.oauth_channels:
            return ConnectorAuthKind.OAUTH2
        if channel_id == "weixin":
            return ConnectorAuthKind.DEVICE_CODE
        if channel_id in {"telegram", "slack", "discord"}:
            return ConnectorAuthKind.API_TOKEN
        return ConnectorAuthKind.APP_CREDENTIALS

    def _audit(
        self,
        channel_id: str,
        action: str,
        outcome: str,
        request_id: str | None,
        field_names: tuple[str, ...],
        error_code: str | None,
    ) -> None:
        if self.audit_sink is None:
            return
        self.audit_sink(
            ChannelAuditEvent(
                account_id=self.owner.account_id,
                organization_id=self.owner.organization_id,
                channel_id=channel_id,
                action=action,
                outcome=outcome,
                request_id=request_id,
                field_names=field_names,
                error_code=_error_code(error_code),
                created_at=datetime.now(UTC),
            )
        )


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SaveChannelRequest(_StrictModel):
    display_name: str = Field(default="", max_length=256)
    config: dict[str, Any] = Field(default_factory=dict, max_length=64)
    secrets: dict[str, str] = Field(default_factory=dict, max_length=64)


def create_channel_self_service_router(service: ChannelSelfService) -> APIRouter:
    router = APIRouter(prefix="/connectors/channels", tags=["connectors"])

    @router.get("")
    def catalog() -> dict[str, Any]:
        return _api(service.catalog)

    @router.put("/{channel_id}/instance")
    def save(
        channel_id: str,
        request: SaveChannelRequest,
        client_request_id: str | None = Header(
            default=None, alias="X-EcoreX-Client-Request-ID"
        ),
    ) -> dict[str, Any]:
        return _api(
            service.save,
            channel_id,
            display_name=request.display_name,
            config=request.config,
            secrets=request.secrets,
            request_id=client_request_id,
        )

    def action(name: str, operation: Callable[..., dict[str, Any]]) -> None:
        def endpoint(
            channel_id: str,
            client_request_id: str | None = Header(
                default=None, alias="X-EcoreX-Client-Request-ID"
            ),
        ) -> dict[str, Any]:
            return _api(operation, channel_id, request_id=client_request_id)

        endpoint.__name__ = f"channel_{name}"
        router.add_api_route(
            f"/{{channel_id}}/{name}", endpoint, methods=["POST"]
        )

    action("test", service.test)
    action("enable", service.enable)
    action("disable", service.disable)
    action("retry", service.retry)
    action("health", service.health)

    @router.post("/{channel_id}/auth/begin")
    def auth_begin(
        channel_id: str,
        client_request_id: str | None = Header(
            default=None, alias="X-EcoreX-Client-Request-ID"
        ),
    ) -> dict[str, Any]:
        return _api(
            service.begin_authorization,
            channel_id,
            request_id=client_request_id,
        )

    def auth_action(
        name: str, operation: Callable[..., dict[str, Any]]
    ) -> None:
        def endpoint(
            channel_id: str,
            flow_id: str,
            client_request_id: str | None = Header(
                default=None, alias="X-EcoreX-Client-Request-ID"
            ),
        ) -> dict[str, Any]:
            return _api(
                operation,
                channel_id,
                flow_id,
                request_id=client_request_id,
            )

        endpoint.__name__ = f"channel_auth_{name}"
        router.add_api_route(
            f"/{{channel_id}}/auth/{{flow_id}}/{name}", endpoint, methods=["POST"]
        )

    auth_action("poll", service.poll_authorization)
    auth_action("cancel", service.cancel_authorization)
    auth_action("refresh", service.refresh_authorization)

    @router.delete(
        "/{channel_id}/instance", status_code=status.HTTP_204_NO_CONTENT
    )
    def disconnect(
        channel_id: str,
        client_request_id: str | None = Header(
            default=None, alias="X-EcoreX-Client-Request-ID"
        ),
    ) -> Response:
        _api(service.disconnect, channel_id, request_id=client_request_id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    return router


def _api(call: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    try:
        return call(*args, **kwargs)
    except ChannelSelfServiceError as error:
        raise HTTPException(
            status_code=error.http_status,
            detail={"code": error.code, "message": "通道操作失败"},
        ) from None


def _display_name(value: Any, channel_id: str) -> str:
    text = str(value or "").strip() or str(
        CHANNEL_CATALOG[channel_id].get("label", {}).get("zh") or channel_id
    )
    if len(text) > 256 or any(character in text for character in ("\x00", "\r", "\n")):
        raise ChannelSelfServiceError("channel_display_name_invalid", 422)
    return text


def _secret_value(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value) > 64 * 1024:
        raise ChannelSelfServiceError("channel_secret_invalid", 422)
    if any(character in value for character in ("\x00", "\r", "\n")):
        raise ChannelSelfServiceError("channel_secret_invalid", 422)
    return value


def _request_id(value: str | None) -> str | None:
    if value is None:
        return None
    if not _REQUEST_ID_RE.fullmatch(value):
        raise ChannelSelfServiceError("channel_request_id_invalid", 422)
    return value


def _device_flow_id(value: Any) -> str:
    if (
        not isinstance(value, str)
        or not value.startswith("wxauth_")
        or len(value) != 39
        or any(character not in "0123456789abcdef" for character in value[7:])
    ):
        raise ChannelSelfServiceError("channel_device_flow_invalid", 422)
    return value


def _validate_device_authorization(value: Any) -> ChannelDeviceAuthorization:
    if not isinstance(value, ChannelDeviceAuthorization):
        raise ChannelSelfServiceError("channel_adapter_result_invalid", 502)
    _device_flow_id(value.flow_id)
    if value.status not in {"pending", "scanned", "confirmed", "expired", "cancelled"}:
        raise ChannelSelfServiceError("channel_adapter_result_invalid", 502)
    if not isinstance(value.expires_at, datetime) or value.expires_at.tzinfo is None:
        raise ChannelSelfServiceError("channel_adapter_result_invalid", 502)
    if value.status in {"pending", "scanned"}:
        if (
            not isinstance(value.verification_url, str)
            or not value.verification_url
            or len(value.verification_url) > 8192
            or any(character in value.verification_url for character in ("\x00", "\r", "\n"))
        ):
            raise ChannelSelfServiceError("channel_adapter_result_invalid", 502)
        if (
            not isinstance(value.qr_image_data_url, str)
            or not value.qr_image_data_url.startswith("data:image/png;base64,")
            or len(value.qr_image_data_url) > 512 * 1024
        ):
            raise ChannelSelfServiceError("channel_adapter_result_invalid", 502)
    elif value.verification_url is not None or value.qr_image_data_url is not None:
        raise ChannelSelfServiceError("channel_adapter_result_invalid", 502)
    if value.status != "confirmed" and (
        value.config is not None or value.secrets is not None
    ):
        raise ChannelSelfServiceError("channel_adapter_result_invalid", 502)
    return value


def _error_code(value: str | None) -> str | None:
    if value is None:
        return None
    code = str(value)
    if not _ERROR_CODE_RE.fullmatch(code):
        raise ChannelSelfServiceError("channel_adapter_result_invalid", 502)
    return code


def _field_required(field: Mapping[str, Any]) -> bool:
    return bool(
        field.get("required") is not False
        and not (field.get("type") in {"number", "bool"} and "default" in field)
    )


def _state_for_health(health: ConnectorHealth) -> ChannelState:
    return {
        ConnectorHealth.UNCONFIGURED: ChannelState.UNCONFIGURED,
        ConnectorHealth.AUTHENTICATING: ChannelState.STARTING,
        ConnectorHealth.CONNECTED: ChannelState.CONNECTED,
        ConnectorHealth.DEGRADED: ChannelState.DEGRADED,
        ConnectorHealth.ERROR: ChannelState.ERROR,
        ConnectorHealth.DISABLED: ChannelState.STOPPED,
    }[health]


def channel_audit_outbox_event(event: ChannelAuditEvent) -> ConnectorOutboxEvent:
    """Translate one secret-free channel lifecycle fact into the audit bridge."""

    identity = "\x00".join(
        (
            event.account_id,
            event.organization_id,
            event.channel_id,
            event.action,
            event.outcome,
            event.request_id or event.created_at.isoformat(),
        )
    )
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    return ConnectorOutboxEvent(
        event_id=f"channel_{digest[:32]}",
        event_type=f"connector.channel.{event.action}",
        aggregate_id=f"channel_{digest[32:]}",
        aggregate_seq=1,
        payload={
            "connector_id": event.channel_id,
            "instance_id": f"channel_{digest[32:]}",
            "status": event.outcome,
            "outcome": event.outcome,
            "error_code": event.error_code,
        },
        created_at=event.created_at,
        lease_token="channel-self-service",
        attempts=1,
    )


__all__ = [
    "ChannelAuditEvent",
    "ChannelCredentialOwner",
    "ChannelLifecycleAdapter",
    "ChannelSelfService",
    "ChannelSelfServiceError",
    "ChannelState",
    "SaveChannelRequest",
    "channel_audit_outbox_event",
    "create_channel_self_service_router",
]
