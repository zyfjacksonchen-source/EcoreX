from __future__ import annotations

import runpy
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_failed_round_diagnostic_is_redacted_and_bounded() -> None:
    module = runpy.run_path(str(ROOT / "scripts/run-v1-image-shared-storage-gate.py"))
    environment = {
        "ECOREX_TEST_POSTGRES_DSN": "postgresql://gate:postgres-secret@db/gate",
        "ECOREX_TEST_S3_ENDPOINT": "https://private-s3.example.test",
        "ECOREX_TEST_S3_ACCESS_KEY": "access-secret",
        "ECOREX_TEST_S3_SECRET_KEY": "s3-secret",
    }
    diagnostic = module["_redacted_tail"](
        b"x" * 9000 + b"\npostgresql://gate:postgres-secret@db/gate",
        b"\naccess-secret s3-secret https://private-s3.example.test real failure",
        environment=environment,
    )

    assert len(diagnostic.encode()) <= 8 * 1024
    assert "real failure" in diagnostic
    assert "[REDACTED]" in diagnostic
    assert all(secret not in diagnostic for secret in environment.values())
