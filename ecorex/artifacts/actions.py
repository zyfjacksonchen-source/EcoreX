"""Backend-authoritative open/reveal execution for public office artifacts.

The browser never supplies a path.  A verified immutable CAS revision is
materialized under the Runtime-owned exports directory, then a platform
launcher receives that server-selected target without invoking a shell.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import stat
import sys
import tempfile
import threading
from typing import Callable, Protocol
from urllib.parse import urlsplit

from .errors import (
    ArtifactActionOutcomeUnknown,
    ArtifactActionUnavailable,
    ArtifactError,
    ArtifactExportFailed,
    ArtifactLaunchFailed,
)
from .identity import sanitize_display_filename
from .models import (
    ArtifactAction,
    ArtifactExternalActionReceipt,
    ArtifactExternalActionStatus,
    ArtifactFamily,
    ArtifactProjection,
    ArtifactScope,
)
from .service import ArtifactService


@dataclass(frozen=True, slots=True)
class ArtifactLaunchTarget:
    kind: str
    value: str = field(repr=False)

    def __post_init__(self) -> None:
        if self.kind not in {"file", "uri"}:
            raise ValueError("artifact launch target must be a file or uri")
        if not str(self.value or ""):
            raise ValueError("artifact launch target must not be empty")


class ArtifactLauncher(Protocol):
    def validate(self, action: ArtifactAction, target: ArtifactLaunchTarget) -> None:
        ...

    def launch(self, action: ArtifactAction, target: ArtifactLaunchTarget) -> None:
        ...


class SystemArtifactLauncher:
    """Windows/macOS launcher using system APIs or fixed executable argv."""

    def __init__(
        self,
        *,
        platform: str | None = None,
        command_runner: Callable[..., object] = subprocess.run,
        startfile: Callable[[str], object] | None = None,
    ) -> None:
        self.platform = str(platform or sys.platform).casefold()
        self._run = command_runner
        self._startfile = startfile or getattr(os, "startfile", None)

    @property
    def _windows(self) -> bool:
        return self.platform.startswith("win")

    @property
    def _macos(self) -> bool:
        return self.platform == "darwin"

    def validate(self, action: ArtifactAction, target: ArtifactLaunchTarget) -> None:
        action = ArtifactAction(action)
        if action not in {ArtifactAction.OPEN, ArtifactAction.REVEAL}:
            raise ArtifactActionUnavailable("unsupported external artifact action")
        if target.kind == "uri" and action is not ArtifactAction.OPEN:
            raise ArtifactActionUnavailable("a cloud link cannot be revealed as a local file")
        if self._windows:
            if action is ArtifactAction.OPEN and self._startfile is None:
                raise ArtifactActionUnavailable("Windows artifact launcher is unavailable")
            return
        if self._macos:
            return
        raise ArtifactActionUnavailable(
            "artifact open and reveal are supported only on Windows and macOS"
        )

    def launch(self, action: ArtifactAction, target: ArtifactLaunchTarget) -> None:
        self.validate(action, target)
        try:
            if self._windows:
                if action is ArtifactAction.OPEN:
                    assert self._startfile is not None
                    self._startfile(target.value)
                    return
                self._run(
                    ["explorer.exe", "/select,", target.value],
                    check=True,
                    shell=False,
                    timeout=15,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    close_fds=True,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                return
            command = ["/usr/bin/open"]
            if action is ArtifactAction.REVEAL:
                command.append("-R")
            command.append(target.value)
            self._run(
                command,
                check=True,
                shell=False,
                timeout=15,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise ArtifactLaunchFailed(
                "the operating system did not accept the artifact action"
            ) from error


class ArtifactExportMaterializer:
    """Atomically materialize verified CAS bytes under one fixed directory."""

    def __init__(
        self,
        service: ArtifactService,
        exports_root: str | Path | None = None,
        *,
        create_storage: bool = True,
    ) -> None:
        self.service = service
        service_root = service.root.resolve()
        self._service_root = service_root
        self.root = Path(exports_root or service_root / "exports").resolve()
        try:
            self.root.relative_to(service_root)
        except ValueError as error:
            raise ValueError("artifact exports directory must be inside the artifact root") from error
        if create_storage:
            self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    @staticmethod
    def _is_reparse(metadata: os.stat_result) -> bool:
        attributes = int(getattr(metadata, "st_file_attributes", 0))
        reparse_flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
        return bool(attributes & reparse_flag)

    def _verify_export_root(self) -> None:
        try:
            metadata = self.root.lstat()
            resolved = self.root.resolve(strict=True)
        except OSError as error:
            raise ArtifactExportFailed("artifact exports directory is unavailable") from error
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or self._is_reparse(metadata)
            or resolved != self.root
        ):
            raise ArtifactExportFailed("artifact exports directory is not trusted")
        try:
            resolved.relative_to(self._service_root)
        except ValueError as error:
            raise ArtifactExportFailed("artifact exports directory escaped its authority") from error

    def materialize(
        self,
        projection: ArtifactProjection,
        *,
        account_id: str,
    ) -> tuple[Path, bytes]:
        self._verify_export_root()
        safe_name = sanitize_display_filename(projection.display_name)
        destination = self.root / safe_name
        if destination.parent != self.root or destination.name != safe_name:
            raise ArtifactExportFailed("artifact export filename is invalid")
        try:
            content = self.service.read_user_content(
                projection.artifact_id,
                projection.revision_id,
                account_id=account_id,
            )
        except ArtifactError:
            raise
        except Exception as error:
            raise ArtifactExportFailed("artifact content could not be verified") from error
        if hashlib.sha256(content).hexdigest() != projection.sha256:
            raise ArtifactExportFailed("artifact content failed export verification")

        with self._lock:
            self._verify_export_root()
            try:
                existing = destination.lstat()
            except FileNotFoundError:
                existing = None
            except OSError as error:
                raise ArtifactExportFailed("artifact export destination is unavailable") from error
            if (
                existing is not None
                and stat.S_ISDIR(existing.st_mode)
                and not stat.S_ISLNK(existing.st_mode)
                and not self._is_reparse(existing)
            ):
                raise ArtifactExportFailed("artifact export destination is unavailable")
            descriptor = -1
            temporary: Path | None = None
            try:
                descriptor, temporary_name = tempfile.mkstemp(
                    prefix=".ecorex-export-",
                    dir=self.root,
                )
                temporary = Path(temporary_name)
                with os.fdopen(descriptor, "wb") as handle:
                    descriptor = -1
                    handle.write(content)
                    handle.flush()
                    os.fsync(handle.fileno())
                    if os.fstat(handle.fileno()).st_size != len(content):
                        raise ArtifactExportFailed("artifact export write was incomplete")
                self._verify_export_root()
                os.replace(temporary, destination)
                temporary = None
                if os.name != "nt":
                    descriptor = os.open(
                        self.root,
                        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
                    )
                    try:
                        os.fsync(descriptor)
                    finally:
                        os.close(descriptor)
                        descriptor = -1
            except ArtifactError:
                raise
            except OSError as error:
                raise ArtifactExportFailed("artifact could not be prepared for this action") from error
            finally:
                if descriptor >= 0:
                    os.close(descriptor)
                if temporary is not None:
                    try:
                        temporary.unlink(missing_ok=True)
                    except OSError:
                        pass
        return destination, content


def _cloud_link_target(content: bytes) -> ArtifactLaunchTarget:
    try:
        payload = json.loads(content.decode("utf-8"))
        url = payload["url"] if isinstance(payload, dict) else None
        parsed = urlsplit(url if isinstance(url, str) else "")
    except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError) as error:
        raise ArtifactExportFailed("cloud-link artifact content is invalid") from error
    if (
        parsed.scheme.casefold() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ArtifactExportFailed("cloud-link artifact target is not allowed")
    return ArtifactLaunchTarget(kind="uri", value=url)


@dataclass(frozen=True, slots=True)
class PreparedArtifactAction:
    receipt: ArtifactExternalActionReceipt
    target: ArtifactLaunchTarget | None = field(repr=False)


class ArtifactActionExecutor:
    """Prepare, fence, and execute one at-most-once external action."""

    def __init__(
        self,
        service: ArtifactService,
        *,
        launcher: ArtifactLauncher | None = None,
        exports_root: str | Path | None = None,
        create_storage: bool = True,
    ) -> None:
        self.service = service
        self.launcher = launcher or SystemArtifactLauncher()
        self.materializer = ArtifactExportMaterializer(
            service,
            exports_root,
            create_storage=create_storage,
        )

    def prepare(
        self,
        artifact_id: str,
        action: ArtifactAction,
        client_request_id: str,
        *,
        account_id: str,
        on_prepared: Callable[
            [sqlite3.Connection, ArtifactExternalActionReceipt, ArtifactScope], None
        ]
        | None = None,
    ) -> PreparedArtifactAction:
        projection = self.service.get_user_artifact(artifact_id, account_id=account_id)
        receipt = self.service.repository.prepare_external_action(
            artifact_id,
            revision_id=projection.revision_id,
            action=action,
            client_request_id=client_request_id,
            account_id=account_id,
            now=self.service.clock(),
            on_prepared=on_prepared,
        )
        if receipt.status is ArtifactExternalActionStatus.COMPLETED:
            return PreparedArtifactAction(receipt=receipt, target=None)
        if receipt.status is ArtifactExternalActionStatus.LAUNCHING:
            raise ArtifactActionOutcomeUnknown(
                "a prior artifact action may already have reached the operating system; use a new request id to try again"
            )
        if receipt.status is ArtifactExternalActionStatus.FAILED:
            raise ArtifactLaunchFailed(
                "the prior artifact action failed; use a new request id to try again"
            )

        try:
            path, content = self.materializer.materialize(
                projection,
                account_id=account_id,
            )
            target = (
                _cloud_link_target(content)
                if projection.family is ArtifactFamily.CLOUD_LINK
                else ArtifactLaunchTarget(kind="file", value=str(path))
            )
            self.launcher.validate(action, target)
        except ArtifactError as error:
            self.service.repository.transition_external_action(
                receipt,
                expected=ArtifactExternalActionStatus.PREPARED,
                target=ArtifactExternalActionStatus.FAILED,
                now=self.service.clock(),
                failure_code=error.code,
            )
            raise
        return PreparedArtifactAction(receipt=receipt, target=target)

    def launch(self, prepared: PreparedArtifactAction) -> ArtifactExternalActionReceipt:
        receipt = prepared.receipt
        if receipt.status is ArtifactExternalActionStatus.COMPLETED:
            return receipt
        if prepared.target is None:
            raise ArtifactLaunchFailed("artifact launch target was not prepared")
        launching, claimed = self.service.repository.claim_external_action_launch(
            receipt,
            now=self.service.clock(),
        )
        if not claimed:
            if launching.status is ArtifactExternalActionStatus.COMPLETED:
                return launching
            raise ArtifactActionOutcomeUnknown(
                "the artifact action may already have reached the operating system; use a new request id to try again"
            )
        try:
            self.launcher.launch(receipt.action, prepared.target)
        except ArtifactError as error:
            self.service.repository.transition_external_action(
                launching,
                expected=ArtifactExternalActionStatus.LAUNCHING,
                target=ArtifactExternalActionStatus.FAILED,
                now=self.service.clock(),
                failure_code=error.code,
            )
            raise
        except Exception as error:
            self.service.repository.transition_external_action(
                launching,
                expected=ArtifactExternalActionStatus.LAUNCHING,
                target=ArtifactExternalActionStatus.FAILED,
                now=self.service.clock(),
                failure_code=ArtifactLaunchFailed.code,
            )
            raise ArtifactLaunchFailed(
                "the operating system did not accept the artifact action"
            ) from error
        completed = self.service.repository.transition_external_action(
            launching,
            expected=ArtifactExternalActionStatus.LAUNCHING,
            target=ArtifactExternalActionStatus.COMPLETED,
            now=self.service.clock(),
        )
        if completed.status is not ArtifactExternalActionStatus.COMPLETED:
            raise ArtifactActionOutcomeUnknown(
                "the artifact was opened but completion could not be confirmed"
            )
        return completed


__all__ = [
    "ArtifactActionExecutor",
    "ArtifactExportMaterializer",
    "ArtifactLauncher",
    "ArtifactLaunchTarget",
    "PreparedArtifactAction",
    "SystemArtifactLauncher",
]
