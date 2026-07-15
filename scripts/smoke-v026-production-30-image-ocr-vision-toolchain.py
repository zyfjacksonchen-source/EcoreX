#!/usr/bin/env python3
"""Run focused production checks for imagegen/OCR/vision/toolchains."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import paramiko


ROOT = Path(__file__).resolve().parents[1]
VERSION = os.environ.get("ECOREX_ACCEPTANCE_VERSION") or os.environ.get("ECOREX_DEPLOY_VERSION") or "0.2.8"
ARTIFACT = ROOT / "docs" / f"v{VERSION}" / "artifacts" / "production-32-image-ocr-vision-toolchain.json"
REMOTE_MARKER = "__ECOREX_PRODUCTION_32_IMAGE_OCR_VISION_JSON__"


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
    index = stdout.rfind(REMOTE_MARKER)
    if index < 0:
        raise RuntimeError("Remote 30-check JSON marker missing")
    return json.loads(stdout[index + len(REMOTE_MARKER):].strip())


REMOTE_SCRIPT = r"""
import base64
import hashlib
import http.cookiejar
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

VERSION = "__VERSION__"
LOCAL_BASE = "http://127.0.0.1:9909"
RUNTIME = Path("/opt/ecorex-web/current/runtime")
VALIDATION_TMP_ROOT = Path("/srv/ecorex-agent-download/validation-tmp")
VALIDATION_TMP_ROOT.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("TMPDIR", str(VALIDATION_TMP_ROOT))
tempfile.tempdir = str(VALIDATION_TMP_ROOT)
CHECKS = []


def add(group, name, ok, detail=None):
    CHECKS.append({
        "index": len(CHECKS) + 1,
        "group": group,
        "name": name,
        "status": "PASS" if bool(ok) else "FAIL",
        "detail": detail or {},
    })


def safe_tool_payload(result):
    payload = getattr(result, "result", result)
    status = getattr(result, "status", "")
    if isinstance(payload, dict):
        return status, payload
    return status, {"text": str(payload)[:1000]}


def request(path, method="GET", data=None, opener=None, timeout=40):
    body = None if data is None else json.dumps(data).encode("utf-8")
    req = urllib.request.Request(
        LOCAL_BASE + path,
        data=body,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        open_fn = opener.open if opener is not None else urllib.request.urlopen
        with open_fn(req, timeout=timeout) as resp:
            text = resp.read(2_000_000).decode("utf-8", errors="replace")
            try:
                payload = json.loads(text)
            except Exception:
                payload = {}
            return {"status": resp.status, "json": payload, "text": text}
    except urllib.error.HTTPError as exc:
        text = exc.read(2000).decode("utf-8", errors="replace")
        return {"status": exc.code, "json": {}, "text": text}
    except Exception as exc:
        return {"status": 0, "json": {}, "text": str(exc)[:240], "errorType": exc.__class__.__name__}


def run(args, timeout=30, cwd=None):
    try:
        return subprocess.run(args, text=True, capture_output=True, timeout=timeout, cwd=cwd)
    except Exception as exc:
        class Result:
            returncode = 999
            stdout = ""
            stderr = str(exc)
        return Result()


def load_service_env():
    for raw in Path("/etc/ecorex-web/ecorex-web.env").read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ[key.strip()] = value.strip().strip('"').strip("'")
    os.environ.setdefault("ECOREX_CONFIG_PATH", "/opt/ecorex-web/state/config.json")
    os.environ.setdefault("ECOREX_STATE_DIR", "/opt/ecorex-web/state")
    os.environ.setdefault("ECOREX_CAPABILITY_STATE_DIR", "/opt/ecorex-web/state/capability-state")
    os.environ.setdefault("ECOREX_CAPABILITY_TARGET_DIR", "/opt/ecorex-web/state/capability-packages")
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", "/opt/ecorex-web/state/playwright-browsers")
    os.environ.setdefault("ECOREX_PLAYWRIGHT_BROWSERS_DIR", "/opt/ecorex-web/state/playwright-browsers")


def create_fixture(path: Path):
    from PIL import Image, ImageDraw, ImageFont

    image = Image.new("RGB", (900, 520), "white")
    draw = ImageDraw.Draw(image)
    try:
        font_big = ImageFont.truetype("DejaVuSans-Bold.ttf", 52)
        font_mid = ImageFont.truetype("DejaVuSans-Bold.ttf", 40)
        font_small = ImageFont.truetype("DejaVuSans.ttf", 32)
    except Exception:
        font_big = font_mid = font_small = None
    draw.rectangle((40, 40, 410, 250), fill=(238, 40, 48), outline=(120, 0, 0), width=4)
    draw.rectangle((490, 40, 860, 250), fill=(40, 180, 70), outline=(0, 90, 20), width=4)
    draw.text((78, 112), "RED BOX", fill="white", font=font_mid)
    draw.text((535, 112), "GREEN BOX", fill="white", font=font_mid)
    draw.text((80, 310), "ECX OCR 4827", fill="black", font=font_big)
    draw.text((80, 382), "URL https://example.com/ecorex-4827", fill="black", font=font_small)
    image.save(path)


def main():
    started = time.time()
    load_service_env()
    os.chdir(str(RUNTIME))
    sys.path.insert(0, str(RUNTIME))
    from config import load_config
    load_config()
    from common.ecorex_tool_permissions import get_tool_permission_broker
    get_tool_permission_broker().set_mode("full-access")

    password = os.environ.get("WEB_PASSWORD", "")
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    login = request("/auth/login", method="POST", data={"email": "image-ocr-vision-smoke@ecorex.local", "password": password}, opener=opener)
    models = request("/api/models", opener=opener)
    tools = request("/api/tools", opener=opener)
    skills = request("/api/skills", opener=opener)
    capabilities = request("/api/capabilities", opener=opener)
    perm = request("/api/tool-permissions", opener=opener)
    combined = json.dumps({"tools": tools["json"], "skills": skills["json"], "capabilities": capabilities["json"]}, ensure_ascii=False).lower()

    # 8 discovery/dependency checks.
    add("discovery", "login works and is not local fallback", login["status"] == 200 and ((login["json"].get("session") or {}).get("localFallback") is False))
    add("discovery", "full-access permission visible", perm["status"] == 200 and perm["json"].get("mode") == "full-access")
    add("discovery", "imagegen capability discoverable", "imagegen" in combined or "image-generation" in combined)
    add("discovery", "ocr capability discoverable", "ocr" in combined)
    add("discovery", "vision capability discoverable", "vision" in combined)
    add("discovery", "browser capability discoverable", "browser" in combined)
    add("discovery", "owned node/npm/npx available", all(run([f"/opt/ecorex-web/node/bin/{name}", "--version"]).returncode == 0 for name in ("node", "npm", "npx")))
    add("discovery", "runtime python image deps import", run(["/opt/ecorex-web/venv/bin/python", "-c", "import PIL, rapidocr_onnxruntime, playwright"], timeout=30).returncode == 0)

    workspace = Path("/srv/ecorex-agent-workspace")
    fixture_dir = workspace / ".ecorex" / "smoke-artifacts"
    fixture_dir.mkdir(parents=True, exist_ok=True)
    fixture = fixture_dir / "ocr-vision-fixture-v027.png"
    create_fixture(fixture)

    from agent.tools.ocr.ocr import OcrTool
    ocr = OcrTool({"cwd": str(fixture_dir)})
    ocr_status, ocr_diag = safe_tool_payload(ocr.execute({"action": "diagnose"}))
    ocr_text_status, ocr_text = safe_tool_payload(ocr.execute({"action": "extract_text", "image": str(fixture), "timeout": 8}))
    ocr_url_status, ocr_urls = safe_tool_payload(ocr.execute({"action": "extract_urls", "image": str(fixture), "timeout": 8}))
    text = str(ocr_text.get("text") or "")
    urls = ocr_urls.get("urls") or []

    # 7 OCR checks.
    add("ocr", "OCR diagnose succeeds", ocr_status == "success" and ocr_diag.get("status") == "success")
    add("ocr", "rapidocr provider is available", bool((ocr_diag.get("providers") or {}).get("rapidocr")))
    add("ocr", "OCR fixture file exists", fixture.is_file() and fixture.stat().st_size > 1000)
    add("ocr", "OCR extracts ECX token", "ECX" in text.upper(), {"textHash": hashlib.sha256(text.encode()).hexdigest()[:12]})
    add("ocr", "OCR extracts number 4827", "4827" in text)
    add("ocr", "OCR extracts expected URL", any("example.com/ecorex-4827" in str(url) for url in urls), {"urlCount": len(urls)})
    add("ocr", "OCR URL extraction provides browser nextAction", ((ocr_urls.get("nextAction") or {}).get("tool") == "browser"))

    from agent.tools.vision.vision import Vision
    vision = Vision({"cwd": str(fixture_dir)})
    vision_status, vision_payload = safe_tool_payload(vision.execute({
        "image": str(fixture),
        "question": "Describe the red and green boxes, then read the black text line below them. Return the visible uppercase tokens and the number.",
    }))
    vision_answer = json.dumps(vision_payload, ensure_ascii=False)
    vision_lower = vision_answer.lower()
    vision_flags = {
        "answerHash": hashlib.sha256(vision_answer.encode("utf-8", errors="replace")).hexdigest()[:12],
        "mentionsEcx": "ecx" in vision_lower,
        "mentionsOcr": "ocr" in vision_lower,
        "mentions4827": "4827" in vision_answer,
    }

    # 5 vision checks.
    add("vision", "Vision tool call succeeds", vision_status == "success", {"status": vision_status})
    add("vision", "Vision answer mentions red", "red" in vision_lower)
    add("vision", "Vision answer mentions green", "green" in vision_lower)
    add("vision", "Vision answer reads ECX/OCR text", vision_flags["mentionsEcx"] or vision_flags["mentionsOcr"], vision_flags)
    add("vision", "Vision answer reads number 4827", vision_flags["mentions4827"], vision_flags)

    from agent.tools.browser.browser_tool import BrowserTool
    browser = BrowserTool({
        "cdp_endpoint": "http://127.0.0.1:9222",
        "cdp_auto_launch": False,
        "cdp_fallback": True,
        "persistent": False,
        "snapshot_max_chars": 2500,
    })
    browser_status, browser_payload = safe_tool_payload(browser.execute({
        "action": "navigate",
        "url": "data:text/html,<title>OCR Chain</title><h1>example.com/ecorex-4827</h1>",
        "timeout": 20000,
    }))
    browser.close()

    from agent.tools.imagegen.imagegen import ImageGenTool
    imagegen = ImageGenTool()
    probe_status, probe = safe_tool_payload(imagegen.execute({"action": "probe"}))
    gen_status, gen = safe_tool_payload(imagegen.execute({
        "prompt": "Create a simple clean square product icon: orange letter X on white background, no text besides X.",
        "size": "1024x1024",
        "output_format": "png",
        "output_dir": str(fixture_dir),
        "quality_retry_max": 0,
        "timeout": 600,
    }))
    batch_status, batch = safe_tool_payload(imagegen.execute({
        "tasks": [
            {"prompt": "Create a tiny clean blue circle icon on white background, no text.", "size": "1024x1024", "output_format": "png"},
            {"prompt": "Create a tiny clean green check icon on white background, no text.", "size": "1024x1024", "output_format": "png"},
        ],
        "output_dir": str(fixture_dir),
        "quality_retry_max": 0,
        "timeout": 900,
    }))

    route = gen.get("route") if isinstance(gen.get("route"), dict) else {}
    images = gen.get("images") or []
    generated_paths = [Path(str(item.get("path") or item.get("url") or "")) for item in images if isinstance(item, dict)]
    model = str(gen.get("model") or "").lower()
    batch_route = batch.get("route") if isinstance(batch.get("route"), dict) else {}
    batch_results = batch.get("taskResults") or []

    original_provider = ""
    original_model = ""
    target_switch = None
    model_payload = models.get("json") if isinstance(models.get("json"), dict) else {}
    chat_cap = ((model_payload.get("capabilities") or {}).get("chat") or {}) if isinstance(model_payload.get("capabilities"), dict) else {}
    if isinstance(chat_cap, dict):
        original_provider = str(chat_cap.get("current_provider") or "")
        original_model = str(chat_cap.get("current_model") or "")
        model_options = chat_cap.get("model_options") if isinstance(chat_cap.get("model_options"), list) else []
    else:
        model_options = []
    configured_options = [
        item for item in model_options
        if isinstance(item, dict) and item.get("configured") is not False and str(item.get("provider") or "") and str(item.get("model") or "")
    ]
    for item in configured_options:
        provider_id = str(item.get("provider") or "")
        model_id = str(item.get("model") or "")
        if provider_id != original_provider and provider_id != "openai":
            target_switch = (provider_id, model_id)
            break
    if not target_switch:
        for item in configured_options:
            provider_id = str(item.get("provider") or "")
            model_id = str(item.get("model") or "")
            if provider_id != original_provider or model_id != original_model:
                target_switch = (provider_id, model_id)
                break
    switch_result = {"status": "not-run", "json": {}}
    switch_verify = {"status": "not-run", "json": {}}
    if target_switch:
        switch_result = request(
            "/api/models",
            method="POST",
            data={"action": "set_capability", "capability": "chat", "provider_id": target_switch[0], "model": target_switch[1]},
            opener=opener,
            timeout=60,
        )
        switch_verify = request("/api/models", opener=opener, timeout=60)
    switch_chat = (((switch_verify.get("json") or {}).get("capabilities") or {}).get("chat") or {}) if isinstance(switch_verify.get("json"), dict) else {}
    switch_confirmed = bool(
        target_switch
        and switch_result.get("status") == 200
        and switch_chat.get("current_provider") == target_switch[0]
        and switch_chat.get("current_model") == target_switch[1]
    )
    switched_status, switched_gen = safe_tool_payload(imagegen.execute({
        "prompt": "After chat model switching, create a simple small orange X icon on white background, no other text.",
        "size": "1024x1024",
        "output_format": "png",
        "output_dir": str(fixture_dir),
        "quality_retry_max": 0,
        "timeout": 600,
    }))
    switched_edit_status, switched_edit = safe_tool_payload(imagegen.execute({
        "prompt": "After chat model switching, edit/reference this image by preserving the red and green boxes and making the background slightly cleaner.",
        "image_url": str(fixture),
        "size": "1024x1024",
        "output_format": "png",
        "output_dir": str(fixture_dir),
        "quality_retry_max": 0,
        "timeout": 600,
    }))
    restore_status = "not-needed"
    restore_confirmed = True
    if original_provider and original_model:
        restore_result = request(
            "/api/models",
            method="POST",
            data={"action": "set_capability", "capability": "chat", "provider_id": original_provider, "model": original_model},
            opener=opener,
            timeout=60,
        )
        restore_status = restore_result.get("status") or "unknown"
        restore_verify = request("/api/models", opener=opener, timeout=60)
        restore_chat = (((restore_verify.get("json") or {}).get("capabilities") or {}).get("chat") or {}) if isinstance(restore_verify.get("json"), dict) else {}
        restore_confirmed = bool(
            restore_result.get("status") == 200
            and restore_chat.get("current_provider") == original_provider
            and restore_chat.get("current_model") == original_model
        )

    switched_route = switched_gen.get("route") if isinstance(switched_gen, dict) and isinstance(switched_gen.get("route"), dict) else {}
    switched_edit_route = switched_edit.get("route") if isinstance(switched_edit, dict) and isinstance(switched_edit.get("route"), dict) else {}

    # 12 imagegen/multi-toolchain checks.
    add("toolchain-imagegen", "Browser step succeeds in OCR chain", browser_status == "success" and "ecorex-4827" in json.dumps(browser_payload, ensure_ascii=False))
    add("toolchain-imagegen", "Imagegen probe is ready", probe_status == "success" and probe.get("status") == "ready")
    add("toolchain-imagegen", "Imagegen provider credentials configured", probe.get("providerConfigured") is True, {"configuredProviderEnvCount": probe.get("configuredProviderEnvCount")})
    add("toolchain-imagegen", "Single image generation succeeds", gen_status == "success", {"status": gen_status, "code": gen.get("code"), "nextAction": gen.get("nextAction")})
    add("toolchain-imagegen", "Single image route uses native generations", route.get("providerApiRoute") == "images.generations" and route.get("shellInvocation") is False and route.get("pythonSubprocess") is False)
    add("toolchain-imagegen", "Single image uses gpt-image-2-pro", model == "gpt-image-2-pro", {"model": gen.get("model")})
    add("toolchain-imagegen", "Generated image file exists", any(path.is_file() and path.stat().st_size > 1000 for path in generated_paths))
    add("toolchain-imagegen", "No Python fallback used for single image", gen.get("pythonFallbackUsed") is False and gen.get("fallbackUsed") is False)
    add("toolchain-imagegen", "Native batch imagegen path used", batch.get("batchMode") == "native_imagegen_tool_loop" and batch_route.get("providerApiRoute") == "native.batch.imagegen" and batch.get("shellFallbackUsed") is False)
    add("toolchain-imagegen", "Batch imagegen produced two successful tasks", batch_status == "success" and batch.get("successCount") == 2 and len([item for item in batch_results if item.get("status") == "success"]) == 2)
    add("toolchain-imagegen", "After chat model switch text-to-image still uses gpt-image-2-pro native route", switch_confirmed and restore_confirmed and switched_status == "success" and str(switched_gen.get("model") or "").lower() == "gpt-image-2-pro" and switched_route.get("providerApiRoute") == "images.generations" and switched_route.get("shellInvocation") is False and switched_route.get("pythonSubprocess") is False and switched_gen.get("pythonFallbackUsed") is False and switched_gen.get("fallbackUsed") is False, {"switchedFrom": [original_provider, original_model], "switchedTo": target_switch or [], "switchStatus": switch_result.get("status"), "restored": restore_status, "restoreConfirmed": restore_confirmed})
    add("toolchain-imagegen", "After chat model switch image edit still uses gpt-image-2-pro edit route", switch_confirmed and restore_confirmed and switched_edit_status == "success" and str(switched_edit.get("model") or "").lower() == "gpt-image-2-pro" and switched_edit_route.get("providerApiRoute") == "images.edits" and switched_edit_route.get("shellInvocation") is False and switched_edit_route.get("pythonSubprocess") is False and switched_edit.get("pythonFallbackUsed") is False and switched_edit.get("fallbackUsed") is False, {"switchedFrom": [original_provider, original_model], "switchedTo": target_switch or [], "switchStatus": switch_result.get("status"), "restored": restore_status, "restoreConfirmed": restore_confirmed})

    failures = [item for item in CHECKS if item["status"] != "PASS"]
    payload = {
        "status": "PASS" if len(CHECKS) == 32 and not failures else "FAIL",
        "version": VERSION,
        "scope": "production-server-32-image-ocr-vision-toolchain",
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "durationSeconds": round(time.time() - started, 2),
        "checkCount": len(CHECKS),
        "passCount": sum(1 for item in CHECKS if item["status"] == "PASS"),
        "failCount": len(failures),
        "checks": CHECKS,
        "failurePreview": failures[:10],
        "artifacts": {
            "fixtureHash": hashlib.sha256(str(fixture).encode()).hexdigest()[:12],
            "generatedImageCount": len(images),
            "batchImageCount": len(batch.get("images") or []) if isinstance(batch, dict) else 0,
        },
        "redaction": {
            "rawPasswordPersisted": False,
            "rawSecretPersisted": False,
            "rawUrlPersisted": False,
            "rawImagePathPersisted": False,
        },
    }
    print("__REMOTE_MARKER__")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    if payload["status"] != "PASS":
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
        _, stdout, stderr = client.exec_command(command, timeout=3600)
        out = stdout.read().decode("utf-8", errors="replace")
        err = stderr.read().decode("utf-8", errors="replace")
        code = stdout.channel.recv_exit_status()
    finally:
        client.close()
    try:
        payload = _extract_remote_json(out)
    except Exception as exc:
        return {
            "status": "FAIL",
            "version": VERSION,
            "scope": "production-server-32-image-ocr-vision-toolchain",
            "generatedLocallyAt": datetime.now(timezone.utc).isoformat(),
            "remoteExitCode": int(code),
            "remoteStdoutHash": _sha_text(out),
            "remoteStderrHash": _sha_text(err),
            "remoteStdoutExcerptRedacted": deployer.redact(out[-3000:]),
            "remoteStderrExcerptRedacted": deployer.redact(err[-3000:]),
            "errorType": exc.__class__.__name__,
            "error": str(exc)[:500],
            "target": {
                "domainHash": deployer.secret_hash(deployer.domain),
                "sshHostHash": deployer.secret_hash(deployer.host),
                "sshUserHash": deployer.secret_hash(deployer.user),
                "rawTargetPersisted": False,
            },
        }
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
    try:
        payload = run()
    except Exception as exc:
        payload = {
            "status": "FAIL",
            "version": VERSION,
            "scope": "production-server-32-image-ocr-vision-toolchain",
            "generatedLocallyAt": datetime.now(timezone.utc).isoformat(),
            "errorType": exc.__class__.__name__,
            "error": str(exc)[:500],
        }
    ARTIFACT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    summary = {
        "status": payload.get("status"),
        "artifact": str(ARTIFACT),
        "checkCount": payload.get("checkCount"),
        "passCount": payload.get("passCount"),
        "failCount": payload.get("failCount"),
        "durationSeconds": payload.get("durationSeconds"),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if payload.get("status") == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
