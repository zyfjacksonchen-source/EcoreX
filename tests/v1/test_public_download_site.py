from __future__ import annotations

import json
import hashlib
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]


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
    assert evidence["hashed_asset_count"] == 5
    html = (ROOT / "deploy" / "ecorex-site" / "index.html").read_text(
        encoding="utf-8"
    )
    assert 'href="/ecorex-agent/admin/"' in html
    assert 'href="/admin/"' not in html


def test_public_download_site_makes_one_click_terminal_install_primary() -> None:
    site = ROOT / "deploy" / "ecorex-site"
    html = (site / "index.html").read_text(encoding="utf-8")
    javascript = next(site.glob("site.*.js")).read_text(encoding="utf-8")

    assert "<title>EcoreX 下载与安装</title>" in html
    assert "<strong>选择系统</strong>" in html
    assert "<strong>复制一键命令</strong>" in html
    assert "<strong>粘贴并执行</strong>" in html
    assert "点击对应卡片中的“复制命令”。" in html
    assert "安装完成后会自动打开 EcoreX 并创建桌面快捷方式。" in html
    assert all(
        technical_term not in html
        for technical_term in ("Bootstrap", "SHA-256", "Ed25519")
    )

    assert 'createElement("div", "command-block is-primary")' in javascript
    assert 'createElement("button", "", "复制命令")' in javascript
    assert "await copyText(command);" in javascript
    assert "appendTerminalCommand(article, artifact);" in javascript
    assert 'createElement("a", "download-link", "下载 EcoreX")' not in javascript
    assert 'createElement("details", "download-help")' not in javascript


def test_public_release_routes_hide_channel_but_map_to_channel_storage() -> None:
    caddy = (
        ROOT / "deploy/ecorex-site/caddy/ecorex-agent.routes.caddy"
    ).read_text(encoding="utf-8")
    nginx = (
        ROOT / "deploy/ecorex-site/nginx/ecorex-agent.conf.example"
    ).read_text(encoding="utf-8")

    canonical = (
        "/ecorex-agent/releases/v1.0.0/"
        "release-stable-0123456789abcdef01234567/release-manifest.json"
    )
    assert "/stable/release-stable-" not in canonical
    assert "(?P<release_channel>stable|canary)" in caddy
    assert (
        "/{re.ecorex_release_file.release_namespace}/"
        "{re.ecorex_release_file.release_channel}/"
        "{re.ecorex_release_file.release_id}/"
        "{re.ecorex_release_file.release_asset}"
    ) in caddy
    assert "(?<ecorex_release_channel>stable|canary)" in nginx
    assert (
        "/v1-artifacts/$ecorex_release_namespace/"
        "$ecorex_release_channel/$ecorex_release_id/$ecorex_release_asset"
    ) in nginx
    assert "handle /ecorex-agent/releases/*" in caddy
    assert "location /ecorex-agent/releases/" in nginx


def test_public_asset_builder_writes_new_hashes_before_switching_html(
    tmp_path: Path,
) -> None:
    site = tmp_path / "site"
    site.mkdir()
    stale_script = site / "site.000000000000.js"
    stale_style = site / "styles.000000000000.css"
    script_payload = b'export const product = "EcoreX";\n'
    style_payload = b":root { color-scheme: light dark; }\n"
    stale_script.write_bytes(script_payload)
    stale_style.write_bytes(style_payload)
    (site / "index.html").write_text(
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
    assert (site / script_name).read_bytes() == script_payload
    assert (site / style_name).read_bytes() == style_payload
    assert not stale_script.exists()
    assert not stale_style.exists()
    html = (site / "index.html").read_text(encoding="utf-8")
    assert f'./{script_name}' in html
    assert f'./{style_name}' in html
    assert not list(site.glob(".*.tmp-*"))

    interrupted_payload = b"unreferenced bytes written before an HTML switch\n"
    interrupted_name = (
        f"site.{hashlib.sha256(interrupted_payload).hexdigest()[:12]}.js"
    )
    (site / interrupted_name).write_bytes(interrupted_payload)
    second = subprocess.run(
        command,
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="strict",
    )
    assert second.returncode == 0, second.stdout + second.stderr
    assert json.loads(second.stdout) == result
    assert not (site / interrupted_name).exists()


def test_public_browser_parser_and_manifest_byte_check_fail_closed() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for the public browser contract test")
    script = r"""
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { pathToFileURL } from "node:url";

const sourcePath = process.argv[1];
const source = await readFile(sourcePath, "utf8");
const moduleUrl = `data:text/javascript;base64,${Buffer.from(source).toString("base64")}`;
const contract = await import(moduleUrl);

const windowsCommand = contract.terminalCommand({
  platform: "windows",
  sha256: "a".repeat(64),
  sources: [
    { url: "https://mirror.example/EcoreX.zip" },
    { url: "https://github.example/EcoreX.zip" },
  ],
});
assert.match(windowsCommand, /curl\.exe/);
assert.match(windowsCommand, /Write-Host/);
assert.match(windowsCommand, /速度和剩余时间/);
assert.match(windowsCommand, /Get-FileHash/);
assert.match(windowsCommand, /github\.example/);
assert.match(windowsCommand, /ecorex-bootstrap\.exe/);
const macCommand = contract.terminalCommand({
  platform: "macos",
  sha256: "b".repeat(64),
  sources: [
    { url: "https://mirror.example/EcoreX.zip" },
    { url: "https://github.example/EcoreX.zip" },
  ],
});
assert.match(macCommand, /curl --fail --location/);
assert.match(macCommand, /速度和剩余时间/);
assert.match(macCommand, /shasum -a 256 -c/);
assert.match(macCommand, /github\.example/);
assert.match(macCommand, /ecorex-bootstrap/);

const oneSource = contract.sourceList(
  [
    {
      source_id: "mirror",
      kind: "github-cn-mirror",
      priority: 0,
      url: "https://mirror.example/EcoreX.exe",
    },
  ],
  "sources",
  "EcoreX.exe",
);
assert.equal(oneSource.length, 1);
assert.equal(oneSource[0].kind, "github-cn-mirror");
assert.equal(contract.sourceList(
  [
    {
      source_id: "mirror",
      kind: "github-cn-mirror",
      priority: 0,
      url: "https://mirror.example/EcoreX.exe",
    },
    {
      source_id: "github",
      kind: "github-release",
      priority: 1,
      url: "https://github.example/EcoreX.exe",
    },
  ],
  "sources",
  "EcoreX.exe",
).length, 2);
assert.throws(() => contract.sourceList([], "sources", "EcoreX.exe"));
assert.throws(() => contract.sourceList(
  [
    {
      source_id: "github",
      kind: "github-release",
      priority: 0,
      url: "https://github.example/EcoreX.exe",
    },
  ],
  "sources",
  "EcoreX.exe",
));

assert.deepEqual(
  contract.normalizePublicIndex({
    schema_version: 1,
    document_type: "ecorex.public-bootstrap-discovery",
    trust: "untrusted-discovery-hint",
    status: "unpublished",
    authority: null,
    freshness: null,
    release: null,
  }),
  { status: "unpublished", trust: "untrusted-discovery-hint" },
);
assert.throws(() => contract.normalizePublicIndex({
  schema_version: 1,
  document_type: "ecorex.public-bootstrap-discovery",
  trust: "untrusted-discovery-hint",
  status: "unpublished",
  authority: null,
  freshness: null,
  release: null,
  download_url: "https://ready.invalid/fake.exe",
}));

const exact = new TextEncoder().encode("exact signed manifest bytes").buffer;
const expected = await contract.sha256Hex(exact);
const sources = [
  { sourceId: "mirror", kind: "github-cn-mirror", priority: 0, url: "https://mirror.example/release-manifest.json", baseUrl: "https://mirror.example" },
  { sourceId: "github", kind: "github-release", priority: 1, url: "https://github.example/release-manifest.json", baseUrl: "https://github.example" },
  { sourceId: "cdn", kind: "ecorex-cdn", priority: 2, url: "https://cdn.example/release-manifest.json", baseUrl: "https://cdn.example" },
];
let calls = 0;
const checked = await contract.verifyManifestBytes(
  { sha256: expected, sources },
  {
    fetchImpl: async (url) => {
      calls += 1;
      const payload = calls === 1 ? new TextEncoder().encode("wrong").buffer : exact;
      return {
        ok: true,
        url,
        headers: { get: () => String(payload.byteLength) },
        arrayBuffer: async () => payload,
      };
    },
  },
);
assert.equal(calls, 2);
assert.equal(checked.kind, "github-release");
let cancelled = false;
let oversizedReads = 0;
const bounded = await contract.verifyManifestBytes(
  { sha256: expected, sources },
  {
    fetchImpl: async (url) => {
      if (url.includes("mirror")) {
        return {
          ok: true,
          url,
          headers: { get: () => null },
          body: {
            getReader: () => ({
              read: async () => {
                oversizedReads += 1;
                return oversizedReads === 1
                  ? { done: false, value: new Uint8Array(1024 * 1024 + 1) }
                  : { done: true };
              },
              cancel: async () => { cancelled = true; },
              releaseLock: () => {},
            }),
          },
        };
      }
      return {
        ok: true,
        url,
        headers: { get: () => String(exact.byteLength) },
        arrayBuffer: async () => exact,
      };
    },
  },
);
assert.equal(cancelled, true);
assert.equal(bounded.kind, "github-release");
await assert.rejects(() => contract.verifyManifestBytes(
  { sha256: "f".repeat(64), sources },
  {
    fetchImpl: async (url) => ({
      ok: true,
      url,
      headers: { get: () => String(exact.byteLength) },
      arrayBuffer: async () => exact,
    }),
  },
));
"""
    javascript = next((ROOT / "deploy/ecorex-site").glob("site.*.js"))
    result = subprocess.run(
        [node, "--input-type=module", "-e", script, str(javascript)],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="strict",
    )
    assert result.returncode == 0, result.stdout + result.stderr
