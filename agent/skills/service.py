"""
Skill service for handling skill CRUD operations.

This service provides a unified interface for managing skills, which can be
called from the cloud control client (LinkAI), the local web console, or any
other management entry point.
"""

import os
import re
import shutil
import zipfile
import tempfile
from typing import Dict, List, Optional
from common.log import logger
from agent.skills.types import Skill, SkillEntry
from agent.skills.manager import CUSTOM_OVERRIDE_MARKER, SkillManager

try:
    import requests
except ImportError:
    requests = None


_SAFE_SKILL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


class SkillService:
    """
    High-level service for skill lifecycle management.
    Wraps SkillManager and provides network-aware operations such as
    downloading skill files from remote URLs.
    """

    def __init__(self, skill_manager: SkillManager):
        """
        :param skill_manager: The SkillManager instance to operate on
        """
        self.manager = skill_manager

    # ------------------------------------------------------------------
    # query
    # ------------------------------------------------------------------
    def query(self) -> List[dict]:
        """
        Query all skills and return a serialisable list.
        Reads from skills_config.json (refreshes from disk if needed).

        :return: list of skill info dicts
        """
        self.manager.refresh_skills()
        config = self.manager.get_skills_config()
        result = list(config.values())
        logger.info(f"[SkillService] query: {len(result)} skills found")
        return result

    # ------------------------------------------------------------------
    # add / install
    # ------------------------------------------------------------------
    def add(self, payload: dict) -> None:
        """
        Add (install) a skill from a remote payload.

        Supported payload types:

        1. ``type: "url"`` – download individual files::

            {
                "name": "web_search",
                "type": "url",
                "enabled": true,
                "files": [
                    {"url": "https://...", "path": "README.md"},
                    {"url": "https://...", "path": "scripts/main.py"}
                ]
            }

        2. ``type: "package"`` – download a zip archive and extract::

            {
                "name": "plugin-custom-tool",
                "type": "package",
                "category": "skills",
                "enabled": true,
                "files": [{"url": "https://cdn.example.com/skills/custom-tool.zip"}]
            }

        :param payload: skill add payload from server
        """
        name = payload.get("name")
        if not name:
            raise ValueError("skill name is required")
        self._validate_skill_name(name)
        self._ensure_skill_mutation_allowed("add", payload)

        skill_dir = self._skill_dir(name)
        if os.path.exists(skill_dir) and not payload.get("replace"):
            raise ValueError(f"skill '{name}' already exists; set replace=true to overwrite")

        if self._is_builtin_skill(name) and not payload.get("allow_builtin_override"):
            raise ValueError(
                f"skill '{name}' is built in; explicit allow_builtin_override=true is required "
                "to install a workspace overlay"
            )

        payload_type = payload.get("type", "url")

        if payload_type == "package":
            self._add_package(name, payload)
        else:
            self._add_url(name, payload)

        if self._is_builtin_skill(name) and payload.get("allow_builtin_override"):
            self._write_custom_override_marker(name)

        self.manager.refresh_skills()

        category = payload.get("category")
        if category and name in self.manager.skills_config:
            self.manager.skills_config[name]["category"] = category
            self.manager._save_skills_config()

    def _add_url(self, name: str, payload: dict) -> None:
        """Install a skill by downloading individual files."""
        files = payload.get("files", [])
        if not files:
            raise ValueError("skill files list is empty")

        skill_dir = self._skill_dir(name)

        tmp_dir = skill_dir + ".tmp"
        if os.path.exists(tmp_dir):
            shutil.rmtree(tmp_dir)
        os.makedirs(tmp_dir, exist_ok=True)

        try:
            for file_info in files:
                url = file_info.get("url")
                rel_path = file_info.get("path")
                if not url or not rel_path:
                    logger.warning(f"[SkillService] add: skip invalid file entry {file_info}")
                    continue
                dest = self._safe_join(tmp_dir, rel_path)
                self._download_file(url, dest)
        except Exception:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            raise

        if os.path.exists(skill_dir):
            shutil.rmtree(skill_dir)
        os.rename(tmp_dir, skill_dir)

        logger.info(f"[SkillService] add: skill '{name}' installed via url ({len(files)} files)")

    def _add_package(self, name: str, payload: dict) -> None:
        """
        Install a skill by downloading a zip archive and extracting it.

        If the archive contains a single top-level directory, that directory
        is used as the skill folder directly; otherwise a new directory named
        after the skill is created to hold the extracted contents.
        """
        files = payload.get("files", [])
        if not files or not files[0].get("url"):
            raise ValueError("package url is required")

        url = files[0]["url"]
        skill_dir = self._skill_dir(name)

        with tempfile.TemporaryDirectory() as tmp_dir:
            zip_path = os.path.join(tmp_dir, "package.zip")
            self._download_file(url, zip_path)

            if not zipfile.is_zipfile(zip_path):
                raise ValueError(f"downloaded file is not a valid zip archive: {url}")

            extract_dir = os.path.join(tmp_dir, "extracted")
            with zipfile.ZipFile(zip_path, "r") as zf:
                self._safe_extract_zip(zf, extract_dir)

            # Determine the actual content root.
            # If the zip has a single top-level directory, use its contents
            # so the skill folder is clean (no extra nesting).
            top_items = [
                item for item in os.listdir(extract_dir)
                if not item.startswith(".")
            ]
            if len(top_items) == 1:
                single = os.path.join(extract_dir, top_items[0])
                if os.path.isdir(single):
                    extract_dir = single

            if os.path.exists(skill_dir):
                shutil.rmtree(skill_dir)
            shutil.copytree(extract_dir, skill_dir)

        logger.info(f"[SkillService] add: skill '{name}' installed via package ({url})")

    # ------------------------------------------------------------------
    # open / close (enable / disable)
    # ------------------------------------------------------------------
    def open(self, payload: dict) -> None:
        """
        Enable a skill by name.

        :param payload: {"name": "skill_name"}
        """
        name = payload.get("name")
        if not name:
            raise ValueError("skill name is required")
        self._validate_skill_name(name)
        self._ensure_skill_mutation_allowed("open", payload)
        self.manager.set_skill_enabled(name, enabled=True)
        logger.info(f"[SkillService] open: skill '{name}' enabled")

    def close(self, payload: dict) -> None:
        """
        Disable a skill by name.

        :param payload: {"name": "skill_name"}
        """
        name = payload.get("name")
        if not name:
            raise ValueError("skill name is required")
        self._validate_skill_name(name)
        self._ensure_skill_mutation_allowed("close", payload)
        self.manager.set_skill_enabled(name, enabled=False)
        logger.info(f"[SkillService] close: skill '{name}' disabled")

    # ------------------------------------------------------------------
    # delete
    # ------------------------------------------------------------------
    def delete(self, payload: dict) -> None:
        """
        Delete a skill by removing its directory entirely.

        :param payload: {"name": "skill_name"}
        """
        name = payload.get("name")
        if not name:
            raise ValueError("skill name is required")
        self._validate_skill_name(name)
        self._ensure_skill_mutation_allowed("delete", payload)

        skill_dir = self._skill_dir(name)
        if os.path.exists(skill_dir):
            shutil.rmtree(skill_dir)
            logger.info(f"[SkillService] delete: removed directory {skill_dir}")
        else:
            logger.warning(f"[SkillService] delete: skill directory not found: {skill_dir}")

        # Refresh will remove the deleted skill from config automatically
        self.manager.refresh_skills()
        logger.info(f"[SkillService] delete: skill '{name}' deleted")

    # ------------------------------------------------------------------
    # dispatch - single entry point for protocol messages
    # ------------------------------------------------------------------
    def dispatch(self, action: str, payload: Optional[dict] = None) -> dict:
        """
        Dispatch a skill management action and return a protocol-compatible
        response dict.

        :param action: one of query / add / open / close / delete
        :param payload: action-specific payload (may be None for query)
        :return: dict with action, code, message, payload
        """
        payload = payload or {}
        try:
            if action == "query":
                result_payload = self.query()
                return {"action": action, "code": 200, "message": "success", "payload": result_payload}
            elif action == "add":
                self.add(payload)
            elif action == "open":
                self.open(payload)
            elif action == "close":
                self.close(payload)
            elif action == "delete":
                self.delete(payload)
            else:
                return {"action": action, "code": 400, "message": f"unknown action: {action}", "payload": None}
            return {"action": action, "code": 200, "message": "success", "payload": None}
        except Exception as e:
            logger.error(f"[SkillService] dispatch error: action={action}, error={e}")
            return {"action": action, "code": 500, "message": str(e), "payload": None}

    # ------------------------------------------------------------------
    # internal helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _validate_skill_name(name: str):
        if not isinstance(name, str) or not _SAFE_SKILL_NAME.match(name):
            raise ValueError("invalid skill name")
        if name in {".", ".."} or os.sep in name or (os.altsep and os.altsep in name):
            raise ValueError("invalid skill name")
        if name.endswith((".", " ")):
            raise ValueError("invalid skill name")
        first_component = name.split(".", 1)[0].upper()
        if first_component in _WINDOWS_RESERVED_NAMES:
            raise ValueError("invalid skill name")

    def _skill_dir(self, name: str) -> str:
        self._validate_skill_name(name)
        return self._safe_join(self.manager.custom_dir, name)

    def _is_builtin_skill(self, name: str) -> bool:
        try:
            candidate = os.path.abspath(os.path.join(self.manager.builtin_dir, name))
            root = os.path.abspath(self.manager.builtin_dir)
            return os.path.commonpath([root, candidate]) == root and os.path.isdir(candidate)
        except Exception:
            return False

    def _write_custom_override_marker(self, name: str) -> None:
        marker_path = os.path.join(self._skill_dir(name), CUSTOM_OVERRIDE_MARKER)
        try:
            with open(marker_path, "w", encoding="utf-8") as handle:
                handle.write(
                    "This workspace skill intentionally overrides an EcoreX built-in skill.\n"
                    "Remove this file to allow managed built-in refresh on future releases.\n"
                )
        except Exception as exc:
            logger.warning(f"[SkillService] Failed to write override marker for '{name}': {exc}")

    def _ensure_skill_mutation_allowed(self, action: str, payload: dict) -> None:
        try:
            from common.ecorex_tool_permissions import get_tool_permission_broker

            decision = get_tool_permission_broker().authorize_noninteractive(
                "skill_write",
                {
                    "action": action,
                    "name": payload.get("name"),
                    "type": payload.get("type"),
                    "category": payload.get("category"),
                },
            )
            if not decision.get("allowed", False):
                raise PermissionError(
                    decision.get("reason")
                    or "Current permission mode blocks skill modifications."
                )
        except PermissionError:
            raise
        except Exception as exc:
            logger.warning(f"[SkillService] permission broker unavailable; skill mutation blocked: {exc}")
            raise PermissionError("Permission broker unavailable; skill modification blocked.")

    @staticmethod
    def _safe_join(root: str, relative_path: str) -> str:
        if not isinstance(relative_path, str) or not relative_path.strip():
            raise ValueError("invalid relative path")
        normalized = relative_path.replace("\\", os.sep).replace("/", os.sep)
        if os.path.isabs(normalized):
            raise ValueError("absolute paths are not allowed")
        parts = [part for part in normalized.split(os.sep) if part]
        if any(part in {".", ".."} for part in parts):
            raise ValueError("path traversal is not allowed")

        root_abs = os.path.abspath(root)
        candidate = os.path.abspath(os.path.join(root_abs, *parts))
        try:
            common = os.path.commonpath([root_abs, candidate])
        except ValueError:
            raise ValueError("path escapes skill root")
        if common != root_abs:
            raise ValueError("path escapes skill root")
        return candidate

    def _safe_extract_zip(self, zf: zipfile.ZipFile, extract_dir: str):
        os.makedirs(extract_dir, exist_ok=True)
        for member in zf.infolist():
            member_path = member.filename.replace("\\", "/")
            if not member_path or member_path.endswith("/"):
                continue
            if member_path.startswith("/") or member_path.startswith("\\"):
                raise ValueError("zip archive contains an absolute path")
            dest = self._safe_join(extract_dir, member_path)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with zf.open(member, "r") as src, open(dest, "wb") as out:
                shutil.copyfileobj(src, out)

    @staticmethod
    def _download_file(url: str, dest: str):
        """
        Download a file from *url* and save to *dest*.

        :param url: remote file URL
        :param dest: local destination path
        """
        if requests is None:
            raise RuntimeError("requests library is required for downloading skill files")

        dest_dir = os.path.dirname(dest)
        if dest_dir:
            os.makedirs(dest_dir, exist_ok=True)

        resp = requests.get(url, timeout=60)
        resp.raise_for_status()
        with open(dest, "wb") as f:
            f.write(resp.content)
        logger.debug(f"[SkillService] downloaded {url} -> {dest}")
