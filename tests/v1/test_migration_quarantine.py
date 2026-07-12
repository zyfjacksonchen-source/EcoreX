from __future__ import annotations

from datetime import UTC, datetime
import json
import os

from fastapi.testclient import TestClient
import pytest

from ecorex.migration import (
    MigrationQuarantineService,
    QuarantineStateError,
)
from ecorex.migration.crypto import SecretRecord, encrypt_quarantine
from ecorex.migration.migrator import QUARANTINE_NAME, REPORT_NAME
from ecorex.runtime import (
    RuntimeExecutionDenied,
    RuntimeExecutionGate,
    RuntimeKernel,
    RuntimeSettings,
    create_app,
)
from ecorex.runtime.commit_guard import transaction_commit_guard


TOKEN = "migration-runtime-" + "r" * 32
CSRF = "migration-csrf-" + "c" * 32
KEY = b"q" * 32
DIGEST = "a" * 64


def _seed(root, *, include_summary: bool = True):
    root.mkdir(parents=True, exist_ok=True)
    records = (
        SecretRecord("config.json", "open_ai_api_key", "sk-never-show-this"),
        SecretRecord("mcp.json", "mcpServers.docs.headers.Authorization", "Bearer hidden"),
    )
    quarantine = {
        "entry_count": 2,
        "activated": False,
        "uploaded": False,
    }
    if include_summary:
        quarantine["summary"] = [
            {"kind": "api_key", "origin": "product_configuration", "count": 1},
            {"kind": "access_token", "origin": "mcp_configuration", "count": 1},
        ]
    (root / REPORT_NAME).write_text(
        json.dumps(
            {
                "status": "completed",
                "source_inventory_digest": DIGEST,
                "quarantine": quarantine,
            }
        ),
        encoding="utf-8",
    )
    encrypt_quarantine(
        records,
        key=KEY,
        associated_digest=DIGEST,
        destination=root / QUARANTINE_NAME,
    )


def test_status_enumerates_only_aggregated_kinds_and_delete_is_idempotent(tmp_path) -> None:
    root = tmp_path / "migrated"
    _seed(root)
    service = MigrationQuarantineService(
        root,
        clock=lambda: datetime(2026, 7, 11, 3, 4, tzinfo=UTC),
    )

    available = service.status()
    assert available.status == "available"
    assert available.entry_count == 2
    assert available.can_delete is True
    assert available.items == (
        {"kind": "api_key", "origin": "product_configuration", "count": 1},
        {"kind": "access_token", "origin": "mcp_configuration", "count": 1},
    )
    assert "open_ai" not in repr(available.to_dict())
    assert "Authorization" not in repr(available.to_dict())
    assert "sk-never" not in repr(available.to_dict())

    deleted = service.delete(
        confirmed=True,
        client_request_id="delete-legacy-credentials-0001",
    )
    replay = service.delete(
        confirmed=True,
        client_request_id="delete-legacy-credentials-replay",
    )
    assert deleted == replay
    assert deleted.status == "deleted"
    assert not (root / QUARANTINE_NAME).exists()
    receipt = (root / "quarantine/legacy-secrets.deleted.json").read_text()
    assert "sk-never" not in receipt
    assert "Authorization" not in receipt


def test_interrupted_unlink_is_completed_without_restoring_or_decrypting_bytes(tmp_path) -> None:
    root = tmp_path / "interrupted"
    _seed(root)
    fired = False

    def fault(phase: str) -> None:
        nonlocal fired
        if phase == "after_quarantine_unlinked" and not fired:
            fired = True
            raise KeyboardInterrupt("simulated process loss")

    service = MigrationQuarantineService(root, fault_hook=fault)
    with pytest.raises(KeyboardInterrupt):
        service.delete(
            confirmed=True,
            client_request_id="delete-legacy-credentials-crash",
        )
    assert not (root / QUARANTINE_NAME).exists()
    assert service.status().status == "available"

    recovered = MigrationQuarantineService(root).delete(
        confirmed=True,
        client_request_id="delete-legacy-credentials-recover",
    )
    assert recovered.status == "deleted"
    assert MigrationQuarantineService(root).status().status == "deleted"


@pytest.mark.parametrize(
    ("phase", "original_exists", "deleting_exists", "receipt_state"),
    (
        ("before_delete_intent", True, False, None),
        ("before_quarantine_staged", True, False, "deleting"),
        ("before_quarantine_unlinked", False, True, "deleting"),
        ("before_delete_completed", False, False, "deleting"),
    ),
)
def test_execution_epoch_fences_each_irreversible_quarantine_phase(
    tmp_path,
    phase: str,
    original_exists: bool,
    deleting_exists: bool,
    receipt_state: str | None,
) -> None:
    root = tmp_path / phase
    _seed(root)
    gate = RuntimeExecutionGate()
    kernel = RuntimeKernel(root / "runtime.sqlite3")
    gate.record_report(kernel.invariants.audit())
    permit = gate.issue_permit(scope="quarantine_delete", subject=phase)

    def close_epoch(observed: str) -> None:
        if observed == phase:
            gate.mark_critical(error_code=f"test_{phase}")

    service = MigrationQuarantineService(root, fault_hook=close_epoch)
    with transaction_commit_guard(lambda: gate.assert_permit(permit)):
        with pytest.raises(RuntimeExecutionDenied):
            service.delete(
                confirmed=True,
                client_request_id=f"delete-legacy-{phase}",
            )

    assert (root / QUARANTINE_NAME).exists() is original_exists
    deleting = root / "quarantine/legacy-secrets.aesgcm.deleting"
    assert deleting.exists() is deleting_exists
    receipt_path = root / "quarantine/legacy-secrets.deleted.json"
    if receipt_state is None:
        assert not receipt_path.exists()
    else:
        assert json.loads(receipt_path.read_text(encoding="utf-8"))["state"] == receipt_state

    recovered = MigrationQuarantineService(root).delete(
        confirmed=True,
        client_request_id=f"delete-legacy-recover-{phase}",
    )
    assert recovered.status == "deleted"


def test_missing_summary_uses_one_non_provider_specific_legacy_category(tmp_path) -> None:
    root = tmp_path / "old-report"
    _seed(root, include_summary=False)
    projection = MigrationQuarantineService(root).status()
    assert projection.items == (
        {"kind": "credential", "origin": "product_configuration", "count": 2},
    )


def test_symlink_or_missing_receipt_fails_closed(tmp_path) -> None:
    root = tmp_path / "unsafe"
    _seed(root)
    quarantine = root / QUARANTINE_NAME
    outside = tmp_path / "outside"
    outside.write_bytes(quarantine.read_bytes())
    quarantine.unlink()
    try:
        os.symlink(outside, quarantine)
    except OSError:
        pytest.skip("this Windows account cannot create symbolic links")
    with pytest.raises(QuarantineStateError):
        MigrationQuarantineService(root).status()


def test_runtime_api_is_authenticated_csrf_protected_and_never_returns_secret_values(
    tmp_path,
) -> None:
    root = tmp_path / "runtime-root"
    _seed(root)
    app = create_app(
        settings=RuntimeSettings(
            database_path=root / "runtime.sqlite3",
            runtime_bearer_token=TOKEN,
            csrf_token=CSRF,
            webui_origins=("http://testserver",),
        )
    )
    auth = {"Authorization": f"Bearer {TOKEN}"}
    mutation = {
        **auth,
        "Origin": "http://testserver",
        "X-EcoreX-CSRF": CSRF,
    }
    with TestClient(app) as client:
        assert client.get("/api/v1/migration/quarantine").status_code == 401
        status = client.get("/api/v1/migration/quarantine", headers=auth)
        assert status.status_code == 200
        assert status.json()["status"] == "available"
        assert "sk-never" not in status.text
        assert client.post(
            "/api/v1/migration/quarantine/delete",
            headers=auth,
            json={"confirmed": True, "client_request_id": "delete-api-no-csrf"},
        ).status_code == 403
        deleted = client.post(
            "/api/v1/migration/quarantine/delete",
            headers=mutation,
            json={"confirmed": True, "client_request_id": "delete-api-with-csrf"},
        )
        assert deleted.status_code == 200
        assert deleted.json()["status"] == "deleted"
