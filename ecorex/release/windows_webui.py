"""Bootstrap-compatible Windows WebUI package projection from a signed Candidate."""

from __future__ import annotations

import base64
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import tempfile
from typing import Any, Mapping
import zipfile

from ecorex import __version__
from ecorex.product_version import stable_release_sequence
from ecorex.server.manifest import WebBundleManifest
from ecorex.update import (
    Ed25519SignatureVerifier,
    ReleaseManifest,
    SignatureEnvelope,
    verify_artifact_file,
    verify_manifest_signature,
)

from .candidate import candidate_receipt_signing_payload


WINDOWS_ARTIFACT_ID = "webui-windows-x64"
WINDOWS_FILE_NAME = f"EcoreX_{__version__}-{WINDOWS_ARTIFACT_ID}.zip"
WINDOWS_RECEIPT_SCHEMA = "emate.windows-webui-build-receipt.v1"
_BOOTSTRAP_ID = "bootstrap-windows-x64"
_CORE_ID = "core-windows-x64"
_WEB_MANIFEST_ID = "web-manifest"
_BOOTSTRAP_MEMBERS = frozenset(
    {
        "bootstrap-config.json",
        "EcoreX Installer.cmd",
        "bin/ecorex-bootstrap.exe",
        "bin/ecorex-sandbox-host.exe",
    }
)
_MAX_JSON = 4 * 1024 * 1024
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_WEBUI_URL = "http://127.0.0.1:8765/"
_INSTALLER = (
    b"@echo off\r\n"
    b'"%~dp0bin\\ecorex-bootstrap.exe" --local-release '
    b'"%~dp0signed\\release"\r\n'
    b"exit /b %errorlevel%\r\n"
)
_README = (
    "Double-click Install EcoreX WebUI.cmd. The signed e-Mate Bootstrap "
    "will verify and install the current Runtime slot, serve the bundled "
    "React WebUI, and open it in the default browser. This package does "
    "not contain Electron or a native desktop UI.\n"
).encode()


class WindowsWebUIBuildError(ValueError):
    pass


def build_windows_webui_package(
    *,
    release_dir: Path,
    candidate_receipt_path: Path,
    output_dir: Path,
    trusted_public_keys: Mapping[str, bytes],
    generated_at: str,
    production: bool,
    production_key_ids: frozenset[str] = frozenset(),
) -> tuple[Path, Path]:
    """Project one authenticated Candidate Bootstrap into the legacy ZIP shape."""

    _aware_timestamp(generated_at)
    release_root = _real_directory(release_dir, "signed Candidate release")
    output_root = output_dir.resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    manifest_path = _real_file(release_root / "release-manifest.json", "manifest")
    manifest_payload = manifest_path.read_bytes()
    manifest = ReleaseManifest.from_json(manifest_payload)
    if manifest.version != __version__ or manifest.channel.value != "stable":
        raise WindowsWebUIBuildError("Candidate is not the stable product version")
    verifier = Ed25519SignatureVerifier(dict(trusted_public_keys))
    verify_manifest_signature(manifest, verifier)
    if production and manifest.signature.key_id not in production_key_ids:
        raise WindowsWebUIBuildError(
            "Candidate signing key is not admitted for production"
        )
    artifacts: dict[str, Path] = {}
    for artifact in manifest.artifacts:
        path = _real_file(release_root / artifact.file_name, "Candidate artifact")
        verify_artifact_file(path, manifest, artifact, verifier)
        artifacts[artifact.artifact_id] = path
    bootstrap = manifest.artifact(_BOOTSTRAP_ID)
    core = manifest.artifact(_CORE_ID)
    web_manifest_artifact = manifest.artifact(_WEB_MANIFEST_ID)
    if bootstrap.platform != "windows" or bootstrap.architecture != "x64":
        raise WindowsWebUIBuildError("Candidate Windows Bootstrap target is invalid")
    if core.platform != "windows" or core.architecture != "x64":
        raise WindowsWebUIBuildError("Candidate Windows Runtime target is invalid")
    if (
        web_manifest_artifact.platform != "all"
        or web_manifest_artifact.architecture != "all"
    ):
        raise WindowsWebUIBuildError("Candidate Web manifest target is invalid")

    candidate_path = _real_file(candidate_receipt_path, "Candidate receipt")
    candidate_payload = candidate_path.read_bytes()
    candidate = _json_object(candidate_payload)
    _verify_candidate_receipt(
        candidate,
        candidate_payload=candidate_payload,
        manifest=manifest,
        manifest_payload=manifest_payload,
        verifier=verifier,
    )
    web_contract = _verify_webui_contract(
        artifacts[_CORE_ID],
        artifacts[_WEB_MANIFEST_ID],
        manifest=manifest,
        verifier=verifier,
    )

    with tempfile.TemporaryDirectory(
        prefix="emate-windows-webui-", dir=output_root
    ) as temporary:
        package_root = Path(temporary)
        _extract_bootstrap(
            artifacts[_BOOTSTRAP_ID],
            package_root,
            manifest=manifest,
            trusted_public_keys=trusted_public_keys,
        )
        installer = package_root / "EcoreX Installer.cmd"
        installer.replace(package_root / "Install EcoreX WebUI.cmd")
        (package_root / "Install EcoreX WebUI.cmd").write_bytes(_INSTALLER)
        signed = package_root / "signed"
        release = signed / "release"
        release.mkdir(parents=True)
        shutil.copyfile(manifest_path, release / "release-manifest.json")
        included_artifacts = tuple(
            item
            for item in manifest.artifacts
            if (item.platform, item.architecture) == ("windows", "x64")
            or item.artifact_id == _WEB_MANIFEST_ID
        )
        for item in included_artifacts:
            shutil.copyfile(artifacts[item.artifact_id], release / item.file_name)
        shutil.copyfile(candidate_path, signed / "candidate-build-receipt.json")
        release_json = _release_contract(manifest, web_contract, production)
        (package_root / "release.json").write_bytes(_canonical_json(release_json))
        (package_root / "README.txt").write_bytes(_README)
        package_path = output_root / WINDOWS_FILE_NAME
        _deterministic_zip(package_root, package_path)

    verified = verify_windows_webui_package(
        package_path,
        trusted_public_keys=trusted_public_keys,
        production=production,
        production_key_ids=production_key_ids,
    )

    package_size, package_sha = _file_identity(package_path)
    receipt_path = output_root / "emate-webui-build-receipt.json"
    provenance = {
        "release_id": manifest.release_id,
        "build_digest": manifest.build_digest,
        "manifest_sha256": hashlib.sha256(manifest_payload).hexdigest(),
        "candidate_receipt_sha256": hashlib.sha256(candidate_payload).hexdigest(),
        "bootstrap_artifact_id": bootstrap.artifact_id,
        "bootstrap_sha256": bootstrap.sha256,
        "core_artifact_id": core.artifact_id,
        "core_sha256": core.sha256,
        "web_manifest_sha256": web_manifest_artifact.sha256,
        "web_bundle_sha256": verified["web_bundle_sha256"],
        "included_artifact_ids": [item.artifact_id for item in included_artifacts],
        "signing_key_id": manifest.signature.key_id,
        "mode": "production" if production else "non-production-fixture",
    }
    receipt = {
        "schema": WINDOWS_RECEIPT_SCHEMA,
        "version": __version__,
        "status": "partial" if production else "non-production",
        "generated_at": generated_at,
        "production_eligible": production,
        "artifacts": [
            {
                "id": WINDOWS_ARTIFACT_ID,
                "file_name": WINDOWS_FILE_NAME,
                "size_bytes": package_size,
                "sha256": package_sha,
                "provenance": provenance,
            }
        ],
    }
    _atomic_replace(receipt_path, _canonical_json(receipt))
    return package_path, receipt_path


def verify_windows_webui_package(
    package_path: Path,
    *,
    trusted_public_keys: Mapping[str, bytes],
    production: bool,
    production_key_ids: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    """Reopen and authenticate the complete legacy-shaped Windows WebUI ZIP."""

    package = _real_file(package_path, "Windows WebUI package")
    if package.name != WINDOWS_FILE_NAME:
        raise WindowsWebUIBuildError("Windows WebUI package name is invalid")
    try:
        archive = zipfile.ZipFile(package)
    except (OSError, zipfile.BadZipFile) as exc:
        raise WindowsWebUIBuildError("Windows WebUI package is invalid") from exc
    with archive:
        members: dict[str, zipfile.ZipInfo] = {}
        for member in archive.infolist():
            name = member.filename
            path = PurePosixPath(name)
            mode = member.external_attr >> 16
            if (
                member.is_dir()
                or "\\" in name
                or path.is_absolute()
                or path.as_posix() != name
                or any(part in {"", ".", ".."} for part in path.parts)
                or name in members
                or stat.S_ISLNK(mode)
                or (mode and not stat.S_ISREG(mode))
            ):
                raise WindowsWebUIBuildError("Windows WebUI package layout is invalid")
            members[name] = member
        manifest_name = "signed/release/release-manifest.json"
        if manifest_name not in members or members[manifest_name].file_size > _MAX_JSON:
            raise WindowsWebUIBuildError("Windows WebUI package manifest is invalid")
        manifest_payload = archive.read(manifest_name)
        try:
            manifest = ReleaseManifest.from_json(manifest_payload)
            verifier = Ed25519SignatureVerifier(dict(trusted_public_keys))
            verify_manifest_signature(manifest, verifier)
        except Exception as exc:
            raise WindowsWebUIBuildError("Windows WebUI package manifest is invalid") from exc
        if manifest.version != __version__ or manifest.channel.value != "stable":
            raise WindowsWebUIBuildError("Windows WebUI package release is invalid")
        if production and manifest.signature.key_id not in production_key_ids:
            raise WindowsWebUIBuildError(
                "Windows WebUI package signing key is not admitted for production"
            )
        included = tuple(
            item
            for item in manifest.artifacts
            if (item.platform, item.architecture) == ("windows", "x64")
            or item.artifact_id == _WEB_MANIFEST_ID
        )
        for item in included:
            if PurePosixPath(item.file_name).parent != PurePosixPath("."):
                raise WindowsWebUIBuildError("Windows WebUI artifact name is invalid")
        release_names = {
            f"signed/release/{item.file_name}" for item in included
        } | {manifest_name}
        expected = release_names | {
            "bootstrap-config.json",
            "Install EcoreX WebUI.cmd",
            "bin/ecorex-bootstrap.exe",
            "bin/ecorex-sandbox-host.exe",
            "signed/candidate-build-receipt.json",
            "release.json",
            "README.txt",
        }
        if set(members) != expected:
            raise WindowsWebUIBuildError("Windows WebUI package inventory is invalid")
        if (
            members["Install EcoreX WebUI.cmd"].file_size != len(_INSTALLER)
            or members["README.txt"].file_size != len(_README)
            or members["release.json"].file_size > _MAX_JSON
            or members["signed/candidate-build-receipt.json"].file_size > _MAX_JSON
            or any(
                members[name].file_size > manifest.artifact(_BOOTSTRAP_ID).size_bytes
                for name in _BOOTSTRAP_MEMBERS - {"EcoreX Installer.cmd"}
            )
        ):
            raise WindowsWebUIBuildError("Windows WebUI package launcher is invalid")
        for item in included:
            if members[f"signed/release/{item.file_name}"].file_size != item.size_bytes:
                raise WindowsWebUIBuildError("Windows WebUI artifact size is invalid")
        try:
            corrupt = archive.testzip()
        except (OSError, EOFError, RuntimeError, zipfile.BadZipFile) as exc:
            raise WindowsWebUIBuildError("Windows WebUI package is corrupt") from exc
        if corrupt is not None:
            raise WindowsWebUIBuildError("Windows WebUI package is corrupt")
        if (
            archive.read("Install EcoreX WebUI.cmd") != _INSTALLER
            or archive.read("README.txt") != _README
        ):
            raise WindowsWebUIBuildError("Windows WebUI package launcher is invalid")
        with tempfile.TemporaryDirectory(prefix="emate-windows-verify-") as temporary:
            root = Path(temporary)
            for name in release_names:
                target = root.joinpath(*PurePosixPath(name).parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(members[name]) as source, target.open("wb") as output:
                    shutil.copyfileobj(source, output)
            artifacts: dict[str, Path] = {}
            for item in included:
                info = members[f"signed/release/{item.file_name}"]
                path = root / "signed" / "release" / item.file_name
                try:
                    verify_artifact_file(path, manifest, item, verifier)
                except Exception as exc:
                    raise WindowsWebUIBuildError(
                        "Windows WebUI artifact signature is invalid"
                    ) from exc
                artifacts[item.artifact_id] = path
            for required in (_BOOTSTRAP_ID, _CORE_ID, _WEB_MANIFEST_ID):
                if required not in artifacts:
                    raise WindowsWebUIBuildError("Windows WebUI artifacts are incomplete")
            bootstrap_root = root / "bootstrap"
            _extract_bootstrap(
                artifacts[_BOOTSTRAP_ID],
                bootstrap_root,
                manifest=manifest,
                trusted_public_keys=trusted_public_keys,
            )
            for name in _BOOTSTRAP_MEMBERS - {"EcoreX Installer.cmd"}:
                if archive.read(name) != (bootstrap_root / name).read_bytes():
                    raise WindowsWebUIBuildError(
                        "Windows WebUI Bootstrap projection is invalid"
                    )
            candidate_payload = archive.read("signed/candidate-build-receipt.json")
            _verify_candidate_receipt(
                _json_object(candidate_payload),
                candidate_payload=candidate_payload,
                manifest=manifest,
                manifest_payload=manifest_payload,
                verifier=verifier,
            )
            web_contract = _verify_webui_contract(
                artifacts[_CORE_ID],
                artifacts[_WEB_MANIFEST_ID],
                manifest=manifest,
                verifier=verifier,
            )
        if _json_object(archive.read("release.json")) != _release_contract(
            manifest, web_contract, production
        ):
            raise WindowsWebUIBuildError("Windows WebUI release contract is invalid")
    return {
        "release_id": manifest.release_id,
        "build_digest": manifest.build_digest,
        "manifest_sha256": hashlib.sha256(manifest_payload).hexdigest(),
        "candidate_receipt_sha256": hashlib.sha256(candidate_payload).hexdigest(),
        "signing_key_id": manifest.signature.key_id,
        "web_bundle_sha256": web_contract["bundle_sha256"],
        "included_artifact_ids": [item.artifact_id for item in included],
    }


def _release_contract(
    manifest: ReleaseManifest, web_contract: Mapping[str, str], production: bool
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "product": "e-Mate",
        "version": __version__,
        "artifact_id": WINDOWS_ARTIFACT_ID,
        "platform": "windows-x64",
        "install_entry": "Install EcoreX WebUI.cmd",
        "launch_authority": "signed-bootstrap-slot",
        "ui_kind": "react-webui",
        "desktop_shell": "browser",
        "native_desktop_ui": False,
        "browser_launch_url": _WEBUI_URL,
        "runtime_artifact_id": _CORE_ID,
        "web_manifest_artifact_id": _WEB_MANIFEST_ID,
        "web_bundle_sha256": web_contract["bundle_sha256"],
        "web_entrypoint": web_contract["entrypoint"],
        "runtime_web_root": web_contract["runtime_web_root"],
        "runtime_web_manifest": web_contract["runtime_web_manifest"],
        "signed_release_path": "signed/release/release-manifest.json",
        "release_id": manifest.release_id,
        "build_digest": manifest.build_digest,
        "production_eligible": production,
    }


def _verify_candidate_receipt(
    value: Mapping[str, Any],
    *,
    candidate_payload: bytes,
    manifest: ReleaseManifest,
    manifest_payload: bytes,
    verifier: Ed25519SignatureVerifier,
) -> None:
    signature_raw = value.get("signature")
    artifacts = value.get("artifacts")
    staging = value.get("staging_provenance")
    if (
        value.get("schema_version") != 2
        or value.get("receipt_type") != "ecorex-candidate-build"
        or value.get("status") != "passed"
        or value.get("version") != manifest.version
        or value.get("release_id") != manifest.release_id
        or value.get("channel") != manifest.channel.value
        or value.get("build_digest") != manifest.build_digest
        or value.get("manifest_sha256") != hashlib.sha256(manifest_payload).hexdigest()
        or not isinstance(value.get("commit_sha"), str)
        or _COMMIT.fullmatch(value["commit_sha"]) is None
        or not isinstance(value.get("web_tree_sha256"), str)
        or _SHA256.fullmatch(value["web_tree_sha256"]) is None
        or not isinstance(staging, Mapping)
        or staging.get("workflow_path")
        != ".github/workflows/ecorex-v1-platform-stage.yml"
        or isinstance(staging.get("workflow_run_id"), bool)
        or not isinstance(staging.get("workflow_run_id"), int)
        or staging["workflow_run_id"] < 1
        or isinstance(staging.get("run_attempt"), bool)
        or not isinstance(staging.get("run_attempt"), int)
        or staging["run_attempt"] < 1
        or not isinstance(staging.get("receipt_sha256"), str)
        or _SHA256.fullmatch(staging["receipt_sha256"]) is None
        or not isinstance(artifacts, Mapping)
        or not isinstance(signature_raw, Mapping)
    ):
        raise WindowsWebUIBuildError("Candidate receipt identity is invalid")
    expected = {
        artifact.artifact_id: {
            "file_name": artifact.file_name,
            "size_bytes": artifact.size_bytes,
            "sha256": artifact.sha256,
        }
        for artifact in manifest.artifacts
    }
    if artifacts != expected:
        raise WindowsWebUIBuildError("Candidate receipt artifact set is invalid")
    signature = SignatureEnvelope.from_dict(signature_raw)
    if signature.key_id != manifest.signature.key_id or not verifier.verify(
        candidate_receipt_signing_payload(value), signature
    ):
        raise WindowsWebUIBuildError("Candidate receipt signature is invalid")


def _extract_bootstrap(
    archive_path: Path,
    destination: Path,
    *,
    manifest: ReleaseManifest,
    trusted_public_keys: Mapping[str, bytes],
) -> None:
    try:
        archive = zipfile.ZipFile(archive_path)
    except (OSError, zipfile.BadZipFile) as exc:
        raise WindowsWebUIBuildError("signed Bootstrap archive is invalid") from exc
    observed: set[str] = set()
    with archive:
        for member in archive.infolist():
            name = member.filename.replace("\\", "/").rstrip("/")
            if member.is_dir() and name == "bin":
                continue
            if (
                name not in _BOOTSTRAP_MEMBERS
                or name in observed
                or PurePosixPath(name).is_absolute()
                or ".." in PurePosixPath(name).parts
                or stat.S_ISLNK(member.external_attr >> 16)
            ):
                raise WindowsWebUIBuildError("signed Bootstrap layout is invalid")
            observed.add(name)
            target = destination.joinpath(*PurePosixPath(name).parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(archive.read(member))
    if observed != set(_BOOTSTRAP_MEMBERS):
        raise WindowsWebUIBuildError("signed Bootstrap layout is incomplete")
    key = trusted_public_keys.get(manifest.signature.key_id)
    if key is None:
        raise WindowsWebUIBuildError("signed Bootstrap trust binding is invalid")
    config = _json_object((destination / "bootstrap-config.json").read_bytes())
    minimum = config.get("minimum_stable")
    encoded_keys = config.get("release_public_keys")
    if (
        not isinstance(encoded_keys, Mapping)
        or not isinstance(minimum, Mapping)
        or minimum.get("version") != manifest.version
        or minimum.get("sequence") != stable_release_sequence(manifest.version)
        or encoded_keys.get(manifest.signature.key_id)
        != base64.b64encode(key).decode("ascii")
    ):
        raise WindowsWebUIBuildError("signed Bootstrap trust binding is invalid")


def _verify_webui_contract(
    core_path: Path,
    web_manifest_path: Path,
    *,
    manifest: ReleaseManifest,
    verifier: Ed25519SignatureVerifier,
    expected_platform: str = "windows",
    expected_architecture: str = "x64",
) -> dict[str, str]:
    """Bind the browser WebUI contract to bytes inside one signed Core."""

    try:
        web_manifest = WebBundleManifest.from_json(web_manifest_path.read_bytes())
    except Exception as exc:
        raise WindowsWebUIBuildError("Candidate Web manifest is invalid") from exc
    if (
        web_manifest.release_id != manifest.release_id
        or web_manifest.version != manifest.version
        or web_manifest.build_digest != manifest.build_digest
        or web_manifest.signature.key_id != manifest.signature.key_id
        or not verifier.verify(web_manifest.canonical_payload(), web_manifest.signature)
    ):
        raise WindowsWebUIBuildError("Candidate Web manifest identity is invalid")
    try:
        archive = zipfile.ZipFile(core_path)
    except (OSError, zipfile.BadZipFile) as exc:
        raise WindowsWebUIBuildError(
            "signed Windows Runtime archive is invalid"
        ) from exc
    with archive:
        names = [
            item.filename.replace("\\", "/").rstrip("/")
            for item in archive.infolist()
            if not item.is_dir()
        ]
        if len(names) != len(set(names)) or "runtime-config.json" not in names:
            raise WindowsWebUIBuildError("signed Windows Runtime layout is invalid")
        config = _json_object(archive.read("runtime-config.json"))
        identity = config.get("identity")
        paths = config.get("paths")
        if (
            not isinstance(identity, Mapping)
            or identity.get("version") != manifest.version
            or identity.get("platform") != expected_platform
            or identity.get("architecture") != expected_architecture
            or not isinstance(paths, Mapping)
        ):
            raise WindowsWebUIBuildError("signed Windows Runtime identity is invalid")
        web_root = _safe_archive_directory(paths.get("web_root"))
        web_manifest_relative = _safe_archive_file(paths.get("web_manifest"))
        if (
            web_manifest_relative not in names
            or archive.read(web_manifest_relative) != web_manifest_path.read_bytes()
        ):
            raise WindowsWebUIBuildError(
                "signed Windows Runtime Web manifest binding is invalid"
            )
        expected: set[str] = set()
        for record in web_manifest.files:
            relative = f"{web_root}/{record.path}"
            expected.add(relative)
            if relative not in names:
                raise WindowsWebUIBuildError(
                    "signed Windows Runtime Web bundle is incomplete"
                )
            payload = archive.read(relative)
            if (
                len(payload) != record.size_bytes
                or hashlib.sha256(payload).hexdigest() != record.sha256
            ):
                raise WindowsWebUIBuildError(
                    "signed Windows Runtime Web file identity is invalid"
                )
        actual = {name for name in names if name.startswith(f"{web_root}/")}
        if actual != expected:
            raise WindowsWebUIBuildError(
                "signed Windows Runtime Web bundle inventory is invalid"
            )
    return {
        "bundle_sha256": web_manifest.bundle_sha256,
        "entrypoint": web_manifest.entrypoint,
        "runtime_web_root": web_root,
        "runtime_web_manifest": web_manifest_relative,
    }


def _safe_archive_directory(value: Any) -> str:
    relative = _safe_archive_path(value)
    if relative.endswith("/"):
        relative = relative.rstrip("/")
    return relative


def _safe_archive_file(value: Any) -> str:
    return _safe_archive_path(value)


def _safe_archive_path(value: Any) -> str:
    if not isinstance(value, str) or "\\" in value:
        raise WindowsWebUIBuildError("signed Windows Runtime Web path is invalid")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or path.as_posix() != value
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise WindowsWebUIBuildError("signed Windows Runtime Web path is invalid")
    return value


def _deterministic_zip(source: Path, destination: Path) -> None:
    temporary = destination.with_name(f".{destination.name}.tmp-{os.getpid()}")
    try:
        with zipfile.ZipFile(
            temporary, "w", zipfile.ZIP_DEFLATED, compresslevel=9
        ) as archive:
            for path in sorted(
                source.rglob("*"), key=lambda item: item.relative_to(source).as_posix()
            ):
                if not path.is_file() or path.is_symlink():
                    continue
                relative = path.relative_to(source).as_posix()
                info = zipfile.ZipInfo(relative, (1980, 1, 1, 0, 0, 0))
                info.create_system = 3
                mode = 0o755 if relative.endswith((".exe", ".cmd")) else 0o644
                info.external_attr = (stat.S_IFREG | mode) << 16
                info.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(info, path.read_bytes(), compresslevel=9)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def _real_directory(path: Path, label: str) -> Path:
    try:
        value = path.resolve(strict=True)
        metadata = path.lstat()
    except OSError:
        raise WindowsWebUIBuildError(f"{label} is unavailable") from None
    if path.is_symlink() or not stat.S_ISDIR(metadata.st_mode):
        raise WindowsWebUIBuildError(f"{label} is unsafe")
    return value


def _real_file(path: Path, label: str) -> Path:
    try:
        value = path.resolve(strict=True)
        metadata = path.lstat()
    except OSError:
        raise WindowsWebUIBuildError(f"{label} is unavailable") from None
    if path.is_symlink() or not stat.S_ISREG(metadata.st_mode):
        raise WindowsWebUIBuildError(f"{label} is unsafe")
    return value


def _json_object(payload: bytes) -> Mapping[str, Any]:
    if not 1 <= len(payload) <= _MAX_JSON:
        raise WindowsWebUIBuildError("JSON contract is invalid")
    try:
        value = json.loads(payload, object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise WindowsWebUIBuildError("JSON contract is invalid") from None
    if not isinstance(value, Mapping):
        raise WindowsWebUIBuildError("JSON contract is invalid")
    return value


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise WindowsWebUIBuildError("JSON contract contains duplicate keys")
        result[key] = value
    return result


def _canonical_json(value: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def _atomic_replace(path: Path, payload: bytes) -> None:
    descriptor, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def _file_identity(path: Path) -> tuple[int, str]:
    payload = path.read_bytes()
    return len(payload), hashlib.sha256(payload).hexdigest()


def _aware_timestamp(value: str) -> None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        raise WindowsWebUIBuildError("generated_at is invalid") from None
    if parsed.tzinfo is None:
        raise WindowsWebUIBuildError("generated_at is invalid")
