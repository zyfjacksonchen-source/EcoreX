#!/usr/bin/env python3
"""Digest-pinnable SSH bridge to the server-resident publication signer.

The private key never crosses SSH.  This adapter receives exactly one signing
payload on stdin and returns only the remote Base64 Ed25519 signature; the
calling ``DigestPinnedExternalSigner`` performs the public-key verification.
"""

from __future__ import annotations

import os
from pathlib import Path
import re
import stat
import sys


MAX_PAYLOAD_BYTES = 16 * 1024 * 1024
MAX_RESPONSE_BYTES = 1024
REMOTE_COMMAND = "/usr/local/sbin/ecorex-sign-publication"
_IP = re.compile(r"\b(\d{1,3}(?:\.\d{1,3}){3})\b")


def _read_credentials(path: Path) -> tuple[str, str, str]:
    try:
        before = path.lstat()
        reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        if (
            stat.S_ISLNK(before.st_mode)
            or bool(getattr(before, "st_file_attributes", 0) & reparse)
            or not stat.S_ISREG(before.st_mode)
            or not 1 <= before.st_size <= 64 * 1024
        ):
            raise OSError
        with path.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            payload = stream.read(64 * 1024 + 1)
            after = os.fstat(stream.fileno())
        current = path.lstat()
    except OSError:
        raise RuntimeError("ssh_signer_credential_file_invalid") from None
    if (
        len(payload) != before.st_size
        or _identity(before) != _identity(opened)
        or _identity(before) != _identity(after)
        or _identity(before) != _identity(current)
    ):
        raise RuntimeError("ssh_signer_credential_file_changed")
    text = payload.decode("utf-8-sig", errors="strict")
    host_match = _IP.search(text)
    ssh_match = re.search(
        r"ssh\s*登录\s*[:：]\s*([^\r\n]+)(.*)",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    user = ssh_match.group(1).strip() if ssh_match else "root"
    password_section = ssh_match.group(2) if ssh_match else text
    passwords = re.findall(r"密码\s*[:：]\s*([^\r\n]+)", password_section)
    if (
        host_match is None
        or not user
        or not passwords
        or "\x00" in user
        or "\x00" in passwords[-1]
    ):
        raise RuntimeError("ssh_signer_credential_file_invalid")
    octets = tuple(int(item) for item in host_match.group(1).split("."))
    if any(item > 255 for item in octets):
        raise RuntimeError("ssh_signer_credential_file_invalid")
    return host_match.group(1), user, passwords[-1].strip()


def _read_channel(channel, *, maximum: int, stderr: bool = False) -> bytes:
    chunks: list[bytes] = []
    size = 0
    while True:
        receiver = channel.recv_stderr if stderr else channel.recv
        chunk = receiver(min(4096, maximum + 1 - size))
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)
        size += len(chunk)
        if size > maximum:
            raise RuntimeError("ssh_signer_response_too_large")


def _sign(payload: bytes, credential_file: Path) -> bytes:
    if not isinstance(payload, bytes) or not 1 <= len(payload) <= MAX_PAYLOAD_BYTES:
        raise RuntimeError("ssh_signer_payload_invalid")
    try:
        import paramiko
    except ImportError:
        raise RuntimeError("ssh_signer_transport_unavailable") from None
    host, user, password = _read_credentials(credential_file)
    client = paramiko.SSHClient()
    client.load_system_host_keys()
    client.set_missing_host_key_policy(paramiko.RejectPolicy())
    try:
        client.connect(
            hostname=host,
            port=22,
            username=user,
            password=password,
            timeout=10,
            banner_timeout=10,
            auth_timeout=10,
            allow_agent=False,
            look_for_keys=False,
        )
        transport = client.get_transport()
        if transport is None or not transport.is_authenticated():
            raise RuntimeError("ssh_signer_authentication_failed")
        channel = transport.open_session(timeout=10)
        try:
            channel.settimeout(45)
            channel.exec_command(REMOTE_COMMAND)
            channel.sendall(payload)
            channel.shutdown_write()
            stdout = _read_channel(channel, maximum=MAX_RESPONSE_BYTES)
            stderr = _read_channel(
                channel, maximum=MAX_RESPONSE_BYTES, stderr=True
            )
            status = channel.recv_exit_status()
        finally:
            channel.close()
    finally:
        client.close()
    if status != 0 or stderr or not re.fullmatch(rb"[A-Za-z0-9+/]{86}==\r?\n", stdout):
        raise RuntimeError("ssh_signer_remote_failed")
    return stdout


def _identity(value: os.stat_result) -> tuple[int, int, int, int]:
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns


def main() -> int:
    try:
        raw = os.environ.get("ECOREX_SSH_SIGNER_CREDENTIAL_FILE")
        if not raw or not Path(raw).is_absolute() or len(sys.argv) != 1:
            raise RuntimeError("ssh_signer_configuration_invalid")
        payload = sys.stdin.buffer.read(MAX_PAYLOAD_BYTES + 1)
        sys.stdout.buffer.write(_sign(payload, Path(raw)))
        return 0
    except Exception:
        sys.stderr.write("ssh_publication_signer_failed\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
