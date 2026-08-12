"""Isolated process adapters for signed dependency-only Capability Packs.

Dependency packs carry native Python modules that must never be imported into
Core.  This adapter verifies and expands the already signed archive into a
private snapshot, revalidates that snapshot before every invocation, and runs
the service through the signed relocatable Pack Python interpreter.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import stat
import tempfile
import threading
from typing import Any, Mapping
import zipfile

from ecorex.capabilities import VerifiedCapabilityPack
from ecorex.integration import dependency_pack_worker
from ecorex.integration.pack_python import PackPythonIdentity

_MAX_ARCHIVE_BYTES = 1024 * 1024 * 1024
_MAX_FILES = 50_000
_MAX_REQUEST_BYTES = 12 * 1024 * 1024
_MAX_RESPONSE_BYTES = 8 * 1024 * 1024
_MAX_WORKER_BYTES = 256 * 1024
OFFICE_READ_JOB_MEMORY_LIMIT_BYTES = (
    dependency_pack_worker.OFFICE_READ_JOB_MEMORY_LIMIT_BYTES
)
OFFICE_READ_PROCESS_MEMORY_LIMIT_BYTES = (
    dependency_pack_worker.OFFICE_READ_PROCESS_MEMORY_LIMIT_BYTES
)


class DependencyPackProcessError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class VerifiedDependencyPackProcessAdapter:
    """Own one non-global, content-bound extraction and process boundary."""

    def __init__(
        self,
        pack: VerifiedCapabilityPack,
        *,
        python_executable: Path,
        python_identity: PackPythonIdentity,
    ) -> None:
        if pack.manifest.pack_id not in {"ocr", "office"}:
            raise DependencyPackProcessError("dependency_pack_service_unsupported")
        self.pack = pack
        self.python_executable = Path(python_executable)
        self.python_identity = python_identity
        self._lock = threading.RLock()
        self._temporary = tempfile.TemporaryDirectory(
            prefix=f"ecorex-{pack.manifest.pack_id}-service-"
        )
        self._root = Path(self._temporary.name) / "pack"
        self._worker = Path(self._temporary.name) / "dependency_pack_worker.py"
        # Dependency packs can contain thousands of native-runtime files
        # (OCR currently carries more than four thousand).  Expanding and
        # hashing them while the HTTP application is being composed keeps the
        # loopback listener closed long enough for Bootstrap to report a false
        # startup failure.  Keep composition side-effect-light and materialize
        # the already-signed pack only when its service is first invoked.  The
        # invocation remains serialized by ``_lock`` and the full snapshot and
        # source artifact are still verified before every execution.
        self._expected_files: Mapping[str, tuple[int, str]] | None = None

    def close(self) -> None:
        self._temporary.cleanup()

    async def aclose(self) -> None:
        self.close()

    def invoke(
        self,
        operation: str,
        payload: Mapping[str, Any],
        *,
        timeout_seconds: float,
    ) -> Mapping[str, Any]:
        # Import lazily: ecorex.release's public facade also imports server
        # composition, which depends on this adapter during module loading.
        from ecorex.release.process_boundary import (
            BoundedProcessError,
            run_bounded_process,
        )

        if not isinstance(operation, str) or not operation or len(operation) > 64:
            raise DependencyPackProcessError("dependency_pack_operation_invalid")
        if not 0.5 <= float(timeout_seconds) <= 30:
            raise DependencyPackProcessError("dependency_pack_timeout_invalid")
        request = json.dumps(
            {
                "schema_version": 1,
                "pack_id": self.pack.manifest.pack_id,
                "operation": operation,
                "payload": dict(payload),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(request) > _MAX_REQUEST_BYTES:
            raise DependencyPackProcessError("dependency_pack_request_too_large")
        with self._lock:
            if self._expected_files is None:
                # _extract_verified_snapshot verifies the signed archive and
                # the extracted snapshot before publishing the in-process
                # authority used by later invocations.
                self._expected_files = self._extract_verified_snapshot()
            else:
                self._verify_snapshot()
                self._verify_artifact()
            # A cold isolated Pack-Python process must import signed native
            # modules before the operation-specific timeout can begin. Keep
            # that startup bounded without turning a healthy first call into
            # a false timeout on slower Windows disks or antivirus scans.
            process_timeout = min(30.0, max(15.0, float(timeout_seconds) + 8.0))
            office_read = self.pack.manifest.pack_id == "office" and operation in {
                "read",
                "edit",
            }
            worker = self._verified_worker()
            command = (
                str(self.python_executable),
                "-I",
                "-B",
                str(worker),
                str(self._root),
            )
            if office_read:
                command += (
                    "--office-read-memory-limit",
                    str(OFFICE_READ_PROCESS_MEMORY_LIMIT_BYTES),
                )
            try:
                result = run_bounded_process(
                    command,
                    payload=request,
                    cwd=self._root,
                    environment=_runtime_environment(),
                    timeout_seconds=process_timeout,
                    max_stdout_bytes=_MAX_RESPONSE_BYTES,
                    max_stderr_bytes=64 * 1024,
                    **(
                        {
                            "windows_process_memory_limit_bytes": (
                                OFFICE_READ_PROCESS_MEMORY_LIMIT_BYTES
                            ),
                            "windows_job_memory_limit_bytes": (
                                OFFICE_READ_JOB_MEMORY_LIMIT_BYTES
                            ),
                        }
                        if os.name == "nt" and office_read
                        else {}
                    ),
                )
            except (BoundedProcessError, OSError, ValueError):
                raise DependencyPackProcessError(
                    "dependency_pack_process_failed"
                ) from None
        if result.returncode != 0:
            raise DependencyPackProcessError("dependency_pack_process_rejected")
        try:
            value = json.loads(result.stdout.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, RecursionError):
            raise DependencyPackProcessError(
                "dependency_pack_response_invalid"
            ) from None
        if (
            not isinstance(value, Mapping)
            or value.get("schema_version") != 1
            or value.get("pack_id") != self.pack.manifest.pack_id
            or value.get("status") != "success"
            or not isinstance(value.get("result"), Mapping)
        ):
            raise DependencyPackProcessError("dependency_pack_response_invalid")
        return dict(value["result"])

    def _verified_worker(self) -> Path:
        loader = getattr(dependency_pack_worker, "__loader__", None)
        source = getattr(dependency_pack_worker, "__file__", None)
        read = getattr(loader, "get_data", None)
        try:
            payload = read(source) if callable(read) and isinstance(source, str) else b""
        except (OSError, TypeError):
            payload = b""
        if not isinstance(payload, bytes) or not 1 <= len(payload) <= _MAX_WORKER_BYTES:
            raise DependencyPackProcessError("dependency_pack_worker_unavailable")
        try:
            if not self._worker.exists():
                descriptor = os.open(
                    self._worker,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                    0o600,
                )
                with os.fdopen(descriptor, "wb") as output:
                    output.write(payload)
                    output.flush()
                    os.fsync(output.fileno())
            metadata = self._worker.lstat()
            if (
                stat.S_ISLNK(metadata.st_mode)
                or not stat.S_ISREG(metadata.st_mode)
                or (os.name != "nt" and metadata.st_mode & 0o077)
                or self._worker.read_bytes() != payload
            ):
                raise OSError
        except OSError:
            raise DependencyPackProcessError(
                "dependency_pack_worker_unavailable"
            ) from None
        return self._worker

    def _verify_artifact(self) -> None:
        path = self.pack.artifact_path
        try:
            before = path.lstat()
            if (
                stat.S_ISLNK(before.st_mode)
                or not stat.S_ISREG(before.st_mode)
                or before.st_size != self.pack.manifest.artifact_size_bytes
            ):
                raise OSError
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                opened = os.fstat(stream.fileno())
                while chunk := stream.read(1024 * 1024):
                    digest.update(chunk)
                after = os.fstat(stream.fileno())
            current = path.lstat()
        except OSError:
            raise DependencyPackProcessError(
                "dependency_pack_artifact_changed"
            ) from None
        identity = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        if (
            (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)
            != identity
            or (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
            != identity
            or (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns)
            != identity
            or digest.hexdigest() != self.pack.manifest.artifact_sha256
        ):
            raise DependencyPackProcessError("dependency_pack_artifact_changed")

    def _extract_verified_snapshot(self) -> Mapping[str, tuple[int, str]]:
        self._verify_artifact()
        self._root.mkdir()
        expected: dict[str, tuple[int, str]] = {}
        try:
            with zipfile.ZipFile(self.pack.artifact_path) as archive:
                members = archive.infolist()
                if not 1 <= len(members) <= _MAX_FILES:
                    raise DependencyPackProcessError("dependency_pack_archive_invalid")
                total = 0
                seen: set[str] = set()
                inventory_payload: bytes | None = None
                payload_records: list[dict[str, Any]] = []
                for member in members:
                    name = member.filename
                    relative = PurePosixPath(name)
                    mode = member.external_attr >> 16
                    file_type = stat.S_IFMT(mode)
                    if (
                        not name
                        or "\\" in name
                        or relative.is_absolute()
                        or any(
                            part in {"", ".", ".."} or ":" in part
                            for part in relative.parts
                        )
                        or name.casefold() in seen
                        or member.flag_bits & 0x1
                        or stat.S_ISLNK(mode)
                        or file_type not in {0, stat.S_IFREG, stat.S_IFDIR}
                    ):
                        raise DependencyPackProcessError(
                            "dependency_pack_archive_invalid"
                        )
                    seen.add(name.casefold())
                    if member.is_dir():
                        continue
                    total += member.file_size
                    if total > _MAX_ARCHIVE_BYTES:
                        raise DependencyPackProcessError(
                            "dependency_pack_archive_invalid"
                        )
                    content = archive.read(member)
                    if len(content) != member.file_size:
                        raise DependencyPackProcessError(
                            "dependency_pack_archive_invalid"
                        )
                    digest = hashlib.sha256(content).hexdigest()
                    target = self._root.joinpath(*relative.parts)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(content)
                    expected[name] = (len(content), digest)
                    if name == "runtime-inventory.json":
                        inventory_payload = content
                    else:
                        payload_records.append(
                            {
                                "path": name,
                                "size_bytes": len(content),
                                "sha256": digest,
                                "mode": 0o755 if mode & stat.S_IXUSR else 0o644,
                            }
                        )
        except DependencyPackProcessError:
            raise
        except (OSError, zipfile.BadZipFile, RuntimeError):
            raise DependencyPackProcessError(
                "dependency_pack_archive_invalid"
            ) from None
        try:
            inventory = json.loads((inventory_payload or b"").decode("utf-8"))
            descriptor = json.loads(
                (self._root / "ecorex-dependency-pack.json").read_text(encoding="utf-8")
            )
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
            raise DependencyPackProcessError(
                "dependency_pack_inventory_invalid"
            ) from None
        expected_service = (
            "ocr.extract" if self.pack.manifest.pack_id == "ocr" else "office.formats"
        )
        expected_adapter = (
            "python-rapidocr-runtime-v1"
            if self.pack.manifest.pack_id == "ocr"
            else "python-office-formats-v1"
        )
        binding = hashlib.sha256(
            json.dumps(
                sorted(payload_records, key=lambda item: item["path"].casefold()),
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        if (
            not isinstance(inventory, Mapping)
            or inventory.get("schema_version") != 1
            or inventory.get("pack_id") != self.pack.manifest.pack_id
            or inventory.get("payload_sha256") != binding
            or descriptor
            != {
                "schema_version": 1,
                "kind": "dependency-service",
                "pack_id": self.pack.manifest.pack_id,
                "adapter": expected_adapter,
                "runtime_api_version": "1.0.0",
                "inventory": "runtime-inventory.json",
                "services": [expected_service],
            }
            or not (self._root / "runtime" / "python").is_dir()
        ):
            raise DependencyPackProcessError("dependency_pack_inventory_invalid")
        self._verify_snapshot(expected)
        return expected

    def _verify_snapshot(
        self, expected: Mapping[str, tuple[int, str]] | None = None
    ) -> None:
        expected = self._expected_files if expected is None else expected
        if expected is None:
            raise DependencyPackProcessError("dependency_pack_snapshot_unavailable")
        observed: dict[str, tuple[int, str]] = {}
        for path in self._root.rglob("*"):
            metadata = path.lstat()
            if stat.S_ISLNK(metadata.st_mode) or bool(
                getattr(metadata, "st_file_attributes", 0)
                & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
            ):
                raise DependencyPackProcessError("dependency_pack_snapshot_changed")
            if path.is_dir():
                continue
            relative = path.relative_to(self._root).as_posix()
            payload = path.read_bytes()
            observed[relative] = (len(payload), hashlib.sha256(payload).hexdigest())
        if observed != dict(expected):
            raise DependencyPackProcessError("dependency_pack_snapshot_changed")


class PackOCRServiceAdapter:
    service_id = "ocr.extract"
    contract_version = "1.0.0"

    def __init__(self, process: VerifiedDependencyPackProcessAdapter) -> None:
        self.process = process

    def extract(self, content: bytes, *, timeout_seconds: float) -> Mapping[str, Any]:
        if not isinstance(content, bytes) or not content:
            raise ValueError("OCR image content is empty")
        return self.process.invoke(
            "extract",
            {"content_base64": base64.b64encode(content).decode("ascii")},
            # The user bound covers inference. A cold isolated worker also
            # has to load signed ONNX native libraries and model weights; keep
            # that startup bounded without making the default 2s request fail
            # before inference can begin.
            timeout_seconds=min(30.0, max(15.0, float(timeout_seconds) + 8.0)),
        )

    async def aclose(self) -> None:
        await self.process.aclose()


class PackOfficeServiceAdapter:
    service_id = "office.formats"
    contract_version = "1.0.0"

    def __init__(self, process: VerifiedDependencyPackProcessAdapter) -> None:
        self.process = process

    def probe(self, *, timeout_seconds: float = 30.0) -> Mapping[str, Any]:
        return self.process.invoke(
            "probe",
            {},
            timeout_seconds=min(30.0, max(1.0, float(timeout_seconds))),
        )

    def create(
        self,
        family: str,
        payload: Mapping[str, Any],
        *,
        timeout_seconds: float = 30.0,
    ) -> Mapping[str, Any]:
        if family not in {"document", "spreadsheet", "presentation", "pdf"}:
            raise ValueError("Office family is unsupported")
        return self.process.invoke(
            "create",
            {**dict(payload), "family": family},
            timeout_seconds=min(30.0, max(1.0, float(timeout_seconds))),
        )

    def read(
        self,
        family: str,
        content: bytes,
        *,
        timeout_seconds: float = 30.0,
    ) -> Mapping[str, Any]:
        if family not in {"document", "spreadsheet", "presentation", "pdf"}:
            raise ValueError("Office family is unsupported")
        if not isinstance(content, bytes) or not 1 <= len(content) <= 5 * 1024 * 1024:
            raise ValueError("Office content is invalid")
        return self.process.invoke(
            "read",
            {
                "family": family,
                "content_base64": base64.b64encode(content).decode("ascii"),
            },
            timeout_seconds=min(30.0, max(1.0, float(timeout_seconds))),
        )

    def edit(
        self,
        family: str,
        content: bytes,
        payload: Mapping[str, Any],
        *,
        timeout_seconds: float = 30.0,
    ) -> Mapping[str, Any]:
        if family not in {"document", "spreadsheet", "presentation", "pdf"}:
            raise ValueError("Office family is unsupported")
        if not isinstance(content, bytes) or not 1 <= len(content) <= 5 * 1024 * 1024:
            raise ValueError("Office content is invalid")
        return self.process.invoke(
            "edit",
            {
                **dict(payload),
                "family": family,
                "content_base64": base64.b64encode(content).decode("ascii"),
            },
            timeout_seconds=min(30.0, max(1.0, float(timeout_seconds))),
        )

    async def aclose(self) -> None:
        await self.process.aclose()


def _runtime_environment() -> Mapping[str, str]:
    allowed = {"LANG", "LC_ALL", "SYSTEMDRIVE", "SYSTEMROOT", "TEMP", "TMP", "WINDIR"}
    result = {
        key.upper(): value
        for key, value in os.environ.items()
        if key.upper() in allowed and isinstance(value, str)
    }
    result.update(
        {
            "PYTHONHASHSEED": "0",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
            "PYTHONTZPATH": "",
            "PYTHONUTF8": "1",
        }
    )
    return result


__all__ = [
    "DependencyPackProcessError",
    "PackOCRServiceAdapter",
    "PackOfficeServiceAdapter",
    "VerifiedDependencyPackProcessAdapter",
]
