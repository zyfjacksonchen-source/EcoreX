param(
    [string]$InstallerPath = "",
    [string]$InstallDir = "",
    [string]$OutputPath = "",
    [string]$ExpectedVersion = "",
    [string]$ExpectedWinArch = "",
    [int]$Port = 19131,
    [int]$RendererDebugPort = 19231,
    [switch]$KeepInstall
)

$ErrorActionPreference = "Stop"

if (-not $InstallerPath) {
    throw "Pass -InstallerPath with the exact signed installer under test."
}

function Resolve-FullPath {
    param([Parameter(Mandatory = $true)][string]$Path)
    return [System.IO.Path]::GetFullPath($Path)
}

function Stop-ProcessesFromInstallDir {
    param([Parameter(Mandatory = $true)][string]$Dir)

    $needle = $Dir.Replace("\", "\\")
    Get-CimInstance Win32_Process |
        Where-Object {
            $_.CommandLine -and (
                $_.CommandLine.Contains($Dir) -or $_.CommandLine.Contains($needle)
            )
        } |
        ForEach-Object {
            try {
                Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
            }
            catch {}
        }
}

function Wait-JsonEndpoint {
    param(
        [Parameter(Mandatory = $true)][string]$Url,
        [int]$TimeoutSeconds = 60
    )

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    $lastError = ""
    while ((Get-Date) -lt $deadline) {
        try {
            $response = Invoke-WebRequest -UseBasicParsing $Url -TimeoutSec 5
            if ($response.StatusCode -lt 500) {
                return $response.Content | ConvertFrom-Json
            }
        }
        catch {
            $lastError = $_.Exception.Message
        }
        Start-Sleep -Milliseconds 800
    }
    throw "Endpoint did not become ready: $Url. Last error: $lastError"
}

function Wait-RendererDom {
    param(
        [Parameter(Mandatory = $true)][int]$DebugPort,
        [int]$TimeoutSeconds = 60
    )

    if (-not (Get-Command node -ErrorAction SilentlyContinue)) {
        throw "Node.js is required on PATH for installed renderer smoke checks."
    }

    $script = @'
const port = Number(process.argv[2]);
const timeoutSeconds = Number(process.argv[3]);
const deadline = Date.now() + timeoutSeconds * 1000;
const endpoint = `http://127.0.0.1:${port}/json`;
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

function evaluate(wsUrl) {
  return new Promise((resolve, reject) => {
    const ws = new WebSocket(wsUrl);
    const id = 1;
    const timer = setTimeout(() => {
      try {
        ws.close();
      } catch {}
      reject(new Error("CDP evaluate timed out"));
    }, 8000);

    ws.onopen = () => {
      ws.send(JSON.stringify({
        id,
        method: "Runtime.evaluate",
        params: {
          returnByValue: true,
          expression: `(() => {
            const root = document.getElementById("root");
            const bodyText = document.body ? document.body.innerText || "" : "";
            return {
              url: location.href,
              title: document.title,
              rootHtmlLength: root && root.innerHTML ? root.innerHTML.length : 0,
              bodyTextLength: bodyText.length,
              bodyTextSample: bodyText.slice(0, 240)
            };
          })()`
        }
      }));
    };
    ws.onerror = () => {
      clearTimeout(timer);
      reject(new Error("CDP websocket failed"));
    };
    ws.onmessage = (event) => {
      const message = JSON.parse(event.data);
      if (message.id !== id) return;
      clearTimeout(timer);
      try {
        ws.close();
      } catch {}
      if (message.error) {
        reject(new Error(message.error.message || "CDP evaluate failed"));
        return;
      }
      resolve(message.result && message.result.result && message.result.result.value);
    };
  });
}

let last = null;
let lastError = "";
while (Date.now() < deadline) {
  try {
    const response = await fetch(endpoint);
    const targets = await response.json();
    const target = targets.find((item) => item.type === "page" && item.webSocketDebuggerUrl);
    if (!target) {
      throw new Error("No page target found");
    }
    last = await evaluate(target.webSocketDebuggerUrl);
    if (last && last.rootHtmlLength > 0 && last.bodyTextLength > 0) {
      console.log(JSON.stringify({ ok: true, ...last }));
      process.exit(0);
    }
  } catch (error) {
    lastError = error && error.message ? error.message : String(error);
  }
  await sleep(800);
}

console.log(JSON.stringify({ ok: false, lastError, last }));
process.exit(2);
'@

    $tempScript = Join-Path ([System.IO.Path]::GetTempPath()) ("ecorex-renderer-smoke-" + [System.Guid]::NewGuid().ToString("N") + ".mjs")
    $encoding = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($tempScript, $script, $encoding)
    try {
        $output = & node $tempScript $DebugPort $TimeoutSeconds
        $exitCode = $LASTEXITCODE
        $joined = ($output -join [Environment]::NewLine).Trim()
        if (-not $joined) {
            throw "Renderer smoke produced no output."
        }
        $result = $joined | ConvertFrom-Json
        $result | Add-Member -NotePropertyName nodeExitCode -NotePropertyValue $exitCode -Force
        if (-not $result.ok) {
            throw "Renderer did not become nonblank on port ${DebugPort}: $joined"
        }
        return $result
    }
    finally {
        Remove-Item -LiteralPath $tempScript -Force -ErrorAction SilentlyContinue
    }
}

function Get-HttpStatus {
    param(
        [Parameter(Mandatory = $true)][string]$Url,
        [string]$Method = "GET",
        [string]$Body = "",
        [hashtable]$Headers = @{}
    )

    try {
        $params = @{
            UseBasicParsing = $true
            Uri = $Url
            Method = $Method
            TimeoutSec = 8
            Headers = $Headers
        }
        if ($Body) {
            $params.Body = $Body
            $params.ContentType = "application/json"
        }
        $response = Invoke-WebRequest @params
        return [int]$response.StatusCode
    }
    catch {
        if ($_.Exception.Response -and $_.Exception.Response.StatusCode) {
            return [int]$_.Exception.Response.StatusCode
        }
        $curlArgs = @(
            "--silent",
            "--output",
            "NUL",
            "--write-out",
            "%{http_code}",
            "--max-time",
            "8",
            "--request",
            $Method
        )
        foreach ($key in $Headers.Keys) {
            $curlArgs += @("--header", "${key}: $($Headers[$key])")
        }
        if ($Body) {
            $curlArgs += @("--header", "Content-Type: application/json", "--data", $Body)
        }
        $curlArgs += $Url
        $statusText = (& curl.exe @curlArgs 2>$null)
        if ($LASTEXITCODE -eq 0 -and $statusText -match "^\d{3}$") {
            return [int]$statusText
        }
        throw
    }
}

function Write-JsonResult {
    param(
        [Parameter(Mandatory = $true)]$Value,
        [string]$Path = ""
    )

    $json = $Value | ConvertTo-Json -Depth 6
    if ($Path) {
        $resolved = [System.IO.Path]::GetFullPath($Path)
        $parent = Split-Path -Parent $resolved
        if ($parent) {
            New-Item -ItemType Directory -Force -Path $parent | Out-Null
        }
        $encoding = New-Object System.Text.UTF8Encoding($false)
        [System.IO.File]::WriteAllText($resolved, $json + [Environment]::NewLine, $encoding)
    }
    $json
}

$installerResolved = Resolve-Path -LiteralPath $InstallerPath -ErrorAction Stop
$installerItem = Get-Item -LiteralPath $installerResolved
if (-not $ExpectedVersion) {
    if ($installerItem.Name -match '^EcoreX_(?<version>\d+\.\d+\.\d+)_') {
        $ExpectedVersion = $Matches.version
    }
    else {
        $packageJsonPath = Join-Path $PSScriptRoot "..\package.json"
        if (Test-Path -LiteralPath $packageJsonPath) {
            $packageJson = Get-Content -Raw -Encoding UTF8 -LiteralPath $packageJsonPath | ConvertFrom-Json
            $ExpectedVersion = [string]$packageJson.version
        }
    }
    if (-not $ExpectedVersion) {
        throw "ExpectedVersion was not provided and could not be inferred from installer name or package.json."
    }
}
$installerSha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $installerResolved).Hash.ToUpperInvariant()
$installerSignature = Get-AuthenticodeSignature -LiteralPath $installerResolved
if ($installerSignature.Status -ne "Valid") {
    throw "Installer signature is not valid: $($installerSignature.StatusMessage)"
}
if (-not $ExpectedWinArch) {
    $ExpectedWinArch = if ($installerItem.Name -match "_ia32-setup\.exe$") { "ia32" } else { "x64" }
}
$expectedPythonBits = if ($ExpectedWinArch -eq "ia32") { 32 } else { 64 }

if (-not $InstallDir) {
    $InstallDir = Join-Path ([System.IO.Path]::GetTempPath()) ("EcoreX-smoke-" + [System.Guid]::NewGuid().ToString("N"))
}
$installResolved = Resolve-FullPath -Path $InstallDir
$tempRoot = Resolve-FullPath -Path ([System.IO.Path]::GetTempPath())
$isSafeTempInstall = $installResolved.StartsWith($tempRoot, [System.StringComparison]::OrdinalIgnoreCase) -and
    ((Split-Path -Leaf $installResolved) -like "EcoreX-smoke-*")

if (Test-Path -LiteralPath $installResolved) {
    throw "InstallDir already exists: $installResolved"
}

$result = [ordered]@{
    installer = [string]$installerResolved
    installerFileName = $installerItem.Name
    installerSize = [int64]$installerItem.Length
    installerSha256 = $installerSha256
    installDir = $installResolved
    webPort = $Port
    rendererDebugPort = $RendererDebugPort
    expectedVersion = $ExpectedVersion
    installerSignatureStatus = [string]$installerSignature.Status
    expectedWinArch = $ExpectedWinArch
    expectedPythonBits = $expectedPythonBits
    installed = $false
    appStarted = $false
    rendererReady = $false
    sidecarReady = $false
    authReady = $false
    authRequired = $false
    authNegativeReady = $false
    cleaned = $false
}

try {
    $installArgs = @("/S", "/D=$installResolved")
    $install = Start-Process -FilePath $installerResolved -ArgumentList $installArgs -Wait -PassThru -WindowStyle Hidden
    if ($install.ExitCode -ne 0) {
        throw "Installer failed with exit code $($install.ExitCode)"
    }
    $result.installed = $true

    $appExe = Join-Path $installResolved "EcoreX.exe"
    $runtimePython = Join-Path $installResolved "resources\ecorex-runtime\python\python.exe"
    $runtimeApp = Join-Path $installResolved "resources\ecorex-runtime\app.py"
    $capabilityManifest = Join-Path $installResolved "resources\ecorex-runtime\capabilities.json"
    $runtimeManifestPath = Join-Path $installResolved "resources\ecorex-runtime\runtime-manifest.json"

    foreach ($path in @($appExe, $runtimePython, $runtimeApp, $capabilityManifest, $runtimeManifestPath)) {
        if (-not (Test-Path -LiteralPath $path)) {
            throw "Expected installed file not found: $path"
        }
    }
    $runtimeManifest = Get-Content -Raw -Encoding UTF8 -LiteralPath $runtimeManifestPath | ConvertFrom-Json
    $result.runtimeWinArch = [string]$runtimeManifest.winArch
    if ($result.runtimeWinArch -ne $ExpectedWinArch) {
        throw "Installed runtime winArch '$($result.runtimeWinArch)' does not match expected '$ExpectedWinArch'."
    }
    $pythonBitsText = (& $runtimePython -c "import struct; print(struct.calcsize('P') * 8)")
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to query runtime Python bitness."
    }
    $result.runtimePythonBits = [int]([string]$pythonBitsText).Trim()
    if ($result.runtimePythonBits -ne $expectedPythonBits) {
        throw "Installed runtime Python bitness '$($result.runtimePythonBits)' does not match expected '$expectedPythonBits'."
    }

    foreach ($signedPath in @($appExe, $runtimePython)) {
        $signature = Get-AuthenticodeSignature -LiteralPath $signedPath
        if ($signature.Status -ne "Valid") {
            throw "Installed binary signature is not valid: $signedPath - $($signature.StatusMessage)"
        }
        if ($signedPath -eq $appExe) {
            $result.appSignatureStatus = [string]$signature.Status
        }
        if ($signedPath -eq $runtimePython) {
            $result.runtimePythonSignatureStatus = [string]$signature.Status
        }
    }

    $previousPort = $env:ECOREX_WEB_PORT
    $previousWebPort = $env:WEB_PORT
    $previousSkip = $env:ECOREX_SKIP_SIDECAR
    $env:ECOREX_WEB_PORT = [string]$Port
    $env:WEB_PORT = [string]$Port
    Remove-Item Env:ECOREX_SKIP_SIDECAR -ErrorAction SilentlyContinue
    try {
        $appArgs = @("--remote-debugging-port=$RendererDebugPort")
        $app = Start-Process -FilePath $appExe -ArgumentList $appArgs -PassThru -WindowStyle Hidden
        $result.appStarted = $true

        $renderer = Wait-RendererDom -DebugPort $RendererDebugPort -TimeoutSeconds 75
        $result.rendererReady = $true
        $result.rendererUrl = [string]$renderer.url
        $result.rendererTitle = [string]$renderer.title
        $result.rendererRootHtmlLength = [int]$renderer.rootHtmlLength
        $result.rendererBodyTextLength = [int]$renderer.bodyTextLength
        $result.rendererBodyTextSample = [string]$renderer.bodyTextSample

        $version = Wait-JsonEndpoint -Url "http://127.0.0.1:$Port/api/version" -TimeoutSeconds 75
        if ($version.version -ne $ExpectedVersion) {
            throw "Unexpected runtime version: $($version.version)"
        }
        $result.sidecarReady = $true
        $result.runtimeVersion = $version.version

        $auth = Wait-JsonEndpoint -Url "http://127.0.0.1:$Port/auth/check" -TimeoutSeconds 15
        if ($auth.status -ne "success") {
            throw "Unexpected auth status: $($auth | ConvertTo-Json -Compress)"
        }
        if (-not $auth.auth_required -or $auth.authenticated) {
            throw "Installed runtime auth/check did not report token protection: $($auth | ConvertTo-Json -Compress)"
        }
        $result.authReady = $true
        $result.authStatus = $auth.status
        $result.authRequired = [bool]$auth.auth_required

        $wrongTokenHeader = @{ "X-EcoreX-Runtime-Token" = "wrong-token" }
        $negativeStatuses = [ordered]@{
            messageNoToken = Get-HttpStatus -Url "http://127.0.0.1:$Port/message" -Method "POST" -Body '{"message":"negative auth smoke","stream":true}'
            messageWrongToken = Get-HttpStatus -Url "http://127.0.0.1:$Port/message" -Method "POST" -Body '{"message":"negative auth smoke","stream":true}' -Headers $wrongTokenHeader
            messageQueryTokenRejected = Get-HttpStatus -Url "http://127.0.0.1:$Port/message?runtime_token=wrong-token" -Method "POST" -Body '{"message":"negative auth smoke","stream":true}'
            streamNoToken = Get-HttpStatus -Url "http://127.0.0.1:$Port/stream?request_id=negative-auth-smoke"
            streamWrongToken = Get-HttpStatus -Url "http://127.0.0.1:$Port/stream?request_id=negative-auth-smoke" -Headers $wrongTokenHeader
            streamQueryTokenRejected = Get-HttpStatus -Url "http://127.0.0.1:$Port/stream?request_id=negative-auth-smoke&runtime_token=wrong-token"
            fileStatNoToken = Get-HttpStatus -Url "http://127.0.0.1:$Port/api/file-stat" -Method "POST" -Body '{"path":"C:\\Windows\\win.ini"}'
            fileStatWrongToken = Get-HttpStatus -Url "http://127.0.0.1:$Port/api/file-stat" -Method "POST" -Body '{"path":"C:\\Windows\\win.ini"}' -Headers $wrongTokenHeader
            fileServeNoToken = Get-HttpStatus -Url "http://127.0.0.1:$Port/api/file?path=C%3A%5CWindows%5Cwin.ini"
            fileServeWrongToken = Get-HttpStatus -Url "http://127.0.0.1:$Port/api/file?path=C%3A%5CWindows%5Cwin.ini" -Headers $wrongTokenHeader
            openPathNoToken = Get-HttpStatus -Url "http://127.0.0.1:$Port/api/open-path" -Method "POST" -Body '{"path":"C:\\Windows\\win.ini","action":"open"}'
            openPathWrongToken = Get-HttpStatus -Url "http://127.0.0.1:$Port/api/open-path" -Method "POST" -Body '{"path":"C:\\Windows\\win.ini","action":"open"}' -Headers $wrongTokenHeader
        }
        $failedNegative = @($negativeStatuses.GetEnumerator() | Where-Object { [int]$_.Value -ne 401 })
        if ($failedNegative.Count -gt 0) {
            throw "Protected installed runtime endpoints did not reject missing/wrong runtime token: $($failedNegative | ConvertTo-Json -Compress)"
        }
        $result.authNegativeReady = $true
        $result.authNegativeStatuses = $negativeStatuses
    }
    finally {
        if ($app -and -not $app.HasExited) {
            Stop-Process -Id $app.Id -Force -ErrorAction SilentlyContinue
        }
        Stop-ProcessesFromInstallDir -Dir $installResolved
        $env:ECOREX_WEB_PORT = $previousPort
        $env:WEB_PORT = $previousWebPort
        if ($null -eq $previousSkip) {
            Remove-Item Env:ECOREX_SKIP_SIDECAR -ErrorAction SilentlyContinue
        }
        else {
            $env:ECOREX_SKIP_SIDECAR = $previousSkip
        }
    }
}
finally {
    if (-not $KeepInstall -and (Test-Path -LiteralPath $installResolved)) {
        Stop-ProcessesFromInstallDir -Dir $installResolved
        $uninstaller = Join-Path $installResolved "Uninstall EcoreX.exe"
        if (Test-Path -LiteralPath $uninstaller) {
            $uninstall = Start-Process -FilePath $uninstaller -ArgumentList "/S" -Wait -PassThru -WindowStyle Hidden
            if ($uninstall.ExitCode -ne 0) {
                Write-Warning "Uninstaller returned exit code $($uninstall.ExitCode)"
            }
        }
        if ($isSafeTempInstall -and (Test-Path -LiteralPath $installResolved)) {
            Remove-Item -LiteralPath $installResolved -Recurse -Force -ErrorAction SilentlyContinue
        }
        $result.cleaned = -not (Test-Path -LiteralPath $installResolved)
    }
}

Write-JsonResult -Value $result -Path $OutputPath
