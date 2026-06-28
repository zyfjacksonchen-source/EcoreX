from __future__ import annotations

import tempfile
from pathlib import Path

from agent.skills.formatter import format_skills_for_prompt
from agent.skills.manager import SkillManager
from agent.skills.service import SkillService


ROOT = Path(__file__).resolve().parents[1]


EXPECTED_FACADES = {
    "office-presentations": "Presentations",
    "office-spreadsheets": "Spreadsheets",
    "office-documents": "documents",
    "office-pdf": "pdf",
    "image-generation": "imagegen",
}


def _load_builtin_only_manager() -> SkillManager:
    tmp = tempfile.TemporaryDirectory(prefix="ecorex-v024-facades-")
    manager = SkillManager(
        builtin_dir=str(ROOT / "skills"),
        custom_dir=str(Path(tmp.name) / "skills"),
        config={},
    )
    manager._v024_tmp = tmp  # keep tempdir alive for the manager lifetime
    manager.extra_dirs = []
    manager.refresh_skills()
    return manager


def test_v024_native_facades_keep_legacy_ids_and_official_mapping():
    manager = _load_builtin_only_manager()
    rows = {row.get("name"): row for row in SkillService(manager).query()}

    for legacy_id, official_skill in EXPECTED_FACADES.items():
        row = rows.get(legacy_id)
        assert row, f"{legacy_id} must remain discoverable"
        assert row.get("source") == "builtin"
        assert row.get("enabled") is not False
        assert row.get("compatibility_id") == legacy_id
        assert row.get("adopts_official_skill") == official_skill
        assert row.get("ecorex_native_facade") is True
        assert row.get("quality_gates"), f"{legacy_id} must declare quality gates"

    for legacy_id in [name for name in EXPECTED_FACADES if name.startswith("office-")]:
        row = rows[legacy_id]
        assert row.get("user_invocable") is True
        assert row.get("mentionable") is True
        assert row.get("mention_category") == "document"


def test_v024_native_facade_metadata_is_visible_in_skill_prompt():
    manager = _load_builtin_only_manager()

    for legacy_id, official_skill in EXPECTED_FACADES.items():
        entry = manager.skills[legacy_id]
        prompt = format_skills_for_prompt([entry.skill])
        assert f"<compatibility_id>{legacy_id}</compatibility_id>" in prompt
        assert f"<adopts_official_skill>{official_skill}</adopts_official_skill>" in prompt
        assert "<ecorex_native_facade>true</ecorex_native_facade>" in prompt
        assert "<quality_gates>" in prompt
