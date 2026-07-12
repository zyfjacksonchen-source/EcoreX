"""Resumable, digest-fenced GitHub Release publication transport."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import mimetypes
import os
from pathlib import Path
import re
import stat as stat_module
from typing import Any, Final, Mapping, Protocol, runtime_checkable
from urllib.parse import quote, urlencode, urlsplit

import httpx

from ecorex.update import ReleaseChannel

from .identity import release_tag


GITHUB_API_VERSION: Final = "2026-03-10"
_SAFE_REPOSITORY_PART = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SAFE_ASSET = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,179}$")
_MAX_JSON_BYTES = 2 * 1024 * 1024

class GitHubPublicationError(RuntimeError):
    """A non-sensitive GitHub publication failure."""

    def __init__(self, code: str, *, retryable: bool = False) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable


@runtime_checkable
class GitHubCredentialProvider(Protocol):
    def bearer_token(self) -> str: ...


class EnvironmentGitHubCredential:
    """Read a CI-injected token at request time; never accept it as CLI text."""

    __slots__ = ("_environment", "_variable")

    def __init__(
        self,
        environment: Mapping[str, str] | None = None,
        variable: str = "ECOREX_GITHUB_TOKEN",
    ) -> None:
        if not re.fullmatch(r"[A-Z][A-Z0-9_]{2,127}", variable):
            raise ValueError("GitHub token environment variable is invalid")
        self._environment = os.environ if environment is None else environment
        self._variable = variable

    def bearer_token(self) -> str:
        token = self._environment.get(self._variable)
        if (
            not isinstance(token, str)
            or not token
            or len(token) > 4096
            or any(ord(character) < 0x21 or ord(character) > 0x7E for character in token)
        ):
            raise GitHubPublicationError("github_credentials_unavailable")
        return token

    def __repr__(self) -> str:
        return f"<EnvironmentGitHubCredential variable={self._variable!r} token=<redacted>>"


@dataclass(frozen=True, slots=True)
class GitHubReleaseDraft:
    release_id: int
    tag_name: str
    upload_url: str
    draft: bool


@dataclass(frozen=True, slots=True)
class GitHubAssetReceipt:
    asset_id: int
    name: str
    size_bytes: int
    sha256: str
    browser_download_url: str


class GitHubReleasePublisher:
    """Create one draft, upload exact signed bytes, then explicitly publish it."""

    def __init__(
        self,
        *,
        owner: str,
        repository: str,
        credentials: GitHubCredentialProvider,
        client: httpx.Client | None = None,
        api_origin: str = "https://api.github.com",
        upload_hosts: frozenset[str] = frozenset({"uploads.github.com"}),
    ) -> None:
        if not _SAFE_REPOSITORY_PART.fullmatch(owner):
            raise ValueError("GitHub owner is invalid")
        if not _SAFE_REPOSITORY_PART.fullmatch(repository):
            raise ValueError("GitHub repository is invalid")
        if not isinstance(credentials, GitHubCredentialProvider):
            raise TypeError("GitHub credential provider is invalid")
        origin = urlsplit(api_origin.rstrip("/"))
        if (
            origin.scheme != "https"
            or origin.hostname != "api.github.com"
            or origin.port not in {None, 443}
            or origin.path
            or origin.query
            or origin.fragment
            or origin.username
            or origin.password
        ):
            raise ValueError("GitHub API origin must be the fixed HTTPS API host")
        normalized_upload_hosts = frozenset(
            host.casefold().rstrip(".") for host in upload_hosts if host
        )
        if normalized_upload_hosts != frozenset({"uploads.github.com"}):
            raise ValueError("GitHub upload host allowlist is invalid")
        self.owner = owner
        self.repository = repository
        self.credentials = credentials
        self.api_origin = "https://api.github.com"
        self.upload_hosts = normalized_upload_hosts
        self._owns_client = client is None
        self.client = client or httpx.Client(
            timeout=httpx.Timeout(connect=15, read=120, write=120, pool=15),
            follow_redirects=False,
            trust_env=False,
            limits=httpx.Limits(max_connections=4, max_keepalive_connections=2),
        )

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def __enter__(self) -> "GitHubReleasePublisher":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def ensure_draft(
        self,
        *,
        version: str,
        channel: ReleaseChannel,
        release_id: str,
    ) -> GitHubReleaseDraft:
        tag = release_tag(version, channel, release_id=release_id)
        if not re.fullmatch(r"release-(?:canary|stable)-[0-9a-f]{24}", release_id):
            raise ValueError("release identity is invalid")
        path = self._repository_path(
            f"/releases/tags/{quote(tag, safe='')}"
        )
        status, value = self._json_request("GET", path, accepted={200, 404})
        if status == 404:
            status, value = self._json_request(
                "POST",
                self._repository_path("/releases"),
                payload={
                    "tag_name": tag,
                    "name": f"EcoreX {version}",
                    "body": f"EcoreX signed release {release_id}",
                    "draft": True,
                    "prerelease": channel is ReleaseChannel.CANARY,
                    "generate_release_notes": False,
                },
                # GitHub returns 422 when another publisher creates the tag
                # between our GET and POST.  Re-read the exact tag instead of
                # treating a safe publication race as a terminal failure.
                accepted={201, 422},
                idempotency_key=f"create:{release_id}",
            )
            if status == 422:
                status, value = self._json_request("GET", path, accepted={200})
        del status
        self._validate_release_contract(
            value,
            version=version,
            channel=channel,
            release_id=release_id,
        )
        return self._release_from_json(value, expected_tag=tag)

    def list_assets(
        self, release: GitHubReleaseDraft
    ) -> tuple[GitHubAssetReceipt, ...]:
        _require_release_id(release.release_id)
        _status, value = self._json_request(
            "GET",
            self._repository_path(
                f"/releases/{release.release_id}/assets?per_page=100&page=1"
            ),
            accepted={200},
        )
        if not isinstance(value, list) or len(value) > 100:
            raise GitHubPublicationError("invalid_asset_list")
        return tuple(self._asset_from_json(item) for item in value)

    def ensure_asset(
        self,
        release: GitHubReleaseDraft,
        path: str | os.PathLike[str],
        *,
        expected_sha256: str,
    ) -> GitHubAssetReceipt:
        if _SHA256.fullmatch(expected_sha256) is None:
            raise ValueError("expected asset SHA-256 is invalid")
        asset_path = Path(path)
        name = asset_path.name
        if _SAFE_ASSET.fullmatch(name) is None:
            raise ValueError("release asset filename is invalid")
        before = _stable_regular_file(asset_path)
        if before.st_size < 1:
            raise GitHubPublicationError("empty_asset")
        local_digest = _hash_stable_file(asset_path, before)
        if local_digest != expected_sha256:
            raise GitHubPublicationError("local_asset_digest_mismatch")

        existing = [asset for asset in self.list_assets(release) if asset.name == name]
        if len(existing) > 1:
            raise GitHubPublicationError("duplicate_remote_asset")
        if existing:
            receipt = existing[0]
            if receipt.size_bytes == before.st_size and receipt.sha256 == expected_sha256:
                self._validate_asset_url(receipt, release)
                return receipt
            raise GitHubPublicationError("remote_asset_conflict")
        if not release.draft:
            raise GitHubPublicationError("published_release_is_incomplete")

        upload_url = self._validated_upload_url(release)
        query = urlencode({"name": name})
        media_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
        token = self.credentials.bearer_token()
        headers = self._headers(token)
        headers["Content-Type"] = media_type
        headers["Accept-Encoding"] = "identity"
        try:
            with asset_path.open("rb") as stream:
                opened = os.fstat(stream.fileno())
                if _identity(opened) != _identity(before):
                    raise GitHubPublicationError("local_asset_changed")
                request = self.client.build_request(
                    "POST",
                    f"{upload_url}?{query}",
                    headers=headers,
                    content=stream,
                )
                response = self.client.send(request, stream=True, follow_redirects=False)
                try:
                    upload_status = response.status_code
                    value = self._consume_json(response, accepted={201, 422})
                finally:
                    response.close()
                stream.seek(0)
                uploaded_source_digest = _hash_open_stream(stream)
                after = os.fstat(stream.fileno())
            if _identity(after) != _identity(before):
                raise GitHubPublicationError("local_asset_changed")
            if uploaded_source_digest != expected_sha256:
                raise GitHubPublicationError("local_asset_changed")
        except GitHubPublicationError:
            raise
        except (OSError, httpx.TimeoutException, httpx.TransportError):
            raise GitHubPublicationError("github_upload_unavailable", retryable=True) from None
        finally:
            token = ""
        if upload_status == 422:
            raced = [
                asset for asset in self.list_assets(release) if asset.name == name
            ]
            if len(raced) != 1:
                raise GitHubPublicationError("github_upload_conflict")
            receipt = raced[0]
        else:
            receipt = self._asset_from_json(value)
        if (
            receipt.name != name
            or receipt.size_bytes != before.st_size
            or receipt.sha256 != expected_sha256
        ):
            raise GitHubPublicationError("uploaded_asset_digest_mismatch")
        self._validate_asset_url(receipt, release)
        return receipt

    def publish(self, release: GitHubReleaseDraft) -> GitHubReleaseDraft:
        if not release.draft:
            return release
        _status, value = self._json_request(
            "PATCH",
            self._repository_path(f"/releases/{release.release_id}"),
            payload={"draft": False},
            accepted={200},
            idempotency_key=f"publish:{release.release_id}",
        )
        result = self._release_from_json(value, expected_tag=release.tag_name)
        if result.release_id != release.release_id or result.draft:
            raise GitHubPublicationError("github_publish_not_confirmed")
        return result

    def _repository_path(self, suffix: str) -> str:
        return (
            f"{self.api_origin}/repos/{quote(self.owner, safe='')}/"
            f"{quote(self.repository, safe='')}{suffix}"
        )

    def _json_request(
        self,
        method: str,
        url: str,
        *,
        payload: Mapping[str, Any] | None = None,
        accepted: set[int],
        idempotency_key: str | None = None,
    ) -> tuple[int, Any]:
        token = self.credentials.bearer_token()
        headers = self._headers(token)
        if idempotency_key is not None:
            headers["Idempotency-Key"] = idempotency_key
        content = None
        if payload is not None:
            try:
                content = json.dumps(
                    payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                ).encode("utf-8")
            except (TypeError, ValueError):
                raise GitHubPublicationError("invalid_github_request") from None
            headers["Content-Type"] = "application/json"
        try:
            request = self.client.build_request(
                method, url, headers=headers, content=content
            )
            response = self.client.send(request, stream=True, follow_redirects=False)
            try:
                value = self._consume_json(response, accepted=accepted)
                return response.status_code, value
            finally:
                response.close()
        except GitHubPublicationError:
            raise
        except (httpx.TimeoutException, httpx.TransportError):
            raise GitHubPublicationError("github_api_unavailable", retryable=True) from None
        finally:
            token = ""

    def _consume_json(self, response: httpx.Response, *, accepted: set[int]) -> Any:
        if response.is_redirect or response.history:
            raise GitHubPublicationError("github_redirect_refused")
        if response.status_code not in accepted:
            retryable = response.status_code in {408, 425, 429, 502, 503, 504}
            raise GitHubPublicationError("github_api_rejected", retryable=retryable)
        if response.headers.get("content-encoding", "identity").casefold() != "identity":
            raise GitHubPublicationError("github_compressed_response")
        content_type = response.headers.get("content-type", "").split(";", 1)[0]
        if content_type != "application/json":
            raise GitHubPublicationError("github_invalid_response")
        body = bytearray()
        for chunk in response.iter_bytes():
            body.extend(chunk)
            if len(body) > _MAX_JSON_BYTES:
                raise GitHubPublicationError("github_response_too_large")
        try:
            return json.loads(bytes(body).decode("utf-8"), object_pairs_hook=_unique_object)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            raise GitHubPublicationError("github_invalid_response") from None

    def _release_from_json(
        self, value: Any, *, expected_tag: str
    ) -> GitHubReleaseDraft:
        if not isinstance(value, Mapping):
            raise GitHubPublicationError("github_invalid_release")
        release_id = value.get("id")
        tag_name = value.get("tag_name")
        upload_url = value.get("upload_url")
        draft = value.get("draft")
        if (
            isinstance(release_id, bool)
            or not isinstance(release_id, int)
            or release_id < 1
            or tag_name != expected_tag
            or not isinstance(upload_url, str)
            or not isinstance(draft, bool)
        ):
            raise GitHubPublicationError("github_invalid_release")
        result = GitHubReleaseDraft(release_id, tag_name, upload_url, draft)
        self._validated_upload_url(result)
        return result

    @staticmethod
    def _validate_release_contract(
        value: Any,
        *,
        version: str,
        channel: ReleaseChannel,
        release_id: str,
    ) -> None:
        if (
            not isinstance(value, Mapping)
            or value.get("name") != f"EcoreX {version}"
            or value.get("body") != f"EcoreX signed release {release_id}"
            or value.get("prerelease") is not (channel is ReleaseChannel.CANARY)
        ):
            raise GitHubPublicationError("github_release_identity_conflict")

    def _validate_asset_url(
        self,
        receipt: GitHubAssetReceipt,
        release: GitHubReleaseDraft,
    ) -> None:
        expected = (
            f"https://github.com/{quote(self.owner, safe='')}/"
            f"{quote(self.repository, safe='')}/releases/download/"
            f"{quote(release.tag_name, safe='')}/{quote(receipt.name, safe='')}"
        )
        if receipt.browser_download_url != expected:
            raise GitHubPublicationError("github_asset_url_identity_conflict")

    def _asset_from_json(self, value: Any) -> GitHubAssetReceipt:
        if not isinstance(value, Mapping):
            raise GitHubPublicationError("github_invalid_asset")
        asset_id = value.get("id")
        name = value.get("name")
        size = value.get("size")
        digest = value.get("digest")
        download = value.get("browser_download_url")
        if (
            isinstance(asset_id, bool)
            or not isinstance(asset_id, int)
            or asset_id < 1
            or not isinstance(name, str)
            or _SAFE_ASSET.fullmatch(name) is None
            or isinstance(size, bool)
            or not isinstance(size, int)
            or size < 0
            or not isinstance(digest, str)
            or not digest.startswith("sha256:")
            or _SHA256.fullmatch(digest[7:]) is None
            or not isinstance(download, str)
        ):
            raise GitHubPublicationError("github_invalid_asset")
        parsed = urlsplit(download)
        if (
            parsed.scheme != "https"
            or parsed.hostname != "github.com"
            or parsed.port not in {None, 443}
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise GitHubPublicationError("github_invalid_asset_url")
        return GitHubAssetReceipt(asset_id, name, size, digest[7:], download)

    def _validated_upload_url(self, release: GitHubReleaseDraft) -> str:
        raw = release.upload_url.split("{", 1)[0]
        parsed = urlsplit(raw)
        expected_path = (
            f"/repos/{self.owner}/{self.repository}/releases/"
            f"{release.release_id}/assets"
        )
        if (
            parsed.scheme != "https"
            or (parsed.hostname or "").casefold().rstrip(".") not in self.upload_hosts
            or parsed.port not in {None, 443}
            or parsed.username
            or parsed.password
            or parsed.path != expected_path
            or parsed.query
            or parsed.fragment
        ):
            raise GitHubPublicationError("github_upload_url_refused")
        return f"https://uploads.github.com{expected_path}"

    @staticmethod
    def _headers(token: str) -> dict[str, str]:
        return {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": GITHUB_API_VERSION,
            "Accept-Encoding": "identity",
            "User-Agent": "EcoreX-Release/1.0",
        }


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _require_release_id(value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError("GitHub release ID is invalid")


def _stable_regular_file(path: Path) -> os.stat_result:
    try:
        metadata = path.lstat()
    except OSError:
        raise GitHubPublicationError("local_asset_unavailable") from None
    attributes = getattr(metadata, "st_file_attributes", 0)
    reparse = getattr(stat_module, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    if (
        not stat_module.S_ISREG(metadata.st_mode)
        or stat_module.S_ISLNK(metadata.st_mode)
        or bool(attributes & reparse)
    ):
        raise GitHubPublicationError("local_asset_is_not_regular")
    return metadata


def _identity(metadata: os.stat_result) -> tuple[int, int, int, int]:
    return metadata.st_dev, metadata.st_ino, metadata.st_size, metadata.st_mtime_ns


def _hash_stable_file(path: Path, before: os.stat_result) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            if _identity(opened) != _identity(before):
                raise GitHubPublicationError("local_asset_changed")
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
            after = os.fstat(stream.fileno())
    except GitHubPublicationError:
        raise
    except OSError:
        raise GitHubPublicationError("local_asset_unavailable") from None
    if _identity(after) != _identity(before):
        raise GitHubPublicationError("local_asset_changed")
    return digest.hexdigest()


def _hash_open_stream(stream: Any) -> str:
    digest = hashlib.sha256()
    while chunk := stream.read(1024 * 1024):
        digest.update(chunk)
    return digest.hexdigest()


__all__ = [
    "EnvironmentGitHubCredential",
    "GITHUB_API_VERSION",
    "GitHubAssetReceipt",
    "GitHubCredentialProvider",
    "GitHubPublicationError",
    "GitHubReleaseDraft",
    "GitHubReleasePublisher",
    "release_tag",
]
