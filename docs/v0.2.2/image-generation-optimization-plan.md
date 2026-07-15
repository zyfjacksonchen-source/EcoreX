# v0.2.2 Image Generation Optimization Slice

Source thread: `019ef9a8-7344-7712-beb9-f9008dd90622`.

## Priority Rule

This slice is subordinate to the v0.2.2 core runtime direction:

- Backend runtime events remain the source of truth.
- Frontend consumes backend projection and does not invent separate image-task state.
- Image progress, retries, fallback, artifacts, and failures must be observable, replayable, and auditable.
- If image-specific speed or compatibility work conflicts with durable runtime projection, the durable runtime projection wins.

## Objective

Improve image generation/editing perceived latency, correctness, and auditability without pretending that normal provider latency can be eliminated. The target is to remove wrong routing, repeated slow retries, serial waste, and invisible work, while exposing OCR, queue, provider call, download, save, artifact, fallback, and failure progress through the same Codex-like runtime projection.

## Planned Work

1. Provider routing and config
   - Add explicit image config keys: `image_provider`, `image_model`, `image_api_key`, `image_api_base`, `image_request_timeout_seconds`, `image_max_retries`, `image_retry_after_cap_seconds`, `image_async_enabled`, `image_job_max_parallel`, and `image_provider_concurrency`.
   - Support `custom` OpenAI-compatible image provider in the image-generation skill, using `CUSTOM_API_KEY/CUSTOM_API_BASE` or explicit image config.
   - Keep OpenAI/LinkAI defaults on `gpt-image-2-pro`; do not force `gpt-image-2-pro` onto custom providers without explicit configuration.

2. Async image jobs
   - Add an `ImageJobService` with `start`, `status`, `collect`, and `cancel`.
   - Web/Desktop uses async jobs by default; legacy non-SSE/IM paths keep synchronous compatibility.
   - Multi-image requests split into independent image tasks with bounded concurrency: `min(count, image_job_max_parallel, provider_concurrency)`.
   - Avoid provider `n>1` by default because provider behavior differs and incremental artifacts are clearer.

3. Scenario orchestration
   - Single image generation: one task, one provider call, one artifact.
   - Multi-image generation: N independent tasks, bounded parallelism, each artifact emitted as soon as it is saved.
   - Single image edit: load/compress input, call `/images/edits` or provider equivalent, emit one artifact.
   - Multi-image edit: either a single fused-reference task with `image_url[]`, or independent edit tasks when the intent is per-image edits or variants.
   - Multi-intent workflows: build an intent DAG with `intent_id`, `step_id`, and `asset_index` so text, OCR, generation, and dependent edit nodes can interleave while preserving UI order.
   - OCR-before-generation: cache OCR/vision briefs by input image hash and reuse them across multiple generated outputs.

4. Observability
   - Emit `image_job.started`, `image_job.progress`, `image_job.artifact`, `image_job.completed`, `image_job.failed`, and `image_job.cancelled` as canonical runtime events.
   - Surface progress through existing `phase`, `tool_heartbeat`, `artifact.created`, and terminal projection paths.
   - Add image/model call span fields: `operation`, `source`, `provider`, `api_key_source`, `api_base_host_hash`, `resolved_model`, `image_mode`, `input_image_count`, `output_count`.
   - Track timings: `ocr_ms`, `prompt_plan_ms`, `input_load_ms`, `input_compress_ms`, `provider_request_ms`, `poll_wait_ms`, `download_ms`, `decode_validate_ms`, `save_ms`, `retry_sleep_ms_total`, `fallback_overhead_ms`, `total_latency_ms`.

5. Alerting thresholds
   - Single image job over 90s: warning.
   - Single image job over 180s: critical.
   - Five-minute p95 over 120s: warning.
   - Retry exhausted rate over 5%: warning.
   - Fallback ratio over 10%: warning.
   - `Retry-After` above cap is recorded but not slept in full.

## Acceptance And Harness

- Provider config:
  - Legacy `create_img()` and skill provider both resolve custom key/base without leaking real keys.
  - Old `open_ai_api_key/open_ai_api_base` behavior remains unchanged.
  - Custom provider does not default to `gpt-image-2-pro` unless explicitly configured.

- Timeout/retry:
  - `Retry-After: 80` is capped by `image_retry_after_cap_seconds`.
  - Custom provider does not repeat slow 80s retries by default.
  - Existing OpenAI/LinkAI `gpt-image-2-pro -> gpt-image-2` fallback behavior remains covered.

- Scenario tests:
  - Single image generation.
  - Multi-image bounded parallel generation.
  - Single image edit.
  - Multi-image fused-reference edit.
  - Multi-image per-image edit.
  - Multi-intent DAG with dependent image step.
  - OCR brief reuse before multi-output generation.

- Observability tests:
  - Every image job emits started/progress/completed or failed/cancelled events.
  - Artifacts are emitted incrementally per completed image.
  - RuntimeProjection reconstructs interrupted image job progress and completed artifacts.
  - Terminal errors preserve provider, model, retry/fallback state, and sanitized failure text.

- Compatibility tests:
  - Non-SSE channels still return final `ReplyType.IMAGE_URL`.
  - Web/Desktop can refresh or reconnect without duplicate image artifacts.
  - Cancel stops queued tasks and marks in-flight tasks as cancelled when possible.
