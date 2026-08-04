#!/usr/bin/env python3
"""Browser UI smoke for the production v0.2.6 Web app.

The script reads operator connection details at runtime, fetches the Web
password through SSH, runs a real Chromium session against the public app URL,
and persists only redacted evidence.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import paramiko
from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[1]
VERSION = "0.2.6"
ARTIFACT_DIR = ROOT / "docs" / f"v{VERSION}" / "artifacts"
ARTIFACT = ARTIFACT_DIR / "production-browser-ui-v026-smoke.json"
SCREENSHOT = ARTIFACT_DIR / "production-browser-ui-v026-smoke.png"


def sha_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest().upper()


def load_deploy_module():
    spec = importlib.util.spec_from_file_location("deploy_v024_production", ROOT / "scripts" / "deploy-v024-production.py")
    if spec is None or spec.loader is None:
        raise RuntimeError("Unable to load deploy-v024-production.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def ssh_read_web_password(host: str, user: str, password: str) -> str:
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
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
    try:
        command = "python3 - <<'PY'\nfrom pathlib import Path\nfor raw in Path('/etc/ecorex-web/ecorex-web.env').read_text(encoding='utf-8', errors='replace').splitlines():\n    if raw.startswith('WEB_PASSWORD='):\n        print(raw.split('=', 1)[1].strip().strip('\\\"').strip(\"'\"))\n        break\nPY"
        _, stdout, stderr = client.exec_command(command, timeout=30)
        out = stdout.read().decode("utf-8", errors="replace").strip()
        err = stderr.read().decode("utf-8", errors="replace").strip()
        code = stdout.channel.recv_exit_status()
        if code != 0 or not out:
            raise RuntimeError(f"Unable to read WEB_PASSWORD: code={code} stderrHash={sha_text(err)[:12]}")
        return out
    finally:
        client.close()


def add(checks: list[dict[str, Any]], name: str, ok: bool, detail: dict[str, Any] | None = None) -> None:
    checks.append({
        "index": len(checks) + 1,
        "name": name,
        "status": "PASS" if bool(ok) else "FAIL",
        "detail": detail or {},
    })


def allowed_failed_response(url: str, status: int) -> bool:
    if "/client/model-config" in url and status in (401, 403):
        return True
    if "favicon" in url.lower() and status in (404,):
        return True
    return False


def allowed_console_error(text: str) -> bool:
    lowered = text.lower()
    if "failed to load resource" in lowered and ("401" in lowered or "403" in lowered):
        return True
    if "favicon" in lowered and "404" in lowered:
        return True
    return False


def main() -> None:
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    deploy = load_deploy_module()
    host, domain, user, ssh_password = deploy.read_server_file()
    web_password = ssh_read_web_password(host, user, ssh_password)
    public_base = f"https://{domain}/ecorex-agent"
    run_id = str(int(time.time()))
    checks: list[dict[str, Any]] = []
    console_errors: list[str] = []
    failed_responses: list[dict[str, Any]] = []
    metrics: dict[str, Any] = {
        "logoResponses": [],
        "modelMenuCount": 0,
        "providerLabels": [],
        "logoImageCount": 0,
        "loadedLogoImageCount": 0,
    }

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1440, "height": 980}, ignore_https_errors=True)
        login = context.request.post(
            f"{public_base}/auth/login",
            data=json.dumps({"email": f"ui-{run_id}@ecorex.local", "password": web_password}),
            headers={"Content-Type": "application/json"},
            timeout=35_000,
        )
        try:
            login_json = login.json()
        except Exception:
            login_json = {}
        session = (login_json.get("session") or {}) if isinstance(login_json, dict) else {}
        user_payload = session.get("user") or {}
        add(checks, "login returns success", login.status == 200 and login_json.get("status") == "success", {"status": login.status})
        add(checks, "login is not local fallback", session.get("authProvider") != "local-fallback")
        add(checks, "login uses smoke email", user_payload.get("email") == f"ui-{run_id}@ecorex.local")

        context.add_init_script(f"window.localStorage.setItem('ecorex-release-notes-seen-version', '{VERSION}');")
        page = context.new_page()
        page.on("console", lambda msg: console_errors.append(msg.text[:300]) if msg.type == "error" and not allowed_console_error(msg.text) else None)

        def on_response(response):
            status = response.status
            url = response.url
            if "/app/assets/logos/" in url:
                metrics["logoResponses"].append({"status": status, "fileHash": sha_text(url.rsplit("/", 1)[-1])[:12]})
            if status >= 400 and not allowed_failed_response(url, status):
                failed_responses.append({"status": status, "urlHash": sha_text(url)[:12]})

        page.on("response", on_response)
        page.goto(f"{public_base}/app/?release=ui-smoke-{run_id}", wait_until="domcontentloaded", timeout=45_000)
        page.wait_for_selector(".app-shell", timeout=30_000)
        page.wait_for_timeout(1800)
        body_text = page.locator("body").inner_text(timeout=10_000)
        add(checks, "app shell rendered", bool(body_text and "EcoreX" in body_text), {"bodyLength": len(body_text)})
        add(checks, "version visible v0.2.6", "v0.2.6" in body_text)

        model_button = page.locator("button[title^='当前模型']").first
        model_button.click(timeout=15_000)
        page.wait_for_selector(".chat-model-popover", timeout=15_000)
        option_buttons = page.locator(".chat-model-popover button")
        option_count = option_buttons.count()
        metrics["modelMenuCount"] = option_count
        menu_text = page.locator(".chat-model-popover").inner_text(timeout=5000)
        provider_labels = [label for label in ("OpenAI", "DeepSeek", "Gemini", "豆包") if label in menu_text]
        metrics["providerLabels"] = provider_labels
        add(checks, "model popover opened", option_count > 0, {"count": option_count})
        add(checks, "model menu has more than gpt-5.6-luna", option_count > 1, {"count": option_count})
        add(checks, "model menu has no Unauthorized", "Unauthorized" not in menu_text)
        add(checks, "model menu shows provider labels", len(provider_labels) >= 4, {"labels": provider_labels})

        logo_images = page.locator(".chat-model-popover .provider-model-icon img")
        metrics["logoImageCount"] = logo_images.count()
        logo_loaded = logo_images.evaluate_all("(imgs) => imgs.filter((img) => img.complete && img.naturalWidth > 0 && img.naturalHeight > 0).length")
        metrics["loadedLogoImageCount"] = logo_loaded
        add(checks, "model menu buttons have loaded provider logo images", option_count > 0 and metrics["logoImageCount"] == option_count and logo_loaded == option_count, {
            "optionCount": option_count,
            "logoImageCount": metrics["logoImageCount"],
            "loadedLogoImageCount": logo_loaded,
        })

        add(checks, "regular general sessions have no task title row", page.locator(".session-group.is-regular .session-group-title").count() == 0)
        add(checks, "old sticky model switch divider absent before click", page.locator(".model-switch-divider").count() == 0)

        candidate = page.locator(".chat-model-popover button:not(.is-active):not(.is-unavailable)").first
        candidate_title = candidate.get_attribute("title") or ""
        candidate.click(timeout=15_000)
        page.wait_for_selector(".message.system.is-model-switch", timeout=15_000)
        switch_text = page.locator(".message.system.is-model-switch").last.inner_text(timeout=5000)
        add(checks, "model can switch through UI", "已切换" in switch_text or "切换" in switch_text, {"targetHash": sha_text(candidate_title)[:12]})
        add(checks, "model switch prompt is normal system message", page.locator(".message-list .message.system.is-model-switch").count() >= 1)
        add(checks, "model switch sticky divider absent", page.locator(".model-switch-divider").count() == 0)

        page.screenshot(path=str(SCREENSHOT), full_page=False)
        add(checks, "ui screenshot captured", SCREENSHOT.is_file() and SCREENSHOT.stat().st_size > 10_000, {"bytes": SCREENSHOT.stat().st_size if SCREENSHOT.exists() else 0})
        add(checks, "provider logo resources return 200", all(item.get("status") == 200 for item in metrics["logoResponses"]) and len(metrics["logoResponses"]) >= option_count, {"count": len(metrics["logoResponses"])})
        add(checks, "no unexpected browser console errors", not console_errors and not failed_responses, {
            "consoleErrorCount": len(console_errors),
            "failedResponseCount": len(failed_responses),
            "failedResponses": failed_responses[:5],
            "consoleErrors": console_errors[:5],
        })
        browser.close()

    failures = [item for item in checks if item["status"] != "PASS"]
    payload = {
        "status": "PASS" if not failures else "FAIL",
        "version": VERSION,
        "scope": "production-browser-ui-v026",
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "target": {
            "domainHash": sha_text(domain)[:16],
            "rawTargetPersisted": False,
        },
        "checkCount": len(checks),
        "passCount": sum(1 for item in checks if item["status"] == "PASS"),
        "failCount": len(failures),
        "checks": checks,
        "failurePreview": failures[:10],
        "metrics": metrics,
        "screenshot": str(SCREENSHOT),
        "redaction": {
            "rawPasswordPersisted": False,
            "rawUrlPersisted": False,
            "rawSecretPersisted": False,
        },
    }
    ARTIFACT.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": payload["status"], "artifact": str(ARTIFACT), "checkCount": payload["checkCount"], "passCount": payload["passCount"], "failCount": payload["failCount"]}, ensure_ascii=False, indent=2))
    if payload["status"] != "PASS":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
