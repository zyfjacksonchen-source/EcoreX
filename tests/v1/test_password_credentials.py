from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta
import hashlib
from pathlib import Path
import sqlite3

import pytest

import ecorex.control_plane.management as management_module
from ecorex.control_plane.management import (
    AdminManagementConflict,
    AdminManagementRepository,
    AdminPasswordAuthenticationError,
    AdminPasswordLocked,
)
from ecorex.control_plane.management_models import (
    CreateAdminUserRequest,
    UpdateAdminUserRequest,
)
from ecorex.control_plane.management_schema import AdminManagementSchemaManager
from ecorex.control_plane.models import ControlPrincipal
from ecorex.control_plane.password_credentials import (
    PBKDF2_ITERATIONS,
    dummy_password_hash,
    parse_encoded_password,
    verify_password_and_upgrade,
)
from ecorex.migration.legacy_password_credentials import (
    import_v0292_password_credentials,
)


KEY = b"p" * 32
NOW = datetime(2026, 7, 19, 9, 0, tzinfo=UTC)
ACTOR = ControlPrincipal(
    subject="password-test-admin",
    client_id="password-tests",
    account_id="admin",
    roles=frozenset({"platform_admin"}),
)


def _repository(path: Path) -> AdminManagementRepository:
    AdminManagementSchemaManager(path).migrate()
    return AdminManagementRepository(path, encryption_key=KEY)


def _request(
    account_id: str,
    email: str,
    *,
    request_id: str,
    password: str | None = None,
) -> CreateAdminUserRequest:
    return CreateAdminUserRequest(
        account_id=account_id,
        display_name=account_id,
        email=email,
        organization_id="org",
        token_limit=1000,
        image_limit=10,
        password=password,
        client_request_id=request_id,
    )


def _django_hash(password: str, *, iterations: int = 260_000) -> str:
    salt = "DjangoSalt1234567890"
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode(), salt.encode(), iterations
    )
    return (
        f"pbkdf2_sha256${iterations}${salt}$"
        + base64.b64encode(digest).decode()
    )


def _ecorex_v0292_hash(password: str) -> str:
    salt = bytes(range(16))
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode(), salt, 180_000
    )
    return (
        "pbkdf2_sha256$180000$"
        + base64.b64encode(salt).decode()
        + "$"
        + base64.b64encode(digest).decode()
    )


def _legacy_source(path: Path, rows: list[tuple[object, ...]]) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "CREATE TABLE users("
            "id TEXT PRIMARY KEY,email TEXT,status TEXT,password_hash TEXT,"
            "deleted_at TEXT)"
        )
        connection.executemany("INSERT INTO users VALUES(?,?,?,?,?)", rows)
        connection.commit()
    finally:
        connection.close()


def test_real_legacy_formats_accept_eight_and_nine_character_passwords() -> None:
    legacy_eight = _ecorex_v0292_hash("abcd1234")
    django_nine = _django_hash("abcde1234")

    legacy_metadata = parse_encoded_password(legacy_eight)
    django_metadata = parse_encoded_password(django_nine)
    assert legacy_metadata.format == "ecorex-v0.2.9.2"
    assert legacy_metadata.iterations == 180_000
    assert len(legacy_metadata.salt) == 16
    assert django_metadata.format == "django"

    verified_eight, upgraded_eight = verify_password_and_upgrade(
        "abcd1234", legacy_eight
    )
    verified_nine, upgraded_nine = verify_password_and_upgrade(
        "abcde1234", django_nine
    )
    assert verified_eight is True
    assert verified_nine is True
    assert parse_encoded_password(upgraded_eight or "").iterations == PBKDF2_ITERATIONS
    assert parse_encoded_password(upgraded_nine or "").iterations == PBKDF2_ITERATIONS
    assert parse_encoded_password(dummy_password_hash()).iterations == PBKDF2_ITERATIONS


def test_legacy_import_isolates_bad_rows_and_login_atomically_upgrades(
    tmp_path: Path,
) -> None:
    target = tmp_path / "target.db"
    repository = _repository(target)
    for account_id, email in (
        ("legacy-eight", "eight@example.com"),
        ("django-nine", "nine@example.com"),
        ("invalid", "invalid@example.com"),
    ):
        repository.create_user(
            _request(
                account_id,
                email,
                request_id=f"create-{account_id}",
            ),
            actor=ACTOR,
        )
    source = tmp_path / "legacy.db"
    _legacy_source(
        source,
        [
            (
                "legacy-eight",
                "eight@example.com",
                "active",
                _ecorex_v0292_hash("abcd1234"),
                None,
            ),
            (
                "django-nine",
                "nine@example.com",
                "active",
                _django_hash("abcde1234"),
                None,
            ),
            (
                "invalid",
                "invalid@example.com",
                "active",
                "pbkdf2_sha256$180000$not-base64$also-bad",
                None,
            ),
            (
                "deleted",
                "deleted@example.com",
                "active",
                _ecorex_v0292_hash("deleted1"),
                NOW.isoformat(),
            ),
        ],
    )

    report = import_v0292_password_credentials(source, target)
    assert report.imported == 2
    assert report.skipped_invalid == 1
    assert report.skipped_deleted == 1

    assert repository.authenticate_password(
        "eight@example.com",
        "abcd1234",
        source_ip="203.0.113.8",
        now=NOW,
    ).account_id == "legacy-eight"
    assert repository.authenticate_password(
        "django-nine",
        "abcde1234",
        source_ip="203.0.113.9",
        now=NOW,
    ).account_id == "django-nine"

    connection = sqlite3.connect(target)
    try:
        credentials = connection.execute(
            "SELECT account_id,encoded_hash,credential_version,source_version "
            "FROM admin_ops_password_credentials ORDER BY account_id"
        ).fetchall()
        audit_actions = {
            row[0]
            for row in connection.execute(
                "SELECT action FROM admin_ops_audit "
                "WHERE action LIKE 'password.login.%'"
            )
        }
    finally:
        connection.close()
    assert [row[0] for row in credentials] == ["django-nine", "legacy-eight"]
    assert all(parse_encoded_password(row[1]).iterations == PBKDF2_ITERATIONS for row in credentials)
    assert all(row[2:] == (2, "admin") for row in credentials)
    assert audit_actions == {"password.login.succeeded"}

    replay = import_v0292_password_credentials(source, target)
    assert replay.imported == 0
    assert replay.skipped_admin_reset == 2


def test_password_idempotency_fingerprint_replays_same_and_conflicts_different(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path / "target.db")
    first = _request(
        "account-1",
        "user@example.com",
        request_id="same-create-request",
        password="same-password",
    )
    created = repository.create_user(first, actor=ACTOR)
    assert repository.create_user(first, actor=ACTOR) == created
    with pytest.raises(AdminManagementConflict, match="reused"):
        repository.create_user(
            _request(
                "account-1",
                "user@example.com",
                request_id="same-create-request",
                password="other-password",
            ),
            actor=ACTOR,
        )

    update = UpdateAdminUserRequest(
        display_name="updated",
        email="user@example.com",
        organization_id="org",
        status="active",
        token_limit=2000,
        image_limit=20,
        password="updated-password",
        expected_revision=created.revision,
        client_request_id="same-update-request",
    )
    updated = repository.update_user("account-1", update, actor=ACTOR)
    assert repository.update_user("account-1", update, actor=ACTOR) == updated
    with pytest.raises(AdminManagementConflict, match="reused"):
        repository.update_user(
            "account-1",
            UpdateAdminUserRequest(
                **update.model_dump(exclude={"password"}),
                password="different-password",
            ),
            actor=ACTOR,
        )
    database_bytes = (tmp_path / "target.db").read_bytes()
    assert b"same-password" not in database_bytes
    assert b"updated-password" not in database_bytes
    assert b"different-password" not in database_bytes


def test_self_service_password_change_reauthenticates_and_never_persists_secrets(
    tmp_path: Path,
) -> None:
    path = tmp_path / "target.db"
    repository = _repository(path)
    repository.create_user(
        _request(
            "account-1",
            "user@example.com",
            request_id="create-password-user",
            password="original-password",
        ),
        actor=ACTOR,
    )
    user = ControlPrincipal(
        subject="account-1",
        client_id="ecorex-webui",
        account_id="account-1",
        roles=frozenset({"user"}),
    )
    changed = repository.change_password(
        current_password="original-password",
        new_password="replacement-password",
        client_request_id="self-password-change-0001",
        actor=user,
    )
    assert changed.status == "changed"
    assert changed.reauthentication_required is True
    assert (
        repository.change_password(
            current_password="original-password",
            new_password="replacement-password",
            client_request_id="self-password-change-0001",
            actor=user,
        )
        == changed
    )
    with pytest.raises(AdminPasswordAuthenticationError):
        repository.authenticate_password("account-1", "original-password")
    assert repository.authenticate_password(
        "account-1", "replacement-password"
    ).account_id == "account-1"
    assert repository.authenticate_password(
        "USER@EXAMPLE.COM", "replacement-password"
    ).account_id == "account-1"
    raw = path.read_bytes()
    assert b"original-password" not in raw
    assert b"replacement-password" not in raw


def test_account_and_ip_rate_limits_are_reserved_before_pbkdf2(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _repository(tmp_path / "target.db")
    monkeypatch.setattr(
        management_module,
        "verify_password_and_upgrade",
        lambda password, encoded: (False, None),
    )

    for index in range(4):
        with pytest.raises(AdminPasswordAuthenticationError):
            repository.authenticate_password(
                "same-account",
                "wrong-password",
                source_ip=f"203.0.113.{index + 1}",
                now=NOW,
            )
    with pytest.raises(AdminPasswordLocked) as account_locked:
        repository.authenticate_password(
            "same-account",
            "wrong-password",
            source_ip="203.0.113.99",
            now=NOW,
        )
    assert account_locked.value.retry_after_seconds == 900

    second = _repository(tmp_path / "target-2.db")
    for index in range(19):
        with pytest.raises(AdminPasswordAuthenticationError):
            second.authenticate_password(
                f"random-{index}",
                "wrong-password",
                source_ip="198.51.100.10",
                now=NOW,
            )
    with pytest.raises(AdminPasswordLocked) as ip_locked:
        second.authenticate_password(
            "random-19",
            "wrong-password",
            source_ip="198.51.100.10",
            now=NOW,
        )
    assert ip_locked.value.retry_after_seconds == 900


def test_password_rate_table_has_ttl_cleanup_and_fail_closed_capacity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "target.db"
    repository = _repository(target)
    connection = sqlite3.connect(target)
    try:
        connection.execute(
            "INSERT INTO admin_ops_password_failures VALUES(?,?,?,?,?,?)",
            (
                "account",
                "a" * 64,
                1,
                (NOW - timedelta(hours=2)).isoformat(),
                None,
                (NOW - timedelta(hours=2)).isoformat(),
            ),
        )
        connection.commit()
    finally:
        connection.close()
    monkeypatch.setattr(
        management_module,
        "verify_password_and_upgrade",
        lambda password, encoded: (False, None),
    )
    monkeypatch.setattr(management_module, "_PASSWORD_RATE_CAPACITY", 2)
    with pytest.raises(AdminPasswordAuthenticationError):
        repository.authenticate_password(
            "first", "wrong-password", source_ip="192.0.2.1", now=NOW
        )
    with pytest.raises(AdminPasswordLocked) as capacity:
        repository.authenticate_password(
            "second", "wrong-password", source_ip="192.0.2.1", now=NOW
        )
    assert capacity.value.retry_after_seconds == 60
    connection = sqlite3.connect(target)
    try:
        stale = connection.execute(
            "SELECT 1 FROM admin_ops_password_failures "
            "WHERE subject_sha256=?",
            ("a" * 64,),
        ).fetchone()
        count = connection.execute(
            "SELECT COUNT(*) FROM admin_ops_password_failures"
        ).fetchone()[0]
    finally:
        connection.close()
    assert stale is None
    assert count <= 2
