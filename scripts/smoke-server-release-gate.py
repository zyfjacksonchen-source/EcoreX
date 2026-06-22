#!/usr/bin/env python3
"""Smoke-test server release gate rejects weak macOS unsigned evidence."""

from __future__ import annotations

import argparse
import hashlib
import http.server
import json
import os
import pathlib
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from typing import Any


def sha256_file(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def free_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
    finally:
        sock.close()


def to_bash_path(path: pathlib.Path) -> str:
    text = str(path.resolve())
    if sys.platform.startswith("win"):
        drive = text[0].lower()
        rest = text[2:].replace("\\", "/")
        return f"/mnt/{drive}{rest}"
    return text


def write_text(path: pathlib.Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


AUTH_NEGATIVE_STATUSES = {
    "messageNoToken": 401,
    "messageWrongToken": 401,
    "messageQueryTokenRejected": 401,
    "streamNoToken": 401,
    "streamWrongToken": 401,
    "streamQueryTokenRejected": 401,
    "fileStatNoToken": 401,
    "fileStatWrongToken": 401,
    "fileServeNoToken": 401,
    "fileServeWrongToken": 401,
    "openPathNoToken": 401,
    "openPathWrongToken": 401,
}


def create_release_fixture(root: pathlib.Path, version: str) -> tuple[pathlib.Path, pathlib.Path, pathlib.Path]:
    release_root = root / "release"
    admin_root = root / "admin"
    current = release_root / "current"
    downloads = current / "downloads"
    for path in (
        downloads,
        current / "admin",
        release_root / "releases",
        admin_root / "app",
        admin_root / "env",
        admin_root / "server" / "caddy",
    ):
        path.mkdir(parents=True, exist_ok=True)

    write_text(current / "index.html", "ok\n")
    write_text(current / "admin" / "index.html", "admin\n")
    write_text(current / "assets" / "icon.png", "icon\n")
    write_text(current / "assets" / "ecorex-app-preview.png", "preview\n")
    write_text(current / "assets" / "ecorex-ecosystem-hub.png", "hub\n")
    write_text(admin_root / "app" / "ecorex_admin_api.py", "print('ok')\n")
    write_text(admin_root / "env" / "ecorex-admin-api.env", "ok=1\n")
    write_text(admin_root / "server" / "caddy" / "Caddyfile.example", ":8080\n")

    artifact_name = f"EcoreX_{version}_arm64.dmg"
    artifact = downloads / artifact_name
    artifact.write_bytes(b"dummy dmg\n")
    digest = sha256_file(artifact)
    size = artifact.stat().st_size

    public_manifest = {
        "product": "EcoreX",
        "version": version,
        "artifacts": [
            {
                "id": "macos-arm64-dmg",
                "status": "ready-unsigned",
                "signature": "unsigned",
                "fileName": artifact_name,
                "href": f"downloads/{artifact_name}",
                "size": size,
                "sha256": digest,
                "installSmoke": {
                    "status": "pass",
                    "version": version,
                    "sha256": digest,
                    "artifact": artifact_name,
                    "arch": "arm64",
                    "bytes": size,
                    "runId": "synthetic-negative",
                    "mounted": True,
                    "appFound": True,
                    "copied": True,
                    "launched": True,
                    "versionOk": True,
                    "sidecarReady": True,
                    "authReady": True,
                    "authRequired": True,
                    "authNegativeReady": True,
                    "gatekeeperInstructionShown": True,
                    "gatekeeperInstructions": "Open System Settings > Privacy & Security and allow EcoreX.",
                    # Intentionally omitted from the public manifest:
                    # authNegativeStatuses
                },
            }
        ],
    }
    local_manifest = json.loads(json.dumps(public_manifest))
    local_manifest["artifacts"][0]["installSmoke"]["authNegativeStatuses"] = dict(AUTH_NEGATIVE_STATUSES)
    # Public HTTP serves the weak manifest, while the local release root keeps a
    # strong manifest. This proves the server checker validates the downloaded
    # public manifest instead of accidentally trusting local files.
    (current / "manifest.json").write_text(json.dumps(public_manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    local_current = release_root / "local-current"
    shutil.copytree(current, local_current)
    (local_current / "manifest.json").write_text(json.dumps(local_manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    shutil.rmtree(current)
    shutil.move(str(local_current), str(current))
    http_current = release_root / "public-current"
    shutil.copytree(current, http_current)
    (http_current / "manifest.json").write_text(json.dumps(public_manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return release_root, admin_root, http_current


def write_public_server_script(path: pathlib.Path, root: str, port: int) -> None:
    path.write_text(
        f"""
import http.server
import pathlib

root = pathlib.Path({root!r})

class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(root), **kwargs)

    def _special(self):
        if self.path.rstrip('/') == '/admin':
            self.send_response(401)
            self.end_headers()
            return True
        if self.path == '/client/model-config':
            self.send_response(403)
            self.end_headers()
            return True
        return False

    def do_GET(self):
        if not self._special():
            super().do_GET()

    def do_HEAD(self):
        if not self._special():
            super().do_HEAD()

    def log_message(self, format, *args):
        return

http.server.ThreadingHTTPServer(('127.0.0.1', {port}), Handler).serve_forever()
""".lstrip(),
        encoding="utf-8",
    )


def run_server_check(repo: pathlib.Path, release_root: pathlib.Path, admin_root: pathlib.Path, port: int, version: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.update({
        "VERSION": version,
        "CHECK_CADDY": "0",
        "CHECK_PUBLIC": "1",
        "PUBLIC_BASE_URL": f"http://127.0.0.1:{port}",
    })

    if sys.platform.startswith("win"):
        command = (
            f"cd {to_bash_path(repo)} && "
            f"VERSION={version} "
            f"RELEASE_ROOT='{to_bash_path(release_root)}' "
            f"ADMIN_ROOT='{to_bash_path(admin_root)}' "
            f"CHECK_CADDY=0 CHECK_PUBLIC=1 "
            f"PUBLIC_BASE_URL='http://127.0.0.1:{port}' "
            "./scripts/check-ecorex-server-release.sh"
        )
        return subprocess.run(
            ["bash", "-lc", command],
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=60,
        )

    env["RELEASE_ROOT"] = str(release_root)
    env["ADMIN_ROOT"] = str(admin_root)
    return subprocess.run(
        ["bash", str(repo / "scripts" / "check-ecorex-server-release.sh")],
        cwd=str(repo),
        env=env,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=60,
    )


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=".")
    parser.add_argument("--version", default="0.1.19")
    parser.add_argument("--output", default="")
    args = parser.parse_args(argv)

    repo = pathlib.Path(args.root).resolve()
    if not (repo / "scripts" / "check-ecorex-server-release.sh").is_file() and (repo.parent / "scripts").is_dir():
        repo = repo.parent

    tmp = pathlib.Path(tempfile.mkdtemp(prefix="ecorex-server-gate-", dir=str(repo / "tmp") if (repo / "tmp").is_dir() else None))
    port = free_port()
    httpd: http.server.ThreadingHTTPServer | None = None
    thread: threading.Thread | None = None
    wsl_http: subprocess.Popen[str] | None = None
    checks: list[dict[str, Any]] = []
    try:
        release_root, admin_root, public_current = create_release_fixture(tmp, args.version)

        class Handler(http.server.SimpleHTTPRequestHandler):
            def __init__(self, *handler_args: Any, **handler_kwargs: Any) -> None:
                super().__init__(*handler_args, directory=str(public_current), **handler_kwargs)

            def _special(self) -> bool:
                if self.path.rstrip("/") == "/admin":
                    self.send_response(401)
                    self.end_headers()
                    return True
                if self.path == "/client/model-config":
                    self.send_response(403)
                    self.end_headers()
                    return True
                return False

            def do_GET(self) -> None:
                if not self._special():
                    super().do_GET()

            def do_HEAD(self) -> None:
                if not self._special():
                    super().do_HEAD()

            def log_message(self, format: str, *log_args: Any) -> None:
                return

        if sys.platform.startswith("win"):
            server_script = tmp / "serve-public.py"
            write_public_server_script(server_script, to_bash_path(public_current), port)
            wsl_http = subprocess.Popen(
                [
                    "bash",
                    "-lc",
                    f"python3 '{to_bash_path(server_script)}'",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                text=True,
            )
        else:
            httpd = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
        time.sleep(1)

        result = run_server_check(repo, release_root, admin_root, port, args.version)
        output = (result.stdout or "") + (result.stderr or "")
        failure_count = output.count("installSmoke requires authNegativeStatuses")
        checks.append({
            "name": "server release rejects macOS unsigned smoke without auth-negative matrix",
            "status": "pass" if result.returncode != 0 and failure_count >= 1 else "fail",
            "evidence": {
                "exitCode": result.returncode,
                "authNegativeFailureCount": failure_count,
                "publicManifestValidated": True,
                "matchingLines": [line for line in output.splitlines() if "authNegativeStatuses" in line],
                "failLines": [line for line in output.splitlines() if line.startswith("FAIL ")][:12],
            },
        })
    finally:
        if httpd:
            httpd.shutdown()
            httpd.server_close()
        if thread:
            thread.join(timeout=5)
        if wsl_http and wsl_http.poll() is None:
            wsl_http.terminate()
            try:
                wsl_http.wait(timeout=5)
            except subprocess.TimeoutExpired:
                wsl_http.kill()
        shutil.rmtree(tmp, ignore_errors=True)

    failures = [item for item in checks if item["status"] != "pass"]
    payload = {
        "status": "pass" if not failures else "fail",
        "version": args.version,
        "generatedAt": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "changeIds": ["REL-002"],
        "checks": checks,
    }
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        output_path = pathlib.Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(text, encoding="utf-8")
    print(text, end="")
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
