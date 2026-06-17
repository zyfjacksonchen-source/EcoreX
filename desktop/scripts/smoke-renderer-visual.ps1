param(
    [string]$DistDir = "dist",
    [string]$OutputDir = "..\tmp\ecorex-desktop-visual",
    [string]$EdgePath = ""
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
    width = config["width"]
    height = config["height"]
    selector = config["selector"]
    expected = config["expected"]
    action = config.get("action", "")
else:
    url, output, width, height, selector, expected = sys.argv[1:7]
    action = ""
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
    if action in ("open-settings", "open-abilities"):
        page.wait_for_selector(".app-shell", state="visible", timeout=30000)
        page.locator(".sidebar-footer button").first.click(timeout=10000)
        page.wait_for_selector(".settings-sheet", state="visible", timeout=10000)
        if action == "open-abilities":
            page.locator(".settings-nav button").nth(2).click(timeout=10000)
            page.wait_for_selector(".skill-toggle-list", state="visible", timeout=10000)
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
    browser.close()
'@

    $scriptPath = Join-Path (Split-Path -Parent $Output) "visual-smoke-runner.py"
    $argsPath = [System.IO.Path]::ChangeExtension([System.IO.Path]::GetTempFileName(), ".json")
    try {
        Set-Content -Encoding UTF8 -LiteralPath $scriptPath -Value $code
        [ordered]@{
            url = $Url
            output = $Output
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
  const history = [
    { role: "user", content: "Prepare today's ad delivery report and preview the attachment.", seq: 1, user_seq: 1, created_at: Date.now() / 1000 - 180 },
    { role: "assistant", content: "I will read the attachment, summarize metric changes, and ask for confirmation before sending.", seq: 2, user_seq: 1, created_at: Date.now() / 1000 - 120 }
  ];
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
    listCapabilityPacks: async () => packs,
    getPermissionState: async () => ({ mode: "smart-ask", grantsCount: 3, auditPath: "visual-smoke", updatedAt: new Date().toISOString() }),
    setPermissionMode: async (mode) => ({ mode, grantsCount: 3, auditPath: "visual-smoke", updatedAt: new Date().toISOString() }),
    resetPermissionGrants: async () => ({ mode: "smart-ask", grantsCount: 0, auditPath: "visual-smoke", updatedAt: new Date().toISOString() }),
    getTelemetryState: async () => ({ configured: true, eventsUrl: "mock", deviceId: "visual-smoke-device", userEmail: session.user.email }),
    chooseFiles: async () => [{ file_path: "C:\\EcoreX\\creative-review.pdf", file_name: "creative-review.pdf", file_type: "file" }],
    savePastedFile: async (input) => ({ file_path: "C:\\EcoreX\\" + (input.fileName || "paste.png"), file_name: input.fileName || "paste.png", file_type: (input.mimeType || "").startsWith("image/") ? "image" : "file" }),
    openPath: async () => "",
    apiJson: async (request) => {
      const path = request.path || "";
      if (path === "/api/version") return { version: "0.1.11" };
      if (path.startsWith("/api/sessions/") && path.endsWith("/generate_title")) return { status: "success", title: "Ad delivery report" };
      if (path.startsWith("/api/sessions")) return { sessions: [{ session_id: "ads-growth", title: "EcoreX ad growth project", msg_count: 4, last_active: new Date().toISOString() }], total: 1 };
      if (path.startsWith("/api/history")) return { messages: history };
      if (path === "/api/tools") return { tools: [{ name: "file" }, { name: "browser" }, { name: "mcp" }] };
      if (path === "/api/skills") return { skills: [{ name: "Daily report" }, { name: "Web search" }] };
      if (path === "/api/models") return { providers: [{ id: "ecorex", model: "gpt-5.5" }], capabilities: [{ name: "chat" }] };
      if (path === "/api/capabilities") return { status: "success", abilities: packs.map((pack) => ({ id: pack.id, packId: pack.id, label: pack.name, notes: pack.summary, kind: "capability-pack", agentCanInstall: true, capabilityState: { installed: pack.installed, state: pack.state, message: pack.message } })) };
      if (path === "/api/agent-install-request") return { status: "success", message: "Install task was handed to the current agent session.", packId: request.body?.packId, packName: request.body?.packName, sessionId: request.body?.sessionId || "ads-growth" };
      if (path === "/message") return { status: "success", inline_reply: "Draft generated. I will wait for confirmation before sending to Feishu.", usage: { inputTokens: 38, outputTokens: 44, totalTokens: 82, model: "gpt-5.5" } };
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
    $captures += Invoke-PlaywrightScreenshot $python "${url}?mode=main&theme=dark" (Join-Path $outputPath "desktop-main-dark.png") "1440,900" "app-shell|session-sidebar"
    $captures += Invoke-PlaywrightScreenshot $python "${url}?mode=main&theme=dark" (Join-Path $outputPath "desktop-settings-dark.png") "1440,900" "settings-sheet" "open-settings"
    $captures += Invoke-PlaywrightScreenshot $python "${url}?mode=main&theme=dark" (Join-Path $outputPath "desktop-abilities-dark.png") "1440,900" "settings-sheet" "open-abilities"

    [ordered]@{
        status = "pass"
        edge = $edge
        playwrightPython = $python
        outputDir = $outputPath
        captures = $captures
    } | ConvertTo-Json -Depth 4
} finally {
    if (Test-Path -LiteralPath $workDir) {
        Remove-Item -LiteralPath $workDir -Recurse -Force
    }
}
