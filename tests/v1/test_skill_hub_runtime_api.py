from __future__ import annotations

import io
from pathlib import Path
import zipfile

from fastapi import FastAPI
from fastapi.testclient import TestClient

from ecorex.extensions import LocalSkillBundleStore, SQLiteExtensionRepository
from ecorex.extensions.hub_api import register_skill_hub_runtime_routes
from ecorex.extensions.service import ExtensionService


def _package() -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr(
            "SKILL.md",
            "---\nname: 文档助手\ndescription: 帮助整理办公文档。\nversion: 1.0.0\n"
            "tags: [\"office\"]\n---\n\n按需提供说明。\n",
        )
    return output.getvalue()


class _Cloud:
    def __init__(self, package: bytes, digest: str) -> None:
        self.package = package
        self.digest = digest
        self.downloads = 0
        self.intent_identity = None
        self.intent_creations = 0
        self.completions = []

    async def list_skills(self, **_filters):
        return {
            "schema_version": 1,
            "items": [{
                "slug": "document-helper",
                "title": "文档助手",
                "summary": "帮助整理办公文档。",
                "version": "1.0.0",
                "package_sha256": self.digest,
                "package_size_bytes": len(self.package),
                "tags": ["office"],
                "category": "office_productivity",
                "uploader": {"nickname": "e-Mate 用户", "author_ref": "author_" + "a" * 24},
                "provenance": {"brand": "e-Mate", "original_platform": None, "original_url": None},
            }],
            "next_cursor": None,
        }

    async def skill_detail(self, *, slug: str):
        listing = await self.list_skills()
        card = listing["items"][0]
        assert slug == card["slug"]
        return {"schema_version": 1, "skill": card, "versions": [card]}

    async def download_package(self, *, slug: str, version: str):
        assert (slug, version) == ("document-helper", "1.0.0")
        self.downloads += 1
        return self.package, self.digest

    async def upload_skill(self, **request):
        return {
            "slug": request["slug"],
            "title": "上传的技能",
            "summary": "已发布到 e-Mate Skill Hub。",
            "version": "1.0.0",
            "package_sha256": self.digest,
            "package_size_bytes": len(self.package),
            "tags": ["office"],
            "category": request["category"],
            "uploader": {"nickname": "e-Mate 用户", "author_ref": "author_" + "b" * 24},
            "provenance": {"brand": "e-Mate", "original_platform": None, "original_url": None},
        }

    async def create_install_intent(self, **request):
        self.intent_creations += 1
        self.intent_identity = dict(request)
        return {"install_intent": "i" * 64}

    async def consume_install_intent(self, *, install_intent: str):
        assert len(install_intent) >= 64
        assert self.intent_identity is not None
        return {
            "slug": self.intent_identity["slug"],
            "version": self.intent_identity["version"],
            "package_sha256": self.intent_identity["package_sha256"],
            "completion_receipt": "c" * 64,
        }

    async def complete_install_intent(self, *, completion_receipt: str, status: str):
        assert completion_receipt == "c" * 64
        self.completions.append(status)


def test_runtime_hub_install_is_digest_bound_enabled_and_immediately_projected(tmp_path: Path) -> None:
    package = _package()
    store = LocalSkillBundleStore(tmp_path / "cas")
    digest = store.ingest_zip(package).artifact_sha256
    service = ExtensionService(
        SQLiteExtensionRepository(tmp_path / "runtime.db"),
        runtime_api_version="1.0.0",
        platform="win32",
        architecture="x64",
        local_bundle_store=store,
    )
    app = FastAPI()
    cloud = _Cloud(package, digest)
    register_skill_hub_runtime_routes(app, client=cloud, extensions=service)
    client = TestClient(app)

    before = client.get("/api/v1/skill-hub/skills").json()
    assert before["items"][0]["installation_status"] == "not_installed"
    detail = client.get("/api/v1/skill-hub/skills/document-helper")
    assert detail.status_code == 200
    assert detail.json()["skill"]["slug"] == "document-helper"
    assert [item["version"] for item in detail.json()["versions"]] == ["1.0.0"]
    downloaded = client.get(
        "/api/v1/skill-hub/skills/document-helper/versions/1.0.0/package"
    )
    assert downloaded.status_code == 200
    assert downloaded.content == package
    assert downloaded.headers["x-skill-content-sha256"] == digest
    assert downloaded.headers["content-disposition"] == (
        'attachment; filename="document-helper-1.0.0.zip"'
    )
    installed = client.post(
        "/api/v1/skill-hub/skills/document-helper/install",
        json={
            "version": "1.0.0",
            "package_sha256": digest,
            "client_request_id": "install-document-helper-1",
        },
    )
    assert installed.status_code == 200
    assert installed.json()["extension"]["status"] == "enabled"
    assert installed.json()["extensions"]["extension_generation"] == 2
    assert client.get("/api/v1/skill-hub/skills").json()["items"][0]["installation_status"] == "installed_enabled"
    assert client.post(
        "/api/v1/skill-hub/skills/document-helper/install",
        json={
            "version": "1.0.0",
            "package_sha256": digest,
            "client_request_id": "install-document-helper-1",
        },
    ).status_code == 200
    assert cloud.downloads == 2  # one explicit download plus one install
    assert cloud.completions == ["installed", "installed"]

    published = client.post(
        "/api/v1/skill-hub/skills",
        json={
            "slug": "uploaded-helper",
            "category": "office_productivity",
            "bundle_base64": "UEsDBA==",
            "client_request_id": "upload-helper-1",
        },
    )
    assert published.status_code == 201
    assert published.json()["slug"] == "uploaded-helper"

    current = service.projection("hub.document-helper")
    service.disable(
        "hub.document-helper",
        expected_revision=current.revision,
        client_request_id="disable-document-helper-1",
    )
    reenabled = client.post(
        "/api/v1/skill-hub/skills/document-helper/install",
        json={
            "version": "1.0.0",
            "package_sha256": digest,
            "client_request_id": "reenable-document-helper-1",
        },
    )
    assert reenabled.status_code == 200
    assert reenabled.json()["extension"]["status"] == "enabled"
    assert cloud.downloads == 2
    assert cloud.completions[-1] == "installed"


def test_runtime_hub_alias_reuses_native_provider_and_rejects_version_drift(
    tmp_path: Path,
) -> None:
    package = _package()
    store = LocalSkillBundleStore(tmp_path / "cas")
    digest = store.ingest_zip(package).artifact_sha256
    service = ExtensionService(
        SQLiteExtensionRepository(tmp_path / "runtime.db"),
        runtime_api_version="1.0.0",
        platform="win32",
        architecture="x64",
        local_bundle_store=store,
    )
    staged = service.install_local_skill_zip(
        package,
        extension_id="skill.office-documents",
        expected_revision=0,
        client_request_id="install:native-office-documents",
    )
    app = FastAPI()
    cloud = _Cloud(package, digest)
    register_skill_hub_runtime_routes(app, client=cloud, extensions=service)
    client = TestClient(app)
    cloud.intent_identity = {
        "slug": "docx",
        "version": "1.0.0",
        "package_sha256": digest,
    }
    reused = client.post(
        "/api/v1/skill-hub/skills/docx/install",
        json={
            "version": "1.0.0",
            "package_sha256": digest,
            "client_request_id": "install-docx-alias-1",
            "install_intent": "d" * 64,
        },
    )
    assert reused.status_code == 200
    assert reused.json()["extension"]["extension_id"] == "skill.office-documents"
    assert cloud.downloads == 0
    assert cloud.intent_creations == 0
    assert cloud.completions == ["installed"]
    assert service.projection(staged.extension_id).status == "enabled"
    assert service.repository.state("hub.docx") is None

    drift = client.post(
        "/api/v1/skill-hub/skills/document-helper/install",
        json={
            "version": "2.0.0",
            "package_sha256": digest,
            "client_request_id": "install-version-drift-1",
        },
    )
    assert drift.status_code == 422
    assert service.repository.state("hub.document-helper") is None
    assert cloud.completions[-1] == "failed"
