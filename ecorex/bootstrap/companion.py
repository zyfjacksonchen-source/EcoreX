"""Single authority for signed Bootstrap companions and desktop entry ownership."""

from __future__ import annotations

import base64
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from typing import Any, Mapping
from urllib.parse import urlsplit
import zipfile

from ecorex.update.fetching import ArtifactFetcher
from ecorex.update.manifest import (
    ReleaseArtifact,
    ReleaseManifest,
    SignatureEnvelope,
)
from ecorex.update.storage import atomic_write_json
from ecorex.update.verification import (
    Ed25519SignatureVerifier,
    SignatureVerifier,
    sha256_file,
    verify_artifact_file,
    verify_artifact_signature,
    verify_manifest_signature,
)


MAX_BOOTSTRAP_BYTES = 10 * 1024 * 1024
MAX_BOOTSTRAP_EXPANDED_BYTES = 32 * 1024 * 1024
DESKTOP_ENTRY_SCHEMA_VERSION = 1
DESKTOP_ACTIVATION_SCHEMA_VERSION = 1
_SAFE_RELEASE_ID = re.compile(r"^release-stable-[0-9a-f]{24}$")
_SAFE_TRANSACTION_ID = re.compile(r"^[0-9a-f]{32}$")
_SAFE_KEY_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_STABLE_V1 = re.compile(r"^1\.(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})$")
_WINDOWS_ENTRY_NAMES = tuple(
    ["EcoreX.lnk", "EcoreX Agent.lnk"]
    + [f"EcoreX Agent ({index}).lnk" for index in range(2, 10)]
)
_MAC_ENTRY_NAMES = tuple(
    ["EcoreX.app", "EcoreX Agent.app"]
    + [f"EcoreX Agent ({index}).app" for index in range(2, 10)]
)


class BootstrapCompanionError(RuntimeError):
    pass


class BootstrapCompanionInstaller:
    """Stages, verifies and activates one host Bootstrap beside the Runtime."""

    def __init__(
        self,
        install_root: str | os.PathLike[str],
        *,
        platform: str,
        architecture: str,
        verifier: SignatureVerifier,
        fetcher: ArtifactFetcher | None = None,
        desktop_directory: str | os.PathLike[str] | None = None,
        windows_security_factory: Any | None = None,
    ) -> None:
        self.root = Path(os.path.abspath(install_root))
        if platform not in {"windows", "macos"}:
            raise ValueError("Bootstrap companion platform is unsupported")
        if architecture not in {"x64", "arm64"} or (
            platform == "windows" and architecture != "x64"
        ):
            raise ValueError("Bootstrap companion architecture is unsupported")
        self.platform = platform
        self.architecture = architecture
        self.verifier = verifier
        self.fetcher = fetcher
        self._windows_security_factory = windows_security_factory
        self._desktop_directory = (
            Path(os.path.abspath(desktop_directory))
            if desktop_directory is not None
            else None
        )

    @property
    def artifact_id(self) -> str:
        return f"bootstrap-{self.platform}-{self.architecture}"

    def stage(self, manifest: ReleaseManifest, transaction_dir: Path) -> Path:
        if self.fetcher is None:
            raise BootstrapCompanionError("Bootstrap companion fetcher is unavailable")
        verify_manifest_signature(manifest, self.verifier)
        artifact = self._artifact(manifest)
        destination = (
            Path(transaction_dir)
            / "bootstrap-companion"
            / artifact.file_name
        )
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            verify_artifact_file(destination, manifest, artifact, self.verifier)
            return destination
        last_error: BaseException | None = None
        for source in manifest.sources:
            try:
                if os.path.lexists(destination):
                    destination.unlink()
                self.fetcher.fetch(
                    source,
                    artifact,
                    destination,
                    resume_from=0,
                    max_bytes=artifact.size_bytes,
                )
                verify_artifact_file(destination, manifest, artifact, self.verifier)
                return destination
            except Exception as error:
                last_error = error
                try:
                    destination.unlink(missing_ok=True)
                except OSError:
                    pass
        raise BootstrapCompanionError(
            "all signed Bootstrap companion sources failed"
        ) from last_error

    def stage_from_directory(
        self,
        manifest: ReleaseManifest,
        artifacts_directory: Path,
        transaction_dir: Path,
    ) -> Path:
        artifact = self._artifact(manifest)
        source = Path(artifacts_directory) / artifact.file_name
        verify_artifact_file(source, manifest, artifact, self.verifier)
        destination = (
            Path(transaction_dir)
            / "bootstrap-companion"
            / artifact.file_name
        )
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            verify_artifact_file(destination, manifest, artifact, self.verifier)
            return destination
        temporary = destination.with_name(destination.name + ".tmp")
        temporary.unlink(missing_ok=True)
        shutil.copyfile(source, temporary)
        verify_artifact_file(temporary, manifest, artifact, self.verifier)
        os.replace(temporary, destination)
        return destination

    def prepare_payload_security(
        self,
        manifest: ReleaseManifest,
        transaction_dir: Path,
    ) -> Any | None:
        """Return security hooks bound to the signed target Bootstrap helper.

        The immutable version directory is populated before Core extraction.
        This makes the helper that provisions the slot the same signed helper
        recorded in that slot, independent of the mutable compatibility copy
        under ``bootstrap/bin``.
        """

        if self.platform != "windows":
            return None
        verify_manifest_signature(manifest, self.verifier)
        artifact = self._artifact(manifest)
        archive = (
            Path(transaction_dir)
            / "bootstrap-companion"
            / artifact.file_name
        )
        verify_artifact_file(archive, manifest, artifact, self.verifier)
        launcher = self._install_versioned(manifest, artifact, archive)
        helper = launcher.with_name("ecorex-sandbox-host.exe")
        helper_digest = sha256_file(helper)
        security_factory = self._windows_security_factory
        if security_factory is None:
            from ecorex.integration.windows_sandbox_security import (
                WindowsSandboxSlotSecurity,
            )

            security_factory = WindowsSandboxSlotSecurity

        return security_factory(
            self.root,
            helper,
            expected_helper_sha256=helper_digest,
        )

    def prepare_activation(
        self,
        manifest: ReleaseManifest,
        transaction_dir: Path,
    ) -> Path:
        verify_manifest_signature(manifest, self.verifier)
        artifact = self._artifact(manifest)
        transaction_dir = Path(transaction_dir)
        transaction_id = transaction_dir.name
        if _SAFE_TRANSACTION_ID.fullmatch(transaction_id) is None:
            raise BootstrapCompanionError(
                "Bootstrap transaction identity is invalid"
            )
        archive = transaction_dir / "bootstrap-companion" / artifact.file_name
        verify_artifact_file(archive, manifest, artifact, self.verifier)
        launcher = self._install_versioned(manifest, artifact, archive)
        self._prepare_desktop_entry(
            manifest,
            launcher,
            transaction_id=transaction_id,
        )
        return launcher

    def prepare_transaction(self, transaction_id: str) -> Path:
        if _SAFE_TRANSACTION_ID.fullmatch(transaction_id) is None:
            raise BootstrapCompanionError("Bootstrap transaction identity is invalid")
        transaction_dir = self.root / "transactions" / transaction_id
        manifest_path = transaction_dir / "release-manifest.json"
        try:
            payload = manifest_path.read_bytes()
        except OSError as error:
            raise BootstrapCompanionError(
                "Bootstrap transaction manifest is unavailable"
            ) from error
        if not 1 <= len(payload) <= 1024 * 1024:
            raise BootstrapCompanionError(
                "Bootstrap transaction manifest is outside its bound"
            )
        manifest = ReleaseManifest.from_json(payload)
        return self.prepare_activation(manifest, transaction_dir)

    def commit_activation(self, transaction_id: str) -> None:
        record_path = self._activation_record_path(transaction_id)
        desktop = self._resolved_desktop()
        record = _load_activation_record(
            record_path,
            root=self.root,
            desktop=desktop,
            platform=self.platform,
            transaction_id=transaction_id,
        )
        if record is None:
            raise BootstrapCompanionError(
                "Bootstrap desktop activation record is unavailable"
            )
        if record["state"] == "committed":
            self._cleanup_activation_backup(record)
            return
        if record["state"] != "prepared":
            raise BootstrapCompanionError(
                "Bootstrap desktop activation cannot be committed"
            )
        entry = _record_entry_path(record, desktop, self.platform)
        if _entry_digest(entry, self.platform) != record["new_digest"]:
            raise BootstrapCompanionError(
                "prepared Bootstrap desktop entry changed before commit"
            )
        new_receipt = record["new_receipt"]
        if not isinstance(new_receipt, Mapping):
            raise BootstrapCompanionError(
                "prepared Bootstrap desktop receipt is invalid"
            )
        atomic_write_json(
            self.root / "bootstrap" / "desktop-entry.json",
            dict(new_receipt),
        )
        updated = dict(record)
        updated["state"] = "committed"
        atomic_write_json(record_path, updated)
        self._cleanup_activation_backup(updated)

    def rollback_activation(self, transaction_id: str) -> None:
        record_path = self._activation_record_path(transaction_id)
        desktop = self._resolved_desktop()
        record = _load_activation_record(
            record_path,
            root=self.root,
            desktop=desktop,
            platform=self.platform,
            transaction_id=transaction_id,
        )
        if record is None:
            return
        if record["state"] == "rolled_back":
            self._cleanup_activation_backup(record, desktop=desktop)
            return
        if record["state"] == "committed":
            raise BootstrapCompanionError(
                "committed Bootstrap desktop activation cannot be rolled back"
            )
        if record["state"] != "rolling_back":
            rolling_back = dict(record)
            rolling_back["state"] = "rolling_back"
            atomic_write_json(record_path, rolling_back)
            record = rolling_back
        self._restore_desktop_entry(record, desktop=desktop)
        updated = dict(record)
        updated["state"] = "rolled_back"
        atomic_write_json(record_path, updated)
        self._cleanup_activation_backup(updated, desktop=desktop)

    def converge_activation(self) -> None:
        """Finish a crash-interrupted desktop transaction from durable facts."""

        directory = self.root / "bootstrap" / "companion-transactions"
        if not directory.exists():
            return
        _require_real_directory(directory, "Bootstrap companion transactions")
        desktop = self._resolved_desktop()
        for record_path in sorted(directory.glob("*/activation.json")):
            transaction_id = record_path.parent.name
            if _SAFE_TRANSACTION_ID.fullmatch(transaction_id) is None:
                continue
            record = _load_activation_record(
                record_path,
                root=self.root,
                desktop=desktop,
                platform=self.platform,
                transaction_id=transaction_id,
            )
            if record is None or record["state"] not in {
                "preparing",
                "prepared",
                "rolling_back",
                "committed",
            }:
                continue
            if record["state"] == "committed":
                self.commit_activation(transaction_id)
                continue
            if record["state"] == "rolling_back":
                self.rollback_activation(transaction_id)
                continue
            receipt = _read_bounded_json(self.root / "activation-receipt.json")
            intent = _read_bounded_json(self.root / "activation-intent.json")
            if (
                isinstance(receipt, Mapping)
                and receipt.get("transaction_id") == transaction_id
                and receipt.get("state") == "confirmed"
            ):
                self.commit_activation(transaction_id)
            elif (
                isinstance(receipt, Mapping)
                and receipt.get("transaction_id") == transaction_id
                and receipt.get("state") == "rolled_back_pre_data"
            ) or (
                not (
                    isinstance(intent, Mapping)
                    and intent.get("transaction_id") == transaction_id
                )
                and not self._current_release_matches(record["release_id"])
            ):
                self.rollback_activation(transaction_id)

    def install_existing(
        self,
        manifest: ReleaseManifest,
        artifacts_directory: Path,
    ) -> Path:
        transaction_dir = self.root / "bootstrap" / "repair" / manifest.release_id
        self.stage_from_directory(
            manifest,
            Path(artifacts_directory),
            transaction_dir,
        )
        repair_id = hashlib.sha256(
            ("repair\0" + manifest.release_id).encode()
        ).hexdigest()[:32]
        canonical_transaction = self.root / "transactions" / repair_id
        if canonical_transaction.exists():
            shutil.rmtree(canonical_transaction)
        canonical_transaction.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(transaction_dir, canonical_transaction)
        try:
            launcher = self.prepare_activation(manifest, canonical_transaction)
            self.commit_activation(repair_id)
            return launcher
        finally:
            if canonical_transaction.exists():
                shutil.rmtree(canonical_transaction)

    def _activation_record_path(self, transaction_id: str) -> Path:
        if _SAFE_TRANSACTION_ID.fullmatch(transaction_id) is None:
            raise BootstrapCompanionError("Bootstrap transaction identity is invalid")
        return (
            self.root
            / "bootstrap"
            / "companion-transactions"
            / transaction_id
            / "activation.json"
        )

    def _resolved_desktop(self, *, create: bool = False) -> Path:
        raw = self._desktop_directory or _desktop_directory(self.platform)
        desktop = Path(os.path.abspath(raw))
        if create:
            desktop.mkdir(parents=False, exist_ok=True)
        _require_real_directory(desktop, "Desktop")
        return desktop

    def _current_release_matches(self, release_id: str) -> bool:
        pointers = _read_bounded_json(self.root / "slot-pointers.json")
        if not isinstance(pointers, Mapping):
            return False
        current = pointers.get("current")
        if not isinstance(current, str):
            return False
        manifest = _read_bounded_json(
            self.root / "slots" / current / "release-manifest.json"
        )
        return (
            isinstance(manifest, Mapping)
            and manifest.get("release_id") == release_id
        )

    def _artifact(self, manifest: ReleaseManifest) -> ReleaseArtifact:
        artifact = manifest.artifact(self.artifact_id)
        if (
            artifact.platform != self.platform
            or artifact.architecture != self.architecture
            or artifact.size_bytes > MAX_BOOTSTRAP_BYTES
        ):
            raise BootstrapCompanionError(
                "signed Bootstrap companion target is invalid"
            )
        verify_artifact_signature(manifest, artifact, self.verifier)
        return artifact

    def _install_versioned(
        self,
        manifest: ReleaseManifest,
        artifact: ReleaseArtifact,
        archive: Path,
    ) -> Path:
        if _SAFE_RELEASE_ID.fullmatch(manifest.release_id) is None:
            raise BootstrapCompanionError("release identity cannot own a Bootstrap")
        versions = _ensure_real_directory_chain(
            self.root,
            ("bootstrap", "versions"),
            "versioned Bootstrap directory",
        )
        staging = Path(
            tempfile.mkdtemp(
                prefix=f".{manifest.release_id}.staging-",
                dir=versions,
            )
        )
        _require_real_directory_chain(
            self.root,
            staging,
            "versioned Bootstrap staging directory",
        )
        try:
            expected, launcher_relative = self._extract_and_validate(
                staging,
                manifest,
                artifact,
                archive,
            )
            final = versions / manifest.release_id
            if _lexists(final):
                if not _trees_match(
                    staging,
                    final,
                    expected,
                    chain_root=self.root,
                ):
                    raise BootstrapCompanionError(
                        "immutable versioned Bootstrap differs from signed bytes"
                    )
                launcher = final.joinpath(*launcher_relative.split("/"))
                _require_real_file_chain(
                    self.root,
                    launcher,
                    "versioned Bootstrap launcher",
                )
                return launcher
            os.replace(staging, final)
            _versioned_tree_projection(
                final,
                expected,
                chain_root=self.root,
            )
            launcher = final.joinpath(*launcher_relative.split("/"))
            _require_real_file_chain(
                self.root,
                launcher,
                "versioned Bootstrap launcher",
            )
            return launcher
        finally:
            if _lexists(staging):
                _require_real_directory_chain(
                    self.root,
                    staging,
                    "versioned Bootstrap staging directory",
                )
                shutil.rmtree(staging)

    def _extract_and_validate(
        self,
        destination: Path,
        manifest: ReleaseManifest,
        artifact: ReleaseArtifact,
        archive_path: Path,
    ) -> tuple[frozenset[str], str]:
        launcher_relative = (
            "bin/ecorex-bootstrap.exe"
            if self.platform == "windows"
            else "bin/ecorex-bootstrap"
        )
        expected = {"bootstrap-config.json", launcher_relative}
        if self.platform == "windows":
            expected.add("bin/ecorex-sandbox-host.exe")
        observed: set[str] = set()
        expanded = 0
        try:
            archive = zipfile.ZipFile(archive_path)
        except (OSError, zipfile.BadZipFile) as error:
            raise BootstrapCompanionError(
                "signed Bootstrap companion archive is unreadable"
            ) from error
        with archive:
            for member in archive.infolist():
                name = member.filename.replace("\\", "/").rstrip("/")
                if not name or name == "bin" and member.is_dir():
                    continue
                if (
                    member.is_dir()
                    or name not in expected
                    or name in observed
                    or Path(name).is_absolute()
                    or ".." in Path(name).parts
                    or stat.S_ISLNK(member.external_attr >> 16)
                ):
                    raise BootstrapCompanionError(
                        "signed Bootstrap companion layout is invalid"
                    )
                expanded += member.file_size
                if expanded > MAX_BOOTSTRAP_EXPANDED_BYTES:
                    raise BootstrapCompanionError(
                        "signed Bootstrap companion expands beyond its bound"
                    )
                target = destination.joinpath(*name.split("/"))
                target.parent.mkdir(parents=True, exist_ok=True)
                mode = 0o700 if name != "bootstrap-config.json" else 0o600
                with archive.open(member) as source, target.open("xb") as output:
                    shutil.copyfileobj(source, output, length=1024 * 1024)
                    output.flush()
                    os.fsync(output.fileno())
                target.chmod(mode)
                observed.add(name)
        if observed != expected:
            raise BootstrapCompanionError(
                "signed Bootstrap companion is incomplete"
            )
        self._validate_staged_config(
            destination / "bootstrap-config.json",
            destination,
            manifest,
            artifact,
        )
        return frozenset(expected), launcher_relative

    def _validate_staged_config(
        self,
        config_path: Path,
        extracted_root: Path,
        manifest: ReleaseManifest,
        artifact: ReleaseArtifact,
    ) -> None:
        try:
            payload = config_path.read_bytes()
            raw = json.loads(payload)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise BootstrapCompanionError(
                "versioned Bootstrap configuration is unreadable"
            ) from error
        required = {
            "schema_version",
            "public_index_url",
            "release_public_keys",
            "publication_public_keys",
            "sandbox_helper_sha256",
            "minimum_stable",
        }
        if not isinstance(raw, dict) or set(raw) != required or raw["schema_version"] != 1:
            raise BootstrapCompanionError(
                "versioned Bootstrap configuration contract is invalid"
            )
        endpoint = urlsplit(raw["public_index_url"])
        if (
            not isinstance(raw["public_index_url"], str)
            or endpoint.scheme != "https"
            or not endpoint.hostname
            or endpoint.username is not None
            or endpoint.query
            or endpoint.fragment
        ):
            raise BootstrapCompanionError(
                "versioned Bootstrap discovery endpoint is invalid"
            )
        release_keys = _decode_keyring(raw["release_public_keys"])
        publication_keys = _decode_keyring(raw["publication_public_keys"])
        if set(release_keys) & set(publication_keys) or {
            value for value in release_keys.values()
        } & {value for value in publication_keys.values()}:
            raise BootstrapCompanionError(
                "versioned Bootstrap signing roles overlap"
            )
        signer_fingerprint = getattr(self.verifier, "key_fingerprint", lambda _id: None)(
            artifact.signature.key_id
        )
        staged_signer = release_keys.get(artifact.signature.key_id)
        if (
            signer_fingerprint is None
            or staged_signer is None
            or hashlib.sha256(staged_signer).hexdigest() != signer_fingerprint
        ):
            raise BootstrapCompanionError(
                "versioned Bootstrap key rotation has no signer continuity"
            )
        minimum = raw["minimum_stable"]
        if not isinstance(minimum, dict) or set(minimum) != {
            "sequence",
            "version",
            "signature",
        }:
            raise BootstrapCompanionError(
                "versioned Bootstrap minimum target is invalid"
            )
        match = _STABLE_V1.fullmatch(str(minimum.get("version", "")))
        expected_sequence = (
            int(match.group(1)) * 1_000_000 + int(match.group(2)) + 1
            if match is not None
            else -1
        )
        if (
            minimum.get("version") != manifest.version
            or minimum.get("sequence") != expected_sequence
            or not isinstance(minimum.get("signature"), Mapping)
        ):
            raise BootstrapCompanionError(
                "versioned Bootstrap minimum target is invalid"
            )
        signature = SignatureEnvelope.from_dict(minimum["signature"])
        minimum_payload = (
            "ecorex.bootstrap-minimum-stable.v1"
            + "\0"
            + str(expected_sequence)
            + "\0"
            + manifest.version
        ).encode()
        staged_verifier = Ed25519SignatureVerifier(release_keys)
        if staged_verifier.verify(minimum_payload, signature) is not True:
            raise BootstrapCompanionError(
                "versioned Bootstrap minimum target signature is invalid"
            )
        helper_digest = raw["sandbox_helper_sha256"]
        if self.platform == "windows":
            helper = extracted_root / "bin" / "ecorex-sandbox-host.exe"
            if (
                not isinstance(helper_digest, str)
                or _SHA256.fullmatch(helper_digest) is None
                or sha256_file(helper) != helper_digest
            ):
                raise BootstrapCompanionError(
                    "versioned Bootstrap sandbox helper is invalid"
                )
        elif helper_digest != "":
            raise BootstrapCompanionError(
                "macOS Bootstrap unexpectedly declares a sandbox helper"
            )

    def _prepare_desktop_entry(
        self,
        manifest: ReleaseManifest,
        launcher: Path,
        *,
        transaction_id: str,
    ) -> Path:
        desktop = self._resolved_desktop(create=True)
        receipt_path = self.root / "bootstrap" / "desktop-entry.json"
        receipt = _load_entry_receipt(receipt_path, self.root, desktop, self.platform)
        record_path = self._activation_record_path(transaction_id)
        existing_record = _load_activation_record(
            record_path,
            root=self.root,
            desktop=desktop,
            platform=self.platform,
            transaction_id=transaction_id,
        )
        if existing_record is not None:
            if (
                existing_record["state"] in {"prepared", "committed"}
                and existing_record["release_id"] == manifest.release_id
                and _entry_digest(
                    _record_entry_path(
                        existing_record,
                        desktop,
                        self.platform,
                    ),
                    self.platform,
                )
                == existing_record["new_digest"]
            ):
                if existing_record["state"] == "committed":
                    self._cleanup_activation_backup(
                        existing_record,
                        desktop=desktop,
                    )
                return _record_entry_path(
                    existing_record,
                    desktop,
                    self.platform,
                )
            if existing_record["state"] in {
                "preparing",
                "prepared",
                "rolling_back",
            }:
                self.rollback_activation(transaction_id)
            elif (
                existing_record["state"] == "rolled_back"
                and existing_record["release_id"] == manifest.release_id
            ):
                self._cleanup_activation_backup(
                    existing_record,
                    desktop=desktop,
                )
            else:
                raise BootstrapCompanionError(
                    "Bootstrap desktop activation transaction was already resolved"
                )
        names = _WINDOWS_ENTRY_NAMES if self.platform == "windows" else _MAC_ENTRY_NAMES
        selected: Path | None = None
        if receipt is not None:
            candidate = Path(receipt["entry_path"])
            if _entry_matches_receipt(candidate, receipt):
                selected = candidate
        if selected is None:
            for name in names:
                candidate = desktop / name
                if not _lexists(candidate):
                    selected = candidate
                    break
                if _entry_is_product_owned(
                    candidate,
                    root=self.root,
                    platform=self.platform,
                ):
                    selected = candidate
                    break
        if selected is None:
            raise BootstrapCompanionError(
                "all safe EcoreX desktop entry names are occupied"
            )
        selected = _require_desktop_entry_path(
            selected,
            desktop,
            self.platform,
        )
        record_directory = record_path.parent
        record_directory.mkdir(parents=True, exist_ok=True)
        _require_real_directory(
            record_directory,
            "Bootstrap companion transaction",
        )
        backup = record_directory / (
            "desktop-entry-backup.lnk"
            if self.platform == "windows"
            else "desktop-entry-backup.app"
        )
        backup_staging = _backup_staging_path(backup)
        if _lexists(backup):
            _recover_unrecorded_backup(
                backup,
                selected,
                platform=self.platform,
            )
        if _lexists(backup_staging):
            _remove_internal_backup_path(
                backup_staging,
                platform=self.platform,
            )
        prior_present = _lexists(selected)
        prior_digest: str | None = None
        prior_receipt: Mapping[str, Any] | None = None
        if prior_present:
            prior_digest = _entry_digest(selected, self.platform)
            if (
                receipt is not None
                and Path(receipt["entry_path"]) == selected
                and _entry_matches_receipt(selected, receipt)
            ):
                prior_receipt = receipt
        kind = "windows-lnk" if self.platform == "windows" else "macos-app"
        preparing: dict[str, Any] = {
            "schema_version": DESKTOP_ACTIVATION_SCHEMA_VERSION,
            "transaction_id": transaction_id,
            "state": "preparing",
            "platform": self.platform,
            "entry_kind": kind,
            "entry_path": str(selected),
            "backup_path": str(backup),
            "prior_present": prior_present,
            "prior_digest": prior_digest,
            "prior_receipt": prior_receipt,
            "release_id": manifest.release_id,
            "new_digest": None,
            "new_receipt": None,
        }
        atomic_write_json(record_path, preparing)
        if prior_present:
            if self.platform == "windows":
                shutil.copyfile(selected, backup_staging)
            else:
                shutil.copytree(selected, backup_staging, symlinks=True)
            if _entry_digest(backup_staging, self.platform) != prior_digest:
                raise BootstrapCompanionError(
                    "Bootstrap desktop activation backup changed"
                )
            os.replace(backup_staging, backup)
        if self.platform == "windows":
            _write_windows_shortcut(selected, self.root, launcher)
            digest = _entry_digest(selected, self.platform)
        else:
            _write_mac_app(selected, self.root, launcher)
            digest = _entry_digest(selected, self.platform)
        new_receipt = {
            "schema_version": DESKTOP_ENTRY_SCHEMA_VERSION,
            "platform": self.platform,
            "entry_kind": kind,
            "entry_name": selected.name,
            "entry_path": str(selected),
            "install_root": str(self.root),
            "launcher_path": str(launcher),
            "release_id": manifest.release_id,
            "entry_digest": digest,
        }
        prepared = dict(preparing)
        prepared["state"] = "prepared"
        prepared["new_digest"] = digest
        prepared["new_receipt"] = new_receipt
        atomic_write_json(record_path, prepared)
        return selected

    def _restore_desktop_entry(
        self,
        record: Mapping[str, Any],
        *,
        desktop: Path,
    ) -> None:
        entry = _record_entry_path(record, desktop, self.platform)
        backup = Path(record["backup_path"])
        state = record["state"]
        current_digest: str | None = None
        if _lexists(entry):
            current_digest = _entry_digest(entry, self.platform)
        new_digest = record.get("new_digest")
        prior_digest = record.get("prior_digest")
        if current_digest is not None:
            known_digest = current_digest in {
                digest
                for digest in (new_digest, prior_digest)
                if isinstance(digest, str)
            }
            preparing_product_entry = (
                new_digest is None
                and _entry_is_product_owned(
                    entry,
                    root=self.root,
                    platform=self.platform,
                )
            )
            if not known_digest and not preparing_product_entry:
                raise BootstrapCompanionError(
                    "Bootstrap desktop entry changed before rollback"
                )
        if state not in {"rolling_back", "preparing", "prepared"}:
            raise BootstrapCompanionError(
                "Bootstrap desktop activation cannot be restored"
            )
        if record["prior_present"]:
            if _lexists(backup):
                if _entry_digest(backup, self.platform) != prior_digest:
                    raise BootstrapCompanionError(
                        "Bootstrap desktop activation backup changed"
                    )
                if _lexists(entry):
                    _remove_desktop_entry_path(
                        entry,
                        desktop=desktop,
                        platform=self.platform,
                    )
                os.replace(backup, entry)
            elif current_digest != prior_digest:
                raise BootstrapCompanionError(
                    "Bootstrap desktop activation backup is unavailable"
                )
            if _entry_digest(entry, self.platform) != prior_digest:
                raise BootstrapCompanionError(
                    "restored Bootstrap desktop entry changed"
                )
        elif _lexists(entry):
            _remove_desktop_entry_path(
                entry,
                desktop=desktop,
                platform=self.platform,
            )
        receipt_path = self.root / "bootstrap" / "desktop-entry.json"
        prior_receipt = record["prior_receipt"]
        if isinstance(prior_receipt, Mapping):
            validated_receipt = _validate_entry_receipt_payload(
                prior_receipt,
                self.root,
                desktop,
                self.platform,
            )
            if validated_receipt is None:
                raise BootstrapCompanionError(
                    "prior Bootstrap desktop receipt is invalid"
                )
            atomic_write_json(receipt_path, dict(prior_receipt))
        else:
            receipt_path.unlink(missing_ok=True)

    def _cleanup_activation_backup(
        self,
        record: Mapping[str, Any],
        *,
        desktop: Path | None = None,
    ) -> None:
        desktop = desktop or self._resolved_desktop()
        _record_entry_path(record, desktop, self.platform)
        backup = Path(record["backup_path"])
        backup_staging = _backup_staging_path(backup)
        if _lexists(backup_staging):
            _remove_internal_backup_path(
                backup_staging,
                platform=self.platform,
            )
        if not _lexists(backup):
            return
        prior_digest = record.get("prior_digest")
        if (
            not isinstance(prior_digest, str)
            or _entry_digest(backup, self.platform) != prior_digest
        ):
            raise BootstrapCompanionError(
                "Bootstrap desktop activation backup changed"
            )
        _remove_internal_backup_path(
            backup,
            platform=self.platform,
        )

    def remove_desktop_entry(self) -> bool:
        """Remove only the exact product-owned entry recorded in our receipt."""

        desktop = self._resolved_desktop()
        receipt_path = self.root / "bootstrap" / "desktop-entry.json"
        receipt = _load_entry_receipt(
            receipt_path,
            self.root,
            desktop,
            self.platform,
        )
        if receipt is None:
            return False
        entry = _require_desktop_entry_path(
            Path(receipt["entry_path"]),
            desktop,
            self.platform,
        )
        if not _entry_matches_receipt(entry, receipt):
            return False
        _remove_desktop_entry_path(
            entry,
            desktop=desktop,
            platform=self.platform,
        )
        receipt_path.unlink(missing_ok=True)
        return True


def _decode_keyring(value: Any) -> dict[str, bytes]:
    if not isinstance(value, dict) or not 1 <= len(value) <= 8:
        raise BootstrapCompanionError("versioned Bootstrap keyring is invalid")
    result: dict[str, bytes] = {}
    for key_id, encoded in value.items():
        if (
            not isinstance(key_id, str)
            or _SAFE_KEY_ID.fullmatch(key_id) is None
            or not isinstance(encoded, str)
        ):
            raise BootstrapCompanionError("versioned Bootstrap keyring is invalid")
        try:
            raw = base64.b64decode(encoded, validate=True)
        except ValueError as error:
            raise BootstrapCompanionError(
                "versioned Bootstrap keyring is invalid"
            ) from error
        if len(raw) != 32:
            raise BootstrapCompanionError("versioned Bootstrap keyring is invalid")
        result[key_id] = raw
    return result


def _trees_match(
    left: Path,
    right: Path,
    expected: frozenset[str],
    *,
    chain_root: Path,
) -> bool:
    left_projection = _versioned_tree_projection(
        left,
        expected,
        chain_root=chain_root,
    )
    right_projection = _versioned_tree_projection(
        right,
        expected,
        chain_root=chain_root,
    )
    return left_projection == right_projection


def _versioned_tree_projection(
    root: Path,
    expected: frozenset[str],
    *,
    chain_root: Path,
) -> dict[str, str]:
    """Read an immutable Bootstrap tree without ever following a link."""

    _require_real_directory_chain(
        chain_root,
        root,
        "versioned Bootstrap tree",
    )
    allowed_files = set(expected)
    allowed_directories = {
        parent.as_posix()
        for relative in expected
        for parent in Path(relative).parents
        if parent != Path(".")
    }
    observed_files: dict[str, str] = {}
    observed_directories: set[str] = set()

    def visit(directory: Path) -> None:
        try:
            entries = list(os.scandir(directory))
        except OSError as error:
            raise BootstrapCompanionError(
                "versioned Bootstrap tree is unreadable"
            ) from error
        for item in entries:
            path = Path(item.path)
            try:
                metadata = path.lstat()
            except OSError as error:
                raise BootstrapCompanionError(
                    "versioned Bootstrap tree changed during validation"
                ) from error
            if _metadata_is_link_or_reparse(metadata):
                raise BootstrapCompanionError(
                    "versioned Bootstrap tree contains a link or reparse point"
                )
            relative = path.relative_to(root).as_posix()
            if stat.S_ISDIR(metadata.st_mode):
                if relative not in allowed_directories:
                    raise BootstrapCompanionError(
                        "versioned Bootstrap tree contains an unexpected directory"
                    )
                observed_directories.add(relative)
                visit(path)
            elif stat.S_ISREG(metadata.st_mode):
                if relative not in allowed_files:
                    raise BootstrapCompanionError(
                        "versioned Bootstrap tree contains an unexpected file"
                    )
                observed_files[relative] = sha256_file(path)
            else:
                raise BootstrapCompanionError(
                    "versioned Bootstrap tree contains a special file"
                )

    visit(root)
    if (
        set(observed_files) != allowed_files
        or observed_directories != allowed_directories
    ):
        raise BootstrapCompanionError(
            "versioned Bootstrap tree is incomplete"
        )
    return observed_files


def _ensure_real_directory_chain(
    root: Path,
    relative_parts: tuple[str, ...],
    label: str,
) -> Path:
    root = Path(os.path.abspath(root))
    if not _lexists(root):
        root.mkdir(parents=True)
    _require_real_directory(root, "EcoreX install root")
    current = root
    for part in relative_parts:
        if not part or part in {".", ".."} or Path(part).name != part:
            raise BootstrapCompanionError(f"{label} is invalid")
        current = current / part
        if not _lexists(current):
            current.mkdir()
        _require_real_directory(current, label)
    return current


def _require_real_directory_chain(
    root: Path,
    path: Path,
    label: str,
) -> None:
    root = Path(os.path.abspath(root))
    path = Path(os.path.abspath(path))
    try:
        relative = path.relative_to(root)
    except ValueError as error:
        raise BootstrapCompanionError(f"{label} escapes its root") from error
    _require_real_directory(root, "EcoreX install root")
    current = root
    for part in relative.parts:
        current = current / part
        _require_real_directory(current, label)


def _require_real_file_chain(
    root: Path,
    path: Path,
    label: str,
) -> None:
    path = Path(os.path.abspath(path))
    _require_real_directory_chain(root, path.parent, label)
    try:
        metadata = path.lstat()
    except OSError as error:
        raise BootstrapCompanionError(f"{label} is unavailable") from error
    if (
        _metadata_is_link_or_reparse(metadata)
        or not stat.S_ISREG(metadata.st_mode)
    ):
        raise BootstrapCompanionError(f"{label} is unsafe")


def _desktop_directory(platform: str) -> Path:
    if platform == "windows":
        if os.name != "nt":
            raise BootstrapCompanionError("Windows Desktop is unavailable")
        class _GUID(ctypes.Structure):
            _fields_ = (
                ("data1", wintypes.DWORD),
                ("data2", wintypes.WORD),
                ("data3", wintypes.WORD),
                ("data4", ctypes.c_ubyte * 8),
            )

        folder_id = _GUID(
            0xB4BFCC3A,
            0xDB2C,
            0x424C,
            (ctypes.c_ubyte * 8)(
                0xB0,
                0x29,
                0x7F,
                0xE9,
                0x9A,
                0x87,
                0xC6,
                0x41,
            ),
        )
        raw_path = ctypes.c_void_p()
        shell32 = ctypes.windll.shell32
        shell32.SHGetKnownFolderPath.argtypes = (
            ctypes.POINTER(_GUID),
            wintypes.DWORD,
            wintypes.HANDLE,
            ctypes.POINTER(ctypes.c_void_p),
        )
        shell32.SHGetKnownFolderPath.restype = ctypes.c_long
        result = shell32.SHGetKnownFolderPath(
            ctypes.byref(folder_id),
            0,
            None,
            ctypes.byref(raw_path),
        )
        if result != 0 or not raw_path.value:
            raise BootstrapCompanionError("Windows Desktop is unavailable")
        try:
            return Path(ctypes.wstring_at(raw_path.value))
        finally:
            ctypes.windll.ole32.CoTaskMemFree(raw_path)
    if sys.platform != "darwin":
        raise BootstrapCompanionError("macOS Desktop is unavailable")
    return Path.home() / "Desktop"


def _load_entry_receipt(
    path: Path,
    root: Path,
    desktop: Path,
    platform: str,
) -> dict[str, Any] | None:
    raw = _read_bounded_json(path)
    return _validate_entry_receipt_payload(raw, root, desktop, platform)


def _validate_entry_receipt_payload(
    raw: Mapping[str, Any] | None,
    root: Path,
    desktop: Path,
    platform: str,
) -> dict[str, Any] | None:
    if not isinstance(raw, Mapping):
        return None
    raw = dict(raw)
    required = {
        "schema_version",
        "platform",
        "entry_kind",
        "entry_name",
        "entry_path",
        "install_root",
        "launcher_path",
        "release_id",
        "entry_digest",
    }
    expected_kind = "windows-lnk" if platform == "windows" else "macos-app"
    names = _WINDOWS_ENTRY_NAMES if platform == "windows" else _MAC_ENTRY_NAMES
    launcher_name = (
        "ecorex-bootstrap.exe" if platform == "windows" else "ecorex-bootstrap"
    )
    entry_name = raw.get("entry_name")
    expected_entry = (
        desktop / entry_name
        if isinstance(entry_name, str) and entry_name in names
        else None
    )
    if (
        set(raw) != required
        or raw["schema_version"] != DESKTOP_ENTRY_SCHEMA_VERSION
        or raw["platform"] != platform
        or raw["entry_kind"] != expected_kind
        or raw["install_root"] != str(root)
        or expected_entry is None
        or not isinstance(raw["entry_path"], str)
        or raw["entry_path"] != str(expected_entry)
        or Path(raw["entry_path"]) != expected_entry
        or not isinstance(raw["launcher_path"], str)
        or Path(raw["launcher_path"]).name != launcher_name
        or not _is_relative_to(
            Path(raw["launcher_path"]),
            root / "bootstrap" / "versions",
        )
        or not isinstance(raw["release_id"], str)
        or _SAFE_RELEASE_ID.fullmatch(raw["release_id"]) is None
        or not isinstance(raw["entry_digest"], str)
        or _SHA256.fullmatch(raw["entry_digest"]) is None
    ):
        return None
    return raw


def _load_activation_record(
    path: Path,
    *,
    root: Path,
    desktop: Path,
    platform: str,
    transaction_id: str,
) -> dict[str, Any] | None:
    raw = _read_bounded_json(path)
    if raw is None:
        return None
    required = {
        "schema_version",
        "transaction_id",
        "state",
        "platform",
        "entry_kind",
        "entry_path",
        "backup_path",
        "prior_present",
        "prior_digest",
        "prior_receipt",
        "release_id",
        "new_digest",
        "new_receipt",
    }
    expected_kind = "windows-lnk" if platform == "windows" else "macos-app"
    names = _WINDOWS_ENTRY_NAMES if platform == "windows" else _MAC_ENTRY_NAMES
    expected_backup = path.parent / (
        "desktop-entry-backup.lnk"
        if platform == "windows"
        else "desktop-entry-backup.app"
    )
    if (
        not isinstance(raw, dict)
        or set(raw) != required
        or raw["schema_version"] != DESKTOP_ACTIVATION_SCHEMA_VERSION
        or raw["transaction_id"] != transaction_id
        or raw["state"] not in {
            "preparing",
            "prepared",
            "rolling_back",
            "committed",
            "rolled_back",
        }
        or raw["platform"] != platform
        or raw["entry_kind"] != expected_kind
        or not isinstance(raw["entry_path"], str)
        or Path(raw["entry_path"]).name not in names
        or raw["entry_path"]
        != str(desktop / Path(raw["entry_path"]).name)
        or Path(raw["entry_path"])
        != desktop / Path(raw["entry_path"]).name
        or Path(raw["backup_path"]) != expected_backup
        or not isinstance(raw["prior_present"], bool)
        or (
            raw["prior_digest"] is not None
            and (
                not isinstance(raw["prior_digest"], str)
                or _SHA256.fullmatch(raw["prior_digest"]) is None
            )
        )
        or raw["prior_present"] != (raw["prior_digest"] is not None)
        or (
            raw["prior_receipt"] is not None
            and _validate_entry_receipt_payload(
                raw["prior_receipt"],
                root,
                desktop,
                platform,
            )
            is None
        )
        or not isinstance(raw["release_id"], str)
        or _SAFE_RELEASE_ID.fullmatch(raw["release_id"]) is None
        or (
            raw["new_digest"] is not None
            and (
                not isinstance(raw["new_digest"], str)
                or _SHA256.fullmatch(raw["new_digest"]) is None
            )
        )
        or (
            raw["new_receipt"] is not None
            and _validate_entry_receipt_payload(
                raw["new_receipt"],
                root,
                desktop,
                platform,
            )
            is None
        )
        or (
            raw["state"] in {"prepared", "committed"}
            and (raw["new_digest"] is None or raw["new_receipt"] is None)
        )
        or (
            isinstance(raw["new_receipt"], Mapping)
            and (
                raw["new_receipt"].get("entry_path") != raw["entry_path"]
                or raw["new_receipt"].get("release_id") != raw["release_id"]
                or raw["new_receipt"].get("entry_digest") != raw["new_digest"]
            )
        )
        or (
            isinstance(raw["prior_receipt"], Mapping)
            and raw["prior_receipt"].get("entry_path") != raw["entry_path"]
        )
        or not _is_relative_to(path, root / "bootstrap" / "companion-transactions")
    ):
        raise BootstrapCompanionError(
            "Bootstrap desktop activation record is invalid"
        )
    return raw


def _record_entry_path(
    record: Mapping[str, Any],
    desktop: Path,
    platform: str,
) -> Path:
    raw = record.get("entry_path")
    if not isinstance(raw, str):
        raise BootstrapCompanionError(
            "Bootstrap desktop activation entry path is invalid"
        )
    return _require_desktop_entry_path(Path(raw), desktop, platform)


def _backup_staging_path(backup: Path) -> Path:
    return backup.with_name(backup.name + ".staging")


def _recover_unrecorded_backup(
    backup: Path,
    entry: Path,
    *,
    platform: str,
) -> None:
    """Discard only an exact duplicate left before old code wrote its record."""

    if not _lexists(entry):
        raise BootstrapCompanionError(
            "unrecorded Bootstrap desktop backup has no source entry"
        )
    try:
        backup_digest = _entry_digest(backup, platform)
        entry_digest = _entry_digest(entry, platform)
    except (BootstrapCompanionError, OSError, ValueError) as error:
        raise BootstrapCompanionError(
            "unrecorded Bootstrap desktop backup is unsafe"
        ) from error
    if backup_digest != entry_digest:
        raise BootstrapCompanionError(
            "unrecorded Bootstrap desktop backup differs from its source"
        )
    _remove_internal_backup_path(backup, platform=platform)


def _remove_internal_backup_path(path: Path, *, platform: str) -> None:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise BootstrapCompanionError(
            "Bootstrap desktop backup is unavailable"
        ) from error
    if _metadata_is_link_or_reparse(metadata):
        raise BootstrapCompanionError(
            "Bootstrap desktop backup became unsafe"
        )
    if platform == "windows":
        if not stat.S_ISREG(metadata.st_mode):
            raise BootstrapCompanionError(
                "Windows desktop backup became unsafe"
            )
        path.unlink()
    else:
        if not stat.S_ISDIR(metadata.st_mode):
            raise BootstrapCompanionError(
                "macOS desktop backup became unsafe"
            )
        shutil.rmtree(path)


def _require_desktop_entry_path(
    entry: Path,
    desktop: Path,
    platform: str,
) -> Path:
    names = _WINDOWS_ENTRY_NAMES if platform == "windows" else _MAC_ENTRY_NAMES
    desktop = Path(os.path.abspath(desktop))
    _require_real_directory(desktop, "Desktop")
    expected = desktop / entry.name
    if (
        not entry.is_absolute()
        or entry.name not in names
        or str(entry) != str(expected)
        or entry != expected
    ):
        raise BootstrapCompanionError(
            "Bootstrap desktop entry is outside the resolved Desktop"
        )
    return expected


def _remove_desktop_entry_path(
    entry: Path,
    *,
    desktop: Path,
    platform: str,
) -> None:
    entry = _require_desktop_entry_path(entry, desktop, platform)
    try:
        metadata = entry.lstat()
    except OSError as error:
        raise BootstrapCompanionError(
            "Bootstrap desktop entry is unavailable"
        ) from error
    if _metadata_is_link_or_reparse(metadata):
        raise BootstrapCompanionError(
            "Bootstrap desktop entry became unsafe"
        )
    if platform == "windows":
        if not stat.S_ISREG(metadata.st_mode):
            raise BootstrapCompanionError(
                "Windows desktop shortcut became unsafe"
            )
        entry.unlink()
    else:
        if not stat.S_ISDIR(metadata.st_mode):
            raise BootstrapCompanionError(
                "macOS desktop app became unsafe"
            )
        shutil.rmtree(entry)


def _read_bounded_json(path: Path) -> dict[str, Any] | None:
    try:
        metadata = path.lstat()
        if (
            _metadata_is_link_or_reparse(metadata)
            or not stat.S_ISREG(metadata.st_mode)
            or not 1 <= metadata.st_size <= 1024 * 1024
        ):
            return None
        payload = path.read_bytes()
        if len(payload) != metadata.st_size:
            return None
        raw = json.loads(payload)
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return raw if isinstance(raw, dict) else None


def _entry_digest(path: Path, platform: str) -> str:
    if platform == "windows":
        try:
            metadata = path.lstat()
        except OSError as error:
            raise BootstrapCompanionError(
                "Windows desktop shortcut is unavailable"
            ) from error
        if (
            _metadata_is_link_or_reparse(metadata)
            or not stat.S_ISREG(metadata.st_mode)
            or not 1 <= metadata.st_size <= 1024 * 1024
        ):
            raise BootstrapCompanionError(
                "Windows desktop shortcut is unsafe"
            )
        return sha256_file(path)
    return _tree_digest(path)


def _entry_matches_receipt(path: Path, receipt: Mapping[str, Any]) -> bool:
    try:
        if not _lexists(path):
            return False
        platform = (
            "windows"
            if receipt["entry_kind"] == "windows-lnk"
            else "macos"
        )
        digest = _entry_digest(path, platform)
    except (BootstrapCompanionError, OSError, ValueError):
        return False
    return digest == receipt["entry_digest"]


def _entry_is_product_owned(path: Path, *, root: Path, platform: str) -> bool:
    if platform == "windows":
        if _is_link_or_reparse(path):
            return False
        projection = _read_windows_shortcut(path)
        if projection is None:
            return False
        target = Path(projection["target"])
        arguments = projection["arguments"]
        return (
            projection["description"] == "EcoreX"
            and arguments == f'--launch-installed --install-root "{root}"'
            and _is_relative_to(target, root / "bootstrap" / "versions")
            and target.name.casefold() == "ecorex-bootstrap.exe"
        )
    if _is_link_or_reparse(path):
        return False
    marker = path / "Contents" / "Resources" / "ecorex-entry.json"
    try:
        raw = json.loads(marker.read_bytes())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        raw = None
    if (
        isinstance(raw, dict)
        and set(raw) == {"schema_version", "install_root", "launcher_path"}
        and raw.get("schema_version") == DESKTOP_ENTRY_SCHEMA_VERSION
        and raw.get("install_root") == str(root)
        and isinstance(raw.get("launcher_path"), str)
        and _is_relative_to(
            Path(raw["launcher_path"]),
            root / "bootstrap" / "versions",
        )
        and Path(raw["launcher_path"]).name == "ecorex-bootstrap"
    ):
        return True
    # Migrate the v1 release-candidate app format once.  The exact private
    # marker plus an install-root-bound launcher is required; unrelated .app
    # bundles using the EcoreX name remain untouched.
    legacy_marker = path / "Contents" / "Resources" / ".ecorex-owned"
    executable = path / "Contents" / "MacOS" / "EcoreX"
    try:
        if legacy_marker.read_bytes() != b"ecorex-desktop-entry-v1\n":
            return False
        wrapper = executable.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    prefix = "#!/bin/sh\nexec '"
    suffix = (
        "' --launch-installed --install-root "
        + _shell_quote(str(root))
        + "\n"
    )
    if not wrapper.startswith(prefix) or not wrapper.endswith(suffix):
        return False
    launcher = Path(wrapper[len(prefix) : -len(suffix)])
    return (
        "'" not in str(launcher)
        and launcher.name == "ecorex-bootstrap"
        and _is_relative_to(launcher, root / "bootstrap" / "versions")
    )


def _write_windows_shortcut(path: Path, root: Path, launcher: Path) -> None:
    if os.name != "nt":
        raise BootstrapCompanionError("Windows shortcut creation is unavailable")
    if _lexists(path) and not _entry_is_product_owned(
        path, root=root, platform="windows"
    ):
        raise BootstrapCompanionError("refusing to replace a non-product shortcut")
    temporary = path.with_name(f".{path.stem}.staging-{os.getpid()}.lnk")
    temporary.unlink(missing_ok=True)
    script = ";".join(
        (
            "$ErrorActionPreference='Stop'",
            "$shell=New-Object -ComObject WScript.Shell",
            "$link=$shell.CreateShortcut($env:ECOREX_SHORTCUT_TEMP)",
            "$link.TargetPath=$env:ECOREX_SHORTCUT_TARGET",
            "$link.Arguments=$env:ECOREX_SHORTCUT_ARGUMENTS",
            "$link.WorkingDirectory=$env:ECOREX_SHORTCUT_WORKDIR",
            "$link.Description='EcoreX'",
            "$link.IconLocation=$env:ECOREX_SHORTCUT_TARGET+',0'",
            "$link.WindowStyle=7",
            "$link.Save()",
        )
    )
    environment = _shortcut_environment(
        ECOREX_SHORTCUT_TEMP=str(temporary),
        ECOREX_SHORTCUT_TARGET=str(launcher),
        ECOREX_SHORTCUT_ARGUMENTS=f'--launch-installed --install-root "{root}"',
        ECOREX_SHORTCUT_WORKDIR=str(launcher.parent),
    )
    _run_powershell(script, environment)
    if not temporary.is_file():
        raise BootstrapCompanionError("Windows shortcut was not created")
    os.replace(temporary, path)


def _read_windows_shortcut(path: Path) -> dict[str, str] | None:
    if os.name != "nt" or not path.is_file():
        return None
    script = ";".join(
        (
            "$ErrorActionPreference='Stop'",
            "$shell=New-Object -ComObject WScript.Shell",
            "$link=$shell.CreateShortcut($env:ECOREX_SHORTCUT_PATH)",
            "$record=[ordered]@{target=$link.TargetPath;arguments=$link.Arguments;description=$link.Description}",
            "[Console]::Out.Write(($record|ConvertTo-Json -Compress))",
        )
    )
    try:
        payload = _run_powershell(
            script,
            _shortcut_environment(ECOREX_SHORTCUT_PATH=str(path)),
        )
        raw = json.loads(payload)
    except (BootstrapCompanionError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict) or set(raw) != {
        "target",
        "arguments",
        "description",
    }:
        return None
    if not all(isinstance(value, str) for value in raw.values()):
        return None
    return raw


def _shortcut_environment(**values: str) -> dict[str, str]:
    allowed = {
        key: value
        for key, value in os.environ.items()
        if key.upper()
        in {
            "APPDATA",
            "LOCALAPPDATA",
            "PROGRAMDATA",
            "SYSTEMDRIVE",
            "SYSTEMROOT",
            "TEMP",
            "TMP",
            "USERPROFILE",
            "WINDIR",
        }
    }
    allowed.update(values)
    return allowed


def _run_powershell(script: str, environment: Mapping[str, str]) -> bytes:
    system_root = environment.get("SYSTEMROOT") or environment.get("SystemRoot")
    if not system_root:
        raise BootstrapCompanionError("Windows system root is unavailable")
    executable = (
        Path(system_root)
        / "System32"
        / "WindowsPowerShell"
        / "v1.0"
        / "powershell.exe"
    )
    encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    try:
        result = subprocess.run(
            [
                str(executable),
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-EncodedCommand",
                encoded,
            ],
            env=dict(environment),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise BootstrapCompanionError("Windows shortcut operation failed") from error
    if result.returncode != 0 or len(result.stdout) > 4096 or len(result.stderr) > 4096:
        raise BootstrapCompanionError("Windows shortcut operation failed")
    return result.stdout


def _write_mac_app(path: Path, root: Path, launcher: Path) -> None:
    if _lexists(path) and not _entry_is_product_owned(
        path, root=root, platform="macos"
    ):
        raise BootstrapCompanionError("refusing to replace a non-product app")
    backup = path.with_name(path.name + ".ecorex-previous")
    _recover_mac_swap(path, backup, root)
    staging = Path(
        tempfile.mkdtemp(prefix=f".{path.stem}.staging-", dir=path.parent)
    )
    try:
        executable_dir = staging / "Contents" / "MacOS"
        resources_dir = staging / "Contents" / "Resources"
        executable_dir.mkdir(parents=True)
        resources_dir.mkdir(parents=True)
        wrapper = (
            "#!/bin/sh\nexec "
            + _shell_quote(str(launcher))
            + " --launch-installed --install-root "
            + _shell_quote(str(root))
            + "\n"
        )
        executable = executable_dir / "EcoreX"
        executable.write_text(wrapper, encoding="utf-8")
        executable.chmod(0o700)
        (staging / "Contents" / "Info.plist").write_text(
            """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>CFBundleDisplayName</key><string>EcoreX</string>
<key>CFBundleExecutable</key><string>EcoreX</string>
<key>CFBundleIdentifier</key><string>net.ecoremedia.ecorex.launcher</string>
<key>CFBundlePackageType</key><string>APPL</string>
<key>LSBackgroundOnly</key><true/>
</dict></plist>
""",
            encoding="utf-8",
        )
        (resources_dir / "ecorex-entry.json").write_text(
            json.dumps(
                {
                    "schema_version": DESKTOP_ENTRY_SCHEMA_VERSION,
                    "install_root": str(root),
                    "launcher_path": str(launcher),
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n",
            encoding="utf-8",
        )
        had_prior = _lexists(path)
        if had_prior:
            os.replace(path, backup)
        try:
            os.replace(staging, path)
        except BaseException:
            if had_prior and backup.exists() and not path.exists():
                os.replace(backup, path)
            raise
        if backup.exists():
            shutil.rmtree(backup)
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def _recover_mac_swap(path: Path, backup: Path, root: Path) -> None:
    if _lexists(backup) and not _entry_is_product_owned(
        backup, root=root, platform="macos"
    ):
        raise BootstrapCompanionError("macOS desktop recovery entry is not product-owned")
    if _lexists(path) and _lexists(backup):
        if not _entry_is_product_owned(path, root=root, platform="macos"):
            raise BootstrapCompanionError("macOS desktop entry is not product-owned")
        shutil.rmtree(backup)
    elif not _lexists(path) and _lexists(backup):
        os.replace(backup, path)


def _tree_digest(root: Path) -> str:
    _require_real_directory(root, "desktop app")
    digest = hashlib.sha256()
    files: list[Path] = []
    for path in root.rglob("*"):
        metadata = path.lstat()
        if _metadata_is_link_or_reparse(metadata):
            raise ValueError("desktop app contains a link")
        if stat.S_ISREG(metadata.st_mode):
            files.append(path)
        elif not stat.S_ISDIR(metadata.st_mode):
            raise ValueError("desktop app contains a special file")
    files.sort()
    if not files:
        raise ValueError("desktop app is empty")
    for path in files:
        relative = path.relative_to(root).as_posix().encode()
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        digest.update(bytes.fromhex(sha256_file(path)))
    return digest.hexdigest()


def _shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
        return True
    except (OSError, ValueError):
        return False


def _lexists(path: Path) -> bool:
    return os.path.lexists(path)


def _metadata_is_link_or_reparse(metadata: os.stat_result) -> bool:
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return stat.S_ISLNK(metadata.st_mode) or bool(
        getattr(metadata, "st_file_attributes", 0) & reparse
    )


def _is_link_or_reparse(path: Path) -> bool:
    try:
        return _metadata_is_link_or_reparse(path.lstat())
    except OSError:
        return True


def _require_real_directory(path: Path, label: str) -> None:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise BootstrapCompanionError(f"{label} is unavailable") from error
    if (
        _metadata_is_link_or_reparse(metadata)
        or not stat.S_ISDIR(metadata.st_mode)
    ):
        raise BootstrapCompanionError(f"{label} is unsafe")
