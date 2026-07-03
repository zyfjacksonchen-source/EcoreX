---
name: image-generation
description: Generate or edit images from text prompts. Use when the user asks to create, draw, design, or edit an image, illustration, photo, icon, poster, or any visual content.
compatibility-id: image-generation
adopts-official-skill: imagegen
ecorex-native-facade: true
quality-gates:
  - project-safe-output
  - structural-image-qa
  - vision-anomaly-qa
  - reference-fidelity-qa
  - retry-ledger
metadata:
  cowagent:
    requires:
      anyEnv:
        - OPENAI_API_KEY
        - GEMINI_API_KEY
        - ARK_API_KEY
        - DASHSCOPE_API_KEY
        - MINIMAX_API_KEY
        - LINKAI_API_KEY
---

# Image Generation

This is the EcoreX-native compatibility facade for the official Codex
`imagegen` workflow. Keep the public EcoreX skill ID `image-generation` stable
and keep EcoreX multi-provider routing as the runtime default. Use the official
`imagegen` skill as the authoritative workflow reference for prompt shaping,
input-image role labeling, project-safe output handling, transparent-background
fallback policy, and validation discipline when it is available in
`<available_skills>`.

If both skills are visible, read this skill first for EcoreX provider/runtime
rules, then read `imagegen` for workflow and QA details. Do not replace
EcoreX's OpenAI/Gemini/Ark/DashScope/MiniMax/LinkAI routing with a single
Codex built-in image path unless the user explicitly asks for that host tool.

Generate and edit images using AI models. The default route uses `gpt-image-2-pro` final image generation, preferring OpenAI and using LinkAI only as a GPT Image compatible route when OpenAI is not configured. If `gpt-image-2-pro` is unavailable, the runtime may visibly retry the same GPT Image compatible route with `gpt-image-2`. **Do not create final images by coding HTML/canvas/SVG/Pillow layouts; use the native image model API route for real image generation.**

In EcoreX Web, the native `imagegen` tool is the only final image generation
route. Use it for text-to-image, image edits, reference-image generation,
multi-image fusion, and batch generation. Pass existing images as `image_url`
or `image_urls`; the runtime normalizes them into the GPT Image edit/reference
route and starts with `gpt-image-2-pro`. Do not replace generation or edits
with shell/Python/PIL/HTML/SVG/canvas scripts, direct provider HTTP scripts,
web search, or network image scraping. `scripts/generate.py` is an in-process
provider runtime module behind the native facade, not a user-facing Python CLI
route.

Supported models (passed via `model` only when the user asks for a specific one):

- **OpenAI** — `gpt-image-2-pro`, `gpt-image-2`, `gpt-image-1`
- **Gemini Nano Banana** — `nano-banana-2`, `nano-banana-pro`, `nano-banana`
- **Seedream (Volcengine Ark)** — `seedream-5.0-lite`, `seedream-4.5`
- **Qwen (DashScope)** — `qwen-image-2.0`, `qwen-image-2.0-pro`
- **MiniMax** — `image-01`

## Usage

Prefer the native runtime tool:

```json
{"prompt": "A corgi astronaut floating in space"}
```

For edits or reference-image generation, always include image inputs:

```json
{"prompt": "Add a Santa hat to the dog", "image_url": "/path/to/dog.png"}
```

```json
{"prompt": "Combine these characters into a group photo", "image_urls": ["/path/a.png", "/path/b.png"]}
```

For batch generation, call `imagegen` once with a `tasks` array. Do not write a
shell/Python loop:

```json
{
  "tasks": [
    {"prompt": "A product hero image, white background", "aspect_ratio": "1:1"},
    {"prompt": "A social banner version", "aspect_ratio": "16:9"}
  ]
}
```

For offline diagnostics outside EcoreX Web, the provider runtime module may be
invoked by maintainers directly. In Web runtime, always call the native
`imagegen` tool so route telemetry reports `in_process_provider_runner` and no
Python CLI fallback is used.

### Parameters

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `prompt` | string | yes | — | Image description |
| `image_url` | string / list | no | null | Input image(s) for editing/reference generation: local file path or URL. Multi-image fusion is supported (pass a list) |
| `image_urls` | list | no | null | Alias for multiple input/reference images; normalized to `image_url` by the runtime |
| `quality` | string | no | auto | `low` / `medium` / `high` (only some backends honour this) |
| `size` | string | no | auto | `512` / `1K` / `2K` / `3K` / `4K`, or pixel value (`1024x1024`) |
| `aspect_ratio` | string | no | null | `1:1` / `3:2` / `2:3` / `16:9` / `9:16` / `21:9` (some backends also support extreme ratios like `1:4` / `8:1`) |

**Higher `quality` and larger `size` cost more and run slower.** In normal cases, when the user does not explicitly specify, `low` or `medium` is sufficient. Only use `high` when the user asks for it.

### Example — generate

```json
{"prompt": "A corgi astronaut floating in space"}
```

With aspect ratio:

```json
{"prompt": "Isometric miniature city of Shanghai at sunset", "size": "2K", "aspect_ratio": "16:9"}
```

### Important: Editing vs Generating

When the user asks to **edit, modify, improve, or generate from an existing/reference image**, pass the original image via `image_url` or `image_urls`. Prefer **local file paths** directly — the runtime handles file reading internally. Without image inputs, the request is a brand-new text-to-image generation instead of an edit/reference generation.

For every input image, label its role before generation:

- edit target: an image whose identity/layout should be preserved while modifying it
- reference image: style, composition, mood, subject, or fidelity guide
- supporting insert/compositing input: asset to merge into the final image

If the user gives a reference image and asks the final image to match it,
perform a post-generation fidelity check before delivery. Compare subject,
composition, style, color/lighting, text/layout, and requested edit
preservation. Record the result in the image job metadata when the runtime
supports it.

### Example — edit (image-to-image)

```json
{"prompt": "Add a Santa hat to the dog", "image_url": "/path/to/dog.png"}
```

Multi-image fusion — pass a list:

```json
{"prompt": "Combine these characters into a group photo", "image_url": ["/path/a.png", "/path/b.png"]}
```

### Output

Prints JSON to stdout:

```json
{
  "model": "gpt-image-2-pro",
  "images": [
    {"url": "/path/to/output.png"}
  ]
}
```

After success, display the image to the user. You can either embed it in markdown (`![description](/path/to/output.png)`) or use the `send` tool.

For project-bound assets, save or move the selected final image into the
workspace and reference that workspace path. Do not leave a project asset only
in a provider temp directory.

Before final delivery, inspect generated images for structural defects:

- decode/open failure, zero-byte or truncated files
- blank/near-blank output
- obvious seams, discontinuities, repeated ghost layers, multi-layer overlays, or pasted-looking regions
- garbled text where text was requested, visible watermark/signature artifacts, and broken subject anatomy/object geometry
- mismatch against reference images when references were supplied

If a defect is found and the request is still satisfiable, retry with a targeted
prompt or provider adjustment and keep a retry ledger. Do not silently ship a
known broken image.

On error:

```json
{
  "error": "error message"
}
```

### Setup

The script needs **at least one** of these API keys (set via `env_config` or `config.json`):

`OPENAI_API_KEY` / `GEMINI_API_KEY` / `ARK_API_KEY` / `DASHSCOPE_API_KEY` / `MINIMAX_API_KEY` / `LINKAI_API_KEY`

Each also has an optional `*_API_BASE` for custom endpoints. By default, the script sends final image generation to `gpt-image-2-pro`. OpenAI is preferred; LinkAI may be used only as a GPT Image compatible route when OpenAI is not configured. If pro is unavailable or exhausted after retry, the script may downgrade only to `gpt-image-2` on the same GPT Image compatible provider and reports that in `model_fallback`. Do not rely on automatic downgrade to another model family for default image generation.

### Error Handling

If the script returns an error after trying all configured backends, **do NOT retry with the same parameters** — the failure is almost always a configuration issue (wrong API key, unsupported API base). Tell the user to fix it via `env_config`, then retry.

### Notes

- OpenAI default mode starts with `gpt-image-2-pro`. If the API reports model/access unavailability or retryable pro failure after retry exhaustion, automatically retry once with `gpt-image-2`; surface `model_fallback` in the result. Do not fall back to Python/PIL/HTML/SVG or to a different model family.
- LinkAI uses the same `gpt-image-2-pro` default when OpenAI is unavailable or when the user explicitly chooses LinkAI; it may use the same visible `gpt-image-2` fallback. legacy `image-2-pro` input is normalized as a compatibility alias and must not be recommended as the default.
- For GPT Image models, use the official Images API parameters (`model`, `prompt`, `n`, `size`, `quality`, `output_format`, `background`, `moderation`). Do not send `response_format`.
- OpenAI requests without `image_url` use `/images/generations`; requests with `image_url` use `/images/edits` with multipart `image` / `image[]`.

- HTTP timeout is 300s — high-resolution generation can take over 200s.
- Omit `quality` / `size` to let the model pick automatically (`auto`).
- Input images for editing are auto-compressed to ≤ 4MB / longest edge ≤ 4096px.
