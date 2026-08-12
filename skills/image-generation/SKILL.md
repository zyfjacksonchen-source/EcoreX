---
name: image-generation
description: Generate or edit images from text prompts with the native e-Mate imagegen tool.
compatibility-id: image-generation
adopts-official-skill: imagegen
ecorex-native-facade: true
quality-gates:
  - project-safe-output
  - structural-image-qa
  - reference-fidelity-qa
  - targeted-retry
---

# Image Generation

Use the native `imagegen` tool. Its contract follows CowAgent while e-Mate
keeps the image model fixed in the Runtime, so never pass or select a model.

## Parameters

- `prompt` is required. Include the subject, composition, style, lighting,
  palette, required text, and edit constraints.
- `image_url` is optional. Pass one local path, HTTP(S) URL, `attachment_id`,
  `artifact_id`, or prior result URL to edit an image. Pass an ordered list to
  combine multiple references. Without it, generate a new image.
- `quality` is `low`, `medium`, `high`, or `auto`. Omit it unless the user or
  deliverable requires a specific quality.
- `size` accepts `512`, `1K`, `2K`, `3K`, `4K`, or a pixel size such as
  `1024x1024`.
- `aspect_ratio` accepts ratios such as `1:1`, `3:2`, `2:3`, `16:9`, `9:16`,
  or `21:9`.

For two to eight independent outputs, make one call with an ordered `tasks`
array. Each task uses the same fields and the Runtime executes them with
bounded concurrency. Never mix top-level image fields with `tasks`.

The result always includes CowAgent-compatible `model` and `images[].url`
fields and e-Mate Artifact identities. Show the successful images to the user.
For a partial batch failure, preserve completed siblings and retry only the
failed task with a corrected prompt. Do not retry an unchanged provider or
configuration failure.
