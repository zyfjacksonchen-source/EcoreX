from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import signal
import stat
import subprocess
import zipfile
from dataclasses import replace
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from ecorex.bootstrap import (
    BootstrapConfigurationError,
    BootstrapExitCode,
    BootstrapReason,
    BootstrapSupervisor,
    BootstrapTrustError,
    DelayedRestartRequester,
    RUNTIME_RESTART_EXIT_CODE,
    RUNTIME_RELOAD_EXIT_CODE,
    RuntimeEndpoint,
    RuntimeLaunchError,
)
from ecorex.bootstrap.health import (
    DEFAULT_ACTIVATION_HEALTH_TIMEOUT_SECONDS,
    MAX_ACTIVATION_HEALTH_TIMEOUT_SECONDS,
    LoopbackActivationHealthProbe,
)
from ecorex.startup_diagnostics import STARTUP_DIAGNOSTIC_TOKEN_ENV
from ecorex.update import (
    Ed25519SignatureVerifier,
    ReleaseArtifact,
    ReleaseChannel,
    ReleaseManifest,
    ReleaseSource,
    SignatureEnvelope,
    SlotStore,
    SourceKind,
)


def _unsigned() -> SignatureEnvelope:
    return SignatureEnvelope(
        algorithm="ed25519",
        key_id="bootstrap-test-key",
        value=base64.b64encode(b"0" * 64).decode("ascii"),
    )


def test_default_activation_health_budget_remains_bounded_for_probe_only_startup() -> None:
    probe = LoopbackActivationHealthProbe()

    assert probe.timeout_seconds == DEFAULT_ACTIVATION_HEALTH_TIMEOUT_SECONDS == 120.0
    with pytest.raises(ValueError, match="between one and 180 seconds"):
        LoopbackActivationHealthProbe(
            timeout_seconds=MAX_ACTIVATION_HEALTH_TIMEOUT_SECONDS + 0.1
        )


def _signed(
    private_key: Ed25519PrivateKey,
    payload: bytes,
) -> SignatureEnvelope:
    return SignatureEnvelope(
        algorithm="ed25519",
        key_id="bootstrap-test-key",
        value=base64.b64encode(private_key.sign(payload)).decode("ascii"),
    )


def _package(version: str, *, second_entrypoint: bool = False) -> bytes:
    output = io.BytesIO()
    executable = zipfile.ZipInfo("bin/ecorex.exe")
    executable.create_system = 3
    executable.external_attr = (stat.S_IFREG | 0o755) << 16
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(executable, f"signed runtime {version}".encode())
        archive.writestr("runtime/version.txt", version)
        if second_entrypoint:
            duplicate = zipfile.ZipInfo("runtime/bin/ecorex.exe")
            duplicate.create_system = 3
            duplicate.external_attr = (stat.S_IFREG | 0o755) << 16
            archive.writestr(duplicate, b"ambiguous runtime")
    return output.getvalue()


def _release(
    private_key: Ed25519PrivateKey,
    version: str,
    package: bytes,
    *,
    artifact_id: str = "core-windows-x64",
    invalid_artifact_signature: bool = False,
) -> tuple[ReleaseManifest, ReleaseArtifact]:
    build_digest = hashlib.sha256(f"build:{version}".encode()).hexdigest()
    artifact = ReleaseArtifact(
        artifact_id=artifact_id,
        platform="windows",
        architecture="x64",
        file_name=f"ecorex-core-{version}.zip",
        size_bytes=len(package),
        sha256=hashlib.sha256(package).hexdigest(),
        signature=_unsigned(),
    )
    artifact = replace(
        artifact,
        signature=(
            _unsigned()
            if invalid_artifact_signature
            else _signed(
                private_key,
                artifact.signed_payload(
                    release_id=f"release-{version.replace('.', '-')}",
                    version=version,
                    build_digest=build_digest,
                ),
            )
        ),
    )
    sources = (
        ReleaseSource(
            "mirror",
            SourceKind.GITHUB_CN_MIRROR,
            0,
            "https://mirror.example/ecorex",
        ),
        ReleaseSource(
            "github",
            SourceKind.GITHUB_RELEASE,
            1,
            "https://github.example/ecorex",
        ),
        ReleaseSource(
            "cdn",
            SourceKind.ECOREX_CDN,
            2,
            "https://cdn.example/ecorex",
        ),
    )
    manifest = ReleaseManifest(
        schema_version=1,
        release_id=f"release-{version.replace('.', '-')}",
        version=version,
        build_digest=build_digest,
        channel=ReleaseChannel.STABLE,
        created_at="2026-07-10T00:00:00+00:00",
        sources=sources,
        artifacts=(artifact,),
        signature=_unsigned(),
    )
    manifest = replace(
        manifest,
        signature=_signed(private_key, manifest.canonical_payload()),
    )
    return manifest, artifact


def _stage(
    install_root: Path,
    private_key: Ed25519PrivateKey,
    version: str,
    *,
    activate: bool,
    second_entrypoint: bool = False,
    artifact_id: str = "core-windows-x64",
    invalid_artifact_signature: bool = False,
) -> tuple[SlotStore, str]:
    package_bytes = _package(version, second_entrypoint=second_entrypoint)
    package_path = install_root.parent / f"package-{version}.zip"
    package_path.write_bytes(package_bytes)
    manifest, artifact = _release(
        private_key,
        version,
        package_bytes,
        artifact_id=artifact_id,
        invalid_artifact_signature=invalid_artifact_signature,
    )
    store = SlotStore(install_root)
    slot_id = f"runtime-{version.replace('.', '-')}"
    store.stage(
        package_path,
        slot_id=slot_id,
        manifest=manifest,
        artifact=artifact,
    )
    if activate:
        store.switch_to(slot_id)
        store.mark_known_good(slot_id)
    return store, slot_id


@pytest.fixture
def signing_key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.generate()


def _verifier(private_key: Ed25519PrivateKey) -> Ed25519SignatureVerifier:
    public_key = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return Ed25519SignatureVerifier({"bootstrap-test-key": public_key})


class _Child:
    def __init__(self, exit_code: int, on_wait=None) -> None:
        self.exit_code = exit_code
        self.on_wait = on_wait
        self.signals: list[int] = []

    def wait(self) -> int:
        if self.on_wait is not None:
            self.on_wait()
        return self.exit_code

    def send_signal(self, signum: int) -> None:
        self.signals.append(signum)


class _Launcher:
    def __init__(
        self, children: list[_Child], *, startup_stage: str | None = None
    ) -> None:
        self.children = children
        self.specs = []
        self.startup_stage = startup_stage

    def start(self, spec):
        self.specs.append(spec)
        if self.startup_stage is not None:
            token = spec.environment[STARTUP_DIAGNOSTIC_TOKEN_ENV]
            directory = spec.cwd.parent.parent.parent / ".runtime-startup"
            (directory / f"{token}.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "stage": self.startup_stage,
                        "token": token,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                encoding="utf-8",
            )
        return self.children.pop(0)


def _supervisor(
    root: Path,
    signing_key: Ed25519PrivateKey,
    launcher,
    **kwargs,
) -> BootstrapSupervisor:
    return BootstrapSupervisor(
        root,
        endpoint=RuntimeEndpoint("127.0.0.1", 9321),
        verifier=_verifier(signing_key),
        launcher=launcher,
        host_platform="windows",
        host_architecture="x64",
        **kwargs,
    )


def test_uses_authoritative_pointer_and_launches_with_fixed_safe_argv(
    tmp_path: Path,
    signing_key: Ed25519PrivateKey,
) -> None:
    install_root = tmp_path / "install"
    _, slot_id = _stage(install_root, signing_key, "1.0.0", activate=True)
    # This label is deliberately untrusted operator convenience.
    (install_root / "current").write_text("../../attacker\n", encoding="utf-8")
    launcher = _Launcher([_Child(0)])
    supervisor = _supervisor(
        install_root,
        signing_key,
        launcher,
        source_environment={
            "API_TOKEN": "must-not-reach-runtime",
            "AWS_SECRET_ACCESS_KEY": "must-not-reach-runtime",
            "PATH": "attacker-path",
            "PYTHONDONTWRITEBYTECODE": "0",
            "PYTHONNOUSERSITE": "0",
            "TEMP": str(tmp_path),
        },
    )

    result = supervisor.run()

    assert result.exit_code == 0
    assert result.launched_slots == (slot_id,)
    spec = launcher.specs[0]
    assert spec.argv == (
        str(spec.executable),
        "serve",
        "--host",
        "127.0.0.1",
        "--port",
        "9321",
    )
    assert spec.cwd == install_root / "slots" / slot_id / "payload"
    assert "API_TOKEN" not in spec.environment
    assert "AWS_SECRET_ACCESS_KEY" not in spec.environment
    assert spec.environment["PATH"] != "attacker-path"
    assert spec.environment["PYTHONDONTWRITEBYTECODE"] == "1"
    assert spec.environment["PYTHONNOUSERSITE"] == "1"
    assert spec.environment["PYTHONTZPATH"] == ""


def test_runtime_failure_reports_only_nonce_bound_safe_startup_stage(
    tmp_path: Path,
    signing_key: Ed25519PrivateKey,
) -> None:
    install_root = tmp_path / "install"
    _, slot_id = _stage(install_root, signing_key, "1.0.0", activate=True)
    launcher = _Launcher([_Child(64)], startup_stage="capability_pack_binding")

    result = _supervisor(install_root, signing_key, launcher).run()

    assert result.reason is BootstrapReason.RUNTIME_FAILED
    assert result.runtime_exit_code == 64
    assert result.runtime_startup_stage == "capability_pack_binding"
    token = launcher.specs[0].environment[STARTUP_DIAGNOSTIC_TOKEN_ENV]
    assert not (install_root / ".runtime-startup" / f"{token}.json").exists()
    assert result.launched_slots == (slot_id,)


def test_current_must_be_known_good_before_any_process_is_created(
    tmp_path: Path,
    signing_key: Ed25519PrivateKey,
) -> None:
    install_root = tmp_path / "install"
    store, slot_id = _stage(install_root, signing_key, "1.0.0", activate=False)
    store.switch_to(slot_id)
    launcher = _Launcher([_Child(0)])

    with pytest.raises(BootstrapTrustError, match="known-good"):
        _supervisor(install_root, signing_key, launcher).run()

    assert launcher.specs == []


@pytest.mark.parametrize("target", ["manifest", "receipt", "payload"])
def test_tampering_any_retained_trust_layer_blocks_launch(
    tmp_path: Path,
    signing_key: Ed25519PrivateKey,
    target: str,
) -> None:
    install_root = tmp_path / target
    _, slot_id = _stage(install_root, signing_key, "1.0.0", activate=True)
    slot = install_root / "slots" / slot_id
    if target == "manifest":
        raw = json.loads((slot / "release-manifest.json").read_text(encoding="utf-8"))
        raw["signature"]["value"] = base64.b64encode(b"x" * 64).decode("ascii")
        (slot / "release-manifest.json").write_text(json.dumps(raw), encoding="utf-8")
    elif target == "receipt":
        with (slot / ".release-package").open("ab") as stream:
            stream.write(b"tampered")
    else:
        (slot / "payload" / "runtime" / "version.txt").write_text("tampered")
    launcher = _Launcher([_Child(0)])

    with pytest.raises(BootstrapTrustError):
        _supervisor(install_root, signing_key, launcher).run()

    assert launcher.specs == []


def test_manifest_validity_cannot_mask_bad_artifact_signature_or_non_core_slot(
    tmp_path: Path,
    signing_key: Ed25519PrivateKey,
) -> None:
    bad_signature = tmp_path / "bad-signature"
    _stage(
        bad_signature,
        signing_key,
        "1.0.0",
        activate=True,
        invalid_artifact_signature=True,
    )
    launcher = _Launcher([_Child(0)])
    with pytest.raises(BootstrapTrustError):
        _supervisor(bad_signature, signing_key, launcher).run()
    assert launcher.specs == []

    wrong_artifact = tmp_path / "wrong-artifact"
    _stage(
        wrong_artifact,
        signing_key,
        "1.0.0",
        activate=True,
        artifact_id="bootstrap-windows-x64",
    )
    launcher = _Launcher([_Child(0)])
    with pytest.raises(BootstrapTrustError, match="canonical Runtime core"):
        _supervisor(wrong_artifact, signing_key, launcher).run()
    assert launcher.specs == []


def test_activation_restart_rereads_current_and_preserves_endpoint(
    tmp_path: Path,
    signing_key: Ed25519PrivateKey,
) -> None:
    install_root = tmp_path / "install"
    store, first_slot = _stage(install_root, signing_key, "1.0.0", activate=True)
    _, second_slot = _stage(install_root, signing_key, "1.0.1", activate=False)

    def activate_second() -> None:
        store.switch_to(second_slot)
        store.mark_known_good(second_slot)

    launcher = _Launcher(
        [_Child(RUNTIME_RESTART_EXIT_CODE, activate_second), _Child(0)]
    )

    result = _supervisor(install_root, signing_key, launcher).run()

    assert result.reason is BootstrapReason.RUNTIME_COMPLETED
    assert result.launched_slots == (first_slot, second_slot)
    assert result.requested_restarts == 1
    assert len(launcher.specs) == 2
    assert launcher.specs[0].argv[2:] == launcher.specs[1].argv[2:]
    assert launcher.specs[0].executable != launcher.specs[1].executable


def test_restart_without_pointer_activation_never_relaunches_old_runtime(
    tmp_path: Path,
    signing_key: Ed25519PrivateKey,
) -> None:
    install_root = tmp_path / "install"
    _stage(install_root, signing_key, "1.0.0", activate=True)
    launcher = _Launcher([_Child(RUNTIME_RESTART_EXIT_CODE)])

    result = _supervisor(install_root, signing_key, launcher).run()

    assert result.reason is BootstrapReason.RESTART_WITHOUT_ACTIVATION
    assert result.exit_code == int(BootstrapExitCode.RUNTIME_FAILURE)
    assert len(launcher.specs) == 1


def test_session_reload_reverifies_and_relaunches_the_same_signed_slot(
    tmp_path: Path,
    signing_key: Ed25519PrivateKey,
) -> None:
    install_root = tmp_path / "session-reload"
    _store, slot_id = _stage(
        install_root,
        signing_key,
        "1.0.0",
        activate=True,
    )
    launcher = _Launcher([_Child(RUNTIME_RELOAD_EXIT_CODE), _Child(0)])

    result = _supervisor(install_root, signing_key, launcher).run()

    assert result.reason is BootstrapReason.RUNTIME_COMPLETED
    assert result.launched_slots == (slot_id, slot_id)
    assert result.requested_restarts == 1
    assert len(launcher.specs) == 2
    assert launcher.specs[0].executable == launcher.specs[1].executable
    assert launcher.specs[0].argv == launcher.specs[1].argv


def test_session_reload_is_bounded_without_weakening_activation_fence(
    tmp_path: Path,
    signing_key: Ed25519PrivateKey,
) -> None:
    install_root = tmp_path / "session-reload-limit"
    _stage(install_root, signing_key, "1.0.0", activate=True)
    launcher = _Launcher(
        [_Child(RUNTIME_RELOAD_EXIT_CODE), _Child(RUNTIME_RELOAD_EXIT_CODE)]
    )

    result = _supervisor(
        install_root,
        signing_key,
        launcher,
        max_requested_restarts=1,
    ).run()

    assert result.reason is BootstrapReason.RESTART_LIMIT_REACHED
    assert result.requested_restarts == 2
    assert len(launcher.specs) == 2


def test_requested_restarts_and_abnormal_exit_codes_are_bounded(
    tmp_path: Path,
    signing_key: Ed25519PrivateKey,
) -> None:
    install_root = tmp_path / "limit"
    store, _ = _stage(install_root, signing_key, "1.0.0", activate=True)
    _, second = _stage(install_root, signing_key, "1.0.1", activate=False)

    def activate_second() -> None:
        store.switch_to(second)
        store.mark_known_good(second)

    launcher = _Launcher(
        [_Child(RUNTIME_RESTART_EXIT_CODE, activate_second), _Child(RUNTIME_RESTART_EXIT_CODE)]
    )
    limited = _supervisor(
        install_root,
        signing_key,
        launcher,
        max_requested_restarts=1,
    ).run()
    assert limited.reason is BootstrapReason.RESTART_LIMIT_REACHED
    assert limited.exit_code == int(BootstrapExitCode.RUNTIME_FAILURE)
    assert len(launcher.specs) == 2

    failure_root = tmp_path / "failure"
    _stage(failure_root, signing_key, "1.0.0", activate=True)
    failed_launcher = _Launcher([_Child(0xDEADBEEF)])
    failed = _supervisor(failure_root, signing_key, failed_launcher).run()
    assert failed.reason is BootstrapReason.RUNTIME_FAILED
    assert failed.exit_code == int(BootstrapExitCode.RUNTIME_FAILURE)
    assert len(failed_launcher.specs) == 1

    signal_root = tmp_path / "signal"
    _stage(signal_root, signing_key, "1.0.0", activate=True)
    signalled = _supervisor(
        signal_root,
        signing_key,
        _Launcher([_Child(-9999)]),
    ).run()
    assert signalled.reason is BootstrapReason.RUNTIME_SIGNALLED
    assert signalled.exit_code == 255


def test_stop_signal_is_forwarded_once_and_prevents_restart(
    tmp_path: Path,
    signing_key: Ed25519PrivateKey,
) -> None:
    install_root = tmp_path / "install"
    _stage(install_root, signing_key, "1.0.0", activate=True)
    child = _Child(RUNTIME_RESTART_EXIT_CODE)
    launcher = _Launcher([child])
    supervisor = _supervisor(install_root, signing_key, launcher)
    child.on_wait = lambda: supervisor.request_stop(int(signal.SIGTERM))

    result = supervisor.run()

    assert result.reason is BootstrapReason.STOP_REQUESTED
    assert result.exit_code == 128 + int(signal.SIGTERM)
    assert child.signals == [int(signal.SIGTERM)]
    assert len(launcher.specs) == 1


def test_default_launcher_uses_no_shell_and_a_sanitized_environment(
    tmp_path: Path,
    signing_key: Ed25519PrivateKey,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_root = tmp_path / "install"
    _stage(install_root, signing_key, "1.0.0", activate=True)
    calls = []

    def fake_popen(*args, **kwargs):
        calls.append((args, kwargs))
        return _Child(0)

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    result = BootstrapSupervisor(
        install_root,
        verifier=_verifier(signing_key),
        host_platform="windows",
        host_architecture="x64",
        source_environment={
            "GITHUB_TOKEN": "secret",
            "SYSTEMDRIVE": "C:",
            "TEMP": str(tmp_path),
            "ECOREX_RUNTIME_OWNER_NONCE": "A" * 43,
        },
    ).run()

    assert result.exit_code == 0
    args, kwargs = calls[0]
    assert isinstance(args[0], list)
    assert kwargs["shell"] is False
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert kwargs["close_fds"] is True
    assert "GITHUB_TOKEN" not in kwargs["env"]
    assert kwargs["env"]["PYTHONDONTWRITEBYTECODE"] == "1"
    assert kwargs["env"]["PYTHONNOUSERSITE"] == "1"
    assert kwargs["env"]["PYTHONTZPATH"] == ""
    assert kwargs["env"]["SYSTEMDRIVE"] == "C:"
    assert kwargs["env"]["ECOREX_RUNTIME_OWNER_NONCE"] == "A" * 43
    assert Path(kwargs["executable"]).is_absolute()

    with pytest.raises(BootstrapConfigurationError):
        BootstrapSupervisor(
            install_root,
            verifier=_verifier(signing_key),
            host_platform="windows",
            host_architecture="x64",
            source_environment={"ECOREX_RUNTIME_OWNER_NONCE": "invalid"},
        )


def test_default_resolver_fails_closed_for_ambiguous_or_linked_entrypoint(
    tmp_path: Path,
    signing_key: Ed25519PrivateKey,
) -> None:
    ambiguous_root = tmp_path / "ambiguous"
    _stage(
        ambiguous_root,
        signing_key,
        "1.0.0",
        activate=True,
        second_entrypoint=True,
    )
    with pytest.raises(RuntimeLaunchError, match="exactly one"):
        _supervisor(ambiguous_root, signing_key, _Launcher([_Child(0)])).run()

    linked_root = tmp_path / "linked"
    _, slot_id = _stage(linked_root, signing_key, "1.0.0", activate=True)
    executable = linked_root / "slots" / slot_id / "payload" / "bin" / "ecorex.exe"
    outside = tmp_path / "outside.exe"
    outside.write_bytes(executable.read_bytes())
    try:
        executable.unlink()
        executable.symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")
    with pytest.raises((BootstrapTrustError, RuntimeLaunchError)):
        _supervisor(linked_root, signing_key, _Launcher([_Child(0)])).run()


def test_delayed_restart_request_is_injectable_single_use_and_dedicated() -> None:
    delays: list[float] = []
    exits: list[int] = []
    scheduled: list[object] = []

    def schedule(callback) -> None:
        scheduled.append(callback)
        callback()

    requester = DelayedRestartRequester(
        delay_seconds=0.2,
        sleeper=lambda delay: delays.append(delay),
        exit_process=lambda code: exits.append(code),
        scheduler=schedule,
    )

    assert requester.request("activation-transaction") is True
    assert requester.request("activation-transaction") is False
    assert requester.requested is True
    assert len(scheduled) == 1
    assert delays == [0.2]
    assert exits == [RUNTIME_RESTART_EXIT_CODE]

    reload_exits: list[int] = []
    reloader = DelayedRestartRequester(
        exit_code=RUNTIME_RELOAD_EXIT_CODE,
        sleeper=lambda _delay: None,
        exit_process=lambda code: reload_exits.append(code),
        scheduler=lambda callback: callback(),
    )
    assert reloader.request("session-generation") is True
    assert reload_exits == [RUNTIME_RELOAD_EXIT_CODE]


@pytest.mark.parametrize(
    ("host", "port"),
    [("0.0.0.0", 8765), ("localhost", 8765), ("127.0.0.1", 0), ("127.0.0.1", 70000)],
)
def test_endpoint_rejects_non_literal_or_out_of_range_values(host: str, port: int) -> None:
    with pytest.raises(BootstrapConfigurationError):
        RuntimeEndpoint(host, port)


@pytest.mark.parametrize(
    ("os_name", "sys_platform", "machine", "pointer_bits", "expected"),
    [
        ("nt", "win32", "", 64, ("windows", "x64")),
        ("nt", "win32", "AMD64", 32, ("windows", "unsupported")),
        ("posix", "darwin", "x86_64", 64, ("macos", "x64")),
        ("posix", "darwin", "arm64", 64, ("macos", "arm64")),
        ("posix", "linux", "x86_64", 64, ("unsupported", "unsupported")),
    ],
)
def test_host_target_normalization_does_not_trust_windows_architecture_environment(
    os_name: str,
    sys_platform: str,
    machine: str,
    pointer_bits: int,
    expected: tuple[str, str],
) -> None:
    from ecorex.bootstrap.supervisor import _normalize_host_target

    assert _normalize_host_target(
        os_name=os_name,
        sys_platform=sys_platform,
        machine=machine,
        pointer_bits=pointer_bits,
    ) == expected
