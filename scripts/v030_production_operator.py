from __future__ import annotations

import hashlib
from pathlib import Path
import re


EXPECTED_DOMAIN_HASH = "A753D877497CBE35"
EXPECTED_HOST_HASH = "CDF1CF905198CA97"


def identity(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest().upper()[:16]


def read_operator_file(path: Path) -> tuple[str, str, str, str]:
    try:
        text = path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        text = path.read_text(encoding="gb18030")
    host_match = re.search(
        r"https?://(\d{1,3}(?:\.\d{1,3}){3})(?::\d+)?|\b(\d{1,3}(?:\.\d{1,3}){3})\b",
        text,
    )
    domain_match = re.search(r"域名\s*[:：]\s*([^\s]+)", text)
    ssh_match = re.search(r"ssh登录\s*[:：]\s*([^\r\n]+)(.*)", text, re.I | re.S)
    if host_match is None or domain_match is None or ssh_match is None:
        raise ValueError("operator_file_invalid")
    host = next(group for group in host_match.groups() if group)
    domain = domain_match.group(1).strip().strip("/")
    user = ssh_match.group(1).strip()
    passwords = re.findall(r"密码\s*[:：]\s*([^\r\n]+)", ssh_match.group(2))
    if not user or not passwords:
        raise ValueError("operator_file_invalid")
    if identity(host) != EXPECTED_HOST_HASH or identity(domain) != EXPECTED_DOMAIN_HASH:
        raise ValueError("production_identity_mismatch")
    return host, domain, user, passwords[-1].strip()


def connect_operator(path: Path):
    try:
        import paramiko
    except ImportError:
        raise RuntimeError("paramiko_unavailable") from None
    host, _domain, user, password = read_operator_file(path)
    client = paramiko.SSHClient()
    client.load_system_host_keys()
    client.set_missing_host_key_policy(paramiko.RejectPolicy())
    client.connect(
        host,
        username=user,
        password=password,
        allow_agent=False,
        look_for_keys=False,
        timeout=20,
        auth_timeout=20,
        banner_timeout=20,
    )
    password = ""
    return client
