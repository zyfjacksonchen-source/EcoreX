import importlib
import importlib.util
import threading
import time
from pathlib import Path
from typing import Dict, Any, Type, Optional
from agent.tools.base_tool import BaseTool
from common.log import logger
from config import conf


def _normalize_mcp_configs(raw) -> list:
    """
    Convert MCP server config to internal list format.
    Supports:
      - list format (mcp_servers):  [{"name": "x", "type": "stdio", ...}]
      - dict format (mcpServers):   {"x": {"command": "npx", ...}}
    """
    if isinstance(raw, list):
        return [item for item in raw if item.get("enabled", True) is not False]
    if isinstance(raw, dict):
        result = []
        for name, cfg in raw.items():
            if not isinstance(cfg, dict) or cfg.get("enabled", True) is False:
                continue
            entry = {"name": name, **cfg}
            if "type" not in entry:
                entry["type"] = "sse" if "url" in entry else "stdio"
            result.append(entry)
        return result
    return []


class ToolManager:
    """
    Tool manager for managing tools.
    """
    _instance = None
    _workspace_instances: dict[Path, "ToolManager"] = {}
    _instance_lock = threading.Lock()

    def __new__(cls, workspace_root=None, *, mcp_oauth_redirect_uri=None):
        """Keep one tool/MCP lifecycle per resolved workspace."""
        del mcp_oauth_redirect_uri
        if workspace_root is None:
            with cls._instance_lock:
                if cls._instance is None:
                    cls._instance = super(ToolManager, cls).__new__(cls)
                    cls._instance.tool_classes = {}
                    cls._instance._initialized = False
                return cls._instance

        root = Path(workspace_root).expanduser().resolve()
        with cls._instance_lock:
            instance = cls._workspace_instances.get(root)
            if instance is None:
                instance = super(ToolManager, cls).__new__(cls)
                instance.tool_classes = {}
                instance._initialized = False
                cls._workspace_instances[root] = instance
            return instance

    def __init__(self, workspace_root=None, *, mcp_oauth_redirect_uri=None):
        # Initialize only once
        if not hasattr(self, 'tool_classes'):
            self.tool_classes = {}  # Dictionary to store tool classes
        if not hasattr(self, '_mcp_registry'):
            self._mcp_registry = None  # Lazy init: only created when MCP servers are configured
        if not hasattr(self, '_mcp_tool_instances'):
            self._mcp_tool_instances: dict = {}  # tool_name -> McpTool instance
        if not hasattr(self, '_mcp_lock'):
            # Guards _mcp_loaded check-then-set so concurrent callers
            # don't trigger duplicate background loaders.
            self._mcp_lock = threading.Lock()
        if not hasattr(self, '_mcp_loaded'):
            # Idempotency flag. Flipped to True the moment the first loader
            # is dispatched (synchronously, inside _mcp_lock). Subsequent
            # _load_mcp_tools() calls become no-ops, so per-session agent
            # initialization never re-forks MCP subprocesses.
            self._mcp_loaded = False
        if not hasattr(self, '_mcp_status'):
            # server_name -> "pending" / "ready" / "failed"
            # Useful for UI / introspection while async loading is in progress.
            self._mcp_status: dict = {}
        if not hasattr(self, '_mcp_signature'):
            # (mtime, sha256) of mcp.json the last time we loaded.
            # Used by refresh_mcp_if_changed() to skip re-parsing when nothing changed.
            self._mcp_signature: tuple = (None, None)
        if not hasattr(self, '_mcp_active_configs'):
            # server_name -> normalized config dict, for diff-based reload.
            self._mcp_active_configs: dict = {}
        if not hasattr(self, '_registry_errors'):
            self._registry_errors: list = []
        if not hasattr(self, '_missing_configured_tools'):
            self._missing_configured_tools: list = []
        if mcp_oauth_redirect_uri is not None:
            self.mcp_oauth_redirect_uri = str(mcp_oauth_redirect_uri)
        if workspace_root is not None:
            self.bind_workspace(workspace_root)

    def bind_workspace(self, workspace_root) -> None:
        """Bind MCP discovery to the same workspace as the current Agent."""

        root = Path(workspace_root).expanduser().resolve()
        if getattr(self, "workspace_root", None) == root:
            return
        if getattr(self, "workspace_root", None) is not None:
            raise RuntimeError(
                "ToolManager is workspace-bound; create the manager for the target workspace"
            )
        self.workspace_root = root
        self._mcp_registry = None
        self._mcp_tool_instances = {}
        self._mcp_loaded = False
        self._mcp_status = {}
        self._mcp_signature = (None, None)
        self._mcp_active_configs = {}

    def _record_registry_error(self, source: str, exc_or_message: Any) -> None:
        if isinstance(exc_or_message, BaseException):
            error_type = exc_or_message.__class__.__name__
            message = str(exc_or_message)
        else:
            error_type = "Error"
            message = str(exc_or_message)
        entry = {
            "source": str(source or "unknown"),
            "errorType": error_type,
            "message": message,
        }
        if entry not in self._registry_errors:
            self._registry_errors.append(entry)
        if len(self._registry_errors) > 50:
            self._registry_errors = self._registry_errors[-50:]

    def load_tools(self, tools_dir: str = "", config_dict=None, *, start_mcp: Optional[bool] = None):
        """
        Load tools from both directory and configuration.

        :param tools_dir: Directory to scan for tool modules
        """
        if tools_dir:
            self._load_tools_from_directory(tools_dir)
            self._configure_tools_from_config()
        else:
            self._load_tools_from_init()
            self._configure_tools_from_config(config_dict)

        should_start_mcp = conf().get("mcp_auto_start", False) if start_mcp is None else bool(start_mcp)
        if should_start_mcp:
            self._load_mcp_tools()
        else:
            logger.info("[ToolManager] MCP auto-start disabled for this load; configured MCP servers can still be started by runtime discovery")

    def _load_tools_from_init(self) -> bool:
        """
        Load tool classes from tools.__init__.__all__

        :return: True if tools were loaded, False otherwise
        """
        try:
            # Try to import the tools package
            tools_package = importlib.import_module("agent.tools")

            # Check if __all__ is defined
            if hasattr(tools_package, "__all__"):
                tool_classes = tools_package.__all__

                # Import each tool class directly from the tools package
                for class_name in tool_classes:
                    try:
                        # Skip base classes
                        if class_name in ["BaseTool", "ToolManager"]:
                            continue

                        # Get the class directly from the tools package
                        if hasattr(tools_package, class_name):
                            cls = getattr(tools_package, class_name)

                            if (
                                    isinstance(cls, type)
                                    and issubclass(cls, BaseTool)
                                    and cls != BaseTool
                            ):
                                try:
                                    # Skip tools that need special initialization
                                    if class_name in ["MemorySearchTool", "MemoryGetTool"]:
                                        logger.debug(f"Skipped tool {class_name} (requires memory_manager)")
                                        continue
                                    # McpTool instances are registered dynamically via _load_mcp_tools()
                                    if class_name == "McpTool":
                                        logger.debug(f"Skipped tool {class_name} (registered dynamically via mcp_servers config)")
                                        continue
                                    
                                    # Create a temporary instance to get the name
                                    temp_instance = cls()
                                    tool_name = temp_instance.name
                                    # Store the class, not the instance
                                    self.tool_classes[tool_name] = cls
                                    logger.debug(f"Loaded tool: {tool_name} from class {class_name}")
                                except ImportError as e:
                                    self._record_registry_error(class_name, e)
                                    # Handle missing dependencies with helpful messages
                                    error_msg = str(e)
                                    if "playwright" in error_msg:
                                        logger.warning(
                                            "[ToolManager] Browser tool not loaded - missing dependencies.\n"
                                            "  To enable browser tool, run:\n"
                                            "    pip install playwright\n"
                                            "    playwright install chromium"
                                        )
                                    elif "markdownify" in error_msg:
                                        logger.warning(
                                            f"[ToolManager] {cls.__name__} not loaded - missing markdownify.\n"
                                            f"  Install with: pip install markdownify"
                                        )
                                    else:
                                        logger.warning(f"[ToolManager] {cls.__name__} not loaded due to missing dependency: {error_msg}")
                                except Exception as e:
                                    self._record_registry_error(class_name, e)
                                    logger.error(f"Error initializing tool class {cls.__name__}: {e}")
                    except Exception as e:
                        self._record_registry_error(class_name, e)
                        logger.error(f"Error importing class {class_name}: {e}")

                return len(self.tool_classes) > 0
            return False
        except ImportError as e:
            self._record_registry_error("agent.tools", e)
            logger.warning("Could not import agent.tools package")
            return False
        except Exception as e:
            self._record_registry_error("agent.tools", e)
            logger.error(f"Error loading tools from __init__.__all__: {e}")
            return False

    def _load_tools_from_directory(self, tools_dir: str):
        """Dynamically load tool classes from directory"""
        tools_path = Path(tools_dir)

        # Traverse all .py files
        for py_file in tools_path.rglob("*.py"):
            # Skip initialization files and base tool files
            if py_file.name in ["__init__.py", "base_tool.py", "tool_manager.py"]:
                continue

            # Get module name
            module_name = py_file.stem

            try:
                # Load module directly from file
                spec = importlib.util.spec_from_file_location(module_name, py_file)
                if spec and spec.loader:
                    module = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(module)

                    # Find tool classes in the module
                    for attr_name in dir(module):
                        cls = getattr(module, attr_name)
                        if (
                                isinstance(cls, type)
                                and issubclass(cls, BaseTool)
                                and cls != BaseTool
                        ):
                            try:
                                # Skip memory tools (they need special initialization with memory_manager)
                                if attr_name in ["MemorySearchTool", "MemoryGetTool"]:
                                    logger.debug(f"Skipped tool {attr_name} (requires memory_manager)")
                                    continue
                                
                                # Create a temporary instance to get the name
                                temp_instance = cls()
                                tool_name = temp_instance.name
                                # Store the class, not the instance
                                self.tool_classes[tool_name] = cls
                            except ImportError as e:
                                self._record_registry_error(attr_name, e)
                                # Handle missing dependencies with helpful messages
                                error_msg = str(e)
                                if "playwright" in error_msg:
                                    logger.warning(
                                        "[ToolManager] Browser tool not loaded - missing dependencies.\n"
                                        "  To enable browser tool, run:\n"
                                        "    pip install playwright\n"
                                        "    playwright install chromium"
                                    )
                                elif "markdownify" in error_msg:
                                    logger.warning(
                                        f"[ToolManager] {cls.__name__} not loaded - missing markdownify.\n"
                                        f"  Install with: pip install markdownify"
                                    )
                                else:
                                    logger.warning(f"[ToolManager] {cls.__name__} not loaded due to missing dependency: {error_msg}")
                            except Exception as e:
                                self._record_registry_error(attr_name, e)
                                logger.error(f"Error initializing tool class {cls.__name__}: {e}")
            except Exception as e:
                self._record_registry_error(str(py_file), e)
                print(f"Error importing module {py_file}: {e}")

    def _configure_tools_from_config(self, config_dict=None):
        """Configure tool classes based on configuration file"""
        try:
            # Get tools configuration
            tools_config = config_dict or conf().get("tools", {})

            # Record tools that are configured but not loaded
            missing_tools = []

            # Store configurations for later use when instantiating
            self.tool_configs = tools_config

            # Check which configured tools are missing
            for tool_name in tools_config:
                if tool_name not in self.tool_classes:
                    missing_tools.append(tool_name)

            # If there are missing tools, record warnings
            self._missing_configured_tools = list(missing_tools)
            if missing_tools:
                for tool_name in missing_tools:
                    if tool_name == "browser":
                        logger.warning(
                            "[ToolManager] Browser tool is configured but not loaded.\n"
                            "  To enable browser tool, run:\n"
                            "    pip install playwright\n"
                            "  Only run `playwright install chromium` when CDP fallback is required."
                        )
                    elif tool_name == "google_search":
                        logger.warning(
                            "[ToolManager] Google Search tool is configured but may need API key.\n"
                            "  Get API key from: https://serper.dev\n"
                            "  Configure in config.json: tools.google_search.api_key"
                        )
                    else:
                        logger.warning(f"[ToolManager] Tool '{tool_name}' is configured but could not be loaded.")

        except Exception as e:
            self._record_registry_error("tool_config", e)
            logger.error(f"Error configuring tools from config: {e}")

    def _mcp_json_path(self) -> str:
        import os
        workspace = str(
            getattr(
                self,
                "workspace_root",
                Path(os.path.expanduser(conf().get("agent_workspace", "~/cow"))).resolve(),
            )
        )
        return os.path.join(workspace, "mcp.json")

    def _read_mcp_json_signature(self):
        """
        Return (mtime, sha256_of_bytes) for ~/cow/mcp.json without parsing.
        Returns (None, None) if the file doesn't exist or is unreadable.
        Cheap enough (one stat + one small read) to call on every agent init.
        """
        import os
        import hashlib
        path = self._mcp_json_path()
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            return (None, None)
        try:
            with open(path, "rb") as f:
                digest = hashlib.sha256(f.read()).hexdigest()
        except OSError:
            return (mtime, None)
        return (mtime, digest)

    def _load_mcp_configs(self) -> list:
        """Load MCP servers only from the currently bound workspace."""
        return self._load_workspace_mcp_configs() or []

    def _load_workspace_mcp_configs(self) -> Optional[list]:
        """Load only the workspace MCP config, returning None when no file exists or parsing fails."""
        import os
        import json as _json

        mcp_json_path = self._mcp_json_path()
        if not os.path.exists(mcp_json_path):
            return None
        try:
            with open(mcp_json_path, "r", encoding="utf-8") as f:
                data = _json.load(f)
            if "mcpServers" in data:
                raw = data["mcpServers"]
            elif "mcp_servers" in data:
                raw = data["mcp_servers"]
            else:
                raw = data
            logger.info(f"[ToolManager] Loading MCP config from {mcp_json_path}")
            return _normalize_mcp_configs(raw)
        except Exception as e:
            logger.warning(f"[ToolManager] Failed to read {mcp_json_path}: {e}")
            return []

    def has_mcp_configured(self, *, include_config_fallback: bool = False) -> bool:
        """Return True only when the current workspace configures an MCP server."""
        del include_config_fallback
        try:
            return bool(self._load_workspace_mcp_configs())
        except Exception as e:
            logger.debug(f"[ToolManager] MCP config probe failed: {e}")
            return False

    def ensure_mcp_configured_loaded(
        self,
        *,
        wait_seconds: float = 0.0,
        poll_interval_seconds: float = 0.1,
        server_name: Optional[str] = None,
    ) -> dict:
        """
        Start or refresh configured MCP servers and return a bounded status snapshot.

        This is intentionally separate from load_tools(start_mcp=...). Runtime
        surfaces that must discover already-configured connectors can call this
        without changing every generic ToolManager load into a blocking startup.
        """
        if getattr(self, "_mcp_loaded", False):
            self.refresh_mcp_if_changed()
        else:
            should_start = self.has_mcp_configured()
            if should_start:
                self._load_mcp_tools()

        deadline = time.time() + max(0.0, float(wait_seconds or 0.0))
        while wait_seconds and time.time() < deadline:
            status = self.list_mcp_status()
            if not status:
                break
            if server_name:
                target_status = str(status.get(server_name) or "")
                if target_status and target_status != "pending":
                    break
                if any(getattr(tool, "server_name", "") == server_name for tool in self._mcp_tool_instances.values()):
                    break
            elif not any(str(value) == "pending" for value in status.values()):
                break
            time.sleep(max(0.02, float(poll_interval_seconds or 0.1)))

        statuses = self.list_mcp_status()
        return {
            "status": statuses,
            "configured": bool(statuses) or self.has_mcp_configured(),
            "toolCount": len(self._mcp_tool_instances),
        }

    def _load_mcp_tools(self):
        """
        Trigger MCP tool loading in a background thread (idempotent).

        Returns immediately. Booting MCP servers (npx, uvx, etc.) takes
        seconds to tens of seconds on first run, which would otherwise
        block agent initialization and the user's first message.
        Built-in tools work fine without MCP, so we let the agent serve
        traffic right away and let MCP servers come online in the
        background. Per-session agents read a snapshot of whatever is
        ready at construction time and gracefully ignore the rest.
        """
        with self._mcp_lock:
            if self._mcp_loaded:
                return
            mcp_servers_config = self._load_mcp_configs()
            # Snapshot the signature now so future refresh_mcp_if_changed()
            # calls can short-circuit when nothing has changed on disk.
            self._mcp_signature = self._read_mcp_json_signature()
            self._mcp_active_configs = {
                cfg.get("name", "<unnamed>"): cfg for cfg in mcp_servers_config
            }
            if not mcp_servers_config:
                # Mark as loaded even when there is nothing to load,
                # so we don't re-read the config file on every call.
                self._mcp_loaded = True
                return

            # Mark pending immediately so list_mcp_status() callers see
            # the in-progress state instead of an empty dict.
            for cfg in mcp_servers_config:
                name = cfg.get("name", "<unnamed>")
                self._mcp_status[name] = "pending"

            self._mcp_loaded = True
            threading.Thread(
                target=self._load_mcp_tools_async,
                args=(mcp_servers_config,),
                daemon=True,
                name="mcp-loader",
            ).start()
            logger.info(
                f"[ToolManager] MCP loading started in background "
                f"({len(mcp_servers_config)} server(s) configured)"
            )

    def refresh_mcp_if_changed(self):
        """
        Cheap check whether ~/cow/mcp.json has changed since last load.
        If it has, do a diff-based reload: start newly added servers,
        shut down removed ones, and restart any whose config was edited.
        Untouched servers are left running.

        Designed to be called on every agent creation. The fast path is
        a single os.stat() — completely free when nothing has changed.
        """
        with self._mcp_lock:
            new_sig = self._read_mcp_json_signature()
            if new_sig == self._mcp_signature:
                return  # no-op fast path

            try:
                new_configs = self._load_mcp_configs()
            except Exception as e:
                logger.warning(f"[ToolManager] MCP reload — failed to parse config: {e}")
                return

            new_by_name = {
                cfg.get("name", "<unnamed>"): cfg for cfg in new_configs
            }
            old_by_name = self._mcp_active_configs

            added = [n for n in new_by_name if n not in old_by_name]
            removed = [n for n in old_by_name if n not in new_by_name]
            changed = [
                n for n in new_by_name
                if n in old_by_name and new_by_name[n] != old_by_name[n]
            ]

            if not (added or removed or changed):
                # Signature drifted but content is logically identical
                # (e.g. user re-saved the file without edits). Just sync.
                self._mcp_signature = new_sig
                return

            logger.info(
                f"[ToolManager] mcp.json changed — "
                f"adding={added}, removing={removed}, restarting={changed}"
            )

            # Tear down removed + changed servers (changed ones get restarted below)
            for name in removed + changed:
                self._teardown_mcp_server(name)

            # Spin up newly added + changed servers in the background
            to_start = [new_by_name[n] for n in added + changed]
            if to_start:
                for cfg in to_start:
                    self._mcp_status[cfg.get("name", "<unnamed>")] = "pending"
                threading.Thread(
                    target=self._load_mcp_tools_async,
                    args=(to_start,),
                    daemon=True,
                    name="mcp-loader-reload",
                ).start()

            self._mcp_active_configs = new_by_name
            self._mcp_signature = new_sig

    def _teardown_mcp_server(self, server_name: str):
        """Shut down one MCP server and drop its tools from the registry."""
        if self._mcp_registry is None:
            return
        client = None
        with self._mcp_registry._registry_lock:
            client = self._mcp_registry._clients.pop(server_name, None)
        if client is not None:
            try:
                client.shutdown()
            except Exception as e:
                logger.warning(f"[MCP] Error shutting down '{server_name}': {e}")
        # Drop tools that belonged to this server.
        for tool_name in list(self._mcp_tool_instances.keys()):
            tool = self._mcp_tool_instances.get(tool_name)
            if tool is not None and getattr(tool, "server_name", None) == server_name:
                self._mcp_tool_instances.pop(tool_name, None)
        self._mcp_status.pop(server_name, None)

    def _load_mcp_tools_async(self, mcp_servers_config):
        """
        Background worker: bring up each MCP server one-by-one and
        publish ready tools to _mcp_tool_instances as they come online.

        Server failures are isolated — one bad server cannot block
        the others, and never raises out of the worker thread.
        """
        try:
            from agent.tools.mcp.mcp_client import (
                McpClient,
                McpClientRegistry,
            )
            from agent.tools.mcp.mcp_tool import McpTool

            registry = self._mcp_registry or McpClientRegistry()
            self._mcp_registry = registry

            for cfg in mcp_servers_config:
                server_name = cfg.get("name", "<unnamed>")
                try:
                    client = McpClient(
                        cfg,
                        oauth_redirect_uri=getattr(
                            self, "mcp_oauth_redirect_uri", None
                        ),
                        reload_callback=self.reload_mcp_server,
                        workspace_identity=(
                            str(self.workspace_root)
                            if getattr(self, "workspace_root", None) is not None
                            else None
                        ),
                    )
                    if not client.initialize():
                        if getattr(client, "needs_auth", False):
                            with registry._registry_lock:
                                registry._clients[server_name] = client
                            self._mcp_status[server_name] = "needs_auth"
                            logger.info(
                                f"[MCP] Server '{server_name}' needs authorization"
                            )
                        else:
                            self._mcp_status[server_name] = "failed"
                            logger.warning(
                                f"[MCP] Server '{server_name}' failed to initialize — skipping"
                            )
                        continue

                    tool_schemas = client.list_tools()
                    added = []
                    for schema in tool_schemas:
                        tool_name = schema.get("name", "")
                        if not tool_name:
                            continue
                        mcp_tool = McpTool(client, schema, server_name)
                        # Atomic dict assignment is GIL-safe; readers iterate
                        # over a list() snapshot to avoid concurrent mutation.
                        self._mcp_tool_instances[tool_name] = mcp_tool
                        added.append(tool_name)

                    # Register client into the shared registry only after its
                    # tools are visible, so callers never see a half-loaded server.
                    with registry._registry_lock:
                        registry._clients[server_name] = client
                    self._mcp_status[server_name] = "ready"
                    logger.info(
                        f"[MCP] Server '{server_name}' ready — "
                        f"{len(added)} tool(s): {added}"
                    )
                except Exception as e:
                    self._mcp_status[server_name] = "failed"
                    logger.warning(f"[MCP] Server '{server_name}' load failed: {e}")

            ready = sum(1 for s in self._mcp_status.values() if s == "ready")
            total = len(self._mcp_status)
            logger.info(
                f"[ToolManager] MCP loading complete: "
                f"{ready}/{total} server(s) ready, "
                f"{len(self._mcp_tool_instances)} tool(s) available"
            )
        except Exception as e:
            logger.warning(f"[ToolManager] MCP background loader crashed: {e}")

    def reload_mcp_server(self, server_name: str) -> None:
        """Restart one configured MCP server after OAuth or config refresh."""

        with self._mcp_lock:
            config = self._mcp_active_configs.get(server_name)
        if not config:
            logger.warning(f"[MCP] reload requested for unknown server '{server_name}'")
            return
        self._teardown_mcp_server(server_name)
        self._mcp_status[server_name] = "pending"
        threading.Thread(
            target=self._load_mcp_tools_async,
            args=([config],),
            daemon=True,
            name=f"mcp-reload-{server_name}",
        ).start()

    def list_mcp_status(self) -> dict:
        """Return {server_name: status} snapshot for UI / debugging."""
        return dict(self._mcp_status)

    def sync_mcp_into_agent(self, agent) -> tuple:
        """
        Reconcile a live agent's tool collection with the current MCP tool registry.

        Adds tools that finished loading after the agent was created,
        and removes tools whose MCP server was torn down. Built-in tools
        on the agent are left untouched.

        Handles both representations CowAgent uses:
          - Agent.tools: list[BaseTool]               (default Agent class)
          - AgentStream.tools: dict[str, BaseTool]    (streaming agent)

        Returns (added_names, removed_names) for logging.
        """
        if agent is None or not hasattr(agent, "tools"):
            return ([], [])

        if getattr(agent, "_evolution_restricted", False) or getattr(
            getattr(agent, "agent", None), "_evolution_restricted", False
        ):
            return ([], [])

        from agent.tools.mcp.mcp_tool import McpTool
        current = self._mcp_tool_instances
        registry_names = set(current.keys())

        agent_tools = agent.tools

        if isinstance(agent_tools, dict):
            agent_mcp_names = {
                name for name, tool in agent_tools.items()
                if isinstance(tool, McpTool)
            }
            added = registry_names - agent_mcp_names
            removed = agent_mcp_names - registry_names
            if not (added or removed):
                return ([], [])
            for name in added:
                existing = agent_tools.get(name)
                if existing is not None and not isinstance(existing, McpTool):
                    logger.warning(
                        f"[MCP] Refusing to replace first-party tool '{name}' "
                        f"with MCP tool from {getattr(current[name], 'server_name', 'unknown')}"
                    )
                    continue
                agent_tools[name] = current[name]
            for name in removed:
                agent_tools.pop(name, None)

        elif isinstance(agent_tools, list):
            agent_mcp_names = {
                t.name for t in agent_tools if isinstance(t, McpTool)
            }
            added = registry_names - agent_mcp_names
            removed = agent_mcp_names - registry_names
            if not (added or removed):
                return ([], [])
            if removed:
                agent.tools = [
                    t for t in agent_tools
                    if not (isinstance(t, McpTool) and t.name in removed)
                ]
            for name in added:
                agent.tools.append(current[name])

        else:
            return ([], [])

        return (sorted(added), sorted(removed))

    def create_tool(self, name: str) -> BaseTool:
        """
        Get a new instance of a tool by name.

        :param name: The name of the tool to get.
        :return: A new instance of the tool or None if not found.
        """
        tool_class = self.tool_classes.get(name)
        if tool_class:
            # Create a new instance
            tool_instance = tool_class()

            # Apply configuration if available
            if hasattr(self, 'tool_configs') and name in self.tool_configs:
                apply_config = getattr(tool_instance, "apply_config", None)
                if callable(apply_config):
                    apply_config(self.tool_configs[name])
                else:
                    tool_instance.config = self.tool_configs[name]

            return tool_instance

        # Fall back to MCP tool instances
        mcp_tool = self._mcp_tool_instances.get(name)
        if mcp_tool:
            return mcp_tool

        return None

    def list_tools(self) -> dict:
        """
        Get information about all loaded tools.

        :return: A dictionary with tool information.
        """
        result = {}
        for name, tool_class in self.tool_classes.items():
            # Create a temporary instance to get schema
            try:
                temp_instance = tool_class()
                result[name] = {
                    "description": temp_instance.description,
                    "parameters": temp_instance.get_json_schema()
                }
            except Exception as e:
                self._record_registry_error(f"schema:{name}", e)
                logger.warning(f"[ToolManager] tool schema unavailable for {name}: {e}")

        # Include MCP tool instances
        for name, mcp_tool in self._mcp_tool_instances.items():
            try:
                result[name] = {
                    "description": mcp_tool.description,
                    "parameters": mcp_tool.params,
                }
            except Exception as e:
                self._record_registry_error(f"mcp_schema:{name}", e)
                logger.warning(f"[ToolManager] MCP tool schema unavailable for {name}: {e}")

        return result

    def registry_health(self) -> dict:
        """Return a public health snapshot for tool discovery diagnostics."""
        import_errors = []
        try:
            tools_package = importlib.import_module("agent.tools")
            getter = getattr(tools_package, "get_tool_import_errors", None)
            if callable(getter):
                import_errors = getter()
        except Exception as e:
            self._record_registry_error("agent.tools.health", e)

        first_party_count = len(self.tool_classes)
        mcp_count = len(self._mcp_tool_instances)
        errors = list(self._registry_errors)
        for entry in import_errors:
            if isinstance(entry, dict):
                mapped = {
                    "source": f"{entry.get('module', '')}.{entry.get('class', '')}".strip("."),
                    "errorType": str(entry.get("errorType") or "Error"),
                    "message": str(entry.get("message") or ""),
                }
                if mapped not in errors:
                    errors.append(mapped)

        if first_party_count > 0:
            status = "ready" if not errors else "degraded"
        elif mcp_count > 0:
            status = "degraded"
        else:
            status = "error"

        return {
            "status": status,
            "firstPartyToolCount": first_party_count,
            "mcpToolCount": mcp_count,
            "totalToolCount": first_party_count + mcp_count,
            "missingConfiguredTools": list(self._missing_configured_tools),
            "errors": errors[:50],
            "mcpStatus": self.list_mcp_status(),
        }

    def shutdown_mcp(self):
        """Shut down all MCP server clients."""
        if self._mcp_registry:
            self._mcp_registry.shutdown_all()
        self._mcp_registry = None
        self._mcp_tool_instances = {}
        self._mcp_status = {}
        self._mcp_loaded = False
        self._mcp_signature = (None, None)
        self._mcp_active_configs = {}
