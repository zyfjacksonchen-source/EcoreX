from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import sqlite3
from urllib.parse import urlsplit

import pytest

from ecorex.control_plane import (
    CloudShareConflict,
    CloudShareKeyRing,
    CloudShareNotFound,
    CloudShareRepository,
    CloudShareSchemaManager,
    CloudShareSchemaError,
)
from ecorex.sharing import SharePayload, SharedMessage


class Clock:
    def __init__(self) -> None:
        self.value = datetime(2026, 7, 10, 15, 34, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.value


def payload(suffix: str, clock: Clock) -> SharePayload:
    return SharePayload(
        schema_version=2,
        share_id="shr_" + suffix * 32,
        thread_id="thread-" + suffix,
        source_watermark=1,
        messages=[
            SharedMessage(
                item_id="item-" + suffix,
                turn_id="turn-" + suffix,
                role="assistant",
                text="share " + suffix,
                created_at=clock.value,
            )
        ],
        created_at=clock.value,
        expires_at=clock.value + timedelta(days=1),
    )


def repository(path, clock: Clock, *, active: str, keys: dict[str, bytes]):
    keyring = CloudShareKeyRing(active_key_id=active, keys=keys)
    CloudShareSchemaManager(path, keyring=keyring).migrate()
    return CloudShareRepository(
        path,
        keyring=keyring,
        public_base_url="https://share.ecorex.test/s",
        clock=clock,
    )


def token(public_url: str) -> str:
    return urlsplit(public_url).path.rsplit("/", 1)[-1]


def seed_pre_keyring_database(path, clock: Clock, key: bytes) -> tuple[SharePayload, str]:
    """Build the exact v1 storage shape to exercise the in-place annotation path."""

    item = payload("9", clock)
    account_id = "account-1"
    remote_id = "cshr_" + "9" * 32
    created_at = clock.value.isoformat()
    expires_at = item.expires_at.isoformat()
    raw_token = base64.urlsafe_b64encode(
        hmac.new(
            key,
            b"ecorex-cloud-share-token-v1\n"
            + account_id.encode()
            + b"\0"
            + item.share_id.encode("ascii"),
            hashlib.sha256,
        ).digest()
    ).decode("ascii").rstrip("=")
    token_sha256 = hashlib.sha256(raw_token.encode("ascii")).hexdigest()
    state = (
        f"ecorex-cloud-share-state-v1\n{remote_id}\0{account_id}\0"
        f"{item.share_id}\0{item.thread_id}\0{item.source_watermark}\0{item.sha256}\0"
        f"{token_sha256}\0active\0{expires_at}\0{created_at}\0"
    ).encode()
    state_mac = hmac.new(key, state, hashlib.sha256).hexdigest()
    event_id = "csaud_" + "9" * 32
    previous = "0" * 64
    audit = (
        f"ecorex-cloud-share-audit-v1\n1\0{event_id}\0{account_id}\0"
        f"share.publish\0{remote_id}\0{item.sha256}\0{previous}\0{created_at}"
    ).encode()
    entry_digest = hmac.new(
        key,
        b"ecorex-cloud-share-audit-mac-v1\n" + audit,
        hashlib.sha256,
    ).hexdigest()
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE cloud_share_snapshots (
                remote_snapshot_id TEXT PRIMARY KEY, account_id TEXT NOT NULL,
                source_share_id TEXT NOT NULL, thread_id TEXT NOT NULL,
                source_watermark INTEGER NOT NULL, payload_json TEXT NOT NULL,
                payload_sha256 TEXT NOT NULL, token_sha256 TEXT NOT NULL UNIQUE,
                state_mac TEXT NOT NULL, status TEXT NOT NULL, expires_at TEXT NOT NULL,
                created_at TEXT NOT NULL, revoked_at TEXT,
                UNIQUE(account_id, source_share_id));
            CREATE TRIGGER cloud_share_identity_immutable
            BEFORE UPDATE OF remote_snapshot_id, account_id, source_share_id,
                thread_id, source_watermark, payload_json, payload_sha256,
                token_sha256, expires_at, created_at
            ON cloud_share_snapshots BEGIN
                SELECT RAISE(ABORT, 'cloud share identity is immutable'); END;
            CREATE TRIGGER cloud_share_status_transition
            BEFORE UPDATE OF status, revoked_at ON cloud_share_snapshots
            WHEN NOT (OLD.status='active' AND NEW.status='revoked'
                AND OLD.revoked_at IS NULL AND NEW.revoked_at IS NOT NULL)
            BEGIN SELECT RAISE(ABORT, 'cloud share status transition is invalid'); END;
            CREATE TRIGGER cloud_share_snapshots_no_delete BEFORE DELETE
            ON cloud_share_snapshots BEGIN
                SELECT RAISE(ABORT, 'cloud share snapshots are append-only'); END;
            CREATE TABLE cloud_share_audit (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL UNIQUE,
                account_id TEXT NOT NULL, action TEXT NOT NULL, target_id TEXT NOT NULL,
                payload_sha256 TEXT NOT NULL, previous_digest TEXT NOT NULL,
                entry_digest TEXT NOT NULL, created_at TEXT NOT NULL);
            CREATE TRIGGER cloud_share_audit_no_update BEFORE UPDATE ON cloud_share_audit
            BEGIN SELECT RAISE(ABORT, 'cloud share audit is append-only'); END;
            CREATE TRIGGER cloud_share_audit_no_delete BEFORE DELETE ON cloud_share_audit
            BEGIN SELECT RAISE(ABORT, 'cloud share audit is append-only'); END;
            """
        )
        connection.execute(
            "INSERT INTO cloud_share_snapshots VALUES(?,?,?,?,?,?,?,?,?,'active',?,?,NULL)",
            (
                remote_id,
                account_id,
                item.share_id,
                item.thread_id,
                item.source_watermark,
                item.canonical_bytes().decode(),
                item.sha256,
                token_sha256,
                state_mac,
                expires_at,
                created_at,
            ),
        )
        connection.execute(
            "INSERT INTO cloud_share_audit VALUES(1,?,?,?,?,?,?,?,?)",
            (
                event_id,
                account_id,
                "share.publish",
                remote_id,
                item.sha256,
                previous,
                entry_digest,
                created_at,
            ),
        )
    return item, raw_token


def test_rotation_keeps_old_url_idempotence_revoke_expiry_and_audit_chain(tmp_path) -> None:
    database = tmp_path / "control.db"
    clock = Clock()
    first_key = b"a" * 32
    second_key = b"b" * 32
    first = repository(database, clock, active="2026-07-a", keys={"2026-07-a": first_key})
    old_payload = payload("a", clock)
    expiring_payload = payload("c", clock)
    old = first.publish("account-1", old_payload, idempotency_key=old_payload.share_id)
    expiring = first.publish(
        "account-1", expiring_payload, idempotency_key=expiring_payload.share_id
    )

    rotated = repository(
        database,
        clock,
        active="2026-07-b",
        keys={"2026-07-a": first_key, "2026-07-b": second_key},
    )
    assert rotated.resolve_public(token(old.public_url)) == old_payload
    repeated = rotated.publish(
        "account-1", old_payload, idempotency_key=old_payload.share_id
    )
    assert repeated == old

    new_payload = payload("b", clock)
    new = rotated.publish("account-1", new_payload, idempotency_key=new_payload.share_id)
    assert new.public_url != old.public_url
    assert rotated.resolve_public(token(new.public_url)) == new_payload
    rotated.revoke("account-1", old.remote_snapshot_id, idempotency_key="revoke-old")
    with pytest.raises(CloudShareNotFound):
        rotated.resolve_public(token(old.public_url))

    # Expiry remains a payload fact and does not depend on the current signing key.
    assert rotated.resolve_public(token(expiring.public_url)) == expiring_payload
    clock.value += timedelta(days=2)
    with pytest.raises(CloudShareNotFound):
        rotated.resolve_public(token(expiring.public_url))

    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        snapshot_keys = {
            row["source_share_id"]: (row["token_key_id"], row["state_mac_version"])
            for row in connection.execute(
                "SELECT source_share_id, token_key_id, state_mac_version "
                "FROM cloud_share_snapshots"
            )
        }
        audit_keys = [
            (row[0], row[1])
            for row in connection.execute(
                "SELECT key_id, mac_version FROM cloud_share_audit ORDER BY sequence"
            )
        ]
    assert snapshot_keys[old_payload.share_id] == ("2026-07-a", 2)
    assert snapshot_keys[expiring_payload.share_id] == ("2026-07-a", 2)
    assert snapshot_keys[new_payload.share_id] == ("2026-07-b", 2)
    assert audit_keys == [
        ("2026-07-a", 2),
        ("2026-07-a", 2),
        ("2026-07-b", 2),
        ("2026-07-b", 2),
    ]


def test_removed_or_unknown_historical_key_fails_closed_without_changing_urls(tmp_path) -> None:
    database = tmp_path / "control.db"
    clock = Clock()
    first_key = b"a" * 32
    second_key = b"b" * 32
    first = repository(database, clock, active="old", keys={"old": first_key})
    old_payload = payload("d", clock)
    old = first.publish("account-1", old_payload, idempotency_key=old_payload.share_id)
    rotated = repository(
        database,
        clock,
        active="new",
        keys={"old": first_key, "new": second_key},
    )
    new_payload = payload("e", clock)
    new = rotated.publish("account-1", new_payload, idempotency_key=new_payload.share_id)

    retired = repository(database, clock, active="new", keys={"new": second_key})
    assert retired.resolve_public(token(new.public_url)) == new_payload
    with pytest.raises(CloudShareConflict, match="unavailable"):
        retired.resolve_public(token(old.public_url))
    with pytest.raises(CloudShareConflict, match="unavailable"):
        retired.publish(
            "account-1", payload("f", clock), idempotency_key="shr_" + "f" * 32
        )

    # Persisted key identity is part of the immutable snapshot identity.
    with sqlite3.connect(database) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="identity is immutable"):
            connection.execute(
                "UPDATE cloud_share_snapshots SET token_key_id='new' "
                "WHERE source_share_id=?",
                (old_payload.share_id,),
            )


def test_keyring_validation_never_accepts_ambiguous_or_invalid_key_authority(tmp_path) -> None:
    with pytest.raises(ValueError, match="active key is missing"):
        CloudShareKeyRing(active_key_id="new", keys={"old": b"a" * 32})
    with pytest.raises(ValueError, match="32 bytes"):
        CloudShareKeyRing(active_key_id="new", keys={"new": b"short"})
    ring = CloudShareKeyRing(active_key_id="new", keys={"new": b"b" * 32})
    with pytest.raises(ValueError, match="either"):
        CloudShareRepository(
            tmp_path / "invalid.db",
            token_key=b"a" * 32,
            keyring=ring,
            public_base_url="https://share.ecorex.test/s",
        )


def test_populated_pre_keyring_storage_requires_and_uses_explicit_legacy_identity(
    tmp_path,
) -> None:
    database = tmp_path / "legacy.db"
    clock = Clock()
    old_key = b"o" * 32
    new_key = b"n" * 32
    old_payload, old_token = seed_pre_keyring_database(database, clock, old_key)

    ambiguous = CloudShareKeyRing(
        active_key_id="new",
        keys={"old": old_key, "new": new_key},
    )
    with pytest.raises(CloudShareSchemaError, match="migration failed"):
        CloudShareSchemaManager(
            database,
            keyring=ambiguous,
        ).migrate()

    with pytest.raises(CloudShareSchemaError):
        CloudShareSchemaManager(
            database,
            keyring=CloudShareKeyRing(
                active_key_id="new",
                legacy_key_id="new",
                keys={"old": old_key, "new": new_key},
            ),
        ).migrate()

    migrated_keyring = CloudShareKeyRing(
        active_key_id="new",
        legacy_key_id="old",
        keys={"old": old_key, "new": new_key},
    )
    CloudShareSchemaManager(database, keyring=migrated_keyring).migrate()
    migrated = CloudShareRepository(
        database,
        keyring=migrated_keyring,
        public_base_url="https://share.ecorex.test/s",
        clock=clock,
    )
    assert migrated.resolve_public(old_token) == old_payload
    fresh_payload = payload("8", clock)
    migrated.publish("account-1", fresh_payload, idempotency_key=fresh_payload.share_id)

    with sqlite3.connect(database) as connection:
        old_state = connection.execute(
            "SELECT token_key_id, state_mac_version FROM cloud_share_snapshots "
            "WHERE source_share_id=?",
            (old_payload.share_id,),
        ).fetchone()
        audit = connection.execute(
            "SELECT key_id, mac_version FROM cloud_share_audit ORDER BY sequence"
        ).fetchall()
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("UPDATE cloud_share_audit SET key_id='new' WHERE sequence=1")
    assert old_state == ("old", 1)
    assert audit == [("old", 1), ("new", 2)]
