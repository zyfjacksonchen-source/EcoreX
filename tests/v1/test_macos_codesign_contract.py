from __future__ import annotations

import hashlib
from pathlib import Path
import runpy
import shutil
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.skipif(sys.platform != "darwin", reason="real macOS CPython contract")
def test_official_macos_cpython_build_support_matches_prune_contract(
    tmp_path: Path,
) -> None:
    version = f"{sys.version_info.major}.{sys.version_info.minor}"
    prefix = Path(sys.base_prefix).resolve(strict=True)
    source_config = prefix / "lib" / f"python{version}" / f"config-{version}-darwin"
    source_libpython = prefix / "lib" / f"libpython{version}.dylib"
    target_stdlib = tmp_path / "lib" / f"python{version}"
    target_stdlib.mkdir(parents=True)
    stager = runpy.run_path(str(ROOT / "platform-staging" / "stager.py"))
    stager["_copy_tree"](
        source_config,
        target_stdlib / source_config.name,
        excluded=frozenset({"__pycache__"}),
    )
    stager["_copy_regular"](
        source_libpython.resolve(strict=True),
        target_stdlib.parent / source_libpython.name,
        executable=True,
    )

    stager["_prune_macos_cpython_build_support"](target_stdlib)

    assert not (target_stdlib / source_config.name).exists()


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
