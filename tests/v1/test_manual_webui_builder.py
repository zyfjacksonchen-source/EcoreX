from __future__ import annotations

import hashlib
import json
from pathlib import Path
import runpy
import zipfile

import pytest


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


def test_manual_webui_runtime_config_is_canonical_and_rebound(tmp_path: Path) -> None:
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
        "version": "1.0.0",
        "platform": "macos",
        "architecture": "arm64",
    }
    assert value["release_public_keys"] == {"v1": "public"}
    assert value["capability_packs"] == [
        {
            "artifact": "ecorex-capability-pack-1.0.0.zip",
            "manifest": "ecorex-capability-pack-1.0.0.json",
        }
    ]


def test_manual_webui_release_sources_are_one_ordered_set() -> None:
    builder = _builder()

    sources = builder["_sources"]()

    assert [(source.source_id, source.priority) for source in sources] == [
        ("github-cn", 0),
        ("github", 1),
        ("cdn", 2),
    ]
    assert sources[0].base_url.startswith("https://gh-proxy.com/")
    assert sources[2].base_url == "https://dl.ecoremedia.net/ecorex-agent/downloads"


def test_manual_webui_macos_core_keeps_both_runtime_entries_executable() -> None:
    builder = _builder()

    assert builder["_core_executable_paths"]("macos") == (
        "bin/ecorex",
        "bin/pack-python/bin/python3",
    )
    assert builder["_core_executable_paths"]("windows") == ("bin/ecorex.exe",)
