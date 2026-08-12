"""One-time legacy Skill directory convergence into ExtensionService/CAS."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
from typing import Mapping

from .local_bundle import LocalSkillBundle, parse_migrated_skill_frontmatter
from .service import ExtensionService


SKILL_ALIASES = {
    "docx": "office-documents",
    "xlsx": "office-spreadsheets",
    "pptx": "office-presentations",
    "pdf": "office-pdf",
    "lark-cli": "feishu-lark",
}
EXCLUDED_SKILL_SLUGS: frozenset[str] = frozenset()
_SLUG = re.compile(r"^[a-z][a-z0-9-]{0,95}$")


@dataclass(frozen=True, slots=True)
class _Candidate:
    slug: str
    directory: Path
    bundle: LocalSkillBundle
    override: bool
    enabled: bool


def migrate_skill_directories(
    service: ExtensionService,
    *,
    builtin_root: str | Path | None,
    custom_roots: tuple[str | Path, ...] = (),
) -> frozenset[str]:
    """Converge source directories without deleting or rewriting user files."""

    if service.local_bundle_store is None:
        return frozenset()
    preferences = _legacy_preferences(service)
    custom: dict[str, _Candidate] = {}
    migrated_names: set[str] = set()
    for root in custom_roots:
        root_path = Path(root)
        config = _skill_config(root_path)
        for directory in _skill_directories(root_path):
            metadata = parse_migrated_skill_frontmatter((directory / "SKILL.md").read_bytes())
            raw_slug = _slug(metadata.name)
            if raw_slug in EXCLUDED_SKILL_SLUGS:
                migrated_names.add(raw_slug)
                continue
            slug = SKILL_ALIASES.get(raw_slug, raw_slug)
            if slug in EXCLUDED_SKILL_SLUGS:
                migrated_names.add(raw_slug)
                continue
            bundle = service.local_bundle_store.ingest_directory(
                directory, migrated_frontmatter=True
            )
            explicit = config.get(raw_slug, config.get(slug))
            enabled = (
                bool(explicit.get("enabled", True))
                if isinstance(explicit, Mapping)
                else preferences.get(slug, slug != "feishu-lark")
            )
            candidate = _Candidate(
                slug=slug,
                directory=directory,
                bundle=bundle,
                override=(directory / ".ecorex-custom-override").is_file(),
                enabled=enabled,
            )
            prior = custom.get(slug)
            if prior is None or (candidate.override, str(candidate.directory)) > (
                prior.override,
                str(prior.directory),
            ):
                custom[slug] = candidate
            migrated_names.update({raw_slug, slug})

    builtins: dict[str, tuple[Path, LocalSkillBundle]] = {}
    if builtin_root is not None:
        for directory in _skill_directories(Path(builtin_root)):
            metadata = parse_migrated_skill_frontmatter((directory / "SKILL.md").read_bytes())
            raw_slug = _slug(metadata.name)
            if raw_slug in EXCLUDED_SKILL_SLUGS:
                migrated_names.add(raw_slug)
                continue
            slug = SKILL_ALIASES.get(raw_slug, raw_slug)
            if slug in EXCLUDED_SKILL_SLUGS:
                migrated_names.add(raw_slug)
                continue
            bundle = service.local_bundle_store.ingest_directory(
                directory, migrated_frontmatter=True
            )
            builtins.setdefault(slug, (directory, bundle))
            migrated_names.update({raw_slug, slug})

    for slug in sorted(set(builtins) | set(custom)):
        extension_id = f"skill.{slug}"
        if _is_uninstall_tombstone(service, extension_id):
            continue
        builtin = builtins.get(slug)
        candidate = custom.get(slug)
        custom_wins = bool(
            candidate
            and (
                candidate.override
                or builtin is None
                or candidate.bundle.artifact_sha256 != builtin[1].artifact_sha256
            )
        )
        selected_directory: Path
        selected_builtin: bool
        enabled: bool
        if custom_wins:
            assert candidate is not None
            selected_directory = candidate.directory
            selected_builtin = False
            enabled = candidate.enabled
        elif builtin is not None:
            selected_directory = builtin[0]
            selected_builtin = True
            enabled = preferences.get(slug, slug != "feishu-lark")
        else:
            continue
        service.register_migrated_skill_directory(
            str(selected_directory),
            extension_id=extension_id,
            builtin=selected_builtin,
            initially_enabled=enabled,
        )
    return frozenset(migrated_names)


def _slug(value: str) -> str:
    slug = value.strip().casefold().replace("_", "-")
    if not _SLUG.fullmatch(slug):
        raise ValueError(f"legacy Skill name cannot form a canonical slug: {value!r}")
    return slug


def _skill_directories(root: Path) -> tuple[Path, ...]:
    if not root.is_dir():
        return ()
    found: list[Path] = []
    for current, directories, files in os.walk(root, followlinks=False):
        directories[:] = sorted(
            name for name in directories if not name.startswith(".")
        )
        if "SKILL.md" in files:
            found.append(Path(current))
            directories[:] = []
    return tuple(sorted(found, key=lambda item: str(item).casefold()))


def _skill_config(root: Path) -> Mapping[str, object]:
    try:
        value = json.loads((root / "skills_config.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    if not isinstance(value, Mapping):
        return {}
    result: dict[str, object] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            continue
        try:
            slug = _slug(key)
        except ValueError:
            continue
        result[SKILL_ALIASES.get(slug, slug)] = item
    return result


def _legacy_preferences(service: ExtensionService) -> dict[str, bool]:
    with service.repository.database.reader() as connection:
        if connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='skill_states'"
        ).fetchone() is None:
            return {}
        rows = connection.execute(
            "SELECT name, enabled, source FROM skill_states "
            "ORDER BY CASE source WHEN 'workspace' THEN 1 ELSE 0 END, skill_id"
        ).fetchall()
    result: dict[str, bool] = {}
    for row in rows:
        try:
            slug = _slug(str(row["name"]))
        except ValueError:
            continue
        result[SKILL_ALIASES.get(slug, slug)] = bool(row["enabled"])
    return result


def _is_uninstall_tombstone(service: ExtensionService, extension_id: str) -> bool:
    state = service.repository.state(extension_id)
    return bool(
        state is not None
        and state.active_revision_id is None
        and state.staged_revision_id is None
    )


__all__ = [
    "EXCLUDED_SKILL_SLUGS",
    "SKILL_ALIASES",
    "migrate_skill_directories",
]
