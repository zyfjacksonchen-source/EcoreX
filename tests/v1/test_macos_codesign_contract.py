from __future__ import annotations

import hashlib
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


@pytest.mark.skipif(sys.platform != "darwin", reason="real macOS codesign contract")
def test_macos_ad_hoc_signature_is_deterministic_and_copy_stable(
    tmp_path: Path,
) -> None:
    source = Path(sys.executable).resolve(strict=True)
    first = tmp_path / "first" / "python3"
    second = tmp_path / "second" / "python3"
    extracted = tmp_path / "extracted" / "python3"
    for destination in (first, second):
        destination.parent.mkdir()
        shutil.copyfile(source, destination)
        destination.chmod(0o755)
        signed = subprocess.run(
            (
                "/usr/bin/codesign",
                "--force",
                "--sign",
                "-",
                "--timestamp=none",
                str(destination),
            ),
            check=False,
            capture_output=True,
            timeout=30,
        )
        if signed.returncode != 0:
            pytest.fail("macOS ad-hoc signing failed")
    assert hashlib.sha256(first.read_bytes()).digest() == hashlib.sha256(
        second.read_bytes()
    ).digest()

    extracted.parent.mkdir()
    shutil.copyfile(first, extracted)
    extracted.chmod(0o755)
    verified = subprocess.run(
        ("/usr/bin/codesign", "--verify", "--strict", str(extracted)),
        check=False,
        capture_output=True,
        timeout=30,
    )
    if verified.returncode != 0:
        pytest.fail("copied macOS ad-hoc signature did not verify")
