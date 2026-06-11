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

function ConvertTo-FileUrl {
    param([string]$Path)
    return ([uri](Resolve-Path -LiteralPath $Path).Path).AbsoluteUri
}

function Invoke-EdgeScreenshot {
    param(
        [string]$Edge,
        [string]$Url,
        [string]$Output,
        [string]$Size
    )

    if (Test-Path -LiteralPath $Output) {
        Remove-Item -LiteralPath $Output -Force
    }

    & $Edge `
        --headless=new `
        --disable-gpu `
        --hide-scrollbars `
        --allow-file-access-from-files `
        --window-size=$Size `
        --virtual-time-budget=5000 `
        --screenshot=$Output `
        $Url | Out-Null

    if ($LASTEXITCODE -ne 0) {
        throw "Edge screenshot failed for $Url"
    }

    if (-not (Test-Path -LiteralPath $Output)) {
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
$edge = Resolve-Edge $EdgePath
$outputPath = Join-Path (Resolve-Path -LiteralPath ".").Path $OutputDir
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
  const session = {
    authenticated: true,
    expiresAt: new Date(Date.now() + 86400000).toISOString(),
    deviceId: "visual-smoke-device",
    user: {
      id: "user-visual",
      name: "企业用户",
      email: "user@ecorex.local",
      role: "member",
      status: "active"
    },
    quota: { dailyLimit: 100000, weeklyLimit: 500000 }
  };
  const packs = [
    { id: "office-pdf", name: "Office/PDF", summary: "文档解析", installMode: "user-or-admin", estimatedSizeMb: 160, state: "not-installed", message: "首次使用时安装", installed: false, policyMode: "ask" },
    { id: "browser-automation", name: "Playwright", summary: "网页自动化", installMode: "admin-recommended", estimatedSizeMb: 220, state: "installed", message: "已安装", installed: true, policyMode: "preinstall" },
    { id: "feishu-lark", name: "飞书/Lark", summary: "办公协作", installMode: "user-or-admin", estimatedSizeMb: 80, state: "not-installed", message: "需要时安装", installed: false, policyMode: "ask" }
  ];
  const history = [
    { role: "user", content: "整理今天的广告投放日报，并预览附件。", seq: 1, user_seq: 1, created_at: Date.now() / 1000 - 180 },
    { role: "assistant", content: "我会先读取附件，整理指标变化，再在发送到飞书前向你确认。", seq: 2, user_seq: 1, created_at: Date.now() / 1000 - 120 }
  ];
  window.ecorexDesktop = {
    platform: "win32",
    shouldUseDarkColors: theme === "dark",
    getSidecarStatus: async () => ({ state: "running", message: "本地运行时已连接", pid: 4242, webPort: 9899 }),
    onSidecarStatus: () => () => {},
    getEnterpriseSession: async () => mode === "auth" ? null : session,
    enterpriseLogin: async () => session,
    enterpriseLogout: async () => null,
    enterpriseChangePassword: async () => ({ ...session, user: { ...session.user, mustChangePassword: false } }),
    checkEnterpriseQuota: async () => ({ ok: true, quota: { allowed: true, dailyUsed: 1200, weeklyUsed: 5400, dailyLimit: 100000, weeklyLimit: 500000 } }),
    refreshEnterprisePolicy: async () => ({ configured: true, changed: false, restarted: false, message: "模型策略已同步", model: "gpt-5.5", provider: "EcoreX" }),
    reportTelemetry: async () => ({ ok: true }),
    listCapabilityPacks: async () => packs,
    installCapabilityPack: async (packId) => {
      const pack = packs.find((item) => item.id === packId) || packs[0];
      pack.installed = true;
      pack.state = "installed";
      pack.message = "已安装";
      return pack;
    },
    getPermissionState: async () => ({ mode: "smart-ask", grantsCount: 3, auditPath: "visual-smoke", updatedAt: new Date().toISOString() }),
    setPermissionMode: async (mode) => ({ mode, grantsCount: 3, auditPath: "visual-smoke", updatedAt: new Date().toISOString() }),
    resetPermissionGrants: async () => ({ mode: "smart-ask", grantsCount: 0, auditPath: "visual-smoke", updatedAt: new Date().toISOString() }),
    getTelemetryState: async () => ({ configured: true, eventsUrl: "mock", deviceId: "visual-smoke-device", userEmail: session.user.email }),
    chooseFiles: async () => [{ file_path: "C:\\EcoreX\\creative-review.pdf", file_name: "creative-review.pdf", file_type: "file" }],
    savePastedFile: async (input) => ({ file_path: "C:\\EcoreX\\" + (input.fileName || "paste.png"), file_name: input.fileName || "paste.png", file_type: (input.mimeType || "").startsWith("image/") ? "image" : "file" }),
    openPath: async () => "",
    apiJson: async (request) => {
      const path = request.path || "";
      if (path === "/api/version") return { version: "0.1.10" };
      if (path.startsWith("/api/sessions/") && path.endsWith("/generate_title")) return { status: "success", title: "广告投放日报" };
      if (path.startsWith("/api/sessions")) return { sessions: [{ session_id: "ads-growth", title: "亦芯广告增长项目", msg_count: 4, last_active: new Date().toISOString() }], total: 1 };
      if (path.startsWith("/api/history")) return { messages: history };
      if (path === "/api/tools") return { tools: [{ name: "file" }, { name: "browser" }, { name: "mcp" }] };
      if (path === "/api/skills") return { skills: [{ name: "日报整理" }, { name: "网页搜索" }] };
      if (path === "/api/models") return { providers: [{ id: "ecorex", model: "gpt-5.5" }], capabilities: [{ name: "chat" }] };
      if (path === "/message") return { status: "success", inline_reply: "已生成日报草稿。发送到飞书前会先等待你确认。", usage: { inputTokens: 38, outputTokens: 44, totalTokens: 82, model: "gpt-5.5" } };
      if (path === "/cancel") return { status: "success", cancelled: 1 };
      if (path === "/api/messages/delete") return { status: "success", deleted: 2 };
      return { status: "success" };
    }
  };
})();
</script>
'@

    $indexHtml = $indexHtml -replace '<script type="module"', "$mockBridge`n    <script type=`"module`""
    Set-Content -Encoding UTF8 -LiteralPath $indexPath -Value $indexHtml

    $url = ConvertTo-FileUrl $indexPath
    $captures = @()
    $captures += Invoke-EdgeScreenshot $edge "$url?mode=auth&theme=light" (Join-Path $outputPath "desktop-auth-light.png") "900,700"
    $captures += Invoke-EdgeScreenshot $edge "$url?mode=main&theme=light" (Join-Path $outputPath "desktop-main-light.png") "1440,900"
    $captures += Invoke-EdgeScreenshot $edge "$url?mode=main&theme=dark" (Join-Path $outputPath "desktop-main-dark.png") "1440,900"

    [ordered]@{
        status = "pass"
        edge = $edge
        outputDir = $outputPath
        captures = $captures
    } | ConvertTo-Json -Depth 4
} finally {
    if (Test-Path -LiteralPath $workDir) {
        Remove-Item -LiteralPath $workDir -Recurse -Force
    }
}
