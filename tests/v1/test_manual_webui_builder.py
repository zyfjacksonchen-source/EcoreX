from __future__ import annotations

import base64
import hashlib
from importlib import metadata
import json
from pathlib import Path
import runpy
import shutil
import subprocess
import sys
from types import SimpleNamespace
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


def test_manual_webui_release_evidence_matches_bootstrap_bounds(
    tmp_path: Path,
) -> None:
    builder = _builder()
    metadata_path = tmp_path / "release-metadata.json"
    sbom_path = tmp_path / "sbom.cdx.json"
    metadata_path.write_bytes(b"{}")
    with sbom_path.open("wb") as stream:
        stream.truncate(39_790_694)

    builder["_verify_release_evidence_bounds"](metadata_path, sbom_path)
    go_source = (ROOT / "platform-staging/bootstrap/main.go").read_text()
    assert builder["MAX_RELEASE_METADATA_BYTES"] == 16 * 1024 * 1024
    assert builder["MAX_RELEASE_SBOM_BYTES"] == 64 * 1024 * 1024
    assert "maxMetadataBytes     = 16 * 1024 * 1024" in go_source
    assert "maxSBOMBytes         = 64 * 1024 * 1024" in go_source

    with sbom_path.open("wb") as stream:
        stream.truncate(builder["MAX_RELEASE_SBOM_BYTES"] + 1)
    with pytest.raises(
        builder["ManualWebUIBuildError"],
        match="manual_webui_release_evidence_invalid",
    ):
        builder["_verify_release_evidence_bounds"](metadata_path, sbom_path)


def test_manual_webui_core_package_shape_matches_bootstrap_bounds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    builder = _builder()
    archive_path = tmp_path / "core.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("payload.bin", b"bounded")
    artifact = SimpleNamespace(
        artifact_id="core-macos-arm64",
        size_bytes=archive_path.stat().st_size,
    )
    built = SimpleNamespace(
        manifest=SimpleNamespace(artifacts=(artifact,)),
        artifact_paths={artifact.artifact_id: archive_path},
    )

    builder["_verify_release_core_bounds"](built)
    assert 62_034_702 <= builder["MAX_CORE_ARCHIVE_BYTES"]
    assert 166_490_214 <= builder["MAX_CORE_EXPANDED_BYTES"]
    go_source = (ROOT / "platform-staging/bootstrap/main.go").read_text()
    assert "maxCoreArchiveBytes  = 150 * 1024 * 1024" in go_source
    assert "maxCoreExpandedBytes = 256 * 1024 * 1024" in go_source

    function_globals = builder["_verify_release_core_bounds"].__globals__
    archive_limit = function_globals["MAX_CORE_ARCHIVE_BYTES"]
    monkeypatch.setitem(function_globals, "MAX_CORE_ARCHIVE_BYTES", 1)
    with pytest.raises(
        builder["ManualWebUIBuildError"],
        match="manual_webui_release_core_bound_invalid",
    ):
        builder["_verify_release_core_bounds"](built)
    monkeypatch.setitem(function_globals, "MAX_CORE_ARCHIVE_BYTES", archive_limit)
    monkeypatch.setitem(function_globals, "MAX_CORE_EXPANDED_BYTES", 1)
    with pytest.raises(
        builder["ManualWebUIBuildError"],
        match="manual_webui_release_core_bound_invalid",
    ):
        builder["_verify_release_core_bounds"](built)


def test_manual_webui_runtime_overlay_tracks_the_complete_active_lock() -> None:
    builder = _builder()

    versions = builder["active_lock_versions"](
        ROOT / "requirements" / "locks" / "runtime.lock"
    )
    assert "regex" in versions
    assert "python-multipart" in versions
    assert len(versions) >= 55


def test_manual_webui_product_overlay_contains_the_cow_runtime_spine(
    tmp_path: Path,
) -> None:
    builder = _builder()
    original_run = builder["_run"]
    core = tmp_path / "core"
    site_packages = builder["_runtime_site_packages"](core, "macos")
    archive = tmp_path / "python311.zip"
    with zipfile.ZipFile(archive, "w") as output:
        member = zipfile.ZipInfo("ecorex/stale.py", builder["FIXED_TIME"])
        member.create_system = 3
        member.external_attr = (0o100644) << 16
        output.writestr(member, b"stale = True\n")

    def fake_run(command, **kwargs):  # noqa: ANN001
        if tuple(command[:3]) == (sys.executable, "-m", "pip"):
            staging = Path(command[command.index("--target") + 1])
            versions = builder["active_lock_versions"](
                ROOT / "requirements" / "locks" / "runtime.lock"
            )
            for name, version in versions.items():
                info = staging / f"{name.replace('-', '_')}-{version}.dist-info"
                info.mkdir(parents=True)
                (info / "METADATA").write_text(
                    f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n",
                    encoding="utf-8",
                )
            regex_package = Path(metadata.distribution("regex").locate_file("regex"))
            shutil.copytree(regex_package, staging / "regex")
            return b""
        return original_run(command, **kwargs)

    builder["_run"] = fake_run
    builder["_install_locked_runtime_overlay"](
        ROOT,
        core,
        tmp_path,
        platform="macos",
        architecture="arm64",
    )
    builder["_install_cow_runtime_overlay"](
        ROOT,
        core,
        tmp_path,
        platform="macos",
    )
    builder["_replace_product_imports"](archive, ROOT)

    with zipfile.ZipFile(archive) as packaged:
        assert "ecorex/runtime/worker.py" in packaged.namelist()
    assert (site_packages / "agent/tools/tool_manager.py").is_file()
    assert (site_packages / "bridge/agent_initializer.py").is_file()
    assert (site_packages / "config.py").is_file()
    probe = subprocess.run(
        (
            sys.executable,
            "-I",
            "-B",
            "-c",
            "import sys; "
            f"sys.path.insert(0, {str(archive)!r}); "
            f"sys.path.insert(0, {str(site_packages)!r}); "
            "import regex; "
            "from agent.tools.search_files.search_files import SearchFiles; "
            "from agent.tools.tool_manager import ToolManager; "
            "from ecorex.runtime.worker import AgentTurnWorker; "
            "assert SearchFiles.__module__ == 'agent.tools.search_files.search_files'; "
            "assert ToolManager.__module__ == 'agent.tools.tool_manager'; "
            "assert AgentTurnWorker.__module__ == 'ecorex.runtime.worker'; "
            f"assert regex.__file__.startswith({str(site_packages)!r}); "
            f"assert sys.modules[ToolManager.__module__].__file__.startswith({str(site_packages)!r})",
        ),
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert probe.returncode == 0, probe.stderr


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
