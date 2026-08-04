from __future__ import annotations

import json
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from ecorex.capabilities import builtin_capability_registry
from ecorex.connectors import builtin_connector_registry
from ecorex.extensions import compose_extension_service
from ecorex.runtime import SQLiteDatabase
from ecorex.update import Ed25519SignatureVerifier


def _skill(root: Path, slug: str, *, script: bool = False) -> Path:
    directory = root / slug
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text(
        "---\n"
        f"name: {slug}\n"
        f"description: {slug} migration fixture\n"
        "mentionable: true\n"
        "metadata:\n  cowagent:\n    default_enabled: true\n"
        "---\n\n# Fixture\n",
        encoding="utf-8",
    )
    if script:
        scripts = directory / "scripts"
        scripts.mkdir()
        (scripts / "run.py").write_text("print('stored, not executed')\n", encoding="utf-8")
    return directory


def _service(database: Path, builtin: Path, custom: Path):
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    return compose_extension_service(
        database_path=database,
        product_version="0.3.0",
        core_build_digest="a" * 64,
        runtime_api_version="1.0.0",
        platform="windows",
        architecture="x64",
        capability_registry=builtin_capability_registry(),
        connector_registry=builtin_connector_registry(),
        installed_pack_ids=frozenset(),
        signature_verifier=Ed25519SignatureVerifier({"release": public}),
        builtin_skill_root=builtin,
        legacy_skill_roots=(custom,),
    )


def test_skill_directory_migration_converges_aliases_overrides_and_tombstones(tmp_path) -> None:
    database = tmp_path / "runtime.db"
    SQLiteDatabase(database)
    builtin = tmp_path / "builtin"
    custom = tmp_path / "custom"
    office = _skill(builtin, "office-documents", script=True)
    cache = office / "scripts" / "__pycache__"
    cache.mkdir()
    (cache / "run.cpython-311.pyc").write_bytes(b"generated cache")
    builtin_pdf = _skill(builtin, "office-pdf")
    copied_pdf = _skill(custom, "office-pdf")
    (copied_pdf / "SKILL.md").write_bytes((builtin_pdf / "SKILL.md").read_bytes())
    copied = _skill(custom, "docx", script=True)
    (copied / "SKILL.md").write_bytes((office / "SKILL.md").read_bytes().replace(
        b"name: office-documents", b"name: docx"
    ))
    # Alias copies have different bytes, so explicit marker is the only safe fold override.
    (copied / ".ecorex-custom-override").write_text("", encoding="utf-8")
    _skill(custom, "lark-cli")
    _skill(custom, "my-skill")
    _skill(custom, "travel-manager")
    (custom / "skills_config.json").write_text(
        json.dumps({"my-skill": {"enabled": False}}), encoding="utf-8"
    )

    service = _service(database, builtin, custom)

    assert service.projection("skill.office-documents").source == "local_bundle"
    assert service.projection("skill.office-pdf").source == "core_bundle"
    assert service.projection("skill.feishu-lark").status == "disabled"
    assert service.projection("skill.my-skill").status == "disabled"
    assert "skill.travel-manager" not in {item.extension_id for item in service.catalog()}
    office_projection = service.projection("skill.office-documents")
    assert service.local_bundle_store.read_verified_file(
        office_projection.active_digest, "scripts/run.py"
    ).startswith(b"print(")
    assert all(
        "__pycache__" not in item.path
        for item in service.local_bundle_store.verify(office_projection.active_digest).files
    )

    removed = service.uninstall(
        "skill.my-skill",
        expected_revision=service.projection("skill.my-skill").revision,
        client_request_id="test:migration-uninstall",
    )
    assert removed.status == "uninstalled"
    assert copied.is_dir() and (custom / "my-skill").is_dir()

    restarted = _service(database, builtin, custom)
    assert restarted.projection("skill.my-skill").status == "uninstalled"
