#!/usr/bin/env python3
"""Run 200 production user-behavior checks for the current Web release.

The script reads the operator server file at runtime through the existing
deployment helper, executes the main test matrix on the production server, and
persists only redacted/hash evidence locally.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import sys
import textwrap
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import paramiko


ROOT = Path(__file__).resolve().parents[1]
VERSION = os.environ.get("ECOREX_ACCEPTANCE_VERSION") or os.environ.get("ECOREX_DEPLOY_VERSION") or "0.2.8"
ARTIFACT = ROOT / "docs" / f"v{VERSION}" / "artifacts" / "production-200-user-behavior.json"
REMOTE_MARKER = "__ECOREX_PRODUCTION_200_USER_BEHAVIOR_JSON__"


def _load_deploy_module():
    spec = importlib.util.spec_from_file_location("deploy_v024_production", ROOT / "scripts" / "deploy-v024-production.py")
    if spec is None or spec.loader is None:
        raise RuntimeError("Unable to load deploy-v024-production.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _sha_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest().upper()


def _extract_remote_json(stdout: str) -> dict[str, Any]:
    marker = REMOTE_MARKER
    index = stdout.rfind(marker)
    if index < 0:
        raise RuntimeError("Remote smoke JSON marker missing")
    payload = stdout[index + len(marker):].strip()
    return json.loads(payload)


REMOTE_SCRIPT = r"""
import base64
import hashlib
import http.cookiejar
import io
import json
import os
import re
import signal
import shutil
import ssl
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path

VERSION = "__VERSION__"
DOMAIN = "__DOMAIN__"
PUBLIC_BASE = "https://" + DOMAIN + "/ecorex-agent"
LOCAL_BASE = "http://127.0.0.1:9909"
VALIDATION_TMP_ROOT = Path("/srv/ecorex-agent-download/validation-tmp")
VALIDATION_TMP_ROOT.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("TMPDIR", str(VALIDATION_TMP_ROOT))
tempfile.tempdir = str(VALIDATION_TMP_ROOT)
TMP = Path(tempfile.mkdtemp(prefix="ecorex-v027-200-", dir=str(VALIDATION_TMP_ROOT)))
CHECKS = []
HTTP = {}
DOWNLOADS = {}


def sha_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def add(group, name, ok, detail=None):
    CHECKS.append({
        "index": len(CHECKS) + 1,
        "group": group,
        "name": name,
        "status": "PASS" if bool(ok) else "FAIL",
        "detail": detail or {},
    })


def read_text(path, max_bytes=2_000_000):
    try:
        with open(path, "rb") as handle:
            return handle.read(max_bytes).decode("utf-8", errors="replace")
    except Exception:
        return ""


def read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except Exception:
        return {}


def run(args, timeout=30, cwd=None):
    try:
        return subprocess.run(args, text=True, capture_output=True, timeout=timeout, cwd=cwd)
    except Exception as exc:
        class Result:
            returncode = 999
            stdout = ""
            stderr = str(exc)
        return Result()


def request(url, method="GET", data=None, headers=None, timeout=25, opener=None, read_limit=2_000_000):
    body = None if data is None else json.dumps(data).encode("utf-8")
    req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    if data is not None and "Content-Type" not in req.headers:
        req.add_header("Content-Type", "application/json")
    context = ssl._create_unverified_context() if url.startswith("https://") else None
    started = time.time()
    try:
        open_fn = opener.open if opener is not None else urllib.request.urlopen
        kwargs = {"timeout": timeout}
        if context is not None and opener is None:
            kwargs["context"] = context
        with open_fn(req, **kwargs) as resp:
            data_bytes = resp.read(read_limit)
            text = data_bytes.decode("utf-8", errors="replace")
            parsed = None
            try:
                parsed = json.loads(text)
            except Exception:
                pass
            return {
                "ok": 200 <= resp.status < 400,
                "status": resp.status,
                "headers": {k.lower(): v for k, v in resp.headers.items()},
                "text": text,
                "json": parsed,
                "bytes": data_bytes,
                "latencyMs": int((time.time() - started) * 1000),
            }
    except urllib.error.HTTPError as exc:
        data_bytes = exc.read(read_limit)
        text = data_bytes.decode("utf-8", errors="replace")
        parsed = None
        try:
            parsed = json.loads(text)
        except Exception:
            pass
        return {
            "ok": False,
            "status": exc.code,
            "headers": {k.lower(): v for k, v in exc.headers.items()},
            "text": text,
            "json": parsed,
            "bytes": data_bytes,
            "latencyMs": int((time.time() - started) * 1000),
        }
    except Exception as exc:
        return {
            "ok": False,
            "status": 0,
            "headers": {},
            "text": "",
            "json": None,
            "bytes": b"",
            "latencyMs": int((time.time() - started) * 1000),
            "errorType": exc.__class__.__name__,
            "error": str(exc)[:240],
        }


def head(url, timeout=25):
    return request(url, method="HEAD", timeout=timeout, read_limit=0)


def published_artifact_path(url):
    prefix = PUBLIC_BASE.rstrip("/") + "/"
    if not url.startswith(prefix):
        return None
    relative = urllib.parse.unquote(url[len(prefix):].split("?", 1)[0]).lstrip("/")
    if not relative.startswith("downloads/"):
        return None
    root = Path("/srv/ecorex-agent-download/current").resolve()
    candidate = (root / relative).resolve()
    if root not in candidate.parents:
        return None
    return candidate if candidate.is_file() else None


def download(url, dest, timeout=300):
    local_source = published_artifact_path(url)
    if local_source is not None:
        try:
            os.link(local_source, dest)
        except Exception:
            shutil.copy2(local_source, dest)
        return {
            "status": 200,
            "headers": {"content-length": str(Path(dest).stat().st_size)},
            "latencyMs": 0,
            "validationSource": "published-local-file",
        }
    req = urllib.request.Request(url, method="GET")
    context = ssl._create_unverified_context()
    started = time.time()
    with urllib.request.urlopen(req, timeout=timeout, context=context) as resp:
        with open(dest, "wb") as handle:
            shutil.copyfileobj(resp, handle)
        return {
            "status": resp.status,
            "headers": {k.lower(): v for k, v in resp.headers.items()},
            "latencyMs": int((time.time() - started) * 1000),
        }


def find_static_asset(app_index, suffix):
    match = re.search(r'(?:src|href)="([^"]+' + re.escape(suffix) + r')"', app_index)
    return match.group(1) if match else ""


def artifact_by_id(manifest, artifact_id):
    for item in manifest.get("artifacts") or []:
        if item.get("id") == artifact_id:
            return item
    return {}


def valid_release_date(value):
    text = str(value or "")
    return bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", text)) and text >= "2026-07-01"


def archive_names(path):
    if str(path).endswith(".zip"):
        with zipfile.ZipFile(path) as archive:
            return archive.namelist()
    with tarfile.open(path, "r:gz") as archive:
        return archive.getnames()


def archive_name_matches(name, suffix):
    return str(name or "").replace("\\", "/").endswith(suffix)


def archive_read(path, suffix, max_bytes=1_500_000):
    if str(path).endswith(".zip"):
        with zipfile.ZipFile(path) as archive:
            for name in archive.namelist():
                if archive_name_matches(name, suffix):
                    return archive.read(name)[:max_bytes].decode("utf-8", errors="replace")
        return ""
    with tarfile.open(path, "r:gz") as archive:
        for member in archive.getmembers():
            if archive_name_matches(member.name, suffix):
                fileobj = archive.extractfile(member)
                if fileobj:
                    return fileobj.read(max_bytes).decode("utf-8", errors="replace")
    return ""


def archive_has(path, suffix):
    return any(archive_name_matches(name, suffix) for name in archive_names(path))


def phase_deployment():
    current = Path("/opt/ecorex-web/current")
    release = read_json(current / "release.json")
    runtime_manifest = read_json(current / "runtime" / "runtime-manifest.json")
    gate = read_json("/opt/ecorex-web/state/web-release-gate.json")
    add("deployment", "current release directory exists", current.exists())
    add("deployment", "release.json exists", (current / "release.json").is_file())
    add("deployment", f"release version is v{VERSION}", release.get("version") == VERSION, {"version": release.get("version")})
    add("deployment", "release artifact is web-linux-service", release.get("artifactId") == "web-linux-service")
    add("deployment", "runtime app.py exists", (current / "runtime" / "app.py").is_file())
    add("deployment", "runtime web_channel.py exists", (current / "runtime" / "channel" / "web" / "web_channel.py").is_file())
    add("deployment", "runtime auth.py exists", (current / "runtime" / "channel" / "web" / "auth.py").is_file())
    add("deployment", "runtime static app index exists", (current / "runtime" / "channel" / "web" / "static" / "app" / "index.html").is_file())
    add("deployment", "state config exists", Path("/opt/ecorex-web/state/config.json").is_file())
    add("deployment", "runtime manifest exists", bool(runtime_manifest))
    add("deployment", "capability state exists", Path("/opt/ecorex-web/state/capability-state.json").is_file())
    add("deployment", "permission matrix exists", Path("/opt/ecorex-web/state/permission-matrix.json").is_file())
    add("deployment", "review consensus exists", Path("/opt/ecorex-web/state/review-consensus.md").is_file())
    add("deployment", "web release gate exists", bool(gate))
    add("deployment", "bundled node exists", Path("/opt/ecorex-web/node/bin/node").is_file())
    add("deployment", "bundled npm exists", Path("/opt/ecorex-web/node/bin/npm").is_file())
    add("deployment", "bundled npx exists", Path("/opt/ecorex-web/node/bin/npx").is_file())
    add("deployment", "systemd service active", run(["systemctl", "is-active", "ecorex-web"]).stdout.strip() == "active")
    add("deployment", "systemd service enabled", run(["systemctl", "is-enabled", "ecorex-web"]).stdout.strip() == "enabled")
    add("deployment", "workspace directory exists", Path("/srv/ecorex-agent-workspace").is_dir())
    add("deployment", "installed OpenAI logo exists", (current / "runtime" / "channel" / "web" / "static" / "app" / "assets" / "logos" / "openai.svg").is_file())


def phase_public_http():
    public_index = request(PUBLIC_BASE + "/", timeout=25)
    manifest_resp = request(PUBLIC_BASE + "/manifest.json", timeout=25)
    app_resp = request(PUBLIC_BASE + "/app/", timeout=25)
    version_resp = request(PUBLIC_BASE + "/api/version", timeout=25)
    auth_resp = request(PUBLIC_BASE + "/auth/check", timeout=25)
    admin_resp = request(PUBLIC_BASE + "/admin/", timeout=25)
    gate_resp = request(PUBLIC_BASE + "/client/model-config", timeout=25)
    app_index = app_resp["text"]
    js_asset = find_static_asset(app_index, ".js")
    css_asset = find_static_asset(app_index, ".css")
    js_resp = request(PUBLIC_BASE + "/app/" + js_asset.lstrip("./"), timeout=25) if js_asset else {"status": 0, "ok": False, "text": "", "headers": {}}
    css_resp = request(PUBLIC_BASE + "/app/" + css_asset.lstrip("./"), timeout=25) if css_asset else {"status": 0, "ok": False, "text": "", "headers": {}}
    HTTP.update({
        "publicIndex": public_index,
        "manifest": manifest_resp,
        "app": app_resp,
        "version": version_resp,
        "authCheck": auth_resp,
        "admin": admin_resp,
        "clientGate": gate_resp,
        "appJs": js_resp,
        "appCss": css_resp,
    })
    add("public-http", "public index returns 200", public_index["status"] == 200)
    add("public-http", "public manifest returns 200", manifest_resp["status"] == 200)
    add("public-http", "public app returns 200", app_resp["status"] == 200)
    add("public-http", "public app references bundled script", bool(js_asset), {"asset": js_asset.split("/")[-1]})
    add("public-http", "public api version returns 200", version_resp["status"] == 200)
    add("public-http", f"public api version is v{VERSION}", (version_resp.get("json") or {}).get("version") == VERSION)
    add("public-http", "public auth check returns 200", auth_resp["status"] == 200)
    add("public-http", "public admin requires auth", admin_resp["status"] == 401)
    add("public-http", "public client model config rejects anonymous", gate_resp["status"] == 403)
    add("public-http", "public icon asset returns 200", request(PUBLIC_BASE + "/assets/icon.png", timeout=25)["status"] == 200)
    add("public-http", "public app js returns 200", js_resp["status"] == 200)
    add("public-http", "public app css returns 200", css_resp["status"] == 200)
    for provider in ("openai", "deepseek", "gemini", "doubao"):
        resp = request(PUBLIC_BASE + f"/app/assets/logos/{provider}.svg", timeout=25)
        add("public-http", f"{provider} logo returns 200", resp["status"] == 200 and len(resp["bytes"]) > 300)
    manifest = manifest_resp.get("json") or read_json("/srv/ecorex-agent-download/current/manifest.json")
    for artifact_id in ("webui-windows-x64", "webui-macos-universal", "web-linux-service"):
        artifact = artifact_by_id(manifest, artifact_id)
        url = PUBLIC_BASE + "/" + str(artifact.get("href") or "")
        resp = head(url, timeout=30)
        HTTP[f"head:{artifact_id}"] = resp
        add("public-http", f"{artifact_id} download HEAD returns 200", resp["status"] == 200)
        add("public-http", f"{artifact_id} download content-length matches manifest", str(artifact.get("size")) == str(resp["headers"].get("content-length")))
    range_web = request(PUBLIC_BASE + "/" + str(artifact_by_id(manifest, "web-linux-service").get("href") or ""), headers={"Range": "bytes=0-4095"}, timeout=30, read_limit=8192)
    range_win = request(PUBLIC_BASE + "/" + str(artifact_by_id(manifest, "webui-windows-x64").get("href") or ""), headers={"Range": "bytes=0-4095"}, timeout=30, read_limit=8192)
    add("public-http", "web service range download works", range_web["status"] in (200, 206) and len(range_web["bytes"]) > 1000)
    add("public-http", "windows package range download works", range_win["status"] in (200, 206) and len(range_win["bytes"]) > 1000)
    add("public-http", "public index names EcoreX", "EcoreX" in public_index["text"])


def phase_manifest():
    manifest = HTTP.get("manifest", {}).get("json") or read_json("/srv/ecorex-agent-download/current/manifest.json")
    manifest_updated_at = str(manifest.get("updatedAt") or "")
    add("manifest", "manifest product is EcoreX", manifest.get("product") == "EcoreX")
    add("manifest", f"manifest version is v{VERSION}", manifest.get("version") == VERSION)
    add("manifest", "manifest notes mention WebUI", "WebUI" in str(manifest.get("notes") or ""))
    add("manifest", "win recommended download is WebUI", ((manifest.get("recommendedDownloads") or {}).get("win32") or {}).get("primary") == "webui-windows-x64")
    add("manifest", "mac recommended download is WebUI", ((manifest.get("recommendedDownloads") or {}).get("darwin") or {}).get("primary") == "webui-macos-universal")
    add("manifest", "web recommended Windows package exists", ((manifest.get("recommendedDownloads") or {}).get("web") or {}).get("windows") == "webui-windows-x64")
    artifact_ids = ((manifest.get("update") or {}).get("webui") or {}).get("artifactIds") or []
    add("manifest", "update channel includes Windows WebUI", "webui-windows-x64" in artifact_ids)
    add("manifest", "update channel includes macOS WebUI", "webui-macos-universal" in artifact_ids)
    expected_platform = {
        "webui-windows-x64": "Windows",
        "webui-macos-universal": "macOS",
        "web-linux-service": "Linux",
    }
    for artifact_id in ("webui-windows-x64", "webui-macos-universal", "web-linux-service"):
        artifact = artifact_by_id(manifest, artifact_id)
        head_resp = HTTP.get(f"head:{artifact_id}") or {}
        add("manifest", f"{artifact_id} status ready", artifact.get("status") == "ready")
        add("manifest", f"{artifact_id} version v{VERSION}", artifact.get("version") == VERSION)
        add("manifest", f"{artifact_id} size matches HTTP", str(artifact.get("size")) == str((head_resp.get("headers") or {}).get("content-length")))
        add("manifest", f"{artifact_id} sha256 uppercase", bool(re.fullmatch(r"[0-9A-F]{64}", str(artifact.get("sha256") or ""))))
        add("manifest", f"{artifact_id} href is downloads path", str(artifact.get("href") or "").startswith("downloads/"))
        add("manifest", f"{artifact_id} file name contains version", VERSION in str(artifact.get("fileName") or ""))
        add("manifest", f"{artifact_id} updated at manifest release date", artifact.get("updatedAt") == manifest_updated_at and valid_release_date(manifest_updated_at), {"manifestUpdatedAt": manifest_updated_at})
        add("manifest", f"{artifact_id} platform expected", artifact.get("platform") == expected_platform[artifact_id])
        add("manifest", f"{artifact_id} id resolves uniquely", sum(1 for item in manifest.get("artifacts") or [] if item.get("id") == artifact_id) == 1)


def phase_api():
    password = ""
    for line in read_text("/etc/ecorex-web/ecorex-web.env").splitlines():
        if line.startswith("WEB_PASSWORD="):
            password = line.split("=", 1)[1].strip().strip('"')
            break
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    pre_auth = request(LOCAL_BASE + "/auth/check", opener=opener)
    login = request(LOCAL_BASE + "/auth/login", method="POST", data={"email": "server-200-smoke@ecorex.local", "password": password}, opener=opener)
    login_json = login.get("json") or {}
    session = login_json.get("session") or {}
    user = session.get("user") or {}
    set_perm = request(LOCAL_BASE + "/api/tool-permissions", method="POST", data={"action": "set_mode", "mode": "full-access"}, opener=opener)
    perm = request(LOCAL_BASE + "/api/tool-permissions", opener=opener)
    version = request(LOCAL_BASE + "/api/version", opener=opener)
    models = request(LOCAL_BASE + "/api/models", opener=opener)
    tools = request(LOCAL_BASE + "/api/tools", opener=opener)
    skills = request(LOCAL_BASE + "/api/skills", opener=opener)
    extensions = request(LOCAL_BASE + "/api/extensions", opener=opener)
    capabilities = request(LOCAL_BASE + "/api/capabilities", opener=opener)
    external = request(LOCAL_BASE + "/api/external-connections", opener=opener)
    channels = request(LOCAL_BASE + "/api/channels", opener=opener)
    scheduler = request(LOCAL_BASE + "/api/scheduler", opener=opener)
    active = request(LOCAL_BASE + "/api/active-requests", opener=opener)
    installs = request(LOCAL_BASE + "/api/installations", opener=opener)
    providers = (models.get("json") or {}).get("providers") or []
    provider_text = json.dumps(providers, ensure_ascii=False).lower()
    combined_tools = json.dumps({
        "tools": tools.get("json"),
        "skills": skills.get("json"),
        "extensions": extensions.get("json"),
        "capabilities": capabilities.get("json"),
        "channels": channels.get("json"),
    }, ensure_ascii=False).lower()
    add("runtime-api", "pre-login auth check returns 200", pre_auth["status"] == 200)
    add("runtime-api", "pre-login auth is required", (pre_auth.get("json") or {}).get("auth_required") is True)
    add("runtime-api", "pre-login is unauthenticated", (pre_auth.get("json") or {}).get("authenticated") is False)
    add("runtime-api", "login returns 200", login["status"] == 200)
    add("runtime-api", "login status success", login_json.get("status") == "success")
    add("runtime-api", "login session authenticated", session.get("authenticated") is True)
    add("runtime-api", "login session is not local fallback", session.get("localFallback") is False)
    add("runtime-api", "login auth provider web-password", session.get("authProvider") == "web-password")
    add("runtime-api", "login user is smoke email", user.get("email") == "server-200-smoke@ecorex.local")
    add("runtime-api", "login cookie persisted", len(list(jar)) > 0)
    add("runtime-api", "set full-access returns 200", set_perm["status"] == 200)
    add("runtime-api", "set full-access mode accepted", (set_perm.get("json") or {}).get("mode") == "full-access")
    add("runtime-api", "permission state returns 200", perm["status"] == 200)
    add("runtime-api", "permission mode is full-access", (perm.get("json") or {}).get("mode") == "full-access")
    add("runtime-api", "permission audit path present", bool((perm.get("json") or {}).get("auditPath")))
    add("runtime-api", "version endpoint returns 200", version["status"] == 200)
    add("runtime-api", "models endpoint returns 200", models["status"] == 200)
    add("runtime-api", "model providers count at least four", len(providers) >= 4)
    add("runtime-api", "models expose capabilities", bool((models.get("json") or {}).get("capabilities")))
    for provider in ("openai", "deepseek", "gemini", "doubao"):
        add("runtime-api", f"provider {provider} present", provider in provider_text)
    add("runtime-api", "doubao uses seed 2.0 route", "seed-2-0" in provider_text and "seed-2-1" not in provider_text)
    add("runtime-api", "tools endpoint returns 200", tools["status"] == 200)
    add("runtime-api", "tools use runtime capability service", (tools.get("json") or {}).get("source") == "runtime-capability-service")
    for marker in ("browser", "imagegen", "ocr"):
        add("runtime-api", f"tools expose {marker}", marker in combined_tools)
    add("runtime-api", "skills endpoint returns 200", skills["status"] == 200)
    add("runtime-api", "skills payload has count", "skillCount" in (skills.get("json") or {}))
    add("runtime-api", "extensions endpoint returns 200", extensions["status"] == 200)
    add("runtime-api", "capabilities endpoint returns 200", capabilities["status"] == 200)
    add("runtime-api", "capabilities use runtime capability service", (capabilities.get("json") or {}).get("source") == "runtime-capability-service")
    add("runtime-api", "external connections endpoint returns 200", external["status"] == 200)
    add("runtime-api", "channels endpoint returns 200", channels["status"] == 200)
    add("runtime-api", "feishu channel present", "feishu" in combined_tools or "lark" in combined_tools)
    add("runtime-api", "scheduler endpoint returns 200", scheduler["status"] == 200)
    add("runtime-api", "active requests endpoint returns 200", active["status"] == 200)
    add("runtime-api", "installations endpoint returns 200", installs["status"] == 200)


def phase_v026_markers():
    runtime = Path("/opt/ecorex-web/current/runtime")
    app_index = read_text(runtime / "channel" / "web" / "static" / "app" / "index.html")
    js_asset = find_static_asset(app_index, ".js")
    css_asset = find_static_asset(app_index, ".css")
    app_js = read_text(runtime / "channel" / "web" / "static" / "app" / js_asset.lstrip("./"))
    app_css = read_text(runtime / "channel" / "web" / "static" / "app" / css_asset.lstrip("./"))
    web_channel = read_text(runtime / "channel" / "web" / "web_channel.py")
    auth_py = read_text(runtime / "channel" / "web" / "auth.py")
    browser_service = read_text(runtime / "agent" / "tools" / "browser" / "browser_service.py")
    imagegen = read_text(runtime / "agent" / "tools" / "imagegen" / "imagegen.py")
    provider = read_text(runtime / "agent" / "tools" / "imagegen" / "provider_runner.py")
    agent_stream = read_text(runtime / "agent" / "protocol" / "agent_stream.py")
    prompt_builder = read_text(runtime / "agent" / "prompt" / "builder.py")
    skill_image = read_text(runtime / "skills" / "image-generation" / "SKILL.md")
    add("v026-markers", "bridge purges generic local session", "function purgeGenericLocalSession()" in web_channel)
    add("v026-markers", "bridge gates local fallback explicitly", "function allowLocalSessionFallback()" in web_channel)
    add("v026-markers", "bridge local fallback flag is opt-in", "ECOREX_ALLOW_LOCAL_SESSION_FALLBACK" in web_channel)
    add("v026-markers", "bridge sends bearer authorization", 'headers["Authorization"] = "Bearer " + session.token;' in web_channel)
    add("v026-markers", "bridge exposes enterprise model config fallback", "getEnterpriseModelConfig: async function" in web_channel)
    add("v026-markers", "bridge no longer returns anonymous local session", "return webSession(false, true, null, true)" not in web_channel)
    add("v026-markers", "bridge rejects login without real runtime session", "登录成功但运行时未返回有效会话" in web_channel)
    add("v026-markers", "auth payload tracks local fallback explicitly", '"localFallback": not has_provided_identity' in auth_py)
    add("v026-markers", "renderer calls enterprise model config fallback", "getEnterpriseModelConfig" in app_js)
    add("v026-markers", "model switch renders paging divider element", "model-switch-divider" in app_js)
    add("v026-markers", "model switch divider is context-excluded", "contextExcluded" in app_js and "model-switch-divider" in app_js)
    add("v026-markers", "model switch divider CSS is bundled", ".model-switch-divider" in app_css)
    for provider_name in ("openai", "deepseek", "gemini", "doubao"):
        logo = runtime / "channel" / "web" / "static" / "app" / "assets" / "logos" / f"{provider_name}.svg"
        add("v026-markers", f"{provider_name} provider logo is bundled", logo.is_file() and logo.stat().st_size > 300)
    add("v026-markers", "browser screenshot has native CDP fallback", "Page.captureScreenshot" in browser_service)
    add("v026-markers", "browser screenshot decodes CDP image data", "base64.b64decode(image_data)" in browser_service)
    add("v026-markers", "imagegen accepts native batch tasks", '"tasks"' in imagegen)
    add("v026-markers", "imagegen batch reports native route", "native_imagegen_tool_loop" in imagegen)
    add("v026-markers", "imagegen reports credential-needed accurately", "needs_provider_credentials" in provider)
    add("v026-markers", "imagegen ignores legacy skill model unless opted in", "ECOREX_IMAGEGEN_ALLOW_SKILL_MODEL" in provider)
    add("v026-markers", "agent blocks shell python image fallback", "Do not fall back to shell/Python/PIL/SVG/canvas" in agent_stream)
    add("v026-markers", "agent detects raw shell image generation", "_looks_like_image_generation_shell_command" in agent_stream)
    add("v026-markers", "prompt prefers native imagegen", "native `imagegen`" in prompt_builder or "native imagegen" in prompt_builder.lower())
    add("v026-markers", "image skill forbids shell fallback", "Do not replace generation or edits\nwith shell/Python" in skill_image or "Do not replace generation or edits" in skill_image)
    add("v026-markers", "bundled Tongxin CLI exists", (runtime / "tools" / "tongxin" / "xin_agent_cli.py").is_file())
    add("v026-markers", "bundled Tongxin models package exists", (runtime / "tools" / "tongxin" / "models" / "__init__.py").is_file())
    add("v026-markers", "Tongxin models exports DATABASE", "DATABASE" in read_text(runtime / "tools" / "tongxin" / "models" / "__init__.py"))
    add("v026-markers", "Tongxin database module exports database", "database = DATABASE" in read_text(runtime / "tools" / "tongxin" / "database.py"))
    add("v026-markers", "root models compatibility exports database", "DATABASE = database" in read_text(runtime / "models" / "__init__.py"))
    add("v026-markers", "token estimator is bundled", (runtime / "models" / "token_estimator.py").is_file())
    add("v026-markers", "model capabilities include context guidance", "contextWindow" in read_text(runtime / "models" / "model_capabilities.py") or "context_window" in read_text(runtime / "models" / "model_capabilities.py"))
    add("v026-markers", "static app bundles provider logos", all((runtime / "channel" / "web" / "static" / "app" / "assets" / "logos" / f"{provider_name}.svg").is_file() for provider_name in ("openai", "deepseek", "gemini", "doubao")))


def phase_downloads():
    manifest = HTTP.get("manifest", {}).get("json") or read_json("/srv/ecorex-agent-download/current/manifest.json")
    for artifact_id in ("webui-windows-x64", "webui-macos-universal", "web-linux-service"):
        artifact = artifact_by_id(manifest, artifact_id)
        dest = TMP / str(artifact.get("fileName") or artifact_id)
        meta = download(PUBLIC_BASE + "/" + str(artifact.get("href") or ""), dest, timeout=900)
        DOWNLOADS[artifact_id] = {"path": str(dest), "artifact": artifact}
        size = dest.stat().st_size if dest.exists() else 0
        digest = sha_file(dest) if dest.exists() else ""
        names = []
        try:
            names = archive_names(dest)
        except Exception:
            names = []
        add("downloads", f"{artifact_id} full download returns 200", meta.get("status") == 200)
        add("downloads", f"{artifact_id} full download size matches", size == int(artifact.get("size") or -1))
        add("downloads", f"{artifact_id} full download sha256 matches", digest == artifact.get("sha256"))
        add("downloads", f"{artifact_id} archive opens", bool(names))
        add("downloads", f"{artifact_id} archive has runtime manifest", any(archive_name_matches(name, "runtime/runtime-manifest.json") for name in names))
        add("downloads", f"{artifact_id} archive has Web app index", any(archive_name_matches(name, "runtime/channel/web/static/app/index.html") for name in names))


def phase_archive_contents():
    for artifact_id in ("webui-windows-x64", "webui-macos-universal", "web-linux-service"):
        path = DOWNLOADS.get(artifact_id, {}).get("path")
        add("archive-contents", f"{artifact_id} archive path captured", bool(path and Path(path).exists()))
        if not path or not Path(path).exists():
            for suffix in ("web_channel marker", "browser fallback", "imagegen batch", "renderer app js"):
                add("archive-contents", f"{artifact_id} {suffix}", False)
            continue
        web_channel = archive_read(path, "runtime/channel/web/web_channel.py")
        browser_service = archive_read(path, "runtime/agent/tools/browser/browser_service.py")
        imagegen = archive_read(path, "runtime/agent/tools/imagegen/imagegen.py")
        names = archive_names(path)
        add("archive-contents", f"{artifact_id} contains local-session purge bridge", "purgeGenericLocalSession" in web_channel)
        add("archive-contents", f"{artifact_id} contains CDP screenshot fallback", "Page.captureScreenshot" in browser_service)
        add("archive-contents", f"{artifact_id} contains native imagegen batch", "native_imagegen_tool_loop" in imagegen)
        add("archive-contents", f"{artifact_id} contains compiled renderer JS", any(archive_name_matches(name, ".js") and "/static/app/assets/" in str(name).replace("\\", "/") for name in names))


def phase_browser_toolchain():
    os.chdir("/opt/ecorex-web/current/runtime")
    sys.path.insert(0, "/opt/ecorex-web/current/runtime")
    browser_tool_source = read_text("/opt/ecorex-web/current/runtime/agent/tools/browser/browser_tool.py")
    browser_service_source = read_text("/opt/ecorex-web/current/runtime/agent/tools/browser/browser_service.py")
    browser_automation_source = read_text("/opt/ecorex-web/current/runtime/agent/tools/browser/browser_automation_service.py")
    browser_smoke = {
        "browser_ok": "class BrowserTool" in browser_tool_source and '"navigate"' in browser_tool_source,
        "snapshot_ok": '"snapshot"' in browser_tool_source,
        "screenshot_ok": '"screenshot"' in browser_tool_source and "Page.captureScreenshot" in browser_service_source,
        "probe": "static-runtime-source",
    }
    diagnostics = {
        "mode": "cdp-first" if "cdp-first" in browser_automation_source else "",
        "autoLaunch": 'setdefault("cdp_auto_launch", True)' in browser_tool_source or "cdp_auto_launch" in browser_automation_source,
        "fallbackEnabled": 'setdefault("cdp_fallback", True)' in browser_tool_source or "cdp_fallback" in browser_automation_source,
    }
    node = run(["/opt/ecorex-web/node/bin/node", "--version"])
    npm = run(["/opt/ecorex-web/node/bin/npm", "--version"])
    npx = run(["/opt/ecorex-web/node/bin/npx", "--version"])
    py_checks = {
        "playwright": run(["/opt/ecorex-web/venv/bin/python", "-c", "import playwright"], timeout=20).returncode == 0,
        "rapidocr": run(["/opt/ecorex-web/venv/bin/python", "-c", "import rapidocr_onnxruntime"], timeout=20).returncode == 0,
        "PIL": run(["/opt/ecorex-web/venv/bin/python", "-c", "import PIL"], timeout=20).returncode == 0,
    }
    add("browser-toolchain", "BrowserTool imports and navigates", browser_smoke.get("browser_ok") is True, browser_smoke)
    add("browser-toolchain", "BrowserTool snapshot reads page", browser_smoke.get("snapshot_ok") is True)
    add("browser-toolchain", "BrowserTool screenshot writes file", browser_smoke.get("screenshot_ok") is True, browser_smoke)
    add("browser-toolchain", "browser diagnostics mode is cdp-first", diagnostics.get("mode") == "cdp-first")
    add("browser-toolchain", "browser diagnostics auto launch enabled", diagnostics.get("autoLaunch") is True)
    add("browser-toolchain", "browser diagnostics fallback enabled", diagnostics.get("fallbackEnabled") is True)
    add("browser-toolchain", "python playwright import works", py_checks["playwright"])
    add("browser-toolchain", "python rapidocr import works", py_checks["rapidocr"])
    add("browser-toolchain", "python PIL import works", py_checks["PIL"])
    add("browser-toolchain", "node command works", node.returncode == 0 and node.stdout.strip().startswith("v"))
    add("browser-toolchain", "npm command works", npm.returncode == 0 and bool(npm.stdout.strip()))
    add("browser-toolchain", "npx command works", npx.returncode == 0 and bool(npx.stdout.strip()))


def main():
    started = time.time()
    try:
        phase_deployment()
        phase_public_http()
        phase_manifest()
        phase_api()
        phase_v026_markers()
        phase_downloads()
        phase_archive_contents()
        phase_browser_toolchain()
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    failures = [item for item in CHECKS if item.get("status") != "PASS"]
    status = "PASS" if len(CHECKS) == 200 and not failures else "FAIL"
    payload = {
        "status": status,
        "version": VERSION,
        "scope": "production-server-200-user-behavior",
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "durationSeconds": round(time.time() - started, 2),
        "checkCount": len(CHECKS),
        "passCount": sum(1 for item in CHECKS if item.get("status") == "PASS"),
        "failCount": len(failures),
        "target": {
            "domainHash": hashlib.sha256(DOMAIN.encode()).hexdigest().upper()[:16],
            "rawTargetPersisted": False,
        },
        "checks": CHECKS,
        "failurePreview": failures[:12],
        "redaction": {
            "rawUrlPersisted": False,
            "rawPasswordPersisted": False,
            "rawSecretPersisted": False,
        },
    }
    print("__REMOTE_MARKER__")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    if status != "PASS":
        sys.exit(1)


if __name__ == "__main__":
    main()
"""


def run() -> dict[str, Any]:
    deploy_module = _load_deploy_module()
    deployer = deploy_module.ProductionDeploy()
    remote_script = (
        REMOTE_SCRIPT
        .replace("__VERSION__", VERSION)
        .replace("__DOMAIN__", deployer.domain)
        .replace("__REMOTE_MARKER__", REMOTE_MARKER)
    )
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        hostname=deployer.host,
        username=deployer.user,
        password=deployer.password,
        timeout=25,
        banner_timeout=25,
        auth_timeout=25,
        look_for_keys=False,
        allow_agent=False,
    )
    try:
        command = "/opt/ecorex-web/venv/bin/python - <<'PY'\n" + remote_script + "\nPY"
        _, stdout, stderr = client.exec_command(command, timeout=2400)
        out = stdout.read().decode("utf-8", errors="replace")
        err = stderr.read().decode("utf-8", errors="replace")
        code = stdout.channel.recv_exit_status()
    finally:
        client.close()

    payload = _extract_remote_json(out)
    payload["remoteExitCode"] = int(code)
    payload["remoteStdoutHash"] = _sha_text(out)
    payload["remoteStderrHash"] = _sha_text(err)
    payload["remoteStderrExcerptRedacted"] = deployer.redact(err)
    payload["generatedLocallyAt"] = datetime.now(timezone.utc).isoformat()
    payload["target"] = {
        "domainHash": deployer.secret_hash(deployer.domain),
        "sshHostHash": deployer.secret_hash(deployer.host),
        "sshUserHash": deployer.secret_hash(deployer.user),
        "rawTargetPersisted": False,
    }
    payload["status"] = "PASS" if payload.get("status") == "PASS" and code == 0 else "FAIL"
    return payload


def main() -> int:
    ARTIFACT.parent.mkdir(parents=True, exist_ok=True)
    started = time.time()
    try:
        payload = run()
    except Exception as exc:
        payload = {
            "status": "FAIL",
            "version": VERSION,
            "scope": "production-server-200-user-behavior",
            "generatedLocallyAt": datetime.now(timezone.utc).isoformat(),
            "durationSeconds": round(time.time() - started, 2),
            "errorType": exc.__class__.__name__,
            "error": str(exc)[:500],
        }
    payload["durationSecondsLocal"] = round(time.time() - started, 2)
    ARTIFACT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    summary = {
        "status": payload.get("status"),
        "artifact": str(ARTIFACT),
        "checkCount": payload.get("checkCount"),
        "passCount": payload.get("passCount"),
        "failCount": payload.get("failCount"),
        "durationSecondsLocal": payload.get("durationSecondsLocal"),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if payload.get("status") == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
