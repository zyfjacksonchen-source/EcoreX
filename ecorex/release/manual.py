"""Fail-closed state and evidence contracts for operator-driven releases."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
from typing import Any, Mapping

SCHEMA_VERSION = 1
COW_HARD_TOOL_IDS = (
    "read", "write", "edit", "bash", "subagent", "search_files", "ls", "send",
    "evolution_undo", "env_config", "scheduler", "web_search", "web_fetch",
    "vision", "ocr", "browser", "imagegen", "memory_search", "memory_get",
)
COW_OFFICE_TOOL_IDS = (
    "office_documents", "office_pdf", "office_presentations", "office_spreadsheets",
)
BUILTIN_TOOL_IDS = COW_HARD_TOOL_IDS + COW_OFFICE_TOOL_IDS
PREPARE_STEPS = (
    "preflight",
    "local-gates",
    "platform-build",
    "candidate-build",
    "package-smoke",
    "github-draft",
    "cloud-stage",
    "site-stage",
)
FINALIZE_STEPS = (
    "github-release",
    "cloud-activation",
    "site-activation",
    "update-notification",
    "online-update",
    "codex-browser",
)
ALL_STEPS = PREPARE_STEPS + FINALIZE_STEPS
_SEMVER = re.compile(r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\Z")
_COMMIT = re.compile(r"[0-9a-f]{40}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_RUN_ID = re.compile(r"[0-9A-Za-z][0-9A-Za-z._-]{7,159}\Z")


class ManualReleaseError(RuntimeError):
    """Stable, non-sensitive operator error."""

    def __init__(self, code: str) -> None:
        if re.fullmatch(r"[a-z][a-z0-9_]{2,95}", code) is None:
            code = "manual_release_failed"
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class ReleaseSpec:
    version: str
    commit: str
    channel: str = "stable"
    from_version: str = "0.3.2"

    def __post_init__(self) -> None:
        if (
            _SEMVER.fullmatch(self.version) is None
            or _SEMVER.fullmatch(self.from_version) is None
            or _COMMIT.fullmatch(self.commit) is None
            or self.channel not in {"stable", "canary"}
            or self.version == self.from_version
        ):
            raise ManualReleaseError("release_identity_invalid")

    @property
    def run_id(self) -> str:
        return f"v{self.version}-{self.commit}"

    def to_dict(self) -> dict[str, str]:
        return {
            "version": self.version,
            "commit": self.commit,
            "channel": self.channel,
            "from_version": self.from_version,
        }


def confirmation_phrase(spec: ReleaseSpec) -> str:
    return f"PUBLISH v{spec.version}@{spec.commit[:8]} AND NOTIFY USERS"


def canonical_json(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError):
        raise ManualReleaseError("release_state_invalid") from None


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        before = path.lstat()
        if path.is_symlink() or not stat.S_ISREG(before.st_mode):
            raise OSError
        with path.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            if _identity(opened) != _identity(before):
                raise OSError
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
            after = os.fstat(stream.fileno())
        current = path.lstat()
    except OSError:
        raise ManualReleaseError("release_evidence_invalid") from None
    if {_identity(before), _identity(opened), _identity(after), _identity(current)} != {
        _identity(before)
    }:
        raise ManualReleaseError("release_evidence_changed")
    return digest.hexdigest()


def _identity(value: os.stat_result) -> tuple[int, int, int, int]:
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns


class ReleaseRunStore:
    """Atomic, resumable run ledger with a monotonic release state machine."""

    def __init__(self, root: Path, spec: ReleaseSpec) -> None:
        self.root = root.expanduser().absolute() / spec.run_id
        self.path = self.root / "run.json"
        self.spec = spec

    @classmethod
    def open(cls, root: Path, run_id: str) -> "ReleaseRunStore":
        if _RUN_ID.fullmatch(run_id) is None:
            raise ManualReleaseError("release_run_id_invalid")
        path = root.expanduser().absolute() / run_id / "run.json"
        value = _read_json(path)
        identity = value.get("release") if isinstance(value, Mapping) else None
        if not isinstance(identity, Mapping):
            raise ManualReleaseError("release_state_invalid")
        store = cls(
            root,
            ReleaseSpec(
                version=str(identity.get("version", "")),
                commit=str(identity.get("commit", "")),
                channel=str(identity.get("channel", "")),
                from_version=str(identity.get("from_version", "")),
            ),
        )
        if store.root.name != run_id:
            raise ManualReleaseError("release_state_invalid")
        store.read()
        return store

    def create(self) -> dict[str, Any]:
        if os.path.lexists(self.path):
            value = self.read()
            if value["release"] != self.spec.to_dict():
                raise ManualReleaseError("release_state_identity_conflict")
            return value
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        value = {
            "schema_version": SCHEMA_VERSION,
            "run_id": self.spec.run_id,
            "release": self.spec.to_dict(),
            "status": "created",
            "steps": {},
            "created_at": _now(),
            "updated_at": _now(),
        }
        self._write(value)
        return value

    def read(self) -> dict[str, Any]:
        value = _read_json(self.path)
        if (
            not isinstance(value, dict)
            or set(value)
            != {
                "schema_version",
                "run_id",
                "release",
                "status",
                "steps",
                "created_at",
                "updated_at",
            }
            or value.get("schema_version") != SCHEMA_VERSION
            or value.get("run_id") != self.spec.run_id
            or value.get("release") != self.spec.to_dict()
            or not isinstance(value.get("steps"), dict)
            or any(step not in ALL_STEPS for step in value["steps"])
        ):
            raise ManualReleaseError("release_state_invalid")
        for step, receipt in value["steps"].items():
            if (
                not isinstance(receipt, dict)
                or receipt.get("status") != "passed"
                or not isinstance(receipt.get("sha256"), str)
                or _SHA256.fullmatch(receipt["sha256"]) is None
                or not isinstance(receipt.get("completed_at"), str)
            ):
                raise ManualReleaseError("release_state_invalid")
        return value

    def complete(self, step: str, receipt: Mapping[str, Any]) -> dict[str, Any]:
        if step not in ALL_STEPS:
            raise ManualReleaseError("release_step_invalid")
        value = self.read() if self.path.exists() else self.create()
        steps = dict(value["steps"])
        payload = canonical_json(dict(receipt))
        digest = hashlib.sha256(payload).hexdigest()
        existing = steps.get(step)
        if existing is not None:
            if existing["sha256"] != digest:
                raise ManualReleaseError("release_step_receipt_conflict")
            return value
        position = ALL_STEPS.index(step)
        missing = [name for name in ALL_STEPS[:position] if name not in steps]
        if missing:
            raise ManualReleaseError("release_step_order_invalid")
        receipt_path = self.root / "receipts" / f"{step}.json"
        _write_new(receipt_path, payload + b"\n")
        steps[step] = {
            "status": "passed",
            "sha256": digest,
            "completed_at": _now(),
        }
        value["steps"] = steps
        value["status"] = _status(steps)
        value["updated_at"] = _now()
        self._write(value)
        return value

    def receipt(self, step: str) -> dict[str, Any]:
        if step not in self.read()["steps"]:
            raise ManualReleaseError("release_step_incomplete")
        return _read_json(self.root / "receipts" / f"{step}.json")

    def _write(self, value: Mapping[str, Any]) -> None:
        payload = canonical_json(value) + b"\n"
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor, temporary = tempfile.mkstemp(
            prefix=".run.", suffix=".json", dir=self.root
        )
        try:
            if fchmod := getattr(os, "fchmod", None):
                fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


def validate_codex_browser_receipt(
    value: Mapping[str, Any],
    *,
    spec: ReleaseSpec,
    run_id: str,
    nonce: str,
    evidence_root: Path,
) -> dict[str, Any]:
    """Validate the browser-tool handoff before a release can become complete."""

    required = {
        "schema_version",
        "executor",
        "status",
        "run_id",
        "nonce",
        "version",
        "installed_url",
        "public_url",
        "checks",
        "observations",
        "evidence",
        "completed_at",
    }
    if (
        not isinstance(value, Mapping)
        or set(value) != required
        or value.get("schema_version") != 1
        or value.get("executor") != "codex-browser-automation"
        or value.get("status") != "passed"
        or value.get("run_id") != run_id
        or value.get("nonce") != nonce
        or value.get("version") != spec.version
        or value.get("installed_url") != "http://127.0.0.1:8765/"
        or value.get("public_url") != "https://dl.ecoremedia.net/ecorex-agent/"
    ):
        raise ManualReleaseError("codex_browser_receipt_invalid")
    checks = value.get("checks")
    required_checks = {
        "installed_runtime_version",
        "visible_ui_version",
        "release_specific_change",
        "authenticated_user_session",
        "production_model_streaming",
        "stream_event_ordering",
        "production_tool_lifecycle",
        "all_builtin_capabilities",
        "process_folding",
        "dynamic_component_terminal_state",
        "scroll_follow",
        "long_session_virtualization",
        "production_image_calls",
        "skill_mcp_runtime_projection",
        "update_notification_handoff",
        "interaction_round_trip",
        "public_download_version",
    }
    if (
        not isinstance(checks, Mapping)
        or set(checks) != required_checks
        or any(result is not True for result in checks.values())
    ):
        raise ManualReleaseError("codex_browser_checks_incomplete")
    observations = value.get("observations")
    if not _valid_browser_observations(observations, spec=spec, nonce=nonce):
        raise ManualReleaseError("codex_browser_observations_invalid")
    evidence = value.get("evidence")
    if not isinstance(evidence, list) or not 10 <= len(evidence) <= 32:
        raise ManualReleaseError("codex_browser_evidence_invalid")
    root = evidence_root.expanduser().resolve(strict=True)
    normalized: list[dict[str, Any]] = []
    for item in evidence:
        if not isinstance(item, Mapping) or set(item) != {"path", "sha256"}:
            raise ManualReleaseError("codex_browser_evidence_invalid")
        relative = Path(str(item.get("path", "")))
        if relative.is_absolute() or ".." in relative.parts or not relative.parts:
            raise ManualReleaseError("codex_browser_evidence_invalid")
        path = (root / relative).resolve(strict=True)
        try:
            path.relative_to(root)
        except ValueError:
            raise ManualReleaseError("codex_browser_evidence_invalid") from None
        digest = sha256_file(path)
        if item.get("sha256") != digest:
            raise ManualReleaseError("codex_browser_evidence_digest_mismatch")
        normalized.append({"path": relative.as_posix(), "sha256": digest})
    return {**dict(value), "evidence": normalized}


def _valid_browser_observations(
    value: Any, *, spec: ReleaseSpec, nonce: str
) -> bool:
    if not isinstance(value, Mapping) or set(value) != {
        "versions",
        "model_stream",
        "tool_call",
        "builtin_capabilities",
        "image_calls",
        "long_session",
        "runtime_projection",
        "release_change",
    }:
        return False
    versions = value.get("versions")
    model = value.get("model_stream")
    tool = value.get("tool_call")
    builtins = value.get("builtin_capabilities")
    image = value.get("image_calls")
    session = value.get("long_session")
    projection = value.get("runtime_projection")
    change = value.get("release_change")
    def text(item: Any) -> bool:
        return isinstance(item, str) and 1 <= len(item) <= 256

    def count(item: Any) -> bool:
        return not isinstance(item, bool) and isinstance(item, int)

    return bool(
        isinstance(versions, Mapping)
        and set(versions)
        == {"installed_runtime", "visible_ui", "public_manifest", "admin_service"}
        and all(item == spec.version for item in versions.values())
        and isinstance(model, Mapping)
        and set(model)
        == {
            "prompt_nonce",
            "request_id",
            "model_id",
            "reasoning_effort",
            "incremental_frame_count",
            "terminal_frame_count",
            "answer_contains_nonce",
        }
        and model.get("prompt_nonce") == nonce
        and text(model.get("request_id"))
        and model.get("model_id") == "ecorex-chat"
        and model.get("reasoning_effort") == "max"
        and count(model.get("incremental_frame_count"))
        and model["incremental_frame_count"] >= 2
        and model.get("terminal_frame_count") == 1
        and model.get("answer_contains_nonce") is True
        and isinstance(tool, Mapping)
        and set(tool)
        == {"tool_call_id", "status_sequence", "terminal_result_present", "read_only"}
        and text(tool.get("tool_call_id"))
        and tool.get("status_sequence") == ["pending", "running", "completed"]
        and tool.get("terminal_result_present") is True
        and tool.get("read_only") is True
        and isinstance(builtins, Mapping)
        and set(builtins) == set(BUILTIN_TOOL_IDS)
        and all(
            isinstance(item, Mapping)
            and set(item)
            == {
                "status", "tool_call_id", "executor", "tool_result_present",
                "terminal_visible", "next_turn_referenced",
            }
            and text(item.get("tool_call_id"))
            and text(item.get("executor"))
            and item.get("tool_result_present") is True
            and item.get("terminal_visible") is True
            and item.get("next_turn_referenced") is True
            and item.get("status") == "completed"
            for item in builtins.values()
        )
        and isinstance(image, Mapping)
        and set(image)
        == {
            "requested_model_id", "actual_model_id", "tool_call_ids", "artifact_ids",
            "completed_count", "single_artifact_per_call", "context_continued",
        }
        and image.get("requested_model_id") == "gpt-image-2"
        and image.get("actual_model_id") == "gpt-image-2-pro"
        and isinstance(image.get("tool_call_ids"), list)
        and all(text(item) for item in image["tool_call_ids"])
        and len(image["tool_call_ids"]) == len(set(image["tool_call_ids"])) == 2
        and isinstance(image.get("artifact_ids"), list)
        and all(text(item) for item in image["artifact_ids"])
        and len(image["artifact_ids"]) == len(set(image["artifact_ids"])) == 2
        and image.get("completed_count") == 2
        and image.get("single_artifact_per_call") is True
        and image.get("context_continued") is True
        and isinstance(session, Mapping)
        and set(session)
        == {"turn_count", "mounted_turn_count", "history_restored", "jump_to_latest", "follow_pause_resume"}
        and count(session.get("turn_count"))
        and session["turn_count"] >= 100
        and count(session.get("mounted_turn_count"))
        and 0 < session["mounted_turn_count"] < session["turn_count"]
        and session.get("history_restored") is True
        and session.get("jump_to_latest") is True
        and session.get("follow_pause_resume") is True
        and isinstance(projection, Mapping)
        and set(projection)
        == {"skill_count", "mcp_count", "mcp_status", "matches_runtime_api"}
        and count(projection.get("skill_count"))
        and projection["skill_count"] >= 1
        and count(projection.get("mcp_count"))
        and projection["mcp_count"] == 0
        and projection.get("mcp_status") == "unconfigured"
        and projection.get("matches_runtime_api") is True
        and isinstance(change, Mapping)
        and set(change) == {"assertion", "matched"}
        and text(change.get("assertion"))
        and change.get("matched") is True
    )


def browser_request(store: ReleaseRunStore) -> dict[str, Any]:
    value = store.read()
    if value["status"] != "awaiting-browser-verification":
        raise ManualReleaseError("codex_browser_verification_not_ready")
    notification = store.receipt("update-notification")
    nonce = notification.get("browser_nonce")
    if not isinstance(nonce, str) or _SHA256.fullmatch(nonce) is None:
        raise ManualReleaseError("codex_browser_nonce_invalid")
    return {
        "schema_version": 1,
        "executor": "codex-browser-automation",
        "run_id": store.spec.run_id,
        "nonce": nonce,
        "expected_version": store.spec.version,
        "installed_url": "http://127.0.0.1:8765/",
        "public_url": "https://dl.ecoremedia.net/ecorex-agent/",
        "required_checks": [
            "installed_runtime_version",
            "visible_ui_version",
            "release_specific_change",
            "authenticated_user_session",
            "production_model_streaming",
            "stream_event_ordering",
            "production_tool_lifecycle",
            "all_builtin_capabilities",
            "process_folding",
            "dynamic_component_terminal_state",
            "scroll_follow",
            "long_session_virtualization",
            "production_image_calls",
            "skill_mcp_runtime_projection",
            "update_notification_handoff",
            "interaction_round_trip",
            "public_download_version",
        ],
        "production_scenarios": [
            {
                "id": "model-stream",
                "action": "send a nonce-bound prompt through the production model",
                "assert": "incremental stream events precede one exact terminal answer",
            },
            {
                "id": "tool-lifecycle",
                "action": "request one harmless read-only production tool call",
                "assert": "pending, running, folded process and terminal result render once",
            },
            {
                "id": "builtin-matrix",
                "action": "exercise every built-in tool through the authenticated production UI",
                "assert": "all Cow hard tools and all four Office tools complete with a visible terminal state",
            },
            {
                "id": "image-calls",
                "action": "invoke imagegen twice through the production UI",
                "assert": "each Cow tool call yields one real Artifact and the second call keeps context",
            },
            {
                "id": "long-session",
                "action": "exercise a conversation beyond the virtualization threshold",
                "assert": "scroll follow, jump-to-latest and historical restoration remain stable",
            },
            {
                "id": "runtime-management",
                "action": "inspect Skill and MCP runtime projections through visible product UI",
                "assert": "installed/enabled state matches the production Runtime response",
            },
        ],
        "receipt_observations": {
            "versions": "installed Runtime, visible UI, public manifest and admin service must all equal expected_version",
            "model_stream": "record nonce, ecorex-chat/max request identity, at least two incremental frames and one terminal frame",
            "tool_call": "record a read-only call id and exact pending/running/completed sequence",
            "builtin_capabilities": "record tool call, executor, result, visible terminal state and next-turn reference for every required_builtin_tool_id",
            "image_calls": "record two independent gpt-image-2 calls, gpt-image-2-pro routing, one Artifact each and context continuity",
            "long_session": "record at least 100 turns, fewer mounted turns, restore, jump and follow pause/resume",
            "runtime_projection": "record visible Skill/MCP counts and equality with Runtime API",
            "release_change": "name and match one release-specific behavior",
        },
        "required_builtin_tool_ids": list(BUILTIN_TOOL_IDS),
        "token_policy": "real production model and image calls are required; user token consumption is authorized",
    }


def _status(steps: Mapping[str, Any]) -> str:
    completed = set(steps)
    if completed == set(ALL_STEPS):
        return "complete"
    if set(PREPARE_STEPS).issubset(completed) and not completed.intersection(
        FINALIZE_STEPS
    ):
        return "awaiting-user-confirmation"
    if "online-update" in completed and "codex-browser" not in completed:
        return "awaiting-browser-verification"
    if completed.intersection(FINALIZE_STEPS):
        return "finalizing"
    return "preparing"


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = path.read_bytes()
        if not 1 <= len(payload) <= 4 * 1024 * 1024:
            raise OSError
        value = json.loads(payload.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise ManualReleaseError("release_state_invalid") from None
    if not isinstance(value, dict):
        raise ManualReleaseError("release_state_invalid")
    return value


def _write_new(path: Path, payload: bytes) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except OSError:
        raise ManualReleaseError("release_receipt_write_failed") from None


def _now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


__all__ = [
    "ALL_STEPS",
    "BUILTIN_TOOL_IDS",
    "COW_HARD_TOOL_IDS",
    "COW_OFFICE_TOOL_IDS",
    "FINALIZE_STEPS",
    "ManualReleaseError",
    "PREPARE_STEPS",
    "ReleaseRunStore",
    "ReleaseSpec",
    "browser_request",
    "canonical_json",
    "confirmation_phrase",
    "sha256_file",
    "validate_codex_browser_receipt",
]
