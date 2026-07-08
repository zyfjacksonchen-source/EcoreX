#!/usr/bin/env python3
"""Deploy EcoreX to the configured production server.

The script reads the local operator server file at runtime, never writes raw
host/user/password/URL/command output to evidence, and writes only redacted
deployment evidence under the matching docs/v*/artifacts directory.
"""

from __future__ import annotations

import hashlib
import json
import os
import posixpath
import re
import shlex
import time
import argparse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import paramiko


ROOT = Path.cwd()
SERVER_FILE = Path(r"C:\Users\user\Desktop\企业服务器地址.txt")
VERSION = os.environ.get("ECOREX_DEPLOY_VERSION", "0.2.4")
SERVICE_NAME = "ecorex-web"
WEB_PORT = "9909"

ARTIFACT_DIR = ROOT / "docs" / f"v{VERSION}" / "artifacts"
OUTPUT = ARTIFACT_DIR / "production-deploy-online.json"
WEB_TAR = ROOT / "release-artifacts" / f"EcoreX_{VERSION}-web-linux-service.tar.gz"
PUBLIC_ZIP = ROOT / "release-artifacts" / f"EcoreX_{VERSION}-public-release.zip"
WEBUI_WINDOWS = ROOT / "release-artifacts" / f"EcoreX_{VERSION}-webui-windows-x64.zip"
WEBUI_MACOS = ROOT / "release-artifacts" / f"EcoreX_{VERSION}-webui-macos-universal.zip"
LOCAL_MANIFEST = ROOT / "deploy" / "ecorex-site" / "manifest.json"
DOWNLOAD_CHUNKS_ROOT = ROOT / "release-artifacts" / "download-chunks"
INSTALL_WEB = ROOT / "scripts" / "install-ecorex-web.sh"
CHECK_WEB = ROOT / "scripts" / "check-ecorex-web-release.sh"
INSTALL_PUBLIC = ROOT / "scripts" / "install-ecorex-public-release.sh"
CHECK_SERVER = ROOT / "scripts" / "check-ecorex-server-release.sh"


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest().upper()


def file_sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on"}


def read_server_file() -> tuple[str, str, str, str]:
    try:
        text = SERVER_FILE.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        text = SERVER_FILE.read_text(encoding="gb18030", errors="replace")

    host = ""
    for pattern in (r"https?://(\d{1,3}(?:\.\d{1,3}){3})(?::\d+)?", r"\b(\d{1,3}(?:\.\d{1,3}){3})\b"):
        match = re.search(pattern, text)
        if match:
            host = match.group(1)
            break
    if not host:
        raise SystemExit("could not parse SSH host from server file")

    domain_match = re.search(r"域名\s*[:：]\s*([^\s]+)", text)
    domain = domain_match.group(1).strip().strip("/") if domain_match else "mvdcm.ecoremedia.net"

    user = "root"
    ssh_section = text
    ssh_match = re.search(r"ssh登录\s*[:：]\s*([^\r\n]+)(.*)", text, flags=re.IGNORECASE | re.S)
    if ssh_match:
        user = ssh_match.group(1).strip() or user
        ssh_section = ssh_match.group(2)

    password = ""
    for match in re.finditer(r"密码\s*[:：]\s*([^\r\n]+)", ssh_section):
        password = match.group(1).strip()
    if not password:
        passwords = re.findall(r"密码\s*[:：]\s*([^\r\n]+)", text)
        if passwords:
            password = passwords[-1].strip()
    if not password:
        raise SystemExit("could not parse SSH password from server file")
    return host, domain, user, password


class ProductionDeploy:
    def __init__(self, *, promote_public_release: bool | None = None) -> None:
        ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
        for path in (WEB_TAR, PUBLIC_ZIP, WEBUI_WINDOWS, WEBUI_MACOS, INSTALL_WEB, CHECK_WEB, INSTALL_PUBLIC, CHECK_SERVER):
            if not path.exists():
                raise SystemExit(f"missing required file: {path}")

        self.web_sha = file_sha(WEB_TAR)
        self.public_sha = file_sha(PUBLIC_ZIP)
        self.download_artifacts = (WEB_TAR, WEBUI_WINDOWS, WEBUI_MACOS)
        self.download_artifact_meta = {
            path.name: {"artifactId": path.name, "size": path.stat().st_size, "sha256": file_sha(path)}
            for path in self.download_artifacts
        }
        self.host, self.domain, self.user, self.password = read_server_file()
        self.public_base_url = f"https://{self.domain}"
        self.public_site_url = f"{self.public_base_url}/ecorex-agent"
        self.promote_public_release = (
            truthy(os.environ.get("ECOREX_PROMOTE_PUBLIC_RELEASE"))
            if promote_public_release is None
            else bool(promote_public_release)
        )
        self.skip_webui_download_upload = (
            truthy(os.environ.get("ECOREX_SKIP_WEBUI_DOWNLOAD_UPLOAD"))
            or self.manifest_webui_downloads_externalized()
        )
        self.remote_dir = f"/srv/ecorex-agent-download/upload-staging/ecorex-v{VERSION.replace('.', '')}-release-{int(time.time())}"
        self.commands: list[dict[str, Any]] = []

    def manifest_webui_downloads_externalized(self) -> bool:
        if not LOCAL_MANIFEST.is_file():
            return False
        try:
            payload = json.loads(LOCAL_MANIFEST.read_text(encoding="utf-8-sig"))
        except Exception:
            return False
        download = payload.get("download") if isinstance(payload.get("download"), dict) else {}
        artifacts = {
            str(item.get("id") or ""): item
            for item in (payload.get("artifacts") or [])
            if isinstance(item, dict)
        }
        required = ("webui-windows-x64", "webui-macos-universal")
        return (
            bool(payload.get("downloadsExternalized"))
            and bool(download.get("mirrors"))
            and all(artifact_id in artifacts for artifact_id in required)
        )

    def secret_hash(self, value: str) -> str:
        return sha256_text(value)[:16]

    def redact(self, text: str) -> str:
        out = text or ""
        for value in (self.host, self.domain, self.user, self.password, self.public_base_url, self.public_site_url):
            if value:
                out = out.replace(value, "[REDACTED]")
        out = re.sub(r"https?://[^\s\)\]\"']+", "[URL]", out)
        out = re.sub(r"\b\d{1,3}(?:\.\d{1,3}){3}\b", "[IP]", out)
        out = re.sub(r"(?i)(password|secret|token|key)(\s*[=:]\s*)[^\s\n]+", r"\1\2[REDACTED]", out)
        out = re.sub(r"/tmp/ecorex-v[0-9]+-release-[0-9]+", "/tmp/[RUN_DIR]", out)
        out = re.sub(r"/srv/ecorex-agent-download/upload-staging/ecorex-v[0-9]+-release-[0-9]+", "/srv/ecorex-agent-download/upload-staging/[RUN_DIR]", out)
        return out[:3000]

    def record(self, name: str, semantic: str, code: int, stdout: str = "", stderr: str = "") -> None:
        self.commands.append(
            {
                "name": name,
                "argvHash": sha256_text(semantic),
                "exitCode": int(code),
                "stdoutHash": sha256_text(stdout or ""),
                "stderrHash": sha256_text(stderr or ""),
                "stdoutExcerptRedacted": self.redact(stdout),
                "stderrExcerptRedacted": self.redact(stderr),
            }
        )

    def connect_client(self) -> None:
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(
            hostname=self.host,
            username=self.user,
            password=self.password,
            timeout=25,
            banner_timeout=25,
            auth_timeout=25,
            look_for_keys=False,
            allow_agent=False,
        )
        self.client = client

    def reconnect_client(self) -> None:
        try:
            self.client.close()
        except Exception:
            pass
        self.connect_client()

    def remote(self, name: str, command: str, *, timeout: int = 900, check: bool = True) -> tuple[int, str, str]:
        stdin, stdout, stderr = self.client.exec_command(command, timeout=timeout)
        del stdin
        out = stdout.read().decode("utf-8", errors="replace")
        err = stderr.read().decode("utf-8", errors="replace")
        code = stdout.channel.recv_exit_status()
        self.record(name, f"{name}\n{command}", code, out, err)
        if check and code != 0:
            raise RuntimeError(f"{name} failed with exit code {code}: {self.redact(out + err)}")
        return code, out, err

    @staticmethod
    def remote_file_size(sftp: paramiko.SFTPClient, remote: str) -> int:
        try:
            return int(sftp.stat(remote).st_size)
        except FileNotFoundError:
            return 0
        except OSError:
            return 0

    def upload_once(self, local: Path, remote: str) -> int:
        local_size = local.stat().st_size
        sftp = self.client.open_sftp()
        try:
            uploaded = self.remote_file_size(sftp, remote)
            if uploaded > local_size:
                sftp.remove(remote)
                uploaded = 0
            mode = "ab" if uploaded else "wb"
            with local.open("rb") as source:
                if uploaded:
                    source.seek(uploaded)
                with sftp.open(remote, mode) as target:
                    for chunk in iter(lambda: source.read(1024 * 1024), b""):
                        target.write(chunk)
            final_size = self.remote_file_size(sftp, remote)
        finally:
            sftp.close()
        if final_size != local_size:
            raise IOError(f"uploaded size mismatch for {local.name}: {final_size}/{local_size}")
        return final_size

    def upload(self, local: Path, remote: str, name: str) -> None:
        attempts = 4
        last_error = ""
        for attempt in range(1, attempts + 1):
            try:
                final_size = self.upload_once(local, remote)
                self.record(name, f"sftp:{local.name}:{remote}:{file_sha(local)}:{final_size}:attempt={attempt}", 0)
                return
            except Exception as exc:
                last_error = exc.__class__.__name__
                self.record(
                    f"{name}_retry_{attempt}",
                    f"sftp-retry:{local.name}:{remote}:attempt={attempt}:{last_error}",
                    1,
                    "",
                    last_error,
                )
                if attempt >= attempts:
                    raise
                time.sleep(min(20, 3 * attempt))
                self.reconnect_client()

    @staticmethod
    def cache_probe_command(file_name: str, expected_sha: str, expected_size: int, cache_path: str, target_path: str) -> str:
        script = f"""
set -euo pipefail
name={shlex.quote(file_name)}
expected_sha={shlex.quote(expected_sha)}
expected_size={shlex.quote(str(expected_size))}
cache={shlex.quote(cache_path)}
target={shlex.quote(target_path)}
mkdir -p "$(dirname "$cache")" "$(dirname "$target")"

sha_upper() {{
  sha256sum "$1" | awk '{{print toupper($1)}}'
}}

size_of() {{
  wc -c < "$1" | tr -d ' '
}}

is_match() {{
  local item="$1"
  [[ -f "$item" ]] || return 1
  [[ "$(size_of "$item")" == "$expected_size" ]] || return 1
  [[ "$(sha_upper "$item")" == "$expected_sha" ]] || return 1
}}

publish_match() {{
  local source="$1"
  cp -p "$source" "$cache"
  cp -p "$cache" "$target"
}}

if is_match "$cache"; then
  cp -p "$cache" "$target"
  echo "cache-hit"
  exit 0
fi

if is_match "$target"; then
  publish_match "$target"
  echo "target-hit"
  exit 0
fi

candidate="/srv/ecorex-agent-download/current/downloads/$name"
if is_match "$candidate"; then
  publish_match "$candidate"
  echo "current-hit"
  exit 0
fi

while IFS= read -r candidate; do
  if is_match "$candidate"; then
    publish_match "$candidate"
    echo "adopted-staging"
    exit 0
  fi
done < <(find /srv/ecorex-agent-download/upload-staging -type f -name "$name" 2>/dev/null | sort -r)

echo "cache-miss"
exit 17
"""
        return "bash -lc " + shlex.quote(script)

    @staticmethod
    def cache_promote_command(part_path: str, cache_path: str, target_path: str, expected_sha: str, expected_size: int) -> str:
        script = f"""
set -euo pipefail
part={shlex.quote(part_path)}
cache={shlex.quote(cache_path)}
target={shlex.quote(target_path)}
expected_sha={shlex.quote(expected_sha)}
expected_size={shlex.quote(str(expected_size))}
mkdir -p "$(dirname "$cache")" "$(dirname "$target")"
actual_size="$(wc -c < "$part" | tr -d ' ')"
actual_sha="$(sha256sum "$part" | awk '{{print toupper($1)}}')"
if [[ "$actual_size" != "$expected_size" || "$actual_sha" != "$expected_sha" ]]; then
  echo "cache upload verification failed"
  exit 1
fi
mv -f "$part" "$cache"
cp -p "$cache" "$target"
echo "uploaded-cache-hit"
"""
        return "bash -lc " + shlex.quote(script)

    def ensure_download_source(self, local: Path, target_remote: str, cache_remote: str, name: str) -> None:
        meta = self.download_artifact_meta[local.name]
        expected_sha = str(meta["sha256"])
        expected_size = int(meta["size"])
        probe = self.cache_probe_command(local.name, expected_sha, expected_size, cache_remote, target_remote)
        code, out, err = self.remote(f"{name}_cache_probe", probe, timeout=900, check=False)
        if code == 0:
            return
        if code != 17:
            raise RuntimeError(f"{name}_cache_probe failed with exit code {code}: {self.redact(out + err)}")

        part_remote = f"{cache_remote}.part-{int(time.time())}"
        self.upload(local, part_remote, f"{name}_cache_upload")
        promote = self.cache_promote_command(part_remote, cache_remote, target_remote, expected_sha, expected_size)
        self.remote(f"{name}_cache_promote", promote, timeout=900)

    def ensure_download_chunks(self, local: Path, remote_downloads_source: str, name: str) -> None:
        chunk_dir = DOWNLOAD_CHUNKS_ROOT / local.name
        if not chunk_dir.is_dir():
            self.record(f"{name}_chunks_skipped", f"download-chunks-missing:{local.name}", 0)
            return
        remote_chunk_dir = posixpath.join(remote_downloads_source, "chunks", local.name)
        self.remote(f"{name}_chunks_prepare", f"mkdir -p {shlex.quote(remote_chunk_dir)}", timeout=60)
        for chunk in sorted(chunk_dir.glob("*.part")):
            remote_chunk = posixpath.join(remote_chunk_dir, chunk.name)
            expected_size = chunk.stat().st_size
            expected_sha = file_sha(chunk)
            probe = self.cache_probe_command(chunk.name, expected_sha, expected_size, remote_chunk, remote_chunk)
            code, out, err = self.remote(f"{name}_chunk_{chunk.stem}_probe", probe, timeout=300, check=False)
            if code == 0:
                continue
            if code != 17:
                raise RuntimeError(f"{name}_chunk_probe failed with exit code {code}: {self.redact(out + err)}")
            self.upload(chunk, remote_chunk, f"{name}_chunk_{chunk.stem}_upload")

    @staticmethod
    def public_chunk_generation_command() -> str:
        script = r"""
set -euo pipefail
python3 - <<'PY'
import hashlib
import json
import pathlib

root = pathlib.Path('/srv/ecorex-agent-download/current')
manifest_path = root / 'manifest.json'
manifest = json.loads(manifest_path.read_text(encoding='utf-8-sig'))

def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest().upper()

def sha256_path(path):
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest().upper()

generated = []
for artifact in manifest.get('artifacts') or []:
    chunked = artifact.get('chunked') or {}
    chunks = chunked.get('chunks') or []
    if not chunks:
        continue
    file_name = artifact.get('fileName') or ''
    source = root / 'downloads' / file_name
    if not source.is_file():
        raise SystemExit(f'missing chunk source artifact: {source}')
    if source.stat().st_size != int(artifact.get('size') or 0):
        raise SystemExit(f'chunk source size mismatch: {file_name}')
    if sha256_path(source) != str(artifact.get('sha256') or '').upper():
        raise SystemExit(f'chunk source sha256 mismatch: {file_name}')
    base_href = str(chunked.get('baseHref') or '').strip('/')
    if not base_href or '..' in pathlib.PurePosixPath(base_href).parts:
        raise SystemExit(f'unsafe chunk baseHref: {base_href}')
    out_dir = root / 'downloads' / base_href
    out_dir.mkdir(parents=True, exist_ok=True)
    with source.open('rb') as handle:
        for chunk in chunks:
            data = handle.read(int(chunk.get('size') or 0))
            expected_sha = str(chunk.get('sha256') or '').upper()
            if len(data) != int(chunk.get('size') or 0) or sha256_bytes(data) != expected_sha:
                raise SystemExit(f'chunk metadata mismatch: {file_name} #{chunk.get("index")}')
            target = out_dir / str(chunk.get('fileName') or '')
            if target.is_file() and target.stat().st_size == len(data) and sha256_path(target) == expected_sha:
                continue
            tmp = target.with_suffix(target.suffix + '.tmp')
            tmp.write_bytes(data)
            tmp.replace(target)
    generated.append({'artifact': file_name, 'chunks': len(chunks), 'baseHref': base_href})

print(json.dumps({'ok': True, 'generated': generated}, ensure_ascii=False, sort_keys=True))
PY
"""
        return "bash -lc " + shlex.quote(script)

    @staticmethod
    def state_script() -> str:
        return r"""
python3 - <<'PY'
import json, os, pathlib, subprocess, urllib.error, urllib.request
expected_version = '__EXPECTED_VERSION__'

def read_json(path):
    try:
        return json.loads(pathlib.Path(path).read_text(encoding='utf-8-sig'))
    except Exception:
        return {}

def run(args):
    try:
        return subprocess.run(args, text=True, capture_output=True, timeout=20).stdout.strip()
    except Exception:
        return ''

current = pathlib.Path('/opt/ecorex-web/current')
public_root = pathlib.Path('/srv/ecorex-agent-download')
release = read_json(current / 'release.json')
installations = read_json('/srv/ecorex-agent-workspace/.ecorex/installations.json')
manifest = read_json('/srv/ecorex-agent-download/current/manifest.json')
staged_manifest = read_json(public_root / f'staged-v{expected_version}' / 'manifest.json')
web_status = None
version_body = ''
try:
    with urllib.request.urlopen('http://127.0.0.1:9909/api/version', timeout=10) as resp:
        web_status = resp.status
        version_body = resp.read(4096).decode('utf-8', errors='replace')
except urllib.error.HTTPError as exc:
    web_status = exc.code
except Exception:
    web_status = None
surface = (installations.get('surfaces') or {}).get('webui-linux-service') or {}
print(json.dumps({
    'currentVersion': release.get('version') or '',
    'currentArtifact': release.get('artifactId') or '',
    'installationManifestVersion': surface.get('version') or '',
    'publicManifestVersion': manifest.get('version') or '',
    'stagedPublicManifestVersion': staged_manifest.get('version') or '',
    'serviceActive': run(['systemctl', 'is-active', 'ecorex-web']) == 'active',
    'serviceEnabled': run(['systemctl', 'is-enabled', 'ecorex-web']) == 'enabled',
    'webVersionStatus': web_status,
    'webVersionBodyHasExpectedVersion': expected_version in version_body,
}, sort_keys=True))
PY
""".replace("__EXPECTED_VERSION__", VERSION)

    def run(self) -> dict[str, Any]:
        self.connect_client()
        try:
            remote_web_tar = posixpath.join(self.remote_dir, WEB_TAR.name)
            remote_public_zip = posixpath.join(self.remote_dir, PUBLIC_ZIP.name)
            remote_downloads_source = posixpath.join(self.remote_dir, "downloads-source")
            remote_tmp_dir = posixpath.join(self.remote_dir, "tmp")
            remote_cache_dir = "/srv/ecorex-agent-download/downloads-cache/sha256"
            remote_install_web = posixpath.join(self.remote_dir, INSTALL_WEB.name)
            remote_check_web = posixpath.join(self.remote_dir, CHECK_WEB.name)
            remote_install_public = posixpath.join(self.remote_dir, INSTALL_PUBLIC.name)
            remote_check_server = posixpath.join(self.remote_dir, CHECK_SERVER.name)

            self.remote(
                "prepare_remote_dir",
                f"mkdir -p {shlex.quote(self.remote_dir)} {shlex.quote(remote_downloads_source)} {shlex.quote(remote_tmp_dir)} {shlex.quote(remote_cache_dir)} "
                f"&& chmod 700 {shlex.quote(remote_tmp_dir)}",
                timeout=60,
            )
            for local, remote, name in (
                (WEB_TAR, remote_web_tar, "upload_web_tar"),
                (PUBLIC_ZIP, remote_public_zip, "upload_public_zip"),
                (INSTALL_WEB, remote_install_web, "upload_install_web"),
                (CHECK_WEB, remote_check_web, "upload_check_web"),
                (INSTALL_PUBLIC, remote_install_public, "upload_install_public"),
                (CHECK_SERVER, remote_check_server, "upload_check_server"),
            ):
                self.upload(local, remote, name)
            if self.skip_webui_download_upload:
                for local, name in (
                    (WEBUI_WINDOWS, "webui_windows_download"),
                    (WEBUI_MACOS, "webui_macos_download"),
                ):
                    meta = self.download_artifact_meta[local.name]
                    self.record(
                        f"{name}_externalized_skipped",
                        f"deployment-external-only:{local.name}:{meta['size']}:{meta['sha256']}",
                        0,
                        "webui download is served by manifest mirrors; server-local upload skipped",
                        "",
                    )
            else:
                for local, name in (
                    (WEBUI_WINDOWS, "webui_windows_download"),
                    (WEBUI_MACOS, "webui_macos_download"),
                ):
                    cache_name = f"{self.download_artifact_meta[local.name]['sha256']}-{local.name}"
                    self.ensure_download_source(
                        local,
                        posixpath.join(remote_downloads_source, local.name),
                        posixpath.join(remote_cache_dir, cache_name),
                        name,
                    )
            self.remote(
                "stage_web_tar_download_source",
                f"cp -p {shlex.quote(remote_web_tar)} {shlex.quote(posixpath.join(remote_downloads_source, WEB_TAR.name))}",
                timeout=120,
            )
            self.remote(
                "chmod_release_scripts",
                "chmod +x "
                + " ".join(shlex.quote(item) for item in (remote_install_web, remote_check_web, remote_install_public, remote_check_server)),
                timeout=60,
            )

            _, pre_out, _ = self.remote("capture_pre_state", self.state_script(), timeout=60, check=False)

            install_web = (
                f"TMPDIR={shlex.quote(remote_tmp_dir)} VERSION={shlex.quote(VERSION)} EXPECTED_SHA256={shlex.quote(self.web_sha)} "
                f"PUBLIC_BASE_URL={shlex.quote(self.public_base_url)} WEB_HOST=127.0.0.1 WEB_PORT={WEB_PORT} "
                f"OPEN_BROWSER=0 START_SERVICE=1 INSTALL_PY_DEPS=1 "
                f"bash {shlex.quote(remote_install_web)} {shlex.quote(remote_web_tar)}"
            )
            self.remote("install_web_service", install_web, timeout=1800)

            check_web = (
                f"TMPDIR={shlex.quote(remote_tmp_dir)} VERSION={shlex.quote(VERSION)} BASE_URL=http://127.0.0.1:{WEB_PORT} "
                f"CHECK_HTTP=1 CHECK_SYSTEMD=1 CHECK_INSTALLED=1 "
                f"bash {shlex.quote(remote_check_web)} {shlex.quote(remote_web_tar)}"
            )
            self.remote("check_web_service", check_web, timeout=300)

            install_public = (
                f"TMPDIR={shlex.quote(remote_tmp_dir)} VERSION={shlex.quote(VERSION)} EXPECTED_SHA256={shlex.quote(self.public_sha)} "
                f"DOWNLOADS_SOURCE_DIR={shlex.quote(remote_downloads_source)} "
                f"PROMOTE_PUBLIC_RELEASE={'1' if self.promote_public_release else '0'} "
                f"bash {shlex.quote(remote_install_public)} {shlex.quote(remote_public_zip)}"
            )
            self.remote("install_public_site_admin", install_public, timeout=900)
            self.remote("generate_public_download_chunks", self.public_chunk_generation_command(), timeout=1800)

            self.remote(
                "restart_admin_api_after_public_install",
                "systemctl restart ecorex-admin-api && "
                "for i in $(seq 1 30); do "
                "code=$(curl -sS -o /dev/null -w '%{http_code}' http://127.0.0.1:18084/client/model-config || true); "
                "if [ \"$code\" = \"403\" ] || [ \"$code\" = \"401\" ] || [ \"$code\" = \"200\" ]; then echo admin-api-ready:$code; exit 0; fi; "
                "sleep 1; "
                "done; "
                "systemctl status ecorex-admin-api --no-pager -l; exit 1",
                timeout=90,
            )

            if self.promote_public_release:
                check_server = (
                    f"TMPDIR={shlex.quote(remote_tmp_dir)} VERSION={shlex.quote(VERSION)} PUBLIC_BASE_URL={shlex.quote(self.public_site_url)} "
                    f"CHECK_PUBLIC=1 CHECK_CADDY=0 bash {shlex.quote(remote_check_server)}"
                )
                self.remote("check_public_site_admin", check_server, timeout=600)
            else:
                self.record("check_public_site_admin_skipped", "public release staged without stable promotion", 0)
            _, post_out, _ = self.remote("capture_post_state", self.state_script(), timeout=60)
        finally:
            self.client.close()

        pre_state = json.loads(pre_out.strip().splitlines()[-1]) if pre_out.strip() else {}
        post_state = json.loads(post_out.strip().splitlines()[-1]) if post_out.strip() else {}
        ok = (
            post_state.get("currentVersion") == VERSION
            and post_state.get("installationManifestVersion") == VERSION
            and (
                post_state.get("publicManifestVersion") == VERSION
                if self.promote_public_release
                else post_state.get("stagedPublicManifestVersion") == VERSION
            )
            and post_state.get("serviceActive") is True
        )
        payload = {
            "status": "PASS" if ok else "FAIL",
            "scope": "production-online-web-and-admin",
            "version": VERSION,
            "generatedAt": datetime.now(timezone.utc).isoformat(),
            "artifacts": {
                "webTarball": {"artifactId": WEB_TAR.name, "size": WEB_TAR.stat().st_size, "sha256": self.web_sha},
                "publicZip": {"artifactId": PUBLIC_ZIP.name, "size": PUBLIC_ZIP.stat().st_size, "sha256": self.public_sha},
                "publicDownloadsSource": self.download_artifact_meta,
            },
            "publicReleasePromotion": {
                "adminTriggerRequired": True,
                "promoted": self.promote_public_release,
                "mode": "stable" if self.promote_public_release else "staged",
            },
            "target": {
                "sshHostHash": self.secret_hash(self.host),
                "sshUserHash": self.secret_hash(self.user),
                "domainHash": self.secret_hash(self.domain),
                "rawTargetPersisted": False,
            },
            "preState": pre_state,
            "postState": post_state,
            "onlineChecks": {
                "webServiceVersion": post_state.get("currentVersion"),
                "installationManifestVersion": post_state.get("installationManifestVersion"),
                "publicManifestVersion": post_state.get("publicManifestVersion"),
                "stagedPublicManifestVersion": post_state.get("stagedPublicManifestVersion"),
                "serviceActive": post_state.get("serviceActive"),
                "serviceEnabled": post_state.get("serviceEnabled"),
                "webVersionStatus": post_state.get("webVersionStatus"),
                "webVersionBodyHasExpectedVersion": post_state.get("webVersionBodyHasExpectedVersion"),
            },
            "commands": self.commands,
            "redaction": {
                "rawTargetPersisted": False,
                "rawPasswordPersisted": False,
                "rawSecretPersisted": False,
                "rawUrlPersisted": False,
                "rawOutputPersisted": False,
            },
        }
        OUTPUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return {"status": payload["status"], "artifact": str(OUTPUT), "onlineChecks": payload["onlineChecks"]}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--promote-public-release",
        action="store_true",
        help="Promote the staged public release to the stable /current pointer after upload and checksum validation.",
    )
    args = parser.parse_args()
    print(
        json.dumps(
            ProductionDeploy(promote_public_release=args.promote_public_release).run(),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
