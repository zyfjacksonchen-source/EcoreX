#!/usr/bin/env python3
"""Validate local EcoreX release artifacts against the download manifest."""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import re
import subprocess
import sys
import tarfile
import zipfile


PUBLISHABLE_STATUSES = {"ready"}
MACOS_UNSIGNED_STATUS = "ready-unsigned"
REQUIRED_AUTH_NEGATIVE_STATUSES = {
    "messageNoToken",
    "messageWrongToken",
    "messageQueryTokenRejected",
    "streamNoToken",
    "streamWrongToken",
    "streamQueryTokenRejected",
    "fileStatNoToken",
    "fileStatWrongToken",
    "fileServeNoToken",
    "fileServeWrongToken",
    "openPathNoToken",
    "openPathWrongToken",
}
FORBIDDEN_WEB_ASSETS = (
    "index-dSHNqlZq.js",
    "index-DBjPv6j0.css",
    "index-C30Hbyh1.js",
    "index-Crsnr3ve.js",
    "index-BTdIth7N.js",
    "index-d9-YMgAJ.js",
    "index-DntImxX6.js",
    "index-DMhz52Zy.js",
    "index-CjBkNLMl.js",
    "index-B_LYG2V7.js",
    "index-CcCofcc7.js",
    "index-vjxmFxDP.js",
    "index-DGSZjdR_.js",
    "index-D7oCsug3.css",
)
REQUIRED_SITE_ASSETS = (
    "site/assets/icon.png",
    "site/assets/ecorex-app-preview.png",
    "site/assets/ecorex-ecosystem-hub.png",
)
REQUIRED_RUNTIME_SUFFIXES = (
    "runtime/config.py",
    "runtime/config-template.json",
    "runtime/channel/web/web_channel.py",
    "runtime/agent/protocol/cancel.py",
    "runtime/agent/protocol/agent_stream.py",
    "runtime/agent/skills/formatter.py",
    "runtime/agent/skills/loader.py",
    "runtime/agent/skills/manager.py",
    "runtime/agent/tools/base_tool.py",
    "runtime/agent/tools/tool_manager.py",
    "runtime/agent/tools/__init__.py",
    "runtime/common/ecorex_release_notes.py",
    "runtime/common/ecorex_tool_permissions.py",
    "runtime/agent/tools/read/read.py",
    "runtime/agent/tools/find/find.py",
    "runtime/agent/tools/ls/ls.py",
    "runtime/agent/tools/write/write.py",
    "runtime/agent/tools/edit/edit.py",
    "runtime/agent/tools/send/send.py",
    "runtime/agent/tools/ecorex_cli/ecorex_cli.py",
    "runtime/agent/tools/feishu_cli/feishu_cli.py",
    "runtime/agent/tools/host_diagnostics/host_diagnostics.py",
    "runtime/agent/tools/agent_capability/agent_capability.py",
    "runtime/agent/tools/subagent/subagent.py",
    "runtime/agent/tools/mcp/mcp_client.py",
    "runtime/agent/tools/mcp/mcp_tool.py",
    "runtime/agent/tools/web_fetch/web_fetch.py",
    "runtime/agent/tools/web_search/web_search.py",
    "runtime/agent/tools/vision/vision.py",
    "runtime/agent/tools/ocr/ocr.py",
    "runtime/agent/tools/imagegen/imagegen.py",
    "runtime/agent/tools/browser/browser_tool.py",
    "runtime/agent/tools/browser/browser_service.py",
    "runtime/agent/tools/browser/browser_automation_service.py",
    "runtime/agent/memory/summarizer.py",
    "runtime/agent/knowledge/service.py",
    "runtime/skills/image-generation/scripts/generate.py",
    "runtime/skills/find/SKILL.md",
    "runtime/skills/skill-creator/SKILL.md",
    "runtime/skills/skill-creator/scripts/init_skill.py",
    "runtime/skills/skill-creator/scripts/quick_validate.py",
)
REQUIRED_WEBUI_RUNTIME_SUFFIXES = (
    "runtime/common/office_pdf_runtime.py",
    "runtime/common/image_quality_runtime.py",
)
REQUIRED_DESKTOP_RUNTIME_FILES = (
    "config.py",
    "config-template.json",
    "channel/web/web_channel.py",
    "agent/protocol/cancel.py",
    "agent/protocol/agent_stream.py",
    "agent/skills/formatter.py",
    "agent/skills/loader.py",
    "agent/skills/manager.py",
    "agent/tools/base_tool.py",
    "agent/tools/tool_manager.py",
    "agent/tools/__init__.py",
    "common/office_pdf_runtime.py",
    "common/image_quality_runtime.py",
    "common/ecorex_release_notes.py",
    "common/ecorex_tool_permissions.py",
    "agent/tools/read/read.py",
    "agent/tools/find/find.py",
    "agent/tools/ls/ls.py",
    "agent/tools/write/write.py",
    "agent/tools/edit/edit.py",
    "agent/tools/send/send.py",
    "agent/tools/ecorex_cli/ecorex_cli.py",
    "agent/tools/feishu_cli/feishu_cli.py",
    "agent/tools/host_diagnostics/host_diagnostics.py",
    "agent/tools/agent_capability/agent_capability.py",
    "agent/tools/subagent/subagent.py",
    "agent/tools/mcp/mcp_client.py",
    "agent/tools/mcp/mcp_tool.py",
    "agent/tools/web_fetch/web_fetch.py",
    "agent/tools/web_search/web_search.py",
    "agent/tools/vision/vision.py",
    "agent/tools/ocr/ocr.py",
    "agent/tools/imagegen/imagegen.py",
    "agent/tools/browser/browser_tool.py",
    "agent/tools/browser/browser_service.py",
    "agent/tools/browser/browser_automation_service.py",
    "agent/memory/summarizer.py",
    "agent/knowledge/service.py",
    "skills/image-generation/scripts/generate.py",
    "skills/find/SKILL.md",
    "skills/skill-creator/SKILL.md",
    "skills/skill-creator/scripts/init_skill.py",
    "skills/skill-creator/scripts/quick_validate.py",
)


def _s(*parts: str) -> str:
    return "".join(parts)


_LEGACY_AGENT_MIXED = _s("Cow", "Agent")
_LEGACY_AGENT_UPPER = _LEGACY_AGENT_MIXED.upper()
_LEGACY_AGENT_LOWER = _LEGACY_AGENT_MIXED.lower()
_LEGACY_AUTHOR = _s("zha", "yuj", "ie")
_LEGACY_CHAT_DASH = _s("chat", "gpt", "-on-", "we", "chat")
_LEGACY_CHAT_UNDERSCORE = _LEGACY_CHAT_DASH.replace("-", "_")
_LEGACY_CHAT_UPPER = _LEGACY_CHAT_DASH.upper()
_LEGACY_CLI_PLUGIN_SNAKE = _s("cow", "_cli")
_LEGACY_CLI_PLUGIN_CAMEL = _s("Cow", "Cli")
_LEGACY_CLI_PLUGIN_UPPER = _s("COW", "_CLI")
_LEGACY_CLI_PRODUCT = _s("Cow", " CLI")
_LEGACY_FORBIDDEN_RELEASE_TEXT = (
    _LEGACY_AGENT_MIXED,
    _LEGACY_AGENT_UPPER,
    _LEGACY_AGENT_LOWER,
    _s("C:", "\\", _LEGACY_AGENT_MIXED),
    _s("C:/", _LEGACY_AGENT_MIXED),
    _LEGACY_AUTHOR,
    _LEGACY_CHAT_DASH,
    _LEGACY_CHAT_UNDERSCORE,
    _LEGACY_CHAT_UPPER,
    _LEGACY_CLI_PLUGIN_SNAKE,
    _LEGACY_CLI_PLUGIN_CAMEL,
    _LEGACY_CLI_PLUGIN_UPPER,
    _LEGACY_CLI_PRODUCT,
)
FORBIDDEN_RELEASE_PATTERN = re.compile("|".join(re.escape(item) for item in _LEGACY_FORBIDDEN_RELEASE_TEXT))
MIGRATION_README_NAMES = {"README.txt", "README-migration.txt"}
MIGRATION_README_REQUIRED_FRAGMENTS = (
    _s("The original v0.1.0 project name was ", _LEGACY_AGENT_MIXED, " and has been renamed to EcoreX."),
    _s(_LEGACY_AGENT_MIXED, " is a historical project name / development stack name only. It does not indicate plagiarism, copying, or third-party ownership."),
    _s("原始 v0.1.0 版本项目名 ", _LEGACY_AGENT_MIXED, " 已改名为 EcoreX；", _LEGACY_AGENT_MIXED, " 是历史项目名称/开发栈名称，不代表抄袭或第三方归属。"),
)
TEXT_ARCHIVE_SUFFIXES = {
    ".cfg",
    ".css",
    ".html",
    ".ini",
    ".js",
    ".json",
    ".md",
    ".mjs",
    ".plist",
    ".ps1",
    ".py",
    ".sh",
    ".toml",
    ".txt",
    ".xml",
    ".yaml",
    ".yml",
}
TEXT_ARCHIVE_NAMES = {"LICENSE", "README", "README.txt", "runtime-manifest.json"}


class ValidationError(Exception):
    pass


def read_json_no_bom(path: pathlib.Path) -> dict:
    raw = path.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        raise ValidationError(f"{path} has a UTF-8 BOM")
    return json.loads(raw.decode("utf-8"))


def read_zip_json_no_bom(archive: zipfile.ZipFile, name: str) -> dict:
    raw = archive.read(name)
    if raw.startswith(b"\xef\xbb\xbf"):
        raise ValidationError(f"{archive.filename}!{name} has a UTF-8 BOM")
    return json.loads(raw.decode("utf-8"))


def sha256_file(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest().upper()


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValidationError(message)


def is_text_archive_name(name: str) -> bool:
    base = pathlib.PurePosixPath(name).name
    return base in TEXT_ARCHIVE_NAMES or pathlib.PurePosixPath(name).suffix.lower() in TEXT_ARCHIVE_SUFFIXES


def is_allowed_migration_readme(name: str, text: str) -> bool:
    base = pathlib.PurePosixPath(name).name
    if base not in MIGRATION_README_NAMES:
        return False
    remaining = text
    for fragment in MIGRATION_README_REQUIRED_FRAGMENTS:
        if fragment not in remaining:
            return False
        remaining = remaining.replace(fragment, "")
    return FORBIDDEN_RELEASE_PATTERN.search(remaining) is None


def require_no_forbidden_release_text(
    label: str,
    name: str,
    text: str,
    *,
    allow_migration_readme: bool = False,
) -> None:
    if allow_migration_readme and is_allowed_migration_readme(name, text):
        return
    match = FORBIDDEN_RELEASE_PATTERN.search(text)
    if match is not None:
        raise ValidationError(f"{label} contains forbidden legacy/private string {match.group(0)!r} in {name}")


def validate_zip_no_forbidden_release_strings(archive: zipfile.ZipFile, label: str) -> None:
    for name in archive.namelist():
        match = FORBIDDEN_RELEASE_PATTERN.search(name)
        if match is not None:
            raise ValidationError(f"{label} contains forbidden legacy/private string {match.group(0)!r} in path {name}")
        if not is_text_archive_name(name):
            continue
        try:
            text = archive.read(name).decode("utf-8", errors="replace")
        except Exception:
            continue
        require_no_forbidden_release_text(label, name, text)


def validate_tar_no_forbidden_release_strings(archive: tarfile.TarFile, label: str) -> None:
    for member in archive.getmembers():
        name = member.name
        match = FORBIDDEN_RELEASE_PATTERN.search(name)
        if match is not None:
            raise ValidationError(f"{label} contains forbidden legacy/private string {match.group(0)!r} in path {name}")
        if not member.isfile() or not is_text_archive_name(name):
            continue
        handle = archive.extractfile(member)
        if handle is None:
            continue
        text = handle.read().decode("utf-8", errors="replace")
        require_no_forbidden_release_text(label, name, text)


def validate_directory_no_forbidden_release_strings(root: pathlib.Path, label: str) -> None:
    for path in root.rglob("*"):
        rel = path.relative_to(root).as_posix()
        match = FORBIDDEN_RELEASE_PATTERN.search(rel)
        if match is not None:
            raise ValidationError(f"{label} contains forbidden legacy/private string {match.group(0)!r} in path {rel}")
        if not path.is_file() or not is_text_archive_name(rel):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        require_no_forbidden_release_text(label, rel, text)


def artifact_path(artifact_dir: pathlib.Path, artifact: dict) -> pathlib.Path:
    return artifact_dir / str(artifact["fileName"])


def artifact_href(artifact: dict) -> str:
    return str(artifact.get("href") or "")


def is_external_artifact(artifact: dict) -> bool:
    href = artifact_href(artifact).lower()
    return bool(artifact.get("external")) or href.startswith(("http://", "https://"))


def is_macos_artifact(artifact_id: str) -> bool:
    return artifact_id.startswith("macos-")


def is_publishable_artifact(artifact: dict) -> bool:
    artifact_id = str(artifact.get("id") or artifact.get("fileName") or "")
    status = str(artifact.get("status") or "")
    if status == "ready" and is_macos_artifact(artifact_id) and str(artifact.get("signature") or "").lower() == "unsigned":
        raise ValidationError(f"{artifact_id} unsigned macOS artifact must use status=ready-unsigned with installSmoke evidence")
    if status in PUBLISHABLE_STATUSES:
        return True
    if status == MACOS_UNSIGNED_STATUS and is_macos_artifact(artifact_id):
        return str(artifact.get("signature") or "").lower() == "unsigned"
    return False


def validate_macos_unsigned_install_smoke(manifest: dict, artifact_id: str, artifact: dict) -> None:
    smoke = artifact.get("installSmoke") or artifact.get("install_smoke")
    require(isinstance(smoke, dict), f"{artifact_id} ready-unsigned requires installSmoke evidence")
    require(str(smoke.get("status") or "").lower() == "pass", f"{artifact_id} installSmoke.status must be pass")
    expected_version = str(manifest.get("version") or "")
    require(str(smoke.get("version") or "") == expected_version, f"{artifact_id} installSmoke.version must be {expected_version}")
    expected_digest = str(artifact.get("sha256") or "").upper()
    require(str(smoke.get("sha256") or "").upper() == expected_digest, f"{artifact_id} installSmoke.sha256 must match artifact SHA256")
    evidence = smoke.get("runId") or smoke.get("run_id") or smoke.get("evidenceUrl") or smoke.get("evidence_url") or smoke.get("evidence")
    require(bool(str(evidence or "").strip()), f"{artifact_id} installSmoke requires runId or evidenceUrl")
    arch = {"macos-arm64-dmg": "arm64", "macos-x64-dmg": "x64"}.get(artifact_id)
    if arch:
        expected_file = f"EcoreX_{expected_version}_{arch}.dmg"
        require(str(artifact.get("fileName") or "") == expected_file, f"{artifact_id} fileName must be {expected_file}")
        require(str(smoke.get("artifact") or "") == expected_file, f"{artifact_id} installSmoke.artifact must be {expected_file}")
        require(str(smoke.get("arch") or "") == arch, f"{artifact_id} installSmoke.arch must be {arch}")
        require(int(smoke.get("bytes") or 0) == int(artifact.get("size") or 0), f"{artifact_id} installSmoke.bytes must match artifact size")
    required_true = [
        "mounted",
        "appFound",
        "copied",
        "launched",
        "versionOk",
        "sidecarReady",
        "authReady",
        "authRequired",
        "authNegativeReady",
        "gatekeeperInstructionShown",
    ]
    missing = [key for key in required_true if not smoke.get(key)]
    require(not missing, f"{artifact_id} installSmoke missing passed flags: {', '.join(missing)}")
    validate_auth_negative_statuses(smoke, f"{artifact_id} installSmoke")
    instructions = smoke.get("gatekeeperInstructions") or smoke.get("instructions") or smoke.get("instructionsUrl") or smoke.get("instructions_url")
    require(bool(str(instructions or "").strip()), f"{artifact_id} installSmoke requires Gatekeeper instructions evidence")


def validate_auth_negative_statuses(smoke: dict, evidence_name: str) -> None:
    negative = smoke.get("authNegativeStatuses")
    require(isinstance(negative, dict), f"{evidence_name} requires authNegativeStatuses")
    missing = sorted(REQUIRED_AUTH_NEGATIVE_STATUSES - set(negative))
    bad = sorted(f"{key}={negative.get(key)}" for key in REQUIRED_AUTH_NEGATIVE_STATUSES & set(negative) if int(negative.get(key) or 0) != 401)
    require(not missing and not bad, f"{evidence_name} authNegativeStatuses invalid missing={missing} bad={bad}")


def validate_windows_installed_smoke(manifest: dict, artifact: dict) -> None:
    version = str(manifest.get("version") or "")
    artifact_id = str(artifact.get("id") or "windows-x64")
    smoke_file = "win-ia32-installed-smoke.json" if artifact_id == "windows-ia32" else "win-installed-smoke.json"
    smoke_path = pathlib.Path("docs") / f"v{version}" / smoke_file
    smoke = read_json_no_bom(smoke_path)
    required_true = [
        "installed",
        "appStarted",
        "sidecarReady",
        "authReady",
        "authRequired",
        "authNegativeReady",
        "cleaned",
    ]
    missing = [key for key in required_true if not smoke.get(key)]
    require(not missing, f"{artifact_id} installed smoke missing passed flags: {', '.join(missing)}")
    require(str(smoke.get("runtimeVersion") or "") == version, f"{artifact_id} installed smoke runtimeVersion must be {version}")
    expected_win_arch = "ia32" if artifact_id == "windows-ia32" else "x64"
    expected_python_bits = 32 if artifact_id == "windows-ia32" else 64
    require(str(smoke.get("runtimeWinArch") or "") == expected_win_arch, f"{artifact_id} installed smoke runtimeWinArch must be {expected_win_arch}")
    require(int(smoke.get("runtimePythonBits") or 0) == expected_python_bits, f"{artifact_id} installed smoke runtimePythonBits must be {expected_python_bits}")
    require(str(smoke.get("installerFileName") or "") == str(artifact.get("fileName") or ""), f"{artifact_id} installed smoke fileName must match manifest")
    require(str(smoke.get("installerSha256") or "").upper() == str(artifact.get("sha256") or "").upper(), f"{artifact_id} installed smoke sha256 must match manifest")
    require(int(smoke.get("installerSize") or 0) == int(artifact.get("size") or 0), f"{artifact_id} installed smoke size must match manifest")
    for key in ("installerSignatureStatus", "appSignatureStatus", "runtimePythonSignatureStatus"):
        require(str(smoke.get(key) or "") == "Valid", f"{artifact_id} installed smoke requires {key}=Valid")
    validate_auth_negative_statuses(smoke, f"{artifact_id} installed smoke")


def validate_external_artifact_metadata(artifact_id: str, artifact: dict) -> None:
    href = artifact_href(artifact)
    expected_size = int(artifact.get("size") or 0)
    expected_digest = str(artifact.get("sha256") or "").upper()
    require(href.lower().startswith(("http://", "https://")), f"external artifact {artifact_id} href is not HTTP(S)")
    require(str(artifact.get("fileName") or ""), f"external artifact {artifact_id} missing fileName")
    require(expected_size > 0, f"external artifact {artifact_id} missing positive size")
    require(bool(re.fullmatch(r"[A-F0-9]{64}", expected_digest)), f"external artifact {artifact_id} missing SHA256")


def validate_manifest_artifacts(manifest: dict, artifact_dir: pathlib.Path) -> list[dict]:
    ready = []
    for artifact in manifest.get("artifacts") or []:
        artifact_id = str(artifact.get("id") or artifact.get("fileName") or "unknown")
        status = str(artifact.get("status") or "")
        if not is_publishable_artifact(artifact):
            print(f"SKIP artifact {artifact_id} status={status}")
            continue
        if artifact_id.startswith("windows-"):
            signature = str(artifact.get("signature") or "").lower()
            require(signature == "valid", f"{artifact_id} publishable artifact requires signature=Valid")
            validate_windows_installed_smoke(manifest, artifact)
        if status == MACOS_UNSIGNED_STATUS:
            require(is_macos_artifact(artifact_id), f"{status} is only allowed for macOS artifacts")
            require(str(artifact.get("signature") or "").lower() == "unsigned", f"{artifact_id} ready-unsigned requires signature=unsigned")
            validate_macos_unsigned_install_smoke(manifest, artifact_id, artifact)
        if is_external_artifact(artifact):
            validate_external_artifact_metadata(artifact_id, artifact)
            ready.append(artifact)
            print(f"PASS external artifact {artifact_id} {artifact_href(artifact)}")
            continue
        path = artifact_path(artifact_dir, artifact)
        require(path.is_file(), f"ready artifact {artifact_id} missing: {path}")
        size = path.stat().st_size
        expected_size = int(artifact.get("size") or 0)
        require(size == expected_size, f"artifact {artifact_id} size {size} != manifest {expected_size}")
        digest = sha256_file(path)
        expected_digest = str(artifact.get("sha256") or "").upper()
        require(digest == expected_digest, f"artifact {artifact_id} sha256 {digest} != manifest {expected_digest}")
        ready.append(artifact)
        print(f"PASS artifact {artifact_id} {path.name}")
    require(ready, "manifest has no ready artifacts")
    return ready


def validate_zip_assets(path: pathlib.Path, label: str) -> None:
    with zipfile.ZipFile(path) as archive:
        validate_zip_no_forbidden_release_strings(archive, label)
        names = archive.namelist()
        if label in {"webui-win-mac", "webui-macos-universal"}:
            require(
                any(name.endswith("Install EcoreX WebUI.app/Contents/MacOS/Install EcoreX WebUI") for name in names),
                f"{label} missing macOS .app installer entrypoint",
            )
            require(
                not any(name.endswith("Install EcoreX WebUI.command") for name in names),
                f"{label} contains terminal-opening macOS .command installer",
            )
            require(
                any("/wheelhouse/mac-arm64/playwright-" in name for name in names),
                f"{label} missing macOS arm64 Playwright wheel",
            )
            require(
                any("/wheelhouse/mac-x64/playwright-" in name for name in names),
                f"{label} missing macOS x64 Playwright wheel",
            )
        for required in REQUIRED_RUNTIME_SUFFIXES + REQUIRED_WEBUI_RUNTIME_SUFFIXES:
            require(any(name.endswith(required) for name in names), f"{label} missing {required}")
        for forbidden in FORBIDDEN_WEB_ASSETS:
            require(not any(name.endswith(forbidden) for name in names), f"{label} contains stale asset {forbidden}")
        validate_runtime_source_texts(lambda suffix: zip_text_by_suffix(archive, f"runtime/{suffix}"), label)
        validate_frontend_bundle_texts(lambda suffix: zip_text_by_suffix(archive, f"runtime/{suffix}"), label)
    print(f"PASS zip runtime/assets {label}")


def validate_tar_assets(path: pathlib.Path, label: str) -> None:
    with tarfile.open(path, "r:gz") as archive:
        validate_tar_no_forbidden_release_strings(archive, label)
        names = archive.getnames()
        if label == "webui-macos-universal":
            require(
                any(name.endswith("Install EcoreX WebUI.app/Contents/MacOS/Install EcoreX WebUI") for name in names),
                f"{label} missing macOS .app installer entrypoint",
            )
            require(
                not any(name.endswith("Install EcoreX WebUI.command") for name in names),
                f"{label} contains terminal-opening macOS .command installer",
            )
            require(
                any("/wheelhouse/mac-arm64/playwright-" in name for name in names),
                f"{label} missing macOS arm64 Playwright wheel",
            )
            require(
                any("/wheelhouse/mac-x64/playwright-" in name for name in names),
                f"{label} missing macOS x64 Playwright wheel",
            )
        for required in REQUIRED_RUNTIME_SUFFIXES:
            require(any(name.endswith(required) for name in names), f"{label} missing {required}")
        for forbidden in FORBIDDEN_WEB_ASSETS:
            require(not any(name.endswith(forbidden) for name in names), f"{label} contains stale asset {forbidden}")
        validate_runtime_source_texts(lambda suffix: tar_text_by_suffix(archive, f"runtime/{suffix}"), label)
        validate_frontend_bundle_texts(lambda suffix: tar_text_by_suffix(archive, f"runtime/{suffix}"), label)
    print(f"PASS tar runtime/assets {label}")


def validate_nested_web_assets(artifact_dir: pathlib.Path, ready: list[dict]) -> None:
    for artifact in ready:
        if is_external_artifact(artifact):
            continue
        artifact_id = str(artifact.get("id") or "")
        path = artifact_path(artifact_dir, artifact)
        if artifact_id in {"webui-win-mac", "webui-windows-x64", "webui-macos-universal"}:
            validate_zip_assets(path, artifact_id)
        elif artifact_id in {"web-linux-service"}:
            validate_tar_assets(path, artifact_id)


def validate_public_zip(
    public_zip: pathlib.Path,
    manifest: dict,
    ready: list[dict],
) -> None:
    require(public_zip.is_file(), f"public release zip missing: {public_zip}")
    with zipfile.ZipFile(public_zip) as archive:
        validate_zip_no_forbidden_release_strings(archive, "public release zip")
        names = set(archive.namelist())
        for required in (
            "README.txt",
            "site/index.html",
            "site/manifest.json",
            "site/admin/index.html",
            "admin-api/ecorex_admin_api.py",
            "server/install-ecorex-public-release.sh",
            "server/check-ecorex-server-release.sh",
            "checksums.json",
        ):
            require(required in names, f"public zip missing {required}")
        for required in REQUIRED_SITE_ASSETS:
            require(required in names, f"public zip missing image asset {required}")
            require(archive.getinfo(required).file_size > 0, f"public zip image asset is empty: {required}")
        public_manifest = read_zip_json_no_bom(archive, "site/manifest.json")
        site_js = archive.read("site/site.js").decode("utf-8", errors="replace")
        require_contains(
            site_js,
            "install-webui.ps1",
            "public site WebUI install command",
        )
        require_contains(
            site_js,
            "webui-windows-x64",
            "public site WebUI Windows artifact",
        )
        require_contains(
            site_js,
            "webui-macos-universal",
            "public site WebUI macOS artifact",
        )
        install_webui_ps1 = archive.read("site/install-webui.ps1").decode("utf-8", errors="replace")
        require_contains(
            install_webui_ps1,
            "function Try-SaveUrlWithCurl",
            "public Windows WebUI bootstrap curl download acceleration",
        )
        require_contains(
            install_webui_ps1,
            '"--continue-at", "-"',
            "public Windows WebUI bootstrap resumable curl download",
        )
        require_contains(
            install_webui_ps1,
            "function Expand-EcoreXZip",
            "public Windows WebUI bootstrap robust zip extraction",
        )
        require_contains(
            install_webui_ps1,
            "function ConvertTo-EcoreXLongPath",
            "public Windows WebUI bootstrap long path extraction support",
        )
        require_contains(
            install_webui_ps1,
            "$source.CopyTo($target)",
            "public Windows WebUI bootstrap stream-copy extraction",
        )
        require_contains(
            install_webui_ps1,
            "Blocked unsafe zip entry",
            "public Windows WebUI bootstrap zip-slip guard",
        )
        require_not_contains(
            install_webui_ps1,
            "Expand-Archive",
            "public Windows WebUI bootstrap avoids PowerShell Archive cleanup race",
        )
        require_not_contains(
            site_js,
            '"windows-x64"',
            "public site retired desktop Windows card",
        )
        checksums = read_zip_json_no_bom(archive, "checksums.json")
        require(public_manifest.get("version") == manifest.get("version"), "public manifest version mismatch")
        checksum_artifacts = checksums.get("artifacts") or {}
        ready_by_id = {str(item.get("id")): item for item in ready}
        require(set(checksum_artifacts) == set(ready_by_id), "checksums.json ready artifact ids mismatch")

        public_ready_by_id = {
            str(item.get("id")): item
            for item in (public_manifest.get("artifacts") or [])
            if is_publishable_artifact(item)
        }
        require(set(public_ready_by_id) == set(ready_by_id), "public manifest ready artifact ids mismatch")
        for artifact_id, artifact in ready_by_id.items():
            public_artifact = public_ready_by_id[artifact_id]
            for key in ("fileName", "status"):
                require(
                    str(public_artifact.get(key) or "") == str(artifact.get(key) or ""),
                    f"public manifest {artifact_id} {key} mismatch",
                )
            for key in ("size",):
                require(
                    int(public_artifact.get(key) or 0) == int(artifact.get(key) or 0),
                    f"public manifest {artifact_id} {key} mismatch",
                )
            require(
                str(public_artifact.get("sha256") or "").upper() == str(artifact.get("sha256") or "").upper(),
                f"public manifest {artifact_id} sha256 mismatch",
            )

        download_files = {
            name.removeprefix("site/downloads/")
            for name in names
            if name.startswith("site/downloads/") and not name.endswith("/")
        }
        expected_download_files = {str(item.get("fileName")) for item in ready if not is_external_artifact(item)}
        windows_ready = next((item for item in ready if str(item.get("id") or "") == "windows-x64" and not is_external_artifact(item)), None)
        if windows_ready:
            installer_name = str(windows_ready.get("fileName") or "")
            expected_download_files.update({"latest.yml", f"{installer_name}.blockmap"})
        windows_ia32_ready = next((item for item in ready if str(item.get("id") or "") == "windows-ia32" and not is_external_artifact(item)), None)
        if windows_ia32_ready:
            installer_name = str(windows_ia32_ready.get("fileName") or "")
            expected_download_files.update({f"ia32/{installer_name}", "ia32/latest.yml", f"ia32/{installer_name}.blockmap"})
        require(download_files == expected_download_files, "public zip download file set mismatch")

        for artifact_id, artifact in ready_by_id.items():
            public_artifact = public_ready_by_id[artifact_id]
            checksum = checksum_artifacts[artifact_id]
            expected_size = int(artifact.get("size") or 0)
            expected_digest = str(artifact.get("sha256") or "").upper()
            if is_external_artifact(artifact):
                validate_external_artifact_metadata(artifact_id, artifact)
                href = artifact_href(artifact)
                require(
                    str(public_artifact.get("href") or "") == href,
                    f"public manifest {artifact_id} href mismatch",
                )
                require(
                    bool(checksum.get("external")) or str(checksum.get("relativePath") or "").lower().startswith(("http://", "https://")),
                    f"checksums external marker missing for {artifact_id}",
                )
                require(str(checksum.get("relativePath") or "") == href, f"checksums href mismatch for {artifact_id}")
                require(int(checksum.get("size") or 0) == expected_size, f"checksums size mismatch for {artifact_id}")
                require(str(checksum.get("sha256") or "").upper() == expected_digest, f"checksums sha mismatch for {artifact_id}")
                continue

            file_name = str(artifact["fileName"])
            rel = f"site/downloads/{file_name}"
            require(rel in names, f"public zip missing ready download {rel}")
            payload = archive.read(rel)
            require(len(payload) == expected_size, f"public zip {artifact_id} size mismatch")
            require(sha256_bytes(payload) == expected_digest, f"public zip {artifact_id} sha256 mismatch")

            require(int(checksum.get("size") or 0) == expected_size, f"checksums size mismatch for {artifact_id}")
            require(str(checksum.get("sha256") or "").upper() == expected_digest, f"checksums sha mismatch for {artifact_id}")
        if windows_ready:
            installer_name = str(windows_ready.get("fileName") or "")
            installer_size = int(windows_ready.get("size") or 0)
            latest_rel = "site/downloads/latest.yml"
            blockmap_rel = f"site/downloads/{installer_name}.blockmap"
            require(latest_rel in names, "public zip missing Windows latest.yml")
            require(blockmap_rel in names, "public zip missing Windows installer blockmap")
            latest_text = archive.read(latest_rel).decode("utf-8")
            require(f"version: {manifest.get('version')}" in latest_text, "latest.yml version mismatch")
            require(f"url: {installer_name}" in latest_text, "latest.yml installer url mismatch")
            require(f"path: {installer_name}" in latest_text, "latest.yml installer path mismatch")
            require(f"size: {installer_size}" in latest_text, "latest.yml installer size mismatch")
            require(len(archive.read(blockmap_rel)) > 0, "Windows installer blockmap is empty")
        if windows_ia32_ready:
            installer_name = str(windows_ia32_ready.get("fileName") or "")
            installer_size = int(windows_ia32_ready.get("size") or 0)
            latest_rel = "site/downloads/ia32/latest.yml"
            installer_rel = f"site/downloads/ia32/{installer_name}"
            blockmap_rel = f"site/downloads/ia32/{installer_name}.blockmap"
            require(latest_rel in names, "public zip missing Windows ia32 latest.yml")
            require(installer_rel in names, "public zip missing Windows ia32 updater installer")
            require(blockmap_rel in names, "public zip missing Windows ia32 installer blockmap")
            latest_text = archive.read(latest_rel).decode("utf-8")
            require(f"version: {manifest.get('version')}" in latest_text, "ia32 latest.yml version mismatch")
            require(f"url: {installer_name}" in latest_text, "ia32 latest.yml installer url mismatch")
            require(f"path: {installer_name}" in latest_text, "ia32 latest.yml installer path mismatch")
            require(f"size: {installer_size}" in latest_text, "ia32 latest.yml installer size mismatch")
            installer_payload = archive.read(installer_rel)
            require(len(installer_payload) == installer_size, "Windows ia32 updater installer size mismatch")
            require(sha256_bytes(installer_payload) == str(windows_ia32_ready.get("sha256") or "").upper(), "Windows ia32 updater installer sha256 mismatch")
            require(len(archive.read(blockmap_rel)) > 0, "Windows ia32 installer blockmap is empty")
    print(f"PASS public zip {public_zip.name}")


def extract_asar_text(asar_path: pathlib.Path, archive_name: str, node_modules: pathlib.Path) -> str:
    asar_module = (node_modules / "@electron" / "asar").resolve()
    require(asar_module.is_dir(), f"asar module missing: {asar_module}")
    script = (
        "const asar=require(process.argv[1]);"
        "const payload=asar.extractFile(process.argv[2], process.argv[3]);"
        "process.stdout.write(Buffer.isBuffer(payload)?payload:Buffer.from(String(payload)));"
    )
    candidates = [archive_name]
    if "/" in archive_name:
        candidates.append(archive_name.replace("/", "\\"))
    last_detail = ""
    for candidate in candidates:
        result = subprocess.run(
            ["node", "-e", script, str(asar_module), str(asar_path.resolve()), candidate],
            check=False,
            capture_output=True,
        )
        if result.returncode == 0:
            return result.stdout.decode("utf-8", errors="replace")
        last_detail = result.stderr.decode("utf-8", errors="replace").strip()
    raise ValidationError(f"failed extracting {archive_name} from {asar_path}: {last_detail}")


def list_asar_files(asar_path: pathlib.Path, node_modules: pathlib.Path) -> list[str]:
    asar_module = (node_modules / "@electron" / "asar").resolve()
    require(asar_module.is_dir(), f"asar module missing: {asar_module}")
    script = (
        "const asar=require(process.argv[1]);"
        "const filesystem=asar.listPackage(process.argv[2]);"
        "process.stdout.write(JSON.stringify(filesystem));"
    )
    result = subprocess.run(
        ["node", "-e", script, str(asar_module), str(asar_path.resolve())],
        check=False,
        capture_output=True,
    )
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise ValidationError(f"failed listing {asar_path}: {detail}")
    payload = json.loads(result.stdout.decode("utf-8"))
    if not isinstance(payload, list):
        raise ValidationError(f"asar file list was not an array for {asar_path}")
    return [str(item).replace("\\", "/").lstrip("/") for item in payload]


def extract_asar_renderer_bundle_text(asar_path: pathlib.Path, node_modules: pathlib.Path) -> str:
    index_text = extract_asar_text(asar_path, "dist/index.html", node_modules)
    js_name, css_name = referenced_frontend_assets(index_text, "desktop renderer index")
    extract_asar_text(asar_path, f"dist/assets/{css_name}", node_modules)
    return extract_asar_text(asar_path, f"dist/assets/{js_name}", node_modules)


def require_contains(text: str, needle: str, label: str) -> None:
    require(needle in text, f"{label} missing {needle!r}")


def require_not_contains(text: str, needle: str, label: str) -> None:
    require(needle not in text, f"{label} contains forbidden {needle!r}")


def require_regex(text: str, pattern: str, label: str) -> None:
    require(re.search(pattern, text) is not None, f"{label} missing pattern {pattern!r}")


def referenced_frontend_assets(index_text: str, label: str) -> tuple[str, str]:
    for forbidden in FORBIDDEN_WEB_ASSETS:
        require_not_contains(index_text, forbidden, label)
    js_match = re.search(r'src="\.?/assets/(index-[^"]+\.js)"', index_text)
    css_match = re.search(r'href="\.?/assets/(index-[^"]+\.css)"', index_text)
    require(js_match is not None, f"{label} missing JS asset")
    require(css_match is not None, f"{label} missing CSS asset")
    return js_match.group(1), css_match.group(1)


def require_section_not_contains(text: str, start: str, end: str, needle: str, label: str) -> None:
    start_idx = text.find(start)
    require(start_idx >= 0, f"{label} missing section start {start!r}")
    end_idx = text.find(end, start_idx + len(start))
    require(end_idx >= 0, f"{label} missing section end {end!r}")
    section = text[start_idx:end_idx]
    require_not_contains(section, needle, label)


def validate_runtime_source_texts(read_text_by_suffix, label: str) -> None:
    config_text = read_text_by_suffix("config.py")
    require_contains(config_text, "DEFAULT_CDP_ENDPOINT", f"{label} config")
    require_contains(config_text, "def chrome_devtools_mcp_args", f"{label} config")
    require_contains(config_text, "--experimentalVision", f"{label} config")
    require_contains(config_text, '"cdp_auto_launch": True', f"{label} config")

    tools_init = read_text_by_suffix("agent/tools/__init__.py")
    require_contains(tools_init, "def _safe_import", f"{label} tools init")
    require_contains(tools_init, "get_tool_import_errors", f"{label} tools init")
    require_contains(tools_init, "ImageGenTool", f"{label} tools init")
    require_contains(tools_init, "TongxinCli", f"{label} tools init")
    require_contains(tools_init, "OfficeDocumentsTool", f"{label} tools init")
    require_contains(tools_init, "OfficePdfTool", f"{label} tools init")
    require_contains(tools_init, "OfficePresentationsTool", f"{label} tools init")
    require_contains(tools_init, "OfficeSpreadsheetsTool", f"{label} tools init")

    base_tool = read_text_by_suffix("agent/tools/base_tool.py")
    require_contains(base_tool, "def apply_config", f"{label} base tool")

    web_channel = read_text_by_suffix("channel/web/web_channel.py")
    require_contains(web_channel, "def _finalize_request_after_worker", f"{label} web channel")
    require_contains(web_channel, "\"releaseNotes\": get_current_release_notes()", f"{label} web channel")
    require_contains(web_channel, "produce failed before worker start", f"{label} web channel")
    require_contains(web_channel, "get_cancel_registry().unregister(request_id)", f"{label} web channel")
    require_contains(web_channel, "def active_requests_snapshot", f"{label} web channel")
    require_contains(web_channel, "'/api/active-requests'", f"{label} web channel")
    require_contains(web_channel, "self.sse_events", f"{label} web channel")
    require_contains(web_channel, "self.sse_subscribers", f"{label} web channel")
    require_contains(web_channel, "def _push_sse_event", f"{label} web channel")
    require_contains(web_channel, "id: {event_id}", f"{label} web channel")
    require_contains(web_channel, "authorize_file_access(", f"{label} web channel")
    require_contains(web_channel, "quality_retry_max", f"{label} web channel image retry")
    require_contains(web_channel, "_image_job_quality_retry_max", f"{label} web channel image retry")
    require_contains(web_channel, "_quality_retry_attempt", f"{label} web channel image retry")
    require_contains(web_channel, "run_image_generation_payload", f"{label} web channel image runner")
    require_contains(web_channel, "image_generation_env_with_config", f"{label} web channel image runner")
    require_contains(web_channel, "Quality retry: regenerate", f"{label} web channel image retry")

    cancel_registry = read_text_by_suffix("agent/protocol/cancel.py")
    require_contains(cancel_registry, "def snapshot", f"{label} cancel registry")
    require_contains(cancel_registry, "\"age_seconds\"", f"{label} cancel registry")
    require_contains(cancel_registry, "\"state\": \"cancelling\"", f"{label} cancel registry")

    agent_stream = read_text_by_suffix("agent/protocol/agent_stream.py")
    require_contains(agent_stream, "TOOL_NAME_ALIASES", f"{label} agent stream")
    require_contains(agent_stream, '"shell": "bash"', f"{label} agent stream")
    require_contains(agent_stream, '"image_generation": "imagegen"', f"{label} agent stream")
    require_contains(agent_stream, "SKILL_CALLABLE_TOOL_ALIASES", f"{label} agent stream")
    require_contains(agent_stream, '"office": (', f"{label} agent stream")
    require_contains(agent_stream, "office_presentations", f"{label} agent stream")
    require_contains(agent_stream, "office_spreadsheets", f"{label} agent stream")
    require_contains(agent_stream, "office_documents", f"{label} agent stream")
    require_contains(agent_stream, "office_pdf", f"{label} agent stream")
    require_contains(agent_stream, "function_call", f"{label} agent stream")
    require_contains(agent_stream, "Permission blocked this external capability", f"{label} agent stream")
    require_contains(agent_stream, "mcp__chrome-devtools__", f"{label} agent stream")
    require_contains(agent_stream, "_force_text_response_once(\"permission-denied\")", f"{label} agent stream")
    require_contains(agent_stream, "def _external_capability_autoroute", f"{label} agent stream")
    require_contains(agent_stream, "def _extract_simple_lark_cli_args", f"{label} agent stream")
    require_contains(agent_stream, "def _looks_like_feishu_cli_command", f"{label} agent stream")
    require_contains(agent_stream, "def _extract_simple_tongxin_cli_args", f"{label} agent stream")
    require_contains(agent_stream, "def _looks_like_tongxin_cli_command", f"{label} agent stream")
    require_contains(agent_stream, "raw bash tongxin-cli", f"{label} agent stream")
    require_contains(agent_stream, "@larksuite/cli", f"{label} agent stream")
    require_contains(agent_stream, "scripts/run.js", f"{label} agent stream")
    require_contains(agent_stream, "\"reroutedFrom\"", f"{label} agent stream")

    runtime_projection = read_text_by_suffix("agent/protocol/runtime_projection.py")
    require_contains(runtime_projection, "_QUALITY_EVIDENCE_ALLOWED_GATES", f"{label} runtime projection")
    require_contains(runtime_projection, "_QUALITY_EVIDENCE_METRIC_KEYS", f"{label} runtime projection")
    require_contains(runtime_projection, "_extract_projection_quality_evidence", f"{label} runtime projection")
    require_contains(runtime_projection, "_safe_projection_quality_check_detail", f"{label} runtime projection")
    require_contains(runtime_projection, "_safe_projection_quality_evidence", f"{label} runtime projection")
    require_contains(runtime_projection, "_safe_projection_quality_metric_string", f"{label} runtime projection")
    require_contains(runtime_projection, "_safe_projection_quality_rendered_artifacts", f"{label} runtime projection")
    require_contains(runtime_projection, "_quality_ref_is_hmac", f"{label} runtime projection")
    require_contains(runtime_projection, "qualityEvidence", f"{label} runtime projection")
    require_contains(runtime_projection, '"renderProof",', f"{label} runtime projection")
    require_contains(runtime_projection, 'record["qualityEvidence"]', f"{label} runtime projection")
    require_contains(runtime_projection, '"decode-valid"', f"{label} runtime projection")
    require_contains(runtime_projection, '"seam-check"', f"{label} runtime projection")
    require_contains(runtime_projection, '"text-glyph-check"', f"{label} runtime projection")
    require_contains(runtime_projection, '"watermark-check"', f"{label} runtime projection")
    require_contains(runtime_projection, '"anomaly-check"', f"{label} runtime projection")
    require_contains(runtime_projection, '"reference-fidelity"', f"{label} runtime projection")
    require_contains(runtime_projection, '"imageAnalysis"', f"{label} runtime projection")
    require_contains(runtime_projection, '"retryGate"', f"{label} runtime projection")
    require_contains(runtime_projection, "_safe_projection_quality_gate(text)", f"{label} runtime projection")

    image_quality_runtime = read_text_by_suffix("common/image_quality_runtime.py")
    require_contains(image_quality_runtime, "def build_image_quality_evidence", f"{label} image quality runtime")
    require_contains(image_quality_runtime, "def analyze_image_quality", f"{label} image quality runtime")
    require_contains(image_quality_runtime, '"decode-valid"', f"{label} image quality runtime")
    require_contains(image_quality_runtime, '"seam-check"', f"{label} image quality runtime")
    require_contains(image_quality_runtime, '"overlay-ghosting-check"', f"{label} image quality runtime")
    require_contains(image_quality_runtime, '"text-glyph-check"', f"{label} image quality runtime")
    require_contains(image_quality_runtime, '"watermark-check"', f"{label} image quality runtime")
    require_contains(image_quality_runtime, '"subject-structure-check"', f"{label} image quality runtime")
    require_contains(image_quality_runtime, '"anomaly-check"', f"{label} image quality runtime")
    require_contains(image_quality_runtime, '"reference-fidelity"', f"{label} image quality runtime")
    require_contains(image_quality_runtime, "def compare_image_reference_quality", f"{label} image quality runtime")
    require_contains(image_quality_runtime, "def build_image_finalization_decision", f"{label} image quality runtime")
    require_contains(image_quality_runtime, "def attach_image_finalization_evidence", f"{label} image quality runtime")
    require_contains(image_quality_runtime, "IMAGE_FINALIZATION_POLICY_VERSION", f"{label} image quality runtime")
    require_contains(image_quality_runtime, "def _analysis_sample", f"{label} image quality runtime")
    require_contains(image_quality_runtime, "def _apply_decoder_draft", f"{label} image quality runtime")
    require_contains(image_quality_runtime, "def _alpha_sample_metrics", f"{label} image quality runtime")
    require_contains(image_quality_runtime, "def _vision_risk_summary", f"{label} image quality runtime")
    require_contains(image_quality_runtime, "referenceMismatchRisk", f"{label} image quality runtime")
    require_contains(image_quality_runtime, "glyphFragmentRisk", f"{label} image quality runtime")
    require_contains(image_quality_runtime, "_EVIDENCE_HMAC_KEY", f"{label} image quality runtime")

    image_job_service = read_text_by_suffix("agent/protocol/image_job_service.py")
    require_contains(image_job_service, "build_image_quality_evidence", f"{label} image job service")
    require_contains(image_job_service, '"qualityEvidence"', f"{label} image job service")
    require_contains(image_job_service, "_authorized_quality_reference_images", f"{label} image job service")
    require_contains(image_job_service, "_image_quality_target", f"{label} image job service")
    require_contains(image_job_service, "_quality_retry_limit", f"{label} image job service")
    require_contains(image_job_service, "_finalize_safe_artifacts", f"{label} image job service")
    require_contains(image_job_service, '"retry"', f"{label} image job service")
    require_contains(image_job_service, "reference_images = _authorized_quality_reference_images(task)", f"{label} image job service")
    require_contains(image_job_service, "reference_images=reference_images", f"{label} image job service")
    require_contains(image_job_service, "authorize_file_access(\"read\"", f"{label} image job service")
    require_contains(image_job_service, "state.artifacts.append(safe_artifact)", f"{label} image job service")
    require_contains(image_job_service, "provider_latency_ms", f"{label} image job service")
    require_contains(image_job_service, "quality_latency_ms", f"{label} image job service")
    require_contains(image_job_service, '"quality_check"', f"{label} image job service")

    imagegen_tool = read_text_by_suffix("agent/tools/imagegen/imagegen.py")
    provider_runner = read_text_by_suffix("agent/tools/imagegen/provider_runner.py")
    require_contains(provider_runner, "def run_image_generation_payload", f"{label} imagegen provider runner")
    require_contains(provider_runner, "load_image_generation_module", f"{label} imagegen provider runner")
    require_contains(provider_runner, "_build_providers_with_env", f"{label} imagegen provider runner")
    require_contains(provider_runner, "runnerMode", f"{label} imagegen provider runner")
    require_contains(imagegen_tool, "build_image_quality_evidence", f"{label} imagegen tool")
    require_contains(imagegen_tool, "run_image_generation_payload", f"{label} imagegen tool")
    require_contains(imagegen_tool, "image_generation_env_with_config", f"{label} imagegen tool")
    require_contains(imagegen_tool, "def _aggregate_image_quality_evidence", f"{label} imagegen tool")
    require_contains(imagegen_tool, "reference_images=authorized_sources", f"{label} imagegen tool")
    require_contains(imagegen_tool, "_with_image_finalization", f"{label} imagegen tool")
    require_contains(imagegen_tool, "quality_retry_max", f"{label} imagegen tool")
    require_contains(imagegen_tool, "Quality retry: regenerate", f"{label} imagegen tool")
    require_contains(imagegen_tool, "def _safe_image_result_row", f"{label} imagegen tool")
    require_contains(imagegen_tool, "def _safe_imagegen_failure_payload", f"{label} imagegen tool")
    require_contains(imagegen_tool, "def _safe_text_presence", f"{label} imagegen tool")
    require_not_contains(imagegen_tool, "stderrTail", f"{label} imagegen tool")
    require_contains(imagegen_tool, '"qualityEvidence"', f"{label} imagegen tool")
    require_contains(imagegen_tool, '"timing"', f"{label} imagegen tool")
    require_contains(imagegen_tool, "providerTotalLatencyMs", f"{label} imagegen tool")

    tool_manager = read_text_by_suffix("agent/tools/tool_manager.py")
    require_contains(tool_manager, "def registry_health", f"{label} tool manager")
    require_contains(tool_manager, "missingConfiguredTools", f"{label} tool manager")
    require_contains(tool_manager, "def _mcp_public_tool_name", f"{label} tool manager")
    require_contains(tool_manager, "Refusing to replace first-party tool", f"{label} tool manager")
    require_contains(tool_manager, "apply_config(self.tool_configs[name])", f"{label} tool manager")

    broker = read_text_by_suffix("common/ecorex_tool_permissions.py")
    require_contains(broker, "def authorize_file_access", f"{label} permission broker")
    require_contains(broker, "def _default_filesystem_profile", f"{label} permission broker")
    require_contains(broker, "def _evaluate_filesystem_profile", f"{label} permission broker")
    require_contains(broker, "def _glob_matches", f"{label} permission broker")
    require_contains(broker, "get_appdata_dir", f"{label} permission broker")
    require_contains(broker, "\"filesystem-access\"", f"{label} permission broker")
    require_contains(broker, '"tongxin_cli"', f"{label} permission broker")
    require_contains(broker, "default-read-only-tongxin-cli", f"{label} permission broker")

    release_notes = read_text_by_suffix("common/ecorex_release_notes.py")
    require(
        re.search(r'"version"\s*:\s*"\d+\.\d+\.\d+"', release_notes) is not None,
        f"{label} release notes missing semantic version",
    )
    require_contains(release_notes, "\"updatePolicy\"", f"{label} release notes")
    require_contains(release_notes, "\"webui\"", f"{label} release notes")

    read_tool = read_text_by_suffix("agent/tools/read/read.py")
    require_contains(read_tool, "authorize_file_access(\"read\"", f"{label} read tool")
    find_tool = read_text_by_suffix("agent/tools/find/find.py")
    require_contains(find_tool, "class Find", f"{label} find tool")
    require_contains(find_tool, "authorize_file_access(\"read\"", f"{label} find tool")
    require_contains(find_tool, "fnmatch.fnmatchcase", f"{label} find tool")
    ls_tool = read_text_by_suffix("agent/tools/ls/ls.py")
    require_contains(ls_tool, "authorize_file_access(\"read\"", f"{label} ls tool")
    write_tool = read_text_by_suffix("agent/tools/write/write.py")
    require_contains(write_tool, "authorize_file_access(\"write\"", f"{label} write tool")
    edit_tool = read_text_by_suffix("agent/tools/edit/edit.py")
    require_contains(edit_tool, "authorize_file_access(\"write\"", f"{label} edit tool")
    send_tool = read_text_by_suffix("agent/tools/send/send.py")
    require_contains(send_tool, "authorize_file_access(\"read\"", f"{label} send tool")
    web_fetch = read_text_by_suffix("agent/tools/web_fetch/web_fetch.py")
    require_contains(web_fetch, "authorize_file_access(", f"{label} web fetch")
    require_contains(web_fetch, "authorize_file_access(\n                \"write\"", f"{label} web fetch")
    require_contains(web_fetch, "remote document download was blocked", f"{label} web fetch")
    web_search = read_text_by_suffix("agent/tools/web_search/web_search.py")
    require_contains(web_search, "internet search was blocked", f"{label} web search")
    vision = read_text_by_suffix("agent/tools/vision/vision.py")
    require_contains(vision, "authorize_file_access(\n                \"read\"", f"{label} vision")
    require_contains(vision, "image analysis and upload was blocked", f"{label} vision")

    memory_summarizer = read_text_by_suffix("agent/memory/summarizer.py")
    require_contains(memory_summarizer, "def _authorize_memory_write", f"{label} memory summarizer")
    require_contains(memory_summarizer, "def _authorize_memory_read", f"{label} memory summarizer")
    require_contains(memory_summarizer, "def _safe_user_segment", f"{label} memory summarizer")
    require_contains(memory_summarizer, "authorize_file_access(", f"{label} memory summarizer")
    knowledge_service = read_text_by_suffix("agent/knowledge/service.py")
    require_contains(knowledge_service, "def _authorize_read", f"{label} knowledge service")
    require_contains(knowledge_service, "authorize_file_access(", f"{label} knowledge service")

    feishu_cli = read_text_by_suffix("agent/tools/feishu_cli/feishu_cli.py")
    require_contains(feishu_cli, "def apply_config", f"{label} feishu cli")
    require_contains(feishu_cli, "self.package = str(self.config.get(\"package\")", f"{label} feishu cli")
    require_contains(feishu_cli, "--device-code", f"{label} feishu cli")
    require_contains(feishu_cli, "\"auth\", \"qrcode\"", f"{label} feishu cli")
    require_contains(feishu_cli, "--app-secret-stdin", f"{label} feishu cli")
    require_contains(feishu_cli, "input_text=app_secret + \"\\n\"", f"{label} feishu cli")
    require_contains(feishu_cli, "def _feishu_credentials", f"{label} feishu cli")
    web_channel = read_text_by_suffix("channel/web/web_channel.py")
    require_contains(web_channel, "def _safe_feishu_cli_status_probe", f"{label} web channel")
    require_contains(web_channel, "agentCliStatus", f"{label} web channel")

    tongxin_cli = read_text_by_suffix("agent/tools/tongxin_cli/tongxin_cli.py")
    require_contains(tongxin_cli, "class TongxinCli", f"{label} tongxin cli")
    require_contains(tongxin_cli, "SUPPORTED_SCRIPT_NAMES", f"{label} tongxin cli")
    require_contains(tongxin_cli, "def _configure", f"{label} tongxin cli")
    require_contains(tongxin_cli, "tools.tongxin_cli.script_path", f"{label} tongxin cli")
    require_contains(tongxin_cli, "READ_ONLY_ALLOWED_COMMANDS", f"{label} tongxin cli")
    require_contains(tongxin_cli, "validate_read_only_tongxin_args", f"{label} tongxin cli")
    require_contains(tongxin_cli, "subprocess.Popen(command", f"{label} tongxin cli")
    require_not_contains(tongxin_cli, "shell=True", f"{label} tongxin cli")

    ecorex_cli = read_text_by_suffix("agent/tools/ecorex_cli/ecorex_cli.py")
    require_contains(ecorex_cli, "class EcoreXCli", f"{label} ecorex cli")
    require_contains(ecorex_cli, "\"skill_list\"", f"{label} ecorex cli")
    require_contains(ecorex_cli, "\"install_browser\"", f"{label} ecorex cli")
    require_contains(ecorex_cli, "authorize_noninteractive(\"skill_write\"", f"{label} ecorex cli")

    optional_abilities = read_text_by_suffix("agent/tools/optional_abilities/optional_abilities.py")
    require_contains(optional_abilities, "class OptionalAbilities", f"{label} optional abilities")
    require_contains(optional_abilities, "config.py is missing v0.2.3 CDP symbols", f"{label} optional abilities")
    require_contains(optional_abilities, "\"chrome-devtools-mcp\"", f"{label} optional abilities")
    require_contains(optional_abilities, "\"browser-automation\"", f"{label} optional abilities")

    browser_tool = read_text_by_suffix("agent/tools/browser/browser_tool.py")
    require_contains(browser_tool, "DEFAULT_CDP_ENDPOINT", f"{label} browser tool")
    require_contains(browser_tool, "setdefault(\"cdp_auto_launch\", True)", f"{label} browser tool")
    require_contains(browser_tool, "setdefault(\"cdp_fallback\", True)", f"{label} browser tool")

    imagegen_tool = read_text_by_suffix("agent/tools/imagegen/imagegen.py")
    require_contains(imagegen_tool, "class ImageGenTool", f"{label} imagegen tool")
    require_contains(imagegen_tool, "generate.py", f"{label} imagegen tool")
    require_contains(imagegen_tool, "run_image_generation_payload", f"{label} imagegen tool")
    require_contains(imagegen_tool, "IMAGE_OUTPUT_DIR", f"{label} imagegen tool")
    require_contains(imagegen_tool, "[REDACTED]", f"{label} imagegen tool")
    require_contains(imagegen_tool, 'authorize_file_access("read"', f"{label} imagegen tool")
    require_contains(imagegen_tool, 'authorize_file_access("write"', f"{label} imagegen tool")

    skill_bridge = read_text_by_suffix("agent/skills/tool_bridge.py")
    require_contains(skill_bridge, "SKILL_CALLABLE_TOOL_ALIASES", f"{label} skill tool bridge")
    require_contains(skill_bridge, '"office-pdf": "office_pdf"', f"{label} skill tool bridge")
    require_contains(skill_bridge, '"presentations": "office_presentations"', f"{label} skill tool bridge")
    require_contains(skill_bridge, '"tongxin-cli": "tongxin_cli"', f"{label} skill tool bridge")
    require_contains(skill_bridge, '"芯助手": "tongxin_cli"', f"{label} skill tool bridge")
    require_contains(skill_bridge, "def skill_agent_surface", f"{label} skill tool bridge")

    skill_formatter = read_text_by_suffix("agent/skills/formatter.py")
    require_contains(skill_formatter, "resolve_callable_tool_name", f"{label} skill formatter")
    require_contains(skill_formatter, "<callable_tool>", f"{label} skill formatter")

    extension_registry = read_text_by_suffix("agent/extensions/registry.py")
    require_contains(extension_registry, "skill_agent_surface", f"{label} extension registry")
    require_contains(extension_registry, "toolSchemaCallable", f"{label} extension registry")

    office_tools = read_text_by_suffix("agent/tools/office_artifacts/office_artifacts.py")
    require_contains(office_tools, "class OfficeDocumentsTool", f"{label} office tools")
    require_contains(office_tools, "class OfficePdfTool", f"{label} office tools")
    require_contains(office_tools, "class OfficePresentationsTool", f"{label} office tools")
    require_contains(office_tools, "class OfficeSpreadsheetsTool", f"{label} office tools")
    require_contains(office_tools, "build_pdf_quality_evidence", f"{label} office tools")
    require_contains(office_tools, "build_presentation_quality_evidence", f"{label} office tools")
    require_contains(office_tools, "authorize_file_access", f"{label} office tools")
    require_contains(imagegen_tool, "image input read blocked by permissions", f"{label} imagegen tool")
    require_contains(imagegen_tool, "image output directory blocked by permissions", f"{label} imagegen tool")

    permissions = read_text_by_suffix("common/ecorex_tool_permissions.py")
    require_contains(permissions, '"imagegen"', f"{label} permission broker")

    skill_formatter = read_text_by_suffix("agent/skills/formatter.py")
    require_contains(skill_formatter, "format_skill_diagnostics_for_prompt", f"{label} skill formatter")
    require_contains(skill_formatter, "<skill_load_diagnostics>", f"{label} skill formatter")

    skill_loader = read_text_by_suffix("agent/skills/loader.py")
    require_contains(skill_loader, "self.last_diagnostics", f"{label} skill loader")
    require_contains(skill_loader, "def get_last_diagnostics", f"{label} skill loader")

    skill_manager = read_text_by_suffix("agent/skills/manager.py")
    require_contains(skill_manager, "self.last_load_diagnostics", f"{label} skill manager")
    require_contains(skill_manager, "format_skill_diagnostics_for_prompt(self.get_load_diagnostics())", f"{label} skill manager")
    require_contains(skill_manager, "MANAGED_BUILTIN_REFRESH_MARKERS", f"{label} skill manager")
    require_contains(skill_manager, "_refresh_managed_builtin_overlays", f"{label} skill manager")
    require_contains(skill_manager, 'DEFAULT_MODEL = "gpt-image-2-pro"', f"{label} skill manager")

    host_diagnostics = read_text_by_suffix("agent/tools/host_diagnostics/host_diagnostics.py")
    require_contains(host_diagnostics, "def _skill_status", f"{label} host diagnostics")
    require_contains(host_diagnostics, "def _tongxin_status", f"{label} host diagnostics")
    require_contains(host_diagnostics, "authorize_file_access(", f"{label} host diagnostics")
    require_contains(host_diagnostics, "\"read\"", f"{label} host diagnostics")
    require_contains(host_diagnostics, "\"blocked\": True", f"{label} host diagnostics")
    require_contains(host_diagnostics, "\"skills\": _skill_status(self.cwd)", f"{label} host diagnostics")
    require_contains(host_diagnostics, "\"hasGoalTool\": False", f"{label} host diagnostics")
    require_contains(host_diagnostics, "\"availableStructuredCliTools\": [\"feishu_cli\", \"tongxin_cli\", \"ecorex_cli\", \"optional_abilities\"]", f"{label} host diagnostics")

    mcp_tool = read_text_by_suffix("agent/tools/mcp/mcp_tool.py")
    require_contains(mcp_tool, "self.remote_name", f"{label} mcp tool")
    require_contains(mcp_tool, "public_name", f"{label} mcp tool")

    image_generation = read_text_by_suffix("skills/image-generation/scripts/generate.py")
    require_contains(image_generation, "DEFAULT_MODEL = \"gpt-image-2-pro\"", f"{label} image-generation skill")
    require_contains(image_generation, "LinkAI default model follows EcoreX's OpenAI image default", f"{label} image-generation skill")
    require_contains(image_generation, "\"output_format\"", f"{label} image-generation skill")
    require_contains(image_generation, "or OpenAIProvider.DEFAULT_MODEL", f"{label} image-generation skill")
    require_not_contains(image_generation, 'FALLBACK_MODEL = "gpt-image-2"', f"{label} image-generation skill")
    require_not_contains(image_generation, "falling back to {self.FALLBACK_MODEL}", f"{label} image-generation skill")
    require_contains(image_generation, "/images/generations", f"{label} image-generation skill")
    require_contains(image_generation, "/images/edits", f"{label} image-generation skill")
    require_contains(image_generation, "image_url=image_url", f"{label} image-generation skill")
    require_section_not_contains(
        image_generation,
        "class OpenAIProvider",
        "class LinkAIProvider",
        "\"response_format\"",
        f"{label} OpenAI image-generation skill",
    )
    find_skill = read_text_by_suffix("skills/find/SKILL.md")
    require_contains(find_skill, "name: find", f"{label} find skill")
    require_contains(find_skill, "Use the `find` tool", f"{label} find skill")

    skill_creator = read_text_by_suffix("skills/skill-creator/SKILL.md")
    require_contains(skill_creator, "name: skill-creator", f"{label} skill-creator")
    require_contains(skill_creator, "Skill Creation Process", f"{label} skill-creator")
    require_contains(skill_creator, "scripts/init_skill.py", f"{label} skill-creator")
    init_skill = read_text_by_suffix("skills/skill-creator/scripts/init_skill.py")
    quick_validate = read_text_by_suffix("skills/skill-creator/scripts/quick_validate.py")
    require_contains(init_skill, "SKILL.md", f"{label} skill-creator init")
    require_contains(quick_validate, "frontmatter", f"{label} skill-creator validation")

    web_channel = read_text_by_suffix("channel/web/web_channel.py")
    require_regex(web_channel, r'"openai"\s*:\s*\[\s*"gpt-image-2-pro"', f"{label} admin image model catalog")
    require_regex(web_channel, r'"linkai"\s*:\s*\[\s*"gpt-image-2-pro"', f"{label} admin image model catalog")
    require_contains(web_channel, '("openai",    "gpt-image-2-pro")', f"{label} admin image auto hint")
    require_contains(web_channel, '("linkai",    "gpt-image-2-pro")', f"{label} admin image LinkAI fallback hint")
    require_not_contains(web_channel, '("linkai",    "image-2-pro")', f"{label} admin image LinkAI fallback hint")


def validate_frontend_bundle_texts(read_text_by_suffix, label: str) -> None:
    index_text = read_text_by_suffix("channel/web/static/app/index.html")
    js_name, css_name = referenced_frontend_assets(index_text, f"{label} static app index")
    renderer = read_text_by_suffix(f"channel/web/static/app/assets/{js_name}")
    read_text_by_suffix(f"channel/web/static/app/assets/{css_name}")
    require_contains(renderer, "voice_attach", f"{label} renderer bundle")
    require_contains(renderer, "extras?.audio", f"{label} renderer bundle")
    require_contains(renderer, "/api/active-requests", f"{label} renderer bundle")
    require_contains(renderer, "activeRequests", f"{label} renderer bundle")
    require_contains(renderer, "releaseNotes", f"{label} renderer bundle")
    require_contains(renderer, "ecorex-release-notes-seen-version", f"{label} renderer bundle")
    require_contains(renderer, "/api/project-folder", f"{label} renderer bundle")
    require_contains(renderer, "/api/open-path", f"{label} renderer bundle")
    require_contains(renderer, "data-ecorex-file-path", f"{label} renderer bundle")
    require_contains(renderer, "decode-valid", f"{label} renderer bundle")
    require_contains(renderer, "seam-check", f"{label} renderer bundle")
    require_contains(renderer, "text-glyph-check", f"{label} renderer bundle")
    require_contains(renderer, "watermark-check", f"{label} renderer bundle")
    require_contains(renderer, "anomaly-check", f"{label} renderer bundle")
    require_contains(renderer, "reference-fidelity", f"{label} renderer bundle")
    require_contains(renderer, "reference-fidelity-skipped-review", f"{label} renderer bundle")
    require_contains(renderer, "retry_count", f"{label} renderer bundle")
    require_contains(renderer, "max_retries", f"{label} renderer bundle")
    require_contains(renderer, "retry_gate", f"{label} renderer bundle")


def zip_text_by_suffix(archive: zipfile.ZipFile, suffix: str) -> str:
    matches = [name for name in archive.namelist() if name.endswith(suffix)]
    require(matches, f"{archive.filename} missing text file suffix {suffix}")
    return archive.read(matches[0]).decode("utf-8", errors="replace")


def tar_text_by_suffix(archive: tarfile.TarFile, suffix: str) -> str:
    matches = [name for name in archive.getnames() if name.endswith(suffix)]
    require(matches, f"{archive.name} missing text file suffix {suffix}")
    handle = archive.extractfile(matches[0])
    require(handle is not None, f"{archive.name} cannot read {matches[0]}")
    return handle.read().decode("utf-8", errors="replace")


def validate_desktop_unpacked(desktop_dir: pathlib.Path, node_modules: pathlib.Path) -> None:
    require(desktop_dir.is_dir(), f"desktop unpacked dir missing: {desktop_dir}")
    resources = desktop_dir / "resources"
    app_asar = resources / "app.asar"
    runtime = resources / "ecorex-runtime"
    require(app_asar.is_file(), f"desktop app.asar missing: {app_asar}")
    require(runtime.is_dir(), f"desktop runtime missing: {runtime}")
    validate_directory_no_forbidden_release_strings(runtime, "desktop runtime")
    desktop_readme = desktop_dir / "README-migration.txt"
    require(desktop_readme.is_file(), f"desktop package missing {desktop_readme.name}")
    require(
        is_allowed_migration_readme(desktop_readme.name, desktop_readme.read_text(encoding="utf-8")),
        f"desktop package {desktop_readme.name} missing EcoreX migration note",
    )

    for rel in REQUIRED_DESKTOP_RUNTIME_FILES:
        require((runtime / rel).is_file(), f"desktop runtime missing {rel}")

    validate_runtime_source_texts(
        lambda suffix: (runtime / suffix).read_text(encoding="utf-8"),
        "desktop runtime",
    )

    broker_text = (runtime / "common" / "ecorex_tool_permissions.py").read_text(encoding="utf-8")
    require_contains(broker_text, "def _interactive_permission_available", "desktop runtime broker")
    require_contains(broker_text, "get_appdata_dir", "desktop runtime broker")
    require_contains(
        broker_text,
        "Interactive permission confirmation is unavailable in this runtime",
        "desktop runtime broker",
    )
    require_contains(
        broker_text,
        "return (tool_name or \"\").strip().lower() in _DANGEROUS_TOOLS",
        "desktop runtime broker",
    )
    require_not_contains(broker_text, "if \"web\" not in channel_type", "desktop runtime broker")

    main_text = extract_asar_text(app_asar, "dist-electron/main.js", node_modules)
    require_no_forbidden_release_text("desktop main process", "dist-electron/main.js", main_text)
    require_contains(main_text, "safeOpenExternal", "desktop main process")
    require_contains(main_text, "externalUrlProtocols", "desktop main process")
    require_not_contains(main_text, "capability-install", "desktop main process")
    require_not_contains(main_text, "capability-preinstall", "desktop main process")
    require_contains(main_text, "blocked external URL protocol", "desktop main process")
    require_contains(main_text, "\"https:\"", "desktop main process")
    require_contains(main_text, "\"mailto:\"", "desktop main process")

    api_bridge_text = extract_asar_text(app_asar, "dist-electron/apiBridge.js", node_modules)
    require_no_forbidden_release_text("desktop api bridge", "dist-electron/apiBridge.js", api_bridge_text)
    require_contains(api_bridge_text, "/api/agent-install-request", "desktop api bridge")
    require_contains(api_bridge_text, "/api/subagents", "desktop api bridge")
    require_contains(api_bridge_text, "/api/project-folder", "desktop api bridge")
    require_contains(api_bridge_text, "/api/logs", "desktop api bridge")
    require_contains(api_bridge_text, "/api/logs/snapshot", "desktop api bridge")
    require_contains(api_bridge_text, "This EcoreX desktop API path is not allowed", "desktop api bridge")

    asar_files = set(list_asar_files(app_asar, node_modules))
    require("dist-electron/updater.js" not in asar_files, "desktop app.asar must not include retired updater.js")
    require_not_contains(main_text, "EcorexUpdateManager", "desktop main process")
    require_not_contains(main_text, "electron-updater", "desktop main process")
    permissions_text = extract_asar_text(app_asar, "dist-electron/permissions.js", node_modules)
    require_no_forbidden_release_text("desktop permission manager", "dist-electron/permissions.js", permissions_text)
    require_contains(permissions_text, "authorizeHostCapability", "desktop permission manager")
    require_contains(permissions_text, "Interactive permission confirmation is required", "desktop permission manager")
    require_contains(permissions_text, "Read Only mode blocks optional host capability installation", "desktop permission manager")

    capabilities_text = extract_asar_text(app_asar, "dist-electron/capabilities.js", node_modules)
    require_no_forbidden_release_text("desktop capability manager", "dist-electron/capabilities.js", capabilities_text)
    require_contains(capabilities_text, "current agent session", "desktop capability manager")
    require_contains(capabilities_text, "fetchCapabilityPolicyWithKeyFallback", "desktop capability manager")
    require_contains(capabilities_text, "invalid client key", "desktop capability manager")
    require_not_contains(capabilities_text, "capability-install", "desktop capability manager")
    require_not_contains(capabilities_text, "preinstallPolicyPacks", "desktop capability manager")

    renderer_text = extract_asar_renderer_bundle_text(app_asar, node_modules)
    require_no_forbidden_release_text("desktop renderer bundle", "renderer", renderer_text)
    require_contains(renderer_text, "voice_attach", "desktop renderer bundle")
    require_contains(renderer_text, "extras?.audio", "desktop renderer bundle")
    require_contains(renderer_text, "/api/project-folder", "desktop renderer bundle")
    require_contains(renderer_text, "/api/open-path", "desktop renderer bundle")
    require_contains(renderer_text, "data-ecorex-file-path", "desktop renderer bundle")
    print(f"PASS desktop unpacked host-boundary {desktop_dir}")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", default="deploy/ecorex-site/manifest.json")
    parser.add_argument("--artifact-dir", default="release-artifacts")
    parser.add_argument("--version", default="")
    parser.add_argument("--public-zip", default="")
    parser.add_argument("--desktop-dir", default="")
    parser.add_argument("--desktop-node-modules", default="desktop/node_modules")
    parser.add_argument(
        "--desktop-only",
        action="store_true",
        help="Validate a local desktop unpacked build without requiring release artifacts.",
    )
    args = parser.parse_args(argv)
    if not args.version:
        desktop_package = pathlib.Path("desktop/package.json")
        require(desktop_package.is_file(), "cannot infer version: desktop/package.json missing")
        args.version = json.loads(desktop_package.read_text(encoding="utf-8")).get("version", "")
        require(args.version, "cannot infer version: desktop/package.json has no version")

    if args.desktop_only:
        require(bool(args.desktop_dir), "--desktop-only requires --desktop-dir")
        validate_desktop_unpacked(pathlib.Path(args.desktop_dir), pathlib.Path(args.desktop_node_modules))
        print("EcoreX desktop artifact validation passed.")
        return 0

    manifest_path = pathlib.Path(args.manifest)
    artifact_dir = pathlib.Path(args.artifact_dir)
    manifest = read_json_no_bom(manifest_path)
    require(manifest.get("version") == args.version, f"manifest version {manifest.get('version')} != {args.version}")
    ready = validate_manifest_artifacts(manifest, artifact_dir)
    validate_nested_web_assets(artifact_dir, ready)
    public_zip = pathlib.Path(args.public_zip) if args.public_zip else artifact_dir / f"EcoreX_{args.version}-public-release.zip"
    validate_public_zip(public_zip, manifest, ready)
    if args.desktop_dir:
        validate_desktop_unpacked(pathlib.Path(args.desktop_dir), pathlib.Path(args.desktop_node_modules))
    print("EcoreX release artifact validation passed.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except ValidationError as exc:
        print(f"FAIL {exc}", file=sys.stderr)
        raise SystemExit(1)
