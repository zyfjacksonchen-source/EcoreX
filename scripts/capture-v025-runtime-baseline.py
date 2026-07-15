#!/usr/bin/env python3
"""Capture EcoreX v0.2.5 runtime/tool dependency baseline evidence.

The report intentionally records dependency ownership and availability, not
business data or credentials. Raw production host/user/password values and
production target identifiers are never written.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import tarfile
import zipfile
from io import BytesIO
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
VERSION = "v0.2.5"
BASELINE_COMPARISON_VERSION = "0.2.4"
SERVER_FILE = Path(r"C:\Users\user\Desktop\企业服务器地址.txt")
OUT_DIR = ROOT / "docs" / VERSION / "artifacts"
OUT_PATH = OUT_DIR / f"{VERSION}-runtime-baseline.json"

RUNTIME_BASENAMES = {
    "node",
    "node.exe",
    "npm",
    "npm.cmd",
    "npx",
    "npx.cmd",
    "lark-cli",
    "lark-cli.cmd",
    "lark-cli.exe",
    "xin_agent_cli.py",
}


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest().upper()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest().upper()
    except Exception:
        return ""


def hash16(value: str) -> str:
    return sha256_text(value)[:16] if value else ""


def user_home() -> str:
    return str(Path.home()).replace("\\", "/").rstrip("/")


def redact_path(value: Any) -> Any:
    if value is None:
        return None
    text = str(value)
    home = user_home()
    normalized = text.replace("\\", "/")
    if home and normalized.lower().startswith(home.lower()):
        normalized = "%USERPROFILE%" + normalized[len(home) :]
    normalized = normalized.replace("\\", "/")
    return normalized


def classify_path(value: Any) -> str:
    if not value:
        return "missing"
    text = str(value).replace("\\", "/").lower()
    local_appdata = str(Path(os.environ.get("LOCALAPPDATA", "")).resolve()).replace("\\", "/").lower() if os.environ.get("LOCALAPPDATA") else ""
    ecorex_root = (local_appdata + "/ecorex webui") if local_appdata else ""
    if ecorex_root and text.startswith(ecorex_root + "/runtime"):
        return "ecorex-bundled"
    if ecorex_root and text.startswith(ecorex_root + "/state"):
        return "ecorex-state"
    home = user_home().lower()
    mac_root = (home + "/library/application support/ecorex webui") if home else ""
    if mac_root and text.startswith(mac_root + "/runtime"):
        return "ecorex-bundled"
    if mac_root and text.startswith(mac_root + "/state"):
        return "ecorex-state"
    if "/opt/ecorex-web/" in text:
        if "/state/" in text:
            return "ecorex-state"
        return "ecorex-bundled"
    if "/.cache/codex-runtimes/" in text or "/.codex/" in text or "/.workbuddy/" in text or text.startswith("c:/cli-main"):
        return "codex-private"
    return "system-path"


def classify_remote_path(value: Any) -> str:
    if not value:
        return "missing"
    text = str(value).replace("\\", "/").lower()
    if "/opt/ecorex-web/" in text:
        return "ecorex-state" if "/state/" in text else "ecorex-bundled"
    if "/.cache/codex-runtimes/" in text or "/.codex/" in text or "/.workbuddy/" in text:
        return "codex-private"
    return "system-path"


def path_summary(value: Any, *, remote: bool = False) -> dict[str, Any]:
    text = str(value or "").strip()
    if not text:
        return {"pathPresent": False, "source": "missing", "basename": ""}
    source = classify_remote_path(text) if remote else classify_path(text)
    return {
        "pathPresent": True,
        "source": source,
        "basename": Path(text.replace("\\", "/")).name,
    }


def read_server_file() -> tuple[str, str, str, str]:
    try:
        text = SERVER_FILE.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        text = SERVER_FILE.read_text(encoding="gb18030", errors="replace")

    host = ""
    for pattern in (r"https?://(\d{1,3}(?:\.\d{1,3}){3})(?::\d+)?", r"\b(\d{1,3}(?:\.\d{1,3}){3})\b"):
        match = re.search(pattern, text)
        if match:
            host = match.group(1)
            break
    if not host:
        raise RuntimeError("could not parse SSH host from server file")

    domain_match = re.search(r"域名\s*[:：]\s*([^\s]+)", text)
    domain = domain_match.group(1).strip().strip("/") if domain_match else ""

    user = "root"
    ssh_section = text
    ssh_match = re.search(r"ssh登录\s*[:：]\s*([^\r\n]+)(.*)", text, flags=re.IGNORECASE | re.S)
    if ssh_match:
        user = ssh_match.group(1).strip() or user
        ssh_section = ssh_match.group(2)

    password = ""
    for match in re.finditer(r"密码\s*[:：]\s*([^\r\n]+)", ssh_section):
        password = match.group(1).strip()
    if not password:
        passwords = re.findall(r"密码\s*[:：]\s*([^\r\n]+)", text)
        if passwords:
            password = passwords[-1].strip()
    if not password:
        raise RuntimeError("could not parse SSH password from server file")
    return host, domain, user, password


def run_local(command: list[str], timeout: int = 30, env: dict[str, str] | None = None, cwd: Path | None = None) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            command,
            cwd=str(cwd or ROOT),
            env=env,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
        return {
            "exitCode": completed.returncode,
            "stdout": completed.stdout[-4000:],
            "stderr": completed.stderr[-2000:],
        }
    except Exception as exc:
        return {"exitCode": None, "error": str(exc)}


def command_path(name: str) -> dict[str, Any]:
    found = shutil.which(name)
    return {
        "name": name,
        "path": redact_path(found),
        "source": classify_path(found),
    }


def artifact_entries(path: Path) -> list[str]:
    if not path.exists():
        return []
    if path.suffix == ".zip":
        with zipfile.ZipFile(path) as archive:
            return archive.namelist()
    if path.name.endswith(".tar.gz"):
        with tarfile.open(path, "r:gz") as archive:
            return [item.name for item in archive.getmembers()]
    return []


def runtime_hits_from_entries(entries: list[str]) -> list[str]:
    hits: list[str] = []
    for entry in entries:
        base = entry.replace("\\", "/").rsplit("/", 1)[-1].lower()
        if base in RUNTIME_BASENAMES:
            hits.append(entry)
    return hits


def nested_archive_summaries(path: Path) -> list[dict[str, Any]]:
    if not path.exists() or path.suffix != ".zip":
        return []
    summaries: list[dict[str, Any]] = []
    try:
        with zipfile.ZipFile(path) as archive:
            for item in archive.infolist():
                lower = item.filename.lower()
                if not (lower.endswith(".zip") or lower.endswith(".tar.gz")):
                    continue
                raw = archive.read(item)
                entries: list[str] = []
                if lower.endswith(".zip"):
                    with zipfile.ZipFile(BytesIO(raw)) as nested:
                        entries = nested.namelist()
                else:
                    with tarfile.open(fileobj=BytesIO(raw), mode="r:gz") as nested:
                        entries = [member.name for member in nested.getmembers()]
                hits = runtime_hits_from_entries(entries)
                summaries.append({
                    "entry": item.filename,
                    "size": item.file_size,
                    "runtimeHitCount": len(hits),
                    "runtimeHits": hits[:80],
                    "toolEntrypoints": {
                        "feishuCliTool": any("runtime/agent/tools/feishu_cli/feishu_cli.py" in entry.replace("\\", "/") for entry in entries),
                        "tongxinCliTool": any("runtime/agent/tools/tongxin_cli/tongxin_cli.py" in entry.replace("\\", "/") for entry in entries),
                        "imagegenTool": any("runtime/agent/tools/imagegen/imagegen.py" in entry.replace("\\", "/") for entry in entries),
                        "officePdfRuntime": any("runtime/common/office_pdf_runtime.py" in entry.replace("\\", "/") for entry in entries),
                    },
                })
    except Exception as exc:
        summaries.append({"status": "error", "errorCode": "nested_artifact_scan_failed", "errorClass": exc.__class__.__name__})
    return summaries


def archive_text_by_suffix(path: Path, suffix: str, limit: int = 200_000) -> str:
    if not path.exists():
        return ""
    try:
        if path.suffix == ".zip":
            with zipfile.ZipFile(path) as archive:
                name = next((item for item in archive.namelist() if item.endswith(suffix)), "")
                if not name:
                    return ""
                return archive.read(name)[:limit].decode("utf-8", errors="replace")
        if path.name.endswith(".tar.gz"):
            with tarfile.open(path, "r:gz") as archive:
                member = next((item for item in archive.getmembers() if item.name.endswith(suffix)), None)
                if not member:
                    return ""
                handle = archive.extractfile(member)
                if not handle:
                    return ""
                return handle.read(limit).decode("utf-8", errors="replace")
    except Exception:
        return ""
    return ""


def scan_artifact(label: str, path: Path) -> dict[str, Any]:
    entries = artifact_entries(path)
    hits = runtime_hits_from_entries(entries)
    config_text = archive_text_by_suffix(path, "runtime/config-template.json") or archive_text_by_suffix(path, "runtime/config.json")
    capabilities_text = archive_text_by_suffix(path, "runtime/capabilities.json")
    install_win = archive_text_by_suffix(path, "scripts/install-ecorex-webui-win.ps1")
    install_mac = archive_text_by_suffix(path, "scripts/install-ecorex-webui-mac.sh")
    install_linux = archive_text_by_suffix(path, "scripts/install-ecorex-web.sh")
    combined_install = "\n".join([install_win, install_mac, install_linux])
    return {
        "label": label,
        "path": redact_path(path),
        "exists": path.exists(),
        "size": path.stat().st_size if path.exists() else 0,
        "sha256": file_sha256(path) if path.exists() else "",
        "baselineArtifactVersion": BASELINE_COMPARISON_VERSION,
        "baselineUse": "prior-version-runtime-gap-comparison",
        "runtimeHitCount": len(hits),
        "runtimeHits": hits[:120],
        "nestedArtifacts": nested_archive_summaries(path),
        "toolEntrypoints": {
            "feishuCliTool": any("runtime/agent/tools/feishu_cli/feishu_cli.py" in item.replace("\\", "/") for item in entries),
            "tongxinCliTool": any("runtime/agent/tools/tongxin_cli/tongxin_cli.py" in item.replace("\\", "/") for item in entries),
            "imagegenTool": any("runtime/agent/tools/imagegen/imagegen.py" in item.replace("\\", "/") for item in entries),
            "officePdfRuntime": any("runtime/common/office_pdf_runtime.py" in item.replace("\\", "/") for item in entries),
        },
        "configSignals": {
            "hasConfig": bool(config_text),
            "hasCapabilitiesManifest": bool(capabilities_text),
            "declaresFeishuCli": "feishu_cli" in config_text or "feishu-lark" in capabilities_text,
            "declaresTongxinCli": "tongxin_cli" in config_text or "tongxin-cli" in capabilities_text,
            "declaresNpxMcp": '"npx' in config_text or '"npx' in combined_install,
            "skipsLarkCliPreinstall": "lark-cli preinstall skipped" in combined_install or "Skipping bundled lark-cli" in combined_install,
        },
    }


def capture_codex_runtime() -> dict[str, Any]:
    node_root = Path.home() / ".cache" / "codex-runtimes" / "codex-primary-runtime" / "dependencies" / "node"
    python_exe = Path.home() / ".cache" / "codex-runtimes" / "codex-primary-runtime" / "dependencies" / "python" / "python.exe"
    node_exe = node_root / "bin" / "node.exe"
    node_modules = node_root / "node_modules"
    modules = [
        "pptxgenjs",
        "pdf-lib",
        "pdfjs-dist",
        "pixelmatch",
        "pngjs",
        "sharp",
        "tesseract.js",
        "docx",
        "marked",
        "playwright",
    ]
    node_probe: dict[str, Any] = {"available": node_exe.exists(), "node": redact_path(node_exe)}
    if node_exe.exists():
        script = (
            "const Module=require('module');Module._initPaths();"
            f"const mods={json.dumps(modules)};"
            "const out={}; for (const m of mods){try{out[m]=require.resolve(m)}catch(e){out[m]=false}};"
            "console.log(JSON.stringify(out));"
        )
        env = os.environ.copy()
        env["NODE_PATH"] = str(node_modules)
        result = run_local([str(node_exe), "-e", script], env=env)
        try:
            resolved = json.loads(result.get("stdout") or "{}")
        except Exception:
            resolved = {}
        node_probe["modules"] = {key: bool(value) for key, value in resolved.items()}
        node_probe["modulePaths"] = {key: redact_path(value) if value else None for key, value in resolved.items()}

    py_modules = ["openpyxl", "pandas", "pptx", "docx", "pdfplumber", "PIL", "numpy", "reportlab", "lark_oapi"]
    python_probe: dict[str, Any] = {"available": python_exe.exists(), "python": redact_path(python_exe)}
    if python_exe.exists():
        probe = (
            "import importlib.util,json;"
            f"mods={json.dumps(py_modules)};"
            "print(json.dumps({m: bool(importlib.util.find_spec(m)) for m in mods}, sort_keys=True))"
        )
        result = run_local([str(python_exe), "-c", probe])
        try:
            python_probe["modules"] = json.loads(result.get("stdout") or "{}")
        except Exception:
            python_probe["modules"] = {}

    skill_roots = [
        Path.home() / ".codex" / "skills" / ".system",
        Path.home() / ".codex" / "plugins" / "cache" / "openai-primary-runtime",
        Path.home() / ".codex" / "plugins" / "cache" / "openai-bundled",
    ]
    skills = sorted(redact_path(path) for root in skill_roots if root.exists() for path in root.rglob("SKILL.md"))
    return {
        "source": "codex-private",
        "node": node_probe,
        "python": python_probe,
        "skills": skills,
    }


def capture_local_ecorex() -> dict[str, Any]:
    local_root = Path(os.environ.get("LOCALAPPDATA", "")) / "EcoreX WebUI"
    runtime = local_root / "runtime"
    state = local_root / "state"
    hits = []
    if local_root.exists():
        for path in local_root.rglob("*"):
            if path.name.lower() in RUNTIME_BASENAMES:
                hits.append({"path": redact_path(path), "source": classify_path(path)})
    python_exe = runtime / "python" / "python.exe"
    feishu_status: dict[str, Any] = {"available": False, "reason": "packaged python missing"}
    tongxin_status: dict[str, Any] = {"available": False, "reason": "packaged python missing"}
    if python_exe.exists():
        probe = """
import json
from pathlib import Path
from agent.tools.feishu_cli.feishu_cli import FeishuCli
from agent.tools.tongxin_cli.tongxin_cli import TongxinCli
runtime = Path(r'%s')
workspace = r'%s'
config = {}
for candidate in [runtime / 'config.json', runtime.parent / 'state' / 'config.json']:
    try:
        if candidate.exists():
            config = json.loads(candidate.read_text(encoding='utf-8-sig'))
            break
    except Exception:
        pass
tools = config.get('tools') if isinstance(config.get('tools'), dict) else {}
feishu_cfg = dict(tools.get('feishu_cli') or {})
feishu_cfg['cwd'] = workspace
tongxin_cfg = dict(tools.get('tongxin_cli') or {})
tongxin_cfg['cwd'] = workspace
status = FeishuCli(feishu_cfg).execute({'action':'status'}).result
tx = TongxinCli(tongxin_cfg).execute({'action':'status'}).result
print(json.dumps({
  'feishu': {k: status.get(k) for k in ['available','command','npm','npx','authState','installRoot','pathHints']},
  'tongxin': {k: tx.get(k) for k in ['available','configured','configurationState','persistedConfig','remoteAuthConfigured','remoteBootstrapAvailable']}
}, ensure_ascii=False, sort_keys=True))
""" % (
            str(runtime).replace("\\", "\\\\"),
            str((Path.home() / "EcoreX")).replace("\\", "\\\\"),
        )
        env = os.environ.copy()
        env["PYTHONPATH"] = str(runtime)
        result = run_local([str(python_exe), "-c", probe], env=env, cwd=runtime)
        try:
            raw = json.loads(result.get("stdout") or "{}")
        except Exception:
            raw = {"error": result}
        raw_feishu = raw.get("feishu") if isinstance(raw.get("feishu"), dict) else raw
        feishu_status = {
            key: ([redact_path(item) for item in value] if isinstance(value, list) else redact_path(value) if key in {"npm", "npx", "installRoot"} else value)
            for key, value in raw_feishu.items()
        }
        if isinstance(raw_feishu.get("command"), list):
            feishu_status["command"] = [redact_path(item) for item in raw_feishu["command"]]
        feishu_status["commandSource"] = classify_path(raw_feishu.get("command", [None])[0] if isinstance(raw_feishu.get("command"), list) and raw_feishu.get("command") else None)
        feishu_status["npmSource"] = classify_path(raw_feishu.get("npm"))
        feishu_status["npxSource"] = classify_path(raw_feishu.get("npx"))
        raw_tongxin = raw.get("tongxin") if isinstance(raw.get("tongxin"), dict) else {}
        tongxin_status = {
            key: value for key, value in raw_tongxin.items()
            if key in {"available", "configured", "configurationState", "persistedConfig", "remoteAuthConfigured", "remoteBootstrapAvailable"}
        }
    return {
        "root": redact_path(local_root),
        "runtimeExists": runtime.exists(),
        "stateExists": state.exists(),
        "runtimeHits": hits[:120],
        "toolStatuses": {
            "feishuCli": feishu_status,
            "tongxinCli": tongxin_status,
        },
    }


def parse_kv_section(lines: list[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in lines:
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        result[key.strip()] = value.strip()
    return result


def split_sections(text: str) -> dict[str, list[str]]:
    sections: dict[str, list[str]] = {}
    current = ""
    markers = {"host-shell", "service-user", "runtime-files", "feishu-status", "tongxin-status"}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line in markers:
            current = line
            sections.setdefault(current, [])
            continue
        if current:
            sections[current].append(raw_line)
    return sections


def summarize_remote_command_section(section: dict[str, str]) -> dict[str, Any]:
    return {
        key: path_summary(value, remote=True)
        for key, value in section.items()
        if key in {"node", "npm", "npx"}
    }


def production_tool_status(payload: dict[str, Any]) -> dict[str, Any]:
    command = payload.get("command")
    command_path = command[0] if isinstance(command, list) and command else ""
    return {
        "available": bool(payload.get("available")),
        "authState": payload.get("authState"),
        "configurationState": payload.get("configurationState"),
        "configured": payload.get("configured"),
        "persistedConfig": payload.get("persistedConfig"),
        "remoteAuthConfigured": payload.get("remoteAuthConfigured"),
        "remoteBootstrapAvailable": payload.get("remoteBootstrapAvailable"),
        "command": path_summary(command_path, remote=True),
        "npm": path_summary(payload.get("npm"), remote=True),
        "npx": path_summary(payload.get("npx"), remote=True),
        "installRoot": path_summary(payload.get("installRoot"), remote=True),
    }


def parse_production_output(text: str) -> dict[str, Any]:
    sections = split_sections(text)
    feishu: dict[str, Any] = {}
    tongxin: dict[str, Any] = {}
    try:
        feishu = json.loads("\n".join(sections.get("feishu-status", [])).strip() or "{}")
    except Exception:
        feishu = {}
    try:
        tongxin = json.loads("\n".join(sections.get("tongxin-status", [])).strip() or "{}")
    except Exception:
        tongxin = {}
    return {
        "hostShell": summarize_remote_command_section(parse_kv_section(sections.get("host-shell", []))),
        "serviceUser": summarize_remote_command_section(parse_kv_section(sections.get("service-user", []))),
        "runtimeFileHits": [
            path_summary(line.strip(), remote=True)
            for line in sections.get("runtime-files", [])
            if line.strip()
        ][:80],
        "toolStatuses": {
            "feishuCli": production_tool_status(feishu),
            "tongxinCli": production_tool_status(tongxin),
        },
    }


def capture_production(include_production: bool = False) -> dict[str, Any]:
    if not include_production:
        return {
            "status": "skipped",
            "reason": "production probing requires explicit --include-production",
            "targetRedacted": True,
        }
    try:
        import paramiko  # type: ignore
    except Exception as exc:
        return {"status": "skipped", "reason": f"paramiko unavailable: {exc}"}
    try:
        host, _domain, user, password = read_server_file()
    except Exception as exc:
        return {"status": "skipped", "reason": f"server file unavailable: {exc}"}

    command = r'''
set -u
printf 'host-shell\n'
printf 'node=%s\n' "$(command -v node 2>/dev/null || true)"
printf 'npm=%s\n' "$(command -v npm 2>/dev/null || true)"
printf 'npx=%s\n' "$(command -v npx 2>/dev/null || true)"
printf 'service-user\n'
sudo -n -u ecorex -H sh -lc 'printf "node=%s\n" "$(command -v node 2>/dev/null || true)"; printf "npm=%s\n" "$(command -v npm 2>/dev/null || true)"; printf "npx=%s\n" "$(command -v npx 2>/dev/null || true)";'
printf 'runtime-files\n'
find /opt/ecorex-web/current/runtime /opt/ecorex-web/current/runtime/tools -maxdepth 4 \( -name node -o -name node.exe -o -name npm -o -name npm.cmd -o -name npx -o -name npx.cmd -o -name lark-cli -o -name lark-cli.cmd -o -name xin_agent_cli.py \) -print 2>/dev/null | sort | head -80
printf 'feishu-status\n'
cd /opt/ecorex-web/current/runtime
sudo -n -u ecorex -H env PYTHONPATH=/opt/ecorex-web/current/runtime /opt/ecorex-web/venv/bin/python - <<'PY'
import json
from pathlib import Path
from agent.tools.feishu_cli.feishu_cli import FeishuCli
config = {}
for candidate in [Path('/opt/ecorex-web/state/config.json'), Path('/opt/ecorex-web/current/runtime/config.json')]:
    try:
        if candidate.exists():
            config = json.loads(candidate.read_text(encoding='utf-8-sig'))
            break
    except Exception:
        pass
tools = config.get('tools') if isinstance(config.get('tools'), dict) else {}
cfg = dict(tools.get('feishu_cli') or {})
cfg['cwd'] = '/srv/ecorex-agent-workspace'
status = FeishuCli(cfg).execute({'action':'status'}).result
print(json.dumps({k: status.get(k) for k in ['available','command','npm','npx','authState','installRoot','pathHints']}, ensure_ascii=False, sort_keys=True))
PY
printf 'tongxin-status\n'
sudo -n -u ecorex -H env PYTHONPATH=/opt/ecorex-web/current/runtime /opt/ecorex-web/venv/bin/python - <<'PY'
import json
from pathlib import Path
from agent.tools.tongxin_cli.tongxin_cli import TongxinCli
config = {}
for candidate in [Path('/opt/ecorex-web/state/config.json'), Path('/opt/ecorex-web/current/runtime/config.json')]:
    try:
        if candidate.exists():
            config = json.loads(candidate.read_text(encoding='utf-8-sig'))
            break
    except Exception:
        pass
tools = config.get('tools') if isinstance(config.get('tools'), dict) else {}
cfg = dict(tools.get('tongxin_cli') or {})
cfg['cwd'] = '/srv/ecorex-agent-workspace'
status = TongxinCli(cfg).execute({'action':'status'}).result
print(json.dumps({k: status.get(k) for k in ['available','configured','configurationState','persistedConfig','remoteAuthConfigured','remoteBootstrapAvailable']}, ensure_ascii=False, sort_keys=True))
PY
'''
    client = paramiko.SSHClient()
    client.load_system_host_keys()
    client.set_missing_host_key_policy(paramiko.RejectPolicy())
    try:
        client.connect(
            hostname=host,
            username=user,
            password=password,
            timeout=25,
            banner_timeout=25,
            auth_timeout=25,
            look_for_keys=False,
            allow_agent=False,
        )
        _, stdout, stderr = client.exec_command(command, timeout=90)
        out = stdout.read().decode("utf-8", errors="replace")
        err = stderr.read().decode("utf-8", errors="replace")
        exit_code = stdout.channel.recv_exit_status()
        status = "success" if exit_code == 0 else "error"
    except Exception as exc:
        return {
            "status": "error",
            "targetRedacted": True,
            "sshHostVerification": "system-known-hosts",
            "errorCode": "production_ssh_probe_failed",
            "errorClass": exc.__class__.__name__,
        }
    finally:
        try:
            client.close()
        except Exception:
            pass
    return {
        "status": status,
        "targetRedacted": True,
        "sshHostVerification": "system-known-hosts",
        "authMethod": "password-from-operator-file",
        "exitCode": exit_code,
        "parsed": parse_production_output(out),
        "stderrPresent": bool(err.strip()),
    }


def capture_git_state() -> dict[str, Any]:
    status = run_local(["git", "status", "--short"])
    head = run_local(["git", "rev-parse", "HEAD"])
    diff = run_local(["git", "diff", "--", "channel/web/web_channel.py", "tests/test_ecorex_web_parallel_backend.py"])
    return {
        "head": (head.get("stdout") or "").strip(),
        "statusShort": status.get("stdout", ""),
        "knownPreexistingEdits": [
            "channel/web/web_channel.py enterprise-login local fallback hardening",
            "tests/test_ecorex_web_parallel_backend.py corresponding assertion update",
        ],
        "trackedDiffHash": sha256_text(diff.get("stdout", "")) if diff.get("stdout") else "",
    }


def dependency_execution_map() -> list[dict[str, Any]]:
    return [
        {
            "dependency": "node",
            "currentResolvers": ["system PATH", "Playwright private node on Windows package only"],
            "targetResolver": "RuntimeDependencyProvider.resolve_executable('node')",
            "expectedOwner": "ecorex-bundled or ecorex-state",
            "usedBy": ["MCP", "Office artifact JS", "PDF JS renderers", "Feishu/Lark CLI runner"],
        },
        {
            "dependency": "npm/npx",
            "currentResolvers": ["system PATH", "package config mcp command"],
            "targetResolver": "RuntimeDependencyProvider.resolve_executable('npm'/'npx')",
            "expectedOwner": "ecorex-bundled or ecorex-state",
            "usedBy": ["capability installs", "MCP bootstrap", "official CLI installs"],
        },
        {
            "dependency": "lark-cli",
            "currentResolvers": ["system PATH", "C:/cli-main", "runtime tools path if present"],
            "targetResolver": "ToolExecutionEnvironment for feishu_cli",
            "expectedOwner": "ecorex-state or ecorex-bundled",
            "usedBy": ["Feishu/Lark structured CLI canary"],
        },
        {
            "dependency": "xin_agent_cli.py",
            "currentResolvers": ["explicit config", "env vars", "trusted local roots", "bootstrap config"],
            "targetResolver": "ToolExecutionEnvironment for tongxin_cli plus verified bootstrap",
            "expectedOwner": "ecorex-state or explicit operator-approved path",
            "usedBy": ["Tongxin structured read-only CLI canary"],
        },
        {
            "dependency": "python",
            "currentResolvers": ["sys.executable", "packaged python", "service venv"],
            "targetResolver": "RuntimeDependencyProvider.python()",
            "expectedOwner": "ecorex-bundled",
            "usedBy": ["all Python tools", "Office/PDF", "Imagegen", "diagnostics"],
        },
    ]


def baseline_gap_signals(report: dict[str, Any]) -> dict[str, Any]:
    local_feishu = (
        report.get("ecorexLocalWebui", {})
        .get("toolStatuses", {})
        .get("feishuCli", {})
    )
    production = report.get("production", {})
    prod_parsed = production.get("parsed") if isinstance(production.get("parsed"), dict) else {}
    prod_tools = prod_parsed.get("toolStatuses") if isinstance(prod_parsed.get("toolStatuses"), dict) else {}
    prod_service = prod_parsed.get("serviceUser") if isinstance(prod_parsed.get("serviceUser"), dict) else {}
    artifacts = report.get("ecorexArtifacts") if isinstance(report.get("ecorexArtifacts"), list) else []
    by_label = {item.get("label"): item for item in artifacts if isinstance(item, dict)}
    return {
        "schemaVersion": "v025-runtime-gap-signals-v1",
        "localFeishuCommandUsesCodexPrivatePath": local_feishu.get("commandSource") == "codex-private",
        "localFeishuUsesSystemPackageManager": local_feishu.get("npmSource") == "system-path" or local_feishu.get("npxSource") == "system-path",
        "productionServiceUserNodeMissing": (prod_service.get("node") or {}).get("source") == "missing",
        "productionServiceUserNpmMissing": (prod_service.get("npm") or {}).get("source") == "missing",
        "productionFeishuCliMissing": (prod_tools.get("feishuCli") or {}).get("authState") == "cli_missing",
        "productionTongxinCliMissingConfig": (prod_tools.get("tongxinCli") or {}).get("configurationState") == "missing",
        "windowsArtifactOnlyHasPlaywrightNode": by_label.get("webui-windows-x64", {}).get("runtimeHitCount") == 1,
        "macosArtifactHasNoRuntimeNode": by_label.get("webui-macos-universal", {}).get("runtimeHitCount") == 0,
        "linuxArtifactHasNoRuntimeNode": by_label.get("web-linux-service", {}).get("runtimeHitCount") == 0,
        "publicReleaseContainsNestedArtifacts": bool(by_label.get("public-release", {}).get("nestedArtifacts")),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--include-production", action="store_true", help="Run pinned-known-host production service-user probe.")
    args = parser.parse_args()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    report = {
        "schemaVersion": "v025-runtime-baseline-v1",
        "version": VERSION,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "script": {
            "path": "scripts/capture-v025-runtime-baseline.py",
            "sha256": file_sha256(ROOT / "scripts" / "capture-v025-runtime-baseline.py"),
            "argv": ["scripts/capture-v025-runtime-baseline.py"] + (["--include-production"] if args.include_production else []),
        },
        "classificationLegend": {
            "ecorex-bundled": "Dependency is inside an EcoreX runtime/release tree.",
            "ecorex-state": "Dependency is inside writable EcoreX user/service state.",
            "system-path": "Dependency is resolved from the host system PATH.",
            "codex-private": "Dependency belongs to Codex/WorkBuddy/development-only private runtimes.",
            "missing": "Dependency is unavailable.",
        },
        "baselineComparisonVersion": BASELINE_COMPARISON_VERSION,
        "baselineComparisonPurpose": "prior-version runtime gap evidence for v0.2.5 implementation; not v0.2.5 supply-chain assurance",
        "dependencyExecutionMap": dependency_execution_map(),
        "git": capture_git_state(),
        "systemPath": {
            "node": command_path("node"),
            "npm": command_path("npm"),
            "npx": command_path("npx"),
            "larkCli": command_path("lark-cli"),
        },
        "codexRuntime": capture_codex_runtime(),
        "ecorexLocalWebui": capture_local_ecorex(),
        "ecorexArtifacts": [
            scan_artifact("webui-windows-x64", ROOT / "release-artifacts" / f"EcoreX_{BASELINE_COMPARISON_VERSION}-webui-windows-x64.zip"),
            scan_artifact("webui-macos-universal", ROOT / "release-artifacts" / f"EcoreX_{BASELINE_COMPARISON_VERSION}-webui-macos-universal.zip"),
            scan_artifact("web-linux-service", ROOT / "release-artifacts" / f"EcoreX_{BASELINE_COMPARISON_VERSION}-web-linux-service.tar.gz"),
            scan_artifact("public-release", ROOT / "release-artifacts" / f"EcoreX_{BASELINE_COMPARISON_VERSION}-public-release.zip"),
        ],
        "production": capture_production(include_production=args.include_production),
    }
    report["baselineGapSignals"] = baseline_gap_signals(report)
    OUT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(str(OUT_PATH))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
