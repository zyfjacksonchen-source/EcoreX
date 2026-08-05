"""
Bash tool - Execute bash commands
"""

import os
import re
import signal
import sys
import subprocess
import tempfile
from typing import Dict, Any

from agent.tools.base_tool import BaseTool, ToolResult
from agent.tools.bash import exit_codes
from agent.tools.utils.truncate import truncate_tail, format_size, DEFAULT_MAX_LINES, DEFAULT_MAX_BYTES
from common.log import logger
from common.utils import expand_path


DEFAULT_BASH_MAX_TIMEOUT_SECONDS = 2 * 60 * 60
LONG_RUNNING_DEFAULT_TIMEOUT_SECONDS = 30 * 60

_LONG_RUNNING_COMMAND_RE = re.compile(
    r"("
    r"openai-image-vision|vision\.sh|image\s*generation|generate[-_\s]?image|"
    r"生图|图片生成|图片重生|图像生成|"
    r"remotion|ffmpeg|imagemagick|magick|playwright\s+install|"
    r"browser[-_\s]?automation|browser[-_\s]?cdp|npm\s+(install|ci|run\s+build)|"
    r"pnpm\s+(install|run\s+build)|yarn\s+(install|build)|pip\s+install|"
    r"docker\s+build|pytest|vitest|cargo\s+(build|test)|go\s+test"
    r")",
    re.IGNORECASE,
)


def _bash_max_timeout_seconds() -> int:
    raw_value = os.environ.get("ECOREX_BASH_MAX_TIMEOUT_SECONDS", "")
    if raw_value:
        try:
            return max(60, min(int(float(raw_value)), 24 * 60 * 60))
        except (TypeError, ValueError):
            logger.warning("[Bash] invalid ECOREX_BASH_MAX_TIMEOUT_SECONDS=%r; using default", raw_value)
    return DEFAULT_BASH_MAX_TIMEOUT_SECONDS


def _looks_long_running_command(command: str) -> bool:
    return bool(command and _LONG_RUNNING_COMMAND_RE.search(command))


class _CommandCancelled(Exception):
    def __init__(self, stdout: str = "", stderr: str = ""):
        super().__init__("command cancelled by user")
        self.stdout = stdout or ""
        self.stderr = stderr or ""


class Bash(BaseTool):
    """Tool for executing bash commands"""

    _IS_WIN = sys.platform == "win32"

    name: str = "bash"
    description: str = f"""Execute a bash command in the current working directory. Returns stdout and stderr. Output is truncated to last {DEFAULT_MAX_LINES} lines or {DEFAULT_MAX_BYTES // 1024}KB (whichever is hit first). If truncated, full output is saved to a temp file.
{'''
PLATFORM: Windows (cmd.exe). Do NOT use Unix-only commands like grep, head, tail, sed, awk.
''' if _IS_WIN else ''}
ENVIRONMENT: All API keys from env_config are auto-injected. Use $VAR_NAME directly.

SAFETY:
- Freely create/modify/delete files within the workspace
- For destructive commands out of workspace, explain and confirm first"""

    params: dict = {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "Bash command to execute"
            },
            "timeout": {
                "type": "integer",
                "description": "Timeout in seconds (optional, default: 30). Use a larger value for long builds, deployments, image generation, or installs."
            }
        },
        "required": ["command"]
    }

    def __init__(self, config: dict = None):
        self.config = config or {}
        self.cwd = self.config.get("cwd", os.getcwd())
        # Ensure working directory exists
        if not os.path.exists(self.cwd):
            os.makedirs(self.cwd, exist_ok=True)
        self.default_timeout = self._normalize_timeout(self.config.get("timeout", 30))
        # Enable safety mode by default (can be disabled in config)
        self.safety_mode = self.config.get("safety_mode", True)

    @staticmethod
    def _normalize_timeout(value, default: int = 30) -> int:
        try:
            timeout = int(value)
        except (TypeError, ValueError):
            timeout = default
        return max(1, min(timeout, _bash_max_timeout_seconds()))

    def _kill_process_tree(self, process: subprocess.Popen):
        if process.poll() is not None:
            return
        if self._IS_WIN:
            try:
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=5,
                )
                return
            except Exception as e:
                logger.debug(f"[Bash] taskkill failed for pid {process.pid}: {e}")
        else:
            try:
                os.killpg(process.pid, signal.SIGKILL)
                return
            except Exception as e:
                logger.debug(f"[Bash] killpg failed for pid {process.pid}: {e}")
        try:
            process.kill()
        except Exception:
            pass

    def _run_command(self, command, timeout: int, env: dict, shell: bool = True, cancel_event=None):
        kwargs = {
            "shell": shell,
            "cwd": self.cwd,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "text": True,
            "encoding": "utf-8",
            "errors": "replace",
            "env": env,
        }
        if self._IS_WIN:
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        else:
            kwargs["start_new_session"] = True

        process = subprocess.Popen(command, **kwargs)
        deadline = None
        if timeout is not None:
            import time
            deadline = time.time() + timeout
        stdout = ""
        stderr = ""
        while True:
            try:
                stdout, stderr = process.communicate(timeout=0.25)
                break
            except subprocess.TimeoutExpired:
                if cancel_event is not None and getattr(cancel_event, "is_set", lambda: False)():
                    self._kill_process_tree(process)
                    try:
                        stdout, stderr = process.communicate(timeout=5)
                    except subprocess.TimeoutExpired:
                        try:
                            process.kill()
                        except Exception:
                            pass
                        stdout, stderr = process.communicate()
                    raise _CommandCancelled(stdout, stderr)
                if deadline is not None:
                    import time
                    if time.time() >= deadline:
                        self._kill_process_tree(process)
                        try:
                            stdout, stderr = process.communicate(timeout=5)
                        except subprocess.TimeoutExpired:
                            try:
                                process.kill()
                            except Exception:
                                pass
                            stdout, stderr = process.communicate()
                        exc = subprocess.TimeoutExpired(command, timeout, output=stdout, stderr=stderr)
                        raise exc
                continue
            except Exception:
                try:
                    if cancel_event is not None and getattr(cancel_event, "is_set", lambda: False)():
                        self._kill_process_tree(process)
                except Exception:
                    pass
                raise
        return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)

    def execute(self, args: Dict[str, Any]) -> ToolResult:
        """
        Execute a bash command
        
        :param args: Dictionary containing the command and optional timeout
        :return: Command output or error
        """
        command = args.get("command", "").strip()
        raw_timeout = args.get("timeout")
        default_timeout = self.default_timeout
        if raw_timeout is None and _looks_long_running_command(command):
            default_timeout = min(LONG_RUNNING_DEFAULT_TIMEOUT_SECONDS, _bash_max_timeout_seconds())
        timeout = self._normalize_timeout(raw_timeout if raw_timeout is not None else default_timeout, default_timeout)

        if not command:
            return ToolResult.fail("Error: command parameter is required")

        # Security check: Prevent direct access to the credential file
        if re.search(r'\.cow[/\\]\.env', command):
            return ToolResult.fail(
                "Error: Access denied. API keys and credentials must be accessed through the env_config tool only."
            )

        if self._looks_like_tongxin_cli_command(command):
            return ToolResult.fail(
                "Error: Do not call Tongxin Assistant CLI through raw bash. "
                "Use the tongxin_cli tool so EcoreX can enforce the all-user read-only command allowlist."
            )

        # Optional safety check - only warn about extremely dangerous commands
        if self.safety_mode:
            warning = self._get_safety_warning(command)
            if warning:
                return ToolResult.fail(
                    f"Safety Warning: {warning}\n\nIf you believe this command is safe and necessary, please ask the user for confirmation first, explaining what the command does and why it's needed.")

        try:
            # Prepare environment with .env file variables
            env = os.environ.copy()
            
            # Load environment variables from ~/.cow/.env if it exists
            env_file = expand_path("~/.cow/.env")
            dotenv_vars = {}
            if os.path.exists(env_file):
                try:
                    from dotenv import dotenv_values
                    dotenv_vars = dotenv_values(env_file)
                    env.update(dotenv_vars)
                    logger.debug(f"[Bash] Loaded {len(dotenv_vars)} variables from {env_file}")
                except ImportError:
                    logger.debug("[Bash] python-dotenv not installed, skipping .env loading")
                except Exception as e:
                    logger.debug(f"[Bash] Failed to load .env: {e}")

            # getuid() only exists on Unix-like systems
            if hasattr(os, 'getuid'):
                logger.debug(f"[Bash] Process UID: {os.getuid()}")
            else:
                logger.debug(f"[Bash] Process User: {os.environ.get('USERNAME', os.environ.get('USER', 'unknown'))}")
            
            # On Windows, convert $VAR references to %VAR% for cmd.exe
            if self._IS_WIN:
                env["PYTHONIOENCODING"] = "utf-8"
                command = self._convert_env_vars_for_windows(command, dotenv_vars)
                if command and not command.strip().lower().startswith("chcp"):
                    command = f"chcp 65001 >nul 2>&1 && {command}"

            result = self._run_command(
                command,
                timeout,
                env,
                shell=True,
                cancel_event=getattr(self, "cancel_event", None),
            )
            
            logger.debug(f"[Bash] Exit code: {result.returncode}")
            logger.debug(f"[Bash] Stdout length: {len(result.stdout)}")
            logger.debug(f"[Bash] Stderr length: {len(result.stderr)}")
            
            # Workaround for exit code 126 with no output
            if result.returncode == 126 and not result.stdout and not result.stderr:
                logger.warning(f"[Bash] Exit 126 with no output - trying alternative execution method")
                # Try using argument list instead of shell=True
                import shlex
                try:
                    parts = shlex.split(command)
                    if len(parts) > 0:
                        logger.info(f"[Bash] Retrying with argument list: {parts[:3]}...")
                        retry_result = self._run_command(
                            parts,
                            timeout=timeout,
                            env=env,
                            shell=False,
                            cancel_event=getattr(self, "cancel_event", None),
                        )
                        logger.debug(f"[Bash] Retry exit code: {retry_result.returncode}, stdout: {len(retry_result.stdout)}, stderr: {len(retry_result.stderr)}")
                        
                        # If retry succeeded, use retry result
                        if retry_result.returncode == 0 or retry_result.stdout or retry_result.stderr:
                            result = retry_result
                        else:
                            # Both attempts failed - check if this is openai-image-vision skill
                            if 'openai-image-vision' in command or 'vision.sh' in command:
                                # Create a mock result with helpful error message
                                from types import SimpleNamespace
                                result = SimpleNamespace(
                                    returncode=1,
                                    stdout='{"error": "图片无法解析", "reason": "该图片格式可能不受支持，或图片文件存在问题", "suggestion": "请尝试其他图片"}',
                                    stderr=''
                                )
                                logger.info(f"[Bash] Converted exit 126 to user-friendly image error message for vision skill")
                except Exception as retry_err:
                    logger.warning(f"[Bash] Retry failed: {retry_err}")

            # When command succeeds with stdout, keep output clean (stderr goes to server log only).
            # When command fails or stdout is empty, include stderr so the agent can diagnose.
            if result.returncode == 0 and result.stdout.strip():
                output = result.stdout
                if result.stderr:
                    logger.info(f"[Bash] stderr (not forwarded): {result.stderr[:500]}")
            else:
                output = result.stdout
                if result.stderr:
                    output += "\n" + result.stderr

            # Check if we need to save full output to temp file
            temp_file_path = None
            total_bytes = len(output.encode('utf-8'))

            if total_bytes > DEFAULT_MAX_BYTES:
                # Save full output to temp file
                with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.log', prefix='bash-') as f:
                    f.write(output)
                    temp_file_path = f.name

            # Apply tail truncation
            truncation = truncate_tail(output)
            output_text = truncation.content or "(no output)"

            # Build result
            details = {}

            if truncation.truncated:
                details["truncation"] = truncation.to_dict()
                if temp_file_path:
                    details["full_output_path"] = temp_file_path

                # Build notice
                start_line = truncation.total_lines - truncation.output_lines + 1
                end_line = truncation.total_lines

                if truncation.last_line_partial:
                    # Edge case: last line alone > 30KB
                    last_line = output.split('\n')[-1] if output else ""
                    last_line_size = format_size(len(last_line.encode('utf-8')))
                    output_text += f"\n\n[Showing last {format_size(truncation.output_bytes)} of line {end_line} (line is {last_line_size}). Full output: {temp_file_path}]"
                elif truncation.truncated_by == "lines":
                    output_text += f"\n\n[Showing lines {start_line}-{end_line} of {truncation.total_lines}. Full output: {temp_file_path}]"
                else:
                    output_text += f"\n\n[Showing lines {start_line}-{end_line} of {truncation.total_lines} ({format_size(DEFAULT_MAX_BYTES)} limit). Full output: {temp_file_path}]"

            # A few search/comparison commands use exit 1 as a useful result.
            is_error, exit_note = exit_codes.interpret(command, result.returncode)
            if is_error:
                output_text += f"\n\nCommand exited with code {result.returncode}"
                return ToolResult.fail({
                    "output": output_text,
                    "exit_code": result.returncode,
                    "details": details if details else None
                })
            if exit_note:
                output_text += f"\n\n[Exit code {result.returncode}: {exit_note}]"

            return ToolResult.success({
                "output": output_text,
                "exit_code": result.returncode,
                "details": details if details else None
            })

        except subprocess.TimeoutExpired:
            return ToolResult.fail(f"Error: Command timed out after {timeout} seconds")
        except _CommandCancelled as exc:
            output = (exc.stdout or "") + ("\n" + exc.stderr if exc.stderr else "")
            output = output.strip()
            if output:
                output = truncate_tail(output).content
                return ToolResult.fail(f"Error: Command cancelled by user.\n{output}")
            return ToolResult.fail("Error: Command cancelled by user.")
        except Exception as e:
            return ToolResult.fail(f"Error executing command: {str(e)}")

    def _get_safety_warning(self, command: str) -> str:
        """
        Get safety warning for absolutely catastrophic commands only.
        Keep the blocklist minimal so the agent retains maximum freedom.

        :param command: Command to check
        :return: Warning message if dangerous, empty string if safe
        """
        # Tokenize to avoid substring false positives (e.g. `rm -rf /tmp/x`
        # must not match `rm -rf /`).
        tokens = command.lower().split()

        # `rm -rf /` or `rm -rf /*` targeting the real root.
        for i, tok in enumerate(tokens):
            if tok != "rm":
                continue
            has_rf = False
            for j in range(i + 1, len(tokens)):
                t = tokens[j]
                if t.startswith("-") and "r" in t and "f" in t:
                    has_rf = True
                elif t in ("--recursive", "--force"):
                    continue
                elif t in ("/", "/*"):
                    if has_rf:
                        return "This command will delete the entire filesystem"
                    break
                else:
                    break

        # Disk wiping
        if "if=/dev/zero" in command.lower() and "dd " in command.lower():
            return "This command can destroy disk data"

        # Power control - match only as a standalone word (\b enforces word boundary)
        if re.search(r'\b(shutdown|reboot|halt|poweroff)\b', command.lower()):
            return "This command will shut down or restart the system"

        return ""

    @staticmethod
    def _looks_like_tongxin_cli_command(command: str) -> bool:
        text = str(command or "").strip().lower().replace("\\", "/")
        if "xin_agent_cli.py" in text or "xin agent cli.py" in text or "xin-agent-cli.py" in text or "tongxin_cli.py" in text:
            return True
        if re.search(r"\b(tongxin-cli|xin-agent-cli|tx-assistant)\b", text):
            return True
        if "/自动报表工具/" in text and "xin_agent" in text:
            return True
        return False

    @staticmethod
    def _convert_env_vars_for_windows(command: str, dotenv_vars: dict) -> str:
        """
        Convert bash-style $VAR / ${VAR} references to cmd.exe %VAR% syntax.
        Only converts variables loaded from .env (user-configured API keys etc.)
        to avoid breaking $PATH, jq expressions, regex, etc.
        """
        if not dotenv_vars:
            return command

        def replace_match(m):
            var_name = m.group(1) or m.group(2)
            if var_name in dotenv_vars:
                return f"%{var_name}%"
            return m.group(0)

        return re.sub(r'\$\{(\w+)\}|\$(\w+)', replace_match, command)
