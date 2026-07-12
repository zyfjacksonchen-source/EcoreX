from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta
import hashlib
import json

import httpx
import pytest

from ecorex.release import (
    HTTPSPublicBootstrapIndexPublisher,
    PublicBootstrapPublicationError,
    public_bootstrap_authority_signing_bytes,
)


PUBLIC_URL = "https://download.example/stable/public-bootstrap-index.json"


class Credential:
    def bearer_token(self) -> str:
        return "protected-publication-token"


class AcceptingVerifier:
    def verify(self, payload, signature) -> bool:
        assert payload
        assert signature.algorithm == "ed25519"
        return True


def _index_bytes() -> bytes:
    release_id = "release-stable-" + "a" * 24
    target = {
        "manifest_sha256": "d" * 64,
        "release_id": release_id,
        "version": "1.0.0",
        "build_digest": "b" * 64,
    }
    signature = {
        "algorithm": "ed25519",
        "key_id": "release-key",
        "value": base64.b64encode(b"s" * 64).decode("ascii"),
    }
    freshness_signature = {
        "algorithm": "ed25519",
        "key_id": "publication-key",
        "value": base64.b64encode(b"f" * 64).decode("ascii"),
    }
    authority_sha256 = hashlib.sha256(
        public_bootstrap_authority_signing_bytes(
            sequence=1,
            revision=release_id,
            target=target,
        )
    ).hexdigest()
    now = datetime.now(UTC).replace(microsecond=0)
    sources = (
        ("mirror", "github-cn-mirror", 0, "https://mirror.example/release"),
        ("github", "github-release", 1, "https://github.example/release"),
        ("cdn", "ecorex-cdn", 2, "https://cdn.example/release"),
    )

    def links(name: str) -> list[dict[str, object]]:
        return [
            {
                "source_id": source_id,
                "kind": kind,
                "priority": priority,
                "url": f"{root}/{name}",
            }
            for source_id, kind, priority, root in sources
        ]

    targets = (
        ("bootstrap-windows-x64", "windows", "x64", "bootstrap-windows-x64.zip"),
        ("bootstrap-macos-arm64", "macos", "arm64", "bootstrap-macos-arm64.zip"),
        ("bootstrap-macos-x64", "macos", "x64", "bootstrap-macos-x64.zip"),
    )
    value = {
        "schema_version": 1,
        "document_type": "ecorex.public-bootstrap-discovery",
        "trust": "untrusted-discovery-hint",
        "status": "published",
        "authority": {
            "sequence": 1,
            "revision": release_id,
            "target": target,
            "signature": signature,
        },
        "freshness": {
            "authority_sha256": authority_sha256,
            "issued_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "expires_at": (now + timedelta(hours=1)).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            ),
            "signature": freshness_signature,
        },
        "release": {
            "release_id": release_id,
            "version": "1.0.0",
            "channel": "stable",
            "created_at": "2026-07-11T12:00:00+08:00",
            "build_digest": "b" * 64,
            "publication_receipt_sha256": "c" * 64,
            "manifest": {
                "file_name": "release-manifest.json",
                "sha256": "d" * 64,
                "signature": signature,
                "sources": links("release-manifest.json"),
            },
            "bootstrap_artifacts": [
                {
                    "artifact_id": artifact_id,
                    "platform": platform,
                    "architecture": architecture,
                    "file_name": file_name,
                    "size_bytes": 1024,
                    "sha256": hashlib.sha256(artifact_id.encode()).hexdigest(),
                    "signature": signature,
                    "sources": links(file_name),
                }
                for artifact_id, platform, architecture, file_name in targets
            ],
        },
    }
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
        + b"\n"
    )


def _publisher(handler, *, verifier=None) -> HTTPSPublicBootstrapIndexPublisher:
    return HTTPSPublicBootstrapIndexPublisher(
        endpoint="https://control.example/api/v1/bootstrap-index",
        allowed_hosts=frozenset({"control.example"}),
        public_url=PUBLIC_URL,
        public_hosts=frozenset({"download.example"}),
        credentials=Credential(),
        verifier=verifier or AcceptingVerifier(),
        freshness_verifier=AcceptingVerifier(),
        client=httpx.Client(
            transport=httpx.MockTransport(handler),
            follow_redirects=False,
            trust_env=False,
        ),
    )


def _stage_response(
    payload: bytes,
    *,
    previous: dict[str, object] | None = None,
    stage_id: str = "bstage_" + "1" * 32,
) -> dict[str, object]:
    index = json.loads(payload)
    authority = index["authority"]
    freshness = index["freshness"]
    return {
        "schema_version": 1,
        "release_id": index["release"]["release_id"],
        "version": index["release"]["version"],
        "state": "staged",
        "index_sha256": hashlib.sha256(payload).hexdigest(),
        "index_size_bytes": len(payload),
        "public_url": PUBLIC_URL,
        "revision_id": stage_id,
        "authority_sequence": authority["sequence"],
        "authority_revision_id": authority["revision"],
        "authority_target": authority["target"],
        "freshness_issued_at": freshness["issued_at"],
        "freshness_expires_at": freshness["expires_at"],
        "active_activation_record_id": (
            previous["activation"] if previous is not None else None
        ),
        "active_sequence": previous["sequence"] if previous is not None else None,
        "active_authority_revision_id": (
            previous["revision"] if previous is not None else None
        ),
        "active_index_sha256": (
            previous["index_sha256"] if previous is not None else None
        ),
        "active_target": previous["target"] if previous is not None else None,
    }


def _active_response(
    payload: bytes,
    *,
    previous: dict[str, object] | None = None,
    stage_id: str = "bstage_" + "1" * 32,
) -> dict[str, object]:
    index = json.loads(payload)
    authority = index["authority"]
    freshness = index["freshness"]
    release = index["release"]
    digest = hashlib.sha256(payload).hexdigest()
    activation_id = "bactive_" + "2" * 32
    proof = {
        "schema_version": 1,
        "record_id": "bread_" + "4" * 32,
        "activation_record_id": activation_id,
        "stage_record_id": stage_id,
        "release_id": release["release_id"],
        "version": release["version"],
        "build_digest": release["build_digest"],
        "sequence": authority["sequence"],
        "revision": authority["revision"],
        "issued_at": freshness["issued_at"],
        "expires_at": freshness["expires_at"],
        "target": authority["target"],
        "index_sha256": digest,
        "index_size_bytes": len(payload),
        "public_url": PUBLIC_URL,
        "read_back_at": "2026-07-12T00:00:00+00:00",
    }
    proof_digest = hashlib.sha256(
        json.dumps(
            proof,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    proof["proof_token"] = (
        f"bootstrap-index-proof:{proof['record_id']}:sha256:{proof_digest}"
    )
    return {
        "schema_version": 1,
        "release_id": release["release_id"],
        "version": release["version"],
        "state": "active-and-read-back",
        "index_sha256": digest,
        "index_size_bytes": len(payload),
        "public_url": PUBLIC_URL,
        "staged_revision_id": stage_id,
        "active_activation_record_id": activation_id,
        "active_sequence": authority["sequence"],
        "active_authority_revision_id": authority["revision"],
        "active_target": authority["target"],
        "public_object_revision_id": "pobj_" + "3" * 32,
        "previous_activation_record_id": (
            previous["activation"] if previous is not None else None
        ),
        "previous_sequence": previous["sequence"] if previous is not None else None,
        "previous_authority_revision_id": (
            previous["revision"] if previous is not None else None
        ),
        "previous_index_sha256": (
            previous["index_sha256"] if previous is not None else None
        ),
        "previous_target": previous["target"] if previous is not None else None,
        "readback": proof,
    }


def test_pointer_requires_server_active_and_readback_proof_before_receipt() -> None:
    payload = _index_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    release_id = "release-stable-" + "a" * 24
    observed: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        observed.append(f"{request.method} {request.url.host}{request.url.path}")
        if request.method == "PUT":
            assert request.headers["authorization"] == "Bearer protected-publication-token"
            assert request.headers["x-ecorex-sha256"] == digest
            assert request.content == payload
            return httpx.Response(
                201,
                headers={"Content-Type": "application/json"},
                json=_stage_response(payload),
            )
        assert request.method == "POST"
        assert json.loads(request.content) == {
            "expected_previous_activation_record_id": None,
            "expected_previous_sequence": None,
            "expected_previous_authority_revision_id": None,
            "expected_previous_index_sha256": None,
            "expected_previous_target": None,
            "index_sha256": digest,
            "revision_id": "bstage_" + "1" * 32,
        }
        return httpx.Response(
            200,
            headers={"Content-Type": "application/json"},
            json=_active_response(payload),
        )

    receipt = _publisher(handler).publish(payload)

    assert receipt.index_sha256 == digest
    assert receipt.active_activation_record_id == "bactive_" + "2" * 32
    assert receipt.previous_activation_record_id is None
    assert receipt.readback_proof_token.startswith(
        "bootstrap-index-proof:bread_"
    )
    assert observed == [
        f"PUT control.example/api/v1/bootstrap-index/candidates/{release_id}",
        f"POST control.example/api/v1/bootstrap-index/candidates/{release_id}/activate",
    ]


@pytest.mark.parametrize("mutation", ["missing-proof", "wrong-proof-digest"])
def test_pointer_refuses_activation_without_valid_server_readback_proof(
    mutation: str,
) -> None:
    payload = _index_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "PUT":
            return httpx.Response(
                201,
                headers={"Content-Type": "application/json"},
                json=_stage_response(payload),
            )
        value = _active_response(payload)
        if mutation == "missing-proof":
            value["state"] = "active"
        else:
            value["readback"]["index_sha256"] = "f" * 64
        return httpx.Response(
            200, headers={"Content-Type": "application/json"}, json=value
        )

    with pytest.raises(
        PublicBootstrapPublicationError,
        match="bootstrap_index_activation_receipt_invalid",
    ):
        _publisher(handler).publish(payload)


def test_pointer_refuses_noncanonical_index_before_remote_mutation() -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500)

    payload = _index_bytes()
    noncanonical = json.dumps(json.loads(payload), indent=2).encode("utf-8")
    with pytest.raises(PublicBootstrapPublicationError, match="not_canonical"):
        _publisher(handler).publish(noncanonical)
    assert calls == 0


def test_pointer_refuses_unverified_authority_before_remote_mutation() -> None:
    calls = 0

    class RejectingVerifier:
        def verify(self, _payload, _signature) -> bool:
            raise ValueError("forged authority")

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500)

    with pytest.raises(
        PublicBootstrapPublicationError,
        match="bootstrap_index_bytes_invalid",
    ):
        _publisher(handler, verifier=RejectingVerifier()).publish(_index_bytes())
    assert calls == 0


def test_pointer_cas_binds_the_complete_previous_public_authority() -> None:
    payload = _index_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    index = json.loads(payload)
    previous = {
        "activation": "bactive_" + "9" * 32,
        "sequence": 1,
        "revision": index["authority"]["revision"],
        "index_sha256": "f" * 64,
        "target": index["authority"]["target"],
    }
    stage_id = "bstage_" + "8" * 32

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "PUT":
            return httpx.Response(
                201,
                headers={"Content-Type": "application/json"},
                json=_stage_response(
                    payload, previous=previous, stage_id=stage_id
                ),
            )
        assert json.loads(request.content) == {
            "expected_previous_activation_record_id": previous["activation"],
            "expected_previous_sequence": previous["sequence"],
            "expected_previous_authority_revision_id": previous["revision"],
            "expected_previous_index_sha256": previous["index_sha256"],
            "expected_previous_target": previous["target"],
            "index_sha256": digest,
            "revision_id": stage_id,
        }
        return httpx.Response(
            409,
            headers={"Content-Type": "application/json"},
            json={"error": "compare_and_swap_failed"},
        )

    with pytest.raises(
        PublicBootstrapPublicationError,
        match="bootstrap_index_publication_rejected",
    ):
        _publisher(handler).publish(payload)
