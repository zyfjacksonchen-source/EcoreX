from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta
import io
import sqlite3
import zipfile

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from ecorex.control_plane.models import ControlPrincipal
from ecorex.control_plane.skill_hub import (
    SkillHubConflict,
    SkillHubRegistry,
    create_skill_hub_router,
)
from ecorex.extensions import LocalSkillBundleStore


def test_skill_hub_versions_are_immutable_searchable_and_pseudonymous(tmp_path) -> None:
    hub = SkillHubRegistry(tmp_path / "hub.db", author_key=b"k" * 32)
    first = hub.publish(
        account_id="account-secret",
        nickname="办公达人",
        slug="office-review",
        version="1.0.0",
        title="文档审阅",
        summary="检查办公文档并给出修改建议。",
        category="office_productivity",
        tags=("office", "review"),
        package_sha256="a" * 64,
        package_size_bytes=123,
        original_platform="cow-skill-hub",
        original_url="https://example.invalid/source",
    )
    assert first.uploader.author_ref.startswith("author_")
    assert "account-secret" not in first.model_dump_json()
    assert hub.list(query="审阅", category="office_productivity") == (first,)

    with pytest.raises(SkillHubConflict):
        hub.publish(
            account_id="account-secret",
            nickname="办公达人",
            slug="office-review",
            version="1.0.0",
            title="不可覆盖",
            summary="同一版本不能覆盖。",
            category="office_productivity",
            tags=("office",),
            package_sha256="b" * 64,
            package_size_bytes=124,
        )

    newer = hub.publish(
        account_id="account-secret",
        nickname="办公达人",
        slug="office-review",
        version="1.1.0",
        title="文档审阅 Plus",
        summary="新的不可变版本。",
        category="office_productivity",
        tags=("office", "review"),
        package_sha256="c" * 64,
        package_size_bytes=125,
        original_platform="cow-skill-hub",
        original_url="https://example.invalid/source-plus",
    )
    assert hub.get("office-review") == newer
    assert hub.get("office-review", version="1.0.0") == first
    assert hub.list(tag="review", source="cow-skill-hub") == (newer,)
    detail = hub.detail("office-review")
    assert detail.skill == newer
    assert [item.version for item in detail.versions] == ["1.1.0", "1.0.0"]
    reopened = SkillHubRegistry(
        tmp_path / "hub.db", author_key=b"k" * 32, initialize=False
    )
    assert reopened.get("office-review") == newer

    hidden = hub.publish(
        account_id="account-email",
        nickname="private@example.com",
        slug="safe-author",
        version="1.0.0",
        title="安全作者",
        summary="作者邮箱不会进入卡片。",
        category="office_productivity",
        tags=("office",),
        package_sha256="d" * 64,
        package_size_bytes=126,
    )
    assert hidden.uploader.nickname == "e-Mate 用户"
    assert "private@example.com" not in hidden.model_dump_json()

    with pytest.raises(ValueError, match="metadata"):
        hub.publish(
            account_id="account-secret",
            nickname="办公达人",
            slug="travel-manager",
            version="1.0.0",
            title="排除项",
            summary="不得重新导入。",
            category="third_party",
            tags=(),
            package_sha256="e" * 64,
            package_size_bytes=127,
        )


def test_skill_hub_http_upload_uses_session_identity_and_downloads_verified_package(tmp_path) -> None:
    hub = SkillHubRegistry(tmp_path / "hub.db", author_key=b"k" * 32)
    store = LocalSkillBundleStore(tmp_path / "cas")
    principal = ControlPrincipal(
        subject="user", client_id="device", account_id="private-account"
    )
    app = FastAPI()
    app.include_router(
        create_skill_hub_router(
            hub,
            store,
            principal_dependency=lambda: principal,
            nickname_resolver=lambda account_id: "e-Mate 用户" if account_id else "",
        )
    )
    package = io.BytesIO()
    with zipfile.ZipFile(package, "w") as archive:
        archive.writestr(
            "SKILL.md",
            "---\nname: 表格助手\ndescription: 批量整理办公表格。\nversion: 1.0.0\n"
            "tags: [\"office\",\"sheet\"]\n---\n\n按需读取并执行。\n",
        )
    encoded = base64.b64encode(package.getvalue()).decode("ascii")
    client = TestClient(app)
    assert client.get(
        "/ecorex-agent/client/skill-hub/v1/skills", params={"source": "../bad"}
    ).status_code == 422
    created = client.post(
        "/ecorex-agent/client/skill-hub/v1/skills",
        json={
            "slug": "sheet-helper",
            "category": "office_productivity",
            "bundle_base64": encoded,
            "client_request_id": "upload-sheet-1",
        },
    )
    assert created.status_code == 201
    assert "private-account" not in created.text
    replay = client.post(
        "/ecorex-agent/client/skill-hub/v1/skills",
        json={
            "slug": "sheet-helper",
            "category": "office_productivity",
            "bundle_base64": encoded,
            "client_request_id": "upload-sheet-1",
        },
    )
    assert replay.status_code == 201
    card = created.json()
    detail = client.get(
        "/ecorex-agent/client/skill-hub/v1/skills/sheet-helper"
    )
    assert detail.status_code == 200
    assert detail.json()["skill"] == card
    assert [item["version"] for item in detail.json()["versions"]] == ["1.0.0"]
    download = client.get(
        f"/ecorex-agent/client/skill-hub/v1/skills/sheet-helper/versions/{card['version']}/package"
    )
    assert download.status_code == 200
    assert download.headers["x-skill-content-sha256"] == card["package_sha256"]
    assert store.ingest_zip(download.content).artifact_sha256 == card["package_sha256"]

    intent = client.post(
        "/ecorex-agent/client/skill-hub/v1/skills/sheet-helper/versions/1.0.0/install-intent",
        json={
            "package_sha256": card["package_sha256"],
            "client_request_id": "install-sheet-helper-1",
        },
    )
    assert intent.status_code == 200
    token = intent.json()["install_intent"]
    assert "private-account" not in token
    claimed = client.post(
        "/ecorex-agent/client/skill-hub/v1/install-intents/consume",
        json={"install_intent": token},
    )
    assert claimed.status_code == 200
    assert claimed.json()["package_sha256"] == card["package_sha256"]
    assert client.post(
        "/ecorex-agent/client/skill-hub/v1/install-intents/consume",
        json={"install_intent": token},
    ).status_code == 409
    completed = client.post(
        "/ecorex-agent/client/skill-hub/v1/install-intents/complete",
        json={
            "completion_receipt": claimed.json()["completion_receipt"],
            "status": "installed",
        },
    )
    assert completed.status_code == 200
    assert hub.install_logs(claimed.json()["intent_id"]) == (
        "created", "claimed", "installed"
    )
    with sqlite3.connect(hub.path) as connection, pytest.raises(sqlite3.IntegrityError):
        connection.execute("DELETE FROM skill_hub_install_logs")


def test_skill_hub_install_intent_is_account_bound_and_expires(tmp_path) -> None:
    hub = SkillHubRegistry(tmp_path / "hub.db", author_key=b"k" * 32)
    hub.publish(
        account_id="publisher",
        nickname="作者",
        slug="intent-skill",
        version="1.0.0",
        title="意图技能",
        summary="安装意图测试。",
        category="office_productivity",
        tags=("office",),
        package_sha256="f" * 64,
        package_size_bytes=128,
    )
    now = datetime(2026, 8, 4, tzinfo=UTC)
    intent = hub.create_install_intent(
        account_id="account-a",
        slug="intent-skill",
        version="1.0.0",
        package_sha256="f" * 64,
        client_request_id="intent-expiry-1",
        now=now,
        ttl_seconds=30,
    )
    token = str(intent["install_intent"])
    middle = len(token) // 2
    tampered = token[:middle] + ("A" if token[middle] != "A" else "B") + token[middle + 1 :]
    with pytest.raises(ValueError, match="token"):
        hub.consume_install_intent(
            account_id="account-a",
            install_intent=tampered,
            now=now,
        )
    with pytest.raises(SkillHubConflict, match="account"):
        hub.consume_install_intent(
            account_id="account-b", install_intent=token, now=now
        )
    with pytest.raises(SkillHubConflict, match="expired"):
        hub.consume_install_intent(
            account_id="account-a",
            install_intent=token,
            now=now + timedelta(seconds=30),
        )
