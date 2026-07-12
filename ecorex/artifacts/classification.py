"""Backend-authoritative artifact classification policy.

No caller-provided visibility or family hint may turn implementation output
into an office deliverable.  This module is the single policy boundary used by
the repository before an artifact can enter a user projection.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Iterable

from .models import (
    ArtifactAction,
    ArtifactFamily,
    ArtifactRole,
    ArtifactVisibility,
)
from .identity import canonicalize_filename_for_policy


SOURCE_CODE_EXTENSIONS = frozenset(
    {
        ".c",
        ".cc",
        ".clj",
        ".cljs",
        ".coffee",
        ".cpp",
        ".cs",
        ".css",
        ".dart",
        ".ex",
        ".exs",
        ".go",
        ".groovy",
        ".h",
        ".hpp",
        ".ipynb",
        ".java",
        ".jl",
        ".js",
        ".jsx",
        ".kt",
        ".kts",
        ".less",
        ".lua",
        ".m",
        ".mm",
        ".php",
        ".pl",
        ".pm",
        ".py",
        ".pyw",
        ".r",
        ".rb",
        ".rs",
        ".sass",
        ".scala",
        ".scss",
        ".sol",
        ".sql",
        ".svelte",
        ".swift",
        ".ts",
        ".tsx",
        ".vb",
        ".vue",
        ".wasm",
        ".xml",
    }
)

SCRIPT_EXTENSIONS = frozenset(
    {".bash", ".bat", ".cmd", ".command", ".fish", ".ps1", ".sh", ".zsh"}
)
DIFF_EXTENSIONS = frozenset({".diff", ".patch", ".rej"})
LOG_EXTENSIONS = frozenset({".log", ".out", ".trace"})
TEMPORARY_EXTENSIONS = frozenset(
    {".bak", ".cache", ".lock", ".part", ".pid", ".state", ".swp", ".temp", ".tmp"}
)

DOCUMENT_EXTENSIONS = frozenset({".doc", ".docx", ".odt", ".pages", ".rtf", ".txt"})
SPREADSHEET_EXTENSIONS = frozenset({".numbers", ".ods", ".xls", ".xlsb", ".xlsm", ".xlsx"})
PRESENTATION_EXTENSIONS = frozenset({".key", ".odp", ".ppt", ".pptm", ".pptx"})
PDF_EXTENSIONS = frozenset({".pdf"})
IMAGE_EXTENSIONS = frozenset(
    {".avif", ".bmp", ".gif", ".heic", ".heif", ".jpeg", ".jpg", ".png", ".tif", ".tiff", ".webp"}
)
AUDIO_EXTENSIONS = frozenset({".aac", ".flac", ".m4a", ".mp3", ".ogg", ".opus", ".wav"})
VIDEO_EXTENSIONS = frozenset({".avi", ".m4v", ".mkv", ".mov", ".mp4", ".mpeg", ".webm"})

# These formats are useful office outputs only when a trusted tool explicitly
# declares them as a final deliverable.  Their extension alone is insufficient.
EXPLICIT_DELIVERABLE_EXTENSIONS = frozenset(
    {".csv", ".htm", ".html", ".json", ".markdown", ".md", ".zip"}
)

_CODE_MIME_PREFIXES = (
    "text/x-python",
    "text/javascript",
    "application/javascript",
    "application/typescript",
    "text/typescript",
    "text/css",
    "text/x-c",
    "text/x-c++",
    "text/x-java",
    "text/x-go",
    "text/x-rust",
    "application/x-ipynb+json",
)
_SCRIPT_MIME_TYPES = frozenset(
    {"application/x-bat", "application/x-powershell", "application/x-sh", "text/x-shellscript"}
)
_DIFF_MIME_TYPES = frozenset({"text/x-diff", "text/x-patch"})
_LOG_MIME_TYPES = frozenset({"text/x-log"})

_FAMILY_EXTENSION_MAP: tuple[tuple[frozenset[str], ArtifactFamily], ...] = (
    (DOCUMENT_EXTENSIONS, ArtifactFamily.DOCUMENT),
    (SPREADSHEET_EXTENSIONS, ArtifactFamily.SPREADSHEET),
    (PRESENTATION_EXTENSIONS, ArtifactFamily.PRESENTATION),
    (PDF_EXTENSIONS, ArtifactFamily.PDF),
    (IMAGE_EXTENSIONS, ArtifactFamily.IMAGE),
    (AUDIO_EXTENSIONS, ArtifactFamily.AUDIO),
    (VIDEO_EXTENSIONS, ArtifactFamily.VIDEO),
)
_ORDINARY_EXTENSIONS = frozenset().union(
    DOCUMENT_EXTENSIONS,
    SPREADSHEET_EXTENSIONS,
    PRESENTATION_EXTENSIONS,
    PDF_EXTENSIONS,
    IMAGE_EXTENSIONS,
    AUDIO_EXTENSIONS,
    VIDEO_EXTENSIONS,
)

INTERNAL_ARTIFACT_FAMILIES = frozenset(
    {
        ArtifactFamily.SOURCE_CODE,
        ArtifactFamily.SCRIPT,
        ArtifactFamily.DIFF,
        ArtifactFamily.LOG,
        ArtifactFamily.TEMPORARY,
        ArtifactFamily.DIRECTORY,
    }
)
INTERNAL_ARTIFACT_ROLES = frozenset(
    {
        ArtifactRole.RENDITION,
        ArtifactRole.SOURCE,
        ArtifactRole.INTERMEDIATE,
        ArtifactRole.DIAGNOSTIC,
    }
)

# These two projections are part of the public Runtime/WebUI contract.  Keep
# the policy next to the classifier that enforces it so contract generation
# cannot grow a second, drifting list of "safe" office artifact values.
PUBLIC_ARTIFACT_FAMILIES = tuple(
    family for family in ArtifactFamily if family not in INTERNAL_ARTIFACT_FAMILIES
)
PUBLIC_ARTIFACT_VISIBILITIES = (
    ArtifactVisibility.PRIMARY,
    ArtifactVisibility.SECONDARY,
)


@dataclass(frozen=True, slots=True)
class ClassificationDecision:
    family: ArtifactFamily
    role: ArtifactRole
    visibility: ArtifactVisibility
    actions: tuple[ArtifactAction, ...]
    reasons: tuple[str, ...]

    @property
    def is_user_visible(self) -> bool:
        return self.visibility in {ArtifactVisibility.PRIMARY, ArtifactVisibility.SECONDARY}


def _basename(value: str) -> str:
    return canonicalize_filename_for_policy(value)


def _extension(value: str) -> str:
    return PurePosixPath(_basename(value)).suffix.casefold()


def _normalize_mime(value: str) -> str:
    return str(value or "application/octet-stream").split(";", 1)[0].strip().casefold()


def _hard_family(name: str, extension: str, mime_type: str) -> tuple[ArtifactFamily | None, str | None]:
    lowered_name = name.casefold()
    if lowered_name.startswith("~$") or lowered_name in {".ds_store", "thumbs.db", "desktop.ini"}:
        return ArtifactFamily.TEMPORARY, "temporary_name"
    if lowered_name == ".env" or lowered_name.startswith(".env."):
        return ArtifactFamily.TEMPORARY, "sensitive_environment_file"
    if extension in SOURCE_CODE_EXTENSIONS or mime_type.startswith(_CODE_MIME_PREFIXES):
        return ArtifactFamily.SOURCE_CODE, "implementation_source"
    if extension in SCRIPT_EXTENSIONS or mime_type in _SCRIPT_MIME_TYPES:
        return ArtifactFamily.SCRIPT, "implementation_script"
    if extension in DIFF_EXTENSIONS or mime_type in _DIFF_MIME_TYPES:
        return ArtifactFamily.DIFF, "implementation_diff"
    if extension in LOG_EXTENSIONS or mime_type in _LOG_MIME_TYPES:
        return ArtifactFamily.LOG, "diagnostic_log"
    if extension in TEMPORARY_EXTENSIONS:
        return ArtifactFamily.TEMPORARY, "temporary_extension"
    return None, None


def _ordinary_family(extension: str, mime_type: str) -> ArtifactFamily | None:
    for extensions, family in _FAMILY_EXTENSION_MAP:
        if extension in extensions:
            return family
    # A MIME hint may classify an extensionless output, but it must never
    # relabel an explicitly unknown/unsafe suffix such as report.pdf.exe.
    if extension and extension not in _ORDINARY_EXTENSIONS:
        return None
    if mime_type == "application/pdf":
        return ArtifactFamily.PDF
    if mime_type.startswith("image/") and mime_type != "image/svg+xml":
        return ArtifactFamily.IMAGE
    if mime_type.startswith("audio/"):
        return ArtifactFamily.AUDIO
    if mime_type.startswith("video/"):
        return ArtifactFamily.VIDEO
    if mime_type in {
        "application/msword",
        "application/rtf",
        "application/vnd.oasis.opendocument.text",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    }:
        return ArtifactFamily.DOCUMENT
    if mime_type in {
        "application/vnd.ms-excel",
        "application/vnd.oasis.opendocument.spreadsheet",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    }:
        return ArtifactFamily.SPREADSHEET
    if mime_type in {
        "application/vnd.ms-powerpoint",
        "application/vnd.oasis.opendocument.presentation",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    }:
        return ArtifactFamily.PRESENTATION
    return None


def _explicit_family(extension: str, mime_type: str) -> ArtifactFamily:
    if extension in {".htm", ".html"} or mime_type == "text/html":
        return ArtifactFamily.WEB_REPORT
    if extension == ".zip" or mime_type == "application/zip":
        return ArtifactFamily.ARCHIVE
    if extension in {".csv", ".json"} or mime_type in {"application/json", "text/csv"}:
        return ArtifactFamily.DATA_EXPORT
    if extension in {".md", ".markdown"} or mime_type in {"text/markdown", "text/plain"}:
        return ArtifactFamily.DOCUMENT
    return ArtifactFamily.DATA_EXPORT


def _actions_for(family: ArtifactFamily, mime_type: str) -> tuple[ArtifactAction, ...]:
    actions: list[ArtifactAction] = []
    if family in {
        ArtifactFamily.DOCUMENT,
        ArtifactFamily.SPREADSHEET,
        ArtifactFamily.PRESENTATION,
        ArtifactFamily.PDF,
        ArtifactFamily.IMAGE,
        ArtifactFamily.AUDIO,
        ArtifactFamily.VIDEO,
        ArtifactFamily.DATA_EXPORT,
        ArtifactFamily.WEB_REPORT,
        ArtifactFamily.CLOUD_LINK,
    }:
        actions.append(ArtifactAction.PREVIEW)
    actions.append(ArtifactAction.OPEN)
    if family != ArtifactFamily.CLOUD_LINK:
        actions.extend((ArtifactAction.DOWNLOAD, ArtifactAction.REVEAL))
    actions.append(ArtifactAction.FEEDBACK)
    if family == ArtifactFamily.IMAGE and mime_type in {
        "image/png",
        "image/jpeg",
        "image/webp",
    }:
        actions.append(ArtifactAction.PRECISE_RETOUCH)
    return tuple(actions)


class ArtifactClassifier:
    """Classify an artifact without trusting a UI-provided extension filter."""

    def classify(
        self,
        requested_name: str,
        mime_type: str,
        *,
        role: ArtifactRole = ArtifactRole.DELIVERABLE,
        requested_visibility: ArtifactVisibility = ArtifactVisibility.PRIMARY,
        explicit_deliverable: bool = False,
        family_hint: ArtifactFamily | None = None,
    ) -> ClassificationDecision:
        role = ArtifactRole(role)
        requested_visibility = ArtifactVisibility(requested_visibility)
        family_hint = ArtifactFamily(family_hint) if family_hint is not None else None
        name = _basename(requested_name)
        extension = _extension(name)
        normalized_mime = _normalize_mime(mime_type)
        reasons: list[str] = []

        family, hard_reason = _hard_family(name, extension, normalized_mime)
        if family is not None:
            reasons.append(hard_reason or "hard_internal_family")
        else:
            ordinary = _ordinary_family(extension, normalized_mime)
            if family_hint in INTERNAL_ARTIFACT_FAMILIES:
                family = family_hint
                reasons.append("trusted_internal_family_hint")
            elif ordinary is not None:
                family = ordinary
                reasons.append("recognized_office_format")
            elif extension in EXPLICIT_DELIVERABLE_EXTENSIONS:
                family = _explicit_family(extension, normalized_mime)
                reasons.append("explicit_deliverable_format")
            elif role in INTERNAL_ARTIFACT_ROLES and family_hint is not None:
                family = family_hint
                reasons.append("trusted_internal_role_family_hint")
            elif (
                explicit_deliverable
                and family_hint == ArtifactFamily.CLOUD_LINK
                and normalized_mime == "application/vnd.ecorex.cloud-link+json"
                and extension in {"", ".link", ".url"}
            ):
                family = ArtifactFamily.CLOUD_LINK
                reasons.append("trusted_cloud_link_declaration")
            else:
                family = ArtifactFamily.TEMPORARY
                reasons.append("unknown_format_fail_closed")

        gated_format = extension in EXPLICIT_DELIVERABLE_EXTENSIONS
        hard_internal = family in INTERNAL_ARTIFACT_FAMILIES
        if hard_internal:
            reasons.append("family_forces_internal")
        if role in INTERNAL_ARTIFACT_ROLES:
            hard_internal = True
            reasons.append("role_forces_internal")
        if requested_visibility == ArtifactVisibility.INTERNAL:
            hard_internal = True
            reasons.append("explicit_internal_visibility")
        if gated_format and not explicit_deliverable:
            hard_internal = True
            reasons.append("missing_explicit_deliverable_declaration")

        visibility = ArtifactVisibility.INTERNAL if hard_internal else requested_visibility
        actions = () if visibility == ArtifactVisibility.INTERNAL else _actions_for(family, normalized_mime)
        return ClassificationDecision(
            family=family,
            role=role,
            visibility=visibility,
            actions=actions,
            reasons=tuple(dict.fromkeys(reasons)),
        )


def is_user_visible(decision: ClassificationDecision) -> bool:
    return decision.is_user_visible


def filter_user_visible(decisions: Iterable[ClassificationDecision]) -> tuple[ClassificationDecision, ...]:
    return tuple(decision for decision in decisions if decision.is_user_visible)
