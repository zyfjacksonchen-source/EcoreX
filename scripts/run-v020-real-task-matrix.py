#!/usr/bin/env python3
"""Run a 30-task EcoreX v0.2.0 validation matrix.

The matrix intentionally avoids model calls while still exercising real local
runtime, public release, packaged archive, and backend invariants that map to
the v0.2.0 production fixes.
"""

from __future__ import annotations

import argparse
import hashlib
import http.server
import json
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import zipfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable


VERSION = "0.2.0"
PUBLIC_BASE = "https://www.ecoreai.cn/ecorex-agent"
EXPECTED_PUBLIC_SHA = "000020E5E46BE4409F7933A957AAE21FB55513DC70E244D0BD2979617C3E9822"
EXPECTED_WIN_SHA = "F97A17E5A72F4932478946E8396E77DA4EDA5069ED8AB2B4BEEBBD31D67F6641"
EXPECTED_MAC_SHA = "3DA292C890CF1DD1786EE0D591542481805AD1337CCD01978FFF1BECE0A869CF"
RENDERER_ASSET = "assets/index-Cq3X-tPV.js"


class MatrixFailure(AssertionError):
    pass


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest().upper()


def http_json(url: str, timeout: int = 20) -> dict[str, Any]:
    req = urllib.request.Request(url, headers={"User-Agent": "EcoreX-v020-matrix/1"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        if resp.status != 200:
            raise MatrixFailure(f"{url} returned HTTP {resp.status}")
        return json.loads(resp.read().decode("utf-8"))


def http_text(url: str, timeout: int = 20) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "EcoreX-v020-matrix/1"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        if resp.status != 200:
            raise MatrixFailure(f"{url} returned HTTP {resp.status}")
        return resp.read().decode("utf-8", errors="replace")


def http_head(url: str, timeout: int = 20) -> tuple[int, int]:
    req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "EcoreX-v020-matrix/1"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        length = int(resp.headers.get("Content-Length") or 0)
        return resp.status, length


def http_sha256_cached(url: str, expected_length: int, expected_sha: str, cache_path: Path, timeout: int = 900) -> dict[str, Any]:
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    if cache_path.is_file() and cache_path.stat().st_size == expected_length:
        actual = sha256_file(cache_path)
        if actual == expected_sha:
            return {"url": url, "status": 200, "length": expected_length, "sha256": actual, "source": "cache", "path": str(cache_path)}
        cache_path.unlink()

    partial_path = Path(str(cache_path) + ".part")
    resume_from = partial_path.stat().st_size if partial_path.exists() else 0
    if resume_from >= expected_length:
        actual = sha256_file(partial_path)
        if actual == expected_sha:
            partial_path.replace(cache_path)
            return {"url": url, "status": 200, "length": expected_length, "sha256": actual, "source": "completed-partial", "path": str(cache_path)}
        partial_path.unlink()
        resume_from = 0

    headers = {"User-Agent": "EcoreX-v020-matrix/1"}
    if resume_from > 0:
        headers["Range"] = f"bytes={resume_from}-"
    req = urllib.request.Request(url, headers=headers)
    h = hashlib.sha256()
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        if resume_from > 0 and resp.status == 200:
            partial_path.unlink(missing_ok=True)
            resume_from = 0
        elif resume_from > 0 and resp.status != 206:
            raise MatrixFailure(f"{url} did not resume from {resume_from}: HTTP {resp.status}")
        elif resume_from == 0 and resp.status != 200:
            raise MatrixFailure(f"{url} returned HTTP {resp.status}")
        length = int(resp.headers.get("Content-Length") or 0)
        expected_response_length = expected_length - resume_from
        if expected_response_length > 0 and length != expected_response_length:
            raise MatrixFailure(f"{url} length mismatch {length} != {expected_length}")
        mode = "ab" if resume_from > 0 else "wb"
        with partial_path.open(mode + "") as f:
            while True:
                chunk = resp.read(1024 * 1024)
                if not chunk:
                    break
                f.write(chunk)
    total = partial_path.stat().st_size
    if expected_length > 0 and total != expected_length:
        raise MatrixFailure(f"{url} streamed length mismatch {total} != {expected_length}")
    actual = sha256_file(partial_path)
    if actual != expected_sha:
        raise MatrixFailure(f"{url} sha mismatch {actual} != {expected_sha}")
    partial_path.replace(cache_path)
    return {"url": url, "status": 200, "length": total, "sha256": actual, "source": "downloaded", "path": str(cache_path)}


def http_sha256(url: str, expected_length: int, timeout: int = 120) -> dict[str, Any]:
    req = urllib.request.Request(url, headers={"User-Agent": "EcoreX-v020-matrix/1"})
    h = hashlib.sha256()
    total = 0
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        if resp.status != 200:
            raise MatrixFailure(f"{url} returned HTTP {resp.status}")
        length = int(resp.headers.get("Content-Length") or 0)
        if expected_length > 0 and length != expected_length:
            raise MatrixFailure(f"{url} length mismatch {length} != {expected_length}")
        while True:
            chunk = resp.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            h.update(chunk)
    if expected_length > 0 and total != expected_length:
        raise MatrixFailure(f"{url} streamed length mismatch {total} != {expected_length}")
    return {"url": url, "status": 200, "length": total, "sha256": h.hexdigest().upper()}


class _RangeDownloadHandler(http.server.BaseHTTPRequestHandler):
    payload = b""
    requests_seen: list[dict[str, Any]] = []

    def log_message(self, _format: str, *_args: Any) -> None:
        return

    def do_HEAD(self) -> None:  # noqa: N802 - stdlib hook
        self.send_response(200)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(len(self.payload)))
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802 - stdlib hook
        range_header = self.headers.get("Range") or ""
        self.requests_seen.append({"range": range_header})
        payload = self.payload
        start = 0
        if range_header.startswith("bytes=") and range_header.endswith("-"):
            try:
                start = int(range_header[len("bytes="):-1])
            except ValueError:
                start = 0
            if start >= len(payload):
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{len(payload)}")
                self.end_headers()
                return
            body = payload[start:]
            self.send_response(206)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Range", f"bytes {start}-{len(payload) - 1}/{len(payload)}")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        self.send_response(200)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def _with_range_server(payload: bytes, fn: Callable[[str, list[dict[str, Any]]], dict[str, Any]]) -> dict[str, Any]:
    class Handler(_RangeDownloadHandler):
        pass

    Handler.payload = payload
    Handler.requests_seen = []
    server = http.server.ThreadingHTTPServer(("0.0.0.0", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_port}/package.zip"
        return fn(url, Handler.requests_seen)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _wsl_path(path: Path) -> str:
    resolved = path.resolve()
    drive = resolved.drive.rstrip(":").lower()
    if not drive:
        return resolved.as_posix()
    rest = resolved.as_posix()[len(resolved.drive):]
    return f"/mnt/{drive}{rest}"


def _wsl_url_for_windows_localhost(url: str) -> str:
    try:
        proc = subprocess.run(
            ["bash", "-lc", "awk '/^nameserver /{print $2; exit}' /etc/resolv.conf"],
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=60,
            check=False,
        )
        host = proc.stdout.strip() if proc.returncode == 0 else ""
    except subprocess.TimeoutExpired:
        host = ""
    if host:
        return url.replace("127.0.0.1", host)
    return url


def assert_true(condition: bool, message: str) -> None:
    if not condition:
        raise MatrixFailure(message)


def artifact_by_id(manifest: dict[str, Any], artifact_id: str) -> dict[str, Any]:
    artifacts = manifest.get("artifacts") if isinstance(manifest.get("artifacts"), list) else []
    for artifact in artifacts:
        if isinstance(artifact, dict) and artifact.get("id") == artifact_id:
            return artifact
    raise MatrixFailure(f"manifest missing artifact {artifact_id}")


def zip_contains(path: Path, predicates: list[Callable[[str, bytes], bool]]) -> list[bool]:
    found = [False] * len(predicates)
    with zipfile.ZipFile(path) as zf:
        for name in zf.namelist():
            if not (
                name.endswith((".py", ".html", ".js", ".css", ".json", ".ps1", ".sh"))
                or name == RENDERER_ASSET
                or RENDERER_ASSET in name
            ):
                continue
            data = b""
            needs_data = any(not state for state in found)
            if needs_data:
                try:
                    data = zf.read(name)
                except Exception:
                    data = b""
            for index, predicate in enumerate(predicates):
                if not found[index] and predicate(name, data):
                    found[index] = True
    return found


def zip_read_by_suffix(path: Path, suffix: str) -> tuple[str, bytes]:
    normalized_suffix = suffix.replace("\\", "/")
    with zipfile.ZipFile(path) as zf:
        matches = [
            name for name in zf.namelist()
            if name.replace("\\", "/").endswith(normalized_suffix)
        ]
        if len(matches) != 1:
            raise MatrixFailure(f"{path.name} expected one {suffix}, found {len(matches)}")
        return matches[0], zf.read(matches[0])


def zip_assert_markers(path: Path, checks: dict[str, list[bytes]]) -> dict[str, Any]:
    evidence: dict[str, Any] = {}
    for suffix, markers in checks.items():
        name, data = zip_read_by_suffix(path, suffix)
        missing = [marker.decode("utf-8", errors="replace") for marker in markers if marker not in data]
        assert_true(not missing, f"{path.name}:{suffix} missing markers {missing}")
        evidence[suffix] = {"entry": name, "markers": [m.decode("utf-8", errors="replace") for m in markers]}
    return {"path": str(path), "entries": evidence}


def run_gh_assets(root: Path) -> dict[str, dict[str, Any]]:
    proc = subprocess.run(
        ["gh", "release", "view", f"v{VERSION}", "--json", "assets"],
        cwd=root,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=60,
        check=False,
    )
    if proc.returncode != 0:
        raise MatrixFailure(f"gh release view failed: {proc.stderr.strip()}")
    payload = json.loads(proc.stdout)
    return {
        str(asset.get("name")): asset
        for asset in payload.get("assets", [])
        if isinstance(asset, dict)
    }


def add_task(tasks: list[tuple[str, Callable[[], dict[str, Any]]]], name: str, fn: Callable[[], dict[str, Any]]) -> None:
    tasks.append((name, fn))


def build_tasks(root: Path) -> list[tuple[str, Callable[[], dict[str, Any]]]]:
    sys.path.insert(0, str(root))
    release_dir = root / "release-artifacts"
    public_zip = release_dir / f"EcoreX_{VERSION}-public-release.zip"
    win_zip = release_dir / f"EcoreX_{VERSION}-webui-windows-x64.zip"
    mac_zip = release_dir / f"EcoreX_{VERSION}-webui-macos-universal.zip"
    public_manifest_url = f"{PUBLIC_BASE}/manifest.json"

    manifest_cache: dict[str, Any] = {}
    gh_cache: dict[str, dict[str, Any]] = {}

    def manifest() -> dict[str, Any]:
        if not manifest_cache:
            manifest_cache.update(http_json(public_manifest_url))
        return manifest_cache

    def gh_assets() -> dict[str, dict[str, Any]]:
        if not gh_cache:
            gh_cache.update(run_gh_assets(root))
        return gh_cache

    tasks: list[tuple[str, Callable[[], dict[str, Any]]]] = []

    add_task(tasks, "local-webui-version", lambda: _local_version())
    add_task(tasks, "local-release-notes-hide-run-center", lambda: _local_notes_no_run_center())
    add_task(tasks, "public-root-http-200", lambda: _http_status(f"{PUBLIC_BASE}/", 200))
    add_task(tasks, "public-manifest-version", lambda: _manifest_version(manifest()))
    add_task(tasks, "manifest-windows-webui-ready", lambda: _manifest_artifact(manifest(), "webui-windows-x64", EXPECTED_WIN_SHA))
    add_task(tasks, "manifest-macos-webui-ready", lambda: _manifest_artifact(manifest(), "webui-macos-universal", EXPECTED_MAC_SHA))
    add_task(tasks, "public-windows-download-sha", lambda: _download_sha(root, manifest(), "webui-windows-x64", EXPECTED_WIN_SHA))
    add_task(tasks, "public-macos-download-sha", lambda: _download_sha(root, manifest(), "webui-macos-universal", EXPECTED_MAC_SHA))
    add_task(tasks, "local-public-zip-sha", lambda: _local_sha(public_zip, EXPECTED_PUBLIC_SHA))
    add_task(tasks, "local-windows-zip-sha", lambda: _local_sha(win_zip, EXPECTED_WIN_SHA))
    add_task(tasks, "local-macos-zip-sha", lambda: _local_sha(mac_zip, EXPECTED_MAC_SHA))
    add_task(tasks, "windows-zip-runtime-markers", lambda: _zip_runtime_markers(win_zip))
    add_task(tasks, "windows-zip-finalization-modules", lambda: _zip_finalization_modules(win_zip))
    add_task(tasks, "macos-zip-runtime-markers", lambda: _zip_runtime_markers(mac_zip))
    add_task(tasks, "macos-zip-finalization-modules", lambda: _zip_finalization_modules(mac_zip))
    add_task(tasks, "windows-zip-renderer-asset", lambda: _zip_name_marker(win_zip, RENDERER_ASSET))
    add_task(tasks, "macos-zip-renderer-asset", lambda: _zip_name_marker(mac_zip, RENDERER_ASSET))
    add_task(tasks, "github-windows-webui-digest", lambda: _gh_digest(gh_assets(), f"EcoreX_{VERSION}-webui-windows-x64.zip", EXPECTED_WIN_SHA))
    add_task(tasks, "github-macos-webui-digest", lambda: _gh_digest(gh_assets(), f"EcoreX_{VERSION}-webui-macos-universal.zip", EXPECTED_MAC_SHA))
    add_task(tasks, "windows-install-download-resume-simulation", lambda: _windows_installer_resume_simulation(root))
    add_task(tasks, "macos-install-download-resume-simulation", lambda: _macos_installer_resume_simulation(root))
    add_task(tasks, "feishu-distinct-chat-batch-not-blocked", lambda: _feishu_distinct_batch())
    add_task(tasks, "feishu-same-chat-repeat-blocked", lambda: _feishu_same_chat_blocked())
    add_task(tasks, "tool-schema-database-boundary", lambda: _tool_schema_database_boundary())
    add_task(tasks, "tool-schema-explicit-feishu-with-mcp", lambda: _tool_schema_explicit_feishu())
    add_task(tasks, "tool-schema-fallback-avoids-feishu", lambda: _tool_schema_fallback_avoids_feishu())
    add_task(tasks, "executor-run-stream-final-response-visible", lambda: _executor_run_stream_final_response_visible())
    add_task(tasks, "bridge-final-response-synthesized", lambda: _bridge_final_response_synthesized())
    add_task(tasks, "bridge-cancelled-response-not-synthesized", lambda: _bridge_cancelled_not_synthesized())
    add_task(tasks, "conversation-store-latest-bot-seq-text", lambda: _conversation_store_bot_seq())

    assert_true(len(tasks) == 30, f"expected 30 tasks, got {len(tasks)}")
    return tasks


def _local_version() -> dict[str, Any]:
    payload = http_json("http://127.0.0.1:9909/api/version", timeout=5)
    assert_true(payload.get("version") == VERSION, f"local version mismatch: {payload.get('version')}")
    return {"version": payload.get("version")}


def _local_notes_no_run_center() -> dict[str, Any]:
    payload = http_json("http://127.0.0.1:9909/api/version", timeout=5)
    text = json.dumps(payload.get("releaseNotes") or {}, ensure_ascii=False)
    assert_true("Run Center" not in text, "release notes still mention Run Center")
    return {"releaseNotesChars": len(text)}


def _http_status(url: str, expected: int) -> dict[str, Any]:
    status, length = http_head(url)
    assert_true(status == expected, f"{url} returned {status}")
    return {"url": url, "status": status, "length": length}


def _manifest_version(payload: dict[str, Any]) -> dict[str, Any]:
    assert_true(payload.get("product") == "EcoreX", "manifest product mismatch")
    assert_true(payload.get("version") == VERSION, f"manifest version mismatch: {payload.get('version')}")
    return {"product": payload.get("product"), "version": payload.get("version")}


def _manifest_artifact(payload: dict[str, Any], artifact_id: str, expected_sha: str) -> dict[str, Any]:
    artifact = artifact_by_id(payload, artifact_id)
    assert_true(artifact.get("status") == "ready", f"{artifact_id} status={artifact.get('status')}")
    assert_true(str(artifact.get("sha256") or "").upper() == expected_sha, f"{artifact_id} sha mismatch")
    assert_true(int(artifact.get("size") or 0) > 0, f"{artifact_id} has empty size")
    return {"id": artifact_id, "size": artifact.get("size"), "sha256": artifact.get("sha256")}


def _download_head(payload: dict[str, Any], artifact_id: str) -> dict[str, Any]:
    artifact = artifact_by_id(payload, artifact_id)
    url = f"{PUBLIC_BASE}/{str(artifact.get('href')).lstrip('/')}"
    status, length = http_head(url)
    assert_true(status == 200, f"{url} returned {status}")
    assert_true(length == int(artifact.get("size") or -1), f"{artifact_id} length mismatch {length} != {artifact.get('size')}")
    return {"url": url, "status": status, "length": length}


def _download_sha(root: Path, payload: dict[str, Any], artifact_id: str, expected_sha: str) -> dict[str, Any]:
    artifact = artifact_by_id(payload, artifact_id)
    url = f"{PUBLIC_BASE}/{str(artifact.get('href')).lstrip('/')}"
    cache_path = root / "release-artifacts" / "public-download-cache" / str(artifact.get("fileName") or f"{artifact_id}.zip")
    evidence = http_sha256_cached(url, int(artifact.get("size") or 0), expected_sha, cache_path, timeout=1200)
    assert_true(evidence["sha256"] == expected_sha, f"{artifact_id} public download sha mismatch")
    return evidence


def _local_sha(path: Path, expected_sha: str) -> dict[str, Any]:
    assert_true(path.is_file(), f"missing file {path}")
    actual = sha256_file(path)
    assert_true(actual == expected_sha, f"{path.name} sha mismatch: {actual}")
    return {"path": str(path), "size": path.stat().st_size, "sha256": actual}


def _zip_marker(path: Path, marker: bytes) -> dict[str, Any]:
    found = zip_contains(path, [lambda _name, data: marker in data])[0]
    assert_true(found, f"{path.name} missing marker {marker!r}")
    return {"path": str(path), "marker": marker.decode("utf-8", errors="replace")}


def _zip_name_marker(path: Path, name_fragment: str) -> dict[str, Any]:
    with zipfile.ZipFile(path) as zf:
        matches = [name for name in zf.namelist() if name_fragment in name]
    assert_true(bool(matches), f"{path.name} missing {name_fragment}")
    return {"path": str(path), "matches": matches[:3]}


def _zip_runtime_markers(path: Path) -> dict[str, Any]:
    return zip_assert_markers(path, {
        "agent/protocol/agent_stream.py": [
            b"def _feishu_cli_chain_key",
            b"def _ensure_final_response_message",
            b"external-capability-chain-budget",
        ],
    })


def _zip_finalization_modules(path: Path) -> dict[str, Any]:
    return zip_assert_markers(path, {
        "bridge/agent_bridge.py": [
            b"def _ensure_final_response_in_messages",
            b"cancelled by user",
        ],
        "agent/memory/conversation_store.py": [
            b"def get_latest_pair_seqs",
            b"Tool-use-only assistant rows",
            b"skip pure tool_result",
        ],
    })


def _gh_digest(assets: dict[str, dict[str, Any]], name: str, expected_sha: str) -> dict[str, Any]:
    asset = assets.get(name) or {}
    digest = str(asset.get("digest") or "").lower()
    assert_true(digest == f"sha256:{expected_sha.lower()}", f"{name} digest mismatch: {digest}")
    return {"name": name, "size": asset.get("size"), "digest": digest}


def _public_script_markers(file_name: str, markers: list[str]) -> dict[str, Any]:
    text = http_text(f"{PUBLIC_BASE}/{file_name}")
    missing = [marker for marker in markers if marker not in text]
    assert_true(not missing, f"{file_name} missing markers {missing}")
    return {"file": file_name, "markers": markers, "chars": len(text)}


def _ps_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _windows_installer_resume_simulation(root: Path) -> dict[str, Any]:
    script_path = root / "deploy" / "ecorex-site" / "install-webui.ps1"
    text = script_path.read_text(encoding="utf-8")
    start = text.index("function Format-Mib")
    end = text.index("$manifestUrl =")
    functions = text[start:end]
    assert_true("416" in functions and "Partial package was already complete" in functions, "PowerShell installer missing 416 recovery branch")
    payload = (b"EcoreX-Windows-WebUI-resume-test-" * 4096) + bytes(range(256)) * 1024
    expected_sha = hashlib.sha256(payload).hexdigest().upper()

    def run(url: str, requests_seen: list[dict[str, Any]]) -> dict[str, Any]:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            resume_cache = tmp_path / "resume.zip"
            complete_cache = tmp_path / "complete.zip"
            stale_cache = tmp_path / "stale.zip"
            (Path(str(resume_cache) + ".part")).write_bytes(payload[: len(payload) // 3])
            (Path(str(complete_cache) + ".part")).write_bytes(payload)
            (Path(str(stale_cache) + ".part")).write_bytes(b"X" * len(payload))
            harness = tmp_path / "download-resume-test.ps1"
            harness.write_text(
                "\n".join([
                    "$ErrorActionPreference = 'Stop'",
                    functions,
                    f"$uri = {_ps_quote(url)}",
                    f"$sha = {_ps_quote(expected_sha)}",
                    f"$resume = Save-UrlWithProgress -Uri $uri -CachePath {_ps_quote(str(resume_cache))} -WorkDir {_ps_quote(str(tmp_path))} -ExpectedSha256 $sha -Retries 1",
                    "if ((Get-Sha256 -Path $resume) -ne $sha) { throw 'resume hash mismatch' }",
                    f"$complete = Save-UrlWithProgress -Uri $uri -CachePath {_ps_quote(str(complete_cache))} -WorkDir {_ps_quote(str(tmp_path))} -ExpectedSha256 $sha -Retries 1",
                    "if ((Get-Sha256 -Path $complete) -ne $sha) { throw 'complete partial hash mismatch' }",
                    f"$stale = Save-UrlWithProgress -Uri $uri -CachePath {_ps_quote(str(stale_cache))} -WorkDir {_ps_quote(str(tmp_path))} -ExpectedSha256 $sha -Retries 2",
                    "if ((Get-Sha256 -Path $stale) -ne $sha) { throw 'stale partial retry hash mismatch' }",
                    "Write-Host 'OK'",
                ]),
                encoding="utf-8",
            )
            proc = subprocess.run(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(harness)],
                cwd=root,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=90,
                check=False,
            )
            if proc.returncode != 0:
                raise MatrixFailure(f"PowerShell resume simulation failed: {proc.stdout}\n{proc.stderr}")
            ranges = [request.get("range", "") for request in requests_seen if request.get("range")]
            assert_true(any(r == f"bytes={len(payload) // 3}-" for r in ranges), f"missing resume range in {ranges}")
            assert_true(any(r == f"bytes={len(payload)}-" for r in ranges), f"missing 416 range in {ranges}")
            return {"script": str(script_path), "payloadBytes": len(payload), "ranges": ranges, "stdoutTail": proc.stdout.strip().splitlines()[-3:]}

    return _with_range_server(payload, run)


def _macos_installer_resume_simulation(root: Path) -> dict[str, Any]:
    script_path = root / "deploy" / "ecorex-site" / "install-webui.sh"
    text = script_path.read_text(encoding="utf-8")
    end = text.index("need_cmd curl")
    functions = text[:end]
    assert_true('curl_args+=("-C" "-")' in functions, "macOS installer missing curl resume args")
    payload = (b"EcoreX-macOS-WebUI-resume-test-" * 4096) + bytes(range(255, -1, -1)) * 1024
    expected_sha = hashlib.sha256(payload).hexdigest().upper()

    def run(url: str, requests_seen: list[dict[str, Any]]) -> dict[str, Any]:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            wsl_url = _wsl_url_for_windows_localhost(url)
            resume_cache = tmp_path / "resume.zip"
            stale_cache = tmp_path / "stale.zip"
            (Path(str(resume_cache) + ".part")).write_bytes(payload[: len(payload) // 4])
            (Path(str(stale_cache) + ".part")).write_bytes(b"Y" * len(payload))
            harness = tmp_path / "download-resume-test.sh"
            harness.write_text(
                "\n".join([
                    functions,
                    f"uri='{wsl_url}'",
                    f"sha='{expected_sha}'",
                    f"resume='{_wsl_path(resume_cache)}'",
                    f"stale='{_wsl_path(stale_cache)}'",
                    "download_file \"$uri\" \"$resume\" \"$sha\"",
                    "[[ \"$(sha256_file \"$resume\")\" == \"$sha\" ]] || { echo 'resume hash mismatch' >&2; exit 41; }",
                    "download_file \"$uri\" \"$stale\" \"$sha\"",
                    "[[ \"$(sha256_file \"$stale\")\" == \"$sha\" ]] || { echo 'stale hash mismatch' >&2; exit 42; }",
                    "echo OK",
                ]),
                encoding="utf-8",
                newline="\n",
            )
            proc = subprocess.run(
                ["bash", _wsl_path(harness)],
                cwd=root,
                text=True,
                encoding="utf-8",
                errors="replace",
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=180,
                check=False,
            )
            if proc.returncode != 0:
                raise MatrixFailure(f"macOS Bash resume simulation failed: {proc.stdout}\n{proc.stderr}")
            ranges = [request.get("range", "") for request in requests_seen if request.get("range")]
            assert_true(any(r == f"bytes={len(payload) // 4}-" for r in ranges), f"missing curl resume range in {ranges}")
            assert_true(any(r == f"bytes={len(payload)}-" for r in ranges), f"missing curl 416 range in {ranges}")
            return {"script": str(script_path), "payloadBytes": len(payload), "ranges": ranges, "wslUrlHost": wsl_url.split("/")[2], "stdoutTail": proc.stdout.strip().splitlines()[-5:]}

    return _with_range_server(payload, run)


def _make_executor():
    from agent.protocol.agent_stream import AgentStreamExecutor

    return AgentStreamExecutor(
        agent=SimpleNamespace(last_usage={}),
        model=SimpleNamespace(),
        system_prompt="",
        tools=[],
    )


def _feishu_distinct_batch() -> dict[str, Any]:
    executor = _make_executor()
    for idx in range(9):
        args = {
            "action": "run",
            "args": [
                "im", "+chat-messages-list", "--as", "user", "--chat-id", f"oc_task_{idx}",
                "--start", "2026-06-23T00:00:00+08:00", "--end", "2026-06-23T20:35:00+08:00",
                "--sort", "asc",
            ],
        }
        should_stop, reason = executor._check_tool_chain_budget("feishu_cli", args)
        assert_true(not should_stop, f"distinct chat {idx} blocked: {reason}")
        executor._record_tool_result("feishu_cli", args, True)
    return {"distinctChats": 9}


def _feishu_same_chat_blocked() -> dict[str, Any]:
    executor = _make_executor()
    args = {
        "action": "run",
        "args": [
            "im", "+chat-messages-list", "--as", "user", "--chat-id", "oc_same",
            "--start", "2026-06-23T00:00:00+08:00", "--end", "2026-06-23T20:35:00+08:00",
        ],
    }
    for _ in range(6):
        executor._record_tool_result("feishu_cli", args, True)
    should_stop, reason = executor._check_tool_chain_budget("feishu_cli", args)
    assert_true(should_stop, "same chat repeat was not blocked")
    return {"blocked": should_stop, "reason": reason}


def _tool_schema_database_boundary() -> dict[str, Any]:
    executor = _make_executor()
    groups = executor._tool_schema_intent_groups("explain this database note")
    assert_true("feishu" not in groups, f"database unexpectedly selected feishu: {groups}")
    feishu_groups = executor._tool_schema_intent_groups("read this Feishu base table")
    assert_true("feishu" in feishu_groups, f"Feishu base did not select feishu: {feishu_groups}")
    return {"databaseGroups": sorted(groups), "feishuGroups": sorted(feishu_groups)}


def _tool_schema_explicit_feishu() -> dict[str, Any]:
    from agent.protocol.agent_stream import AgentStreamExecutor

    tools = [
        SimpleNamespace(name="read", description="read tool", params={"type": "object", "properties": {}}),
        SimpleNamespace(name="feishu_cli", description="feishu tool", params={"type": "object", "properties": {}}),
        SimpleNamespace(name="mcp__server__tool", description="mcp tool", params={"type": "object", "properties": {}}),
    ]
    executor = AgentStreamExecutor(
        agent=SimpleNamespace(last_usage={}),
        model=SimpleNamespace(),
        system_prompt="",
        tools=tools,
        messages=[{"role": "user", "content": [{"type": "text", "text": "please use feishu_cli status"}]}],
    )
    selected, budget = executor._select_tools_for_schema()
    assert_true("feishu_cli" in selected, "explicit feishu_cli was not selected")
    return {"selected": sorted(selected), "reason": budget["selection_reasons"].get("feishu_cli")}


def _tool_schema_fallback_avoids_feishu() -> dict[str, Any]:
    from agent.protocol.agent_stream import AgentStreamExecutor

    tools = [
        SimpleNamespace(name="feishu_cli", description="feishu tool", params={"type": "object", "properties": {}}),
        SimpleNamespace(name="mcp__server__tool", description="mcp tool", params={"type": "object", "properties": {}}),
    ]
    executor = AgentStreamExecutor(
        agent=SimpleNamespace(last_usage={}),
        model=SimpleNamespace(),
        system_prompt="",
        tools=tools,
        messages=[{"role": "user", "content": [{"type": "text", "text": "plain unrelated request"}]}],
    )
    selected, _budget = executor._select_tools_for_schema()
    assert_true("feishu_cli" not in selected, "fallback selected feishu_cli")
    assert_true("mcp__server__tool" in selected, "fallback did not select MCP tool")
    return {"selected": sorted(selected)}


def _executor_final_response_appended() -> dict[str, Any]:
    executor = _make_executor()
    executor.messages = [
        {"role": "user", "content": [{"type": "text", "text": "summarize Feishu"}]},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "call_1", "name": "feishu_cli", "input": {}}]},
    ]
    executor._ensure_final_response_message("Final answer persisted.")
    last = executor.messages[-1]
    assert_true(last.get("role") == "assistant", "last message is not assistant")
    assert_true((last.get("content") or [{}])[0].get("type") == "text", "last assistant is not text")
    return {"messages": len(executor.messages), "text": (last.get("content") or [{}])[0].get("text")}


def _executor_run_stream_final_response_visible() -> dict[str, Any]:
    from agent.protocol.agent_stream import AgentStreamExecutor
    from agent.tools.base_tool import ToolResult

    final_text = "最终结论：已读取飞书群聊消息，并整理出明确结果。"

    class FakeAgent:
        last_usage: dict[str, Any] = {}
        memory_manager = None
        max_context_tokens = 10000

        @staticmethod
        def _estimate_message_tokens(_message):
            return 1

        @staticmethod
        def _get_model_context_window():
            return 10000

        @staticmethod
        def _get_context_reserve_tokens():
            return 1000

    class FakeModel:
        model = "matrix-feishu-finalizer"

        def __init__(self):
            self.requests = []

        def call_stream(self, request):
            self.requests.append(request)
            if len(self.requests) == 1:
                yield {
                    "choices": [{
                        "delta": {
                            "tool_calls": [{
                                "index": 0,
                                "id": "call_matrix_feishu",
                                "function": {
                                    "name": "feishu_cli",
                                    "arguments": json.dumps({
                                        "action": "run",
                                        "args": [
                                            "im", "+chat-messages-list",
                                            "--chat-id", "oc_matrix",
                                            "--start", "2026-06-23T00:00:00+08:00",
                                            "--end", "2026-06-23T20:35:00+08:00",
                                        ],
                                    }),
                                },
                            }]
                        },
                        "finish_reason": "tool_calls",
                    }]
                }
                return
            yield {"choices": [{"delta": {"content": final_text}, "finish_reason": "stop"}]}

    class FakeFeishuTool:
        name = "feishu_cli"
        description = "structured Feishu CLI"
        params = {"type": "object", "properties": {}}

        def execute_tool(self, _args):
            return ToolResult.success({"status": "success", "output": "群聊 oc_matrix 有 1 条有效消息。"})

    events: list[dict[str, Any]] = []
    executor = AgentStreamExecutor(
        agent=FakeAgent(),
        model=FakeModel(),
        system_prompt="",
        tools=[FakeFeishuTool()],
        max_turns=5,
        on_event=lambda event: events.append(event),
    )
    executor._authorize_tool_execution = lambda *_args, **_kwargs: {"allowed": True}
    final_response = executor.run_stream("提取飞书群聊消息并给出最终结论")
    assert_true(final_response == final_text, f"final response mismatch: {final_response}")
    assert_true(events and events[-1]["type"] == "agent_end", "last event is not agent_end")
    assert_true(events[-1]["data"].get("final_response") == final_text, "agent_end missing final response")
    last = executor.messages[-1]
    assert_true(last.get("role") == "assistant", "last message is not assistant")
    content = last.get("content") or []
    assert_true(any(block.get("type") == "text" and block.get("text") == final_text for block in content), "last assistant text missing final response")
    assert_true(not any(block.get("type") == "tool_use" for block in content), "last assistant still contains tool_use")
    return {
        "turnRequests": len(executor.model.requests),
        "events": [event["type"] for event in events],
        "finalText": final_text,
        "messageCount": len(executor.messages),
    }


def _bridge_final_response_synthesized() -> dict[str, Any]:
    from bridge.agent_bridge import _ensure_final_response_in_messages

    agent = SimpleNamespace(messages=[], messages_lock=__import__("threading").RLock())
    messages = [{"role": "assistant", "content": [{"type": "tool_use", "id": "call_1", "name": "feishu_cli", "input": {}}]}]
    _ensure_final_response_in_messages(agent, messages, "Final answer from bridge.")
    assert_true(messages[-1]["content"][0]["type"] == "text", "bridge did not append text")
    assert_true(agent.messages[-1]["content"][0]["text"] == "Final answer from bridge.", "agent memory not mirrored")
    return {"messages": len(messages), "agentMessages": len(agent.messages)}


def _bridge_cancelled_not_synthesized() -> dict[str, Any]:
    from bridge.agent_bridge import _ensure_final_response_in_messages

    agent = SimpleNamespace(messages=[], messages_lock=__import__("threading").RLock())
    messages = [{"role": "assistant", "content": [{"type": "text", "text": "_(Cancelled by user)_"}]}]
    _ensure_final_response_in_messages(agent, messages, "_(Cancelled)_")
    assert_true(len(messages) == 1, "cancelled response synthesized a duplicate")
    assert_true(not agent.messages, "cancelled response mirrored to agent memory")
    return {"messages": len(messages), "agentMessages": len(agent.messages)}


def _conversation_store_bot_seq() -> dict[str, Any]:
    from agent.memory.conversation_store import ConversationStore

    with tempfile.TemporaryDirectory() as tmp:
        store = ConversationStore(Path(tmp) / "conversation.sqlite3")
        store.append_messages("s", [
            {"role": "user", "content": "question"},
            {"role": "assistant", "content": "final answer"},
            {"role": "assistant", "content": [{"type": "tool_use", "id": "late", "name": "feishu_cli", "input": {}}]},
        ], channel_type="web")
        seqs = store.get_latest_pair_seqs("s")
    assert_true(seqs.get("user_seq") == 0, f"user_seq mismatch: {seqs}")
    assert_true(seqs.get("bot_seq") == 1, f"bot_seq mismatch: {seqs}")
    return {"seqs": seqs}


def run_matrix(root: Path) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    for index, (name, fn) in enumerate(build_tasks(root), start=1):
        started = time.perf_counter()
        try:
            evidence = fn()
            status = "pass"
            error = ""
        except Exception as exc:  # noqa: BLE001 - evidence should capture all failures
            evidence = {}
            status = "fail"
            error = f"{type(exc).__name__}: {exc}"
        elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
        item = {
            "id": f"T{index:02d}",
            "name": name,
            "status": status,
            "elapsedMs": elapsed_ms,
            "evidence": evidence,
        }
        if error:
            item["error"] = error
        results.append(item)
        print(f"{item['id']} {status.upper()} {name} ({elapsed_ms}ms)")
        if error:
            print(f"  {error}")

    failures = [item for item in results if item["status"] != "pass"]
    return {
        "status": "pass" if not failures else "fail",
        "version": VERSION,
        "taskCount": len(results),
        "passed": len(results) - len(failures),
        "failed": len(failures),
        "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "tasks": results,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--output", default="docs/v0.2.0/real-task-matrix.json")
    args = parser.parse_args()

    root = Path(args.root).resolve()
    payload = run_matrix(root)
    output = (root / args.output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Wrote {output}")
    return 0 if payload["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
