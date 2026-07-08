#!/usr/bin/env python3
"""Source-contract smoke for the v0.2.2 Web hotfix slices.

This is intentionally lightweight: browser/manual smokes still verify runtime
behavior, while this script records whether the codebase keeps the hotfix
contracts visible and reviewable.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def contains(source: str, needle: str) -> bool:
    return needle in source


def not_contains(source: str, needle: str) -> bool:
    return needle not in source


def check_slice(name: str, checks: list[tuple[str, bool]]) -> dict[str, Any]:
    failed = [label for label, ok in checks if not ok]
    return {
        "name": name,
        "status": "PASS" if not failed else "FAIL",
        "checks": [{"label": label, "status": "PASS" if ok else "FAIL"} for label, ok in checks],
        "failed": failed,
    }


def run() -> dict[str, Any]:
    app = read("desktop/src/App.tsx")
    message = read("desktop/src/components/MessageContent.tsx")
    tokens = read("desktop/src/styles/tokens.css")
    app_css = read("desktop/src/styles/app.css")
    console_js = read("channel/web/static/js/console.js")
    console_css = read("channel/web/static/css/console.css")
    chat_html = read("channel/web/chat.html")
    web_channel = read("channel/web/web_channel.py")
    release_notes = read("common/ecorex_release_notes.py")
    cli_version = read("cli/VERSION")
    admin_api = read("deploy/ecorex-admin-api/ecorex_admin_api.py")
    admin_html = read("deploy/ecorex-site/admin/index.html")
    admin_js = read("deploy/ecorex-site/admin/admin.js")
    site_html = read("deploy/ecorex-site/index.html")
    site_css = read("deploy/ecorex-site/styles.css")
    install_ps1 = read("deploy/ecorex-site/install-webui.ps1")
    install_sh = read("deploy/ecorex-site/install-webui.sh")
    motion_smoke = read("scripts/smoke-web-status-motion-browser.py")

    slices = [
        check_slice("HFX-01 session isolation", [
            ("new session clears message state ref", contains(app, "messagesRef.current = emptyMessages")),
            ("restore cache syncs message ref before render", contains(app, "messagesRef.current = nextMessages")),
            ("new session view uses empty visible messages", contains(app, "const isNewSessionView = visibleMessages.length === 0 && !hasPendingAssistantMessage;")),
        ]),
        check_slice("HFX-02 sweep removed", [
            ("desktop css has no ecorex text sweep keyframe", not_contains(app_css, "ecorex-text-sweep")),
            ("legacy css has no ecorexTextSweep keyframe", not_contains(console_css, "ecorexTextSweep")),
            ("motion smoke asserts no sweep animation", contains(motion_smoke, "should not sweep animate")),
        ]),
        check_slice("HFX-03 codex-like new session", [
            ("chat pane marks new session state", contains(app, 'chat-pane${isNewSessionView ? " is-new-session" : ""}')),
            ("new session headline present", contains(app, "和EcoreX一起开始工作")),
            ("project folder entry present", contains(app, "项目文件夹")),
            ("project start menu lists existing projects", contains(app, "projectStartMatches")),
            ("project start menu can import new folder", contains(app, "导入新文件夹")),
            ("project start menu can start without project", contains(app, "不使用项目")),
            ("general conversation entry present", contains(app, "通用会话")),
            ("legacy web static welcome uses codex-like headline", contains(chat_html, "和EcoreX一起开始工作")),
            ("legacy config exposes explicit welcome title", contains(web_channel, '"welcome_title": welcome_title')),
            ("legacy console does not fall back welcome to product title", contains(console_js, "data.welcome_title || '和EcoreX一起开始工作'")),
            ("legacy web static welcome has no old example cards", not_contains(chat_html, "example-card")),
            ("legacy newChat builds codex-like welcome", contains(console_js, "function createCodexLikeWelcomeScreen()")),
        ]),
        check_slice("HFX-03b run timing disclosure", [
            ("app computes assistant run timing label", contains(app, "function messageRunTimingLabel")),
            ("message content accepts run timing label", contains(message, "runTimingLabel?: string")),
            ("process summary renders run timing", contains(message, "agent-process-timing")),
            ("process timing has css treatment", contains(app_css, ".agent-process-timing")),
            ("browser smoke asserts process summary timing", contains(read("scripts/smoke-web-hotfix-react-browser.py"), "run elapsed timing is not visible in process summary")),
            ("online smoke sends a real timing probe", contains(read("scripts/smoke-v022-online-web-browser.py"), "run timing smoke failed")),
            ("release gate requires online final timing", contains(read("scripts/check-v022-release-gate.py"), "must prove final run timing is visible")),
        ]),
        check_slice("HFX-04 version surfaces", [
            ("cli version is v0.2.2", cli_version.strip() == "0.2.2"),
            ("release notes version v0.2.2", contains(release_notes, '"version": "0.2.2"')),
            ("web bridge reports dynamic runtime version", contains(web_channel, "var WEB_APP_VERSION = __ECOREX_WEB_APP_VERSION__;")),
            ("admin api version v0.2.6", contains(admin_api, 'VERSION = "0.2.6"')),
            ("admin html fallback v0.2.2", contains(admin_html, 'data-metric="version">0.2.2')),
            ("admin js fallback v0.2.2", contains(admin_js, 'version: "0.2.2"')),
            ("site html fallback v0.2.2", contains(site_html, "<strong data-version>0.2.2</strong>")),
            ("installer ps1 v0.2.2", contains(install_ps1, "EcoreX WebUI installer script: 0.2.2")),
            ("installer sh v0.2.2", contains(install_sh, "EcoreX WebUI installer script: 0.2.2")),
        ]),
        check_slice("HFX-05 feishu writeback", [
            ("feishu register calls channel connect", contains(web_channel, 'ChannelsHandler()._handle_connect("feishu"')),
            ("feishu writeback reports channel configured", contains(web_channel, '"channel_configured": writeback.get("status") == "success"')),
            ("feishu writeback reports capability refresh", contains(web_channel, '"capability_refresh_required": bool(payload.get("capability_refresh_required"))')),
            ("feishu register response does not return raw app secret", not_contains(web_channel, '"app_secret": app_secret')),
            ("feishu frontend does not reconnect with raw app secret", not_contains(console_js, "connectFeishuAfterRegister")),
            ("feishu register logs use redaction helper", contains(web_channel, "def _redact_feishu_register_text(value: Any) -> str:")),
            ("feishu redaction handles json or colon secrets", contains(web_channel, "secret_key = r\"(?:app[_-]?secret|client[_-]?secret|token|password|credential|api[_-]?key)\"")),
            ("feishu register accepts sdk credential shape drift", contains(web_channel, "extract_feishu_register_credentials(result)")),
            ("webui core deps include lark-oapi", contains(read("runtime-packs/core-requirements.txt"), "lark-oapi>=1.5.5")),
            ("packaged runtime core deps include lark-oapi", contains(read("desktop/runtime/ecorex-runtime/core-requirements.txt"), "lark-oapi>=1.5.5")),
            ("windows webui package preinstalls active runtime lark_oapi", contains(read("scripts/prepare-ecorex-webui-local-release.ps1"), 'Install-WindowsRuntimeDependency -RuntimeDir $winRuntime -ModuleName "lark_oapi"')),
            ("windows webui installer does not pip install lark_oapi on first run", not_contains(read("scripts/prepare-ecorex-webui-local-release.ps1"), 'Ensure-PythonDependency -Python $python -StateDir $stateDir -ModuleName "lark_oapi"')),
        ]),
        check_slice("HFX-06 collapse and artifact dedupe", [
            ("general collapse excludes merely active row", contains(app, "generalSessions.some((row) => sessionRowNeedsReveal(row, { includeActive: false }))")),
            ("react artifact dedupe normalizes full source", contains(app, "function normalizeArtifactKeySource(value?: string)")),
            ("react artifact dedupe avoids basename-only images", not_contains(app, 'return `image:${fileName}`;')),
            ("message artifact shelf avoids basename-only images", not_contains(message, 'return `image:${fileName}`;')),
            ("legacy artifact canonical key exists", contains(console_js, "function canonicalArtifactDedupeKey")),
            ("legacy artifact dedupe avoids basename-only images", not_contains(console_js, "image:${basename}")),
            ("legacy artifact append dedupes DOM", contains(console_js, "function appendArtifactCard(mediaEl, artifact)")),
        ]),
        check_slice("HFX-09 auth identity projection", [
            ("login identity is not local fallback", contains(web_channel, "localFallback: !hasProvidedIdentity")),
            ("python session identity is not local fallback", contains(web_channel, '"localFallback": not has_provided_identity')),
            ("login email is sent to local auth", contains(web_channel, "body: { email: input.email, password: input.password }")),
            ("generic auth-required local session is rejected", contains(web_channel, "if (authRequired && !(identity && identity.email)) return null;")),
            ("auth check can restore cookie identity session", contains(web_channel, 'payload["session"] = AuthLoginHandler._session_payload(email)')),
            ("web bridge consumes auth check session", contains(web_channel, "writeLocalSession(auth.session);")),
        ]),
        check_slice("HFX-10 streaming smoothness", [
            ("stream render throttle lowered", contains(message, "const STREAM_RENDER_THROTTLE_CHARS = 1200;")),
            ("long live markdown uses windowed render", contains(message, "const STREAM_LIVE_FULL_RENDER_CHARS = 12000;")),
            ("streaming tail renders through MarkdownBlock", contains(message, "<MarkdownBlock content={cleanedTail}")),
            ("streaming no longer uses plain-text LiveStreamingText", not_contains(message, "function LiveStreamingText")),
            ("react markdown uses markdown-it", contains(message, "new MarkdownIt({")),
            ("aggressive no-space heading normalization removed", not_contains(message, 'replace(/^(\\\\s{0,3}#{1,6})(\\\\S)/')),
        ]),
        check_slice("HFX-11 font baseline", [
            ("desktop system ui stack", contains(tokens, "-apple-system") and contains(tokens, "BlinkMacSystemFont")),
            ("desktop mono stack", contains(tokens, "ui-monospace") and contains(tokens, '"SFMono-Regular"')),
            ("legacy console system ui stack", contains(console_css, "--font-sans: -apple-system")),
            ("legacy chat tailwind system ui stack", contains(chat_html, "BlinkMacSystemFont") and contains(chat_html, "ui-monospace")),
            ("public site font baseline", contains(site_css, "--font-sans: -apple-system")),
        ]),
        check_slice("HFX-12 post-release regression hardening", [
            ("history merge preserves local assistant run timing", contains(app, "function mergeLocalAssistantRunTiming") and contains(app, "runTiming: {")),
            ("project row starts a fresh project draft", contains(app, "const selectOrCreateProjectSession = (project: ProjectFolder) => {\n    startNewSession(project);\n  };")),
            ("project row no longer selects an existing project session first", not_contains(app, "const existing = allSessions.find((row) => row.projectId === project.id);")),
            ("mobile web shell keeps chat pane usable", contains(app_css, "@media (max-width: 720px)") and contains(app_css, "grid-template-columns: 64px minmax(0, 1fr);")),
            ("browser smoke covers delayed stale history race", contains(read("scripts/smoke-web-hotfix-react-browser.py"), "lateHistoryRaceSuppressed")),
            ("browser smoke covers artifact menu Escape", contains(read("scripts/smoke-web-hotfix-react-browser.py"), "artifactMenuEscape")),
            ("browser smoke covers project menu outside click", contains(read("scripts/smoke-web-hotfix-react-browser.py"), "projectMenuOutsideClick")),
            ("browser smoke covers chat file menu outside click", contains(read("scripts/smoke-web-hotfix-react-browser.py"), "chatFileMenuOutsideClick")),
            ("browser smoke covers run timing after history refresh", contains(read("scripts/smoke-web-hotfix-react-browser.py"), "runTimingAfterHistoryRefresh")),
            ("browser smoke covers narrow viewport overflow", contains(read("scripts/smoke-web-hotfix-react-browser.py"), "narrowViewport")),
            ("browser smoke defaults to R22-13 artifacts", contains(read("scripts/smoke-web-hotfix-react-browser.py"), "r22-13-react-browser-smoke.json")),
        ]),
    ]
    status = "PASS" if all(item["status"] == "PASS" for item in slices) else "FAIL"
    return {
        "status": status,
        "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "slices": slices,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run v0.2.2 Web hotfix source-contract smoke.")
    parser.add_argument("--artifact", default="", help="Optional JSON artifact path.")
    args = parser.parse_args()
    result = run()
    if args.artifact:
        target = Path(args.artifact)
        if not target.is_absolute():
            target = ROOT / target
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
