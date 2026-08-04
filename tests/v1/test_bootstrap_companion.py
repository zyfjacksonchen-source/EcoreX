from __future__ import annotations

import base64
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import subprocess

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import pytest

from ecorex import __version__
from ecorex.bootstrap import companion as companion_module
from ecorex.bootstrap.companion import (
    BootstrapCompanionError,
    BootstrapCompanionInstaller,
)
from ecorex.product_version import stable_release_sequence
from ecorex.release import (
    ArtifactBuildInput,
    ArtifactKind,
    Ed25519MemorySigner,
    ReleaseBuildSpec,
    ReleaseBuilder,
)
from ecorex.update import (
    Ed25519SignatureVerifier,
    LocalSourceFetcher,
    ReleaseChannel,
    ReleaseSource,
    SourceKind,
)


def _built_bootstrap(
    tmp_path: Path,
    *,
    platform: str,
    architecture: str,
    release_key: Ed25519PrivateKey,
    publication_key: Ed25519PrivateKey,
    suffix: str,
):
    source = tmp_path / f"source-{suffix}"
    (source / "bin").mkdir(parents=True)
    launcher_name = (
        "ecorex-bootstrap.exe"
        if platform == "windows"
        else "ecorex-bootstrap"
    )
    launcher = source / "bin" / launcher_name
    launcher.write_bytes(("bootstrap-" + suffix).encode())
    executable_paths = [f"bin/{launcher_name}"]
    installer_name = (
        "EcoreX Installer.cmd"
        if platform == "windows"
        else "EcoreX Installer.command"
    )
    installer_entry = source / installer_name
    installer_entry.write_bytes(
        (
            b"@echo off\r\n"
            b"\"%~dp0bin\\ecorex-bootstrap.exe\" %*\r\n"
            b"exit /b %errorlevel%\r\n"
        )
        if platform == "windows"
        else (
            b"#!/bin/sh\n"
            b"BASE_DIR=$(CDPATH= cd -- \"$(dirname -- \"$0\")\" && pwd)\n"
            b"exec \"$BASE_DIR/bin/ecorex-bootstrap\" \"$@\"\n"
        )
    )
    if platform == "macos":
        installer_entry.chmod(0o755)
        executable_paths.append(installer_name)
    helper_digest = ""
    if platform == "windows":
        helper = source / "bin" / "ecorex-sandbox-host.exe"
        helper.write_bytes(("helper-" + suffix).encode())
        helper_digest = hashlib.sha256(helper.read_bytes()).hexdigest()
        executable_paths.append("bin/ecorex-sandbox-host.exe")
    release_public = release_key.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    publication_public = publication_key.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    sequence = stable_release_sequence(__version__)
    minimum_payload = (
        b"ecorex.bootstrap-minimum-stable.v1\0"
        + str(sequence).encode()
        + b"\0"
        + __version__.encode()
    )
    config = {
        "schema_version": 1,
        "public_index_url": "https://dl.ecoremedia.net/ecorex-agent/public-bootstrap-index.json",
        "release_public_keys": {
            "release-test": base64.b64encode(release_public).decode()
        },
        "publication_public_keys": {
            "publication-test": base64.b64encode(publication_public).decode()
        },
        "sandbox_helper_sha256": helper_digest,
        "minimum_stable": {
            "sequence": sequence,
            "version": __version__,
            "signature": {
                "algorithm": "ed25519",
                "key_id": "release-test",
                "value": base64.b64encode(
                    release_key.sign(minimum_payload)
                ).decode(),
            },
        },
    }
    (source / "bootstrap-config.json").write_text(
        json.dumps(config, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    sources = (
        ReleaseSource(
            "mirror",
            SourceKind.GITHUB_CN_MIRROR,
            0,
            "https://mirror.example/v1",
        ),
        ReleaseSource(
            "github",
            SourceKind.GITHUB_RELEASE,
            1,
            "https://github.example/v1",
        ),
        ReleaseSource(
            "cdn",
            SourceKind.ECOREX_CDN,
            2,
            "https://cdn.example/v1",
        ),
    )
    built = ReleaseBuilder(
        Ed25519MemorySigner("release-test", release_key)
    ).build(
        ReleaseBuildSpec(
            channel=ReleaseChannel.STABLE,
            created_at=datetime.now(UTC).replace(microsecond=0).isoformat(),
            sources=sources,
            artifacts=(
                ArtifactBuildInput(
                    source_dir=source,
                    kind=ArtifactKind.BOOTSTRAP,
                    platform=platform,
                    architecture=architecture,
                    executable_paths=tuple(executable_paths),
                ),
            ),
        ),
        tmp_path / f"release-{suffix}",
    )
    verifier = Ed25519SignatureVerifier({"release-test": release_public})
    fetcher = LocalSourceFetcher(
        {item.source_id: built.output_dir for item in sources}
    )
    return built, verifier, fetcher


def _prepare(
    installer: BootstrapCompanionInstaller,
    built,
    transaction_id: str,
) -> Path:
    transaction = installer.root / "transactions" / transaction_id
    transaction.mkdir(parents=True)
    (transaction / "release-manifest.json").write_text(
        built.manifest.to_json(include_signature=True),
        encoding="utf-8",
    )
    installer.stage(built.manifest, transaction)
    return installer.prepare_activation(built.manifest, transaction)


def _released_legacy_url_payload(host: str = "127.0.0.1") -> bytes:
    # v0.2.9.2 scripts/prepare-ecorex-webui-local-release.ps1 used
    # Set-Content -Encoding ASCII with a string already ending in CRLF.
    return (
        "[InternetShortcut]\r\n"
        f"URL=http://{host}:9909/app/\r\n\r\n"
    ).encode("ascii")


def _released_legacy_cmd_payload(
    *,
    local_app_data: Path,
    system_root: Path,
) -> bytes:
    # v0.3.0 Write-WebUiShortcuts fallback template, emitted by
    # [System.IO.File]::WriteAllText(..., [Text.Encoding]::ASCII).
    powershell = (
        system_root.resolve(strict=True)
        / "System32"
        / "WindowsPowerShell"
        / "v1.0"
        / "powershell.exe"
    )
    launcher = (
        local_app_data
        / "EcoreX WebUI"
        / "Launch EcoreX WebUI.ps1"
    )
    return (
        "@echo off\r\n"
        f'"{powershell}" -NoProfile -ExecutionPolicy Bypass '
        f'-File "{launcher}"\r\n'
    ).encode("ascii")


@pytest.mark.skipif(os.name != "nt", reason="Windows helper rotation contract")
def test_signed_companion_prepares_target_helper_before_slot_security(
    tmp_path: Path,
) -> None:
    release_key = Ed25519PrivateKey.generate()
    publication_key = Ed25519PrivateKey.generate()
    desktop = tmp_path / "Desktop"
    desktop.mkdir()
    built, verifier, fetcher = _built_bootstrap(
        tmp_path,
        platform="windows",
        architecture="x64",
        release_key=release_key,
        publication_key=publication_key,
        suffix="target-security",
    )
    root = tmp_path / "install"
    installer = BootstrapCompanionInstaller(
        root,
        platform="windows",
        architecture="x64",
        verifier=verifier,
        fetcher=fetcher,
        desktop_directory=desktop,
    )
    transaction = root / "transactions" / ("a" * 32)
    transaction.mkdir(parents=True)
    installer.stage(built.manifest, transaction)

    security = installer.prepare_payload_security(
        built.manifest,
        transaction,
    )

    assert security is not None
    helper = (
        root
        / "bootstrap"
        / "versions"
        / built.manifest.release_id
        / "bin"
        / "ecorex-sandbox-host.exe"
    )
    digest = hashlib.sha256(helper.read_bytes()).hexdigest()
    assert security.expected_helper_sha256 == digest
    assert security.bootstrap_helper == (
        root
        / "bootstrap"
        / "helpers"
        / digest
        / "ecorex-sandbox-host.exe"
    ).resolve(strict=True)
    assert not (root / "bootstrap" / "desktop-entry.json").exists()
    assert not any(desktop.iterdir())


def test_windows_fallback_entry_is_preserved_and_update_rollback_is_exact(
    tmp_path: Path,
) -> None:
    if os.name != "nt":
        return
    release_key = Ed25519PrivateKey.generate()
    publication_key = Ed25519PrivateKey.generate()
    desktop = tmp_path / "Desktop"
    desktop.mkdir()
    user_entry = desktop / "EcoreX.lnk"
    user_entry.write_bytes(b"user-owned-shortcut")
    root = tmp_path / "install"

    first, verifier, fetcher = _built_bootstrap(
        tmp_path,
        platform="windows",
        architecture="x64",
        release_key=release_key,
        publication_key=publication_key,
        suffix="first",
    )
    installer = BootstrapCompanionInstaller(
        root,
        platform="windows",
        architecture="x64",
        verifier=verifier,
        fetcher=fetcher,
        desktop_directory=desktop,
    )
    _prepare(installer, first, "1" * 32)
    fallback = desktop / "EcoreX Agent.lnk"
    assert user_entry.read_bytes() == b"user-owned-shortcut"
    assert fallback.is_file()
    installer.commit_activation("1" * 32)
    first_digest = hashlib.sha256(fallback.read_bytes()).hexdigest()
    first_receipt = json.loads(
        (root / "bootstrap" / "desktop-entry.json").read_text(encoding="utf-8")
    )
    assert first_receipt["entry_name"] == fallback.name

    second, second_verifier, second_fetcher = _built_bootstrap(
        tmp_path,
        platform="windows",
        architecture="x64",
        release_key=release_key,
        publication_key=publication_key,
        suffix="second",
    )
    second_installer = BootstrapCompanionInstaller(
        root,
        platform="windows",
        architecture="x64",
        verifier=second_verifier,
        fetcher=second_fetcher,
        desktop_directory=desktop,
    )
    _prepare(second_installer, second, "2" * 32)
    assert hashlib.sha256(fallback.read_bytes()).hexdigest() != first_digest
    second_installer.rollback_activation("2" * 32)
    assert hashlib.sha256(fallback.read_bytes()).hexdigest() == first_digest
    assert json.loads(
        (root / "bootstrap" / "desktop-entry.json").read_text(encoding="utf-8")
    ) == first_receipt
    assert user_entry.read_bytes() == b"user-owned-shortcut"


@pytest.mark.skipif(os.name != "nt", reason="Windows legacy shortcut migration")
def test_windows_fresh_install_keeps_legacy_webui_out_of_canonical_name_selection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release_key = Ed25519PrivateKey.generate()
    publication_key = Ed25519PrivateKey.generate()
    desktop = tmp_path / "Desktop"
    desktop.mkdir()
    local_app_data = tmp_path / "LocalAppData"
    legacy_root = local_app_data / "EcoreX WebUI"
    legacy_root.mkdir(parents=True)
    legacy_script = legacy_root / "Launch EcoreX WebUI.ps1"
    legacy_script.write_text("Write-Host legacy\n", encoding="utf-8")
    monkeypatch.setenv("LOCALAPPDATA", str(local_app_data))
    legacy_entry = desktop / "EcoreX WebUI.lnk"
    powershell = (
        Path(os.environ["SYSTEMROOT"])
        / "System32"
        / "WindowsPowerShell"
        / "v1.0"
        / "powershell.exe"
    )
    companion_module._run_powershell(
        ";".join(
            (
                "$shell=New-Object -ComObject WScript.Shell",
                "$link=$shell.CreateShortcut($env:ECOREX_TEST_SHORTCUT)",
                "$link.TargetPath=$env:ECOREX_TEST_TARGET",
                "$link.Arguments=$env:ECOREX_TEST_ARGUMENTS",
                "$link.WorkingDirectory=$env:ECOREX_TEST_WORKDIR",
                "$link.Description='Start or reopen EcoreX WebUI'",
                "$link.Save()",
            )
        ),
        companion_module._shortcut_environment(
            ECOREX_TEST_SHORTCUT=str(legacy_entry),
            ECOREX_TEST_TARGET=str(powershell),
            ECOREX_TEST_ARGUMENTS=(
                '-NoProfile -ExecutionPolicy Bypass -File '
                f'"{legacy_script}"'
            ),
            ECOREX_TEST_WORKDIR=str(legacy_root),
        ),
    )
    original_legacy_digest = hashlib.sha256(legacy_entry.read_bytes()).hexdigest()
    built, verifier, fetcher = _built_bootstrap(
        tmp_path,
        platform="windows",
        architecture="x64",
        release_key=release_key,
        publication_key=publication_key,
        suffix="fresh-legacy-canonical-selection",
    )
    root = tmp_path / "install"
    installer = BootstrapCompanionInstaller(
        root,
        platform="windows",
        architecture="x64",
        verifier=verifier,
        fetcher=fetcher,
        desktop_directory=desktop,
    )

    _prepare(installer, built, "d" * 32)

    canonical = desktop / "EcoreX.lnk"
    assert canonical.is_file()
    assert hashlib.sha256(legacy_entry.read_bytes()).hexdigest() == original_legacy_digest

    installer.commit_activation("d" * 32)

    assert canonical.is_file()
    assert not legacy_entry.exists()
    receipt = json.loads(
        (root / "bootstrap" / "desktop-entry.json").read_text(encoding="utf-8")
    )
    assert receipt["entry_name"] == "EcoreX.lnk"


@pytest.mark.skipif(os.name != "nt", reason="Windows legacy shortcut migration")
def test_windows_upgrade_atomically_takes_over_exact_legacy_webui_shortcut(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release_key = Ed25519PrivateKey.generate()
    publication_key = Ed25519PrivateKey.generate()
    desktop = tmp_path / "Desktop"
    desktop.mkdir()
    local_app_data = tmp_path / "LocalAppData"
    legacy_root = local_app_data / "EcoreX WebUI"
    legacy_root.mkdir(parents=True)
    legacy_script = legacy_root / "Launch EcoreX WebUI.ps1"
    legacy_script.write_text("Write-Host legacy\n", encoding="utf-8")
    monkeypatch.setenv("LOCALAPPDATA", str(local_app_data))
    root = tmp_path / "install"

    first, verifier, fetcher = _built_bootstrap(
        tmp_path,
        platform="windows",
        architecture="x64",
        release_key=release_key,
        publication_key=publication_key,
        suffix="legacy-takeover-first",
    )
    installer = BootstrapCompanionInstaller(
        root,
        platform="windows",
        architecture="x64",
        verifier=verifier,
        fetcher=fetcher,
        desktop_directory=desktop,
    )
    _prepare(installer, first, "a" * 32)
    installer.commit_activation("a" * 32)
    current_entry = desktop / "EcoreX.lnk"
    assert current_entry.is_file()
    current_receipt = json.loads(
        (root / "bootstrap" / "desktop-entry.json").read_text(encoding="utf-8")
    )

    legacy_entry = desktop / "EcoreX WebUI.lnk"
    system_root = os.environ["SYSTEMROOT"]
    powershell = (
        Path(system_root)
        / "System32"
        / "WindowsPowerShell"
        / "v1.0"
        / "powershell.exe"
    )
    companion_module._run_powershell(
        ";".join(
            (
                "$shell=New-Object -ComObject WScript.Shell",
                "$link=$shell.CreateShortcut($env:ECOREX_TEST_SHORTCUT)",
                "$link.TargetPath=$env:ECOREX_TEST_TARGET",
                "$link.Arguments=$env:ECOREX_TEST_ARGUMENTS",
                "$link.WorkingDirectory=$env:ECOREX_TEST_WORKDIR",
                "$link.Description='Start or reopen EcoreX WebUI'",
                "$link.Save()",
            )
        ),
        companion_module._shortcut_environment(
            ECOREX_TEST_SHORTCUT=str(legacy_entry),
            ECOREX_TEST_TARGET=str(powershell),
            ECOREX_TEST_ARGUMENTS=(
                '-NoProfile -ExecutionPolicy Bypass -File '
                f'"{legacy_script}"'
            ),
            ECOREX_TEST_WORKDIR=str(legacy_root),
        ),
    )
    original_legacy_digest = hashlib.sha256(legacy_entry.read_bytes()).hexdigest()

    second, second_verifier, second_fetcher = _built_bootstrap(
        tmp_path,
        platform="windows",
        architecture="x64",
        release_key=release_key,
        publication_key=publication_key,
        suffix="legacy-takeover-second",
    )
    second_installer = BootstrapCompanionInstaller(
        root,
        platform="windows",
        architecture="x64",
        verifier=second_verifier,
        fetcher=second_fetcher,
        desktop_directory=desktop,
    )
    _prepare(second_installer, second, "b" * 32)
    migrated = companion_module._read_windows_shortcut(current_entry)
    assert migrated is not None
    assert migrated["description"] == "EcoreX"
    assert Path(migrated["target"]).name.casefold() == "ecorex-bootstrap.exe"
    assert hashlib.sha256(current_entry.read_bytes()).hexdigest() != current_receipt[
        "entry_digest"
    ]
    assert hashlib.sha256(legacy_entry.read_bytes()).hexdigest() == original_legacy_digest
    second_installer.rollback_activation("b" * 32)
    assert hashlib.sha256(current_entry.read_bytes()).hexdigest() == current_receipt[
        "entry_digest"
    ]
    assert hashlib.sha256(legacy_entry.read_bytes()).hexdigest() == original_legacy_digest
    assert json.loads(
        (root / "bootstrap" / "desktop-entry.json").read_text(encoding="utf-8")
    ) == current_receipt

    _prepare(second_installer, second, "c" * 32)
    second_installer.commit_activation("c" * 32)
    receipt = json.loads(
        (root / "bootstrap" / "desktop-entry.json").read_text(encoding="utf-8")
    )
    assert receipt["entry_name"] == "EcoreX.lnk"
    assert current_entry.exists()
    assert not legacy_entry.exists()
    assert second_installer.remove_desktop_entry() is True
    assert not current_entry.exists()
    assert second_installer.remove_desktop_entry() is False


@pytest.mark.skipif(os.name != "nt", reason="Windows legacy entry migration")
def test_windows_upgrade_cleans_released_url_and_cmd_with_rollback_and_uninstall(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release_key = Ed25519PrivateKey.generate()
    publication_key = Ed25519PrivateKey.generate()
    desktop = tmp_path / "Desktop"
    desktop.mkdir()
    local_app_data = tmp_path / "LocalAppData"
    legacy_root = local_app_data / "EcoreX WebUI"
    legacy_root.mkdir(parents=True)
    (legacy_root / "Launch EcoreX WebUI.ps1").write_text(
        "Write-Host legacy\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("LOCALAPPDATA", str(local_app_data))
    url_entry = desktop / "EcoreX WebUI.url"
    cmd_entry = desktop / "EcoreX WebUI.cmd"
    url_payload = _released_legacy_url_payload()
    cmd_payload = _released_legacy_cmd_payload(
        local_app_data=local_app_data,
        system_root=Path(os.environ["SYSTEMROOT"]),
    )
    url_entry.write_bytes(url_payload)
    cmd_entry.write_bytes(cmd_payload)

    built, verifier, fetcher = _built_bootstrap(
        tmp_path,
        platform="windows",
        architecture="x64",
        release_key=release_key,
        publication_key=publication_key,
        suffix="released-url-cmd-migration",
    )
    root = tmp_path / "install"
    installer = BootstrapCompanionInstaller(
        root,
        platform="windows",
        architecture="x64",
        verifier=verifier,
        fetcher=fetcher,
        desktop_directory=desktop,
    )

    first_transaction = "6" * 32
    _prepare(installer, built, first_transaction)
    current = desktop / "EcoreX.lnk"
    assert current.exists()
    assert url_entry.read_bytes() == url_payload
    assert cmd_entry.read_bytes() == cmd_payload
    record = json.loads(
        (
            root
            / "bootstrap"
            / "companion-transactions"
            / first_transaction
            / "activation.json"
        ).read_text(encoding="utf-8")
    )
    assert {
        item["entry_name"] for item in record["legacy_entries"]
    } == {"EcoreX WebUI.url", "EcoreX WebUI.cmd"}

    installer.rollback_activation(first_transaction)
    assert not current.exists()
    assert url_entry.read_bytes() == url_payload
    assert cmd_entry.read_bytes() == cmd_payload

    second_transaction = "7" * 32
    _prepare(installer, built, second_transaction)
    current = desktop / "EcoreX.lnk"
    original_remove = companion_module._remove_legacy_windows_entry_path
    removals = 0

    def crash_after_first_legacy_removal(
        path: Path,
        *,
        desktop: Path,
    ) -> None:
        nonlocal removals
        original_remove(path, desktop=desktop)
        removals += 1
        if removals == 1:
            raise KeyboardInterrupt("simulated commit cleanup crash")

    monkeypatch.setattr(
        companion_module,
        "_remove_legacy_windows_entry_path",
        crash_after_first_legacy_removal,
    )
    with pytest.raises(KeyboardInterrupt, match="cleanup crash"):
        installer.commit_activation(second_transaction)
    assert current.exists()
    assert sum(path.exists() for path in (url_entry, cmd_entry)) == 1

    monkeypatch.setattr(
        companion_module,
        "_remove_legacy_windows_entry_path",
        original_remove,
    )
    installer.converge_activation()
    assert not url_entry.exists()
    assert not cmd_entry.exists()
    committed_record = json.loads(
        (
            root
            / "bootstrap"
            / "companion-transactions"
            / second_transaction
            / "activation.json"
        ).read_text(encoding="utf-8")
    )
    assert committed_record["legacy_entries"] == []
    assert [path.name for path in desktop.iterdir()] == ["EcoreX.lnk"]

    assert installer.remove_desktop_entry() is True
    assert not any(desktop.iterdir())
    assert installer.remove_desktop_entry() is False


@pytest.mark.skipif(os.name != "nt", reason="Windows legacy entry migration")
def test_windows_legacy_entry_matchers_preserve_custom_and_malicious_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release_key = Ed25519PrivateKey.generate()
    publication_key = Ed25519PrivateKey.generate()
    desktop = tmp_path / "Desktop"
    desktop.mkdir()
    local_app_data = tmp_path / "LocalAppData"
    legacy_root = local_app_data / "EcoreX WebUI"
    legacy_root.mkdir(parents=True)
    (legacy_root / "Launch EcoreX WebUI.ps1").write_text(
        "Write-Host legacy\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("LOCALAPPDATA", str(local_app_data))
    url_entry = desktop / "EcoreX WebUI.url"
    cmd_entry = desktop / "EcoreX WebUI.cmd"
    custom_url = (
        b"[InternetShortcut]\r\n"
        b"URL=http://127.0.0.1:9909/app/\r\n"
        b"IconFile=C:\\Users\\user\\custom.ico\r\n"
    )
    malicious_cmd = (
        _released_legacy_cmd_payload(
            local_app_data=local_app_data,
            system_root=Path(os.environ["SYSTEMROOT"]),
        )
        + b"start https://attacker.invalid/\r\n"
    )
    url_entry.write_bytes(_released_legacy_url_payload())
    cmd_entry.write_bytes(malicious_cmd)

    discovered = companion_module._discover_legacy_windows_entries(desktop)
    assert [item["entry_name"] for item in discovered] == [
        "EcoreX WebUI.url"
    ]
    assert (
        companion_module._legacy_windows_entry_kind(
            url_entry,
            _released_legacy_url_payload("localhost"),
        )
        == "windows-url"
    )

    built, verifier, fetcher = _built_bootstrap(
        tmp_path,
        platform="windows",
        architecture="x64",
        release_key=release_key,
        publication_key=publication_key,
        suffix="custom-legacy-preserved",
    )
    root = tmp_path / "install"
    installer = BootstrapCompanionInstaller(
        root,
        platform="windows",
        architecture="x64",
        verifier=verifier,
        fetcher=fetcher,
        desktop_directory=desktop,
    )
    transaction_id = "8" * 32
    _prepare(installer, built, transaction_id)
    current = desktop / "EcoreX.lnk"
    # Ownership must be revoked if the exact tag fixture is changed between
    # prepare and commit.
    url_entry.write_bytes(custom_url)
    installer.commit_activation(transaction_id)
    assert current.exists()
    assert url_entry.read_bytes() == custom_url
    assert cmd_entry.read_bytes() == malicious_cmd
    record = json.loads(
        (
            root
            / "bootstrap"
            / "companion-transactions"
            / transaction_id
            / "activation.json"
        ).read_text(encoding="utf-8")
    )
    assert record["legacy_entries"] == []

    assert installer.remove_desktop_entry() is True
    assert url_entry.read_bytes() == custom_url
    assert cmd_entry.read_bytes() == malicious_cmd


def test_macos_fallback_app_and_receipt_owned_removal(tmp_path: Path) -> None:
    release_key = Ed25519PrivateKey.generate()
    publication_key = Ed25519PrivateKey.generate()
    desktop = tmp_path / "Desktop"
    desktop.mkdir()
    user_app = desktop / "EcoreX.app"
    user_app.mkdir()
    (user_app / "user.txt").write_text("preserve", encoding="utf-8")
    root = tmp_path / "install"
    built, verifier, fetcher = _built_bootstrap(
        tmp_path,
        platform="macos",
        architecture="arm64",
        release_key=release_key,
        publication_key=publication_key,
        suffix="mac",
    )
    installer = BootstrapCompanionInstaller(
        root,
        platform="macos",
        architecture="arm64",
        verifier=verifier,
        fetcher=fetcher,
        desktop_directory=desktop,
    )
    _prepare(installer, built, "3" * 32)
    fallback = desktop / "EcoreX Agent.app"
    assert fallback.is_dir()
    assert (user_app / "user.txt").read_text(encoding="utf-8") == "preserve"
    installer.commit_activation("3" * 32)
    assert installer.remove_desktop_entry() is True
    assert not fallback.exists()
    assert (user_app / "user.txt").read_text(encoding="utf-8") == "preserve"


def test_rollback_reenters_after_backup_restore_crash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release_key = Ed25519PrivateKey.generate()
    publication_key = Ed25519PrivateKey.generate()
    desktop = tmp_path / "Desktop"
    desktop.mkdir()
    root = tmp_path / "install"
    first, verifier, fetcher = _built_bootstrap(
        tmp_path,
        platform="macos",
        architecture="arm64",
        release_key=release_key,
        publication_key=publication_key,
        suffix="rollback-first",
    )
    installer = BootstrapCompanionInstaller(
        root,
        platform="macos",
        architecture="arm64",
        verifier=verifier,
        fetcher=fetcher,
        desktop_directory=desktop,
    )
    _prepare(installer, first, "4" * 32)
    installer.commit_activation("4" * 32)
    entry = desktop / "EcoreX.app"
    first_digest = companion_module._entry_digest(entry, "macos")
    first_receipt = json.loads(
        (root / "bootstrap" / "desktop-entry.json").read_text(encoding="utf-8")
    )

    second, second_verifier, second_fetcher = _built_bootstrap(
        tmp_path,
        platform="macos",
        architecture="arm64",
        release_key=release_key,
        publication_key=publication_key,
        suffix="rollback-second",
    )
    second_installer = BootstrapCompanionInstaller(
        root,
        platform="macos",
        architecture="arm64",
        verifier=second_verifier,
        fetcher=second_fetcher,
        desktop_directory=desktop,
    )
    transaction_id = "5" * 32
    _prepare(second_installer, second, transaction_id)
    record_path = (
        root
        / "bootstrap"
        / "companion-transactions"
        / transaction_id
        / "activation.json"
    )
    record = json.loads(record_path.read_text(encoding="utf-8"))
    backup = Path(record["backup_path"])
    real_replace = os.replace

    class SimulatedCrash(BaseException):
        pass

    def replace_then_crash(source, destination):
        real_replace(source, destination)
        if Path(source) == backup and Path(destination) == entry:
            raise SimulatedCrash

    monkeypatch.setattr(companion_module.os, "replace", replace_then_crash)
    with pytest.raises(SimulatedCrash):
        second_installer.rollback_activation(transaction_id)
    monkeypatch.setattr(companion_module.os, "replace", real_replace)

    interrupted = json.loads(record_path.read_text(encoding="utf-8"))
    assert interrupted["state"] == "rolling_back"
    assert not os.path.lexists(backup)
    assert companion_module._entry_digest(entry, "macos") == first_digest

    second_installer.converge_activation()
    converged = json.loads(record_path.read_text(encoding="utf-8"))
    assert converged["state"] == "rolled_back"
    assert companion_module._entry_digest(entry, "macos") == first_digest
    assert json.loads(
        (root / "bootstrap" / "desktop-entry.json").read_text(encoding="utf-8")
    ) == first_receipt


@pytest.mark.parametrize(
    "failure_point",
    ("partial-copy", "copy", "atomic-rename"),
)
def test_backup_creation_crash_converges_and_same_transaction_retries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_point: str,
) -> None:
    release_key = Ed25519PrivateKey.generate()
    publication_key = Ed25519PrivateKey.generate()
    desktop = tmp_path / "Desktop"
    desktop.mkdir()
    root = tmp_path / "install"
    first, verifier, fetcher = _built_bootstrap(
        tmp_path,
        platform="macos",
        architecture="arm64",
        release_key=release_key,
        publication_key=publication_key,
        suffix=f"backup-first-{failure_point}",
    )
    installer = BootstrapCompanionInstaller(
        root,
        platform="macos",
        architecture="arm64",
        verifier=verifier,
        fetcher=fetcher,
        desktop_directory=desktop,
    )
    _prepare(installer, first, "b" * 32)
    installer.commit_activation("b" * 32)
    entry = desktop / "EcoreX.app"
    first_digest = companion_module._entry_digest(entry, "macos")
    first_receipt = json.loads(
        (root / "bootstrap" / "desktop-entry.json").read_text(encoding="utf-8")
    )

    second, second_verifier, second_fetcher = _built_bootstrap(
        tmp_path,
        platform="macos",
        architecture="arm64",
        release_key=release_key,
        publication_key=publication_key,
        suffix=f"backup-second-{failure_point}",
    )
    second_installer = BootstrapCompanionInstaller(
        root,
        platform="macos",
        architecture="arm64",
        verifier=second_verifier,
        fetcher=second_fetcher,
        desktop_directory=desktop,
    )
    transaction_id = "c" * 32
    transaction = root / "transactions" / transaction_id
    transaction.mkdir(parents=True)
    (transaction / "release-manifest.json").write_text(
        second.manifest.to_json(include_signature=True),
        encoding="utf-8",
    )
    second_installer.stage(second.manifest, transaction)
    record_path = (
        root
        / "bootstrap"
        / "companion-transactions"
        / transaction_id
        / "activation.json"
    )

    class SimulatedCrash(BaseException):
        pass

    if failure_point in {"partial-copy", "copy"}:
        real_copytree = companion_module.shutil.copytree

        def copy_then_crash(source, destination, *args, **kwargs):
            if failure_point == "partial-copy":
                partial = Path(destination)
                partial.mkdir()
                (partial / "partial-file").write_bytes(b"interrupted")
            else:
                real_copytree(source, destination, *args, **kwargs)
            raise SimulatedCrash

        monkeypatch.setattr(
            companion_module.shutil,
            "copytree",
            copy_then_crash,
        )
    else:
        real_replace = os.replace

        def rename_then_crash(source, destination):
            real_replace(source, destination)
            if (
                Path(source).name == "desktop-entry-backup.app.staging"
                and Path(destination).name == "desktop-entry-backup.app"
            ):
                raise SimulatedCrash

        monkeypatch.setattr(
            companion_module.os,
            "replace",
            rename_then_crash,
        )

    with pytest.raises(SimulatedCrash):
        second_installer.prepare_activation(second.manifest, transaction)
    monkeypatch.undo()

    interrupted = json.loads(record_path.read_text(encoding="utf-8"))
    assert interrupted["state"] == "preparing"
    assert companion_module._entry_digest(entry, "macos") == first_digest
    second_installer.converge_activation()
    converged = json.loads(record_path.read_text(encoding="utf-8"))
    assert converged["state"] == "rolled_back"
    assert companion_module._entry_digest(entry, "macos") == first_digest
    assert json.loads(
        (root / "bootstrap" / "desktop-entry.json").read_text(encoding="utf-8")
    ) == first_receipt
    assert not os.path.lexists(Path(interrupted["backup_path"]))
    assert not os.path.lexists(
        Path(interrupted["backup_path"]).with_name(
            Path(interrupted["backup_path"]).name + ".staging"
        )
    )

    second_installer.prepare_activation(second.manifest, transaction)
    assert companion_module._entry_digest(entry, "macos") != first_digest
    second_installer.rollback_activation(transaction_id)
    assert companion_module._entry_digest(entry, "macos") == first_digest


@pytest.mark.parametrize("recovery_action", ("converge", "prepare-retry"))
def test_committed_activation_crash_converges_backup_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    recovery_action: str,
) -> None:
    release_key = Ed25519PrivateKey.generate()
    publication_key = Ed25519PrivateKey.generate()
    desktop = tmp_path / "Desktop"
    desktop.mkdir()
    root = tmp_path / "install"
    first, verifier, fetcher = _built_bootstrap(
        tmp_path,
        platform="macos",
        architecture="arm64",
        release_key=release_key,
        publication_key=publication_key,
        suffix=f"commit-first-{recovery_action}",
    )
    installer = BootstrapCompanionInstaller(
        root,
        platform="macos",
        architecture="arm64",
        verifier=verifier,
        fetcher=fetcher,
        desktop_directory=desktop,
    )
    _prepare(installer, first, "d" * 32)
    installer.commit_activation("d" * 32)

    second, second_verifier, second_fetcher = _built_bootstrap(
        tmp_path,
        platform="macos",
        architecture="arm64",
        release_key=release_key,
        publication_key=publication_key,
        suffix=f"commit-second-{recovery_action}",
    )
    second_installer = BootstrapCompanionInstaller(
        root,
        platform="macos",
        architecture="arm64",
        verifier=second_verifier,
        fetcher=second_fetcher,
        desktop_directory=desktop,
    )
    transaction_id = "e" * 32
    transaction = root / "transactions" / transaction_id
    transaction.mkdir(parents=True)
    (transaction / "release-manifest.json").write_text(
        second.manifest.to_json(include_signature=True),
        encoding="utf-8",
    )
    second_installer.stage(second.manifest, transaction)
    second_installer.prepare_activation(second.manifest, transaction)
    record_path = (
        root
        / "bootstrap"
        / "companion-transactions"
        / transaction_id
        / "activation.json"
    )
    prepared = json.loads(record_path.read_text(encoding="utf-8"))
    backup = Path(prepared["backup_path"])
    assert os.path.lexists(backup)

    class SimulatedCrash(BaseException):
        pass

    def crash_before_cleanup(*_args, **_kwargs):
        raise SimulatedCrash

    monkeypatch.setattr(
        second_installer,
        "_cleanup_activation_backup",
        crash_before_cleanup,
    )
    with pytest.raises(SimulatedCrash):
        second_installer.commit_activation(transaction_id)
    monkeypatch.undo()

    interrupted = json.loads(record_path.read_text(encoding="utf-8"))
    assert interrupted["state"] == "committed"
    assert os.path.lexists(backup)
    if recovery_action == "converge":
        second_installer.converge_activation()
    else:
        second_installer.prepare_activation(second.manifest, transaction)
    assert not os.path.lexists(backup)
    assert not os.path.lexists(
        backup.with_name(backup.name + ".staging")
    )
    assert json.loads(record_path.read_text(encoding="utf-8"))["state"] == "committed"


def test_activation_record_and_receipt_cannot_escape_resolved_desktop(
    tmp_path: Path,
) -> None:
    release_key = Ed25519PrivateKey.generate()
    publication_key = Ed25519PrivateKey.generate()
    desktop = tmp_path / "Desktop"
    desktop.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    outside_entry = outside / "EcoreX.app"
    outside_entry.mkdir()
    (outside_entry / "user.txt").write_text("preserve", encoding="utf-8")
    root = tmp_path / "install"
    built, verifier, fetcher = _built_bootstrap(
        tmp_path,
        platform="macos",
        architecture="arm64",
        release_key=release_key,
        publication_key=publication_key,
        suffix="desktop-bound",
    )
    installer = BootstrapCompanionInstaller(
        root,
        platform="macos",
        architecture="arm64",
        verifier=verifier,
        fetcher=fetcher,
        desktop_directory=desktop,
    )
    transaction_id = "6" * 32
    _prepare(installer, built, transaction_id)
    record_path = (
        root
        / "bootstrap"
        / "companion-transactions"
        / transaction_id
        / "activation.json"
    )
    record = json.loads(record_path.read_text(encoding="utf-8"))
    record["entry_path"] = str(outside_entry)
    record["new_receipt"]["entry_path"] = str(outside_entry)
    record["new_receipt"]["entry_name"] = outside_entry.name
    record_path.write_text(json.dumps(record), encoding="utf-8")
    with pytest.raises(
        BootstrapCompanionError,
        match="activation record is invalid",
    ):
        installer.rollback_activation(transaction_id)
    assert (outside_entry / "user.txt").read_text(encoding="utf-8") == "preserve"

    record["entry_path"] = str(desktop / "EcoreX.app")
    record["new_receipt"]["entry_path"] = str(desktop / "EcoreX.app")
    record_path.write_text(json.dumps(record), encoding="utf-8")
    installer.commit_activation(transaction_id)
    receipt_path = root / "bootstrap" / "desktop-entry.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["entry_path"] = str(outside_entry)
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    assert installer.remove_desktop_entry() is False
    assert (outside_entry / "user.txt").read_text(encoding="utf-8") == "preserve"


@pytest.mark.parametrize("link_level", ("versions", "release", "bin"))
def test_versioned_bootstrap_rejects_directory_links_without_following(
    tmp_path: Path,
    link_level: str,
) -> None:
    release_key = Ed25519PrivateKey.generate()
    publication_key = Ed25519PrivateKey.generate()
    desktop = tmp_path / "Desktop"
    desktop.mkdir()
    root = tmp_path / "install"
    built, verifier, fetcher = _built_bootstrap(
        tmp_path,
        platform="macos",
        architecture="arm64",
        release_key=release_key,
        publication_key=publication_key,
        suffix=f"link-{link_level}",
    )
    installer = BootstrapCompanionInstaller(
        root,
        platform="macos",
        architecture="arm64",
        verifier=verifier,
        fetcher=fetcher,
        desktop_directory=desktop,
    )
    _prepare(installer, built, "7" * 32)
    versions = root / "bootstrap" / "versions"
    release = versions / built.manifest.release_id
    attacked = {
        "versions": versions,
        "release": release,
        "bin": release / "bin",
    }[link_level]
    moved = tmp_path / f"moved-{link_level}"
    os.replace(attacked, moved)
    try:
        _create_directory_link(attacked, moved)
    except OSError as error:
        os.replace(moved, attacked)
        pytest.skip(f"directory links are unavailable: {error}")
    try:
        transaction_id = "8" * 32
        transaction = root / "transactions" / transaction_id
        transaction.mkdir(parents=True)
        installer.stage(built.manifest, transaction)
        with pytest.raises(
            BootstrapCompanionError,
            match="unsafe|link|reparse",
        ):
            installer.prepare_activation(built.manifest, transaction)
    finally:
        _remove_directory_link(attacked)
        os.replace(moved, attacked)


def test_existing_versioned_bootstrap_rejects_file_symlink(
    tmp_path: Path,
) -> None:
    release_key = Ed25519PrivateKey.generate()
    publication_key = Ed25519PrivateKey.generate()
    desktop = tmp_path / "Desktop"
    desktop.mkdir()
    root = tmp_path / "install"
    built, verifier, fetcher = _built_bootstrap(
        tmp_path,
        platform="macos",
        architecture="arm64",
        release_key=release_key,
        publication_key=publication_key,
        suffix="file-link",
    )
    installer = BootstrapCompanionInstaller(
        root,
        platform="macos",
        architecture="arm64",
        verifier=verifier,
        fetcher=fetcher,
        desktop_directory=desktop,
    )
    _prepare(installer, built, "9" * 32)
    launcher = (
        root
        / "bootstrap"
        / "versions"
        / built.manifest.release_id
        / "bin"
        / "ecorex-bootstrap"
    )
    original = tmp_path / "original-bootstrap"
    os.replace(launcher, original)
    try:
        launcher.symlink_to(original)
    except OSError as error:
        os.replace(original, launcher)
        pytest.skip(f"file symlinks are unavailable: {error}")
    try:
        transaction = root / "transactions" / ("a" * 32)
        transaction.mkdir(parents=True)
        installer.stage(built.manifest, transaction)
        with pytest.raises(
            BootstrapCompanionError,
            match="link or reparse",
        ):
            installer.prepare_activation(built.manifest, transaction)
    finally:
        launcher.unlink()
        os.replace(original, launcher)


def _create_directory_link(link: Path, target: Path) -> None:
    if os.name == "nt":
        result = subprocess.run(
            [
                os.environ.get("COMSPEC", "cmd.exe"),
                "/d",
                "/c",
                "mklink",
                "/J",
                str(link),
                str(target),
            ],
            capture_output=True,
            check=False,
            timeout=10,
        )
        if result.returncode != 0:
            raise OSError("cannot create a Windows directory junction")
        return
    link.symlink_to(target, target_is_directory=True)


def _remove_directory_link(link: Path) -> None:
    if os.name == "nt":
        os.rmdir(link)
    else:
        link.unlink()
