"""Install-time Windows AppContainer permission-domain provisioning.

The signed Bootstrap helper provisions fresh empty slot roots before Core and
Capability Pack extraction.  The Runtime helper then attests the inherited
tree.  Stable workspace permissions belong to the canonical workspace-root
set, not to an individual slot, so failed-slot cleanup never tears down a
permission domain still referenced by current/previous/known-good slots.
"""

from __future__ import annotations

from collections.abc import Mapping
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import tempfile
from typing import Any
import zipfile

from ecorex.update import ReleaseArtifact, ReleaseManifest, SlotStore
from ecorex.update.storage import (
    _durable_replace,
    _fsync_directory,
    atomic_write_json,
)

from .sandbox import (
    SANDBOX_LAUNCH_PROTOCOL,
    WINDOWS_CPU_RATE_HARD_CAP,
    WINDOWS_JOB_MEMORY_LIMIT_BYTES,
    WINDOWS_PROCESS_MEMORY_LIMIT_BYTES,
    _run_bounded_probe,
    _sha256_file,
    _trusted_regular_file,
)
from .windows_path_identity import windows_invariant_path_key


_MAX_CONFIG_BYTES = 256 * 1024
_PREPARATION_FILE = ".sandbox-security-preparation.json"
_HELPER_FILE_NAME = "ecorex-sandbox-host.exe"
_STABLE_PROVISION_CONTRACT = "windows-appcontainer-stable-provision-v3"
_STRICT_INHERITANCE_PROOF = "immutable-read-tree-mutable-workspace-acl-mic-v3"
_RECEIPT_KEYS = {
    "appcontainer_sid",
    "cpu_rate_hard_cap",
    "helper_sha256",
    "inheritance_proof",
    "job_memory_limit_bytes",
    "operation",
    "permission_domain_sha256",
    "process_memory_limit_bytes",
    "read_roots_sha256",
    "root_security_sha256",
    "schema_version",
    "slot_digest",
    "status",
    "tree_security_sha256",
    "workspace_roots_sha256",
}
_MARKER_KEYS = {
    "appcontainer_sid",
    "attestation_receipt_sha256",
    "attestation_security_policy_sha256",
    "contract",
    "helper_sha256",
    "permission_domain_sha256",
    "provision_helper_sha256",
    "provision_receipt_sha256",
    "read_roots_sha256",
    "root_security_sha256",
    "schema_version",
    "slot_digest",
    "workspace_roots_sha256",
}


class WindowsSandboxSecurityError(RuntimeError):
    pass


class WindowsSandboxSlotSecurity:
    """Product-owned pre-extract/attest/cleanup hooks for InstallCoordinator."""

    def __init__(
        self,
        install_root: Path | str,
        bootstrap_helper: Path | str,
        *,
        expected_helper_sha256: str,
    ) -> None:
        if os.name != "nt":
            raise WindowsSandboxSecurityError("Windows sandbox security is Windows-only")
        self.install_root = _real_directory(Path(install_root))
        helper = _trusted_regular_file(Path(bootstrap_helper))
        expected = str(expected_helper_sha256).casefold()
        if len(expected) != 64 or any(value not in "0123456789abcdef" for value in expected):
            raise WindowsSandboxSecurityError("Bootstrap helper digest is invalid")
        if _sha256_file(helper) != expected:
            raise WindowsSandboxSecurityError("Bootstrap helper digest changed")
        bootstrap_root = _real_directory(self.install_root / "bootstrap")
        try:
            helper.relative_to(bootstrap_root)
        except ValueError:
            raise WindowsSandboxSecurityError(
                "Bootstrap helper is outside the trusted install directory"
            ) from None
        self.expected_helper_sha256 = expected
        self.bootstrap_helper = self._retain_helper(helper, expected)

    @classmethod
    def for_provision_digest(
        cls,
        install_root: Path | str,
        expected_helper_sha256: str,
        *,
        release_id: str | None = None,
    ) -> WindowsSandboxSlotSecurity:
        """Resolve an immutable helper by the digest recorded in one slot."""

        root = _real_directory(Path(install_root))
        expected = str(expected_helper_sha256).casefold()
        if len(expected) != 64 or any(
            value not in "0123456789abcdef" for value in expected
        ):
            raise WindowsSandboxSecurityError("Bootstrap helper digest is invalid")
        candidates = [
            root
            / "bootstrap"
            / "helpers"
            / expected
            / _HELPER_FILE_NAME,
        ]
        if release_id is not None:
            if (
                not isinstance(release_id, str)
                or not release_id
                or len(release_id) > 128
                or any(
                    value
                    not in (
                        "abcdefghijklmnopqrstuvwxyz"
                        "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
                        "0123456789._-"
                    )
                    for value in release_id
                )
            ):
                raise WindowsSandboxSecurityError(
                    "Bootstrap helper release identity is invalid"
                )
            candidates.append(
                root
                / "bootstrap"
                / "versions"
                / release_id
                / "bin"
                / _HELPER_FILE_NAME
            )
        candidates.append(root / "bootstrap" / "bin" / _HELPER_FILE_NAME)
        for candidate in candidates:
            try:
                helper = _trusted_regular_file(candidate)
                if _sha256_file(helper) == expected:
                    return cls(
                        root,
                        helper,
                        expected_helper_sha256=expected,
                    )
            except (OSError, ValueError, WindowsSandboxSecurityError):
                continue
        raise WindowsSandboxSecurityError(
            "Provisioning helper for the signed slot is unavailable"
        )

    def prepare(
        self,
        slot_root: Path,
        payload_root: Path,
        package_path: Path,
        manifest: ReleaseManifest,
        artifact: ReleaseArtifact,
    ) -> Mapping[str, Any]:
        slot = _real_directory(slot_root)
        payload = _real_directory(payload_root)
        workspaces = self._workspace_roots_from_archive(
            package_path, manifest=manifest, artifact=artifact
        )
        self._authorize_workspace_reuse(slot, workspaces)
        if any(payload.iterdir()):
            raise WindowsSandboxSecurityError(
                "Pre-extract Runtime payload root is not fresh and empty"
            )
        read_roots = (payload,)
        durable = self._preparation_record(
            state="provisioning",
            slot_digest=manifest.build_digest,
            workspaces=workspaces,
            receipt=None,
        )
        atomic_write_json(slot / _PREPARATION_FILE, durable)
        receipt = self._invoke(
            self.bootstrap_helper,
            "provision",
            slot=slot,
            read_roots=read_roots,
            workspaces=workspaces,
            slot_digest=manifest.build_digest,
            timeout_seconds=30,
        )
        self._validate_receipt(
            receipt,
            operation="provision",
            slot=slot,
            read_roots=read_roots,
            workspaces=workspaces,
            slot_digest=manifest.build_digest,
            inheritance_proof="fresh-empty-roots-v1",
            expected_helper_sha256=self.expected_helper_sha256,
        )
        durable = self._preparation_record(
            state="provisioned",
            slot_digest=manifest.build_digest,
            workspaces=workspaces,
            receipt=receipt,
        )
        atomic_write_json(slot / _PREPARATION_FILE, durable)
        return {
            "receipt": receipt,
            "workspace_relatives": [
                PurePosixPath(root.relative_to(self.install_root).as_posix()).as_posix()
                for root in workspaces
            ],
        }

    def attest(
        self,
        slot_root: Path,
        payload_root: Path,
        package_path: Path,
        manifest: ReleaseManifest,
        artifact: ReleaseArtifact,
        preparation: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        del package_path
        slot = _real_directory(slot_root)
        payload = _real_directory(payload_root)
        workspaces = self._workspace_roots_from_payload(
            payload, manifest=manifest, artifact=artifact
        )
        read_roots = (payload,)
        runtime_helper = _trusted_regular_file(payload / "bin" / "ecorex-sandbox-host.exe")
        runtime_helper_sha256 = _sha256_file(runtime_helper)
        if runtime_helper_sha256 != self.expected_helper_sha256:
            raise WindowsSandboxSecurityError(
                "Runtime and signed Bootstrap sandbox helpers differ"
            )
        provision_raw = preparation.get("receipt")
        if not isinstance(provision_raw, Mapping):
            raise WindowsSandboxSecurityError("Provision receipt is unavailable")
        provision = dict(provision_raw)
        self._validate_receipt(
            provision,
            operation="provision",
            slot=slot,
            read_roots=read_roots,
            workspaces=workspaces,
            slot_digest=manifest.build_digest,
            inheritance_proof="fresh-empty-roots-v1",
            expected_helper_sha256=self.expected_helper_sha256,
        )
        attestation = self._invoke(
            runtime_helper,
            "attest",
            slot=slot,
            read_roots=read_roots,
            workspaces=workspaces,
            slot_digest=manifest.build_digest,
            timeout_seconds=120,
            extra=("--mode", "strict"),
        )
        self._validate_receipt(
            attestation,
            operation="attest",
            slot=slot,
            read_roots=read_roots,
            workspaces=workspaces,
            slot_digest=manifest.build_digest,
            inheritance_proof=_STRICT_INHERITANCE_PROOF,
            expected_helper_sha256=runtime_helper_sha256,
        )
        identity_fields = (
            "appcontainer_sid",
            "permission_domain_sha256",
            "read_roots_sha256",
            "root_security_sha256",
            "slot_digest",
            "workspace_roots_sha256",
        )
        if any(provision.get(key) != attestation.get(key) for key in identity_fields):
            raise WindowsSandboxSecurityError(
                "Post-extract sandbox identity differs from its provision receipt"
            )
        return {
            "schema_version": 1,
            "contract": _STABLE_PROVISION_CONTRACT,
            "appcontainer_sid": attestation["appcontainer_sid"],
            "helper_sha256": attestation["helper_sha256"],
            "permission_domain_sha256": attestation["permission_domain_sha256"],
            "provision_helper_sha256": provision["helper_sha256"],
            "read_roots_sha256": attestation["read_roots_sha256"],
            "root_security_sha256": attestation["root_security_sha256"],
            "slot_digest": attestation["slot_digest"],
            "workspace_roots_sha256": attestation["workspace_roots_sha256"],
            "provision_receipt_sha256": _json_sha256(provision),
            "attestation_receipt_sha256": _json_sha256(attestation),
            "attestation_security_policy_sha256": attestation[
                "tree_security_sha256"
            ],
        }

    def validate(
        self,
        slot_root: Path,
        manifest: ReleaseManifest,
        artifact: ReleaseArtifact,
        marker: Mapping[str, Any],
    ) -> bool:
        try:
            if set(marker) != _MARKER_KEYS or marker.get("schema_version") != 1:
                return False
            self._helper_for_digest(
                str(marker.get("provision_helper_sha256", ""))
            )
            slot = _real_directory(slot_root)
            payload = _real_directory(slot / "payload")
            workspaces = self._workspace_roots_from_payload(
                payload, manifest=manifest, artifact=artifact
            )
            read_roots = (payload,)
            runtime_helper = _trusted_regular_file(
                payload / "bin" / "ecorex-sandbox-host.exe"
            )
            runtime_helper_sha256 = _sha256_file(runtime_helper)
            receipt = self._invoke(
                runtime_helper,
                "attest",
                slot=slot,
                read_roots=read_roots,
                workspaces=workspaces,
                slot_digest=manifest.build_digest,
                timeout_seconds=30,
                extra=("--mode", "strict"),
            )
            self._validate_receipt(
                receipt,
                operation="attest",
                slot=slot,
                read_roots=read_roots,
                workspaces=workspaces,
                slot_digest=manifest.build_digest,
                inheritance_proof=_STRICT_INHERITANCE_PROOF,
                expected_helper_sha256=runtime_helper_sha256,
            )
            for key in (
                "appcontainer_sid",
                "helper_sha256",
                "permission_domain_sha256",
                "read_roots_sha256",
                "root_security_sha256",
                "slot_digest",
                "workspace_roots_sha256",
            ):
                if marker.get(key) != receipt.get(key):
                    return False
            if (
                marker.get("attestation_security_policy_sha256")
                != receipt.get("tree_security_sha256")
                or marker.get("attestation_receipt_sha256")
                != _json_sha256(receipt)
            ):
                return False
            return marker.get("contract") == _STABLE_PROVISION_CONTRACT
        except Exception:
            return False

    def cleanup_failed(
        self,
        slot_root: Path,
        payload_root: Path,
        manifest: ReleaseManifest,
        artifact: ReleaseArtifact,
        preparation: Mapping[str, Any],
    ) -> None:
        del payload_root, manifest, artifact, preparation
        self.cleanup_abandoned(slot_root)

    def cleanup_abandoned(self, slot_root: Path) -> None:
        """Converge a crash-left pre-extract permission intent before deletion."""

        slot = _real_directory(slot_root)
        try:
            slot.relative_to(_real_directory(self.install_root / "slots"))
        except ValueError:
            raise WindowsSandboxSecurityError(
                "Abandoned sandbox slot is outside the install root"
            ) from None
        preparation_path = slot / _PREPARATION_FILE
        payload_path = slot / "payload"
        if not os.path.lexists(payload_path):
            if os.path.lexists(preparation_path) or any(slot.iterdir()):
                raise WindowsSandboxSecurityError(
                    "Abandoned sandbox slot has data but no payload root"
                )
            return
        payload = _real_directory(payload_path)
        if not os.path.lexists(preparation_path):
            # Stage creates the empty payload directory before calling prepare.
            # With no durable intent, native provisioning has not started yet.
            if any(payload.iterdir()) or any(
                child != payload for child in slot.iterdir()
            ):
                raise WindowsSandboxSecurityError(
                    "Abandoned sandbox slot has data but no preparation intent"
                )
            return
        value = self._load_preparation_record(slot)
        workspaces = tuple(
            _real_directory(
                self.install_root.joinpath(*PurePosixPath(relative).parts)
            )
            for relative in value["workspace_relatives"]
        )
        if (
            value["permission_domain_sha256"]
            != _permission_domain_digest(workspaces)
        ):
            raise WindowsSandboxSecurityError(
                "Abandoned sandbox permission domain changed"
            )
        read_roots = (payload,)
        receipt = value["receipt"]
        provision_helper_sha256 = str(value["provision_helper_sha256"])
        provision_helper = self._helper_for_digest(provision_helper_sha256)
        if receipt is not None:
            if not isinstance(receipt, Mapping):
                raise WindowsSandboxSecurityError(
                    "Abandoned sandbox provision receipt is invalid"
                )
            self._validate_receipt(
                receipt,
                operation="provision",
                slot=slot,
                read_roots=read_roots,
                workspaces=workspaces,
                slot_digest=value["slot_digest"],
                inheritance_proof="fresh-empty-roots-v1",
                expected_helper_sha256=provision_helper_sha256,
            )
        self._invoke(
            provision_helper,
            "unprovision-slot",
            slot=slot,
            read_roots=read_roots,
            workspaces=workspaces,
            slot_digest=value["slot_digest"],
            timeout_seconds=120,
        )
        if not self._permission_domain_referenced(
            value["permission_domain_sha256"], excluding=slot
        ):
            self._invoke(
                provision_helper,
                "unprovision-domain",
                slot=slot,
                read_roots=read_roots,
                workspaces=workspaces,
                slot_digest=value["slot_digest"],
                timeout_seconds=120,
            )
        try:
            (slot / _PREPARATION_FILE).unlink()
        except FileNotFoundError:
            pass

    def cleanup_slot(
        self,
        slot_root: Path,
        manifest: ReleaseManifest,
        artifact: ReleaseArtifact,
        marker: Mapping[str, Any],
    ) -> None:
        """Remove one retained slot grant without tearing down shared workspace ACLs."""

        slot = _real_directory(slot_root)
        payload = _real_directory(slot / "payload")
        workspaces = self._workspace_roots_from_payload(
            payload,
            manifest=manifest,
            artifact=artifact,
        )
        read_roots = (payload,)
        if not self.validate(slot, manifest, artifact, marker):
            raise WindowsSandboxSecurityError(
                "Retained slot sandbox receipt is not valid for cleanup"
            )
        provision_helper = self._helper_for_digest(
            str(marker.get("provision_helper_sha256", ""))
        )
        self._invoke(
            provision_helper,
            "unprovision-slot",
            slot=slot,
            read_roots=read_roots,
            workspaces=workspaces,
            slot_digest=manifest.build_digest,
            timeout_seconds=120,
        )
        permission_domain = str(marker.get("permission_domain_sha256", ""))
        if not self._permission_domain_referenced(permission_domain, excluding=slot):
            self._invoke(
                provision_helper,
                "unprovision-domain",
                slot=slot,
                read_roots=read_roots,
                workspaces=workspaces,
                slot_digest=manifest.build_digest,
                timeout_seconds=120,
            )

    def _retain_helper(self, helper: Path, expected_digest: str) -> Path:
        helpers = self.install_root / "bootstrap" / "helpers"
        helpers.mkdir(parents=True, exist_ok=True)
        helpers = _real_directory(helpers)
        final = helpers / expected_digest
        destination = final / _HELPER_FILE_NAME
        if os.path.lexists(final):
            _real_directory(final)
            retained = _trusted_regular_file(destination)
            if _sha256_file(retained) != expected_digest:
                raise WindowsSandboxSecurityError(
                    "Immutable sandbox helper store conflicts with its digest"
                )
            _fsync_directory(final)
            _fsync_directory(helpers)
            return retained
        staging = Path(
            tempfile.mkdtemp(prefix=f".{expected_digest}.", dir=helpers)
        )
        try:
            copied = staging / _HELPER_FILE_NAME
            _copy_verified_helper(helper, copied, expected_digest)
            _fsync_directory(staging)
            try:
                _durable_replace(staging, final, replace_existing=False)
            except OSError:
                if not os.path.lexists(final):
                    raise
                _real_directory(final)
            retained = _trusted_regular_file(destination)
            if _sha256_file(retained) != expected_digest:
                raise WindowsSandboxSecurityError(
                    "Retained sandbox helper digest changed"
                )
            _fsync_directory(final)
            _fsync_directory(helpers)
            return retained
        except (OSError, ValueError) as error:
            raise WindowsSandboxSecurityError(
                "Sandbox helper could not enter the immutable store"
            ) from error
        finally:
            if staging.exists():
                shutil.rmtree(staging)

    def _helper_for_digest(self, expected_digest: str) -> Path:
        if len(expected_digest) != 64 or any(
            character not in "0123456789abcdef"
            for character in expected_digest
        ):
            raise WindowsSandboxSecurityError(
                "Sandbox provision helper identity is invalid"
            )
        candidates = (
            self.install_root
            / "bootstrap"
            / "helpers"
            / expected_digest
            / _HELPER_FILE_NAME,
            self.bootstrap_helper,
            self.install_root / "bootstrap" / "bin" / _HELPER_FILE_NAME,
        )
        for candidate in candidates:
            try:
                helper = _trusted_regular_file(candidate)
                if _sha256_file(helper) == expected_digest:
                    return helper
            except (OSError, ValueError):
                continue
        raise WindowsSandboxSecurityError(
            "Sandbox provision helper is no longer available"
        )

    def _preparation_record(
        self,
        *,
        state: str,
        slot_digest: str,
        workspaces: tuple[Path, ...],
        receipt: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        if state not in {"provisioning", "provisioned"}:
            raise WindowsSandboxSecurityError(
                "Sandbox preparation state is invalid"
            )
        value: dict[str, Any] = {
            "schema_version": 2,
            "contract": "windows-appcontainer-preparation-v2",
            "state": state,
            "slot_digest": slot_digest,
            "provision_helper_sha256": self.expected_helper_sha256,
            "workspace_relatives": [
                PurePosixPath(root.relative_to(self.install_root).as_posix()).as_posix()
                for root in workspaces
            ],
            "permission_domain_sha256": _permission_domain_digest(workspaces),
            "receipt": dict(receipt) if receipt is not None else None,
        }
        value["record_digest"] = _json_sha256(value)
        return value

    def _load_preparation_record(self, slot: Path) -> dict[str, Any]:
        path = _trusted_regular_file(slot / _PREPARATION_FILE)
        if not 1 <= path.stat().st_size <= 64 * 1024:
            raise WindowsSandboxSecurityError(
                "Sandbox preparation record size is invalid"
            )
        try:
            value = json.loads(
                path.read_text(encoding="utf-8"),
                object_pairs_hook=_unique_object,
            )
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
            raise WindowsSandboxSecurityError(
                "Sandbox preparation record is unreadable"
            ) from None
        common_fields = {
            "schema_version",
            "contract",
            "state",
            "slot_digest",
            "workspace_relatives",
            "permission_domain_sha256",
            "receipt",
            "record_digest",
        }
        if not isinstance(value, dict):
            raise WindowsSandboxSecurityError(
                "Sandbox preparation record fields are invalid"
            )
        schema_version = value.get("schema_version")
        expected = (
            common_fields
            if schema_version == 1
            else common_fields | {"provision_helper_sha256"}
        )
        if set(value) != expected:
            raise WindowsSandboxSecurityError(
                "Sandbox preparation record fields are invalid"
            )
        unsigned = dict(value)
        digest = unsigned.pop("record_digest")
        state = value.get("state")
        relatives = value.get("workspace_relatives")
        if (
            schema_version not in {1, 2}
            or value.get("contract")
            != (
                "windows-appcontainer-preparation-v1"
                if schema_version == 1
                else "windows-appcontainer-preparation-v2"
            )
            or state not in {"provisioning", "provisioned"}
            or not isinstance(value.get("slot_digest"), str)
            or len(value["slot_digest"]) != 64
            or any(character not in "0123456789abcdef" for character in value["slot_digest"])
            or not isinstance(value.get("permission_domain_sha256"), str)
            or len(value["permission_domain_sha256"]) != 64
            or not isinstance(relatives, list)
            or not relatives
            or len(relatives) > 32
            or not isinstance(digest, str)
            or digest != _json_sha256(unsigned)
            or (state == "provisioning" and value.get("receipt") is not None)
            or (state == "provisioned" and not isinstance(value.get("receipt"), Mapping))
        ):
            raise WindowsSandboxSecurityError(
                "Sandbox preparation record identity is invalid"
            )
        for relative in relatives:
            if not isinstance(relative, str) or "\\" in relative or ":" in relative:
                raise WindowsSandboxSecurityError(
                    "Sandbox preparation workspace is invalid"
                )
            path_value = PurePosixPath(relative)
            if path_value.is_absolute() or any(
                part in {"", ".", ".."} for part in path_value.parts
            ):
                raise WindowsSandboxSecurityError(
                    "Sandbox preparation workspace is invalid"
                )
            try:
                path_value.relative_to(PurePosixPath("workspace"))
            except ValueError:
                raise WindowsSandboxSecurityError(
                    "Sandbox preparation workspace escaped its managed root"
                ) from None
        provision_helper_sha256 = (
            self.expected_helper_sha256
            if schema_version == 1
            else value.get("provision_helper_sha256")
        )
        if (
            not isinstance(provision_helper_sha256, str)
            or len(provision_helper_sha256) != 64
            or any(
                character not in "0123456789abcdef"
                for character in provision_helper_sha256
            )
        ):
            raise WindowsSandboxSecurityError(
                "Sandbox preparation helper identity is invalid"
            )
        value["provision_helper_sha256"] = provision_helper_sha256
        return value

    def _invoke(
        self,
        helper: Path,
        operation: str,
        *,
        slot: Path,
        read_roots: tuple[Path, ...],
        workspaces: tuple[Path, ...],
        slot_digest: str,
        timeout_seconds: float,
        extra: tuple[str, ...] = (),
    ) -> dict[str, Any]:
        command = [
            str(helper),
            operation,
            "--protocol",
            SANDBOX_LAUNCH_PROTOCOL,
            "--install-root",
            str(self.install_root),
            "--slot-root",
            str(slot),
            "--slot-digest",
            slot_digest,
            "--workspace-digest",
            _ordered_roots_digest(workspaces),
        ]
        for root in read_roots:
            command.extend(("--read-root", str(root)))
        for root in workspaces:
            command.extend(("--workspace", str(root)))
        command.extend(extra)
        completed = _run_bounded_probe(
            tuple(command),
            timeout_seconds=timeout_seconds,
            stdout_limit=16 * 1024,
            stderr_limit=4 * 1024,
        )
        if completed is None or completed.returncode != 0 or completed.stderr:
            raise WindowsSandboxSecurityError(
                f"Windows sandbox helper {operation} failed"
            )
        try:
            value = json.loads(completed.stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
            raise WindowsSandboxSecurityError("Sandbox helper receipt is invalid") from None
        if not isinstance(value, dict):
            raise WindowsSandboxSecurityError("Sandbox helper receipt is invalid")
        canonical = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        if canonical != completed.stdout:
            raise WindowsSandboxSecurityError("Sandbox helper receipt is not canonical")
        return value

    def _validate_receipt(
        self,
        receipt: Mapping[str, Any],
        *,
        operation: str,
        slot: Path,
        read_roots: tuple[Path, ...],
        workspaces: tuple[Path, ...],
        slot_digest: str,
        inheritance_proof: str,
        expected_helper_sha256: str,
    ) -> None:
        if (
            set(receipt) != _RECEIPT_KEYS
            or receipt.get("schema_version") != 1
            or receipt.get("status") != "passed"
            or receipt.get("operation") != operation
            or receipt.get("inheritance_proof") != inheritance_proof
            or receipt.get("helper_sha256") != expected_helper_sha256
            or receipt.get("slot_digest") != slot_digest
            or receipt.get("workspace_roots_sha256")
            != _ordered_roots_digest(workspaces)
            or receipt.get("permission_domain_sha256")
            != _permission_domain_digest(workspaces)
            or receipt.get("read_roots_sha256")
            != _relative_roots_digest(read_roots, slot)
            or receipt.get("cpu_rate_hard_cap") != WINDOWS_CPU_RATE_HARD_CAP
            or receipt.get("process_memory_limit_bytes")
            != WINDOWS_PROCESS_MEMORY_LIMIT_BYTES
            or receipt.get("job_memory_limit_bytes") != WINDOWS_JOB_MEMORY_LIMIT_BYTES
        ):
            raise WindowsSandboxSecurityError("Sandbox helper receipt identity is invalid")
        for key in (
            "root_security_sha256",
            "tree_security_sha256",
            "permission_domain_sha256",
            "read_roots_sha256",
            "workspace_roots_sha256",
        ):
            value = receipt.get(key)
            if not isinstance(value, str) or len(value) != 64 or any(
                character not in "0123456789abcdef" for character in value
            ):
                raise WindowsSandboxSecurityError(
                    "Sandbox helper receipt digest is invalid"
                )
        sid = receipt.get("appcontainer_sid")
        if not isinstance(sid, str) or not sid.startswith("S-1-15-2-"):
            raise WindowsSandboxSecurityError("Sandbox helper SID is invalid")

    def _workspace_roots_from_archive(
        self,
        package_path: Path,
        *,
        manifest: ReleaseManifest,
        artifact: ReleaseArtifact,
    ) -> tuple[Path, ...]:
        try:
            with zipfile.ZipFile(package_path) as archive:
                members = [
                    item for item in archive.infolist() if item.filename == "runtime-config.json"
                ]
                if len(members) != 1:
                    raise WindowsSandboxSecurityError(
                        "Signed Core has no unique Runtime configuration"
                    )
                member = members[0]
                if (
                    member.is_dir()
                    or member.flag_bits & 0x1
                    or not 1 <= member.file_size <= _MAX_CONFIG_BYTES
                ):
                    raise WindowsSandboxSecurityError(
                        "Signed Runtime configuration member is unsafe"
                    )
                with archive.open(member) as stream:
                    payload = stream.read(_MAX_CONFIG_BYTES + 1)
        except (OSError, zipfile.BadZipFile, RuntimeError):
            raise WindowsSandboxSecurityError("Signed Core cannot be inspected") from None
        return self._workspace_roots_from_bytes(
            payload, manifest=manifest, artifact=artifact
        )

    def _workspace_roots_from_payload(
        self,
        payload: Path,
        *,
        manifest: ReleaseManifest,
        artifact: ReleaseArtifact,
    ) -> tuple[Path, ...]:
        path = _trusted_regular_file(payload / "runtime-config.json")
        raw = path.read_bytes()
        if not 1 <= len(raw) <= _MAX_CONFIG_BYTES:
            raise WindowsSandboxSecurityError("Runtime configuration size is invalid")
        return self._workspace_roots_from_bytes(raw, manifest=manifest, artifact=artifact)

    def _workspace_roots_from_bytes(
        self,
        payload: bytes,
        *,
        manifest: ReleaseManifest,
        artifact: ReleaseArtifact,
    ) -> tuple[Path, ...]:
        try:
            raw = json.loads(payload.decode("utf-8"), object_pairs_hook=_unique_object)
            identity = raw["identity"]
            values = raw["paths"]["workspace_roots"]
        except (KeyError, TypeError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
            raise WindowsSandboxSecurityError("Runtime workspace contract is invalid") from None
        if (
            not isinstance(identity, Mapping)
            or identity.get("version") != manifest.version
            or identity.get("platform") != artifact.platform
            or identity.get("architecture") != artifact.architecture
            or not isinstance(values, list)
            or not values
            or len(values) > 32
        ):
            raise WindowsSandboxSecurityError("Runtime workspace identity is invalid")
        relatives: list[PurePosixPath] = []
        for value in values:
            if not isinstance(value, str) or not value or "\\" in value or ":" in value:
                raise WindowsSandboxSecurityError("Runtime workspace path is invalid")
            relative = PurePosixPath(value)
            if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
                raise WindowsSandboxSecurityError("Runtime workspace path is invalid")
            try:
                relative.relative_to(PurePosixPath("workspace"))
            except ValueError:
                raise WindowsSandboxSecurityError(
                    "Runtime workspace escaped the managed workspace directory"
                ) from None
            relatives.append(relative)
        if len(set(relatives)) != len(relatives):
            raise WindowsSandboxSecurityError("Runtime workspace roots are duplicated")
        for position, left in enumerate(relatives):
            for right in relatives[position + 1 :]:
                if _posix_contains(left, right) or _posix_contains(right, left):
                    raise WindowsSandboxSecurityError(
                        "Runtime workspace roots overlap"
                    )
        managed = self.install_root / "workspace"
        managed.mkdir(parents=True, exist_ok=True)
        _real_directory(managed)
        roots: list[Path] = []
        for relative in relatives:
            root = self.install_root.joinpath(*relative.parts)
            root.mkdir(parents=True, exist_ok=True)
            roots.append(_real_directory(root))
        return tuple(roots)

    def _workspace_roots_from_payload_or_preparation(
        self,
        payload: Path,
        preparation: Mapping[str, Any],
    ) -> tuple[Path, ...]:
        del payload
        values = preparation.get("workspace_relatives")
        receipt = preparation.get("receipt")
        if (
            not isinstance(values, list)
            or not values
            or not isinstance(receipt, Mapping)
            or any(not isinstance(value, str) for value in values)
        ):
            raise WindowsSandboxSecurityError(
                "Failed slot workspace identity cannot be reconstructed"
            )
        candidates = tuple(
            _real_directory(self.install_root.joinpath(*PurePosixPath(value).parts))
            for value in values
        )
        if _ordered_roots_digest(candidates) != receipt.get("workspace_roots_sha256"):
            raise WindowsSandboxSecurityError(
                "Failed slot workspace identity cannot be reconstructed"
            )
        return candidates

    def _authorize_workspace_reuse(
        self,
        candidate_slot: Path,
        workspaces: tuple[Path, ...],
    ) -> None:
        """Require a full trusted-tree proof before reusing non-empty workspaces."""

        target_domain = _permission_domain_digest(workspaces)
        target_paths = {windows_invariant_path_key(root) for root in workspaces}
        matched_domain = False
        for slot_id in self._referenced_slot_ids(excluding=candidate_slot):
            slot = self.install_root / "slots" / slot_id
            marker = self._read_slot_marker(slot)
            security = marker.get("security_provision")
            if not isinstance(security, Mapping):
                raise WindowsSandboxSecurityError(
                    "Referenced slot has no sandbox security receipt"
                )
            prior_workspaces, manifest, artifact = self._slot_workspace_identity(
                slot,
                marker,
            )
            prior_paths = {
                windows_invariant_path_key(root) for root in prior_workspaces
            }
            if not target_paths.intersection(prior_paths):
                continue
            if security.get("permission_domain_sha256") != target_domain:
                raise WindowsSandboxSecurityError(
                    "Signed update changed an overlapping workspace permission domain"
                )
            if prior_paths != target_paths:
                raise WindowsSandboxSecurityError(
                    "Signed update changed the roots of a live workspace permission domain"
                )
            self._attest_referenced_slot(
                slot,
                manifest=manifest,
                artifact=artifact,
                workspaces=prior_workspaces,
                marker=security,
            )
            matched_domain = True
        if matched_domain:
            return
        try:
            non_empty = any(any(root.iterdir()) for root in workspaces)
        except OSError:
            raise WindowsSandboxSecurityError(
                "Workspace freshness cannot be inspected"
            ) from None
        if non_empty:
            raise WindowsSandboxSecurityError(
                "A non-empty workspace has no trusted permission-domain reference"
            )

    def _attest_referenced_slot(
        self,
        slot: Path,
        *,
        manifest: ReleaseManifest,
        artifact: ReleaseArtifact,
        workspaces: tuple[Path, ...],
        marker: Mapping[str, Any],
    ) -> None:
        if set(marker) != _MARKER_KEYS or marker.get("schema_version") != 1:
            raise WindowsSandboxSecurityError(
                "Referenced slot sandbox marker is invalid"
            )
        payload = _real_directory(slot / "payload")
        read_roots = (payload,)
        helper = _trusted_regular_file(payload / "bin" / "ecorex-sandbox-host.exe")
        helper_sha256 = _sha256_file(helper)
        receipt = self._invoke(
            helper,
            "attest",
            slot=slot,
            read_roots=read_roots,
            workspaces=workspaces,
            slot_digest=manifest.build_digest,
            timeout_seconds=120,
            extra=("--mode", "strict"),
        )
        self._validate_receipt(
            receipt,
            operation="attest",
            slot=slot,
            read_roots=read_roots,
            workspaces=workspaces,
            slot_digest=manifest.build_digest,
            inheritance_proof=_STRICT_INHERITANCE_PROOF,
            expected_helper_sha256=helper_sha256,
        )
        for key in (
            "appcontainer_sid",
            "helper_sha256",
            "permission_domain_sha256",
            "read_roots_sha256",
            "root_security_sha256",
            "slot_digest",
            "workspace_roots_sha256",
        ):
            if marker.get(key) != receipt.get(key):
                raise WindowsSandboxSecurityError(
                    "Referenced slot sandbox identity changed"
                )
        if marker.get("attestation_security_policy_sha256") != receipt.get(
            "tree_security_sha256"
        ):
            raise WindowsSandboxSecurityError(
                "Referenced slot sandbox tree security changed"
            )

    def _slot_workspace_identity(
        self,
        slot: Path,
        marker: Mapping[str, Any],
    ) -> tuple[tuple[Path, ...], ReleaseManifest, ReleaseArtifact]:
        try:
            manifest = ReleaseManifest.from_json(
                _trusted_regular_file(slot / "release-manifest.json").read_bytes()
            )
            artifact_id = marker.get("artifact_id")
            if not isinstance(artifact_id, str):
                raise ValueError
            artifact = manifest.artifact(artifact_id)
            payload = _real_directory(slot / "payload")
            workspaces = self._workspace_roots_from_payload(
                payload,
                manifest=manifest,
                artifact=artifact,
            )
        except Exception:
            raise WindowsSandboxSecurityError(
                "Referenced slot workspace identity is invalid"
            ) from None
        return workspaces, manifest, artifact

    def _read_slot_marker(self, slot: Path) -> dict[str, Any]:
        path = _trusted_regular_file(slot / ".slot.json")
        try:
            if path.stat().st_size > 256 * 1024:
                raise ValueError
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
            raise WindowsSandboxSecurityError(
                "Referenced slot marker is invalid"
            ) from None
        if not isinstance(value, dict):
            raise WindowsSandboxSecurityError("Referenced slot marker is invalid")
        return value

    def _referenced_slot_ids(self, *, excluding: Path) -> tuple[str, ...]:
        try:
            pointers = SlotStore(self.install_root).pointers()
        except Exception:
            raise WindowsSandboxSecurityError("Slot pointers are invalid") from None
        identifiers = {
            item
            for item in (
                pointers.current,
                pointers.previous,
                *pointers.known_good,
            )
            if item is not None
        }
        active_path = self.install_root / "active-transaction.json"
        if active_path.exists():
            try:
                active = json.loads(active_path.read_text(encoding="utf-8"))
                active_slot = active.get("slot_id") if isinstance(active, Mapping) else None
                if isinstance(active_slot, str):
                    identifiers.add(active_slot)
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                raise WindowsSandboxSecurityError(
                    "Active slot authority is unreadable"
                ) from None
        excluded = excluding.resolve(strict=False)
        result: list[str] = []
        for slot_id in sorted(identifiers):
            if not isinstance(slot_id, str) or not slot_id or any(
                character in slot_id for character in "/\\\0\r\n"
            ):
                raise WindowsSandboxSecurityError("Referenced slot identity is invalid")
            candidate = self.install_root / "slots" / slot_id
            if candidate.resolve(strict=False) == excluded:
                continue
            if candidate.is_dir():
                _real_directory(candidate)
                result.append(slot_id)
        return tuple(result)

    def _permission_domain_referenced(
        self, permission_domain: str, *, excluding: Path
    ) -> bool:
        for slot_id in self._referenced_slot_ids(excluding=excluding):
            marker = self._read_slot_marker(
                self.install_root / "slots" / slot_id
            )
            security = marker.get("security_provision")
            if not isinstance(security, Mapping):
                raise WindowsSandboxSecurityError(
                    "Referenced slot security receipt is unavailable"
                )
            if security.get("permission_domain_sha256") == permission_domain:
                return True
        return False


def _copy_verified_helper(source: Path, destination: Path, expected_digest: str) -> None:
    source = _trusted_regular_file(source)
    try:
        before = source.lstat()
        digest = hashlib.sha256()
        with source.open("rb") as input_stream, destination.open("xb") as output_stream:
            opened = os.fstat(input_stream.fileno())
            while chunk := input_stream.read(1024 * 1024):
                digest.update(chunk)
                output_stream.write(chunk)
            output_stream.flush()
            os.fsync(output_stream.fileno())
            after_open = os.fstat(input_stream.fileno())
        after = source.lstat()
    except OSError as error:
        raise WindowsSandboxSecurityError(
            "Sandbox helper immutable copy failed"
        ) from error
    identity = (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
    )
    if (
        digest.hexdigest() != expected_digest
        or (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)
        != identity
        or (
            after_open.st_dev,
            after_open.st_ino,
            after_open.st_size,
            after_open.st_mtime_ns,
        )
        != identity
        or (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        != identity
    ):
        raise WindowsSandboxSecurityError(
            "Sandbox helper changed while entering the immutable store"
        )
    destination.chmod(0o700)
    retained = _trusted_regular_file(destination)
    if _sha256_file(retained) != expected_digest:
        raise WindowsSandboxSecurityError(
            "Sandbox helper immutable copy did not verify"
        )


def _real_directory(path: Path) -> Path:
    absolute = Path(os.path.abspath(path))
    current = absolute
    while True:
        metadata = current.lstat()
        reparse = getattr(metadata, "st_file_attributes", 0) & getattr(
            stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400
        )
        if stat.S_ISLNK(metadata.st_mode) or reparse or not stat.S_ISDIR(metadata.st_mode):
            raise WindowsSandboxSecurityError("Sandbox security path is not a real directory")
        if current.parent == current:
            break
        current = current.parent
    return absolute.resolve(strict=True)


def _ordered_roots_digest(roots: tuple[Path, ...]) -> str:
    return hashlib.sha256("\0".join(str(root) for root in roots).encode()).hexdigest()


def _permission_domain_digest(roots: tuple[Path, ...]) -> str:
    values = sorted(windows_invariant_path_key(root) for root in roots)
    return hashlib.sha256("\0".join(values).encode()).hexdigest()


def _relative_roots_digest(roots: tuple[Path, ...], slot: Path) -> str:
    values = sorted(
        windows_invariant_path_key(root.relative_to(slot)) for root in roots
    )
    return hashlib.sha256("\0".join(values).encode()).hexdigest()


def _json_sha256(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _posix_contains(candidate: PurePosixPath, root: PurePosixPath) -> bool:
    try:
        candidate.relative_to(root)
        return True
    except ValueError:
        return False


__all__ = [
    "WindowsSandboxSecurityError",
    "WindowsSandboxSlotSecurity",
]
