from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import pytest

from ecorex.control_plane import (
    ControlPlaneConflict,
    ControlPlaneRepository,
    ControlPrincipal,
    ReleaseGateError,
    migrate_control_plane_database,
    required_release_gates,
)
from ecorex.release import (
    Ed25519MemorySigner,
    GateAttestationError,
    build_unsigned_gate_bundle,
    sign_gate_bundle,
    validate_signed_gate_bundle,
)
from ecorex.update import (
    Ed25519SignatureVerifier,
    ReleaseArtifact,
    ReleaseChannel,
    ReleaseManifest,
    ReleaseSource,
    SignatureEnvelope,
    SourceKind,
)


COMMIT = "a" * 40
RUN_ID = 8817


class AcceptingVerifier:
    def verify(self, payload, signature) -> bool:
        assert payload and signature.key_id == "release-key"
        return True


def _signature(key_id: str = "release-key") -> SignatureEnvelope:
    return SignatureEnvelope(
        algorithm="ed25519",
        key_id=key_id,
        value=base64.b64encode(b"s" * 64).decode("ascii"),
    )


def _manifest(
    channel: ReleaseChannel = ReleaseChannel.CANARY,
    *,
    key_id: str = "release-key",
) -> ReleaseManifest:
    payload = b"core"
    return ReleaseManifest(
        schema_version=1,
        release_id=f"release-{channel.value}-" + "b" * 24,
        version="1.0.0",
        build_digest=hashlib.sha256(b"build").hexdigest(),
        channel=channel,
        created_at="2026-07-14T00:00:00+00:00",
        sources=(
            ReleaseSource(
                "mirror",
                SourceKind.GITHUB_CN_MIRROR,
                0,
                "https://mirror.example/releases",
            ),
            ReleaseSource(
                "github",
                SourceKind.GITHUB_RELEASE,
                1,
                "https://github.example/releases",
            ),
            ReleaseSource(
                "cdn",
                SourceKind.ECOREX_CDN,
                2,
                "https://cdn.example/releases",
            ),
        ),
        artifacts=(
            ReleaseArtifact(
                artifact_id="core-windows-x64",
                platform="windows",
                architecture="x64",
                file_name="core.zip",
                size_bytes=len(payload),
                sha256=hashlib.sha256(payload).hexdigest(),
                signature=_signature(key_id),
            ),
        ),
        signature=_signature(key_id),
    )


def _gates(names: frozenset[str]) -> dict[str, dict[str, str]]:
    publication = "publication-receipt:sha256:" + "c" * 64
    result: dict[str, dict[str, str]] = {}
    for name in names:
        if name in {"github-release", "mirror-sync", "cdn-sync"}:
            evidence = publication
        elif name == "bootstrap-index":
            evidence = (
                "bootstrap-index-proof:bread_"
                + "d" * 32
                + ":sha256:"
                + "e" * 64
            )
        else:
            evidence = "gate-receipt:sha256:" + hashlib.sha256(name.encode()).hexdigest()
        result[name] = {"status": "passed", "evidence": evidence}
    return result


def _manifest_file_sha256(manifest: ReleaseManifest) -> str:
    return hashlib.sha256(manifest.to_json().encode()).hexdigest()


def _fake_bundle(manifest: ReleaseManifest, *, phase: str = "finalize") -> dict:
    names = required_release_gates(manifest.channel)
    if manifest.channel is ReleaseChannel.STABLE and phase == "prepare":
        names -= {"bootstrap-index"}
    unsigned = build_unsigned_gate_bundle(
        phase=phase,
        commit_sha=COMMIT,
        workflow_run_id=RUN_ID,
        manifest=manifest,
        manifest_sha256=hashlib.sha256(manifest.to_json().encode()).hexdigest(),
        gates=_gates(names),
    )
    return {**unsigned, "signature": _signature().to_dict()}


def test_gate_bundle_is_domain_signed_and_fails_closed_on_tamper() -> None:
    private = Ed25519PrivateKey.generate()
    signer = Ed25519MemorySigner("release-key", private)
    verifier = Ed25519SignatureVerifier(
        {"release-key": signer.public_key_bytes}
    )
    manifest = _manifest()
    names = frozenset({"unit", "github-release", "mirror-sync", "cdn-sync"})
    unsigned = build_unsigned_gate_bundle(
        phase="finalize",
        commit_sha=COMMIT,
        workflow_run_id=RUN_ID,
        manifest=manifest,
        manifest_sha256="f" * 64,
        gates=_gates(names),
    )
    signed = sign_gate_bundle(unsigned, signer=signer, manifest=manifest)
    assert set(
        validate_signed_gate_bundle(
            signed,
            manifest=manifest,
            expected_gates=names,
            expected_phase="finalize",
            verifier=verifier,
            expected_manifest_sha256="f" * 64,
        )
    ) == names

    signed["gates"]["unit"]["evidence"] = "gate-receipt:sha256:" + "0" * 64
    with pytest.raises(GateAttestationError, match="signature"):
        validate_signed_gate_bundle(
            signed,
            manifest=manifest,
            expected_gates=names,
            expected_phase="finalize",
            verifier=verifier,
            expected_manifest_sha256="f" * 64,
        )


def test_gate_bundle_rejects_wrong_candidate_phase_gate_set_and_key() -> None:
    trusted = Ed25519MemorySigner("release-key", Ed25519PrivateKey.generate())
    impostor = Ed25519MemorySigner("release-key", Ed25519PrivateKey.generate())
    verifier = Ed25519SignatureVerifier(
        {"release-key": trusted.public_key_bytes}
    )
    manifest = _manifest()
    expected = required_release_gates(manifest.channel)
    incomplete = expected - {"live-image"}
    unsigned = build_unsigned_gate_bundle(
        phase="finalize",
        commit_sha=COMMIT,
        workflow_run_id=RUN_ID,
        manifest=manifest,
        manifest_sha256="f" * 64,
        gates=_gates(incomplete),
    )
    signed = sign_gate_bundle(unsigned, signer=trusted, manifest=manifest)
    with pytest.raises(GateAttestationError, match="gate_set"):
        validate_signed_gate_bundle(
            signed,
            manifest=manifest,
            expected_gates=expected,
            expected_phase="finalize",
            verifier=verifier,
            expected_manifest_sha256="f" * 64,
        )
    with pytest.raises(GateAttestationError, match="identity"):
        validate_signed_gate_bundle(
            signed,
            manifest=_manifest(ReleaseChannel.STABLE),
            expected_gates=expected,
            expected_phase="finalize",
            verifier=verifier,
            expected_manifest_sha256="f" * 64,
        )
    with pytest.raises(GateAttestationError, match="identity"):
        validate_signed_gate_bundle(
            signed,
            manifest=manifest,
            expected_gates=incomplete,
            expected_phase="prepare",
            verifier=verifier,
            expected_manifest_sha256="f" * 64,
        )
    impostor_signed = sign_gate_bundle(
        unsigned,
        signer=impostor,
        manifest=manifest,
    )
    with pytest.raises(GateAttestationError, match="signature"):
        validate_signed_gate_bundle(
            impostor_signed,
            manifest=manifest,
            expected_gates=incomplete,
            expected_phase="finalize",
            verifier=verifier,
            expected_manifest_sha256="f" * 64,
        )


def test_gate_bundle_signing_script_pins_external_signer_and_revalidates(
    tmp_path,
) -> None:
    private = Ed25519PrivateKey.generate()
    private_raw = private.private_bytes(
        serialization.Encoding.Raw,
        serialization.PrivateFormat.Raw,
        serialization.NoEncryption(),
    )
    public = private.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    adapter = tmp_path / "signer_adapter.py"
    site_packages = next(path for path in sys.path if path.endswith("site-packages"))
    adapter.write_text(
        f"""\
import base64
import os
import sys
sys.path.insert(0, {site_packages!r})
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

payload = sys.stdin.buffer.read()
seed = base64.b64decode(os.environ["ACTIONS_ID_TOKEN_REQUEST_TOKEN"], validate=True)
signature = Ed25519PrivateKey.from_private_bytes(seed).sign(payload)
sys.stdout.write(base64.b64encode(signature).decode("ascii") + "\\n")
""",
        encoding="utf-8",
        newline="\n",
    )
    manifest = _manifest()
    manifest_path = tmp_path / "release-manifest.json"
    manifest_path.write_text(manifest.to_json(), encoding="utf-8")
    expected = required_release_gates(manifest.channel)
    unsigned = build_unsigned_gate_bundle(
        phase="finalize",
        commit_sha=COMMIT,
        workflow_run_id=RUN_ID,
        manifest=manifest,
        manifest_sha256=hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        gates=_gates(expected),
    )
    unsigned_path = tmp_path / "unsigned.json"
    unsigned_path.write_text(json.dumps(unsigned), encoding="utf-8")
    output = tmp_path / "signed.json"
    executable = Path(sys.executable).resolve()
    environment = {
        **os.environ,
        "ECOREX_RELEASE_SIGNER_EXECUTABLE": str(executable),
        "ECOREX_RELEASE_SIGNER_EXECUTABLE_SHA256": hashlib.sha256(
            executable.read_bytes()
        ).hexdigest(),
        "ECOREX_RELEASE_SIGNER_ADAPTER": str(adapter),
        "ECOREX_RELEASE_SIGNER_ADAPTER_SHA256": hashlib.sha256(
            adapter.read_bytes()
        ).hexdigest(),
        "ECOREX_RELEASE_SIGNER_KEY_ID": "release-key",
        "ECOREX_RELEASE_SIGNER_PUBLIC_KEY": base64.b64encode(public).decode(),
        "ACTIONS_ID_TOKEN_REQUEST_TOKEN": base64.b64encode(private_raw).decode(),
    }
    script = Path(__file__).resolve().parents[2] / "scripts/sign-v1-release-gate-bundle.py"
    command = [
        sys.executable,
        str(script),
        "--unsigned",
        str(unsigned_path),
        "--manifest",
        str(manifest_path),
        "--output",
        str(output),
    ]
    completed = subprocess.run(
        command,
        env=environment,
        capture_output=True,
        check=False,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr.decode(errors="replace")
    signed = json.loads(output.read_text(encoding="utf-8"))
    assert set(
        validate_signed_gate_bundle(
            signed,
            manifest=manifest,
            expected_gates=expected,
            expected_phase="finalize",
            verifier=Ed25519SignatureVerifier({"release-key": public}),
            expected_manifest_sha256=hashlib.sha256(
                manifest_path.read_bytes()
            ).hexdigest(),
        )
    ) == expected

    unsigned["version"] = "1.0.1"
    unsigned_path.write_text(json.dumps(unsigned), encoding="utf-8")
    rejected = subprocess.run(
        [*command[:-1], str(tmp_path / "rejected.json")],
        env=environment,
        capture_output=True,
        check=False,
        timeout=30,
    )
    assert rejected.returncode == 1
    assert b"release_gate_bundle_invalid" in rejected.stderr


def test_repository_binds_each_phase_once_and_requires_final_attestation(
    tmp_path,
) -> None:
    database = tmp_path / "control.db"
    migrate_control_plane_database(database)
    repository = ControlPlaneRepository(database, verifier=AcceptingVerifier())
    actor = ControlPrincipal(
        subject="release-ci",
        client_id="release-ci",
        account_id="ops",
        roles=frozenset({"release_admin"}),
    )
    manifest = _manifest(ReleaseChannel.STABLE)
    repository.create_candidate(
        manifest,
        manifest_file_sha256=_manifest_file_sha256(manifest),
        actor=actor,
        client_request_id="candidate-stable",
    )
    prepare = _fake_bundle(manifest, phase="prepare")
    repository.record_gate_bundle(
        manifest.release_id,
        prepare,
        actor=actor,
        client_request_id="prepare-bundle",
    )
    drifted = {**prepare, "commit_sha": "f" * 40}
    with pytest.raises(ControlPlaneConflict, match="different evidence"):
        repository.record_gate_bundle(
            manifest.release_id,
            drifted,
            actor=actor,
            client_request_id="prepare-bundle-drift",
        )
    # Even a direct database actor cannot turn the prepare-phase gate rows
    # into publish authority without the separately signed final bundle.
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO control_release_gates("
            "release_id,gate_name,status,evidence,updated_at) VALUES(?,?,?,?,?)",
            (
                manifest.release_id,
                "bootstrap-index",
                "passed",
                "bootstrap-index-proof:bread_"
                + "d" * 32
                + ":sha256:"
                + "e" * 64,
                "2026-07-14T00:00:00+00:00",
            ),
        )
    with pytest.raises(ReleaseGateError, match="final signed"):
        repository.publish(
            manifest.release_id,
            actor=actor,
            client_request_id="publish-with-prepare-only",
        )
    with sqlite3.connect(database) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                "DELETE FROM control_release_gate_attestations WHERE release_id=?",
                (manifest.release_id,),
            )
def test_repository_refuses_manual_pass_and_reverifies_final_bundle(tmp_path) -> None:
    database = tmp_path / "control.db"
    migrate_control_plane_database(database)
    repository = ControlPlaneRepository(database, verifier=AcceptingVerifier())
    actor = ControlPrincipal(
        subject="release-ci",
        client_id="release-ci",
        account_id="ops",
        roles=frozenset({"release_admin"}),
    )
    manifest = _manifest()
    repository.create_candidate(
        manifest,
        manifest_file_sha256=_manifest_file_sha256(manifest),
        actor=actor,
        client_request_id="candidate",
    )
    with pytest.raises(ReleaseGateError, match="signed Candidate-bound bundle"):
        repository.record_gate(
            manifest.release_id,
            "unit",
            status="passed",
            evidence="gate-receipt:sha256:" + "1" * 64,
            actor=actor,
            client_request_id="manual-pass",
        )

    wrong_manifest_bytes = _fake_bundle(manifest)
    wrong_manifest_bytes["manifest_sha256"] = "0" * 64
    with pytest.raises(ReleaseGateError, match="untrusted"):
        repository.record_gate_bundle(
            manifest.release_id,
            wrong_manifest_bytes,
            actor=actor,
            client_request_id="wrong-manifest-bytes",
        )

    bundle = _fake_bundle(manifest)
    candidate = repository.record_gate_bundle(
        manifest.release_id,
        bundle,
        actor=actor,
        client_request_id="bundle",
    )
    assert not candidate.missing_gates
    assert set(candidate.gates) == required_release_gates(manifest.channel)
    with pytest.raises(ControlPlaneConflict, match="signed release gates are immutable"):
        repository.record_gate(
            manifest.release_id,
            "unit",
            status="failed",
            evidence="operator override",
            actor=actor,
            client_request_id="manual-fail-after-bundle",
        )
    assert repository.publish(
        manifest.release_id,
        actor=actor,
        client_request_id="publish",
    ).status == "published"

    other_manifest = _manifest()
    other_id = "release-canary-" + "c" * 24
    raw = other_manifest.to_dict()
    raw["release_id"] = other_id
    other_manifest = ReleaseManifest.from_dict(raw)
    repository.create_candidate(
        other_manifest,
        manifest_file_sha256=_manifest_file_sha256(other_manifest),
        actor=actor,
        client_request_id="candidate-other",
    )
    repository.record_gate_bundle(
        other_id,
        _fake_bundle(other_manifest),
        actor=actor,
        client_request_id="bundle-other",
    )
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE control_release_gates SET evidence=? "
            "WHERE release_id=? AND gate_name='unit'",
            ("gate-receipt:sha256:" + "9" * 64, other_id),
        )
    with pytest.raises(ReleaseGateError, match="drifted"):
        repository.publish(
            other_id,
            actor=actor,
            client_request_id="publish-other",
        )
