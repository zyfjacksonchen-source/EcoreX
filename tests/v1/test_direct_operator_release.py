from __future__ import annotations

import base64
import copy
import hashlib
import json
import os
from pathlib import Path
import runpy

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
import pytest

from ecorex.release import (
    ArtifactBuildInput,
    ArtifactKind,
    DirectReleaseWaiverError,
    Ed25519MemorySigner,
    ReleaseBuilder,
    ReleaseBuildSpec,
    build_direct_release_waiver,
    candidate_receipt_signing_payload,
    parse_external_public_key_description,
    validate_direct_release_waiver,
)
from ecorex.update import (
    ReleaseChannel,
    ReleaseSource,
    SignatureEnvelope,
    SourceKind,
)


COMMIT = "a" * 40
INSTRUCTION_SHA256 = hashlib.sha256(b"operator direct release waiver").hexdigest()


def test_external_publication_key_description_is_public_only_and_digest_bound() -> None:
    public = Ed25519PrivateKey.generate().public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    value = {
        "schema_version": 1,
        "role": "publication",
        "algorithm": "ed25519",
        "key_id": "ecorex-production-publication-2026",
        "public_key_base64": base64.b64encode(public).decode("ascii"),
        "public_key_sha256": hashlib.sha256(public).hexdigest(),
    }
    assert parse_external_public_key_description(
        value, expected_role="publication"
    ) == ("ecorex-production-publication-2026", public)
    tampered = dict(value, public_key_sha256="0" * 64)
    with pytest.raises(DirectReleaseWaiverError, match="description_invalid"):
        parse_external_public_key_description(
            tampered, expected_role="publication"
        )


def _release(tmp_path: Path):
    source = tmp_path / "bootstrap"
    source.mkdir()
    (source / "ecorex-bootstrap.exe").write_bytes(b"real staged bootstrap")
    release_private = Ed25519PrivateKey.generate()
    publication_private = Ed25519PrivateKey.generate()
    release_signer = Ed25519MemorySigner("direct-release-test", release_private)
    publication_signer = Ed25519MemorySigner(
        "direct-publication-test", publication_private
    )
    built = ReleaseBuilder(release_signer).build(
        ReleaseBuildSpec(
            channel=ReleaseChannel.STABLE,
            created_at="2026-07-16T12:00:00Z",
            sources=(
                ReleaseSource(
                    "github-cn",
                    SourceKind.GITHUB_CN_MIRROR,
                    0,
                    "https://mirror.example/v1.0.0/stable",
                ),
                ReleaseSource(
                    "github",
                    SourceKind.GITHUB_RELEASE,
                    1,
                    "https://github.com/example/ecorex/releases/download",
                ),
                ReleaseSource(
                    "cdn",
                    SourceKind.ECOREX_CDN,
                    2,
                    "https://cdn.example/v1.0.0/stable",
                ),
            ),
            artifacts=(
                ArtifactBuildInput(
                    source_dir=source,
                    kind=ArtifactKind.BOOTSTRAP,
                    platform="windows",
                    architecture="x64",
                    executable_paths=("ecorex-bootstrap.exe",),
                ),
            ),
        ),
        tmp_path / "release",
    )
    release_public = release_signer.public_key_bytes
    publication_public = publication_signer.public_key_bytes
    return built, release_signer, release_public, publication_signer, publication_public


def _candidate_receipt(built, signer: Ed25519MemorySigner) -> tuple[dict, bytes]:
    manifest_bytes = built.manifest_path.read_bytes()
    value = {
        "schema_version": 2,
        "receipt_type": "ecorex-candidate-build",
        "status": "passed",
        "code": None,
        "commit_sha": COMMIT,
        "staging_provenance": {
            "workflow_path": ".github/workflows/ecorex-v1-platform-stage.yml",
            "workflow_run_id": 42,
            "run_attempt": 1,
            "receipt_sha256": "b" * 64,
        },
        "release_id": built.manifest.release_id,
        "version": built.manifest.version,
        "channel": built.manifest.channel.value,
        "build_digest": built.manifest.build_digest,
        "python_dependency_lock_sha256": "c" * 64,
        "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "web_tree_sha256": "d" * 64,
        "stage_receipts": {"stage": "e" * 64},
        "artifacts": {},
        "signing": {
            "algorithm": "ed25519",
            "key_id": signer.key_id,
            "operation_count": 2,
            "executable_sha256": "f" * 64,
            "adapter_sha256": "1" * 64,
        },
    }
    signature = signer.sign(candidate_receipt_signing_payload(value))
    value["signature"] = SignatureEnvelope(
        "ed25519", signer.key_id, base64.b64encode(signature).decode("ascii")
    ).to_dict()
    payload = (
        json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()
    return value, payload


def test_direct_waiver_never_represents_skipped_gates_as_passed(
    tmp_path: Path,
) -> None:
    built, release_signer, release_public, publication_signer, publication_public = (
        _release(tmp_path)
    )
    receipt, receipt_bytes = _candidate_receipt(built, release_signer)
    manifest_bytes = built.manifest_path.read_bytes()

    waiver = build_direct_release_waiver(
        manifest=built.manifest,
        manifest_bytes=manifest_bytes,
        candidate_receipt=receipt,
        candidate_receipt_bytes=receipt_bytes,
        commit_sha=COMMIT,
        operator_instruction_sha256=INSTRUCTION_SHA256,
        signer=release_signer,
        signer_public_key=release_public,
        publication_key_id=publication_signer.key_id,
        publication_public_key=publication_public,
        created_at="2026-07-16T12:30:00Z",
    )

    assert waiver["status"] == "operator-waived"
    assert waiver["protected_pipeline"]["represented_as_passed"] is False
    assert {
        item["status"] for item in waiver["protected_pipeline"]["gates"].values()
    } == {
        "not-run",
        "substituted-by-dpapi-and-attested-encrypted-volume-software-keys",
    }
    assert waiver["publication"] == {
        "status": "not-yet-published",
        "live_pointer_authorized": False,
        "required_publication_policy": "stable-primary-only",
        "required_source_ids": ["github-cn"],
        "requires_published_signed_index": True,
    }

    tampered = copy.deepcopy(waiver)
    tampered["protected_pipeline"]["gates"]["managed-live-cdp-acceptance"] = {
        "status": "passed",
        "represented_as_passed": True,
    }
    with pytest.raises(DirectReleaseWaiverError, match="false_pass"):
        validate_direct_release_waiver(
            tampered,
            expected_manifest=built.manifest,
            expected_manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
            expected_candidate_receipt_sha256=hashlib.sha256(
                receipt_bytes
            ).hexdigest(),
            expected_commit_sha=COMMIT,
            expected_operator_instruction_sha256=INSTRUCTION_SHA256,
            release_public_key=release_public,
            publication_key_id=publication_signer.key_id,
            publication_public_key=publication_public,
        )


@pytest.mark.skipif(os.name != "nt", reason="Windows DPAPI contract")
def test_dpapi_adapter_keeps_two_distinct_seeds_out_of_public_description(
    tmp_path: Path,
) -> None:
    module = runpy.run_path(
        str(
            Path(__file__).resolve().parents[2]
            / "scripts"
            / "ecorex-v1-dpapi-ed25519-signer.py"
        )
    )
    description = module["_initialize"](tmp_path)
    release_payload = b"candidate manifest payload"
    publication_payload = b"ecorex.public-bootstrap-freshness.v1\0pointer"
    release_signature = module["_sign"](release_payload, tmp_path)
    publication_signature = module["_sign"](publication_payload, tmp_path)

    release_public = base64.b64decode(
        description["release"]["public_key_base64"], validate=True
    )
    publication_public = base64.b64decode(
        description["publication"]["public_key_base64"], validate=True
    )
    assert release_public != publication_public
    Ed25519PublicKey.from_public_bytes(release_public).verify(
        release_signature, release_payload
    )
    Ed25519PublicKey.from_public_bytes(publication_public).verify(
        publication_signature, publication_payload
    )
    public_description = json.dumps(description, sort_keys=True)
    for role in ("release", "publication"):
        stored = json.loads((tmp_path / f"{role}-key.json").read_text())
        assert stored["protected_seed_base64"] not in public_description
        assert stored["protection"] == "windows-dpapi-current-user"
        assert "private" not in public_description.casefold()
