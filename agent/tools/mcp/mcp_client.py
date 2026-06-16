"""
MCP (Model Context Protocol) client module.

Implements JSON-RPC 2.0 over stdio, SSE and Streamable HTTP transports
without any external MCP SDK dependency.
"""

import json
import os
import queue
import re
import signal
import subprocess
import threading
import time
import urllib.request
import urllib.error
from typing import Optional

from common.log import logger


# Aliases accepted for the Streamable HTTP transport type
_STREAMABLE_HTTP_ALIASES = {"streamable-http", "streamable_http", "streamablehttp", "http"}
_DEFAULT_CDP_ENDPOINT = "http://127.0.0.1:9222"


_SENSITIVE_RE = re.compile(
    r"(?i)(api[_-]?key|token|password|secret|authorization)(\"?\s*[:=]\s*\"?)[^\",\s&}]+"
)


def _mask_sensitive(text: str) -> str:
    value = text or ""
    value = re.sub(r"sk-[A-Za-z0-9_\-]{12,}", "sk-***", value)
    value = re.sub(r"gh[pousr]_[A-Za-z0-9_]{12,}", "ghp_***", value)
    return _SENSITIVE_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}***", value)


def _mcp_permission_tool_name(server_name: str) -> str:
    return "browser" if (server_name or "").strip() == "chrome-devtools" else "mcp_server"


def _normalize_timeout(value, default: int = 120) -> int:
    try:
        timeout = int(value)
    except (TypeError, ValueError):
        timeout = default
    return max(1, min(timeout, 600))


def _is_default_chrome_devtools_config(server_name: str, command, args) -> bool:
    if (server_name or "").strip() != "chrome-devtools":
        return False
    command_name = os.path.basename(str(command or "").strip()).lower()
    if command_name not in {"npx", "npx.cmd"}:
        return False
    if not isinstance(args, list):
        return False
    parts = [str(item).strip() for item in args]
    return parts == [
        "chrome-devtools-mcp@latest",
        "--browserUrl",
        _DEFAULT_CDP_ENDPOINT,
        "--no-usage-statistics",
    ]


class McpClient:
    """Single MCP Server client supporting stdio, SSE and Streamable HTTP transports."""

    def __init__(self, config: dict):
        """
        config examples:
          stdio:           {"name": "filesystem", "type": "stdio", "command": "npx", "args": [...]}
          SSE:             {"name": "my-api",    "type": "sse",   "url": "http://localhost:8000/sse"}
          streamable-http: {"name": "pubmed",    "type": "streamable-http", "url": "https://x/mcp"}
        """
        self.config = config
        self.name: str = config.get("name", "unknown")
        raw_transport: str = config.get("type", "stdio")
        # Per-server timeout for tool calls (default 120s, suitable for data queries)
        self._timeout: int = _normalize_timeout(config.get("timeout", 120))
        # Normalize streamable-http aliases to a single internal key
        self.transport: str = (
            "streamable-http"
            if raw_transport.lower() in _STREAMABLE_HTTP_ALIASES
            else raw_transport
        )

        # stdio state
        self._proc: Optional[subprocess.Popen] = None
        self._read_queue: queue.Queue = queue.Queue()

        # SSE state
        self._sse_url: Optional[str] = None
        self._post_url: Optional[str] = None  # endpoint for sending messages (resolved from SSE)

        # Streamable HTTP state
        self._http_url: Optional[str] = None
        self._http_headers: dict = {}  # extra headers from user config (e.g. Authorization)
        self._http_session_id: Optional[str] = None  # Mcp-Session-Id assigned by the server

        # Shared state
        self._next_id = 1
        self._id_lock = threading.Lock()
        # _call_lock serializes all requests on the single stdio pipe.
        # SSE and streamable-http use independent HTTP requests, so they
        # do not acquire this lock (see _send_request).
        self._call_lock = threading.Lock()
        # _http_lock protects _http_session_id initialization across
        # concurrent streamable-http requests.
        self._http_lock = threading.Lock()
        self._initialized = False

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def initialize(self) -> bool:
        """Connect and perform the MCP handshake. Returns True on success."""
        try:
            if self.transport == "stdio":
                return self._init_stdio()
            elif self.transport == "sse":
                return self._init_sse()
            elif self.transport == "streamable-http":
                return self._init_streamable_http()
            else:
                logger.warning(f"[MCP:{self.name}] Unknown transport type: {self.transport!r}")
                return False
        except Exception as e:
            logger.warning(f"[MCP:{self.name}] Initialization failed: {e}")
            return False

    def list_tools(self) -> list:
        """Return the tool list from this server.

        Each item is a dict: {"name": str, "description": str, "inputSchema": dict}
        """
        resp = self._send_request("tools/list", {})
        self._raise_for_rpc_error(resp)
        tools = resp.get("result", {}).get("tools", [])
        return [
            {
                "name": t.get("name", ""),
                "description": t.get("description", ""),
                "inputSchema": t.get("inputSchema", {}),
            }
            for t in tools
        ]

    def call_tool(self, name: str, arguments: dict, cancel_event=None) -> str:
        """Call a tool and return the result as a string."""
        resp = self._send_request(
            "tools/call",
            {"name": name, "arguments": arguments},
            cancel_event=cancel_event,
        )
        self._raise_for_rpc_error(resp)

        result = resp.get("result") or {}
        content = result.get("content", [])
        parts = [item.get("text", "") for item in content if item.get("type") == "text"]
        text = "\n".join(parts)
        if result.get("isError"):
            raise RuntimeError(text or f"[MCP:{self.name}] tool returned isError=true")
        return text

    def shutdown(self):
        """Close the connection / terminate the child process."""
        if self._proc is not None:
            proc = self._proc
            try:
                proc.stdin.close()
            except Exception:
                pass
            try:
                if os.name == "nt":
                    subprocess.run(
                        ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=5,
                    )
                else:
                    os.killpg(proc.pid, signal.SIGTERM)
                proc.wait(timeout=5)
            except Exception:
                try:
                    if os.name == "nt":
                        subprocess.run(
                            ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            timeout=5,
                        )
                    else:
                        os.killpg(proc.pid, signal.SIGKILL)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass
            self._proc = None
            logger.debug(f"[MCP:{self.name}] stdio process terminated")

        # Best-effort streamable-http session termination
        if self.transport == "streamable-http" and self._http_session_id and self._http_url:
            try:
                req = urllib.request.Request(
                    self._http_url,
                    method="DELETE",
                    headers={"Mcp-Session-Id": self._http_session_id, **self._http_headers},
                )
                with urllib.request.urlopen(req, timeout=5):
                    pass
            except Exception:
                pass
            self._http_session_id = None

        self._initialized = False

    # ------------------------------------------------------------------
    # stdio transport
    # ------------------------------------------------------------------

    def _init_stdio(self) -> bool:
        command = self.config.get("command")
        if not command:
            logger.warning(f"[MCP:{self.name}] stdio config missing 'command'")
            return False

        args = self.config.get("args", [])
        if not self._authorize_stdio_start(command, args):
            return False
        extra_env = self.config.get("env", None)
        env = {**os.environ, **extra_env} if extra_env else None

        popen_kwargs = {}
        if os.name == "nt":
            popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        else:
            popen_kwargs["start_new_session"] = True

        self._proc = subprocess.Popen(
            [command] + list(args),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            env=env,
            **popen_kwargs,
        )
        logger.debug(f"[MCP:{self.name}] stdio process started (pid={self._proc.pid})")

        threading.Thread(
            target=self._drain_stderr, daemon=True, name=f"mcp-stderr-{self.name}"
        ).start()
        threading.Thread(
            target=self._drain_stdout, daemon=True, name=f"mcp-stdout-{self.name}"
        ).start()

        return self._handshake()

    def _drain_stderr(self):
        for line in self._proc.stderr:
            line = line.strip()
            if line:
                logger.warning(f"[MCP:{self.name}] stderr: {_mask_sensitive(line)}")

    def _authorize_stdio_start(self, command, args) -> bool:
        try:
            from common.ecorex_tool_permissions import get_tool_permission_broker

            decision = get_tool_permission_broker().authorize_noninteractive(
                _mcp_permission_tool_name(self.name),
                {
                    "server": self.name,
                    "command": str(command),
                    "args": [str(item) for item in list(args or [])[:12]],
                    "trusted_default_chrome_devtools": _is_default_chrome_devtools_config(
                        self.name,
                        command,
                        args,
                    ),
                },
            )
            if decision.get("allowed", True):
                return True
            logger.warning(
                f"[MCP:{self.name}] stdio startup blocked by permission boundary: "
                f"{decision.get('reason', 'not allowed')}"
            )
            return False
        except Exception as exc:
            logger.warning(f"[MCP:{self.name}] permission check failed; startup blocked: {exc}")
            return False

    def _drain_stdout(self):
        """Background thread: read lines from stdout and put them into the queue."""
        try:
            for line in self._proc.stdout:
                self._read_queue.put(line)
        except Exception:
            pass
        finally:
            try:
                self._read_queue.put("")
            except Exception:
                pass

    def _readline_with_timeout(self, timeout: Optional[int] = None) -> str:
        """Read one line from stdio stdout with a hard timeout (cross-platform).

        Uses the per-server timeout from mcp.json config when no explicit
        timeout is provided.
        """
        effective = timeout if timeout is not None else self._timeout
        try:
            line = self._read_queue.get(timeout=effective)
        except queue.Empty:
            raise TimeoutError(f"[MCP:{self.name}] stdio read timed out after {effective}s")
        if not line:
            raise IOError(f"[MCP:{self.name}] stdio process closed unexpectedly")
        return line

    def _stdio_send(self, message: dict, cancel_event=None) -> dict:
        """Send a JSON-RPC message over stdio and read the response."""
        raw = json.dumps(message) + "\n"
        self._proc.stdin.write(raw)
        self._proc.stdin.flush()

        expected_id = message.get("id")
        deadline = time.time() + max(1, self._timeout)
        while True:
            self._raise_if_cancelled(cancel_event)
            try:
                line = self._read_queue.get(timeout=0.25)
            except queue.Empty:
                if time.time() >= deadline:
                    self.shutdown()
                    raise TimeoutError(f"[MCP:{self.name}] stdio read timed out after {self._timeout}s")
                continue
            if not line:
                raise IOError(f"[MCP:{self.name}] stdio process closed unexpectedly")
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            if "id" not in data:
                logger.debug(f"[MCP:{self.name}] notification skipped: {data.get('method', '?')}")
                continue
            # Verify response id matches request id to avoid consuming a stale
            # response left over from a previously failed/timed-out request.
            if data.get("id") != expected_id:
                logger.warning(
                    f"[MCP:{self.name}] Stale response id={data.get('id')} "
                    f"(expected {expected_id}), skipping"
                )
                continue
            return data

    # ------------------------------------------------------------------
    # SSE transport
    # ------------------------------------------------------------------

    def _init_sse(self) -> bool:
        url = self.config.get("url")
        if not url:
            logger.warning(f"[MCP:{self.name}] SSE config missing 'url'")
            return False
        if not self._authorize_remote_start(url):
            return False

        self._sse_url = url

        # Read the first SSE event to discover the POST endpoint
        try:
            self._post_url = self._sse_discover_endpoint()
        except Exception as e:
            logger.warning(f"[MCP:{self.name}] SSE endpoint discovery failed: {e}")
            return False

        return self._handshake()

    def _sse_discover_endpoint(self) -> str:
        """Open SSE stream and read the 'endpoint' event to learn the POST URL."""
        req = urllib.request.Request(
            self._sse_url,
            headers={"Accept": "text/event-stream"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            for raw_line in resp:
                line = raw_line.decode("utf-8").rstrip("\n\r")
                if line.startswith("data:"):
                    data = line[len("data:"):].strip()
                    # Some servers send JSON with a "uri" or plain path
                    if data.startswith("{"):
                        parsed = json.loads(data)
                        return parsed.get("uri") or parsed.get("url") or parsed.get("endpoint")
                    # Plain relative or absolute URL
                    if data.startswith("http"):
                        return data
                    # Relative path: resolve against SSE base
                    from urllib.parse import urljoin
                    return urljoin(self._sse_url, data)
        raise ValueError(f"[MCP:{self.name}] No endpoint event received from SSE stream")

    def _sse_send(self, message: dict, cancel_event=None) -> dict:
        """POST a JSON-RPC message to the server and return the response."""
        self._raise_if_cancelled(cancel_event)
        body = json.dumps(message).encode("utf-8")
        req = urllib.request.Request(
            self._post_url,
            data=body,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            self._raise_if_cancelled(cancel_event)
            raw = resp.read().decode("utf-8")
            self._raise_if_cancelled(cancel_event)
            return json.loads(raw)

    # ------------------------------------------------------------------
    # Streamable HTTP transport (MCP spec 2025-03-26)
    # ------------------------------------------------------------------

    def _init_streamable_http(self) -> bool:
        url = self.config.get("url")
        if not url:
            logger.warning(f"[MCP:{self.name}] streamable-http config missing 'url'")
            return False
        if not self._authorize_remote_start(url):
            return False

        self._http_url = url
        # Allow user-provided headers (e.g. {"Authorization": "Bearer xxx"})
        extra_headers = self.config.get("headers") or {}
        if isinstance(extra_headers, dict):
            self._http_headers = {str(k): str(v) for k, v in extra_headers.items()}

        return self._handshake()

    def _authorize_remote_start(self, url) -> bool:
        try:
            from common.ecorex_tool_permissions import get_tool_permission_broker

            decision = get_tool_permission_broker().authorize_noninteractive(
                _mcp_permission_tool_name(self.name),
                {
                    "server": self.name,
                    "url": str(url),
                },
            )
            if decision.get("allowed", True):
                return True
            logger.warning(
                f"[MCP:{self.name}] remote startup blocked by permission boundary: "
                f"{decision.get('reason', 'not allowed')}"
            )
            return False
        except Exception as exc:
            logger.warning(f"[MCP:{self.name}] permission check failed; remote startup blocked: {exc}")
            return False

    def _streamable_http_send(self, message: dict, cancel_event=None) -> dict:
        """POST a JSON-RPC request and return the response (JSON or SSE-wrapped)."""
        return self._streamable_http_post(message, expect_response=True, cancel_event=cancel_event)

    def _streamable_http_post(self, message: dict, expect_response: bool, cancel_event=None) -> dict:
        """
        POST a JSON-RPC message over Streamable HTTP.

        Per the spec, the response Content-Type can be either:
          - application/json   -> single JSON-RPC response in body
          - text/event-stream  -> SSE stream; we read until we get a matching response
        """
        self._raise_if_cancelled(cancel_event)
        body = json.dumps(message).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        # Read session id under lock to avoid racing with the
        # initialization write below during concurrent requests.
        with self._http_lock:
            sid = self._http_session_id
        if sid:
            headers["Mcp-Session-Id"] = sid
        headers.update(self._http_headers)

        req = urllib.request.Request(
            self._http_url,
            data=body,
            method="POST",
            headers=headers,
        )

        try:
            resp = urllib.request.urlopen(req, timeout=30)
        except urllib.error.HTTPError as e:
            # Surface the server-provided error body for easier debugging
            detail = ""
            try:
                detail = e.read().decode("utf-8", errors="ignore")
            except Exception:
                pass
            raise IOError(
                f"[MCP:{self.name}] streamable-http HTTP {e.code}: {detail[:200]}"
            )

        with resp:
            self._raise_if_cancelled(cancel_event)
            # Capture session id assigned by the server (if any)
            session_id = resp.headers.get("Mcp-Session-Id")
            # Double-checked lock: only the first response sets the
            # session id, preventing concurrent initializers from
            # overwriting each other.
            if session_id and not self._http_session_id:
                with self._http_lock:
                    if not self._http_session_id:
                        self._http_session_id = session_id

            status = resp.status if hasattr(resp, "status") else resp.getcode()

            # Notifications: server may reply with 202 Accepted and no body
            if not expect_response or status == 202:
                try:
                    resp.read()
                except Exception:
                    pass
                return {}

            content_type = (resp.headers.get("Content-Type") or "").lower()
            expected_id = message.get("id")

            if "text/event-stream" in content_type:
                return self._read_sse_response(
                    resp,
                    expected_id,
                    cancel_event=cancel_event,
                    timeout_seconds=self._timeout,
                )

            raw = resp.read().decode("utf-8")
            self._raise_if_cancelled(cancel_event)
            if not raw:
                return {}
            return json.loads(raw)

    def _read_sse_response(self, resp, expected_id, cancel_event=None, timeout_seconds=None) -> dict:
        """Read an SSE stream and return the first JSON-RPC response with matching id."""
        data_buf: list = []
        deadline = time.time() + max(1, _normalize_timeout(timeout_seconds, self._timeout))
        for raw_line in resp:
            self._raise_if_cancelled(cancel_event)
            if time.time() >= deadline:
                raise TimeoutError(f"[MCP:{self.name}] streamable-http SSE timed out after {self._timeout}s")
            line = raw_line.decode("utf-8").rstrip("\n\r")
            if line == "":
                # End of an SSE event, attempt to parse accumulated data
                if data_buf:
                    payload = "\n".join(data_buf)
                    data_buf = []
                    try:
                        msg = json.loads(payload)
                    except json.JSONDecodeError:
                        continue
                    # Skip notifications / mismatched ids
                    if "id" not in msg:
                        continue
                    if expected_id is None or msg.get("id") == expected_id:
                        return msg
                continue
            if line.startswith(":"):
                continue  # SSE comment / keepalive
            if line.startswith("data:"):
                data_buf.append(line[len("data:"):].lstrip())
            # Ignore 'event:' / 'id:' lines; we only care about JSON-RPC payloads

        raise IOError(f"[MCP:{self.name}] streamable-http SSE stream closed before response")

    # ------------------------------------------------------------------
    # Common JSON-RPC helpers
    # ------------------------------------------------------------------

    def _next_request_id(self) -> int:
        with self._id_lock:
            rid = self._next_id
            self._next_id += 1
        return rid

    def _build_request(self, method: str, params: dict) -> dict:
        return {
            "jsonrpc": "2.0",
            "id": self._next_request_id(),
            "method": method,
            "params": params,
        }

    @staticmethod
    def _format_rpc_error(error) -> str:
        if isinstance(error, dict):
            code = error.get("code")
            message = error.get("message", "Unknown MCP error")
            data = error.get("data")
            if data is not None:
                return f"MCP error {code}: {message}; data={_mask_sensitive(str(data))[:300]}"
            return f"MCP error {code}: {message}"
        return f"MCP error: {error}"

    def _raise_for_rpc_error(self, resp: dict) -> None:
        if isinstance(resp, dict) and "error" in resp:
            raise RuntimeError(self._format_rpc_error(resp["error"]))

    def _raise_if_cancelled(self, cancel_event) -> None:
        if cancel_event is not None and getattr(cancel_event, "is_set", lambda: False)():
            try:
                self.shutdown()
            finally:
                raise RuntimeError(f"[MCP:{self.name}] tool call cancelled by user")

    def _build_notification(self, method: str, params: dict) -> dict:
        return {"jsonrpc": "2.0", "method": method, "params": params}

    def _send_request(self, method: str, params: dict, cancel_event=None) -> dict:
        """Send a request and return the full response dict."""
        if not self._initialized and method != "initialize":
            raise RuntimeError(f"[MCP:{self.name}] Client not initialized")

        self._raise_if_cancelled(cancel_event)
        message = self._build_request(method, params)

        # stdio transport uses a single pipe and must be serialized.
        # SSE and streamable-http use independent HTTP requests and
        # can safely run concurrently across sessions.
        if self.transport == "stdio":
            with self._call_lock:
                return self._stdio_send(message, cancel_event=cancel_event)
        elif self.transport == "sse":
            return self._sse_send(message, cancel_event=cancel_event)
        elif self.transport == "streamable-http":
            return self._streamable_http_send(message, cancel_event=cancel_event)
        else:
            raise ValueError(f"[MCP:{self.name}] Unsupported transport: {self.transport}")

    def _send_notification(self, method: str, params: dict):
        """Fire-and-forget notification (no response expected)."""
        notification = self._build_notification(method, params)
        raw = json.dumps(notification) + "\n"

        if self.transport == "stdio":
            self._proc.stdin.write(raw)
            self._proc.stdin.flush()
        elif self.transport == "sse":
            body = raw.encode("utf-8")
            req = urllib.request.Request(
                self._post_url,
                data=body,
                method="POST",
                headers={"Content-Type": "application/json"},
            )
            try:
                with urllib.request.urlopen(req, timeout=10):
                    pass
            except Exception:
                pass  # notifications are fire-and-forget
        elif self.transport == "streamable-http":
            try:
                self._streamable_http_post(notification, expect_response=False)
            except Exception:
                pass  # notifications are fire-and-forget

    def _handshake(self) -> bool:
        """Perform the MCP initialize / notifications/initialized handshake."""
        init_params = {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "CowAgent", "version": "1.0"},
        }
        # Temporarily mark as initialized so _send_request doesn't block
        self._initialized = True
        try:
            resp = self._send_request("initialize", init_params)
        except Exception as e:
            self._initialized = False
            logger.warning(f"[MCP:{self.name}] Handshake initialize failed: {e}")
            return False

        if "error" in resp:
            self._initialized = False
            logger.warning(f"[MCP:{self.name}] Handshake error: {resp['error']}")
            return False

        self._send_notification("notifications/initialized", {})
        logger.debug(f"[MCP:{self.name}] Handshake complete")
        return True


class McpClientRegistry:
    """Global singleton managing the lifecycle of all MCP Server clients."""

    _instance = None
    _instance_lock = threading.Lock()

    def __new__(cls):
        with cls._instance_lock:
            if cls._instance is None:
                obj = super().__new__(cls)
                obj._clients: dict[str, McpClient] = {}
                obj._registry_lock = threading.Lock()
                cls._instance = obj
        return cls._instance

    def start_all(self, configs: list) -> None:
        """Initialize McpClient for each config entry; skip failures with a warning."""
        if not configs:
            return

        for cfg in configs:
            name = cfg.get("name", "<unnamed>")
            client = McpClient(cfg)
            ok = client.initialize()
            if ok:
                with self._registry_lock:
                    self._clients[name] = client
                logger.info(f"[MCP] Server '{name}' initialized successfully")
            else:
                logger.warning(f"[MCP] Server '{name}' failed to initialize — skipping")

    def get(self, server_name: str) -> Optional[McpClient]:
        """Return the initialized client for server_name, or None."""
        with self._registry_lock:
            return self._clients.get(server_name)

    def all_clients(self) -> dict:
        """Return a copy of the {name: McpClient} mapping."""
        with self._registry_lock:
            return dict(self._clients)

    def shutdown_all(self) -> None:
        """Shut down all managed clients."""
        with self._registry_lock:
            clients = list(self._clients.values())
            self._clients.clear()

        for client in clients:
            try:
                client.shutdown()
            except Exception as e:
                logger.warning(f"[MCP] Error shutting down '{client.name}': {e}")

        logger.info("[MCP] All servers shut down")
