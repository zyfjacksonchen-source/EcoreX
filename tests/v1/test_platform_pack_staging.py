from __future__ import annotations

import ast
import hashlib
import inspect
import json
import os
from pathlib import Path, PurePosixPath
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
from ecorex.integration.pack_process import (
    ProcessCapabilityPackAdapter,
    _inspect_zipapp,
)


ROOT = Path(__file__).resolve().parents[2]
PACKS = ROOT / "release" / "capability-packs"


def test_platform_stage_copies_builtin_skills_into_core_payload(tmp_path: Path) -> None:
    stager = runpy.run_path(str(ROOT / "platform-staging" / "stager.py"))
    core = tmp_path / "core"
    core.mkdir()

    stager["_stage_builtin_skills"](core)

    source = ROOT / "skills"
    expected = {
        path.relative_to(source)
        for path in source.rglob("*")
        if path.is_file()
        and "__pycache__" not in path.parts
        and path.suffix not in {".pyc", ".pyo"}
    }
    staged = {
        path.relative_to(core / "skills")
        for path in (core / "skills").rglob("*")
        if path.is_file()
    }
    assert staged == expected
    assert {
        "office-documents",
        "office-spreadsheets",
        "office-presentations",
        "office-pdf",
    } <= {path.parent.name for path in staged if path.name == "SKILL.md"}


def test_platform_stager_reserves_stdout_for_its_single_protocol_response() -> None:
    source = (ROOT / "platform-staging" / "stager.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    stdout_writes: list[ast.Call] = []
    for call in (node for node in ast.walk(tree) if isinstance(node, ast.Call)):
        if isinstance(call.func, ast.Name) and call.func.id == "print":
            file_keyword = next(
                (keyword.value for keyword in call.keywords if keyword.arg == "file"),
                None,
            )
            assert (
                isinstance(file_keyword, ast.Attribute)
                and isinstance(file_keyword.value, ast.Name)
                and file_keyword.value.id == "sys"
                and file_keyword.attr == "stderr"
            ), "platform stager progress must never contaminate protocol stdout"
        if (
            isinstance(call.func, ast.Attribute)
            and call.func.attr == "write"
            and isinstance(call.func.value, ast.Attribute)
            and isinstance(call.func.value.value, ast.Name)
            and call.func.value.value.id == "sys"
            and call.func.value.attr == "stdout"
        ):
            stdout_writes.append(call)
    assert len(stdout_writes) == 1
    assert isinstance(stdout_writes[0].args[0], ast.Constant)
    assert stdout_writes[0].args[0].value == '{"schema_version":1,"status":"passed"}'


def test_platform_stage_records_required_product_service_configuration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stager = runpy.run_path(str(ROOT / "platform-staging" / "stager.py"))
    drill = runpy.run_path(
        str(ROOT / "scripts" / "drill_v1_windows_signed_candidate.py")
    )
    payload = drill["_runtime_config"](b"r" * 32, b"b" * 32, b"s" * 32)
    template = tmp_path / "runtime-config.json"
    template.write_bytes(payload)
    monkeypatch.setenv("ECOREX_STAGE_RUNTIME_CONFIG_TEMPLATE", str(template))
    monkeypatch.setenv(
        "ECOREX_STAGE_RUNTIME_CONFIG_TEMPLATE_SHA256",
        hashlib.sha256(payload).hexdigest(),
    )
    core = tmp_path / "core"
    core.mkdir()

    digest, services = stager["_write_runtime_config"](
        core, "windows", "x64"
    )

    assert digest == hashlib.sha256(
        (core / "runtime-config.json").read_bytes()
    ).hexdigest()
    assert set(services) == {"gateway", "image_orchestration", "share"}
    assert all(item["configured"] is True for item in services.values())
    assert all(len(item["host_sha256"]) == 64 for item in services.values())
    assert "localhost" not in json.dumps(services)
    config = json.loads((core / "runtime-config.json").read_text(encoding="utf-8"))
    assert config["capability_packs"] == list(
        stager["required_capability_pack_projection"](
            platform="windows",
            architecture="x64",
            version=stager["__version__"],
        )
    )


def test_platform_stage_rejects_missing_image_or_share_service(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stager = runpy.run_path(str(ROOT / "platform-staging" / "stager.py"))
    drill = runpy.run_path(
        str(ROOT / "scripts" / "drill_v1_windows_signed_candidate.py")
    )
    raw = json.loads(
        drill["_runtime_config"](b"r" * 32, b"b" * 32, b"s" * 32)
    )
    raw["image_orchestration"] = None
    payload = json.dumps(raw, sort_keys=True, separators=(",", ":")).encode()
    template = tmp_path / "runtime-config.json"
    template.write_bytes(payload)
    monkeypatch.setenv("ECOREX_STAGE_RUNTIME_CONFIG_TEMPLATE", str(template))
    monkeypatch.setenv(
        "ECOREX_STAGE_RUNTIME_CONFIG_TEMPLATE_SHA256",
        hashlib.sha256(payload).hexdigest(),
    )
    core = tmp_path / "core"
    core.mkdir()

    with pytest.raises(stager["StageError"]) as missing:
        stager["_write_runtime_config"](core, "windows", "x64")
    assert missing.value.code == "runtime_config_product_services_missing"


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
        "bash",
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
    contract["contract_id"] = (
        "sandbox_"
        + hashlib.sha256(
            json.dumps(contract, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    )
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


def test_pack_python_manifest_binds_interpreter_and_complete_closure(
    tmp_path: Path,
) -> None:
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


def test_pack_python_rejects_manifest_digest_change_and_symlink_escape(
    tmp_path: Path,
) -> None:
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


def test_browser_pack_evaluate_matches_cowagent_without_an_approval_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.syspath_prepend(str(PACKS / "common"))
    browser = runpy.run_path(str(PACKS / "browser" / "browser_pack.py"))

    class Page:
        url = "https://example.com/"

        def evaluate(self, expression):  # noqa: ANN001
            assert expression == "document.body.textContent"
            return "Example content"

    result = browser["_perform_page_operation"](
        Page(),
        "evaluate",
        {"script": "document.body.textContent"},
    )
    assert result == {
        "url": "https://example.com/",
        "value": "Example content",
    }


def test_browser_navigate_returns_a_snapshot_without_a_second_tool_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.syspath_prepend(str(PACKS / "common"))
    browser = runpy.run_path(str(PACKS / "browser" / "browser_pack.py"))

    class Page:
        url = "about:blank"

        def set_default_timeout(self, timeout):
            assert timeout == 20_000

        def goto(self, url, *, wait_until, timeout):
            assert wait_until == "domcontentloaded"
            assert timeout == 20_000
            self.url = url

        def evaluate(self, expression):
            assert "data-emate-ref" in expression
            return {"text": "Example content", "interactive": []}

        def title(self):
            return "Example"

    result = browser["_BrowserSession"](object(), Page()).execute(
        {"action": "navigate", "url": "about:blank"}
    )
    assert result == {
        "url": "about:blank",
        "title": "Example",
        "text": "Example content",
        "interactive": [],
    }


def retired_legacy_browser_stage_gate_probes_the_packaged_cow_navigate_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stager = runpy.run_path(str(ROOT / "platform-staging" / "stager.py"))
    gate = stager["_browser_gates"]
    globals_ = gate.__globals__
    assert {"greenlet", "playwright", "pyee"} <= set(
        globals_["_RUNTIME_DISTRIBUTIONS"]
    )
    assert "from playwright.sync_api import sync_playwright" in stager[
        "_pack_python_probe_command"
    ](Path("pack-python"))[-1]
    pack = tmp_path / "browser-pack"
    pack.mkdir()
    zipapp = tmp_path / "browser.pyz"
    zipapp.write_bytes(b"packaged-browser")
    captured = {}

    monkeypatch.setitem(globals_, "_temporary_zipapp", lambda _pack: zipapp)
    monkeypatch.setitem(
        globals_,
        "_read_canonical_process_pack_descriptor",
        lambda *_args, **_kwargs: {"pack_id": "browser"},
    )
    monkeypatch.setitem(globals_, "_gate", lambda *_args, **_kwargs: None)
    monkeypatch.setitem(globals_, "_supply_chain", lambda *_args, **_kwargs: {})

    def invoke(_interpreter, _zipapp, request, *, timeout):  # noqa: ANN001
        captured.update({"request": request, "timeout": timeout})
        return {
            "status": "completed",
            "result": {"text": "ecorex-stage-ready"},
        }

    monkeypatch.setitem(globals_, "_invoke_zipapp", invoke)

    gate(
        pack,
        interpreter=Path(sys.executable),
        inventory=(),
        evidence=tmp_path / "evidence",
    )

    assert captured["timeout"] == 60
    assert captured["request"]["tool_id"] == "browser"
    assert captured["request"]["arguments"] == {
        "action": "navigate",
        "url": "data:text/html,<title>ECoreX Stage</title><body>ecorex-stage-ready</body>",
        "timeout": 20_000,
    }


def test_browser_pack_reuses_one_cowagent_session_across_tool_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.syspath_prepend(str(PACKS / "common"))
    browser = runpy.run_path(str(PACKS / "browser" / "browser_pack.py"))
    request_type = browser["Request"]
    created = []

    class Session:
        def __init__(self) -> None:
            self.calls = []
            self.closed = False

        def execute(self, arguments):  # noqa: ANN001
            self.calls.append(dict(arguments))
            return {"call_count": len(self.calls)}

        def close(self) -> None:
            self.closed = True

    class Engine:
        def __init__(self) -> None:
            self.closed = False

        def new_session(self):  # noqa: ANN201
            session = Session()
            created.append(session)
            return session

        def close(self) -> None:
            self.closed = True

    handler_type = browser["BrowserPackHandler"]
    monkeypatch.setitem(handler_type.__init__.__globals__, "_BrowserEngine", Engine)
    handler = handler_type()

    def request(thread_id: str, action: str, url: str | None = None):  # noqa: ANN202
        arguments = {"action": action}
        if url is not None:
            arguments["url"] = url
        return request_type(
            request_id=f"request-{thread_id}-{action}",
            pack_id="browser",
            tool_id="browser",
            arguments=arguments,
            context={
                "effective_sandbox": "danger-full-access",
                "execution_scope": {"thread_id": thread_id},
            },
        )

    assert handler(request("thread-a", "navigate", "https://example.com")) == {
        "call_count": 1
    }
    assert handler(request("thread-a", "snapshot")) == {"call_count": 2}
    assert handler(request("thread-b", "snapshot")) == {"call_count": 3}
    assert len(created) == 1
    assert created[0].calls[1] == {"action": "snapshot"}
    handler.close()
    assert all(session.closed for session in created)


def test_browser_fetch_returns_readable_content_instead_of_base64(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.syspath_prepend(str(PACKS / "common"))
    browser = runpy.run_path(str(PACKS / "browser" / "browser_pack.py"))
    title, text, links = browser["_readable_web_content"](
        b"<html><head><title>Example</title><style>hidden</style></head>"
        b"<body><h1>Readable page</h1><a href='/about'>About us</a></body></html>",
        "text/html; charset=utf-8",
        "https://example.com/start",
    )
    assert title == "Example"
    assert "Readable page" in text
    assert "hidden" not in text
    assert links == [{"url": "https://example.com/about", "text": "About us"}]


def test_browser_search_returns_structured_public_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.syspath_prepend(str(PACKS / "common"))
    browser = runpy.run_path(str(PACKS / "browser" / "browser_pack.py"))
    parser = browser["_DuckSearchHTML"]()
    parser.feed(
        "<a class='result__a' href='//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fdoc'>"
        "Example document</a><div class='result__snippet'>Useful public summary</div>"
    )
    parser.close()
    assert parser.results == [
        {
            "title": "Example document",
            "url": "https://example.com/doc",
            "snippet": "Useful public summary",
        }
    ]


def test_browser_pack_retries_function_body_and_sandbox_classifies_soft_exits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.syspath_prepend(str(PACKS / "common"))
    browser = runpy.run_path(str(PACKS / "browser" / "browser_pack.py"))
    sandbox = runpy.run_path(str(PACKS / "sandbox" / "sandbox_pack.py"))

    class Page:
        url = "https://example.com/"

        def __init__(self) -> None:
            self.calls = []

        def evaluate(self, expression):
            self.calls.append(expression)
            if len(self.calls) == 1:
                raise RuntimeError("Illegal return statement")
            return "ok"

    page = Page()
    result = browser["_perform_page_operation"](
        page,
        "evaluate",
        {"script": "return 'ok'"},
    )
    assert result["value"] == "ok"
    assert page.calls[-1] == "(() => {\nreturn 'ok'\n})()"
    assert sandbox["_interpret_exit"]("rg absent .", 1) == (
        False,
        "No matches found",
    )
    assert sandbox["_interpret_exit"]("python broken.py", 1) == (True, None)


def retired_legacy_browser_stage_selects_revision_matched_relocatable_headless_shell(
    tmp_path: Path,
) -> None:
    stager = runpy.run_path(str(ROOT / "platform-staging" / "stager.py"))
    cache = tmp_path / "ms-playwright"
    chromium = (
        cache
        / "chromium-1169"
        / "chrome"
        / ("chrome.exe" if os.name == "nt" else "Chromium")
    )
    chromium.parent.mkdir(parents=True)
    chromium.write_bytes(b"full Chromium application")
    chromium.chmod(0o755)
    shell = (
        cache
        / "chromium_headless_shell-1169"
        / (
            "chrome-win"
            if os.name == "nt"
            else "chrome-mac"
            if sys.platform == "darwin"
            else "chrome-linux"
        )
        / ("headless_shell.exe" if os.name == "nt" else "headless_shell")
    )
    shell.parent.mkdir(parents=True)
    shell.write_bytes(b"relocatable headless shell")
    shell.chmod(0o755)

    root, executable = stager["_playwright_headless_shell"](chromium.resolve())

    assert root == (cache / "chromium_headless_shell-1169").resolve()
    assert executable == shell.resolve()
    assert "chromium-1169" not in executable.parts


def retired_legacy_browser_stage_rejects_missing_or_ambiguous_headless_shell(
    tmp_path: Path,
) -> None:
    stager = runpy.run_path(str(ROOT / "platform-staging" / "stager.py"))
    stage_error = stager["StageError"]
    cache = tmp_path / "ms-playwright"
    chromium = cache / "chromium-1169" / "chrome" / "Chromium"
    chromium.parent.mkdir(parents=True)
    chromium.write_bytes(b"full Chromium application")
    chromium.chmod(0o755)

    with pytest.raises(stage_error) as missing:
        stager["_playwright_headless_shell"](chromium.resolve())
    assert missing.value.code == "playwright_headless_shell_unavailable"

    executable_name = "headless_shell.exe" if os.name == "nt" else "headless_shell"
    for directory in ("one", "two"):
        candidate = (
            cache / "chromium_headless_shell-1169" / directory / executable_name
        )
        candidate.parent.mkdir(parents=True, exist_ok=True)
        candidate.write_bytes(directory.encode())
        candidate.chmod(0o755)
    with pytest.raises(stage_error) as ambiguous:
        stager["_playwright_headless_shell"](chromium.resolve())
    assert ambiguous.value.code == "playwright_headless_shell_layout_invalid"


def retired_legacy_browser_stage_rejects_link_anywhere_in_headless_shell_tree(
    tmp_path: Path,
) -> None:
    stager = runpy.run_path(str(ROOT / "platform-staging" / "stager.py"))
    stage_error = stager["StageError"]
    cache = tmp_path / "ms-playwright"
    chromium = cache / "chromium-1169" / "chrome" / "Chromium"
    chromium.parent.mkdir(parents=True)
    chromium.write_bytes(b"full Chromium application")
    chromium.chmod(0o755)
    shell_root = cache / "chromium_headless_shell-1169"
    executable = (
        shell_root
        / (
            "chrome-win"
            if os.name == "nt"
            else "chrome-mac"
            if sys.platform == "darwin"
            else "chrome-linux"
        )
        / ("headless_shell.exe" if os.name == "nt" else "headless_shell")
    )
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"relocatable headless shell")
    executable.chmod(0o755)
    outside = tmp_path / "outside-resource.pak"
    outside.write_bytes(b"outside")
    try:
        (shell_root / "resource.pak").symlink_to(outside)
    except OSError as error:
        pytest.skip(f"symlink creation is unavailable: {error}")

    with pytest.raises(stage_error) as linked:
        stager["_playwright_headless_shell"](chromium.resolve())
    assert linked.value.code == "playwright_headless_shell_layout_invalid"


@pytest.mark.skipif(os.name == "nt", reason="POSIX executable-mode contract")
def retired_legacy_browser_stage_normalizes_only_the_pinned_playwright_driver_mode(
    tmp_path: Path,
) -> None:
    stager = runpy.run_path(str(ROOT / "platform-staging" / "stager.py"))
    python_root = tmp_path / "python"
    driver = (
        python_root
        / "playwright"
        / "driver"
        / ("node.exe" if os.name == "nt" else "node")
    )
    ordinary = python_root / "playwright" / "driver" / "package" / "cli.js"
    driver.parent.mkdir(parents=True)
    ordinary.parent.mkdir(parents=True)
    driver.write_bytes(b"driver")
    ordinary.write_bytes(b"ordinary")
    driver.chmod(0o644)
    ordinary.chmod(0o644)

    stager["_normalize_playwright_driver_mode"](
        python_root,
        platform="windows" if os.name == "nt" else "macos",
    )

    expected_driver_mode = 0o755
    assert stat.S_IMODE(driver.stat().st_mode) == expected_driver_mode
    assert stat.S_IMODE(ordinary.stat().st_mode) == 0o644
    records = {item["path"]: item for item in stager["_tree_records"](python_root)}
    driver_path = driver.relative_to(python_root).as_posix()
    ordinary_path = ordinary.relative_to(python_root).as_posix()
    assert records[driver_path]["mode"] == expected_driver_mode
    assert records[ordinary_path]["mode"] == 0o644
    archive = tmp_path / "runtime.zip"
    stager["_write_zip"](python_root, archive)
    with zipfile.ZipFile(archive) as payload:
        assert (
            stat.S_IMODE(payload.getinfo(driver_path).external_attr >> 16)
            == expected_driver_mode
        )
        assert stat.S_IMODE(payload.getinfo(ordinary_path).external_attr >> 16) == 0o644


def retired_legacy_browser_stage_rejects_missing_pinned_playwright_driver(
    tmp_path: Path,
) -> None:
    stager = runpy.run_path(str(ROOT / "platform-staging" / "stager.py"))
    python_root = tmp_path / "python"
    python_root.mkdir()

    with pytest.raises(stager["StageError"]) as missing:
        stager["_normalize_playwright_driver_mode"](
            python_root,
            platform="windows" if os.name == "nt" else "macos",
        )
    assert missing.value.code == "playwright_driver_layout_invalid"


def retired_legacy_browser_stage_surfaces_only_allowlisted_pack_smoke_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stager = runpy.run_path(str(ROOT / "platform-staging" / "stager.py"))
    stage_error = stager["StageError"]
    pack = tmp_path / "browser"
    pack.mkdir()
    (pack / "ecorex-pack.json").write_bytes(
        stager["_canonical_process_pack_descriptor"]("browser")
    )
    zipapp = tmp_path / "browser.pyz"
    zipapp.write_bytes(b"bounded browser probe")
    monkeypatch.setitem(
        stager["_browser_gates"].__globals__,
        "_temporary_zipapp",
        lambda _pack: zipapp,
    )
    monkeypatch.setitem(
        stager["_browser_gates"].__globals__,
        "_invoke_zipapp",
        lambda *_args, **_kwargs: {
            "status": "failed",
            "error_code": "browser_runtime_import_failed",
        },
    )

    with pytest.raises(stage_error) as classified:
        stager["_browser_gates"](
            pack,
            interpreter=Path(sys.executable),
            inventory=(),
            evidence=tmp_path / "evidence",
        )
    assert classified.value.code == "browser_pack_smoke_browser_runtime_import_failed"

    zipapp.write_bytes(b"bounded browser probe")
    monkeypatch.setitem(
        stager["_browser_gates"].__globals__,
        "_invoke_zipapp",
        lambda *_args, **_kwargs: {
            "status": "failed",
            "error_code": "must_not_surface",
        },
    )
    with pytest.raises(stage_error) as redacted:
        stager["_browser_gates"](
            pack,
            interpreter=Path(sys.executable),
            inventory=(),
            evidence=tmp_path / "other-evidence",
        )
    assert redacted.value.code == "browser_pack_smoke_failed"


def retired_legacy_macos_browser_runtime_signs_every_expected_architecture_macho(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stager = runpy.run_path(str(ROOT / "platform-staging" / "stager.py"))
    prepare = stager["_prepare_macos_browser_runtime"]
    globals_ = prepare.__globals__
    runtime = tmp_path / "runtime"
    driver = runtime / "python" / "playwright" / "driver" / "node"
    chromium = runtime / "browser" / "chrome-mac" / "headless_shell"
    for binary in (driver, chromium):
        binary.parent.mkdir(parents=True, exist_ok=True)
        binary.write_bytes(b"\xcf\xfa\xed\xfe")
        binary.chmod(0o755)
    original_is_file = Path.is_file
    monkeypatch.setattr(
        Path,
        "is_file",
        lambda path: True
        if path == Path("/usr/bin/codesign")
        else original_is_file(path),
    )
    monkeypatch.setitem(
        globals_,
        "_macos_macho_files",
        lambda _root: (driver, chromium),
    )
    monkeypatch.setitem(
        globals_,
        "_macos_architectures",
        lambda _binary: ("arm64",),
    )
    commands: list[tuple[str, ...]] = []
    monkeypatch.setitem(
        globals_,
        "_run",
        lambda command, **_kwargs: commands.append(tuple(command)),
    )
    archive_checks: list[Path] = []
    monkeypatch.setitem(
        globals_,
        "_assert_macos_browser_signature_archive_stability",
        lambda root: archive_checks.append(root),
    )

    prepare(runtime, architecture="arm64")

    assert archive_checks == [runtime]
    codesign = str(Path("/usr/bin/codesign"))
    assert commands == [
        (
            codesign,
            "--force",
            "--sign",
            "-",
            "--timestamp=none",
            str(driver),
        ),
        (
            codesign,
            "--force",
            "--sign",
            "-",
            "--timestamp=none",
            str(chromium),
        ),
        (codesign, "--verify", "--strict", str(driver)),
        (codesign, "--verify", "--strict", str(chromium)),
    ]


def test_macos_stage_signs_and_verifies_native_launcher_and_bootstrap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stager = runpy.run_path(str(ROOT / "platform-staging" / "stager.py"))
    sign = stager["_adhoc_sign_macos_binary"]
    commands: list[tuple[tuple[str, ...], dict[str, object]]] = []
    monkeypatch.setitem(
        sign.__globals__,
        "_run",
        lambda command, **kwargs: commands.append((tuple(command), kwargs)),
    )
    binary = tmp_path / "ecorex"

    sign(binary, cwd=tmp_path)

    assert [command for command, _kwargs in commands] == [
        (
            "/usr/bin/codesign",
            "--force",
            "--sign",
            "-",
            "--timestamp=none",
            str(binary),
        ),
        ("/usr/bin/codesign", "--verify", "--strict", str(binary)),
    ]
    assert all(
        kwargs["cwd"] == tmp_path
        and kwargs["timeout"] == 30
        and kwargs["code"] == "macos_native_signing_failed"
        for _command, kwargs in commands
    )
    source = (ROOT / "platform-staging" / "stager.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    call_sites = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_adhoc_sign_macos_binary"
    ]
    assert len(call_sites) == 2


def retired_legacy_macos_browser_runtime_rejects_wrong_architecture_before_signing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stager = runpy.run_path(str(ROOT / "platform-staging" / "stager.py"))
    prepare = stager["_prepare_macos_browser_runtime"]
    globals_ = prepare.__globals__
    stage_error = stager["StageError"]
    runtime = tmp_path / "runtime"
    binary = runtime / "driver" / "node"
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"\xcf\xfa\xed\xfe")
    binary.chmod(0o755)
    original_is_file = Path.is_file
    monkeypatch.setattr(
        Path,
        "is_file",
        lambda path: True
        if path == Path("/usr/bin/codesign")
        else original_is_file(path),
    )
    monkeypatch.setitem(globals_, "_macos_macho_files", lambda _root: (binary,))
    monkeypatch.setitem(
        globals_, "_macos_architectures", lambda _binary: ("x86_64",)
    )

    with pytest.raises(stage_error) as rejected:
        prepare(runtime, architecture="arm64")
    assert rejected.value.code == "browser_runtime_macho_architecture_invalid"


def retired_legacy_macos_browser_signature_gate_uses_archive_equivalent_round_trip(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stager = runpy.run_path(str(ROOT / "platform-staging" / "stager.py"))
    verify = stager["_assert_macos_browser_signature_archive_stability"]
    globals_ = verify.__globals__
    stage_error = stager["StageError"]
    runtime = tmp_path / "runtime"
    executable = runtime / "driver" / "node"
    resource = runtime / "browser" / "resource.pak"
    executable.parent.mkdir(parents=True)
    resource.parent.mkdir(parents=True)
    executable.write_bytes(b"signed-macho")
    executable.chmod(0o755)
    resource.write_bytes(b"resource")
    monkeypatch.setitem(
        globals_, "_macos_snapshot_signatures_valid", lambda _root: True
    )

    verify(runtime)

    monkeypatch.setitem(
        globals_, "_macos_snapshot_signatures_valid", lambda _root: False
    )
    with pytest.raises(stage_error) as nonportable:
        verify(runtime)
    assert (
        nonportable.value.code
        == "browser_runtime_macho_signature_not_portable"
    )


def test_browser_pack_allows_local_workflows_but_blocks_metadata_and_websockets(
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

    local = Route("http://127.0.0.1:8765/local-app")
    browser["_guard_browser_request"](local)
    assert local.continued is True
    assert local.aborted is None

    metadata = Route("http://169.254.169.254/latest/meta-data")
    browser["_guard_browser_request"](metadata)
    assert metadata.aborted == "blockedbyclient"
    assert metadata.continued is False

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


def test_browser_pack_accepts_proxy_fake_ip_only_for_public_hostname(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.syspath_prepend(str(PACKS / "common"))
    browser = runpy.run_path(str(PACKS / "browser" / "browser_pack.py"))
    contract_error = browser["ContractError"]

    monkeypatch.setattr(
        browser["socket"],
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (2, 1, 6, "", ("198.18.0.117", 443)),
        ],
    )
    browser["_validate_public_url"]("https://example.com")
    with pytest.raises(contract_error, match="browser_url_not_allowed"):
        browser["_validate_public_url"]("https://198.18.0.117")

    monkeypatch.setattr(
        browser["socket"],
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (2, 1, 6, "", ("127.0.0.1", 443)),
        ],
    )
    with pytest.raises(contract_error, match="browser_url_not_allowed"):
        browser["_validate_public_url"]("https://example.com")


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
            self.name = str(tmp_path)

        def cleanup(self) -> None:
            observed["cleaned"] = True

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
        "cleaned": True,
    }


def test_browser_pack_phase_errors_are_fixed_and_cleanup_preserves_primary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.syspath_prepend(str(PACKS / "common"))
    browser = runpy.run_path(str(PACKS / "browser" / "browser_pack.py"))
    contract_error = browser["ContractError"]

    with pytest.raises(contract_error) as phase:
        browser["_browser_phase"](
            "browser_network_guard_failed",
            lambda: (_ for _ in ()).throw(AttributeError("private detail")),
        )
    assert phase.value.code == "browser_network_guard_failed"
    assert "private detail" not in str(phase.value)

    original = contract_error("browser_navigation_failed", retryable=True)
    with pytest.raises(contract_error) as preserved:
        try:
            raise original
        finally:
            browser["_browser_cleanup_phase"](
                "browser_close_failed",
                lambda: (_ for _ in ()).throw(OSError("private cleanup detail")),
                suppress=sys.exception() is not None,
            )
    assert preserved.value is original

    with pytest.raises(contract_error) as cleanup:
        browser["_browser_cleanup_phase"](
            "browser_close_failed",
            lambda: (_ for _ in ()).throw(OSError("private cleanup detail")),
            suppress=False,
        )
    assert cleanup.value.code == "browser_close_failed"


def test_sandbox_pack_acknowledges_exact_core_contract_and_fixed_shell(
    tmp_path: Path,
) -> None:
    artifact = _zipapp(tmp_path, "sandbox")
    request = _request(
        "sandbox",
        "bash",
        {"command": "pwd"},
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
        "timeout_seconds": 125.0,
        "stdout_limit_bytes": 4 * 1024 * 1024,
        "stderr_limit_bytes": 64 * 1024,
    }
    contract["contract_id"] = (
        "sandbox_"
        + hashlib.sha256(
            json.dumps(contract, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    )
    request["context"]["sandbox_contract"] = contract
    response = _invoke(artifact, request)
    assert response["status"] == "completed"
    assert response["sandbox_contract_id"] == contract["contract_id"]
    assert Path(response["result"]["stdout"].strip()).resolve() == tmp_path.resolve()

    request["context"]["sandbox_contract"]["contract_id"] = "sandbox_" + "0" * 64
    rejected = _invoke(artifact, request)
    assert rejected["status"] == "failed"
    assert rejected["error_code"] == "shell_sandbox_contract_invalid"


def test_sandbox_pack_accepts_attested_windows_workspace_runtime_read_scope(
    tmp_path: Path,
) -> None:
    artifact = _zipapp(tmp_path, "sandbox")
    request = _request(
        "sandbox",
        "bash",
        {"command": "echo ecorex-workspace-runtime", "timeout_seconds": 5},
        tmp_path,
    )
    roots_digest = hashlib.sha256(str(tmp_path.resolve()).encode()).hexdigest()
    contract = {
        "profile": "workspace-write",
        "backend_id": "windows-appcontainer",
        "os_enforced": True,
        "workspace_roots_sha256": roots_digest,
        "filesystem_read_scope": "workspace-and-runtime",
        "filesystem_write_scope": "workspace-only",
        "network_scope": "denied",
        "process_tree_scope": "contained-inherited",
        "timeout_seconds": 10.0,
        "stdout_limit_bytes": 4 * 1024 * 1024,
        "stderr_limit_bytes": 64 * 1024,
    }
    contract["contract_id"] = (
        "sandbox_"
        + hashlib.sha256(
            json.dumps(contract, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    )
    request["context"].update(
        {
            "effective_sandbox": "workspace-write",
            "sandbox_contract": contract,
        }
    )

    response = _invoke(artifact, request)

    assert response["status"] == "completed"
    assert "ecorex-workspace-runtime" in response["result"]["stdout"]


@pytest.mark.skipif(os.name != "nt", reason="Windows PowerShell path contract")
def test_sandbox_pack_resolves_trusted_windows_powershell_from_child_path(
    tmp_path: Path,
) -> None:
    artifact = _zipapp(tmp_path, "sandbox")
    request = _sandbox_shell_request(
        tmp_path,
        'powershell -NoProfile -NonInteractive -Command "Get-Date -Format yyyy"',
    )

    response = _invoke(artifact, request)

    assert response["status"] == "completed"
    year = response["result"]["stdout"].strip()
    assert year.isdigit() and len(year) == 4


def test_sandbox_pack_streams_and_stops_stdout_flood(tmp_path: Path) -> None:
    artifact = _zipapp(tmp_path, "sandbox")
    marker = "stdout-flood-sensitive-marker"
    command = _shell_script_command(
        tmp_path,
        "stdout-flood.py",
        "import sys;"
        f"sys.stdout.buffer.write({marker.encode()!r}+b'x'*(600*1024));"
        "sys.stdout.flush()",
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
        "sys.stderr.flush()",
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
        f"sys.stderr.buffer.write({stderr.encode()!r})",
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
    assert "msvcp140_sha256" in build
    assert "Microsoft.VC143.CRT" in build
    assert "toolchain-manifest.json" in build
    assert "compiler_identity_untrusted" not in build  # composed from trusted label
    assert "Resolve-TrustedTool" in build
    assert "& $linkPath" in build
    assert "ecorex_sandbox_security.cpp" in build
    assert "ecorex_sandbox_process.cpp" in build
    assert "ExpectedToolchainManifestSha256" in build
    assert "ExpectedSourceSetSha256" in build
    assert "else { 'caller-pinned' }" in build
    assert "GitHubHostedCompatibility" in build
    assert "github-hosted-ci-compatibility" in build
    assert "github_hosted_compatibility_boundary_invalid" in build
    assert "$env:GITHUB_ACTIONS -cne 'true'" in build
    assert "$env:RUNNER_OS -cne 'Windows'" in build
    assert "$env:ImageOS -cne 'win22'" in build
    assert "observedLibraryDigests" in build
    assert "SpecialFolder]::ProgramFiles)" in build
    assert "SpecialFolder]::ProgramFilesX86" in build
    assert "SpecialFolder]::Windows" in build
    assert "${env:ProgramFiles(x86)}" not in build
    stager_source = (ROOT / "platform-staging/stager.py").read_text(encoding="utf-8")
    assert "ECOREX_GITHUB_HOSTED_WINDOWS_NATIVE_COMPATIBILITY" in stager_source
    assert 'command += ("-GitHubHostedCompatibility",)' in stager_source


def test_windows_native_source_authority_is_checkout_platform_independent() -> None:
    source_names = (
        "ecorex_launcher.cpp",
        "ecorex_sandbox_host.cpp",
        "ecorex_sandbox_security.cpp",
        "ecorex_sandbox_process.cpp",
        "ecorex_sandbox_host_internal.h",
    )
    output = subprocess.check_output(
        ("git", "check-attr", "text", "eol", "--", *source_names),
        cwd=ROOT / "platform-staging/native/windows",
        text=True,
    )
    assert output.splitlines() == [
        line
        for name in source_names
        for line in (f"{name}: text: set", f"{name}: eol: lf")
    ]


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
    manifest_source = ROOT / "platform-staging/native/windows/toolchain-manifest.json"
    manifest_value = json.loads(manifest_source.read_text(encoding="utf-8"))
    output = tmp_path / "native"
    output.mkdir()
    launcher = output / "ecorex.exe"
    helper = output / "ecorex-sandbox-host.exe"
    msvcp = output / "msvcp140.dll"
    launcher.write_bytes(b"trusted-launcher-test")
    helper.write_bytes(b"trusted-helper-test")
    msvcp.write_bytes(b"trusted-app-local-runtime-test")
    manifest_value["runtime_libraries"]["msvcp140.dll"]["sha256"] = hashlib.sha256(
        msvcp.read_bytes()
    ).hexdigest()
    manifest = tmp_path / "toolchain-manifest.json"
    manifest.write_text(
        json.dumps(manifest_value, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
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
        "linker_authenticode_thumbprint": tools["linker"]["authenticode_thumbprint"],
        "c1xx_sha256": tools["c1xx"]["sha256"],
        "c1xx_authenticode_thumbprint": tools["c1xx"]["authenticode_thumbprint"],
        "c2_sha256": tools["c2"]["sha256"],
        "c2_authenticode_thumbprint": tools["c2"]["authenticode_thumbprint"],
        "runtime_launcher_sha256": hashlib.sha256(launcher.read_bytes()).hexdigest(),
        "sandbox_helper_sha256": hashlib.sha256(helper.read_bytes()).hexdigest(),
        "msvcp140_sha256": hashlib.sha256(msvcp.read_bytes()).hexdigest(),
        "msvcp140_file_version": manifest_value["runtime_libraries"][
            "msvcp140.dll"
        ]["file_version"],
        "msvcp140_authenticode_thumbprint": manifest_value["runtime_libraries"][
            "msvcp140.dll"
        ]["authenticode_thumbprint"],
    }
    receipt_path = output / "native-build-receipt.json"
    receipt_path.write_text(
        json.dumps(receipt, separators=(",", ":")) + "\n", encoding="utf-8"
    )
    stager["_validate_windows_native_receipt"](
        output,
        toolchain_manifest=manifest,
    )

    receipt["authority_mode"] = "github-hosted-ci-compatibility"
    receipt["library_set_sha256"] = "5" * 64
    for name in ("compiler", "linker", "c1xx", "c2"):
        receipt[f"{name}_sha256"] = "6" * 64
        receipt[f"{name}_authenticode_thumbprint"] = "7" * 40
    receipt["compiler_file_version"] = "19.44.99999.0"
    receipt["linker_file_version"] = "14.44.99999.0"
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
    stager["_validate_windows_native_receipt"](
        output,
        toolchain_manifest=manifest,
        github_hosted_compatibility=True,
    )

    receipt["authority_mode"] = "caller-pinned"
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


def test_windows_installs_locked_msvcp_beside_pack_python(tmp_path: Path) -> None:
    stager = runpy.run_path(str(ROOT / "platform-staging/stager.py"))
    native = tmp_path / "native"
    native.mkdir()
    for name in ("ecorex.exe", "ecorex-sandbox-host.exe", "msvcp140.dll"):
        (native / name).write_bytes(name.encode("ascii"))
    core = tmp_path / "core"
    (core / "bin/pack-python").mkdir(parents=True)

    stager["_install_native"](native, core, "windows")

    assert (core / "bin/pack-python/msvcp140.dll").read_bytes() == b"msvcp140.dll"
    probe = stager["_pack_python_probe_command"](
        Path("pack-python")
    )[-1]
    assert "from greenlet import greenlet" in probe
    assert "GetModuleFileNameW" in probe
    assert "parent == Path(sys.executable).resolve(strict=True).parent" in probe


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
        output / "msvcp140.dll",
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
        key: value for key, value in os.environ.items() if key.upper() not in blocked
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
        output / "msvcp140.dll",
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
        key: value for key, value in os.environ.items() if key.upper() not in blocked
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
        output / "msvcp140.dll",
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
        key: value for key, value in os.environ.items() if key.upper() not in blocked
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
            if path.is_file() and path.suffix.casefold() in {
                ".py",
                ".json",
                ".md",
                ".c",
                ".cpp",
                ".ps1",
                ".sh",
            }:
                text = path.read_text(encoding="utf-8")
                assert not any(marker in text for marker in forbidden), path


def test_platform_stager_binds_installed_runtime_inventory_to_hash_lock() -> None:
    stager = runpy.run_path(str(ROOT / "platform-staging" / "stager.py"))
    assert "lark-channel-sdk" in stager["_RUNTIME_DISTRIBUTIONS"]
    assert "qrcode" in stager["_RUNTIME_DISTRIBUTIONS"]
    versions = stager["_active_lock_versions"](
        ROOT / "requirements" / "locks" / "runtime.lock"
    )
    assert set(stager["_RUNTIME_DISTRIBUTIONS"]) == set(versions)
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


def test_windows_pack_python_uses_base_interpreter_not_venv_launcher(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stager = runpy.run_path(str(ROOT / "platform-staging" / "stager.py"))
    base = tmp_path / "cpython-base"
    base.mkdir()
    (base / "Lib").mkdir()
    base_interpreter = base / "python.exe"
    base_interpreter.write_bytes(b"real-cpython-interpreter")
    venv = tmp_path / "build-venv"
    (venv / "Scripts").mkdir(parents=True)
    venv_interpreter = venv / "Scripts" / "python.exe"
    venv_interpreter.write_bytes(b"venv-launcher-needs-pyvenv-cfg")
    (venv / "pyvenv.cfg").write_text(f"home = {base}\n", encoding="utf-8")
    monkeypatch.setattr(sys, "base_prefix", str(base))
    monkeypatch.setattr(sys, "executable", str(venv_interpreter))

    prefix, executable, stdlib = stager["_base_python_runtime_source"]("windows")

    assert prefix == base.resolve()
    assert executable == base_interpreter.resolve()
    assert executable.read_bytes() == b"real-cpython-interpreter"
    assert executable != venv_interpreter.resolve()
    assert stdlib == (base / "Lib").resolve()


def test_macos_pack_python_selects_real_framework_app_interpreter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stager = runpy.run_path(str(ROOT / "platform-staging" / "stager.py"))
    version = f"{sys.version_info.major}.{sys.version_info.minor}"
    base = tmp_path / "cpython-base"
    trampoline = base / "bin" / f"python{version}"
    interpreter = (
        base
        / "Resources"
        / "Python.app"
        / "Contents"
        / "MacOS"
        / "Python"
    )
    stdlib = base / "lib" / f"python{version}"
    trampoline.parent.mkdir(parents=True)
    interpreter.parent.mkdir(parents=True)
    stdlib.mkdir(parents=True)
    trampoline.write_bytes(b"framework-trampoline-needs-python-app")
    interpreter.write_bytes(b"real-macos-framework-cpython")
    venv = tmp_path / "venv" / "bin" / "python"
    venv.parent.mkdir(parents=True)
    venv.write_bytes(b"venv-python")
    monkeypatch.setattr(sys, "base_prefix", str(base))
    monkeypatch.setattr(sys, "executable", str(venv))

    prefix, selected, selected_stdlib = stager["_base_python_runtime_source"]("macos")

    assert prefix == base.resolve()
    assert selected == interpreter.resolve()
    assert selected.read_bytes() == b"real-macos-framework-cpython"
    assert selected != trampoline.resolve()
    assert selected_stdlib == stdlib.resolve()


def test_macos_pack_python_never_falls_back_to_framework_trampoline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stager = runpy.run_path(str(ROOT / "platform-staging" / "stager.py"))
    version = f"{sys.version_info.major}.{sys.version_info.minor}"
    base = tmp_path / "cpython-base"
    trampoline = base / "bin" / f"python{version}"
    stdlib = base / "lib" / f"python{version}"
    trampoline.parent.mkdir(parents=True)
    stdlib.mkdir(parents=True)
    trampoline.write_bytes(b"framework-trampoline-needs-python-app")
    monkeypatch.setattr(sys, "base_prefix", str(base))

    with pytest.raises(stager["StageError"], match="pack_python_base_runtime_invalid"):
        stager["_base_python_runtime_source"]("macos")


def test_macos_pack_python_refuses_linked_framework_app_interpreter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stager = runpy.run_path(str(ROOT / "platform-staging" / "stager.py"))
    version = f"{sys.version_info.major}.{sys.version_info.minor}"
    base = tmp_path / "cpython-base"
    trampoline = base / "bin" / f"python{version}"
    interpreter = (
        base
        / "Resources"
        / "Python.app"
        / "Contents"
        / "MacOS"
        / "Python"
    )
    stdlib = base / "lib" / f"python{version}"
    trampoline.parent.mkdir(parents=True)
    interpreter.parent.mkdir(parents=True)
    stdlib.mkdir(parents=True)
    trampoline.write_bytes(b"framework-trampoline-needs-python-app")
    try:
        interpreter.symlink_to(trampoline)
    except (NotImplementedError, OSError) as error:
        pytest.skip(f"symlink creation is unavailable: {error}")
    monkeypatch.setattr(sys, "base_prefix", str(base))

    with pytest.raises(stager["StageError"], match="pack_python_base_runtime_invalid"):
        stager["_base_python_runtime_source"]("macos")


@pytest.mark.skipif(
    sys.platform != "darwin"
    or not (
        Path(sys.base_prefix)
        / "Resources"
        / "Python.app"
        / "Contents"
        / "MacOS"
        / "Python"
    ).is_file(),
    reason="requires the real python.org macOS Framework installation",
)
def test_macos_pack_python_real_framework_source_is_the_app_interpreter() -> None:
    stager = runpy.run_path(str(ROOT / "platform-staging" / "stager.py"))
    prefix, interpreter, stdlib = stager["_base_python_runtime_source"]("macos")
    expected = (
        prefix
        / "Resources"
        / "Python.app"
        / "Contents"
        / "MacOS"
        / "Python"
    )
    version = f"{sys.version_info.major}.{sys.version_info.minor}"

    assert interpreter == expected
    assert interpreter.is_file()
    assert not interpreter.is_symlink()
    assert stdlib == prefix / "lib" / f"python{version}"
    assert os.uname().machine in stager["_macos_architectures"](interpreter)


def test_macos_base_runtime_dylib_link_resolves_inside_prefix(
    tmp_path: Path,
) -> None:
    stager = runpy.run_path(str(ROOT / "platform-staging" / "stager.py"))
    prefix = tmp_path / "base"
    lib = prefix / "lib"
    lib.mkdir(parents=True)
    target = lib / "libpython3.11.9.dylib"
    target.write_bytes(b"versioned-macos-libpython")
    alias = lib / "libpython3.11.dylib"
    try:
        alias.symlink_to(target.name)
    except (NotImplementedError, OSError) as error:
        pytest.skip(f"symlink creation is unavailable: {error}")

    resolved = stager["_base_runtime_regular_file"](
        alias,
        prefix=prefix.resolve(),
    )

    assert resolved == target.resolve()
    destination = tmp_path / "closure" / alias.name
    stager["_copy_regular"](resolved, destination, executable=True)
    assert destination.read_bytes() == target.read_bytes()
    assert not destination.is_symlink()


def test_macos_base_runtime_dylib_link_cannot_escape_prefix(
    tmp_path: Path,
) -> None:
    stager = runpy.run_path(str(ROOT / "platform-staging" / "stager.py"))
    prefix = tmp_path / "base"
    lib = prefix / "lib"
    lib.mkdir(parents=True)
    outside = tmp_path / "outside.dylib"
    outside.write_bytes(b"outside-authority")
    alias = lib / "libpython3.11.dylib"
    try:
        alias.symlink_to(outside)
    except (NotImplementedError, OSError) as error:
        pytest.skip(f"symlink creation is unavailable: {error}")

    with pytest.raises(stager["StageError"], match="pack_python_base_runtime_invalid"):
        stager["_base_runtime_regular_file"](
            alias,
            prefix=prefix.resolve(),
        )


@pytest.mark.parametrize("target_kind", ["directory", "broken"])
def test_macos_base_runtime_dylib_link_requires_regular_target(
    tmp_path: Path,
    target_kind: str,
) -> None:
    stager = runpy.run_path(str(ROOT / "platform-staging" / "stager.py"))
    prefix = tmp_path / "base"
    lib = prefix / "lib"
    lib.mkdir(parents=True)
    target = lib / "invalid-target"
    if target_kind == "directory":
        target.mkdir()
    alias = lib / "libpython3.11.dylib"
    try:
        alias.symlink_to(target.name, target_is_directory=target_kind == "directory")
    except (NotImplementedError, OSError) as error:
        pytest.skip(f"symlink creation is unavailable: {error}")

    with pytest.raises(stager["StageError"], match="pack_python_base_runtime_invalid"):
        stager["_base_runtime_regular_file"](
            alias,
            prefix=prefix.resolve(),
        )


def test_macos_base_runtime_dylib_rejects_reparse_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stager = runpy.run_path(str(ROOT / "platform-staging" / "stager.py"))
    prefix = tmp_path / "base"
    prefix.mkdir()
    candidate = prefix / "libpython3.11.dylib"
    candidate.write_bytes(b"runtime-library")
    original_lstat = Path.lstat
    calls = 0

    def fake_lstat(path: Path) -> object:
        nonlocal calls
        metadata = original_lstat(path)
        if path == candidate:
            calls += 1
            if calls >= 2:
                return SimpleNamespace(
                    st_mode=metadata.st_mode,
                    st_file_attributes=getattr(
                        stat,
                        "FILE_ATTRIBUTE_REPARSE_POINT",
                        0x400,
                    ),
                    st_reparse_tag=1,
                )
        return metadata

    monkeypatch.setattr(Path, "lstat", fake_lstat)

    with pytest.raises(stager["StageError"], match="pack_python_base_runtime_invalid"):
        stager["_base_runtime_regular_file"](
            candidate,
            prefix=prefix.resolve(),
        )


def test_macos_python_closure_rewrites_framework_loads_and_install_name(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stager = runpy.run_path(str(ROOT / "platform-staging" / "stager.py"))
    closure = tmp_path / "closure"
    interpreter = closure / "bin" / "python3"
    library = closure / "lib" / "libpython3.11.dylib"
    helper = closure / "lib" / "libhelper.dylib"
    stable = closure / "lib" / "libstable.dylib"
    interpreter.parent.mkdir(parents=True)
    library.parent.mkdir(parents=True)
    macho = b"\xcf\xfa\xed\xfe" + b"test-mach-o"
    interpreter.write_bytes(macho)
    library.write_bytes(macho)
    helper.write_bytes(macho)
    stable.write_bytes(macho)
    source_prefix = tmp_path / "toolcache" / "Python" / "3.11.9" / "arm64"
    source_prefix.mkdir(parents=True)
    framework_python = "/Library/Frameworks/Python.framework/Versions/3.11/Python"
    install_names: dict[Path, str | None] = {
        interpreter.resolve(): None,
        library.resolve(): framework_python,
        helper.resolve(): "@rpath/libhelper.dylib",
        stable.resolve(): "@loader_path/libstable.dylib",
    }
    dependencies: dict[Path, list[str]] = {
        interpreter.resolve(): [
            framework_python,
            "@rpath/libhelper.dylib",
            "/usr/lib/libSystem.B.dylib",
        ],
        library.resolve(): ["/usr/lib/libSystem.B.dylib"],
        helper.resolve(): ["/usr/lib/libSystem.B.dylib"],
        stable.resolve(): ["/usr/lib/libSystem.B.dylib"],
    }
    rpaths: dict[Path, list[str]] = {
        interpreter.resolve(): [
            str(source_prefix / "lib"),
            "@loader_path/../../../../outside-closure",
        ],
        library.resolve(): [],
        helper.resolve(): [],
        stable.resolve(): [],
    }
    commands: list[tuple[str, ...]] = []
    original_is_file = Path.is_file

    def fake_is_file(path: Path) -> bool:
        if path.as_posix() in {
            "/usr/bin/lipo",
            "/usr/bin/otool",
            "/usr/bin/install_name_tool",
            "/usr/bin/codesign",
        }:
            return True
        return original_is_file(path)

    def fake_run(
        command: tuple[str, ...],
        **kwargs: object,
    ) -> SimpleNamespace:
        commands.append(command)
        tool = command[0]
        if command[:2] == ("/usr/bin/lipo", "-archs"):
            return SimpleNamespace(stdout=b"arm64 x86_64\n")
        if command[:2] == ("/usr/bin/otool", "-arch"):
            operation = command[3]
            binary = Path(command[4]).resolve()
            if operation == "-D":
                name = install_names[binary]
                suffix = f"{name}\n" if name is not None else ""
                return SimpleNamespace(
                    stdout=(f"{binary} (architecture {command[2]}):\n{suffix}").encode(
                        "utf-8"
                    )
                )
            if operation == "-L":
                values = list(dependencies[binary])
                name = install_names[binary]
                if name is not None:
                    values.insert(0, name)
                lines = "".join(
                    f"\t{value} (compatibility version 1.0.0, current version 1.0.0)\n"
                    for value in values
                )
                return SimpleNamespace(
                    stdout=(f"{binary} (architecture {command[2]}):\n{lines}").encode(
                        "utf-8"
                    )
                )
            assert operation == "-l"
            lines = "".join(
                "Load command 0\n"
                "          cmd LC_RPATH\n"
                "      cmdsize 48\n"
                f"         path {value} (offset 12)\n"
                for value in rpaths[binary]
            )
            return SimpleNamespace(stdout=lines.encode("utf-8"))
        if tool == "/usr/bin/install_name_tool":
            binary = Path(command[-1]).resolve()
            index = 1
            while index < len(command) - 1:
                operation = command[index]
                if operation == "-id":
                    install_names[binary] = command[index + 1]
                    index += 2
                    continue
                if operation == "-change":
                    old, new = command[index + 1 : index + 3]
                    dependencies[binary] = [
                        new if value == old else value for value in dependencies[binary]
                    ]
                    index += 3
                    continue
                assert operation == "-delete_rpath"
                rpaths[binary].remove(command[index + 1])
                index += 2
            return SimpleNamespace(stdout=b"")
        assert tool == "/usr/bin/codesign"
        return SimpleNamespace(stdout=b"")

    monkeypatch.setattr(Path, "is_file", fake_is_file)
    monkeypatch.setitem(
        stager["_relocate_macos_python_closure"].__globals__,
        "_run",
        fake_run,
    )
    monkeypatch.setitem(
        stager["_relocate_macos_python_closure"].__globals__,
        "_assert_macos_signature_copy_stability",
        lambda _closure: commands.append(("portable-signature-gate",)),
    )

    stager["_relocate_macos_python_closure"](
        closure,
        source_prefix=source_prefix,
        architecture="x64",
    )

    assert (
        json.loads((closure / "native-components.json").read_text())["architecture"]
        == "x64"
    )

    assert dependencies[interpreter.resolve()] == [
        "@loader_path/../lib/libpython3.11.dylib",
        "@loader_path/../lib/libhelper.dylib",
        "/usr/lib/libSystem.B.dylib",
    ]
    assert install_names[library.resolve()] == "@loader_path/libpython3.11.dylib"
    assert install_names[helper.resolve()] == "@loader_path/libhelper.dylib"
    assert rpaths[interpreter.resolve()] == []
    assert not any("-delete_all_rpaths" in command for command in commands)
    assert any(
        "-delete_rpath" in command
        for command in commands
        if command[0] == "/usr/bin/install_name_tool"
    )
    modified = {
        Path(command[-1]).resolve()
        for command in commands
        if command[0] == "/usr/bin/install_name_tool"
    }
    signed = {
        Path(command[-1]).resolve()
        for command in commands
        if command[:2] == ("/usr/bin/codesign", "--force")
    }
    verified = {
        Path(command[-1]).resolve()
        for command in commands
        if command[:2] == ("/usr/bin/codesign", "--verify")
    }
    assert modified == {
        interpreter.resolve(),
        library.resolve(),
        helper.resolve(),
    }
    assert signed == verified == {
        interpreter.resolve(),
        library.resolve(),
        helper.resolve(),
        stable.resolve(),
    }
    relocation_indices = tuple(
        index
        for index, command in enumerate(commands)
        if command[0] == "/usr/bin/install_name_tool"
    )
    signing_indices = tuple(
        index
        for index, command in enumerate(commands)
        if command[:2] == ("/usr/bin/codesign", "--force")
    )
    verification_indices = tuple(
        index
        for index, command in enumerate(commands)
        if command[:2] == ("/usr/bin/codesign", "--verify")
    )
    assert relocation_indices and signing_indices and verification_indices
    assert max(relocation_indices) < min(signing_indices)
    assert max(signing_indices) < min(verification_indices)
    portable_gate_index = commands.index(("portable-signature-gate",))
    assert max(verification_indices) < portable_gate_index
    assert any(
        index > portable_gate_index and command[:2] == ("/usr/bin/otool", "-arch")
        for index, command in enumerate(commands)
    )
    inspected = {
        (command[2], command[3], Path(command[4]).resolve())
        for command in commands
        if command[:2] == ("/usr/bin/otool", "-arch")
    }
    assert {
        (architecture, operation, binary)
        for architecture in ("arm64", "x86_64")
        for operation in ("-D", "-L", "-l")
        for binary in (interpreter.resolve(), library.resolve(), helper.resolve())
    }.issubset(inspected)


def test_macos_rpath_dependency_requires_unique_closure_target(
    tmp_path: Path,
) -> None:
    stager = runpy.run_path(str(ROOT / "platform-staging" / "stager.py"))
    closure = tmp_path / "closure"
    binary = closure / "bin" / "python3"
    first = closure / "one" / "libduplicate.dylib"
    second = closure / "two" / "libduplicate.dylib"
    for path in (binary, first, second):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\xcf\xfa\xed\xfe-macho")
    source_prefix = tmp_path / "source"
    source_prefix.mkdir()

    with pytest.raises(
        stager["StageError"], match="pack_python_macho_dependency_unresolved"
    ):
        stager["_macos_relocation_target"](
            "@rpath/libduplicate.dylib",
            binary=binary.resolve(),
            closure=closure.resolve(),
            source_prefix=source_prefix.resolve(),
            macho_files=(binary.resolve(), first.resolve(), second.resolve()),
        )


def test_macos_absolute_dependency_never_falls_back_to_same_basename(
    tmp_path: Path,
) -> None:
    stager = runpy.run_path(str(ROOT / "platform-staging" / "stager.py"))
    closure = tmp_path / "closure"
    binary = closure / "bin" / "python3"
    unrelated = closure / "wheel" / "libcrypto.3.dylib"
    for path in (binary, unrelated):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\xcf\xfa\xed\xfe-macho")
    source_prefix = tmp_path / "source"
    source_prefix.mkdir()

    with pytest.raises(
        stager["StageError"], match="pack_python_macho_dependency_unresolved"
    ):
        stager["_macos_relocation_target"](
            "/outside/base/lib/libcrypto.3.dylib",
            binary=binary.resolve(),
            closure=closure.resolve(),
            source_prefix=source_prefix.resolve(),
            macho_files=(binary.resolve(), unrelated.resolve()),
        )


def test_macos_base_dependencies_materialize_recursively_to_fixpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stager = runpy.run_path(str(ROOT / "platform-staging" / "stager.py"))
    closure = tmp_path / "closure"
    interpreter = closure / "bin" / "python3"
    source_prefix = tmp_path / "source"
    first_source = source_prefix / "lib" / "libssl.3.dylib"
    second_source = source_prefix / "lib" / "libcrypto.3.dylib"
    for path in (interpreter, first_source, second_source):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\xcf\xfa\xed\xfe-macho")

    globals_ = stager["_materialize_macos_python_dependencies"].__globals__
    monkeypatch.setitem(
        globals_,
        "_macos_architectures",
        lambda _path: ("x86_64",),
    )
    monkeypatch.setitem(
        globals_,
        "_macos_install_name",
        lambda _path, *, architecture: None,
    )

    def fake_dependencies(
        path: Path,
        *,
        architecture: str,
        install_name: str | None,
    ) -> tuple[str, ...]:
        del architecture, install_name
        if path.name == "python3":
            return ("fixture:first",)
        if path.name == "libssl.3.dylib":
            return ("fixture:second",)
        return ("/usr/lib/libSystem.B.dylib",)

    def fake_source(
        dependency: str,
        *,
        source_prefix: Path,
    ) -> tuple[Path, PurePosixPath] | None:
        del source_prefix
        if dependency == "fixture:first":
            return first_source.resolve(), PurePosixPath("lib/libssl.3.dylib")
        if dependency == "fixture:second":
            return second_source.resolve(), PurePosixPath("lib/libcrypto.3.dylib")
        return None

    monkeypatch.setitem(globals_, "_macos_dependencies", fake_dependencies)
    monkeypatch.setitem(globals_, "_macos_base_dependency_source", fake_source)
    source_hashes = {
        "libssl.3.dylib": hashlib.sha256(first_source.read_bytes()).hexdigest(),
        "libcrypto.3.dylib": hashlib.sha256(second_source.read_bytes()).hexdigest(),
    }
    monkeypatch.setitem(
        globals_,
        "MACOS_NATIVE_COMPONENTS",
        {
            name: SimpleNamespace(source_sha256=digest)
            for name, digest in source_hashes.items()
        },
    )

    materialized = stager["_materialize_macos_python_dependencies"](
        closure,
        source_prefix=source_prefix,
    )

    assert materialized == (
        PurePosixPath("lib/libcrypto.3.dylib"),
        PurePosixPath("lib/libssl.3.dylib"),
    )
    assert (
        closure / "lib" / "libssl.3.dylib"
    ).read_bytes() == first_source.read_bytes()
    assert (
        closure / "lib" / "libcrypto.3.dylib"
    ).read_bytes() == second_source.read_bytes()


def test_macos_base_dependency_rejects_preexisting_closure_collision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stager = runpy.run_path(str(ROOT / "platform-staging" / "stager.py"))
    closure = tmp_path / "closure"
    interpreter = closure / "bin" / "python3"
    preexisting = closure / "lib" / "libssl.3.dylib"
    source_prefix = tmp_path / "source"
    trusted_source = source_prefix / "lib" / "libssl.3.dylib"
    for path in (interpreter, preexisting, trusted_source):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\xcf\xfa\xed\xfe-macho")

    globals_ = stager["_materialize_macos_python_dependencies"].__globals__
    monkeypatch.setitem(globals_, "_macos_architectures", lambda _path: ("arm64",))
    monkeypatch.setitem(
        globals_,
        "_macos_install_name",
        lambda _path, *, architecture: None,
    )
    monkeypatch.setitem(
        globals_,
        "_macos_dependencies",
        lambda path, **_kwargs: (
            ("fixture:libssl",)
            if path.name == "python3"
            else ("/usr/lib/libSystem.B.dylib",)
        ),
    )
    monkeypatch.setitem(
        globals_,
        "_macos_base_dependency_source",
        lambda dependency, **_kwargs: (
            (trusted_source.resolve(), PurePosixPath("lib/libssl.3.dylib"))
            if dependency == "fixture:libssl"
            else None
        ),
    )
    monkeypatch.setitem(
        globals_,
        "MACOS_NATIVE_COMPONENTS",
        {
            "libssl.3.dylib": SimpleNamespace(
                source_sha256=hashlib.sha256(trusted_source.read_bytes()).hexdigest()
            )
        },
    )

    with pytest.raises(
        stager["StageError"], match="pack_python_macho_dependency_collision"
    ):
        stager["_materialize_macos_python_dependencies"](
            closure,
            source_prefix=source_prefix,
        )


@pytest.mark.skipif(
    sys.platform != "darwin", reason="POSIX absolute dependency fixture"
)
def test_macos_base_dependency_source_is_exact_confined_and_regular(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stager = runpy.run_path(str(ROOT / "platform-staging" / "stager.py"))
    source_prefix = tmp_path / "source"
    dependency = source_prefix / "lib" / "libncursesw.5.dylib"
    dependency.parent.mkdir(parents=True)
    dependency.write_bytes(b"\xcf\xfa\xed\xfe-macho")
    globals_ = stager["_macos_base_dependency_source"].__globals__
    original_contract = globals_["MACOS_NATIVE_COMPONENTS"]
    contract = original_contract[dependency.name]
    monkeypatch.setitem(
        globals_,
        "MACOS_NATIVE_COMPONENTS",
        {
            **original_contract,
            dependency.name: SimpleNamespace(
                source_sha256=hashlib.sha256(dependency.read_bytes()).hexdigest(),
                name=contract.name,
                version=contract.version,
                license=contract.license,
                notice_token=contract.notice_token,
            ),
        },
    )

    resolved = stager["_macos_base_dependency_source"](
        dependency.as_posix(),
        source_prefix=source_prefix.resolve(),
    )

    assert resolved == (
        dependency.resolve(),
        PurePosixPath("lib/libncursesw.5.dylib"),
    )
    assert (
        stager["_macos_base_dependency_source"](
            (tmp_path / "outside.dylib").as_posix(),
            source_prefix=source_prefix.resolve(),
        )
        is None
    )
    monkeypatch.setitem(globals_, "MACOS_NATIVE_COMPONENTS", original_contract)
    with pytest.raises(
        stager["StageError"],
        match="pack_python_macho_dependency_source_digest_mismatch",
    ):
        stager["_macos_base_dependency_source"](
            dependency.as_posix(),
            source_prefix=source_prefix.resolve(),
        )


@pytest.mark.skipif(sys.platform != "darwin", reason="POSIX symlink fixture")
def test_macos_base_dependency_source_rejects_symlink(
    tmp_path: Path,
) -> None:
    stager = runpy.run_path(str(ROOT / "platform-staging" / "stager.py"))
    source_prefix = tmp_path / "source"
    target = source_prefix / "lib" / "target.dylib"
    dependency = source_prefix / "lib" / "libssl.3.dylib"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"\xcf\xfa\xed\xfe-macho")
    dependency.symlink_to(target)

    with pytest.raises(
        stager["StageError"],
        match="pack_python_macho_dependency_source_invalid",
    ):
        stager["_macos_base_dependency_source"](
            dependency.as_posix(),
            source_prefix=source_prefix.resolve(),
        )


def test_macos_materialized_framework_dylib_requires_matching_installer_notice(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stager = runpy.run_path(str(ROOT / "platform-staging" / "stager.py"))
    closure = tmp_path / "closure"
    closure.mkdir()
    license_path = tmp_path / "License.rtf"
    license_path.write_bytes(b"OpenSSL 3.0.13\nNCurses 5.9\n")
    globals_ = stager["_materialize_macos_python_license"].__globals__
    monkeypatch.setitem(
        globals_,
        "_macos_installer_license_path",
        lambda _version: license_path,
    )
    monkeypatch.setitem(
        globals_,
        "PYTHON_MACOS_LICENSE",
        {
            "path": "licenses/python-macos-installer-License.rtf",
            "size_bytes": len(license_path.read_bytes()),
            "sha256": hashlib.sha256(license_path.read_bytes()).hexdigest(),
            "tokens": (b"OpenSSL 3.0.13", b"NCurses 5.9"),
        },
    )

    stager["_materialize_macos_python_license"](
        closure,
        materialized=(
            PurePosixPath("lib/libssl.3.dylib"),
            PurePosixPath("lib/libncursesw.5.dylib"),
        ),
    )

    copied = closure / "licenses" / "python-macos-installer-License.rtf"
    assert copied.read_bytes() == license_path.read_bytes()
    license_path.write_bytes(b"OpenSSL 3.0.13 only\n")
    with pytest.raises(stager["StageError"], match="pack_python_macho_license_invalid"):
        stager["_materialize_macos_python_license"](
            tmp_path / "second-closure",
            materialized=(PurePosixPath("lib/libncursesw.5.dylib"),),
        )


def test_macos_materialized_native_inventory_is_canonical_and_complete(
    tmp_path: Path,
) -> None:
    stager = runpy.run_path(str(ROOT / "platform-staging" / "stager.py"))
    closure = tmp_path / "closure"
    library = closure / "lib" / "libssl.3.dylib"
    notice = closure / "licenses" / "python-macos-installer-License.rtf"
    library.parent.mkdir(parents=True)
    notice.parent.mkdir(parents=True)
    library.write_bytes(b"relocated-libssl")
    notice.write_bytes(b"OpenSSL 3.0.13")

    stager["_write_macos_native_inventory"](
        closure,
        materialized=(PurePosixPath("lib/libssl.3.dylib"),),
        architecture="arm64",
    )

    raw = (closure / "native-components.json").read_bytes()
    assert raw.endswith(b"\n")
    value = json.loads(raw)
    assert value["architecture"] == "arm64"
    assert value["license_notice"]["path"] == (
        "licenses/python-macos-installer-License.rtf"
    )
    assert value["distribution"]["sha256"] == (
        "b6cfdee2571ca56ee895043ca1e7110fb78a878cee3eb0c21accb2de34d24b55"
    )
    assert value["components"] == [
        {
            "license": "Apache-2.0",
            "license_text": "licenses/native/openssl-3.0.13-LICENSE.txt",
            "name": "OpenSSL",
            "path": "lib/libssl.3.dylib",
            "sha256": hashlib.sha256(library.read_bytes()).hexdigest(),
            "source_sha256": (
                "22f984c4947e9ea11528ad86d219f145ae9cd45983e3850d34d781d1b38ce5d6"
            ),
            "version": "3.0.13",
        }
    ]


def test_macos_any_unclassified_materialized_library_fails_license_gate(
    tmp_path: Path,
) -> None:
    stager = runpy.run_path(str(ROOT / "platform-staging" / "stager.py"))
    closure = tmp_path / "closure"
    closure.mkdir()

    with pytest.raises(
        stager["StageError"], match="pack_python_macho_component_unclassified"
    ):
        stager["_materialize_macos_python_license"](
            closure,
            materialized=(PurePosixPath("lib/libfuture.1.dylib"),),
        )


@pytest.mark.parametrize(
    "dependency",
    ("@unknown/libpython.dylib", "/usr/lib/../tmp/libpython.dylib"),
)
def test_macos_dependency_rejects_unknown_token_and_dot_segments(
    dependency: str,
) -> None:
    stager = runpy.run_path(str(ROOT / "platform-staging" / "stager.py"))

    with pytest.raises(
        stager["StageError"], match="pack_python_macho_dependency_invalid"
    ):
        stager["_macos_dependency_requires_relocation"](
            dependency,
            source_prefix=Path("/trusted/base"),
        )


@pytest.mark.parametrize(
    ("output", "expected"),
    ((b"arm64\n", ("arm64",)), (b"x86_64 arm64\n", ("x86_64", "arm64"))),
)
def test_macos_lipo_architecture_contract_supports_thin_and_fat(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    output: bytes,
    expected: tuple[str, ...],
) -> None:
    stager = runpy.run_path(str(ROOT / "platform-staging" / "stager.py"))
    binary = tmp_path / "binary"
    binary.write_bytes(b"\xca\xfe\xba\xbf-fat")

    def fake_run(command: tuple[str, ...], **kwargs: object) -> SimpleNamespace:
        assert command == ("/usr/bin/lipo", "-archs", str(binary))
        return SimpleNamespace(stdout=output)

    monkeypatch.setitem(stager["_macos_architectures"].__globals__, "_run", fake_run)

    assert stager["_macos_architectures"](binary) == expected


def test_macos_fat_rpath_drift_fails_before_install_name_tool() -> None:
    stager = runpy.run_path(str(ROOT / "platform-staging" / "stager.py"))

    with pytest.raises(
        stager["StageError"], match="pack_python_macho_architecture_drift"
    ):
        stager["_common_macos_rpaths"](
            {
                "arm64": ("@loader_path/arm",),
                "x86_64": ("@loader_path/intel",),
            }
        )


def test_macos_otool_parsers_preserve_spaces_and_parentheses(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stager = runpy.run_path(str(ROOT / "platform-staging" / "stager.py"))
    binary = tmp_path / "binary"
    binary.write_bytes(b"\xcf\xfa\xed\xfe-thin")
    dependency = (
        "/private/tool cache/lib (compatibility version archive)/libpython.dylib"
    )
    rpath = "@loader_path/runtime (offset archive)/lib"

    def fake_run(command: tuple[str, ...], **kwargs: object) -> SimpleNamespace:
        operation = command[3]
        if operation == "-L":
            return SimpleNamespace(
                stdout=(
                    f"{binary} (architecture arm64):\n"
                    f"\t{dependency} (compatibility version 1.0.0, "
                    "current version 1.0.0)\n"
                ).encode("utf-8")
            )
        assert operation == "-l"
        return SimpleNamespace(
            stdout=(
                "Load command 0\n"
                "          cmd LC_RPATH\n"
                "      cmdsize 96\n"
                f"         path {rpath} (offset 12)\n"
            ).encode("utf-8")
        )

    parser_globals = stager["_macos_dependencies"].__globals__
    monkeypatch.setitem(parser_globals, "_run", fake_run)

    assert stager["_macos_dependencies"](
        binary,
        architecture="arm64",
        install_name=None,
    ) == (dependency,)
    assert stager["_macos_rpaths"](
        binary,
        architecture="arm64",
    ) == (rpath,)


def test_macos_pack_probe_denies_source_and_framework_reads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stager = runpy.run_path(str(ROOT / "platform-staging" / "stager.py"))
    source_prefix = tmp_path / "toolcache-python"
    source_prefix.mkdir()
    source_canary = source_prefix / "python-source-canary"
    source_canary.write_bytes(b"readable-before-sandbox")
    framework_root = tmp_path / "Library" / "Frameworks" / "Python.framework"
    framework_root.mkdir(parents=True)
    core = tmp_path / "core"
    core.mkdir()
    positive_canary = core / "pack-python.json"
    positive_canary.write_bytes(b"signed-pack-manifest")
    interpreter = core / "pack-python" / "bin" / "python3"
    interpreter.parent.mkdir(parents=True)
    interpreter.write_bytes(b"python")
    original_is_file = Path.is_file
    environments: list[dict[str, str]] = []

    def fake_is_file(path: Path) -> bool:
        if path.as_posix() in {
            "/usr/bin/sandbox-exec",
            "/usr/bin/true",
            "/bin/cat",
        }:
            return True
        return original_is_file(path)

    monkeypatch.setattr(Path, "is_file", fake_is_file)
    probe_globals = stager["_run_macos_isolated_pack_probe"].__globals__
    monkeypatch.setitem(probe_globals, "_PYTHON_FRAMEWORK_ROOT", framework_root)
    commands: list[tuple[str, ...]] = []

    def fake_bounded_process(
        command: tuple[str, ...], **kwargs: object
    ) -> SimpleNamespace:
        commands.append(command)
        environments.append(dict(kwargs["environment"]))  # type: ignore[arg-type]
        if Path(command[-2]).as_posix() == "/bin/cat":
            if (
                Path(command[-1]).name == "pack-python.json"
                and Path(command[-1]) != positive_canary
            ):
                return SimpleNamespace(
                    returncode=0,
                    stdout=b"signed-pack-manifest",
                    stderr=b"",
                )
            return SimpleNamespace(returncode=1, stdout=b"", stderr=b"denied")
        if command == probe or command[-4:] == probe[1:]:
            return SimpleNamespace(returncode=0, stdout=b"1.0.0\n", stderr=b"")
        if command[-1] == "print('__ECOREX_PACK_BOOTSTRAP_OK__')":
            return SimpleNamespace(
                returncode=0,
                stdout=b"__ECOREX_PACK_BOOTSTRAP_OK__\n",
                stderr=b"",
            )
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setitem(
        probe_globals,
        "run_bounded_process",
        fake_bounded_process,
    )
    probe = (str(interpreter), "-I", "-B", "-c", "full-pack-probe")

    result = stager["_run_macos_isolated_pack_probe"](
        probe,
        cwd=core,
        source_prefix=source_prefix,
        source_canary=source_canary,
    )

    assert result.stdout == b"1.0.0\n"
    assert len(commands) == 8
    profile_check = commands[0]
    assert Path(profile_check[-1]).as_posix() == "/usr/bin/true"
    positive = commands[1]
    assert Path(positive[-2]).as_posix() == "/bin/cat"
    assert Path(positive[-1]).name == "pack-python.json"
    assert Path(positive[-1]) != positive_canary
    source_denial = commands[2]
    assert Path(source_denial[-2]).as_posix() == "/bin/cat"
    assert Path(source_denial[-1]) == source_canary
    canonical_denial = commands[3]
    assert Path(canonical_denial[-2]).as_posix() == "/bin/cat"
    assert Path(canonical_denial[-1]) == positive_canary
    isolated_bootstrap = commands[4]
    assert isolated_bootstrap[-1] == "print('__ECOREX_PACK_BOOTSTRAP_OK__')"
    command = commands[5]
    assert Path(command[0]).as_posix() == "/usr/bin/sandbox-exec"
    assert command[1] == "-p"
    assert command[-4:] == probe[1:]
    profile = command[2]
    baseline_bootstrap = commands[6]
    assert baseline_bootstrap[-1] == "print('__ECOREX_PACK_BOOTSTRAP_OK__')"
    baseline = commands[7]
    assert baseline[-4:] == probe[1:]
    assert environments[4]["TMPDIR"] == environments[5]["TMPDIR"]
    assert environments[6]["TMPDIR"] == environments[7]["TMPDIR"]
    assert environments[4]["TMPDIR"] != environments[6]["TMPDIR"]
    isolated_root_literal = stager["_seatbelt_literal"](
        Path(environments[4]["TMPDIR"]).parent
    )
    baseline_root_literal = stager["_seatbelt_literal"](
        Path(environments[6]["TMPDIR"]).parent
    )
    assert f'(deny file-write* (subpath "{baseline_root_literal}"))' in profile
    assert f'(deny file-write* (subpath "{isolated_root_literal}"))' in baseline[2]
    source_literal = stager["_seatbelt_literal"](source_prefix.resolve())
    assert f'(deny file-read* (subpath "{source_literal}"))' in profile
    framework = stager["_seatbelt_literal"](framework_root)
    assert f'(deny file-read* (subpath "{framework}"))' in profile
    core_literal = stager["_seatbelt_literal"](core.resolve())
    assert f'(deny file-read* (subpath "{core_literal}"))' in profile
    assert f'(deny file-read* (subpath "{core_literal}"))' in baseline[2]
    assert f'(deny file-write* (subpath "{core_literal}"))' in profile
    assert f'(deny file-write* (subpath "{core_literal}"))' in baseline[2]
    assert f'(deny file-read* (subpath "{source_literal}"))' not in baseline[2]
    assert f'(deny file-read* (subpath "{framework}"))' not in baseline[2]
    for command_with_profile in (
        isolated_bootstrap,
        command,
        baseline_bootstrap,
        baseline,
    ):
        assert f'(deny file-write* (subpath "{source_literal}"))' in (
            command_with_profile[2]
        )
        assert f'(deny file-write* (subpath "{framework}"))' in (
            command_with_profile[2]
        )
    assert Path(source_denial[0]).as_posix() == "/usr/bin/sandbox-exec"


def test_macos_pack_probe_classifies_bootstrap_only_after_combined_denial_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stager = runpy.run_path(str(ROOT / "platform-staging" / "stager.py"))
    source_prefix = tmp_path / "toolcache-python"
    source_prefix.mkdir()
    source_canary = source_prefix / "python-source-canary"
    source_canary.write_bytes(b"readable-before-sandbox")
    framework_root = tmp_path / "Library" / "Frameworks" / "Python.framework"
    framework_root.mkdir(parents=True)
    core = tmp_path / "core"
    core.mkdir()
    (core / "pack-python.json").write_bytes(b"signed-pack-manifest")
    interpreter = core / "pack-python" / "bin" / "python3"
    interpreter.parent.mkdir(parents=True)
    interpreter.write_bytes(b"python")
    interpreter.chmod(0o755)
    original_is_file = Path.is_file

    def fake_is_file(path: Path) -> bool:
        if path.as_posix() in {
            "/usr/bin/sandbox-exec",
            "/usr/bin/true",
            "/bin/cat",
        }:
            return True
        return original_is_file(path)

    monkeypatch.setattr(Path, "is_file", fake_is_file)
    probe_globals = stager["_run_macos_isolated_pack_probe"].__globals__
    monkeypatch.setitem(probe_globals, "_PYTHON_FRAMEWORK_ROOT", framework_root)
    commands: list[tuple[str, ...]] = []

    def fake_bounded_process(
        command: tuple[str, ...], **kwargs: object
    ) -> SimpleNamespace:
        commands.append(command)
        if Path(command[-2]).as_posix() == "/bin/cat":
            if Path(command[-1]).name == "pack-python.json" and Path(
                command[-1]
            ) != (core / "pack-python.json"):
                return SimpleNamespace(
                    returncode=0,
                    stdout=b"signed-pack-manifest",
                    stderr=b"",
                )
            return SimpleNamespace(returncode=1, stdout=b"", stderr=b"denied")
        if command[-1] == "print('__ECOREX_PACK_BOOTSTRAP_OK__')":
            return SimpleNamespace(returncode=1, stdout=b"", stderr=b"private")
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    diagnosed_after: list[int] = []

    def fake_diagnostic(**kwargs: object) -> str:
        diagnosed_after.append(len(commands))
        assert stager["_seatbelt_literal"](source_prefix.resolve()) in str(
            kwargs["source_rules"]
        )
        assert stager["_seatbelt_literal"](framework_root.resolve()) in str(
            kwargs["framework_rules"]
        )
        write_rules = str(kwargs["source_framework_write_rules"])
        assert stager["_seatbelt_literal"](source_prefix.resolve()) in write_rules
        assert stager["_seatbelt_literal"](framework_root.resolve()) in write_rules
        return "pack_python_sandbox_bootstrap_source_dependency_failed"

    monkeypatch.setitem(probe_globals, "run_bounded_process", fake_bounded_process)
    monkeypatch.setitem(
        probe_globals,
        "_diagnose_macos_bootstrap_execution_failure",
        fake_diagnostic,
    )

    with pytest.raises(stager["StageError"]) as raised:
        stager["_run_macos_isolated_pack_probe"](
            (str(interpreter), "-I", "-B", "-c", "full-pack-probe"),
            cwd=core,
            source_prefix=source_prefix,
            source_canary=source_canary,
        )

    assert raised.value.code == (
        "pack_python_sandbox_bootstrap_source_dependency_failed"
    )
    assert diagnosed_after == [5]
    assert commands[4][-1] == "print('__ECOREX_PACK_BOOTSTRAP_OK__')"
    combined_profile = commands[4][2]
    assert stager["_seatbelt_literal"](source_prefix.resolve()) in combined_profile
    assert stager["_seatbelt_literal"](framework_root.resolve()) in combined_profile


@pytest.mark.parametrize(
    ("outcomes", "expected"),
    (
        (
            (False,),
            "pack_python_sandbox_bootstrap_baseline_policy_failed",
        ),
        (
            (True, False, True),
            "pack_python_sandbox_bootstrap_source_write_dependency_failed",
        ),
        (
            (True, True, False),
            "pack_python_sandbox_bootstrap_framework_write_dependency_failed",
        ),
        (
            (True, False, False),
            "pack_python_sandbox_bootstrap_source_and_framework_write_dependency_failed",
        ),
        (
            (True, True, True, False),
            "pack_python_sandbox_bootstrap_combined_write_policy_failed",
        ),
        (
            (True, True, True, True, False, True),
            "pack_python_sandbox_bootstrap_source_dependency_failed",
        ),
        (
            (True, True, True, True, True, False),
            "pack_python_sandbox_bootstrap_framework_dependency_failed",
        ),
        (
            (True, True, True, True, False, False),
            "pack_python_sandbox_bootstrap_source_and_framework_dependency_failed",
        ),
        (
            (True, True, True, True, True, True),
            "pack_python_sandbox_bootstrap_combined_policy_failed",
        ),
    ),
)
def test_macos_bootstrap_diagnostic_maps_only_stable_failure_classes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    outcomes: tuple[bool, ...],
    expected: str,
) -> None:
    stager = runpy.run_path(str(ROOT / "platform-staging" / "stager.py"))
    diagnostic_globals = stager[
        "_diagnose_macos_bootstrap_execution_failure"
    ].__globals__
    canonical_core = tmp_path / "canonical" / "core"
    baseline_core = tmp_path / "baseline" / "core"
    isolated_core = tmp_path / "isolated" / "core"
    interpreter = canonical_core / "pack-python" / "bin" / "python3"
    interpreter.parent.mkdir(parents=True)
    interpreter.write_bytes(b"python")
    stager["_copy_tree"](canonical_core, baseline_core)
    stager["_copy_tree"](canonical_core, isolated_core)
    binding = stager["_tree_binding_sha256"](canonical_core)
    observed: list[tuple[str, Path]] = []
    direct_observed: list[Path] = []
    remaining = list(outcomes)

    monkeypatch.setitem(
        diagnostic_globals,
        "_macos_snapshot_signatures_valid",
        lambda _root: True,
    )
    def fake_direct(**kwargs: object) -> bool:
        direct_observed.append(Path(kwargs["temporary_directory"]))
        return True

    monkeypatch.setitem(
        diagnostic_globals, "_macos_bootstrap_direct_succeeds", fake_direct
    )

    def fake_succeeds(**kwargs: object) -> bool:
        observed.append((str(kwargs["profile"]), Path(kwargs["temporary_directory"])))
        return remaining.pop(0)

    monkeypatch.setitem(
        diagnostic_globals,
        "_macos_bootstrap_diagnostic_succeeds",
        fake_succeeds,
    )

    result = stager["_diagnose_macos_bootstrap_execution_failure"](
        sandbox=Path("/usr/bin/sandbox-exec"),
        canonical_core=canonical_core,
        baseline_core=baseline_core,
        isolated_core=isolated_core,
        baseline_root=baseline_core.parent,
        isolated_root=isolated_core.parent,
        interpreter_relative=Path("pack-python/bin/python3"),
        canonical_rules="canonical-rules;",
        source_rules="source-deny;",
        framework_rules="framework-deny;",
        source_write_rules="source-write-deny;",
        framework_write_rules="framework-write-deny;",
        source_framework_write_rules="source-write-deny;framework-write-deny;",
        canonical_binding=binding,
    )

    assert result == expected
    assert not remaining
    assert len({temporary for _profile, temporary in observed}) == len(observed)
    assert len(direct_observed) == 1
    assert len(observed) == len(outcomes)
    diagnostic_parent = direct_observed[0].parent.parent
    diagnostic_roots = tuple(
        diagnostic_parent / name
        for name in (
            "direct",
            "baseline",
            "source-write-denied",
            "framework-write-denied",
            "all-write-denied",
            "source-read-denied",
            "framework-read-denied",
        )
    )
    for index, (profile, _temporary) in enumerate(observed, start=1):
        assert "canonical-rules;" in profile
        for peer_index, peer in enumerate(diagnostic_roots):
            if peer_index == index:
                continue
            literal = stager["_seatbelt_literal"](peer)
            assert f'(deny file-read* (subpath "{literal}"))' in profile
            assert f'(deny file-map-executable (subpath "{literal}"))' in profile
            assert f'(deny file-write* (subpath "{literal}"))' in profile
    expected_policy_fragments = (
        (),
        ("source-write-deny;",),
        ("framework-write-deny;",),
        ("source-write-deny;framework-write-deny;",),
        ("source-deny;",),
        ("framework-deny;",),
    )
    for (profile, _temporary), fragments in zip(
        observed, expected_policy_fragments, strict=False
    ):
        for fragment in fragments:
            assert fragment in profile
    assert "source-write-deny;" not in observed[0][0]
    assert "framework-write-deny;" not in observed[0][0]


def test_macos_bootstrap_diagnostic_rejects_invalid_snapshot_signature(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stager = runpy.run_path(str(ROOT / "platform-staging" / "stager.py"))
    diagnostic_globals = stager[
        "_diagnose_macos_bootstrap_execution_failure"
    ].__globals__
    canonical_core = tmp_path / "canonical" / "core"
    baseline_core = tmp_path / "baseline" / "core"
    isolated_core = tmp_path / "isolated" / "core"
    canonical_core.mkdir(parents=True)
    (canonical_core / "member").write_bytes(b"member")
    stager["_copy_tree"](canonical_core, baseline_core)
    stager["_copy_tree"](canonical_core, isolated_core)
    binding = stager["_tree_binding_sha256"](canonical_core)
    called = False

    monkeypatch.setitem(
        diagnostic_globals,
        "_macos_snapshot_signatures_valid",
        lambda root: root == baseline_core,
    )

    def unexpected_probe(**_kwargs: object) -> bool:
        nonlocal called
        called = True
        return True

    monkeypatch.setitem(
        diagnostic_globals,
        "_macos_bootstrap_diagnostic_succeeds",
        unexpected_probe,
    )

    result = stager["_diagnose_macos_bootstrap_execution_failure"](
        sandbox=Path("/usr/bin/sandbox-exec"),
        canonical_core=canonical_core,
        baseline_core=baseline_core,
        isolated_core=isolated_core,
        baseline_root=baseline_core.parent,
        isolated_root=isolated_core.parent,
        interpreter_relative=Path("pack-python/bin/python3"),
        canonical_rules="canonical-rules;",
        source_rules="source-deny;",
        framework_rules="framework-deny;",
        source_write_rules="source-write-deny;",
        framework_write_rules="framework-write-deny;",
        source_framework_write_rules="source-framework-write-deny;",
        canonical_binding=binding,
    )

    assert result == "pack_python_sandbox_bootstrap_snapshot_signature_invalid"
    assert called is False


def test_macos_bootstrap_diagnostic_prioritizes_stage_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stager = runpy.run_path(str(ROOT / "platform-staging" / "stager.py"))
    diagnostic = stager["_diagnose_macos_bootstrap_execution_failure"]
    diagnostic_globals = diagnostic.__globals__
    canonical_core = tmp_path / "canonical" / "core"
    baseline_core = tmp_path / "baseline" / "core"
    isolated_core = tmp_path / "isolated" / "core"
    interpreter_relative = Path("pack-python/bin/python3")
    interpreter = canonical_core / interpreter_relative
    interpreter.parent.mkdir(parents=True)
    interpreter.write_bytes(b"python")
    stager["_copy_tree"](canonical_core, baseline_core)
    stager["_copy_tree"](canonical_core, isolated_core)
    binding = stager["_tree_binding_sha256"](canonical_core)
    sandbox_calls = 0

    monkeypatch.setitem(
        diagnostic_globals,
        "_macos_snapshot_signatures_valid",
        lambda _root: True,
    )
    monkeypatch.setitem(
        diagnostic_globals,
        "_macos_bootstrap_direct_succeeds",
        lambda **_kwargs: True,
    )

    def mutate_then_fail(**_kwargs: object) -> bool:
        nonlocal sandbox_calls
        sandbox_calls += 1
        (canonical_core / "unexpected").write_bytes(b"mutation")
        return False

    monkeypatch.setitem(
        diagnostic_globals,
        "_macos_bootstrap_diagnostic_succeeds",
        mutate_then_fail,
    )

    result = diagnostic(
        sandbox=Path("/usr/bin/sandbox-exec"),
        canonical_core=canonical_core,
        baseline_core=baseline_core,
        isolated_core=isolated_core,
        baseline_root=baseline_core.parent,
        isolated_root=isolated_core.parent,
        interpreter_relative=interpreter_relative,
        canonical_rules="canonical-rules;",
        source_rules="source-deny;",
        framework_rules="framework-deny;",
        source_write_rules="source-write-deny;",
        framework_write_rules="framework-write-deny;",
        source_framework_write_rules="source-framework-write-deny;",
        canonical_binding=binding,
    )

    assert result == "pack_python_probe_snapshot_mutated"
    assert sandbox_calls == 1


def test_macos_bootstrap_diagnostic_separates_direct_snapshot_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stager = runpy.run_path(str(ROOT / "platform-staging" / "stager.py"))
    diagnostic = stager["_diagnose_macos_bootstrap_execution_failure"]
    diagnostic_globals = diagnostic.__globals__
    canonical_core = tmp_path / "canonical" / "core"
    baseline_core = tmp_path / "baseline" / "core"
    isolated_core = tmp_path / "isolated" / "core"
    interpreter_relative = Path("pack-python/bin/python3")
    interpreter = canonical_core / interpreter_relative
    interpreter.parent.mkdir(parents=True)
    interpreter.write_bytes(b"python")
    (baseline_core.parent / "tmp").mkdir(parents=True)
    stager["_copy_tree"](canonical_core, baseline_core)
    stager["_copy_tree"](canonical_core, isolated_core)
    binding = stager["_tree_binding_sha256"](canonical_core)
    sandbox_called = False
    direct_observed: dict[str, Path] = {}

    monkeypatch.setitem(
        diagnostic_globals,
        "_macos_snapshot_signatures_valid",
        lambda _root: True,
    )
    def direct_fails(**kwargs: object) -> bool:
        direct_observed["cwd"] = Path(kwargs["cwd"])
        direct_observed["interpreter"] = Path(kwargs["interpreter"])
        direct_observed["temporary"] = Path(kwargs["temporary_directory"])
        return False

    monkeypatch.setitem(
        diagnostic_globals, "_macos_bootstrap_direct_succeeds", direct_fails
    )

    def unexpected_sandbox(**_kwargs: object) -> bool:
        nonlocal sandbox_called
        sandbox_called = True
        return True

    monkeypatch.setitem(
        diagnostic_globals,
        "_macos_bootstrap_diagnostic_succeeds",
        unexpected_sandbox,
    )

    result = diagnostic(
        sandbox=Path("/usr/bin/sandbox-exec"),
        canonical_core=canonical_core,
        baseline_core=baseline_core,
        isolated_core=isolated_core,
        baseline_root=baseline_core.parent,
        isolated_root=isolated_core.parent,
        interpreter_relative=interpreter_relative,
        canonical_rules="canonical-rules;",
        source_rules="source-deny;",
        framework_rules="framework-deny;",
        source_write_rules="source-write-deny;",
        framework_write_rules="framework-write-deny;",
        source_framework_write_rules="source-framework-write-deny;",
        canonical_binding=binding,
    )

    assert result == "pack_python_bootstrap_snapshot_direct_execution_failed"
    assert sandbox_called is False
    assert direct_observed["interpreter"].is_relative_to(direct_observed["cwd"])
    assert direct_observed["temporary"].parent == direct_observed["cwd"].parent
    assert direct_observed["cwd"] not in {
        canonical_core,
        baseline_core,
        isolated_core,
    }


@pytest.mark.parametrize(
    ("returncode", "suffix"),
    (
        (81, "bootstrap_failed"),
        (82, "native_imports_failed"),
        (83, "asgi_imports_failed"),
        (84, "resources_failed"),
        (85, "tzdata_failed"),
        (86, "image_codec_failed"),
    ),
)
def test_macos_pack_probe_maps_only_fixed_failure_phases(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    returncode: int,
    suffix: str,
) -> None:
    stager = runpy.run_path(str(ROOT / "platform-staging" / "stager.py"))
    probe_globals = stager["_run_macos_pack_probe_process"].__globals__

    def fake_bounded_process(
        command: tuple[str, ...], **kwargs: object
    ) -> SimpleNamespace:
        return SimpleNamespace(returncode=returncode, stdout=b"", stderr=b"private")

    monkeypatch.setitem(probe_globals, "run_bounded_process", fake_bounded_process)

    with pytest.raises(stager["StageError"]) as raised:
        stager["_run_macos_pack_probe_process"](
            ("pack-python", "-c", "probe"),
            cwd=tmp_path,
            code_prefix="pack_python_probe",
        )

    assert raised.value.code == f"pack_python_probe_{suffix}"


def test_macos_pack_probe_recovers_phase_when_sandbox_normalizes_exit_code(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stager = runpy.run_path(str(ROOT / "platform-staging" / "stager.py"))
    probe_globals = stager["_run_macos_pack_probe_process"].__globals__

    def fake_bounded_process(
        command: tuple[str, ...], **kwargs: object
    ) -> SimpleNamespace:
        return SimpleNamespace(
            returncode=1,
            stdout=b"__ECOREX_PACK_PROBE_NATIVE_IMPORTS_FAILED__\n",
            stderr=b"private",
        )

    monkeypatch.setitem(probe_globals, "run_bounded_process", fake_bounded_process)

    with pytest.raises(stager["StageError"]) as raised:
        stager["_run_macos_pack_probe_process"](
            ("sandbox-exec", "pack-python"),
            cwd=tmp_path,
            code_prefix="pack_python_sandbox_probe",
        )

    assert raised.value.code == "pack_python_sandbox_probe_native_imports_failed"


def test_pack_python_base_member_cannot_escape_closure_authority(
    tmp_path: Path,
) -> None:
    stager = runpy.run_path(str(ROOT / "platform-staging" / "stager.py"))
    prefix = tmp_path / "base"
    prefix.mkdir()
    outside = tmp_path / "outside-python"
    outside.write_bytes(b"untrusted")

    with pytest.raises(stager["StageError"], match="pack_python_base_runtime_invalid"):
        stager["_base_runtime_member"](
            outside,
            prefix=prefix.resolve(),
            directory=False,
        )


@pytest.mark.parametrize("directory", [False, True], ids=["file", "directory"])
def test_pack_python_base_member_rejects_original_symlink(
    tmp_path: Path,
    directory: bool,
) -> None:
    stager = runpy.run_path(str(ROOT / "platform-staging" / "stager.py"))
    prefix = tmp_path / "base"
    prefix.mkdir()
    target = prefix / ("real-directory" if directory else "real-file")
    if directory:
        target.mkdir()
    else:
        target.write_bytes(b"real-runtime-member")
    candidate = prefix / ("directory-link" if directory else "file-link")
    try:
        candidate.symlink_to(target, target_is_directory=directory)
    except (NotImplementedError, OSError) as error:
        pytest.skip(f"symlink creation is unavailable: {error}")

    with pytest.raises(stager["StageError"], match="pack_python_base_runtime_invalid"):
        stager["_base_runtime_member"](
            candidate,
            prefix=prefix.resolve(),
            directory=directory,
        )


@pytest.mark.parametrize("directory", [False, True], ids=["file", "directory"])
@pytest.mark.parametrize("marker", ["symlink", "reparse"])
def test_pack_python_base_member_rejects_original_link_marker_before_resolve(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    directory: bool,
    marker: str,
) -> None:
    stager = runpy.run_path(str(ROOT / "platform-staging" / "stager.py"))
    prefix = tmp_path / "base"
    prefix.mkdir()
    candidate = prefix / ("reparse-directory" if directory else "reparse-file")
    if directory:
        candidate.mkdir()
    else:
        candidate.write_bytes(b"runtime-member")
    resolved_prefix = prefix.resolve()
    original_lstat = Path.lstat
    original_resolve = Path.resolve
    candidate_metadata = original_lstat(candidate)
    simulated_link = SimpleNamespace(
        st_mode=(
            stat.S_IFLNK | stat.S_IMODE(candidate_metadata.st_mode)
            if marker == "symlink"
            else candidate_metadata.st_mode
        ),
        st_file_attributes=(
            getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
            if marker == "reparse"
            else 0
        ),
        st_reparse_tag=0,
    )

    def fake_lstat(path: Path) -> object:
        if path == candidate:
            return simulated_link
        return original_lstat(path)

    def reject_candidate_resolve(path: Path, strict: bool = False) -> Path:
        if path == candidate:
            raise AssertionError("link candidate must be rejected before resolve")
        return original_resolve(path, strict=strict)

    monkeypatch.setattr(Path, "lstat", fake_lstat)
    monkeypatch.setattr(Path, "resolve", reject_candidate_resolve)

    with pytest.raises(stager["StageError"], match="pack_python_base_runtime_invalid"):
        stager["_base_runtime_member"](
            candidate,
            prefix=resolved_prefix,
            directory=directory,
        )


def test_platform_stager_bundles_and_forces_signed_iana_timezone_data() -> None:
    stager = runpy.run_path(str(ROOT / "platform-staging" / "stager.py"))

    assert "tzdata" in stager["_RUNTIME_DISTRIBUTIONS"]
    assert "python-multipart" in stager["_RUNTIME_DISTRIBUTIONS"]
    assert stager["_runtime_environment"]()["PYTHONTZPATH"] == ""
    probe = stager["_pack_python_probe_command"](Path("pack-python"))
    assert probe[:4] == ("pack-python", "-I", "-B", "-c")
    compile(probe[4], "<pack-python-probe>", "exec")
    assert "from ecorex.permission_bridge import verified_runtime_full_access" in probe[4]
    assert "import cryptography" in probe[4]
    assert "import fastapi,httpx,pydantic,uvicorn,websockets" in probe[4]
    assert "from PIL import Image" in probe[4]
    assert "__ECOREX_PACK_PROBE_IMAGE_CODEC_FAILED__" in probe[4]
    assert "import certifi" in probe[4]
    assert "import tzdata,zoneinfo" in probe[4]
    for returncode in range(81, 87):
        assert f"raise SystemExit({returncode})" in probe[4]
    assert "AdminWebAssets.load" in probe[4]
    assert "certifi.where" in probe[4]
    assert "from multipart.multipart import parse_options_header" in probe[4]
    assert "zoneinfo.reset_tzpath(())" in probe[4]
    assert "zoneinfo.ZoneInfo('Asia/Shanghai')" in probe[4]


def test_platform_stager_compacts_only_zip_safe_runtime_imports(
    tmp_path: Path,
) -> None:
    stager = runpy.run_path(str(ROOT / "platform-staging" / "stager.py"))
    runtime = tmp_path / "pack-python"
    stdlib = runtime / "Lib"
    packages = stdlib / "site-packages"
    (stdlib / "encodings").mkdir(parents=True)
    (stdlib / "encodings" / "__init__.py").write_text("codec = True\n")
    (stdlib / "pathdata").mkdir()
    (stdlib / "pathdata" / "__init__.py").write_text("value = True\n")
    (stdlib / "pathdata" / "template.txt").write_text("keep-on-disk\n")
    (packages / "ecorex").mkdir(parents=True)
    (packages / "ecorex" / "__init__.py").write_text("version = 'test'\n")
    (packages / "ecorex" / "asset.json").write_text("{}\n")
    (packages / "native_pkg").mkdir()
    (packages / "native_pkg" / "__init__.py").write_text("native = True\n")
    (packages / "native_pkg" / "speedups.pyd").write_bytes(b"native")
    (packages / "demo-1.0.dist-info").mkdir()
    (packages / "demo-1.0.dist-info" / "METADATA").write_text("Name: demo\n")
    (packages / "typing_extensions.py").write_text("value = True\n")
    (packages / "policy.pth").write_text("do-not-archive\n")

    evidence = stager["_compact_python_import_closure"](
        runtime,
        target_stdlib=stdlib,
        site_packages=packages,
        platform="windows",
    )

    archive_path = (
        runtime / f"python{sys.version_info.major}{sys.version_info.minor}.zip"
    )
    assert evidence["relative_path"] == archive_path.name
    assert evidence["member_count"] == 5
    with zipfile.ZipFile(archive_path) as archive:
        assert set(archive.namelist()) == {
            "demo-1.0.dist-info/METADATA",
            "ecorex/__init__.py",
            "ecorex/asset.json",
            "encodings/__init__.py",
            "typing_extensions.py",
        }
    assert not (stdlib / "encodings").exists()
    assert (stdlib / "pathdata" / "template.txt").is_file()
    assert (packages / "native_pkg" / "speedups.pyd").is_file()
    assert (packages / "native_pkg" / "__init__.py").is_file()
    assert (packages / "policy.pth").is_file()


def test_platform_stager_prunes_only_cpython_macos_build_support(
    tmp_path: Path,
) -> None:
    stager = runpy.run_path(str(ROOT / "platform-staging" / "stager.py"))
    version = f"{sys.version_info.major}.{sys.version_info.minor}"
    stdlib = tmp_path / "lib" / f"python{version}"
    config = stdlib / f"config-{sys.version_info.major}.{sys.version_info.minor}-darwin"
    config.mkdir(parents=True)
    libpython = b"\xcf\xfa\xed\xfe" + b"canonical-libpython"
    (stdlib.parent / f"libpython{version}.dylib").write_bytes(libpython)
    payloads = {
        "Makefile": b"makefile",
        "Setup": b"setup",
        "Setup.bootstrap": b"setup-bootstrap",
        "Setup.local": b"setup-local",
        "Setup.stdlib": b"setup-stdlib",
        "config.c": b"config-c",
        "config.c.in": b"config-c-in",
        "install-sh": b"install-sh",
        f"libpython{version}.a": libpython,
        f"libpython{version}.dylib": libpython,
        "makesetup": b"makesetup",
        "python-config.py": b"python-config",
        "python.o": b"\xcf\xfa\xed\xfe" + b"object",
    }
    for name, payload in payloads.items():
        (config / name).write_bytes(payload)
    preserved = stdlib / "configparser"
    preserved.mkdir()
    (preserved / "__init__.py").write_text("value = True\n", encoding="utf-8")

    stager["_prune_macos_cpython_build_support"](stdlib)

    assert not config.exists()
    assert (preserved / "__init__.py").is_file()


def test_platform_stager_refuses_unknown_cpython_build_support_member(
    tmp_path: Path,
) -> None:
    stager = runpy.run_path(str(ROOT / "platform-staging" / "stager.py"))
    version = f"{sys.version_info.major}.{sys.version_info.minor}"
    stdlib = tmp_path / "lib" / f"python{version}"
    config = stdlib / f"config-{version}-darwin"
    config.mkdir(parents=True)
    (stdlib.parent / f"libpython{version}.dylib").write_bytes(b"canonical")
    (config / "unexpected-runtime.dylib").write_bytes(b"runtime")

    with pytest.raises(
        stager["StageError"], match="pack_python_build_support_contract_mismatch"
    ):
        stager["_prune_macos_cpython_build_support"](stdlib)
    assert (config / "unexpected-runtime.dylib").is_file()


@pytest.mark.parametrize("suffix", (".a", ".bc", ".obj", ".rlib", ".O"))
def test_platform_stager_rejects_unclassified_macos_build_object(
    tmp_path: Path, suffix: str,
) -> None:
    stager = runpy.run_path(str(ROOT / "platform-staging" / "stager.py"))
    runtime = tmp_path / "pack-python"
    archive = runtime / "site-packages" / f"unexpected{suffix}"
    archive.parent.mkdir(parents=True)
    archive.write_bytes(b"!<arch>\n")

    with pytest.raises(
        stager["StageError"], match="pack_python_build_object_unclassified"
    ):
        stager["_reject_macos_build_objects"](runtime)


def test_platform_stager_rejects_nonportable_full_macos_signature_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    stager = runpy.run_path(str(ROOT / "platform-staging" / "stager.py"))
    closure = tmp_path / "pack-python"
    closure.mkdir()
    (closure / "member").write_bytes(b"signed-bytes")
    monkeypatch.setitem(
        stager,
        "_macos_snapshot_signatures_valid",
        lambda _root: False,
    )
    stager["_assert_macos_signature_copy_stability"].__globals__.update(
        {"_macos_snapshot_signatures_valid": stager["_macos_snapshot_signatures_valid"]}
    )

    with pytest.raises(
        stager["StageError"], match="pack_python_macho_signature_not_portable"
    ):
        stager["_assert_macos_signature_copy_stability"](closure)


def test_platform_supply_chain_scans_compacted_import_archive(
    tmp_path: Path,
) -> None:
    stager = runpy.run_path(str(ROOT / "platform-staging" / "stager.py"))
    archive = tmp_path / "python311.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as output:
        output.writestr(
            "module.py",
            b"TOKEN = 'ghp_abcdefghijklmnopqrstuvwxyz123456'\n",
        )

    with pytest.raises(
        stager["StageError"], match="stage_supply_chain_secret_match"
    ) as rejected:
        stager["_supply_chain"](
            tmp_path,
            (),
            lock_profile="runtime",
            require_complete=False,
        )
    archive_payload = b"TOKEN = 'ghp_abcdefghijklmnopqrstuvwxyz123456'\n"
    assert rejected.value.diagnostic == {
        "content_sha256": hashlib.sha256(archive_payload).hexdigest(),
        "detector_id": "github_token",
        "kind": "archive_member",
        "location_sha256": hashlib.sha256(
            b"python311.zip!/module.py"
        ).hexdigest(),
    }

    unsafe_root = tmp_path / "unsafe"
    unsafe_root.mkdir()
    with zipfile.ZipFile(unsafe_root / "python311.zip", "w") as output:
        output.writestr("Package/module.py", b"first = True\n")
        output.writestr("package/module.py", b"second = True\n")
    with pytest.raises(
        stager["StageError"], match="stage_supply_chain_archive_invalid"
    ):
        stager["_supply_chain"](
            unsafe_root,
            (),
            lock_profile="runtime",
            require_complete=False,
        )

    noncanonical_root = tmp_path / "noncanonical"
    noncanonical_root.mkdir()
    with zipfile.ZipFile(noncanonical_root / "python311.zip", "w") as output:
        output.writestr("package//module.py", b"value = True\n")
    with pytest.raises(
        stager["StageError"], match="stage_supply_chain_archive_invalid"
    ):
        stager["_supply_chain"](
            noncanonical_root,
            (),
            lock_profile="runtime",
            require_complete=False,
        )


def retired_legacy_platform_stager_emits_runtime_canonical_process_pack_descriptor(
    tmp_path: Path,
) -> None:
    pack_id = "browser"
    stager = runpy.run_path(str(ROOT / "platform-staging" / "stager.py"))
    pack = tmp_path / pack_id
    pack.mkdir()
    expected = stager["_expected_process_pack_descriptor"](pack_id)
    source_style = (
        json.dumps(
            expected,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")
    (pack / "ecorex-pack.json").write_bytes(source_style)

    observed = stager["_normalize_process_pack_descriptor"](
        pack,
        pack_id=pack_id,
    )
    canonical = json.dumps(
        expected,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")

    assert observed == expected
    assert (pack / "ecorex-pack.json").read_bytes() == canonical
    assert not canonical.endswith(b"\n")
    assert (
        stager["_read_canonical_process_pack_descriptor"](
            pack,
            pack_id=pack_id,
        )
        == expected
    )

    (pack / "__main__.py").write_text("raise SystemExit(0)\n", encoding="utf-8")
    artifact = tmp_path / f"{pack_id}.zip"
    with zipfile.ZipFile(artifact, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(pack.iterdir()):
            archive.write(path, path.name)
    inspected = _inspect_zipapp(
        SimpleNamespace(
            artifact_path=artifact,
            manifest=SimpleNamespace(
                pack_id=pack_id,
                runtime_api_version="1.0.0",
                tools=tuple(
                    SimpleNamespace(tool_id=tool_id) for tool_id in expected["tools"]
                ),
            ),
        )
    )
    assert inspected.pack_id == pack_id
    assert inspected.tools == tuple(expected["tools"])


def retired_legacy_platform_stager_rejects_semantically_drifted_process_pack_descriptor(
    tmp_path: Path,
) -> None:
    stager = runpy.run_path(str(ROOT / "platform-staging" / "stager.py"))
    pack = tmp_path / "browser"
    pack.mkdir()
    drifted = stager["_expected_process_pack_descriptor"]("browser")
    drifted["tools"] = ["web_fetch"]
    (pack / "ecorex-pack.json").write_text(
        json.dumps(drifted, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(
        stager["StageError"],
        match="capability_pack_descriptor_invalid",
    ):
        stager["_normalize_process_pack_descriptor"](
            pack,
            pack_id="browser",
        )


def test_windows_gui_launcher_and_embedded_cli_are_probed_separately() -> None:
    source = (ROOT / "platform-staging" / "stager.py").read_text(encoding="utf-8")

    assert '(str(launcher), "--help")' in source
    assert '(str(interpreter), "-I", "-B", "-m", "ecorex.server", "--help")' in source
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


def test_platform_supply_chain_scan_does_not_treat_opaque_native_bytes_as_tokens(
    tmp_path: Path,
) -> None:
    stager = runpy.run_path(str(ROOT / "platform-staging" / "stager.py"))
    token_like = b"ghp_abcdefghijklmnopqrstuvwxyz123456"
    (tmp_path / "signed-native-member").write_bytes(
        b"\xcf\xfa\xed\xfe\x00\x01" + token_like + b"\x00\xff\x80"
    )
    with zipfile.ZipFile(tmp_path / "browser-runtime.zip", "w") as archive:
        archive.writestr(
            "browser/native-member",
            b"\x7fELF\x00\x01" + token_like + b"\x00\xff\x80",
        )

    evidence = stager["_supply_chain"](
        tmp_path,
        (),
        lock_profile="runtime",
        require_complete=False,
    )

    assert evidence["secret_scan"] == "passed"


def test_platform_supply_chain_scan_treats_opaque_private_key_fixture_as_native_data(
    tmp_path: Path,
) -> None:
    stager = runpy.run_path(str(ROOT / "platform-staging" / "stager.py"))
    private_key = (
        b"-----BEGIN OPENSSH PRIVATE KEY-----\n"
        b"QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVo=\n"
        b"-----END OPENSSH PRIVATE KEY-----\n"
    )
    (tmp_path / "native-member").write_bytes(
        b"\xcf\xfa\xed\xfe\x00\xff" + private_key + b"\x00\x80"
    )

    evidence = stager["_supply_chain"](
        tmp_path,
        (),
        lock_profile="runtime",
        require_complete=False,
    )

    assert evidence["secret_scan"] == "passed"


def test_platform_supply_chain_scan_rejects_private_key_in_text_contract(
    tmp_path: Path,
) -> None:
    stager = runpy.run_path(str(ROOT / "platform-staging" / "stager.py"))
    (tmp_path / "credential.pem").write_bytes(
        b"\xff\x00-----BEGIN OPENSSH PRIVATE KEY-----\n"
        b"QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVo=\n"
        b"-----END OPENSSH PRIVATE KEY-----\n"
    )

    with pytest.raises(stager["StageError"], match="stage_supply_chain_secret_match"):
        stager["_supply_chain"](
            tmp_path,
            (),
            lock_profile="runtime",
            require_complete=False,
        )


def test_platform_supply_chain_scan_rejects_token_in_malformed_text_payload(
    tmp_path: Path,
) -> None:
    stager = runpy.run_path(str(ROOT / "platform-staging" / "stager.py"))
    (tmp_path / "malformed.py").write_bytes(
        b"\xff\x00credential='ghp_abcdefghijklmnopqrstuvwxyz123456'\n"
    )

    with pytest.raises(
        stager["StageError"], match="stage_supply_chain_secret_match"
    ) as rejected:
        stager["_supply_chain"](
            tmp_path,
            (),
            lock_profile="runtime",
            require_complete=False,
        )
    assert rejected.value.diagnostic == {
        "content_sha256": hashlib.sha256(
            (tmp_path / "malformed.py").read_bytes()
        ).hexdigest(),
        "detector_id": "github_token",
        "kind": "regular",
        "location_sha256": hashlib.sha256(b"malformed.py").hexdigest(),
    }


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
    test_module = distribution_root / "example_runtime" / "tests" / "test_runtime.py"
    test_module.parent.mkdir()
    test_module.write_text("raise AssertionError\n", encoding="utf-8")
    test_example = (
        distribution_root / "example_runtime" / "test-examples" / "sample.txt"
    )
    test_example.parent.mkdir()
    test_example.write_text("test-only\n", encoding="utf-8")
    typing_stub = distribution_root / "example_runtime" / "api.pyi"
    typing_stub.write_text("VALUE: int\n", encoding="utf-8")
    build_header = distribution_root / "example_runtime" / "include" / "runtime.h"
    build_header.parent.mkdir()
    build_header.write_text("#define BUILD_ONLY 1\n", encoding="utf-8")
    metadata = Message()
    metadata["Name"] = "example-runtime"
    metadata["License-Expression"] = "MIT"

    class Distribution:
        version = "1.0.0"
        files = (
            Path("example_runtime/__init__.py"),
            Path("example_runtime/tests/test_runtime.py"),
            Path("example_runtime/test-examples/sample.txt"),
            Path("example_runtime/api.pyi"),
            Path("example_runtime/include/runtime.h"),
        )
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
    assert not (destination / "example_runtime" / "tests").exists()
    assert not (destination / "example_runtime" / "test-examples").exists()
    assert not (destination / "example_runtime" / "api.pyi").exists()
    assert not (destination / "example_runtime" / "include" / "runtime.h").exists()


def test_platform_stage_size_gate_records_top_files_and_rejects_build_payload(
    tmp_path: Path,
) -> None:
    stager = runpy.run_path(str(ROOT / "platform-staging" / "stager.py"))
    stage = tmp_path / "stage"
    runtime = stage / "core" / "bin" / "pack-python"
    runtime.mkdir(parents=True)
    (runtime / "python.exe").write_bytes(b"runtime")
    (stage / "packs" / "channels").mkdir(parents=True)
    (stage / "packs" / "channels" / "contracts.json").write_bytes(b"{}")
    evidence = tmp_path / "evidence"

    stager["_stage_size_gate"](
        stage,
        platform="windows",
        architecture="x64",
        evidence=evidence,
    )

    value = json.loads((evidence / "package-size.json").read_text(encoding="utf-8"))
    assert value["details"]["runtime_bytes"] == len(b"runtime")
    assert value["details"]["top_files"][0]["path"] == (
        "core/bin/pack-python/python.exe"
    )

    (runtime / "debug.pyi").write_text("BUILD_ONLY: bool\n", encoding="utf-8")
    with pytest.raises(
        stager["StageError"], match="stage_runtime_development_file_present"
    ):
        stager["_stage_size_gate"](
            stage,
            platform="windows",
            architecture="x64",
            evidence=evidence,
        )


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
    with pytest.raises(stager["StageError"], match="dependency_pack_payload_mismatch"):
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


def retired_legacy_macos_sandbox_failure_codes_are_explicitly_allowlisted() -> None:
    stager = runpy.run_path(str(ROOT / "platform-staging" / "stager.py"))
    public_codes = stager["_MACOS_SANDBOX_FAILURE_CODES"]
    classify = stager["_sandbox_failure_code"]

    assert len(public_codes) == 22
    for code in public_codes:
        assert classify("macos", code) == code
        assert classify("windows", code) == "sandbox_boundary_probe_failed"
    assert (
        classify("macos", "provider path /private/tmp/secret")
        == "sandbox_boundary_probe_failed"
    )


@pytest.mark.parametrize(
    ("events", "expected"),
    (
        (
            [
                {
                    "Action": "fail",
                    "Package": "ecorex.local/bootstrap",
                    "Test": "TestFreshBootstrapStateDirectoryAndTrustedLocalMigrationSource",
                }
            ],
            "bootstrap_test_local_migration_failed",
        ),
        (
            [
                {
                    "Action": "fail",
                    "Package": "ecorex.local/bootstrap",
                    "Test": "TestBoundedBufferFailsAtTheConfiguredLimit",
                },
                {
                    "Action": "fail",
                    "Package": "ecorex.local/bootstrap",
                    "Test": "TestUnknown",
                },
            ],
            "bootstrap_test_multiple_failed",
        ),
        (
            [
                {
                    "Action": "fail",
                    "Package": "ecorex.local/bootstrap",
                    "Test": "TestUnknown",
                }
            ],
            "bootstrap_test_unknown_failed",
        ),
        (
            [
                {
                    "Action": "fail",
                    "Package": "another/package",
                    "Test": "TestFreshBootstrapStateDirectoryAndTrustedLocalMigrationSource",
                }
            ],
            "bootstrap_test_package_failed",
        ),
    ),
)
def test_bootstrap_go_test_failures_are_fixed_and_non_disclosing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    events: list[dict[str, str]],
    expected: str,
) -> None:
    stager = runpy.run_path(str(ROOT / "platform-staging" / "stager.py"))
    result_type = stager["BoundedProcessResult"]

    def failed(*_args: object, **_kwargs: object):
        payload = b"\n".join(
            json.dumps(event, separators=(",", ":")).encode("utf-8")
            for event in events
        )
        return result_type(returncode=1, stdout=payload, stderr=b"private")

    monkeypatch.setitem(
        stager["_run_bootstrap_tests"].__globals__, "run_bounded_process", failed
    )
    with pytest.raises(stager["StageError"]) as raised:
        stager["_run_bootstrap_tests"](
            "go", source=tmp_path, environment={"GOTOOLCHAIN": "local"}
        )
    assert raised.value.code == expected
    assert "private" not in str(raised.value)
    if expected == "bootstrap_test_multiple_failed":
        assert raised.value.diagnostic == {
            "failed_codes": (
                "bootstrap_test_bounded_buffer_failed,"
                "bootstrap_test_unknown_failed"
            ),
            "failure_count": "2",
        }
    else:
        assert raised.value.diagnostic is None


def test_bootstrap_multiple_failure_diagnostic_rejects_unfixed_content() -> None:
    stager = runpy.run_path(str(ROOT / "platform-staging" / "stager.py"))
    error = stager["StageError"](
        "bootstrap_test_multiple_failed",
        diagnostic={"failed_codes": "private/path", "failure_count": "2"},
    )
    assert error.diagnostic is None


def test_bootstrap_go_test_success_does_not_expose_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stager = runpy.run_path(str(ROOT / "platform-staging" / "stager.py"))
    result_type = stager["BoundedProcessResult"]
    monkeypatch.setitem(
        stager["_run_bootstrap_tests"].__globals__,
        "run_bounded_process",
        lambda *_args, **_kwargs: result_type(
            returncode=0, stdout=b"not-json", stderr=b"private"
        ),
    )
    stager["_run_bootstrap_tests"](
        "go", source=tmp_path, environment={"GOTOOLCHAIN": "local"}
    )


def test_bootstrap_security_tests_use_a_canonical_real_temp_root() -> None:
    source = (ROOT / "platform-staging" / "bootstrap" / "main_test.go").read_text(
        encoding="utf-8"
    )
    assert "resolved, err := filepath.EvalSymlinks(raw)" in source
    assert "metadata.Mode()&os.ModeSymlink != 0" in source
    assert source.count("canonicalTestTempDir(t)") >= 9
    assert source.count("ensureBootstrapStateDirectory(root)") == 3


def test_office_pack_declares_formats_not_rendering() -> None:
    descriptor = json.loads(
        (PACKS / "office" / "ecorex-dependency-pack.json").read_text(encoding="utf-8")
    )
    stager_source = (ROOT / "platform-staging" / "stager.py").read_text(
        encoding="utf-8"
    )
    probe_source = (
        ROOT / "platform-staging" / "probes" / "dependency_pack_probe.py"
    ).read_text(encoding="utf-8")

    assert descriptor["adapter"] == "python-office-formats-v1"
    assert descriptor["services"] == ["office.formats"]
    assert "office-format-smoke" in stager_source
    assert '"worker_create_edit"' in stager_source
    assert "worker._office_edit(" in probe_source
    assert "office-runtime-smoke" not in stager_source


def test_bootstrap_requires_the_complete_cow_pack_set() -> None:
    source = (ROOT / "platform-staging" / "bootstrap" / "main.go").read_text(
        encoding="utf-8"
    )

    assert (
        '[]string{"channels", "image", "ocr", "office"}' in source
    )
    assert 'strings.HasPrefix(item.ArtifactID, "capability-pack-")' in source
    assert "unexpected host Capability Pack" in source
