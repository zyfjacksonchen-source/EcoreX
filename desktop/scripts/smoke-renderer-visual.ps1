param(
    [string]$DistDir = "dist",
    [string]$OutputDir = "..\tmp\ecorex-desktop-visual",
    [string]$EdgePath = "",
    [string]$EvidencePath = "..\docs\v0.1.18\acceptance-smoke.json"
)

$ErrorActionPreference = "Stop"

function Resolve-Edge {
    param([string]$PreferredPath)

    if ($PreferredPath -and (Test-Path -LiteralPath $PreferredPath)) {
        return (Resolve-Path -LiteralPath $PreferredPath).Path
    }

    $candidates = @(
        "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        "C:\Program Files\Microsoft\Edge\Application\msedge.exe"
    )
    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath $candidate) {
            return $candidate
        }
    }

    throw "Microsoft Edge was not found. Pass -EdgePath to the executable."
}

function Resolve-PlaywrightPython {
    $candidates = @(
        (Join-Path $PSScriptRoot "..\runtime\ecorex-runtime\python\python.exe"),
        "python"
    )

    foreach ($candidate in $candidates) {
        $python = $candidate
        if ($candidate -eq "python") {
            $command = Get-Command python -ErrorAction SilentlyContinue
            if (-not $command) {
                continue
            }
            $python = $command.Source
        }
        elseif (-not (Test-Path -LiteralPath $candidate)) {
            continue
        }

        & $python -c "import importlib.util, sys; sys.exit(0 if importlib.util.find_spec('playwright') else 1)" 2>$null
        if ($LASTEXITCODE -eq 0) {
            return (Resolve-Path -LiteralPath $python).Path
        }
    }

    throw "Playwright is required for visual smoke screenshots. Install the browser-automation capability pack first."
}

function ConvertTo-FileUrl {
    param([string]$Path)
    return ([uri](Resolve-Path -LiteralPath $Path).Path).AbsoluteUri
}

function Invoke-PlaywrightScreenshot {
    param(
        [string]$Python,
        [string]$Url,
        [string]$Output,
        [string]$Size,
        [string]$ExpectedDom = "",
        [string]$Action = ""
    )

    if (Test-Path -LiteralPath $Output) {
        Remove-Item -LiteralPath $Output -Force
    }

    $parts = $Size.Split(",")
    if ($parts.Count -ne 2) {
        throw "Invalid screenshot size: $Size"
    }
    $selector = "body"
    if ($ExpectedDom -match "settings-sheet") {
        $selector = ".settings-sheet"
    }
    elseif ($ExpectedDom -match "auth-panel") {
        $selector = ".auth-panel"
    }
    elseif ($ExpectedDom -match "app-shell|session-sidebar") {
        $selector = ".app-shell"
    }
    elseif ($ExpectedDom -match "skill-mention-popover") {
        $selector = ".skill-mention-popover"
    }
    elseif ($ExpectedDom -match "artifact-shelf|image-preview-popover") {
        $selector = ".artifact-shelf"
    }
    elseif ($ExpectedDom -match "long-answer-disclosure|markdown-content") {
        $selector = ".message.assistant"
    }

    $code = @'
import sys
import json
from pathlib import Path
from playwright.sync_api import sync_playwright

if len(sys.argv) == 2 and sys.argv[1].lower().endswith(".json"):
    with open(sys.argv[1], "r", encoding="utf-8-sig") as fh:
        config = json.load(fh)
    url = config["url"]
    output = config["output"]
    metrics_output = config.get("metricsOutput", "")
    width = config["width"]
    height = config["height"]
    selector = config["selector"]
    expected = config["expected"]
    action = config.get("action", "")
else:
    url, output, width, height, selector, expected = sys.argv[1:7]
    action = ""
    metrics_output = ""
width = int(width)
height = int(height)
with sync_playwright() as p:
    browser = p.chromium.launch(headless=True, args=["--allow-file-access-from-files", "--disable-web-security"])
    page = browser.new_page()
    errors = []
    page.on("console", lambda msg: errors.append(f"console:{msg.type}:{msg.text}"))
    page.on("pageerror", lambda exc: errors.append(f"pageerror:{exc}"))
    page.set_viewport_size({"width": width, "height": height})
    page.goto(url, wait_until="networkidle", timeout=30000)
    action_metrics = {}
    if action in ("open-settings", "open-abilities"):
        page.wait_for_selector(".app-shell", state="visible", timeout=30000)
        page.locator(".sidebar-footer button").first.click(timeout=10000)
        page.wait_for_selector(".settings-sheet", state="visible", timeout=10000)
    if action == "open-abilities":
        page.locator(".settings-nav button").nth(2).click(timeout=10000)
        page.wait_for_selector(".skill-toggle-list", state="visible", timeout=10000)
    elif action == "skill-menu":
        page.wait_for_selector(".app-shell", state="visible", timeout=30000)
        page.locator(".composer textarea").fill("@Skill")
        popover = page.locator(".skill-mention-popover")
        popover.wait_for(state="visible", timeout=10000)
        box = popover.bounding_box()
        if not box:
            browser.close()
            raise SystemExit("Skill mention popover has no bounding box")
        max_height = min(260, height * 0.38) + 4
        if box["height"] > max_height:
            browser.close()
            raise SystemExit(f"Skill mention popover exceeds max height: box={box}, viewport={width}x{height}")
        button_count = popover.locator("button").count()
        if button_count < 40:
            browser.close()
            raise SystemExit(f"Skill mention list is truncated: only {button_count} options")
        scrollable = popover.evaluate("(el) => el.scrollHeight > el.clientHeight")
        if not scrollable:
            browser.close()
            raise SystemExit("Skill mention popover is not scrollable with many skills")
    elif action == "lark-hidden":
        page.wait_for_selector(".app-shell", state="visible", timeout=30000)
        page.locator(".composer textarea").fill("@lark")
        popover = page.locator(".skill-mention-popover")
        popover.wait_for(state="visible", timeout=10000)
        button_count = popover.locator("button").count()
        if button_count != 0:
            browser.close()
            raise SystemExit(f"Lark CLI skills should be hidden from active @ suggestions, found {button_count}")
        if popover.locator(".skill-mention-empty").count() != 1:
            browser.close()
            raise SystemExit("Hidden Lark CLI skill did not show the background hint row")
    elif action == "long-markdown":
        page.wait_for_selector(".app-shell", state="visible", timeout=30000)
        if page.locator(".project-session-list .session-row .session-main").count() > 0:
            page.locator(".project-session-list .session-row .session-main").first.click(timeout=10000)
        elif page.locator(".session-list .session-row .session-main").count() > 0:
            page.locator(".session-list .session-row .session-main").first.click(timeout=10000)
        page.wait_for_selector(".long-answer-disclosure", state="visible", timeout=10000)
        before = page.evaluate("""() => {
            const latest = Array.from(document.querySelectorAll('.message.assistant')).pop();
            return {
                text: latest?.innerText || '',
                nodes: latest?.querySelectorAll('*').length || 0,
                collapsed: !!latest?.querySelector('.long-answer-disclosure')
            };
        }""")
        if "```" in before["text"] or "###" in before["text"]:
            browser.close()
            raise SystemExit("Long markdown collapsed view exposed raw markdown markers")
        started = page.evaluate("performance.now()")
        page.locator(".long-answer-toggle").first.click(timeout=10000)
        page.wait_for_selector(".long-answer-disclosure.is-expanded", state="visible", timeout=10000)
        ended = page.evaluate("performance.now()")
        assistant_text = page.locator(".message.assistant").last.evaluate("(el) => el.textContent || ''", timeout=10000)
        if "###" in assistant_text or "```" in assistant_text:
            browser.close()
            raise SystemExit("Rendered assistant message still exposes raw markdown markers")
        if page.locator(".markdown-content h3, .markdown-content h4, .markdown-content h5").count() == 0:
            browser.close()
            raise SystemExit("Long markdown heading was not rendered as HTML")
        if page.locator(".markdown-content table").count() == 0:
            browser.close()
            raise SystemExit("Long markdown table was not rendered as HTML")
        if page.locator(".markdown-content pre code").count() == 0:
            browser.close()
            raise SystemExit("Long markdown code block was not rendered as HTML")
        after = page.evaluate("""() => {
            const latest = Array.from(document.querySelectorAll('.message.assistant')).pop();
            return {
                text: latest?.innerText || '',
                nodes: latest?.querySelectorAll('*').length || 0,
                markdownBlocks: latest?.querySelectorAll('.markdown-content').length || 0,
                expanded: !!latest?.querySelector('.long-answer-disclosure.is-expanded')
            };
        }""")
        action_metrics = {
            "collapsed": before["collapsed"],
            "beforeNodes": before["nodes"],
            "afterNodes": after["nodes"],
            "markdownBlocks": after["markdownBlocks"],
            "expandMs": ended - started,
            "expanded": after["expanded"],
            "textChars": len(after["text"])
        }
        if action_metrics["expandMs"] > 3000:
            browser.close()
            raise SystemExit(f"Long markdown expand took too long: {action_metrics}")
    elif action == "stream-long":
        page.wait_for_selector(".app-shell", state="visible", timeout=30000)
        page.locator(".composer textarea").fill("Stream a long markdown checklist")
        page.locator(".send-button").click(timeout=10000)
        page.wait_for_function("document.body.innerText.includes('Production Stream')", timeout=10000)
        page.wait_for_timeout(300)
        assistant_text = page.locator(".message.assistant").last.inner_text(timeout=10000)
        if "###" in assistant_text or "```" in assistant_text:
            browser.close()
            raise SystemExit("Streaming assistant message exposed raw markdown markers")
        if "Gate" not in assistant_text or "Markdown" not in assistant_text:
            browser.close()
            raise SystemExit("Streaming markdown table content was not visible")
        page.wait_for_timeout(2200)
        if page.locator(".send-button.stop").count() > 0:
            browser.close()
            raise SystemExit("Send button remained in stop state after streaming response settled")
        if page.locator("text=正在连接后台任务").count() > 0 or page.locator("text=姝ｅ湪杩炴帴鍚庡彴浠诲姟").count() > 0:
            browser.close()
            raise SystemExit("Backend connection status remained after streaming completion")
    elif action == "switch-stream":
        page.wait_for_selector(".app-shell", state="visible", timeout=30000)
        session_buttons = page.locator(".session-list .session-main")
        if session_buttons.count() > 0:
            session_buttons.first.click(timeout=10000)
        page.locator(".composer textarea").fill("Second turn with session switching")
        page.locator(".send-button").click(timeout=10000)
        page.wait_for_function("document.body.innerText.includes('Production Stream')", timeout=10000)
        if session_buttons.count() < 2:
            browser.close()
            raise SystemExit("Switch-stream smoke requires at least two sessions")
        session_buttons.nth(1).click(timeout=10000)
        page.wait_for_timeout(250)
        session_buttons.first.click(timeout=10000)
        page.wait_for_function("document.body.innerText.includes('Production Stream') || document.body.innerText.includes('Switchback verified')", timeout=10000)
        page.wait_for_timeout(1600)
        assistant_text = page.locator(".message.assistant").last.inner_text(timeout=10000)
        if "Switchback verified" not in assistant_text and "Production Stream" not in assistant_text:
            browser.close()
            raise SystemExit("Stream result was not visible after switching sessions back")
        if page.locator(".send-button.stop").count() > 0:
            browser.close()
            raise SystemExit("Send button remained in stop state after switch-stream response settled")
    elif action == "switch-race":
        page.wait_for_selector(".app-shell", state="visible", timeout=30000)
        session_buttons = page.locator(".session-list .session-main")
        if session_buttons.count() < 2:
            browser.close()
            raise SystemExit("Switch-race smoke requires at least two sessions")
        session_buttons.first.click(timeout=10000)
        initial_assistant_count = page.locator(".message.assistant").count()
        if initial_assistant_count == 0:
            browser.close()
            raise SystemExit("Switch-race smoke requires an existing first-turn assistant message")
        page.locator(".composer textarea").fill("Second turn race: switch away before first delta")
        page.locator(".send-button").click(timeout=10000)
        page.wait_for_timeout(40)
        session_buttons.nth(1).click(timeout=10000)
        page.wait_for_timeout(450)
        session_buttons.first.click(timeout=10000)
        page.wait_for_function(
            """(initialCount) => {
                const messages = Array.from(document.querySelectorAll('.message.assistant'));
                if (messages.length <= initialCount) return false;
                const latest = messages[messages.length - 1]?.innerText || '';
                return latest.trim().length > 0;
            }""",
            arg=initial_assistant_count,
            timeout=1000
        )
        page.wait_for_function("document.body.innerText.includes('Switchback verified') || document.body.innerText.includes('Production Stream')", timeout=10000)
        assistant_text = page.locator(".message.assistant").last.inner_text(timeout=10000)
        if "###" in assistant_text or "```" in assistant_text:
            browser.close()
            raise SystemExit("Switch-race stream exposed raw markdown markers after returning")
        page.wait_for_timeout(1800)
        if page.locator(".send-button.stop").count() > 0:
            browser.close()
            raise SystemExit("Send button remained in stop state after switch-race response settled")
    elif action == "stream-100k":
        page.wait_for_selector(".app-shell", state="visible", timeout=30000)
        page.locator(".composer textarea").fill("Stream a 100k markdown stress response")
        page.locator(".send-button").click(timeout=10000)
        page.wait_for_function("window.__streamDoneCount >= 1", timeout=45000)
        stats = page.evaluate("({ leaks: window.__streamMarkerLeaks || [], chars: window.__streamTotalDeltaChars || 0 })")
        if stats["chars"] < 100000:
            browser.close()
            raise SystemExit(f"100k stream did not deliver enough delta characters: {stats['chars']}")
        if stats["leaks"]:
            browser.close()
            raise SystemExit(f"100k stream exposed raw markdown markers during delta rendering: {stats['leaks'][:3]}")
        try:
            page.wait_for_function("document.body.innerText.includes('100K-DONE')", timeout=30000)
        except Exception:
            stats = page.evaluate("""() => ({
                done: window.__streamDoneCount || 0,
                chars: window.__streamTotalDeltaChars || 0,
                leaks: window.__streamMarkerLeaks || [],
                assistants: document.querySelectorAll('.message.assistant').length,
                body: (document.body.innerText || '').slice(0, 1200),
                uiState: (localStorage.getItem('ecorex-session-ui-state') || '').slice(0, 1200)
            })""")
            if "100K-DONE" not in (stats.get("body") or "") and "100K-DONE" not in (stats.get("uiState") or ""):
                browser.close()
                raise SystemExit(f"100k stream final view did not include completion marker: {stats}")
        assistant_text = page.evaluate("""() => {
            const messages = Array.from(document.querySelectorAll('.message.assistant'));
            const latest = messages.length ? messages[messages.length - 1].textContent || '' : '';
            return latest || document.body.innerText || '';
        }""")
        if "100K-DONE" not in assistant_text:
            browser.close()
            raise SystemExit("100k stream final view did not include completion marker")
        if "###" in assistant_text or "```" in assistant_text:
            browser.close()
            raise SystemExit("100k stream final view exposed raw markdown markers")
        if page.locator(".send-button.stop").count() > 0:
            browser.close()
            raise SystemExit("Send button remained in stop state after 100k stream")
        action_metrics = page.evaluate("""() => {
            const messages = Array.from(document.querySelectorAll('.message.assistant'));
            const latest = messages[messages.length - 1] || document.body;
            return {
                deliveredChars: window.__streamTotalDeltaChars || 0,
                markerLeaks: (window.__streamMarkerLeaks || []).length,
                latestTextChars: (latest.innerText || '').length,
                latestDomNodes: latest.querySelectorAll('*').length,
                markdownBlocks: latest.querySelectorAll('.markdown-content').length,
                longDisclosure: latest.querySelectorAll('.long-answer-disclosure').length,
                stopButtons: document.querySelectorAll('.send-button.stop').length
            };
        }""")
        if action_metrics["latestDomNodes"] > 2500:
            browser.close()
            raise SystemExit(f"100k stream rendered too many DOM nodes: {action_metrics}")
    elif action == "postdone-tail":
        page.wait_for_selector(".app-shell", state="visible", timeout=30000)
        page.locator(".composer textarea").fill("Stream with post-done artifact tail")
        page.locator(".send-button").click(timeout=10000)
        page.wait_for_function("document.body.innerText.includes('Production Stream')", timeout=10000)
        try:
            page.wait_for_function("document.querySelectorAll('.artifact-shelf').length > 0", timeout=30000)
        except Exception:
            stats = page.evaluate("({ attempts: window.__postDoneArtifactAttempts || 0, emitted: window.__postDoneArtifactEmitted || 0, closed: window.__postDoneArtifactClosed || 0, done: window.__streamDoneCount || 0 })")
            browser.close()
            raise SystemExit(f"Post-done artifact tail did not render: {stats}")
        if page.locator(".send-button.stop").count() > 0:
            browser.close()
            raise SystemExit("Send button remained in stop state after post-done tail")
        if page.locator("text=正在连接后台任务").count() > 0 or page.locator("text=姝ｅ湪杩炴帴鍚庡彴浠诲姟").count() > 0:
            browser.close()
            raise SystemExit("Backend connection status remained after post-done tail")
    elif action == "terminal-boundary":
        page.wait_for_selector(".app-shell", state="visible", timeout=30000)
        page.locator(".composer textarea").fill("Stream terminal boundary race")
        page.locator(".send-button").click(timeout=10000)
        page.wait_for_function("document.body.innerText.includes('BOUNDARY-FINAL')", timeout=15000)
        assistant_text = page.locator(".message.assistant").last.inner_text(timeout=10000)
        if "Boundary prefix" not in assistant_text or "BOUNDARY-FINAL" not in assistant_text:
            browser.close()
            raise SystemExit(f"Terminal boundary lost flushed delta or final text: {assistant_text[:240]}")
        if page.locator(".send-button.stop").count() > 0:
            browser.close()
            raise SystemExit("Send button remained in stop state after terminal boundary")
        mutation_count = page.evaluate("""() => new Promise((resolve) => {
            let count = 0;
            const observer = new MutationObserver((mutations) => { count += mutations.length; });
            observer.observe(document.body, { childList: true, subtree: true, attributes: true, characterData: true });
            setTimeout(() => {
                observer.disconnect();
                resolve(count);
            }, 900);
        })""")
        if mutation_count > 80:
            browser.close()
            raise SystemExit(f"Terminal boundary kept mutating after completion: {mutation_count}")
        body_text = page.evaluate("document.body.innerText || ''")
        if "正在连接后台任务" in body_text or "Response stalled" in body_text:
            browser.close()
            raise SystemExit("Terminal boundary revived waiting/stalled UI after completion")
    elif action == "artifact-preview":
        page.wait_for_selector(".app-shell", state="visible", timeout=30000)
        if page.locator(".project-session-list .session-row .session-main").count() > 0:
            page.locator(".project-session-list .session-row .session-main").first.click(timeout=10000)
        elif page.locator(".session-list .session-row .session-main").count() > 0:
            page.locator(".session-list .session-row .session-main").first.click(timeout=10000)
        page.wait_for_selector(".artifact-shelf", state="visible", timeout=10000)
        thumb = page.locator(".artifact-row-icon img").first
        thumb.wait_for(state="visible", timeout=10000)
        natural_width = thumb.evaluate("(img) => img.naturalWidth")
        if natural_width <= 0:
            browser.close()
            raise SystemExit("Artifact thumbnail image did not load")
        page.locator(".artifact-row .artifact-row-actions button").first.click(timeout=10000)
        page.wait_for_selector(".image-preview-popover img", state="visible", timeout=10000)
        preview_width = page.locator(".image-preview-popover img").evaluate("(img) => img.naturalWidth")
        if preview_width <= 0:
            browser.close()
            raise SystemExit("Artifact preview image did not load")
        action_metrics = {"thumbNaturalWidth": natural_width, "previewNaturalWidth": preview_width}
    elif action == "send-stability":
        page.wait_for_selector(".app-shell", state="visible", timeout=30000)
        page.locator(".composer textarea").fill("Run a quick smoke response")
        page.locator(".send-button").click(timeout=10000)
        page.wait_for_timeout(1200)
        if page.locator(".send-button.stop").count() > 0:
            browser.close()
            raise SystemExit("Send button remained in stop state after mock response settled")
        if page.locator("text=正在连接后台任务").count() > 0:
            browser.close()
            raise SystemExit("Backend connection status is still shown after task completion")
        first_box = page.locator(".session-list .session-row .session-main").first.bounding_box()
        page.wait_for_timeout(600)
        second_box = page.locator(".session-list .session-row .session-main").first.bounding_box()
        if first_box and second_box and abs(first_box["x"] - second_box["x"]) + abs(first_box["y"] - second_box["y"]) > 1:
            browser.close()
            raise SystemExit("Session summary position shifted after task completion")
    try:
        page.wait_for_selector(selector, state="visible", timeout=30000)
    except Exception:
        print("\n".join(errors), flush=True)
        print(page.content()[:3000], flush=True)
        raise
    html = page.content()
    if expected and not any(token in html for token in expected.split("|")):
        browser.close()
        raise SystemExit(f"DOM did not contain expected marker: {expected}")
    if "HTTP ERROR" in html or "This page isn't working" in html:
        browser.close()
        raise SystemExit("Browser error page opened")
    if selector == ".settings-sheet":
        box = page.locator(selector).bounding_box()
        if not box:
            browser.close()
            raise SystemExit("Settings sheet has no bounding box")
        overflows = (
            box["x"] < 0
            or box["y"] < 0
            or box["x"] + box["width"] > width
            or box["y"] + box["height"] > height
        )
        if overflows:
            browser.close()
            raise SystemExit(f"Settings sheet overflows viewport: box={box}, viewport={width}x{height}")
    Path(output).parent.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=output, full_page=False)
    if metrics_output:
        Path(metrics_output).parent.mkdir(parents=True, exist_ok=True)
        Path(metrics_output).write_text(json.dumps(action_metrics, ensure_ascii=False, indent=2), encoding="utf-8")
    browser.close()
'@

    $scriptPath = Join-Path (Split-Path -Parent $Output) "visual-smoke-runner.py"
    $argsPath = [System.IO.Path]::ChangeExtension([System.IO.Path]::GetTempFileName(), ".json")
    $metricsPath = [System.IO.Path]::ChangeExtension($Output, ".metrics.json")
    try {
        Set-Content -Encoding UTF8 -LiteralPath $scriptPath -Value $code
        [ordered]@{
            url = $Url
            output = $Output
            metricsOutput = $metricsPath
            width = [int]$parts[0]
            height = [int]$parts[1]
            selector = $selector
            expected = $ExpectedDom
            action = $Action
        } | ConvertTo-Json -Depth 4 | Set-Content -Encoding UTF8 -LiteralPath $argsPath
        $previousErrorActionPreference = $ErrorActionPreference
        $ErrorActionPreference = "Continue"
        try {
            $pythonOutput = & $Python "$scriptPath" "$argsPath" 2>&1
            $pythonExitCode = $LASTEXITCODE
        }
        finally {
            $ErrorActionPreference = $previousErrorActionPreference
        }
        if ($pythonExitCode -ne 0) {
            if ($pythonOutput) {
                Write-Host $pythonOutput
            }
            throw "Playwright screenshot failed for $Url"
        }
    }
    finally {
        if (Test-Path -LiteralPath $argsPath) {
            Remove-Item -LiteralPath $argsPath -Force
        }
    }

    if (-not (Test-Path -LiteralPath $Output)) {
        if ($pythonOutput) {
            Write-Host $pythonOutput
        }
        throw "Screenshot was not created: $Output"
    }

    $length = (Get-Item -LiteralPath $Output).Length
    if ($length -lt 10000) {
        throw "Screenshot is unexpectedly small ($length bytes): $Output"
    }

    return [ordered]@{
        file = $Output
        bytes = $length
        action = $Action
        metrics = if (Test-Path -LiteralPath $metricsPath) { Get-Content -Raw -Encoding UTF8 -LiteralPath $metricsPath | ConvertFrom-Json } else { $null }
    }
}

$distPath = Resolve-Path -LiteralPath $DistDir
$edge = if ($EdgePath) { Resolve-Edge $EdgePath } else { "" }
$python = Resolve-PlaywrightPython
$outputPath = [System.IO.Path]::GetFullPath((Join-Path (Resolve-Path -LiteralPath ".").Path $OutputDir))
New-Item -ItemType Directory -Path $outputPath -Force | Out-Null

$workDir = Join-Path $env:TEMP ("ecorex-renderer-visual-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $workDir | Out-Null

try {
    Copy-Item -LiteralPath (Join-Path $distPath "assets") -Destination (Join-Path $workDir "assets") -Recurse
    $indexPath = Join-Path $workDir "index.html"
    $indexHtml = Get-Content -Encoding UTF8 -Raw -LiteralPath (Join-Path $distPath "index.html")

    $mockBridge = @'
<script>
(() => {
  const params = new URLSearchParams(window.location.search);
  const mode = params.get("mode") || "main";
  const theme = params.get("theme") || "light";
  const streamModes = new Set(["stream", "switch", "switchrace", "stream100k", "postdone", "terminalrace"]);
  window.__streamMarkerLeaks = [];
  window.__streamTotalDeltaChars = 0;
  window.__streamDoneCount = 0;
  window.__streamLeakCheckPending = false;
  window.__postDoneArtifactAttempts = 0;
  window.__postDoneArtifactEmitted = 0;
  window.__postDoneArtifactClosed = 0;
  window.localStorage.setItem("ecorex-theme", theme);
  window.localStorage.setItem("ecorex-projects", JSON.stringify([{
    id: "project-ecorex",
    name: "EcoreX",
    path: "C:\\EcoreX",
    memoryPath: "C:\\EcoreX\\.ecorex\\project-memory.md",
    dreamsPath: "C:\\EcoreX\\.ecorex\\dreams",
    updatedAt: new Date().toISOString()
  }]));
  window.localStorage.setItem("ecorex-session-projects", JSON.stringify({ "ads-growth": "project-ecorex" }));
  window.localStorage.setItem("ecorex-session-titles", JSON.stringify({ "ads-growth": "\u4ea6\u82af\u5e7f\u544a\u589e\u957f\u9879\u76ee" }));
  const session = {
    authenticated: true,
    expiresAt: new Date(Date.now() + 86400000).toISOString(),
    deviceId: "visual-smoke-device",
    user: {
      id: "user-visual",
      name: "Enterprise User",
      email: "user@ecorex.local",
      role: "member",
      status: "active"
    },
    quota: { dailyLimit: 100000, weeklyLimit: 500000 }
  };
  const packs = [
    { id: "office-pdf", name: "Office/PDF", summary: "Document parsing", installMode: "user-or-admin", estimatedSizeMb: 160, state: "not-installed", message: "Install on first use", installed: false, policyMode: "ask" },
    { id: "browser-automation", name: "Playwright", summary: "Browser automation", installMode: "admin-recommended", estimatedSizeMb: 220, state: "installed", message: "Installed", installed: true, policyMode: "preinstall" },
    { id: "feishu-lark", name: "Feishu/Lark", summary: "Office collaboration", installMode: "user-or-admin", estimatedSizeMb: 80, state: "not-installed", message: "Install when needed", installed: false, policyMode: "ask" }
  ];
  const skillRows = Array.from({ length: 48 }, (_, index) => ({
    name: `Skill ${String(index + 1).padStart(2, "0")}`,
    display_name: `Skill ${String(index + 1).padStart(2, "0")} · Production`,
    description: "Production smoke skill",
    source: "visual-smoke",
    path: `C:\\EcoreX\\skills\\skill-${String(index + 1).padStart(2, "0")}\\SKILL.md`,
    enabled: true
  })).concat(Array.from({ length: 18 }, (_, index) => ({
    name: `lark-${String(index + 1).padStart(2, "0")}`,
    display_name: `Lark CLI ${String(index + 1).padStart(2, "0")}`,
    description: "Lark CLI helper skill",
    source: "extra",
    path: `C:\\Users\\user\\.agents\\skills\\lark-${String(index + 1).padStart(2, "0")}\\SKILL.md`,
    enabled: true
  })));
  const previewDataUrl = "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='640' height='360' viewBox='0 0 640 360'%3E%3Crect width='640' height='360' fill='%23232636'/%3E%3Ccircle cx='520' cy='96' r='44' fill='%23f4c76a'/%3E%3Crect x='56' y='210' width='528' height='58' rx='16' fill='%2359c3c3'/%3E%3Ctext x='56' y='156' font-family='Arial' font-size='56' fill='white'%3EEcoreX Preview%3C/text%3E%3C/svg%3E";
  let streamCounter = 0;
  const streamRecords = {};
  const defaultHistory = [
    { role: "user", content: "Prepare today's ad delivery report and preview the attachment.", seq: 1, user_seq: 1, created_at: Date.now() / 1000 - 180 },
    { role: "assistant", content: "I will read the attachment, summarize metric changes, and ask for confirmation before sending.", seq: 2, user_seq: 1, created_at: Date.now() / 1000 - 120 }
  ];
  const secondaryHistory = [
    { role: "user", content: "Keep this session as a switch target.", seq: 1, user_seq: 1, created_at: Date.now() / 1000 - 240 },
    { role: "assistant", content: "Secondary session is ready.", seq: 2, user_seq: 1, created_at: Date.now() / 1000 - 220 }
  ];
  const longMarkdownHistory = [
    { role: "user", content: "Generate a long production checklist with markdown.", seq: 1, user_seq: 1, created_at: Date.now() / 1000 - 180 },
    {
      role: "assistant",
      seq: 2,
      user_seq: 1,
      created_at: Date.now() / 1000 - 120,
      content: [
        "### Production Readiness",
        "",
        "| Gate | Owner | Status |",
        "| --- | --- | --- |",
        "| Runtime | Desktop | Pass |",
        "| Artifacts | Renderer | Pass |",
        "",
        "```json",
        "{\"version\":\"0.1.18\",\"gate\":\"visual-smoke\"}",
        "```",
        "",
        "1. Keep the first paint rendered as HTML.",
        "2. Keep long answers collapsed without leaking raw markdown.",
        "3. Keep artifacts bound to the owning session.",
        "",
        "This paragraph pads the response so the long answer component must switch into collapsed mode. ".repeat(40)
      ].join("\n")
    }
  ];
  const artifactHistory = [
    { role: "user", content: "Create image deliverables and show thumbnails.", seq: 1, user_seq: 1, created_at: Date.now() / 1000 - 180 },
    {
      role: "assistant",
      seq: 2,
      user_seq: 1,
      request_id: "visual-artifact-request",
      created_at: Date.now() / 1000 - 120,
      content: "Generated the cover image and verified the local artifact shelf.",
      extras: {
        artifacts: [{
          id: "visual-cover",
          kind: "image",
          title: "visual-cover.png",
          path: "/uploads/visual-cover.png",
          thumbnailUrl: previewDataUrl,
          previewUrl: previewDataUrl,
          sizeBytes: 4096,
          stats: { bytesWritten: 4096 }
        }]
      }
    }
  ];
  const streamSmokeHistory = streamModes.has(mode)
    ? (mode === "switchrace" ? defaultHistory : [])
    : null;
  const historyBySession = {
    "ads-growth": streamSmokeHistory || (mode === "long" ? longMarkdownHistory : mode === "artifact" ? artifactHistory : defaultHistory),
    "brand-lab": secondaryHistory
  };
  const history = historyBySession["ads-growth"];
  function recordStreamHistory(record, finalText) {
    const sessionId = record.sessionId || "ads-growth";
    historyBySession[sessionId] = [
      ...(historyBySession[sessionId] || []),
      { role: "user", content: record.prompt || "Stream smoke", seq: 3, user_seq: 2, created_at: Date.now() / 1000 - 2 },
      { role: "assistant", content: finalText, seq: 4, user_seq: 2, request_id: record.requestId, created_at: Date.now() / 1000 - 1 }
    ];
  }
  function emitStream(eventSource, eventId, item) {
    if (item.type === "artifact") window.__postDoneArtifactAttempts += 1;
    if (eventSource.readyState === 2) {
      if (item.type === "artifact") window.__postDoneArtifactClosed += 1;
      return;
    }
    if (item.type === "artifact") window.__postDoneArtifactEmitted += 1;
    const payload = { ...item, request_id: eventSource.requestId };
    eventSource.onmessage?.({ data: JSON.stringify(payload), lastEventId: String(eventId) });
    if (item.type === "delta" || item.type === "message_update") {
      window.__streamTotalDeltaChars += String(item.content || item.text || item.delta || "").length;
      if (window.__streamLeakCheckPending) return;
      window.__streamLeakCheckPending = true;
      setTimeout(() => {
        window.__streamLeakCheckPending = false;
        const messages = Array.from(document.querySelectorAll(".message.assistant"));
        const rawText = messages.length ? messages[messages.length - 1].textContent || "" : "";
        const text = rawText.length > 12000 ? `${rawText.slice(0, 8000)}\n${rawText.slice(-4000)}` : rawText;
        if (text.includes("###") || text.includes("```")) {
          window.__streamMarkerLeaks.push({ eventId, sample: text.slice(0, 180) });
        }
      }, 32);
    }
    if (item.type === "done") {
      window.__streamDoneCount += 1;
    }
  }
  function runStream(eventSource) {
    const record = streamRecords[eventSource.requestId] || {};
    const modeName = record.mode || mode;
    if (modeName === "terminalrace") {
      const finalText = "Boundary prefix BOUNDARY-FINAL";
      setTimeout(() => emitStream(eventSource, 1, { type: "delta", content: "Boundary prefix" }), 30);
      setTimeout(() => emitStream(eventSource, 2, { type: "tool_start", tool: "boundary-check", tool_call_id: "terminal-boundary-tool", input: { ok: true } }), 34);
      setTimeout(() => {
        recordStreamHistory(record, finalText);
        emitStream(eventSource, 3, { type: "done", content: finalText, user_seq: 2, bot_seq: 4 });
      }, 38);
      setTimeout(() => eventSource.onerror?.({ type: "error" }), 180);
      return;
    }
    const stressBody = modeName === "stream100k"
      ? Array.from({ length: 520 }, (_, index) => `Stress paragraph ${index + 1}: ` + "long streaming markdown content ".repeat(8)).join("\n\n")
      : " ".repeat(4000);
    const finalText = [
      "### Production Stream",
      "",
      "| Gate | State |",
      "| --- | --- |",
      "| Markdown | Pass |",
      "| Switchback | Pass |",
      "",
      "```json",
      "{\"stream\":\"ok\",\"version\":\"0.1.18\"}",
      "```",
      "",
      (modeName === "switch" || modeName === "switchrace") ? "Switchback verified after returning to the original session." : "Streaming markdown rendered without raw marker flash.",
      modeName === "stream100k" ? "100K-DONE" : "",
      stressBody
    ].join("\n");
    const chunks = modeName === "stream100k"
      ? Array.from({ length: Math.ceil(finalText.length / 2400) }, (_, index) => finalText.slice(index * 2400, (index + 1) * 2400))
      : [
          finalText.slice(0, 90),
          finalText.slice(90, 220),
          finalText.slice(220, 900),
          finalText.slice(900)
        ];
    chunks.forEach((chunk, index) => {
      const delay = modeName === "stream100k" ? 40 + index * 24 : 80 + index * 140;
      setTimeout(() => emitStream(eventSource, index + 1, { type: "delta", content: chunk }), delay);
    });
    setTimeout(() => {
      recordStreamHistory(record, finalText);
      emitStream(eventSource, 10, { type: "done", content: finalText, user_seq: 2, bot_seq: 4 });
    }, modeName === "stream100k" ? 40 + chunks.length * 24 + 350 : 850);
    if (modeName === "postdone") {
      setTimeout(() => emitStream(eventSource, 11, {
        type: "artifact",
        artifact: {
          id: "postdone-cover",
          kind: "image",
          title: "postdone-cover.png",
          path: "/uploads/postdone-cover.png",
          thumbnailUrl: previewDataUrl,
          previewUrl: previewDataUrl,
          sizeBytes: 4096
        }
      }), 1150);
      setTimeout(() => eventSource.onerror?.({ type: "error" }), 1450);
    }
  }
  class MockEventSource {
    constructor(url) {
      this.url = url;
      this.readyState = 0;
      this.requestId = new URL(url).searchParams.get("request_id") || "";
      setTimeout(() => {
        this.readyState = 1;
        runStream(this);
      }, 20);
    }
    close() {
      this.readyState = 2;
    }
  }
  window.EventSource = MockEventSource;
  window.ecorexDesktop = {
    platform: "win32",
    shouldUseDarkColors: theme === "dark",
    getSidecarStatus: async () => ({ state: "running", message: "Runtime connected", pid: 4242, webPort: 9899 }),
    onSidecarStatus: () => () => {},
    getEnterpriseSession: async () => mode === "auth" ? null : session,
    enterpriseLogin: async () => session,
    enterpriseLogout: async () => null,
    enterpriseChangePassword: async () => ({ ...session, user: { ...session.user, mustChangePassword: false } }),
    checkEnterpriseQuota: async () => ({ ok: true, quota: { allowed: true, dailyUsed: 1200, weeklyUsed: 5400, dailyLimit: 100000, weeklyLimit: 500000 } }),
    refreshEnterprisePolicy: async () => ({ configured: true, changed: false, restarted: false, message: "Model policy synced", model: "gpt-5.5", provider: "EcoreX" }),
    reportTelemetry: async () => ({ ok: true }),
    runtimeToken: async () => "visual-smoke-runtime-token",
    listCapabilityPacks: async () => packs,
    getPermissionState: async () => ({ mode: "smart-ask", grantsCount: 3, auditPath: "visual-smoke", updatedAt: new Date().toISOString() }),
    setPermissionMode: async (mode) => ({ mode, grantsCount: 3, auditPath: "visual-smoke", updatedAt: new Date().toISOString() }),
    resetPermissionGrants: async () => ({ mode: "smart-ask", grantsCount: 0, auditPath: "visual-smoke", updatedAt: new Date().toISOString() }),
    getTelemetryState: async () => ({ configured: true, eventsUrl: "mock", deviceId: "visual-smoke-device", userEmail: session.user.email }),
    statPath: async (filePath) => ({ path: filePath, exists: true, isFile: true, isDirectory: false, sizeBytes: 4096, status: "ready" }),
    chooseFiles: async () => [{ file_path: "C:\\EcoreX\\creative-review.pdf", file_name: "creative-review.pdf", file_type: "file" }],
    savePastedFile: async (input) => ({ file_path: "C:\\EcoreX\\" + (input.fileName || "paste.png"), file_name: input.fileName || "paste.png", file_type: (input.mimeType || "").startsWith("image/") ? "image" : "file" }),
    openPath: async () => "",
    apiJson: async (request) => {
      const path = request.path || "";
      if (path === "/api/version") return { version: "0.1.18" };
      if (path.startsWith("/api/sessions/") && path.endsWith("/generate_title")) return { status: "success", title: "Ad delivery report" };
      if (path.startsWith("/api/sessions")) return {
        sessions: [
          { session_id: "ads-growth", title: "EcoreX ad growth project", msg_count: 4, last_active: new Date().toISOString() },
          { session_id: "brand-lab", title: "Brand lab switch target", msg_count: 2, last_active: new Date(Date.now() - 300000).toISOString() }
        ],
        total: 2
      };
      if (path.startsWith("/api/history")) {
        const query = path.includes("?") ? path.slice(path.indexOf("?") + 1) : "";
        const sessionId = new URLSearchParams(query).get("session_id") || "ads-growth";
        const fallbackHistory = mode === "switchrace" ? defaultHistory : (streamSmokeHistory ? [] : history);
        return { messages: historyBySession[sessionId] || fallbackHistory };
      }
      if (path === "/api/tools") return { tools: [{ name: "file" }, { name: "browser" }, { name: "mcp" }] };
      if (path === "/api/skills") return { skills: skillRows };
      if (path === "/api/models") return { providers: [{ id: "ecorex", model: "gpt-5.5" }], capabilities: [{ name: "chat" }] };
      if (path === "/api/capabilities") return { status: "success", abilities: packs.map((pack) => ({ id: pack.id, packId: pack.id, label: pack.name, notes: pack.summary, kind: "capability-pack", agentCanInstall: true, capabilityState: { installed: pack.installed, state: pack.state, message: pack.message } })) };
      if (path === "/api/agent-install-request") return { status: "success", message: "Install task was handed to the current agent session.", packId: request.body?.packId, packName: request.body?.packName, sessionId: request.body?.sessionId || "ads-growth" };
      if (path === "/message") {
        if (streamModes.has(mode)) {
          const requestId = `visual-${mode}-${++streamCounter}`;
          streamRecords[requestId] = {
            requestId,
            mode: mode,
            sessionId: request.body?.session_id || "ads-growth",
            prompt: request.body?.content || request.body?.message || "Stream smoke"
          };
          return { status: "success", request_id: requestId, stream: true, usage: { inputTokens: 38, outputTokens: 44, totalTokens: 82, model: "gpt-5.5" } };
        }
        return { status: "success", inline_reply: "Draft generated. I will wait for confirmation before sending to Feishu.", usage: { inputTokens: 38, outputTokens: 44, totalTokens: 82, model: "gpt-5.5" } };
      }
      if (path === "/cancel") return { status: "success", cancelled: 1 };
      if (path === "/api/messages/delete") return { status: "success", deleted: 2 };
      return { status: "success" };
    }
  };
})();
</script>
'@

    $indexHtml = $indexHtml.Replace('<script type="module"', "$mockBridge`n    <script type=`"module`"")
    Set-Content -Encoding UTF8 -LiteralPath $indexPath -Value $indexHtml
    Copy-Item -LiteralPath $indexPath -Destination (Join-Path $outputPath "visual-smoke-index.html") -Force

    $url = ConvertTo-FileUrl $indexPath
    $captures = @()
    $captures += Invoke-PlaywrightScreenshot $python "${url}?mode=auth&theme=light" (Join-Path $outputPath "desktop-auth-light.png") "900,700" "auth-panel"
    $captures += Invoke-PlaywrightScreenshot $python "${url}?mode=main&theme=light" (Join-Path $outputPath "desktop-main-light.png") "1440,900" "app-shell|session-sidebar"
    $captures += Invoke-PlaywrightScreenshot $python "${url}?mode=main&theme=light" (Join-Path $outputPath "desktop-settings-light.png") "1440,900" "settings-sheet" "open-settings"
    $captures += Invoke-PlaywrightScreenshot $python "${url}?mode=main&theme=light" (Join-Path $outputPath "desktop-abilities-light.png") "1440,900" "settings-sheet" "open-abilities"
    $captures += Invoke-PlaywrightScreenshot $python "${url}?mode=skills&theme=light" (Join-Path $outputPath "desktop-skill-menu-light.png") "1440,900" "skill-mention-popover" "skill-menu"
    $captures += Invoke-PlaywrightScreenshot $python "${url}?mode=skills&theme=light" (Join-Path $outputPath "desktop-skill-lark-hidden-light.png") "1440,900" "skill-mention-popover" "lark-hidden"
    $captures += Invoke-PlaywrightScreenshot $python "${url}?mode=long&theme=light" (Join-Path $outputPath "desktop-long-markdown-light.png") "1440,900" "long-answer-disclosure|markdown-content" "long-markdown"
    $captures += Invoke-PlaywrightScreenshot $python "${url}?mode=stream&theme=light" (Join-Path $outputPath "desktop-stream-long-markdown-light.png") "1440,900" "markdown-content" "stream-long"
    $captures += Invoke-PlaywrightScreenshot $python "${url}?mode=switch&theme=light" (Join-Path $outputPath "desktop-switch-stream-light.png") "1440,900" "markdown-content" "switch-stream"
    $captures += Invoke-PlaywrightScreenshot $python "${url}?mode=switchrace&theme=light" (Join-Path $outputPath "desktop-switch-race-light.png") "1440,900" "markdown-content" "switch-race"
    $captures += Invoke-PlaywrightScreenshot $python "${url}?mode=stream100k&theme=light" (Join-Path $outputPath "desktop-stream-100k-light.png") "1440,900" "markdown-content" "stream-100k"
    $captures += Invoke-PlaywrightScreenshot $python "${url}?mode=postdone&theme=light" (Join-Path $outputPath "desktop-postdone-tail-light.png") "1440,900" "artifact-shelf|markdown-content" "postdone-tail"
    $captures += Invoke-PlaywrightScreenshot $python "${url}?mode=terminalrace&theme=light" (Join-Path $outputPath "desktop-terminal-boundary-light.png") "1440,900" "markdown-content" "terminal-boundary"
    $captures += Invoke-PlaywrightScreenshot $python "${url}?mode=artifact&theme=light" (Join-Path $outputPath "desktop-artifact-preview-light.png") "1440,900" "artifact-shelf|image-preview-popover" "artifact-preview"
    $captures += Invoke-PlaywrightScreenshot $python "${url}?mode=main&theme=light" (Join-Path $outputPath "desktop-send-stability-light.png") "1440,900" "app-shell|session-sidebar" "send-stability"
    $captures += Invoke-PlaywrightScreenshot $python "${url}?mode=main&theme=dark" (Join-Path $outputPath "desktop-main-dark.png") "1440,900" "app-shell|session-sidebar"
    $captures += Invoke-PlaywrightScreenshot $python "${url}?mode=main&theme=dark" (Join-Path $outputPath "desktop-settings-dark.png") "1440,900" "settings-sheet" "open-settings"
    $captures += Invoke-PlaywrightScreenshot $python "${url}?mode=main&theme=dark" (Join-Path $outputPath "desktop-abilities-dark.png") "1440,900" "settings-sheet" "open-abilities"

    $captureActions = @{}
    foreach ($capture in $captures) {
        if ($capture.action) {
            $captureActions[$capture.action] = $true
        }
    }

    $result = [ordered]@{
        status = "pass"
        version = "0.1.18"
        changeIds = @("STAB-004", "UX-004", "PERF-001")
        scenarios = [ordered]@{
            noResponseDeadLoop = [ordered]@{
                status = if ($captureActions["terminal-boundary"] -and $captureActions["postdone-tail"]) { "pass" } else { "missing" }
                evidence = "terminal-boundary proves post-terminal onerror does not revive waiting UI; postdone-tail proves tail merge remains visible"
            }
            stalledStream = [ordered]@{
                status = if ($captureActions["switch-race"]) { "pass" } else { "missing" }
                evidence = "switch-race validates reconnect/replay after an interrupted non-first-turn stream without blanking content"
            }
            terminalNoFlicker = [ordered]@{
                status = if ($captureActions["terminal-boundary"]) { "pass" } else { "missing" }
                evidence = "terminal-boundary gates delta/tool/done/onerror ordering, send-button stop state, backend waiting text, and settled mutation budget"
            }
            longMarkdown = [ordered]@{
                status = if ($captureActions["stream-100k"] -and $captureActions["long-markdown"]) { "pass" } else { "missing" }
                chars = 100000
                evidence = "stream-100k gates delivered chars, raw markdown marker leaks, terminal convergence, and stop-button state"
            }
        }
        edge = $edge
        playwrightPython = $python
        outputDir = $outputPath
        captures = $captures
    }
    $json = $result | ConvertTo-Json -Depth 6
    if ($EvidencePath) {
        $resolvedEvidencePath = [System.IO.Path]::GetFullPath((Join-Path (Resolve-Path -LiteralPath ".").Path $EvidencePath))
        New-Item -ItemType Directory -Path (Split-Path -Parent $resolvedEvidencePath) -Force | Out-Null
        [System.IO.File]::WriteAllText($resolvedEvidencePath, $json + [Environment]::NewLine, [System.Text.UTF8Encoding]::new($false))
    }
    $json
} finally {
    if (Test-Path -LiteralPath $workDir) {
        Remove-Item -LiteralPath $workDir -Recurse -Force
    }
}
