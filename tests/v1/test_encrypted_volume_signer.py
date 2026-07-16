from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
import pytest

from ecorex.security.encrypted_volume_signer import (
    EncryptedVolumeSignerError,
    ROLES,
    describe_keyring,
    initialize_keyring,
    public_key_description,
    sign_for_role,
)


ATTESTATION = hashlib.sha256(b"encrypted-volume-proof").hexdigest()


@pytest.mark.skipif(os.name == "nt", reason="POSIX ownership/mode contract")
def test_keyring_is_independent_public_only_and_attestation_bound(tmp_path: Path) -> None:
    root = tmp_path / "keys"
    root.mkdir(mode=0o700)
    value = initialize_keyring(root, attestation_sha256=ATTESTATION)
    assert value["status"] == "ready"
    public_values = []
    for role in ROLES:
        public = base64.b64decode(value[role]["public_key_base64"], validate=True)
        public_values.append(public)
        payload = f"payload:{role}".encode()
        signature = sign_for_role(
            role,
            payload,
            root=root,
            expected_attestation_sha256=ATTESTATION,
        )
        Ed25519PublicKey.from_public_bytes(public).verify(signature, payload)
        assert "private" not in json.dumps(value[role], sort_keys=True).casefold()
        description = public_key_description(root, role=role)
        assert set(description) == {
            "schema_version",
            "role",
            "algorithm",
            "key_id",
            "public_key_base64",
            "public_key_sha256",
        }
    assert len(set(public_values)) == len(ROLES)
    assert describe_keyring(root) == value
    with pytest.raises(EncryptedVolumeSignerError, match="attestation_mismatch"):
        sign_for_role(
            "publication",
            b"payload",
            root=root,
            expected_attestation_sha256="0" * 64,
        )


@pytest.mark.skipif(os.name == "nt", reason="POSIX ownership/mode contract")
def test_signer_rejects_permissions_and_document_drift(tmp_path: Path) -> None:
    root = tmp_path / "keys"
    root.mkdir(mode=0o700)
    initialize_keyring(root, attestation_sha256=ATTESTATION)
    document = root / "publication.json"
    document.chmod(0o644)
    with pytest.raises(EncryptedVolumeSignerError, match="document_invalid"):
        sign_for_role(
            "publication",
            b"payload",
            root=root,
            expected_attestation_sha256=ATTESTATION,
        )
