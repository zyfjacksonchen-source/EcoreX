from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import runpy

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from ecorex.release import Ed25519MemorySigner
from ecorex.release.protected_deployment import (
    DOMAIN,
    PUBLIC_SITE_SCHEMA_VERSION,
    ProtectedDeploymentAdmissionError,
    admission_sha256,
    public_site_v2_payload,
    sign_admission,
    verify_admission,
)


ZERO = "0" * 64
ONE = "1" * 64
TWO = "2" * 64


def _payload(now: datetime) -> dict[str, object]:
    return {
        "admission_id": "deploy-0123456789abcdef",
        "repository": "owner/ecorex",
        "commit_sha": "a" * 40,
        "channel": "stable",
        "candidate": {
            "workflow_run_id": 41,
            "run_attempt": 2,
            "artifact_id": 73,
            "artifact_sha256": ZERO,
            "release_id": "release-stable-0123456789abcdef01234567",
            "version": "1.0.0",
            "build_digest": ONE,
        },
        "gates": {
            "candidate": ZERO,
            "cdp_acceptance": ONE,
            "image_soak": TWO,
            "live_image": ZERO,
            "live_model": ONE,
            "signature": TWO,
        },
        "targets": {
            "cloud": {"artifact_sha256": ZERO, "manifest_sha256": ONE},
            "control_plane": {"release_manifest_sha256": TWO},
            "public_site": {"tree_sha256": ONE, "public_index_sha256": TWO},
        },
        "decision": {"mode": "create-and-activate", "rollout_percentage": 10},
        "issued_at": now.isoformat().replace("+00:00", "Z"),
        "expires_at": (now + timedelta(hours=1))
        .isoformat()
        .replace("+00:00", "Z"),
    }


def test_signed_admission_binds_every_production_target() -> None:
    now = datetime(2026, 7, 17, tzinfo=timezone.utc)
    signer = Ed25519MemorySigner("deployment-key", Ed25519PrivateKey.generate())
    document = sign_admission(_payload(now), signer=signer)

    body = verify_admission(
        document,
        public_keys={signer.key_id: signer.public_key_bytes},
        now=now + timedelta(minutes=1),
    )

    assert body["candidate"]["artifact_id"] == 73
    assert body["targets"]["cloud"]["artifact_sha256"] == ZERO
    assert body["targets"]["public_site"]["tree_sha256"] == ONE
    assert admission_sha256(document)
    assert DOMAIN.endswith(b"\0")


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("channel",), "dev"),
        (("decision", "rollout_percentage"), 0),
        (("candidate", "artifact_sha256"), "f" * 63),
        (("targets", "public_site", "tree_sha256"), TWO),
    ],
)
def test_admission_rejects_mutation(
    path: tuple[str, ...], value: object
) -> None:
    now = datetime(2026, 7, 17, tzinfo=timezone.utc)
    signer = Ed25519MemorySigner("deployment-key", Ed25519PrivateKey.generate())
    document = sign_admission(_payload(now), signer=signer)
    current: dict[str, object] = document["admission"]
    for name in path[:-1]:
        current = current[name]  # type: ignore[assignment]
    current[path[-1]] = value

    with pytest.raises(ProtectedDeploymentAdmissionError):
        verify_admission(
            document,
            public_keys={signer.key_id: signer.public_key_bytes},
            now=now + timedelta(minutes=1),
        )


def test_admission_expires_and_has_bounded_lifetime() -> None:
    now = datetime(2026, 7, 17, tzinfo=timezone.utc)
    signer = Ed25519MemorySigner("deployment-key", Ed25519PrivateKey.generate())
    document = sign_admission(_payload(now), signer=signer)

    with pytest.raises(
        ProtectedDeploymentAdmissionError,
        match="protected_deployment_admission_expired",
    ):
        verify_admission(
            document,
            public_keys={signer.key_id: signer.public_key_bytes},
            now=now + timedelta(hours=2),
        )

    payload = _payload(now)
    payload["expires_at"] = (now + timedelta(days=2)).isoformat().replace(
        "+00:00", "Z"
    )
    with pytest.raises(
        ProtectedDeploymentAdmissionError,
        match="protected_deployment_admission_time_invalid",
    ):
        sign_admission(payload, signer=signer)


def test_public_site_v2_uses_generic_admission_not_direct_waiver() -> None:
    now = datetime(2026, 7, 17, tzinfo=timezone.utc)
    signer = Ed25519MemorySigner("deployment-key", Ed25519PrivateKey.generate())
    document = sign_admission(_payload(now), signer=signer)
    body = document["admission"]
    authorization = public_site_v2_payload(
        admission=body,
        admission_digest=admission_sha256(document),
        release_id=body["candidate"]["release_id"],
        site_tree_sha256=body["targets"]["public_site"]["tree_sha256"],
        public_index_sha256=body["targets"]["public_site"]["public_index_sha256"],
        admin_identity_sha256=ZERO,
    )

    assert PUBLIC_SITE_SCHEMA_VERSION == 2
    assert "waiver_sha256" not in authorization
    assert "direct_receipt_sha256" not in authorization
    assert authorization["admission_sha256"] == admission_sha256(document)


def test_mutation_boundary_rejects_dispatch_decision_mismatch(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    now = datetime.now(timezone.utc)
    signer = Ed25519MemorySigner("deployment-key", Ed25519PrivateKey.generate())
    admission = tmp_path / "admission.json"
    admission.write_text(json.dumps(sign_admission(_payload(now), signer=signer)))
    script = runpy.run_path(
        str(
            Path(__file__).resolve().parents[2]
            / "scripts/verify-v1-protected-deployment-admission.py"
        )
    )
    trusted = base64.b64encode(signer.public_key_bytes).decode("ascii")

    result = script["run"](
        [
            "--admission",
            str(admission),
            "--trusted-key",
            f"{signer.key_id}={trusted}",
            "--repository",
            "owner/ecorex",
            "--commit-sha",
            "a" * 40,
            "--candidate-run-id",
            "41",
            "--candidate-artifact-id",
            "73",
            "--channel",
            "stable",
            "--mode",
            "create",
            "--rollout-percentage",
            "10",
        ]
    )

    assert result == 1
    assert "protected_deployment_dispatch_identity_mismatch" in capsys.readouterr().err


def test_checked_in_schemas_are_strict_and_versioned() -> None:
    root = Path(__file__).resolve().parents[2]
    admission = json.loads(
        (root / "release/v1/protected-deployment-admission.schema.json").read_text()
    )
    public_site = json.loads(
        (
            root / "release/v1/public-site-deployment-authorization.schema.json"
        ).read_text()
    )
    assert admission["properties"]["schema_version"]["const"] == 1
    assert admission["additionalProperties"] is False
    assert public_site["properties"]["schema_version"]["const"] == 2
    required = public_site["properties"]["authorization"]["required"]
    assert "admission_sha256" in required
    assert "waiver_sha256" not in required


def test_protected_workflow_dag_uses_isolated_roles_and_generic_admission() -> None:
    root = Path(__file__).resolve().parents[2]
    candidate = (root / ".github/workflows/ecorex-v1-candidate.yml").read_text()
    promote = (
        root / ".github/workflows/ecorex-v1-promote-candidate.yml"
    ).read_text()

    assert "\n  cloud-build-unsigned:" in candidate
    assert "\n  cloud-sign:" in candidate
    assert "\n  cloud-finalize:" in candidate
    assert "runs-on: [self-hosted, linux, arm64, ecorex-cloud-build]" in candidate
    assert "runs-on: [self-hosted, linux, x64, ecorex-release-sign]" in candidate
    assert "      - cloud-finalize" in candidate
    cloud_builder = candidate[
        candidate.index("\n  cloud-build-unsigned:") : candidate.index("\n  cloud-sign:")
    ]
    cloud_signer = candidate[
        candidate.index("\n  cloud-sign:") : candidate.index("\n  cloud-finalize:")
    ]
    assert "ECOREX_RELEASE_SIGNER_EXECUTABLE" not in cloud_builder
    assert ".cloud/artifact" not in cloud_signer
    assert "cloud-unsigned-signature-descriptor.json" in cloud_signer
    assert "cloud-release-manifest.signing-payload" in cloud_signer
    assert "actions/download-artifact" not in candidate[candidate.index("\n  cloud-build-unsigned:") :]
    for job in (
        "validate-request",
        "publish-and-promote",
        "authorize-production",
        "deploy-production",
        "authorize-finalization",
        "activate-rollout",
        "finalize-production",
    ):
        assert f"\n  {job}:" in promote
    assert "ecorex-deployment-authorize" in promote
    assert "ecorex-production-deploy" in promote
    assert "ecorex-production-readback" in promote
    assert "sign-v1-protected-deployment-admission.py" in promote
    assert "verify-v1-protected-deployment-admission.py" in promote
    assert "if: ${{ inputs.publication_mode == 'create-and-activate' }}" in promote
    assert '--mode "${{ inputs.publication_mode }}"' in promote
    assert '--rollout-percentage "${{ inputs.rollout_percentage }}"' in promote
    protected_tail = promote[promote.index("\n  authorize-production:") :]
    assert "direct-release" not in protected_tail
    assert "direct-deployable" not in protected_tail
