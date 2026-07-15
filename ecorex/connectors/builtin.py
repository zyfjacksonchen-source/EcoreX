"""Built-in v1 connector definitions. Adapters are capability packs."""

from __future__ import annotations

from .models import (
    ConnectorActionSpec,
    ConnectorAuthKind,
    ConnectorDefinition,
    ConnectorEffect,
    ConnectorEventSpec,
    ConnectorTier,
)
from .registry import ConnectorAdapter, ConnectorRegistry


_OBJECT = {"type": "object"}
_DOCUMENT_READ_INPUT = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "document_id": {"type": "string", "minLength": 1, "maxLength": 512},
    },
    "required": ["document_id"],
}
_DOCUMENT_WRITE_INPUT = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "document_id": {"type": "string", "minLength": 1, "maxLength": 512},
        "revision_id": {"type": "string", "minLength": 1, "maxLength": 512},
        "title": {"type": "string", "minLength": 1, "maxLength": 1024},
        "content": {"type": "string", "maxLength": 4 * 1024 * 1024},
    },
}
_DOCUMENT_LIST_INPUT = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "cursor": {"type": "string", "minLength": 1, "maxLength": 1024},
        "limit": {"type": "integer", "minimum": 1, "maximum": 500},
    },
}
_DRIVE_SEARCH_INPUT = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "query": {"type": "string", "minLength": 1, "maxLength": 4096},
        "cursor": {"type": "string", "minLength": 1, "maxLength": 1024},
        "limit": {"type": "integer", "minimum": 1, "maximum": 500},
    },
    "required": ["query"],
}
_MESSAGE_SEND_INPUT = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "conversation_id": {"type": "string", "minLength": 1, "maxLength": 512},
        "recipient_id": {"type": "string", "minLength": 1, "maxLength": 512},
        "text": {"type": "string", "minLength": 1, "maxLength": 128 * 1024},
    },
    "required": ["text"],
}
_PUBLIC_ID = {
    "type": "string",
    "maxLength": 512,
    "x-ecorex-public-kind": "public_id",
}
_PUBLIC_URI = {
    "type": "string",
    "maxLength": 4096,
    "x-ecorex-public-kind": "public_uri",
}
_PUBLIC_TEXT = {
    "type": "string",
    "maxLength": 4 * 1024 * 1024,
    "x-ecorex-public-kind": "text",
}
_PUBLIC_TITLE = {
    "type": ["string", "null"],
    "maxLength": 1024,
    "x-ecorex-public-kind": "text",
}
_PUBLIC_TIMESTAMP = {
    "type": ["string", "null"],
    "maxLength": 64,
    "x-ecorex-public-kind": "timestamp",
}
_BASE_PROPERTIES = {
    "ok": {"type": "boolean"},
    "action_id": {
        "type": "string",
        "maxLength": 128,
        "x-ecorex-public-kind": "enum",
    },
    "title": _PUBLIC_TITLE,
}
_DOCUMENT_PROPERTIES = {
    "document_id": _PUBLIC_ID,
    "revision_id": {
        "type": ["string", "null"],
        **{key: value for key, value in _PUBLIC_ID.items() if key != "type"},
    },
    "title": _PUBLIC_TITLE,
    "content": {
        "type": ["string", "null"],
        **{key: value for key, value in _PUBLIC_TEXT.items() if key != "type"},
    },
    "url": {"type": ["string", "null"], **{k: v for k, v in _PUBLIC_URI.items() if k != "type"}},
    "updated_at": _PUBLIC_TIMESTAMP,
}
_DOCUMENT = {
    "type": "object",
    "additionalProperties": False,
    "properties": _DOCUMENT_PROPERTIES,
}
_DOCUMENT_READ_RESULT = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        **_BASE_PROPERTIES,
        **_DOCUMENT_PROPERTIES,
        "document": {
            "type": ["object", "null"],
            **{key: value for key, value in _DOCUMENT.items() if key != "type"},
        },
    },
}
_DOCUMENT_LIST_RESULT = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        **_BASE_PROPERTIES,
        "items": {"type": "array", "items": _DOCUMENT, "maxItems": 500},
        "has_more": {"type": "boolean"},
        "next_cursor": {
            "type": ["string", "null"],
            "maxLength": 1024,
            "x-ecorex-public-kind": "connector_cursor",
        },
    },
}
_DOCUMENT_WRITE_RESULT = {
    "type": "object",
    "additionalProperties": False,
    "properties": {**_BASE_PROPERTIES, **_DOCUMENT_PROPERTIES},
}
_DRIVE_ITEM = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "file_id": _PUBLIC_ID,
        "name": _PUBLIC_TITLE,
        "kind": {
            "type": "string",
            "maxLength": 64,
            "x-ecorex-public-kind": "enum",
        },
        "mime_type": {
            "type": ["string", "null"],
            "maxLength": 255,
            "x-ecorex-public-kind": "mime_type",
        },
        "url": {
            "type": ["string", "null"],
            **{key: value for key, value in _PUBLIC_URI.items() if key != "type"},
        },
        "modified_at": _PUBLIC_TIMESTAMP,
    },
}
_DRIVE_SEARCH_RESULT = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        **_BASE_PROPERTIES,
        "items": {"type": "array", "items": _DRIVE_ITEM, "maxItems": 500},
        "has_more": {"type": "boolean"},
        "next_cursor": {
            "type": ["string", "null"],
            "maxLength": 1024,
            "x-ecorex-public-kind": "connector_cursor",
        },
    },
}
_MESSAGE_SEND_RESULT = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        **_BASE_PROPERTIES,
        "message_id": _PUBLIC_ID,
        "conversation_id": {
            "type": ["string", "null"],
            **{key: value for key, value in _PUBLIC_ID.items() if key != "type"},
        },
        "sent_at": _PUBLIC_TIMESTAMP,
        "url": {
            "type": ["string", "null"],
            **{key: value for key, value in _PUBLIC_URI.items() if key != "type"},
        },
    },
}

_ACTION_INTENT_ALIASES = {
    "documents.read": ("读取文档", "查看文档", "read document", "get document"),
    "documents.list": ("列出文档", "文档列表", "list documents"),
    "documents.write": ("编辑文档", "修改文档", "写入文档", "edit document"),
    "drive.search": ("搜索云空间", "查找云盘", "search drive"),
    "messages.send": ("发送消息", "发送通知", "send message"),
}


def _read(
    action_id: str,
    name: str,
    scopes: frozenset[str],
    output_schema: dict,
    *,
    input_schema: dict,
) -> ConnectorActionSpec:
    return ConnectorActionSpec(
        action_id=action_id,
        display_name=name,
        description=name,
        input_schema=input_schema,
        output_schema=output_schema,
        effects=frozenset({ConnectorEffect.READ}),
        required_scopes=scopes,
        intent_aliases=_ACTION_INTENT_ALIASES.get(action_id, ()),
    )


def _write(
    action_id: str,
    name: str,
    scopes: frozenset[str],
    output_schema: dict,
    *,
    input_schema: dict,
) -> ConnectorActionSpec:
    return ConnectorActionSpec(
        action_id=action_id,
        display_name=name,
        description=name,
        input_schema=input_schema,
        output_schema=output_schema,
        effects=frozenset({ConnectorEffect.WRITE}),
        required_scopes=scopes,
        idempotent=True,
        intent_aliases=_ACTION_INTENT_ALIASES.get(action_id, ()),
    )


def builtin_connector_definitions() -> tuple[ConnectorDefinition, ...]:
    feishu = ConnectorDefinition(
        connector_id="feishu",
        contract_version="1.0",
        display_name="飞书",
        description="飞书消息、文档、云空间与协作能力",
        tier=ConnectorTier.STABLE,
        auth_kinds=(ConnectorAuthKind.OAUTH2, ConnectorAuthKind.APP_CREDENTIALS),
        config_schema=_OBJECT,
        actions=(
            _read(
                "documents.read",
                "读取飞书文档",
                frozenset({"docx:document:readonly"}),
                _DOCUMENT_READ_RESULT,
                input_schema=_DOCUMENT_READ_INPUT,
            ),
            _write(
                "documents.write",
                "编辑飞书文档",
                frozenset({"docx:document"}),
                _DOCUMENT_WRITE_RESULT,
                input_schema=_DOCUMENT_WRITE_INPUT,
            ),
            _read(
                "drive.search",
                "搜索飞书云空间",
                frozenset({"drive:drive:readonly"}),
                _DRIVE_SEARCH_RESULT,
                input_schema=_DRIVE_SEARCH_INPUT,
            ),
            _write(
                "messages.send",
                "发送飞书消息",
                frozenset({"im:message"}),
                _MESSAGE_SEND_RESULT,
                input_schema=_MESSAGE_SEND_INPUT,
            ),
        ),
        events=(
            ConnectorEventSpec(
                event_id="messages.received",
                display_name="收到飞书消息",
                required_scopes=frozenset({"im:message:readonly"}),
            ),
        ),
        icon_key="feishu",
    )
    tencent_docs = ConnectorDefinition(
        connector_id="tencent-docs",
        contract_version="1.0",
        display_name="腾讯文档",
        description="腾讯文档、表格与在线协作能力",
        tier=ConnectorTier.STABLE,
        auth_kinds=(ConnectorAuthKind.OAUTH2, ConnectorAuthKind.API_TOKEN),
        config_schema=_OBJECT,
        actions=(
            _read(
                "documents.list",
                "列出腾讯文档",
                frozenset({"docs.read"}),
                _DOCUMENT_LIST_RESULT,
                input_schema=_DOCUMENT_LIST_INPUT,
            ),
            _read(
                "documents.read",
                "读取腾讯文档",
                frozenset({"docs.read"}),
                _DOCUMENT_READ_RESULT,
                input_schema=_DOCUMENT_READ_INPUT,
            ),
            _write(
                "documents.write",
                "编辑腾讯文档",
                frozenset({"docs.write"}),
                _DOCUMENT_WRITE_RESULT,
                input_schema=_DOCUMENT_WRITE_INPUT,
            ),
        ),
        icon_key="tencent-docs",
    )
    # Keep every non-Web messaging channel shipped by v0.3 visible through the
    # one v1 Connector contract.  The IDs intentionally match the migration
    # adapter's canonical legacy IDs; otherwise a migrated WeCom/Official
    # Account instance becomes an orphan that can never appear in this catalog.
    beta_channels = (
        ("dingtalk", "钉钉"),
        ("wecom_bot", "企业微信智能机器人"),
        ("wechatcom_app", "企业微信自建应用"),
        ("wechat_kf", "微信客服"),
        ("wechatmp", "微信公众号"),
        ("wechatmp_service", "公众号客服"),
        ("weixin", "微信"),
        ("qq", "QQ"),
        ("telegram", "Telegram"),
        ("slack", "Slack"),
        ("discord", "Discord"),
    )
    beta = tuple(
        ConnectorDefinition(
            connector_id=connector_id,
            contract_version="1.0",
            display_name=display_name,
            description=f"{display_name}消息渠道（Beta）",
            tier=ConnectorTier.BETA,
            auth_kinds=(ConnectorAuthKind.APP_CREDENTIALS,),
            config_schema=_OBJECT,
            actions=(
                _write(
                    "messages.send",
                    f"发送{display_name}消息",
                    frozenset({"messages.send"}),
                    _MESSAGE_SEND_RESULT,
                    input_schema=_MESSAGE_SEND_INPUT,
                ),
            ),
            events=(
                ConnectorEventSpec(
                    event_id="messages.received",
                    display_name=f"收到{display_name}消息",
                    required_scopes=frozenset({"messages.read"}),
                ),
            ),
            icon_key=connector_id,
        )
        for connector_id, display_name in beta_channels
    )
    return (feishu, tencent_docs, *beta)


def builtin_connector_registry(
    adapters: dict[str, ConnectorAdapter] | None = None,
) -> ConnectorRegistry:
    """Build the authoritative connector catalog and attach installed packs.

    Unknown adapter IDs are rejected so a misspelled or stale capability pack
    cannot create an invisible second registry.
    """

    adapters = dict(adapters or {})
    definitions = builtin_connector_definitions()
    known = {definition.connector_id for definition in definitions}
    unknown = set(adapters) - known
    if unknown:
        raise ValueError("adapters reference unknown connectors: " + ", ".join(sorted(unknown)))
    registry = ConnectorRegistry()
    for definition in definitions:
        registry.register(definition, adapters.get(definition.connector_id))
    registry.seal()
    return registry
