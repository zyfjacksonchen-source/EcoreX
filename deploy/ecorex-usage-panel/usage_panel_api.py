#!/usr/bin/env python3
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

VERSION = "0.2.9.1"
DB_PATH = "/srv/ecorex-agent-admin/data/ecorex-admin.sqlite3"
HOST = "127.0.0.1"
PORT = 18105
TZ = timezone(timedelta(hours=8))
ADMIN_API_DIRS = [
    os.environ.get("ECOREX_ADMIN_API_DIR", ""),
    "/srv/ecorex-agent-admin/app",
    "/srv/ecorex-agent-download/current/admin",
    str(pathlib.Path(__file__).resolve().parent.parent / "ecorex-admin-api"),
]

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
    if rec.get("completedAt"):
        return "成功"
    if rec.get("cancelled"):
        return "中止"
    return "失败"


def build_payload(start: datetime, end: datetime) -> dict:
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        users = {
            (row["email"] or "").lower(): row["name"]
            for row in conn.execute("SELECT name, email FROM users WHERE email IS NOT NULL")
        }
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

    raw_events = []
    for index, event in enumerate(rows, 1):
        email = (event.get("user_email") or event.get("user_key") or "unknown").strip()
        user = users.get(email.lower()) or email.split("@")[0]
        raw_event = event.get("event_type") or "unknown"
        raw_status = event.get("status") or "unknown"
        created = parse_time(event.get("created_at"))
        token_usage = token_usage_from_detail(event.get("detail"))
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
                "hasUsage": token_usage["hasUsage"],
                "inputTokens": token_usage["inputTokens"],
                "outputTokens": token_usage["outputTokens"],
                "totalTokens": token_usage["totalTokens"],
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
        email = (artifact.get("user_email") or artifact.get("user_key") or "unknown").strip().lower()
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
        email = (event.get("user_email") or event.get("user_key") or "unknown").strip()
        key = (email, request_id)
        created = parse_time(event.get("created_at"))
        rec = requests.setdefault(
            key,
            {
                "email": email,
                "user": users.get(email.lower()) or email.split("@")[0],
                "requestId": request_id,
                "acceptedAt": None,
                "completedAt": None,
                "firstAt": created,
                "tools": Counter(),
                "scenarios": Counter(),
                "artifactEvents": 0,
                "problemEvents": 0,
                "cancelled": False,
                "hasUsage": False,
                "noUsageFlag": False,
                "inputTokens": 0,
                "outputTokens": 0,
                "totalTokens": 0,
            },
        )
        rec["firstAt"] = min(rec["firstAt"], created)
        event_type = event.get("event_type") or ""
        status = event.get("status") or ""
        if event_type == "run.accepted":
            rec["acceptedAt"] = min([item for item in [rec["acceptedAt"], created] if item], default=created)
        if event_type == "run.completed":
            rec["completedAt"] = max([item for item in [rec["completedAt"], created] if item], default=created)
        if event_type == "run.cancelled" or status == "cancelled":
            rec["cancelled"] = True
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
        token_usage = token_usage_from_detail(event.get("detail"))
        rec["inputTokens"] += token_usage["inputTokens"]
        rec["outputTokens"] += token_usage["outputTokens"]
        rec["totalTokens"] += token_usage["totalTokens"]
        rec["hasUsage"] = rec["hasUsage"] or bool(token_usage["hasUsage"])
        rec["noUsageFlag"] = rec["noUsageFlag"] or token_usage["hasUsage"] is False
        tool = detail.get("tool") if isinstance(detail, dict) else None
        if tool:
            rec["tools"][TOOL_ZH.get(str(tool), str(tool))] += 1
            rec["scenarios"][scenario_from_tool(str(tool), detail)] += 1

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
        tasks.append(
            {
                "user": rec["user"],
                "email": rec["email"],
                "date": rec["acceptedAt"].strftime("%Y-%m-%d"),
                "time": rec["acceptedAt"].strftime("%H:%M:%S"),
                "requestId": rec["requestId"],
                "success": success,
                "statusCategory": status_category,
                "durationMinutes": round(duration, 2) if duration is not None else None,
                "needsIntervention": rec["problemEvents"] > 0 or not success,
                "problemEvents": rec["problemEvents"],
                "artifactEvents": rec["artifactEvents"],
                "hasUsage": rec["hasUsage"],
                "noUsageFlag": rec["noUsageFlag"],
                "inputTokens": rec["inputTokens"],
                "outputTokens": rec["outputTokens"],
                "totalTokens": rec["totalTokens"] or rec["inputTokens"] + rec["outputTokens"],
                "scenario": scenario,
                "mainTools": "、".join(f"{name} {count}" for name, count in rec["tools"].most_common(3))
                or "无工具调用记录",
            }
        )

    date_count = max(1, (end.date() - start.date()).days)
    dates = [(start + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(date_count)]
    users_list = sorted({row["user"] for row in raw_events})
    scenario_set = {task["scenario"] for task in tasks}
    scenarios = [name for name in SCENARIO_ORDER if name in scenario_set]
    scenarios.extend(sorted(scenario_set - set(scenarios)))
    summary_rows = []
    for user in users_list:
        email = next((task["email"] for task in tasks if task["user"] == user), "")
        if not email:
            email = next((event["email"] for event in raw_events if event["user"] == user), "")
        for date in dates:
            slice_tasks = [task for task in tasks if task["user"] == user and task["date"] == date]
            total = len(slice_tasks)
            successes = sum(1 for task in slice_tasks if task["success"])
            stopped = sum(1 for task in slice_tasks if task.get("statusCategory") == "中止")
            failed = max(0, total - successes - stopped)
            interventions = sum(1 for task in slice_tasks if task["needsIntervention"])
            durations = [task["durationMinutes"] for task in slice_tasks if task["durationMinutes"] is not None]
            scene_counts = Counter(task["scenario"] for task in slice_tasks)
            input_tokens = sum(task.get("inputTokens", 0) for task in slice_tasks)
            output_tokens = sum(task.get("outputTokens", 0) for task in slice_tasks)
            total_tokens = sum(task.get("totalTokens", 0) for task in slice_tasks)
            usage_tasks = sum(1 for task in slice_tasks if task.get("totalTokens", 0) > 0)
            no_usage_tasks = total - usage_tasks
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
                    "tokenUsageTasks": usage_tasks,
                    "missingTokenTasks": no_usage_tasks,
                    "remarks": "；".join(remarks),
                }
            )

    total_tasks = len(tasks)
    success_tasks = sum(1 for task in tasks if task["success"])
    stopped_tasks = sum(1 for task in tasks if task.get("statusCategory") == "中止")
    failed_tasks = max(0, total_tasks - success_tasks - stopped_tasks)
    interventions = sum(1 for task in tasks if task["needsIntervention"])
    durations = [task["durationMinutes"] for task in tasks if task["durationMinutes"] is not None]
    input_tokens = sum(task.get("inputTokens", 0) for task in tasks)
    output_tokens = sum(task.get("outputTokens", 0) for task in tasks)
    total_tokens = sum(task.get("totalTokens", 0) for task in tasks)
    token_usage_tasks = sum(1 for task in tasks if task.get("totalTokens", 0) > 0)
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
            "missingTokenTasks": total_tasks - token_usage_tasks,
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
        default_start = datetime(2026, 6, 22, tzinfo=TZ)
        default_end = datetime(2026, 6, 29, tzinfo=TZ)
        start = parse_date(query.get("start", [""])[0], default_start)
        end = parse_date(query.get("end", [""])[0], default_end)
        if end <= start:
            self.send_json(400, {"ok": False, "error": "invalid range"})
            return
        try:
            self.send_json(200, build_payload(start, end))
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
