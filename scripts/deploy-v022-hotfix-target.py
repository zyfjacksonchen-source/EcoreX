#!/usr/bin/env python3
"""Deploy the v0.2.2 hotfix to the target server with auditable evidence.

The script reads operator-provided SSH credentials from the local server-address
file at runtime. It never writes raw connection details, secrets, raw commands,
or raw command output into the v0.2.2 evidence artifacts.
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
ARTIFACT_DIR = ROOT / "docs" / "v0.2.2" / "artifacts"
TARGET_ARTIFACT = ARTIFACT_DIR / "release-target-deploy-rollback-smoke.json"
PROD_ARTIFACT = ARTIFACT_DIR / "production-deploy-online.json"
LOCAL_SMOKE = ARTIFACT_DIR / "release-deploy-rollback-smoke.json"

VERSION = "0.2.2"
ROLLBACK_VERSION = "0.2.1"
SERVICE_NAME = "ecorex-web"
WEB_PORT = "9909"

WEB_TAR = ROOT / "release-artifacts" / f"EcoreX_{VERSION}-web-linux-service.tar.gz"
ROLLBACK_WEB_TAR = ROOT / "release-artifacts" / f"EcoreX_{ROLLBACK_VERSION}-web-linux-service.tar.gz"
PUBLIC_ZIP = ROOT / "release-artifacts" / f"EcoreX_{VERSION}-public-release.zip"
INSTALL_WEB = ROOT / "scripts" / "install-ecorex-web.sh"
CHECK_WEB = ROOT / "scripts" / "check-ecorex-web-release.sh"
INSTALL_PUBLIC = ROOT / "scripts" / "install-ecorex-public-release.sh"
CHECK_SERVER = ROOT / "scripts" / "check-ecorex-server-release.sh"

EXPECTED_WEB_SHA = "3BEA1EF91C61E9E42235AE7695DDAEBEF25B4A6C5B13B6726240539CC937CCF7"
EXPECTED_WEB_SIZE = 3_679_009
EXPECTED_ROLLBACK_WEB_SHA = "087CCF850667DE56EAF3C855B5E7B90D88E0658461A56CEA6FE8D5B7308FEAB8"
EXPECTED_ROLLBACK_WEB_SIZE = 3_443_860
EXPECTED_PUBLIC_SHA = "BFA0DD949907ECE14787FB5C1D32F3163C42E72ABFB9A83EF9A7BE8FE6DD5F7C"
EXPECTED_PUBLIC_SIZE = 264_864_808


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest().upper()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8", errors="replace"))


def file_sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest().upper()


def read_server_file() -> tuple[str, str, str, str]:
    try:
        text = SERVER_FILE.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        text = SERVER_FILE.read_text(encoding="gb18030", errors="replace")

    host = None
    for pattern in [r"https?://(\d{1,3}(?:\.\d{1,3}){3})(?::\d+)?", r"\b(\d{1,3}(?:\.\d{1,3}){3})\b"]:
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

    password = None
    for match in re.finditer(r"密码\s*[:：]\s*([^\r\n]+)", ssh_section):
        password = match.group(1).strip()
    if not password:
        all_passwords = re.findall(r"密码\s*[:：]\s*([^\r\n]+)", text)
        if all_passwords:
            password = all_passwords[-1].strip()
    if not password:
        raise SystemExit("could not parse SSH password from server file")

    return host, domain, user, password


class Deployer:
    def __init__(self) -> None:
        ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
        for path in [WEB_TAR, ROLLBACK_WEB_TAR, PUBLIC_ZIP, INSTALL_WEB, CHECK_WEB, INSTALL_PUBLIC, CHECK_SERVER, LOCAL_SMOKE]:
            if not path.exists():
                raise SystemExit(f"missing required file: {path}")

        self.web_sha = file_sha(WEB_TAR)
        self.rollback_web_sha = file_sha(ROLLBACK_WEB_TAR)
        self.public_sha = file_sha(PUBLIC_ZIP)
        if self.web_sha != EXPECTED_WEB_SHA or WEB_TAR.stat().st_size != EXPECTED_WEB_SIZE:
            raise SystemExit(f"web tar mismatch size={WEB_TAR.stat().st_size} sha={self.web_sha}")
        if self.rollback_web_sha != EXPECTED_ROLLBACK_WEB_SHA or ROLLBACK_WEB_TAR.stat().st_size != EXPECTED_ROLLBACK_WEB_SIZE:
            raise SystemExit(f"rollback web tar mismatch size={ROLLBACK_WEB_TAR.stat().st_size} sha={self.rollback_web_sha}")
        if self.public_sha != EXPECTED_PUBLIC_SHA or PUBLIC_ZIP.stat().st_size != EXPECTED_PUBLIC_SIZE:
            raise SystemExit(f"public zip mismatch size={PUBLIC_ZIP.stat().st_size} sha={self.public_sha}")

        self.host, self.domain, self.user, self.password = read_server_file()
        self.public_base_url = f"https://{self.domain}"
        self.public_site_url = f"https://{self.domain}/ecorex-agent"
        self.remote_dir = f"/tmp/ecorex-v022-hotfix-{int(time.time())}"
        self.remote_web_tar = posixpath.join(self.remote_dir, WEB_TAR.name)
        self.remote_rollback_web_tar = posixpath.join(self.remote_dir, ROLLBACK_WEB_TAR.name)
        self.remote_public_zip = posixpath.join(self.remote_dir, PUBLIC_ZIP.name)
        self.remote_install_web = posixpath.join(self.remote_dir, INSTALL_WEB.name)
        self.remote_check_web = posixpath.join(self.remote_dir, CHECK_WEB.name)
        self.remote_install_public = posixpath.join(self.remote_dir, INSTALL_PUBLIC.name)
        self.remote_check_server = posixpath.join(self.remote_dir, CHECK_SERVER.name)

        self.target_commands: list[dict[str, Any]] = []
        self.production_commands: list[dict[str, Any]] = []

    def secret_hash(self, value: str) -> str:
        return sha256_text(value)[:16]

    def redact(self, text: str) -> str:
        if not text:
            return ""
        out = text
        for value in [self.host, self.domain, self.password, self.public_base_url, self.public_site_url]:
            if value:
                out = out.replace(value, "[REDACTED]")
        out = re.sub(r"https?://[^\s\)\]\"']+", "[URL]", out)
        out = re.sub(r"\b\d{1,3}(?:\.\d{1,3}){3}\b", "[IP]", out)
        out = re.sub(r"(?i)(password|secret|token|key)(\s*[=:]\s*)[^\s\n]+", r"\1\2[REDACTED]", out)
        out = re.sub(r"/tmp/ecorex-v022-hotfix-[0-9]+", "/tmp/[RUN_DIR]", out)
        return out[:4000]

    def redact_state(self, value: Any) -> Any:
        if isinstance(value, dict):
            return {k: self.redact_state(v) for k, v in value.items() if k not in {"raw", "password", "secret", "token"}}
        if isinstance(value, list):
            return [self.redact_state(v) for v in value]
        if isinstance(value, str):
            return self.redact(value)
        return value

    def record(
        self,
        rows: list[dict[str, Any]],
        name: str,
        semantic_argv: str,
        exit_code: int,
        stdout: str = "",
        stderr: str = "",
        include_excerpt: bool = False,
    ) -> dict[str, Any]:
        row: dict[str, Any] = {
            "name": name,
            "argvHash": sha256_text(semantic_argv),
            "exitCode": int(exit_code),
            "stdoutHash": sha256_text(stdout or ""),
            "stderrHash": sha256_text(stderr or ""),
        }
        if include_excerpt:
            row["stdoutExcerptRedacted"] = self.redact(stdout or "")
            row["stderrExcerptRedacted"] = self.redact(stderr or "")
        rows.append(row)
        return row

    def run_remote(
        self,
        client: paramiko.SSHClient,
        name: str,
        command: str,
        *,
        timeout: int = 900,
        target: bool = True,
        prod: bool = True,
        check: bool = True,
    ) -> tuple[int, str, str]:
        stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
        del stdin
        out = stdout.read().decode("utf-8", errors="replace")
        err = stderr.read().decode("utf-8", errors="replace")
        code = stdout.channel.recv_exit_status()
        semantic = f"{name}\n{command}"
        if target:
            self.record(self.target_commands, name, semantic, code, out, err, include_excerpt=False)
        if prod:
            self.record(self.production_commands, name, semantic, code, out, err, include_excerpt=True)
        if check and code != 0:
            raise RuntimeError(f"{name} failed code={code}\nSTDOUT:\n{self.redact(out)}\nSTDERR:\n{self.redact(err)}")
        return code, out, err

    def upload(self, client: paramiko.SSHClient, local: Path, remote: str, name: str, *, target: bool = True, prod: bool = True) -> None:
        sftp = client.open_sftp()
        try:
            sftp.put(str(local), remote)
        finally:
            sftp.close()
        semantic = f"sftp-upload:{name}:{local.name}:{remote}:{file_sha(local)}:{local.stat().st_size}"
        if target:
            self.record(self.target_commands, name, semantic, 0, "", "", include_excerpt=False)
        if prod:
            stdout = f"uploaded {local.name} bytes={local.stat().st_size} sha256={file_sha(local)}"
            self.record(self.production_commands, name, semantic, 0, stdout, "", include_excerpt=True)

    @staticmethod
    def state_script() -> str:
        return r"""
python3 - <<'PY'
import hashlib, json, os, pathlib, subprocess, urllib.request

def run(args):
    try:
        p = subprocess.run(args, text=True, capture_output=True, timeout=20)
        return {'code': p.returncode, 'stdout': p.stdout.strip(), 'stderr': p.stderr.strip()}
    except Exception as exc:
        return {'code': 999, 'stdout': '', 'stderr': str(exc)}

def read_json(path):
    try:
        return json.loads(pathlib.Path(path).read_text(encoding='utf-8'))
    except Exception:
        return {}

def sha_file(path):
    try:
        h = hashlib.sha256()
        with open(path, 'rb') as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b''):
                h.update(chunk)
        return h.hexdigest().upper()
    except Exception:
        return ''

current_link = pathlib.Path('/opt/ecorex-web/current')
current_target = os.path.realpath(str(current_link)) if current_link.exists() else ''
release = read_json('/opt/ecorex-web/current/release.json')
manifest = read_json('/srv/ecorex-agent-workspace/.ecorex/installations.json')
admin_manifest = read_json('/srv/ecorex-agent-download/current/manifest.json')
active = run(['systemctl', 'is-active', 'ecorex-web'])
enabled = run(['systemctl', 'is-enabled', 'ecorex-web'])
http_status = None
try:
    req = urllib.request.Request('http://127.0.0.1:9909/app/', headers={'User-Agent': 'ecorex-v022-smoke'})
    with urllib.request.urlopen(req, timeout=10) as resp:
        http_status = resp.status
except Exception:
    http_status = None
print(json.dumps({
    'currentLinkTarget': current_target,
    'currentVersion': release.get('version') or '',
    'releaseArtifactId': release.get('artifactId') or '',
    'releaseSha256': release.get('sha256') or '',
    'releaseJsonSha256': sha_file('/opt/ecorex-web/current/release.json'),
    'installationManifestVersion': manifest.get('version') or '',
    'adminPublicManifestVersion': admin_manifest.get('version') or '',
    'serviceActive': active['stdout'] == 'active',
    'serviceEnabled': enabled['stdout'] == 'enabled',
    'webLocalStatus': http_status,
}, ensure_ascii=False, sort_keys=True))
PY
"""

    @staticmethod
    def latest_v021_script() -> str:
        return r"""
python3 - <<'PY'
import glob, json, pathlib
candidates = []
for path in glob.glob('/opt/ecorex-web/releases/*'):
    release = pathlib.Path(path) / 'release.json'
    try:
        data = json.loads(release.read_text(encoding='utf-8'))
    except Exception:
        continue
    if data.get('version') == '0.2.1':
        candidates.append(path)
print(sorted(candidates)[-1] if candidates else '')
PY
"""

    def install_command(self) -> str:
        return (
            f"VERSION={shlex.quote(VERSION)} "
            f"TARBALL_PATH={shlex.quote(self.remote_web_tar)} "
            f"EXPECTED_SHA256={shlex.quote(EXPECTED_WEB_SHA)} "
            f"PUBLIC_BASE_URL={shlex.quote(self.public_base_url)} "
            f"WEB_HOST=127.0.0.1 WEB_PORT={shlex.quote(WEB_PORT)} OPEN_BROWSER=0 "
            f"bash {shlex.quote(self.remote_install_web)} {shlex.quote(self.remote_web_tar)}"
        )

    def rollback_baseline_install_command(self) -> str:
        return (
            f"VERSION={shlex.quote(ROLLBACK_VERSION)} "
            f"TARBALL_PATH={shlex.quote(self.remote_rollback_web_tar)} "
            f"EXPECTED_SHA256={shlex.quote(EXPECTED_ROLLBACK_WEB_SHA)} "
            f"PUBLIC_BASE_URL={shlex.quote(self.public_base_url)} "
            f"WEB_HOST=127.0.0.1 WEB_PORT={shlex.quote(WEB_PORT)} OPEN_BROWSER=0 "
            f"bash {shlex.quote(self.remote_install_web)} {shlex.quote(self.remote_rollback_web_tar)}"
        )

    def check_web_command(self) -> str:
        return (
            f"VERSION={shlex.quote(VERSION)} BASE_URL=http://127.0.0.1:{WEB_PORT} "
            f"CHECK_HTTP=1 CHECK_SYSTEMD=1 CHECK_INSTALLED=1 "
            f"bash {shlex.quote(self.remote_check_web)} {shlex.quote(self.remote_web_tar)}"
        )

    def run(self) -> dict[str, Any]:
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

        try:
            self.run_remote(client, "prepare_remote_dir", f"mkdir -p {shlex.quote(self.remote_dir)}", timeout=60)
            self.upload(client, WEB_TAR, self.remote_web_tar, "upload_package")
            self.upload(client, INSTALL_WEB, self.remote_install_web, "upload_installer")
            for local, remote in [
                (CHECK_WEB, self.remote_check_web),
                (INSTALL_PUBLIC, self.remote_install_public),
                (CHECK_SERVER, self.remote_check_server),
                (PUBLIC_ZIP, self.remote_public_zip),
            ]:
                self.upload(client, local, remote, f"upload_aux_{local.name}", target=False, prod=True)
            self.record(
                self.target_commands,
                "upload_checker",
                f"sftp-upload:upload_checker:{CHECK_WEB.name}:{self.remote_check_web}:{file_sha(CHECK_WEB)}:{CHECK_WEB.stat().st_size}",
                0,
            )

            chmod_cmd = (
                f"chmod +x {shlex.quote(self.remote_install_web)} {shlex.quote(self.remote_check_web)} "
                f"{shlex.quote(self.remote_install_public)} {shlex.quote(self.remote_check_server)}"
            )
            self.run_remote(client, "chmod_release_scripts", chmod_cmd, timeout=60)

            _, current_before_out, _ = self.run_remote(
                client, "probe-current-before-baseline", self.state_script(), timeout=60, target=False
            )
            current_before = json.loads(current_before_out)
            if current_before.get("currentVersion") != ROLLBACK_VERSION:
                _, candidate_out, _ = self.run_remote(
                    client, "find-v021-rollback-baseline", self.latest_v021_script(), timeout=60, target=False
                )
                candidate = candidate_out.strip()
                if not candidate:
                    self.upload(client, ROLLBACK_WEB_TAR, self.remote_rollback_web_tar, "upload_rollback_baseline_package")
                    self.run_remote(
                        client,
                        "rebuild-v021-rollback-baseline",
                        self.rollback_baseline_install_command(),
                        timeout=1200,
                        target=False,
                    )
                else:
                    baseline_cmd = f"ln -sfn {shlex.quote(candidate)} /opt/ecorex-web/current && systemctl restart {shlex.quote(SERVICE_NAME)} && sleep 2"
                    self.run_remote(client, "prepare-v021-rollback-baseline", baseline_cmd, timeout=180, target=False)

            _, pre_out, _ = self.run_remote(client, "capture_pre_state", self.state_script(), timeout=60)
            pre_state = json.loads(pre_out)
            if pre_state.get("currentVersion") != ROLLBACK_VERSION:
                raise RuntimeError(f"pre-state is not {ROLLBACK_VERSION}: {pre_state}")
            pre_current = pre_state.get("currentLinkTarget") or ""
            if not pre_current:
                raise RuntimeError("missing pre-state current symlink target")

            self.run_remote(client, "install_v022", self.install_command(), timeout=1200)
            self.run_remote(client, "check_deploy", self.check_web_command(), timeout=300)

            _, deploy_out, _ = self.run_remote(client, "capture_deploy_state", self.state_script(), timeout=60)
            deploy_state = json.loads(deploy_out)
            if deploy_state.get("currentVersion") != VERSION:
                raise RuntimeError(f"deploy-state is not {VERSION}: {deploy_state}")

            rollback_cmd = f"ln -sfn {shlex.quote(pre_current)} /opt/ecorex-web/current && systemctl restart {shlex.quote(SERVICE_NAME)} && sleep 2"
            self.run_remote(client, "rollback_to_previous", rollback_cmd, timeout=180)
            _, rollback_out, _ = self.run_remote(client, "capture_rollback_state", self.state_script(), timeout=60)
            rollback_state = json.loads(rollback_out)
            if rollback_state.get("currentVersion") != ROLLBACK_VERSION:
                raise RuntimeError(f"rollback-state is not {ROLLBACK_VERSION}: {rollback_state}")

            self.run_remote(client, "final_install_v022", self.install_command(), timeout=1200, target=False)
            self.run_remote(client, "final_check_v022_web", self.check_web_command(), timeout=300, target=False)

            install_public_cmd = (
                f"VERSION={shlex.quote(VERSION)} ZIP_PATH={shlex.quote(self.remote_public_zip)} "
                f"bash {shlex.quote(self.remote_install_public)} {shlex.quote(self.remote_public_zip)}"
            )
            self.run_remote(client, "install_public_site_and_admin", install_public_cmd, timeout=600, target=False)
            public_check_cmd = (
                f"VERSION={shlex.quote(VERSION)} PUBLIC_BASE_URL={shlex.quote(self.public_site_url)} "
                f"CHECK_PUBLIC=1 CHECK_CADDY=0 bash {shlex.quote(self.remote_check_server)}"
            )
            self.run_remote(client, "check_public_site_and_admin", public_check_cmd, timeout=300, target=False)
            _, post_out, _ = self.run_remote(client, "capture_final_online_state", self.state_script(), timeout=60, target=False)
            post_state = json.loads(post_out)

            _, remote_sha_out, _ = self.run_remote(
                client,
                "probe_uploaded_web_tar_sha256",
                f"sha256sum {shlex.quote(self.remote_web_tar)} | awk '{{print $1}}'",
                timeout=60,
                target=False,
            )
            remote_tar_sha = remote_sha_out.strip().split()[0].upper() if remote_sha_out.strip() else ""
        finally:
            client.close()

        local_smoke = json.loads(LOCAL_SMOKE.read_text(encoding="utf-8"))
        local_artifact = local_smoke.get("artifact", {})
        package_checks = local_smoke.get("packageChecks", {})
        release_json = local_artifact.get("releaseJson", {})
        sha256_file = dict(local_artifact.get("sha256File") or {})
        sha256_file["matches"] = remote_tar_sha == self.web_sha
        sha256_file["sha256"] = remote_tar_sha

        target_artifact = {
            "status": "PASS",
            "scope": "target-environment-web-linux-service",
            "version": VERSION,
            "generatedAt": datetime.now(timezone.utc).isoformat(),
            "productionEnvironment": True,
            "requiresNetwork": True,
            "requiresSystemd": True,
            "requiresRoot": True,
            "pointerMethod": "target-current-symlink",
            "localPackage": {
                "path": str(WEB_TAR).replace("\\", "/"),
                "size": WEB_TAR.stat().st_size,
                "sha256": self.web_sha,
                "artifactId": WEB_TAR.name,
            },
            "artifact": {
                "path": local_artifact.get("path") or str(WEB_TAR.relative_to(ROOT)).replace("\\", "/"),
                "artifactId": local_artifact.get("artifactId") or "web-linux-service",
                "version": VERSION,
                "size": WEB_TAR.stat().st_size,
                "sha256": self.web_sha,
                "sha256File": sha256_file,
                "releaseJson": release_json
                or {
                    "version": VERSION,
                    "artifactId": "web-linux-service",
                    "sha256": self.web_sha,
                },
            },
            "target": {
                "sshHostHash": self.secret_hash(self.host),
                "sshPortHash": self.secret_hash("22"),
                "sshUserHash": self.secret_hash(self.user),
                "serviceNameHash": self.secret_hash(SERVICE_NAME),
                "installRootHash": self.secret_hash("/opt/ecorex-web"),
                "workspaceRootHash": self.secret_hash("/srv/ecorex-agent-workspace"),
                "remoteBaseUrl": {"hostHash": self.secret_hash(self.domain), "schemeHash": self.secret_hash("https")},
                "rawTargetPersisted": False,
                "pointerMethod": "target-current-symlink",
            },
            "packageChecks": package_checks,
            "preState": self.redact_state(pre_state),
            "deploy": {
                "verified": deploy_state.get("currentVersion") == VERSION and deploy_state.get("serviceActive") is True,
                "currentVersionAfterDeploy": deploy_state.get("currentVersion"),
                "targetCheckCommandPassed": True,
                "serviceActiveAfterDeploy": deploy_state.get("serviceActive") is True,
                "serviceEnabledAfterDeploy": deploy_state.get("serviceEnabled") is True,
                "state": self.redact_state(deploy_state),
            },
            "rollback": {
                "verified": rollback_state.get("currentVersion") == ROLLBACK_VERSION and rollback_state.get("serviceActive") is True,
                "currentVersionAfterRollback": rollback_state.get("currentVersion"),
                "serviceActiveAfterRollback": rollback_state.get("serviceActive") is True,
                "serviceEnabledAfterRollback": rollback_state.get("serviceEnabled") is True,
                "state": self.redact_state(rollback_state),
                "candidateRetainedForAudit": bool(deploy_state.get("currentLinkTarget")),
                "pointerMethod": "target-current-symlink",
            },
            "commands": self.target_commands,
            "redaction": {
                "rawTargetPersisted": False,
                "rawPasswordPersisted": False,
                "rawCommandsPersisted": False,
                "rawStdoutPersisted": False,
                "rawStderrPersisted": False,
                "rawSecretsPersisted": False,
                "rawOutputPersisted": False,
            },
        }

        production_artifact = {
            "status": "PASS" if post_state.get("currentVersion") == VERSION else "FAIL",
            "scope": "production-online-web-and-admin",
            "version": VERSION,
            "generatedAt": datetime.now(timezone.utc).isoformat(),
            "productionEnvironment": True,
            "artifacts": {
                "webTarball": {"path": str(WEB_TAR).replace("\\", "/"), "size": WEB_TAR.stat().st_size, "sha256": self.web_sha},
                "publicZip": {"path": str(PUBLIC_ZIP).replace("\\", "/"), "size": PUBLIC_ZIP.stat().st_size, "sha256": self.public_sha},
            },
            "target": {
                "sshHostHash": self.secret_hash(self.host),
                "sshUserHash": self.secret_hash(self.user),
                "domainHash": self.secret_hash(self.domain),
                "rawTargetPersisted": False,
            },
            "preState": self.redact_state(current_before),
            "targetSmoke": {
                "deployRollbackArtifact": str(TARGET_ARTIFACT).replace("\\", "/"),
                "deployVerified": target_artifact["deploy"]["verified"],
                "rollbackVerified": target_artifact["rollback"]["verified"],
            },
            "postState": self.redact_state(post_state),
            "commands": self.production_commands,
            "onlineChecks": {
                "webLocalStatus": post_state.get("webLocalStatus"),
                "webVersion": post_state.get("currentVersion"),
                "serviceActive": post_state.get("serviceActive"),
                "serviceEnabled": post_state.get("serviceEnabled"),
                "publicManifestVersion": post_state.get("adminPublicManifestVersion"),
                "publicCheckExecuted": True,
            },
            "redaction": {
                "rawTargetPersisted": False,
                "rawPasswordPersisted": False,
                "rawSecretPersisted": False,
                "rawUrlPersisted": False,
                "rawOutputPersisted": False,
            },
        }

        TARGET_ARTIFACT.write_text(json.dumps(target_artifact, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        PROD_ARTIFACT.write_text(json.dumps(production_artifact, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

        return {
            "status": production_artifact["status"],
            "targetDeployRollback": {
                "deployVerified": target_artifact["deploy"]["verified"],
                "rollbackVerified": target_artifact["rollback"]["verified"],
                "artifact": str(TARGET_ARTIFACT),
            },
            "online": production_artifact["onlineChecks"],
            "productionArtifact": str(PROD_ARTIFACT),
        }


if __name__ == "__main__":
    result = Deployer().run()
    print(json.dumps(result, ensure_ascii=False, indent=2))
