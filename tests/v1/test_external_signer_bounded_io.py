from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import sys
import time

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
import pytest

from ecorex.release import DigestPinnedExternalSigner, SigningError


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _signer_for_adapter(
    tmp_path: Path,
    source: str,
    *,
    timeout_seconds: float = 3,
    extra_environment: dict[str, str] | None = None,
) -> tuple[DigestPinnedExternalSigner, bytes]:
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
    adapter = tmp_path / "adapter.py"
    site_packages = next(path for path in sys.path if path.endswith("site-packages"))
    adapter.write_text(
        f"import sys\nsys.path.insert(0, {site_packages!r})\n{source}",
        encoding="utf-8",
        newline="\n",
    )
    executable = Path(sys.executable).resolve(strict=True)
    signer = DigestPinnedExternalSigner(
        key_id="bounded-io-test-key",
        public_key=public,
        executable_path=executable,
        executable_sha256=_sha256(executable),
        adapter_path=adapter.resolve(strict=True),
        adapter_sha256=_sha256(adapter),
        environment={
            **os.environ,
            "ACTIONS_ID_TOKEN_REQUEST_TOKEN": base64.b64encode(private_raw).decode(),
            **(extra_environment or {}),
        },
        timeout_seconds=timeout_seconds,
    )
    return signer, public


def test_external_signer_accepts_one_bounded_valid_signature(tmp_path: Path) -> None:
    signer, public = _signer_for_adapter(
        tmp_path,
        """\
import base64
import os
import sys
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

payload = sys.stdin.buffer.read()
seed = base64.b64decode(os.environ["ACTIONS_ID_TOKEN_REQUEST_TOKEN"], validate=True)
signature = Ed25519PrivateKey.from_private_bytes(seed).sign(payload)
sys.stdout.write(base64.b64encode(signature).decode("ascii") + "\\n")
""",
    )
    payload = b"bounded normal signing payload"

    signature = signer.sign(payload)

    Ed25519PublicKey.from_public_bytes(public).verify(signature, payload)
    assert signer.public_key_bytes == public
    assert len(signer.receipts) == 1


def test_external_signer_forwards_only_the_ssh_credential_path_selector(
    tmp_path: Path,
) -> None:
    credential = tmp_path / "server.txt"
    credential.write_text("non-secret-fixture-path", encoding="utf-8")
    signer, public = _signer_for_adapter(
        tmp_path,
        """\
import base64
import os
import sys
from pathlib import Path
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

payload = sys.stdin.buffer.read()
credential = Path(os.environ["ECOREX_SSH_SIGNER_CREDENTIAL_FILE"])
if credential.read_text(encoding="utf-8") != "non-secret-fixture-path":
    raise RuntimeError
if "ECOREX_UNREVIEWED_SECRET" in os.environ:
    raise RuntimeError
seed = base64.b64decode(os.environ["ACTIONS_ID_TOKEN_REQUEST_TOKEN"], validate=True)
signature = Ed25519PrivateKey.from_private_bytes(seed).sign(payload)
sys.stdout.write(base64.b64encode(signature).decode("ascii") + "\\n")
""",
        extra_environment={
            "ECOREX_SSH_SIGNER_CREDENTIAL_FILE": str(credential),
            "ECOREX_UNREVIEWED_SECRET": "must-not-cross",
        },
    )

    payload = b"publication signer environment payload"
    signature = signer.sign(payload)

    Ed25519PublicKey.from_public_bytes(public).verify(signature, payload)
    assert len(signer.receipts) == 1


def test_external_signer_terminates_infinite_stdout_at_256_bytes(
    tmp_path: Path,
) -> None:
    signer, _public = _signer_for_adapter(
        tmp_path,
        """\
import os
import sys

sys.stdin.buffer.read()
while True:
    os.write(1, b"x" * 4096)
""",
    )

    with pytest.raises(SigningError, match="safe output limit"):
        signer.sign(b"payload")
    assert signer.receipts == ()


def test_external_signer_terminates_large_finite_stdout(tmp_path: Path) -> None:
    signer, _public = _signer_for_adapter(
        tmp_path,
        """\
import sys

sys.stdin.buffer.read()
sys.stdout.buffer.write(b"x" * (1024 * 1024))
sys.stdout.buffer.flush()
""",
    )

    with pytest.raises(SigningError, match="safe output limit"):
        signer.sign(b"payload")
    assert signer.receipts == ()


def test_external_signer_bounds_and_never_discloses_stderr(tmp_path: Path) -> None:
    secret = "stderr-secret-marker"
    signer, _public = _signer_for_adapter(
        tmp_path,
        f"""\
import sys

sys.stdin.buffer.read()
sys.stderr.write({secret!r} * 10000)
sys.stderr.flush()
""",
    )

    with pytest.raises(SigningError, match="safe output limit") as captured:
        signer.sign(b"payload")
    assert secret not in str(captured.value)
    assert captured.value.__cause__ is None
    assert signer.receipts == ()


def test_external_signer_timeout_terminates_descendant_process(tmp_path: Path) -> None:
    child_started = tmp_path / "child-started"
    child_escaped = tmp_path / "child-escaped"
    child_source = (
        "from pathlib import Path; import time; "
        f"Path({str(child_started)!r}).write_text('started'); "
        "time.sleep(1.5); "
        f"Path({str(child_escaped)!r}).write_text('escaped')"
    )
    adapter_source = f"""\
import subprocess
import sys
import time
from pathlib import Path

subprocess.Popen([sys.executable, "-c", {json.dumps(child_source)}])
deadline = time.monotonic() + 0.75
while not Path({str(child_started)!r}).exists() and time.monotonic() < deadline:
    time.sleep(0.01)
time.sleep(30)
"""
    signer, _public = _signer_for_adapter(
        tmp_path,
        adapter_source,
        timeout_seconds=1,
    )

    with pytest.raises(SigningError, match="timed out safely"):
        signer.sign(b"payload")

    assert child_started.exists(), "fixture must prove that the descendant started"
    time.sleep(0.8)
    assert not child_escaped.exists(), "the descendant escaped process-tree termination"
    assert signer.receipts == ()
