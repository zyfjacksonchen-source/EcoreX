#!/usr/bin/env python3
"""Deploy EcoreX v0.2.4 to the configured production server.

The script reads the local operator server file at runtime, never writes raw
host/user/password/URL/command output to evidence, and writes only redacted
deployment evidence under docs/v0.2.4/artifacts.
"""

from __future__ import annotations

import hashlib
import json
import posixpath
import re
import shlex
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import paramiko


ROOT = Path.cwd()
SERVER_FILE = Path(r"C:\Users\user\Desktop\企业服务器地址.txt")
VERSION = "0.2.4"
SERVICE_NAME = "ecorex-web"
WEB_PORT = "9909"

ARTIFACT_DIR = ROOT / "docs" / "v0.2.4" / "artifacts"
OUTPUT = ARTIFACT_DIR / "production-deploy-online.json"
WEB_TAR = ROOT / "release-artifacts" / f"EcoreX_{VERSION}-web-linux-service.tar.gz"
PUBLIC_ZIP = ROOT / "release-artifacts" / f"EcoreX_{VERSION}-public-release.zip"
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
    def __init__(self) -> None:
        ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
        for path in (WEB_TAR, PUBLIC_ZIP, INSTALL_WEB, CHECK_WEB, INSTALL_PUBLIC, CHECK_SERVER):
            if not path.exists():
                raise SystemExit(f"missing required file: {path}")

        self.web_sha = file_sha(WEB_TAR)
        self.public_sha = file_sha(PUBLIC_ZIP)
        self.host, self.domain, self.user, self.password = read_server_file()
        self.public_base_url = f"https://{self.domain}"
        self.public_site_url = f"{self.public_base_url}/ecorex-agent"
        self.remote_dir = f"/tmp/ecorex-v024-release-{int(time.time())}"
        self.commands: list[dict[str, Any]] = []

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
        out = re.sub(r"/tmp/ecorex-v024-release-[0-9]+", "/tmp/[RUN_DIR]", out)
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

    def upload(self, local: Path, remote: str, name: str) -> None:
        sftp = self.client.open_sftp()
        try:
            sftp.put(str(local), remote)
        finally:
            sftp.close()
        self.record(name, f"sftp:{local.name}:{remote}:{file_sha(local)}:{local.stat().st_size}", 0)

    @staticmethod
    def state_script() -> str:
        return r"""
python3 - <<'PY'
import json, os, pathlib, subprocess, urllib.error, urllib.request

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
release = read_json(current / 'release.json')
installations = read_json('/srv/ecorex-agent-workspace/.ecorex/installations.json')
manifest = read_json('/srv/ecorex-agent-download/current/manifest.json')
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
    'serviceActive': run(['systemctl', 'is-active', 'ecorex-web']) == 'active',
    'serviceEnabled': run(['systemctl', 'is-enabled', 'ecorex-web']) == 'enabled',
    'webVersionStatus': web_status,
    'webVersionBodyHas024': '"0.2.4"' in version_body or '0.2.4' in version_body,
}, sort_keys=True))
PY
"""

    def run(self) -> dict[str, Any]:
        self.client = paramiko.SSHClient()
        self.client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        self.client.connect(
            hostname=self.host,
            username=self.user,
            password=self.password,
            timeout=25,
            banner_timeout=25,
            auth_timeout=25,
            look_for_keys=False,
            allow_agent=False,
        )
        try:
            remote_web_tar = posixpath.join(self.remote_dir, WEB_TAR.name)
            remote_public_zip = posixpath.join(self.remote_dir, PUBLIC_ZIP.name)
            remote_install_web = posixpath.join(self.remote_dir, INSTALL_WEB.name)
            remote_check_web = posixpath.join(self.remote_dir, CHECK_WEB.name)
            remote_install_public = posixpath.join(self.remote_dir, INSTALL_PUBLIC.name)
            remote_check_server = posixpath.join(self.remote_dir, CHECK_SERVER.name)

            self.remote("prepare_remote_dir", f"mkdir -p {shlex.quote(self.remote_dir)}", timeout=60)
            for local, remote, name in (
                (WEB_TAR, remote_web_tar, "upload_web_tar"),
                (PUBLIC_ZIP, remote_public_zip, "upload_public_zip"),
                (INSTALL_WEB, remote_install_web, "upload_install_web"),
                (CHECK_WEB, remote_check_web, "upload_check_web"),
                (INSTALL_PUBLIC, remote_install_public, "upload_install_public"),
                (CHECK_SERVER, remote_check_server, "upload_check_server"),
            ):
                self.upload(local, remote, name)
            self.remote(
                "chmod_release_scripts",
                "chmod +x "
                + " ".join(shlex.quote(item) for item in (remote_install_web, remote_check_web, remote_install_public, remote_check_server)),
                timeout=60,
            )

            _, pre_out, _ = self.remote("capture_pre_state", self.state_script(), timeout=60, check=False)

            install_web = (
                f"VERSION={shlex.quote(VERSION)} EXPECTED_SHA256={shlex.quote(self.web_sha)} "
                f"PUBLIC_BASE_URL={shlex.quote(self.public_base_url)} WEB_HOST=127.0.0.1 WEB_PORT={WEB_PORT} "
                f"OPEN_BROWSER=0 START_SERVICE=1 INSTALL_PY_DEPS=1 "
                f"bash {shlex.quote(remote_install_web)} {shlex.quote(remote_web_tar)}"
            )
            self.remote("install_web_service", install_web, timeout=1800)

            check_web = (
                f"VERSION={shlex.quote(VERSION)} BASE_URL=http://127.0.0.1:{WEB_PORT} "
                f"CHECK_HTTP=1 CHECK_SYSTEMD=1 CHECK_INSTALLED=1 "
                f"bash {shlex.quote(remote_check_web)} {shlex.quote(remote_web_tar)}"
            )
            self.remote("check_web_service", check_web, timeout=300)

            install_public = (
                f"VERSION={shlex.quote(VERSION)} EXPECTED_SHA256={shlex.quote(self.public_sha)} "
                f"bash {shlex.quote(remote_install_public)} {shlex.quote(remote_public_zip)}"
            )
            self.remote("install_public_site_admin", install_public, timeout=900)

            check_server = (
                f"VERSION={shlex.quote(VERSION)} PUBLIC_BASE_URL={shlex.quote(self.public_site_url)} "
                f"CHECK_PUBLIC=1 CHECK_CADDY=0 bash {shlex.quote(remote_check_server)}"
            )
            self.remote("check_public_site_admin", check_server, timeout=600)
            _, post_out, _ = self.remote("capture_post_state", self.state_script(), timeout=60)
        finally:
            self.client.close()

        pre_state = json.loads(pre_out.strip().splitlines()[-1]) if pre_out.strip() else {}
        post_state = json.loads(post_out.strip().splitlines()[-1]) if post_out.strip() else {}
        ok = (
            post_state.get("currentVersion") == VERSION
            and post_state.get("installationManifestVersion") == VERSION
            and post_state.get("publicManifestVersion") == VERSION
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
                "serviceActive": post_state.get("serviceActive"),
                "serviceEnabled": post_state.get("serviceEnabled"),
                "webVersionStatus": post_state.get("webVersionStatus"),
                "webVersionBodyHas024": post_state.get("webVersionBodyHas024"),
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
    print(json.dumps(ProductionDeploy().run(), ensure_ascii=False, indent=2, sort_keys=True))
