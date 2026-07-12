from __future__ import annotations

from enum import StrEnum


class SharingError(RuntimeError):
    pass


class ShareNotFound(SharingError):
    pass


class ShareConflict(SharingError):
    pass


class ShareUnavailable(SharingError):
    pass


class ShareMediaContractCode(StrEnum):
    """Stable, public-safe reasons why a share cannot preserve an image."""

    IMAGE_PREVIEW_MISSING = "share_image_preview_missing"
    IMAGE_PREVIEW_TOO_LARGE = "share_image_preview_too_large"
    IMAGE_PREVIEW_UNSUPPORTED = "share_image_preview_unsupported"
    IMAGE_PREVIEW_INVALID = "share_image_preview_invalid"
    MEDIA_TOTAL_TOO_LARGE = "share_media_total_too_large"
    SCHEMA_UPGRADE_REQUIRED = "share_schema_upgrade_required"


_MEDIA_ERROR_DETAILS: dict[ShareMediaContractCode, tuple[str, bool, str]] = {
    ShareMediaContractCode.IMAGE_PREVIEW_MISSING: (
        "有图片还没有可分享的预览图。请等待图片处理完成后重试。",
        True,
        "wait_for_preview_then_retry",
    ),
    ShareMediaContractCode.IMAGE_PREVIEW_TOO_LARGE: (
        "图片预览超过分享上限。请生成较小的预览图后重试。",
        True,
        "regenerate_preview_then_retry",
    ),
    ShareMediaContractCode.IMAGE_PREVIEW_UNSUPPORTED: (
        "图片预览格式暂不支持分享。请生成 PNG、JPEG、WebP、GIF 或 AVIF 预览后重试。",
        True,
        "regenerate_preview_then_retry",
    ),
    ShareMediaContractCode.IMAGE_PREVIEW_INVALID: (
        "图片预览未通过完整性检查。请重新生成预览图后重试。",
        True,
        "regenerate_preview_then_retry",
    ),
    ShareMediaContractCode.MEDIA_TOTAL_TOO_LARGE: (
        "本次图片预览总量超过分享上限。请减少图片数量或生成较小预览后重试。",
        True,
        "reduce_images_then_retry",
    ),
    ShareMediaContractCode.SCHEMA_UPGRADE_REQUIRED: (
        "这个分享数据版本已停止发布。请从当前会话重新创建分享链接。",
        False,
        "recreate_share",
    ),
}


class ShareMediaContractError(ShareConflict):
    """A sanitized fail-closed image-sharing decision.

    ``retryable`` describes whether the user may retry after performing the
    returned action. A durable publisher must still treat the already frozen
    invalid payload as terminal; retrying the same bytes cannot repair it.
    """

    def __init__(self, code: ShareMediaContractCode | str) -> None:
        try:
            normalized = ShareMediaContractCode(code)
            message, retryable, action = _MEDIA_ERROR_DETAILS[normalized]
        except (KeyError, ValueError):
            normalized = ShareMediaContractCode.IMAGE_PREVIEW_INVALID
            message, retryable, action = _MEDIA_ERROR_DETAILS[normalized]
        self.code = normalized.value
        self.retryable = retryable
        self.action = action
        self.user_message = message
        super().__init__(message)

    def public_detail(self) -> dict[str, object]:
        return {
            "code": self.code,
            "message": self.user_message,
            "retryable": self.retryable,
            "action": self.action,
        }
