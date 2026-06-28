#!/usr/bin/env python3
"""Run a redacted real Feishu/Lark IM credential smoke.

The smoke performs read-only checks only:
1. `lark-cli auth status --verify`
2. `lark-cli im +chat-list --as user --page-size 1 --format json`

Raw user IDs, chat IDs, names, tokens, and chat metadata are never written to
the JSON artifact. Evidence keeps only status booleans, scope checks, counts,
and short hashes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ARTIFACT = ROOT / "docs" / "v0.2.2" / "artifacts" / "feishu-im-real-credential-smoke.json"
REQUIRED_IM_SCOPES = ("im:chat:read", "im:message")
SECRET_MARKERS = ("access_token", "refresh_token", "tenant_access_token", "user_access_token", "app_secret")


class FeishuSmokeError(RuntimeError):
    """Raised when the real Feishu/IM smoke cannot prove the contract."""


def _hash(value: Any) -> str:
    text = "" if value is None else str(value)
    if not text:
        return ""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _repo_relative(path: Path) -> str:
    try:
        return path.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return str(path.resolve())


def _lark_command() -> list[str]:
    command = shutil.which("lark-cli.cmd") or shutil.which("lark-cli.exe") or shutil.which("lark-cli")
    if not command:
        raise FeishuSmokeError("lark-cli is not available on PATH")
    path = Path(command)
    if path.suffix.lower() == ".ps1":
        pwsh = shutil.which("pwsh") or shutil.which("powershell")
        if not pwsh:
            raise FeishuSmokeError("lark-cli resolved to a PowerShell script but pwsh/powershell is unavailable")
        return [pwsh, "-NoLogo", "-NoProfile", "-File", str(path)]
    return [str(path)]


def _run_lark(args: list[str], *, timeout: int) -> dict[str, Any]:
    cmd = _lark_command() + args
    env = os.environ.copy()
    env.setdefault("PYTHONIOENCODING", "utf-8")
    proc = subprocess.run(
        cmd,
        cwd=str(ROOT),
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=timeout,
        env=env,
        check=False,
    )
    if proc.returncode != 0:
        raise FeishuSmokeError(
            f"lark-cli {' '.join(args)} failed: "
            f"exit={proc.returncode} stdoutHash={_hash(proc.stdout)} stderrHash={_hash(proc.stderr)}"
        )
    stdout = (proc.stdout or "").strip()
    if not stdout:
        raise FeishuSmokeError(f"lark-cli {' '.join(args)} returned empty output")
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise FeishuSmokeError(
            f"lark-cli {' '.join(args)} did not return JSON: "
            f"stdoutHash={_hash(stdout)} parseError={exc.msg}"
        ) from exc
    return payload


def _run_lark_text(args: list[str], *, timeout: int) -> str:
    cmd = _lark_command() + args
    proc = subprocess.run(
        cmd,
        cwd=str(ROOT),
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    if proc.returncode != 0:
        raise FeishuSmokeError(
            f"lark-cli {' '.join(args)} failed: "
            f"exit={proc.returncode} stdoutHash={_hash(proc.stdout)} stderrHash={_hash(proc.stderr)}"
        )
    return (proc.stdout or "").strip()


def _scope_set(value: Any) -> set[str]:
    if isinstance(value, str):
        return {item.strip() for item in value.split() if item.strip()}
    if isinstance(value, list):
        return {str(item).strip() for item in value if str(item).strip()}
    return set()


def _assert_no_secret_keys(value: Any, path: str = "$") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            key_text = str(key).lower()
            if any(marker in key_text for marker in SECRET_MARKERS):
                raise FeishuSmokeError(f"redacted artifact contains forbidden secret key at {path}.{key}")
            _assert_no_secret_keys(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _assert_no_secret_keys(child, f"{path}[{index}]")
    elif isinstance(value, str):
        lowered = value.lower()
        if "ou_" in lowered or "oc_" in lowered or "om_" in lowered:
            raise FeishuSmokeError(f"redacted artifact contains raw Feishu id at {path}")
        if "bearer " in lowered or "token" in lowered or "secret" in lowered:
            raise FeishuSmokeError(f"redacted artifact contains token-shaped text at {path}")


def _auth_summary(payload: dict[str, Any]) -> dict[str, Any]:
    identities = payload.get("identities") if isinstance(payload.get("identities"), dict) else {}
    user = identities.get("user") if isinstance(identities.get("user"), dict) else {}
    bot = identities.get("bot") if isinstance(identities.get("bot"), dict) else {}
    scopes = _scope_set(payload.get("scope") or user.get("scope"))
    missing_scopes = sorted(scope for scope in REQUIRED_IM_SCOPES if scope not in scopes)
    if missing_scopes:
        raise FeishuSmokeError(f"missing required IM scopes: {', '.join(missing_scopes)}")
    if user.get("available") is not True:
        raise FeishuSmokeError("user identity is not available")
    return {
        "brand": str(payload.get("brand") or ""),
        "defaultIdentity": str(payload.get("identity") or ""),
        "appIdHash": _hash(payload.get("appId")),
        "user": {
            "available": bool(user.get("available")),
            "status": str(user.get("status") or payload.get("tokenStatus") or ""),
            "tokenStatus": str(user.get("tokenStatus") or payload.get("tokenStatus") or ""),
            "openIdHash": _hash(user.get("openId") or payload.get("userOpenId")),
        },
        "bot": {
            "available": bool(bot.get("available")),
            "status": str(bot.get("status") or ""),
        },
        "scopeChecks": {scope: scope in scopes for scope in REQUIRED_IM_SCOPES},
        "scopeCount": len(scopes),
    }


def _extract_items(payload: Any) -> tuple[list[Any], dict[str, Any]]:
    if isinstance(payload, list):
        return payload, {}
    if not isinstance(payload, dict):
        raise FeishuSmokeError("chat-list response root must be object or list")
    data = payload.get("data")
    if isinstance(data, dict):
        for key in ("items", "chats", "list"):
            if isinstance(data.get(key), list):
                return data[key], data
    for key in ("items", "chats", "list"):
        if isinstance(payload.get(key), list):
            return payload[key], payload
    return [], data if isinstance(data, dict) else payload


def _chat_summary(payload: Any) -> dict[str, Any]:
    items, container = _extract_items(payload)
    first = items[0] if items and isinstance(items[0], dict) else {}
    first_id = first.get("chat_id") or first.get("chatId") or first.get("id") or ""
    response_shape = []
    if isinstance(container, dict):
        response_shape = [
            str(key)
            for key in sorted(container.keys())
            if "token" not in str(key).lower() and "secret" not in str(key).lower()
        ][:20]
    return {
        "command": "lark-cli im +chat-list --as user --page-size 1 --format json",
        "identity": "user",
        "readOnly": True,
        "itemCount": len(items),
        "hasMore": bool(container.get("has_more") or container.get("hasMore")),
        "firstChatIdHash": _hash(first_id),
        "firstChatType": str(first.get("chat_type") or first.get("chatType") or first.get("chat_mode") or ""),
        "responseShape": response_shape,
    }


def run_smoke(args: argparse.Namespace) -> dict[str, Any]:
    version_text = _run_lark_text(["--version"], timeout=args.timeout)
    auth_payload = _run_lark(["auth", "status", "--verify"], timeout=args.timeout)
    chat_payload = _run_lark(
        ["im", "+chat-list", "--as", "user", "--page-size", "1", "--format", "json"],
        timeout=args.timeout,
    )
    auth = _auth_summary(auth_payload)
    im = _chat_summary(chat_payload)
    if im["itemCount"] < 0:
        raise FeishuSmokeError("invalid chat-list item count")

    result = {
        "status": "PASS",
        "scope": "real-feishu-im-readonly",
        "executedAt": datetime.now(timezone.utc).isoformat(),
        "requiresNetwork": True,
        "writesMessages": False,
        "writesFiles": False,
        "rawIdentifiersPersisted": False,
        "larkCli": {
            "versionOutputHash": _hash(version_text),
        },
        "auth": auth,
        "im": im,
        "redaction": {
            "rawUserIds": "hashed",
            "rawChatIds": "hashed",
            "rawNames": "omitted",
            "rawTokens": "omitted",
            "rawChatPayload": "not_persisted",
        },
    }
    _assert_no_secret_keys(result)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact", default=str(DEFAULT_ARTIFACT.relative_to(ROOT)))
    parser.add_argument("--timeout", type=int, default=45)
    args = parser.parse_args(argv)

    try:
        result = run_smoke(args)
    except Exception as exc:  # pragma: no cover
        print(json.dumps({"status": "FAIL", "error": str(exc)}, ensure_ascii=True, indent=2))
        return 1

    artifact = Path(args.artifact)
    if not artifact.is_absolute():
        artifact = ROOT / artifact
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text(json.dumps(result, ensure_ascii=True, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=True, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
