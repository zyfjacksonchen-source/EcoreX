"""Deterministic, catalog-driven capability discovery.

Discovery is deliberately separate from invocation authority.  It converts a
bounded query into reviewed semantic facets and exact catalog evidence; it
never changes availability, governance, exposure or approval.  A successful
search returns summaries only.  Runtime grants a deferred schema only after a
separate exact ``tool_describe`` call.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import islice
import re
import unicodedata
from typing import Any, Mapping

from .intent_routing import (
    IntentRoutingPolicy,
    builtin_intent_routing_policy,
    normalize_intent_text,
)
from .models import ToolSpec, normalize_reference, stable_digest
from .models_catalog import (
    MAX_MANAGED_MODELS,
    MAX_MODEL_ALIASES,
    MAX_MODEL_ALIAS_BYTES,
    MAX_MODEL_CAPABILITIES,
    MAX_MODEL_CAPABILITY_BYTES,
    MAX_MODEL_ID_BYTES,
)


MAX_DISCOVERY_QUERY_BYTES = 4096
MAX_DISCOVERY_UNITS = 32
MAX_DISCOVERY_EVIDENCE_BYTES = 256
MAX_DISCOVERY_GROUP_RULES = 16
MAX_DISCOVERY_PHRASE_GROUPS = 4
PROVIDER_SELECTION_POLICY_ID = "ecorex.provider_fairness"
PROVIDER_SELECTION_POLICY_VERSION = "1.0.0"
# After exact-reference matches, at most half of a bounded result is reserved
# for product-reviewed contracts.  The remaining slots are selected by the
# least-represented exact provider identity, preventing one MCP catalog from
# flooding every useful result while retaining reviewed Core visibility.
PROVIDER_REVIEWED_SLOT_NUMERATOR = 1
PROVIDER_REVIEWED_SLOT_DENOMINATOR = 2
_POLICY_ID_RE = re.compile(r"^[a-z][a-z0-9]*(?:\.[a-z][a-z0-9_-]*){1,7}$")
_SEMVER_RE = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)
_BIDI_CONTROLS = frozenset(
    {
        "\u061c",
        "\u200e",
        "\u200f",
        "\u202a",
        "\u202b",
        "\u202c",
        "\u202d",
        "\u202e",
        "\u2066",
        "\u2067",
        "\u2068",
        "\u2069",
    }
)
_DISCOVERY_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "the",
        "please",
        "could",
        "would",
        "can",
        "you",
        "me",
        "us",
        "my",
        "our",
        "some",
        "this",
        "that",
        "to",
        "for",
    }
)


def normalize_discovery_text(value: str) -> str:
    """Normalize a query for whole-word/phrase matching.

    Zero-width format characters and bidi controls are removed before
    matching so copied office text cannot create an invisible alternate
    query.  This function is deterministic and intentionally does not perform
    fuzzy spelling or substring inference.
    """

    if not isinstance(value, str):
        raise ValueError("discovery query must be a string")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError:
        raise ValueError("discovery query must be valid Unicode") from None
    if not 1 <= size <= MAX_DISCOVERY_QUERY_BYTES:
        raise ValueError("discovery query exceeds the product limit")
    normalized = unicodedata.normalize("NFKC", value).casefold()
    normalized = "".join(
        character
        for character in normalized
        if unicodedata.category(character) != "Cf"
        and character not in _BIDI_CONTROLS
    )
    normalized = re.sub(r"[^\w]+", " ", normalized, flags=re.UNICODE)
    # Tool/model aliases use hyphen and underscore interchangeably.  Search
    # phrases use spaces, so all three forms of image-2 share one identity.
    normalized = re.sub(r"[_\s]+", " ", normalized, flags=re.UNICODE).strip()
    if not normalized:
        raise ValueError("discovery query has no searchable content")
    return normalized


def _phrase(value: str) -> str:
    normalized = normalize_intent_text(value)
    # ``normalize_reference`` also canonicalizes underscores used in model
    # aliases; convert the resulting separator to the phrase representation.
    if "_" in normalized:
        normalized = normalize_reference(normalized).replace("-", " ")
    return normalized


def _contains_phrase(
    text: str,
    phrase: str,
    *,
    allow_short_cjk_substring: bool = False,
) -> bool:
    phrase = _phrase(phrase)
    if not phrase:
        return False
    cjk_characters = tuple(
        character for character in phrase if "\u3400" <= character <= "\u9fff"
    )
    if cjk_characters:
        # A two-character substring is not a Chinese word boundary.  In
        # particular, “陌生图像识别” contains the bytes for “生图” but is
        # not an image-generation request.  Short terms may use substring
        # matching only while satisfying separate AND phrase groups.
        if len(cjk_characters) <= 2 and not allow_short_cjk_substring:
            return f" {phrase} " in f" {text} "
        return phrase in text
    return f" {phrase} " in f" {text} "


def _query_units(query: str) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            unit for unit in query.split() if unit not in _DISCOVERY_STOPWORDS
        )
    )[:MAX_DISCOVERY_UNITS]


def _utf8_size(value: str) -> int | None:
    try:
        return len(value.encode("utf-8"))
    except UnicodeEncodeError:
        return None


def _safe_catalog_component(value: object, *, maximum_bytes: int) -> str | None:
    if not isinstance(value, str):
        return None
    size = _utf8_size(value)
    if (
        not value.strip()
        or size is None
        or size > maximum_bytes
        or any(unicodedata.category(character).startswith("C") for character in value)
    ):
        return None
    normalized = _phrase(value)
    normalized_size = _utf8_size(normalized)
    if normalized_size is None or normalized_size > maximum_bytes:
        return None
    return normalized or None


def _truncate_utf8(value: str, maximum_bytes: int) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= maximum_bytes:
        return value
    # Dropping only an incomplete final code point is deterministic and keeps
    # evidence valid UTF-8 without ever exceeding the protocol byte limit.
    return encoded[:maximum_bytes].decode("utf-8", errors="ignore").rstrip()


def _evidence(label: str, value: str) -> str:
    return _truncate_utf8(f"discovery:{label}:{value}", MAX_DISCOVERY_EVIDENCE_BYTES)


@dataclass(frozen=True, slots=True)
class DiscoveryMatch:
    match_class: str
    rank: int
    specificity: int
    matched_facets: tuple[str, ...]
    evidence: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class DiscoveryPolicy:
    policy_id: str
    version: str
    routing_policy_digest: str
    facet_terms: Mapping[str, tuple[str, ...]]
    # Each entry is OR across reviewed rules, AND across a rule's groups and
    # OR within a group.  Keeping this structure prevents a lone verb such as
    # design/设计 from acquiring a trusted media facet.
    facet_phrase_group_rules: Mapping[
        str,
        tuple[tuple[tuple[str, ...], ...], ...],
    ]
    model_capability_facets: Mapping[str, frozenset[str]]

    def __post_init__(self) -> None:
        if not _POLICY_ID_RE.fullmatch(self.policy_id):
            raise ValueError("discovery policy ID is invalid")
        if not _SEMVER_RE.fullmatch(self.version):
            raise ValueError("discovery policy version is invalid")
        if not re.fullmatch(r"[0-9a-f]{64}", self.routing_policy_digest):
            raise ValueError("discovery routing policy digest is invalid")
        if not 1 <= len(self.facet_terms) <= 128:
            raise ValueError("discovery facet lexicon is invalid")
        for facet, terms in self.facet_terms.items():
            if (
                not _POLICY_ID_RE.fullmatch(facet)
                or not isinstance(terms, tuple)
                or not 1 <= len(terms) <= 256
            ):
                raise ValueError("discovery facet lexicon is invalid")
            normalized = tuple(_phrase(term) for term in terms)
            if any(not term or len(term.encode("utf-8")) > 128 for term in normalized):
                raise ValueError("discovery term is invalid")
            if len(set(normalized)) != len(normalized):
                raise ValueError("discovery terms must be unique after normalization")
        if len(self.facet_phrase_group_rules) > 128:
            raise ValueError("discovery phrase group lexicon is invalid")
        for facet, rule_groups in self.facet_phrase_group_rules.items():
            if (
                not _POLICY_ID_RE.fullmatch(facet)
                or not isinstance(rule_groups, tuple)
                or not 1 <= len(rule_groups) <= MAX_DISCOVERY_GROUP_RULES
            ):
                raise ValueError("discovery phrase group lexicon is invalid")
            for groups in rule_groups:
                if (
                    not isinstance(groups, tuple)
                    or not 1 <= len(groups) <= MAX_DISCOVERY_PHRASE_GROUPS
                ):
                    raise ValueError("discovery phrase group rule is invalid")
                for group in groups:
                    if not isinstance(group, tuple) or not 1 <= len(group) <= 256:
                        raise ValueError("discovery phrase group is invalid")
                    normalized = tuple(_phrase(term) for term in group)
                    if any(
                        not term or len(term.encode("utf-8")) > 128
                        for term in normalized
                    ):
                        raise ValueError("discovery phrase group term is invalid")
                    if len(set(normalized)) != len(normalized):
                        raise ValueError(
                            "discovery phrase group terms must be unique after normalization"
                        )
        for capability, facets in self.model_capability_facets.items():
            if (
                not normalize_reference(capability)
                or not isinstance(facets, frozenset)
                or not facets
                or any(not _POLICY_ID_RE.fullmatch(facet) for facet in facets)
            ):
                raise ValueError("model capability discovery mapping is invalid")

    def to_dict(self) -> dict[str, Any]:
        return {
            "policy_id": self.policy_id,
            "version": self.version,
            "routing_policy_digest": self.routing_policy_digest,
            "facet_terms": {
                facet: list(terms)
                for facet, terms in sorted(self.facet_terms.items())
            },
            "facet_phrase_group_rules": {
                facet: [
                    [list(group) for group in groups]
                    for groups in rule_groups
                ]
                for facet, rule_groups in sorted(
                    self.facet_phrase_group_rules.items()
                )
            },
            "model_capability_facets": {
                capability: sorted(facets)
                for capability, facets in sorted(
                    self.model_capability_facets.items()
                )
            },
            "provider_selection": {
                "policy_id": PROVIDER_SELECTION_POLICY_ID,
                "version": PROVIDER_SELECTION_POLICY_VERSION,
                "exact_reference_precedes_fairness": True,
                "reviewed_slot_numerator": PROVIDER_REVIEWED_SLOT_NUMERATOR,
                "reviewed_slot_denominator": PROVIDER_REVIEWED_SLOT_DENOMINATOR,
                "group_identity": ["kind", "provider_id", "revision_id"],
            },
        }

    @property
    def digest(self) -> str:
        return stable_digest(self.to_dict())

    def match(
        self,
        query: str,
        spec: ToolSpec,
        *,
        model_catalog_payload: Mapping[str, Any] | None = None,
    ) -> DiscoveryMatch | None:
        normalized = normalize_discovery_text(query)
        normalized_reference = normalize_reference(query)
        references = {
            normalize_reference(value) for value in (spec.tool_id, *spec.aliases)
        }
        if normalized_reference in references:
            return DiscoveryMatch(
                match_class="exact_reference",
                rank=700,
                specificity=len(normalized_reference),
                matched_facets=(),
                evidence=("discovery:exact_reference",),
            )

        model_facets, model_evidence = self._model_facets(
            normalized,
            model_catalog_payload,
        )
        compatible_model_facets = tuple(sorted(model_facets & spec.routing_facets))
        if compatible_model_facets:
            return DiscoveryMatch(
                match_class="model_alias",
                rank=650,
                specificity=max(len(value) for value in model_evidence),
                matched_facets=compatible_model_facets,
                evidence=tuple(
                    [
                        *(_evidence("model_alias", value) for value in model_evidence),
                        *(
                            _evidence("facet", facet)
                            for facet in compatible_model_facets
                        ),
                    ]
                )[:16],
            )

        matched_terms: list[str] = []
        matched_facets: set[str] = set()
        exact_term = False
        for facet in sorted(spec.routing_facets):
            for term in self.facet_terms.get(facet, ()):
                normalized_term = _phrase(term)
                if normalized == normalized_term or _contains_phrase(normalized, term):
                    matched_terms.append(normalized_term)
                    matched_facets.add(facet)
                    exact_term = exact_term or normalized == normalized_term
            for rule_groups in self.facet_phrase_group_rules.get(facet, ()):
                group_matches = tuple(
                    tuple(
                        _phrase(term)
                        for term in group
                        if _contains_phrase(
                            normalized,
                            term,
                            allow_short_cjk_substring=True,
                        )
                    )
                    for group in rule_groups
                )
                if group_matches and all(group_matches):
                    matched_terms.extend(
                        term for group in group_matches for term in group
                    )
                    matched_facets.add(facet)
        if matched_facets:
            terms = tuple(dict.fromkeys(matched_terms))[:8]
            facets = tuple(sorted(matched_facets))
            return DiscoveryMatch(
                match_class=("reviewed_term_exact" if exact_term else "reviewed_term"),
                rank=600 if exact_term else 550,
                specificity=max(len(term) for term in terms),
                matched_facets=facets,
                evidence=tuple(
                    [
                        *(_evidence("reviewed_term", term) for term in terms),
                        *(_evidence("facet", facet) for facet in facets),
                    ]
                )[:16],
            )

        units = _query_units(normalized)
        if not units:
            return None
        tag_phrases = tuple(_phrase(value) for value in spec.intent_tags)
        tag_units = {
            unit
            for value in tag_phrases
            for unit in _query_units(value)
        }
        display_units = set(_query_units(_phrase(spec.display_name)))
        description_units = set(_query_units(_phrase(spec.description)))
        query_unit_set = set(units)
        # Latin tags already have reliable word units.  Phrase containment is
        # reserved for multi-character CJK tags, where the query normally has
        # no spaces (for example 陌生图像识别 -> 图像识别).  This avoids
        # a generic tag such as "image" outranking "inspect image".
        tag_phrase_match = any(
            any("\u3400" <= character <= "\u9fff" for character in phrase)
            and _contains_phrase(normalized, phrase)
            for phrase in tag_phrases
        )
        for match_class, rank, field_units, phrase_match in (
            ("provider_tag", 400, tag_units, tag_phrase_match),
            ("display_name", 350, display_units, False),
            ("description", 200, description_units, False),
        ):
            if query_unit_set <= field_units or phrase_match:
                return DiscoveryMatch(
                    match_class=match_class,
                    rank=rank,
                    specificity=sum(len(unit) for unit in units),
                    matched_facets=(),
                    evidence=(
                        f"discovery:{match_class}:"
                        f"{'tag_phrase' if phrase_match else 'whole_units'}",
                    ),
                )
        return None

    def _model_facets(
        self,
        normalized_query: str,
        payload: Mapping[str, Any] | None,
    ) -> tuple[frozenset[str], tuple[str, ...]]:
        if not isinstance(payload, Mapping):
            return frozenset(), ()
        modalities = payload.get("modalities")
        if not isinstance(modalities, Mapping):
            return frozenset(), ()
        seen: set[str] = set()
        facets: set[str] = set()
        evidence: list[str] = []
        for raw_models in islice(modalities.values(), 8):
            if not isinstance(raw_models, list):
                continue
            for raw in islice(raw_models, MAX_MANAGED_MODELS):
                if not isinstance(raw, Mapping):
                    continue
                model_id = _safe_catalog_component(
                    raw.get("model_id"),
                    maximum_bytes=MAX_MODEL_ID_BYTES,
                )
                if model_id is None:
                    continue
                canonical_model_id = normalize_reference(model_id)
                if canonical_model_id in seen:
                    continue
                seen.add(canonical_model_id)
                aliases = raw.get("aliases")
                references = [model_id]
                if isinstance(aliases, list):
                    references.extend(
                        normalized
                        for value in islice(aliases, MAX_MODEL_ALIASES)
                        if (
                            normalized := _safe_catalog_component(
                                value,
                                maximum_bytes=MAX_MODEL_ALIAS_BYTES,
                            )
                        )
                        is not None
                    )
                matched = tuple(
                    reference
                    for reference in dict.fromkeys(references)
                    if _contains_phrase(normalized_query, reference)
                )
                if not matched:
                    continue
                capabilities = raw.get("capabilities")
                if isinstance(capabilities, list):
                    for capability in islice(capabilities, MAX_MODEL_CAPABILITIES):
                        normalized_capability = _safe_catalog_component(
                            capability,
                            maximum_bytes=MAX_MODEL_CAPABILITY_BYTES,
                        )
                        if normalized_capability is not None:
                            facets.update(
                                self.model_capability_facets.get(
                                    normalize_reference(normalized_capability),
                                    frozenset(),
                                )
                            )
                evidence.extend(matched)
        return frozenset(facets), tuple(dict.fromkeys(evidence))[:8]


def builtin_discovery_policy(
    routing_policy: IntentRoutingPolicy | None = None,
) -> DiscoveryPolicy:
    """Build the reviewed search lexicon from routing facets, not tool IDs."""

    routing = routing_policy or builtin_intent_routing_policy()
    facet_terms: dict[str, list[str]] = {}
    facet_phrase_group_rules: dict[
        str,
        list[tuple[tuple[str, ...], ...]],
    ] = {}
    for rule in routing.rules:
        for facet in rule.required_facets_any:
            facet_terms.setdefault(facet, []).extend(rule.positive_phrases)
            if rule.required_phrase_groups:
                facet_phrase_group_rules.setdefault(facet, []).append(
                    rule.required_phrase_groups
                )

    # Fine-grained edit facets are searchable and replaceable independently of
    # the current image tool.  They are deliberately catalog metadata, not a
    # branch on ``imagegen``.
    facet_terms.update(
        {
            "media.image.edit.retouch": [
                "retouch",
                "photo retouching",
                "精修",
                "修图",
                "修圖",
            ],
            "media.image.edit.background_remove": [
                "background removal",
                "remove background",
                "remove the background",
                "去背景",
                "抠图",
                "去背",
                "摳圖",
            ],
        }
    )
    canonical_terms = {
        facet: tuple(
            dict.fromkeys(
                term
                for value in values
                if (term := _phrase(value))
            )
        )
        for facet, values in facet_terms.items()
    }
    return DiscoveryPolicy(
        policy_id="ecorex.discovery",
        version="1.2.0",
        routing_policy_digest=routing.digest,
        facet_terms=canonical_terms,
        facet_phrase_group_rules={
            facet: tuple(
                tuple(
                    tuple(_phrase(term) for term in group)
                    for group in groups
                )
                for groups in rules
            )
            for facet, rules in facet_phrase_group_rules.items()
        },
        model_capability_facets={
            "image-generation": frozenset({"media.image.create"}),
            "image-edit": frozenset({"media.image.edit"}),
        },
    )


__all__ = [
    "DiscoveryMatch",
    "DiscoveryPolicy",
    "MAX_DISCOVERY_QUERY_BYTES",
    "MAX_DISCOVERY_UNITS",
    "PROVIDER_REVIEWED_SLOT_DENOMINATOR",
    "PROVIDER_REVIEWED_SLOT_NUMERATOR",
    "PROVIDER_SELECTION_POLICY_ID",
    "PROVIDER_SELECTION_POLICY_VERSION",
    "builtin_discovery_policy",
    "normalize_discovery_text",
]
