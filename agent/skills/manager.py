"""
Skill manager for managing skill lifecycle and operations.
"""

import os
import json
from typing import Dict, List, Optional, Set
from pathlib import Path
from common.log import logger
from agent.skills.types import Skill, SkillEntry, SkillSnapshot
from agent.skills.loader import SkillLoader
from agent.skills.formatter import format_skill_diagnostics_for_prompt, format_skill_entries_for_prompt
from agent.skills.frontmatter import get_frontmatter_value, parse_boolean_value

SKILLS_CONFIG_FILE = "skills_config.json"
CUSTOM_OVERRIDE_MARKER = ".ecorex-custom-override"

MANAGED_BUILTIN_REFRESH_MARKERS: Dict[str, List[str]] = {
    # These are official EcoreX built-ins that may have been copied into
    # ~/EcoreX/skills by older releases. If that stale copy is left in place it
    # silently overrides the fixed built-in version.
    "image-generation": [
        'DEFAULT_MODEL = "gpt-image-2-pro"',
        "OpenAI default mode starts with `gpt-image-2-pro`",
        "model_fallback",
        "LinkAI default model follows EcoreX's OpenAI image default",
        '"output_format"',
        "/images/edits",
        "requests with `image_url` use",
    ],
}


class SkillManager:
    """Manages skills for an agent."""

    def __init__(
        self,
        builtin_dir: Optional[str] = None,
        custom_dir: Optional[str] = None,
        config: Optional[Dict] = None,
    ):
        """
        Initialize the skill manager.

        :param builtin_dir: Built-in skills directory (project root ``skills/``)
        :param custom_dir: Custom skills directory (workspace ``skills/``)
        :param config: Configuration dictionary
        """
        project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        self.builtin_dir = builtin_dir or os.path.join(project_root, 'skills')
        self.custom_dir = custom_dir or os.path.join(project_root, 'workspace', 'skills')
        self.extra_dirs: List[str] = []
        self.config = config or {}
        self._skills_config_path = os.path.join(self.custom_dir, SKILLS_CONFIG_FILE)

        # skills_config: full skill metadata keyed by name
        # { "web-fetch": {"name": ..., "description": ..., "source": ..., "enabled": true}, ... }
        self.skills_config: Dict[str, dict] = {}
        self.legacy_migration_preferences: Dict[str, dict] = {}

        self.loader = SkillLoader()
        self.skills: Dict[str, SkillEntry] = {}
        self.builtin_catalog_names: Set[str] = set()
        self.last_load_diagnostics: List[str] = []

        # Load skills on initialization
        self.refresh_skills()

    def refresh_skills(self):
        """Reload all skills from builtin and custom directories, then sync config."""
        try:
            from ecorex.extensions.live_authority import live_extension_skill_roots

            live_roots = live_extension_skill_roots()
            if live_roots is not None:
                self.extra_dirs = list(live_roots)
        except Exception:
            pass
        self._refresh_managed_builtin_overlays()
        self.builtin_catalog_names = self._load_builtin_catalog_names()
        self.skills = self.loader.load_all_skills(
            builtin_dir=self.builtin_dir,
            custom_dir=self.custom_dir,
            extra_dirs=self.extra_dirs,
        )
        self.last_load_diagnostics = self.loader.get_last_diagnostics()
        self._sync_skills_config()
        logger.debug(f"SkillManager: Loaded {len(self.skills)} skills")

    def _load_builtin_catalog_names(self) -> Set[str]:
        """Snapshot names shipped in the factory built-in catalog."""
        if not self.builtin_dir or not os.path.isdir(self.builtin_dir):
            return set()
        try:
            result = self.loader.load_skills_from_dir(self.builtin_dir, source="builtin")
            if result.diagnostics:
                logger.debug(
                    f"[SkillManager] Built-in catalog diagnostics: {len(result.diagnostics)} issues"
                )
            return {skill.name for skill in result.skills}
        except Exception as exc:
            logger.warning(f"[SkillManager] Failed loading built-in skill catalog: {exc}")
            return set()

    def is_builtin_catalog_skill(self, name: str) -> bool:
        """Return True when ``name`` belongs to the shipped factory catalog."""
        return name in self.builtin_catalog_names

    def _refresh_managed_builtin_overlays(self) -> None:
        """
        Detect stale workspace copies of official built-in skills.

        Same-name workspace skills normally override built-ins. That remains
        useful for explicit user overrides, but older EcoreX builds also copied
        selected built-ins into the workspace. Starting with the follow-up
        v0.1.15 iteration, startup must not mutate or replace user/workspace
        skills. We only log diagnostics here; users can explicitly fork or
        repair skills through the registry/skill UI.
        """
        if not self.builtin_dir or not self.custom_dir:
            return

        builtin_root = Path(self.builtin_dir)
        custom_root = Path(self.custom_dir)
        if not builtin_root.exists() or not custom_root.exists():
            return

        for skill_name, markers in MANAGED_BUILTIN_REFRESH_MARKERS.items():
            builtin_skill_dir = builtin_root / skill_name
            custom_skill_dir = custom_root / skill_name
            if not builtin_skill_dir.is_dir() or not custom_skill_dir.exists():
                continue
            if (custom_skill_dir / CUSTOM_OVERRIDE_MARKER).exists():
                continue

            try:
                builtin_text = self._read_skill_tree_text(builtin_skill_dir)
                custom_text = self._read_skill_tree_text(custom_skill_dir)
            except Exception as exc:
                logger.warning(
                    f"[SkillManager] Failed checking managed skill '{skill_name}': {exc}"
                )
                continue

            if not all(marker in builtin_text for marker in markers):
                logger.warning(
                    f"[SkillManager] Built-in skill '{skill_name}' is missing managed refresh markers"
                )
                continue
            if all(marker in custom_text for marker in markers):
                continue

            logger.warning(
                f"[SkillManager] Workspace skill '{skill_name}' appears to be a stale "
                "copy of a built-in skill. It is left untouched; use the registry UI "
                "to inspect, disable, or fork/repair it explicitly."
            )

    @staticmethod
    def _read_skill_tree_text(skill_dir: Path) -> str:
        parts: List[str] = []
        for path in sorted(skill_dir.rglob("*")):
            if not path.is_file():
                continue
            if path.name == CUSTOM_OVERRIDE_MARKER:
                continue
            if path.suffix.lower() not in {".md", ".py", ".json", ".yml", ".yaml", ".txt"}:
                continue
            try:
                parts.append(path.read_text(encoding="utf-8", errors="replace"))
            except Exception:
                continue
        return "\n".join(parts)

    def get_load_diagnostics(self, limit: Optional[int] = None) -> List[str]:
        diagnostics = list(self.last_load_diagnostics or [])
        if limit is None:
            return diagnostics
        return diagnostics[:max(0, limit)]

    # ------------------------------------------------------------------
    # skills_config.json management
    # ------------------------------------------------------------------
    def _load_skills_config(self) -> Dict[str, dict]:
        """Load skills_config.json from custom_dir. Returns empty dict if not found."""
        if not os.path.exists(self._skills_config_path):
            return {}
        try:
            with open(self._skills_config_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
        except Exception as e:
            logger.warning(f"[SkillManager] Failed to load {SKILLS_CONFIG_FILE}: {e}")
        return {}

    def _save_skills_config(self):
        """Persist local Skill preferences beside the local Skill directory."""

        os.makedirs(self.custom_dir, exist_ok=True)
        try:
            with open(self._skills_config_path, "w", encoding="utf-8") as file:
                json.dump(self.skills_config, file, indent=4, ensure_ascii=False)
        except Exception as exc:
            logger.error(f"[SkillManager] Failed to save {SKILLS_CONFIG_FILE}: {exc}")

    def _sync_skills_config(self):
        """
        Build display metadata from directory-scanned skills.

        - New skills: enabled by default.
        - Legacy ``skills_config.json`` is deliberately not read here.  The
          Extension migration owns its one-time import and ExtensionService
          owns every live enablement decision afterwards.
        - name/description/source are refreshed from the latest scan.
        """
        saved = self._load_skills_config()
        self.legacy_migration_preferences = dict(saved)
        merged: Dict[str, dict] = {}

        for name, entry in self.skills.items():
            skill = entry.skill
            prev = saved.get(name, {})
            category = prev.get("category") or get_frontmatter_value(skill.frontmatter, "category") or "skill"
            builtin_catalog = self.is_builtin_catalog_skill(name) or skill.source == "builtin"
            enabled = prev.get(
                "enabled",
                entry.metadata.default_enabled if entry.metadata else True,
            )

            entry_dict = {
                "name": name,
                "description": skill.description,
                "source": skill.source,
                "enabled": bool(enabled),
                "default_enabled": True,
                "builtin_catalog": builtin_catalog,
                "category": category,
            }
            mentionable_raw = get_frontmatter_value(skill.frontmatter, "mentionable")
            if mentionable_raw is not None:
                entry_dict["mentionable"] = parse_boolean_value(mentionable_raw, default=True)
            elif "mentionable" in prev:
                entry_dict["mentionable"] = parse_boolean_value(prev.get("mentionable"), default=True)

            mention_category = (
                prev.get("mention_category")
                or get_frontmatter_value(skill.frontmatter, "mention-category")
                or get_frontmatter_value(skill.frontmatter, "mention_category")
            )
            if mention_category:
                entry_dict["mention_category"] = str(mention_category)

            mention_hidden_reason = (
                prev.get("mention_hidden_reason")
                or get_frontmatter_value(skill.frontmatter, "mention-hidden-reason")
                or get_frontmatter_value(skill.frontmatter, "mention_hidden_reason")
            )
            if mention_hidden_reason:
                entry_dict["mention_hidden_reason"] = str(mention_hidden_reason)

            display_name = prev.get("display_name")
            if display_name:
                entry_dict["display_name"] = display_name
            merged[name] = entry_dict

        self.skills_config = merged
        self._save_skills_config()

    def is_skill_enabled(self, name: str) -> bool:
        """
        Check if a skill is enabled according to skills_config.

        :param name: skill name
        :return: True if enabled (default True if not in config)
        """
        entry = self.skills_config.get(name)
        return True if entry is None else bool(entry.get("enabled", True))

    def set_skill_enabled(self, name: str, enabled: bool):
        """
        Set a skill's enabled state and persist.

        :param name: skill name
        :param enabled: True to enable, False to disable
        """
        if name not in self.skills_config:
            raise ValueError(f"skill '{name}' not found in config")
        try:
            from ecorex.extensions.live_authority import (
                live_skill_enabled,
                set_live_skill_enabled,
            )

            if live_skill_enabled(name) is not None:
                set_live_skill_enabled(name, enabled)
        except RuntimeError:
            raise
        except Exception:
            pass
        self.skills_config[name]["enabled"] = bool(enabled)
        self._save_skills_config()

    def get_skills_config(self) -> Dict[str, dict]:
        """
        Return the full skills_config dict (for query API).

        :return: copy of skills_config
        """
        return {name: dict(row) for name, row in self.skills_config.items()}
    
    def get_skill(self, name: str) -> Optional[SkillEntry]:
        """
        Get a skill by name.
        
        :param name: Skill name
        :return: SkillEntry or None if not found
        """
        return self.skills.get(name)
    
    def list_skills(self) -> List[SkillEntry]:
        """
        Get all loaded skills.
        
        :return: List of all skill entries
        """
        return list(self.skills.values())
    
    @staticmethod
    def _normalize_skill_filter(skill_filter: Optional[List[str]]) -> Optional[List[str]]:
        """Normalize a skill_filter list into a flat list of stripped names."""
        if skill_filter is None:
            return None
        normalized = []
        for item in skill_filter:
            if isinstance(item, str):
                name = item.strip()
                if name:
                    normalized.append(name)
            elif isinstance(item, list):
                for subitem in item:
                    if isinstance(subitem, str):
                        name = subitem.strip()
                        if name:
                            normalized.append(name)
        return normalized or None

    def filter_skills(
        self,
        skill_filter: Optional[List[str]] = None,
        include_disabled: bool = False,
    ) -> List[SkillEntry]:
        """
        Filter skills that are eligible (enabled + requirements met).

        :param skill_filter: List of skill names to include (None = all)
        :param include_disabled: Whether to include disabled skills
        :return: Filtered list of eligible skill entries
        """
        from agent.skills.config import should_include_skill

        entries = list(self.skills.values())

        entries = [e for e in entries if should_include_skill(e, self.config)]

        normalized = self._normalize_skill_filter(skill_filter)
        if normalized is not None:
            entries = [e for e in entries if e.skill.name in normalized]

        if not include_disabled:
            entries = [e for e in entries if self.is_skill_enabled(e.skill.name)]

        from config import conf
        if not conf().get("knowledge", True):
            entries = [e for e in entries if e.skill.name != "knowledge-wiki"]

        return entries

    def filter_unavailable_skills(
        self,
        skill_filter: Optional[List[str]] = None,
    ) -> tuple:
        """
        Find skills that are enabled but have unmet requirements.

        :param skill_filter: Optional list of skill names to include
        :return: Tuple of (entries, missing_map) where missing_map maps
                 skill name to its missing requirements dict
        """
        from agent.skills.config import should_include_skill, get_missing_requirements

        entries = list(self.skills.values())

        # Only enabled skills
        entries = [e for e in entries if self.is_skill_enabled(e.skill.name)]

        normalized = self._normalize_skill_filter(skill_filter)
        if normalized is not None:
            entries = [e for e in entries if e.skill.name in normalized]

        # Keep only those that fail should_include_skill (requirements not met)
        unavailable = []
        missing_map: Dict[str, dict] = {}
        for e in entries:
            if not should_include_skill(e, self.config):
                missing = get_missing_requirements(e)
                if missing:
                    unavailable.append(e)
                    missing_map[e.skill.name] = missing

        return unavailable, missing_map

    def build_skills_prompt(
        self,
        skill_filter: Optional[List[str]] = None,
    ) -> str:
        """
        Build a formatted prompt containing available skills
        and brief hints for unavailable ones.

        :param skill_filter: Optional list of skill names to include
        :return: Formatted skills prompt
        """
        from common.log import logger
        from agent.skills.formatter import format_unavailable_skills_for_prompt

        eligible = self.filter_skills(skill_filter=skill_filter, include_disabled=False)
        logger.debug(f"[SkillManager] Eligible: {len(eligible)} skills (total: {len(self.skills)})")
        if eligible:
            skill_names = [e.skill.name for e in eligible]
            logger.debug(f"[SkillManager] Eligible skills: {skill_names}")

        result = format_skill_entries_for_prompt(eligible)

        unavailable, missing_map = self.filter_unavailable_skills(skill_filter=skill_filter)
        if unavailable:
            unavailable_names = [e.skill.name for e in unavailable]
            logger.debug(f"[SkillManager] Unavailable skills (setup needed): {unavailable_names}")
            result += format_unavailable_skills_for_prompt(unavailable, missing_map)

        result += format_skill_diagnostics_for_prompt(self.get_load_diagnostics())

        logger.debug(f"[SkillManager] Generated prompt length: {len(result)}")
        return result
    
    def build_skill_snapshot(
        self,
        skill_filter: Optional[List[str]] = None,
        version: Optional[int] = None,
    ) -> SkillSnapshot:
        """
        Build a snapshot of skills for a specific run.
        
        :param skill_filter: Optional list of skill names to include
        :param version: Optional version number for the snapshot
        :return: SkillSnapshot
        """
        entries = self.filter_skills(skill_filter=skill_filter, include_disabled=False)
        prompt = format_skill_entries_for_prompt(entries)
        prompt += format_skill_diagnostics_for_prompt(self.get_load_diagnostics())
        
        skills_info = []
        resolved_skills = []
        
        for entry in entries:
            skills_info.append({
                'name': entry.skill.name,
                'primary_env': entry.metadata.primary_env if entry.metadata else None,
            })
            resolved_skills.append(entry.skill)
        
        return SkillSnapshot(
            prompt=prompt,
            skills=skills_info,
            resolved_skills=resolved_skills,
            version=version,
        )
    
    def get_skill_by_key(self, skill_key: str) -> Optional[SkillEntry]:
        """
        Get a skill by its skill key (which may differ from name).
        
        :param skill_key: Skill key to look up
        :return: SkillEntry or None
        """
        for entry in self.skills.values():
            if entry.metadata and entry.metadata.skill_key == skill_key:
                return entry
            if entry.skill.name == skill_key:
                return entry
        return None
