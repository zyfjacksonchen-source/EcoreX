#!/usr/bin/env python3
import argparse
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, unquote, urlparse


VERSION = "0.2.2"
PASSWORD_ITERATIONS = 180000
SESSION_DAYS = 7
DEFAULT_CLIENT_EVENT_KEY = "ecorex-web-v0.2.2-web.1"
DEFAULT_COMPAT_CLIENT_EVENT_KEYS = (
    "ecorex-web-v0.2.2-web.1",
    "ecorex-web-v0.2.1-web.1",
    "ecorex-desktop-v0.2.0",
    "ecorex-desktop-v0.1.19",
    "ecorex-desktop-v0.1.10",
    "ecorex-desktop-v0.1.11",
    "ecorex-desktop-v0.1.12",
    "ecorex-desktop-v0.1.13",
    "ecorex-desktop-v0.1.14",
    "ecorex-desktop-v0.1.15",
    "ecorex-desktop-v0.1.16",
    "ecorex-desktop-v0.1.18",
    "ecorex-desktop-v0.1.17",
    "ecorex-web-v0.2.0-web.1",
    "ecorex-web-v0.1.19-web.1",
    "ecorex-web-v0.1.11-web.1",
    "ecorex-web-v0.1.12-web.1",
    "ecorex-web-v0.1.13-web.1",
    "ecorex-web-v0.1.14-web.1",
    "ecorex-web-v0.1.15-web.1",
    "ecorex-web-v0.1.16-web.1",
    "ecorex-web-v0.1.18-web.1",
    "ecorex-web-v0.1.17-web.1",
)
DEFAULT_ADMIN_USERNAME = "admin"
SYNC_DETAIL_DENY_KEYS = {
    "body",
    "blob",
    "bytes",
    "content",
    "data",
    "data_base64",
    "database64",
    "delta",
    "file_content",
    "final_text",
    "html",
    "input",
    "markdown",
    "message",
    "messages",
    "output",
    "prompt",
    "raw",
    "response",
    "text",
    "transcript",
}
SYNC_ARTIFACT_PATH_KEYS = {
    "path",
    "filepath",
    "file_path",
    "previewurl",
    "preview_url",
    "relativepath",
    "relative_path",
    "statuspath",
    "status_path",
    "thumbnailurl",
    "thumbnail_url",
    "url",
}
SYNC_PHASE2_MESSAGES_ENV = "ECOREX_SYNC_PHASE2_MESSAGES_ENABLED"
SYNC_PHASE3_ARTIFACT_FILES_ENV = "ECOREX_SYNC_PHASE3_ARTIFACT_FILES_ENABLED"
SYNC_ARTIFACT_MAX_AUTO_BYTES_ENV = "ECOREX_SYNC_ARTIFACT_MAX_AUTO_BYTES"
SYNC_ARTIFACT_CHUNK_BYTES_ENV = "ECOREX_SYNC_ARTIFACT_CHUNK_BYTES"
SYNC_ARTIFACT_BYTES_PER_SECOND_ENV = "ECOREX_SYNC_ARTIFACT_BYTES_PER_SECOND"
SYNC_MESSAGE_MAX_BATCH_ENV = "ECOREX_SYNC_MESSAGE_MAX_BATCH"
SYNC_MESSAGE_MAX_CONTENT_BYTES_ENV = "ECOREX_SYNC_MESSAGE_MAX_CONTENT_BYTES"
RUNTIME_AUDIT_EVENT_TYPES = {
    "approval.requested",
    "artifact.created",
    "artifact.limit",
    "artifact.updated",
    "assistant.delta",
    "assistant.snapshot",
    "capability.policy_blocked",
    "image_job.artifact",
    "image_job.cancelled",
    "image_job.completed",
    "image_job.failed",
    "image_job.progress",
    "image_job.started",
    "message.assistant.finalized",
    "message.finalizing",
    "model.delta",
    "permission.requested",
    "reasoning.update",
    "run.cancelled",
    "run.completed",
    "run.failed",
    "run.interrupted",
    "run.phase",
    "run.started",
    "stream.replay_gap",
    "subagent.cancelled",
    "subagent.completed",
    "subagent.failed",
    "subagent.started",
    "subagent.timeout",
    "subagent.updated",
    "tool.completed",
    "tool.deadline_extended",
    "tool.failed",
    "tool.finished",
    "tool.heartbeat",
    "tool.started",
}
RUNTIME_AUDIT_TERMINAL_EVENT_TYPES = {
    "message.assistant.finalized",
    "run.cancelled",
    "run.completed",
    "run.failed",
}
RUNTIME_AUDIT_STATUSES = {
    "blocked",
    "cancelled",
    "completed",
    "denied",
    "failed",
    "interrupted",
    "pending",
    "queued",
    "ready",
    "running",
    "stream_lost",
    "timeout",
}
RUNTIME_AUDIT_SOURCES = {
    "admin",
    "agent",
    "bridge",
    "client",
    "desktop-client",
    "image_job",
    "runtime",
    "scheduler",
    "subagent",
    "tool",
    "web",
    "web_channel",
}
RUNTIME_AUDIT_DETAIL_KEYS = {
    "action",
    "artifact_count",
    "error_type",
    "install_allowed",
    "job_id",
    "pack_id_redacted",
    "phase",
    "policy_mode",
    "policy_source",
    "retryable",
    "status",
    "terminal_reason",
    "tool",
}


class ForbiddenError(Exception):
    pass


class RateLimitError(Exception):
    pass

DEFAULT_USERS = [
    ("运营管理员", "admin@ecorex.local", "admin", "active"),
    ("广告优化师", "media@ecorex.local", "member", "active"),
    ("创意协作", "creative@ecorex.local", "member", "invited"),
]

DEFAULT_USAGE = [
    ("chat", "对话与规划", 7400),
    ("skill", "Skill 调用", 4800),
    ("mcp", "MCP 调用", 3600),
    ("preview", "文件预览", 6300),
]

DEFAULT_LOGS = [
    ("error", "Web", "EcoreX Web failure collection is ready for v0.2.2 validation.", "unread"),
]

DEFAULT_CAPABILITIES = [
    ("feishu-lark", "飞书 / Lark 连接器", "管理员预置", "约 58 MB", "建议预置"),
    ("office-pdf", "Office / PDF 解析", "首次使用安装", "约 92 MB", "可由用户安装"),
    ("browser-automation", "浏览器自动化", "管理员预置", "约 180 MB", "建议预置"),
    ("voice", "语音能力", "首次使用安装", "约 45 MB", "可由用户安装"),
    ("im-channels", "Slack / Discord / Telegram / WeChat / DingTalk", "管理员预置", "约 95 MB", "建议预置"),
    ("memory-heavy", "高级记忆与数据处理", "管理员预置", "约 120 MB", "建议预置"),
    ("model-connectors", "模型厂商 SDK", "首次使用安装", "约 80 MB", "可由用户安装"),
]

PROVIDER_CONFIG_KEYS = {
    "openai": ("open_ai_api_key", "open_ai_api_base", "openai"),
    "deepseek": ("deepseek_api_key", "deepseek_api_base", "openai"),
    "custom": ("custom_api_key", "custom_api_base", "custom"),
    "zhipu": ("zhipu_ai_api_key", "zhipu_ai_api_base", "zhipuai"),
    "moonshot": ("moonshot_api_key", "moonshot_base_url", "moonshot"),
    "doubao": ("ark_api_key", "ark_base_url", "doubao"),
    "qianfan": ("qianfan_api_key", "qianfan_api_base", "qianfan"),
    "gemini": ("gemini_api_key", "gemini_api_base", "gemini"),
    "claude": ("claude_api_key", "claude_api_base", "claudeapi"),
}


def now_dt():
    return datetime.now(timezone.utc).astimezone()


def now_iso():
    return now_dt().isoformat(timespec="seconds")


def compact_text(value, limit=500):
    if value is None:
        return ""
    text = str(value).strip()
    return text[:limit]


def normalize_image_model(value):
    model = compact_text(value or "gpt-image-2-pro", 120)
    aliases = {
        "image-2-pro": "gpt-image-2-pro",
        "image-2": "gpt-image-2",
    }
    return aliases.get(model, model)


def as_int(value, default=0, minimum=0, maximum=10_000_000_000):
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, number))


def json_dumps(value):
    return json.dumps(value or {}, ensure_ascii=False, separators=(",", ":"))


def json_loads(value, default=None):
    if not value:
        return default if default is not None else {}
    try:
        return json.loads(value)
    except Exception:
        return default if default is not None else {}


def short_hash(value, length=32):
    return hashlib.sha256(str(value or "").encode("utf-8", "ignore")).hexdigest()[:length]


def user_key_for(email, device_id=""):
    identity = compact_text(email, 180).lower() or compact_text(device_id, 180) or "anonymous"
    return short_hash(identity, 32)


def sync_safe_json(value, *, deny_keys=None, max_depth=4, string_limit=1000):
    deny = {str(key).lower().replace("-", "_") for key in (deny_keys or SYNC_DETAIL_DENY_KEYS)}

    def normalize_key(key):
        return str(key or "").lower().replace("-", "_")

    def scrub(item, depth=0):
        if depth > max_depth:
            return compact_text(type(item).__name__, 80)
        if isinstance(item, dict):
            result = {}
            for key, child in item.items():
                normalized = normalize_key(key)
                if normalized in deny:
                    result[str(key)] = "[omitted]"
                    continue
                result[str(key)] = scrub(child, depth + 1)
            return result
        if isinstance(item, list):
            return [scrub(child, depth + 1) for child in item[:32]]
        if isinstance(item, str):
            return compact_text(item, string_limit)
        if isinstance(item, (int, float, bool)) or item is None:
            return item
        return compact_text(item, string_limit)

    return scrub(value)


def runtime_audit_hash(value, length=16):
    return short_hash(value, length) if value else ""


def runtime_audit_token(value, limit=80):
    return compact_text(value, limit)


def runtime_audit_enum(value, allowed, limit=80):
    token = runtime_audit_token(value, limit)
    return token if token in allowed else ""


def runtime_audit_value_summary(value, allowed, field, limit=80):
    token = runtime_audit_enum(value, allowed, limit)
    if token:
        return {field: token}
    if value:
        return {
            field: "unknown",
            f"{field}Hash": runtime_audit_hash(value),
            f"{field}Redacted": True,
        }
    return {field: ""}


def runtime_audit_timestamp(value):
    raw = runtime_audit_token(value, 80)
    if not raw:
        return ""
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}(?:[T\s]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?)?", raw):
        return ""
    return raw


def runtime_audit_timestamp_summary(value, field):
    stamp = runtime_audit_timestamp(value)
    if stamp:
        return {field: stamp}
    if value:
        return {f"{field}Hash": runtime_audit_hash(value), f"{field}Redacted": True}
    return {field: ""}


def runtime_audit_detail_summary(value):
    detail = json_loads(value, {}) if isinstance(value, str) else (value if isinstance(value, dict) else {})
    if not isinstance(detail, dict):
        return {"redacted": True, "shape": "non-object"}
    normalized = {
        str(key): str(key or "").lower().replace("-", "_")
        for key in detail.keys()
    }
    visible_keys = sorted(
        key for key, normalized_key in normalized.items()
        if normalized_key in RUNTIME_AUDIT_DETAIL_KEYS
    )[:16]
    unknown_count = sum(1 for normalized_key in normalized.values() if normalized_key not in RUNTIME_AUDIT_DETAIL_KEYS)
    result = {
        "redacted": True,
        "shape": "object",
        "keyCount": len(detail),
    }
    if visible_keys:
        result["keys"] = visible_keys
    if unknown_count:
        result["unknownKeyCount"] = unknown_count
    return result


def sync_artifact_path_ext(path_value):
    text = str(path_value or "").strip()
    if not text:
        return ""
    try:
        parsed = urlparse(text)
        candidate = parsed.path or text
    except Exception:
        candidate = text
    _, ext = os.path.splitext(candidate)
    return compact_text(ext.lower(), 32)


def mask_secret(value):
    if not value:
        return ""
    text = str(value)
    if len(text) <= 8:
        return text[:2] + "****"
    return text[:4] + "****" + text[-4:]


def hash_token(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def canonical_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def env_flag(name, default=False):
    value = os.environ.get(name)
    if value is None:
        return default
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def admin_auth_configured():
    return bool(os.environ.get("ECOREX_ADMIN_PASSWORD") or os.environ.get("ECOREX_ADMIN_TOKEN") or os.environ.get("ECOREX_ADMIN_API_KEY"))


def admin_basic_usernames():
    raw = os.environ.get("ECOREX_ADMIN_USERNAMES", "")
    if raw:
        names = [item.strip() for item in raw.split(",") if item.strip()]
        if names:
            return names
    return [os.environ.get("ECOREX_ADMIN_USERNAME", DEFAULT_ADMIN_USERNAME)]


def constant_equal(left, right):
    if not left or not right:
        return False
    return hmac.compare_digest(str(left), str(right))


def device_id_matches(stored, provided):
    stored_text = compact_text(stored, 180)
    provided_text = compact_text(provided, 180)
    if not stored_text or not provided_text:
        return True
    try:
        stored_variants = {stored_text, quote(stored_text, safe=""), unquote(stored_text)}
        provided_variants = {provided_text, quote(provided_text, safe=""), unquote(provided_text)}
        return bool(stored_variants & provided_variants)
    except Exception:
        return stored_text == provided_text


def client_event_keys():
    raw = os.environ.get("ECOREX_CLIENT_EVENT_KEYS") or os.environ.get("ECOREX_CLIENT_EVENT_KEY", DEFAULT_CLIENT_EVENT_KEY)
    keys = [item.strip() for item in str(raw or "").split(",") if item.strip()]
    for key in DEFAULT_COMPAT_CLIENT_EVENT_KEYS:
        if key not in keys:
            keys.append(key)
    return keys


def hash_password(password):
    password = str(password or "")
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PASSWORD_ITERATIONS)
    return "pbkdf2_sha256${}${}${}".format(
        PASSWORD_ITERATIONS,
        base64.b64encode(salt).decode("ascii"),
        base64.b64encode(digest).decode("ascii"),
    )


def verify_password(password, stored):
    if not password or not stored:
        return False
    try:
        scheme, rounds, salt_b64, digest_b64 = stored.split("$", 3)
        if scheme != "pbkdf2_sha256":
            return False
        salt = base64.b64decode(salt_b64.encode("ascii"))
        expected = base64.b64decode(digest_b64.encode("ascii"))
        actual = hashlib.pbkdf2_hmac("sha256", str(password).encode("utf-8"), salt, int(rounds))
        return hmac.compare_digest(actual, expected)
    except Exception:
        return False


class AdminStore:
    def __init__(self, db_path):
        self.db_path = db_path
        db_dir = os.path.dirname(os.path.abspath(db_path))
        if db_dir:
            os.makedirs(db_dir, exist_ok=True)
        self.init_db()

    def connect(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def init_db(self):
        with self.connect() as conn:
            conn.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    email TEXT NOT NULL UNIQUE,
                    role TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS usage_events (
                    id TEXT PRIMARY KEY,
                    category TEXT NOT NULL,
                    label TEXT NOT NULL,
                    amount INTEGER NOT NULL DEFAULT 0,
                    user_email TEXT,
                    detail TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS error_logs (
                    id TEXT PRIMARY KEY,
                    level TEXT NOT NULL,
                    source TEXT NOT NULL,
                    message TEXT NOT NULL,
                    status TEXT NOT NULL,
                    user_email TEXT,
                    device_id TEXT,
                    session_id TEXT,
                    tool TEXT,
                    detail TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS capability_policy (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    mirror TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    offline_cache TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS capability_packs (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    size TEXT NOT NULL,
                    status TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id TEXT PRIMARY KEY,
                    action TEXT NOT NULL,
                    actor TEXT,
                    target TEXT,
                    detail TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS model_credentials (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    model TEXT NOT NULL,
                    bot_type TEXT NOT NULL,
                    api_base TEXT NOT NULL,
                    api_key TEXT NOT NULL,
                    scope_type TEXT NOT NULL,
                    scope_value TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS client_sessions (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    token_hash TEXT NOT NULL UNIQUE,
                    device_id TEXT,
                    app_version TEXT,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    revoked_at TEXT,
                    FOREIGN KEY(user_id) REFERENCES users(id)
                );
                CREATE TABLE IF NOT EXISTS sync_events (
                    id TEXT PRIMARY KEY,
                    sync_key TEXT NOT NULL UNIQUE,
                    event_type TEXT NOT NULL,
                    org_id TEXT,
                    user_email TEXT,
                    user_key TEXT,
                    device_id TEXT,
                    session_id TEXT,
                    request_id TEXT,
                    status TEXT,
                    source TEXT,
                    detail TEXT,
                    created_at TEXT NOT NULL,
                    ingested_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sync_artifacts (
                    id TEXT PRIMARY KEY,
                    sync_key TEXT NOT NULL UNIQUE,
                    artifact_id TEXT NOT NULL,
                    org_id TEXT,
                    user_email TEXT,
                    user_key TEXT,
                    device_id TEXT,
                    session_id TEXT,
                    request_id TEXT,
                    kind TEXT,
                    intent TEXT,
                    operation TEXT,
                    status TEXT,
                    title TEXT,
                    path_hash TEXT,
                    path_ext TEXT,
                    mime_type TEXT,
                    size_bytes INTEGER NOT NULL DEFAULT 0,
                    metadata TEXT,
                    created_at TEXT NOT NULL,
                    ingested_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sync_messages (
                    id TEXT PRIMARY KEY,
                    sync_key TEXT NOT NULL UNIQUE,
                    org_id TEXT,
                    user_email TEXT,
                    user_key TEXT,
                    device_id TEXT,
                    session_id TEXT,
                    request_id TEXT,
                    message_id TEXT,
                    seq INTEGER NOT NULL DEFAULT 0,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    content_sha256 TEXT NOT NULL,
                    content_size_bytes INTEGER NOT NULL DEFAULT 0,
                    extras TEXT,
                    created_at TEXT NOT NULL,
                    ingested_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sync_artifact_files (
                    id TEXT PRIMARY KEY,
                    sync_key TEXT NOT NULL UNIQUE,
                    artifact_id TEXT NOT NULL,
                    org_id TEXT,
                    user_email TEXT,
                    user_key TEXT,
                    device_id TEXT,
                    session_id TEXT,
                    request_id TEXT,
                    title TEXT,
                    mime_type TEXT,
                    size_bytes INTEGER NOT NULL DEFAULT 0,
                    content_sha256 TEXT NOT NULL,
                    chunk_count INTEGER NOT NULL DEFAULT 1,
                    received_chunks INTEGER NOT NULL DEFAULT 0,
                    received_bytes INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL,
                    metadata TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    ingested_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sync_artifact_file_chunks (
                    id TEXT PRIMARY KEY,
                    content_sha256 TEXT NOT NULL,
                    chunk_index INTEGER NOT NULL,
                    chunk_sha256 TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL DEFAULT 0,
                    data BLOB NOT NULL,
                    created_at TEXT NOT NULL,
                    ingested_at TEXT NOT NULL,
                    UNIQUE(content_sha256, chunk_index)
                );
                CREATE TABLE IF NOT EXISTS sync_artifact_rate_limits (
                    user_key TEXT PRIMARY KEY,
                    available_at_ms INTEGER NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )
            self.migrate(conn)
            self.seed(conn)

    def table_columns(self, conn, table):
        return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}

    def add_column(self, conn, table, name, definition):
        if name not in self.table_columns(conn, table):
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")

    def migrate(self, conn):
        self.add_column(conn, "users", "password_hash", "TEXT")
        self.add_column(conn, "users", "must_change_password", "INTEGER NOT NULL DEFAULT 0")
        self.add_column(conn, "users", "daily_token_limit", "INTEGER NOT NULL DEFAULT 0")
        self.add_column(conn, "users", "weekly_token_limit", "INTEGER NOT NULL DEFAULT 0")
        self.add_column(conn, "users", "last_login_at", "TEXT")
        self.add_column(conn, "users", "deleted_at", "TEXT")
        self.add_column(conn, "usage_events", "device_id", "TEXT")
        self.add_column(conn, "usage_events", "session_id", "TEXT")
        self.add_column(conn, "usage_events", "model", "TEXT")
        self.add_column(conn, "usage_events", "provider", "TEXT")
        self.add_column(conn, "usage_events", "input_tokens", "INTEGER NOT NULL DEFAULT 0")
        self.add_column(conn, "usage_events", "output_tokens", "INTEGER NOT NULL DEFAULT 0")
        self.add_column(conn, "usage_events", "total_tokens", "INTEGER NOT NULL DEFAULT 0")
        self.add_column(conn, "error_logs", "category", "TEXT NOT NULL DEFAULT ''")
        self.add_column(conn, "error_logs", "label", "TEXT NOT NULL DEFAULT ''")
        self.add_column(conn, "error_logs", "app_version", "TEXT")
        self.add_column(conn, "capability_packs", "preinstall", "INTEGER NOT NULL DEFAULT 0")
        self.add_column(conn, "capability_packs", "preinstall_reason", "TEXT NOT NULL DEFAULT ''")
        rate_columns = self.table_columns(conn, "sync_artifact_rate_limits")
        if rate_columns and "available_at_ms" not in rate_columns:
            conn.execute("DROP TABLE IF EXISTS sync_artifact_rate_limits")
            conn.execute(
                """
                CREATE TABLE sync_artifact_rate_limits (
                    user_key TEXT PRIMARY KEY,
                    available_at_ms INTEGER NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_users_email ON users(lower(email))")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_usage_user_time ON usage_events(lower(user_email), created_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_logs_user_device_time ON error_logs(lower(user_email), device_id, created_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_token ON client_sessions(token_hash)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sync_events_user_time ON sync_events(user_key, created_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sync_events_request ON sync_events(request_id, event_type)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sync_artifacts_user_time ON sync_artifacts(user_key, created_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sync_artifacts_request ON sync_artifacts(request_id, artifact_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sync_messages_user_time ON sync_messages(user_key, created_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sync_messages_session_seq ON sync_messages(session_id, seq)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sync_messages_request ON sync_messages(request_id, seq)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sync_artifact_files_user_time ON sync_artifact_files(user_key, created_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sync_artifact_files_request ON sync_artifact_files(request_id, artifact_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sync_artifact_files_content ON sync_artifact_files(content_sha256, status)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sync_artifact_chunks_content ON sync_artifact_file_chunks(content_sha256, chunk_index)")
        conn.commit()

    def seed(self, conn):
        created = now_iso()
        if conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0:
            seed_password = os.environ.get("ECOREX_SEED_DEFAULT_PASSWORD", "")
            if env_flag("ECOREX_SEED_DEFAULT_USERS") and seed_password:
                conn.executemany(
                    """
                    INSERT INTO users
                    (id, name, email, role, status, password_hash, must_change_password, daily_token_limit, weekly_token_limit, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?)
                    """,
                    [
                        (
                            str(uuid.uuid4()),
                            name,
                            email,
                            role,
                            status,
                            hash_password(seed_password),
                            100000,
                            500000,
                            created,
                            created,
                        )
                        for name, email, role, status in DEFAULT_USERS
                    ],
                )
        else:
            for row in conn.execute("SELECT id FROM users WHERE password_hash IS NULL OR password_hash=''").fetchall():
                conn.execute(
                    "UPDATE users SET password_hash=?, must_change_password=1, updated_at=? WHERE id=?",
                    (hash_password(secrets.token_urlsafe(18)), created, row["id"]),
                )
        if not env_flag("ECOREX_ALLOW_DEFAULT_USERS"):
            default_emails = [email for _, email, _, _ in DEFAULT_USERS]
            rows = conn.execute(
                f"SELECT id FROM users WHERE lower(email) IN ({','.join('?' for _ in default_emails)}) AND deleted_at IS NULL",
                [email.lower() for email in default_emails],
            ).fetchall()
            if rows:
                conn.executemany(
                    "UPDATE users SET status='disabled', deleted_at=?, updated_at=? WHERE id=?",
                    [(created, created, row["id"]) for row in rows],
                )
                conn.executemany(
                    "UPDATE client_sessions SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL",
                    [(created, row["id"]) for row in rows],
                )

        if conn.execute("SELECT COUNT(*) FROM usage_events").fetchone()[0] == 0:
            conn.executemany(
                "INSERT INTO usage_events (id, category, label, amount, total_tokens, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                [(str(uuid.uuid4()), category, label, amount, amount, created) for category, label, amount in DEFAULT_USAGE],
            )
        if conn.execute("SELECT COUNT(*) FROM error_logs").fetchone()[0] == 0:
            conn.executemany(
                "INSERT INTO error_logs (id, level, source, message, status, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                [(str(uuid.uuid4()), *log, created) for log in DEFAULT_LOGS],
            )
        if conn.execute("SELECT COUNT(*) FROM capability_policy").fetchone()[0] == 0:
            conn.execute(
                "INSERT INTO capability_policy (id, mirror, mode, offline_cache, updated_at) VALUES (1, ?, ?, ?, ?)",
                ("https://pypi.org/simple", "preinstall", "未配置", created),
            )
        if conn.execute("SELECT COUNT(*) FROM capability_packs").fetchone()[0] == 0:
            conn.executemany(
                "INSERT INTO capability_packs (id, name, mode, size, status, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                [(*pack, created) for pack in DEFAULT_CAPABILITIES],
            )
        conn.commit()

    def audit(self, conn, action, actor="", target="", detail=None):
        safe_detail = dict(detail or {})
        for key in ("apiKey", "api_key", "password", "initialPassword", "token"):
            safe_detail.pop(key, None)
        conn.execute(
            "INSERT INTO audit_events (id, action, actor, target, detail, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (str(uuid.uuid4()), action, compact_text(actor, 120), compact_text(target, 160), json_dumps(safe_detail), now_iso()),
        )

    def state(self, filters=None):
        filters = filters or {}
        with self.connect() as conn:
            users = [self.serialize_user(row) for row in conn.execute(
                """
                SELECT id, name, email, role, status, must_change_password, daily_token_limit, weekly_token_limit,
                       last_login_at, deleted_at, created_at, updated_at
                FROM users
                WHERE deleted_at IS NULL
                ORDER BY created_at DESC
                """
            )]
            usage_by_user = self.usage_by_user(conn)
            logs = self.query_logs(conn, filters)
            log_users = self.log_users(conn)
            sync_summary = self.sync_summary(conn)
            sync_policy = self.sync_policy()
            runtime_audit = self.runtime_audit(conn, filters)
            policy = dict(conn.execute("SELECT mirror, mode, offline_cache AS offlineCache, updated_at AS updatedAt FROM capability_policy WHERE id = 1").fetchone())
            capabilities = [dict(row) for row in conn.execute("SELECT id, name, mode, size, status, updated_at AS updatedAt FROM capability_packs ORDER BY id")]
            global_model = self.get_global_model(conn, masked=True)
            model_credentials = [global_model] if global_model else []
            total_tokens = sum(item["totalTokens"] for item in usage_by_user)
            summary = {
                "users": len(users),
                "monthlyCalls": total_tokens,
                "tokens": total_tokens,
                "errors": sum(1 for item in logs if item["level"] == "error" and item["status"] != "read"),
                "capabilities": sum(1 for item in capabilities if "建议" in item["status"]),
                "modelCredentials": 1 if global_model and global_model.get("enabled") else 0,
                "version": VERSION,
                "syncEvents": sync_summary["events"],
                "syncArtifacts": sync_summary["artifacts"],
                "syncMessages": sync_summary["messages"],
                "syncArtifactFiles": sync_summary["artifactFiles"],
                "runtimeAuditEvents": runtime_audit["summary"]["events"],
                "runtimeAuditRequests": runtime_audit["summary"]["requests"],
            }
            return {
                "ok": True,
                "version": VERSION,
                "users": users,
                "usage": self.legacy_usage(conn),
                "usageByUser": usage_by_user,
                "logs": logs,
                "logUsers": log_users,
                "capabilityPolicy": policy,
                "capabilities": capabilities,
                "globalModel": global_model,
                "modelCredentials": model_credentials,
                "syncSummary": sync_summary,
                "syncPolicy": sync_policy,
                "runtimeAudit": runtime_audit,
                "summary": summary,
            }

    def serialize_user(self, row):
        return {
            "id": row["id"],
            "name": row["name"],
            "email": row["email"],
            "role": row["role"],
            "status": row["status"],
            "mustChangePassword": bool(row["must_change_password"]),
            "dailyTokenLimit": int(row["daily_token_limit"] or 0),
            "weeklyTokenLimit": int(row["weekly_token_limit"] or 0),
            "lastLoginAt": row["last_login_at"],
            "deletedAt": row["deleted_at"],
            "createdAt": row["created_at"],
            "updatedAt": row["updated_at"],
        }

    def legacy_usage(self, conn):
        rows = conn.execute(
            """
            SELECT category, label, SUM(CASE WHEN total_tokens > 0 THEN total_tokens ELSE amount END) AS amount
            FROM usage_events
            GROUP BY category, label
            ORDER BY label
            """
        ).fetchall()
        return [
            {"category": row["category"], "label": row["label"], "value": int(row["amount"] or 0)}
            for row in rows
        ]

    def sync_summary(self, conn):
        event_row = conn.execute(
            """
            SELECT COUNT(*) AS count,
                   COUNT(DISTINCT NULLIF(session_id, '')) AS sessions,
                   COUNT(DISTINCT NULLIF(request_id, '')) AS requests,
                   MAX(ingested_at) AS last_ingested_at
            FROM sync_events
            """
        ).fetchone()
        artifact_row = conn.execute(
            """
            SELECT COUNT(*) AS count,
                   COUNT(DISTINCT NULLIF(request_id, '')) AS requests,
                   MAX(ingested_at) AS last_ingested_at
            FROM sync_artifacts
            """
        ).fetchone()
        message_row = conn.execute(
            """
            SELECT COUNT(*) AS count,
                   COUNT(DISTINCT NULLIF(session_id, '')) AS sessions,
                   COUNT(DISTINCT NULLIF(request_id, '')) AS requests,
                   MAX(ingested_at) AS last_ingested_at
            FROM sync_messages
            """
        ).fetchone()
        artifact_file_row = conn.execute(
            """
            SELECT COUNT(*) AS count,
                   SUM(CASE WHEN status='complete' THEN 1 ELSE 0 END) AS complete_count,
                   SUM(CASE WHEN status='complete' THEN size_bytes ELSE 0 END) AS complete_bytes,
                   COUNT(DISTINCT NULLIF(request_id, '')) AS requests,
                   MAX(ingested_at) AS last_ingested_at
            FROM sync_artifact_files
            """
        ).fetchone()
        artifact_chunk_row = conn.execute(
            """
            SELECT COUNT(*) AS chunks,
                   COALESCE(SUM(size_bytes), 0) AS stored_bytes
            FROM sync_artifact_file_chunks
            """
        ).fetchone()
        last_values = [
            value
            for value in (
                event_row["last_ingested_at"] if event_row else "",
                artifact_row["last_ingested_at"] if artifact_row else "",
                message_row["last_ingested_at"] if message_row else "",
                artifact_file_row["last_ingested_at"] if artifact_file_row else "",
            )
            if value
        ]
        return {
            "events": int(event_row["count"] or 0) if event_row else 0,
            "artifacts": int(artifact_row["count"] or 0) if artifact_row else 0,
            "messages": int(message_row["count"] or 0) if message_row else 0,
            "artifactFiles": int(artifact_file_row["count"] or 0) if artifact_file_row else 0,
            "artifactFilesComplete": int(artifact_file_row["complete_count"] or 0) if artifact_file_row else 0,
            "artifactFileBytes": int(artifact_file_row["complete_bytes"] or 0) if artifact_file_row else 0,
            "artifactFileChunks": int(artifact_chunk_row["chunks"] or 0) if artifact_chunk_row else 0,
            "artifactFileStoredBytes": int(artifact_chunk_row["stored_bytes"] or 0) if artifact_chunk_row else 0,
            "sessions": int(event_row["sessions"] or 0) if event_row else 0,
            "requests": int(event_row["requests"] or 0) if event_row else 0,
            "artifactRequests": int(artifact_row["requests"] or 0) if artifact_row else 0,
            "artifactFileRequests": int(artifact_file_row["requests"] or 0) if artifact_file_row else 0,
            "messageSessions": int(message_row["sessions"] or 0) if message_row else 0,
            "messageRequests": int(message_row["requests"] or 0) if message_row else 0,
            "lastIngestedAt": max(last_values) if last_values else "",
        }

    def sync_policy(self):
        phase2_enabled = env_flag(SYNC_PHASE2_MESSAGES_ENV, False)
        phase3_enabled = env_flag(SYNC_PHASE3_ARTIFACT_FILES_ENV, False)
        max_message_batch = as_int(os.environ.get(SYNC_MESSAGE_MAX_BATCH_ENV), 1000, 1, 10_000)
        max_message_content_bytes = as_int(os.environ.get(SYNC_MESSAGE_MAX_CONTENT_BYTES_ENV), 256 * 1024, 1024, 16 * 1024 * 1024)
        max_auto_bytes = as_int(os.environ.get(SYNC_ARTIFACT_MAX_AUTO_BYTES_ENV), 10 * 1024 * 1024, 0, 10 * 1024 * 1024 * 1024)
        chunk_bytes = as_int(os.environ.get(SYNC_ARTIFACT_CHUNK_BYTES_ENV), 2 * 1024 * 1024, 64 * 1024, 64 * 1024 * 1024)
        bytes_per_second = as_int(os.environ.get(SYNC_ARTIFACT_BYTES_PER_SECOND_ENV), 1024 * 1024, 0, 1024 * 1024 * 1024)
        return {
            "phase1": {
                "eventsEnabled": True,
                "artifactMetadataEnabled": True,
                "storesChatBodies": False,
                "storesArtifactFiles": False,
            },
            "phase2": {
                "chatBodiesEnabled": phase2_enabled,
                "envFlag": SYNC_PHASE2_MESSAGES_ENV,
                "implemented": True,
                "maxBatchMessages": max_message_batch,
                "maxContentBytes": max_message_content_bytes,
            },
            "phase3": {
                "artifactFilesEnabled": phase3_enabled,
                "envFlag": SYNC_PHASE3_ARTIFACT_FILES_ENV,
                "implemented": True,
                "maxAutoBytes": max_auto_bytes,
                "chunkBytes": chunk_bytes,
                "bytesPerSecond": bytes_per_second,
                "dedupe": True,
                "killSwitch": not phase3_enabled,
            },
        }

    def sync_status(self):
        with self.connect() as conn:
            return {
                "ok": True,
                "syncSummary": self.sync_summary(conn),
                "syncPolicy": self.sync_policy(),
            }

    def _runtime_audit_where(self, filters=None, *, include_event_type=True):
        filters = filters or {}
        clauses = []
        params = []
        user_email = compact_text(filters.get("userEmail") or filters.get("user_email"), 180).lower()
        device_id = compact_text(filters.get("deviceId") or filters.get("device_id"), 180)
        event_type = compact_text(filters.get("eventType") or filters.get("event_type"), 120)
        if user_email:
            clauses.append("lower(user_email) = lower(?)")
            params.append(user_email)
        if device_id:
            clauses.append("device_id = ?")
            params.append(device_id)
        if include_event_type and event_type:
            if runtime_audit_enum(event_type, RUNTIME_AUDIT_EVENT_TYPES, 120):
                clauses.append("event_type = ?")
                params.append(event_type)
            else:
                clauses.append("1 = 0")
        return (" WHERE " + " AND ".join(clauses)) if clauses else "", params

    def _runtime_audit_event_projection(self, row):
        raw_event_type = row["event_type"]
        event_type_summary = runtime_audit_value_summary(
            raw_event_type,
            RUNTIME_AUDIT_EVENT_TYPES,
            "eventType",
            120,
        )
        source_summary = runtime_audit_value_summary(row["source"], RUNTIME_AUDIT_SOURCES, "source", 80)
        status_summary = runtime_audit_value_summary(row["status"], RUNTIME_AUDIT_STATUSES, "status", 80)
        return {
            "eventHash": runtime_audit_hash("|".join([
                str(row["sync_key"] or ""),
                str(raw_event_type or ""),
                str(row["created_at"] or ""),
            ])),
            **event_type_summary,
            "requestHash": runtime_audit_hash(row["request_id"]),
            "sessionHash": runtime_audit_hash(row["session_id"]),
            "userHash": runtime_audit_hash(row["user_key"] or row["user_email"]),
            "deviceHash": runtime_audit_hash(row["device_id"]),
            **source_summary,
            **status_summary,
            **runtime_audit_timestamp_summary(row["created_at"], "createdAt"),
            "ingestedAt": row["ingested_at"],
            "detail": runtime_audit_detail_summary(row["detail"]),
            "redacted": True,
        }

    def _runtime_audit_request_projection(self, row, artifact_counts, message_counts):
        request_id = row["request_id"]
        request_key = self._runtime_audit_request_count_key(row)
        result = {
            "requestHash": runtime_audit_hash(request_id),
            "sessionHash": runtime_audit_hash(row["session_id"]),
            "userHash": runtime_audit_hash(row["user_key"]),
            "deviceHash": runtime_audit_hash(row["device_id"]),
            "eventCount": int(row["event_count"] or 0),
            "terminalEventCount": int(row["terminal_count"] or 0),
            "artifactCount": int(artifact_counts.get(request_key, 0)),
            "messageCount": int(message_counts.get(request_key, 0)),
            **runtime_audit_timestamp_summary(row["first_created_at"], "firstEventAt"),
            **runtime_audit_timestamp_summary(row["last_created_at"], "lastEventAt"),
            "lastIngestedAt": row["last_ingested_at"] or "",
            "redacted": True,
        }
        return {key: value for key, value in result.items() if value not in ("", None)}

    def _runtime_audit_request_count_key(self, row):
        return tuple(str(row[key] or "") for key in ("request_id", "session_id", "user_key", "device_id"))

    def _runtime_audit_scoped_request_count(self, conn, table, where, params):
        scope = where + (" AND " if where else " WHERE ") + "request_id IS NOT NULL AND request_id != ''"
        row = conn.execute(
            f"""
            SELECT COUNT(*) AS count
            FROM (
                SELECT request_id, session_id, user_key, device_id
                FROM {table}
                {scope}
                GROUP BY request_id, session_id, user_key, device_id
            ) scoped_requests
            """,
            params,
        ).fetchone()
        return int(row["count"] or 0) if row else 0

    def runtime_audit(self, conn, filters=None):
        filters = filters or {}
        limit = as_int(filters.get("auditLimit") or filters.get("limit"), 50, 1, 200)
        event_where, event_params = self._runtime_audit_where(filters, include_event_type=True)
        shared_where, shared_params = self._runtime_audit_where(filters, include_event_type=False)

        event_row = conn.execute(
            f"""
            SELECT COUNT(*) AS count,
                   COUNT(DISTINCT NULLIF(session_id, '')) AS sessions,
                   MAX(ingested_at) AS last_ingested_at
            FROM sync_events
            {event_where}
            """,
            event_params,
        ).fetchone()
        artifact_row = conn.execute(
            f"""
            SELECT COUNT(*) AS count
            FROM sync_artifacts
            {shared_where}
            """,
            shared_params,
        ).fetchone()
        message_row = conn.execute(
            f"""
            SELECT COUNT(*) AS count
            FROM sync_messages
            {shared_where}
            """,
            shared_params,
        ).fetchone()
        scoped_event_requests = self._runtime_audit_scoped_request_count(conn, "sync_events", event_where, event_params)
        scoped_artifact_requests = self._runtime_audit_scoped_request_count(conn, "sync_artifacts", shared_where, shared_params)
        scoped_message_requests = self._runtime_audit_scoped_request_count(conn, "sync_messages", shared_where, shared_params)

        event_type_counts = {}
        unknown_event_type_count = 0
        for row in conn.execute(
            f"SELECT event_type, COUNT(*) AS count FROM sync_events {event_where} GROUP BY event_type",
            event_params,
        ).fetchall():
            safe_type = runtime_audit_enum(row["event_type"], RUNTIME_AUDIT_EVENT_TYPES, 120) or "unknown"
            count = int(row["count"] or 0)
            event_type_counts[safe_type] = event_type_counts.get(safe_type, 0) + count
            if safe_type == "unknown":
                unknown_event_type_count += count

        source_counts = {}
        for row in conn.execute(
            f"SELECT source, COUNT(*) AS count FROM sync_events {event_where} GROUP BY source",
            event_params,
        ).fetchall():
            safe_source = runtime_audit_enum(row["source"], RUNTIME_AUDIT_SOURCES, 80) or "unknown"
            source_counts[safe_source] = source_counts.get(safe_source, 0) + int(row["count"] or 0)

        status_counts = {}
        for row in conn.execute(
            f"SELECT status, COUNT(*) AS count FROM sync_events {event_where} GROUP BY status",
            event_params,
        ).fetchall():
            safe_status = runtime_audit_enum(row["status"], RUNTIME_AUDIT_STATUSES, 80) or "unknown"
            status_counts[safe_status] = status_counts.get(safe_status, 0) + int(row["count"] or 0)

        terminal_literals = ",".join(f"'{item}'" for item in sorted(RUNTIME_AUDIT_TERMINAL_EVENT_TYPES))
        request_rows = conn.execute(
            f"""
            SELECT request_id,
                   session_id,
                   user_key,
                   device_id,
                   COUNT(*) AS event_count,
                   SUM(CASE WHEN event_type IN ({terminal_literals}) THEN 1 ELSE 0 END) AS terminal_count,
                   MIN(created_at) AS first_created_at,
                   MAX(created_at) AS last_created_at,
                   MAX(ingested_at) AS last_ingested_at
            FROM sync_events
            {event_where + (' AND ' if event_where else ' WHERE ')}request_id IS NOT NULL AND request_id != ''
            GROUP BY request_id, session_id, user_key, device_id
            ORDER BY MAX(ingested_at) DESC
            LIMIT ?
            """,
            [*event_params, min(limit, 50)],
        ).fetchall()
        request_ids = list(dict.fromkeys(row["request_id"] for row in request_rows if row["request_id"]))
        artifact_counts = {}
        message_counts = {}
        if request_ids:
            placeholders = ",".join("?" for _ in request_ids)
            count_scope = shared_where + (" AND " if shared_where else " WHERE ") + f"request_id IN ({placeholders})"
            artifact_counts = {
                self._runtime_audit_request_count_key(row): int(row["count"] or 0)
                for row in conn.execute(
                    f"""
                    SELECT request_id, session_id, user_key, device_id, COUNT(*) AS count
                    FROM sync_artifacts
                    {count_scope}
                    GROUP BY request_id, session_id, user_key, device_id
                    """,
                    [*shared_params, *request_ids],
                ).fetchall()
            }
            message_counts = {
                self._runtime_audit_request_count_key(row): int(row["count"] or 0)
                for row in conn.execute(
                    f"""
                    SELECT request_id, session_id, user_key, device_id, COUNT(*) AS count
                    FROM sync_messages
                    {count_scope}
                    GROUP BY request_id, session_id, user_key, device_id
                    """,
                    [*shared_params, *request_ids],
                ).fetchall()
            }

        recent_rows = conn.execute(
            f"""
            SELECT sync_key, event_type, user_email, user_key, device_id, session_id, request_id,
                   status, source, detail, created_at, ingested_at
            FROM sync_events
            {event_where}
            ORDER BY ingested_at DESC, created_at DESC
            LIMIT ?
            """,
            [*event_params, limit],
        ).fetchall()

        event_count = int(event_row["count"] or 0) if event_row else 0
        terminal_count = sum(
            count for event_type, count in event_type_counts.items()
            if event_type in RUNTIME_AUDIT_TERMINAL_EVENT_TYPES
        )
        capability_block_count = int(event_type_counts.get("capability.policy_blocked", 0))
        return {
            "status": "success",
            "sourceOfTruth": "admin-sync-runtime-events",
            "summary": {
                "events": event_count,
                "requests": scoped_event_requests,
                "sessions": int(event_row["sessions"] or 0) if event_row else 0,
                "artifacts": int(artifact_row["count"] or 0) if artifact_row else 0,
                "artifactRequests": scoped_artifact_requests,
                "messages": int(message_row["count"] or 0) if message_row else 0,
                "messageRequests": scoped_message_requests,
                "terminalEvents": terminal_count,
                "capabilityPolicyBlocked": capability_block_count,
                "unknownEventTypes": unknown_event_type_count,
                "lastIngestedAt": event_row["last_ingested_at"] if event_row else "",
            },
            "eventTypeCounts": dict(sorted(event_type_counts.items())),
            "sourceCounts": dict(sorted(source_counts.items())),
            "statusCounts": dict(sorted(status_counts.items())),
            "requests": [
                self._runtime_audit_request_projection(row, artifact_counts, message_counts)
                for row in request_rows
            ],
            "recentEvents": [
                self._runtime_audit_event_projection(row)
                for row in recent_rows
            ],
            "privacy": {
                "redacted": True,
                "includesRawRuntimePayloads": False,
                "includesRawRequestSessionIds": False,
                "includesRawDeviceIds": False,
                "includesPromptText": False,
                "includesArtifactPaths": False,
            },
            "redacted": True,
        }

    def ingest_sync_messages(self, payload, token="", device_id="", require_user=True):
        policy = self.sync_policy()
        if not policy["phase2"]["chatBodiesEnabled"]:
            raise ForbiddenError("phase 2 chat body sync is disabled")
        identity = self._sync_identity(payload, token=token, device_id=device_id, require_user=require_user)
        raw_messages = payload.get("messages") if isinstance(payload.get("messages"), list) else []
        if not raw_messages and (payload.get("role") or "content" in payload or "text" in payload):
            raw_messages = [payload]
        if len(raw_messages) > int(policy["phase2"]["maxBatchMessages"] or 1000):
            raise ValueError("sync message batch too large")

        stamp = now_iso()
        message_rows = [
            self._sync_message_row(payload, item, identity, policy)
            for item in raw_messages
            if isinstance(item, dict)
        ]
        with self.connect() as conn:
            for row in message_rows:
                conn.execute(
                    """
                    INSERT INTO sync_messages
                    (id, sync_key, org_id, user_email, user_key, device_id, session_id, request_id,
                     message_id, seq, role, content, content_sha256, content_size_bytes, extras,
                     created_at, ingested_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(sync_key) DO UPDATE SET
                        org_id=excluded.org_id,
                        user_email=excluded.user_email,
                        user_key=excluded.user_key,
                        device_id=excluded.device_id,
                        session_id=excluded.session_id,
                        request_id=excluded.request_id,
                        message_id=excluded.message_id,
                        seq=excluded.seq,
                        role=excluded.role,
                        content=excluded.content,
                        content_sha256=excluded.content_sha256,
                        content_size_bytes=excluded.content_size_bytes,
                        extras=excluded.extras,
                        created_at=excluded.created_at,
                        ingested_at=excluded.ingested_at
                    """,
                    (
                        str(uuid.uuid4()),
                        row["sync_key"],
                        row["org_id"],
                        row["user_email"],
                        row["user_key"],
                        row["device_id"],
                        row["session_id"],
                        row["request_id"],
                        row["message_id"],
                        row["seq"],
                        row["role"],
                        row["content"],
                        row["content_sha256"],
                        row["content_size_bytes"],
                        json_dumps(row["extras"]),
                        row["created_at"],
                        stamp,
                    ),
                )
            if message_rows:
                self.audit(
                    conn,
                    "sync.messages.ingest",
                    identity["user_email"] or "client",
                    "client_sync_messages",
                    {"messages": len(message_rows)},
                )
            conn.commit()
        return {
            "ok": True,
            "messagesAccepted": len(message_rows),
        }

    def _normalize_sha256(self, value, field_name, required=True):
        text = compact_text(value, 80).lower()
        if not text:
            if required:
                raise ValueError(f"{field_name} is required")
            return ""
        if len(text) != 64 or any(ch not in "0123456789abcdef" for ch in text):
            raise ValueError(f"{field_name} must be a sha256 hex digest")
        return text

    def _decode_artifact_chunk(self, payload):
        raw = None
        for key in ("contentBase64", "content_base64", "chunkBase64", "chunk_base64", "dataBase64", "data_base64"):
            if key in payload:
                raw = payload.get(key)
                break
        if raw is None:
            raise ValueError("artifact chunk contentBase64 is required")
        text = str(raw or "")
        if text.startswith("data:") and "," in text:
            text = text.split(",", 1)[1]
        try:
            return base64.b64decode(text.encode("ascii"), validate=True)
        except Exception:
            padded = text + ("=" * ((4 - len(text) % 4) % 4))
            try:
                return base64.urlsafe_b64decode(padded.encode("ascii"))
            except Exception as exc:
                raise ValueError("artifact chunk contentBase64 is invalid") from exc

    def _sync_artifact_file_metadata(self, payload, artifact):
        return sync_safe_json(
            {
                "artifact": artifact,
                "metadata": payload.get("metadata") or payload.get("extras") or {},
            },
            deny_keys=(
                SYNC_DETAIL_DENY_KEYS
                | SYNC_ARTIFACT_PATH_KEYS
                | {
                    "contentbase64",
                    "content_base64",
                    "chunkbase64",
                    "chunk_base64",
                    "database64",
                    "data_base64",
                    "idempotencykey",
                    "idempotency_key",
                    "filesynckey",
                    "file_sync_key",
                }
            ),
        )

    def _sync_artifact_rate_limit(self, conn, identity, chunk_size, policy, stamp):
        bytes_per_second = int(policy["phase3"]["bytesPerSecond"] or 0)
        if bytes_per_second <= 0 or chunk_size <= 0:
            return
        now_ms = int(time.time() * 1000)
        user_key = identity["user_key"]
        row = conn.execute(
            "SELECT available_at_ms FROM sync_artifact_rate_limits WHERE user_key=?",
            (user_key,),
        ).fetchone()
        available_at_ms = int(row["available_at_ms"] or 0) if row else 0
        if available_at_ms and now_ms < available_at_ms:
            raise RateLimitError("phase 3 artifact file sync rate limit exceeded")
        duration_ms = max(1, int((chunk_size * 1000 + bytes_per_second - 1) / bytes_per_second))
        next_available_at_ms = max(now_ms, available_at_ms) + duration_ms
        conn.execute(
            """
            INSERT INTO sync_artifact_rate_limits (user_key, available_at_ms, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(user_key) DO UPDATE SET
                available_at_ms=excluded.available_at_ms,
                updated_at=excluded.updated_at
            """,
            (user_key, next_available_at_ms, stamp),
        )

    def _sync_artifact_chunk_state(self, conn, content_sha256):
        row = conn.execute(
            """
            SELECT COUNT(*) AS chunks,
                   COALESCE(SUM(size_bytes), 0) AS bytes
            FROM sync_artifact_file_chunks
            WHERE content_sha256=?
            """,
            (content_sha256,),
        ).fetchone()
        return {
            "received_chunks": int(row["chunks"] or 0) if row else 0,
            "received_bytes": int(row["bytes"] or 0) if row else 0,
        }

    def _sync_artifact_complete_sha256(self, conn, content_sha256, chunk_count):
        digest = hashlib.sha256()
        rows = conn.execute(
            """
            SELECT chunk_index, data
            FROM sync_artifact_file_chunks
            WHERE content_sha256=?
            ORDER BY chunk_index ASC
            """,
            (content_sha256,),
        ).fetchall()
        if len(rows) != chunk_count:
            return ""
        for expected, row in enumerate(rows):
            if int(row["chunk_index"] or 0) != expected:
                return ""
            digest.update(bytes(row["data"]))
        return digest.hexdigest()

    def ingest_sync_artifact_file(self, payload, token="", device_id="", require_user=True):
        policy = self.sync_policy()
        identity = self._sync_identity(payload, token=token, device_id=device_id, require_user=require_user)
        if not policy["phase3"]["artifactFilesEnabled"]:
            raise ForbiddenError("phase 3 artifact file sync is disabled")

        artifact = payload.get("artifact") if isinstance(payload.get("artifact"), dict) else {}
        artifact_id = compact_text(
            payload.get("artifactId")
            or payload.get("artifact_id")
            or artifact.get("safeArtifactId")
            or artifact.get("safe_artifact_id")
            or artifact.get("artifactId")
            or artifact.get("artifact_id")
            or artifact.get("id"),
            180,
        )
        if not artifact_id:
            raise ValueError("artifactId is required")

        chunk_bytes = self._decode_artifact_chunk(payload)
        chunk_sha256 = hashlib.sha256(chunk_bytes).hexdigest()
        supplied_chunk_sha256 = self._normalize_sha256(
            payload.get("chunkSha256") or payload.get("chunk_sha256"),
            "chunkSha256",
            required=False,
        )
        if supplied_chunk_sha256 and supplied_chunk_sha256 != chunk_sha256:
            raise ValueError("artifact chunk sha256 mismatch")

        chunk_index = as_int(payload.get("chunkIndex") or payload.get("chunk_index"), 0, 0, 10_000_000)
        chunk_count = as_int(payload.get("chunkCount") or payload.get("chunk_count"), 1, 1, 10_000_000)
        if chunk_index >= chunk_count:
            raise ValueError("artifact chunkIndex must be lower than chunkCount")
        chunk_limit = int(policy["phase3"]["chunkBytes"] or 0)
        if chunk_limit and len(chunk_bytes) > chunk_limit:
            raise ValueError("artifact chunk exceeds policy chunkBytes")

        content_sha256 = self._normalize_sha256(
            payload.get("contentSha256")
            or payload.get("content_sha256")
            or payload.get("fileSha256")
            or payload.get("file_sha256")
            or payload.get("sha256")
            or artifact.get("contentSha256")
            or artifact.get("content_sha256")
            or (chunk_sha256 if chunk_count == 1 else ""),
            "contentSha256",
            required=True,
        )
        if chunk_count == 1 and content_sha256 != chunk_sha256:
            raise ValueError("artifact content sha256 mismatch")

        total_size = as_int(
            payload.get("totalSizeBytes")
            or payload.get("total_size_bytes")
            or payload.get("sizeBytes")
            or payload.get("size_bytes")
            or artifact.get("sizeBytes")
            or artifact.get("size_bytes"),
            len(chunk_bytes) if chunk_count == 1 else 0,
            0,
            10 * 1024 * 1024 * 1024,
        )
        max_auto_bytes = int(policy["phase3"]["maxAutoBytes"] or 0)
        if max_auto_bytes and total_size > max_auto_bytes:
            raise ValueError("artifact file exceeds policy maxAutoBytes")

        session_id = compact_text(payload.get("sessionId") or payload.get("session_id") or artifact.get("sessionId") or artifact.get("session_id"), 180)
        request_id = compact_text(payload.get("requestId") or payload.get("request_id") or artifact.get("requestId") or artifact.get("request_id"), 180)
        title = compact_text(payload.get("title") or artifact.get("title") or artifact.get("fileName") or artifact.get("file_name") or "artifact", 240)
        mime_type = compact_text(payload.get("mimeType") or payload.get("mime_type") or artifact.get("mimeType") or artifact.get("mime_type"), 120)
        created_at = compact_text(payload.get("createdAt") or payload.get("created_at") or artifact.get("createdAt") or artifact.get("created_at") or now_iso(), 80)
        base_key = "|".join([identity["org_id"], identity["user_key"], session_id, request_id, artifact_id, content_sha256])
        sync_key = compact_text(
            payload.get("fileSyncKey")
            or payload.get("file_sync_key")
            or payload.get("syncKey")
            or payload.get("sync_key")
            or payload.get("idempotencyKey")
            or payload.get("idempotency_key")
            or f"artifact-file:{short_hash(base_key, 40)}",
            180,
        )
        metadata = self._sync_artifact_file_metadata(payload, artifact)

        stamp = now_iso()
        chunk_inserted = False
        with self.connect() as conn:
            existing_chunk = conn.execute(
                """
                SELECT chunk_sha256, size_bytes
                FROM sync_artifact_file_chunks
                WHERE content_sha256=? AND chunk_index=?
                """,
                (content_sha256, chunk_index),
            ).fetchone()
            if existing_chunk:
                if existing_chunk["chunk_sha256"] != chunk_sha256 or int(existing_chunk["size_bytes"] or 0) != len(chunk_bytes):
                    raise ValueError("artifact chunk conflicts with stored content")
            else:
                self._sync_artifact_rate_limit(conn, identity, len(chunk_bytes), policy, stamp)
                conn.execute(
                    """
                    INSERT INTO sync_artifact_file_chunks
                    (id, content_sha256, chunk_index, chunk_sha256, size_bytes, data, created_at, ingested_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (str(uuid.uuid4()), content_sha256, chunk_index, chunk_sha256, len(chunk_bytes), chunk_bytes, created_at, stamp),
                )
                chunk_inserted = True

            state = self._sync_artifact_chunk_state(conn, content_sha256)
            status = "receiving"
            if state["received_chunks"] >= chunk_count:
                if state["received_bytes"] != total_size:
                    raise ValueError("artifact file size mismatch")
                complete_sha256 = self._sync_artifact_complete_sha256(conn, content_sha256, chunk_count)
                if complete_sha256 != content_sha256:
                    raise ValueError("artifact file sha256 mismatch")
                status = "complete"

            conn.execute(
                """
                INSERT INTO sync_artifact_files
                (id, sync_key, artifact_id, org_id, user_email, user_key, device_id, session_id,
                 request_id, title, mime_type, size_bytes, content_sha256, chunk_count,
                 received_chunks, received_bytes, status, metadata, created_at, updated_at, ingested_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(sync_key) DO UPDATE SET
                    artifact_id=excluded.artifact_id,
                    org_id=excluded.org_id,
                    user_email=excluded.user_email,
                    user_key=excluded.user_key,
                    device_id=excluded.device_id,
                    session_id=excluded.session_id,
                    request_id=excluded.request_id,
                    title=excluded.title,
                    mime_type=excluded.mime_type,
                    size_bytes=excluded.size_bytes,
                    content_sha256=excluded.content_sha256,
                    chunk_count=excluded.chunk_count,
                    received_chunks=excluded.received_chunks,
                    received_bytes=excluded.received_bytes,
                    status=excluded.status,
                    metadata=excluded.metadata,
                    updated_at=excluded.updated_at,
                    ingested_at=excluded.ingested_at
                """,
                (
                    str(uuid.uuid4()),
                    sync_key,
                    artifact_id,
                    identity["org_id"],
                    identity["user_email"],
                    identity["user_key"],
                    identity["device_id"],
                    session_id,
                    request_id,
                    title,
                    mime_type,
                    total_size,
                    content_sha256,
                    chunk_count,
                    state["received_chunks"],
                    state["received_bytes"],
                    status,
                    json_dumps(metadata),
                    created_at,
                    stamp,
                    stamp,
                ),
            )
            self.audit(
                conn,
                "sync.artifact_file.ingest",
                identity["user_email"] or "client",
                "client_sync_artifact_file",
                {
                    "artifactId": artifact_id,
                    "chunkIndex": chunk_index,
                    "chunkCount": chunk_count,
                    "status": status,
                    "bytes": len(chunk_bytes),
                    "deduped": not chunk_inserted,
                },
            )
            conn.commit()

        return {
            "ok": True,
            "artifactId": artifact_id,
            "contentSha256": content_sha256,
            "chunkIndex": chunk_index,
            "chunkCount": chunk_count,
            "chunkAccepted": True,
            "chunkStored": chunk_inserted,
            "deduped": not chunk_inserted,
            "receivedChunks": state["received_chunks"],
            "receivedBytes": state["received_bytes"],
            "complete": status == "complete",
            "status": status,
        }

    def usage_by_user(self, conn):
        default_emails = [email.lower() for _, email, _, _ in DEFAULT_USERS]
        placeholders = ",".join("?" for _ in default_emails)
        users = conn.execute(
            f"""
            SELECT id, name, email, daily_token_limit, weekly_token_limit, deleted_at
            FROM users
            WHERE deleted_at IS NULL AND status='active' AND lower(email) NOT IN ({placeholders})
            ORDER BY created_at DESC
            """,
            default_emails,
        ).fetchall()
        result = []
        for user in users:
            quota = self.quota_state(conn, user["email"])
            result.append(
                {
                    "userId": user["id"],
                    "name": user["name"],
                    "email": user["email"],
                    "dailyTokenLimit": int(user["daily_token_limit"] or 0),
                    "weeklyTokenLimit": int(user["weekly_token_limit"] or 0),
                    "deletedAt": user["deleted_at"],
                    "dailyTokens": quota["dailyUsed"],
                    "weeklyTokens": quota["weeklyUsed"],
                    "totalTokens": quota["totalUsed"],
                    "dailyRemaining": quota["dailyRemaining"],
                    "weeklyRemaining": quota["weeklyRemaining"],
                    "overDaily": quota["overDaily"],
                    "overWeekly": quota["overWeekly"],
                }
            )
        return result

    def log_users(self, conn):
        rows = conn.execute(
            """
            SELECT name, email, deleted_at FROM users
            UNION
            SELECT COALESCE(NULLIF(user_email, ''), '未知用户') AS name, user_email AS email, NULL AS deleted_at
            FROM error_logs
            WHERE user_email IS NOT NULL AND user_email != ''
            UNION
            SELECT COALESCE(NULLIF(user_email, ''), '未知用户') AS name, user_email AS email, NULL AS deleted_at
            FROM usage_events
            WHERE user_email IS NOT NULL AND user_email != ''
            ORDER BY email
            """
        ).fetchall()
        return [
            {
                "name": row["name"] or row["email"],
                "email": row["email"],
                "deletedAt": row["deleted_at"],
            }
            for row in rows
            if row["email"]
        ]

    def query_logs(self, conn, filters=None):
        filters = filters or {}
        where = []
        values = []
        if filters.get("userEmail"):
            where.append("lower(user_email)=lower(?)")
            values.append(compact_text(filters["userEmail"], 180))
        if filters.get("deviceId"):
            where.append("device_id=?")
            values.append(compact_text(filters["deviceId"], 180))
        level = compact_text(filters.get("level") or "error", 32)
        if level and level != "all":
            where.append("level=?")
            values.append(level)
        if filters.get("from"):
            where.append("created_at>=?")
            values.append(compact_text(filters["from"], 40))
        if filters.get("to"):
            where.append("created_at<=?")
            values.append(compact_text(filters["to"], 40))
        sql = "SELECT * FROM error_logs"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY created_at DESC LIMIT ?"
        values.append(as_int(filters.get("limit"), 100, 1, 500))
        rows = conn.execute(sql, values).fetchall()
        return [
            {
                "id": row["id"],
                "level": row["level"],
                "source": row["source"],
                "message": row["message"],
                "status": row["status"],
                "userEmail": row["user_email"],
                "deviceId": row["device_id"],
                "sessionId": row["session_id"],
                "tool": row["tool"],
                "appVersion": row["app_version"],
                "detail": json_loads(row["detail"]),
                "time": row["created_at"],
            }
            for row in rows
        ]

    def get_user_by_email(self, conn, email):
        return conn.execute(
            "SELECT * FROM users WHERE lower(email)=lower(?) AND deleted_at IS NULL",
            (compact_text(email, 180),),
        ).fetchone()

    def create_user(self, payload):
        name = compact_text(payload.get("name"), 120)
        email = compact_text(payload.get("email"), 180).lower()
        role = compact_text(payload.get("role") or "member", 32)
        password = payload.get("initialPassword") or payload.get("password")
        if not name or "@" not in email:
            raise ValueError("name and a valid email are required")
        if not password or len(str(password)) < 8:
            raise ValueError("initial password must be at least 8 characters")
        if role not in ("admin", "member"):
            role = "member"
        stamp = now_iso()
        user_id = str(uuid.uuid4())
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO users
                (id, name, email, role, status, password_hash, must_change_password,
                 daily_token_limit, weekly_token_limit, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    name,
                    email,
                    role,
                    compact_text(payload.get("status") or "active", 32),
                    hash_password(password),
                    1,
                    as_int(payload.get("dailyTokenLimit") or payload.get("daily_token_limit")),
                    as_int(payload.get("weeklyTokenLimit") or payload.get("weekly_token_limit")),
                    stamp,
                    stamp,
                ),
            )
            self.audit(conn, "user.create", payload.get("actor", "admin"), email, {"role": role})
            conn.commit()
        return self.state()

    def update_user(self, user_id, payload):
        allowed = {}
        string_map = {"name": "name", "email": "email", "role": "role", "status": "status"}
        int_map = {"dailyTokenLimit": "daily_token_limit", "weeklyTokenLimit": "weekly_token_limit"}
        for key, column in string_map.items():
            if key in payload:
                allowed[column] = compact_text(payload[key], 180)
        for key, column in int_map.items():
            if key in payload:
                allowed[column] = as_int(payload[key])
        if "role" in allowed and allowed["role"] not in ("admin", "member"):
            raise ValueError("invalid role")
        if "status" in allowed and allowed["status"] not in ("active", "invited", "disabled"):
            raise ValueError("invalid status")
        if "email" in allowed and "@" not in allowed["email"]:
            raise ValueError("valid email is required")
        if "name" in allowed and not allowed["name"]:
            raise ValueError("name is required")
        if not allowed:
            return self.state()
        allowed["updated_at"] = now_iso()
        sets = ", ".join(f"{column}=?" for column in allowed)
        values = list(allowed.values()) + [user_id]
        with self.connect() as conn:
            cur = conn.execute(f"UPDATE users SET {sets} WHERE id=? AND deleted_at IS NULL", values)
            if cur.rowcount == 0:
                raise KeyError("user not found")
            self.audit(conn, "user.update", payload.get("actor", "admin"), user_id, allowed)
            conn.commit()
        return self.state()

    def delete_user(self, user_id, payload):
        stamp = now_iso()
        with self.connect() as conn:
            cur = conn.execute(
                "UPDATE users SET status='disabled', deleted_at=?, updated_at=? WHERE id=? AND deleted_at IS NULL",
                (stamp, stamp, user_id),
            )
            if cur.rowcount == 0:
                raise KeyError("user not found")
            conn.execute("UPDATE client_sessions SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL", (stamp, user_id))
            self.audit(conn, "user.delete", payload.get("actor", "admin"), user_id, {"softDelete": True})
            conn.commit()
        return self.state()

    def reset_user_password(self, user_id, payload):
        password = payload.get("initialPassword") or payload.get("password")
        if not password or len(str(password)) < 8:
            raise ValueError("password must be at least 8 characters")
        stamp = now_iso()
        with self.connect() as conn:
            cur = conn.execute(
                "UPDATE users SET password_hash=?, must_change_password=1, updated_at=? WHERE id=? AND deleted_at IS NULL",
                (hash_password(password), stamp, user_id),
            )
            if cur.rowcount == 0:
                raise KeyError("user not found")
            conn.execute("UPDATE client_sessions SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL", (stamp, user_id))
            self.audit(conn, "user.reset_password", payload.get("actor", "admin"), user_id, {})
            conn.commit()
        return self.state()

    def change_password(self, payload):
        user = self.require_session(payload.get("token"), payload.get("deviceId"))
        old_password = payload.get("oldPassword")
        new_password = payload.get("newPassword")
        if not verify_password(old_password, user["password_hash"]):
            raise ValueError("old password is incorrect")
        if not new_password or len(str(new_password)) < 8:
            raise ValueError("new password must be at least 8 characters")
        stamp = now_iso()
        with self.connect() as conn:
            conn.execute(
                "UPDATE users SET password_hash=?, must_change_password=0, updated_at=? WHERE id=?",
                (hash_password(new_password), stamp, user["id"]),
            )
            self.audit(conn, "user.change_password", user["email"], user["id"], {})
            conn.commit()
        return {"ok": True}

    def login(self, payload):
        email = compact_text(payload.get("email"), 180).lower()
        password = payload.get("password") or ""
        device_id = compact_text(payload.get("deviceId") or payload.get("device_id"), 180)
        app_version = compact_text(payload.get("appVersion") or payload.get("app_version") or VERSION, 40)
        with self.connect() as conn:
            user = self.get_user_by_email(conn, email)
            if not user or user["status"] == "disabled" or not verify_password(password, user["password_hash"]):
                raise ValueError("invalid email or password")
            if user["status"] not in ("active", "invited"):
                raise ValueError("user is not active")
            token = secrets.token_urlsafe(32)
            stamp = now_iso()
            expires = (now_dt() + timedelta(days=SESSION_DAYS)).isoformat(timespec="seconds")
            conn.execute(
                """
                INSERT INTO client_sessions
                (id, user_id, token_hash, device_id, app_version, created_at, expires_at, last_seen_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (str(uuid.uuid4()), user["id"], hash_token(token), device_id, app_version, stamp, expires, stamp),
            )
            conn.execute(
                "UPDATE users SET last_login_at=?, status='active', updated_at=? WHERE id=?",
                (stamp, stamp, user["id"]),
            )
            self.audit(conn, "client.login", email, user["id"], {"deviceId": device_id, "appVersion": app_version})
            conn.commit()
            fresh = conn.execute("SELECT * FROM users WHERE id=?", (user["id"],)).fetchone()
            quota = self.quota_state(conn, fresh["email"])
        return {
            "ok": True,
            "token": token,
            "deviceId": device_id,
            "expiresAt": expires,
            "user": self.serialize_user(fresh),
            "quota": quota,
            "version": VERSION,
        }

    def require_session(self, token, device_id=""):
        token = compact_text(token, 400)
        if not token:
            raise PermissionError("missing user token")
        token_hash = hash_token(token)
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT s.*, u.*
                FROM client_sessions s
                JOIN users u ON u.id = s.user_id
                WHERE s.token_hash=? AND s.revoked_at IS NULL AND u.deleted_at IS NULL
                """,
                (token_hash,),
            ).fetchone()
            if not row:
                raise PermissionError("invalid user token")
            expires_at = datetime.fromisoformat(row["expires_at"])
            if expires_at < now_dt():
                raise PermissionError("user token expired")
            if row["status"] == "disabled":
                raise PermissionError("user is disabled")
            if not device_id_matches(row["device_id"], device_id):
                raise PermissionError("device does not match user session")
            conn.execute("UPDATE client_sessions SET last_seen_at=? WHERE token_hash=?", (now_iso(), token_hash))
            conn.commit()
            return row

    def quota_state(self, conn, email):
        email = compact_text(email, 180).lower()
        user = self.get_user_by_email(conn, email)
        if not user:
            return {
                "allowed": False,
                "reason": "用户不存在",
                "dailyUsed": 0,
                "weeklyUsed": 0,
                "totalUsed": 0,
                "dailyLimit": 0,
                "weeklyLimit": 0,
                "dailyRemaining": None,
                "weeklyRemaining": None,
                "overDaily": False,
                "overWeekly": False,
            }
        today = now_dt().date().isoformat()
        week_start = (now_dt().date() - timedelta(days=now_dt().date().weekday())).isoformat()

        def total_since(since=None):
            params = [email]
            sql = """
                SELECT SUM(CASE WHEN total_tokens > 0 THEN total_tokens ELSE amount END) AS tokens
                FROM usage_events
                WHERE lower(user_email)=lower(?)
            """
            if since:
                sql += " AND created_at >= ?"
                params.append(since)
            return int(conn.execute(sql, params).fetchone()["tokens"] or 0)

        daily_used = total_since(today)
        weekly_used = total_since(week_start)
        total_used = total_since()
        daily_limit = int(user["daily_token_limit"] or 0)
        weekly_limit = int(user["weekly_token_limit"] or 0)
        over_daily = daily_limit > 0 and daily_used >= daily_limit
        over_weekly = weekly_limit > 0 and weekly_used >= weekly_limit
        reason = ""
        if over_daily:
            reason = "今日 token 额度已用完"
        elif over_weekly:
            reason = "本周 token 额度已用完"
        return {
            "allowed": not (over_daily or over_weekly),
            "reason": reason,
            "dailyUsed": daily_used,
            "weeklyUsed": weekly_used,
            "totalUsed": total_used,
            "dailyLimit": daily_limit,
            "weeklyLimit": weekly_limit,
            "dailyRemaining": None if daily_limit <= 0 else max(0, daily_limit - daily_used),
            "weeklyRemaining": None if weekly_limit <= 0 else max(0, weekly_limit - weekly_used),
            "overDaily": over_daily,
            "overWeekly": over_weekly,
        }

    def check_quota(self, payload, token="", device_id=""):
        user = self.require_session(token or payload.get("token"), device_id or payload.get("deviceId"))
        estimated = as_int(payload.get("estimatedTokens"), 0)
        with self.connect() as conn:
            quota = self.quota_state(conn, user["email"])
        allowed = quota["allowed"]
        if allowed and estimated > 0:
            if quota["dailyRemaining"] is not None and estimated > quota["dailyRemaining"]:
                allowed = False
                quota["reason"] = "本次请求预计超过今日 token 额度"
            if quota["weeklyRemaining"] is not None and estimated > quota["weeklyRemaining"]:
                allowed = False
                quota["reason"] = "本次请求预计超过本周 token 额度"
        quota["allowed"] = allowed
        quota["userEmail"] = user["email"]
        return {"ok": True, "quota": quota}

    def mark_logs_read(self, payload):
        ids = payload.get("ids") or []
        with self.connect() as conn:
            if ids:
                conn.executemany("UPDATE error_logs SET status='read' WHERE id=?", [(compact_text(item, 80),) for item in ids])
            else:
                conn.execute("UPDATE error_logs SET status='read' WHERE level='error'")
            self.audit(conn, "log.mark_read", payload.get("actor", "admin"), "error_logs", {"ids": ids})
            conn.commit()
        return self.state()

    def update_policy(self, payload):
        mirror = compact_text(payload.get("mirror"), 300) or "https://pypi.org/simple"
        mode = compact_text(payload.get("mode"), 32) or "preinstall"
        offline_cache = compact_text(payload.get("offlineCache") or payload.get("offline_cache"), 300) or "未配置"
        if mode not in ("ask", "preinstall", "disabled"):
            raise ValueError("invalid capability policy mode")
        with self.connect() as conn:
            conn.execute(
                "UPDATE capability_policy SET mirror=?, mode=?, offline_cache=?, updated_at=? WHERE id=1",
                (mirror, mode, offline_cache, now_iso()),
            )
            self.audit(conn, "capability_policy.update", payload.get("actor", "admin"), "capability_policy", {"mode": mode, "mirror": mirror})
            conn.commit()
        return self.state()

    def _mask_model_credential(self, row):
        row["enabled"] = bool(row.get("enabled"))
        row["apiKeyMask"] = mask_secret(row.pop("apiKey", ""))
        return row

    def get_global_model(self, conn, masked=False):
        row = conn.execute(
            """
            SELECT id, name, provider, model, bot_type AS botType, api_base AS baseUrl,
                   api_key AS apiKey, scope_type AS scopeType, scope_value AS scopeValue,
                   enabled, created_at AS createdAt, updated_at AS updatedAt
            FROM model_credentials
            WHERE scope_type='global'
            ORDER BY enabled DESC, updated_at DESC
            LIMIT 1
            """
        ).fetchone()
        if not row:
            return None
        data = dict(row)
        data["enabled"] = bool(data["enabled"])
        if masked:
            data["apiKeyMask"] = mask_secret(data.pop("apiKey", ""))
        return data

    def upsert_global_model(self, payload):
        prepared = self._prepare_model_credential({**payload, "scopeType": "global", "scopeValue": "", "enabled": True}, require_api_key=False)
        stamp = now_iso()
        with self.connect() as conn:
            current = self.get_global_model(conn, masked=False)
            if current:
                api_key = prepared["api_key"] or current["apiKey"]
                conn.execute(
                    """
                    UPDATE model_credentials
                    SET name=?, provider=?, model=?, bot_type=?, api_base=?, api_key=?,
                        scope_type='global', scope_value='', enabled=1, updated_at=?
                    WHERE id=?
                    """,
                    (
                        prepared["name"],
                        prepared["provider"],
                        prepared["model"],
                        prepared["bot_type"],
                        prepared["api_base"],
                        api_key,
                        stamp,
                        current["id"],
                    ),
                )
                target = current["id"]
            else:
                if not prepared["api_key"]:
                    raise ValueError("api key is required for the first model configuration")
                target = str(uuid.uuid4())
                conn.execute(
                    """
                    INSERT INTO model_credentials
                    (id, name, provider, model, bot_type, api_base, api_key, scope_type, scope_value, enabled, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 'global', '', 1, ?, ?)
                    """,
                    (
                        target,
                        prepared["name"],
                        prepared["provider"],
                        prepared["model"],
                        prepared["bot_type"],
                        prepared["api_base"],
                        prepared["api_key"],
                        stamp,
                        stamp,
                    ),
                )
            conn.execute("UPDATE model_credentials SET enabled=0 WHERE scope_type!='global'")
            self.audit(conn, "model.global.upsert", payload.get("actor", "admin"), target, self._audit_model_detail(prepared))
            conn.commit()
        return self.state()

    def create_model_credential(self, payload):
        return self.upsert_global_model(payload)

    def update_model_credential(self, credential_id, payload):
        payload = {**payload, "id": credential_id}
        return self.upsert_global_model(payload)

    def delete_model_credential(self, credential_id, payload):
        raise ValueError("global model cannot be deleted")

    def resolve_client_model_config(self, user_email="", device_id="", token=""):
        user = self.require_session(token, device_id)
        user_email = user["email"]
        with self.connect() as conn:
            quota = self.quota_state(conn, user_email)
            if not quota["allowed"]:
                raise PermissionError(quota["reason"] or "token quota exceeded")
            selected = self.get_global_model(conn, masked=False)
        if not selected or not selected.get("enabled"):
            return {"ok": True, "configured": False, "settings": {}, "updatedAt": "", "version": VERSION}
        api_key_name, api_base_name, default_bot_type = PROVIDER_CONFIG_KEYS.get(selected["provider"], PROVIDER_CONFIG_KEYS["custom"])
        bot_type = selected["botType"] or default_bot_type
        settings = {
            "model": selected["model"],
            "bot_type": bot_type,
            api_key_name: selected["apiKey"],
            api_base_name: selected["baseUrl"],
        }
        return {
            "ok": True,
            "configured": True,
            "id": selected["id"],
            "name": selected["name"],
            "provider": selected["provider"],
            "model": selected["model"],
            "baseUrl": selected["baseUrl"],
            "scopeType": "global",
            "userEmail": user_email,
            "updatedAt": selected["updatedAt"],
            "settings": settings,
            "version": VERSION,
        }

    def resolve_client_capability_policy(self):
        with self.connect() as conn:
            policy = dict(conn.execute("SELECT mirror, mode, offline_cache AS offlineCache, updated_at AS updatedAt FROM capability_policy WHERE id = 1").fetchone())
            capabilities = [
                dict(row)
                for row in conn.execute("SELECT id, name, mode, size, status, updated_at AS updatedAt FROM capability_packs ORDER BY id")
            ]
        return {
            "ok": True,
            "policy": policy,
            "capabilities": capabilities,
            "updatedAt": policy.get("updatedAt", ""),
            "version": VERSION,
        }

    def test_model_credential(self, payload):
        action = compact_text(payload.get("action") or payload.get("test") or "connectivity", 32)
        if action not in ("connectivity", "chat", "image"):
            raise ValueError("unsupported model test action")

        with self.connect() as conn:
            current = self.get_global_model(conn, masked=False)

        merged = {
            "name": payload.get("name") or (current or {}).get("name") or "EcoreX 企业模型",
            "provider": payload.get("provider") or (current or {}).get("provider") or "custom",
            "model": payload.get("model") or (current or {}).get("model") or "",
            "baseUrl": payload.get("baseUrl") or payload.get("api_base") or (current or {}).get("baseUrl") or "",
            "apiKey": payload.get("apiKey") or payload.get("api_key") or (current or {}).get("apiKey") or "",
            "botType": payload.get("botType") or payload.get("bot_type") or (current or {}).get("botType") or "",
        }
        prepared = self._prepare_model_credential(merged, require_api_key=True)

        if action == "connectivity":
            result = self._test_openai_models(prepared)
            if not result["ok"]:
                fallback = self._test_openai_chat(prepared, prompt="只回复 OK，用于 EcoreX 连通性测试。", purpose="connectivity")
                if fallback["ok"]:
                    return {
                        **fallback,
                        "test": "connectivity",
                        "message": "模型列表端点不可用，但聊天端点已连通。",
                    }
            return result
        if action == "chat":
            return self._test_openai_chat(prepared, prompt="请用一句中文回复：EcoreX 会话测试通过。", purpose="chat")
        return self._test_openai_image(prepared, payload)

    def _endpoint_candidates(self, base_url, endpoint):
        base = compact_text(base_url, 500).rstrip("/")
        if not base:
            return []
        lower = base.lower()
        if lower.endswith("/" + endpoint.lower()):
            candidates = [base]
        elif lower.endswith("/v1"):
            candidates = [f"{base}/{endpoint}"]
        else:
            candidates = [f"{base}/v1/{endpoint}", f"{base}/{endpoint}"]
        unique = []
        for item in candidates:
            if item not in unique:
                unique.append(item)
        return unique

    def _request_json(self, method, url, api_key, body=None, timeout=25):
        started = time.monotonic()
        data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
            "User-Agent": f"EcoreX-Admin/{VERSION}",
        }
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                raw = response.read(512_000).decode("utf-8", errors="replace")
                payload = json_loads(raw, {})
                return {
                    "ok": 200 <= response.status < 300,
                    "statusCode": response.status,
                    "latencyMs": int((time.monotonic() - started) * 1000),
                    "payload": payload,
                    "raw": raw[:1200],
                }
        except urllib.error.HTTPError as exc:
            raw = exc.read(120_000).decode("utf-8", errors="replace")
            payload = json_loads(raw, {})
            return {
                "ok": False,
                "statusCode": exc.code,
                "latencyMs": int((time.monotonic() - started) * 1000),
                "payload": payload,
                "raw": raw[:1200],
                "error": payload.get("error", {}).get("message") if isinstance(payload.get("error"), dict) else raw[:300],
            }
        except Exception as exc:
            return {
                "ok": False,
                "statusCode": 0,
                "latencyMs": int((time.monotonic() - started) * 1000),
                "payload": {},
                "raw": "",
                "error": str(exc),
            }

    def _test_openai_models(self, prepared):
        last = None
        for url in self._endpoint_candidates(prepared["api_base"], "models"):
            result = self._request_json("GET", url, prepared["api_key"], timeout=18)
            last = result
            if result["ok"]:
                data = result.get("payload") or {}
                count = len(data.get("data") or []) if isinstance(data.get("data"), list) else 0
                return {
                    "ok": True,
                    "test": "connectivity",
                    "message": f"连通性测试通过，模型列表端点可访问{f'，返回 {count} 个模型' if count else ''}。",
                    "endpoint": url,
                    "statusCode": result["statusCode"],
                    "latencyMs": result["latencyMs"],
                }
        return self._model_test_failure("connectivity", "模型列表端点不可用。", last)

    def _test_openai_chat(self, prepared, prompt, purpose="chat"):
        body = {
            "model": prepared["model"],
            "messages": [
                {"role": "system", "content": "你是 EcoreX 管理后台的连通性测试助手。"},
                {"role": "user", "content": prompt},
            ],
            "stream": False,
            "max_tokens": 32,
            "temperature": 0,
        }
        last = None
        for url in self._endpoint_candidates(prepared["api_base"], "chat/completions"):
            result = self._request_json("POST", url, prepared["api_key"], body=body, timeout=35)
            last = result
            if result["ok"]:
                payload = result.get("payload") or {}
                choices = payload.get("choices") if isinstance(payload, dict) else []
                preview = ""
                if choices:
                    message = (choices[0] or {}).get("message") or {}
                    preview = compact_text(message.get("content"), 120)
                return {
                    "ok": True,
                    "test": purpose,
                    "message": "会话测试通过，模型已返回响应。",
                    "endpoint": url,
                    "statusCode": result["statusCode"],
                    "latencyMs": result["latencyMs"],
                    "replyPreview": preview,
                }
        return self._model_test_failure(purpose, "会话端点不可用或模型名不可用。", last)

    def _test_openai_image(self, prepared, payload):
        image_model = normalize_image_model(payload.get("imageModel") or payload.get("image_model") or "gpt-image-2-pro")
        body = {
            "model": image_model,
            "prompt": "EcoreX image generation connectivity test, simple orange brand icon on white background.",
            "size": compact_text(payload.get("imageSize") or "512x512", 32),
            "n": 1,
        }
        last = None
        for url in self._endpoint_candidates(prepared["api_base"], "images/generations"):
            result = self._request_json("POST", url, prepared["api_key"], body=body, timeout=60)
            last = result
            if result["ok"]:
                return {
                    "ok": True,
                    "test": "image",
                    "message": "生图测试通过，图像生成端点已返回结果。",
                    "endpoint": url,
                    "statusCode": result["statusCode"],
                    "latencyMs": result["latencyMs"],
                }
        return self._model_test_failure("image", "生图端点不可用，或当前模型/上游不支持图像生成。", last)

    def _model_test_failure(self, test, prefix, result):
        result = result or {}
        raw_error = result.get("error") or result.get("raw") or "无返回内容"
        return {
            "ok": False,
            "test": test,
            "message": f"{prefix} {compact_text(raw_error, 220)}",
            "endpoint": "",
            "statusCode": result.get("statusCode", 0),
            "latencyMs": result.get("latencyMs", 0),
        }

    def _prepare_model_credential(self, payload, require_api_key):
        provider = compact_text(payload.get("provider") or "openai", 32).lower()
        if provider not in PROVIDER_CONFIG_KEYS:
            raise ValueError("unsupported provider")
        _, _, default_bot_type = PROVIDER_CONFIG_KEYS[provider]
        api_key = compact_text(payload.get("api_key") or payload.get("apiKey"), 500)
        if require_api_key and not api_key:
            raise ValueError("api key is required")
        model = compact_text(payload.get("model"), 120)
        api_base = compact_text(payload.get("api_base") or payload.get("baseUrl"), 300)
        if not model or not api_base:
            raise ValueError("model and baseUrl are required")
        return {
            "name": compact_text(payload.get("name") or f"{provider} / {model}", 120),
            "provider": provider,
            "model": model,
            "bot_type": compact_text(payload.get("bot_type") or payload.get("botType") or default_bot_type, 64),
            "api_base": api_base,
            "api_key": api_key,
            "scope_type": "global",
            "scope_value": "",
            "enabled": True,
        }

    def _audit_model_detail(self, prepared):
        detail = dict(prepared)
        detail.pop("api_key", None)
        detail["apiKeyMask"] = mask_secret(prepared.get("api_key"))
        return detail

    def reset_usage(self, payload):
        stamp = now_iso()
        with self.connect() as conn:
            conn.execute("DELETE FROM usage_events")
            conn.executemany(
                "INSERT INTO usage_events (id, category, label, amount, total_tokens, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                [(str(uuid.uuid4()), category, label, amount, amount, stamp) for category, label, amount in DEFAULT_USAGE],
            )
            self.audit(conn, "usage.reset", payload.get("actor", "admin"), "usage_events", {})
            conn.commit()
        return self.state()

    def _sync_identity(self, payload, token="", device_id="", require_user=True):
        user_email = compact_text(payload.get("userEmail") or payload.get("user_email"), 180).lower()
        resolved_device_id = compact_text(payload.get("deviceId") or payload.get("device_id") or device_id, 180)
        if token or require_user:
            user = self.require_session(token, device_id or payload.get("deviceId") or payload.get("device_id"))
            user_email = user["email"]
            resolved_device_id = compact_text(user["device_id"] or resolved_device_id, 180)
        return {
            "org_id": compact_text(payload.get("orgId") or payload.get("org_id") or "default", 120),
            "user_email": user_email,
            "user_key": user_key_for(user_email, resolved_device_id),
            "device_id": resolved_device_id,
        }

    def _sync_event_row(self, payload, item, identity):
        event_type = compact_text(item.get("eventType") or item.get("event_type") or item.get("type") or "event", 80)
        session_id = compact_text(item.get("sessionId") or item.get("session_id") or payload.get("sessionId") or payload.get("session_id"), 180)
        request_id = compact_text(item.get("requestId") or item.get("request_id") or payload.get("requestId") or payload.get("request_id"), 180)
        status = compact_text(item.get("status") or item.get("state") or "", 80)
        source = compact_text(item.get("source") or payload.get("source") or "client", 80)
        created_at = compact_text(item.get("createdAt") or item.get("created_at") or now_iso(), 80)
        detail = sync_safe_json(item.get("detail") or item.get("metadata") or {})
        base_key = "|".join([
            identity["org_id"],
            identity["user_key"],
            session_id,
            request_id,
            event_type,
            status,
            compact_text(item.get("phase") or "", 80),
        ])
        sync_key = compact_text(
            item.get("idempotencyKey") or item.get("idempotency_key") or f"event:{short_hash(base_key, 40)}",
            160,
        )
        return {
            **identity,
            "sync_key": sync_key,
            "event_type": event_type,
            "session_id": session_id,
            "request_id": request_id,
            "status": status,
            "source": source,
            "detail": detail,
            "created_at": created_at,
        }

    def _sync_artifact_row(self, payload, item, identity):
        session_id = compact_text(item.get("sessionId") or item.get("session_id") or payload.get("sessionId") or payload.get("session_id"), 180)
        request_id = compact_text(item.get("requestId") or item.get("request_id") or payload.get("requestId") or payload.get("request_id"), 180)
        title = compact_text(item.get("title") or item.get("fileName") or item.get("file_name") or item.get("name") or "artifact", 240)
        raw_path = ""
        for key in ("path", "file_path", "filePath", "relativePath", "relative_path", "url", "previewUrl", "preview_url"):
            if item.get(key):
                raw_path = str(item.get(key) or "")
                break
        raw_artifact_id = item.get("artifactId") or item.get("artifact_id") or item.get("id") or raw_path or title
        artifact_id = compact_text(item.get("safeArtifactId") or item.get("safe_artifact_id") or f"artifact:{short_hash(raw_artifact_id, 40)}", 180)
        path_hash = compact_text(item.get("pathHash") or item.get("path_hash") or (short_hash(raw_path, 64) if raw_path else ""), 80)
        path_ext = compact_text(item.get("pathExt") or item.get("path_ext") or sync_artifact_path_ext(raw_path), 32)
        metadata = sync_safe_json(
            item,
            deny_keys=(
                SYNC_DETAIL_DENY_KEYS
                | SYNC_ARTIFACT_PATH_KEYS
                | {"id", "artifactid", "artifact_id", "idempotencykey", "idempotency_key"}
            ),
        )
        base_key = "|".join([
            identity["org_id"],
            identity["user_key"],
            session_id,
            request_id,
            artifact_id,
        ])
        sync_key = compact_text(
            item.get("idempotencyKey") or item.get("idempotency_key") or f"artifact:{short_hash(base_key, 40)}",
            160,
        )
        return {
            **identity,
            "sync_key": sync_key,
            "artifact_id": artifact_id,
            "session_id": session_id,
            "request_id": request_id,
            "kind": compact_text(item.get("kind") or item.get("fileType") or item.get("file_type") or "file", 40),
            "intent": compact_text(item.get("intent") or "deliverable", 60),
            "operation": compact_text(item.get("operation") or "created", 60),
            "status": compact_text(item.get("status") or "ready", 60),
            "title": title,
            "path_hash": path_hash,
            "path_ext": path_ext,
            "mime_type": compact_text(item.get("mimeType") or item.get("mime_type") or "", 120),
            "size_bytes": as_int(item.get("sizeBytes") or item.get("size_bytes"), 0),
            "metadata": metadata,
            "created_at": compact_text(item.get("createdAt") or item.get("created_at") or now_iso(), 80),
        }

    def _sync_message_row(self, payload, item, identity, policy):
        session_id = compact_text(item.get("sessionId") or item.get("session_id") or payload.get("sessionId") or payload.get("session_id"), 180)
        request_id = compact_text(item.get("requestId") or item.get("request_id") or payload.get("requestId") or payload.get("request_id"), 180)
        message_id = compact_text(item.get("messageId") or item.get("message_id") or item.get("id"), 180)
        seq = as_int(item.get("seq") or item.get("messageSeq") or item.get("message_seq"), 0, 0, 10_000_000_000)
        role = compact_text(item.get("role") or "message", 40)
        if "content" in item:
            content_value = item.get("content")
        elif "text" in item:
            content_value = item.get("text")
        else:
            content_value = ""
        content_json = canonical_json(content_value)
        content_bytes = content_json.encode("utf-8")
        max_content_bytes = int(policy["phase2"]["maxContentBytes"] or 0)
        if max_content_bytes and len(content_bytes) > max_content_bytes:
            raise ValueError("sync message content too large")
        content_sha256 = hashlib.sha256(content_bytes).hexdigest()
        extras = sync_safe_json(
            item.get("extras") or item.get("metadata") or {},
            deny_keys=(
                SYNC_DETAIL_DENY_KEYS
                | SYNC_ARTIFACT_PATH_KEYS
                | {"content", "text", "message", "messages", "idempotencykey", "idempotency_key"}
            ),
        )
        base_key = "|".join([
            identity["org_id"],
            identity["user_key"],
            session_id,
            request_id,
            str(seq),
            role,
            message_id,
            content_sha256,
        ])
        sync_key = compact_text(
            item.get("idempotencyKey") or item.get("idempotency_key") or f"message:{short_hash(base_key, 40)}",
            180,
        )
        return {
            **identity,
            "sync_key": sync_key,
            "session_id": session_id,
            "request_id": request_id,
            "message_id": message_id,
            "seq": seq,
            "role": role,
            "content": content_json,
            "content_sha256": content_sha256,
            "content_size_bytes": len(content_bytes),
            "extras": extras,
            "created_at": compact_text(item.get("createdAt") or item.get("created_at") or now_iso(), 80),
        }

    def ingest_sync_events(self, payload, token="", device_id="", require_user=True):
        identity = self._sync_identity(payload, token=token, device_id=device_id, require_user=require_user)
        raw_events = payload.get("events") if isinstance(payload.get("events"), list) else []
        raw_artifacts = payload.get("artifacts") if isinstance(payload.get("artifacts"), list) else []
        if isinstance(payload.get("artifact"), dict):
            raw_artifacts = [*raw_artifacts, payload["artifact"]]
        if not raw_events and not raw_artifacts and (payload.get("eventType") or payload.get("event_type")):
            raw_events = [payload]
        if len(raw_events) > 100 or len(raw_artifacts) > 100:
            raise ValueError("sync batch too large")

        stamp = now_iso()
        event_rows = [
            self._sync_event_row(payload, item, identity)
            for item in raw_events
            if isinstance(item, dict)
        ]
        artifact_rows = [
            self._sync_artifact_row(payload, item, identity)
            for item in raw_artifacts
            if isinstance(item, dict)
        ]
        with self.connect() as conn:
            for row in event_rows:
                conn.execute(
                    """
                    INSERT INTO sync_events
                    (id, sync_key, event_type, org_id, user_email, user_key, device_id, session_id,
                     request_id, status, source, detail, created_at, ingested_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(sync_key) DO UPDATE SET
                        event_type=excluded.event_type,
                        org_id=excluded.org_id,
                        user_email=excluded.user_email,
                        user_key=excluded.user_key,
                        device_id=excluded.device_id,
                        session_id=excluded.session_id,
                        request_id=excluded.request_id,
                        status=excluded.status,
                        source=excluded.source,
                        detail=excluded.detail,
                        created_at=excluded.created_at,
                        ingested_at=excluded.ingested_at
                    """,
                    (
                        str(uuid.uuid4()),
                        row["sync_key"],
                        row["event_type"],
                        row["org_id"],
                        row["user_email"],
                        row["user_key"],
                        row["device_id"],
                        row["session_id"],
                        row["request_id"],
                        row["status"],
                        row["source"],
                        json_dumps(row["detail"]),
                        row["created_at"],
                        stamp,
                    ),
                )
            for row in artifact_rows:
                conn.execute(
                    """
                    INSERT INTO sync_artifacts
                    (id, sync_key, artifact_id, org_id, user_email, user_key, device_id, session_id,
                     request_id, kind, intent, operation, status, title, path_hash, path_ext,
                     mime_type, size_bytes, metadata, created_at, ingested_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(sync_key) DO UPDATE SET
                        artifact_id=excluded.artifact_id,
                        org_id=excluded.org_id,
                        user_email=excluded.user_email,
                        user_key=excluded.user_key,
                        device_id=excluded.device_id,
                        session_id=excluded.session_id,
                        request_id=excluded.request_id,
                        kind=excluded.kind,
                        intent=excluded.intent,
                        operation=excluded.operation,
                        status=excluded.status,
                        title=excluded.title,
                        path_hash=excluded.path_hash,
                        path_ext=excluded.path_ext,
                        mime_type=excluded.mime_type,
                        size_bytes=excluded.size_bytes,
                        metadata=excluded.metadata,
                        created_at=excluded.created_at,
                        ingested_at=excluded.ingested_at
                    """,
                    (
                        str(uuid.uuid4()),
                        row["sync_key"],
                        row["artifact_id"],
                        row["org_id"],
                        row["user_email"],
                        row["user_key"],
                        row["device_id"],
                        row["session_id"],
                        row["request_id"],
                        row["kind"],
                        row["intent"],
                        row["operation"],
                        row["status"],
                        row["title"],
                        row["path_hash"],
                        row["path_ext"],
                        row["mime_type"],
                        row["size_bytes"],
                        json_dumps(row["metadata"]),
                        row["created_at"],
                        stamp,
                    ),
                )
            if event_rows or artifact_rows:
                self.audit(
                    conn,
                    "sync.ingest",
                    identity["user_email"] or "client",
                    "client_sync",
                    {"events": len(event_rows), "artifacts": len(artifact_rows)},
                )
            conn.commit()
        return {
            "ok": True,
            "eventsAccepted": len(event_rows),
            "artifactsAccepted": len(artifact_rows),
        }

    def ingest_event(self, payload, include_state=True, token="", device_id="", require_user=False):
        kind = compact_text(payload.get("type") or payload.get("kind"), 32)
        stamp = now_iso()
        user_email = compact_text(payload.get("userEmail") or payload.get("user_email"), 180).lower()
        device_from_header = compact_text(device_id, 180)
        resolved_device_id = compact_text(payload.get("deviceId") or payload.get("device_id") or device_from_header, 180)
        if token or require_user:
            user = self.require_session(token, device_id or payload.get("deviceId"))
            user_email = user["email"]
            device_from_header = compact_text(user["device_id"] or device_from_header, 180)
            resolved_device_id = device_from_header
        with self.connect() as conn:
            if kind == "usage":
                detail = payload.get("detail") or {}
                input_tokens = as_int(payload.get("inputTokens") or payload.get("input_tokens") or detail.get("inputTokens"))
                output_tokens = as_int(payload.get("outputTokens") or payload.get("output_tokens") or detail.get("outputTokens"))
                total_tokens = as_int(payload.get("totalTokens") or payload.get("total_tokens") or detail.get("totalTokens"))
                amount = as_int(payload.get("amount"), 1)
                if total_tokens <= 0:
                    total_tokens = input_tokens + output_tokens if input_tokens or output_tokens else amount
                conn.execute(
                    """
                    INSERT INTO usage_events
                    (id, category, label, amount, user_email, device_id, session_id, model, provider,
                     input_tokens, output_tokens, total_tokens, detail, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(uuid.uuid4()),
                        compact_text(payload.get("category") or "chat", 80),
                        compact_text(payload.get("label") or payload.get("category") or "桌面端调用", 120),
                        amount,
                        user_email,
                        resolved_device_id,
                        compact_text(payload.get("sessionId") or payload.get("session_id"), 180),
                        compact_text(payload.get("model") or detail.get("model"), 120),
                        compact_text(payload.get("provider") or detail.get("provider"), 80),
                        input_tokens,
                        output_tokens,
                        total_tokens,
                        json_dumps(detail),
                        stamp,
                    ),
                )
            elif kind == "error":
                conn.execute(
                    """
                    INSERT INTO error_logs
                    (id, level, source, message, status, user_email, device_id, session_id, tool, app_version, detail, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(uuid.uuid4()),
                        "error",
                        compact_text(payload.get("source") or "Desktop", 80),
                        compact_text(payload.get("message") or "客户端事件", 1000),
                        "unread",
                        user_email,
                        resolved_device_id,
                        compact_text(payload.get("sessionId") or payload.get("session_id"), 180),
                        compact_text(payload.get("tool"), 120),
                        compact_text(payload.get("appVersion") or payload.get("app_version") or VERSION, 40),
                        json_dumps(payload.get("detail") or {}),
                        stamp,
                    ),
                )
            elif kind in ("warn", "info", "success"):
                pass
            else:
                raise ValueError("unsupported event type")
            self.audit(conn, "event.ingest", payload.get("actor", "desktop"), kind, payload)
            conn.commit()
        result = {"ok": True}
        if include_state:
            result["state"] = self.state()
        return result


class AdminHandler(BaseHTTPRequestHandler):
    store = None

    def log_message(self, fmt, *args):
        return

    def _path(self):
        path = urlparse(self.path).path.rstrip("/") or "/"
        for prefix in ("/ecorex-agent/admin/api", "/ecorex-agent/api/admin", "/admin/api", "/api/admin"):
            if path == prefix:
                return "/"
            if path.startswith(prefix + "/"):
                return path[len(prefix):]
        prefix = "/ecorex-agent/client"
        if path == prefix:
            return "/client"
        if path.startswith(prefix + "/"):
            return "/client" + path[len(prefix):]
        return path

    def _query(self):
        return {key: values[-1] for key, values in parse_qs(urlparse(self.path).query).items()}

    def _is_artifact_file_sync_path(self, path):
        parts = path.strip("/").split("/")
        return (
            path in ("/client/sync/artifact-files", "/client/sync/artifact-blobs")
            or (
                len(parts) == 4
                and parts[0] == "client"
                and parts[1] == "sync"
                and parts[2] in ("artifact-files", "artifact-blobs")
            )
        )

    def _json_body_limit(self):
        default_limit = 1024 * 1024
        path = self._path()
        if not self._is_artifact_file_sync_path(path):
            return default_limit
        policy = self.store.sync_policy() if self.store else {}
        phase3 = policy.get("phase3") or {}
        if not phase3.get("artifactFilesEnabled"):
            return default_limit
        chunk_bytes = int(phase3.get("chunkBytes") or 0)
        return min(max(default_limit, chunk_bytes * 2 + 512 * 1024), 128 * 1024 * 1024)

    def _origin_allowed(self):
        origin = self.headers.get("Origin", "")
        if not origin:
            return ""
        allowed = {
            item.strip()
            for item in os.environ.get("ECOREX_ALLOWED_ORIGINS", "").split(",")
            if item.strip()
        }
        host = self.headers.get("Host", "")
        if host:
            allowed.add(f"https://{host}")
            allowed.add(f"http://{host}")
        return origin if origin in allowed else ""

    def _send_cors_headers(self):
        origin = self._origin_allowed()
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Credentials", "true")

    def _json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self._send_cors_headers()
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        length = int(self.headers.get("Content-Length") or "0")
        if length > self._json_body_limit():
            raise ValueError("payload too large")
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8-sig"))

    def _client_key_valid(self):
        provided = self.headers.get("X-EcoreX-Client-Key", "")
        return any(hmac.compare_digest(expected, provided) for expected in client_event_keys())

    def _admin_authorized(self):
        token = os.environ.get("ECOREX_ADMIN_TOKEN") or os.environ.get("ECOREX_ADMIN_API_KEY")
        if token:
            provided = self.headers.get("X-EcoreX-Admin-Key", "")
            auth = self.headers.get("Authorization", "")
            if auth.lower().startswith("bearer "):
                provided = auth.split(" ", 1)[1].strip()
            if constant_equal(provided, token):
                return True

        password = os.environ.get("ECOREX_ADMIN_PASSWORD", "")
        if password:
            auth = self.headers.get("Authorization", "")
            if auth.lower().startswith("basic "):
                try:
                    decoded = base64.b64decode(auth.split(" ", 1)[1].strip()).decode("utf-8")
                    provided_user, provided_password = decoded.split(":", 1)
                    return constant_equal(provided_password, password) and any(
                        constant_equal(provided_user, username) for username in admin_basic_usernames()
                    )
                except Exception:
                    return False
        return False

    def _require_admin(self):
        if not admin_auth_configured():
            self._json(503, {"ok": False, "error": "admin authentication is not configured"})
            return False
        if self._admin_authorized():
            return True
        body = json.dumps({"ok": False, "error": "admin authentication required"}, ensure_ascii=False).encode("utf-8")
        self.send_response(401)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self._send_cors_headers()
        self.send_header("WWW-Authenticate", 'Basic realm="EcoreX Admin"')
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        return False

    def _user_token(self):
        auth = self.headers.get("Authorization", "")
        if auth.lower().startswith("bearer "):
            return auth.split(" ", 1)[1].strip()
        return self.headers.get("X-EcoreX-User-Token", "")

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header(
            "Access-Control-Allow-Headers",
            "Authorization, Content-Type, X-EcoreX-Admin-Key, X-EcoreX-Client-Key, X-EcoreX-User-Email, X-EcoreX-User-Token, X-EcoreX-Device-Id, X-EcoreX-Org-Id",
        )
        self.send_header("Access-Control-Allow-Methods", "GET,POST,PUT,PATCH,DELETE,OPTIONS")
        self._send_cors_headers()
        self.end_headers()

    def do_GET(self):
        path = self._path()
        try:
            if path in ("/", "/health"):
                self._json(200, {"ok": True, "product": "EcoreX", "version": VERSION})
            elif path == "/state":
                if not self._require_admin():
                    return
                self._json(200, self.store.state(self._query()))
            elif path == "/logs":
                if not self._require_admin():
                    return
                self._json(200, {"ok": True, "logs": self.store.state(self._query())["logs"]})
            elif path == "/runtime-audit":
                if not self._require_admin():
                    return
                with self.store.connect() as conn:
                    self._json(200, {"ok": True, "runtimeAudit": self.store.runtime_audit(conn, self._query())})
            elif path in ("/client/model-config", "/model-config"):
                if not self._client_key_valid():
                    self._json(403, {"ok": False, "error": "invalid client key"})
                    return
                self._json(
                    200,
                    self.store.resolve_client_model_config(
                        self.headers.get("X-EcoreX-User-Email", ""),
                        self.headers.get("X-EcoreX-Device-Id", ""),
                        self._user_token(),
                    ),
                )
            elif path in ("/client/capability-policy", "/capability-policy/client"):
                if not self._client_key_valid():
                    self._json(403, {"ok": False, "error": "invalid client key"})
                    return
                self._json(200, self.store.resolve_client_capability_policy())
            elif path in ("/client/sync/status", "/sync/status/client"):
                if not self._client_key_valid():
                    self._json(403, {"ok": False, "error": "invalid client key"})
                    return
                self._json(200, self.store.sync_status())
            elif path in ("/client/sync/policy", "/sync/policy/client"):
                if not self._client_key_valid():
                    self._json(403, {"ok": False, "error": "invalid client key"})
                    return
                self._json(200, {"ok": True, "syncPolicy": self.store.sync_policy()})
            else:
                self._json(404, {"ok": False, "error": "not found"})
        except ForbiddenError as exc:
            self._json(403, {"ok": False, "error": str(exc), "syncPolicy": self.store.sync_policy()})
        except PermissionError as exc:
            self._json(401, {"ok": False, "error": str(exc)})
        except NotImplementedError as exc:
            self._json(501, {"ok": False, "error": str(exc), "syncPolicy": self.store.sync_policy()})
        except Exception as exc:
            self._json(500, {"ok": False, "error": str(exc)})

    def do_POST(self):
        path = self._path()
        try:
            payload = self._read_json()
            if path == "/users":
                if not self._require_admin():
                    return
                self._json(200, self.store.create_user(payload))
            elif path in ("/client/auth/change-password", "/auth/change-password"):
                if not self._client_key_valid():
                    self._json(403, {"ok": False, "error": "invalid client key"})
                    return
                self._json(200, self.store.change_password({**payload, "token": self._user_token()}))
            elif path in ("/client/auth/login", "/auth/login"):
                if not self._client_key_valid():
                    self._json(403, {"ok": False, "error": "invalid client key"})
                    return
                self._json(200, self.store.login(payload))
            elif path in ("/client/quota/check", "/quota/check"):
                if not self._client_key_valid():
                    self._json(403, {"ok": False, "error": "invalid client key"})
                    return
                self._json(200, self.store.check_quota(payload, self._user_token(), self.headers.get("X-EcoreX-Device-Id", "")))
            elif path == "/logs/mark-read":
                if not self._require_admin():
                    return
                self._json(200, self.store.mark_logs_read(payload))
            elif path == "/usage/reset":
                if not self._require_admin():
                    return
                self._json(200, self.store.reset_usage(payload))
            elif path == "/capability-policy":
                if not self._require_admin():
                    return
                self._json(200, self.store.update_policy(payload))
            elif path == "/model-credentials/global/test":
                if not self._require_admin():
                    return
                self._json(200, self.store.test_model_credential(payload))
            elif path in ("/model-credentials", "/model-credentials/global"):
                if not self._require_admin():
                    return
                self._json(200, self.store.create_model_credential(payload))
            elif path == "/events":
                if not self._require_admin():
                    return
                self._json(200, self.store.ingest_event(payload))
            elif path in ("/client/events", "/events/client"):
                if not self._client_key_valid():
                    self._json(403, {"ok": False, "error": "invalid client key"})
                    return
                self._json(
                    200,
                    self.store.ingest_event(
                        {**payload, "actor": "desktop-client"},
                        include_state=False,
                        token=self._user_token(),
                        device_id=self.headers.get("X-EcoreX-Device-Id", ""),
                        require_user=True,
                    ),
                )
            elif path in ("/client/sync/events", "/events/sync/client", "/client/sync/artifacts"):
                if not self._client_key_valid():
                    self._json(403, {"ok": False, "error": "invalid client key"})
                    return
                self._json(
                    200,
                    self.store.ingest_sync_events(
                        {**payload, "actor": "desktop-client"},
                        token=self._user_token(),
                        device_id=self.headers.get("X-EcoreX-Device-Id", ""),
                        require_user=True,
                    ),
                )
            elif path in ("/client/sync/messages", "/sync/messages/client"):
                if not self._client_key_valid():
                    self._json(403, {"ok": False, "error": "invalid client key"})
                    return
                self._json(
                    200,
                    self.store.ingest_sync_messages(
                        {**payload, "actor": "desktop-client"},
                        token=self._user_token(),
                        device_id=self.headers.get("X-EcoreX-Device-Id", ""),
                        require_user=True,
                    ),
                )
            elif path in ("/client/sync/artifact-files", "/client/sync/artifact-blobs"):
                if not self._client_key_valid():
                    self._json(403, {"ok": False, "error": "invalid client key"})
                    return
                self._json(
                    200,
                    self.store.ingest_sync_artifact_file(
                        {**payload, "actor": "desktop-client"},
                        token=self._user_token(),
                        device_id=self.headers.get("X-EcoreX-Device-Id", ""),
                        require_user=True,
                    ),
                )
            else:
                parts = path.strip("/").split("/")
                if len(parts) == 3 and parts[0] == "users" and parts[2] == "reset-password":
                    if not self._require_admin():
                        return
                    self._json(200, self.store.reset_user_password(parts[1], payload))
                else:
                    self._json(404, {"ok": False, "error": "not found"})
        except ForbiddenError as exc:
            self._json(403, {"ok": False, "error": str(exc), "syncPolicy": self.store.sync_policy()})
        except PermissionError as exc:
            self._json(401, {"ok": False, "error": str(exc)})
        except NotImplementedError as exc:
            self._json(501, {"ok": False, "error": str(exc), "syncPolicy": self.store.sync_policy()})
        except RateLimitError as exc:
            self._json(429, {"ok": False, "error": str(exc), "syncPolicy": self.store.sync_policy()})
        except ValueError as exc:
            self._json(400, {"ok": False, "error": str(exc)})
        except sqlite3.IntegrityError as exc:
            self._json(409, {"ok": False, "error": str(exc)})
        except Exception as exc:
            self._json(500, {"ok": False, "error": str(exc)})

    def do_PUT(self):
        path = self._path()
        try:
            payload = self._read_json()
            parts = path.strip("/").split("/")
            if (
                path in ("/client/sync/artifact-files", "/client/sync/artifact-blobs")
                or (
                    len(parts) == 4
                    and parts[0] == "client"
                    and parts[1] == "sync"
                    and parts[2] in ("artifact-files", "artifact-blobs")
                )
            ):
                if not self._client_key_valid():
                    self._json(403, {"ok": False, "error": "invalid client key"})
                    return
                self._json(
                    200,
                    self.store.ingest_sync_artifact_file(
                        {**payload, "artifactId": parts[3] if len(parts) == 4 else payload.get("artifactId")},
                        token=self._user_token(),
                        device_id=self.headers.get("X-EcoreX-Device-Id", ""),
                        require_user=True,
                    ),
                )
            else:
                self._json(404, {"ok": False, "error": "not found"})
        except ForbiddenError as exc:
            self._json(403, {"ok": False, "error": str(exc), "syncPolicy": self.store.sync_policy()})
        except PermissionError as exc:
            self._json(401, {"ok": False, "error": str(exc)})
        except NotImplementedError as exc:
            self._json(501, {"ok": False, "error": str(exc), "syncPolicy": self.store.sync_policy()})
        except RateLimitError as exc:
            self._json(429, {"ok": False, "error": str(exc), "syncPolicy": self.store.sync_policy()})
        except ValueError as exc:
            self._json(400, {"ok": False, "error": str(exc)})
        except Exception as exc:
            self._json(500, {"ok": False, "error": str(exc)})

    def do_PATCH(self):
        path = self._path()
        try:
            payload = self._read_json()
            parts = path.strip("/").split("/")
            if len(parts) == 2 and parts[0] == "users":
                if not self._require_admin():
                    return
                self._json(200, self.store.update_user(parts[1], payload))
            elif len(parts) == 2 and parts[0] == "model-credentials":
                if not self._require_admin():
                    return
                self._json(200, self.store.update_model_credential(parts[1], payload))
            elif path == "/model-credentials/global":
                if not self._require_admin():
                    return
                self._json(200, self.store.upsert_global_model(payload))
            else:
                self._json(404, {"ok": False, "error": "not found"})
        except KeyError as exc:
            self._json(404, {"ok": False, "error": str(exc)})
        except ValueError as exc:
            self._json(400, {"ok": False, "error": str(exc)})
        except Exception as exc:
            self._json(500, {"ok": False, "error": str(exc)})

    def do_DELETE(self):
        path = self._path()
        try:
            payload = {}
            parts = path.strip("/").split("/")
            if len(parts) == 2 and parts[0] == "users":
                if not self._require_admin():
                    return
                self._json(200, self.store.delete_user(parts[1], payload))
            elif len(parts) == 2 and parts[0] == "model-credentials":
                if not self._require_admin():
                    return
                self._json(400, {"ok": False, "error": "global model cannot be deleted"})
            else:
                self._json(404, {"ok": False, "error": "not found"})
        except KeyError as exc:
            self._json(404, {"ok": False, "error": str(exc)})
        except Exception as exc:
            self._json(500, {"ok": False, "error": str(exc)})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default=os.environ.get("ECOREX_ADMIN_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("ECOREX_ADMIN_PORT", "18084")))
    parser.add_argument("--db", default=os.environ.get("ECOREX_ADMIN_DB", "./ecorex-admin.sqlite3"))
    args = parser.parse_args()

    AdminHandler.store = AdminStore(args.db)
    server = ThreadingHTTPServer((args.host, args.port), AdminHandler)
    print(f"EcoreX Admin API {VERSION} listening on {args.host}:{args.port}, db={args.db}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
