from __future__ import annotations

import pytest
from pydantic import ValidationError

from ecorex.capabilities import (
    ManagedModelCatalog,
    ManagedModelSpec,
    ModelModality,
    ModelModalityMismatch,
    UnknownModelError,
    builtin_model_catalog,
)
from ecorex.runtime.api import _managed_catalog_from_cloud, project_model_catalog
from ecorex.protocol import CreateTurnRequest
from ecorex.managed_model_policy import (
    ECOREX_CHAT_MODEL_POLICIES,
    require_managed_chat_mapping,
)


def _catalog() -> ManagedModelCatalog:
    return ManagedModelCatalog(
        (
            ManagedModelSpec(
                model_id="ecorex-chat",
                display_name="EcoreX Chat",
                modalities=frozenset({ModelModality.CHAT, ModelModality.VISION}),
                aliases=("chat", "default"),
                capabilities=frozenset({"tools", "vision"}),
                default_for=frozenset({ModelModality.CHAT, ModelModality.VISION}),
            ),
            ManagedModelSpec(
                model_id="gpt-image-2",
                display_name="EcoreX Image 2",
                modalities=frozenset({ModelModality.IMAGE}),
                aliases=("image2", "image-2"),
                capabilities=frozenset({"image_generation", "image_edit"}),
                default_for=frozenset({ModelModality.IMAGE}),
            ),
        )
    )


@pytest.mark.parametrize(
    "reference",
    ["image2", "image-2", "image_2", "gpt-image-2", "IMAGE_2"],
)
def test_image2_aliases_resolve_to_one_backend_model(reference: str) -> None:
    resolution = _catalog().resolve(reference, modality=ModelModality.IMAGE)
    assert resolution.canonical_model_id == "gpt-image-2"
    assert resolution.catalog_snapshot_id.startswith("models_")


def test_builtin_catalog_publishes_one_canonical_image_2_alias() -> None:
    catalog = builtin_model_catalog()
    model = catalog.for_modality(ModelModality.IMAGE)[0]

    assert model.aliases == ("image2", "image-2")
    assert catalog.resolve("image_2", modality=ModelModality.IMAGE).canonical_model_id == (
        "gpt-image-2"
    )


def test_builtin_chat_catalog_publishes_versioned_gpt_56_sol_policy() -> None:
    catalog = builtin_model_catalog()
    model = catalog.for_modality(ModelModality.CHAT)[0]

    assert model.model_id == "ecorex-chat"
    assert model.display_name == "GPT-5.6 SOL · 中等推理"
    assert {"chat", "tools", "vision", "reasoning"} <= model.capabilities
    assert catalog.resolve(
        "gpt-5.6-sol", modality=ModelModality.CHAT
    ).canonical_model_id == "ecorex-chat"
    assert model.model_policy is not None
    assert model.model_policy.to_dict() == {
        "schema_version": 1,
        "policy_id": "ecorex-chat-gpt-5.6-sol",
        "policy_version": "1.0.0",
        "local_model_id": "ecorex-chat",
        "upstream_model_id": "gpt-5.6-sol",
        "reasoning_effort": "medium",
        "context_management": {
            "type": "compaction",
            "compact_threshold_tokens": 272_000,
        },
    }
    assert catalog.to_dict()["modalities"]["chat"][0]["model_policy"] == (
        model.model_policy.to_dict()
    )


def test_builtin_chat_catalog_restores_all_managed_v03_provider_choices() -> None:
    catalog = builtin_model_catalog()
    models = {model.model_id: model for model in catalog.for_modality(ModelModality.CHAT)}

    assert set(models) == {
        "ecorex-chat",
        "ecorex-deepseek-v4-pro",
        "ecorex-gemini-3.1-pro",
        "ecorex-doubao-seed-2.0-pro",
    }
    assert catalog.resolve("deepseek-v4-pro", modality=ModelModality.CHAT).canonical_model_id == (
        "ecorex-deepseek-v4-pro"
    )
    assert catalog.resolve("gemini", modality=ModelModality.CHAT).canonical_model_id == (
        "ecorex-gemini-3.1-pro"
    )
    assert catalog.resolve("doubao", modality=ModelModality.CHAT).canonical_model_id == (
        "ecorex-doubao-seed-2.0-pro"
    )
    assert models["ecorex-gemini-3.1-pro"].model_policy.compact_threshold_tokens == 900_000
    assert models["ecorex-doubao-seed-2.0-pro"].model_policy.compact_threshold_tokens == 224_000


def test_gateway_mapping_accepts_only_exact_managed_policy_subsets() -> None:
    full_mapping = {
        policy.local_model_id: policy.upstream_model_id
        for policy in ECOREX_CHAT_MODEL_POLICIES
    }
    require_managed_chat_mapping(full_mapping)
    require_managed_chat_mapping({"ecorex-chat": "gpt-5.6-sol"})
    with pytest.raises(ValueError, match="violates managed policy"):
        require_managed_chat_mapping({"ecorex-deepseek-v4-pro": "deepseek-chat"})
    with pytest.raises(ValueError, match="violates managed policy"):
        require_managed_chat_mapping({"unknown-model": "unknown-model"})


def test_catalog_is_available_before_a_thread_exists_and_separates_modalities() -> None:
    catalog = _catalog()
    payload = catalog.to_dict()

    assert payload["modalities"]["chat"][0]["model_id"] == "ecorex-chat"
    assert payload["modalities"]["image"][0]["model_id"] == "gpt-image-2"
    assert catalog.resolve(None, modality=ModelModality.IMAGE).canonical_model_id == "gpt-image-2"
    with pytest.raises(ModelModalityMismatch):
        catalog.resolve("image2", modality=ModelModality.CHAT)
    with pytest.raises(UnknownModelError):
        catalog.resolve("bring-your-own-key-model", modality=ModelModality.CHAT)


def test_v1_turn_contract_rejects_the_legacy_generic_model_field() -> None:
    with pytest.raises(ValidationError, match="model"):
        CreateTurnRequest.model_validate(
            {"input": "legacy", "model": "gpt-image-2"}
        )


def _model(**overrides: object) -> ManagedModelSpec:
    values: dict[str, object] = {
        "model_id": "model-one",
        "display_name": "Model One",
        "modalities": frozenset({ModelModality.CHAT}),
    }
    values.update(overrides)
    return ManagedModelSpec(**values)  # type: ignore[arg-type]


def test_model_metadata_is_canonical_and_snapshot_order_independent() -> None:
    first = ManagedModelCatalog(
        (
            _model(
                aliases=("SECOND_ALIAS", "first_alias"),
                capabilities=frozenset({"tool_use", "vision"}),
            ),
        )
    )
    second = ManagedModelCatalog(
        (
            _model(
                aliases=("first-alias", "second-alias"),
                capabilities=frozenset({"vision", "tool-use"}),
            ),
        )
    )

    assert first.snapshot_id == second.snapshot_id
    assert first.for_modality(ModelModality.CHAT)[0].aliases == (
        "second-alias",
        "first-alias",
    )
    assert first.resolve("SECOND_ALIAS", modality=ModelModality.CHAT).canonical_model_id == (
        "model-one"
    )


@pytest.mark.parametrize(
    "overrides",
    (
        {"model_id": "bad\nmodel"},
        {"display_name": "bad\u202ename"},
        {"aliases": ("bad\u200balias",)},
        {"capabilities": frozenset({"bad\tcapability"})},
        {"model_id": "bad\ud800model"},
        {"display_name": "bad\ud800name"},
    ),
)
def test_model_metadata_rejects_controls_and_invalid_unicode(
    overrides: dict[str, object],
) -> None:
    with pytest.raises(ValueError):
        _model(**overrides)


def test_model_metadata_enforces_utf8_byte_boundaries() -> None:
    assert _model(display_name="图" * 85).display_name == "图" * 85
    with pytest.raises(ValueError, match="display_name"):
        _model(display_name="图" * 86)
    assert len(_model(model_id="m" * 128).model_id.encode("utf-8")) == 128
    with pytest.raises(ValueError, match="model_id"):
        _model(model_id="m" * 129)


def test_model_metadata_enforces_alias_and_capability_bounds() -> None:
    with pytest.raises(ValueError, match="aliases exceed"):
        _model(aliases=tuple(f"alias-{index}" for index in range(33)))
    with pytest.raises(ValueError, match="capabilities exceed"):
        _model(capabilities=frozenset(f"cap-{index}" for index in range(65)))
    with pytest.raises(ValueError, match="aliases must be unique"):
        _model(aliases=("image-2", "image_2"))
    with pytest.raises(ValueError, match="capabilities must be unique"):
        _model(capabilities=frozenset({"image-edit", "image_edit"}))


def test_model_catalog_enforces_count_and_normalized_reference_uniqueness() -> None:
    with pytest.raises(ValueError, match="product limit"):
        ManagedModelCatalog(
            _model(model_id=f"model-{index:03d}") for index in range(257)
        )
    with pytest.raises(ValueError, match="duplicate model alias"):
        ManagedModelCatalog(
            (
                _model(model_id="image-2"),
                _model(model_id="image_2", display_name="Other"),
            )
        )


def test_cloud_revision_is_the_same_executable_and_bootstrap_catalog_snapshot() -> None:
    base = builtin_model_catalog()
    cloud = {
        "schema_version": 1,
        "models": ["ecorex-chat", "gpt-image-2"],
        "catalog": [
            {
                "config_id": "config-chat",
                "revision": 7,
                "local_model_id": "ecorex-chat",
                "modality": "chat",
                "display_name": "EcoreX Main · July",
                "upstream_model_id": "gpt-5.6-sol-july",
                "provider_preset": "openai_responses",
                "is_default": True,
            },
            {
                "config_id": "config-image",
                "revision": 3,
                "local_model_id": "gpt-image-2",
                "modality": "image_generation",
                "display_name": "Image 2 Production",
                "upstream_model_id": "image-2-production",
                "provider_preset": "openai_images",
                "is_default": True,
            },
        ],
    }

    executable = _managed_catalog_from_cloud(base, cloud)
    bootstrap = project_model_catalog(executable)
    chat = executable.get("ecorex-chat")

    assert bootstrap.snapshot_id == executable.snapshot_id
    assert bootstrap.chat[0].display_name == "EcoreX Main · July"
    assert chat.model_policy is not None
    assert chat.model_policy.upstream_model_id == "gpt-5.6-sol-july"
    assert chat.model_policy.compact_threshold_tokens == 272_000
    assert executable.snapshot_id != base.snapshot_id
