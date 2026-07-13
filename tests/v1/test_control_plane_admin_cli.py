from __future__ import annotations

import base64
from dataclasses import replace
import hashlib
import json
from urllib.parse import quote

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from ecorex.control_plane import (
    BootstrapFreshnessRunProjection,
    BootstrapFreshnessStatusProjection,
    BootstrapIndexProofProjection,
    CandidateProjection,
    REQUIRED_RELEASE_GATES,
    RolloutProjection,
)
from ecorex.control_plane.cli import run
from ecorex.release import (
    Ed25519MemorySigner,
    build_unsigned_gate_bundle,
    sign_gate_bundle,
)
from ecorex.release.signing import sign_envelope
from ecorex.update import (
    ReleaseArtifact,
    ReleaseChannel,
    ReleaseManifest,
    ReleaseSource,
    SignatureEnvelope,
    SourceKind,
)


_SIGNER = Ed25519MemorySigner("release-key", Ed25519PrivateKey.generate())
_TRUSTED_KEY = "release-key=" + base64.b64encode(
    _SIGNER.public_key_bytes
).decode("ascii")


def manifest() -> ReleaseManifest:
    placeholder = SignatureEnvelope(
        algorithm="ed25519",
        key_id="release-key",
        value=base64.b64encode(b"\0" * 64).decode(),
    )
    payload = b"core package"
    artifact = ReleaseArtifact(
        artifact_id="core-windows-x64",
        platform="windows",
        architecture="x64",
        file_name="ecorex-core.zip",
        size_bytes=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
        signature=placeholder,
    )
    artifact = replace(
        artifact,
        signature=sign_envelope(
            _SIGNER,
            artifact.signed_payload(
                release_id="release-stable-" + "a" * 24,
                version="1.0.0",
                build_digest=hashlib.sha256(b"build").hexdigest(),
            ),
        ),
    )
    release = ReleaseManifest(
        schema_version=1,
        release_id="release-stable-" + "a" * 24,
        version="1.0.0",
        build_digest=hashlib.sha256(b"build").hexdigest(),
        channel=ReleaseChannel.STABLE,
        created_at="2026-07-10T12:00:00+00:00",
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
                "cdn", SourceKind.ECOREX_CDN, 2, "https://cdn.example/releases"
            ),
        ),
        artifacts=(artifact,),
        signature=placeholder,
    )
    return replace(release, signature=sign_envelope(_SIGNER, release.canonical_payload()))


class FakeClient:
    def __init__(self) -> None:
        self.calls = []
        self.closed = 0
        release = manifest()
        self.manifest_sha256 = hashlib.sha256(
            release.to_json().encode("utf-8")
        ).hexdigest()
        self.build_digest = release.build_digest
        self.bootstrap_proof_token = (
            "bootstrap-index-proof:bread_" + "4" * 32 + ":sha256:" + "5" * 64
        )

    @staticmethod
    def _candidate(status="candidate"):
        return CandidateProjection(
            release_id="release-stable-" + "a" * 24,
            version="1.0.0",
            build_digest="b" * 64,
            channel="stable",
            status=status,
            gates={gate: "passed" for gate in REQUIRED_RELEASE_GATES},
            missing_gates=[],
        )

    def create_candidate(
        self, _manifest, *, manifest_sha256, client_request_id
    ):
        assert len(manifest_sha256) == 64
        self.calls.append(("candidate", client_request_id))
        return self._candidate()

    def record_gate_bundle(
        self, _release_id, attestation, *, client_request_id
    ):
        assert attestation["attestation_type"] == "ecorex-release-gate-bundle"
        assert attestation["signature"]["key_id"] == "release-key"
        self.calls.append(("gate-bundle", client_request_id))
        return self._candidate()

    def publish(self, _release_id, *, client_request_id):
        self.calls.append(("publish", client_request_id))
        return self._candidate("published")

    def trusted_bootstrap_index_proof(self, release_id):
        self.calls.append(("bootstrap-proof", "read"))
        target = {
            "manifest_sha256": self.manifest_sha256,
            "release_id": release_id,
            "version": "1.0.0",
            "build_digest": self.build_digest,
        }
        return BootstrapIndexProofProjection(
            schema_version=1,
            record_id="bread_" + "4" * 32,
            activation_record_id="bactive_" + "2" * 32,
            stage_record_id="bstage_" + "1" * 32,
            release_id=release_id,
            version="1.0.0",
            build_digest=self.build_digest,
            sequence=1,
            revision=release_id,
            issued_at="2026-07-12T00:00:00Z",
            expires_at="2026-07-12T01:00:00Z",
            target=target,
            index_sha256="d" * 64,
            index_size_bytes=1024,
            public_url=("https://download.example/stable/public-bootstrap-index.json"),
            read_back_at="2026-07-12T00:00:01+00:00",
            proof_token=self.bootstrap_proof_token,
        )

    def create_rollout(self, _release_id, **kwargs):
        self.calls.append(("rollout", kwargs["client_request_id"]))
        return RolloutProjection(
            rollout_id="rollout-one",
            release_id="release-stable-" + "a" * 24,
            channel="stable",
            status="draft",
            percentage=kwargs["percentage"],
            target_organization_ids=kwargs["organizations"],
            target_account_ids=kwargs["accounts"],
            minimum_compatible_version=kwargs["minimum_compatible_version"],
            created_at="2026-07-10T12:00:00+00:00",
        )

    def rollout_action(self, rollout_id, action, *, client_request_id):
        self.calls.append((f"{action}:{rollout_id}", client_request_id))
        return RolloutProjection(
            rollout_id=rollout_id,
            release_id="release-stable-" + "a" * 24,
            channel="stable",
            status="active",
            percentage=10,
            target_organization_ids=[],
            target_account_ids=[],
            minimum_compatible_version="0.3.0",
            created_at="2026-07-10T12:00:00+00:00",
        )

    @staticmethod
    def _freshness_payload() -> dict[str, object]:
        return {
            "schema_version": 1,
            "status": "healthy",
            "active_expires_at": "2026-07-13T00:00:00Z",
            "active_authority_sha256": "f" * 64,
            "remaining_seconds": 36000,
            "last_checked_at": "2026-07-12T14:00:00+00:00",
            "next_check_at": "2026-07-12T15:00:00+00:00",
            "last_attempt_record_id": "brefresh_" + "a" * 32,
            "last_success_at": "2026-07-12T14:00:00+00:00",
            "last_failure_at": None,
            "last_error_code": None,
            "lease_owner_id": None,
            "lease_expires_at": None,
            "updated_at": "2026-07-12T14:00:00+00:00",
            "automation_enabled": True,
            "signer_configured": True,
            "lead_seconds": 28800,
            "check_interval_seconds": 3600,
            "lease_seconds": 600,
            "scheduler_running": True,
            "scheduler_ready": True,
            "scheduler_last_heartbeat_at": "2026-07-12T14:00:00+00:00",
            "scheduler_last_error_code": None,
            "scheduler_heartbeat_max_age_seconds": 4320,
        }

    def bootstrap_freshness_status(self):
        self.calls.append(("freshness-status", "read"))
        return BootstrapFreshnessStatusProjection.model_validate(
            self._freshness_payload()
        )

    def refresh_bootstrap_freshness(self, *, client_request_id):
        self.calls.append(("freshness-refresh", client_request_id))
        return BootstrapFreshnessRunProjection.model_validate(
            {**self._freshness_payload(), "run_state": "succeeded"}
        )

    def close(self):
        self.closed += 1


class AmbiguousFreshnessClient(FakeClient):
    def __init__(self) -> None:
        super().__init__()
        self.authority_sha256 = "f" * 64
        self.active_expires_at = "2026-07-13T00:00:00Z"
        self.responses: dict[str, BootstrapFreshnessRunProjection] = {}
        self.request_ids: list[str] = []
        self.publication_count = 0
        self.lose_next_response = True

    def bootstrap_freshness_status(self):
        payload = self._freshness_payload()
        payload["active_authority_sha256"] = self.authority_sha256
        payload["active_expires_at"] = self.active_expires_at
        return BootstrapFreshnessStatusProjection.model_validate(payload)

    def refresh_bootstrap_freshness(self, *, client_request_id):
        self.request_ids.append(client_request_id)
        response = self.responses.get(client_request_id)
        if response is None:
            self.publication_count += 1
            self.active_expires_at = (
                f"2026-07-{13 + self.publication_count:02d}T00:00:00Z"
            )
            payload = self._freshness_payload()
            payload["active_authority_sha256"] = self.authority_sha256
            payload["active_expires_at"] = self.active_expires_at
            response = BootstrapFreshnessRunProjection.model_validate(
                {**payload, "run_state": "succeeded"}
            )
            self.responses[client_request_id] = response
            if self.lose_next_response:
                self.lose_next_response = False
                raise RuntimeError("simulated response loss")
        return response


def publication_receipt(tmp_path, release: ReleaseManifest):
    manifest_payload = release.to_json().encode("utf-8")
    manifest_digest = hashlib.sha256(manifest_payload).hexdigest()
    identities = {
        release.artifacts[0].file_name: (
            release.artifacts[0].size_bytes,
            release.artifacts[0].sha256,
        ),
        "release-manifest.json": (len(manifest_payload), manifest_digest),
        "release-metadata.json": (8, hashlib.sha256(b"metadata").hexdigest()),
        "sbom.cdx.json": (4, hashlib.sha256(b"sbom").hexdigest()),
    }
    sources = {}
    for source in release.sources:
        sources[source.source_id] = [
            {
                "name": name,
                "size_bytes": size,
                "sha256": digest,
                "url": f"{source.base_url}/{quote(name, safe='')}",
            }
            for name, (size, digest) in sorted(identities.items())
        ]
    value = {
        "schema_version": 1,
        "release_id": release.release_id,
        "version": release.version,
        "manifest_sha256": manifest_digest,
        "github_release_id": 42,
        "github_draft": False,
        "source_receipts": sources,
    }
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    path = tmp_path / "publication-receipt.json"
    path.write_bytes(payload)
    token = "publication-receipt:sha256:" + hashlib.sha256(payload).hexdigest()
    return path, token


def bootstrap_index_receipt(tmp_path, release: ReleaseManifest, publication_path):
    publication_sha256 = hashlib.sha256(publication_path.read_bytes()).hexdigest()
    manifest_sha256 = hashlib.sha256(release.to_json().encode("utf-8")).hexdigest()
    proof_token = "bootstrap-index-proof:bread_" + "4" * 32 + ":sha256:" + "5" * 64
    value = {
        "schema_version": 1,
        "receipt_type": "ecorex-public-bootstrap-index-publication",
        "release_id": release.release_id,
        "version": release.version,
        "state": "active-and-read-back",
        "index_sha256": hashlib.sha256(b"public index").hexdigest(),
        "index_size_bytes": len(b"public index"),
        "public_url": "https://download.example/stable/public-bootstrap-index.json",
        "staged_revision_id": "bstage_" + "1" * 32,
        "active_activation_record_id": "bactive_" + "2" * 32,
        "active_sequence": 1,
        "active_authority_revision_id": release.release_id,
        "active_target": {
            "manifest_sha256": manifest_sha256,
            "release_id": release.release_id,
            "version": release.version,
            "build_digest": release.build_digest,
        },
        "public_object_revision_id": "pobj_" + "3" * 32,
        "previous_activation_record_id": None,
        "previous_sequence": None,
        "previous_authority_revision_id": None,
        "previous_index_sha256": None,
        "previous_target": None,
        "readback_record_id": "bread_" + "4" * 32,
        "readback_proof_token": proof_token,
        "read_back_at": "2026-07-12T00:00:01+00:00",
        "cache_control": "no-store",
        "manifest_sha256": manifest_sha256,
        "release_publication_receipt_sha256": publication_sha256,
        "stage_receipt_sha256": "6" * 64,
    }
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    path = tmp_path / "bootstrap-index-publication-receipt.json"
    path.write_bytes(payload)
    return path, proof_token


def signed_gate_bundle(
    release: ReleaseManifest,
    *,
    publication_token: str,
    bootstrap_token: str | None,
    phase: str,
) -> dict:
    names = set(REQUIRED_RELEASE_GATES)
    if phase == "prepare":
        names.remove("bootstrap-index")
    gates = {}
    for gate in names:
        if gate in {"github-release", "mirror-sync", "cdn-sync"}:
            evidence = publication_token
        elif gate == "bootstrap-index":
            assert bootstrap_token is not None
            evidence = bootstrap_token
        else:
            evidence = "gate-receipt:sha256:" + hashlib.sha256(gate.encode()).hexdigest()
        gates[gate] = {"status": "passed", "evidence": evidence}
    unsigned = build_unsigned_gate_bundle(
        phase=phase,
        commit_sha="a" * 40,
        workflow_run_id=7001,
        manifest=release,
        manifest_sha256=hashlib.sha256(release.to_json().encode()).hexdigest(),
        gates=gates,
    )
    return sign_gate_bundle(unsigned, signer=_SIGNER, manifest=release)


def test_promote_journal_reuses_request_ids_and_never_duplicates_rollout(
    tmp_path, capsys
) -> None:
    release = manifest()
    manifest_path = tmp_path / "manifest.json"
    evidence_path = tmp_path / "evidence.json"
    journal_path = tmp_path / "promotion.json"
    manifest_path.write_text(release.to_json(), encoding="utf-8")
    receipt_path, publication_token = publication_receipt(tmp_path, release)
    bootstrap_receipt_path, bootstrap_token = bootstrap_index_receipt(
        tmp_path, release, receipt_path
    )
    evidence_path.write_text(
        json.dumps(
            signed_gate_bundle(
                release,
                publication_token=publication_token,
                bootstrap_token=bootstrap_token,
                phase="finalize",
            )
        ),
        encoding="utf-8",
    )
    fake = FakeClient()
    argv = [
        "--endpoint",
        "https://control.ecorex.test",
        "--allowed-host",
        "control.ecorex.test",
        "promote",
        "--manifest",
        str(manifest_path),
        "--evidence",
        str(evidence_path),
        "--trusted-key",
        _TRUSTED_KEY,
        "--publication-receipt",
        str(receipt_path),
        "--bootstrap-index-receipt",
        str(bootstrap_receipt_path),
        "--journal",
        str(journal_path),
        "--percentage",
        "10",
        "--organization",
        "org-b",
        "--organization",
        "org-a",
        "--account",
        "account-a",
        "--minimum-compatible-version",
        "0.3.0",
        "--activate",
    ]
    assert run(argv, client_factory=lambda _args: fake) == 0
    first_calls = list(fake.calls)
    first_ids = dict(first_calls)
    assert sum(name == "rollout" for name, _request_id in first_calls) == 1
    assert sum(name.startswith("activate:") for name, _request_id in first_calls) == 1
    names = [name for name, _request_id in first_calls]
    assert names.index("bootstrap-proof") < names.index("gate-bundle")
    assert names.index("gate-bundle") < names.index("rollout")
    assert names.index("rollout") < names.index("publish")
    assert names.index("publish") < names.index("activate:rollout-one")

    fake.calls.clear()
    assert run(argv, client_factory=lambda _args: fake) == 0
    second_ids = dict(fake.calls)
    assert second_ids["candidate"] == first_ids["candidate"]
    assert second_ids["publish"] == first_ids["publish"]
    assert second_ids["gate-bundle"] == first_ids["gate-bundle"]
    assert all(name != "rollout" for name, _request_id in fake.calls)
    assert all(not name.startswith("activate:") for name, _request_id in fake.calls)
    assert fake.closed == 2
    output = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert output["rollout_id"] == "rollout-one"
    assert output["activated"] is True
    stored = json.loads(journal_path.read_text(encoding="utf-8"))
    assert "token" not in json.dumps(stored).casefold()
    assert stored["schema_version"] == 4
    assert len(stored["rollout_target_sha256"]) == 64
    assert len(stored["prepare_evidence_sha256"]) == 64
    assert len(stored["final_evidence_sha256"]) == 64

    changed = list(argv)
    changed[changed.index("--percentage") + 1] = "20"
    fake.calls.clear()
    assert run(changed, client_factory=lambda _args: fake) == 1
    assert fake.calls == []
    assert "does not match" in capsys.readouterr().err


def test_promote_dry_run_rejects_incomplete_or_failed_evidence(
    tmp_path, capsys
) -> None:
    manifest_path = tmp_path / "manifest.json"
    evidence_path = tmp_path / "evidence.json"
    manifest_path.write_text(manifest().to_json(), encoding="utf-8")
    evidence_path.write_text(
        json.dumps({"lint": {"status": "failed", "evidence": "ci://lint"}}),
        encoding="utf-8",
    )
    result = run(
        [
            "promote",
            "--manifest",
            str(manifest_path),
            "--evidence",
            str(evidence_path),
            "--trusted-key",
            _TRUSTED_KEY,
            "--journal",
            str(tmp_path / "unused.json"),
            "--dry-run",
        ]
    )
    assert result == 1
    assert "bundle" in capsys.readouterr().err
    assert not (tmp_path / "unused.json").exists()


def test_promote_rejects_publication_gates_bound_to_unrelated_receipts(
    tmp_path,
    capsys,
) -> None:
    release = manifest()
    manifest_path = tmp_path / "manifest.json"
    evidence_path = tmp_path / "evidence.json"
    manifest_path.write_text(release.to_json(), encoding="utf-8")
    receipt_path, publication_token = publication_receipt(tmp_path, release)
    evidence = signed_gate_bundle(
        release,
        publication_token="publication-receipt:sha256:" + "f" * 64,
        bootstrap_token=None,
        phase="prepare",
    )
    evidence_path.write_text(json.dumps(evidence), encoding="utf-8")
    result = run(
        [
            "promote",
            "--manifest",
            str(manifest_path),
            "--evidence",
            str(evidence_path),
            "--trusted-key",
            _TRUSTED_KEY,
            "--phase",
            "prepare",
            "--publication-receipt",
            str(receipt_path),
            "--journal",
            str(tmp_path / "unused.json"),
            "--dry-run",
        ]
    )
    assert result == 1
    assert "same publication receipt" in capsys.readouterr().err


def test_admin_cli_exposes_freshness_status_and_one_shot_refresh(
    tmp_path, capsys
) -> None:
    fake = FakeClient()
    common = [
        "--endpoint",
        "https://control.ecorex.test",
        "--allowed-host",
        "control.ecorex.test",
    ]
    assert (
        run(
            [*common, "bootstrap-freshness-status"],
            client_factory=lambda _args: fake,
        )
        == 0
    )
    status = json.loads(capsys.readouterr().out)
    assert status["status"] == "healthy"
    refresh_args = [
        *common,
        "refresh-bootstrap-freshness",
        "--request-journal",
        str(tmp_path / "freshness-request.json"),
    ]
    assert (
        run(
            refresh_args,
            client_factory=lambda _args: fake,
        )
        == 0
    )
    refreshed = json.loads(capsys.readouterr().out)
    assert refreshed["run_state"] == "succeeded"
    first_request_id = [
        request_id for name, request_id in fake.calls if name == "freshness-refresh"
    ][-1]
    assert (
        run(
            refresh_args,
            client_factory=lambda _args: fake,
        )
        == 0
    )
    capsys.readouterr()
    refresh_ids = [
        request_id for name, request_id in fake.calls if name == "freshness-refresh"
    ]
    assert refresh_ids[0] == first_request_id
    assert refresh_ids[1] != first_request_id
    assert (
        run(
            [
                *common,
                "refresh-bootstrap-freshness",
                "--client-request-id",
                "operator-retry-identity",
            ],
            client_factory=lambda _args: fake,
        )
        == 0
    )
    capsys.readouterr()
    assert fake.calls[-1] == (
        "freshness-refresh",
        "operator-retry-identity",
    )
    assert [name for name, _request_id in fake.calls] == [
        "freshness-status",
        "freshness-status",
        "freshness-refresh",
        "freshness-status",
        "freshness-refresh",
        "freshness-refresh",
    ]


def test_manual_freshness_journal_reuses_pending_after_response_loss(
    tmp_path, capsys
) -> None:
    client = AmbiguousFreshnessClient()
    journal = tmp_path / "freshness-request.json"
    argv = [
        "--endpoint",
        "https://control.ecorex.test",
        "--allowed-host",
        "control.ecorex.test",
        "refresh-bootstrap-freshness",
        "--request-journal",
        str(journal),
    ]
    assert run(argv, client_factory=lambda _args: client) == 1
    capsys.readouterr()
    first_id = client.request_ids[-1]
    assert client.publication_count == 1
    pending = json.loads(journal.read_text(encoding="utf-8"))
    assert pending["pending"]["request_id"] == first_id

    # The server committed and changed freshness expiry, but immutable
    # authority stayed the same. The local pending identity must win.
    assert run(argv, client_factory=lambda _args: client) == 0
    capsys.readouterr()
    assert client.request_ids[-1] == first_id
    assert client.publication_count == 1
    completed = json.loads(journal.read_text(encoding="utf-8"))
    assert completed["pending"] is None
    assert completed["audit"][-1]["event"] == "completed"

    # A later intentional refresh starts a new durable identity only after the
    # previous success was observed and atomically cleared.
    assert run(argv, client_factory=lambda _args: client) == 0
    capsys.readouterr()
    assert client.request_ids[-1] != first_id
    assert client.publication_count == 2


def test_manual_freshness_journal_invalidates_pending_on_authority_change(
    tmp_path, capsys
) -> None:
    client = AmbiguousFreshnessClient()
    journal = tmp_path / "freshness-target-change.json"
    argv = [
        "--endpoint",
        "https://control.ecorex.test",
        "--allowed-host",
        "control.ecorex.test",
        "refresh-bootstrap-freshness",
        "--request-journal",
        str(journal),
    ]
    assert run(argv, client_factory=lambda _args: client) == 1
    capsys.readouterr()
    stale_id = client.request_ids[-1]
    client.authority_sha256 = "e" * 64
    assert run(argv, client_factory=lambda _args: client) == 0
    capsys.readouterr()
    assert client.request_ids[-1] != stale_id
    value = json.loads(journal.read_text(encoding="utf-8"))
    assert any(
        event["event"] == "invalidated-target-change"
        and event["request_id"] == stale_id
        for event in value["audit"]
    )
