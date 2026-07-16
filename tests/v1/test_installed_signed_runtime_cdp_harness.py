from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import sys

import pytest

from ecorex.release.process_boundary import BoundedProcessResult
from ecorex.update import SlotPointers


ROOT = Path(__file__).resolve().parents[2]


def _module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def drill():
    return _module(
        "test_installed_signed_runtime_drill",
        ROOT / "scripts" / "drill_v1_windows_signed_candidate.py",
    )


@pytest.fixture(scope="module")
def wrapper():
    return _module(
        "test_installed_signed_runtime_wrapper",
        ROOT / "scripts" / "run_v1_windows_signed_candidate_cdp.py",
    )


def _context(drill):
    return drill.LiveRuntimeAcceptanceContext(
        base_url="http://127.0.0.1:23456",
        source_commit="a" * 40,
        release_id="release-1.0.0-stable-test",
        version="1.0.0",
        build_digest="b" * 64,
        artifact_id="core-windows-x64",
        artifact_sha256="c" * 64,
        slot_id="slot-current-known-good",
    )


def _browser_report(context) -> dict[str, object]:
    return {
        "schema_version": 1,
        "status": "passed",
        "evidence_class": "installed-signed-runtime-cdp",
        "transport": "google-chrome-cdp",
        "acceptance_scope": "unauthenticated-shell-smoke",
        "mock_server_spawned": False,
        "ga_endpoint_contacted": False,
        "full_office_scenario_acceptance_claimed": False,
        "promotion_claimed": False,
        "runtime": {
            "origin": context.base_url,
            "release_id": context.release_id,
            "version": context.version,
            "api_version": "v1",
            "event_schema_version": 1,
            "storage_schema_version": 1,
            "authenticated": False,
            "index_cache_control": "no-store",
        },
        "browser": {
            "product": "Chrome/140.0.0.0",
            "protocol_version": "1.3",
            "isolated_profile": True,
            "external_network_blocked": True,
        },
        "ui": {
            "brand_visible": True,
            "new_task_visible": True,
            "managed_login_boundary_visible": True,
        },
        "diagnostics": {
            "console_errors": 0,
            "page_errors": 0,
            "failed_requests": 0,
            "external_requests": 0,
        },
        "screenshot_sha256": "d" * 64,
    }


def test_wrapper_runs_only_the_fixed_bounded_harness(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    drill,
    wrapper,
) -> None:
    context = _context(drill)
    node = tmp_path / "node.exe"
    node.write_bytes(b"node fixture")
    captured: dict[str, object] = {}

    def fake_process(command, **kwargs):
        captured["command"] = tuple(command)
        captured.update(kwargs)
        return BoundedProcessResult(
            returncode=0,
            stdout=json.dumps(_browser_report(context)).encode(),
            stderr=b"",
        )

    monkeypatch.setattr(wrapper, "_node_executable", lambda: node)
    monkeypatch.setattr(wrapper, "run_bounded_process", fake_process)
    monkeypatch.setenv("ECOREX_MODEL_API_KEY", "must-not-cross")
    monkeypatch.setenv("HTTPS_PROXY", "http://must-not-cross.invalid")

    evidence = wrapper._run_browser_acceptance(
        context,
        SimpleNamespace(remaining=lambda: 90.0),
    )

    command = captured["command"]
    assert command[0] == str(node)
    assert command[1].endswith("run-installed-signed-runtime-cdp.mjs")
    assert f"--base-url={context.base_url}" in command
    assert f"--expected-release-id={context.release_id}" in command
    assert f"--expected-version={context.version}" in command
    assert all("bearer" not in item.casefold() for item in command)
    assert all("ga-mock" not in item.casefold() for item in command)
    assert captured["max_stdout_bytes"] == 256 * 1024
    assert captured["max_stderr_bytes"] == 4 * 1024
    assert captured["hide_window"] is True
    assert "ECOREX_MODEL_API_KEY" not in captured["environment"]
    assert "HTTPS_PROXY" not in captured["environment"]
    assert evidence["candidate"]["source_commit"] == context.source_commit
    assert evidence["candidate"]["build_digest"] == context.build_digest
    assert evidence["candidate"]["slot_id"] == context.slot_id
    assert evidence["mock_or_fixture_runtime_used"] is False
    assert evidence["promotion_claimed"] is False


def test_wrapper_rejects_browser_identity_drift(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    drill,
    wrapper,
) -> None:
    context = _context(drill)
    node = tmp_path / "node.exe"
    node.write_bytes(b"node fixture")
    report = _browser_report(context)
    report["runtime"] = {**report["runtime"], "release_id": "other-release"}
    monkeypatch.setattr(wrapper, "_node_executable", lambda: node)
    monkeypatch.setattr(
        wrapper,
        "run_bounded_process",
        lambda *_args, **_kwargs: BoundedProcessResult(
            returncode=0,
            stdout=json.dumps(report).encode(),
            stderr=b"",
        ),
    )
    with pytest.raises(wrapper.drill.DrillError, match="failed its contract"):
        wrapper._run_browser_acceptance(
            context,
            SimpleNamespace(remaining=lambda: 90.0),
        )


class _FakeSlots:
    def __init__(self, root: Path, slot_id: str) -> None:
        self.path = root / slot_id
        self.path.mkdir(parents=True)
        self.slot_id = slot_id
        self.receipt_calls = 0
        self.pointer_calls = 0

    def pointers(self) -> SlotPointers:
        self.pointer_calls += 1
        return SlotPointers(current=self.slot_id, known_good=(self.slot_id,))

    def slot_path(self, slot_id: str) -> Path:
        assert slot_id == self.slot_id
        return self.path

    def validate_receipt(self, **_kwargs) -> Path:
        self.receipt_calls += 1
        return self.path


class _FakeSecurity:
    def __init__(self) -> None:
        self.calls = 0

    def validate(self, *_args) -> bool:
        self.calls += 1
        return True


def test_callback_is_inside_authoritative_known_good_rollback_window(
    tmp_path: Path,
    drill,
) -> None:
    slot_id = "slot-current-known-good"
    slots = _FakeSlots(tmp_path, slot_id)
    security = _FakeSecurity()
    artifact = SimpleNamespace(
        artifact_id="core-windows-x64",
        sha256="c" * 64,
    )
    manifest = SimpleNamespace(
        release_id="release-1.0.0-stable-test",
        version="1.0.0",
        build_digest="b" * 64,
    )
    observed = []

    evidence = drill._execute_live_runtime_acceptance(
        slots=slots,
        security=security,
        manifest=manifest,
        artifact=artifact,
        security_marker={"contract": "test"},
        expected_slot=slot_id,
        source_commit="a" * 40,
        port=23456,
        deadline=drill.Deadline.after(30),
        callback=lambda context, _deadline: (
            observed.append(context),
            {"status": "passed", "schema_version": 1},
        )[1],
        rollback_is_authoritative=lambda: True,
    )

    assert evidence == {"schema_version": 1, "status": "passed"}
    assert observed[0].slot_id == slot_id
    assert observed[0].build_digest == "b" * 64
    assert slots.pointer_calls == 2
    assert slots.receipt_calls == 2
    assert security.calls == 2


def test_callback_never_runs_without_rollback_authority(tmp_path: Path, drill) -> None:
    slot_id = "slot-current-known-good"
    called = False

    def callback(_context, _deadline):
        nonlocal called
        called = True
        return {"status": "passed"}

    with pytest.raises(drill.DrillError, match="rollback terminal"):
        drill._execute_live_runtime_acceptance(
            slots=_FakeSlots(tmp_path, slot_id),
            security=_FakeSecurity(),
            manifest=SimpleNamespace(
                release_id="release-1.0.0-stable-test",
                version="1.0.0",
                build_digest="b" * 64,
            ),
            artifact=SimpleNamespace(
                artifact_id="core-windows-x64",
                sha256="c" * 64,
            ),
            security_marker={"contract": "test"},
            expected_slot=slot_id,
            source_commit="a" * 40,
            port=23456,
            deadline=drill.Deadline.after(30),
            callback=callback,
            rollback_is_authoritative=lambda: False,
        )
    assert called is False


def test_live_evidence_rejects_credentials(drill) -> None:
    with pytest.raises(drill.DrillError, match="credential field"):
        drill._assert_redacted_live_acceptance_evidence(
            {"status": "passed", "access_token": "must-not-persist"}
        )


def test_node_runner_has_no_fixture_server_or_security_bypass() -> None:
    source = (
        ROOT / "desktop" / "tools" / "run-installed-signed-runtime-cdp.mjs"
    ).read_text(encoding="utf-8")

    assert "ga-mock-server" not in source
    assert "--no-sandbox" not in source
    assert "--disable-web-security" not in source
    assert "ignoreHTTPSErrors" not in source
    assert 'parsed.hostname !== "127.0.0.1"' in source
    assert "chromium.connectOverCDP" in source
    assert '"--remote-debugging-port=0"' in source
    assert "DevToolsActivePort" in source
    assert "response.arrayBuffer()" not in source
    assert "mock_server_spawned: false" in source
    assert "ga_endpoint_contacted: false" in source
