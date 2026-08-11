from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
import runpy
import zipfile

import pytest

from ecorex import __version__

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "build-v1-manual-webui.py"


def _builder() -> dict[str, object]:
    return runpy.run_path(str(SCRIPT))


def test_manual_webui_builder_pins_and_rechecks_base_package(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    builder = _builder()
    package = tmp_path / "base.zip"
    with zipfile.ZipFile(package, "w") as archive:
        archive.writestr("release/release-manifest.json", b"{}")
    digest = hashlib.sha256(package.read_bytes()).hexdigest()
    monkeypatch.setitem(builder["BASE_PACKAGES"], "windows", (package.name, digest))

    with builder["_base_package"](package, "windows") as archive:
        assert archive.namelist() == ["release/release-manifest.json"]

    package.write_bytes(package.read_bytes() + b"tampered")
    with pytest.raises(
        builder["ManualWebUIBuildError"],
        match="manual_webui_base_windows_invalid",
    ):
        builder["_base_package"](package, "windows")


def test_manual_webui_builder_preserves_predecessor_release_trust(
    tmp_path: Path,
) -> None:
    builder = _builder()
    public = bytes(range(32))
    key_id = f"release-{hashlib.sha256(public).hexdigest()[:20]}"
    trust = tmp_path / "predecessor.json"
    trust.write_text(
        json.dumps(
            {
                "schema": builder["PREDECESSOR_TRUST_SCHEMA"],
                "version": "2.0.0",
                "release_id": "release-stable-" + "a" * 24,
                "build_digest": "b" * 64,
                "signing_key_id": key_id,
                "release_public_keys": {
                    key_id: base64.b64encode(public).decode("ascii")
                },
            }
        ),
        encoding="utf-8",
    )

    keys, identity = builder["_load_predecessor_trust"](trust)

    assert tuple(keys) == (key_id,)
    assert identity == {
        "version": "2.0.0",
        "release_id": "release-stable-" + "a" * 24,
        "build_digest": "b" * 64,
        "signing_key_id": key_id,
    }

    value = json.loads(trust.read_text(encoding="utf-8"))
    value["release_public_keys"][key_id] = value["release_public_keys"][key_id][:-2] + "9="
    trust.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(
        builder["ManualWebUIBuildError"],
        match="manual_webui_predecessor_trust_invalid",
    ):
        builder["_load_predecessor_trust"](trust)


def test_checked_in_predecessor_trust_covers_supported_v2_release_identities() -> None:
    keys, identity = _builder()["_load_predecessor_trust"](
        ROOT / "release" / "v1" / "desktop-predecessor-trust.json"
    )

    assert identity == {
        "version": "2.0.2",
        "release_id": "release-stable-0fc72baa9cde99e7edcdbaeb",
        "build_digest": "0fc72baa9cde99e7edcdbaeb59d378fd2eb8980a4aec2a8563220192daadd274",
        "signing_key_id": "ecorex-webui-release-87e4b43e080932855e2b",
    }
    assert keys == {
        "ecorex-webui-release-0ef113eca992433d9d43": "+v+fPP/7gWk/VB1k2V8hRmIfkQ/j+IobGgfC+PaWR7A=",
        "ecorex-webui-release-4f7c45cbc4965e3f5e83": "mS+1bsB7xm5mNd1YXyDI6D93w9AHCSw2IyMjHyU7NiE=",
        "ecorex-webui-release-87e4b43e080932855e2b": "ehzYdNnp2cPlGIn3aW4khw/lY/WjJGedQol3XWyQJrc=",
        "ecorex-webui-release-cfb9b141bd87235444ca": "bXoHp+C9D1I6amv2yfM8BN+qpzztXHJCvi9VOyXJ/fA=",
    }


def test_manual_webui_runtime_config_is_canonical_and_rebound(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ECOREX_V1_FEISHU_CONNECTOR_ENABLED", raising=False)
    builder = _builder()
    core = tmp_path / "core"
    core.mkdir()
    config = core / "runtime-config.json"
    config.write_text(
        json.dumps(
            {
                "identity": {
                    "version": "0.3.2",
                    "platform": "macos",
                    "architecture": "arm64",
                },
                "release_public_keys": {"old": "key"},
                "capability_packs": [
                    {
                        "artifact": "ecorex-capability-pack-0.3.2.zip",
                        "manifest": "ecorex-capability-pack-0.3.2.json",
                    }
                ],
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    builder["_runtime_config"](
        core,
        platform="macos",
        architecture="arm64",
        release_keys={"v1": "public"},
    )

    payload = config.read_bytes()
    value = json.loads(payload)
    assert payload == json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    assert value["identity"] == {
        "version": __version__,
        "platform": "macos",
        "architecture": "arm64",
    }
    assert value["release_public_keys"] == {"v1": "public"}
    assert value["audit"] == {
        "endpoint": "https://dl.ecoremedia.net/api/v1/audit/records",
        "allowed_hosts": ["dl.ecoremedia.net"],
        "dispatch_seconds": 5,
        "raw_retention_days": 30,
        "aggregate_retention_days": 180,
    }
    assert value["connectors"] is None
    assert value["capability_packs"] == [
        {
            "artifact": f"ecorex-capability-pack-{__version__}.zip",
            "manifest": f"ecorex-capability-pack-{__version__}.json",
        }
    ]


def test_manual_webui_runtime_enables_feishu_only_by_explicit_release_flag(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    builder = _builder()
    core = tmp_path / "core"
    core.mkdir()
    (core / "runtime-config.json").write_text(
        json.dumps({"capability_packs": []}), encoding="utf-8"
    )
    monkeypatch.setenv("ECOREX_V1_FEISHU_CONNECTOR_ENABLED", "true")

    builder["_runtime_config"](
        core,
        platform="windows",
        architecture="x64",
        release_keys={"v1": "public"},
    )

    assert json.loads((core / "runtime-config.json").read_text())["connectors"] == {
        "endpoint": "https://dl.ecoremedia.net/api/v1/connectors",
        "allowed_hosts": ["dl.ecoremedia.net"],
        "enabled_connectors": ["feishu"],
    }


def test_manual_webui_release_sources_are_one_ordered_set() -> None:
    builder = _builder()

    sources = builder["_sources"]()

    assert [(source.source_id, source.priority) for source in sources] == [
        ("github-cn", 0),
        ("github", 1),
        ("cdn", 2),
    ]
    assert sources[0].base_url.startswith("https://gh-proxy.com/")
    assert sources[1].base_url.endswith(f"/EcoreX/releases/download/v{__version__}")
    assert sources[2].base_url == "https://mvdcm.ecoremedia.net/e-mate/update"


def test_manual_webui_macos_core_keeps_both_runtime_entries_executable() -> None:
    builder = _builder()

    assert builder["_core_executable_paths"]("macos") == (
        "bin/ecorex",
        "bin/pack-python/bin/python3",
    )
    assert builder["_core_executable_paths"]("windows") == ("bin/ecorex.exe",)


def test_manual_webui_channel_overlay_keeps_fastapi_multipart_authority() -> None:
    builder = _builder()

    assert builder["_canonical_distribution_name"](
        Path("web.py-0.61.dist-info")
    ) == "web-py"
    assert "multipart" not in builder["CHANNEL_RUNTIME_PACKAGES"]
    assert "multipart" not in builder["CHANNEL_RUNTIME_DISTRIBUTIONS"]
    assert {
        "aiohappyeyeballs",
        "aiohttp",
        "dingtalk-stream",
        "discord-py",
        "lark-oapi",
        "python-telegram-bot",
        "slack-bolt",
        "web-py",
        "websocket-client",
        "wechatpy",
    } <= builder["CHANNEL_RUNTIME_DISTRIBUTIONS"]


def test_manual_webui_core_contains_exact_tracked_builtin_skills(tmp_path: Path) -> None:
    builder = _builder()
    core = tmp_path / "core"
    (core / "skills").mkdir(parents=True)
    (core / "skills" / "stale.txt").write_text("stale", encoding="utf-8")

    builder["_replace_builtin_skills"](core, ROOT)

    expected = {
        path.relative_to(ROOT)
        for path in builder["_tracked_source_files"](ROOT, "skills")
    }
    actual = {
        path.relative_to(core)
        for path in (core / "skills").rglob("*")
        if path.is_file()
    }
    assert actual == expected
    assert not (core / "skills" / "stale.txt").exists()
    assert (core / "skills" / "office-presentations" / "SKILL.md").is_file()


def test_manual_update_contract_is_the_only_release_authority() -> None:
    contract = (
        ROOT / "release" / "v1" / "CLI_AND_MANUAL_UPDATE_CONTRACT.md"
    ).read_text(encoding="utf-8")
    readme = (ROOT / "release" / "v1" / "README.md").read_text(encoding="utf-8")

    for invariant in (
        "sole normative source of truth",
        "new Core delta -> reuse unchanged Packs by SHA-256",
        "publicly read back before acceptance",
        "stable update pointer is the sole mutable publication fact",
        "as the final operation",
    ):
        assert invariant in contract
    assert (
        "sole normative production, update and manual operator source of truth"
        in readme
    )
