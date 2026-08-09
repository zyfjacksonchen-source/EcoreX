"""Managed HTTPS ingress for WeChat callback-based message channels."""

from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import hashlib
import hmac
import json
from pathlib import Path
import re
import secrets
import sqlite3
import struct
from typing import Any, Callable, Mapping
from urllib.parse import quote, urlsplit
import uuid
import xml.etree.ElementTree as ElementTree

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from ecorex.observability.audit import AuditPayloadCipher
from ecorex.protocol import AuditRecordProjection
from ecorex.runtime.database import json_dumps

from .audit import CloudAuditRepository
from .models import ControlPrincipal


_CHANNELS = frozenset(
    {"wechatcom_app", "wechat_kf", "wechatmp", "wechatmp_service"}
)
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,511}$")
_BINDING_ID = re.compile(r"^wxbind_[A-Za-z0-9_-]{43}$")
_LEASE_ID = re.compile(r"^wxlease_[A-Za-z0-9_-]{16,128}$")
_IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_MAX_CALLBACK_BYTES = 1024 * 1024
_MAX_TEXT_BYTES = 128 * 1024
_MAX_PROVIDER_BYTES = 8 * 1024 * 1024


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _canonical(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _organization(principal: ControlPrincipal) -> str:
    return principal.organization_id or f"personal:{principal.account_id}"


def _bounded(value: Any, name: str, maximum: int) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value.encode("utf-8")) > maximum
        or any(ord(character) < 32 for character in value)
    ):
        raise WechatCallbackError(f"invalid_{name}", status_code=422)
    return value


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BindingRequest(_StrictModel):
    channel_id: str = Field(min_length=1, max_length=64)
    app_id: str = Field(min_length=1, max_length=512)
    agent_id: str | None = Field(default=None, min_length=1, max_length=128)
    app_secret: SecretStr = Field(min_length=1, max_length=4096)
    token: SecretStr = Field(min_length=1, max_length=1024)
    encoding_aes_key: SecretStr = Field(min_length=43, max_length=44)


class PullRequest(_StrictModel):
    binding_id: str = Field(min_length=1, max_length=128)
    lease_id: str = Field(min_length=1, max_length=160)
    limit: int = Field(default=20, ge=1, le=100)


class InboxMutationRequest(_StrictModel):
    binding_id: str = Field(min_length=1, max_length=128)
    event_id: str = Field(min_length=1, max_length=128)
    lease_id: str = Field(min_length=1, max_length=160)


class OutboundRequest(_StrictModel):
    binding_id: str = Field(min_length=1, max_length=128)
    event_id: str = Field(min_length=1, max_length=128)
    text: str = Field(min_length=1, max_length=_MAX_TEXT_BYTES)


class WechatCallbackError(RuntimeError):
    def __init__(
        self,
        code: str,
        *,
        status_code: int = 503,
        retryable: bool = False,
        uncertain: bool = False,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.status_code = status_code
        self.retryable = retryable
        self.uncertain = uncertain


@dataclass(frozen=True, slots=True)
class _Inbound:
    provider_message_id: str
    conversation_id: str
    text: str
    reply_context: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class _KfSyncResult:
    messages: tuple[_Inbound, ...]
    cursor: str
    has_more: bool


class WechatProviderClient:
    """Fixed-host WeChat/WeCom client; no provider URL is tenant-controlled."""

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._owns_client = client is None
        self.client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10, read=30, write=30, pool=10),
            follow_redirects=False,
            trust_env=False,
            limits=httpx.Limits(max_connections=16, max_keepalive_connections=8),
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self.client.aclose()

    async def sync_kf(
        self,
        credentials: Mapping[str, str],
        *,
        cursor: str,
        sync_token: str,
    ) -> _KfSyncResult:
        access_token = await self._wecom_token(credentials)
        value = await self._json(
            "POST",
            "https://qyapi.weixin.qq.com/cgi-bin/kf/sync_msg",
            params={"access_token": access_token},
            body={
                "cursor": cursor,
                "token": sync_token,
                "limit": 1000,
                "voice_format": 0,
            },
            send_may_have_committed=False,
        )
        raw_messages = value.get("msg_list")
        if not isinstance(raw_messages, list) or len(raw_messages) > 1000:
            raise WechatCallbackError("provider_response_invalid")
        messages: list[_Inbound] = []
        for item in raw_messages:
            if not isinstance(item, dict) or item.get("msgtype") != "text":
                continue
            text = item.get("text")
            origin = item.get("origin")
            external_user = item.get("external_userid")
            open_kfid = item.get("open_kfid")
            message_id = item.get("msgid")
            if (
                not isinstance(text, dict)
                or not isinstance(text.get("content"), str)
                or origin != 3
                or not all(
                    isinstance(value, str) and value
                    for value in (external_user, open_kfid, message_id)
                )
            ):
                continue
            messages.append(
                _Inbound(
                    provider_message_id=message_id,
                    conversation_id=external_user,
                    text=text["content"],
                    reply_context={
                        "to_user": external_user,
                        "open_kfid": open_kfid,
                    },
                )
            )
        cursor_value = value.get("next_cursor", cursor)
        if not isinstance(cursor_value, str) or len(cursor_value) > 64 * 1024:
            raise WechatCallbackError("provider_response_invalid")
        return _KfSyncResult(
            messages=tuple(messages),
            cursor=cursor_value,
            has_more=value.get("has_more") in {1, True},
        )

    async def send(
        self,
        channel_id: str,
        credentials: Mapping[str, str],
        reply_context: Mapping[str, str],
        text: str,
    ) -> str:
        if channel_id == "wechatcom_app":
            token = await self._wecom_token(credentials)
            value = await self._json(
                "POST",
                "https://qyapi.weixin.qq.com/cgi-bin/message/send",
                params={"access_token": token},
                body={
                    "touser": reply_context["to_user"],
                    "msgtype": "text",
                    "agentid": credentials["agent_id"],
                    "text": {"content": text},
                },
                send_may_have_committed=True,
            )
            return str(value.get("msgid") or "accepted")
        if channel_id == "wechat_kf":
            token = await self._wecom_token(credentials)
            value = await self._json(
                "POST",
                "https://qyapi.weixin.qq.com/cgi-bin/kf/send_msg",
                params={"access_token": token},
                body={
                    "touser": reply_context["to_user"],
                    "open_kfid": reply_context["open_kfid"],
                    "msgtype": "text",
                    "text": {"content": text},
                },
                send_may_have_committed=True,
            )
            return str(value.get("msgid") or "accepted")
        if channel_id == "wechatmp_service":
            token = await self._mp_token(credentials)
            await self._json(
                "POST",
                "https://api.weixin.qq.com/cgi-bin/message/custom/send",
                params={"access_token": token},
                body={
                    "touser": reply_context["to_user"],
                    "msgtype": "text",
                    "text": {"content": text},
                },
                send_may_have_committed=True,
            )
            return "accepted"
        raise WechatCallbackError("passive_reply_required", status_code=409)

    async def _wecom_token(self, credentials: Mapping[str, str]) -> str:
        value = await self._json(
            "GET",
            "https://qyapi.weixin.qq.com/cgi-bin/gettoken",
            params={
                "corpid": credentials["app_id"],
                "corpsecret": credentials["app_secret"],
            },
            send_may_have_committed=False,
        )
        return _bounded(value.get("access_token"), "provider_token", 8192)

    async def _mp_token(self, credentials: Mapping[str, str]) -> str:
        value = await self._json(
            "GET",
            "https://api.weixin.qq.com/cgi-bin/token",
            params={
                "grant_type": "client_credential",
                "appid": credentials["app_id"],
                "secret": credentials["app_secret"],
            },
            send_may_have_committed=False,
        )
        return _bounded(value.get("access_token"), "provider_token", 8192)

    async def _json(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, Any],
        body: Mapping[str, Any] | None = None,
        send_may_have_committed: bool,
    ) -> dict[str, Any]:
        try:
            response = await self.client.request(
                method,
                url,
                params=dict(params),
                content=None if body is None else _canonical(dict(body)).encode(),
                headers={
                    "Accept": "application/json",
                    "Accept-Encoding": "identity",
                    "Content-Type": "application/json; charset=utf-8",
                },
            )
        except (httpx.TimeoutException, httpx.TransportError):
            raise WechatCallbackError(
                "provider_delivery_uncertain"
                if send_may_have_committed
                else "provider_unavailable",
                retryable=not send_may_have_committed,
                uncertain=send_may_have_committed,
            ) from None
        if response.is_redirect or response.history:
            raise WechatCallbackError("provider_redirect_refused")
        if response.headers.get("content-encoding", "identity").casefold() != "identity":
            raise WechatCallbackError("provider_response_invalid")
        if len(response.content) > _MAX_PROVIDER_BYTES:
            raise WechatCallbackError("provider_response_too_large")
        try:
            value = json.loads(response.content.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise WechatCallbackError("provider_response_invalid") from None
        if not isinstance(value, dict):
            raise WechatCallbackError("provider_response_invalid")
        if response.status_code >= 500 or response.status_code in {408, 425, 429}:
            raise WechatCallbackError("provider_unavailable", retryable=True)
        code = value.get("errcode", 0)
        if response.status_code != 200 or code not in {0, "0", None}:
            raise WechatCallbackError("provider_rejected", status_code=422)
        return value


class _WechatCrypto:
    def __init__(self, *, token: str, aes_key: str, receive_id: str) -> None:
        self.token = _bounded(token, "callback_token", 1024)
        self.receive_id = _bounded(receive_id, "callback_receive_id", 512)
        try:
            self.key = base64.b64decode(aes_key + "=", validate=True)
        except Exception:
            raise WechatCallbackError("invalid_encoding_aes_key", status_code=422) from None
        if len(self.key) != 32:
            raise WechatCallbackError("invalid_encoding_aes_key", status_code=422)

    def decrypt(
        self,
        encrypted: str,
        *,
        signature: str,
        timestamp: str,
        nonce: str,
    ) -> bytes:
        expected = hashlib.sha1(
            "".join(sorted((self.token, timestamp, nonce, encrypted))).encode()
        ).hexdigest()
        if not hmac.compare_digest(expected, signature):
            raise WechatCallbackError("callback_signature_invalid", status_code=403)
        try:
            ciphertext = base64.b64decode(encrypted, validate=True)
            decryptor = Cipher(
                algorithms.AES(self.key), modes.CBC(self.key[:16])
            ).decryptor()
            padded = decryptor.update(ciphertext) + decryptor.finalize()
            unpadder = padding.PKCS7(128).unpadder()
            plaintext = unpadder.update(padded) + unpadder.finalize()
        except Exception:
            raise WechatCallbackError("callback_cipher_invalid", status_code=403) from None
        if len(plaintext) < 20:
            raise WechatCallbackError("callback_cipher_invalid", status_code=403)
        size = struct.unpack(">I", plaintext[16:20])[0]
        if size > _MAX_CALLBACK_BYTES or 20 + size > len(plaintext):
            raise WechatCallbackError("callback_cipher_invalid", status_code=403)
        message = plaintext[20 : 20 + size]
        try:
            receive_id = plaintext[20 + size :].decode("utf-8")
        except UnicodeDecodeError:
            raise WechatCallbackError("callback_cipher_invalid", status_code=403) from None
        if not hmac.compare_digest(receive_id, self.receive_id):
            raise WechatCallbackError("callback_recipient_invalid", status_code=403)
        return message

    def encrypt(self, message: bytes, *, timestamp: str, nonce: str) -> bytes:
        if len(message) > _MAX_CALLBACK_BYTES:
            raise WechatCallbackError("callback_response_too_large")
        plaintext = (
            secrets.token_bytes(16)
            + struct.pack(">I", len(message))
            + message
            + self.receive_id.encode()
        )
        padder = padding.PKCS7(128).padder()
        padded = padder.update(plaintext) + padder.finalize()
        encryptor = Cipher(
            algorithms.AES(self.key), modes.CBC(self.key[:16])
        ).encryptor()
        encrypted = base64.b64encode(
            encryptor.update(padded) + encryptor.finalize()
        ).decode("ascii")
        signature = hashlib.sha1(
            "".join(sorted((self.token, timestamp, nonce, encrypted))).encode()
        ).hexdigest()
        root = ElementTree.Element("xml")
        for name, value in (
            ("Encrypt", encrypted),
            ("MsgSignature", signature),
            ("TimeStamp", timestamp),
            ("Nonce", nonce),
        ):
            ElementTree.SubElement(root, name).text = value
        return ElementTree.tostring(root, encoding="utf-8")


class WechatCallbackGateway:
    def __init__(
        self,
        database_path: str | Path,
        *,
        encryption_key: bytes,
        audit_repository: CloudAuditRepository,
        public_callback_base_url: str,
        provider: WechatProviderClient | None = None,
        clock: Callable[[], datetime] = _now,
        passive_wait_seconds: float = 4.5,
    ) -> None:
        self.database_path = Path(database_path).expanduser().resolve()
        self.cipher = AuditPayloadCipher(encryption_key)
        if not isinstance(audit_repository, CloudAuditRepository):
            raise TypeError("cloud audit repository is required")
        parsed = urlsplit(public_callback_base_url.rstrip("/"))
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.port not in {None, 443}
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path.rstrip("/") != "/api/v1/channels/wechat/callback"
        ):
            raise ValueError("WeChat public callback base URL is invalid")
        if not 0.1 <= passive_wait_seconds <= 4.5:
            raise ValueError("WeChat passive reply wait is invalid")
        self.audit_repository = audit_repository
        self.public_callback_base_url = public_callback_base_url.rstrip("/")
        self.provider = provider or WechatProviderClient()
        self.clock = clock
        self.passive_wait_seconds = float(passive_wait_seconds)

    async def aclose(self) -> None:
        await self.provider.aclose()

    def create_router(
        self,
        *,
        principal_dependency: Callable[..., ControlPrincipal],
    ) -> APIRouter:
        router = APIRouter(tags=["wechat-callback-channels"])

        @router.post("/api/v1/channels/wechat/bindings")
        async def bind(
            request: BindingRequest,
            principal: ControlPrincipal = Depends(principal_dependency),
        ) -> dict[str, Any]:
            try:
                result = self.bind(principal, request)
                self._drain_audit()
                return result
            except WechatCallbackError as error:
                raise self._http_error(error) from None

        @router.post("/api/v1/channels/wechat/inbox/pull")
        async def pull(
            request: PullRequest,
            principal: ControlPrincipal = Depends(principal_dependency),
        ) -> dict[str, Any]:
            try:
                await self._sync_kf(principal, request.binding_id)
                result = self.pull(principal, request)
                self._drain_audit()
                return result
            except WechatCallbackError as error:
                raise self._http_error(error) from None

        @router.post("/api/v1/channels/wechat/inbox/ack")
        async def acknowledge(
            request: InboxMutationRequest,
            principal: ControlPrincipal = Depends(principal_dependency),
        ) -> dict[str, Any]:
            try:
                result = self.acknowledge(principal, request)
                self._drain_audit()
                return result
            except WechatCallbackError as error:
                raise self._http_error(error) from None

        @router.post("/api/v1/channels/wechat/inbox/abandon")
        async def abandon(
            request: InboxMutationRequest,
            principal: ControlPrincipal = Depends(principal_dependency),
        ) -> dict[str, Any]:
            try:
                return self.abandon(principal, request)
            except WechatCallbackError as error:
                raise self._http_error(error) from None

        @router.post("/api/v1/channels/wechat/outbound")
        async def outbound(
            request: OutboundRequest,
            idempotency_key: str = Header(..., alias="Idempotency-Key"),
            principal: ControlPrincipal = Depends(principal_dependency),
        ) -> dict[str, Any]:
            try:
                result = await self.outbound(principal, request, idempotency_key)
                self._drain_audit()
                return result
            except WechatCallbackError as error:
                raise self._http_error(error) from None

        @router.get("/api/v1/channels/wechat/callback/{binding_id}")
        async def verify_callback(
            binding_id: str,
            msg_signature: str = Query(...),
            timestamp: str = Query(...),
            nonce: str = Query(...),
            echostr: str = Query(...),
        ) -> Response:
            try:
                credentials = self._public_credentials(binding_id)
                plaintext = self._crypto(credentials).decrypt(
                    _bounded(echostr, "callback_echo", _MAX_CALLBACK_BYTES),
                    signature=_bounded(msg_signature, "callback_signature", 128),
                    timestamp=_bounded(timestamp, "callback_timestamp", 32),
                    nonce=_bounded(nonce, "callback_nonce", 128),
                )
                return Response(plaintext, media_type="text/plain")
            except WechatCallbackError as error:
                raise self._http_error(error) from None

        @router.post("/api/v1/channels/wechat/callback/{binding_id}")
        async def callback(
            binding_id: str,
            request: Request,
            msg_signature: str = Query(...),
            timestamp: str = Query(...),
            nonce: str = Query(...),
        ) -> Response:
            try:
                content_length = request.headers.get("content-length")
                if (
                    content_length is None
                    or not content_length.isdigit()
                    or not 1 <= int(content_length) <= _MAX_CALLBACK_BYTES
                ):
                    raise WechatCallbackError(
                        "callback_length_invalid", status_code=413
                    )
                body = bytearray()
                async for chunk in request.stream():
                    body.extend(chunk)
                    if len(body) > int(content_length):
                        raise WechatCallbackError(
                            "callback_length_invalid", status_code=413
                        )
                if len(body) != int(content_length):
                    raise WechatCallbackError(
                        "callback_length_invalid", status_code=400
                    )
                event = self.ingest_callback(
                    binding_id,
                    bytes(body),
                    signature=msg_signature,
                    timestamp=timestamp,
                    nonce=nonce,
                )
                self._drain_audit()
                if event is None:
                    return Response(b"success", media_type="text/plain")
                text = await self._wait_passive(binding_id, event["event_id"])
                if text is None:
                    return Response(b"success", media_type="text/plain")
                reply = self._passive_xml(event, text)
                encrypted = self._crypto(
                    self._public_credentials(binding_id)
                ).encrypt(reply, timestamp=timestamp, nonce=nonce)
                return Response(encrypted, media_type="application/xml")
            except WechatCallbackError as error:
                raise self._http_error(error) from None

        return router

    def bind(
        self, principal: ControlPrincipal, request: BindingRequest
    ) -> dict[str, Any]:
        channel_id = request.channel_id
        if channel_id not in _CHANNELS:
            raise WechatCallbackError("channel_not_supported", status_code=404)
        if channel_id == "wechatmp":
            raise WechatCallbackError(
                "passive_runtime_unavailable", status_code=409
            )
        app_id = _bounded(request.app_id, "app_id", 512)
        agent_id = request.agent_id
        if channel_id == "wechatcom_app":
            agent_id = _bounded(agent_id, "agent_id", 128)
        elif agent_id is not None:
            raise WechatCallbackError("invalid_agent_id", status_code=422)
        material = {
            "app_id": app_id,
            "agent_id": agent_id or "",
            "app_secret": _bounded(
                request.app_secret.get_secret_value(), "app_secret", 4096
            ),
            "token": _bounded(request.token.get_secret_value(), "token", 1024),
            "encoding_aes_key": _bounded(
                request.encoding_aes_key.get_secret_value(), "encoding_aes_key", 44
            ),
        }
        _WechatCrypto(
            token=material["token"],
            aes_key=material["encoding_aes_key"],
            receive_id=app_id,
        )
        organization_id = _organization(principal)
        app_sha = _sha(app_id)
        now = _iso(self.clock())
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT binding_id FROM wechat_callback_bindings WHERE "
                "account_id=? AND organization_id=? AND channel_id=? "
                "AND app_id_sha256=?",
                (principal.account_id, organization_id, channel_id, app_sha),
            ).fetchone()
            binding_id = (
                str(existing["binding_id"])
                if existing is not None
                else "wxbind_" + secrets.token_urlsafe(32)
            )
            envelope = self.cipher.encrypt(
                _canonical(material),
                associated_data="wechat-binding:" + binding_id,
            )
            if existing is None:
                connection.execute(
                    "INSERT INTO wechat_callback_bindings("
                    "binding_id,channel_id,account_id,organization_id,app_id_sha256,"
                    "credential_envelope_json,status,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,'enabled',?,?)",
                    (
                        binding_id,
                        channel_id,
                        principal.account_id,
                        organization_id,
                        app_sha,
                        envelope,
                        now,
                        now,
                    ),
                )
                if channel_id == "wechat_kf":
                    connection.execute(
                        "INSERT INTO wechat_callback_kf_state("
                        "binding_id,dirty,updated_at) VALUES(?,0,?)",
                        (binding_id, now),
                    )
            else:
                connection.execute(
                    "UPDATE wechat_callback_bindings SET credential_envelope_json=?,"
                    "status='enabled',updated_at=? WHERE binding_id=?",
                    (envelope, now, binding_id),
                )
            self._enqueue_audit(
                connection,
                event_type="wechat_callback.binding.configured",
                account_id=principal.account_id,
                organization_id=organization_id,
                binding_id=binding_id,
                payload={
                    "channel_id": channel_id,
                    "status": "enabled",
                    "credential_fingerprint": _sha(_canonical(material)),
                },
            )
            connection.commit()
        except sqlite3.IntegrityError:
            if connection.in_transaction:
                connection.rollback()
            raise WechatCallbackError("callback_mode_conflict", status_code=409) from None
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()
        material.clear()
        return {
            "binding_id": binding_id,
            "channel_id": channel_id,
            "callback_url": f"{self.public_callback_base_url}/{binding_id}",
            "status": "enabled",
            "external_display_name": "e-Mate",
            "setup_requirement": (
                "请在微信或企业微信管理后台将外部应用或账号名称设置为 e-Mate"
            ),
        }

    def pull(
        self, principal: ControlPrincipal, request: PullRequest
    ) -> dict[str, Any]:
        self._validate_binding_id(request.binding_id)
        if _LEASE_ID.fullmatch(request.lease_id) is None:
            raise WechatCallbackError("lease_id_invalid", status_code=422)
        now = self.clock().astimezone(UTC)
        expires = now + timedelta(seconds=30)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            binding = self._owned_binding(connection, principal, request.binding_id)
            connection.execute(
                "UPDATE wechat_callback_inbox SET state='ready',lease_id=NULL,"
                "lease_expires_at=NULL WHERE binding_id=? AND state='leased' "
                "AND lease_expires_at<=?",
                (request.binding_id, _iso(now)),
            )
            rows = connection.execute(
                "SELECT event_id,payload_envelope_json,created_at FROM "
                "wechat_callback_inbox WHERE binding_id=? AND state='ready' "
                "ORDER BY created_at,event_id LIMIT ?",
                (request.binding_id, request.limit),
            ).fetchall()
            event_ids = [str(row["event_id"]) for row in rows]
            for event_id in event_ids:
                connection.execute(
                    "UPDATE wechat_callback_inbox SET state='leased',lease_id=?,"
                    "lease_expires_at=? WHERE event_id=? AND state='ready'",
                    (request.lease_id, _iso(expires), event_id),
                )
            connection.commit()
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()
        items: list[dict[str, Any]] = []
        for row in rows:
            event_id = str(row["event_id"])
            envelope = row["payload_envelope_json"]
            if not isinstance(envelope, str):
                raise WechatCallbackError("inbox_payload_unavailable")
            try:
                payload = json.loads(
                    self.cipher.decrypt(
                        envelope, associated_data="wechat-inbox:" + event_id
                    )
                )
            except Exception:
                raise WechatCallbackError("inbox_payload_unavailable") from None
            if not isinstance(payload, dict) or set(payload) != {
                "channel_id",
                "conversation_id",
                "message_id",
                "text",
            }:
                raise WechatCallbackError("inbox_payload_unavailable")
            items.append(
                {
                    "event_id": event_id,
                    "channel_id": binding["channel_id"],
                    "conversation_id": payload["conversation_id"],
                    "message_id": payload["message_id"],
                    "text": payload["text"],
                    "created_at": str(row["created_at"]),
                }
            )
        return {
            "binding_id": request.binding_id,
            "lease_id": request.lease_id,
            "items": items,
        }

    def acknowledge(
        self, principal: ControlPrincipal, request: InboxMutationRequest
    ) -> dict[str, Any]:
        self._validate_mutation(request)
        now = _iso(self.clock())
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            binding = self._owned_binding(connection, principal, request.binding_id)
            changed = connection.execute(
                "UPDATE wechat_callback_inbox SET state='acknowledged',"
                "payload_envelope_json=NULL,lease_id=NULL,lease_expires_at=NULL,"
                "acknowledged_at=? WHERE event_id=? AND binding_id=? "
                "AND state='leased' AND lease_id=?",
                (
                    now,
                    request.event_id,
                    request.binding_id,
                    request.lease_id,
                ),
            ).rowcount
            if changed != 1:
                row = connection.execute(
                    "SELECT state FROM wechat_callback_inbox WHERE event_id=? "
                    "AND binding_id=?",
                    (request.event_id, request.binding_id),
                ).fetchone()
                if row is None or str(row["state"]) != "acknowledged":
                    raise WechatCallbackError("inbox_lease_conflict", status_code=409)
            self._enqueue_audit(
                connection,
                event_type="wechat_callback.inbox.acknowledged",
                account_id=principal.account_id,
                organization_id=_organization(principal),
                binding_id=request.binding_id,
                payload={
                    "channel_id": str(binding["channel_id"]),
                    "event_id_sha256": _sha(request.event_id),
                },
            )
            connection.commit()
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()
        return {"acknowledged": True}

    def abandon(
        self, principal: ControlPrincipal, request: InboxMutationRequest
    ) -> dict[str, Any]:
        self._validate_mutation(request)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._owned_binding(connection, principal, request.binding_id)
            changed = connection.execute(
                "UPDATE wechat_callback_inbox SET reply_envelope_json=NULL WHERE "
                "event_id=? AND binding_id=? AND state='acknowledged'",
                (request.event_id, request.binding_id),
            ).rowcount
            if changed != 1:
                raise WechatCallbackError("inbox_state_conflict", status_code=409)
            connection.commit()
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()
        return {"abandoned": True}

    async def outbound(
        self,
        principal: ControlPrincipal,
        request: OutboundRequest,
        idempotency_key: str,
    ) -> dict[str, Any]:
        self._validate_binding_id(request.binding_id)
        if _IDEMPOTENCY_KEY.fullmatch(idempotency_key) is None:
            raise WechatCallbackError("idempotency_key_invalid", status_code=422)
        text = _bounded(request.text, "outbound_text", _MAX_TEXT_BYTES)
        request_sha = _sha(
            _canonical(
                {
                    "binding_id": request.binding_id,
                    "event_id": request.event_id,
                    "text": text,
                }
            )
        )
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            binding = self._owned_binding(connection, principal, request.binding_id)
            inbox = connection.execute(
                "SELECT reply_envelope_json,passive_deadline_at FROM "
                "wechat_callback_inbox WHERE binding_id=? AND event_id=? "
                "AND state='acknowledged'",
                (request.binding_id, request.event_id),
            ).fetchone()
            if inbox is None or not isinstance(inbox["reply_envelope_json"], str):
                raise WechatCallbackError("reply_context_unavailable", status_code=409)
            existing = connection.execute(
                "SELECT request_sha256,state,error_code FROM "
                "wechat_callback_deliveries WHERE binding_id=? AND idempotency_key=?",
                (request.binding_id, idempotency_key),
            ).fetchone()
            if existing is not None:
                if not hmac.compare_digest(str(existing["request_sha256"]), request_sha):
                    raise WechatCallbackError("idempotency_conflict", status_code=409)
                state = str(existing["state"])
                if state in {"sent", "ready"}:
                    connection.commit()
                    return {"state": state, "error_code": existing["error_code"]}
                if state == "active":
                    connection.execute(
                        "UPDATE wechat_callback_deliveries SET state='uncertain',"
                        "payload_envelope_json=NULL,error_code='delivery_uncertain',"
                        "updated_at=? WHERE binding_id=? AND idempotency_key=?",
                        (_iso(self.clock()), request.binding_id, idempotency_key),
                    )
                    connection.commit()
                    raise WechatCallbackError(
                        "delivery_uncertain", status_code=409, uncertain=True
                    )
                raise WechatCallbackError(
                    str(existing["error_code"] or "delivery_failed"), status_code=409
                )
            envelope = self.cipher.encrypt(
                text,
                associated_data=f"wechat-delivery:{request.binding_id}:{idempotency_key}",
            )
            now = _iso(self.clock())
            connection.execute(
                "INSERT INTO wechat_callback_deliveries("
                "binding_id,idempotency_key,event_id,request_sha256,"
                "payload_envelope_json,state,created_at,updated_at) "
                "VALUES(?,?,?,?,?,'active',?,?)",
                (
                    request.binding_id,
                    idempotency_key,
                    request.event_id,
                    request_sha,
                    envelope,
                    now,
                    now,
                ),
            )
            if str(binding["channel_id"]) == "wechatmp":
                deadline = inbox["passive_deadline_at"]
                if (
                    not isinstance(deadline, str)
                    or datetime.fromisoformat(deadline) <= self.clock()
                ):
                    connection.execute(
                        "UPDATE wechat_callback_deliveries SET state='failed',"
                        "payload_envelope_json=NULL,error_code='passive_reply_expired',"
                        "updated_at=? WHERE binding_id=? AND idempotency_key=?",
                        (now, request.binding_id, idempotency_key),
                    )
                    connection.commit()
                    raise WechatCallbackError(
                        "passive_reply_expired", status_code=409
                    )
                connection.execute(
                    "UPDATE wechat_callback_deliveries SET state='ready',updated_at=? "
                    "WHERE binding_id=? AND idempotency_key=?",
                    (now, request.binding_id, idempotency_key),
                )
                connection.commit()
                return {"state": "ready", "error_code": None}
            reply_envelope = str(inbox["reply_envelope_json"])
            credential_envelope = str(binding["credential_envelope_json"])
            channel_id = str(binding["channel_id"])
            connection.commit()
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()
        credentials = self._decrypt_credentials(
            request.binding_id, credential_envelope
        )
        try:
            reply_context = json.loads(
                self.cipher.decrypt(
                    reply_envelope,
                    associated_data="wechat-reply:" + request.event_id,
                )
            )
            if not isinstance(reply_context, dict):
                raise ValueError
        except Exception:
            raise WechatCallbackError("reply_context_unavailable") from None
        try:
            provider_message_id = await self.provider.send(
                channel_id, credentials, reply_context, text
            )
        except WechatCallbackError as error:
            self._finish_delivery(
                request.binding_id,
                idempotency_key,
                "uncertain" if error.uncertain else "failed",
                error.code,
            )
            raise
        finally:
            credentials.clear()
            reply_context.clear()
        self._finish_delivery(
            request.binding_id, idempotency_key, "sent", None
        )
        return {
            "state": "sent",
            "error_code": None,
            "provider_message_id_sha256": _sha(provider_message_id),
        }

    def ingest_callback(
        self,
        binding_id: str,
        body: bytes,
        *,
        signature: str,
        timestamp: str,
        nonce: str,
    ) -> dict[str, str] | None:
        credentials = self._public_credentials(binding_id)
        encrypted = self._outer_encrypted(body)
        plaintext = self._crypto(credentials).decrypt(
            encrypted,
            signature=_bounded(signature, "callback_signature", 128),
            timestamp=_bounded(timestamp, "callback_timestamp", 32),
            nonce=_bounded(nonce, "callback_nonce", 128),
        )
        fields = self._xml_fields(plaintext)
        channel_id = credentials["channel_id"]
        if channel_id == "wechat_kf":
            sync_token = fields.get("Token")
            if not sync_token:
                raise WechatCallbackError("callback_payload_invalid", status_code=422)
            self._mark_kf_dirty(binding_id, sync_token)
            credentials.clear()
            return None
        if fields.get("MsgType") != "text" or not fields.get("Content"):
            credentials.clear()
            return None
        to_user = fields.get("ToUserName")
        from_user = fields.get("FromUserName")
        if not to_user or not from_user:
            raise WechatCallbackError("callback_payload_invalid", status_code=422)
        provider_message_id = fields.get("MsgId") or _sha(
            plaintext.decode("utf-8", "strict")
        )
        passive_deadline = (
            self.clock() + timedelta(seconds=self.passive_wait_seconds)
            if channel_id == "wechatmp"
            else None
        )
        event_id = self._store_inbound(
            binding_id,
            credentials,
            _Inbound(
                provider_message_id=provider_message_id,
                conversation_id=from_user,
                text=fields["Content"],
                reply_context={"to_user": from_user},
            ),
            passive_deadline=passive_deadline,
        )
        credentials.clear()
        if channel_id != "wechatmp":
            return None
        return {
            "event_id": event_id,
            "to_user": from_user,
            "from_user": to_user,
            "created_at": fields.get("CreateTime") or str(int(self.clock().timestamp())),
        }

    async def _sync_kf(
        self, principal: ControlPrincipal, binding_id: str
    ) -> None:
        self._validate_binding_id(binding_id)
        connection = self._connect()
        try:
            binding = self._owned_binding(connection, principal, binding_id)
            if str(binding["channel_id"]) != "wechat_kf":
                return
            row = connection.execute(
                "SELECT cursor_envelope_json,sync_token_envelope_json,dirty FROM "
                "wechat_callback_kf_state WHERE binding_id=?",
                (binding_id,),
            ).fetchone()
        finally:
            connection.close()
        if row is None or int(row["dirty"]) == 0:
            return
        sync_envelope = row["sync_token_envelope_json"]
        if not isinstance(sync_envelope, str):
            raise WechatCallbackError("kf_sync_token_unavailable")
        try:
            sync_token = self.cipher.decrypt(
                sync_envelope, associated_data="wechat-kf-token:" + binding_id
            )
            cursor = (
                self.cipher.decrypt(
                    str(row["cursor_envelope_json"]),
                    associated_data="wechat-kf-cursor:" + binding_id,
                )
                if isinstance(row["cursor_envelope_json"], str)
                else ""
            )
        except Exception:
            raise WechatCallbackError("kf_cursor_unavailable") from None
        credentials = self._decrypt_credentials(
            binding_id, str(binding["credential_envelope_json"])
        )
        try:
            result = await self.provider.sync_kf(
                credentials, cursor=cursor, sync_token=sync_token
            )
        finally:
            credentials.clear()
            sync_token = ""
            cursor = ""
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            for message in result.messages:
                self._store_inbound_tx(
                    connection,
                    binding_id,
                    str(binding["channel_id"]),
                    str(binding["account_id"]),
                    str(binding["organization_id"]),
                    message,
                    passive_deadline=None,
                )
            cursor_envelope = self.cipher.encrypt(
                result.cursor, associated_data="wechat-kf-cursor:" + binding_id
            )
            connection.execute(
                "UPDATE wechat_callback_kf_state SET cursor_envelope_json=?,dirty=?,"
                "updated_at=? WHERE binding_id=?",
                (
                    cursor_envelope,
                    1 if result.has_more else 0,
                    _iso(self.clock()),
                    binding_id,
                ),
            )
            self._enqueue_audit(
                connection,
                event_type="wechat_callback.kf.synchronized",
                account_id=str(binding["account_id"]),
                organization_id=str(binding["organization_id"]),
                binding_id=binding_id,
                payload={
                    "channel_id": "wechat_kf",
                    "message_count": len(result.messages),
                    "has_more": result.has_more,
                },
            )
            connection.commit()
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def _store_inbound(
        self,
        binding_id: str,
        credentials: Mapping[str, str],
        inbound: _Inbound,
        *,
        passive_deadline: datetime | None,
    ) -> str:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            binding = connection.execute(
                "SELECT channel_id,account_id,organization_id FROM "
                "wechat_callback_bindings WHERE binding_id=? AND status='enabled'",
                (binding_id,),
            ).fetchone()
            if binding is None or str(binding["channel_id"]) != credentials["channel_id"]:
                raise WechatCallbackError("binding_unavailable", status_code=404)
            event_id = self._store_inbound_tx(
                connection,
                binding_id,
                str(binding["channel_id"]),
                str(binding["account_id"]),
                str(binding["organization_id"]),
                inbound,
                passive_deadline=passive_deadline,
            )
            connection.commit()
            return event_id
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def _store_inbound_tx(
        self,
        connection: sqlite3.Connection,
        binding_id: str,
        channel_id: str,
        account_id: str,
        organization_id: str,
        inbound: _Inbound,
        *,
        passive_deadline: datetime | None,
    ) -> str:
        message_id = _bounded(inbound.provider_message_id, "message_id", 1024)
        conversation_id = _bounded(inbound.conversation_id, "conversation_id", 1024)
        text = _bounded(inbound.text, "message_text", _MAX_TEXT_BYTES)
        provider_sha = _sha(message_id)
        event_id = "wxevt_" + hashlib.sha256(
            f"{binding_id}\0{message_id}".encode()
        ).hexdigest()
        payload = {
            "channel_id": channel_id,
            "conversation_id": conversation_id,
            "message_id": message_id,
            "text": text,
        }
        payload_envelope = self.cipher.encrypt(
            _canonical(payload), associated_data="wechat-inbox:" + event_id
        )
        reply_envelope = self.cipher.encrypt(
            _canonical(dict(inbound.reply_context)),
            associated_data="wechat-reply:" + event_id,
        )
        created_at = _iso(self.clock())
        connection.execute(
            "INSERT OR IGNORE INTO wechat_callback_inbox("
            "event_id,binding_id,provider_message_sha256,payload_envelope_json,"
            "reply_envelope_json,state,passive_deadline_at,created_at) "
            "VALUES(?,?,?,?,?,'ready',?,?)",
            (
                event_id,
                binding_id,
                provider_sha,
                payload_envelope,
                reply_envelope,
                _iso(passive_deadline) if passive_deadline is not None else None,
                created_at,
            ),
        )
        self._enqueue_audit(
            connection,
            event_type="wechat_callback.inbox.received",
            account_id=account_id,
            organization_id=organization_id,
            binding_id=binding_id,
            payload={
                "channel_id": channel_id,
                "event_id_sha256": _sha(event_id),
                "provider_message_sha256": provider_sha,
            },
        )
        return event_id

    def _mark_kf_dirty(self, binding_id: str, sync_token: str) -> None:
        sync_token = _bounded(sync_token, "kf_sync_token", 64 * 1024)
        envelope = self.cipher.encrypt(
            sync_token, associated_data="wechat-kf-token:" + binding_id
        )
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            binding = connection.execute(
                "SELECT account_id,organization_id,channel_id FROM "
                "wechat_callback_bindings WHERE binding_id=? AND status='enabled'",
                (binding_id,),
            ).fetchone()
            if binding is None or str(binding["channel_id"]) != "wechat_kf":
                raise WechatCallbackError("binding_unavailable", status_code=404)
            connection.execute(
                "UPDATE wechat_callback_kf_state SET sync_token_envelope_json=?,"
                "dirty=1,updated_at=? WHERE binding_id=?",
                (envelope, _iso(self.clock()), binding_id),
            )
            self._enqueue_audit(
                connection,
                event_type="wechat_callback.kf.notified",
                account_id=str(binding["account_id"]),
                organization_id=str(binding["organization_id"]),
                binding_id=binding_id,
                payload={"channel_id": "wechat_kf"},
            )
            connection.commit()
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    async def _wait_passive(self, binding_id: str, event_id: str) -> str | None:
        deadline = asyncio.get_running_loop().time() + self.passive_wait_seconds
        while asyncio.get_running_loop().time() < deadline:
            connection = self._connect()
            try:
                row = connection.execute(
                    "SELECT idempotency_key,payload_envelope_json FROM "
                    "wechat_callback_deliveries WHERE binding_id=? AND event_id=? "
                    "AND state='ready' ORDER BY created_at LIMIT 1",
                    (binding_id, event_id),
                ).fetchone()
                if row is not None:
                    key = str(row["idempotency_key"])
                    envelope = row["payload_envelope_json"]
                    if not isinstance(envelope, str):
                        raise WechatCallbackError("passive_reply_unavailable")
                    try:
                        text = self.cipher.decrypt(
                            envelope,
                            associated_data=f"wechat-delivery:{binding_id}:{key}",
                        )
                    except Exception:
                        raise WechatCallbackError("passive_reply_unavailable") from None
                    connection.execute("BEGIN IMMEDIATE")
                    changed = connection.execute(
                        "UPDATE wechat_callback_deliveries SET state='sent',"
                        "payload_envelope_json=NULL,updated_at=? WHERE binding_id=? "
                        "AND idempotency_key=? AND state='ready'",
                        (_iso(self.clock()), binding_id, key),
                    ).rowcount
                    if changed == 1:
                        connection.execute(
                            "UPDATE wechat_callback_inbox SET reply_envelope_json=NULL "
                            "WHERE binding_id=? AND event_id=?",
                            (binding_id, event_id),
                        )
                        connection.commit()
                        return text
                    connection.rollback()
            finally:
                connection.close()
            await asyncio.sleep(0.05)
        return None

    @staticmethod
    def _passive_xml(event: Mapping[str, str], text: str) -> bytes:
        root = ElementTree.Element("xml")
        for name, value in (
            ("ToUserName", event["to_user"]),
            ("FromUserName", event["from_user"]),
            ("CreateTime", event["created_at"]),
            ("MsgType", "text"),
            ("Content", text),
        ):
            ElementTree.SubElement(root, name).text = value
        return ElementTree.tostring(root, encoding="utf-8")

    def _finish_delivery(
        self,
        binding_id: str,
        idempotency_key: str,
        state: str,
        error_code: str | None,
    ) -> None:
        if state not in {"sent", "failed", "uncertain"}:
            raise ValueError("WeChat delivery terminal state is invalid")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT event_id FROM wechat_callback_deliveries WHERE "
                "binding_id=? AND idempotency_key=? AND state='active'",
                (binding_id, idempotency_key),
            ).fetchone()
            if row is None:
                raise WechatCallbackError("delivery_state_conflict", status_code=409)
            connection.execute(
                "UPDATE wechat_callback_deliveries SET state=?,"
                "payload_envelope_json=NULL,error_code=?,updated_at=? WHERE "
                "binding_id=? AND idempotency_key=? AND state='active'",
                (
                    state,
                    error_code,
                    _iso(self.clock()),
                    binding_id,
                    idempotency_key,
                ),
            )
            connection.execute(
                "UPDATE wechat_callback_inbox SET reply_envelope_json=NULL "
                "WHERE binding_id=? AND event_id=?",
                (binding_id, str(row["event_id"])),
            )
            connection.commit()
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def _public_credentials(self, binding_id: str) -> dict[str, str]:
        self._validate_binding_id(binding_id)
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT channel_id,credential_envelope_json FROM "
                "wechat_callback_bindings WHERE binding_id=? AND status='enabled'",
                (binding_id,),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            raise WechatCallbackError("binding_unavailable", status_code=404)
        material = self._decrypt_credentials(
            binding_id, str(row["credential_envelope_json"])
        )
        material["channel_id"] = str(row["channel_id"])
        return material

    def _decrypt_credentials(
        self, binding_id: str, envelope: str
    ) -> dict[str, str]:
        try:
            value = json.loads(
                self.cipher.decrypt(
                    envelope, associated_data="wechat-binding:" + binding_id
                )
            )
        except Exception:
            raise WechatCallbackError("binding_credentials_unavailable") from None
        if not isinstance(value, dict) or set(value) != {
            "app_id",
            "agent_id",
            "app_secret",
            "token",
            "encoding_aes_key",
        } or any(not isinstance(item, str) for item in value.values()):
            raise WechatCallbackError("binding_credentials_unavailable")
        return value

    @staticmethod
    def _crypto(credentials: Mapping[str, str]) -> _WechatCrypto:
        return _WechatCrypto(
            token=credentials["token"],
            aes_key=credentials["encoding_aes_key"],
            receive_id=credentials["app_id"],
        )

    @staticmethod
    def _outer_encrypted(body: bytes) -> str:
        fields = WechatCallbackGateway._xml_fields(body)
        encrypted = fields.get("Encrypt")
        if not encrypted:
            raise WechatCallbackError("callback_encryption_required", status_code=403)
        return encrypted

    @staticmethod
    def _xml_fields(body: bytes) -> dict[str, str]:
        if (
            not body
            or len(body) > _MAX_CALLBACK_BYTES
            or b"<!DOCTYPE" in body.upper()
            or b"<!ENTITY" in body.upper()
        ):
            raise WechatCallbackError("callback_payload_invalid", status_code=422)
        try:
            root = ElementTree.fromstring(body)
        except ElementTree.ParseError:
            raise WechatCallbackError("callback_payload_invalid", status_code=422) from None
        if root.tag != "xml" or len(root) > 128:
            raise WechatCallbackError("callback_payload_invalid", status_code=422)
        fields: dict[str, str] = {}
        for child in root:
            if len(child) or child.tag in fields or child.text is None:
                raise WechatCallbackError("callback_payload_invalid", status_code=422)
            if len(child.text.encode("utf-8")) > _MAX_CALLBACK_BYTES:
                raise WechatCallbackError("callback_payload_invalid", status_code=422)
            fields[child.tag] = child.text
        return fields

    def _owned_binding(
        self,
        connection: sqlite3.Connection,
        principal: ControlPrincipal,
        binding_id: str,
    ) -> sqlite3.Row:
        self._validate_binding_id(binding_id)
        row = connection.execute(
            "SELECT * FROM wechat_callback_bindings WHERE binding_id=? "
            "AND account_id=? AND organization_id=? AND status='enabled'",
            (binding_id, principal.account_id, _organization(principal)),
        ).fetchone()
        if row is None:
            raise WechatCallbackError("binding_unavailable", status_code=404)
        return row

    @staticmethod
    def _validate_binding_id(binding_id: str) -> None:
        if _BINDING_ID.fullmatch(binding_id) is None:
            raise WechatCallbackError("binding_id_invalid", status_code=422)

    @staticmethod
    def _validate_mutation(request: InboxMutationRequest) -> None:
        WechatCallbackGateway._validate_binding_id(request.binding_id)
        if (
            not request.event_id.startswith("wxevt_")
            or _LEASE_ID.fullmatch(request.lease_id) is None
        ):
            raise WechatCallbackError("inbox_identity_invalid", status_code=422)

    def _enqueue_audit(
        self,
        connection: sqlite3.Connection,
        *,
        event_type: str,
        account_id: str,
        organization_id: str,
        binding_id: str,
        payload: Mapping[str, Any],
    ) -> None:
        source = event_type + "\0" + binding_id + "\0" + _canonical(dict(payload))
        audit_id = "audit_" + hashlib.sha256(source.encode()).hexdigest()
        connection.execute(
            "INSERT OR IGNORE INTO wechat_callback_audit_outbox("
            "audit_id,event_type,account_id,organization_id,binding_id,payload_json,"
            "created_at) VALUES(?,?,?,?,?,?,?)",
            (
                audit_id,
                event_type,
                account_id,
                organization_id,
                binding_id,
                _canonical(dict(payload)),
                _iso(self.clock()),
            ),
        )

    def _drain_audit(self) -> None:
        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT * FROM wechat_callback_audit_outbox WHERE delivered_at IS NULL "
                "ORDER BY created_at,audit_id LIMIT 100"
            ).fetchall()
        finally:
            connection.close()
        for row in rows:
            payload = json.loads(str(row["payload_json"]))
            payload["organization_id"] = str(row["organization_id"])
            payload["binding_id_sha256"] = _sha(str(row["binding_id"]))
            encoded = json_dumps(payload).encode()
            created = datetime.fromisoformat(str(row["created_at"]))
            record = AuditRecordProjection(
                audit_id=str(row["audit_id"]),
                source_event_id=str(row["audit_id"]),
                category="connector",
                event_type=str(row["event_type"]),
                account_id=str(row["account_id"]),
                payload=payload,
                payload_sha256=hashlib.sha256(encoded).hexdigest(),
                binary_included=False,
                delivery_status="published",
                attempts=1,
                created_at=created,
                published_at=self.clock(),
            )
            principal = ControlPrincipal(
                subject="wechat-callback:" + _sha(str(row["binding_id"]))[:16],
                client_id="wechat-provider",
                account_id=str(row["account_id"]),
                organization_id=str(row["organization_id"]),
            )
            try:
                self.audit_repository.ingest(
                    principal, record, idempotency_key=record.audit_id
                )
            except Exception:
                continue
            connection = self._connect()
            try:
                with connection:
                    connection.execute(
                        "UPDATE wechat_callback_audit_outbox SET delivered_at=? "
                        "WHERE audit_id=? AND delivered_at IS NULL",
                        (_iso(self.clock()), record.audit_id),
                    )
            finally:
                connection.close()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    @staticmethod
    def _http_error(error: WechatCallbackError) -> HTTPException:
        return HTTPException(
            status_code=error.status_code,
            detail={
                "code": error.code,
                "retryable": error.retryable,
                "uncertain": error.uncertain,
            },
        )


__all__ = [
    "BindingRequest",
    "InboxMutationRequest",
    "OutboundRequest",
    "PullRequest",
    "WechatCallbackError",
    "WechatCallbackGateway",
    "WechatProviderClient",
]
