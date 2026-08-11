from __future__ import annotations

import hashlib
import json
from pathlib import Path
import runpy
from types import SimpleNamespace

import pytest
ROOT = Path(__file__).resolve().parents[2]
MODULE = runpy.run_path(str(ROOT / "scripts/finalize-emate-windows-signing.py"))
VERSION, COMMIT, THUMBPRINT = "2.0.1", "a" * 40, "C" * 40
INSTALLER = f"e-Mate-Setup-{VERSION}-x64.exe"
def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _inputs(root: Path) -> SimpleNamespace:
    base = root / "base"
    files = {INSTALLER: b"unsigned", "other.bin": b"untouched", "runtime/release/release-manifest.json": b"manifest"}
    for name, payload in files.items():
        path = base / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
    index = {"schema_version": 2, "distribution_mode": "unsigned-manual", "version": VERSION, "downloads": [{"target": "windows-x64", "file_name": INSTALLER, "size_bytes": 8, "sha256": _sha(base / INSTALLER)}]}
    (base / "download-index.json").write_text(json.dumps(index) + "\n")
    records = []
    for path in sorted(item for item in base.rglob("*") if item.is_file()):
        relative = path.relative_to(base).as_posix()
        records.append({"path": relative, "role": "pointer" if relative == "download-index.json" else "immutable-runtime" if relative.startswith("runtime/") else "immutable-desktop", "source_artifact": "feed-gate" if relative == "download-index.json" else "runtime" if relative.startswith("runtime/") else "windows-x64", "size_bytes": path.stat().st_size, "sha256": _sha(path)})
    base_id = hashlib.sha256(json.dumps(records, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    activation = {"strategy": "same-filesystem-current-symlink-rename", "allowed_operations": ["activate", "rollback"], "link": "/srv/e-mate-update/current", "pointer_files": ["download-index.json"], "missing_files_must_return": 404, "receipt_required_fields": ["operation", "feed_build_id", "previous_target", "new_target", "manifest_sha256", "public_readback_sha256", "completed_at"]}
    receipt = {"schema_version": 2, "document_type": "emate.desktop-feed-stage", "distribution_mode": "unsigned-manual", "status": "activation-ready-unsigned-manual", "version": VERSION, "source_commit": COMMIT, "release_id": "release", "build_digest": "d" * 64, "runtime_manifest_sha256": _sha(base / "runtime/release/release-manifest.json"), "feed_build_id": base_id, "candidate_target": f"releases/v{VERSION}-{base_id[:16]}", "nginx_config_sha256": "e" * 64, "files": records, "activation": activation}
    (base / "feed-stage-receipt.json").write_text(json.dumps(receipt) + "\n")
    signed = root / INSTALLER
    signed.write_bytes(b"authenticode-signed")
    evidence = root / "authenticode.json"
    evidence.write_text(json.dumps({"schema_version": 1, "document_type": "emate.windows-authenticode-receipt", "status": "verified", "version": VERSION, "source_commit": COMMIT, "base_feed_build_id": base_id, "file_name": INSTALLER, "unsigned_sha256": _sha(base / INSTALLER), "signed_sha256": _sha(signed), "signed_size_bytes": signed.stat().st_size, "signature_status": "Valid", "signer_certificate_thumbprint": THUMBPRINT}) + "\n")
    return SimpleNamespace(base_feed=base, signed_windows_installer=signed, authenticode_receipt=evidence, output=root / "final", expected_version=VERSION, expected_source_sha=COMMIT, expected_base_feed_build_id=base_id, expected_signer_thumbprint=THUMBPRINT)


def test_postsign_feed_replaces_only_installer_and_rebinds_inventory(tmp_path: Path) -> None:
    args = _inputs(tmp_path)
    MODULE["finalize"](args)
    assert (args.output / INSTALLER).read_bytes() == args.signed_windows_installer.read_bytes()
    assert (args.output / "other.bin").read_bytes() == b"untouched"
    index = json.loads((args.output / "download-index.json").read_text())["downloads"][0]
    assert index["sha256"] == _sha(args.signed_windows_installer)
    assert index["authenticode"] == {"status": "verified", "signer_certificate_thumbprint": THUMBPRINT}
    receipt = json.loads((args.output / "feed-stage-receipt.json").read_text())
    assert receipt["feed_build_id"] != args.expected_base_feed_build_id
    assert receipt["activation"]["pointer_files"] == ["download-index.json"]
    assert any(item["path"] == "windows-authenticode-receipt.json" for item in receipt["files"])


@pytest.mark.parametrize("failure", ["wrong-signer", "unsigned", "signed-tamper", "second-file-drift"])
def test_postsign_feed_rejects_invalid_evidence_or_drift(tmp_path: Path, failure: str) -> None:
    args = _inputs(tmp_path)
    evidence = json.loads(args.authenticode_receipt.read_text())
    if failure == "wrong-signer": evidence["signer_certificate_thumbprint"] = "D" * 40
    elif failure == "unsigned": evidence["signature_status"] = "NotSigned"
    elif failure == "signed-tamper": args.signed_windows_installer.write_bytes(b"tampered")
    else: (args.base_feed / "other.bin").write_bytes(b"drift")
    args.authenticode_receipt.write_text(json.dumps(evidence) + "\n")
    with pytest.raises(MODULE["FinalizeError"]): MODULE["finalize"](args)
    assert not args.output.exists()


def test_windows_receipt_uses_native_authenticode_verifier() -> None:
    source = (ROOT / "scripts/write-emate-windows-authenticode-receipt.ps1").read_text()
    assert "Get-AuthenticodeSignature" in source and '$signature.Status -ne "Valid"' in source
    assert "SignerCertificate.Thumbprint" in source
