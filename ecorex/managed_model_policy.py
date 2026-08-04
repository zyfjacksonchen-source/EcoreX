"""Authoritative v1 policy for EcoreX-managed model identities.

The public Runtime model ID is intentionally stable across upstream model
upgrades.  Both the local catalog and the cloud Model Gateway import this
module so an environment mapping cannot silently change the provider model or
drop the reasoning/context policy while keeping the same public identity.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from types import MappingProxyType
from typing import Literal, Mapping


_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SEMVER = re.compile(r"^(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)$")


@dataclass(frozen=True, slots=True)
class ManagedChatModelPolicy:
    """Immutable provider execution policy behind one local chat model."""

    schema_version: Literal[1]
    policy_id: str
    policy_version: str
    local_model_id: str
    upstream_model_id: str
    display_name: str
    aliases: tuple[str, ...]
    reasoning_effort: Literal["medium", "high"]
    context_management_type: Literal["compaction"]
    compact_threshold_tokens: int

    def __post_init__(self) -> None:
        if (
            self.schema_version != 1
            or _SAFE_ID.fullmatch(self.policy_id) is None
            or _SEMVER.fullmatch(self.policy_version) is None
            or _SAFE_ID.fullmatch(self.local_model_id) is None
            or _SAFE_ID.fullmatch(self.upstream_model_id) is None
            or not self.display_name.strip()
            or len(self.display_name.encode("utf-8")) > 256
            or not self.aliases
            or len(self.aliases) > 32
            or any(_SAFE_ID.fullmatch(alias) is None for alias in self.aliases)
            or len(set(alias.casefold() for alias in self.aliases)) != len(self.aliases)
            or self.reasoning_effort not in {"medium", "high"}
            or self.context_management_type != "compaction"
            or isinstance(self.compact_threshold_tokens, bool)
            or not 1_000 <= self.compact_threshold_tokens <= 2_000_000
        ):
            raise ValueError("managed chat model policy is invalid")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "policy_id": self.policy_id,
            "policy_version": self.policy_version,
            "local_model_id": self.local_model_id,
            "upstream_model_id": self.upstream_model_id,
            "reasoning_effort": self.reasoning_effort,
            "context_management": {
                "type": self.context_management_type,
                "compact_threshold_tokens": self.compact_threshold_tokens,
            },
        }


ECOREX_CHAT_MODEL_POLICY = ManagedChatModelPolicy(
    schema_version=1,
    policy_id="ecorex-chat-gpt-5.6-luna",
    policy_version="1.1.0",
    # Keep this public identity stable so v0.3.0 data and existing clients do
    # not need a model-ID rewrite when the managed provider model changes.
    local_model_id="ecorex-chat",
    upstream_model_id="gpt-5.6-luna",
    display_name="GPT-5.6 Luna · 高推理",
    aliases=("chat", "default", "gpt-5.6-luna", "gpt5.6-luna"),
    reasoning_effort="high",
    context_management_type="compaction",
    compact_threshold_tokens=272_000,
)

ECOREX_SOL_MODEL_POLICY = ManagedChatModelPolicy(
    schema_version=1,
    policy_id="ecorex-gpt-5.6-sol",
    policy_version="1.0.0",
    local_model_id="ecorex-gpt-5.6-sol",
    upstream_model_id="gpt-5.6-sol",
    display_name="GPT-5.6 Sol · 中推理",
    aliases=("sol", "gpt-5.6-sol", "gpt5.6-sol"),
    reasoning_effort="medium",
    context_management_type="compaction",
    compact_threshold_tokens=272_000,
)

ECOREX_DEEPSEEK_MODEL_POLICY = ManagedChatModelPolicy(
    schema_version=1,
    policy_id="ecorex-deepseek-v4-pro",
    policy_version="1.0.0",
    local_model_id="ecorex-deepseek-v4-pro",
    upstream_model_id="deepseek-v4-pro",
    display_name="DeepSeek V4 Pro",
    aliases=("deepseek", "deepseek-v4-pro"),
    reasoning_effort="medium",
    context_management_type="compaction",
    compact_threshold_tokens=900_000,
)

ECOREX_GEMINI_MODEL_POLICY = ManagedChatModelPolicy(
    schema_version=1,
    policy_id="ecorex-gemini-3.1-pro-preview",
    policy_version="1.0.0",
    local_model_id="ecorex-gemini-3.1-pro",
    upstream_model_id="gemini-3.1-pro-preview",
    display_name="Gemini 3.1 Pro",
    aliases=("gemini", "gemini-3.1-pro", "gemini-3.1-pro-preview"),
    reasoning_effort="medium",
    context_management_type="compaction",
    compact_threshold_tokens=900_000,
)

ECOREX_DOUBAO_MODEL_POLICY = ManagedChatModelPolicy(
    schema_version=1,
    policy_id="ecorex-doubao-seed-2.0-pro",
    policy_version="1.0.0",
    local_model_id="ecorex-doubao-seed-2.0-pro",
    upstream_model_id="doubao-seed-2-0-pro-260215",
    display_name="豆包 Seed 2.0 Pro",
    aliases=("doubao", "doubao-seed-2.0-pro", "doubao-seed-2-0-pro-260215"),
    reasoning_effort="medium",
    context_management_type="compaction",
    compact_threshold_tokens=224_000,
)

ECOREX_CHAT_MODEL_POLICIES = (
    ECOREX_CHAT_MODEL_POLICY,
    ECOREX_SOL_MODEL_POLICY,
    ECOREX_DEEPSEEK_MODEL_POLICY,
    ECOREX_GEMINI_MODEL_POLICY,
    ECOREX_DOUBAO_MODEL_POLICY,
)

MANAGED_CHAT_MODEL_POLICIES = MappingProxyType(
    {policy.local_model_id: policy for policy in ECOREX_CHAT_MODEL_POLICIES}
)


def managed_chat_model_policy(local_model_id: str) -> ManagedChatModelPolicy:
    try:
        return MANAGED_CHAT_MODEL_POLICIES[local_model_id]
    except KeyError:
        raise ValueError("managed chat model policy is unavailable") from None


def require_managed_chat_mapping(mapping: Mapping[str, str]) -> None:
    """Fail closed unless every configured route matches a known policy.

    A non-empty subset is allowed so the cloud can stage model rollout without
    teaching the local Runtime any provider URL or credential.
    """

    configured = dict(mapping)
    if not configured or any(
        local_model_id not in MANAGED_CHAT_MODEL_POLICIES
        or MANAGED_CHAT_MODEL_POLICIES[local_model_id].upstream_model_id
        != upstream_model_id
        for local_model_id, upstream_model_id in configured.items()
    ):
        raise ValueError("managed chat model mapping violates managed policy")
