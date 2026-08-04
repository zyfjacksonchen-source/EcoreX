from __future__ import annotations

import asyncio
import hashlib
import json

import httpx

from ecorex.session.device_transport import HTTPSDeviceAuthorizationBroker


def test_skill_hub_transport_is_same_origin_authenticated_and_digest_bound() -> None:
    package = b"skill-package"
    digest = hashlib.sha256(package).hexdigest()
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.headers["authorization"] == "Bearer " + "t" * 128
        if request.method == "POST":
            body = json.loads(request.content)
            if request.url.path.endswith("/install-intent"):
                return httpx.Response(200, json={"install_intent": "i" * 64})
            if request.url.path.endswith("/install-intents/consume"):
                return httpx.Response(200, json={
                    "slug": "document-helper",
                    "version": "1.0.0",
                    "package_sha256": digest,
                    "completion_receipt": "c" * 64,
                })
            if request.url.path.endswith("/install-intents/complete"):
                return httpx.Response(200, json={
                    "schema_version": 1, "status": body["status"]
                })
            return httpx.Response(201, json={
                "slug": body["slug"],
                "title": "上传技能",
                "summary": "已发布。",
                "version": "1.0.0",
                "package_sha256": digest,
                "package_size_bytes": len(package),
                "tags": [],
                "category": body["category"],
                "uploader": {"nickname": "e-Mate 用户", "author_ref": "author_" + "a" * 24},
                "provenance": {"brand": "e-Mate", "original_platform": None, "original_url": None},
            })
        if request.url.path.endswith("/package"):
            return httpx.Response(200, content=package, headers={"X-Skill-Content-SHA256": digest})
        if request.url.path.endswith("/skills/document-helper"):
            card = {
                "slug": "document-helper", "title": "文档助手", "summary": "整理文档。",
                "version": "1.0.0", "package_sha256": digest,
                "package_size_bytes": len(package), "tags": ["office"],
                "category": "office_productivity",
                "uploader": {"nickname": "e-Mate 用户", "author_ref": "author_" + "a" * 24},
                "provenance": {"brand": "e-Mate", "original_platform": "cow-skill-hub", "original_url": None},
            }
            return httpx.Response(200, json={"schema_version": 1, "skill": card, "versions": [card]})
        return httpx.Response(
            200,
            content=json.dumps({"schema_version": 1, "items": [], "next_cursor": None}).encode(),
        )

    async def exercise() -> None:
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        broker = HTTPSDeviceAuthorizationBroker(
            "https://control.example",
            client_id="e-mate-runtime",
            allowed_hosts=frozenset({"control.example"}),
            client=client,
        )
        listing = await broker.skill_hub_list(
            access_token="t" * 128,
            query="office",
            category="office_productivity",
            tag="office",
            source="cow-skill-hub",
            cursor=None,
            limit=24,
        )
        downloaded = await broker.skill_hub_download(
            access_token="t" * 128,
            slug="document-helper",
            version="1.0.0",
        )
        detail = await broker.skill_hub_detail(
            access_token="t" * 128, slug="document-helper"
        )
        uploaded = await broker.skill_hub_upload(
            access_token="t" * 128,
            slug="uploaded-helper",
            category="office_productivity",
            bundle_base64="UEsDBA==",
            client_request_id="upload-helper-1",
        )
        assert listing["items"] == []
        assert downloaded == (package, digest)
        assert detail["skill"]["slug"] == "document-helper"
        assert uploaded["slug"] == "uploaded-helper"
        intent = await broker.skill_hub_create_install_intent(
            access_token="t" * 128,
            slug="document-helper",
            version="1.0.0",
            package_sha256=digest,
            client_request_id="install-document-helper-1",
        )
        claimed = await broker.skill_hub_consume_install_intent(
            access_token="t" * 128,
            install_intent=intent["install_intent"],
        )
        await broker.skill_hub_complete_install_intent(
            access_token="t" * 128,
            completion_receipt=claimed["completion_receipt"],
            status="installed",
        )
        await client.aclose()

    asyncio.run(exercise())
    assert requests[0].url == httpx.URL(
        "https://control.example/ecorex-agent/client/skill-hub/v1/skills"
        "?query=office&limit=24&category=office_productivity"
        "&tag=office&source=cow-skill-hub"
    )
    assert requests[1].url.path == (
        "/ecorex-agent/client/skill-hub/v1/skills/document-helper/versions/1.0.0/package"
    )
    assert requests[2].url.path.endswith("/skills/document-helper")
    assert requests[3].url.path == "/ecorex-agent/client/skill-hub/v1/skills"
    assert requests[4].url.path.endswith("/install-intent")
    assert requests[5].url.path.endswith("/install-intents/consume")
    assert requests[6].url.path.endswith("/install-intents/complete")
