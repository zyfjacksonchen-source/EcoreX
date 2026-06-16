#!/usr/bin/env python3
"""Create a Feishu/Lark Bitable customer-review sheet for a Xiaohongshu note."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


DEFAULT_SCHEMA = Path(__file__).resolve().parents[1] / "assets" / "feishu" / "xhs_bitable_schema.json"


try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


def load_json(path: str | Path | None) -> dict[str, Any]:
    if not path:
        return {}
    with open(path, "r", encoding="utf-8-sig") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return data


def compact(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


def find_first(obj: Any, keys: set[str]) -> str:
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key in keys and isinstance(value, str):
                return value
        for value in obj.values():
            found = find_first(value, keys)
            if found:
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = find_first(item, keys)
            if found:
                return found
    return ""


def find_first_list_item(obj: Any, keys: set[str]) -> str:
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key in keys and isinstance(value, list) and value:
                first = value[0]
                if isinstance(first, str):
                    return first
            found = find_first_list_item(value, keys)
            if found:
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = find_first_list_item(item, keys)
            if found:
                return found
    return ""


def run_cmd(cmd: list[str], dry_run: bool, cwd: Path | None = None) -> dict[str, Any]:
    if dry_run:
        return {"ok": True, "dry_run": True, "cmd": cmd, "cwd": str(cwd) if cwd else "", "stdout": "", "stderr": "", "json": {}}
    proc = subprocess.run(cmd, text=True, capture_output=True, cwd=str(cwd) if cwd else None)
    parsed: Any = {}
    if proc.stdout.strip():
        try:
            parsed = json.loads(proc.stdout)
        except Exception:
            parsed = {}
    return {
        "ok": proc.returncode == 0,
        "cmd": cmd,
        "cwd": str(cwd) if cwd else "",
        "stdout": proc.stdout,
        "stderr": proc.stderr,
        "returncode": proc.returncode,
        "json": parsed,
    }


def build_record(pack: dict[str, Any], manifest: dict[str, Any]) -> dict[str, Any]:
    return {
        "推荐标题": str(pack.get("selected_title", "")),
        "标题候选": "\n".join(str(title) for title in pack.get("titles", [])),
        "正文": str(pack.get("body", "")),
        "TAG": " ".join(str(tag) for tag in pack.get("tags", [])),
        "首评": str(pack.get("first_comment", "")),
        "审核自检": compact(pack.get("audit_check", {})),
        "状态": "待审核",
        "产出时间": str(manifest.get("created_at", "")) or time.strftime("%Y-%m-%d %H:%M:%S"),
        "审核意见": "",
        "本地路径": compact(manifest),
    }


def collect_attachments(pack: dict[str, Any], manifest: dict[str, Any]) -> list[str]:
    paths: list[str] = []
    cover = pack.get("cover", {}) if isinstance(pack.get("cover"), dict) else {}
    cover_design = pack.get("cover_design", {}) if isinstance(pack.get("cover_design"), dict) else {}

    for path in [cover_design.get("final_cover_path", ""), cover.get("final_image_path", "")]:
        path = str(path or "")
        if path and Path(path).exists():
            paths.append(path)

    for item in pack.get("inner_pages", []) if isinstance(pack.get("inner_pages"), list) else []:
        if isinstance(item, dict):
            path = str(item.get("image_path", "") or "")
            if path and Path(path).exists():
                paths.append(path)

    for key in ["docx_path", "docx", "output"]:
        path = str(manifest.get(key, "") or "")
        if path and Path(path).exists():
            paths.append(path)

    return list(dict.fromkeys(paths))


def relative_attachment_args(paths: list[str]) -> tuple[Path, list[str]]:
    resolved = [Path(path).resolve() for path in paths]
    workdir = Path(os.path.commonpath([str(path.parent) for path in resolved]))
    return workdir, [str(path.relative_to(workdir)) for path in resolved]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--brief", required=True)
    parser.add_argument("--note-pack", required=True)
    parser.add_argument("--manifest")
    parser.add_argument("--schema", default=str(DEFAULT_SCHEMA))
    parser.add_argument("--base-token")
    parser.add_argument("--table-id")
    parser.add_argument("--folder-token")
    parser.add_argument("--name")
    parser.add_argument("--customer")
    parser.add_argument("--public-read", action="store_true", help="set internet-visible read permission after explicit user confirmation")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    brief = load_json(args.brief)
    pack = load_json(args.note_pack)
    manifest = load_json(args.manifest)
    schema = load_json(args.schema)
    lark_cli = shutil.which("lark-cli") or "lark-cli"
    commands: list[dict[str, Any]] = []

    try:
        base_token = args.base_token
        table_id = args.table_id or schema.get("table_name", "小红书笔记审核")
        created_base = False
        customer = args.customer or str(brief.get("brand", "") or "客户")
        base_name = args.name or f"{customer}-小红书笔记审核"
        table_name = str(schema.get("table_name_template", "{customer}-{timestamp}-待审核")).format(
            customer=customer,
            timestamp=time.strftime("%Y%m%d%H%M"),
        )

        if not args.dry_run:
            auth = run_cmd([lark_cli, "auth", "status"], dry_run=False)
            commands.append(auth)
            if not auth["ok"]:
                raise RuntimeError("lark-cli is not authenticated; run: lark-cli auth login --domain base")

        if not base_token:
            cmd = [lark_cli, "base", "+base-create", "--as", "user", "--name", base_name, "--time-zone", "Asia/Shanghai"]
            if args.folder_token:
                cmd.extend(["--folder-token", args.folder_token])
            result = run_cmd(cmd, args.dry_run)
            commands.append(result)
            base_token = find_first(result.get("json"), {"app_token", "base_token", "token"}) or "<base_token_from_base-create>"
            created_base = bool(result.get("ok"))

        if not args.table_id:
            fields_json = json.dumps(schema.get("fields", []), ensure_ascii=False)
            view_json = json.dumps(schema.get("view", [{"name": "客户审核", "type": "grid"}]), ensure_ascii=False)
            result = run_cmd(
                [
                    lark_cli,
                    "base",
                    "+table-create",
                    "--as",
                    "user",
                    "--base-token",
                    base_token,
                    "--name",
                    table_name,
                    "--fields",
                    fields_json,
                    "--view",
                    view_json,
                ],
                args.dry_run,
            )
            commands.append(result)
            table_id = find_first(result.get("json"), {"table_id", "id"}) or table_name

        gallery = schema.get("gallery", {}) if isinstance(schema.get("gallery"), dict) else {}
        if gallery:
            view_name = str(gallery.get("view_name", "画册"))
            cover_field = str(gallery.get("cover_field", "图片"))
            visible_fields = gallery.get("visible_fields", ["图片", "推荐标题", "标题候选", "正文"])
            commands.append(run_cmd(
                [
                    lark_cli,
                    "base",
                    "+view-set-card",
                    "--as",
                    "user",
                    "--base-token",
                    base_token,
                    "--table-id",
                    table_id,
                    "--view-id",
                    view_name,
                    "--json",
                    json.dumps({"cover_field": cover_field}, ensure_ascii=False),
                ],
                args.dry_run,
            ))
            commands.append(run_cmd(
                [
                    lark_cli,
                    "base",
                    "+view-set-visible-fields",
                    "--as",
                    "user",
                    "--base-token",
                    base_token,
                    "--table-id",
                    table_id,
                    "--view-id",
                    view_name,
                    "--json",
                    json.dumps({"visible_fields": visible_fields}, ensure_ascii=False),
                ],
                args.dry_run,
            ))

        field_list = run_cmd([lark_cli, "base", "+field-list", "--as", "user", "--base-token", base_token, "--table-id", table_id], args.dry_run)
        commands.append(field_list)

        record = build_record(pack, manifest)
        upsert = run_cmd(
            [
                lark_cli,
                "base",
                "+record-upsert",
                "--as",
                "user",
                "--base-token",
                base_token,
                "--table-id",
                table_id,
                "--json",
                json.dumps(record, ensure_ascii=False),
            ],
            args.dry_run,
        )
        commands.append(upsert)
        record_id = (
            find_first(upsert.get("json"), {"record_id", "id"})
            or find_first_list_item(upsert.get("json"), {"record_id_list", "record_ids", "ids"})
            or "<record_id_from_record-upsert>"
        )

        attachments = collect_attachments(pack, manifest)
        if attachments:
            upload_cwd, upload_files = relative_attachment_args(attachments)
            upload_cmd = [
                lark_cli,
                "base",
                "+record-upload-attachment",
                "--as",
                "user",
                "--base-token",
                base_token,
                "--table-id",
                table_id,
                "--record-id",
                record_id,
                "--field-id",
                "图片",
            ]
            for path in upload_files:
                upload_cmd.extend(["--file", path])
            commands.append(run_cmd(upload_cmd, args.dry_run, cwd=upload_cwd))

        if args.public_read:
            permission_params = {"token": base_token, "type": "bitable"}
            permission_body = {
                "external_access": True,
                "link_share_entity": "anyone_readable",
                "share_entity": "anyone",
                "security_entity": "anyone_can_view",
                "comment_entity": "anyone_can_view",
            }
            commands.append(run_cmd(
                [
                    lark_cli,
                    "drive",
                    "permission.public",
                    "patch",
                    "--as",
                    "user",
                    "--params",
                    json.dumps(permission_params, ensure_ascii=False),
                    "--data",
                    json.dumps(permission_body, ensure_ascii=False),
                    "--yes",
                ],
                args.dry_run,
            ))

        if created_base and not args.table_id:
            table_list = run_cmd([lark_cli, "base", "+table-list", "--as", "user", "--base-token", base_token], args.dry_run)
            commands.append(table_list)
            tables = (((table_list.get("json") or {}).get("data") or {}).get("tables") or [])
            for table in tables:
                if isinstance(table, dict) and table.get("name") == "数据表" and table.get("id") != table_id:
                    commands.append(run_cmd(
                        [
                            lark_cli,
                            "base",
                            "+table-delete",
                            "--as",
                            "user",
                            "--base-token",
                            base_token,
                            "--table-id",
                            str(table.get("id")),
                            "--yes",
                        ],
                        args.dry_run,
                    ))
                    break

        ok = all(command.get("ok") for command in commands)
        print(json.dumps({
            "ok": ok,
            "base_token": base_token,
            "base_url": f"https://my.feishu.cn/base/{base_token}",
            "table_id": table_id,
            "record_id": record_id,
            "public_read_requested": args.public_read,
            "commands": commands,
        }, ensure_ascii=False, indent=2))
        return 0 if ok else 3
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc), "commands": commands}, ensure_ascii=False, indent=2))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
