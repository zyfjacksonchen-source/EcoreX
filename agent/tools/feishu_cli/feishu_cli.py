"""Feishu/Lark CLI tool wrapper for EcoreX runtimes.

This tool keeps Feishu access out of ad-hoc shell commands. It resolves an
already available `lark-cli`, reports auth state, starts split-flow user auth,
and runs CLI commands with bounded timeouts. It only installs the official CLI
after a structured find-skill discovery gate requests that on-demand setup.
"""

from __future__ import annotations

import json
import hashlib
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from agent.tools.base_tool import BaseTool, ToolResult
from common.log import logger


DEFAULT_LARK_CLI_PACKAGE = os.environ.get("ECOREX_LARK_CLI_PACKAGE", "@larksuite/cli@1.0.56")
FEISHU_LARK_SOURCE_URL = "https://github.com/larksuite/cli"
FEISHU_LARK_MIRROR_URLS = ["https://registry.npmmirror.com/@larksuite/cli"]
DEFAULT_NPM_REGISTRY = "https://registry.npmjs.org"
DOMESTIC_NPM_REGISTRY = "https://registry.npmmirror.com"
DEFAULT_TIMEOUT_SECONDS = 45
DEFAULT_AUTH_URL_WAIT_SECONDS = 20
AUTH_SESSION_RETENTION_SECONDS = 15 * 60
AUTH_SESSION_OUTPUT_LIMIT = 12000

_AUTH_SESSIONS: Dict[str, Dict[str, Any]] = {}
_AUTH_SESSIONS_LOCK = threading.Lock()


_SECRET_PATTERNS = [
    re.compile(r"(?i)(access[_-]?token|refresh[_-]?token|app[_-]?secret|tenant[_-]?token|authorization)(\"?\s*[:=]\s*\")([^\"\s,]+)"),
    re.compile(r"(?i)(token|secret|password|api[_-]?key)(=|:)\s*[^\s,&}\"]+"),
]
_SENSITIVE_JSON_KEY_RE = re.compile(
    r"(?i)(access[_-]?token|refresh[_-]?token|tenant[_-]?token|token|secret|password|api[_-]?key|authorization|cookie)"
)


class _ProcessCancelled(Exception):
    def __init__(self, stdout: str = "", stderr: str = ""):
        super().__init__("process cancelled by user")
        self.stdout = stdout or ""
        self.stderr = stderr or ""


def _sanitize(text: str) -> str:
    value = text or ""
    value = re.sub(
        r"(?i)([\"']?(?:access[_-]?token|refresh[_-]?token|tenant[_-]?token|app[_-]?secret|auth[_-]?header|authorization|api[_-]?key|token|secret|password)[\"']?\s*[:=]\s*[\"']?)(?:bearer\s+)?[^\"'\r\n,}&]+",
        r"\1***",
        value,
    )
    for pattern in _SECRET_PATTERNS:
        if pattern.groups >= 3:
            value = pattern.sub(lambda m: f"{m.group(1)}{m.group(2)}***", value)
        else:
            value = pattern.sub(lambda m: f"{m.group(1)}{m.group(2)}***", value)
    value = re.sub(r"sk-[A-Za-z0-9_\-]{12,}", "sk-***", value)
    value = re.sub(r"gh[pousr]_[A-Za-z0-9_]{12,}", "ghp_***", value)
    return value


def _sanitize_json_payload(value: Any, depth: int = 0) -> Any:
    if depth > 8:
        return "[redacted-nested]"
    if isinstance(value, dict):
        safe: Dict[str, Any] = {}
        for key, child in value.items():
            key_text = str(key)
            safe[key_text] = "***" if _SENSITIVE_JSON_KEY_RE.search(key_text) else _sanitize_json_payload(child, depth + 1)
        return safe
    if isinstance(value, list):
        return [_sanitize_json_payload(item, depth + 1) for item in value[:200]]
    if isinstance(value, str):
        return _sanitize(value)
    return value


def _prepend_path(env: Dict[str, str], path: Optional[Path]) -> None:
    if not path or not path.exists():
        return
    current = env.get("PATH", "")
    raw = str(path)
    parts = current.split(os.pathsep) if current else []
    if raw not in parts:
        env["PATH"] = raw + (os.pathsep + current if current else "")


def _configured_install_root_from_conf() -> Optional[Path]:
    try:
        from config import conf

        tools = conf().get("tools", {})
        config = tools.get("feishu_cli") if isinstance(tools, dict) else None
        if isinstance(config, dict):
            value = config.get("install_root") or config.get("installRoot")
            if value:
                return Path(str(value)).expanduser()
    except Exception:
        return None
    return None


def _candidate_bin_dirs() -> List[Path]:
    home = Path.home()
    dirs: List[Path] = []
    install_root_override = os.environ.get("ECOREX_LARK_CLI_INSTALL_ROOT")
    if install_root_override:
        install_root = Path(install_root_override).expanduser()
        dirs.extend([
            install_root / "bin",
            install_root / "node_modules" / ".bin",
        ])
    configured_install_root = _configured_install_root_from_conf()
    if configured_install_root:
        dirs.extend([
            configured_install_root / "bin",
            configured_install_root / "node_modules" / ".bin",
        ])
    if os.name == "nt":
        for base in (os.environ.get("APPDATA"), os.environ.get("LOCALAPPDATA")):
            if base:
                dirs.append(Path(base) / "npm")
        for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)")):
            if base:
                dirs.append(Path(base) / "nodejs")
        dirs.append(Path("C:/cli-main/bin"))
        dirs.append(Path("C:/EcoreX Artifact Desk/cli-main/bin"))
        dirs.append(Path("C:/EcoreX Artifact Desk/cli-main"))
    else:
        dirs.extend([
            home / ".npm-global" / "bin",
            home / ".npm" / "bin",
            Path("/usr/local/bin"),
            Path("/opt/homebrew/bin"),
        ])

    runtime_root = Path(__file__).resolve().parents[3]
    dirs.extend([
        runtime_root / "bin",
        runtime_root / "tools" / "bin",
        runtime_root / "node" / "bin",
        runtime_root / "tools" / "lark-cli" / "bin",
        runtime_root / "tools" / "lark-cli" / "node_modules" / ".bin",
    ])
    return dirs


def _tool_env() -> Dict[str, str]:
    env = os.environ.copy()
    env.setdefault("PYTHONIOENCODING", "utf-8")
    for candidate in _candidate_bin_dirs():
        _prepend_path(env, candidate)
    return env


def _which(name: str, env: Optional[Dict[str, str]] = None) -> Optional[str]:
    search_path = (env or os.environ).get("PATH")
    found = shutil.which(name, path=search_path)
    if found:
        return found
    if os.name == "nt" and not name.lower().endswith(".cmd"):
        return shutil.which(f"{name}.cmd", path=search_path)
    return None


def _find_local_lark_runner(env: Dict[str, str]) -> Optional[List[str]]:
    node = _which("node", env)
    if not node:
        return None
    install_root_override = env.get("ECOREX_LARK_CLI_INSTALL_ROOT") or os.environ.get("ECOREX_LARK_CLI_INSTALL_ROOT")
    override_root = Path(install_root_override).expanduser() if install_root_override else None
    candidates = [
        *(([
            override_root / "scripts" / "run.js",
            override_root / "node_modules" / "@larksuite" / "cli" / "scripts" / "run.js",
        ]) if override_root else []),
        Path("C:/EcoreX Artifact Desk/cli-main/scripts/run.js"),
        Path(__file__).resolve().parents[3] / "tools" / "lark-cli" / "scripts" / "run.js",
        Path(__file__).resolve().parents[3] / "tools" / "lark-cli" / "node_modules" / "@larksuite" / "cli" / "scripts" / "run.js",
        Path(__file__).resolve().parents[3] / "node_modules" / "@larksuite" / "cli" / "scripts" / "run.js",
    ]
    for script in candidates:
        if script.exists():
            return [node, str(script)]
    return None


def _find_direct_lark_binary() -> Optional[str]:
    names = ["lark-cli.exe", "lark-cli.cmd", "lark-cli"] if os.name == "nt" else ["lark-cli"]
    for directory in _candidate_bin_dirs():
        for name in names:
            candidate = directory / name
            if candidate.exists():
                return str(candidate)
    return None


def _resolve_lark_command(env: Dict[str, str]) -> Optional[List[str]]:
    cli = _which("lark-cli", env)
    if cli:
        return [cli]
    direct = _find_direct_lark_binary()
    if direct:
        return [direct]
    return _find_local_lark_runner(env)


def _kill_process_tree(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
            )
            return
        except Exception as exc:
            logger.debug(f"[FeishuCli] taskkill failed for pid {process.pid}: {exc}")
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
            return
        except Exception as exc:
            logger.debug(f"[FeishuCli] killpg failed for pid {process.pid}: {exc}")
    try:
        process.kill()
    except Exception:
        pass


def _run_process(
    command: List[str],
    timeout: int,
    env: Dict[str, str],
    cwd: Optional[str] = None,
    cancel_event=None,
    input_text: Optional[str] = None,
) -> subprocess.CompletedProcess:
    kwargs: Dict[str, Any] = {
        "cwd": cwd or os.getcwd(),
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "env": env,
    }
    if input_text is not None:
        kwargs["stdin"] = subprocess.PIPE
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        kwargs["start_new_session"] = True
    process = subprocess.Popen(command, **kwargs)
    deadline = time.time() + max(1, timeout)
    pending_input = input_text
    while True:
        try:
            stdout, stderr = process.communicate(input=pending_input, timeout=0.25)
            break
        except subprocess.TimeoutExpired:
            pending_input = None
            if cancel_event is not None and getattr(cancel_event, "is_set", lambda: False)():
                _kill_process_tree(process)
                try:
                    stdout, stderr = process.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    try:
                        process.kill()
                    except Exception:
                        pass
                    stdout, stderr = process.communicate()
                raise _ProcessCancelled(stdout, stderr)
            if time.time() >= deadline:
                _kill_process_tree(process)
                try:
                    stdout, stderr = process.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    try:
                        process.kill()
                    except Exception:
                        pass
                    stdout, stderr = process.communicate()
                raise subprocess.TimeoutExpired(command, timeout, output=stdout, stderr=stderr)
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


def _trim_chunks(chunks: List[str], max_chars: int = 12000) -> None:
    text = "".join(chunks)
    if len(text) > max_chars:
        chunks[:] = [text[-max_chars:]]


def _cleanup_auth_sessions_locked(now: Optional[float] = None) -> None:
    current = now or time.time()
    stale: List[str] = []
    for session_id, session in list(_AUTH_SESSIONS.items()):
        process = session.get("process")
        deadline_at = float(session.get("deadlineAt") or session.get("startedAt") or current)
        retention_until = deadline_at + AUTH_SESSION_RETENTION_SECONDS
        running = bool(process is not None and getattr(process, "poll", lambda: 1)() is None)
        if not running and current > retention_until:
            stale.append(session_id)
    for session_id in stale:
        _AUTH_SESSIONS.pop(session_id, None)


def _register_auth_session(session: Dict[str, Any]) -> None:
    session_id = str(session.get("sessionId") or "").strip()
    if not session_id:
        return
    with _AUTH_SESSIONS_LOCK:
        _cleanup_auth_sessions_locked()
        _AUTH_SESSIONS[session_id] = session
    threading.Thread(
        target=_auth_session_watchdog,
        args=(session_id,),
        daemon=True,
        name="feishu-config-init-watchdog",
    ).start()


def _auth_session_watchdog(session_id: str) -> None:
    while True:
        process = None
        with _AUTH_SESSIONS_LOCK:
            session = _AUTH_SESSIONS.get(session_id)
            if not session:
                return
            process = session.get("process")
            if process is None or process.poll() is not None:
                return
            wait_seconds = float(session.get("deadlineAt") or time.time()) - time.time()
            if wait_seconds <= 0:
                session["timedOut"] = True
                break
        time.sleep(min(max(wait_seconds, 0.1), 30.0))
    if process is not None and process.poll() is None:
        _kill_process_tree(process)
    with _AUTH_SESSIONS_LOCK:
        session = _AUTH_SESSIONS.get(session_id)
        if session:
            session["exitCode"] = process.poll() if process is not None else session.get("exitCode")
            session["completedAt"] = time.time()


def _cancel_running_auth_sessions_for_workdir(workdir: Path) -> int:
    try:
        target = str(workdir.expanduser().resolve())
    except Exception:
        target = str(workdir)
    victims: List[subprocess.Popen] = []
    with _AUTH_SESSIONS_LOCK:
        for session in _AUTH_SESSIONS.values():
            process = session.get("process")
            session_cwd = str(session.get("cwd") or "")
            try:
                session_cwd = str(Path(session_cwd).expanduser().resolve())
            except Exception:
                pass
            if session_cwd == target and process is not None and process.poll() is None:
                session["superseded"] = True
                session["completedAt"] = time.time()
                victims.append(process)
    for process in victims:
        _kill_process_tree(process)
    return len(victims)


def _auth_status_is_ready(status_payload: Dict[str, Any]) -> bool:
    return bool(status_payload.get("authenticated")) or str(status_payload.get("authState") or "").strip().lower() == "ready"


def _auth_status_state(status_payload: Dict[str, Any]) -> str:
    return str(status_payload.get("authState") or ("ready" if status_payload.get("authenticated") else "unknown"))


def _auth_session_output(session: Dict[str, Any]) -> tuple[str, Any]:
    lock = session.get("lock")
    stdout_chunks = session.get("stdoutChunks")
    stderr_chunks = session.get("stderrChunks")
    if lock is not None and isinstance(stdout_chunks, list) and isinstance(stderr_chunks, list):
        with lock:
            stdout = "".join(stdout_chunks)[-AUTH_SESSION_OUTPUT_LIMIT:]
            stderr = "".join(stderr_chunks)[-AUTH_SESSION_OUTPUT_LIMIT:]
    else:
        stdout = ""
        stderr = ""
        stdout_path = Path(str(session.get("stdoutLogPath") or ""))
        stderr_path = Path(str(session.get("stderrLogPath") or ""))
        for path, target in ((stdout_path, "stdout"), (stderr_path, "stderr")):
            try:
                text = path.read_text(encoding="utf-8", errors="replace")[-AUTH_SESSION_OUTPUT_LIMIT:]
            except Exception:
                text = ""
            if target == "stdout":
                stdout = text
            else:
                stderr = text
    output = stdout + ("\n" + stderr if stderr else "")
    return (
        output[-AUTH_SESSION_OUTPUT_LIMIT:] if len(output) > AUTH_SESSION_OUTPUT_LIMIT else output,
        _sanitize_json_payload(_parse_json_output(stdout)),
    )


def _auth_session_snapshot(session_id: str, *, kill_expired: bool = True) -> Dict[str, Any]:
    clean_id = str(session_id or "").strip()
    if not clean_id:
        return {"status": "error", "message": "session_id is required", "writebackPending": False}
    with _AUTH_SESSIONS_LOCK:
        _cleanup_auth_sessions_locked()
        session = _AUTH_SESSIONS.get(clean_id)
    if not session:
        return {
            "status": "not_found",
            "sessionId": clean_id,
            "writebackPending": False,
            "backgroundProcess": False,
            "message": "Feishu CLI auth session was not found or already expired.",
        }

    process = session.get("process")
    now = time.time()
    deadline_at = float(session.get("deadlineAt") or now)
    exit_code = process.poll() if process is not None else session.get("exitCode")
    timed_out = bool(session.get("timedOut"))
    superseded = bool(session.get("superseded"))
    if exit_code is None and kill_expired and now >= deadline_at:
        timed_out = True
        try:
            _kill_process_tree(process)
        except Exception:
            pass
        exit_code = process.poll() if process is not None else None

    output, parsed = _auth_session_output(session)
    url = str(session.get("verificationUrl") or "").strip() or _auth_url_from_result({"output": output, "json": parsed})
    if superseded:
        status = "cancelled"
        writeback_pending = False
        background = False
    elif timed_out or session.get("timedOut"):
        status = "timeout"
        writeback_pending = False
        background = False
    elif exit_code is None:
        status = "auth_pending"
        writeback_pending = True
        background = True
    else:
        status = "success" if exit_code == 0 else "error"
        writeback_pending = False
        background = False

    lock = session.get("lock")
    if lock is not None:
        with lock:
            if url:
                session["verificationUrl"] = url
            session["exitCode"] = exit_code
            session["timedOut"] = bool(timed_out or session.get("timedOut"))
            if exit_code is not None and not session.get("completedAt"):
                session["completedAt"] = now

    payload: Dict[str, Any] = {
        "status": status,
        "sessionId": clean_id,
        "exitCode": exit_code,
        "pid": session.get("pid"),
        "authFlow": "config_init_status",
        "backgroundProcess": background,
        "writebackPending": writeback_pending,
        "cliWritebackTimeoutSeconds": int(session.get("timeoutSeconds") or DEFAULT_TIMEOUT_SECONDS),
        "startedAt": session.get("startedAt"),
        "deadlineAt": session.get("deadlineAt"),
        "output": output or "(no output)",
        "json": parsed,
        "stdoutLogPath": session.get("stdoutLogPath"),
        "stderrLogPath": session.get("stderrLogPath"),
    }
    if url:
        payload["verificationUrl"] = url
    if status == "auth_pending":
        payload["message"] = "Waiting for lark-cli to receive Feishu auth write-back."
    elif status == "success":
        payload["processCompleted"] = True
        payload["message"] = "lark-cli config process completed; verifying Feishu CLI auth status."
    elif status == "timeout":
        payload["message"] = "Feishu CLI auth write-back window expired; start authorization again."
    else:
        payload["message"] = "Feishu CLI auth/config process exited before write-back completed."
    return payload


def _run_process_until_auth_url(
    command: List[str],
    timeout: int,
    env: Dict[str, str],
    cwd: Optional[str] = None,
    cancel_event=None,
    url_wait_seconds: int = DEFAULT_AUTH_URL_WAIT_SECONDS,
) -> Dict[str, Any]:
    """Start a blocking auth/config command and return once its URL appears."""
    workdir = Path(cwd or os.getcwd()).expanduser()
    workdir.mkdir(parents=True, exist_ok=True)
    try:
        workdir = workdir.resolve()
    except Exception:
        pass
    log_dir = workdir / ".ecorex" / "lark-auth"
    log_dir.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256((" ".join(command) + str(time.time())).encode("utf-8")).hexdigest()[:16]
    session_id = f"lark-auth-{digest}"
    stdout_path = log_dir / f"lark-config-init-{digest}.out.log"
    stderr_path = log_dir / f"lark-config-init-{digest}.err.log"
    stdout_path.touch()
    stderr_path.touch()
    _cancel_running_auth_sessions_for_workdir(workdir)

    kwargs: Dict[str, Any] = {
        "cwd": str(workdir),
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "env": env,
    }
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        kwargs["start_new_session"] = True

    process = subprocess.Popen(command, **kwargs)
    lock = threading.Lock()
    stdout_chunks: List[str] = []
    stderr_chunks: List[str] = []
    started_at = time.time()
    timeout_seconds = max(1, int(timeout))
    session = {
        "sessionId": session_id,
        "process": process,
        "pid": process.pid,
        "lock": lock,
        "stdoutChunks": stdout_chunks,
        "stderrChunks": stderr_chunks,
        "stdoutLogPath": str(stdout_path),
        "stderrLogPath": str(stderr_path),
        "startedAt": started_at,
        "deadlineAt": started_at + timeout_seconds,
        "timeoutSeconds": timeout_seconds,
        "cwd": str(workdir),
        "command": list(command),
        "verificationUrl": "",
    }
    _register_auth_session(session)

    def _reader(stream, path: Path, chunks: List[str]) -> None:
        try:
            with path.open("a", encoding="utf-8", errors="replace") as handle:
                while True:
                    piece = stream.readline()
                    if not piece:
                        break
                    sanitized = _sanitize(piece)
                    handle.write(sanitized)
                    handle.flush()
                    with lock:
                        chunks.append(sanitized)
                        _trim_chunks(chunks)
        except Exception as exc:
            logger.debug(f"[FeishuCli] auth log reader failed for {path}: {exc}")
        finally:
            try:
                stream.close()
            except Exception:
                pass

    def _snapshot() -> tuple[str, Any]:
        with lock:
            stdout = "".join(stdout_chunks)[-12000:]
            stderr = "".join(stderr_chunks)[-12000:]
        output = stdout + ("\n" + stderr if stderr else "")
        return (
            output[-12000:] if len(output) > 12000 else output,
            _sanitize_json_payload(_parse_json_output(stdout)),
        )

    threading.Thread(
        target=_reader,
        args=(process.stdout, stdout_path, stdout_chunks),
        daemon=True,
        name="feishu-config-init-stdout",
    ).start()
    threading.Thread(
        target=_reader,
        args=(process.stderr, stderr_path, stderr_chunks),
        daemon=True,
        name="feishu-config-init-stderr",
    ).start()
    threading.Thread(
        target=lambda: process.wait(),
        daemon=True,
        name="feishu-config-init-wait",
    ).start()

    wait_seconds = max(1, min(max(1, int(timeout)), max(1, int(url_wait_seconds))))
    deadline = time.time() + wait_seconds
    while True:
        if cancel_event is not None and getattr(cancel_event, "is_set", lambda: False)():
            _kill_process_tree(process)
            output, _ = _snapshot()
            return {
                "status": "cancelled",
                "sessionId": session_id,
                "exitCode": None,
                "pid": process.pid,
                "output": output or "(no output)",
                "stdoutLogPath": str(stdout_path),
                "stderrLogPath": str(stderr_path),
                "message": "lark-cli command cancelled by user",
            }

        exit_code = process.poll()
        output, parsed = _snapshot()
        url = _auth_url_from_result({"output": output, "json": parsed})
        if url:
            with lock:
                session["verificationUrl"] = url
                session["exitCode"] = exit_code
            return {
                "status": "auth_pending" if exit_code is None else ("success" if exit_code == 0 else "error"),
                "sessionId": session_id,
                "exitCode": exit_code,
                "pid": process.pid,
                "backgroundProcess": exit_code is None,
                "writebackPending": exit_code is None,
                "cliWritebackTimeoutSeconds": max(1, int(timeout)),
                "output": output or "(no output)",
                "json": parsed,
                "stdoutLogPath": str(stdout_path),
                "stderrLogPath": str(stderr_path),
                "verificationUrl": url,
            }
        if exit_code is not None:
            with lock:
                session["exitCode"] = exit_code
                session["completedAt"] = time.time()
            return {
                "status": "success" if exit_code == 0 else "error",
                "sessionId": session_id,
                "exitCode": exit_code,
                "pid": process.pid,
                "backgroundProcess": False,
                "writebackPending": False,
                "output": output or "(no output)",
                "json": parsed,
                "stdoutLogPath": str(stdout_path),
                "stderrLogPath": str(stderr_path),
            }
        if time.time() >= deadline:
            _kill_process_tree(process)
            with lock:
                session["timedOut"] = True
                session["exitCode"] = process.poll()
            output, _ = _snapshot()
            return {
                "status": "timeout",
                "sessionId": session_id,
                "exitCode": None,
                "pid": process.pid,
                "backgroundProcess": False,
                "writebackPending": False,
                "output": output or "(no output)",
                "stdoutLogPath": str(stdout_path),
                "stderrLogPath": str(stderr_path),
                "message": (
                    f"lark-cli did not emit a verification URL within {wait_seconds} seconds; "
                    "the config process was stopped before the write-back window expired."
                ),
            }
        time.sleep(0.1)


def _parse_json_output(output: str) -> Any:
    text = (output or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:
        return None


def _as_args(raw: Any) -> List[str]:
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, list):
        return [str(item) for item in raw]
    return []


def _clean_cli_value(value: Any) -> str:
    return str(value or "").strip()


def _json_find_first(payload: Any, keys: Iterable[str]) -> str:
    wanted = {key.lower() for key in keys}
    if isinstance(payload, dict):
        for key, value in payload.items():
            if str(key).lower() in wanted:
                text = _clean_cli_value(value)
                if text:
                    return text
        for value in payload.values():
            found = _json_find_first(value, keys)
            if found:
                return found
    elif isinstance(payload, list):
        for item in payload:
            found = _json_find_first(item, keys)
            if found:
                return found
    return ""


def _extract_first_url(text: str) -> str:
    match = re.search(r"https?://[^\s\"'<>]+", text or "")
    return match.group(0) if match else ""


def _auth_url_from_result(result: Dict[str, Any]) -> str:
    parsed = result.get("json")
    url = _json_find_first(parsed, (
        "verification_url",
        "verification_uri",
        "verification_uri_complete",
        "console_url",
        "url",
    ))
    return url or _extract_first_url(str(result.get("output") or ""))


def _device_code_from_result(result: Dict[str, Any]) -> str:
    return _json_find_first(result.get("json"), ("device_code", "deviceCode", "device-code"))


def _auth_json_bool(payload: Any) -> Optional[bool]:
    if not isinstance(payload, dict):
        return None
    for key in (
        "authenticated",
        "isAuthenticated",
        "is_authenticated",
        "authorized",
        "configured",
        "loggedIn",
        "logged_in",
        "login",
    ):
        value = payload.get(key)
        if isinstance(value, bool):
            return value
    data = payload.get("data")
    if isinstance(data, dict):
        nested = _auth_json_bool(data)
        if nested is not None:
            return nested
        if data.get("defaultAs") or data.get("identity") or data.get("user") or data.get("tenant"):
            return True
    if payload.get("defaultAs") or payload.get("identity") or payload.get("user") or payload.get("tenant"):
        return True
    if payload.get("ok") is False:
        return False
    return None


def _auth_state_from_status_result(result: Dict[str, Any]) -> str:
    if result.get("status") == "timeout":
        return "unknown"
    parsed_state = _auth_json_bool(result.get("json"))
    if parsed_state is True:
        return "ready"
    if parsed_state is False:
        return "needs_login"
    output = str(result.get("output") or "").lower()
    if any(marker in output for marker in (
        "not configured",
        "not login",
        "not logged",
        "unauthorized",
        "no credential",
        "未配置",
        "未登录",
        "未授权",
    )):
        return "needs_login"
    if any(marker in output for marker in (
        "logged in",
        "authenticated",
        "authorized",
        "已登录",
        "已授权",
    )):
        return "ready"
    if result.get("exitCode") not in (0, None):
        return "needs_login"
    return "unknown"


def _check_expected_paths(paths: Iterable[str]) -> Dict[str, Any]:
    checked = []
    missing = []
    empty_dirs = []
    for raw in paths or []:
        path = Path(str(raw))
        exists = path.exists()
        count = None
        if exists and path.is_dir():
            count = len([item for item in path.iterdir()])
            if count == 0:
                empty_dirs.append(str(path))
        if not exists:
            missing.append(str(path))
        checked.append({"path": str(path), "exists": exists, "entries": count})
    return {"checked": checked, "missing": missing, "emptyDirs": empty_dirs}


def _has_find_skill_gate(args: Dict[str, Any]) -> bool:
    for key in ("discovery_source", "source", "via", "gate", "resolved_by"):
        value = str(args.get(key) or "").strip().lower().replace("_", "-")
        if value in {"find", "find-skill", "find skill"} or "find-skill" in value:
            return True
    result = args.get("find_skill_result")
    if result is None:
        result = args.get("findSkillResult")
    if result is None:
        return False
    if not isinstance(result, dict):
        return False
    status = str(result.get("status") or result.get("state") or result.get("result") or "").strip().lower()
    positive = (
        status in {"success", "ok", "found", "pass", "passed", "ready", "available"}
        or result.get("success") is True
        or result.get("ok") is True
        or result.get("found") is True
        or result.get("available") is True
    )
    if not positive:
        return False
    try:
        text = json.dumps(result, ensure_ascii=False, sort_keys=True).lower()
    except Exception:
        text = str(result).lower()
    return any(hint in text for hint in ("feishu", "lark", "飞书", "@larksuite", "lark-cli"))


def _find_skill_gate_error(package: str) -> Dict[str, Any]:
    return {
        "status": "error",
        "available": False,
        "discoveryOnly": True,
        "package": package,
        "sourceUrl": FEISHU_LARK_SOURCE_URL,
        "mirrorUrls": FEISHU_LARK_MIRROR_URLS,
        "message": (
            "Feishu/Lark CLI installation must be discovered through the built-in "
            "find skill / find-skill gate first. Retry feishu_cli install with "
            "discovery_source='find-skill' or a find_skill_result payload."
        ),
        "nextAction": {
            "skill": "find",
            "ability": "find-skill",
            "query": "official Feishu Lark CLI @larksuite/cli install source",
        },
    }


class FeishuCli(BaseTool):
    name: str = "feishu_cli"
    description: str = (
        "Use this instead of bash for Feishu/Lark operations when lark-cli is already "
        "available. It runs official lark-cli status/help diagnostics first, lets the "
        "agent choose the next auth/config step from that evidence, runs bounded "
        "lark-cli commands, and can install the official @larksuite/cli only after a "
        "find-skill/on-demand flow requests it."
    )
    params: dict = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "description": "One of: status, ensure, diagnose, install, agent_auth, config_init, config_init_status, auth_login, run",
            },
            "session_id": {
                "type": "string",
                "description": "Session ID returned by config_init for polling CLI auth/config write-back.",
            },
            "args": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Arguments after lark-cli for action=run, e.g. ['base', '+record-list', '--as', 'user', ...].",
            },
            "scope": {
                "type": "string",
                "description": "Scope for auth_login, e.g. search:docs:read. Use domain when unsure.",
            },
            "domain": {
                "type": "string",
                "description": "Domain for auth_login, e.g. base, docs, drive. Omit when the agent has not diagnosed the target domain yet.",
            },
            "device_code": {
                "type": "string",
                "description": "Device code returned by a previous auth_login --no-wait flow; completes authorization.",
            },
            "app_id": {
                "type": "string",
                "description": "Feishu/Lark app ID for config_init. If omitted, EcoreX uses saved external connection credentials.",
            },
            "app_secret": {
                "type": "string",
                "description": "Feishu/Lark app secret for config_init. Passed to lark-cli over stdin, never as a command argument.",
            },
            "brand": {
                "type": "string",
                "description": "Brand for config_init, usually feishu or lark. Defaults to feishu.",
            },
            "timeout": {
                "type": "integer",
                "description": "Timeout seconds for install/run actions.",
            },
            "install_if_missing": {
                "type": "boolean",
                "description": "Deprecated for ensure. Use action=install after find-skill discovery and user permission.",
            },
            "registry": {
                "type": "string",
                "description": "Optional npm registry for action=install. Defaults to npmjs, then npmmirror fallback.",
            },
            "discovery_source": {
                "type": "string",
                "description": "Required for action=install. Must be find-skill after built-in skill discovery.",
            },
            "find_skill_result": {
                "type": "object",
                "description": "Structured result returned by the built-in find skill/find-skill gate.",
            },
            "expected_paths": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional files/directories expected after a command. Empty directories are reported as errors.",
            },
        },
        "required": ["action"],
    }

    def __init__(self, config: dict = None):
        self.apply_config(config or {})

    def apply_config(self, config: dict) -> None:
        self.config = config or {}
        self.cwd = self.config.get("cwd", os.getcwd())
        self.package = str(self.config.get("package") or DEFAULT_LARK_CLI_PACKAGE)
        self.auto_install = bool(self.config.get("auto_install", False))

    def _env(self) -> Dict[str, str]:
        env = _tool_env()
        install_root = self._install_root()
        env["ECOREX_LARK_CLI_INSTALL_ROOT"] = str(install_root)
        _prepend_path(env, install_root / "bin")
        _prepend_path(env, install_root / "node_modules" / ".bin")
        return env

    def execute(self, args: Dict[str, Any]) -> ToolResult:
        action = str(args.get("action") or "").strip().lower()
        timeout = int(args.get("timeout") or self.config.get("timeout") or DEFAULT_TIMEOUT_SECONDS)
        install_if_missing = False
        env = self._env()

        if action == "status":
            return ToolResult.success(self._status(env, auth_timeout=max(1, min(timeout, 15))))
        if action == "diagnose":
            return ToolResult.success(self._diagnose(env))
        if action == "ensure":
            return self._ensure(env, timeout, install_if_missing=install_if_missing)
        if action == "install":
            if not _has_find_skill_gate(args):
                return ToolResult.fail(_find_skill_gate_error(self.package))
            return self._install(env, timeout, registry=str(args.get("registry") or "").strip())
        if action in {"agent_auth", "agent_authorize", "authorize_agent"}:
            ensure = self._ensure_payload(env, timeout, install_if_missing)
            if not ensure.get("available"):
                return ToolResult.fail(ensure)
            return self._agent_auth(args, env, timeout)
        if action == "config_init":
            ensure = self._ensure_payload(env, timeout, install_if_missing)
            if not ensure.get("available"):
                return ToolResult.fail(ensure)
            return self._config_init(args, env, timeout)
        if action in {"config_init_status", "agent_auth_status", "auth_status"}:
            return self._config_init_status(args, env, timeout)
        if action == "auth_login":
            ensure = self._ensure_payload(env, timeout, install_if_missing)
            if not ensure.get("available"):
                return ToolResult.fail(ensure)
            return self._auth_login(args, env, timeout)
        if action == "run":
            ensure = self._ensure_payload(env, timeout, install_if_missing)
            if not ensure.get("available"):
                return ToolResult.fail(ensure)
            return self._run_cli(args, env, timeout)
        return ToolResult.fail({"status": "error", "message": "action must be one of: status, ensure, diagnose, install, agent_auth, config_init, config_init_status, auth_login, run"})

    def _status(self, env: Dict[str, str], auth_timeout: int = 15) -> Dict[str, Any]:
        command = _resolve_lark_command(env)
        payload: Dict[str, Any] = {
            "status": "success",
            "available": bool(command),
            "command": command,
            "npm": _which("npm", env),
            "npx": _which("npx", env),
            "package": self.package,
            "installRoot": str(self._install_root()),
            "sourceUrl": FEISHU_LARK_SOURCE_URL,
            "mirrorUrls": FEISHU_LARK_MIRROR_URLS,
            "authState": "unknown" if command else "cli_missing",
            "pathHints": [str(path) for path in _candidate_bin_dirs() if path.exists()],
        }
        if command:
            result = self._safe_run(command + ["auth", "status", "--json"], env, auth_timeout)
            output = str(result.get("output") or "").lower()
            if result.get("exitCode") != 0 and any(marker in output for marker in (
                "unknown option",
                "unknown flag",
                "flag provided but not defined",
                "unrecognized option",
                "unrecognized flag",
                "no such option",
            )):
                result = self._safe_run(command + ["auth", "status"], env, auth_timeout)
            payload["authStatus"] = result
            payload["authState"] = _auth_state_from_status_result(result)
            payload["authenticated"] = payload["authState"] == "ready"
            if payload["authState"] == "needs_login":
                payload["nextAction"] = {
                    "tool": "feishu_cli",
                    "action": "agent_auth",
                    "reason": "diagnose official lark-cli auth/config flow before choosing scope, domain, or config init",
                }
        return payload

    def _ensure(self, env: Dict[str, str], timeout: int, install_if_missing: bool) -> ToolResult:
        return ToolResult.success(self._ensure_payload(env, timeout, install_if_missing))

    def _ensure_payload(self, env: Dict[str, str], timeout: int, install_if_missing: bool) -> Dict[str, Any]:
        command = _resolve_lark_command(env)
        if command:
            return {"status": "success", "available": True, "command": command, "installedNow": False}
        return self._missing_payload(env)

    def _install_root(self) -> Path:
        override = self.config.get("install_root") or os.environ.get("ECOREX_LARK_CLI_INSTALL_ROOT")
        if override:
            return Path(str(override)).expanduser()
        configured = _configured_install_root_from_conf()
        if configured:
            return configured
        return Path(__file__).resolve().parents[3] / "tools" / "lark-cli"

    def _diagnose(self, env: Dict[str, str]) -> Dict[str, Any]:
        status = self._status(env)
        status.update({
            "action": "diagnose",
            "installRoot": str(self._install_root()),
            "officialRegistry": DEFAULT_NPM_REGISTRY,
            "domesticRegistry": DOMESTIC_NPM_REGISTRY,
        })
        command = _resolve_lark_command(env)
        if command:
            status["officialAuthDiagnostics"] = self._official_auth_diagnostics(command, env, timeout=15)
        node = _which("node", env)
        if node:
            status["nodeVersion"] = self._safe_run([node, "--version"], env, 10)
        return status

    def _official_auth_diagnostics(self, command: List[str], env: Dict[str, str], timeout: int) -> Dict[str, Any]:
        """Ask lark-cli what auth/config capabilities it exposes before choosing a flow."""
        probe_timeout = max(3, min(int(timeout or DEFAULT_TIMEOUT_SECONDS), 8))
        probes: Dict[str, Any] = {}
        probe_commands = {
            "authStatusJson": ["auth", "status", "--json"],
            "authLoginHelp": ["auth", "login", "--help"],
            "configInitHelp": ["config", "init", "--help"],
            "authQrcodeHelp": ["auth", "qrcode", "--help"],
        }
        for probe_name, probe_args in probe_commands.items():
            result = self._safe_run(command + probe_args, env, probe_timeout)
            output = str(result.get("output") or "")
            probes[probe_name] = {
                "exitCode": result.get("exitCode"),
                "status": result.get("status"),
                "json": result.get("json"),
                "outputPreview": output[:1600],
            }

        auth_help = str(probes.get("authLoginHelp", {}).get("outputPreview") or "").lower()
        config_help = str(probes.get("configInitHelp", {}).get("outputPreview") or "").lower()
        qrcode_help = str(probes.get("authQrcodeHelp", {}).get("outputPreview") or "").lower()
        status_payload = {
            "status": "success",
            "authState": _auth_state_from_status_result({
                "status": probes.get("authStatusJson", {}).get("status"),
                "exitCode": probes.get("authStatusJson", {}).get("exitCode"),
                "output": probes.get("authStatusJson", {}).get("outputPreview"),
                "json": probes.get("authStatusJson", {}).get("json"),
            }),
            "probes": probes,
            "capabilities": {
                "authLoginNoWaitJson": "--no-wait" in auth_help and "--json" in auth_help,
                "authLoginDeviceCode": "--device-code" in auth_help,
                "configInitNew": "--new" in config_help,
                "configInitSecretStdin": "--app-secret-stdin" in config_help,
                "authQrcode": "qrcode" in qrcode_help or "--output" in qrcode_help,
            },
            "selectionPolicy": (
                "Agent chooses from official lark-cli diagnostics at runtime. "
                "EcoreX does not default to a fixed domain/scope or rewrite Feishu URLs."
            ),
        }
        return status_payload

    @staticmethod
    def _merge_official_diagnostics(payload: Any, diagnostics: Dict[str, Any], decision: str) -> Dict[str, Any]:
        merged = payload if isinstance(payload, dict) else {"result": payload}
        merged["officialAuthDiagnostics"] = diagnostics
        merged["authDecision"] = decision
        merged["fixedFlow"] = False
        return merged

    def _agent_auth(self, args: Dict[str, Any], env: Dict[str, str], timeout: int) -> ToolResult:
        command = _resolve_lark_command(env)
        if not command:
            return ToolResult.fail(self._missing_payload(env))

        status = self._status(env, auth_timeout=max(1, min(timeout, 15)))
        diagnostics = self._official_auth_diagnostics(command, env, timeout)
        if _auth_status_is_ready(status):
            return ToolResult.success({
                "status": "success",
                "authRequired": False,
                "authCompleted": True,
                "authenticated": True,
                "authState": _auth_status_state(status),
                "officialAuthDiagnostics": diagnostics,
                "authDecision": "status_ready",
                "fixedFlow": False,
                "message": "Feishu/Lark CLI is already authenticated according to official lark-cli status.",
            })

        device_code = _clean_cli_value(args.get("device_code") or args.get("deviceCode"))
        if device_code:
            result = self._auth_login(args, env, timeout)
            result.result = self._merge_official_diagnostics(getattr(result, "result", {}), diagnostics, "complete_device_code")
            return result

        scope = str(args.get("scope") or "").strip()
        domain = str(args.get("domain") or "").strip()
        if scope or domain:
            result = self._auth_login(args, env, timeout)
            result.result = self._merge_official_diagnostics(getattr(result, "result", {}), diagnostics, "auth_login_split_flow_from_agent_target")
            return result

        capabilities = diagnostics.get("capabilities") if isinstance(diagnostics.get("capabilities"), dict) else {}
        if capabilities.get("configInitNew") is not False:
            flow_args = dict(args)
            flow_args["use_saved_credentials"] = False
            flow_args["args"] = []
            result = self._config_init(flow_args, env, timeout)
            result.result = self._merge_official_diagnostics(getattr(result, "result", {}), diagnostics, "config_init_new_from_official_diagnostics")
            return result

        if capabilities.get("authLoginNoWaitJson"):
            return ToolResult.fail({
                "status": "needs_target_scope",
                "authRequired": True,
                "authCompleted": False,
                "authenticated": False,
                "authState": _auth_status_state(status),
                "officialAuthDiagnostics": diagnostics,
                "fixedFlow": False,
                "message": (
                    "lark-cli exposes split-flow login, but the agent needs to choose a scope or domain "
                    "from the user's actual task before starting auth_login."
                ),
                "nextAction": {
                    "tool": "feishu_cli",
                    "action": "agent_auth",
                    "requires": ["scope or domain derived from the target Feishu operation"],
                },
            })

        return ToolResult.fail({
            "status": "error",
            "authRequired": True,
            "authCompleted": False,
            "authenticated": False,
            "authState": _auth_status_state(status),
            "officialAuthDiagnostics": diagnostics,
            "fixedFlow": False,
            "message": "No supported official lark-cli auth/config flow was found from diagnostics.",
            "nextAction": {"tool": "feishu_cli", "action": "diagnose"},
        })

    def _install(self, env: Dict[str, str], timeout: int, registry: str = "") -> ToolResult:
        existing = _resolve_lark_command(env)
        if existing:
            return ToolResult.success({
                "status": "success",
                "available": True,
                "installedNow": False,
                "command": existing,
                "message": "lark-cli is already available.",
            })

        npm = _which("npm", env)
        if not npm:
            return ToolResult.fail({
                **self._missing_payload(env, "npm is not available; cannot install @larksuite/cli on demand."),
                "nextAction": "Install Node.js/npm or ask an administrator to preinstall @larksuite/cli.",
            })

        install_root = self._install_root()
        install_root.mkdir(parents=True, exist_ok=True)
        registries = [registry] if registry else [DEFAULT_NPM_REGISTRY, DOMESTIC_NPM_REGISTRY]
        attempts: List[Dict[str, Any]] = []
        for npm_registry in [item for item in registries if item]:
            command = [
                npm,
                "install",
                "--prefix",
                str(install_root),
                self.package,
                "--registry",
                npm_registry,
            ]
            result = self._safe_run(command, env, timeout)
            attempts.append({
                "registry": npm_registry,
                "exitCode": result.get("exitCode"),
                "status": result.get("status"),
                "output": result.get("output", "")[-1200:],
            })
            if result.get("exitCode") == 0:
                next_env = self._env()
                resolved = _resolve_lark_command(next_env)
                return ToolResult.success({
                    "status": "success",
                    "available": bool(resolved),
                    "installedNow": True,
                    "command": resolved,
                    "package": self.package,
                    "installRoot": str(install_root),
                    "sourceUrl": FEISHU_LARK_SOURCE_URL,
                    "registry": npm_registry,
                    "fallbackUsed": npm_registry != DEFAULT_NPM_REGISTRY,
                    "attempts": attempts,
                    "message": "Installed official @larksuite/cli on demand.",
                })

        return ToolResult.fail({
            "status": "error",
            "available": False,
            "package": self.package,
            "installRoot": str(install_root),
            "sourceUrl": FEISHU_LARK_SOURCE_URL,
            "mirrorUrls": FEISHU_LARK_MIRROR_URLS,
            "attempts": attempts,
            "message": "Failed to install official @larksuite/cli from npmjs.org and domestic npm mirror.",
        })

    def _missing_payload(self, env: Dict[str, str], message: str = "") -> Dict[str, Any]:
        return {
            "status": "error",
            "available": False,
            "discoveryOnly": True,
            "message": message or "lark-cli is not available. EcoreX no longer auto-installs the old CLI path; use the built-in find skill/find-skill gate first.",
            "npm": _which("npm", env),
            "sourceUrl": FEISHU_LARK_SOURCE_URL,
            "mirrorUrls": FEISHU_LARK_MIRROR_URLS,
            "installHint": (
                "Use the built-in find skill first (gated as find-skill) to discover and install the Feishu/Lark skill or connector. "
                f"For real CLI work, run feishu_cli action=install with discovery_source='find-skill' to install official {self.package}. "
                f"If npmjs.org times out, EcoreX retries with domestic npm mirror {DOMESTIC_NPM_REGISTRY}."
            ),
            "pathHints": [str(path) for path in _candidate_bin_dirs() if path.exists()],
        }

    def _config_init(self, args: Dict[str, Any], env: Dict[str, str], timeout: int) -> ToolResult:
        command = _resolve_lark_command(env)
        if not command:
            return ToolResult.fail(self._missing_payload(env))
        app_id, app_secret, credential_source = self._feishu_credentials(args)
        brand = _clean_cli_value(args.get("brand") or "feishu") or "feishu"
        if app_id and app_secret:
            cli_args = ["config", "init", "--app-id", app_id, "--app-secret-stdin", "--brand", brand]
            name = _clean_cli_value(args.get("name"))
            if name:
                cli_args.extend(["--name", name])
            extra = _as_args(args.get("args"))
            if extra:
                cli_args.extend(extra)
            result = self._safe_run(command + cli_args, env, timeout, input_text=app_secret + "\n")
            result["credentialSource"] = credential_source
            result["message"] = (
                "lark-cli config initialized from EcoreX Feishu external connection credentials."
                if result.get("exitCode") == 0
                else "lark-cli config initialization failed."
            )
            if result.get("exitCode") != 0:
                return ToolResult.fail(result)
            return ToolResult.success(result)

        cli_args = ["config", "init", "--new"]
        extra = _as_args(args.get("args"))
        if extra:
            cli_args.extend(extra)
        result = _run_process_until_auth_url(
            command + cli_args,
            timeout=timeout,
            env=env,
            cwd=self.cwd,
            cancel_event=getattr(self, "cancel_event", None),
        )
        result["command"] = self._display_command(command + cli_args)
        url = str(result.get("verificationUrl") or _auth_url_from_result(result))
        if url:
            result["verificationUrl"] = url
            result["qrCode"] = self._generate_auth_qrcode(command, env, url, timeout)
            result["authFlow"] = "config_init_start"
            result["authRequired"] = True
            if result.get("backgroundProcess"):
                result["message"] = (
                    "Open the verification URL or scan the QR code now. "
                    f"The lark-cli config process is still running and can write back within {timeout} seconds."
                )
                next_action = {"tool": "feishu_cli", "action": "config_init_status"}
                if result.get("sessionId"):
                    next_action["session_id"] = result.get("sessionId")
                result["nextAction"] = next_action
            elif result.get("exitCode") == 0:
                result["authRequired"] = False
                result["message"] = "lark-cli config initialization completed."
            else:
                result["message"] = (
                    "lark-cli emitted a verification URL but exited before config write-back could continue."
                )
        if result.get("exitCode") not in (0, None) or result.get("status") in {"timeout", "cancelled"}:
            return ToolResult.fail(result)
        return ToolResult.success(result)

    def _config_init_status(self, args: Dict[str, Any], env: Dict[str, str], timeout: int) -> ToolResult:
        session_id = _clean_cli_value(args.get("session_id") or args.get("sessionId"))
        snapshot = _auth_session_snapshot(session_id)
        if snapshot.get("status") == "not_found" or snapshot.get("status") == "error":
            return ToolResult.fail(snapshot)
        if snapshot.get("status") == "success":
            auth_status = self._status(env, auth_timeout=max(1, min(timeout, 15)))
            snapshot["authStatus"] = auth_status
            snapshot["authState"] = _auth_status_state(auth_status)
            snapshot["authenticated"] = _auth_status_is_ready(auth_status)
            snapshot["authCompleted"] = bool(snapshot["authenticated"])
            snapshot["authRequired"] = not bool(snapshot["authenticated"])
            if not snapshot["authenticated"]:
                snapshot["status"] = "auth_incomplete"
                snapshot["message"] = (
                    "lark-cli config process exited, but Feishu CLI auth status is not ready yet. "
                    "Retry authorization or run feishu_cli status for details."
                )
        else:
            snapshot["authRequired"] = bool(snapshot.get("writebackPending"))
        if snapshot.get("status") in {"timeout", "error", "cancelled", "auth_incomplete"}:
            return ToolResult.fail(snapshot)
        return ToolResult.success(snapshot)

    def _auth_login(self, args: Dict[str, Any], env: Dict[str, str], timeout: int) -> ToolResult:
        command = _resolve_lark_command(env)
        if not command:
            return ToolResult.fail(self._missing_payload(env))

        device_code = _clean_cli_value(args.get("device_code") or args.get("deviceCode"))
        if device_code:
            result = self._safe_run(command + ["auth", "login", "--device-code", device_code], env, timeout)
            result["authFlow"] = "complete"
            if result.get("exitCode") != 0:
                return ToolResult.fail(result)
            result["authStatus"] = self._status(env, auth_timeout=max(1, min(timeout, 15)))
            result["authState"] = _auth_status_state(result["authStatus"])
            result["authenticated"] = _auth_status_is_ready(result["authStatus"])
            result["authCompleted"] = bool(result["authenticated"])
            if not result["authenticated"]:
                result["status"] = "auth_incomplete"
                result["message"] = "lark-cli auth login exited 0, but Feishu CLI auth status is not ready."
                return ToolResult.fail(result)
            return ToolResult.success(result)

        cli_args = ["auth", "login"]
        scope = str(args.get("scope") or "").strip()
        domain = str(args.get("domain") or "").strip()
        if scope:
            cli_args.extend(["--scope", scope])
        elif domain:
            cli_args.extend(["--domain", domain])
        else:
            return ToolResult.fail({
                "status": "needs_target_scope",
                "authRequired": True,
                "fixedFlow": False,
                "message": "auth_login requires a scope or domain chosen from official lark-cli diagnostics and the target operation.",
                "nextAction": {"tool": "feishu_cli", "action": "agent_auth"},
            })
        cli_args.extend(["--no-wait", "--json"])

        result = self._safe_run(command + cli_args, env, timeout)
        url = _auth_url_from_result(result)
        device_code = _device_code_from_result(result)
        result["authFlow"] = "start"
        if device_code:
            result["deviceCode"] = device_code
        if url:
            result["verificationUrl"] = url
            result["qrCode"] = self._generate_auth_qrcode(command, env, url, timeout)
            result["authRequired"] = True
            result["message"] = (
                "Open the verification URL or scan the QR code, finish Feishu authorization, "
                "then run feishu_cli auth_login again with device_code to complete."
            )
            result["nextAction"] = {
                "tool": "feishu_cli",
                "action": "auth_login",
                "device_code": device_code,
            }
        if result.get("exitCode") != 0:
            return ToolResult.fail(result)
        return ToolResult.success(result)

    def _feishu_credentials(self, args: Dict[str, Any]) -> tuple[str, str, str]:
        app_id = _clean_cli_value(
            args.get("app_id")
            or args.get("appId")
            or args.get("feishu_app_id")
            or args.get("client_id")
            or args.get("clientId")
        )
        app_secret = _clean_cli_value(
            args.get("app_secret")
            or args.get("appSecret")
            or args.get("feishu_app_secret")
            or args.get("client_secret")
            or args.get("clientSecret")
        )
        if app_id and app_secret:
            return app_id, app_secret, "tool_args"

        use_saved = args.get("use_saved_credentials")
        if use_saved is None:
            use_saved = args.get("useSavedCredentials")
        if use_saved is False:
            return "", "", "disabled_by_agent_auth"

        env_app_id = _clean_cli_value(os.environ.get("FEISHU_APP_ID") or os.environ.get("LARK_APP_ID"))
        env_app_secret = _clean_cli_value(os.environ.get("FEISHU_APP_SECRET") or os.environ.get("LARK_APP_SECRET"))
        if env_app_id and env_app_secret:
            return env_app_id, env_app_secret, "environment"

        try:
            from config import conf, load_config

            cfg = conf()
            if not cfg:
                load_config()
                cfg = conf()
            config_app_id = _clean_cli_value(cfg.get("feishu_app_id") or cfg.get("lark_app_id"))
            config_app_secret = _clean_cli_value(cfg.get("feishu_app_secret") or cfg.get("lark_app_secret"))
            if config_app_id and config_app_secret:
                return config_app_id, config_app_secret, "ecorex_external_connection"
        except Exception as exc:
            logger.debug(f"[FeishuCli] failed reading Feishu credentials from config: {exc}")
        return "", "", "missing"

    def _generate_auth_qrcode(self, command: List[str], env: Dict[str, str], url: str, timeout: int) -> Dict[str, Any]:
        if not url:
            return {"status": "skipped", "message": "verification URL missing"}
        cwd = Path(self.cwd or os.getcwd()).expanduser()
        cwd.mkdir(parents=True, exist_ok=True)
        qr_dir = cwd / ".ecorex" / "lark-auth"
        qr_dir.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
        relative_output = f".ecorex/lark-auth/feishu-auth-{digest}.png"
        result = self._safe_run(
            command + ["auth", "qrcode", url, "--output", relative_output, "--size", "256"],
            env,
            max(3, min(timeout, 5)),
        )
        path = cwd / relative_output
        return {
            "status": result.get("status"),
            "exitCode": result.get("exitCode"),
            "path": str(path),
            "relativePath": relative_output,
            "output": result.get("output", ""),
        }

    def _run_cli(self, args: Dict[str, Any], env: Dict[str, str], timeout: int) -> ToolResult:
        command = _resolve_lark_command(env)
        if not command:
            return ToolResult.fail(self._missing_payload(env))
        cli_args = _as_args(args.get("args"))
        if cli_args and Path(cli_args[0]).name.lower() in {"lark-cli", "lark-cli.cmd"}:
            cli_args = cli_args[1:]
        if not cli_args:
            return ToolResult.fail({"status": "error", "message": "args is required for action=run"})

        result = self._safe_run(command + cli_args, env, timeout)
        expected = _check_expected_paths(args.get("expected_paths") or [])
        if expected["checked"]:
            result["expectedPaths"] = expected
        if result.get("exitCode") != 0:
            return ToolResult.fail(result)
        parsed = result.get("json")
        if isinstance(parsed, dict) and parsed.get("ok") is False:
            return ToolResult.fail(result)
        if expected["missing"] or expected["emptyDirs"]:
            result["status"] = "error"
            result["message"] = "Command exited 0 but expected output files were missing or empty."
            return ToolResult.fail(result)
        return ToolResult.success(result)

    def _safe_run(self, command: List[str], env: Dict[str, str], timeout: int, input_text: Optional[str] = None) -> Dict[str, Any]:
        try:
            result = _run_process(
                command,
                timeout=timeout,
                env=env,
                cwd=self.cwd,
                cancel_event=getattr(self, "cancel_event", None),
                input_text=input_text,
            )
        except subprocess.TimeoutExpired as exc:
            return {
                "status": "timeout",
                "exitCode": None,
                "command": self._display_command(command),
                "output": _sanitize((exc.output or "") + ("\n" + exc.stderr if exc.stderr else ""))[-4000:],
                "message": f"lark-cli command timed out after {timeout} seconds",
            }
        except _ProcessCancelled as exc:
            return {
                "status": "cancelled",
                "exitCode": None,
                "command": self._display_command(command),
                "output": _sanitize((exc.stdout or "") + ("\n" + exc.stderr if exc.stderr else ""))[-4000:],
                "message": "lark-cli command cancelled by user",
            }
        output = (result.stdout or "") + ("\n" + result.stderr if result.stderr else "")
        output = _sanitize(output)
        return {
            "status": "success" if result.returncode == 0 else "error",
            "exitCode": result.returncode,
            "command": self._display_command(command),
            "output": output[-12000:] if output else "(no output)",
            "json": _sanitize_json_payload(_parse_json_output(result.stdout)),
        }

    @staticmethod
    def _display_command(command: List[str]) -> List[str]:
        display = []
        redact_next = False
        sensitive_flags = {
            "--app-id",
            "--app_secret",
            "--app-secret",
            "--device-code",
            "--access-token",
            "--refresh-token",
        }
        for part in command:
            text = str(part)
            if redact_next:
                display.append("***")
                redact_next = False
            elif text in sensitive_flags:
                display.append(text)
                redact_next = True
            elif re.search(r"(?i)(token|secret|password|api[_-]?key)", text):
                display.append("***")
            else:
                display.append(text)
        return display
