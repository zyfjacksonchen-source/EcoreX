"""Fail-closed production Candidate assembly from attested platform stages."""

from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import tempfile
from typing import Any, Mapping
from urllib.parse import urlsplit

from ecorex._version import __version__
from ecorex.pack_catalog import (
    CAPABILITY_PACK_SERVICE_IDS,
    CAPABILITY_PACK_TOOL_IDS,
)
from ecorex.integration.pack_python import (
    PACK_PYTHON_MANIFEST,
    expected_pack_python_path,
    scan_pack_python_closure,
)
from ecorex.update import (
    Ed25519SignatureVerifier,
    ReleaseChannel,
    ReleaseManifest,
    ReleaseSource,
    SourceKind,
    SignatureEnvelope,
    verify_artifact_file,
    verify_artifact_signature,
    verify_manifest_signature,
)
from ecorex.update.manifest import portable_path_segment_key, validate_portable_path_segment

from .builder import ReleaseBuilder
from .dependency_lock import DependencyLockError, load_dependency_lock_manifest
from .errors import ReleaseBuildError
from .external_signer import DigestPinnedExternalSigner
from .models import (
    ArtifactBuildInput,
    ArtifactKind,
    BuiltRelease,
    CoreDeltaBuildInput,
    ReleaseBuildSpec,
    WebBundleBuildInput,
)
from .public_index import PublicBootstrapIndexError, stable_pointer_sequence
from .secret_scan import detect_secret
from .signing import SigningError


CANDIDATE_RECIPE_SCHEMA_VERSION = 1
STAGE_RECEIPT_SCHEMA_VERSION = 1
CANDIDATE_RECEIPT_SCHEMA_VERSION = 2
CANDIDATE_RECEIPT_SIGNING_DOMAIN = b"ecorex-candidate-build-receipt-v2\n"
STAGE_WORKFLOW_PATH = ".github/workflows/ecorex-v1-platform-stage.yml"
TARGETS = (("windows", "x64"), ("macos", "arm64"), ("macos", "x64"))
PACK_TOOLS: Mapping[str, tuple[str, ...]] = CAPABILITY_PACK_TOOL_IDS
PACK_SERVICES: Mapping[str, tuple[str, ...]] = CAPABILITY_PACK_SERVICE_IDS
PACK_REQUIRED_FILES: Mapping[str, tuple[str, ...]] = {
    "browser": ("__main__.py", "ecorex-pack.json"),
    "channels": ("ecorex-dependency-pack.json", "runtime-inventory.json"),
    "image": ("__main__.py", "ecorex-image-pack.json"),
    "ocr": ("ecorex-dependency-pack.json", "runtime-inventory.json"),
    "office": ("ecorex-dependency-pack.json", "runtime-inventory.json"),
}
STAGE_GATES: Mapping[str, frozenset[str]] = {
    "core": frozenset(
        {
            "runtime-launch",
            "loopback-health",
            "dependency-closure",
            "package-size",
            "supply-chain",
        }
    ),
    "bootstrap": frozenset({"bootstrap-launch", "toolchain", "supply-chain"}),
    "browser": frozenset(
        {"pack-contract", "browser-smoke", "process-isolation", "supply-chain"}
    ),
    "channels": frozenset(
        {"pack-contract", "connector-contract", "schema-smoke", "supply-chain"}
    ),
    "image": frozenset(
        {"pack-contract", "image-adapter-smoke", "provider-failure", "supply-chain"}
    ),
    "ocr": frozenset(
        {"pack-contract", "ocr-runtime-smoke", "model-closure", "supply-chain"}
    ),
    "office": frozenset(
        {"pack-contract", "office-format-smoke", "format-closure", "supply-chain"}
    ),
}
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_MAX_JSON_BYTES = 2 * 1024 * 1024
_MAX_STAGE_FILES = 50_000
_MAX_STAGE_BYTES = 2 * 1024 * 1024 * 1024
_MAX_SECRET_SCAN_BYTES = 4 * 1024 * 1024


class CandidateBuildError(RuntimeError):
    """Stable non-sensitive Candidate assembly error."""

    def __init__(self, code: str) -> None:
        if _SAFE_ID.fullmatch(code) is None:
            code = "candidate_build_failed"
        self.code = code
        super().__init__(code)


class StageTree:
    __slots__ = ("digest", "file_count", "files", "size_bytes")

    def __init__(
        self,
        *,
        digest: str,
        file_count: int,
        size_bytes: int,
        files: Mapping[str, Mapping[str, Any]],
    ) -> None:
        self.digest = digest
        self.file_count = file_count
        self.size_bytes = size_bytes
        self.files = dict(files)


def build_candidate(
    *,
    recipe_path: str | os.PathLike[str],
    input_root: str | os.PathLike[str],
    web_dist: str | os.PathLike[str],
    destination: str | os.PathLike[str],
    receipt_path: str | os.PathLike[str],
    expected_commit: str,
    expected_workflow_run_id: int,
    staging_provenance_path: str | os.PathLike[str],
    dependency_lock_manifest_path: str | os.PathLike[str],
    signer: DigestPinnedExternalSigner,
    delta_base_release_dir: str | os.PathLike[str] | None = None,
) -> BuiltRelease:
    """Verify every real stage, then delegate immutable bytes to ReleaseBuilder."""

    if _COMMIT.fullmatch(expected_commit) is None:
        raise CandidateBuildError("candidate_commit_invalid")
    if (
        isinstance(expected_workflow_run_id, bool)
        or not isinstance(expected_workflow_run_id, int)
        or expected_workflow_run_id < 1
    ):
        raise CandidateBuildError("candidate_staging_provenance_invalid")
    try:
        dependency_lock = load_dependency_lock_manifest(
            dependency_lock_manifest_path
        )
    except DependencyLockError:
        raise CandidateBuildError("candidate_dependency_lock_invalid") from None
    root = _real_directory(input_root, "candidate_input_root_invalid")
    provenance, provenance_sha256 = _validate_staging_provenance(
        root=root,
        value=staging_provenance_path,
        expected_commit=expected_commit,
        expected_workflow_run_id=expected_workflow_run_id,
    )
    recipe_file = _contained_regular_file(
        root,
        recipe_path,
        code="candidate_recipe_invalid",
    )
    recipe = _read_json(recipe_file, code="candidate_recipe_invalid")
    if not isinstance(recipe, dict) or set(recipe) != {
        "schema_version",
        "channel",
        "created_at",
        "sources",
        "inputs",
    }:
        raise CandidateBuildError("candidate_recipe_invalid")
    if recipe.get("schema_version") != CANDIDATE_RECIPE_SCHEMA_VERSION:
        raise CandidateBuildError("candidate_recipe_schema_unsupported")
    try:
        channel = ReleaseChannel(recipe.get("channel"))
    except (TypeError, ValueError):
        raise CandidateBuildError("candidate_channel_invalid") from None
    created_at = recipe.get("created_at")
    if not isinstance(created_at, str) or not created_at:
        raise CandidateBuildError("candidate_created_at_invalid")
    sources = _release_sources(recipe.get("sources"), channel=channel)
    raw_inputs = recipe.get("inputs")
    expected_stage_count = len(TARGETS) * (2 + len(PACK_TOOLS))
    if not isinstance(raw_inputs, list) or len(raw_inputs) != expected_stage_count:
        raise CandidateBuildError("candidate_stage_set_incomplete")

    with tempfile.TemporaryDirectory(prefix="ecorex-candidate-frozen-stages-") as raw_freeze:
        freeze_root = Path(raw_freeze).resolve(strict=True)
        definitions: list[ArtifactBuildInput] = []
        receipt_digests: dict[str, str] = {}
        observed: set[tuple[str, str, str]] = set()
        for raw in raw_inputs:
            definition, stage_key, stage_id, receipt_digest = _stage_input(
                root=root,
                value=raw,
                expected_commit=expected_commit,
                expected_workflow_run_id=expected_workflow_run_id,
                expected_workflow_run_attempt=int(provenance["run_attempt"]),
                freeze_root=freeze_root,
            )
            if stage_key in observed or stage_id in receipt_digests:
                raise CandidateBuildError("candidate_stage_identity_duplicate")
            observed.add(stage_key)
            receipt_digests[stage_id] = receipt_digest
            definitions.append(definition)
        expected = {
            ("core", platform, architecture)
            for platform, architecture in TARGETS
        }.union(
            {
                ("bootstrap", platform, architecture)
                for platform, architecture in TARGETS
            }
        ).union(
            {
                (pack_id, platform, architecture)
                for pack_id in PACK_TOOLS
                for platform, architecture in TARGETS
            }
        )
        if observed != expected:
            raise CandidateBuildError("candidate_stage_set_incomplete")
        _bind_bootstrap_minimum_stable(definitions, signer)
        _require_signer_trust(definitions, signer)
        delta_bases = (
            _verified_delta_bases(
                delta_base_release_dir,
                signer=signer,
                channel=channel,
            )
            if delta_base_release_dir is not None
            else ()
        )

        web_root = _real_directory(web_dist, "candidate_web_bundle_invalid")
        if not (web_root / "index.html").is_file():
            raise CandidateBuildError("candidate_web_bundle_invalid")
        spec = ReleaseBuildSpec(
            channel=channel,
            created_at=created_at,
            sources=sources,
            artifacts=tuple(definitions),
            web_bundle=WebBundleBuildInput(web_root),
            release_scoped_sources=True,
            dependency_lock_sha256=dependency_lock.sha256,
            core_delta_bases=delta_bases,
        )
        try:
            built = ReleaseBuilder(signer).build(spec, destination)
        except SigningError:
            raise CandidateBuildError("candidate_external_signing_failed") from None
        except ReleaseBuildError:
            # ReleaseBuilder validation messages may contain runner-local paths.
            raise CandidateBuildError("candidate_release_contract_failed") from None
        _write_candidate_receipt(
            path=Path(receipt_path),
            built=built,
            expected_commit=expected_commit,
            expected_workflow_run_id=expected_workflow_run_id,
            staging_provenance_sha256=provenance_sha256,
            staging_run_attempt=int(provenance["run_attempt"]),
            stage_receipts=receipt_digests,
            signer=signer,
            web_tree=scan_stage_tree(web_root),
            dependency_lock_sha256=dependency_lock.sha256,
        )
        return built


def _verified_delta_bases(
    release_dir: str | os.PathLike[str],
    *,
    signer: DigestPinnedExternalSigner,
    channel: ReleaseChannel,
) -> tuple[CoreDeltaBuildInput, ...]:
    """Authenticate the previous release before deriving optional deltas."""

    root = _real_directory(release_dir, "candidate_delta_base_invalid")
    manifest_path = root / "release-manifest.json"
    try:
        manifest = ReleaseManifest.from_json(
            _read_regular_bytes(
                manifest_path,
                code="candidate_delta_base_invalid",
            )
        )
        if manifest.channel is not channel or manifest.version == __version__:
            raise CandidateBuildError("candidate_delta_base_invalid")
        verifier = Ed25519SignatureVerifier(
            {signer.key_id: signer.public_key_bytes}
        )
        verify_manifest_signature(manifest, verifier)
        result: list[CoreDeltaBuildInput] = []
        for platform, architecture in TARGETS:
            artifact = manifest.artifact(f"core-{platform}-{architecture}")
            if (
                artifact.platform != platform
                or artifact.architecture != architecture
            ):
                raise CandidateBuildError("candidate_delta_base_invalid")
            verify_artifact_signature(manifest, artifact, verifier)
            package = root / artifact.file_name
            verify_artifact_file(package, manifest, artifact, verifier)
            result.append(
                CoreDeltaBuildInput(
                    base_manifest=manifest,
                    base_artifact=artifact,
                    base_package=package,
                )
            )
        return tuple(result)
    except CandidateBuildError:
        raise
    except Exception:
        raise CandidateBuildError("candidate_delta_base_invalid") from None


def write_stage_receipt(
    *,
    source_dir: str | os.PathLike[str],
    destination: str | os.PathLike[str],
    stage_id: str,
    commit_sha: str,
    workflow_run_id: int,
    workflow_run_attempt: int = 1,
    producer_executable_sha256: str,
    producer_adapter_sha256: str | None,
    kind: str,
    platform: str,
    architecture: str,
    pack_id: str | None,
    gate_evidence: Mapping[str, str],
) -> Path:
    """Write a typed receipt only after a real stage satisfies its contract."""

    if _SAFE_ID.fullmatch(stage_id) is None or _COMMIT.fullmatch(commit_sha) is None:
        raise CandidateBuildError("stage_receipt_identity_invalid")
    if (
        isinstance(workflow_run_id, bool)
        or not isinstance(workflow_run_id, int)
        or workflow_run_id < 1
        or isinstance(workflow_run_attempt, bool)
        or not isinstance(workflow_run_attempt, int)
        or workflow_run_attempt < 1
    ):
        raise CandidateBuildError("stage_receipt_producer_invalid")
    if _SHA256.fullmatch(producer_executable_sha256) is None or (
        producer_adapter_sha256 is not None
        and _SHA256.fullmatch(producer_adapter_sha256) is None
    ):
        raise CandidateBuildError("stage_receipt_producer_invalid")
    _validate_target(platform, architecture)
    normalized_kind = pack_id if kind == "capability-pack" else kind
    if kind == "core":
        if pack_id is not None:
            raise CandidateBuildError("stage_receipt_kind_invalid")
    elif kind == "bootstrap":
        if pack_id is not None:
            raise CandidateBuildError("stage_receipt_kind_invalid")
    elif kind == "capability-pack":
        if pack_id not in PACK_TOOLS:
            raise CandidateBuildError("stage_receipt_kind_invalid")
    else:
        raise CandidateBuildError("stage_receipt_kind_invalid")
    expected_gates = STAGE_GATES[normalized_kind]
    if set(gate_evidence) != expected_gates or any(
        _SHA256.fullmatch(str(value)) is None for value in gate_evidence.values()
    ):
        raise CandidateBuildError("stage_receipt_gates_incomplete")
    root = _real_directory(source_dir, "stage_source_invalid")
    tree = scan_stage_tree(root)
    runtime_interpreter = _validate_stage_payload(
        root=root,
        tree=tree,
        platform=platform,
        architecture=architecture,
        kind=kind,
        pack_id=pack_id,
    )
    value = {
        "schema_version": STAGE_RECEIPT_SCHEMA_VERSION,
        "receipt_type": "ecorex-candidate-stage",
        "stage_id": stage_id,
        "commit_sha": commit_sha,
        "producer": {
            "workflow_path": STAGE_WORKFLOW_PATH,
            "workflow_run_id": workflow_run_id,
            "workflow_run_attempt": workflow_run_attempt,
            "runner_os": platform,
            "stager_executable_sha256": producer_executable_sha256,
            "stager_adapter_sha256": producer_adapter_sha256,
        },
        "kind": kind,
        "platform": platform,
        "architecture": architecture,
        "pack_id": pack_id,
        "source_tree_sha256": tree.digest,
        "source_tree_file_count": tree.file_count,
        "source_tree_size_bytes": tree.size_bytes,
        "runtime_interpreter": runtime_interpreter,
        "install_projection": _stage_install_projection(
            kind,
            pack_id,
            platform=platform,
            architecture=architecture,
        ),
        "gates": {
            gate: {"status": "passed", "evidence_sha256": gate_evidence[gate]}
            for gate in sorted(gate_evidence)
        },
    }
    output = Path(destination).expanduser().resolve()
    _atomic_create_json(output, value)
    return output


def scan_stage_tree(source_dir: str | os.PathLike[str]) -> StageTree:
    root = _real_directory(source_dir, "stage_source_invalid")
    records: list[dict[str, Any]] = []
    files: dict[str, Mapping[str, Any]] = {}
    seen_paths: set[str] = set()
    entry_count = 0
    total = 0
    pending = [root]
    while pending:
        directory = pending.pop()
        try:
            entries = sorted(
                os.scandir(directory),
                key=lambda item: item.name.casefold(),
                reverse=True,
            )
        except OSError:
            raise CandidateBuildError("stage_source_unreadable") from None
        for entry in entries:
            entry_count += 1
            if entry_count > _MAX_STAGE_FILES:
                raise CandidateBuildError("stage_source_file_limit")
            path = Path(entry.path)
            try:
                metadata = path.lstat()
            except OSError:
                raise CandidateBuildError("stage_source_unreadable") from None
            reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
            if entry.is_symlink() or stat.S_ISLNK(metadata.st_mode) or bool(
                getattr(metadata, "st_file_attributes", 0) & reparse
            ):
                raise CandidateBuildError("stage_source_link_refused")
            relative = path.relative_to(root).as_posix()
            _validate_relative_path(relative)
            collision_key = "/".join(
                portable_path_segment_key(part) for part in PurePosixPath(relative).parts
            )
            if collision_key in seen_paths:
                raise CandidateBuildError("stage_source_path_collision")
            seen_paths.add(collision_key)
            if stat.S_ISDIR(metadata.st_mode):
                pending.append(path)
                continue
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size < 0:
                raise CandidateBuildError("stage_source_entry_invalid")
            total += metadata.st_size
            if total > _MAX_STAGE_BYTES:
                raise CandidateBuildError("stage_source_size_limit")
            sha256 = _stable_file_sha256(path, metadata, logical_path=relative)
            record = {
                "path": relative,
                "size_bytes": metadata.st_size,
                "sha256": sha256,
            }
            records.append(record)
            files[relative] = record
    if not records:
        raise CandidateBuildError("stage_source_empty")
    records.sort(key=lambda item: str(item["path"]))
    digest = hashlib.sha256(
        b"ecorex-candidate-stage-v1\n" + _canonical_json(records) + b"\n"
    ).hexdigest()
    return StageTree(
        digest=digest,
        file_count=len(records),
        size_bytes=total,
        files=files,
    )


def write_failure_receipt(
    path: str | os.PathLike[str],
    *,
    code: str,
    expected_commit: str | None,
) -> Path:
    normalized = code if _SAFE_ID.fullmatch(str(code)) else "candidate_build_failed"
    commit = expected_commit if isinstance(expected_commit, str) and _COMMIT.fullmatch(expected_commit) else None
    output = Path(path).expanduser().resolve()
    value = {
        "schema_version": CANDIDATE_RECEIPT_SCHEMA_VERSION,
        "receipt_type": "ecorex-candidate-build",
        "status": "failed",
        "code": normalized,
        "commit_sha": commit,
        "release_id": None,
    }
    _atomic_create_json(output, value)
    return output


def _validate_staging_provenance(
    *,
    root: Path,
    value: str | os.PathLike[str],
    expected_commit: str,
    expected_workflow_run_id: int,
) -> tuple[Mapping[str, Any], str]:
    path = _contained_regular_file(
        root,
        value,
        code="candidate_staging_provenance_invalid",
    )
    payload = _read_regular_bytes(
        path,
        code="candidate_staging_provenance_invalid",
    )
    try:
        receipt = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        raise CandidateBuildError("candidate_staging_provenance_invalid") from None
    expected = {
        "schema_version",
        "status",
        "workflow_path",
        "workflow_run_id",
        "run_attempt",
        "commit_sha",
        "repository",
        "metadata_sha256",
    }
    if (
        not isinstance(receipt, dict)
        or set(receipt) != expected
        or receipt.get("schema_version") != 1
        or receipt.get("status") != "passed"
        or receipt.get("workflow_path") != STAGE_WORKFLOW_PATH
        or receipt.get("workflow_run_id") != expected_workflow_run_id
        or isinstance(receipt.get("run_attempt"), bool)
        or not isinstance(receipt.get("run_attempt"), int)
        or receipt["run_attempt"] < 1
        or receipt.get("commit_sha") != expected_commit
        or not isinstance(receipt.get("repository"), str)
        or not receipt["repository"]
        or _SHA256.fullmatch(str(receipt.get("metadata_sha256"))) is None
    ):
        raise CandidateBuildError("candidate_staging_provenance_invalid")
    return receipt, hashlib.sha256(payload).hexdigest()


def _freeze_stage_tree(
    *,
    source: Path,
    destination: Path,
    expected: StageTree,
) -> Path:
    """Copy only receipt-bound files into a private immutable build input."""

    if os.path.lexists(destination):
        raise CandidateBuildError("candidate_stage_identity_duplicate")
    try:
        destination.mkdir(parents=False, exist_ok=False)
    except OSError:
        raise CandidateBuildError("candidate_stage_freeze_failed") from None
    for relative, record in sorted(expected.files.items()):
        pure = PurePosixPath(relative)
        _validate_relative_path(relative)
        target = destination.joinpath(*pure.parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        _copy_stage_file(
            source.joinpath(*pure.parts),
            target,
            expected_size=int(record["size_bytes"]),
            expected_sha256=str(record["sha256"]),
        )
    frozen = scan_stage_tree(destination)
    if (
        frozen.digest != expected.digest
        or frozen.file_count != expected.file_count
        or frozen.size_bytes != expected.size_bytes
        or frozen.files != expected.files
    ):
        raise CandidateBuildError("candidate_stage_freeze_mismatch")
    return destination


def _copy_stage_file(
    source: Path,
    destination: Path,
    *,
    expected_size: int,
    expected_sha256: str,
) -> None:
    digest = hashlib.sha256()
    written = 0
    try:
        before = source.lstat()
        reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        if (
            stat.S_ISLNK(before.st_mode)
            or bool(getattr(before, "st_file_attributes", 0) & reparse)
            or not stat.S_ISREG(before.st_mode)
            or before.st_size != expected_size
        ):
            raise OSError
        with source.open("rb") as input_stream, destination.open("xb") as output_stream:
            opened = os.fstat(input_stream.fileno())
            if _stat_identity(opened) != _stat_identity(before):
                raise OSError
            while chunk := input_stream.read(1024 * 1024):
                written += len(chunk)
                if written > expected_size:
                    raise OSError
                digest.update(chunk)
                output_stream.write(chunk)
            output_stream.flush()
            os.fsync(output_stream.fileno())
            after = os.fstat(input_stream.fileno())
        current = source.lstat()
    except OSError:
        raise CandidateBuildError("candidate_stage_changed_during_freeze") from None
    if (
        _stat_identity(opened) != _stat_identity(before)
        or _stat_identity(after) != _stat_identity(before)
        or _stat_identity(current) != _stat_identity(before)
        or written != expected_size
        or digest.hexdigest() != expected_sha256
    ):
        raise CandidateBuildError("candidate_stage_changed_during_freeze")


def _stage_input(
    *,
    root: Path,
    value: Any,
    expected_commit: str,
    expected_workflow_run_id: int,
    expected_workflow_run_attempt: int,
    freeze_root: Path,
) -> tuple[ArtifactBuildInput, tuple[str, str, str], str, str]:
    if not isinstance(value, dict) or set(value) != {"source_dir", "receipt"}:
        raise CandidateBuildError("candidate_stage_input_invalid")
    source = _contained_directory(root, value.get("source_dir"))
    receipt_file = _contained_regular_file(
        root,
        value.get("receipt"),
        code="candidate_stage_receipt_invalid",
    )
    receipt_bytes = _read_regular_bytes(receipt_file, code="candidate_stage_receipt_invalid")
    try:
        receipt = json.loads(receipt_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        raise CandidateBuildError("candidate_stage_receipt_invalid") from None
    expected_keys = {
        "schema_version",
        "receipt_type",
        "stage_id",
        "commit_sha",
        "producer",
        "kind",
        "platform",
        "architecture",
        "pack_id",
        "source_tree_sha256",
        "source_tree_file_count",
        "source_tree_size_bytes",
        "runtime_interpreter",
        "install_projection",
        "gates",
    }
    if (
        not isinstance(receipt, dict)
        or set(receipt) != expected_keys
        or receipt.get("schema_version") != STAGE_RECEIPT_SCHEMA_VERSION
        or receipt.get("receipt_type") != "ecorex-candidate-stage"
        or receipt.get("commit_sha") != expected_commit
    ):
        raise CandidateBuildError("candidate_stage_receipt_invalid")
    stage_id = receipt.get("stage_id")
    platform = receipt.get("platform")
    architecture = receipt.get("architecture")
    kind = receipt.get("kind")
    pack_id = receipt.get("pack_id")
    if not isinstance(stage_id, str) or _SAFE_ID.fullmatch(stage_id) is None:
        raise CandidateBuildError("candidate_stage_receipt_invalid")
    _validate_target(platform, architecture)
    normalized_kind = pack_id if kind == "capability-pack" else kind
    if (
        (kind == "core" and pack_id is not None)
        or (kind == "bootstrap" and pack_id is not None)
        or (kind == "capability-pack" and pack_id not in PACK_TOOLS)
        or normalized_kind not in STAGE_GATES
    ):
        raise CandidateBuildError("candidate_stage_kind_invalid")
    producer = receipt.get("producer")
    expected_runner = platform
    if (
        not isinstance(producer, dict)
        or set(producer)
        != {
            "workflow_path",
            "workflow_run_id",
            "workflow_run_attempt",
            "runner_os",
            "stager_executable_sha256",
            "stager_adapter_sha256",
        }
        or producer.get("workflow_path") != STAGE_WORKFLOW_PATH
        or isinstance(producer.get("workflow_run_id"), bool)
        or not isinstance(producer.get("workflow_run_id"), int)
        or producer["workflow_run_id"] != expected_workflow_run_id
        or isinstance(producer.get("workflow_run_attempt"), bool)
        or not isinstance(producer.get("workflow_run_attempt"), int)
        or producer["workflow_run_attempt"] != expected_workflow_run_attempt
        or producer.get("runner_os") != expected_runner
        or _SHA256.fullmatch(str(producer.get("stager_executable_sha256"))) is None
        or (
            producer.get("stager_adapter_sha256") is not None
            and _SHA256.fullmatch(str(producer.get("stager_adapter_sha256"))) is None
        )
    ):
        raise CandidateBuildError("candidate_stage_producer_invalid")
    gates = receipt.get("gates")
    if not isinstance(gates, dict) or set(gates) != STAGE_GATES[normalized_kind]:
        raise CandidateBuildError("candidate_stage_gates_incomplete")
    for gate in gates.values():
        if (
            not isinstance(gate, dict)
            or set(gate) != {"status", "evidence_sha256"}
            or gate.get("status") != "passed"
            or _SHA256.fullmatch(str(gate.get("evidence_sha256"))) is None
        ):
            raise CandidateBuildError("candidate_stage_gates_incomplete")
    tree = scan_stage_tree(source)
    if (
        receipt.get("source_tree_sha256") != tree.digest
        or receipt.get("source_tree_file_count") != tree.file_count
        or receipt.get("source_tree_size_bytes") != tree.size_bytes
    ):
        raise CandidateBuildError("candidate_stage_tree_mismatch")
    runtime_interpreter = _validate_stage_payload(
        root=source,
        tree=tree,
        platform=platform,
        architecture=architecture,
        kind=kind,
        pack_id=pack_id,
    )
    if receipt.get("runtime_interpreter") != runtime_interpreter:
        raise CandidateBuildError("candidate_stage_runtime_interpreter_mismatch")
    if receipt.get("install_projection") != _stage_install_projection(
        kind,
        pack_id,
        platform=platform,
        architecture=architecture,
    ):
        raise CandidateBuildError("candidate_stage_install_projection_mismatch")
    frozen_source = _freeze_stage_tree(
        source=source,
        destination=freeze_root / stage_id,
        expected=tree,
    )
    if kind == "core":
        executable = "bin/ecorex.exe" if platform == "windows" else "bin/ecorex"
        executable_paths = (executable,)
        if platform == "macos":
            executable_paths += ("bin/pack-python/bin/python3",)
        definition = ArtifactBuildInput(
            source_dir=frozen_source,
            kind=ArtifactKind.CORE,
            platform=platform,
            architecture=architecture,
            executable_paths=executable_paths,
            product_runtime=True,
        )
        stage_key = ("core", platform, architecture)
    elif kind == "bootstrap":
        executable = (
            "bin/ecorex-bootstrap.exe"
            if platform == "windows"
            else "bin/ecorex-bootstrap"
        )
        installer = (
            "EcoreX Installer.cmd"
            if platform == "windows"
            else "EcoreX Installer.command"
        )
        definition = ArtifactBuildInput(
            source_dir=frozen_source,
            kind=ArtifactKind.BOOTSTRAP,
            platform=platform,
            architecture=architecture,
            executable_paths=(
                executable,
                *((installer,) if platform == "macos" else ()),
            ),
        )
        stage_key = ("bootstrap", platform, architecture)
    else:
        definition = ArtifactBuildInput(
            source_dir=frozen_source,
            kind=ArtifactKind.CAPABILITY_PACK,
            platform=platform,
            architecture=architecture,
            executable_paths=("__main__.py",) if pack_id == "browser" else (),
            pack_id=pack_id,
            pack_tool_ids=PACK_TOOLS[str(pack_id)],
            pack_service_ids=PACK_SERVICES[str(pack_id)],
            runtime_api_version="1.0.0",
        )
        stage_key = (str(pack_id), platform, architecture)
    return (
        definition,
        stage_key,
        stage_id,
        hashlib.sha256(receipt_bytes).hexdigest(),
    )


def _validate_stage_payload(
    *,
    root: Path,
    tree: StageTree,
    platform: str,
    architecture: str,
    kind: str,
    pack_id: str | None,
) -> Mapping[str, Any] | None:
    if kind == "bootstrap":
        launcher = (
            "bin/ecorex-bootstrap.exe"
            if platform == "windows"
            else "bin/ecorex-bootstrap"
        )
        installer = (
            "EcoreX Installer.cmd"
            if platform == "windows"
            else "EcoreX Installer.command"
        )
        required = (launcher, installer, "bootstrap-config.json")
        if platform == "windows":
            required += ("bin/ecorex-sandbox-host.exe",)
    elif pack_id is None:
        launcher = "bin/ecorex.exe" if platform == "windows" else "bin/ecorex"
        interpreter_path = expected_pack_python_path(platform)
        required = (
            launcher,
            "runtime-config.json",
            PACK_PYTHON_MANIFEST,
            interpreter_path,
        )
        if platform == "windows":
            # The product-owned AppContainer/Job Object helper belongs to the
            # immutable Runtime slot. A Windows Core without it is safe only by
            # disabling shell, not a releasable full v1 Candidate.
            required += ("bin/ecorex-sandbox-host.exe",)
    else:
        required = PACK_REQUIRED_FILES[pack_id]
    if any(path not in tree.files for path in required):
        raise CandidateBuildError("stage_required_binary_missing")
    runtime_interpreter: Mapping[str, Any] | None = None
    if kind == "bootstrap":
        installer_payload = (
            b"@echo off\r\n"
            b"\"%~dp0bin\\ecorex-bootstrap.exe\" %*\r\n"
            b"exit /b %errorlevel%\r\n"
            if platform == "windows"
            else (
                b"#!/bin/sh\n"
                b"BASE_DIR=$(CDPATH= cd -- \"$(dirname -- \"$0\")\" && pwd)\n"
                b"exec \"$BASE_DIR/bin/ecorex-bootstrap\" \"$@\"\n"
            )
        )
        if (root / installer).read_bytes() != installer_payload:
            raise CandidateBuildError("stage_bootstrap_installer_invalid")
        bootstrap_config = _validate_bootstrap_config(
            root / "bootstrap-config.json",
            platform=platform,
        )
        if (
            platform == "windows"
            and bootstrap_config["sandbox_helper_sha256"]
            != tree.files["bin/ecorex-sandbox-host.exe"]["sha256"]
        ):
            raise CandidateBuildError("stage_bootstrap_config_invalid")
    elif pack_id is None:
        descriptor = _read_json(
            root / PACK_PYTHON_MANIFEST,
            code="stage_pack_python_contract_invalid",
        )
        expected_keys = {
            "schema_version",
            "platform",
            "architecture",
            "relative_path",
            "size_bytes",
            "sha256",
            "closure_file_count",
            "closure_size_bytes",
            "closure_sha256",
        }
        interpreter_record = tree.files[interpreter_path]
        try:
            closure = scan_pack_python_closure(root / "bin" / "pack-python")
        except Exception:
            raise CandidateBuildError("stage_pack_python_contract_invalid") from None
        if (
            not isinstance(descriptor, dict)
            or set(descriptor) != expected_keys
            or descriptor.get("schema_version") != 1
            or descriptor.get("platform") != platform
            or descriptor.get("architecture") != architecture
            or descriptor.get("relative_path") != interpreter_path
            or descriptor.get("size_bytes") != interpreter_record["size_bytes"]
            or descriptor.get("sha256") != interpreter_record["sha256"]
            or descriptor.get("closure_file_count") != closure["file_count"]
            or descriptor.get("closure_size_bytes") != closure["size_bytes"]
            or descriptor.get("closure_sha256") != closure["sha256"]
        ):
            raise CandidateBuildError("stage_pack_python_contract_invalid")
        runtime_interpreter = {
            key: descriptor[key]
            for key in (
                "relative_path",
                "size_bytes",
                "sha256",
                "closure_file_count",
                "closure_size_bytes",
                "closure_sha256",
            )
        }
        try:
            from ecorex.server.config import ProductRuntimeConfig

            runtime_config = ProductRuntimeConfig.from_bytes(
                _read_regular_bytes(
                    root / "runtime-config.json",
                    code="stage_runtime_config_invalid",
                )
            )
        except Exception:
            raise CandidateBuildError("stage_runtime_config_invalid") from None
        expected_pack_paths = tuple(
            (
                pack,
                (
                    f"capability-packs/{pack}/ecorex-capability-pack-{pack}-"
                    f"{platform}-{architecture}-{__version__}.json"
                ),
                (
                    f"capability-packs/{pack}/ecorex-capability-pack-{pack}-"
                    f"{platform}-{architecture}-{__version__}.zip"
                ),
            )
            for pack in PACK_TOOLS
        )
        if (
            runtime_config.identity.version != __version__
            or runtime_config.identity.platform != platform
            or runtime_config.identity.architecture != architecture
            or tuple(
                (definition.pack_id, definition.manifest, definition.artifact)
                for definition in runtime_config.capability_packs
            )
            != expected_pack_paths
        ):
            raise CandidateBuildError("stage_runtime_pack_projection_invalid")
    if pack_id == "browser":
        descriptor = _read_json(root / "ecorex-pack.json", code="stage_pack_contract_invalid")
        expected_tools = list(PACK_TOOLS[pack_id])
        if (
            not isinstance(descriptor, dict)
            or descriptor.get("schema_version") != 1
            or descriptor.get("protocol") != "ecorex-stdio-tool-v1"
            or descriptor.get("pack_id") != pack_id
            or descriptor.get("runtime_api_version") != "1.0.0"
            or descriptor.get("tools") != expected_tools
        ):
            raise CandidateBuildError("stage_pack_contract_invalid")
    elif pack_id == "image":
        descriptor = _read_json(
            root / "ecorex-image-pack.json", code="stage_pack_contract_invalid"
        )
        if (
            not isinstance(descriptor, dict)
            or set(descriptor) != {
                "schema_version",
                "pack_id",
                "runtime_api_version",
                "tools",
                "adapter",
            }
            or descriptor.get("schema_version") != 1
            or descriptor.get("pack_id") != "image"
            or descriptor.get("runtime_api_version") != "1.0.0"
            or descriptor.get("tools") != list(PACK_TOOLS["image"])
            or descriptor.get("adapter") != "core-managed-image-v1"
        ):
            raise CandidateBuildError("stage_pack_contract_invalid")
    elif pack_id in {"channels", "ocr", "office"}:
        descriptor = _read_json(
            root / "ecorex-dependency-pack.json",
            code="stage_pack_contract_invalid",
        )
        inventory = _read_json(
            root / "runtime-inventory.json",
            code="stage_pack_contract_invalid",
        )
        expected_adapter = {
            "channels": "managed-channel-contracts-v1",
            "ocr": "python-rapidocr-runtime-v1",
            "office": "python-office-formats-v1",
        }[pack_id]
        if (
            not isinstance(descriptor, dict)
            or set(descriptor)
            != {
                "schema_version",
                "pack_id",
                "runtime_api_version",
                "kind",
                "services",
                "adapter",
                "inventory",
            }
            or descriptor.get("schema_version") != 1
            or descriptor.get("pack_id") != pack_id
            or descriptor.get("runtime_api_version") != "1.0.0"
            or descriptor.get("kind") != "dependency-service"
            or descriptor.get("services") != list(PACK_SERVICES[pack_id])
            or descriptor.get("adapter") != expected_adapter
            or descriptor.get("inventory") != "runtime-inventory.json"
            or not isinstance(inventory, dict)
            or set(inventory)
            != {"schema_version", "pack_id", "distributions", "payload_sha256"}
            or inventory.get("schema_version") != 1
            or inventory.get("pack_id") != pack_id
            or not isinstance(inventory.get("distributions"), list)
            or _SHA256.fullmatch(str(inventory.get("payload_sha256"))) is None
        ):
            raise CandidateBuildError("stage_pack_contract_invalid")
    return runtime_interpreter


def _stage_install_projection(
    kind: str,
    pack_id: str | None,
    *,
    platform: str,
    architecture: str,
) -> Mapping[str, Any]:
    """Declare the eventual atomic slot layout without claiming installation."""

    if kind == "bootstrap":
        return {
            "scope": "bootstrap",
            "entrypoint": (
                "bin/ecorex-bootstrap.exe"
                if platform == "windows"
                else "bin/ecorex-bootstrap"
            ),
            "installs": "core-plus-required-packs",
        }
    if pack_id is None:
        return {
            "scope": "slot-payload",
            "payload_root": ".",
            "required_pack_ids": list(PACK_TOOLS),
        }
    return {
        "scope": "slot-payload",
        "pack_id": pack_id,
        "manifest_relative_path": (
            f"capability-packs/{pack_id}/ecorex-capability-pack-{pack_id}-"
            f"{platform}-{architecture}-{__version__}.json"
        ),
        "artifact_relative_path": (
            f"capability-packs/{pack_id}/ecorex-capability-pack-{pack_id}-"
            f"{platform}-{architecture}-{__version__}.zip"
        ),
    }


def _stable_release_sequence(version: str) -> int:
    try:
        return stable_pointer_sequence(version)
    except PublicBootstrapIndexError:
        raise CandidateBuildError("stage_bootstrap_config_invalid")


def _minimum_stable_payload(sequence: int, version: str) -> bytes:
    return b"\0".join(
        (
            b"ecorex.bootstrap-minimum-stable.v1",
            str(sequence).encode("ascii"),
            version.encode("ascii"),
        )
    )


def _bind_bootstrap_minimum_stable(
    definitions: list[ArtifactBuildInput],
    signer: DigestPinnedExternalSigner,
) -> None:
    sequence = _stable_release_sequence(__version__)
    payload = _minimum_stable_payload(sequence, __version__)
    try:
        signature = signer.sign(payload)
    except Exception:
        raise CandidateBuildError("candidate_external_signing_failed") from None
    if not isinstance(signature, bytes) or len(signature) != 64:
        raise CandidateBuildError("candidate_external_signing_failed")
    minimum = {
        "sequence": sequence,
        "version": __version__,
        "signature": {
            "algorithm": "ed25519",
            "key_id": signer.key_id,
            "value": base64.b64encode(signature).decode("ascii"),
        },
    }
    found = 0
    for definition in definitions:
        if definition.kind is not ArtifactKind.BOOTSTRAP:
            continue
        found += 1
        path = Path(definition.source_dir) / "bootstrap-config.json"
        value = dict(
            _validate_bootstrap_config(
                path,
                platform=definition.platform,
                require_minimum_stable=False,
            )
        )
        if value.get("minimum_stable") is not None:
            raise CandidateBuildError("stage_bootstrap_config_invalid")
        value["minimum_stable"] = minimum
        _atomic_replace_json(path, value)
        _validate_bootstrap_config(
            path,
            platform=definition.platform,
            require_minimum_stable=True,
        )
    if found != len(TARGETS):
        raise CandidateBuildError("candidate_release_trust_mismatch")


def _validate_bootstrap_config(
    path: Path,
    *,
    platform: str,
    require_minimum_stable: bool = False,
) -> Mapping[str, Any]:
    value = _read_json(path, code="stage_bootstrap_config_invalid")
    if not isinstance(value, dict) or set(value) != {
        "schema_version",
        "public_index_url",
        "release_public_keys",
        "publication_public_keys",
        "sandbox_helper_sha256",
        "minimum_stable",
    }:
        raise CandidateBuildError("stage_bootstrap_config_invalid")
    url = value.get("public_index_url")
    parsed = urlsplit(url if isinstance(url, str) else "")
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.port not in {None, 443}
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or not parsed.path
    ):
        raise CandidateBuildError("stage_bootstrap_config_invalid")
    keys = value.get("release_public_keys")
    publication_keys = value.get("publication_public_keys")
    sandbox_helper_sha256 = value.get("sandbox_helper_sha256")
    if (
        value.get("schema_version") != 1
        or not isinstance(keys, dict)
        or not 1 <= len(keys) <= 8
        or not isinstance(publication_keys, dict)
        or not 1 <= len(publication_keys) <= 8
        or (
            platform == "windows"
            and _SHA256.fullmatch(str(sandbox_helper_sha256)) is None
        )
        or (platform == "macos" and sandbox_helper_sha256 != "")
        or platform not in {"windows", "macos"}
    ):
        raise CandidateBuildError("stage_bootstrap_config_invalid")
    for key_id, encoded in keys.items():
        if not isinstance(key_id, str) or _SAFE_ID.fullmatch(key_id) is None:
            raise CandidateBuildError("stage_bootstrap_config_invalid")
        if not isinstance(encoded, str):
            raise CandidateBuildError("stage_bootstrap_config_invalid")
        try:
            raw = base64.b64decode(encoded, validate=True)
        except (TypeError, ValueError):
            raise CandidateBuildError("stage_bootstrap_config_invalid") from None
        if len(raw) != 32 or base64.b64encode(raw).decode("ascii") != encoded:
            raise CandidateBuildError("stage_bootstrap_config_invalid")
    decoded_release = {
        key_id: base64.b64decode(encoded, validate=True)
        for key_id, encoded in keys.items()
    }
    decoded_publication: dict[str, bytes] = {}
    for key_id, encoded in publication_keys.items():
        if not isinstance(key_id, str) or _SAFE_ID.fullmatch(key_id) is None:
            raise CandidateBuildError("stage_bootstrap_config_invalid")
        if not isinstance(encoded, str):
            raise CandidateBuildError("stage_bootstrap_config_invalid")
        try:
            raw = base64.b64decode(encoded, validate=True)
        except (TypeError, ValueError):
            raise CandidateBuildError("stage_bootstrap_config_invalid") from None
        if len(raw) != 32 or base64.b64encode(raw).decode("ascii") != encoded:
            raise CandidateBuildError("stage_bootstrap_config_invalid")
        decoded_publication[key_id] = raw
    if set(decoded_release).intersection(decoded_publication) or set(
        decoded_release.values()
    ).intersection(decoded_publication.values()):
        raise CandidateBuildError("stage_bootstrap_config_invalid")
    minimum = value.get("minimum_stable")
    if minimum is None:
        if require_minimum_stable:
            raise CandidateBuildError("stage_bootstrap_config_invalid")
        return value
    if not isinstance(minimum, dict) or set(minimum) != {
        "sequence",
        "version",
        "signature",
    }:
        raise CandidateBuildError("stage_bootstrap_config_invalid")
    sequence = minimum.get("sequence")
    version = minimum.get("version")
    signature_raw = minimum.get("signature")
    if (
        isinstance(sequence, bool)
        or not isinstance(sequence, int)
        or version != __version__
        or sequence != _stable_release_sequence(__version__)
        or not isinstance(signature_raw, Mapping)
    ):
        raise CandidateBuildError("stage_bootstrap_config_invalid")
    try:
        signature = SignatureEnvelope.from_dict(signature_raw)
        raw_keys = {
            key_id: base64.b64decode(encoded, validate=True)
            for key_id, encoded in keys.items()
        }
        verdict = Ed25519SignatureVerifier(raw_keys).verify(
            _minimum_stable_payload(sequence, version),
            signature,
        )
    except Exception:
        raise CandidateBuildError("stage_bootstrap_config_invalid") from None
    if verdict is not True:
        raise CandidateBuildError("stage_bootstrap_config_invalid")
    return value


def _require_signer_trust(
    definitions: list[ArtifactBuildInput],
    signer: DigestPinnedExternalSigner,
) -> None:
    """Bind every Runtime and Bootstrap trust ring to the actual KMS signer."""

    try:
        from ecorex.server.config import ProductRuntimeConfig

        cores: dict[tuple[str, str], Mapping[str, bytes]] = {}
        bootstraps: dict[tuple[str, str], Mapping[str, Any]] = {}
        for definition in definitions:
            target = (definition.platform, definition.architecture)
            if definition.kind is ArtifactKind.CORE:
                config = ProductRuntimeConfig.from_bytes(
                    _read_regular_bytes(
                        Path(definition.source_dir) / "runtime-config.json",
                        code="candidate_release_trust_mismatch",
                    )
                )
                cores[target] = config.release_public_keys
            elif definition.kind is ArtifactKind.BOOTSTRAP:
                bootstraps[target] = _validate_bootstrap_config(
                    Path(definition.source_dir) / "bootstrap-config.json",
                    platform=definition.platform,
                    require_minimum_stable=True,
                )
        if set(cores) != set(TARGETS) or set(bootstraps) != set(TARGETS):
            raise CandidateBuildError("candidate_release_trust_mismatch")
        index_urls: set[str] = set()
        publication_keyrings: set[bytes] = set()
        for target in TARGETS:
            core_keys = dict(cores[target])
            bootstrap = bootstraps[target]
            bootstrap_keys = {
                key_id: base64.b64decode(encoded, validate=True)
                for key_id, encoded in bootstrap["release_public_keys"].items()
            }
            if core_keys != bootstrap_keys:
                raise CandidateBuildError("candidate_release_trust_mismatch")
            if core_keys.get(signer.key_id) != signer.public_key_bytes:
                raise CandidateBuildError("candidate_release_trust_mismatch")
            publication_keyrings.add(
                _canonical_json(bootstrap["publication_public_keys"])
            )
            index_urls.add(str(bootstrap["public_index_url"]))
        if len(index_urls) != 1 or len(publication_keyrings) != 1:
            raise CandidateBuildError("candidate_release_trust_mismatch")
    except CandidateBuildError:
        raise
    except Exception:
        raise CandidateBuildError("candidate_release_trust_mismatch") from None


def _release_sources(
    value: Any, *, channel: ReleaseChannel
) -> tuple[ReleaseSource, ...]:
    if not isinstance(value, list) or len(value) != 3:
        raise CandidateBuildError("candidate_sources_invalid")
    kinds = (
        SourceKind.GITHUB_CN_MIRROR,
        SourceKind.GITHUB_RELEASE,
        SourceKind.ECOREX_CDN,
    )
    sources: list[ReleaseSource] = []
    for priority, (raw, expected_kind) in enumerate(zip(value, kinds, strict=True)):
        if not isinstance(raw, dict) or set(raw) != {"source_id", "kind", "base_url"}:
            raise CandidateBuildError("candidate_sources_invalid")
        if raw.get("kind") != expected_kind.value:
            raise CandidateBuildError("candidate_sources_invalid")
        base_url = raw.get("base_url")
        parsed = urlsplit(base_url if isinstance(base_url, str) else "")
        channel_suffix = channel.value
        expected_suffixes = (
            ("/releases/download",)
            if expected_kind is SourceKind.GITHUB_RELEASE
            else (
                # A GitHub CN accelerator is a read-through proxy, not an
                # uploadable replica.  Its root therefore mirrors the GitHub
                # ``releases/download`` namespace and is resolved to the same
                # immutable tag as the GitHub origin by ReleaseBuilder.
                # Dedicated EcoreX mirrors keep the release-id namespace.
                ("/releases/download", f"/v{__version__}/{channel_suffix}")
                if expected_kind is SourceKind.GITHUB_CN_MIRROR
                # The production CDN exposes one immutable version namespace.
                # Channel remains durable replica state, but is not part of
                # the signed public URL consumed by clients.
                else (f"/v{__version__}",)
            )
        )
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.port not in {None, 443}
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or not any(
                parsed.path.rstrip("/").endswith(suffix)
                for suffix in expected_suffixes
            )
        ):
            raise CandidateBuildError("candidate_sources_invalid")
        try:
            sources.append(
                ReleaseSource(
                    raw.get("source_id"), expected_kind, priority, base_url.rstrip("/")
                )
            )
        except (TypeError, ValueError):
            raise CandidateBuildError("candidate_sources_invalid") from None
    return tuple(sources)


def _validate_target(platform: Any, architecture: Any) -> None:
    if (platform, architecture) not in TARGETS:
        raise CandidateBuildError("candidate_target_invalid")


def _contained_directory(root: Path, value: Any) -> Path:
    if not isinstance(value, str) or not value:
        raise CandidateBuildError("candidate_stage_path_invalid")
    relative = PurePosixPath(value.replace("\\", "/"))
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        raise CandidateBuildError("candidate_stage_path_invalid")
    path = (root / Path(*relative.parts)).resolve(strict=True)
    try:
        path.relative_to(root)
    except ValueError:
        raise CandidateBuildError("candidate_stage_path_escape") from None
    return _real_directory(path, "candidate_stage_path_invalid")


def _contained_regular_file(root: Path, value: Any, *, code: str) -> Path:
    if isinstance(value, os.PathLike):
        value = os.fspath(value)
    if not isinstance(value, str) or not value:
        raise CandidateBuildError(code)
    candidate = Path(value)
    if candidate.is_absolute():
        path = candidate.resolve(strict=True)
    else:
        relative = PurePosixPath(value.replace("\\", "/"))
        if any(part in {"", ".", ".."} for part in relative.parts):
            raise CandidateBuildError(code)
        path = (root / Path(*relative.parts)).resolve(strict=True)
    try:
        path.relative_to(root)
    except ValueError:
        raise CandidateBuildError(code) from None
    _read_regular_bytes(path, code=code)
    return path


def _real_directory(value: str | os.PathLike[str], code: str) -> Path:
    try:
        raw = Path(value).expanduser()
        metadata = raw.lstat()
    except (TypeError, OSError):
        raise CandidateBuildError(code) from None
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    if (
        stat.S_ISLNK(metadata.st_mode)
        or bool(getattr(metadata, "st_file_attributes", 0) & reparse)
        or not stat.S_ISDIR(metadata.st_mode)
    ):
        raise CandidateBuildError(code)
    return raw.resolve(strict=True)


def _read_json(path: Path, *, code: str) -> Any:
    payload = _read_regular_bytes(path, code=code)
    try:
        return json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        raise CandidateBuildError(code) from None


def _read_regular_bytes(path: Path, *, code: str) -> bytes:
    try:
        before = path.lstat()
        reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        if (
            stat.S_ISLNK(before.st_mode)
            or bool(getattr(before, "st_file_attributes", 0) & reparse)
            or not stat.S_ISREG(before.st_mode)
            or not 1 <= before.st_size <= _MAX_JSON_BYTES
        ):
            raise CandidateBuildError(code)
        with path.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            if _stat_identity(opened) != _stat_identity(before):
                raise CandidateBuildError(code)
            payload = stream.read(_MAX_JSON_BYTES + 1)
            after = os.fstat(stream.fileno())
        current = path.lstat()
    except CandidateBuildError:
        raise
    except OSError:
        raise CandidateBuildError(code) from None
    identity = _stat_identity(before)
    if (
        len(payload) != before.st_size
        or _stat_identity(opened) != identity
        or _stat_identity(after) != identity
        or _stat_identity(current) != identity
    ):
        raise CandidateBuildError(code)
    return payload


def _stable_file_sha256(
    path: Path,
    before: os.stat_result,
    *,
    logical_path: str,
) -> str:
    digest = hashlib.sha256()
    scan_payload = bytearray() if before.st_size <= _MAX_SECRET_SCAN_BYTES else None
    try:
        with path.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            if _stat_identity(opened) != _stat_identity(before):
                raise CandidateBuildError("stage_source_changed")
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
                if scan_payload is not None:
                    scan_payload.extend(chunk)
            after = os.fstat(stream.fileno())
        current = path.lstat()
    except CandidateBuildError:
        raise
    except OSError:
        raise CandidateBuildError("stage_source_unreadable") from None
    identity = _stat_identity(before)
    if _stat_identity(opened) != identity or _stat_identity(after) != identity or _stat_identity(current) != identity:
        raise CandidateBuildError("stage_source_changed")
    if scan_payload is not None and detect_secret(bytes(scan_payload), logical_path):
        raise CandidateBuildError("stage_source_secret_detected")
    return digest.hexdigest()


def _validate_relative_path(value: str) -> None:
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts:
        raise CandidateBuildError("stage_source_path_invalid")
    for part in path.parts:
        try:
            validate_portable_path_segment(part)
        except (TypeError, ValueError):
            raise CandidateBuildError("stage_source_path_invalid") from None


def _write_candidate_receipt(
    *,
    path: Path,
    built: BuiltRelease,
    expected_commit: str,
    expected_workflow_run_id: int,
    staging_provenance_sha256: str,
    staging_run_attempt: int,
    stage_receipts: Mapping[str, str],
    signer: DigestPinnedExternalSigner,
    web_tree: StageTree,
    dependency_lock_sha256: str,
) -> None:
    manifest_bytes = built.manifest_path.read_bytes()
    receipts = signer.receipts
    if not receipts:
        raise CandidateBuildError("candidate_signature_receipt_missing")
    # The receipt is a second, explicit trust object.  It is not sufficient to
    # merely mention the signed manifest: the complete CI provenance and all
    # stage receipt digests must themselves be authenticated.  Predicting the
    # one additional signing operation avoids a circular signature field while
    # keeping the external-signer audit count exact.
    value: dict[str, Any] = {
        "schema_version": CANDIDATE_RECEIPT_SCHEMA_VERSION,
        "receipt_type": "ecorex-candidate-build",
        "status": "passed",
        "code": None,
        "commit_sha": expected_commit,
        "staging_provenance": {
            "workflow_path": STAGE_WORKFLOW_PATH,
            "workflow_run_id": expected_workflow_run_id,
            "run_attempt": staging_run_attempt,
            "receipt_sha256": staging_provenance_sha256,
        },
        "release_id": built.manifest.release_id,
        "version": built.manifest.version,
        "channel": built.manifest.channel.value,
        "build_digest": built.manifest.build_digest,
        "python_dependency_lock_sha256": dependency_lock_sha256,
        "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "web_tree_sha256": web_tree.digest,
        "stage_receipts": dict(sorted(stage_receipts.items())),
        "artifacts": {
            artifact.artifact_id: {
                "file_name": artifact.file_name,
                "size_bytes": artifact.size_bytes,
                "sha256": artifact.sha256,
            }
            for artifact in sorted(
                built.manifest.artifacts, key=lambda item: item.artifact_id
            )
        },
        "signing": {
            "algorithm": "ed25519",
            "key_id": signer.key_id,
            "operation_count": len(receipts) + 1,
            "executable_sha256": receipts[0].executable_sha256,
            "adapter_sha256": receipts[0].adapter_sha256,
        },
    }
    payload = candidate_receipt_signing_payload(value)
    try:
        detached = signer.sign(payload)
    except SigningError:
        raise CandidateBuildError("candidate_receipt_signing_failed") from None
    if len(signer.receipts) != value["signing"]["operation_count"]:
        raise CandidateBuildError("candidate_receipt_signing_failed")
    value["signature"] = {
        "algorithm": "ed25519",
        "key_id": signer.key_id,
        "value": base64.b64encode(detached).decode("ascii"),
    }
    _atomic_create_json(path.expanduser().resolve(), value)


def candidate_receipt_signing_payload(value: Mapping[str, Any]) -> bytes:
    """Return the canonical, domain-separated unsigned v2 receipt bytes."""

    unsigned = dict(value)
    unsigned.pop("signature", None)
    return CANDIDATE_RECEIPT_SIGNING_DOMAIN + _canonical_json(unsigned) + b"\n"


def _atomic_create_json(path: Path, value: Mapping[str, Any]) -> None:
    if os.path.lexists(path):
        raise CandidateBuildError("candidate_receipt_already_exists")
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = _canonical_json(value) + b"\n"
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        with temporary.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _atomic_replace_json(path: Path, value: Mapping[str, Any]) -> None:
    if not path.is_file() or path.is_symlink():
        raise CandidateBuildError("stage_bootstrap_config_invalid")
    payload = _canonical_json(value) + b"\n"
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        with temporary.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except OSError:
        raise CandidateBuildError("stage_bootstrap_config_invalid") from None
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _canonical_json(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError):
        raise CandidateBuildError("candidate_json_invalid") from None


def _stat_identity(value: os.stat_result) -> tuple[int, int, int, int]:
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns


__all__ = [
    "CANDIDATE_RECEIPT_SCHEMA_VERSION",
    "CANDIDATE_RECEIPT_SIGNING_DOMAIN",
    "CANDIDATE_RECIPE_SCHEMA_VERSION",
    "CandidateBuildError",
    "PACK_TOOLS",
    "PACK_SERVICES",
    "STAGE_GATES",
    "STAGE_RECEIPT_SCHEMA_VERSION",
    "STAGE_WORKFLOW_PATH",
    "TARGETS",
    "build_candidate",
    "candidate_receipt_signing_payload",
    "scan_stage_tree",
    "write_failure_receipt",
    "write_stage_receipt",
]
