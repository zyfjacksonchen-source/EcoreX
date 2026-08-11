from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import re
import runpy
import subprocess
import sys
import zipfile

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
import pytest
from jsonschema import Draft202012Validator

from ecorex._version import __version__
from ecorex.release import (
    DigestPinnedExternalSigner,
    candidate_receipt_signing_payload,
)
from ecorex.integration.pack_python import build_pack_python_manifest
from ecorex.control_plane.repository import (
    REQUIRED_RELEASE_GATES,
    required_release_gates,
)
from ecorex.release.candidate import (
    CandidateBuildError,
    PACK_SERVICES,
    PACK_TOOLS,
    STAGE_GATES,
    build_candidate,
    write_stage_receipt,
)
from ecorex.release.macos_native_contract import PYTHON_MACOS_DISTRIBUTION
from ecorex.update import (
    Ed25519SignatureVerifier,
    ReleaseArtifact,
    ReleaseChannel,
    ReleaseManifest,
    ReleaseSource,
    SignatureEnvelope,
    SourceKind,
    verify_artifact_file,
    verify_manifest_signature,
)


COMMIT = "a" * 40
RUN_ID = 123456
STAGER_SHA256 = hashlib.sha256(b"pinned-platform-stager").hexdigest()
PRODUCT_VERSION = __version__


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_supply_chain_preflight_licenses_every_runtime_lock_package(
    tmp_path: Path,
) -> None:
    repository = Path(__file__).resolve().parents[2]
    report_path = tmp_path / "supply-chain-preflight.json"
    result = subprocess.run(
        [
            sys.executable,
            str(repository / "scripts/check-v1-candidate-supply-chain.py"),
            "preflight",
            "--repo",
            str(repository),
            "--report",
            str(report_path),
        ],
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr.decode(errors="replace")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    runtime_count = report["gates"]["dependency-lock"]["runtime_packages"]
    licenses = report["gates"]["license"]["python_packages"]
    assert runtime_count > 0
    assert len(licenses) == runtime_count
    assert {
        (item["name"].casefold(), item["version"], item["license"])
        for item in licenses
        if item["name"].casefold() == "tzdata"
    } == {("tzdata", "2026.2", "Apache-2.0")}


def test_supply_chain_uses_reviewed_license_for_inactive_marker_dependency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = Path(__file__).resolve().parents[2]
    module = runpy.run_path(
        str(repository / "scripts/check-v1-candidate-supply-chain.py")
    )
    metadata_module = module["importlib_metadata"]

    def missing(_name: str):
        raise metadata_module.PackageNotFoundError

    monkeypatch.setattr(metadata_module, "metadata", missing)
    python, _node = module["_license_inventory"](
        repository,
        {"colorama": "0.4.6"},
    )

    assert python == [
        {"name": "colorama", "version": "0.4.6", "license": "BSD-3-Clause"}
    ]
    with pytest.raises(ValueError, match="license_package_missing:fastapi"):
        module["_license_inventory"](
            repository,
            {"fastapi": "0.120.4"},
        )


def test_candidate_archive_uses_shared_text_and_private_key_secret_policy(
    tmp_path: Path,
) -> None:
    repository = Path(__file__).resolve().parents[2]
    module = runpy.run_path(
        str(repository / "scripts/check-v1-candidate-supply-chain.py")
    )
    archive_path = tmp_path / "candidate.zip"
    token_like = b"ghp_abcdefghijklmnopqrstuvwxyz123456"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("runtime/native-member", b"\xcf\xfa\xed\xfe\x00" + token_like)
        archive.writestr("runtime/config.py", b"value = 'ordinary'\n")
    module["_scan_archive"](archive_path)

    private_key = (
        b"-----BEGIN OPENSSH PRIVATE KEY-----\n"
        b"QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVo=\n"
        b"-----END OPENSSH PRIVATE KEY-----\n"
    )
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("runtime/native-member", b"\xcf\xfa\xed\xfe\x00" + private_key)
    module["_scan_archive"](archive_path)

    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("runtime/credential.pem", b"\xff\x00" + private_key)
    with pytest.raises(ValueError, match="candidate_archive_secret_match"):
        module["_scan_archive"](archive_path)


def test_supply_chain_rejects_bootstrap_without_signed_minimum_stable(
    tmp_path: Path,
) -> None:
    repository = Path(__file__).resolve().parents[2]
    module = runpy.run_path(
        str(repository / "scripts/check-v1-candidate-supply-chain.py")
    )
    archive_path = tmp_path / "bootstrap.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr(
            "bootstrap-config.json",
            json.dumps(
                {
                    "schema_version": 1,
                    "public_index_url": "https://example.test/index.json",
                    "release_public_keys": {},
                    "publication_public_keys": {},
                    "sandbox_helper_sha256": "0" * 64,
                    "minimum_stable": None,
                }
            ),
        )

    with pytest.raises(
        ValueError,
        match="candidate_bootstrap_minimum_stable_invalid",
    ):
        module["_verify_bootstrap_minimum_stable"](archive_path, __version__)


def _external_signer(tmp_path: Path) -> tuple[DigestPinnedExternalSigner, bytes, bytes]:
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
    adapter = tmp_path / "kms_adapter.py"
    site_packages = next(path for path in sys.path if path.endswith("site-packages"))
    adapter.write_text(
        f"""\
import base64
import os
import sys
sys.path.insert(0, {site_packages!r})
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

if len(sys.argv) != 1:
    raise SystemExit(90)
payload = sys.stdin.buffer.read()
seed = base64.b64decode(os.environ["ACTIONS_ID_TOKEN_REQUEST_TOKEN"], validate=True)
signature = Ed25519PrivateKey.from_private_bytes(seed).sign(payload)
sys.stdout.write(base64.b64encode(signature).decode("ascii") + "\\n")
""",
        encoding="utf-8",
        newline="\n",
    )
    executable = Path(sys.executable).resolve(strict=True)
    signer = DigestPinnedExternalSigner(
        key_id="candidate-test-key",
        public_key=public,
        executable_path=executable,
        executable_sha256=_sha(executable),
        adapter_path=adapter.resolve(),
        adapter_sha256=_sha(adapter),
        environment={
            **os.environ,
            "ACTIONS_ID_TOKEN_REQUEST_TOKEN": base64.b64encode(private_raw).decode(),
        },
    )
    return signer, public, private_raw


def _gate_evidence(kind: str) -> dict[str, str]:
    return {
        gate: hashlib.sha256(f"{kind}:{gate}".encode()).hexdigest()
        for gate in STAGE_GATES[kind]
    }


def _runtime_config(platform: str, architecture: str, public: bytes) -> bytes:
    encoded = base64.b64encode(public).decode("ascii")
    rollback_encoded = base64.b64encode(
        hashlib.sha256(b"candidate-rollback-key\0" + public).digest()
    ).decode("ascii")
    value = {
        "schema_version": 1,
        "identity": {
            "version": PRODUCT_VERSION,
            "platform": platform,
            "architecture": architecture,
        },
        "paths": {
            "database": "state/runtime.sqlite3",
            "web_root": "web",
            "web_manifest": "web-manifest.json",
            "workspace_roots": ["workspace"],
        },
        "release_public_keys": {"candidate-test-key": encoded},
        "rollback_public_keys": {"candidate-rollback-key": rollback_encoded},
        "session_public_keys": {"session-test-key": encoded},
        "gateway": {
            "endpoint": "https://gateway.example/v1/responses",
            "allowed_hosts": ["gateway.example"],
        },
        "device_authorization": {
            "base_url": "https://identity.example",
            "allowed_hosts": ["identity.example"],
            "client_id": "ecorex-product",
            "timeout_seconds": 20,
            "supervisor_poll_seconds": 1,
        },
        "update": {
            "release_feed_endpoint": "https://control.example/api/v1/releases/latest",
            "signal_endpoint": "wss://control.example/api/v1/client/updates/ws",
            "control_plane_hosts": ["control.example"],
            "artifact_hosts": [
                "cdn.example",
                "github.com",
                "mirror.example",
            ],
            "channel": "canary",
            "poll_interval_seconds": 300,
        },
        "share": None,
        "image_orchestration": None,
        "audit": None,
        "tracing": None,
        "connectors": None,
        "capability_packs": [
            {
                "pack_id": pack_id,
                "manifest": (
                    f"capability-packs/{pack_id}/ecorex-capability-pack-{pack_id}-"
                    f"{platform}-{architecture}-{PRODUCT_VERSION}.json"
                ),
                "artifact": (
                    f"capability-packs/{pack_id}/ecorex-capability-pack-{pack_id}-"
                    f"{platform}-{architecture}-{PRODUCT_VERSION}.zip"
                ),
            }
            for pack_id in PACK_TOOLS
        ],
    }
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _web_dist(path: Path) -> Path:
    content = b"document.body.dataset.ecorex='ready';\n"
    digest = hashlib.sha256(content).hexdigest()
    asset = path / "assets" / f"app.{digest[:12]}.js"
    asset.parent.mkdir(parents=True)
    asset.write_bytes(content)
    (path / "index.html").write_text(
        "<!doctype html><html><head><!--__ECOREX_RUNTIME_CONFIG__-->"
        f'<script type="module" src="/assets/{asset.name}"></script>'
        "</head><body><div id=\"root\"></div></body></html>",
        encoding="utf-8",
        newline="\n",
    )
    return path


def _stages(root: Path, public: bytes) -> list[dict[str, str]]:
    inputs: list[dict[str, str]] = []
    for platform, architecture in (
        ("windows", "x64"),
        ("macos", "arm64"),
        ("macos", "x64"),
    ):
        target = f"{platform}-{architecture}"
        core = root / "stages" / target / "core"
        launcher = core / "bin" / ("ecorex.exe" if platform == "windows" else "ecorex")
        launcher.parent.mkdir(parents=True)
        launcher.write_bytes(b"real packaged runtime binary fixture")
        if platform == "windows":
            (core / "bin" / "ecorex-sandbox-host.exe").write_bytes(
                b"real AppContainer helper binary fixture"
            )
        pack_python = (
            core / "bin" / "pack-python" / "python.exe"
            if platform == "windows"
            else core / "bin" / "pack-python" / "bin" / "python3"
        )
        pack_python.parent.mkdir(parents=True)
        pack_python.write_bytes(b"real relocatable pack interpreter fixture")
        pack_python.chmod(0o755)
        if platform == "macos":
            (core / "bin" / "pack-python" / "native-components.json").write_text(
                json.dumps(
                    {
                        "architecture": architecture,
                        "components": [],
                        "distribution": dict(PYTHON_MACOS_DISTRIBUTION),
                        "license_notice": None,
                        "license_texts": [],
                        "platform": "macos",
                        "schema_version": 1,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n",
                encoding="utf-8",
                newline="\n",
            )
        (core / "pack-python.json").write_bytes(
            build_pack_python_manifest(
                core,
                platform=platform,
                architecture=architecture,
            )
        )
        (core / "runtime-config.json").write_bytes(
            _runtime_config(platform, architecture, public)
        )
        receipt = root / "receipts" / target / "core.json"
        write_stage_receipt(
            source_dir=core,
            destination=receipt,
            stage_id=f"core-{target}",
            commit_sha=COMMIT,
            workflow_run_id=RUN_ID,
            producer_executable_sha256=STAGER_SHA256,
            producer_adapter_sha256=None,
            kind="core",
            platform=platform,
            architecture=architecture,
            pack_id=None,
            gate_evidence=_gate_evidence("core"),
        )
        inputs.append(
            {
                "source_dir": f"stages/{target}/core",
                "receipt": f"receipts/{target}/core.json",
            }
        )
        bootstrap = root / "stages" / target / "bootstrap"
        bootstrap_launcher = bootstrap / "bin" / (
            "ecorex-bootstrap.exe"
            if platform == "windows"
            else "ecorex-bootstrap"
        )
        bootstrap_launcher.parent.mkdir(parents=True)
        bootstrap_launcher.write_bytes(b"real dependency-free Bootstrap fixture")
        bootstrap_launcher.chmod(0o755)
        installer = bootstrap / (
            "EcoreX Installer.cmd"
            if platform == "windows"
            else "EcoreX Installer.command"
        )
        installer.write_bytes(
            (
                b"@echo off\r\n"
                b"\"%~dp0bin\\ecorex-bootstrap.exe\" %*\r\n"
                b"exit /b %errorlevel%\r\n"
            )
            if platform == "windows"
            else (
                b"#!/bin/sh\n"
                b"BASE_DIR=$(CDPATH= cd -- \"$(dirname -- \"$0\")\" && pwd)\n"
                b"exec \"$BASE_DIR/bin/ecorex-bootstrap\" \"$@\"\n"
            )
        )
        if platform == "macos":
            installer.chmod(0o755)
        sandbox_helper_sha256 = ""
        if platform == "windows":
            helper = bootstrap / "bin" / "ecorex-sandbox-host.exe"
            helper.write_bytes(b"real signed Windows sandbox helper fixture")
            sandbox_helper_sha256 = hashlib.sha256(helper.read_bytes()).hexdigest()
        (bootstrap / "bootstrap-config.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "public_index_url": (
                        "https://download.example/public-bootstrap-index.json"
                    ),
                    "sandbox_helper_sha256": sandbox_helper_sha256,
                    "minimum_stable": None,
                    "release_public_keys": {
                        "candidate-test-key": base64.b64encode(public).decode("ascii")
                    },
                    "publication_public_keys": {
                        "publication-test-key": base64.b64encode(
                            hashlib.sha256(b"publication\0" + public).digest()
                        ).decode("ascii")
                    },
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )
        bootstrap_receipt = root / "receipts" / target / "bootstrap.json"
        write_stage_receipt(
            source_dir=bootstrap,
            destination=bootstrap_receipt,
            stage_id=f"bootstrap-{target}",
            commit_sha=COMMIT,
            workflow_run_id=RUN_ID,
            producer_executable_sha256=STAGER_SHA256,
            producer_adapter_sha256=None,
            kind="bootstrap",
            platform=platform,
            architecture=architecture,
            pack_id=None,
            gate_evidence=_gate_evidence("bootstrap"),
        )
        inputs.append(
            {
                "source_dir": f"stages/{target}/bootstrap",
                "receipt": f"receipts/{target}/bootstrap.json",
            }
        )
        for pack_id in PACK_TOOLS:
            tools = list(PACK_TOOLS[pack_id])
            services = list(PACK_SERVICES[pack_id])
            pack = root / "stages" / target / "packs" / pack_id
            pack.mkdir(parents=True)
            if pack_id == "image":
                (pack / "__main__.py").write_text(
                    "raise SystemExit('managed bridge fixture')\n", encoding="utf-8"
                )
                (pack / "ecorex-image-pack.json").write_text(
                    json.dumps(
                        {
                            "schema_version": 1,
                            "pack_id": "image",
                            "runtime_api_version": "1.0.0",
                            "tools": tools,
                            "adapter": "core-managed-image-v1",
                        },
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    encoding="utf-8",
                )
            elif pack_id == "browser":
                (pack / "__main__.py").write_text(
                    "raise SystemExit('fixture is packaging-only')\n", encoding="utf-8"
                )
                (pack / "ecorex-pack.json").write_text(
                    json.dumps(
                        {
                            "schema_version": 1,
                            "protocol": "ecorex-stdio-tool-v1",
                            "pack_id": pack_id,
                            "runtime_api_version": "1.0.0",
                            "tools": tools,
                        },
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    encoding="utf-8",
                )
            else:
                (pack / "ecorex-dependency-pack.json").write_text(
                    json.dumps(
                        {
                            "adapter": {
                                "channels": "managed-channel-contracts-v1",
                                "ocr": "python-rapidocr-runtime-v1",
                                "office": "python-office-formats-v1",
                            }[pack_id],
                            "inventory": "runtime-inventory.json",
                            "kind": "dependency-service",
                            "pack_id": pack_id,
                            "runtime_api_version": "1.0.0",
                            "schema_version": 1,
                            "services": services,
                        },
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    encoding="utf-8",
                )
                (pack / "runtime-inventory.json").write_text(
                    json.dumps(
                        {
                            "distributions": (
                                []
                                if pack_id == "channels"
                                else [
                                    {
                                        "name": (
                                            "rapidocr-onnxruntime"
                                            if pack_id == "ocr"
                                            else "python-docx"
                                        ),
                                        "version": "1.0.0",
                                    }
                                ]
                            ),
                            "pack_id": pack_id,
                            "payload_sha256": hashlib.sha256(
                                f"{pack_id}:{target}:runtime".encode()
                            ).hexdigest(),
                            "schema_version": 1,
                        },
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    encoding="utf-8",
                )
            receipt = root / "receipts" / target / f"{pack_id}.json"
            write_stage_receipt(
                source_dir=pack,
                destination=receipt,
                stage_id=f"{pack_id}-{target}",
                commit_sha=COMMIT,
                workflow_run_id=RUN_ID,
                producer_executable_sha256=STAGER_SHA256,
                producer_adapter_sha256=None,
                kind="capability-pack",
                platform=platform,
                architecture=architecture,
                pack_id=pack_id,
                gate_evidence=_gate_evidence(pack_id),
            )
            inputs.append(
                {
                    "source_dir": f"stages/{target}/packs/{pack_id}",
                    "receipt": f"receipts/{target}/{pack_id}.json",
                }
            )
    return inputs


def _recipe(root: Path, inputs: list[dict[str, str]]) -> Path:
    value = {
        "schema_version": 1,
        "channel": "canary",
        "created_at": "2026-07-11T12:00:00+08:00",
        "sources": [
            {
                "source_id": "github-cn",
                "kind": "github-cn-mirror",
                "base_url": (
                    f"https://mirror.example/ecorex/v{PRODUCT_VERSION}/canary"
                ),
            },
            {
                "source_id": "github",
                "kind": "github-release",
                "base_url": "https://github.com/ecorex/ecorex/releases/download",
            },
            {
                "source_id": "cdn",
                "kind": "ecorex-cdn",
                "base_url": f"https://cdn.example/ecorex/v{PRODUCT_VERSION}",
            },
        ],
        "inputs": inputs,
    }
    path = root / "candidate-recipe.json"
    path.write_text(
        json.dumps(value, sort_keys=True, separators=(",", ":")), encoding="utf-8"
    )
    return path


def _staging_provenance(root: Path, *, run_id: int = RUN_ID) -> Path:
    value = {
        "schema_version": 1,
        "status": "passed",
        "workflow_path": ".github/workflows/ecorex-v1-platform-stage.yml",
        "workflow_run_id": run_id,
        "run_attempt": 1,
        "commit_sha": COMMIT,
        "repository": "ecorex/ecorex",
        "metadata_sha256": hashlib.sha256(b"staging-run-metadata").hexdigest(),
    }
    path = root / "staging-provenance.json"
    path.write_text(
        json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return path


def test_external_signer_uses_stdin_and_emits_only_redacted_receipts(tmp_path: Path) -> None:
    signer, public, private = _external_signer(tmp_path)
    payload = b"exact canonical release payload"

    signature = signer.sign(payload)

    Ed25519PublicKey.from_public_bytes(public).verify(signature, payload)
    assert signer.receipts[0].payload_sha256 == hashlib.sha256(payload).hexdigest()
    assert private not in json.dumps(signer.receipts[0].to_dict()).encode()
    assert private not in repr(signer).encode()
    assert "redacted" in repr(signer)


def test_external_signer_rejects_unpinned_adapter(tmp_path: Path) -> None:
    signer, _public, _private = _external_signer(tmp_path)
    adapter = tmp_path / "kms_adapter.py"
    adapter.write_text("print('substituted')\n", encoding="utf-8")

    with pytest.raises(Exception, match="digest|changed|signer"):
        signer.sign(b"must not be accepted")
    assert signer.receipts == ()


def test_candidate_builds_three_bootstraps_runtime_archives_and_fifteen_real_packs(
    tmp_path: Path,
) -> None:
    signer, public, private = _external_signer(tmp_path)
    input_root = tmp_path / "input"
    input_root.mkdir()
    recipe = _recipe(input_root, _stages(input_root, public))
    provenance = _staging_provenance(input_root)
    repository = Path(__file__).resolve().parents[2]
    recipe_schema = json.loads(
        (repository / "release/v1/candidate-recipe.schema.json").read_text()
    )
    stage_schema = json.loads(
        (repository / "release/v1/stage-receipt.schema.json").read_text()
    )
    Draft202012Validator(recipe_schema).validate(json.loads(recipe.read_text()))
    Draft202012Validator(stage_schema).validate(
        json.loads((input_root / "receipts/windows-x64/core.json").read_text())
    )
    Draft202012Validator(stage_schema).validate(
        json.loads((input_root / "receipts/windows-x64/bootstrap.json").read_text())
    )
    built = build_candidate(
        recipe_path=recipe,
        input_root=input_root,
        web_dist=_web_dist(tmp_path / "dist"),
        destination=tmp_path / "release",
        receipt_path=tmp_path / "candidate-receipt.json",
        expected_commit=COMMIT,
        expected_workflow_run_id=RUN_ID,
        staging_provenance_path=provenance,
        dependency_lock_manifest_path=repository / "requirements/locks/manifest.json",
        signer=signer,
    )

    artifact_ids = {artifact.artifact_id for artifact in built.manifest.artifacts}
    assert {
        "core-windows-x64",
        "core-macos-arm64",
        "core-macos-x64",
        "bootstrap-windows-x64",
        "bootstrap-macos-arm64",
        "bootstrap-macos-x64",
    }.issubset(artifact_ids)
    assert sum(item.startswith("capability-pack-") for item in artifact_ids) == 30
    assert "web-manifest" in artifact_ids
    assert built.manifest.sources[0].base_url.endswith(
        f"/canary/{built.manifest.release_id}"
    )
    assert built.manifest.sources[1].base_url.endswith(
        f"/v{PRODUCT_VERSION}-canary-{built.manifest.release_id.rsplit('-', 1)[1]}"
    )
    assert built.manifest.sources[2].base_url.endswith(
        f"/v{PRODUCT_VERSION}/{built.manifest.release_id}"
    )
    verifier = Ed25519SignatureVerifier({signer.key_id: public})
    verify_manifest_signature(built.manifest, verifier)
    for artifact in built.manifest.artifacts:
        verify_artifact_file(
            built.artifact_paths[artifact.artifact_id],
            built.manifest,
            artifact,
            verifier,
        )
    for artifact_id in ("core-macos-arm64", "core-macos-x64"):
        with zipfile.ZipFile(built.artifact_paths[artifact_id]) as archive:
            interpreter = archive.getinfo("bin/pack-python/bin/python3")
            assert (interpreter.external_attr >> 16) & 0o777 == 0o755
    with zipfile.ZipFile(
        built.artifact_paths["bootstrap-windows-x64"]
    ) as windows_bootstrap:
        assert windows_bootstrap.read("EcoreX Installer.cmd").startswith(
            b"@echo off\r\n"
        )
    for artifact_id in ("bootstrap-macos-arm64", "bootstrap-macos-x64"):
        with zipfile.ZipFile(built.artifact_paths[artifact_id]) as archive:
            installer = archive.getinfo("EcoreX Installer.command")
            assert (installer.external_attr >> 16) & 0o777 == 0o755
    all_bytes = b"".join(path.read_bytes() for path in built.output_dir.iterdir())
    assert private not in all_bytes
    receipt = json.loads((tmp_path / "candidate-receipt.json").read_text())
    assert receipt["status"] == "passed"
    assert receipt["schema_version"] == 2
    candidate_schema = json.loads(
        (repository / "release/v1/candidate-build-receipt.schema.json").read_text()
    )
    Draft202012Validator(candidate_schema).validate(receipt)
    assert verifier.verify(
        candidate_receipt_signing_payload(receipt),
        SignatureEnvelope.from_dict(receipt["signature"]),
    ) is True
    assert receipt["python_dependency_lock_sha256"] == hashlib.sha256(
        (repository / "requirements/locks/manifest.json").read_bytes()
    ).hexdigest()
    metadata = json.loads(built.metadata_path.read_text())
    assert (
        metadata["python_dependency_lock_sha256"]
        == receipt["python_dependency_lock_sha256"]
    )
    sbom = json.loads(built.sbom_path.read_text())
    lock_component = next(
        component
        for component in sbom["components"]
        if component.get("bom-ref") == "dependency-lock:python"
    )
    assert lock_component["hashes"] == [
        {
            "alg": "SHA-256",
            "content": receipt["python_dependency_lock_sha256"],
        }
    ]
    assert len(receipt["stage_receipts"]) == 21
    assert receipt["staging_provenance"]["workflow_run_id"] == RUN_ID
    assert receipt["signing"]["operation_count"] == len(signer.receipts)
    supply = subprocess.run(
        [
            sys.executable,
            str(repository / "scripts/check-v1-candidate-supply-chain.py"),
            "release",
            "--release-dir",
            str(built.output_dir),
            "--dependency-lock-manifest",
            str(repository / "requirements/locks/manifest.json"),
            "--report",
            str(tmp_path / "release-supply-chain.json"),
        ],
        capture_output=True,
        check=False,
    )
    assert supply.returncode == 0, supply.stderr.decode(errors="replace")


def test_candidate_rejects_mutated_stage_before_any_signing(tmp_path: Path) -> None:
    signer, public, _private = _external_signer(tmp_path)
    input_root = tmp_path / "input"
    input_root.mkdir()
    inputs = _stages(input_root, public)
    recipe = _recipe(input_root, inputs)
    provenance = _staging_provenance(input_root)
    (input_root / inputs[0]["source_dir"] / "runtime-config.json").write_bytes(b"{}")

    with pytest.raises(CandidateBuildError, match="candidate_stage_tree_mismatch"):
        build_candidate(
            recipe_path=recipe,
            input_root=input_root,
            web_dist=_web_dist(tmp_path / "dist"),
            destination=tmp_path / "release",
            receipt_path=tmp_path / "candidate-receipt.json",
            expected_commit=COMMIT,
            expected_workflow_run_id=RUN_ID,
            staging_provenance_path=provenance,
            dependency_lock_manifest_path=(
                Path(__file__).resolve().parents[2]
                / "requirements/locks/manifest.json"
            ),
            signer=signer,
        )
    assert signer.receipts == ()
    assert not (tmp_path / "release").exists()


def test_candidate_rejects_stage_receipt_from_a_different_run_attempt(
    tmp_path: Path,
) -> None:
    signer, public, _private = _external_signer(tmp_path)
    input_root = tmp_path / "input"
    input_root.mkdir()
    inputs = _stages(input_root, public)
    recipe = _recipe(input_root, inputs)
    provenance = _staging_provenance(input_root)
    receipt_path = input_root / inputs[0]["receipt"]
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["producer"]["workflow_run_attempt"] = 2
    receipt_path.write_text(
        json.dumps(receipt, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
        newline="\n",
    )

    with pytest.raises(CandidateBuildError, match="candidate_stage_producer_invalid"):
        build_candidate(
            recipe_path=recipe,
            input_root=input_root,
            web_dist=_web_dist(tmp_path / "dist"),
            destination=tmp_path / "release",
            receipt_path=tmp_path / "candidate-receipt.json",
            expected_commit=COMMIT,
            expected_workflow_run_id=RUN_ID,
            staging_provenance_path=provenance,
            dependency_lock_manifest_path=(
                Path(__file__).resolve().parents[2]
                / "requirements/locks/manifest.json"
            ),
            signer=signer,
        )
    assert signer.receipts == ()


def test_stage_receipt_refuses_placeholder_pack(tmp_path: Path) -> None:
    source = tmp_path / "browser"
    source.mkdir()
    (source / "README.txt").write_text("not an executable pack", encoding="utf-8")

    with pytest.raises(CandidateBuildError, match="stage_required_binary_missing"):
        write_stage_receipt(
            source_dir=source,
            destination=tmp_path / "receipt.json",
            stage_id="browser-windows-x64",
            commit_sha=COMMIT,
            workflow_run_id=RUN_ID,
            producer_executable_sha256=STAGER_SHA256,
            producer_adapter_sha256=None,
            kind="capability-pack",
            platform="windows",
            architecture="x64",
            pack_id="browser",
            gate_evidence=_gate_evidence("browser"),
        )


def test_stage_receipt_rejects_embedded_private_credentials(tmp_path: Path) -> None:
    source = tmp_path / "image"
    source.mkdir()
    (source / "ecorex-image-pack.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "pack_id": "image",
                "runtime_api_version": "1.0.0",
                "tools": ["imagegen", "vision"],
                "adapter": "core-managed-image-v1",
            }
        ),
        encoding="utf-8",
    )
    (source / "leaked.txt").write_text(
        "ghp_abcdefghijklmnopqrstuvwxyz", encoding="utf-8"
    )

    with pytest.raises(CandidateBuildError, match="stage_source_secret_detected"):
        write_stage_receipt(
            source_dir=source,
            destination=tmp_path / "receipt.json",
            stage_id="image-windows-x64",
            commit_sha=COMMIT,
            workflow_run_id=RUN_ID,
            producer_executable_sha256=STAGER_SHA256,
            producer_adapter_sha256=None,
            kind="capability-pack",
            platform="windows",
            architecture="x64",
            pack_id="image",
            gate_evidence=_gate_evidence("image"),
        )


def test_stage_receipt_accepts_non_secret_dependency_marker_substrings(
    tmp_path: Path,
) -> None:
    source = tmp_path / "image"
    source.mkdir()
    (source / "ecorex-image-pack.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "pack_id": "image",
                "runtime_api_version": "1.0.0",
                "tools": ["imagegen", "vision"],
                "adapter": "core-managed-image-v1",
            }
        ),
        encoding="utf-8",
    )
    (source / "__main__.py").write_text(
        "raise SystemExit('managed bridge test')\n",
        encoding="utf-8",
    )
    (source / "ssh.py").write_text(
        '_SK_START = b"-----BEGIN OPENSSH PRIVATE KEY-----"\n',
        encoding="utf-8",
    )
    (source / "font-data.txt").write_text(
        "QAHAEwAAQAAAAAAAgAHAGQAAQAAAAAAAwAaAKIAAQAAAAAABAAHAM0AAQAAAA",
        encoding="utf-8",
    )

    receipt = write_stage_receipt(
        source_dir=source,
        destination=tmp_path / "receipt.json",
        stage_id="image-windows-x64",
        commit_sha=COMMIT,
        workflow_run_id=RUN_ID,
        producer_executable_sha256=STAGER_SHA256,
        producer_adapter_sha256=None,
        kind="capability-pack",
        platform="windows",
        architecture="x64",
        pack_id="image",
        gate_evidence=_gate_evidence("image"),
    )

    assert (
        json.loads(receipt.read_text(encoding="utf-8"))["receipt_type"]
        == "ecorex-candidate-stage"
    )


def test_stage_receipt_does_not_treat_opaque_native_bytes_as_tokens(
    tmp_path: Path,
) -> None:
    source = tmp_path / "image"
    source.mkdir()
    (source / "ecorex-image-pack.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "pack_id": "image",
                "runtime_api_version": "1.0.0",
                "tools": ["imagegen", "vision"],
                "adapter": "core-managed-image-v1",
            }
        ),
        encoding="utf-8",
    )
    (source / "__main__.py").write_text(
        "raise SystemExit('managed bridge test')\n",
        encoding="utf-8",
    )
    (source / "signed-native-member").write_bytes(
        b"\xcf\xfa\xed\xfe\x00\x01ghp_abcdefghijklmnopqrstuvwxyz123456\x00\xff\x80"
    )

    receipt = write_stage_receipt(
        source_dir=source,
        destination=tmp_path / "receipt.json",
        stage_id="image-macos-arm64",
        commit_sha=COMMIT,
        workflow_run_id=RUN_ID,
        producer_executable_sha256=STAGER_SHA256,
        producer_adapter_sha256=None,
        kind="capability-pack",
        platform="macos",
        architecture="arm64",
        pack_id="image",
        gate_evidence=_gate_evidence("image"),
    )

    assert json.loads(receipt.read_text(encoding="utf-8"))["source_tree_file_count"] == 3


def test_candidate_cli_writes_typed_failure_when_protected_signer_is_missing(
    tmp_path: Path,
) -> None:
    repository = Path(__file__).resolve().parents[2]
    receipt = tmp_path / "candidate-failure.json"
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("ECOREX_RELEASE_SIGNER_")
    }
    result = subprocess.run(
        [
            sys.executable,
            str(repository / "scripts/build-v1-candidate.py"),
            "--recipe",
            str(tmp_path / "missing-recipe.json"),
            "--input-root",
            str(tmp_path),
            "--web-dist",
            str(tmp_path),
            "--output",
            str(tmp_path / "release"),
            "--receipt",
            str(receipt),
            "--expected-commit",
            COMMIT,
            "--expected-staging-run-id",
            str(RUN_ID),
            "--staging-provenance",
            str(tmp_path / "missing-provenance.json"),
            "--dependency-lock-manifest",
            str(repository / "requirements/locks/manifest.json"),
        ],
        capture_output=True,
        check=False,
        env=environment,
    )
    assert result.returncode == 1
    failure = json.loads(receipt.read_text())
    assert failure == {
        "schema_version": 2,
        "receipt_type": "ecorex-candidate-build",
        "status": "failed",
        "code": "candidate_signer_configuration_missing",
        "commit_sha": COMMIT,
        "release_id": None,
    }
    assert not (tmp_path / "release").exists()


def test_candidate_and_publication_workflows_are_split_and_default_safe() -> None:
    platform_stage = (
        Path(__file__).resolve().parents[2]
        / ".github"
        / "workflows"
        / "ecorex-v1-platform-stage.yml"
    ).read_text(encoding="utf-8")
    candidate = (
        Path(__file__).resolve().parents[2]
        / ".github"
        / "workflows"
        / "ecorex-v1-candidate.yml"
    ).read_text(encoding="utf-8")
    root = Path(__file__).resolve().parents[2]
    manual = (root / "scripts/release-v1.py").read_text(encoding="utf-8")

    assert "github.sha != inputs.source_sha" in platform_stage
    assert "github.repository != 'zyfjacksonchen-source/EcoreX'" in platform_stage
    assert "github.ref_protected" not in platform_stage

    assert "pull_request:" not in candidate
    assert "workflow_dispatch:" in candidate
    assert "REQUESTED_SHA: ${{ inputs.source_sha }}" in candidate
    assert 'os.environ["SOURCE_SHA"] == os.environ["REQUESTED_SHA"]' in candidate
    assert "refs/heads/main" in candidate
    assert "github.ref_protected" not in candidate
    assert "cancel-in-progress: false" in candidate
    assert "ecorex-release-signing-${{ inputs.channel }}" in candidate
    assert "ecorex-v1-accepted-${{ inputs.channel }}" in candidate
    assert "publish-assets" not in candidate
    assert "contents: write" not in candidate
    assert "ECOREX_CONTROL_PLANE_TOKEN" not in candidate
    assert "ECOREX_GITHUB_RELEASE_REPOSITORY" in candidate
    assert candidate.count(
        "ECOREX_GITHUB_RELEASE_REPOSITORY: zyfjacksonchen-source/EcoreX-installers"
    ) == 2
    assert 'os.environ["REPOSITORY"] == "zyfjacksonchen-source/EcoreX"' in candidate
    assert '--repository "$ECOREX_GITHUB_RELEASE_REPOSITORY"' in candidate
    delta_download = candidate.split(
        "- name: Download optional prior signed Core set for delta derivation", 1
    )[1].split("- name:", 1)[0]
    assert "GH_TOKEN: ${{ secrets.ECOREX_GITHUB_RELEASE_READ_TOKEN }}" in delta_download
    assert "GH_TOKEN: ${{ github.token }}" not in delta_download
    assert "ECOREX_GITHUB_RELEASE_TOKEN" not in candidate

    assert not (root / ".github/workflows/ecorex-v1-promote-candidate.yml").exists()
    assert "class GitHubActions" in manual
    assert "_READ_ONLY_BUILD_WORKFLOWS" in manual
    assert "ecorex-v1-promote-candidate.yml" not in manual
    assert "def _publication_prepare(" in manual
    assert "def _stage_production(" in manual
    assert "def _github_release(" in manual
    assert "def _remote_activation(" in manual
    assert "def _activate_update_notification(" in manual
    assert "def _compensate_activation(" in manual
    assert "release_interactive_confirmation_required" in manual
    assert manual.index("_interactive_confirmation(confirmation_phrase(store.spec))") < manual.index(
        "_github_release(store)"
    )
    assert manual.index("_github_release(store)") < manual.index("_remote_activation(store)")
    assert manual.index("_remote_activation(store)") < manual.index(
        "_activate_update_notification(store)"
    )


def test_every_external_action_in_v1_workflows_is_commit_pinned() -> None:
    workflows = Path(__file__).resolve().parents[2] / ".github" / "workflows"
    external_uses = re.compile(r"^\s*uses:\s*([^\s#]+)", re.MULTILINE)
    immutable_ref = re.compile(r"^[^@]+@[0-9a-f]{40}$")

    observed: list[str] = []
    for path in sorted(workflows.glob("ecorex-v1-*.yml")):
        for reference in external_uses.findall(path.read_text(encoding="utf-8")):
            if reference.startswith("./"):
                continue
            observed.append(f"{path.name}:{reference}")
            assert immutable_ref.fullmatch(reference), (
                f"external GitHub Action must use an immutable commit SHA: "
                f"{path.name}:{reference}"
            )

    assert observed


def test_staging_provenance_rejects_pr_or_different_workflow(tmp_path: Path) -> None:
    script = Path(__file__).resolve().parents[2] / "scripts" / "verify-v1-staging-provenance.py"
    metadata = {
        "id": RUN_ID,
        "head_sha": COMMIT,
        "event": "workflow_dispatch",
        "status": "completed",
        "conclusion": "success",
        "path": ".github/workflows/ecorex-v1-platform-stage.yml",
        "repository": {"full_name": "ecorex/ecorex"},
        "head_repository": {"full_name": "ecorex/ecorex"},
        "pull_requests": [],
        "run_attempt": 1,
    }
    path = tmp_path / "run.json"
    path.write_text(json.dumps(metadata), encoding="utf-8")
    valid = subprocess.run(
        [
            sys.executable,
            str(script),
            "--metadata",
            str(path),
            "--expected-run-id",
            str(RUN_ID),
            "--expected-commit",
            COMMIT,
            "--expected-repository",
            "ecorex/ecorex",
            "--receipt",
            str(tmp_path / "receipt.json"),
        ],
        capture_output=True,
        check=False,
    )
    assert valid.returncode == 0

    metadata["pull_requests"] = [{"number": 7}]
    path.write_text(json.dumps(metadata), encoding="utf-8")
    rejected = subprocess.run(
        [
            sys.executable,
            str(script),
            "--metadata",
            str(path),
            "--expected-run-id",
            str(RUN_ID),
            "--expected-commit",
            COMMIT,
            "--expected-repository",
            "ecorex/ecorex",
            "--receipt",
            str(tmp_path / "second.json"),
        ],
        capture_output=True,
        check=False,
    )
    assert rejected.returncode == 1
    assert b"staging_run_provenance_rejected" in rejected.stderr


def test_evidence_assembler_requires_all_nonpublication_gates(tmp_path: Path) -> None:
    repository = Path(__file__).resolve().parents[2]
    publication_gates = {"github-release", "mirror-sync", "cdn-sync"}
    signature = SignatureEnvelope(
        algorithm="ed25519",
        key_id="release-key",
        value=base64.b64encode(b"signature").decode("ascii"),
    )
    payload = b"candidate core"
    manifest = ReleaseManifest(
        schema_version=1,
        release_id="release-1.0.0-canary",
        version="1.0.0",
        build_digest=hashlib.sha256(b"build").hexdigest(),
        channel=ReleaseChannel.CANARY,
        created_at="2026-07-11T12:00:00+08:00",
        sources=(
            ReleaseSource("mirror", SourceKind.GITHUB_CN_MIRROR, 0, "https://m.example/r"),
            ReleaseSource("github", SourceKind.GITHUB_RELEASE, 1, "https://g.example/r"),
            ReleaseSource("cdn", SourceKind.ECOREX_CDN, 2, "https://c.example/r"),
        ),
        artifacts=(
            ReleaseArtifact(
                artifact_id="core-windows-x64",
                platform="windows",
                architecture="x64",
                file_name="core.zip",
                size_bytes=len(payload),
                sha256=hashlib.sha256(payload).hexdigest(),
                signature=signature,
            ),
        ),
        signature=signature,
    )
    manifest_path = tmp_path / "release-manifest.json"
    manifest_path.write_text(manifest.to_json(), encoding="utf-8")
    required = required_release_gates(ReleaseChannel.CANARY)
    manifest_sha256 = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    receipts = tmp_path / "gates"
    receipts.mkdir()
    for gate in required - publication_gates:
        (receipts / f"{gate}.json").write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "receipt_type": "ecorex-release-gate",
                    "gate": gate,
                    "status": "passed",
                    "commit_sha": COMMIT,
                    "workflow_run_id": RUN_ID,
                    "release_id": manifest.release_id,
                    "version": manifest.version,
                    "channel": manifest.channel.value,
                    "build_digest": manifest.build_digest,
                    "manifest_sha256": manifest_sha256,
                    "evidence_type": "test-fixture",
                    "evidence_sha256": hashlib.sha256(gate.encode()).hexdigest(),
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )
    publication = tmp_path / "publication.json"
    reserved = {
        "release-manifest.json": (len(manifest_path.read_bytes()), manifest_sha256),
        "release-metadata.json": (1, hashlib.sha256(b"m").hexdigest()),
        "sbom.cdx.json": (1, hashlib.sha256(b"s").hexdigest()),
        "core.zip": (len(payload), hashlib.sha256(payload).hexdigest()),
    }
    publication.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "release_id": manifest.release_id,
                "version": manifest.version,
                "manifest_sha256": manifest_sha256,
                "github_release_id": 1,
                "github_draft": False,
                "source_receipts": {
                    source.source_id: [
                        {
                            "name": name,
                            "size_bytes": size,
                            "sha256": digest,
                            "url": f"{source.base_url}/{name}",
                        }
                        for name, (size, digest) in sorted(reserved.items())
                    ]
                    for source in manifest.sources
                },
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    output = tmp_path / "evidence.json"
    result = subprocess.run(
        [
            sys.executable,
            str(repository / "scripts/assemble-v1-release-evidence.py"),
            "--receipts-dir",
            str(receipts),
            "--publication-receipt",
            str(publication),
            "--manifest",
            str(manifest_path),
            "--expected-commit",
            COMMIT,
            "--expected-workflow-run-id",
            str(RUN_ID),
            "--output",
            str(output),
        ],
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    evidence = json.loads(output.read_text())
    assert evidence["attestation_type"] == "ecorex-release-gate-bundle"
    assert evidence["phase"] == "finalize"
    assert evidence["commit_sha"] == COMMIT
    assert evidence["workflow_run_id"] == RUN_ID
    assert set(evidence["gates"]) == required
    assert "signature" not in evidence
    assert len(
        {
            evidence["gates"][gate]["evidence"]
            for gate in publication_gates
        }
    ) == 1

    gate = sorted(required - publication_gates)[0]
    receipt_path = receipts / f"{gate}.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["workflow_run_id"] = RUN_ID + 1
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    mixed = subprocess.run(
        [
            sys.executable,
            str(repository / "scripts/assemble-v1-release-evidence.py"),
            "--receipts-dir", str(receipts),
            "--publication-receipt", str(publication),
            "--manifest", str(manifest_path),
            "--expected-commit", COMMIT,
            "--expected-workflow-run-id", str(RUN_ID),
            "--output", str(tmp_path / "mixed.json"),
        ],
        capture_output=True,
        check=False,
    )
    assert mixed.returncode == 1

    receipt["workflow_run_id"] = float("inf")
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    nonfinite = subprocess.run(
        [
            sys.executable,
            str(repository / "scripts/assemble-v1-release-evidence.py"),
            "--receipts-dir", str(receipts),
            "--publication-receipt", str(publication),
            "--manifest", str(manifest_path),
            "--expected-commit", COMMIT,
            "--expected-workflow-run-id", str(RUN_ID),
            "--output", str(tmp_path / "nonfinite.json"),
        ],
        capture_output=True,
        check=False,
    )
    assert nonfinite.returncode == 1


def test_recipe_assembler_uses_release_scoped_channel_roots(tmp_path: Path) -> None:
    repository = Path(__file__).resolve().parents[2]
    for platform, architecture in (
        ("windows", "x64"),
        ("macos", "arm64"),
        ("macos", "x64"),
    ):
        target = f"{platform}-{architecture}"
        (tmp_path / f"stages/{target}/core").mkdir(parents=True)
        (tmp_path / f"stages/{target}/bootstrap").mkdir(parents=True)
        for pack_id in PACK_TOOLS:
            (tmp_path / f"stages/{target}/packs/{pack_id}").mkdir(parents=True)
        receipts = tmp_path / "receipts" / target
        receipts.mkdir(parents=True)
        for name in ("core", "bootstrap", *PACK_TOOLS):
            (receipts / f"{name}.json").write_text("{}", encoding="utf-8")
    output = tmp_path / "recipe.json"
    environment = {
        **os.environ,
        "ECOREX_RELEASE_MIRROR_BASE_URL": (
            f"https://mirror.example/ecorex/v{PRODUCT_VERSION}"
        ),
        "ECOREX_RELEASE_CDN_BASE_URL": (
            f"https://cdn.example/ecorex/v{PRODUCT_VERSION}"
        ),
    }
    result = subprocess.run(
        [
            sys.executable,
            str(repository / "scripts/assemble-v1-candidate-recipe.py"),
            "--input-root",
            str(tmp_path),
            "--output",
            str(output),
            "--channel",
            "canary",
            "--created-at",
            "2026-07-11T12:00:00+08:00",
            "--repository",
            "ecorex/ecorex",
        ],
        capture_output=True,
        check=False,
        env=environment,
    )
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    recipe = json.loads(output.read_text())
    assert len(recipe["inputs"]) == 21
    assert recipe["sources"][0]["base_url"].endswith(
        f"/v{PRODUCT_VERSION}/canary"
    )
    assert recipe["sources"][1]["base_url"].endswith("/releases/download")
    assert recipe["sources"][2]["base_url"].endswith(f"/v{PRODUCT_VERSION}")


def test_recipe_assembler_preserves_github_cn_proxy_namespace(tmp_path: Path) -> None:
    repository = Path(__file__).resolve().parents[2]
    for platform, architecture in (
        ("windows", "x64"),
        ("macos", "arm64"),
        ("macos", "x64"),
    ):
        target = f"{platform}-{architecture}"
        (tmp_path / f"stages/{target}/core").mkdir(parents=True)
        (tmp_path / f"stages/{target}/bootstrap").mkdir(parents=True)
        for pack_id in PACK_TOOLS:
            (tmp_path / f"stages/{target}/packs/{pack_id}").mkdir(parents=True)
        receipts = tmp_path / "receipts" / target
        receipts.mkdir(parents=True)
        for name in ("core", "bootstrap", *PACK_TOOLS):
            (receipts / f"{name}.json").write_text("{}", encoding="utf-8")
    output = tmp_path / "proxy-recipe.json"
    proxy_root = (
        "https://ghproxy.net/https://github.com/"
        "zyfjacksonchen-source/EcoreX-installers/releases/download"
    )
    result = subprocess.run(
        [
            sys.executable,
            str(repository / "scripts/assemble-v1-candidate-recipe.py"),
            "--input-root",
            str(tmp_path),
            "--output",
            str(output),
            "--channel",
            "stable",
            "--created-at",
            "2026-07-16T12:00:00+08:00",
            "--repository",
            "zyfjacksonchen-source/EcoreX-installers",
        ],
        capture_output=True,
        check=False,
        env={
            **os.environ,
            "ECOREX_RELEASE_MIRROR_BASE_URL": proxy_root,
            "ECOREX_RELEASE_CDN_BASE_URL": (
                "https://dl.ecoremedia.net/ecorex-agent/releases/"
                f"v{PRODUCT_VERSION}"
            ),
        },
    )
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    recipe = json.loads(output.read_text())
    assert recipe["sources"][0]["base_url"] == proxy_root
    assert recipe["sources"][1]["base_url"].endswith("/releases/download")
    assert recipe["sources"][2]["base_url"].endswith(f"/v{PRODUCT_VERSION}")
