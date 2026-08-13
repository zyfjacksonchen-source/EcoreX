from __future__ import annotations

import base64
import hashlib
from io import BytesIO
import json
from pathlib import Path
import stat
import subprocess
import sys
from typing import Mapping
import venv
import zipfile
import zlib

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
    PackOfficeServiceAdapter,
    VerifiedDependencyPackProcessAdapter,
)
from ecorex.integration.dependency_pack_worker import _verify_ooxml_archive
from ecorex.integration.pack_python import PackPythonIdentity
from ecorex.release.process_boundary import BoundedProcessResult
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


def _compressed_multi_stream_pdf() -> bytes:
    objects: list[bytes] = [b"", b""]
    page_ids: list[int] = []
    compressed = zlib.compress(b" " * (2 * 1024 * 1024), level=9)
    for _ in range(8):
        page_id = len(objects) + 1
        stream_id = page_id + 1
        page_ids.append(page_id)
        objects.extend(
            (
                (
                    f"<< /Type /Page /Parent 2 0 R /Resources <<>> "
                    f"/MediaBox [0 0 100 100] /Contents {stream_id} 0 R >>"
                ).encode(),
                (
                    f"<< /Length {len(compressed)} /Filter /FlateDecode >>\nstream\n"
                ).encode()
                + compressed
                + b"\nendstream",
            )
        )
    objects[0] = b"<< /Type /Catalog /Pages 2 0 R >>"
    objects[1] = (
        f"<< /Type /Pages /Count {len(page_ids)} /Kids ["
        + " ".join(f"{value} 0 R" for value in page_ids)
        + "] >>"
    ).encode()
    output = bytearray(b"%PDF-1.7\n")
    offsets = [0]
    for object_id, value in enumerate(objects, start=1):
        offsets.append(len(output))
        output.extend(f"{object_id} 0 obj\n".encode())
        output.extend(value)
        output.extend(b"\nendobj\n")
    xref = len(output)
    output.extend(f"xref\n0 {len(objects) + 1}\n".encode())
    output.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        output.extend(f"{offset:010d} 00000 n \n".encode())
    output.extend(
        (
            f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref}\n%%EOF\n"
        ).encode()
    )
    return bytes(output)


def _large_ooxml_document() -> bytes:
    output = BytesIO()
    document = (
        b'<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        b"<w:body>"
        + b"<w:p><w:r><w:t>x</w:t></w:r></w:p>" * 800_000
        + b"</w:body></w:document>"
    )
    with zipfile.ZipFile(
        output,
        "w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=9,
    ) as archive:
        archive.writestr(
            "[Content_Types].xml",
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>',
        )
        archive.writestr("word/document.xml", document)
    return output.getvalue()


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
    rapidocr_before = sys.modules.get("rapidocr_onnxruntime")
    try:
        result = PackOCRServiceAdapter(process).extract(
            b"bounded-image", timeout_seconds=8.0
        )
        assert result["provider"] == "rapidocr_onnxruntime"
        assert "4827" in result["text"]
        assert sys.modules.get("numpy") is numpy_before
        assert sys.modules.get("rapidocr_onnxruntime") is rapidocr_before
    finally:
        process.close()


def test_ocr_cold_worker_gets_one_bounded_attempt_beyond_30_seconds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = VerifiedDependencyPackProcessAdapter(
        _verified_dependency_pack(
            tmp_path,
            "ocr",
            {"ecorex-dependency-pack.json": b"{}"},
        ),
        python_executable=Path(sys.executable),
        python_identity=_identity(),
    )
    process._expected_files = {}
    process._root.mkdir()
    worker = tmp_path / "worker.py"
    worker.write_text("", encoding="utf-8")
    monkeypatch.setattr(process, "_verify_snapshot", lambda: None)
    monkeypatch.setattr(process, "_verify_artifact", lambda: None)
    monkeypatch.setattr(process, "_verified_worker", lambda: worker)
    calls: list[float] = []

    def invoke_once(*_args, **kwargs):
        calls.append(kwargs["timeout_seconds"])
        assert 31.9 < kwargs["timeout_seconds"] <= 38.0
        return BoundedProcessResult(
            returncode=0,
            stdout=json.dumps(
                {
                    "schema_version": 1,
                    "pack_id": "ocr",
                    "status": "success",
                    "result": {"provider": "rapidocr_onnxruntime", "text": "OCR"},
                }
            ).encode(),
            stderr=b"",
        )

    monkeypatch.setattr(
        "ecorex.release.process_boundary.run_bounded_process",
        invoke_once,
    )
    try:
        result = PackOCRServiceAdapter(process).extract(
            b"cold-image",
            timeout_seconds=2.0,
        )
        assert result["text"] == "OCR"
        assert calls == [38.0]
    finally:
        process.close()


def test_public_cow_ocr_uses_the_verified_pack_and_keeps_pack_errors_failed(
    tmp_path: Path,
) -> None:
    from agent.tools.ocr.ocr import OcrTool, bind_ocr_pack_service
    from ecorex.runtime import RuntimeSettings, create_app

    class Service:
        service_id = "ocr.extract"

        def __init__(self) -> None:
            self.error = False

        def extract(self, content: bytes, *, timeout_seconds: float):
            assert content == b"public-cow-image"
            assert timeout_seconds == 8
            if self.error:
                raise RuntimeError("pack internal error")
            return {
                "status": "success",
                "provider": "rapidocr_onnxruntime",
                "text": "TEST1\nTEST2",
                "latencyMs": 1,
                "cacheHit": False,
            }

    service = Service()
    create_app(
        settings=RuntimeSettings(
            database_path=tmp_path / "runtime.db",
            artifact_root=tmp_path / "artifacts",
            capability_pack_services={"ocr.extract": service},
            close_capability_pack_services_on_shutdown=False,
        )
    )
    image = "data:image/png;base64," + base64.b64encode(
        b"public-cow-image"
    ).decode("ascii")
    try:
        result = OcrTool().execute(
            {"action": "extract_text", "image": image, "timeout": 8}
        )
        assert result.status == "success"
        assert result.result["text"] == "TEST1\nTEST2"
        assert result.result["ocr"]["provider"] == "rapidocr_onnxruntime"

        service.error = True
        failed = OcrTool().execute(
            {"action": "extract_text", "image": image, "timeout": 8}
        )
        assert failed.status == "error"
        assert failed.result["ocr"]["status"] == "error"
        assert "pack internal error" not in str(failed.result)
    finally:
        bind_ocr_pack_service(None)


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


def test_dependency_worker_does_not_require_core_installed_in_pack_python(
    tmp_path: Path,
) -> None:
    environment = tmp_path / "isolated-python"
    venv.EnvBuilder(with_pip=False, symlinks=sys.platform != "win32").create(
        environment
    )
    interpreter = environment / (
        "Scripts/python.exe" if sys.platform == "win32" else "bin/python"
    )
    probe = subprocess.run(
        (
            str(interpreter),
            "-I",
            "-c",
            "import importlib.util; print(importlib.util.find_spec('ecorex'))",
        ),
        cwd=tmp_path,
        check=True,
        stdout=subprocess.PIPE,
        text=True,
    )
    assert probe.stdout.strip() == "None"

    files = {"ecorex-dependency-pack.json": b"{}"}
    for module in ("docx", "openpyxl", "pptx", "pypdf", "reportlab"):
        files[f"runtime/python/{module}/__init__.py"] = b"PACK_RUNTIME = True\n"
    process = VerifiedDependencyPackProcessAdapter(
        _verified_dependency_pack(tmp_path, "office", files),
        python_executable=interpreter,
        python_identity=_identity(),
    )
    try:
        assert process.invoke("probe", {}, timeout_seconds=8.0)["provider"] == (
            "python-office-formats-v1"
        )
    finally:
        process.close()


def test_office_service_adapter_uses_the_existing_verified_process_contract() -> None:
    class Process:
        call = None

        def invoke(self, operation, payload, *, timeout_seconds):
            self.call = (operation, payload, timeout_seconds)
            return {"provider": "office"}

    process = Process()
    adapter = PackOfficeServiceAdapter(process)

    result = adapter.probe(timeout_seconds=9.0)
    assert result == {"provider": "office"}
    assert process.call == ("probe", {}, 9.0)

    result = adapter.create(
        "document",
        {"title": "Release notes", "sections": []},
        timeout_seconds=12.0,
    )

    assert result == {"provider": "office"}
    assert process.call == (
        "create",
        {"family": "document", "title": "Release notes", "sections": []},
        12.0,
    )

    result = adapter.read(
        "document",
        b"bounded-docx",
        timeout_seconds=10.0,
    )

    assert result == {"provider": "office"}
    assert process.call == (
        "read",
        {
            "family": "document",
            "content_base64": "Ym91bmRlZC1kb2N4",
        },
        10.0,
    )

    result = adapter.edit(
        "document",
        b"bounded-docx",
        {"title": "Revised", "sections": []},
        timeout_seconds=11.0,
    )

    assert result == {"provider": "office"}
    assert process.call == (
        "edit",
        {
            "family": "document",
            "content_base64": "Ym91bmRlZC1kb2N4",
            "title": "Revised",
            "sections": [],
        },
        11.0,
    )


def test_office_reader_rejects_path_confused_ooxml_archives() -> None:
    payload = BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        archive.writestr("../document.xml", "<document/>")

    with pytest.raises(ValueError, match="OOXML archive member is invalid"):
        _verify_ooxml_archive(payload.getvalue())


def test_office_reader_memory_limit_rejects_parser_exhaustion_and_parent_survives(
    tmp_path: Path,
) -> None:
    bomb = (
        b"class Bomb:\n"
        b" def __init__(self,*args,**kwargs):\n"
        b"  self.payload=bytearray(600*1024*1024)\n"
    )
    files = {
        "ecorex-dependency-pack.json": b"{}",
        "runtime/python/docx/__init__.py": bomb + b"Document=Bomb\n",
        "runtime/python/pypdf/__init__.py": bomb + b"PdfReader=Bomb\n",
        "runtime/python/openpyxl/__init__.py": b"PACK_RUNTIME=True\n",
        "runtime/python/pptx/__init__.py": b"PACK_RUNTIME=True\n",
        "runtime/python/reportlab/__init__.py": b"PACK_RUNTIME=True\n",
    }
    process = VerifiedDependencyPackProcessAdapter(
        _verified_dependency_pack(tmp_path, "office", files),
        python_executable=Path(sys.executable),
        python_identity=_identity(),
    )
    service = PackOfficeServiceAdapter(process)
    try:
        for family, content in (
            ("pdf", _compressed_multi_stream_pdf()),
            ("document", _large_ooxml_document()),
        ):
            assert len(content) < 5 * 1024 * 1024
            with pytest.raises(
                DependencyPackProcessError,
                match="dependency_pack_process_rejected",
            ):
                service.read(family, content, timeout_seconds=8.0)
            assert process.invoke("probe", {}, timeout_seconds=8.0)["provider"] == (
                "python-office-formats-v1"
            )
            assert bytearray(1024 * 1024) == bytes(1024 * 1024)
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
