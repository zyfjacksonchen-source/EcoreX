import tempfile
from pathlib import Path

import config
from agent.prompt import workspace
from common.ecorex_identity import sanitize_assistant_identity


OLD_ECOREX_PERSONA = (
    "You are EcoreX, the desktop AI Agent for Yixin Advertising. Keep a professional, rigorous, concise tone. "
    "Address the user as tongxue. Always identify as EcoreX. Confirm goals and constraints first, then provide executable steps. "
    "When using tools, files, web search, Skills, or MCP, clearly explain the reason and result."
)


def test_default_identity_templates_seed_xiaoxin_without_identity_questions():
    assert "小芯" in workspace._AGENT_TEMPLATE_ZH
    assert "同学" in workspace._AGENT_TEMPLATE_ZH
    assert "专业" in workspace._AGENT_TEMPLATE_ZH
    assert "严谨" in workspace._AGENT_TEMPLATE_ZH

    assert "不要主动询问" in workspace._BOOTSTRAP_TEMPLATE_ZH
    assert "Do not proactively ask" in workspace._BOOTSTRAP_TEMPLATE_EN
    assert "仅在用户没有提出具体任务" not in workspace._BOOTSTRAP_TEMPLATE_ZH
    assert "Ask the core questions only" not in workspace._BOOTSTRAP_TEMPLATE_EN


def test_default_identity_marks_first_run_onboarding_done():
    with tempfile.TemporaryDirectory() as tmp:
        files = workspace.ensure_workspace(tmp)
        bootstrap_path = Path(tmp) / workspace.DEFAULT_BOOTSTRAP_FILENAME
        assert bootstrap_path.exists()
        assert Path(files.agent_path).read_text(encoding="utf-8").count("小芯") >= 1

        loaded = workspace.load_context_files(tmp)
        assert not bootstrap_path.exists()
        assert all(item.path != workspace.DEFAULT_BOOTSTRAP_FILENAME for item in loaded)
        assert any(item.path == workspace.DEFAULT_AGENT_FILENAME and "小芯" in item.content for item in loaded)


def test_legacy_default_persona_migrates_to_xiaoxin():
    cfg = {"character_desc": OLD_ECOREX_PERSONA}
    config._ensure_ecorex_runtime_defaults(cfg)
    assert "小芯" in cfg["character_desc"]
    assert "同学" in cfg["character_desc"]
    assert "专业" in cfg["character_desc"]
    assert "严谨" in cfg["character_desc"]


def test_custom_persona_is_preserved():
    cfg = {"character_desc": "custom persona"}
    config._ensure_ecorex_runtime_defaults(cfg)
    assert cfg["character_desc"] == "custom persona"


def test_legacy_assistant_self_name_is_sanitized_to_xiaoxin():
    assert sanitize_assistant_identity("我是 CowAgent。") == "我是 小芯。"


def test_provider_built_in_self_identity_is_sanitized_to_xiaoxin():
    text = "我是小芯，一个由 Google Deepmind 团队研发的 AI 助手。可以帮你处理文件。"
    sanitized = sanitize_assistant_identity(text)
    assert sanitized == "我是小芯，EcoreX WebUI 的 AI 助手。可以帮你处理文件。"
    assert "Google" not in sanitized
    assert "Deepmind" not in sanitized

    text = "我是小芯，是基于 Google DeepMind 团队研发的 Antigravity 智能体架构的 AI 助手。"
    sanitized = sanitize_assistant_identity(text)
    assert sanitized == "我是小芯，EcoreX WebUI 的 AI 助手。"
    assert "Antigravity" not in sanitized
