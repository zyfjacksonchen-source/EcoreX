#!/usr/bin/env python3
"""Browser-smoke the deployed v0.2.2 Web/Admin surfaces.

Credentials are read at runtime from the local server-address file and the
remote Web env file. Evidence persists only version/UI metrics and hashes.
"""

from __future__ import annotations

import json
import os
import re
import time
import importlib.util
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import paramiko
from playwright.sync_api import sync_playwright


ROOT = Path.cwd()
ARTIFACT = ROOT / "docs" / "v0.2.2" / "artifacts" / "online-web-browser-smoke.json"
SCREENSHOT = ROOT / "docs" / "v0.2.2" / "artifacts" / "online-web-browser-smoke.png"

_DEPLOY_SPEC = importlib.util.spec_from_file_location(
    "deploy_v022_hotfix_target", ROOT / "scripts" / "deploy-v022-hotfix-target.py"
)
if _DEPLOY_SPEC is None or _DEPLOY_SPEC.loader is None:
    raise RuntimeError("unable to load deploy-v022-hotfix-target.py")
_DEPLOY_MODULE = importlib.util.module_from_spec(_DEPLOY_SPEC)
_DEPLOY_SPEC.loader.exec_module(_DEPLOY_MODULE)

Deployer = _DEPLOY_MODULE.Deployer
VERSION = _DEPLOY_MODULE.VERSION
sha256_text = _DEPLOY_MODULE.sha256_text


def read_remote_web_password(deployer: Deployer) -> str:
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
        command = "awk -F= '/^WEB_PASSWORD=/{print substr($0, index($0,$2)); exit}' /etc/ecorex-web/ecorex-web.env"
        _, stdout, stderr = client.exec_command(command, timeout=30)
        out = stdout.read().decode("utf-8", errors="replace").strip()
        err = stderr.read().decode("utf-8", errors="replace").strip()
        code = stdout.channel.recv_exit_status()
        if code != 0 or not out:
            raise RuntimeError(f"unable to read remote WEB_PASSWORD code={code} stderr={deployer.redact(err)}")
        return out
    finally:
        client.close()


def strip_secret(value: str, deployer: Deployer) -> str:
    return deployer.redact(value)


def run_smoke() -> dict[str, Any]:
    started = time.time()
    deployer = Deployer()
    smoke_email = os.environ.get("ECOREX_ONLINE_SMOKE_EMAIL", "").strip() or "qa.hotfix@example.com"
    smoke_password = os.environ.get("ECOREX_ONLINE_SMOKE_PASSWORD", "")
    web_password = smoke_password or read_remote_web_password(deployer)
    base = f"https://{deployer.domain}/ecorex-agent"
    app_url = f"{base}/app/"
    login_url = f"{base}/auth/login"
    manifest_url = f"{base}/manifest.json"
    admin_health_candidates = [f"{base}/client/health", f"{base}/client/", f"{base}/api/admin/health"]
    email = smoke_email
    errors: list[str] = []
    console_warnings: list[str] = []

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 1440, "height": 920},
            ignore_https_errors=True,
            user_agent="EcoreX-v0.2.2-online-smoke",
        )
        page = context.new_page()
        page.on("pageerror", lambda exc: errors.append(f"pageerror:{exc}"))

        def handle_console(msg: Any) -> None:
            if msg.type != "error":
                return
            event = f"console:{msg.type}:{msg.text}"
            if "401" in msg.text and "Unauthorized" in msg.text:
                console_warnings.append(event)
                return
            errors.append(event)

        page.on("console", handle_console)

        login_response = context.request.post(
            login_url,
            data=json.dumps({"email": email, "password": web_password}),
            headers={"Content-Type": "application/json"},
            timeout=30_000,
        )
        login_json: dict[str, Any] = {}
        try:
            login_json = login_response.json()
        except Exception:
            login_json = {}
        if login_response.status >= 400 or login_json.get("status") != "success":
            raise RuntimeError(f"online login failed status={login_response.status} body={strip_secret(login_response.text(), deployer)}")

        page.goto(app_url, wait_until="domcontentloaded", timeout=45_000)
        page.wait_for_selector(".app-shell", timeout=45_000)
        page.wait_for_timeout(800)
        if page.locator(".release-notes-backdrop").count():
            close_notes = page.locator("button[aria-label='关闭更新说明']").first
            if close_notes.count():
                close_notes.click(timeout=5_000)
            else:
                page.get_by_text("知道了").click(timeout=5_000)
            page.wait_for_timeout(300)
        # Force the codex-like empty state so stale/last-session content cannot hide the new-session regression.
        new_button = page.locator(".sidebar-actions button").first
        if new_button.count():
            new_button.click(timeout=5_000)
            page.wait_for_timeout(500)

        project_menu_metrics = {"visible": False, "hasImport": False, "hasNoProject": False, "closedOnBlank": False}
        project_start_trigger = page.locator(".new-session-project-picker .new-session-option").first
        if project_start_trigger.count():
            project_start_trigger.click(timeout=5_000)
            page.wait_for_timeout(300)
            project_menu_metrics = page.evaluate(
                """() => {
                  const menu = document.querySelector('.new-session-project-menu');
                  const text = menu ? (menu.innerText || '') : '';
                  return {
                    visible: Boolean(menu),
                    hasImport: text.includes('导入新文件夹'),
                    hasNoProject: text.includes('不使用项目'),
                    hasSearch: Boolean(menu && menu.querySelector('.project-start-search input[placeholder="搜索项目"]'))
                  };
                }"""
            )
            page.mouse.click(24, 24)
            page.wait_for_timeout(150)
            project_menu_metrics["closedOnBlank"] = page.locator(".new-session-project-menu").count() == 0

        metrics = page.evaluate(
            """(email) => {
              const text = document.body.innerText || '';
              const bodyStyle = getComputedStyle(document.body);
              const code = document.querySelector('code, pre, .monospace');
              const codeFamily = code ? getComputedStyle(code).fontFamily : '';
              const versionVisible = /v0\\.2\\.2|0\\.2\\.2/.test(text) && !/v0\\.2\\.1/.test(text);
              const hasHeadline = text.includes('和EcoreX一起开始工作');
              const oldHeadlineHidden = !text.includes('我们应该在 EcoreX 中构建什么');
              const hasProject = text.includes('项目文件夹');
              const hasGeneral = text.includes('通用会话');
              const localLeak = /local@ecorex\\.local|EcoreX用户|Local/i.test(text);
              const runCenterLeak = /Run Center|RUNCENTER/.test(text);
              const bodyFamily = bodyStyle.fontFamily;
              return {
                emailVisible: text.includes(email),
                versionVisible,
                newSessionHeadline: hasHeadline,
                oldHeadlineHidden,
                projectEntry: hasProject,
                generalEntry: hasGeneral,
                runCenterHidden: !runCenterLeak,
                localFallbackHidden: !localLeak,
                bodyFont: bodyFamily,
                codeFont: codeFamily,
                bodyHasSystemStack: /-apple-system|BlinkMacSystemFont|Segoe UI/i.test(bodyFamily),
                codeHasMonoStack: codeFamily ? /ui-monospace|SFMono|Consolas|Menlo/i.test(codeFamily) : true,
                textHash: ''
              };
            }""",
            email,
        )
        metrics["projectStartMenu"] = project_menu_metrics

        run_timing_metrics: dict[str, Any] = {
            "attempted": True,
            "visible": False,
            "inProcessSummary": False,
            "fallbackVisible": False,
            "finalLabelVisible": False,
        }
        try:
            page.evaluate(
                """() => {
                  const textarea = document.querySelector('.composer textarea');
                  if (!textarea) throw new Error('composer textarea missing');
                  const value = '请只回复 OK，用最少字。';
                  const setter = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value')?.set;
                  if (setter) setter.call(textarea, value);
                  else textarea.value = value;
                  textarea.dispatchEvent(new InputEvent('input', { bubbles: true, inputType: 'insertText', data: value }));
                }"""
            )
            page.wait_for_timeout(120)
            page.locator(".send-button").first.click(timeout=5_000)
            page.wait_for_function(
                """() => {
                  const text = document.body.innerText || '';
                  return /已在\\s+\\d|已处理\\s+\\d[^\\n]{0,24}后/.test(text);
                }""",
                timeout=120_000,
            )
            page.wait_for_timeout(400)
            run_timing_metrics = page.evaluate(
                """() => {
                  const text = document.body.innerText || '';
                  const finalLabelVisible = /已在\\s+\\d|已处理\\s+\\d[^\\n]{0,24}后/.test(text);
                  return {
                    attempted: true,
                    visible: finalLabelVisible,
                    finalLabelVisible,
                    inProcessSummary: Boolean(document.querySelector('.agent-process-timing')),
                    fallbackVisible: Boolean(document.querySelector('.message-run-timing'))
                  };
                }"""
            )
        except Exception as exc:
            errors.append(f"run timing smoke failed: {strip_secret(str(exc), deployer)}")
        metrics["runTiming"] = run_timing_metrics
        metrics["runTimingVisible"] = bool(run_timing_metrics.get("visible"))
        metrics["runTimingInProcessSummary"] = bool(run_timing_metrics.get("inProcessSummary"))
        metrics["runTimingFallbackVisible"] = bool(run_timing_metrics.get("fallbackVisible"))

        page.set_viewport_size({"width": 390, "height": 760})
        page.wait_for_timeout(250)
        narrow_metrics = page.evaluate(
            """() => {
              const chatPane = document.querySelector('.chat-pane');
              const messageList = document.querySelector('.message-list');
              const overflow = {
                document: document.documentElement.scrollWidth - document.documentElement.clientWidth,
                chatPane: chatPane ? chatPane.scrollWidth - chatPane.clientWidth : 0,
                messageList: messageList ? messageList.scrollWidth - messageList.clientWidth : 0
              };
              return {
                width: window.innerWidth,
                height: window.innerHeight,
                overflow,
                noHorizontalOverflow: overflow.document <= 1 && overflow.chatPane <= 1 && overflow.messageList <= 1
              };
            }"""
        )
        metrics["narrowViewport"] = narrow_metrics
        if not narrow_metrics.get("noHorizontalOverflow"):
            errors.append(f"narrow viewport overflow failed: {narrow_metrics}")

        body_text = page.locator("body").inner_text(timeout=5_000)
        metrics["textHash"] = sha256_text(body_text)[:16]
        if not metrics["emailVisible"]:
            errors.append("logged-in email is not visible")
        for key in ["versionVisible", "newSessionHeadline", "oldHeadlineHidden", "projectEntry", "generalEntry", "runCenterHidden", "localFallbackHidden", "bodyHasSystemStack", "codeHasMonoStack", "runTimingVisible"]:
            if not metrics.get(key):
                errors.append(f"{key} failed")
        for key in ["visible", "hasImport", "hasNoProject", "hasSearch", "closedOnBlank"]:
            if not project_menu_metrics.get(key):
                errors.append(f"projectStartMenu.{key} failed")

        manifest_response = context.request.get(manifest_url, timeout=30_000)
        manifest_json: dict[str, Any] = {}
        try:
            manifest_json = manifest_response.json()
        except Exception:
            manifest_json = {}
        if manifest_response.status != 200 or manifest_json.get("version") != VERSION:
            errors.append(f"manifest version/status failed status={manifest_response.status} version={manifest_json.get('version')}")

        admin_health = {"status": 0, "version": "", "pathHash": ""}
        for url in admin_health_candidates:
            response = context.request.get(url, timeout=15_000)
            payload: dict[str, Any] = {}
            try:
                payload = response.json()
            except Exception:
                payload = {}
            if response.status == 200 and payload.get("version") == VERSION:
                admin_health = {"status": response.status, "version": payload.get("version"), "pathHash": sha256_text(url)[:16]}
                break
        if admin_health["version"] != VERSION:
            errors.append("admin health version v0.2.2 not reachable through public proxy")

        SCREENSHOT.parent.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(SCREENSHOT), full_page=True)
        browser.close()

    result = {
        "status": "PASS" if not errors else "FAIL",
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "duration_ms": round((time.time() - started) * 1000),
        "target": {
            "domainHash": deployer.secret_hash(deployer.domain),
            "appUrlHash": sha256_text(app_url)[:16],
            "rawTargetPersisted": False,
        },
        "login": {
            "status": login_response.status,
            "sessionEmailVisible": metrics["emailVisible"],
            "sessionProvider": login_json.get("session", {}).get("authProvider") if isinstance(login_json.get("session"), dict) else "",
            "emailHash": sha256_text(email)[:16],
            "emailSource": "env" if os.environ.get("ECOREX_ONLINE_SMOKE_EMAIL", "").strip() else "default",
        },
        "manifest": {
            "status": manifest_response.status,
            "version": manifest_json.get("version"),
            "artifactVersions": sorted(
                {
                    item.get("version")
                    for item in manifest_json.get("artifacts", [])
                    if isinstance(item, dict) and item.get("id") in {"webui-windows-x64", "webui-macos-universal", "web-linux-service"}
                }
            ),
        },
        "adminHealth": admin_health,
        "metrics": metrics,
        "consoleErrors": [item for item in errors if item.startswith("console:")],
        "consoleWarnings": console_warnings,
        "assertionErrors": [item for item in errors if not item.startswith("console:")],
        "screenshot": str(SCREENSHOT),
        "redaction": {
            "rawTargetPersisted": False,
            "rawPasswordPersisted": False,
            "rawSecretsPersisted": False,
        },
    }
    ARTIFACT.parent.mkdir(parents=True, exist_ok=True)
    ARTIFACT.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if errors:
        raise RuntimeError(json.dumps(result, ensure_ascii=False, indent=2))
    return result


if __name__ == "__main__":
    print(json.dumps(run_smoke(), ensure_ascii=False, indent=2))
