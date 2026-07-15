from __future__ import annotations

from datetime import UTC, datetime, timedelta
import sqlite3

from fastapi.testclient import TestClient

from ecorex.protocol import CreateTurnRequest, InteractionKind, TurnStatus
from ecorex.runtime import RuntimeKernel, RuntimeSettings, create_app
from ecorex.runtime.invariants import RuntimeInvariantAuditor


def _logical_database_snapshot(database) -> tuple:
    """Compare durable facts without depending on SQLite page layout/WAL state."""

    with sqlite3.connect(database) as connection:
        tables = [
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_schema "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()
        ]
        snapshot = []
        for table in tables:
            quoted_table = '"' + table.replace('"', '""') + '"'
            columns = [
                str(row[1])
                for row in connection.execute(
                    f"PRAGMA table_info({quoted_table})"
                ).fetchall()
            ]
            order = ", ".join(
                '"' + column.replace('"', '""') + '"' for column in columns
            )
            rows = connection.execute(
                f"SELECT * FROM {quoted_table}" + (f" ORDER BY {order}" if order else "")
            ).fetchall()
            snapshot.append((table, tuple(columns), tuple(tuple(row) for row in rows)))
        return tuple(snapshot)


def test_cold_start_audits_before_any_business_convergence(
    tmp_path,
    monkeypatch,
) -> None:
    database = tmp_path / "runtime.db"
    kernel = RuntimeKernel(database)
    thread = kernel.create_thread()
    created = kernel.create_turn(
        thread.thread_id,
        CreateTurnRequest(input="等待人工输入"),
    )
    kernel.transition_turn(created.turn.turn_id, TurnStatus.PREPARING)
    leased = kernel.jobs.lease_next("bootstrap-boundary-worker")
    assert leased is not None and leased.lease_token is not None
    kernel.jobs.start(
        leased.job_id,
        "bootstrap-boundary-worker",
        leased.lease_token,
    )
    interaction = kernel.request_interaction(
        job_id=leased.job_id,
        worker_id="bootstrap-boundary-worker",
        lease_token=leased.lease_token,
        kind=InteractionKind.INFORMATION,
        prompt="请补充信息",
        idempotency_key="bootstrap-boundary-interaction",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    corrupt_thread = kernel.create_thread()
    corrupt = kernel.create_turn(
        corrupt_thread.thread_id,
        CreateTurnRequest(input="制造只读保护样本"),
    )
    with kernel.database.transaction() as connection:
        connection.execute(
            "UPDATE interactions SET expires_at=? WHERE interaction_id=?",
            (
                (datetime.now(UTC) - timedelta(seconds=2)).isoformat(),
                interaction.interaction_id,
            ),
        )
        connection.execute(
            "UPDATE turns SET status='completed' WHERE turn_id=?",
            (corrupt.turn.turn_id,),
        )
        assert int(
            connection.execute(
                "SELECT COUNT(*) FROM output_preferences"
            ).fetchone()[0]
        ) == 0

    observed_output_counts: list[int] = []
    original_audit = RuntimeInvariantAuditor.audit

    def observed_audit(self):
        with self.database.reader() as connection:
            observed_output_counts.append(
                int(
                    connection.execute(
                        "SELECT COUNT(*) FROM output_preferences"
                    ).fetchone()[0]
                )
            )
        return original_audit(self)

    monkeypatch.setattr(RuntimeInvariantAuditor, "audit", observed_audit)
    durable_before_startup = _logical_database_snapshot(database)
    app = create_app(
        settings=RuntimeSettings(
            database_path=database,
            runtime_bearer_token="r" * 43,
            csrf_token="c" * 43,
            webui_origins=("http://testserver",),
        )
    )

    assert observed_output_counts and observed_output_counts[0] == 0
    assert all(count == 0 for count in observed_output_counts)
    assert _logical_database_snapshot(database) == durable_before_startup
    assert not (tmp_path / "artifacts").exists()
    assert not (tmp_path / "extension-cas").exists()
    assert not (tmp_path / "outputs").exists()
    assert list(tmp_path.glob(".*.audit-key")) == []
    assert app.state.runtime_execution_gate.snapshot().status == "critical"
    assert app.state.runtime.interactions.get(interaction.interaction_id).status.value == (
        "pending"
    )
    assert app.state.runtime.jobs.get(leased.job_id).status.value == "waiting_human"
    events = app.state.runtime.events.page(thread.thread_id, limit=1000).events
    assert all(event.event_type != "interaction.expired" for event in events)
    protected = _logical_database_snapshot(database)
    with TestClient(app) as client:
        response = client.get(
            "/api/v1/system/health?technical=true",
            headers={"Authorization": f"Bearer {'r' * 43}"},
        )
        assert response.status_code == 200
        assert response.json()["overall"] == "critical"
        assert _logical_database_snapshot(database) == protected
    assert _logical_database_snapshot(database) == protected


def test_product_get_catalogs_and_health_are_strict_database_projections(
    tmp_path,
) -> None:
    database = tmp_path / "runtime.db"
    token = "r" * 43
    app = create_app(
        settings=RuntimeSettings(
            database_path=database,
            runtime_bearer_token=token,
            csrf_token="c" * 43,
            webui_origins=("http://testserver",),
        )
    )
    client = TestClient(app)
    headers = {"Authorization": f"Bearer {token}"}
    paths = (
        "/api/v1/bootstrap",
        "/api/v1/update",
        "/api/v1/threads",
        "/api/v1/connectors",
        "/api/v1/extensions",
        "/api/v1/memory",
        "/api/v1/output/locations",
        "/api/v1/output/preference",
        "/api/v1/artifacts",
        "/api/v1/migration/quarantine",
        "/api/v1/observability/audit",
        "/api/v1/system/health",
        "/api/v1/system/metrics",
    )
    expected = _logical_database_snapshot(database)
    for path in paths:
        response = client.get(path, headers=headers)
        assert response.status_code == 200, (path, response.text)
        assert _logical_database_snapshot(database) == expected, path


def test_healthy_startup_converges_defaults_once_and_restart_is_idempotent(
    tmp_path,
) -> None:
    database = tmp_path / "runtime.db"
    settings = RuntimeSettings(
        database_path=database,
        runtime_bearer_token="r" * 43,
        csrf_token="c" * 43,
        webui_origins=("http://testserver",),
    )
    first = create_app(settings=settings)
    assert first.state.runtime_execution_gate.snapshot().healthy
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM runtime_permission_state"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM connector_definitions"
        ).fetchone()[0] >= 2
        assert connection.execute(
            "SELECT COUNT(*) FROM runtime_snapshots"
        ).fetchone()[0] >= 4
        assert connection.execute(
            "SELECT COUNT(*) FROM output_preferences"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT value FROM memory_meta WHERE key='revision'"
        ).fetchone()[0] == "0"
        assert connection.execute(
            "SELECT COUNT(*) FROM observability_audit_cursors"
        ).fetchone()[0] == 1
    assert (tmp_path / "artifacts" / "blobs").is_dir()
    assert (tmp_path / "extension-cas").is_dir()
    assert (tmp_path / "outputs").is_dir()

    converged = _logical_database_snapshot(database)
    second = create_app(
        settings=RuntimeSettings(
            database_path=database,
            runtime_bearer_token="r" * 43,
            csrf_token="c" * 43,
            webui_origins=("http://testserver",),
        )
    )
    assert second.state.runtime_execution_gate.snapshot().healthy
    assert _logical_database_snapshot(database) == converged
