from __future__ import annotations

import base64
import hashlib
from importlib import metadata
import json
import os
from pathlib import Path
import runpy
import shutil
import stat
import struct
import subprocess
import sys
from types import SimpleNamespace
import zipfile

import pytest

from ecorex import __version__
from ecorex.release.builder import _build_deterministic_zip
from ecorex.update.storage import _extract_zip_safely

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "build-v1-manual-webui.py"


def _builder() -> dict[str, object]:
    return runpy.run_path(str(SCRIPT))


def _pe_with_import(name: str) -> bytes:
    payload = bytearray(0x400)
    payload[:2] = b"MZ"
    struct.pack_into("<I", payload, 0x3C, 0x80)
    payload[0x80:0x84] = b"PE\0\0"
    struct.pack_into("<H12xH", payload, 0x86, 1, 0xF0)
    optional = 0x98
    struct.pack_into("<H106xI8xI", payload, optional, 0x20B, 16, 0x1000)
    section = optional + 0xF0
    struct.pack_into("<IIII", payload, section + 8, 0x200, 0x1000, 0x200, 0x200)
    struct.pack_into("<I", payload, 0x20C, 0x1050)
    payload[0x250 : 0x251 + len(name)] = name.encode("ascii") + b"\0"
    return bytes(payload)


def test_manual_webui_requires_pe_imported_msvc_beside_pack_python(
    tmp_path: Path,
) -> None:
    builder = _builder()
    pack_python = tmp_path / "core/bin/pack-python"
    extension = pack_python / "Lib/site-packages/greenlet/_greenlet.pyd"
    extension.parent.mkdir(parents=True)
    extension.write_bytes(_pe_with_import("MSVCP140.dll"))

    with pytest.raises(
        builder["ManualWebUIBuildError"],
        match="manual_webui_windows_msvc_closure_invalid",
    ):
        builder["_require_windows_msvc_closure"](tmp_path / "core")

    (pack_python / "msvcp140.dll").write_bytes(b"microsoft-signed-runtime")
    builder["_require_windows_msvc_closure"](tmp_path / "core")


def test_manual_webui_release_core_keeps_bound_msvc(tmp_path: Path) -> None:
    builder = _builder()
    archive = tmp_path / "core.zip"
    payload = b"caller-bound-msvcp140"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("bin/pack-python/msvcp140.dll", payload)
    artifact = SimpleNamespace(artifact_id="core-windows-x64")
    built = SimpleNamespace(
        manifest=SimpleNamespace(artifacts=(artifact,)),
        artifact_paths={artifact.artifact_id: archive},
    )

    builder["_verify_release_windows_msvc"](
        built, hashlib.sha256(payload).hexdigest()
    )
    with pytest.raises(
        builder["ManualWebUIBuildError"],
        match="manual_webui_windows_msvc_closure_invalid",
    ):
        builder["_verify_release_windows_msvc"](built, "0" * 64)


def test_manual_webui_reuses_stager_native_receipt_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    builder = _builder()
    native = tmp_path / "native"
    native.mkdir()
    (native / "msvcp140.dll").write_bytes(b"microsoft-signed-runtime")
    (native / "native-build-receipt.json").write_text("{}", encoding="utf-8")
    calls = []

    def validate(output, **kwargs):  # noqa: ANN001
        calls.append((output, kwargs))

    monkeypatch.setattr(
        builder["runpy"],
        "run_path",
        lambda path: {"_validate_windows_native_receipt": validate},
    )

    library, evidence = builder["_validated_windows_native_runtime"](ROOT, native)

    assert library == native / "msvcp140.dll"
    assert evidence["msvcp140_sha256"] == hashlib.sha256(library.read_bytes()).hexdigest()
    assert calls[0][0] == native
    assert calls[0][1]["toolchain_manifest"] == (
        ROOT / "platform-staging/native/windows/toolchain-manifest.json"
    )
    assert calls[0][1]["source_root"] == ROOT / "platform-staging/native/windows"
    assert calls[0][1]["github_hosted_compatibility"] is True


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


@pytest.mark.parametrize(
    ("platform", "executable"),
    (
        ("macos", "chrome-mac/headless_shell"),
        ("windows", "chrome-win/headless_shell.exe"),
    ),
)
def test_manual_webui_moves_verified_browser_runtime_into_core(
    tmp_path: Path,
    platform: str,
    executable: str,
) -> None:
    builder = _builder()
    browser_runtime = tmp_path / "browser-runtime.zip"
    member = f"browser/chromium_headless_shell-1169/{executable}"
    with zipfile.ZipFile(browser_runtime, "w") as archive:
        info = zipfile.ZipInfo(member)
        info.create_system = 3
        info.external_attr = (stat.S_IFREG | 0o755) << 16
        archive.writestr(info, b"browser")
    predecessor_pack = tmp_path / "browser-pack.zip"
    descriptor = json.dumps(
        {"archive_sha256": hashlib.sha256(browser_runtime.read_bytes()).hexdigest()}
    )
    with zipfile.ZipFile(predecessor_pack, "w") as archive:
        for name, payload in (
            ("browser-runtime.json", descriptor.encode()),
            ("browser-runtime.zip", browser_runtime.read_bytes()),
        ):
            info = zipfile.ZipInfo(name)
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | 0o644) << 16
            archive.writestr(info, payload)
    core = tmp_path / "core"
    core.mkdir()

    builder["_install_bundled_browser_runtime"](
        core,
        predecessor_pack,
        tmp_path / "stage",
        platform=platform,
    )

    bundled = core / "ms-playwright" / "chromium_headless_shell-1169" / executable
    assert bundled.read_bytes() == b"browser"
    assert bundled.relative_to(core).as_posix() in builder["_core_executable_paths"](
        platform,
        core,
    )


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


def test_manual_webui_runtime_config_rebuilds_exact_cow_pack_projection(
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
                        "pack_id": pack_id,
                        "artifact": (
                            f"capability-packs/{pack_id}/ecorex-capability-pack-"
                            f"{pack_id}-macos-arm64-0.3.2.zip"
                        ),
                        "manifest": (
                            f"capability-packs/{pack_id}/ecorex-capability-pack-"
                            f"{pack_id}-macos-arm64-0.3.2.json"
                        ),
                    }
                    for pack_id in (
                        "browser",
                        "channels",
                        "image",
                        "ocr",
                        "office",
                        "sandbox",
                    )
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
            "pack_id": pack_id,
            "artifact": (
                f"capability-packs/{pack_id}/ecorex-capability-pack-{pack_id}-"
                f"macos-arm64-{__version__}.zip"
            ),
            "manifest": (
                f"capability-packs/{pack_id}/ecorex-capability-pack-{pack_id}-"
                f"macos-arm64-{__version__}.json"
            ),
        }
        for pack_id in ("channels", "image", "ocr", "office")
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
    assert sources[0].base_url == (
        "https://pub-ada3f610c0234a76838f4e19fe2bb25e.r2.dev/"
        f"desktop/v{__version__}"
    )
    assert sources[1].base_url.endswith(f"/EcoreX/releases/download/v{__version__}")
    assert sources[2].base_url == "https://dl.ecoremedia.net/e-mate/update"


def test_manual_webui_bootstrap_uses_same_origin_public_index() -> None:
    source = (ROOT / "scripts/build-v1-manual-webui.py").read_text(encoding="utf-8")

    assert (
        'public_url = "https://dl.ecoremedia.net/e-mate/update/'
        'public-bootstrap-index.json"'
    ) in source
    assert "mvdcm.ecoremedia.net/e-mate/update/public-bootstrap-index.json" not in source


def test_manual_webui_macos_core_keeps_runtime_entries_executable() -> None:
    builder = _builder()

    assert builder["_core_executable_paths"]("macos") == (
        "bin/ecorex",
        "bin/pack-python/bin/python3",
        "bin/pack-python/lib/python3.11/site-packages/playwright/driver/node",
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
    # Cow desktop ships Playwright's SDK/driver in Core, not a Browser Pack.
    assert 300 * 1024 * 1024 <= builder["MAX_CORE_EXPANDED_BYTES"]
    go_source = (ROOT / "platform-staging/bootstrap/main.go").read_text()
    assert "maxCoreArchiveBytes  = 256 * 1024 * 1024" in go_source
    assert "maxCoreExpandedBytes = 640 * 1024 * 1024" in go_source

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


def test_manual_webui_runtime_overlay_targets_supported_intel_macos(
    tmp_path: Path,
) -> None:
    builder = _builder()
    commands: list[tuple[str, ...]] = []

    def stop_after_command(command, **_kwargs):  # noqa: ANN001
        commands.append(tuple(command))
        raise RuntimeError("captured")

    builder["_install_locked_runtime_overlay"].__globals__["_run"] = stop_after_command
    with pytest.raises(RuntimeError, match="captured"):
        builder["_install_locked_runtime_overlay"](
            ROOT,
            tmp_path / "core",
            tmp_path,
            platform="macos",
            architecture="x64",
        )

    command = commands[0]
    assert command[command.index("--platform") + 1] == "macosx_11_0_x86_64"


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits are a macOS runtime contract")
def test_manual_webui_core_stages_executable_playwright_driver_on_macos(
    tmp_path: Path,
) -> None:
    builder = _builder()
    driver_paths = {
        "macos": "bin/pack-python/lib/python3.11/site-packages/playwright/driver/node",
        "windows": "bin/pack-python/Lib/site-packages/playwright/driver/node.exe",
    }
    expected_modes = {"macos": 0o755, "windows": 0o644}

    for platform, driver_path in driver_paths.items():
        core = tmp_path / platform / "core"
        driver = core / driver_path
        driver.parent.mkdir(parents=True)
        driver.write_bytes(b"driver")
        for executable_path in builder["_core_executable_paths"](platform):
            executable = core / executable_path
            executable.parent.mkdir(parents=True, exist_ok=True)
            executable.write_bytes(b"executable")
        package = tmp_path / f"core-{platform}.zip"
        _build_deterministic_zip(
            source=core,
            destination=package,
            executable_paths=builder["_core_executable_paths"](platform),
            size_limit=1024 * 1024,
            expanded_limit=1024 * 1024,
        )
        staged = tmp_path / platform / "staged"
        staged.mkdir()
        _extract_zip_safely(
            package,
            staged,
            max_members=100,
            max_unpacked_bytes=1024 * 1024,
        )

        assert stat.S_IMODE((staged / driver_path).stat().st_mode) == expected_modes[platform]


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
    assert (site_packages / "agent/tools/search_files/search_files.py").read_bytes() == (
        ROOT / "agent/tools/search_files/search_files.py"
    ).read_bytes()
    assert any((site_packages / "regex").glob("_regex.*"))


def test_manual_webui_product_probe_isolated_from_signed_core(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    builder = _builder()
    function_globals = builder["_prepare_stages"].__globals__
    monkeypatch.setattr(function_globals["sys"], "platform", "darwin")
    monkeypatch.setattr(function_globals["host_platform"], "machine", lambda: "arm64")
    monkeypatch.setitem(function_globals, "TARGETS", (("macos", "arm64"),))
    monkeypatch.setitem(function_globals, "PACK_TOOLS", {})
    for name in (
        "_install_locked_runtime_overlay",
        "_install_cow_runtime_overlay",
        "_install_bundled_browser_runtime",
        "_replace_product_imports",
        "_replace_builtin_skills",
        "_runtime_config",
    ):
        monkeypatch.setitem(function_globals, name, lambda *args, **kwargs: None)
    monkeypatch.setitem(
        function_globals, "build_pack_python_manifest", lambda *args, **kwargs: b"{}"
    )
    monkeypatch.setitem(
        function_globals,
        "resolve_pack_python",
        lambda *args, **kwargs: (Path(sys.executable), None),
    )
    python_zip = tmp_path / "python311.zip"
    with zipfile.ZipFile(python_zip, "w"):
        pass
    core_archive = tmp_path / "core.zip"
    with zipfile.ZipFile(core_archive, "w") as archive:
        member = zipfile.ZipInfo("bin/python311.zip", builder["FIXED_TIME"])
        member.create_system = 3
        member.external_attr = (0o100644) << 16
        archive.writestr(member, python_zip.read_bytes())

    calls: list[tuple[Path, Path, int]] = []

    def fake_run(command, **kwargs):  # noqa: ANN001
        data_dir = Path(kwargs["environment"]["EMATE_DATA_DIR"])
        assert data_dir.is_dir()
        (data_dir / "run.log").write_text("probe", encoding="utf-8")
        calls.append((Path(kwargs["cwd"]), data_dir, kwargs["timeout"]))
        return b""

    monkeypatch.setitem(function_globals, "_run", fake_run)
    stage_root = tmp_path / "stages"
    builder["_prepare_stages"](
        ROOT,
        {
            "core-macos-arm64": core_archive,
            "capability-pack-browser-macos-arm64": core_archive,
        },
        stage_root,
        {},
    )

    core = stage_root / "macos-arm64" / "core"
    assert len(calls) == 1
    assert calls[0][0] == core
    assert calls[0][1].parent == core.parent
    assert calls[0][2] == 120
    assert not calls[0][1].exists()
    assert not (core / "run.log").exists()


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


def retired_legacy_manual_webui_rebuilds_browser_pack_from_current_cow_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    builder = _builder()
    function_globals = builder["_prepare_stages"].__globals__
    monkeypatch.setitem(function_globals, "TARGETS", (("windows", "x64"),))
    monkeypatch.setitem(
        function_globals,
        "PACK_TOOLS",
        {"browser": ("browser", "web_fetch", "web_search")},
    )
    for name in (
        "_install_locked_runtime_overlay",
        "_install_cow_runtime_overlay",
        "_replace_product_imports",
        "_replace_builtin_skills",
        "_runtime_config",
    ):
        monkeypatch.setitem(function_globals, name, lambda *args, **kwargs: None)
    monkeypatch.setitem(
        function_globals, "build_pack_python_manifest", lambda *args, **kwargs: b"{}"
    )
    monkeypatch.setitem(
        function_globals,
        "resolve_pack_python",
        lambda *args, **kwargs: (Path(sys.executable), None),
    )
    monkeypatch.setitem(
        function_globals, "_require_windows_msvc_closure", lambda *args: None
    )

    python_zip = tmp_path / "python311.zip"
    with zipfile.ZipFile(python_zip, "w"):
        pass

    def write_member(archive: zipfile.ZipFile, name: str, payload: bytes) -> None:
        member = zipfile.ZipInfo(name, builder["FIXED_TIME"])
        member.create_system = 3
        member.external_attr = (0o100644) << 16
        archive.writestr(member, payload)

    core = tmp_path / "core.zip"
    with zipfile.ZipFile(core, "w") as archive:
        write_member(archive, "bin/python311.zip", python_zip.read_bytes())

    runtime_manifest = b'{"verified":"predecessor"}'
    runtime_archive = b"verified predecessor browser runtime"
    browser = tmp_path / "browser.zip"
    with zipfile.ZipFile(browser, "w") as archive:
        write_member(archive, "__main__.py", b"run('browser', {'cdp', 'fetch'}, handle)\n")
        write_member(archive, "browser_pack.py", b"def handle(request): return request\n")
        write_member(
            archive,
            "ecorex-pack.json",
            b'{"pack_id":"browser","protocol":"ecorex-stdio-tool-v1",'
            b'"runtime_api_version":"1.0.0","schema_version":1,'
            b'"tools":["cdp","fetch"]}',
        )
        write_member(archive, "ecorex_pack_protocol.py", b"stale = True\n")
        write_member(archive, "stale.py", b"stale = True\n")
        write_member(archive, "browser-runtime.json", runtime_manifest)
        write_member(archive, "browser-runtime.zip", runtime_archive)

    (tmp_path / "msvcp140.dll").write_bytes(b"verified-msvc-runtime")

    stages = builder["_prepare_stages"](
        ROOT,
        {
            "core-windows-x64": core,
            "capability-pack-browser-windows-x64": browser,
        },
        tmp_path / "stages",
        {},
        tmp_path / "msvcp140.dll",
    )
    pack = stages[("windows", "x64")]["browser"]
    source = ROOT / "release" / "capability-packs"

    assert (pack / "__main__.py").read_bytes() == (
        source / "browser" / "__main__.py"
    ).read_bytes()
    assert (pack / "browser_pack.py").read_bytes() == (
        source / "browser" / "browser_pack.py"
    ).read_bytes()
    assert (pack / "ecorex_pack_protocol.py").read_bytes() == (
        source / "common" / "ecorex_pack_protocol.py"
    ).read_bytes()
    expected_descriptor = {
        "schema_version": 1,
        "protocol": "ecorex-stdio-tool-v1",
        "pack_id": "browser",
        "runtime_api_version": "1.0.0",
        "tools": ["browser", "web_fetch", "web_search"],
    }
    assert (pack / "ecorex-pack.json").read_bytes() == json.dumps(
        expected_descriptor,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    assert (pack / "browser-runtime.json").read_bytes() == runtime_manifest
    assert (pack / "browser-runtime.zip").read_bytes() == runtime_archive
    assert not (pack / "stale.py").exists()


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
