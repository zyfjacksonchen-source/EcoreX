from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from urllib.parse import quote

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from ecorex.control_plane.cli import run as control_plane_run
from ecorex.control_plane.repository import required_release_gates
from ecorex.release import (
    ArtifactBuildInput,
    ArtifactKind,
    Ed25519MemorySigner,
    ReleaseBuildSpec,
    ReleaseBuilder,
    sign_gate_bundle,
)
from ecorex.update import ReleaseChannel, ReleaseSource, SourceKind


PUBLICATION_GATES = frozenset({"github-release", "mirror-sync", "cdn-sync"})
COMMIT = "1" * 40


def test_real_builder_bytes_flow_through_receipt_evidence_and_promotion(
    tmp_path: Path,
    capsys,
) -> None:
    """The pretty JSON + LF emitted by ReleaseBuilder is the one identity."""

    source = tmp_path / "source"
    source.mkdir()
    (source / "runtime.txt").write_text("EcoreX Runtime\n", encoding="utf-8")
    signer = Ed25519MemorySigner(
        "release-key-2026",
        Ed25519PrivateKey.generate(),
    )
    sources = (
        ReleaseSource(
            "mirror",
            SourceKind.GITHUB_CN_MIRROR,
            0,
            "https://mirror.example/ecorex/v1.0.0/canary",
        ),
        ReleaseSource(
            "github",
            SourceKind.GITHUB_RELEASE,
            1,
            "https://github.example/ecorex/v1.0.0/canary",
        ),
        ReleaseSource(
            "cdn",
            SourceKind.ECOREX_CDN,
            2,
            "https://cdn.example/ecorex/v1.0.0/canary",
        ),
    )
    built = ReleaseBuilder(signer).build(
        ReleaseBuildSpec(
            channel=ReleaseChannel.CANARY,
            created_at="2026-07-12T12:00:00+08:00",
            sources=sources,
            artifacts=(
                ArtifactBuildInput(
                    source_dir=source,
                    kind=ArtifactKind.CORE,
                    platform="windows",
                    architecture="x64",
                ),
            ),
        ),
        tmp_path / "release",
    )
    manifest_payload = built.manifest_path.read_bytes()
    assert manifest_payload.endswith(b"\n")
    assert manifest_payload != built.manifest.to_json().encode("utf-8")
    manifest_sha256 = hashlib.sha256(manifest_payload).hexdigest()

    immutable_files = {
        path.name: path.read_bytes()
        for path in built.output_dir.iterdir()
        if path.is_file()
    }
    expected_names = {
        "release-manifest.json",
        "release-metadata.json",
        "sbom.cdx.json",
        *(artifact.file_name for artifact in built.manifest.artifacts),
    }
    assert set(immutable_files) == expected_names
    source_receipts = {
        release_source.source_id: [
            {
                "name": name,
                "size_bytes": len(immutable_files[name]),
                "sha256": hashlib.sha256(immutable_files[name]).hexdigest(),
                "url": f"{release_source.base_url}/{quote(name, safe='')}",
            }
            for name in sorted(expected_names)
        ]
        for release_source in built.manifest.sources
    }
    publication_value = {
        "schema_version": 1,
        "release_id": built.manifest.release_id,
        "version": built.manifest.version,
        "manifest_sha256": manifest_sha256,
        "github_release_id": 101,
        "github_draft": False,
        "source_receipts": source_receipts,
    }
    publication_path = tmp_path / "publication-receipt.json"
    publication_path.write_bytes(
        json.dumps(
            publication_value,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )

    gates = required_release_gates(ReleaseChannel.CANARY)
    receipt_directory = tmp_path / "gates"
    receipt_directory.mkdir()
    for gate in sorted(gates - PUBLICATION_GATES):
        (receipt_directory / f"{gate}.json").write_text(
            json.dumps(
                    {
                        "schema_version": 2,
                        "receipt_type": "ecorex-release-gate",
                        "gate": gate,
                    "status": "passed",
                    "commit_sha": COMMIT,
                        "workflow_run_id": 42,
                        "release_id": built.manifest.release_id,
                        "version": built.manifest.version,
                        "channel": built.manifest.channel.value,
                        "build_digest": built.manifest.build_digest,
                        "manifest_sha256": manifest_sha256,
                        "evidence_type": "test-fixture",
                        "evidence_sha256": hashlib.sha256(gate.encode()).hexdigest(),
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )
    evidence_path = tmp_path / "release-evidence.json"
    repository = Path(__file__).resolve().parents[2]
    assembled = subprocess.run(
        [
            sys.executable,
            str(repository / "scripts/assemble-v1-release-evidence.py"),
            "--receipts-dir",
            str(receipt_directory),
            "--publication-receipt",
            str(publication_path),
            "--manifest",
            str(built.manifest_path),
            "--expected-commit",
            COMMIT,
            "--output",
            str(evidence_path),
        ],
        capture_output=True,
        check=False,
    )
    assert assembled.returncode == 0, assembled.stderr.decode(errors="replace")
    unsigned = json.loads(evidence_path.read_text(encoding="utf-8"))
    evidence_path.write_text(
        json.dumps(
            sign_gate_bundle(
                unsigned,
                signer=signer,
                manifest=built.manifest,
            ),
            sort_keys=True,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    trusted_key = "release-key-2026=" + base64.b64encode(
        signer.public_key_bytes
    ).decode("ascii")

    journal = tmp_path / "dry-run-journal.json"
    assert (
        control_plane_run(
            [
                "promote",
                "--manifest",
                str(built.manifest_path),
                "--evidence",
                str(evidence_path),
                "--trusted-key",
                trusted_key,
                "--publication-receipt",
                str(publication_path),
                "--journal",
                str(journal),
                "--percentage",
                "1",
                "--dry-run",
            ]
        )
        == 0
    )
    output = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert output["dry_run"] is True
    assert output["publication_receipt"].startswith(
        "publication-receipt:sha256:"
    )
    assert journal.exists() is False
