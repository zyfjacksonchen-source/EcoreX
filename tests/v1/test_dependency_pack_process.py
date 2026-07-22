from __future__ import annotations

import hashlib
import json
from pathlib import Path
import stat
import sys
from typing import Mapping
import zipfile

import pytest

from ecorex.capabilities import (
    CapabilityPackManifest,
    PackServiceBinding,
    VerifiedCapabilityPack,
)
from ecorex.capabilities.pack_services import builtin_pack_service_specs
from ecorex.integration.dependency_pack_process import (
    DependencyPackProcessError,
    PackOCRServiceAdapter,
    VerifiedDependencyPackProcessAdapter,
)
from ecorex.integration.pack_python import PackPythonIdentity
from ecorex.update import SignatureEnvelope


def _verified_dependency_pack(
    tmp_path: Path, pack_id: str, files: Mapping[str, bytes]
) -> VerifiedCapabilityPack:
    files = dict(files)
    files["ecorex-dependency-pack.json"] = json.dumps(
        {
            "schema_version": 1,
            "kind": "dependency-service",
            "pack_id": pack_id,
            "adapter": (
                "python-rapidocr-runtime-v1"
                if pack_id == "ocr"
                else "python-office-formats-v1"
            ),
            "runtime_api_version": "1.0.0",
            "inventory": "runtime-inventory.json",
            "services": ["ocr.extract" if pack_id == "ocr" else "office.formats"],
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    records = [
        {
            "path": name,
            "size_bytes": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
            "mode": 0o644,
        }
        for name, payload in sorted(files.items(), key=lambda item: item[0].casefold())
    ]
    inventory = json.dumps(
        {
            "schema_version": 1,
            "pack_id": pack_id,
            "distributions": [],
            "payload_sha256": hashlib.sha256(
                json.dumps(records, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest(),
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    artifact = tmp_path / f"{pack_id}.zip"
    with zipfile.ZipFile(artifact, "w") as archive:
        for name, payload in {**files, "runtime-inventory.json": inventory}.items():
            info = zipfile.ZipInfo(name)
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | 0o644) << 16
            archive.writestr(info, payload)
    service = builtin_pack_service_specs()[
        "ocr.extract" if pack_id == "ocr" else "office.formats"
    ]
    manifest = CapabilityPackManifest(
        schema_version=2,
        pack_id=pack_id,
        version="1.0.0",
        runtime_api_version="1.0.0",
        platform="windows",
        architecture="x64",
        artifact_file_name=artifact.name,
        artifact_size_bytes=artifact.stat().st_size,
        artifact_sha256=hashlib.sha256(artifact.read_bytes()).hexdigest(),
        tools=(),
        services=(
            PackServiceBinding(
                service_id=service.service_id,
                service_version=service.version,
                contract_sha256=service.contract_sha256,
            ),
        ),
        signature=SignatureEnvelope(algorithm="ed25519", key_id="test", value="AA=="),
    )
    return VerifiedCapabilityPack._verified(manifest, artifact)


def _identity() -> PackPythonIdentity:
    return PackPythonIdentity(
        platform="windows",
        architecture="x64",
        relative_path="bin/pack-python/python.exe",
        size_bytes=1,
        sha256="0" * 64,
        closure_file_count=1,
        closure_size_bytes=1,
        closure_sha256="0" * 64,
    )


def test_ocr_executes_only_from_verified_installed_pack_snapshot(
    tmp_path: Path,
) -> None:
    files = {
        "ecorex-dependency-pack.json": b"{}",
        "runtime/python/numpy.py": b"def asarray(value): return value\n",
        "runtime/python/PIL/__init__.py": b"from . import Image, ImageOps\n",
        "runtime/python/PIL/Image.py": (
            b"class Opened:\n"
            b" def __enter__(self): return self\n"
            b" def __exit__(self,*args): return False\n"
            b" def convert(self,mode): return self\n"
            b"def open(stream): return Opened()\n"
        ),
        "runtime/python/PIL/ImageOps.py": b"def exif_transpose(value): return value\n",
        "runtime/python/rapidocr_onnxruntime/__init__.py": (
            b"class RapidOCR:\n"
            b" def __call__(self,pixels):\n"
            b"  return ([[None, 'ECOREX OCR 4827', 0.99]], 0.01)\n"
        ),
    }
    verified = _verified_dependency_pack(tmp_path, "ocr", files)
    process = VerifiedDependencyPackProcessAdapter(
        verified,
        python_executable=Path(sys.executable),
        python_identity=_identity(),
    )
    numpy_before = sys.modules.get("numpy")
    try:
        result = PackOCRServiceAdapter(process).extract(
            b"bounded-image", timeout_seconds=8.0
        )
        assert result["provider"] == "rapidocr_onnxruntime"
        assert "4827" in result["text"]
        assert sys.modules.get("numpy") is numpy_before
        assert "rapidocr_onnxruntime" not in sys.modules
    finally:
        process.close()


def test_office_native_dependency_service_is_executable_without_sys_path_pollution(
    tmp_path: Path,
) -> None:
    files = {"ecorex-dependency-pack.json": b"{}"}
    for module in ("docx", "openpyxl", "pptx", "pypdf", "reportlab"):
        files[f"runtime/python/{module}/__init__.py"] = b"PACK_RUNTIME = True\n"
    verified = _verified_dependency_pack(tmp_path, "office", files)
    process = VerifiedDependencyPackProcessAdapter(
        verified,
        python_executable=Path(sys.executable),
        python_identity=_identity(),
    )
    try:
        result = process.invoke("probe", {}, timeout_seconds=8.0)
        assert result == {
            "provider": "python-office-formats-v1",
            "modules": ["docx", "openpyxl", "pptx", "pypdf", "reportlab"],
        }
    finally:
        process.close()


def test_dependency_pack_snapshot_mutation_fails_closed(tmp_path: Path) -> None:
    files = {
        "ecorex-dependency-pack.json": b"{}",
        "runtime/python/docx/__init__.py": b"PACK_RUNTIME = True\n",
        "runtime/python/openpyxl/__init__.py": b"PACK_RUNTIME = True\n",
        "runtime/python/pptx/__init__.py": b"PACK_RUNTIME = True\n",
        "runtime/python/pypdf/__init__.py": b"PACK_RUNTIME = True\n",
        "runtime/python/reportlab/__init__.py": b"PACK_RUNTIME = True\n",
    }
    process = VerifiedDependencyPackProcessAdapter(
        _verified_dependency_pack(tmp_path, "office", files),
        python_executable=Path(sys.executable),
        python_identity=_identity(),
    )
    try:
        # Snapshot materialization is intentionally lazy so large OCR/Office
        # packs cannot delay the Runtime listener during application
        # composition.  Establish the verified snapshot before mutating it.
        assert process.invoke("probe", {}, timeout_seconds=8.0)["provider"] == (
            "python-office-formats-v1"
        )
        target = process._root / "runtime" / "python" / "docx" / "__init__.py"
        target.write_text("tampered = True\n", encoding="utf-8")
        with pytest.raises(
            DependencyPackProcessError, match="dependency_pack_snapshot_changed"
        ):
            process.invoke("probe", {}, timeout_seconds=8.0)
    finally:
        process.close()


def test_dependency_pack_materialization_is_deferred_until_first_invocation(
    tmp_path: Path,
) -> None:
    verified = _verified_dependency_pack(
        tmp_path,
        "office",
        {"ecorex-dependency-pack.json": b"{}"},
    )
    process = VerifiedDependencyPackProcessAdapter(
        verified,
        python_executable=Path(sys.executable),
        python_identity=_identity(),
    )
    try:
        assert process._expected_files is None
        assert not process._root.exists()
    finally:
        process.close()
