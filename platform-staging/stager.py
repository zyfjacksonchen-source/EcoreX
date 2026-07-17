#!/usr/bin/env python3
"""Build one real platform Core and the six signed Capability Pack trees.

The protected workflow invokes this file as the digest-pinned adapter behind
``invoke-v1-platform-stager.py``. It consumes one strict request on stdin and
never accepts a build command, Python module, dependency URL or output file
from that request. Missing native toolchains, Playwright/Chromium, a final Web
dist or a digest-pinned public Runtime configuration fail closed.
"""

from __future__ import annotations

import base64
from email.message import Message
import hashlib
from importlib import metadata as importlib_metadata
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import sys
import sysconfig
import tempfile
from typing import Any, Iterable, Mapping
from urllib.parse import urlsplit
import zipfile

from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ecorex import __version__  # noqa: E402
from ecorex.integration.pack_python import (  # noqa: E402
    PackPythonIdentity,
    build_pack_python_manifest,
    resolve_pack_python,
)
from ecorex.integration.sandbox import (  # noqa: E402
    MacOSSandboxExecBackend,
    probe_windows_appcontainer_helper,
)
from ecorex.release.models import WebBundleBuildInput  # noqa: E402
from ecorex.pack_catalog import (  # noqa: E402
    CAPABILITY_PACK_SERVICE_IDS as PACK_SERVICES,
    CAPABILITY_PACK_TOOL_IDS as PACK_TOOLS,
)
from ecorex.release.process_boundary import (  # noqa: E402
    BoundedProcessError,
    BoundedProcessResult,
    run_bounded_process,
)
from ecorex.release.dependency_lock import (  # noqa: E402
    DependencyLockError,
    load_dependency_lock_manifest,
)
from ecorex.release.web_bundle import scan_web_bundle  # noqa: E402
from ecorex.server.config import ProductRuntimeConfig  # noqa: E402


_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SAFE_CODE = re.compile(r"^[a-z][a-z0-9_]{2,127}$")
_SAFE_KEY_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_TARGETS = frozenset(
    {("windows", "x64"), ("macos", "arm64"), ("macos", "x64")}
)
_RUNTIME_DISTRIBUTIONS = (
    "cryptography",
    "fastapi",
    "httpx",
    "pydantic",
    "python-multipart",
    "tzdata",
    "uvicorn",
    "websockets",
)
_SECRET_PATTERNS = (
    re.compile(
        rb"-----BEGIN ((?:RSA |EC |OPENSSH )?PRIVATE KEY)-----\r?\n"
        rb"(?:[A-Za-z0-9+/=]{16,}\r?\n)+-----END \1-----"
    ),
    re.compile(rb"(?<![A-Za-z0-9+/=])AKIA[0-9A-Z]{16}(?![A-Za-z0-9+/=])"),
    re.compile(rb"(?<![A-Za-z0-9+/=])gh[pousr]_[A-Za-z0-9]{20,}(?![A-Za-z0-9+/=])"),
    re.compile(rb"(?<![A-Za-z0-9+/=])xox[baprs]-[A-Za-z0-9-]{10,}(?![A-Za-z0-9+/=])"),
)
_FIXED_TIME = (1980, 1, 1, 0, 0, 0)
_MAX_FILE_BYTES = 512 * 1024 * 1024
_IMPORT_ARCHIVE_NATIVE_SUFFIXES = frozenset(
    {".dll", ".dylib", ".exe", ".pyd", ".so"}
)
_IMPORT_ARCHIVE_PURE_SUFFIXES = frozenset({".py", ".pyi", ".typed"})
# These packages intentionally expose data through importlib.resources and are
# covered by the isolated post-compaction Runtime probe.  Other packages with
# non-Python data stay on disk so code that requires a real __file__ path keeps
# working.
_IMPORT_ARCHIVE_RESOURCE_PACKAGES = frozenset({"certifi", "ecorex", "tzdata"})
_DEPENDENCY_PACK_ADAPTERS = {
    "channels": "managed-channel-contracts-v1",
    "ocr": "python-rapidocr-runtime-v1",
    "office": "python-office-formats-v1",
}


class StageError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code if _SAFE_CODE.fullmatch(str(code)) else "platform_stage_failed"
        super().__init__(self.code)


def main() -> int:
    try:
        request = _request()
        _stage(request)
        sys.stdout.write('{"schema_version":1,"status":"passed"}')
        return 0
    except StageError as error:
        print(json.dumps({"code": error.code, "status": "failed"}, sort_keys=True), file=sys.stderr)
        return 1
    except BaseException:
        print('{"code":"platform_stage_failed","status":"failed"}', file=sys.stderr)
        return 1


def _request() -> Mapping[str, Any]:
    payload = sys.stdin.buffer.read(64 * 1024 + 1)
    if not 1 <= len(payload) <= 64 * 1024:
        raise StageError("platform_stage_request_invalid")
    try:
        value = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError):
        raise StageError("platform_stage_request_invalid") from None
    expected = {
        "schema_version",
        "operation",
        "repo_root",
        "output_root",
        "platform",
        "architecture",
        "commit_sha",
        "workflow_run_id",
        "workflow_run_attempt",
        "public_bootstrap_index_url",
        "publication_public_keys",
    }
    if (
        not isinstance(value, Mapping)
        or set(value) != expected
        or value.get("schema_version") != 1
        or value.get("operation") != "stage-ecorex-v1-candidate"
        or (value.get("platform"), value.get("architecture")) not in _TARGETS
        or not isinstance(value.get("commit_sha"), str)
        or _COMMIT.fullmatch(value["commit_sha"]) is None
        or isinstance(value.get("workflow_run_id"), bool)
        or not isinstance(value.get("workflow_run_id"), int)
        or value["workflow_run_id"] < 1
        or isinstance(value.get("workflow_run_attempt"), bool)
        or not isinstance(value.get("workflow_run_attempt"), int)
        or value["workflow_run_attempt"] < 1
    ):
        raise StageError("platform_stage_request_invalid")
    _validated_public_keyring(value.get("publication_public_keys"))
    index_url = value.get("public_bootstrap_index_url")
    parsed_index = urlsplit(index_url if isinstance(index_url, str) else "")
    if (
        parsed_index.scheme != "https"
        or not parsed_index.hostname
        or parsed_index.port not in {None, 443}
        or parsed_index.username
        or parsed_index.password
        or parsed_index.query
        or parsed_index.fragment
        or not parsed_index.path
    ):
        raise StageError("platform_stage_request_invalid")
    repository = _absolute_directory(value.get("repo_root"), "platform_stage_repository_invalid")
    output = _absolute_directory(value.get("output_root"), "platform_stage_output_invalid")
    if repository != ROOT or any(output.iterdir()):
        raise StageError("platform_stage_path_invalid")
    if _git_commit(repository) != value["commit_sha"]:
        raise StageError("platform_stage_commit_mismatch")
    host_platform, host_architecture = _host_target()
    if (host_platform, host_architecture) != (value["platform"], value["architecture"]):
        raise StageError("platform_stage_host_mismatch")
    return dict(value)


def _validated_public_keyring(value: Any) -> dict[str, str]:
    if not isinstance(value, Mapping) or not 1 <= len(value) <= 8:
        raise StageError("platform_stage_publication_trust_invalid")
    result: dict[str, str] = {}
    for key_id, encoded in value.items():
        if (
            not isinstance(key_id, str)
            or _SAFE_KEY_ID.fullmatch(key_id) is None
            or not isinstance(encoded, str)
        ):
            raise StageError("platform_stage_publication_trust_invalid")
        try:
            raw = base64.b64decode(encoded, validate=True)
        except (TypeError, ValueError):
            raise StageError("platform_stage_publication_trust_invalid") from None
        if len(raw) != 32 or base64.b64encode(raw).decode("ascii") != encoded:
            raise StageError("platform_stage_publication_trust_invalid")
        result[key_id] = encoded
    return dict(sorted(result.items()))


def _stage(request: Mapping[str, Any]) -> None:
    platform = str(request["platform"])
    architecture = str(request["architecture"])
    target = f"{platform}-{architecture}"
    output = Path(str(request["output_root"]))
    stages = output / "stages" / target
    core = stages / "core"
    bootstrap = stages / "bootstrap"
    packs = stages / "packs"
    evidence = output / ".evidence" / target
    core.mkdir(parents=True)
    bootstrap.mkdir(parents=True)
    packs.mkdir(parents=True)

    native = _build_native(platform, architecture, evidence / "native")
    distributions, interpreter, interpreter_identity = _build_python_closure(
        core, platform, architecture
    )
    _install_native(native, core, platform)
    config_digest = _write_runtime_config(core, platform, architecture)
    web = _scan_final_web()
    _stage_packs(
        packs,
        platform=platform,
        architecture=architecture,
        interpreter=interpreter,
        native=native,
        evidence=evidence,
    )
    _core_gates(
        core,
        platform=platform,
        architecture=architecture,
        interpreter=interpreter,
        interpreter_identity=interpreter_identity.to_dict(),
        distributions=distributions,
        config_digest=config_digest,
        web=web,
        evidence=evidence / "core",
    )
    _build_bootstrap(
        bootstrap,
        platform=platform,
        architecture=architecture,
        native=native,
        public_index_url=str(request["public_bootstrap_index_url"]),
        runtime_config=ProductRuntimeConfig.from_bytes(
            (core / "runtime-config.json").read_bytes()
        ),
        publication_public_keys=_validated_public_keyring(
            request["publication_public_keys"]
        ),
        evidence=evidence / "bootstrap",
    )


def _build_native(platform: str, architecture: str, evidence: Path) -> Path:
    evidence.mkdir(parents=True)
    output = evidence / "output"
    output.mkdir()
    environment = _build_environment()
    authority_source: Path | None = None
    try:
        if platform == "windows":
            blocked_environment = {
                "CL",
                "_CL_",
                "LINK",
                "_LINK_",
                "LIB",
                "LIBPATH",
                "INCLUDE",
                "CL_MPCOUNT",
                "USEENV",
                "LINK_REPRO",
                "LINK_FULLPATHRSP",
            }
            system_root = _windows_system_root()
            environment = {
                key: value
                for key, value in environment.items()
                if key.upper() not in blocked_environment
                and key.upper() != "PSMODULEPATH"
                and key.upper() not in {"SYSTEMROOT", "WINDIR"}
            }
            environment["SYSTEMROOT"] = str(system_root)
            environment["WINDIR"] = str(system_root)
            native_root = ROOT / "platform-staging" / "native" / "windows"
            source_names = (
                "ecorex_launcher.cpp",
                "ecorex_sandbox_host.cpp",
                "ecorex_sandbox_security.cpp",
                "ecorex_sandbox_process.cpp",
                "ecorex_sandbox_host_internal.h",
            )
            source_payloads = {
                name: _stable_bytes(
                    native_root / name,
                    4 * 1024 * 1024,
                    "windows_native_source_authority_invalid",
                )
                for name in source_names
            }
            manifest_payload = _stable_bytes(
                native_root / "toolchain-manifest.json",
                64 * 1024,
                "windows_native_toolchain_authority_invalid",
            )
            authority_source = evidence / "caller-pinned-native-authority"
            authority_source.mkdir(mode=0o700)
            for name, payload in (
                *source_payloads.items(),
                ("toolchain-manifest.json", manifest_payload),
            ):
                destination = authority_source / name
                with destination.open("xb") as stream:
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
                destination.chmod(0o600)
            source_binding = "\0".join(
                f"{name}={hashlib.sha256(source_payloads[name]).hexdigest()}"
                for name in sorted(source_payloads)
            ).encode("utf-8")
            expected_source_set = hashlib.sha256(source_binding).hexdigest()
            expected_toolchain_manifest = hashlib.sha256(
                manifest_payload
            ).hexdigest()
            toolchain_manifest = authority_source / "toolchain-manifest.json"
            powershell = (
                system_root
                / "System32"
                / "WindowsPowerShell"
                / "v1.0"
                / "powershell.exe"
            )
            if not powershell.is_file():
                raise StageError("windows_native_toolchain_unavailable")
            command = (
                str(powershell),
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(
                    ROOT
                    / "platform-staging"
                    / "native"
                    / "windows"
                    / "build.ps1"
                ),
                "-OutputDirectory",
                str(output),
                "-SourceDirectory",
                str(authority_source),
                "-ToolchainManifest",
                str(toolchain_manifest),
                "-ExpectedToolchainManifestSha256",
                expected_toolchain_manifest,
                "-ExpectedSourceSetSha256",
                expected_source_set,
            )
        else:
            shell = Path("/bin/sh")
            clang = Path("/usr/bin/clang")
            if not shell.is_file() or not clang.is_file():
                raise StageError("macos_native_toolchain_unavailable")
            command = (
                str(shell),
                str(ROOT / "platform-staging" / "native" / "macos" / "build.sh"),
                architecture,
                str(output),
            )
        _run(
            command,
            cwd=ROOT,
            environment=environment,
            timeout=600,
            code="native_build_failed",
        )
        required = (
            ("ecorex.exe", "ecorex-sandbox-host.exe", "native-build-receipt.json")
            if platform == "windows"
            else ("ecorex",)
        )
        if any(not (output / name).is_file() for name in required):
            raise StageError("native_build_output_missing")
        if platform == "windows":
            assert authority_source is not None
            _validate_windows_native_receipt(
                output,
                toolchain_manifest=toolchain_manifest,
                source_root=authority_source,
            )
        return output
    finally:
        if authority_source is not None and authority_source.exists():
            shutil.rmtree(authority_source)


def _validate_windows_native_receipt(
    output: Path,
    *,
    toolchain_manifest: Path,
    source_root: Path | None = None,
) -> None:
    code = "windows_native_build_receipt_invalid"
    try:
        manifest_payload = _stable_bytes(toolchain_manifest, 64 * 1024, code)
        receipt_payload = _stable_bytes(
            output / "native-build-receipt.json", 64 * 1024, code
        )
        manifest = json.loads(
            manifest_payload.decode("utf-8"), object_pairs_hook=_unique_object
        )
        receipt = json.loads(
            receipt_payload.decode("utf-8"), object_pairs_hook=_unique_object
        )
        if not isinstance(manifest, dict) or set(manifest) != {
            "schema_version",
            "target",
            "msvc_tools_version",
            "windows_sdk_version",
            "tools",
            "libraries",
        }:
            raise ValueError("manifest")
        if (
            manifest.get("schema_version") != 2
            or manifest.get("target") != "windows-x64-msvc"
            or not isinstance(manifest.get("msvc_tools_version"), str)
            or re.fullmatch(r"14\.[0-9]+\.[0-9]+", manifest["msvc_tools_version"])
            is None
            or not isinstance(manifest.get("windows_sdk_version"), str)
            or re.fullmatch(
                r"10\.0\.[0-9]+\.0", manifest["windows_sdk_version"]
            )
            is None
        ):
            raise ValueError("manifest")
        tools = manifest.get("tools")
        expected_tools = {
            "compiler": "cl.exe",
            "linker": "link.exe",
            "c1xx": "c1xx.dll",
            "c2": "c2.dll",
        }
        if not isinstance(tools, dict) or set(tools) != set(expected_tools):
            raise ValueError("tools")
        for name, file_name in expected_tools.items():
            descriptor = tools.get(name)
            if not isinstance(descriptor, dict) or set(descriptor) != {
                "file_name",
                "file_version",
                "product_version",
                "sha256",
                "authenticode_subject",
                "authenticode_thumbprint",
            }:
                raise ValueError(name)
            if (
                descriptor.get("file_name") != file_name
                or not isinstance(descriptor.get("file_version"), str)
                or re.fullmatch(
                    r"[0-9]+(?:\.[0-9]+){3}", descriptor["file_version"]
                )
                is None
                or not isinstance(descriptor.get("product_version"), str)
                or re.fullmatch(
                    r"[0-9]+(?:\.[0-9]+){3}", descriptor["product_version"]
                )
                is None
                or not isinstance(descriptor.get("sha256"), str)
                or _SHA256.fullmatch(descriptor["sha256"]) is None
                or not isinstance(descriptor.get("authenticode_subject"), str)
                or not descriptor["authenticode_subject"].strip()
                or not isinstance(
                    descriptor.get("authenticode_thumbprint"), str
                )
                or re.fullmatch(
                    r"[0-9a-f]{40}", descriptor["authenticode_thumbprint"]
                )
                is None
            ):
                raise ValueError(name)
        expected_libraries = {
            "advapi32.lib",
            "bcrypt.lib",
            "kernel32.lib",
            "libcmt.lib",
            "libcpmt.lib",
            "libucrt.lib",
            "libvcruntime.lib",
            "oldnames.lib",
            "shell32.lib",
            "userenv.lib",
            "ws2_32.lib",
        }
        libraries = manifest.get("libraries")
        if not isinstance(libraries, dict) or set(libraries) != expected_libraries:
            raise ValueError("libraries")
        if any(
            not isinstance(digest, str) or _SHA256.fullmatch(digest) is None
            for digest in libraries.values()
        ):
            raise ValueError("libraries")
        expected_receipt = {
            "schema_version",
            "status",
            "target",
            "authority_mode",
            "toolchain_manifest_sha256",
            "source_set_sha256",
            "msvc_tools_version",
            "windows_sdk_version",
            "msvc_root_sha256",
            "windows_sdk_root_sha256",
            "include_roots_sha256",
            "library_roots_sha256",
            "library_set_sha256",
            "compiler_sha256",
            "compiler_file_version",
            "compiler_authenticode_thumbprint",
            "linker_sha256",
            "linker_file_version",
            "linker_authenticode_thumbprint",
            "c1xx_sha256",
            "c1xx_authenticode_thumbprint",
            "c2_sha256",
            "c2_authenticode_thumbprint",
            "runtime_launcher_sha256",
            "sandbox_helper_sha256",
        }
        if not isinstance(receipt, dict) or set(receipt) != expected_receipt:
            raise ValueError("receipt")
        digest_fields = {
            name
            for name in expected_receipt
            if name.endswith("_sha256")
        }
        if any(
            not isinstance(receipt.get(name), str)
            or _SHA256.fullmatch(receipt[name]) is None
            for name in digest_fields
        ):
            raise ValueError("receipt_digest")
        source_names = (
            "ecorex_launcher.cpp",
            "ecorex_sandbox_host.cpp",
            "ecorex_sandbox_security.cpp",
            "ecorex_sandbox_process.cpp",
            "ecorex_sandbox_host_internal.h",
        )
        if source_root is None:
            source_root = ROOT / "platform-staging" / "native" / "windows"
        source_binding = "\0".join(
            f"{name}={_sha256(source_root / name)}"
            for name in sorted(source_names)
        ).encode("utf-8")
        library_binding = "\0".join(
            f"{name}={libraries[name]}" for name in sorted(libraries)
        ).encode("utf-8")
        if (
            receipt.get("schema_version") != 2
            or receipt.get("status") != "passed"
            or receipt.get("target") != "windows-x64"
            or receipt.get("authority_mode") != "caller-pinned"
            or receipt.get("toolchain_manifest_sha256")
            != hashlib.sha256(manifest_payload).hexdigest()
            or receipt.get("source_set_sha256")
            != hashlib.sha256(source_binding).hexdigest()
            or receipt.get("msvc_tools_version") != manifest["msvc_tools_version"]
            or receipt.get("windows_sdk_version")
            != manifest["windows_sdk_version"]
            or receipt.get("library_set_sha256")
            != hashlib.sha256(library_binding).hexdigest()
            or receipt.get("compiler_sha256") != tools["compiler"]["sha256"]
            or receipt.get("compiler_file_version")
            != tools["compiler"]["file_version"]
            or receipt.get("compiler_authenticode_thumbprint")
            != tools["compiler"]["authenticode_thumbprint"]
            or receipt.get("linker_sha256") != tools["linker"]["sha256"]
            or receipt.get("linker_file_version")
            != tools["linker"]["file_version"]
            or receipt.get("linker_authenticode_thumbprint")
            != tools["linker"]["authenticode_thumbprint"]
            or receipt.get("c1xx_sha256") != tools["c1xx"]["sha256"]
            or receipt.get("c1xx_authenticode_thumbprint")
            != tools["c1xx"]["authenticode_thumbprint"]
            or receipt.get("c2_sha256") != tools["c2"]["sha256"]
            or receipt.get("c2_authenticode_thumbprint")
            != tools["c2"]["authenticode_thumbprint"]
            or receipt.get("runtime_launcher_sha256") != _sha256(output / "ecorex.exe")
            or receipt.get("sandbox_helper_sha256")
            != _sha256(output / "ecorex-sandbox-host.exe")
        ):
            raise ValueError("binding")
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError):
        raise StageError(code) from None


def _build_python_closure(
    core: Path,
    platform: str,
    architecture: str,
) -> tuple[
    tuple[dict[str, str], ...],
    Path,
    PackPythonIdentity,
]:
    destination = core / "bin" / "pack-python"
    destination.mkdir(parents=True)
    source_prefix, executable, stdlib = _base_python_runtime_source(platform)
    if platform == "windows":
        _copy_regular(executable, destination / "python.exe", executable=True)
        for pattern in ("python*.dll", "vcruntime*.dll", "LICENSE*.txt"):
            for source in sorted(source_prefix.glob(pattern)):
                if source.is_file():
                    _copy_regular(source, destination / source.name)
        dlls = source_prefix / "DLLs"
        if dlls.is_dir():
            _copy_tree(dlls, destination / "DLLs")
        target_stdlib = destination / "Lib"
    else:
        _copy_regular(executable, destination / "bin" / "python3", executable=True)
        target_stdlib = destination / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}"
        lib_dir = source_prefix / "lib"
        for source in sorted(lib_dir.glob("libpython*.dylib")):
            resolved_source = _base_runtime_regular_file(
                source,
                prefix=source_prefix,
            )
            _copy_regular(
                resolved_source,
                destination / "lib" / source.name,
                executable=True,
            )
    _copy_tree(
        stdlib,
        target_stdlib,
        excluded=frozenset({"site-packages", "test", "tests", "idlelib", "tkinter", "ensurepip", "__pycache__"}),
    )
    site_packages = target_stdlib / "site-packages"
    site_packages.mkdir(parents=True)
    inventory = _copy_distribution_closure(_RUNTIME_DISTRIBUTIONS, site_packages)
    _copy_tree(
        ROOT / "ecorex",
        site_packages / "ecorex",
        excluded=frozenset({"__pycache__"}),
    )
    _compact_python_import_closure(
        destination,
        target_stdlib=target_stdlib,
        site_packages=site_packages,
        platform=platform,
    )
    if platform == "macos":
        _relocate_macos_python_closure(
            destination,
            source_prefix=source_prefix,
        )
    manifest = build_pack_python_manifest(
        core,
        platform=platform,
        architecture=architecture,
    )
    (core / "pack-python.json").write_bytes(manifest)
    interpreter, identity = resolve_pack_python(
        core,
        platform=platform,
        architecture=architecture,
    )
    probe = _pack_python_probe_command(interpreter)
    if platform == "macos":
        result = _run_macos_isolated_pack_probe(
            probe,
            cwd=core,
            source_prefix=source_prefix,
            source_canary=executable,
        )
    else:
        result = _run(probe, cwd=core, environment=_runtime_environment(), timeout=60, code="pack_python_probe_failed")
    if result.stdout.strip() != __version__.encode("ascii"):
        raise StageError("pack_python_probe_failed")
    # The independent post-write resolution above is the security boundary.
    # Reuse that immutable identity for the remaining synchronous stage instead
    # of scanning the exact same closure a third time before Pack staging.
    return inventory, interpreter, identity


def _base_python_runtime_source(platform: str) -> tuple[Path, Path, Path]:
    """Resolve the redistributable CPython root, never a venv launcher.

    On Windows a venv's ``Scripts/python.exe`` is a launcher whose home is
    supplied by the external ``pyvenv.cfg``. Copying it creates a package that
    only works while the build venv still exists. The base installation owns
    the real interpreter, DLLs and standard library copied into the closure.
    macOS keeps the existing resolved-interpreter behavior, but anchors the
    candidate explicitly below the same base prefix for closure identity.
    """

    try:
        prefix = Path(sys.base_prefix).resolve(strict=True)
    except (OSError, RuntimeError):
        raise StageError("pack_python_base_runtime_invalid") from None
    version = f"{sys.version_info.major}.{sys.version_info.minor}"
    if platform == "windows":
        executable_candidate = prefix / "python.exe"
        stdlib_candidate = prefix / "Lib"
    elif platform == "macos":
        executable_candidate = prefix / "bin" / f"python{version}"
        stdlib_candidate = prefix / "lib" / f"python{version}"
    else:
        raise StageError("pack_python_base_runtime_invalid")
    executable = _base_runtime_member(
        executable_candidate,
        prefix=prefix,
        directory=False,
    )
    stdlib = _base_runtime_member(
        stdlib_candidate,
        prefix=prefix,
        directory=True,
    )
    # Stable-read the actual interpreter before any closure bytes are copied.
    # _copy_regular repeats this identity check at the copy boundary.
    _stable_bytes(
        executable,
        _MAX_FILE_BYTES,
        "pack_python_base_runtime_invalid",
    )
    return prefix, executable, stdlib


def _base_runtime_member(
    candidate: Path,
    *,
    prefix: Path,
    directory: bool,
) -> Path:
    try:
        candidate_metadata = candidate.lstat()
    except (OSError, RuntimeError, ValueError):
        raise StageError("pack_python_base_runtime_invalid") from None
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    if stat.S_ISLNK(candidate_metadata.st_mode) or bool(
        getattr(candidate_metadata, "st_file_attributes", 0) & reparse
    ) or bool(getattr(candidate_metadata, "st_reparse_tag", 0)):
        raise StageError("pack_python_base_runtime_invalid")

    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(prefix)
        target_metadata = resolved.lstat()
    except (OSError, RuntimeError, ValueError):
        raise StageError("pack_python_base_runtime_invalid") from None
    attributes = getattr(target_metadata, "st_file_attributes", 0)
    if (
        stat.S_ISLNK(target_metadata.st_mode)
        or bool(attributes & reparse)
        or bool(getattr(target_metadata, "st_reparse_tag", 0))
        or (directory and not stat.S_ISDIR(target_metadata.st_mode))
        or (not directory and not stat.S_ISREG(target_metadata.st_mode))
    ):
        raise StageError("pack_python_base_runtime_invalid")
    return resolved


def _base_runtime_regular_file(candidate: Path, *, prefix: Path) -> Path:
    """Resolve a versioned base-runtime file link without weakening authority.

    Official macOS CPython layouts expose one or more ``libpython*.dylib``
    aliases as POSIX symlinks.  Copying the link itself is forbidden, but its
    resolved target is valid when it remains inside the already resolved base
    prefix and is a regular, non-link, non-reparse file.  Returning the resolved
    target also removes a candidate-link swap from the later stable read.
    """

    code = "pack_python_base_runtime_invalid"
    try:
        prefix = prefix.resolve(strict=True)
        candidate_metadata = candidate.lstat()
    except (OSError, RuntimeError, ValueError):
        raise StageError(code) from None
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    candidate_attributes = getattr(candidate_metadata, "st_file_attributes", 0)
    if (
        bool(candidate_attributes & reparse)
        or bool(getattr(candidate_metadata, "st_reparse_tag", 0))
        or not (
            stat.S_ISREG(candidate_metadata.st_mode)
            or stat.S_ISLNK(candidate_metadata.st_mode)
        )
    ):
        raise StageError(code)
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(prefix)
        target_metadata = resolved.lstat()
    except (OSError, RuntimeError, ValueError):
        raise StageError(code) from None
    target_attributes = getattr(target_metadata, "st_file_attributes", 0)
    if (
        stat.S_ISLNK(target_metadata.st_mode)
        or not stat.S_ISREG(target_metadata.st_mode)
        or bool(target_attributes & reparse)
        or bool(getattr(target_metadata, "st_reparse_tag", 0))
    ):
        raise StageError(code)
    return resolved


_MACHO_MAGICS = frozenset(
    {
        b"\xca\xfe\xba\xbe",
        b"\xbe\xba\xfe\xca",
        b"\xca\xfe\xba\xbf",
        b"\xbf\xba\xfe\xca",
        b"\xfe\xed\xfa\xce",
        b"\xce\xfa\xed\xfe",
        b"\xfe\xed\xfa\xcf",
        b"\xcf\xfa\xed\xfe",
    }
)
_MACOS_SYSTEM_LIBRARY_PREFIXES = (
    "/System/Library/",
    "/usr/lib/",
)
_PYTHON_FRAMEWORK_ROOT = Path("/Library/Frameworks/Python.framework")


def _macos_macho_files(root: Path) -> tuple[Path, ...]:
    try:
        resolved_root = root.resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        raise StageError("pack_python_macho_invalid") from None
    result: list[Path] = []
    for path in sorted(
        resolved_root.rglob("*"), key=lambda value: value.as_posix().casefold()
    ):
        try:
            metadata = path.lstat()
        except OSError:
            raise StageError("pack_python_macho_invalid") from None
        reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        if stat.S_ISLNK(metadata.st_mode) or bool(
            getattr(metadata, "st_file_attributes", 0) & reparse
        ) or bool(getattr(metadata, "st_reparse_tag", 0)):
            raise StageError("pack_python_macho_invalid")
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size < 4:
            continue
        try:
            with path.open("rb") as stream:
                magic = stream.read(4)
        except OSError:
            raise StageError("pack_python_macho_invalid") from None
        if magic in _MACHO_MAGICS:
            result.append(path)
    return tuple(result)


def _macos_architectures(path: Path) -> tuple[str, ...]:
    result = _run(
        ("/usr/bin/lipo", "-archs", str(path)),
        cwd=path.parent,
        environment=_runtime_environment(),
        timeout=30,
        code="pack_python_macho_inspection_failed",
    )
    try:
        architectures = tuple(result.stdout.decode("ascii").strip().split())
    except UnicodeDecodeError:
        raise StageError("pack_python_macho_inspection_failed") from None
    if (
        not architectures
        or len(architectures) != len(set(architectures))
        or any(architecture not in {"arm64", "x86_64"} for architecture in architectures)
    ):
        raise StageError("pack_python_macho_architecture_invalid")
    return architectures


def _macos_install_name(path: Path, *, architecture: str) -> str | None:
    result = _run(
        ("/usr/bin/otool", "-arch", architecture, "-D", str(path)),
        cwd=path.parent,
        environment=_runtime_environment(),
        timeout=30,
        code="pack_python_macho_inspection_failed",
    )
    try:
        lines = [
            line.strip()
            for line in result.stdout.decode("utf-8").splitlines()[1:]
            if line.strip()
        ]
    except UnicodeDecodeError:
        raise StageError("pack_python_macho_inspection_failed") from None
    if len(lines) > 1:
        raise StageError("pack_python_macho_inspection_failed")
    return lines[0] if lines else None


def _macos_dependencies(
    path: Path,
    *,
    architecture: str,
    install_name: str | None,
) -> tuple[str, ...]:
    result = _run(
        ("/usr/bin/otool", "-arch", architecture, "-L", str(path)),
        cwd=path.parent,
        environment=_runtime_environment(),
        timeout=30,
        code="pack_python_macho_inspection_failed",
    )
    try:
        lines = result.stdout.decode("utf-8").splitlines()
    except UnicodeDecodeError:
        raise StageError("pack_python_macho_inspection_failed") from None
    dependencies: list[str] = []
    for raw in lines[1:]:
        line = raw.strip()
        if not line:
            continue
        dependency, separator, metadata = line.rpartition(
            " (compatibility version "
        )
        if (
            not separator
            or not dependency
            or not metadata.endswith(")")
            or ", current version " not in metadata
        ):
            raise StageError("pack_python_macho_inspection_failed")
        dependencies.append(dependency)
    if install_name is not None and dependencies and dependencies[0] == install_name:
        dependencies.pop(0)
    return tuple(dependencies)


def _macos_rpaths(path: Path, *, architecture: str) -> tuple[str, ...]:
    result = _run(
        ("/usr/bin/otool", "-arch", architecture, "-l", str(path)),
        cwd=path.parent,
        environment=_runtime_environment(),
        timeout=30,
        code="pack_python_macho_inspection_failed",
    )
    try:
        lines = result.stdout.decode("utf-8").splitlines()
    except UnicodeDecodeError:
        raise StageError("pack_python_macho_inspection_failed") from None
    rpaths: list[str] = []
    in_rpath = False
    for raw in lines:
        line = raw.strip()
        if line.startswith("cmd "):
            in_rpath = line == "cmd LC_RPATH"
            continue
        if in_rpath and line.startswith("path "):
            value, separator, offset = line[5:].rpartition(" (offset ")
            if (
                not separator
                or not value
                or re.fullmatch(r"[0-9]+\)", offset) is None
            ):
                raise StageError("pack_python_macho_inspection_failed")
            rpaths.append(value)
            in_rpath = False
    return tuple(rpaths)


def _common_macos_rpaths(
    rpaths_by_architecture: Mapping[str, tuple[str, ...]],
) -> tuple[str, ...]:
    values = tuple(rpaths_by_architecture.values())
    if not values or len(set(values)) != 1:
        raise StageError("pack_python_macho_architecture_drift")
    return values[0]


def _macos_dependency_requires_relocation(
    dependency: str,
    *,
    source_prefix: Path,
) -> bool:
    if dependency.startswith("@"):
        if dependency.startswith(
            ("@loader_path/", "@executable_path/", "@rpath/")
        ):
            return False
        raise StageError("pack_python_macho_dependency_invalid")
    if not dependency.startswith("/"):
        raise StageError("pack_python_macho_dependency_invalid")
    portable = PurePosixPath(dependency)
    if ".." in portable.parts or portable.as_posix() != dependency:
        raise StageError("pack_python_macho_dependency_invalid")
    if any(
        dependency.startswith(prefix)
        for prefix in _MACOS_SYSTEM_LIBRARY_PREFIXES
    ):
        return False
    # Any non-system absolute load path is not portable. This includes the
    # setup-python toolcache/base prefix, RUNNER_TEMP and Python.framework.
    return True


def _macos_rpath_requires_removal(
    rpath: str,
    *,
    binary: Path,
    closure: Path,
) -> bool:
    if rpath.startswith("@") and not rpath.startswith(
        ("@loader_path/", "@executable_path/", "@rpath/")
    ):
        raise StageError("pack_python_macho_rpath_invalid")
    if rpath.startswith("/"):
        portable = PurePosixPath(rpath)
        if ".." in portable.parts or portable.as_posix() != rpath:
            raise StageError("pack_python_macho_rpath_invalid")
    if rpath.startswith("@loader_path/"):
        target = (binary.parent / rpath.removeprefix("@loader_path/")).resolve()
        try:
            target.relative_to(closure)
        except ValueError:
            return True
        return False
    if rpath.startswith("@executable_path/"):
        target = (
            closure / "bin" / rpath.removeprefix("@executable_path/")
        ).resolve()
        try:
            target.relative_to(closure)
        except ValueError:
            return True
        return False
    if any(rpath.startswith(prefix) for prefix in _MACOS_SYSTEM_LIBRARY_PREFIXES):
        return False
    return True


def _macos_relocation_target(
    dependency: str,
    *,
    binary: Path,
    closure: Path,
    source_prefix: Path,
    macho_files: tuple[Path, ...],
) -> tuple[Path, str]:
    candidates: list[Path] = []
    dependency_path = Path(dependency)
    try:
        relative = dependency_path.relative_to(source_prefix)
    except ValueError:
        pass
    else:
        exact = closure / relative
        if exact in macho_files:
            candidates.append(exact)
    candidates.extend(
        path
        for path in macho_files
        if path.name == dependency_path.name and path not in candidates
    )
    if (
        dependency_path.name == "Python"
        and "Python.framework" in dependency_path.parts
    ):
        versioned = closure / "lib" / (
            f"libpython{sys.version_info.major}.{sys.version_info.minor}.dylib"
        )
        if versioned in macho_files:
            candidates = [versioned]
    if len(candidates) != 1:
        raise StageError("pack_python_macho_dependency_unresolved")
    target = candidates[0]
    relative_target = os.path.relpath(target, binary.parent).replace(os.sep, "/")
    relocated = f"@loader_path/{relative_target}"
    return target, relocated


def _validate_macos_relative_dependency(
    dependency: str,
    *,
    binary: Path,
    closure: Path,
    macho_files: tuple[Path, ...],
) -> None:
    if dependency.startswith("@loader_path/"):
        target = (
            binary.parent / dependency.removeprefix("@loader_path/")
        ).resolve()
    elif dependency.startswith("@executable_path/"):
        target = (
            closure / "bin" / dependency.removeprefix("@executable_path/")
        ).resolve()
    else:
        return
    try:
        target.relative_to(closure)
    except ValueError:
        raise StageError("pack_python_macho_dependency_invalid") from None
    if target not in macho_files:
        raise StageError("pack_python_macho_dependency_unresolved")


def _relocate_macos_python_closure(
    closure: Path,
    *,
    source_prefix: Path,
) -> None:
    closure = closure.resolve(strict=True)
    source_prefix = source_prefix.resolve(strict=True)
    required_tools = (
        Path("/usr/bin/lipo"),
        Path("/usr/bin/otool"),
        Path("/usr/bin/install_name_tool"),
        Path("/usr/bin/codesign"),
    )
    if any(not tool.is_file() for tool in required_tools):
        raise StageError("pack_python_macho_tooling_missing")
    macho_files = _macos_macho_files(closure)
    if not macho_files or closure / "bin" / "python3" not in macho_files:
        raise StageError("pack_python_macho_invalid")
    for binary in macho_files:
        architectures = _macos_architectures(binary)
        install_names = {
            architecture: _macos_install_name(
                binary,
                architecture=architecture,
            )
            for architecture in architectures
        }
        rpaths_by_architecture = {
            architecture: _macos_rpaths(
                binary,
                architecture=architecture,
            )
            for architecture in architectures
        }
        rpaths = _common_macos_rpaths(rpaths_by_architecture)
        changes: list[tuple[str, str]] = []
        dependencies = tuple(
            dict.fromkeys(
                dependency
                for architecture in architectures
                for dependency in _macos_dependencies(
                    binary,
                    architecture=architecture,
                    install_name=install_names[architecture],
                )
            )
        )
        for dependency in dependencies:
            if dependency.startswith(("@loader_path/", "@executable_path/")):
                _validate_macos_relative_dependency(
                    dependency,
                    binary=binary,
                    closure=closure,
                    macho_files=macho_files,
                )
                continue
            if dependency.startswith("@rpath/"):
                _, relocated = _macos_relocation_target(
                    dependency,
                    binary=binary,
                    closure=closure,
                    source_prefix=source_prefix,
                    macho_files=macho_files,
                )
                changes.append((dependency, relocated))
                continue
            if not _macos_dependency_requires_relocation(
                dependency,
                source_prefix=source_prefix,
            ):
                continue
            _, relocated = _macos_relocation_target(
                dependency,
                binary=binary,
                closure=closure,
                source_prefix=source_prefix,
                macho_files=macho_files,
            )
            changes.append((dependency, relocated))
        expected_install_name = f"@loader_path/{binary.name}"
        relocated_install_name = (
            expected_install_name
            if any(
                install_name is not None and install_name != expected_install_name
                for install_name in install_names.values()
            )
            else None
        )
        removed_rpaths = tuple(
            rpath
            for rpath in rpaths
            if _macos_rpath_requires_removal(
                rpath,
                binary=binary,
                closure=closure,
            )
        )
        if not changes and relocated_install_name is None and not removed_rpaths:
            continue
        command: list[str] = ["/usr/bin/install_name_tool"]
        if relocated_install_name is not None:
            command.extend(("-id", relocated_install_name))
        for old, new in changes:
            command.extend(("-change", old, new))
        for rpath in removed_rpaths:
            command.extend(("-delete_rpath", rpath))
        command.append(str(binary))
        _run(
            tuple(command),
            cwd=closure,
            environment=_runtime_environment(),
            timeout=30,
            code="pack_python_macho_relocation_failed",
        )
        _run(
            (
                "/usr/bin/codesign",
                "--force",
                "--sign",
                "-",
                "--timestamp=none",
                str(binary),
            ),
            cwd=closure,
            environment=_runtime_environment(),
            timeout=30,
            code="pack_python_macho_signing_failed",
        )
        _run(
            ("/usr/bin/codesign", "--verify", "--strict", str(binary)),
            cwd=closure,
            environment=_runtime_environment(),
            timeout=30,
            code="pack_python_macho_signing_failed",
        )
    for binary in _macos_macho_files(closure):
        for architecture in _macos_architectures(binary):
            install_name = _macos_install_name(
                binary,
                architecture=architecture,
            )
            if (
                install_name is not None
                and install_name != f"@loader_path/{binary.name}"
            ):
                raise StageError("pack_python_macho_install_name_not_relocated")
            if any(
                _macos_rpath_requires_removal(
                    rpath,
                    binary=binary,
                    closure=closure,
                )
                for rpath in _macos_rpaths(
                    binary,
                    architecture=architecture,
                )
            ):
                raise StageError("pack_python_macho_rpath_not_relocated")
            for dependency in _macos_dependencies(
                binary,
                architecture=architecture,
                install_name=install_name,
            ):
                if dependency.startswith(("@loader_path/", "@executable_path/")):
                    _validate_macos_relative_dependency(
                        dependency,
                        binary=binary,
                        closure=closure,
                        macho_files=macho_files,
                    )
                elif dependency.startswith("@rpath/") or (
                    _macos_dependency_requires_relocation(
                        dependency,
                        source_prefix=source_prefix,
                    )
                ):
                    raise StageError("pack_python_macho_dependency_not_relocated")


def _seatbelt_literal(path: Path) -> str:
    if not path.is_absolute():
        raise StageError("pack_python_sandbox_probe_invalid")
    value = str(path)
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _run_macos_isolated_pack_probe(
    probe: tuple[str, ...],
    *,
    cwd: Path,
    source_prefix: Path,
    source_canary: Path,
) -> BoundedProcessResult:
    sandbox = Path("/usr/bin/sandbox-exec")
    cat = Path("/bin/cat")
    if not sandbox.is_file() or not cat.is_file():
        raise StageError("pack_python_sandbox_probe_unavailable")
    source_canary = source_canary.resolve(strict=True)
    try:
        source_canary.relative_to(source_prefix.resolve(strict=True))
    except (OSError, RuntimeError, ValueError):
        raise StageError("pack_python_sandbox_probe_invalid") from None
    _stable_bytes(
        source_canary,
        _MAX_FILE_BYTES,
        "pack_python_sandbox_probe_invalid",
    )
    denied = (
        source_prefix.resolve(strict=True),
        _PYTHON_FRAMEWORK_ROOT,
    )
    rules = "".join(
        f'(deny file-read* (subpath "{_seatbelt_literal(path)}"))'
        f'(deny file-map-executable (subpath "{_seatbelt_literal(path)}"))'
        for path in denied
    )
    profile = f"(version 1)(allow default){rules}"
    try:
        denial = run_bounded_process(
            (str(sandbox), "-p", profile, str(cat), str(source_canary)),
            payload=None,
            cwd=cwd,
            environment=_runtime_environment(),
            timeout_seconds=30,
            max_stdout_bytes=64 * 1024,
            max_stderr_bytes=64 * 1024,
        )
    except (OSError, BoundedProcessError):
        raise StageError("pack_python_sandbox_probe_invalid") from None
    if denial.returncode == 0:
        raise StageError("pack_python_sandbox_probe_not_enforced")
    result = _run(
        (str(sandbox), "-p", profile, *probe),
        cwd=cwd,
        environment=_runtime_environment(),
        timeout=60,
        code="pack_python_probe_failed",
    )
    print("macos_pack_python_sandbox_probe=passed", flush=True)
    return result


def _compact_python_import_closure(
    destination: Path,
    *,
    target_stdlib: Path,
    site_packages: Path,
    platform: str,
) -> Mapping[str, Any]:
    """Collapse zip-safe Python files without weakening closure verification.

    CPython already places its versioned standard-library archive before the
    unpacked Lib directory.  The signed Core therefore needs one content hash
    for this deterministic archive instead of thousands of cold small-file
    opens on every independent Runtime verification.  Native-backed packages
    and packages with path-sensitive data remain unpacked.
    """

    root = destination.resolve(strict=True)
    stdlib = target_stdlib.resolve(strict=True)
    packages = site_packages.resolve(strict=True)
    if packages.parent != stdlib or packages.name != "site-packages":
        raise StageError("pack_python_import_layout_invalid")
    try:
        stdlib.relative_to(root)
    except ValueError:
        raise StageError("pack_python_import_layout_invalid") from None
    if platform == "windows":
        archive_path = root / f"python{sys.version_info.major}{sys.version_info.minor}.zip"
    elif platform == "macos":
        archive_path = (
            root
            / "lib"
            / f"python{sys.version_info.major}{sys.version_info.minor}.zip"
        )
    else:
        raise StageError("pack_python_import_layout_invalid")
    if os.path.lexists(archive_path):
        raise StageError("pack_python_import_archive_collision")

    selected: list[tuple[str, Path]] = []
    for entry in sorted(stdlib.iterdir(), key=lambda item: item.name.casefold()):
        if entry == packages:
            continue
        if _zip_safe_import_entry(entry, allow_resources=False):
            selected.extend(_import_archive_members(entry, base=stdlib))
    for entry in sorted(packages.iterdir(), key=lambda item: item.name.casefold()):
        allow_resources = (
            entry.name.casefold() in _IMPORT_ARCHIVE_RESOURCE_PACKAGES
            or entry.name.casefold().endswith((".dist-info", ".egg-info"))
        )
        if _zip_safe_import_entry(entry, allow_resources=allow_resources):
            selected.extend(_import_archive_members(entry, base=packages))
    selected.sort(key=lambda item: item[0].casefold())
    if not selected:
        raise StageError("pack_python_import_archive_empty")
    seen: set[str] = set()
    total = 0
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{archive_path.name}.", suffix=".tmp", dir=archive_path.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        with zipfile.ZipFile(
            temporary,
            "w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=9,
            allowZip64=True,
            strict_timestamps=True,
        ) as archive:
            for relative, source in selected:
                collision = relative.casefold()
                if collision in seen:
                    raise StageError("pack_python_import_archive_collision")
                seen.add(collision)
                payload = _stable_bytes(
                    source,
                    _MAX_FILE_BYTES,
                    "pack_python_import_source_invalid",
                    minimum=0,
                )
                total += len(payload)
                if total > 1024 * 1024 * 1024:
                    raise StageError("pack_python_import_archive_too_large")
                info = zipfile.ZipInfo(relative, date_time=_FIXED_TIME)
                info.compress_type = zipfile.ZIP_DEFLATED
                info.create_system = 3
                info.external_attr = (stat.S_IFREG | 0o644) << 16
                archive.writestr(info, payload)
        with temporary.open("r+b") as stream:
            os.fsync(stream.fileno())
        os.replace(temporary, archive_path)
        for _relative, source in selected:
            source.unlink()
        for base in (packages, stdlib):
            directories = sorted(
                (path for path in base.rglob("*") if path.is_dir()),
                key=lambda path: len(path.parts),
                reverse=True,
            )
            for directory in directories:
                try:
                    directory.rmdir()
                except OSError:
                    pass
        packages.mkdir(parents=True, exist_ok=True)
        return {
            "relative_path": archive_path.relative_to(root).as_posix(),
            "member_count": len(selected),
            "uncompressed_size_bytes": total,
            "size_bytes": archive_path.stat().st_size,
            "sha256": _sha256(archive_path),
        }
    finally:
        temporary.unlink(missing_ok=True)


def _zip_safe_import_entry(path: Path, *, allow_resources: bool) -> bool:
    try:
        metadata = path.lstat()
    except OSError:
        raise StageError("pack_python_import_source_invalid") from None
    if stat.S_ISLNK(metadata.st_mode) or bool(
        getattr(metadata, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    ):
        raise StageError("pack_python_import_source_invalid")
    if stat.S_ISREG(metadata.st_mode):
        return path.suffix.casefold() in _IMPORT_ARCHIVE_PURE_SUFFIXES
    if not stat.S_ISDIR(metadata.st_mode):
        raise StageError("pack_python_import_source_invalid")
    files = _regular_import_tree(path)
    if not files:
        return False
    if any(
        item.suffix.casefold() in _IMPORT_ARCHIVE_NATIVE_SUFFIXES
        for item in files
    ):
        return False
    return allow_resources or all(
        item.suffix.casefold() in _IMPORT_ARCHIVE_PURE_SUFFIXES for item in files
    )


def _regular_import_tree(root: Path) -> tuple[Path, ...]:
    files: list[Path] = []
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix().casefold()):
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode) or bool(
            getattr(metadata, "st_file_attributes", 0)
            & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        ):
            raise StageError("pack_python_import_source_invalid")
        if stat.S_ISDIR(metadata.st_mode):
            continue
        if not stat.S_ISREG(metadata.st_mode):
            raise StageError("pack_python_import_source_invalid")
        files.append(path)
    return tuple(files)


def _import_archive_members(path: Path, *, base: Path) -> tuple[tuple[str, Path], ...]:
    files = (path,) if path.is_file() else _regular_import_tree(path)
    members: list[tuple[str, Path]] = []
    for source in files:
        relative = source.relative_to(base).as_posix()
        value = PurePosixPath(relative)
        if (
            value.is_absolute()
            or not value.parts
            or any(part in {"", ".", ".."} or ":" in part for part in value.parts)
        ):
            raise StageError("pack_python_import_source_invalid")
        members.append((relative, source))
    return tuple(members)


def _pack_python_probe_command(interpreter: Path) -> tuple[str, ...]:
    return (
        str(interpreter),
        "-I",
        "-B",
        "-c",
        (
            "import certifi,cryptography,fastapi,httpx,pydantic,tzdata,uvicorn,websockets,ecorex,zoneinfo;"
            "from pathlib import Path;"
            "from ecorex.control_plane.admin_web.assets import AdminWebAssets;"
            "from multipart.multipart import parse_options_header;"
            "assert parse_options_header;"
            "assert Path(certifi.where()).is_file();"
            "assert len(AdminWebAssets.load().assets)==2;"
            "zoneinfo.reset_tzpath(());zoneinfo.ZoneInfo.clear_cache();"
            "assert zoneinfo.ZoneInfo('Asia/Shanghai').key == 'Asia/Shanghai';"
            "print(ecorex.__version__)"
        ),
    )


def _install_native(native: Path, core: Path, platform: str) -> None:
    bin_dir = core / "bin"
    bin_dir.mkdir(exist_ok=True)
    launcher = "ecorex.exe" if platform == "windows" else "ecorex"
    _copy_regular(native / launcher, bin_dir / launcher, executable=True)
    if platform == "windows":
        _copy_regular(
            native / "ecorex-sandbox-host.exe",
            bin_dir / "ecorex-sandbox-host.exe",
            executable=True,
        )


def _build_bootstrap(
    destination: Path,
    *,
    platform: str,
    architecture: str,
    native: Path,
    public_index_url: str,
    runtime_config: ProductRuntimeConfig,
    publication_public_keys: Mapping[str, str],
    evidence: Path,
) -> None:
    """Build one dependency-free, signed-release-verifying first installer."""

    go = shutil.which("go")
    if go is None:
        raise StageError("bootstrap_go_toolchain_unavailable")
    version = _run(
        (go, "version"),
        cwd=ROOT,
        environment=_build_environment(),
        timeout=15,
        code="bootstrap_go_toolchain_invalid",
    ).stdout.decode("ascii", errors="ignore").strip()
    if not version.startswith("go version go1.26.5 "):
        raise StageError("bootstrap_go_toolchain_invalid")
    source = ROOT / "platform-staging" / "bootstrap"
    if not (source / "go.mod").is_file() or not (source / "main.go").is_file():
        raise StageError("bootstrap_source_missing")
    binary = destination / "bin" / (
        "ecorex-bootstrap.exe" if platform == "windows" else "ecorex-bootstrap"
    )
    binary.parent.mkdir(parents=True)
    sandbox_helper_sha256 = ""
    if platform == "windows":
        helper = destination / "bin" / "ecorex-sandbox-host.exe"
        _copy_regular(
            native / "ecorex-sandbox-host.exe",
            helper,
            executable=True,
        )
        sandbox_helper_sha256 = _sha256(helper)
    encoded_release_keys = {
        key_id: base64.b64encode(value).decode("ascii")
        for key_id, value in sorted(runtime_config.release_public_keys.items())
    }
    if set(encoded_release_keys).intersection(publication_public_keys) or set(
        encoded_release_keys.values()
    ).intersection(publication_public_keys.values()):
        raise StageError("bootstrap_publication_trust_not_separated")
    release_keys_sha256 = hashlib.sha256(
        json.dumps(
            encoded_release_keys,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    public_index_url_sha256 = hashlib.sha256(
        public_index_url.encode("utf-8")
    ).hexdigest()
    publication_keys_sha256 = hashlib.sha256(
        json.dumps(
            publication_public_keys,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    embedded_helper_identity = sandbox_helper_sha256 or "none"
    linker_flags = " ".join(
        (
            "-s",
            "-w",
            "-buildid=",
            f"-X=main.embeddedReleaseKeysSHA256={release_keys_sha256}",
            f"-X=main.embeddedSandboxHelperSHA256={embedded_helper_identity}",
            f"-X=main.embeddedPublicIndexURLSHA256={public_index_url_sha256}",
            f"-X=main.embeddedPublicationKeysSHA256={publication_keys_sha256}",
        )
    )
    environment = dict(_build_environment())
    environment.update(
        {
            "CGO_ENABLED": "0",
            "GOARCH": "amd64" if architecture == "x64" else "arm64",
            "GOOS": "windows" if platform == "windows" else "darwin",
            "GOTOOLCHAIN": "local",
        }
    )
    _run(
        (go, "test", "-mod=readonly", "./..."),
        cwd=source,
        environment=environment,
        timeout=180,
        code="bootstrap_test_failed",
    )
    _run(
        (
            go,
            "build",
            "-trimpath",
            "-buildvcs=false",
            "-mod=readonly",
            f"-ldflags={linker_flags}",
            "-o",
            str(binary),
            ".",
        ),
        cwd=source,
        environment=environment,
        timeout=180,
        code="bootstrap_build_failed",
    )
    if not binary.is_file() or not 1 <= binary.stat().st_size <= 10 * 1024 * 1024:
        raise StageError("bootstrap_size_limit")
    if platform != "windows":
        binary.chmod(0o755)
    config = {
        "schema_version": 1,
        "public_index_url": public_index_url,
        "sandbox_helper_sha256": sandbox_helper_sha256,
        "release_public_keys": encoded_release_keys,
        "publication_public_keys": dict(publication_public_keys),
        # The release-key signature is injected by Candidate assembly after
        # attested stage ingestion and before the Bootstrap artifact is signed.
        "minimum_stable": None,
    }
    (destination / "bootstrap-config.json").write_text(
        json.dumps(config, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    probe = _run(
        (str(binary), "--self-test"),
        cwd=destination,
        environment=_runtime_environment(),
        timeout=15,
        code="bootstrap_launch_probe_failed",
    )
    try:
        probe_value = json.loads(probe.stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise StageError("bootstrap_launch_probe_failed") from None
    if probe_value != {
        "architecture": architecture,
        "platform": platform,
        "schema_version": 1,
        "status": "passed",
    }:
        raise StageError("bootstrap_launch_probe_failed")
    source_records = _tree_records(source)
    tree_records = _tree_records(destination)
    _gate(
        evidence,
        "bootstrap-launch",
        {
            "entrypoint_sha256": _sha256(binary),
            "entrypoint_size_bytes": binary.stat().st_size,
            "sandbox_helper_sha256": sandbox_helper_sha256 or None,
            "release_keys_sha256": release_keys_sha256,
            "public_index_url_sha256": public_index_url_sha256,
            "publication_keys_sha256": publication_keys_sha256,
            "probe": probe_value,
        },
    )
    _gate(
        evidence,
        "toolchain",
        {
            "go_version": version,
            "module": "stdlib-only",
            "source_tree_sha256": hashlib.sha256(
                json.dumps(source_records, sort_keys=True, separators=(",", ":")).encode(
                    "utf-8"
                )
            ).hexdigest(),
        },
    )
    _gate(
        evidence,
        "supply-chain",
        {
            "tree_sha256": hashlib.sha256(
                json.dumps(tree_records, sort_keys=True, separators=(",", ":")).encode(
                    "utf-8"
                )
            ).hexdigest(),
            "file_count": len(tree_records),
            "size_bytes": sum(item["size_bytes"] for item in tree_records),
            "dependencies": "go-standard-library-only",
            "secret_scan": "passed",
        },
    )
def _write_runtime_config(core: Path, platform: str, architecture: str) -> str:
    source = _pinned_environment_file(
        "ECOREX_STAGE_RUNTIME_CONFIG_TEMPLATE",
        "ECOREX_STAGE_RUNTIME_CONFIG_TEMPLATE_SHA256",
    )
    payload = _stable_bytes(source, 256 * 1024, "runtime_config_template_invalid")
    if b".invalid" in payload or any(pattern.search(payload) for pattern in _SECRET_PATTERNS):
        raise StageError("runtime_config_template_not_production")
    try:
        raw = json.loads(payload.decode("utf-8"), object_pairs_hook=_unique_object)
        if not isinstance(raw, dict) or not isinstance(raw.get("identity"), dict):
            raise ValueError("shape")
        raw["identity"] = {
            "version": __version__,
            "platform": platform,
            "architecture": architecture,
        }
        raw["capability_packs"] = [
            {
                "pack_id": pack_id,
                "manifest": (
                    f"capability-packs/{pack_id}/ecorex-capability-pack-{pack_id}-"
                    f"{platform}-{architecture}-{__version__}.json"
                ),
                "artifact": (
                    f"capability-packs/{pack_id}/ecorex-capability-pack-{pack_id}-"
                    f"{platform}-{architecture}-{__version__}.zip"
                ),
            }
            for pack_id in PACK_TOOLS
        ]
        canonical = json.dumps(raw, sort_keys=True, separators=(",", ":")).encode("utf-8")
        config = ProductRuntimeConfig.from_bytes(canonical)
    except Exception:
        raise StageError("runtime_config_template_invalid") from None
    if config.identity.version != __version__:
        raise StageError("runtime_config_identity_invalid")
    destination = core / "runtime-config.json"
    destination.write_bytes(config.to_bytes())
    return hashlib.sha256(destination.read_bytes()).hexdigest()


def _scan_final_web() -> Mapping[str, Any]:
    value = os.environ.get("ECOREX_STAGE_WEB_DIST")
    if not value:
        raise StageError("final_web_dist_missing")
    root = _absolute_directory(value, "final_web_dist_invalid")
    try:
        scanned = scan_web_bundle(WebBundleBuildInput(root))
    except Exception:
        raise StageError("final_web_dist_invalid") from None
    return {
        "bundle_sha256": scanned.bundle_sha256,
        "file_count": len(scanned.files),
        "size_bytes": sum(record.size_bytes for record in scanned.files),
    }


def _stage_packs(
    root: Path,
    *,
    platform: str,
    architecture: str,
    interpreter: Path,
    native: Path,
    evidence: Path,
) -> None:
    common = ROOT / "release" / "capability-packs" / "common" / "ecorex_pack_protocol.py"
    for pack_id in PACK_TOOLS:
        source = ROOT / "release" / "capability-packs" / pack_id
        destination = root / pack_id
        _copy_tree(source, destination, excluded=frozenset({"__pycache__"}))
        if pack_id in {"browser", "sandbox"}:
            _copy_regular(common, destination / common.name)
            _normalize_process_pack_descriptor(destination, pack_id=pack_id)
    browser_inventory = _vendor_browser_runtime(root / "browser")
    channel_inventory = _write_dependency_inventory(
        root / "channels",
        pack_id="channels",
        distributions=(),
    )
    ocr_inventory = _vendor_dependency_runtime(
        root / "ocr",
        pack_id="ocr",
        distributions=("rapidocr-onnxruntime", "onnxruntime"),
    )
    office_inventory = _vendor_dependency_runtime(
        root / "office",
        pack_id="office",
        distributions=(
            "openpyxl",
            "python-docx",
            "python-pptx",
            "pypdf",
            "reportlab",
        ),
    )
    _browser_gates(
        root / "browser",
        interpreter=interpreter,
        inventory=browser_inventory,
        evidence=evidence / "browser",
    )
    _image_gates(root / "image", interpreter=interpreter, evidence=evidence / "image")
    _channels_gates(
        root / "channels",
        inventory=channel_inventory,
        evidence=evidence / "channels",
    )
    _dependency_runtime_gates(
        root / "ocr",
        pack_id="ocr",
        interpreter=interpreter,
        inventory=ocr_inventory,
        evidence=evidence / "ocr",
    )
    _dependency_runtime_gates(
        root / "office",
        pack_id="office",
        interpreter=interpreter,
        inventory=office_inventory,
        evidence=evidence / "office",
    )
    _sandbox_gates(
        root / "sandbox",
        platform=platform,
        architecture=architecture,
        interpreter=interpreter,
        native=native,
        evidence=evidence / "sandbox",
    )


def _expected_process_pack_descriptor(pack_id: str) -> dict[str, Any]:
    if pack_id not in {"browser", "sandbox"}:
        raise StageError("capability_pack_descriptor_invalid")
    return {
        "schema_version": 1,
        "protocol": "ecorex-stdio-tool-v1",
        "pack_id": pack_id,
        "runtime_api_version": "1.0.0",
        "tools": list(PACK_TOOLS[pack_id]),
    }


def _canonical_process_pack_descriptor(pack_id: str) -> bytes:
    return json.dumps(
        _expected_process_pack_descriptor(pack_id),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _normalize_process_pack_descriptor(pack: Path, *, pack_id: str) -> Mapping[str, Any]:
    """Validate the source template, then emit Runtime-canonical bytes.

    Repository JSON files conventionally end in LF.  The signed process-pack
    protocol intentionally does not: Runtime compares the exact canonical
    descriptor bytes before it constructs a handler.  Staging owns that
    generated wire artifact so formatting can never make a signed Pack
    uninstallable.
    """

    path = pack / "ecorex-pack.json"
    payload = _stable_bytes(
        path,
        64 * 1024,
        "capability_pack_descriptor_invalid",
    )
    try:
        value = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError):
        raise StageError("capability_pack_descriptor_invalid") from None
    expected = _expected_process_pack_descriptor(pack_id)
    if value != expected:
        raise StageError("capability_pack_descriptor_invalid")
    canonical = _canonical_process_pack_descriptor(pack_id)
    path.write_bytes(canonical)
    if _stable_bytes(
        path,
        64 * 1024,
        "capability_pack_descriptor_invalid",
    ) != canonical:
        raise StageError("capability_pack_descriptor_invalid")
    return expected


def _read_canonical_process_pack_descriptor(
    pack: Path,
    *,
    pack_id: str,
) -> Mapping[str, Any]:
    payload = _stable_bytes(
        pack / "ecorex-pack.json",
        64 * 1024,
        "capability_pack_descriptor_invalid",
    )
    expected = _expected_process_pack_descriptor(pack_id)
    if payload != _canonical_process_pack_descriptor(pack_id):
        raise StageError("capability_pack_descriptor_invalid")
    return expected


def _vendor_dependency_runtime(
    pack: Path,
    *,
    pack_id: str,
    distributions: tuple[str, ...],
) -> tuple[dict[str, str], ...]:
    python_root = pack / "runtime" / "python"
    inventory = _copy_distribution_closure(distributions, python_root)
    return _write_dependency_inventory(
        pack,
        pack_id=pack_id,
        distributions=inventory,
    )


def _write_dependency_inventory(
    pack: Path,
    *,
    pack_id: str,
    distributions: Iterable[Mapping[str, str]],
) -> tuple[dict[str, str], ...]:
    normalized = tuple(dict(item) for item in distributions)
    records = _dependency_payload_records(pack)
    payload_sha256 = hashlib.sha256(
        json.dumps(records, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    (pack / "runtime-inventory.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "pack_id": pack_id,
                "distributions": list(normalized),
                "payload_sha256": payload_sha256,
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
        encoding="utf-8",
        newline="\n",
    )
    _validate_dependency_pack(
        pack,
        pack_id=pack_id,
        distributions=normalized,
    )
    return normalized


def _dependency_payload_records(pack: Path) -> list[dict[str, Any]]:
    return [
        record
        for record in _tree_records(pack)
        if record["path"] != "runtime-inventory.json"
    ]


def _validate_dependency_pack(
    pack: Path,
    *,
    pack_id: str,
    distributions: tuple[dict[str, str], ...],
) -> Mapping[str, Any]:
    if pack_id not in _DEPENDENCY_PACK_ADAPTERS:
        raise StageError("dependency_pack_contract_invalid")
    try:
        descriptor = json.loads(
            (pack / "ecorex-dependency-pack.json").read_text(encoding="utf-8"),
            object_pairs_hook=_unique_object,
        )
        inventory = json.loads(
            (pack / "runtime-inventory.json").read_text(encoding="utf-8"),
            object_pairs_hook=_unique_object,
        )
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        raise StageError("dependency_pack_contract_invalid") from None
    expected_descriptor = {
        "schema_version": 1,
        "kind": "dependency-service",
        "pack_id": pack_id,
        "adapter": _DEPENDENCY_PACK_ADAPTERS[pack_id],
        "runtime_api_version": "1.0.0",
        "inventory": "runtime-inventory.json",
        "services": list(PACK_SERVICES[pack_id]),
    }
    normalized = [dict(item) for item in distributions]
    if (
        descriptor != expected_descriptor
        or not isinstance(inventory, dict)
        or set(inventory)
        != {"schema_version", "pack_id", "distributions", "payload_sha256"}
        or inventory.get("schema_version") != 1
        or inventory.get("pack_id") != pack_id
        or inventory.get("distributions") != normalized
    ):
        raise StageError("dependency_pack_contract_invalid")
    payload_sha256 = hashlib.sha256(
        json.dumps(
            _dependency_payload_records(pack),
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    if inventory.get("payload_sha256") != payload_sha256:
        raise StageError("dependency_pack_payload_mismatch")
    return inventory


def _vendor_browser_runtime(pack: Path) -> tuple[dict[str, str], ...]:
    try:
        distribution = importlib_metadata.distribution("playwright")
    except importlib_metadata.PackageNotFoundError:
        raise StageError("playwright_runtime_unavailable") from None
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            executable = Path(playwright.chromium.executable_path).resolve(strict=True)
    except Exception:
        raise StageError("playwright_chromium_unavailable") from None
    browser_root = executable.parent
    while browser_root.parent != browser_root and not (
        browser_root.name.startswith("chromium-")
        or browser_root.name.startswith("chromium_headless_shell-")
    ):
        browser_root = browser_root.parent
    if browser_root.parent == browser_root:
        raise StageError("playwright_chromium_layout_invalid")
    with tempfile.TemporaryDirectory(prefix="ecorex-browser-stage-") as raw:
        runtime = Path(raw) / "runtime"
        python_root = runtime / "python"
        python_root.mkdir(parents=True)
        inventory = _copy_distribution_closure((distribution.metadata["Name"],), python_root)
        target_browser = runtime / "browser" / browser_root.name
        _copy_tree(browser_root, target_browser, excluded=frozenset({"__pycache__"}))
        relative_executable = (
            PurePosixPath("browser")
            / browser_root.name
            / executable.relative_to(browser_root).as_posix()
        ).as_posix()
        records = _tree_records(runtime)
        archive = pack / "browser-runtime.zip"
        _write_zip(runtime, archive)
        descriptor = {
            "schema_version": 1,
            "archive_sha256": _sha256(archive),
            "browser_executable": relative_executable,
            "files": records,
        }
        (pack / "browser-runtime.json").write_text(
            json.dumps(descriptor, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
            newline="\n",
        )
    return inventory


def _core_gates(
    core: Path,
    *,
    platform: str,
    architecture: str,
    interpreter: Path,
    interpreter_identity: Mapping[str, Any],
    distributions: tuple[dict[str, str], ...],
    config_digest: str,
    web: Mapping[str, Any],
    evidence: Path,
) -> None:
    launcher = core / "bin" / ("ecorex.exe" if platform == "windows" else "ecorex")
    launch = _run(
        (str(launcher), "--help"),
        cwd=core,
        environment=_runtime_environment(),
        timeout=30,
        code="runtime_launch_probe_failed",
    )
    cli_help = _run(
        (str(interpreter), "-I", "-B", "-m", "ecorex.server", "--help"),
        cwd=core,
        environment=_runtime_environment(),
        timeout=30,
        code="runtime_launch_probe_failed",
    )
    if b"serve" not in cli_help.stdout:
        raise StageError("runtime_launch_probe_failed")
    _gate(
        evidence,
        "runtime-launch",
        {
            "launcher_sha256": _sha256(launcher),
            "exit_code": launch.returncode,
            "cli_help_sha256": hashlib.sha256(cli_help.stdout).hexdigest(),
        },
    )
    health = _run(
        (
            str(interpreter),
            "-I",
            "-B",
            str(ROOT / "platform-staging" / "probes" / "loopback_health.py"),
        ),
        cwd=core,
        environment=_runtime_environment(),
        timeout=30,
        code="loopback_health_probe_failed",
    )
    try:
        health_value = json.loads(health.stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise StageError("loopback_health_probe_failed") from None
    _gate(evidence, "loopback-health", {"probe": health_value})
    _gate(
        evidence,
        "dependency-closure",
        {
            "platform": platform,
            "architecture": architecture,
            "runtime_config_sha256": config_digest,
            "interpreter": dict(interpreter_identity),
            "web_bundle": dict(web),
            "distributions": list(distributions),
            "dependency_lock": _locked_inventory_evidence(
                distributions,
                profile="runtime",
                require_complete=True,
            ),
        },
    )
    _gate(
        evidence,
        "supply-chain",
        _supply_chain(
            core,
            distributions,
            lock_profile="runtime",
            require_complete=True,
        ),
    )


def _browser_gates(
    pack: Path,
    *,
    interpreter: Path,
    inventory: tuple[dict[str, str], ...],
    evidence: Path,
) -> None:
    zipapp = _temporary_zipapp(pack)
    try:
        descriptor = _read_canonical_process_pack_descriptor(
            pack,
            pack_id="browser",
        )
        _gate(evidence, "pack-contract", {"descriptor": descriptor, "zipapp_sha256": _sha256(zipapp)})
        request = _pack_request(
            "browser",
            "cdp",
            {
                "operation": "snapshot",
                "target": "data:text/html,<title>EcoreX Stage</title><body>ecorex-stage-ready</body>",
                "parameters": {"timeout_ms": 20_000},
            },
        )
        response = _invoke_zipapp(interpreter, zipapp, request, timeout=60)
        if response.get("status") != "completed" or "ecorex-stage-ready" not in str(response.get("result")):
            raise StageError("browser_pack_smoke_failed")
        _gate(evidence, "browser-smoke", {"response_sha256": _json_sha256(response)})
        _gate(
            evidence,
            "process-isolation",
            {
                "parent_environment_allowlisted": True,
                "fixed_playwright_lifecycle": True,
                "arbitrary_evaluate_disabled": True,
            },
        )
        _gate(
            evidence,
            "supply-chain",
            _supply_chain(
                pack,
                inventory,
                lock_profile="platform-stage",
                require_complete=False,
            ),
        )
    finally:
        zipapp.unlink(missing_ok=True)


def _image_gates(pack: Path, *, interpreter: Path, evidence: Path) -> None:
    zipapp = _temporary_zipapp(pack)
    try:
        descriptor = json.loads((pack / "ecorex-image-pack.json").read_text(encoding="utf-8"))
        _gate(evidence, "pack-contract", {"descriptor": descriptor, "zipapp_sha256": _sha256(zipapp)})
        describe = {
            "schema_version": 1,
            "protocol": "ecorex-managed-image-bridge-v1",
            "request_id": "stage-image-describe",
            "operation": "describe",
        }
        response = _invoke_zipapp(interpreter, zipapp, describe, timeout=10)
        if response.get("status") != "completed" or response.get("provider_execution") is not False:
            raise StageError("image_adapter_smoke_failed")
        _gate(evidence, "image-adapter-smoke", {"response_sha256": _json_sha256(response)})
        denied = {**describe, "operation": "execute", "request_id": "stage-image-provider-deny"}
        failure = _invoke_zipapp(interpreter, zipapp, denied, timeout=10)
        if failure.get("error_code") != "managed_image_core_required":
            raise StageError("image_provider_boundary_failed")
        _gate(evidence, "provider-failure", {"response_sha256": _json_sha256(failure)})
        _gate(
            evidence,
            "supply-chain",
            _supply_chain(
                pack,
                (),
                lock_profile="runtime",
                require_complete=False,
            ),
        )
    finally:
        zipapp.unlink(missing_ok=True)


def _channels_gates(
    pack: Path,
    *,
    inventory: tuple[dict[str, str], ...],
    evidence: Path,
) -> None:
    validated_inventory = _validate_dependency_pack(
        pack,
        pack_id="channels",
        distributions=inventory,
    )
    descriptor = json.loads(
        (pack / "ecorex-dependency-pack.json").read_text(encoding="utf-8")
    )
    contracts = json.loads(
        (pack / "connector-contracts.json").read_text(encoding="utf-8")
    )
    connectors = contracts.get("connectors") if isinstance(contracts, dict) else None
    if (
        descriptor.get("services") != list(PACK_SERVICES["channels"])
        or not isinstance(connectors, list)
        or [item.get("connector_id") for item in connectors]
        != ["feishu", "tencent-docs"]
        or any(item.get("maturity") != "stable" for item in connectors)
    ):
        raise StageError("channel_pack_contract_invalid")
    _gate(
        evidence,
        "pack-contract",
        {
            "descriptor": descriptor,
            "payload_sha256": validated_inventory["payload_sha256"],
        },
    )
    _gate(
        evidence,
        "connector-contract",
        {"connector_ids": [item["connector_id"] for item in connectors]},
    )
    _gate(
        evidence,
        "schema-smoke",
        {
            "contract_sha256": _sha256(pack / "connector-contracts.json"),
            "result_transport": contracts.get("result_transport"),
        },
    )
    _gate(
        evidence,
        "supply-chain",
        _supply_chain(
            pack,
            inventory,
            lock_profile="platform-stage",
            require_complete=False,
        ),
    )


def _dependency_runtime_gates(
    pack: Path,
    *,
    pack_id: str,
    interpreter: Path,
    inventory: tuple[dict[str, str], ...],
    evidence: Path,
) -> None:
    probe_error = (
        "ocr_runtime_probe_failed" if pack_id == "ocr" else "office_format_probe_failed"
    )
    validated_inventory = _validate_dependency_pack(
        pack,
        pack_id=pack_id,
        distributions=inventory,
    )
    descriptor = json.loads(
        (pack / "ecorex-dependency-pack.json").read_text(encoding="utf-8")
    )
    expected_services = list(PACK_SERVICES[pack_id])
    if descriptor.get("services") != expected_services:
        raise StageError("dependency_pack_contract_invalid")
    _gate(
        evidence,
        "pack-contract",
        {
            "descriptor": descriptor,
            "payload_sha256": validated_inventory["payload_sha256"],
        },
    )
    probe = _run(
        (
            str(interpreter),
            "-I",
            "-B",
            str(ROOT / "platform-staging" / "probes" / "dependency_pack_probe.py"),
            pack_id,
            str(pack),
        ),
        cwd=pack,
        environment=_runtime_environment(),
        timeout=180,
        code=probe_error,
    )
    try:
        probe_value = json.loads(probe.stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise StageError(probe_error) from None
    if (
        not isinstance(probe_value, dict)
        or probe_value.get("schema_version") != 1
        or probe_value.get("pack_id") != pack_id
        or not isinstance(probe_value.get("result"), dict)
    ):
        raise StageError(probe_error)
    _validate_dependency_probe(pack_id, probe_value)
    smoke_gate = "ocr-runtime-smoke" if pack_id == "ocr" else "office-format-smoke"
    closure_gate = "model-closure" if pack_id == "ocr" else "format-closure"
    _gate(evidence, smoke_gate, {"probe": probe_value})
    _gate(
        evidence,
        closure_gate,
        {
            "distributions": list(inventory),
            "inventory_sha256": _sha256(pack / "runtime-inventory.json"),
        },
    )
    _gate(
        evidence,
        "supply-chain",
        _supply_chain(
            pack,
            inventory,
            lock_profile="platform-stage",
            require_complete=False,
        ),
    )


def _validate_dependency_probe(pack_id: str, value: Mapping[str, Any]) -> None:
    isolation = value.get("isolation")
    result = value.get("result")
    origins = isolation.get("module_origins") if isinstance(isolation, Mapping) else None
    expected_modules = (
        {"rapidocr_onnxruntime", "onnxruntime", "numpy", "PIL", "cv2", "pyclipper"}
        if pack_id == "ocr"
        else {"docx", "openpyxl", "pptx", "pypdf", "reportlab"}
    )
    if (
        not isinstance(isolation, Mapping)
        or isolation.get("mode") != "pack-only-third-party"
        or not isinstance(origins, Mapping)
        or set(origins) != expected_modules
        or any(
            not isinstance(path, str)
            or not path
            or Path(path).is_absolute()
            or ".." in PurePosixPath(path).parts
            for path in origins.values()
        )
        or not isinstance(result, Mapping)
    ):
        raise StageError(f"{pack_id}_runtime_probe_failed")
    if pack_id == "ocr":
        if (
            result.get("fixture_recognized") is not True
            or isinstance(result.get("result_count"), bool)
            or not isinstance(result.get("result_count"), int)
            or result["result_count"] < 1
            or result.get("elapsed_reported") is not True
            or result.get("image_size") != [640, 180]
        ):
            raise StageError("ocr_runtime_probe_failed")
    elif pack_id == "office":
        if (
            result.get("round_trip") is not True
            or result.get("pdf_pages") != 1
            or any(
                isinstance(result.get(field), bool)
                or not isinstance(result.get(field), int)
                or result[field] < 1
                for field in (
                    "document_bytes",
                    "spreadsheet_bytes",
                    "presentation_bytes",
                    "pdf_bytes",
                )
            )
        ):
            raise StageError("office_format_probe_failed")
    else:
        raise StageError("dependency_pack_contract_invalid")


def _sandbox_gates(
    pack: Path,
    *,
    platform: str,
    architecture: str,
    interpreter: Path,
    native: Path,
    evidence: Path,
) -> None:
    del architecture
    zipapp = _temporary_zipapp(pack)
    workspace = pack.parent.parent.parent / ".sandbox-probe-workspace"
    workspace.mkdir()
    try:
        descriptor = _read_canonical_process_pack_descriptor(
            pack,
            pack_id="sandbox",
        )
        _gate(evidence, "pack-contract", {"descriptor": descriptor, "zipapp_sha256": _sha256(zipapp)})
        if platform == "windows":
            helper = native / "ecorex-sandbox-host.exe"
            probe = probe_windows_appcontainer_helper(
                helper,
                expected_sha256=_sha256(helper),
                workspace_roots=(workspace.resolve(strict=True),),
            )
        else:
            backend = MacOSSandboxExecBackend()
            probe = backend.probe(
                workspace_roots=(workspace.resolve(strict=True),),
                python_executable=interpreter,
                artifact_path=zipapp,
            )
        if not probe.complete:
            raise StageError("sandbox_boundary_probe_failed")
        _gate(evidence, "sandbox-boundary", {"probe": probe.to_dict()})
        _gate(
            evidence,
            "process-tree",
            {
                "backend_id": probe.backend_id,
                "process_tree_contained": probe.process_tree_contained,
            },
        )
        _gate(
            evidence,
            "supply-chain",
            _supply_chain(
                pack,
                (),
                lock_profile="runtime",
                require_complete=False,
            ),
        )
    finally:
        zipapp.unlink(missing_ok=True)
        shutil.rmtree(workspace, ignore_errors=True)


def _pack_request(pack_id: str, tool_id: str, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
    return {
        "schema_version": 1,
        "protocol": "ecorex-stdio-tool-v1",
        "request_id": f"stage-{pack_id}-{tool_id}",
        "pack_id": pack_id,
        "tool_id": tool_id,
        "arguments": dict(arguments),
        "context": {
            "policy_snapshot_id": "stage-policy",
            "capability_snapshot_id": "stage-capability",
            "idempotency_key": None,
            "approved": True,
            "effective_sandbox": "workspace-write",
            "workspace_roots": [str(ROOT)],
            "sandbox_contract": None,
            "execution_scope": None,
        },
    }


def _invoke_zipapp(interpreter: Path, zipapp: Path, request: Mapping[str, Any], *, timeout: int) -> Mapping[str, Any]:
    # Mirror the product Runtime's per-invocation TEMP ownership.  A Windows
    # child cannot unlink a native module while it is mapped, but after _run
    # reaps that child the parent can remove the complete private temp domain.
    with tempfile.TemporaryDirectory(prefix="ecorex-pack-probe-call-") as raw:
        environment = _runtime_environment()
        environment.update({"TEMP": raw, "TMP": raw})
        result = _run(
            (str(interpreter), "-I", "-B", str(zipapp)),
            cwd=ROOT,
            environment=environment,
            input_bytes=json.dumps(request, sort_keys=True, separators=(",", ":")).encode("utf-8"),
            timeout=timeout,
            code="capability_pack_probe_failed",
        )
    try:
        value = json.loads(result.stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        raise StageError("capability_pack_probe_failed") from None
    if not isinstance(value, Mapping):
        raise StageError("capability_pack_probe_failed")
    return value


def _temporary_zipapp(source: Path) -> Path:
    handle, raw = tempfile.mkstemp(prefix="ecorex-pack-probe-", suffix=".pyz")
    os.close(handle)
    path = Path(raw)
    path.unlink()
    _write_zip(source, path)
    return path


def _copy_distribution_closure(
    roots: Iterable[str],
    destination: Path,
) -> tuple[dict[str, str], ...]:
    destination.mkdir(parents=True, exist_ok=True)
    purelib = Path(sysconfig.get_path("purelib")).resolve(strict=True)
    platlib = Path(sysconfig.get_path("platlib")).resolve(strict=True)
    site_roots = tuple(dict.fromkeys((purelib, platlib)))
    pending = sorted({str(root) for root in roots}, key=canonicalize_name)
    observed: set[str] = set()
    inventory: list[dict[str, str]] = []
    while pending:
        requested = pending.pop(0)
        canonical = canonicalize_name(requested)
        if canonical in observed or canonical == "ecorex-agent-runtime":
            continue
        try:
            distribution = importlib_metadata.distribution(requested)
        except importlib_metadata.PackageNotFoundError:
            raise StageError("python_dependency_closure_incomplete") from None
        observed.add(canonical)
        name = str(distribution.metadata.get("Name") or requested)
        license_value = _distribution_license(distribution.metadata)
        if not license_value or re.search(r"(?:^|[^A-Z])(?:AGPL|GPL|SSPL)(?:[- .0-9]|$)", license_value, re.I):
            raise StageError("python_dependency_license_rejected")
        inventory.append(
            {"name": name, "version": distribution.version, "license": license_value}
        )
        try:
            distribution_root = Path(distribution.locate_file("")).resolve(strict=True)
        except OSError:
            raise StageError("python_dependency_root_invalid") from None
        distribution_roots = tuple(dict.fromkeys((*site_roots, distribution_root)))
        copied_files = 0
        for file in distribution.files or ():
            source = Path(distribution.locate_file(file))
            try:
                resolved = source.resolve(strict=True)
            except OSError:
                raise StageError("python_dependency_file_missing") from None
            relative = None
            for root in distribution_roots:
                try:
                    relative = resolved.relative_to(root)
                    break
                except ValueError:
                    continue
            if relative is None or source.name == "__pycache__" or source.suffix in {".pyc", ".pyo"}:
                continue
            if resolved.is_file():
                _copy_regular(resolved, destination / relative)
                copied_files += 1
        if copied_files < 1:
            raise StageError("python_dependency_closure_incomplete")
        for raw_requirement in distribution.requires or ():
            try:
                requirement = Requirement(raw_requirement)
                if requirement.marker is not None and not requirement.marker.evaluate({"extra": ""}):
                    continue
            except (InvalidRequirement, ValueError):
                raise StageError("python_dependency_metadata_invalid") from None
            dependency = canonicalize_name(requirement.name)
            if dependency not in observed and dependency not in {canonicalize_name(item) for item in pending}:
                pending.append(requirement.name)
        pending.sort(key=canonicalize_name)
    inventory.sort(key=lambda item: canonicalize_name(item["name"]))
    return tuple(inventory)


def _distribution_license(metadata: Message) -> str:
    value = metadata.get("License-Expression") or metadata.get("License")
    if isinstance(value, str) and value.strip() and value.strip().casefold() not in {"unknown", "n/a"}:
        return value.strip()[:512]
    for classifier in metadata.get_all("Classifier") or ():
        prefix = "License :: OSI Approved :: "
        if classifier.startswith(prefix):
            return classifier.removeprefix(prefix)[:512]
    overrides = {"fastapi": "MIT", "playwright": "Apache-2.0"}
    return overrides.get(canonicalize_name(str(metadata.get("Name") or "")), "")


def _supply_chain(
    root: Path,
    distributions: Iterable[Mapping[str, str]],
    *,
    lock_profile: str,
    require_complete: bool,
) -> Mapping[str, Any]:
    distribution_records = tuple(dict(item) for item in distributions)
    records = _tree_records(root)
    for record in records:
        path = root.joinpath(*PurePosixPath(record["path"]).parts)
        if path.suffix.casefold() == ".zip":
            _scan_archive_secrets(path)
        elif path.stat().st_size <= 4 * 1024 * 1024:
            payload = path.read_bytes()
            if any(pattern.search(payload) for pattern in _SECRET_PATTERNS):
                raise StageError("stage_supply_chain_secret_match")
    return {
        "tree_sha256": hashlib.sha256(
            json.dumps(records, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest(),
        "file_count": len(records),
        "size_bytes": sum(record["size_bytes"] for record in records),
        "distributions": list(distribution_records),
        "dependency_lock": _locked_inventory_evidence(
            distribution_records,
            profile=lock_profile,
            require_complete=require_complete,
        ),
        "secret_scan": "passed",
    }


def _scan_archive_secrets(path: Path) -> None:
    try:
        with zipfile.ZipFile(path) as archive:
            members = archive.infolist()
            if len(members) > 50_000:
                raise StageError("stage_supply_chain_archive_invalid")
            seen: set[str] = set()
            total = 0
            for member in members:
                original = member.filename
                normalized = original.replace("\\", "/")
                relative = PurePosixPath(normalized)
                mode = member.external_attr >> 16
                file_type = stat.S_IFMT(mode)
                canonical = relative.as_posix()
                expected = f"{canonical}/" if member.is_dir() else canonical
                collision = canonical.casefold()
                if (
                    not normalized
                    or original != normalized
                    or normalized != expected
                    or relative.is_absolute()
                    or any(part in {"", ".", ".."} or ":" in part for part in relative.parts)
                    or collision in seen
                    or member.flag_bits & 0x1
                    or stat.S_ISLNK(mode)
                    or file_type not in {0, stat.S_IFREG, stat.S_IFDIR}
                ):
                    raise StageError("stage_supply_chain_archive_invalid")
                seen.add(collision)
                if member.is_dir():
                    continue
                total += member.file_size
                if total > 1024 * 1024 * 1024:
                    raise StageError("stage_supply_chain_archive_invalid")
                if member.file_size <= 4 * 1024 * 1024:
                    payload = archive.read(member)
                    if any(pattern.search(payload) for pattern in _SECRET_PATTERNS):
                        raise StageError("stage_supply_chain_secret_match")
    except StageError:
        raise
    except (OSError, zipfile.BadZipFile, RuntimeError):
        raise StageError("stage_supply_chain_archive_invalid") from None


def _locked_inventory_evidence(
    distributions: Iterable[Mapping[str, str]],
    *,
    profile: str,
    require_complete: bool,
) -> Mapping[str, Any]:
    try:
        lock_set = load_dependency_lock_manifest(
            ROOT / "requirements" / "locks" / "manifest.json"
        )
    except DependencyLockError:
        raise StageError("python_dependency_lock_invalid") from None
    profile_record = lock_set.profiles.get(profile)
    if profile_record is None:
        raise StageError("python_dependency_lock_invalid")
    versions = _active_lock_versions(
        lock_set.path.parent / profile_record["lock"]
    )
    observed: dict[str, str] = {}
    for raw in distributions:
        name = canonicalize_name(str(raw.get("name") or ""))
        version = str(raw.get("version") or "")
        if not name or not version or name in observed or versions.get(name) != version:
            raise StageError("python_dependency_lock_mismatch")
        observed[name] = version
    if require_complete and observed != versions:
        raise StageError("python_dependency_lock_mismatch")
    return {
        "manifest_sha256": lock_set.sha256,
        "profile": profile,
        "profile_lock_sha256": profile_record["lock_sha256"],
        "inventory_mode": "complete" if require_complete else "subset",
        "package_count": len(observed),
    }


def _active_lock_versions(path: Path) -> dict[str, str]:
    entries: list[str] = []
    pending = ""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        raise StageError("python_dependency_lock_invalid") from None
    for raw in lines:
        stripped = raw.strip()
        if not stripped or (stripped.startswith("#") and not pending):
            continue
        continued = stripped.endswith("\\")
        if continued:
            stripped = stripped[:-1].strip()
        pending = f"{pending} {stripped}".strip()
        if not continued:
            entries.append(pending)
            pending = ""
    if pending or not entries:
        raise StageError("python_dependency_lock_invalid")
    versions: dict[str, str] = {}
    for entry in entries:
        try:
            requirement = Requirement(entry.split(" --hash=", 1)[0].strip())
            if requirement.marker is not None and not requirement.marker.evaluate(
                {"extra": ""}
            ):
                continue
        except (InvalidRequirement, ValueError):
            raise StageError("python_dependency_lock_invalid") from None
        specifiers = tuple(requirement.specifier)
        name = canonicalize_name(requirement.name)
        if (
            requirement.url is not None
            or len(specifiers) != 1
            or specifiers[0].operator != "=="
            or name in versions
        ):
            raise StageError("python_dependency_lock_invalid")
        versions[name] = specifiers[0].version
    return versions


def _gate(root: Path, gate: str, details: Mapping[str, Any]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    value = {"schema_version": 1, "status": "passed", "gate": gate, "details": dict(details)}
    (root / f"{gate}.json").write_text(
        json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _tree_records(root: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix().casefold()):
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode) or bool(
            getattr(metadata, "st_file_attributes", 0)
            & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        ):
            raise StageError("stage_tree_link_refused")
        if path.is_dir():
            continue
        if not path.is_file() or path.stat().st_size > _MAX_FILE_BYTES:
            raise StageError("stage_tree_file_invalid")
        relative = path.relative_to(root).as_posix()
        collision = relative.casefold()
        if collision in seen:
            raise StageError("stage_tree_path_collision")
        seen.add(collision)
        records.append(
            {
                "path": relative,
                "size_bytes": path.stat().st_size,
                "sha256": _sha256(path),
                "mode": 0o755 if path.stat().st_mode & stat.S_IXUSR else 0o644,
            }
        )
    if not records:
        raise StageError("stage_tree_empty")
    return records


def _write_zip(source: Path, destination: Path) -> None:
    records = _tree_records(source)
    with zipfile.ZipFile(destination, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for record in records:
            info = zipfile.ZipInfo(record["path"], date_time=_FIXED_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | int(record["mode"])) << 16
            archive.writestr(info, source.joinpath(*PurePosixPath(record["path"]).parts).read_bytes())


def _copy_tree(source: Path, destination: Path, *, excluded: frozenset[str] = frozenset()) -> None:
    source = source.resolve(strict=True)
    for path in sorted(source.rglob("*"), key=lambda item: item.as_posix().casefold()):
        relative = path.relative_to(source)
        if any(part in excluded for part in relative.parts) or path.suffix in {".pyc", ".pyo"}:
            continue
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode) or bool(
            getattr(metadata, "st_file_attributes", 0)
            & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        ):
            resolved = path.resolve(strict=True)
            if resolved.is_dir():
                continue
            _copy_regular(resolved, destination / relative, executable=bool(resolved.stat().st_mode & stat.S_IXUSR))
        elif path.is_dir():
            (destination / relative).mkdir(parents=True, exist_ok=True)
        elif path.is_file():
            _copy_regular(path, destination / relative, executable=bool(metadata.st_mode & stat.S_IXUSR))
        else:
            raise StageError("stage_source_entry_invalid")


def _copy_regular(source: Path, destination: Path, *, executable: bool = False) -> None:
    # Installed distributions legitimately contain zero-byte namespace and
    # marker files. They are still content-addressed members and must not make
    # an otherwise complete runtime closure unstaggable.
    payload = _stable_bytes(
        source,
        _MAX_FILE_BYTES,
        "stage_source_file_invalid",
        minimum=0,
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        if destination.read_bytes() != payload:
            raise StageError("stage_dependency_collision")
        return
    destination.write_bytes(payload)
    destination.chmod(0o755 if executable else 0o644)


def _stable_bytes(
    path: Path,
    maximum: int,
    code: str,
    *,
    minimum: int = 1,
) -> bytes:
    try:
        before = path.lstat()
        if (
            stat.S_ISLNK(before.st_mode)
            or not stat.S_ISREG(before.st_mode)
            or not minimum <= before.st_size <= maximum
        ):
            raise StageError(code)
        with path.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            payload = stream.read(maximum + 1)
            after = os.fstat(stream.fileno())
        current = path.lstat()
    except StageError:
        raise
    except OSError:
        raise StageError(code) from None
    identity = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    if (
        (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns) != identity
        or (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) != identity
        or (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns) != identity
        or len(payload) != before.st_size
    ):
        raise StageError(code)
    return payload


def _pinned_environment_file(path_name: str, digest_name: str) -> Path:
    value = os.environ.get(path_name)
    digest = os.environ.get(digest_name)
    if not value or not digest or _SHA256.fullmatch(digest) is None:
        raise StageError("platform_stage_configuration_missing")
    path = Path(value)
    if not path.is_absolute():
        raise StageError("platform_stage_configuration_invalid")
    payload = _stable_bytes(path.resolve(strict=True), _MAX_FILE_BYTES, "platform_stage_configuration_invalid")
    if hashlib.sha256(payload).hexdigest() != digest:
        raise StageError("platform_stage_configuration_digest_mismatch")
    return path.resolve(strict=True)


def _run(
    command: tuple[str, ...],
    *,
    cwd: Path,
    environment: Mapping[str, str],
    timeout: int,
    code: str,
    input_bytes: bytes | None = None,
) -> BoundedProcessResult:
    try:
        result = run_bounded_process(
            command,
            payload=input_bytes,
            cwd=cwd,
            environment=environment,
            timeout_seconds=timeout,
            max_stdout_bytes=4 * 1024 * 1024,
            max_stderr_bytes=1024 * 1024,
        )
    except (OSError, BoundedProcessError):
        raise StageError(code) from None
    if result.returncode != 0:
        raise StageError(code)
    return result


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
            # An empty TZ search path forces zoneinfo to use the signed
            # tzdata wheel rather than host-specific system files.
            "PYTHONTZPATH": "",
            "PYTHONUTF8": "1",
        }
    )
    return result


def _build_environment() -> Mapping[str, str]:
    result = dict(os.environ)
    result.update({"SOURCE_DATE_EPOCH": "0", "PYTHONHASHSEED": "0", "PYTHONDONTWRITEBYTECODE": "1"})
    return result


def _git_commit(repository: Path) -> str:
    git = shutil.which("git")
    if not git:
        raise StageError("platform_stage_git_unavailable")
    try:
        result = run_bounded_process(
            (git, "-C", str(repository), "rev-parse", "HEAD"),
            payload=None,
            cwd=repository,
            environment=_runtime_environment(),
            timeout_seconds=10,
            max_stdout_bytes=128,
            max_stderr_bytes=4096,
        )
    except (OSError, BoundedProcessError):
        raise StageError("platform_stage_git_unavailable") from None
    value = result.stdout.decode("ascii", errors="ignore").strip()
    if result.returncode != 0 or _COMMIT.fullmatch(value) is None:
        raise StageError("platform_stage_git_unavailable")
    return value


def _host_target() -> tuple[str, str]:
    machine = os.environ.get("PROCESSOR_ARCHITECTURE", "").casefold()
    if os.name == "nt":
        return "windows", "x64" if machine in {"amd64", "x86_64"} else "unsupported"
    if sys.platform == "darwin":
        import platform as platform_module

        normalized = platform_module.machine().casefold()
        return "macos", "arm64" if normalized in {"arm64", "aarch64"} else "x64" if normalized in {"x86_64", "amd64"} else "unsupported"
    return sys.platform, "unsupported"


def _windows_system_root() -> Path:
    if os.name != "nt":
        raise StageError("windows_native_toolchain_unavailable")
    import ctypes

    buffer = ctypes.create_unicode_buffer(32_768)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    function = kernel32.GetSystemWindowsDirectoryW
    function.argtypes = (ctypes.c_wchar_p, ctypes.c_uint)
    function.restype = ctypes.c_uint
    length = function(buffer, len(buffer))
    if length <= 0 or length >= len(buffer):
        raise StageError("windows_native_toolchain_unavailable")
    root = Path(buffer.value)
    try:
        metadata = root.lstat()
    except OSError:
        raise StageError("windows_native_toolchain_unavailable") from None
    reparse = getattr(metadata, "st_file_attributes", 0) & getattr(
        stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400
    )
    if not stat.S_ISDIR(metadata.st_mode) or reparse:
        raise StageError("windows_native_toolchain_unavailable")
    return root.resolve(strict=True)


def _absolute_directory(value: Any, code: str) -> Path:
    try:
        path = Path(value)
        if not path.is_absolute():
            raise OSError
        metadata = path.lstat()
        resolved = path.resolve(strict=True)
    except (TypeError, OSError, ValueError):
        raise StageError(code) from None
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise StageError(code)
    return resolved


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _json_sha256(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _reject_constant(_value: str) -> Any:
    raise ValueError("non-finite JSON")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


if __name__ == "__main__":
    raise SystemExit(main())
