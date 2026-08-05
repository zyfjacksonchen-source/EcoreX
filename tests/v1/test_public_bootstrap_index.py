from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta
import hashlib
import json
from pathlib import Path
from urllib.parse import quote

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import jsonschema
import pytest

from ecorex import __version__
from ecorex.control_plane.cli import run
from ecorex.control_plane.repository import _parse_bootstrap_index_bytes
from ecorex.release import (
    ArtifactBuildInput,
    ArtifactKind,
    Ed25519MemorySigner,
    PublicBootstrapPublicationReceipt,
    PublicBootstrapStageReceipt,
    PublicBootstrapIndexError,
    ReleaseBuilder,
    ReleaseBuildSpec,
    build_public_bootstrap_index,
    public_bootstrap_authority_signing_bytes,
    public_bootstrap_freshness_signing_bytes,
    refresh_public_bootstrap_freshness,
    stable_pointer_sequence,
    unpublished_public_bootstrap_index,
    validate_public_bootstrap_index,
    write_public_bootstrap_index,
)
from ecorex.update import (
    Ed25519SignatureVerifier,
    ReleaseChannel,
    ReleaseSource,
    SourceKind,
    SignatureEnvelope,
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _canonical_digest(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _release(tmp_path: Path, *, include_all_targets: bool = True):
    source = tmp_path / "bootstrap-source"
    source.mkdir(parents=True)
    (source / "ecorex-bootstrap").write_bytes(b"signed bootstrap supervisor")
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    targets = [
        ("windows", "x64"),
        ("macos", "arm64"),
        ("macos", "x64"),
    ]
    if not include_all_targets:
        targets.pop()
    artifacts = tuple(
        ArtifactBuildInput(
            source_dir=source,
            kind=ArtifactKind.BOOTSTRAP,
            platform=platform,
            architecture=architecture,
            executable_paths=("ecorex-bootstrap",),
        )
        for platform, architecture in targets
    )
    spec = ReleaseBuildSpec(
        channel=ReleaseChannel.STABLE,
        created_at="2026-07-10T22:00:00+08:00",
        sources=(
            ReleaseSource(
                "github-cn",
                SourceKind.GITHUB_CN_MIRROR,
                0,
                "https://mirror.example/ecorex/v1.0.0",
            ),
            ReleaseSource(
                "github",
                SourceKind.GITHUB_RELEASE,
                1,
                "https://github.com/acme/ecorex/releases/download/v1.0.0",
            ),
            ReleaseSource(
                "cdn",
                SourceKind.ECOREX_CDN,
                2,
                "https://cdn.example/ecorex/v1.0.0",
            ),
        ),
        artifacts=artifacts,
    )
    signer = Ed25519MemorySigner("release-key-2026", private)
    built = ReleaseBuilder(signer).build(spec, tmp_path / "release")
    verifier = Ed25519SignatureVerifier({"release-key-2026": public})
    freshness_private = Ed25519PrivateKey.generate()
    freshness_public = freshness_private.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    freshness_signer = Ed25519MemorySigner("publication-key-2026", freshness_private)
    freshness_verifier = Ed25519SignatureVerifier(
        {"publication-key-2026": freshness_public}
    )
    return (
        built,
        verifier,
        public,
        signer,
        freshness_verifier,
        freshness_public,
        freshness_signer,
    )


def _receipt(built) -> tuple[dict[str, object], str]:
    files = sorted(built.output_dir.iterdir(), key=lambda path: path.name)
    source_receipts: dict[str, list[dict[str, object]]] = {}
    for source in built.manifest.sources:
        source_receipts[source.source_id] = [
            {
                "name": path.name,
                "size_bytes": path.stat().st_size,
                "sha256": _sha256(path),
                "url": f"{source.base_url}/{quote(path.name, safe='')}",
            }
            for path in files
        ]
    value: dict[str, object] = {
        "schema_version": 1,
        "release_id": built.manifest.release_id,
        "version": built.manifest.version,
        "manifest_sha256": _sha256(built.manifest_path),
        "github_release_id": 42,
        "github_draft": False,
        "source_receipts": source_receipts,
    }
    return value, _canonical_digest(value)


def test_public_index_is_only_a_three_target_discovery_hint(tmp_path: Path) -> None:
    built, verifier, _public, signer, fresh_verifier, _fresh_public, fresh_signer = (
        _release(tmp_path)
    )
    receipt, receipt_digest = _receipt(built)
    index = build_public_bootstrap_index(
        manifest=built.manifest,
        manifest_bytes=built.manifest_path.read_bytes(),
        manifest_sha256=_sha256(built.manifest_path),
        publication_receipt=receipt,
        publication_receipt_sha256=receipt_digest,
        verifier=verifier,
        freshness_verifier=fresh_verifier,
        signer=signer,
        freshness_signer=fresh_signer,
    )

    assert index["trust"] == "untrusted-discovery-hint"
    assert index["status"] == "published"
    authority = index["authority"]
    assert isinstance(authority, dict)
    expected_sequence = stable_pointer_sequence(__version__)
    assert authority["sequence"] == expected_sequence
    assert authority["revision"] == built.manifest.release_id
    assert authority["target"] == {
        "manifest_sha256": _sha256(built.manifest_path),
        "release_id": built.manifest.release_id,
        "version": __version__,
        "build_digest": built.manifest.build_digest,
    }
    release = index["release"]
    assert isinstance(release, dict)
    assert release["publication_receipt_sha256"] == receipt_digest
    assert [
        (item["artifact_id"], item["platform"], item["architecture"])
        for item in release["bootstrap_artifacts"]
    ] == [
        ("bootstrap-windows-x64", "windows", "x64"),
        ("bootstrap-macos-arm64", "macos", "arm64"),
        ("bootstrap-macos-x64", "macos", "x64"),
    ]
    assert [source["kind"] for source in release["manifest"]["sources"]] == [
        "github-cn-mirror"
    ]
    assert all(
        item["sources"][0]["url"].startswith("https://mirror.example/")
        for item in release["bootstrap_artifacts"]
    )

    root = Path(__file__).resolve().parents[2]
    schema = json.loads(
        (root / "release/v1/public-bootstrap-index.schema.json").read_text(
            encoding="utf-8"
        )
    )
    validator = jsonschema.Draft202012Validator(
        schema, format_checker=jsonschema.FormatChecker()
    )
    validator.validate(index)
    validator.validate(unpublished_public_bootstrap_index())
    validate_public_bootstrap_index(
        index, verifier=verifier, freshness_verifier=fresh_verifier
    )
    validate_public_bootstrap_index(unpublished_public_bootstrap_index())


def test_pointer_sequence_is_deterministic_monotonic_and_signature_reproducible(
    tmp_path: Path,
) -> None:
    assert stable_pointer_sequence("0.3.0") == 30_001
    assert stable_pointer_sequence("0.3.1") == 30_002
    assert stable_pointer_sequence("1.0.0") == 100_000_001
    with pytest.raises(PublicBootstrapIndexError, match="final product SemVer"):
        stable_pointer_sequence("1.0.1-rc.1")

    built, verifier, _public, signer, fresh_verifier, _fresh_public, fresh_signer = (
        _release(tmp_path)
    )
    receipt, receipt_digest = _receipt(built)
    signed = build_public_bootstrap_index(
        manifest=built.manifest,
        manifest_bytes=built.manifest_path.read_bytes(),
        manifest_sha256=_sha256(built.manifest_path),
        publication_receipt=receipt,
        publication_receipt_sha256=receipt_digest,
        verifier=verifier,
        freshness_verifier=fresh_verifier,
        signer=signer,
        freshness_signer=fresh_signer,
    )
    authority = signed["authority"]
    assert isinstance(authority, dict)
    expected_sequence = stable_pointer_sequence(__version__)
    existing = SignatureEnvelope.from_dict(authority["signature"])
    freshness = signed["freshness"]
    assert isinstance(freshness, dict)
    existing_freshness = SignatureEnvelope.from_dict(freshness["signature"])
    reproduced = build_public_bootstrap_index(
        manifest=built.manifest,
        manifest_bytes=built.manifest_path.read_bytes(),
        manifest_sha256=_sha256(built.manifest_path),
        publication_receipt=receipt,
        publication_receipt_sha256=receipt_digest,
        verifier=verifier,
        freshness_verifier=fresh_verifier,
        authority_signature=existing,
        freshness_signature=existing_freshness,
        freshness_issued_at=str(freshness["issued_at"]),
        freshness_expires_at=str(freshness["expires_at"]),
    )
    assert reproduced == signed
    target = authority["target"]
    assert isinstance(target, dict)
    assert (
        verifier.verify(
            public_bootstrap_authority_signing_bytes(
                sequence=expected_sequence,
                revision=built.manifest.release_id,
                target=target,
            ),
            existing,
        )
        is True
    )


def test_legacy_v1017_sequence_is_only_accepted_by_explicit_migration_mode(
    tmp_path: Path,
) -> None:
    built, verifier, _public, signer, fresh_verifier, _fresh_public, fresh_signer = (
        _release(tmp_path)
    )
    receipt, receipt_digest = _receipt(built)
    legacy = build_public_bootstrap_index(
        manifest=built.manifest,
        manifest_bytes=built.manifest_path.read_bytes(),
        manifest_sha256=_sha256(built.manifest_path),
        publication_receipt=receipt,
        publication_receipt_sha256=receipt_digest,
        verifier=verifier,
        freshness_verifier=fresh_verifier,
        signer=signer,
        freshness_signer=fresh_signer,
    )
    legacy["release"]["version"] = "1.0.17"
    legacy["authority"]["target"]["version"] = "1.0.17"
    legacy["authority"]["sequence"] = 18
    authority_payload = public_bootstrap_authority_signing_bytes(
        sequence=18,
        revision=legacy["authority"]["revision"],
        target=legacy["authority"]["target"],
    )
    legacy["authority"]["signature"] = SignatureEnvelope(
        "ed25519",
        signer.key_id,
        base64.b64encode(signer.sign(authority_payload)).decode("ascii"),
    ).to_dict()
    legacy["freshness"]["authority_sha256"] = hashlib.sha256(
        authority_payload
    ).hexdigest()
    freshness_payload = public_bootstrap_freshness_signing_bytes(
        authority_sha256=legacy["freshness"]["authority_sha256"],
        issued_at=legacy["freshness"]["issued_at"],
        expires_at=legacy["freshness"]["expires_at"],
    )
    legacy["freshness"]["signature"] = SignatureEnvelope(
        "ed25519",
        fresh_signer.key_id,
        base64.b64encode(fresh_signer.sign(freshness_payload)).decode("ascii"),
    ).to_dict()

    with pytest.raises(PublicBootstrapIndexError, match="target is inconsistent"):
        validate_public_bootstrap_index(legacy)
    validate_public_bootstrap_index(
        legacy,
        verifier=verifier,
        freshness_verifier=fresh_verifier,
        allow_legacy_v1017_sequence=True,
    )
    _parse_bootstrap_index_bytes(
        json.dumps(
            legacy,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        + b"\n",
        verifier=verifier,
        freshness_verifier=fresh_verifier,
    )
    refresh_now = datetime.strptime(
        legacy["freshness"]["issued_at"], "%Y-%m-%dT%H:%M:%SZ"
    ).replace(tzinfo=UTC)
    refreshed = refresh_public_bootstrap_freshness(
        legacy,
        verifier=verifier,
        freshness_verifier=fresh_verifier,
        freshness_signer=fresh_signer,
        issued_at=refresh_now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        expires_at=(refresh_now + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        now=refresh_now,
        allow_legacy_v1017_sequence=True,
    )
    assert refreshed["authority"] == legacy["authority"]


def test_freshness_uses_independent_online_key_and_exact_bounded_window(
    tmp_path: Path,
) -> None:
    (
        built,
        verifier,
        release_public,
        signer,
        freshness_verifier,
        _fresh_public,
        freshness_signer,
    ) = _release(tmp_path)
    receipt, receipt_digest = _receipt(built)
    now = datetime(2026, 7, 12, 8, 0, 0, tzinfo=UTC)
    issued_at = "2026-07-12T08:00:00Z"
    expires_at = "2026-07-12T09:00:00Z"
    index = build_public_bootstrap_index(
        manifest=built.manifest,
        manifest_bytes=built.manifest_path.read_bytes(),
        manifest_sha256=_sha256(built.manifest_path),
        publication_receipt=receipt,
        publication_receipt_sha256=receipt_digest,
        verifier=verifier,
        freshness_verifier=freshness_verifier,
        signer=signer,
        freshness_signer=freshness_signer,
        freshness_issued_at=issued_at,
        freshness_expires_at=expires_at,
        now=now,
    )
    authority = index["authority"]
    freshness = index["freshness"]
    assert isinstance(authority, dict)
    assert isinstance(freshness, dict)
    assert authority["signature"]["key_id"] == "release-key-2026"
    assert freshness["signature"]["key_id"] == "publication-key-2026"
    authority_payload = public_bootstrap_authority_signing_bytes(
        sequence=int(authority["sequence"]),
        revision=str(authority["revision"]),
        target=authority["target"],
    )
    authority_sha256 = hashlib.sha256(authority_payload).hexdigest()
    assert public_bootstrap_freshness_signing_bytes(
        authority_sha256=authority_sha256,
        issued_at=issued_at,
        expires_at=expires_at,
    ) == (
        "ecorex.public-bootstrap-freshness.v1\0"
        f"{authority_sha256}\0{issued_at}\0{expires_at}"
    ).encode("ascii")

    refreshed = build_public_bootstrap_index(
        manifest=built.manifest,
        manifest_bytes=built.manifest_path.read_bytes(),
        manifest_sha256=_sha256(built.manifest_path),
        publication_receipt=receipt,
        publication_receipt_sha256=receipt_digest,
        verifier=verifier,
        freshness_verifier=freshness_verifier,
        authority_signature=SignatureEnvelope.from_dict(authority["signature"]),
        freshness_signer=freshness_signer,
        freshness_issued_at="2026-07-12T08:10:00Z",
        freshness_expires_at="2026-07-12T10:00:00Z",
        now=now + timedelta(minutes=10),
    )
    assert refreshed["authority"] == authority
    assert refreshed["freshness"] != freshness

    with pytest.raises(PublicBootstrapIndexError, match="distinct keys"):
        build_public_bootstrap_index(
            manifest=built.manifest,
            manifest_bytes=built.manifest_path.read_bytes(),
            manifest_sha256=_sha256(built.manifest_path),
            publication_receipt=receipt,
            publication_receipt_sha256=receipt_digest,
            verifier=verifier,
            freshness_verifier=verifier,
            signer=signer,
            freshness_signer=signer,
            now=now,
        )
    shared_private = Ed25519PrivateKey.generate()
    shared_public = shared_private.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    with pytest.raises(PublicBootstrapIndexError, match="distinct keys"):
        build_public_bootstrap_index(
            manifest=built.manifest,
            manifest_bytes=built.manifest_path.read_bytes(),
            manifest_sha256=_sha256(built.manifest_path),
            publication_receipt=receipt,
            publication_receipt_sha256=receipt_digest,
            verifier=Ed25519SignatureVerifier(
                {
                    "release-key-2026": release_public,
                    "release-alias": shared_public,
                }
            ),
            freshness_verifier=Ed25519SignatureVerifier(
                {"publication-alias": shared_public}
            ),
            signer=Ed25519MemorySigner("release-alias", shared_private),
            freshness_signer=Ed25519MemorySigner("publication-alias", shared_private),
            now=now,
        )
    with pytest.raises(PublicBootstrapIndexError, match="expired"):
        validate_public_bootstrap_index(
            index,
            verifier=verifier,
            freshness_verifier=freshness_verifier,
            now=datetime(2026, 7, 12, 9, 0, 0, tzinfo=UTC),
        )
    with pytest.raises(PublicBootstrapIndexError, match="too far in the future"):
        build_public_bootstrap_index(
            manifest=built.manifest,
            manifest_bytes=built.manifest_path.read_bytes(),
            manifest_sha256=_sha256(built.manifest_path),
            publication_receipt=receipt,
            publication_receipt_sha256=receipt_digest,
            verifier=verifier,
            freshness_verifier=freshness_verifier,
            signer=signer,
            freshness_signer=freshness_signer,
            freshness_issued_at="2026-07-12T08:05:01Z",
            freshness_expires_at="2026-07-12T09:00:00Z",
            now=now,
        )
    with pytest.raises(PublicBootstrapIndexError, match="lifetime"):
        build_public_bootstrap_index(
            manifest=built.manifest,
            manifest_bytes=built.manifest_path.read_bytes(),
            manifest_sha256=_sha256(built.manifest_path),
            publication_receipt=receipt,
            publication_receipt_sha256=receipt_digest,
            verifier=verifier,
            freshness_verifier=freshness_verifier,
            signer=signer,
            freshness_signer=freshness_signer,
            freshness_issued_at=issued_at,
            freshness_expires_at=(now + timedelta(days=1, seconds=1)).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            ),
            now=now,
        )


def test_public_index_fails_closed_for_incomplete_or_unpublished_release(
    tmp_path: Path,
) -> None:
    (
        built,
        verifier,
        _public,
        signer,
        fresh_verifier,
        _fresh_public,
        fresh_signer,
    ) = _release(tmp_path / "complete")
    receipt, receipt_digest = _receipt(built)
    draft_receipt = dict(receipt)
    draft_receipt["github_draft"] = True
    with pytest.raises(PublicBootstrapIndexError, match="shape|public release"):
        build_public_bootstrap_index(
            manifest=built.manifest,
            manifest_bytes=built.manifest_path.read_bytes(),
            manifest_sha256=_sha256(built.manifest_path),
            publication_receipt=draft_receipt,
            publication_receipt_sha256=_canonical_digest(draft_receipt),
            verifier=verifier,
            freshness_verifier=fresh_verifier,
            signer=signer,
            freshness_signer=fresh_signer,
        )

    (
        incomplete,
        incomplete_verifier,
        _public,
        incomplete_signer,
        incomplete_fresh_verifier,
        _fresh_public,
        incomplete_fresh_signer,
    ) = _release(tmp_path / "incomplete", include_all_targets=False)
    incomplete_receipt, incomplete_digest = _receipt(incomplete)
    with pytest.raises(PublicBootstrapIndexError, match="requires exactly"):
        build_public_bootstrap_index(
            manifest=incomplete.manifest,
            manifest_bytes=incomplete.manifest_path.read_bytes(),
            manifest_sha256=_sha256(incomplete.manifest_path),
            publication_receipt=incomplete_receipt,
            publication_receipt_sha256=incomplete_digest,
            verifier=incomplete_verifier,
            freshness_verifier=incomplete_fresh_verifier,
            signer=incomplete_signer,
            freshness_signer=incomplete_fresh_signer,
        )


def test_public_index_schema_and_runtime_reject_loose_browser_documents(
    tmp_path: Path,
) -> None:
    built, verifier, _public, signer, fresh_verifier, _fresh_public, fresh_signer = (
        _release(tmp_path)
    )
    receipt, receipt_digest = _receipt(built)
    valid = build_public_bootstrap_index(
        manifest=built.manifest,
        manifest_bytes=built.manifest_path.read_bytes(),
        manifest_sha256=_sha256(built.manifest_path),
        publication_receipt=receipt,
        publication_receipt_sha256=receipt_digest,
        verifier=verifier,
        freshness_verifier=fresh_verifier,
        signer=signer,
        freshness_signer=fresh_signer,
    )
    root = Path(__file__).resolve().parents[2]
    schema = json.loads(
        (root / "release/v1/public-bootstrap-index.schema.json").read_text(
            encoding="utf-8"
        )
    )
    validator = jsonschema.Draft202012Validator(
        schema, format_checker=jsonschema.FormatChecker()
    )

    invalid_documents = []
    extra = json.loads(json.dumps(valid))
    extra["download_url"] = "https://ready.invalid/fake.exe"
    invalid_documents.append(extra)
    bad_signature = json.loads(json.dumps(valid))
    bad_signature["release"]["manifest"]["signature"]["value"] = "AA=="
    invalid_documents.append(bad_signature)
    credential_url = json.loads(json.dumps(valid))
    credential_url["release"]["manifest"]["sources"][0]["url"] = (
        "https://secret@mirror.example/release-manifest.json"
    )
    invalid_documents.append(credential_url)
    unsafe_name = json.loads(json.dumps(valid))
    unsafe_name["release"]["bootstrap_artifacts"][0]["file_name"] = "../agent.exe"
    invalid_documents.append(unsafe_name)
    naive_time = json.loads(json.dumps(valid))
    naive_time["release"]["created_at"] = "2026-07-10T22:00:00"
    invalid_documents.append(naive_time)
    wrong_target = json.loads(json.dumps(valid))
    wrong_target["authority"]["target"]["build_digest"] = "f" * 64

    for invalid in invalid_documents:
        with pytest.raises(jsonschema.ValidationError):
            validator.validate(invalid)
        with pytest.raises(PublicBootstrapIndexError):
            validate_public_bootstrap_index(invalid)

    # JSON Schema can validate each field but cannot express equality between
    # signed target identity and the projected release. The runtime boundary
    # performs that cross-field check.
    validator.validate(wrong_target)
    with pytest.raises(PublicBootstrapIndexError, match="inconsistent"):
        validate_public_bootstrap_index(wrong_target)

    forged_signature = json.loads(json.dumps(valid))
    signature = forged_signature["authority"]["signature"]["value"]
    forged_signature["authority"]["signature"]["value"] = (
        "A" if signature[0] != "A" else "B"
    ) + signature[1:]
    with pytest.raises(PublicBootstrapIndexError, match="signature"):
        validate_public_bootstrap_index(
            forged_signature,
            verifier=verifier,
            freshness_verifier=fresh_verifier,
        )


def test_public_index_rejects_receipt_reformat_and_origin_byte_drift(
    tmp_path: Path,
) -> None:
    built, verifier, _public, signer, fresh_verifier, _fresh_public, fresh_signer = (
        _release(tmp_path)
    )
    receipt, receipt_digest = _receipt(built)
    with pytest.raises(PublicBootstrapIndexError, match="exact signed manifest bytes"):
        build_public_bootstrap_index(
            manifest=built.manifest,
            manifest_bytes=built.manifest_path.read_bytes() + b" ",
            manifest_sha256=_sha256(built.manifest_path),
            publication_receipt=receipt,
            publication_receipt_sha256=receipt_digest,
            verifier=verifier,
            freshness_verifier=fresh_verifier,
            signer=signer,
            freshness_signer=fresh_signer,
        )
    with pytest.raises(PublicBootstrapIndexError, match="canonical bytes"):
        build_public_bootstrap_index(
            manifest=built.manifest,
            manifest_bytes=built.manifest_path.read_bytes(),
            manifest_sha256=_sha256(built.manifest_path),
            publication_receipt=receipt,
            publication_receipt_sha256="f" * 64,
            verifier=verifier,
            freshness_verifier=fresh_verifier,
            signer=signer,
            freshness_signer=fresh_signer,
        )

    forged_manifest_receipt = json.loads(json.dumps(receipt))
    forged_manifest_receipt["manifest_sha256"] = "f" * 64
    for entries in forged_manifest_receipt["source_receipts"].values():
        next(entry for entry in entries if entry["name"] == "release-manifest.json")[
            "sha256"
        ] = "f" * 64
    with pytest.raises(PublicBootstrapIndexError, match="exact signed manifest bytes"):
        build_public_bootstrap_index(
            manifest=built.manifest,
            manifest_bytes=built.manifest_path.read_bytes(),
            manifest_sha256="f" * 64,
            publication_receipt=forged_manifest_receipt,
            publication_receipt_sha256=_canonical_digest(forged_manifest_receipt),
            verifier=verifier,
            freshness_verifier=fresh_verifier,
            signer=signer,
            freshness_signer=fresh_signer,
        )

    drifted = json.loads(json.dumps(receipt))
    cdn_entries = drifted["source_receipts"]["cdn"]
    next(entry for entry in cdn_entries if entry["name"] == "sbom.cdx.json")[
        "sha256"
    ] = "f" * 64
    with pytest.raises(PublicBootstrapIndexError, match="identical release bytes"):
        build_public_bootstrap_index(
            manifest=built.manifest,
            manifest_bytes=built.manifest_path.read_bytes(),
            manifest_sha256=_sha256(built.manifest_path),
            publication_receipt=drifted,
            publication_receipt_sha256=_canonical_digest(drifted),
            verifier=verifier,
            freshness_verifier=fresh_verifier,
            signer=signer,
            freshness_signer=fresh_signer,
        )


def test_public_index_cli_verifies_then_atomically_writes_pointer(
    tmp_path: Path,
    capsys,
) -> None:
    (
        built,
        _verifier,
        public,
        signer,
        _fresh_verifier,
        fresh_public,
        fresh_signer,
    ) = _release(tmp_path)
    receipt, _receipt_digest = _receipt(built)
    receipt_path = tmp_path / "publication-receipt.json"
    receipt_path.write_text(
        json.dumps(
            receipt,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    output = tmp_path / "site" / "public-bootstrap-index.json"
    exit_code = run(
        [
            "build-public-bootstrap-index",
            "--release-dir",
            str(built.output_dir),
            "--publication-receipt",
            str(receipt_path),
            "--output",
            str(output),
            "--trusted-key",
            "release-key-2026=" + base64.b64encode(public).decode("ascii"),
            "--trusted-publication-key",
            "publication-key-2026=" + base64.b64encode(fresh_public).decode("ascii"),
        ],
        public_pointer_signer_factory=lambda _args: signer,
        public_freshness_signer_factory=lambda _args: fresh_signer,
    )
    assert exit_code == 0
    assert json.loads(output.read_text(encoding="utf-8"))["status"] == "published"
    assert not list(output.parent.glob(".*.tmp-*"))
    assert not output.with_suffix(output.suffix + ".lock").exists()
    command_result = json.loads(capsys.readouterr().out)
    assert command_result["trust"] == "untrusted-discovery-hint"
    assert command_result["pointer_sequence"] == stable_pointer_sequence(__version__)
    assert command_result["pointer_revision"] == built.manifest.release_id
    assert command_result["pointer_signature_key_id"] == "release-key-2026"
    assert command_result["pointer_target"]["manifest_sha256"] == _sha256(
        built.manifest_path
    )
    assert command_result["output"] == str(output.resolve())
    assert command_result["output_sha256"] == _sha256(output)


def test_public_index_cli_never_replaces_placeholder_after_failed_verification(
    tmp_path: Path,
) -> None:
    (
        built,
        _verifier,
        public,
        signer,
        _fresh_verifier,
        fresh_public,
        fresh_signer,
    ) = _release(tmp_path)
    receipt, _receipt_digest = _receipt(built)
    receipt_path = tmp_path / "receipt.json"
    receipt_path.write_text(
        json.dumps(receipt, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    output = tmp_path / "site" / "public-bootstrap-index.json"
    output.parent.mkdir()
    placeholder = json.dumps(unpublished_public_bootstrap_index(), sort_keys=True)
    output.write_text(placeholder, encoding="utf-8")
    bad_public = bytearray(public)
    bad_public[0] ^= 1

    exit_code = run(
        [
            "build-public-bootstrap-index",
            "--release-dir",
            str(built.output_dir),
            "--publication-receipt",
            str(receipt_path),
            "--output",
            str(output),
            "--trusted-key",
            "release-key-2026=" + base64.b64encode(bytes(bad_public)).decode("ascii"),
            "--trusted-publication-key",
            "publication-key-2026=" + base64.b64encode(fresh_public).decode("ascii"),
        ],
        public_pointer_signer_factory=lambda _args: signer,
        public_freshness_signer_factory=lambda _args: fresh_signer,
    )
    assert exit_code == 1
    assert output.read_text(encoding="utf-8") == placeholder


def test_public_index_cli_stages_then_activates_the_same_signed_authority(
    tmp_path: Path,
) -> None:
    (
        built,
        verifier,
        public,
        signer,
        fresh_verifier,
        fresh_public,
        fresh_signer,
    ) = _release(tmp_path)
    receipt, receipt_digest = _receipt(built)
    receipt_path = tmp_path / "publication-receipt.json"
    receipt_path.write_text(
        json.dumps(receipt, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    index = build_public_bootstrap_index(
        manifest=built.manifest,
        manifest_bytes=built.manifest_path.read_bytes(),
        manifest_sha256=_sha256(built.manifest_path),
        publication_receipt=receipt,
        publication_receipt_sha256=receipt_digest,
        verifier=verifier,
        freshness_verifier=fresh_verifier,
        signer=signer,
        freshness_signer=fresh_signer,
    )
    index_path, index_sha256 = write_public_bootstrap_index(
        tmp_path / "public-bootstrap-index.json",
        index,
    )
    public_url = "https://download.example/stable/public-bootstrap-index.json"
    authority = index["authority"]
    freshness = index["freshness"]
    assert isinstance(authority, dict)
    assert isinstance(freshness, dict)

    class FakePublisher:
        def __init__(self) -> None:
            self.public_url = public_url
            self.staged_payload: bytes | None = None
            self.closed = 0

        def stage(self, payload: bytes) -> PublicBootstrapStageReceipt:
            self.staged_payload = payload
            return PublicBootstrapStageReceipt(
                release_id=built.manifest.release_id,
                version=built.manifest.version,
                index_sha256=hashlib.sha256(payload).hexdigest(),
                index_size_bytes=len(payload),
                public_url=public_url,
                staged_revision_id="bstage_" + "1" * 32,
                authority_sequence=int(authority["sequence"]),
                authority_revision_id=str(authority["revision"]),
                authority_target=dict(authority["target"]),
                freshness_issued_at=str(freshness["issued_at"]),
                freshness_expires_at=str(freshness["expires_at"]),
                expected_previous_activation_record_id=None,
                expected_previous_sequence=None,
                expected_previous_authority_revision_id=None,
                expected_previous_index_sha256=None,
                expected_previous_target=None,
            )

        def activate(
            self,
            payload: bytes,
            staged: PublicBootstrapStageReceipt,
        ) -> PublicBootstrapPublicationReceipt:
            assert payload == self.staged_payload
            assert staged.staged_revision_id == "bstage_" + "1" * 32
            return PublicBootstrapPublicationReceipt(
                release_id=built.manifest.release_id,
                version=built.manifest.version,
                index_sha256=hashlib.sha256(payload).hexdigest(),
                index_size_bytes=len(payload),
                public_url=public_url,
                staged_revision_id=staged.staged_revision_id,
                active_activation_record_id="bactive_" + "2" * 32,
                active_sequence=staged.authority_sequence,
                active_authority_revision_id=staged.authority_revision_id,
                active_target=staged.authority_target,
                public_object_revision_id="pobj_" + "3" * 32,
                previous_activation_record_id=None,
                previous_sequence=None,
                previous_authority_revision_id=None,
                previous_index_sha256=None,
                previous_target=None,
                readback_record_id="bread_" + "4" * 32,
                readback_proof_token=(
                    "bootstrap-index-proof:bread_" + "4" * 32 + ":sha256:" + "5" * 64
                ),
                read_back_at="2026-07-12T00:00:00+00:00",
            )

        def close(self) -> None:
            self.closed += 1

    publisher = FakePublisher()
    key = "release-key-2026=" + base64.b64encode(public).decode("ascii")
    freshness_key = "publication-key-2026=" + base64.b64encode(fresh_public).decode(
        "ascii"
    )
    stage_receipt = tmp_path / "bootstrap-index-stage-receipt.json"
    common = [
        "--release-dir",
        str(built.output_dir),
        "--publication-receipt",
        str(receipt_path),
        "--index",
        str(index_path),
        "--publication-config",
        str(tmp_path / "factory-owned-config.json"),
        "--trusted-key",
        key,
        "--trusted-publication-key",
        freshness_key,
    ]
    assert (
        run(
            [
                "stage-public-bootstrap-index",
                *common,
                "--receipt",
                str(stage_receipt),
            ],
            public_bootstrap_publisher_factory=lambda _args: publisher,
        )
        == 0
    )
    staged = json.loads(stage_receipt.read_text(encoding="utf-8"))
    assert staged["index_sha256"] == index_sha256
    assert staged["manifest_sha256"] == _sha256(built.manifest_path)

    active_receipt = tmp_path / "bootstrap-index-publication-receipt.json"
    assert (
        run(
            [
                "activate-public-bootstrap-index",
                *common,
                "--stage-receipt",
                str(stage_receipt),
                "--receipt",
                str(active_receipt),
            ],
            public_bootstrap_publisher_factory=lambda _args: publisher,
        )
        == 0
    )
    active = json.loads(active_receipt.read_text(encoding="utf-8"))
    assert active["state"] == "active-and-read-back"
    assert (
        active["stage_receipt_sha256"]
        == hashlib.sha256(stage_receipt.read_bytes()).hexdigest()
    )
    assert publisher.closed == 2


def test_public_index_atomic_write_never_exposes_invalid_or_partial_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "site" / "public-bootstrap-index.json"
    output.parent.mkdir()
    sentinel = b"existing-pointer\n"
    output.write_bytes(sentinel)

    invalid = unpublished_public_bootstrap_index()
    invalid["release"] = {"download": "https://ready.invalid/fake.exe"}
    with pytest.raises(PublicBootstrapIndexError, match="authority or release"):
        write_public_bootstrap_index(output, invalid)
    assert output.read_bytes() == sentinel

    def fail_replace(_source: object, _destination: object) -> None:
        raise OSError("simulated crash before atomic commit")

    monkeypatch.setattr("ecorex.release.public_index.os.replace", fail_replace)
    with pytest.raises(OSError, match="simulated crash"):
        write_public_bootstrap_index(output, unpublished_public_bootstrap_index())
    assert output.read_bytes() == sentinel
    assert not list(output.parent.glob(".public-bootstrap-index.json.tmp-*"))
