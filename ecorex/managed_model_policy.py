"""Authoritative v1 policy for EcoreX-managed model identities.

The public Runtime model ID is intentionally stable across upstream model
upgrades.  Both the local catalog and the cloud Model Gateway import this
module so an environment mapping cannot silently change the provider model or
drop the reasoning/context policy while keeping the same public identity.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Literal


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
    reasoning_effort: Literal["medium"]
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
            or self.reasoning_effort != "medium"
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
    policy_id="ecorex-chat-gpt-5.6-sol",
    policy_version="1.0.0",
    # Keep this public identity stable so v0.3.0 data and existing clients do
    # not need a model-ID rewrite when the managed provider model changes.
    local_model_id="ecorex-chat",
    upstream_model_id="gpt-5.6-sol",
    display_name="GPT-5.6 SOL · 中等推理",
    aliases=("chat", "default", "gpt-5.6-sol", "gpt5.6-sol"),
    reasoning_effort="medium",
    context_management_type="compaction",
    compact_threshold_tokens=272_000,
)


def require_managed_chat_mapping(mapping: dict[str, str]) -> None:
    """Fail closed unless the stable local model maps to its signed policy."""

    expected = ECOREX_CHAT_MODEL_POLICY
    if mapping != {expected.local_model_id: expected.upstream_model_id}:
        raise ValueError("ecorex-chat upstream model mapping violates managed policy")
