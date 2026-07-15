from __future__ import annotations

import json
import hashlib
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WEBUI_VERSION = "0.2.9.2"
ADMIN_API_VERSION = "0.2.9.1"
USAGE_PANEL_VERSION = "0.2.9.1"
LEGACY_UPGRADE_VERSION = "0.2.9"
VERSION = WEBUI_VERSION


def test_v029_release_version_anchors_are_aligned():
    package = json.loads((ROOT / "desktop" / "package.json").read_text(encoding="utf-8"))
    package_lock = json.loads((ROOT / "desktop" / "package-lock.json").read_text(encoding="utf-8"))
    local_packager = (ROOT / "scripts" / "prepare-ecorex-webui-local-release.ps1").read_text(encoding="utf-8")
    win_installer = (ROOT / "deploy" / "ecorex-site" / "install-webui.ps1").read_text(encoding="utf-8")
    mac_installer = (ROOT / "deploy" / "ecorex-site" / "install-webui.sh").read_text(encoding="utf-8")
    admin_api = (ROOT / "deploy" / "ecorex-admin-api" / "ecorex_admin_api.py").read_text(encoding="utf-8")
    web_channel = (ROOT / "channel" / "web" / "web_channel.py").read_text(encoding="utf-8")

    assert (ROOT / "cli" / "VERSION").read_text(encoding="utf-8").strip() == VERSION
    assert package["version"] == VERSION
    assert f"-Version {VERSION} -SkipCombinedPackage" in package["scripts"]["webui:package"]
    assert package_lock["version"] == VERSION
    assert package_lock["packages"][""]["version"] == VERSION
    assert f'[string]$Version = "{VERSION}"' in local_packager
    assert f"EcoreX-WebUI-Installer/{VERSION}" in win_installer
    assert f"EcoreX WebUI installer script: {VERSION}" in win_installer
    assert f"EcoreX WebUI installer script: {VERSION}" in mac_installer
    assert f'VERSION = "{ADMIN_API_VERSION}"' in admin_api
    assert 'DEFAULT_CLIENT_EVENT_KEY = "ecorex-web-v0.2.9.1-web.1"' in admin_api
    assert '"ecorex-web-v0.2.9-web.1"' in admin_api
    assert '"ecorex-web-v0.2.8-web.1"' in admin_api
    assert 'WEB_ENTERPRISE_CLIENT_KEYS = (\n    "ecorex-web-v0.2.9.1-web.1",' in web_channel
    assert '"ecorex-web-v0.2.9-web.1"' in web_channel
    assert '"ecorex-web-v0.2.8-web.1"' in web_channel
    assert f"EcoreX-WebArtifactFeedback/{ADMIN_API_VERSION}" in web_channel
    assert f"EcoreX-WebSessionShare/{ADMIN_API_VERSION}" in web_channel
    assert f"EcoreX-WebReleaseNotice/{ADMIN_API_VERSION}" in web_channel


def test_v029_release_notes_are_user_facing_and_current():
    from common.ecorex_release_notes import get_current_release_notes

    notes = get_current_release_notes()

    assert notes["version"] == VERSION
    assert VERSION in notes["title"]
    assert "小芯" in json.dumps(notes, ensure_ascii=False)
    assert "精准修图" in json.dumps(notes, ensure_ascii=False)
    assert "在线更新" in json.dumps(notes, ensure_ascii=False)
    assert "Gemini" in json.dumps(notes, ensure_ascii=False)
    assert VERSION in notes["updatePolicy"]["windows"]
    assert VERSION in notes["updatePolicy"]["macos"]


def test_v029_public_manifest_webui_metadata_is_current_and_artifacts_are_real():
    manifest = json.loads((ROOT / "deploy" / "ecorex-site" / "manifest.json").read_text(encoding="utf-8"))
    artifacts = {item["id"]: item for item in manifest["artifacts"]}

    assert manifest["version"] == VERSION
    assert f"v{VERSION}" in manifest["notes"]
    assert "WebUI-first release" in manifest["notes"]
    assert [item["id"] for item in manifest["download"]["mirrors"]] == [
        f"ecorex-github-cn-mirror-v{VERSION}",
        f"ecorex-download-origin-v{VERSION}",
        f"ecorex-download-cdn-v{VERSION}",
    ]
    assert manifest["download"]["mirrors"][0]["baseUrl"] == f"https://gh-proxy.com/https://github.com/zhangyifanjackson-dotcom/EcoreX-installers/releases/download/v{VERSION}"
    assert manifest["download"]["mirrors"][0]["kind"] == "github-release-cn-mirror"
    assert manifest["download"]["mirrors"][1]["baseUrl"] == "https://mvdcm.ecoremedia.net/ecorex-agent/downloads"
    assert manifest["download"]["mirrors"][2]["baseUrl"] == "https://dl.ecoremedia.net/ecorex-agent/downloads"
    assert manifest["download"]["mode"] == "github-cn-primary"
    assert "minimumTargetBytesPerSecond" not in manifest["download"]
    assert "fallback" not in manifest["download"]
    assert manifest["update"]["webui"]["artifactIds"] == [
        "webui-windows-x64",
        "webui-macos-universal",
    ]

    for artifact_id, platform, source_marker in (
        ("webui-windows-x64", "Windows", f"Local v{VERSION} WebUI"),
        ("webui-macos-universal", "macOS", f"Local v{VERSION} WebUI"),
        ("web-linux-service", "Linux", f"Local v{VERSION} Web service"),
    ):
        artifact = artifacts[artifact_id]
        assert artifact["version"] == VERSION
        assert artifact["platform"] == platform
        assert artifact["fileName"].startswith(f"EcoreX_{VERSION}-")
        assert artifact["href"] == f"downloads/{artifact['fileName']}"
        assert artifact["status"] == "ready"
        assert artifact["size"] > 0
        assert len(artifact["sha256"]) == 64
        assert source_marker in artifact["source"]
        if artifact_id == "webui-windows-x64":
            assert "chunked" not in artifact

        download_path = ROOT / "release-artifacts" / artifact["fileName"]
        if not download_path.exists():
            download_path = ROOT / "deploy" / "ecorex-site" / artifact["href"]
        payload = download_path.read_bytes()
        assert len(payload) == artifact["size"]
        assert hashlib.sha256(payload).hexdigest().upper() == artifact["sha256"]


def test_v029_webui_installers_use_cdn_primary_parallel_download():
    win_installer = (ROOT / "deploy" / "ecorex-site" / "install-webui.ps1").read_text(encoding="utf-8")
    mac_installer = (ROOT / "deploy" / "ecorex-site" / "install-webui.sh").read_text(encoding="utf-8")
    web_channel = (ROOT / "channel" / "web" / "web_channel.py").read_text(encoding="utf-8")

    assert '[string]$BaseUrl = "https://dl.ecoremedia.net/ecorex-agent"' in win_installer
    assert "[int]$Port = 9909" in win_installer
    assert "return 16" in win_installer
    assert "[Math]::Min(32, $parsed)" in win_installer
    assert "Try-SaveUrlWithParallelCurl" in win_installer
    assert "Using adaptive CDN download" in win_installer
    assert "Using CDN chunked package download" in win_installer
    assert "CDN chunk download progress:" in win_installer
    assert "Save-ArtifactChunks -Manifest $manifest" in win_installer
    assert '\"--range\", \"$($Chunk.Start)-$($Chunk.End)\"' in win_installer
    assert '$next = $chunks | Where-Object' in win_installer
    assert '$next = @($chunks' not in win_installer
    assert "Chunk {0} stalled" in win_installer
    assert "CDN download progress:" in win_installer
    assert "ETA" in win_installer
    assert "Test-DownloadSourceAvailable" in win_installer
    assert "Skipping unavailable $sourceLabel download source" in win_installer
    assert "Using $sourceLabel download source" in win_installer
    assert "All EcoreX WebUI download sources failed" in win_installer
    assert "Save-UrlWithFallback -Urls $downloadUrls" in win_installer
    assert '$args += @("-Port", $Port)' in win_installer
    assert "Get-DownloadMinimumBytesPerSecond" not in win_installer
    assert "minimumTargetBytesPerSecond" not in win_installer
    assert "--speed-limit" not in win_installer
    assert "Download speed guard enabled" not in win_installer

    assert 'BASE_URL="${ECOREX_BASE_URL:-https://dl.ecoremedia.net/ecorex-agent}"' in mac_installer
    assert 'DOWNLOAD_PARALLEL_PARTS="${ECOREX_DOWNLOAD_PARALLEL_PARTS:-16}"' in mac_installer
    assert 'count=32' in mac_installer
    assert "download_file_parallel" in mac_installer
    assert '--range "${start}-${end}"' in mac_installer
    assert "CDN download progress:" in mac_installer
    assert "format_eta" in mac_installer
    assert 'download_file_from_urls "$ZIP_PATH" "$ARTIFACT_SHA" "$ARTIFACT_SIZE"' in mac_installer
    assert "Using primary CDN download source" in mac_installer
    assert "DOWNLOAD_MIN_BPS" not in mac_installer
    assert "minimumTargetBytesPerSecond" not in mac_installer
    assert "--speed-limit" not in mac_installer
    assert "Slow-source guard" not in mac_installer

    assert "_artifact_download_url(manifest, artifact)" in web_channel
    assert 'path_mode == "fileName"' in web_channel
    assert 'return f"{base}/{path.lstrip' in web_channel


def test_v029_production_deploy_preserves_download_artifact_mtime_for_cdn_cache():
    deploy_script = (ROOT / "scripts" / "deploy-v024-production.py").read_text(encoding="utf-8")
    public_install = (ROOT / "scripts" / "install-ecorex-public-release.sh").read_text(encoding="utf-8")

    assert 'cp -p "$source" "$cache"' in deploy_script
    assert 'cp -p "$cache" "$target"' in deploy_script
    assert 'f"cp -p {shlex.quote(remote_web_tar)}' in deploy_script
    assert "ensure_download_chunks" in deploy_script
    assert "generate_public_download_chunks" in deploy_script
    assert "public_chunk_generation_command" in deploy_script
    assert 'DOWNLOAD_CHUNKS_ROOT / local.name' in deploy_script
    assert 'cp -a "$DOWNLOADS_SOURCE_DIR/." "$tmp_dir/site/downloads/"' in public_install
    assert 'cp -f "$cache" "$target"' not in deploy_script
    assert 'cp -f "$source" "$cache"' not in deploy_script


def test_v029_default_release_uses_github_cn_mirror_before_ecorex_cdn():
    default_release = (ROOT / "scripts" / "release-ecorex-default.ps1").read_text(encoding="utf-8")
    public_packager = (ROOT / "scripts" / "prepare-ecorex-public-release.ps1").read_text(encoding="utf-8")

    assert "EcoreX-installers/releases/download/v$Version" in default_release
    assert "ecorex-github-cn-mirror-v$Version" in default_release
    assert "ecorex-download-origin-v$Version" in default_release
    assert "github-release-cn-mirror" in default_release
    assert '"-AssetDownloadBaseUrls", "$githubCnMirrorUrl,$originUrl,$downloadCdnUrl"' in default_release
    assert "EcoreX-installers/releases/download/v" in public_packager
    assert 'downloadMode = if ([string]$configuredDownloadMirrors[0].kind -eq "github-release-cn-mirror")' in public_packager


def test_v029_public_release_zip_uses_github_cn_mirror_manifest():
    with zipfile.ZipFile(ROOT / "release-artifacts" / f"EcoreX_{VERSION}-public-release.zip") as archive:
        manifest = json.loads(archive.read("site/manifest.json").decode("utf-8-sig"))
    windows = next(item for item in manifest["artifacts"] if item["id"] == "webui-windows-x64")

    assert manifest["download"]["mode"] == "github-cn-primary"
    assert manifest["download"]["mirrors"][0]["kind"] == "github-release-cn-mirror"
    assert manifest["download"]["mirrors"][0]["baseUrl"].startswith("https://gh-proxy.com/https://github.com/")
    assert manifest["download"]["mirrors"][1]["baseUrl"] == "https://mvdcm.ecoremedia.net/ecorex-agent/downloads"
    assert manifest["download"]["mirrors"][2]["baseUrl"] == "https://dl.ecoremedia.net/ecorex-agent/downloads"
    assert "chunked" not in windows


def test_v029_legacy_webui_online_upgrade_smoke_passed():
    smoke_script = (ROOT / "scripts" / "smoke-v028-legacy-webui-online-upgrade.ps1").read_text(encoding="utf-8")
    assert "-Version $TargetVersion -BaseUrl $BaseUrl -Port $Port -NoBrowser" in smoke_script

    report = json.loads((ROOT / "docs" / "v0.2.9" / "artifacts" / "legacy-webui-online-upgrade.json").read_text(encoding="utf-8"))
    assert report["status"] == "PASS"
    assert report["targetVersion"] == LEGACY_UPGRADE_VERSION
    assert report["legacyVersions"] == ["0.2.8"]
    assert report["failCount"] == 0

    checks = {item["name"]: item for item in report["checks"]}
    notice = checks["legacy runtime receives v0.2.8 update notification"]
    assert notice["status"] == "PASS"
    assert notice["detail"]["currentVersion"] == "0.2.8"
    assert notice["detail"]["latestVersion"] == LEGACY_UPGRADE_VERSION
    assert notice["detail"]["artifactSha256"] == "3323BD22C920C7AA5CD42D4F42D2C1F8322CF76BCF08DD2F90CEDE5EC813FC73"

    upgrade = checks["legacy runtime upgrades online to v0.2.8"]
    assert upgrade["status"] == "PASS"
    assert upgrade["detail"]["upgradedVersion"] == LEGACY_UPGRADE_VERSION
    assert upgrade["detail"]["updateStateStatus"] == "installed"
    assert upgrade["detail"]["updateStateMode"] == "background"


def test_v029_independent_usage_panel_slice_is_deployed_and_validated():
    usage_root = ROOT / "deploy" / "ecorex-usage-panel"
    index = (usage_root / "index.html").read_text(encoding="utf-8")
    app_js = (usage_root / "app.js").read_text(encoding="utf-8")
    api_py = (usage_root / "usage_panel_api.py").read_text(encoding="utf-8")

    assert "EcoreX Agent 使用情况分析面板" in index
    assert "服务器 RAW 上报分析" in index
    assert "v0.2.9 审计补充" in index
    assert "使用场景" in index
    assert "创作文案/标题/报告/脚本等" in index
    assert "制作图片、海报图片编辑等" in index
    assert "操作在线文档（飞书/腾讯文档）等" in index
    assert "有效产物" in index
    assert "下拇指回溯" in index
    assert "./api/runtime-audit?limit=80" in app_js
    assert "包含 imagegen / image_job" in app_js
    assert "effectiveArtifacts" in app_js
    assert "feedbackTraces" in app_js
    assert f'VERSION = "{USAGE_PANEL_VERSION}"' in api_py
    assert '"/api/runtime-audit"' in api_py
    assert '"/api/health"' in api_py
    assert '"/api/data"' in api_py

    report = json.loads(
        (ROOT / "docs" / "v0.2.9.1" / "artifacts" / "production-usage-panel-scenario-definitions.json")
        .read_text(encoding="utf-8")
    )
    assert report["status"] == "PASS"
    assert report["version"] == USAGE_PANEL_VERSION
    probe = report["probe"]
    assert probe["healthVersion"] == USAGE_PANEL_VERSION
    assert probe["publicStatus"] == 401
    assert probe["markers"]["scenarioDefinitions"] is True
    assert probe["markers"]["scenarioDetails"] is True
    assert probe["markers"]["indexDefinitionsUpdated"] is True
    assert probe["markers"]["detailStyle"] is True
