import importlib.util
import io
import json
import tarfile
import zipfile
from pathlib import Path

import pytest


def _load_script(name: str):
    path = Path(__file__).resolve().parents[1] / "scripts" / name
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


write_manifest = _load_script("write-v025-runtime-manifest.py")
check_manifest = _load_script("check-v025-runtime-manifest.py")
release_validator = _load_script("validate-ecorex-release-artifacts.py")


def test_v025_release_validator_accepts_refactored_active_requests_route():
    files = {
        "channel/web/routes.py": "WEB_ROUTES = ('/api/active-requests', 'ActiveRequestsHandler')",
        "channel/web/projection.py": (
            "class ActiveRequestsHandler:\n"
            "    def GET(self):\n"
            "        return channel.active_requests_snapshot()\n"
        ),
    }

    release_validator.validate_active_requests_route(
        lambda suffix: files[suffix],
        "def active_requests_snapshot(self):\n    return {}\n",
        "unit",
    )


def test_v025_release_validator_rejects_missing_refactored_active_requests_route():
    files = {
        "channel/web/routes.py": "WEB_ROUTES = ()",
        "channel/web/projection.py": "class ActiveRequestsHandler: pass\n",
    }

    with pytest.raises(release_validator.ValidationError):
        release_validator.validate_active_requests_route(
            lambda suffix: files[suffix],
            "def active_requests_snapshot(self):\n    return {}\n",
            "unit",
        )


def test_v027_runtime_update_state_payload_is_redacted_and_refreshable(monkeypatch, tmp_path):
    pytest.importorskip("web")
    from channel.web import web_channel

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    monkeypatch.setenv("ECOREX_WEBUI_STATE_DIR", str(state_dir))
    (state_dir / "update-state.json").write_text(
        json.dumps(
            {
                "product": "EcoreX WebUI",
                "version": "0.2.7",
                "mode": "background",
                "status": "installed",
                "reason": "",
                "url": "http://127.0.0.1:9909/app/?token=secret",
                "browserAction": "defer-to-existing-tab-soft-refresh",
                "activationPolicy": "prompt-soft-refresh-existing-tab",
                "healthCheck": {"endpoint": "/api/version", "status": "pass", "passed": True},
                "generatedAt": "2026-07-02T00:00:00Z",
            }
        ),
        encoding="utf-8",
    )

    payload = web_channel._runtime_update_state_payload()

    assert payload["stateAvailable"] is True
    assert payload["mode"] == "background"
    assert payload["status"] == "installed"
    assert payload["browserAction"] == "defer-to-existing-tab-soft-refresh"
    assert payload["activationPolicy"] == "prompt-soft-refresh-existing-tab"
    assert payload["healthCheck"]["passed"] is True
    assert payload["refreshRequired"] is True
    assert payload["url"] == "http://127.0.0.1:9909/app/"
    assert str(tmp_path) not in json.dumps(payload, ensure_ascii=False)


def _touch(path: Path, text: str = "") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _write_zip(path: Path, members: dict[str, str]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        for name, text in members.items():
            archive.writestr(name, text)
    return path


def _write_tar_gz(path: Path, members: dict[str, str]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(path, "w:gz") as archive:
        for name, text in members.items():
            payload = text.encode("utf-8")
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
    return path


def _write_wheel(path: Path, dist: str | None = None) -> Path:
    dist_name = dist or path.name.split("-", 1)[0]
    return _write_zip(
        path,
        {
            f"{dist_name}-1.0.dist-info/WHEEL": "Wheel-Version: 1.0\n",
            f"{dist_name}/__init__.py": "",
        },
    )


def _write_node_archive(path: Path) -> Path:
    return _write_zip(
        path,
        {
            "node-v22-test-win-x64/node.exe": "",
            "node-v22-test-win-x64/npm.cmd": "",
            "node-v22-test-win-x64/npx.cmd": "",
        },
    )


def _bundle_python(runtime: Path) -> None:
    _touch(runtime / "python" / "python.exe")
    site = runtime / "python" / "Lib" / "site-packages"
    for module in ("aiohttp", "requests", "chardet", "numpy", "PIL", "pypdf", "pdfminer", "docx", "pptx", "openpyxl", "xlsxwriter", "markdownify", "reportlab", "fitz", "rapidocr_onnxruntime", "dotenv", "yaml", "croniter", "click", "qrcode", "json_repair", "playwright", "lark_oapi", "web", "legacy_cgi"):
        _touch(site / module / "__init__.py")


def _bundle_macos_installer(package_root: Path) -> None:
    _write_tar_gz(package_root / "python" / "cpython-3.11.15+test-aarch64-apple-darwin.tar.gz", {"Python.framework/Versions/3.11/bin/python3": ""})
    _write_tar_gz(package_root / "python" / "cpython-3.11.15+test-x86_64-apple-darwin.tar.gz", {"Python.framework/Versions/3.11/bin/python3": ""})
    for package in write_manifest.PYTHON_MODULES:
        wheel_name = write_manifest.normalized_distribution_name(package).replace("-", "_")
        _write_wheel(package_root / "wheelhouse" / "mac-arm64" / f"{wheel_name}-1.0-py3-none-any.whl", wheel_name)
        _write_wheel(package_root / "wheelhouse" / "mac-x64" / f"{wheel_name}-1.0-py3-none-any.whl", wheel_name)


def test_v025_runtime_manifest_windows_bundled_runtime_passes(tmp_path):
    runtime = tmp_path / "runtime"
    _bundle_python(runtime)

    manifest = write_manifest.build_manifest(runtime, tmp_path, "0.2.5", "windows-x64")

    assert manifest["runtimeDependencies"]["python"]["status"] == "bundled"
    assert manifest["toolStates"]["browser_mcp"]["status"] == "missing_dependency"
    check_manifest.check_manifest(manifest, expected_platform="windows-x64", expected_version="0.2.5")


def test_v025_runtime_manifest_rejects_python_directory_as_runtime(tmp_path):
    runtime = tmp_path / "runtime"
    (runtime / "python" / "python.exe").mkdir(parents=True)
    manifest = write_manifest.build_manifest(runtime, tmp_path, "0.2.5", "windows-x64")

    assert manifest["runtimeDependencies"]["python"]["status"] == "missing"
    with pytest.raises(check_manifest.ManifestError):
        check_manifest.check_manifest(manifest, expected_platform="windows-x64", expected_version="0.2.5", runtime_root=runtime, package_root=tmp_path)


def test_v025_runtime_manifest_macos_installer_bundle_passes(tmp_path):
    runtime = tmp_path / "package" / "runtime"
    package_root = tmp_path / "package"
    _bundle_macos_installer(package_root)

    manifest = write_manifest.build_manifest(runtime, package_root, "0.2.5", "macos-universal")

    assert manifest["runtimeDependencies"]["python"]["status"] == "installer-bundled"
    assert manifest["runtimeDependencies"]["pythonPackages"]["openpyxl"]["status"] == "installer-bundled"
    assert manifest["runtimeDependencies"]["pythonPackages"]["openpyxl"]["archives"]
    check_manifest.check_manifest(manifest, expected_platform="macos-universal", expected_version="0.2.5")


def test_v025_runtime_manifest_macos_accepts_root_python_archives(tmp_path):
    runtime = tmp_path / "package" / "runtime"
    package_root = tmp_path / "package"
    _bundle_macos_installer(package_root)
    for archive in list((package_root / "python").glob("cpython-*.tar.gz")):
        archive.rename(package_root / archive.name)

    manifest = write_manifest.build_manifest(runtime, package_root, "0.2.5", "macos-universal")

    assert manifest["runtimeDependencies"]["python"]["status"] == "installer-bundled"
    assert all(not item.startswith("python/") for item in manifest["runtimeDependencies"]["python"]["archives"])
    check_manifest.check_manifest(
        manifest,
        expected_platform="macos-universal",
        expected_version="0.2.5",
        runtime_root=runtime,
        package_root=package_root,
    )


def test_v025_runtime_manifest_macos_requires_both_wheelhouses(tmp_path):
    runtime = tmp_path / "package" / "runtime"
    package_root = tmp_path / "package"
    _write_tar_gz(package_root / "python" / "cpython-3.11.15+test-aarch64-apple-darwin.tar.gz", {"Python.framework/Versions/3.11/bin/python3": ""})
    for package in write_manifest.PYTHON_MODULES:
        wheel_name = write_manifest.normalized_distribution_name(package).replace("-", "_")
        _write_wheel(package_root / "wheelhouse" / "mac-arm64" / f"{wheel_name}-1.0-py3-none-any.whl", wheel_name)

    manifest = write_manifest.build_manifest(runtime, package_root, "0.2.5", "macos-universal")

    with pytest.raises(check_manifest.ManifestError):
        check_manifest.check_manifest(manifest, expected_platform="macos-universal", expected_version="0.2.5")


def test_v025_runtime_manifest_linux_ignores_macos_wheelhouse(tmp_path):
    runtime = tmp_path / "package" / "runtime"
    package_root = tmp_path / "package"
    _touch(runtime / "python" / "bin" / "python3")
    for package in write_manifest.PYTHON_MODULES:
        wheel_name = write_manifest.normalized_distribution_name(package).replace("-", "_")
        _write_wheel(package_root / "wheelhouse" / "mac-arm64" / f"{wheel_name}-1.0-py3-none-any.whl", wheel_name)
        _write_wheel(package_root / "wheelhouse" / "mac-x64" / f"{wheel_name}-1.0-py3-none-any.whl", wheel_name)

    manifest = write_manifest.build_manifest(runtime, package_root, "0.2.5", "linux-service")

    assert manifest["runtimeDependencies"]["pythonPackages"]["docx"]["status"] == "external-required"
    assert "archives" not in manifest["runtimeDependencies"]["pythonPackages"]["docx"]
    assert manifest["toolStates"]["office_pdf"]["status"] == "install-ready"
    check_manifest.check_manifest(manifest, expected_platform="linux-service", expected_version="0.2.5")

    manifest["toolStates"]["office_pdf"] = {"status": "ready", "missingDependencies": []}
    with pytest.raises(check_manifest.ManifestError):
        check_manifest.check_manifest(manifest, expected_platform="linux-service", expected_version="0.2.5")
    with pytest.raises(release_validator.ValidationError):
        release_validator.validate_v025_runtime_manifests([manifest], "web-linux-service", "0.2.5")

    manifest["toolStates"]["office_pdf"] = {"status": "install-ready", "missingDependencies": []}
    manifest["runtimeDependencies"]["pythonPackages"]["docx"] = {
        "package": "python-docx",
        "status": "installer-bundled",
        "archives": [
            "wheelhouse/mac-arm64/python_docx-1.0-py3-none-any.whl",
            "wheelhouse/mac-x64/python_docx-1.0-py3-none-any.whl",
        ],
    }
    with pytest.raises(check_manifest.ManifestError):
        check_manifest.check_manifest(manifest, expected_platform="linux-service", expected_version="0.2.5")
    with pytest.raises(release_validator.ValidationError):
        release_validator.validate_v025_runtime_manifests([manifest], "web-linux-service", "0.2.5")


def test_v025_runtime_manifest_linux_rejects_non_required_installer_bundled_package(tmp_path):
    runtime = tmp_path / "package" / "runtime"
    package_root = tmp_path / "package"
    _touch(runtime / "python" / "bin" / "python3")
    _write_wheel(package_root / "wheelhouse" / "mac-arm64" / "Pillow-1.0-py3-none-any.whl", "Pillow")
    manifest = write_manifest.build_manifest(runtime, package_root, "0.2.5", "linux-service")
    manifest["runtimeDependencies"]["pythonPackages"]["PIL"] = {
        "package": "Pillow",
        "status": "installer-bundled",
        "archives": ["wheelhouse/mac-arm64/Pillow-1.0-py3-none-any.whl"],
    }
    manifest["toolStates"]["ocr"] = {"status": "install-ready", "missingDependencies": []}

    with pytest.raises(check_manifest.ManifestError):
        check_manifest.check_manifest(manifest, expected_platform="linux-service", expected_version="0.2.5")
    with pytest.raises(release_validator.ValidationError):
        release_validator.validate_v025_runtime_manifests([manifest], "web-linux-service", "0.2.5")


def test_v025_runtime_manifest_macos_rejects_non_required_external_required_package(tmp_path):
    runtime = tmp_path / "package" / "runtime"
    package_root = tmp_path / "package"
    _bundle_macos_installer(package_root)
    manifest = write_manifest.build_manifest(runtime, package_root, "0.2.5", "macos-universal")
    manifest["runtimeDependencies"]["pythonPackages"]["aiohttp"] = {
        "package": "aiohttp",
        "status": "external-required",
    }

    with pytest.raises(check_manifest.ManifestError):
        check_manifest.check_manifest(manifest, expected_platform="macos-universal", expected_version="0.2.5")
    with pytest.raises(release_validator.ValidationError):
        release_validator.validate_v025_runtime_manifests([manifest], "webui-macos-universal", "0.2.5")


def test_v025_runtime_manifest_macos_requires_wheel_archive_evidence(tmp_path):
    runtime = tmp_path / "package" / "runtime"
    package_root = tmp_path / "package"
    _bundle_macos_installer(package_root)
    manifest = write_manifest.build_manifest(runtime, package_root, "0.2.5", "macos-universal")
    manifest["runtimeDependencies"]["pythonPackages"]["openpyxl"].pop("archives", None)

    with pytest.raises(check_manifest.ManifestError):
        check_manifest.check_manifest(manifest, expected_platform="macos-universal", expected_version="0.2.5")
    with pytest.raises(release_validator.ValidationError):
        release_validator.validate_v025_runtime_manifests([manifest], "webui-macos-universal", "0.2.5")


def test_v025_runtime_manifest_macos_rejects_external_required_packages(tmp_path):
    runtime = tmp_path / "package" / "runtime"
    package_root = tmp_path / "package"
    _bundle_macos_installer(package_root)
    manifest = write_manifest.build_manifest(runtime, package_root, "0.2.5", "macos-universal")
    for item in manifest["runtimeDependencies"]["pythonPackages"].values():
        item.clear()
        item.update({"package": "placeholder", "status": "external-required"})
    for package, module in write_manifest.PYTHON_MODULES.items():
        manifest["runtimeDependencies"]["pythonPackages"][module]["package"] = package
    manifest["toolStates"]["office_pdf"] = {"status": "install-ready", "missingDependencies": []}

    with pytest.raises(check_manifest.ManifestError):
        check_manifest.check_manifest(manifest, expected_platform="macos-universal", expected_version="0.2.5")
    with pytest.raises(release_validator.ValidationError):
        release_validator.validate_v025_runtime_manifests([manifest], "webui-macos-universal", "0.2.5")


def test_v025_runtime_manifest_rejects_install_ready_with_missing_tool_dependency(tmp_path):
    runtime = tmp_path / "package" / "runtime"
    package_root = tmp_path / "package"
    _bundle_macos_installer(package_root)
    manifest = write_manifest.build_manifest(runtime, package_root, "0.2.5", "macos-universal")
    manifest["runtimeDependencies"]["pythonPackages"]["pdfminer"] = {
        "package": "pdfminer.six",
        "status": "missing",
    }
    manifest["toolStates"]["office_pdf"] = {"status": "install-ready", "missingDependencies": []}

    with pytest.raises(check_manifest.ManifestError):
        check_manifest.check_manifest(manifest, expected_platform="macos-universal", expected_version="0.2.5")
    with pytest.raises(release_validator.ValidationError):
        release_validator.validate_v025_runtime_manifests([manifest], "webui-macos-universal", "0.2.5")


def test_v025_runtime_manifest_node_archive_does_not_make_lark_cli_ready(tmp_path):
    runtime = tmp_path / "runtime"
    _bundle_python(runtime)
    _write_node_archive(tmp_path / "node" / "node-v22-test-win-x64.zip")

    manifest = write_manifest.build_manifest(runtime, tmp_path, "0.2.5", "windows-x64")

    assert manifest["runtimeDependencies"]["executables"]["node"]["status"] == "installer-bundled"
    assert manifest["runtimeDependencies"]["executables"]["lark-cli"]["status"] == "missing"
    assert manifest["toolStates"]["feishu_cli"]["status"] == "discovery-only"
    check_manifest.check_manifest(manifest, expected_platform="windows-x64", expected_version="0.2.5")


def test_v025_runtime_manifest_rejects_ready_tool_with_missing_dependencies(tmp_path):
    runtime = tmp_path / "runtime"
    _bundle_python(runtime)
    manifest = write_manifest.build_manifest(runtime, tmp_path, "0.2.5", "windows-x64")
    manifest["toolStates"]["browser_mcp"] = {"status": "ready", "missingDependencies": ["npx"]}

    with pytest.raises(check_manifest.ManifestError):
        check_manifest.check_manifest(manifest, expected_platform="windows-x64", expected_version="0.2.5")
    with pytest.raises(release_validator.ValidationError):
        release_validator.validate_v025_runtime_manifests([manifest], "unit")


def test_v025_runtime_manifest_browser_mcp_requires_node(tmp_path):
    runtime = tmp_path / "runtime"
    _bundle_python(runtime)
    manifest = write_manifest.build_manifest(runtime, tmp_path, "0.2.5", "windows-x64")
    manifest["runtimeDependencies"]["executables"]["npx"] = {"status": "bundled", "path": "node/npx.cmd"}
    manifest["runtimeDependencies"]["executables"]["node"] = {"status": "missing"}
    manifest["toolStates"]["browser_mcp"] = {"status": "ready", "missingDependencies": []}

    with pytest.raises(check_manifest.ManifestError):
        check_manifest.check_manifest(manifest, expected_platform="windows-x64", expected_version="0.2.5")


def test_v025_runtime_manifest_rejects_installer_bundled_without_archive_evidence(tmp_path):
    runtime = tmp_path / "runtime"
    _bundle_python(runtime)
    manifest = write_manifest.build_manifest(runtime, tmp_path, "0.2.5", "windows-x64")
    manifest["runtimeDependencies"]["executables"]["node"] = {"status": "installer-bundled"}
    manifest["runtimeDependencies"]["executables"]["npx"] = {"status": "installer-bundled"}
    manifest["toolStates"]["browser_mcp"] = {"status": "ready", "missingDependencies": []}

    with pytest.raises(check_manifest.ManifestError):
        check_manifest.check_manifest(manifest, expected_platform="windows-x64", expected_version="0.2.5")
    with pytest.raises(release_validator.ValidationError):
        release_validator.validate_v025_runtime_manifests([manifest], "webui-windows-x64", "0.2.5")


def test_v025_runtime_manifest_rejects_scalar_archives(tmp_path):
    runtime = tmp_path / "runtime"
    _bundle_python(runtime)
    manifest = write_manifest.build_manifest(runtime, tmp_path, "0.2.5", "windows-x64")
    manifest["runtimeDependencies"]["executables"]["node"] = {"status": "installer-bundled", "archives": "node/node.zip"}

    with pytest.raises(check_manifest.ManifestError):
        check_manifest.check_manifest(manifest, expected_platform="windows-x64", expected_version="0.2.5")
    with pytest.raises(release_validator.ValidationError):
        release_validator.validate_v025_runtime_manifests([manifest], "webui-windows-x64", "0.2.5")


def test_v025_runtime_manifest_rejects_absolute_paths(tmp_path):
    runtime = tmp_path / "runtime"
    _bundle_python(runtime)
    manifest = write_manifest.build_manifest(runtime, tmp_path, "0.2.5", "windows-x64")
    manifest["runtimeDependencies"]["python"]["path"] = "C:/Users/example/python.exe"

    with pytest.raises(check_manifest.ManifestError):
        check_manifest.check_manifest(manifest, expected_platform="windows-x64", expected_version="0.2.5")


def test_v025_runtime_manifest_rejects_directory_for_executable_reference(tmp_path):
    runtime = tmp_path / "runtime"
    _bundle_python(runtime)
    (runtime / "node" / "node.exe").mkdir(parents=True)
    manifest = write_manifest.build_manifest(runtime, tmp_path, "0.2.5", "windows-x64")
    manifest["runtimeDependencies"]["executables"]["node"] = {"status": "bundled", "path": "node/node.exe"}

    with pytest.raises(check_manifest.ManifestError):
        check_manifest.check_manifest(
            manifest,
            expected_platform="windows-x64",
            expected_version="0.2.5",
            runtime_root=runtime,
            package_root=tmp_path,
        )


def test_release_validator_requires_expected_platform_manifests(tmp_path):
    runtime = tmp_path / "runtime"
    _bundle_python(runtime)
    windows_manifest = write_manifest.build_manifest(runtime, tmp_path, "0.2.5", "windows-x64")
    mac_manifest = write_manifest.build_manifest(tmp_path / "mac" / "runtime", tmp_path / "mac", "0.2.5", "macos-universal")
    mac_manifest["runtimeDependencies"]["python"]["status"] = "installer-bundled"
    mac_manifest["runtimeDependencies"]["python"]["architectures"] = ["mac-arm64", "mac-x64"]
    mac_manifest["runtimeDependencies"]["python"]["archives"] = [
        "python/cpython-3.11.15+test-aarch64-apple-darwin.tar.gz",
        "python/cpython-3.11.15+test-x86_64-apple-darwin.tar.gz",
    ]
    for item in mac_manifest["runtimeDependencies"]["pythonPackages"].values():
        item["status"] = "installer-bundled"
        item["wheelhouse"] = ["mac-arm64", "mac-x64"]
        dist = write_manifest.normalized_distribution_name(str(item.get("package") or "pkg")).replace("-", "_")
        item["archives"] = [f"wheelhouse/mac-arm64/{dist}-1.0-py3-none-any.whl", f"wheelhouse/mac-x64/{dist}-1.0-py3-none-any.whl"]
    mac_manifest["toolStates"]["office_pdf"] = {"status": "install-ready", "missingDependencies": []}
    mac_manifest["toolStates"]["ocr"] = {"status": "install-ready", "missingDependencies": []}
    mac_manifest["toolStates"]["browser_mcp"] = {"status": "missing_dependency", "missingDependencies": ["node", "npx"]}
    mac_manifest["toolStates"]["feishu_cli"] = {"status": "discovery-only", "missingDependencies": []}
    mac_manifest["releaseGate"]["installReady"] = True

    with pytest.raises(release_validator.ValidationError):
        release_validator.validate_v025_runtime_manifests([windows_manifest], "webui-win-mac", "0.2.5")
    release_validator.validate_v025_runtime_manifests([windows_manifest, mac_manifest], "webui-win-mac", "0.2.5")


def test_release_validator_rejects_missing_archive_references(tmp_path):
    runtime = tmp_path / "runtime"
    _bundle_python(runtime)
    manifest = write_manifest.build_manifest(runtime, tmp_path, "0.2.5", "windows-x64")
    manifest["runtimeDependencies"]["executables"]["node"] = {"status": "bundled", "path": "node/node.exe"}
    manifest["runtimeDependencies"]["executables"]["npx"] = {"status": "bundled", "path": "node/npx.cmd"}
    manifest["toolStates"]["browser_mcp"] = {"status": "ready", "missingDependencies": []}
    archive_names = {
        "pkg/runtime/runtime-manifest.json",
        "pkg/runtime/python/python.exe",
    }

    with pytest.raises(release_validator.ValidationError):
        release_validator.validate_v025_runtime_manifests(
            [("pkg/runtime/runtime-manifest.json", manifest)],
            "webui-windows-x64",
            "0.2.5",
            archive_names,
        )


def test_v025_runtime_manifest_rejects_wheelhouse_evidence_for_executable(tmp_path):
    runtime = tmp_path / "runtime"
    _bundle_python(runtime)
    manifest = write_manifest.build_manifest(runtime, tmp_path, "0.2.5", "windows-x64")
    manifest["runtimeDependencies"]["executables"]["node"] = {"status": "installer-bundled", "wheelhouse": ["mac-arm64", "mac-x64"]}

    with pytest.raises(check_manifest.ManifestError):
        check_manifest.check_manifest(manifest, expected_platform="windows-x64", expected_version="0.2.5")
    with pytest.raises(release_validator.ValidationError):
        release_validator.validate_v025_runtime_manifests([manifest], "webui-windows-x64", "0.2.5")


def test_v025_runtime_manifest_rejects_non_string_archive_entries(tmp_path):
    runtime = tmp_path / "runtime"
    _bundle_python(runtime)
    manifest = write_manifest.build_manifest(runtime, tmp_path, "0.2.5", "windows-x64")
    manifest["runtimeDependencies"]["executables"]["node"] = {"status": "installer-bundled", "archives": [123]}

    with pytest.raises(check_manifest.ManifestError):
        check_manifest.check_manifest(manifest, expected_platform="windows-x64", expected_version="0.2.5")
    with pytest.raises(release_validator.ValidationError):
        release_validator.validate_v025_runtime_manifests([manifest], "webui-windows-x64", "0.2.5")


def test_v025_runtime_manifest_rejects_wrong_mac_wheel_distribution(tmp_path):
    runtime = tmp_path / "package" / "runtime"
    package_root = tmp_path / "package"
    _bundle_macos_installer(package_root)
    manifest = write_manifest.build_manifest(runtime, package_root, "0.2.5", "macos-universal")
    manifest["runtimeDependencies"]["pythonPackages"]["openpyxl"]["archives"] = [
        "wheelhouse/mac-arm64/playwright-1.0-py3-none-any.whl",
        "wheelhouse/mac-x64/playwright-1.0-py3-none-any.whl",
    ]

    with pytest.raises(check_manifest.ManifestError):
        check_manifest.check_manifest(manifest, expected_platform="macos-universal", expected_version="0.2.5")
    with pytest.raises(release_validator.ValidationError):
        release_validator.validate_v025_runtime_manifests([manifest], "webui-macos-universal", "0.2.5")


def test_v025_runtime_manifest_rejects_forged_macos_python_architectures(tmp_path):
    runtime = tmp_path / "package" / "runtime"
    package_root = tmp_path / "package"
    _bundle_macos_installer(package_root)
    manifest = write_manifest.build_manifest(runtime, package_root, "0.2.5", "macos-universal")
    manifest["runtimeDependencies"]["python"]["architectures"] = ["mac-arm64", "mac-x64"]
    manifest["runtimeDependencies"]["python"]["archives"] = ["python/cpython-no-mac-arch-evidence.tar.gz"]

    with pytest.raises(check_manifest.ManifestError):
        check_manifest.check_manifest(manifest, expected_platform="macos-universal", expected_version="0.2.5")
    with pytest.raises(release_validator.ValidationError):
        release_validator.validate_v025_runtime_manifests([manifest], "webui-macos-universal", "0.2.5")


def test_v025_runtime_manifest_rejects_ambiguous_macos_python_archive(tmp_path):
    runtime = tmp_path / "package" / "runtime"
    package_root = tmp_path / "package"
    _write_tar_gz(package_root / "python" / "cpython-3.11.15-test-arm64-x64-apple-darwin.tar.gz", {"Python.framework/Versions/3.11/bin/python3": ""})
    for package in write_manifest.PYTHON_MODULES:
        wheel_name = write_manifest.normalized_distribution_name(package).replace("-", "_")
        _write_wheel(package_root / "wheelhouse" / "mac-arm64" / f"{wheel_name}-1.0-py3-none-any.whl", wheel_name)
        _write_wheel(package_root / "wheelhouse" / "mac-x64" / f"{wheel_name}-1.0-py3-none-any.whl", wheel_name)

    manifest = write_manifest.build_manifest(runtime, package_root, "0.2.5", "macos-universal")

    assert manifest["runtimeDependencies"]["python"]["status"] == "missing"
    manifest["runtimeDependencies"]["python"] = {
        "status": "installer-bundled",
        "archives": ["python/cpython-3.11.15-test-arm64-x64-apple-darwin.tar.gz"],
        "architectures": ["mac-arm64", "mac-x64"],
    }
    with pytest.raises(check_manifest.ManifestError):
        check_manifest.check_manifest(manifest, expected_platform="macos-universal", expected_version="0.2.5")
    with pytest.raises(release_validator.ValidationError):
        release_validator.validate_v025_runtime_manifests([manifest], "webui-macos-universal", "0.2.5")


def test_v025_runtime_manifest_rejects_mutable_package_metadata(tmp_path):
    runtime = tmp_path / "package" / "runtime"
    package_root = tmp_path / "package"
    _bundle_macos_installer(package_root)
    manifest = write_manifest.build_manifest(runtime, package_root, "0.2.5", "macos-universal")
    manifest["runtimeDependencies"]["pythonPackages"]["openpyxl"]["package"] = "playwright"

    with pytest.raises(check_manifest.ManifestError):
        check_manifest.check_manifest(manifest, expected_platform="macos-universal", expected_version="0.2.5")
    with pytest.raises(release_validator.ValidationError):
        release_validator.validate_v025_runtime_manifests([manifest], "webui-macos-universal", "0.2.5")


def test_v025_runtime_manifest_rejects_missing_package_metadata(tmp_path):
    runtime = tmp_path / "package" / "runtime"
    package_root = tmp_path / "package"
    _bundle_macos_installer(package_root)
    manifest = write_manifest.build_manifest(runtime, package_root, "0.2.5", "macos-universal")
    manifest["runtimeDependencies"]["pythonPackages"]["docx"].pop("package")

    with pytest.raises(check_manifest.ManifestError):
        check_manifest.check_manifest(manifest, expected_platform="macos-universal", expected_version="0.2.5")
    with pytest.raises(release_validator.ValidationError):
        release_validator.validate_v025_runtime_manifests([manifest], "webui-macos-universal", "0.2.5")


def test_v025_runtime_manifest_rejects_bundled_package_path_mismatch(tmp_path):
    runtime = tmp_path / "runtime"
    _bundle_python(runtime)
    manifest = write_manifest.build_manifest(runtime, tmp_path, "0.2.5", "windows-x64")
    manifest["runtimeDependencies"]["pythonPackages"]["openpyxl"]["path"] = "agent/tools/read/read.py"

    with pytest.raises(check_manifest.ManifestError):
        check_manifest.check_manifest(manifest, expected_platform="windows-x64", expected_version="0.2.5")
    with pytest.raises(release_validator.ValidationError):
        release_validator.validate_v025_runtime_manifests([manifest], "webui-windows-x64", "0.2.5")


def test_v025_runtime_manifest_rejects_bundled_package_outside_site_packages(tmp_path):
    runtime = tmp_path / "runtime"
    _bundle_python(runtime)
    manifest = write_manifest.build_manifest(runtime, tmp_path, "0.2.5", "windows-x64")
    manifest["runtimeDependencies"]["pythonPackages"]["docx"]["path"] = "tools/docx/__init__.py"

    with pytest.raises(check_manifest.ManifestError):
        check_manifest.check_manifest(manifest, expected_platform="windows-x64", expected_version="0.2.5")
    with pytest.raises(release_validator.ValidationError):
        release_validator.validate_v025_runtime_manifests([manifest], "webui-windows-x64", "0.2.5")


def test_v025_runtime_manifest_rejects_empty_package_directories(tmp_path):
    runtime = tmp_path / "runtime"
    _touch(runtime / "python" / "python.exe")
    site = runtime / "python" / "Lib" / "site-packages"
    for module in ("aiohttp", "requests", "PIL", "pypdf", "pdfminer", "docx", "pptx", "openpyxl", "xlsxwriter", "markdownify", "reportlab", "fitz", "rapidocr_onnxruntime", "dotenv", "yaml", "croniter", "click", "qrcode", "json_repair", "playwright", "lark_oapi"):
        (site / module).mkdir(parents=True, exist_ok=True)

    manifest = write_manifest.build_manifest(runtime, tmp_path, "0.2.5", "windows-x64")

    assert manifest["runtimeDependencies"]["pythonPackages"]["docx"]["status"] == "missing"
    with pytest.raises(check_manifest.ManifestError):
        check_manifest.check_manifest(manifest, expected_platform="windows-x64", expected_version="0.2.5")


def test_v025_runtime_manifest_rejects_init_py_directory(tmp_path):
    runtime = tmp_path / "runtime"
    _bundle_python(runtime)
    init_file = runtime / "python" / "Lib" / "site-packages" / "docx" / "__init__.py"
    init_file.unlink()
    init_file.mkdir()
    manifest = write_manifest.build_manifest(runtime, tmp_path, "0.2.5", "windows-x64")
    manifest["runtimeDependencies"]["pythonPackages"]["docx"] = {
        "package": "python-docx",
        "status": "bundled",
        "path": "python/Lib/site-packages/docx/__init__.py",
    }

    with pytest.raises(check_manifest.ManifestError):
        check_manifest.check_manifest(
            manifest,
            expected_platform="windows-x64",
            expected_version="0.2.5",
            runtime_root=runtime,
            package_root=tmp_path,
        )
    with pytest.raises(release_validator.ValidationError):
        release_validator.validate_v025_runtime_manifests(
            [("pkg/runtime/runtime-manifest.json", manifest)],
            "webui-windows-x64",
            "0.2.5",
            {"pkg/runtime/runtime-manifest.json", "pkg/runtime/python/Lib/site-packages/docx/__init__.py"},
            {"pkg/runtime/runtime-manifest.json"},
        )


def test_v025_runtime_manifest_rejects_runtime_identity_spoofing(tmp_path):
    runtime = tmp_path / "runtime"
    _bundle_python(runtime)
    manifest = write_manifest.build_manifest(runtime, tmp_path, "0.2.5", "windows-x64")
    manifest["runtimeDependencies"]["python"]["path"] = "scripts/check-v025-runtime-manifest.py"
    manifest["runtimeDependencies"]["executables"]["node"] = {"status": "bundled", "path": "scripts/check-v025-runtime-manifest.py"}
    manifest["runtimeDependencies"]["executables"]["npx"] = {"status": "bundled", "path": "scripts/check-v025-runtime-manifest.py"}
    manifest["runtimeDependencies"]["toolFiles"]["tongxinCli"] = {"status": "bundled", "path": "scripts/check-v025-runtime-manifest.py"}
    manifest["toolStates"]["browser_mcp"] = {"status": "ready", "missingDependencies": []}
    manifest["toolStates"]["tongxin_cli"] = {"status": "ready", "missingDependencies": []}

    with pytest.raises(check_manifest.ManifestError):
        check_manifest.check_manifest(manifest, expected_platform="windows-x64", expected_version="0.2.5")
    with pytest.raises(release_validator.ValidationError):
        release_validator.validate_v025_runtime_manifests([manifest], "webui-windows-x64", "0.2.5")


def test_v025_runtime_manifest_rejects_lark_cli_node_archive_spoof(tmp_path):
    runtime = tmp_path / "runtime"
    _bundle_python(runtime)
    _write_node_archive(tmp_path / "node" / "node-v22-test-win-x64.zip")
    manifest = write_manifest.build_manifest(runtime, tmp_path, "0.2.5", "windows-x64")
    manifest["runtimeDependencies"]["executables"]["lark-cli"] = {
        "status": "installer-bundled",
        "archives": ["node/node-v22-test-win-x64.zip"],
    }
    manifest["toolStates"]["feishu_cli"] = {"status": "ready", "missingDependencies": []}

    with pytest.raises(check_manifest.ManifestError):
        check_manifest.check_manifest(manifest, expected_platform="windows-x64", expected_version="0.2.5")
    with pytest.raises(release_validator.ValidationError):
        release_validator.validate_v025_runtime_manifests([manifest], "webui-windows-x64", "0.2.5")


def test_v025_runtime_manifest_rejects_node_readme_as_installer_archive(tmp_path):
    runtime = tmp_path / "runtime"
    _bundle_python(runtime)
    _touch(tmp_path / "node" / "node-readme.txt")
    manifest = write_manifest.build_manifest(runtime, tmp_path, "0.2.5", "windows-x64")

    assert manifest["runtimeDependencies"]["executables"]["node"]["status"] == "missing"

    manifest["runtimeDependencies"]["executables"]["node"] = {
        "status": "installer-bundled",
        "archives": ["node/node-readme.txt"],
    }
    manifest["runtimeDependencies"]["executables"]["npx"] = {
        "status": "installer-bundled",
        "archives": ["node/node-readme.txt"],
    }
    manifest["toolStates"]["browser_mcp"] = {"status": "install-ready", "missingDependencies": []}

    with pytest.raises(check_manifest.ManifestError):
        check_manifest.check_manifest(manifest, expected_platform="windows-x64", expected_version="0.2.5")
    with pytest.raises(release_validator.ValidationError):
        release_validator.validate_v025_runtime_manifests([manifest], "webui-windows-x64", "0.2.5")


def test_v025_runtime_manifest_rejects_lark_cli_archive_under_node_prefix(tmp_path):
    runtime = tmp_path / "runtime"
    _bundle_python(runtime)
    _write_zip(tmp_path / "node" / "lark-cli-node-v22-win-x64.zip", {"lark-cli-node-v22-win-x64/lark-cli.cmd": ""})
    manifest = write_manifest.build_manifest(runtime, tmp_path, "0.2.5", "windows-x64")
    manifest["runtimeDependencies"]["executables"]["lark-cli"] = {
        "status": "installer-bundled",
        "archives": ["node/lark-cli-node-v22-win-x64.zip"],
    }
    manifest["toolStates"]["feishu_cli"] = {"status": "install-ready", "missingDependencies": []}

    with pytest.raises(check_manifest.ManifestError):
        check_manifest.check_manifest(manifest, expected_platform="windows-x64", expected_version="0.2.5")
    with pytest.raises(release_validator.ValidationError):
        release_validator.validate_v025_runtime_manifests([manifest], "webui-windows-x64", "0.2.5")


def test_v025_runtime_manifest_ignores_empty_installer_archives(tmp_path):
    runtime = tmp_path / "package" / "runtime"
    package_root = tmp_path / "package"
    _touch(package_root / "python" / "cpython-3.11.15+test-aarch64-apple-darwin.tar.gz")
    _touch(package_root / "node" / "node-v22-test-win-x64.zip")
    _touch(package_root / "wheelhouse" / "mac-arm64" / "openpyxl-1.0-py3-none-any.whl")

    mac_manifest = write_manifest.build_manifest(runtime, package_root, "0.2.5", "macos-universal")
    win_manifest = write_manifest.build_manifest(runtime, package_root, "0.2.5", "windows-x64")

    assert mac_manifest["runtimeDependencies"]["python"]["status"] == "missing"
    assert mac_manifest["runtimeDependencies"]["pythonPackages"]["openpyxl"]["status"] == "missing"
    assert win_manifest["runtimeDependencies"]["executables"]["node"]["status"] == "missing"


def test_v025_release_validator_rejects_unsafe_artifact_filename(tmp_path):
    manifest = {
        "artifacts": [
            {
                "id": "web-linux-service",
                "status": "ready",
                "fileName": "../escape.tar.gz",
                "size": 1,
                "sha256": "A" * 64,
            }
        ]
    }

    with pytest.raises(release_validator.ValidationError):
        release_validator.validate_manifest_artifacts(manifest, tmp_path)


def test_v025_release_validator_rejects_publishable_external_artifact(tmp_path):
    manifest = {
        "artifacts": [
            {
                "id": "web-linux-service",
                "status": "ready",
                "fileName": "EcoreX_0.2.5-web-linux-service.tar.gz",
                "href": "https://downloads.example.invalid/EcoreX_0.2.5-web-linux-service.tar.gz",
                "external": True,
                "size": 1,
                "sha256": "A" * 64,
            }
        ]
    }

    with pytest.raises(release_validator.ValidationError):
        release_validator.validate_manifest_artifacts(manifest, tmp_path)


def test_v025_release_validator_rejects_empty_installer_archive_payload(tmp_path):
    runtime = tmp_path / "package" / "runtime"
    package_root = tmp_path / "package"
    _bundle_macos_installer(package_root)
    manifest = write_manifest.build_manifest(runtime, package_root, "0.2.5", "macos-universal")

    with pytest.raises(release_validator.ValidationError):
        release_validator.validate_manifest_archive_payloads(
            "pkg/runtime/runtime-manifest.json",
            manifest,
            "unit",
            lambda _name: b"",
        )


def test_v025_release_validator_accepts_cpython_versioned_executable_payload(tmp_path):
    archive_path = _write_tar_gz(
        tmp_path / "cpython-3.11.15+test-aarch64-apple-darwin.tar.gz",
        {"python/bin/python3.11": ""},
    )

    assert release_validator.cpython_payload_matches(archive_path.name, archive_path.read_bytes()) is True


def test_v025_public_zip_rejects_externalized_local_artifact_href(tmp_path):
    payload = b"release-payload"
    artifact_name = "EcoreX_0.2.5-web-linux-service.tar.gz"
    digest = release_validator.sha256_bytes(payload)
    artifact = {
        "id": "web-linux-service",
        "status": "ready",
        "fileName": artifact_name,
        "href": f"downloads/{artifact_name}",
        "size": len(payload),
        "sha256": digest,
    }
    public_artifact = dict(artifact)
    public_artifact["href"] = f"https://downloads.example.invalid/{artifact_name}"
    public_artifact["external"] = True
    public_zip = tmp_path / "public.zip"

    with zipfile.ZipFile(public_zip, "w") as archive:
        archive.writestr("README.txt", "EcoreX public release\n")
        archive.writestr("site/index.html", "<html></html>")
        archive.writestr("site/admin/index.html", "<html></html>")
        archive.writestr("admin-api/ecorex_admin_api.py", "print('ok')\n")
        archive.writestr("server/install-ecorex-public-release.sh", "#!/bin/sh\n")
        archive.writestr("server/check-ecorex-server-release.sh", "#!/bin/sh\n")
        archive.writestr("site/assets/icon.png", b"x")
        archive.writestr("site/assets/ecorex-app-preview.png", b"x")
        archive.writestr("site/assets/ecorex-ecosystem-hub.png", b"x")
        archive.writestr("site/site.js", "install-webui.ps1 webui-windows-x64 webui-macos-universal")
        archive.writestr(
            "site/install-webui.ps1",
            'function Try-SaveUrlWithCurl {}\n"--continue-at", "-"\nfunction Expand-EcoreXZip {}\nfunction ConvertTo-EcoreXLongPath {}\n$source.CopyTo($target)\nBlocked unsafe zip entry\n',
        )
        archive.writestr("site/manifest.json", json.dumps({"version": "0.2.5", "artifacts": [public_artifact]}))
        archive.writestr(
            "checksums.json",
            json.dumps(
                {
                    "artifacts": {
                        "web-linux-service": {
                            "fileName": artifact_name,
                            "relativePath": f"site/downloads/{artifact_name}",
                            "size": len(payload),
                            "sha256": digest,
                            "status": "ready",
                        }
                    }
                }
            ),
        )
        archive.writestr(f"site/downloads/{artifact_name}", payload)

    with pytest.raises(release_validator.ValidationError):
        release_validator.validate_public_zip(public_zip, {"version": "0.2.5"}, [artifact])


def test_v027_public_zip_accepts_externalized_downloads_contract(tmp_path):
    payload = b"release-payload"
    artifact_name = "EcoreX_0.2.7-web-linux-service.tar.gz"
    digest = release_validator.sha256_bytes(payload)
    artifact = {
        "id": "web-linux-service",
        "status": "ready",
        "fileName": artifact_name,
        "href": f"downloads/{artifact_name}",
        "size": len(payload),
        "sha256": digest,
    }
    public_artifact = dict(artifact)
    public_zip = tmp_path / "public-thin.zip"

    with zipfile.ZipFile(public_zip, "w") as archive:
        archive.writestr("README.txt", "EcoreX public release\n")
        archive.writestr("site/index.html", "<html></html>")
        archive.writestr("site/admin/index.html", "<html></html>")
        archive.writestr("admin-api/ecorex_admin_api.py", "print('ok')\n")
        archive.writestr("server/install-ecorex-public-release.sh", "#!/bin/sh\n")
        archive.writestr("server/check-ecorex-server-release.sh", "#!/bin/sh\n")
        archive.writestr("site/assets/icon.png", b"x")
        archive.writestr("site/assets/ecorex-app-preview.png", b"x")
        archive.writestr("site/assets/ecorex-ecosystem-hub.png", b"x")
        archive.writestr("site/site.js", "install-webui.ps1 webui-windows-x64 webui-macos-universal")
        archive.writestr(
            "site/install-webui.ps1",
            'function Try-SaveUrlWithCurl {}\n"--continue-at", "-"\nfunction Expand-EcoreXZip {}\nfunction ConvertTo-EcoreXLongPath {}\n$source.CopyTo($target)\nBlocked unsafe zip entry\n',
        )
        archive.writestr(
            "site/manifest.json",
            json.dumps(
                {
                    "version": "0.2.7",
                    "downloadsExternalized": True,
                    "update": {
                        "webui": {
                            "channel": "stable",
                            "promotion": "admin-gated",
                            "backgroundUpdate": {
                                "idleGate": "/api/active-requests",
                                "browserAfterInstall": "soft-refresh-existing-tab",
                                "manualBrowserOpen": "first-install-or-user-request",
                                "activationPolicy": "prompt-soft-refresh-existing-tab",
                                "healthCheck": "/api/version",
                                "stateFile": "update-state.json",
                                "autoLaunchBrowser": "never-in-background",
                            },
                        }
                    },
                    "artifacts": [public_artifact],
                }
            ),
        )
        archive.writestr(
            "checksums.json",
            json.dumps(
                {
                    "version": "0.2.7",
                    "downloadsExternalized": True,
                    "downloadsSource": "public-release-downloads-source",
                    "artifacts": {
                        "web-linux-service": {
                            "fileName": artifact_name,
                            "relativePath": f"site/downloads/{artifact_name}",
                            "href": f"downloads/{artifact_name}",
                            "size": len(payload),
                            "sha256": digest,
                            "status": "ready",
                            "external": True,
                            "deploymentSourceFileName": artifact_name,
                        }
                    },
                }
            ),
        )

    release_validator.validate_public_zip(public_zip, {"version": "0.2.7"}, [artifact])


def test_v027_public_release_updates_are_admin_promotion_gated():
    root = Path(__file__).resolve().parents[1]
    public_installer = (root / "scripts" / "install-ecorex-public-release.sh").read_text(encoding="utf-8")
    deployer = (root / "scripts" / "deploy-v024-production.py").read_text(encoding="utf-8")
    real_gate = (root / "scripts" / "真实发布校验.py").read_text(encoding="utf-8")
    admin_api = (root / "deploy" / "ecorex-admin-api" / "ecorex_admin_api.py").read_text(encoding="utf-8")
    admin_html = (root / "deploy" / "ecorex-site" / "admin" / "index.html").read_text(encoding="utf-8")
    admin_js = (root / "deploy" / "ecorex-site" / "admin" / "admin.js").read_text(encoding="utf-8")

    assert 'PROMOTE_PUBLIC_RELEASE="${PROMOTE_PUBLIC_RELEASE:-0}"' in public_installer
    assert 'ln -sfn "$release_dir" "$RELEASE_ROOT/staged-v${VERSION}"' in public_installer
    assert 'if [[ "$PROMOTE_PUBLIC_RELEASE" == "1" ]]; then' in public_installer
    assert 'ln -sfn "$release_dir" "$RELEASE_ROOT/current"' in public_installer
    assert 'promotion: $promotion_status' in public_installer
    assert "ECOREX_PROMOTE_PUBLIC_RELEASE" in deployer
    assert "--promote-public-release" in deployer
    assert '"adminTriggerRequired": True' in deployer
    assert '"mode": "stable" if self.promote_public_release else "staged"' in deployer
    assert 'remote_tmp_dir = posixpath.join(self.remote_dir, "tmp")' in deployer
    assert 'remote_cache_dir = "/srv/ecorex-agent-download/downloads-cache/sha256"' in deployer
    assert 'def cache_probe_command(' in deployer
    assert 'def ensure_download_source(' in deployer
    assert 'sha256sum "$1"' in deployer
    assert '"adopted-staging"' in deployer
    assert '"webui_windows_download"' in deployer
    assert '"webui_macos_download"' in deployer
    assert 'f"{name}_cache_probe"' in deployer
    assert 'f"{name}_cache_upload"' in deployer
    assert '&& chmod 700 {shlex.quote(remote_tmp_dir)}' in deployer
    assert 'f"TMPDIR={shlex.quote(remote_tmp_dir)} VERSION={shlex.quote(VERSION)} EXPECTED_SHA256=' in deployer
    assert 'f"TMPDIR={shlex.quote(remote_tmp_dir)} VERSION={shlex.quote(VERSION)} BASE_URL=' in deployer
    assert 'f"TMPDIR={shlex.quote(remote_tmp_dir)} VERSION={shlex.quote(VERSION)} PUBLIC_BASE_URL=' in deployer
    assert 'os.environ["ECOREX_PROMOTE_PUBLIC_RELEASE"] = "1"' in real_gate
    assert 'path == "/release/state"' in admin_api
    assert 'path == "/release/promote"' in admin_api
    assert "_validate_release_dir(release_dir, manifest, verify_sha=True)" in admin_api
    assert "os.replace(str(tmp_pointer), str(current_pointer))" in admin_api
    assert '"release.promote"' in admin_api
    assert "data-release-summary" in admin_html
    assert "data-release-promote" in admin_js
    assert 'request("/release/state")' in admin_js
    assert 'mutate("/release/promote"' in admin_js


def test_v027_webui_installers_keep_windows_macos_user_flow_consistent():
    root = Path(__file__).resolve().parents[1]
    win_installer = (root / "deploy" / "ecorex-site" / "install-webui.ps1").read_text(encoding="utf-8")
    mac_installer = (root / "deploy" / "ecorex-site" / "install-webui.sh").read_text(encoding="utf-8")
    local_packager = (root / "scripts" / "prepare-ecorex-webui-local-release.ps1").read_text(encoding="utf-8")
    manifest = json.loads((root / "deploy" / "ecorex-site" / "manifest.json").read_text(encoding="utf-8"))

    assert 'Join-Url $BaseUrl "manifest.json"' in win_installer
    assert 'join_url "$BASE_URL" "manifest.json"' in mac_installer
    assert "does not match requested" in win_installer
    assert "does not match requested" in mac_installer
    assert 'id -eq "webui-windows-x64" -and $_.status -eq "ready"' in win_installer
    assert '"webui-macos-universal"' in mac_installer
    assert '$artifactUrl = Join-Url $BaseUrl $artifact.href' in win_installer
    assert 'ARTIFACT_URL="$(join_url "$BASE_URL" "$ARTIFACT_HREF")"' in mac_installer
    assert 'Test-ExpectedHash -Path $CachePath -ExpectedSha256 $ExpectedSha256' in win_installer
    assert 'sha256_file "$destination")" == "$expected_sha"' in mac_installer
    assert '"--continue-at", "-"' in win_installer
    assert 'resume_args=(-C -)' in mac_installer
    assert "EcoreX-WebUI-Installer/0.2.7" in win_installer
    assert "EcoreX WebUI installer script: 0.2.7" in win_installer
    assert "EcoreX WebUI installer script: 0.2.7" in mac_installer
    assert "install-ecorex-webui-win.ps1" in win_installer
    assert "install-ecorex-webui-mac.sh" in mac_installer
    webui_policy = manifest["update"]["webui"]
    assert webui_policy["promotion"] == "admin-gated"
    assert webui_policy["backgroundUpdate"]["idleGate"] == "/api/active-requests"
    assert webui_policy["backgroundUpdate"]["browserAfterInstall"] == "soft-refresh-existing-tab"
    assert webui_policy["backgroundUpdate"]["manualBrowserOpen"] == "first-install-or-user-request"
    assert webui_policy["backgroundUpdate"]["activationPolicy"] == "prompt-soft-refresh-existing-tab"
    assert webui_policy["backgroundUpdate"]["healthCheck"] == "/api/version"
    assert webui_policy["backgroundUpdate"]["stateFile"] == "update-state.json"
    assert webui_policy["backgroundUpdate"]["autoLaunchBrowser"] == "never-in-background"
    assert 'ValidateSet("manual", "background")' in local_packager
    assert '$backgroundUpdate = $UpdateMode -ieq "background"' in local_packager
    assert 'Get-ActiveRequestCount -StateDir $stateDir' in local_packager
    assert 'Write-UpdateState -StateDir $stateDir -Status "deferred" -Reason "active_requests"' in local_packager
    assert 'activationPolicy = if ($backgroundUpdate) { "prompt-soft-refresh-existing-tab" }' in local_packager
    assert 'autoLaunchBrowser = if ($backgroundUpdate) { "never-in-background" }' in local_packager
    assert 'healthCheck = [ordered]@{' in local_packager
    assert 'if ((-not $NoBrowser) -and (-not $backgroundUpdate))' in local_packager
    assert 'UPDATE_MODE="${ECOREX_UPDATE_MODE:-manual}"' in local_packager
    assert 'BACKGROUND_UPDATE=1' in local_packager
    assert 'active_request_count' in local_packager
    assert 'write_update_state "deferred" "active_requests"' in local_packager
    assert '"activationPolicy": "prompt-soft-refresh-existing-tab" if sys.argv[3] == "background"' in local_packager
    assert '"autoLaunchBrowser": "never-in-background" if sys.argv[3] == "background"' in local_packager
    assert '"healthCheck": {' in local_packager
    assert 'if [[ "$OPEN_BROWSER" == "1" && "$BACKGROUND_UPDATE" != "1" ]]' in local_packager
    assert 'defer-to-existing-tab-soft-refresh' in local_packager


def test_v027_real_release_browser_toolchain_probe_is_timeout_bounded():
    root = Path(__file__).resolve().parents[1]
    legacy_smoke = (root / "scripts" / "smoke-v026-production-200-user-behavior.py").read_text(encoding="utf-8")
    agent_smoke = (root / "scripts" / "smoke-v026-production-agent-product-acceptance.py").read_text(encoding="utf-8")
    image_smoke = (root / "scripts" / "smoke-v026-production-30-image-ocr-vision-toolchain.py").read_text(encoding="utf-8")

    assert '"probe": "static-runtime-source"' in legacy_smoke
    assert 'browser_tool_source = read_text("/opt/ecorex-web/current/runtime/agent/tools/browser/browser_tool.py")' in legacy_smoke
    assert 'browser_service_source = read_text("/opt/ecorex-web/current/runtime/agent/tools/browser/browser_service.py")' in legacy_smoke
    assert 'browser_automation_source = read_text("/opt/ecorex-web/current/runtime/agent/tools/browser/browser_automation_service.py")' in legacy_smoke
    assert "Page.captureScreenshot" in legacy_smoke
    assert "BrowserTool({" not in legacy_smoke
    assert 'browser_automation_diagnostics({})' not in legacy_smoke
    assert 'VALIDATION_TMP_ROOT = Path("/srv/ecorex-agent-download/validation-tmp")' in legacy_smoke
    assert 'VALIDATION_TMP_ROOT = Path("/srv/ecorex-agent-download/validation-tmp")' in agent_smoke
    assert 'VALIDATION_TMP_ROOT = Path("/srv/ecorex-agent-download/validation-tmp")' in image_smoke
    assert 'remote_tmp_root = "/srv/ecorex-agent-download/validation-tmp"' in agent_smoke
    assert "client.open_sftp()" in agent_smoke
    assert 'handle.write(remote_script)' in agent_smoke
    assert 'TMPDIR={shlex.quote(remote_tmp_root)}' in agent_smoke
    assert 'python - <<' not in agent_smoke
    assert 'mpi_policy = (mpi_payload.get("sourcePolicy") or {}) if isinstance(mpi_payload, dict) else {}' in agent_smoke
    assert 'os.environ.setdefault("TMPDIR", str(VALIDATION_TMP_ROOT))' in legacy_smoke
    assert 'os.environ.setdefault("TMPDIR", str(VALIDATION_TMP_ROOT))' in agent_smoke
    assert 'os.environ.setdefault("TMPDIR", str(VALIDATION_TMP_ROOT))' in image_smoke
    assert "tempfile.tempdir = str(VALIDATION_TMP_ROOT)" in legacy_smoke
    assert "tempfile.tempdir = str(VALIDATION_TMP_ROOT)" in agent_smoke
    assert "tempfile.tempdir = str(VALIDATION_TMP_ROOT)" in image_smoke
    assert 'tempfile.mkdtemp(prefix="ecorex-v027-200-", dir=str(VALIDATION_TMP_ROOT))' in legacy_smoke
    assert 'tempfile.mkdtemp(prefix="ecorex-agent-acceptance-", dir=str(VALIDATION_TMP_ROOT))' in agent_smoke
    assert "def published_artifact_path(url):" in legacy_smoke
    assert 'relative.startswith("downloads/")' in legacy_smoke
    assert '"validationSource": "published-local-file"' in legacy_smoke
    assert "os.link(local_source, dest)" in legacy_smoke


def test_v025_linux_release_scripts_install_core_requirements_and_manifest():
    install_script = (Path(__file__).resolve().parents[1] / "scripts" / "install-ecorex-web.sh").read_text(encoding="utf-8")
    check_script = (Path(__file__).resolve().parents[1] / "scripts" / "check-ecorex-web-release.sh").read_text(encoding="utf-8")
    local_packager = (Path(__file__).resolve().parents[1] / "scripts" / "prepare-ecorex-webui-local-release.ps1").read_text(encoding="utf-8")
    runtime_pack_root = Path(__file__).resolve().parents[1] / "runtime-packs"
    core_requirements = (runtime_pack_root / "core-requirements.txt").read_text(encoding="utf-8")
    root_requirements = (Path(__file__).resolve().parents[1] / "requirements.txt").read_text(encoding="utf-8")

    assert 'core-requirements.txt' in install_script
    assert 'Environment=ECOREX_INSTALL_ROOT=$INSTALL_ROOT' in install_script
    assert 'Environment=ECOREX_STATE_DIR=$STATE_DIR' in install_script
    assert 'Environment=ECOREX_NODE_ROOT=$INSTALL_ROOT/node' in install_script
    assert 'feishu_cli.setdefault("allow_system_node", False)' in install_script
    assert 'install_node_runtime "$bundle_root"' in install_script
    assert 'run_web_core_baseline_gate "$runtime_dir"' in install_script
    assert '--strict' in install_script
    assert 'check-web-core-runtime-baseline.py' in install_script
    assert 'member.isfile()' in install_script
    assert 'member.isdir()' in install_script
    assert 'member.issym()' in install_script
    assert 'Unsupported Node archive member type' in install_script
    assert 'Unsupported Node archive top-level member' in install_script
    assert 'was not present in SHASUMS256.txt' in install_script
    assert 'os.chmod(destination, safe_mode(member))' in install_script
    assert 'tar.extract(member' not in install_script
    assert 'Copy-IfExists -Source (Join-Path $repoRoot "scripts/check-web-core-runtime-baseline.py")' in (
        Path(__file__).resolve().parents[1] / "scripts" / "prepare-ecorex-web-release.ps1"
    ).read_text(encoding="utf-8")
    assert 'Resolve-RequiredPath (Join-Path $repoRoot "runtime-packs")' in (
        Path(__file__).resolve().parents[1] / "scripts" / "prepare-ecorex-web-release.ps1"
    ).read_text(encoding="utf-8")
    assert '"/runtime/core-requirements.txt"' in check_script
    assert '"/runtime/runtime-manifest.json"' in check_script
    assert '"/runtime/scripts/check-web-core-runtime-baseline.py"' in check_script
    assert 'check_file "$INSTALL_ROOT/current/runtime/core-requirements.txt"' in check_script
    assert 'check_file "$INSTALL_ROOT/current/runtime/runtime-manifest.json"' in check_script
    assert 'check_file "$INSTALL_ROOT/current/runtime/scripts/check-web-core-runtime-baseline.py"' in check_script
    assert 'check_file "$INSTALL_ROOT/node/bin/node"' in check_script
    assert 'check_runtime_baseline' in check_script
    assert 'runtime baseline releaseReady' in check_script
    assert "numpy>=1.21" in core_requirements
    assert "web.py>=0.76,<0.77" in core_requirements
    assert "git+https://github.com/webpy/webpy.git" not in core_requirements
    assert "git+https://github.com/webpy/webpy.git" not in root_requirements
    assert core_requirements == (Path(__file__).resolve().parents[1] / "desktop" / "runtime-packs" / "core-requirements.txt").read_text(encoding="utf-8")
    assert (runtime_pack_root / "capabilities.json").read_text(encoding="utf-8") == (
        Path(__file__).resolve().parents[1] / "desktop" / "runtime-packs" / "capabilities.json"
    ).read_text(encoding="utf-8")
    assert '"allow_system_node": false' in (Path(__file__).resolve().parents[1] / "config-template.json").read_text(encoding="utf-8")
    assert 'Install-WindowsRuntimeDependency -RuntimeDir $winRuntime -ModuleName "web"' in local_packager
    assert 'PackageSpec "web.py>=0.76,<0.77"' in local_packager
    assert 'Install-WindowsRuntimeDependency -RuntimeDir $winRuntime -ModuleName "chardet"' in local_packager
    assert 'Install-WindowsRuntimeDependency -RuntimeDir $winRuntime -ModuleName "numpy"' in local_packager


def test_v027_macos_stage_runtime_preinstalls_node_and_playwright_browser():
    root = Path(__file__).resolve().parents[1]
    mac_stage = (root / "desktop" / "scripts" / "stage-runtime-mac.sh").read_text(encoding="utf-8")
    win_stage = (root / "desktop" / "scripts" / "stage-runtime-win.ps1").read_text(encoding="utf-8")
    dependency_provider = (root / "common" / "runtime_dependencies.py").read_text(encoding="utf-8")
    baseline_checker = (root / "scripts" / "check-web-core-runtime-baseline.py").read_text(encoding="utf-8")

    assert 'NODE_VERSION="${NODE_VERSION:-22.22.0}"' in mac_stage
    assert "--node-version" in mac_stage
    assert "--skip-node-install" in mac_stage
    assert "--skip-playwright-browser-install" in mac_stage
    assert "stage_node" in mac_stage
    assert "node-v${NODE_VERSION}-darwin-${node_arch}.tar.gz" in mac_stage
    assert 'PLAYWRIGHT_BROWSERS_PATH="$RUNTIME_DIR/playwright-browsers"' in mac_stage
    assert '"nodeVersion": None if skip_node == "1" else node_version' in mac_stage
    assert '"playwrightBrowserInstall": skip_playwright_browser != "1"' in mac_stage
    assert 'NODE_VERSION' in win_stage or 'NodeVersion' in win_stage
    assert "def playwright_browser_dirs" in dependency_provider
    assert 'env.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(browser_dirs[0]))' in dependency_provider
    assert 'self.runtime_root / "playwright-browsers"' in dependency_provider
    assert "def _capture_browser_runtime(runtime_root: Path, state_root: Path)" in baseline_checker
    assert "ecorex-bundled-playwright" in baseline_checker


def test_v025_runtime_manifest_rejects_bad_native_and_tool_file_statuses(tmp_path):
    runtime = tmp_path / "runtime"
    _bundle_python(runtime)
    manifest = write_manifest.build_manifest(runtime, tmp_path, "0.2.5", "windows-x64")
    manifest["runtimeDependencies"]["nativeBins"]["pdfinfo"] = {"status": "bogus"}

    with pytest.raises(check_manifest.ManifestError):
        check_manifest.check_manifest(manifest, expected_platform="windows-x64", expected_version="0.2.5")
    with pytest.raises(release_validator.ValidationError):
        release_validator.validate_v025_runtime_manifests([manifest], "webui-windows-x64", "0.2.5")

    manifest = write_manifest.build_manifest(runtime, tmp_path, "0.2.5", "windows-x64")
    manifest["runtimeDependencies"]["toolFiles"]["tongxinCli"] = {"status": "bogus"}
    manifest["toolStates"]["tongxin_cli"] = {"status": "configure-required", "missingDependencies": ["xin_agent_cli.py"]}

    with pytest.raises(check_manifest.ManifestError):
        check_manifest.check_manifest(manifest, expected_platform="windows-x64", expected_version="0.2.5")
    with pytest.raises(release_validator.ValidationError):
        release_validator.validate_v025_runtime_manifests([manifest], "webui-windows-x64", "0.2.5")


def test_release_validator_rejects_directory_member_for_executable_reference(tmp_path):
    runtime = tmp_path / "runtime"
    _bundle_python(runtime)
    manifest = write_manifest.build_manifest(runtime, tmp_path, "0.2.5", "windows-x64")
    manifest["runtimeDependencies"]["executables"]["node"] = {"status": "bundled", "path": "node/node.exe"}
    manifest["toolStates"]["browser_mcp"] = {"status": "missing_dependency", "missingDependencies": ["npx"]}
    archive_names = {
        "pkg/runtime/runtime-manifest.json",
        "pkg/runtime/python/python.exe",
        "pkg/runtime/node/node.exe",
    }
    archive_file_names = {
        "pkg/runtime/runtime-manifest.json",
        "pkg/runtime/python/python.exe",
    }

    with pytest.raises(release_validator.ValidationError):
        release_validator.validate_v025_runtime_manifests(
            [("pkg/runtime/runtime-manifest.json", manifest)],
            "webui-windows-x64",
            "0.2.5",
            archive_names,
            archive_file_names,
        )


def test_v025_runtime_manifest_rejects_directory_for_tool_file(tmp_path):
    runtime = tmp_path / "runtime"
    _bundle_python(runtime)
    (runtime / "tools" / "tongxin" / "xin_agent_cli.py").mkdir(parents=True)
    manifest = write_manifest.build_manifest(runtime, tmp_path, "0.2.5", "windows-x64")
    manifest["runtimeDependencies"]["toolFiles"]["tongxinCli"] = {
        "status": "bundled",
        "path": "tools/tongxin/xin_agent_cli.py",
    }
    manifest["toolStates"]["tongxin_cli"] = {"status": "ready", "missingDependencies": []}

    with pytest.raises(check_manifest.ManifestError):
        check_manifest.check_manifest(
            manifest,
            expected_platform="windows-x64",
            expected_version="0.2.5",
            runtime_root=runtime,
            package_root=tmp_path,
        )


def test_v025_runtime_manifest_cli_roundtrip(tmp_path):
    runtime = tmp_path / "runtime"
    _bundle_python(runtime)
    output = tmp_path / "runtime-manifest.json"

    assert write_manifest.main([
        "--runtime-root",
        str(runtime),
        "--package-root",
        str(tmp_path),
        "--version",
        "0.2.5",
        "--platform",
        "windows-x64",
        "--output",
        str(output),
    ]) == 0

    data = json.loads(output.read_text(encoding="utf-8"))
    assert data["schemaVersion"] == "v0.2.5-runtime-manifest-v1"
    assert check_manifest.main([
        str(output),
        "--platform",
        "windows-x64",
        "--version",
        "0.2.5",
        "--runtime-root",
        str(runtime),
        "--package-root",
        str(tmp_path),
    ]) == 0
