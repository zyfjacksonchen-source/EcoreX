---
name: image-generation
description: Shape and verify image-generation or image-edit workflows that use the native e-Mate imagegen tool.
compatibility-id: image-generation
adopts-official-skill: imagegen
ecorex-native-facade: true
quality-gates:
  - project-safe-output
  - structural-image-qa
  - reference-fidelity-qa
  - targeted-retry
---

# Native Image Generation Workflow

Use the native `imagegen` tool for the final bitmap. Runtime-owned ToolSpec,
model catalog, Pack availability and permissions are authoritative; this Skill
does not select providers, API keys, models or installation commands.

## Choose the operation

- With no source image, treat the request as a new generation.
- When the user asks to edit, preserve, combine or follow an existing image,
  pass its authenticated Artifact or attachment identity to `imagegen`.
- Label each source mentally as the edit target, a visual reference, or a
  compositing input. Preserve the target's identity and layout unless the user
  asks to replace them.

## Shape the native request

- Put the complete visual brief in `instruction`: subject, composition, style,
  lighting, palette, required text and constraints.
- Use `reference_artifact_ids` for existing e-Mate image Artifacts and
  `attachment_ids` for images attached to the current Turn.
- Set `size` or `quality` only when the user requests them or the deliverable
  clearly requires them. Do not invent provider-specific parameters.
- Call `imagegen` once for one requested result. Split genuinely different
  deliverables into separate task steps instead of inventing a batch schema.

## Verify and recover

- Confirm the result is a decodable, non-empty image and that the requested
  subject, composition, text and edit preservation are present.
- When references were supplied, compare subject identity, layout, style,
  colour and requested changes before delivery.
- If a result has a specific visible defect, retry with a targeted correction
  that names that defect. Do not repeat an unchanged request.
- If Runtime reports the image Pack, model or service unavailable, follow the
  structured recovery choices. Do not run npm, pip, provider HTTP scripts,
  Pillow, HTML, SVG or canvas as a substitute for final image generation.

## Deliver

Return the Runtime-created image Artifact and briefly identify any incomplete
constraint. Do not expose provider temporary paths, credentials or internal
orchestration responses.
