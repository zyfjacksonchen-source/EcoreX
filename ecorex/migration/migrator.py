"""Copy-on-write released EcoreX -> v1.0 migration service.

The migrator never opens a legacy database in write mode.  It builds a new
Runtime database and CAS under a disposable staging directory, verifies both,
re-hashes the source, and only then publishes the target with ``os.replace``.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
import mimetypes
import os
from pathlib import Path
import re
import shutil
import sqlite3
import tempfile
from typing import Any, Callable, Iterable, Mapping
from urllib.parse import parse_qs, urlparse, urlunparse

from ecorex.artifacts import (
    ArtifactProjection,
    ArtifactRole,
    ArtifactService,
    ArtifactVisibility,
    ContentAddressedStore,
    ContentIntegrityError,
)
from ecorex.protocol import CreateTurnRequest
from ecorex.runtime import EventStore, SQLiteDatabase, intent_fingerprint
from ecorex.runtime.database import json_dumps

from .cas_authority import CAS_AUTHORITY_NAME, build_cas_authority
from .crypto import (
    SecretRecord,
    collect_secrets,
    decrypt_quarantine,
    encrypt_quarantine,
    is_secret_key,
)
from .errors import (
    DuplicateLegacyIdError,
    MigrationError,
    MigrationVerificationError,
    QuarantineKeyRequired,
    SourceChangedError,
    SourceLayoutError,
    TargetConflictError,
)
from .inventory import (
    DEFAULT_SOURCE_VERSION,
    SUPPORTED_SOURCE_VERSIONS,
    assert_disjoint_roots,
    inventory_index,
    inventory_source,
    sha256_file,
)
from .legacy import (
    CONVERSATION_CANDIDATES,
    MEMORY_INDEX_CANDIDATES,
    LegacyConversations,
    LegacyMemory,
    LegacyQueuedRequests,
    LegacyReleaseEvidence,
    LegacyRuntimeLedger,
    LegacySchedulerTasks,
    LegacyWarning,
    discover_existing,
    read_conversations,
    read_json_object,
    read_memory,
    read_queued_requests,
    read_release_evidence,
    read_runtime_ledger,
    read_scheduler_tasks,
    snapshot_sqlite,
    sqlite_schema_fingerprint,
)
from .models import (
    BackupRecord,
    MigrationReport,
    MigrationWarning,
    SourceInventory,
)
from .path_security import (
    is_within,
    lexical_absolute,
    lstat_identity,
    reject_link_or_reparse,
    secure_regular_file,
    stable_read_bytes,
)
from .schema import IMPORT_LAYOUT_VERSION, initialize_target_database
from .schema_identity import (
    ImportSchemaIdentity,
    ImportSchemaIdentityError,
    current_import_schema_identity,
    data_generation_id,
)


TARGET_VERSION = "1.0.0"
TARGET_DATABASE_NAME = "runtime.sqlite3"
TARGET_ARTIFACT_ROOT_NAME = "artifacts"
REPORT_NAME = "migration-report.json"
INVENTORY_NAME = "source-inventory.json"
BACKUP_MANIFEST_NAME = "backup-manifest.json"
TRACE_NAME = "migration-trace.jsonl"
QUARANTINE_NAME = "quarantine/legacy-secrets.aesgcm"
LEGACY_MISSING_INPUT_TEXT = "（从 v0.3 导入：原始用户指令不可用）"

REMAINING_MAPPINGS = (
    "Legacy runtime events are preserved as diagnostic history rather than replayed through the v1 reducer.",
    "Active and queued v0.3 runs are staged as user-confirmed recovery drafts and never execute automatically.",
    "Legacy scheduler tasks are preserved disabled until the user confirms the v1 schedule and connector policy.",
    "Legacy permission grants and filesystem paths are not activated; only default/full-access intent is staged for account binding.",
    "Pre-v0.3 memory indexes using memory_chunks/file_metadata instead of chunks/files require a dedicated adapter.",
    "Legacy embeddings and FTS tables are intentionally discarded and must be rebuilt from canonical memory records.",
    "Custom Skill source trees are not installed automatically; only enablement metadata is staged pending contract validation.",
    "Non-Tencent generic MCP command arguments are not replayed; connector metadata requires revalidation or re-authentication.",
    "Remote artifact URLs without an explicit cloud-link contract, oversized files, and files outside source_root remain manual-review items.",
)

_CHANNEL_FIELDS: dict[str, tuple[tuple[str, bool], ...]] = {
    "weixin": (),
    "feishu": (("feishu_app_id", False), ("feishu_app_secret", True)),
    "dingtalk": (("dingtalk_client_id", False), ("dingtalk_client_secret", True)),
    "wecom_bot": (("wecom_bot_id", False), ("wecom_bot_secret", True)),
    "qq": (("qq_app_id", False), ("qq_app_secret", True)),
    "wechatcom_app": (
        ("wechatcom_corp_id", False),
        ("wechatcomapp_agent_id", False),
        ("wechatcomapp_secret", True),
        ("wechatcomapp_token", True),
        ("wechatcomapp_aes_key", True),
        ("wechatcomapp_port", False),
    ),
    "wechat_kf": (
        ("wechat_kf_corp_id", False),
        ("wechat_kf_secret", True),
        ("wechat_kf_token", True),
        ("wechat_kf_aes_key", True),
        ("wechat_kf_port", False),
    ),
    "wechatmp": (
        ("wechatmp_app_id", False),
        ("wechatmp_app_secret", True),
        ("wechatmp_token", True),
        ("wechatmp_aes_key", True),
        ("wechatmp_port", False),
    ),
    "wechatmp_service": (
        ("wechatmp_app_id", False),
        ("wechatmp_app_secret", True),
        ("wechatmp_token", True),
        ("wechatmp_aes_key", True),
        ("wechatmp_port", False),
    ),
    "telegram": (("telegram_token", True),),
    "slack": (("slack_bot_token", True), ("slack_app_token", True)),
    "discord": (("discord_token", True),),
}

_CHANNEL_ALIASES = {
    "wx": "weixin",
    "lark": "feishu",
    "wecom": "wecom_bot",
    "wecom_app": "wechatcom_app",
    "wechatcom": "wechatcom_app",
}

_SENSITIVE_TEXT_PATTERNS = (
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"(?i)\bsk-[A-Za-z0-9_-]{8,}\b"),
    re.compile(
        r"(?i)((?:api[_-]?key|token|secret|password|authorization)\s*[:=]\s*[\"']?)([^\"'\s,;}{]+)"
    ),
)


@dataclass(slots=True)
class MigrationOptions:
    source_root: str | Path
    target_root: str | Path
    source_version: str = DEFAULT_SOURCE_VERSION
    dry_run: bool = False
    quarantine_key: bytes | None = None
    conversation_database: str | Path | None = None
    memory_database: str | Path | None = None
    config_file: str | Path | None = None
    mcp_file: str | Path | None = None
    ui_state_file: str | Path | None = None
    skills_config_file: str | Path | None = None
    permission_file: str | Path | None = None
    release_evidence_file: str | Path | None = None
    baseline_root: str | Path | None = None
    sample_size: int = 3
    max_artifact_bytes: int = 512 * 1024 * 1024
    fault_injector: Callable[[str], None] | None = None


@dataclass(frozen=True, slots=True)
class _ArtifactImport:
    projection: ArtifactProjection
    relative_path: str
    source_sha256: str


def _stable_id(prefix: str, *parts: object) -> str:
    encoded = "\x1f".join(str(part) for part in parts).encode("utf-8")
    return f"{prefix}_{hashlib.sha256(encoded).hexdigest()[:26]}"


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _legacy_datetime(value: Any) -> datetime:
    try:
        timestamp = float(value)
    except (TypeError, ValueError):
        timestamp = 0.0
    if abs(timestamp) > 10_000_000_000:
        timestamp /= 1000.0
    try:
        return datetime.fromtimestamp(timestamp, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return datetime.fromtimestamp(0, tz=timezone.utc)


def _legacy_timestamp(value: Any) -> int:
    if isinstance(value, str) and value.strip():
        raw = value.strip()
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return int(parsed.timestamp())
        except (ValueError, OverflowError, OSError):
            pass
    return int(_legacy_datetime(value).timestamp())


def _json_value(value: Any, *, depth: int = 0) -> Any:
    if depth > 16:
        return "[legacy value exceeded nesting limit]"
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for index, (key, child) in enumerate(value.items()):
            if index >= 4096:
                result["_migration_truncated"] = True
                break
            result[str(key)] = _json_value(child, depth=depth + 1)
        return result
    if isinstance(value, (list, tuple)):
        return [_json_value(child, depth=depth + 1) for child in value[:4096]]
    return str(value)


def _parse_legacy_json(raw: Any, *, fallback_to_text: bool = True) -> Any:
    if not isinstance(raw, str):
        return _json_value(raw)
    if not raw.strip():
        return {} if not fallback_to_text else ""
    try:
        return _json_value(json.loads(raw))
    except (json.JSONDecodeError, TypeError, ValueError):
        return raw if fallback_to_text else {}


def _display_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        return "\n".join(part for item in value if (part := _display_text(item))).strip()
    if isinstance(value, Mapping):
        if isinstance(value.get("text"), str):
            return str(value["text"]).strip()
        if "content" in value:
            return _display_text(value["content"])
    return ""


def _without_secrets(value: Any, *, depth: int = 0) -> Any:
    if depth > 16:
        return "[redacted]"
    if isinstance(value, Mapping):
        return {
            str(key): _without_secrets(child, depth=depth + 1)
            for key, child in value.items()
            if not is_secret_key(str(key)) and str(key) != "artifacts"
        }
    if isinstance(value, list):
        return [_without_secrets(child, depth=depth + 1) for child in value[:4096]]
    return _json_value(value, depth=depth)


def _redact_sensitive_text(value: str) -> str:
    text = value
    for pattern in _SENSITIVE_TEXT_PATTERNS:
        if pattern.pattern.casefold().startswith("(?i)(bearer"):
            text = pattern.sub(r"\1[redacted]", text)
        elif "sk-" in pattern.pattern:
            text = pattern.sub("sk-[redacted]", text)
        else:
            text = pattern.sub(r"\1[redacted]", text)
    return text


def _sanitized_legacy_value(value: Any, *, depth: int = 0) -> Any:
    if depth > 16:
        return "[redacted]"
    if isinstance(value, str):
        return _redact_sensitive_text(value)
    if isinstance(value, Mapping):
        return {
            str(key): _sanitized_legacy_value(child, depth=depth + 1)
            for key, child in list(value.items())[:4096]
            if not is_secret_key(str(key))
        }
    if isinstance(value, (list, tuple)):
        return [_sanitized_legacy_value(child, depth=depth + 1) for child in value[:4096]]
    return _json_value(value, depth=depth)


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def _safe_subject(value: object, *, limit: int = 160) -> str:
    text = str(value or "").replace("\x00", "").strip()
    return text[:limit]


def _quarantine_summary(records: Iterable[SecretRecord]) -> tuple[dict[str, Any], ...]:
    """Aggregate non-sensitive credential categories for the deletion UI."""

    counts: dict[tuple[str, str], int] = defaultdict(int)
    for record in records:
        source = record.source_relative_path.casefold().replace("\\", "/")
        if "mcp" in source:
            origin = "mcp_configuration"
        elif "skill" in source:
            origin = "skill_configuration"
        elif "permission" in source:
            origin = "permission_configuration"
        else:
            origin = "product_configuration"
        key = record.key_path.rsplit(".", 1)[-1].casefold().replace("-", "_")
        compact = key.replace("_", "")
        if "apikey" in compact:
            kind = "api_key"
        elif "refreshtoken" in compact:
            kind = "refresh_token"
        elif "token" in compact or "authorization" in compact or "cookie" in compact:
            kind = "access_token"
        elif "password" in compact:
            kind = "password"
        elif any(marker in compact for marker in ("privatekey", "accesskey", "aeskey")):
            kind = "cryptographic_key"
        elif "secret" in compact:
            kind = "client_secret"
        else:
            kind = "credential"
        counts[(kind, origin)] += 1
    return tuple(
        {"kind": kind, "origin": origin, "count": count}
        for (kind, origin), count in sorted(counts.items())
    )


def _legacy_turn_input_contract_errors(
    connection: sqlite3.Connection,
    *,
    source_inventory_digest: str,
) -> tuple[str, ...]:
    """Validate intent and snapshot facts for directly imported legacy Turns."""

    previous_row_factory = connection.row_factory
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            """
            SELECT
                legacy.legacy_id,
                legacy.legacy_parent_id,
                turn.turn_id,
                turn.thread_id,
                turn.input_text,
                turn.agent_model_id,
                turn.image_model_id,
                turn.client_message_id,
                turn.metadata_json,
                turn.created_at,
                revision.revision_id,
                revision.ordinal,
                revision.source,
                revision.input_text AS revision_input_text,
                revision.agent_model_id AS revision_agent_model_id,
                revision.image_model_id AS revision_image_model_id,
                revision.client_message_id AS revision_client_message_id,
                revision.explicit_tool_ids_json,
                revision.metadata_json AS revision_metadata_json,
                revision.intent_fingerprint,
                revision.created_at AS revision_created_at,
                accepted.payload_json AS accepted_payload_json,
                accepted.client_message_id AS accepted_client_message_id,
                accepted.config_snapshot_id,
                accepted.capability_snapshot_id,
                accepted.permission_snapshot_id,
                accepted.extension_snapshot_id,
                (SELECT COUNT(*) FROM turn_input_revisions AS all_revisions
                 WHERE all_revisions.turn_id = turn.turn_id) AS revision_count,
                (SELECT COUNT(*) FROM turn_execution_batches AS batches
                 WHERE batches.turn_id = turn.turn_id) AS execution_batch_count,
                (SELECT COUNT(*) FROM events AS accepted_events
                 WHERE accepted_events.turn_id = turn.turn_id
                   AND accepted_events.event_type = 'turn.accepted') AS accepted_count,
                (SELECT COUNT(*) FROM events AS snapshot_events
                 WHERE snapshot_events.turn_id = turn.turn_id
                   AND (
                       snapshot_events.config_snapshot_id IS NOT NULL
                       OR snapshot_events.capability_snapshot_id IS NOT NULL
                       OR snapshot_events.permission_snapshot_id IS NOT NULL
                       OR snapshot_events.extension_snapshot_id IS NOT NULL
                   )) AS snapshot_event_count
            FROM legacy_id_map AS legacy
            LEFT JOIN turns AS turn ON turn.turn_id = legacy.target_id
            LEFT JOIN turn_input_revisions AS revision
              ON revision.turn_id = turn.turn_id AND revision.ordinal = 0
            LEFT JOIN events AS accepted
              ON accepted.turn_id = turn.turn_id
             AND accepted.event_type = 'turn.accepted'
            WHERE legacy.entity_kind = 'turn'
            ORDER BY legacy.legacy_parent_id, legacy.legacy_id
            """
        ).fetchall()
    finally:
        connection.row_factory = previous_row_factory

    errors: list[str] = []
    for row in rows:
        subject = f"{row['legacy_parent_id']}:{row['legacy_id']}"
        if row["turn_id"] is None:
            errors.append(f"{subject}:missing_turn")
            continue
        if int(row["revision_count"] or 0) != 1 or row["revision_id"] is None:
            errors.append(f"{subject}:initial_revision_count")
            continue
        if int(row["execution_batch_count"] or 0) != 0:
            errors.append(f"{subject}:historical_turn_has_execution_batch")
        if int(row["accepted_count"] or 0) != 1:
            errors.append(f"{subject}:accepted_event_count")
            continue
        if int(row["snapshot_event_count"] or 0) != 0:
            errors.append(f"{subject}:historical_turn_claims_runtime_snapshot")

        try:
            turn_metadata = json.loads(str(row["metadata_json"]))
            revision_metadata = json.loads(str(row["revision_metadata_json"]))
            explicit_tool_ids = json.loads(str(row["explicit_tool_ids_json"]))
            accepted_payload = json.loads(str(row["accepted_payload_json"]))
        except (TypeError, ValueError, json.JSONDecodeError):
            errors.append(f"{subject}:invalid_intent_json")
            continue
        if (
            not isinstance(turn_metadata, dict)
            or not isinstance(revision_metadata, dict)
            or not isinstance(explicit_tool_ids, list)
            or not isinstance(accepted_payload, dict)
        ):
            errors.append(f"{subject}:invalid_intent_shape")
            continue

        try:
            request = CreateTurnRequest(
                input=str(row["input_text"]),
                agent_model_id=str(row["agent_model_id"]),
                image_model_id=row["image_model_id"],
                explicit_tool_ids=explicit_tool_ids,
                client_message_id=row["client_message_id"],
                metadata=turn_metadata,
            )
        except (TypeError, ValueError):
            errors.append(f"{subject}:invalid_runtime_intent")
            continue

        expected_revision_id = _stable_id(
            "rev",
            source_inventory_digest,
            str(row["legacy_parent_id"]),
            str(row["legacy_id"]),
            "initial",
        )
        if (
            row["revision_id"] != expected_revision_id
            or int(row["ordinal"]) != 0
            or row["source"] != "initial"
            or row["revision_input_text"] != request.input
            or row["revision_agent_model_id"] != request.agent_model_id
            or row["revision_image_model_id"] != request.image_model_id
            or row["revision_client_message_id"] != request.client_message_id
            or explicit_tool_ids != []
            or revision_metadata != request.metadata
            or row["intent_fingerprint"] != intent_fingerprint(request)
            or row["revision_created_at"] != row["created_at"]
        ):
            errors.append(f"{subject}:initial_revision_mismatch")
        if request.agent_model_id != "ecorex-chat" or request.image_model_id not in {
            None,
            "gpt-image-2",
        }:
            errors.append(f"{subject}:unsafe_legacy_model_activation")
        if (
            accepted_payload.get("input") != request.input
            or accepted_payload.get("agent_model_id") != request.agent_model_id
            or accepted_payload.get("image_model_id") != request.image_model_id
            or accepted_payload.get("explicit_tool_ids") != []
            or accepted_payload.get("metadata") != request.metadata
            or accepted_payload.get("model_catalog_snapshot_id") is not None
            or row["accepted_client_message_id"] != request.client_message_id
            or any(
                row[key] is not None
                for key in (
                    "config_snapshot_id",
                    "capability_snapshot_id",
                    "permission_snapshot_id",
                    "extension_snapshot_id",
                )
            )
        ):
            errors.append(f"{subject}:accepted_intent_or_snapshot_mismatch")
    return tuple(errors)


class V030ToV1Migrator:
    def __init__(self, options: MigrationOptions):
        if options.source_version not in SUPPORTED_SOURCE_VERSIONS:
            raise SourceLayoutError("legacy source version is unsupported")
        self.options = options
        self.warnings: list[MigrationWarning] = []
        self.backups: list[BackupRecord] = []
        self.counts: dict[str, int] = defaultdict(int)
        self.secret_records: list[SecretRecord] = []
        self._trace_rows: list[dict[str, Any]] = []
        self._pinned_paths: dict[str, Path] = {}
        self.source_evidence: LegacyReleaseEvidence | None = None
        self._turn_by_request_id: dict[str, str] = {}
        self._thread_ids: dict[str, str] = {}
        self._baseline_counts: dict[str, int] = defaultdict(int)
        self._baseline_artifact_ids: set[str] = set()

    def _trace(self, stage: str, **fields: Any) -> None:
        row = {"stage": stage, "recorded_at": _iso_now()}
        row.update({key: _json_value(value) for key, value in fields.items()})
        self._trace_rows.append(row)
        if self.options.fault_injector is not None:
            self.options.fault_injector(stage)

    def _clone_baseline_state(self, staging: Path) -> None:
        if self.options.baseline_root is None:
            return
        baseline = lexical_absolute(self.options.baseline_root)
        if not baseline.is_dir() or baseline.is_symlink():
            raise TargetConflictError("late migration baseline is not a real v1 state directory")
        if is_within(staging, baseline) or is_within(baseline, staging):
            raise TargetConflictError("late migration baseline overlaps staging")
        source_database = baseline / TARGET_DATABASE_NAME
        if not source_database.is_file() or source_database.is_symlink():
            raise TargetConflictError("late migration baseline database is unavailable")

        for source in baseline.rglob("*"):
            relative = source.relative_to(baseline)
            try:
                reject_link_or_reparse(
                    lstat_identity(source, label="late migration baseline entry"),
                    label="late migration baseline entry",
                )
            except SourceLayoutError as error:
                raise TargetConflictError(
                    "late migration baseline contains a link or reparse point"
                ) from error
            if relative.as_posix() in {
                TARGET_DATABASE_NAME,
                TARGET_DATABASE_NAME + "-wal",
                TARGET_DATABASE_NAME + "-shm",
                REPORT_NAME,
                INVENTORY_NAME,
                BACKUP_MANIFEST_NAME,
                TRACE_NAME,
                CAS_AUTHORITY_NAME,
            }:
                continue
            destination = staging / relative
            if source.is_dir():
                destination.mkdir(parents=True, exist_ok=True)
                continue
            if not source.is_file():
                raise TargetConflictError("late migration baseline contains a special file")
            destination.parent.mkdir(parents=True, exist_ok=True)
            with source.open("rb") as input_file, destination.open("xb") as output_file:
                shutil.copyfileobj(input_file, output_file, length=1024 * 1024)

        destination_database = staging / TARGET_DATABASE_NAME
        source_connection = sqlite3.connect(f"file:{source_database.as_posix()}?mode=ro", uri=True)
        destination_connection = sqlite3.connect(str(destination_database))
        try:
            source_connection.backup(destination_connection)
            integrity = destination_connection.execute("PRAGMA integrity_check").fetchone()
            foreign_keys = destination_connection.execute("PRAGMA foreign_key_check").fetchall()
            mappings = int(
                destination_connection.execute("SELECT COUNT(*) FROM legacy_id_map").fetchone()[0]
            )
            if integrity != ("ok",) or foreign_keys:
                raise TargetConflictError("late migration baseline database failed integrity verification")
            if mappings:
                raise TargetConflictError("late migration baseline already contains legacy mappings")
        except sqlite3.Error as error:
            raise TargetConflictError("late migration baseline database is incompatible") from error
        finally:
            destination_connection.close()
            source_connection.close()

        database = SQLiteDatabase(destination_database)
        artifact_service = ArtifactService(
            staging / TARGET_ARTIFACT_ROOT_NAME,
            database_path=destination_database,
        )
        with database.reader() as connection:
            self._baseline_counts.update(
                {
                    "threads": int(connection.execute("SELECT COUNT(*) FROM threads").fetchone()[0]),
                    "turns": int(connection.execute("SELECT COUNT(*) FROM turns").fetchone()[0]),
                    "turn_input_revisions": int(connection.execute("SELECT COUNT(*) FROM turn_input_revisions").fetchone()[0]),
                    "messages": 0,
                    "artifact_items": 0,
                    "projects": int(connection.execute("SELECT COUNT(*) FROM projects").fetchone()[0]),
                    "project_bindings": int(connection.execute("SELECT COUNT(*) FROM project_thread_bindings").fetchone()[0]),
                    "memory_records": int(connection.execute("SELECT COUNT(*) FROM memory_canonical_records").fetchone()[0]),
                    "memory_files": int(connection.execute("SELECT COUNT(*) FROM memory_files").fetchone()[0]),
                    "connectors": int(connection.execute("SELECT COUNT(*) FROM connector_instances").fetchone()[0]),
                    "skill_states": int(connection.execute("SELECT COUNT(*) FROM skill_states").fetchone()[0]),
                    "legacy_runs": 0,
                    "legacy_run_events": 0,
                    "pending_work": 0,
                    "scheduler_tasks": 0,
                    "permission_preferences": 0,
                    "thread_branches": int(connection.execute("SELECT COUNT(*) FROM threads WHERE forked_from_thread_id IS NOT NULL").fetchone()[0]),
                    "source_evidence": 0,
                    "events": int(connection.execute("SELECT COUNT(*) FROM events").fetchone()[0]),
                }
            )
        self._baseline_artifact_ids = {
            projection.artifact_id for projection in artifact_service.list_user_artifacts()
        }
        self.counts["baseline_threads_preserved"] = self._baseline_counts["threads"]
        with database.reader() as connection:
            baseline_messages = int(
                connection.execute(
                    "SELECT COUNT(*) FROM items WHERE kind='message'"
                ).fetchone()[0]
            )
        self.counts["baseline_items_preserved"] = baseline_messages
        self.counts["baseline_artifacts_preserved"] = len(self._baseline_artifact_ids)
        self.counts["baseline_merge"] = 1
        self._trace("baseline.cloned", counts=dict(self._baseline_counts))

    def _warn(self, code: str, subject: object, detail: str) -> None:
        self.warnings.append(
            MigrationWarning(
                code=_safe_subject(code, limit=80),
                subject=_safe_subject(subject),
                detail=_safe_subject(detail, limit=320),
            )
        )

    def _legacy_warnings(self, values: Iterable[LegacyWarning]) -> None:
        for value in values:
            self._warn(value.code, value.subject, value.detail)

    @staticmethod
    def _resolve_override(source: Path, value: str | Path | None) -> Path | None:
        if value is None:
            return None
        raw = Path(value).expanduser()
        candidate = raw if raw.is_absolute() else source / raw
        try:
            resolved = secure_regular_file(
                candidate,
                label="legacy database override",
                root=source,
            )
        except SourceLayoutError:
            raise SourceLayoutError("legacy database override must be a regular file inside source_root")
        return resolved

    @staticmethod
    def _migration_id(inventory: SourceInventory) -> str:
        identity = hashlib.sha256(
            f"{inventory.source_version}\0{inventory.digest}".encode("utf-8")
        ).hexdigest()
        return f"mig_{identity[:26]}"

    def _existing_report(
        self, source: Path, target: Path, inventory: SourceInventory
    ) -> MigrationReport | None:
        if not target.exists():
            return None
        report_path = target / REPORT_NAME
        if not target.is_dir() or not report_path.is_file():
            raise TargetConflictError("v1 target already exists without a completed migration report")
        try:
            payload = json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise TargetConflictError("v1 target migration report is unreadable") from error
        if (
            payload.get("status") != "completed"
            or payload.get("source_inventory_digest") != inventory.digest
            or payload.get("migration_id") != self._migration_id(inventory)
            or payload.get("source_version") != inventory.source_version
            or payload.get("target_version") != TARGET_VERSION
        ):
            raise TargetConflictError("v1 target belongs to a different or incomplete migration")
        schema_identity = self._report_schema_identity(payload)
        quarantine_payload = (
            payload.get("quarantine")
            if isinstance(payload.get("quarantine"), Mapping)
            else {}
        )
        self._verify_published_target(
            target,
            inventory.digest,
            migration_id=self._migration_id(inventory),
            schema_identity=schema_identity,
            quarantine_entry_count=int(quarantine_payload.get("entry_count") or 0),
            quarantine_key=self.options.quarantine_key,
        )
        after = self._inventory_source(source)
        if after != inventory:
            raise SourceChangedError("legacy source changed while validating idempotent migration")
        return self._report_from_payload(payload, idempotent_replay=True)

    @staticmethod
    def _report_schema_identity(payload: Mapping[str, Any]) -> ImportSchemaIdentity:
        import_layout_version = payload.get("import_layout_version")
        storage_schema_version = payload.get("storage_schema_version")
        target_schema_sha256 = payload.get("target_schema_sha256")
        generation_id = payload.get("data_generation_id")
        migration_id = payload.get("migration_id")
        source_digest = payload.get("source_inventory_digest")
        if (
            isinstance(import_layout_version, bool)
            or not isinstance(import_layout_version, int)
            or isinstance(storage_schema_version, bool)
            or not isinstance(storage_schema_version, int)
            or not isinstance(target_schema_sha256, str)
            or not isinstance(generation_id, str)
            or not isinstance(migration_id, str)
            or not isinstance(source_digest, str)
        ):
            raise TargetConflictError("completed migration schema identity is invalid")
        try:
            identity = ImportSchemaIdentity(
                import_layout_version=import_layout_version,
                target_storage_schema_version=storage_schema_version,
                target_schema_sha256=target_schema_sha256,
                data_generation_id=generation_id,
            )
            expected_generation = data_generation_id(
                migration_id=migration_id,
                source_inventory_digest=source_digest,
                import_layout_version=identity.import_layout_version,
                target_storage_schema_version=identity.target_storage_schema_version,
                target_schema_sha256=identity.target_schema_sha256,
            )
        except ImportSchemaIdentityError as error:
            raise TargetConflictError(
                "completed migration schema identity is invalid"
            ) from error
        if identity.data_generation_id != expected_generation:
            raise TargetConflictError(
                "completed migration data generation identity is inconsistent"
            )
        return identity

    @staticmethod
    def _report_from_payload(payload: Mapping[str, Any], *, idempotent_replay: bool) -> MigrationReport:
        warnings = tuple(
            MigrationWarning(
                code=str(item.get("code") or "legacy_warning"),
                subject=str(item.get("subject") or ""),
                detail=str(item.get("detail") or ""),
            )
            for item in payload.get("warnings", [])
            if isinstance(item, Mapping)
        )
        backups = tuple(
            BackupRecord(
                source_relative_path=str(item.get("source_relative_path") or ""),
                backup_relative_path=str(item.get("backup_relative_path") or ""),
                source_sha256=str(item.get("source_sha256") or ""),
                backup_sha256=str(item.get("backup_sha256") or ""),
                kind=str(item.get("kind") or "sqlite_snapshot"),
            )
            for item in payload.get("backups", [])
            if isinstance(item, Mapping)
        )
        quarantine = payload.get("quarantine") if isinstance(payload.get("quarantine"), Mapping) else {}
        raw_summary = quarantine.get("summary")
        quarantine_summary = tuple(
            {
                "kind": str(item.get("kind") or "credential"),
                "origin": str(item.get("origin") or "product_configuration"),
                "count": int(item.get("count") or 0),
            }
            for item in raw_summary
            if isinstance(item, Mapping)
        ) if isinstance(raw_summary, list) else ()
        return MigrationReport(
            migration_id=str(payload["migration_id"]),
            status="completed",
            dry_run=False,
            idempotent_replay=idempotent_replay,
            source_version=str(payload["source_version"]),
            target_version=TARGET_VERSION,
            storage_schema_version=int(payload.get("storage_schema_version") or 0),
            import_layout_version=int(payload.get("import_layout_version") or 0),
            target_schema_sha256=str(payload.get("target_schema_sha256") or ""),
            data_generation_id=str(payload.get("data_generation_id") or ""),
            source_inventory_digest=str(payload["source_inventory_digest"]),
            counts={str(key): int(value) for key, value in dict(payload.get("counts") or {}).items()},
            warnings=warnings,
            backups=backups,
            sampled_artifact_ids=tuple(str(item) for item in payload.get("sampled_artifact_ids", [])),
            quarantine_entry_count=int(quarantine.get("entry_count") or 0),
            quarantine_summary=quarantine_summary,
            remaining_mappings=tuple(str(item) for item in payload.get("remaining_mappings", [])),
            source_evidence=(
                dict(payload.get("source_evidence") or {})
                if isinstance(payload.get("source_evidence"), Mapping)
                else {}
            ),
        )

    @staticmethod
    def _verify_published_target(
        target: Path,
        source_digest: str,
        *,
        migration_id: str,
        schema_identity: ImportSchemaIdentity,
        quarantine_entry_count: int,
        quarantine_key: bytes | None,
    ) -> None:
        database_path = target / TARGET_DATABASE_NAME
        if not database_path.is_file():
            raise TargetConflictError("completed migration target is missing its v1 database")
        try:
            connection = sqlite3.connect(str(database_path))
            rows = connection.execute(
                "SELECT source_inventory_digest, status FROM migration_runs"
            ).fetchall()
            integrity = connection.execute("PRAGMA integrity_check").fetchone()
            foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
            artifact_digests = [
                str(item[0])
                for item in connection.execute(
                    "SELECT DISTINCT source_sha256 FROM migration_artifact_links"
                )
            ]
            memory_digests = [
                str(item[0])
                for item in connection.execute(
                    "SELECT blob_sha256 FROM migration_memory_blob_links"
                )
            ]
            mismatched_links = connection.execute(
                """
                SELECT COUNT(*)
                FROM migration_artifact_links AS links
                JOIN artifact_revisions AS revisions
                  ON revisions.revision_id = links.revision_id
                WHERE links.source_sha256 <> revisions.sha256
                """
            ).fetchone()[0]
            source_evidence_count = connection.execute(
                "SELECT COUNT(*) FROM migration_source_evidence"
            ).fetchone()[0]
            executable_legacy_work = connection.execute(
                """
                SELECT
                    (SELECT COUNT(*) FROM legacy_scheduler_tasks
                     WHERE activation_status NOT IN ('requires_user_confirmation', 'unsupported_action'))
                  + (SELECT COUNT(*) FROM legacy_pending_work
                     WHERE recovery_status <> 'requires_user_confirmation')
                """
            ).fetchone()[0]
            migration_meta = {
                str(item[0]): str(item[1])
                for item in connection.execute(
                    "SELECT key,value FROM migration_meta WHERE key IN ("
                    "'import_layout_version','migration_id',"
                    "'source_inventory_digest','data_generation_id',"
                    "'import_target_storage_schema_version',"
                    "'import_target_schema_sha256') ORDER BY key"
                ).fetchall()
            }
            turn_input_contract_errors = _legacy_turn_input_contract_errors(
                connection,
                source_inventory_digest=source_digest,
            )
        except sqlite3.Error as error:
            raise TargetConflictError("completed migration target database is invalid") from error
        finally:
            if "connection" in locals():
                connection.close()
        expected_meta = {
            "import_layout_version": str(schema_identity.import_layout_version),
            "migration_id": migration_id,
            "source_inventory_digest": source_digest,
            "data_generation_id": schema_identity.data_generation_id,
            "import_target_storage_schema_version": str(
                schema_identity.target_storage_schema_version
            ),
            "import_target_schema_sha256": schema_identity.target_schema_sha256,
        }
        if (
            rows != [(source_digest, "completed")]
            or integrity != ("ok",)
            or foreign_keys
            or mismatched_links
            or source_evidence_count != 1
            or executable_legacy_work
            or migration_meta != expected_meta
            or turn_input_contract_errors
        ):
            raise TargetConflictError("completed migration target failed integrity verification")
        blob_root = target / TARGET_ARTIFACT_ROOT_NAME / "blobs"
        if (artifact_digests or memory_digests) and not blob_root.is_dir():
            raise TargetConflictError("completed migration target is missing its CAS")
        blobs = ContentAddressedStore(blob_root)
        try:
            for digest in dict.fromkeys((*artifact_digests, *memory_digests)):
                blobs.read_bytes(digest)
        except (OSError, ValueError, ContentIntegrityError) as error:
            raise TargetConflictError("completed migration target contains a missing or corrupt CAS blob") from error

        if quarantine_entry_count:
            quarantine_path = target / QUARANTINE_NAME
            if not quarantine_path.is_file():
                raise TargetConflictError("completed migration target is missing credential quarantine")
            try:
                envelope = json.loads(quarantine_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as error:
                raise TargetConflictError("completed migration credential quarantine is unreadable") from error
            if envelope.get("associated_digest") != source_digest:
                raise TargetConflictError("credential quarantine belongs to a different source inventory")
            if quarantine_key is not None:
                try:
                    recovered = decrypt_quarantine(quarantine_path, key=quarantine_key)
                except Exception as error:
                    raise TargetConflictError("credential quarantine authentication failed") from error
                if (
                    recovered.get("source_inventory_digest") != source_digest
                    or len(recovered.get("entries") or []) != quarantine_entry_count
                ):
                    raise TargetConflictError("credential quarantine ledger is inconsistent")

    def _make_staging(self, target: Path) -> tuple[Path, tempfile.TemporaryDirectory[str] | None]:
        if self.options.dry_run:
            temporary = tempfile.TemporaryDirectory(prefix="ecorex-legacy-dry-run-")
            staging = Path(temporary.name).resolve(strict=True) / "target"
            staging.mkdir()
            return staging, temporary
        target.parent.mkdir(parents=True, exist_ok=True)
        # Keep the generated component short enough for Windows installations
        # whose user-data root is already near MAX_PATH.  mkdtemp still gives
        # atomic collision handling; the marker below is the deletion authority.
        staging = Path(tempfile.mkdtemp(prefix=".ecx-", dir=target.parent))
        (staging / ".ecorex-migration-staging").write_text(
            "released-to-v1.0\n", encoding="ascii"
        )
        return staging, None

    @staticmethod
    def _discard_staging(staging: Path | None, target: Path) -> None:
        if staging is None or not staging.exists() or staging == target:
            return
        if staging.is_symlink():
            raise SourceLayoutError("migration staging path unexpectedly became a symlink")
        resolved = staging.resolve(strict=True)
        expected_real_parent = target.parent.resolve(strict=True)
        marker = resolved / ".ecorex-migration-staging"
        try:
            marker_ok = (
                marker.is_file()
                and not marker.is_symlink()
                and marker.read_text(encoding="ascii") == "released-to-v1.0\n"
            )
        except (OSError, UnicodeDecodeError):
            marker_ok = False
        real_name_ok = (
            resolved.parent == expected_real_parent
            and resolved.name.startswith(".ecx-")
            and marker_ok
        )
        dry_name_ok = resolved.parent.name.startswith("ecorex-legacy-dry-run-")
        if not (real_name_ok or dry_name_ok):
            raise SourceLayoutError("refusing to remove an unexpected migration staging path")
        shutil.rmtree(resolved)

    def _snapshot_databases(
        self,
        source: Path,
        staging: Path,
        inventory: SourceInventory,
    ) -> tuple[LegacyConversations, LegacyMemory, LegacyRuntimeLedger]:
        index = inventory_index(inventory)
        conversation_source = self._resolve_override(source, self.options.conversation_database)
        if conversation_source is None:
            conversation_source = discover_existing(source, CONVERSATION_CANDIDATES)
        memory_source = self._resolve_override(source, self.options.memory_database)
        if memory_source is None:
            memory_source = discover_existing(source, MEMORY_INDEX_CANDIDATES)

        conversations = LegacyConversations((), ())
        memories = LegacyMemory((), ())
        snapshots: dict[Path, Path] = {}
        snapshot_labels: dict[str, Path] = {}
        for kind, database_source in (
            ("conversations", conversation_source),
            ("memory-index", memory_source),
        ):
            if database_source is None:
                self._warn(f"{kind}_missing", kind, "optional legacy database was not found")
                continue
            relative = database_source.relative_to(source).as_posix()
            inventory_entry = index.get(relative)
            if inventory_entry is None or inventory_entry.kind != "file":
                raise SourceLayoutError(f"legacy {kind} database is not in the source inventory")
            backup = snapshots.get(database_source)
            if backup is None:
                backup = staging / "backups" / f"{kind}.sqlite3"
                snapshot_sqlite(database_source, backup, subject=f"legacy {kind} database")
                snapshots[database_source] = backup
                self.backups.append(
                    BackupRecord(
                        source_relative_path=relative,
                        backup_relative_path=backup.relative_to(staging).as_posix(),
                        source_sha256=inventory_entry.sha256,
                        backup_sha256=sha256_file(backup),
                    )
                )
            snapshot_labels[relative] = backup
            if kind == "conversations":
                conversations = read_conversations(backup)
                self._legacy_warnings(conversations.warnings)
            else:
                memories = read_memory(backup)
                self._legacy_warnings(memories.warnings)
        runtime_ledgers = [read_runtime_ledger(path) for path in dict.fromkeys(snapshots.values())]
        runtime = self._merge_runtime_ledgers(runtime_ledgers)
        self._legacy_warnings(runtime.warnings)
        schema_digest, schema_tables = sqlite_schema_fingerprint(snapshot_labels)
        marker = self._pinned_paths.get("release-evidence")
        marker_label = "@pinned/release-evidence" if marker is not None else None
        self.source_evidence = read_release_evidence(
            source,
            expected_source_version=self.options.source_version,
            marker_override=marker,
            marker_label=marker_label,
            schema_fingerprint=schema_digest,
            schema_tables=schema_tables,
        )
        if self.source_evidence.evidence_level == "release_schema_compatible_unattested":
            self._warn(
                "release_evidence_unattested",
                self.options.source_version,
                "released data schema matches, but the installed package commit cannot be proven from this workspace",
            )
        return conversations, memories, runtime

    @staticmethod
    def _merge_runtime_ledgers(
        ledgers: Iterable[LegacyRuntimeLedger],
    ) -> LegacyRuntimeLedger:
        runs: dict[str, dict[str, Any]] = {}
        events: dict[tuple[str, int], dict[str, Any]] = {}
        warnings: list[LegacyWarning] = []
        for ledger in ledgers:
            warnings.extend(ledger.warnings)
            for row in ledger.runs:
                request_id = str(row["request_id"])
                existing = runs.get(request_id)
                if existing is not None and json_dumps(existing) != json_dumps(row):
                    raise DuplicateLegacyIdError(
                        f"legacy run {request_id!r} differs across database snapshots"
                    )
                runs[request_id] = dict(row)
            for row in ledger.events:
                identity = (str(row["request_id"]), int(row["event_seq"]))
                existing = events.get(identity)
                if existing is not None and json_dumps(existing) != json_dumps(row):
                    raise DuplicateLegacyIdError(
                        f"legacy run event {identity[0]}:{identity[1]} differs across snapshots"
                    )
                events[identity] = dict(row)
        for (request_id, event_seq), event in events.items():
            run = runs.get(request_id)
            event_session = str(event.get("session_id") or "")
            if (
                run is not None
                and event_session
                and event_session != str(run.get("session_id") or "")
            ):
                raise DuplicateLegacyIdError(
                    f"legacy run event {request_id}:{event_seq} differs from its run owner across snapshots"
                )
        return LegacyRuntimeLedger(
            runs=tuple(
                sorted(runs.values(), key=lambda row: (float(row.get("created_at") or 0), str(row["request_id"])))
            ),
            events=tuple(
                sorted(
                    events.values(),
                    key=lambda row: (float(row.get("created_at") or 0), int(row["event_id"])),
                )
            ),
            warnings=tuple(warnings),
        )

    def _read_optional_json(
        self,
        source: Path,
        relative: str,
        *,
        pin_label: str,
        security_critical: bool,
    ) -> tuple[dict[str, Any], str]:
        path = self._pinned_paths.get(pin_label, source / relative)
        source_label = f"@pinned/{pin_label}" if pin_label in self._pinned_paths else relative
        if not os.path.lexists(path):
            return {}, source_label
        try:
            path = secure_regular_file(
                path,
                label=f"legacy {source_label}",
                root=None if pin_label in self._pinned_paths else source,
            )
            return read_json_object(path), source_label
        except (OSError, ValueError, json.JSONDecodeError, SourceLayoutError) as error:
            if security_critical:
                raise MigrationError(f"legacy {source_label} cannot be safely parsed") from error
            self._warn("optional_json_unreadable", source_label, "optional metadata was skipped")
            return {}, source_label

    def _load_legacy_json(
        self, source: Path
    ) -> tuple[
        dict[str, Any],
        dict[str, Any],
        dict[str, Any],
        dict[str, Any],
        dict[str, Any],
    ]:
        config, config_label = self._read_optional_json(
            source, "config.json", pin_label="config", security_critical=True
        )
        mcp, mcp_label = self._read_optional_json(
            source, "mcp.json", pin_label="mcp", security_critical=True
        )
        ui_state, _ui_label = self._read_optional_json(
            source,
            ".ecorex/ui-state.json",
            pin_label="ui-state",
            security_critical=False,
        )
        skills, _skills_label = self._read_optional_json(
            source,
            "skills/skills_config.json",
            pin_label="skills-config",
            security_critical=False,
        )
        permission_relative = "permissions/permissions.json"
        if "permissions" not in self._pinned_paths:
            embedded_permission = source / ".ecorex" / "permissions.json"
            if os.path.lexists(embedded_permission):
                try:
                    secure_regular_file(
                        embedded_permission,
                        label="legacy embedded permission file",
                        root=source,
                    )
                except SourceLayoutError as error:
                    raise MigrationError(
                        "legacy embedded permission file is unsafe"
                    ) from error
                permission_relative = ".ecorex/permissions.json"
        permissions, _permission_label = self._read_optional_json(
            source,
            permission_relative,
            pin_label="permissions",
            security_critical=True,
        )
        for relative, payload in ((config_label, config), (mcp_label, mcp)):
            self.secret_records.extend(
                collect_secrets(payload, source_relative_path=relative)
            )
        self.counts["quarantined_secrets"] = len(self.secret_records)
        return config, mcp, ui_state, skills, permissions

    def _merge_ui_state_history(
        self,
        conversations: LegacyConversations,
        ui_state: Mapping[str, Any],
    ) -> LegacyConversations:
        """Enrich canonical sessions without reviving deleted WebUI cache rows."""

        sessions = {str(row["session_id"]): dict(row) for row in conversations.sessions}
        messages = [dict(row) for row in conversations.messages]
        message_counts: dict[str, int] = defaultdict(int)
        for row in messages:
            message_counts[str(row["session_id"])] += 1
        warnings: list[LegacyWarning] = []
        session_titles = ui_state.get("sessionTitles")
        session_titles = session_titles if isinstance(session_titles, Mapping) else {}
        pinned_sessions = ui_state.get("pinnedSessions")
        pinned_sessions = pinned_sessions if isinstance(pinned_sessions, Mapping) else {}
        pinned_times = ui_state.get("pinnedSessionTimes")
        pinned_times = pinned_times if isinstance(pinned_times, Mapping) else {}
        enriched_titles = 0
        pinned_count = 0
        for session_id, session in sessions.items():
            cached_title = str(session_titles.get(session_id) or "").strip()
            if cached_title and not bool(session.get("title_locked")):
                if str(session.get("title") or "") != cached_title:
                    enriched_titles += 1
                session["title"] = cached_title
            session["pinned"] = bool(pinned_sessions.get(session_id))
            session["pinned_at"] = pinned_times.get(session_id)
            if session["pinned"]:
                pinned_count += 1
        self.counts["session_summaries"] = sum(
            bool(str(session.get("title") or "").strip()) for session in sessions.values()
        )
        self.counts["session_titles_enriched"] = enriched_titles
        self.counts["pinned_threads"] = pinned_count

        deleted_cache_ids = (
            set(str(key) for key in session_titles)
            | set(str(key) for key in (ui_state.get("sessionUiState") or {}))
        ) - set(sessions)
        if deleted_cache_ids:
            self.counts["deleted_session_cache_excluded"] = len(deleted_cache_ids)
            warnings.append(
                LegacyWarning(
                    "deleted_session_cache_excluded",
                    "WebUI session cache",
                    "cached session ids absent from the canonical database were not restored",
                )
            )

        raw_session_state = ui_state.get("sessionUiState")
        if not isinstance(raw_session_state, Mapping):
            return LegacyConversations(
                sessions=tuple(
                    sorted(
                        sessions.values(),
                        key=lambda row: (
                            _legacy_timestamp(row.get("created_at")),
                            str(row["session_id"]),
                        ),
                    )
                ),
                messages=tuple(messages),
                warnings=tuple(warnings),
            )

        entries = list(raw_session_state.items())
        if len(entries) > 200:
            warnings.append(
                LegacyWarning(
                    "ui_history_session_limit",
                    "sessionUiState",
                    "only the first 200 cached sessions supported by the released runtime were considered",
                )
            )
        imported_messages = 0
        for raw_session_id, raw_cached in entries[:200]:
            session_id = str(raw_session_id or "").strip()
            if (
                not session_id
                or "\x00" in session_id
                or len(session_id) > 1024
                or not isinstance(raw_cached, Mapping)
            ):
                warnings.append(
                    LegacyWarning(
                        "ui_history_session_rejected",
                        "sessionUiState",
                        "malformed cached session was skipped",
                    )
                )
                continue
            if session_id not in sessions:
                # The database is the deletion authority. UI cache is allowed
                # to enrich a surviving row, never to resurrect an absent one.
                continue
            # The released v0.3 hydrator never overwrote canonical DB history.
            if message_counts.get(session_id, 0) > 0:
                continue
            raw_messages = raw_cached.get("messages")
            if not isinstance(raw_messages, list) or not raw_messages:
                continue
            converted: list[dict[str, Any]] = []
            if len(raw_messages) > 200:
                warnings.append(
                    LegacyWarning(
                        "ui_history_message_limit",
                        session_id,
                        "only the first 200 cached messages supported by the released runtime were considered",
                    )
                )
            for raw_message in raw_messages[:200]:
                if not isinstance(raw_message, Mapping):
                    continue
                role = str(raw_message.get("role") or "").strip().casefold()
                if role not in {"user", "assistant"}:
                    continue
                text = str(raw_message.get("content") or "").strip()
                raw_attachments = raw_message.get("attachments")
                attachments = (
                    [dict(item) for item in raw_attachments[:50] if isinstance(item, Mapping)]
                    if isinstance(raw_attachments, list)
                    else []
                )
                if role == "assistant" and raw_message.get("pending") and not text:
                    continue
                if not text and not attachments:
                    continue
                extras: dict[str, Any] = {}
                if attachments:
                    extras["attachments"] = _without_secrets(attachments)
                request_id = str(raw_message.get("requestId") or "").strip()
                if request_id:
                    extras["request_id"] = request_id
                    extras["turn_id"] = request_id
                for source_key, target_key in (("userSeq", "user_seq"), ("botSeq", "bot_seq")):
                    value = raw_message.get(source_key)
                    if isinstance(value, int) and not isinstance(value, bool):
                        extras[target_key] = value
                converted.append(
                    {
                        "session_id": session_id,
                        "seq": len(converted),
                        "role": role,
                        "content": json_dumps([{"type": "text", "text": text}]) if text else "",
                        "created_at": _legacy_timestamp(
                            raw_message.get("createdAt")
                            or raw_cached.get("lastActivityAt")
                            or ui_state.get("savedAt")
                            or ui_state.get("updatedAt")
                        ),
                        "extras": json_dumps(extras) if extras else "",
                    }
                )
            if not converted:
                continue
            messages.extend(converted)
            message_counts[session_id] = len(converted)
            imported_messages += len(converted)
        if imported_messages:
            self.counts["ui_history_messages"] += imported_messages
            warnings.append(
                LegacyWarning(
                    "ui_history_hydrated",
                    "sessionUiState",
                    "cached WebUI history for surviving empty database sessions was imported",
                )
            )
        return LegacyConversations(
            sessions=tuple(
                sorted(
                    sessions.values(),
                    key=lambda row: (_legacy_timestamp(row.get("created_at")), str(row["session_id"])),
                )
            ),
            messages=tuple(
                sorted(
                    messages,
                    key=lambda row: (str(row["session_id"]), int(row["seq"])),
                )
            ),
            warnings=tuple(warnings),
        )

    def _configure_pinned_sources(self, source: Path, target: Path) -> None:
        requested = {
            "config": self.options.config_file,
            "mcp": self.options.mcp_file,
            "ui-state": self.options.ui_state_file,
            "skills-config": self.options.skills_config_file,
            "permissions": self.options.permission_file,
            "release-evidence": self.options.release_evidence_file,
        }
        pinned: dict[str, Path] = {}
        for label, raw_value in requested.items():
            if raw_value is None:
                continue
            raw = Path(raw_value).expanduser()
            candidate = raw if raw.is_absolute() else source / raw
            try:
                resolved = secure_regular_file(
                    candidate,
                    label=f"pinned {label} source",
                )
            except SourceLayoutError:
                raise SourceLayoutError(f"pinned {label} source must be a regular file")
            if is_within(resolved, target):
                raise SourceLayoutError("pinned legacy metadata must not be inside the v1 target")
            pinned[label] = resolved
        self._pinned_paths = pinned

    def _inventory_source(self, source: Path) -> SourceInventory:
        return inventory_source(
            source,
            pinned_files=self._pinned_paths,
            source_version=self.options.source_version,
        )

    @staticmethod
    def _has_symlink_component(source: Path, candidate: Path) -> bool:
        try:
            secure_regular_file(
                candidate,
                label="legacy source file",
                root=source,
            )
        except SourceLayoutError:
            return True
        return False

    def _resolve_source_file(
        self,
        source: Path,
        raw_value: object,
        inventory: SourceInventory,
        *,
        memory_fallback: bool = False,
    ) -> tuple[Path, str] | None:
        raw = str(raw_value or "").strip()
        if not raw or "\x00" in raw:
            return None
        if raw.startswith("/api/file"):
            raw = (parse_qs(urlparse(raw).query).get("path") or [""])[0]
        if raw.startswith("file://"):
            parsed = urlparse(raw)
            if parsed.netloc not in ("", "localhost"):
                return None
            raw = parsed.path
            if os.name == "nt" and raw.startswith("/") and len(raw) > 2 and raw[2] == ":":
                raw = raw[1:]
        raw_path = Path(raw)
        if not raw_path.is_absolute() and ".." in raw_path.parts:
            return None

        candidates = [raw_path] if raw_path.is_absolute() else [source / raw_path]
        if memory_fallback and not raw_path.is_absolute():
            candidates.insert(0, source / "memory" / raw_path)
        index = inventory_index(inventory)
        for candidate in candidates:
            lexical = lexical_absolute(candidate)
            if not is_within(lexical, source) or self._has_symlink_component(source, lexical):
                continue
            try:
                resolved = secure_regular_file(
                    lexical,
                    label="legacy referenced file",
                    root=source,
                )
            except (OSError, RuntimeError, SourceLayoutError):
                continue
            if (
                not is_within(resolved, source)
            ):
                continue
            relative = resolved.relative_to(source).as_posix()
            entry = index.get(relative)
            if entry is None or entry.kind != "file":
                continue
            return resolved, relative
        return None

    @staticmethod
    def _artifact_entries(message: Mapping[str, Any]) -> tuple[dict[str, Any], ...]:
        extras = _parse_legacy_json(message.get("extras"), fallback_to_text=False)
        if not isinstance(extras, Mapping):
            return ()
        artifacts = extras.get("artifacts")
        if not isinstance(artifacts, list):
            return ()
        return tuple(dict(item) for item in artifacts if isinstance(item, Mapping))

    @staticmethod
    def _scrub_cloud_url(value: str) -> str | None:
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            return None
        host = parsed.hostname.casefold()
        if parsed.port:
            host = f"{host}:{parsed.port}"
        return urlunparse((parsed.scheme, host, parsed.path or "/", "", "", ""))

    def _import_artifacts(
        self,
        *,
        source: Path,
        inventory: SourceInventory,
        messages: Iterable[Mapping[str, Any]],
        service: ArtifactService,
    ) -> dict[tuple[str, int], tuple[_ArtifactImport, ...]]:
        by_message: dict[tuple[str, int], list[_ArtifactImport]] = defaultdict(list)
        cache: dict[tuple[str, str], _ArtifactImport] = {}
        for message in messages:
            session_id = str(message["session_id"])
            sequence = int(message["seq"])
            message_key = (session_id, sequence)
            seen_in_message: set[str] = set()
            for raw in self._artifact_entries(message):
                legacy_role = str(raw.get("role") or "").casefold()
                if legacy_role in {"rendition", "source", "intermediate", "diagnostic"}:
                    self.counts["artifacts_excluded_internal"] += 1
                    self._warn(
                        "artifact_excluded_internal",
                        f"{session_id}:{sequence}",
                        f"legacy role {legacy_role} is not a user deliverable",
                    )
                    continue
                status = str(raw.get("status") or "ready").casefold()
                if status in {"failed", "error", "pending", "queued", "running", "retrying"}:
                    self.counts["artifacts_skipped_unready"] += 1
                    continue
                intent = str(raw.get("intent") or "").casefold()
                operation = str(raw.get("operation") or "").casefold()
                path_value = next(
                    (
                        raw.get(key)
                        for key in ("path", "relativePath", "file_path", "filePath")
                        if raw.get(key)
                    ),
                    "",
                )
                raw_url = str(raw.get("url") or path_value or "").strip()
                kind = str(raw.get("kind") or "").casefold()
                if raw_url.startswith(("https://", "http://")):
                    if intent != "deliverable" or kind not in {"link", "cloud_link"}:
                        self.counts["artifacts_skipped_remote"] += 1
                        continue
                    scrubbed = self._scrub_cloud_url(raw_url)
                    if scrubbed is None:
                        self.counts["artifacts_skipped_unsafe"] += 1
                        continue
                    cache_key = ("cloud", scrubbed)
                    imported = cache.get(cache_key)
                    if imported is None:
                        projection = service.create_cloud_link(
                            scrubbed,
                            requested_name=str(raw.get("title") or "云端办公产物"),
                        )
                        imported = _ArtifactImport(
                            projection=projection,
                            relative_path="cloud-link",
                            source_sha256=projection.sha256,
                        )
                        cache[cache_key] = imported
                        self.counts["artifacts"] += 1
                    if imported.projection.artifact_id not in seen_in_message:
                        by_message[message_key].append(imported)
                        seen_in_message.add(imported.projection.artifact_id)
                    continue

                resolved = self._resolve_source_file(source, path_value, inventory)
                if resolved is None:
                    self.counts["artifacts_skipped_unsafe"] += 1
                    self._warn(
                        "artifact_path_rejected",
                        f"{session_id}:{sequence}",
                        "artifact path was missing, external, traversing, or linked",
                    )
                    continue
                source_path, relative = resolved
                actual_name = source_path.name
                guessed_mime = mimetypes.guess_type(actual_name)[0]
                # Legacy MIME metadata is presentation data and is not trusted
                # to upgrade an unknown extension into a visible deliverable.
                mime_type = guessed_mime or "application/octet-stream"
                declaration = None
                explicitly_final = intent == "deliverable" and operation not in {"modified", "patched"}
                if explicitly_final:
                    declaration = service.issue_trusted_deliverable_declaration("migration.v0.3")
                decision = service.classify(
                    actual_name,
                    mime_type,
                    role=ArtifactRole.DELIVERABLE,
                    requested_visibility=ArtifactVisibility.PRIMARY,
                    declaration=declaration,
                )
                if not decision.is_user_visible:
                    self.counts["artifacts_excluded_internal"] += 1
                    self._warn(
                        "artifact_excluded_internal",
                        f"{session_id}:{sequence}",
                        f"backend classification forced {decision.family.value} to internal",
                    )
                    continue
                entry = inventory_index(inventory)[relative]
                try:
                    content = stable_read_bytes(
                        source_path,
                        label="legacy Artifact",
                        maximum=self.options.max_artifact_bytes,
                        root=source,
                    )
                except SourceLayoutError:
                    self.counts["artifacts_skipped_oversized"] += 1
                    self._warn(
                        "artifact_oversized",
                        f"{session_id}:{sequence}",
                        "artifact exceeded the configured migration byte limit or became unsafe",
                    )
                    continue
                actual_digest = hashlib.sha256(content).hexdigest()
                if actual_digest != entry.sha256:
                    raise SourceChangedError("legacy artifact changed after source inventory")
                cache_key = (relative, actual_digest)
                imported = cache.get(cache_key)
                if imported is None:
                    projection = service.create_artifact(
                        content,
                        requested_name=actual_name,
                        mime_type=mime_type,
                        declaration=declaration,
                    )
                    imported = _ArtifactImport(
                        projection=projection,
                        relative_path=relative,
                        source_sha256=actual_digest,
                    )
                    cache[cache_key] = imported
                    self.counts["artifacts"] += 1
                if imported.projection.artifact_id not in seen_in_message:
                    by_message[message_key].append(imported)
                    seen_in_message.add(imported.projection.artifact_id)
        return {key: tuple(value) for key, value in by_message.items()}

    @staticmethod
    def _message_groups(messages: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
        groups: list[list[dict[str, Any]]] = []
        current: list[dict[str, Any]] = []
        for message in messages:
            role = str(message.get("role") or "").casefold()
            if role == "user" and current:
                groups.append(current)
                current = []
            current.append(message)
        if current:
            groups.append(current)
        return groups

    @staticmethod
    def _group_request_id(group: Iterable[Mapping[str, Any]]) -> str | None:
        values: list[str] = []
        for message in group:
            extras = _parse_legacy_json(message.get("extras"), fallback_to_text=False)
            if not isinstance(extras, Mapping):
                continue
            value = str(extras.get("request_id") or extras.get("turn_id") or "").strip()
            if value and value not in values:
                values.append(value)
        return values[0] if len(values) == 1 else None

    @staticmethod
    def _imported_run_status(source_status: str) -> tuple[str, str, str]:
        status = source_status.casefold()
        if status == "completed":
            return "completed", "legacy_import", "turn.status_changed"
        if status in {"failed", "timeout"}:
            return "failed", "legacy_timeout" if status == "timeout" else "legacy_failed", "turn.status_changed"
        if status == "cancelled":
            return "cancelled", "legacy_cancelled", "turn.status_changed"
        return "interrupted", "legacy_migration_requires_user_confirmation", "turn.status_changed"

    @staticmethod
    def _import_initial_turn_input_revision(
        connection: sqlite3.Connection,
        *,
        source_inventory_digest: str,
        legacy_session_id: str,
        legacy_start_seq: int,
        thread_id: str,
        turn_id: str,
        request: CreateTurnRequest,
        created_at: datetime,
    ) -> bool:
        """Persist the Runtime's ordinal-zero intent fact for a legacy Turn.

        Conversation import writes the parent Turn directly because historical
        Turns are terminal and must never enqueue work.  This helper mirrors
        the Runtime intent contract without creating a Job or binding a current
        snapshot.  Its deterministic identity and exact-match replay check make
        the backfill idempotent inside the copy-on-write transaction.
        """

        if not connection.in_transaction:
            raise RuntimeError("legacy Turn input import requires an active transaction")
        revision_id = _stable_id(
            "rev",
            source_inventory_digest,
            legacy_session_id,
            legacy_start_seq,
            "initial",
        )
        timestamp = created_at.isoformat(timespec="microseconds")
        fingerprint = intent_fingerprint(request)
        expected = (
            revision_id,
            thread_id,
            turn_id,
            0,
            "initial",
            request.input,
            request.agent_model_id,
            request.image_model_id,
            json_dumps(list(request.explicit_tool_ids)),
            json_dumps(request.metadata),
            request.client_message_id,
            fingerprint,
            timestamp,
        )
        existing = connection.execute(
            "SELECT revision_id, thread_id, turn_id, ordinal, source, input_text, "
            "agent_model_id, image_model_id, explicit_tool_ids_json, metadata_json, "
            "client_message_id, intent_fingerprint, created_at "
            "FROM turn_input_revisions WHERE turn_id = ? ORDER BY ordinal",
            (turn_id,),
        ).fetchall()
        if existing:
            observed = tuple(existing[0])
            if len(existing) != 1 or observed != expected:
                raise MigrationVerificationError(
                    "legacy Turn initial input revision conflicts with its deterministic identity"
                )
            return False
        try:
            connection.execute(
                "INSERT INTO turn_input_revisions("
                "revision_id, thread_id, turn_id, ordinal, source, input_text, "
                "agent_model_id, image_model_id, explicit_tool_ids_json, metadata_json, "
                "client_message_id, intent_fingerprint, created_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                expected,
            )
        except sqlite3.IntegrityError as error:
            raise MigrationVerificationError(
                "legacy Turn initial input revision identity is inconsistent"
            ) from error
        return True

    def _import_conversations(
        self,
        *,
        inventory: SourceInventory,
        conversations: LegacyConversations,
        runtime: LegacyRuntimeLedger,
        artifacts: Mapping[tuple[str, int], tuple[_ArtifactImport, ...]],
        database: SQLiteDatabase,
    ) -> dict[str, str]:
        events = EventStore(database)
        messages_by_session: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for message in conversations.messages:
            messages_by_session[str(message["session_id"])].append(message)
        groups_by_session = {
            session_id: self._message_groups(messages)
            for session_id, messages in messages_by_session.items()
        }
        request_occurrences: dict[str, int] = defaultdict(int)
        for groups in groups_by_session.values():
            for group in groups:
                request_id = self._group_request_id(group)
                if request_id:
                    request_occurrences[request_id] += 1
        ambiguous_request_ids = {
            request_id
            for request_id, count in request_occurrences.items()
            if count > 1
        }
        if ambiguous_request_ids:
            self.counts["ambiguous_legacy_request_ids"] = len(ambiguous_request_ids)
            self.counts["ambiguous_legacy_request_occurrences"] = sum(
                request_occurrences[request_id]
                for request_id in ambiguous_request_ids
            )
            self._warn(
                "ambiguous_legacy_request_identity",
                "conversation history",
                "reused legacy request ids were preserved on every Turn but left unbound from the run ledger",
            )
        thread_ids: dict[str, str] = {}
        runs_by_id = {str(row["request_id"]): row for row in runtime.runs}

        with database.transaction() as connection:
            for session in conversations.sessions:
                legacy_session_id = str(session["session_id"])
                thread_id = _stable_id("thr", inventory.digest, legacy_session_id)
                thread_ids[legacy_session_id] = thread_id
                created = _legacy_datetime(session.get("created_at"))
                updated = max(created, _legacy_datetime(session.get("last_active")))
                legacy_metadata = _parse_legacy_json(
                    session.get("metadata_json"), fallback_to_text=False
                )
                metadata = {
                    "pinned": bool(session.get("pinned")),
                    "migration": {
                        "source_version": inventory.source_version,
                        "legacy_session_id": legacy_session_id,
                        "channel_type": str(session.get("channel_type") or ""),
                        "context_start_seq": int(session.get("context_start_seq") or 0),
                        "source_inventory_digest": inventory.digest,
                        "legacy_pinned_at": session.get("pinned_at"),
                    },
                    "legacy_metadata": _without_secrets(legacy_metadata),
                }
                client_request_id = _stable_id("migreq", inventory.digest, legacy_session_id)
                fingerprint = hashlib.sha256(
                    json_dumps(
                        {"title": str(session.get("title") or ""), "metadata": metadata}
                    ).encode("utf-8")
                ).hexdigest()
                events.append_in_transaction(
                    connection,
                    thread_id=thread_id,
                    event_type="thread.created",
                    payload={"title": str(session.get("title") or ""), "metadata": metadata},
                    correlation_id=client_request_id,
                    idempotency_key="thread:created",
                    created_at=created,
                )
                connection.execute(
                    """
                    INSERT INTO threads(
                        thread_id, status, title, metadata_json,
                        client_request_id, request_fingerprint,
                        forked_from_thread_id, forked_from_turn_id, forked_from_seq,
                        created_at, updated_at
                    ) VALUES (?, 'active', ?, ?, ?, ?, NULL, NULL, NULL, ?, ?)
                    """,
                    (
                        thread_id,
                        str(session.get("title") or "") or None,
                        json_dumps(metadata),
                        client_request_id,
                        fingerprint,
                        created.isoformat(timespec="microseconds"),
                        updated.isoformat(timespec="microseconds"),
                    ),
                )
                connection.execute(
                    "INSERT INTO legacy_id_map(entity_kind, legacy_id, target_id) VALUES ('session', ?, ?)",
                    (legacy_session_id, thread_id),
                )
                self.counts["threads"] += 1

                groups = groups_by_session.get(legacy_session_id, [])
                for group_index, group in enumerate(groups):
                    start_seq = int(group[0]["seq"])
                    turn_id = _stable_id("trn", inventory.digest, legacy_session_id, start_seq)
                    user_message = next(
                        (item for item in group if str(item.get("role") or "").casefold() == "user"),
                        None,
                    )
                    input_content = _parse_legacy_json(
                        user_message.get("content") if user_message else ""
                    )
                    input_text = _display_text(input_content)
                    client_message_id = (
                        _stable_id("msg", inventory.digest, legacy_session_id, int(user_message["seq"]))
                        if user_message is not None
                        else None
                    )
                    turn_created = _legacy_datetime(group[0].get("created_at"))
                    turn_updated = max(_legacy_datetime(item.get("created_at")) for item in group)
                    legacy_request_id = self._group_request_id(group)
                    request_identity_ambiguous = bool(
                        legacy_request_id
                        and legacy_request_id in ambiguous_request_ids
                    )
                    legacy_run = (
                        None
                        if request_identity_ambiguous
                        else runs_by_id.get(legacy_request_id or "")
                    )
                    imported_status = "completed"
                    terminal_reason = "legacy_import"
                    terminal_event_type = "turn.status_changed"
                    legacy_model: str | None = None
                    if legacy_run is not None:
                        imported_status, terminal_reason, terminal_event_type = self._imported_run_status(
                            str(legacy_run.get("status") or "")
                        )
                        legacy_model = (
                            str(legacy_run.get("model") or "").strip() or None
                        )
                        turn_updated = max(
                            turn_updated, _legacy_datetime(legacy_run.get("updated_at"))
                        )
                    turn_metadata = {
                        "migration": {
                            "source_version": inventory.source_version,
                            "legacy_start_seq": start_seq,
                            "legacy_group_index": group_index,
                            "legacy_request_id": legacy_request_id,
                            "legacy_request_id_ambiguous": request_identity_ambiguous,
                            "legacy_request_occurrence_count": (
                                request_occurrences.get(legacy_request_id, 0)
                                if legacy_request_id
                                else 0
                            ),
                            "legacy_run_status": (
                                str(legacy_run.get("status")) if legacy_run is not None else None
                            ),
                            "legacy_run_phase": (
                                str(legacy_run.get("phase") or "") if legacy_run is not None else None
                            ),
                            "legacy_model": legacy_model,
                            "input_recovery": (
                                "source_user_message"
                                if input_text
                                else "missing_or_empty_user_message"
                            ),
                        }
                    }
                    if not input_text:
                        # v1 Turn intent is non-empty.  Preserve the absence as
                        # explicit migration metadata instead of fabricating a
                        # user instruction from an assistant-only history row.
                        input_text = LEGACY_MISSING_INPUT_TEXT
                    # v0.3 used one ambiguous model field.  The read adapter
                    # quarantines that value as migration metadata and maps
                    # only known image aliases into the new, separate slot.
                    # Historical Turns never activate an imported provider ID.
                    agent_model_id = "ecorex-chat"
                    image_model_id = (
                        "gpt-image-2"
                        if str(legacy_model or "").casefold().replace("_", "-")
                        in {"image2", "image-2", "gpt-image-2"}
                        else None
                    )
                    initial_request = CreateTurnRequest(
                        input=input_text,
                        agent_model_id=agent_model_id,
                        image_model_id=image_model_id,
                        explicit_tool_ids=[],
                        client_message_id=client_message_id,
                        metadata=turn_metadata,
                    )
                    events.append_in_transaction(
                        connection,
                        thread_id=thread_id,
                        turn_id=turn_id,
                        client_message_id=client_message_id,
                        event_type="turn.accepted",
                        payload={
                            "input": initial_request.input,
                            "agent_model_id": initial_request.agent_model_id,
                            "image_model_id": initial_request.image_model_id,
                            "explicit_tool_ids": initial_request.explicit_tool_ids,
                            "metadata": initial_request.metadata,
                            # Historical terminal Turns were never executed by
                            # v1 and therefore cannot claim a model-catalog or
                            # any other Runtime snapshot.
                            "model_catalog_snapshot_id": None,
                        },
                        idempotency_key=f"{turn_id}:accepted",
                        created_at=turn_created,
                    )
                    connection.execute(
                        """
                        INSERT INTO turns(
                            turn_id, thread_id, status, input_text,
                            agent_model_id, image_model_id,
                            client_message_id, metadata_json, terminal_reason,
                            created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            turn_id,
                            thread_id,
                            imported_status,
                            initial_request.input,
                            initial_request.agent_model_id,
                            initial_request.image_model_id,
                            initial_request.client_message_id,
                            json_dumps(initial_request.metadata),
                            terminal_reason,
                            turn_created.isoformat(timespec="microseconds"),
                            turn_updated.isoformat(timespec="microseconds"),
                        ),
                    )
                    if self._import_initial_turn_input_revision(
                        connection,
                        source_inventory_digest=inventory.digest,
                        legacy_session_id=legacy_session_id,
                        legacy_start_seq=start_seq,
                        thread_id=thread_id,
                        turn_id=turn_id,
                        request=initial_request,
                        created_at=turn_created,
                    ):
                        self.counts["turn_input_revisions"] += 1
                    connection.execute(
                        "INSERT INTO legacy_id_map(entity_kind, legacy_id, target_id, legacy_parent_id) VALUES ('turn', ?, ?, ?)",
                        (str(start_seq), turn_id, legacy_session_id),
                    )
                    self.counts["turns"] += 1
                    if legacy_request_id and not request_identity_ambiguous:
                        existing_turn = self._turn_by_request_id.get(legacy_request_id)
                        if existing_turn is not None and existing_turn != turn_id:
                            raise MigrationVerificationError(
                                "legacy request identity analysis became inconsistent"
                            )
                        self._turn_by_request_id[legacy_request_id] = turn_id

                    for message in group:
                        sequence = int(message["seq"])
                        role = str(message.get("role") or "unknown").casefold() or "unknown"
                        content = _parse_legacy_json(message.get("content"))
                        extras = _parse_legacy_json(message.get("extras"), fallback_to_text=False)
                        message_id = _stable_id("itm", inventory.digest, legacy_session_id, sequence)
                        message_client_id = client_message_id if message is user_message else None
                        message_content = {
                            "role": role,
                            "text": _display_text(content),
                            "legacy_content": content,
                            "legacy_seq": sequence,
                            "extras": _without_secrets(extras),
                        }
                        message_time = _legacy_datetime(message.get("created_at"))
                        events.append_in_transaction(
                            connection,
                            thread_id=thread_id,
                            turn_id=turn_id,
                            item_id=message_id,
                            client_message_id=message_client_id,
                            event_type="item.created",
                            payload={
                                "kind": "message",
                                "status": "completed",
                                "content": message_content,
                            },
                            idempotency_key=f"{turn_id}:legacy-message:{sequence}",
                            created_at=message_time,
                        )
                        connection.execute(
                            """
                            INSERT INTO items(
                                item_id, thread_id, turn_id, kind, status,
                                content_json, client_message_id, created_at, updated_at
                            ) VALUES (?, ?, ?, 'message', 'completed', ?, ?, ?, ?)
                            """,
                            (
                                message_id,
                                thread_id,
                                turn_id,
                                json_dumps(message_content),
                                message_client_id,
                                message_time.isoformat(timespec="microseconds"),
                                message_time.isoformat(timespec="microseconds"),
                            ),
                        )
                        legacy_message_id = f"{legacy_session_id}:{sequence}"
                        connection.execute(
                            "INSERT INTO legacy_id_map(entity_kind, legacy_id, target_id, legacy_parent_id) VALUES ('message', ?, ?, ?)",
                            (str(sequence), message_id, legacy_session_id),
                        )
                        self.counts["messages"] += 1

                        for ordinal, imported in enumerate(
                            artifacts.get((legacy_session_id, sequence), ())
                        ):
                            artifact_item_id = _stable_id(
                                "itm",
                                inventory.digest,
                                legacy_session_id,
                                sequence,
                                "artifact",
                                ordinal,
                                imported.projection.artifact_id,
                            )
                            public_projection = imported.projection.to_dict()
                            artifact_content = {
                                "artifact": public_projection,
                                "legacy_seq": sequence,
                            }
                            events.append_in_transaction(
                                connection,
                                thread_id=thread_id,
                                turn_id=turn_id,
                                item_id=artifact_item_id,
                                event_type="item.created",
                                payload={
                                    "kind": "artifact",
                                    "status": "completed",
                                    "content": artifact_content,
                                },
                                idempotency_key=f"{turn_id}:legacy-artifact:{sequence}:{ordinal}",
                                created_at=message_time,
                            )
                            connection.execute(
                                """
                                INSERT INTO items(
                                    item_id, thread_id, turn_id, kind, status,
                                    content_json, client_message_id, created_at, updated_at
                                ) VALUES (?, ?, ?, 'artifact', 'completed', ?, NULL, ?, ?)
                                """,
                                (
                                    artifact_item_id,
                                    thread_id,
                                    turn_id,
                                    json_dumps(artifact_content),
                                    message_time.isoformat(timespec="microseconds"),
                                    message_time.isoformat(timespec="microseconds"),
                                ),
                            )
                            connection.execute(
                                """
                                INSERT INTO migration_artifact_links(
                                    item_id, artifact_id, revision_id, legacy_message_id,
                                    legacy_relative_path, source_sha256
                                ) VALUES (?, ?, ?, ?, ?, ?)
                                """,
                                (
                                    artifact_item_id,
                                    imported.projection.artifact_id,
                                    imported.projection.revision_id,
                                    legacy_message_id,
                                    imported.relative_path,
                                    imported.source_sha256,
                                ),
                            )
                            self.counts["artifact_items"] += 1

                    events.append_in_transaction(
                        connection,
                        thread_id=thread_id,
                        turn_id=turn_id,
                        event_type=terminal_event_type,
                        payload={
                            # A historical imported Turn is never executed by
                            # v1.  Its one terminal transition therefore starts
                            # at the accepted fact instead of fabricating live
                            # preparing/model/tool phases that did not occur.
                            "from": "accepted",
                            "to": imported_status,
                            "reason": terminal_reason,
                        },
                        idempotency_key=f"{turn_id}:{imported_status}",
                        created_at=turn_updated,
                    )
        self._thread_ids = dict(thread_ids)
        return thread_ids

    @staticmethod
    def _recovery_attachments(value: Any) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            return []
        result: list[dict[str, Any]] = []
        for item in value[:50]:
            if not isinstance(item, Mapping):
                continue
            candidate = {
                key: _sanitized_legacy_value(item.get(key))
                for key in (
                    "file_name",
                    "fileName",
                    "file_type",
                    "fileType",
                    "file_path",
                    "filePath",
                    "path",
                    "url",
                )
                if item.get(key) not in (None, "")
            }
            if candidate:
                result.append(candidate)
        return result

    def _import_runtime_ledger(
        self,
        *,
        inventory: SourceInventory,
        runtime: LegacyRuntimeLedger,
        queued: LegacyQueuedRequests,
        database: SQLiteDatabase,
    ) -> None:
        queued_by_id = {str(row["request_id"]): row for row in queued.records}
        runs_by_id = {str(row["request_id"]): row for row in runtime.runs}
        inventory_entries = inventory_index(inventory)
        with database.transaction() as connection:
            for run in runtime.runs:
                request_id = str(run["request_id"])
                session_id = str(run["session_id"])
                source_status = str(run.get("status") or "")
                imported_status, _terminal_reason, _terminal_event = self._imported_run_status(
                    source_status
                )
                metadata = run.get("metadata")
                metadata = metadata if isinstance(metadata, Mapping) else {}
                queued_record = queued_by_id.get(request_id)
                queued_payload = (
                    queued_record.get("payload")
                    if isinstance(queued_record, Mapping)
                    and isinstance(queued_record.get("payload"), Mapping)
                    else {}
                )
                visible_input = str(
                    queued_payload.get("visible_message")
                    or queued_payload.get("visible_prompt")
                    or metadata.get("visible_message")
                    or ""
                ).strip()[: 64 * 1024]
                is_active = source_status in {
                    "queued",
                    "running",
                    "cancelling",
                    "finalizing",
                    "recovering",
                }
                recovery_status = (
                    "requires_user_confirmation"
                    if is_active and visible_input
                    else "diagnostic_only"
                    if is_active
                    else "historical"
                )
                thread_id = self._thread_ids.get(session_id)
                turn_id = self._turn_by_request_id.get(request_id)
                row_digest = hashlib.sha256(
                    json_dumps(_json_value(run)).encode("utf-8")
                ).hexdigest()
                connection.execute(
                    """
                    INSERT INTO legacy_run_records(
                        request_id, session_id, thread_id, turn_id, run_type,
                        source_status, imported_status, phase, recovery_status,
                        terminal_reason, error_code, model, provider, metadata_json,
                        source_row_digest, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        request_id,
                        session_id,
                        thread_id,
                        turn_id,
                        str(run.get("run_type") or "message")[:80],
                        source_status,
                        imported_status,
                        str(run.get("phase") or "")[:160],
                        recovery_status,
                        str(run.get("terminal_reason") or "")[:320] or None,
                        str(run.get("error_code") or "")[:160] or None,
                        str(run.get("model") or "")[:256] or None,
                        str(run.get("provider") or "")[:160] or None,
                        json_dumps(_sanitized_legacy_value(metadata)),
                        row_digest,
                        _legacy_datetime(run.get("created_at")).isoformat(timespec="microseconds"),
                        _legacy_datetime(run.get("updated_at")).isoformat(timespec="microseconds"),
                    ),
                )
                self.counts["legacy_runs"] += 1
                if recovery_status == "requires_user_confirmation":
                    raw_attachments = queued_payload.get("attachments") or metadata.get(
                        "attachment_items"
                    )
                    attachments = self._recovery_attachments(raw_attachments)
                    source_payload_digest: str | None = None
                    if queued_record is not None:
                        source_label = str(queued_record.get("source_relative_path") or "")
                        source_entry = inventory_entries.get(source_label)
                        if source_entry is None or source_entry.kind != "file":
                            raise SourceLayoutError(
                                "legacy queued request is missing from the source inventory"
                            )
                        source_payload_digest = source_entry.sha256
                    connection.execute(
                        """
                        INSERT INTO legacy_pending_work(
                            request_id, thread_id, turn_id, visible_input,
                            attachments_json, source_payload_digest, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            request_id,
                            thread_id,
                            turn_id,
                            _redact_sensitive_text(visible_input),
                            json_dumps(attachments),
                            source_payload_digest,
                            _legacy_datetime(
                                (queued_record or {}).get("created_at")
                                if isinstance(queued_record, Mapping)
                                else run.get("created_at")
                            ).isoformat(timespec="microseconds"),
                        ),
                    )
                    self.counts["pending_work"] += 1

            for event in runtime.events:
                request_id = str(event["request_id"])
                payload = event.get("payload")
                payload = payload if isinstance(payload, Mapping) else {}
                connection.execute(
                    """
                    INSERT INTO legacy_run_event_records(
                        request_id, event_seq, source_event_id, session_id, turn_id,
                        event_type, payload_json, source, idempotency_key, orphaned,
                        created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        request_id,
                        int(event["event_seq"]),
                        int(event["event_id"]),
                        str(event.get("session_id") or "")[:1024],
                        str(event.get("turn_id") or "")[:256],
                        str(event.get("event_type") or "")[:160],
                        json_dumps(_sanitized_legacy_value(payload)),
                        str(event.get("source") or "runtime")[:80],
                        str(event.get("idempotency_key") or "")[:512],
                        int(request_id not in runs_by_id),
                        _legacy_datetime(event.get("created_at")).isoformat(
                            timespec="microseconds"
                        ),
                    ),
                )
                self.counts["legacy_run_events"] += 1

            for run in runtime.runs:
                metadata = run.get("metadata")
                if not isinstance(metadata, Mapping):
                    continue
                if str(metadata.get("interrupt_mode") or "").casefold() != "branch":
                    continue
                parent_request_id = str(metadata.get("interrupts_request_id") or "").strip()
                parent_run = runs_by_id.get(parent_request_id)
                child_thread_id = self._thread_ids.get(str(run["session_id"]))
                parent_thread_id = (
                    self._thread_ids.get(str(parent_run["session_id"]))
                    if parent_run is not None
                    else None
                )
                if (
                    not parent_request_id
                    or parent_run is None
                    or child_thread_id is None
                    or parent_thread_id is None
                    or child_thread_id == parent_thread_id
                ):
                    self._warn(
                        "branch_lineage_unresolved",
                        str(run["request_id"]),
                        "branch metadata was preserved, but its parent Thread could not be proven",
                    )
                    continue
                parent_turn_id = self._turn_by_request_id.get(parent_request_id)
                child_started_at = _legacy_datetime(run.get("created_at")).isoformat(
                    timespec="microseconds"
                )
                if parent_turn_id:
                    seq_row = connection.execute(
                        "SELECT MAX(seq) FROM events WHERE thread_id = ? AND turn_id = ? "
                        "AND created_at <= ?",
                        (parent_thread_id, parent_turn_id, child_started_at),
                    ).fetchone()
                else:
                    seq_row = connection.execute(
                        "SELECT MAX(seq) FROM events WHERE thread_id = ? AND created_at <= ?",
                        (parent_thread_id, child_started_at),
                    ).fetchone()
                parent_seq = int(seq_row[0] or 0)
                if parent_seq < 1:
                    fallback = connection.execute(
                        "SELECT MIN(seq) FROM events WHERE thread_id = ?",
                        (parent_thread_id,),
                    ).fetchone()
                    parent_seq = int(fallback[0] or 1)
                existing = connection.execute(
                    "SELECT forked_from_thread_id FROM threads WHERE thread_id = ?",
                    (child_thread_id,),
                ).fetchone()
                if existing is None:
                    raise MigrationVerificationError("legacy branch child Thread disappeared")
                if existing[0] not in (None, parent_thread_id):
                    raise DuplicateLegacyIdError(
                        "legacy branch Thread has conflicting parent relationships"
                    )
                connection.execute(
                    """
                    UPDATE threads
                    SET forked_from_thread_id = ?, forked_from_turn_id = ?, forked_from_seq = ?
                    WHERE thread_id = ?
                    """,
                    (parent_thread_id, parent_turn_id, parent_seq, child_thread_id),
                )
                self.counts["thread_branches"] += 1

    def _import_scheduler_tasks(
        self,
        *,
        inventory: SourceInventory,
        tasks: LegacySchedulerTasks,
        database: SQLiteDatabase,
    ) -> None:
        supported_actions = {"send_message", "agent_task", "tool_call", "skill_call"}
        with database.transaction() as connection:
            for task in tasks.tasks:
                task_id = str(task["id"])
                action = task.get("action")
                action = action if isinstance(action, Mapping) else {}
                action_type = str(action.get("type") or "").casefold()
                activation_status = (
                    "requires_user_confirmation"
                    if action_type in supported_actions
                    else "unsupported_action"
                )
                row_digest = hashlib.sha256(
                    json_dumps(_json_value(task)).encode("utf-8")
                ).hexdigest()
                connection.execute(
                    """
                    INSERT INTO legacy_scheduler_tasks(
                        task_id, name, legacy_enabled, activation_status,
                        schedule_json, action_json, next_run_at, last_run_at,
                        source_row_digest, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        task_id,
                        _safe_subject(task.get("name"), limit=512),
                        int(bool(task.get("enabled", True))),
                        activation_status,
                        json_dumps(_sanitized_legacy_value(task.get("schedule") or {})),
                        json_dumps(_sanitized_legacy_value(action)),
                        str(task.get("next_run_at") or "")[:80] or None,
                        str(task.get("last_run_at") or "")[:80] or None,
                        row_digest,
                        str(task.get("created_at") or "")[:80] or None,
                        str(task.get("updated_at") or "")[:80] or None,
                    ),
                )
                self.counts["scheduler_tasks"] += 1

    def _import_permission_preference(
        self,
        *,
        permissions: Mapping[str, Any],
        database: SQLiteDatabase,
    ) -> None:
        if not permissions:
            return
        source_mode = str(permissions.get("mode") or "smart-ask").strip().casefold()
        known_modes = {"full-access", "smart-ask", "always-ask", "read-only", "custom"}
        if source_mode not in known_modes:
            self._warn(
                "permission_mode_normalized",
                "permissions",
                "unknown legacy permission mode was staged as the v1 default profile",
            )
        target_profile = "full_access" if source_mode == "full-access" else "default"
        raw_grants = permissions.get("alwaysAllow")
        raw_grants = raw_grants if isinstance(raw_grants, Mapping) else {}
        grants = sorted(
            str(key)[:256]
            for key, value in raw_grants.items()
            if value and isinstance(key, str) and key.strip()
        )[:4096]
        metadata = {
            "source_updated_at": str(permissions.get("updatedAt") or "")[:80] or None,
            "remembered_grant_count": len(grants),
            "filesystem_rule_count": (
                len((permissions.get("filesystem") or {}).get("rules") or [])
                if isinstance(permissions.get("filesystem"), Mapping)
                and isinstance((permissions.get("filesystem") or {}).get("rules"), list)
                else 0
            ),
            "note": "paths and remembered grants require v1 policy review before activation",
        }
        source_digest = hashlib.sha256(
            json_dumps(_json_value(permissions)).encode("utf-8")
        ).hexdigest()
        with database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO legacy_permission_preferences(
                    preference_id, source_mode, target_profile, activation_status,
                    remembered_grants_json, filesystem_policy_present,
                    metadata_json, source_digest
                ) VALUES ('legacy-default', ?, ?, 'staged_for_account_binding', ?, ?, ?, ?)
                """,
                (
                    source_mode if source_mode in known_modes else "smart-ask",
                    target_profile,
                    json_dumps(grants),
                    int(isinstance(permissions.get("filesystem"), Mapping)),
                    json_dumps(metadata),
                    source_digest,
                ),
            )
        self.counts["permission_preferences"] += 1

    def _store_source_evidence(self, database: SQLiteDatabase) -> None:
        if self.source_evidence is None:
            raise MigrationVerificationError("legacy source evidence was not evaluated")
        evidence = self.source_evidence
        with database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO migration_source_evidence(
                    evidence_id, evidence_level, marker_label, marker_sha256,
                    declared_version, declared_commit, package_sha256,
                    schema_fingerprint, schema_tables_json
                ) VALUES ('v030-source', ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    evidence.evidence_level,
                    evidence.marker_label,
                    evidence.marker_sha256,
                    evidence.declared_version,
                    evidence.declared_commit,
                    evidence.package_sha256,
                    evidence.schema_fingerprint,
                    json_dumps(list(evidence.schema_tables)),
                ),
            )
        self.counts["source_evidence"] = 1

    @staticmethod
    def _bounded_path_metadata(value: Any) -> str:
        return str(value or "").replace("\x00", "").strip()[:4096]

    def _import_projects(
        self,
        *,
        inventory: SourceInventory,
        ui_state: Mapping[str, Any],
        conversations: LegacyConversations,
        thread_ids: Mapping[str, str],
        database: SQLiteDatabase,
    ) -> None:
        records: dict[str, dict[str, Any]] = {}
        raw_projects = ui_state.get("projects")
        if isinstance(raw_projects, list):
            for raw in raw_projects:
                if not isinstance(raw, Mapping):
                    continue
                legacy_id = str(raw.get("id") or raw.get("projectId") or "").strip()
                project_path = self._bounded_path_metadata(raw.get("path") or raw.get("projectPath"))
                if not legacy_id and project_path:
                    legacy_id = _stable_id("legacy-project", project_path)
                if not legacy_id:
                    continue
                item = {
                    "legacy_id": legacy_id,
                    "name": _safe_subject(raw.get("name") or raw.get("title") or legacy_id),
                    "path": project_path,
                    "memory_path": self._bounded_path_metadata(
                        raw.get("memoryPath") or raw.get("projectMemoryPath")
                    ),
                    "dreams_path": self._bounded_path_metadata(
                        raw.get("dreamsPath") or raw.get("projectDreamsPath")
                    ),
                    "metadata": _without_secrets(
                        {
                            key: value
                            for key, value in raw.items()
                            if key
                            not in {
                                "id",
                                "projectId",
                                "name",
                                "title",
                                "path",
                                "projectPath",
                                "memoryPath",
                                "projectMemoryPath",
                                "dreamsPath",
                                "projectDreamsPath",
                            }
                        }
                    ),
                }
                existing = records.get(legacy_id)
                if existing is not None and existing != item:
                    raise DuplicateLegacyIdError(
                        f"legacy project id {legacy_id!r} has conflicting UI rows"
                    )
                records[legacy_id] = item

        for session in conversations.sessions:
            legacy_id = str(session.get("project_id") or "").strip()
            project_path = self._bounded_path_metadata(session.get("project_path"))
            if not legacy_id and project_path:
                legacy_id = _stable_id("legacy-project", project_path)
            if not legacy_id:
                continue
            candidate = {
                "legacy_id": legacy_id,
                "name": _safe_subject(session.get("project_name") or legacy_id),
                "path": project_path,
                "memory_path": self._bounded_path_metadata(session.get("project_memory_path")),
                "dreams_path": self._bounded_path_metadata(session.get("project_dreams_path")),
                "metadata": {},
            }
            existing = records.get(legacy_id)
            if existing is None:
                records[legacy_id] = candidate
            else:
                for key in ("name", "path", "memory_path", "dreams_path"):
                    if not existing.get(key) and candidate.get(key):
                        existing[key] = candidate[key]
                    elif (
                        key == "path"
                        and existing.get(key)
                        and candidate.get(key)
                        and existing[key] != candidate[key]
                    ):
                        self._warn(
                            "project_path_conflict",
                            legacy_id,
                            "UI project path retained over session project path",
                        )

        active_project = str(ui_state.get("activeProjectId") or "").strip()
        pinned_raw = ui_state.get("pinnedProjects")
        pinned = {
            str(key)
            for key, value in pinned_raw.items()
            if isinstance(pinned_raw, Mapping) and bool(value)
        } if isinstance(pinned_raw, Mapping) else set()
        target_ids = {
            legacy_id: _stable_id("prj", inventory.digest, legacy_id)
            for legacy_id in records
        }

        session_projects = ui_state.get("sessionProjects")
        session_projects = session_projects if isinstance(session_projects, Mapping) else {}
        session_bindings = ui_state.get("sessionProjectBindings")
        session_bindings = session_bindings if isinstance(session_bindings, Mapping) else {}
        session_rows = {str(row["session_id"]): row for row in conversations.sessions}

        with database.transaction() as connection:
            for legacy_id in sorted(records):
                item = records[legacy_id]
                project_id = target_ids[legacy_id]
                connection.execute(
                    """
                    INSERT INTO projects(
                        project_id, legacy_project_id, name, project_path,
                        memory_path, dreams_path, pinned, active, metadata_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        project_id,
                        legacy_id,
                        item["name"] or legacy_id,
                        item["path"],
                        item["memory_path"],
                        item["dreams_path"],
                        int(legacy_id in pinned),
                        int(legacy_id == active_project),
                        json_dumps(item["metadata"]),
                    ),
                )
                connection.execute(
                    "INSERT INTO legacy_id_map(entity_kind, legacy_id, target_id) VALUES ('project', ?, ?)",
                    (legacy_id, project_id),
                )
                self.counts["projects"] += 1

            for legacy_session_id, thread_id in thread_ids.items():
                binding = session_bindings.get(legacy_session_id)
                binding_map = binding if isinstance(binding, Mapping) else {}
                legacy_project_id = str(
                    binding_map.get("projectId")
                    or session_projects.get(legacy_session_id)
                    or session_rows.get(legacy_session_id, {}).get("project_id")
                    or ""
                ).strip()
                if not legacy_project_id:
                    continue
                project_id = target_ids.get(legacy_project_id)
                if project_id is None:
                    self._warn(
                        "project_binding_orphaned",
                        legacy_session_id,
                        "session project binding referenced an unknown project",
                    )
                    continue
                source_kind = (
                    "ui_binding"
                    if binding_map
                    else "ui_mapping"
                    if legacy_session_id in session_projects
                    else "session_row"
                )
                connection.execute(
                    """
                    INSERT INTO project_thread_bindings(
                        thread_id, project_id, source, metadata_json
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (
                        thread_id,
                        project_id,
                        source_kind,
                        json_dumps(_without_secrets(dict(binding_map))),
                    ),
                )
                self.counts["project_bindings"] += 1

    def _canonical_memory_path(
        self,
        source: Path,
        raw_value: object,
        inventory: SourceInventory,
    ) -> tuple[str, tuple[Path, str] | None, str]:
        raw = str(raw_value or "").replace("\x00", "").strip()
        resolved = self._resolve_source_file(
            source, raw, inventory, memory_fallback=True
        )
        if resolved is not None:
            return resolved[1], resolved, "stored"
        raw_path = Path(raw) if raw else Path("missing")
        if raw and not raw_path.is_absolute() and ".." not in raw_path.parts:
            normalized = (Path("memory") / raw_path).as_posix()
            return normalized[:4096], None, "missing"
        opaque = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]
        return f"unresolved/{opaque}", None, "unsafe"

    def _import_memory(
        self,
        *,
        source: Path,
        inventory: SourceInventory,
        memory: LegacyMemory,
        database: SQLiteDatabase,
        artifact_service: ArtifactService,
    ) -> None:
        memory_paths: dict[str, tuple[str, tuple[Path, str] | None, str]] = {}
        for row in memory.files:
            raw_path = str(row.get("path") or "")
            memory_paths[raw_path] = self._canonical_memory_path(
                source, raw_path, inventory
            )

        with database.transaction() as connection:
            for row in memory.chunks:
                legacy_id = str(row["id"])
                canonical_path, _resolved, availability = self._canonical_memory_path(
                    source, row.get("path"), inventory
                )
                if availability == "unsafe":
                    self._warn(
                        "memory_path_rejected",
                        legacy_id,
                        "memory chunk path was external or traversing",
                    )
                try:
                    start_line = max(0, int(row.get("start_line") or 0))
                    end_line = max(start_line, int(row.get("end_line") or start_line))
                except (TypeError, ValueError):
                    start_line = end_line = 0
                    self._warn(
                        "memory_line_range_repaired",
                        legacy_id,
                        "invalid line range was normalized to zero",
                    )
                metadata = _parse_legacy_json(
                    row.get("metadata"), fallback_to_text=False
                )
                record_id = _stable_id("mem", inventory.digest, legacy_id)
                connection.execute(
                    """
                    INSERT INTO memory_canonical_records(
                        record_id, legacy_chunk_id, user_id, scope, source, path,
                        start_line, end_line, text, legacy_hash, metadata_json,
                        embedding_state, created_at, updated_at, memory_origin, memory_state
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'rebuild_required', ?, ?,
                              'imported', 'active')
                    """,
                    (
                        record_id,
                        legacy_id,
                        row.get("user_id"),
                        str(row.get("scope") or "shared"),
                        str(row.get("source") or "memory"),
                        canonical_path,
                        start_line,
                        end_line,
                        str(row.get("text") or ""),
                        str(row.get("hash") or ""),
                        json_dumps(_without_secrets(metadata)),
                        row.get("created_at"),
                        row.get("updated_at"),
                    ),
                )
                connection.execute(
                    "INSERT INTO legacy_id_map(entity_kind, legacy_id, target_id) VALUES ('memory_chunk', ?, ?)",
                    (legacy_id, record_id),
                )
                self.counts["memory_records"] += 1

            for row in memory.files:
                raw_path = str(row.get("path") or "")
                canonical_path, resolved, availability = memory_paths[raw_path]
                blob_sha256: str | None = None
                if resolved is not None:
                    path, relative = resolved
                    try:
                        content = stable_read_bytes(
                            path,
                            label="legacy memory file",
                            maximum=self.options.max_artifact_bytes,
                            root=source,
                        )
                    except SourceLayoutError:
                        availability = "missing"
                        self._warn(
                            "memory_file_oversized",
                            canonical_path,
                            "memory file metadata was retained but bytes exceeded the migration limit or became unsafe",
                        )
                    if availability != "missing":
                        inventory_entry = inventory_index(inventory)[relative]
                        digest = hashlib.sha256(content).hexdigest()
                        if digest != inventory_entry.sha256:
                            raise SourceChangedError("legacy memory file changed after source inventory")
                        blob = artifact_service.blobs.put_bytes(content)
                        blob_sha256 = blob.sha256
                        try:
                            declared_size = int(row.get("size") or 0)
                        except (TypeError, ValueError):
                            declared_size = -1
                        if declared_size != len(content):
                            self._warn(
                                "memory_file_size_recomputed",
                                canonical_path,
                                "legacy index size differed from source bytes",
                            )
                elif availability == "unsafe":
                    self._warn(
                        "memory_file_path_rejected",
                        canonical_path,
                        "memory file content was not read outside source_root",
                    )
                try:
                    mtime = int(row.get("mtime") or 0)
                    size_bytes = max(0, int(row.get("size") or 0))
                except (TypeError, ValueError):
                    mtime = size_bytes = 0
                connection.execute(
                    """
                    INSERT INTO memory_files(
                        path, source, legacy_hash, mtime, size_bytes,
                        updated_at, blob_sha256, availability, memory_origin, memory_state
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'imported', 'active')
                    """,
                    (
                        canonical_path,
                        str(row.get("source") or "memory"),
                        str(row.get("hash") or ""),
                        mtime,
                        size_bytes,
                        row.get("updated_at"),
                        blob_sha256,
                        availability,
                    ),
                )
                self.counts["memory_files"] += 1
                if blob_sha256:
                    connection.execute(
                        "INSERT INTO migration_memory_blob_links(path, blob_sha256) "
                        "VALUES (?, ?)",
                        (canonical_path, blob_sha256),
                    )
                    self.counts["memory_files_stored"] += 1

    @staticmethod
    def _active_channels(config: Mapping[str, Any]) -> set[str]:
        raw = config.get("channel_type")
        values = raw.split(",") if isinstance(raw, str) else raw if isinstance(raw, list) else []
        return {
            _CHANNEL_ALIASES.get(str(value).strip().casefold(), str(value).strip().casefold())
            for value in values
            if str(value).strip()
        }

    @staticmethod
    def _mcp_servers(payload: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
        raw = payload.get("mcpServers")
        if isinstance(raw, Mapping):
            return {
                str(name): dict(value)
                for name, value in raw.items()
                if isinstance(value, Mapping)
            }
        raw_list = payload.get("mcp_servers")
        if isinstance(raw_list, list):
            return {
                str(item.get("name")): {
                    key: value for key, value in item.items() if key != "name"
                }
                for item in raw_list
                if isinstance(item, Mapping) and item.get("name")
            }
        return {}

    @staticmethod
    def _endpoint_origin(raw: Any) -> str:
        parsed = urlparse(str(raw or ""))
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return ""
        host = parsed.hostname.casefold()
        try:
            port = parsed.port
        except ValueError:
            return ""
        if port:
            host = f"{host}:{port}"
        return f"{parsed.scheme}://{host}"

    def _import_connectors(
        self,
        *,
        inventory: SourceInventory,
        config: Mapping[str, Any],
        mcp: Mapping[str, Any],
        database: SQLiteDatabase,
    ) -> None:
        active = self._active_channels(config)
        connector_rows: dict[str, dict[str, Any]] = {}
        for connector_id, fields in _CHANNEL_FIELDS.items():
            configured_fields = [
                key for key, _secret in fields if config.get(key) not in (None, "")
            ]
            if connector_id not in active and not configured_fields:
                continue
            metadata_values = {
                key: _json_value(config.get(key))
                for key, secret in fields
                if not secret and config.get(key) not in (None, "")
            }
            has_credentials = any(
                secret and config.get(key) not in (None, "") for key, secret in fields
            )
            connector_rows[connector_id] = {
                "tier": "stable" if connector_id == "feishu" else "beta",
                "legacy_enabled": connector_id in active,
                "credential_quarantined": has_credentials,
                "source": "config.json",
                "metadata": {
                    "configured_fields": configured_fields,
                    "non_secret_values": metadata_values,
                },
            }

        for server_name, entry in self._mcp_servers(mcp).items():
            lowered = server_name.strip().casefold()
            connector_id = "tencent-docs" if lowered == "tencent-docs" else f"mcp-{hashlib.sha256(lowered.encode()).hexdigest()[:16]}"
            has_credentials = bool(collect_secrets(entry, source_relative_path="mcp.json"))
            connector_rows[connector_id] = {
                "tier": "stable" if connector_id == "tencent-docs" else "beta",
                "legacy_enabled": True,
                "credential_quarantined": has_credentials,
                "source": "mcp.json",
                "metadata": {
                    "legacy_server_name": _safe_subject(server_name),
                    "type": _safe_subject(entry.get("type")),
                    "endpoint_origin": self._endpoint_origin(entry.get("url")),
                    "command_name": Path(str(entry.get("command") or "")).name[:128],
                },
            }

        with database.transaction() as connection:
            for connector_id in sorted(connector_rows):
                row = connector_rows[connector_id]
                quarantined = bool(row["credential_quarantined"])
                activation_status = "requires_reauth" if quarantined else "pending_validation"
                connection.execute(
                    """
                    INSERT INTO connector_instances(
                        instance_id, connector_id, tier, legacy_enabled,
                        activation_status, credential_quarantined, source, metadata_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        _stable_id("con", inventory.digest, connector_id),
                        connector_id,
                        row["tier"],
                        int(row["legacy_enabled"]),
                        activation_status,
                        int(quarantined),
                        row["source"],
                        json_dumps(row["metadata"]),
                    ),
                )
                self.counts["connectors"] += 1

    @staticmethod
    def _skill_source_group(value: Any) -> str:
        lowered = str(value or "").casefold()
        if "builtin" in lowered:
            return "builtin"
        if "plugin" in lowered:
            return "plugin"
        if "custom" in lowered or "workspace" in lowered:
            return "workspace"
        return "external"

    def _import_skills(
        self,
        *,
        inventory: SourceInventory,
        skills: Mapping[str, Any],
        database: SQLiteDatabase,
    ) -> None:
        with database.transaction() as connection:
            for raw_name in sorted(skills):
                value = skills[raw_name]
                if not isinstance(value, Mapping):
                    continue
                name = str(raw_name).replace("\x00", "").strip()
                if not name or len(name) > 256:
                    self._warn("skill_name_rejected", "skill", "invalid skill name was skipped")
                    continue
                metadata = {
                    key: _without_secrets(value.get(key))
                    for key in (
                        "description",
                        "category",
                        "default_enabled",
                        "builtin_catalog",
                        "mentionable",
                        "mention_category",
                        "display_name",
                    )
                    if key in value
                }
                connection.execute(
                    """
                    INSERT INTO skill_states(
                        skill_id, name, enabled, source, activation_status, metadata_json
                    ) VALUES (?, ?, ?, ?, 'pending_contract_validation', ?)
                    """,
                    (
                        _stable_id("skl", inventory.digest, name),
                        name,
                        int(bool(value.get("enabled", True))),
                        self._skill_source_group(value.get("source")),
                        json_dumps(metadata),
                    ),
                )
                self.counts["skill_states"] += 1

    def _store_warnings(self, database: SQLiteDatabase) -> None:
        with database.transaction() as connection:
            for index, warning in enumerate(self.warnings):
                connection.execute(
                    """
                    INSERT INTO migration_warnings(warning_index, code, subject, detail)
                    VALUES (?, ?, ?, ?)
                    """,
                    (index, warning.code, warning.subject, warning.detail),
                )

    @staticmethod
    def _secret_needles(records: Iterable[SecretRecord]) -> tuple[bytes, ...]:
        values: list[bytes] = []

        def visit(value: Any) -> None:
            if isinstance(value, str):
                encoded = value.encode("utf-8")
                if len(encoded) >= 6:
                    values.append(encoded)
            elif isinstance(value, Mapping):
                for child in value.values():
                    visit(child)
            elif isinstance(value, list):
                for child in value:
                    visit(child)

        for record in records:
            visit(record.value)
        return tuple(dict.fromkeys(values))

    def _assert_plaintext_secrets_absent(self, staging: Path) -> None:
        needles = self._secret_needles(self.secret_records)
        if not needles:
            return
        for path in staging.rglob("*"):
            if not path.is_file():
                continue
            relative = path.relative_to(staging).as_posix()
            if relative == QUARANTINE_NAME or relative.startswith("backups/"):
                continue
            data = path.read_bytes()
            if any(needle in data for needle in needles):
                raise MigrationVerificationError(
                    "a quarantined credential was found outside the encrypted quarantine"
                )

    def _verify_stage(
        self,
        *,
        staging: Path,
        inventory: SourceInventory,
        database: SQLiteDatabase,
        artifact_service: ArtifactService,
        schema_identity: ImportSchemaIdentity,
    ) -> tuple[str, ...]:
        with database.reader() as connection:
            try:
                observed_schema_identity = current_import_schema_identity(
                    connection,
                    migration_id=self._migration_id(inventory),
                    source_inventory_digest=inventory.digest,
                    import_layout_version=schema_identity.import_layout_version,
                )
            except ImportSchemaIdentityError as error:
                raise MigrationVerificationError(
                    "staged import schema identity could not be verified"
                ) from error
            migration_meta = {
                str(row[0]): str(row[1])
                for row in connection.execute(
                    "SELECT key,value FROM migration_meta WHERE key IN ("
                    "'import_layout_version','migration_id',"
                    "'source_inventory_digest','data_generation_id',"
                    "'import_target_storage_schema_version',"
                    "'import_target_schema_sha256') ORDER BY key"
                ).fetchall()
            }
            integrity = connection.execute("PRAGMA integrity_check").fetchall()
            foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
            actual = {
                "threads": connection.execute("SELECT COUNT(*) FROM threads").fetchone()[0],
                "turns": connection.execute("SELECT COUNT(*) FROM turns").fetchone()[0],
                "turn_input_revisions": connection.execute(
                    "SELECT COUNT(*) FROM turn_input_revisions"
                ).fetchone()[0],
                "messages": connection.execute(
                    "SELECT COUNT(*) FROM legacy_id_map WHERE entity_kind = 'message'"
                ).fetchone()[0],
                "artifact_items": connection.execute(
                    "SELECT COUNT(*) FROM migration_artifact_links"
                ).fetchone()[0],
                "projects": connection.execute("SELECT COUNT(*) FROM projects").fetchone()[0],
                "project_bindings": connection.execute(
                    "SELECT COUNT(*) FROM project_thread_bindings"
                ).fetchone()[0],
                "memory_records": connection.execute(
                    "SELECT COUNT(*) FROM memory_canonical_records"
                ).fetchone()[0],
                "memory_files": connection.execute("SELECT COUNT(*) FROM memory_files").fetchone()[0],
                "connectors": connection.execute(
                    "SELECT COUNT(*) FROM connector_instances"
                ).fetchone()[0],
                "skill_states": connection.execute("SELECT COUNT(*) FROM skill_states").fetchone()[0],
                "legacy_runs": connection.execute(
                    "SELECT COUNT(*) FROM legacy_run_records"
                ).fetchone()[0],
                "legacy_run_events": connection.execute(
                    "SELECT COUNT(*) FROM legacy_run_event_records"
                ).fetchone()[0],
                "pending_work": connection.execute(
                    "SELECT COUNT(*) FROM legacy_pending_work"
                ).fetchone()[0],
                "scheduler_tasks": connection.execute(
                    "SELECT COUNT(*) FROM legacy_scheduler_tasks"
                ).fetchone()[0],
                "permission_preferences": connection.execute(
                    "SELECT COUNT(*) FROM legacy_permission_preferences"
                ).fetchone()[0],
                "thread_branches": connection.execute(
                    "SELECT COUNT(*) FROM threads WHERE forked_from_thread_id IS NOT NULL"
                ).fetchone()[0],
                "source_evidence": connection.execute(
                    "SELECT COUNT(*) FROM migration_source_evidence"
                ).fetchone()[0],
                "events": connection.execute("SELECT COUNT(*) FROM events").fetchone()[0],
            }
            digest_rows = connection.execute(
                """
                SELECT links.source_sha256, revisions.sha256
                FROM migration_artifact_links AS links
                JOIN artifact_revisions AS revisions
                  ON revisions.revision_id = links.revision_id
                """
            ).fetchall()
            memory_digests = [
                str(row[0])
                for row in connection.execute(
                    "SELECT blob_sha256 FROM migration_memory_blob_links"
                )
            ]
            orphan_items = connection.execute(
                """
                SELECT COUNT(*)
                FROM items AS item
                LEFT JOIN turns AS turn ON turn.turn_id = item.turn_id
                LEFT JOIN threads AS thread ON thread.thread_id = item.thread_id
                WHERE turn.turn_id IS NULL OR thread.thread_id IS NULL
                   OR turn.thread_id <> item.thread_id
                """
            ).fetchone()[0]
            invalid_run_links = connection.execute(
                """
                SELECT COUNT(*)
                FROM legacy_run_records AS run
                LEFT JOIN turns AS turn ON turn.turn_id = run.turn_id
                WHERE run.turn_id IS NOT NULL
                  AND (turn.turn_id IS NULL OR turn.thread_id <> run.thread_id)
                """
            ).fetchone()[0]
            executable_legacy_work = connection.execute(
                """
                SELECT
                    (SELECT COUNT(*) FROM legacy_scheduler_tasks
                     WHERE activation_status NOT IN ('requires_user_confirmation', 'unsupported_action'))
                  + (SELECT COUNT(*) FROM legacy_pending_work
                     WHERE recovery_status <> 'requires_user_confirmation')
                """
            ).fetchone()[0]
            invalid_forks = connection.execute(
                """
                SELECT COUNT(*) FROM threads
                WHERE forked_from_thread_id = thread_id
                   OR (forked_from_thread_id IS NOT NULL AND forked_from_seq IS NULL)
                """
            ).fetchone()[0]
            turn_input_contract_errors = _legacy_turn_input_contract_errors(
                connection,
                source_inventory_digest=inventory.digest,
            )

        expected_meta = {
            "import_layout_version": str(schema_identity.import_layout_version),
            "migration_id": self._migration_id(inventory),
            "source_inventory_digest": inventory.digest,
            "data_generation_id": schema_identity.data_generation_id,
            "import_target_storage_schema_version": str(
                schema_identity.target_storage_schema_version
            ),
            "import_target_schema_sha256": schema_identity.target_schema_sha256,
        }
        if observed_schema_identity != schema_identity or migration_meta != expected_meta:
            raise MigrationVerificationError(
                "staged import schema identity differs from its migration ledger"
            )
        if [tuple(row) for row in integrity] != [("ok",)]:
            raise MigrationVerificationError("staged target failed SQLite integrity_check")
        if foreign_keys:
            raise MigrationVerificationError("staged target contains foreign-key violations")
        if orphan_items:
            raise MigrationVerificationError("staged message/item relations are inconsistent")
        if invalid_run_links:
            raise MigrationVerificationError("staged legacy run/Turn relations are inconsistent")
        if executable_legacy_work:
            raise MigrationVerificationError("legacy work was staged in an executable state")
        if invalid_forks:
            raise MigrationVerificationError("staged legacy branch lineage is inconsistent")
        if turn_input_contract_errors:
            raise MigrationVerificationError(
                "staged legacy Turn input/snapshot contract is inconsistent: "
                + turn_input_contract_errors[0]
            )
        for key, value in actual.items():
            expected = int(self._baseline_counts.get(key, 0)) + int(
                self.counts.get(key, 0)
            )
            if key != "events" and int(value) != expected:
                raise MigrationVerificationError(
                    f"staged {key} count does not match the migration ledger"
                )
            self.counts[key] = int(value)
        if any(str(source_sha) != str(revision_sha) for source_sha, revision_sha in digest_rows):
            raise MigrationVerificationError("staged Artifact revision digest differs from source inventory")

        projections = artifact_service.list_user_artifacts()
        if len(projections) != len(self._baseline_artifact_ids) + self.counts.get(
            "artifacts", 0
        ):
            raise MigrationVerificationError("staged user Artifact count is inconsistent")
        if any(not projection.is_user_visible for projection in projections):
            raise MigrationVerificationError("an internal Artifact leaked into the user projection")
        internal_suffixes = {".py", ".js", ".ts", ".tsx", ".css", ".sh", ".ps1", ".diff", ".log"}
        if any(Path(item.display_name).suffix.casefold() in internal_suffixes for item in projections):
            raise MigrationVerificationError("an implementation file leaked into the user Artifact projection")
        sampled: list[str] = []
        imported_projections = [
            projection
            for projection in projections
            if projection.artifact_id not in self._baseline_artifact_ids
        ]
        for projection in imported_projections[: max(0, int(self.options.sample_size))]:
            content = artifact_service.read_user_content(
                projection.artifact_id, projection.revision_id
            )
            if hashlib.sha256(content).hexdigest() != projection.sha256:
                raise MigrationVerificationError("sampled Artifact preview failed digest verification")
            sampled.append(projection.artifact_id)
        for digest in memory_digests:
            artifact_service.blobs.read_bytes(digest)

        referenced_digests = [str(row[1]) for row in digest_rows]
        referenced_digests.extend(memory_digests)
        _write_json(
            staging / CAS_AUTHORITY_NAME,
            build_cas_authority(
                source_inventory_digest=inventory.digest,
                digests=referenced_digests,
            ),
        )

        self._assert_plaintext_secrets_absent(staging)
        checkpoint_connection = database.connect()
        try:
            checkpoint_connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            checkpoint_connection.close()
        self.counts["source_inventory_entries"] = len(inventory.entries)
        self.counts["source_inventory_bytes"] = inventory.total_bytes
        self.counts["cas_blobs"] = sum(
            1
            for path in (staging / TARGET_ARTIFACT_ROOT_NAME / "blobs").rglob("*")
            if path.is_file()
        )
        return tuple(sampled)

    def _write_trace(self, staging: Path) -> None:
        path = staging / TRACE_NAME
        path.write_text(
            "".join(
                json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
                for row in self._trace_rows
            ),
            encoding="utf-8",
        )

    def _complete_run(
        self,
        *,
        database: SQLiteDatabase,
        report: MigrationReport,
    ) -> None:
        with database.transaction() as connection:
            connection.execute(
                """
                UPDATE migration_runs
                SET status = ?, completed_at = ?, report_json = ?
                WHERE migration_id = ?
                """,
                (
                    report.status,
                    _iso_now(),
                    json_dumps(report.to_dict()),
                    report.migration_id,
                ),
            )

    @staticmethod
    def _verify_completed_database(
        database: SQLiteDatabase, migration_id: str, expected_status: str
    ) -> None:
        connection = database.connect()
        try:
            row = connection.execute(
                "SELECT status, completed_at FROM migration_runs WHERE migration_id = ?",
                (migration_id,),
            ).fetchone()
            integrity = connection.execute("PRAGMA integrity_check").fetchall()
            foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            connection.close()
        if row is None or row["status"] != expected_status or not row["completed_at"]:
            raise MigrationVerificationError("completed migration ledger was not persisted")
        if [tuple(item) for item in integrity] != [("ok",)] or foreign_keys:
            raise MigrationVerificationError("completed migration database failed final verification")

    def run(self) -> MigrationReport:
        source, target = assert_disjoint_roots(
            Path(self.options.source_root), Path(self.options.target_root)
        )
        self._configure_pinned_sources(source, target)
        before = self._inventory_source(source)
        migration_id = self._migration_id(before)
        existing = self._existing_report(source, target, before)
        if existing is not None:
            return existing

        staging: Path | None = None
        temporary: tempfile.TemporaryDirectory[str] | None = None
        published = False
        try:
            staging, temporary = self._make_staging(target)
            self._clone_baseline_state(staging)
            self._trace(
                "inventory.completed",
                source_inventory_digest=before.digest,
                entry_count=len(before.entries),
            )
            _write_json(staging / INVENTORY_NAME, before.to_dict())

            conversations, memory, runtime = self._snapshot_databases(
                source, staging, before
            )
            self._trace(
                "backups.completed",
                backup_count=len(self.backups),
                session_count=len(conversations.sessions),
                message_count=len(conversations.messages),
                memory_record_count=len(memory.chunks),
                legacy_run_count=len(runtime.runs),
                legacy_run_event_count=len(runtime.events),
            )
            config, mcp_file, ui_state, skills, permissions = self._load_legacy_json(source)
            conversations = self._merge_ui_state_history(conversations, ui_state)
            self._legacy_warnings(conversations.warnings)
            queued_requests = read_queued_requests(source)
            scheduler_tasks = read_scheduler_tasks(source)
            self._legacy_warnings(queued_requests.warnings)
            self._legacy_warnings(scheduler_tasks.warnings)
            if self.secret_records and self.options.quarantine_key is None:
                if self.options.dry_run:
                    self._warn(
                        "quarantine_key_required_for_commit",
                        "quarantine",
                        "dry-run found credentials; a vault-supplied key is required for commit",
                    )
                else:
                    raise QuarantineKeyRequired(
                        "legacy credentials require a vault-supplied quarantine key"
                    )

            database_path = staging / TARGET_DATABASE_NAME
            database = initialize_target_database(database_path)
            artifact_service = ArtifactService(
                staging / TARGET_ARTIFACT_ROOT_NAME, database_path=database_path
            )
            with database.transaction() as connection:
                try:
                    schema_identity = current_import_schema_identity(
                        connection,
                        migration_id=migration_id,
                        source_inventory_digest=before.digest,
                        import_layout_version=IMPORT_LAYOUT_VERSION,
                    )
                except ImportSchemaIdentityError as error:
                    raise MigrationVerificationError(
                        "import target schema identity could not be established"
                    ) from error
                for key, value in {
                    "migration_id": migration_id,
                    "source_inventory_digest": before.digest,
                    "data_generation_id": schema_identity.data_generation_id,
                    "import_target_storage_schema_version": str(
                        schema_identity.target_storage_schema_version
                    ),
                    "import_target_schema_sha256": schema_identity.target_schema_sha256,
                }.items():
                    connection.execute(
                        "INSERT OR REPLACE INTO migration_meta(key, value) VALUES (?, ?)",
                        (key, value),
                    )
                connection.execute(
                    """
                    INSERT INTO migration_runs(
                        migration_id, source_version, target_version,
                        source_inventory_digest, status, started_at, report_json
                    ) VALUES (?, ?, ?, ?, 'staging', ?, '{}')
                    """,
                    (
                        migration_id,
                        before.source_version,
                        TARGET_VERSION,
                        before.digest,
                        _iso_now(),
                    ),
                )
            self._trace(
                "target.initialized",
                import_layout_version=IMPORT_LAYOUT_VERSION,
                target_storage_schema_version=(
                    schema_identity.target_storage_schema_version
                ),
                target_schema_sha256=schema_identity.target_schema_sha256,
                data_generation_id=schema_identity.data_generation_id,
            )

            imported_artifacts = self._import_artifacts(
                source=source,
                inventory=before,
                messages=conversations.messages,
                service=artifact_service,
            )
            thread_ids = self._import_conversations(
                inventory=before,
                conversations=conversations,
                runtime=runtime,
                artifacts=imported_artifacts,
                database=database,
            )
            self._import_runtime_ledger(
                inventory=before,
                runtime=runtime,
                queued=queued_requests,
                database=database,
            )
            self._import_projects(
                inventory=before,
                ui_state=ui_state,
                conversations=conversations,
                thread_ids=thread_ids,
                database=database,
            )
            self._import_memory(
                source=source,
                inventory=before,
                memory=memory,
                database=database,
                artifact_service=artifact_service,
            )
            merged_mcp_servers = self._mcp_servers(
                {"mcp_servers": config.get("mcp_servers", [])}
            )
            merged_mcp_servers.update(self._mcp_servers(mcp_file))
            self._import_connectors(
                inventory=before,
                config=config,
                mcp={"mcpServers": merged_mcp_servers},
                database=database,
            )
            self._import_skills(
                inventory=before,
                skills=skills,
                database=database,
            )
            self._import_scheduler_tasks(
                inventory=before,
                tasks=scheduler_tasks,
                database=database,
            )
            self._import_permission_preference(
                permissions=permissions,
                database=database,
            )
            self._store_source_evidence(database)
            if self.secret_records and self.options.quarantine_key is not None:
                encrypt_quarantine(
                    self.secret_records,
                    key=self.options.quarantine_key,
                    associated_digest=before.digest,
                    destination=staging / QUARANTINE_NAME,
                )
            self._store_warnings(database)
            self._trace("import.completed", counts=dict(self.counts))

            sampled = self._verify_stage(
                staging=staging,
                inventory=before,
                database=database,
                artifact_service=artifact_service,
                schema_identity=schema_identity,
            )
            self._trace("verification.completed", sampled_artifact_count=len(sampled))
            status = "dry_run_verified" if self.options.dry_run else "completed"
            report = MigrationReport(
                migration_id=migration_id,
                status=status,
                dry_run=self.options.dry_run,
                idempotent_replay=False,
                source_version=before.source_version,
                target_version=TARGET_VERSION,
                storage_schema_version=(
                    schema_identity.target_storage_schema_version
                ),
                import_layout_version=schema_identity.import_layout_version,
                target_schema_sha256=schema_identity.target_schema_sha256,
                data_generation_id=schema_identity.data_generation_id,
                source_inventory_digest=before.digest,
                counts=dict(self.counts),
                warnings=tuple(self.warnings),
                backups=tuple(self.backups),
                sampled_artifact_ids=sampled,
                quarantine_entry_count=len(self.secret_records),
                quarantine_summary=_quarantine_summary(self.secret_records),
                remaining_mappings=REMAINING_MAPPINGS,
                source_evidence=(
                    self.source_evidence.to_dict() if self.source_evidence is not None else {}
                ),
            )
            self._complete_run(database=database, report=report)
            self._verify_completed_database(database, migration_id, status)
            _write_json(staging / BACKUP_MANIFEST_NAME, {
                "schema_version": 1,
                "source_inventory_digest": before.digest,
                "source_unchanged": True,
                "backups": [item.to_dict() for item in self.backups],
            })
            _write_json(staging / REPORT_NAME, report.to_dict())
            self._write_trace(staging)

            after = self._inventory_source(source)
            if after != before:
                raise SourceChangedError("legacy source changed before migration publication")
            self._trace("source.reverified", source_inventory_digest=after.digest)
            self._write_trace(staging)
            if self.options.dry_run:
                return report
            if target.exists():
                raise TargetConflictError("v1 target appeared while migration was staging")
            (staging / ".ecorex-migration-staging").unlink(missing_ok=True)
            final_check = self._inventory_source(source)
            if final_check != before:
                raise SourceChangedError("legacy source changed at migration commit boundary")
            os.replace(staging, target)
            published = True
            return report
        except BaseException as error:
            if not published:
                self._discard_staging(staging, target)
            try:
                after_failure = self._inventory_source(source)
            except BaseException as inventory_error:
                raise SourceChangedError(
                    "legacy source could not be re-inventoried after migration failure"
                ) from inventory_error
            if after_failure != before and not isinstance(error, SourceChangedError):
                raise SourceChangedError(
                    "legacy source changed while a failed migration was staging"
                ) from error
            raise
        finally:
            if temporary is not None:
                temporary.cleanup()


def migrate_v030_to_v1(
    source_root: str | Path,
    target_root: str | Path,
    *,
    source_version: str = DEFAULT_SOURCE_VERSION,
    dry_run: bool = False,
    quarantine_key: bytes | None = None,
    conversation_database: str | Path | None = None,
    memory_database: str | Path | None = None,
    config_file: str | Path | None = None,
    mcp_file: str | Path | None = None,
    ui_state_file: str | Path | None = None,
    skills_config_file: str | Path | None = None,
    permission_file: str | Path | None = None,
    release_evidence_file: str | Path | None = None,
    baseline_root: str | Path | None = None,
    sample_size: int = 3,
) -> MigrationReport:
    """Service entry point used by installers and the future control plane."""

    return V030ToV1Migrator(
        MigrationOptions(
            source_root=source_root,
            target_root=target_root,
            source_version=source_version,
            dry_run=dry_run,
            quarantine_key=quarantine_key,
            conversation_database=conversation_database,
            memory_database=memory_database,
            config_file=config_file,
            mcp_file=mcp_file,
            ui_state_file=ui_state_file,
            skills_config_file=skills_config_file,
            permission_file=permission_file,
            release_evidence_file=release_evidence_file,
            baseline_root=baseline_root,
            sample_size=sample_size,
        )
    ).run()


def migrate_legacy_to_v1(
    source_root: str | Path,
    target_root: str | Path,
    *,
    source_version: str,
    **options: Any,
) -> MigrationReport:
    """Version-explicit entry point for supported released installations."""

    return migrate_v030_to_v1(
        source_root,
        target_root,
        source_version=source_version,
        **options,
    )
