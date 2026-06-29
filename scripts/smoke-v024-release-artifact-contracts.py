#!/usr/bin/env python3
"""Validate v0.2.4 release artifacts for native facades, skill governance, Lark, and Office/PDF."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import pathlib
import tarfile
import zipfile
from datetime import datetime, timezone
from typing import Any


EXPECTED_FACADES = {
    "office-presentations": "Presentations",
    "office-spreadsheets": "Spreadsheets",
    "office-documents": "documents",
    "office-pdf": "pdf",
    "image-generation": "imagegen",
}

EXPECTED_TOOLS = {
    "office-presentations": "office_presentations",
    "office-spreadsheets": "office_spreadsheets",
    "office-documents": "office_documents",
    "office-pdf": "office_pdf",
    "image-generation": "imagegen",
}

OFFICE_RUNTIME_REQUIREMENTS = (
    "pypdf",
    "pdfminer.six",
    "python-docx",
    "python-pptx",
    "openpyxl",
    "xlsxwriter",
    "markdownify",
    "reportlab",
    "pymupdf",
)

OFFICE_RUNTIME_MODULES = (
    "pypdf",
    "pdfminer",
    "docx",
    "pptx",
    "openpyxl",
    "xlsxwriter",
    "markdownify",
    "reportlab",
    "fitz",
)

WEBUI_ARTIFACTS = {
    "windows": {
        "id": "webui-windows-x64",
        "name": "EcoreX_0.2.4-webui-windows-x64.zip",
        "install_suffix": "scripts/install-ecorex-webui-win.ps1",
    },
    "macos": {
        "id": "webui-macos-universal",
        "name": "EcoreX_0.2.4-webui-macos-universal.zip",
        "install_suffix": "",
    },
}

WEB_SERVICE_ARTIFACT = {
    "id": "web-linux-service",
    "name": "EcoreX_0.2.4-web-linux-service.tar.gz",
}

RUNTIME_PROJECTION_QUALITY_MARKERS = (
    "_QUALITY_EVIDENCE_ALLOWED_GATES",
    "_QUALITY_EVIDENCE_METRIC_KEYS",
    "_extract_projection_quality_evidence",
    "_safe_projection_quality_check_detail",
    "_safe_projection_quality_evidence",
    "_safe_projection_quality_metric_string",
    "_safe_projection_quality_rendered_artifacts",
    "_quality_ref_is_hmac",
    "qualityEvidence",
    '"renderProof",',
    'record["qualityEvidence"]',
    '"retryGate"',
    "_safe_projection_quality_gate(text)",
)

IMAGE_QUALITY_RUNTIME_MARKERS = (
    "build_image_quality_evidence",
    "analyze_image_quality",
    "decode-valid",
    "artifact-integrity",
    "non-blank",
    "seam-check",
    "overlay-ghosting-check",
    "text-glyph-check",
    "watermark-check",
    "subject-structure-check",
    "anomaly-check",
    "reference-fidelity",
    "_vision_risk_summary",
    "compare_image_reference_quality",
    "build_image_finalization_decision",
    "attach_image_finalization_evidence",
    "IMAGE_FINALIZATION_POLICY_VERSION",
    "_analysis_sample",
    "_apply_decoder_draft",
    "_alpha_sample_metrics",
    "referenceMismatchRisk",
    "glyphFragmentRisk",
    "watermarkRisk",
    "anomalyRisk",
    "overlayGhostingRisk",
    "_EVIDENCE_HMAC_KEY",
)

FEISHU_FORBIDDEN_CHANNEL_TEMPLATES = (
    "或点击链接创建: {qr_url}",
    'logger.debug(f"[FeiShu] receive request: {request}")',
    "Image cached for session {session_id}",
    "File cached for session {session_id}",
    "register_app status: {info}",
    "Failed to get video duration via ffprobe: {result.stderr}",
    "Failed to get video duration: {e}",
    "_feishu_log_text(e)",
    "exc_info=True",
    "websocket handle message error: %s",
    "Websocket client error: %s",
    "Stream: send card failed: %s",
    "Stream: create/send card exception: {e}",
    "Stream: update text failed: {res_json}",
    "Stream: finalize card (close+summary) failed: {res_json}",
    "upload failed: %s",
    "upload video exception: %s",
    "upload audio exception: %s",
    "upload file exception: %s",
    "upload_response.content",
    "response.text",
)

FEISHU_FORBIDDEN_MESSAGE_TEMPLATES = (
    "Downloaded single image, key={image_key}, path={image_path}",
    "Image downloaded from post message, key={image_key}, path={image_path}",
    "Received post message with {len(image_keys)} image(s) and text: {self.content}",
    "audio message: file_key={file_key}, save_path={self.content}",
    "downloading audio: file_key={file_key}, msg_id={self.msg_id}",
    "Failed to download file, file_ref=%s, status=%s, res=%s",
    "Failed to download audio, file_ref=%s, status=%s, res=%s",
    "response.text",
    "_feishu_msg_log_text(e)",
)


def sha256_file(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def add_check(checks: list[dict[str, Any]], label: str, ok: bool, evidence: Any) -> None:
    checks.append({
        "label": label,
        "status": "PASS" if ok else "FAIL",
        "evidence": evidence,
    })


def zip_entry_by_suffix(archive: zipfile.ZipFile, suffix: str) -> str:
    matches = [name for name in archive.namelist() if name.replace("\\", "/").endswith(suffix)]
    if len(matches) != 1:
        raise AssertionError(f"{archive.filename} expected one entry ending with {suffix}, got {len(matches)}")
    return matches[0]


def zip_text_by_suffix(archive: zipfile.ZipFile, suffix: str) -> tuple[str, str]:
    name = zip_entry_by_suffix(archive, suffix)
    return name, archive.read(name).decode("utf-8", errors="replace")


def zip_json_by_suffix(archive: zipfile.ZipFile, suffix: str) -> tuple[str, dict[str, Any]]:
    name, text = zip_text_by_suffix(archive, suffix)
    return name, json.loads(text)


def zip_text_concat_by_suffixes(archive: zipfile.ZipFile, suffixes: tuple[str, ...]) -> str:
    parts: list[str] = []
    for name in archive.namelist():
        normalized = name.replace("\\", "/").lower()
        if normalized.endswith(suffixes):
            try:
                parts.append(archive.read(name).decode("utf-8", errors="replace"))
            except Exception:
                continue
    return "\n".join(parts)


def tar_entry_by_suffix(archive: tarfile.TarFile, suffix: str) -> str:
    matches = [
        member.name
        for member in archive.getmembers()
        if member.isfile() and member.name.replace("\\", "/").endswith(suffix)
    ]
    if len(matches) != 1:
        raise AssertionError(f"{archive.name} expected one entry ending with {suffix}, got {len(matches)}")
    return matches[0]


def tar_text_by_suffix(archive: tarfile.TarFile, suffix: str) -> tuple[str, str]:
    name = tar_entry_by_suffix(archive, suffix)
    member = archive.extractfile(name)
    if member is None:
        raise AssertionError(f"{archive.name} could not read {name}")
    return name, member.read().decode("utf-8", errors="replace")


def tar_text_concat_by_suffixes(archive: tarfile.TarFile, suffixes: tuple[str, ...]) -> str:
    parts: list[str] = []
    for member in archive.getmembers():
        normalized = member.name.replace("\\", "/").lower()
        if not member.isfile() or not normalized.endswith(suffixes):
            continue
        extracted = archive.extractfile(member)
        if extracted is None:
            continue
        try:
            parts.append(extracted.read().decode("utf-8", errors="replace"))
        except Exception:
            continue
    return "\n".join(parts)


def facade_doc_ok(text: str, legacy_id: str, official_skill: str) -> tuple[bool, dict[str, bool]]:
    evidence = {
        "compatibility": f"compatibility-id: {legacy_id}" in text,
        "official": f"adopts-official-skill: {official_skill}" in text,
        "nativeFacade": "ecorex-native-facade: true" in text,
        "qualityGates": "quality-gates:" in text,
    }
    return all(evidence.values()), evidence


def missing_fragments(text: str, required: tuple[str, ...]) -> list[str]:
    return [item for item in required if item not in text]


def present_fragments(text: str, forbidden: tuple[str, ...]) -> list[str]:
    return [item for item in forbidden if item in text]


def artifact_kind_extensions(source: str, kind: str) -> list[str]:
    tree = ast.parse(source)
    for node in tree.body:
        value_node = None
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "ARTIFACT_KINDS"
            for target in node.targets
        ):
            value_node = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id == "ARTIFACT_KINDS":
            value_node = node.value
        if value_node is None:
            continue
        value = ast.literal_eval(value_node)
        return list(((value.get(kind) or {}).get("extensions") or []))
    return []


def projection_quality_marker_evidence(text: str, entry: str) -> tuple[bool, dict[str, Any]]:
    missing = missing_fragments(text, RUNTIME_PROJECTION_QUALITY_MARKERS)
    return not missing, {
        "entrySuffix": entry.split("runtime/", 1)[-1],
        "missing": missing,
    }


def image_quality_marker_evidence(text: str, entry: str) -> tuple[bool, dict[str, Any]]:
    missing = missing_fragments(text, IMAGE_QUALITY_RUNTIME_MARKERS)
    return not missing, {
        "entrySuffix": entry.split("runtime/", 1)[-1],
        "missing": missing,
    }


def inspect_webui_artifact(path: pathlib.Path, artifact_id: str, install_suffix: str) -> dict[str, Any]:
    if not path.is_file():
        raise AssertionError(f"missing artifact: {path.name}")

    checks: list[dict[str, Any]] = []
    with zipfile.ZipFile(path) as archive:
        archive_names = [name.replace("\\", "/") for name in archive.namelist()]
        archive_names_lower = [name.lower() for name in archive_names]
        bytecode_entries = [
            name for name in archive_names_lower
            if "/__pycache__/" in name or name.endswith((".pyc", ".pyo"))
        ]
        add_check(
            checks,
            f"{artifact_id} release package strips generated Python bytecode",
            not bytecode_entries,
            {"entryCount": len(bytecode_entries)},
        )
        for legacy_id, official_skill in EXPECTED_FACADES.items():
            entry, text = zip_text_by_suffix(archive, f"runtime/skills/{legacy_id}/SKILL.md")
            ok, evidence = facade_doc_ok(text, legacy_id, official_skill)
            evidence["entrySuffix"] = entry.split("runtime/", 1)[-1]
            add_check(checks, f"{artifact_id} {legacy_id} facade metadata", ok, evidence)

        formatter_entry, formatter = zip_text_by_suffix(archive, "runtime/agent/skills/formatter.py")
        service_entry, service = zip_text_by_suffix(archive, "runtime/agent/skills/service.py")
        manager_entry, manager = zip_text_by_suffix(archive, "runtime/agent/skills/manager.py")
        skill_bridge_entry, skill_bridge = zip_text_by_suffix(archive, "runtime/agent/skills/tool_bridge.py")
        registry_entry, registry = zip_text_by_suffix(archive, "runtime/agent/extensions/registry.py")
        tools_init_entry, tools_init = zip_text_by_suffix(archive, "runtime/agent/tools/__init__.py")
        office_tools_entry, office_tools = zip_text_by_suffix(archive, "runtime/agent/tools/office_artifacts/office_artifacts.py")
        agent_stream_entry, agent_stream = zip_text_by_suffix(archive, "runtime/agent/protocol/agent_stream.py")
        runtime_projection_entry, runtime_projection = zip_text_by_suffix(archive, "runtime/agent/protocol/runtime_projection.py")
        agent_capability_entry, agent_capability = zip_text_by_suffix(archive, "runtime/agent/tools/agent_capability/agent_capability.py")
        optional_abilities_entry, optional_abilities = zip_text_by_suffix(archive, "runtime/agent/tools/optional_abilities/optional_abilities.py")
        broker_entry, broker = zip_text_by_suffix(archive, "runtime/common/ecorex_tool_permissions.py")
        bash_entry, bash_tool = zip_text_by_suffix(archive, "runtime/agent/tools/bash/bash.py")
        web_channel_entry, web_channel = zip_text_by_suffix(archive, "runtime/channel/web/web_channel.py")
        feishu_cli_entry, feishu_cli = zip_text_by_suffix(archive, "runtime/agent/tools/feishu_cli/feishu_cli.py")
        tongxin_entry, tongxin_tool = zip_text_by_suffix(archive, "runtime/agent/tools/tongxin_cli/tongxin_cli.py")
        image_quality_entry, image_quality_runtime = zip_text_by_suffix(archive, "runtime/common/image_quality_runtime.py")
        image_job_entry, image_job_service = zip_text_by_suffix(archive, "runtime/agent/protocol/image_job_service.py")
        imagegen_tool_entry, imagegen_tool = zip_text_by_suffix(archive, "runtime/agent/tools/imagegen/imagegen.py")
        provider_runner_entry, provider_runner = zip_text_by_suffix(archive, "runtime/agent/tools/imagegen/provider_runner.py")
        add_check(
            checks,
            f"{artifact_id} prompt exposes facade fields",
            "<compatibility_id>" in formatter
            and "<adopts_official_skill>" in formatter
            and "<ecorex_native_facade>" in formatter
            and "<quality_gates>" in formatter,
            {"entrySuffix": formatter_entry.split("runtime/", 1)[-1]},
        )
        missing_callable_tools = [
            tool_name
            for tool_name in EXPECTED_TOOLS.values()
            if f"<callable_tool>{{_escape_xml(callable_tool)}}</callable_tool>" not in formatter
            and tool_name not in skill_bridge
        ]
        add_check(
            checks,
            f"{artifact_id} prompt exposes callable tool bridge",
            "<callable_tool>" in formatter
            and "resolve_callable_tool_name" in formatter
            and not missing_callable_tools,
            {
                "formatterEntrySuffix": formatter_entry.split("runtime/", 1)[-1],
                "skillBridgeEntrySuffix": skill_bridge_entry.split("runtime/", 1)[-1],
                "missingTools": missing_callable_tools,
            },
        )
        add_check(
            checks,
            f"{artifact_id} Feishu CLI Codex-style split auth bridge",
            "--device-code" in feishu_cli
            and '"auth", "qrcode"' in feishu_cli
            and "--app-secret-stdin" in feishu_cli
            and "input_text=app_secret + \"\\n\"" in feishu_cli
            and "def _feishu_credentials" in feishu_cli
            and "def _safe_feishu_cli_status_probe" in web_channel
            and "agentCliStatus" in web_channel,
            {
                "feishuCliEntrySuffix": feishu_cli_entry.split("runtime/", 1)[-1],
                "webChannelEntrySuffix": web_channel_entry.split("runtime/", 1)[-1],
            },
        )
        add_check(
            checks,
            f"{artifact_id} skill API exposes facade fields",
            "compatibility_id" in service
            and "adopts_official_skill" in service
            and "ecorex_native_facade" in service
            and "quality_gates" in service,
            {"entrySuffix": service_entry.split("runtime/", 1)[-1]},
        )
        add_check(
            checks,
            f"{artifact_id} Skill governance API schema packaged",
            "SKILL_SOURCE_GROUP_LABELS" in service
            and "SKILL_PURPOSE_GROUP_LABELS" in service
            and "_decorate_skill_governance" in service
            and "builtin_catalog" in service
            and "source_group" in service
            and "purpose_group" in service
            and "toggleable" in service
            and "Built-in factory skills are always enabled and cannot be disabled." in service,
            {"entrySuffix": service_entry.split("runtime/", 1)[-1]},
        )
        add_check(
            checks,
            f"{artifact_id} Skill governance builtin lock packaged",
            "builtin_catalog_names" in manager
            and "is_builtin_catalog_skill" in manager
            and '"builtin_catalog": builtin_catalog' in manager
            and '"source": skill.source' in manager
            and "Built-in factory skills are always enabled and cannot be disabled." in manager,
            {"entrySuffix": manager_entry.split("runtime/", 1)[-1]},
        )
        add_check(
            checks,
            f"{artifact_id} Skill governance extension projection packaged",
            "sourceGroup" in registry
            and "purposeGroup" in registry
            and "sourceLabel" in registry
            and "purposeLabel" in registry
            and "builtinCatalog" in registry
            and "toggleable" in registry
            and "built-in-locked" in registry
            and "skill_agent_surface" in registry
            and "toolSchemaCallable" in registry,
            {"entrySuffix": registry_entry.split("runtime/", 1)[-1]},
        )
        add_check(
            checks,
            f"{artifact_id} skill callable tool bridge packaged",
            "SKILL_CALLABLE_TOOL_ALIASES" in skill_bridge
            and '"office-pdf": "office_pdf"' in skill_bridge
            and '"presentations": "office_presentations"' in skill_bridge
            and '"tongxin-cli": "tongxin_cli"' in skill_bridge
            and '"芯助手": "tongxin_cli"' in skill_bridge
            and "def skill_agent_surface" in skill_bridge,
            {"entrySuffix": skill_bridge_entry.split("runtime/", 1)[-1]},
        )
        console_entry, console_js = zip_text_by_suffix(archive, "runtime/channel/web/static/js/console.js")
        add_check(
            checks,
            f"{artifact_id} Skill governance legacy console lock packaged",
            "sk.locked === true || sourceGroup === 'builtin'" in console_js
            and "sk.toggleable !== false" in console_js
            and 'disabled aria-disabled="true"' in console_js
            and "skill.toggleable === false" in console_js,
            {"entrySuffix": console_entry.split("runtime/", 1)[-1]},
        )
        app_bundle_text = zip_text_concat_by_suffixes(archive, (".js", ".css", ".html"))
        add_check(
            checks,
            f"{artifact_id} Skill governance WebUI display packaged",
            "skill-source-section" in app_bundle_text
            and "skill-purpose-group" in app_bundle_text
            and "内置能力默认启用" in app_bundle_text
            and "图像 / 媒体" in app_bundle_text
            and "办公能力" in app_bundle_text,
            {"bundleTextScanned": True},
        )
        add_check(
            checks,
            f"{artifact_id} Skill governance Streaming Markdown live render packaged",
            "streaming-markdown" in app_bundle_text
            and "streaming-tail" not in app_bundle_text
            and "streaming-code" not in app_bundle_text
            and "chars streaming" not in app_bundle_text,
            {"bundleTextScanned": True},
        )
        add_check(
            checks,
            f"{artifact_id} Office/PDF QA evidence WebUI display packaged",
            "quality-evidence-badge" in app_bundle_text
            and "quality-evidence-panel" in app_bundle_text
            and "PDF" in app_bundle_text
            and "decode-valid" in app_bundle_text
            and "seam-check" in app_bundle_text
            and "text-glyph-check" in app_bundle_text
            and "watermark-check" in app_bundle_text
            and "anomaly-check" in app_bundle_text
            and "reference-fidelity" in app_bundle_text
            and "reference-fidelity-skipped-review" in app_bundle_text
            and "retry_count" in app_bundle_text
            and "max_retries" in app_bundle_text
            and "retry_gate" in app_bundle_text
            and "未通过" in app_bundle_text,
            {"bundleTextScanned": True},
        )
        add_check(
            checks,
            f"{artifact_id} Tongxin CLI WebUI configure-only display packaged",
            "配置检查" in app_bundle_text
            and "默认只读" in app_bundle_text
            and "安装并继续" in app_bundle_text,
            {"bundleTextScanned": True},
        )
        add_check(
            checks,
            f"{artifact_id} Tongxin CLI read-only tool packaged",
            "TongxinCli" in tools_init
            and "class TongxinCli" in tongxin_tool
            and "READ_ONLY_ALLOWED_COMMANDS" in tongxin_tool
            and "validate_read_only_tongxin_args" in tongxin_tool
            and "_COMMAND_ALLOWED_FLAGS" in tongxin_tool
            and "_sanitize_json" in tongxin_tool
            and "_is_sensitive_json_key" in tongxin_tool
            and "_SENSITIVE_JSON_COMPACT_KEYS" in tongxin_tool
            and "auth[_-]?header" in tongxin_tool
            and "credential[_-]?id" in tongxin_tool
            and "bearer\\s+" in tongxin_tool
            and '"json": _sanitize_json(self._parse_json(result.stdout))' in tongxin_tool
            and "shell=True" not in tongxin_tool,
            {
                "toolsInitEntrySuffix": tools_init_entry.split("runtime/", 1)[-1],
                "toolEntrySuffix": tongxin_entry.split("runtime/", 1)[-1],
            },
        )
        add_check(
            checks,
            f"{artifact_id} Tongxin CLI permission and raw bash guard packaged",
            '"tongxin_cli"' in broker
            and "default-read-only-tongxin-cli" in broker
            and "def _extract_simple_tongxin_cli_args" in agent_stream
            and "raw bash tongxin-cli" in agent_stream
            and "agent_capability_install_pack_preflight" in agent_stream
            and "SKILL_CALLABLE_TOOL_ALIASES" in agent_stream
            and '"office": (' in agent_stream
            and "office_presentations" in agent_stream
            and "office_spreadsheets" in agent_stream
            and "office_documents" in agent_stream
            and "office_pdf" in agent_stream
            and "Do not call Tongxin Assistant CLI through raw bash" in bash_tool,
            {
                "brokerEntrySuffix": broker_entry.split("runtime/", 1)[-1],
                "agentStreamEntrySuffix": agent_stream_entry.split("runtime/", 1)[-1],
                "bashEntrySuffix": bash_entry.split("runtime/", 1)[-1],
            },
        )
        add_check(
            checks,
            f"{artifact_id} Office/PDF callable skill tools packaged",
            "OfficeDocumentsTool" in tools_init
            and "OfficePdfTool" in tools_init
            and "OfficePresentationsTool" in tools_init
            and "OfficeSpreadsheetsTool" in tools_init
            and "class OfficeDocumentsTool" in office_tools
            and "class OfficePdfTool" in office_tools
            and "class OfficePresentationsTool" in office_tools
            and "class OfficeSpreadsheetsTool" in office_tools
            and "build_pdf_quality_evidence" in office_tools
            and "build_presentation_quality_evidence" in office_tools
            and "authorize_file_access" in office_tools,
            {
                "toolsInitEntrySuffix": tools_init_entry.split("runtime/", 1)[-1],
                "officeToolsEntrySuffix": office_tools_entry.split("runtime/", 1)[-1],
            },
        )
        add_check(
            checks,
            f"{artifact_id} Tongxin CLI configure-only auto-configuration packaged",
            "TONGXIN_CLI_INSTALL_HINT" in optional_abilities
            and "_configure_tongxin_cli" in optional_abilities
            and "configurationState" in optional_abilities
            and "agentCanInstall\": bool(meta.get(\"packId\")) and not bool(meta.get(\"configureOnly\"))" in optional_abilities
            and "OptionalAbilities().execute(configure_args)" in agent_capability
            and "script_path" in agent_capability
            and '"tongxin": "tongxin-cli"' in agent_capability,
            {
                "optionalAbilitiesEntrySuffix": optional_abilities_entry.split("runtime/", 1)[-1],
                "agentCapabilityEntrySuffix": agent_capability_entry.split("runtime/", 1)[-1],
            },
        )
        add_check(
            checks,
            f"{artifact_id} Tongxin CLI install request guidance packaged",
            'normalized_pack_id in {"tongxin", "tongxin-cli", "xin-agent", "xin-agent-cli", "tx-assistant"}' in web_channel
            and "Connect the EcoreX Tongxin Assistant read-only CLI capability" in web_channel
            and "Do not install through raw bash/curl/npm/git" in web_channel
            and "tools.tongxin_cli.script_path" in web_channel
            and "Only read-only queries are allowed for all users" in web_channel,
            {"webChannelEntrySuffix": web_channel_entry.split("runtime/", 1)[-1]},
        )
        add_check(
            checks,
            f"{artifact_id} Web image retry defaults packaged",
            "quality_retry_max" in web_channel
            and "_image_job_quality_retry_max" in web_channel
            and "_quality_retry_attempt" in web_channel
            and "run_image_generation_payload" in web_channel
            and "image_generation_env_with_config" in web_channel
            and "Quality retry: regenerate" in web_channel,
            {"webChannelEntrySuffix": web_channel_entry.split("runtime/", 1)[-1]},
        )

        office_entry, office_runtime = zip_text_by_suffix(archive, "runtime/common/office_pdf_runtime.py")
        add_check(
            checks,
            f"{artifact_id} Office/PDF runtime primitives packaged",
            "probe_office_pdf_runtime" in office_runtime
            and "inspect_office_pdf_artifact" in office_runtime
            and "render_pdf_pages" in office_runtime
            and "render_presentation_preview" in office_runtime
            and "render_spreadsheet_preview" in office_runtime
            and "render_document_preview" in office_runtime
            and "build_quality_evidence" in office_runtime
            and "build_presentation_quality_evidence" in office_runtime
            and "build_spreadsheet_quality_evidence" in office_runtime
            and "build_document_quality_evidence" in office_runtime
            and "build_pdf_quality_evidence" in office_runtime
            and "analyze_presentation_quality" in office_runtime
            and "analyze_spreadsheet_quality" in office_runtime
            and "analyze_document_quality" in office_runtime
            and "analyze_pdf_quality" in office_runtime
            and "compare_pdf_page_quality" in office_runtime
            and "_render_artifact_proof" in office_runtime
            and "_register_trusted_render_artifact" in office_runtime
            and "_TRUSTED_RENDER_REGISTRY" in office_runtime
            and "require_render_proof=True" in office_runtime
            and "require_registered_proof=True" in office_runtime
            and "_normalize_presentation_authoring_route" in office_runtime
            and "_sanitize_check_detail" in office_runtime
            and "PRESENTATION_OVERLAP_RATIO_THRESHOLD" in office_runtime
            and "SPREADSHEET_MAX_CELLS" in office_runtime,
            {"entrySuffix": office_entry.split("runtime/", 1)[-1]},
        )
        spreadsheet_extensions = artifact_kind_extensions(office_runtime, "spreadsheet")
        add_check(
            checks,
            f"{artifact_id} Office/PDF runtime redacted render contract",
            "include_paths" not in office_runtime
            and 'item["path"]' not in office_runtime
            and "item['path']" not in office_runtime,
            {"entrySuffix": office_entry.split("runtime/", 1)[-1]},
        )
        add_check(
            checks,
            f"{artifact_id} Office/PDF trusted presentation render contract",
            "renderProof" in office_runtime
            and "rendered_count = len(render_items)" in office_runtime
            and "_sanitize_render_artifacts(renders or [], require_render_proof=True)" in office_runtime,
            {"entrySuffix": office_entry.split("runtime/", 1)[-1]},
        )
        add_check(
            checks,
            f"{artifact_id} Office/PDF spreadsheet quality evidence packaged",
            "build_spreadsheet_quality_evidence" in office_runtime
            and "numericTextRiskCount" in office_runtime
            and "formulaErrorTokenCount" in office_runtime
            and "dashboard-structure" in office_runtime
            and "render_spreadsheet_preview" in office_runtime,
            {"entrySuffix": office_entry.split("runtime/", 1)[-1]},
        )
        add_check(
            checks,
            f"{artifact_id} Office/PDF document quality evidence packaged",
            "build_document_quality_evidence" in office_runtime
            and "analyze_document_quality" in office_runtime
            and "render_document_preview" in office_runtime
            and "tableIssueCount" in office_runtime
            and "commentReferenceCount" in office_runtime
            and "commentIdMismatchCount" in office_runtime
            and "structure-check" in office_runtime,
            {"entrySuffix": office_entry.split("runtime/", 1)[-1]},
        )
        add_check(
            checks,
            f"{artifact_id} Office/PDF PDF quality evidence packaged",
            "build_pdf_quality_evidence" in office_runtime
            and "analyze_pdf_quality" in office_runtime
            and "compare_pdf_page_quality" in office_runtime
            and "visual-diff" in office_runtime
            and "tableCandidatePageCount" in office_runtime
            and "_inspect_pdf_page_drawing_stream" in office_runtime,
            {"entrySuffix": office_entry.split("runtime/", 1)[-1]},
        )
        add_check(
            checks,
            f"{artifact_id} Office/PDF PDF registered render proof packaged",
            "require_registered_proof=True" in office_runtime
            and "trusted_source_ref=_hash_ref(source)" in office_runtime
            and "_is_registered_trusted_render_artifact" in office_runtime,
            {"entrySuffix": office_entry.split("runtime/", 1)[-1]},
        )
        add_check(
            checks,
            f"{artifact_id} Office/PDF QA evidence projection packaged",
            *projection_quality_marker_evidence(runtime_projection, runtime_projection_entry),
        )
        add_check(
            checks,
            f"{artifact_id} Image structural QA runtime packaged",
            *image_quality_marker_evidence(image_quality_runtime, image_quality_entry),
        )
        add_check(
            checks,
            f"{artifact_id} Image structural QA job/tool integration packaged",
            "build_image_quality_evidence" in image_job_service
            and '"qualityEvidence"' in image_job_service
            and "state.artifacts.append(safe_artifact)" in image_job_service
            and "_authorized_quality_reference_images" in image_job_service
            and "_image_quality_target" in image_job_service
            and "_quality_retry_limit" in image_job_service
            and "_finalize_safe_artifacts" in image_job_service
            and "reference_images = _authorized_quality_reference_images(task)" in image_job_service
            and "reference_images=reference_images" in image_job_service
            and "authorize_file_access(\"read\"" in image_job_service
            and "provider_latency_ms" in image_job_service
            and "quality_latency_ms" in image_job_service
            and '"quality_check"' in image_job_service
            and "run_image_generation_payload" in provider_runner
            and "_build_providers_with_env" in provider_runner
            and "runnerMode" in provider_runner
            and "run_image_generation_payload" in imagegen_tool
            and "image_generation_env_with_config" in imagegen_tool
            and "build_image_quality_evidence" in imagegen_tool
            and "_aggregate_image_quality_evidence" in imagegen_tool
            and "_with_image_finalization" in imagegen_tool
            and "quality_retry_max" in imagegen_tool
            and "Quality retry: regenerate" in imagegen_tool
            and "reference_images=authorized_sources" in imagegen_tool
            and "_safe_image_result_row" in imagegen_tool
            and "_safe_imagegen_failure_payload" in imagegen_tool
            and "_safe_text_presence" in imagegen_tool
            and "stderrTail" not in imagegen_tool
            and '"qualityEvidence"' in imagegen_tool
            and '"timing"' in imagegen_tool
            and "providerTotalLatencyMs" in imagegen_tool,
            {
                "imageJobEntrySuffix": image_job_entry.split("runtime/", 1)[-1],
                "imagegenEntrySuffix": imagegen_tool_entry.split("runtime/", 1)[-1],
                "providerRunnerEntrySuffix": provider_runner_entry.split("runtime/", 1)[-1],
            },
        )
        add_check(
            checks,
            f"{artifact_id} Office/PDF spreadsheet extension contract",
            ".tsv" in spreadsheet_extensions
            and ".xls" not in spreadsheet_extensions
            and {".csv", ".xlsx", ".xlsm"}.issubset(set(spreadsheet_extensions)),
            {"extensions": spreadsheet_extensions},
        )

        presentation_skill_entry, presentation_skill = zip_text_by_suffix(
            archive,
            "runtime/skills/office-presentations/SKILL.md",
        )
        add_check(
            checks,
            f"{artifact_id} Office/PDF presentation quality gates packaged",
            "layout-bounds" in presentation_skill
            and "font-size-check" in presentation_skill
            and "chart-integrity" in presentation_skill
            and "presentation QA evidence builder" in presentation_skill,
            {"entrySuffix": presentation_skill_entry.split("runtime/", 1)[-1]},
        )

        spreadsheet_skill_entry, spreadsheet_skill = zip_text_by_suffix(
            archive,
            "runtime/skills/office-spreadsheets/SKILL.md",
        )
        document_skill_entry, document_skill = zip_text_by_suffix(
            archive,
            "runtime/skills/office-documents/SKILL.md",
        )
        pdf_skill_entry, pdf_skill = zip_text_by_suffix(
            archive,
            "runtime/skills/office-pdf/SKILL.md",
        )
        add_check(
            checks,
            f"{artifact_id} Office/PDF document quality gates packaged",
            "structure-check" in document_skill
            and "render-docx evidence must come from trusted runtime render output" in document_skill,
            {"entrySuffix": document_skill_entry.split("runtime/", 1)[-1]},
        )
        add_check(
            checks,
            f"{artifact_id} Office/PDF spreadsheet legacy XLS guidance",
            ".tsv" in spreadsheet_skill
            and "convert legacy .xls to .xlsx" in spreadsheet_skill.lower()
            and ".xlsx, .xls, .csv" not in spreadsheet_skill.lower(),
            {"entrySuffix": spreadsheet_skill_entry.split("runtime/", 1)[-1]},
        )
        add_check(
            checks,
            f"{artifact_id} Office/PDF spreadsheet quality gates packaged",
            "dashboard-structure" in spreadsheet_skill
            and "render-preview" in spreadsheet_skill
            and "render-preview evidence must come from trusted runtime render output" in spreadsheet_skill,
            {"entrySuffix": spreadsheet_skill_entry.split("runtime/", 1)[-1]},
        )
        add_check(
            checks,
            f"{artifact_id} Office/PDF PDF quality gates packaged",
            "visual-diff" in pdf_skill
            and "PDF QA evidence builder" in pdf_skill
            and "render evidence must come from trusted runtime render output" in pdf_skill,
            {"entrySuffix": pdf_skill_entry.split("runtime/", 1)[-1]},
        )

        _, capabilities = zip_json_by_suffix(archive, "runtime/capabilities.json")
        packs = {str(item.get("id")): item for item in capabilities.get("packs", []) if isinstance(item, dict)}
        tongxin_pack = packs.get("tongxin-cli") or {}
        add_check(
            checks,
            f"{artifact_id} Tongxin CLI capability manifest packaged",
            tongxin_pack.get("readOnly") is True
            and tongxin_pack.get("defaultEnabled") is True
            and tongxin_pack.get("configureOnly") is True
            and "realtime summary --xhs-channel all" in (tongxin_pack.get("allowedCommands") or []),
            {
                "packPresent": bool(tongxin_pack),
                "readOnly": tongxin_pack.get("readOnly"),
                "defaultEnabled": tongxin_pack.get("defaultEnabled"),
                "configureOnly": tongxin_pack.get("configureOnly"),
            },
        )
        office_pack = packs.get("office-pdf") or {}
        office_requirements = {str(item).lower() for item in office_pack.get("requirements") or []}
        office_modules = {str(item) for item in office_pack.get("moduleChecks") or []}
        add_check(
            checks,
            f"{artifact_id} Office/PDF capability manifest packaged",
            all(any(required in item for item in office_requirements) for required in OFFICE_RUNTIME_REQUIREMENTS)
            and set(OFFICE_RUNTIME_MODULES).issubset(office_modules)
            and office_pack.get("discoveryOnly") is not True,
            {
                "missingRequirements": [
                    required
                    for required in OFFICE_RUNTIME_REQUIREMENTS
                    if not any(required in item for item in office_requirements)
                ],
                "missingModules": sorted(set(OFFICE_RUNTIME_MODULES) - office_modules),
            },
        )

        readiness_entry, readiness = zip_text_by_suffix(archive, "runtime/common/feishu_runtime_readiness.py")
        core_entry, core_requirements = zip_text_by_suffix(archive, "runtime/core-requirements.txt")
        core_lower = core_requirements.lower()
        missing_office_core = [item for item in OFFICE_RUNTIME_REQUIREMENTS if item not in core_lower]
        add_check(
            checks,
            f"{artifact_id} core runtime declares Office/PDF dependencies",
            not missing_office_core,
            {"missing": missing_office_core, "entrySuffix": core_entry.split("runtime/", 1)[-1]},
        )
        if artifact_id == "webui-windows-x64":
            reportlab_entries = [name for name in archive_names_lower if "/site-packages/reportlab" in name]
            pymupdf_entries = [
                name for name in archive_names_lower
                if "/site-packages/pymupdf" in name or "/site-packages/fitz" in name
            ]
            add_check(
                checks,
                f"{artifact_id} bundled Office/PDF wheels installed",
                bool(reportlab_entries) and bool(pymupdf_entries),
                {"reportlabEntryCount": len(reportlab_entries), "pymupdfEntryCount": len(pymupdf_entries)},
            )
        elif artifact_id == "webui-macos-universal":
            wheel_checks = {}
            for arch in ("mac-arm64", "mac-x64"):
                arch_entries = [name for name in archive_names_lower if f"/wheelhouse/{arch}/" in name]
                wheel_checks[arch] = {
                    "reportlab": any("reportlab" in name for name in arch_entries),
                    "pymupdf": any("pymupdf" in name for name in arch_entries),
                }
            add_check(
                checks,
                f"{artifact_id} bundled Office/PDF macOS wheels",
                all(item["reportlab"] and item["pymupdf"] for item in wheel_checks.values()),
                wheel_checks,
            )
        add_check(
            checks,
            f"{artifact_id} Lark SDK readiness probe packaged",
            "probe_lark_oapi" in readiness
            and "lark_oapi_available" in readiness
            and "Install lark-oapi into the active EcoreX WebUI Python runtime" in readiness,
            {"entrySuffix": readiness_entry.split("runtime/", 1)[-1]},
        )
        add_check(
            checks,
            f"{artifact_id} core runtime declares lark-oapi",
            "lark-oapi" in core_requirements,
            {"entrySuffix": core_entry.split("runtime/", 1)[-1]},
        )

        if install_suffix:
            install_entry, install_text = zip_text_by_suffix(archive, install_suffix)
            add_check(
                checks,
                f"{artifact_id} installer does not pip install lark-oapi on first run",
                "Ensure-PythonDependency" not in install_text
                and "python-deps-install.last.log" not in install_text
                and 'Test-PythonModule -Python $python -ModuleName "lark_oapi"' in install_text,
                {"entrySuffix": install_entry},
            )

        lark_entries = [name for name in archive.namelist() if "lark_oapi" in name or "lark_oapi-" in name]
        add_check(
            checks,
            f"{artifact_id} bundled lark_oapi payload present",
            bool(lark_entries),
            {"entryCount": len(lark_entries), "installEnsuresOnFirstRun": False},
        )
        lark_dep_entries = [
            name for name in archive_names_lower
            if "requests_toolbelt" in name or "requests-toolbelt" in name
        ]
        add_check(
            checks,
            f"{artifact_id} bundled lark dependency payload present",
            bool(lark_dep_entries),
            {"entryCount": len(lark_dep_entries)},
        )

        channel_entry, channel_source = zip_text_by_suffix(archive, "runtime/channel/feishu/feishu_channel.py")
        message_entry, message_source = zip_text_by_suffix(archive, "runtime/channel/feishu/feishu_message.py")
        mask_entry, mask_source = zip_text_by_suffix(archive, "runtime/common/ecorex_public_payload.py")
        channel_required = (
            "_feishu_log_ref",
            "_feishu_event_log_summary",
            "_feishu_register_status_summary",
            "_feishu_api_response_log_summary",
            "error_type",
        )
        message_required = (
            "_feishu_msg_log_ref",
            "body_bytes",
            "error_type",
        )
        mask_required = (
            "(?:file|img)_v",
            "C:\\\\Users\\\\[redacted]",
            "open.feishu.cn",
        )
        add_check(
            checks,
            f"{artifact_id} Feishu log redaction helpers packaged",
            not missing_fragments(channel_source, channel_required)
            and not missing_fragments(message_source, message_required)
            and not missing_fragments(mask_source, mask_required),
            {
                "channelEntrySuffix": channel_entry.split("runtime/", 1)[-1],
                "messageEntrySuffix": message_entry.split("runtime/", 1)[-1],
                "maskEntrySuffix": mask_entry.split("runtime/", 1)[-1],
            },
        )
        channel_hits = present_fragments(channel_source, FEISHU_FORBIDDEN_CHANNEL_TEMPLATES)
        message_hits = present_fragments(message_source, FEISHU_FORBIDDEN_MESSAGE_TEMPLATES)
        add_check(
            checks,
            f"{artifact_id} no legacy raw Feishu channel log templates",
            not channel_hits,
            {"hitCount": len(channel_hits)},
        )
        add_check(
            checks,
            f"{artifact_id} no legacy raw Feishu message log templates",
            not message_hits,
            {"hitCount": len(message_hits)},
        )

    failures = [item for item in checks if item["status"] != "PASS"]
    return {
        "artifactId": artifact_id,
        "artifactName": path.name,
        "size": path.stat().st_size,
        "sha256": sha256_file(path),
        "checks": checks,
        "failed": failures,
    }


def inspect_web_service_artifact(path: pathlib.Path, artifact_id: str) -> dict[str, Any]:
    if not path.is_file():
        raise AssertionError(f"missing artifact: {path.name}")

    checks: list[dict[str, Any]] = []
    with tarfile.open(path, "r:gz") as archive:
        runtime_projection_entry, runtime_projection = tar_text_by_suffix(
            archive,
            "runtime/agent/protocol/runtime_projection.py",
        )
        add_check(
            checks,
            f"{artifact_id} Office/PDF QA evidence projection packaged",
            *projection_quality_marker_evidence(runtime_projection, runtime_projection_entry),
        )
        image_quality_entry, image_quality_runtime = tar_text_by_suffix(
            archive,
            "runtime/common/image_quality_runtime.py",
        )
        image_job_entry, image_job_service = tar_text_by_suffix(
            archive,
            "runtime/agent/protocol/image_job_service.py",
        )
        imagegen_tool_entry, imagegen_tool = tar_text_by_suffix(
            archive,
            "runtime/agent/tools/imagegen/imagegen.py",
        )
        provider_runner_entry, provider_runner = tar_text_by_suffix(
            archive,
            "runtime/agent/tools/imagegen/provider_runner.py",
        )
        web_channel_entry, web_channel = tar_text_by_suffix(
            archive,
            "runtime/channel/web/web_channel.py",
        )
        add_check(
            checks,
            f"{artifact_id} Image structural QA runtime packaged",
            *image_quality_marker_evidence(image_quality_runtime, image_quality_entry),
        )
        add_check(
            checks,
            f"{artifact_id} Image structural QA job/tool integration packaged",
            "build_image_quality_evidence" in image_job_service
            and '"qualityEvidence"' in image_job_service
            and "state.artifacts.append(safe_artifact)" in image_job_service
            and "_authorized_quality_reference_images" in image_job_service
            and "_image_quality_target" in image_job_service
            and "_quality_retry_limit" in image_job_service
            and "_finalize_safe_artifacts" in image_job_service
            and "reference_images = _authorized_quality_reference_images(task)" in image_job_service
            and "reference_images=reference_images" in image_job_service
            and "authorize_file_access(\"read\"" in image_job_service
            and "provider_latency_ms" in image_job_service
            and "quality_latency_ms" in image_job_service
            and '"quality_check"' in image_job_service
            and "run_image_generation_payload" in provider_runner
            and "_build_providers_with_env" in provider_runner
            and "runnerMode" in provider_runner
            and "run_image_generation_payload" in imagegen_tool
            and "image_generation_env_with_config" in imagegen_tool
            and "build_image_quality_evidence" in imagegen_tool
            and "_aggregate_image_quality_evidence" in imagegen_tool
            and "_with_image_finalization" in imagegen_tool
            and "quality_retry_max" in imagegen_tool
            and "Quality retry: regenerate" in imagegen_tool
            and "reference_images=authorized_sources" in imagegen_tool
            and "_safe_image_result_row" in imagegen_tool
            and "_safe_imagegen_failure_payload" in imagegen_tool
            and "_safe_text_presence" in imagegen_tool
            and "stderrTail" not in imagegen_tool
            and '"timing"' in imagegen_tool
            and "providerTotalLatencyMs" in imagegen_tool,
            {
                "imageJobEntrySuffix": image_job_entry.split("runtime/", 1)[-1],
                "imagegenEntrySuffix": imagegen_tool_entry.split("runtime/", 1)[-1],
                "providerRunnerEntrySuffix": provider_runner_entry.split("runtime/", 1)[-1],
            },
        )
        add_check(
            checks,
            f"{artifact_id} Web image retry defaults packaged",
            "quality_retry_max" in web_channel
            and "_image_job_quality_retry_max" in web_channel
            and "_quality_retry_attempt" in web_channel
            and "run_image_generation_payload" in web_channel
            and "image_generation_env_with_config" in web_channel
            and "Quality retry: regenerate" in web_channel,
            {"webChannelEntrySuffix": web_channel_entry.split("runtime/", 1)[-1]},
        )
        app_bundle_text = tar_text_concat_by_suffixes(archive, (".js", ".css", ".html"))
        add_check(
            checks,
            f"{artifact_id} Office/PDF QA evidence WebUI display packaged",
            "quality-evidence-badge" in app_bundle_text
            and "quality-evidence-panel" in app_bundle_text
            and "PDF" in app_bundle_text
            and "decode-valid" in app_bundle_text
            and "seam-check" in app_bundle_text
            and "text-glyph-check" in app_bundle_text
            and "watermark-check" in app_bundle_text
            and "anomaly-check" in app_bundle_text
            and "reference-fidelity" in app_bundle_text
            and "reference-fidelity-skipped-review" in app_bundle_text
            and "retry_count" in app_bundle_text
            and "max_retries" in app_bundle_text
            and "retry_gate" in app_bundle_text
            and "未通过" in app_bundle_text,
            {"bundleTextScanned": True},
        )

    failures = [item for item in checks if item["status"] != "PASS"]
    return {
        "artifactId": artifact_id,
        "artifactName": path.name,
        "size": path.stat().st_size,
        "sha256": sha256_file(path),
        "checks": checks,
        "failed": failures,
    }


def load_public_zip_contract(
    path: pathlib.Path,
    windows: dict[str, Any],
    macos: dict[str, Any],
    web_service: dict[str, Any],
) -> dict[str, Any]:
    if not path.is_file():
        raise AssertionError(f"missing artifact: {path.name}")

    checks: list[dict[str, Any]] = []
    with zipfile.ZipFile(path) as archive:
        names = set(archive.namelist())
        _, manifest = zip_json_by_suffix(archive, "site/manifest.json")
        artifacts = {
            item.get("id"): item
            for item in manifest.get("artifacts", [])
            if isinstance(item, dict)
        }
        for item in (windows, macos, web_service):
            manifest_item = artifacts.get(item["artifactId"]) or {}
            download_entry = f"site/downloads/{item['artifactName']}"
            add_check(
                checks,
                f"public release embeds {item['artifactId']}",
                download_entry in names,
                {"entry": download_entry},
            )
            add_check(
                checks,
                f"public manifest matches {item['artifactId']}",
                manifest_item.get("size") == item["size"]
                and str(manifest_item.get("sha256") or "").upper() == item["sha256"],
                {
                    "manifestSize": manifest_item.get("size"),
                    "artifactSize": item["size"],
                    "hashMatches": str(manifest_item.get("sha256") or "").upper() == item["sha256"],
                },
            )

    failures = [item for item in checks if item["status"] != "PASS"]
    return {
        "artifactId": "public-release",
        "artifactName": path.name,
        "size": path.stat().st_size,
        "sha256": sha256_file(path),
        "checks": checks,
        "failed": failures,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--release-dir", default="release-artifacts")
    parser.add_argument("--version", default="0.2.4")
    parser.add_argument("--native-output", default="docs/v0.2.4/artifacts/native-skill-facades-release-contract.json")
    parser.add_argument("--skill-output", default="docs/v0.2.4/artifacts/skill-governance-release-contract.json")
    parser.add_argument("--tongxin-output", default="docs/v0.2.4/artifacts/tongxin-cli-release-contract.json")
    parser.add_argument("--lark-output", default="docs/v0.2.4/artifacts/feishu-lark-oapi-release-artifact-contract.json")
    parser.add_argument("--office-output", default="docs/v0.2.4/artifacts/office-pdf-runtime-release-contract.json")
    args = parser.parse_args()

    release_dir = pathlib.Path(args.release_dir)
    artifacts = {
        platform: inspect_webui_artifact(
            release_dir / info["name"],
            info["id"],
            info["install_suffix"],
        )
        for platform, info in WEBUI_ARTIFACTS.items()
    }
    web_service = inspect_web_service_artifact(
        release_dir / WEB_SERVICE_ARTIFACT["name"],
        WEB_SERVICE_ARTIFACT["id"],
    )
    public = load_public_zip_contract(
        release_dir / f"EcoreX_{args.version}-public-release.zip",
        artifacts["windows"],
        artifacts["macos"],
        web_service,
    )

    generated_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    native_checks = []
    skill_checks = []
    tongxin_checks = []
    lark_checks = []
    office_checks = []
    for platform_payload in artifacts.values():
        native_checks.extend(
            item for item in platform_payload["checks"]
            if "facade" in item["label"]
            or "skill API" in item["label"]
            or "prompt exposes" in item["label"]
            or "callable tool" in item["label"]
        )
        skill_checks.extend(
            item for item in platform_payload["checks"]
            if "Skill governance" in item["label"]
        )
        tongxin_checks.extend(
            item for item in platform_payload["checks"]
            if "Tongxin CLI" in item["label"]
        )
        lark_checks.extend(
            item for item in platform_payload["checks"]
            if "Lark" in item["label"]
            or "lark" in item["label"]
            or "Feishu" in item["label"]
            or "legacy raw" in item["label"]
            or "installer actively" in item["label"]
        )
        office_checks.extend(
            item for item in platform_payload["checks"]
            if "Office/PDF" in item["label"]
            or "office-pdf" in item["label"]
            or "Image structural QA" in item["label"]
            or "core runtime declares Office/PDF" in item["label"]
        )
    office_checks.extend(web_service["checks"])
    native_checks.extend(public["checks"])
    skill_checks.extend(public["checks"])
    tongxin_checks.extend(public["checks"])
    lark_checks.extend(public["checks"])
    office_checks.extend(public["checks"])

    native_failures = [item for item in native_checks if item["status"] != "PASS"]
    skill_failures = [item for item in skill_checks if item["status"] != "PASS"]
    tongxin_failures = [item for item in tongxin_checks if item["status"] != "PASS"]
    lark_failures = [item for item in lark_checks if item["status"] != "PASS"]
    office_failures = [item for item in office_checks if item["status"] != "PASS"]
    native_payload = {
        "status": "PASS" if not native_failures else "FAIL",
        "generatedAt": generated_at,
        "scope": "R24-01 webui release native skill facade contract",
        "windows": {key: artifacts["windows"][key] for key in ("artifactId", "artifactName", "size", "sha256")},
        "macos": {key: artifacts["macos"][key] for key in ("artifactId", "artifactName", "size", "sha256")},
        "publicRelease": {key: public[key] for key in ("artifactId", "artifactName", "size", "sha256")},
        "checks": native_checks,
        "failed": native_failures,
        "redacted": True,
    }
    lark_payload = {
        "status": "PASS" if not lark_failures else "FAIL",
        "generatedAt": generated_at,
        "scope": "R24-02A webui release lark runtime contract",
        "windows": {key: artifacts["windows"][key] for key in ("artifactId", "artifactName", "size", "sha256")},
        "macos": {key: artifacts["macos"][key] for key in ("artifactId", "artifactName", "size", "sha256")},
        "publicRelease": {key: public[key] for key in ("artifactId", "artifactName", "size", "sha256")},
        "checks": lark_checks,
        "failed": lark_failures,
        "redacted": True,
    }
    skill_payload = {
        "status": "PASS" if not skill_failures else "FAIL",
        "generatedAt": generated_at,
        "scope": "R24-01B webui release skill governance contract",
        "windows": {key: artifacts["windows"][key] for key in ("artifactId", "artifactName", "size", "sha256")},
        "macos": {key: artifacts["macos"][key] for key in ("artifactId", "artifactName", "size", "sha256")},
        "publicRelease": {key: public[key] for key in ("artifactId", "artifactName", "size", "sha256")},
        "checks": skill_checks,
        "failed": skill_failures,
        "redacted": True,
    }
    tongxin_payload = {
        "status": "PASS" if not tongxin_failures else "FAIL",
        "generatedAt": generated_at,
        "scope": "R24-02 webui release Tongxin CLI read-only contract",
        "windows": {key: artifacts["windows"][key] for key in ("artifactId", "artifactName", "size", "sha256")},
        "macos": {key: artifacts["macos"][key] for key in ("artifactId", "artifactName", "size", "sha256")},
        "publicRelease": {key: public[key] for key in ("artifactId", "artifactName", "size", "sha256")},
        "checks": tongxin_checks,
        "failed": tongxin_failures,
        "redacted": True,
    }
    office_payload = {
        "status": "PASS" if not office_failures else "FAIL",
        "generatedAt": generated_at,
        "scope": "R24-04 webui release Office/PDF runtime contract",
        "windows": {key: artifacts["windows"][key] for key in ("artifactId", "artifactName", "size", "sha256")},
        "macos": {key: artifacts["macos"][key] for key in ("artifactId", "artifactName", "size", "sha256")},
        "webLinuxService": {key: web_service[key] for key in ("artifactId", "artifactName", "size", "sha256")},
        "publicRelease": {key: public[key] for key in ("artifactId", "artifactName", "size", "sha256")},
        "checks": office_checks,
        "failed": office_failures,
        "redacted": True,
    }

    for output, payload in (
        (pathlib.Path(args.native_output), native_payload),
        (pathlib.Path(args.skill_output), skill_payload),
        (pathlib.Path(args.tongxin_output), tongxin_payload),
        (pathlib.Path(args.lark_output), lark_payload),
        (pathlib.Path(args.office_output), office_payload),
    ):
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    overall_ok = (
        native_payload["status"] == "PASS"
        and lark_payload["status"] == "PASS"
        and skill_payload["status"] == "PASS"
        and tongxin_payload["status"] == "PASS"
        and office_payload["status"] == "PASS"
    )
    print(json.dumps({
        "status": "PASS" if overall_ok else "FAIL",
        "native": native_payload["status"],
        "skillGovernance": skill_payload["status"],
        "tongxin": tongxin_payload["status"],
        "lark": lark_payload["status"],
        "officePdf": office_payload["status"],
        "redacted": True,
    }, ensure_ascii=False, indent=2))
    return 0 if overall_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
