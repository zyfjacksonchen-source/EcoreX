from __future__ import annotations

from typing import Mapping
import inspect

import pytest

from ecorex.connectors import (
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
