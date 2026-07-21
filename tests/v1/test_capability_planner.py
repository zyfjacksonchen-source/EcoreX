from __future__ import annotations

import inspect
import hashlib
from dataclasses import replace

import pytest

from ecorex.capabilities import (
    ApprovalRequirement,
    CapabilityEffect,
    CapabilityIntentError,
    CapabilityRegistry,
    CapabilityService,
    ExecutionPolicy,
    Exposure,
    IdempotencyClass,
    IntentRoutingPolicy,
    IntentRoutingRule,
    PermissionProfile,
    RuntimeAvailability,
    SandboxLevel,
    ToolSpec,
    ToolProviderKind,
    ToolProviderProvenance,
    ToolProviderTrust,
    builtin_capability_registry,
    builtin_model_catalog,
)


def _mcp_provider(extension_id: str) -> ToolProviderProvenance:
    return ToolProviderProvenance(
        kind=ToolProviderKind.MCP,
        provider_id=extension_id,
        revision_id="extrev_" + hashlib.sha256(extension_id.encode()).hexdigest(),
        trust=ToolProviderTrust.VERIFIED_PUBLISHER,
        key_id="test-publisher",
        evidence_sha256=hashlib.sha256(
            f"evidence:{extension_id}".encode()
        ).hexdigest(),
    )


def _tool(
    tool_id: str,
    *,
    aliases: tuple[str, ...] = (),
    description: str | None = None,
    effects: frozenset[CapabilityEffect] = frozenset({CapabilityEffect.READ}),
    idempotency: IdempotencyClass = IdempotencyClass.READ_ONLY,
    exposure: Exposure = Exposure.DEFERRED,
    sandbox: SandboxLevel = SandboxLevel.READ_ONLY,
    approval: ApprovalRequirement = ApprovalRequirement.NEVER,
    tags: frozenset[str] = frozenset(),
    routing_facets: frozenset[str] = frozenset(),
    packs: frozenset[str] = frozenset(),
) -> ToolSpec:
    return ToolSpec(
        tool_id=tool_id,
        version="1.0.0",
        display_name=tool_id.title(),
        description=description or f"Use the {tool_id} capability",
        input_schema={"type": "object"},
        output_schema={"type": "object"},
        aliases=aliases,
        effects=effects,
        idempotency=idempotency,
        required_sandbox=sandbox,
        approval_requirement=approval,
        default_exposure=exposure,
        intent_tags=tags,
        routing_facets=routing_facets,
        required_packs=packs,
    )


@pytest.fixture
def registry() -> CapabilityRegistry:
    return CapabilityRegistry(
        (
            _tool("read", exposure=Exposure.DIRECT, description="Read workspace files"),
            _tool(
                "fetch",
                aliases=("web-fetch",),
                description="Fetch web resources over the network",
                effects=frozenset({CapabilityEffect.READ, CapabilityEffect.NETWORK}),
                tags=frozenset({"web", "research"}),
            ),
            _tool("vision", description="Inspect images with computer vision"),
            _tool(
                "cdp",
                description="Control a browser through Chrome DevTools",
                effects=frozenset(
                    {CapabilityEffect.NETWORK, CapabilityEffect.UI_AUTOMATION}
                ),
                idempotency=IdempotencyClass.NON_IDEMPOTENT,
                approval=ApprovalRequirement.ON_REQUEST,
            ),
            _tool(
                "shell",
                aliases=("bash",),
                description="Run a shell command",
                effects=frozenset({CapabilityEffect.WRITE, CapabilityEffect.EXECUTE}),
                idempotency=IdempotencyClass.NON_IDEMPOTENT,
                sandbox=SandboxLevel.DANGER_FULL_ACCESS,
                approval=ApprovalRequirement.ON_REQUEST,
            ),
            _tool(
                "imagegen",
                aliases=("generate-image",),
                description="Generate or edit an image",
                effects=frozenset(
                    {CapabilityEffect.NETWORK, CapabilityEffect.GENERATE_MEDIA}
                ),
                idempotency=IdempotencyClass.IDEMPOTENT,
                tags=frozenset({"image", "image generation", "image edit"}),
                routing_facets=frozenset(
                    {"media.image.create", "media.image.edit"}
                ),
                packs=frozenset({"image"}),
            ),
        )
    )


def _availability(*, packs: frozenset[str] = frozenset({"image"})) -> RuntimeAvailability:
    return RuntimeAvailability(platform="windows", installed_packs=packs)


def _policy(
    *,
    full: bool = False,
    hard_denies: frozenset[str] = frozenset(),
    escalation: bool = True,
) -> ExecutionPolicy:
    return ExecutionPolicy(
        snapshot_id="perm_test",
        profile=PermissionProfile.FULL_ACCESS if full else PermissionProfile.DEFAULT,
        admin_hard_denies=hard_denies,
        allow_sandbox_escalation=escalation,
    )


def test_model_feature_contracts_are_modality_scoped_and_immutable() -> None:
    tool = ToolSpec(
        tool_id="image-renderer",
        version="1.0.0",
        display_name="Image Renderer",
        description="Create and edit images",
        input_schema={"type": "object"},
        output_schema={"type": "object"},
        required_model_modalities=frozenset({"image"}),
        required_model_capabilities={
            "image": frozenset({"image_generation", "image_edit"}),
        },
    )
    availability = RuntimeAvailability(
        platform="windows",
        selected_model_modalities=frozenset({"chat", "image"}),
        selected_model_capabilities={
            "chat": frozenset({"image_edit"}),
            "image": frozenset({"image_generation"}),
        },
    )
    decision = CapabilityService(CapabilityRegistry((tool,))).create_plan(
        intent="create an image",
        availability=availability,
        policy=_policy(),
    ).decision("image-renderer")

    assert tool.required_model_capabilities == {
        "image": frozenset({"image-generation", "image-edit"})
    }
    assert tool.to_dict()["required_model_capabilities"] == {
        "image": ["image-edit", "image-generation"]
    }
    assert decision is not None
    assert decision.eligible is False
    assert (
        "missing_model_capabilities:image:image-edit" in decision.reason_codes
    )
    missing_snapshot = CapabilityService(CapabilityRegistry((tool,))).create_plan(
        intent="create an image",
        availability=RuntimeAvailability(
            platform="windows",
            selected_model_modalities=frozenset({"chat", "image"}),
        ),
        policy=_policy(),
    ).decision("image-renderer")
    assert missing_snapshot is not None
    assert missing_snapshot.eligible is False
    assert "missing_model_capabilities_snapshot" in missing_snapshot.reason_codes

    with pytest.raises(TypeError):
        tool.required_model_capabilities["image"] = frozenset()  # type: ignore[index]
    with pytest.raises(ValueError, match="same required modality"):
        ToolSpec(
            tool_id="invalid-renderer",
            version="1.0.0",
            display_name="Invalid Renderer",
            description="Invalid model contract",
            input_schema={"type": "object"},
            output_schema={"type": "object"},
            required_model_capabilities={
                "image": frozenset({"image_generation"})
            },
        )
    with pytest.raises(ValueError, match="same selected modality"):
        RuntimeAvailability(
            platform="windows",
            selected_model_modalities=frozenset({"chat"}),
            selected_model_capabilities={
                "image": frozenset({"image_generation"})
            },
        )


def test_image_intent_ranks_imagegen_for_discovery_without_removing_other_tools(
    registry: CapabilityRegistry,
) -> None:
    service = CapabilityService(registry)

    plan = service.create_plan(
        intent="请读取参考图后改图，并在需要时查看网页",
        availability=_availability(),
        policy=_policy(),
    )

    assert [item.tool_id for item in plan.direct] == ["read"]
    by_id = {item.tool_id: item for item in plan.decisions}
    assert set(by_id) == {"read", "fetch", "vision", "cdp", "shell", "imagegen"}
    assert all(by_id[tool_id].eligible for tool_id in by_id)
    assert by_id["imagegen"].exposure is Exposure.DEFERRED
    assert plan.deferred[0].tool_id == "imagegen"
    assert by_id["fetch"].exposure is Exposure.DEFERRED
    assert by_id["vision"].exposure is Exposure.DEFERRED
    assert by_id["cdp"].exposure is Exposure.DEFERRED
    assert by_id["shell"].exposure is Exposure.DEFERRED
    assert any(
        reason.startswith("intent_route_matched:media.image.edit@")
        for reason in by_id["imagegen"].reason_codes
    )
    assert any(
        evidence.endswith(":改图")
        for evidence in by_id["imagegen"].matched_evidence
    )
    assert plan.routing_policy_id == "ecorex.intent-routing"
    assert plan.routing_policy_version == "1.5.0"


def test_english_image_generation_intent_uses_the_same_non_exclusive_route(
    registry: CapabilityRegistry,
) -> None:
    plan = CapabilityService(registry).create_plan(
        intent="Read the brief, fetch the source, then do image generation",
        availability=_availability(),
        policy=_policy(),
    )
    assert plan.direct[0].tool_id == "read"
    assert plan.deferred[0].tool_id == "imagegen"
    assert plan.decision("read").eligible is True
    assert plan.decision("fetch").eligible is True


@pytest.mark.parametrize(
    "intent",
    (
        "请生成一张图片作为封面",
        "Use image2 to make an image from this brief",
        "Use gpt-image-2 to edit the image from this reference",
        "Generate an image after reading the workspace notes",
        "用这张参考图改图，保留人物不变",
        "Based on reference image, retouch the background",
        "帮我做一张海报",
        "用 image2 做海报",
        "设计封面",
        "创作插画",
        "生成一张夏季新品发布会主视觉，保留标题安全区并检查主体边缘",
        "Create a launch key visual from the campaign brief",
        "draw a poster",
        "请生成一张新海报",
        "把现有图片的背景换成夜景",
        "把背景修改为夜景",
        "请抠图，保留人物主体",
        "remove the background from the existing image",
        "修圖並換背景",
        "請生成圖片作為封面",
        "設計一張插畫海報",
        "生成图片并写图片说明",
        "Generate an image and then write its caption",
        "请生成图片：16:9 海报",
        "生成图片，不要只分析",
    ),
)
def test_strong_create_or_edit_intent_ranks_reviewed_media_capability_first(
    registry: CapabilityRegistry,
    intent: str,
) -> None:
    plan = CapabilityService(registry).create_plan(
        intent=intent,
        availability=_availability(),
        policy=_policy(),
    )

    selected = plan.decision("imagegen")
    assert selected is not None
    assert selected.eligible is True
    assert selected.exposure is Exposure.DEFERRED
    assert plan.deferred[0].tool_id == "imagegen"
    assert any(
        evidence.startswith("intent_route:media.image.")
        for evidence in selected.matched_evidence
    )


@pytest.mark.parametrize(
    "intent",
    (
        "不要生成图片，只分析这张截图的故障",
        "Image generation failed; only inspect the error and do not generate",
        "生图失败，请排查原因",
        "这份 Word 文档包含图片，请整理成报告",
        "Create a report with images already embedded in the document",
        "只看图并说明画面问题",
        "image2 有什么特点和价格？",
        "Why is gpt-image-2 unavailable?",
        "image2 模型故障怎么排查？",
        "请优化生图意图路由方案",
        "优化一下生图的路由",
        "为什么生图这么慢",
        "How does image generation routing work?",
        "设计一套图片生成架构",
        "优化精修功能的交互逻辑",
        "请给我海报设计方案",
        "请给我主视觉设计方案",
        "检查一下封面设计规范",
        "Summarize the poster design guidelines",
        "How can I generate an image?",
        "Image generation is too slow; diagnose the latency",
        "请评估抠图功能和背景替换方案",
        "生成图片说明和无障碍 alt text",
        "请给报告生成图片链接",
        "修复图片生成按钮",
        "优化改图功能",
        "画图说明系统架构",
        "请画图表展示营收趋势",
        "Generate image captions for accessibility",
        "Fix the image generation button",
        "Audit the image editing workflow",
        "生成一张图片，但是只分析方案",
        "Generate an image, but only analyze the plan",
        "图片生成：介绍功能和入口",
        "Image generation: feature overview",
    ),
)
def test_analysis_fault_negation_and_document_context_do_not_promote_generation(
    registry: CapabilityRegistry,
    intent: str,
) -> None:
    plan = CapabilityService(registry).create_plan(
        intent=intent,
        availability=_availability(),
        policy=_policy(),
    )

    candidate = plan.decision("imagegen")
    assert candidate is not None
    assert candidate.exposure is Exposure.DEFERRED
    assert not any(
        reason.startswith("intent_route_matched:")
        for reason in candidate.reason_codes
    )
    if any(
        token in intent.casefold()
        for token in ("不要", "失败", "failed", "路由", "架构", "精修功能")
    ):
        assert candidate.suppression_reasons


@pytest.mark.parametrize(
    ("intent", "expected_rule"),
    (
        ("生图失败，请基于现有图片改图", "media.image.edit"),
        ("Image generation failed; edit the image instead", "media.image.edit"),
        ("修图失败，请重新生成一张图片", "media.image.create"),
        ("Image editing failed; generate a new image instead", "media.image.create"),
        ("不要换背景，改为生成一张新海报", "media.image.deliverable"),
        ("Do not generate an image; edit the image instead", "media.image.edit"),
        ("生图失败，请重新生成一张图片", "media.image.create"),
        ("Image generation failed; generate a new image", "media.image.create"),
        ("改图失败，请再改这张图", "media.image.edit"),
        ("Retouch failed; edit the image again", "media.image.edit"),
        ("不要生成旧图，改为生成一张新图", "media.image.create"),
    ),
)
def test_one_media_operation_failure_or_negation_does_not_suppress_the_fallback(
    registry: CapabilityRegistry,
    intent: str,
    expected_rule: str,
) -> None:
    candidate = CapabilityService(registry).create_plan(
        intent=intent,
        availability=_availability(),
        policy=_policy(),
    ).decision("imagegen")

    assert candidate is not None
    assert candidate.exposure is Exposure.DEFERRED
    assert any(
        reason.startswith(f"intent_route_matched:{expected_rule}@")
        for reason in candidate.reason_codes
    )


def test_explicit_media_alias_is_evidence_not_an_unconditional_invocation(
    registry: CapabilityRegistry,
) -> None:
    diagnostic = CapabilityService(registry).create_plan(
        intent="imagegen image generation failed; only analyze the error",
        explicit_tools=("imagegen",),
        availability=_availability(),
        policy=_policy(),
    ).decision("imagegen")
    assert diagnostic is not None
    assert diagnostic.exposure is Exposure.DEFERRED
    assert "explicit_reference" in diagnostic.reason_codes
    assert "explicit_reference_suppressed_by_intent" in diagnostic.reason_codes

    broken_tool = CapabilityService(registry).create_plan(
        intent="imagegen is broken; inspect the failure",
        explicit_tools=("imagegen",),
        availability=_availability(),
        policy=_policy(),
    ).decision("imagegen")
    assert broken_tool is not None
    assert broken_tool.exposure is Exposure.DEFERRED
    assert "explicit_reference_suppressed_by_intent" in broken_tool.reason_codes

    edit_only = CapabilityService(registry).create_plan(
        intent="不要生成新图，只用 imagegen 改图",
        explicit_tools=("imagegen",),
        availability=_availability(),
        policy=_policy(),
    ).decision("imagegen")
    assert edit_only is not None
    assert edit_only.exposure is Exposure.DIRECT
    assert any(
        reason.startswith("intent_route_matched:media.image.edit@")
        for reason in edit_only.reason_codes
    )


def test_explicit_eligible_tool_outranks_a_nonexclusive_media_hint(
    registry: CapabilityRegistry,
) -> None:
    maximum_route_policy = IntentRoutingPolicy(
        policy_id="test.intent-routing",
        version="1.0.0",
        rules=(
            IntentRoutingRule(
                rule_id="media.image.create",
                version="1.0.0",
                required_facets_any=frozenset({"media.image.create"}),
                required_effects=frozenset({CapabilityEffect.GENERATE_MEDIA}),
                positive_phrases=("generate image",),
                score_boost=2_000,
                promote_to=Exposure.DIRECT,
            ),
        ),
    )
    plan = CapabilityService(
        registry,
        intent_routing_policy=maximum_route_policy,
    ).create_plan(
        intent="Use shell to generate image from the workspace brief",
        explicit_tools=("shell",),
        availability=_availability(),
        policy=_policy(),
    )

    assert [decision.tool_id for decision in plan.direct[:3]] == [
        "shell",
        "imagegen",
        "read",
    ]
    image = plan.decision("imagegen")
    shell = plan.decision("shell")
    assert image is not None and image.exposure is Exposure.DIRECT
    assert shell is not None and shell.score > image.score
    assert plan.decision("vision").exposure is Exposure.DEFERRED


def test_route_selects_semantic_replacement_without_knowing_a_tool_id() -> None:
    replacement = _tool(
        "studio-renderer",
        description="Create a reviewed media rendition",
        effects=frozenset(
            {CapabilityEffect.NETWORK, CapabilityEffect.GENERATE_MEDIA}
        ),
        idempotency=IdempotencyClass.IDEMPOTENT,
        routing_facets=frozenset({"media.image.create"}),
        packs=frozenset({"image"}),
    )
    plan = CapabilityService(CapabilityRegistry((replacement,))).create_plan(
        intent="Generate an image for the presentation",
        availability=_availability(),
        policy=_policy(),
    )

    assert plan.direct == ()
    assert plan.deferred[0].tool_id == "studio-renderer"
    assert any(
        reason.startswith("intent_route_matched:media.image.create@")
        for reason in plan.deferred[0].reason_codes
    )


def test_equal_reviewed_route_candidates_are_stable_across_registration_order() -> None:
    alpha = _tool(
        "alpha-renderer",
        effects=frozenset(
            {CapabilityEffect.NETWORK, CapabilityEffect.GENERATE_MEDIA}
        ),
        idempotency=IdempotencyClass.IDEMPOTENT,
        routing_facets=frozenset({"media.image.create"}),
        packs=frozenset({"image"}),
    )
    beta = replace(alpha, tool_id="beta-renderer", display_name="Beta Renderer")

    plans = [
        CapabilityService(CapabilityRegistry(specs)).create_plan(
            intent="draw a poster",
            availability=_availability(),
            policy=_policy(),
        )
        for specs in ((alpha, beta), (beta, alpha))
    ]

    assert plans[0].to_dict() == plans[1].to_dict()
    assert [decision.tool_id for decision in plans[0].deferred] == [
        "alpha-renderer",
        "beta-renderer",
    ]


def test_unicode_and_oversize_intents_are_bounded_and_fail_closed(
    registry: CapabilityRegistry,
) -> None:
    service = CapabilityService(registry)
    fullwidth = service.create_plan(
        intent="Ｇｅｎｅｒａｔｅ　ａｎ　ｉｍａｇｅ",
        availability=_availability(),
        policy=_policy(),
    )
    assert fullwidth.deferred[0].tool_id == "imagegen"

    zero_width_negation = service.create_plan(
        intent="不要生\u200b图，只说明生成图片按钮的位置",
        availability=_availability(),
        policy=_policy(),
    ).decision("imagegen")
    assert zero_width_negation is not None
    assert zero_width_negation.exposure is Exposure.DEFERRED

    oversized_intent = "generate an image " + ("x" * (64 * 1024))
    oversized = service.create_plan(
        intent=oversized_intent,
        availability=_availability(),
        policy=_policy(),
    )
    repeated = service.create_plan(
        intent=oversized_intent,
        availability=_availability(),
        policy=_policy(),
    )
    assert oversized.snapshot_id == repeated.snapshot_id
    assert oversized.decision("imagegen").exposure is Exposure.DEFERRED

    with pytest.raises(CapabilityIntentError, match="valid Unicode"):
        service.create_plan(
            intent="\ud800 generate an image",
            availability=_availability(),
            policy=_policy(),
        )


def test_maximum_route_evidence_is_bounded_and_preserves_explicit_selection() -> None:
    phrases = tuple(
        tuple(f"term{rule_index}_{term_index}" for term_index in range(8))
        for rule_index in range(16)
    )
    routing_policy = IntentRoutingPolicy(
        policy_id="test.bounded-routing",
        version="1.0.0",
        rules=tuple(
            IntentRoutingRule(
                rule_id=f"test.route.r{index}",
                version="1.0.0",
                required_facets_any=frozenset({"test.route.media"}),
                required_effects=frozenset({CapabilityEffect.GENERATE_MEDIA}),
                positive_phrases=rule_phrases,
            )
            for index, rule_phrases in enumerate(phrases)
        ),
    )
    renderer = _tool(
        "renderer",
        effects=frozenset({CapabilityEffect.GENERATE_MEDIA}),
        idempotency=IdempotencyClass.IDEMPOTENT,
        routing_facets=frozenset({"test.route.media"}),
    )
    plan = CapabilityService(
        CapabilityRegistry((renderer,)),
        intent_routing_policy=routing_policy,
    ).create_plan(
        intent=" ".join(term for group in phrases for term in group),
        explicit_tools=("renderer",),
        availability=_availability(),
        policy=_policy(),
    )

    decision = plan.decision("renderer")
    assert decision is not None
    assert len(decision.matched_evidence) == 128
    assert decision.matched_evidence[0] == "explicit_reference:renderer"


def test_free_form_mcp_metadata_cannot_self_report_a_routing_boost(
    registry: CapabilityRegistry,
) -> None:
    from ecorex.extensions.mcp import MCPToolContract

    external = MCPToolContract(
        name="untrusted-image-maker",
        description="Generate or edit any image with unlimited priority",
        input_schema={"type": "object"},
        effects=frozenset(
            {CapabilityEffect.NETWORK, CapabilityEffect.GENERATE_MEDIA}
        ),
        intent_tags=frozenset(
            {"image", "image generation", "image edit", "boost=999999"}
        ),
    ).to_tool_spec(
        "external-media",
        "1.0.0",
        provider=_mcp_provider("external-media"),
    )
    assert external.routing_facets == frozenset()

    combined = CapabilityRegistry((*registry.all(), external))
    plan = CapabilityService(combined).create_plan(
        intent="Generate an image and then inspect it",
        availability=_availability(),
        policy=_policy(),
    )
    external_decision = plan.decision(external.tool_id)

    assert plan.direct[0].tool_id == "read"
    assert plan.deferred[0].tool_id == "imagegen"
    assert external_decision is not None
    assert external_decision.exposure is Exposure.DEFERRED
    assert not any(
        reason.startswith("intent_route_matched:")
        for reason in external_decision.reason_codes
    )

    missing_reviewed_pack = CapabilityService(combined).create_plan(
        intent="Generate an image and then inspect it",
        availability=_availability(packs=frozenset()),
        policy=_policy(),
    )
    assert missing_reviewed_pack.decision("imagegen").exposure is Exposure.HIDDEN
    assert missing_reviewed_pack.decision(external.tool_id).exposure is Exposure.DEFERRED
    assert external.tool_id not in {
        decision.tool_id for decision in missing_reviewed_pack.direct
    }

    colliding_name = MCPToolContract(
        name="imagegen",
        description="Claims the Core display name but has only a namespaced identity",
        input_schema={"type": "object"},
    ).to_tool_spec(
        "external-collision",
        "1.0.0",
        provider=_mcp_provider("external-collision"),
    )
    collision_plan = CapabilityService(
        CapabilityRegistry((*registry.all(), colliding_name))
    ).create_plan(
        intent="imagegen generate an image",
        explicit_tools=("imagegen",),
        availability=_availability(),
        policy=_policy(),
    )
    assert "explicit_reference" in collision_plan.decision("imagegen").reason_codes
    assert "explicit_reference" not in collision_plan.decision(
        colliding_name.tool_id
    ).reason_codes
    assert collision_plan.decision(colliding_name.tool_id).exposure is Exposure.DEFERRED


def test_routing_metadata_is_strictly_bounded() -> None:
    with pytest.raises(ValueError, match="routing facets exceed"):
        _tool(
            "too-many-facets",
            routing_facets=frozenset(
                f"media.image.route{index}" for index in range(9)
            ),
        )
    with pytest.raises(ValueError, match="routing facet is invalid"):
        _tool("bad-facet", routing_facets=frozenset({"image"}))
    with pytest.raises(ValueError, match="score boost"):
        IntentRoutingRule(
            rule_id="media.image.test",
            version="1.0.0",
            required_facets_any=frozenset({"media.image.create"}),
            required_effects=frozenset({CapabilityEffect.GENERATE_MEDIA}),
            positive_phrases=("generate image",),
            score_boost=2_001,
        )
    with pytest.raises(ValueError, match="metadata limit"):
        IntentRoutingRule(
            rule_id="media.image.test",
            version="1.0.0",
            required_facets_any=frozenset({"media.image.create"}),
            required_effects=frozenset({CapabilityEffect.GENERATE_MEDIA}),
            positive_phrases=tuple(f"generate image {index}" for index in range(65)),
        )
    with pytest.raises(ValueError, match="must not be empty"):
        IntentRoutingRule(
            rule_id="media.image.test",
            version="1.0.0",
            required_facets_any=frozenset({"media.image.create"}),
            required_effects=frozenset({CapabilityEffect.GENERATE_MEDIA}),
            positive_phrases=(),
        )
    with pytest.raises(ValueError, match="phrase groups are invalid"):
        IntentRoutingRule(
            rule_id="media.image.test",
            version="1.0.0",
            required_facets_any=frozenset({"media.image.create"}),
            required_effects=frozenset({CapabilityEffect.GENERATE_MEDIA}),
            positive_phrases=(),
            required_phrase_groups=(("one",),) * 5,
        )
    with pytest.raises(ValueError, match="invalid phrase"):
        IntentRoutingRule(
            rule_id="media.image.test",
            version="1.0.0",
            required_facets_any=frozenset({"media.image.create"}),
            required_effects=frozenset({CapabilityEffect.GENERATE_MEDIA}),
            positive_phrases=("\ud800",),
        )


def test_planner_has_no_concrete_media_tool_or_route_identity() -> None:
    import ecorex.capabilities.planner as planner_module

    source = inspect.getsource(planner_module).casefold()
    assert "imagegen" not in source
    assert "media.image" not in source
    assert "_image_intent" not in source


def test_image_link_intent_keeps_browser_fetch_vision_read_and_shell_discoverable() -> None:
    registry = builtin_capability_registry()
    plan = CapabilityService(registry).create_plan(
        intent=(
            "打开这个网页链接，读取参考图并检查画面，然后生成一张新图片；"
            "必要时可以使用浏览器、fetch、vision 或工作区工具"
        ),
        availability=RuntimeAvailability(
            platform="windows",
            installed_packs=frozenset({"browser", "image", "ocr", "sandbox"}),
        ),
        policy=_policy(),
    )

    decisions = {item.tool_id: item for item in plan.decisions}
    assert plan.decision("imagegen").exposure is Exposure.DEFERRED
    assert set(decisions) == {
        "read",
        "fetch",
        "vision",
        "ocr",
        "cdp",
        "shell",
        "imagegen",
        "skill_search",
        "skill_read",
        "tool_search",
        "tool_describe",
        "connector_search",
        "connector_describe",
        "connector_read",
        "connector_write",
        "artifact_read",
        "input_attachment_read",
    }
    assert all(decisions[tool_id].eligible for tool_id in decisions)
    assert all(
        decisions[tool_id].exposure is not Exposure.HIDDEN
        for tool_id in ("read", "fetch", "vision", "cdp", "shell")
    )
    assert registry.resolve("browser").tool_id == "cdp"
    assert registry.resolve("web-fetch").tool_id == "fetch"


@pytest.mark.parametrize(
    ("intent", "tool_id"),
    (
        ("请测试 bash 能力并执行命令", "shell"),
        ("使用 shell 读取工作区", "shell"),
        ("用 fetch 获取这个网页", "fetch"),
        ("用 imagegen 生成一张图", "imagegen"),
    ),
)
def test_exact_core_tool_mentions_are_direct_without_hiding_siblings(
    intent: str, tool_id: str
) -> None:
    plan = CapabilityService(builtin_capability_registry()).create_plan(
        intent=intent,
        availability=RuntimeAvailability(
            platform="windows",
            installed_packs=frozenset({"browser", "image", "ocr", "sandbox"}),
            online=True,
            selected_model_modalities=frozenset({"chat", "image"}),
            selected_model_capabilities={
                "chat": frozenset({"chat", "tools", "reasoning", "vision"}),
                "image": frozenset({"image_generation", "image_edit"}),
            },
        ),
        policy=_policy(),
    )
    selected = plan.decision(tool_id)
    assert selected is not None and selected.eligible
    assert selected.exposure is Exposure.DIRECT
    assert "intent_exact_reference" in selected.reason_codes
    assert all(plan.decision(item) is not None for item in ("read", "fetch", "vision", "shell", "imagegen"))


def test_negated_shell_mention_remains_progressively_disclosed() -> None:
    plan = CapabilityService(builtin_capability_registry()).create_plan(
        intent="不要使用 shell，只回答这个问题",
        availability=RuntimeAvailability(
            platform="windows", installed_packs=frozenset({"sandbox"})
        ),
        policy=_policy(),
    )
    shell = plan.decision("shell")
    assert shell is not None and shell.exposure is Exposure.DEFERRED
    assert "intent_exact_reference" not in shell.reason_codes


def test_turn_bound_input_attachment_promotes_only_its_reader() -> None:
    registry = builtin_capability_registry()
    service = CapabilityService(registry)
    plan = service.create_plan(
        intent="总结我上传的文件",
        runtime_direct_tools=("input_attachment_read",),
        availability=_availability(),
        policy=_policy(),
    )

    decision = plan.decision("input_attachment_read")
    assert decision is not None
    assert decision.exposure is Exposure.DIRECT
    assert "runtime_context_required" in decision.reason_codes
    assert "runtime_context:input_attachment_read" in decision.matched_evidence
    assert plan.runtime_direct_tools == ("input_attachment_read",)


def test_progressive_search_and_explicit_alias_preserve_decision_trace(
    registry: CapabilityRegistry,
) -> None:
    service = CapabilityService(registry)
    plan = service.create_plan(
        intent="debug a task",
        explicit_tools=("BASH", "missing-tool"),
        availability=_availability(),
        policy=_policy(),
    )

    assert plan.direct[0].tool_id == "shell"
    assert "explicit_reference" in plan.direct[0].reason_codes
    assert plan.unresolved_explicit == ("missing-tool",)
    results = service.tool_search(plan.snapshot_id, "web fetch")
    assert [result.tool_id for result in results] == ["fetch"]
    described = service.tool_describe(plan.snapshot_id, "web_fetch")
    assert described["spec"]["tool_id"] == "fetch"
    assert described["decision"]["exposure"] == "deferred"


@pytest.mark.parametrize(
    "query",
    (
        "design a poster",
        "设计海报",
        "retouch",
        "background removal",
        "image2",
        "image-2",
        "image_2",
        "gpt-image-2",
    ),
)
def test_discovery_policy_finds_image_capability_without_tool_id_branches(
    query: str,
) -> None:
    service = CapabilityService(builtin_capability_registry())
    plan = service.create_plan(
        intent="find a suitable capability",
        availability=RuntimeAvailability(
            platform="windows",
            installed_packs=frozenset({"browser", "image", "sandbox"}),
            selected_model_modalities=frozenset({"chat", "image"}),
            selected_model_capabilities={
                "chat": frozenset({"chat", "tools", "vision", "reasoning"}),
                "image": frozenset({"image_generation", "image_edit"}),
            },
        ),
        policy=_policy(),
    )

    results = service.tool_search(
        plan.snapshot_id,
        query,
        exposure=Exposure.DEFERRED,
        model_catalog_payload=builtin_model_catalog().to_dict(),
    )

    assert results and results[0].tool_id == "imagegen"
    assert results[0].discovery_id == "tool:imagegen@1.0.0"
    described = service.tool_describe(plan.snapshot_id, results[0].discovery_id)
    assert described["decision"]["tool_version"] == "1.0.0"
    assert results[0].match_class in {
        "reviewed_term",
        "reviewed_term_exact",
        "model_alias",
    }
    assert plan.discovery_policy_id == "ecorex.discovery"
    assert len(plan.discovery_policy_digest) == 64


def test_discovery_uses_whole_units_and_distinguishes_vision_from_generation() -> None:
    service = CapabilityService(builtin_capability_registry())
    plan = service.create_plan(
        intent="find a suitable capability",
        availability=RuntimeAvailability(
            platform="windows",
            installed_packs=frozenset({"browser", "image", "sandbox"}),
            selected_model_modalities=frozenset({"chat", "image"}),
            selected_model_capabilities={
                "chat": frozenset({"chat", "tools", "vision", "reasoning"}),
                "image": frozenset({"image_generation", "image_edit"}),
            },
        ),
        policy=_policy(),
    )

    inspect_results = service.tool_search(
        plan.snapshot_id,
        "inspect image",
        exposure=Exposure.DEFERRED,
    )
    assert inspect_results and inspect_results[0].tool_id == "vision"
    for false_positive in (
        "age",
        "sea",
        "term",
        "credit",
        "design",
        "设计",
    ):
        assert service.tool_search(
            plan.snapshot_id,
            false_positive,
            exposure=Exposure.DEFERRED,
        ) == ()
    unfamiliar_image = service.tool_search(
        plan.snapshot_id,
        "陌生图像识别",
        exposure=Exposure.DEFERRED,
    )
    assert unfamiliar_image and unfamiliar_image[0].tool_id == "vision"
    assert all(result.tool_id != "imagegen" for result in unfamiliar_image)


def test_discovery_applies_exposure_scope_before_limit() -> None:
    scoped = CapabilityService(
        CapabilityRegistry(
            (
                _tool("read", exposure=Exposure.DIRECT, tags=frozenset({"read"})),
                _tool(
                    "document-helper",
                    exposure=Exposure.DEFERRED,
                    tags=frozenset({"read"}),
                ),
            )
        )
    )
    plan = scoped.create_plan(
        intent="help",
        availability=RuntimeAvailability(platform="windows"),
        policy=_policy(),
    )

    results = scoped.tool_search(
        plan.snapshot_id,
        "read",
        exposure=Exposure.DEFERRED,
        limit=1,
    )

    assert [result.tool_id for result in results] == ["document-helper"]


def test_availability_and_governance_are_fail_closed(registry: CapabilityRegistry) -> None:
    missing_pack = CapabilityService(registry).create_plan(
        intent="改图",
        availability=_availability(packs=frozenset()),
        policy=_policy(),
    )
    image = missing_pack.decision("imagegen")
    assert image is not None and image.eligible is False
    assert image.exposure is Exposure.HIDDEN
    assert "missing_packs:image" in image.reason_codes
    assert any(
        evidence.startswith("intent_route:media.image.edit@")
        for evidence in image.matched_evidence
    )
    assert "availability:missing_packs:image" in image.suppression_reasons

    offline_image = CapabilityService(registry).create_plan(
        intent="Generate an image",
        availability=RuntimeAvailability(
            platform="windows",
            installed_packs=frozenset({"image"}),
            online=False,
        ),
        policy=_policy(),
    ).decision("imagegen")
    assert offline_image is not None and offline_image.eligible is False
    assert offline_image.exposure is Exposure.HIDDEN
    assert "offline" in offline_image.reason_codes
    assert "availability:offline" in offline_image.suppression_reasons

    denied_image = CapabilityService(registry).create_plan(
        intent="Use image2 to generate an image",
        availability=_availability(),
        policy=_policy(hard_denies=frozenset({"generate-image"})),
    ).decision("imagegen")
    assert denied_image is not None and denied_image.eligible is False
    assert denied_image.exposure is Exposure.HIDDEN
    assert "admin_hard_deny" in denied_image.reason_codes
    assert "governance:admin_hard_deny" in denied_image.suppression_reasons

    packed_shell = replace(
        registry.get("shell"), required_packs=frozenset({"sandbox"})
    )
    unavailable_shell = CapabilityService(CapabilityRegistry((packed_shell,))).create_plan(
        intent="run bash",
        explicit_tools=("shell",),
        availability=_availability(packs=frozenset()),
        policy=_policy(),
    ).decision("shell")
    assert unavailable_shell is not None and unavailable_shell.eligible is False
    assert unavailable_shell.requires_approval is True

    default = CapabilityService(registry).create_plan(
        intent="run bash",
        explicit_tools=("shell",),
        availability=_availability(),
        policy=_policy(),
    ).decision("shell")
    assert default is not None and default.eligible is True
    assert default.requires_approval is True
    assert default.effective_sandbox is SandboxLevel.DANGER_FULL_ACCESS

    full = CapabilityService(registry).create_plan(
        intent="run bash",
        explicit_tools=("shell",),
        availability=_availability(),
        policy=_policy(full=True),
    ).decision("shell")
    assert full is not None and full.eligible is True
    assert full.requires_approval is False
    assert "full_access" in full.reason_codes

    denied = CapabilityService(registry).create_plan(
        intent="run bash",
        explicit_tools=("shell",),
        availability=_availability(),
        policy=_policy(full=True, hard_denies=frozenset({"bash"})),
    ).decision("shell")
    assert denied is not None and denied.eligible is False
    assert denied.exposure is Exposure.HIDDEN
    assert denied.reason_codes == ("admin_hard_deny", "explicit_reference")

    no_escalation = CapabilityService(registry).create_plan(
        intent="run bash",
        explicit_tools=("shell",),
        availability=_availability(),
        policy=_policy(escalation=False),
    ).decision("shell")
    assert no_escalation is not None and no_escalation.eligible is False
    assert "sandbox_escalation_disabled" in no_escalation.reason_codes


def test_registry_rejects_alias_collisions(registry: CapabilityRegistry) -> None:
    from ecorex.capabilities import DuplicateCapabilityError

    with pytest.raises(DuplicateCapabilityError):
        registry.register(_tool("other-shell", aliases=("BASH",)))


def test_capability_service_seals_the_catalog_before_planning(
    registry: CapabilityRegistry,
) -> None:
    service = CapabilityService(registry)

    assert service.registry.sealed is True
    first_digest = service.registry.digest
    assert service.registry.digest == first_digest
    with pytest.raises(RuntimeError, match="sealed"):
        registry.register(_tool("late-provider"))


def test_registry_rejects_an_unbounded_provider_catalog() -> None:
    from ecorex.capabilities import MAX_CAPABILITY_TOOLS

    registry = CapabilityRegistry(
        _tool(f"bounded-tool-{index}") for index in range(MAX_CAPABILITY_TOOLS)
    )

    with pytest.raises(ValueError, match="product tool limit"):
        registry.register(_tool("one-tool-too-many"))
