# EcoreX v1 managed device identity broker

## Result

The v1 Control Plane now owns the cloud side of the Runtime's existing
`HTTPSDeviceAuthorizationBroker` contract:

- `POST /v1/device/authorize` creates or replays one 15-minute challenge.
- `POST /v1/device/token` returns `pending`, `slow_down`, `denied`, `expired`,
  one exact authorized grant, or rotates a valid refresh grant.
- `GET/POST <verification URL path>` provides a no-script legacy-account
  verification page.
- `POST /v1/device/verify/legacy` supports a JSON WebUI verification flow.
- `POST /api/v1/admin/device/approve` lets a `platform_admin`/`user_admin`
  bind a challenge to an active managed account.

An authorized grant contains a 15-minute EdDSA access JWT and an Ed25519-signed
managed-session lease with an exact 72-hour lifetime. Access-token and lease
signing roles must use different key IDs and production public keys. Production
signing is performed through digest-pinned stdin-only external KMS/HSM adapters.

Runtime refresh begins before the 15-minute JWT expires. An in-process lock and
durable database claim form a single-flight boundary; refresh replay is
idempotent for the source lease, and the access/refresh pair is installed with
the same vault-first/two-phase session commit as initial login. Refresh rotates
the bearer pair and increments the lease revision without extending the
original 72-hour policy expiry. A crash after session commit is reconciled from
the active lease digest. `invalid_grant` transitions to explicit reauthorization
without deleting the last signed policy lease or its audit history.

## Persistence and secret boundary

`DeviceIdentitySchemaManager` installs durable authorities into the same
single-node Control Plane SQLite WAL database: flows, immutable initial and
refresh grants,
per-account revision high-water marks, one-way v0.2.9.2 credential mappings,
and a chained append-only audit ledger. Schema receipts and every compiled SQL
object are fingerprinted and validated before serve.

The database never stores plaintext device codes, access tokens, refresh
tokens, legacy credentials, private signing keys, or the derivation/pepper
secrets. Device and refresh credentials are deterministic HMAC derivations;
legacy credentials are stored only as a domain-separated keyed commitment.
This makes retry/restart replay stable without persisting bearer material.

## Production configuration

Enable only after schema migration, admin users, and active managed models
exist:

```text
ECOREX_CP_DEVICE_IDENTITY_ENABLED=true
ECOREX_CP_DEVICE_ISSUER=https://dl.ecoremedia.net
ECOREX_CP_DEVICE_AUDIENCE=ecorex-managed-runtime
ECOREX_CP_DEVICE_VERIFICATION_URL=https://dl.ecoremedia.net/device
ECOREX_CP_DEVICE_ALLOWED_CLIENT_IDS=ecorex-webui,ecorex-admin-web
ECOREX_CP_DEVICE_PLATFORM_ADMIN_ACCOUNT_IDS=<active account IDs, comma separated>

ECOREX_CP_DEVICE_DERIVATION_KEY_B64=<secret-provider value, 32-64 bytes>
ECOREX_CP_DEVICE_LEGACY_PEPPER_B64=<different secret-provider value, 32-64 bytes>

ECOREX_CP_DEVICE_ACCESS_SIGNER_KEY_ID=<access key id>
ECOREX_CP_DEVICE_ACCESS_SIGNER_PUBLIC_KEY_B64=<raw Ed25519 public key>
ECOREX_CP_DEVICE_ACCESS_SIGNER_EXECUTABLE=<absolute pinned executable>
ECOREX_CP_DEVICE_ACCESS_SIGNER_EXECUTABLE_SHA256=<sha256>
ECOREX_CP_DEVICE_ACCESS_SIGNER_ADAPTER=<optional absolute adapter>
ECOREX_CP_DEVICE_ACCESS_SIGNER_ADAPTER_SHA256=<required with adapter>

ECOREX_CP_DEVICE_LEASE_SIGNER_KEY_ID=<managed-session key id>
ECOREX_CP_DEVICE_LEASE_SIGNER_PUBLIC_KEY_B64=<raw Ed25519 public key>
ECOREX_CP_DEVICE_LEASE_SIGNER_EXECUTABLE=<absolute pinned executable>
ECOREX_CP_DEVICE_LEASE_SIGNER_EXECUTABLE_SHA256=<sha256>
ECOREX_CP_DEVICE_LEASE_SIGNER_ADAPTER=<optional absolute adapter>
ECOREX_CP_DEVICE_LEASE_SIGNER_ADAPTER_SHA256=<required with adapter>
```

Both public keys must be installed in their consumers: the access key in the
Model Gateway/Control Plane access-token verifier, and the lease key in Runtime
`managed_session_public_keys`.

The platform-admin allowlist is deployment configuration, not a client claim.
Every configured account must already exist and be active or composition fails.
Only allowlisted accounts receive `platform_admin`, `user_admin`, `model_admin`,
`release_admin`, and `user`; every other active account receives only `user`.
The Admin Web uses `ecorex-admin-web`, holds access and refresh tokens only in
page memory, refreshes single-flight, and returns to device login on an invalid
grant. The folded manual-token form is emergency fallback only.

Run explicit migration and validation before serve:

```text
python -m ecorex.control_plane schema migrate
python -m ecorex.control_plane schema check
python -m ecorex.control_plane serve
```

The ingress must route `/v1/device/*` and the configured verification path to
the Control Plane over trusted internal HTTP while terminating public HTTPS.
Do not enable access logs containing request bodies or query strings.

## v0.2.9.2 credential migration

Create the bounded NDJSON from the released v0.2.9.2 Admin SQLite database with
the read-only exporter. `--dry-run` emits only aggregate counts and digest:

```text
python scripts/export-v0292-legacy-identities.py \
  --database <v0.2.9.2 admin.sqlite> --dry-run

python scripts/export-v0292-legacy-identities.py \
  --database <v0.2.9.2 admin.sqlite> \
  | python -m ecorex.control_plane device legacy-import
```

Each line has the exact contract:

```json
{"account_id":"account-1","credential_sha256":"<legacy SHA-256 token commitment>","display_name":"User","email":"user@example.com","role":"member","daily_token_limit":0,"weekly_token_limit":0,"session_expires_at":"2026-07-17T12:00:00Z","source_record_sha256":"<64 lowercase hex>"}
```

The exporter opens SQLite with `mode=ro`, `nofollow=1`, `query_only=ON`, reads
only `users` and `client_sessions`, and excludes soft-deleted or disabled users
plus revoked or expired sessions. v0.2.9.2 already stores only SHA-256 token
commitments, so neither plaintext credentials nor chat data can enter the
pipeline or logs. The import is transactional and idempotent. It validates that
every account already exists in the v1 admin directory, wraps the legacy
commitment in a domain-separated keyed commitment, and reports only aggregate
imported/replayed counts. It has no code path to Runtime
Thread/session tables; deleted conversations therefore cannot be restored by
identity migration. Conversation migration remains governed independently by
the canonical v0.2.9.2 database deletion authority.

Legacy verification is limited to five failures per challenge. The fifth
failure permanently denies that challenge.

## Verification performed

```text
python -m pytest -q tests/v1/test_control_plane_device_identity.py \
  tests/v1/test_device_identity_management.py \
  tests/v1/test_managed_session_refresh.py \
  tests/v1/test_legacy_identity_export.py \
  tests/v1/test_control_plane_admin_web.py \
  tests/v1/test_managed_device_authorization.py \
  tests/v1/test_managed_session_authority.py

python -m ruff check ecorex/control_plane ecorex/session \
  tests/v1/test_control_plane_device_identity.py
# passed
```

The focused tests cover exact Runtime HTTP compatibility, idempotent begin and
approval, refresh single-flight and crash recovery, access JWT verification,
72-hour lease preservation, platform-admin allowlisting, Admin Web ephemeral
device login, plaintext absence, read-only v0.2.9.2 export/import replay,
bounded failures, and the guarantee that identity migration does not write
deleted-session state.
