from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from agent.extensions.registry import ExtensionRegistry
from agent.skills.manager import SkillManager
from agent.skills.service import SkillService


ROOT = Path(__file__).resolve().parents[1]


def _manager(custom_root: Path, *, builtin_root: Path | None = None) -> SkillManager:
    manager = SkillManager(
        builtin_dir=str(builtin_root or ROOT / "skills"),
        custom_dir=str(custom_root / "skills"),
        config={},
    )
    manager.extra_dirs = []
    manager.refresh_skills()
    return manager


def _write_skill(root: Path, name: str, description: str) -> None:
    skill_dir = root / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\nUse this skill for {description}.\n",
        encoding="utf-8",
    )


def test_builtin_skills_are_default_enabled_locked_and_purpose_grouped():
    with tempfile.TemporaryDirectory(prefix="ecorex-v024-skill-governance-") as tmp:
        manager = _manager(Path(tmp))

        manager.skills_config["office-documents"]["enabled"] = False
        manager.skills_config["office-documents"]["source"] = "custom"
        manager._save_skills_config()
        manager.refresh_skills()

        rows = {row["name"]: row for row in SkillService(manager).query()}
        office = rows["office-documents"]
        assert office["source"] == "builtin"
        assert office["source_group"] == "builtin"
        assert office["builtin_catalog"] is True
        assert office["source_label"] == "内置"
        assert office["purpose_group"] == "office"
        assert office["purpose_label"] == "办公能力"
        assert office["enabled"] is True
        assert office["default_enabled"] is True
        assert office["toggleable"] is False
        assert office["locked"] is True
        assert office["lock_reason"] == "builtin-default-enabled"

        image = rows["image-generation"]
        assert image["source_group"] == "builtin"
        assert image["purpose_group"] == "image_media"


def test_builtin_skill_close_is_rejected_at_service_and_manager_boundaries():
    with tempfile.TemporaryDirectory(prefix="ecorex-v024-skill-lock-") as tmp:
        manager = _manager(Path(tmp))
        service = SkillService(manager)

        with pytest.raises(PermissionError):
            service.close({"name": "office-pdf"})
        with pytest.raises(PermissionError):
            manager.set_skill_enabled("office-pdf", False)

        assert SkillService(manager).query()[0].get("source_group")


def test_shadowed_builtin_catalog_skill_remains_default_enabled_and_locked():
    with tempfile.TemporaryDirectory(prefix="ecorex-v024-skill-shadow-") as tmp:
        root = Path(tmp)
        builtin_root = root / "builtin"
        external_root = root / "external"
        custom_root = root / "workspace" / "skills"
        _write_skill(builtin_root, "factory-shadow", "factory office skill")
        _write_skill(external_root, "factory-shadow", "external shadow office skill")

        manager = SkillManager(
            builtin_dir=str(builtin_root),
            custom_dir=str(custom_root),
            config={},
        )
        manager.extra_dirs = [str(external_root)]
        manager.refresh_skills()

        row = {item["name"]: item for item in SkillService(manager).query()}["factory-shadow"]
        assert row["source"] == "extra"
        assert row["builtin_catalog"] is True
        assert row["builtinCatalog"] is True
        assert row["source_group"] == "builtin"
        assert row["source_label"] == "内置"
        assert row["enabled"] is True
        assert row["default_enabled"] is True
        assert row["toggleable"] is False
        assert row["locked"] is True

        with pytest.raises(PermissionError):
            SkillService(manager).close({"name": "factory-shadow", "_permission_checked": True})
        with pytest.raises(PermissionError):
            manager.set_skill_enabled("factory-shadow", False)


def test_custom_and_external_skills_share_schema_but_keep_source_policy():
    with tempfile.TemporaryDirectory(prefix="ecorex-v024-skill-sources-") as tmp:
        root = Path(tmp)
        custom_skills = root / "skills"
        external_root = root / "external"
        builtin_root = root / "empty-builtin"
        builtin_root.mkdir()
        _write_skill(custom_skills, "my-custom-skill", "custom workflow helper")
        _write_skill(external_root, "vendor-office-helper", "external office helper")

        manager = SkillManager(
            builtin_dir=str(builtin_root),
            custom_dir=str(custom_skills),
            config={},
        )
        manager.extra_dirs = [str(external_root)]
        manager.refresh_skills()

        rows = {row["name"]: row for row in SkillService(manager).query()}
        custom = rows["my-custom-skill"]
        external = rows["vendor-office-helper"]

        assert custom["source"] == "custom"
        assert custom["source_group"] == "custom"
        assert custom["source_label"] == "自建"
        assert custom["toggleable"] is True
        assert custom["locked"] is False

        assert external["source"] == "extra"
        assert external["source_group"] == "external"
        assert external["source_label"] == "外部"
        assert external["purpose_group"] == "office"
        assert external["toggleable"] is True
        assert external["locked"] is False


def test_extension_registry_projects_same_skill_governance_fields():
    with tempfile.TemporaryDirectory(prefix="ecorex-v024-extension-governance-") as tmp:
        payload = ExtensionRegistry(tmp).list_extensions()
        rows = {
            row["id"]: row
            for row in payload["extensions"]
            if row.get("id", "").startswith("skill:")
        }

        office = rows["skill:office-spreadsheets"]
        assert office["type"] == "builtin_skill"
        assert office["sourceGroup"] == "builtin"
        assert office["sourceLabel"] == "内置"
        assert office["builtinCatalog"] is True
        assert office["purposeGroup"] == "office"
        assert office["purposeLabel"] == "办公能力"
        assert office["enabled"] is True
        assert office["defaultEnabled"] is True
        assert office["toggleable"] is False
        assert office["locked"] is True


def test_legacy_web_console_respects_locked_skill_toggles():
    console_js = (ROOT / "channel" / "web" / "static" / "js" / "console.js").read_text(encoding="utf-8")

    assert "sk.locked === true || sourceGroup === 'builtin'" in console_js
    assert "sk.toggleable !== false" in console_js
    assert 'disabled aria-disabled="true"' in console_js
    assert "skill.toggleable === false" in console_js
