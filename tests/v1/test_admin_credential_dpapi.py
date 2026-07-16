from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import runpy
import sys

import pytest

from ecorex.control_plane.management import AdminManagementRepository
from ecorex.control_plane.management_schema import AdminManagementSchemaManager


@pytest.mark.skipif(os.name != "nt", reason="Windows CurrentUser DPAPI")
def test_admin_credential_document_never_contains_plaintext(tmp_path: Path) -> None:
    module = runpy.run_path(
        str(
            Path(__file__).resolve().parents[2]
            / "scripts"
            / "ecorex-v1-admin-credential.py"
        )
    )
    path = tmp_path / "admin.json"
    description = module["_initialize"]("ecorex-platform-admin", path)
    value, credential = module["_load"](path)
    try:
        raw = path.read_bytes()
        assert bytes(credential) not in raw
        assert description["credential_sha256"] == value["credential_sha256"]
        assert "protected_credential_base64" not in json.dumps(description)
    finally:
        for index in range(len(credential)):
            credential[index] = 0


def test_platform_admin_bootstrap_creates_and_reuses_active_user(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    module = runpy.run_path(
        str(
            Path(__file__).resolve().parents[2]
            / "scripts"
            / "bootstrap-v1-platform-admin.py"
        )
    )
    database = tmp_path / "control-plane.db"
    key = b"k" * 32
    AdminManagementSchemaManager(database).migrate()
    monkeypatch.setenv("ECOREX_CP_DATABASE_PATH", str(database))
    monkeypatch.setenv(
        "ECOREX_CP_MODEL_CONFIG_ENCRYPTION_KEY_B64",
        base64.b64encode(key).decode("ascii"),
    )
    monkeypatch.setattr(sys, "argv", ["bootstrap-v1-platform-admin.py"])

    assert module["main"]() == 0
    first = json.loads(capsys.readouterr().out)
    assert first == {
        "account_id": "ecorex-platform-admin",
        "created": True,
        "ok": True,
    }
    assert module["main"]() == 0
    second = json.loads(capsys.readouterr().out)
    assert second == {
        "account_id": "ecorex-platform-admin",
        "created": False,
        "ok": True,
    }
    user = AdminManagementRepository(database, encryption_key=key).get_user(
        "ecorex-platform-admin"
    )
    assert user.status == "active"
