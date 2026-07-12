from __future__ import annotations

import hashlib
import inspect
import json
import os
from pathlib import Path
from email.message import Message
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import time
import zipfile
import runpy
from types import SimpleNamespace

import pytest

import ecorex.integration.pack_python as pack_python_module
import ecorex.integration.sandbox as sandbox_module
from ecorex.integration.pack_python import (
    PackPythonError,
    build_pack_python_manifest,
    resolve_pack_python,
)
from ecorex.integration.pack_process import ProcessCapabilityPackAdapter


ROOT = Path(__file__).resolve().parents[2]
PACKS = ROOT / "release" / "capability-packs"


def _pack_python(payload: Path, *, platform: str = "windows") -> Path:
    relative = (
        Path("bin/pack-python/python.exe")
        if platform == "windows"
        else Path("bin/pack-python/bin/python3")
    )
    interpreter = payload / relative
    interpreter.parent.mkdir(parents=True)
    interpreter.write_bytes(Path(sys.executable).read_bytes())
    interpreter.chmod(0o755)
    architecture = "x64"
    (payload / "pack-python.json").write_bytes(
        build_pack_python_manifest(
            payload,
            platform=platform,
            architecture=architecture,
        )
    )
    return interpreter


def _zipapp(tmp_path: Path, pack_id: str) -> Path:
    source = tmp_path / f"{pack_id}-source"
    shutil.copytree(PACKS / pack_id, source)
    if pack_id in {"browser", "sandbox"}:
        shutil.copy2(
            PACKS / "common" / "ecorex_pack_protocol.py",
            source / "ecorex_pack_protocol.py",
        )
    artifact = tmp_path / f"{pack_id}.pyz"
    with zipfile.ZipFile(artifact, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(source.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(source).as_posix())
    return artifact


def _invoke(artifact: Path, request: dict) -> dict:
    result = subprocess.run(
        (sys.executable, "-I", "-B", str(artifact)),
        input=json.dumps(request, sort_keys=True, separators=(",", ":")).encode(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    return json.loads(result.stdout)


def _request(pack_id: str, tool_id: str, arguments: dict, workspace: Path) -> dict:
    return {
        "schema_version": 1,
        "protocol": "ecorex-stdio-tool-v1",
        "request_id": f"test-{pack_id}-{tool_id}",
        "pack_id": pack_id,
        "tool_id": tool_id,
        "arguments": arguments,
        "context": {
            "policy_snapshot_id": "policy-test",
            "capability_snapshot_id": "capability-test",
            "idempotency_key": "test-operation",
            "approved": True,
            "effective_sandbox": "danger-full-access",
            "workspace_roots": [str(workspace)],
            "sandbox_contract": None,
            "execution_scope": None,
        },
    }


def _shell_script_command(
    root: Path,
    name: str,
    script: str,
    *arguments: str,
) -> str:
    script_path = root / name
    script_path.write_text(script, encoding="utf-8")
    argv = (sys.executable, "-I", str(script_path), *arguments)
    return subprocess.list2cmdline(argv) if os.name == "nt" else shlex.join(argv)


def _sandbox_shell_request(
    workspace: Path,
    command: str,
    *,
    timeout_seconds: int = 5,
) -> dict:
    request = _request(
        "sandbox",
        "shell",
        {"command": command, "timeout_seconds": timeout_seconds},
        workspace,
    )
    roots_digest = hashlib.sha256(str(workspace.resolve()).encode()).hexdigest()
    contract = {
        "profile": "danger-full-access",
        "backend_id": "unit-job-owner",
        "os_enforced": False,
        "workspace_roots_sha256": roots_digest,
        "filesystem_read_scope": "host-unrestricted",
        "filesystem_write_scope": "host-unrestricted",
        "network_scope": "host-unrestricted",
        "process_tree_scope": "contained-inherited",
        "timeout_seconds": float(max(10, timeout_seconds + 5)),
        "stdout_limit_bytes": 4 * 1024 * 1024,
        "stderr_limit_bytes": 64 * 1024,
    }
    contract["contract_id"] = "sandbox_" + hashlib.sha256(
        json.dumps(contract, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    request["context"]["sandbox_contract"] = contract
    return request


def _process_is_active(pid: int) -> bool:
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
    import ctypes

    handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
    if not handle:
        return False
    try:
        exit_code = ctypes.c_ulong()
        if not ctypes.windll.kernel32.GetExitCodeProcess(
            handle, ctypes.byref(exit_code)
        ):
            return False
        return exit_code.value == 259
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)


def _force_kill(pid: int) -> None:
    if os.name == "nt":
        subprocess.run(
            ("taskkill", "/PID", str(pid), "/T", "/F"),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        return
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def test_pack_python_has_no_sys_executable_or_path_fallback(tmp_path: Path) -> None:
    payload = tmp_path / "payload"
    payload.mkdir()
    with pytest.raises(PackPythonError) as raised:
        resolve_pack_python(payload, platform="windows", architecture="x64")
    assert raised.value.code == "pack_python_manifest_invalid"
    interpreter_parameter = inspect.signature(
        ProcessCapabilityPackAdapter.__init__
    ).parameters["python_executable"]
    assert interpreter_parameter.default is inspect.Parameter.empty


def test_pack_python_manifest_binds_interpreter_and_complete_closure(tmp_path: Path) -> None:
    payload = tmp_path / "payload"
    payload.mkdir()
    interpreter = _pack_python(payload)
    resolved, identity = resolve_pack_python(
        payload,
        platform="windows",
        architecture="x64",
    )
    assert resolved == interpreter.resolve()
    assert identity.sha256 == hashlib.sha256(interpreter.read_bytes()).hexdigest()
    (interpreter.parent / "injected.py").write_text("pass\n", encoding="utf-8")
    with pytest.raises(PackPythonError) as changed:
        resolve_pack_python(payload, platform="windows", architecture="x64")
    assert changed.value.code == "pack_python_closure_mismatch"


def test_pack_python_closure_binds_legitimate_zero_byte_markers(tmp_path: Path) -> None:
    payload = tmp_path / "payload"
    interpreter = payload / "bin" / "pack-python" / "python.exe"
    interpreter.parent.mkdir(parents=True)
    interpreter.write_bytes(b"product-python")
    marker = interpreter.parent / "namespace-marker.pth"
    marker.write_bytes(b"")
    manifest = json.loads(
        build_pack_python_manifest(
            payload,
            platform="windows",
            architecture="x64",
        )
    )
    assert manifest["closure_file_count"] == 2
    assert manifest["closure_size_bytes"] == len(b"product-python")
    (payload / "pack-python.json").write_text(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    _resolved, identity = resolve_pack_python(
        payload,
        platform="windows",
        architecture="x64",
    )
    assert identity.closure_sha256 == manifest["closure_sha256"]

    marker.write_bytes(b"changed")
    with pytest.raises(PackPythonError) as changed:
        resolve_pack_python(payload, platform="windows", architecture="x64")
    assert changed.value.code == "pack_python_closure_mismatch"


def test_pack_python_zero_byte_exception_does_not_relax_entry_or_size_bounds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = tmp_path / "directory-entry"
    interpreter = payload / "bin" / "pack-python" / "python.exe"
    interpreter.mkdir(parents=True)
    with pytest.raises(PackPythonError) as directory:
        build_pack_python_manifest(payload, platform="windows", architecture="x64")
    assert directory.value.code == "pack_python_interpreter_invalid"

    closure = tmp_path / "oversize-closure"
    closure.mkdir()
    (closure / "empty.marker").write_bytes(b"")
    (closure / "large.bin").write_bytes(b"12345")
    monkeypatch.setattr(pack_python_module, "MAX_CLOSURE_BYTES", 4)
    with pytest.raises(PackPythonError) as oversized:
        pack_python_module.scan_pack_python_closure(closure)
    assert oversized.value.code == "pack_python_closure_invalid"


def test_pack_python_zero_byte_member_still_has_toctou_identity_fence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    closure = tmp_path / "closure"
    closure.mkdir()
    target = closure / "marker.pth"
    target.write_bytes(b"")
    original_open = Path.open

    class RacingStream:
        def __init__(self, stream) -> None:
            self.stream = stream

        def __enter__(self):
            self.stream.__enter__()
            return self

        def __exit__(self, *args):
            return self.stream.__exit__(*args)

        def fileno(self):
            return self.stream.fileno()

        def read(self, *args):
            payload = self.stream.read(*args)
            os.utime(
                target,
                ns=(target.stat().st_atime_ns, target.stat().st_mtime_ns + 1_000_000),
            )
            return payload

    def racing_open(path: Path, *args, **kwargs):
        stream = original_open(path, *args, **kwargs)
        return RacingStream(stream) if path == target else stream

    monkeypatch.setattr(Path, "open", racing_open)
    with pytest.raises(PackPythonError) as changed:
        pack_python_module.scan_pack_python_closure(closure)
    assert changed.value.code == "pack_python_closure_invalid"


def test_pack_python_rejects_manifest_digest_change_and_symlink_escape(tmp_path: Path) -> None:
    payload = tmp_path / "payload"
    payload.mkdir()
    interpreter = _pack_python(payload)
    interpreter.write_bytes(interpreter.read_bytes() + b"changed")
    with pytest.raises(PackPythonError) as digest:
        resolve_pack_python(payload, platform="windows", architecture="x64")
    assert digest.value.code == "pack_python_interpreter_digest_mismatch"

    payload2 = tmp_path / "payload2"
    payload2.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "python.exe").write_bytes(b"outside")
    target = payload2 / "bin" / "pack-python"
    target.parent.mkdir(parents=True)
    try:
        target.symlink_to(outside, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"symlink creation is unavailable: {error}")
    with pytest.raises(PackPythonError):
        build_pack_python_manifest(payload2, platform="windows", architecture="x64")


def test_image_pack_is_only_a_managed_core_handshake(tmp_path: Path) -> None:
    artifact = _zipapp(tmp_path, "image")
    describe = _invoke(
        artifact,
        {
            "schema_version": 1,
            "protocol": "ecorex-managed-image-bridge-v1",
            "request_id": "image-describe",
            "operation": "describe",
        },
    )
    assert describe["status"] == "completed"
    assert describe["provider_execution"] is False
    assert describe["adapter"] == "core-managed-image-v1"
    denied = _invoke(
        artifact,
        {
            "schema_version": 1,
            "protocol": "ecorex-managed-image-bridge-v1",
            "request_id": "image-execute",
            "operation": "execute",
        },
    )
    assert denied["status"] == "failed"
    assert denied["error_code"] == "managed_image_core_required"


def test_browser_pack_has_no_arbitrary_evaluate_operation(tmp_path: Path) -> None:
    artifact = _zipapp(tmp_path, "browser")
    response = _invoke(
        artifact,
        _request(
            "browser",
            "cdp",
            {
                "operation": "evaluate",
                "target": "data:text/html,<body>blocked</body>",
                "parameters": {"expression": "process.exit()"},
            },
            tmp_path,
        ),
    )
    assert response["status"] == "failed"
    assert response["error_code"] == "browser_operation_not_supported"


def test_browser_pack_guards_every_subrequest_and_denies_websockets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.syspath_prepend(str(PACKS / "common"))
    browser = runpy.run_path(str(PACKS / "browser" / "browser_pack.py"))

    class Route:
        def __init__(self, url: str) -> None:
            self.request = type("Request", (), {"url": url})()
            self.continued = False
            self.aborted = None

        def continue_(self) -> None:
            self.continued = True

        def abort(self, *, error_code: str) -> None:
            self.aborted = error_code

    private = Route("http://127.0.0.1/private")
    browser["_guard_browser_request"](private)
    assert private.aborted == "blockedbyclient"
    assert private.continued is False

    local_document = Route("data:text/html,<body>safe document</body>")
    browser["_guard_browser_request"](local_document)
    assert local_document.continued is True
    assert local_document.aborted is None

    class WebSocketRoute:
        closed = False

        def close(self) -> None:
            self.closed = True

    class Context:
        request_pattern = None
        request_handler = None
        websocket_pattern = None
        websocket_handler = None

        def route(self, pattern: str, handler) -> None:  # noqa: ANN001
            self.request_pattern = pattern
            self.request_handler = handler

        def route_web_socket(self, pattern: str, handler) -> None:  # noqa: ANN001
            self.websocket_pattern = pattern
            self.websocket_handler = handler

    context = Context()
    browser["_install_browser_network_guard"](context)
    assert context.request_pattern == "**/*"
    assert context.request_handler is browser["_guard_browser_request"]
    assert context.websocket_pattern == "**/*"
    socket_route = WebSocketRoute()
    context.websocket_handler(socket_route)
    assert socket_route.closed is True


def test_browser_runtime_manifest_rejects_escape_and_duplicate_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.syspath_prepend(str(PACKS / "common"))
    browser = runpy.run_path(str(PACKS / "browser" / "browser_pack.py"))
    archive = b"signed browser runtime"

    def encoded(*, executable: str, paths: list[str]) -> bytes:
        value = {
            "schema_version": 1,
            "archive_sha256": hashlib.sha256(archive).hexdigest(),
            "browser_executable": executable,
            "files": [
                {
                    "path": path,
                    "size_bytes": 1,
                    "sha256": "0" * 64,
                    "mode": 0o755,
                }
                for path in paths
            ],
        }
        return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()

    with pytest.raises(Exception, match="browser_runtime_manifest_invalid"):
        browser["_parse_runtime_manifest"](
            encoded(executable="../python", paths=["../python"]),
            archive,
        )
    with pytest.raises(Exception, match="browser_runtime_manifest_invalid"):
        browser["_parse_runtime_manifest"](
            encoded(executable="bin/chromium", paths=["bin/chromium", "bin/chromium"]),
            archive,
        )


def test_browser_runtime_delegates_windows_native_cleanup_to_parent_temp_domain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.syspath_prepend(str(PACKS / "common"))
    browser = runpy.run_path(str(PACKS / "browser" / "browser_pack.py"))
    outer = tmp_path / "browser.pyz"
    with zipfile.ZipFile(outer, "w") as archive:
        archive.writestr("browser-runtime.json", b"manifest")
        archive.writestr("browser-runtime.zip", b"archive")
    observed: dict[str, object] = {}

    class Temporary:
        def __init__(self, *, prefix: str, ignore_cleanup_errors: bool) -> None:
            observed.update(
                prefix=prefix,
                ignore_cleanup_errors=ignore_cleanup_errors,
            )

        def __enter__(self) -> str:
            return str(tmp_path)

        def __exit__(self, *_args) -> None:
            return None

    monkeypatch.setattr(sys, "argv", [str(outer)])
    monkeypatch.setattr(browser["tempfile"], "TemporaryDirectory", Temporary)
    runtime_globals = browser["_browser_runtime"].__wrapped__.__globals__
    monkeypatch.setitem(
        runtime_globals,
        "_parse_runtime_manifest",
        lambda _manifest, _archive: {"browser_executable": "browser/chrome.exe"},
    )
    monkeypatch.setitem(
        runtime_globals,
        "_extract_verified_runtime",
        lambda _archive, destination, _manifest: destination.mkdir(),
    )

    with browser["_browser_runtime"]() as runtime:
        assert runtime == tmp_path / "payload"
    assert observed == {
        "prefix": "ecorex-browser-runtime-",
        "ignore_cleanup_errors": os.name == "nt",
    }


def test_sandbox_pack_acknowledges_exact_core_contract_and_fixed_shell(tmp_path: Path) -> None:
    artifact = _zipapp(tmp_path, "sandbox")
    request = _request(
        "sandbox",
        "shell",
        {"command": "echo ecorex-pack-ready", "timeout_seconds": 5},
        tmp_path,
    )
    roots_digest = hashlib.sha256(str(tmp_path.resolve()).encode()).hexdigest()
    contract = {
        "profile": "danger-full-access",
        "backend_id": "unit-job-owner",
        "os_enforced": False,
        "workspace_roots_sha256": roots_digest,
        "filesystem_read_scope": "host-unrestricted",
        "filesystem_write_scope": "host-unrestricted",
        "network_scope": "host-unrestricted",
        "process_tree_scope": "contained-inherited",
        "timeout_seconds": 10.0,
        "stdout_limit_bytes": 4 * 1024 * 1024,
        "stderr_limit_bytes": 64 * 1024,
    }
    contract["contract_id"] = "sandbox_" + hashlib.sha256(
        json.dumps(contract, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    request["context"]["sandbox_contract"] = contract
    response = _invoke(artifact, request)
    assert response["status"] == "completed"
    assert response["sandbox_contract_id"] == contract["contract_id"]
    assert "ecorex-pack-ready" in response["result"]["stdout"]

    request["context"]["sandbox_contract"]["contract_id"] = "sandbox_" + "0" * 64
    rejected = _invoke(artifact, request)
    assert rejected["status"] == "failed"
    assert rejected["error_code"] == "shell_sandbox_contract_invalid"


def test_sandbox_pack_streams_and_stops_stdout_flood(tmp_path: Path) -> None:
    artifact = _zipapp(tmp_path, "sandbox")
    marker = "stdout-flood-sensitive-marker"
    command = _shell_script_command(
        tmp_path,
        "stdout-flood.py",
        "import sys;"
        f"sys.stdout.buffer.write({marker.encode()!r}+b'x'*(600*1024));"
        "sys.stdout.flush()"
    )

    response = _invoke(artifact, _sandbox_shell_request(tmp_path, command))

    assert response["status"] == "failed"
    assert response["error_code"] == "shell_output_too_large"
    assert marker not in json.dumps(response)


def test_sandbox_pack_streams_and_stops_stderr_flood_without_leaking_it(
    tmp_path: Path,
) -> None:
    artifact = _zipapp(tmp_path, "sandbox")
    marker = "stderr-flood-sensitive-marker"
    command = _shell_script_command(
        tmp_path,
        "stderr-flood.py",
        "import sys;"
        f"sys.stderr.buffer.write({marker.encode()!r}+b'x'*(48*1024));"
        "sys.stderr.flush()"
    )

    response = _invoke(artifact, _sandbox_shell_request(tmp_path, command))

    assert response["status"] == "failed"
    assert response["error_code"] == "shell_output_too_large"
    assert marker not in json.dumps(response)


def test_sandbox_pack_timeout_terminates_the_command(tmp_path: Path) -> None:
    artifact = _zipapp(tmp_path, "sandbox")
    command = _shell_script_command(
        tmp_path,
        "timeout.py",
        "import time;time.sleep(120)",
    )
    started = time.monotonic()

    response = _invoke(
        artifact,
        _sandbox_shell_request(tmp_path, command, timeout_seconds=1),
    )

    assert time.monotonic() - started < 8
    assert response["status"] == "failed"
    assert response["error_code"] == "shell_command_timeout"


def test_sandbox_pack_reaps_background_descendants_before_reply(tmp_path: Path) -> None:
    artifact = _zipapp(tmp_path, "sandbox")
    child_pid_path = tmp_path / "sandbox-child.pid"
    script = (
        "import pathlib,subprocess,sys;"
        "child=subprocess.Popen([sys.executable,'-I','-c','import time;time.sleep(120)'],"
        "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL);"
        "pathlib.Path(sys.argv[1]).write_text(str(child.pid),encoding='ascii')"
    )
    command = _shell_script_command(
        tmp_path,
        "descendant.py",
        script,
        str(child_pid_path),
    )
    child_pid: int | None = None
    try:
        response = _invoke(artifact, _sandbox_shell_request(tmp_path, command))
        child_pid = int(child_pid_path.read_text(encoding="ascii"))
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and _process_is_active(child_pid):
            time.sleep(0.02)

        assert response["status"] == "completed"
        assert response["result"]["exit_code"] == 0
        assert _process_is_active(child_pid) is False
    finally:
        if child_pid is not None and _process_is_active(child_pid):
            _force_kill(child_pid)


def test_sandbox_pack_preserves_bounded_unicode_output(tmp_path: Path) -> None:
    artifact = _zipapp(tmp_path, "sandbox")
    stdout = "你好，EcoreX。"
    stderr = "提示：已完成。"
    command = _shell_script_command(
        tmp_path,
        "unicode.py",
        "import sys;"
        f"sys.stdout.buffer.write({stdout.encode()!r});"
        f"sys.stderr.buffer.write({stderr.encode()!r})"
    )

    response = _invoke(artifact, _sandbox_shell_request(tmp_path, command))

    assert response["status"] == "completed"
    assert response["result"]["exit_code"] == 0
    assert response["result"]["stdout"] == stdout
    assert response["result"]["stderr"] == stderr


def test_windows_helper_source_contains_real_appcontainer_and_job_boundaries() -> None:
    native_root = ROOT / "platform-staging/native/windows"
    source = "\n".join(
        (native_root / name).read_text(encoding="utf-8")
        for name in (
            "ecorex_sandbox_host.cpp",
            "ecorex_sandbox_security.cpp",
            "ecorex_sandbox_process.cpp",
            "ecorex_sandbox_host_internal.h",
        )
    )
    for symbol in (
        "CreateAppContainerProfile",
        "PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES",
        "JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE",
        "AssignProcessToJobObject",
        "TerminateJobObject",
        "SetNamedSecurityInfoW",
        "BCryptHashData",
        "PrepareProbeWorkspace",
        "ecorex_sandbox_probe:child_boundary",
    ):
        assert symbol in source
    build = (ROOT / "platform-staging/native/windows/build.ps1").read_text(
        encoding="utf-8"
    )
    assert "Get-Sha256Hex" in build
    assert "Get-FileHash" not in build
    assert "/Brepro" in build
    assert "/GUARD:CF" in build
    assert "sandbox_helper_sha256" in build
    assert "toolchain-manifest.json" in build
    assert "compiler_identity_untrusted" not in build  # composed from trusted label
    assert "Resolve-TrustedTool" in build
    assert "& $linkPath" in build
    assert "ecorex_sandbox_security.cpp" in build
    assert "ecorex_sandbox_process.cpp" in build
    assert "ExpectedToolchainManifestSha256" in build
    assert "ExpectedSourceSetSha256" in build
    assert "authority_mode = 'caller-pinned'" in build
    assert "SpecialFolder]::ProgramFilesX86" in build
    assert "SpecialFolder]::Windows" in build
    assert "${env:ProgramFiles(x86)}" not in build


def test_staged_windows_helper_probe_is_behavioral_but_cannot_authorize_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    helper = tmp_path / "ecorex-sandbox-host.exe"
    helper.write_bytes(b"signed-native-helper")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    roots = (workspace.resolve(strict=True),)
    roots_digest = hashlib.sha256(str(roots[0]).encode("utf-8")).hexdigest()
    expected = {
        "backend": "windows-appcontainer",
        "cpu_rate_hard_cap": sandbox_module.WINDOWS_CPU_RATE_HARD_CAP,
        "filesystem_read_scoped": True,
        "filesystem_write_scoped": True,
        "job_memory_limit_bytes": sandbox_module.WINDOWS_JOB_MEMORY_LIMIT_BYTES,
        "network_denied": True,
        "process_memory_limit_bytes": sandbox_module.WINDOWS_PROCESS_MEMORY_LIMIT_BYTES,
        "process_tree_contained": True,
        "protocol": sandbox_module.SANDBOX_LAUNCH_PROTOCOL,
        "workspace_roots_sha256": roots_digest,
    }
    payload = json.dumps(expected, sort_keys=True, separators=(",", ":")).encode()
    observed: dict[str, object] = {}

    def run(command, *, timeout_seconds):
        observed.update(command=command, timeout_seconds=timeout_seconds)
        return SimpleNamespace(returncode=0, stdout=payload, stderr=b"")

    monkeypatch.setattr(sandbox_module, "_run_bounded_probe", run)
    helper_digest = hashlib.sha256(helper.read_bytes()).hexdigest()
    probe = sandbox_module.probe_windows_appcontainer_helper(
        helper,
        expected_sha256=helper_digest,
        workspace_roots=roots,
    )

    assert probe.complete is True
    assert observed["timeout_seconds"] == 10
    assert observed["command"][:3] == (
        str(helper.resolve()),
        "probe",
        "--protocol",
    )
    runtime = sandbox_module.WindowsAppContainerSandboxBackend(
        helper,
        expected_sha256=helper_digest,
    )
    denied = runtime.probe(
        workspace_roots=roots,
        python_executable=Path(sys.executable),
        artifact_path=helper,
    )
    assert denied.complete is False
    assert denied.reason == "windows_appcontainer_security_receipt_invalid"


def test_windows_native_receipt_is_bound_to_pinned_toolchain_and_binaries(
    tmp_path: Path,
) -> None:
    stager = runpy.run_path(str(ROOT / "platform-staging/stager.py"))
    manifest = ROOT / "platform-staging/native/windows/toolchain-manifest.json"
    manifest_value = json.loads(manifest.read_text(encoding="utf-8"))
    output = tmp_path / "native"
    output.mkdir()
    launcher = output / "ecorex.exe"
    helper = output / "ecorex-sandbox-host.exe"
    launcher.write_bytes(b"trusted-launcher-test")
    helper.write_bytes(b"trusted-helper-test")
    source_names = (
        "ecorex_launcher.cpp",
        "ecorex_sandbox_host.cpp",
        "ecorex_sandbox_security.cpp",
        "ecorex_sandbox_process.cpp",
        "ecorex_sandbox_host_internal.h",
    )
    native_root = ROOT / "platform-staging/native/windows"
    source_binding = "\0".join(
        f"{name}={hashlib.sha256((native_root / name).read_bytes()).hexdigest()}"
        for name in sorted(source_names)
    )
    library_binding = "\0".join(
        f"{name}={manifest_value['libraries'][name]}"
        for name in sorted(manifest_value["libraries"])
    )
    tools = manifest_value["tools"]
    receipt = {
        "schema_version": 2,
        "status": "passed",
        "target": "windows-x64",
        "authority_mode": "caller-pinned",
        "toolchain_manifest_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest(),
        "source_set_sha256": hashlib.sha256(source_binding.encode()).hexdigest(),
        "msvc_tools_version": manifest_value["msvc_tools_version"],
        "windows_sdk_version": manifest_value["windows_sdk_version"],
        "msvc_root_sha256": "1" * 64,
        "windows_sdk_root_sha256": "2" * 64,
        "include_roots_sha256": "3" * 64,
        "library_roots_sha256": "4" * 64,
        "library_set_sha256": hashlib.sha256(library_binding.encode()).hexdigest(),
        "compiler_sha256": tools["compiler"]["sha256"],
        "compiler_file_version": tools["compiler"]["file_version"],
        "compiler_authenticode_thumbprint": tools["compiler"][
            "authenticode_thumbprint"
        ],
        "linker_sha256": tools["linker"]["sha256"],
        "linker_file_version": tools["linker"]["file_version"],
        "linker_authenticode_thumbprint": tools["linker"][
            "authenticode_thumbprint"
        ],
        "c1xx_sha256": tools["c1xx"]["sha256"],
        "c1xx_authenticode_thumbprint": tools["c1xx"][
            "authenticode_thumbprint"
        ],
        "c2_sha256": tools["c2"]["sha256"],
        "c2_authenticode_thumbprint": tools["c2"][
            "authenticode_thumbprint"
        ],
        "runtime_launcher_sha256": hashlib.sha256(launcher.read_bytes()).hexdigest(),
        "sandbox_helper_sha256": hashlib.sha256(helper.read_bytes()).hexdigest(),
    }
    receipt_path = output / "native-build-receipt.json"
    receipt_path.write_text(
        json.dumps(receipt, separators=(",", ":")) + "\n", encoding="utf-8"
    )
    stager["_validate_windows_native_receipt"](
        output,
        toolchain_manifest=manifest,
    )

    receipt["compiler_sha256"] = "0" * 64
    receipt_path.write_text(
        json.dumps(receipt, separators=(",", ":")) + "\n", encoding="utf-8"
    )
    with pytest.raises(
        stager["StageError"], match="windows_native_build_receipt_invalid"
    ):
        stager["_validate_windows_native_receipt"](
            output,
            toolchain_manifest=manifest,
        )


@pytest.mark.skipif(os.name != "nt", reason="Windows native build contract")
def test_windows_native_system_root_ignores_process_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stager = runpy.run_path(str(ROOT / "platform-staging/stager.py"))
    monkeypatch.setenv("SYSTEMROOT", r"C:\untrusted-windows")
    monkeypatch.setenv("WINDIR", r"C:\untrusted-windows")

    trusted = stager["_windows_system_root"]()

    assert trusted.is_dir()
    assert str(trusted).casefold() != r"c:\untrusted-windows"


@pytest.mark.skipif(os.name != "nt", reason="Windows native build contract")
@pytest.mark.parametrize(
    "variable",
    ("CL", "_CL_", "LINK", "_LINK_", "LIB", "INCLUDE", "LIBPATH"),
)
def test_windows_native_build_rejects_injected_toolchain_environment_and_stale_outputs(
    tmp_path: Path,
    variable: str,
) -> None:
    output = tmp_path / variable.replace("_", "x")
    output.mkdir()
    published = (
        output / "native-build-receipt.json",
        output / "ecorex.exe",
        output / "ecorex-sandbox-host.exe",
    )
    for path in published:
        path.write_bytes(b"stale-passed-output")
    blocked = {
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
    environment = {
        key: value
        for key, value in os.environ.items()
        if key.upper() not in blocked
    }
    environment[variable] = "/untrusted"
    powershell = (
        Path(os.environ["SYSTEMROOT"])
        / "System32/WindowsPowerShell/v1.0/powershell.exe"
    )
    result = subprocess.run(
        (
            str(powershell),
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(ROOT / "platform-staging/native/windows/build.ps1"),
            "-OutputDirectory",
            str(output),
        ),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=30,
        check=False,
        env=environment,
    )
    assert result.returncode != 0
    assert not any(path.exists() for path in published)


@pytest.mark.skipif(os.name != "nt", reason="Windows native build contract")
def test_windows_native_build_clears_published_outputs_on_late_validation_failure(
    tmp_path: Path,
) -> None:
    output = tmp_path / "native"
    output.mkdir()
    published = (
        output / "native-build-receipt.json",
        output / "ecorex.exe",
        output / "ecorex-sandbox-host.exe",
    )
    for path in published:
        path.write_bytes(b"stale-passed-output")
    invalid_manifest = tmp_path / "toolchain.json"
    invalid_manifest.write_text("{}\n", encoding="utf-8")
    native_root = ROOT / "platform-staging/native/windows"
    source_names = (
        "ecorex_launcher.cpp",
        "ecorex_sandbox_host.cpp",
        "ecorex_sandbox_security.cpp",
        "ecorex_sandbox_process.cpp",
        "ecorex_sandbox_host_internal.h",
    )
    source_binding = "\0".join(
        f"{name}={hashlib.sha256((native_root / name).read_bytes()).hexdigest()}"
        for name in sorted(source_names)
    ).encode("utf-8")
    blocked = {
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
        "PSMODULEPATH",
    }
    environment = {
        key: value
        for key, value in os.environ.items()
        if key.upper() not in blocked
    }
    powershell = (
        Path(os.environ["SYSTEMROOT"])
        / "System32/WindowsPowerShell/v1.0/powershell.exe"
    )
    result = subprocess.run(
        (
            str(powershell),
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(ROOT / "platform-staging/native/windows/build.ps1"),
            "-OutputDirectory",
            str(output),
            "-ToolchainManifest",
            str(invalid_manifest),
            "-SourceDirectory",
            str(native_root),
            "-ExpectedToolchainManifestSha256",
            hashlib.sha256(invalid_manifest.read_bytes()).hexdigest(),
            "-ExpectedSourceSetSha256",
            hashlib.sha256(source_binding).hexdigest(),
        ),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=30,
        check=False,
        env=environment,
    )
    assert result.returncode != 0
    assert not any(path.exists() for path in published)


@pytest.mark.skipif(os.name != "nt", reason="Windows native build contract")
@pytest.mark.parametrize("mutation_target", ("source", "manifest"))
def test_windows_native_caller_pin_rejects_authority_mutation_before_build_lock(
    tmp_path: Path,
    mutation_target: str,
) -> None:
    native_root = ROOT / "platform-staging/native/windows"
    authority = tmp_path / "authority"
    authority.mkdir()
    source_names = (
        "ecorex_launcher.cpp",
        "ecorex_sandbox_host.cpp",
        "ecorex_sandbox_security.cpp",
        "ecorex_sandbox_process.cpp",
        "ecorex_sandbox_host_internal.h",
    )
    for name in (*source_names, "toolchain-manifest.json"):
        shutil.copyfile(native_root / name, authority / name)
    source_binding = "\0".join(
        f"{name}={hashlib.sha256((authority / name).read_bytes()).hexdigest()}"
        for name in sorted(source_names)
    ).encode("utf-8")
    expected_sources = hashlib.sha256(source_binding).hexdigest()
    manifest = authority / "toolchain-manifest.json"
    expected_manifest = hashlib.sha256(manifest.read_bytes()).hexdigest()
    output = tmp_path / "output"
    published = (
        output / "native-build-receipt.json",
        output / "ecorex.exe",
        output / "ecorex-sandbox-host.exe",
    )
    blocked = {
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
        "PSMODULEPATH",
    }
    environment = {
        key: value
        for key, value in os.environ.items()
        if key.upper() not in blocked
    }
    powershell = (
        Path(os.environ["SYSTEMROOT"])
        / "System32/WindowsPowerShell/v1.0/powershell.exe"
    )

    def quote(value: Path | str) -> str:
        return "'" + str(value).replace("'", "''") + "'"

    delayed_command = " ".join(
        (
            "Start-Sleep -Milliseconds 500; &",
            quote(native_root / "build.ps1"),
            "-OutputDirectory",
            quote(output),
            "-SourceDirectory",
            quote(authority),
            "-ToolchainManifest",
            quote(manifest),
            "-ExpectedToolchainManifestSha256",
            expected_manifest,
            "-ExpectedSourceSetSha256",
            expected_sources,
        )
    )
    process = subprocess.Popen(
        (
            str(powershell),
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            delayed_command,
        ),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=environment,
    )
    mutation_path = (
        authority / "ecorex_sandbox_host_internal.h"
        if mutation_target == "source"
        else manifest
    )
    with mutation_path.open("ab") as stream:
        stream.write(
            b"\n// benign caller-pin race test\n"
            if mutation_target == "source"
            else b"\n"
        )
        stream.flush()
        os.fsync(stream.fileno())
    stdout, _ = process.communicate(timeout=30)

    assert process.returncode != 0, stdout.decode(errors="replace")
    assert not any(path.exists() for path in published)


def test_pack_sources_and_stage_scripts_have_no_placeholder_markers() -> None:
    roots = (PACKS, ROOT / "platform-staging")
    forbidden = ("TODO", "fixture is packaging-only", "placeholder pack")
    for root in roots:
        for path in root.rglob("*"):
            if path.is_file() and path.suffix.casefold() in {".py", ".json", ".md", ".c", ".cpp", ".ps1", ".sh"}:
                text = path.read_text(encoding="utf-8")
                assert not any(marker in text for marker in forbidden), path


def test_platform_stager_binds_installed_runtime_inventory_to_hash_lock() -> None:
    stager = runpy.run_path(str(ROOT / "platform-staging" / "stager.py"))
    versions = stager["_active_lock_versions"](
        ROOT / "requirements" / "locks" / "runtime.lock"
    )
    inventory = tuple(
        {"name": name, "version": version, "license": "test"}
        for name, version in sorted(versions.items())
    )

    evidence = stager["_locked_inventory_evidence"](
        inventory,
        profile="runtime",
        require_complete=True,
    )

    assert evidence["inventory_mode"] == "complete"
    assert evidence["package_count"] == len(versions)
    assert len(evidence["manifest_sha256"]) == 64
    changed = list(inventory)
    changed[0] = {**changed[0], "version": "0.0.0"}
    with pytest.raises(stager["StageError"], match="python_dependency_lock_mismatch"):
        stager["_locked_inventory_evidence"](
            changed,
            profile="runtime",
            require_complete=True,
        )


def test_windows_gui_launcher_and_embedded_cli_are_probed_separately() -> None:
    source = (ROOT / "platform-staging" / "stager.py").read_text(encoding="utf-8")

    assert '(str(launcher), "--help")' in source
    assert (
        '(str(interpreter), "-I", "-B", "-m", "ecorex.server", "--help")'
        in source
    )
    assert 'if b"serve" not in cli_help.stdout:' in source
    assert 'if b"serve" not in launch.stdout:' not in source


def test_platform_supply_chain_scan_distinguishes_dependency_markers_from_secrets(
    tmp_path: Path,
) -> None:
    stager = runpy.run_path(str(ROOT / "platform-staging" / "stager.py"))
    (tmp_path / "ssh.py").write_bytes(
        b'_SK_START = b"-----BEGIN OPENSSH PRIVATE KEY-----"\n'
    )
    (tmp_path / "font-data.txt").write_bytes(
        b"QAHAEwAAQAAAAAAAgAHAGQAAQAAAAAAAwAaAKIAAQAAAAAABAAHAM0AAQAAAA"
    )

    evidence = stager["_supply_chain"](
        tmp_path,
        (),
        lock_profile="runtime",
        require_complete=False,
    )

    assert evidence["secret_scan"] == "passed"


@pytest.mark.parametrize(
    "payload",
    (
        b'credential="AKIAABCDEFGHIJKLMNOP"\n',
        (
            b"-----BEGIN OPENSSH PRIVATE KEY-----\n"
            b"QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVo=\n"
            b"-----END OPENSSH PRIVATE KEY-----\n"
        ),
    ),
)
def test_platform_supply_chain_scan_still_rejects_complete_secret_shapes(
    tmp_path: Path,
    payload: bytes,
) -> None:
    stager = runpy.run_path(str(ROOT / "platform-staging" / "stager.py"))
    (tmp_path / "leaked.txt").write_bytes(payload)

    with pytest.raises(stager["StageError"], match="stage_supply_chain_secret_match"):
        stager["_supply_chain"](
            tmp_path,
            (),
            lock_profile="runtime",
            require_complete=False,
        )


def test_platform_stager_copies_locked_distribution_from_user_site(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stager = runpy.run_path(str(ROOT / "platform-staging" / "stager.py"))
    distribution_root = tmp_path / "user-site"
    package = distribution_root / "example_runtime" / "__init__.py"
    package.parent.mkdir(parents=True)
    package.write_text("VALUE = 1\n", encoding="utf-8")
    metadata = Message()
    metadata["Name"] = "example-runtime"
    metadata["License-Expression"] = "MIT"

    class Distribution:
        version = "1.0.0"
        files = (Path("example_runtime/__init__.py"),)
        requires: tuple[str, ...] = ()

        @property
        def metadata(self) -> Message:
            return metadata

        def locate_file(self, value: str | Path) -> Path:
            return distribution_root / value

    monkeypatch.setattr(
        stager["importlib_metadata"],
        "distribution",
        lambda _name: Distribution(),
    )
    destination = tmp_path / "closure"
    inventory = stager["_copy_distribution_closure"](
        ("example-runtime",),
        destination,
    )

    assert inventory == (
        {"name": "example-runtime", "version": "1.0.0", "license": "MIT"},
    )
    assert (destination / "example_runtime" / "__init__.py").read_text(
        encoding="utf-8"
    ) == "VALUE = 1\n"


def test_dependency_inventory_is_deterministic_and_binds_pack_payload(
    tmp_path: Path,
) -> None:
    stager = runpy.run_path(str(ROOT / "platform-staging" / "stager.py"))
    staged: list[Path] = []
    for name in ("first", "second"):
        pack = tmp_path / name
        shutil.copytree(PACKS / "channels", pack)
        stager["_write_dependency_inventory"](
            pack,
            pack_id="channels",
            distributions=(),
        )
        staged.append(pack)

    assert (staged[0] / "runtime-inventory.json").read_bytes() == (
        staged[1] / "runtime-inventory.json"
    ).read_bytes()
    (staged[0] / "connector-contracts.json").write_bytes(b"{}")
    with pytest.raises(
        stager["StageError"], match="dependency_pack_payload_mismatch"
    ):
        stager["_validate_dependency_pack"](
            staged[0],
            pack_id="channels",
            distributions=(),
        )


def test_dependency_probe_removes_every_non_pack_site_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    probe = runpy.run_path(
        str(ROOT / "platform-staging" / "probes" / "dependency_pack_probe.py")
    )
    python_root = tmp_path / "pack" / "runtime" / "python"
    python_root.mkdir(parents=True)
    stdlib = tmp_path / "stdlib"
    system_site = tmp_path / "core" / "Lib" / "site-packages"
    distribution_site = tmp_path / "core" / "lib" / "dist-packages"
    monkeypatch.setattr(
        sys,
        "path",
        [str(system_site), str(stdlib), str(distribution_site)],
    )

    probe["_activate_pack_runtime"](python_root)

    assert sys.path == [str(python_root), str(stdlib)]


def test_dependency_probe_requires_every_module_origin_inside_pack(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    probe = runpy.run_path(
        str(ROOT / "platform-staging" / "probes" / "dependency_pack_probe.py")
    )
    python_root = tmp_path / "pack" / "runtime" / "python"
    python_root.mkdir(parents=True)
    outside = tmp_path / "core" / "site-packages" / "docx" / "__init__.py"
    outside.parent.mkdir(parents=True)
    outside.write_text("", encoding="utf-8")
    fake_module = type("FakeModule", (), {"__file__": str(outside)})()
    monkeypatch.setitem(sys.modules, "docx", fake_module)

    with pytest.raises(RuntimeError, match="escaped Pack runtime"):
        probe["_module_origins"](("docx",), python_root)


def test_dependency_probe_evidence_rejects_zero_result_ocr() -> None:
    stager = runpy.run_path(str(ROOT / "platform-staging" / "stager.py"))
    modules = {
        name: f"{name}/__init__.py"
        for name in (
            "rapidocr_onnxruntime",
            "onnxruntime",
            "numpy",
            "PIL",
            "cv2",
            "pyclipper",
        )
    }
    value = {
        "isolation": {
            "mode": "pack-only-third-party",
            "module_origins": modules,
        },
        "result": {
            "result_count": 0,
            "elapsed_reported": True,
            "image_size": [640, 180],
            "fixture_recognized": False,
        },
    }

    with pytest.raises(stager["StageError"], match="ocr_runtime_probe_failed"):
        stager["_validate_dependency_probe"]("ocr", value)


def test_office_pack_declares_formats_not_rendering() -> None:
    descriptor = json.loads(
        (PACKS / "office" / "ecorex-dependency-pack.json").read_text(
            encoding="utf-8"
        )
    )
    stager_source = (ROOT / "platform-staging" / "stager.py").read_text(
        encoding="utf-8"
    )

    assert descriptor["adapter"] == "python-office-formats-v1"
    assert descriptor["services"] == ["office.formats"]
    assert "office-format-smoke" in stager_source
    assert "office-runtime-smoke" not in stager_source


def test_bootstrap_requires_the_complete_six_pack_set() -> None:
    source = (ROOT / "platform-staging" / "bootstrap" / "main.go").read_text(
        encoding="utf-8"
    )

    assert (
        '[]string{"browser", "channels", "image", "ocr", "office", "sandbox"}'
        in source
    )
    assert 'strings.HasPrefix(item.ArtifactID, "capability-pack-")' in source
    assert "unexpected host Capability Pack" in source
