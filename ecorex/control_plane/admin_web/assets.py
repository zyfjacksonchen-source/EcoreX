"""Verified, content-addressed assets for the administrator release console."""

from __future__ import annotations

from base64 import b64encode
from dataclasses import dataclass
import hashlib
from importlib import resources
import json
from pathlib import Path
import re
from types import MappingProxyType
from typing import Mapping


_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_ASSET_PREFIX = re.compile(r"^(?:/[A-Za-z0-9._-]+)+$")
_EXPECTED_FILES = frozenset(
    {"index.html", "admin.css", "admin.js", "asset-manifest.json"}
)
_SOURCE_ASSETS = frozenset({"index.html", "admin.css", "admin.js"})
_SIZE_LIMITS = {
    "index.html": 512 * 1024,
    "admin.css": 2 * 1024 * 1024,
    "admin.js": 4 * 1024 * 1024,
}


class AdminWebAssetError(RuntimeError):
    """Raised when the embedded administrator UI fails closed verification."""


@dataclass(frozen=True, slots=True)
class VerifiedAdminAsset:
    source_name: str
    public_name: str
    media_type: str
    digest: str
    sri: str
    content: bytes

    @classmethod
    def from_content(
        cls, source_name: str, content: bytes, expected_digest: str
    ) -> "VerifiedAdminAsset":
        digest = hashlib.sha256(content).hexdigest()
        if digest != expected_digest:
            raise AdminWebAssetError(f"administrator asset digest mismatch: {source_name}")
        suffix = Path(source_name).suffix
        if suffix == ".css":
            media_type = "text/css"
        elif suffix == ".js":
            media_type = "text/javascript"
        else:
            media_type = "text/html"
        stem = Path(source_name).stem
        public_name = f"{stem}.{digest}{suffix}"
        sri = "sha256-" + b64encode(bytes.fromhex(digest)).decode("ascii")
        return cls(
            source_name=source_name,
            public_name=public_name,
            media_type=media_type,
            digest=digest,
            sri=sri,
            content=content,
        )


@dataclass(frozen=True, slots=True)
class AdminWebAssets:
    index_template: str
    index_digest: str
    assets: Mapping[str, VerifiedAdminAsset]

    @classmethod
    def load(cls, static_directory: Path | None = None) -> "AdminWebAssets":
        payloads = (
            _directory_asset_payloads(static_directory)
            if static_directory is not None
            else _package_asset_payloads()
        )
        manifest_bytes = payloads["asset-manifest.json"]
        if not 1 <= len(manifest_bytes) <= 64 * 1024:
            raise AdminWebAssetError("administrator asset manifest size is invalid")
        try:
            manifest = json.loads(manifest_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
            raise AdminWebAssetError("administrator asset manifest is invalid") from error
        if (
            not isinstance(manifest, dict)
            or set(manifest) != {"schema_version", "assets"}
            or manifest.get("schema_version") != 1
            or not isinstance(manifest.get("assets"), dict)
            or set(manifest["assets"]) != _SOURCE_ASSETS
        ):
            raise AdminWebAssetError("administrator asset manifest contract is invalid")

        verified_by_source: dict[str, VerifiedAdminAsset] = {}
        for name in sorted(_SOURCE_ASSETS):
            expected_digest = manifest["assets"].get(name)
            if not isinstance(expected_digest, str) or _DIGEST.fullmatch(expected_digest) is None:
                raise AdminWebAssetError(f"administrator asset digest is invalid: {name}")
            content = payloads[name]
            if not 1 <= len(content) <= _SIZE_LIMITS[name]:
                raise AdminWebAssetError(f"administrator asset size is invalid: {name}")
            verified_by_source[name] = VerifiedAdminAsset.from_content(
                name, content, expected_digest
            )

        try:
            index_template = verified_by_source["index.html"].content.decode("utf-8")
        except UnicodeDecodeError as error:
            raise AdminWebAssetError("administrator HTML is not valid UTF-8") from error
        placeholders = {
            "__ADMIN_CSS_URL__",
            "__ADMIN_CSS_SRI__",
            "__ADMIN_JS_URL__",
            "__ADMIN_JS_SRI__",
        }
        if any(index_template.count(placeholder) != 1 for placeholder in placeholders):
            raise AdminWebAssetError("administrator HTML placeholders are invalid")
        if re.search(r"__ADMIN_[A-Z_]+__", index_template.replace("__ADMIN_CSS_URL__", "").replace("__ADMIN_CSS_SRI__", "").replace("__ADMIN_JS_URL__", "").replace("__ADMIN_JS_SRI__", "")):
            raise AdminWebAssetError("administrator HTML contains an unknown placeholder")

        public_assets = {
            asset.public_name: asset
            for name, asset in verified_by_source.items()
            if name in {"admin.css", "admin.js"}
        }
        return cls(
            index_template=index_template,
            index_digest=verified_by_source["index.html"].digest,
            assets=MappingProxyType(public_assets),
        )

    def render_index(self, asset_prefix: str) -> str:
        normalized = asset_prefix.rstrip("/")
        if _ASSET_PREFIX.fullmatch(normalized) is None:
            raise AdminWebAssetError("administrator asset URL prefix is invalid")
        css = next(asset for asset in self.assets.values() if asset.source_name == "admin.css")
        javascript = next(
            asset for asset in self.assets.values() if asset.source_name == "admin.js"
        )
        return (
            self.index_template.replace(
                "__ADMIN_CSS_URL__", f"{normalized}/{css.public_name}"
            )
            .replace("__ADMIN_CSS_SRI__", css.sri)
            .replace(
                "__ADMIN_JS_URL__", f"{normalized}/{javascript.public_name}"
            )
            .replace("__ADMIN_JS_SRI__", javascript.sri)
        )

    def get(self, public_name: str) -> VerifiedAdminAsset | None:
        return self.assets.get(public_name)


def _directory_asset_payloads(candidate_root: Path) -> dict[str, bytes]:
    if candidate_root.is_symlink():
        raise AdminWebAssetError("administrator static directory is invalid")
    root = candidate_root.resolve()
    if not root.is_dir():
        raise AdminWebAssetError("administrator static directory is invalid")
    present = frozenset(entry.name for entry in root.iterdir())
    if present != _EXPECTED_FILES:
        raise AdminWebAssetError("administrator static allowlist does not match")
    payloads: dict[str, bytes] = {}
    for name in _EXPECTED_FILES:
        path = root / name
        if not path.is_file() or path.is_symlink() or path.resolve().parent != root:
            raise AdminWebAssetError(f"administrator static file is invalid: {name}")
        try:
            payloads[name] = path.read_bytes()
        except OSError as error:
            raise AdminWebAssetError(
                f"administrator asset cannot be read: {name}"
            ) from error
    return payloads


def _package_asset_payloads() -> dict[str, bytes]:
    """Read signed package resources from a directory or zipimport archive."""

    try:
        root = resources.files(__package__).joinpath("static")
        entries = tuple(root.iterdir())
        present = frozenset(entry.name for entry in entries)
        if present != _EXPECTED_FILES or any(
            not root.joinpath(name).is_file() for name in _EXPECTED_FILES
        ):
            raise AdminWebAssetError(
                "administrator static allowlist does not match"
            )
        return {name: root.joinpath(name).read_bytes() for name in _EXPECTED_FILES}
    except AdminWebAssetError:
        raise
    except (FileNotFoundError, OSError, TypeError) as error:
        raise AdminWebAssetError(
            "administrator static directory is invalid"
        ) from error


__all__ = [
    "AdminWebAssetError",
    "AdminWebAssets",
    "VerifiedAdminAsset",
]
