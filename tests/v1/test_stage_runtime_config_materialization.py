from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from ecorex.release.repository_readiness import default_release_repository_contract
from ecorex.release.stage_runtime_config import (
    MAX_RUNTIME_CONFIG_BASE64_BYTES,
    MAX_RUNTIME_CONFIG_BYTES,
    StageRuntimeConfigError,
    decode_stage_runtime_config,
    materialize_stage_runtime_config,
    remove_stage_runtime_config,
)


ROOT = Path(__file__).resolve().parents[2]


def _payload() -> bytes:
    return json.dumps(
        {
            "identity": {
                "architecture": "x64",
                "platform": "windows",
                "version": "1.0.0",
            },
            "managed_gateway": "https://gateway.example.test",
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def _encoded(payload: bytes | None = None) -> tuple[str, str]:
    value = _payload() if payload is None else payload
    return base64.b64encode(value).decode(), hashlib.sha256(value).hexdigest()


def _payload_of_size(size: int) -> bytes:
    prefix = b'{"identity":{},"padding":"'
    suffix = b'"}'
    assert size >= len(prefix) + len(suffix)
    return prefix + (b"a" * (size - len(prefix) - len(suffix))) + suffix


def test_decode_authenticates_exact_json_bytes() -> None:
    encoded, digest = _encoded()

    assert decode_stage_runtime_config(encoded, expected_sha256=digest) == _payload()


@pytest.mark.parametrize(
    ("encoded", "digest", "code"),
    (
        ("not-base64", "a" * 64, "stage_runtime_config_base64_invalid"),
        (_encoded()[0], "A" * 64, "stage_runtime_config_digest_invalid"),
        (_encoded()[0], "b" * 64, "stage_runtime_config_digest_mismatch"),
    ),
)
def test_decode_fails_closed_on_transport_identity(
    encoded: str, digest: str, code: str
) -> None:
    with pytest.raises(StageRuntimeConfigError, match=code):
        decode_stage_runtime_config(encoded, expected_sha256=digest)


def test_decode_rejects_duplicate_keys_and_provider_transport_oversize() -> None:
    duplicate = b'{"identity":{},"identity":{}}'
    encoded, digest = _encoded(duplicate)
    with pytest.raises(StageRuntimeConfigError, match="stage_runtime_config_json_invalid"):
        decode_stage_runtime_config(encoded, expected_sha256=digest)

    oversized = _payload_of_size(MAX_RUNTIME_CONFIG_BYTES + 1)
    encoded, digest = _encoded(oversized)
    assert len(encoded) > MAX_RUNTIME_CONFIG_BASE64_BYTES
    with pytest.raises(
        StageRuntimeConfigError, match="stage_runtime_config_transport_too_large"
    ):
        decode_stage_runtime_config(encoded, expected_sha256=digest)


def test_decode_accepts_exact_github_variable_boundary() -> None:
    payload = _payload_of_size(MAX_RUNTIME_CONFIG_BYTES)
    encoded, digest = _encoded(payload)

    assert len(encoded) == MAX_RUNTIME_CONFIG_BASE64_BYTES
    assert decode_stage_runtime_config(encoded, expected_sha256=digest) == payload


def test_materialization_is_private_idempotent_and_digest_bound(tmp_path: Path) -> None:
    output = (tmp_path / "ecorex-runtime-config.json").resolve()
    encoded, digest = _encoded()

    first = materialize_stage_runtime_config(
        output, encoded=encoded, expected_sha256=digest
    )
    second = materialize_stage_runtime_config(
        output, encoded=encoded, expected_sha256=digest
    )

    assert first == second == {
        "schema_version": 1,
        "sha256": digest,
        "size_bytes": len(_payload()),
        "status": "materialized",
    }
    assert output.read_bytes() == _payload()
    if os.name != "nt":
        assert output.stat().st_mode & 0o077 == 0


def test_materialization_refuses_existing_conflict_and_alias(tmp_path: Path) -> None:
    output = (tmp_path / "ecorex-runtime-config.json").resolve()
    output.write_bytes(b"foreign")
    encoded, digest = _encoded()

    with pytest.raises(StageRuntimeConfigError, match="stage_runtime_config_output_conflict"):
        materialize_stage_runtime_config(
            output, encoded=encoded, expected_sha256=digest
        )

    if os.name != "nt":
        output.unlink()
        target = tmp_path / "target.json"
        target.write_bytes(_payload())
        output.symlink_to(target)
        with pytest.raises(StageRuntimeConfigError, match="stage_runtime_config_output_invalid"):
            materialize_stage_runtime_config(
                output, encoded=encoded, expected_sha256=digest
            )


def test_cleanup_removes_only_the_authenticated_file(tmp_path: Path) -> None:
    output = (tmp_path / "ecorex-runtime-config.json").resolve()
    encoded, digest = _encoded()
    materialize_stage_runtime_config(output, encoded=encoded, expected_sha256=digest)

    with pytest.raises(StageRuntimeConfigError, match="stage_runtime_config_cleanup_refused"):
        remove_stage_runtime_config(output, expected_sha256="b" * 64)
    assert output.exists()
    assert remove_stage_runtime_config(output, expected_sha256=digest) == {
        "schema_version": 1,
        "sha256": digest,
        "status": "removed",
    }
    assert remove_stage_runtime_config(output, expected_sha256=None) == {
        "schema_version": 1,
        "status": "absent",
    }


def test_cli_receipt_contains_identity_but_not_config_or_path(tmp_path: Path) -> None:
    output = (tmp_path / "ecorex-runtime-config.json").resolve()
    receipt = tmp_path / "receipt.json"
    encoded, digest = _encoded()
    environment = dict(os.environ)
    environment.update(
        {
            "ECOREX_STAGE_RUNTIME_CONFIG_BASE64": encoded,
            "ECOREX_STAGE_RUNTIME_CONFIG_SHA256": digest,
        }
    )

    result = subprocess.run(
        (
            sys.executable,
            str(ROOT / "scripts/materialize-v1-stage-runtime-config.py"),
            "materialize",
            "--output",
            str(output),
            "--receipt",
            str(receipt),
        ),
        cwd=ROOT,
        env=environment,
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    value = json.loads(receipt.read_text(encoding="utf-8"))
    assert value["sha256"] == digest
    serialized = receipt.read_text(encoding="utf-8")
    assert encoded not in serialized
    assert str(output) not in serialized


def test_cleanup_cli_runs_before_third_party_dependencies_are_installed(
    tmp_path: Path,
) -> None:
    output = (tmp_path / "ecorex-runtime-config.json").resolve()
    environment = dict(os.environ)
    environment.pop("PYTHONHOME", None)
    environment.pop("PYTHONPATH", None)
    environment["ECOREX_STAGE_RUNTIME_CONFIG_SHA256"] = "a" * 64

    result = subprocess.run(
        (
            sys.executable,
            "-S",
            str(ROOT / "scripts/materialize-v1-stage-runtime-config.py"),
            "remove",
            "--output",
            str(output),
        ),
        cwd=ROOT,
        env=environment,
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"schema_version": 1, "status": "absent"}


def test_workflows_isolate_exact_windows_stage_and_privileged_runners() -> None:
    stage = (ROOT / ".github/workflows/ecorex-v1-platform-stage.yml").read_text(
        encoding="utf-8"
    )
    candidate = (ROOT / ".github/workflows/ecorex-v1-candidate.yml").read_text(
        encoding="utf-8"
    )
    contract = default_release_repository_contract()

    assert 'runs_on: \'"windows-2022"\'' in stage
    assert "default: false" in stage
    assert "inputs.include_windows && '__none__' || 'windows-x64'" in stage
    assert "fromJSON(matrix.runs_on)" in stage
    assert "runs_on: windows-latest" not in stage
    assert "ECOREX_GITHUB_HOSTED_WINDOWS_NATIVE_COMPATIBILITY" in stage
    assert "macos-15" in stage
    assert "macos-15-intel" in stage
    assert "ECOREX_STAGE_RUNTIME_CONFIG_{0}_BASE64" in stage
    assert "materialize-v1-stage-runtime-config.py materialize" in stage
    assert "materialize-v1-stage-runtime-config.py remove" in stage
    assert "runs-on: ubuntu-24.04" in candidate
    assert "runs-on: [self-hosted, linux, x64, ecorex-image-soak]" not in candidate
    assert {runner.role for runner in contract.runners} == {
        "cloud-build",
        "deployment-authorize",
        "live-acceptance",
        "production-deploy",
        "production-readback",
        "release-publication",
        "release-sign",
    }
