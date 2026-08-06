from __future__ import annotations

import sqlite3
from types import SimpleNamespace

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from ecorex.capabilities import builtin_capability_registry
from ecorex.connectors import builtin_connector_registry
from ecorex.extensions import compose_extension_service
from ecorex.runtime import SQLiteDatabase
from ecorex.update import Ed25519SignatureVerifier


def _extension_rows(path) -> tuple[tuple[str, tuple[tuple, ...]], ...]:
    with sqlite3.connect(path) as connection:
        tables = tuple(
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type='table' "
                "AND name LIKE 'extension_%' ORDER BY name"
            ).fetchall()
        )
        return tuple(
            (
                table,
                tuple(
                    tuple(row)
                    for row in connection.execute(
                        f'SELECT * FROM "{table}" ORDER BY rowid'
                    ).fetchall()
                ),
            )
            for table in tables
        )


def test_product_extension_projection_only_build_then_converges_once(tmp_path) -> None:
    database = tmp_path / "runtime.db"
    SQLiteDatabase(database)
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    before = _extension_rows(database)
    cas_root = tmp_path / "extension-cas"
    runner = SimpleNamespace(supports=lambda _runtime: True, run=lambda *_a, **_k: None)
    runner_bindings: list[bool] = []

    service = compose_extension_service(
        database_path=database,
        product_version="1.0.0",
        core_build_digest="a" * 64,
        runtime_api_version="1.0.0",
        platform="windows",
        architecture="x64",
        capability_registry=builtin_capability_registry(),
        connector_registry=builtin_connector_registry(),
        installed_pack_ids=frozenset(),
        signature_verifier=Ed25519SignatureVerifier({"release": public}),
        skill_runner_factory=lambda store: (
            runner_bindings.append(store.root.is_dir()) or runner
        ),
        initialize=False,
        create_storage=False,
    )

    assert service.startup_converged is False
    assert service.project_snapshot().items == ()
    assert _extension_rows(database) == before
    assert not cas_root.exists()
    assert service.skill_runner is None
    assert runner_bindings == []

    service.converge_startup()
    assert service.startup_converged is True
    assert cas_root.is_dir()
    assert service.skill_runner is runner
    assert runner_bindings == [True]
    assert service.project_snapshot().items
    converged = _extension_rows(database)
    assert converged != before

    service.converge_startup()
    assert _extension_rows(database) == converged
