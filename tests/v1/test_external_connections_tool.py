from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent.tools.external_connections import (
    ExternalConnectionRuntime,
    ExternalConnectionsTool,
    bind_external_connection_runtime,
)


class _ConnectorService:
    def __init__(self) -> None:
        self.calls = []

    async def begin_connect(self, connector_id, **kwargs):
        self.calls.append((connector_id, kwargs))
        return SimpleNamespace(
            to_dict=lambda: {
                "flow_id": "connflow_owned123",
                "connector_id": "feishu",
                "auth_kind": "oauth2",
                "expires_at": (datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
                "authorization_url": "https://auth.example.test/owned",
                "user_code": None,
                "verification_url": None,
            }
        )

    def catalog(self):
        return ()


class _ConnectorRepository:
    owner_prefix = None

    def lifecycle_owner_has_flow(self, owner_prefix, flow_id):
        if self.owner_prefix is None:
            self.owner_prefix = owner_prefix
        return owner_prefix == self.owner_prefix and flow_id == "connflow_owned123"

    def auth_completion_for_flow(self, flow_id):
        return None

    def get_active_flow(self, flow_id):
        return SimpleNamespace(connector_id="feishu")


class _ChannelService:
    PNG = b"\x89PNG\r\n\x1a\nreal-owned-qr"

    def catalog(self):
        return {
            "items": [
                {
                    "channel_id": "weixin",
                    "instance": None,
                    "fields": [],
                    "actions": {"enable": False},
                },
                {
                    "channel_id": "dingtalk",
                    "instance": None,
                    "fields": [
                        {
                            "key": "dingtalk_client_secret",
                            "label": "Client Secret",
                            "required": True,
                            "secret": True,
                            "configured": False,
                        }
                    ],
                    "actions": {"enable": False},
                },
            ]
        }

    def begin_authorization(self, channel_id, *, request_id):
        assert channel_id == "weixin"
        assert request_id
        return {
            "channel_id": "weixin",
            "flow_id": "weixinflow_owned123",
            "status": "pending",
            "verification_url": "https://weixin.example.test/scan",
            "qr_image_data_url": "data:image/png;base64,"
            + base64.b64encode(self.PNG).decode("ascii"),
            "expires_at": (datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
        }

    def poll_authorization(self, channel_id, flow_id, *, request_id):
        assert (channel_id, flow_id) == ("weixin", "weixinflow_owned123")
        return {
            "channel_id": "weixin",
            "flow_id": flow_id,
            "status": "confirmed",
            "verification_url": None,
            "qr_image_data_url": None,
            "expires_at": datetime.now(UTC).isoformat(),
        }


class _MCPService:
    def __init__(self) -> None:
        self.server = None

    def for_workspace(self, workspace):
        self.workspace = Path(workspace)
        return self

    def list(self):
        return [] if self.server is None else [self.server]

    def create(self, request):
        assert request.auth_kind == "oauth2"
        assert request.endpoint == "https://docs.qq.com/openapi/mcp"
        self.server = {
            "server_id": "user.mcp.tencent",
            "endpoint": request.endpoint,
            "auth_kind": request.auth_kind,
            "credential_configured": False,
            "tool_count": 0,
            "tool_names": [],
        }
        return self.server

    def begin_oauth(self, server_id):
        assert server_id == "user.mcp.tencent"
        return {
            "service_id": server_id,
            "state": "authorizing",
            "authorization_url": "https://docs.qq.com/scenario/open-claw.html",
            "expires_at": 123,
        }

    def oauth_items(self):
        return []


@pytest.fixture(autouse=True)
def _reset_runtime_binding():
    yield
    bind_external_connection_runtime(None)


def test_agent_connection_tool_uses_real_owned_lifecycles(tmp_path: Path) -> None:
    connector = _ConnectorService()
    mcp = _MCPService()
    bind_external_connection_runtime(
        ExternalConnectionRuntime(
            connector_service=connector,
            connector_repository=_ConnectorRepository(),
            connector_oauth_return_uri="http://127.0.0.1:8765/api/v1/connectors/oauth/callback",
            channel_service=_ChannelService(),
            mcp_service=mcp,
        )
    )
    tool = ExternalConnectionsTool()
    tool.apply_config({"cwd": str(tmp_path)})
    tool.context = SimpleNamespace(
        _current_request_id="turn-owned", _current_session_id="session-owned"
    )

    feishu = tool.execute({"provider": "feishu", "action": "start"})
    assert feishu.status == "success"
    assert feishu.result["flow_id"] == "connflow_owned123"
    assert connector.calls[0][1]["return_uri"].startswith("http://127.0.0.1:")
    feishu_poll = tool.execute(
        {"provider": "feishu", "action": "poll", "flow_id": "connflow_owned123"}
    )
    assert feishu_poll.status == "success"
    assert feishu_poll.result["state"] == "awaiting_callback"
    tool.context._current_session_id = "other-session"
    not_owned = tool.execute(
        {"provider": "feishu", "action": "poll", "flow_id": "connflow_owned123"}
    )
    assert not_owned.status == "error"
    assert not_owned.result["error_code"] == "external_connection_flow_not_owned"
    tool.context._current_session_id = "session-owned"

    tencent = tool.execute({"provider": "tencent-docs", "action": "start"})
    assert tencent.status == "success"
    assert tencent.result["state"] == "authorizing"
    assert mcp.server["auth_kind"] == "oauth2"

    weixin = tool.execute({"provider": "weixin", "action": "start"})
    assert weixin.status == "success"
    assert "qr_image_data_url" not in weixin.result
    artifact = Path(weixin.result["qr_artifact_path"])
    assert artifact.read_bytes() == _ChannelService.PNG
    assert artifact.stat().st_mode & 0o777 == 0o600
    confirmed = tool.execute(
        {
            "provider": "weixin",
            "action": "poll",
            "flow_id": "weixinflow_owned123",
        }
    )
    assert confirmed.status == "success"
    assert confirmed.result["ready"] is True

    dingtalk = tool.execute({"provider": "dingtalk", "action": "start"})
    assert dingtalk.status == "success"
    assert dingtalk.result["requires_user_handoff"] is True
    assert dingtalk.result["fields"] == [
        {
            "key": "dingtalk_client_secret",
            "label": "Client Secret",
            "required": True,
            "secret": True,
            "configured": False,
        }
    ]


def test_agent_connection_tool_returns_stable_error_codes(tmp_path: Path) -> None:
    class FailedConnector(_ConnectorService):
        async def begin_connect(self, connector_id, **kwargs):
            error = RuntimeError("provider response must not leak")
            error.code = "connector_auth_error"
            raise error

    bind_external_connection_runtime(
        ExternalConnectionRuntime(
            connector_service=FailedConnector(),
            connector_repository=_ConnectorRepository(),
            connector_oauth_return_uri="http://127.0.0.1:8765/api/v1/connectors/oauth/callback",
            channel_service=_ChannelService(),
            mcp_service=_MCPService(),
        )
    )
    tool = ExternalConnectionsTool()
    tool.apply_config({"cwd": str(tmp_path)})

    result = tool.execute({"provider": "feishu", "action": "start"})

    assert result.status == "error"
    assert result.result["error_code"] == "connector_auth_error"
    assert "provider response" not in str(result.result)
