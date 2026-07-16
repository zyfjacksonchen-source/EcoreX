from __future__ import annotations

import base64
from pathlib import Path
import runpy
import sys
from types import SimpleNamespace

import pytest


def _module() -> dict:
    return runpy.run_path(
        str(
            Path(__file__).resolve().parents[2]
            / "scripts"
            / "ecorex-v1-ssh-publication-signer.py"
        )
    )


def test_credential_file_parser_is_bounded_and_selects_ssh_section(tmp_path: Path) -> None:
    module = _module()
    path = tmp_path / "server.txt"
    path.write_text(
        "服务器: 192.0.2.10\n密码: wrong\nSSH登录：root\n密码： correct-secret\n",
        encoding="utf-8",
    )
    assert module["_read_credentials"](path) == (
        "192.0.2.10",
        "root",
        "correct-secret",
    )


def test_credential_file_parser_rejects_links_and_invalid_ip(tmp_path: Path) -> None:
    module = _module()
    target = tmp_path / "target.txt"
    target.write_text("999.0.0.1\nSSH登录:root\n密码:secret\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="credential_file_invalid"):
        module["_read_credentials"](target)
    link = tmp_path / "link.txt"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symlink unavailable")
    with pytest.raises(RuntimeError, match="credential_file_invalid"):
        module["_read_credentials"](link)


def test_signer_uses_fixed_command_host_key_policy_and_stdin_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _module()
    path = tmp_path / "server.txt"
    path.write_text(
        "服务器: 192.0.2.10\nSSH登录：operator\n密码： secret\n",
        encoding="utf-8",
    )
    signature = base64.b64encode(b"s" * 64) + b"\n"

    class Channel:
        def __init__(self) -> None:
            self.stdout = [signature, b""]
            self.command = ""
            self.payload = b""
            self.write_closed = False
            self.closed = False

        def settimeout(self, value: int) -> None:
            assert value == 45

        def exec_command(self, value: str) -> None:
            self.command = value

        def sendall(self, value: bytes) -> None:
            self.payload += value

        def shutdown_write(self) -> None:
            self.write_closed = True

        def recv(self, _maximum: int) -> bytes:
            return self.stdout.pop(0)

        def recv_stderr(self, _maximum: int) -> bytes:
            return b""

        def recv_exit_status(self) -> int:
            return 0

        def close(self) -> None:
            self.closed = True

    channel = Channel()

    class Transport:
        def is_authenticated(self) -> bool:
            return True

        def open_session(self, *, timeout: int) -> Channel:
            assert timeout == 10
            return channel

    class RejectPolicy:
        pass

    class SSHClient:
        instance: "SSHClient"

        def __init__(self) -> None:
            SSHClient.instance = self
            self.loaded_host_keys = False
            self.policy: object | None = None
            self.connect_kwargs: dict[str, object] = {}
            self.closed = False

        def load_system_host_keys(self) -> None:
            self.loaded_host_keys = True

        def set_missing_host_key_policy(self, value: object) -> None:
            self.policy = value

        def connect(self, **kwargs: object) -> None:
            self.connect_kwargs = kwargs

        def get_transport(self) -> Transport:
            return Transport()

        def close(self) -> None:
            self.closed = True

    monkeypatch.setitem(
        sys.modules,
        "paramiko",
        SimpleNamespace(SSHClient=SSHClient, RejectPolicy=RejectPolicy),
    )

    assert module["_sign"](b"payload-to-sign", path) == signature
    client = SSHClient.instance
    assert client.loaded_host_keys is True
    assert isinstance(client.policy, RejectPolicy)
    assert client.connect_kwargs == {
        "hostname": "192.0.2.10",
        "port": 22,
        "username": "operator",
        "password": "secret",
        "timeout": 10,
        "banner_timeout": 10,
        "auth_timeout": 10,
        "allow_agent": False,
        "look_for_keys": False,
    }
    assert channel.command == "/usr/local/sbin/ecorex-sign-publication"
    assert channel.payload == b"payload-to-sign"
    assert channel.write_closed is True
    assert channel.closed is True
    assert client.closed is True
