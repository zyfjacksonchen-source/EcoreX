#!/usr/bin/env python3
"""Server-authoritative usage analytics for the EcoreX operator panel."""
from __future__ import annotations

import json
import os
import pathlib
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

VERSION = "1.0.3"
DB_PATH = "/srv/ecorex-agent-admin/data/ecorex-admin.sqlite3"
CONTROL_PLANE_DB_PATH = os.environ.get(
    "ECOREX_CONTROL_PLANE_DATABASE_PATH",
    "/var/lib/ecorex/control-plane/control-plane.sqlite3",
)
GATEWAY_DB_PATH = os.environ.get(
    "ECOREX_GATEWAY_DATABASE_PATH",
    "/var/lib/ecorex/gateway/gateway.sqlite3",
)
HOST = "127.0.0.1"
PORT = 18105
TZ = timezone(timedelta(hours=8))
MAX_DATA_RANGE_DAYS = 90
MAX_DATA_RESPONSE_ROWS = 8_000
ADMIN_API_DIRS = [
    os.environ.get("ECOREX_ADMIN_API_DIR", ""),
    "/srv/ecorex-agent-admin/app",
    "/srv/ecorex-agent-download/current/admin",
    str(pathlib.Path(__file__).resolve().parent.parent / "ecorex-admin-api"),
]


class UsagePanelRequestError(ValueError):
    """A bounded client error that must be raised before payload materialization."""

    def __init__(
        self,
        *,
        status: int,
        code: str,
        message: str,
        actual: int | None = None,
        limit: int | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.actual = actual
        self.limit = limit

    def payload(self) -> dict[str, object]:
        value: dict[str, object] = {
            "ok": False,
            "error": self.code,
            "message": self.message,
        }
        if self.actual is not None:
            value["actual"] = self.actual
        if self.limit is not None:
            value["limit"] = self.limit
        return value

EVENT_TYPE_ZH = {
    "run.accepted": "任务已接收",
    "run.completed": "任务已完成",
    "run.cancelled": "任务已取消",
    "run.failed": "任务失败",
    "run.interrupted": "任务中断",
    "artifact.updated": "产物已更新",
    "artifact.limit": "产物数量受限",
    "tool.started": "工具开始执行",
    "tool.finished": "工具执行结束",
    "tool.failed": "工具执行失败",
}
STATUS_ZH = {
    "running": "进行中",
    "completed": "已完成",
    "failed": "失败",
    "ready": "已就绪",
    "cancelled": "已取消",
    "limited": "受限",
    "pending": "等待中",
    "queued": "排队中",
}
TOOL_ZH = {
    "feishu_cli": "飞书工具",
    "bash": "命令行",
    "read": "读取文件",
    "vision": "图片识别",
    "ls": "查看目录",
    "write": "写入文件",
    "find": "查找文件",
    "send": "发送消息",
    "edit": "编辑文件",
    "host_diagnostics": "本机诊断",
    "imagegen": "图片生成",
    "web_search": "网页搜索",
    "web_fetch": "网页读取",
    "browser": "浏览器操作",
    "ocr": "图片文字识别",
    "subagent": "子任务助手",
    "scheduler": "定时任务",
    "mcp": "外部工具连接",
}
DETAIL_KEY_ZH = {
    "tool": "工具",
    "toolCallId": "工具调用编号",
    "executionTime": "执行耗时",
    "artifactCount": "产物数量",
    "stream": "流式返回",
    "hasUsage": "包含用量信息",
    "hasTurnIdentity": "包含轮次身份",
    "acknowledged": "已确认",
    "omittedArtifactCount": "省略产物数量",
    "inputTokens": "输入 Token",
    "outputTokens": "输出 Token",
    "totalTokens": "总 Token",
    "input_tokens": "输入 Token",
    "output_tokens": "输出 Token",
    "total_tokens": "总 Token",
}


def parse_date(value: str, fallback: datetime) -> datetime:
    if not value:
        return fallback
    try:
        return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=TZ)
    except ValueError:
        return fallback


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat((value or "").replace("Z", "+00:00")).astimezone(TZ)


def yesno(value) -> str:
    if isinstance(value, bool):
        return "是" if value else "否"
    lowered = str(value).lower()
    if lowered == "true":
        return "是"
    if lowered == "false":
        return "否"
    return str(value)


def json_loads(value, default=None):
    try:
        return json.loads(value or "{}")
    except Exception:
        return default if default is not None else {}


def metadata_feedback_signal(metadata: dict) -> str:
    raw = (
        metadata.get("artifactFeedbackSignal")
        or metadata.get("artifact_feedback_signal")
        or metadata.get("feedbackSignal")
        or metadata.get("feedback_signal")
        or "default"
    )
    signal = str(raw or "default").strip().lower()
    return signal if signal in {"default", "thumbs_up", "thumbs_down"} else "default"


def metadata_validity(metadata: dict) -> str:
    raw = (
        metadata.get("artifactValidity")
        or metadata.get("artifact_validity")
        or metadata.get("validity")
        or "valid"
    )
    validity = str(raw or "valid").strip().lower()
    if validity not in {"valid", "invalid"}:
        validity = "valid"
    return "invalid" if validity == "invalid" or metadata_feedback_signal(metadata) == "thumbs_down" else "valid"


def is_effective_artifact(status: str, metadata: dict) -> bool:
    normalized_status = str(status or "ready").strip().lower()
    if metadata_validity(metadata) == "invalid":
        return False
    return normalized_status in {"", "ready", "complete", "completed", "valid"}


def load_admin_store():
    for item in ADMIN_API_DIRS:
        if item and pathlib.Path(item).is_dir() and item not in sys.path:
            sys.path.insert(0, item)
    from ecorex_admin_api import AdminStore  # type: ignore

    class ReadOnlyAdminStore(AdminStore):
        def __init__(self, db_path):
            self.db_path = db_path

        def connect(self):
            conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
            return conn

    return ReadOnlyAdminStore(DB_PATH)


def build_runtime_audit(query: dict) -> dict:
    allowed_filters = {
        "limit",
        "auditLimit",
        "userEmail",
        "user_email",
        "userKey",
        "user_key",
        "deviceId",
        "device_id",
        "eventType",
        "event_type",
        "start",
        "end",
        "from",
        "to",
        "createdFrom",
        "created_from",
        "createdTo",
        "created_to",
    }
    filters = {
        key: values[0]
        for key, values in (query or {}).items()
        if values and key in allowed_filters
    }
    if "limit" not in filters and "auditLimit" not in filters:
        filters["limit"] = "80"
    store = load_admin_store()
    with store.connect() as conn:
        audit = store.runtime_audit(conn, filters)
    audit["filters"] = {
        key: filters.get(key)
        for key in ("userEmail", "user_email", "userKey", "user_key", "start", "end", "from", "to", "createdFrom", "createdTo")
        if filters.get(key)
    }
    payload = {"ok": True, "version": VERSION, "runtimeAudit": audit}
    payload["filters"] = audit["filters"]
    for key in ("summary", "actionTypeCounts", "actionTypeLabels", "userActions", "effectiveArtifacts", "feedbackTraces"):
        payload[key] = audit.get(key)
    return payload


def detail_summary(value: str) -> str:
    obj = json_loads(value, {})
    if not isinstance(obj, dict) or not obj:
        return ""
    parts = []
    for key, raw in obj.items():
        label = DETAIL_KEY_ZH.get(key, key)
        if key == "tool":
            shown = TOOL_ZH.get(str(raw), str(raw))
        elif key in {"stream", "hasUsage", "hasTurnIdentity", "acknowledged"}:
            shown = yesno(raw)
        elif key == "executionTime":
            try:
                shown = f"{float(raw):g} 秒"
            except Exception:
                shown = str(raw)
        else:
            shown = str(raw)
        parts.append(f"{label}：{shown}")
    return "；".join(parts)[:500]


def token_number(value) -> int:
    if isinstance(value, bool) or value is None:
        return 0
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        cleaned = value.replace(",", "").strip()
        if cleaned.isdigit():
            return int(cleaned)
    return 0


def token_usage_from_detail(value: str) -> dict:
    obj = json_loads(value, {})
    usage = {"inputTokens": 0, "outputTokens": 0, "totalTokens": 0, "hasUsage": None}
    input_keys = {"inputtokens", "inputtoken", "prompttokens", "prompttoken", "inputtokencount", "prompttokencount"}
    output_keys = {
        "outputtokens",
        "outputtoken",
        "completiontokens",
        "completiontoken",
        "outputtokencount",
        "completiontokencount",
    }
    total_keys = {"totaltokens", "totaltoken", "tokens", "tokencount", "total"}

    def normalized(key) -> str:
        return "".join(ch for ch in str(key).lower() if ch.isalnum())

    def visit(item):
        if isinstance(item, dict):
            for key, raw in item.items():
                norm = normalized(key)
                if norm == "hasusage" and isinstance(raw, bool):
                    usage["hasUsage"] = raw
                elif norm in input_keys:
                    usage["inputTokens"] += token_number(raw)
                elif norm in output_keys:
                    usage["outputTokens"] += token_number(raw)
                elif norm in total_keys:
                    usage["totalTokens"] += token_number(raw)
                if isinstance(raw, (dict, list)):
                    visit(raw)
        elif isinstance(item, list):
            for child in item:
                visit(child)

    visit(obj)
    if not usage["totalTokens"]:
        usage["totalTokens"] = usage["inputTokens"] + usage["outputTokens"]
    if usage["hasUsage"] is None and usage["totalTokens"]:
        usage["hasUsage"] = True
    return usage


def canonical_email(value: object) -> str:
    return str(value or "").strip().casefold()


def _read_optional_rows(
    paths: list[str],
    table: str,
    query: str,
    parameters: tuple[object, ...] = (),
) -> list[dict]:
    """Read the first available copy of an optional v1 fact table.

    Production keeps the legacy panel, Control Plane and Gateway databases in
    separate files. Tests and migration rehearsals may co-locate their tables.
    Trying the configured authority first and the legacy database second keeps
    both layouts supported without counting a copied table twice.
    """

    seen: set[str] = set()
    for raw_path in paths:
        path = str(raw_path or "").strip()
        if not path:
            continue
        normalized = os.path.normcase(os.path.abspath(path))
        if normalized in seen:
            continue
        seen.add(normalized)
        if not pathlib.Path(path).is_file():
            continue
        connection = sqlite3.connect(f"file:{pathlib.Path(path).as_posix()}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        try:
            exists = connection.execute(
                "SELECT 1 FROM sqlite_schema WHERE type='table' AND name=?",
                (table,),
            ).fetchone()
            if exists is None:
                continue
            return [dict(row) for row in connection.execute(query, parameters)]
        finally:
            connection.close()
    return []


def merged_identity_catalog(
    legacy_rows: list[dict],
    admin_rows: list[dict],
    observed_emails: set[str],
    observed_accounts: set[str],
) -> tuple[dict[str, str], list[str], dict[str, str]]:
    """Return display labels, stable identity order and account aliases.

    Email is the canonical cross-version identity when available. An account
    without email remains addressable by its canonical account id. The account
    alias map is what lets Gateway facts join the legacy email ledger.
    """

    entries: dict[str, str] = {}
    account_aliases: dict[str, str] = {}
    for row in legacy_rows:
        if row.get("deleted_at") not in (None, ""):
            continue
        identity = canonical_email(row.get("email"))
        if not identity:
            continue
        entries[identity] = str(row.get("name") or "").strip() or identity.split("@")[0]

    for row in admin_rows:
        account_id = canonical_email(row.get("account_id"))
        email = canonical_email(row.get("email"))
        identity = email or account_id
        if not identity:
            continue
        if account_id:
            account_aliases[account_id] = identity
        display_name = str(row.get("display_name") or "").strip()
        entries[identity] = display_name or entries.get(identity) or identity.split("@")[0]

    for email in observed_emails:
        if email:
            entries.setdefault(email, email.split("@")[0])
    for account_id in observed_accounts:
        if not account_id:
            continue
        identity = account_aliases.get(account_id, account_id)
        account_aliases.setdefault(account_id, identity)
        entries.setdefault(identity, identity.split("@")[0])

    if "" in observed_emails or "" in observed_accounts:
        entries.setdefault("", "未识别用户")

    name_counts = Counter(entries.values())
    labels = {
        identity: (
            name
            if name_counts[name] == 1 or not identity
            else f"{name} · {identity}"
        )
        for identity, name in entries.items()
    }
    identities = sorted(labels, key=lambda identity: labels[identity].casefold())
    return labels, identities, account_aliases


def validate_data_request(start: datetime, end: datetime) -> dict[str, int]:
    """Reject unsafe ranges before ``build_payload`` allocates response rows."""

    if start.tzinfo is None or end.tzinfo is None or end <= start:
        raise UsagePanelRequestError(
            status=400,
            code="invalid_range",
            message="日期范围无效",
        )
    day_count = (end.date() - start.date()).days
    if day_count <= 0:
        raise UsagePanelRequestError(
            status=400,
            code="invalid_range",
            message="日期范围无效",
        )
    if day_count > MAX_DATA_RANGE_DAYS:
        raise UsagePanelRequestError(
            status=422,
            code="range_too_large",
            message=f"单次最多查询 {MAX_DATA_RANGE_DAYS} 天",
            actual=day_count,
            limit=MAX_DATA_RANGE_DAYS,
        )

    parameters = (start.isoformat(), end.isoformat())
    connection = sqlite3.connect(
        f"file:{pathlib.Path(DB_PATH).as_posix()}?mode=ro",
        uri=True,
    )
    connection.row_factory = sqlite3.Row
    try:
        legacy_users = [
            dict(row)
            for row in connection.execute(
                "SELECT name,email,deleted_at FROM users ORDER BY lower(email)"
            )
        ]
        observed_sync_identities = [
            dict(row)
            for row in connection.execute(
                """
                SELECT DISTINCT CASE
                    WHEN TRIM(COALESCE(user_email,'')) <> '' THEN user_email
                    ELSE COALESCE(user_key,'')
                END AS identity
                FROM sync_events
                WHERE datetime(created_at) >= datetime(?)
                  AND datetime(created_at) < datetime(?)
                """,
                parameters,
            )
        ]
        observed_usage_identities = [
            dict(row)
            for row in connection.execute(
                """
                SELECT DISTINCT user_email
                FROM usage_events
                WHERE datetime(created_at) >= datetime(?)
                  AND datetime(created_at) < datetime(?)
                """,
                parameters,
            )
        ]
        raw_event_count = int(
            connection.execute(
                """
                SELECT COUNT(*)
                FROM sync_events
                WHERE datetime(created_at) >= datetime(?)
                  AND datetime(created_at) < datetime(?)
                """,
                parameters,
            ).fetchone()[0]
        )
        legacy_task_upper_bound = int(
            connection.execute(
                """
                SELECT COUNT(DISTINCT request_id)
                FROM sync_events
                WHERE datetime(created_at) >= datetime(?)
                  AND datetime(created_at) < datetime(?)
                  AND TRIM(COALESCE(request_id,'')) <> ''
                """,
                parameters,
            ).fetchone()[0]
        )
    finally:
        connection.close()

    admin_rows = _read_optional_rows(
        [CONTROL_PLANE_DB_PATH, DB_PATH],
        "admin_ops_users",
        """
        SELECT account_id,display_name,email,organization_id,status
        FROM admin_ops_users
        ORDER BY lower(COALESCE(email,account_id)),account_id
        """,
    )
    gateway_accounts = _read_optional_rows(
        [GATEWAY_DB_PATH, DB_PATH],
        "gateway_requests",
        """
        SELECT DISTINCT account_id
        FROM gateway_requests
        WHERE (
            datetime(created_at) >= datetime(?) AND datetime(created_at) < datetime(?)
        ) OR (
            datetime(updated_at) >= datetime(?) AND datetime(updated_at) < datetime(?)
        )
        """,
        parameters + parameters,
    )
    gateway_count_rows = _read_optional_rows(
        [GATEWAY_DB_PATH, DB_PATH],
        "gateway_requests",
        """
        SELECT COUNT(*) AS request_count
        FROM gateway_requests
        WHERE (
            datetime(created_at) >= datetime(?) AND datetime(created_at) < datetime(?)
        ) OR (
            datetime(updated_at) >= datetime(?) AND datetime(updated_at) < datetime(?)
        )
        """,
        parameters + parameters,
    )
    observed_emails = {
        canonical_email(row.get("identity"))
        for row in observed_sync_identities
    }
    observed_emails.update(
        canonical_email(row.get("user_email"))
        for row in observed_usage_identities
    )
    observed_accounts = {
        canonical_email(row.get("account_id"))
        for row in gateway_accounts
    }
    _, identity_order, _ = merged_identity_catalog(
        legacy_users,
        admin_rows,
        observed_emails,
        observed_accounts,
    )
    identity_count = len(identity_order)
    gateway_task_upper_bound = (
        int(gateway_count_rows[0].get("request_count") or 0)
        if gateway_count_rows
        else 0
    )
    task_upper_bound = legacy_task_upper_bound + gateway_task_upper_bound
    scenario_count = min(len(SCENARIO_ORDER), task_upper_bound)
    summary_row_count = identity_count * day_count
    # Arrays returned by build_payload: summaryRows, rawEvents, tasks, users,
    # dates, scenarios, and the three chart series. Cross-ledger task overlap
    # is deliberately counted twice so this remains a safe upper bound.
    projected_response_rows = (
        summary_row_count
        + raw_event_count
        + task_upper_bound
        + (identity_count * 2)
        + (day_count * 2)
        + (scenario_count * 2)
        + 7
    )
    if projected_response_rows > MAX_DATA_RESPONSE_ROWS:
        raise UsagePanelRequestError(
            status=413,
            code="response_too_large",
            message="查询结果过大，请缩短日期范围",
            actual=projected_response_rows,
            limit=MAX_DATA_RESPONSE_ROWS,
        )
    return {
        "days": day_count,
        "identities": identity_count,
        "summary_rows": summary_row_count,
        "raw_events": raw_event_count,
        "task_upper_bound": task_upper_bound,
        "projected_response_rows": projected_response_rows,
    }


def build_data_request_payload(query: dict[str, list[str]]) -> dict:
    default_start = datetime(2026, 6, 22, tzinfo=TZ)
    default_end = datetime(2026, 6, 29, tzinfo=TZ)
    start = parse_date(query.get("start", [""])[0], default_start)
    end = parse_date(query.get("end", [""])[0], default_end)
    validate_data_request(start, end)
    return build_payload(start, end)


def usage_request_id(row: dict, gateway_request_ids: set[str] | None = None) -> str:
    direct = str(row.get("request_id") or "").strip()
    if direct:
        return direct
    detail = json_loads(row.get("detail"), {})
    if isinstance(detail, dict):
        for key in (
            "request_id",
            "requestId",
            "provider_request_id",
            "providerRequestId",
        ):
            value = str(detail.get(key) or "").strip()
            if value:
                return value
        for container_key in ("request", "usage", "metadata"):
            container = detail.get(container_key)
            if isinstance(container, dict):
                for key in ("request_id", "requestId", "provider_request_id"):
                    value = str(container.get(key) or "").strip()
                    if value:
                        return value
    row_id = str(row.get("id") or "").strip()
    if gateway_request_ids and row_id in gateway_request_ids:
        return row_id
    return ""


def usage_row_projection(row: dict) -> dict:
    """Normalize one immutable provider usage fact without parsing event prose.

    ``usage_events`` is the server ledger.  The old panel re-parsed every
    ``sync_events.detail`` object recursively, which could count input/output
    and total fields more than once and could not represent model calls that
    had no matching task event.
    """

    input_tokens = max(0, token_number(row.get("input_tokens")))
    output_tokens = max(0, token_number(row.get("output_tokens")))
    reported_total = max(0, token_number(row.get("total_tokens")))
    total_tokens = max(reported_total, input_tokens + output_tokens)
    detail = json_loads(row.get("detail"), {})
    if not isinstance(detail, dict):
        detail = {}
    cache_read = token_number(
        detail.get("cache_read_input_tokens")
        or detail.get("cacheReadInputTokens")
        or detail.get("cached_tokens")
        or detail.get("cachedTokens")
    )
    cache_write = token_number(
        detail.get("cache_write_input_tokens")
        or detail.get("cacheWriteInputTokens")
    )
    usage_source = str(
        detail.get("usageSource")
        or detail.get("usage_source")
        or ("provider" if total_tokens else "unreported")
    ).strip()
    return {
        "inputTokens": input_tokens,
        "outputTokens": output_tokens,
        "totalTokens": total_tokens,
        "cacheReadTokens": max(0, cache_read),
        "cacheWriteTokens": max(0, cache_write),
        "usageSource": usage_source[:64],
        "estimated": usage_source.casefold() == "estimated",
    }


def identity_catalog(user_rows: list[dict], observed_emails: set[str]) -> tuple[dict, list[str]]:
    active = [
        row
        for row in user_rows
        if row.get("deleted_at") in (None, "")
        and canonical_email(row.get("email"))
    ]
    name_counts = Counter(str(row.get("name") or "").strip() for row in active)
    labels: dict[str, str] = {}
    for row in active:
        email = canonical_email(row.get("email"))
        name = str(row.get("name") or "").strip() or email.split("@")[0]
        labels[email] = name if name_counts[name] == 1 else f"{name} · {email}"
    for email in sorted(observed_emails):
        if email and email not in labels:
            labels[email] = email.split("@")[0]
    if "" in observed_emails:
        labels[""] = "未识别用户"
    ordered = sorted(labels.values(), key=str.casefold)
    return labels, ordered


def result_class(status: str, event_type: str) -> str:
    s = (status or "").lower()
    t = (event_type or "").lower()
    if s == "completed" or t.endswith(".completed"):
        return "成功事件"
    if s in {"failed", "cancelled"} or t.endswith(".failed") or t.endswith(".cancelled"):
        return "失败事件"
    if s == "limited" or t.endswith(".limit"):
        return "受限事件"
    return "过程事件"


SCENARIO_ORDER = ["创作内容", "制作素材", "搜索查询", "处理数据", "编辑文档", "交付通知", "系统维护"]


def scenario_from_tool(tool: str, detail: dict | None = None) -> str:
    """Map old tool-centric buckets to the board taxonomy defined in the Tencent Doc."""
    normalized = (tool or "").strip().lower()
    text = json.dumps(detail or {}, ensure_ascii=False).lower()
    if any(token in text for token in ("飞书", "lark", "腾讯文档", "docs.qq", "docx", "online doc")):
        return "编辑文档"
    if any(token in text for token in ("xlsx", "excel", "csv", "word", "ppt", "pdf", "表格", "数据")):
        return "处理数据"
    if any(token in text for token in ("海报", "图片", "image", "ocr", "视觉", ".png", ".jpg", ".jpeg", ".webp")):
        return "制作素材"
    if any(token in text for token in ("搜索", "检索", "抓取", "http://", "https://", "网页")):
        return "搜索查询"
    if any(token in text for token in ("部署", "安装", "配置", "日志", "排错", "环境", "版本", "权限")):
        return "系统维护"
    if any(token in text for token in ("打包", "发送", "通知", "提醒", "展示", "预览")):
        return "交付通知"

    if normalized in {"feishu_cli", "lark_doc", "lark_sheets", "tencent_docs", "docs", "mcp"}:
        return "编辑文档"
    if normalized in {"vision", "ocr", "imagegen", "image", "image_edit"}:
        return "制作素材"
    if normalized in {"web_search", "web_fetch", "browser", "find"}:
        return "搜索查询"
    if normalized in {"read", "data", "spreadsheet", "sheet"}:
        return "处理数据"
    if normalized in {"write", "edit"}:
        return "创作内容"
    if normalized in {"send", "scheduler"}:
        return "交付通知"
    if normalized in {"bash", "host_diagnostics", "subagent", "terminal", "shell"}:
        return "系统维护"
    return "创作内容"


def task_status_category(rec: dict) -> str:
    if rec.get("failed"):
        return "失败"
    if rec.get("completedAt"):
        return "成功"
    if rec.get("cancelled"):
        return "中止"
    return "失败"


def build_payload(start: datetime, end: datetime) -> dict:
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        user_rows = [
            dict(row)
            for row in conn.execute(
                "SELECT name, email, deleted_at FROM users ORDER BY lower(email)"
            )
        ]
        rows = [
            dict(row)
            for row in conn.execute(
                """
                SELECT id, sync_key, event_type, org_id, user_email, user_key, device_id,
                       session_id, request_id, source, status, detail, created_at, ingested_at
                FROM sync_events
                WHERE datetime(created_at) >= datetime(?) AND datetime(created_at) < datetime(?)
                ORDER BY datetime(created_at), id
                """,
                (start.isoformat(), end.isoformat()),
            )
        ]
        legacy_usage_rows = [
            dict(row)
            for row in conn.execute(
                """
                SELECT *
                FROM usage_events
                WHERE datetime(created_at) >= datetime(?) AND datetime(created_at) < datetime(?)
                ORDER BY datetime(created_at), id
                """,
                (start.isoformat(), end.isoformat()),
            )
        ]
        try:
            artifact_rows = [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT user_email, user_key, status, metadata, created_at
                    FROM sync_artifacts
                    WHERE datetime(created_at) >= datetime(?) AND datetime(created_at) < datetime(?)
                    ORDER BY datetime(created_at)
                    """,
                    (start.isoformat(), end.isoformat()),
                )
            ]
        except sqlite3.Error:
            artifact_rows = []
    finally:
        conn.close()

    admin_rows = _read_optional_rows(
        [CONTROL_PLANE_DB_PATH, DB_PATH],
        "admin_ops_users",
        """
        SELECT account_id, display_name, email, organization_id, status
        FROM admin_ops_users
        ORDER BY lower(COALESCE(email, account_id)), account_id
        """,
    )
    gateway_request_rows = _read_optional_rows(
        [GATEWAY_DB_PATH, DB_PATH],
        "gateway_requests",
        """
        SELECT request_id, account_id, model_id, trace_id, status,
               terminal_event_type, created_at, updated_at
        FROM gateway_requests
        WHERE (
            datetime(created_at) >= datetime(?) AND datetime(created_at) < datetime(?)
        ) OR (
            datetime(updated_at) >= datetime(?) AND datetime(updated_at) < datetime(?)
        )
        ORDER BY datetime(created_at), request_id
        """,
        (start.isoformat(), end.isoformat(), start.isoformat(), end.isoformat()),
    )
    gateway_event_rows = _read_optional_rows(
        [GATEWAY_DB_PATH, DB_PATH],
        "gateway_events",
        """
        SELECT request_id, seq, payload_json, created_at
        FROM gateway_events
        WHERE datetime(created_at) >= datetime(?) AND datetime(created_at) < datetime(?)
        ORDER BY datetime(created_at), request_id, seq
        """,
        (start.isoformat(), end.isoformat()),
    )

    observed_emails = {
        canonical_email(event.get("user_email") or event.get("user_key"))
        for event in rows
    }
    observed_emails.update(
        canonical_email(usage.get("user_email"))
        for usage in legacy_usage_rows
    )
    observed_accounts = {
        canonical_email(request.get("account_id"))
        for request in gateway_request_rows
    }
    labels_by_email, identity_order, account_aliases = merged_identity_catalog(
        user_rows,
        admin_rows,
        observed_emails,
        observed_accounts,
    )
    users_list = [labels_by_email[identity] for identity in identity_order]

    gateway_requests_by_id = {
        str(row.get("request_id") or "").strip(): row
        for row in gateway_request_rows
        if str(row.get("request_id") or "").strip()
    }
    gateway_request_ids = set(gateway_requests_by_id)
    merged_usage: dict[tuple[str, str], dict] = {}
    anonymous_usage_sequence = 0
    for row in legacy_usage_rows:
        identity = canonical_email(row.get("user_email"))
        request_id = usage_request_id(row, gateway_request_ids)
        anonymous_usage_sequence += 1
        key = (
            "request",
            request_id,
        ) if request_id else (
            "legacy",
            str(row.get("id") or anonymous_usage_sequence),
        )
        normalized = dict(row)
        normalized["_identity"] = identity
        normalized["_request_id"] = request_id
        merged_usage[key] = normalized

    # A Gateway request id is the cross-ledger idempotency identity. When a
    # legacy usage row and a v1 completion describe the same request, the
    # immutable Gateway completion is authoritative and replaces the old copy.
    gateway_completion_by_request: dict[str, dict] = {}
    for event in gateway_event_rows:
        payload = json_loads(event.get("payload_json"), {})
        if (
            not isinstance(payload, dict)
            or payload.get("event_type")
            not in {"response.completed", "tool_call.requested"}
        ):
            continue
        request_id = str(event.get("request_id") or "").strip()
        request = gateway_requests_by_id.get(request_id)
        if not request:
            continue
        usage = payload.get("usage")
        if not isinstance(usage, dict):
            continue
        account_id = canonical_email(request.get("account_id"))
        identity = account_aliases.get(account_id, account_id)
        input_tokens = token_number(usage.get("input_tokens") or usage.get("prompt_tokens"))
        output_tokens = token_number(
            usage.get("output_tokens") or usage.get("completion_tokens")
        )
        total_tokens = token_number(usage.get("total_tokens"))
        gateway_completion_by_request[request_id] = {
            "id": f"gateway:{request_id}",
            "category": "chat",
            "label": f"gateway.{payload['event_type']}",
            "user_email": identity,
            "detail": json.dumps(
                {
                    "usageSource": "gateway",
                    "requestId": request_id,
                },
                ensure_ascii=False,
            ),
            "created_at": event.get("created_at") or request.get("updated_at"),
            "device_id": "",
            "session_id": request.get("trace_id") or "",
            "model": request.get("model_id") or "",
            "provider": "managed_gateway",
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": max(total_tokens, input_tokens + output_tokens),
            "_identity": identity,
            "_request_id": request_id,
        }
    for request_id, row in gateway_completion_by_request.items():
        merged_usage[("request", request_id)] = row
    usage_rows = sorted(
        merged_usage.values(),
        key=lambda row: (
            parse_time(str(row.get("created_at") or start.isoformat())),
            str(row.get("id") or ""),
        ),
    )

    raw_events = []
    for index, event in enumerate(rows, 1):
        email = canonical_email(event.get("user_email") or event.get("user_key"))
        user = labels_by_email.get(email, "未识别用户")
        raw_event = event.get("event_type") or "unknown"
        raw_status = event.get("status") or "unknown"
        created = parse_time(event.get("created_at"))
        raw_events.append(
            {
                "seq": index,
                "user": user,
                "email": email,
                "date": created.strftime("%Y-%m-%d"),
                "time": created.strftime("%H:%M:%S"),
                "eventType": EVENT_TYPE_ZH.get(raw_event, raw_event),
                "resultClass": result_class(raw_status, raw_event),
                "status": STATUS_ZH.get(raw_status, raw_status),
                "source": "网页端" if event.get("source") == "WebUI" else (event.get("source") or ""),
                "requestId": event.get("request_id") or "",
                "sessionId": event.get("session_id") or "",
                "device": (event.get("device_id") or "")[:16],
                "detail": detail_summary(event.get("detail")),
                # Token facts come only from usage_events below.  Event prose is
                # diagnostic data and is never a billing/usage source.
                "hasUsage": False,
                "inputTokens": 0,
                "outputTokens": 0,
                "totalTokens": 0,
                "rawEventType": raw_event,
                "rawStatus": raw_status,
            }
        )

    effective_artifact_counts = Counter()
    invalid_artifact_counts = Counter()
    for artifact in artifact_rows:
        metadata = json_loads(artifact.get("metadata"), {})
        if not isinstance(metadata, dict):
            continue
        email = canonical_email(artifact.get("user_email") or artifact.get("user_key"))
        created = parse_time(artifact.get("created_at"))
        key = (email, created.strftime("%Y-%m-%d"))
        if metadata_validity(metadata) == "invalid":
            invalid_artifact_counts[key] += 1
        elif is_effective_artifact(artifact.get("status"), metadata):
            effective_artifact_counts[key] += 1

    requests = {}
    for event in rows:
        request_id = event.get("request_id") or ""
        if not request_id:
            continue
        email = canonical_email(event.get("user_email") or event.get("user_key"))
        created = parse_time(event.get("created_at"))
        rec = requests.setdefault(
            request_id,
            {
                "email": email,
                "user": labels_by_email.get(email, "未识别用户"),
                "requestId": request_id,
                "sessionId": event.get("session_id") or "",
                "acceptedAt": None,
                "completedAt": None,
                "firstAt": created,
                "tools": Counter(),
                "scenarios": Counter(),
                "artifactEvents": 0,
                "problemEvents": 0,
                "cancelled": False,
                "failed": False,
                "hasUsage": False,
                "noUsageFlag": False,
                "inputTokens": 0,
                "outputTokens": 0,
                "totalTokens": 0,
            },
        )
        rec["firstAt"] = min(rec["firstAt"], created)
        if not rec["sessionId"] and event.get("session_id"):
            rec["sessionId"] = event["session_id"]
        event_type = event.get("event_type") or ""
        status = event.get("status") or ""
        if event_type == "run.accepted":
            rec["acceptedAt"] = min([item for item in [rec["acceptedAt"], created] if item], default=created)
        if event_type == "run.completed":
            rec["completedAt"] = max([item for item in [rec["completedAt"], created] if item], default=created)
        if event_type == "run.cancelled" or status == "cancelled":
            rec["cancelled"] = True
        if event_type == "run.failed" or status == "failed":
            rec["failed"] = True
        if event_type == "artifact.updated":
            rec["artifactEvents"] += 1
        if status in {"failed", "cancelled", "limited"} or event_type in {
            "run.cancelled",
            "run.failed",
            "artifact.limit",
            "tool.failed",
        }:
            rec["problemEvents"] += 1
        detail = json_loads(event.get("detail"), {})
        tool = detail.get("tool") if isinstance(detail, dict) else None
        if tool:
            rec["tools"][TOOL_ZH.get(str(tool), str(tool))] += 1
            rec["scenarios"][scenario_from_tool(str(tool), detail)] += 1

    for request in gateway_request_rows:
        request_id = str(request.get("request_id") or "").strip()
        if not request_id:
            continue
        account_id = canonical_email(request.get("account_id"))
        identity = account_aliases.get(account_id, account_id)
        accepted_at = parse_time(request.get("created_at"))
        updated_at = parse_time(request.get("updated_at"))
        terminal = str(request.get("terminal_event_type") or "").strip()
        rec = requests.setdefault(
            request_id,
            {
                "email": identity,
                "user": labels_by_email.get(identity, "未识别用户"),
                "requestId": request_id,
                "sessionId": request.get("trace_id") or "",
                "acceptedAt": accepted_at,
                "completedAt": None,
                "firstAt": accepted_at,
                "tools": Counter(),
                "scenarios": Counter(),
                "artifactEvents": 0,
                "problemEvents": 0,
                "cancelled": False,
                "failed": False,
                "hasUsage": False,
                "noUsageFlag": False,
                "inputTokens": 0,
                "outputTokens": 0,
                "totalTokens": 0,
            },
        )
        # The v1 account directory is authoritative for Gateway facts. This
        # also makes a matching legacy sync request converge on one identity.
        if identity:
            rec["email"] = identity
            rec["user"] = labels_by_email.get(identity, rec["user"])
        rec["acceptedAt"] = min(
            [item for item in (rec.get("acceptedAt"), accepted_at) if item],
            default=accepted_at,
        )
        rec["firstAt"] = min(rec.get("firstAt") or accepted_at, accepted_at)
        if not rec.get("sessionId") and request.get("trace_id"):
            rec["sessionId"] = request["trace_id"]
        if str(request.get("status") or "") == "completed":
            if terminal == "response.failed":
                rec["failed"] = True
                rec["problemEvents"] += 1
            else:
                rec["completedAt"] = max(
                    [item for item in (rec.get("completedAt"), updated_at) if item],
                    default=updated_at,
                )

    usage_by_request: dict[str, dict] = {}
    for row in usage_rows:
        request_id = str(row.get("_request_id") or "").strip()
        if request_id:
            usage_by_request[request_id] = usage_row_projection(row)

    tasks = []
    for rec in requests.values():
        if not rec["acceptedAt"]:
            continue
        status_category = task_status_category(rec)
        success = status_category == "成功"
        duration = None
        if success:
            duration = max(0, (rec["completedAt"] - rec["acceptedAt"]).total_seconds() / 60)
        scenario = rec["scenarios"].most_common(1)[0][0] if rec["scenarios"] else "创作内容"
        task_usage = usage_by_request.get(rec["requestId"])
        tasks.append(
            {
                "user": rec["user"],
                "email": rec["email"],
                "date": rec["acceptedAt"].strftime("%Y-%m-%d"),
                "time": rec["acceptedAt"].strftime("%H:%M:%S"),
                "requestId": rec["requestId"],
                "sessionId": rec["sessionId"],
                "success": success,
                "statusCategory": status_category,
                "durationMinutes": round(duration, 2) if duration is not None else None,
                "needsIntervention": rec["problemEvents"] > 0 or not success,
                "problemEvents": rec["problemEvents"],
                "artifactEvents": rec["artifactEvents"],
                "hasUsage": task_usage is not None,
                "noUsageFlag": task_usage is None,
                "inputTokens": int((task_usage or {}).get("inputTokens", 0)),
                "outputTokens": int((task_usage or {}).get("outputTokens", 0)),
                "totalTokens": int((task_usage or {}).get("totalTokens", 0)),
                "scenario": scenario,
                "mainTools": "、".join(f"{name} {count}" for name, count in rec["tools"].most_common(3))
                or "无工具调用记录",
            }
        )

    date_count = max(1, (end.date() - start.date()).days)
    dates = [(start + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(date_count)]
    usage_by_user_date: dict[tuple[str, str], dict] = {}
    for row in usage_rows:
        email = canonical_email(row.get("_identity") or row.get("user_email"))
        created = parse_time(row.get("created_at"))
        key = (email, created.strftime("%Y-%m-%d"))
        projection = usage_row_projection(row)
        bucket = usage_by_user_date.setdefault(
            key,
            {
                "records": 0,
                "inputTokens": 0,
                "outputTokens": 0,
                "totalTokens": 0,
                "cacheReadTokens": 0,
                "cacheWriteTokens": 0,
                "cacheReportedRecords": 0,
                "estimatedRecords": 0,
                "models": Counter(),
                "sources": Counter(),
                "requestIds": set(),
                "unlinkedRecords": 0,
            },
        )
        bucket["records"] += 1
        request_id = str(row.get("_request_id") or "").strip()
        if request_id:
            bucket["requestIds"].add(request_id)
        else:
            bucket["unlinkedRecords"] += 1
        for field in (
            "inputTokens",
            "outputTokens",
            "totalTokens",
            "cacheReadTokens",
            "cacheWriteTokens",
        ):
            bucket[field] += projection[field]
        bucket["estimatedRecords"] += int(projection["estimated"])
        bucket["cacheReportedRecords"] += int(projection["cacheReadTokens"] > 0)
        model = str(row.get("model") or "").strip()
        provider = str(row.get("provider") or "").strip()
        if model:
            bucket["models"][model] += 1
        bucket["sources"][projection["usageSource"] or provider or "provider"] += 1

    scenario_set = {task["scenario"] for task in tasks}
    scenarios = [name for name in SCENARIO_ORDER if name in scenario_set]
    scenarios.extend(sorted(scenario_set - set(scenarios)))
    summary_rows = []
    for email in identity_order:
        user = labels_by_email[email]
        for date in dates:
            slice_tasks = [
                task
                for task in tasks
                if task["email"] == email and task["date"] == date
            ]
            total = len(slice_tasks)
            successes = sum(1 for task in slice_tasks if task["success"])
            stopped = sum(1 for task in slice_tasks if task.get("statusCategory") == "中止")
            failed = max(0, total - successes - stopped)
            interventions = sum(1 for task in slice_tasks if task["needsIntervention"])
            durations = [task["durationMinutes"] for task in slice_tasks if task["durationMinutes"] is not None]
            scene_counts = Counter(task["scenario"] for task in slice_tasks)
            usage = usage_by_user_date.get((email, date), {})
            input_tokens = int(usage.get("inputTokens", 0))
            output_tokens = int(usage.get("outputTokens", 0))
            total_tokens = int(usage.get("totalTokens", 0))
            usage_records = int(usage.get("records", 0))
            usage_tasks = (
                len(usage.get("requestIds", set()))
                + int(usage.get("unlinkedRecords", 0))
            )
            remarks = []
            if date in {"2026-06-22", "2026-06-23"} and total == 0:
                remarks.append("服务器未收到详细事件上报")
            elif total == 0:
                remarks.append("当天无任务记录")
            if interventions:
                remarks.append(f"{interventions} 个任务需复查")
            if total and not interventions:
                remarks.append("当天任务均未触发失败/取消/受限事件")
            summary_rows.append(
                {
                    "id": f"{user}|{date}",
                    "user": user,
                    "email": email,
                    "date": date,
                    "totalTasks": total,
                    "successTasks": successes,
                    "failedTasks": failed,
                    "stoppedTasks": stopped,
                    "successRate": round(successes / total * 100, 1) if total else 0,
                    "avgCompletionMinutes": round(sum(durations) / len(durations), 2) if durations else None,
                    "interventionCount": interventions,
                    "interventionRate": round(interventions / total * 100, 1) if total else 0,
                    "mainScenario": "、".join(f"{name} {value}" for name, value in scene_counts.most_common(3))
                    if scene_counts
                    else "无",
                    "autoArtifactEvents": sum(task["artifactEvents"] for task in slice_tasks),
                    "effectiveArtifacts": int(effective_artifact_counts.get(((email or "").lower(), date), 0)),
                    "invalidArtifacts": int(invalid_artifact_counts.get(((email or "").lower(), date), 0)),
                    "inputTokens": input_tokens,
                    "outputTokens": output_tokens,
                    "totalTokens": total_tokens or input_tokens + output_tokens,
                    "tokenUsageRecords": usage_records,
                    "tokenUsageTasks": usage_tasks,
                    "missingTokenTasks": max(0, total - usage_tasks),
                    "cacheReadTokens": int(usage.get("cacheReadTokens", 0)),
                    "cacheWriteTokens": int(usage.get("cacheWriteTokens", 0)),
                    "cacheInputTokens": input_tokens,
                    "cacheReportedRecords": int(usage.get("cacheReportedRecords", 0)),
                    "tokenEstimatedRecords": int(usage.get("estimatedRecords", 0)),
                    "tokenModels": "、".join(name for name, _ in usage.get("models", Counter()).most_common(3)),
                    "tokenSources": "、".join(name for name, _ in usage.get("sources", Counter()).most_common(3)),
                    "remarks": "；".join(remarks),
                }
            )

    total_tasks = len(tasks)
    success_tasks = sum(1 for task in tasks if task["success"])
    stopped_tasks = sum(1 for task in tasks if task.get("statusCategory") == "中止")
    failed_tasks = max(0, total_tasks - success_tasks - stopped_tasks)
    interventions = sum(1 for task in tasks if task["needsIntervention"])
    durations = [task["durationMinutes"] for task in tasks if task["durationMinutes"] is not None]
    input_tokens = sum(usage_row_projection(row)["inputTokens"] for row in usage_rows)
    output_tokens = sum(usage_row_projection(row)["outputTokens"] for row in usage_rows)
    total_tokens = sum(usage_row_projection(row)["totalTokens"] for row in usage_rows)
    usage_sessions = {
        str(row.get("session_id") or "").strip()
        for row in usage_rows
        if str(row.get("session_id") or "").strip()
    }
    usage_request_ids = {
        str(row.get("_request_id") or "").strip()
        for row in usage_rows
        if str(row.get("_request_id") or "").strip()
    }
    token_usage_tasks = sum(
        1
        for task in tasks
        if (
            task.get("requestId") in usage_request_ids
            or task.get("sessionId") in usage_sessions
        )
    )
    scenario_counts = Counter(task["scenario"] for task in tasks)
    daily_counts = []
    for date in dates:
        slice_tasks = [task for task in tasks if task["date"] == date]
        daily_counts.append(
            {
                "date": date,
                "total": len(slice_tasks),
                "success": sum(1 for task in slice_tasks if task["success"]),
                "intervention": sum(1 for task in slice_tasks if task["needsIntervention"]),
            }
        )
    user_counts = []
    for user in users_list:
        slice_tasks = [task for task in tasks if task["user"] == user]
        user_counts.append(
            {
                "user": user,
                "total": len(slice_tasks),
                "success": sum(1 for task in slice_tasks if task["success"]),
                "intervention": sum(1 for task in slice_tasks if task["needsIntervention"]),
            }
        )
    user_counts.sort(key=lambda item: (-item["total"], item["user"]))
    busiest = max(daily_counts, key=lambda item: item["total"], default={"date": "", "total": 0})
    insights = [
        f"当前范围服务器 RAW 共 {len(raw_events)} 条事件，按 request_id 去重后为 {total_tasks} 个任务。",
        f"任务集中在 {busiest['date']}，当天有 {busiest['total']} 个任务。",
        f"成功任务 {success_tasks} 个，失败 {failed_tasks} 个，中止 {stopped_tasks} 个；按任务口径成功率为 {round(success_tasks / total_tasks * 100, 1) if total_tasks else 0}%。",
        "6 月 22 日和 6 月 23 日服务器未收到详细事件上报，图表里保留为 0，避免补造数据。",
        "人工干预次数为根据失败、取消、受限事件推算的需复查任务数，RAW 中没有单独的人工点击字段。",
    ]
    return {
        "meta": {
            "title": "EcoreX 上周 Agent 使用情况分析面板",
            "range": f"{start.strftime('%Y-%m-%d')} 至 {(end - timedelta(days=1)).strftime('%Y-%m-%d')}",
            "startDate": start.strftime("%Y-%m-%d"),
            "endDate": (end - timedelta(days=1)).strftime("%Y-%m-%d"),
            "generatedAt": datetime.now(TZ).isoformat(timespec="seconds"),
            "rawSheetUrl": "https://my.feishu.cn/sheets/KGias0a8OhQvrNtX9lict6Jznkg",
            "source": "服务器 sync_events RAW 实时查询",
            "version": VERSION,
            "live": True,
        },
        "kpis": {
            "rawEvents": len(raw_events),
            "tasks": total_tasks,
            "successTasks": success_tasks,
            "failedTasks": failed_tasks,
            "stoppedTasks": stopped_tasks,
            "successRate": round(success_tasks / total_tasks * 100, 1) if total_tasks else 0,
            "avgCompletionMinutes": round(sum(durations) / len(durations), 2) if durations else 0,
            "interventions": interventions,
            "interventionRate": round(interventions / total_tasks * 100, 1) if total_tasks else 0,
            "users": len(users_list),
            "inputTokens": input_tokens,
            "outputTokens": output_tokens,
            "totalTokens": total_tokens or input_tokens + output_tokens,
            "tokenUsageTasks": token_usage_tasks,
            "missingTokenTasks": max(0, total_tasks - token_usage_tasks),
            "tokenUsageRate": round(token_usage_tasks / total_tasks * 100, 1) if total_tasks else 0,
            "effectiveArtifacts": sum(effective_artifact_counts.values()),
            "invalidArtifacts": sum(invalid_artifact_counts.values()),
        },
        "users": users_list,
        "dates": dates,
        "scenarios": scenarios,
        "summaryRows": summary_rows,
        "tasks": tasks,
        "rawEvents": raw_events,
        "charts": {
            "daily": daily_counts,
            "users": user_counts,
            "scenarios": [{"name": name, "value": value} for name, value in scenario_counts.most_common()],
        },
        "insights": insights,
    }


def _account_identity(account_id: str) -> str:
    normalized = canonical_email(account_id)
    if not normalized:
        raise ValueError("usage account identity is invalid")
    rows = _read_optional_rows(
        [CONTROL_PLANE_DB_PATH, DB_PATH],
        "admin_ops_users",
        """
        SELECT account_id, email
        FROM admin_ops_users
        ORDER BY account_id
        """,
    )
    for row in rows:
        if canonical_email(row.get("account_id")) == normalized:
            return canonical_email(row.get("email")) or normalized
    raise KeyError("usage account does not exist")


def _coverage_started_at() -> datetime | None:
    candidates: list[datetime] = []
    for rows in (
        _read_optional_rows(
            [DB_PATH],
            "usage_events",
            "SELECT MIN(created_at) AS created_at FROM usage_events",
        ),
        _read_optional_rows(
            [DB_PATH],
            "sync_events",
            "SELECT MIN(created_at) AS created_at FROM sync_events",
        ),
        _read_optional_rows(
            [GATEWAY_DB_PATH, DB_PATH],
            "gateway_requests",
            "SELECT MIN(created_at) AS created_at FROM gateway_requests",
        ),
    ):
        if not rows or not rows[0].get("created_at"):
            continue
        try:
            candidates.append(parse_time(str(rows[0]["created_at"])))
        except (TypeError, ValueError):
            continue
    if not candidates:
        return None
    return min(candidates).astimezone(timezone.utc)


def build_account_usage_projection(
    account_id: str,
    *,
    timezone_name: str = "Asia/Shanghai",
    now: datetime | None = None,
) -> dict:
    """Project the Composer and panel from the exact same merged usage ledger."""

    if not isinstance(timezone_name, str) or not timezone_name.strip():
        raise ValueError("usage timezone is required")
    try:
        zone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        raise ValueError("usage timezone is invalid") from None
    # The operator panel's calendar contract is currently Shanghai time. Fail
    # closed instead of returning a plausible-looking projection whose daily
    # rows were grouped in another zone.
    if getattr(zone, "key", timezone_name) != "Asia/Shanghai":
        raise ValueError("usage timezone is not supported")
    calculated_at = now or datetime.now(timezone.utc)
    if calculated_at.tzinfo is None:
        raise ValueError("usage clock must be timezone-aware")
    calculated_at = calculated_at.astimezone(timezone.utc)
    local_now = calculated_at.astimezone(zone)
    day_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_start = day_start - timedelta(days=day_start.weekday())
    end = day_start + timedelta(days=1)
    identity = _account_identity(account_id)
    validate_data_request(week_start, end)
    payload = build_payload(week_start, end)
    rows = [
        row
        for row in payload.get("summaryRows", [])
        if canonical_email(row.get("email")) == identity
    ]
    today_label = day_start.strftime("%Y-%m-%d")

    def totals(selected: list[dict]) -> dict[str, int]:
        input_tokens = sum(max(0, token_number(row.get("inputTokens"))) for row in selected)
        output_tokens = sum(max(0, token_number(row.get("outputTokens"))) for row in selected)
        total_tokens = sum(max(0, token_number(row.get("totalTokens"))) for row in selected)
        return {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": max(total_tokens, input_tokens + output_tokens),
        }

    return {
        "schema_version": 1,
        "scope": "account",
        "timezone": timezone_name,
        "today": totals([row for row in rows if row.get("date") == today_label]),
        "week": totals(rows),
        "week_started_at": week_start.astimezone(timezone.utc).isoformat(),
        "coverage_started_at": (
            value.isoformat() if (value := _coverage_started_at()) is not None else None
        ),
        "calculated_at": calculated_at.isoformat(),
    }


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        if parsed.path in {"/api/health", "/health"}:
            self.send_json(200, {"ok": True, "version": VERSION, "service": "ecorex-usage-panel-api"})
            return
        if parsed.path in {"/api/runtime-audit", "/runtime-audit"}:
            try:
                self.send_json(200, build_runtime_audit(query))
            except Exception as exc:
                self.send_json(500, {"ok": False, "version": VERSION, "error": str(exc)[:200]})
            return
        if parsed.path in {"/api/state", "/state"}:
            try:
                audit = build_runtime_audit(query)
                self.send_json(200, {"ok": True, "version": VERSION, "summary": audit.get("summary")})
            except Exception as exc:
                self.send_json(500, {"ok": False, "version": VERSION, "error": str(exc)[:200]})
            return
        if parsed.path not in {"/api/data", "/data"}:
            self.send_json(404, {"ok": False, "error": "not found"})
            return
        try:
            self.send_json(200, build_data_request_payload(query))
        except UsagePanelRequestError as exc:
            self.send_json(exc.status, exc.payload())
        except Exception as exc:
            self.send_json(500, {"ok": False, "error": str(exc)[:200]})

    def log_message(self, fmt, *args):
        return

    def send_json(self, status: int, payload: dict):
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main():
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"EcoreX usage panel API listening on {HOST}:{PORT}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
