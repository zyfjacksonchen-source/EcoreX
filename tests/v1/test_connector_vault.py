from __future__ import annotations

from typing import Mapping
import inspect
import os

import pytest

from ecorex.connectors import (
    EphemeralEncryptedCredentialVault,
    LocalEncryptedCredentialVault,
    MacOSKeychainCredentialVault,
    WindowsCredentialVault,
    production_credential_vault,
)
import ecorex.connectors.vault as vault_module


class FakeBinaryBackend:
    def __init__(self) -> None:
        self.values: dict[str, bytes] = {}
        self.fail: str | None = None

    def put(self, reference: str, payload: bytes) -> None:
        if self.fail == "put":
            raise RuntimeError(payload.decode("utf-8"))
        self.values[reference] = bytes(payload)

    def get(self, reference: str) -> bytes:
        if self.fail == "get":
            raise RuntimeError("TOP-SECRET-IN-BACKEND-ERROR")
        return self.values[reference]

    def delete(self, reference: str) -> None:
        if self.fail == "delete":
            raise RuntimeError("TOP-SECRET-IN-BACKEND-ERROR")
        self.values.pop(reference, None)


@pytest.mark.parametrize("vault_type", [WindowsCredentialVault, MacOSKeychainCredentialVault])
def test_platform_vault_round_trip_uses_injected_backend(vault_type) -> None:
    backend = FakeBinaryBackend()
    vault = vault_type(backend=backend)
    material: Mapping[str, str] = {"access_token": "TOP-SECRET", "refresh_token": "REFRESH"}
    vault.put("ecorex/connectors/test", material)
    assert b"TOP-SECRET" in backend.values["ecorex/connectors/test"]
    assert vault.get("ecorex/connectors/test") == material
    vault.delete("ecorex/connectors/test")
    assert backend.values == {}


def test_platform_vault_errors_never_echo_backend_or_secret_data() -> None:
    backend = FakeBinaryBackend()
    vault = WindowsCredentialVault(backend=backend)
    backend.fail = "put"
    with pytest.raises(RuntimeError) as error:
        vault.put("ecorex/connectors/test", {"access_token": "TOP-SECRET"})
    assert str(error.value) == "credential vault write failed"
    assert "TOP-SECRET" not in str(error.value)
    backend.fail = "get"
    with pytest.raises(RuntimeError) as error:
        vault.get("ecorex/connectors/test")
    assert str(error.value) == "credential vault read failed"
    backend.fail = "delete"
    with pytest.raises(RuntimeError) as error:
        vault.delete("ecorex/connectors/test")
    assert str(error.value) == "credential vault delete failed"


def test_unsupported_platform_vault_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(vault_module.sys, "platform", "linux")
    with pytest.raises(RuntimeError, match="no supported production credential vault"):
        production_credential_vault()


def test_macos_vault_uses_security_framework_without_secret_argv() -> None:
    source = inspect.getsource(vault_module._MacOSKeychainBackend)
    assert "SecItemAdd" in source
    assert "SecItemCopyMatching" in source
    assert "subprocess" not in source
    assert "add-generic-password" not in source


def test_platform_vaults_use_emate_product_identity() -> None:
    assert vault_module._WindowsCredentialBackend._target("test") == "e-Mate:test"
    assert 'credential.UserName = "e-Mate"' in inspect.getsource(
        vault_module._WindowsCredentialBackend
    )
    assert (
        vault_module._MacOSKeychainBackend._SERVICE
        == "net.ecoremedia.emate.connector-credentials"
    )


def test_acceptance_vault_survives_restart_without_plaintext(tmp_path) -> None:
    path = tmp_path / "acceptance.vault"
    key = os.urandom(32)
    first = EphemeralEncryptedCredentialVault(path, key=key)
    first.put(
        "ecorex/session/test",
        {"access_token": "TOP-SECRET", "refresh_token": "REFRESH-SECRET"},
    )

    payload = path.read_bytes()
    assert b"TOP-SECRET" not in payload
    assert b"REFRESH-SECRET" not in payload
    second = EphemeralEncryptedCredentialVault(path, key=key)
    assert second.get("ecorex/session/test") == {
        "access_token": "TOP-SECRET",
        "refresh_token": "REFRESH-SECRET",
    }
    with pytest.raises(RuntimeError, match="read failed"):
        EphemeralEncryptedCredentialVault(path, key=os.urandom(32)).get(
            "ecorex/session/test"
        )
    second.delete("ecorex/session/test")
    assert not path.exists()


def test_local_desktop_vault_survives_restart_without_keychain_or_plaintext(
    tmp_path,
) -> None:
    reference = "ecorex/session/test"
    material = {"access_token": "TOP-SECRET", "refresh_token": "REFRESH-SECRET"}

    first = LocalEncryptedCredentialVault(tmp_path)
    first.put(reference, material)
    second = LocalEncryptedCredentialVault(tmp_path)

    assert second.get(reference) == material
    assert b"TOP-SECRET" not in (tmp_path / ".credential-vault").read_bytes()
    assert (tmp_path / ".credential-vault.key").stat().st_mode & 0o077 == 0
    assert (tmp_path / ".credential-vault").stat().st_mode & 0o077 == 0


def test_local_desktop_vault_fsyncs_each_directory_entry_mutation(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = []
    original = vault_module._fsync_parent

    def tracked(path):
        calls.append(path)
        original(path)

    monkeypatch.setattr(vault_module, "_fsync_parent", tracked)
    vault = LocalEncryptedCredentialVault(tmp_path)
    vault.put("ecorex/session/test", {"access_token": "first"})
    vault.put("ecorex/session/test", {"access_token": "replacement"})
    vault.delete("ecorex/session/test")

    assert calls == [
        tmp_path / ".credential-vault.key",
        tmp_path / ".credential-vault",
        tmp_path / ".credential-vault",
        tmp_path / ".credential-vault",
    ]
