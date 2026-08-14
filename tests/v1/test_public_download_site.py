from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]
SITE = ROOT / "deploy" / "ecorex-site"


def test_public_download_site_static_gate_passes() -> None:
    result = subprocess.run(
        [sys.executable, "scripts/check-v1-public-download-site.py"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="strict",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    evidence = json.loads(result.stdout)
    assert evidence["status"] == "passed"
    assert evidence["public_pointer"] == "unpublished"
    assert evidence["hashed_asset_count"] == 10


def test_public_download_site_uses_real_product_assets_and_dynamic_release_data() -> None:
    html = (SITE / "index.html").read_text(encoding="utf-8")
    javascript = next(SITE.glob("site.*.js")).read_text(encoding="utf-8")

    assert "每次继续" not in html
    assert "上次的" not in html
    assert "Agent工作新范式" in html
    assert "从自己干到通过agent快速落地想法。" in html
    assert "企业智能体桌面工作区" in html
    assert "emate-logo.e0bf52b1480f.png" in html
    assert "emate-desktop-workspace.622f3434f88c.jpg" in html
    assert "emate-download-robot.9fbe832b9873.png" in html
    assert "e-mate-hero-decor.d7f99a88447b.png" in html
    assert "emate-download-icon.5014add964e1.svg" in html
    assert "emate-platform-apple.0bed6ae6a1b9.png" in html
    assert "emate-platform-windows.dd86c8094b5a.png" in html
    assert "创意中心" not in html
    assert 'href="/ecorex-agent/admin/"' in html
    assert 'href="/admin/"' not in html
    assert 'data-platform="macos"' in html
    assert 'data-platform="windows"' in html
    assert "/e-mate/update/download-index.json" in javascript
    assert "normalizeDownloadIndex" in javascript
    assert "targetFromPlatformSignals" in javascript
    assert "downloadSources(index, download.target)" in javascript
    assert "link.href = sources[0]" in javascript
    assert "github.com" not in javascript
    assert "ghproxy" not in javascript and "ghfast" not in javascript
    assert 'summary.textContent = "核对 SHA-256"' in javascript
    assert 'label.textContent = "立即下载"' in javascript
    assert "WEBUI_RELEASE" not in javascript
    for version in ("1.0.0", "2.0.0"):
        assert version not in html
        assert version not in javascript


def test_public_download_site_exposes_cow_style_shared_runtime_webui() -> None:
    html = (SITE / "index.html").read_text(encoding="utf-8")
    webui = (SITE / "webui.html").read_text(encoding="utf-8")
    javascript = next(SITE.glob("site.*.js")).read_text(encoding="utf-8")

    assert 'href="./webui.html"' in html
    assert "WebUI 浏览器入口" in html
    assert "http://127.0.0.1:8765/" in webui
    assert "同一个本机 Runtime" in webui
    assert "无需安装第二套运行环境" in webui
    assert "先打开 e-Mate 桌面端" in webui
    assert 'data-copy-webui-command="macos"' in webui
    assert 'data-copy-webui-command="windows"' in webui
    assert 'data-standalone-install hidden' in webui
    assert "curl -fsSL https://dl.ecoremedia.net/e-mate/update/install-webui.sh | sh" in webui
    assert "irm https://dl.ecoremedia.net/e-mate/update/install-webui.ps1 | iex" in webui
    assert "loadStandaloneAdmission" in javascript
    assert "/e-mate/update/public-bootstrap-index.json" in javascript
    assert "pub-ada3f610c0234a76838f4e19fe2bb25e.r2.dev" in javascript
    assert "github.com" not in webui and "mvdcm" not in webui and "aliyun" not in webui
    assert "navigator.clipboard.writeText" in javascript
    assert "9899" not in webui
    assert "9909" not in webui


def test_macos_unsigned_install_guide_is_local_accessible_and_safe() -> None:
    html = (SITE / "index.html").read_text(encoding="utf-8")
    guide = (SITE / "install-macos.html").read_text(encoding="utf-8")

    assert 'data-mac-install-guide hidden href="./install-macos.html"' in html
    assert "DMG" in guide
    assert "应用程序" in guide
    assert "按住 Control 键点按" in guide
    assert "仍要打开" in guide
    assert "先核对下载页标注的 SHA-256" in guide
    assert 'xattr -dr com.apple.quarantine "/Applications/e-Mate.app"' in guide
    assert 'open -a "e-Mate"' in guide
    assert "sudo" not in guide
    assert "spctl --master-disable" not in guide
    assert 'data-copy-macos-command aria-label="复制允许打开命令"' in guide
    references = re.findall(r'(?:src|href)="([^"]+)"', guide)
    assert not any(
        value.startswith(("http://", "https://", "//")) for value in references
    )


def test_public_release_routes_hide_channel_but_map_to_channel_storage() -> None:
    caddy = (SITE / "caddy/ecorex-agent.routes.caddy").read_text(encoding="utf-8")
    nginx = (SITE / "nginx/ecorex-agent.conf.example").read_text(encoding="utf-8")

    canonical = (
        "/ecorex-agent/releases/v9.8.7/"
        "release-stable-0123456789abcdef01234567/release-manifest.json"
    )
    assert "/stable/release-stable-" not in canonical
    assert "(?P<release_channel>stable|canary)" in caddy
    assert "{re.ecorex_release_file.release_channel}" in caddy
    assert "(?<ecorex_release_channel>stable|canary)" in nginx
    assert "$ecorex_release_channel/$ecorex_release_id" in nginx


def test_public_asset_builder_writes_new_hashes_before_switching_html(
    tmp_path: Path,
) -> None:
    site = tmp_path / "site"
    site.mkdir()
    stale_script = site / "site.000000000000.js"
    stale_style = site / "styles.000000000000.css"
    script_payload = b'export const product = "e-Mate";\n'
    style_payload = b":root { color-scheme: light; }\n"
    stale_script.write_bytes(script_payload)
    stale_style.write_bytes(style_payload)
    (site / "index.html").write_text(
        '<link rel="stylesheet" href="./styles.000000000000.css">\n'
        '<script type="module" src="./site.000000000000.js"></script>\n',
        encoding="utf-8",
    )
    (site / "install-macos.html").write_text(
        '<link rel="stylesheet" href="./styles.000000000000.css">\n'
        '<script type="module" src="./site.000000000000.js"></script>\n',
        encoding="utf-8",
    )

    command = [
        sys.executable,
        "scripts/build-v1-public-download-site.py",
        "--site-root",
        str(site),
    ]
    first = subprocess.run(
        command,
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="strict",
    )
    assert first.returncode == 0, first.stdout + first.stderr
    result = json.loads(first.stdout)
    script_name = f"site.{hashlib.sha256(script_payload).hexdigest()[:12]}.js"
    style_name = f"styles.{hashlib.sha256(style_payload).hexdigest()[:12]}.css"
    assert result["javascript"]["name"] == script_name
    assert result["stylesheet"]["name"] == style_name
    assert not stale_script.exists()
    assert not stale_style.exists()
    html = (site / "index.html").read_text(encoding="utf-8")
    assert f'./{script_name}' in html
    assert f'./{style_name}' in html
    guide = (site / "install-macos.html").read_text(encoding="utf-8")
    assert f'./{script_name}' in guide
    assert f'./{style_name}' in guide


def test_public_browser_download_index_contract_fails_closed() -> None:
    node = shutil.which("node")
    if node is None:
        candidate = ROOT / ".candidate/toolchains/node-v22.23.1-darwin-arm64/bin/node"
        node = str(candidate) if candidate.exists() else None
    if node is None:
        pytest.skip("Node.js is required for the public browser contract test")
    script = r"""
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";

const source = await readFile(process.argv[1], "utf8");
const contract = await import(`data:text/javascript;base64,${Buffer.from(source).toString("base64")}`);
const version = "9.8.7";
const download = (target, platform, architecture, fileName) => ({
  target,
  platform,
  architecture,
  file_name: fileName,
  url: `https://pub-ada3f610c0234a76838f4e19fe2bb25e.r2.dev/desktop/v${version}/${fileName}`,
  size_bytes: 42,
  sha256: "a".repeat(64),
});
const raw = {
  schema_version: 2,
  product: "e-Mate",
  version,
  distribution_mode: "unsigned-manual",
  released_at: "2026-08-09T00:00:00Z",
  downloads: [
    download("windows-x64", "windows", "x64", `e-Mate-Setup-${version}-x64.exe`),
    download("macos-arm64", "macos", "arm64", `e-Mate-${version}-arm64.dmg`),
    download("macos-x64", "macos", "x64", `e-Mate-${version}-x64.dmg`),
  ],
};
const signed = structuredClone(raw);
signed.schema_version = 1;
delete signed.distribution_mode;
raw.downloads[0].authenticode = { status: "verified", signer_certificate_thumbprint: "C".repeat(40) };
assert.equal(contract.normalizeDownloadIndex(raw).version, version);
assert.equal(contract.normalizeDownloadIndex(raw).distribution_mode, "unsigned-manual");
assert.equal(contract.normalizeDownloadIndex(raw).downloads[0].authenticode.status, "verified");
assert.deepEqual(contract.downloadSources(contract.normalizeDownloadIndex(raw), "windows-x64"), [
  `https://pub-ada3f610c0234a76838f4e19fe2bb25e.r2.dev/desktop/v${version}/e-Mate-Setup-${version}-x64.exe`,
]);
assert.deepEqual(contract.downloadSources(contract.normalizeDownloadIndex(raw), "unknown"), []);
assert.deepEqual(contract.installationTrustCopy(contract.normalizeDownloadIndex(raw)), {
  release: "Windows 已签名 · macOS 手动安装（未签名）",
  help: "Windows 安装包已验证数字签名；macOS 暂未签名，请按系统提示允许打开。",
});
const unsignedManual = structuredClone(raw);
delete unsignedManual.downloads[0].authenticode;
assert.deepEqual(contract.installationTrustCopy(contract.normalizeDownloadIndex(unsignedManual)), {
  release: "手动安装（未签名）",
  help: "当前候选暂未签名，请按系统提示允许打开。",
});
assert.throws(() => contract.normalizeDownloadIndex({ ...raw, distribution_mode: "unknown" }));
assert.equal(contract.normalizeDownloadIndex(signed).distribution_mode, "signed-automatic");
assert.throws(() => contract.normalizeDownloadIndex({ ...raw, downloads: [{ ...raw.downloads[0], authenticode: { status: "verified", signer_certificate_thumbprint: "c".repeat(40) } }, ...raw.downloads.slice(1)] }));
assert.throws(() => contract.normalizeDownloadIndex({ ...raw, downloads: [raw.downloads[0], { ...raw.downloads[1], authenticode: raw.downloads[0].authenticode }, raw.downloads[2]] }));
assert.throws(() => contract.normalizeDownloadIndex({ ...raw, version: `v${version}` }));
assert.throws(() => contract.normalizeDownloadIndex({ ...raw, extra: true }));
assert.throws(() => contract.normalizeDownloadIndex({ ...raw, downloads: [raw.downloads[0], raw.downloads[0], raw.downloads[2]] }));
assert.throws(() => contract.normalizeDownloadIndex({ ...raw, downloads: [{ ...raw.downloads[0], url: "https://evil.invalid/app.exe" }, ...raw.downloads.slice(1)] }));
assert.equal(contract.targetFromPlatformSignals({ source: "Windows NT" }), "windows-x64");
assert.equal(contract.targetFromPlatformSignals({ source: "MacIntel", architecture: "arm" }), "macos-arm64");
assert.equal(contract.targetFromPlatformSignals({ source: "MacIntel", architecture: "x86_64" }), "macos-x64");
assert.equal(contract.targetFromPlatformSignals({ source: "MacIntel" }), null);
assert.equal(contract.targetFromPlatformSignals({ source: "iPhone", architecture: "arm64" }), null);
assert.equal(contract.targetFromPlatformSignals({ source: "iPad", renderer: "Apple M2" }), null);
assert.equal(contract.isMacDesktop({ source: "MacIntel Mozilla/5.0 (Macintosh)" }), true);
assert.equal(contract.isMacDesktop({ source: "Win32 Mozilla/5.0 (Windows NT 10.0)" }), false);
assert.equal(contract.isMacDesktop({ source: "iPhone Mac OS X" }), false);
assert.deepEqual(contract.indexSources({ hostname: "127.0.0.1", pathname: "/" }), ["./download-index.json"]);
assert.deepEqual(contract.indexSources({ hostname: "mvdcm.ecoremedia.net", pathname: "/e-mate/" }), ["/e-mate/update/download-index.json"]);
assert.deepEqual(contract.indexSources({ hostname: "dl.ecoremedia.net", pathname: "/ecorex-agent/" }), ["/e-mate/update/download-index.json"]);
assert.deepEqual(contract.indexSources({ hostname: "example.invalid", pathname: "/" }), ["/e-mate/update/download-index.json"]);
const bootstrap = (platform, architecture) => {
  const file_name = `bootstrap-${platform}-${architecture}.zip`;
  return { platform, architecture, file_name, sha256: "b".repeat(64), sources: [{
    url: `https://pub-ada3f610c0234a76838f4e19fe2bb25e.r2.dev/desktop/v${version}/${file_name}`,
  }] };
};
const standalone = {
  schema_version: 1,
  document_type: "ecorex.public-bootstrap-discovery",
  trust: "untrusted-discovery-hint",
  status: "published",
  freshness: { issued_at: "2020-01-01T00:00:00Z", expires_at: "2099-01-01T00:00:00Z" },
  release: {
    version,
    manifest: { sources: [{ url: `https://pub-ada3f610c0234a76838f4e19fe2bb25e.r2.dev/desktop/v${version}/release-manifest.json` }] },
    bootstrap_artifacts: [bootstrap("windows", "x64"), bootstrap("macos", "arm64"), bootstrap("macos", "x64")],
  },
};
globalThis.fetch = async () => ({ ok: true, text: async () => JSON.stringify(standalone) });
assert.equal(await contract.loadStandaloneAdmission(), true);
standalone.release.bootstrap_artifacts[0].sources[0].url = "https://github.com/old/bootstrap.zip";
await assert.rejects(contract.loadStandaloneAdmission());
"""
    source = next(SITE.glob("site.*.js"))
    result = subprocess.run(
        [node, "--input-type=module", "-e", script, str(source)],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
