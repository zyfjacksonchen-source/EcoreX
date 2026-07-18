from __future__ import annotations

import base64
import asyncio
import copy
import hashlib
import json
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import pytest

from ecorex.release import (
    ArtifactBuildInput,
    ArtifactKind,
    DirectReleaseAdmissionError,
    DirectReleaseAdmissionPolicy,
    Ed25519MemorySigner,
    ReleaseBuildSpec,
    ReleaseBuilder,
    build_direct_release_waiver,
    build_unsigned_direct_admission,
    candidate_receipt_signing_payload,
    sign_direct_admission,
    validate_signed_direct_admission,
)
from ecorex.update import (
    Ed25519SignatureVerifier,
    ReleaseChannel,
    ReleaseSource,
    SignatureEnvelope,
    SourceKind,
)
from ecorex.control_plane import (
    ControlPlaneConflict,
    ControlPlaneRepository,
    ControlPlaneSchemaManager,
    ControlPrincipal,
    ReleaseGateError,
    required_release_gates,
    required_publication_gates,
)
from ecorex.control_plane.app import (
    _DirectAdmissionBodyLimitMiddleware,
    _MAX_DIRECT_ADMISSION_REQUEST_BYTES,
)
from ecorex.control_plane.cli import run as release_cli_run


COMMIT = "a" * 40
INSTRUCTION = hashlib.sha256(b"direct production instruction").hexdigest()


def _canonical(value: dict) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _fixture(tmp_path: Path):
    source = tmp_path / "bootstrap"
    source.mkdir()
    (source / "ecorex-bootstrap.exe").write_bytes(b"bootstrap")
    release_signer = Ed25519MemorySigner(
        "release-test", Ed25519PrivateKey.generate()
    )
    publication_signer = Ed25519MemorySigner(
        "publication-test", Ed25519PrivateKey.generate()
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
                    "https://mirror.example/releases/release",
                ),
                ReleaseSource(
                    "github",
                    SourceKind.GITHUB_RELEASE,
                    1,
                    "https://github.com/example/releases/download/release",
                ),
                ReleaseSource(
                    "cdn",
                    SourceKind.ECOREX_CDN,
                    2,
                    "https://cdn.example/releases/release",
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
    manifest_bytes = built.manifest_path.read_bytes()
    candidate = {
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
            "key_id": release_signer.key_id,
            "operation_count": 2,
            "executable_sha256": "f" * 64,
            "adapter_sha256": "1" * 64,
        },
    }
    raw_signature = release_signer.sign(candidate_receipt_signing_payload(candidate))
    candidate["signature"] = SignatureEnvelope(
        "ed25519",
        release_signer.key_id,
        base64.b64encode(raw_signature).decode(),
    ).to_dict()
    candidate_bytes = _canonical(candidate) + b"\n"
    waiver = build_direct_release_waiver(
        manifest=built.manifest,
        manifest_bytes=manifest_bytes,
        candidate_receipt=candidate,
        candidate_receipt_bytes=candidate_bytes,
        commit_sha=COMMIT,
        operator_instruction_sha256=INSTRUCTION,
        signer=release_signer,
        signer_public_key=release_signer.public_key_bytes,
        publication_key_id=publication_signer.key_id,
        publication_public_key=publication_signer.public_key_bytes,
        created_at="2026-07-16T12:30:00Z",
    )
    waiver_bytes = _canonical(waiver) + b"\n"
    expected_names = {
        "release-manifest.json": (
            len(manifest_bytes),
            hashlib.sha256(manifest_bytes).hexdigest(),
        ),
        "release-metadata.json": (10, "2" * 64),
        "sbom.cdx.json": (20, "3" * 64),
    }
    expected_names.update(
        {
            artifact.file_name: (artifact.size_bytes, artifact.sha256)
            for artifact in built.manifest.artifacts
        }
    )
    publication = {
        "schema_version": 1,
        "release_id": built.manifest.release_id,
        "version": built.manifest.version,
        "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "github_release_id": 42,
        "github_draft": False,
        "source_receipts": {
            source.source_id: [
                {
                    "name": name,
                    "size_bytes": identity[0],
                    "sha256": identity[1],
                    "url": f"{source.base_url}/{name}",
                }
                for name, identity in sorted(expected_names.items())
            ]
            for source in built.manifest.sources
        },
    }
    publication_bytes = _canonical(publication)
    policy = DirectReleaseAdmissionPolicy(
        enabled=True,
        release_id=built.manifest.release_id,
        operator_instruction_sha256=INSTRUCTION,
        release_public_keys={release_signer.key_id: release_signer.public_key_bytes},
        publication_public_keys={
            publication_signer.key_id: publication_signer.public_key_bytes
        },
    )
    return (
        built,
        release_signer,
        publication_signer,
        manifest_bytes,
        candidate_bytes,
        waiver_bytes,
        publication_bytes,
        policy,
    )


def test_direct_bundle_records_live_gates_only_as_waived(tmp_path: Path) -> None:
    (
        built,
        release_signer,
        publication_signer,
        manifest_bytes,
        candidate_bytes,
        waiver_bytes,
        publication_bytes,
        policy,
    ) = _fixture(tmp_path)
    publication_sha = hashlib.sha256(publication_bytes).hexdigest()
    waiver_sha = hashlib.sha256(waiver_bytes).hexdigest()
    gates = {
        "unit": {"status": "passed", "evidence": "gate-receipt:sha256:" + "4" * 64},
        "mirror-sync": {
            "status": "passed",
            "evidence": f"publication-receipt:sha256:{publication_sha}",
        },
        **{
            gate: {
                "status": "waived",
                "evidence": f"operator-waiver:sha256:{waiver_sha}",
            }
            for gate in ("live-model", "live-image", "cdp-acceptance")
        },
    }
    unsigned = build_unsigned_direct_admission(
        phase="prepare",
        manifest=built.manifest,
        manifest_bytes=manifest_bytes,
        commit_sha=COMMIT,
        operator_instruction_sha256=INSTRUCTION,
        candidate_receipt_bytes=candidate_bytes,
        operator_waiver_bytes=waiver_bytes,
        publication_receipt_bytes=publication_bytes,
        publication_key_id=publication_signer.key_id,
        gates=gates,
    )
    signed = sign_direct_admission(
        unsigned, signer=release_signer, manifest=built.manifest
    )
    result = validate_signed_direct_admission(
        signed,
        manifest=built.manifest,
        expected_manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
        expected_gates=frozenset(gates),
        expected_phase="prepare",
        policy=policy,
        release_verifier=Ed25519SignatureVerifier(
            {release_signer.key_id: release_signer.public_key_bytes}
        ),
    )
    assert {
        gate: result.gates[gate]["status"]
        for gate in ("live-model", "live-image", "cdp-acceptance")
    } == {
        "live-model": "waived",
        "live-image": "waived",
        "cdp-acceptance": "waived",
    }

    class BrokenVerifier:
        def verify(self, _payload, _signature):
            raise RuntimeError("secret backend detail")

    with pytest.raises(
        DirectReleaseAdmissionError, match="admission_signature_invalid"
    ) as failure:
        validate_signed_direct_admission(
            signed,
            manifest=built.manifest,
            expected_manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
            expected_gates=frozenset(gates),
            expected_phase="prepare",
            policy=policy,
            release_verifier=BrokenVerifier(),
        )
    assert "secret backend detail" not in str(failure.value)

    forged = copy.deepcopy(signed)
    forged["gates"]["live-model"]["status"] = "passed"
    forged = sign_direct_admission(
        {key: value for key, value in forged.items() if key != "signature"},
        signer=release_signer,
        manifest=built.manifest,
    )
    with pytest.raises(
        DirectReleaseAdmissionError, match="live_acceptance_false_pass"
    ):
        validate_signed_direct_admission(
            forged,
            manifest=built.manifest,
            expected_manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
            expected_gates=frozenset(gates),
            expected_phase="prepare",
            policy=policy,
            release_verifier=Ed25519SignatureVerifier(
                {release_signer.key_id: release_signer.public_key_bytes}
            ),
        )


def test_direct_policy_is_disabled_and_single_release_scoped(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    built = fixture[0]
    with pytest.raises(DirectReleaseAdmissionError, match="disabled_configuration"):
        DirectReleaseAdmissionPolicy(release_id=built.manifest.release_id)
    with pytest.raises(DirectReleaseAdmissionError, match="configuration_invalid"):
        DirectReleaseAdmissionPolicy(
            enabled=True,
            release_id=built.manifest.release_id,
            operator_instruction_sha256=INSTRUCTION,
            release_public_keys={"same": b"a" * 32},
            publication_public_keys={"same": b"b" * 32},
        )
    release_ring = {"release": b"r" * 32}
    publication_ring = {"publication": b"p" * 32}
    policy = DirectReleaseAdmissionPolicy(
        enabled=True,
        release_id=built.manifest.release_id,
        operator_instruction_sha256=INSTRUCTION,
        release_public_keys=release_ring,
        publication_public_keys=publication_ring,
    )
    release_ring["release"] = b"x" * 32
    publication_ring["publication"] = b"y" * 32
    assert policy.release_public_keys == {"release": b"r" * 32}
    assert policy.publication_public_keys == {"publication": b"p" * 32}
    with pytest.raises(TypeError):
        policy.release_public_keys["another"] = b"z" * 32


def test_control_plane_projects_waived_without_mutating_passed_gate_schema(
    tmp_path: Path,
) -> None:
    (
        built,
        release_signer,
        publication_signer,
        manifest_bytes,
        candidate_bytes,
        waiver_bytes,
        publication_bytes,
        policy,
    ) = _fixture(tmp_path)
    required = required_release_gates(ReleaseChannel.STABLE) - {"bootstrap-index"}
    publication_sha = hashlib.sha256(publication_bytes).hexdigest()
    waiver_sha = hashlib.sha256(waiver_bytes).hexdigest()
    gates: dict[str, dict[str, str]] = {}
    for gate in required:
        if gate in {"live-model", "live-image", "cdp-acceptance"}:
            gates[gate] = {
                "status": "waived",
                "evidence": f"operator-waiver:sha256:{waiver_sha}",
            }
        elif gate in required_publication_gates(ReleaseChannel.STABLE):
            gates[gate] = {
                "status": "passed",
                "evidence": f"publication-receipt:sha256:{publication_sha}",
            }
        else:
            gates[gate] = {
                "status": "passed",
                "evidence": "gate-receipt:sha256:"
                + hashlib.sha256(gate.encode()).hexdigest(),
            }
    unsigned = build_unsigned_direct_admission(
        phase="prepare",
        manifest=built.manifest,
        manifest_bytes=manifest_bytes,
        commit_sha=COMMIT,
        operator_instruction_sha256=INSTRUCTION,
        candidate_receipt_bytes=candidate_bytes,
        operator_waiver_bytes=waiver_bytes,
        publication_receipt_bytes=publication_bytes,
        publication_key_id=publication_signer.key_id,
        gates=gates,
    )
    admission = sign_direct_admission(
        unsigned, signer=release_signer, manifest=built.manifest
    )
    database = tmp_path / "control.db"
    ControlPlaneSchemaManager(database).migrate()
    repository = ControlPlaneRepository(
        database,
        verifier=Ed25519SignatureVerifier(
            {release_signer.key_id: release_signer.public_key_bytes}
        ),
        direct_release_policy=policy,
    )
    actor = ControlPrincipal(
        subject="operator", client_id="admin", account_id="account"
    )
    repository.create_candidate(
        built.manifest,
        manifest_file_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
        actor=actor,
        client_request_id="candidate",
    )
    candidate = repository.record_direct_admission(
        built.manifest.release_id,
        admission,
        actor=actor,
        client_request_id="direct-prepare",
    )
    assert candidate.missing_gates == ["bootstrap-index"]
    assert {
        gate: candidate.gates[gate]
        for gate in ("live-model", "live-image", "cdp-acceptance")
    } == {
        "live-model": "waived",
        "live-image": "waived",
        "cdp-acceptance": "waived",
    }
    with repository._read_transaction() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM control_release_gates "
            "WHERE gate_name IN ('live-model','live-image','cdp-acceptance')"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM direct_release_gate_waivers "
            "WHERE status='waived'"
        ).fetchone()[0] == 3
    with pytest.raises(ReleaseGateError, match="non-waived required"):
        repository.publish(
            built.manifest.release_id,
            actor=actor,
            client_request_id="publish-too-early",
        )
    with pytest.raises(ControlPlaneConflict, match="cannot be mixed"):
        repository.record_gate_bundle(
            built.manifest.release_id,
            {},
            actor=actor,
            client_request_id="mixed",
        )
    repository._require_bootstrap_index_proof = lambda *_args, **_kwargs: None
    repository._require_direct_publication_bootstrap_binding = (
        lambda *_args, **_kwargs: None
    )
    final_gates = dict(gates)
    final_gates["bootstrap-index"] = {
        "status": "passed",
        "evidence": "bootstrap-index-proof:bread_"
        + "9" * 32
        + ":sha256:"
        + "8" * 64,
    }
    final_unsigned = build_unsigned_direct_admission(
        phase="finalize",
        manifest=built.manifest,
        manifest_bytes=manifest_bytes,
        commit_sha=COMMIT,
        operator_instruction_sha256=INSTRUCTION,
        candidate_receipt_bytes=candidate_bytes,
        operator_waiver_bytes=waiver_bytes,
        publication_receipt_bytes=publication_bytes,
        publication_key_id=publication_signer.key_id,
        gates=final_gates,
    )
    final_admission = sign_direct_admission(
        final_unsigned, signer=release_signer, manifest=built.manifest
    )
    finalized = repository.record_direct_admission(
        built.manifest.release_id,
        final_admission,
        actor=actor,
        client_request_id="direct-finalize",
    )
    replayed = repository.record_direct_admission(
        built.manifest.release_id,
        final_admission,
        actor=actor,
        client_request_id="direct-finalize",
    )
    assert finalized == replayed
    assert finalized.missing_gates == []
    prepare_sha = hashlib.sha256(_canonical(admission)).hexdigest()
    with repository._read_transaction() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM direct_release_admissions WHERE release_id=?",
            (built.manifest.release_id,),
        ).fetchone()[0] == 2
        assert {
            row[0]
            for row in connection.execute(
                "SELECT attestation_sha256 FROM direct_release_gate_waivers "
                "WHERE release_id=?",
                (built.manifest.release_id,),
            )
        } == {prepare_sha}
    published = repository.publish(
        built.manifest.release_id,
        actor=actor,
        client_request_id="publish-final",
    )
    assert published.status == "published"


def test_release_cli_dry_run_consumes_direct_prepare_bundle(
    tmp_path: Path, capsys
) -> None:
    (
        built,
        release_signer,
        publication_signer,
        manifest_bytes,
        candidate_bytes,
        waiver_bytes,
        publication_bytes,
        _policy,
    ) = _fixture(tmp_path)
    required = required_release_gates(ReleaseChannel.STABLE) - {"bootstrap-index"}
    publication_sha = hashlib.sha256(publication_bytes).hexdigest()
    waiver_sha = hashlib.sha256(waiver_bytes).hexdigest()
    gates: dict[str, dict[str, str]] = {}
    for gate in required:
        if gate in {"live-model", "live-image", "cdp-acceptance"}:
            gates[gate] = {
                "status": "waived",
                "evidence": f"operator-waiver:sha256:{waiver_sha}",
            }
        elif gate in required_publication_gates(ReleaseChannel.STABLE):
            gates[gate] = {
                "status": "passed",
                "evidence": f"publication-receipt:sha256:{publication_sha}",
            }
        else:
            gates[gate] = {
                "status": "passed",
                "evidence": "gate-receipt:sha256:"
                + hashlib.sha256(gate.encode()).hexdigest(),
            }
    admission = sign_direct_admission(
        build_unsigned_direct_admission(
            phase="prepare",
            manifest=built.manifest,
            manifest_bytes=manifest_bytes,
            commit_sha=COMMIT,
            operator_instruction_sha256=INSTRUCTION,
            candidate_receipt_bytes=candidate_bytes,
            operator_waiver_bytes=waiver_bytes,
            publication_receipt_bytes=publication_bytes,
            publication_key_id=publication_signer.key_id,
            gates=gates,
        ),
        signer=release_signer,
        manifest=built.manifest,
    )
    evidence_path = tmp_path / "direct-prepare.json"
    evidence_path.write_bytes(_canonical(admission))
    publication_path = tmp_path / "publication.json"
    publication_path.write_bytes(publication_bytes)
    release_key = release_signer.key_id + "=" + base64.b64encode(
        release_signer.public_key_bytes
    ).decode()
    publication_key = publication_signer.key_id + "=" + base64.b64encode(
        publication_signer.public_key_bytes
    ).decode()
    result = release_cli_run(
        [
            "promote",
            "--direct-admission",
            "--phase",
            "prepare",
            "--manifest",
            str(built.manifest_path),
            "--evidence",
            str(evidence_path),
            "--publication-receipt",
            str(publication_path),
            "--trusted-key",
            release_key,
            "--trusted-publication-key",
            publication_key,
            "--operator-instruction-sha256",
            INSTRUCTION,
            "--journal",
            str(tmp_path / "promotion.json"),
            "--dry-run",
        ]
    )
    assert result == 0
    output = json.loads(capsys.readouterr().out)
    assert output["direct_admission"] is True
    assert output["phase"] == "prepare"
    assert output["gate_count"] == len(required)


def test_direct_admission_body_is_bounded_before_json_parsing() -> None:
    called = False
    sent: list[dict] = []

    async def inner(_scope, _receive, _send):
        nonlocal called
        called = True

    async def receive():
        raise AssertionError("oversized declared body must not be consumed")

    async def send(message):
        sent.append(message)

    class Authenticator:
        def authenticate(self, token):
            assert token == "x" * 24
            return ControlPrincipal(
                subject="operator",
                client_id="admin",
                account_id="account",
                roles=frozenset({"release_admin"}),
            )

    scope = {
        "type": "http",
        "method": "PUT",
        "path": "/api/v1/admin/releases/release-stable-"
        + "a" * 24
        + "/direct-admission",
        "headers": [
            (b"authorization", b"Bearer " + b"x" * 24),
            (
                b"content-length",
                str(_MAX_DIRECT_ADMISSION_REQUEST_BYTES + 1).encode(),
            )
        ],
    }
    asyncio.run(
        _DirectAdmissionBodyLimitMiddleware(
            inner, authenticator=Authenticator()
        )(scope, receive, send)
    )
    assert called is False
    assert sent[0]["status"] == 413


def test_nginx_32m_allowance_is_scoped_to_release_admin_routes() -> None:
    route = (
        Path(__file__).resolve().parents[2]
        / "deploy"
        / "ecorex-cloud-sidecar"
        / "nginx"
        / "ecorex-cloud.routes.conf"
    ).read_text(encoding="utf-8")
    direct_location = (
        'location ~ "^/api/v1/admin/releases/'
        '[A-Za-z0-9][A-Za-z0-9._-]{0,127}/direct-admission$" {'
    )
    release_block = route.split(direct_location, 1)[1].split("\n}", 1)[0]
    general_block = route.split("location /api/v1/admin/ {", 1)[1].split(
        "\n}", 1
    )[0]
    assert "client_max_body_size 32m;" in release_block
    assert "limit_except PUT { deny all; }" in release_block
    assert "auth_request /_ecorex_release_admin_auth;" in release_block
    assert "client_body_timeout 10s;" in release_block
    assert "client_body_buffer_size 64k;" in release_block
    assert "proxy_request_buffering on;" in release_block
    assert "proxy_request_buffering off;" not in release_block
    assert "client_max_body_size 32m;" not in general_block
    assert route.count("client_max_body_size 32m;") == 1
    auth_block = route.split(
        "location = /_ecorex_release_admin_auth {", 1
    )[1].split("\n}", 1)[0]
    assert "internal;" in auth_block
    assert "proxy_method GET;" in auth_block
    assert "proxy_pass_request_body off;" in auth_block
    assert 'proxy_set_header Content-Length "";' in auth_block
    assert "proxy_set_header Authorization $http_authorization;" in auth_block


def test_direct_admission_auth_and_busy_rejections_never_read_body() -> None:
    class Authenticator:
        def authenticate(self, token):
            if token == "valid-" + "x" * 24:
                return ControlPrincipal(
                    subject="operator",
                    client_id="admin",
                    account_id="account",
                    roles=frozenset({"release_admin"}),
                )
            if token == "user-" + "x" * 24:
                return ControlPrincipal(
                    subject="user",
                    client_id="user-client",
                    account_id="user-account",
                )
            raise PermissionError("secret provider detail")

    path = (
        "/api/v1/admin/releases/release-stable-"
        + "a" * 24
        + "/direct-admission"
    )

    async def exercise():
        entered = asyncio.Event()
        release = asyncio.Event()
        first_sent: list[dict] = []
        rejected: list[dict] = []
        unauthorized: list[dict] = []
        forbidden: list[dict] = []
        unauthorized_reads = 0
        forbidden_reads = 0
        busy_reads = 0

        async def inner(_scope, receive, _send):
            await receive()
            entered.set()
            await release.wait()

        middleware = _DirectAdmissionBodyLimitMiddleware(
            inner, authenticator=Authenticator()
        )
        valid_headers = [
            (b"authorization", b"Bearer valid-" + b"x" * 24),
            (b"content-length", b"2"),
        ]

        async def first_receive():
            return {"type": "http.request", "body": b"{}", "more_body": False}

        async def first_send(message):
            first_sent.append(message)

        first = asyncio.create_task(
            middleware(
                {
                    "type": "http",
                    "method": "PUT",
                    "path": path,
                    "headers": valid_headers,
                },
                first_receive,
                first_send,
            )
        )
        await entered.wait()

        async def busy_receive():
            nonlocal busy_reads
            busy_reads += 1
            raise AssertionError("busy request body must not be read")

        async def busy_send(message):
            rejected.append(message)

        await middleware(
            {
                "type": "http",
                "method": "PUT",
                "path": path,
                "headers": valid_headers,
            },
            busy_receive,
            busy_send,
        )

        async def unauthorized_receive():
            nonlocal unauthorized_reads
            unauthorized_reads += 1
            raise AssertionError("unauthorized request body must not be read")

        async def unauthorized_send(message):
            unauthorized.append(message)

        await middleware(
            {
                "type": "http",
                "method": "PUT",
                "path": path,
                "headers": [
                    (b"authorization", b"Bearer invalid-" + b"x" * 24),
                    (b"content-length", str(32 * 1024 * 1024).encode()),
                ],
            },
            unauthorized_receive,
            unauthorized_send,
        )

        async def forbidden_receive():
            nonlocal forbidden_reads
            forbidden_reads += 1
            raise AssertionError("non-admin request body must not be read")

        async def forbidden_send(message):
            forbidden.append(message)

        await middleware(
            {
                "type": "http",
                "method": "PUT",
                "path": path,
                "headers": [
                    (b"authorization", b"Bearer user-" + b"x" * 24),
                    (b"content-length", str(32 * 1024 * 1024).encode()),
                ],
            },
            forbidden_receive,
            forbidden_send,
        )
        release.set()
        await first
        return (
            rejected,
            unauthorized,
            forbidden,
            busy_reads,
            unauthorized_reads,
            forbidden_reads,
        )

    (
        rejected,
        unauthorized,
        forbidden,
        busy_reads,
        unauthorized_reads,
        forbidden_reads,
    ) = asyncio.run(exercise())
    assert rejected[0]["status"] == 429
    assert unauthorized[0]["status"] == 401
    assert forbidden[0]["status"] == 403
    assert busy_reads == 0
    assert unauthorized_reads == 0
    assert forbidden_reads == 0
    assert b"secret provider detail" not in b"".join(
        message.get("body", b"") for message in unauthorized
    )


def test_direct_admission_middleware_does_not_capture_neighbor_routes() -> None:
    called = False

    class RejectAll:
        def authenticate(self, _token):
            raise AssertionError("neighbor route must bypass direct authentication")

    async def exercise():
        nonlocal called

        async def inner(_scope, _receive, _send):
            nonlocal called
            called = True

        async def receive():
            raise AssertionError("neighbor test app does not consume a body")

        async def send(_message):
            return None

        await _DirectAdmissionBodyLimitMiddleware(
            inner, authenticator=RejectAll()
        )(
            {
                "type": "http",
                "method": "PUT",
                "path": "/api/v1/admin/releases/release-stable-"
                + "a" * 24
                + "/gate-bundle",
                "headers": [],
            },
            receive,
            send,
        )

    asyncio.run(exercise())
    assert called is True
