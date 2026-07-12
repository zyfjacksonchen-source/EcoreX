from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import fields
from datetime import datetime, timedelta, timezone
import hashlib

import pytest

from ecorex.artifacts import (
    ArtifactFamily,
    ArtifactRole,
    ArtifactScope,
    ArtifactService,
    ArtifactVisibility,
)


FIXED_NOW = datetime(
    2026,
    7,
    10,
    15,
    34,
    tzinfo=timezone(timedelta(hours=8)),
)


def _connector_result_preparation(
    service: ArtifactService,
    payload: bytes,
    *,
    index: int = 0,
):
    return service.prepare_artifact(
        payload,
        requested_name="飞书_读取文档_结果.json",
        mime_type="application/json; charset=utf-8",
        requested_visibility=ArtifactVisibility.SECONDARY,
        declaration=service.issue_trusted_deliverable_declaration(
            "connector.result",
            family=ArtifactFamily.DATA_EXPORT,
        ),
        scope=ArtifactScope(
            account_id="account-a",
            thread_id="thread-a",
            turn_id=f"turn-{index}",
            created_by_tool_id="connector_read",
        ),
    )


def test_prepared_artifact_keeps_exact_cas_identity_and_commits_without_path_leakage(
    tmp_path,
) -> None:
    service = ArtifactService(
        tmp_path / "artifacts",
        database_path=tmp_path / "runtime.db",
        clock=lambda: FIXED_NOW,
    )
    payload = '{"items":["上海","深圳"],"ok":true}'.encode()
    prepared = _connector_result_preparation(service, payload)

    assert prepared.sha256 == hashlib.sha256(payload).hexdigest()
    assert prepared.size_bytes == len(payload)
    assert prepared.mime_type == "application/json"
    assert service.blobs.read_bytes(prepared.sha256) == payload
    assert "path" not in {field.name for field in fields(prepared)}
    assert str(service.root) not in repr(prepared)

    with service.repository.database.transaction() as connection:
        projection = service.create_artifact_in_transaction(connection, prepared)

    assert projection.family is ArtifactFamily.DATA_EXPORT
    assert projection.role is ArtifactRole.DELIVERABLE
    assert projection.visibility is ArtifactVisibility.SECONDARY
    assert projection.sha256 == prepared.sha256
    assert projection.size_bytes == len(payload)
    assert projection.display_name == "飞书_读取文档_结果_20260710-1534_01.json"
    assert service.read_user_content(
        projection.artifact_id,
        account_id="account-a",
    ) == payload
    assert service.get_artifact_scope(projection.artifact_id) == prepared.scope
    assert service.list_user_artifacts(account_id="account-b") == ()
    public_payload = projection.to_dict()
    assert "path" not in public_payload
    assert "requested_name" not in public_payload
    assert str(service.root) not in repr(public_payload)


def test_transaction_rollback_leaves_only_unreachable_cas_bytes(tmp_path) -> None:
    service = ArtifactService(
        tmp_path / "artifacts",
        database_path=tmp_path / "runtime.db",
        clock=lambda: FIXED_NOW,
    )
    payload = b'{"rows":[1,2,3]}'
    prepared = _connector_result_preparation(service, payload)

    with pytest.raises(RuntimeError, match="force rollback"):
        with service.repository.database.transaction() as connection:
            projection = service.create_artifact_in_transaction(connection, prepared)
            assert connection.execute(
                "SELECT 1 FROM artifact_entities WHERE artifact_id=?",
                (projection.artifact_id,),
            ).fetchone() is not None
            raise RuntimeError("force rollback")

    assert service.list_user_artifacts(account_id="account-a") == ()
    assert service.blobs.read_bytes(prepared.sha256) == payload
    with service.repository.database.reader() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM artifact_entities"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM artifact_revisions"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM artifact_display_name_claims"
        ).fetchone()[0] == 0


def test_transactional_creation_rejects_missing_foreign_and_forged_authority(
    tmp_path,
) -> None:
    service = ArtifactService(
        tmp_path / "artifacts-a",
        database_path=tmp_path / "runtime-a.db",
        clock=lambda: FIXED_NOW,
    )
    other = ArtifactService(
        tmp_path / "artifacts-b",
        database_path=tmp_path / "runtime-b.db",
        clock=lambda: FIXED_NOW,
    )
    prepared = _connector_result_preparation(service, b'{"ok":true}')

    connection = service.repository.database.connect()
    try:
        with pytest.raises(RuntimeError, match="active Runtime database transaction"):
            service.create_artifact_in_transaction(connection, prepared)
    finally:
        connection.close()

    with other.repository.database.transaction() as foreign_connection:
        with pytest.raises(RuntimeError, match="different database"):
            service.create_artifact_in_transaction(foreign_connection, prepared)

    with service.repository.database.transaction() as connection:
        with pytest.raises(ValueError, match="not issued by this Artifact service"):
            other.create_artifact_in_transaction(connection, prepared)

    assert service.list_user_artifacts(account_id="account-a") == ()
    assert other.list_user_artifacts(account_id="account-a") == ()


def test_concurrent_external_transactions_keep_minute_names_unique(tmp_path) -> None:
    service = ArtifactService(
        tmp_path / "artifacts",
        database_path=tmp_path / "runtime.db",
        clock=lambda: FIXED_NOW,
    )
    prepared = tuple(
        _connector_result_preparation(
            service,
            f'{{"index":{index}}}'.encode(),
            index=index,
        )
        for index in range(64)
    )

    def commit(item):
        with service.repository.database.transaction() as connection:
            return service.create_artifact_in_transaction(connection, item)

    with ThreadPoolExecutor(max_workers=16) as pool:
        projections = list(pool.map(commit, prepared))

    assert len({item.artifact_id for item in projections}) == 64
    assert len({item.revision_id for item in projections}) == 64
    assert len({item.display_name.casefold() for item in projections}) == 64
    assert all(item.display_name.endswith(".json") for item in projections)
    assert len(service.list_user_artifacts(account_id="account-a")) == 64
    for item, expected in zip(projections, prepared, strict=True):
        assert service.read_user_content(
            item.artifact_id,
            account_id="account-a",
        ) == service.blobs.read_bytes(expected.sha256)
