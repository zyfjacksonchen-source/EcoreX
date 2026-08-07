"""Product-owned, versioned intent routing hints.

The planner consumes this policy generically.  It does not know any concrete
tool identity or media vocabulary.  Free-form MCP metadata cannot create one
of these rules or attach a trusted routing facet; only reviewed Core/catalog
composition does so.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
import unicodedata

from .models import CapabilityEffect, Exposure, ToolSpec, stable_digest


_POLICY_ID_RE = re.compile(r"^[a-z][a-z0-9_-]*(?:\.[a-z][a-z0-9_-]*){1,7}$")
_SEMVER_RE = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)

MAX_ROUTING_RULES = 16
MAX_ROUTING_PHRASES = 64
MAX_ROUTING_PHRASE_GROUPS = 4
MAX_ROUTING_PHRASE_BYTES = 128
MAX_ROUTING_SCORE_BOOST = 2_000
MAX_EVIDENCE_TERMS_PER_RULE = 8
MAX_ROUTING_INTENT_BYTES = 64 * 1024
MAX_PHRASE_OCCURRENCES = 16
MAX_ROUTING_CLAUSES = 32

_IMAGE_CONTEXT_FOLLOWUPS = (
    "再来一张",
    "再生成一张",
    "再做一张",
    "再画一张",
    "换一个版本",
    "换个版本",
    "再出一个版本",
    "another one",
    "one more",
    "another version",
    "make another",
    "generate another",
)
_IMAGE_CONTEXT_NEGATIONS = (
    "不要",
    "不用",
    "别",
    "停止",
    "取消",
    "do not",
    "don't",
    "dont",
    "not",
    "stop",
    "cancel",
)

_CLAUSE_BREAK_RE = re.compile(
    r"[\r\n,，。!！？?；;]+"
    r"|\.(?![A-Za-z0-9])"
    r"|\b(?:but|however|instead|then)\b"
    r"|(?:但是|不过|然后|转而)",
    flags=re.IGNORECASE,
)


def normalize_intent_text(value: str) -> str:
    """Normalize prose for bounded phrase matching, without fuzzy inference."""

    normalized = unicodedata.normalize("NFKC", str(value or "")).casefold()
    # Copy/paste from office documents and IM clients can insert zero-width
    # format characters inside a word.  They carry no routing meaning and must
    # not let a negative phrase evade matching (for example ``不要生\u200b图``).
    normalized = "".join(
        character
        for character in normalized
        if unicodedata.category(character) != "Cf"
    )
    return re.sub(r"[^\w]+", " ", normalized, flags=re.UNICODE).strip()


def normalize_intent_clauses(value: str) -> tuple[str, ...]:
    """Return bounded ordered clauses for last-explicit-intent precedence."""

    normalized = unicodedata.normalize("NFKC", str(value or "")).casefold()
    normalized = "".join(
        character
        for character in normalized
        if unicodedata.category(character) != "Cf"
    )
    clauses = tuple(
        clause
        for part in _CLAUSE_BREAK_RE.split(
            normalized,
            maxsplit=MAX_ROUTING_CLAUSES - 1,
        )
        if (clause := normalize_intent_text(part))
    )
    return clauses or (normalize_intent_text(normalized),)


def _utf8_size(value: str) -> int | None:
    try:
        return len(value.encode("utf-8"))
    except UnicodeEncodeError:
        return None


def intent_is_routable(value: object) -> bool:
    """Return whether an intent is safe to normalize inside the router."""

    if not isinstance(value, str):
        return False
    size = _utf8_size(value)
    return size is not None and size <= MAX_ROUTING_INTENT_BYTES


def intent_inherits_image_context(value: object) -> bool:
    """Return whether short prose should inherit a verified image route.

    These phrases never route on their own. Callers must also have an attached
    image, a successful image tool call, or an earlier reviewed image intent.
    """

    if not intent_is_routable(value):
        return False
    normalized = normalize_intent_text(value)
    if any(
        _context_starts_with(normalized, prefix)
        for prefix in _IMAGE_CONTEXT_NEGATIONS
    ):
        return False
    return any(
        _contains_phrase(normalized, phrase) for phrase in _IMAGE_CONTEXT_FOLLOWUPS
    )


def _contains_phrase(normalized_intent: str, phrase: str) -> bool:
    normalized_phrase = normalize_intent_text(phrase)
    if not normalized_phrase:
        return False
    # CJK phrases have no reliable whitespace boundary.  Product-owned phrase
    # lists therefore use exact normalized substrings.  Latin/digit phrases
    # use padded word boundaries so e.g. ``edit`` cannot match ``credit``.
    if any("\u3400" <= character <= "\u9fff" for character in normalized_phrase):
        return normalized_phrase in normalized_intent
    return f" {normalized_phrase} " in f" {normalized_intent} "


_SUPPRESSION_CANCELLERS = (
    "不要",
    "不",
    "别",
    "別",
    "并非",
    "不是",
    "not",
    "do not",
    "don't",
    "dont",
)


def _active_suppression(normalized_intent: str, phrase: str) -> bool:
    """Return true only when a reviewed suppression is not itself negated."""

    for start, _end in _phrase_spans(normalized_intent, phrase):
        before = normalized_intent[:start]
        if any(_context_ends_with(before, value) for value in _SUPPRESSION_CANCELLERS):
            continue
        return True
    return False


def _phrase_spans(normalized_intent: str, phrase: str) -> tuple[tuple[int, int], ...]:
    """Return bounded normalized spans for one reviewed phrase."""

    normalized_phrase = normalize_intent_text(phrase)
    if not normalized_phrase:
        return ()
    if any("\u3400" <= character <= "\u9fff" for character in normalized_phrase):
        spans: list[tuple[int, int]] = []
        offset = 0
        while len(spans) < MAX_PHRASE_OCCURRENCES:
            start = normalized_intent.find(normalized_phrase, offset)
            if start < 0:
                break
            end = start + len(normalized_phrase)
            spans.append((start, end))
            offset = end
        return tuple(spans)
    return tuple(
        match.span()
        for match in list(
            re.finditer(
                rf"(?<!\w){re.escape(normalized_phrase)}(?!\w)",
                normalized_intent,
                flags=re.UNICODE,
            )
        )[:MAX_PHRASE_OCCURRENCES]
    )


def _context_starts_with(value: str, phrase: str) -> bool:
    normalized = normalize_intent_text(phrase)
    candidate = value.lstrip()
    if not normalized:
        return False
    if any("\u3400" <= character <= "\u9fff" for character in normalized):
        return candidate.startswith(normalized)
    return re.match(rf"{re.escape(normalized)}(?:\b|$)", candidate) is not None


def _context_ends_with(value: str, phrase: str) -> bool:
    normalized = normalize_intent_text(phrase)
    candidate = value.rstrip()
    if not normalized:
        return False
    if any("\u3400" <= character <= "\u9fff" for character in normalized):
        return candidate.endswith(normalized)
    return re.search(rf"(?:^|\b){re.escape(normalized)}$", candidate) is not None


def _match_with_local_context(
    normalized_intent: str,
    phrase: str,
    *,
    blocked_prefixes: tuple[str, ...],
    blocked_suffixes: tuple[str, ...],
    peer_spans: tuple[tuple[int, int, int], ...] = (),
    phrase_spans: tuple[tuple[int, int], ...] | None = None,
) -> tuple[bool, tuple[str, ...]]:
    """Accept a phrase when at least one occurrence is not locally blocked.

    Local blockers distinguish a deliverable request from prose that merely
    names a feature or text output. For example, ``生成图片并写图片说明`` keeps
    the first occurrence, while ``生成图片说明`` blocks its only occurrence.
    """

    normalized_phrase = normalize_intent_text(phrase)
    spans = tuple(
        (start, end)
        for start, end in (
            _phrase_spans(normalized_intent, phrase)
            if phrase_spans is None
            else phrase_spans
        )
        if not any(
            peer_length > len(normalized_phrase)
            and peer_start <= start
            and peer_end >= end
            for peer_start, peer_end, peer_length in peer_spans
        )
    )
    if not spans:
        return False, ()
    blockers: list[str] = []
    for start, end in spans:
        prefix = next(
            (
                item
                for item in blocked_prefixes
                if _context_ends_with(normalized_intent[:start], item)
            ),
            None,
        )
        suffix = next(
            (
                item
                for item in blocked_suffixes
                if _context_starts_with(normalized_intent[end:], item)
            ),
            None,
        )
        if prefix is None and suffix is None:
            return True, ()
        if prefix is not None:
            blockers.append(f"prefix:{normalize_intent_text(prefix)}")
        if suffix is not None:
            blockers.append(f"suffix:{normalize_intent_text(suffix)}")
    return False, tuple(dict.fromkeys(blockers))[:MAX_EVIDENCE_TERMS_PER_RULE]


def _validated_phrases(
    phrases: tuple[str, ...],
    *,
    label: str,
    require_non_empty: bool,
) -> tuple[str, ...]:
    if not isinstance(phrases, tuple) or any(
        not isinstance(phrase, str) for phrase in phrases
    ):
        raise ValueError(f"{label} must be a tuple of strings")
    if require_non_empty and not phrases:
        raise ValueError(f"{label} must not be empty")
    if len(phrases) > MAX_ROUTING_PHRASES:
        raise ValueError(f"{label} exceeds the product metadata limit")
    normalized = tuple(normalize_intent_text(phrase) for phrase in phrases)
    if any(
        not phrase
        or (size := _utf8_size(source)) is None
        or size > MAX_ROUTING_PHRASE_BYTES
        for source, phrase in zip(phrases, normalized, strict=True)
    ):
        raise ValueError(f"{label} contains an invalid phrase")
    if len(set(normalized)) != len(normalized):
        raise ValueError(f"{label} must be unique after normalization")
    return normalized


def _validated_phrase_groups(
    groups: tuple[tuple[str, ...], ...],
) -> tuple[tuple[str, ...], ...]:
    if (
        not isinstance(groups, tuple)
        or len(groups) > MAX_ROUTING_PHRASE_GROUPS
        or any(not isinstance(group, tuple) for group in groups)
    ):
        raise ValueError("intent routing phrase groups are invalid")
    validated = tuple(
        _validated_phrases(
            group,
            label="intent routing phrase group",
            require_non_empty=True,
        )
        for group in groups
    )
    if sum(len(group) for group in validated) > MAX_ROUTING_PHRASES:
        raise ValueError("intent routing phrase groups exceed the product metadata limit")
    return validated


@dataclass(frozen=True, slots=True)
class IntentRoutingRule:
    rule_id: str
    version: str
    required_facets_any: frozenset[str]
    required_effects: frozenset[CapabilityEffect]
    positive_phrases: tuple[str, ...]
    # Each group is OR internally and the groups are ANDed.  This permits
    # reviewed, tool-agnostic evidence such as (create verb) AND (media
    # deliverable) without promoting a capability for a bare noun.
    required_phrase_groups: tuple[tuple[str, ...], ...] = ()
    blocked_prefixes: tuple[str, ...] = ()
    blocked_suffixes: tuple[str, ...] = ()
    suppression_phrases: tuple[str, ...] = ()
    score_boost: int = 1_200
    # Ranking is the safe product default.  A reviewed policy that truly needs
    # direct exposure must opt in explicitly; adding a new semantic rule can
    # never become an invocation shortcut merely because an author omitted
    # this field.
    promote_to: Exposure | None = None

    def __post_init__(self) -> None:
        if not _POLICY_ID_RE.fullmatch(self.rule_id):
            raise ValueError("intent routing rule_id is invalid")
        if not _SEMVER_RE.fullmatch(self.version):
            raise ValueError("intent routing rule version is invalid")
        if (
            not isinstance(self.required_facets_any, frozenset)
            or not 1 <= len(self.required_facets_any) <= 8
        ):
            raise ValueError("intent routing rule facets are invalid")
        if any(
            not _POLICY_ID_RE.fullmatch(facet)
            or (size := _utf8_size(facet)) is None
            or size > MAX_ROUTING_PHRASE_BYTES
            for facet in self.required_facets_any
        ):
            raise ValueError("intent routing rule facet is invalid")
        if (
            not isinstance(self.required_effects, frozenset)
            or not self.required_effects
            or len(self.required_effects) > len(CapabilityEffect)
            or any(
                not isinstance(effect, CapabilityEffect)
                for effect in self.required_effects
            )
        ):
            raise ValueError("intent routing rule effects are invalid")
        phrase_groups = _validated_phrase_groups(self.required_phrase_groups)
        _validated_phrases(
            self.positive_phrases,
            label="intent routing positive phrases",
            require_non_empty=not phrase_groups,
        )
        _validated_phrases(
            self.suppression_phrases,
            label="intent routing suppression phrases",
            require_non_empty=False,
        )
        _validated_phrases(
            self.blocked_prefixes,
            label="intent routing blocked prefixes",
            require_non_empty=False,
        )
        _validated_phrases(
            self.blocked_suffixes,
            label="intent routing blocked suffixes",
            require_non_empty=False,
        )
        if (
            isinstance(self.score_boost, bool)
            or not isinstance(self.score_boost, int)
            or not 1 <= self.score_boost <= MAX_ROUTING_SCORE_BOOST
        ):
            raise ValueError("intent routing score boost is outside product bounds")
        if self.promote_to not in {None, Exposure.DIRECT}:
            raise ValueError("intent routing rules may only rank or promote to direct exposure")

    def to_dict(self) -> dict[str, object]:
        return {
            "rule_id": self.rule_id,
            "version": self.version,
            "required_facets_any": sorted(self.required_facets_any),
            "required_effects": sorted(effect.value for effect in self.required_effects),
            "positive_phrases": list(self.positive_phrases),
            "required_phrase_groups": [
                list(group) for group in self.required_phrase_groups
            ],
            "blocked_prefixes": list(self.blocked_prefixes),
            "blocked_suffixes": list(self.blocked_suffixes),
            "suppression_phrases": list(self.suppression_phrases),
            "score_boost": self.score_boost,
            "promote_to": self.promote_to.value if self.promote_to is not None else None,
        }


@dataclass(frozen=True, slots=True)
class IntentRouteEvidence:
    rule_id: str
    rule_version: str
    matched_terms: tuple[str, ...]
    suppressed_by: tuple[str, ...]
    score_delta: int
    promote_to: Exposure | None

    @property
    def matched(self) -> bool:
        return bool(self.matched_terms) and not self.suppressed_by


@dataclass(frozen=True, slots=True)
class IntentRoutingPolicy:
    policy_id: str
    version: str
    rules: tuple[IntentRoutingRule, ...]

    def __post_init__(self) -> None:
        if not _POLICY_ID_RE.fullmatch(self.policy_id):
            raise ValueError("intent routing policy_id is invalid")
        if not _SEMVER_RE.fullmatch(self.version):
            raise ValueError("intent routing policy version is invalid")
        if (
            not isinstance(self.rules, tuple)
            or not 1 <= len(self.rules) <= MAX_ROUTING_RULES
            or any(not isinstance(rule, IntentRoutingRule) for rule in self.rules)
        ):
            raise ValueError("intent routing policy rule count is invalid")
        identities = {(rule.rule_id, rule.version) for rule in self.rules}
        if len(identities) != len(self.rules):
            raise ValueError("intent routing rules must have unique identities")

    @property
    def digest(self) -> str:
        return stable_digest(self.to_dict())

    def to_dict(self) -> dict[str, object]:
        return {
            "policy_id": self.policy_id,
            "version": self.version,
            "rules": [rule.to_dict() for rule in self.rules],
        }

    def evaluate(self, intent: str, spec: ToolSpec) -> tuple[IntentRouteEvidence, ...]:
        if not intent_is_routable(intent):
            return ()
        normalized_clauses = normalize_intent_clauses(intent)
        evidence: list[IntentRouteEvidence] = []
        for rule in self.rules:
            if not (spec.routing_facets & rule.required_facets_any):
                continue
            if not rule.required_effects.issubset(spec.effects):
                continue
            routing_phrases = tuple(
                dict.fromkeys(
                    (
                        *rule.positive_phrases,
                        *(
                            phrase
                            for group in rule.required_phrase_groups
                            for phrase in group
                        ),
                    )
                )
            )
            matched: tuple[str, ...] = ()
            suppressed: tuple[str, ...] = ()
            for normalized_clause in normalized_clauses:
                local_blockers: list[str] = []
                routing_spans_by_phrase = {
                    phrase: _phrase_spans(normalized_clause, phrase)
                    for phrase in routing_phrases
                }
                routing_spans = tuple(
                    (start, end, len(normalize_intent_text(phrase)))
                    for phrase in routing_phrases
                    for start, end in routing_spans_by_phrase[phrase]
                )

                def accepted_phrase(phrase: str) -> bool:
                    accepted, blockers = _match_with_local_context(
                        normalized_clause,
                        phrase,
                        blocked_prefixes=rule.blocked_prefixes,
                        blocked_suffixes=rule.blocked_suffixes,
                        peer_spans=routing_spans,
                        phrase_spans=routing_spans_by_phrase[phrase],
                    )
                    local_blockers.extend(blockers)
                    return accepted

                exact_matches = tuple(
                    phrase
                    for phrase in rule.positive_phrases
                    if accepted_phrase(phrase)
                )
                group_matches = tuple(
                    tuple(phrase for phrase in group if accepted_phrase(phrase))
                    for group in rule.required_phrase_groups
                )
                groups_satisfied = bool(group_matches) and all(group_matches)
                clause_matched = (
                    tuple(
                        dict.fromkeys(
                            (
                                *exact_matches,
                                *(
                                    phrase
                                    for group in group_matches
                                    for phrase in group
                                ),
                            )
                        )
                    )[:MAX_EVIDENCE_TERMS_PER_RULE]
                    if exact_matches or groups_satisfied
                    else ()
                )
                global_suppressions = tuple(
                    phrase
                    for phrase in rule.suppression_phrases
                    if _active_suppression(normalized_clause, phrase)
                )[:MAX_EVIDENCE_TERMS_PER_RULE]
                clause_suppressed = global_suppressions or (
                    tuple(dict.fromkeys(local_blockers))[
                        :MAX_EVIDENCE_TERMS_PER_RULE
                    ]
                    if not clause_matched
                    else ()
                )
                if not clause_matched and not clause_suppressed:
                    continue
                # The latest explicit clause wins. This handles corrections
                # such as "generation failed; generate a new image" without
                # letting an earlier request override a final "only analyze".
                matched = clause_matched
                suppressed = clause_suppressed
            if not matched and not suppressed:
                continue
            accepted = bool(matched) and not suppressed
            evidence.append(
                IntentRouteEvidence(
                    rule_id=rule.rule_id,
                    rule_version=rule.version,
                    matched_terms=matched,
                    suppressed_by=suppressed,
                    score_delta=rule.score_boost if accepted else 0,
                    promote_to=rule.promote_to if accepted else None,
                )
            )
        return tuple(evidence)


def builtin_intent_routing_policy() -> IntentRoutingPolicy:
    """Return the reviewed v1 policy; changing it changes every plan digest."""

    meta_prefixes = (
        "修复",
        "排查",
        "调试",
        "优化",
        "评估",
        "分析",
        "解释",
        "review",
        "audit",
        "debug",
        "fix",
        "optimize",
        "优化一下",
        "分析一下",
        "explain",
    )
    meta_suffixes = (
        "说明",
        "描述",
        "链接",
        "列表",
        "按钮",
        "入口",
        "功能",
        "接口",
        "路由",
        "方案",
        "架构",
        "价格",
        "模型",
        "性能",
        "延迟",
        "故障",
        "失败",
        "教程",
        "指南",
        "规范",
        "提示词",
        "介绍",
        "概述",
        "表展示",
        "表说明",
        "表分析",
        "caption",
        "captions",
        "description",
        "descriptions",
        "link",
        "links",
        "list",
        "button",
        "entry point",
        "feature",
        "api",
        "routing",
        "workflow",
        "architecture",
        "pricing",
        "model",
        "performance",
        "latency",
        "failure",
        "guide",
        "documentation",
        "prompt",
        "introduction",
        "overview",
    )
    shared_suppressions = (
        "只分析",
        "仅分析",
        "只看图",
        "only analyze",
        "just analyze",
        "only inspect",
        "imagegen failed",
        "imagegen is broken",
        "imagegen故障",
    )
    create_discussion_suppressions = (
        "生图失败",
        "图片生成失败",
        "image generation failed",
        "debug image generation",
        "生图意图",
        "生图路由",
        "生图的路由",
        "生图方案",
        "图片生成架构",
        "图片生成价格",
        "图片生成模型",
        "image generation architecture",
        "image generation routing",
        "image generation pricing",
        "how image generation works",
        "how can i generate an image",
        "how do i generate an image",
        "how to generate an image",
        "如何生成图片",
        "怎么生成图片",
        "图片生成很慢",
        "生图很慢",
        "生图这么慢",
        "为什么生图",
        "生图为什么",
        "生图延迟",
        "图片生成性能",
        "image generation is slow",
        "image generation is too slow",
        "image generation latency",
        "生圖失敗",
        "圖片生成失敗",
        "生圖方案",
        "圖片生成架構",
    )
    edit_discussion_suppressions = (
        "修图失败",
        "改图失败",
        "image editing failed",
        "retouch failed",
        "debug image editing",
        "修图意图",
        "修图方案",
        "修图架构",
        "精修功能",
        "抠图功能",
        "抠图方案",
        "抠图算法",
        "换背景方案",
        "背景替换方案",
        "image editing architecture",
        "image editing workflow",
        "background removal workflow",
        "background removal architecture",
        "how to edit an image",
    )
    return IntentRoutingPolicy(
        policy_id="ecorex.intent-routing",
        version="1.6.0",
        rules=(
            IntentRoutingRule(
                rule_id="media.image.create",
                version="1.6.0",
                required_facets_any=frozenset({"media.image.create"}),
                required_effects=frozenset({CapabilityEffect.GENERATE_MEDIA}),
                positive_phrases=(
                    "generate image",
                    "generate an image",
                    "generate a new image",
                    "image generation",
                    "create image",
                    "create an image",
                    "make image",
                    "make an image",
                    "draw image",
                    "draw an image",
                    "text to image",
                    "生图",
                    "生成图",
                    "生成图片",
                    "生成圖片",
                    "生成一张图",
                    "生成一张图片",
                    "生成一张新图",
                    "生成一张新图片",
                    "图片生成",
                    "画一张图",
                    "画一张图片",
                    "画图",
                    "绘图",
                    "绘制图片",
                    "畫一張圖",
                    "創作圖片",
                    "製作圖片",
                    "创建图片",
                    "制作图片",
                    "做一张图",
                    "出图",
                ),
                blocked_prefixes=meta_prefixes,
                blocked_suffixes=meta_suffixes,
                suppression_phrases=(
                    "不要生成",
                    "不生成图片",
                    "别生成",
                    "无需生成",
                    "不要生图",
                    "别生图",
                    "不要生成圖片",
                    "別生圖",
                    "无法生成图片",
                    "do not generate",
                    "don't generate",
                    "no image generation",
                    "without generating",
                    "cannot generate",
                    "can't generate",
                    *shared_suppressions,
                    *create_discussion_suppressions,
                ),
                score_boost=1_200,
                promote_to=Exposure.DIRECT,
            ),
            IntentRoutingRule(
                rule_id="media.image.deliverable",
                version="1.6.0",
                required_facets_any=frozenset({"media.image.create"}),
                required_effects=frozenset({CapabilityEffect.GENERATE_MEDIA}),
                positive_phrases=(),
                required_phrase_groups=(
                    (
                        "帮我做",
                        "做一张",
                        "做个",
                        "做",
                        "制作",
                        "设计",
                        "创作",
                        "設計",
                        "創作",
                        "製作",
                        "生成",
                        "draw",
                        "design",
                        "create",
                        "make",
                    ),
                    (
                        "海报",
                        "封面",
                        "插画",
                        "插畫",
                        "宣传图",
                        "配图",
                        "主视觉",
                        "key visual",
                        "poster",
                        "illustration",
                        "artwork",
                        "banner",
                    ),
                ),
                blocked_prefixes=meta_prefixes,
                blocked_suffixes=meta_suffixes,
                suppression_phrases=(
                    "海报设计方案",
                    "海报设计规范",
                    "海报设计指南",
                    "封面设计方案",
                    "封面设计规范",
                    "封面设计指南",
                    "插画创作方案",
                    "插画设计方案",
                    "插画教程",
                    "怎么设计海报",
                    "如何设计海报",
                    "poster design guidelines",
                    "poster design specification",
                    "poster design system",
                    "cover design guidelines",
                    "cover design specification",
                    "illustration workflow",
                    "how to draw a poster",
                    "how do i draw a poster",
                    *shared_suppressions,
                ),
                score_boost=1_200,
                promote_to=Exposure.DIRECT,
            ),
            IntentRoutingRule(
                rule_id="media.image.edit",
                version="1.6.0",
                required_facets_any=frozenset({"media.image.edit"}),
                required_effects=frozenset({CapabilityEffect.GENERATE_MEDIA}),
                positive_phrases=(
                    "edit image",
                    "edit an image",
                    "edit the image",
                    "modify image",
                    "modify the image",
                    "retouch image",
                    "retouch the image",
                    "change background",
                    "change the background",
                    "replace background",
                    "replace the background",
                    "remove background",
                    "remove the background",
                    "cut out the subject",
                    "isolate the subject",
                    "inpaint",
                    "outpaint",
                    "use reference image",
                    "using a reference image",
                    "based on reference image",
                    "改图",
                    "修图",
                    "精修",
                    "编辑图片",
                    "修改图片",
                    "修改这张图",
                    "改这张图",
                    "改这张参考图",
                    "修这张图",
                    "基于参考图",
                    "用参考图",
                    "参考图改",
                    "局部重绘",
                    "换背景",
                    "去背景",
                    "替换背景",
                    "背景换成",
                    "背景改成",
                    "背景修改为",
                    "抠图",
                    "修圖",
                    "編輯圖片",
                    "修改圖片",
                    "換背景",
                    "替換背景",
                    "去背",
                    "摳圖",
                    "去掉路人",
                    "路人去掉",
                    "去除水印",
                    "水印去除",
                    "remove the person",
                    "remove a person",
                    "remove the watermark",
                ),
                blocked_prefixes=meta_prefixes,
                blocked_suffixes=meta_suffixes,
                suppression_phrases=(
                    "不要改图",
                    "不要修改图片",
                    "不要修改圖片",
                    "别改图",
                    "无需改图",
                    "不要修图",
                    "别修图",
                    "不要修圖",
                    "別修圖",
                    "不要换背景",
                    "别换背景",
                    "不要換背景",
                    "別換背景",
                    "不要抠图",
                    "别抠图",
                    "不要摳圖",
                    "別摳圖",
                    "无法修图",
                    "do not edit",
                    "don't edit",
                    "dont edit",
                    "without editing",
                    "do not change the background",
                    "don't change the background",
                    "do not remove the background",
                    "don't remove the background",
                    *shared_suppressions,
                    *edit_discussion_suppressions,
                ),
                score_boost=1_200,
                promote_to=Exposure.DIRECT,
            ),
        ),
    )


__all__ = [
    "IntentRouteEvidence",
    "IntentRoutingPolicy",
    "IntentRoutingRule",
    "MAX_ROUTING_PHRASES",
    "MAX_ROUTING_PHRASE_GROUPS",
    "MAX_ROUTING_INTENT_BYTES",
    "MAX_ROUTING_RULES",
    "MAX_ROUTING_SCORE_BOOST",
    "builtin_intent_routing_policy",
    "intent_is_routable",
    "intent_inherits_image_context",
    "normalize_intent_clauses",
    "normalize_intent_text",
]
