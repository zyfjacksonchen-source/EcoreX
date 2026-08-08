from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from ecorex.migration.cowagent_data import (
    CowAgentDataMigrationError,
    LegacyDataRoot,
    RECEIPT_RELATIVE_PATH,
    default_cowagent_data_roots,
    default_emate_data_root,
    migrate_cowagent_data,
)


def _write(path: Path, value: str | bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(value, bytes):
        path.write_bytes(value)
    else:
        path.write_text(value, encoding="utf-8")


def test_default_locations_cover_supported_desktop_platforms(tmp_path: Path) -> None:
    windows_env = {
        "APPDATA": str(tmp_path / "roaming"),
        "LOCALAPPDATA": str(tmp_path / "local"),
    }
    windows = default_cowagent_data_roots(
        home=tmp_path, environ=windows_env, platform="win32"
    )
    assert windows == (
        LegacyDataRoot("legacy-config", tmp_path / ".cow"),
        LegacyDataRoot("legacy-roaming", tmp_path / "roaming/CowAgent"),
        LegacyDataRoot("legacy-local", tmp_path / "local/CowAgent"),
        LegacyDataRoot("legacy-workspace", tmp_path / "cow"),
    )
    assert (
        default_emate_data_root(home=tmp_path, environ=windows_env, platform="win32")
        == tmp_path / ".emate"
    )
    assert default_emate_data_root(home=tmp_path, environ={}, platform="darwin") == (
        tmp_path / ".emate"
    )
    assert default_emate_data_root(home=tmp_path, environ={}, platform="linux") == (
        tmp_path / ".emate"
    )


def test_first_import_is_secret_free_audited_and_idempotent(tmp_path: Path) -> None:
    source = tmp_path / ".cow"
    target = tmp_path / "e-Mate"
    _write(source / "memory/long-term/index.db", b"conversation-history")
    _write(source / "knowledge/notes.md", "knowledge")
    _write(source / "scheduler/tasks.json", '{"tasks":{"one":{"enabled":false}}}')
    _write(source / "skills/custom/SKILL.md", "# custom")
    _write(source / "attachments/image.png", b"image")
    _write(source / "MEMORY.md", "memory")
    _write(source / ".env", "OPENAI_API_KEY=sk-never-copy")
    _write(source / "skills/custom/credentials.json", '{"token":"never-copy"}')
    _write(source / "browser_profile/Cookies", b"never-copy")
    _write(
        source / "config.json",
        json.dumps(
            {
                "channel_type": "feishu",
                "feishu_app_id": "cli-safe-id",
                "feishu_app_secret": "never-copy-secret",
                "feishu_token": "never-copy-token",
                "group_chat_prefix": ["@helper"],
                "model": "legacy-local-model",
                "open_ai_api_key": "sk-never-copy",
                "web_password": "never-copy-password",
            }
        ),
    )
    before = {
        path.relative_to(source): path.read_bytes()
        for path in source.rglob("*")
        if path.is_file()
    }

    result = migrate_cowagent_data(
        target, source_roots=(LegacyDataRoot("legacy-config", source),)
    )

    assert result.status == "completed"
    assert result.copied_files == 7
    assert (
        target / "memory/long-term/index.db"
    ).read_bytes() == b"conversation-history"
    assert (target / "knowledge/notes.md").read_text(encoding="utf-8") == "knowledge"
    settings = json.loads(
        (target / "channels/imported-legacy-config-settings.json").read_text(
            encoding="utf-8"
        )
    )
    assert settings == {
        "channel_type": "feishu",
        "feishu_app_id": "cli-safe-id",
        "group_chat_prefix": ["@helper"],
    }
    assert not (target / ".env").exists()
    assert not (target / "browser_profile").exists()
    assert not (target / "skills/custom/credentials.json").exists()

    receipt = json.loads((target / RECEIPT_RELATIVE_PATH).read_text(encoding="utf-8"))
    encoded = json.dumps(receipt, ensure_ascii=False)
    assert str(source) not in encoded
    assert "sk-never-copy" not in encoded
    assert "never-copy-secret" not in encoded
    assert {item["reason"] for item in receipt["skipped"]} >= {
        "secret_named_file",
        "secret_or_browser_state",
    }
    after = {
        path.relative_to(source): path.read_bytes()
        for path in source.rglob("*")
        if path.is_file()
    }
    assert after == before

    _write(source / "memory/created-after-first-run.md", "late")
    replay = migrate_cowagent_data(
        target, source_roots=(LegacyDataRoot("legacy-config", source),)
    )
    assert replay.status == "already_completed"
    assert replay.idempotent_replay is True
    assert not (target / "memory/created-after-first-run.md").exists()


def test_links_are_not_followed_and_existing_data_is_not_overwritten(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    target = tmp_path / "target"
    outside = tmp_path / "outside.txt"
    _write(outside, "outside")
    _write(source / "knowledge/current.md", "legacy")
    _write(target / "knowledge/current.md", "e-Mate")
    link = source / "attachments/escape.txt"
    link.parent.mkdir(parents=True)
    try:
        os.symlink(outside, link)
    except OSError:
        pytest.skip("this account cannot create symbolic links")

    result = migrate_cowagent_data(
        target, source_roots=(LegacyDataRoot("legacy-workspace", source),)
    )

    assert result.skipped_entries == 2
    assert (target / "knowledge/current.md").read_text(encoding="utf-8") == "e-Mate"
    assert not (target / "attachments/escape.txt").exists()
    receipt = json.loads((target / RECEIPT_RELATIVE_PATH).read_text(encoding="utf-8"))
    assert receipt["files"][0]["status"] == "target_conflict"
    assert receipt["skipped"] == [
        {
            "path": "attachments/escape.txt",
            "reason": "unsafe_link_or_reparse",
            "source": "legacy-workspace",
        }
    ]


def test_replay_preserves_live_data_modified_after_import(tmp_path: Path) -> None:
    source = tmp_path / "source"
    target = tmp_path / "target"
    _write(source / "memory/fact.md", "original")
    migrate_cowagent_data(
        target, source_roots=(LegacyDataRoot("legacy-workspace", source),)
    )
    (target / "memory/fact.md").write_text("tampered", encoding="utf-8")

    replay = migrate_cowagent_data(
        target, source_roots=(LegacyDataRoot("legacy-workspace", source),)
    )
    assert replay.status == "already_completed"
    assert (target / "memory/fact.md").read_text(encoding="utf-8") == "tampered"


def test_tampered_receipt_fails_closed(tmp_path: Path) -> None:
    source = tmp_path / "source"
    target = tmp_path / "target"
    _write(source / "memory/fact.md", "original")
    migrate_cowagent_data(
        target, source_roots=(LegacyDataRoot("legacy-workspace", source),)
    )
    receipt_path = target / RECEIPT_RELATIVE_PATH
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["status"] = "forged"
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")

    with pytest.raises(CowAgentDataMigrationError, match="receipt is invalid"):
        migrate_cowagent_data(
            target, source_roots=(LegacyDataRoot("legacy-workspace", source),)
        )


def test_missing_source_is_a_non_destructive_noop(tmp_path: Path) -> None:
    target = tmp_path / "target"
    result = migrate_cowagent_data(
        target,
        source_roots=(LegacyDataRoot("legacy-config", tmp_path / "missing"),),
    )
    assert result.status == "source_missing"
    assert not result.receipt_path.exists()


def test_desktop_startup_runs_import_before_runtime_composition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ecorex.server import cli

    order: list[tuple[str, Path | None]] = []
    monkeypatch.setattr(
        cli,
        "migrate_legacy_desktop_data",
        lambda target: order.append(("migration", Path(target))),
    )
    monkeypatch.delenv("EMATE_DESKTOP", raising=False)
    monkeypatch.delenv("EMATE_DATA_DIR", raising=False)

    def runtime_loader(**_kwargs):
        order.append(("runtime", None))
        raise ValueError("stop after order assertion")

    with pytest.raises(cli.ProductRuntimeConfigurationError):
        cli.build_product_runtime_server(
            host="127.0.0.1",
            port=8765,
            runtime_loader=runtime_loader,
        )
    assert order == [("runtime", None)]

    order.clear()
    monkeypatch.setenv("EMATE_DESKTOP", "1")
    monkeypatch.setenv("EMATE_DATA_DIR", str(tmp_path / ".emate"))
    with pytest.raises(cli.ProductRuntimeConfigurationError):
        cli.build_product_runtime_server(
            host="127.0.0.1",
            port=8765,
            runtime_loader=runtime_loader,
        )
    assert order == [
        ("migration", tmp_path / ".emate"),
        ("runtime", None),
    ]
