# S4b - Web Multi-Model Selection

## Goal

Add Web chat model selection without changing image generation routing. The chat selector must be driven by admin-managed model configuration, show only one strongest model per provider, preserve the existing `gpt-5.5` chat model, and keep image generation pinned to `gpt-image-2-pro`.

## Scope

- Web model capability API: `ModelsHandler._chat_capability()` and `_handle_set_capability({"capability": "chat"})`.
- Renderer model selector in the chat composer.
- Shared model capability metadata and context-window policy.
- Token estimation used by automatic context compaction.

Desktop shell and Electron sidecar behavior are out of scope except for the existing renderer bundle used by the Web app.

## Design

- Chat providers are still configured through the admin/config surface.
- `/api/models` exposes `capabilities.chat.model_options`.
- Each configured provider contributes only one chat model. The selected model must be the strongest model that is actually usable with the configured credentials; stronger provider models that fail entitlement checks stay in diagnostics instead of the primary switcher.
- OpenAI `gpt-5.5` is always preserved as the canonical EcoreX chat model entry. It can be configured by the normal admin-managed model policy cache; if no usable OpenAI credential exists, the option is retained with `configured=false` and `hint=needs credentials` instead of disappearing.
- The current model is sorted first, then configured providers, then retained unconfigured entries.
- Switching chat model writes only chat keys (`model`, `bot_type`, provider routing, context policy). It does not update image-generation keys.
- Image generation continues to report `gpt-image-2-pro` through the image capability and remains outside the chat switcher.
- Context policy follows the selected chat model:
  - `gpt-5.5`: 1,000,000 context window, 800,000 auto-compact threshold, local tokenizer.
  - DeepSeek V4 Pro: 1,000,000 context window, 800,000 auto-compact threshold, conservative estimator.
  - Gemini 3.1 Pro Preview: 1,048,576 context window, 838,860 auto-compact threshold, conservative estimator.
  - Doubao Seed 2.0 Pro: 256,000 context window, 204,800 auto-compact threshold, conservative estimator.

## Changes

- Added `ModelContextPolicy` and provider-aware context policies in `models/model_capabilities.py`.
- Added provider-aware local token estimation in `models/token_estimator.py`.
- Synced agent context budgets from `model_auto_compact_token_limit`.
- Added Web chat model option contract and deterministic chat switching response.
- Added renderer chat model menu, switch divider, dynamic context meter, and provider model icons.
- Restored local current chat model to `gpt-5.5`; the active OpenAI key/base can come from the admin-managed enterprise model policy cache.
- Kept `text_to_image` and image-generation model at `gpt-image-2-pro`.
- Rejected unconfigured chat-model switch requests on the backend and disabled unavailable model rows in the Web selector.
- Removed the previous frontend display alias that could render `deepseek-*` model names as `gpt-5.5`.
- Kept release-local runtime configs secret-clean; multi-vendor keys are tested through the local key file/runtime config, not baked into the package.

## Acceptance

- `/api/models` returns `current_provider=openai`, `current_model=gpt-5.5`, `capabilities.provider=openai`, `max_tokens_param=max_completion_tokens`, and image model `gpt-image-2-pro` in the local release runtime smoke.
- The release-local package remains secret-clean, so it only shows the admin-configured OpenAI option during package smoke.
- Real multi-vendor connectivity smoke verifies the actual menu set: `openai:gpt-5.5`, `deepseek:deepseek-v4-pro`, `gemini:gemini-3.1-pro-preview`, `doubao:doubao-seed-2-0-pro-260215`.
- The `openai:gpt-5.5` connectivity smoke must report `credentialSource=admin_policy_cache` or `runtime_config`; the local multi-model key note file is not allowed to satisfy the OpenAI credential boundary.
- Renderer contract includes provider-specific model icons before model labels, not the generic bot icon in the selector rows.
- Renderer contract shows the real selected model name; it must not alias `deepseek-*` or other provider models to `gpt-5.5`.
- Chat model switch inserts a conversation divider: `已切换 xx 模型`.
- Image generation is not coupled to chat model selection.
- Tests pass:
  - `python -m py_compile ...`
  - `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest tests/test_models_handler.py tests/test_model_capabilities.py tests/test_web_runtime_goal.py -q`
  - `npm run typecheck -- --pretty false`
  - `npm run build:renderer`

## Residual Notes

- `gpt-5.5` uses the key/base already configured by the admin management page via the cached enterprise model policy. The package config itself still contains no OpenAI secret, and the connectivity smoke no longer falls back to the local multi-model key note file for OpenAI.
- The local Doubao key did not have access to `doubao-seed-2.1-pro` during provider smoke; `doubao-seed-2-0-pro-260215` was reachable and is therefore the Doubao menu entry for this release. This is a provider entitlement/configuration boundary, not a runtime dependency failure.
