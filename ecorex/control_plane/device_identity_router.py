"""Bounded FastAPI routes for the managed device identity broker."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import html
import ipaddress
from typing import Literal
from urllib.parse import parse_qs, urlsplit

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from .device_identity import (
    DeviceIdentityConflict,
    DeviceIdentityError,
    DeviceIdentityNotFound,
    DeviceIdentityUnauthorized,
    DeviceIdentityUnavailable,
    ManagedDeviceIdentityBroker,
)
from .management import (
    AdminManagementRepository,
    AdminPasswordAuthenticationError,
    AdminPasswordLocked,
)


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DeviceAuthorizeRequest(_StrictModel):
    schema_version: int = Field(ge=1, le=1)
    client_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$",
    )


class DeviceTokenRequest(DeviceAuthorizeRequest):
    provider_flow_id: str = Field(pattern=r"^dif_[0-9a-f]{32}$")
    device_code: str = Field(min_length=16, max_length=256)


class RefreshTokenRequest(DeviceAuthorizeRequest):
    grant_type: Literal["refresh_token"]
    lease_id: str = Field(
        min_length=1,
        max_length=256,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,255}$",
    )
    refresh_token: str = Field(min_length=16, max_length=4096)


class RevokeSessionRequest(DeviceAuthorizeRequest):
    lease_id: str = Field(
        min_length=1,
        max_length=256,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,255}$",
    )
    account_id: str = Field(
        min_length=1,
        max_length=256,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,255}$",
    )
    refresh_token: str = Field(min_length=16, max_length=4096)


class LegacyDeviceVerifyRequest(_StrictModel):
    schema_version: int = Field(ge=1, le=1)
    user_code: str = Field(pattern=r"^[A-Za-z2-9]{4}-[A-Za-z2-9]{4}$")
    credential: str = Field(min_length=8, max_length=4096)


class AdminDeviceApproveRequest(_StrictModel):
    schema_version: int = Field(ge=1, le=1)
    user_code: str = Field(pattern=r"^[A-Za-z2-9]{4}-[A-Za-z2-9]{4}$")
    account_id: str = Field(
        min_length=1,
        max_length=256,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,255}$",
    )


class PasswordLoginRequest(DeviceAuthorizeRequest):
    identifier: str = Field(min_length=1, max_length=254)
    # Existing accounts may still carry an eight-character legacy password.
    # New credentials and admin resets remain subject to the stricter
    # ten-character management contract.
    password: SecretStr = Field(min_length=8, max_length=256)


def _password_login_source_ip(request: Request) -> str | None:
    """Resolve one rate-limit source without trusting public proxy headers."""

    if request.client is None:
        return None
    try:
        direct = ipaddress.ip_address(request.client.host.strip())
    except (AttributeError, ValueError):
        return None

    mapped = getattr(direct, "ipv4_mapped", None)
    direct_is_loopback = direct.is_loopback or (
        mapped is not None and mapped.is_loopback
    )
    if not direct_is_loopback:
        return direct.compressed

    forwarded = request.headers.getlist("x-real-ip")
    if not forwarded:
        return direct.compressed
    if len(forwarded) != 1 or "," in forwarded[0]:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "invalid_proxy_client_ip",
                "message": "login request source is invalid",
            },
        )
    try:
        return ipaddress.ip_address(forwarded[0].strip()).compressed
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "invalid_proxy_client_ip",
                "message": "login request source is invalid",
            },
        ) from None


def create_device_identity_router(
    broker: ManagedDeviceIdentityBroker,
    *,
    admin_dependency: Callable[..., object] | None = None,
    password_repository: AdminManagementRepository | None = None,
) -> APIRouter:
    if not isinstance(broker, ManagedDeviceIdentityBroker):
        raise TypeError("managed device identity broker is required")
    router = APIRouter()
    verification_path = urlsplit(broker.verification_url).path or "/device"

    @router.get(verification_path, include_in_schema=False)
    async def verification_page() -> HTMLResponse:
        return _verification_html()

    @router.post(verification_path, include_in_schema=False)
    async def verification_submit(request: Request) -> HTMLResponse:
        if request.headers.get("content-type", "").split(";", 1)[0].strip() != (
            "application/x-www-form-urlencoded"
        ):
            return _verification_html("请求无效，请重新输入。", status_code=415)
        length = request.headers.get("content-length")
        if length is None or not length.isdigit() or int(length) > 8192:
            return _verification_html("请求无效，请重新输入。", status_code=400)
        payload = await request.body()
        if len(payload) > 8192:
            return _verification_html("请求无效，请重新输入。", status_code=400)
        try:
            values = parse_qs(
                payload.decode("utf-8"),
                strict_parsing=True,
                max_num_fields=2,
            )
            user_code = values["user_code"][0]
            credential = values["credential"][0]
            if (
                set(values) != {"user_code", "credential"}
                or len(values["user_code"]) != 1
                or len(values["credential"]) != 1
            ):
                raise ValueError
            broker.verify_legacy_credential(
                user_code=user_code,
                credential=credential,
            )
        except (KeyError, IndexError, UnicodeDecodeError, ValueError):
            return _verification_html("请求无效，请重新输入。", status_code=400)
        except DeviceIdentityError:
            return _verification_html(
                "验证未通过，请检查验证码和凭据。", status_code=401
            )
        return _verification_html("验证完成，可以返回 EcoreX。", success=True)

    @router.post("/v1/device/authorize")
    async def authorize(
        request: DeviceAuthorizeRequest,
        idempotency_key: str = Header(
            alias="Idempotency-Key", min_length=8, max_length=256
        ),
    ) -> dict[str, object]:
        try:
            return broker.begin(
                client_id=request.client_id,
                idempotency_key=idempotency_key,
            ).to_dict()
        except DeviceIdentityError as error:
            raise device_identity_error_response(error) from None

    @router.post("/v1/device/token")
    async def token(
        request: DeviceTokenRequest | RefreshTokenRequest,
        idempotency_key: str = Header(
            alias="Idempotency-Key", min_length=8, max_length=256
        ),
    ) -> dict[str, object]:
        try:
            if isinstance(request, RefreshTokenRequest):
                return broker.refresh(
                    client_id=request.client_id,
                    lease_id=request.lease_id,
                    refresh_token=request.refresh_token,
                    idempotency_key=idempotency_key,
                ).to_dict()
            return broker.poll(
                client_id=request.client_id,
                provider_flow_id=request.provider_flow_id,
                device_code=request.device_code,
                idempotency_key=idempotency_key,
            ).to_dict()
        except DeviceIdentityError as error:
            raise device_identity_error_response(error) from None

    @router.post("/v1/device/revoke")
    async def revoke(
        request: RevokeSessionRequest,
        idempotency_key: str = Header(
            alias="Idempotency-Key", min_length=8, max_length=256
        ),
    ) -> dict[str, object]:
        try:
            return broker.revoke(
                client_id=request.client_id,
                lease_id=request.lease_id,
                account_id=request.account_id,
                refresh_token=request.refresh_token,
                idempotency_key=idempotency_key,
            ).to_dict()
        except DeviceIdentityError as error:
            raise device_identity_error_response(error) from None

    if password_repository is not None:
        if not isinstance(password_repository, AdminManagementRepository):
            raise TypeError("password account repository is invalid")

        @router.post("/v1/session/login")
        async def password_login(
            payload: PasswordLoginRequest,
            request: Request,
            idempotency_key: str = Header(
                alias="Idempotency-Key", min_length=8, max_length=256
            ),
        ) -> dict[str, object]:
            password = payload.password.get_secret_value()
            source_ip = _password_login_source_ip(request)
            try:
                user = await asyncio.to_thread(
                    password_repository.authenticate_password,
                    payload.identifier,
                    password,
                    source_ip=source_ip,
                )
                result = await asyncio.to_thread(
                    broker.grant_account,
                    client_id=payload.client_id,
                    account_id=user.account_id,
                    idempotency_key=idempotency_key,
                )
            except AdminPasswordLocked as error:
                raise HTTPException(
                    status_code=429,
                    detail={
                        "code": "password_login_rate_limited",
                        "message": "account login is temporarily unavailable",
                    },
                    headers={"Retry-After": str(error.retry_after_seconds)},
                ) from None
            except AdminPasswordAuthenticationError:
                raise HTTPException(
                    status_code=401,
                    detail={
                        "code": "invalid_credentials",
                        "message": "account login failed",
                    },
                ) from None
            except DeviceIdentityError as error:
                raise device_identity_error_response(error) from None
            finally:
                password = ""
            return result.to_dict()

    @router.post("/v1/device/verify/legacy")
    async def verify_legacy(request: LegacyDeviceVerifyRequest) -> dict[str, object]:
        try:
            broker.verify_legacy_credential(
                user_code=request.user_code,
                credential=request.credential,
            )
        except DeviceIdentityError as error:
            raise device_identity_error_response(error) from None
        return {"schema_version": 1, "status": "authorized"}

    if admin_dependency is not None:

        @router.post("/api/v1/admin/device/approve")
        async def admin_approve(
            request: AdminDeviceApproveRequest,
            _current: object = Depends(admin_dependency),
        ) -> dict[str, object]:
            try:
                lease = broker.approve(
                    user_code=request.user_code,
                    account_id=request.account_id,
                )
            except DeviceIdentityError as error:
                raise device_identity_error_response(error) from None
            return {
                "schema_version": 1,
                "status": "authorized",
                "lease_id": lease.claims.lease_id,
                "expires_at": lease.claims.expires_at.isoformat().replace(
                    "+00:00", "Z"
                ),
                "revision": lease.claims.revision,
            }

    return router


def device_identity_exception_status(error: DeviceIdentityError) -> int:
    if isinstance(error, DeviceIdentityNotFound):
        return 404
    if isinstance(error, DeviceIdentityUnauthorized):
        return 401
    if isinstance(error, DeviceIdentityConflict):
        return 409
    if isinstance(error, DeviceIdentityUnavailable):
        return 503
    return 500


def device_identity_error_response(error: DeviceIdentityError) -> HTTPException:
    # The public message is deliberately invariant; raw credential, signer and
    # persistence failures never cross this boundary.
    return HTTPException(
        status_code=device_identity_exception_status(error),
        detail={"code": error.code, "message": "device authorization failed"},
    )


def _verification_html(
    message: str = "输入 EcoreX 显示的验证码和原有账户凭据。",
    *,
    success: bool = False,
    status_code: int = 200,
) -> HTMLResponse:
    safe_message = html.escape(message, quote=True)
    form = (
        ""
        if success
        else """
      <form method="post" autocomplete="off">
        <label>设备验证码<input name="user_code" inputmode="text" maxlength="9" required></label>
        <label>账户凭据<input name="credential" type="password" maxlength="4096" required></label>
        <button type="submit">继续</button>
      </form>"""
    )
    document = f"""<!doctype html><html lang="zh-CN"><head>
    <meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
    <title>EcoreX 设备登录</title><style>
    :root{{color-scheme:light dark;font:14px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}}
    body{{margin:0;min-height:100vh;display:grid;place-items:center;background:#111;color:#fcfcfc}}
    main{{width:min(360px,calc(100vw - 32px));padding:24px;border-radius:16px;background:#1b1b1b}}
    h1{{font-size:18px;margin:0 0 8px}}p{{color:#aaa;margin:0 0 20px}}label{{display:block;margin:12px 0}}
    input{{box-sizing:border-box;width:100%;margin-top:6px;padding:10px 12px;border:1px solid #333;border-radius:10px;background:#161616;color:inherit}}
    button{{width:100%;padding:10px;border:0;border-radius:10px;background:#e88335;color:#111;font-weight:600}}
    </style></head><body><main><h1>EcoreX 设备登录</h1><p>{safe_message}</p>{form}</main></body></html>"""
    return HTMLResponse(
        document,
        status_code=status_code,
        headers={
            "Cache-Control": "no-store",
            "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'",
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
        },
    )


__all__ = [
    "AdminDeviceApproveRequest",
    "DeviceAuthorizeRequest",
    "DeviceTokenRequest",
    "LegacyDeviceVerifyRequest",
    "RefreshTokenRequest",
    "create_device_identity_router",
    "device_identity_error_response",
    "device_identity_exception_status",
]
