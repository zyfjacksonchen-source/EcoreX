"""Small immutable Skill Hub catalog backed by e-Mate identity and CAS facts."""

from __future__ import annotations

import hashlib
import hmac
import json
import base64
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
import re
import secrets
import sqlite3
from typing import Literal, Sequence

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, Field

from ecorex.extensions import LocalSkillBundleStore
from ecorex.extensions.models import parse_semver
from ecorex.extensions.skill_migration import EXCLUDED_SKILL_SLUGS
from ecorex.protocol import (
    SkillHubCardProjection,
    SkillHubDetailProjection,
    SkillHubUploaderProjection,
    SkillProvenance,
)


SkillHubCategory = Literal["third_party", "content_creation", "office_productivity"]
_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{1,95}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_TAG = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_SOURCE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._ -]{0,63}$")
_EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
_INTENT_ID = re.compile(r"^intent_[0-9a-f]{32}$")


class SkillHubConflict(RuntimeError):
    pass


class SkillHubRegistry:
    """Global discovery metadata; package bytes stay in the existing CAS."""

    def __init__(
        self, database_path: str | Path, *, author_key: bytes, initialize: bool = True
    ) -> None:
        if len(author_key) < 32:
            raise ValueError("Skill Hub author key is too short")
        self.path = Path(database_path)
        self.author_key = bytes(author_key)
        if initialize:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        elif not self.path.is_file():
            raise RuntimeError("Skill Hub database is unavailable")
        with self._connection() as connection:
            if initialize:
                connection.executescript(
                    """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS skill_hub_skills (
                    slug TEXT PRIMARY KEY,
                    latest_version TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS skill_hub_versions (
                    slug TEXT NOT NULL REFERENCES skill_hub_skills(slug),
                    version TEXT NOT NULL,
                    package_sha256 TEXT NOT NULL,
                    package_size_bytes INTEGER NOT NULL,
                    title TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    category TEXT NOT NULL,
                    tags_json TEXT NOT NULL,
                    uploader_nickname TEXT NOT NULL,
                    author_ref TEXT NOT NULL,
                    original_platform TEXT,
                    original_url TEXT,
                    published_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY(slug, version),
                    UNIQUE(package_sha256)
                );
                CREATE TRIGGER IF NOT EXISTS skill_hub_versions_no_update
                BEFORE UPDATE ON skill_hub_versions BEGIN
                    SELECT RAISE(ABORT, 'Skill Hub versions are immutable');
                END;
                CREATE TRIGGER IF NOT EXISTS skill_hub_versions_no_delete
                BEFORE DELETE ON skill_hub_versions BEGIN
                    SELECT RAISE(ABORT, 'Skill Hub versions are immutable');
                END;
                CREATE TABLE IF NOT EXISTS skill_hub_install_intents (
                    intent_id TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL,
                    account_ref TEXT NOT NULL,
                    slug TEXT NOT NULL,
                    version TEXT NOT NULL,
                    package_sha256 TEXT NOT NULL,
                    client_request_id TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('created','claimed','installed','failed')),
                    claimed_at TEXT,
                    completed_at TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS skill_hub_install_logs (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    intent_id TEXT NOT NULL,
                    account_ref TEXT NOT NULL,
                    slug TEXT NOT NULL,
                    version TEXT NOT NULL,
                    package_sha256 TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('created','claimed','installed','failed')),
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TRIGGER IF NOT EXISTS skill_hub_install_logs_no_update
                BEFORE UPDATE ON skill_hub_install_logs BEGIN
                    SELECT RAISE(ABORT, 'Skill Hub install logs are immutable');
                END;
                CREATE TRIGGER IF NOT EXISTS skill_hub_install_logs_no_delete
                BEFORE DELETE ON skill_hub_install_logs BEGIN
                    SELECT RAISE(ABORT, 'Skill Hub install logs are immutable');
                END;
                    """
                )
            else:
                objects = {
                    (row[0], row[1])
                    for row in connection.execute(
                        "SELECT type,name FROM sqlite_master WHERE name IN "
                        "('skill_hub_skills','skill_hub_versions',"
                        "'skill_hub_versions_no_update','skill_hub_versions_no_delete',"
                        "'skill_hub_install_intents','skill_hub_install_logs',"
                        "'skill_hub_install_logs_no_update','skill_hub_install_logs_no_delete')"
                    )
                }
                if objects != {
                    ("table", "skill_hub_skills"),
                    ("table", "skill_hub_versions"),
                    ("trigger", "skill_hub_versions_no_update"),
                    ("trigger", "skill_hub_versions_no_delete"),
                    ("table", "skill_hub_install_intents"),
                    ("table", "skill_hub_install_logs"),
                    ("trigger", "skill_hub_install_logs_no_update"),
                    ("trigger", "skill_hub_install_logs_no_delete"),
                }:
                    raise RuntimeError("Skill Hub schema is unavailable")

    def author_ref(self, account_id: str) -> str:
        if not account_id:
            raise ValueError("Skill Hub publisher account is required")
        digest = hmac.new(self.author_key, account_id.encode("utf-8"), hashlib.sha256).hexdigest()
        return "author_" + digest[:24]

    def publish(
        self,
        *,
        account_id: str,
        nickname: str,
        slug: str,
        version: str,
        title: str,
        summary: str,
        category: SkillHubCategory,
        tags: Sequence[str],
        package_sha256: str,
        package_size_bytes: int,
        original_platform: str | None = None,
        original_url: str | None = None,
    ) -> SkillHubCardProjection:
        normalized_tags = tuple(sorted(set(tags)))
        nickname = _public_nickname(account_id, nickname)
        if (
            not _SLUG.fullmatch(slug)
            or slug in EXCLUDED_SKILL_SLUGS
            or not title.strip()
            or not summary.strip()
            or len(title) > 128
            or len(summary) > 2048
            or category not in {"third_party", "content_creation", "office_productivity"}
            or len(normalized_tags) != len(tags)
            or len(normalized_tags) > 32
            or any(not _TAG.fullmatch(tag) for tag in normalized_tags)
            or not _SHA256.fullmatch(package_sha256)
            or not 1 <= package_size_bytes <= 64 * 1024 * 1024
            or len(nickname) > 64
        ):
            raise ValueError("Skill Hub publication metadata is invalid")
        parse_semver(version)
        author_ref = self.author_ref(account_id)
        try:
            with self._connection() as connection:
                existing = connection.execute(
                    "SELECT * FROM skill_hub_versions WHERE slug=? AND version=?",
                    (slug, version),
                ).fetchone()
                if existing is not None:
                    expected = (
                        package_sha256, package_size_bytes, title.strip(), summary.strip(), category,
                        json.dumps(normalized_tags, ensure_ascii=False, separators=(",", ":")),
                        nickname, author_ref, original_platform, original_url,
                    )
                    observed = tuple(
                        existing[key]
                        for key in (
                            "package_sha256", "package_size_bytes", "title", "summary", "category",
                            "tags_json", "uploader_nickname", "author_ref", "original_platform", "original_url",
                        )
                    )
                    if observed != expected:
                        raise SkillHubConflict("Skill Hub slug/version already exists")
                    return self.get(slug, version=version)
                current = connection.execute(
                    "SELECT latest_version FROM skill_hub_skills WHERE slug = ?", (slug,)
                ).fetchone()
                if current is None:
                    connection.execute(
                        "INSERT INTO skill_hub_skills(slug,latest_version) VALUES (?,?)",
                        (slug, version),
                    )
                connection.execute(
                    "INSERT INTO skill_hub_versions(slug,version,package_sha256,package_size_bytes,"
                    "title,summary,category,tags_json,uploader_nickname,author_ref,original_platform,original_url) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        slug, version, package_sha256, package_size_bytes, title.strip(), summary.strip(),
                        category, json.dumps(normalized_tags, ensure_ascii=False, separators=(",", ":")),
                        nickname, author_ref, original_platform, original_url,
                    ),
                )
                if current is not None and parse_semver(version) > parse_semver(str(current[0])):
                    connection.execute(
                        "UPDATE skill_hub_skills SET latest_version=?,updated_at=CURRENT_TIMESTAMP WHERE slug=?",
                        (version, slug),
                    )
        except sqlite3.IntegrityError as error:
            raise SkillHubConflict("Skill Hub slug/version or package digest already exists") from error
        return self.get(slug, version=version)

    def get(self, slug: str, *, version: str | None = None) -> SkillHubCardProjection:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT v.* "
                "FROM skill_hub_skills s JOIN skill_hub_versions v ON v.slug=s.slug "
                "AND v.version=COALESCE(?,s.latest_version) WHERE s.slug=?",
                (version, slug),
            ).fetchone()
        if row is None:
            raise KeyError(slug)
        return self._card(row)

    def list(
        self,
        *,
        query: str = "",
        category: SkillHubCategory | None = None,
        tag: str | None = None,
        source: str | None = None,
        cursor: str | None = None,
        limit: int = 24,
    ) -> tuple[SkillHubCardProjection, ...]:
        if not 1 <= limit <= 100 or (cursor is not None and not _SLUG.fullmatch(cursor)):
            raise ValueError("Skill Hub cursor or limit is invalid")
        clauses = ["s.slug > ?", "(? = '' OR s.slug LIKE ? ESCAPE '\\' OR v.title LIKE ? ESCAPE '\\' OR v.summary LIKE ? ESCAPE '\\')"]
        escaped = query.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = f"%{escaped}%"
        values: list[object] = [cursor or "", query.strip(), pattern, pattern, pattern]
        if category is not None:
            if category not in {"third_party", "content_creation", "office_productivity"}:
                raise ValueError("Skill Hub category is invalid")
            clauses.append("v.category = ?")
            values.append(category)
        if tag is not None:
            if not _TAG.fullmatch(tag):
                raise ValueError("Skill Hub tag is invalid")
            clauses.append("instr(v.tags_json, ?) > 0")
            values.append(json.dumps(tag, ensure_ascii=False))
        if source is not None:
            if not _SOURCE.fullmatch(source):
                raise ValueError("Skill Hub source is invalid")
            clauses.append("v.original_platform = ?")
            values.append(source)
        values.append(limit)
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT v.* "
                "FROM skill_hub_skills s JOIN skill_hub_versions v ON v.slug=s.slug "
                "AND v.version=s.latest_version WHERE " + " AND ".join(clauses) +
                " ORDER BY s.slug LIMIT ?",
                values,
            ).fetchall()
        return tuple(self._card(row) for row in rows)

    def detail(self, slug: str) -> SkillHubDetailProjection:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM skill_hub_versions WHERE slug=?", (slug,)
            ).fetchall()
        if not rows:
            raise KeyError(slug)
        versions = sorted(
            (self._card(row) for row in rows),
            key=lambda card: parse_semver(card.version),
            reverse=True,
        )
        return SkillHubDetailProjection(skill=versions[0], versions=versions)

    def create_install_intent(
        self,
        *,
        account_id: str,
        slug: str,
        version: str,
        package_sha256: str,
        client_request_id: str,
        ttl_seconds: int = 300,
        now: datetime | None = None,
    ) -> dict[str, object]:
        card = self.get(slug, version=version)
        if (
            not hmac.compare_digest(card.package_sha256, package_sha256)
            or not 30 <= ttl_seconds <= 900
            or not 8 <= len(client_request_id) <= 192
        ):
            raise ValueError("Skill Hub install intent is invalid")
        issued = (now or datetime.now(UTC)).astimezone(UTC)
        intent_id = "intent_" + secrets.token_hex(16)
        expires_at = issued + timedelta(seconds=ttl_seconds)
        account_ref = self.author_ref(account_id)
        payload = {
            "schema_version": 1,
            "intent_id": intent_id,
            "account_ref": account_ref,
            "slug": slug,
            "version": version,
            "package_sha256": package_sha256,
            "expires_at": expires_at.isoformat(),
        }
        token = self._signed_token("install", payload)
        with self._connection() as connection:
            connection.execute(
                "INSERT INTO skill_hub_install_intents(intent_id,account_id,account_ref,slug,version,"
                "package_sha256,client_request_id,expires_at,status) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    intent_id, account_id, account_ref, slug, version, package_sha256,
                    client_request_id, expires_at.isoformat(), "created",
                ),
            )
            self._append_install_log(connection, payload, "created")
        return {"schema_version": 1, "install_intent": token, **payload}

    def consume_install_intent(
        self,
        *,
        account_id: str,
        install_intent: str,
        now: datetime | None = None,
    ) -> dict[str, object]:
        payload = self._verify_token("install", install_intent)
        if (
            set(payload)
            != {
                "schema_version", "intent_id", "account_ref", "slug", "version",
                "package_sha256", "expires_at",
            }
            or payload.get("schema_version") != 1
            or _INTENT_ID.fullmatch(str(payload.get("intent_id", ""))) is None
            or _SLUG.fullmatch(str(payload.get("slug", ""))) is None
            or _SHA256.fullmatch(str(payload.get("package_sha256", ""))) is None
        ):
            raise ValueError("Skill Hub install intent payload is invalid")
        current = (now or datetime.now(UTC)).astimezone(UTC)
        if payload.get("account_ref") != self.author_ref(account_id):
            raise SkillHubConflict("Skill Hub install intent account changed")
        try:
            expires_at = datetime.fromisoformat(str(payload["expires_at"])).astimezone(UTC)
        except (KeyError, ValueError):
            raise ValueError("Skill Hub install intent expiry is invalid") from None
        if current >= expires_at:
            raise SkillHubConflict("Skill Hub install intent expired")
        intent_id = str(payload.get("intent_id", ""))
        try:
            with self._connection() as connection:
                row = connection.execute(
                    "SELECT * FROM skill_hub_install_intents WHERE intent_id=?", (intent_id,)
                ).fetchone()
                expected = (
                    account_id, payload.get("account_ref"), payload.get("slug"),
                    payload.get("version"), payload.get("package_sha256"),
                    payload.get("expires_at"), "created",
                )
                if row is None or tuple(
                    row[key] for key in (
                        "account_id", "account_ref", "slug", "version",
                        "package_sha256", "expires_at", "status",
                    )
                ) != expected:
                    raise SkillHubConflict("Skill Hub install intent was already consumed")
                changed = connection.execute(
                    "UPDATE skill_hub_install_intents SET status='claimed',claimed_at=? "
                    "WHERE intent_id=? AND status='created'",
                    (current.isoformat(), intent_id),
                )
                if changed.rowcount != 1:
                    raise SkillHubConflict("Skill Hub install intent was already consumed")
                self._append_install_log(connection, payload, "claimed")
        except sqlite3.OperationalError as error:
            raise SkillHubConflict("Skill Hub install intent is busy") from error
        completion = self._signed_token(
            "completion",
            {"schema_version": 1, "intent_id": intent_id, "account_ref": payload["account_ref"]},
        )
        return {"schema_version": 1, **payload, "completion_receipt": completion}

    def complete_install_intent(
        self,
        *,
        account_id: str,
        completion_receipt: str,
        status: Literal["installed", "failed"],
        now: datetime | None = None,
    ) -> None:
        receipt = self._verify_token("completion", completion_receipt)
        if (
            set(receipt) != {"schema_version", "intent_id", "account_ref"}
            or receipt.get("schema_version") != 1
            or _INTENT_ID.fullmatch(str(receipt.get("intent_id", ""))) is None
        ):
            raise ValueError("Skill Hub completion receipt is invalid")
        intent_id = str(receipt.get("intent_id", ""))
        current = (now or datetime.now(UTC)).astimezone(UTC)
        try:
            with self._connection() as connection:
                row = connection.execute(
                    "SELECT * FROM skill_hub_install_intents WHERE intent_id=?", (intent_id,)
                ).fetchone()
                if (
                    row is None
                    or row["account_id"] != account_id
                    or row["account_ref"] != receipt.get("account_ref")
                    or row["status"] != "claimed"
                ):
                    raise SkillHubConflict("Skill Hub install completion is invalid")
                changed = connection.execute(
                    "UPDATE skill_hub_install_intents SET status=?,completed_at=? "
                    "WHERE intent_id=? AND status='claimed'",
                    (status, current.isoformat(), intent_id),
                )
                if changed.rowcount != 1:
                    raise SkillHubConflict("Skill Hub install completion is invalid")
                self._append_install_log(connection, dict(row), status)
        except sqlite3.OperationalError as error:
            raise SkillHubConflict("Skill Hub install completion is busy") from error

    def install_logs(self, intent_id: str) -> tuple[str, ...]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT status FROM skill_hub_install_logs WHERE intent_id=? ORDER BY seq",
                (intent_id,),
            ).fetchall()
        return tuple(str(row[0]) for row in rows)

    def _signed_token(self, purpose: str, payload: dict[str, object]) -> str:
        body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        signature = hmac.new(self.author_key, purpose.encode() + b"\0" + body, hashlib.sha256).digest()
        return base64.urlsafe_b64encode(body + signature).decode().rstrip("=")

    def _verify_token(self, purpose: str, token: str) -> dict[str, object]:
        if not isinstance(token, str) or not 64 <= len(token) <= 4096:
            raise ValueError("Skill Hub install token is invalid")
        try:
            raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
            body, signature = raw[:-32], raw[-32:]
            expected = hmac.new(
                self.author_key, purpose.encode() + b"\0" + body, hashlib.sha256
            ).digest()
            payload = json.loads(body.decode())
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
            raise ValueError("Skill Hub install token is invalid") from None
        if not hmac.compare_digest(signature, expected) or not isinstance(payload, dict):
            raise ValueError("Skill Hub install token is invalid")
        return payload

    @staticmethod
    def _append_install_log(connection, payload, status: str) -> None:
        connection.execute(
            "INSERT INTO skill_hub_install_logs(intent_id,account_ref,slug,version,"
            "package_sha256,status) VALUES (?,?,?,?,?,?)",
            (
                payload["intent_id"], payload["account_ref"], payload.get("slug", ""),
                payload.get("version", ""), payload.get("package_sha256", ""), status,
            ),
        )

    @contextmanager
    def _connection(self):
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _card(row: sqlite3.Row) -> SkillHubCardProjection:
        return SkillHubCardProjection(
            slug=row["slug"],
            title=row["title"],
            summary=row["summary"],
            version=row["version"],
            package_sha256=row["package_sha256"],
            package_size_bytes=row["package_size_bytes"],
            tags=json.loads(row["tags_json"]),
            category=row["category"],
            uploader=SkillHubUploaderProjection(
                nickname=row["uploader_nickname"], author_ref=row["author_ref"]
            ),
            provenance=SkillProvenance(
                original_platform=row["original_platform"], original_url=row["original_url"]
            ),
        )


def _public_nickname(account_id: str, nickname: str) -> str:
    normalized = " ".join(
        re.sub(r"[\x00-\x1f\x7f]+", " ", str(nickname)).split()
    )
    if (
        not normalized
        or normalized.casefold() == account_id.casefold()
        or _EMAIL.search(normalized)
    ):
        return "e-Mate 用户"
    return normalized[:64]


class SkillHubUploadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    slug: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,95}$")
    category: SkillHubCategory
    bundle_base64: str = Field(min_length=4, max_length=96 * 1024 * 1024)
    client_request_id: str = Field(min_length=8, max_length=192)


class SkillHubInstallIntentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    package_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    client_request_id: str = Field(min_length=8, max_length=192)


class SkillHubInstallConsumeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    install_intent: str = Field(min_length=64, max_length=4096)


class SkillHubInstallCompleteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    completion_receipt: str = Field(min_length=64, max_length=4096)
    status: Literal["installed", "failed"]


def create_skill_hub_router(
    registry: SkillHubRegistry,
    bundle_store: LocalSkillBundleStore,
    *,
    principal_dependency,
    nickname_resolver,
) -> APIRouter:
    """Expose the authenticated global Hub without a second identity system."""

    import base64
    import binascii
    import io
    import zipfile

    router = APIRouter(prefix="/ecorex-agent/client/skill-hub/v1", tags=["skill-hub"])

    @router.get("/skills")
    def skills(
        query: str = Query(default="", max_length=128),
        category: SkillHubCategory | None = None,
        tag: str | None = Query(default=None, pattern=r"^[a-z0-9][a-z0-9._-]{0,63}$"),
        source: str | None = Query(default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9._ -]{0,63}$"),
        cursor: str | None = None,
        limit: int = Query(default=24, ge=1, le=100),
        _principal=Depends(principal_dependency),
    ) -> dict:
        items = registry.list(
            query=query, category=category, tag=tag, source=source,
            cursor=cursor, limit=limit,
        )
        return {
            "schema_version": 1,
            "items": [item.model_dump(mode="json") for item in items],
            "next_cursor": items[-1].slug if len(items) == limit else None,
        }

    @router.get("/skills/{slug}")
    def detail(slug: str, _principal=Depends(principal_dependency)) -> SkillHubDetailProjection:
        try:
            return registry.detail(slug)
        except KeyError:
            raise HTTPException(status_code=404, detail="Skill was not found") from None

    @router.post("/skills", status_code=201)
    def upload(
        request: SkillHubUploadRequest,
        principal=Depends(principal_dependency),
    ) -> SkillHubCardProjection:
        try:
            payload = base64.b64decode(request.bundle_base64, validate=True)
        except (binascii.Error, ValueError):
            raise HTTPException(status_code=422, detail="Skill package is not canonical base64") from None
        if base64.b64encode(payload).decode("ascii") != request.bundle_base64:
            raise HTTPException(status_code=422, detail="Skill package is not canonical base64")
        try:
            bundle = bundle_store.ingest_zip(payload)
            return registry.publish(
                account_id=principal.account_id,
                nickname=nickname_resolver(principal.account_id),
                slug=request.slug,
                version=bundle.metadata.version,
                title=bundle.metadata.name,
                summary=bundle.metadata.description,
                category=request.category,
                tags=bundle.metadata.tags,
                package_sha256=bundle.artifact_sha256,
                package_size_bytes=len(payload),
            )
        except SkillHubConflict as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except (ValueError, RuntimeError) as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @router.get("/skills/{slug}/versions/{version}/package")
    def package(
        slug: str,
        version: str,
        _principal=Depends(principal_dependency),
    ) -> Response:
        try:
            card = registry.get(slug, version=version)
            bundle = bundle_store.verify(card.package_sha256)
        except (KeyError, RuntimeError):
            raise HTTPException(status_code=404, detail="Skill package was not found") from None
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for record in bundle.files:
                info = zipfile.ZipInfo(record.path, date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o100644 << 16
                archive.writestr(
                    info,
                    bundle_store.read_verified_file(card.package_sha256, record.path),
                )
        return Response(
            output.getvalue(),
            media_type="application/zip",
            headers={"X-Skill-Content-SHA256": card.package_sha256},
        )

    @router.post("/skills/{slug}/versions/{version}/install-intent")
    def create_install_intent(
        slug: str,
        version: str,
        request: SkillHubInstallIntentRequest,
        principal=Depends(principal_dependency),
    ) -> dict[str, object]:
        try:
            return registry.create_install_intent(
                account_id=principal.account_id,
                slug=slug,
                version=version,
                package_sha256=request.package_sha256,
                client_request_id=request.client_request_id,
            )
        except KeyError:
            raise HTTPException(status_code=404, detail="Skill was not found") from None
        except (ValueError, SkillHubConflict) as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @router.post("/install-intents/consume")
    def consume_install_intent(
        request: SkillHubInstallConsumeRequest,
        principal=Depends(principal_dependency),
    ) -> dict[str, object]:
        try:
            return registry.consume_install_intent(
                account_id=principal.account_id,
                install_intent=request.install_intent,
            )
        except (ValueError, SkillHubConflict) as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @router.post("/install-intents/complete")
    def complete_install_intent(
        request: SkillHubInstallCompleteRequest,
        principal=Depends(principal_dependency),
    ) -> dict[str, object]:
        try:
            registry.complete_install_intent(
                account_id=principal.account_id,
                completion_receipt=request.completion_receipt,
                status=request.status,
            )
        except (ValueError, SkillHubConflict) as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        return {"schema_version": 1, "status": request.status}

    return router


__all__ = ["SkillHubConflict", "SkillHubRegistry", "create_skill_hub_router"]
