"""Resumable administrator CLI for candidate-to-rollout publication."""

from __future__ import annotations

import argparse
import base64
from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat as stat_module
import sys
from typing import Any, Callable, Mapping
from urllib.parse import quote, urlsplit

from ecorex.update import ReleaseChannel, ReleaseManifest
from ecorex.update import (
    Ed25519SignatureVerifier,
    verify_artifact_file,
    verify_artifact_signature,
    verify_manifest_signature,
    SignatureEnvelope,
)
from ecorex.release import (
    PUBLIC_BOOTSTRAP_INDEX_FILE_NAME,
    EnvironmentGitHubCredential,
    EnvironmentPublicBootstrapCredential,
    EnvironmentReplicaCredential,
    DigestPinnedExternalSigner,
    GitHubReleasePublisher,
    HTTPSReadThroughReleaseMirror,
    HTTPSReleaseReplicaPublisher,
    HTTPSPublicBootstrapIndexPublisher,
    PublicBootstrapStageReceipt,
    ReleaseSigner,
    ReleaseAssetPublicationCoordinator,
    build_public_bootstrap_index,
    stable_pointer_sequence,
    write_public_bootstrap_index,
    gate_bundle_sha256,
    validate_signed_gate_bundle,
)
from ecorex.update.locking import ProductFileLock

from .client import AdminControlPlaneClient, EnvironmentAdminCredential
from .repository import required_release_gates


MAX_MANIFEST_BYTES = 16 * 1024 * 1024
MAX_EVIDENCE_BYTES = 2 * 1024 * 1024
_PUBLICATION_GATES = frozenset({"github-release", "mirror-sync", "cdn-sync"})
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SAFE_LOCAL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_PUBLICATION_EVIDENCE = re.compile(r"^publication-receipt:sha256:[0-9a-f]{64}$")
_BOOTSTRAP_INDEX_EVIDENCE = re.compile(
    r"^bootstrap-index-stage-receipt:sha256:[0-9a-f]{64}$"
)
_FRESHNESS_TIME = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$"
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ecorex-release")
    parser.add_argument("--endpoint", default=os.getenv("ECOREX_CONTROL_PLANE_URL"))
    parser.add_argument(
        "--allowed-host",
        action="append",
        default=None,
        help="explicit Control Plane host allowlist; repeat for multiple hosts",
    )
    parser.add_argument("--token-env", default="ECOREX_CONTROL_PLANE_TOKEN")
    commands = parser.add_subparsers(dest="command", required=True)

    promote = commands.add_parser(
        "promote", help="publish and optionally activate one release"
    )
    promote.add_argument("--manifest", required=True, type=Path)
    promote.add_argument("--evidence", required=True, type=Path)
    promote.add_argument(
        "--trusted-key",
        action="append",
        required=True,
        metavar="KEY_ID=BASE64_PUBLIC_KEY",
    )
    promote.add_argument("--publication-receipt", type=Path)
    promote.add_argument("--bootstrap-index-receipt", type=Path)
    promote.add_argument(
        "--phase", choices=("prepare", "finalize", "auto"), default="auto"
    )
    promote.add_argument("--journal", required=True, type=Path)
    promote.add_argument("--percentage", type=int, default=1)
    promote.add_argument("--organization", action="append", default=[])
    promote.add_argument("--account", action="append", default=[])
    promote.add_argument("--minimum-compatible-version")
    promote.add_argument("--activate", action="store_true")
    promote.add_argument("--dry-run", action="store_true")

    action = commands.add_parser("rollout", help="activate, pause or halt a rollout")
    action.add_argument("rollout_id")
    action.add_argument("action", choices=("activate", "pause", "halt"))

    switch = commands.add_parser(
        "kill-switch", help="set or clear a channel kill switch"
    )
    switch.add_argument("channel", choices=("canary", "stable"))
    switch.add_argument("state", choices=("set", "clear"))

    commands.add_parser(
        "distribution", help="show live client version/update distribution"
    )
    commands.add_parser(
        "bootstrap-freshness-status",
        help="show durable automatic public-pointer freshness health",
    )
    refresh_freshness = commands.add_parser(
        "refresh-bootstrap-freshness",
        help="request one idempotent same-authority freshness refresh now",
    )
    refresh_freshness.add_argument(
        "--client-request-id",
        help=(
            "stable retry identity; defaults to a deterministic identity bound "
            "to the current active freshness expiry"
        ),
    )
    refresh_freshness.add_argument(
        "--request-journal",
        type=Path,
        help="durable pending-request journal path (defaults to the user state directory)",
    )

    upload = commands.add_parser(
        "upload-github",
        help="verify one built release and resumably upload its exact bytes",
    )
    upload.add_argument("--release-dir", required=True, type=Path)
    upload.add_argument("--owner", required=True)
    upload.add_argument("--repository", required=True)
    upload.add_argument(
        "--trusted-key",
        action="append",
        required=True,
        metavar="KEY_ID=BASE64_PUBLIC_KEY",
    )
    upload.add_argument("--github-token-env", default="ECOREX_GITHUB_TOKEN")
    publish_assets = commands.add_parser(
        "publish-assets",
        help=(
            "verify once, publish GitHub and CDN, then verify the signed "
            "domestic read-through mirror"
        ),
    )
    publish_assets.add_argument("--release-dir", required=True, type=Path)
    publish_assets.add_argument("--publication-config", required=True, type=Path)
    publish_assets.add_argument("--receipt", required=True, type=Path)
    publish_assets.add_argument(
        "--trusted-key",
        action="append",
        required=True,
        metavar="KEY_ID=BASE64_PUBLIC_KEY",
    )
    publish_assets.add_argument(
        "--publish-github",
        action="store_true",
        help="make GitHub public only after both signed replicas are ready",
    )
    public_index = commands.add_parser(
        "build-public-bootstrap-index",
        help=(
            "verify a fully published stable release and atomically generate "
            "its untrusted public Bootstrap discovery index"
        ),
    )
    public_index.add_argument("--release-dir", required=True, type=Path)
    public_index.add_argument("--publication-receipt", required=True, type=Path)
    public_index.add_argument(
        "--output",
        required=True,
        type=Path,
        help=f"atomic output path named {PUBLIC_BOOTSTRAP_INDEX_FILE_NAME}",
    )
    public_index.add_argument(
        "--trusted-key",
        action="append",
        required=True,
        metavar="KEY_ID=BASE64_PUBLIC_KEY",
    )
    public_index.add_argument(
        "--trusted-publication-key",
        action="append",
        required=True,
        metavar="KEY_ID=BASE64_PUBLIC_KEY",
    )
    stage_index = commands.add_parser(
        "stage-public-bootstrap-index",
        help=(
            "independently verify and stage the stable Bootstrap discovery "
            "index without changing the public pointer"
        ),
    )
    stage_index.add_argument("--release-dir", required=True, type=Path)
    stage_index.add_argument("--publication-receipt", required=True, type=Path)
    stage_index.add_argument("--index", required=True, type=Path)
    stage_index.add_argument("--publication-config", required=True, type=Path)
    stage_index.add_argument("--receipt", required=True, type=Path)
    stage_index.add_argument(
        "--trusted-key",
        action="append",
        required=True,
        metavar="KEY_ID=BASE64_PUBLIC_KEY",
    )
    stage_index.add_argument(
        "--trusted-publication-key",
        action="append",
        required=True,
        metavar="KEY_ID=BASE64_PUBLIC_KEY",
    )
    activate_index = commands.add_parser(
        "activate-public-bootstrap-index",
        help=(
            "CAS-activate an exact staged stable Bootstrap index and publicly "
            "read it back after Control Plane activation"
        ),
    )
    activate_index.add_argument("--release-dir", required=True, type=Path)
    activate_index.add_argument("--publication-receipt", required=True, type=Path)
    activate_index.add_argument("--index", required=True, type=Path)
    activate_index.add_argument("--stage-receipt", required=True, type=Path)
    activate_index.add_argument("--publication-config", required=True, type=Path)
    activate_index.add_argument("--receipt", required=True, type=Path)
    activate_index.add_argument(
        "--trusted-key",
        action="append",
        required=True,
        metavar="KEY_ID=BASE64_PUBLIC_KEY",
    )
    activate_index.add_argument(
        "--trusted-publication-key",
        action="append",
        required=True,
        metavar="KEY_ID=BASE64_PUBLIC_KEY",
    )
    return parser


def _read_json(path: Path, *, limit: int, label: str) -> Any:
    payload = _read_bounded_file_bytes(path, limit=limit, label=label)
    try:
        return json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        raise ValueError(f"{label} is invalid JSON") from None


def _read_bounded_file_bytes(path: Path, *, limit: int, label: str) -> bytes:
    """Read one regular file through a stable descriptor with a hard bound."""

    raw = path.expanduser()
    try:
        metadata = raw.lstat()
    except OSError as error:
        raise ValueError(
            f"{label} cannot be read: {error.__class__.__name__}"
        ) from None
    reparse = getattr(stat_module, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    if (
        stat_module.S_ISLNK(metadata.st_mode)
        or bool(getattr(metadata, "st_file_attributes", 0) & reparse)
        or not stat_module.S_ISREG(metadata.st_mode)
        or not 1 <= metadata.st_size <= limit
    ):
        raise ValueError(f"{label} is empty or exceeds its size limit")
    resolved = raw.resolve(strict=True)
    try:
        with resolved.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            if _stat_identity(opened) != _stat_identity(metadata):
                raise ValueError(f"{label} changed while opening")
            payload = stream.read(limit + 1)
            after = os.fstat(stream.fileno())
    except ValueError:
        raise
    except OSError as error:
        raise ValueError(
            f"{label} cannot be read: {error.__class__.__name__}"
        ) from None
    if (
        _stat_identity(after) != _stat_identity(opened)
        or not 1 <= len(payload) <= limit
    ):
        raise ValueError(f"{label} changed while reading or exceeds its size limit")
    return payload


class PromotionJournal:
    def __init__(
        self,
        path: Path,
        release_id: str,
        manifest_sha256: str,
        publication_evidence: str,
        rollout_target_sha256: str,
        prepare_evidence_sha256: str,
        final_evidence_sha256: str | None,
    ) -> None:
        self.path = path.expanduser().resolve()
        self.release_id = release_id
        self.manifest_sha256 = manifest_sha256
        self.publication_evidence = publication_evidence
        self.rollout_target_sha256 = rollout_target_sha256
        self.prepare_evidence_sha256 = prepare_evidence_sha256
        self.final_evidence_sha256 = final_evidence_sha256
        self.data = self._load_or_create()

    def _load_or_create(self) -> dict[str, Any]:
        if not self.path.exists():
            data = {
                "schema_version": 4,
                "release_id": self.release_id,
                "manifest_sha256": self.manifest_sha256,
                "publication_evidence": self.publication_evidence,
                "rollout_target_sha256": self.rollout_target_sha256,
                "prepare_evidence_sha256": self.prepare_evidence_sha256,
                "final_evidence_sha256": self.final_evidence_sha256,
                "request_ids": {},
                "rollout_id": None,
                "activated": False,
            }
            self._write(data)
            return data
        value = _read_json(self.path, limit=1024 * 1024, label="promotion journal")
        expected_steps = {
            "candidate.create",
            "release.publish",
            "rollout.create",
            "rollout.activate",
            "gate-bundle.prepare",
            "gate-bundle.finalize",
        }
        request_ids = value.get("request_ids") if isinstance(value, dict) else None
        rollout_id = value.get("rollout_id") if isinstance(value, dict) else None
        if (
            not isinstance(value, dict)
            or set(value)
            != {
                "schema_version",
                "release_id",
                "manifest_sha256",
                "publication_evidence",
                "rollout_target_sha256",
                "prepare_evidence_sha256",
                "final_evidence_sha256",
                "request_ids",
                "rollout_id",
                "activated",
            }
            or value.get("schema_version") != 4
            or value.get("release_id") != self.release_id
            or value.get("manifest_sha256") != self.manifest_sha256
            or value.get("publication_evidence") != self.publication_evidence
            or value.get("rollout_target_sha256") != self.rollout_target_sha256
            or _SHA256.fullmatch(str(value.get("rollout_target_sha256"))) is None
            or value.get("prepare_evidence_sha256") != self.prepare_evidence_sha256
            or not isinstance(value.get("prepare_evidence_sha256"), str)
            or _SHA256.fullmatch(value["prepare_evidence_sha256"]) is None
            or (
                value.get("final_evidence_sha256") is not None
                and (
                    not isinstance(value.get("final_evidence_sha256"), str)
                    or _SHA256.fullmatch(value["final_evidence_sha256"]) is None
                )
            )
            or _PUBLICATION_EVIDENCE.fullmatch(self.publication_evidence) is None
            or not isinstance(request_ids, dict)
            or not set(request_ids).issubset(expected_steps)
            or any(
                not isinstance(request_id, str)
                or re.fullmatch(r"release_[0-9a-f]{32}", request_id) is None
                for request_id in request_ids.values()
            )
            or (
                rollout_id is not None
                and (
                    not isinstance(rollout_id, str)
                    or _SAFE_LOCAL_ID.fullmatch(rollout_id) is None
                )
            )
            or not isinstance(value.get("activated"), bool)
            or (value.get("activated") and rollout_id is None)
        ):
            raise ValueError("promotion journal does not match this release manifest")
        stored_final = value.get("final_evidence_sha256")
        if self.final_evidence_sha256 is not None:
            if stored_final not in {None, self.final_evidence_sha256}:
                raise ValueError("promotion journal final evidence changed")
            if stored_final is None:
                value["final_evidence_sha256"] = self.final_evidence_sha256
                self._write(value)
        return value

    def request_id(self, step: str) -> str:
        request_ids = self.data["request_ids"]
        existing = request_ids.get(step)
        if isinstance(existing, str) and existing:
            return existing
        # The journal is an optimization, not the sole idempotency authority.
        # Derive the same request ID after a runner loss or workflow rerun so
        # Control Plane replay still converges instead of creating a second
        # rollout with fresh randomness.
        request_id = "release_" + hashlib.sha256(
            (
                self.release_id
                + "\0"
                + self.manifest_sha256
                + "\0"
                + self.publication_evidence
                + "\0"
                + self.rollout_target_sha256
                + "\0"
                + self.prepare_evidence_sha256
                + "\0"
                + step
            ).encode("utf-8")
        ).hexdigest()[:32]
        request_ids[step] = request_id
        self._write(self.data)
        return request_id

    def record_rollout(self, rollout_id: str) -> None:
        if (
            not isinstance(rollout_id, str)
            or _SAFE_LOCAL_ID.fullmatch(rollout_id) is None
        ):
            raise ValueError("promotion journal rollout identity is invalid")
        existing = self.data.get("rollout_id")
        if existing not in {None, rollout_id}:
            raise ValueError("promotion journal rollout identity changed")
        self.data["rollout_id"] = rollout_id
        self._write(self.data)

    def record_activated(self) -> None:
        self.data["activated"] = True
        self._write(self.data)

    def _write(self, value: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(self.path.name + ".tmp-" + secrets.token_hex(8))
        payload = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        try:
            with temporary.open("xb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            _durable_replace(temporary, self.path)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


class FreshnessRequestJournal:
    """One local pending identity retained across ambiguous network outcomes."""

    _PURPOSE = "bootstrap-freshness.manual-refresh"
    _MAX_AUDIT_EVENTS = 64

    def __init__(
        self,
        path: Path,
        *,
        endpoint: str,
        authority_sha256: str | None,
    ) -> None:
        if authority_sha256 is not None and _SHA256.fullmatch(authority_sha256) is None:
            raise ValueError("active Bootstrap authority identity is invalid")
        self.path = path.expanduser().resolve()
        self.endpoint_sha256 = hashlib.sha256(endpoint.encode("utf-8")).hexdigest()
        self.authority_sha256 = authority_sha256
        self.data = self._load_or_create()

    def pending_request_id(self) -> str:
        pending = self.data["pending"]
        if isinstance(pending, dict):
            return str(pending["request_id"])
        nonce = secrets.token_bytes(32)
        request_id = (
            "release_"
            + hashlib.sha256(
                b"ecorex.manual-bootstrap-freshness-request.v1\0"
                + self.endpoint_sha256.encode("ascii")
                + b"\0"
                + (self.authority_sha256 or "no-active-pointer").encode("ascii")
                + b"\0"
                + self._PURPOSE.encode("ascii")
                + b"\0"
                + nonce
            ).hexdigest()[:32]
        )
        self.data["pending"] = {
            "request_id": request_id,
            "created_at": _utc_timestamp(),
        }
        self._audit("created", request_id)
        self._write()
        return request_id

    def complete(self, request_id: str) -> None:
        pending = self.data["pending"]
        if not isinstance(pending, dict) or pending.get("request_id") != request_id:
            raise ValueError("freshness request journal completion is inconsistent")
        self._audit("completed", request_id)
        self.data["pending"] = None
        self._write()

    def _load_or_create(self) -> dict[str, Any]:
        if not self.path.exists():
            data = self._new_data()
            self.data = data
            self._write()
            return data
        value = _read_json(
            self.path,
            limit=256 * 1024,
            label="Bootstrap freshness request journal",
        )
        if not self._valid(value):
            raise ValueError("Bootstrap freshness request journal is invalid")
        assert isinstance(value, dict)
        changed = (
            value["endpoint_sha256"] != self.endpoint_sha256
            or value["authority_sha256"] != self.authority_sha256
        )
        if changed:
            pending = value["pending"]
            if isinstance(pending, dict):
                self.data = value
                self._audit("invalidated-target-change", str(pending["request_id"]))
                value["pending"] = None
            value["endpoint_sha256"] = self.endpoint_sha256
            value["authority_sha256"] = self.authority_sha256
            self.data = value
            self._write()
        return value

    def _new_data(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "purpose": self._PURPOSE,
            "endpoint_sha256": self.endpoint_sha256,
            "authority_sha256": self.authority_sha256,
            "pending": None,
            "audit": [],
            "updated_at": _utc_timestamp(),
        }

    def _valid(self, value: Any) -> bool:
        if (
            not isinstance(value, dict)
            or set(value)
            != {
                "schema_version",
                "purpose",
                "endpoint_sha256",
                "authority_sha256",
                "pending",
                "audit",
                "updated_at",
            }
            or value.get("schema_version") != 1
            or value.get("purpose") != self._PURPOSE
            or _SHA256.fullmatch(str(value.get("endpoint_sha256"))) is None
            or (
                value.get("authority_sha256") is not None
                and _SHA256.fullmatch(str(value.get("authority_sha256"))) is None
            )
            or not isinstance(value.get("audit"), list)
            or len(value["audit"]) > self._MAX_AUDIT_EVENTS
            or not isinstance(value.get("updated_at"), str)
        ):
            return False
        pending = value.get("pending")
        if pending is not None and (
            not isinstance(pending, dict)
            or set(pending) != {"request_id", "created_at"}
            or _SAFE_LOCAL_ID.fullmatch(str(pending.get("request_id"))) is None
            or not isinstance(pending.get("created_at"), str)
        ):
            return False
        return all(
            isinstance(event, dict)
            and set(event) == {"event", "request_id", "at"}
            and event.get("event")
            in {"created", "completed", "invalidated-target-change"}
            and _SAFE_LOCAL_ID.fullmatch(str(event.get("request_id"))) is not None
            and isinstance(event.get("at"), str)
            for event in value["audit"]
        )

    def _audit(self, event: str, request_id: str) -> None:
        events = self.data["audit"]
        events.append(
            {"event": event, "request_id": request_id, "at": _utc_timestamp()}
        )
        if len(events) > self._MAX_AUDIT_EVENTS:
            del events[: len(events) - self._MAX_AUDIT_EVENTS]

    def _write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.data["updated_at"] = _utc_timestamp()
        temporary = self.path.with_name(self.path.name + ".tmp-" + secrets.token_hex(8))
        payload = json.dumps(
            self.data,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        try:
            with temporary.open("xb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            _durable_replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)


def _utc_timestamp() -> str:
    return datetime.now(UTC).isoformat()


ClientFactory = Callable[[argparse.Namespace], AdminControlPlaneClient]
GitHubPublisherFactory = Callable[[argparse.Namespace], GitHubReleasePublisher]
PublicationCoordinatorFactory = Callable[
    [argparse.Namespace], ReleaseAssetPublicationCoordinator
]
PublicBootstrapPublisherFactory = Callable[
    [argparse.Namespace], HTTPSPublicBootstrapIndexPublisher
]
PublicPointerSignerFactory = Callable[[argparse.Namespace], ReleaseSigner]
PublicFreshnessSignerFactory = Callable[[argparse.Namespace], ReleaseSigner]


@dataclass(frozen=True, slots=True)
class VerifiedReleaseDirectory:
    manifest: ReleaseManifest
    manifest_bytes: bytes
    files: tuple[Path, ...]
    expected_sha256: Mapping[str, str]


def _client(args: argparse.Namespace) -> AdminControlPlaneClient:
    if not args.endpoint:
        raise ValueError("--endpoint or ECOREX_CONTROL_PLANE_URL is required")
    hosts = args.allowed_host or [
        host.strip()
        for host in os.getenv("ECOREX_CONTROL_PLANE_HOSTS", "").split(",")
        if host.strip()
    ]
    return AdminControlPlaneClient(
        args.endpoint,
        credentials=EnvironmentAdminCredential(os.environ, args.token_env),
        allowed_hosts=frozenset(hosts),
    )


def _explicit_freshness_request_id(args: argparse.Namespace) -> str | None:
    explicit = args.client_request_id
    if explicit is None:
        return None
    if _SAFE_LOCAL_ID.fullmatch(explicit) is None:
        raise ValueError("manual freshness client request identity is invalid")
    return explicit


def _freshness_request_journal_path(args: argparse.Namespace) -> Path:
    if args.request_journal is not None:
        return args.request_journal.expanduser().resolve()
    override = os.environ.get("ECOREX_RELEASE_STATE_DIRECTORY")
    if override:
        root = Path(override).expanduser()
        if not root.is_absolute() or "\x00" in override:
            raise ValueError("release state directory must be absolute")
        root = root.resolve()
    elif os.name == "nt" and os.environ.get("LOCALAPPDATA"):
        root = Path(os.environ["LOCALAPPDATA"]) / "EcoreX" / "release-state"
    else:
        root = Path.home() / ".ecorex" / "release-state"
    return root / "bootstrap-freshness-request.json"


def _manual_freshness_refresh(
    args: argparse.Namespace,
    client: AdminControlPlaneClient,
) -> dict[str, Any]:
    explicit = _explicit_freshness_request_id(args)
    if explicit is not None:
        return client.refresh_bootstrap_freshness(
            client_request_id=explicit
        ).model_dump(mode="json")
    journal_path = _freshness_request_journal_path(args)
    journal_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = journal_path.with_suffix(journal_path.suffix + ".lock")
    with ProductFileLock(lock_path, timeout=0):
        status = client.bootstrap_freshness_status()
        if (
            status.active_expires_at is not None
            and status.active_authority_sha256 is None
        ):
            raise ValueError("Control Plane omitted the active Bootstrap authority")
        journal = FreshnessRequestJournal(
            journal_path,
            endpoint=str(args.endpoint),
            authority_sha256=status.active_authority_sha256,
        )
        request_id = journal.pending_request_id()
        response = client.refresh_bootstrap_freshness(client_request_id=request_id)
        if response.run_state in {"succeeded", "not-due", "no-active"}:
            journal.complete(request_id)
        return response.model_dump(mode="json")


def _github_publisher(args: argparse.Namespace) -> GitHubReleasePublisher:
    return GitHubReleasePublisher(
        owner=args.owner,
        repository=args.repository,
        credentials=EnvironmentGitHubCredential(variable=args.github_token_env),
    )


def _publication_coordinator(
    args: argparse.Namespace,
) -> ReleaseAssetPublicationCoordinator:
    value = _read_json(
        args.publication_config,
        limit=1024 * 1024,
        label="release publication config",
    )
    if not isinstance(value, dict) or set(value) != {
        "schema_version",
        "github",
        "mirror",
        "cdn",
    }:
        raise ValueError("release publication config has an invalid shape")
    if value.get("schema_version") != 1:
        raise ValueError("release publication config schema is unsupported")
    github_value = value.get("github")
    if not isinstance(github_value, dict) or set(github_value) != {
        "owner",
        "repository",
        "token_env",
    }:
        raise ValueError("GitHub publication config has an invalid shape")

    def replica_value(
        label: str,
    ) -> tuple[str, str, frozenset[str], frozenset[str], str]:
        item = value.get(label)
        if not isinstance(item, dict) or set(item) != {
            "source_id",
            "endpoint",
            "allowed_hosts",
            "public_hosts",
            "token_env",
        }:
            raise ValueError(f"{label} publication config has an invalid shape")
        allowed = _strict_hosts(item.get("allowed_hosts"), f"{label} allowed hosts")
        public = _strict_hosts(item.get("public_hosts"), f"{label} public hosts")
        string_values = (
            item.get("source_id"),
            item.get("endpoint"),
            item.get("token_env"),
        )
        if not all(isinstance(part, str) and part for part in string_values):
            raise ValueError(f"{label} publication config contains an invalid value")
        return string_values[0], string_values[1], allowed, public, string_values[2]

    mirror_value = value.get("mirror")
    mirror_read_through = False
    if isinstance(mirror_value, dict) and set(mirror_value) == {
        "source_id",
        "mode",
        "public_hosts",
    }:
        mirror_source_id = mirror_value.get("source_id")
        if (
            not isinstance(mirror_source_id, str)
            or not mirror_source_id
            or mirror_value.get("mode") != "github-read-through"
        ):
            raise ValueError("mirror publication config contains an invalid value")
        mirror_public_hosts = _strict_hosts(
            mirror_value.get("public_hosts"), "mirror public hosts"
        )
        mirror_args: tuple[
            str, str | None, frozenset[str], frozenset[str], str | None
        ] = (
            mirror_source_id,
            None,
            frozenset(),
            mirror_public_hosts,
            None,
        )
        mirror_read_through = True
    else:
        mirror_args = replica_value("mirror")
    cdn_args = replica_value("cdn")
    if mirror_args[0] == cdn_args[0]:
        raise ValueError("mirror and CDN source identities must be distinct")
    if not mirror_args[3].isdisjoint(cdn_args[3]):
        raise ValueError("mirror and CDN public download hosts must be distinct")
    owner = github_value.get("owner")
    repository = github_value.get("repository")
    token_env = github_value.get("token_env")
    if not all(
        isinstance(part, str) and part for part in (owner, repository, token_env)
    ):
        raise ValueError("GitHub publication config contains an invalid value")

    mirror: HTTPSReleaseReplicaPublisher | HTTPSReadThroughReleaseMirror | None = None
    github: GitHubReleasePublisher | None = None
    cdn: HTTPSReleaseReplicaPublisher | None = None
    try:
        if mirror_read_through:
            mirror = HTTPSReadThroughReleaseMirror(
                source_id=mirror_args[0],
                public_hosts=mirror_args[3],
            )
        else:
            assert mirror_args[1] is not None and mirror_args[4] is not None
            mirror = HTTPSReleaseReplicaPublisher(
                source_id=mirror_args[0],
                endpoint=mirror_args[1],
                allowed_hosts=mirror_args[2],
                public_hosts=mirror_args[3],
                credentials=EnvironmentReplicaCredential(variable=mirror_args[4]),
            )
        github = GitHubReleasePublisher(
            owner=owner,
            repository=repository,
            credentials=EnvironmentGitHubCredential(variable=token_env),
        )
        cdn = HTTPSReleaseReplicaPublisher(
            source_id=cdn_args[0],
            endpoint=cdn_args[1],
            allowed_hosts=cdn_args[2],
            public_hosts=cdn_args[3],
            credentials=EnvironmentReplicaCredential(variable=cdn_args[4]),
        )
        return ReleaseAssetPublicationCoordinator(
            mirror=mirror,
            github=github,
            cdn=cdn,
        )
    except Exception:
        for resource in (cdn, github, mirror):
            if resource is not None:
                resource.close()
        raise


def _public_bootstrap_publisher(
    args: argparse.Namespace,
) -> HTTPSPublicBootstrapIndexPublisher:
    value = _read_json(
        args.publication_config,
        limit=1024 * 1024,
        label="Bootstrap index publication config",
    )
    expected = {
        "schema_version",
        "endpoint",
        "allowed_hosts",
        "public_url",
        "public_hosts",
        "token_env",
    }
    if (
        not isinstance(value, dict)
        or set(value) != expected
        or value.get("schema_version") != 1
    ):
        raise ValueError("Bootstrap index publication config has an invalid shape")
    strings = (
        value.get("endpoint"),
        value.get("public_url"),
        value.get("token_env"),
    )
    if not all(isinstance(item, str) and item for item in strings):
        raise ValueError("Bootstrap index publication config contains an invalid value")
    release_keys, publication_keys = _independent_trusted_keyrings(
        args.trusted_key,
        args.trusted_publication_key,
    )
    return HTTPSPublicBootstrapIndexPublisher(
        endpoint=strings[0],
        allowed_hosts=_strict_hosts(
            value.get("allowed_hosts"), "Bootstrap index control hosts"
        ),
        public_url=strings[1],
        public_hosts=_strict_hosts(
            value.get("public_hosts"), "Bootstrap index public hosts"
        ),
        credentials=EnvironmentPublicBootstrapCredential(variable=strings[2]),
        verifier=Ed25519SignatureVerifier(release_keys),
        freshness_verifier=Ed25519SignatureVerifier(publication_keys),
    )


def _public_pointer_signer(_args: argparse.Namespace) -> ReleaseSigner:
    """Create the same digest-pinned release-key boundary used by Candidate CI."""

    def required(name: str) -> str:
        value = os.environ.get(name)
        if not isinstance(value, str) or not value or "\x00" in value:
            raise ValueError("public pointer signer configuration is incomplete")
        return value

    try:
        public_key = base64.b64decode(
            required("ECOREX_RELEASE_SIGNER_PUBLIC_KEY"),
            validate=True,
        )
    except (TypeError, ValueError):
        raise ValueError("public pointer signer public key is invalid") from None
    executable_sha256 = required("ECOREX_RELEASE_SIGNER_EXECUTABLE_SHA256")
    if _SHA256.fullmatch(executable_sha256) is None:
        raise ValueError("public pointer signer executable digest is invalid")
    adapter = os.environ.get("ECOREX_RELEASE_SIGNER_ADAPTER") or None
    adapter_sha256 = os.environ.get("ECOREX_RELEASE_SIGNER_ADAPTER_SHA256") or None
    return DigestPinnedExternalSigner(
        key_id=required("ECOREX_RELEASE_SIGNER_KEY_ID"),
        public_key=public_key,
        executable_path=required("ECOREX_RELEASE_SIGNER_EXECUTABLE"),
        executable_sha256=executable_sha256,
        adapter_path=adapter,
        adapter_sha256=adapter_sha256,
        environment=os.environ,
    )


def _public_freshness_signer(_args: argparse.Namespace) -> ReleaseSigner:
    """Create the distinct online publication/timestamp KMS boundary."""

    def required(name: str) -> str:
        value = os.environ.get(name)
        if not isinstance(value, str) or not value or "\x00" in value:
            raise ValueError("public freshness signer configuration is incomplete")
        return value

    try:
        public_key = base64.b64decode(
            required("ECOREX_PUBLICATION_SIGNER_PUBLIC_KEY"),
            validate=True,
        )
    except (TypeError, ValueError):
        raise ValueError("public freshness signer public key is invalid") from None
    executable_sha256 = required("ECOREX_PUBLICATION_SIGNER_EXECUTABLE_SHA256")
    if _SHA256.fullmatch(executable_sha256) is None:
        raise ValueError("public freshness signer executable digest is invalid")
    adapter = os.environ.get("ECOREX_PUBLICATION_SIGNER_ADAPTER") or None
    adapter_sha256 = os.environ.get("ECOREX_PUBLICATION_SIGNER_ADAPTER_SHA256") or None
    return DigestPinnedExternalSigner(
        key_id=required("ECOREX_PUBLICATION_SIGNER_KEY_ID"),
        public_key=public_key,
        executable_path=required("ECOREX_PUBLICATION_SIGNER_EXECUTABLE"),
        executable_sha256=executable_sha256,
        adapter_path=adapter,
        adapter_sha256=adapter_sha256,
        environment=os.environ,
    )


def _strict_hosts(value: Any, label: str) -> frozenset[str]:
    if (
        not isinstance(value, list)
        or not value
        or not all(
            isinstance(host, str) and host.strip() == host and host for host in value
        )
    ):
        raise ValueError(f"{label} must be a non-empty string list")
    normalized = frozenset(host.casefold().rstrip(".") for host in value)
    if len(normalized) != len(value) or any(not host for host in normalized):
        raise ValueError(f"{label} contains a duplicate or invalid host")
    return normalized


def _trusted_release_keys(values: list[str]) -> dict[str, bytes]:
    keys: dict[str, bytes] = {}
    for value in values:
        key_id, separator, encoded = value.partition("=")
        if not separator or not key_id or key_id in keys:
            raise ValueError(
                "--trusted-key entries must have unique KEY_ID=BASE64 form"
            )
        try:
            material = base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError):
            raise ValueError("--trusted-key contains invalid Base64") from None
        if len(material) != 32:
            raise ValueError("--trusted-key must contain one raw Ed25519 public key")
        keys[key_id] = material
    return keys


def _independent_trusted_keyrings(
    release_values: list[str],
    publication_values: list[str],
) -> tuple[dict[str, bytes], dict[str, bytes]]:
    release_keys = _trusted_release_keys(release_values)
    publication_keys = _trusted_release_keys(publication_values)
    release_fingerprints = {
        hashlib.sha256(material).digest() for material in release_keys.values()
    }
    publication_fingerprints = {
        hashlib.sha256(material).digest() for material in publication_keys.values()
    }
    if set(release_keys) & set(publication_keys) or (
        release_fingerprints & publication_fingerprints
    ):
        raise ValueError(
            "release and publication trust roles must use distinct Ed25519 keys"
        )
    return release_keys, publication_keys


def _release_directory_files(
    release_dir: Path,
    manifest: ReleaseManifest,
) -> tuple[Path, ...]:
    root = release_dir.expanduser().resolve(strict=True)
    if not root.is_dir():
        raise ValueError("release directory is not a directory")
    reserved_names = {
        "release-manifest.json",
        "release-metadata.json",
        "sbom.cdx.json",
    }
    artifact_names = tuple(artifact.file_name for artifact in manifest.artifacts)
    if len(artifact_names) != len(set(artifact_names)) or reserved_names.intersection(
        artifact_names
    ):
        raise ValueError("release artifact filenames collide with publication files")
    expected_names = reserved_names.union(artifact_names)
    observed: set[str] = set()
    files: list[Path] = []
    for entry in root.iterdir():
        metadata = entry.lstat()
        reparse = getattr(stat_module, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        if (
            stat_module.S_ISLNK(metadata.st_mode)
            or bool(getattr(metadata, "st_file_attributes", 0) & reparse)
            or not stat_module.S_ISREG(metadata.st_mode)
            or metadata.st_size < 1
        ):
            raise ValueError("release directory contains a non-regular or empty entry")
        observed.add(entry.name)
        files.append(entry)
    if observed != expected_names:
        raise ValueError("release directory contains missing or unexpected files")
    return tuple(sorted(files, key=lambda item: item.name))


def _file_sha256(path: Path) -> str:
    try:
        before = path.lstat()
    except OSError:
        raise ValueError("release file cannot be inspected") from None
    reparse = getattr(stat_module, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    if (
        stat_module.S_ISLNK(before.st_mode)
        or bool(getattr(before, "st_file_attributes", 0) & reparse)
        or not stat_module.S_ISREG(before.st_mode)
    ):
        raise ValueError("release file is not a regular file")
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            if _stat_identity(opened) != _stat_identity(before):
                raise ValueError("release file changed while opening")
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
            after = os.fstat(stream.fileno())
    except ValueError:
        raise
    except OSError:
        raise ValueError("release file cannot be read") from None
    if _stat_identity(after) != _stat_identity(before):
        raise ValueError("release file changed while hashing")
    return digest.hexdigest()


def _stat_identity(value: os.stat_result) -> tuple[int, int, int, int]:
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns


def _verify_release_directory(
    release_dir_value: Path,
    trusted_key_values: list[str],
) -> VerifiedReleaseDirectory:
    release_dir = release_dir_value.expanduser().resolve(strict=True)
    manifest_path = release_dir / "release-manifest.json"
    manifest_bytes = _read_bounded_file_bytes(
        manifest_path, limit=MAX_MANIFEST_BYTES, label="release manifest"
    )
    manifest = ReleaseManifest.from_json(manifest_bytes)
    verifier = Ed25519SignatureVerifier(_trusted_release_keys(trusted_key_values))
    verify_manifest_signature(manifest, verifier)
    files = _release_directory_files(release_dir, manifest)
    expected_digests: dict[str, str] = {}
    for artifact in manifest.artifacts:
        verify_artifact_signature(manifest, artifact, verifier)
        artifact_path = release_dir / artifact.file_name
        verify_artifact_file(artifact_path, manifest, artifact, verifier)
        expected_digests[artifact.file_name] = artifact.sha256
    metadata = _read_json(
        release_dir / "release-metadata.json",
        limit=MAX_EVIDENCE_BYTES,
        label="release metadata",
    )
    if not isinstance(metadata, dict):
        raise ValueError("release metadata must be an object")
    for name in ("release-manifest.json", "sbom.cdx.json"):
        expected = metadata.get(
            "manifest_sha256" if name == "release-manifest.json" else "sbom_sha256"
        )
        if not isinstance(expected, str) or len(expected) != 64:
            raise ValueError("release metadata is missing an output digest")
        expected_digests[name] = expected
    metadata_path = release_dir / "release-metadata.json"
    expected_digests[metadata_path.name] = _file_sha256(metadata_path)
    for path in files:
        if _file_sha256(path) != expected_digests[path.name]:
            raise ValueError("release directory digest verification failed")
    return VerifiedReleaseDirectory(manifest, manifest_bytes, files, expected_digests)


def _upload_github(
    args: argparse.Namespace,
    publisher: GitHubReleasePublisher,
) -> dict[str, Any]:
    verified = _verify_release_directory(args.release_dir, args.trusted_key)
    manifest = verified.manifest

    draft = publisher.ensure_draft(
        version=manifest.version,
        channel=manifest.channel,
        release_id=manifest.release_id,
    )
    receipts = []
    for path in verified.files:
        receipt = publisher.ensure_asset(
            draft,
            path,
            expected_sha256=verified.expected_sha256[path.name],
        )
        receipts.append(
            {
                "name": receipt.name,
                "size_bytes": receipt.size_bytes,
                "sha256": receipt.sha256,
                "url": receipt.browser_download_url,
            }
        )
    return {
        "release_id": manifest.release_id,
        "version": manifest.version,
        "github_release_id": draft.release_id,
        "draft": draft.draft,
        "assets": receipts,
    }


def _publish_assets(
    args: argparse.Namespace,
    coordinator: ReleaseAssetPublicationCoordinator,
) -> dict[str, Any]:
    verified = _verify_release_directory(args.release_dir, args.trusted_key)
    receipt_path = _publication_receipt_path(args.receipt)
    lock_path = receipt_path.with_suffix(receipt_path.suffix + ".lock")
    # Hold the release-identity lock across remote mutation and the durable
    # receipt.  Otherwise two processes targeting the same initially-missing
    # receipt path could publish different releases before discovering the
    # conflict only at local write time.
    with ProductFileLock(lock_path, timeout=0):
        _validate_publication_receipt_identity(
            receipt_path,
            release_id=verified.manifest.release_id,
            manifest_sha256=verified.expected_sha256["release-manifest.json"],
        )
        publication = coordinator.publish(
            manifest=verified.manifest,
            files=verified.files,
            expected_sha256=verified.expected_sha256,
            publish_github=args.publish_github,
        )
        sources = {
            source_id: [dict(receipt) for receipt in receipts]
            for source_id, receipts in publication.source_receipts.items()
        }
        receipt_value = {
            "schema_version": 1,
            "release_id": publication.release_id,
            "version": verified.manifest.version,
            "manifest_sha256": verified.expected_sha256["release-manifest.json"],
            "github_release_id": publication.github_release_id,
            "github_draft": publication.github_draft,
            "source_receipts": sources,
        }
        receipt_path, receipt_sha256 = _write_publication_receipt_unlocked(
            receipt_path, receipt_value
        )
    return {
        **receipt_value,
        "receipt_path": str(receipt_path),
        "receipt_sha256": receipt_sha256,
    }


def _build_public_bootstrap_discovery(
    args: argparse.Namespace,
    signer: ReleaseSigner,
    freshness_signer: ReleaseSigner,
) -> dict[str, Any]:
    """Generate the browser-facing pointer only after full release verification."""

    verified, receipt_sha256, index = _expected_public_bootstrap_discovery(
        release_dir=args.release_dir,
        publication_receipt=args.publication_receipt,
        trusted_keys=args.trusted_key,
        trusted_freshness_keys=args.trusted_publication_key,
        signer=signer,
        freshness_signer=freshness_signer,
    )
    output_path, output_sha256 = write_public_bootstrap_index(args.output, index)
    authority = index.get("authority")
    if not isinstance(authority, dict):
        raise ValueError("signed public Bootstrap authority is missing")
    signature = authority.get("signature")
    target = authority.get("target")
    if not isinstance(signature, dict) or not isinstance(target, dict):
        raise ValueError("signed public Bootstrap authority is invalid")
    return {
        "schema_version": 1,
        "release_id": verified.manifest.release_id,
        "version": verified.manifest.version,
        "status": "published",
        "trust": "untrusted-discovery-hint",
        "pointer_sequence": authority.get("sequence"),
        "pointer_revision": authority.get("revision"),
        "pointer_target": target,
        "pointer_signature_key_id": signature.get("key_id"),
        "publication_receipt_sha256": receipt_sha256,
        "output": str(output_path),
        "output_sha256": output_sha256,
    }


def _expected_public_bootstrap_discovery(
    *,
    release_dir: Path,
    publication_receipt: Path,
    trusted_keys: list[str],
    trusted_freshness_keys: list[str],
    signer: ReleaseSigner | None = None,
    authority_signature: SignatureEnvelope | None = None,
    freshness_signer: ReleaseSigner | None = None,
    freshness_signature: SignatureEnvelope | None = None,
    freshness_issued_at: str | None = None,
    freshness_expires_at: str | None = None,
) -> tuple[VerifiedReleaseDirectory, str, dict[str, object]]:
    verified = _verify_release_directory(release_dir, trusted_keys)
    receipt_path = _publication_receipt_path(publication_receipt)
    receipt_sha256 = _file_sha256(receipt_path)
    receipt_value = _read_json(
        receipt_path,
        limit=MAX_EVIDENCE_BYTES,
        label="release publication receipt",
    )
    if _file_sha256(receipt_path) != receipt_sha256:
        raise ValueError("release publication receipt changed while reading")
    if not isinstance(receipt_value, dict):
        raise ValueError("release publication receipt must be an object")
    release_keys, publication_keys = _independent_trusted_keyrings(
        trusted_keys,
        trusted_freshness_keys,
    )
    verifier = Ed25519SignatureVerifier(release_keys)
    freshness_verifier = Ed25519SignatureVerifier(publication_keys)
    index = build_public_bootstrap_index(
        manifest=verified.manifest,
        manifest_bytes=verified.manifest_bytes,
        manifest_sha256=verified.expected_sha256["release-manifest.json"],
        publication_receipt=receipt_value,
        publication_receipt_sha256=receipt_sha256,
        verifier=verifier,
        freshness_verifier=freshness_verifier,
        signer=signer,
        authority_signature=authority_signature,
        freshness_signer=freshness_signer,
        freshness_signature=freshness_signature,
        freshness_issued_at=freshness_issued_at,
        freshness_expires_at=freshness_expires_at,
    )
    return verified, receipt_sha256, index


def _prepared_public_bootstrap_discovery(
    args: argparse.Namespace,
) -> tuple[VerifiedReleaseDirectory, str, bytes]:
    """Reproduce and verify the exact discovery bytes for either phase."""

    index_bytes = _read_bounded_file_bytes(
        args.index,
        limit=256 * 1024,
        label="public Bootstrap index",
    )
    try:
        actual_index = json.loads(index_bytes.decode("utf-8"))
        authority = actual_index["authority"]
        authority_signature = SignatureEnvelope.from_dict(authority["signature"])
        freshness = actual_index["freshness"]
        freshness_signature = SignatureEnvelope.from_dict(freshness["signature"])
        freshness_issued_at = freshness["issued_at"]
        freshness_expires_at = freshness["expires_at"]
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        KeyError,
        TypeError,
        ValueError,
        RecursionError,
    ):
        raise ValueError("public Bootstrap authority signature is invalid") from None
    verified, publication_receipt_sha256, expected_index = (
        _expected_public_bootstrap_discovery(
            release_dir=args.release_dir,
            publication_receipt=args.publication_receipt,
            trusted_keys=args.trusted_key,
            trusted_freshness_keys=args.trusted_publication_key,
            authority_signature=authority_signature,
            freshness_signature=freshness_signature,
            freshness_issued_at=freshness_issued_at,
            freshness_expires_at=freshness_expires_at,
        )
    )
    expected_bytes = (
        json.dumps(
            expected_index,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        + b"\n"
    )
    if index_bytes != expected_bytes:
        raise ValueError("public Bootstrap index differs from the signed release")
    return verified, publication_receipt_sha256, index_bytes


def _stage_public_bootstrap_discovery(
    args: argparse.Namespace,
    publisher: HTTPSPublicBootstrapIndexPublisher,
) -> dict[str, Any]:
    """Stage verified bytes while preserving the current public authority."""

    verified, publication_receipt_sha256, index_bytes = (
        _prepared_public_bootstrap_discovery(args)
    )
    staged = publisher.stage(index_bytes)
    manifest_sha256 = verified.expected_sha256["release-manifest.json"]
    receipt_value = {
        **staged.to_dict(),
        "manifest_sha256": manifest_sha256,
        "release_publication_receipt_sha256": publication_receipt_sha256,
    }
    receipt_path, receipt_sha256 = _write_bootstrap_index_receipt(
        args.receipt, receipt_value
    )
    return {
        **receipt_value,
        "receipt_path": str(receipt_path),
        "receipt_sha256": receipt_sha256,
    }


def _activate_public_bootstrap_discovery(
    args: argparse.Namespace,
    publisher: HTTPSPublicBootstrapIndexPublisher,
) -> dict[str, Any]:
    """CAS-activate a verified stage receipt, then read public bytes back."""

    verified, publication_receipt_sha256, index_bytes = (
        _prepared_public_bootstrap_discovery(args)
    )
    manifest_sha256 = verified.expected_sha256["release-manifest.json"]
    stage_path = _publication_receipt_path(args.stage_receipt)
    stage_receipt_sha256 = _file_sha256(stage_path)
    stage_value = _read_json(
        stage_path,
        limit=MAX_EVIDENCE_BYTES,
        label="Bootstrap index stage receipt",
    )
    if _file_sha256(stage_path) != stage_receipt_sha256:
        raise ValueError("Bootstrap index stage receipt changed while reading")
    staged = _validated_bootstrap_index_stage_receipt(
        stage_value,
        manifest=verified.manifest,
        manifest_sha256=manifest_sha256,
        release_publication_receipt_sha256=publication_receipt_sha256,
        expected_index_sha256=hashlib.sha256(index_bytes).hexdigest(),
        expected_index_size_bytes=len(index_bytes),
        expected_public_url=publisher.public_url,
    )
    publication = publisher.activate(index_bytes, staged)
    receipt_value = {
        **publication.to_dict(),
        "manifest_sha256": manifest_sha256,
        "release_publication_receipt_sha256": publication_receipt_sha256,
        "stage_receipt_sha256": stage_receipt_sha256,
    }
    receipt_path, receipt_sha256 = _write_bootstrap_index_receipt(
        args.receipt, receipt_value
    )
    return {
        **receipt_value,
        "receipt_path": str(receipt_path),
        "receipt_sha256": receipt_sha256,
    }


def _write_bootstrap_index_receipt(
    path_value: Path,
    value: Mapping[str, Any],
) -> tuple[Path, str]:
    path = _publication_receipt_path(path_value)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    with ProductFileLock(lock_path, timeout=0):
        if path.exists():
            existing = _read_json(
                path,
                limit=MAX_EVIDENCE_BYTES,
                label="Bootstrap index publication receipt",
            )
            if existing != dict(value):
                raise ValueError(
                    "Bootstrap index publication receipt belongs to a different publication"
                )
            return path, _file_sha256(path)
        return _write_publication_receipt_unlocked(path, value)


def _write_publication_receipt(
    path_value: Path,
    value: Mapping[str, Any],
) -> tuple[Path, str]:
    path = _publication_receipt_path(path_value)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    with ProductFileLock(lock_path, timeout=0):
        _validate_publication_receipt_identity(
            path,
            release_id=str(value.get("release_id") or ""),
            manifest_sha256=str(value.get("manifest_sha256") or ""),
        )
        return _write_publication_receipt_unlocked(path, value)


def _write_publication_receipt_unlocked(
    path: Path,
    value: Mapping[str, Any],
) -> tuple[Path, str]:
    payload = json.dumps(
        dict(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    digest = hashlib.sha256(payload).hexdigest()
    temporary = path.with_name(path.name + ".tmp-" + secrets.token_hex(8))
    try:
        with temporary.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        _durable_replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return path, digest


def _durable_replace(source: Path, target: Path) -> None:
    if os.name != "nt":
        os.replace(source, target)
        descriptor = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        return
    import ctypes
    from ctypes import wintypes

    move_file = ctypes.WinDLL("kernel32", use_last_error=True).MoveFileExW
    move_file.argtypes = (wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD)
    move_file.restype = wintypes.BOOL
    if not move_file(str(source), str(target), 0x1 | 0x8):
        raise OSError(ctypes.get_last_error(), "durable receipt replace failed")


def _validate_publication_receipt_identity(
    path_value: Path,
    *,
    release_id: str,
    manifest_sha256: str,
) -> None:
    path = _publication_receipt_path(path_value)
    if not path.exists():
        return
    existing = _read_json(
        path,
        limit=MAX_EVIDENCE_BYTES,
        label="release publication receipt",
    )
    if (
        not isinstance(existing, dict)
        or existing.get("schema_version") != 1
        or existing.get("release_id") != release_id
        or existing.get("manifest_sha256") != manifest_sha256
    ):
        raise ValueError("release publication receipt belongs to a different release")


def _publication_receipt_path(path_value: Path) -> Path:
    raw = path_value.expanduser()
    if os.path.lexists(raw):
        metadata = raw.lstat()
        reparse = getattr(stat_module, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        if (
            stat_module.S_ISLNK(metadata.st_mode)
            or bool(getattr(metadata, "st_file_attributes", 0) & reparse)
            or not stat_module.S_ISREG(metadata.st_mode)
        ):
            raise ValueError("release publication receipt is not a regular file")
    return raw.resolve()


def _publication_evidence_token(
    path_value: Path,
    *,
    manifest: ReleaseManifest,
    manifest_sha256: str,
) -> str:
    """Bind all three publication gates to one exact, matching receipt."""

    path = _publication_receipt_path(path_value)
    before_digest = _file_sha256(path)
    value = _read_json(
        path,
        limit=MAX_EVIDENCE_BYTES,
        label="release publication receipt",
    )
    if _file_sha256(path) != before_digest:
        raise ValueError("release publication receipt changed while reading")
    expected_keys = {
        "schema_version",
        "release_id",
        "version",
        "manifest_sha256",
        "github_release_id",
        "github_draft",
        "source_receipts",
    }
    if (
        not isinstance(value, dict)
        or set(value) != expected_keys
        or value.get("schema_version") != 1
        or value.get("release_id") != manifest.release_id
        or value.get("version") != manifest.version
        or value.get("manifest_sha256") != manifest_sha256
        or isinstance(value.get("github_release_id"), bool)
        or not isinstance(value.get("github_release_id"), int)
        or value["github_release_id"] < 1
        or value.get("github_draft") is not False
        or not isinstance(value.get("source_receipts"), dict)
    ):
        raise ValueError("release publication receipt does not match the manifest")

    source_receipts = value["source_receipts"]
    expected_source_ids = tuple(source.source_id for source in manifest.sources)
    if set(source_receipts) != set(expected_source_ids):
        raise ValueError("release publication receipt source set is incomplete")
    reserved_names = {
        "release-manifest.json",
        "release-metadata.json",
        "sbom.cdx.json",
    }
    artifact_names = tuple(artifact.file_name for artifact in manifest.artifacts)
    if len(artifact_names) != len(set(artifact_names)) or reserved_names.intersection(
        artifact_names
    ):
        raise ValueError("release manifest artifact filenames collide")
    expected_names = reserved_names.union(artifact_names)
    artifacts = {artifact.file_name: artifact for artifact in manifest.artifacts}
    common_identity: dict[str, tuple[int, str]] | None = None
    for source in manifest.sources:
        entries = source_receipts.get(source.source_id)
        if not isinstance(entries, list) or len(entries) != len(expected_names):
            raise ValueError("release publication receipt asset set is incomplete")
        identities: dict[str, tuple[int, str]] = {}
        for entry in entries:
            if not isinstance(entry, dict) or set(entry) != {
                "name",
                "size_bytes",
                "sha256",
                "url",
            }:
                raise ValueError("release publication receipt asset is invalid")
            name = entry.get("name")
            size = entry.get("size_bytes")
            sha256 = entry.get("sha256")
            url = entry.get("url")
            if (
                not isinstance(name, str)
                or name not in expected_names
                or name in identities
                or isinstance(size, bool)
                or not isinstance(size, int)
                or size < 1
                or not isinstance(sha256, str)
                or _SHA256.fullmatch(sha256) is None
                or url != f"{source.base_url}/{quote(name, safe='')}"
            ):
                raise ValueError("release publication receipt asset is invalid")
            artifact = artifacts.get(name)
            if artifact is not None and (
                size != artifact.size_bytes or sha256 != artifact.sha256
            ):
                raise ValueError(
                    "release publication receipt artifact identity differs"
                )
            if name == "release-manifest.json" and sha256 != manifest_sha256:
                raise ValueError("release publication receipt manifest digest differs")
            identities[name] = (size, sha256)
        if set(identities) != expected_names:
            raise ValueError("release publication receipt asset set is incomplete")
        if common_identity is None:
            common_identity = identities
        elif identities != common_identity:
            raise ValueError("release publication origins contain different bytes")
    return f"publication-receipt:sha256:{before_digest}"


def _canonical_freshness_time(value: Any) -> bool:
    return isinstance(value, str) and _FRESHNESS_TIME.fullmatch(value) is not None


def _valid_bootstrap_predecessor(value: tuple[Any, ...]) -> bool:
    if len(value) != 5:
        return False
    if all(item is None for item in value):
        return True
    activation, sequence, revision, digest, target = value
    return (
        all(item is not None for item in value)
        and isinstance(activation, str)
        and re.fullmatch(r"bactive_[0-9a-f]{32}", activation) is not None
        and not isinstance(sequence, bool)
        and isinstance(sequence, int)
        and sequence > 0
        and isinstance(revision, str)
        and re.fullmatch(r"release-stable-[0-9a-f]{24}", revision) is not None
        and isinstance(digest, str)
        and _SHA256.fullmatch(digest) is not None
        and isinstance(target, dict)
        and set(target) == {"manifest_sha256", "release_id", "version", "build_digest"}
        and target.get("release_id") == revision
        and isinstance(target.get("manifest_sha256"), str)
        and _SHA256.fullmatch(str(target.get("manifest_sha256"))) is not None
        and isinstance(target.get("build_digest"), str)
        and _SHA256.fullmatch(str(target.get("build_digest"))) is not None
        and isinstance(target.get("version"), str)
    )


def _validated_bootstrap_index_stage_receipt(
    value: Any,
    *,
    manifest: ReleaseManifest,
    manifest_sha256: str,
    release_publication_receipt_sha256: str,
    expected_index_sha256: str | None = None,
    expected_index_size_bytes: int | None = None,
    expected_public_url: str | None = None,
) -> PublicBootstrapStageReceipt:
    """Validate an immutable stage/CAS receipt against one signed release."""

    expected = {
        "schema_version",
        "receipt_type",
        "release_id",
        "version",
        "state",
        "index_sha256",
        "index_size_bytes",
        "public_url",
        "staged_revision_id",
        "authority_sequence",
        "authority_revision_id",
        "authority_target",
        "freshness_issued_at",
        "freshness_expires_at",
        "expected_previous_activation_record_id",
        "expected_previous_sequence",
        "expected_previous_authority_revision_id",
        "expected_previous_index_sha256",
        "expected_previous_target",
        "manifest_sha256",
        "release_publication_receipt_sha256",
    }
    previous = (
        (
            value.get("expected_previous_activation_record_id"),
            value.get("expected_previous_sequence"),
            value.get("expected_previous_authority_revision_id"),
            value.get("expected_previous_index_sha256"),
            value.get("expected_previous_target"),
        )
        if isinstance(value, dict)
        else (None,) * 5
    )
    target = value.get("authority_target") if isinstance(value, dict) else None
    public_url = value.get("public_url") if isinstance(value, dict) else None
    parsed = urlsplit(public_url if isinstance(public_url, str) else "")
    try:
        public_port = parsed.port
    except ValueError:
        public_port = -1
    if (
        not isinstance(value, dict)
        or set(value) != expected
        or value.get("schema_version") != 1
        or value.get("receipt_type") != "ecorex-public-bootstrap-index-stage"
        or value.get("release_id") != manifest.release_id
        or value.get("version") != manifest.version
        or value.get("state") != "staged"
        or not isinstance(value.get("index_sha256"), str)
        or _SHA256.fullmatch(value["index_sha256"]) is None
        or isinstance(value.get("index_size_bytes"), bool)
        or not isinstance(value.get("index_size_bytes"), int)
        or not 1 <= value["index_size_bytes"] <= 256 * 1024
        or parsed.scheme != "https"
        or not parsed.hostname
        or public_port not in {None, 443}
        or parsed.username
        or parsed.password
        or not parsed.path.endswith("/" + PUBLIC_BOOTSTRAP_INDEX_FILE_NAME)
        or parsed.query
        or parsed.fragment
        or _SAFE_LOCAL_ID.fullmatch(str(value.get("staged_revision_id") or "")) is None
        or value.get("authority_sequence") != stable_pointer_sequence(manifest.version)
        or value.get("authority_revision_id") != manifest.release_id
        or target
        != {
            "manifest_sha256": manifest_sha256,
            "release_id": manifest.release_id,
            "version": manifest.version,
            "build_digest": manifest.build_digest,
        }
        or not _canonical_freshness_time(value.get("freshness_issued_at"))
        or not _canonical_freshness_time(value.get("freshness_expires_at"))
        or not _valid_bootstrap_predecessor(previous)
        or value.get("manifest_sha256") != manifest_sha256
        or value.get("release_publication_receipt_sha256")
        != release_publication_receipt_sha256
        or (
            expected_index_sha256 is not None
            and value.get("index_sha256") != expected_index_sha256
        )
        or (
            expected_index_size_bytes is not None
            and value.get("index_size_bytes") != expected_index_size_bytes
        )
        or (
            expected_public_url is not None
            and value.get("public_url") != expected_public_url
        )
    ):
        raise ValueError("Bootstrap index stage receipt does not match release")
    return PublicBootstrapStageReceipt(
        release_id=manifest.release_id,
        version=manifest.version,
        index_sha256=str(value["index_sha256"]),
        index_size_bytes=int(value["index_size_bytes"]),
        public_url=str(value["public_url"]),
        staged_revision_id=str(value["staged_revision_id"]),
        authority_sequence=int(value["authority_sequence"]),
        authority_revision_id=str(value["authority_revision_id"]),
        authority_target=dict(value["authority_target"]),
        freshness_issued_at=str(value["freshness_issued_at"]),
        freshness_expires_at=str(value["freshness_expires_at"]),
        expected_previous_activation_record_id=(
            str(previous[0]) if previous[0] is not None else None
        ),
        expected_previous_sequence=(
            int(previous[1]) if previous[1] is not None else None
        ),
        expected_previous_authority_revision_id=(
            str(previous[2]) if previous[2] is not None else None
        ),
        expected_previous_index_sha256=(
            str(previous[3]) if previous[3] is not None else None
        ),
        expected_previous_target=(
            dict(previous[4]) if previous[4] is not None else None
        ),
    )


def _bootstrap_index_evidence_token(
    path_value: Path,
    *,
    manifest: ReleaseManifest,
    manifest_sha256: str,
    release_publication_receipt_sha256: str,
) -> str:
    """Read a server-issued active+readback proof from a durable receipt."""

    path = _publication_receipt_path(path_value)
    before_digest = _file_sha256(path)
    value = _read_json(
        path,
        limit=MAX_EVIDENCE_BYTES,
        label="Bootstrap index publication receipt",
    )
    if _file_sha256(path) != before_digest:
        raise ValueError("Bootstrap index publication receipt changed while reading")
    expected = {
        "schema_version",
        "receipt_type",
        "release_id",
        "version",
        "state",
        "index_sha256",
        "index_size_bytes",
        "public_url",
        "staged_revision_id",
        "active_activation_record_id",
        "active_sequence",
        "active_authority_revision_id",
        "active_target",
        "public_object_revision_id",
        "previous_activation_record_id",
        "previous_sequence",
        "previous_authority_revision_id",
        "previous_index_sha256",
        "previous_target",
        "readback_record_id",
        "readback_proof_token",
        "read_back_at",
        "cache_control",
        "manifest_sha256",
        "release_publication_receipt_sha256",
        "stage_receipt_sha256",
    }
    target = value.get("active_target") if isinstance(value, dict) else None
    proof_token = value.get("readback_proof_token") if isinstance(value, dict) else None
    if (
        not isinstance(value, dict)
        or set(value) != expected
        or value.get("schema_version") != 1
        or value.get("receipt_type") != "ecorex-public-bootstrap-index-publication"
        or value.get("release_id") != manifest.release_id
        or value.get("version") != manifest.version
        or value.get("state") != "active-and-read-back"
        or value.get("manifest_sha256") != manifest_sha256
        or value.get("release_publication_receipt_sha256")
        != release_publication_receipt_sha256
        or value.get("cache_control") != "no-store"
        or value.get("active_sequence") != stable_pointer_sequence(manifest.version)
        or value.get("active_authority_revision_id") != manifest.release_id
        or target
        != {
            "manifest_sha256": manifest_sha256,
            "release_id": manifest.release_id,
            "version": manifest.version,
            "build_digest": manifest.build_digest,
        }
        or not isinstance(value.get("index_sha256"), str)
        or _SHA256.fullmatch(str(value.get("index_sha256"))) is None
        or not isinstance(value.get("stage_receipt_sha256"), str)
        or _SHA256.fullmatch(str(value.get("stage_receipt_sha256"))) is None
        or not isinstance(proof_token, str)
        or re.fullmatch(
            r"bootstrap-index-proof:bread_[0-9a-f]{32}:sha256:[0-9a-f]{64}",
            proof_token,
        )
        is None
        or not isinstance(value.get("readback_record_id"), str)
        or not proof_token.startswith(
            f"bootstrap-index-proof:{value.get('readback_record_id')}:sha256:"
        )
    ):
        raise ValueError("Bootstrap index publication receipt does not match release")
    return proof_token


def _promotion_evidence_digest(
    *,
    manifest_sha256: str,
    publication_token: str,
    gates: Mapping[str, str],
    rollout_target_sha256: str,
) -> str:
    payload = json.dumps(
        {
            "manifest_sha256": manifest_sha256,
            "publication_token": publication_token,
            "gates": dict(sorted(gates.items())),
            "rollout_target_sha256": rollout_target_sha256,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _promotion_rollout_target(
    manifest: ReleaseManifest,
    args: argparse.Namespace,
) -> tuple[dict[str, Any], str]:
    target = {
        "channel": manifest.channel.value,
        "percentage": args.percentage,
        "organizations": sorted(set(args.organization)),
        "accounts": sorted(set(args.account)),
        "minimum_compatible_version": args.minimum_compatible_version,
    }
    payload = (
        "ecorex.promotion-rollout-target.v1\0"
        + json.dumps(
            target,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    ).encode("utf-8")
    return target, hashlib.sha256(payload).hexdigest()


def _promote(
    args: argparse.Namespace, client: AdminControlPlaneClient | None
) -> dict[str, Any]:
    manifest_payload = _read_bounded_file_bytes(
        args.manifest, limit=MAX_MANIFEST_BYTES, label="release manifest"
    )
    try:
        manifest_value = json.loads(manifest_payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        raise ValueError("release manifest is invalid JSON") from None
    manifest = ReleaseManifest.from_dict(manifest_value)
    digest = hashlib.sha256(manifest_payload).hexdigest()
    verifier = Ed25519SignatureVerifier(_trusted_release_keys(args.trusted_key))
    verify_manifest_signature(manifest, verifier)
    for artifact in manifest.artifacts:
        verify_artifact_signature(manifest, artifact, verifier)
    evidence = _read_json(
        args.evidence, limit=MAX_EVIDENCE_BYTES, label="release evidence"
    )
    if not isinstance(evidence, dict):
        raise ValueError("release evidence must be a JSON object")
    required_gates = required_release_gates(manifest.channel)
    phase = args.phase
    if manifest.channel is not ReleaseChannel.STABLE and phase != "auto":
        raise ValueError("phased promotion is reserved for the stable channel")
    if phase == "prepare" and args.activate:
        raise ValueError("prepare phase cannot activate a rollout")
    expected_gates = (
        required_gates - {"bootstrap-index"}
        if manifest.channel is ReleaseChannel.STABLE and phase == "prepare"
        else required_gates
    )
    bundle_phase = "prepare" if phase == "prepare" else "finalize"
    validated = validate_signed_gate_bundle(
        evidence,
        manifest=manifest,
        expected_gates=frozenset(expected_gates),
        expected_phase=bundle_phase,
        verifier=verifier,
        expected_manifest_sha256=digest,
    )
    normalized = {
        gate: result["evidence"] for gate, result in validated.items()
    }
    if not 1 <= args.percentage <= 100:
        raise ValueError("rollout percentage must be between one and 100")
    rollout_target, rollout_target_sha256 = _promotion_rollout_target(manifest, args)
    # Publication receipts bind the exact immutable release-manifest.json
    # bytes, including their deterministic formatting and terminal newline.
    # Re-serializing the parsed object here would create a second, incompatible
    # identity and make a real ReleaseBuilder output impossible to promote.
    if args.publication_receipt is None:
        raise ValueError("--publication-receipt is required to bind publication gates")
    publication_token = _publication_evidence_token(
        args.publication_receipt,
        manifest=manifest,
        manifest_sha256=digest,
    )
    publication_evidence = {normalized[gate] for gate in _PUBLICATION_GATES}
    if publication_evidence != {publication_token}:
        raise ValueError(
            "GitHub, mirror and CDN gates must reference the same publication receipt"
        )
    bootstrap_index_token: str | None = None
    if manifest.channel is ReleaseChannel.STABLE and phase != "prepare":
        if args.bootstrap_index_receipt is None:
            raise ValueError(
                "--bootstrap-index-receipt is required for stable promotion"
            )
        bootstrap_index_token = _bootstrap_index_evidence_token(
            args.bootstrap_index_receipt,
            manifest=manifest,
            manifest_sha256=digest,
            release_publication_receipt_sha256=publication_token.rsplit(":", 1)[1],
        )
        if normalized.get("bootstrap-index") != bootstrap_index_token:
            raise ValueError(
                "stable Bootstrap index gate must reference server readback proof"
            )
    elif args.bootstrap_index_receipt is not None:
        raise ValueError(
            "prepare promotion must not consume a Bootstrap proof"
            if manifest.channel is ReleaseChannel.STABLE
            else "canary promotion must not mutate the stable Bootstrap index"
        )
    if args.dry_run:
        return {
            "dry_run": True,
            "release_id": manifest.release_id,
            "version": manifest.version,
            "channel": manifest.channel.value,
            "gate_count": len(normalized),
            "percentage": args.percentage,
            "activate": args.activate,
            "phase": phase,
            "gate_bundle_sha256": gate_bundle_sha256(evidence),
            "publication_receipt": publication_token,
            "bootstrap_index_receipt": bootstrap_index_token,
        }
    if client is None:
        raise RuntimeError("Control Plane client is required")
    lock_path = (
        args.journal.expanduser().resolve().with_suffix(args.journal.suffix + ".lock")
    )
    prepare_gates = {
        gate: detail for gate, detail in normalized.items() if gate != "bootstrap-index"
    }
    prepare_evidence_sha256 = _promotion_evidence_digest(
        manifest_sha256=digest,
        publication_token=publication_token,
        gates=prepare_gates,
        rollout_target_sha256=rollout_target_sha256,
    )
    final_evidence_sha256 = (
        _promotion_evidence_digest(
            manifest_sha256=digest,
            publication_token=publication_token,
            gates=normalized,
            rollout_target_sha256=rollout_target_sha256,
        )
        if phase != "prepare"
        else None
    )
    with ProductFileLock(lock_path, timeout=0):
        journal = PromotionJournal(
            args.journal,
            manifest.release_id,
            digest,
            publication_token,
            rollout_target_sha256,
            prepare_evidence_sha256,
            final_evidence_sha256,
        )
        candidate = client.create_candidate(
            manifest_value,
            manifest_sha256=digest,
            client_request_id=journal.request_id("candidate.create"),
        )
        trusted_bootstrap_proof = None
        if manifest.channel is ReleaseChannel.STABLE and phase != "prepare":
            assert bootstrap_index_token is not None
            trusted_bootstrap_proof = client.trusted_bootstrap_index_proof(
                manifest.release_id
            )
            if (
                trusted_bootstrap_proof.proof_token != bootstrap_index_token
                or trusted_bootstrap_proof.version != manifest.version
                or trusted_bootstrap_proof.build_digest != manifest.build_digest
                or trusted_bootstrap_proof.revision != manifest.release_id
                or trusted_bootstrap_proof.target.manifest_sha256 != digest
                or trusted_bootstrap_proof.target.release_id != manifest.release_id
            ):
                raise ValueError("trusted Bootstrap proof does not match the release")
        candidate = client.record_gate_bundle(
            manifest.release_id,
            evidence,
            client_request_id=journal.request_id(f"gate-bundle.{bundle_phase}"),
        )
        rollout_id = journal.data.get("rollout_id")
        if rollout_id is None:
            rollout = client.create_rollout(
                manifest.release_id,
                percentage=int(rollout_target["percentage"]),
                organizations=list(rollout_target["organizations"]),
                accounts=list(rollout_target["accounts"]),
                minimum_compatible_version=rollout_target["minimum_compatible_version"],
                client_request_id=journal.request_id("rollout.create"),
            )
            journal.record_rollout(rollout.rollout_id)
        else:
            rollout = None
        rollout_id = str(journal.data["rollout_id"])
        if phase == "prepare":
            return {
                "phase": phase,
                "release": candidate.model_dump(mode="json"),
                "rollout": (
                    rollout.model_dump(mode="json") if rollout is not None else None
                ),
                "rollout_id": rollout_id,
                "activated": False,
                "journal": str(journal.path),
            }
        candidate = client.publish(
            manifest.release_id,
            client_request_id=journal.request_id("release.publish"),
        )
        if args.activate and not journal.data.get("activated"):
            rollout = client.rollout_action(
                rollout_id,
                "activate",
                client_request_id=journal.request_id("rollout.activate"),
            )
            journal.record_activated()
        return {
            "phase": phase,
            "release": candidate.model_dump(mode="json"),
            "rollout": rollout.model_dump(mode="json") if rollout is not None else None,
            "rollout_id": rollout_id,
            "activated": bool(journal.data.get("activated")),
            "bootstrap_proof": (
                trusted_bootstrap_proof.model_dump(mode="json")
                if trusted_bootstrap_proof is not None
                else None
            ),
            "journal": str(journal.path),
        }


def run(
    argv: list[str] | None = None,
    *,
    client_factory: ClientFactory = _client,
    github_publisher_factory: GitHubPublisherFactory = _github_publisher,
    publication_coordinator_factory: PublicationCoordinatorFactory = (
        _publication_coordinator
    ),
    public_bootstrap_publisher_factory: PublicBootstrapPublisherFactory = (
        _public_bootstrap_publisher
    ),
    public_pointer_signer_factory: PublicPointerSignerFactory = (
        _public_pointer_signer
    ),
    public_freshness_signer_factory: PublicFreshnessSignerFactory = (
        _public_freshness_signer
    ),
) -> int:
    args = _parser().parse_args(argv)
    client: AdminControlPlaneClient | None = None
    try:
        if args.command == "upload-github":
            github = github_publisher_factory(args)
            try:
                result = _upload_github(args, github)
            finally:
                github.close()
        elif args.command == "publish-assets":
            coordinator = publication_coordinator_factory(args)
            try:
                result = _publish_assets(args, coordinator)
            finally:
                coordinator.close()
        elif args.command == "build-public-bootstrap-index":
            result = _build_public_bootstrap_discovery(
                args,
                public_pointer_signer_factory(args),
                public_freshness_signer_factory(args),
            )
        elif args.command in {
            "stage-public-bootstrap-index",
            "activate-public-bootstrap-index",
        }:
            publisher = public_bootstrap_publisher_factory(args)
            try:
                if args.command == "stage-public-bootstrap-index":
                    result = _stage_public_bootstrap_discovery(args, publisher)
                else:
                    result = _activate_public_bootstrap_discovery(args, publisher)
            finally:
                publisher.close()
        elif args.command == "promote" and args.dry_run:
            result = _promote(args, None)
        else:
            client = client_factory(args)
            if args.command == "promote":
                result = _promote(args, client)
            elif args.command == "rollout":
                result = client.rollout_action(
                    args.rollout_id,
                    args.action,
                    client_request_id="release_" + secrets.token_hex(16),
                ).model_dump(mode="json")
            elif args.command == "kill-switch":
                result = client.set_kill_switch(
                    ReleaseChannel(args.channel),
                    active=args.state == "set",
                    client_request_id="release_" + secrets.token_hex(16),
                ).model_dump(mode="json")
            elif args.command == "bootstrap-freshness-status":
                result = client.bootstrap_freshness_status().model_dump(mode="json")
            elif args.command == "refresh-bootstrap-freshness":
                result = _manual_freshness_refresh(args, client)
            else:
                result = client.distribution().model_dump(mode="json")
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except Exception as error:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": error.__class__.__name__,
                    "message": str(error),
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1
    finally:
        if client is not None:
            client.close()


def main() -> None:
    raise SystemExit(run())


if __name__ == "__main__":
    main()
