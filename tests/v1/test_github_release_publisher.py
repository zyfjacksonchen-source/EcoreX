from __future__ import annotations

import hashlib
import json
from pathlib import Path

import httpx
import pytest

from ecorex.release import (
    EnvironmentGitHubCredential,
    GitHubPublicationError,
    GitHubReleasePublisher,
    release_tag,
)
from ecorex.update import ReleaseChannel


RELEASE_ID = "release-stable-0123456789abcdef01234567"


def test_release_channels_have_distinct_immutable_tag_namespaces() -> None:
    assert release_tag("1.0.0", ReleaseChannel.STABLE) == "v1.0.0"
    assert (
        release_tag(
            "1.0.0",
            ReleaseChannel.CANARY,
            release_id="release-canary-0123456789abcdef01234567",
        )
        == "v1.0.0-canary-0123456789abcdef01234567"
    )


class Credential:
    def bearer_token(self) -> str:
        return "github-installation-token-secret"


def _release(*, draft: bool = True, upload_url: str | None = None) -> dict:
    return {
        "id": 77,
        "tag_name": "v1.0.0",
        "name": "EcoreX 1.0.0",
        "body": f"EcoreX signed release {RELEASE_ID}",
        "prerelease": False,
        "upload_url": upload_url
        or "https://uploads.github.com/repos/acme/ecorex/releases/77/assets{?name,label}",
        "draft": draft,
    }


def _asset(name: str, payload: bytes, *, asset_id: int = 10) -> dict:
    digest = hashlib.sha256(payload).hexdigest()
    return {
        "id": asset_id,
        "name": name,
        "size": len(payload),
        "digest": f"sha256:{digest}",
        "browser_download_url": (
            f"https://github.com/acme/ecorex/releases/download/v1.0.0/{name}"
        ),
    }


def test_draft_upload_resume_and_publish_are_digest_fenced(tmp_path: Path) -> None:
    payload = b"signed release bytes"
    package = tmp_path / "core-windows-x64.zip"
    package.write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest()
    calls: list[tuple[str, str]] = []
    uploaded = False
    draft_published = False

    def asset_payload() -> dict:
        result = _asset(package.name, payload)
        if not draft_published:
            result["browser_download_url"] = (
                "https://github.com/acme/ecorex/releases/download/"
                f"untagged-c1935de23995d4876c39/{package.name}"
            )
        return result

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal uploaded, draft_published
        calls.append((request.method, str(request.url)))
        assert request.headers["authorization"] == (
            "Bearer github-installation-token-secret"
        )
        assert request.headers["x-github-api-version"] == "2026-03-10"
        if request.method == "GET" and request.url.path.endswith(
            "/releases/tags/v1.0.0"
        ):
            return httpx.Response(
                404,
                json={"message": "not found"},
                headers={"content-type": "application/json"},
            )
        if request.method == "POST" and request.url.path.endswith("/releases"):
            body = json.loads(request.content)
            assert body["draft"] is True
            assert body["prerelease"] is False
            assert RELEASE_ID in body["body"]
            return httpx.Response(
                201,
                json=_release(),
                headers={"content-type": "application/json"},
            )
        if request.method == "GET" and request.url.path.endswith(
            "/releases/77/assets"
        ):
            assets = [asset_payload()] if uploaded else []
            return httpx.Response(
                200,
                json=assets,
                headers={"content-type": "application/json"},
            )
        if request.method == "POST" and request.url.host == "uploads.github.com":
            assert request.url.params["name"] == package.name
            assert request.content == payload
            uploaded = True
            return httpx.Response(
                201,
                json=asset_payload(),
                headers={"content-type": "application/json"},
            )
        if request.method == "PATCH" and request.url.path.endswith("/releases/77"):
            assert json.loads(request.content) == {"draft": False}
            draft_published = True
            return httpx.Response(
                200,
                json=_release(draft=False),
                headers={"content-type": "application/json"},
            )
        raise AssertionError((request.method, request.url))

    client = httpx.Client(transport=httpx.MockTransport(handler))
    publisher = GitHubReleasePublisher(
        owner="acme",
        repository="ecorex",
        credentials=Credential(),
        client=client,
    )
    draft = publisher.ensure_draft(
        version="1.0.0",
        channel=ReleaseChannel.STABLE,
        release_id=RELEASE_ID,
    )
    first = publisher.ensure_asset(draft, package, expected_sha256=digest)
    second = publisher.ensure_asset(draft, package, expected_sha256=digest)
    published = publisher.publish(draft)
    public = publisher.ensure_asset(published, package, expected_sha256=digest)

    assert first == second
    assert first.sha256 == digest
    assert public.browser_download_url.endswith(f"/v1.0.0/{package.name}")
    assert published.draft is False
    assert sum(
        method == "POST" and "uploads.github.com" in url
        for method, url in calls
    ) == 1
    assert "github-installation-token-secret" not in repr(calls)


def test_existing_remote_digest_conflict_never_overwrites_asset(tmp_path: Path) -> None:
    package = tmp_path / "core-windows-x64.zip"
    package.write_bytes(b"new bytes")
    digest = hashlib.sha256(package.read_bytes()).hexdigest()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/releases/tags/v1.0.0"):
            body = _release()
        elif request.url.path.endswith("/releases/77/assets"):
            body = [_asset(package.name, b"old bytes")]
        else:
            raise AssertionError("publisher must not mutate a conflicting asset")
        return httpx.Response(
            200, json=body, headers={"content-type": "application/json"}
        )

    publisher = GitHubReleasePublisher(
        owner="acme",
        repository="ecorex",
        credentials=Credential(),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    draft = publisher.ensure_draft(
        version="1.0.0",
        channel=ReleaseChannel.STABLE,
        release_id=RELEASE_ID,
    )
    with pytest.raises(GitHubPublicationError, match="remote_asset_conflict"):
        publisher.ensure_asset(draft, package, expected_sha256=digest)


@pytest.mark.parametrize(
    "upload_url",
    [
        "http://uploads.github.com/repos/acme/ecorex/releases/77/assets{?name}",
        "https://evil.example/repos/acme/ecorex/releases/77/assets{?name}",
        "https://uploads.github.com/repos/acme/other/releases/77/assets{?name}",
        "https://user:pass@uploads.github.com/repos/acme/ecorex/releases/77/assets",
    ],
)
def test_hypermedia_upload_url_is_strictly_allowlisted(upload_url: str) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=_release(upload_url=upload_url),
            headers={"content-type": "application/json"},
        )

    publisher = GitHubReleasePublisher(
        owner="acme",
        repository="ecorex",
        credentials=Credential(),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(GitHubPublicationError, match="upload_url_refused"):
        publisher.ensure_draft(
            version="1.0.0",
            channel=ReleaseChannel.STABLE,
            release_id=RELEASE_ID,
        )


def test_retryable_failure_and_redirect_do_not_leak_remote_body() -> None:
    secret = "remote-provider-secret"

    def unavailable(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            503,
            json={"message": secret},
            headers={"content-type": "application/json"},
        )

    publisher = GitHubReleasePublisher(
        owner="acme",
        repository="ecorex",
        credentials=Credential(),
        client=httpx.Client(transport=httpx.MockTransport(unavailable)),
    )
    with pytest.raises(GitHubPublicationError) as failure:
        publisher.ensure_draft(
            version="1.0.0",
            channel=ReleaseChannel.STABLE,
            release_id=RELEASE_ID,
        )
    assert failure.value.retryable is True
    assert secret not in str(failure.value)

    redirecting = GitHubReleasePublisher(
        owner="acme",
        repository="ecorex",
        credentials=Credential(),
        client=httpx.Client(
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(
                    302,
                    headers={
                        "location": "https://evil.example",
                        "content-type": "application/json",
                    },
                )
            )
        ),
    )
    with pytest.raises(GitHubPublicationError, match="redirect_refused"):
        redirecting.ensure_draft(
            version="1.0.0",
            channel=ReleaseChannel.STABLE,
            release_id=RELEASE_ID,
        )


def test_environment_credential_is_late_bound_and_redacted() -> None:
    environment = {"ECOREX_GITHUB_TOKEN": "first-token"}
    credential = EnvironmentGitHubCredential(environment)
    assert credential.bearer_token() == "first-token"
    environment["ECOREX_GITHUB_TOKEN"] = "second-token"
    assert credential.bearer_token() == "second-token"
    assert "second-token" not in repr(credential)

    environment["ECOREX_GITHUB_TOKEN"] = "line1\nline2"
    with pytest.raises(GitHubPublicationError):
        credential.bearer_token()


def test_concurrent_release_and_asset_creation_resume_by_exact_digest(
    tmp_path: Path,
) -> None:
    package = tmp_path / "core.zip"
    package.write_bytes(b"raced exact bytes")
    digest = hashlib.sha256(package.read_bytes()).hexdigest()
    get_release_count = 0
    list_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal get_release_count, list_count
        if request.method == "GET" and request.url.path.endswith(
            "/releases/tags/v1.0.0"
        ):
            get_release_count += 1
            if get_release_count == 1:
                return httpx.Response(
                    404,
                    json={"message": "not found"},
                    headers={"content-type": "application/json"},
                )
            return httpx.Response(
                200,
                json=_release(),
                headers={"content-type": "application/json"},
            )
        if request.method == "POST" and request.url.path.endswith("/releases"):
            return httpx.Response(
                422,
                json={"message": "already_exists"},
                headers={"content-type": "application/json"},
            )
        if request.method == "GET" and request.url.path.endswith(
            "/releases/77/assets"
        ):
            list_count += 1
            value = [] if list_count == 1 else [_asset(package.name, package.read_bytes())]
            return httpx.Response(
                200,
                json=value,
                headers={"content-type": "application/json"},
            )
        if request.method == "POST" and request.url.host == "uploads.github.com":
            return httpx.Response(
                422,
                json={"message": "already_exists"},
                headers={"content-type": "application/json"},
            )
        raise AssertionError((request.method, request.url))

    publisher = GitHubReleasePublisher(
        owner="acme",
        repository="ecorex",
        credentials=Credential(),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    draft = publisher.ensure_draft(
        version="1.0.0",
        channel=ReleaseChannel.STABLE,
        release_id=RELEASE_ID,
    )
    receipt = publisher.ensure_asset(draft, package, expected_sha256=digest)
    assert receipt.sha256 == digest
    assert get_release_count == 2
    assert list_count == 2


def test_existing_tag_with_different_product_identity_is_rejected() -> None:
    conflicting = _release()
    conflicting["body"] = "unrelated release"
    publisher = GitHubReleasePublisher(
        owner="acme",
        repository="ecorex",
        credentials=Credential(),
        client=httpx.Client(
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(
                    200,
                    json=conflicting,
                    headers={"content-type": "application/json"},
                )
            )
        ),
    )
    with pytest.raises(GitHubPublicationError, match="identity_conflict"):
        publisher.ensure_draft(
            version="1.0.0",
            channel=ReleaseChannel.STABLE,
            release_id=RELEASE_ID,
        )
