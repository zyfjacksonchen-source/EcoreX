"""Pinned-root, streaming and exclusive filesystem publication primitives."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import stat
from typing import Any
import uuid

from ecorex.artifacts.identity import sanitize_display_filename

from .errors import (
    OutputIntegrityError,
    OutputLocationUnavailable,
    OutputMaterializationFailed,
    OutputRootChanged,
    OutputRootUnsafe,
    OutputValidationError,
)
from .models import OutputLocationAlias
from .repository import StoredMaterialization, StoredPolicy, canonical_json


@dataclass(frozen=True, slots=True)
class DirectoryLease:
    root: Path
    directory_fd: int | None = None
    windows_handle: int | None = None


@dataclass(frozen=True, slots=True)
class CasSource:
    expected_sha256: str
    expected_size: int
    path: Path | None = None
    fallback_bytes: bytes | None = None


def _is_reparse(stat_result: os.stat_result) -> bool:
    attributes = int(getattr(stat_result, "st_file_attributes", 0))
    reparse_flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    return bool(attributes & reparse_flag) or bool(getattr(stat_result, "st_reparse_tag", 0))


class SafeOutputFilesystem:
    """Owns all host-path knowledge; transport projections never reach here."""

    def __init__(
        self,
        configured_roots: Mapping[OutputLocationAlias | str, str | Path],
        *,
        prepare_roots: bool = True,
    ) -> None:
        # Keep POSIX directory descriptors open for the Runtime lifetime. An
        # unlinked directory whose inode is pinned cannot be recycled for a
        # replacement path, closing the inode-reuse gap between policy capture
        # and publication. Windows acquires an exclusive directory handle per
        # publication in ``_lease_policy_root`` instead.
        self._pinned_roots: dict[Path, int] = {}
        roots: dict[OutputLocationAlias, Path] = {}
        for raw_alias, raw_root in configured_roots.items():
            try:
                alias = OutputLocationAlias(raw_alias)
            except ValueError as error:
                raise OutputValidationError("configured output alias is unsupported") from error
            if alias in roots:
                raise OutputValidationError("configured output alias is duplicated")
            roots[alias] = (
                self._prepare_configured_root(raw_root)
                if prepare_roots
                else self._configured_root_path(raw_root)
            )
        self._roots = roots

    def close(self) -> None:
        descriptors = tuple(self._pinned_roots.values())
        self._pinned_roots.clear()
        for descriptor in descriptors:
            try:
                os.close(descriptor)
            except OSError:
                pass

    def __del__(self) -> None:
        self.close()

    def is_configured(self, alias: OutputLocationAlias) -> bool:
        return alias in self._roots

    def inspect_configured_root(
        self, alias: OutputLocationAlias
    ) -> tuple[Path, int, int, str]:
        root = self._roots.get(alias)
        if root is None:
            raise OutputLocationUnavailable("the selected output location is unavailable")
        return self._inspect_root(root)

    def project_root_fingerprint(self, alias: OutputLocationAlias) -> str:
        """Return a deterministic read-only identity for a configured root.

        Projection-only startup must not create a missing output directory just
        to render settings.  Existing roots still receive the full device/inode
        verification; only an absent configured root uses a virtual identity
        that can never be persisted as an execution policy.
        """

        root = self._roots.get(alias)
        if root is None:
            raise OutputLocationUnavailable("the selected output location is unavailable")
        try:
            root.lstat()
        except FileNotFoundError:
            return hashlib.sha256(
                canonical_json(
                    {
                        "path": os.path.normcase(str(root)),
                        "state": "unprepared",
                    }
                ).encode("utf-8")
            ).hexdigest()
        except OSError as error:
            raise OutputLocationUnavailable(
                "the selected output location is unavailable"
            ) from error
        return self._inspect_root(root)[3]

    def configured_root_digest(self, alias: OutputLocationAlias) -> str:
        """Return a path-opaque identity without touching the filesystem."""

        root = self._roots.get(alias)
        if root is None:
            raise OutputLocationUnavailable("the selected output location is unavailable")
        return hashlib.sha256(
            canonical_json(
                {"configured_path": os.path.normcase(str(root))}
            ).encode("utf-8")
        ).hexdigest()

    @staticmethod
    def cas_source(blobs: Any, sha256: str, size_bytes: int) -> CasSource:
        path_for = getattr(blobs, "path_for", None)
        if callable(path_for):
            try:
                path = Path(path_for(sha256))
            except (OSError, TypeError, ValueError) as error:
                raise OutputIntegrityError(
                    "the authoritative artifact content is unavailable"
                ) from error
            return CasSource(
                expected_sha256=sha256,
                expected_size=size_bytes,
                path=path,
            )
        # Product ArtifactService always has a CAS path.  The byte fallback is
        # intentionally limited to trusted in-process adapters and test doubles.
        try:
            content = bytes(blobs.read_bytes(sha256, verify=True))
        except Exception as error:
            raise OutputIntegrityError(
                "the authoritative artifact content is unavailable"
            ) from error
        return CasSource(
            expected_sha256=sha256,
            expected_size=size_bytes,
            fallback_bytes=content,
        )

    def publish(
        self,
        stored: StoredMaterialization,
        policy: StoredPolicy,
        source: CasSource,
        *,
        fault_hook: Callable[[str, str], None],
        collision_handler: Callable[[StoredMaterialization, str], StoredMaterialization],
    ) -> bool:
        """Return true when an already-identical destination was reused."""

        while True:
            self._validate_policy_root(policy)
            fault_hook("after_root_validation", stored.projection.materialization_id)
            self._validate_policy_root(policy)
            with self._lease_policy_root(policy) as lease:
                destination_name = self._destination_name(stored.projection.display_name)
                existing = self._existing_digest_in(lease, destination_name)
                if existing is not None:
                    observed_sha, observed_size = existing
                    if (
                        observed_sha == stored.projection.sha256
                        and observed_size == stored.projection.size_bytes
                    ):
                        self._stream_cas(source)
                        return True
                    stored = collision_handler(stored, observed_sha)
                    continue

                temporary_name = (
                    ".ecorex-output-"
                    + stored.projection.materialization_id[4:20]
                    + "-"
                    + uuid.uuid4().hex
                    + ".tmp"
                )
                temp_identity: tuple[int, int] | None = None
                descriptor: int | None = None
                try:
                    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
                    descriptor = self._open_in_lease(
                        lease, temporary_name, flags, mode=0o600
                    )
                    temporary_stat = os.fstat(descriptor)
                    temp_identity = (
                        int(temporary_stat.st_dev),
                        int(temporary_stat.st_ino),
                    )
                    with os.fdopen(descriptor, "wb", closefd=True) as handle:
                        descriptor = None
                        self._stream_cas(source, destination=handle)
                        handle.flush()
                        os.fsync(handle.fileno())
                    fault_hook("after_temp_fsync", stored.projection.materialization_id)
                    self._validate_lease(policy, lease)
                    try:
                        self._link_in_lease(lease, temporary_name, destination_name)
                    except OSError as error:
                        raced = self._existing_digest_in(lease, destination_name)
                        if raced is None:
                            raise OutputMaterializationFailed(
                                "the output file could not be published atomically"
                            ) from error
                        observed_sha, observed_size = raced
                        if (
                            observed_sha == stored.projection.sha256
                            and observed_size == stored.projection.size_bytes
                        ):
                            return True
                        stored = collision_handler(stored, observed_sha)
                        continue
                    self._fsync_lease(lease)
                    self._validate_lease(policy, lease)
                    published = self._existing_digest_in(lease, destination_name)
                    expected = (
                        stored.projection.sha256,
                        stored.projection.size_bytes,
                    )
                    if published != expected:
                        raise OutputIntegrityError("the published output failed verification")
                    fault_hook("after_publish", stored.projection.materialization_id)
                    return False
                except (
                    OutputIntegrityError,
                    OutputMaterializationFailed,
                    OutputRootChanged,
                    OutputRootUnsafe,
                ):
                    raise
                except OSError as error:
                    raise OutputMaterializationFailed(
                        "the output file could not be written"
                    ) from error
                finally:
                    if descriptor is not None:
                        os.close(descriptor)
                    self._safe_unlink_temporary_in(
                        lease, temporary_name, temp_identity
                    )

    def verify_completed(
        self,
        stored: StoredMaterialization,
        policy: StoredPolicy,
        source: CasSource,
    ) -> None:
        self._stream_cas(source)
        with self._lease_policy_root(policy) as lease:
            existing = self._existing_digest_in(
                lease, self._destination_name(stored.projection.display_name)
            )
        expected = (stored.projection.sha256, stored.projection.size_bytes)
        if existing != expected:
            raise OutputIntegrityError("the completed output file failed verification")

    @staticmethod
    def _stream_cas(source: CasSource, *, destination: Any | None = None) -> None:
        digest = hashlib.sha256()
        size = 0
        if source.path is None:
            content = source.fallback_bytes or b""
            if destination is not None:
                destination.write(content)
            digest.update(content)
            size = len(content)
        else:
            try:
                before = os.lstat(source.path)
                if (
                    not stat.S_ISREG(before.st_mode)
                    or stat.S_ISLNK(before.st_mode)
                    or _is_reparse(before)
                ):
                    raise OutputIntegrityError(
                        "the authoritative artifact content is not a regular CAS blob"
                    )
                flags = (
                    os.O_RDONLY
                    | getattr(os, "O_BINARY", 0)
                    | getattr(os, "O_NOFOLLOW", 0)
                )
                descriptor = os.open(source.path, flags)
                with os.fdopen(descriptor, "rb", closefd=True) as handle:
                    opened = os.fstat(handle.fileno())
                    if not stat.S_ISREG(opened.st_mode):
                        raise OutputIntegrityError(
                            "the authoritative artifact content is not a regular CAS blob"
                        )
                    while True:
                        chunk = handle.read(1024 * 1024)
                        if not chunk:
                            break
                        if destination is not None:
                            destination.write(chunk)
                        digest.update(chunk)
                        size += len(chunk)
                after = os.lstat(source.path)
            except OutputIntegrityError:
                raise
            except OSError as error:
                raise OutputIntegrityError(
                    "the authoritative artifact content cannot be read"
                ) from error
            identities = {
                (int(before.st_dev), int(before.st_ino)),
                (int(opened.st_dev), int(opened.st_ino)),
                (int(after.st_dev), int(after.st_ino)),
            }
            if len(identities) != 1 or stat.S_ISLNK(after.st_mode) or _is_reparse(after):
                raise OutputIntegrityError(
                    "the authoritative artifact content changed during verification"
                )
        if size != source.expected_size or digest.hexdigest() != source.expected_sha256:
            raise OutputIntegrityError(
                "the authoritative artifact content failed verification"
            )

    @staticmethod
    def _safe_unlink_temporary_in(
        lease: DirectoryLease,
        name: str,
        identity: tuple[int, int] | None,
    ) -> None:
        if identity is None:
            return
        try:
            if lease.directory_fd is not None:
                value = os.stat(
                    name,
                    dir_fd=lease.directory_fd,
                    follow_symlinks=False,
                )
            else:
                value = os.lstat(lease.root / name)
            if (
                stat.S_ISREG(value.st_mode)
                and not stat.S_ISLNK(value.st_mode)
                and (int(value.st_dev), int(value.st_ino)) == identity
            ):
                if lease.directory_fd is not None:
                    os.unlink(name, dir_fd=lease.directory_fd)
                else:
                    (lease.root / name).unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass  # Never unlink through a root whose identity is uncertain.

    @staticmethod
    def _fsync_lease(lease: DirectoryLease) -> None:
        if lease.directory_fd is not None:
            os.fsync(lease.directory_fd)
            return
        if os.name == "nt":
            return
        descriptor = os.open(
            lease.root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        )
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @staticmethod
    def _destination_name(display_name: str) -> str:
        safe = sanitize_display_filename(display_name, max_length=180)
        if safe != display_name or safe in {".", ".."} or any(
            separator in safe for separator in ("/", "\\")
        ):
            raise OutputRootUnsafe("the artifact output name is unsafe")
        return safe

    @staticmethod
    def _existing_digest_in(
        lease: DirectoryLease, name: str
    ) -> tuple[str, int] | None:
        try:
            if lease.directory_fd is not None:
                before = os.stat(
                    name,
                    dir_fd=lease.directory_fd,
                    follow_symlinks=False,
                )
            else:
                before = os.lstat(lease.root / name)
        except FileNotFoundError:
            return None
        except OSError as error:
            raise OutputMaterializationFailed(
                "the existing output cannot be inspected"
            ) from error
        if stat.S_ISLNK(before.st_mode) or _is_reparse(before) or not stat.S_ISREG(before.st_mode):
            raise OutputRootUnsafe("the output name is occupied by an unsafe filesystem entry")
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = SafeOutputFilesystem._open_in_lease(lease, name, flags)
            with os.fdopen(descriptor, "rb", closefd=True) as handle:
                opened = os.fstat(handle.fileno())
                if not stat.S_ISREG(opened.st_mode):
                    raise OutputRootUnsafe("the output name is not a regular file")
                digest = hashlib.sha256()
                size = 0
                while True:
                    chunk = handle.read(1024 * 1024)
                    if not chunk:
                        break
                    digest.update(chunk)
                    size += len(chunk)
        except OutputRootUnsafe:
            raise
        except OSError as error:
            raise OutputMaterializationFailed(
                "the existing output cannot be read"
            ) from error
        try:
            if lease.directory_fd is not None:
                after = os.stat(
                    name,
                    dir_fd=lease.directory_fd,
                    follow_symlinks=False,
                )
            else:
                after = os.lstat(lease.root / name)
        except OSError as error:
            raise OutputRootChanged("the output changed during verification") from error
        identities = {
            (int(before.st_dev), int(before.st_ino)),
            (int(opened.st_dev), int(opened.st_ino)),
            (int(after.st_dev), int(after.st_ino)),
        }
        if len(identities) != 1:
            raise OutputRootChanged("the output changed during verification")
        if stat.S_ISLNK(after.st_mode) or _is_reparse(after):
            raise OutputRootUnsafe("the output changed into an unsafe entry")
        return digest.hexdigest(), size

    @staticmethod
    def _open_in_lease(
        lease: DirectoryLease, name: str, flags: int, *, mode: int = 0o666
    ) -> int:
        if lease.directory_fd is not None:
            return os.open(name, flags, mode, dir_fd=lease.directory_fd)
        return os.open(lease.root / name, flags, mode)

    @staticmethod
    def _link_in_lease(
        lease: DirectoryLease, source_name: str, destination_name: str
    ) -> None:
        if lease.directory_fd is not None:
            os.link(
                source_name,
                destination_name,
                src_dir_fd=lease.directory_fd,
                dst_dir_fd=lease.directory_fd,
                follow_symlinks=False,
            )
            return
        os.link(lease.root / source_name, lease.root / destination_name)

    @contextmanager
    def _lease_policy_root(self, policy: StoredPolicy) -> Iterator[DirectoryLease]:
        root = self._validate_policy_root(policy)
        if os.name != "nt":
            flags = (
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0)
            )
            try:
                descriptor = os.open(root, flags)
            except OSError as error:
                raise OutputRootChanged(
                    "the frozen output location cannot be pinned"
                ) from error
            try:
                opened = os.fstat(descriptor)
                if (
                    not stat.S_ISDIR(opened.st_mode)
                    or int(opened.st_dev) != policy.root_device
                    or int(opened.st_ino) != policy.root_inode
                ):
                    raise OutputRootChanged("the frozen output location was replaced")
                self._validate_policy_root(policy)
                yield DirectoryLease(root=root, directory_fd=descriptor)
            finally:
                os.close(descriptor)
            return

        try:
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            create_file = kernel32.CreateFileW
            create_file.argtypes = (
                wintypes.LPCWSTR,
                wintypes.DWORD,
                wintypes.DWORD,
                wintypes.LPVOID,
                wintypes.DWORD,
                wintypes.DWORD,
                wintypes.HANDLE,
            )
            create_file.restype = wintypes.HANDLE
            handle = create_file(
                str(root),
                0,
                0x00000001 | 0x00000002,
                None,
                3,
                0x02000000 | 0x00200000,
                None,
            )
            invalid = ctypes.c_void_p(-1).value
            if handle in (None, 0, invalid):
                raise OSError(ctypes.get_last_error(), "directory handle unavailable")
            close_handle = kernel32.CloseHandle
            close_handle.argtypes = (wintypes.HANDLE,)
            close_handle.restype = wintypes.BOOL
        except OSError as error:
            raise OutputRootChanged(
                "the frozen output location cannot be pinned"
            ) from error
        try:
            self._validate_policy_root(policy)
            yield DirectoryLease(root=root, windows_handle=int(handle))
        finally:
            close_handle(handle)

    def _validate_lease(self, policy: StoredPolicy, lease: DirectoryLease) -> None:
        if lease.directory_fd is not None:
            opened = os.fstat(lease.directory_fd)
            if (
                not stat.S_ISDIR(opened.st_mode)
                or int(opened.st_dev) != policy.root_device
                or int(opened.st_ino) != policy.root_inode
            ):
                raise OutputRootChanged("the frozen output location was replaced")
        self._validate_policy_root(policy)

    def _prepare_configured_root(self, raw_root: str | Path) -> Path:
        root = self._configured_root_path(raw_root)
        try:
            root.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise OutputLocationUnavailable(
                "a configured output location cannot be prepared"
            ) from error
        self._inspect_root(root)
        return root

    @staticmethod
    def _configured_root_path(raw_root: str | Path) -> Path:
        if isinstance(raw_root, bytes) or not str(raw_root or "").strip():
            raise OutputValidationError("configured output root is invalid")
        root = Path(raw_root).expanduser()
        if not root.is_absolute():
            raise OutputValidationError("configured output roots must be absolute")
        return Path(os.path.abspath(root))

    def _inspect_root(self, root: Path) -> tuple[Path, int, int, str]:
        try:
            value = os.lstat(root)
            resolved = root.resolve(strict=True)
        except OSError as error:
            raise OutputLocationUnavailable(
                "the selected output location is unavailable"
            ) from error
        if not stat.S_ISDIR(value.st_mode) or stat.S_ISLNK(value.st_mode) or _is_reparse(value):
            raise OutputRootUnsafe("the selected output location is not a safe directory")
        if os.path.normcase(str(resolved)) != os.path.normcase(str(root)):
            raise OutputRootUnsafe(
                "the selected output location contains a symbolic redirect"
            )
        if os.name != "nt":
            pinned = self._pinned_root_stat(root)
            if (int(value.st_dev), int(value.st_ino)) != (
                int(pinned.st_dev),
                int(pinned.st_ino),
            ):
                raise OutputRootChanged("the frozen output location was replaced")
        device, inode = int(value.st_dev), int(value.st_ino)
        fingerprint = hashlib.sha256(
            canonical_json(
                {
                    "path": os.path.normcase(str(root)),
                    "device": device,
                    "inode": inode,
                }
            ).encode("utf-8")
        ).hexdigest()
        return root, device, inode, fingerprint

    def _pinned_root_stat(self, root: Path) -> os.stat_result:
        descriptor = self._pinned_roots.get(root)
        if descriptor is None:
            flags = (
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0)
            )
            try:
                before = os.lstat(root)
                descriptor = os.open(root, flags)
                opened = os.fstat(descriptor)
                after = os.lstat(root)
            except OSError as error:
                if descriptor is not None:
                    os.close(descriptor)
                raise OutputLocationUnavailable(
                    "the selected output location cannot be pinned"
                ) from error
            identities = {
                (int(before.st_dev), int(before.st_ino), int(before.st_ctime_ns)),
                (int(opened.st_dev), int(opened.st_ino), int(opened.st_ctime_ns)),
                (int(after.st_dev), int(after.st_ino), int(after.st_ctime_ns)),
            }
            if (
                len(identities) != 1
                or not stat.S_ISDIR(opened.st_mode)
                or stat.S_ISLNK(after.st_mode)
                or _is_reparse(after)
            ):
                os.close(descriptor)
                raise OutputRootChanged("the output location changed while being pinned")
            self._pinned_roots[root] = descriptor
        try:
            return os.fstat(descriptor)
        except OSError as error:
            raise OutputRootChanged("the pinned output location is unavailable") from error

    def _validate_policy_root(self, policy: StoredPolicy) -> Path:
        inspected = self._inspect_root(Path(policy.root_path))
        if (
            inspected[1] != policy.root_device
            or inspected[2] != policy.root_inode
            or inspected[3] != policy.root_fingerprint
        ):
            raise OutputRootChanged("the frozen output location was replaced")
        return inspected[0]


__all__ = ["CasSource", "SafeOutputFilesystem"]
