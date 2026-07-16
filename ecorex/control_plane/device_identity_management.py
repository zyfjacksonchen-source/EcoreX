"""Administrator-management directory adapter for device identities."""

from __future__ import annotations

from .device_identity import (
    DeviceAccountIdentity,
    DeviceIdentityNotFound,
    DeviceIdentityUnauthorized,
)
from .management import AdminManagementNotFound, AdminManagementRepository


_PLATFORM_ADMIN_ROLES = (
    "model_admin",
    "platform_admin",
    "release_admin",
    "user",
    "user_admin",
)


class AdminManagementDeviceAccountDirectory:
    """Project only active user/catalog data; model secrets never leave storage."""

    def __init__(
        self,
        repository: AdminManagementRepository,
        *,
        platform_admin_account_ids: frozenset[str] = frozenset(),
    ) -> None:
        if not isinstance(repository, AdminManagementRepository):
            raise TypeError("administrator management repository is required")
        self.repository = repository
        self.platform_admin_account_ids = frozenset(platform_admin_account_ids)
        for account_id in self.platform_admin_account_ids:
            try:
                user = repository.get_user(account_id)
            except AdminManagementNotFound:
                raise ValueError(
                    "configured platform administrator does not exist"
                ) from None
            if user.status != "active":
                raise ValueError("configured platform administrator is not active")

    def resolve(self, account_id: str) -> DeviceAccountIdentity:
        try:
            user = self.repository.get_user(account_id)
        except AdminManagementNotFound:
            raise DeviceIdentityNotFound("device account does not exist") from None
        if user.status != "active":
            raise DeviceIdentityUnauthorized("device account is suspended")
        models = tuple(
            sorted(
                {
                    str(item["local_model_id"])
                    for item in self.repository.active_public_catalog()
                    if item.get("local_model_id")
                }
            )
        )
        if not models:
            raise DeviceIdentityNotFound("device account has no managed models")
        token_remaining = max(0, user.token_limit - user.tokens_used)
        image_remaining = max(0, user.image_limit - user.images_used)
        # A zero configured limit means unlimited in the v0.2.9.2/v1 admin
        # contract. The access-token verifier itself caps request_limit at 1m.
        request_limit = (
            1_000_000 if user.token_limit == 0 else min(1_000_000, token_remaining)
        )
        return DeviceAccountIdentity(
            account_id=user.account_id,
            organization_id=user.organization_id or f"personal:{user.account_id}",
            display_name=user.display_name,
            roles=(
                _PLATFORM_ADMIN_ROLES
                if user.account_id in self.platform_admin_account_ids
                else ("user",)
            ),
            model_allowlist=models,
            quota={
                "managed_requests": max(1, request_limit),
                "concurrent_requests": 4,
                "token_limit": user.token_limit,
                "tokens_remaining": token_remaining,
                "image_limit": user.image_limit,
                "images_remaining": image_remaining,
            },
        )


__all__ = ["AdminManagementDeviceAccountDirectory"]
